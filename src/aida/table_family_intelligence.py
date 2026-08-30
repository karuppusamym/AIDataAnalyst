"""RL-1: table family / temporal intelligence -- pure detection logic.

Across a schema, groups of tables are often really *one logical entity*
expressed multiple ways over time or state. This module implements four
detectors as small, pure functions that operate on already-fetched, plain
data (table names and column name/type/nullable tuples) -- no session, no
I/O -- following the exact split established by
``aida.catalog_bulk_actions``: the API layer (``aida.table_family_api``) is
responsible for the bounded database fetch and for persisting whatever a
detector decides, and de-duplicating against previously-persisted rows.
That keeps detection heuristics directly unit-testable without a database
and makes every detector idempotent -- calling it twice on the same input
always returns the same candidates.

Every detector here is heuristic, evidence-backed pattern matching, not
certainty -- confidence scores are deliberately conservative, and every
candidate carries an ``evidence`` payload precise enough for a human
reviewer to see *why* it fired without re-querying anything. This is why
RL-1's exit condition is review-gated (PENDING candidates), not
auto-applied.

Detectors covered:

* ``detect_snapshot_families`` -- tables named with an incrementing
  date/period suffix (``sales_20240101``, ``sales_20240201``, ...).
* ``detect_history_families`` -- a "live" table plus a sibling that
  accumulates its historical versions (``orders`` / ``orders_history``).
* ``detect_delta_families`` -- a table plus an incremental change-set
  sibling meant to be applied to it (``customers`` / ``customers_delta``).
* ``detect_scd_tables`` -- a single table carrying explicit temporal
  validity columns (SCD Type 2), not a family of several tables.

``detect_table_families`` runs all four and returns the combined list. Each
detector only ever compares tables *within* the single schema it is given
-- the caller fetches one schema's tables at a time, which is what keeps
this bounded (see RL-1 / CT-2: no unbounded cross-schema or
cross-datasource scanning).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# A single table's columns, as fetched by the caller: (name, physical_type, nullable).
ColumnInput = tuple[str, str, bool]

# A single table, as fetched by the caller: (table_id, table_name, columns).
TableInput = tuple[UUID, str, Sequence[ColumnInput]]

FAMILY_TYPES = ("SNAPSHOT", "HISTORY", "DELTA", "SCD")


@dataclass(frozen=True)
class FamilyCandidateDraft:
    """One detector finding, ready for the API layer to dedupe and persist."""

    family_type: str
    member_table_ids: list[UUID]
    base_table_id: UUID | None
    detection_rule: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def member_key(self) -> tuple[str, frozenset[UUID]]:
        """Identity used for de-duplication: family type + member set."""
        return (self.family_type, frozenset(self.member_table_ids))


# ---------------------------------------------------------------------------
# Snapshot family detection
# ---------------------------------------------------------------------------

# A family must have at least this many date/period-suffixed siblings before
# it is reported at all. Two tables sharing a plausible date suffix is weak,
# coincidental-looking evidence on its own (lots of unrelated table pairs
# happen to both end in a number); three or more sharing the exact same base
# is a much stronger signal of a genuine per-period series and is the
# threshold this module treats as "a family" rather than a coincidence.
MIN_SNAPSHOT_FAMILY_SIZE = 3

# Suffix token patterns, most specific (and highest-confidence) first. Each
# pattern fully matches a table name as `<base><sep><token>` and is anchored
# on both ends. `_SEP` is a single optional separator so both
# `sales_20240101` (separator present) and `sales20240101` (no separator,
# still unambiguous because the date fields themselves are range-checked)
# are recognized.
_SEP = r"[_-]?"
_YEAR = r"(?:19|20)\d{2}"
_MONTH = r"0[1-9]|1[0-2]"
_DAY = r"0[1-9]|[12]\d|3[01]"

_DATE_SUFFIX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "YYYYMMDD",
        re.compile(rf"^(?P<base>.+?){_SEP}(?P<y>{_YEAR})(?P<m>{_MONTH})(?P<d>{_DAY})$"),
    ),
    (
        "YYYY_MM_DD",
        re.compile(rf"^(?P<base>.+?)[_-](?P<y>{_YEAR})[_-](?P<m>{_MONTH})[_-](?P<d>{_DAY})$"),
    ),
    (
        "YYYY_MM",
        re.compile(rf"^(?P<base>.+?)[_-](?P<y>{_YEAR})[_-](?P<m>{_MONTH})$"),
    ),
)

# Weakest, most general period token: a bare trailing integer of bounded
# length (1-6 digits), e.g. `part_001`, `extract_7`. Tried only after every
# date pattern above has failed to match, since a date-shaped suffix is
# always the more specific (and more confident) explanation.
_SEQUENCE_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+?)[_-](?P<n>\d{1,6})$")


def _match_snapshot_suffix(table_name: str) -> tuple[str, str, str] | None:
    """Return (base, token_kind, matched_token) if `table_name` ends in a
    recognized date-like or sequential-period suffix, else None."""
    for kind, pattern in _DATE_SUFFIX_PATTERNS:
        match = pattern.match(table_name)
        if match:
            return match.group("base"), kind, match.group(0)[len(match.group("base")) :]
    match = _SEQUENCE_SUFFIX_PATTERN.match(table_name)
    if match:
        return match.group("base"), "SEQUENCE", match.group(0)[len(match.group("base")) :]
    return None


def detect_snapshot_families(tables: Sequence[TableInput]) -> list[FamilyCandidateDraft]:
    """Group tables sharing a base name plus a date-like/sequential suffix.

    Emits one SNAPSHOT candidate per base-name group with >= 3 members
    (see ``MIN_SNAPSHOT_FAMILY_SIZE``). No `base_table_id` is set -- a pure
    snapshot series has no single "current" member.
    """
    groups: dict[str, list[tuple[UUID, str, str, str]]] = {}
    for table_id, table_name, _columns in tables:
        matched = _match_snapshot_suffix(table_name)
        if matched is None:
            continue
        base, kind, token = matched
        base_key = base.casefold()
        groups.setdefault(base_key, []).append((table_id, table_name, kind, token))

    candidates: list[FamilyCandidateDraft] = []
    for members in groups.values():
        if len(members) < MIN_SNAPSHOT_FAMILY_SIZE:
            continue
        kinds_present = {kind for _, _, kind, _ in members}
        # Confidence: date-shaped suffixes are a much stronger signal than a
        # bare incrementing integer (which could just as easily be an
        # unrelated numbering scheme), so score the group by its strongest
        # matched kind. A larger family is also modestly more convincing
        # than a bare minimum-sized one, capped well short of certainty --
        # this is still name-pattern matching, not schema/shape comparison.
        strongest_is_date = kinds_present != {"SEQUENCE"}
        base_confidence = 0.65 if strongest_is_date else 0.5
        confidence = min(
            0.9, base_confidence + 0.05 * (len(members) - MIN_SNAPSHOT_FAMILY_SIZE)
        )
        candidates.append(
            FamilyCandidateDraft(
                family_type="SNAPSHOT",
                member_table_ids=[table_id for table_id, *_ in members],
                base_table_id=None,
                detection_rule="SNAPSHOT_DATE_SUFFIX_SERIES_V1",
                confidence=round(confidence, 2),
                evidence={
                    "base_name": members[0][1][: -len(members[0][3])] or members[0][1],
                    "member_count": len(members),
                    "suffix_kinds": sorted(kinds_present),
                    "members": [
                        {"table_id": str(table_id), "table_name": name, "matched_suffix": token}
                        for table_id, name, _kind, token in members
                    ],
                },
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# History/audit and delta family detection (shared shape)
# ---------------------------------------------------------------------------

# Documented suffix -> confidence. A full, unambiguous word (`_history`,
# `_audit`, `_delta`, `_cdc`) scores higher than an abbreviation or a more
# generic word that occasionally means something else in a table name
# (`_archive` can denote cold storage rather than an audit trail; `_diff`
# sometimes just names a computed difference column set, not a changeset
# table) -- still well above the "corroborating only" band since the
# suffix is on the table name itself and a live sibling was actually found.
HISTORY_SUFFIXES: dict[str, float] = {
    "_history": 0.9,
    "_audit": 0.9,
    "_hist": 0.85,
    "_archive": 0.8,
}

DELTA_SUFFIXES: dict[str, float] = {
    "_delta": 0.9,
    "_cdc": 0.9,
    "_changes": 0.85,
    "_diff": 0.75,
}


def _detect_suffix_family(
    tables: Sequence[TableInput],
    *,
    family_type: str,
    suffixes: dict[str, float],
    detection_rule: str,
) -> list[FamilyCandidateDraft]:
    by_name = {name.casefold(): table_id for table_id, name, _columns in tables}
    seen_pairs: set[frozenset[UUID]] = set()
    candidates: list[FamilyCandidateDraft] = []
    for table_id, table_name, _columns in tables:
        lowered = table_name.casefold()
        for suffix, confidence in suffixes.items():
            if not lowered.endswith(suffix):
                continue
            base_name = table_name[: -len(suffix)]
            if not base_name:
                continue
            base_table_id = by_name.get(base_name.casefold())
            if base_table_id is None:
                # No live sibling in this schema -- the suffix alone is not
                # sufficient evidence of a real family (e.g. a table simply
                # named `..._archive` with nothing to pair it with).
                continue
            pair_key = frozenset({table_id, base_table_id})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            candidates.append(
                FamilyCandidateDraft(
                    family_type=family_type,
                    member_table_ids=[base_table_id, table_id],
                    base_table_id=base_table_id,
                    detection_rule=detection_rule,
                    confidence=confidence,
                    evidence={
                        "base_table_name": base_name,
                        "sibling_table_name": table_name,
                        "matched_suffix": suffix,
                    },
                )
            )
    return candidates


def detect_history_families(tables: Sequence[TableInput]) -> list[FamilyCandidateDraft]:
    """Detect a live table plus a history/audit sibling in the same schema.

    Documented suffix list: ``_history``, ``_hist``, ``_audit``, ``_archive``.
    Only fires when the non-suffixed base table actually exists.
    """
    return _detect_suffix_family(
        tables,
        family_type="HISTORY",
        suffixes=HISTORY_SUFFIXES,
        detection_rule="HISTORY_SUFFIX_SIBLING_V1",
    )


def detect_delta_families(tables: Sequence[TableInput]) -> list[FamilyCandidateDraft]:
    """Detect a base table plus an incremental change-set sibling.

    Documented suffix list: ``_delta``, ``_cdc``, ``_changes``, ``_diff``.
    Only fires when the non-suffixed base table actually exists.
    """
    return _detect_suffix_family(
        tables,
        family_type="DELTA",
        suffixes=DELTA_SUFFIXES,
        detection_rule="DELTA_SUFFIX_SIBLING_V1",
    )


# ---------------------------------------------------------------------------
# SCD Type 2 detection
# ---------------------------------------------------------------------------

# "From"-side and "to"-side temporal-validity column names (case-insensitive,
# exact match on the column name). Finding one from each side on the same
# table is the strongest possible signal -- every row genuinely carries an
# explicit validity window -- and is sufficient on its own.
SCD_FROM_COLUMNS = frozenset({"effective_date", "valid_from", "start_date", "effective_from"})
SCD_TO_COLUMNS = frozenset({"expiration_date", "valid_to", "end_date", "effective_to"})

# Corroborating-only columns: real SCD Type 2 tables very often also carry
# one of these, but each is common enough in ordinary (non-SCD) tables that
# it must never be sufficient by itself -- it only strengthens a from/to
# finding, or promotes a single from-or-to column to "sufficient". Note a
# bare `version` column is deliberately *excluded* here: it is too ambiguous
# (row-lock/optimistic-concurrency versioning is common and unrelated to
# SCD) to count as corroborating evidence at all.
SCD_CORROBORATING_COLUMNS = frozenset({"is_current", "current_flag", "version_number"})


def detect_scd_tables(tables: Sequence[TableInput]) -> list[FamilyCandidateDraft]:
    """Detect individual tables carrying SCD Type 2 temporal-validity columns.

    Sufficient to emit a candidate:
      * a "from"-side AND a "to"-side column together (strong), or
      * a "from"-side OR "to"-side column plus at least one corroborating
        column (moderate).
    Not sufficient alone: a lone from/to column, or any number of
    corroborating columns with no from/to column at all (e.g. a table with
    only an ambiguous `version` column never fires).

    Each qualifying table is its own SCD candidate, with itself as the sole
    member and `base_table_id` -- SCD Type 2 is a property of one table's
    columns, not a family of several tables.
    """
    candidates: list[FamilyCandidateDraft] = []
    for table_id, table_name, columns in tables:
        lowered_names = {name.casefold() for name, _physical_type, _nullable in columns}
        from_hits = sorted(lowered_names & SCD_FROM_COLUMNS)
        to_hits = sorted(lowered_names & SCD_TO_COLUMNS)
        corroborating_hits = sorted(lowered_names & SCD_CORROBORATING_COLUMNS)

        has_from = bool(from_hits)
        has_to = bool(to_hits)
        has_corroborating = bool(corroborating_hits)

        if has_from and has_to:
            # Strong: both sides of the validity window are present. A
            # corroborating column on top nudges confidence up slightly.
            confidence = 0.95 if has_corroborating else 0.85
            strength = "STRONG_FROM_AND_TO"
        elif (has_from or has_to) and has_corroborating:
            # Moderate: only one side of the window, but a status/version
            # column corroborates that rows are versioned over time.
            confidence = 0.6
            strength = "MODERATE_SINGLE_SIDE_PLUS_CORROBORATING"
        else:
            # Not sufficient: a lone from/to column, corroborating columns
            # alone, or neither -- do not emit.
            continue

        candidates.append(
            FamilyCandidateDraft(
                family_type="SCD",
                member_table_ids=[table_id],
                base_table_id=table_id,
                detection_rule="SCD_TYPE2_TEMPORAL_VALIDITY_COLUMNS_V1",
                confidence=confidence,
                evidence={
                    "table_name": table_name,
                    "strength": strength,
                    "from_columns_found": from_hits,
                    "to_columns_found": to_hits,
                    "corroborating_columns_found": corroborating_hits,
                },
            )
        )
    return candidates


def detect_table_families(tables: Sequence[TableInput]) -> list[FamilyCandidateDraft]:
    """Run every detector over one schema's tables and return all findings.

    Bounded by construction: this never compares tables across schemas or
    datasources -- the caller (``table_family_api``) fetches exactly one
    schema's tables/columns per call.
    """
    return [
        *detect_snapshot_families(tables),
        *detect_history_families(tables),
        *detect_delta_families(tables),
        *detect_scd_tables(tables),
    ]
