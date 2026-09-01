"""AT-14: seeded, reproducible acceptance sampling over a batch of pending
drafted-prose review items -- language fields only (GL-9's
``AssetDescriptionDraft`` table descriptions today; the same pure functions
apply to any future language-field drafting pipeline that routes through the
``governance_review`` queue).

Reviewing every AI-drafted prose item in a large batch one at a time does not
scale, but auto-publishing high-confidence drafts without any human review
does not either (`Docs/90-reference/04-analysis-algorithms.md` SS4's 0.70
model-only confidence cap and ADR-0001's "models propose, deterministic
services decide" both name the same line this module must not cross).
Acceptance sampling is the middle path this row asks for: a steward reviews
a random, reproducible SAMPLE of a batch -- not the whole batch, and not a
model self-assessment -- and that sample decision is what gets applied, to
exactly the sampled items. The seed and the drawn member ids are the audit
evidence that makes "we sampled this batch and decided X" replayable and
defensible after the fact, rather than an unverifiable claim.

Everything in this module is pure and DB-free: no session, no I/O, nothing
that touches wall-clock time except the (clearly separate, non-deterministic)
`generate_seed` helper. That is what makes the sampling *itself* directly
testable -- same seed against the same batch membership must draw the same
ids, every time, in this process or any other.
"""

from __future__ import annotations

import math
import random
import secrets
from collections.abc import Sequence
from uuid import UUID

# A caller-supplied or server-generated seed is stored and echoed back as a
# plain JSON number (in an AuditEvent.details JSON column and in the API
# response) -- keep it within the range every JSON/JS number and Postgres
# JSON integer can round-trip exactly, rather than the full 64-bit space.
MAX_SEED = (2**63) - 1


def resolve_sample_size(
    batch_size: int,
    *,
    sample_size: int | None = None,
    sample_fraction: float | None = None,
) -> int:
    """Resolve a target sample size for a batch of ``batch_size`` pending
    items, from either an explicit count or a fraction of the batch.

    Always returns a value clamped to ``[1, batch_size]`` when
    ``batch_size > 0`` (a batch with at least one member always yields at
    least a sample of 1 -- "sampling" a batch down to zero review would be
    indistinguishable from skipping review entirely) and ``0`` when the batch
    itself is empty. A fraction is rounded *up* (``math.ceil``) so a small
    fraction of a small batch still reviews at least one item rather than
    rounding away to nothing.

    Choosing between ``sample_size`` and ``sample_fraction`` (or requiring
    exactly one of them) is the API layer's job via request validation --
    this function stays permissive and pure: given both, ``sample_size``
    wins; given neither, the whole batch is the "sample" (nothing to
    resolve).
    """
    if batch_size <= 0:
        return 0
    if sample_size is not None:
        return max(1, min(sample_size, batch_size))
    if sample_fraction is not None:
        computed = math.ceil(batch_size * sample_fraction)
        return max(1, min(computed, batch_size))
    return batch_size


def draw_reproducible_sample(
    member_ids: Sequence[UUID], *, sample_size: int, seed: int
) -> list[UUID]:
    """Deterministically draw ``sample_size`` distinct ids from
    ``member_ids``.

    Reproducibility contract -- this is the whole point of the row, and the
    property every test in ``tests/test_sampling_review.py`` exercises
    directly:

    * The SAME ``seed`` against the SAME batch MEMBERSHIP (a set -- the
      input order of ``member_ids`` never affects the result) always draws
      the SAME ids, in the SAME returned order, on every call, in this
      process or any other.
    * A DIFFERENT ``seed`` against the same membership generally draws a
      DIFFERENT sample (not guaranteed for every possible seed pair -- two
      seeds can coincidentally draw the same subset, especially for a small
      batch or a large sample fraction -- but is true for the seeds this
      module's own tests use).
    * ``random.Random(seed)`` is instantiated fresh on every call, never a
      shared or global RNG, so no other sampling call anywhere in the
      process -- concurrent or otherwise -- can perturb this one's draw.

    ``sample_size >= len(member_ids)`` returns the whole (deduplicated,
    sorted) batch -- there is nothing left to sample, and doing so is not an
    error. ``sample_size <= 0`` returns an empty list.
    """
    unique_sorted = sorted(set(member_ids), key=str)
    if sample_size >= len(unique_sorted):
        return unique_sorted
    if sample_size <= 0:
        return []
    # Not cryptographic use -- reproducibility from a caller-known seed is
    # the requirement here, which a CSPRNG cannot provide by design.
    rng = random.Random(seed)  # noqa: S311
    drawn = rng.sample(unique_sorted, sample_size)
    return sorted(drawn, key=str)


def generate_seed() -> int:
    """A fresh, unpredictable seed for a server-generated (as opposed to
    caller-supplied, controlled-review) sampling draw.

    Deliberately NOT part of ``draw_reproducible_sample``'s pure contract --
    this is the one function in this module that is not deterministic, by
    design (it draws from OS entropy via `secrets`). Call it once per draw,
    record the value it returns in the audit trail and API response, and
    every subsequent replay of that draw passes the recorded value back in
    as an explicit ``seed`` rather than calling this again.
    """
    return secrets.randbelow(MAX_SEED)


__all__ = [
    "MAX_SEED",
    "draw_reproducible_sample",
    "generate_seed",
    "resolve_sample_size",
]
