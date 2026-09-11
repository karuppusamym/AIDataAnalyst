"""ADR-0029: the steward agent.

The first task agent (`aida.task_agent`): a workload identity -- `agent:steward`
by default -- that works the stewardship backlog under its own `AgentContract`.
`Docs/10-architecture/adr/ADR-0029-steward-agent.md` records why it exists.

Its three capabilities:

* **TABLE_DESCRIPTION.** Tables come from the AT-5 documentation worklist in
  its default priority order (`usage x impact x deficit`), so the agent works
  the backlog in the order a human steward is shown it rather than in one of its
  own. Each gets GL-9's evidence-composed draft (`asset_description_service`),
  submitted for review as the agent's own request. Left alone: a table whose
  draft is already open, text a reviewer already rejected for that table, and a
  draft under GL-9's evidence bar -- which could never be submitted anyway.
* **COLUMN_DESCRIPTION.** The columns of the same worklist tables, in the same
  order, that have no approved description, were not deliberately retired and
  have no draft open. Each gets the column drafter's evidence-composed draft
  (`column_description_service`, never its model-assisted path), submitted as
  the agent's own request. A column too thin to clear the review bar is passed
  over without an item: a table's worth of them would bury the run's list, and
  the column drafts screen lists every one. Text a reviewer rejected for a
  column is never raised again.
* **GLOSSARY_LINK.** GL-8's approved-label exact matches
  (`glossary_link_candidates`), each proposed and submitted. A link a reviewer
  rejected is never raised again.

Authority, the kill switch, the tier ceiling, budgets, the one write path, the
ledger and the outcome measure belong to the shared runtime, not to this
module, so they cannot differ between agents. What is here is only *what* this
agent proposes and *how it chooses*. Nothing here calls a model: all three
producers are deterministic functions of catalog rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    compose_draft_text,
    evidence_payload,
    gather_evidence,
    score_evidence,
    text_fingerprint,
)
from aida.column_description_service import (
    COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE,
    ORIGIN_METADATA,
    ColumnEvidence,
    column_evidence_payload,
    compose_column_draft_text,
    gather_table_column_evidence,
    score_column_evidence,
)
from aida.column_description_service import OPEN_DRAFT_STATUSES as _COLUMN_OPEN_DRAFT_STATUSES
from aida.column_documentation import current_descriptions_by_column_id
from aida.description_withdrawal import withdrawn_column_versions
from aida.documentation_worklist import rank_documentation_worklist
from aida.documentation_worklist_signals import gather_documentation_worklist_signals
from aida.glossary_link_candidates import (
    GlossaryLinkCandidate,
    build_glossary_link_proposal,
    find_glossary_link_candidates,
)
from aida.models import AssetDescriptionDraft, ColumnDescriptionDraft, MetadataColumn, MetadataTable
from aida.security import SecurityContext
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    CapabilityWork,
    TaskAgentCapability,
    TaskAgentItem,
    TaskAgentOutcome,
    TaskAgentRun,
    TaskAgentRunRequest,
    TaskAgentSpec,
    run_task_agent,
)
from atlas.platform.config import Settings

CAPABILITY_TABLE_DESCRIPTION: Final = "TABLE_DESCRIPTION"
CAPABILITY_COLUMN_DESCRIPTION: Final = "COLUMN_DESCRIPTION"
CAPABILITY_GLOSSARY_LINK: Final = "GLOSSARY_LINK"

STEWARD_AGENT: Final = TaskAgentSpec(
    key="steward",
    audit_roles=frozenset({"DataSteward"}),
    capabilities=(
        TaskAgentCapability(
            key=CAPABILITY_TABLE_DESCRIPTION,
            object_type="ASSET_DESCRIPTION_DRAFT",
            intent="steward.propose_table_description",
            producer="asset_description_service: GL-9 draft composed from catalog evidence",
        ),
        TaskAgentCapability(
            key=CAPABILITY_COLUMN_DESCRIPTION,
            object_type=COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE,
            intent="steward.propose_column_description",
            producer="column_description_service: evidence-scored column draft, no model",
        ),
        TaskAgentCapability(
            key=CAPABILITY_GLOSSARY_LINK,
            object_type="GLOSSARY_LINK_PROPOSAL",
            intent="steward.propose_glossary_link",
            producer="glossary_link_candidates: GL-8 approved-label exact match",
        ),
    ),
)

# Skips: one table or link the agent looked at and deliberately left alone.
SKIP_OPEN_DRAFT: Final = "open_draft_exists"
SKIP_REJECTED_BEFORE: Final = "identical_text_rejected"
SKIP_BELOW_EVIDENCE_BAR: Final = "below_evidence_bar"

#: Tables examined per run, as a multiple of the proposal limit. Bounds the work
#: of a run whose worklist is mostly already in review: with a limit of 10 it
#: reads at most 40 tables' evidence, however long the backlog is.
_EXAMINE_FACTOR: Final = 4

#: The same default a steward's own request uses
#: (`schemas.GlossaryLinkProposalGenerate.minimum_confidence`). Every GL-8 match
#: scores 1.0 or 0.92, so this admits every exact label match and nothing else.
_LINK_MINIMUM_CONFIDENCE: Final = 0.75

#: A table with a draft in either state is already somebody's work in progress.
#: The same pair `asset_description_api.generate_asset_description_drafts` uses.
_OPEN_DRAFT_STATUSES: Final = ("DRAFT", "PENDING_APPROVAL")


# ---------------------------------------------------------------------------
# TABLE_DESCRIPTION
# ---------------------------------------------------------------------------


async def _worklist(run: TaskAgentRun) -> tuple[list[Any], dict[UUID, MetadataTable]]:
    """The AT-5 worklist's entries in the steward's own order -- `priority`
    ranking, real usage first and zero-volume tables after it, documented tables
    already excluded -- and its active tables within the run's scope."""
    session = run.session
    signals = await gather_documentation_worklist_signals(
        session,
        organization_id=run.organization_id,
        scan_limit=run.settings.agent_retrieval_scan_limit,
        include_zero_volume=True,
    )
    entries, _total = rank_documentation_worklist(
        signals, limit=len(signals), include_zero_volume=True
    )
    if not entries:
        return [], {}
    table_filters: list[Any] = [
        MetadataTable.organization_id == run.organization_id,
        MetadataTable.id.in_([entry.table_id for entry in entries]),
        MetadataTable.status == "ACTIVE",
    ]
    if run.datasource_id is not None:
        table_filters.append(MetadataTable.datasource_id == run.datasource_id)
    tables = {
        table.id: table
        for table in (await session.scalars(select(MetadataTable).where(*table_filters))).all()
    }
    return list(entries), tables


