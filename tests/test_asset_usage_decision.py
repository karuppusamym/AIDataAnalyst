"""AT-13: `aida.asset_usage_decision.compute_usage_decision` -- pure, DB-free
decision logic, tested in total isolation from the MCP composition/dispatch
code (`aida.mcp_server`/`aida.asset_context`). No database, no session, no
mocking: every test below calls the function directly with plain values.
"""

from __future__ import annotations

import itertools

import pytest

from aida.asset_usage_decision import compute_usage_decision

_CERTIFICATION_STATES = ("NONE", "CERTIFIED", "EXPIRED", "REVOKED")
_QUALITY_STATES = ("INCIDENT_OPEN", "STALE", "PASSING", "UNKNOWN")


def test_certified_healthy_owned_unclassified_is_allowed() -> None:
    """The tracker row's own example: "certified + healthy quality + no open
    incidents -> ALLOWED"."""
    result = compute_usage_decision(
        certification_state="CERTIFIED",
        quality_state="PASSING",
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=False,
    )

    assert result.decision == "ALLOWED"
    assert all(factor.flag == "OK" for factor in result.factors)


def test_open_critical_incident_blocks_regardless_of_everything_else() -> None:
    """The tracker row's own example: "an open critical quality incident ->
    BLOCKED"."""
    result = compute_usage_decision(
        certification_state="CERTIFIED",
        quality_state="INCIDENT_OPEN",
        has_open_critical_incident=True,
        has_owner=True,
        has_sensitive_classification=False,
    )

    assert result.decision == "BLOCKED"
    incident_factor = next(
        f for f in result.factors if f.name == "open_critical_quality_incident"
    )
    assert incident_factor.flag == "BLOCKED"


def test_revoked_certification_blocks_even_with_passing_quality() -> None:
    result = compute_usage_decision(
        certification_state="REVOKED",
        quality_state="PASSING",
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=False,
    )

    assert result.decision == "BLOCKED"
    cert_factor = next(f for f in result.factors if f.name == "certification_state")
    assert cert_factor.flag == "BLOCKED"


def test_uncertified_and_unowned_is_caution_not_blocked() -> None:
    """The tracker row's own example: "uncertified + no owner -> caution"."""
    result = compute_usage_decision(
        certification_state="NONE",
        quality_state="PASSING",
        has_open_critical_incident=False,
        has_owner=False,
        has_sensitive_classification=False,
    )

    assert result.decision == "ALLOWED_WITH_CAUTION"


@pytest.mark.parametrize("certification_state", ["NONE", "EXPIRED"])
def test_uncertified_or_expired_alone_is_caution(certification_state: str) -> None:
    result = compute_usage_decision(
        certification_state=certification_state,
        quality_state="PASSING",
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=False,
    )

    assert result.decision == "ALLOWED_WITH_CAUTION"


def test_non_critical_open_incident_is_caution_not_blocked() -> None:
    result = compute_usage_decision(
        certification_state="CERTIFIED",
        quality_state="INCIDENT_OPEN",
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=False,
    )

    assert result.decision == "ALLOWED_WITH_CAUTION"
    incident_factor = next(
        f for f in result.factors if f.name == "open_critical_quality_incident"
    )
    quality_factor = next(f for f in result.factors if f.name == "quality_state")
    assert incident_factor.flag == "CAUTION"
    assert quality_factor.flag == "CAUTION"


@pytest.mark.parametrize("quality_state", ["STALE", "UNKNOWN"])
def test_stale_or_unknown_quality_alone_is_caution(quality_state: str) -> None:
    result = compute_usage_decision(
        certification_state="CERTIFIED",
        quality_state=quality_state,
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=False,
    )

    assert result.decision == "ALLOWED_WITH_CAUTION"
    quality_factor = next(f for f in result.factors if f.name == "quality_state")
    assert quality_factor.flag == "CAUTION"


def test_no_owner_alone_is_caution() -> None:
    result = compute_usage_decision(
        certification_state="CERTIFIED",
        quality_state="PASSING",
        has_open_critical_incident=False,
        has_owner=False,
        has_sensitive_classification=False,
    )

    assert result.decision == "ALLOWED_WITH_CAUTION"
    owner_factor = next(f for f in result.factors if f.name == "has_owner")
    assert owner_factor.flag == "CAUTION"


