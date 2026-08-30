from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aida.dbt_quality_bridge import (
    dbt_incident_fingerprint,
    infer_dbt_test_anomaly_type,
    reconcile_dbt_test_quality,
)
from aida.models import (
    DataQualityIncident,
    DbtResource,
    MetadataTable,
)
from aida.security import SecurityContext


def test_infer_anomaly_type() -> None:
    assert (
        infer_dbt_test_anomaly_type("not_null_customer_id", "test.bank.not_null_customer_id")
        == "NOT_NULL_VIOLATION"
    )
    assert (
        infer_dbt_test_anomaly_type("unique_customer_id", "test.bank.unique_customer_id")
        == "UNIQUENESS_BREACH"
    )
    assert (
        infer_dbt_test_anomaly_type(
            "relationships_customer_id", "test.bank.relationships_customer_id"
        )
        == "RELATIONSHIP_BREACH"
    )
    assert (
        infer_dbt_test_anomaly_type(
            "accepted_values_status", "test.bank.accepted_values_status"
        )
        == "ACCEPTED_VALUES_BREACH"
    )
    assert (
        infer_dbt_test_anomaly_type("custom_assertion", "test.bank.custom_assertion")
        == "TRANSFORMATION_TEST_FAILURE"
    )


def test_fingerprint_deterministic() -> None:
    org_id = uuid4()
    ds_id = uuid4()
    table_id = uuid4()
    test_uid = "test.bank.customer_summary_not_null"

    fp1 = dbt_incident_fingerprint(org_id, ds_id, table_id, test_uid)
    fp2 = dbt_incident_fingerprint(org_id, ds_id, table_id, test_uid)
    assert fp1 == fp2
    assert len(fp1) == 64


@pytest.mark.asyncio
async def test_reconcile_dbt_test_creates_incident() -> None:
    from unittest.mock import AsyncMock, MagicMock

    org_id = uuid4()
    ds_id = uuid4()
    table_id = uuid4()
    import_id = uuid4()

    table = MetadataTable(
        id=table_id,
        organization_id=org_id,
        schema_id=uuid4(),
        datasource_id=ds_id,
        name="customer_summary",
        object_type="TABLE",
        fingerprint="fp-table",
    )

    model_res = DbtResource(
        id=uuid4(),
        organization_id=org_id,
        artifact_import_id=import_id,
        unique_id="model.bank.customer_summary",
        resource_type="MODEL",
        package_name="bank",
        name="customer_summary",
        sql_parse_status="PARSED",
        column_names=["customer_id", "balance"],
        matched_table_id=table_id,
    )

    test_res = DbtResource(
        id=uuid4(),
        organization_id=org_id,
        artifact_import_id=import_id,
        unique_id="test.bank.not_null_customer_id",
        resource_type="TEST",
        package_name="bank",
        name="not_null_customer_id",
        sql_parse_status="PARSED",
        column_names=[],
        depends_on_unique_ids=["model.bank.customer_summary"],
        test_status="FAIL",
        test_failures=12,
        test_execution_time=0.45,
    )

    context = SecurityContext(
        principal_id="admin@bank.internal",
        principal_type="USER",
        roles=frozenset({"PlatformAdmin"}),
        organization_id=org_id,
    )

    session = AsyncMock()
    session.get = AsyncMock(return_value=table)
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()

    counts = await reconcile_dbt_test_quality(
        session,
        organization_id=org_id,
        datasource_id=ds_id,
        dbt_resources=[model_res, test_res],
        context=context,
    )

    assert counts["incidents_opened"] == 1
    added_objs = [call[0][0] for call in session.add.call_args_list]
    added_incident = next(obj for obj in added_objs if isinstance(obj, DataQualityIncident))
    assert added_incident.status == "OPEN"
    assert added_incident.anomaly_type == "NOT_NULL_VIOLATION"
    assert added_incident.severity == "WARNING"
    assert added_incident.evidence["failures"] == 12


@pytest.mark.asyncio
async def test_reconcile_dbt_test_resolves_existing_incident() -> None:
    from unittest.mock import AsyncMock, MagicMock

    org_id = uuid4()
    ds_id = uuid4()
    table_id = uuid4()
    import_id = uuid4()

    table = MetadataTable(
        id=table_id,
        organization_id=org_id,
        schema_id=uuid4(),
        datasource_id=ds_id,
        name="customer_summary",
        object_type="TABLE",
        fingerprint="fp-table",
    )

    model_res = DbtResource(
        id=uuid4(),
        organization_id=org_id,
        artifact_import_id=import_id,
        unique_id="model.bank.customer_summary",
        resource_type="MODEL",
        package_name="bank",
        name="customer_summary",
        sql_parse_status="PARSED",
        column_names=["customer_id", "balance"],
        matched_table_id=table_id,
    )

    test_res = DbtResource(
        id=uuid4(),
        organization_id=org_id,
        artifact_import_id=import_id,
        unique_id="test.bank.not_null_customer_id",
        resource_type="TEST",
        package_name="bank",
        name="not_null_customer_id",
        sql_parse_status="PARSED",
        column_names=[],
        depends_on_unique_ids=["model.bank.customer_summary"],
        test_status="PASS",
        test_failures=0,
        test_execution_time=0.12,
    )

    existing_incident = DataQualityIncident(
        organization_id=org_id,
        datasource_id=ds_id,
        table_id=table_id,
        fingerprint="fp123",
        anomaly_type="NOT_NULL_VIOLATION",
        severity="WARNING",
        status="OPEN",
        summary="Prior failure",
        evidence={},
        first_observed_at=datetime.now(UTC),
        last_observed_at=datetime.now(UTC),
    )

    context = SecurityContext(
        principal_id="admin@bank.internal",
        principal_type="USER",
        roles=frozenset({"PlatformAdmin"}),
        organization_id=org_id,
    )

    session = AsyncMock()
    session.get = AsyncMock(return_value=table)
    session.scalar = AsyncMock(return_value=existing_incident)
    session.add = MagicMock()
    session.flush = AsyncMock()

    counts = await reconcile_dbt_test_quality(
        session,
        organization_id=org_id,
        datasource_id=ds_id,
        dbt_resources=[model_res, test_res],
        context=context,
    )

    assert counts["incidents_resolved"] == 1
    assert existing_incident.status == "RESOLVED"
    assert existing_incident.resolved_by == "SYSTEM_DBT_PASS"


