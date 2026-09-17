"""profiling -- PRIVATE. The value-free facets derived when a profile is persisted.

R11-FP04 (review 2026-09-16 F06.2). FP-04 is deliberately split: the design
authority (`Docs/10-architecture/20-database-footprint-and-agent-context.md`
§16.3) says to extend bounded profile operations with "uniqueness, nulls,
distributions, units/pattern evidence and observation scope" and to
"**separately** design governed sample-row requests", and adds that FP-04
"must remain useful when row sampling is disabled". This module is the
aggregate half. Nothing here reads, returns or derives a source value: every
function takes counts and answers a ratio, a class or a boolean.

**Why a module rather than a property.** Two rules about column uniqueness
were re-derived, differently, in two places:

* `aida.relationship_validation.ColumnFacts.profiled_unique` compared the
  approximate distinct count against 98% of the *non-null* count, and the
  answer decides whether a proposed join's uniqueness evidence counts at all;
* `aida.composite_key_inference._distinct_ratio` divided the same distinct
  count by the *sampled row* count, so the same column could read as unique to
  one consumer and not the other -- by exactly the null rate, silently.

Both now read one stored facet computed once, here, at write time. That also
means a reader no longer needs the counts to answer "is this column unique",
which is what lets a withheld-facet read still be truthful about the shape it
can show (see `aida.api.get_latest_table_profile`).

The threshold stays where it was -- `approximate_distinct_count` is
approximate by construction (`ColumnProfile`), so an exactly-unique column
routinely undercounts by a hair and a hard `distinct == non_null` test would
call it non-unique.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aida.connectors.base import (
    FACET_REASON_CODES,
    FACET_REASON_UNRECORDED,
    FACET_STATUSES,
    LENGTH_BUCKET_SCHEME,
    OBSERVATION_SCOPES,
    PROFILE_FACETS,
    ColumnProfileSnapshot,
    ProfileFacetStatus,
)

#: A column whose distinct count reaches this share of its non-null count is
#: treated as unique. Moved here from `aida.relationship_validation`, which
#: keeps importing it under its own name so no caller changed.
PROFILED_UNIQUE_MIN_RATIO = 0.98

#: What a read puts where a facet would have been when policy withholds it.
#:
#: The same shape `column_description_model.WITHHELD` and
#: `context_product_coverage._screened` already use: a visible marker plus a
#: count, never a silent omission. A profile that quietly dropped the facets it
#: was not allowed to serve would be indistinguishable from one whose engine
#: could not compute them -- and the reader would draw a conclusion about the
#: data from a fact about their own permissions.
WITHHELD_BY_POLICY = "[withheld by policy]"

#: The column is entirely null, so it has no distribution to classify.
CARDINALITY_EMPTY = "EMPTY"
#: Every non-null row carries the same single value.
CARDINALITY_CONSTANT = "CONSTANT"
#: Few enough distinct values to behave like a code list, whatever the row count.
CARDINALITY_CATEGORICAL = "CATEGORICAL"
#: Many rows per distinct value -- a poor key, a plausible group-by.
CARDINALITY_LOW_SELECTIVITY = "LOW_SELECTIVITY"
#: Few rows per distinct value, but not one.
CARDINALITY_HIGH_SELECTIVITY = "HIGH_SELECTIVITY"
#: Effectively one row per value: `PROFILED_UNIQUE_MIN_RATIO` or better.
CARDINALITY_UNIQUE = "UNIQUE"

CARDINALITY_CLASSES = frozenset(
    {
        CARDINALITY_EMPTY,
        CARDINALITY_CONSTANT,
        CARDINALITY_CATEGORICAL,
        CARDINALITY_LOW_SELECTIVITY,
        CARDINALITY_HIGH_SELECTIVITY,
        CARDINALITY_UNIQUE,
    }
)

#: At or below this many distinct values a column reads as a code list
#: regardless of its ratio -- a country column in a billion-row table has a
#: near-zero distinct ratio and is still a code list, which is the fact a
#: planner and a reviewer both want.
CATEGORICAL_MAX_DISTINCT = 50

#: Below this distinct ratio a column has many rows per value.
LOW_SELECTIVITY_MAX_RATIO = 0.2


def distinct_ratio(
    *, non_null_count: int | None, approximate_distinct_count: int | None
) -> float | None:
    """Distinct values per non-null value, or None when there is nothing to divide.

    Clipped to 1.0 because the numerator is an approximation and can overshoot
    (the same clip `composite_key_inference` applied to its own copy).
    """
    if non_null_count is None or approximate_distinct_count is None:
        return None
    if non_null_count <= 0:
        return None
    return min(approximate_distinct_count / non_null_count, 1.0)


def effectively_unique(
    *, non_null_count: int | None, approximate_distinct_count: int | None
) -> bool | None:
    """The `PROFILED_UNIQUE_MIN_RATIO` rule, as one stored answer.

    None -- not False -- when the column is entirely null: "no non-null value
    repeats" and "there is nothing to repeat" are different facts, and a
    profile that answered False would let a join's uniqueness evidence be
    silently withdrawn by an empty column rather than reported as unknown.
    """
    ratio = distinct_ratio(
        non_null_count=non_null_count, approximate_distinct_count=approximate_distinct_count
    )
    if ratio is None:
        return None
    return ratio >= PROFILED_UNIQUE_MIN_RATIO


def cardinality_class(
    *, non_null_count: int | None, approximate_distinct_count: int | None
) -> str | None:
    """Which shape of distribution the counts describe.

    A class of a statistic, never a bucket of values: it names how the rows
    spread, and carries no boundary, label or exemplar drawn from the data
    (ADR-0014). The ladder is ordered most-specific-first so a constant column
    is never also reported as categorical.
    """
    if non_null_count is None or approximate_distinct_count is None:
        return None
    if non_null_count <= 0:
        return CARDINALITY_EMPTY
    if approximate_distinct_count <= 1:
        return CARDINALITY_CONSTANT
    ratio = min(approximate_distinct_count / non_null_count, 1.0)
    if ratio >= PROFILED_UNIQUE_MIN_RATIO:
        return CARDINALITY_UNIQUE
    if approximate_distinct_count <= CATEGORICAL_MAX_DISTINCT:
        return CARDINALITY_CATEGORICAL
    if ratio < LOW_SELECTIVITY_MAX_RATIO:
        return CARDINALITY_LOW_SELECTIVITY
    return CARDINALITY_HIGH_SELECTIVITY


def persistable_facet_status(statuses: Iterable[ProfileFacetStatus]) -> list[dict[str, str]]:
    """Render facet statuses for `ColumnProfile.unavailable_facets`, closed-vocabulary only.

    INV-6. A connector's facet status reaches this function having passed
    through driver code, and the tempting implementation of an unavailable
    reason is to forward whatever the driver said -- which routinely quotes the
    offending row (`workflows.activities`' `analysis_run.error_message` rule is
    the same rule). So nothing outside `PROFILE_FACETS` / `FACET_STATUSES` is
    persisted at all, and a reason outside `FACET_REASON_CODES` is replaced by
    `UNRECORDED`: the fact that the facet is missing survives, the unvetted
    text does not.

    Returns `[]` rather than None for "every facet was available", so a caller
    can tell an empty list from a profile written before this facet existed
    (which stores SQL NULL).
    """
    rendered: list[dict[str, str]] = []
    for status in statuses:
        if status.facet not in PROFILE_FACETS or status.status not in FACET_STATUSES:
            # An unknown facet or status is not a missing facet the platform can
            # describe; dropping it is the fail-closed choice, and the connector
            # test suite is where a typo here is meant to be caught.
            continue
        reason = (
            status.reason_code
            if status.reason_code in FACET_REASON_CODES
            else FACET_REASON_UNRECORDED
        )
        rendered.append({"facet": status.facet, "status": status.status, "reason_code": reason})
    return rendered


def persistable_observation_scope(scope: Any) -> str | None:
    """`TableProfile.observation_scope`, admitted only from the shared vocabulary.

    INV-6 applies to this column for the same reason it applies to a facet's
    reason code: it is a free-form `String(20)` that a connector fills in, and a
    connector is the one place in this system where a source driver's own text
    is in scope. A scope outside `OBSERVATION_SCOPES` is also the one value that
    would silently restore the defect this facet exists to fix --
    `ProfileBounds.scope` falls back to the old `sampled >= estimate`
    derivation for anything it does not recognise -- so storing NULL, which
    means "not recorded", is both the value-free answer and the one that reads
    correctly downstream.
    """
    return scope if scope in OBSERVATION_SCOPES else None


def derived_column_facets(snapshot: ColumnProfileSnapshot) -> dict[str, Any]:
    """Every R11-FP04 `ColumnProfile` facet column, as keyword arguments.

    One function so both writers -- `profile_datasource` (the single-activity
    path) and `profile_table_task` (the fan-out path) -- persist the same
    facets from the same counts. They already drifted once on this table: the
    value-bearing artifact is written by only one of the two, which is correct
    but was not obvious, and a second hand-rolled facet block here would have
    made a divergence between the two paths invisible.

    Note what this does *not* do: it never looks at a value, because a
    `ColumnProfileSnapshot` has none to look at. `length_bucket_scheme` is set
    from the code constant precisely when there are counts to interpret, so a
    reader can never be handed counts without knowing which boundaries produced
    them -- and never the boundaries themselves (ADR-0014).
    """
    return {
        "distinct_ratio": distinct_ratio(
            non_null_count=snapshot.non_null_count,
            approximate_distinct_count=snapshot.approximate_distinct_count,
        ),
        "effectively_unique": effectively_unique(
            non_null_count=snapshot.non_null_count,
            approximate_distinct_count=snapshot.approximate_distinct_count,
        ),
        "cardinality_class": cardinality_class(
            non_null_count=snapshot.non_null_count,
            approximate_distinct_count=snapshot.approximate_distinct_count,
        ),
        "blank_count": snapshot.blank_count,
        "whitespace_only_count": snapshot.whitespace_only_count,
        "length_bucket_scheme": (
            LENGTH_BUCKET_SCHEME if snapshot.length_bucket_counts is not None else None
        ),
        "length_bucket_counts": (
            None
            if snapshot.length_bucket_counts is None
            else [int(count) for count in snapshot.length_bucket_counts]
        ),
        "frequency_entropy_bits": snapshot.frequency_entropy_bits,
        "unavailable_facets": persistable_facet_status(snapshot.facet_status),
    }


def facet_status_from_stored(stored: Any) -> list[dict[str, str]]:
    """Read `ColumnProfile.unavailable_facets` back, dropping anything unrecognised.

    Defensive on read as well as on write: this column is JSON, so a row
    written by an older revision, by a hand-fix or by a future one cannot be
    assumed to match the vocabulary. Same fail-closed rule as the writer --
    unknown entries vanish rather than reaching an API response.
    """
    if not isinstance(stored, list):
        return []
    rendered: list[dict[str, str]] = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        facet = entry.get("facet")
        status = entry.get("status")
        reason = entry.get("reason_code")
        if facet not in PROFILE_FACETS or status not in FACET_STATUSES:
            continue
        rendered.append(
            {
                "facet": str(facet),
                "status": str(status),
                "reason_code": (
                    str(reason) if reason in FACET_REASON_CODES else FACET_REASON_UNRECORDED
                ),
            }
        )
    return rendered
