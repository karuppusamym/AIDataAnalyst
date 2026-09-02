"""N17: promoted exemplars as a context-product section.

Mirrors `tests/test_context_product_negative_knowledge.py` (N16) exactly,
the pattern this row is explicitly asked to follow: the same determinism,
scope, and target-selection proof, applied to `exemplars[]`
(`aida.context_compiler`'s `ResolvedExemplar`) instead of
`negative_knowledge[]`.

Three things are exercised:

1. Determinism -- the same exemplar state compiled twice produces
   byte-identical section content and `artifact_hash`
   (`compile_context_product` stays a pure function of its arguments).
2. Scope -- `aida.exemplar_store.find_confirmed_agent_runs` and
   `aida.context_compiler_api._load_exemplars` (the glue that feeds it into
   compilation) return only exemplars whose *own resolved TABLE objects*
   touch the given context-product version's table scope; a confirmed run
   over an out-of-scope table never comes back, and an unconfirmed
   (non-`ELIGIBLE`) run never gets promoted at all.
3. Target selection -- the section appears in MCP/REST/YAML (Atlas-native
   envelopes) and is absent from OSI/ODCS/SNOWFLAKE_SEMANTIC_VIEW/
   DATABRICKS_METRIC_VIEW (vendor-standard schemas with no equivalent
   field).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.context_compiler import ResolvedExemplar, ResolvedTableReference, compile_context_product
from aida.context_compiler_api import _load_exemplars
from aida.db import Base
from aida.models import AgentRun, ContextProduct, ContextProductVersion, QueryMemoryEvidence
from tests.context_path_eval.scenario import ORDER_LOOKUP_TOOL_SLUG, build_scenario

# `asyncio_mode = "auto"` (pyproject.toml) runs every `async def test_*` on
# its own -- this file mixes pure (no DB) and DB-backed tests.


def _fixture(
    table_id: str,
) -> tuple[ContextProduct, ContextProductVersion, list[ResolvedTableReference]]:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    product = ContextProduct(
        id=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        product_key="orders_context",
        lifecycle_status="ACTIVE",
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Orders context",
        description="Approved orders metadata context.",
        purpose="Support bounded order analysis.",
        owner_principal="orders-owner",
        table_ids=[table_id],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=[],
        allowed_consumer_roles=["Analyst"],
        lineage_depth=2,
        quality_requirements={"minimum_score": 80},
        policy_summary={"source_values": "GATEWAY_ONLY"},
        fingerprint="b" * 64,
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    tables = [ResolvedTableReference(table_id=table_id, qualified_name="DB.COMMERCE.ORDERS")]
    return product, version, tables


def _exemplar(case_id: str = "confirmed-run-1") -> ResolvedExemplar:
    return ResolvedExemplar(
        case_id=case_id,
        source="CONFIRMED_RUN",
        resolved_object_types=("GOVERNED_TOOL", "TABLE"),
        selected_tool_slug="order_lookup",
        semantic_version_kind="technical-metadata",
        policy_status="COMPLETED",
        policy_reason_code="MISSING_TOOL_PARAMETERS:customer_id",
        artifact_hash="deadbeef" * 8,
    )


# ---------------------------------------------------------------------------
# 1. Determinism
# ---------------------------------------------------------------------------


def test_exemplars_section_is_deterministic() -> None:
    table_id = str(uuid4())
    product, version, tables = _fixture(table_id)

    # Two independently-built lists carrying the same values -- not the same
    # object -- to prove the hash tracks content, not identity.
    first = compile_context_product(product, version, "REST", tables, None, [_exemplar()])
    second = compile_context_product(product, version, "REST", tables, None, [_exemplar()])

    assert first.content == second.content
    assert first.artifact_hash == second.artifact_hash

    third = compile_context_product(product, version, "REST", tables, None, [_exemplar()])
    assert third.artifact_hash == first.artifact_hash


def test_exemplars_absence_is_also_deterministic() -> None:
    table_id = str(uuid4())
    product, version, tables = _fixture(table_id)

    without_arg = compile_context_product(product, version, "MCP", tables)
    with_empty_list = compile_context_product(product, version, "MCP", tables, None, [])

    assert without_arg.artifact_hash == with_empty_list.artifact_hash
    assert '"exemplars"' in without_arg.content
    assert '"count": 0' in without_arg.content


# ---------------------------------------------------------------------------
# 2. Scope
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_run(
    db: AsyncSession,
    *,
    organization_id: object,
    datasource_id: object,
    table_id: str,
    tool_id: str,
    tool_version_id: object,
    memory_status: str,
) -> AgentRun:
    agent_run = AgentRun(
        organization_id=organization_id,
        datasource_id=datasource_id,
        principal_id="analyst@bank.com",
        status="COMPLETED",
        question_hash="q" * 64,
        generation_source="TOOL",
        semantic_version=f"technical-metadata:{datasource_id}",
        plan_evidence={
            "strategy": "CLARIFICATION",
            "selected_tool_version_id": str(tool_version_id),
            "tool_decisions": [
                {"tool_version_id": str(tool_version_id), "decision": "SELECTED"}
            ],
        },
        retrieval_evidence=[
            {"object_type": "TABLE", "object_id": table_id},
            {"object_type": "GOVERNED_TOOL", "object_id": tool_id},
        ],
        recommended_tool_version_id=tool_version_id,
        failure_reason="MISSING_TOOL_PARAMETERS:customer_id",
    )
    db.add(agent_run)
    await db.flush()
    db.add(
        QueryMemoryEvidence(
            organization_id=organization_id,
            datasource_id=datasource_id,
            agent_run_id=agent_run.id,
            query_execution_id=uuid4(),
            question_hash=agent_run.question_hash,
            sql_hash="s" * 64,
            semantic_version=agent_run.semantic_version,
            status=memory_status,
            positive_feedback_count=1 if memory_status == "ELIGIBLE" else 0,
        )
    )
    await db.flush()
    return agent_run


async def test_load_exemplars_excludes_out_of_scope_and_unconfirmed_runs(
    session: AsyncSession,
) -> None:
    scenario = await build_scenario(session)
    tool_version = scenario.tool_version_by_slug[ORDER_LOOKUP_TOOL_SLUG]
    in_scope_table = str(scenario.fact_orders.id)
    out_of_scope_table = str(uuid4())
    tool_id = str(tool_version.tool_id)

    in_scope_confirmed = await _seed_run(
        session,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        table_id=in_scope_table,
        tool_id=tool_id,
        tool_version_id=tool_version.id,
        memory_status="ELIGIBLE",
    )
    await _seed_run(
        session,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        table_id=out_of_scope_table,
        tool_id=tool_id,
        tool_version_id=tool_version.id,
        memory_status="ELIGIBLE",
    )
    await _seed_run(
        session,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        table_id=in_scope_table,
        tool_id=tool_id,
        tool_version_id=tool_version.id,
        memory_status="OBSERVED",  # no confirmation yet -- must never be promoted
    )

    exemplars = await _load_exemplars(session, scenario.organization.id, [in_scope_table])

    assert [item.case_id for item in exemplars] == [f"confirmed-run-{in_scope_confirmed.id}"]
    assert exemplars[0].resolved_object_types == ("GOVERNED_TOOL", "TABLE")


async def test_load_exemplars_feeds_scoped_exemplars_into_compilation(
    session: AsyncSession,
) -> None:
    """End-to-end glue: `context_compiler_api._load_exemplars` resolves DB
    state into `ResolvedExemplar`s that compile straight into the artifact.
    """
    scenario = await build_scenario(session)
    tool_version = scenario.tool_version_by_slug[ORDER_LOOKUP_TOOL_SLUG]
    in_scope_table = str(scenario.fact_orders.id)
    confirmed_run = await _seed_run(
        session,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        table_id=in_scope_table,
        tool_id=str(tool_version.tool_id),
        tool_version_id=tool_version.id,
        memory_status="ELIGIBLE",
    )

    product, version, tables = _fixture(in_scope_table)
    version.organization_id = scenario.organization.id

    exemplars = await _load_exemplars(session, scenario.organization.id, version.table_ids)
    assert [item.case_id for item in exemplars] == [f"confirmed-run-{confirmed_run.id}"]

    compiled = compile_context_product(product, version, "MCP", tables, None, exemplars)
    assert f"confirmed-run-{confirmed_run.id}" in compiled.content
    assert "order_lookup" in compiled.content


# ---------------------------------------------------------------------------
# 3. Target selection
# ---------------------------------------------------------------------------


def test_exemplars_present_only_on_atlas_native_targets() -> None:
    table_id = str(uuid4())
    product, version, tables = _fixture(table_id)
    exemplars = [_exemplar()]

    for target, parse in [
        ("MCP", "json"),
        ("REST", "json"),
        ("YAML", "yaml"),
    ]:
        compiled = compile_context_product(product, version, target, tables, None, exemplars)
        parsed = (
            yaml.safe_load(compiled.content) if parse == "yaml" else json.loads(compiled.content)
        )
        context_key = "spec" if target == "YAML" else "context"
        assert "exemplars" in parsed[context_key], target
        assert parsed[context_key]["exemplars"]["count"] == 1

    for target in [
        "OSI",
        "ODCS",
        "SNOWFLAKE_SEMANTIC_VIEW",
        "DATABRICKS_METRIC_VIEW",
    ]:
        compiled = compile_context_product(product, version, target, tables, None, exemplars)
        assert "exemplars" not in compiled.content, target