def test_sensitive_classification_alone_is_caution() -> None:
    result = compute_usage_decision(
        certification_state="CERTIFIED",
        quality_state="PASSING",
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=True,
    )

    assert result.decision == "ALLOWED_WITH_CAUTION"
    classification_factor = next(
        f for f in result.factors if f.name == "has_sensitive_classification"
    )
    assert classification_factor.flag == "CAUTION"


def test_critical_incident_beats_caution_signals_present_simultaneously() -> None:
    # Uncertified AND no owner AND sensitive AND a critical incident: the
    # decision is still just BLOCKED (the worst single flag), not some
    # stacked/compounded state -- but every factor stays visible.
    result = compute_usage_decision(
        certification_state="NONE",
        quality_state="INCIDENT_OPEN",
        has_open_critical_incident=True,
        has_owner=False,
        has_sensitive_classification=True,
    )

    assert result.decision == "BLOCKED"
    assert len(result.factors) == 5


def test_unknown_certification_state_raises() -> None:
    with pytest.raises(ValueError, match="certification_state"):
        compute_usage_decision(
            certification_state="BOGUS",
            quality_state="PASSING",
            has_open_critical_incident=False,
            has_owner=True,
            has_sensitive_classification=False,
        )


def test_unknown_quality_state_raises() -> None:
    with pytest.raises(ValueError, match="quality_state"):
        compute_usage_decision(
            certification_state="CERTIFIED",
            quality_state="BOGUS",
            has_open_critical_incident=False,
            has_owner=True,
            has_sensitive_classification=False,
        )


def test_result_serializes_every_factor_never_a_bare_label() -> None:
    result = compute_usage_decision(
        certification_state="EXPIRED",
        quality_state="STALE",
        has_open_critical_incident=False,
        has_owner=False,
        has_sensitive_classification=True,
    )

    body = result.as_dict()

    assert body["decision"] == "ALLOWED_WITH_CAUTION"
    assert isinstance(body["factors"], list)
    assert len(body["factors"]) == 5
    for factor in body["factors"]:
        assert set(factor) == {"name", "value", "flag"}
        assert factor["flag"] in {"OK", "CAUTION", "BLOCKED"}


def test_decision_is_deterministic_across_repeated_calls() -> None:
    kwargs = dict(
        certification_state="CERTIFIED",
        quality_state="INCIDENT_OPEN",
        has_open_critical_incident=False,
        has_owner=True,
        has_sensitive_classification=False,
    )

    results = [compute_usage_decision(**kwargs) for _ in range(5)]

    assert all(result.as_dict() == results[0].as_dict() for result in results)


def test_every_combination_of_states_produces_a_decision_without_raising() -> None:
    """Exhaustive over the full combinatorial input space (4 certification
    states x 4 quality states x 2 x 2 x 2 booleans = 256 combinations):
    every one maps to exactly one of the three valid decisions,
    deterministically, and the worst-factor invariant always holds."""
    for (
        certification_state,
        quality_state,
        has_open_critical_incident,
        has_owner,
        has_sensitive_classification,
    ) in itertools.product(
        _CERTIFICATION_STATES, _QUALITY_STATES, (False, True), (False, True), (False, True)
    ):
        result = compute_usage_decision(
            certification_state=certification_state,
            quality_state=quality_state,
            has_open_critical_incident=has_open_critical_incident,
            has_owner=has_owner,
            has_sensitive_classification=has_sensitive_classification,
        )

        assert result.decision in {"ALLOWED", "ALLOWED_WITH_CAUTION", "BLOCKED"}
        assert len(result.factors) == 5

        flags = {factor.flag for factor in result.factors}
        if "BLOCKED" in flags:
            assert result.decision == "BLOCKED"
        elif "CAUTION" in flags:
            assert result.decision == "ALLOWED_WITH_CAUTION"
        else:
            assert result.decision == "ALLOWED"

        # Repeating the same call yields the identical result -- determinism
        # holds at every point in the combinatorial space, not just the
        # hand-picked scenarios above.
        repeat = compute_usage_decision(
            certification_state=certification_state,
            quality_state=quality_state,
            has_open_critical_incident=has_open_critical_incident,
            has_owner=has_owner,
            has_sensitive_classification=has_sensitive_classification,
        )
        assert repeat.as_dict() == result.as_dict()
