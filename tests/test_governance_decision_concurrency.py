"""F05: one governance review reaches exactly one terminal decision.

The review (`Docs/review-2026-09-05/REVIEW.md`, F05) recorded that the
single-item endpoint claimed a review with `SELECT ... FOR UPDATE` before
checking PENDING, while the bulk endpoint loaded ordinary ORM objects and
checked their *in-memory* status. Two checkers could both act on the same
pending review -- one approving while the other rejected -- and both would
proceed, each writing its own audit row, outbox event and target-object
transition.

**How this file proves the fix without depending on row locks.** The tests
run against SQLite, where `SELECT ... FOR UPDATE` compiles away entirely, so
a test that leaned on locking would prove nothing here. It does not need to:
the guard is a compare-and-set, `UPDATE ... WHERE id = :id AND status =
'PENDING'`, whose single-statement atomicity is the same on SQLite as on
PostgreSQL. Every test below therefore reproduces the *actual* F05 scenario
rather than simulating a lock:

    two independent sessions, each with its own connection to the same
    (file-backed) database, both holding a `GovernanceReview` object they
    read while it was PENDING; one commits its decision; the other then
    tries to act on the copy it already has.

That second caller's in-memory object still says PENDING -- which is exactly
the stale read the old bulk path trusted -- so if the claim were not
compare-and-set, it would proceed. The file-backed engine matters: the
in-memory SQLite engine used elsewhere in this suite shares one connection
through `StaticPool`, which cannot represent two concurrent transactions.

`test_a_lost_claim_is_detected_between_the_read_and_the_write` additionally
forces the *interleaved* ordering (the competing decision lands after this
caller has already passed its own PENDING pre-check) by patching the claim
step, so the window the review describes is exercised directly and not only
its outcome.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aida import governance_decision_service, reviewer_agent
from aida.config import Settings
from aida.db import Base
from aida.governance_decision_contracts import GovernanceDecisionRefused
from aida.models import (
    AssetDescriptionDraft,
    AuditEvent,
    GlossaryTerm,
    GovernanceReview,
    Organization,
    OutboxEvent,
    TermSemanticBinding,
)
from aida.review_risk_tiers import known_object_types
from aida.schemas import (
    GovernanceDecisionRequest,
    GovernanceReviewBulkDecisionRequest,
)
from aida.security_types import SecurityContext
from aida.semantic_api import bulk_decide_governance_reviews, decide_governance_review

# `AuditEvent.id` is a `BigInteger` autoincrement primary key that relies on
# PostgreSQL's identity generation; SQLite only auto-populates a bare
# `INTEGER PRIMARY KEY`. Same workaround as `test_bulk_governance_decisions.py`.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


# --- fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A *file-backed* SQLite database, so two sessions get two connections.

    The in-memory + `StaticPool` engine the rest of this suite uses funnels
    every session through one connection, which makes "two checkers, two
    transactions" impossible to express. Contention is the whole subject
    here, so this fixture pays for a temporary file instead.
    """
    created = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'governance.db'}")
    async with created.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


def _context(org: Organization, principal: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )


async def _seed_binding_and_review(
    session: AsyncSession,
    org: Organization,
    term: GlossaryTerm,
    *,
    requested_by: str = "maker",
) -> GovernanceReview:
    """The leanest object type the queue supports (TERM_SEMANTIC_BINDING)."""
    binding = TermSemanticBinding(
        organization_id=org.id,
        term_id=term.id,
        semantic_object_type="SEMANTIC_METRIC",
        semantic_object_id=uuid4(),
        status="PENDING_APPROVAL",
        requested_by=requested_by,
    )
    session.add(binding)
    await session.flush()
    review = GovernanceReview(
        organization_id=org.id,
        object_type="TERM_SEMANTIC_BINDING",
        object_id=str(binding.id),
        requested_action="APPROVE",
        requested_by=requested_by,
        status="PENDING",
    )
    session.add(review)
    await session.flush()
    return review


