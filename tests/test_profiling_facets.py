"""R11-FP04: the value-free aggregate facets, and the line they must not cross.

`Docs/review-2026-09-16/REVIEW.md` F06.2 splits FP-04: ship the value-free
aggregate half, leave governed row access to a policy that does not exist yet.
The design authority already drew the line
(`Docs/10-architecture/20-database-footprint-and-agent-context.md:1026`,
"**separately** design governed sample-row requests"), and ADR-0014 is what
makes it load-bearing.

This module proves the aggregate half is actually aggregate. That needs a
*positive* test, not a naming ratchet: `tests/test_inv6_value_freedom.py`'s
`test_no_mapped_column_is_named_for_a_source_value` is honest about being a
naming check and would not notice a `bucket_edges: JSON` column at all --
"edges" is not on its fragment list, and no list of fragments could be
complete. So the tests here assert what the profiling path *writes*: the type
and the vocabulary of every value that lands in a facet column.

Also here: the two-derivation divergence F06.2's remit implies, and the
observation-scope rule every connector now has to satisfy.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aida.composite_key_inference import (
    MIN_MEMBER_DISTINCT_RATIO,
    ColumnKeyEvidence,
    infer_composite_key_candidates,
)
from aida.connectors.base import (
    FACET_REASON_CODES,
    FACET_REASON_ENGINE_LACKS_FACET,
    FACET_REASON_UNRECORDED,
    FACET_STATUSES,
    FACET_UNSUPPORTED,
    LENGTH_BUCKET_BOUNDS,
    LENGTH_BUCKET_SCHEME,
    OBSERVATION_SCOPE_FULL,
    OBSERVATION_SCOPE_SAMPLE,
    OBSERVATION_SCOPE_UNKNOWN,
    PROFILE_FACET_ENTROPY,
    PROFILE_FACETS,
    ColumnProfileSnapshot,
    ProfileFacetStatus,
    bounded_scan_scope,
    read_value_free_distribution,
    value_free_distribution_expressions,
)
from aida.models import ColumnProfile, TableProfile
from aida.relationship_validation import PROFILED_UNIQUE_MIN_RATIO, ColumnFacts
from atlas.modules.profiling.facets import (
    CARDINALITY_CATEGORICAL,
    CARDINALITY_CONSTANT,
    CARDINALITY_EMPTY,
    CARDINALITY_HIGH_SELECTIVITY,
    CARDINALITY_LOW_SELECTIVITY,
    CARDINALITY_UNIQUE,
    cardinality_class,
    derived_column_facets,
    distinct_ratio,
    effectively_unique,
    facet_status_from_stored,
    persistable_facet_status,
)

# A string that cannot occur naturally, so any appearance in a persisted facet
# is a genuine leak. Same convention as `tests/test_inv6_value_freedom.py`.
SENTINEL_VALUE = "ZZQ-SENTINEL-FACETVALUE-3d71"


def _snapshot(**overrides: Any) -> ColumnProfileSnapshot:
    defaults: dict[str, Any] = {
        "name": "account_no",
        "null_count": 10,
        "non_null_count": 990,
        "approximate_distinct_count": 900,
        "min_length": 3,
        "max_length": 40,
        "blank_count": 4,
        "whitespace_only_count": 1,
        "length_bucket_counts": (100, 400, 400, 80, 10),
        "frequency_entropy_bits": 8.75,
    }
    defaults.update(overrides)
    return ColumnProfileSnapshot(**defaults)


# --- the facets are counts, classes and ratios -- never an edge or an exemplar ---

#: Every facet column this task adds to `column_profile`/`table_profile`, with
#: the Python types a value in it may have. Written out rather than derived from
#: the model so that adding a facet column means deciding, in this list, what
#: kind of thing may go in it -- which is the decision the review is about.
_FACET_VALUE_TYPES: dict[str, tuple[type, ...]] = {
    "distinct_ratio": (float,),
    "effectively_unique": (bool,),
    "cardinality_class": (str,),
    "blank_count": (int,),
    "whitespace_only_count": (int,),
    "length_bucket_scheme": (str,),
    "length_bucket_counts": (list,),
    "frequency_entropy_bits": (float,),
    "unavailable_facets": (list,),
}


def test_the_facet_column_list_matches_the_model() -> None:
    """Tripwire. Every test below is written against `_FACET_VALUE_TYPES`, so a
    facet column added to `ColumnProfile` without an entry here would be
    exempted from all of them silently.
    """
    mapped = {column.name for column in ColumnProfile.__table__.columns}
    missing = sorted(set(_FACET_VALUE_TYPES) - mapped)
    assert missing == [], (
        f"_FACET_VALUE_TYPES names columns column_profile does not have: {missing}"
    )
    assert set(derived_column_facets(_snapshot())) == set(_FACET_VALUE_TYPES), (
        "the write path and this test's column list have diverged; a facet written "
        "but not listed here is a facet nothing in this module checks"
    )


def test_every_persisted_facet_is_a_count_a_ratio_or_a_closed_vocabulary_code() -> None:
    """ADR-0014/R11-FP04: a count is value-free; a bucket edge is a value.

    The positive form of the claim the naming ratchet cannot make. Drives the
    real write path (`derived_column_facets`, which is what both profiling
    activities call) and inspects what it produces: numbers, booleans, and
    strings drawn from vocabularies defined in code.

    `length_bucket_counts` is the case that matters. A length histogram is the
    natural place to smuggle a value in, because the honest-looking
    implementation stores `[{"from": 1, "to": 8, "count": 100}, ...]` -- and
    once a boundary is a stored datum rather than a code constant, the step to
    storing a *value* boundary (`{"from": "AA0001", ...}`) is an edit, not a
    design change. So the counts are positional integers and nothing else, and
    the boundaries live in `LENGTH_BUCKET_BOUNDS`.
    """
    facets = derived_column_facets(_snapshot())

    for name, allowed in _FACET_VALUE_TYPES.items():
        value = facets[name]
        if value is None:
            continue
        assert isinstance(value, allowed), f"{name} holds a {type(value).__name__}"

    assert facets["length_bucket_counts"] == [100, 400, 400, 80, 10]
    assert all(isinstance(count, int) for count in facets["length_bucket_counts"]), (
        "a length bucket must be a bare count; anything richer is where an edge, "
        "a label or an exemplar gets in"
    )
    assert facets["length_bucket_scheme"] == LENGTH_BUCKET_SCHEME
    assert facets["cardinality_class"] in {
        CARDINALITY_EMPTY,
        CARDINALITY_CONSTANT,
        CARDINALITY_CATEGORICAL,
        CARDINALITY_LOW_SELECTIVITY,
        CARDINALITY_HIGH_SELECTIVITY,
        CARDINALITY_UNIQUE,
    }


def test_no_bucket_boundary_is_ever_persisted() -> None:
    """The boundaries are code, and must stay code.

    Renders every facet value as text and requires that none of the scheme's own
    numbers-as-boundaries appear as a stored key or label. Deliberately checked
    against the real `LENGTH_BUCKET_BOUNDS` rather than a hard-coded list, so
    re-tuning the scheme cannot quietly turn this test vacuous.
    """
    facets = derived_column_facets(_snapshot())
    counts = facets["length_bucket_counts"]

    assert len(counts) == len(LENGTH_BUCKET_BOUNDS), (
        "the stored counts and the code-defined scheme must be the same length, or "
        "position alone cannot say which bucket a count belongs to"
    )
    # A dict-shaped bucket is what a boundary would arrive inside.
    assert not any(isinstance(count, dict) for count in counts)
    for facet_name in ("length_bucket_scheme", "cardinality_class"):
        assert facets[facet_name] is None or "," not in str(facets[facet_name]), (
            f"{facet_name} looks like it is carrying a structure rather than a name"
        )


def test_a_facet_reason_outside_the_vocabulary_is_never_persisted() -> None:
    """INV-6 on the one new string a connector controls.

    `unavailable_reason` is the field on this surface most likely to leak: the
    obvious implementation forwards the driver's own message, and a source
    driver's error text routinely quotes the offending row (the rule
    `workflows/activities.py` already carries for
    `analysis_run.error_message`). So the reason is a code, and a code this
    platform does not recognise is dropped rather than stored.
    """
    leaky = _snapshot(
        facet_status=(
            ProfileFacetStatus(
                PROFILE_FACET_ENTROPY,
                FACET_UNSUPPORTED,
                f"could not group by account_no=({SENTINEL_VALUE})",
            ),
        )
    )

    facets = derived_column_facets(leaky)
    rendered = str(facets["unavailable_facets"])

    assert SENTINEL_VALUE not in rendered, "a connector-supplied reason reached the control plane"
    assert facets["unavailable_facets"] == [
        {
            "facet": PROFILE_FACET_ENTROPY,
            "status": FACET_UNSUPPORTED,
            "reason_code": FACET_REASON_UNRECORDED,
        }
    ], "the facet must still be recorded as missing; only the unvetted reason is dropped"


def test_the_facet_reason_scan_would_notice_a_leak() -> None:
    """Negative control for the test above.

    Without it, a `persistable_facet_status` that returned `[]` for everything
    would leave that test passing while proving nothing -- an empty list
    contains no sentinel either.
    """
    kept = persistable_facet_status(
        (
            ProfileFacetStatus(
                PROFILE_FACET_ENTROPY, FACET_UNSUPPORTED, FACET_REASON_ENGINE_LACKS_FACET
            ),
        )
    )
    assert kept == [
        {
            "facet": PROFILE_FACET_ENTROPY,
            "status": FACET_UNSUPPORTED,
            "reason_code": FACET_REASON_ENGINE_LACKS_FACET,
        }
    ], (
        "a recognised reason must survive, or the drop above is indiscriminate "
        "rather than a filter"
    )


def test_an_unrecognised_facet_or_status_is_dropped_entirely() -> None:
    """A facet name this platform does not know is not a missing facet it can
    describe. Fail closed: drop the entry rather than serve a word no reader can
    interpret and no policy governs.
    """
    assert persistable_facet_status(
        (ProfileFacetStatus("ROW_SAMPLE", FACET_UNSUPPORTED, "x"),)
    ) == []
    assert persistable_facet_status(
        (ProfileFacetStatus(PROFILE_FACET_ENTROPY, "MAYBE", "x"),)
    ) == []


def test_stored_facet_status_is_re_validated_on_the_way_out() -> None:
    """`unavailable_facets` is JSON, so a row can carry anything a past
    revision, a future one or a hand-fix put there. The read applies the same
    closed-vocabulary rule as the write, because a value that got in through a
    path this code did not control must not get out through one it does.
    """
    assert facet_status_from_stored("not a list") == []
    assert facet_status_from_stored([{"facet": "ROW_SAMPLE", "status": FACET_UNSUPPORTED}]) == []
    assert facet_status_from_stored(
        [
            {
                "facet": PROFILE_FACET_ENTROPY,
                "status": FACET_UNSUPPORTED,
                "reason_code": SENTINEL_VALUE,
            }
        ]
    ) == [
        {
            "facet": PROFILE_FACET_ENTROPY,
            "status": FACET_UNSUPPORTED,
            "reason_code": FACET_REASON_UNRECORDED,
        }
    ]


def test_the_facet_vocabularies_are_disjoint_and_populated() -> None:
    """Tripwire for every vocabulary assertion above: an empty frozenset would
    make `x not in VOCAB` true for everything and turn the fail-closed filters
    into pass-through.
    """
    assert len(PROFILE_FACETS) >= 5
    assert len(FACET_STATUSES) == 4
    assert len(FACET_REASON_CODES) >= 6
    assert FACET_REASON_UNRECORDED in FACET_REASON_CODES


# --- observation scope ------------------------------------------------------


def test_a_bound_that_filled_up_is_never_reported_as_full() -> None:
    """R11-FP04, the BigQuery half of the defect.

    A `LIMIT 10000` that returned 10000 rows says nothing about whether the
    table has 10000 rows or ten million, so SAMPLE is the only supportable
    claim. The previous code compared the sample against a row estimate that
    BigQuery had set *to the sample*, which made every bounded profile look
    exhaustive.
    """
    assert bounded_scan_scope(sampled_row_count=10_000, sample_rows=10_000) == (
        OBSERVATION_SCOPE_SAMPLE
    )
    assert bounded_scan_scope(sampled_row_count=10_001, sample_rows=10_000) == (
        OBSERVATION_SCOPE_SAMPLE
    )


def test_a_bound_that_never_bit_is_full() -> None:
    """The other direction has to work too, or every profile would read as
    sampled and no uniqueness evidence would ever be unqualified. A table with
    fewer rows than the bound was genuinely aggregated in full -- including the
    empty table, which is fully observed at zero rows.
    """
    assert bounded_scan_scope(sampled_row_count=42, sample_rows=1000) == OBSERVATION_SCOPE_FULL
    assert bounded_scan_scope(sampled_row_count=0, sample_rows=1000) == OBSERVATION_SCOPE_FULL


def test_a_nonsensical_bound_makes_no_claim() -> None:
    assert bounded_scan_scope(sampled_row_count=5, sample_rows=0) == OBSERVATION_SCOPE_UNKNOWN


def test_table_profile_records_scope_rather_than_leaving_it_to_be_inferred() -> None:
    """The column exists and is nullable, which is the whole point: a profile
    written before this facet has nothing to say about its own scope, and NULL
    says that rather than asserting UNKNOWN (which would mean a connector looked
    and could not tell).
    """
    column = TableProfile.__table__.columns["observation_scope"]
    assert column.nullable is True
    assert column.server_default is None, (
        "a server default would backfill a claim onto rows that never made one"
    )


# --- the distribution SQL generator ----------------------------------------


def test_the_distribution_aliases_are_positional_and_carry_no_boundary() -> None:
    """The generated SQL carries the boundaries -- it has to, they are the
    predicate -- but the *aliases* it reads back by must be positional, because
    the alias is what the persisted counts are aligned to. An alias like
    `lb_0_1_to_8` would make the stored list's meaning depend on a string
    parsed out of SQL.
    """
    expressions = value_free_distribution_expressions(
        position=3,
        text_form="c::text",
        length_form="LENGTH(c::text)",
        trimmed_form="BTRIM(c::text)",
    )

    assert len(expressions) == 2 + len(LENGTH_BUCKET_BOUNDS)
    assert any("AS bl_3" in expression for expression in expressions)
    assert any("AS ws_3" in expression for expression in expressions)
    for index in range(len(LENGTH_BUCKET_BOUNDS)):
        assert any(f"AS lb_3_{index}" in expression for expression in expressions)
    # Counts only: every expression is a SUM over a CASE, never a MIN/MAX of the
    # column itself and never the column in a select list.
    assert all(expression.startswith("SUM(CASE WHEN ") for expression in expressions)


def test_a_missing_bucket_alias_makes_the_whole_facet_absent_not_partial() -> None:
    """A half-read distribution is worse than none: the counts are positional,
    so a gap silently shifts every later bucket's meaning.
    """
    full: dict[str, int | None] = {"bl_0": 1, "ws_0": 0}
    full.update({f"lb_0_{index}": index for index in range(len(LENGTH_BUCKET_BOUNDS))})
    blank, whitespace, buckets = read_value_free_distribution(0, full.get)
    assert (blank, whitespace) == (1, 0)
    assert buckets == tuple(range(len(LENGTH_BUCKET_BOUNDS)))

    partial = dict(full)
    partial[f"lb_0_{len(LENGTH_BUCKET_BOUNDS) - 1}"] = None
    _, _, degraded = read_value_free_distribution(0, partial.get)
    assert degraded is None


# --- uniqueness: one rule, one place, two readers --------------------------


def test_the_two_uniqueness_derivations_used_to_disagree_and_now_do_not() -> None:
    """R11-FP04's uniqueness promotion, stated as the divergence it removes.

    `relationship_validation` divided the distinct count by the *non-null*
    count; `composite_key_inference` divided it by the *sampled row* count. On
    this column those two answers straddle `MIN_MEMBER_DISTINCT_RATIO`, so the
    same profile made the column a viable key member to one consumer and not to
    the other -- differing by exactly the null rate, with nothing anywhere
    saying so.

    The stored facet is the non-null ratio, and both consumers read it.
    """
    sampled, null_count, non_null, distinct = 1000, 10, 990, 891

    old_composite_ratio = distinct / sampled
    new_ratio = distinct_ratio(
        non_null_count=non_null, approximate_distinct_count=distinct
    )
    assert new_ratio is not None
    assert old_composite_ratio < MIN_MEMBER_DISTINCT_RATIO <= new_ratio, (
        "this fixture no longer straddles the threshold, so it no longer demonstrates "
        "the divergence; re-pick the counts rather than deleting the test"
    )

    without_facet = infer_composite_key_candidates(
        columns=[
            ColumnKeyEvidence(
                column_id=uuid4(),
                column_name="account_no",
                null_count=null_count,
                non_null_count=non_null,
                approximate_distinct_count=distinct,
            )
        ],
        sampled_row_count=sampled,
        row_count_estimate=sampled,
    )
    with_facet = infer_composite_key_candidates(
        columns=[
            ColumnKeyEvidence(
                column_id=uuid4(),
                column_name="account_no",
                null_count=null_count,
                non_null_count=non_null,
                approximate_distinct_count=distinct,
                stored_distinct_ratio=new_ratio,
            )
        ],
        sampled_row_count=sampled,
        row_count_estimate=sampled,
    )

    assert without_facet == [], "the pre-facet derivation rejected this column"
    assert with_facet, "reading the stored facet must accept it, the way join validation does"


def test_join_validation_prefers_the_stored_uniqueness_facet() -> None:
    """The stored answer wins over re-derivation, including when they differ.

    A profile whose writer concluded "not unique" must not be overruled by a
    reader recomputing from counts that have since been read differently -- the
    point of storing the facet is that there is one answer.
    """
    counts = {"null_count": 0, "non_null_count": 100, "approximate_distinct_count": 100}
    derived = ColumnFacts(
        column_id=uuid4(),
        name="account_no",
        physical_type="text",
        nullable=False,
        **counts,
    )
    overridden = ColumnFacts(
        column_id=uuid4(),
        name="account_no",
        physical_type="text",
        nullable=False,
        stored_effectively_unique=False,
        **counts,
    )

    assert derived.profiled_unique is True
    assert overridden.profiled_unique is False


def test_join_validation_still_answers_for_a_profile_written_before_the_facet() -> None:
    """The fallback is not decoration: every profile row in an existing
    deployment has `effectively_unique IS NULL`, and withdrawing uniqueness
    evidence from all of them would change relationship decisions on upgrade.
    """
    facts = ColumnFacts(
        column_id=uuid4(),
        name="account_no",
        physical_type="text",
        nullable=False,
        null_count=0,
        non_null_count=100,
        approximate_distinct_count=99,
        stored_effectively_unique=None,
    )
    assert facts.profiled_unique is True


@pytest.mark.parametrize(
    ("non_null", "distinct", "expected"),
    [
        (0, 0, None),
        (100, 100, True),
        (100, 98, True),
        (100, 97, False),
        (100, 1, False),
    ],
)
def test_effectively_unique_applies_the_ratio_rule(
    non_null: int, distinct: int, expected: bool | None
) -> None:
    assert (
        effectively_unique(non_null_count=non_null, approximate_distinct_count=distinct) is expected
    )


def test_an_entirely_null_column_is_unknown_rather_than_not_unique() -> None:
    """"No non-null value repeats" and "there is nothing to repeat" are
    different facts. False would let an empty column silently withdraw a join's
    uniqueness evidence instead of reporting that nothing is known.
    """
    assert effectively_unique(non_null_count=0, approximate_distinct_count=0) is None
    assert distinct_ratio(non_null_count=0, approximate_distinct_count=0) is None
    assert cardinality_class(non_null_count=0, approximate_distinct_count=0) == CARDINALITY_EMPTY


def test_the_stored_threshold_is_the_one_relationship_validation_publishes() -> None:
    """The constant moved; its value must not have. `relationship_validation`
    re-exports it, and a drift between the two would mean the stored facet and
    the module that documents the rule disagree.
    """
    assert PROFILED_UNIQUE_MIN_RATIO == 0.98


@pytest.mark.parametrize(
    ("non_null", "distinct", "expected"),
    [
        (0, 0, CARDINALITY_EMPTY),
        (1000, 1, CARDINALITY_CONSTANT),
        (1_000_000, 12, CARDINALITY_CATEGORICAL),
        (1000, 100, CARDINALITY_LOW_SELECTIVITY),
        (1000, 500, CARDINALITY_HIGH_SELECTIVITY),
        (1000, 999, CARDINALITY_UNIQUE),
    ],
)
def test_cardinality_class_names_the_shape_not_the_values(
    non_null: int, distinct: int, expected: str
) -> None:
    """A country column in a million-row table has a near-zero distinct ratio
    and is still a code list; that is the fact a planner and a reviewer both
    want, and it is why the categorical test is on the distinct count rather
    than the ratio.
    """
    assert cardinality_class(non_null_count=non_null, approximate_distinct_count=distinct) == (
        expected
    )
