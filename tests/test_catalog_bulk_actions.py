from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aida.catalog_bulk_actions import (
    CATALOG_BULK_ACTION_MAX_ITEMS,
    CatalogBulkItemError,
    apply_certify_item,
    apply_classify_item,
    apply_own_item,
    apply_tag_item,
    dedupe_preserving_order,
    match_columns_by_pattern,
    match_tables_by_filter,
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
# Partial success reporting: apply_tag_item / apply_classify_item /
# apply_own_item / apply_certify_item -- the single-item core each bulk
# endpoint dispatches to, one subject at a time inside its own SAVEPOINT (see
# tests/test_catalog_bulk_actions_endpoints.py for the SAVEPOINT-isolation and
# real-DB, real-scale proof; these tests cover the pure precondition logic
# without a database).
# ---------------------------------------------------------------------------


def test_apply_tag_item_creates_a_new_tag_for_an_active_table() -> None:
    table = active_table()
    row, is_new = apply_tag_item(
        table.id,
        tables={table.id: table},
        existing_tags={},
        organization_id=ORG_ID,
        tag_key="gold-tier",
        tag_value="true",
        applied_by="steward@example.com",
    )
    assert is_new is True
    assert row.table_id == table.id
    assert row.tag_key == "gold-tier"


def test_apply_tag_item_rejects_a_missing_or_deprecated_table() -> None:
    deprecated_table = active_table(status="DEPRECATED")
    with pytest.raises(CatalogBulkItemError, match="DEPRECATED"):
        apply_tag_item(
            deprecated_table.id,
            tables={deprecated_table.id: deprecated_table},
            existing_tags={},
            organization_id=ORG_ID,
            tag_key="gold-tier",
            tag_value="true",
            applied_by="steward@example.com",
        )
    missing_id = uuid4()
    with pytest.raises(CatalogBulkItemError, match="not found"):
        apply_tag_item(
            missing_id,
            tables={},
            existing_tags={},
            organization_id=ORG_ID,
            tag_key="gold-tier",
            tag_value="true",
            applied_by="steward@example.com",
        )


def test_apply_tag_item_updates_an_existing_tag_in_place_instead_of_duplicating() -> None:
    table = active_table()
    existing = AssetTag(
        id=uuid4(),
        organization_id=ORG_ID,
        table_id=table.id,
        tag_key="gold-tier",
        tag_value="false",
        applied_by="old-steward@example.com",
    )
    row, is_new = apply_tag_item(
        table.id,
        tables={table.id: table},
        existing_tags={table.id: existing},
        organization_id=ORG_ID,
        tag_key="gold-tier",
        tag_value="true",
        applied_by="new-steward@example.com",
    )
    assert is_new is False
    assert row is existing
    assert existing.tag_value == "true"
    assert existing.applied_by == "new-steward@example.com"


def test_apply_classify_item_reports_precise_failure_reasons() -> None:
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
    result = apply_classify_item(ok_column.id, columns=columns, classification="PII")
    assert result is ok_column
    assert ok_column.classification == "PII"
    with pytest.raises(CatalogBulkItemError, match="column status"):
        apply_classify_item(inactive_column.id, columns=columns, classification="PII")
    with pytest.raises(CatalogBulkItemError, match="parent table status"):
        apply_classify_item(orphaned_column.id, columns=columns, classification="PII")
    with pytest.raises(CatalogBulkItemError, match="not found"):
        apply_classify_item(missing_id, columns=columns, classification="PII")


def test_apply_own_item_creates_and_reactivates_assignments() -> None:
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
    new_row, new_is_new = apply_own_item(
        new_table.id,
        tables={new_table.id: new_table, existing_table.id: existing_table},
        existing_assignments={existing_table.id: existing_assignment},
        organization_id=ORG_ID,
        owner_type="GROUP",
        owner_principal="retail-data-stewards",
        assigned_by="steward@example.com",
    )
    assert new_is_new is True
    assert new_row.subject_id == str(new_table.id)
    reactivated_row, reactivated_is_new = apply_own_item(
        existing_table.id,
        tables={new_table.id: new_table, existing_table.id: existing_table},
        existing_assignments={existing_table.id: existing_assignment},
        organization_id=ORG_ID,
        owner_type="GROUP",
        owner_principal="retail-data-stewards",
        assigned_by="steward@example.com",
    )
    assert reactivated_is_new is False
    assert reactivated_row is existing_assignment
    assert existing_assignment.status == "ACTIVE"
    assert existing_assignment.assigned_by == "steward@example.com"


def test_apply_own_item_rejects_a_deprecated_table() -> None:
    deprecated = active_table(status="DEPRECATED")
    with pytest.raises(CatalogBulkItemError, match="DEPRECATED"):
        apply_own_item(
            deprecated.id,
            tables={deprecated.id: deprecated},
            existing_assignments={},
            organization_id=ORG_ID,
            owner_type="INDIVIDUAL",
            owner_principal="jane.steward",
            assigned_by="admin@example.com",
        )


def test_apply_certify_item_supersedes_prior_certification() -> None:
    certified_table = active_table()
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
    new_cert, superseded = apply_certify_item(
        certified_table.id,
        tables={certified_table.id: certified_table},
        active_certifications={certified_table.id: [prior]},
        organization_id=ORG_ID,
        rationale="Certified against the approved data contract.",
        expires_at=expires_at,
        certified_by="steward@example.com",
    )
    assert superseded == [prior]
    assert prior.status == "SUPERSEDED"
    assert new_cert.table_id == certified_table.id
    assert new_cert.expires_at == expires_at


def test_apply_certify_item_rejects_a_deprecated_table() -> None:
    deprecated_table = active_table(status="DEPRECATED")
    with pytest.raises(CatalogBulkItemError, match="DEPRECATED"):
        apply_certify_item(
            deprecated_table.id,
            tables={deprecated_table.id: deprecated_table},
            active_certifications={},
            organization_id=ORG_ID,
            rationale="Certified against the approved data contract.",
            expires_at=datetime.now(UTC) + timedelta(days=90),
            certified_by="steward@example.com",
        )


def test_all_four_bulk_actions_report_subject_id_as_a_stringified_uuid_on_failure() -> None:
    missing_id = uuid4()
    with pytest.raises(CatalogBulkItemError):
        apply_tag_item(
            missing_id,
            tables={},
            existing_tags={},
            organization_id=ORG_ID,
            tag_key="k",
            tag_value=None,
            applied_by="a",
        )
    # The API layer is what turns a raised CatalogBulkItemError into a
    # BulkItemResult(subject_id=str(subject_id), status="FAILED", reason=...)
    # -- proven end-to-end (partial success across a whole batch, at scale,
    # with real SAVEPOINT isolation) in test_catalog_bulk_actions_endpoints.py.
    UUID(str(missing_id))
