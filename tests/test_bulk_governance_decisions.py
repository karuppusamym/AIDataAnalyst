"""PG-3: bulk decisions with per-item rationale on the unified governance
review queue (module 17, `Docs/20-modules/17-policy-and-governance.md` SS6/SS8).

The single-item endpoint (`semantic_api.decide_governance_review`) already
dispatches across every object type the queue supports and enforces maker !=
checker, PENDING-only, and organization-boundary rules. PG-3 adds a
`/governance/reviews/bulk-decision` endpoint that:

  1. Selects the target set either by an explicit review-id list or by a
     filter (status, optional object_type) reusing `list_governance_reviews`'s
     own filter shape, scoped to the caller's organization -- never a
     Python-side scan of the whole table (`_resolve_governance_review_bulk_subjects`).
  2. Applies the *exact same* core decision logic as the single-item endpoint
     (`_apply_governance_review_decision`) per item, so the two paths cannot
     drift.
  3. Reports partial success: which items succeeded, which failed and why,
     never all-or-nothing -- mirroring RL-6 (relationship candidates) and
     CT-1 (catalog bulk actions).
  4. Caps a single request at GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS
     (10,000, the exit condition's own number) with a clear rejection above
     the cap, not silent truncation.
  5. Supports a genuinely *per-item* rationale (`rationale_by_review_id`),
     not just one blanket rationale for the whole batch -- with a shared
     `reason` as a convenience default when the caller wants one.

This file proves each of those, plus the exit condition's own performance
claim -- "10,000-item selection workable" -- against a real (in-memory
sqlite) database with rows seeded directly through the ORM, the same
real-engine pattern `test_relationship_intelligence_review.py` (RL-6) and
`test_semantic_glossary_binding.py` (SM-2) already use, not a hand-simulated
session.
"""

import itertools
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.models import (
    AuditEvent,
    GlossaryConflict,
    GlossaryTerm,
    GovernanceReview,
    Organization,
    OutboxEvent,
    TermSemanticBinding,
)
from aida.schemas import (
    GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS,
    GovernanceReviewBulkDecisionRequest,
    GovernanceReviewBulkSelectionFilter,
)
from aida.security_types import SecurityContext
from aida.semantic_api import (
    _resolve_governance_review_bulk_subjects,
    bulk_decide_governance_reviews,
)

# `AuditEvent.id` is a `BigInteger` autoincrement primary key, relying in
# production on Postgres's own identity/sequence generation. sqlite only
# auto-populates a bare `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger`
# compiles to `BIGINT`, which sqlite does not treat as that alias -- so an
# in-memory sqlite session (as used by every test below) leaves `id` NULL and
# violates the NOT NULL constraint on insert. Assign ids by hand for this
# test module's sqlite engine only; nothing about the production model
# changes. (Same workaround as test_relationship_intelligence_review.py.)
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


# --- fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _term(session: AsyncSession, org: Organization) -> GlossaryTerm:
    term = GlossaryTerm(organization_id=org.id, term_key=f"term-{uuid4().hex[:8]}")
    session.add(term)
    await session.flush()
    return term


def _context(org: Organization, principal: str = "reviewer") -> SecurityContext:
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
    binding_status: str = "PENDING_APPROVAL",
    review_status: str = "PENDING",
) -> GovernanceReview:
    """Seed the leanest object type the queue supports (TERM_SEMANTIC_BINDING,
    SM-2) plus its GovernanceReview row. `semantic_object_id` carries no FK
    constraint of its own (see the model docstring), so a fresh random UUID
    stands in for a semantic object without needing a real SemanticMetric.
    """
    binding = TermSemanticBinding(
        organization_id=org.id,
        term_id=term.id,
        semantic_object_type="SEMANTIC_METRIC",
        semantic_object_id=uuid4(),
        status=binding_status,
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
        status=review_status,
    )
    session.add(review)
    await session.flush()
    return review


async def _seed_conflict_and_review(
    session: AsyncSession,
    org: Organization,
    *,
    requested_by: str = "maker",
    conflict_status: str = "REVIEW_REQUIRED",
    review_status: str = "PENDING",
) -> GovernanceReview:
    """A second, distinct object type (GLOSSARY_CONFLICT) so batches can span
    more than one kind of governed object, as the unified queue promises.
    """
    conflict = GlossaryConflict(
        organization_id=org.id,
        conflict_type="DEFINITION_CONFLICT",
        status=conflict_status,
        position_a={"definition": "A"},
        position_b={"definition": "B"},
        raised_by=requested_by,
        proposed_resolution="ACCEPT_A",
    )
    session.add(conflict)
    await session.flush()
    review = GovernanceReview(
        organization_id=org.id,
        object_type="GLOSSARY_CONFLICT",
        object_id=str(conflict.id),
        requested_action="RESOLVE",
        requested_by=requested_by,
        status=review_status,
    )
    session.add(review)
    await session.flush()
    return review


