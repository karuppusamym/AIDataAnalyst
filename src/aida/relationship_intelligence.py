"""Deterministic, metadata-only relationship intelligence (module 06).

Everything in this file operates on names, types, declared constraints, and
value-free profile statistics only (ADR-0014) -- never on sampled source
values. It is intentionally free of any database session so that it can be
exhaustively unit tested; ``aida.intelligence_api`` fetches rows and calls
into these functions.

Covers three open items from ``Docs/20-modules/06-relationship-intelligence.md``:

* RL-1 -- table family / temporal intelligence (``detect_table_families``)
* RL-2 -- canonical table resolution (``resolve_canonical_member``)
* RL-3 -- composite relationship candidates (``generate_composite_relationship_candidates``)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """Lowercase, separator-normalized token form of an identifier."""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _has_token(normalized: str, pattern: str) -> bool:
    """True if ``pattern`` occurs as an underscore-delimited run inside ``normalized``."""
    return f"_{pattern}_" in f"_{normalized}_"


def _find_matches(names: dict[str, str], patterns: frozenset[str]) -> list[str]:
    """Return original column names whose normalized form matches any pattern."""
    return [
        original
        for original, normalized in names.items()
        if any(_has_token(normalized, pattern) for pattern in patterns)
    ]


@dataclass(frozen=True, slots=True)
class ColumnMeta:
    """Metadata-only view of one column: names, types, and (optional) profile stats."""

    id: UUID
    table_id: UUID
    name: str
    physical_type: str
    nullable: bool
    ordinal_position: int
    null_count: int | None = None
    non_null_count: int | None = None
    approximate_distinct_count: int | None = None

    @property
    def null_rate(self) -> float | None:
        if self.null_count is None or self.non_null_count is None:
            return None
        total = self.null_count + self.non_null_count
        return (self.null_count / total) if total else 0.0


# --------------------------------------------------------------------------
# RL-1 -- table family / temporal intelligence
# --------------------------------------------------------------------------

TABLE_FAMILY_ALGORITHM_VERSION = "table-family-v1"
MIN_FAMILY_CONFIDENCE = 0.60

REFERENCE_MAX_ROW_COUNT = 10_000
REFERENCE_MIN_INBOUND_REFERENCES = 2
NEAR_DUPLICATE_KEY_MAX_DISTINCT_RATIO = 0.7
DELTA_LOW_CARDINALITY_MAX_DISTINCT = 10
SNAPSHOT_MIN_CLUSTER_SIZE = 2
SNAPSHOT_MIN_COLUMN_JACCARD = 0.75

_TEMPORAL_START_PATTERNS = frozenset(
    {
        "effective_date",
        "effective_from",
        "valid_from",
        "start_date",
        "as_of_date",
        "asof_date",
        "snapshot_date",
        "record_date",
        "valid_start",
        "version_date",
    }
)
_TEMPORAL_END_PATTERNS = frozenset(
    {
        "effective_to",
        "valid_to",
        "end_date",
        "expiry_date",
        "expiration_date",
        "valid_end",
    }
)
_VERSION_PATTERNS = frozenset(
    {"version", "version_number", "row_version", "revision", "rev_number", "record_version"}
)
_CURRENT_FLAG_PATTERNS = frozenset(
    {"is_current", "current_flag", "active_flag", "is_active", "current_indicator", "is_latest"}
)
_CHANGE_OP_PATTERNS = frozenset(
    {"op", "operation", "op_type", "change_type", "cdc_operation", "dml_type", "action_type"}
)
_CDC_META_PATTERNS = frozenset(
    {"cdc_timestamp", "kafka_offset", "lsn", "commit_lsn", "source_lsn", "deleted", "cdc_sequence"}
)
_INSERT_AUDIT_PATTERNS = frozenset({"created_at", "inserted_at", "load_ts", "ingested_at"})
_UPDATE_AUDIT_PATTERNS = frozenset({"updated_at", "modified_at", "last_modified", "update_ts"})

_HISTORY_NAME_SUFFIXES = ("history", "hist", "audit", "log")
_SNAPSHOT_NAME_TOKENS = ("snapshot", "snap", "bak", "backup", "archive", "old", "copy")


@dataclass(frozen=True, slots=True)
class TableFamilyObservation:
    """One table's metadata-only shape, as needed for family detection."""

    table_id: UUID
    table_name: str
    schema_id: UUID
    primary_key_columns: tuple[str, ...]
    columns: tuple[ColumnMeta, ...]
    row_count_estimate: int | None = None
    inbound_reference_count: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FamilySignal:
    confidence: float
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TableFamilyMemberResult:
    table_id: UUID
    confidence: float
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TableFamilyGroupResult:
    family_key: str
    family_type: str
    algorithm_version: str
    confidence: float
    evidence: dict[str, Any]
    members: tuple[TableFamilyMemberResult, ...]


