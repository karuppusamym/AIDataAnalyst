"""ADR-0029: quality rules derived from profile history, and their decision.

The quality agent (`aida.quality_agent`) proposes; this module holds the rules
it proposes by and the effect of a person's decision on what it proposed, so
the agent and the decision adapter share one definition of a rule key.

**What is proposed.** Two of DQ-4's value-free rule types, each from a table's
most recent completed profiles (at most `PROFILE_WINDOW`), and only when at
least `MINIMUM_PROFILES` of them agree:

* `TABLE_ROW_COUNT_MIN` -- a floor at `FLOOR_FRACTION` of the smallest row count
  in the window, for a table that held at least `MINIMUM_ROWS_FOR_FLOOR` rows in
  every profile of it. No profile in the window would have tripped it. The row
  count is the one `custom_quality_rules.evaluate_rule` will compare the floor
  with: the estimate when there is one, the sampled count otherwise.
* `COLUMN_NULL_RATE_MAX` -- a ceiling `NULL_RATE_HEADROOM` above the worst null
  rate in the window, for a column that never exceeded `NULL_RATE_ELIGIBLE_MAX`.
  A column that is normally complete and suddenly is not is the regression a
  ceiling exists to catch; a column that is sometimes half empty is not a
  candidate at all.

Both read only counts the profiler already stores, so no source value is read,
and none reaches a proposal, its evidence or the agent's ledger (INV-6).

**What is never proposed again.** A rule key -- (table, column, rule type) -- is
covered once any `QualityRule` exists for it, enabled or not, or any proposal
does, in any state. A rule a person disabled, or a proposal a person rejected,
is an answer; proposing it again on every run would keep asking the question
until somebody gave the other answer. Selection excludes covered keys in SQL,
so a run never spends its examination budget on them.

**The decision.** `decide_quality_rule_proposal` is this object type's
governance adapter (`governance_decision_service.TargetEffectAdapter`),
registered by `semantic_api`. Approval creates an enabled rule in the
datasource's `AGENT_RULE_PACK_NAME` pack -- created by the approver if it does
not exist yet -- so it runs on DQ-4's schedule from then on, and its incidents
gate governed tools and demote retrieval like any other rule's. That is why the
object type is T2 (`review_risk_tiers`) and no agent can approve it. The rule's
`created_by` is the approver: the person who switched the control on is the one
accountable for it, and the proposal keeps the agent as its author.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Float, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from aida.governance_decision_contracts import TargetEffect
from aida.models import (
    ColumnProfile,
    GovernanceReview,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    QualityRule,
    QualityRulePack,
    TableProfile,
)
from aida.quality_rule_proposal_model import (
    PROPOSAL_APPROVED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    QualityRuleProposal,
)
from aida.security import SecurityContext

QUALITY_RULE_PROPOSAL_OBJECT_TYPE: Final = "QUALITY_RULE_PROPOSAL"
RULE_TABLE_ROW_COUNT_MIN: Final = "TABLE_ROW_COUNT_MIN"
RULE_COLUMN_NULL_RATE_MAX: Final = "COLUMN_NULL_RATE_MAX"

#: A rule is derived from a table's most recent completed profiles, at most this
#: many -- history old enough to predate the table's current shape does not vote.
PROFILE_WINDOW: Final = 5
#: Fewer than this is a measurement, not a pattern.
MINIMUM_PROFILES: Final = 3
#: A floor under a table this small would fire on ordinary churn.
MINIMUM_ROWS_FOR_FLOOR: Final = 100
FLOOR_FRACTION: Final = 0.5
#: Only a column at most this empty in every profile gets a ceiling.
NULL_RATE_ELIGIBLE_MAX: Final = 0.05
NULL_RATE_HEADROOM: Final = 0.02
#: Where an approved proposal's rule goes: one pack per datasource.
AGENT_RULE_PACK_NAME: Final = "Agent-proposed rules"

_COMPLETED: Final = "COMPLETED"
_ACTIVE: Final = "ACTIVE"
_RULE_NAME_LIMIT: Final = 200


@dataclass(frozen=True, slots=True)
class DerivedRule:
    rule_type: str
    threshold: float
    #: How much history it rests on: `profiles_used / PROFILE_WINDOW`.
    confidence: float
    profiles_used: int
    #: Counts and rates the profiler already stores -- never a source value.
    evidence: dict[str, Any] = field(default_factory=dict)


def _confidence(profiles_used: int) -> float:
    return round(min(profiles_used, PROFILE_WINDOW) / PROFILE_WINDOW, 2)


def derive_row_count_floor(
    *, profiles_used: int, smallest: int, largest: int
) -> DerivedRule | None:
    """A floor no profile in the window would have tripped, or `None` below
    the evidence bar."""
    if profiles_used < MINIMUM_PROFILES or smallest < MINIMUM_ROWS_FOR_FLOOR:
        return None
    return DerivedRule(
        rule_type=RULE_TABLE_ROW_COUNT_MIN,
        threshold=float(math.floor(smallest * FLOOR_FRACTION)),
        confidence=_confidence(profiles_used),
        profiles_used=profiles_used,
        evidence={
            "profiles_used": profiles_used,
            "smallest_row_count": smallest,
            "largest_row_count": largest,
            "floor_fraction": FLOOR_FRACTION,
        },
    )


def derive_null_rate_ceiling(*, profiles_used: int, worst_null_rate: float) -> DerivedRule | None:
    """A ceiling just above a normally-complete column's worst null rate, or
    `None` for a column that is not normally complete."""
    if profiles_used < MINIMUM_PROFILES or worst_null_rate > NULL_RATE_ELIGIBLE_MAX:
        return None
    return DerivedRule(
        rule_type=RULE_COLUMN_NULL_RATE_MAX,
        threshold=round(min(1.0, worst_null_rate + NULL_RATE_HEADROOM), 4),
        confidence=_confidence(profiles_used),
        profiles_used=profiles_used,
        evidence={
            "profiles_used": profiles_used,
            "worst_null_rate": round(worst_null_rate, 6),
            "headroom": NULL_RATE_HEADROOM,
        },
    )


@dataclass(frozen=True, slots=True)
class RuleCandidate:
    """One uncovered rule key and the rule derived for it. Plain values only, so
    a candidate survives a rolled-back savepoint."""

    datasource_id: UUID
    table_id: UUID
    #: `schema.table`.
    table_name: str
    column_id: UUID | None
    column_name: str | None
    derived: DerivedRule

    @property
    def rule_type(self) -> str:
        return self.derived.rule_type

    @property
    def subject_id(self) -> UUID:
        return self.column_id or self.table_id

    @property
    def subject_name(self) -> str:
        if self.column_name is None:
            return self.table_name
        return f"{self.table_name}.{self.column_name}"


def _recent_profiles(organization_id: UUID, datasource_id: UUID | None) -> Subquery:
    """Each table's most recent completed profiles, at most `PROFILE_WINDOW`."""
    filters: list[ColumnElement[bool]] = [
        TableProfile.organization_id == organization_id,
        TableProfile.status == _COMPLETED,
    ]
    if datasource_id is not None:
        filters.append(TableProfile.datasource_id == datasource_id)
    ranked = (
        select(
            TableProfile.id.label("profile_id"),
            TableProfile.table_id.label("table_id"),
            # The count `custom_quality_rules.evaluate_rule` compares a floor with.
            func.coalesce(TableProfile.row_count_estimate, TableProfile.sampled_row_count).label(
                "row_count"
            ),
            func.row_number()
            .over(
                partition_by=TableProfile.table_id,
                order_by=[TableProfile.created_at.desc(), TableProfile.id.desc()],
            )
            .label("recency"),
        )
        .where(*filters)
        .subquery()
    )
    return (
        select(ranked.c.profile_id, ranked.c.table_id, ranked.c.row_count)
        .where(ranked.c.recency <= PROFILE_WINDOW)
        .subquery()
    )


