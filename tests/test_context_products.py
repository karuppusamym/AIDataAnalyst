"""Contract and lifecycle coverage for governed Context Products."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.context_product_api import (
    _can_read_context_product_version,
    context_product_fingerprint,
    create_context_product_version,
    delete_context_product_consumer_binding,
    get_context_product_version,
    list_context_product_consumer_bindings,
    set_context_product_consumer_binding,
    update_context_product_version,
)
from aida.context_product_policy import (
    can_serve_pinned_version,
    current_published_version_number,
    evaluate_context_product_quality,
    is_version_retired,
    is_within_support_window,
    resolve_bound_version,
    was_previously_authorized_consumer,
)
from aida.db import Base
from aida.main import app
from aida.mcp_server import _context_product_role_eligible, _read_context_product_resource
from aida.models import (
    AuditEvent,
    ContextProduct,
    ContextProductConsumerBinding,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    GovernanceReview,
    Organization,
    OutboxEvent,
    Project,
)
from aida.schemas import (
    ContextProductConsumerBindingCreate,
    ContextProductDefinition,
    ContextProductVersionCreate,
    GovernanceDecisionRequest,
)
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review


class _GovernanceDecisionSession:
    def __init__(self, *, get_results: list[object]) -> None:
        self._get_queue = list(get_results)
        self.added: list[object] = []
        self.executed_statements: list[object] = []
        self.committed = False

    async def get(self, _model: type[object], _identity: object) -> object:
        return self._get_queue.pop(0)

    async def scalar(self, _statement: object) -> object:
        return self._get_queue.pop(0)

    async def execute(self, statement: object) -> None:
        self.executed_statements.append(statement)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _ContextProductReadSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.execute_count = 0
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _statement: object) -> object:
        row = self.row if self.execute_count == 0 else None
        self.execute_count += 1

        class _Result:
            def first(self_inner) -> object:
                return row

            def all(self_inner) -> list[object]:
                return []

        return _Result()

    async def scalars(self, _statement: object) -> object:
        class _Scalars:
            def all(self_inner) -> list[object]:
                return []

        return _Scalars()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def _definition(**changes: object) -> ContextProductDefinition:
    values: dict[str, object] = {
        "name": "Revenue analysis context",
        "description": "Approved definitions and tools for revenue analysis.",
        "purpose": "Give analyst agents bounded, governed revenue context.",
        "owner_type": "GROUP",
        "owner_principal": "data-product-owner",
        "table_ids": [uuid4()],
        "allowed_consumer_roles": ["Analyst"],
    }
    values.update(changes)
    return ContextProductDefinition.model_validate(values)


def _candidate(*, organization_id: UUID, product_id: UUID) -> ContextProductVersion:
    definition = _definition()
    return ContextProductVersion(
        id=uuid4(),
        organization_id=organization_id,
        product_id=product_id,
        version=2,
        status="REVIEW_REQUIRED",
        name=definition.name,
        description=definition.description,
        purpose=definition.purpose,
        owner_type=definition.owner_type,
        owner_principal=definition.owner_principal,
        table_ids=[str(value) for value in definition.table_ids],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=[],
        allowed_consumer_roles=definition.allowed_consumer_roles,
        lineage_depth=definition.lineage_depth,
        quality_requirements=definition.quality_requirements.model_dump(mode="json"),
        policy_summary=definition.policy_summary.model_dump(mode="json"),
        fingerprint=context_product_fingerprint(definition),
        created_by="product-author",
    )


def _review(*, organization_id: UUID, version_id: UUID) -> GovernanceReview:
    return GovernanceReview(
        id=uuid4(),
        organization_id=organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(version_id),
        requested_action="PUBLISH",
        status="PENDING",
        requested_by="product-author",
    )


def _reviewer(*, organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="independent-reviewer",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


def _published_product(
    *, allowed_roles: list[str]
) -> tuple[ContextProductVersion, ContextProduct]:
    organization_id = uuid4()
    product = ContextProduct(
        id=uuid4(),
        organization_id=organization_id,
        project_id=uuid4(),
        product_key="revenue_context",
        lifecycle_status="ACTIVE",
        created_by="product-author",
    )
    version = _candidate(organization_id=organization_id, product_id=product.id)
    version.status = "PUBLISHED"
    version.version = 1
    version.allowed_consumer_roles = allowed_roles
    version.published_at = datetime.now(UTC)
    return version, product


def test_definition_requires_at_least_one_governed_reference() -> None:
    with pytest.raises(ValidationError, match="at least one governed reference"):
        _definition(table_ids=[])


def test_definition_rejects_duplicate_references_and_roles() -> None:
    table_id = uuid4()
    with pytest.raises(ValidationError, match="reference identifiers must be unique"):
        _definition(table_ids=[table_id, table_id])
    with pytest.raises(ValidationError, match="consumer roles must be unique"):
        _definition(allowed_consumer_roles=["Analyst", "Analyst"])


def test_definition_fails_closed_on_raw_context_or_direct_source_access() -> None:
    with pytest.raises(ValidationError):
        _definition(policy_summary={"source_values": "DIRECT", "retention": "NO_RAW_CONTEXT"})
    with pytest.raises(ValidationError):
        _definition(policy_summary={"source_values": "GATEWAY_ONLY", "retention": "PERSIST"})


def test_fingerprint_is_deterministic_and_content_addressed() -> None:
    table_id = uuid4()
    first = _definition(table_ids=[table_id])
    same = _definition(table_ids=[table_id])
    changed = _definition(table_ids=[table_id], purpose="Support a different governed use case.")

    assert context_product_fingerprint(first) == context_product_fingerprint(same)
    assert context_product_fingerprint(first) != context_product_fingerprint(changed)


def test_openapi_exposes_context_product_authoring_and_review_routes() -> None:
    paths = app.openapi()["paths"]

    assert "post" in paths["/v1/projects/{project_id}/context-products"]
    assert "get" in paths["/v1/context-products/{product_id}/versions"]
    assert "put" in paths["/v1/context-product-versions/{version_id}"]
    assert "post" in paths["/v1/context-product-versions/{version_id}/submit"]
    assert "post" in paths["/v1/context-product-versions/{version_id}/deprecate"]


def test_context_product_role_binding_is_fail_closed_with_admin_exemption() -> None:
    assert _context_product_role_eligible(frozenset({"Analyst"}), ["Analyst"])
    assert not _context_product_role_eligible(frozenset({"Viewer"}), ["Analyst"])
    assert not _context_product_role_eligible(frozenset({"Analyst"}), [])
    assert _context_product_role_eligible(frozenset({"PlatformAdmin"}), [])


def test_rest_consumers_cannot_read_drafts_or_products_for_other_roles() -> None:
    organization_id = uuid4()
    version = _candidate(organization_id=organization_id, product_id=uuid4())
    analyst = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Analyst"}),
    )
    author = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    version.status = "DRAFT"
    assert not _can_read_context_product_version(analyst, version)
    assert _can_read_context_product_version(author, version)

    version.status = "PUBLISHED"
    assert _can_read_context_product_version(analyst, version)
    version.allowed_consumer_roles = ["RiskAnalyst"]
    assert not _can_read_context_product_version(analyst, version)


def test_quality_policy_fails_closed_for_missing_low_or_critical_evidence() -> None:
    first, second = uuid4(), uuid4()
    decision = evaluate_context_product_quality(
        table_ids=[first, second],
        minimum_score=85,
        deny_on_critical_incident=True,
        latest_scores={first: 72},
        critical_incident_table_ids={second},
    )

    assert decision.allowed is False
    assert decision.reasons == (
        "MISSING_QUALITY_EVIDENCE",
        "QUALITY_SCORE_BELOW_MINIMUM",
        "ACTIVE_CRITICAL_INCIDENT",
    )
    assert decision.lowest_score == 72


def test_quality_policy_allows_complete_healthy_evidence() -> None:
    table_id = uuid4()
    decision = evaluate_context_product_quality(
        table_ids=[table_id],
        minimum_score=85,
        deny_on_critical_incident=True,
        latest_scores={table_id: 96},
        critical_incident_table_ids=set(),
    )

    assert decision.allowed is True
    assert decision.reasons == ()


async def test_mcp_read_returns_version_pinned_value_free_context_and_evidence() -> None:
    version, product = _published_product(allowed_roles=["Analyst"])
    context = SecurityContext(
        principal_id="analyst-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = _ContextProductReadSession((version, product))
    uri = "atlas://context-products/revenue_context/versions/1"

    result = await _read_context_product_resource(
        uri, session, context, "corr-context-read"  # type: ignore[arg-type]
    )

    payload = json.loads(result["contents"][0]["text"])
    assert payload["product_key"] == "revenue_context"
    assert payload["version"] == 1
    assert payload["fingerprint"] == version.fingerprint
    assert "governed_references" in payload
    assert "rows" not in payload
    assert "source_values" not in payload
    assert payload["policy_summary"]["source_values"] == "GATEWAY_ONLY"
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "context.product_consumed.v1"
        for value in session.added
    )
    assert any(isinstance(value, ContextProductConsumptionEdge) for value in session.added)
    assert session.committed is True


async def test_mcp_read_hides_role_denial_behind_not_found_response() -> None:
    version, product = _published_product(allowed_roles=["RiskAnalyst"])
    context = SecurityContext(
        principal_id="viewer-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Viewer"}),
    )
    denied_session = _ContextProductReadSession((version, product))
    missing_session = _ContextProductReadSession(None)
    uri = "atlas://context-products/revenue_context/versions/1"

    denied = await _read_context_product_resource(
        uri, denied_session, context, "corr-denied"  # type: ignore[arg-type]
    )
    missing = await _read_context_product_resource(
        uri, missing_session, context, "corr-missing"  # type: ignore[arg-type]
    )

    assert denied == missing
    assert denied["contents"][0]["text"] == "Resource not found or not accessible."
    assert any(
        isinstance(value, AuditEvent)
        and value.action == "mcp.context_product.role_binding_denied"
        for value in denied_session.added
    )
    assert denied_session.committed is True
    assert missing_session.added == []


# --- AT-7(a)/AT-D1: support window and distinguishable retirement -----------


class _ContextProductRetiredReadSession:
    """Fake session for `_read_context_product_resource` retirement-signal
    coverage: `.execute()` returns the preset (version, product) row on its
    first call and an empty result after (same shape as
    `_ContextProductReadSession`); `.scalar()` pops preset values in call
    order for `was_previously_authorized_consumer` and
    `current_published_version_number`. An empty `scalar_results` queue that
    is never popped from proves those DB lookups were never even attempted.
    """

    def __init__(self, row: object, *, scalar_results: list[object]) -> None:
        self.row = row
        self.execute_count = 0
        self._scalar_queue = list(scalar_results)
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _statement: object) -> object:
        row = self.row if self.execute_count == 0 else None
        self.execute_count += 1

        class _Result:
            def first(self_inner) -> object:
                return row

        return _Result()

    async def scalar(self, _statement: object) -> object:
        return self._scalar_queue.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


class _ScalarQueueSession:
    """Minimal fake session exposing only `.scalar()`, for testing the
    context_product_policy helpers directly without a full read path."""

    def __init__(self, values: list[object]) -> None:
        self._queue = list(values)

    async def scalar(self, _statement: object) -> object:
        return self._queue.pop(0)


def test_is_within_support_window_and_can_serve_pinned_version() -> None:
    version, _ = _published_product(allowed_roles=["Analyst"])
    version.status = "SUPPORTED"

    version.support_window_ends_at = None
    assert is_within_support_window(version) is True
    assert can_serve_pinned_version(version) is True
    assert is_version_retired(version) is False

    version.support_window_ends_at = datetime.now(UTC) + timedelta(days=1)
    assert is_within_support_window(version) is True
    assert can_serve_pinned_version(version) is True

    version.support_window_ends_at = datetime.now(UTC) - timedelta(seconds=1)
    assert is_within_support_window(version) is False
    assert can_serve_pinned_version(version) is False
    assert is_version_retired(version) is True


def test_published_version_can_always_serve_and_is_never_retired() -> None:
    version, _ = _published_product(allowed_roles=["Analyst"])
    assert version.status == "PUBLISHED"
    assert can_serve_pinned_version(version) is True
    assert is_version_retired(version) is False


def test_is_version_retired_covers_terminal_statuses_but_not_pending_ones() -> None:
    version, _ = _published_product(allowed_roles=["Analyst"])
    for terminal_status in ("SUPERSEDED", "DEPRECATED"):
        version.status = terminal_status
        assert is_version_retired(version) is True
        assert can_serve_pinned_version(version) is False
    for pending_status in ("DRAFT", "REVIEW_REQUIRED", "REJECTED", "DEPRECATION_REVIEW"):
        version.status = pending_status
        assert is_version_retired(version) is False
        assert can_serve_pinned_version(version) is False


async def test_was_previously_authorized_consumer_requires_a_real_prior_allow_edge() -> None:
    """The subtle half of AT-7(a): proof of past authorization is an actual
    recorded consumption edge, not a role match -- see the docstring on
    `was_previously_authorized_consumer` itself for why a role match alone
    would let a role-eligible-but-never-consumed caller fish version numbers
    to discover which ones used to exist."""
    proven_session = _ScalarQueueSession([uuid4()])
    assert (
        await was_previously_authorized_consumer(
            proven_session, version_id=uuid4(), principal_id="analyst-agent"  # type: ignore[arg-type]
        )
        is True
    )

    unproven_session = _ScalarQueueSession([None])
    assert (
        await was_previously_authorized_consumer(
            unproven_session, version_id=uuid4(), principal_id="analyst-agent"  # type: ignore[arg-type]
        )
        is False
    )


async def test_current_published_version_number_returns_the_live_version_or_none() -> None:
    live_session = _ScalarQueueSession([3])
    assert await current_published_version_number(live_session, uuid4()) == 3  # type: ignore[arg-type]

    no_current_session = _ScalarQueueSession([None])
    assert await current_published_version_number(no_current_session, uuid4()) is None  # type: ignore[arg-type]


async def test_mcp_read_serves_a_supported_version_within_its_support_window() -> None:
    """A version-pinned MCP URI keeps pinning after a newer version is
    approved: the superseded version is SUPPORTED, not instantly hidden, and
    reads exactly like PUBLISHED for an eligible, in-window consumer."""
    version, product = _published_product(allowed_roles=["Analyst"])
    version.status = "SUPPORTED"
    version.support_window_ends_at = datetime.now(UTC) + timedelta(days=10)
    context = SecurityContext(
        principal_id="analyst-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = _ContextProductReadSession((version, product))
    uri = "atlas://context-products/revenue_context/versions/1"

    result = await _read_context_product_resource(
        uri, session, context, "corr-supported"  # type: ignore[arg-type]
    )

    payload = json.loads(result["contents"][0]["text"])
    assert payload["version"] == 1
    assert payload["_governance"]["status"] == "SUPPORTED"
    assert any(isinstance(value, ContextProductConsumptionEdge) for value in session.added)
    assert session.committed is True


async def test_mcp_retired_read_signals_retirement_when_previously_authorized() -> None:
    """Case 2 of AT-7(a)'s three-way test: pinned-and-retired, but the
    caller genuinely read this exact version before it was retired -- they
    get a distinguishable retirement signal, not the anti-enumeration
    "not found"."""
    version, product = _published_product(allowed_roles=["Analyst"])
    version.status = "SUPERSEDED"
    context = SecurityContext(
        principal_id="analyst-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    # scalar() call order: was_previously_authorized_consumer (proof found),
    # then current_published_version_number.
    session = _ContextProductRetiredReadSession(
        (version, product), scalar_results=[uuid4(), 2]
    )
    uri = "atlas://context-products/revenue_context/versions/1"

    result = await _read_context_product_resource(
        uri, session, context, "corr-retired"  # type: ignore[arg-type]
    )

    payload = json.loads(result["contents"][0]["text"])
    assert payload["status"] == "RETIRED"
    assert payload["version"] == 1
    assert payload["current_version"] == 2
    assert any(
        isinstance(value, AuditEvent) and value.action == "mcp.context_product.retired"
        for value in session.added
    )
    assert session.committed is True


async def test_mcp_retired_read_stays_anti_enumeration_when_never_authorized() -> None:
    """Case 3 of AT-7(a)'s three-way test: a caller whose role would be
    eligible but who never actually consumed this exact version gets the
    identical anti-enumeration "not found" as always -- a role match alone is
    not proof of prior authorization."""
    version, product = _published_product(allowed_roles=["Analyst"])
    version.status = "SUPERSEDED"
    context = SecurityContext(
        principal_id="never-consumed-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = _ContextProductRetiredReadSession((version, product), scalar_results=[None])
    uri = "atlas://context-products/revenue_context/versions/1"

    result = await _read_context_product_resource(
        uri, session, context, "corr-never-authorized"  # type: ignore[arg-type]
    )

    assert result["contents"][0]["text"] == "Resource not found or not accessible."


async def test_mcp_retired_read_denies_ineligible_role_without_history_lookup() -> None:
    """A role-ineligible caller gets the same anti-enumeration response as
    always, and the retirement-authorization history lookup is never even
    attempted for them -- an empty `scalar_results` queue that is never
    popped from proves the role gate short-circuits first."""
    version, product = _published_product(allowed_roles=["RiskAnalyst"])
    version.status = "SUPERSEDED"
    context = SecurityContext(
        principal_id="viewer-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Viewer"}),
    )
    session = _ContextProductRetiredReadSession((version, product), scalar_results=[])
    uri = "atlas://context-products/revenue_context/versions/1"

    result = await _read_context_product_resource(
        uri, session, context, "corr-ineligible-retired"  # type: ignore[arg-type]
    )

    assert result["contents"][0]["text"] == "Resource not found or not accessible."


def test_rest_read_gate_accepts_supported_within_window_like_published() -> None:
    """`_can_read_context_product_version` (the REST gate) treats a SUPPORTED
    version within its window exactly like PUBLISHED for an eligible role."""
    version, _ = _published_product(allowed_roles=["Analyst"])
    analyst = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=version.organization_id,
        roles=frozenset({"Analyst"}),
    )

    version.status = "SUPPORTED"
    version.support_window_ends_at = datetime.now(UTC) + timedelta(days=1)
    assert _can_read_context_product_version(analyst, version)

    version.support_window_ends_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not _can_read_context_product_version(analyst, version)

    version.status = "SUPERSEDED"
    assert not _can_read_context_product_version(analyst, version)


class _RestRetiredVersionReadSession:
    """Fake session for the REST `get_context_product_version` retirement
    branch: `.get()` serves `_version_scope`'s two lookups (the version,
    then its product) in order; `.scalar()` serves the retirement-check
    lookups (`was_previously_authorized_consumer`, then
    `current_published_version_number`) in order.
    """

    def __init__(self, *, version: object, product: object, scalar_results: list[object]) -> None:
        self._get_queue = [version, product]
        self._scalar_queue = list(scalar_results)
        self.added: list[object] = []
        self.committed = False

    async def get(self, _model: type[object], _identity: object) -> object:
        return self._get_queue.pop(0)

    async def scalar(self, _statement: object) -> object:
        return self._scalar_queue.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


async def test_rest_retired_read_returns_410_when_previously_authorized() -> None:
    """The REST twin of the MCP retirement-signal test: a caller who was
    genuinely authorized for this exact version before gets a distinguishable
    410, carrying the current published version to re-pin to -- not the same
    404 an unauthorized caller or a nonexistent version gets."""
    version, product = _published_product(allowed_roles=["Analyst"])
    version.status = "SUPERSEDED"
    context = SecurityContext(
        principal_id="analyst-agent",
        principal_type="USER",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = _RestRetiredVersionReadSession(
        version=version, product=product, scalar_results=[uuid4(), 2]
    )

    with pytest.raises(HTTPException) as retired:
        await get_context_product_version(
            version.id, context, session  # type: ignore[arg-type]
        )

    assert retired.value.status_code == 410
    assert retired.value.detail["error"] == "context_product_version_retired"
    assert retired.value.detail["current_version"] == 2
    assert session.committed is True


async def test_rest_read_of_a_retired_version_stays_404_for_a_never_authorized_consumer() -> None:
    version, product = _published_product(allowed_roles=["Analyst"])
    version.status = "SUPERSEDED"
    context = SecurityContext(
        principal_id="never-consumed-user",
        principal_type="USER",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = _RestRetiredVersionReadSession(
        version=version, product=product, scalar_results=[None]
    )

    with pytest.raises(HTTPException) as denied:
        await get_context_product_version(
            version.id, context, session  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 404


async def test_approval_publishes_candidate_and_supports_prior_version() -> None:
    """AT-7(a)/AT-D1: the version being replaced enters SUPPORTED (still
    readable by a version-pinned consumer for its support window), not
    fully-hidden SUPERSEDED, in the same transaction that publishes the new
    version."""
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    review = _review(organization_id=organization_id, version_id=candidate.id)
    # get_results is popped in call order: GovernanceReview (outer lookup),
    # then the candidate (`session.get`), then the prior PUBLISHED version's
    # own `support_window_days` (`session.scalar`) -- `None` here models a
    # product configured for "supported until explicit retirement".
    session = _GovernanceDecisionSession(get_results=[review, candidate, None])

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _reviewer(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert result.status == "APPROVED"
    assert candidate.status == "PUBLISHED"
    assert candidate.approved_by == "independent-reviewer"
    assert candidate.approved_at is not None
    assert candidate.published_at is not None
    assert len(session.executed_statements) == 1
    compiled = str(session.executed_statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "context_product_version" in compiled
    assert "SUPPORTED" in compiled
    assert "SUPERSEDED" not in compiled
    assert "PUBLISHED" in compiled
    assert "superseded_by_version_id" in compiled
    assert "support_window_ends_at" in compiled
    assert candidate.id.hex in compiled
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "context.product_published.v1"
        for value in session.added
    )
    assert any(isinstance(value, AuditEvent) for value in session.added)
    assert session.committed is True


async def test_approval_computes_a_fixed_support_window_from_the_prior_version() -> None:
    """When the version being replaced was itself submitted with a fixed
    `support_window_days`, the new SUPPORTED deadline is computed from it
    (`now + support_window_days`), not left indefinite."""
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    review = _review(organization_id=organization_id, version_id=candidate.id)
    session = _GovernanceDecisionSession(get_results=[review, candidate, 30])

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _reviewer(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    compiled = str(session.executed_statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "support_window_ends_at" in compiled
    set_clause = compiled.split("SET", 1)[1].split("WHERE", 1)[0]
    ends_at_assignment = [
        part for part in set_clause.split(",") if "support_window_ends_at" in part
    ][0]
    assert "NULL" not in ends_at_assignment


async def test_rejection_does_not_modify_other_versions() -> None:
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    review = _review(organization_id=organization_id, version_id=candidate.id)
    session = _GovernanceDecisionSession(get_results=[review, candidate])

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="REJECT", reason="Product evidence is incomplete."),
        _reviewer(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert result.status == "REJECTED"
    assert candidate.status == "REJECTED"
    assert candidate.approved_by is None
    assert session.executed_statements == []
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "context.product_rejected.v1"
        for value in session.added
    )


async def test_approved_deprecation_retires_published_context_product() -> None:
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    candidate.status = "PUBLISHED"
    review = _review(organization_id=organization_id, version_id=candidate.id)
    review.requested_action = "DEPRECATE"
    session = _GovernanceDecisionSession(get_results=[review, candidate])

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _reviewer(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert result.status == "APPROVED"
    assert candidate.status == "DEPRECATED"
    assert session.executed_statements == []
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "context.product_deprecated.v1"
        for value in session.added
    )


async def test_a_supported_version_can_be_explicitly_retired_early() -> None:
    """AT-7(a): "supported ... or until explicit retirement" -- a steward is
    not forced to wait out a SUPPORTED version's support window; the same
    DEPRECATE maker-checker flow that retires a PUBLISHED version also
    accepts one that is currently SUPPORTED."""
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    candidate.status = "SUPPORTED"
    candidate.support_window_ends_at = datetime(2030, 1, 1, tzinfo=UTC)
    review = _review(organization_id=organization_id, version_id=candidate.id)
    review.requested_action = "DEPRECATE"
    session = _GovernanceDecisionSession(get_results=[review, candidate])

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _reviewer(organization_id=organization_id),
        session,  # type: ignore[arg-type]
    )

    assert result.status == "APPROVED"
    assert candidate.status == "DEPRECATED"


# --- CX-2: versioned, owned, approved ---------------------------------------


def test_ownership_requires_an_explicit_individual_or_group_owner_type() -> None:
    """Ownership must be explicit and typed, mirroring the INDIVIDUAL/GROUP
    shape already used for tables and glossary terms (`OwnershipRuleCreate`) --
    not a bare, untyped owner string.
    """
    with pytest.raises(ValidationError):
        _definition(owner_type="TEAM")
    with pytest.raises(ValidationError):
        ContextProductDefinition.model_validate(
            {
                "name": "Revenue analysis context",
                "description": "Approved definitions and tools for revenue analysis.",
                "purpose": "Give analyst agents bounded, governed revenue context.",
                "owner_principal": "data-product-owner",
                "table_ids": [uuid4()],
                "allowed_consumer_roles": ["Analyst"],
            }
        )

    individual = _definition(owner_type="INDIVIDUAL")
    group = _definition(owner_type="GROUP")
    assert individual.owner_type == "INDIVIDUAL"
    assert group.owner_type == "GROUP"


class _CreateVersionSession:
    """Fake session for `create_context_product_version`: `.get()` and
    `.scalar()` each pop preset results in call order, `.scalars()` returns
    the preset governed-reference rows, and every UPDATE/DELETE statement is
    recorded so a test can prove the prior version was never touched.
    """

    def __init__(
        self,
        *,
        get_results: list[object],
        scalar_results: list[object],
        scalars_results: list[list[object]],
    ) -> None:
        self._get_queue = list(get_results)
        self._scalar_queue = list(scalar_results)
        self._scalars_queue = list(scalars_results)
        self.added: list[object] = []
        self.executed_statements: list[object] = []
        self.committed = False

    async def get(self, _model: type[object], _identity: object) -> object:
        return self._get_queue.pop(0)

    async def scalar(self, _statement: object) -> object:
        return self._scalar_queue.pop(0)

    async def scalars(self, _statement: object) -> object:
        values = self._scalars_queue.pop(0)

        class _Scalars:
            def all(self_inner) -> list[object]:
                return values

        return _Scalars()

    async def execute(self, statement: object) -> None:
        self.executed_statements.append(statement)

    def add(self, value: object) -> None:
        # Mirror the column defaults `models.ContextProductVersion` declares
        # (`id`, `status`, `created_at`, `updated_at`) -- a real flush against
        # a live engine applies them; this fake session never flushes, so it
        # applies them itself rather than asserting against attributes a real
        # database would have already filled in.
        if isinstance(value, ContextProductVersion):
            if value.id is None:
                value.id = uuid4()
            if value.status is None:
                value.status = "DRAFT"
            if value.created_at is None:
                value.created_at = datetime.now(UTC)
            if value.updated_at is None:
                value.updated_at = datetime.now(UTC)
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def _active_product(*, organization_id: UUID, project_id: UUID) -> ContextProduct:
    return ContextProduct(
        id=uuid4(),
        organization_id=organization_id,
        project_id=project_id,
        product_key="revenue_context",
        lifecycle_status="ACTIVE",
        created_by="product-author",
    )


def _project(*, organization_id: UUID) -> Project:
    return Project(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        name="Revenue",
        slug="revenue",
        status="ACTIVE",
    )


async def test_editing_a_product_creates_a_new_version_and_leaves_the_prior_one_untouched() -> (
    None
):
    """CX-2 versioning: a change to a context product creates a new,
    numbered version pinned to `based_on_version_id`; it never mutates the
    version it was drafted from -- that version stays queryable exactly as
    it was (superseding it is the checker's job, at approval, per
    `test_approval_publishes_candidate_and_supersedes_prior_version`)."""
    organization_id = uuid4()
    project = _project(organization_id=organization_id)
    product = _active_product(organization_id=organization_id, project_id=project.id)
    published = _candidate(organization_id=organization_id, product_id=product.id)
    published.status = "PUBLISHED"
    published.version = 1
    table_id = published.table_ids[0]
    context = SecurityContext(
        principal_id="product-author",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )
    session = _CreateVersionSession(
        get_results=[product, project, published],
        scalar_results=[1],
        scalars_results=[[UUID(table_id)]],
    )
    body = ContextProductVersionCreate(
        name=published.name,
        description="Refreshed definitions and tools for revenue analysis.",
        purpose=published.purpose,
        owner_type="GROUP",
        owner_principal="data-product-owner",
        table_ids=[UUID(table_id)],
        allowed_consumer_roles=["Analyst"],
        based_on_version_id=published.id,
    )

    new_version = await create_context_product_version(
        product.id, body, context, session  # type: ignore[arg-type]
    )

    assert new_version.version == 2
    assert new_version.status == "DRAFT"
    assert new_version.based_on_version_id == published.id
    assert new_version.description == body.description
    # The prior published version is a distinct object and was never queried
    # for mutation -- only its role bindings for the *new* version were
    # touched (a delete scoped to the new version's own id).
    assert published.status == "PUBLISHED"
    assert published.description != new_version.description
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "context.product_draft_created.v1"
        for value in session.added
    )
    assert session.committed is True


async def test_only_a_draft_version_can_be_edited() -> None:
    """CX-2 versioning: once a version leaves DRAFT (submitted, published,
    or otherwise decided) it is immutable -- further changes require a new
    version, not an in-place edit."""
    organization_id = uuid4()
    for immutable_status in ("REVIEW_REQUIRED", "PUBLISHED", "REJECTED", "DEPRECATED"):
        candidate = _candidate(organization_id=organization_id, product_id=uuid4())
        candidate.status = immutable_status
        product = ContextProduct(
            id=candidate.product_id,
            organization_id=organization_id,
            project_id=uuid4(),
            product_key="revenue_context",
            lifecycle_status="ACTIVE",
            created_by="product-author",
        )
        session = _CreateVersionSession(
            get_results=[candidate, product],
            scalar_results=[],
            scalars_results=[],
        )
        context = SecurityContext(
            principal_id="product-author",
            principal_type="USER",
            organization_id=organization_id,
            roles=frozenset({"DataSteward"}),
        )

        with pytest.raises(HTTPException) as denied:
            await update_context_product_version(
                candidate.id,
                _definition(),  # type: ignore[arg-type]
                context,
                session,  # type: ignore[arg-type]
            )

        assert denied.value.status_code == 409
        assert "draft" in denied.value.detail


async def test_context_product_self_approval_is_rejected() -> None:
    """CX-2 maker-checker: the principal who proposed a context product
    version can never be the principal who approves or rejects it -- the
    shared `decide_governance_review` maker-checker guard (INV-8) covers
    `CONTEXT_PRODUCT_VERSION` exactly like every other governed object type.
    """
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    review = _review(organization_id=organization_id, version_id=candidate.id)
    session = _GovernanceDecisionSession(get_results=[review])
    maker_context = SecurityContext(
        principal_id=review.requested_by,  # identical to the maker
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )

    with pytest.raises(HTTPException) as denied:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            maker_context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert "maker-checker" in denied.value.detail
    # The candidate was never touched -- the guard fires before the review's
    # object_type is even inspected.
    assert candidate.status == "REVIEW_REQUIRED"
    assert session.committed is False


async def test_mcp_resource_query_never_matches_an_unpublished_version() -> None:
    """CX-2 x CX-3: governed-tool scope resolution only ever matches a
    PUBLISHED or SUPPORTED version (AT-7(a) -- a version-pinned tool scope
    keeps working through a version's support window, not just while it is
    the single current PUBLISHED one) -- the SQL predicate itself excludes
    DRAFT, REVIEW_REQUIRED, REJECTED, SUPERSEDED, and DEPRECATED versions, so
    a consumer can never race a pending approval or resolve tool scope
    against a fully retired version.
    """
    from aida.mcp_server import _resolve_context_product_scope

    organization_id = uuid4()
    context = SecurityContext(
        principal_id="analyst-agent",
        principal_type="SERVICE",
        organization_id=organization_id,
        roles=frozenset({"Analyst"}),
    )

    class _CapturingSession:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)

            class _Result:
                def first(self_inner) -> object:
                    return None

            return _Result()

    session = _CapturingSession()
    uri = "atlas://context-products/revenue_context/versions/1"

    result = await _resolve_context_product_scope(uri, session, context)  # type: ignore[arg-type]

    assert result is None
    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "PUBLISHED" in compiled
    assert "SUPPORTED" in compiled
    assert "context_product_version.status" in compiled


# ---------------------------------------------------------------------------
# AT-7(b): consumer-binding registry (staged rollout)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def binding_session() -> AsyncIterator[AsyncSession]:
    """Real in-memory sqlite -- the binding registry does real INSERT/SELECT
    against a unique constraint and two foreign keys, which a hand-rolled
    fake session cannot faithfully exercise the way the rest of this file's
    tests do for simpler single-statement policy functions."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seeded_product(
    session: AsyncSession, *, published_version: int = 1
) -> tuple[ContextProduct, ContextProductVersion]:
    organization_id = uuid4()
    org = Organization(id=organization_id, name="Bank", slug=f"bank-{organization_id.hex[:8]}")
    project = Project(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        name="Analytics",
        slug="analytics",
    )
    product = ContextProduct(
        id=uuid4(),
        organization_id=organization_id,
        project_id=project.id,
        product_key="revenue_context",
        lifecycle_status="ACTIVE",
        created_by="product-author",
    )
    version = _candidate(organization_id=organization_id, product_id=product.id)
    version.status = "PUBLISHED"
    version.version = published_version
    session.add_all([org, project, product, version])
    await session.flush()
    return product, version


