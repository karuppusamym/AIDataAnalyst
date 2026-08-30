"""Usage-derived eval question suite: a change-set regression gate (ST-A8).

Atlan's Context Engineering Studio mines BI dashboards and SQL query logs into
"hundreds of questions your AI agent needs to answer correctly" and gates
deployment on them (`Docs/20-modules/18-studio.md` SS8). Free-text LLM grading
of raw questions/answers is not this codebase's shape (ADR-0014: no raw
production values in the control plane), so this is the deterministic
equivalent: mine *which governed metric or tool* real usage exercised --
never the query text or the answer -- from two existing, already value-free
edge sources:

  * `aida.consumption_lineage` (`ConsumptionRecord` rows for
    `resource_type="governed_tool_version"`) -- an MCP/API consumer actually
    invoked this governed tool.
  * `aida.bi_lineage` (`BiReportMetricEdge` / `BiMetricColumnEdge`, persisted
    by `bi_api.py`) -- a BI dashboard/report is bound to a field that resolves,
    via the same physical table+column a governed metric is defined on, to
    that metric. A dashboard depending on a metric is a routinely-asked
    question about it.

Each distinct object gets exactly one `StudioEvalQuestion` (`object_type`,
`object_id`), referencing a single evidence edge by id -- never the edge's
raw content. Mining is idempotent: an object that already has a question is
left alone, so re-running the scan on a schedule or by hand never duplicates.

The regression check (`check_eval_regressions`) reuses the existing test
harness's validators (`aida.studio_test_harness.run_test`) unchanged: for
every change-set item whose object has a mined question, run the same
compilation/shape check the item's own test gate already runs, but frame a
failure as "a question this object used to answer now fails" rather than
"this edit is malformed." `studio_api.py` wires the result into the existing
test gate (`run_tests`) and submission check (`submit_change_set`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    BiMetricColumnEdge,
    BiReportMetricEdge,
    ConsumptionRecord,
    GovernedTool,
    GovernedToolVersion,
    SemanticMetricVersion,
    StudioEvalQuestion,
)
from aida.studio import ChangeItem, TestResult
from aida.studio_test_harness import run_test

EvalObjectType = Literal["METRIC", "TOOL"]
EvalEvidenceSource = Literal["CONSUMPTION", "BI"]

# Bound the scan (coding standard S5: every multi-row operation takes a bound
# and reports truncation rather than silently returning a partial result as if
# it were complete).
DEFAULT_MINING_SCAN_LIMIT = 500


@dataclass(frozen=True, slots=True)
class MinedCandidate:
    """One (object, evidence) pair a mining pass wants persisted as a question."""

    object_type: EvalObjectType
    object_id: str
    evidence_source: EvalEvidenceSource
    evidence_edge_id: str
    label: str


@dataclass(frozen=True, slots=True)
class MiningResult:
    """Bounded outcome of one mining pass -- what was scanned and created."""

    consumption_edges_scanned: int
    bi_edges_scanned: int
    questions_created: int
    questions_already_mined: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class EvalRegressionCheck:
    """One mined question re-checked against a change set's proposed edit."""

    question: StudioEvalQuestion
    result: TestResult


