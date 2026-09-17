"""R11-FP03: the definition-history read, and the things it must never serve.

`metadata_routine_definition_version` has appended a row per first capture and
per moved definition since 2026-09-15, and until now nothing read it. A history
that is written and never readable is the same as no history, so this file pins
the read -- and, more importantly, pins the four properties that make it safe to
have at all:

(a) **the body is never served.** Not for a reviewer, not for a PlatformAdmin,
    not in a field that "only" holds a digest. `test_no_response_carries_body
    _text` walks the whole serialised payload of every state and asserts the
    stored text appears in none of it, because a leak here is a leak of the
    largest indirect-injection surface in the estate;

(b) **a withheld version keeps its row**, with a marker and a reason code.
    Dropping it would let a reader infer something about the data from a fact
    about their own entitlement, which is the rule `ProfilePanel` and
    `context_product_coverage` already follow;

(c) **the diff is the answer.** "This procedure started writing a second table
    on 12 September" is the question the surface exists for, and it is
    answerable without quoting a line of SQL;

(d) **the gate is real.** Not "a gate exists in the module": a caller the
    authorization gate refuses gets a 403 from this handler, and another
    organization's routine is not readable at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import aida.definition_history_api as definition_history_api
from aida.column_description_model import WITHHELD
from aida.definition_history_api import (
    FOOTPRINT_BASIS_REPARSED,
    FOOTPRINT_COMPUTED,
    FOOTPRINT_COMPUTED_NO_BASELINE,
    FOOTPRINT_NOT_COMPUTED,
    FOOTPRINT_UNAVAILABLE,
    FOOTPRINT_UNCHANGED_LITERALS_ONLY,
    FOOTPRINT_WITHHELD,
    WITHHELD_BODY_NOT_STORED,
    WITHHELD_BODY_QUARANTINED,
    WITHHELD_BODY_UNAVAILABLE,
    derive_footprint,
    get_routine_definition_history,
)
from aida.envelope_models import MetadataRoutine, MetadataRoutineDefinitionVersion
from aida.models import DataSource, MetadataSchema, Organization
from aida.procedure_lineage import UnparsedReason
from aida.schemas import RoutineDefinitionHistoryRead
from aida.security_types import SecurityContext
from tests.support.task_agents import agent_settings, seed_estate, task_agent_session

_SETTINGS = agent_settings()
#: The posture that turns an unresolved workspace into a denial. One setting, no
#: code change -- `authorization_gate.DENY_UNRESOLVED` -- which is what makes a
#: 403 here evidence that this handler reaches the gate rather than decorates it.
_DENYING_SETTINGS = agent_settings(unresolved_workspace_posture="DENY")
_STEWARD = "steward@example.com"

#: One read, one write.
BODY_V1 = """CREATE PROCEDURE settle() LANGUAGE SQL AS $$
INSERT INTO public.ledger (id, amt) SELECT id, amt FROM public.postings WHERE amt > 0;
$$"""
#: The same, plus a second write target. This pair is the whole point of the
#: surface: the difference between them is reportable, and neither body is.
BODY_V2 = """CREATE PROCEDURE settle() LANGUAGE SQL AS $$
INSERT INTO public.ledger (id, amt) SELECT id, amt FROM public.postings WHERE amt > 0;
INSERT INTO public.audit (id) SELECT id FROM public.postings;
$$"""
#: A body with a statement the parser cannot read, so `parse_completed` is false
#: and a reason code is reported -- the prefix only.
BODY_DYNAMIC = """CREATE PROCEDURE settle() LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('INSERT INTO %I SELECT * FROM public.postings', tbl);
  INSERT INTO public.ledger SELECT id FROM public.postings;
