"""GL-9 exit-condition tests.

Two things must be true no matter how the drafting logic evolves:

(a) a low-evidence draft can never reach a state a non-reviewer could
    mistake for published — the minimum-evidence gate (`ensure_reviewable`)
    blocks it before a `governance_review` row is ever created, and

(b) a high-confidence draft still requires independent approval — there is
    no code path, however high the score, that publishes a description
    without going through `semantic_api.decide_governance_review`, and that
    function's shared maker-checker guard (self-approval denied) runs
    before the ASSET_DESCRIPTION_DRAFT branch is ever reached.

The rest of the file exercises the deterministic scoring and composition
pure functions directly: no database, no network, no external model call —
matching how the sibling GL-8 inference and business-semantics inference
logic is tested elsewhere in this suite.
"""

import inspect
import re
from uuid import uuid4

import pytest
from fastapi import HTTPException

import aida.asset_description_api as asset_description_api
import aida.asset_description_service as asset_description_service
import aida.semantic_api as semantic_api
from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    AssetEvidence,
    compose_draft_text,
    ensure_reviewable,
    evidence_payload,
    score_evidence,
    text_fingerprint,
)
from aida.main import app


def _evidence(**overrides: object) -> AssetEvidence:
    defaults: dict[str, object] = {
        "table_id": uuid4(),
        "table_name": "accounts",
        "schema_name": "public",
        "column_count": 0,
        "primary_key_columns": (),
        "foreign_key_count": 0,
        "upstream_table_names": (),
        "upstream_edge_ids": (),
        "downstream_table_names": (),
        "downstream_edge_ids": (),
        "dbt_description": None,
        "dbt_documented_column_count": 0,
        "business_name": None,
        "business_description": None,
        "business_annotation_id": None,
        "grain_statement": None,
        "bound_term_names": (),
        "bound_term_ids": (),
    }
    defaults.update(overrides)
    return AssetEvidence(**defaults)  # type: ignore[arg-type]


def _well_documented_evidence() -> AssetEvidence:
    return _evidence(
        column_count=6,
        primary_key_columns=("account_id",),
        foreign_key_count=2,
        upstream_table_names=("raw_accounts",),
        upstream_edge_ids=(uuid4(),),
        downstream_table_names=("account_summary",),
        downstream_edge_ids=(uuid4(),),
        dbt_description="Curated customer deposit account records.",
        dbt_documented_column_count=6,
        business_name="Customer Accounts",
        business_description="Approved, deduplicated deposit account records.",
        business_annotation_id=uuid4(),
        grain_statement="One row per account_id.",
        bound_term_names=("Deposit Account",),
        bound_term_ids=(uuid4(),),
    )


def test_asset_description_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/asset-description-drafts/generate" in paths
    assert "/v1/organizations/{organization_id}/asset-description-drafts" in paths
    assert "/v1/asset-description-drafts/{draft_id}/submit" in paths


def test_score_evidence_is_a_deterministic_pure_function() -> None:
    evidence = _well_documented_evidence()
    first = score_evidence(evidence)
    second = score_evidence(evidence)
    assert first == second


def test_score_evidence_rewards_corroborating_evidence() -> None:
    bare = score_evidence(_evidence())
    partial = score_evidence(
        _evidence(column_count=3, primary_key_columns=("id",), bound_term_names=("Some Term",))
    )
    rich = score_evidence(_well_documented_evidence())

    assert bare.overall < partial.overall < rich.overall
    for dimension in ("accuracy", "clarity", "style", "completeness"):
        assert getattr(bare, dimension) <= getattr(rich, dimension)


def test_zero_evidence_scores_below_the_review_threshold() -> None:
    bare = score_evidence(_evidence())
    assert bare.overall < MINIMUM_EVIDENCE_FOR_REVIEW


def test_well_documented_table_scores_at_or_above_the_review_threshold() -> None:
    rich = score_evidence(_well_documented_evidence())
    assert rich.overall >= MINIMUM_EVIDENCE_FOR_REVIEW
    assert rich.overall == 1.0