async def _mine_tool_candidates(
    session: AsyncSession,
    *,
    organization_id: UUID,
    scan_limit: int,
) -> tuple[list[MinedCandidate], int]:
    """Recent tool invocations -> one candidate per distinct `GovernedTool`.

    `ConsumptionRecord.resource_id` for `resource_type="governed_tool_version"`
    names a specific *version*; the candidate targets the stable parent tool
    id, matching the identity a Studio TOOL change item is authored against.
    """
    rows = (
        await session.scalars(
            select(ConsumptionRecord)
            .where(
                ConsumptionRecord.organization_id == organization_id,
                ConsumptionRecord.resource_type == "governed_tool_version",
            )
            .order_by(ConsumptionRecord.consumed_at.desc())
            .limit(scan_limit)
        )
    ).all()
    scanned = len(rows)
    if not rows:
        return [], scanned

    edge_by_version_id: dict[UUID, ConsumptionRecord] = {}
    for row in rows:
        try:
            version_id = UUID(row.resource_id)
        except ValueError:
            continue
        edge_by_version_id.setdefault(version_id, row)
    if not edge_by_version_id:
        return [], scanned

    versions = (
        await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.id.in_(edge_by_version_id.keys()),
                GovernedToolVersion.organization_id == organization_id,
            )
        )
    ).all()
    if not versions:
        return [], scanned

    tool_ids = {v.tool_id for v in versions}
    tools_by_id = {
        t.id: t
        for t in (
            await session.scalars(
                select(GovernedTool).where(
                    GovernedTool.id.in_(tool_ids),
                    GovernedTool.organization_id == organization_id,
                )
            )
        ).all()
    }

    candidates: list[MinedCandidate] = []
    seen_tool_ids: set[UUID] = set()
    for version in versions:
        if version.tool_id in seen_tool_ids:
            continue
        tool = tools_by_id.get(version.tool_id)
        edge = edge_by_version_id.get(version.id)
        if tool is None or edge is None:
            continue
        seen_tool_ids.add(version.tool_id)
        candidates.append(
            MinedCandidate(
                object_type="TOOL",
                object_id=str(tool.id),
                evidence_source="CONSUMPTION",
                evidence_edge_id=str(edge.id),
                label=f"tool:{tool.slug}",
            )
        )
    return candidates, scanned


async def _mine_metric_candidates(
    session: AsyncSession,
    *,
    organization_id: UUID,
    scan_limit: int,
) -> tuple[list[MinedCandidate], int]:
    """Recent BI report->metric edges -> one candidate per governed metric a
    dashboard's field resolves to, via the physical table+column both share.

    A BI field's own metric identity (`BiMetricNode`) is vendor-internal --
    nothing in this codebase binds it to a governed `SemanticMetric` directly.
    `bi_api.py` already resolves a BI field's *source column* to a catalog
    `metadata_table`/`metadata_column` at import time
    (`BiMetricColumnEdge.matched_table_id`/`matched_column_id`); reusing that
    match against `SemanticMetricVersion.source_table_id`/`measure_column_id`
    is the same physical-column identity a metric is itself defined on, so it
    is the deterministic, value-free way to say "this dashboard depends on
    this governed metric."
    """
    edges = (
        await session.scalars(
            select(BiReportMetricEdge)
            .where(BiReportMetricEdge.organization_id == organization_id)
            .order_by(BiReportMetricEdge.created_at.desc())
            .limit(scan_limit)
        )
    ).all()
    scanned = len(edges)
    if not edges:
        return [], scanned

    bi_metric_ids = {e.metric_id for e in edges}
    column_edges = (
        await session.scalars(
            select(BiMetricColumnEdge).where(
                BiMetricColumnEdge.organization_id == organization_id,
                BiMetricColumnEdge.metric_id.in_(bi_metric_ids),
                BiMetricColumnEdge.matched_table_id.isnot(None),
                BiMetricColumnEdge.matched_column_id.isnot(None),
            )
        )
    ).all()
    if not column_edges:
        return [], scanned

    pairs_by_bi_metric: dict[UUID, list[tuple[UUID, UUID]]] = {}
    table_ids: set[UUID] = set()
    column_ids: set[UUID] = set()
    for column_edge in column_edges:
        table_id = column_edge.matched_table_id
        column_id = column_edge.matched_column_id
        if table_id is None or column_id is None:
            continue
        pairs_by_bi_metric.setdefault(column_edge.metric_id, []).append((table_id, column_id))
        table_ids.add(table_id)
        column_ids.add(column_id)

    metric_versions = (
        await session.scalars(
            select(SemanticMetricVersion).where(
                SemanticMetricVersion.organization_id == organization_id,
                SemanticMetricVersion.status == "PUBLISHED",
                SemanticMetricVersion.source_table_id.in_(table_ids),
                SemanticMetricVersion.measure_column_id.in_(column_ids),
            )
        )
    ).all()
    metric_by_pair = {
        (mv.source_table_id, mv.measure_column_id): mv
        for mv in metric_versions
        if mv.measure_column_id is not None
    }

    candidates: list[MinedCandidate] = []
    seen_metric_ids: set[UUID] = set()
    for edge in edges:
        for pair in pairs_by_bi_metric.get(edge.metric_id, []):
            metric_version = metric_by_pair.get(pair)
            if metric_version is None or metric_version.metric_id in seen_metric_ids:
                continue
            seen_metric_ids.add(metric_version.metric_id)
            candidates.append(
                MinedCandidate(
                    object_type="METRIC",
                    object_id=str(metric_version.metric_id),
                    evidence_source="BI",
                    evidence_edge_id=str(edge.id),
                    label=f"metric:{metric_version.name}",
                )
            )
            break
    return candidates, scanned


