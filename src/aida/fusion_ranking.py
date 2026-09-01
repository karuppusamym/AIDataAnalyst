"""
Fusion Ranking with Inspectable Factors
==========================================

Combines scores from lexical (BM25/full-text), vector (cosine similarity),
and graph (proximity) signals using Reciprocal Rank Fusion (RRF) or
weighted linear combination.

Every factor is inspectable in the evidence payload.  ``quality_trust`` and
``usage_popularity`` are real signals (RT-7, RT-6): the former is derived
from `quality_coupling.demote_in_retrieval` against a candidate's real open
`DataQualityIncident` rows, the latter from a candidate's real historical
`QueryExecution.referenced_tables` hit count -- both computed in
`retrieval.py::hybrid_retrieve_enhanced`'s Stage 4, not hardcoded here.

Architecture
------------
- ``RankedCandidate``: internal struct with per-signal scores.
- ``FusionConfig``   : weights and method selection.
- ``fuse_results``   : main entry point -- combine and rank.
- ``reciprocal_rank_fusion``: RRF implementation.
- ``weighted_linear_fusion``: weighted sum implementation.

All inputs must be policy-filtered BEFORE fusion -- this module does
not apply any org/source scoping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class FusionConfig:
    """Fusion ranking configuration."""

    method: str = "rrf"  # 'rrf' or 'weighted_linear'
    rrf_k: int = 60  # RRF constant (standard value)
    lexical_weight: float = 0.30
    vector_weight: float = 0.30
    graph_weight: float = 0.20
    # RT-7 / RT-6: real signals (quality-incident demotion, execution-history
    # popularity), weighted enough to move ranking -- not the inert 0.05
    # placeholders these carried while both were hardcoded raw_score=0.5.
    quality_trust_weight: float = 0.10
    usage_popularity_weight: float = 0.10


# ---------------------------------------------------------------------------
# Candidate representation
# ---------------------------------------------------------------------------


@dataclass
class SignalScore:
    """Score from one retrieval signal."""

    signal: str
    raw_score: float
    rank: int | None = None  # Position in this signal's ranked list


@dataclass
class RankedCandidate:
    """A candidate with scores from multiple signals."""

    object_type: str
    object_id: str
    display_name: str
    signals: list[SignalScore] = field(default_factory=list)
    final_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_signal(self, name: str) -> SignalScore | None:
        for s in self.signals:
            if s.signal == name:
                return s
        return None


# ---------------------------------------------------------------------------
# Factor detail for evidence
# ---------------------------------------------------------------------------


@dataclass
class FactorDetail:
    """One factor in the fusion evidence breakdown."""

    signal: str
    raw_score: float
    weight: float
    weighted_score: float
    rank: int | None = None


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    candidates: list[RankedCandidate],
    *,
    config: FusionConfig,
) -> list[RankedCandidate]:
    """Apply Reciprocal Rank Fusion across all signals.

    RRF score for a document d = sum over all signals S of:
        1 / (k + rank_S(d))

    where k is the RRF constant (default 60).
    """
    k = config.rrf_k
    signal_names = _collect_signal_names(candidates)

    # Assign ranks per signal (1-based, by raw_score descending)
    for signal_name in signal_names:
        scored = [
            (c, c.get_signal(signal_name))
            for c in candidates
            if c.get_signal(signal_name) is not None
        ]
        scored.sort(key=lambda x: x[1].raw_score, reverse=True)  # type: ignore[union-attr]
        for rank, (_, sig) in enumerate(scored, start=1):
            sig.rank = rank  # type: ignore[union-attr]

    weight_map = _signal_weight_map(config)

    for candidate in candidates:
        rrf_score = 0.0
        for signal in candidate.signals:
            if signal.rank is not None:
                weight = weight_map.get(signal.signal, 1.0 / len(signal_names))
                rrf_score += weight * (1.0 / (k + signal.rank))
        candidate.final_score = round(rrf_score, 8)

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Weighted Linear Combination
# ---------------------------------------------------------------------------


def weighted_linear_fusion(
    candidates: list[RankedCandidate],
    *,
    config: FusionConfig,
) -> list[RankedCandidate]:
    """Apply weighted linear combination across all signals.

    final_score = sum(weight_i * raw_score_i) for each signal i.
    """
    weight_map = _signal_weight_map(config)

    # Assign ranks for evidence completeness
    signal_names = _collect_signal_names(candidates)
    for signal_name in signal_names:
        scored = [
            (c, c.get_signal(signal_name))
            for c in candidates
            if c.get_signal(signal_name) is not None
        ]
        scored.sort(key=lambda x: x[1].raw_score, reverse=True)  # type: ignore[union-attr]
        for rank, (_, sig) in enumerate(scored, start=1):
            sig.rank = rank  # type: ignore[union-attr]

    for candidate in candidates:
        weighted_sum = 0.0
        for signal in candidate.signals:
            weight = weight_map.get(signal.signal, 0.0)
            weighted_sum += weight * signal.raw_score
        candidate.final_score = round(weighted_sum, 8)

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def fuse_results(
    candidates: list[RankedCandidate],
    *,
    config: FusionConfig | None = None,
    top_k: int = 25,
) -> list[RankedCandidate]:
    """Combine and rank candidates from multiple retrieval signals.

    Parameters
    ----------
    candidates  Pre-merged list of candidates with signal scores attached.
                Must be policy-filtered BEFORE calling this function.
    config      Fusion configuration (default: RRF with standard weights).
    top_k       Maximum results to return.

    Returns
    -------
    Ranked list of candidates with ``final_score`` and per-signal evidence.
    """
    if config is None:
        config = FusionConfig()

    if not candidates:
        return []

    if config.method == "rrf":
        ranked = reciprocal_rank_fusion(candidates, config=config)
    elif config.method == "weighted_linear":
        ranked = weighted_linear_fusion(candidates, config=config)
    else:
        raise ValueError(f"Unknown fusion method: {config.method}")

    return ranked[:top_k]


def build_evidence(
    candidate: RankedCandidate,
    config: FusionConfig,
) -> list[FactorDetail]:
    """Build inspectable evidence for one candidate's ranking."""
    weight_map = _signal_weight_map(config)
    factors: list[FactorDetail] = []
    for signal in candidate.signals:
        weight = weight_map.get(signal.signal, 0.0)
        if config.method == "rrf" and signal.rank is not None:
            weighted_score = weight * (1.0 / (config.rrf_k + signal.rank))
        else:
            weighted_score = weight * signal.raw_score
        factors.append(
            FactorDetail(
                signal=signal.signal,
                raw_score=round(signal.raw_score, 6),
                weight=round(weight, 4),
                weighted_score=round(weighted_score, 8),
                rank=signal.rank,
            )
        )
    return factors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_signal_names(candidates: list[RankedCandidate]) -> list[str]:
    """Collect unique signal names across all candidates, preserving order."""
    seen: set[str] = set()
    names: list[str] = []
    for c in candidates:
        for s in c.signals:
            if s.signal not in seen:
                seen.add(s.signal)
                names.append(s.signal)
    return names


