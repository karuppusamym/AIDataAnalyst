"""Metric-formula collision detection (AT-17).

GL-3 already detects when two *glossary terms* collide -- two different
`GlossaryTerm`s sharing a display name or synonym. AT-17 is the sibling gap
GL-3 does not close: two *metrics* that compute the same number a different
way, published under different names and owners, so a question routed to one
and a question routed to the other silently disagree. This module is the
pure, DB-free half of that detector, mirroring the `aida.semantic_diff`
(SM-7) idiom already established in this module (07): it takes plain
`Mapping[str, Any]` snapshots -- the shape a caller's own
`SemanticMetricVersion` read already takes -- and returns a structured
result with no session, no ORM import, and no I/O, so it is fully
unit-testable (`tests/test_metric_formula_signature.py`) without a database.
The DB-facing half that turns real `SemanticMetricVersion` rows into these
snapshots, dedupes against already-open conflicts, and persists a
`GlossaryConflict` row per new collision lives in `aida.semantic_api`
(`detect_metric_formula_collisions`), the only place that touches a session
for this feature -- see that function's docstring for how the result here
gets wired into the same governance queue GL-3 built.

**What "formula" means for a `SemanticMetricVersion`, and why sqlglot does
not apply here.** GL-3's synonym collision compares free-text labels. The
natural instinct for a metric-formula collision is to reach for a raw SQL
expression and parse it with `sqlglot` (as `aida.sql_guard` already does
elsewhere in this codebase). That does not apply here: `SemanticMetricVersion`
(`aida.models`) carries no SQL text and no expression DSL at all -- a
metric's formula is entirely structural: one `aggregation` enum
(`SUM`/`COUNT`/`AVG`/`MIN`/`MAX`, see `SemanticMetricCreate` in
`aida.schemas`) applied to one `measure_column_id` (nullable only for
`COUNT`), over one `source_table_id`, at one free-text `grain`, anchored to
one optional `default_time_column_id`. There is no ratio/composite metric
type in this schema (no numerator/denominator, no derived-metric-of-metrics
shape), so the textbook cross-aggregation example -- "`SUM(amount) /
COUNT(*)` and `AVG(amount)` compute the same thing" -- cannot even be posed
as two *different* metric definitions here: nothing in this schema can
express a ratio metric to begin with. Parsing SQL would be solving a problem
this schema does not have.

**What this module actually detects, and its honest limit.** Two different
metrics (different `metric_id`, i.e. different name/slug/owner) collide when
their structural formula tuple -- `(aggregation, source_table_id,
measure_column_id, default_time_column_id, grain)` -- is the same computation:

* ``EXACT_MATCH`` -- every field is identical, including the raw `grain`
  string. The purest case: two metrics that are byte-for-byte the same
  formula under two different names.
* ``NORMALIZED_GRAIN_MATCH`` -- every field matches except `grain`, which
  matches only after the same `strip().casefold()` normalization GL-3
  already applies to glossary labels (e.g. ``"Daily"`` and ``"daily "``).
  This is the one genuinely *semantic, not textual* equivalence this module
  reaches: two formulas that read as different strings but denote the same
  grain.

Anything past that is out of reach and this module does not attempt it, by
design rather than oversight:

* **No cross-aggregation algebra.** `SUM`/`COUNT`/`AVG`/`MIN`/`MAX` are
  compared for literal equality only. Proving `SUM(x)/COUNT(*) == AVG(x)`,
  or that two `MIN`s over overlapping-but-not-identical filters agree, is
  not attempted -- and, per the paragraph above, the ratio case cannot even
  arise as two metric rows in this schema.
* **No column-alias resolution.** Two different `measure_column_id`s are
  always treated as different, even if lineage or the glossary would say
  they trace to the same underlying value (e.g. a renamed or re-derived
  copy of the same column). Only an exact `measure_column_id` match counts.
* **No grain synonym taxonomy.** `NORMALIZED_GRAIN_MATCH` is case/whitespace
  normalization only, deliberately mirroring GL-3's own label normalization
  rather than inventing a "daily" == "1d" == "per day" business dictionary
  this codebase has no other authority for.

Exact-formula and grain-normalized duplication is still a real, valuable
catch on its own: it is precisely the "two metrics computing the same thing
differently[-named]" case that corrupts an answer when a question happens to
route to the less-visible one -- it is just not full semantic equivalence
over arbitrary formulas, which this schema has no representation for in the
first place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

MatchKind = Literal["EXACT_MATCH", "NORMALIZED_GRAIN_MATCH"]


@dataclass(frozen=True, slots=True)
class MetricFormulaSignature:
    """The normalized formula identity of one published `SemanticMetricVersion`.

    `metric_id` (not `metric_version.id`) is what distinguishes one metric
    from another -- a metric can have many versions, but two versions of the
    *same* metric are never a "collision", they are the same definition over
    time. `metric_version_id` is carried through only for reporting (which
    concrete version was scanned), never for the equality comparison.
    """

    metric_version_id: str
    metric_id: str
    metric_name: str
    aggregation: str
    source_table_id: str
    measure_column_id: str | None
    default_time_column_id: str | None
    grain_raw: str
    grain_normalized: str


@dataclass(frozen=True, slots=True)
class FormulaCollision:
    """One detected collision between two different metrics' formulas."""

    match_kind: MatchKind
    left: MetricFormulaSignature
    right: MetricFormulaSignature