def base_entity_key(table_name: str) -> tuple[str, str | None, str | None]:
    """Strip a recognized variant suffix from a table name.

    Returns ``(base, date_suffix, variant_kind)`` where ``variant_kind`` is
    ``"date"``, ``"snapshot_token"``, ``"history"``, or ``None`` when no
    recognized suffix was present.
    """
    normalized = _normalize(table_name)
    date_match = re.match(
        r"^(?P<base>.+)_(?P<suffix>\d{8}|\d{4}_\d{2}_\d{2}|\d{6})$", normalized
    )
    if date_match:
        return date_match.group("base"), date_match.group("suffix"), "date"
    for token in _SNAPSHOT_NAME_TOKENS:
        token_match = re.match(rf"^(?P<base>.+)_{token}_?(?P<suffix>\d*)$", normalized)
        if token_match:
            suffix = token_match.group("suffix") or None
            return token_match.group("base"), suffix, "snapshot_token"
    for suffix_word in _HISTORY_NAME_SUFFIXES:
        if normalized.endswith(f"_{suffix_word}"):
            return normalized[: -(len(suffix_word) + 1)], None, "history"
    return normalized, None, None


def _score_history(observation: TableFamilyObservation) -> FamilySignal:
    names = {column.name: _normalize(column.name) for column in observation.columns}
    temporal = _find_matches(names, _TEMPORAL_START_PATTERNS | _TEMPORAL_END_PATTERNS)
    versioned = _find_matches(names, _VERSION_PATTERNS)
    _, _, variant_kind = base_entity_key(observation.table_name)
    name_hint = variant_kind == "history"

    near_duplicate_keys = False
    ratio: float | None = None
    if len(observation.primary_key_columns) == 1:
        pk_name = observation.primary_key_columns[0]
        pk_column = next(
            (c for c in observation.columns if c.name.lower() == pk_name.lower()), None
        )
        if (
            pk_column is not None
            and pk_column.approximate_distinct_count is not None
            and pk_column.non_null_count
        ):
            ratio = pk_column.approximate_distinct_count / pk_column.non_null_count
            near_duplicate_keys = ratio <= NEAR_DUPLICATE_KEY_MAX_DISTINCT_RATIO

    confidence = 0.0
    if temporal:
        confidence += 0.40
    if versioned:
        confidence += 0.20
    if near_duplicate_keys:
        confidence += 0.30
    if name_hint:
        confidence += 0.15
    confidence = min(confidence, 0.95)
    evidence = {
        "temporal_columns": temporal,
        "version_columns": versioned,
        "near_duplicate_keys": near_duplicate_keys,
        "distinct_to_row_ratio": round(ratio, 4) if ratio is not None else None,
        "name_hint": name_hint,
    }
    return FamilySignal(confidence, evidence)


