"""R11-FP08 exit-condition tests: a routine's Atlas-authored description.

Tables and views had the whole description lifecycle; a routine had none of it,
so the only thing the platform could say about a procedure was the source's own
comment. This file pins the three properties the extension has to preserve --
the same three `tests/test_asset_description.py` pins for tables and
`tests/test_column_description.py` for columns, because a third governance
contract for one kind of fact is exactly what R11-S4 froze:

(a) a low-evidence draft can never reach a state a non-reviewer could mistake
    for published: the shared minimum-evidence gate blocks it before a
    `governance_review` row exists, and the gate is the *same* constant, not a
    routine-specific one;

(b) a high-confidence draft still requires independent approval -- there is one
    publisher, reached only through the adapter registry the decision service
    dispatches on, and editing a draft disqualifies the editor as its approver;

(c) the type is registered in the tier ladder at T0. Left unregistered,
    `risk_tier_for` fails closed to T3, which is *above* every agent's hard
    ceiling -- so the draft would not be "held to a higher bar" but invisible to
    `agent_decidable_object_types` and to every tier-counted oversight bound.

`tests/test_routine_description_body_states.py` covers the body-state
vocabulary, the no-body-text rule, the drift refusal and the package refusal.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import aida.reviewer_agent as reviewer_agent
import aida.semantic_api as semantic_api
from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    ensure_reviewable,
    text_fingerprint,
)
from aida.envelope_models import (
    MetadataRoutine,
    MetadataRoutineDefinitionVersion,
    MetadataRoutineParameter,
    RoutineDescriptionDraft,
)
from aida.models import DataSource, GovernanceReview, MetadataSchema, Organization
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.review_risk_tiers import TIER_T0, agent_decidable_object_types, risk_tier_for
from aida.routine_description_service import (
    ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE,
    apply_routine_description_draft,
    compose_routine_draft_text,
    current_routine_description,
    gather_routine_evidence,
    publish_routine_documentation_version,
    reject_routine_description_draft,
    resolve_routine_description,
    routine_evidence_payload,
    routine_refusal_reason,
    score_routine_evidence,
)
from tests.support.task_agents import seed_estate, seed_table, task_agent_session

BODY = "CREATE PROCEDURE sp_settle() AS BEGIN INSERT INTO public.ledger SELECT ? END"


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def seed_routine(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str = "sp_settle",
    routine_type: str = "PROCEDURE",
    **overrides: Any,
) -> MetadataRoutine:
    """One routine, with the columns a description's evidence actually reads.

    Shared with `tests/test_routine_description_body_states.py` rather than
    added to `tests/support/task_agents.py`: that module is the task-agent
    estate, and a routine is not part of it.

    Callers must pass a body consistent with `availability`:
    `ck_metadata_routine_availability_matches_body` makes AVAILABLE exactly
    equivalent to a non-NULL `body_sql_redacted`, so AVAILABLE needs `""` or
    real text and UNAVAILABLE needs `None`. No default is supplied here
    because either default would be wrong for half the cases.
    """
    fields: dict[str, Any] = {
        "signature": "()",
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "availability": "AVAILABLE",
        "status": "ACTIVE",
    }
    fields.update(overrides)
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        routine_type=routine_type,
        fingerprint="fp",
        **fields,
    )
    session.add(routine)
    await session.flush()
    return routine


def lineage_edge(
    org: Organization,
    datasource: DataSource,
    routine: MetadataRoutine,
    *,
    statement_ordinal: int = 1,
    source_table_id: Any = None,
    target_table_id: Any = None,
    is_write: bool = False,
    is_intermediate: bool = False,
    review_status: str = "ACTIVE",
    transformation_type: str = "INSERT_SELECT",
) -> DeepProcedureLineageEdge:
    """One parsed routine-lineage edge, with the NOT NULL columns filled in.

    `source_table`/`target_table` are the parser's raw names and are required by
    the model; the description evidence never reads them, only the resolved ids.
    """
    return DeepProcedureLineageEdge(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        routine_id=routine.id,
        statement_ordinal=statement_ordinal,
        source_table="public.postings",
        source_column="*",
        target_table="public.ledger",
        target_column="*",
        source_resolved=source_table_id is not None,
        source_table_id=source_table_id,
        target_table_id=target_table_id,
        transformation_type=transformation_type,
        confidence="HIGH",
        dialect="postgres",
        sql_hash=uuid4().hex,
        is_write=is_write,
        is_intermediate=is_intermediate,
        review_status=review_status,
    )


async def seed_definition_version(
    session: AsyncSession, routine: MetadataRoutine, *, version_number: int = 1
) -> MetadataRoutineDefinitionVersion:
    version = MetadataRoutineDefinitionVersion(
        id=uuid4(),
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        body_sql_redacted=routine.body_sql_redacted,
        body_fingerprint=routine.body_fingerprint,
        availability=routine.availability,
        truncated=routine.truncated,
        redaction_status=routine.redaction_status,
        screening_status=routine.screening_status,
        version_number=version_number,
        captured_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    return version


async def well_evidenced_routine(
    session: AsyncSession,
) -> tuple[Organization, DataSource, MetadataSchema, MetadataRoutine]:
    """A routine carrying enough evidence to clear the shared review bar."""
    org, datasource, schema = await seed_estate(session)
    ledger = await seed_table(session, org, datasource, schema, name="ledger")
    postings = await seed_table(session, org, datasource, schema, name="postings")
    routine = await seed_routine(
        session,
        org,
        datasource,
        schema,
        signature="(p_as_of date)",
        body_sql_redacted=BODY,
        body_fingerprint="bf",
        source_description="Settles the day's postings into the ledger.",
        is_deterministic=False,
        security_mode="DEFINER",
    )
    session.add(
        MetadataRoutineParameter(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            name="p_as_of",
            ordinal_position=1,
            physical_type="date",
            fingerprint="fp",
        )
    )
    for ordinal, (source_id, target_id, is_write) in enumerate(
        ((postings.id, None, False), (None, ledger.id, True)), start=1
    ):
        session.add(
            lineage_edge(
                org,
                datasource,
                routine,
                statement_ordinal=ordinal,
                source_table_id=source_id,
                target_table_id=target_id,
                is_write=is_write,
            )
        )
    await seed_definition_version(session, routine)
    await session.flush()
    return org, datasource, schema, routine


async def pending_draft(
    session: AsyncSession, routine: MetadataRoutine, **overrides: Any
) -> RoutineDescriptionDraft:
    evidence = await gather_routine_evidence(session, routine)
    scores = score_routine_evidence(evidence)
    fields: dict[str, Any] = {
        "evidence": routine_evidence_payload(evidence),
        "status": "PENDING_APPROVAL",
        "base_description_version": evidence.current_description_version,
    }
    fields.update(overrides)
    drafted_text = compose_routine_draft_text(evidence)
    draft = RoutineDescriptionDraft(
        id=uuid4(),
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        drafted_text=drafted_text,
        text_fingerprint=text_fingerprint(drafted_text),
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall,
        created_by="agent:steward",
        **fields,
    )
    session.add(draft)
    await session.flush()
    return draft


# --- (a) the evidence gate is the shared one -------------------------------


async def test_a_routine_with_nothing_known_about_it_cannot_reach_review(
    session: AsyncSession,
) -> None:
    """A name and a kind are not a description. This is the case the module
    exists to refuse: `sp_recalc` with a withheld body, no parsed lineage and no
    source comment yields prose that is true and says nothing, and the shared
    bar keeps it out of a reviewer's queue."""
    org, datasource, schema = await seed_estate(session)
    routine = await seed_routine(
        session,
        org,
        datasource,
        schema,
        name="sp_recalc",
        signature="",
        availability="UNAVAILABLE",
        unavailable_reason="module is encrypted",
    )

    evidence = await gather_routine_evidence(session, routine)
    scores = score_routine_evidence(evidence)

    assert scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW
    with pytest.raises(HTTPException) as refused:
        ensure_reviewable(scores.overall)
    assert refused.value.status_code == 422


