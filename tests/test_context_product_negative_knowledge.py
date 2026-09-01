"""N16: negative knowledge as a context-product section.

EE.3 (`aida.negative_knowledge`) already built a queryable "what we decided
is not true" surface (`NegativeAssertionRecord`, re-proposal suppression).
This closes N16 by surfacing that data as a section of a compiled context
product (`aida.context_compiler.compile_context_product`), bounded to the
context product version's own declared table scope and wired through the
same determinism/`artifact_hash` guarantee every other section already has.

Three things are exercised:

1. Determinism -- the same rejected-inference state compiled twice produces
   byte-identical section content and `artifact_hash` (`context_compiler.py`
   level, no DB -- `compile_context_product` is a pure function of its
   arguments).
2. Scope -- `aida.negative_knowledge.query_negatives_for_scope` and
   `aida.context_compiler_api._load_negative_knowledge` (the glue that feeds
   it into compilation) return only assertions touching the given table
   scope; a rejection on a table outside that scope never comes back.
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

from aida.context_compiler import (
    ResolvedNegativeAssertion,
    ResolvedTableReference,
    compile_context_product,
)
from aida.context_compiler_api import _load_negative_knowledge
from aida.db import Base
from aida.models import ContextProduct, ContextProductVersion, NegativeAssertionRecord, Organization
from aida.negative_knowledge import query_negatives_for_scope

# `asyncio_mode = "auto"` (pyproject.toml) runs every `async def test_*` on
# its own -- this file mixes pure (no DB) and DB-backed tests.


def _fixture() -> tuple[ContextProduct, ContextProductVersion, list[ResolvedTableReference]]:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    product = ContextProduct(
        id=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        product_key="risk_context",
        lifecycle_status="ACTIVE",
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    table_id = str(uuid4())
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Risk context",
        description="Approved risk metadata context.",
        purpose="Support bounded portfolio risk analysis.",
        owner_principal="risk-owner",
        table_ids=[table_id],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=[],
        allowed_consumer_roles=["Analyst"],
        lineage_depth=2,
        quality_requirements={"minimum_score": 80},
        policy_summary={"source_values": "GATEWAY_ONLY"},
        fingerprint="a" * 64,
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    tables = [ResolvedTableReference(table_id=table_id, qualified_name="DB.RISK.EXPOSURE")]
    return product, version, tables


def _rejection(table_id: str) -> ResolvedNegativeAssertion:
    return ResolvedNegativeAssertion(
        subject_id=f"table:{table_id}",
        assertion_type="INFERENCE_REJECTED",
        predicate={"domain": "payments", "entity": "fraud_flag"},
        rejected_by="steward@bank.com",
        rejected_at="2026-08-20T12:00:00+00:00",
        suppression_active=True,
        lift_reason=None,
    )


# ---------------------------------------------------------------------------
# 1. Determinism
# ---------------------------------------------------------------------------


def test_negative_knowledge_section_is_deterministic() -> None:
    product, version, tables = _fixture()
    rejections = [_rejection(tables[0].table_id)]

    # Two independently-built lists carrying the same values -- not the same
    # object -- to prove the hash tracks content, not identity.
    first = compile_context_product(
        product, version, "REST", tables, [_rejection(tables[0].table_id)]
    )
    second = compile_context_product(
        product, version, "REST", tables, [_rejection(tables[0].table_id)]
    )

    assert first.content == second.content
    assert first.artifact_hash == second.artifact_hash

    third = compile_context_product(product, version, "REST", tables, rejections)
    assert third.artifact_hash == first.artifact_hash


def test_negative_knowledge_absence_is_also_deterministic() -> None:
    product, version, tables = _fixture()

    without_arg = compile_context_product(product, version, "MCP", tables)
    with_empty_list = compile_context_product(product, version, "MCP", tables, [])

    assert without_arg.artifact_hash == with_empty_list.artifact_hash
    assert '"negative_knowledge"' in without_arg.content
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


async def _seed_org(db: AsyncSession) -> Organization:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(organization)
    await db.flush()
    return organization


async def test_query_negatives_for_scope_excludes_out_of_scope_subject(
    session: AsyncSession,
) -> None:
    organization = await _seed_org(session)
    in_scope_table = str(uuid4())
    out_of_scope_table = str(uuid4())
    now = datetime.now(UTC)

    session.add_all(
        [
            NegativeAssertionRecord(
                organization_id=organization.id,
                assertion_type="INFERENCE_REJECTED",
                subject_id=f"table:{in_scope_table}",
                predicate={"entity": "fraud_flag"},
                evidence={},
                rejected_by="steward",
                rejected_at=now,
                suppression_active=True,
                material_change_hash="h1",
            ),
            NegativeAssertionRecord(
                organization_id=organization.id,
                assertion_type="RELATIONSHIP_REJECTED",
                subject_id=in_scope_table,  # bare id, no "kind:" prefix
                predicate={"source": "a", "target": "b"},
                evidence={},
                rejected_by="steward",
                rejected_at=now,
                suppression_active=True,
                material_change_hash="h2",
            ),
            NegativeAssertionRecord(
                organization_id=organization.id,
                assertion_type="INFERENCE_REJECTED",
                subject_id=f"table:{out_of_scope_table}",
                predicate={"entity": "unrelated"},
                evidence={},
                rejected_by="steward",
                rejected_at=now,
                suppression_active=True,
                material_change_hash="h3",
            ),
        ]
    )
    await session.flush()

    results = await query_negatives_for_scope(session, organization.id, [in_scope_table])

    subject_ids = {record.subject_id for record in results}
    assert subject_ids == {f"table:{in_scope_table}", in_scope_table}
    assert f"table:{out_of_scope_table}" not in subject_ids


async def test_query_negatives_for_scope_excludes_lifted_suppression_by_default(
    session: AsyncSession,
) -> None:
    organization = await _seed_org(session)
    table_id = str(uuid4())
    now = datetime.now(UTC)

    session.add(
        NegativeAssertionRecord(
            organization_id=organization.id,
            assertion_type="INFERENCE_REJECTED",
            subject_id=f"table:{table_id}",
            predicate={"entity": "fraud_flag"},
            evidence={},
            rejected_by="steward",
            rejected_at=now,
            suppression_active=False,
            material_change_hash="h1",
        )
    )
    await session.flush()

    active_only = await query_negatives_for_scope(session, organization.id, [table_id])
    everything = await query_negatives_for_scope(
        session, organization.id, [table_id], suppression_active_only=False
    )

    assert active_only == []
    assert len(everything) == 1


async def test_load_negative_knowledge_feeds_scoped_rejections_into_compilation(
    session: AsyncSession,
) -> None:
    """End-to-end glue: `context_compiler_api._load_negative_knowledge`
    resolves DB rows into `ResolvedNegativeAssertion`s that compile straight
    into the artifact, and an out-of-scope rejection never leaks in.
    """
    product, version, tables = _fixture()
    in_scope_table = tables[0].table_id
    out_of_scope_table = str(uuid4())
    organization = Organization(
        id=version.organization_id, name="Bank", slug=f"bank-{uuid4().hex[:8]}"
    )
    session.add(organization)
    await session.flush()
    now = datetime.now(UTC)
    session.add_all(
        [
            NegativeAssertionRecord(
                organization_id=organization.id,
                assertion_type="INFERENCE_REJECTED",
                subject_id=f"table:{in_scope_table}",
                predicate={"entity": "fraud_flag"},
                evidence={"reason": "manual review"},
                rejected_by="steward@bank.com",
                rejected_at=now,
                suppression_active=True,
                material_change_hash="h1",
            ),
            NegativeAssertionRecord(
                organization_id=organization.id,
                assertion_type="INFERENCE_REJECTED",
                subject_id=f"table:{out_of_scope_table}",
                predicate={"entity": "unrelated"},
                evidence={},
                rejected_by="steward@bank.com",
                rejected_at=now,
                suppression_active=True,
                material_change_hash="h2",
            ),
        ]
    )
    await session.flush()

    negative_knowledge = await _load_negative_knowledge(
        session, organization.id, version.table_ids
    )
    assert [item.subject_id for item in negative_knowledge] == [f"table:{in_scope_table}"]

    compiled = compile_context_product(product, version, "MCP", tables, negative_knowledge)
    assert f"table:{in_scope_table}" in compiled.content
    assert f"table:{out_of_scope_table}" not in compiled.content


# ---------------------------------------------------------------------------
# 3. Target selection
# ---------------------------------------------------------------------------


def test_negative_knowledge_present_only_on_atlas_native_targets() -> None:
    product, version, tables = _fixture()
    rejections = [_rejection(tables[0].table_id)]

    for target, parse in [
        ("MCP", "json"),
        ("REST", "json"),
        ("YAML", "yaml"),
    ]:
        compiled = compile_context_product(product, version, target, tables, rejections)
        parsed = (
            yaml.safe_load(compiled.content) if parse == "yaml" else json.loads(compiled.content)
        )
        context_key = "spec" if target == "YAML" else "context"
        assert "negative_knowledge" in parsed[context_key], target
        assert parsed[context_key]["negative_knowledge"]["count"] == 1

    for target in [
        "OSI",
        "ODCS",
        "SNOWFLAKE_SEMANTIC_VIEW",
        "DATABRICKS_METRIC_VIEW",
    ]:
        compiled = compile_context_product(product, version, target, tables, rejections)
        assert "negative_knowledge" not in compiled.content, target