def _author_context(*, organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )


async def test_set_binding_creates_then_moves_it(binding_session: AsyncSession) -> None:
    """A second `PUT` for the same consumer moves the existing binding rather
    than creating a duplicate row -- the unique constraint on
    (product_id, consumer_principal_id) would reject a real duplicate insert,
    so a passing second call already proves the upsert path, not just the
    returned response."""
    product, v1 = await _seeded_product(binding_session)
    v2 = _candidate(organization_id=product.organization_id, product_id=product.id)
    v2.version = 2
    v2.status = "DRAFT"
    binding_session.add(v2)
    await binding_session.flush()
    context = _author_context(organization_id=product.organization_id)

    created = await set_context_product_consumer_binding(
        product.id,
        "analyst-agent",
        ContextProductConsumerBindingCreate(bound_version_id=v1.id),
        context=context,
        session=binding_session,
    )
    assert created.bound_version_number == 1

    moved = await set_context_product_consumer_binding(
        product.id,
        "analyst-agent",
        ContextProductConsumerBindingCreate(bound_version_id=v2.id),
        context=context,
        session=binding_session,
    )
    assert moved.id == created.id
    assert moved.bound_version_number == 2

    count = await binding_session.scalar(
        select(func.count()).select_from(ContextProductConsumerBinding)
    )
    assert count == 1


