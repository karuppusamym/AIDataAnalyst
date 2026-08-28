import pytest
from pydantic import ValidationError

from aida.main import app
from aida.schemas import (
    AssetDocumentationVersionCreate,
    AssetTermLinkCreate,
    BulkStewardshipOperationCreate,
    GlossaryConflictResolution,
    GlossaryTermCreate,
    OwnershipRuleCreate,
)


def test_glossary_and_documentation_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/glossary-terms" in paths
    assert "/v1/glossary-terms/{term_id}/versions" in paths
    assert "/v1/glossary-term-versions/{version_id}/submit" in paths
    assert "/v1/metadata/tables/{table_id}/documentation" in paths
    assert "/v1/metadata/tables/{table_id}/documentation-versions" in paths
    assert "/v1/asset-documentation-versions/{version_id}/submit" in paths
    assert "/v1/metadata/tables/{table_id}/glossary-links" in paths
    assert "/v1/asset-term-links/{link_id}" in paths


def test_complete_stewardship_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/organizations/{organization_id}/glossary-categories",
        "/v1/glossary-terms/{term_id}/deprecate",
        "/v1/organizations/{organization_id}/ownership-rules",
        "/v1/ownership-rules/{rule_id}/apply",
        "/v1/organizations/{organization_id}/ownership-assignments",
        "/v1/organizations/{organization_id}/stewardship/bulk-operations",
        "/v1/organizations/{organization_id}/stewardship/coverage",
        "/v1/organizations/{organization_id}/stewardship/coverage/snapshots",
        "/v1/organizations/{organization_id}/glossary-conflicts",
        "/v1/organizations/{organization_id}/glossary-conflicts/detect",
        "/v1/glossary-conflicts/{conflict_id}/resolution",
        "/v1/organizations/{organization_id}/glossary-link-proposals/generate",
        "/v1/organizations/{organization_id}/glossary-link-proposals",
        "/v1/glossary-link-proposals/{proposal_id}/submit",
    }
    assert expected <= paths.keys()


def test_glossary_term_requires_stable_lowercase_key_and_definition() -> None:
    with pytest.raises(ValidationError):
        GlossaryTermCreate(
            term_key="Monthly Revenue",
            display_name="Monthly revenue",
            definition="A governed monthly revenue definition.",
        )


def test_glossary_term_normalizes_and_deduplicates_synonyms() -> None:
    term = GlossaryTermCreate(
        term_key="monthly_revenue",
        display_name="Monthly revenue",
        definition="Governed monthly recognized revenue across approved products.",
        synonyms=[" MRR ", "Monthly recurring revenue"],
    )
    assert term.synonyms == ["MRR", "Monthly recurring revenue"]
    with pytest.raises(ValidationError, match="unique ignoring case"):
        GlossaryTermCreate(
            term_key="monthly_revenue",
            display_name="Monthly revenue",
            definition="Governed monthly recognized revenue across approved products.",
            synonyms=["MRR", "mrr"],
        )
    with pytest.raises(ValidationError):
        GlossaryTermCreate(
            term_key="monthly_revenue",
            display_name="Monthly revenue",
            definition="short",
        )


def test_asset_documentation_normalizes_aliases() -> None:
    document = AssetDocumentationVersionCreate(
        aliases=[" Customer master ", "Client directory"],
        readme="Authoritative customer-level asset documentation.",
        owner_principal="customer-data-steward",
    )
    assert document.aliases == ["Customer master", "Client directory"]


def test_asset_documentation_rejects_duplicate_aliases_ignoring_case() -> None:
    with pytest.raises(ValidationError, match="unique ignoring case"):
        AssetDocumentationVersionCreate(
            aliases=["Customer Master", "customer master"],
            readme="Authoritative customer-level asset documentation.",
        )


def test_asset_term_link_requires_uuid() -> None:
    with pytest.raises(ValidationError):
        AssetTermLinkCreate(term_id="not-a-uuid")


def test_bulk_stewardship_contract_enforces_operation_parameters() -> None:
    with pytest.raises(ValidationError, match="require owner_type"):
        BulkStewardshipOperationCreate(
            operation_type="ASSIGN_OWNERSHIP",
            subject_type="TABLE",
            subject_ids=["11111111-1111-1111-1111-111111111111"],
        )
    with pytest.raises(ValidationError, match="subject_ids must be unique"):
        BulkStewardshipOperationCreate(
            operation_type="LINK_TERM",
            subject_type="TABLE",
            subject_ids=[
                "11111111-1111-1111-1111-111111111111",
                "11111111-1111-1111-1111-111111111111",
            ],
            term_id="22222222-2222-2222-2222-222222222222",
        )


def test_certification_and_conflict_resolution_require_governance_evidence() -> None:
    with pytest.raises(ValidationError, match="require expires_at"):
        BulkStewardshipOperationCreate(
            operation_type="CERTIFY_ASSET",
            subject_type="TABLE",
            subject_ids=["11111111-1111-1111-1111-111111111111"],
            rationale="Certified against the approved data contract.",
        )
    with pytest.raises(ValidationError):
        GlossaryConflictResolution(
            resolution="MERGE",
            rationale="short",
        )


def test_ownership_rule_supports_individual_group_and_bounded_match_fields() -> None:
    rule = OwnershipRuleCreate(
        rule_key="retail_tables",
        display_name="Retail table owners",
        match_field="QUALIFIED_NAME",
        match_pattern="retail.*",
        owner_type="GROUP",
        owner_principal="retail-data-stewards",
    )
    assert rule.owner_type == "GROUP"
    with pytest.raises(ValidationError):
        OwnershipRuleCreate(
            rule_key="retail_tables",
            display_name="Retail table owners",
            match_field="DATABASE_NAME",
            match_pattern="retail",
            owner_type="GROUP",
            owner_principal="retail-data-stewards",
        )
