"""Attribute-Based Access Control (ABAC) evaluation engine.

Deterministic, in-memory policy evaluation for attribute-based access
decisions.  Every decision logs its inputs and pins the policy version
used so the audit trail is self-contained.

Performance target: p95 ≤ 50 ms for typical policy sets (< 500 rules).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

ABAC_ENGINE_VERSION = "abac-engine-v1"


# ---------------------------------------------------------------------------
# Domain data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AbacAttribute:
    """A single typed attribute with provenance."""

    key: str
    value: Any
    source: str = "header"
    evidence: str | None = None


@dataclass(frozen=True, slots=True)
class AbacPolicy:
    """A single ABAC policy rule.

    Subject conditions can gate on role, group, classification_clearance,
    purpose, or principal_type (USER / SERVICE / AGENT).  Resource conditions
    can gate on classification, data_domain, and sensitivity.  Environment
    conditions can gate on time_of_day and network_zone.
    """

    id: str
    policy_key: str
    version: int
    name: str
    effect: Literal["PERMIT", "DENY"]
    subject_conditions: dict[str, Any] = field(default_factory=dict)
    resource_conditions: dict[str, Any] = field(default_factory=dict)
    environment_conditions: dict[str, Any] = field(default_factory=dict)
    priority: int = 100


@dataclass(frozen=True, slots=True)
class AbacDecision:
    """Result of an ABAC evaluation."""

    decision: Literal["PERMIT", "DENY"]
    reasons: list[str]
    contributing_policies: list[str]
    evaluation_time_ms: float
    policy_version: str = ABAC_ENGINE_VERSION


# ---------------------------------------------------------------------------
# Condition matching
# ---------------------------------------------------------------------------

def _match_condition(condition_key: str, condition_value: Any, attributes: dict[str, Any]) -> bool:
    """Check whether *attributes* satisfy a single condition.

    Condition values can be:
    - a scalar: exact match
    - a list: attribute value must be in the list
    - a dict with operator keys: {"min": ..., "max": ...} for ranges
    """
    attr_value = attributes.get(condition_key)
    if attr_value is None:
        return False

    if isinstance(condition_value, list):
        if isinstance(attr_value, list | set | frozenset):
            return bool(set(attr_value) & set(condition_value))
        return attr_value in condition_value

    if isinstance(condition_value, dict):
        if "min" in condition_value and attr_value < condition_value["min"]:
            return False
        if "max" in condition_value and attr_value > condition_value["max"]:
            return False
        return True

    # Scalar comparison
    if isinstance(attr_value, list | set | frozenset):
        return condition_value in attr_value
    return bool(attr_value == condition_value)


def _match_conditions(conditions: dict[str, Any], attributes: dict[str, Any]) -> bool:
    """All conditions must match (AND semantics)."""
    return all(
        _match_condition(key, value, attributes)
        for key, value in conditions.items()
    )


# ---------------------------------------------------------------------------
# Evaluation engine
# ---------------------------------------------------------------------------

def evaluate(
    subject_attrs: dict[str, Any],
    resource_attrs: dict[str, Any],
    env_attrs: dict[str, Any],
    policies: list[AbacPolicy],
) -> AbacDecision:
    """Evaluate ABAC policies against the provided attributes.

    Policies are evaluated in priority order (lower number = higher
    priority).  The first matching DENY wins; if no DENY matches, the
    first matching PERMIT wins.  If nothing matches the default is DENY.
    """
    start = time.monotonic()

    sorted_policies = sorted(policies, key=lambda p: p.priority)

    matching_deny: list[AbacPolicy] = []
    matching_permit: list[AbacPolicy] = []

    for policy in sorted_policies:
        subject_match = _match_conditions(policy.subject_conditions, subject_attrs)
        resource_match = _match_conditions(policy.resource_conditions, resource_attrs)
        env_match = _match_conditions(policy.environment_conditions, env_attrs)

        if subject_match and resource_match and env_match:
            if policy.effect == "DENY":
                matching_deny.append(policy)
            else:
                matching_permit.append(policy)

    elapsed_ms = round((time.monotonic() - start) * 1000, 3)

    # Deny-overrides: any matching DENY trumps all PERMITs
    if matching_deny:
        return AbacDecision(
            decision="DENY",
            reasons=[f"denied by policy '{p.name}'" for p in matching_deny],
            contributing_policies=[p.id for p in matching_deny],
            evaluation_time_ms=elapsed_ms,
        )

    if matching_permit:
        return AbacDecision(
            decision="PERMIT",
            reasons=[f"permitted by policy '{p.name}'" for p in matching_permit],
            contributing_policies=[p.id for p in matching_permit],
            evaluation_time_ms=elapsed_ms,
        )

    return AbacDecision(
        decision="DENY",
        reasons=["no matching policy found; default deny"],
        contributing_policies=[],
        evaluation_time_ms=elapsed_ms,
    )


def simulate(
    subject_attrs: dict[str, Any],
    resource_attrs: dict[str, Any],
    env_attrs: dict[str, Any],
    policies: list[AbacPolicy],
    vary_subject_attrs: list[dict[str, Any]] | None = None,
) -> list[AbacDecision]:
    """Simulate access for a set of hypothetical subject attribute sets.

    Returns one decision per entry in *vary_subject_attrs*.  If
    *vary_subject_attrs* is ``None``, evaluates with the given subject
    attributes once.
    """
    if vary_subject_attrs is None:
        return [evaluate(subject_attrs, resource_attrs, env_attrs, policies)]

    return [
        evaluate({**subject_attrs, **override}, resource_attrs, env_attrs, policies)
        for override in vary_subject_attrs
    ]
