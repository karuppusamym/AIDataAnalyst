"""ADR-0027: risk tiers, the reviewer agent, and the three hard conditions.

The conditions are the whole argument for letting an agent check anything,
so each gets its own test and each is asserted at the mechanism rather than
at the configuration:

* **(a) the agent can only approve or reject, and only T0/T1.** Asserted by
  the tier table, by the allowlist being derived *from* the tier table, and
  by a static scan proving the agent never reaches a publish/activate path.
* **(b) every approval is sampled at a 5% floor.** Asserted deterministically.
* **(c) one human action suspends it.** Asserted end to end.

Plus the property that makes the whole thing INV-8-compatible: the agent
never decides an item it proposed.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AuditEvent,
    GovernanceReview,
    Organization,
    ReviewAuditSample,
)
from aida.review_risk_tiers import (
    TIER_T0,
    TIER_T1,
    TIER_T2,
    TIER_T3,
    agent_decidable_object_types,
    known_object_types,
    risk_tier_for,
    tier_at_or_below,
)
from aida.reviewer_agent import (
    REASON_DISABLED,
    REASON_SUSPENDED,
    ReviewerAgentUnavailable,
    auto_decide_tier0_tier1,
    organization_suspended,
    pre_review_pending,
    resolve_audit_sample,
    sampled_for_audit,
    set_suspended,
)
from tests.support.doubles import security_context

REPO_ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "reviewer_agent_enabled": True,
        "reviewer_agent_principal_id": "agent:reviewer",
        "reviewer_agent_max_tier": "T1",
        "reviewer_agent_sampling_rate": 0.05,
        "reviewer_agent_suspended": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _seed_review(
    session: AsyncSession,
    org: Organization,
    *,
    object_type: str = "ASSET_DESCRIPTION_DRAFT",
    requested_by: str = "steward-a",
    status: str = "PENDING",
    object_id: str | None = None,
    requested_action: str = "PUBLISH",
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=org.id,
        object_type=object_type,
        object_id=object_id or str(uuid4()),
        requested_action=requested_action,
        status=status,
        requested_by=requested_by,
    )
    session.add(review)
    await session.flush()
    return review


# ---------------------------------------------------------------------------
# 1. The tier table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("object_type", "expected"),
    [
        ("ASSET_DESCRIPTION_DRAFT", TIER_T0),
        ("BUSINESS_ANNOTATION", TIER_T0),
        ("METADATA_ENRICHMENT_PROPOSAL", TIER_T0),
        ("GLOSSARY_LINK_PROPOSAL", TIER_T1),
        ("TERM_SEMANTIC_BINDING", TIER_T1),
        ("SEMANTIC_MODEL_VERSION", TIER_T2),
        ("GOVERNED_TOOL_VERSION", TIER_T2),
        ("CONTEXT_PRODUCT_VERSION", TIER_T2),
        ("MODEL_ROUTE_CONFIGURATION", TIER_T3),
        ("CROSS_BOUNDARY_GRANT", TIER_T3),
        ("AI_ASSET_VERSION", TIER_T3),
        ("DATA_PRODUCT_ACCESS_REQUEST", TIER_T3),
    ],
)
def test_the_tier_table_classifies_the_object_types_that_matter(
    object_type: str, expected: str
) -> None:
    assert risk_tier_for(object_type) == expected


def test_an_unknown_object_type_is_t3() -> None:
    """Fail closed. Adding a governed object type therefore defaults to
    human-only review until someone deliberately classifies it."""
    assert risk_tier_for("SOMETHING_INVENTED_NEXT_QUARTER") == TIER_T3


def test_every_object_type_the_codebase_creates_is_classified() -> None:
    """The tier table has to keep up with the object types that actually
    reach the review queue, or an unclassified one silently becomes T3 and
    a human queue quietly grows. Scans for real `GovernanceReview(...)`
    constructions rather than trusting a hand-maintained list.
    """
    import re

    created: set[str] = set()
    for path in (REPO_ROOT / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"GovernanceReview\(\s*(?:[^()]|\([^()]*\))*?object_type=([\"'])([A-Z_]+)\1",
            source,
            re.S,
        ):
            created.add(match.group(2))
    unclassified = sorted(created - known_object_types())
    assert unclassified == [], (
        "these object types reach the review queue but the ADR-0027 tier table "
        f"does not classify them (they default to T3): {unclassified}"
    )


def test_a_large_bulk_operation_is_not_tier_1() -> None:
    """An operation big enough that the platform insisted on a human is not
    one an agent should wave through."""
    assert risk_tier_for("BULK_STEWARDSHIP_OPERATION", {"item_count": 3}) == TIER_T1
    assert risk_tier_for("BULK_STEWARDSHIP_OPERATION", {"item_count": 5000}) == TIER_T2


def test_an_unparseable_bulk_count_is_not_evidence_of_smallness() -> None:
    assert risk_tier_for("BULK_STEWARDSHIP_OPERATION", {"item_count": "many"}) == TIER_T2


def test_tier_ordering() -> None:
    assert tier_at_or_below(TIER_T0, TIER_T1)
    assert tier_at_or_below(TIER_T1, TIER_T1)
    assert not tier_at_or_below(TIER_T2, TIER_T1)
    assert not tier_at_or_below(TIER_T3, TIER_T2)


def test_an_unrecognised_tier_is_never_at_or_below_anything() -> None:
    assert not tier_at_or_below("T7", TIER_T3)
    assert not tier_at_or_below(TIER_T0, "banana")


# ---------------------------------------------------------------------------
# 2. Condition (a): the agent can only ever touch T0/T1
# ---------------------------------------------------------------------------


def test_the_allowlist_is_derived_from_the_tier_table_not_from_config() -> None:
    """Config can narrow what the agent may touch; it can never widen it.

    Even asked for a T3 ceiling, the allowlist only ever contains what the
    tier table classifies -- and the auto-decide path re-checks the tier per
    item, so a T2/T3 type cannot slip through a mis-set ceiling.
    """
    at_t1 = agent_decidable_object_types(TIER_T1)
    assert "ASSET_DESCRIPTION_DRAFT" in at_t1
    assert "GLOSSARY_LINK_PROPOSAL" in at_t1
    assert "SEMANTIC_MODEL_VERSION" not in at_t1
    assert "MODEL_ROUTE_CONFIGURATION" not in at_t1
    for object_type in agent_decidable_object_types(TIER_T3):
        assert object_type in known_object_types()


def test_no_t2_or_t3_type_is_ever_agent_decidable_at_the_default_ceiling() -> None:
    decidable = agent_decidable_object_types()
    for object_type in decidable:
        assert risk_tier_for(object_type) in (TIER_T0, TIER_T1)


@pytest.mark.asyncio
async def test_auto_decide_refuses_a_t2_item_even_when_it_is_pre_reviewed(
    session: AsyncSession,
) -> None:
    """The tier guard is per item, so a T2 proposal sitting in the queue with
    an APPROVE recommendation is still not decided."""
    org = await _seed_org(session)
    review = await _seed_review(session, org, object_type="SEMANTIC_MODEL_VERSION")
    review.risk_tier = TIER_T2
    review.pre_review_recommendation = "APPROVE"
    review.pre_reviewed_at = datetime.now(UTC)
    review.pre_reviewed_by = "agent:reviewer"
    await session.flush()

    decisions = await auto_decide_tier0_tier1(session, org.id, settings=_settings())

    assert decisions == []
    assert review.status == "PENDING"


@pytest.mark.asyncio
async def test_the_reviewer_agent_cannot_reach_a_publish_or_activate_path() -> None:
    """ADR-0027 condition (a), as a static property rather than a behaviour.

    `reviewer_agent.py` may reach exactly one decision entry point --
    `_apply_governance_review_decision`, the same core a human checker uses.
    It must not import or call anything that publishes a semantic version,
    activates a model route, or grants access directly. Same technique as
    `test_inv4_authorization_wiring.py`'s static scan.
    """
    source = (REPO_ROOT / "src" / "aida" / "reviewer_agent.py").read_text(encoding="utf-8")
    forbidden = (
        "publish_semantic",
        "activate_route",
        "approve_model_route",
        "grant_access",
        "provision_access",
        "execute_tool",
        "QueryExecutionGateway",
    )
    hits = [name for name in forbidden if name in source]
    assert hits == [], f"reviewer_agent must not reach these paths: {hits}"
    assert "_apply_governance_review_decision" in source, (
        "the agent must decide through the same core a human checker uses, "
        "not through a private path of its own"
    )


# ---------------------------------------------------------------------------
# 3. Condition (b): sampling, with a floor
# ---------------------------------------------------------------------------


def test_sampling_is_deterministic_for_a_review_id() -> None:
    review_id = uuid4()
    first = sampled_for_audit(review_id, 0.5)
    assert all(sampled_for_audit(review_id, 0.5) is first for _ in range(10))


def test_sampling_never_drops_below_the_five_percent_floor() -> None:
    """A rate below the floor -- hand-edited config, or a row written before
    the CHECK constraint existed -- still samples at 5%."""
    ids = [UUID(int=index) for index in range(600)]
    at_floor = sum(sampled_for_audit(i, 0.05) for i in ids)
    at_zero = sum(sampled_for_audit(i, 0.0) for i in ids)
    assert at_zero == at_floor > 0


def test_a_higher_rate_samples_more() -> None:
    ids = [UUID(int=index) for index in range(600)]
    assert sum(sampled_for_audit(i, 0.5) for i in ids) > sum(
        sampled_for_audit(i, 0.05) for i in ids
    )


# ---------------------------------------------------------------------------
# 4. Condition (c): suspension, and the disabled default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_is_disabled_by_default(session: AsyncSession) -> None:
    """An organization that never enables this sees exactly today's
    behaviour: every item waits for a human."""
    org = await _seed_org(session)
    defaults = Settings(_env_file=None, environment="test")
    assert defaults.reviewer_agent_enabled is False
    with pytest.raises(ReviewerAgentUnavailable) as excinfo:
        await auto_decide_tier0_tier1(session, org.id, settings=defaults)
    assert excinfo.value.reason_code == REASON_DISABLED


@pytest.mark.asyncio
async def test_process_wide_suspension_stops_everything(session: AsyncSession) -> None:
    org = await _seed_org(session)
    with pytest.raises(ReviewerAgentUnavailable) as excinfo:
        await auto_decide_tier0_tier1(
            session, org.id, settings=_settings(reviewer_agent_suspended=True)
        )
    assert excinfo.value.reason_code == REASON_SUSPENDED


@pytest.mark.asyncio
async def test_one_human_action_suspends_and_resumes_one_organization(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    other = await _seed_org(session)
    context = security_context(organization_id=org.id, principal_id="risk-officer")

    await set_suspended(
        session, org.id, suspended=True, context=context, reason="disagreement spike"
    )
    await session.flush()
    assert await organization_suspended(session, org.id) is True
    assert await organization_suspended(session, other.id) is False

    with pytest.raises(ReviewerAgentUnavailable) as excinfo:
        await auto_decide_tier0_tier1(session, org.id, settings=_settings())
    assert excinfo.value.reason_code == REASON_SUSPENDED

    await set_suspended(session, org.id, suspended=False, context=context)
    await session.flush()
    assert await organization_suspended(session, org.id) is False


@pytest.mark.asyncio
async def test_suspension_is_audited(session: AsyncSession) -> None:
    org = await _seed_org(session)
    context = security_context(organization_id=org.id, principal_id="risk-officer")
    await set_suspended(
        session, org.id, suspended=True, context=context, reason="spike"
    )
    await session.flush()
    rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "reviewer_agent.suspend")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["suspended"] is True


# ---------------------------------------------------------------------------
# 5. Pre-review: evidence and recommendations, deciding nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_review_decides_nothing(session: AsyncSession) -> None:
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    await pre_review_pending(session, org.id, settings=_settings())
    assert review.status == "PENDING"
    assert review.decided_by is None


@pytest.mark.asyncio
async def test_pre_review_attaches_tier_recommendation_and_evidence(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    outcomes = await pre_review_pending(session, org.id, settings=_settings())
    assert len(outcomes) == 1
    assert review.risk_tier == TIER_T0
    assert review.pre_review_recommendation == "APPROVE"
    assert review.pre_reviewed_by == "agent:reviewer"
    assert review.pre_review_evidence["rule_version"] == 1
    assert review.pre_review_evidence["negative_knowledge_hits"] == 0


@pytest.mark.asyncio
async def test_a_previously_rejected_proposal_is_recommended_for_rejection(
    session: AsyncSession,
) -> None:
    """Negative knowledge: the platform has already been told this is wrong,
    and re-proposing it does not make it right."""
    org = await _seed_org(session)
    object_id = str(uuid4())
    await _seed_review(session, org, object_id=object_id, status="REJECTED")
    fresh = await _seed_review(session, org, object_id=object_id)

    await pre_review_pending(session, org.id, settings=_settings())

    assert fresh.pre_review_recommendation == "REJECT"
    assert fresh.pre_review_evidence["negative_knowledge_hits"] == 1


@pytest.mark.asyncio
async def test_a_t3_item_is_never_recommended_for_approval(session: AsyncSession) -> None:
    org = await _seed_org(session)
    review = await _seed_review(session, org, object_type="MODEL_ROUTE_CONFIGURATION")
    await pre_review_pending(session, org.id, settings=_settings())
    assert review.risk_tier == TIER_T3
    assert review.pre_review_recommendation == "NONE"


@pytest.mark.asyncio
async def test_pre_review_is_idempotent(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _seed_review(session, org)
    first = await pre_review_pending(session, org.id, settings=_settings())
    second = await pre_review_pending(session, org.id, settings=_settings())
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_pre_review_is_organization_scoped(session: AsyncSession) -> None:
    org_a = await _seed_org(session)
    org_b = await _seed_org(session)
    await _seed_review(session, org_b)
    assert await pre_review_pending(session, org_a.id, settings=_settings()) == []


@pytest.mark.asyncio
async def test_pre_review_is_audited(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _seed_review(session, org)
    await pre_review_pending(session, org.id, settings=_settings())
    await session.flush()
    rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "reviewer_agent.pre_review")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["reviewed"] == 1


# ---------------------------------------------------------------------------
# 6. INV-8: the agent never decides its own proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_never_decides_an_item_it_proposed(session: AsyncSession) -> None:
    """The shared decision path enforces maker != checker too; this asserts
    the agent skips rather than failing mid-batch on a 409."""
    org = await _seed_org(session)
    review = await _seed_review(session, org, requested_by="agent:reviewer")
    review.risk_tier = TIER_T0
    review.pre_review_recommendation = "APPROVE"
    review.pre_reviewed_at = datetime.now(UTC)
    await session.flush()

    decisions = await auto_decide_tier0_tier1(session, org.id, settings=_settings())

    assert decisions == []
    assert review.status == "PENDING"


@pytest.mark.asyncio
async def test_an_item_with_no_recommendation_is_left_alone(session: AsyncSession) -> None:
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    review.risk_tier = TIER_T0
    review.pre_review_recommendation = "NONE"
    review.pre_reviewed_at = datetime.now(UTC)
    await session.flush()

    assert await auto_decide_tier0_tier1(session, org.id, settings=_settings()) == []
    assert review.status == "PENDING"


@pytest.mark.asyncio
async def test_an_item_that_was_never_pre_reviewed_is_left_alone(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    assert await auto_decide_tier0_tier1(session, org.id, settings=_settings()) == []
    assert review.status == "PENDING"


# ---------------------------------------------------------------------------
# 7. Sample resolution
# ---------------------------------------------------------------------------


async def _seed_sample(session: AsyncSession, org: Organization) -> ReviewAuditSample:
    review = await _seed_review(session, org, status="APPROVED")
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=review.id,
        agent_principal_id="agent:reviewer",
        object_type="ASSET_DESCRIPTION_DRAFT",
        risk_tier=TIER_T0,
        decision="APPROVED",
        sampled_at=datetime.now(UTC),
        human_outcome="PENDING",
    )
    session.add(sample)
    await session.flush()
    return sample


@pytest.mark.asyncio
async def test_resolving_a_sample_records_the_verdict_and_audits(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    sample = await _seed_sample(session, org)
    context = security_context(organization_id=org.id, principal_id="reviewer-h")

    await resolve_audit_sample(
        session,
        sample,
        human_outcome="DISAGREED",
        rationale="the description named the wrong system of record",
        context=context,
    )
    await session.flush()

    assert sample.human_outcome == "DISAGREED"
    assert sample.human_principal_id == "reviewer-h"
    assert sample.resolved_at is not None
    rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "reviewer_agent.sample.resolve")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["human_outcome"] == "DISAGREED"


@pytest.mark.asyncio
async def test_a_rationale_is_mandatory(session: AsyncSession) -> None:
    org = await _seed_org(session)
    sample = await _seed_sample(session, org)
    with pytest.raises(ValueError, match="rationale is mandatory"):
        await resolve_audit_sample(
            session,
            sample,
            human_outcome="AGREED",
            rationale="   ",
            context=security_context(organization_id=org.id),
        )


@pytest.mark.asyncio
async def test_a_sample_cannot_be_resolved_twice(session: AsyncSession) -> None:
    org = await _seed_org(session)
    sample = await _seed_sample(session, org)
    context = security_context(organization_id=org.id)
    await resolve_audit_sample(
        session, sample, human_outcome="AGREED", rationale="fine", context=context
    )
    with pytest.raises(ValueError, match="already resolved"):
        await resolve_audit_sample(
            session, sample, human_outcome="AGREED", rationale="fine", context=context
        )


@pytest.mark.asyncio
async def test_an_invalid_outcome_is_refused(session: AsyncSession) -> None:
    org = await _seed_org(session)
    sample = await _seed_sample(session, org)
    with pytest.raises(ValueError, match="AGREED or DISAGREED"):
        await resolve_audit_sample(
            session,
            sample,
            human_outcome="MAYBE",
            rationale="unsure",
            context=security_context(organization_id=org.id),
        )
