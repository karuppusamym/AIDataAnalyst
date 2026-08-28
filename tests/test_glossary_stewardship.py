from uuid import uuid4

import pytest
from pydantic import ValidationError

from aida.main import app
from aida.schemas import (
    BulkStewardshipOperationCreate,
    GlossaryCategoryCreate,
    GlossaryConflictResolution,
    GlossaryLinkProposalGenerate,
    GlossaryTermDeprecationRequest,
    OwnershipRuleCreate,
)


def test_glossary_stewardship_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]

    # GL-1: term deprecation, maker-checker via the shared governance review queue
    assert "/v1/glossary-terms/{term_id}/deprecate" in paths
    assert "/v1/organizations/{organization_id}/stewardship/bulk-operations" in paths

    # GL-2: bulk, rule-based ownership assignment
    assert "/v1/organizations/{organization_id}/ownership-rules" in paths
    assert "/v1/ownership-rules/{rule_id}/apply" in paths
    assert "/v1/organizations/{organization_id}/ownership-assignments" in paths

    # GL-3: conflict detection and resolution, losing position retained
    assert "/v1/organizations/{organization_id}/glossary-conflicts" in paths
    assert "/v1/organizations/{organization_id}/glossary-conflicts/detect" in paths
    assert "/v1/glossary-conflicts/{conflict_id}/resolution" in paths

    # GL-4: coverage scoring, per domain/LOB, trended over time
    assert "/v1/organizations/{organization_id}/stewardship/coverage" in paths
    assert "/v1/organizations/{organization_id}/stewardship/coverage/snapshots" in paths

    # GL-8 (breadth already built alongside the required scope)
    assert "/v1/organizations/{organization_id}/glossary-link-proposals/generate" in paths
    assert "/v1/glossary-link-proposals/{proposal_id}/submit" in paths
    assert "/v1/organizations/{organization_id}/glossary-categories" in paths


def test_glossary_term_deprecation_requires_a_real_reason() -> None:
    with pytest.raises(ValidationError):
        GlossaryTermDeprecationRequest(reason="too short")

    request = GlossaryTermDeprecationRequest(reason="Superseded by the FY26 revenue definition.")
    assert request.reason.startswith("Superseded")


def test_ownership_rule_supports_domain_schema_tag_and_pattern_matching() -> None:
    for match_field in ("TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME", "DOMAIN_KEY", "TAG"):
        rule = OwnershipRuleCreate(
            rule_key="finance-staging-owner",
            display_name="Finance staging owner",
            match_field=match_field,
            match_pattern="stg_finance_*",
            owner_type="INDIVIDUAL",
            owner_principal="finance-data-steward",
        )
        assert rule.match_field == match_field

    with pytest.raises(ValidationError):
        OwnershipRuleCreate(
            rule_key="bad",
            display_name="Bad rule",
            match_field="LOB",
            match_pattern="*",
            owner_type="INDIVIDUAL",
            owner_principal="someone",
        )


def test_bulk_ownership_assignment_requires_owner_fields() -> None:
    with pytest.raises(ValidationError, match="owner_type and owner_principal"):
        BulkStewardshipOperationCreate(
            operation_type="ASSIGN_OWNERSHIP",
            subject_type="TABLE",
            subject_ids=[uuid4()],
        )

    operation = BulkStewardshipOperationCreate(
        operation_type="ASSIGN_OWNERSHIP",
        subject_type="TABLE",
        subject_ids=[uuid4(), uuid4()],
        owner_type="GROUP",
        owner_principal="risk-data-team",
    )
    assert operation.owner_principal == "risk-data-team"


def test_bulk_operation_rejects_duplicate_subjects() -> None:
    subject_id = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        BulkStewardshipOperationCreate(
            operation_type="LINK_TERM",
            subject_type="TABLE",
            subject_ids=[subject_id, subject_id],
            term_id=uuid4(),
        )


def test_link_term_operation_requires_term_id() -> None:
    with pytest.raises(ValidationError, match="term_id"):
        BulkStewardshipOperationCreate(
            operation_type="LINK_TERM",
            subject_type="TABLE",
            subject_ids=[uuid4()],
        )


def test_deprecate_and_certify_operations_require_rationale() -> None:
    with pytest.raises(ValidationError, match="rationale"):
        BulkStewardshipOperationCreate(
            operation_type="DEPRECATE_TERM",
            subject_type="TERM",
            subject_ids=[uuid4()],
        )
    with pytest.raises(ValidationError, match="rationale"):
        BulkStewardshipOperationCreate(
            operation_type="CERTIFY_ASSET",
            subject_type="TABLE",
            subject_ids=[uuid4()],
        )


def test_certify_operation_requires_an_expiry() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        BulkStewardshipOperationCreate(
            operation_type="CERTIFY_ASSET",
            subject_type="TABLE",
            subject_ids=[uuid4()],
            rationale="Owner confirmed accuracy against the source ledger.",
        )


def test_glossary_conflict_resolution_requires_rationale() -> None:
    with pytest.raises(ValidationError):
        GlossaryConflictResolution(resolution="ACCEPT_POSITION_A", rationale="short")

    # The losing position is never discarded by the resolution payload itself --
    # RETAIN_BOTH is a first-class resolution outcome, not a fallback.
    resolution = GlossaryConflictResolution(
        resolution="RETAIN_BOTH",
        rationale="Both definitions are valid in their respective reporting contexts.",
    )
    assert resolution.resolution == "RETAIN_BOTH"


def test_glossary_link_proposal_generation_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        GlossaryLinkProposalGenerate(minimum_confidence=0.1)
    with pytest.raises(ValidationError):
        GlossaryLinkProposalGenerate(limit=0)

    defaults = GlossaryLinkProposalGenerate()
    assert 0.5 <= defaults.minimum_confidence <= 1.0
    assert 1 <= defaults.limit <= 500


def test_glossary_category_key_must_be_stable_lowercase() -> None:
    with pytest.raises(ValidationError):
        GlossaryCategoryCreate(
            category_key="Finance Domain",
            display_name="Finance",
            description="Finance-related governed terms.",
        )

    category = GlossaryCategoryCreate(
        category_key="finance",
        display_name="Finance",
        description="Finance-related governed terms.",
    )
    assert category.category_key == "finance"
