"""PR-1: composite key inference -- evidence-backed, review-gated candidate keys.

This module implements the *inference* half as small, pure functions over
already-computed profiling evidence (the `TableProfile` / `ColumnProfile`
shape) with no session or I/O -- the same division `aida.catalog_bulk_actions`
(CT-1) draws between a pure planning module and its API layer. The API layer
(`aida.composite_key_api`) does the bounded database fetch and persists
whatever this module proposes as `PENDING` `CompositeKeyCandidate` rows for a
human reviewer to accept or reject, mirroring the maker-checker discipline
`aida.intelligence_api.decide_relationship_candidate` already established for
`RelationshipCandidate`.

There is no live connector access in this environment (no source credentials
for any datasource), so this cannot execute `SELECT COUNT(DISTINCT (a, b))`
against the real data to *measure* a composite key's true joint selectivity.
It can only reason from the *independent* single-column statistics already
captured by `ColumnProfile` -- null rate and approximate distinct count --
and from that produce a bounded, capped-confidence, human-reviewable guess.

That is precisely why every candidate here carries a full evidence blob and a
conservative confidence ceiling well below where a corroborated signal like
`RelationshipCandidate` might land, and why review is not optional: two
columns can each be 90% distinct on their own and still be entirely
redundant together (e.g. one derived from the other, or both derived from a
shared upstream key) -- their combination would then be no more distinct
than either column alone, yet this heuristic, having no way to observe that
correlation, would still score it near the top. Treat `confidence` here as an
optimistic upper bound on plausibility, not a measurement: a low minimum
member ratio rules a combination out (the combination cannot be more
distinct than its least-distinct member), but a high minimum only means
"plausible", never "confirmed".

Single-column keys are simply the size-1 case of the same search -- there is
no separate code path for them.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any
from uuid import UUID

# --- Bounds --------------------------------------------------------------
# Every constant below exists to keep this heuristic's search space bounded
# and explicit, in the spirit of `dbt_artifacts.MAX_RESOURCES` / `MAX_EDGES`
# -- this can never become an unbounded combinatorial scan over a wide table.

# A candidate key needing more columns than this is not something this
# heuristic attempts: past 3 columns the odds of a *coincidental* match among
# independently-profiled columns rise fast, while genuine composite keys
# wider than 3 columns are uncommon in practice.
MAX_KEY_SIZE = 3

# Only the top-N eligible columns (ranked by individual distinct ratio) are
# considered as candidate key *members* at all. This is the primary
# combinatorial guard: with N members and a key-size cap of 3, the largest
# possible search is C(N,1) + C(N,2) + C(N,3) combinations -- for N=8 that is
# 8 + 28 + 56 = 92, comfortably inside MAX_COMBINATIONS_EVALUATED below even
# before that second cap does anything on its own.
MAX_CANDIDATE_MEMBERS = 8

# Hard ceiling on how many column combinations are actually scored, enforced
# independently of MAX_CANDIDATE_MEMBERS so a pathologically wide table can
# never turn this into an unbounded blow-up even if the member pool were
# larger for some future reason.
MAX_COMBINATIONS_EVALUATED = 200

# At most this many candidates are returned (highest confidence first) -- a
# reviewer should see a short, ranked shortlist, not every combination that
# happened to clear the bar.
MAX_CANDIDATES_RETURNED = 20

# A key member's own profile must show no more than this null rate. Profiling
# is sampled and approximate (see `ColumnProfile`), so a hard
# `null_count == 0` would be brittle; 1% leaves room for sampling noise while
# still ruling out any column with a real population of nulls, which a
# genuine key column would never have.
MAX_MEMBER_NULL_RATE = 0.01

# A key member's own profile must show at least this much distinct-value
# ratio (approximate_distinct_count / sampled_row_count). Below this, a
# column is dominated by a handful of repeated values -- a poor key member,
# and one that would otherwise inflate the combination count for no
# plausible benefit.
MIN_MEMBER_DISTINCT_RATIO = 0.9

# Each additional member column beyond the first multiplies the confidence by
# this factor. A wider composite key is a strictly more specific -- and so a
# priori less likely -- claim than a narrower one built from the same
# evidence; this reflects that preference for parsimony without requiring a
# second, unavailable signal.
PER_EXTRA_MEMBER_DISCOUNT = 0.97

# This is a derived, unmeasured heuristic, never a corroborated match: the
# ceiling below is well below anywhere `RelationshipCandidate` (backed by an
# actual name/type/value-shape match) might reach, so nothing downstream can
# mistake a heuristic guess for a certified key. It is used as a *scaling*
# factor (see `infer_composite_key_candidates`), not a clamp: member columns
# must already clear MIN_MEMBER_DISTINCT_RATIO (0.9) to be eligible at all,
# which is itself above this ceiling, so a clamp would make every candidate
# saturate at the same score and destroy the ranking between them. Scaling
# instead means confidence rises smoothly from 0 towards this ceiling as
# evidence strengthens (higher member distinct ratios, fewer members), and
# can never exceed it.
MAX_CONFIDENCE = 0.6

DETECTION_RULE_PREFIX = "composite_key_min_distinct_ratio_v1"


@dataclass(frozen=True)
class ColumnKeyEvidence:
    """Value-free, per-column profiling evidence -- the `ColumnProfile` shape."""

    column_id: UUID
    column_name: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int


@dataclass(frozen=True)
class CompositeKeyCandidateProposal:
    """One scored candidate key, ready to persist as a PENDING CompositeKeyCandidate."""

    column_ids: tuple[UUID, ...]
    column_names: tuple[str, ...]
    confidence: float
    detection_rule: str
    evidence: dict[str, Any]


def _distinct_ratio(evidence: ColumnKeyEvidence, sampled_row_count: int) -> float:
    if sampled_row_count <= 0:
        return 0.0
    # approximate_distinct_count is an approximation (see ColumnProfile) and can
    # slightly overshoot sampled_row_count; clip to a valid ratio.
    return min(evidence.approximate_distinct_count / sampled_row_count, 1.0)


def _null_rate(evidence: ColumnKeyEvidence, sampled_row_count: int) -> float:
    if sampled_row_count <= 0:
        return 1.0
    return evidence.null_count / sampled_row_count


def infer_composite_key_candidates(
    *,
    columns: Sequence[ColumnKeyEvidence],
    sampled_row_count: int,
    row_count_estimate: int | None,
    declared_key_column_ids: Collection[UUID] = (),
) -> list[CompositeKeyCandidateProposal]:
    """Propose bounded, scored composite key candidates from independent column stats.

    ``columns`` should be every profiled column of one table (i.e. every
    ``ColumnProfile`` row for its latest ``TableProfile``); ``sampled_row_count``
    and ``row_count_estimate`` are that ``TableProfile``'s context, carried
    into each proposal's evidence so a reviewer can see what the ratios below
    were computed against without re-querying anything.

    ``declared_key_column_ids`` should contain every column already covered
    by a ``MetadataConstraint`` of type ``PRIMARY_KEY`` or ``UNIQUE``. Any
    such column is dropped from the candidate pool entirely -- it forms no
    *undeclared* key regardless of which other columns it might be grouped
    with, and the point of this heuristic is to surface undeclared candidates,
    not to restate or extend what is already known.

    Returns an empty list if there is no profiling evidence at all
    (``sampled_row_count <= 0`` or no columns), rather than fabricating
    candidates with nothing behind them.
    """
    if sampled_row_count <= 0 or not columns:
        return []

    declared = set(declared_key_column_ids)
    eligible: list[tuple[ColumnKeyEvidence, float]] = []
    for column in columns:
        if column.column_id in declared:
            continue
        if _null_rate(column, sampled_row_count) > MAX_MEMBER_NULL_RATE:
            continue
        ratio = _distinct_ratio(column, sampled_row_count)
        if ratio < MIN_MEMBER_DISTINCT_RATIO:
            continue
        eligible.append((column, ratio))

    if not eligible:
        return []

    # Rank by individual distinct ratio and keep only the most promising
    # members -- the combinatorial guard described above.
    eligible.sort(key=lambda pair: pair[1], reverse=True)
    pool = eligible[:MAX_CANDIDATE_MEMBERS]

    proposals: list[CompositeKeyCandidateProposal] = []
    evaluated = 0
    max_size = min(MAX_KEY_SIZE, len(pool))
    for size in range(1, max_size + 1):
        if evaluated >= MAX_COMBINATIONS_EVALUATED:
            break
        size_discount = PER_EXTRA_MEMBER_DISCOUNT ** (size - 1)
        for combo in combinations(pool, size):
            if evaluated >= MAX_COMBINATIONS_EVALUATED:
                break
            evaluated += 1
            min_ratio = min(ratio for _, ratio in combo)
            # Scaled by the ceiling, not clamped to it (see MAX_CONFIDENCE) --
            # both factors are already <= 1.0 so this can never exceed the
            # ceiling, and it varies smoothly with the underlying evidence.
            confidence = round(MAX_CONFIDENCE * min_ratio * size_discount, 4)
            evidence: dict[str, Any] = {
                "algorithm": DETECTION_RULE_PREFIX,
                "sampled_row_count": sampled_row_count,
                "row_count_estimate": row_count_estimate,
                "min_member_distinct_ratio": min_ratio,
                "size_discount_applied": size_discount,
                "columns": [
                    {
                        "column_id": str(member.column_id),
                        "column_name": member.column_name,
                        "null_count": member.null_count,
                        "non_null_count": member.non_null_count,
                        "approximate_distinct_count": member.approximate_distinct_count,
                        "distinct_ratio": ratio,
                    }
                    for member, ratio in combo
                ],
            }
            proposals.append(
                CompositeKeyCandidateProposal(
                    column_ids=tuple(member.column_id for member, _ in combo),
                    column_names=tuple(member.column_name for member, _ in combo),
                    confidence=confidence,
                    detection_rule=f"{DETECTION_RULE_PREFIX}_size_{size}",
                    evidence=evidence,
                )
            )

    proposals.sort(key=lambda proposal: proposal.confidence, reverse=True)
    return proposals[:MAX_CANDIDATES_RETURNED]
