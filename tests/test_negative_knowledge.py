"""Tests for negative knowledge surface."""

from datetime import UTC, datetime
from uuid import uuid4

from aida.negative_knowledge import (
    NegativeAssertion,
    compute_predicate_hash,
)

# ---------------------------------------------------------------------------
# Predicate hashing
# ---------------------------------------------------------------------------


def test_predicate_hash_deterministic() -> None:
    pred = {"source": "col_a", "target": "col_b", "type": "FOREIGN_KEY"}
    h1 = compute_predicate_hash(pred)
    h2 = compute_predicate_hash(pred)
    assert h1 == h2


def test_predicate_hash_order_independent() -> None:
    """JSON keys are sorted, so order doesn't matter."""
    pred_a = {"source": "col_a", "target": "col_b"}
    pred_b = {"target": "col_b", "source": "col_a"}
    assert compute_predicate_hash(pred_a) == compute_predicate_hash(pred_b)


def test_predicate_hash_different_for_different_predicates() -> None:
    pred_a = {"source": "col_a", "target": "col_b"}
    pred_b = {"source": "col_a", "target": "col_c"}
    assert compute_predicate_hash(pred_a) != compute_predicate_hash(pred_b)


# ---------------------------------------------------------------------------
# NegativeAssertion data
# ---------------------------------------------------------------------------


def test_negative_assertion_defaults() -> None:
    na = NegativeAssertion(
        id=None,
        assertion_type="RELATIONSHIP_REJECTED",
        subject_id="col:abc123",
        predicate={"source": "a", "target": "b"},
        evidence={"reason": "manual review"},
        rejected_by="steward@bank.com",
        rejected_at=datetime.now(UTC),
    )
    assert na.suppression_active is True
    assert na.material_change_hash is None


def test_negative_assertion_suppression_flag() -> None:
    na = NegativeAssertion(
        id=uuid4(),
        assertion_type="INFERENCE_REJECTED",
        subject_id="table:xyz",
        predicate={"domain": "payments", "entity": "fraud_flag"},
        evidence={"confidence": 0.3},
        rejected_by="admin",
        rejected_at=datetime.now(UTC),
        suppression_active=False,
    )
    assert na.suppression_active is False


# ---------------------------------------------------------------------------
# Re-proposal suppression logic
# ---------------------------------------------------------------------------


def test_re_proposal_suppression_uses_hash() -> None:
    """Suppression matching is based on predicate hash, not object identity."""
    pred = {"source": "col_a", "target": "col_b", "type": "FK_INFERRED"}
    hash_original = compute_predicate_hash(pred)
    hash_same = compute_predicate_hash(dict(pred))  # new dict, same values
    assert hash_original == hash_same


def test_material_change_lifts_suppression() -> None:
    """Different predicate hash means evidence changed materially."""
    original = {"source": "col_a", "target": "col_b", "confidence": 0.3}
    changed = {"source": "col_a", "target": "col_b", "confidence": 0.9}
    assert compute_predicate_hash(original) != compute_predicate_hash(changed)


# ---------------------------------------------------------------------------
# Assertion type coverage
# ---------------------------------------------------------------------------


def test_all_assertion_types_constructible() -> None:
    """All four assertion types can be instantiated."""
    types = [
        "RELATIONSHIP_REJECTED",
        "INFERENCE_REJECTED",
        "TERM_CONFLICT_RESOLVED",
        "CLASSIFICATION_OVERRIDDEN",
    ]
    now = datetime.now(UTC)
    for assertion_type in types:
        na = NegativeAssertion(
            id=None,
            assertion_type=assertion_type,
            subject_id=f"test:{assertion_type}",
            predicate={"type": assertion_type},
            evidence={},
            rejected_by="test",
            rejected_at=now,
        )
        assert na.assertion_type == assertion_type
