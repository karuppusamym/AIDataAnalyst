"""Contract and lifecycle coverage for governed Context Products."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aida.context_product_api import (
    _can_read_context_product_version,
    context_product_fingerprint,
)
from aida.main import app
from aida.mcp_server import _context_product_role_eligible, _read_context_product_resource
from aida.models import (
    AuditEvent,
    ContextProduct,
    ContextProductVersion,
    GovernanceReview,
    OutboxEvent,
)
from aida.schemas import ContextProductDefinition, GovernanceDecisionRequest
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
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _statement: object) -> object:
        row = self.row

        class _Result:
            def first(self_inner) -> object:
                return row

        return _Result()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def _definition(**changes: object) -> ContextProductDefinition:
    values: dict[str, object] = {
        "name": "Revenue analysis context",
        "description": "Approved definitions and tools for revenue analysis.",
        "purpose": "Give analyst agents bounded, governed revenue context.",
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


async def test_approval_publishes_candidate_and_supersedes_prior_version() -> None:
    organization_id = uuid4()
    candidate = _candidate(organization_id=organization_id, product_id=uuid4())
    review = _review(organization_id=organization_id, version_id=candidate.id)
    session = _GovernanceDecisionSession(get_results=[review, candidate])

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
    assert "SUPERSEDED" in compiled
    assert "PUBLISHED" in compiled
    assert candidate.id.hex in compiled
    assert any(
        isinstance(value, OutboxEvent) and value.event_type == "context.product_published.v1"
        for value in session.added
    )
    assert any(isinstance(value, AuditEvent) for value in session.added)
    assert session.committed is True


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