async def test_the_review_bar_is_the_same_constant_for_all_three_description_kinds() -> None:
    """One threshold, imported, not copied. Three constants would drift, and a
    routine-specific bar would be the first place to quietly lower one."""
    import aida.column_description_api as column_description_api
    import aida.routine_description_api as routine_description_api

    assert (
        routine_description_api.MINIMUM_EVIDENCE_FOR_REVIEW
        is column_description_api.MINIMUM_EVIDENCE_FOR_REVIEW
        is MINIMUM_EVIDENCE_FOR_REVIEW
    )


async def test_a_well_evidenced_routine_clears_the_bar(session: AsyncSession) -> None:
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)

    evidence = await gather_routine_evidence(session, routine)
    scores = score_routine_evidence(evidence)
    text = compose_routine_draft_text(evidence)

    assert scores.overall >= MINIMUM_EVIDENCE_FOR_REVIEW
    assert evidence.reads_table_names == ("postings",)
    assert evidence.writes_table_names == ("ledger",)
    assert "It reads from postings." in text
    assert "It writes to ledger." in text
    assert "Settles the day's postings into the ledger." in text


async def test_a_proposed_or_rejected_lineage_edge_is_not_evidence(
    session: AsyncSession,
) -> None:
    """ADR-0026's line, applied to routine lineage. An edge nobody has approved
    is not yet a fact about what the procedure touches, and one a reviewer
    rejected is evidence of nothing -- so neither may appear in prose a reader
    will take as the platform's own statement."""
    org, datasource, schema = await seed_estate(session)
    table = await seed_table(session, org, datasource, schema, name="ledger")
    routine = await seed_routine(session, org, datasource, schema, body_sql_redacted=BODY)
    for ordinal, review_status in enumerate(("PROPOSED", "REJECTED"), start=1):
        session.add(
            lineage_edge(
                org,
                datasource,
                routine,
                statement_ordinal=ordinal,
                target_table_id=table.id,
                is_write=True,
                review_status=review_status,
            )
        )
    await session.flush()

    evidence = await gather_routine_evidence(session, routine)

    assert evidence.writes_table_names == ()
    assert evidence.lineage_edge_count == 0
    assert "ledger" not in compose_routine_draft_text(evidence)