def test_compose_draft_text_uses_only_evidence_fields_no_model_call() -> None:
    bare_text = compose_draft_text(_evidence(table_name="ledger_entries", column_count=0))
    assert bare_text == "ledger_entries is a table in the public schema with 0 columns."

    rich_text = compose_draft_text(_well_documented_evidence())
    assert "accounts is a table in the public schema with 6 columns." in rich_text
    assert "keyed by account_id" in rich_text
    assert "populated from raw_accounts" in rich_text
    assert "feeds account_summary" in rich_text
    assert "Curated customer deposit account records." in rich_text
    assert "Approved, deduplicated deposit account records." in rich_text
    assert "One row per account_id." in rich_text
    assert "Deposit Account" in rich_text


def test_text_fingerprint_is_stable_and_evidence_payload_is_json_safe() -> None:
    text = compose_draft_text(_well_documented_evidence())
    assert text_fingerprint(text) == text_fingerprint(text)

    evidence = _well_documented_evidence()
    payload = evidence_payload(evidence)
    assert payload["bound_term_ids"] == [str(evidence.bound_term_ids[0])]
    assert isinstance(payload["dbt_description_present"], bool)


# --- exit condition (a): a low-evidence draft never reaches PENDING_APPROVAL ---


def test_ensure_reviewable_blocks_low_evidence_drafts() -> None:
    bare_score = score_evidence(_evidence()).overall
    with pytest.raises(HTTPException) as exc_info:
        ensure_reviewable(bare_score)
    assert exc_info.value.status_code == 422


def test_ensure_reviewable_allows_well_evidenced_drafts() -> None:
    rich_score = score_evidence(_well_documented_evidence()).overall
    ensure_reviewable(rich_score)  # must not raise


def test_submit_endpoint_calls_the_evidence_gate_before_creating_a_review() -> None:
    """`ensure_reviewable` must run, and must run before any GovernanceReview
    row is constructed, in the submit endpoint's source — so a draft that
    fails the gate can never leave a GovernanceReview behind."""
    source = inspect.getsource(asset_description_api.submit_asset_description_draft)
    gate_at = source.index("ensure_reviewable(")
    review_construction_at = source.index("GovernanceReview(\n")
    assert gate_at < review_construction_at


# --- exit condition (b): no path publishes without decide_governance_review ---


def test_apply_asset_description_draft_has_exactly_one_call_site() -> None:
    """The only function that can move a draft to APPROVED and publish its
    text onto AssetDocumentationVersion is `apply_asset_description_draft`.
    It must be called from nowhere but `semantic_api.decide_governance_review`
    — i.e. there is no bypass, however high a draft's confidence score."""
    source = inspect.getsource(semantic_api)
    call_pattern = re.compile(r"apply_asset_description_draft\(")
    matches = call_pattern.findall(source)
    # one import + one call site inside decide_governance_review
    assert source.count("apply_asset_description_draft") == 2
    assert len(matches) == 1

    decide_source = inspect.getsource(semantic_api.decide_governance_review)
    assert "apply_asset_description_draft(" in decide_source
    assert "reject_asset_description_draft(" in decide_source


def test_decide_governance_review_checks_self_approval_before_publishing() -> None:
    """The shared maker-checker guard must textually precede the
    ASSET_DESCRIPTION_DRAFT dispatch branch, so self-approval is denied
    before any GL-9 draft can be published — the same guard every other
    governed object type in this dispatch chain relies on."""
    decide_source = inspect.getsource(semantic_api.decide_governance_review)
    guard_at = decide_source.index("maker-checker separation is required")
    branch_at = decide_source.index('review.object_type == "ASSET_DESCRIPTION_DRAFT"')
    publish_at = decide_source.index("apply_asset_description_draft(")
    assert guard_at < branch_at < publish_at


def test_apply_asset_description_draft_refuses_a_non_pending_draft() -> None:
    """Belt-and-braces: even if something else called the publish function
    directly, it refuses to act on a draft that is not PENDING_APPROVAL —
    the state `decide_governance_review` puts it in immediately before
    calling apply."""
    source = inspect.getsource(asset_description_service.apply_asset_description_draft)
    assert 'draft.status != "PENDING_APPROVAL"' in source
    assert "raise HTTPException" in source
