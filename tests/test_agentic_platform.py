from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

import aida.product_marketplace_api as product_marketplace_api
from aida.ai_registry import compute_ai_trust_score, score_assessment_controls
from aida.config import Settings
from aida.context_compiler import (
    ResolvedTableReference,
    compilation_drift_paths,
    compile_context_product,
    validate_compiled_artifact,
)
from aida.context_product_policy import evaluate_context_product_purpose
from aida.entitlements import apply_entitlement
from aida.main import app
from aida.mcp_budget import consume_mcp_budget
from aida.mcp_server import _entity_match_score
from aida.models import (
    AiAssessment,
    AiAssetVersion,
    ContextProduct,
    ContextProductVersion,
    DataProduct,
    DataProductAccessRequest,
    DataProductVersion,
    GovernanceReview,
    OutboxEvent,
)
from aida.platform_schemas import (
    AiAssetDefinition,
    DataProductCreate,
    MarketplaceAccessRequestCreate,
)
from aida.product_marketplace_api import (
    _build_portfolio_trend_points,
    approve_access_request,
    evaluate_contract_compatibility,
    request_marketplace_access,
)
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review


def test_contract_compatibility_reports_removed_and_changed_fields() -> None:
    previous = [
        {"name": "customer_id", "data_type": "STRING", "required": True},
        {"name": "balance", "data_type": "DECIMAL", "required": False},
    ]
    candidate = [
        {"name": "balance", "data_type": "STRING", "required": True},
    ]

    findings = evaluate_contract_compatibility(previous, candidate, "FULL")

    assert {finding["code"] for finding in findings} == {
        "FIELD_REMOVED",
        "FIELD_TYPE_CHANGED",
        "FIELD_BECAME_REQUIRED",
    }


def test_data_product_definition_requires_unique_output_ports() -> None:
    common = {
        "product_key": "customer_360",
        "name": "Customer 360 product",
        "description": "Published customer profile data for approved analytical use.",
        "domain_name": "Customer",
        "owner_principal": "customer-data-owner",
        "usage_terms": "Use only for approved customer service and analytics purposes.",
        "classification": "CONFIDENTIAL",
    }
    with pytest.raises(ValidationError, match="output port"):
        DataProductCreate.model_validate(
            {
                **common,
                "ports": [
                    {
                        "port_key": "source",
                        "direction": "INPUT",
                        "name": "Source",
                        "description": "Input source",
                        "asset_type": "TABLE",
                        "asset_id": str(uuid4()),
                    }
                ],
            }
        )


def test_marketplace_access_approval_sets_bounded_expiry() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    request = DataProductAccessRequest(
        id=uuid4(),
        organization_id=uuid4(),
        data_product_version_id=uuid4(),
        requested_by="analyst",
        purpose="Approved customer analytics investigation.",
        duration_days=30,
        status="PENDING",
        governance_review_id=uuid4(),
    )

    approve_access_request(
        request,
        reviewer="independent-reviewer",
        reason="Purpose and duration are proportionate.",
        approved=True,
        now=now,
    )

    assert request.status == "APPROVED"
    assert request.decided_by == "independent-reviewer"
    assert request.expires_at is not None
    assert (request.expires_at - now).days == 30


class _MarketplaceAccessSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.timeline: list[str] = []
        self.committed = False

    async def scalar(self, _statement: object) -> object:
        self.timeline.append("scalar")
        return None

    def add(self, value: object) -> None:
        self.added.append(value)
        self.timeline.append(type(value).__name__)

    async def flush(self) -> None:
        self.timeline.append("flush")

    async def commit(self) -> None:
        self.committed = True
        self.timeline.append("commit")

    async def rollback(self) -> None:
        self.timeline.append("rollback")


class _GovernanceDecisionSession:
    def __init__(self, *, get_results: list[object]) -> None:
        self._get_queue = list(get_results)
        self.added: list[object] = []
        self.committed = False

    async def get(self, _model: type[object], _identity: object) -> object:
        return self._get_queue.pop(0)

    async def scalar(self, _statement: object) -> object:
        return self._get_queue.pop(0)

    async def execute(self, _statement: object) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