async def mine_eval_questions(
    session: AsyncSession,
    *,
    organization_id: UUID,
    scan_limit: int = DEFAULT_MINING_SCAN_LIMIT,
) -> MiningResult:
    """Scan recent consumption and BI lineage edges; persist one
    `StudioEvalQuestion` per distinct governed metric/tool they touch.

    Idempotent -- dedups against already-mined objects, so this is safe to
    call repeatedly (by hand or on a future schedule) without ever
    duplicating a question. Does not flush or commit; the caller owns the
    unit of work, matching every other write path in this module.
    """
    tool_candidates, consumption_scanned = await _mine_tool_candidates(
        session, organization_id=organization_id, scan_limit=scan_limit
    )
    metric_candidates, bi_scanned = await _mine_metric_candidates(
        session, organization_id=organization_id, scan_limit=scan_limit
    )
    candidates = [*tool_candidates, *metric_candidates]

    existing_keys: set[tuple[str, str]] = set()
    if candidates:
        existing_rows = await session.execute(
            select(StudioEvalQuestion.object_type, StudioEvalQuestion.object_id).where(
                StudioEvalQuestion.organization_id == organization_id
            )
        )
        existing_keys = {(row.object_type, row.object_id) for row in existing_rows}

    created = 0
    for candidate in candidates:
        key = (candidate.object_type, candidate.object_id)
        if key in existing_keys:
            continue
        session.add(
            StudioEvalQuestion(
                organization_id=organization_id,
                object_type=candidate.object_type,
                object_id=candidate.object_id,
                evidence_source=candidate.evidence_source,
                evidence_edge_id=candidate.evidence_edge_id,
                label=candidate.label,
            )
        )
        existing_keys.add(key)
        created += 1

    return MiningResult(
        consumption_edges_scanned=consumption_scanned,
        bi_edges_scanned=bi_scanned,
        questions_created=created,
        questions_already_mined=len(candidates) - created,
        truncated=consumption_scanned >= scan_limit or bi_scanned >= scan_limit,
    )


async def load_eval_questions_for_objects(
    session: AsyncSession,
    *,
    organization_id: UUID,
    object_keys: set[tuple[str, str]],
) -> list[StudioEvalQuestion]:
    """Load mined questions for the given `(object_type, object_id)` pairs."""
    if not object_keys:
        return []
    object_types = {k[0] for k in object_keys}
    object_ids = {k[1] for k in object_keys}
    rows = (
        await session.scalars(
            select(StudioEvalQuestion).where(
                StudioEvalQuestion.organization_id == organization_id,
                StudioEvalQuestion.object_type.in_(object_types),
                StudioEvalQuestion.object_id.in_(object_ids),
            )
        )
    ).all()
    return [q for q in rows if (q.object_type, q.object_id) in object_keys]


def check_eval_regressions(
    change_items: list[ChangeItem],
    questions: list[StudioEvalQuestion],
) -> list[EvalRegressionCheck]:
    """Re-run the harness validator for every mined question this change set
    touches -- the regression gate itself.

    Only questions whose `(object_type, object_id)` matches a change item in
    this change set are checked: nothing changed for an untouched object, so
    there is nothing to regress. `run_test` is the exact validator the item's
    own test-gate check already runs (`_validate_metric_item` /
    `_validate_tool_item`); what is new is *why* a failure here matters -- the
    question's own existence is proof this object's current shape used to
    resolve for a real consumer, so a failure now is a regression, not merely
    an author's edit that has not passed review yet.
    """
    items_by_object: dict[tuple[str, str], ChangeItem] = {
        (item.object_type, item.object_id): item for item in change_items
    }
    checks: list[EvalRegressionCheck] = []
    for question in questions:
        item = items_by_object.get((question.object_type, question.object_id))
        if item is None:
            continue
        checks.append(EvalRegressionCheck(question=question, result=run_test(item)))
    return checks
