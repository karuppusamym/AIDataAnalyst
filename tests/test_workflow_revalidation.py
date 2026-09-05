"""Regression coverage for the export/authoring/plan review findings."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aida.asset_description_api import DescriptionDraftEdit, edit_asset_description_draft
from aida.db import Base
from aida.models import (
    AssetDescriptionDraft,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    MetadataEnrichmentProposal,
    MetadataTable,
    QueryExecution,
    ToolExecution,
)
from aida.newly_created_table_drafter import (
    enqueue_description_draft_for_table,
    enqueue_semantics_for_source,
)
from aida.query_gateway import GatewayResult, QueryExecutionGateway
from aida.security import SecurityContext
from aida.tool_plan_runtime import resolve_plan_tools
from aida.tool_plans import PlanBudget, PlanStep, StepResult, ToolPlan, execute_plan, validate_plan
from aida.tool_plans_api import ToolPlanCreate, create_tool_plan, execute_tool_plan
from aida.workflows.activities import persist_discovery_snapshot
from tests.test_auto_enqueue_on_ingest import _catalog, _seed_datasource


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()



def context(org):
    return SecurityContext(
        principal_id="author",
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"PlatformAdmin"}),
    )


async def test_unwired_executor_fails_instead_of_claiming_completion():
    plan = ToolPlan(uuid4(), "Unwired", [PlanStep(1, str(uuid4()), "1", {})], PlanBudget())
    result = await execute_plan(plan, uuid4(), None, "author")
    assert result.status == "FAILED"
    assert "unavailable" in result.step_results[0].error_message


async def test_timeout_stops_execution_and_skips_dependent_steps():
    async def slow(step, _context):
        await asyncio.sleep(1)
        return StepResult(step.sequence, "COMPLETED")

    plan = ToolPlan(
        uuid4(),
        "Bounded",
        [
            PlanStep(1, "a", "1", {}, timeout_seconds=0.01),
            PlanStep(2, "b", "1", {}, dependencies=[1]),
        ],
        PlanBudget(),
    )
    result = await execute_plan(plan, uuid4(), None, "author", slow)
    assert result.status == "FAILED"
    assert [s.status for s in result.step_results] == ["FAILED", "SKIPPED"]


def test_duplicate_sequences_are_rejected_before_topological_sort():
    plan = ToolPlan(
        uuid4(), "Duplicate", [PlanStep(1, "a", "1", {}), PlanStep(1, "b", "1", {})], PlanBudget()
    )
    assert not validate_plan(plan).valid


async def test_nonexistent_published_tool_cannot_validate(session):
    datasource, _ = await _seed_datasource(session)
    plan = ToolPlan(uuid4(), "Invalid", [PlanStep(1, str(uuid4()), "999", {})], PlanBudget())
    validation, versions = await resolve_plan_tools(
        session, plan, context(datasource.organization_id)
    )
    assert not validation.valid and not versions


async def test_completed_scan_creates_reviewable_semantics_once(session):
    datasource, analysis = await _seed_datasource(session)
    await persist_discovery_snapshot(
        session,
        analysis,
        datasource,
        _catalog(["accounts"]),
        deprecate_missing=False,
        connector_capabilities={},
    )
    analysis.status = "COMPLETED"
    await session.flush()
    await enqueue_semantics_for_source(session, datasource.id)
    await enqueue_semantics_for_source(session, datasource.id)
    proposals = list(await session.scalars(select(MetadataEnrichmentProposal)))
    assert len(proposals) == 1
    review = await session.get(GovernanceReview, proposals[0].governance_review_id)
    assert review.status == "PENDING"


async def test_description_edit_preserves_evidence_and_rejects_stale_edits(session):
    datasource, analysis = await _seed_datasource(session)
    await persist_discovery_snapshot(
        session,
        analysis,
        datasource,
        _catalog(["accounts"]),
        deprecate_missing=False,
        connector_capabilities={},
    )
    table = await session.scalar(select(MetadataTable))
    draft = await enqueue_description_draft_for_table(
        session, organization_id=datasource.organization_id, table=table
    )
    original = draft.drafted_text
    edited = await edit_asset_description_draft(
        draft.id,
        DescriptionDraftEdit(
            drafted_text="Reviewed account records with one row per account.",
            expected_text=original,
        ),
        context(datasource.organization_id),
        session,
    )
    assert edited.status == "DRAFT"
    assert edited.evidence["edited_by"] == "author"
    with pytest.raises(HTTPException) as stale:
        await edit_asset_description_draft(
            draft.id,
            DescriptionDraftEdit(
                drafted_text="A stale editor must not overwrite the new text.",
                expected_text=original,
            ),
            context(datasource.organization_id),
            session,
        )
    assert stale.value.status_code == 409
    stored = await session.get(AssetDescriptionDraft, draft.id)
    assert stored.drafted_text == edited.drafted_text


async def test_plan_reaches_real_governed_tool_gateway_and_records_receipt(session, monkeypatch):
    import aida.tool_plans_api as plans_api
    from aida.config import Settings

    datasource, analysis = await _seed_datasource(session)
    await persist_discovery_snapshot(
        session,
        analysis,
        datasource,
        _catalog(["accounts"]),
        deprecate_missing=False,
        connector_capabilities={},
    )
    tool = GovernedTool(
        id=uuid4(),
        organization_id=datasource.organization_id,
        project_id=datasource.project_id,
        slug="accounts",
    )
    version = GovernedToolVersion(
        id=uuid4(),
        organization_id=datasource.organization_id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Accounts",
        description="Account records",
        datasource_id=datasource.id,
        sql_template="SELECT id FROM public.accounts",
        referenced_tables=["public.accounts"],
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint="workflow-test",
        created_by="maker",
    )
    session.add_all([tool, version])
    await session.commit()
    calls = []

    async def gateway_execute(_self, db, **kwargs):
        calls.append(kwargs)
        execution = QueryExecution(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            principal_id="author",
            status="COMPLETED",
            dialect="postgres",
            sql_hash="test",
            normalized_sql=kwargs["sql"],
            referenced_tables=["public.accounts"],
            referenced_columns=["id"],
            column_lineage=[],
            row_count=0,
            elapsed_ms=1,
        )
        db.add(execution)
        await db.flush()
        return GatewayResult(execution, (), ())

    monkeypatch.setattr(QueryExecutionGateway, "execute", gateway_execute)
    # Independent sessions share the same SQLite database, just as runtime sessions do.
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(
        plans_api, "session_factory", async_sessionmaker(session.bind, expire_on_commit=False)
    )
    settings = Settings(edition="ENTERPRISE")
    created = await create_tool_plan(
        ToolPlanCreate(
            name="Actual execution",
            steps=[
                {
                    "sequence": 1,
                    "tool_id": str(tool.id),
                    "tool_version": "1",
                    "parameters": {},
                }
            ],
        ),
        context(datasource.organization_id),
        session,
        settings,
    )
    result = await execute_tool_plan(
        created.id, context(datasource.organization_id), session, settings
    )
    assert result.status == "COMPLETED" and len(calls) == 1
    assert " ".join(calls[0]["sql"].split()) == "SELECT id FROM public.accounts"
    assert await session.scalar(select(ToolExecution.id)) is not None