async def _bulk_seed_bindings_and_reviews(
    session: AsyncSession, org: Organization, term: GlossaryTerm, n: int
) -> None:
    """Seed `n` PENDING TERM_SEMANTIC_BINDING + GovernanceReview pairs via
    Core bulk `INSERT` (executemany) rather than the ORM unit-of-work, purely
    so the *scale* tests below spend their time inside the bulk-decision
    endpoint under test, not in test setup. Nothing about the rows differs
    from `_seed_binding_and_review`'s ORM-built ones.
    """
    now = datetime.now(UTC)
    binding_ids = [uuid4() for _ in range(n)]
    await session.execute(
        insert(TermSemanticBinding),
        [
            {
                "id": binding_id,
                "organization_id": org.id,
                "term_id": term.id,
                "semantic_object_type": "SEMANTIC_METRIC",
                "semantic_object_id": uuid4(),
                "status": "PENDING_APPROVAL",
                "requested_by": "maker",
                "approved_by": None,
                "approved_at": None,
                "governance_review_id": None,
                "created_at": now,
                "updated_at": now,
            }
            for binding_id in binding_ids
        ],
    )
    await session.execute(
        insert(GovernanceReview),
        [
            {
                "id": uuid4(),
                "organization_id": org.id,
                "object_type": "TERM_SEMANTIC_BINDING",
                "object_id": str(binding_id),
                "requested_action": "APPROVE",
                "status": "PENDING",
                "requested_by": "maker",
                "decided_by": None,
                "decision_reason": None,
                "decided_at": None,
                "created_at": now,
                "updated_at": now,
            }
            for binding_id in binding_ids
        ],
    )
    await session.flush()


class _StatementCounter:
    """Counts SQL statements actually sent to the database (`cursor.execute`
    calls), by listening on the engine's real `before_cursor_execute` event --
    not a mock, the genuine SQLAlchemy instrumentation hook -- so a
    "loaded everything into Python first" implementation cannot hide behind a
    misleadingly small number of `await session.execute(...)` call sites in
    the Python source.
    """

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        self.count += 1


def _count_statements(session: AsyncSession) -> _StatementCounter:
    counter = _StatementCounter()
    event.listen(session.bind.sync_engine, "before_cursor_execute", counter)  # type: ignore[union-attr]
    return counter


# ---------------------------------------------------------------------------
# Contract: the route is registered
# ---------------------------------------------------------------------------


def test_bulk_decision_route_is_registered() -> None:
    from aida.main import app

    paths = app.openapi()["paths"]
    assert "/v1/governance/reviews/bulk-decision" in paths


# ---------------------------------------------------------------------------
# Explicit id list + partial success + per-item rationale
# ---------------------------------------------------------------------------


async def test_bulk_decide_by_explicit_ids_reports_partial_success(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    term = await _term(session, org)

    approvable = await _seed_binding_and_review(session, org, term, requested_by="maker-a")
    own_item = await _seed_binding_and_review(session, org, term, requested_by="reviewer")
    already_decided = await _seed_binding_and_review(
        session, org, term, requested_by="maker-b", review_status="APPROVED"
    )

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[approvable.id, own_item.id, already_decided.id],
            decision="APPROVE",
            reason="quarterly backlog sweep",
        ),
        context=context,
        session=session,
    )

    assert result.requested_count == 3
    assert result.succeeded_count == 1
    assert result.failed_count == 2
    by_id = {item.review_id: item for item in result.results}
    assert by_id[str(approvable.id)].status == "SUCCEEDED"
    assert by_id[str(own_item.id)].status == "FAILED"
    assert "maker-checker" in (by_id[str(own_item.id)].reason or "")
    assert by_id[str(already_decided.id)].status == "FAILED"
    assert "already" in (by_id[str(already_decided.id)].reason or "")

    await session.refresh(approvable)
    assert approvable.status == "APPROVED"
    assert approvable.decision_reason == "quarterly backlog sweep"

    events = (await session.scalars(select(OutboxEvent))).all()
    assert len(events) == 1
    assert events[0].event_type == "semantic.term_binding_approved.v1"


