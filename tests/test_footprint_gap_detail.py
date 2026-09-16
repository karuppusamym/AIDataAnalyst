"""R11-FP05: a gap's count expands into the objects behind it, and into nothing else.

The counts told a steward there were seven withheld definitions and left them to find which
seven. These tests drive `footprint_gap_objects` against the same estate shape the counts are
proven on, and pin what the drill-down may say:

* each kind lists the objects its own count came from, named as the catalog names them, with the
  stable code the record carries and no free text from the source;
* the gap whose objects Atlas never saw names none of them, and says why rather than returning an
  empty list that would read as "none";
* another source's objects are never in the answer, and an unknown kind is refused rather than
  answered with an empty list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import aida.footprint_gaps_api as footprint_gaps_api
from aida.authorization_gate import AuthorizationDenied
from aida.change_signal_models import MetadataChangeSignal
from aida.envelope_models import MetadataViewDefinition
from aida.footprint_gap_detail import (
    UNNAMEABLE,
    UnknownGapKind,
    footprint_gap_objects,
)
from aida.footprint_gaps_api import get_footprint_gap_objects
from aida.models import DataQualityIncident
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    seed_table,
    task_agent_session,
)
from tests.test_footprint_gaps import _edge, _routine

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


async def _estate(session: AsyncSession) -> Any:
    """One source with one of each gap, and a second source holding the same shapes."""
    org, datasource, schema = await seed_estate(session)
    _, elsewhere, elsewhere_schema = await seed_estate(session, organization=org)
    view = await seed_table(session, org, datasource, schema, name="v_hidden", object_type="VIEW")
    table = await seed_table(session, org, datasource, schema, name="orders")
    foreign_view = await seed_table(
        session, org, elsewhere, elsewhere_schema, name="v_other", object_type="VIEW"
    )
    session.add_all(
        [
            MetadataViewDefinition(
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=view.id,
                definition_sql_redacted=None,
                availability="UNAVAILABLE",
                unavailable_reason="module is encrypted",
                fingerprint="fp",
            ),
            MetadataViewDefinition(
                organization_id=org.id,
                datasource_id=elsewhere.id,
                table_id=foreign_view.id,
                definition_sql_redacted=None,
                availability="UNAVAILABLE",
                unavailable_reason="module is encrypted",
                fingerprint="fp",
            ),
        ]
    )
    unresolved = _routine(org, datasource, schema, "r_unresolved")
    held = _routine(org, datasource, schema, "r_quarantined", screening_status="QUARANTINED")
    session.add_all([unresolved, held])
    await session.flush()
    session.add_all(
        [
            _edge(
                org,
                datasource,
                unresolved,
                transformation_type="UNPARSED",
                unparsed_reason="NESTED_PROCEDURE_CALL: public.x (NOT_CAPTURED)",
            ),
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
    return org, datasource, elsewhere, view, table, unresolved, held


async def _objects(session: AsyncSession, org: Any, datasource: Any, kind: str) -> Any:
    return await footprint_gap_objects(
        session, organization_id=org.id, datasource_id=datasource.id, kind=kind
    )


async def test_withheld_code_names_the_objects_it_counted() -> None:
    async with task_agent_session() as session:
        org, datasource, elsewhere, view, _table, _unresolved, _held = await _estate(session)

        detail = await _objects(session, org, datasource, "CODE_WITHHELD")
        foreign = await _objects(session, org, elsewhere, "CODE_WITHHELD")

    (withheld,) = detail.objects
    assert (withheld.object_type, withheld.object_id) == ("VIEW", view.id)
    assert withheld.qualified_name.endswith(".v_hidden")
    assert detail.resolution == "SOURCE_ACCESS" and detail.owner == "source administrator"
    assert detail.truncated is False and detail.note is None
    # No per-object code: the kind already says the definition was withheld, and the reason the
    # source gave for withholding it is free text that belongs behind the gated code route.
    assert withheld.detail is None
    # The other source's withheld view belongs to the other source's answer, and only there.
    assert [item.object_id for item in foreign.objects] != [view.id]


async def test_a_quarantined_body_and_an_unread_call_carry_their_own_codes() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, _table, unresolved, held = await _estate(session)

        quarantined = await _objects(session, org, datasource, "CODE_QUARANTINED")
        callees = await _objects(session, org, datasource, "LINEAGE_UNRESOLVED_CALLEE")

    assert [item.object_id for item in quarantined.objects] == [held.id]
    (callee,) = callees.objects
    assert (callee.object_type, callee.object_id) == ("ROUTINE", unresolved.id)
    # The code inside the marker, not the statement fragment beside it.
    assert callee.detail == "NOT_CAPTURED"
    assert "public.x" not in callee.qualified_name


async def test_a_hold_and_a_pending_signal_name_the_table_they_are_about() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, table, _unresolved, _held = await _estate(session)

        holds = await _objects(session, org, datasource, "SOURCE_CHANGE_HOLDS")
        signals = await _objects(session, org, datasource, "CHANGE_SIGNALS_PENDING")

    assert [(item.object_type, item.object_id, item.detail) for item in holds.objects] == [
        ("TABLE", table.id, "CRITICAL")
    ]
    assert [(item.object_id, item.detail) for item in signals.objects] == [
        (table.id, "STRUCTURE_CHANGED")
    ]


async def test_the_objects_atlas_never_saw_are_named_by_nobody_and_said_so() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, _table, _unresolved, _held = await _estate(session)

        detail = await _objects(session, org, datasource, UNNAMEABLE)

    # The honest empty case: a count with no list, and a sentence saying why there is none.
    assert detail.objects == []
    assert detail.note is not None and "not in the catalog" in detail.note
    assert detail.resolution == "SOURCE_ACCESS"


async def test_a_kind_this_platform_does_not_record_is_refused() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, _table, _unresolved, _held = await _estate(session)

        with pytest.raises(UnknownGapKind):
            await _objects(session, org, datasource, "NOT_A_GAP")


# --- the route: who may ask, and what a refusal looks like ------------------------------------


async def _ask(
    session: AsyncSession, org: Any, datasource: Any, kind: str, **overrides: Any
) -> Any:
    return await get_footprint_gap_objects(
        datasource.id,
        kind,
        context=overrides.get(
            "context", human(org, "ops-1", frozenset({"Operations"}))
        ),
        session=session,
        settings=agent_settings(),
    )


async def test_the_route_answers_for_a_source_the_caller_may_read() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, view, _table, _unresolved, _held = await _estate(session)

        detail = await _ask(session, org, datasource, "CODE_WITHHELD")

    assert [item.object_id for item in detail.objects] == [view.id]


async def test_a_source_the_caller_may_not_read_answers_as_one_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, _table, _unresolved, _held = await _estate(session)

        async def denied(*_args: Any, **_kwargs: Any) -> Any:
            raise AuthorizationDenied("DATASOURCE_NOT_GRANTED")

        monkeypatch.setattr(footprint_gaps_api, "gate", denied)

        with pytest.raises(HTTPException) as refusal:
            await _ask(session, org, datasource, "CODE_WITHHELD")

    # The same answer as a source that does not exist: a refusal that said "denied" would tell
    # an unauthorized caller the source is there, which is what the summary route also avoids.
    assert refusal.value.status_code == 404
    assert refusal.value.detail == "datasource not found"


async def test_a_kind_nobody_records_is_not_answered_with_an_empty_list() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, _table, _unresolved, _held = await _estate(session)

        with pytest.raises(HTTPException) as refusal:
            await _ask(session, org, datasource, "MADE_UP_KIND")

    assert refusal.value.status_code == 404


async def test_another_organizations_source_is_refused_before_it_is_read() -> None:
    async with task_agent_session() as session:
        org, datasource, _elsewhere, _view, _table, _unresolved, _held = await _estate(session)
        stranger = human(
            SimpleNamespace(id=uuid4()), "ops-2", frozenset({"Operations"})
        )

        with pytest.raises(HTTPException) as refusal:
            await _ask(session, org, datasource, "CODE_WITHHELD", context=stranger)

    assert refusal.value.status_code == 403