async def _table_descriptions(run: TaskAgentRun) -> None:
    session = run.session
    entries, tables = await _worklist(run)
    if not tables:
        return
    open_drafts = set(
        (
            await session.scalars(
                select(AssetDescriptionDraft.table_id).where(
                    AssetDescriptionDraft.organization_id == run.organization_id,
                    AssetDescriptionDraft.table_id.in_(list(tables)),
                    AssetDescriptionDraft.status.in_(_OPEN_DRAFT_STATUSES),
                )
            )
        ).all()
    )
    proposed = 0
    examined = 0
    examine_budget = run.outcome.limit * _EXAMINE_FACTOR
    for entry in entries:
        table = tables.get(entry.table_id)
        if table is None:
            continue
        if proposed >= run.outcome.limit or examined >= examine_budget:
            return
        examined += 1
        if table.id in open_drafts:
            run.add(
                run.item(
                    CAPABILITY_TABLE_DESCRIPTION,
                    action=ACTION_SKIPPED,
                    subject_id=table.id,
                    subject_name=table.name,
                    reason=SKIP_OPEN_DRAFT,
                    rank=entry.rank,
                )
            )
            continue
        if not await run.may_continue():
            return
        item = run.add(
            await run.guarded(
                CAPABILITY_TABLE_DESCRIPTION,
                subject_id=table.id,
                subject_name=table.name,
                rank=entry.rank,
                work=partial(_draft_table_description, run, table, entry.rank),
            )
        )
        if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
            proposed += 1