END
$$"""

_CAPTURED_AT = datetime(2026, 9, 12, 9, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _context(organization_id: UUID | None, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id=_STEWARD,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles or ("DataSteward",)),
    )


async def _seed_routine(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str = "settle",
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        routine_type="PROCEDURE",
        signature="()",
        body_sql_redacted=BODY_V1,
        body_fingerprint="bf",
        availability="AVAILABLE",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return routine


async def _capture(
    session: AsyncSession,
    routine: MetadataRoutine,
    *,
    version_number: int,
    body: str | None,
    change_class: str | None,
    availability: str = "AVAILABLE",
    redaction_status: str = "LEXICAL",
    screening_status: str = "CLEAN",
    truncated: bool = False,
    unavailable_reason: str | None = None,
    analysis_run_id: UUID | None = None,
    days: int = 0,
) -> MetadataRoutineDefinitionVersion:
    """One appended definition version, written the way ingestion writes one."""
    version = MetadataRoutineDefinitionVersion(
        id=uuid4(),
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        version_number=version_number,
        body_sql_redacted=body,
        body_fingerprint=None if body is None else f"raw-{version_number}",
        availability=availability,
        unavailable_reason=unavailable_reason,
        truncated=truncated,
        redaction_status=redaction_status,
        screening_status=screening_status,
        change_class=change_class,
        analysis_run_id=analysis_run_id,
        captured_at=_CAPTURED_AT + timedelta(days=days),
    )
    session.add(version)
    await session.flush()
    return version


async def _read(
    session: AsyncSession,
    routine: MetadataRoutine,
    *,
    context: SecurityContext | None = None,
    settings: Any = _SETTINGS,
    limit: int = 20,
    offset: int = 0,
) -> RoutineDefinitionHistoryRead:
    return await get_routine_definition_history(
        routine.id,
        limit=limit,
        offset=offset,
        context=context or _context(routine.organization_id),
        session=session,
        settings=settings,
    )


async def _two_version_history(
    session: AsyncSession,
) -> tuple[Organization, DataSource, MetadataSchema, MetadataRoutine]:
    """The headline case: one capture, then a structural change adding a write."""
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None, days=-2)
    await _capture(session, routine, version_number=2, body=BODY_V2, change_class="STRUCTURAL")
    return org, datasource, schema, routine


# ---------------------------------------------------------------------------
# (a) the body is never served
# ---------------------------------------------------------------------------


async def test_no_response_carries_body_text(session: AsyncSession) -> None:
    """The load-bearing assertion of the whole feature.

    Every state is represented on this one routine -- a readable capture, a
    literal-only repeat, a quarantined version, a version the source withheld
    and one whose text was never stored -- and the serialised response is
    searched for the stored text itself and for a distinctive identifier out of
    it. A future field that "helpfully" echoed a snippet fails here, which is
    the only reason this is asserted over the payload rather than field by
    field.
    """
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None)
    await _capture(session, routine, version_number=2, body=BODY_V1, change_class="LITERAL_ONLY")
    await _capture(
        session,
        routine,
        version_number=3,
        body=BODY_V2,
        change_class="STRUCTURAL",
        screening_status="QUARANTINED",
    )
    await _capture(
        session,
        routine,
        version_number=4,
        body=None,
        change_class="STRUCTURAL",
        availability="UNAVAILABLE",
        unavailable_reason="the login may not read this body",
    )
    await _capture(
        session,
        routine,
        version_number=5,
        body=BODY_V2,
        change_class="STRUCTURAL",
        redaction_status="UNPARSED",
    )

    history = await _read(session, routine)
    payload = json.dumps(history.model_dump(mode="json"))

    assert BODY_V1 not in payload
    assert BODY_V2 not in payload
    # Not just the whole body: no fragment of the program either.
    assert "INSERT INTO" not in payload
    assert "SELECT" not in payload
    assert all(version.body_released is False for version in history.versions)
    # And the digests that stand in for it are of the *stored* text, so the raw
    # fingerprint -- which is a digest of literal-bearing source text -- is not
    # in the payload under any name.
    assert "raw-1" not in payload


async def test_every_read_role_gets_the_same_bodyless_answer(session: AsyncSession) -> None:
    """A PlatformAdmin is not a way round the rule. The gate decides whether the
    *digest* may be reported; no role makes the text releasable here."""
    _org, _ds, _schema, routine = await _two_version_history(session)
    for role in ("PlatformAdmin", "Auditor", "Viewer", "DataSteward"):
        history = await _read(session, routine, context=_context(routine.organization_id, role))
        payload = json.dumps(history.model_dump(mode="json"))
        assert BODY_V2 not in payload, role
        assert [version.body_released for version in history.versions] == [False, False], role


# ---------------------------------------------------------------------------
# (b) a withheld version keeps its row
# ---------------------------------------------------------------------------


async def test_a_quarantined_version_keeps_its_row_with_a_marker_and_reason(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None)
    await _capture(
        session,
        routine,
        version_number=2,
        body=BODY_V2,
        change_class="STRUCTURAL",
        screening_status="QUARANTINED",
    )

    history = await _read(session, routine)
    assert [version.version_number for version in history.versions] == [2, 1]
    quarantined = history.versions[0]
    assert quarantined.withheld_marker == WITHHELD
    assert quarantined.withheld_reason_code == WITHHELD_BODY_QUARANTINED
    assert quarantined.footprint_state == FOOTPRINT_WITHHELD
    assert quarantined.parse_completed is None
    assert quarantined.writes_table_names == []
    # The digest is still reported, and this is the deliberate asymmetry: R11-FP12's
    # coverage already publishes a digest beside a false `definition_available`,
    # because screening keeps prompt-risky *text* out of a model context and a
    # digest is not text. Nulling it would withhold the one fact a steward can
    # act on -- that the definition moved -- as well as the body.
    assert quarantined.definition_digest is not None
    assert quarantined.definition_digest != history.versions[1].definition_digest


async def test_an_unavailable_version_says_so_rather_than_reading_as_empty(
    session: AsyncSession,
) -> None:
    """`UNAVAILABLE` is the source refusing the body; `WITHHELD` is Atlas
    refusing to release it. Different things to do next, so different codes."""
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None)
    await _capture(
        session,
        routine,
        version_number=2,
        body=None,
        change_class="STRUCTURAL",
        availability="UNAVAILABLE",
        unavailable_reason="the login may not read this body",
    )

    history = await _read(session, routine)
    refused = history.versions[0]
    assert refused.availability == "UNAVAILABLE"
    assert refused.unavailable_reason == "the login may not read this body"
    assert refused.withheld_reason_code == WITHHELD_BODY_UNAVAILABLE
    assert refused.footprint_state == FOOTPRINT_UNAVAILABLE


async def test_an_unparsed_redaction_status_is_its_own_reason(session: AsyncSession) -> None:
    """`UNPARSED` stores no text at all (`sql_redaction.RedactedSql`), so the
    refusal is "nothing was stored", not "screening said no"."""
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(
        session,
        routine,
        version_number=1,
        body=BODY_V1,
        change_class=None,
        redaction_status="UNPARSED",
    )

    history = await _read(session, routine)
    assert history.versions[0].withheld_reason_code == WITHHELD_BODY_NOT_STORED
    assert history.versions[0].footprint_state == FOOTPRINT_WITHHELD
    assert history.versions[0].definition_digest is None


# ---------------------------------------------------------------------------
# (c) the diff is the answer
# ---------------------------------------------------------------------------


async def test_a_structural_change_names_the_table_the_procedure_started_writing(
    session: AsyncSession,
) -> None:
    """The sentence this feature exists to produce, with no SQL in sight."""
    _org, _ds, _schema, routine = await _two_version_history(session)

    history = await _read(session, routine)
    assert history.footprint_basis == FOOTPRINT_BASIS_REPARSED
    latest, first = history.versions
    assert latest.version_number == 2
    assert latest.change_class == "STRUCTURAL"
    assert latest.footprint_state == FOOTPRINT_COMPUTED
    # SQLite hands timestamps back naive; the date is the load-bearing part.
    assert latest.captured_at.replace(tzinfo=UTC) == _CAPTURED_AT
    assert latest.writes_added == ["public.audit"]
    assert latest.writes_removed == []
    assert latest.reads_added == []
    assert latest.reads_removed == []
    assert latest.writes_table_names == ["public.audit", "public.ledger"]
    assert latest.reads_table_names == ["public.postings"]
    # And the digest pair says the same thing independently of the class.
    assert latest.previous_definition_digest == first.definition_digest
    assert latest.definition_digest != first.definition_digest


async def test_the_first_capture_reads_as_everything_new(session: AsyncSession) -> None:
    """Version 1 changed nothing, because there was nothing to change. What it
    *touches* is still new to this history, and is reported as added; what it
    must not have is a previous digest it could be compared against."""
    _org, _ds, _schema, routine = await _two_version_history(session)

    first = (await _read(session, routine)).versions[1]
    assert first.version_number == 1
    assert first.change_class is None
    assert first.previous_definition_digest is None
    assert first.reads_added == ["public.postings"]
    assert first.writes_added == ["public.ledger"]
    assert first.reads_removed == []
    assert first.writes_removed == []


async def test_a_literal_only_change_claims_no_footprint_change(
    session: AsyncSession,
) -> None:
    """`LITERAL_ONLY` means the stored, value-free text did not move
    (`change_signals.code_change_signal`), so the footprint cannot have moved
    either -- and the two digests prove it without anyone taking the class on
    trust."""
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None)
    await _capture(session, routine, version_number=2, body=BODY_V1, change_class="LITERAL_ONLY")

    latest, first = (await _read(session, routine)).versions
    assert latest.footprint_state == FOOTPRINT_UNCHANGED_LITERALS_ONLY
    assert latest.definition_digest == first.definition_digest
    assert latest.previous_definition_digest == first.definition_digest
    assert (latest.reads_added, latest.reads_removed) == ([], [])
    assert (latest.writes_added, latest.writes_removed) == ([], [])
    # Still reported, not blanked: the reader wants to know what it touches now.
    assert latest.writes_table_names == ["public.ledger"]


async def test_a_retired_write_is_reported_as_removed(session: AsyncSession) -> None:
    """The other direction, which a reader acts on differently: this procedure
    stopped writing a table something downstream may still expect it to."""
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V2, change_class=None)
    await _capture(session, routine, version_number=2, body=BODY_V1, change_class="STRUCTURAL")

    latest = (await _read(session, routine)).versions[0]
    assert latest.writes_removed == ["public.audit"]
    assert latest.writes_added == []


async def test_a_statement_the_parser_cannot_read_is_said_with_codes_only(
    session: AsyncSession,
) -> None:
    """A reason code, never a reason *string*: the per-statement suffix can carry
    a callee name or a parse-error message quoting a value (INV-6), which is why
    `routine_lineage_edges.unparsed_reason_codes` exists and is what is served.
    """
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_DYNAMIC, change_class=None)

    version = (await _read(session, routine)).versions[0]
    assert version.parse_completed is False
    assert version.unparsed_reason_codes == [UnparsedReason.DYNAMIC_SQL.value]
    known = {reason.value for reason in UnparsedReason}
    for code in version.unparsed_reason_codes:
        assert code in known
        assert ":" not in code and " " not in code
    # The footprint it *could* read is still reported, qualified by the flag
    # above rather than withheld: a partial answer with its limitation stated is
    # the `ProfilePanel` rule, and a blank would read as "touches nothing".
    assert version.writes_table_names == ["public.ledger"]


async def test_the_derived_footprint_names_nothing_routine_local() -> None:
    """A temp table is not a table, and a steward handed one goes looking for it.

    `#staging` is written and then read back, so the read-back edge is a
    perfectly ordinary non-intermediate edge whose source is routine-local --
    which is how a name-based footprint acquires a table that does not exist.
    Both endpoints of the real dependency still appear, because the parser
    synthesises the transitive edge across the hop.
    """
    body = """CREATE PROCEDURE dbo.probe AS