async def test_set_binding_rejects_a_version_from_a_different_product(
    binding_session: AsyncSession,
) -> None:
    product, _v1 = await _seeded_product(binding_session)
    other_product, other_version = await _seeded_product(binding_session)
    context = _author_context(organization_id=product.organization_id)

    with pytest.raises(HTTPException) as excinfo:
        await set_context_product_consumer_binding(
            product.id,
            "analyst-agent",
            ContextProductConsumerBindingCreate(bound_version_id=other_version.id),
            context=context,
            session=binding_session,
        )
    assert excinfo.value.status_code == 422
    assert other_product.id != product.id


async def test_list_bindings_reports_bound_version_numbers(
    binding_session: AsyncSession,
) -> None:
    product, v1 = await _seeded_product(binding_session)
    context = _author_context(organization_id=product.organization_id)
    for consumer in ("analyst-agent", "reporting-agent"):
        await set_context_product_consumer_binding(
            product.id,
            consumer,
            ContextProductConsumerBindingCreate(bound_version_id=v1.id),
            context=context,
            session=binding_session,
        )

    page = await list_context_product_consumer_bindings(
        product.id, limit=100, offset=0, context=context, session=binding_session
    )
    assert page.total == 2
    assert {item.consumer_principal_id for item in page.items} == {
        "analyst-agent",
        "reporting-agent",
    }
    assert all(item.bound_version_number == 1 for item in page.items)