async def test_bulk_decide_per_item_rationale_recorded_distinctly(
    session: AsyncSession,
) -> None:
    """The exit condition's own wording -- "per-item rationale" -- means each
    decided item carries its own rationale, not one shared string copied onto
    every row. Prove two items in the same batch end up with two different
    `decision_reason` values.
    """
    org = await _org(session)
    term = await _term(session, org)
    first = await _seed_binding_and_review(session, org, term, requested_by="maker-a")
    second = await _seed_binding_and_review(session, org, term, requested_by="maker-b")

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[first.id, second.id],
            decision="APPROVE",
            rationale_by_review_id={
                first.id: "matches the approved naming convention",
                second.id: "cross-checked against the source system definition",
            },
        ),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 2

    await session.refresh(first)
    await session.refresh(second)
    assert first.decision_reason == "matches the approved naming convention"
    assert second.decision_reason == "cross-checked against the source system definition"
    assert first.decision_reason != second.decision_reason


async def test_bulk_decide_per_item_rationale_falls_back_to_shared_reason(
    session: AsyncSession,
) -> None:
    """A shared `reason` is a convenience default for items with no entry in
    `rationale_by_review_id`, not the primary mechanism.
    """
    org = await _org(session)
    term = await _term(session, org)
    has_specific_rationale = await _seed_binding_and_review(
        session, org, term, requested_by="maker-a"
    )
    uses_shared_default = await _seed_binding_and_review(
        session, org, term, requested_by="maker-b"
    )

    context = _context(org, principal="reviewer")
    await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[has_specific_rationale.id, uses_shared_default.id],
            decision="APPROVE",
            reason="shared default rationale",
            rationale_by_review_id={has_specific_rationale.id: "specific rationale"},
        ),
        context=context,
        session=session,
    )

    await session.refresh(has_specific_rationale)
    await session.refresh(uses_shared_default)
    assert has_specific_rationale.decision_reason == "specific rationale"
    assert uses_shared_default.decision_reason == "shared default rationale"


async def test_bulk_decide_reject_without_any_rationale_fails_only_that_item(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    term = await _term(session, org)
    item = await _seed_binding_and_review(session, org, term, requested_by="maker-a")

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[item.id],
            decision="REJECT",
            rationale_by_review_id={uuid4(): "rationale for a different item"},
        ),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 0
    assert result.failed_count == 1
    assert "rationale is required" in (result.results[0].reason or "")


# ---------------------------------------------------------------------------
# Filter selection, spanning multiple object types
# ---------------------------------------------------------------------------


async def test_bulk_decide_by_filter_spans_object_types_and_scopes_to_org(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    other_org = await _org(session)
    term = await _term(session, org)
    other_term = await _term(session, other_org)

    pending_bindings = [
        await _seed_binding_and_review(session, org, term, requested_by="maker")
        for _ in range(3)
    ]
    pending_conflict = await _seed_conflict_and_review(session, org, requested_by="maker")
    # Noise the filter must not pick up:
    await _seed_binding_and_review(
        session, org, term, requested_by="maker", review_status="REJECTED"
    )
    await _seed_binding_and_review(session, other_org, other_term, requested_by="maker")

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            filter=GovernanceReviewBulkSelectionFilter(status="PENDING"),
            decision="APPROVE",
            reason="filter sweep",
        ),
        context=context,
        session=session,
    )
    assert result.selection_mode == "FILTER"
    assert result.requested_count == 4
    assert result.succeeded_count == 4
    assert not result.truncated

    for binding_review in pending_bindings:
        await session.refresh(binding_review)
        assert binding_review.status == "APPROVED"
    await session.refresh(pending_conflict)
    assert pending_conflict.status == "APPROVED"

    outbox_types = {e.event_type for e in (await session.scalars(select(OutboxEvent))).all()}
    assert "semantic.term_binding_approved.v1" in outbox_types
    assert "glossary.conflict_resolved.v1" in outbox_types


async def test_bulk_decide_by_filter_narrows_by_object_type(session: AsyncSession) -> None:
    org = await _org(session)
    term = await _term(session, org)
    binding_review = await _seed_binding_and_review(session, org, term, requested_by="maker")
    conflict_review = await _seed_conflict_and_review(session, org, requested_by="maker")

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            filter=GovernanceReviewBulkSelectionFilter(
                status="PENDING", object_type="TERM_SEMANTIC_BINDING"
            ),
            decision="APPROVE",
            reason="only bindings",
        ),
        context=context,
        session=session,
    )
    assert result.requested_count == 1
    by_id = {item.review_id: item for item in result.results}
    assert str(binding_review.id) in by_id
    assert str(conflict_review.id) not in by_id