async def _seed_agent_decidable_review(
    session: AsyncSession,
    org: Organization,
    *,
    requested_by: str = "maker",
    overall_score: float = 0.95,
) -> tuple[GovernanceReview, AssetDescriptionDraft]:
    """A review the reviewer agent may actually decide.

    `TERM_SEMANTIC_BINDING` -- the leanest type this file otherwise uses --
    stopped being agent-decidable when AR-03 landed: it is a steward's
    unscored assertion, so the agent has no object-specific evidence to judge
    it by and abstains. Tests that assert an auto-*decision* therefore need a
    proposal that carries its own score. `ASSET_DESCRIPTION_DRAFT` is the
    cheapest one: a draft row and the review that points at it.
    """
    review = GovernanceReview(
        organization_id=org.id,
        object_type="ASSET_DESCRIPTION_DRAFT",
        object_id="pending",
        requested_action="PUBLISH",
        requested_by=requested_by,
        status="PENDING",
    )
    session.add(review)
    await session.flush()
    draft = AssetDescriptionDraft(
        organization_id=org.id,
        table_id=uuid4(),
        drafted_text="Customer master, one row per customer.",
        text_fingerprint="f" * 64,
        accuracy_score=overall_score,
        clarity_score=overall_score,
        style_score=overall_score,
        completeness_score=overall_score,
        overall_score=overall_score,
        evidence={"source": "deterministic"},
        status="PENDING_APPROVAL",
        governance_review_id=review.id,
        created_by=requested_by,
    )
    session.add(draft)
    await session.flush()
    review.object_id = str(draft.id)
    await session.flush()
    return review, draft


async def _term(session: AsyncSession, org: Organization) -> GlossaryTerm:
    term = GlossaryTerm(organization_id=org.id, term_key=f"term-{uuid4().hex[:8]}")
    session.add(term)
    await session.flush()
    return term


async def _seed(engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """One organization, one pending TERM_SEMANTIC_BINDING review. Returns
    `(organization_id, review_id, binding_id)` -- ids rather than instances,
    because each test loads its own copy per session on purpose."""
    async with _sessions(engine)() as setup:
        org = await _org(setup)
        term = await _term(setup, org)
        review = await _seed_binding_and_review(setup, org, term)
        await setup.commit()
        return org.id, review.id, UUID(review.object_id)


async def _load(session: AsyncSession, review_id: UUID) -> GovernanceReview:
    review = await session.get(GovernanceReview, review_id)
    assert review is not None
    return review


async def _load_org(session: AsyncSession, organization_id: UUID) -> Organization:
    org = await session.get(Organization, organization_id)
    assert org is not None
    return org


async def _side_effect_counts(
    engine: AsyncEngine, review_id: UUID, binding_id: UUID
) -> tuple[int, int]:
    """(audit rows naming this review, outbox events for its target)."""
    async with _sessions(engine)() as reader:
        audits = await reader.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.resource_id == str(review_id))
        )
        events = await reader.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == str(binding_id))
        )
        return int(audits or 0), int(events or 0)


# ---------------------------------------------------------------------------
# 1. single vs. bulk
# ---------------------------------------------------------------------------


async def test_single_decision_loses_to_a_committed_bulk_decision(engine: AsyncEngine) -> None:
    """A checker who opened a review while it was PENDING, and pressed
    Approve after a bulk sweep already rejected it, gets a 409 carrying the
    review's *refreshed* state -- not a silent success, not a 500, and not a
    second set of side effects."""
    organization_id, review_id, binding_id = await _seed(engine)
    maker = _sessions(engine)

    async with maker() as bulk_session, maker() as single_session:
        # Both checkers read the review while it is still PENDING.
        single_review = await _load(single_session, review_id)
        assert single_review.status == "PENDING"
        bulk_org = await _load_org(bulk_session, organization_id)

        await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=[review_id], decision="REJECT", reason="sweeping the backlog"
            ),
            context=_context(bulk_org, "checker-bulk"),
            session=bulk_session,
        )

        single_org = await _load_org(single_session, organization_id)
        with pytest.raises(HTTPException) as raised:
            await decide_governance_review(
                review_id,
                GovernanceDecisionRequest(decision="APPROVE"),
                context=_context(single_org, "checker-single"),
                session=single_session,
            )

    assert raised.value.status_code == 409
    detail = raised.value.detail
    assert isinstance(detail, dict), "the loser must receive refreshed state, not a bare string"
    assert detail["outcome"] == "CONFLICT"
    assert detail["review"]["status"] == "REJECTED"
    assert detail["review"]["decided_by"] == "checker-bulk"
    assert detail["review"]["decision_reason"] == "sweeping the backlog"
    # ...and it still reads as a sentence for the caller that only shows a
    # per-item reason string (`asset_description_api`'s sample review).
    assert str(detail) == "governance review is already rejected"

    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "REJECTED"
        binding = await reader.get(TermSemanticBinding, binding_id)
        assert binding is not None
        assert binding.status == "REJECTED"

    audits, events = await _side_effect_counts(engine, review_id, binding_id)
    assert events == 1, "exactly one outbox event for one terminal decision"
    assert audits == 0, "the bulk path files one batch-level audit row, not a per-review one"


