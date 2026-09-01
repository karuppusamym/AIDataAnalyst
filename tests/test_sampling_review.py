"""AT-14 pure-function tests: seeded, reproducible acceptance sampling over a
batch of pending drafted-prose review items. No database, no HTTP, no
network -- every property here is about `aida.sampling_review`'s draw
function alone, matching how the sibling GL-9 evidence-scoring pure
functions are tested (`tests/test_asset_description.py`).
"""

import random
from uuid import UUID, uuid4

from aida.sampling_review import (
    MAX_SEED,
    draw_reproducible_sample,
    generate_seed,
    resolve_sample_size,
)


def _batch(n: int) -> list[UUID]:
    return [uuid4() for _ in range(n)]


# --- resolve_sample_size -----------------------------------------------------


def test_resolve_sample_size_empty_batch_is_zero() -> None:
    assert resolve_sample_size(0, sample_size=10) == 0
    assert resolve_sample_size(0, sample_fraction=0.5) == 0


def test_resolve_sample_size_explicit_count_is_clamped_to_batch() -> None:
    assert resolve_sample_size(10, sample_size=3) == 3
    assert resolve_sample_size(10, sample_size=1000) == 10  # can't exceed batch size
    assert resolve_sample_size(10, sample_size=0) == 1  # never rounds a batch down to zero review


def test_resolve_sample_size_fraction_rounds_up_and_clamps() -> None:
    assert resolve_sample_size(100, sample_fraction=0.1) == 10
    assert resolve_sample_size(3, sample_fraction=0.1) == 1  # ceil(0.3) -> 1, never 0
    assert resolve_sample_size(10, sample_fraction=1.0) == 10


def test_resolve_sample_size_prefers_explicit_count_over_fraction() -> None:
    assert resolve_sample_size(100, sample_size=5, sample_fraction=0.9) == 5


def test_resolve_sample_size_defaults_to_whole_batch_when_neither_given() -> None:
    assert resolve_sample_size(50) == 50


# --- draw_reproducible_sample: the reproducibility contract itself ---------


def test_same_seed_and_batch_draws_the_identical_sample_every_time() -> None:
    batch = _batch(200)
    first = draw_reproducible_sample(batch, sample_size=20, seed=42)
    second = draw_reproducible_sample(batch, sample_size=20, seed=42)
    third = draw_reproducible_sample(list(batch), sample_size=20, seed=42)
    assert first == second == third
    assert len(first) == 20
    assert len(set(first)) == 20  # no duplicates
    assert set(first) <= set(batch)


def test_draw_is_independent_of_input_list_order() -> None:
    """'Same batch membership' means the same SET of ids -- the order the
    caller happened to list them in (e.g. from a DB query with no stable
    ORDER BY) must never change which ids get drawn."""
    batch = _batch(150)
    shuffled = list(batch)
    random.Random(7).shuffle(shuffled)  # noqa: S311 -- not cryptographic use
    assert batch != shuffled  # sanity: the shuffle actually changed order

    original_order_draw = draw_reproducible_sample(batch, sample_size=15, seed=99)
    shuffled_order_draw = draw_reproducible_sample(shuffled, sample_size=15, seed=99)
    assert original_order_draw == shuffled_order_draw


def test_a_fresh_random_random_is_used_per_call_not_a_shared_global_state() -> None:
    """Interleaving unrelated `random` module calls between two identical
    draws must not perturb either draw -- each call seeds its own
    `random.Random(seed)`, never consulting/advancing the global `random`
    module state."""
    batch = _batch(80)
    first = draw_reproducible_sample(batch, sample_size=10, seed=123)
    for _ in range(500):
        random.random()  # noqa: S311 -- perturb global random state, if it were (mis)used
    second = draw_reproducible_sample(batch, sample_size=10, seed=123)
    assert first == second


def test_different_seed_generally_draws_a_different_sample() -> None:
    batch = _batch(300)
    drawn_by_seed = {
        seed: tuple(draw_reproducible_sample(batch, sample_size=30, seed=seed))
        for seed in range(10)
    }
    # Not every pair of seeds is guaranteed to differ, but across 10 seeds
    # drawing 30-of-300 it would be exceptional for every draw to coincide --
    # assert at least one differs from the first.
    assert len({drawn_by_seed[0]} | {value for value in drawn_by_seed.values()}) > 1


def test_sample_size_at_or_above_batch_size_returns_the_whole_batch() -> None:
    batch = _batch(12)
    exact = draw_reproducible_sample(batch, sample_size=12, seed=1)
    over = draw_reproducible_sample(batch, sample_size=999, seed=1)
    assert set(exact) == set(batch)
    assert set(over) == set(batch)
    # Whole-batch draws from two different seeds must still agree -- there
    # is nothing left for the seed to choose between.
    over_other_seed = draw_reproducible_sample(batch, sample_size=999, seed=2)
    assert exact == over == over_other_seed


def test_zero_or_negative_sample_size_draws_nothing() -> None:
    batch = _batch(10)
    assert draw_reproducible_sample(batch, sample_size=0, seed=1) == []
    assert draw_reproducible_sample(batch, sample_size=-5, seed=1) == []


def test_empty_batch_draws_nothing_regardless_of_sample_size() -> None:
    assert draw_reproducible_sample([], sample_size=5, seed=1) == []


def test_duplicate_member_ids_in_input_are_deduplicated_before_sampling() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    batch_with_dupes = [a, a, b, b, b, c]
    drawn = draw_reproducible_sample(batch_with_dupes, sample_size=2, seed=1)
    assert len(drawn) == 2
    assert len(set(drawn)) == 2


def test_drawn_sample_is_returned_in_stable_sorted_order() -> None:
    """The returned order is always sorted (by string form), independent of
    the RNG's internal draw order -- so two callers comparing a drawn list
    directly (not just as a set) get a consistent answer."""
    batch = _batch(50)
    drawn = draw_reproducible_sample(batch, sample_size=10, seed=5)
    assert drawn == sorted(drawn, key=str)


# --- generate_seed: the one non-deterministic helper in this module --------


def test_generate_seed_is_in_range_and_not_trivially_constant() -> None:
    seeds = {generate_seed() for _ in range(20)}
    assert all(0 <= seed <= MAX_SEED for seed in seeds)
    assert len(seeds) > 1  # astronomically unlikely to collide 20/20 times
