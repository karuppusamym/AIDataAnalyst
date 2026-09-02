"""Group K / AT-12: semantic mining of warehouse query history.

Extends AT-5 from a ranking worklist to a meaning source (tracker AT-12):
mines *structure* -- which tables joined on which columns, which columns a
query grouped by, which columns it filtered on -- out of a warehouse's own
query log, never the data values a query touched. Per INV-6/AT-C3's
value-free constraint (already established for control-plane state
elsewhere on this platform -- `test_inv6_value_freedom.py`), the SQL text a
`WarehouseQueryLogEntry` carries is parsed only for its shape: every literal
node `sqlglot` produces is walked past and never read, so no filter *value*,
customer id, or amount from the warehouse's real data can reach a candidate
or its evidence. Mirrors `sql_lineage_parser.py`'s own posture (parse-only,
literals redacted/ignored, graceful degradation to "no structure extracted"
on anything unparseable, never a guess).

Per `Docs/review-2026-08/atlan-context/00-decisions.md` §4, this is
*explicitly not a lineage source*: a query-log-derived join is a candidate
into the existing maker-checker review queue and never an asserted edge.
Two shapes come out of the log:

* **Join candidates** -- reuse `RelationshipCandidate` unmodified (a new
  `detection_rule`, `QUERY_LOG_JOIN_V1`, is the only new vocabulary). This is
  the same queue `discover_relationship_candidates` (RL-2) already lands
  same-source candidates in and the same queue's own decision endpoint
  (`decide_relationship_candidate`) already reviews -- no new schema, no new
  route.
* **Metric candidates** -- `QueryHistoryMetricCandidate` (`models.py`, Group
  K addition): an aggregation over a measure column, grouped by a grain
  column set, seen repeatedly. `SemanticMetricProposal` (SM-4) cannot
  represent this -- it requires a `source_annotation_id` pointing at an
  approved business annotation and carries NL-description-quality scores
  that do not apply to a candidate whose only evidence is recurrence in the
  query log -- so this is the "clearly-scoped new candidate type" AT-12
  anticipates. Lands in the unified `GovernanceReview` queue
  (`QUERY_HISTORY_METRIC_CANDIDATE`), dispatched from
  `semantic_api._apply_governance_review_decision` the same way AT-11's
  `COLUMN_CLASSIFICATION_PROMOTION` is.

AT-C2 lane 3 (model judgements are proposal-only under a 0.70 confidence
cap, maker != checker) governs every candidate this module creates:
`QUERY_HISTORY_CONFIDENCE_CAP` is enforced in `_lane3_confidence`, and
nothing here ever sets a candidate to an approved/authoritative state --
only an independent reviewer's APPROVE can, through the existing queues.

Output is bounded on both axes that make a naive mine-everything pass
produce unbounded noise from a large query log: `QUERY_HISTORY_MIN_OCCURRENCES`
drops anything seen only once or twice (not a pattern, a fluke), and
`QUERY_HISTORY_MAX_JOIN_CANDIDATES` / `QUERY_HISTORY_MAX_METRIC_CANDIDATES`
cap how many of the highest-occurrence survivors are actually landed,
ranked by occurrence count descending -- the same "rank, then cap" shape
`generate_composite_relationship_candidates` (RL-3) already uses.

Honest scope limit: no connector implements `get_query_history()` yet (AT-D3
-- both Snowflake and Databricks still report `query_history=False`, and
this module does not flip either flag; it takes already-materialized
`WarehouseQueryLogEntry` rows as input, connector-agnostic, so a connector
that later implements real query-history extraction only has to produce
this shape). See the AT-12 tracker row for what "certified" would require
before that flag flips.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.models import (
    DataSource,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
    QueryHistoryMetricCandidate,
    RelationshipCandidate,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.negative_knowledge import compute_predicate_hash
from aida.relationship_candidate_review import (
    load_suppressed_relationship_predicate_hashes,
    relationship_candidate_negative_predicate,
)
from aida.security import SecurityContext

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    _SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover -- sqlglot is a pinned direct dependency
    _SQLGLOT_AVAILABLE = False

QUERY_HISTORY_MINER_VERSION = "query-history-miner-v1"

#: AT-C2 lane 3: model judgements are proposal-only, capped at 0.70
#: confidence -- a query-log-mined candidate is inferred from usage patterns,
#: never a declared constraint or view DDL (those are lanes 1/2), so it can
#: never reach the trust a corroborated candidate can.
QUERY_HISTORY_CONFIDENCE_CAP = 0.70
_CONFIDENCE_FLOOR = 0.35
#: Occurrence count at which `_lane3_confidence` saturates at the cap --
#: deliberately small (an analytics query log repeats the same shape
#: constantly) so the cap is actually reachable, not aspirational.
_CONFIDENCE_SATURATION_OCCURRENCES = 20

#: A shape seen fewer than this many times across the log is noise, not a
#: pattern -- dropped before ranking, not merely ranked low.
QUERY_HISTORY_MIN_OCCURRENCES = 3
#: Bounded output, per datasource per mining run -- see module docstring.
QUERY_HISTORY_MAX_JOIN_CANDIDATES = 50
QUERY_HISTORY_MAX_METRIC_CANDIDATES = 25

_AGGREGATE_EXPRESSION_TYPES: dict[type[Any], str] = {}
if _SQLGLOT_AVAILABLE:
    _AGGREGATE_EXPRESSION_TYPES = {
        exp.Sum: "SUM",
        exp.Avg: "AVG",
        exp.Max: "MAX",
        exp.Min: "MIN",
        exp.Count: "COUNT",
    }


# ---------------------------------------------------------------------------
# 1. Value-free structure extraction (pure -- no database, no session)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WarehouseQueryLogEntry:
    """One row of a warehouse's query history, as a connector would surface
    it. `sql_text` is parsed for structure only -- see the module docstring.
    """

    query_id: str
    sql_text: str
    executed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ColumnRef:
    table: str
    column: str


@dataclass(frozen=True, slots=True)
class JoinColumnPair:
    left_table: str
    left_column: str
    right_table: str
    right_column: str

    def normalized(self) -> JoinColumnPair:
        """Order-independent identity: `a.x = b.y` and `b.y = a.x` are the
        same join pair. Ties broken lexically so the choice is deterministic.
        """
        left = (self.left_table, self.left_column)
        right = (self.right_table, self.right_column)
        if left <= right:
            return self
        return JoinColumnPair(
            self.right_table, self.right_column, self.left_table, self.left_column
        )


@dataclass(frozen=True, slots=True)
class AggregateMeasure:
    aggregation: str
    table: str
    column: str


@dataclass(frozen=True, slots=True)
class QueryStructure:
    """The value-free shape of one query: never a literal, never a row value."""

    query_id: str
    referenced_tables: frozenset[str]
    join_pairs: tuple[JoinColumnPair, ...] = ()
    group_by_columns: tuple[ColumnRef, ...] = ()
    aggregate_measures: tuple[AggregateMeasure, ...] = ()
    filter_columns: tuple[ColumnRef, ...] = ()


def _collect_table_aliases(select_stmt: exp.Select) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table_expr in select_stmt.find_all(exp.Table):
        name = table_expr.name
        if not name:
            continue
        table_name = name.lower()
        aliases[table_name] = table_name
        alias = table_expr.alias
        if alias:
            aliases[alias.lower()] = table_name
    return aliases


def _resolve_column_table(column: exp.Column, aliases: dict[str, str]) -> str | None:
    table_token = column.table
    if table_token:
        return aliases.get(table_token.lower())
    if len(aliases) == 1:
        return next(iter(aliases.values()))
    return None


def extract_query_structure(
    entry: WarehouseQueryLogEntry, *, dialect: str = "postgres"
) -> QueryStructure | None:
    """Parse one query log entry into its value-free structure, or `None`.

    Never raises: an unparseable or unsupported statement degrades to `None`
    (dropped by the caller) rather than a guessed or partial structure,
    mirroring `sql_lineage_parser.parse_view_lineage`'s own posture. Every
    walk below reads `exp.Column`/`exp.Table` nodes only -- `exp.Literal`
    nodes (the actual filtered *values*) are never inspected or copied into
    the returned structure.
    """
    if not _SQLGLOT_AVAILABLE:
        return None
    try:
        parsed = sqlglot.parse_one(entry.sql_text, read=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return None
    select_stmt = parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    if select_stmt is None:
        return None

    aliases = _collect_table_aliases(select_stmt)
    if not aliases:
        return None
    referenced_tables = frozenset(aliases.values())

    join_pairs: list[JoinColumnPair] = []
    for join in select_stmt.find_all(exp.Join):
        on_clause = join.args.get("on")
        if on_clause is None:
            continue
        for equality in on_clause.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            left_table = _resolve_column_table(left, aliases)
            right_table = _resolve_column_table(right, aliases)
            if not left_table or not right_table or left_table == right_table:
                continue
            join_pairs.append(
                JoinColumnPair(
                    left_table, left.name.lower(), right_table, right.name.lower()
                ).normalized()
            )

    group_by_columns: list[ColumnRef] = []
    group_clause = select_stmt.args.get("group")
    if group_clause is not None:
        for item in group_clause.expressions:
            if isinstance(item, exp.Column):
                table = _resolve_column_table(item, aliases)
                if table:
                    group_by_columns.append(ColumnRef(table, item.name.lower()))

    aggregate_measures: list[AggregateMeasure] = []
    for select_expr in select_stmt.expressions:
        node = select_expr.this if isinstance(select_expr, exp.Alias) else select_expr
        agg_name = _AGGREGATE_EXPRESSION_TYPES.get(type(node))
        if agg_name is None:
            continue
        inner = node.this
        if isinstance(inner, exp.Column):
            table = _resolve_column_table(inner, aliases)
            if table:
                aggregate_measures.append(AggregateMeasure(agg_name, table, inner.name.lower()))

    filter_columns: list[ColumnRef] = []
    where_clause = select_stmt.args.get("where")
    if where_clause is not None:
        for column in where_clause.find_all(exp.Column):
            table = _resolve_column_table(column, aliases)
            if table:
                filter_columns.append(ColumnRef(table, column.name.lower()))

    return QueryStructure(
        query_id=entry.query_id,
        referenced_tables=referenced_tables,
        join_pairs=tuple(dict.fromkeys(join_pairs)),
        group_by_columns=tuple(dict.fromkeys(group_by_columns)),
        aggregate_measures=tuple(dict.fromkeys(aggregate_measures)),
        filter_columns=tuple(dict.fromkeys(filter_columns)),
    )


# ---------------------------------------------------------------------------
# 2. Frequency mining: aggregate, rank, bound (pure)
# ---------------------------------------------------------------------------


def _lane3_confidence(occurrence_count: int) -> float:
    """AT-C2 lane 3: capped at `QUERY_HISTORY_CONFIDENCE_CAP`, saturating
    well before every query in a large corpus would need to agree.
    """
    progress = min(occurrence_count / _CONFIDENCE_SATURATION_OCCURRENCES, 1.0)
    value = _CONFIDENCE_FLOOR + progress * (QUERY_HISTORY_CONFIDENCE_CAP - _CONFIDENCE_FLOOR)
    return round(min(QUERY_HISTORY_CONFIDENCE_CAP, value), 4)


@dataclass(frozen=True, slots=True)
class JoinPairFrequency:
    pair: JoinColumnPair
    occurrence_count: int
    sample_query_ids: tuple[str, ...]
    confidence: float


def mine_join_pair_frequencies(
    structures: Sequence[QueryStructure],
    *,
    min_occurrences: int = QUERY_HISTORY_MIN_OCCURRENCES,
    max_candidates: int = QUERY_HISTORY_MAX_JOIN_CANDIDATES,
) -> list[JoinPairFrequency]:
    """Every join pair seen at least `min_occurrences` times, ranked by
    occurrence count descending and capped at `max_candidates` -- the same
    rank-then-cap shape `generate_composite_relationship_candidates` uses.
    """
    counts: Counter[JoinColumnPair] = Counter()
    samples: dict[JoinColumnPair, list[str]] = {}
    for structure in structures:
        for pair in set(structure.join_pairs):
            counts[pair] += 1
            samples.setdefault(pair, []).append(structure.query_id)
    survivors = [
        JoinPairFrequency(
            pair=pair,
            occurrence_count=count,
            sample_query_ids=tuple(samples[pair][:5]),
            confidence=_lane3_confidence(count),
        )
        for pair, count in counts.items()
        if count >= min_occurrences
    ]
    survivors.sort(key=lambda item: (-item.occurrence_count, item.pair.left_table))
    return survivors[:max_candidates]


@dataclass(frozen=True, slots=True)
class MetricShapeFrequency:
    measure: AggregateMeasure
    grain: tuple[ColumnRef, ...]
    occurrence_count: int
    sample_query_ids: tuple[str, ...]
    confidence: float

    @property
    def fingerprint(self) -> str:
        return grain_fingerprint(self.grain)


def grain_fingerprint(grain: Sequence[ColumnRef]) -> str:
    """Stable identity for an (unordered) grain column set."""
    material = json.dumps(
        sorted(f"{ref.table}.{ref.column}" for ref in grain),
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def mine_metric_shape_frequencies(
    structures: Sequence[QueryStructure],
    *,
    min_occurrences: int = QUERY_HISTORY_MIN_OCCURRENCES,
    max_candidates: int = QUERY_HISTORY_MAX_METRIC_CANDIDATES,
) -> list[MetricShapeFrequency]:
    """Every (aggregation, measure column, grain) shape seen at least
    `min_occurrences` times. A query's `group_by_columns` is taken as the
    grain for every aggregate measure the same query selects -- the common
    analytics shape of one aggregate per grouped query; a query with several
    aggregates over the same grain contributes one shape per measure.
    """
    counts: Counter[tuple[AggregateMeasure, tuple[ColumnRef, ...]]] = Counter()
    samples: dict[tuple[AggregateMeasure, tuple[ColumnRef, ...]], list[str]] = {}
    for structure in structures:
        if not structure.aggregate_measures:
            continue
        grain = tuple(sorted(structure.group_by_columns, key=lambda ref: (ref.table, ref.column)))
        for measure in set(structure.aggregate_measures):
            key = (measure, grain)
            counts[key] += 1
            samples.setdefault(key, []).append(structure.query_id)
    survivors = [
        MetricShapeFrequency(
            measure=measure,
            grain=grain,
            occurrence_count=count,
            sample_query_ids=tuple(samples[(measure, grain)][:5]),
            confidence=_lane3_confidence(count),
        )
        for (measure, grain), count in counts.items()
        if count >= min_occurrences
    ]
    survivors.sort(key=lambda item: (-item.occurrence_count, item.measure.table))
    return survivors[:max_candidates]


# ---------------------------------------------------------------------------
# 3. Catalog resolution + landing candidates in the review queue (DB-facing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CatalogTable:
    table_id: UUID
    columns: dict[str, UUID] = field(default_factory=dict)


async def _load_catalog_index(
    session: AsyncSession, datasource_id: UUID
) -> dict[str, _CatalogTable]:
    """`table_name.lower()` -> real catalog ids, scoped to one datasource.

    Query-log table/column references are resolved against this index --
    anything the log names that the catalog does not recognise (a dropped
    table, a typo, cross-database SQL) is silently unresolvable and drops
    the candidate rather than guessing at an identity.
    """
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataTable.name, MetadataColumn.id, MetadataColumn.name)
            .join(MetadataColumn, MetadataColumn.table_id == MetadataTable.id)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    index: dict[str, _CatalogTable] = {}
    for table_id, table_name, column_id, column_name in rows:
        entry = index.setdefault(table_name.lower(), _CatalogTable(table_id=table_id))
        entry.columns[column_name.lower()] = column_id
    return index


@dataclass(frozen=True, slots=True)
class QueryHistoryMiningResult:
    """What one mining run produced -- every candidate landed, plus honest
    counts of what was mined but never resolved or landed, so a caller can
    tell "nothing in the log matched this shape" apart from "the catalog
    doesn't have these tables".
    """

    relationship_candidates: tuple[RelationshipCandidate, ...]
    metric_candidates: tuple[QueryHistoryMetricCandidate, ...]
    join_shapes_mined: int
    metric_shapes_mined: int
    join_candidates_unresolved: int
    metric_candidates_unresolved: int
    join_candidates_suppressed: int


async def mine_and_land_query_history_candidates(
    session: AsyncSession,
    context: SecurityContext,
    *,
    datasource: DataSource,
    project_id: UUID,
    entries: Sequence[WarehouseQueryLogEntry],
    correlation_id: str,
    dialect: str = "postgres",
) -> QueryHistoryMiningResult:
    """The end-to-end mining pass: parse -> aggregate -> resolve -> land.

    Every candidate this creates starts `PENDING` (join) or opens a `PENDING`
    `GovernanceReview` (metric) -- nothing here is ever auto-authoritative.
    One `record_audit` for the whole run (mirrors
    `discover_relationship_candidates`'s own one-audit-per-scan convention),
    not one per candidate.
    """
    structures = [
        structure
        for structure in (extract_query_structure(entry, dialect=dialect) for entry in entries)
        if structure is not None
    ]
    catalog = await _load_catalog_index(session, datasource.id)

    join_frequencies = mine_join_pair_frequencies(structures)
    existing_pairs = {
        (source_id, target_id)
        for source_id, target_id in (
            await session.execute(
                select(
                    RelationshipCandidate.source_column_id, RelationshipCandidate.target_column_id
                ).where(RelationshipCandidate.datasource_id == datasource.id)
            )
        ).all()
    }
    suppressed_hashes = await load_suppressed_relationship_predicate_hashes(
        session, datasource.organization_id
    )

    relationship_candidates: list[RelationshipCandidate] = []
    join_unresolved = 0
    join_suppressed = 0
    for frequency in join_frequencies:
        pair = frequency.pair
        left = catalog.get(pair.left_table)
        right = catalog.get(pair.right_table)
        left_column = left.columns.get(pair.left_column) if left else None
        right_column = right.columns.get(pair.right_column) if right else None
        if left is None or right is None or left_column is None or right_column is None:
            join_unresolved += 1
            continue
        forward = (left_column, right_column)
        reverse = (right_column, left_column)
        if forward in existing_pairs or reverse in existing_pairs:
            continue
        suppressed = compute_predicate_hash(
            relationship_candidate_negative_predicate(left_column, right_column)
        ) in suppressed_hashes or compute_predicate_hash(
            relationship_candidate_negative_predicate(right_column, left_column)
        ) in suppressed_hashes
        if suppressed:
            join_suppressed += 1
            continue
        candidate = RelationshipCandidate(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            target_datasource_id=datasource.id,
            source_table_id=left.table_id,
            source_column_id=left_column,
            target_table_id=right.table_id,
            target_column_id=right_column,
            detection_rule="QUERY_LOG_JOIN_V1",
            confidence=frequency.confidence,
            evidence={
                "algorithm_version": QUERY_HISTORY_MINER_VERSION,
                "occurrence_count": frequency.occurrence_count,
                "sample_query_ids": list(frequency.sample_query_ids),
                "value_free": True,
                "source_values_inspected": False,
                "confidence_cap": QUERY_HISTORY_CONFIDENCE_CAP,
            },
            created_by=context.principal_id,
        )
        session.add(candidate)
        relationship_candidates.append(candidate)
        existing_pairs.add(forward)

    metric_frequencies = mine_metric_shape_frequencies(structures)
    existing_metric_shapes = {
        (row.table_id, row.measure_column_id, row.aggregation, row.grain_fingerprint)
        for row in (
            await session.scalars(
                select(QueryHistoryMetricCandidate).where(
                    QueryHistoryMetricCandidate.datasource_id == datasource.id
                )
            )
        ).all()
    }

    metric_candidates: list[QueryHistoryMetricCandidate] = []
    metric_unresolved = 0
    for metric_frequency in metric_frequencies:
        measure = metric_frequency.measure
        table = catalog.get(measure.table)
        measure_column_id = table.columns.get(measure.column) if table else None
        grain_ids: list[UUID] = []
        grain_resolved = True
        for ref in metric_frequency.grain:
            grain_table = catalog.get(ref.table)
            grain_column_id = grain_table.columns.get(ref.column) if grain_table else None
            if grain_column_id is None:
                grain_resolved = False
                break
            grain_ids.append(grain_column_id)
        if table is None or measure_column_id is None or not grain_resolved:
            metric_unresolved += 1
            continue
        fingerprint = metric_frequency.fingerprint
        shape_key = (table.table_id, measure_column_id, measure.aggregation, fingerprint)
        if shape_key in existing_metric_shapes:
            continue
        metric_candidate = QueryHistoryMetricCandidate(
            organization_id=datasource.organization_id,
            project_id=project_id,
            datasource_id=datasource.id,
            table_id=table.table_id,
            measure_column_id=measure_column_id,
            aggregation=measure.aggregation,
            grain_column_ids=[str(column_id) for column_id in grain_ids],
            grain_fingerprint=fingerprint,
            detection_rule="QUERY_LOG_METRIC_V1",
            occurrence_count=metric_frequency.occurrence_count,
            confidence=metric_frequency.confidence,
            evidence={
                "algorithm_version": QUERY_HISTORY_MINER_VERSION,
                "occurrence_count": metric_frequency.occurrence_count,
                "sample_query_ids": list(metric_frequency.sample_query_ids),
                "value_free": True,
                "source_values_inspected": False,
                "confidence_cap": QUERY_HISTORY_CONFIDENCE_CAP,
            },
            created_by=context.principal_id,
        )
        session.add(metric_candidate)
        await session.flush()
        review = GovernanceReview(
            organization_id=datasource.organization_id,
            object_type="QUERY_HISTORY_METRIC_CANDIDATE",
            object_id=str(metric_candidate.id),
            requested_action="CREATE",
            requested_by=context.principal_id,
        )
        session.add(review)
        await session.flush()
        metric_candidate.governance_review_id = review.id
        record_outbox(
            session,
            organization_id=datasource.organization_id,
            aggregate_type="governance_review",
            aggregate_id=str(review.id),
            event_type="governance.review_requested.v1",
            payload={
                "review_id": str(review.id),
                "object_type": review.object_type,
                "object_id": str(metric_candidate.id),
                "table_id": str(table.table_id),
                "aggregation": measure.aggregation,
            },
        )
        metric_candidates.append(metric_candidate)
        existing_metric_shapes.add(shape_key)

    await session.flush()
    record_audit(
        session,
        context,
        action="query_history_miner.mine",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "queries_scanned": len(entries),
            "queries_parsed": len(structures),
            "relationship_candidates_created": len(relationship_candidates),
            "metric_candidates_created": len(metric_candidates),
            "join_candidates_unresolved": join_unresolved,
            "join_candidates_suppressed_by_negative_knowledge": join_suppressed,
            "metric_candidates_unresolved": metric_unresolved,
            "value_inspection": False,
        },
    )
    return QueryHistoryMiningResult(
        relationship_candidates=tuple(relationship_candidates),
        metric_candidates=tuple(metric_candidates),
        join_shapes_mined=len(join_frequencies),
        metric_shapes_mined=len(metric_frequencies),
        join_candidates_unresolved=join_unresolved,
        metric_candidates_unresolved=metric_unresolved,
        join_candidates_suppressed=join_suppressed,
    )


# ---------------------------------------------------------------------------
# 4. Review gate: candidate -> published metric only via the maker-checker
#    queue (mirrors classification_propagation.apply_classification_promotion)
# ---------------------------------------------------------------------------


def _conflict(detail: str) -> Exception:
    """A 409-mapped error, matching the governance dispatcher's own idiom
    (`classification_propagation._conflict`). Imported lazily so this module
    carries no hard dependency on FastAPI beyond what the shared review path
    already provides.
    """
    from fastapi import HTTPException

    return HTTPException(status_code=409, detail=detail)


def _metric_fingerprint(candidate: QueryHistoryMetricCandidate) -> str:
    payload = json.dumps(
        {
            "table_id": str(candidate.table_id),
            "measure_column_id": str(candidate.measure_column_id),
            "aggregation": candidate.aggregation,
            "grain_fingerprint": candidate.grain_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def apply_query_history_metric_candidate_decision(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    context: SecurityContext,
    now: datetime,
) -> tuple[str, str, str, dict[str, Any]]:
    """Apply an approved/rejected query-history metric candidate decision.

    Dispatched from `semantic_api._apply_governance_review_decision`, which
    has already enforced maker != checker, PENDING-only, and the
    organization-boundary preconditions -- so this is the one place a mined
    candidate becomes a real, published `SemanticMetric` +
    `SemanticMetricVersion`, and only under an independent APPROVE. On
    REJECT nothing is published; the candidate is marked `REJECTED` and
    stays as negative evidence (never deleted), matching every other
    reject-and-retain convention on this platform
    (`reject_metric_suggestion_proposal`, GL-8/GL-9's link/description
    proposal rejections).
    """
    candidate = await session.get(QueryHistoryMetricCandidate, UUID(review.object_id))
    if candidate is None or candidate.organization_id != review.organization_id:
        raise _conflict("review target is unavailable")
    if candidate.status != "PENDING":
        raise _conflict("query history metric candidate is no longer pending review")

    if decision == "APPROVE":
        slug = f"query-history-{candidate.id.hex[:12]}"
        metric = await session.scalar(
            select(SemanticMetric).where(
                SemanticMetric.project_id == candidate.project_id,
                SemanticMetric.slug == slug,
            )
        )
        if metric is None:
            metric = SemanticMetric(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                slug=slug,
            )
            session.add(metric)
            await session.flush()

        latest_model_version = await session.scalar(
            select(SemanticModelVersion.version)
            .where(SemanticModelVersion.project_id == candidate.project_id)
            .order_by(SemanticModelVersion.version.desc())
            .limit(1)
        )
        model_version = SemanticModelVersion(
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
            version=(latest_model_version or 0) + 1,
            name=f"Query-history metric: {candidate.aggregation}({slug})",
            change_summary=(
                "Auto-created to host a metric mined from warehouse query-log "
                f"structure (AT-12); see QueryHistoryMetricCandidate {candidate.id} "
                "for provenance."
            ),
            status="PUBLISHED",
            created_by="system:query-history-miner",
            approved_by=context.principal_id,
            approved_at=now,
            published_at=now,
        )
        session.add(model_version)
        await session.flush()

        version = SemanticMetricVersion(
            organization_id=candidate.organization_id,
            semantic_model_version_id=model_version.id,
            metric_id=metric.id,
            version=1,
            status="PUBLISHED",
            name=f"{candidate.aggregation} (mined from query history)",
            description=(
                "Candidate metric mined from recurring warehouse query-log structure: "
                f"{candidate.aggregation} over a measure column, grouped by "
                f"{len(candidate.grain_column_ids)} grain column(s), seen "
                f"{candidate.occurrence_count} time(s) in the query log."
            ),
            aggregation=candidate.aggregation,
            grain=", ".join(candidate.grain_column_ids) or "(none)",
            source_table_id=candidate.table_id,
            measure_column_id=candidate.measure_column_id,
            allowed_dimension_column_ids=list(candidate.grain_column_ids),
            fingerprint=_metric_fingerprint(candidate),
            created_by=candidate.created_by,
        )
        session.add(version)
        await session.flush()

        candidate.status = "APPROVED"
        candidate.published_metric_version_id = version.id
        event_type = "query_history_metric_candidate.approved.v1"
        aggregate_id = str(version.id)
    else:
        candidate.status = "REJECTED"
        event_type = "query_history_metric_candidate.rejected.v1"
        aggregate_id = str(candidate.id)

    candidate.reviewed_by = context.principal_id
    candidate.reviewed_at = now
    payload = {
        "candidate_id": str(candidate.id),
        "table_id": str(candidate.table_id),
        "measure_column_id": str(candidate.measure_column_id),
        "aggregation": candidate.aggregation,
        "review_id": str(review.id),
    }
    return event_type, "query_history_metric_candidate", aggregate_id, payload