BEGIN
  SELECT id INTO #staging FROM dbo.postings;
  INSERT INTO dbo.ledger (id) SELECT id FROM #staging;
END"""
    footprint = derive_footprint(body, dialect="tsql")
    assert footprint.reads == frozenset({"dbo.postings"})
    assert footprint.writes == frozenset({"dbo.ledger"})
    named = footprint.reads | footprint.writes
    # `<RESULT>` and `<LOCAL>`, the parser's own markers, are filtered by the
    # same `persistable_table` the edge writer uses.
    assert not any(name.startswith("<") for name in named)


# ---------------------------------------------------------------------------
# paging, and the baseline a page needs
# ---------------------------------------------------------------------------


async def test_a_page_diffs_its_oldest_row_against_the_row_below_it(
    session: AsyncSession,
) -> None:
    """A page boundary must not turn into a false "nothing changed".

    With one row per page, version 2's diff still has to be against version 1 --
    which is not on the page. The read fetches one row below the window for
    exactly this, so the answer does not depend on where the page happens to
    break.
    """
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None, days=-2)
    await _capture(session, routine, version_number=2, body=BODY_V2, change_class="STRUCTURAL")
    await _capture(session, routine, version_number=3, body=BODY_V1, change_class="STRUCTURAL")

    whole = await _read(session, routine)
    assert whole.total == 3
    assert [version.version_number for version in whole.versions] == [3, 2, 1]

    paged = await _read(session, routine, limit=1, offset=1)
    assert paged.total == 3
    assert [version.version_number for version in paged.versions] == [2]
    assert paged.versions[0].writes_added == ["public.audit"]
    assert paged.versions[0].previous_definition_digest == whole.versions[2].definition_digest


async def test_a_routine_with_no_captured_definition_answers_an_empty_history(
    session: AsyncSession,
) -> None:
    """An Oracle package member's body belongs to its package, so it has no
    version of its own. That is a real answer -- and not a 404, which would mean
    no such routine, nor an invented version 1."""
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema, name="member")

    history = await _read(session, routine)
    assert history.total == 0
    assert history.versions == []
    assert history.routine_qualified_name == "bank.public.member"


async def test_a_version_past_the_parse_budget_says_so(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over budget is not "unchanged". A blank there would be read as "this
    capture touched the same tables", which is a claim nobody made -- so it is
    `NOT_COMPUTED`, and the answer to it is a narrower window.

    The budget is spent from the newest end, because that is the end a history
    is read from: if it has to bite, it must bite on the oldest rows rather than
    on the change somebody is looking at today. The newest row is then
    `COMPUTED_NO_BASELINE` rather than `COMPUTED`, because its predecessor was
    not derived and so nothing may be claimed about what it *changed*.
    """
    org, datasource, schema = await seed_estate(session)
    routine = await _seed_routine(session, org, datasource, schema)
    await _capture(session, routine, version_number=1, body=BODY_V1, change_class=None)
    await _capture(session, routine, version_number=2, body=BODY_V2, change_class="STRUCTURAL")
    await _capture(session, routine, version_number=3, body=BODY_DYNAMIC, change_class="STRUCTURAL")
    monkeypatch.setattr(definition_history_api, "_FOOTPRINT_PARSE_BUDGET", 1)

    history = await _read(session, routine)
    states = {version.version_number: version.footprint_state for version in history.versions}
    assert states[3] == FOOTPRINT_COMPUTED_NO_BASELINE
    assert states[2] == FOOTPRINT_NOT_COMPUTED
    assert states[1] == FOOTPRINT_NOT_COMPUTED
    newest = history.versions[0]
    assert (newest.writes_added, newest.writes_removed) == ([], [])
    # The digests are unaffected: they cost no parse, so a narrowed page is not
    # a narrowed history.
    assert all(version.definition_digest is not None for version in history.versions)