async def _draft_table_description(
    run: TaskAgentRun, table: MetadataTable, rank: int
) -> TaskAgentItem:
    capability = CAPABILITY_TABLE_DESCRIPTION
    session = run.session
    table_id, table_name = table.id, table.name
    evidence = await gather_evidence(session, table)
    drafted_text = compose_draft_text(evidence)
    fingerprint = text_fingerprint(drafted_text)
    # Negative knowledge: these words, for this table, were already put in front
    # of a human, who said no.
    rejected_before = await session.scalar(
        select(AssetDescriptionDraft.id)
        .where(
            AssetDescriptionDraft.table_id == table_id,
            AssetDescriptionDraft.status == "REJECTED",
            AssetDescriptionDraft.text_fingerprint == fingerprint,
        )
        .limit(1)
    )
    if rejected_before is not None:
        return run.item(
            capability,
            action=ACTION_SKIPPED,
            subject_id=table_id,
            subject_name=table_name,
            reason=SKIP_REJECTED_BEFORE,
            rank=rank,
        )
    scores = score_evidence(evidence)
    # GL-9's own submission bar (`ensure_reviewable`). A draft under it could
    # never be submitted, so the agent does not create one.
    if scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
        return run.item(
            capability,
            action=ACTION_SKIPPED,
            subject_id=table_id,
            subject_name=table_name,
            reason=SKIP_BELOW_EVIDENCE_BAR,
            confidence=scores.overall,
            rank=rank,
        )
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=table_id,
            subject_name=table_name,
            confidence=scores.overall,
            rank=rank,
        )
    draft = AssetDescriptionDraft(
        organization_id=run.organization_id,
        table_id=table_id,
        drafted_text=drafted_text,
        text_fingerprint=fingerprint,
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall,
        evidence={
            **evidence_payload(evidence),
            "origin": "METADATA",
            "worklist_rank": rank,
            "agent_run": run.outcome.run_id,
        },
        # Submitted as it is created: the agent's draft *is* its request for
        # review, the transition a steward makes with
        # `submit_asset_description_draft`.
        status="PENDING_APPROVAL",
        created_by=run.principal_id,
    )
    session.add(draft)
    await session.flush()
    review = await run.open_review(
        capability,
        object_id=draft.id,
        requested_action="PUBLISH",
        details={
            "table_id": str(table_id),
            "overall_score": scores.overall,
            "worklist_rank": rank,
        },
    )
    draft.governance_review_id = review.id
    return await run.proposed(
        capability,
        review=review,
        object_id=draft.id,
        subject_id=table_id,
        subject_name=table_name,
        inputs={
            "capability": capability,
            "table_id": str(table_id),
            "text_fingerprint": fingerprint,
        },
        confidence=scores.overall,
        rank=rank,
    )


# ---------------------------------------------------------------------------
# COLUMN_DESCRIPTION
# ---------------------------------------------------------------------------


async def _undescribed_columns(
    session: AsyncSession, table: MetadataTable
) -> list[MetadataColumn]:
    """A table's active columns with no approved description. A retired one is
    a decision, not a gap (`description_withdrawal`): drafting over it would
    quietly re-propose what a reviewer retired, so those are left out too --
    the same default the column drafts endpoint applies."""
    columns = list(
        (
            await session.scalars(
                select(MetadataColumn)
                .where(MetadataColumn.table_id == table.id, MetadataColumn.status == "ACTIVE")
                .order_by(MetadataColumn.ordinal_position, MetadataColumn.id)
            )
        ).all()
    )
    column_ids = [column.id for column in columns]
    described = await current_descriptions_by_column_id(session, column_ids)
    retired = await withdrawn_column_versions(
        session, [column_id for column_id in column_ids if column_id not in described]
    )
    return [column for column in columns if column.id not in described and column.id not in retired]