def _signal_weight_map(config: FusionConfig) -> dict[str, float]:
    """Map signal names to their configured weights."""
    return {
        "lexical": config.lexical_weight,
        "vector": config.vector_weight,
        "graph": config.graph_weight,
        "quality_trust": config.quality_trust_weight,
        "usage_popularity": config.usage_popularity_weight,
    }


def merge_candidates(
    *result_lists: list[tuple[str, str, str, float, dict[str, Any]]],
    signal_names: list[str],
) -> list[RankedCandidate]:
    """Merge results from multiple signals into a unified candidate list.

    Each result_list contains tuples of:
    ``(object_type, object_id, display_name, score, metadata)``

    ``signal_names[i]`` names the signal for ``result_lists[i]``.

    Candidates with the same ``(object_type, object_id)`` are merged
    so each carries scores from all signals that found it.
    """
    index: dict[tuple[str, str], RankedCandidate] = {}

    for i, results in enumerate(result_lists):
        signal_name = signal_names[i] if i < len(signal_names) else f"signal_{i}"
        for obj_type, obj_id, display_name, score, metadata in results:
            key = (obj_type, obj_id)
            if key not in index:
                index[key] = RankedCandidate(
                    object_type=obj_type,
                    object_id=obj_id,
                    display_name=display_name,
                    metadata=metadata,
                )
            index[key].signals.append(
                SignalScore(signal=signal_name, raw_score=score)
            )

    return list(index.values())