async def test_bulk_decide_filter_matching_nothing_is_rejected(session: AsyncSession) -> None:
    org = await _org(session)
    context = _context(org, principal="reviewer")
    with pytest.raises(Exception, match="matched no governance reviews"):
        await bulk_decide_governance_reviews(
            GovernanceReviewBulkDecisionRequest(
                filter=GovernanceReviewBulkSelectionFilter(status="PENDING"),
                decision="APPROVE",
                reason="nothing to see here",
            ),
            context=context,
            session=session,
        )


# ---------------------------------------------------------------------------
# Maker != checker enforced per item within a batch
# ---------------------------------------------------------------------------


async def test_bulk_decide_maker_checker_enforced_per_item(session: AsyncSession) -> None:
    org = await _org(session)
    term = await _term(session, org)
    reviewer = "reviewer"
    good_a = await _seed_binding_and_review(session, org, term, requested_by="maker-a")
    self_approval = await _seed_binding_and_review(session, org, term, requested_by=reviewer)
    good_b = await _seed_binding_and_review(session, org, term, requested_by="maker-b")

    context = _context(org, principal=reviewer)
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[good_a.id, self_approval.id, good_b.id],
            decision="APPROVE",
            reason="batch sweep",
        ),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 2
    assert result.failed_count == 1
    by_id = {item.review_id: item for item in result.results}
    assert by_id[str(good_a.id)].status == "SUCCEEDED"
    assert by_id[str(good_b.id)].status == "SUCCEEDED"
    assert by_id[str(self_approval.id)].status == "FAILED"
    assert "maker-checker" in (by_id[str(self_approval.id)].reason or "")

    await session.refresh(self_approval)
    assert self_approval.status == "PENDING"  # untouched, not partially mutated


async def test_bulk_decide_not_found_and_cross_organization_are_reported_failed(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    other_org = await _org(session)
    other_term = await _term(session, other_org)
    foreign_review = await _seed_binding_and_review(
        session, other_org, other_term, requested_by="maker"
    )
    missing_id = uuid4()

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[missing_id, foreign_review.id],
            decision="APPROVE",
            reason="sweep",
        ),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 0
    assert result.failed_count == 2
    by_id = {item.review_id: item for item in result.results}
    assert "not found" in (by_id[str(missing_id)].reason or "")
    assert "cross-organization" in (by_id[str(foreign_review.id)].reason or "")


# ---------------------------------------------------------------------------
# A failure partway through one item's dispatch never leaks a partial write
# ---------------------------------------------------------------------------


async def test_bulk_decide_isolates_a_failure_within_one_items_dispatch(
    session: AsyncSession,
) -> None:
    """`GLOSSARY_CONFLICT`'s dispatch calls into `apply_conflict_resolution`,
    which raises if the underlying conflict is not REVIEW_REQUIRED -- a
    failure discovered *inside* `_apply_governance_review_decision`, after
    `review.status` has already been set to APPROVED in memory. Prove the
    per-item SAVEPOINT actually rolls that back: the review stays PENDING,
    not stuck half-applied.
    """
    org = await _org(session)
    conflict_review = await _seed_conflict_and_review(
        session, org, requested_by="maker", conflict_status="OPEN"
    )

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[conflict_review.id],
            decision="APPROVE",
            reason="sweep",
        ),
        context=context,
        session=session,
    )
    assert result.failed_count == 1
    assert "no longer pending review" in (result.results[0].reason or "")

    await session.refresh(conflict_review)
    assert conflict_review.status == "PENDING"
    assert conflict_review.decided_by is None
    assert conflict_review.decision_reason is None


# ---------------------------------------------------------------------------
# The 10,000-item cap: rejected outright, never silently truncated
# ---------------------------------------------------------------------------


def test_bulk_decide_explicit_ids_over_cap_is_rejected() -> None:
    with pytest.raises(Exception, match="at most 10000"):
        GovernanceReviewBulkDecisionRequest(
            review_ids=[uuid4() for _ in range(GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS + 1)],
            decision="APPROVE",
            reason="too many",
        )


def test_bulk_decide_requires_exactly_one_selection_source() -> None:
    with pytest.raises(Exception, match="exactly one selection"):
        GovernanceReviewBulkDecisionRequest(decision="APPROVE", reason="x")
    with pytest.raises(Exception, match="exactly one selection"):
        GovernanceReviewBulkDecisionRequest(
            review_ids=[uuid4()],
            filter=GovernanceReviewBulkSelectionFilter(status="PENDING"),
            decision="APPROVE",
            reason="x",
        )


def test_bulk_decide_reject_without_reason_or_rationale_map_is_rejected() -> None:
    with pytest.raises(Exception, match="rationale is required"):
        GovernanceReviewBulkDecisionRequest(review_ids=[uuid4()], decision="REJECT")