def _score_scd(observation: TableFamilyObservation) -> FamilySignal:
    names = {column.name: _normalize(column.name) for column in observation.columns}
    start_cols = _find_matches(names, _TEMPORAL_START_PATTERNS)
    end_cols = _find_matches(names, _TEMPORAL_END_PATTERNS)
    current_flag_cols = _find_matches(names, _CURRENT_FLAG_PATTERNS)
    has_pair = bool(start_cols) and bool(end_cols)

    confidence = 0.0
    if has_pair:
        confidence += 0.55
    if current_flag_cols:
        confidence += 0.35
    confidence = min(confidence, 0.95)
    evidence = {
        "effective_columns": start_cols,
        "expiry_columns": end_cols,
        "current_flag_columns": current_flag_cols,
        "effective_expiry_pair": has_pair,
    }
    return FamilySignal(confidence, evidence)


def _score_delta_cdc(observation: TableFamilyObservation) -> FamilySignal:
    names = {column.name: _normalize(column.name) for column in observation.columns}
    op_cols = _find_matches(names, _CHANGE_OP_PATTERNS)
    cdc_meta_cols = _find_matches(names, _CDC_META_PATTERNS)
    low_cardinality_op = False
    for column in observation.columns:
        if column.name in op_cols and column.approximate_distinct_count is not None:
            if column.approximate_distinct_count <= DELTA_LOW_CARDINALITY_MAX_DISTINCT:
                low_cardinality_op = True

    confidence = 0.0
    if op_cols:
        confidence += 0.45
    if low_cardinality_op:
        confidence += 0.25
    if cdc_meta_cols:
        confidence += 0.30
    confidence = min(confidence, 0.95)
    evidence = {
        "change_operation_columns": op_cols,
        "cdc_metadata_columns": cdc_meta_cols,
        "low_cardinality_operation_column": low_cardinality_op,
    }
    return FamilySignal(confidence, evidence)


def _score_append_only(observation: TableFamilyObservation) -> FamilySignal:
    names = {column.name: _normalize(column.name) for column in observation.columns}
    insert_audit = _find_matches(names, _INSERT_AUDIT_PATTERNS)
    update_audit = _find_matches(names, _UPDATE_AUDIT_PATTERNS)
    single_pk = len(observation.primary_key_columns) == 1
    monotonic_pk_type = False
    if single_pk:
        pk_name = observation.primary_key_columns[0]
        pk_column = next(
            (c for c in observation.columns if c.name.lower() == pk_name.lower()), None
        )
        if pk_column is not None:
            monotonic_pk_type = (
                pk_column.ordinal_position == 1
                and not pk_column.nullable
                and any(
                    token in pk_column.physical_type.lower()
                    for token in ("int", "serial", "uuid", "identity")
                )
            )

    confidence = 0.0
    if insert_audit:
        confidence += 0.35
    if insert_audit and not update_audit:
        confidence += 0.30
    if single_pk and monotonic_pk_type:
        confidence += 0.30
    confidence = min(confidence, 0.95)
    evidence = {
        "insert_audit_columns": insert_audit,
        "update_audit_columns": update_audit,
        "single_monotonic_primary_key": single_pk and monotonic_pk_type,
    }
    return FamilySignal(confidence, evidence)


def _score_reference(observation: TableFamilyObservation) -> FamilySignal:
    is_small = (
        observation.row_count_estimate is not None
        and observation.row_count_estimate <= REFERENCE_MAX_ROW_COUNT
    )
    is_widely_referenced = observation.inbound_reference_count >= REFERENCE_MIN_INBOUND_REFERENCES
    confidence = 0.0
    if is_small and is_widely_referenced:
        confidence = 0.55 + min(observation.inbound_reference_count, 10) * 0.03
    confidence = min(confidence, 0.90)
    evidence = {
        "row_count_estimate": observation.row_count_estimate,
        "small_table": is_small,
        "inbound_reference_count": observation.inbound_reference_count,
        "widely_referenced": is_widely_referenced,
    }
    return FamilySignal(confidence, evidence)