def _covered(rule_type: str, table_id: Any, column_id: Any | None) -> ColumnElement[bool]:
    """Any rule or any proposal for this key, in any state. `table_id` and
    `column_id` are values or correlated columns alike."""
    rule_column = (
        QualityRule.column_id.is_(None) if column_id is None else QualityRule.column_id == column_id
    )
    proposal_column = (
        QualityRuleProposal.column_id.is_(None)
        if column_id is None
        else QualityRuleProposal.column_id == column_id
    )
    return or_(
        exists().where(
            QualityRule.table_id == table_id, QualityRule.rule_type == rule_type, rule_column
        ),
        exists().where(
            QualityRuleProposal.table_id == table_id,
            QualityRuleProposal.rule_type == rule_type,
            proposal_column,
        ),
    )


async def rule_is_covered(
    session: AsyncSession, *, table_id: UUID, column_id: UUID | None, rule_type: str
) -> bool:
    return bool(await session.scalar(select(_covered(rule_type, table_id, column_id))))


async def row_count_floor_candidates(
    session: AsyncSession, organization_id: UUID, *, datasource_id: UUID | None, limit: int
) -> list[RuleCandidate]:
    """Active tables with enough recent history and no floor rule or proposal."""
    recent = _recent_profiles(organization_id, datasource_id)
    stats = (
        select(
            recent.c.table_id.label("table_id"),
            func.count().label("profiles_used"),
            func.min(recent.c.row_count).label("smallest"),
            func.max(recent.c.row_count).label("largest"),
        )
        .group_by(recent.c.table_id)
        .having(func.count() >= MINIMUM_PROFILES)
        .having(func.min(recent.c.row_count) >= MINIMUM_ROWS_FOR_FLOOR)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                MetadataTable.id,
                MetadataTable.name,
                MetadataTable.datasource_id,
                MetadataSchema.name,
                stats.c.profiles_used,
                stats.c.smallest,
                stats.c.largest,
            )
            .select_from(MetadataTable)
            .join(stats, stats.c.table_id == MetadataTable.id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.status == _ACTIVE,
                ~_covered(RULE_TABLE_ROW_COUNT_MIN, MetadataTable.id, None),
            )
            .order_by(MetadataTable.name, MetadataTable.id)
            .limit(limit)
        )
    ).all()
    candidates: list[RuleCandidate] = []
    for table_id, table_name, table_datasource_id, schema_name, used, smallest, largest in rows:
        derived = derive_row_count_floor(
            profiles_used=int(used), smallest=int(smallest), largest=int(largest)
        )
        if derived is not None:
            candidates.append(
                RuleCandidate(
                    datasource_id=table_datasource_id,
                    table_id=table_id,
                    table_name=f"{schema_name}.{table_name}",
                    column_id=None,
                    column_name=None,
                    derived=derived,
                )
            )
    return candidates


