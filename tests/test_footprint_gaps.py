"""R11-FP05/FP17: every recorded footprint gap is counted, routed, and never disclosed past a gate.

Seeds one datasource with one of each gap the footprint work records -- a withheld definition, a
quarantined body, a routine waiting for the lineage agent, a routine with an UNPARSED statement,
an undecided edge, a source-change hold and an unprocessed change signal -- and a second datasource
the caller may not read. Pins the counts, the route each kind names, the queue age, and that the
denied datasource contributes nothing, not even to the totals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import aida.footprint_gaps as footprint_gaps_module
from aida.authorization_gate import AuthorizationDenied
from aida.change_signal_models import MetadataChangeSignal
from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.footprint_gaps import GAP_DEFINITIONS, footprint_gaps
from aida.models import AnalysisRun, DataQualityIncident
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    seed_table,
    task_agent_session,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _routine(
    org: Any, datasource: Any, schema: Any, name: str, **overrides: Any
) -> MetadataRoutine:
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": org.id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "signature": "()",
        "routine_type": "PROCEDURE",
        "body_sql_redacted": "BEGIN NULL; END;",
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "availability": "AVAILABLE",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    return MetadataRoutine(**values)


def _edge(org: Any, datasource: Any, routine: MetadataRoutine, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "organization_id": org.id,
        "datasource_id": datasource.id,
        "routine_id": routine.id,
        "statement_ordinal": 1,
        "source_table": "public.a",
        "source_column": "x",
        "target_table": "public.b",
        "target_column": "x",
        "transformation_type": "DIRECT",
        "confidence": "FULL",
        "dialect": "postgres",
        "sql_hash": "h",
        "review_status": "ACTIVE",
    }
    values.update(overrides)
    return DeepProcedureLineageEdge(**values)


async def test_each_gap_is_counted_routed_and_hidden_past_the_gate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, datasource, schema = await seed_estate(session)
    _, denied, denied_schema = await seed_estate(session, organization=org)
    view = await seed_table(
        session, org, datasource, schema, name="v_hidden", object_type="VIEW"
    )
    table = await seed_table(session, org, datasource, schema, name="orders")
    session.add(
        MetadataViewDefinition(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=view.id,
            definition_sql_redacted=None,
            availability="UNAVAILABLE",
            unavailable_reason="module is encrypted",
            fingerprint="fp",
        )
    )
    dynamic = _routine(org, datasource, schema, "r_dynamic")
    proposed = _routine(org, datasource, schema, "r_proposed")
    unresolved = _routine(org, datasource, schema, "r_unresolved")
    session.add_all(
        [
            _routine(org, datasource, schema, "r_quarantined", screening_status="QUARANTINED"),
            _routine(org, datasource, schema, "r_waiting"),
            _routine(org, datasource, schema, "r_package", routine_type="PACKAGE"),
            dynamic,
            proposed,
            unresolved,
            _routine(
                org,
                denied,
                denied_schema,
                "r_denied",
                availability="UNAVAILABLE",
                body_sql_redacted=None,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            # A gap nobody can act on: the callee is itself on the call path.
            _edge(
                org,
                datasource,
                dynamic,
                transformation_type="UNPARSED",
                unparsed_reason="NESTED_PROCEDURE_CALL: public.y (CYCLE)",
            ),
            # A gap a source administrator can close.
            _edge(
                org,
                datasource,
                unresolved,
                transformation_type="UNPARSED",
                unparsed_reason="NESTED_PROCEDURE_CALL: public.x (NOT_CAPTURED)",
            ),
            _edge(org, datasource, proposed, review_status="PROPOSED"),
            DataQualityIncident(
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=table.id,
                fingerprint=uuid4().hex,
                anomaly_type="SOURCE_CHANGE",
                severity="CRITICAL",
                status="OPEN",
                summary="held",
                first_observed_at=NOW,
                last_observed_at=NOW,
            ),
            MetadataChangeSignal(
                organization_id=org.id,
                datasource_id=datasource.id,
                subject_kind="TABLE",
                subject_id=table.id,
                signal_type="STRUCTURE_CHANGED",
                detected_at=NOW - timedelta(minutes=30),
            ),
        ]
    )
    await session.commit()

    real_gate = footprint_gaps_module.gate

    async def gate(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("datasource_id") == denied.id:
            raise AuthorizationDenied("DATASOURCE_NOT_GRANTED")
        return await real_gate(*args, **kwargs)

    monkeypatch.setattr(footprint_gaps_module, "gate", gate)

    result = await footprint_gaps(
        session,
        context=human(org, "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=org.id,
        now=NOW,
    )

    (listed,) = result.datasources
    assert listed.datasource_id == datasource.id
    by_kind = {gap.kind: gap for gap in listed.gaps}
    assert {kind: gap.count for kind, gap in by_kind.items()} == {
        "CODE_WITHHELD": 1,
        "CODE_QUARANTINED": 1,
        # r_waiting only: the quarantined body is not eligible, the package is not a callable
        # routine, and the other two already have edges.
        "LINEAGE_AWAITING_PARSE": 1,
        "LINEAGE_UNPARSED_STATEMENTS": 2,
        # Of those two, the one whose callee is simply not captured here.
        "LINEAGE_UNRESOLVED_CALLEE": 1,
        "LINEAGE_AWAITING_REVIEW": 1,
        "SOURCE_CHANGE_HOLDS": 1,
        "CHANGE_SIGNALS_PENDING": 1,
    }
    for gap in listed.gaps:
        assert (gap.resolution, gap.owner, gap.explanation) == GAP_DEFINITIONS[gap.kind]
    assert by_kind["CODE_WITHHELD"].resolution == "SOURCE_ACCESS"
    assert listed.oldest_pending_signal_minutes == 30
    # The denied datasource's withheld routine is in no count, total included.
    assert result.totals["CODE_WITHHELD"] == 1


async def test_what_the_scanning_login_may_not_see_is_a_gap_with_a_route(
    session: AsyncSession,
) -> None:
    """R11-FP02: objects outside the catalog entirely, counted from the last run's receipt."""
    org, datasource, _ = await seed_estate(session)

    def run(status: str, at: datetime, invisible: dict[str, int | None]) -> AnalysisRun:
        return AnalysisRun(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            mode="FULL",
            status=status,
            created_at=at,
            updated_at=at,
            discovery_receipt={
                "kinds": {
                    kind: {"discovered": 1, "excluded": 0, "invisible": hidden}
                    for kind, hidden in invisible.items()
                }
            },
        )

    session.add_all(
        [
            # An older run saw more; the count follows the last completed one, not the worst.
            run("COMPLETED", NOW - timedelta(days=2), {"TABLE": 900}),
            run("COMPLETED", NOW - timedelta(hours=1), {"TABLE": 412, "VIEW": 3}),
            # A run still going says nothing yet.
            run("RUNNING", NOW, {"TABLE": 1_000}),
        ]
    )
    await session.commit()

    result = await footprint_gaps(
        session,
        context=human(org, "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=org.id,
        now=NOW,
    )

    (listed,) = result.datasources
    (gap,) = [item for item in listed.gaps if item.kind == "SOURCE_OBJECTS_INVISIBLE"]
    assert gap.count == 415
    assert (gap.resolution, gap.owner) == ("SOURCE_ACCESS", "source administrator")


async def test_a_source_that_could_not_be_asked_reports_no_invisible_gap(
    session: AsyncSession,
) -> None:
    """`invisible: null` is "we could not ask", and must never be counted as none hidden."""
    org, datasource, _ = await seed_estate(session)
    session.add(
        AnalysisRun(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            mode="FULL",
            status="COMPLETED",
            created_at=NOW,
            updated_at=NOW,
            discovery_receipt={
                "kinds": {"TABLE": {"discovered": 4, "excluded": 0, "invisible": None}}
            },
        )
    )
    await session.commit()

    result = await footprint_gaps(
        session,
        context=human(org, "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=org.id,
        now=NOW,
    )

    (listed,) = result.datasources
    assert [gap for gap in listed.gaps if gap.kind == "SOURCE_OBJECTS_INVISIBLE"] == []
    assert "SOURCE_OBJECTS_INVISIBLE" not in result.totals