_SINGLE_TABLE_SCORERS: dict[str, Any] = {
    "SCD": _score_scd,
    "HISTORY": _score_history,
    "DELTA_CDC": _score_delta_cdc,
    "APPEND_ONLY": _score_append_only,
    "REFERENCE": _score_reference,
}


def evaluate_single_table_family_signals(
    observation: TableFamilyObservation,
) -> dict[str, FamilySignal]:
    """Score every non-snapshot family type for one table, independent of siblings."""
    return {name: scorer(observation) for name, scorer in _SINGLE_TABLE_SCORERS.items()}


def _column_name_set(observation: TableFamilyObservation) -> frozenset[str]:
    return frozenset(_normalize(column.name) for column in observation.columns)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


@dataclass(frozen=True, slots=True)
class _SnapshotCluster:
    base_key: str
    schema_id: UUID
    members: tuple[TableFamilyObservation, ...]
    min_column_jaccard: float
    date_suffixed_count: int


def _snapshot_clusters(
    observations: list[TableFamilyObservation],
) -> list[_SnapshotCluster]:
    grouped: dict[tuple[UUID, str], list[tuple[TableFamilyObservation, str | None]]] = {}
    for observation in observations:
        base, suffix, variant_kind = base_entity_key(observation.table_name)
        if variant_kind not in ("date", "snapshot_token"):
            continue
        grouped.setdefault((observation.schema_id, base), []).append((observation, suffix))

    clusters: list[_SnapshotCluster] = []
    for (schema_id, base_key), members_with_suffix in grouped.items():
        if len(members_with_suffix) < SNAPSHOT_MIN_CLUSTER_SIZE:
            continue
        members_with_suffix = sorted(
            members_with_suffix, key=lambda item: str(item[0].table_id)
        )
        members = tuple(item[0] for item in members_with_suffix)
        column_sets = [_column_name_set(member) for member in members]
        min_similarity = 1.0
        for i in range(len(column_sets)):
            for j in range(i + 1, len(column_sets)):
                min_similarity = min(min_similarity, _jaccard(column_sets[i], column_sets[j]))
        if min_similarity < SNAPSHOT_MIN_COLUMN_JACCARD:
            continue
        date_suffixed_count = sum(1 for _, suffix in members_with_suffix if suffix)
        clusters.append(
            _SnapshotCluster(
                base_key=base_key,
                schema_id=schema_id,
                members=members,
                min_column_jaccard=min_similarity,
                date_suffixed_count=date_suffixed_count,
            )
        )
    return clusters


def _snapshot_cluster_confidence(cluster: _SnapshotCluster) -> float:
    confidence = 0.65
    confidence += min(len(cluster.members) - 2, 3) * 0.05
    if cluster.date_suffixed_count == len(cluster.members):
        confidence += 0.10
    confidence += (cluster.min_column_jaccard - SNAPSHOT_MIN_COLUMN_JACCARD) * 0.2
    return min(confidence, 0.95)


