"""R11-FP16: a governed tool generated from a routine stands only while that routine does.

* The procedure-tool route binds a draft to its routine and to the fingerprint of the definition
  its SQL was copied from.
* A draft generated before its routine changed is refused at approval, however late it is
  approved. A draft regenerated from the current definition is approved.
* A published version whose routine was redefined, retired or removed is blocked at execution,
  before any SQL is rendered, and the audit record says why. A routine restored to exactly the
  bound definition stands again.
* A version recorded before the binding existed falls back to change signals detected after it
  was generated -- never after it was approved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.change_signal_models import MetadataChangeSignal
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataRoutine
from aida.models import AuditEvent, GovernanceReview, GovernedToolVersion, ToolExecution
from aida.procedure_tool_api import ProcedureToolBlueprintRequest, create_procedure_tool_blueprint
from aida.query_gateway import QueryExecutionGateway
from aida.schemas import GovernedToolVersionRead, ToolExecutionRequest
from aida.semantic_api import _decide_governed_tool_version
from aida.tool_api import execute_tool
from aida.tool_source_binding import (
    REASON_CHANGED_SINCE_GENERATION,
    REASON_DEFINITION_CHANGED,
    REASON_SOURCE_RETIRED,
    SOURCE_CHANGED_MESSAGE,
    SOURCE_DEFINITION_MOVED,
    fetch_source_binding_holds,
    source_binding_drift,
)
from tests.support.doubles import security_context
from tests.test_procedure_tool_blueprint import _Scenario as ProcedureScenario
from tests.test_quality_runtime_coupling import _fake_execute
from tests.test_quality_runtime_coupling import _Scenario as CouplingScenario

_REPORT_BODY = (
    "CREATE PROCEDURE dbo.usp_report AS BEGIN "
    "SELECT c.customer_id, SUM(o.amount) AS total_amount "
    "FROM public.orders o JOIN public.customers c ON c.customer_id = o.customer_id "
    "GROUP BY c.customer_id; END"
)
DEFINITION_A = "a" * 64
DEFINITION_B = "b" * 64


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _generate(
    db: AsyncSession, scenario: ProcedureScenario, routine: MetadataRoutine
) -> GovernedToolVersionRead:
    return await create_procedure_tool_blueprint(
        scenario.project.id,
        ProcedureToolBlueprintRequest(
            slug="customer_order_totals",
            name="Customer order totals",
            description="Read surface from a proven read-only procedure.",
            datasource_id=scenario.datasource.id,
            routine_id=routine.id,
            allowed_roles=["Analyst"],
        ),
        context=scenario.maker(),
        session=db,
        settings=Settings(),
    )


async def _approve(
    db: AsyncSession, scenario: ProcedureScenario, version_id: object
) -> GovernedToolVersion:
    review = GovernanceReview(
        organization_id=scenario.organization.id,
        object_type="GOVERNED_TOOL_VERSION",
        object_id=str(version_id),
        requested_action="PUBLISH",
        requested_by="tool-maker",
    )
    db.add(review)
    await db.flush()
    await _decide_governed_tool_version(
        db,
        review,
        decision="APPROVE",
        reason=None,
        context=security_context(
            organization_id=scenario.organization.id, roles=frozenset({"Reviewer"})
        ),
        now=datetime.now(UTC),
    )
    version = await db.get(GovernedToolVersion, version_id)
    assert version is not None
    return version


def _catalog_routine(scenario: CouplingScenario) -> MetadataRoutine:
    return MetadataRoutine(
        id=uuid4(),
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        schema_id=scenario.table.schema_id,
        name="usp_report",
        routine_type="PROCEDURE",
        body_sql_redacted="SELECT 1",
        body_fingerprint=DEFINITION_A,
        redaction_status="PARSED",
        screening_status="CLEAN",
        availability="AVAILABLE",
        status="ACTIVE",
        fingerprint="fp-routine",
    )


async def test_a_procedure_tool_draft_is_bound_to_its_routine_definition(
    db: AsyncSession,
) -> None:
    scenario = await ProcedureScenario(db).build()
    routine = scenario.routine(body=_REPORT_BODY)
    routine.body_fingerprint = DEFINITION_A
    db.add(routine)
    await db.flush()

    created = await _generate(db, scenario, routine)

    version = await db.get(GovernedToolVersion, created.id)
    assert version is not None
    assert (created.source_routine_id, version.source_definition_fingerprint) == (
        routine.id,
        DEFINITION_A,
    )


async def test_a_draft_generated_before_its_routine_changed_is_refused_at_approval(
    db: AsyncSession,
) -> None:
    scenario = await ProcedureScenario(db).build()
    routine = scenario.routine(body=_REPORT_BODY)
    routine.body_fingerprint = DEFINITION_A
    db.add(routine)
    await db.flush()
    stale = await _generate(db, scenario, routine)
    # The source redefines the routine after the draft was generated, before anyone approves it.
    routine.body_fingerprint = DEFINITION_B
    await db.flush()

    with pytest.raises(HTTPException) as refused:
        await _approve(db, scenario, stale.id)

    detail = refused.value.detail
    assert refused.value.status_code == 409
    assert (detail["code"], detail["reason"]) == (
        SOURCE_DEFINITION_MOVED,
        REASON_DEFINITION_CHANGED,
    )
    assert str(detail) == SOURCE_CHANGED_MESSAGE
    draft = await db.get(GovernedToolVersion, stale.id)
    assert draft is not None and (draft.status, draft.approved_at) == ("DRAFT", None)

    regenerated = await _generate(db, scenario, routine)
    published = await _approve(db, scenario, regenerated.id)
    assert (published.status, published.source_definition_fingerprint) == (
        "PUBLISHED",
        DEFINITION_B,
    )


async def test_a_published_version_stands_only_while_its_routine_matches_the_binding(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    scenario = await CouplingScenario(db).build()
    routine = _catalog_routine(scenario)
    db.add(routine)
    version = await scenario.tool_version()
    version.source_routine_id = routine.id
    version.source_definition_fingerprint = DEFINITION_A
    version.approved_at = datetime.now(UTC)
    await db.flush()

    async def run() -> None:
        await execute_tool(
            version.id,
            ToolExecutionRequest(parameters={}),
            context=scenario.analyst(),
            session=db,
            settings=Settings(),
        )

    await run()
    routine.body_fingerprint = DEFINITION_B
    await db.flush()
    with pytest.raises(HTTPException) as redefined:
        await run()
    assert redefined.value.status_code == 409
    assert SOURCE_CHANGED_MESSAGE in redefined.value.detail

    routine.body_fingerprint = DEFINITION_A
    await db.flush()
    await run()

    routine.status = "DEPRECATED"
    await db.flush()
    assert await source_binding_drift(db, version) == REASON_SOURCE_RETIRED
    with pytest.raises(HTTPException):
        await run()

    assert len((await db.scalars(select(ToolExecution))).all()) == 2
    denied = (await db.scalars(select(AuditEvent).where(AuditEvent.outcome == "DENIED"))).all()
    assert len(denied) == 2
    assert all(event.details["source_definition_changed"] is True for event in denied)


async def test_a_version_without_a_bound_definition_is_held_by_changes_after_generation(
    db: AsyncSession,
) -> None:
    scenario = await CouplingScenario(db).build()
    routine = _catalog_routine(scenario)
    db.add(routine)
    version = await scenario.tool_version()
    version.source_routine_id = routine.id
    generated_at = version.created_at
    # Approved after the change: approval time must not hide it.
    version.approved_at = generated_at + timedelta(days=2)
    await db.flush()
    assert await source_binding_drift(db, version) is None

    db.add(
        MetadataChangeSignal(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            subject_kind="ROUTINE",
            subject_id=routine.id,
            signal_type="DEFINITION_CHANGED",
            change_class="STRUCTURAL",
            detected_at=generated_at + timedelta(days=1),
        )
    )
    await db.flush()

    assert await source_binding_drift(db, version) == REASON_CHANGED_SINCE_GENERATION
    assets, holds = await fetch_source_binding_holds(db, version)
    assert assets == [str(routine.id)]
    assert [(hold.severity, hold.status) for hold in holds] == [("CRITICAL", "OPEN")]


async def test_a_tool_not_extracted_from_a_routine_has_no_routine_dependency(
    db: AsyncSession,
) -> None:
    scenario = await CouplingScenario(db).build()
    version = await scenario.tool_version()

    assert await fetch_source_binding_holds(db, version) == ([], [])