def normalize_metric_formula(snapshot: Mapping[str, Any]) -> MetricFormulaSignature:
    """Build a comparable signature from one metric-version snapshot.

    `snapshot` carries at minimum `metric_version_id`, `metric_id`,
    `metric_name`, `aggregation`, `source_table_id`, `grain`, and optionally
    `measure_column_id`/`default_time_column_id` (absent or `None` for both
    is valid -- e.g. a `COUNT` metric with no measure column). All identity
    fields are coerced through `str()` so callers may pass UUID objects or
    strings interchangeably, the same latitude `aida.semantic_diff` snapshots
    already take.
    """
    measure_column_id = snapshot.get("measure_column_id")
    default_time_column_id = snapshot.get("default_time_column_id")
    grain_raw = str(snapshot["grain"])
    return MetricFormulaSignature(
        metric_version_id=str(snapshot["metric_version_id"]),
        metric_id=str(snapshot["metric_id"]),
        metric_name=str(snapshot.get("metric_name", "")),
        aggregation=str(snapshot["aggregation"]).strip().upper(),
        source_table_id=str(snapshot["source_table_id"]),
        measure_column_id=str(measure_column_id) if measure_column_id else None,
        default_time_column_id=(
            str(default_time_column_id) if default_time_column_id else None
        ),
        grain_raw=grain_raw,
        grain_normalized=grain_raw.strip().casefold(),
    )


def _formula_key(
    signature: MetricFormulaSignature, *, use_normalized_grain: bool
) -> tuple[str, str, str | None, str | None, str]:
    return (
        signature.aggregation,
        signature.source_table_id,
        signature.measure_column_id,
        signature.default_time_column_id,
        signature.grain_normalized if use_normalized_grain else signature.grain_raw,
    )


def compare_formulas(
    left: MetricFormulaSignature, right: MetricFormulaSignature
) -> MatchKind | None:
    """Classify how (if at all) two metric-version signatures collide.

    Returns `None` when the two signatures belong to the same metric (not a
    collision -- that is just version history) or compute genuinely
    different things. See the module docstring for exactly what
    `EXACT_MATCH`/`NORMALIZED_GRAIN_MATCH` do and do not cover.
    """
    if left.metric_id == right.metric_id:
        return None
    if _formula_key(left, use_normalized_grain=False) == _formula_key(
        right, use_normalized_grain=False
    ):
        return "EXACT_MATCH"
    if _formula_key(left, use_normalized_grain=True) == _formula_key(
        right, use_normalized_grain=True
    ):
        return "NORMALIZED_GRAIN_MATCH"
    return None


def find_formula_collisions(
    snapshots: Sequence[Mapping[str, Any]],
) -> list[FormulaCollision]:
    """Pairwise-scan a batch of metric-version snapshots for formula collisions.

    One snapshot per metric is expected (the caller's job, mirroring
    `detect_glossary_conflicts`, is to pass the currently `PUBLISHED` version
    of each metric) -- if several versions of the same metric are passed
    anyway, `compare_formulas` still never reports a same-`metric_id` pair as
    a collision. At most one collision is reported per unordered
    `(metric_id, metric_id)` pair even if more than two metrics share a
    formula (an N-way collision becomes `N choose 2` pairwise entries here;
    the caller dedupes against already-open conflicts the same way
    `detect_glossary_conflicts` does).
    """
    signatures = [normalize_metric_formula(snapshot) for snapshot in snapshots]
    collisions: list[FormulaCollision] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, left in enumerate(signatures):
        for right in signatures[i + 1 :]:
            match = compare_formulas(left, right)
            if match is None:
                continue
            pair_key = (
                (left.metric_id, right.metric_id)
                if left.metric_id < right.metric_id
                else (right.metric_id, left.metric_id)
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            collisions.append(FormulaCollision(match_kind=match, left=left, right=right))
    return collisions