async def null_rate_ceiling_candidates(
    session: AsyncSession, organization_id: UUID, *, datasource_id: UUID | None, limit: int
) -> list[RuleCandidate]:
    """Active columns normally complete across enough recent history, with no
    null-rate rule or proposal."""
    recent = _recent_profiles(organization_id, datasource_id)
    total = ColumnProfile.null_count + ColumnProfile.non_null_count
    null_rate = cast(ColumnProfile.null_count, Float) / cast(total, Float)
    stats = (
        select(
            recent.c.table_id.label("table_id"),
            ColumnProfile.column_id.label("column_id"),
            func.count().label("profiles_used"),
            func.max(null_rate).label("worst"),
        )
        .select_from(recent)
        .join(ColumnProfile, ColumnProfile.table_profile_id == recent.c.profile_id)
        .where(total > 0)
        .group_by(recent.c.table_id, ColumnProfile.column_id)
        .having(func.count() >= MINIMUM_PROFILES)
        .having(func.max(null_rate) <= NULL_RATE_ELIGIBLE_MAX)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                MetadataTable.id,
                MetadataTable.name,
                MetadataTable.datasource_id,
                MetadataSchema.name,
                MetadataColumn.id,
                MetadataColumn.name,
                stats.c.profiles_used,
                stats.c.worst,
            )
            .select_from(stats)
            .join(MetadataColumn, MetadataColumn.id == stats.c.column_id)
            .join(MetadataTable, MetadataTable.id == stats.c.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.status == _ACTIVE,
                MetadataColumn.table_id == MetadataTable.id,
                MetadataColumn.status == _ACTIVE,
                ~_covered(RULE_COLUMN_NULL_RATE_MAX, MetadataTable.id, MetadataColumn.id),
            )
            .order_by(MetadataTable.name, MetadataColumn.ordinal_position, MetadataColumn.id)
            .limit(limit)
        )
    ).all()
    candidates: list[RuleCandidate] = []
    for (
        table_id,
        table_name,
        table_datasource_id,
        schema_name,
        column_id,
        column_name,
        used,
        worst,
    ) in rows:
        derived = derive_null_rate_ceiling(profiles_used=int(used), worst_null_rate=float(worst))
        if derived is not None:
            candidates.append(
                RuleCandidate(
                    datasource_id=table_datasource_id,
                    table_id=table_id,
                    table_name=f"{schema_name}.{table_name}",
                    column_id=column_id,
                    column_name=column_name,
                    derived=derived,
                )
            )
    return candidates


def _rule_name(candidate: RuleCandidate) -> str:
    label = (
        "Row-count floor"
        if candidate.rule_type == RULE_TABLE_ROW_COUNT_MIN
        else "Null-rate ceiling"
    )
    return f"{label} on {candidate.subject_name}"[:_RULE_NAME_LIMIT]