async def test_request_marketplace_access_flushes_review_before_access_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    product = DataProduct(
        id=uuid4(),
        organization_id=organization_id,
        project_id=uuid4(),
        product_key="customer_portfolio",
        lifecycle_status="ACTIVE",
        created_by="owner",
    )
    version = DataProductVersion(
        id=uuid4(),
        organization_id=organization_id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Customer portfolio",
        description="Published customer portfolio data product.",
        domain_name="Customer",
        owner_principal="owner",
        usage_terms="Approved use only.",
        classification="CONFIDENTIAL",
        certification_status="CERTIFIED",
        quality_score=92,
        lineage_coverage=80,
        discoverable_roles=["Analyst"],
        consumer_roles=["DataConsumer"],
        fingerprint="portfolio-fingerprint",
        created_by="owner",
    )

    async def _fake_version_scope(_session: object, _version_id: object, _context: object) -> tuple[
        DataProduct, DataProductVersion
    ]:
        return product, version

    monkeypatch.setattr(product_marketplace_api, "_version_scope", _fake_version_scope)
    session = _MarketplaceAccessSession()
    context = SecurityContext(
        principal_id="marketplace-analyst",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Analyst"}),
    )

    result = await request_marketplace_access(
        version.id,
        MarketplaceAccessRequestCreate(
            purpose="Approved customer analytics for verification.",
            duration_days=30,
        ),
        context,
        session,  # type: ignore[arg-type]
    )

    assert result.data_product_version_id == version.id
    assert session.committed is True
    assert session.timeline[:4] == [
        "scalar",
        "GovernanceReview",
        "flush",
        "DataProductAccessRequest",
    ]


async def test_governance_access_approval_outbox_serializes_expiry() -> None:
    organization_id = uuid4()
    review = GovernanceReview(
        id=uuid4(),
        organization_id=organization_id,
        object_type="DATA_PRODUCT_ACCESS_REQUEST",
        object_id=str(uuid4()),
        requested_action="GRANT_ACCESS",
        status="PENDING",
        requested_by="marketplace-analyst",
    )
    access_request = DataProductAccessRequest(
        id=uuid4(),
        organization_id=organization_id,
        data_product_version_id=uuid4(),
        requested_by="marketplace-analyst",
        purpose="Approved customer analytics for verification.",
        duration_days=30,
        status="PENDING",
        governance_review_id=review.id,
    )
    session = _GovernanceDecisionSession(get_results=[review, access_request])
    context = SecurityContext(
        principal_id="reviewer",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )

    result = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE", reason="Time-bounded access approved."),
        context,
        session,  # type: ignore[arg-type]
    )

    outbox = next(item for item in session.added if isinstance(item, OutboxEvent))

    assert result.status == "APPROVED"
    assert access_request.status == "APPROVED"
    assert access_request.expires_at is not None
    assert session.committed is True
    assert outbox.payload["expires_at"] == access_request.expires_at.isoformat()


def test_portfolio_trend_points_are_bounded_and_deterministic() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    points = _build_portfolio_trend_points(
        now=now,
        window_days=14,
        bucket_days=7,
        access_request_times=[now - timedelta(days=13), now - timedelta(days=2)],
        context_read_times=[now - timedelta(days=8)],
        mcp_operation_times=[now - timedelta(days=8), now - timedelta(days=1)],
        mcp_tool_call_times=[now - timedelta(days=1)],
        agent_runs=[
            (now - timedelta(days=10), "GOVERNED_TOOL"),
            (now - timedelta(days=1), "MODEL_GATEWAY"),
        ],
        query_execution_times=[now - timedelta(days=10), now - timedelta(hours=1)],
    )

    assert len(points) == 2
    assert points[0].bucket_start == now - timedelta(days=14)
    assert points[0].bucket_end == now - timedelta(days=7)
    assert points[0].access_requests == 1
    assert points[0].context_reads == 1
    assert points[0].mcp_operations == 1
    assert points[0].mcp_tool_calls == 0
    assert points[0].agent_runs == 1
    assert points[0].governed_tool_runs == 1
    assert points[0].model_gateway_runs == 0
    assert points[0].query_executions == 1
    assert points[1].access_requests == 1
    assert points[1].context_reads == 0
    assert points[1].mcp_operations == 1
    assert points[1].mcp_tool_calls == 1
    assert points[1].agent_runs == 1
    assert points[1].governed_tool_runs == 0
    assert points[1].model_gateway_runs == 1
    assert points[1].query_executions == 1


