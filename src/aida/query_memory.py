"""AG-7: query-memory similarity + safe adaptation.

**What "memory" is here.** `QueryMemoryEvidence` (ST-05-era, already in `models.py`)
is deliberately value-free: it stores `question_hash` and `sql_hash` -- never the
question text or an executable, literal-bearing SQL string. That is not an
oversight this module works around; it is `AgentRun`'s own stated contract ("raw
user questions are intentionally not persisted") and `QueryExecution.normalized_sql`
is *redacted* before it is ever written (`sql_redaction.redact_sql_literals`,
applied in `query_gateway.py` before the row is saved -- "the executable form
never leaves the gateway"). So there is no persisted text to embed and no
literal-bearing SQL to replay, by design, and this module does not add either.

**The similarity substrate actually available.** `QueryExecution.referenced_tables`
(SQL-qualified names, no values) is real, already-persisted structural evidence of
*what a past successful query was about*. The live retrieval stage
(`retrieval.py`/`agent_orchestrator.py`) already resolves a *new* question to a set
of candidate `MetadataTable` ids before any SQL is generated. Comparing those two
table-id sets with plain Jaccard overlap is a genuine, value-free similarity signal
that needs no new persisted state -- no embedding column, no new table. It is a
coarser signal than natural-language semantic similarity, and this module does not
claim otherwise: two questions about the same tables are not necessarily the same
question. It is offered as *grounding*, never as an automatic execution path (the
module 13 spec's own words for `QueryMemoryEvidence`), which is exactly how
`agent_orchestrator.py` wires it in -- see the module docstring there.

**Version-awareness.** A candidate is invalid the moment either of two things is
true, both checked at read time, using only fields every table already has:

* `QueryMemoryEvidence.semantic_version` (copied from the `AgentRun` that produced
  it) no longer equals the semantic version `agent_orchestrator.py` resolves fresh
  for *this* run -- the same "PUBLISHED `SemanticModelVersion`, else
  technical-metadata" computation used everywhere else in this codebase for
  "has anything semantic changed" (see `GovernedAgentOrchestrator.run`).
* Any `MetadataTable` the candidate referenced has `updated_at` later than the
  candidate's own `AgentRun.updated_at` (bumped by `TimestampMixin`'s `onupdate`
  the moment that run was finalised to `COMPLETED`) -- catching a raw
  catalog/schema change that never triggered a semantic-model publish. A
  referenced table that no longer resolves at all (renamed, dropped) is treated
  the same way: unresolved is not "unchanged".

Negative feedback already suppresses reuse before this module ever runs --
`QueryMemoryEvidence.status` only reaches `ELIGIBLE` once positive feedback exists
and no negative feedback has (see `intelligence_api.py::upsert_query_feedback`);
this module still filters on `status == "ELIGIBLE"` defensively rather than
trusting the caller's query to have done it.

Every function down to `find_query_memory_match` is pure and database-free, tested
without a session in `tests/test_query_memory.py`, mirroring
`aida.quality_coupling` / `aida.tool_first_rate`'s split. `find_query_memory_match`
itself is the one place that touches a session -- the DB-facing translation from
ORM rows to the pure dataclasses above, kept thin and single-purpose the same way
`quality_coupling.resolve_table_ids` is (which this module reuses directly rather
than re-resolving table names its own way).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AgentRun, DataSource, MetadataTable, QueryExecution, QueryMemoryEvidence
from aida.quality_coupling import resolve_table_ids
from aida.timeutil import as_utc

#: `QueryMemoryEvidence.status` values that make a row eligible for reuse at all.
#: Anything else (`OBSERVED`: no feedback yet; `SUPPRESSED`: negative feedback
#: exists) is excluded before similarity or version checks even run.
ELIGIBLE_STATUS = "ELIGIBLE"


@dataclass(frozen=True, slots=True)
class MemoryCandidateFacts:
    """Everything the pure scorer needs about one `QueryMemoryEvidence` row,
    already translated out of ORM objects so the scorer never touches a session."""

    memory_evidence_id: str
    agent_run_id: str
    query_execution_id: str
    normalized_sql: str
    referenced_table_ids: frozenset[str]
    semantic_version: str | None
    status: str
    run_completed_at: datetime


@dataclass(frozen=True, slots=True)
class StaleCheck:
    """Why a candidate is (or is not) stale -- every reason inspectable, matching
    this codebase's "every factor inspectable" convention (`tool_first_rate.py`,
    `connector_health.py`, `trust_scoring.py`)."""

    is_stale: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    """The winning candidate, plus the evidence that made it eligible."""

    memory_evidence_id: str
    agent_run_id: str
    query_execution_id: str
    normalized_sql: str
    similarity: float
    semantic_version: str | None
    referenced_table_ids: tuple[str, ...] = field(default_factory=tuple)

    def evidence(self) -> dict[str, Any]:
        """Value-free: ids, a score and a count -- never SQL text or table names,
        so this is safe to fold into `AgentRun.plan_evidence` unredacted."""
        return {
            "memory_evidence_id": self.memory_evidence_id,
            "source_agent_run_id": self.agent_run_id,
            "source_query_execution_id": self.query_execution_id,
            "similarity": self.similarity,
            "semantic_version": self.semantic_version,
            "referenced_table_count": len(self.referenced_table_ids),
        }


def jaccard_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Set overlap, 0.0-1.0. Either side empty is defined as no similarity
    (not undefined/1.0) -- an empty table set carries no structural signal to
    match on, so it can never win a comparison."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return round(len(a & b) / union, 4)


def check_candidate_staleness(
    *,
    candidate_semantic_version: str | None,
    current_semantic_version: str | None,
    referenced_table_ids: frozenset[str],
    table_updated_at: dict[str, datetime],
    run_completed_at: datetime,
) -> StaleCheck:
    """Version-aware invalidation (module 13 spec §9: "Suppressed when that
    version is superseded"). Stale if the semantic version has moved on, or if
    ANY table this candidate referenced has been touched since -- a table that
    no longer resolves at all counts as touched, not as absent evidence."""
    reasons: list[str] = []
    if candidate_semantic_version != current_semantic_version:
        reasons.append("SEMANTIC_VERSION_SUPERSEDED")
    # `as_utc`: SQLite (the test backend) drops tzinfo on read-back even for a
    # `DateTime(timezone=True)` column; PostgreSQL does not. Comparing through
    # `as_utc` on both sides is this codebase's one fix for that class of bug
    # (see `aida.timeutil`'s own docstring) rather than a bare `>` here.
    completed_at = as_utc(run_completed_at)
    for table_id in sorted(referenced_table_ids):
        updated_at = table_updated_at.get(table_id)
        if updated_at is None:
            reasons.append(f"TABLE_UNRESOLVED:{table_id}")
        elif as_utc(updated_at) > completed_at:
            reasons.append(f"TABLE_VERSION_ADVANCED:{table_id}")
    return StaleCheck(is_stale=bool(reasons), reasons=tuple(reasons))


def select_best_match(
    candidates: Sequence[MemoryCandidateFacts],
    *,
    target_table_ids: frozenset[str],
    current_semantic_version: str | None,
    table_updated_at: dict[str, datetime],
    min_similarity: float,
) -> MemoryMatch | None:
    """Highest-similarity candidate that is both `ELIGIBLE` and not stale, above
    `min_similarity`. Deterministic tie-break on `agent_run_id` so this function
    has one answer for one input, which is what makes it unit-testable at all."""
    best: MemoryMatch | None = None
    for candidate in candidates:
        if candidate.status != ELIGIBLE_STATUS:
            continue
        stale = check_candidate_staleness(
            candidate_semantic_version=candidate.semantic_version,
            current_semantic_version=current_semantic_version,
            referenced_table_ids=candidate.referenced_table_ids,
            table_updated_at=table_updated_at,
            run_completed_at=candidate.run_completed_at,
        )
        if stale.is_stale:
            continue
        similarity = jaccard_similarity(target_table_ids, candidate.referenced_table_ids)
        if similarity < min_similarity:
            continue
        if (
            best is None
            or similarity > best.similarity
            or (similarity == best.similarity and candidate.agent_run_id < best.agent_run_id)
        ):
            best = MemoryMatch(
                memory_evidence_id=candidate.memory_evidence_id,
                agent_run_id=candidate.agent_run_id,
                query_execution_id=candidate.query_execution_id,
                normalized_sql=candidate.normalized_sql,
                similarity=similarity,
                semantic_version=candidate.semantic_version,
                referenced_table_ids=tuple(sorted(candidate.referenced_table_ids)),
            )
    return best


async def find_query_memory_match(
    session: AsyncSession,
    *,
    datasource: DataSource,
    current_semantic_version: str | None,
    retrieved_table_ids: frozenset[str],
    min_similarity: float,
    scan_limit: int,
) -> MemoryMatch | None:
    """The one place this module touches a session. Reads real,
    already-persisted `QueryMemoryEvidence` / `AgentRun` / `QueryExecution` /
    `MetadataTable` rows, translates them into `MemoryCandidateFacts`, and hands
    them to the pure scorer above. Never writes, and returns `None` (never a
    weaker/partial answer) the moment there is nothing to compare against.
    """
    if not retrieved_table_ids:
        return None
    rows = (
        await session.execute(
            select(QueryMemoryEvidence, AgentRun, QueryExecution)
            .join(AgentRun, AgentRun.id == QueryMemoryEvidence.agent_run_id)
            .join(QueryExecution, QueryExecution.id == QueryMemoryEvidence.query_execution_id)
            .where(
                QueryMemoryEvidence.organization_id == datasource.organization_id,
                QueryMemoryEvidence.datasource_id == datasource.id,
                QueryMemoryEvidence.status == ELIGIBLE_STATUS,
                QueryExecution.status == "COMPLETED",
                QueryExecution.normalized_sql.is_not(None),
            )
            .order_by(QueryMemoryEvidence.updated_at.desc())
            .limit(scan_limit)
        )
    ).all()
    if not rows:
        return None

    all_names: set[str] = set()
    for _memory, _run, execution in rows:
        all_names.update(execution.referenced_tables or [])
    name_to_id = await resolve_table_ids(
        session, datasource=datasource, table_names=sorted(all_names)
    )

    all_table_ids = {table_id for table_id in name_to_id.values()}
    table_updated_at: dict[str, datetime] = {}
    if all_table_ids:
        table_rows = (
            await session.execute(
                select(MetadataTable.id, MetadataTable.updated_at).where(
                    MetadataTable.id.in_(all_table_ids)
                )
            )
        ).all()
        table_updated_at = {str(table_id): updated_at for table_id, updated_at in table_rows}

    candidates: list[MemoryCandidateFacts] = []
    for memory, run, execution in rows:
        referenced_ids = frozenset(
            str(name_to_id[name])
            for name in (execution.referenced_tables or [])
            if name in name_to_id
        )
        candidates.append(
            MemoryCandidateFacts(
                memory_evidence_id=str(memory.id),
                agent_run_id=str(memory.agent_run_id),
                query_execution_id=str(memory.query_execution_id),
                normalized_sql=execution.normalized_sql or "",
                referenced_table_ids=referenced_ids,
                semantic_version=memory.semantic_version,
                status=memory.status,
                run_completed_at=run.updated_at,
            )
        )

    return select_best_match(
        candidates,
        target_table_ids=frozenset(retrieved_table_ids),
        current_semantic_version=current_semantic_version,
        table_updated_at=table_updated_at,
        min_similarity=min_similarity,
    )


def retrieved_table_ids_from_hits(hits: Sequence[Any]) -> frozenset[str]:
    """The same table-id extraction `GovernedAgentOrchestrator._model_context`
    already does from retrieval hits (TABLE hits' own `object_id`, plus any
    `table_id`/`source_table_id` a non-TABLE hit's metadata names), pulled out
    as a pure, reusable, unit-testable function rather than duplicated.
    """
    table_ids: set[str] = set()
    for hit in hits:
        if getattr(hit, "object_type", None) == "TABLE":
            table_ids.add(str(hit.object_id))
        metadata = getattr(hit, "metadata", None) or {}
        table_id = metadata.get("table_id") or metadata.get("source_table_id")
        if table_id:
            table_ids.add(str(table_id))
    return frozenset(table_ids)
