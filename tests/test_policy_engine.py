"""Unit coverage for attribute-based access evaluation (ADR-0018, policy_engine.py).

The engine is pure by design -- no session, no I/O -- so every one of these runs
without infrastructure. The properties asserted here are the ones the access model
is actually sold on, so each test names the property rather than the function.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from aida.policy_engine import (
    ACTIONS,
    PolicyRecord,
    Resource,
    Subject,
    evaluate,
    simulate,
)

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _policy(
    *,
    effect: str = "ALLOW",
    priority: int = 100,
    subject: dict | None = None,
    resource: dict | None = None,
    actions: tuple[str, ...] = (),
    transform: dict | None = None,
    condition: dict | None = None,
    code: str = "p",
    identifier: UUID | None = None,
) -> PolicyRecord:
    return PolicyRecord(
        id=identifier or uuid4(),
        code=code,
        version=1,
        effect=effect,
        priority=priority,
        subject_match=subject or {},
        resource_match=resource or {},
        action_match=actions,
        transform=transform or {},
        condition=condition or {},
    )


def _subject(**overrides: object) -> Subject:
    base: dict[str, object] = {
        "principal_id": "alice",
        "principal_kind": "HUMAN",
        "roles": frozenset({"Analyst"}),
        "workspace_id": uuid4(),
        "purpose": None,
    }
    base.update(overrides)
    return Subject(**base)  # type: ignore[arg-type]


def _resource(**overrides: object) -> Resource:
    base: dict[str, object] = {"resource_type": "TABLE", "resource_id": "tbl_1"}
    base.update(overrides)
    return Resource(**base)  # type: ignore[arg-type]


def test_an_empty_policy_set_denies_everything() -> None:
    """INV-4. A control plane that has loaded no policies must refuse, not permit.

    The opposite default -- "no policies, so nothing is restricted" -- is the
    failure mode that ends up in an incident report.
    """
    decision = evaluate((), _subject(), _resource(), "READ_DATA", now=_NOW)
    assert decision.allowed is False
    assert decision.reason_code == "NO_APPLICABLE_ALLOW_POLICY"


def test_an_allow_matching_the_subject_role_permits_the_action() -> None:
    policies = (_policy(subject={"roles": ["Analyst"]}, actions=("READ_DATA",)),)
    decision = evaluate(policies, _subject(), _resource(), "READ_DATA", now=_NOW)
    assert decision.allowed is True
    assert decision.reason_code == "ALLOWED_BY_POLICY"


def test_a_policy_with_no_action_match_applies_to_every_action() -> None:
    policies = (_policy(subject={"roles": ["Analyst"]}),)
    for action in sorted(ACTIONS):
        assert evaluate(policies, _subject(), _resource(), action, now=_NOW).allowed is True


def test_a_role_the_subject_does_not_hold_does_not_match() -> None:
    policies = (_policy(subject={"roles": ["Steward"]}),)
    assert evaluate(policies, _subject(), _resource(), "READ_DATA", now=_NOW).allowed is False


@pytest.mark.parametrize("allow_priority", [1, 100, 10_000])
def test_deny_beats_allow_at_every_priority(allow_priority: int) -> None:
    """DENY is a hard ceiling, not a high-priority ALLOW.

    A model where a sufficiently privileged principal can out-prioritise a deny is
    not one a bank control framework accepts, so priority must not be able to lift
    a deny at any value -- including one above the deny's own.
    """
    policies = (
        _policy(effect="ALLOW", priority=allow_priority, subject={"roles": ["Analyst"]}),
        _policy(effect="DENY", priority=1, resource={"classifications": ["PII"]}),
    )
    decision = evaluate(
        policies, _subject(), _resource(classifications=frozenset({"PII"})), "READ_DATA", now=_NOW
    )
    assert decision.allowed is False
    assert decision.reason_code == "DENIED_BY_POLICY"


def test_platform_admin_cannot_override_a_deny() -> None:
    """The same ceiling, stated against the most privileged role the system has."""
    policies = (
        _policy(effect="ALLOW", priority=9_999, subject={"roles": ["PlatformAdmin"]}),
        _policy(effect="DENY", resource={"classifications": ["SECRET"]}),
    )
    decision = evaluate(
        policies,
        _subject(roles=frozenset({"PlatformAdmin"})),
        _resource(classifications=frozenset({"SECRET"})),
        "READ_DATA",
        now=_NOW,
    )
    assert decision.allowed is False


def test_principal_kind_agent_is_expressible_as_a_single_policy() -> None:
    """"Humans may see full account numbers, agents never do" -- one policy.

    This is the control most often asked for once agents reach production and it is
    inexpressible under role-based access alone, which is the concrete argument for
    an attribute-based model.
    """
    policies = (
        _policy(effect="ALLOW", subject={"roles": ["Analyst"]}),
        _policy(
            effect="DENY",
            subject={"principal_kind": "AGENT"},
            resource={"classifications": ["PII"]},
            actions=("READ_DATA",),
        ),
    )
    sensitive = _resource(classifications=frozenset({"PII"}))

    human = evaluate(policies, _subject(principal_kind="HUMAN"), sensitive, "READ_DATA", now=_NOW)
    agent = evaluate(policies, _subject(principal_kind="AGENT"), sensitive, "READ_DATA", now=_NOW)

    assert human.allowed is True
    assert agent.allowed is False
    # The same agent may still read metadata: the deny is scoped to READ_DATA.
    assert evaluate(
        policies, _subject(principal_kind="AGENT"), sensitive, "READ_METADATA", now=_NOW
    ).allowed is True


def test_a_policy_covers_assets_discovered_later_because_it_keys_on_classification() -> None:
    """The whole argument for governance by classification rather than enumeration.

    The policy names no resource id. An asset created after the policy was written
    is governed by it the moment it carries the classification.
    """
    policies = (_policy(effect="DENY", resource={"classifications": ["PCI"]}),)
    brand_new = _resource(resource_id="discovered_next_tuesday", classifications=frozenset({"PCI"}))
    assert evaluate(policies, _subject(), brand_new, "READ_DATA", now=_NOW).allowed is False


def test_business_node_match_uses_the_ancestor_closure() -> None:
    """A policy on a parent node covers assets assigned only to a child.

    The closure is computed by the caller (`business_graph.ancestor_closure`), so
    what this asserts is that the engine matches on the whole closure rather than
    on one node.
    """
    retail = uuid4()
    retail_cards = uuid4()
    policies = (
        _policy(subject={"roles": ["Analyst"]}, resource={"business_node_ids": [str(retail)]}),
    )
    asset_in_child = _resource(business_node_ids=frozenset({retail_cards, retail}))
    asset_elsewhere = _resource(business_node_ids=frozenset({uuid4()}))
    assert evaluate(policies, _subject(), asset_in_child, "READ_DATA", now=_NOW).allowed is True
    assert evaluate(policies, _subject(), asset_elsewhere, "READ_DATA", now=_NOW).allowed is False


def test_schema_pattern_matching() -> None:
    policies = (_policy(subject={"roles": ["Analyst"]}, resource={"schema_pattern": "rtl_*"}),)
    assert evaluate(
        policies, _subject(), _resource(schema_name="rtl_customer"), "READ_DATA", now=_NOW
    ).allowed is True
    assert evaluate(
        policies, _subject(), _resource(schema_name="mkt_customer"), "READ_DATA", now=_NOW
    ).allowed is False
    # A resource with no schema cannot satisfy a schema-scoped policy (fail closed).
    assert evaluate(policies, _subject(), _resource(), "READ_DATA", now=_NOW).allowed is False


def test_mask_obligations_accumulate_across_policies() -> None:
    """Two MASK policies both apply. Taking the union is the only safe resolution."""
    policies = (
        _policy(effect="ALLOW", subject={"roles": ["Analyst"]}),
        _policy(effect="MASK", transform={"classifications": ["PII"]}),
        _policy(effect="MASK", transform={"classifications": ["PCI"], "masking_profile": "STRICT"}),
    )
    decision = evaluate(policies, _subject(), _resource(), "READ_DATA", now=_NOW)
    assert decision.allowed is True
    assert decision.masked_classifications == frozenset({"PII", "PCI"})
    assert decision.masking_profile == "STRICT"


def test_row_filters_accumulate() -> None:
    policies = (
        _policy(effect="ALLOW", subject={"roles": ["Analyst"]}),
        _policy(effect="FILTER", transform={"row_filter": "region = 'EMEA'"}),
        _policy(effect="FILTER", transform={"row_filter": "is_deleted = false"}),
    )
    decision = evaluate(policies, _subject(), _resource(), "READ_DATA", now=_NOW)
    assert sorted(decision.row_filters) == ["is_deleted = false", "region = 'EMEA'"]


def test_time_window_conditions_are_honoured() -> None:
    window = {
        "not_before": (_NOW + timedelta(days=1)).isoformat(),
        "not_after": (_NOW + timedelta(days=2)).isoformat(),
    }
    policies = (_policy(subject={"roles": ["Analyst"]}, condition=window),)
    assert evaluate(policies, _subject(), _resource(), "READ_DATA", now=_NOW).allowed is False
    inside = _NOW + timedelta(days=1, hours=1)
    assert evaluate(policies, _subject(), _resource(), "READ_DATA", now=inside).allowed is True


def test_quality_state_can_gate_access() -> None:
    """Quality coupled to runtime as a policy condition rather than a subsystem.

    "Do not serve this to an agent while its quality incident is open" is a
    condition on an existing policy, which is why ADR-0018 folds the standalone
    data-quality gating module into policy.
    """
    policies = (
        _policy(
            subject={"roles": ["Analyst"]},
            condition={"deny_when_quality_state_in": ["INCIDENT_OPEN"]},
        ),
    )
    healthy = _resource(quality_state="HEALTHY")
    broken = _resource(quality_state="INCIDENT_OPEN")
    assert evaluate(policies, _subject(), healthy, "READ_DATA", now=_NOW).allowed is True
    assert evaluate(policies, _subject(), broken, "READ_DATA", now=_NOW).allowed is False


def test_an_unknown_action_is_refused_rather_than_defaulted() -> None:
    policies = (_policy(),)
    decision = evaluate(policies, _subject(), _resource(), "DROP_EVERYTHING", now=_NOW)
    assert decision.allowed is False
    assert decision.reason_code == "UNKNOWN_ACTION"


def test_the_highest_priority_allow_wins_and_ties_break_deterministically() -> None:
    """A replayed decision must reach the same policy a year later."""
    low = _policy(priority=10, code="low", identifier=UUID(int=1))
    high_a = _policy(priority=500, code="high-a", identifier=UUID(int=2))
    high_b = _policy(priority=500, code="high-b", identifier=UUID(int=3))
    forward = evaluate((low, high_a, high_b), _subject(), _resource(), "READ_DATA", now=_NOW)
    reversed_order = evaluate((high_b, high_a, low), _subject(), _resource(), "READ_DATA", now=_NOW)
    assert forward.matched_policy_code == "high-a"
    assert reversed_order.matched_policy_code == "high-a"


def test_a_decision_never_carries_a_resource_value() -> None:
    """INV-6. A decision is safe to audit: reason codes and identifiers only."""
    policies = (_policy(effect="DENY", resource={"classifications": ["PII"]}),)
    decision = evaluate(
        policies, _subject(), _resource(classifications=frozenset({"PII"})), "READ_DATA", now=_NOW
    )
    # This assertion was originally written as
    #     assert "tbl_1" not in rendered or decision.matched_policy_id is not None
    # whose right-hand side is always true, so the whole test asserted nothing and
    # passed regardless. Kept in the history as a reminder that a green test is not
    # evidence until you have watched it fail.
    rendered = repr(decision)
    assert "tbl_1" not in rendered, "a decision must not echo the resource identifier"
    assert "PII" not in rendered, "a decision must not echo the resource classification"
    assert decision.reason_code == "DENIED_BY_POLICY"
    # The decision exposes no transform expression or policy body, only its identity.
    assert not hasattr(decision, "subject_match")
    assert not hasattr(decision, "resource_match")


# --- PG-8: "who could see this?" ---------------------------------------------


def test_simulate_answers_who_could_see_this_in_one_pass() -> None:
    """PG-8. One resource, several hypothetical subjects, one decision each --
    built directly on `evaluate`, the same engine the query path reaches, not a
    second evaluator that could drift from it."""
    policies = (
        _policy(subject={"principal_kind": "HUMAN"}, actions=("READ_DATA",)),
        _policy(
            effect="DENY",
            subject={"principal_kind": "AGENT"},
            resource={"classifications": ["PII"]},
            actions=("READ_DATA",),
        ),
    )
    resource = _resource(classifications=frozenset({"PII"}))
    subjects = (
        _subject(principal_kind="HUMAN"),
        _subject(principal_kind="AGENT"),
        _subject(principal_kind="SERVICE"),
    )

    decisions = simulate(policies, subjects, resource, "READ_DATA", now=_NOW)

    assert len(decisions) == 3
    assert decisions[0].allowed is True  # HUMAN: matches the human ALLOW
    assert decisions[1].allowed is False  # AGENT: DENY over PII is a hard ceiling
    assert decisions[1].reason_code == "DENIED_BY_POLICY"
    assert decisions[2].allowed is False  # SERVICE: matches no ALLOW at all


def test_simulate_with_no_subjects_returns_no_decisions() -> None:
    decisions = simulate((), (), _resource(), "READ_DATA", now=_NOW)
    assert decisions == ()


def test_simulate_is_equivalent_to_evaluating_each_subject_alone() -> None:
    """Not a second implementation to drift from `evaluate` -- literally built on it."""
    policies = (_policy(subject={"roles": ["Analyst"]}, actions=("READ_DATA",)),)
    subjects = (_subject(roles=frozenset({"Analyst"})), _subject(roles=frozenset({"Viewer"})))

    decisions = simulate(policies, subjects, _resource(), "READ_DATA", now=_NOW)

    expected = tuple(
        evaluate(policies, subject, _resource(), "READ_DATA", now=_NOW) for subject in subjects
    )
    assert decisions == expected