async def test_a_temp_table_hop_is_the_procedures_own_plumbing(
    session: AsyncSession,
) -> None:
    """`is_intermediate` edges are excluded, the filter
    `asset_description_service._parsed_lineage_neighbours` already applies to
    the same table: a scratch table a procedure builds and drops is not
    something a reader should be told it writes."""
    org, datasource, schema = await seed_estate(session)
    scratch = await seed_table(session, org, datasource, schema, name="tmp_stage")
    routine = await seed_routine(session, org, datasource, schema, body_sql_redacted=BODY)
    session.add(
        lineage_edge(
            org,
            datasource,
            routine,
            target_table_id=scratch.id,
            is_write=True,
            is_intermediate=True,
        )
    )
    await session.flush()

    evidence = await gather_routine_evidence(session, routine)

    assert evidence.writes_table_names == ()
    assert "tmp_stage" not in compose_routine_draft_text(evidence)


# --- (b) one publisher, reached only through the registry -------------------


def test_the_only_routine_description_publisher_is_the_governed_adapter() -> None:
    """`apply_routine_description_draft` is called from exactly one place, and
    that place is reachable only through the adapter registry the decision
    service dispatches on -- never called directly by an endpoint. The same
    assertion `tests/test_asset_description.py` makes for tables."""
    adapter_source = inspect.getsource(semantic_api._decide_routine_description_draft)
    assert "apply_routine_description_draft(" in adapter_source
    assert "reject_routine_description_draft(" in adapter_source

    assert (
        semantic_api._TARGET_EFFECT_ADAPTERS[ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE]
        is semantic_api._decide_routine_description_draft
    )
    decide_source = inspect.getsource(semantic_api.decide_governance_review)
    assert "apply_routine_description_draft(" not in decide_source
    assert "_apply_governance_review_decision(" in decide_source


def test_no_routine_description_api_endpoint_publishes() -> None:
    """There is no approve endpoint, on purpose: a direct publish route would
    be a way around the one gate that makes the content trustworthy."""
    import aida.routine_description_api as routine_description_api

    source = inspect.getsource(routine_description_api)
    assert "apply_routine_description_draft" not in source
    assert "publish_routine_documentation_version" not in source


async def test_a_draft_editor_cannot_approve_their_own_edit(session: AsyncSession) -> None:
    """Editing is authorship. The guard here is one half of a mechanism whose
    other half is the `editors` stamp in `edit_routine_description_draft`."""
    from tests.support.doubles import security_context

    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(
        session,
        routine,
        evidence={
            **routine_evidence_payload(await gather_routine_evidence(session, routine)),
            "editors": ["steward-2"],
            "edited_by": "steward-2",
        },
    )
    review = GovernanceReview(
        id=uuid4(),
        organization_id=routine.organization_id,
        object_type=ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE,
        object_id=str(draft.id),
        requested_action="PUBLISH",
        requested_by="steward-1",
    )
    session.add(review)
    await session.flush()

    with pytest.raises(HTTPException) as refused:
        await semantic_api._decide_routine_description_draft(
            session,
            review,
            decision="APPROVE",
            reason=None,
            context=security_context(
                organization_id=routine.organization_id,
                principal_id="steward-2",
                roles=frozenset({"Reviewer"}),
            ),
            now=datetime.now(UTC),
        )

    assert refused.value.status_code == 409
    assert "editor cannot approve" in str(refused.value.detail)
    assert draft.status == "PENDING_APPROVAL"


