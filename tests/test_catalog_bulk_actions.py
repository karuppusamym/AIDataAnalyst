from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aida.catalog_bulk_actions import (
    CATALOG_BULK_ACTION_MAX_ITEMS,
    dedupe_preserving_order,
    match_columns_by_pattern,
    match_tables_by_filter,
    plan_certify,
    plan_classify,
    plan_own,
    plan_tag,
)
from aida.main import app
from aida.models import (
    AssetCertification,
    AssetTag,
    MetadataColumn,
    MetadataTable,
    OwnershipAssignment,
)
from aida.schemas import (
    CatalogBulkCertifyRequest,
    CatalogBulkClassifyRequest,
    CatalogBulkOwnRequest,
    CatalogBulkSelectionFilter,
    CatalogBulkTagRequest,
)

ORG_ID = uuid4()


def active_table(status: str = "ACTIVE") -> MetadataTable:
    return MetadataTable(id=uuid4(), status=status)


def active_column(status: str = "ACTIVE") -> MetadataColumn:
    return MetadataColumn(id=uuid4(), status=status)


# ---------------------------------------------------------------------------
# API contract: the new endpoints are registered on the existing catalog router
# ---------------------------------------------------------------------------


def test_catalog_bulk_action_endpoints_are_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/organizations/{organization_id}/tables/bulk-tag",
        "/v1/organizations/{organization_id}/tables/bulk-classify",
        "/v1/organizations/{organization_id}/tables/bulk-own",
        "/v1/organizations/{organization_id}/tables/bulk-certify",
        "/v1/organizations/{organization_id}/catalog-bulk-actions",
        "/v1/organizations/{organization_id}/catalog-bulk-actions/{run_id}",
    }
    assert expected <= paths.keys()
    for path in (
        "/v1/organizations/{organization_id}/tables/bulk-tag",
        "/v1/organizations/{organization_id}/tables/bulk-classify",
        "/v1/organizations/{organization_id}/tables/bulk-own",
        "/v1/organizations/{organization_id}/tables/bulk-certify",
    ):
        assert "post" in paths[path]


# ---------------------------------------------------------------------------
# Request contract: filter-or-explicit-selection and the batch-size bound
# ---------------------------------------------------------------------------


def test_bulk_tag_requires_exactly_one_selection_source() -> None:
    with pytest.raises(ValidationError, match="exactly one selection"):
        CatalogBulkTagRequest(tag_key="pii-review")
    with pytest.raises(ValidationError, match="exactly one selection"):
        CatalogBulkTagRequest(
            table_ids=[uuid4()],
            filter=CatalogBulkSelectionFilter(datasource_id=uuid4(), match_pattern="*"),
            tag_key="pii-review",
        )


def test_bulk_tag_accepts_explicit_ids_or_a_filter() -> None:
    by_ids = CatalogBulkTagRequest(table_ids=[uuid4(), uuid4()], tag_key="gold-tier")
    assert by_ids.filter is None
    by_filter = CatalogBulkTagRequest(
        filter=CatalogBulkSelectionFilter(
            datasource_id=uuid4(), match_field="SCHEMA_NAME", match_pattern="retail*"
        ),
        tag_key="gold-tier",
    )
    assert by_filter.table_ids is None


def test_explicit_selection_batch_size_is_bounded_with_a_clear_error() -> None:
    too_many = [uuid4() for _ in range(CATALOG_BULK_ACTION_MAX_ITEMS + 1)]
    with pytest.raises(ValidationError, match="at most 500 items"):
        CatalogBulkTagRequest(table_ids=too_many, tag_key="gold-tier")
    # exactly at the cap is fine
    at_cap = [uuid4() for _ in range(CATALOG_BULK_ACTION_MAX_ITEMS)]
    request = CatalogBulkTagRequest(table_ids=at_cap, tag_key="gold-tier")
    assert len(request.table_ids or []) == CATALOG_BULK_ACTION_MAX_ITEMS


def test_bulk_classify_requires_exactly_one_selection_and_a_supported_value() -> None:
    with pytest.raises(ValidationError):
        CatalogBulkClassifyRequest(classification="PII")
    with pytest.raises(ValidationError):
        CatalogBulkClassifyRequest(table_ids=[uuid4()], classification="TOP_SECRET")
    request = CatalogBulkClassifyRequest(column_ids=[uuid4()], classification="PII")
    assert request.classification == "PII"


