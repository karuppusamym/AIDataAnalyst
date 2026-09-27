"""R11-C6 hole 2: who may change an agent's contract, and how far.

`put_agent_contract` and the kill-switch endpoints gated on a role
(`CONTRACT_AUTHORS`) plus an organization, and nothing else.
`validate_contract_definition` refuses naming *yourself* as the agent
principal, which stops one developer supervising their own agent -- but two
developers editing each other's agents defeats that in one step, and the
plainer problem needed no collusion at all: any single `AgentDeveloper` could
widen any other agent's envelope anywhere in the organization, or release the
kill switch an operator had just engaged.

The control implemented, in two halves:

1. **Ownership.** A direct contract control is bound to
   `AiAssetVersion.owner_principal` -- the accountable human the platform
   already recorded against the agent version. `PlatformAdmin` is the
   break-glass, and is audited as one.
2. **Widening goes to review.** `contract_widening` names the dimensions an
   edit would loosen; any of them refuses the direct write with a 409 that
   points at `POST .../agent-contract-requests`, which is maker != checker
   *and* eval-gated at decision time. Tightening stays a one-principal
   action, deliberately.

Plus maker != checker on releasing a kill switch, read from the audit log --
which `AgentContract`'s own docstring already names as where the engage/
release history lives.

Removing any one of these makes a test here fail:

- drop the `_require_agent_steward` call from `put_agent_contract` and
  `test_a_developer_cannot_edit_another_developers_agent_contract` returns a
  contract instead of raising 403;
- drop the `contract_widening` branch and every
  `test_widening_*` case applies the widened contract and returns 200;
- drop the `_require_agent_steward` call from the release path and
  `test_a_non_owner_cannot_release_another_agents_kill_switch` releases it;
- drop the `engaged_by` comparison and
  `test_the_principal_who_engaged_a_kill_switch_cannot_release_it` releases it.

None of these paths had *any* test before this file, which is a fair part of
why the hole survived a clause-by-clause audit of everything around it.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida.agent_contract_api import (
    AgentContractWrite,
    CapabilityEnvelopeModel,
    KillSwitchRequest,
    _definition_from,
    _kill_switch_engaged_by,
    engage_agent_kill_switch,
    put_agent_contract,
    release_agent_kill_switch,
)
from aida.agent_contracts import (
    REASON_WIDENING_NEEDS_REVIEW,
    contract_widening,
)
from aida.models import AgentContract, AiAsset, AiAssetVersion, AuditEvent
from aida.security import SecurityContext

OWNER = "alice@bank.example"
OTHER_DEVELOPER = "bob@bank.example"
OPERATOR = "ops@bank.example"
ORG = uuid4()
VERSION_ID = uuid4()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _ContractSession:
    """`agent_contract_api`'s session surface for one contract control.

    `execute` answers `load_agent_asset_version`'s join with the
    `(asset, version)` pair. `scalar` answers `load_agent_contract` with the
    stored contract, and then `_kill_switch_engaged_by` with the engaging
    principal -- both are single-value reads, served from a queue in call
    order so a test can say exactly what the database holds.
    """

    def __init__(
        self,
        *,
        asset: AiAsset,
        version: AiAssetVersion,
        scalars: list[object],
    ) -> None:
        self._row = (asset, version)
        self._scalar_queue = list(scalars)
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement: object) -> object:
        row = self._row

        class _Result:
            def first(self_inner) -> object:
                return row

        return _Result()

    async def scalar(self, _statement: object) -> object:
        return self._scalar_queue.pop(0) if self._scalar_queue else None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
        # The primary-key default and `TimestampMixin`'s two columns are
        # applied by the database on flush, and `_contract_read` serializes
        # all three -- so a create-path test would otherwise fail on
        # serialization rather than on the control under test.
        stamped = datetime.now(UTC)
        for value in self.added:
            if isinstance(value, AgentContract) and value.created_at is None:
                value.id = value.id or uuid4()
                value.created_at = stamped
                value.updated_at = stamped


def _asset_and_version() -> tuple[AiAsset, AiAssetVersion]:
    asset = AiAsset(
        id=uuid4(),
        organization_id=ORG,
        asset_kind="AGENT",
        created_by=OWNER,
    )
    version = AiAssetVersion(
        id=VERSION_ID,
        organization_id=ORG,
        asset_id=asset.id,
        version=1,
        owner_principal=OWNER,
        created_by=OWNER,
    )
    return asset, version


def _stored(**changes: Any) -> AgentContract:
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": ORG,
        "ai_asset_version_id": VERSION_ID,
        "agent_principal_id": "agent:revenue-bot",
        "capability_envelope": {
            "tool_slugs": ["quarterly_revenue"],
            "context_product_ids": ["revenue_context"],
            "write_lanes": [],
        },
        "autonomy_tier": "T1",
        "supervisor_persona": "STEWARD",
        "kill_scope": "ALL",
        "kill_engaged": False,
        "sampling_rate": 0.25,
        "daily_token_cap": 100_000,
        "per_run_token_cap": 5_000,
        "wall_clock_seconds_cap": 60,
        "eval_gate_threshold": 0.8,
        "created_by": OWNER,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(changes)
    return AgentContract(**values)


def _write(**changes: Any) -> AgentContractWrite:
    """A PUT body that matches `_stored()` exactly, so any single change a
    test makes is the only thing under test."""
    values: dict[str, Any] = {
        "agent_principal_id": "agent:revenue-bot",
        "capability_envelope": CapabilityEnvelopeModel(
            tool_slugs=["quarterly_revenue"],
            context_product_ids=["revenue_context"],
            write_lanes=[],
        ),
        "autonomy_tier": "T1",
        "supervisor_persona": "STEWARD",
        "kill_scope": "ALL",
        "sampling_rate": 0.25,
        "daily_token_cap": 100_000,
        "per_run_token_cap": 5_000,
        "wall_clock_seconds_cap": 60,
        "eval_gate_threshold": 0.8,
    }
    values.update(changes)
    return AgentContractWrite(**values)


def _context(principal_id: str, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=ORG,
        roles=frozenset(roles or ("AgentDeveloper",)),
    )


def _audit(session: _ContractSession) -> list[AuditEvent]:
    return [value for value in session.added if isinstance(value, AuditEvent)]


def _reasons(session: _ContractSession) -> list[str]:
    return [str(row.details.get("reason")) for row in _audit(session)]


async def _put(
    body: AgentContractWrite, context: SecurityContext, session: _ContractSession
) -> object:
    return await put_agent_contract(
        ORG, VERSION_ID, body, context, session  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# contract_widening: the pure rule
# ---------------------------------------------------------------------------


def test_an_identical_rewrite_widens_nothing() -> None:
    """The idempotent second PUT the endpoint's own docstring promises has to
    keep working, or the control would forbid re-submitting a form."""
    assert contract_widening(_stored(), _definition_from(_write())) == ()


def test_narrowing_every_dimension_widens_nothing() -> None:
    """Tightening the leash stays a one-principal action. An operator who
    has to convene a committee to *reduce* an agent's blast radius will
    leave it wide instead, which is the opposite of the control's purpose.
    """
    narrower = _write(
        capability_envelope=CapabilityEnvelopeModel(
            tool_slugs=[], context_product_ids=[], write_lanes=[]
        ),
        autonomy_tier="T0",
        sampling_rate=1.0,
        daily_token_cap=1_000,
        per_run_token_cap=100,
        wall_clock_seconds_cap=5,
        eval_gate_threshold=0.95,
    )

    assert contract_widening(_stored(), _definition_from(narrower)) == ()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {
                "capability_envelope": CapabilityEnvelopeModel(
                    tool_slugs=["quarterly_revenue", "customer_pii_export"],
                    context_product_ids=["revenue_context"],
                )
            },
            "capability_envelope.tool_slugs",
            id="a-new-tool-slug",
        ),
        pytest.param(
            {
                "capability_envelope": CapabilityEnvelopeModel(
                    tool_slugs=["quarterly_revenue"],
                    context_product_ids=["revenue_context", "hr_compensation_context"],
                )
            },
            "capability_envelope.context_product_ids",
            id="a-new-context-product",
        ),
        pytest.param({"autonomy_tier": "T3"}, "autonomy_tier", id="a-higher-tier"),
        pytest.param({"sampling_rate": 0.05}, "sampling_rate", id="less-auditing"),
        pytest.param(
            {"daily_token_cap": 10_000_000}, "daily_token_cap", id="a-raised-cap"
        ),
        pytest.param({"daily_token_cap": None}, "daily_token_cap", id="no-cap-at-all"),
        pytest.param(
            {"per_run_token_cap": None}, "per_run_token_cap", id="no-per-run-cap"
        ),
        pytest.param(
            {"wall_clock_seconds_cap": 3_600},
            "wall_clock_seconds_cap",
            id="a-longer-run",
        ),
        pytest.param(
            {"eval_gate_threshold": 0.1},
            "eval_gate_threshold",
            id="a-lower-eval-bar",
        ),
        pytest.param(
            {"eval_gate_threshold": None}, "eval_gate_threshold", id="no-eval-gate"
        ),
        pytest.param(
            {"agent_principal_id": "agent:some-other-bot"},
            "agent_principal_id",
            id="a-different-workload-identity",
        ),
    ],
)
def test_each_widening_dimension_is_detected(changes: dict[str, Any], expected: str) -> None:
    assert expected in contract_widening(_stored(), _definition_from(_write(**changes)))


def test_narrowing_the_kill_scope_counts_as_widening_the_agent() -> None:
    """The one that reads backwards, and the reason the PUT path can weaken a
    kill switch without ever touching `kill_engaged`.

    `ALL` stops every agent in the organization; `AGENT` stops only this one.
    Editing `ALL` down to `AGENT` therefore disengages the switch for
    everything else it was covering -- a smaller *scope* is a larger
    *licence*.
    """
    assert "kill_scope" in contract_widening(
        _stored(kill_scope="ALL"), _definition_from(_write(kill_scope="AGENT"))
    )
    assert "kill_scope" not in contract_widening(
        _stored(kill_scope="AGENT"), _definition_from(_write(kill_scope="ALL"))
    )


def test_an_unrecognised_stored_enum_value_routes_the_edit_to_review() -> None:
    """Fail closed where the order cannot answer.

    A `kill_scope` or `autonomy_tier` the enum no longer contains cannot be
    compared. Answering "not widened" would let a kill scope loosen on a
    technicality, so any change away from an uncomparable value counts --
    and an identical rewrite of one still does not.
    """
    stale = _stored(kill_scope="LEGACY_EVERYTHING", autonomy_tier="T9")
    widened = contract_widening(stale, _definition_from(_write()))

    assert "kill_scope" in widened
    assert "autonomy_tier" in widened
    assert contract_widening(
        stale, _definition_from(_write(kill_scope="ALL", autonomy_tier="T1"))
    ) == ("autonomy_tier", "kill_scope")


def test_an_unparseable_stored_envelope_makes_everything_new() -> None:
    """Matches `envelope_violation`: an envelope the platform cannot
    interpret allows nothing, so it can only be widened, never matched.
    """
    corrupt = _stored(capability_envelope={"tool_slugs": "not-a-list"})

    widened = contract_widening(corrupt, _definition_from(_write()))

    assert "capability_envelope.tool_slugs" in widened
    assert "capability_envelope.context_product_ids" in widened


def test_widening_is_reported_sorted_and_complete() -> None:
    """A refusal has to be able to name everything it objected to, in a
    stable order an audit row years later still parses."""
    widened = contract_widening(
        _stored(),
        _definition_from(
            _write(
                autonomy_tier="T3",
                daily_token_cap=None,
                sampling_rate=0.05,
            )
        ),
    )

    assert widened == ("autonomy_tier", "daily_token_cap", "sampling_rate")


# ---------------------------------------------------------------------------
# put_agent_contract: ownership
# ---------------------------------------------------------------------------


async def test_a_developer_cannot_edit_another_developers_agent_contract() -> None:
    """The hole, stated plainly. `bob` holds `AgentDeveloper` in the same
    organization and is not this agent's registered owner.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(asset=asset, version=version, scalars=[_stored()])

    with pytest.raises(HTTPException) as denied:
        await _put(_write(), _context(OTHER_DEVELOPER), session)

    assert denied.value.status_code == 403
    assert "registered owner" in str(denied.value.detail)
    assert "agent_contract_not_steward" in _reasons(session)