async def test_bulk_decision_loses_to_a_committed_single_decision(engine: AsyncEngine) -> None:
    """The mirror image: the bulk batch read the review while it was PENDING
    and is the one that arrives second. Its item is reported CONFLICT rather
    than applied, and the single checker's decision stands untouched."""
    organization_id, review_id, binding_id = await _seed(engine)
    maker = _sessions(engine)

    async with maker() as bulk_session, maker() as single_session:
        bulk_review = await _load(bulk_session, review_id)
        assert bulk_review.status == "PENDING"

        single_org = await _load_org(single_session, organization_id)
        await decide_governance_review(
            review_id,
            GovernanceDecisionRequest(decision="APPROVE"),
            context=_context(single_org, "checker-single"),
            session=single_session,
        )

        bulk_org = await _load_org(bulk_session, organization_id)
        result = await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=[review_id], decision="REJECT", reason="sweeping the backlog"
            ),
            context=_context(bulk_org, "checker-bulk"),
            session=bulk_session,
        )

    assert result.succeeded_count == 0
    assert result.failed_count == 1
    item = result.results[0]
    assert item.status == "FAILED"
    assert item.outcome == "CONFLICT"
    assert "already approved" in (item.reason or "")

    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "APPROVED"
        assert final.decided_by == "checker-single"
        binding = await reader.get(TermSemanticBinding, binding_id)
        assert binding is not None
        assert binding.status == "ACTIVE"

    _, events = await _side_effect_counts(engine, review_id, binding_id)
    assert events == 1


# ---------------------------------------------------------------------------
# 2. bulk vs. bulk
# ---------------------------------------------------------------------------


async def test_two_bulk_batches_over_the_same_review_produce_one_decision(
    engine: AsyncEngine,
) -> None:
    """Two checkers sweep overlapping selections, one approving and one
    rejecting. Exactly one wins; the other's item is CONFLICT; the target
    object transitions once."""
    organization_id, review_id, binding_id = await _seed(engine)
    maker = _sessions(engine)

    async with maker() as first, maker() as second:
        # Both batches read the same PENDING review before either decides.
        assert (await _load(first, review_id)).status == "PENDING"
        assert (await _load(second, review_id)).status == "PENDING"

        first_result = await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=[review_id], decision="APPROVE", reason="batch one"
            ),
            context=_context(await _load_org(first, organization_id), "checker-one"),
            session=first,
        )
        second_result = await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=[review_id], decision="REJECT", reason="batch two"
            ),
            context=_context(await _load_org(second, organization_id), "checker-two"),
            session=second,
        )

    outcomes = {first_result.results[0].outcome, second_result.results[0].outcome}
    assert outcomes == {"APPLIED", "CONFLICT"}
    assert first_result.succeeded_count + second_result.succeeded_count == 1

    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "APPROVED"
        assert final.decided_by == "checker-one"

    _, events = await _side_effect_counts(engine, review_id, binding_id)
    assert events == 1, "the loser must not add a second outbox event"


