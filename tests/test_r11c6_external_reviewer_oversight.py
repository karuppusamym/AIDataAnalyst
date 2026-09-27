"""R11-C6: ADR-0027 applied to every non-human decider, not only ours.

ADR-0027's safety case for automated review rests on four things: a risk-tier
ceiling, a sampled fraction humans actually read, a suspension switch an
operator can throw, and the backlog bounds that keep the sample honest. All
four lived in `reviewer_agent.auto_decide_tier0_tier1` -- *above* the decision
service -- so they governed the platform's own reviewer agent and nothing
else.

An externally-supplied agent identity holding the `Reviewer` role reached the
decision endpoints directly and passed only the two checks
`check_decision_permitted` makes: organization, and maker != checker. No
ceiling, so it could approve at any tier. No sample, so nobody was auditing
it. And suspension did not stop it -- which is the serious half, because
suspension is what an operator reaches for when something is going wrong, and
it was believed to have stopped automated review when it had stopped one
implementation of it.

The regime now runs inside `decide_review`, the single point every decision
passes through. These tests drive that function, because "the control was on
one surface" was the defect: four of R11-C6's findings were one missed
surface each, and a control placed per-surface is a control with a list of
surfaces to keep up to date.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from aida import reviewer_agent
from aida.config import Settings
from aida.governance_decision_contracts import (
    AgentOversightOutcome,
    GovernanceDecisionRefused,
)
from aida.governance_decision_service import (
    _AGENT_GUARD,
    decide_review,
    register_agent_decision_guard,
)
from aida.models import GovernanceReview, ReviewAuditSample
from aida.reviewer_agent import REASON_SUSPENDED, REASON_TIER_EXCEEDED
from aida.security import SecurityContext

ORG = uuid4()
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _review(object_type: str = "ASSET_DESCRIPTION_DRAFT") -> GovernanceReview:
    return GovernanceReview(
        id=uuid4(),
        organization_id=ORG,
        object_type=object_type,
        object_id=uuid4(),
        requested_action="PUBLISH",
        status="PENDING",
        requested_by="steward@bank.example",
    )


def _agent(principal_id: str = "agent:third-party-reviewer") -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT",
        organization_id=ORG,
        roles=frozenset({"Reviewer"}),
    )


def _human() -> SecurityContext:
    return SecurityContext(
        principal_id="checker@bank.example",
        principal_type="USER",
        organization_id=ORG,
        roles=frozenset({"Reviewer"}),
    )


class _Session:
    """Enough session for the gate, the claim, and nothing beyond it."""

    def __init__(self, *, claim_wins: bool = True) -> None:
        self.added: list[Any] = []
        self._claim_wins = claim_wins

    async def scalar(self, _statement: object) -> object:
        return None

    async def scalars(self, _statement: object) -> object:
        class _S:
            def all(self_inner) -> list[object]:
                return []

        return _S()

    async def execute(self, _statement: object) -> object:
        wins = self._claim_wins

        class _Result:
            rowcount = 1 if wins else 0

            def first(self_inner) -> object:
                return None

            def all(self_inner) -> list[object]:
                return []

        return _Result()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def refresh(self, _instance: object) -> None:
        # A lost claim re-reads the row to report the state it observed.
        return None


def _samples(session: _Session) -> list[ReviewAuditSample]:
    return [v for v in session.added if isinstance(v, ReviewAuditSample)]


@pytest.fixture
def allow_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard that permits and samples nothing, so a test can isolate the
    service's own behaviour from the policy's."""

    async def _guard(
        session: Any, *, review: Any, decision: str, context: Any
    ) -> AgentOversightOutcome:
        return AgentOversightOutcome()

    monkeypatch.setattr("aida.governance_decision_service._AGENT_GUARD", [_guard])


# --------------------------------------------------------------------------- #
# The regime reaches this path at all
# --------------------------------------------------------------------------- #


def test_the_platform_registers_its_guard_on_import() -> None:
    """Without this the fail-closed branch below is all anyone would ever get,
    and every agent decision would be refused."""
    assert _AGENT_GUARD, "no agent oversight guard is registered"
    assert _AGENT_GUARD[0] is reviewer_agent.agent_decision_oversight


def test_re_registering_a_different_guard_is_refused() -> None:
    """A second registrar must not be able to quietly relax the regime -- the
    same rule the target adapters follow, for the same reason."""

    async def _other(
        session: Any, *, review: Any, decision: str, context: Any
    ) -> AgentOversightOutcome:
        return AgentOversightOutcome()

    with pytest.raises(RuntimeError):
        register_agent_decision_guard(_other)


