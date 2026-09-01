"""AG-7 -- query memory similarity + safe adaptation (`aida.query_memory`).

Pure-logic tests, no database: `jaccard_similarity`, `check_candidate_staleness`
and `select_best_match` are deterministic functions over plain dataclasses,
mirroring `tests/test_quality_coupling.py` / `tests/test_tool_first_rate.py`'s
own "pure logic tested without a database" convention. The DB-facing half
(`find_query_memory_match`) and the end-to-end "validation cannot be bypassed"
proof live in `tests/test_agent_orchestrator_query_memory.py`, against a real
(in-memory sqlite) database through the real orchestrator, the same way
`tests/test_quality_runtime_coupling.py` proves AG-6.
"""

from datetime import UTC, datetime

from aida.query_memory import (
    ELIGIBLE_STATUS,
    MemoryCandidateFacts,
    check_candidate_staleness,
    jaccard_similarity,
    retrieved_table_ids_from_hits,
    select_best_match,
)

T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"
T3 = "33333333-3333-3333-3333-333333333333"

RUN_COMPLETED = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
BEFORE_RUN = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
AFTER_RUN = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _candidate(
    *,
    memory_evidence_id: str = "mem-1",
    agent_run_id: str = "run-1",
    query_execution_id: str = "exec-1",
    normalized_sql: str = "SELECT * FROM t WHERE x = :redacted",
    referenced_table_ids: frozenset[str] = frozenset({T1, T2}),
    semantic_version: str | None = "semantic-model:abc:v1",
    status: str = ELIGIBLE_STATUS,
    run_completed_at: datetime = RUN_COMPLETED,
) -> MemoryCandidateFacts:
    return MemoryCandidateFacts(
        memory_evidence_id=memory_evidence_id,
        agent_run_id=agent_run_id,
        query_execution_id=query_execution_id,
        normalized_sql=normalized_sql,
        referenced_table_ids=referenced_table_ids,
        semantic_version=semantic_version,
        status=status,
        run_completed_at=run_completed_at,
    )


# --- jaccard_similarity ---


def test_jaccard_identical_sets_is_one() -> None:
    assert jaccard_similarity(frozenset({T1, T2}), frozenset({T1, T2})) == 1.0


def test_jaccard_disjoint_sets_is_zero() -> None:
    assert jaccard_similarity(frozenset({T1}), frozenset({T2})) == 0.0


def test_jaccard_partial_overlap() -> None:
    # {T1, T2} vs {T1, T3}: intersection 1, union 3 -> 0.3333
    assert jaccard_similarity(frozenset({T1, T2}), frozenset({T1, T3})) == 0.3333


def test_jaccard_either_side_empty_is_zero_not_undefined() -> None:
    assert jaccard_similarity(frozenset(), frozenset({T1})) == 0.0
    assert jaccard_similarity(frozenset({T1}), frozenset()) == 0.0
    assert jaccard_similarity(frozenset(), frozenset()) == 0.0


# --- check_candidate_staleness ---


def test_not_stale_when_version_matches_and_tables_untouched() -> None:
    result = check_candidate_staleness(
        candidate_semantic_version="semantic-model:abc:v1",
        current_semantic_version="semantic-model:abc:v1",
        referenced_table_ids=frozenset({T1, T2}),
        table_updated_at={T1: BEFORE_RUN, T2: BEFORE_RUN},
        run_completed_at=RUN_COMPLETED,
    )
    assert result.is_stale is False
    assert result.reasons == ()


def test_stale_when_semantic_version_has_moved_on() -> None:
    result = check_candidate_staleness(
        candidate_semantic_version="semantic-model:abc:v1",
        current_semantic_version="semantic-model:abc:v2",
        referenced_table_ids=frozenset({T1}),
        table_updated_at={T1: BEFORE_RUN},
        run_completed_at=RUN_COMPLETED,
    )
    assert result.is_stale is True
    assert "SEMANTIC_VERSION_SUPERSEDED" in result.reasons


def test_stale_when_a_referenced_table_changed_after_the_run() -> None:
    """The specific "version-aware" case AG-7 asks for: a referenced object's
    version has advanced since the candidate run completed, even though the
    whole-model semantic_version string never moved (e.g. an unpublished
    catalog re-ingestion)."""
    result = check_candidate_staleness(
        candidate_semantic_version="semantic-model:abc:v1",
        current_semantic_version="semantic-model:abc:v1",
        referenced_table_ids=frozenset({T1, T2}),
        table_updated_at={T1: BEFORE_RUN, T2: AFTER_RUN},
        run_completed_at=RUN_COMPLETED,
    )
    assert result.is_stale is True
    assert f"TABLE_VERSION_ADVANCED:{T2}" in result.reasons
    # T1 is untouched -- only the table that actually changed is named.
    assert not any(reason.startswith(f"TABLE_VERSION_ADVANCED:{T1}") for reason in result.reasons)


def test_stale_when_a_referenced_table_no_longer_resolves() -> None:
    """A dropped/renamed table is treated as changed, not as absent evidence --
    the conservative direction for a governance-sensitive gate."""
    result = check_candidate_staleness(
        candidate_semantic_version="v1",
        current_semantic_version="v1",
        referenced_table_ids=frozenset({T1}),
        table_updated_at={},
        run_completed_at=RUN_COMPLETED,
    )
    assert result.is_stale is True
    assert f"TABLE_UNRESOLVED:{T1}" in result.reasons