# ---------------------------------------------------------------------------
# (d) the gate is real
# ---------------------------------------------------------------------------


async def test_the_authorization_gate_refuses_this_handler(session: AsyncSession) -> None:
    """Not "the module contains a gate call": the handler's own answer changes.

    Under `unresolved_workspace_posture="DENY"` the gate refuses an access it
    cannot resolve a workspace for, and this read returns 403 rather than the
    history. A handler that only imported `gate_read` would pass the AST scan in
    `tests/test_inv4_authorization_wiring.py` and fail here.
    """
    _org, _ds, _schema, routine = await _two_version_history(session)

    with pytest.raises(HTTPException) as refused:
        await _read(session, routine, settings=_DENYING_SETTINGS)
    assert refused.value.status_code == 403


async def test_another_organizations_routine_is_not_readable(session: AsyncSession) -> None:
    """INV-5. The routine is fetched by primary key, which crosses
    organizations, so the tenancy check is what makes this read scoped at all --
    and it has to be restated in the handler, not inherited from the query."""
    _org, _ds, _schema, routine = await _two_version_history(session)

    with pytest.raises(HTTPException) as refused:
        await _read(session, routine, context=_context(uuid4()))
    assert refused.value.status_code == 403


async def test_an_unknown_routine_is_a_404_not_an_empty_history(
    session: AsyncSession,
) -> None:
    """"No such routine" and "this routine has no captured definition" are
    different answers, and collapsing them would make the empty history above
    unreadable."""
    org, _ds, _schema = await seed_estate(session)
    with pytest.raises(HTTPException) as refused:
        await get_routine_definition_history(
            uuid4(),
            limit=20,
            offset=0,
            context=_context(org.id),
            session=session,
            settings=_SETTINGS,
        )
    assert refused.value.status_code == 404
