"""AT-13: pure, DB-free `usage_decision` logic for `get_asset_context`.

`Docs/60-delivery/03-tracker.md` AT-13's exit criterion names a real failure
mode: Atlan's own MCP transcript has the *model* concluding "safe to use,
ensure your pipeline respects that policy" -- the model acting as policy
oracle and handing enforcement back to the caller. The fix is not a better
prompt; it is a decision the server computes deterministically from signals
it already has, with every contributing factor named in the response, so an
agent (or a human) never has to trust a model's summary of whether an asset
is safe to use.

This module knows nothing about sessions, tables, or SQL -- it takes already
composed state (certification state, quality state, whether an open
CRITICAL incident exists, whether an owner is assigned, whether any column
carries a sensitive classification) and returns a decision plus the factor
list that produced it. `aida.asset_context`/`aida.mcp_server` compose those
inputs from the real data; this function is what stays unit-testable in
total isolation from all of that.

Decision table (each factor's own flag, then the call's overall decision is
the worst flag across all factors -- BLOCKED beats CAUTION beats OK):

``certification_state``
    ``REVOKED`` -> BLOCKED (a revoked certification is a standing refusal,
    stronger than "never certified"). ``CERTIFIED`` -> OK. ``NONE`` /
    ``EXPIRED`` -> CAUTION (uncertified or lapsed, not refused).
``open_critical_quality_incident``
    An open (``OPEN``/``ACKNOWLEDGED``) ``CRITICAL``-severity
    `DataQualityIncident` -> BLOCKED. A non-critical open incident (quality
    state ``INCIDENT_OPEN`` without a critical one) -> CAUTION.
``quality_state``
    Mirrors the incident factor when quality state is ``INCIDENT_OPEN``
    (same underlying fact, not double-counted); ``STALE``/``UNKNOWN`` ->
    CAUTION (no recent observation to stand behind); ``PASSING`` -> OK.
``has_owner``
    No assigned owner -> CAUTION (nobody accountable to ask). Present -> OK.
``has_sensitive_classification``
    Any column carrying a `aida.classification.SENSITIVE_CLASSES` value ->
    CAUTION (handling constraints apply regardless of quality/certification
    health). None -> OK.

``ALLOWED`` therefore requires: certified, no open incident of any kind,
recently-observed passing quality, an assigned owner, and no sensitive
column -- the "certified + healthy quality + no open incidents" example in
the tracker row, made concrete and total over every combination.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UsageDecision = Literal["ALLOWED", "ALLOWED_WITH_CAUTION", "BLOCKED"]
FactorFlag = Literal["OK", "CAUTION", "BLOCKED"]

_CERTIFICATION_STATES = frozenset({"NONE", "CERTIFIED", "EXPIRED", "REVOKED"})
_QUALITY_STATES = frozenset({"INCIDENT_OPEN", "STALE", "PASSING", "UNKNOWN"})

_FLAG_RANK: dict[FactorFlag, int] = {"OK": 0, "CAUTION": 1, "BLOCKED": 2}


@dataclass(frozen=True, slots=True)
class UsageDecisionFactor:
    """One input the decision was computed from -- named, valued, and its own
    contribution shown, so the response is never a bare label (AT-13's
    explicit rejection of "safe to use, trust me")."""

    name: str
    value: str
    flag: FactorFlag

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value, "flag": self.flag}


@dataclass(frozen=True, slots=True)
class UsageDecisionResult:
    decision: UsageDecision
    factors: tuple[UsageDecisionFactor, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "factors": [factor.as_dict() for factor in self.factors],
        }


def compute_usage_decision(
    *,
    certification_state: str,
    quality_state: str,
    has_open_critical_incident: bool,
    has_owner: bool,
    has_sensitive_classification: bool,
) -> UsageDecisionResult:
    """Deterministic, explainable, server-side usage decision.

    Raises `ValueError` for a state outside the vocabulary
    `catalog_read_model._certification_state`/`_quality_state` actually
    produce -- an unrecognized state should fail loudly here, not silently
    fall through to a permissive default.
    """
    if certification_state not in _CERTIFICATION_STATES:
        raise ValueError(f"unknown certification_state: {certification_state!r}")
    if quality_state not in _QUALITY_STATES:
        raise ValueError(f"unknown quality_state: {quality_state!r}")

    factors: list[UsageDecisionFactor] = []

    if certification_state == "REVOKED":
        cert_flag: FactorFlag = "BLOCKED"
    elif certification_state == "CERTIFIED":
        cert_flag = "OK"
    else:  # NONE, EXPIRED
        cert_flag = "CAUTION"
    factors.append(UsageDecisionFactor("certification_state", certification_state, cert_flag))

    if has_open_critical_incident:
        incident_flag: FactorFlag = "BLOCKED"
    elif quality_state == "INCIDENT_OPEN":
        incident_flag = "CAUTION"
    else:
        incident_flag = "OK"
    factors.append(
        UsageDecisionFactor(
            "open_critical_quality_incident", str(has_open_critical_incident), incident_flag
        )
    )

    if quality_state == "INCIDENT_OPEN":
        # Same underlying fact as the incident factor above -- reuse its
        # flag rather than compute a second, potentially inconsistent one.
        quality_flag: FactorFlag = incident_flag
    elif quality_state == "PASSING":
        quality_flag = "OK"
    else:  # STALE, UNKNOWN
        quality_flag = "CAUTION"
    factors.append(UsageDecisionFactor("quality_state", quality_state, quality_flag))

    owner_flag: FactorFlag = "OK" if has_owner else "CAUTION"
    factors.append(UsageDecisionFactor("has_owner", str(has_owner), owner_flag))

    classification_flag: FactorFlag = "CAUTION" if has_sensitive_classification else "OK"
    factors.append(
        UsageDecisionFactor(
            "has_sensitive_classification",
            str(has_sensitive_classification),
            classification_flag,
        )
    )

    worst_rank = max(_FLAG_RANK[factor.flag] for factor in factors)
    decision: UsageDecision = (
        "BLOCKED" if worst_rank == 2 else "ALLOWED_WITH_CAUTION" if worst_rank == 1 else "ALLOWED"
    )

    return UsageDecisionResult(decision=decision, factors=tuple(factors))