async def _column_descriptions(run: TaskAgentRun) -> None:
    session = run.session
    entries, tables = await _worklist(run)
    if not tables:
        return
    proposed = 0
    examined = 0
    examine_budget = run.outcome.limit * _EXAMINE_FACTOR
    for entry in entries:
        table = tables.get(entry.table_id)
        if table is None:
            continue
        if proposed >= run.outcome.limit or examined >= examine_budget:
            return
        columns = await _undescribed_columns(session, table)
        if not columns:
            continue
        open_drafts = set(
            (
                await session.scalars(
                    select(ColumnDescriptionDraft.column_id).where(
                        ColumnDescriptionDraft.column_id.in_([column.id for column in columns]),
                        ColumnDescriptionDraft.status.in_(_COLUMN_OPEN_DRAFT_STATUSES),
                    )
                )
            ).all()
        )
        # The chosen columns have no approved description, so no current
        # version to compose against: the same evidence a steward's request gets.
        evidence_by_column = await gather_table_column_evidence(session, table, columns, {})
        for column in columns:
            if proposed >= run.outcome.limit or examined >= examine_budget:
                return
            examined += 1
            subject_name = f"{table.name}.{column.name}"
            if column.id in open_drafts:
                run.add(
                    run.item(
                        CAPABILITY_COLUMN_DESCRIPTION,
                        action=ACTION_SKIPPED,
                        subject_id=column.id,
                        subject_name=subject_name,
                        reason=SKIP_OPEN_DRAFT,
                        rank=entry.rank,
                    )
                )
                continue
            evidence = evidence_by_column[column.id]
            scores = score_column_evidence(evidence)
            if scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
                continue
            if not await run.may_continue():
                return
            item = run.add(
                await run.guarded(
                    CAPABILITY_COLUMN_DESCRIPTION,
                    subject_id=column.id,
                    subject_name=subject_name,
                    rank=entry.rank,
                    work=partial(
                        _draft_column_description,
                        run,
                        table.id,
                        column.id,
                        subject_name,
                        evidence,
                        entry.rank,
                    ),
                )
            )
            if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
                proposed += 1


async def _draft_column_description(
    run: TaskAgentRun,
    table_id: UUID,
    column_id: UUID,
    subject_name: str,
    evidence: ColumnEvidence,
    rank: int,
) -> TaskAgentItem:
    capability = CAPABILITY_COLUMN_DESCRIPTION
    session = run.session
    drafted_text = compose_column_draft_text(evidence)
    fingerprint = text_fingerprint(drafted_text)
    # Negative knowledge, as for tables: these words, for this column, were
    # already put in front of a human, who said no.
    rejected_before = await session.scalar(
        select(ColumnDescriptionDraft.id)
        .where(
            ColumnDescriptionDraft.column_id == column_id,
            ColumnDescriptionDraft.status == "REJECTED",
            ColumnDescriptionDraft.text_fingerprint == fingerprint,
        )
        .limit(1)
    )
    if rejected_before is not None:
        return run.item(
            capability,
            action=ACTION_SKIPPED,
            subject_id=column_id,
            subject_name=subject_name,
            reason=SKIP_REJECTED_BEFORE,
            rank=rank,
        )
    scores = score_column_evidence(evidence)
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=column_id,
            subject_name=subject_name,
            confidence=scores.overall,
            rank=rank,
        )
    draft = ColumnDescriptionDraft(
        organization_id=run.organization_id,
        table_id=table_id,
        column_id=column_id,
        drafted_text=drafted_text,
        text_fingerprint=fingerprint,
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall,
        evidence={
            **column_evidence_payload(evidence),
            "origin": ORIGIN_METADATA,
            "worklist_rank": rank,
            "agent_run": run.outcome.run_id,
        },
        # Submitted as it is created, the transition a steward makes with
        # `submit_column_description_draft`.
        status="PENDING_APPROVAL",
        base_description_version=evidence.current_description_version,
        created_by=run.principal_id,
    )
    session.add(draft)
    await session.flush()
    review = await run.open_review(
        capability,
        object_id=draft.id,
        requested_action="PUBLISH",
        details={
            "table_id": str(table_id),
            "column_id": str(column_id),
            "overall_score": scores.overall,
            "worklist_rank": rank,
        },
    )
    draft.governance_review_id = review.id
    return await run.proposed(
        capability,
        review=review,
        object_id=draft.id,
        subject_id=column_id,
        subject_name=subject_name,
        inputs={
            "capability": capability,
            "table_id": str(table_id),
            "column_id": str(column_id),
            "text_fingerprint": fingerprint,
        },
        confidence=scores.overall,
        rank=rank,
    )