async def test_approval_publishes_and_supersedes_the_prior_version(
    session: AsyncSession,
) -> None:
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    now = datetime.now(UTC)
    first = await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description="An earlier, human-authored description.",
        created_by="steward-1",
        approved_by="reviewer",
        approved_at=now,
    )
    draft = await pending_draft(session, routine, base_description_version=1)

    event_type, published = await apply_routine_description_draft(
        session, draft, reviewer="reviewer", now=now
    )

    assert event_type == "routine_description.approved.v1"
    assert (published.version, published.status) == (2, "APPROVED")
    assert published.description == draft.drafted_text
    assert first.status == "SUPERSEDED"
    assert draft.published_version_id == published.id
    # The published version names the body it describes, which is the edge the
    # table and column stores have no analogue for.
    assert published.source_definition_version_id is not None
    current = await current_routine_description(session, routine.id)
    assert current is not None and current.id == published.id


async def test_a_draft_written_against_an_older_version_is_refused(
    session: AsyncSession,
) -> None:
    """`column_description_service`'s lost-update rule, applied to the third
    path. Someone published in the window, so the reviewer read a draft written
    against text that is no longer current."""
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(session, routine, base_description_version=None)
    await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description="Someone else described it first.",
        created_by="steward-1",
        approved_by="reviewer",
        approved_at=datetime.now(UTC),
    )

    with pytest.raises(HTTPException) as refused:
        await apply_routine_description_draft(
            session, draft, reviewer="reviewer", now=datetime.now(UTC)
        )

    assert refused.value.status_code == 409
    assert "changed after the draft was composed" in str(refused.value.detail)
    assert (draft.status, draft.published_version_id) == ("PENDING_APPROVAL", None)


async def test_a_draft_for_a_retired_routine_is_refused(session: AsyncSession) -> None:
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


async def test_a_rejected_draft_is_kept_as_negative_knowledge(
    session: AsyncSession,
) -> None:
    """R11-FP10's rule for the third draft kind: the same words, or the same
    evidence under different words, do not come back."""
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(session, routine)

    event_type = await reject_routine_description_draft(
        draft, reviewer="reviewer", now=datetime.now(UTC)
    )
    await session.flush()

    assert (event_type, draft.status) == ("routine_description.rejected.v1", "REJECTED")
    evidence = await gather_routine_evidence(session, routine)
    assert (
        await routine_refusal_reason(
            session,
            routine.id,
            drafted_text=draft.drafted_text,
            payload=routine_evidence_payload(evidence),
        )
        == "TEXT"
    )
    assert (
        await routine_refusal_reason(
            session,
            routine.id,
            drafted_text="Entirely different words about the same procedure.",
            payload=routine_evidence_payload(evidence),
        )
        == "EVIDENCE"
    )


# --- (c) the tier ladder ----------------------------------------------------


def test_the_routine_draft_type_is_registered_at_t0() -> None:
    """Mandatory, not cosmetic. `risk_tier_for` fails closed to T3 for an
    unknown type, and T3 is above `HARD_MAX_AGENT_TIER`: an unregistered routine
    draft would be outside the ladder rather than high in it -- absent from
    `agent_decidable_object_types`, uncounted by every tier-counted oversight
    bound, and mis-reported to an auditor reading tiers as a description of what
    automation may touch."""
    assert risk_tier_for(ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE) == TIER_T0
    assert risk_tier_for("ASSET_DESCRIPTION_DRAFT") == TIER_T0
    assert ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE in agent_decidable_object_types(TIER_T0)


async def test_the_reviewer_agent_abstains_on_a_routine_description(
    session: AsyncSession,
) -> None:
    """The deliberate omission, asserted rather than described in a comment.

    T0 puts routine drafts inside the agent's ceiling, so what keeps the agent
    off them is the absence of an evidence resolver. `score_routine_evidence`
    measures how well *catalogued* a routine is -- a signature exists, a body is
    held, some approved lineage exists -- and a routine carries no authored
    statement of meaning for it to read, so a resolver would add a number that
    clears the approve threshold and says nothing about whether the prose is
    true. See `reviewer_agent._EVIDENCE_RESOLVERS` for the full reasoning."""
    assert ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE not in reviewer_agent._EVIDENCE_RESOLVERS

    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    draft = await pending_draft(session, routine)
    review = GovernanceReview(
        id=uuid4(),
        organization_id=routine.organization_id,
        object_type=ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE,
        object_id=str(draft.id),
        requested_action="PUBLISH",
        requested_by="steward-1",
    )
    session.add(review)
    await session.flush()

    evidence = await reviewer_agent._resolve_evidence(session, review)

    assert evidence.resolved is False
    assert evidence.reason == reviewer_agent.EVIDENCE_NO_RESOLVER