def build_quality_rule_proposal(
    candidate: RuleCandidate, *, organization_id: UUID, created_by: str, agent_run: str
) -> QualityRuleProposal:
    derived = candidate.derived
    return QualityRuleProposal(
        organization_id=organization_id,
        datasource_id=candidate.datasource_id,
        table_id=candidate.table_id,
        column_id=candidate.column_id,
        rule_type=derived.rule_type,
        threshold=derived.threshold,
        name=_rule_name(candidate),
        confidence=derived.confidence,
        evidence={**derived.evidence, "agent_run": agent_run},
        status=PROPOSAL_PENDING,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


async def _agent_rule_pack(
    session: AsyncSession, proposal: QualityRuleProposal, *, approver: str
) -> QualityRulePack:
    """The datasource's pack for approved agent proposals, created on first use.

    A pack a person disabled stays disabled: the approved rule joins it and runs
    when they switch the pack back on.
    """
    lookup = select(QualityRulePack).where(
        QualityRulePack.datasource_id == proposal.datasource_id,
        QualityRulePack.name == AGENT_RULE_PACK_NAME,
    )
    pack = await session.scalar(lookup)
    if pack is not None:
        return pack
    try:
        async with session.begin_nested():
            pack = QualityRulePack(
                organization_id=proposal.organization_id,
                datasource_id=proposal.datasource_id,
                name=AGENT_RULE_PACK_NAME,
                enabled=True,
                created_by=approver,
            )
            session.add(pack)
            await session.flush()
    except IntegrityError:
        # Another approval created it first; `(datasource_id, name)` is unique.
        pack = await session.scalar(lookup)
        if pack is None:
            raise
    return pack


async def apply_quality_rule_proposal(
    session: AsyncSession, proposal: QualityRuleProposal, *, approver: str, now: datetime
) -> QualityRule:
    table = await session.get(MetadataTable, proposal.table_id)
    if table is None or table.status != _ACTIVE or table.datasource_id != proposal.datasource_id:
        raise HTTPException(
            status_code=409, detail="the proposed rule's table is no longer active"
        )
    if proposal.column_id is not None:
        column = await session.get(MetadataColumn, proposal.column_id)
        if column is None or column.table_id != proposal.table_id:
            raise HTTPException(
                status_code=409, detail="the proposed rule's column is no longer in its table"
            )
    pack = await _agent_rule_pack(session, proposal, approver=approver)
    rule = QualityRule(
        organization_id=proposal.organization_id,
        rule_pack_id=pack.id,
        table_id=proposal.table_id,
        column_id=proposal.column_id,
        name=proposal.name,
        rule_type=proposal.rule_type,
        threshold=proposal.threshold,
        enabled=True,
        created_by=approver,
    )
    session.add(rule)
    await session.flush()
    proposal.status = PROPOSAL_APPROVED
    proposal.reviewed_by = approver
    proposal.reviewed_at = now
    proposal.applied_rule_id = rule.id
    return rule


def reject_quality_rule_proposal(
    proposal: QualityRuleProposal, *, reviewer: str, now: datetime
) -> None:
    proposal.status = PROPOSAL_REJECTED
    proposal.reviewed_by = reviewer
    proposal.reviewed_at = now


async def decide_quality_rule_proposal(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> TargetEffect:
    """The governance adapter for `QUALITY_RULE_PROPOSAL`.

    Handed a review already claimed, like every adapter; a proposal that is
    gone, another organization's, or no longer pending is a 409 and the claim
    unwinds with it.
    """
    try:
        proposal_id = UUID(review.object_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="review target is unavailable") from None
    proposal = await session.get(QualityRuleProposal, proposal_id)
    if proposal is None or proposal.organization_id != review.organization_id:
        raise HTTPException(status_code=409, detail="review target is unavailable")
    if proposal.status != PROPOSAL_PENDING:
        raise HTTPException(
            status_code=409, detail="quality rule proposal is no longer pending review"
        )
    rule_id: UUID | None = None
    if decision == "APPROVE":
        rule = await apply_quality_rule_proposal(
            session, proposal, approver=context.principal_id, now=now
        )
        rule_id = rule.id
        event_type = "data_quality.rule_proposal.approved.v1"
    else:
        reject_quality_rule_proposal(proposal, reviewer=context.principal_id, now=now)
        event_type = "data_quality.rule_proposal.rejected.v1"
    return TargetEffect(
        event_type,
        "quality_rule_proposal",
        str(proposal.id),
        {
            "proposal_id": str(proposal.id),
            "review_id": str(review.id),
            "table_id": str(proposal.table_id),
            "column_id": str(proposal.column_id) if proposal.column_id else None,
            "rule_type": proposal.rule_type,
            "threshold": proposal.threshold,
            "rule_id": str(rule_id) if rule_id else None,
        },
    )