# ---------------------------------------------------------------------------
# GLOSSARY_LINK
# ---------------------------------------------------------------------------


async def _glossary_links(run: TaskAgentRun) -> None:
    scan = await find_glossary_link_candidates(
        run.session,
        organization_id=run.organization_id,
        minimum_confidence=_LINK_MINIMUM_CONFIDENCE,
        limit=run.outcome.limit,
        datasource_id=run.datasource_id,
        active_tables_only=True,
    )
    for candidate in scan.candidates:
        if not await run.may_continue():
            return
        run.add(
            await run.guarded(
                CAPABILITY_GLOSSARY_LINK,
                subject_id=candidate.table.id,
                subject_name=candidate.table.name,
                related_id=candidate.term.id,
                related_name=candidate.term_version.display_name,
                work=partial(_propose_glossary_link, run, candidate),
            )
        )


async def _propose_glossary_link(
    run: TaskAgentRun, candidate: GlossaryLinkCandidate
) -> TaskAgentItem:
    capability = CAPABILITY_GLOSSARY_LINK
    table_id, table_name = candidate.table.id, candidate.table.name
    term_id, term_name = candidate.term.id, candidate.term_version.display_name
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=table_id,
            subject_name=table_name,
            confidence=candidate.confidence,
            related_id=term_id,
            related_name=term_name,
        )
    proposal = build_glossary_link_proposal(
        candidate, organization_id=run.organization_id, created_by=run.principal_id
    )
    # Submitted as it is created, the transition a steward makes with
    # `submit_glossary_link_proposal`.
    proposal.status = "REVIEW_REQUIRED"
    run.session.add(proposal)
    await run.session.flush()
    review = await run.open_review(
        capability,
        object_id=proposal.id,
        requested_action="APPROVE_LINK",
        details={
            "table_id": str(table_id),
            "term_id": str(term_id),
            "confidence": candidate.confidence,
        },
    )
    proposal.governance_review_id = review.id
    return await run.proposed(
        capability,
        review=review,
        object_id=proposal.id,
        subject_id=table_id,
        subject_name=table_name,
        inputs={
            "capability": capability,
            "table_id": str(table_id),
            "term_id": str(term_id),
            "source_annotation_id": str(candidate.source_annotation_id),
        },
        confidence=candidate.confidence,
        related_id=term_id,
        related_name=term_name,
    )


STEWARD_WORK: Final[Mapping[str, CapabilityWork]] = {
    CAPABILITY_TABLE_DESCRIPTION: _table_descriptions,
    CAPABILITY_COLUMN_DESCRIPTION: _column_descriptions,
    CAPABILITY_GLOSSARY_LINK: _glossary_links,
}


async def run_steward_agent(
    session: AsyncSession,
    organization_id: UUID,
    *,
    request: TaskAgentRunRequest,
    settings: Settings,
    triggered_by: SecurityContext,
) -> TaskAgentOutcome:
    """One bounded steward run; see `task_agent.run_task_agent`."""
    return await run_task_agent(
        session,
        organization_id,
        spec=STEWARD_AGENT,
        work=STEWARD_WORK,
        request=request,
        settings=settings,
        triggered_by=triggered_by,
    )