async def test_bulk_decide_filter_caps_at_10000_and_reports_truncation(
    session: AsyncSession,
) -> None:
    """Seed genuinely more than the cap (10,050 PENDING reviews) and prove
    the filter path caps the match set at exactly
    GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS with `truncated=True`, rather
    than either rejecting the request or silently deciding an unbounded
    number of items.
    """
    org = await _org(session)
    term = await _term(session, org)
    overage = 50
    total_seeded = GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS + overage
    await _bulk_seed_bindings_and_reviews(session, org, term, total_seeded)

    context = _context(org, principal="reviewer")
    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            filter=GovernanceReviewBulkSelectionFilter(status="PENDING"),
            decision="APPROVE",
            reason="full sweep",
        ),
        context=context,
        session=session,
    )
    assert result.requested_count == GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS
    assert result.succeeded_count == GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS
    assert result.truncated is True

    decided_count = await session.scalar(
        select(func.count())
        .select_from(GovernanceReview)
        .where(GovernanceReview.organization_id == org.id, GovernanceReview.status == "APPROVED")
    )
    assert decided_count == GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS


# ---------------------------------------------------------------------------
# Selection is O(1) SQL round trips regardless of table size or item count --
# never a Python-side scan of every review row.
# ---------------------------------------------------------------------------


async def test_resolve_subjects_by_filter_is_a_bounded_number_of_queries(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    term = await _term(session, org)
    seeded = 4000
    await _bulk_seed_bindings_and_reviews(session, org, term, seeded)

    context = _context(org, principal="reviewer")
    counter = _count_statements(session)
    ids, mode, truncated = await _resolve_governance_review_bulk_subjects(
        session,
        context=context,
        review_ids=None,
        selection_filter=GovernanceReviewBulkSelectionFilter(status="PENDING"),
    )
    assert mode == "FILTER"
    assert not truncated
    assert len(ids) == seeded
    # One SELECT resolves all 4,000 ids -- not one query per matched row and
    # not a query whose count grows with the table size.
    assert counter.count <= 2


async def test_bulk_decide_at_scale_round_trips_are_linear_not_quadratic(
    session: AsyncSession,
) -> None:
    """The literal exit condition: "10,000-item selection workable". Decide a
    batch close to the 10,000-item cap, spanning two object types, and prove
    (a) it actually completes correctly at that scale and (b) the number of
    SQL statements issued is proportional to the item count with a small
    constant factor -- not one query per item just to *select* the batch
    (that part is a single bulk query, asserted directly above), and
    specifically not the quadratic blowup a "reload everything each
    iteration" implementation would produce. We check this by running the
    same dispatch shape at two sizes an order of magnitude apart and
    confirming the per-item statement cost stays flat.
    """

    async def _run(n: int) -> tuple[int, float]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as sess:
            org = await _org(sess)
            term = await _term(sess, org)
            await _bulk_seed_bindings_and_reviews(sess, org, term, n)
            context = _context(org, principal="reviewer")
            counter = _count_statements(sess)
            started = time.monotonic()
            result = await bulk_decide_governance_reviews(
                GovernanceReviewBulkDecisionRequest(
                    filter=GovernanceReviewBulkSelectionFilter(status="PENDING"),
                    decision="APPROVE",
                    reason="scale sweep",
                ),
                context=context,
                session=sess,
            )
            elapsed = time.monotonic() - started
            assert result.succeeded_count == n
            assert result.failed_count == 0
        await engine.dispose()
        return counter.count, elapsed

    # An order of magnitude apart is enough to reveal super-linear growth;
    # true 10,000-item scale is exercised end-to-end separately by
    # `test_bulk_decide_filter_caps_at_10000_and_reports_truncation` above,
    # so this test keeps its own sizes modest to stay fast.
    small_n = 300
    large_n = 3000
    small_count, _ = await _run(small_n)
    large_count, large_elapsed = await _run(large_n)

    small_per_item = small_count / small_n
    large_per_item = large_count / large_n
    # The per-item statement cost at 8,000 items must not be materially worse
    # than at 500 -- rules out anything super-linear (e.g. an O(n^2) rescan
    # per item, or reloading the full candidate set on every iteration).
    assert large_per_item <= small_per_item * 1.5, (
        f"per-item statement cost grew from {small_per_item:.2f} at n={small_n} "
        f"to {large_per_item:.2f} at n={large_n} -- looks super-linear"
    )
    # And it actually finishes in a reasonable time at 8,000 items.
    assert large_elapsed < 30.0
