"""R11-FP08: a routine's description is built from what Atlas holds of its body.

The shape of `tests/test_asset_description_views.py`, for the routine axis. Two
properties that have to hold in every body state, and are easy to lose by
accident:

**The state reaches the evidence.** A routine whose body was withheld, truncated
or quarantined is a routine Atlas knows less about, and the draft has to say so
rather than reading as though the body had been read in full. Saying nothing is
the failure this closes: prose that omits the caveat is indistinguishable from
prose written against a complete body.

**The draft never quotes the body.** A procedure body is the largest
indirect-injection surface envelope 1.1 introduced (see the note on
`MetadataRoutine.body_sql_redacted`), and a description is prose every reader of
the routine -- and every model given the routine as context -- will read. Each
case below seeds a body containing a distinctive token and asserts no part of it
reaches the text, whatever the state.

Also here: the drift refusal (a published description must describe the body it
was written against, checked by *named definition version* rather than a
re-derived digest, which is the one thing this pair can do that the table and
view pairs cannot), and the package refusal by name.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.routine_description_service import (
    DEFINITION_MOVED,
    PACKAGE_NOT_DESCRIBABLE,
    apply_routine_description_draft,
    compose_routine_draft_text,
    ensure_routines_are_describable,
    gather_routine_evidence,
    is_describable_routine,
    routine_body_facts,
)
from tests.support.task_agents import seed_estate, task_agent_session
from tests.test_routine_description import (
    pending_draft,
    seed_definition_version,
    seed_routine,
    well_evidenced_routine,
)

#: A body carrying tokens nothing in a description may echo: the statement
#: keyword, an identifier only the body names, and a redaction placeholder.
BODY = (
    "CREATE PROCEDURE sp_settle() AS BEGIN "
    "INSERT INTO public.ledger SELECT ? FROM staging_zzz_secret_table END"
)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


@pytest.mark.parametrize(
    ("columns", "state"),
    [
        ({"body_sql_redacted": BODY, "body_fingerprint": "bf"}, "CAPTURED"),
        (
            {"body_sql_redacted": BODY, "body_fingerprint": "bf", "truncated": True},
            "TRUNCATED",
        ),
        (
            {
                "body_sql_redacted": BODY,
                "body_fingerprint": "bf",
                "screening_status": "QUARANTINED",
            },
            "QUARANTINED",
        ),
        (
            {
                "body_sql_redacted": None,
                "body_fingerprint": None,
                "availability": "UNAVAILABLE",
                "unavailable_reason": "module is encrypted",
            },
            "WITHHELD",
        ),
        ({"body_sql_redacted": "", "body_fingerprint": None}, "NOT_CAPTURED"),
    ],
)
async def test_the_body_state_reaches_the_evidence_and_the_draft_never_quotes_it(
    session: AsyncSession, columns: dict[str, Any], state: str
) -> None:
    org, datasource, schema = await seed_estate(session)
    routine = await seed_routine(session, org, datasource, schema, **columns)

    evidence = await gather_routine_evidence(session, routine)
    text = compose_routine_draft_text(evidence)

    assert evidence.body_state == state
    if columns.get("body_sql_redacted"):
        assert evidence.body_digest == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    else:
        assert evidence.body_digest is None
    assert text.startswith("sp_settle is a stored procedure in the public schema.")
    # The state is *said*, not left to be inferred from silence.
    assert state == "CAPTURED" or "body" in text
    for token in ("INSERT", "SELECT", "CREATE PROCEDURE", "staging_zzz_secret_table", "?"):
        assert token not in text


async def test_not_captured_is_an_empty_body_because_a_null_one_cannot_be_available(
    session: AsyncSession,
) -> None:
    """The one state a routine derives differently from a view, and why.

    `ck_metadata_routine_availability_matches_body` makes
    `availability = 'AVAILABLE'` exactly equivalent to a non-NULL
    `body_sql_redacted`, so the view's "no definition row at all" has no direct
    analogue on a routine: an AVAILABLE routine with a NULL body is refused by
    the database. What can exist is the encoding table's fourth row -- AVAILABLE
    with an empty string, the source answering and handing over no body text --
    and that is what `NOT_CAPTURED` reports. A NULL body is always a
    withholding, because the constraint means the source declined."""
    org, datasource, schema = await seed_estate(session)
    empty = await seed_routine(
        session, org, datasource, schema, name="sp_empty", body_sql_redacted=""
    )
    withheld = await seed_routine(
        session,
        org,
        datasource,
        schema,
        name="sp_withheld",
        body_sql_redacted=None,
        availability="UNAVAILABLE",
        unavailable_reason="module is encrypted",
    )

    assert routine_body_facts(empty) == ("NOT_CAPTURED", None)
    assert routine_body_facts(withheld) == ("WITHHELD", None)

    # The constraint, asserted rather than assumed -- it is what makes the
    # derivation above the only honest one.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await seed_routine(
            session,
            org,
            datasource,
            schema,
            name="sp_impossible",
            body_sql_redacted=None,
            availability="AVAILABLE",
        )
    await session.rollback()


async def test_a_draft_is_not_published_once_the_routines_body_has_moved(
    session: AsyncSession,
) -> None:
    """Drift is a *named version* comparison, not a re-derived digest.

    `MetadataRoutineDefinitionVersion` is immutable and append-only, so a rescan
    that captured a redefined body wrote a new row and the published
    description's provenance points at the old one. That is stronger than
    hashing the current text -- a retire-and-recapture cycle producing
    byte-identical text is a real change a digest would call unchanged -- and
    cheaper, because nothing has to be read and hashed to answer it."""
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(session, routine)
    await seed_definition_version(session, routine, version_number=2)

    with pytest.raises(HTTPException) as refused:
        await apply_routine_description_draft(
            session, draft, reviewer="reviewer", now=datetime.now(UTC)
        )

    assert refused.value.status_code == 409
    detail = refused.value.detail
    assert isinstance(detail, dict) and detail["code"] == DEFINITION_MOVED
    assert str(detail).startswith("The routine's body changed")
    assert (draft.status, draft.published_version_id) == ("PENDING_APPROVAL", None)


async def test_a_draft_is_not_published_once_the_routine_itself_is_retired(
    session: AsyncSession,
) -> None:
    """`tool_source_binding.current_source_definition` is the one function that
    answers "does this routine still stand?", reused here so a retired routine
    cannot mean one thing to a governed tool and another to a description."""
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(session, routine)
    routine.status = "DEPRECATED"
    await session.flush()

    with pytest.raises(HTTPException) as refused:
        await apply_routine_description_draft(
            session, draft, reviewer="reviewer", now=datetime.now(UTC)
        )

    assert refused.value.status_code == 409
    assert "no longer active" in str(refused.value.detail)


async def test_a_draft_whose_body_still_stands_is_published(session: AsyncSession) -> None:
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(session, routine)

    event_type, version = await apply_routine_description_draft(
        session, draft, reviewer="reviewer", now=datetime.now(UTC)
    )

    assert (event_type, draft.status) == ("routine_description.approved.v1", "APPROVED")
    assert version.description == draft.drafted_text
    # Published prose never carries the body either.
    assert "INSERT" not in version.description


async def test_a_package_is_refused_by_name_and_not_silently_skipped(
    session: AsyncSession,
) -> None:
    """Packages wait on R11-FP03, and every other routine-consuming path in the
    platform excludes them (`footprint_gaps`, `footprint_gap_detail`,
    `procedure_tool_blueprint`). A package is a container for subprograms, not a
    callable unit with a signature, a return type or lineage of its own, so a
    description of one would be a description of nothing.

    Refused with a reason rather than dropped from the result: a steward who
    names a package and gets an empty response has learned nothing about why,
    and the refusal lists the ids so the client can drop them and ask again."""
    org, datasource, schema = await seed_estate(session)
    package = await seed_routine(
        session,
        org,
        datasource,
        schema,
        name="pkg_settlement",
        routine_type="PACKAGE",
        body_sql_redacted=BODY,
    )
    member = await seed_routine(
        session,
        org,
        datasource,
        schema,
        name="settle",
        package_name="pkg_settlement",
        body_sql_redacted=BODY,
    )

    assert is_describable_routine(package) is False
    assert is_describable_routine(member) is True

    with pytest.raises(HTTPException) as refused:
        ensure_routines_are_describable([member, package])

    assert refused.value.status_code == 422
    detail = refused.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == PACKAGE_NOT_DESCRIBABLE
    assert detail["routine_ids"] == [str(package.id)]
    assert "R11-FP03" in str(detail)
    # A describable request is not refused.
    ensure_routines_are_describable([member])