async def test_an_agent_decision_is_refused_when_no_guard_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-4. An unregistered target adapter means an object type nobody can
    decide, which is noticed at once; an unregistered guard would mean an
    agent nobody supervises, which is noticed by nothing. So absence refuses.
    """
    monkeypatch.setattr("aida.governance_decision_service._AGENT_GUARD", [])

    with pytest.raises(GovernanceDecisionRefused) as excinfo:
        await decide_review(
            _Session(),  # type: ignore[arg-type]
            _review(),
            decision="APPROVE",
            reason="r",
            context=_agent(),
            now=NOW,
        )

    assert excinfo.value.outcome == "NOT_PERMITTED"
    # The detail, not just the outcome: with the gate removed this call falls
    # through to an unregistered adapter, which also answers NOT_PERMITTED --
    # so an outcome-only assertion passes for the wrong reason.
    assert excinfo.value.detail == "agent review oversight is not available"


# --------------------------------------------------------------------------- #
# The three controls, on the external door
# --------------------------------------------------------------------------- #


async def test_suspension_stops_an_external_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect that mattered most. An operator throws the switch believing
    automated review has stopped; before this it had stopped ours only."""
    monkeypatch.setattr(
        reviewer_agent, "organization_suspended", _async_true
    )

    with pytest.raises(GovernanceDecisionRefused) as excinfo:
        await decide_review(
            _Session(),  # type: ignore[arg-type]
            _review(),
            decision="APPROVE",
            reason="r",
            context=_agent(),
            now=NOW,
        )

    assert excinfo.value.detail == REASON_SUSPENDED


async def test_the_tier_ceiling_binds_an_external_agent() -> None:
    """`reviewer_agent_max_tier` clamps to T1, and an object type above the
    ceiling is not the agent's to decide however it arrived."""
    with pytest.raises(GovernanceDecisionRefused) as excinfo:
        await decide_review(
            _Session(),  # type: ignore[arg-type]
            _review(object_type="MODEL_ROUTE_CONFIGURATION"),
            decision="APPROVE",
            reason="r",
            context=_agent(),
            now=NOW,
        )

    assert excinfo.value.detail == REASON_TIER_EXCEEDED


async def test_a_sampled_approval_writes_its_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sampling is what ADR-0027 condition (b) trades for unattended
    decisions, and an external agent was making them unsampled.

    The rate is raised to 1.0 so the sampler selects this review. The real
    sampler still runs -- `sampled_for_audit` is a pure function of the review
    id, so at the default rate the outcome would depend on which `uuid4` the
    fixture happened to produce, which is a coin flip dressed as a test.
    """
    monkeypatch.setattr(reviewer_agent, "get_settings", _sampling_everything)
    session = _Session()
    outcome = await reviewer_agent.agent_decision_oversight(
        session,  # type: ignore[arg-type]
        review=_review(),
        decision="APPROVE",
        context=_agent(),
    )

    assert outcome.reason is None
    assert outcome.sample is not None
    assert outcome.sample.decision == "APPROVED"
    assert outcome.sample.human_outcome == "PENDING"
    assert outcome.sample.agent_principal_id == "agent:third-party-reviewer"


async def test_a_rejection_is_not_sampled() -> None:
    """Condition (b) is about what the agent let *through*."""
    outcome = await reviewer_agent.agent_decision_oversight(
        _Session(),  # type: ignore[arg-type]
        review=_review(),
        decision="REJECT",
        context=_agent(),
    )

    assert outcome.sample is None


# --------------------------------------------------------------------------- #
# Ordering, learned from a real failure
# --------------------------------------------------------------------------- #


async def test_the_sample_is_written_only_after_the_claim_is_won(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AR-04 caught this and a reading of the code would not have.

    With the insert placed before the compare-and-set, three workers racing
    one organization's queue all pass oversight, all add a sample for the same
    review, and the two that lose the claim die on
    `uq_review_audit_sample_review` -- a 23505 that poisons the transaction,
    so a lost claim stops being the orderly refusal the batch shrugs off.
    Observed: `['aborted:23505', 'committed', 'aborted:23505']`.

    A loser writes no sample.
    """
    session = _Session(claim_wins=False)

    with pytest.raises(GovernanceDecisionRefused):
        await decide_review(
            session,  # type: ignore[arg-type]
            _review(),
            decision="APPROVE",
            reason="r",
            context=_agent(),
            now=NOW,
        )

    assert not _samples(session), "a lost claim still wrote an audit sample"


# --------------------------------------------------------------------------- #
# Who this must not touch
# --------------------------------------------------------------------------- #


async def test_a_human_decision_never_reaches_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Reviewer` is a role humans hold. The regime is keyed on the
    authenticated principal *type*, so a human checker is untouched by a
    suspension that exists to stop automation."""
    monkeypatch.setattr(reviewer_agent, "organization_suspended", _async_true)
    called: list[str] = []

    async def _tripwire(
        session: Any, *, review: Any, decision: str, context: Any
    ) -> AgentOversightOutcome:
        called.append(context.principal_id)
        return AgentOversightOutcome()

    monkeypatch.setattr("aida.governance_decision_service._AGENT_GUARD", [_tripwire])

    # Claim lost, so the call stops before any adapter -- this asserts about
    # the gate, and the gate runs before the claim.
    with pytest.raises(GovernanceDecisionRefused):
        await decide_review(
            _Session(claim_wins=False),  # type: ignore[arg-type]
            _review(),
            decision="APPROVE",
            reason="r",
            context=_human(),
            now=NOW,
        )

    assert called == [], "a human decision was put through agent oversight"


async def _async_true(_session: object, _organization_id: UUID) -> bool:
    return True


def _sampling_everything() -> Settings:
    """Real settings with the sample rate at 1.0, so the deterministic sampler
    selects every review instead of one in twenty."""
    return Settings(
        _env_file=None, environment="test", reviewer_agent_sampling_rate=1.0
    )