def detect_table_families(
    observations: list[TableFamilyObservation],
) -> list[TableFamilyGroupResult]:
    """Detect table families (history/snapshot/delta/SCD/append-only/reference).

    Deterministic and metadata-only. Returns one group per detected family;
    tables with no signal above ``MIN_FAMILY_CONFIDENCE`` are omitted.
    """
    by_table_id = {observation.table_id: observation for observation in observations}
    claimed: dict[UUID, TableFamilyGroupResult] = {}

    for cluster in _snapshot_clusters(observations):
        snapshot_confidence = _snapshot_cluster_confidence(cluster)
        if snapshot_confidence < MIN_FAMILY_CONFIDENCE:
            continue
        member_results = []
        wins = True
        for member in cluster.members:
            best_single = max(
                evaluate_single_table_family_signals(member).values(),
                key=lambda signal: signal.confidence,
                default=FamilySignal(0.0, {}),
            )
            if best_single.confidence > snapshot_confidence:
                wins = False
                break
        if not wins:
            continue
        for member in cluster.members:
            _, suffix, _ = base_entity_key(member.table_name)
            member_results.append(
                TableFamilyMemberResult(
                    table_id=member.table_id,
                    confidence=snapshot_confidence,
                    evidence={
                        "date_or_variant_suffix": suffix,
                        "column_count": len(member.columns),
                    },
                )
            )
        group = TableFamilyGroupResult(
            family_key=f"{cluster.schema_id}:{cluster.base_key}",
            family_type="SNAPSHOT",
            algorithm_version=TABLE_FAMILY_ALGORITHM_VERSION,
            confidence=snapshot_confidence,
            evidence={
                "member_count": len(cluster.members),
                "min_column_name_jaccard": round(cluster.min_column_jaccard, 4),
                "date_suffixed_member_count": cluster.date_suffixed_count,
                "signal": "DATE_PARTITIONED_FULL_COPIES",
            },
            members=tuple(member_results),
        )
        for member in cluster.members:
            claimed[member.table_id] = group

    groups: dict[str, TableFamilyGroupResult] = {
        group.family_key: group for group in claimed.values()
    }

    for table_id, observation in by_table_id.items():
        if table_id in claimed:
            continue
        signals = evaluate_single_table_family_signals(observation)
        family_type, best = max(signals.items(), key=lambda item: item[1].confidence)
        if best.confidence < MIN_FAMILY_CONFIDENCE:
            continue
        base, _, _ = base_entity_key(observation.table_name)
        family_key = f"{observation.schema_id}:{base}:{family_type}"
        groups[family_key] = TableFamilyGroupResult(
            family_key=family_key,
            family_type=family_type,
            algorithm_version=TABLE_FAMILY_ALGORITHM_VERSION,
            confidence=best.confidence,
            evidence={**best.evidence, "all_family_scores": {
                name: round(signal.confidence, 4) for name, signal in signals.items()
            }},
            members=(
                TableFamilyMemberResult(
                    table_id=observation.table_id,
                    confidence=best.confidence,
                    evidence=best.evidence,
                ),
            ),
        )

    return sorted(groups.values(), key=lambda group: group.family_key)


# --------------------------------------------------------------------------
# RL-2 -- canonical table resolution
# --------------------------------------------------------------------------

CANONICAL_RESOLUTION_ALGORITHM_VERSION = "canonical-resolution-v1"


@dataclass(frozen=True, slots=True)
class CanonicalCandidate:
    """One family member as input to canonical selection."""

    table_id: UUID
    table_name: str
    row_count_estimate: int | None = None
    inbound_reference_count: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CanonicalResolution:
    table_id: UUID
    confidence: float
    algorithm_version: str
    evidence: dict[str, Any]


def _snapshot_recency_token(table_name: str) -> str | None:
    _, suffix, variant_kind = base_entity_key(table_name)
    return suffix if variant_kind == "date" else None


def _history_penalized(table_name: str) -> bool:
    _, _, variant_kind = base_entity_key(table_name)
    return variant_kind in ("history", "snapshot_token")


