"""The retrieval pipeline's stage boundaries hold their own rules.

`hybrid_retrieve_enhanced` is now a composition (`aida.retrieval_stages`), and a
composition is only worth anything if each stage can be reasoned about alone. So
these tests assert the *rules*, not the shape: the candidate set is bounded where
it says it is, a channel's contribution becomes a signal exactly one way, a
cancelled retrieval stops at a stage boundary rather than mid-stage, and every
stage reports what it cost and what it was worth.

Deliberately not a second copy of the quality benchmark: whether the ranking is
*good* is measured end-to-end by `scripts/quality_benchmark.py` against a
committed baseline. What is measured here is whether the stage contract is
honoured.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.fusion_ranking import RankedCandidate, SignalScore
from aida.models import DataSource
from aida.retrieval import HybridRetrievalHit
from aida.retrieval_metrics import SkipReason
from aida.retrieval_stages import (
    CandidatePool,
    ChannelReport,
    ChannelResult,
    DeadlineToken,
    NeverCancelled,
    PredicateToken,
    RetrievalCancelled,
    RetrievalRequest,
    SignalContribution,
    check_cancelled,
    merge_contributions,
)


def _datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="stage-fixture",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://stages",
        status="ACTIVE",
    )


def _request(**overrides: Any) -> RetrievalRequest:
    datasource = overrides.pop("datasource", None) or _datasource()
    defaults: dict[str, Any] = {
        "datasource": datasource,
        "question": "which tables hold settlement volume",
        "settings": Settings(),
        "organization_id": datasource.organization_id,
    }
    defaults.update(overrides)
    return RetrievalRequest(**defaults)


def _pool(*keys: str) -> CandidatePool:
    candidates = {
        key: RankedCandidate(
            object_type=key.split(":")[0],
            object_id=key.split(":")[1],
            display_name=key,
            signals=[SignalScore(signal="lexical", raw_score=0.5)],
            metadata={"origin": "lexical"},
        )
        for key in keys
    }
    return CandidatePool(authorized=[], candidates=candidates)


# ---------------------------------------------------------------------------
# The candidate bound
# ---------------------------------------------------------------------------


def test_the_authorized_bound_is_stated_not_inherited() -> None:
    """The review's point is that the candidate set should be bounded
    *explicitly* rather than by whichever downstream cut happens to be smallest.
    A caller that names a bound gets that bound; one that does not gets the
    documented default, which is the same number the lexical stage already
    applied -- so the default is behaviour-preserving and the override is real.
    """
    default = _request()
    assert default.authorized_limit == default.settings.agent_retrieval_limit

    narrowed = _request(candidate_limit=3)
    assert narrowed.authorized_limit == 3
    assert narrowed.result_limit == narrowed.settings.agent_retrieval_limit, (
        "bounding the candidate set must not silently also change how many "
        "results the caller is handed"
    )


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_a_retrieval_runs_to_completion_by_default() -> None:
    """Cancellation is opt-in. Every existing caller passes no token, and a
    default that could stop a retrieval would change their behaviour."""
    request = _request()
    assert isinstance(request.cancel, NeverCancelled)
    for channel in ("lexical", "vector", "graph", "trust", "fusion", "evidence"):
        check_cancelled(request, channel)  # must not raise


def test_a_cancelled_token_stops_the_pipeline_at_the_boundary() -> None:
    """`RetrievalCancelled`, not `asyncio.CancelledError`: this is a cooperative
    stop the pipeline chose, and a caller that catches it can still return the
    partial ranking it already has. Conflating the two would make "the client
    hung up" indistinguishable from "the event loop is tearing this down".
    """
    request = _request(cancel=PredicateToken(predicate=lambda: True))
    with pytest.raises(RetrievalCancelled) as cancelled:
        check_cancelled(request, "graph")
    assert "graph" in str(cancelled.value)


def test_a_deadline_token_expires_on_its_own() -> None:
    """A real, self-driven cancellation source rather than a hook waiting for a
    caller: a retrieval that has already spent its budget stops instead of also
    paying for the remaining channels."""
    assert DeadlineToken.after(60.0).cancelled() is False
    assert DeadlineToken.after(-1.0).cancelled() is True


def test_a_predicate_token_follows_its_predicate() -> None:
    live = {"disconnected": False}
    token = PredicateToken(predicate=lambda: live["disconnected"])
    assert token.cancelled() is False
    live["disconnected"] = True
    assert token.cancelled() is True


# ---------------------------------------------------------------------------
# The merge rule
# ---------------------------------------------------------------------------


def _result(*contributions: SignalContribution) -> ChannelResult:
    return ChannelResult(
        contributions=list(contributions),
        report=ChannelReport(
            channel="vector",
            scored=len(contributions),
            contributed=0,
            seconds=0.0,
            mean_score=0.0,
        ),
    )


def test_a_channel_appends_a_signal_to_a_candidate_that_already_exists() -> None:
    pool = _pool("TABLE:t1")
    merge_contributions(
        pool,
        _result(
            SignalContribution(
                object_type="TABLE",
                object_id="t1",
                display_name="settlements",
                signal="vector",
                raw_score=0.8,
            )
        ),
    )
    signals = [(s.signal, s.raw_score) for s in pool.candidates["TABLE:t1"].signals]
    assert signals == [("lexical", 0.5), ("vector", 0.8)]


def test_a_channel_may_introduce_a_candidate_no_earlier_channel_found() -> None:
    """Graph expansion exists to reach past what lexical and vector found; a
    merge rule that only ever re-scored existing candidates would make the
    channel inert."""
    pool = _pool("TABLE:t1")
    merge_contributions(
        pool,
        _result(
            SignalContribution(
                object_type="TABLE",
                object_id="t2",
                display_name="settlement_lines",
                signal="graph",
                raw_score=0.4,
                metadata={"graph_expansion_path": ["TABLE:t1", "TABLE:t2"]},
            )
        ),
    )
    assert set(pool.candidates) == {"TABLE:t1", "TABLE:t2"}
    introduced = pool.candidates["TABLE:t2"]
    assert [s.signal for s in introduced.signals] == ["graph"]
    assert introduced.metadata["graph_expansion_path"] == ["TABLE:t1", "TABLE:t2"]


def test_a_channel_cannot_overwrite_an_existing_candidates_metadata() -> None:
    """One authoritative merge rule, and it is conservative: a later channel may
    *add* one of a closed set of provenance keys to a candidate an earlier
    channel established, and may not touch anything else. Before this rule was
    stated in one place, `vector_path` was set with `setdefault` on one branch
    and plain assignment on another.
    """
    pool = _pool("TABLE:t1")
    pool.candidates["TABLE:t1"].metadata["vector_path"] = "PERSISTED_INDEX"
    merge_contributions(
        pool,
        _result(
            SignalContribution(
                object_type="TABLE",
                object_id="t1",
                display_name="settlements",
                signal="vector",
                raw_score=0.9,
                metadata={
                    "vector_path": "LIVE_EMBED",
                    "origin": "vector",
                    "datasource_id": "smuggled",
                },
            )
        ),
    )
    metadata = pool.candidates["TABLE:t1"].metadata
    assert metadata["vector_path"] == "PERSISTED_INDEX", "an existing key was overwritten"
    assert metadata["origin"] == "lexical", "a non-mergeable key was overwritten"
    assert "datasource_id" not in metadata, "a channel smuggled a new key onto a candidate"


def test_merging_records_the_channels_report() -> None:
    """The pool's reports are the per-retrieval evidence the log line carries;
    a merge that dropped one would leave a channel invisible."""
    pool = _pool("TABLE:t1")
    merge_contributions(pool, _result())
    assert [report.channel for report in pool.reports] == ["vector"]


# ---------------------------------------------------------------------------
# Stage reporting
# ---------------------------------------------------------------------------


def test_a_report_distinguishes_rescoring_from_contributing() -> None:
    """`scored` minus `contributed` is how much a channel merely re-ranked what
    somebody else already found. A channel whose `contributed` collapses to zero
    is still costing latency and is the thing an operator needs to see."""
    report = ChannelReport(
        channel="vector", scored=25, contributed=0, seconds=0.31, mean_score=0.42
    )
    evidence = report.evidence()
    assert evidence["scored"] == 25
    assert evidence["contributed"] == 0
    assert evidence["seconds"] == 0.31
    assert evidence["mean_score"] == 0.42
    assert evidence["skipped_reason"] is None


def test_a_skipped_channel_says_why_in_a_closed_vocabulary() -> None:
    """The vector channel's skip reason originates in a provider's free-text
    message. Free text on a metric label is F17's unbounded-cardinality finding
    with a different source, so the reason is mapped to a closed enum and the
    original message goes to the log."""
    report = ChannelReport(
        channel="vector",
        scored=0,
        contributed=0,
        seconds=0.0,
        mean_score=0.0,
        skipped_reason=SkipReason.PROVIDER_UNAVAILABLE,
    )
    assert report.evidence()["skipped_reason"] == "PROVIDER_UNAVAILABLE"
    assert set(SkipReason) == {
        SkipReason.DISABLED,
        SkipReason.PROVIDER_UNAVAILABLE,
        SkipReason.NO_SEEDS,
        SkipReason.CANCELLED,
    }


# ---------------------------------------------------------------------------
# The stage set itself
# ---------------------------------------------------------------------------


def test_the_pipeline_stages_are_the_ones_the_review_named() -> None:
    """R02 names the boundaries: authorized candidates, lexical/vector/graph
    providers, fusion, trust, evidence. A stage quietly added or dropped changes
    what "one authoritative implementation per invariant" covers, so the set is
    asserted rather than left to the reader of `hybrid_retrieve_enhanced`.
    """
    import inspect

    from aida import retrieval, retrieval_stages

    for name in (
        "select_authorized_candidates",
        "run_vector_channel",
        "run_graph_channel",
        "merge_contributions",
        "run_trust_channel",
        "fuse",
        "assemble_evidence",
    ):
        assert hasattr(retrieval_stages, name), f"stage {name} is missing"

    source = inspect.getsource(retrieval.hybrid_retrieve_enhanced)
    for stage in (
        "select_authorized_candidates",
        "run_vector_channel",
        "run_graph_channel",
        "run_trust_channel",
        "fuse(",
        "assemble_evidence",
    ):
        assert stage in source, f"the composition no longer calls {stage}"
    assert source.count("check_cancelled") >= 6, (
        "cancellation is checked at every stage boundary; a stage added without "
        "a check is a stage a cancelled retrieval still pays for"
    )


def test_hits_keep_their_public_shape() -> None:
    """The decomposition must be invisible to every caller: `HybridRetrievalHit`
    is what `agent_intelligence.GovernedRetriever` and the retrieval-preview
    endpoint both consume."""
    hit = HybridRetrievalHit(
        object_type="TABLE",
        object_id="t1",
        display_name="settlements",
        score=0.7,
        reason_codes=["lexical"],
        metadata={"retrieval_evidence": {"final_score": 0.7}},
    )
    evidence = hit.evidence()
    assert set(evidence) == {
        "object_type",
        "object_id",
        "display_name",
        "score",
        "reason_codes",
        "metadata",
    }


async def test_vector_channel_ranks_nothing_when_nothing_is_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty authorized set is the policy filter's answer, not an absent filter.

    `search_persisted_index(candidates=None)` means "no candidate filter" and
    ranks the whole organization's index. The stage used to pass
    `candidates=refs or None`, so a caller authorized for nothing got the
    estate's top-N semantic hits back -- policy applied before ranking, then
    discarded by a falsy empty tuple.
    """
    from aida import retrieval_stages as stages
    from aida import vector_index_service
    from aida.vector_index_service import IndexFreshness

    recorded: dict[str, object] = {}

    class _Batch:
        vectors = ([0.1, 0.2],)

    class _Provider:
        async def embed(self, texts: list[str]) -> _Batch:
            return _Batch()

    async def _fresh(session: object, organization_id: object, **kwargs: object) -> IndexFreshness:
        return IndexFreshness(
            usable=True,
            reason="FRESH",
            entries=1_000,
            signature="sig",
            built_at=None,
            age_minutes=1.0,
        )

    async def _search(session: object, organization_id: object, query: object, **kwargs: object):
        recorded["candidates"] = kwargs.get("candidates")
        return ()

    monkeypatch.setattr(stages, "resolve_embedding_provider", lambda *a, **k: _Provider())
    monkeypatch.setattr(vector_index_service, "index_freshness", _fresh)
    monkeypatch.setattr(vector_index_service, "search_persisted_index", _search)

    result = await stages.run_vector_channel(
        None,  # type: ignore[arg-type]
        _request(),
        CandidatePool(authorized=[], candidates={}),
    )

    assert recorded["candidates"] == ()
    assert result.contributions == []