async def test_the_registered_owner_may_still_correct_their_own_contract() -> None:
    """The path's stated purpose survives: a correction by the accountable
    owner that takes nothing away from the platform applies directly.
    """
    asset, version = _asset_and_version()
    stored = _stored()
    session = _ContractSession(asset=asset, version=version, scalars=[stored])

    read = await _put(_write(wall_clock_seconds_cap=30), _context(OWNER), session)

    assert read.wall_clock_seconds_cap == 30  # type: ignore[attr-defined]
    assert "agent_contract_not_steward" not in _reasons(session)


async def test_a_platform_admin_breaks_the_glass_and_is_audited_for_it() -> None:
    """The break-glass is a real exemption from *ownership* and is recorded
    as one -- it is not an exemption from the widening rule below.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(asset=asset, version=version, scalars=[_stored()])

    read = await _put(
        _write(autonomy_tier="T0"), _context(OPERATOR, "PlatformAdmin"), session
    )

    assert read.autonomy_tier == "T0"  # type: ignore[attr-defined]
    actions = [row.action for row in _audit(session)]
    assert "agent_contract.update.break_glass" in actions


# ---------------------------------------------------------------------------
# put_agent_contract: widening goes to review
# ---------------------------------------------------------------------------


async def test_the_owner_cannot_widen_their_own_agents_envelope_directly() -> None:
    """Ownership is not authority to grant. Adding a tool slug hands the
    agent something it did not have, which is a T3 grant
    (`review_risk_tiers`: `AGENT_CONTRACT` sits with model routes), and this
    platform does not let one principal make a T3 grant alone anywhere else.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(asset=asset, version=version, scalars=[_stored()])
    widening = _write(
        capability_envelope=CapabilityEnvelopeModel(
            tool_slugs=["quarterly_revenue", "customer_pii_export"],
            context_product_ids=["revenue_context"],
        )
    )

    with pytest.raises(HTTPException) as denied:
        await _put(widening, _context(OWNER), session)

    assert denied.value.status_code == 409
    assert "agent contract request" in str(denied.value.detail)
    assert "capability_envelope.tool_slugs" in str(denied.value.detail)
    assert REASON_WIDENING_NEEDS_REVIEW in _reasons(session)


