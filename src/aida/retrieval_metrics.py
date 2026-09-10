"""Per-stage instrumentation for the hybrid retrieval pipeline.

The 2026-09-05 review's retrieval row asks for "stage metrics, bounded
candidates, cancellation, per-channel quality/latency". This module is the
first and last of those: one fixed, small set of series, labelled only by
channel name and by a closed set of skip reasons.

Why the reason label is mapped rather than passed through: the vector channel's
skip reason originates in an `EmbeddingUnavailable` message, which is free text
a provider can change. Free text on a metric label is F17's unbounded-label
finding with a different source, so `SkipReason` is a closed enum and the
original message goes to the log where it belongs.
"""

from __future__ import annotations

from enum import StrEnum

from prometheus_client import Counter, Histogram

# The channels a retrieval can draw candidates from, plus the two stages that
# consume rather than produce them. Closed set -- a new channel is a code
# change here as well as in `aida.retrieval_stages`, which is the point.
RETRIEVAL_CHANNELS: tuple[str, ...] = (
    "lexical",
    "vector",
    "graph",
    "trust",
    "fusion",
    "evidence",
)


class SkipReason(StrEnum):
    """Why a channel produced nothing. Closed so it is safe as a metric label."""

    DISABLED = "DISABLED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    NO_SEEDS = "NO_SEEDS"
    CANCELLED = "CANCELLED"


RETRIEVAL_CHANNEL_SECONDS = Histogram(
    "aida_retrieval_channel_duration_seconds",
    "Wall-clock time spent in one retrieval channel.",
    labelnames=("channel",),
)
RETRIEVAL_CHANNEL_CANDIDATES = Histogram(
    "aida_retrieval_channel_candidates",
    "Candidates a retrieval channel scored.",
    labelnames=("channel",),
    buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1_000, 5_000),
)
RETRIEVAL_CHANNEL_CONTRIBUTED = Histogram(
    "aida_retrieval_channel_new_candidates",
    (
        "Candidates a channel added that no earlier channel had found. The "
        "channel's marginal value, as opposed to how much it re-scored."
    ),
    labelnames=("channel",),
    buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1_000),
)
RETRIEVAL_CHANNEL_MEAN_SCORE = Histogram(
    "aida_retrieval_channel_mean_score",
    (
        "Mean raw signal score a channel assigned, in [0,1]. Per-channel "
        "quality: a channel whose mean collapses is still running and still "
        "costing latency while contributing nothing to the ranking."
    ),
    labelnames=("channel",),
    buckets=(0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
RETRIEVAL_CHANNEL_SKIPPED = Counter(
    "aida_retrieval_channel_skipped_total",
    "Times a retrieval channel was skipped, by closed-set reason.",
    labelnames=("channel", "reason"),
)
RETRIEVAL_CANDIDATES_BOUNDED = Counter(
    "aida_retrieval_candidate_bound_applied_total",
    (
        "Retrievals whose authorized-candidate set was truncated by the "
        "explicit candidate bound rather than by a downstream cut."
    ),
)
RETRIEVAL_CANCELLED = Counter(
    "aida_retrieval_cancelled_total",
    "Retrievals abandoned at a stage boundary because their token was cancelled.",
    labelnames=("channel",),
)
RETRIEVAL_SECONDS = Histogram(
    "aida_retrieval_duration_seconds",
    "Wall-clock time for one complete hybrid retrieval.",
)

__all__ = [
    "RETRIEVAL_CANCELLED",
    "RETRIEVAL_CANDIDATES_BOUNDED",
    "RETRIEVAL_CHANNELS",
    "RETRIEVAL_CHANNEL_CANDIDATES",
    "RETRIEVAL_CHANNEL_CONTRIBUTED",
    "RETRIEVAL_CHANNEL_MEAN_SCORE",
    "RETRIEVAL_CHANNEL_SECONDS",
    "RETRIEVAL_CHANNEL_SKIPPED",
    "RETRIEVAL_SECONDS",
    "SkipReason",
]