def resolve_canonical_member(
    candidates: list[CanonicalCandidate], *, family_type: str
) -> CanonicalResolution:
    """Pick the family member an agent should default to, with inspectable evidence.

    Deterministic: recency (snapshot date token, then ``updated_at``), then
    how widely referenced the table is, then row count, then table id, as a
    final tie-break so the result never depends on input ordering.
    """
    if not candidates:
        raise ValueError("resolve_canonical_member requires at least one candidate")
    if len(candidates) == 1:
        only = candidates[0]
        return CanonicalResolution(
            table_id=only.table_id,
            confidence=1.0,
            algorithm_version=CANONICAL_RESOLUTION_ALGORITHM_VERSION,
            evidence={"reason": "ONLY_FAMILY_MEMBER"},
        )

    scored: list[tuple[tuple[Any, ...], CanonicalCandidate, dict[str, Any]]] = []
    for candidate in candidates:
        snapshot_token = _snapshot_recency_token(candidate.table_name)
        penalized = _history_penalized(candidate.table_name)
        sort_key = (
            snapshot_token or "",  # lexicographic works for YYYYMMDD / YYYYMM / YYYY_MM_DD-ish
            0 if not penalized else -1,
            candidate.updated_at or datetime.min.replace(tzinfo=None),
            candidate.inbound_reference_count,
            candidate.row_count_estimate or 0,
        )
        detail = {
            "table_id": str(candidate.table_id),
            "snapshot_recency_token": snapshot_token,
            "history_or_variant_suffixed": penalized,
            "inbound_reference_count": candidate.inbound_reference_count,
            "row_count_estimate": candidate.row_count_estimate,
            "updated_at": candidate.updated_at.isoformat() if candidate.updated_at else None,
        }
        scored.append((sort_key, candidate, detail))

    scored.sort(key=lambda item: item[0], reverse=True)
    # Deterministic final tie-break on table_id when every other signal ties.
    top_key = scored[0][0]
    tied = [item for item in scored if item[0] == top_key]
    tied.sort(key=lambda item: str(item[1].table_id))
    winner_key, winner, winner_detail = tied[0]

    spread = max(1, len(scored) - 1)
    rank = next(i for i, item in enumerate(scored) if item[1].table_id == winner.table_id)
    confidence = round(0.55 + 0.4 * (1 - rank / spread), 4)

    return CanonicalResolution(
        table_id=winner.table_id,
        confidence=confidence,
        algorithm_version=CANONICAL_RESOLUTION_ALGORITHM_VERSION,
        evidence={
            "family_type": family_type,
            "selected": winner_detail,
            "candidates_considered": [detail for _, _, detail in scored],
        },
    )


# --------------------------------------------------------------------------
# RL-3 -- composite (multi-column) relationship candidates
# --------------------------------------------------------------------------

COMPOSITE_ALGORITHM_VERSION = "composite-relationship-v1"
COMPOSITE_DETECTION_RULE = "COMPOSITE_EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1"
COMPOSITE_MAX_CONFIDENCE = 0.93
COMPOSITE_HIGH_NULL_RATE = 0.98


@dataclass(frozen=True, slots=True)
class CompositeMemberPair:
    ordinal: int
    source_column_id: UUID
    target_column_id: UUID
    source_column_name: str
    target_column_name: str


@dataclass(frozen=True, slots=True)
class CompositeCandidateResult:
    source_table_id: UUID
    target_table_id: UUID
    members: tuple[CompositeMemberPair, ...]
    confidence: float
    evidence: dict[str, Any]
    detection_rule: str = COMPOSITE_DETECTION_RULE

    @property
    def fingerprint(self) -> str:
        return composite_group_fingerprint(
            self.source_table_id,
            self.target_table_id,
            [(pair.source_column_id, pair.target_column_id) for pair in self.members],
        )