async def test_not_even_a_platform_admin_widens_an_envelope_directly() -> None:
    """The widening rule has no break-glass. A break-glass on a grant is
    just the grant with extra steps, and the reviewed path is *available* --
    this is a redirection, not a refusal of the change itself.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(asset=asset, version=version, scalars=[_stored()])

    with pytest.raises(HTTPException) as denied:
        await _put(_write(autonomy_tier="T3"), _context(OPERATOR, "PlatformAdmin"), session)

    assert denied.value.status_code == 409
    assert "autonomy_tier" in str(denied.value.detail)


async def test_weakening_a_kill_scope_through_the_put_path_is_refused() -> None:
    """The PUT never writes `kill_engaged`, so it looked incapable of
    touching a kill switch. `kill_scope` is the back door: `ALL` -> `AGENT`
    leaves the flag alone and stops covering every other agent.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_scope="ALL", kill_engaged=True)]
    )

    with pytest.raises(HTTPException) as denied:
        await _put(_write(kill_scope="AGENT"), _context(OWNER), session)

    assert denied.value.status_code == 409
    assert "kill_scope" in str(denied.value.detail)


async def test_creating_a_first_contract_is_not_treated_as_widening() -> None:
    """Documented boundary, asserted so it cannot drift silently: there is no
    prior authority to widen, the reviewed+eval-gated path already exists for
    onboarding, and ownership still binds the write. The granted dimensions
    are written into the audit row so a first grant is legible as a grant.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(asset=asset, version=version, scalars=[None])

    read = await _put(_write(), _context(OWNER), session)

    assert read.autonomy_tier == "T1"  # type: ignore[attr-defined]
    created = next(row for row in _audit(session) if row.action == "agent_contract.create")
    assert "capability_envelope.tool_slugs" in created.details["granted_on_create"]
    assert "autonomy_tier" in created.details["granted_on_create"]


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------


async def test_anyone_with_the_role_may_still_engage_a_kill_switch() -> None:
    """Deliberately uncontrolled direction. An emergency brake only the
    accountable owner can pull is a brake that is not there at 3am, so
    engaging keeps the plain `CONTRACT_AUTHORS` gate -- ownership and
    maker != checker apply to *releasing*.
    """
    asset, version = _asset_and_version()
    stored = _stored(kill_engaged=False)
    session = _ContractSession(asset=asset, version=version, scalars=[stored])

    read = await engage_agent_kill_switch(
        ORG,
        VERSION_ID,
        _reason("runaway spend"),
        _context(OPERATOR, "AgentDeveloper"),
        session,  # type: ignore[arg-type]
    )

    assert read.kill_engaged is True


async def test_a_non_owner_cannot_release_another_agents_kill_switch() -> None:
    asset, version = _asset_and_version()
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_engaged=True)]
    )

    with pytest.raises(HTTPException) as denied:
        await release_agent_kill_switch(
            ORG,
            VERSION_ID,
            _reason("looks fine to me"),
            _context(OTHER_DEVELOPER),
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 403
    assert "agent_contract_not_steward" in _reasons(session)


async def test_the_principal_who_engaged_a_kill_switch_cannot_release_it() -> None:
    """Maker != checker on the switch, the same INV-8 rule every
    `GovernanceReview` decision in this codebase already applies. The owner
    engaged it; the owner does not get to decide it was a false alarm alone.
    """
    asset, version = _asset_and_version()
    # scalar order: load_agent_contract, then _kill_switch_engaged_by.
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_engaged=True), OWNER]
    )

    with pytest.raises(HTTPException) as denied:
        await release_agent_kill_switch(
            ORG,
            VERSION_ID,
            _reason("false alarm"),
            _context(OWNER),
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert "maker-checker" in str(denied.value.detail)
    assert "agent_kill_switch_self_release" in _reasons(session)


async def test_a_delegate_cannot_release_what_their_delegator_engaged() -> None:
    """Self-release by proxy, closed the same way
    `governance_decision_service.check_decision_permitted` closes
    self-approval by proxy. Without this, a delegation grant would launder
    the check.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_engaged=True), OPERATOR]
    )
    delegate = replace(
        _context(OWNER),
        active_delegation_id=uuid4(),
        active_delegator_principal_id=OPERATOR,
    )

    with pytest.raises(HTTPException) as denied:
        await release_agent_kill_switch(
            ORG,
            VERSION_ID,
            _reason("my delegator says it is fine"),
            delegate,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert "agent_kill_switch_self_release" in _reasons(session)


async def test_the_owner_releases_a_switch_a_different_principal_engaged() -> None:
    """The normal case the control must not break: an operator stops the
    agent, the accountable owner fixes it and lifts the stop. Two
    principals, which is the whole point.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_engaged=True), OPERATOR]
    )

    read = await release_agent_kill_switch(
        ORG,
        VERSION_ID,
        _reason("root caused and patched"),
        _context(OWNER),
        session,  # type: ignore[arg-type]
    )

    assert read.kill_engaged is False
    released = next(
        row for row in _audit(session) if row.action == "agent_contract.release"
    )
    assert released.details["engaged_by"] == OPERATOR


async def test_a_release_with_no_engagement_evidence_still_enforces_ownership() -> None:
    """The fail-safe, asserted so its weakness is on the record rather than
    discovered later.

    An engagement older than the audit retention window leaves nothing to
    compare against. A kill switch nobody can ever release is its own
    outage, so the release proceeds on ownership alone -- and the audit row
    records `engaged_by: null`, which is what tells an auditor that this
    particular release cleared the weaker of the two checks.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_engaged=True), None]
    )

    read = await release_agent_kill_switch(
        ORG,
        VERSION_ID,
        _reason("stale engagement"),
        _context(OWNER),
        session,  # type: ignore[arg-type]
    )

    assert read.kill_engaged is False
    released = next(
        row for row in _audit(session) if row.action == "agent_contract.release"
    )
    assert released.details["engaged_by"] is None