def test_stale_can_report_multiple_independent_reasons() -> None:
    result = check_candidate_staleness(
        candidate_semantic_version="v1",
        current_semantic_version="v2",
        referenced_table_ids=frozenset({T1}),
        table_updated_at={T1: AFTER_RUN},
        run_completed_at=RUN_COMPLETED,
    )
    assert result.is_stale is True
    assert "SEMANTIC_VERSION_SUPERSEDED" in result.reasons
    assert f"TABLE_VERSION_ADVANCED:{T1}" in result.reasons


# --- select_best_match ---


def test_no_candidates_returns_none() -> None:
    match = select_best_match(
        [],
        target_table_ids=frozenset({T1}),
        current_semantic_version="v1",
        table_updated_at={},
        min_similarity=0.5,
    )
    assert match is None


def test_selects_the_only_eligible_non_stale_above_threshold_candidate() -> None:
    candidate = _candidate(referenced_table_ids=frozenset({T1, T2}))
    match = select_best_match(
        [candidate],
        target_table_ids=frozenset({T1, T2}),
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: BEFORE_RUN, T2: BEFORE_RUN},
        min_similarity=0.5,
    )
    assert match is not None
    assert match.memory_evidence_id == "mem-1"
    assert match.similarity == 1.0
    assert match.normalized_sql == candidate.normalized_sql


def test_below_threshold_is_rejected() -> None:
    candidate = _candidate(referenced_table_ids=frozenset({T1, T3}))
    match = select_best_match(
        [candidate],
        target_table_ids=frozenset({T1, T2}),  # overlap 1/3 = 0.3333
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: BEFORE_RUN, T3: BEFORE_RUN},
        min_similarity=0.5,
    )
    assert match is None


def test_non_eligible_status_is_never_offered_even_with_perfect_overlap() -> None:
    """Negative feedback (status != ELIGIBLE) suppresses reuse before
    similarity is even considered -- module 13 spec's "feedback suppression"."""
    candidate = _candidate(status="SUPPRESSED", referenced_table_ids=frozenset({T1}))
    match = select_best_match(
        [candidate],
        target_table_ids=frozenset({T1}),
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: BEFORE_RUN},
        min_similarity=0.1,
    )
    assert match is None


def test_stale_candidate_is_never_offered_even_with_perfect_overlap() -> None:
    candidate = _candidate(referenced_table_ids=frozenset({T1}))
    match = select_best_match(
        [candidate],
        target_table_ids=frozenset({T1}),
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: AFTER_RUN},  # table changed after the run completed
        min_similarity=0.1,
    )
    assert match is None


def test_highest_similarity_wins_among_multiple_valid_candidates() -> None:
    weak = _candidate(
        memory_evidence_id="mem-weak",
        agent_run_id="run-a",
        referenced_table_ids=frozenset({T1, T3}),
    )
    strong = _candidate(
        memory_evidence_id="mem-strong",
        agent_run_id="run-b",
        referenced_table_ids=frozenset({T1, T2}),
    )
    match = select_best_match(
        [weak, strong],
        target_table_ids=frozenset({T1, T2}),
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: BEFORE_RUN, T2: BEFORE_RUN, T3: BEFORE_RUN},
        min_similarity=0.1,
    )
    assert match is not None
    assert match.memory_evidence_id == "mem-strong"


def test_tie_break_is_deterministic_by_agent_run_id() -> None:
    a = _candidate(
        memory_evidence_id="mem-a", agent_run_id="run-a", referenced_table_ids=frozenset({T1})
    )
    b = _candidate(
        memory_evidence_id="mem-b", agent_run_id="run-b", referenced_table_ids=frozenset({T1})
    )
    match = select_best_match(
        [b, a],  # order-independent -- deterministic on agent_run_id, not input order
        target_table_ids=frozenset({T1}),
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: BEFORE_RUN},
        min_similarity=0.1,
    )
    assert match is not None
    assert match.agent_run_id == "run-a"


def test_evidence_is_value_free() -> None:
    """The match's own `.evidence()` never carries SQL text or table identity,
    only ids/scores/counts -- safe to fold unredacted into plan_evidence."""
    candidate = _candidate(referenced_table_ids=frozenset({T1, T2}))
    match = select_best_match(
        [candidate],
        target_table_ids=frozenset({T1, T2}),
        current_semantic_version="semantic-model:abc:v1",
        table_updated_at={T1: BEFORE_RUN, T2: BEFORE_RUN},
        min_similarity=0.1,
    )
    assert match is not None
    evidence = match.evidence()
    assert "normalized_sql" not in evidence
    assert "referenced_table_ids" not in evidence
    assert evidence["referenced_table_count"] == 2
    assert evidence["similarity"] == 1.0


# --- retrieved_table_ids_from_hits ---


class _Hit:
    def __init__(self, object_type: str, object_id: str, metadata: dict[str, object] | None = None):
        self.object_type = object_type
        self.object_id = object_id
        self.metadata = metadata or {}


def test_retrieved_table_ids_picks_up_table_hits() -> None:
    hits = [_Hit("TABLE", T1), _Hit("COLUMN", "col-1", {"table_id": T2})]
    assert retrieved_table_ids_from_hits(hits) == frozenset({T1, T2})


def test_retrieved_table_ids_ignores_non_table_hits_without_a_table_reference() -> None:
    hits = [_Hit("GOVERNED_TOOL", "tool-1")]
    assert retrieved_table_ids_from_hits(hits) == frozenset()


def test_retrieved_table_ids_empty_for_no_hits() -> None:
    assert retrieved_table_ids_from_hits([]) == frozenset()
