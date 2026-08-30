"""Contract and lifecycle coverage for governed Context Products."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aida.context_product_api import (
    _can_read_context_product_version,
    context_product_fingerprint,
    create_context_product_version,
    update_context_product_version,
)
from aida.context_product_policy import evaluate_context_product_quality
from aida.main import app
from aida.mcp_server import _context_product_role_eligible, _read_context_product_resource
from aida.models import (
    AuditEvent,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    GovernanceReview,
    OutboxEvent,
    Project,
)
from aida.schemas import (
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
    """CX-2 x CX-3: only an approved (`PUBLISHED`) version is exposed through
    the MCP-facing read path -- the SQL predicate itself excludes DRAFT,
    REVIEW_REQUIRED, REJECTED, SUPERSEDED, and DEPRECATED versions, so a
    consumer can never race a pending approval or read a retired version.
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
    assert "context_product_version.status" in compiled