async def test_a_non_owner_platform_admin_may_release_as_break_glass() -> None:
    """Someone has to be able to lift a stop when the owner is unreachable,
    and maker != checker still applies to them.
    """
    asset, version = _asset_and_version()
    session = _ContractSession(
        asset=asset, version=version, scalars=[_stored(kill_engaged=True), OWNER]
    )

    read = await release_agent_kill_switch(
        ORG,
        VERSION_ID,
        _reason("owner on leave, incident closed"),
        _context(OPERATOR, "PlatformAdmin"),
        session,  # type: ignore[arg-type]
    )

    assert read.kill_engaged is False
    actions = [row.action for row in _audit(session)]
    assert "agent_contract.release.break_glass" in actions


async def test_the_engagement_lookup_is_scoped_and_only_reads_successes() -> None:
    """INV-5 on the evidence read itself. The engager is resolved from rows
    for *this* organization and *this* agent version, from successful
    engagements only -- a DENIED attempt by someone else must never become
    the principal a release is held against, and neither must a release row.
    """
    captured: list[str] = []

    class _Capture:
        async def scalar(self, statement: object) -> object:
            captured.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
            return None

    assert (
        await _kill_switch_engaged_by(
            _Capture(),  # type: ignore[arg-type]
            organization_id=ORG,
            ai_asset_version_id=VERSION_ID,
        )
        is None
    )

    compiled = captured[0]
    # SQLAlchemy renders a bound UUID literal without its dashes.
    assert ORG.hex in compiled
    assert str(VERSION_ID) in compiled
    assert "agent_contract.kill" in compiled
    assert "'SUCCESS'" in compiled
    assert "agent_contract.release" not in compiled


def _reason(text: str) -> KillSwitchRequest:
    return KillSwitchRequest(reason=text)


def test_the_fixtures_describe_a_real_agent_version() -> None:
    asset, version = _asset_and_version()
    assert asset.asset_kind == "AGENT"
    assert version.owner_principal == OWNER
    assert version.owner_principal != "agent:revenue-bot"
    assert isinstance(version.id, UUID)
    assert _stored().created_at <= datetime.now(UTC) + timedelta(seconds=1)
