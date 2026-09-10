"""Behavioral coverage for the governed-tool version publish/reject/deprecate
lifecycle, driven through `decide_governance_review` (the shared maker-checker
decision endpoint in `semantic_api.py`). Before this file, nothing in the suite
called `decide_governance_review` at all -- the only evidence for this lifecycle
was that the review-request routes in `tool_api.py` existed.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.expression import UpdateBase

from aida.models import AuditEvent, GovernanceReview, GovernedToolVersion, OutboxEvent
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from tests.support.doubles import ScriptedDmlResult


class _GovernanceDecisionSession:
    """Fake session for `decide_governance_review`: `.get()` pops preset results
    in call order (review, then the reviewed object), `.execute()` records every
    UPDATE statement issued (so the supersede logic can be verified against its
    compiled SQL, not just trusted), and `.add()`/`.commit()` behave like the
    other fake sessions in this suite.
    """

    def __init__(self, *, get_results: list[object]) -> None:
        self._get_queue = list(get_results)
        self.added: list[object] = []
        self.executed_statements: list[object] = []
        self.timeline: list[str] = []

    async def get(self, _model: type[object], _identity: object) -> object:
        return self._get_queue.pop(0)

    async def scalar(self, _statement: object) -> object:
        return self._get_queue.pop(0)

    async def execute(self, statement: object) -> object:
        """Record the statement, and answer DML the way a real session does.

        `governance_decision_service.claim_review` decides whether it won the
        review by reading `rowcount` off this result (review-2026-09-05 F05).
        Returning None modelled a session that no longer exists: every write
        path through the decision service now issues a compare-and-set UPDATE
        first. `rowcount = 1` is the honest answer for this double, which holds
        exactly the one review the claim targets and is not simulating
        contention -- the concurrency behaviour itself is proven against real
        connections in `tests/test_governance_decision_concurrency.py`.
        """
        self.executed_statements.append(statement)
        if isinstance(statement, UpdateBase):
            return ScriptedDmlResult(rowcount=1)
        return None


    @property
    def target_statements(self) -> list[object]:
        """Statements other than the decision service's compare-and-set claim.

        Every decision now issues one `UPDATE governance_review SET status=...
        WHERE id=... AND status='PENDING'` before any target work runs
        (review-2026-09-05 F05). That claim is not what these assertions are
        about -- they are about what the *target adapter* did -- so it is
        excluded here rather than by loosening `== []` into `<= 1`, which
        would stop the assertions catching a stray supersede.
        """
        return [
            statement
            for statement in self.executed_statements
            if getattr(getattr(statement, "table", None), "name", None) != "governance_review"
        ]

    @property
    def claim_statements(self) -> list[object]:
        """The compare-and-set claims, so a test can assert one was issued."""
        return [
            statement
            for statement in self.executed_statements
            if getattr(getattr(statement, "table", None), "name", None) == "governance_review"
        ]

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.timeline.append("commit")


def _pending_tool_version_review(
    *, organization_id, tool_version_id, requested_action: str
) -> GovernanceReview:
    return GovernanceReview(
        id=uuid4(),
        organization_id=organization_id,
        object_type="GOVERNED_TOOL_VERSION",
        object_id=str(tool_version_id),
        requested_action=requested_action,
        status="PENDING",
        requested_by="tool-dev",
    )


def _reviewer_context(*, organization_id) -> SecurityContext:
    return SecurityContext(
        principal_id="reviewer",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


def _sample_tool_version(
    *, organization_id, tool_id, version: int, status: str
) -> GovernedToolVersion:
    return GovernedToolVersion(
        id=uuid4(),
        organization_id=organization_id,
        tool_id=tool_id,
        version=version,
        status=status,
        name="Quarterly revenue by region",
        description="A governed tool used for the lifecycle tests.",
        datasource_id=uuid4(),
        sql_template="SELECT 1",
        referenced_tables=[],
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint=f"fingerprint-v{version}",
        created_by="tool-dev",
    )


async def test_approving_publish_promotes_the_version_and_supersedes_the_prior_published_one() -> (
    None
):
    organization_id = uuid4()
    tool_id = uuid4()
    candidate = _sample_tool_version(
        organization_id=organization_id, tool_id=tool_id, version=2, status="REVIEW_REQUIRED"
    )
    review = _pending_tool_version_review(
        organization_id=organization_id, tool_version_id=candidate.id, requested_action="PUBLISH"
    )
    session = _GovernanceDecisionSession(get_results=[review, candidate])

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _reviewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert candidate.status == "PUBLISHED"
    assert candidate.approved_by == "reviewer"
    assert candidate.approved_at is not None
    assert result.status == "APPROVED"
    assert result.decided_by == "reviewer"

    # The prior published version for this same tool must be superseded --
    # verify the actual UPDATE statement issued, not just the end state.
    assert len(session.target_statements) == 1
    compiled = str(
        session.target_statements[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "governed_tool_version" in compiled
    assert "SUPERSEDED" in compiled
    assert "PUBLISHED" in compiled
    # UUID literal-binds compile without hyphens; compare on the hex form.
    assert candidate.id.hex in compiled  # excludes the version being promoted

    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "tool.version.published.v1"
        for value in session.added
    )
    assert any(isinstance(value, AuditEvent) for value in session.added)
    assert session.timeline == ["commit"]


async def test_rejecting_publish_marks_the_version_rejected_without_superseding_anything() -> None:
    organization_id = uuid4()
    tool_id = uuid4()
    candidate = _sample_tool_version(
        organization_id=organization_id, tool_id=tool_id, version=2, status="REVIEW_REQUIRED"
    )
    review = _pending_tool_version_review(
        organization_id=organization_id, tool_version_id=candidate.id, requested_action="PUBLISH"
    )
    session = _GovernanceDecisionSession(get_results=[review, candidate])

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(
            decision="REJECT", reason="SQL template drifted from the approved semantic model."
        ),
        _reviewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert candidate.status == "REJECTED"
    assert candidate.approved_by is None
    assert result.status == "REJECTED"
    # A rejection never touches any other version's status.
    assert session.target_statements == []
    # The decision still claimed the review exactly once: a decision that
    # supersedes nothing is not a decision that skipped the F05 guard.
    assert len(session.claim_statements) == 1
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "tool.version.rejected.v1"
        for value in session.added
    )


async def test_approving_deprecation_moves_a_published_version_to_deprecated() -> None:
    organization_id = uuid4()
    tool_id = uuid4()
    published = _sample_tool_version(
        organization_id=organization_id, tool_id=tool_id, version=3, status="PUBLISHED"
    )
    review = _pending_tool_version_review(
        organization_id=organization_id, tool_version_id=published.id, requested_action="DEPRECATE"
    )
    session = _GovernanceDecisionSession(get_results=[review, published])

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _reviewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert published.status == "DEPRECATED"
    # Deprecation never runs the supersede-prior-published-version update.
    assert session.target_statements == []
    # The decision still claimed the review exactly once: a decision that
    # supersedes nothing is not a decision that skipped the F05 guard.
    assert len(session.claim_statements) == 1
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "tool.version.deprecated.v1"
        for value in session.added
    )


async def test_deprecation_request_is_rejected_when_the_version_is_no_longer_published() -> None:
    organization_id = uuid4()
    tool_id = uuid4()
    # Already deprecated (or otherwise not published) by the time the review is decided.
    stale = _sample_tool_version(
        organization_id=organization_id, tool_id=tool_id, version=3, status="DRAFT"
    )
    review = _pending_tool_version_review(
        organization_id=organization_id, tool_version_id=stale.id, requested_action="DEPRECATE"
    )
    session = _GovernanceDecisionSession(get_results=[review, stale])

    with pytest.raises(HTTPException) as denied:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            _reviewer_context(organization_id=organization_id),
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert session.timeline == []  # rejected before any commit


async def test_the_reviewer_cannot_decide_a_review_they_requested_themselves() -> None:
    organization_id = uuid4()
    tool_id = uuid4()
    candidate = _sample_tool_version(
        organization_id=organization_id, tool_id=tool_id, version=2, status="REVIEW_REQUIRED"
    )
    review = _pending_tool_version_review(
        organization_id=organization_id, tool_version_id=candidate.id, requested_action="PUBLISH"
    )
    session = _GovernanceDecisionSession(get_results=[review])
    same_principal_context = SecurityContext(
        principal_id="tool-dev",  # matches review.requested_by
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )

    with pytest.raises(HTTPException) as denied:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            same_principal_context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert "maker-checker" in denied.value.detail