async def test_delete_binding_removes_it_and_404s_when_absent(
    binding_session: AsyncSession,
) -> None:
    product, v1 = await _seeded_product(binding_session)
    context = _author_context(organization_id=product.organization_id)
    await set_context_product_consumer_binding(
        product.id,
        "analyst-agent",
        ContextProductConsumerBindingCreate(bound_version_id=v1.id),
        context=context,
        session=binding_session,
    )

    await delete_context_product_consumer_binding(
        product.id, "analyst-agent", context=context, session=binding_session
    )

    remaining = await binding_session.scalar(
        select(func.count()).select_from(ContextProductConsumerBinding)
    )
    assert remaining == 0

    with pytest.raises(HTTPException) as excinfo:
        await delete_context_product_consumer_binding(
            product.id, "analyst-agent", context=context, session=binding_session
        )
    assert excinfo.value.status_code == 404


async def test_resolve_bound_version_prefers_a_servable_binding_over_a_newer_published_version(
    binding_session: AsyncSession,
) -> None:
    """The staged-rollout point: a bound consumer stays on their pinned
    version even after a newer one publishes, as long as theirs can still be
    served (SUPPORTED within its window counts, same as PUBLISHED)."""
    product, v1 = await _seeded_product(binding_session)
    v1.status = "SUPPORTED"
    v1.support_window_ends_at = datetime.now(UTC) + timedelta(days=10)
    v2 = _candidate(organization_id=product.organization_id, product_id=product.id)
    v2.version = 2
    v2.status = "PUBLISHED"
    binding_session.add(v2)
    await binding_session.flush()
    context = _author_context(organization_id=product.organization_id)
    await set_context_product_consumer_binding(
        product.id,
        "analyst-agent",
        ContextProductConsumerBindingCreate(bound_version_id=v1.id),
        context=context,
        session=binding_session,
    )

    resolved = await resolve_bound_version(
        binding_session, product_id=product.id, principal_id="analyst-agent"
    )

    assert resolved is not None
    assert resolved.id == v1.id


async def test_resolve_bound_version_falls_back_once_the_binding_is_fully_retired(
    binding_session: AsyncSession,
) -> None:
    product, v1 = await _seeded_product(binding_session)
    v1.status = "SUPERSEDED"
    v2 = _candidate(organization_id=product.organization_id, product_id=product.id)
    v2.version = 2
    v2.status = "PUBLISHED"
    binding_session.add(v2)
    await binding_session.flush()
    context = _author_context(organization_id=product.organization_id)
    await set_context_product_consumer_binding(
        product.id,
        "analyst-agent",
        ContextProductConsumerBindingCreate(bound_version_id=v1.id),
        context=context,
        session=binding_session,
    )

    resolved = await resolve_bound_version(
        binding_session, product_id=product.id, principal_id="analyst-agent"
    )

    assert resolved is not None
    assert resolved.id == v2.id


async def test_resolve_bound_version_returns_the_current_published_version_when_unbound(
    binding_session: AsyncSession,
) -> None:
    product, v1 = await _seeded_product(binding_session)

    resolved = await resolve_bound_version(
        binding_session, product_id=product.id, principal_id="never-bound-agent"
    )

    assert resolved is not None
    assert resolved.id == v1.id
