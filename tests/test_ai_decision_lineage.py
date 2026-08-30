"""Tests for AI decision lineage edge recording and querying.

Pure unit tests without DB -- tests the domain types and their invariants.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aida.ai_decision_lineage import (
    AiDecisionEdge,
)

# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------

class TestAiDecisionEdge:
    def test_retrieval_selected_edge(self) -> None:
        run_id = uuid4()
        edge = AiDecisionEdge(
            run_id=run_id,
            decision_type="RETRIEVAL_SELECTED",
            source_node="orchestrator",
            target_node="table:sales.orders",
            reason="highest relevance score (0.95) for the question",
        )

        assert edge.decision_type == "RETRIEVAL_SELECTED"
        assert edge.source_node == "orchestrator"
        assert edge.target_node == "table:sales.orders"
        assert edge.reason != ""

    def test_retrieval_rejected_edge(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="RETRIEVAL_REJECTED",
            source_node="orchestrator",
            target_node="table:hr.employees",
            reason="classification_clearance insufficient; required=CONFIDENTIAL, had=PUBLIC",
        )

        assert edge.decision_type == "RETRIEVAL_REJECTED"
        assert "classification_clearance" in edge.reason

    def test_tool_selected_edge(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="TOOL_SELECTED",
            source_node="orchestrator",
            target_node="tool:monthly-revenue-report/v3",
            reason="exact match on governed tool for question pattern",
        )

        assert edge.decision_type == "TOOL_SELECTED"

    def test_tool_rejected_edge(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="TOOL_REJECTED",
            source_node="orchestrator",
            target_node="tool:employee-lookup/v1",
            reason="role 'Analyst' not in tool allowed_roles",
        )

        assert edge.decision_type == "TOOL_REJECTED"

    def test_refusal_edge_records_control(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="REFUSAL",
            source_node="prompt_risk_classifier",
            target_node="agent_run:abcd-1234",
            reason="BLOCKED by prompt risk classifier",
            control_version="deterministic-prompt-risk-v1",
        )

        assert edge.decision_type == "REFUSAL"
        assert edge.control_version == "deterministic-prompt-risk-v1"
        assert "BLOCKED" in edge.reason

    def test_refusal_without_control_version_allowed(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="REFUSAL",
            source_node="policy_engine",
            target_node="agent_run:xyz",
            reason="principal not authorized for this data domain",
        )

        assert edge.control_version is None


# ---------------------------------------------------------------------------
# Rejection tracking
# ---------------------------------------------------------------------------

class TestRejectionTracking:
    def test_rejection_reason_always_recorded(self) -> None:
        """Rejection reasons must always be present, not just selections."""
        edges = [
            AiDecisionEdge(
                run_id=uuid4(),
                decision_type="RETRIEVAL_REJECTED",
                source_node="orchestrator",
                target_node=f"table:t{i}",
                reason=f"reason for rejection {i}",
            )
            for i in range(5)
        ]

        for edge in edges:
            assert edge.reason != ""
            assert "REJECTED" in edge.decision_type

    def test_mixed_selection_rejection_in_same_run(self) -> None:
        run_id = uuid4()
        edges = [
            AiDecisionEdge(
                run_id=run_id,
                decision_type="RETRIEVAL_SELECTED",
                source_node="orchestrator",
                target_node="table:sales.orders",
                reason="selected: top relevance",
            ),
            AiDecisionEdge(
                run_id=run_id,
                decision_type="RETRIEVAL_REJECTED",
                source_node="orchestrator",
                target_node="table:hr.salaries",
                reason="rejected: PII classification blocked for principal_type AGENT",
            ),
            AiDecisionEdge(
                run_id=run_id,
                decision_type="TOOL_SELECTED",
                source_node="orchestrator",
                target_node="tool:revenue-report/v2",
                reason="governed tool match",
            ),
            AiDecisionEdge(
                run_id=run_id,
                decision_type="TOOL_REJECTED",
                source_node="orchestrator",
                target_node="tool:admin-panel/v1",
                reason="role not in allowed_roles",
            ),
        ]

        selected = [e for e in edges if "SELECTED" in e.decision_type]
        rejected = [e for e in edges if "REJECTED" in e.decision_type]
        assert len(selected) == 2
        assert len(rejected) == 2
        assert all(e.run_id == run_id for e in edges)


# ---------------------------------------------------------------------------
# Refusal evidence
# ---------------------------------------------------------------------------

class TestRefusalEvidence:
    def test_refusal_has_control_version(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="REFUSAL",
            source_node="injection_defense",
            target_node="agent_run:test",
            reason="indirect injection detected in column description",
            control_version="injection-defense-v1",
            evidence={"threat_type": "INSTRUCTION_OVERRIDE", "content_origin": "column:desc"},
        )

        assert edge.control_version is not None
        assert edge.evidence["threat_type"] == "INSTRUCTION_OVERRIDE"

    def test_refusal_evidence_is_value_free(self) -> None:
        """Evidence must not contain source data values."""
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="REFUSAL",
            source_node="abac_engine",
            target_node="agent_run:test",
            reason="DENY by ABAC policy",
            evidence={
                "policy_ids": ["p1", "p2"],
                "decision": "DENY",
                "evaluation_time_ms": 1.5,
            },
        )

        # No source data values in evidence
        evidence_str = str(edge.evidence)
        forbidden = ["SELECT", "password", "secret", "raw_value"]
        for word in forbidden:
            assert word not in evidence_str


# ---------------------------------------------------------------------------
# Value-freedom invariant
# ---------------------------------------------------------------------------

class TestValueFreedom:
    def test_edge_does_not_carry_source_data(self) -> None:
        """Edges must be value-free: no SQL, no passwords, no row values."""
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="RETRIEVAL_SELECTED",
            source_node="orchestrator",
            target_node="table:orders",
            reason="selected by relevance score",
            evidence={"relevance_score": 0.92, "table_id": str(uuid4())},
        )

        # Verify the edge is a structural record, not carrying data
        assert "SELECT" not in edge.reason
        assert "SELECT" not in str(edge.evidence)
        assert edge.source_node != ""
        assert edge.target_node != ""


# ---------------------------------------------------------------------------
# Edge type exhaustiveness
# ---------------------------------------------------------------------------

class TestDecisionTypes:
    @pytest.mark.parametrize(
        "decision_type",
        ["RETRIEVAL_SELECTED", "RETRIEVAL_REJECTED", "TOOL_SELECTED", "TOOL_REJECTED", "REFUSAL"],
    )
    def test_all_decision_types_constructible(self, decision_type: str) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type=decision_type,  # type: ignore[arg-type]
            source_node="test",
            target_node="test",
            reason="test reason",
        )
        assert edge.decision_type == decision_type


# ---------------------------------------------------------------------------
# Timestamp
# ---------------------------------------------------------------------------

class TestTimestamp:
    def test_default_timestamp_is_utc(self) -> None:
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="RETRIEVAL_SELECTED",
            source_node="x",
            target_node="y",
            reason="z",
        )
        assert edge.timestamp.tzinfo is not None

    def test_custom_timestamp(self) -> None:
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        edge = AiDecisionEdge(
            run_id=uuid4(),
            decision_type="REFUSAL",
            source_node="x",
            target_node="y",
            reason="z",
            timestamp=ts,
        )
        assert edge.timestamp == ts