def composite_group_fingerprint(
    source_table_id: UUID,
    target_table_id: UUID,
    ordered_column_pairs: list[tuple[UUID, UUID]],
) -> str:
    """Stable identity for a composite candidate: order matters, dedupe is exact."""
    material = "|".join(
        [str(source_table_id), str(target_table_id)]
        + [f"{source}:{target}" for source, target in ordered_column_pairs]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def generate_composite_relationship_candidates(
    *,
    columns_by_table: dict[UUID, tuple[ColumnMeta, ...]],
    composite_primary_keys: dict[UUID, tuple[str, ...]],
    declared_composite_foreign_keys: frozenset[
        tuple[UUID, tuple[str, ...], UUID, tuple[str, ...]]
    ] = frozenset(),
    existing_fingerprints: frozenset[str] = frozenset(),
    max_group_columns: int = 4,
    max_candidates_per_table: int = 25,
) -> list[CompositeCandidateResult]:
    """Propose bounded, evidence-backed composite (multi-column) FK-like candidates.

    Follows the module's pruning order: declared constraints are skipped as
    facts, name/type compatibility prunes first, profile-based nullability
    prunes further, ordinal alignment only refines the score, and survivors
    are capped per target table.
    """
    results: list[CompositeCandidateResult] = []

    for target_table_id in sorted(composite_primary_keys, key=str):
        pk_columns = composite_primary_keys[target_table_id]
        if not (2 <= len(pk_columns) <= max_group_columns):
            continue
        target_columns = columns_by_table.get(target_table_id, ())
        target_by_lower = {column.name.lower(): column for column in target_columns}
        pk_column_metas = [target_by_lower.get(name.lower()) for name in pk_columns]
        if any(meta is None for meta in pk_column_metas):
            continue

        per_table_count = 0
        for source_table_id in sorted(columns_by_table, key=str):
            if source_table_id == target_table_id or per_table_count >= max_candidates_per_table:
                continue
            source_columns = columns_by_table[source_table_id]
            source_by_lower = {column.name.lower(): column for column in source_columns}

            # Rule 2: name and type compatibility prunes first -- every PK column
            # must exist on the source side with a compatible physical type.
            matched: list[tuple[ColumnMeta, ColumnMeta]] = []
            type_mismatch = False
            for target_meta in pk_column_metas:
                assert target_meta is not None
                source_meta = source_by_lower.get(target_meta.name.lower())
                if source_meta is None:
                    matched = []
                    break
                if source_meta.physical_type.lower() != target_meta.physical_type.lower():
                    type_mismatch = True
                    break
                matched.append((source_meta, target_meta))
            if type_mismatch or len(matched) != len(pk_column_metas):
                continue

            ordered_pairs = [(source.id, target.id) for source, target in matched]
            source_names = tuple(source.name.lower() for source, _ in matched)
            target_names = tuple(target.name.lower() for _, target in matched)
            declared_key = (source_table_id, source_names, target_table_id, target_names)
            if declared_key in declared_composite_foreign_keys:
                continue  # Rule 1: declared constraints are facts, not candidates.

            fingerprint = composite_group_fingerprint(
                source_table_id, target_table_id, ordered_pairs
            )
            if fingerprint in existing_fingerprints:
                continue

            # Rule 3: cardinality/nullability profile pruning, best-effort.
            null_rates = [source.null_rate for source, _ in matched]
            known_null_rates = [rate for rate in null_rates if rate is not None]
            all_source_null = bool(known_null_rates) and all(
                rate is not None and rate >= COMPOSITE_HIGH_NULL_RATE for rate in known_null_rates
            )
            if all_source_null:
                continue

            # Rule 4: ordinal position / parent-table context refines (not a prune).
            source_ordinals = [source.ordinal_position for source, _ in matched]
            ordinal_alignment = source_ordinals == sorted(source_ordinals)

            confidence = 0.65 + 0.05 * (len(matched) - 2)
            if known_null_rates and max(known_null_rates) < 0.05:
                confidence += 0.05
            if ordinal_alignment:
                confidence += 0.05
            confidence = min(confidence, COMPOSITE_MAX_CONFIDENCE)

            members = tuple(
                CompositeMemberPair(
                    ordinal=index,
                    source_column_id=source.id,
                    target_column_id=target.id,
                    source_column_name=source.name,
                    target_column_name=target.name,
                )
                for index, (source, target) in enumerate(matched)
            )
            evidence = {
                "algorithm_version": COMPOSITE_ALGORITHM_VERSION,
                "member_count": len(members),
                "column_name_match": "EXACT_CASE_INSENSITIVE",
                "physical_type_match": "EXACT",
                "target_is_primary_key": True,
                "ordinal_alignment": ordinal_alignment,
                "known_null_rates": [round(rate, 4) for rate in known_null_rates] or None,
                "source_values_inspected": False,
            }
            results.append(
                CompositeCandidateResult(
                    source_table_id=source_table_id,
                    target_table_id=target_table_id,
                    members=members,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
            per_table_count += 1

    return results
