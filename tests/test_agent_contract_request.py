"""AG-10 extension: reviewed, eval-gated agent contract requests.

Mirrors `test_agent_eval_gate.py`'s own three-layer shape for the identical
reason: this is the same gate, reused for a second decision branch.

1. The submit endpoint (`submit_agent_contract_request`) -- validation the
   direct-write path already enforces (self-authorization, enum closure)
   must refuse a request exactly the same way, and a valid submission opens
   both the `AgentContractRequest` row and its `GovernanceReview`.
2. The real, shared dispatcher (`semantic_api.decide_governance_review`):
   an APPROVE with no confirmed exemplars is blocked (409, INSUFFICIENT_DATA,
   nothing written); an APPROVE with a passing confirmed corpus activates the
   request and writes a real `AgentContract`; a REJECT never touches
   `AgentContract` at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_contract_api import AgentContractWrite, CapabilityEnvelopeModel
from aida.agent_contract_request_api import (
    AgentContractRequestCreate,
    AgentContractRequestRead,
    get_agent_contract_request,
    list_agent_contract_requests,
    submit_agent_contract_request,
)
from aida.db import Base
from aida.models import (
    AgentContract,
    AgentContractRequest,
    AiAsset,
    AiAssetVersion,
    GovernanceReview,
    Organization,
)
from aida.schemas import GovernanceDecisionRequest
from aida.semantic_api import decide_governance_review
from tests.context_path_eval.scenario import build_scenario
from tests.support.doubles import security_context
from tests.test_agent_eval_gate import _seed_confirmed_run


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_org(db: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    return org


async def _seed_agent_version(
    db: AsyncSession, organization_id: UUID, *, created_by: str = "priya.steward"
) -> AiAssetVersion:
    asset = AiAsset(
        organization_id=organization_id,
        asset_key=f"agent-{uuid4().hex[:8]}",
        asset_kind="AGENT",
        created_by=created_by,
    )
    db.add(asset)
    await db.flush()
    version = AiAssetVersion(
        organization_id=organization_id,
        asset_id=asset.id,
        version=1,
        status="DRAFT",
        name="External triage agent",
        description="A third-party agent proposed for onboarding.",
        intended_use="Read-only triage suggestions.",
        owner_principal=created_by,
        provider_type="EXTERNAL",
        risk_tier="LOW",
        documentation_url=None,
        fingerprint=f"fp-{uuid4().hex[:8]}",
        created_by=created_by,
    )
    db.add(version)
    await db.flush()
    return version


def _body(ai_asset_version_id: UUID, *, agent_principal_id: str = "agent:external-triage") -> AgentContractRequestCreate:
    return AgentContractRequestCreate(
        ai_asset_version_id=ai_asset_version_id,
        agent_principal_id=agent_principal_id,
        capability_envelope=CapabilityEnvelopeModel(
            tool_slugs=["order-lookup"], context_product_ids=[], write_lanes=[]
        ),
        autonomy_tier="T1",
        supervisor_persona="STEWARD",
        kill_scope="AGENT",
        sampling_rate=0.1,
    )


# ---------------------------------------------------------------------------
# 1. Submit endpoint
# ---------------------------------------------------------------------------


async def test_submit_opens_a_request_and_a_matching_governance_review(db: AsyncSession) -> None:
    org = await _seed_org(db)
    version = await _seed_agent_version(db, org.id)
    context = security_context(
        organization_id=org.id, principal_id="dev@vendor.example", roles=frozenset({"AgentDeveloper"})
    )

    result = await submit_agent_contract_request(org.id, _body(version.id), context, db)

    assert result.status == "PENDING"
    assert result.ai_asset_version_id == version.id
    assert result.governance_review_id is not None
    request = await db.get(AgentContractRequest, result.id)
    assert request is not None
    assert request.definition["agent_principal_id"] == "agent:external-triage"
    review = await db.get(GovernanceReview, result.governance_review_id)
    assert review is not None
    assert review.object_type == "AGENT_CONTRACT_REQUEST"
    assert review.object_id == str(result.id)
    assert review.status == "PENDING"


async def test_submit_refuses_self_authorization_exactly_like_the_direct_write_path(
    db: AsyncSession,
) -> None:
    org = await _seed_org(db)
    version = await _seed_agent_version(db, org.id)
    context = security_context(
        organization_id=org.id, principal_id="dev@vendor.example", roles=frozenset({"AgentDeveloper"})
    )
    body = _body(version.id, agent_principal_id="dev@vendor.example")  # names the submitter itself

    with pytest.raises(HTTPException) as excinfo:
        await submit_agent_contract_request(org.id, body, context, db)

    assert excinfo.value.status_code == 422


async def test_submit_404s_for_a_version_that_is_not_an_agent_kind_asset_in_this_org(
    db: AsyncSession,
) -> None:
    org = await _seed_org(db)
    context = security_context(
        organization_id=org.id, principal_id="dev@vendor.example", roles=frozenset({"AgentDeveloper"})
    )

    with pytest.raises(HTTPException) as excinfo:
        await submit_agent_contract_request(org.id, _body(uuid4()), context, db)

    assert excinfo.value.status_code == 404


async def test_list_and_get_round_trip_a_submitted_request(db: AsyncSession) -> None:
    org = await _seed_org(db)
    version = await _seed_agent_version(db, org.id)
    context = security_context(
        organization_id=org.id, principal_id="dev@vendor.example", roles=frozenset({"AgentDeveloper"})
    )
    submitted = await submit_agent_contract_request(org.id, _body(version.id), context, db)

    page = await list_agent_contract_requests(
        org.id, status_filter=None, ai_asset_version_id=None, limit=50, offset=0, context=context, session=db
    )
    assert page.total == 1
    assert page.items[0]["id"] == str(submitted.id)

    fetched = await get_agent_contract_request(submitted.id, context, db)
    assert fetched.id == submitted.id
    assert fetched.status == "PENDING"


def _review_id(request: AgentContractRequestRead) -> UUID:
    assert request.governance_review_id is not None
    return request.governance_review_id


# ---------------------------------------------------------------------------
# 2. The real, shared decision dispatcher
# ---------------------------------------------------------------------------


async def test_approval_is_blocked_with_no_confirmed_exemplars_and_nothing_is_written(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db)
    version = await _seed_agent_version(db, scenario.organization.id, created_by="maker@vendor.example")
    maker = security_context(
        organization_id=scenario.organization.id,
        principal_id="maker@vendor.example",
        roles=frozenset({"AgentDeveloper"}),
    )
    submitted = await submit_agent_contract_request(
        scenario.organization.id, _body(version.id), maker, db
    )
    checker = security_context(
        organization_id=scenario.organization.id, principal_id="checker", roles=frozenset({"Reviewer"})
    )

    with pytest.raises(HTTPException) as excinfo:
        await decide_governance_review(
            _review_id(submitted), GovernanceDecisionRequest(decision="APPROVE"), checker, db
        )

    assert excinfo.value.status_code == 409
    assert "INSUFFICIENT_DATA" in str(excinfo.value.detail)
    refreshed = await db.get(AgentContractRequest, submitted.id)
    assert refreshed is not None
    assert refreshed.status == "PENDING"
    written = await db.scalar(
        select(AgentContract).where(AgentContract.ai_asset_version_id == version.id)
    )
    assert written is None


async def test_approval_with_a_passing_confirmed_corpus_activates_and_writes_the_contract(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db)
    await _seed_confirmed_run(db, scenario)
    version = await _seed_agent_version(db, scenario.organization.id, created_by="maker@vendor.example")
    maker = security_context(
        organization_id=scenario.organization.id,
        principal_id="maker@vendor.example",
        roles=frozenset({"AgentDeveloper"}),
    )
    submitted = await submit_agent_contract_request(
        scenario.organization.id, _body(version.id), maker, db
    )
    checker = security_context(
        organization_id=scenario.organization.id, principal_id="checker", roles=frozenset({"Reviewer"})
    )

    decided = await decide_governance_review(
        _review_id(submitted), GovernanceDecisionRequest(decision="APPROVE"), checker, db
    )

    assert decided.status == "APPROVED"
    refreshed = await db.get(AgentContractRequest, submitted.id)
    assert refreshed is not None
    assert refreshed.status == "ACTIVATED"
    assert refreshed.eval_gate_verdict == "PASS"
    assert refreshed.activated_at is not None

    contract = await db.scalar(
        select(AgentContract).where(AgentContract.ai_asset_version_id == version.id)
    )
    assert contract is not None
    assert contract.agent_principal_id == "agent:external-triage"
    assert contract.autonomy_tier == "T1"
    assert contract.kill_engaged is False


async def test_rejection_never_touches_agent_contract(db: AsyncSession) -> None:
    scenario = await build_scenario(db)
    version = await _seed_agent_version(db, scenario.organization.id, created_by="maker@vendor.example")
    maker = security_context(
        organization_id=scenario.organization.id,
        principal_id="maker@vendor.example",
        roles=frozenset({"AgentDeveloper"}),
    )
    submitted = await submit_agent_contract_request(
        scenario.organization.id, _body(version.id), maker, db
    )
    checker = security_context(
        organization_id=scenario.organization.id, principal_id="checker", roles=frozenset({"Reviewer"})
    )

    decided = await decide_governance_review(
        _review_id(submitted),
        GovernanceDecisionRequest(decision="REJECT", reason="not evaluated yet"),
        checker,
        db,
    )

    assert decided.status == "REJECTED"
    refreshed = await db.get(AgentContractRequest, submitted.id)
    assert refreshed is not None
    assert refreshed.status == "REJECTED"
    assert refreshed.activated_at is None
    written = await db.scalar(
        select(AgentContract).where(AgentContract.ai_asset_version_id == version.id)
    )
    assert written is None


async def test_maker_cannot_check_their_own_agent_contract_request(db: AsyncSession) -> None:
    org = await _seed_org(db)
    version = await _seed_agent_version(db, org.id, created_by="dev@vendor.example")
    maker = security_context(
        organization_id=org.id, principal_id="dev@vendor.example", roles=frozenset({"AgentDeveloper"})
    )
    submitted = await submit_agent_contract_request(org.id, _body(version.id), maker, db)

    with pytest.raises(HTTPException) as excinfo:
        await decide_governance_review(
            _review_id(submitted), GovernanceDecisionRequest(decision="APPROVE"), maker, db
        )

    assert excinfo.value.status_code == 409
    assert "maker-checker" in str(excinfo.value.detail)