async def test_a_lost_claim_is_detected_between_the_read_and_the_write(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interleaving the review describes, forced rather than hoped for.

    The competing decision is committed from a second connection *after* this
    batch has already loaded the review and passed its own PENDING
    pre-filter, and immediately before the compare-and-set runs. This is the
    exact window in which the old code proceeded on a stale read.
    """
    organization_id, review_id, binding_id = await _seed(engine)
    maker = _sessions(engine)
    real_claim = governance_decision_service.claim_review
    intercepted = False

    async def claim_after_a_competing_commit(
        session: AsyncSession, review: GovernanceReview, **kwargs: object
    ) -> None:
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            async with maker() as intruder:
                await real_claim(
                    intruder,
                    await _load(intruder, review_id),
                    decision="REJECT",
                    reason="the other checker got here first",
                    context=_context(await _load_org(intruder, organization_id), "intruder"),
                    now=datetime.now(UTC),
                )
                await intruder.commit()
        return await real_claim(session, review, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(governance_decision_service, "claim_review", claim_after_a_competing_commit)
    async with maker() as batch:
        result = await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=[review_id], decision="APPROVE", reason="batch"
            ),
            context=_context(await _load_org(batch, organization_id), "checker"),
            session=batch,
        )

    assert intercepted
    assert result.results[0].outcome == "CONFLICT"
    assert result.succeeded_count == 0

    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "REJECTED"
        assert final.decided_by == "intruder"
        binding = await reader.get(TermSemanticBinding, binding_id)
        assert binding is not None
        # The winner's REJECT never ran an adapter (it was a bare claim), and
        # the loser's APPROVE was rolled back with its savepoint -- so the
        # binding is untouched rather than half-approved.
        assert binding.status == "PENDING_APPROVAL"

    _, events = await _side_effect_counts(engine, review_id, binding_id)
    assert events == 0


# ---------------------------------------------------------------------------
# 3. partial success is preserved: a losing item does not poison its batch
# ---------------------------------------------------------------------------


async def test_a_conflicted_item_does_not_stop_the_rest_of_the_batch(
    engine: AsyncEngine,
) -> None:
    """One item of a three-item batch is decided by somebody else first. The
    other two still apply, and the batch reports PARTIAL_SUCCESS rather than
    failing whole -- the per-item savepoint semantics PG-3 established."""
    maker = _sessions(engine)
    async with maker() as setup:
        org = await _org(setup)
        term = await _term(setup, org)
        reviews = [await _seed_binding_and_review(setup, org, term) for _ in range(3)]
        await setup.commit()
        organization_id = org.id
        review_ids = [review.id for review in reviews]
        binding_ids = [UUID(review.object_id) for review in reviews]

    async with maker() as batch, maker() as intruder:
        for review_id in review_ids:
            assert (await _load(batch, review_id)).status == "PENDING"

        await decide_governance_review(
            review_ids[1],
            GovernanceDecisionRequest(decision="REJECT", reason="not this one"),
            context=_context(await _load_org(intruder, organization_id), "intruder"),
            session=intruder,
        )

        result = await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=review_ids, decision="APPROVE", reason="batch"
            ),
            context=_context(await _load_org(batch, organization_id), "checker"),
            session=batch,
        )

    assert result.succeeded_count == 2
    assert result.failed_count == 1
    by_id = {item.review_id: item for item in result.results}
    assert by_id[str(review_ids[0])].outcome == "APPLIED"
    assert by_id[str(review_ids[1])].outcome == "CONFLICT"
    assert by_id[str(review_ids[2])].outcome == "APPLIED"

    async with maker() as reader:
        statuses = [(await _load(reader, review_id)).status for review_id in review_ids]
        assert statuses == ["APPROVED", "REJECTED", "APPROVED"]
        bindings = [await reader.get(TermSemanticBinding, value) for value in binding_ids]
        assert [binding.status for binding in bindings if binding is not None] == [
            "ACTIVE",
            "REJECTED",
            "ACTIVE",
        ]


async def test_a_failing_item_rolls_back_only_its_own_savepoint(engine: AsyncEngine) -> None:
    """An item whose *target* refuses (not a conflict) leaves its own review
    PENDING and nothing else. Proven alongside a sibling item that applies,
    so the rollback is demonstrably scoped to one savepoint."""
    maker = _sessions(engine)
    async with maker() as setup:
        org = await _org(setup)
        term = await _term(setup, org)
        good = await _seed_binding_and_review(setup, org, term)
        doomed = await _seed_binding_and_review(setup, org, term)
        # The target binding has already moved on, so the adapter's
        # "no longer pending review" precondition fails after the claim.
        doomed_binding = await setup.get(TermSemanticBinding, UUID(doomed.object_id))
        assert doomed_binding is not None
        doomed_binding.status = "ACTIVE"
        await setup.commit()
        organization_id = org.id
        good_id, doomed_id = good.id, doomed.id

    async with maker() as batch:
        result = await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                review_ids=[good_id, doomed_id], decision="APPROVE", reason="batch"
            ),
            context=_context(await _load_org(batch, organization_id), "checker"),
            session=batch,
        )

    by_id = {item.review_id: item for item in result.results}
    assert by_id[str(good_id)].outcome == "APPLIED"
    assert by_id[str(doomed_id)].outcome == "FAILED"
    assert "no longer pending review" in (by_id[str(doomed_id)].reason or "")

    async with maker() as reader:
        assert (await _load(reader, good_id)).status == "APPROVED"
        failed = await _load(reader, doomed_id)
        assert failed.status == "PENDING", "a failed item's claim must be rolled back"
        assert failed.decided_by is None
        assert failed.decided_at is None


# ---------------------------------------------------------------------------
# 4. reviewer automation obeys the same invariant
# ---------------------------------------------------------------------------


def _agent_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "reviewer_agent_enabled": True,
        "reviewer_agent_suspended": False,
        "reviewer_agent_max_tier": "T1",
        "reviewer_agent_principal_id": "agent:reviewer",
        "reviewer_agent_sampling_rate": 0.05,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def test_reviewer_automation_never_overrides_a_human_decision(
    engine: AsyncEngine,
) -> None:
    """ADR-0027 automation goes through the same claim. A review a human
    decided after the agent's batch read it is left alone: no second
    decision, no outcome reported, no audit row, no outbox event."""
    maker = _sessions(engine)
    async with maker() as setup:
        org = await _org(setup)
        term = await _term(setup, org)
        review = await _seed_binding_and_review(setup, org, term)
        review.pre_reviewed_at = datetime.now(UTC)
        review.pre_review_recommendation = "APPROVE"
        review.risk_tier = "T1"
        await setup.commit()
        organization_id, review_id = org.id, review.id
        binding_id = UUID(review.object_id)

    async with maker() as agent_session, maker() as human_session:
        # The agent's pass reads the review while it is PENDING...
        assert (await _load(agent_session, review_id)).status == "PENDING"
        # ...a human decides it first...
        await decide_governance_review(
            review_id,
            GovernanceDecisionRequest(decision="REJECT", reason="human judgement"),
            context=_context(await _load_org(human_session, organization_id), "human"),
            session=human_session,
        )
        # ...and the agent's pass then tries to act on what it read.
        outcomes = await reviewer_agent.auto_decide_tier0_tier1(
            agent_session, organization_id, settings=_agent_settings()
        )
        await agent_session.commit()

    assert outcomes == [], "the agent must not decide a review a human already decided"

    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "REJECTED"
        assert final.decided_by == "human"
        binding = await reader.get(TermSemanticBinding, binding_id)
        assert binding is not None
        assert binding.status == "REJECTED"

    _, events = await _side_effect_counts(engine, review_id, binding_id)
    assert events == 1


async def test_reviewer_automation_approving_actually_approves(engine: AsyncEngine) -> None:
    """The uncontended case, which the previous code got wrong: the agent
    passed the terminal status ("APPROVED") where the verdict ("APPROVE") was
    expected, and the shared core read anything that was not exactly
    "APPROVE" as a rejection -- so an auto-*approval* rejected the proposal
    and reported success. `TERMINAL_STATUS` now derives one from the other.

    Since AR-03/AR-04 the agent will not act on a recommendation it did not
    itself derive from live evidence, so this seeds a scored proposal and runs
    the real pre-review pass rather than writing "APPROVE" onto the row.
    """
    maker = _sessions(engine)
    async with maker() as setup:
        org = await _org(setup)
        review, draft = await _seed_agent_decidable_review(setup, org)
        await reviewer_agent.pre_review_pending(setup, org.id, settings=_agent_settings())
        assert review.pre_review_recommendation == "APPROVE"
        await setup.commit()
        organization_id, review_id = org.id, review.id
        draft_id = draft.id

    async with maker() as agent_session:
        outcomes = await reviewer_agent.auto_decide_tier0_tier1(
            agent_session, organization_id, settings=_agent_settings()
        )
        await agent_session.commit()

    assert [outcome.decision for outcome in outcomes] == ["APPROVED"]

    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "APPROVED"
        assert final.decided_by == "agent:reviewer"
        applied = await reader.get(AssetDescriptionDraft, draft_id)
        assert applied is not None
        assert applied.status == "APPROVED", "an auto-approval must publish, not reject"
        assert applied.published_version_id is not None

    _, events = await _side_effect_counts(engine, review_id, draft_id)
    assert events == 1


async def test_reviewer_automation_abstains_without_object_specific_evidence(
    engine: AsyncEngine,
) -> None:
    """AR-03, at the decision boundary rather than at the rule.

    `TERM_SEMANTIC_BINDING` is T1 and inside the ceiling, so the old rule
    approved it on the strength of nothing arguing against it. It is a
    steward's unscored assertion; an agent agreeing with it adds throughput
    and no independent check, so the agent now leaves it alone -- even with an
    APPROVE recommendation written onto the row by hand.
    """
    maker = _sessions(engine)
    async with maker() as setup:
        org = await _org(setup)
        term = await _term(setup, org)
        review = await _seed_binding_and_review(setup, org, term)
        review.pre_reviewed_at = datetime.now(UTC)
        review.pre_review_recommendation = "APPROVE"
        review.risk_tier = "T1"
        await setup.commit()
        organization_id, review_id = org.id, review.id
        binding_id = UUID(review.object_id)

    async with maker() as agent_session:
        outcomes = await reviewer_agent.auto_decide_tier0_tier1(
            agent_session, organization_id, settings=_agent_settings()
        )
        await agent_session.commit()

    assert outcomes == []
    async with maker() as reader:
        final = await _load(reader, review_id)
        assert final.status == "PENDING"
        binding = await reader.get(TermSemanticBinding, binding_id)
        assert binding is not None
        assert binding.status == "PENDING_APPROVAL"


# ---------------------------------------------------------------------------
# 5. the service's own contract
# ---------------------------------------------------------------------------


async def test_claiming_a_review_twice_in_one_session_refuses_the_second(
    session: AsyncSession,
) -> None:
    """The narrowest statement of the invariant, with no endpoints involved:
    a review is claimable exactly once, and the second attempt is told what
    the first one wrote."""
    org = await _org(session)
    term = await _term(session, org)
    review = await _seed_binding_and_review(session, org, term)
    now = datetime.now(UTC)

    await governance_decision_service.claim_review(
        session,
        review,
        decision="APPROVE",
        reason="first",
        context=_context(org, "checker-one"),
        now=now,
    )
    assert review.status == "APPROVED"

    with pytest.raises(GovernanceDecisionRefused) as raised:
        await governance_decision_service.claim_review(
            session,
            review,
            decision="REJECT",
            reason="second",
            context=_context(org, "checker-two"),
            now=now,
        )
    assert raised.value.outcome == "CONFLICT"
    assert raised.value.review_state is not None
    assert raised.value.review_state.status == "APPROVED"
    assert raised.value.review_state.decided_by == "checker-one"


async def test_a_terminal_status_is_not_accepted_where_a_verdict_belongs(
    session: AsyncSession,
) -> None:
    """`normalize_verdict` refuses "APPROVED" loudly. The old expression
    (`"APPROVED" if decision == "APPROVE" else "REJECTED"`) silently turned
    that mistake into a rejection, which is how the reviewer agent's
    auto-approvals came to reject."""
    org = await _org(session)
    term = await _term(session, org)
    review = await _seed_binding_and_review(session, org, term)

    with pytest.raises(ValueError, match="unsupported governance decision verdict"):
        await governance_decision_service.claim_review(
            session,
            review,
            decision="APPROVED",
            reason="wrong vocabulary",
            context=_context(org, "checker"),
            now=datetime.now(UTC),
        )
    assert review.status == "PENDING"


def test_every_object_type_the_queue_classifies_has_a_registered_adapter() -> None:
    """The registry is populated by `semantic_api` at import time (R03: the
    service must not import the router to find its adapters). This is the
    check that the arrangement stays complete rather than degrading into a
    422 somebody discovers in production.

    The object types deliberately *not* decided through this queue are named
    individually, so adding a new governed type fails here until it is either
    given an adapter or recorded as out of scope.
    """
    # Classified for ranking/tiering but decided by their own endpoints, not
    # by the unified governance queue.
    decided_elsewhere = {
        "ACCESS_POLICY",
        "AGENT_CONTRACT",
        "BUSINESS_ANNOTATION",
        "GLOSSARY_TERM",
        "GOVERNED_TOOL",
        "SEMANTIC_METRIC",
        "SOURCE_BINDING",
        "TOOL_CERTIFICATION_RUN",
        "WORKSPACE_MEMBERSHIP",
    }
    missing = governance_decision_service.unregistered_object_types(
        known_object_types() - decided_elsewhere
    )
    assert missing == frozenset()