def test_bulk_own_requires_owner_fields() -> None:
    with pytest.raises(ValidationError):
        CatalogBulkOwnRequest(table_ids=[uuid4()], owner_type="INDIVIDUAL", owner_principal="x")
    request = CatalogBulkOwnRequest(
        table_ids=[uuid4()], owner_type="GROUP", owner_principal="retail-data-stewards"
    )
    assert request.owner_type == "GROUP"


def test_bulk_certify_requires_rationale_and_expiry() -> None:
    with pytest.raises(ValidationError):
        CatalogBulkCertifyRequest(
            table_ids=[uuid4()],
            rationale="short",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    request = CatalogBulkCertifyRequest(
        table_ids=[uuid4()],
        rationale="Certified against the approved quarterly data contract.",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    assert request.rationale.startswith("Certified")


# ---------------------------------------------------------------------------
# Selection resolution: filter matching and the bounded-scan cap
# ---------------------------------------------------------------------------


def test_match_tables_by_filter_is_case_insensitive_and_scoped_to_qualified_name() -> None:
    retail_orders = MetadataTable(id=uuid4(), name="Orders", status="ACTIVE")
    finance_orders = MetadataTable(id=uuid4(), name="orders_archive", status="ACTIVE")
    candidates = [(retail_orders, "retail"), (finance_orders, "finance")]
    matched, truncated = match_tables_by_filter(
        candidates, match_field="QUALIFIED_NAME", match_pattern="retail.orders"
    )
    assert matched == [retail_orders.id]
    assert truncated is False


def test_match_tables_by_filter_truncates_at_the_cap_instead_of_growing_unbounded() -> None:
    candidates = [
        (MetadataTable(id=uuid4(), name=f"table_{i}", status="ACTIVE"), "sales") for i in range(10)
    ]
    matched, truncated = match_tables_by_filter(
        candidates, match_field="TABLE_NAME", match_pattern="table_*", cap=3
    )
    assert len(matched) == 3
    assert truncated is True


def test_match_columns_by_pattern_matches_and_respects_cap() -> None:
    names = ("ssn", "email", "id")
    columns = [MetadataColumn(id=uuid4(), name=name, status="ACTIVE") for name in names]
    matched, truncated = match_columns_by_pattern(columns, name_pattern="*")
    assert len(matched) == 3
    assert truncated is False
    matched_capped, truncated_capped = match_columns_by_pattern(columns, name_pattern="*", cap=1)
    assert len(matched_capped) == 1
    assert truncated_capped is True


def test_dedupe_preserving_order() -> None:
    a, b = uuid4(), uuid4()
    assert dedupe_preserving_order([a, b, a]) == [a, b]


# ---------------------------------------------------------------------------
# Partial success reporting: plan_tag / plan_classify / plan_own / plan_certify
# ---------------------------------------------------------------------------


def test_plan_tag_reports_partial_success_for_missing_and_deprecated_tables() -> None:
    ok_table = active_table()
    deprecated_table = active_table(status="DEPRECATED")
    missing_id = uuid4()
    subject_ids = [ok_table.id, deprecated_table.id, missing_id]
    plan = plan_tag(
        subject_ids,
        tables={ok_table.id: ok_table, deprecated_table.id: deprecated_table},
        existing_tags={},
        organization_id=ORG_ID,
        tag_key="gold-tier",
        tag_value="true",
        applied_by="steward@example.com",
    )
    assert plan.succeeded_count == 1
    assert plan.failed_count == 2
    by_id = {item.subject_id: item for item in plan.results}
    assert by_id[str(ok_table.id)].status == "SUCCEEDED"
    assert by_id[str(deprecated_table.id)].status == "FAILED"
    assert "DEPRECATED" in (by_id[str(deprecated_table.id)].reason or "")
    assert by_id[str(missing_id)].status == "FAILED"
    assert "not found" in (by_id[str(missing_id)].reason or "")
    assert len(plan.new_rows) == 1
    assert plan.new_rows[0].table_id == ok_table.id
    assert plan.new_rows[0].tag_key == "gold-tier"


def test_plan_tag_updates_an_existing_tag_in_place_instead_of_duplicating() -> None:
    table = active_table()
    existing = AssetTag(
        id=uuid4(),
        organization_id=ORG_ID,
        table_id=table.id,
        tag_key="gold-tier",
        tag_value="false",
        applied_by="old-steward@example.com",
    )
    plan = plan_tag(
        [table.id],
        tables={table.id: table},
        existing_tags={table.id: existing},
        organization_id=ORG_ID,
        tag_key="gold-tier",
        tag_value="true",
        applied_by="new-steward@example.com",
    )
    assert plan.succeeded_count == 1
    assert plan.new_rows == []
    assert existing.tag_value == "true"
    assert existing.applied_by == "new-steward@example.com"


def test_plan_classify_reports_partial_success_across_columns() -> None:
    ok_column = active_column()
    ok_table = active_table()
    inactive_column = active_column(status="DEPRECATED")
    inactive_table = active_table()
    orphaned_column = active_column()
    deprecated_parent_table = active_table(status="DEPRECATED")
    missing_id = uuid4()
    columns = {
        ok_column.id: (ok_column, ok_table),
        inactive_column.id: (inactive_column, inactive_table),
        orphaned_column.id: (orphaned_column, deprecated_parent_table),
    }
    subject_ids = [ok_column.id, inactive_column.id, orphaned_column.id, missing_id]
    plan = plan_classify(subject_ids, columns=columns, classification="PII")
    assert plan.succeeded_count == 1
    assert plan.failed_count == 3
    assert ok_column.classification == "PII"
    by_id = {item.subject_id: item for item in plan.results}
    assert "column status" in (by_id[str(inactive_column.id)].reason or "")
    assert "parent table status" in (by_id[str(orphaned_column.id)].reason or "")
    assert "not found" in (by_id[str(missing_id)].reason or "")


def test_plan_own_creates_and_reactivates_assignments() -> None:
    new_table = active_table()
    existing_table = active_table()
    existing_assignment = OwnershipAssignment(
        id=uuid4(),
        organization_id=ORG_ID,
        subject_type="TABLE",
        subject_id=str(existing_table.id),
        owner_type="GROUP",
        owner_principal="retail-data-stewards",
        assignment_kind="MANUAL",
        status="INACTIVE",
        assigned_by="prior-run@example.com",
    )
    plan = plan_own(
        [new_table.id, existing_table.id],
        tables={new_table.id: new_table, existing_table.id: existing_table},
        existing_assignments={existing_table.id: existing_assignment},
        organization_id=ORG_ID,
        owner_type="GROUP",
        owner_principal="retail-data-stewards",
        assigned_by="steward@example.com",
    )
    assert plan.succeeded_count == 2
    assert len(plan.new_rows) == 1
    assert plan.new_rows[0].subject_id == str(new_table.id)
    assert existing_assignment.status == "ACTIVE"
    assert existing_assignment.assigned_by == "steward@example.com"


def test_plan_own_fails_deprecated_tables_while_succeeding_active_ones() -> None:
    active = active_table()
    deprecated = active_table(status="DEPRECATED")
    plan = plan_own(
        [active.id, deprecated.id],
        tables={active.id: active, deprecated.id: deprecated},
        existing_assignments={},
        organization_id=ORG_ID,
        owner_type="INDIVIDUAL",
        owner_principal="jane.steward",
        assigned_by="admin@example.com",
    )
    assert plan.succeeded_count == 1
    assert plan.failed_count == 1


def test_plan_certify_supersedes_prior_certification_and_reports_failures() -> None:
    certified_table = active_table()
    deprecated_table = active_table(status="DEPRECATED")
    prior = AssetCertification(
        id=uuid4(),
        organization_id=ORG_ID,
        table_id=certified_table.id,
        status="ACTIVE",
        rationale="Prior quarter certification.",
        certified_by="old-owner@example.com",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    expires_at = datetime.now(UTC) + timedelta(days=90)
    plan = plan_certify(
        [certified_table.id, deprecated_table.id],
        tables={certified_table.id: certified_table, deprecated_table.id: deprecated_table},
        active_certifications={certified_table.id: [prior]},
        organization_id=ORG_ID,
        rationale="Certified against the approved data contract.",
        expires_at=expires_at,
        certified_by="steward@example.com",
    )
    assert plan.succeeded_count == 1
    assert plan.failed_count == 1
    assert prior.status == "SUPERSEDED"
    assert len(plan.new_rows) == 1
    new_cert = plan.new_rows[0]
    assert new_cert.table_id == certified_table.id
    assert new_cert.expires_at == expires_at
    by_id = {item.subject_id: item for item in plan.results}
    assert by_id[str(deprecated_table.id)].status == "FAILED"


def test_all_four_bulk_actions_share_the_same_result_shape() -> None:
    table = active_table()
    tag_plan = plan_tag(
        [table.id],
        tables={table.id: table},
        existing_tags={},
        organization_id=ORG_ID,
        tag_key="k",
        tag_value=None,
        applied_by="a",
    )
    assert {item.status for item in tag_plan.results} <= {"SUCCEEDED", "FAILED"}
    for item in tag_plan.results:
        assert isinstance(item.subject_id, str)
        UUID(item.subject_id)  # subject_id is always a stringified UUID