# --- the read surface -------------------------------------------------------


async def test_the_precedence_chain_prefers_approved_then_proposed_then_the_source(
    session: AsyncSession,
) -> None:
    """`atlas.modules.catalog.service._description`'s rungs, for a routine. The
    flags are what keep the collapse honest: without them a reader cannot tell
    prose the platform asserts from a proposal nobody approved, or from the
    source system's own comment."""
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)

    # Rung 3: only the source's comment.
    resolved = await resolve_routine_description(session, routine)
    assert resolved.text == "Settles the day's postings into the ledger."
    assert (resolved.is_proposed, resolved.is_withdrawn, resolved.is_source_comment) == (
        False,
        False,
        True,
    )

    # Rung 2: a draft awaiting review, carried as a proposal.
    draft = await pending_draft(session, routine)
    resolved = await resolve_routine_description(session, routine)
    assert resolved.text == draft.drafted_text
    assert (resolved.is_proposed, resolved.is_source_comment) == (True, False)

    # Rung 1: an approved version outranks both.
    await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description="Approved: settles postings into the ledger nightly.",
        created_by="steward-1",
        approved_by="reviewer",
        approved_at=datetime.now(UTC),
    )
    resolved = await resolve_routine_description(session, routine)
    assert resolved.text == "Approved: settles postings into the ledger nightly."
    assert (resolved.is_proposed, resolved.is_withdrawn, resolved.is_source_comment) == (
        False,
        False,
        False,
    )


async def test_a_withdrawn_description_returns_the_routine_to_its_source_comment(
    session: AsyncSession,
) -> None:
    """Withdrawal is a decision about what *this platform* says. It returns the
    routine to the state it was in before anyone here described it rather than
    suppressing observed source metadata Atlas has no authority over -- the
    answer the table chain already gives -- and `is_withdrawn` is still reported
    so "described once, retired" reads differently from "never described"."""
    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    version = await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description="Approved, then retired.",
        created_by="steward-1",
        approved_by="reviewer",
        approved_at=datetime.now(UTC),
    )
    version.status = "WITHDRAWN"
    await session.flush()

    resolved = await resolve_routine_description(session, routine)

    assert resolved.text == "Settles the day's postings into the ledger."
    assert (resolved.is_withdrawn, resolved.is_source_comment) == (True, True)
    assert await current_routine_description(session, routine.id) is None


async def test_a_context_product_ships_a_routines_meaning_at_both_doors(
    session: AsyncSession,
) -> None:
    """R11-FP08's read-surface half. A routine in a context product used to ship
    a digest and no meaning while the tables beside it carried descriptions.
    `coverage_section` is what both doors render -- compilation and MCP's
    resource read -- so the assertion is made against it."""
    from aida.context_compiler import coverage_section
    from aida.context_product_coverage import load_routine_references

    _org, _datasource, _schema, routine = await well_evidenced_routine(session)
    await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description="Settles postings into the ledger nightly.",
        created_by="steward-1",
        approved_by="reviewer",
        approved_at=datetime.now(UTC),
    )

    references = await load_routine_references(
        session, routine.organization_id, [routine.id], []
    )
    section = coverage_section(references, [])

    assert len(section["routines"]) == 1
    shipped = section["routines"][0]
    assert shipped["description"] == "Settles postings into the ledger nightly."
    assert shipped["description_state"] == "APPROVED"
    # Never the body, at either door.
    assert "CREATE PROCEDURE" not in str(section)


async def test_an_undescribed_or_proposed_routine_ships_no_text_but_says_which(
    session: AsyncSession,
) -> None:
    from aida.context_product_coverage import load_routine_references

    _org, _datasource, _schema, routine = await well_evidenced_routine(session)

    undescribed = await load_routine_references(
        session, routine.organization_id, [routine.id], []
    )
    assert (undescribed[0].description, undescribed[0].description_state) == (None, "NONE")

    await pending_draft(session, routine)
    proposed = await load_routine_references(
        session, routine.organization_id, [routine.id], []
    )
    # A draft is not what the platform asserts, so no text -- but a consumer is
    # told one exists rather than being left to read `None` as "nobody looked".
    assert (proposed[0].description, proposed[0].description_state) == (None, "PROPOSED")
