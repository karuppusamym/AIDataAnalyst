"""Tests for the ABAC evaluation engine and policy logic."""

import time

import pytest

from aida.abac import (
    ABAC_ENGINE_VERSION,
    AbacPolicy,
    _match_condition,
    _match_conditions,
    evaluate,
    simulate,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _policy(
    id: str = "p1",
    key: str = "test",
    effect: str = "PERMIT",
    subject: dict | None = None,
    resource: dict | None = None,
    env: dict | None = None,
    priority: int = 100,
) -> AbacPolicy:
    return AbacPolicy(
        id=id,
        policy_key=key,
        version=1,
        name=f"Test policy {id}",
        effect=effect,
        subject_conditions=subject or {},
        resource_conditions=resource or {},
        environment_conditions=env or {},
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Condition matching
# ---------------------------------------------------------------------------

class TestConditionMatching:
    def test_scalar_match(self) -> None:
        assert _match_condition("role", "admin", {"role": "admin"}) is True
        assert _match_condition("role", "admin", {"role": "viewer"}) is False

    def test_list_condition_matches_scalar_attribute(self) -> None:
        assert _match_condition("role", ["admin", "viewer"], {"role": "admin"}) is True
        assert _match_condition("role", ["admin", "viewer"], {"role": "editor"}) is False

    def test_list_condition_matches_list_attribute(self) -> None:
        # Intersection semantics: any overlap is a match
        assert _match_condition("roles", ["admin"], {"roles": ["admin", "viewer"]}) is True
        assert _match_condition("roles", ["editor"], {"roles": ["admin", "viewer"]}) is False

    def test_dict_range_condition(self) -> None:
        assert _match_condition("hour", {"min": 9, "max": 17}, {"hour": 12}) is True
        assert _match_condition("hour", {"min": 9, "max": 17}, {"hour": 3}) is False
        assert _match_condition("hour", {"min": 9, "max": 17}, {"hour": 20}) is False

    def test_missing_attribute_does_not_match(self) -> None:
        assert _match_condition("role", "admin", {}) is False

    def test_scalar_condition_matches_list_attribute(self) -> None:
        assert _match_condition("role", "admin", {"role": ["admin", "viewer"]}) is True
        assert _match_condition("role", "editor", {"role": ["admin", "viewer"]}) is False

    def test_all_conditions_must_match(self) -> None:
        conditions = {"role": "admin", "clearance": "TOP_SECRET"}
        assert _match_conditions(conditions, {"role": "admin", "clearance": "TOP_SECRET"}) is True
        assert _match_conditions(conditions, {"role": "admin", "clearance": "SECRET"}) is False

    def test_empty_conditions_always_match(self) -> None:
        assert _match_conditions({}, {"anything": "value"}) is True


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

class TestEvaluation:
    def test_permit_when_matching_permit_policy(self) -> None:
        policies = [_policy(effect="PERMIT", subject={"role": "admin"})]
        result = evaluate({"role": "admin"}, {}, {}, policies)

        assert result.decision == "PERMIT"
        assert len(result.contributing_policies) == 1
        assert result.policy_version == ABAC_ENGINE_VERSION

    def test_deny_when_no_matching_policy(self) -> None:
        policies = [_policy(effect="PERMIT", subject={"role": "admin"})]
        result = evaluate({"role": "viewer"}, {}, {}, policies)

        assert result.decision == "DENY"
        assert "no matching policy found" in result.reasons[0]

    def test_deny_overrides_permit(self) -> None:
        policies = [
            _policy(id="permit", effect="PERMIT", subject={"role": "admin"}, priority=200),
            _policy(
                id="deny",
                effect="DENY",
                subject={"role": "admin"},
                resource={"classification": "TOP_SECRET"},
                priority=100,
            ),
        ]
        result = evaluate(
            {"role": "admin"},
            {"classification": "TOP_SECRET"},
            {},
            policies,
        )

        assert result.decision == "DENY"
        assert "deny" in result.contributing_policies

    def test_resource_conditions_evaluated(self) -> None:
        policies = [
            _policy(
                effect="PERMIT",
                subject={"role": "analyst"},
                resource={"classification": ["PUBLIC", "INTERNAL"]},
            ),
        ]

        public = evaluate({"role": "analyst"}, {"classification": "PUBLIC"}, {}, policies)
        assert public.decision == "PERMIT"
        restricted = evaluate(
            {"role": "analyst"}, {"classification": "RESTRICTED"}, {}, policies
        )
        assert restricted.decision == "DENY"

    def test_environment_conditions_evaluated(self) -> None:
        policies = [
            _policy(
                effect="PERMIT",
                subject={"role": "analyst"},
                env={"time_of_day": {"min": 9, "max": 17}},
            ),
        ]

        assert evaluate({"role": "analyst"}, {}, {"time_of_day": 12}, policies).decision == "PERMIT"
        assert evaluate({"role": "analyst"}, {}, {"time_of_day": 3}, policies).decision == "DENY"

    def test_evaluation_time_recorded(self) -> None:
        policies = [_policy(effect="PERMIT")]
        result = evaluate({}, {}, {}, policies)
        assert result.evaluation_time_ms >= 0

    def test_multiple_reasons_collected(self) -> None:
        policies = [
            _policy(id="d1", effect="DENY", subject={"role": "admin"}, priority=10),
            _policy(id="d2", effect="DENY", subject={"role": "admin"}, priority=20),
        ]
        result = evaluate({"role": "admin"}, {}, {}, policies)
        assert result.decision == "DENY"
        assert len(result.reasons) == 2
        assert len(result.contributing_policies) == 2


# ---------------------------------------------------------------------------
# Agent-vs-human gating (PG-2)
# ---------------------------------------------------------------------------

class TestPrincipalTypeGating:
    def test_agent_denied_pii_access(self) -> None:
        policies = [
            _policy(
                id="deny-agent-pii",
                effect="DENY",
                subject={"principal_type": "AGENT"},
                resource={"classification": "PII"},
                priority=10,
            ),
            _policy(
                id="permit-all",
                effect="PERMIT",
                priority=100,
            ),
        ]

        agent_result = evaluate(
            {"principal_type": "AGENT", "role": "analyst"},
            {"classification": "PII"},
            {},
            policies,
        )
        assert agent_result.decision == "DENY"

    def test_human_permitted_pii_access(self) -> None:
        policies = [
            _policy(
                id="deny-agent-pii",
                effect="DENY",
                subject={"principal_type": "AGENT"},
                resource={"classification": "PII"},
                priority=10,
            ),
            _policy(
                id="permit-all",
                effect="PERMIT",
                priority=100,
            ),
        ]

        human_result = evaluate(
            {"principal_type": "USER", "role": "analyst"},
            {"classification": "PII"},
            {},
            policies,
        )
        assert human_result.decision == "PERMIT"

    def test_service_account_gating(self) -> None:
        policies = [
            _policy(
                id="deny-service-restricted",
                effect="DENY",
                subject={"principal_type": "SERVICE"},
                resource={"sensitivity": "HIGH"},
                priority=10,
            ),
            _policy(id="permit-all", effect="PERMIT", priority=100),
        ]

        result = evaluate(
            {"principal_type": "SERVICE"},
            {"sensitivity": "HIGH"},
            {},
            policies,
        )
        assert result.decision == "DENY"


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class TestSimulation:
    def test_simulate_single_evaluation(self) -> None:
        policies = [_policy(effect="PERMIT", subject={"role": "admin"})]
        results = simulate({"role": "admin"}, {}, {}, policies)

        assert len(results) == 1
        assert results[0].decision == "PERMIT"

    def test_simulate_vary_subject_attrs(self) -> None:
        policies = [
            _policy(effect="PERMIT", subject={"role": "admin"}),
            _policy(
                id="deny-viewer",
                effect="DENY",
                subject={"role": "viewer"},
                resource={"classification": "RESTRICTED"},
                priority=10,
            ),
        ]

        results = simulate(
            {"role": "admin"},
            {"classification": "RESTRICTED"},
            {},
            policies,
            vary_subject_attrs=[
                {"role": "admin"},
                {"role": "viewer"},
                {"role": "analyst"},
            ],
        )

        assert len(results) == 3
        assert results[0].decision == "PERMIT"   # admin
        assert results[1].decision == "DENY"      # viewer (explicit deny)
        assert results[2].decision == "DENY"      # analyst (no matching policy)


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_evaluation_under_50ms_with_500_policies(self) -> None:
        """Verify p95 ≤ 50ms target with a realistic policy count."""
        policies = [
            _policy(
                id=f"p{i}",
                effect="PERMIT" if i % 2 == 0 else "DENY",
                subject={"role": f"role_{i}"},
                resource={"classification": f"class_{i}"},
                priority=i,
            )
            for i in range(500)
        ]

        timings: list[float] = []
        for _ in range(100):
            start = time.monotonic()
            evaluate({"role": "role_250"}, {"classification": "class_250"}, {}, policies)
            elapsed_ms = (time.monotonic() - start) * 1000
            timings.append(elapsed_ms)

        timings.sort()
        p95 = timings[int(len(timings) * 0.95)]
        assert p95 < 50, f"p95 evaluation time {p95:.2f}ms exceeds 50ms target"


# ---------------------------------------------------------------------------
# Decision integrity
# ---------------------------------------------------------------------------

class TestDecisionIntegrity:
    def test_decision_contains_policy_version(self) -> None:
        result = evaluate({}, {}, {}, [_policy(effect="PERMIT")])
        assert result.policy_version == ABAC_ENGINE_VERSION

    def test_empty_policies_default_deny(self) -> None:
        result = evaluate({"role": "admin"}, {}, {}, [])
        assert result.decision == "DENY"
        assert "no matching policy found" in result.reasons[0]

    def test_decision_is_frozen(self) -> None:
        result = evaluate({}, {}, {}, [_policy(effect="PERMIT")])
        with pytest.raises(AttributeError):
            result.decision = "PERMIT"  # type: ignore[misc]