def _compiler_fixture() -> tuple[
    ContextProduct, ContextProductVersion, list[ResolvedTableReference]
]:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    product = ContextProduct(
        id=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        product_key="risk_context",
        lifecycle_status="ACTIVE",
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Risk context",
        description="Approved risk metadata context.",
        purpose="Support bounded portfolio risk analysis.",
        owner_principal="risk-owner",
        table_ids=[str(uuid4())],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=[],
        allowed_consumer_roles=["Analyst"],
        lineage_depth=2,
        quality_requirements={"minimum_score": 80},
        policy_summary={"source_values": "GATEWAY_ONLY"},
        fingerprint="a" * 64,
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    tables = [
        ResolvedTableReference(table_id=version.table_ids[0], qualified_name="DB.RISK.EXPOSURE")
    ]
    return product, version, tables


def test_context_compiler_is_repeatable_and_reports_structural_drift() -> None:
    product, version, tables = _compiler_fixture()

    first = compile_context_product(product, version, "ODCS", tables)
    second = compile_context_product(product, version, "ODCS", tables)

    assert first.artifact_hash == second.artifact_hash
    assert first.content == second.content
    deployed = first.content.replace("portfolio risk", "credit risk")
    assert compilation_drift_paths(first.content, deployed)


def test_ai_trust_score_is_explainable_and_blocked_by_incident() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    version = AiAssetVersion(
        id=uuid4(),
        organization_id=uuid4(),
        asset_id=uuid4(),
        version=1,
        status="APPROVED",
        name="Credit decision agent",
        description="Agent supporting reviewed credit decisions.",
        intended_use="Provide bounded recommendations to a human underwriter.",
        owner_principal="model-risk-owner",
        provider_type="ON_PREM",
        risk_tier="HIGH",
        documentation_url="https://docs.example/credit-agent",
        context_product_version_ids=[],
        model_route_ids=[],
        policy_control_ids=["human-oversight", "fairness", "monitoring", "privacy"],
        evaluation_evidence={"pass_rate": 0.95, "evidence_id": "eval-1"},
        runtime_evidence={
            "success_rate": 0.99,
            "open_critical_incidents": 1,
            "evidence_id": "runtime-1",
        },
        fingerprint="b" * 64,
        created_by="maker",
        approved_by="checker",
        approved_at=now,
        created_at=now,
        updated_at=now,
    )
    assessment = AiAssessment(
        id=uuid4(),
        organization_id=version.organization_id,
        ai_asset_version_id=version.id,
        framework="NIST_AI_RMF",
        framework_version="1.0",
        status="PASS",
        score=90,
        control_results=[],
        findings=[],
        assessed_by="model-risk",
        created_at=now,
        updated_at=now,
    )

    result = compute_ai_trust_score(version, assessment, computed_at=now)

    assert sum(factor.maximum for factor in result.factors) == 100
    assert 0 <= result.score <= 100
    assert result.grade == "BLOCKED"
    assert "OPEN_CRITICAL_RUNTIME_INCIDENT" in result.blockers
    assert {factor.factor for factor in result.factors} == {
        "DOCUMENTATION",
        "ACCOUNTABILITY",
        "GOVERNANCE_LIFECYCLE",
        "POLICY_COVERAGE",
        "EVALUATION_POSTURE",
        "RUNTIME_POSTURE",
        "INDEPENDENT_ASSESSMENT",
    }


def test_assessment_scoring_is_weighted_and_deterministic() -> None:
    score, status, findings = score_assessment_controls(
        [
            {"control_key": "a", "title": "A", "weight": 3, "outcome": "PASS"},
            {
                "control_key": "b",
                "title": "B",
                "weight": 1,
                "outcome": "FAIL",
                "finding": "Missing evidence",
            },
        ]
    )

    assert score == 75
    assert status == "NEEDS_REMEDIATION"
    assert findings == [{"control_key": "b", "title": "B", "finding": "Missing evidence"}]


def test_ai_registry_rejects_raw_prompt_evidence() -> None:
    with pytest.raises(ValidationError, match="cannot contain raw prompt"):
        AiAssetDefinition.model_validate(
            {
                "name": "Risk agent",
                "description": "A documented and governed risk analysis agent.",
                "intended_use": "Support bounded analysis by authorized risk analysts.",
                "owner_principal": "risk-owner",
                "provider_type": "ON_PREM",
                "risk_tier": "HIGH",
                "evaluation_evidence": {"prompt": "secret question"},
            }
        )


def test_fuzzy_entity_score_prefers_exact_and_qualified_matches() -> None:
    exact = _entity_match_score("customers", "customers")
    qualified = _entity_match_score("customers", "warehouse.public.customers")
    unrelated = _entity_match_score("customers", "monthly_revenue")

    assert exact == 1.0
    assert exact > qualified > unrelated


async def test_disabled_mcp_budget_has_no_redis_dependency() -> None:
    context = SecurityContext(
        principal_id="agent-workload",
        principal_type="WORKLOAD",
        organization_id=uuid4(),
        roles=frozenset({"Analyst"}),
    )
    decision = await consume_mcp_budget(
        Settings(mcp_budget_enabled=False, _env_file=None), context, "TOOL_DAY"
    )

    assert decision.allowed is True
    assert decision.used == 0


def test_context_product_purpose_abac_fails_closed_when_restricted() -> None:
    policy = {"allowed_purposes": ["Fraud Investigation", "Regulatory Reporting"]}

    assert evaluate_context_product_purpose("fraud investigation", policy).allowed is True
    assert evaluate_context_product_purpose(None, policy).reason == "BUSINESS_PURPOSE_REQUIRED"
    assert (
        evaluate_context_product_purpose("marketing", policy).reason
        == "BUSINESS_PURPOSE_NOT_ALLOWED"
    )


def test_yaml_compilation_is_idiomatic_and_structurally_valid() -> None:
    product, version, tables = _compiler_fixture()
    compiled = compile_context_product(product, version, "YAML", tables)

    assert compiled.content.startswith("apiVersion:")
    assert validate_compiled_artifact("YAML", compiled.content).valid is True
    invalid = validate_compiled_artifact("ODCS", '{"kind":"Wrong"}')
    assert invalid.valid is False
    assert "ODCS_KIND_INVALID" in invalid.findings


async def test_outbox_entitlement_stays_pending_without_external_side_effect() -> None:
    access_request = DataProductAccessRequest(
        id=uuid4(),
        organization_id=uuid4(),
        data_product_version_id=uuid4(),
        requested_by="agent-workload",
        purpose="Approved fraud investigation",
        duration_days=30,
        governance_review_id=uuid4(),
    )

    result = await apply_entitlement(
        Settings(entitlement_provider="outbox", _env_file=None), access_request, "PROVISION"
    )

    assert result.status == "PENDING"
    assert result.provider == "outbox"


def test_agentic_platform_routes_are_published() -> None:
    paths = app.openapi()["paths"]

    assert "/v1/projects/{project_id}/data-products" in paths
    assert "/v1/marketplace/products" in paths
    assert "/v1/organizations/{organization_id}/portfolio-analytics/summary" in paths
    assert "/v1/organizations/{organization_id}/portfolio-analytics/trends" in paths
    assert "/v1/data-products/{product_id}/contracts" in paths
    assert "/v1/context-product-versions/{version_id}/compile" in paths
    assert "/v1/organizations/{organization_id}/ai-assets" in paths
    assert "/v1/ai-asset-versions/{version_id}/trust" in paths
    assert "/v1/context-product-versions/{version_id}/compile/download" in paths
    assert "/v1/context-compiler/validate" in paths
    assert "/v1/ai-assessment-templates" in paths
    assert "/v1/ai-asset-versions/{version_id}/trust-history" in paths
    assert "/v1/marketplace/access-requests/{request_id}/entitlement" in paths
