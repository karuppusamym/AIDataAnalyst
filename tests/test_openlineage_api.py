from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida.models import (
    AuditEvent,
    DataSource,
    OpenLineageColumnEdge,
    OpenLineageDataset,
    OpenLineageRunEvent,
    OpenLineageTableEdge,
    OrganizationIntegrationPolicy,
    OutboxEvent,
)
from aida.openlineage_api import (
    get_openlineage_run_event,
    ingest_openlineage_run_event,
    list_openlineage_run_events,
)
from aida.schemas import OpenLineageIngestRequest
from aida.security_types import SecurityContext


def make_context(
    organization_id: UUID | None, roles: frozenset[str] = frozenset({"MetadataIngestor"})
) -> SecurityContext:
    return SecurityContext(
        principal_id="tester@example.com",
        principal_type="USER",
        organization_id=organization_id,
        roles=roles,
    )


def make_policy(
    organization_id: UUID, openlineage_enabled: bool = True
) -> OrganizationIntegrationPolicy:
    return OrganizationIntegrationPolicy(
        id=uuid4(),
        organization_id=organization_id,
        transformation_metadata_integrations={"openlineage": openlineage_enabled},
    )


def make_session(
    *,
    scalar_results: list[Any],
    execute_result_rows: list[Any] | None = None,
) -> MagicMock:
    """Build a MagicMock standing in for AsyncSession.

    - `session.scalar(...)` returns successive entries from `scalar_results`,
      matching call order in the handler (integration-policy lookup, then any
      fingerprint-dedup lookup).
    - `session.execute(...)` (the catalog-match join query) returns an object
      whose `.all()` yields `execute_result_rows`.
    - `session.add` records everything it is given onto `session.added`.
    - `session.flush`/`session.commit`/`session.refresh` are no-op AsyncMocks.
    """
    session = MagicMock()
    session.added: list[Any] = []
    session.add = MagicMock(side_effect=session.added.append)
    session.scalar = AsyncMock(side_effect=list(scalar_results))

    execute_result = MagicMock()
    execute_result.all.return_value = execute_result_rows or []
    session.execute = AsyncMock(return_value=execute_result)

    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.get = AsyncMock()
    session.scalars = AsyncMock()
    return session


def valid_event_payload() -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-01-15T12:30:00Z",
        "producer": "https://github.com/apache/airflow",
        "job": {"namespace": "airflow", "name": "orders_etl"},
        "run": {"runId": "d46e465b-d358-4d32-83d4-df660ff614dd"},
        "inputs": [
            {"namespace": "snowflake://acct", "name": "raw.orders"},
            {"namespace": "snowflake://acct", "name": "raw.customers"},
        ],
        "outputs": [
            {
                "namespace": "snowflake://acct",
                "name": "analytics.order_summary",
                "facets": {
                    "columnLineage": {
                        "fields": {
                            "total_amount": {
                                "inputFields": [
                                    {
                                        "namespace": "snowflake://acct",
                                        "name": "raw.orders",
                                        "field": "amount",
                                        "transformations": [
                                            {"type": "AGGREGATION", "subtype": "SUM"}
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Ingest: happy path -- persistence shape/count + audit + outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_persists_datasets_edges_and_records_audit_outbox() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id)

    session = make_session(
        scalar_results=[make_policy(org_id), None],  # policy enabled, no existing fingerprint
    )
    session.get.return_value = datasource

    body = OpenLineageIngestRequest(datasource_id=datasource_id, event=valid_event_payload())

    with patch(
        "aida.openlineage_api._event_read", new=AsyncMock(return_value="EVENT_READ_SENTINEL")
    ) as event_read:
        result = await ingest_openlineage_run_event(body, context=context, session=session)

    assert result == "EVENT_READ_SENTINEL"

    run_events = [obj for obj in session.added if isinstance(obj, OpenLineageRunEvent)]
    datasets = [obj for obj in session.added if isinstance(obj, OpenLineageDataset)]
    table_edges = [obj for obj in session.added if isinstance(obj, OpenLineageTableEdge)]
    column_edges = [obj for obj in session.added if isinstance(obj, OpenLineageColumnEdge)]
    audit_events = [obj for obj in session.added if isinstance(obj, AuditEvent)]
    outbox_events = [obj for obj in session.added if isinstance(obj, OutboxEvent)]

    assert len(run_events) == 1
    event = run_events[0]
    assert event.organization_id == org_id
    assert event.datasource_id == datasource_id
    assert event.input_dataset_count == 2
    assert event.output_dataset_count == 1
    assert event.table_edge_count == 2  # 2 inputs x 1 output
    assert event.column_edge_count == 1
    assert event.unresolved_dataset_count == 3  # no catalog rows -> nothing matched
    assert event.imported_by == context.principal_id

    assert len(datasets) == 3
    assert {d.direction for d in datasets} == {"INPUT", "OUTPUT"}
    assert sum(1 for d in datasets if d.direction == "INPUT") == 2
    assert sum(1 for d in datasets if d.direction == "OUTPUT") == 1
    assert all(d.run_event_id == event.id for d in datasets)
    assert all(d.matched_table_id is None for d in datasets)

    assert len(table_edges) == 2
    assert {(e.input_dataset_name, e.output_dataset_name) for e in table_edges} == {
        ("raw.orders", "analytics.order_summary"),
        ("raw.customers", "analytics.order_summary"),
    }
    assert all(e.run_event_id == event.id for e in table_edges)

    assert len(column_edges) == 1
    column_edge = column_edges[0]
    assert column_edge.input_dataset_name == "raw.orders"
    assert column_edge.input_column_name == "amount"
    assert column_edge.output_dataset_name == "analytics.order_summary"
    assert column_edge.output_column_name == "total_amount"
    assert column_edge.transformation_type == "AGGREGATION"
    assert column_edge.transformation_subtype == "SUM"
    assert column_edge.run_event_id == event.id

    assert len(audit_events) == 1
    assert audit_events[0].action == "openlineage_run_event.import"
    assert audit_events[0].resource_type == "openlineage_run_event"
    assert audit_events[0].outcome == "SUCCESS"

    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == "openlineage.run_event.ingested.v1"
    assert outbox_events[0].aggregate_type == "openlineage_run_event"
    assert outbox_events[0].payload["table_edge_count"] == 2
    assert outbox_events[0].payload["column_edge_count"] == 1

    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()
    event_read.assert_awaited_once_with(session, event)


@pytest.mark.asyncio
async def test_ingest_resolves_matched_table_ids_from_catalog() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    matched_table_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id)

    table_row = MagicMock(id=matched_table_id)
    table_row.name = "order_summary"
    schema_row = MagicMock()
    schema_row.name = "analytics"
    catalog_row = MagicMock()
    catalog_row.name = "snowflake"

    session = make_session(
        scalar_results=[make_policy(org_id), None],
        execute_result_rows=[(table_row, schema_row, catalog_row)],
    )
    session.get.return_value = datasource

    body = OpenLineageIngestRequest(datasource_id=datasource_id, event=valid_event_payload())

    with patch("aida.openlineage_api._event_read", new=AsyncMock(return_value="SENTINEL")):
        await ingest_openlineage_run_event(body, context=context, session=session)

    datasets = [obj for obj in session.added if isinstance(obj, OpenLineageDataset)]
    output_dataset = next(d for d in datasets if d.direction == "OUTPUT")
    input_datasets = [d for d in datasets if d.direction == "INPUT"]

    assert output_dataset.matched_table_id == matched_table_id
    assert all(d.matched_table_id is None for d in input_datasets)

    table_edges = [obj for obj in session.added if isinstance(obj, OpenLineageTableEdge)]
    for edge in table_edges:
        assert edge.output_table_id == matched_table_id
        assert edge.input_table_id is None

    run_event = next(obj for obj in session.added if isinstance(obj, OpenLineageRunEvent))
    assert run_event.unresolved_dataset_count == 2  # only the 2 inputs remain unmatched


# ---------------------------------------------------------------------------
# Ingest: rejection of invalid events, integration-disabled, cross-org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_rejects_invalid_event_with_422() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id)

    session = make_session(scalar_results=[make_policy(org_id)])
    session.get.return_value = datasource

    invalid_event = valid_event_payload()
    del invalid_event["job"]
    body = OpenLineageIngestRequest(datasource_id=datasource_id, event=invalid_event)

    with pytest.raises(HTTPException) as exc_info:
        await ingest_openlineage_run_event(body, context=context, session=session)

    assert exc_info.value.status_code == 422
    assert "job" in exc_info.value.detail
    assert session.added == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_rejects_when_integration_disabled() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id)

    session = make_session(scalar_results=[make_policy(org_id, openlineage_enabled=False)])
    session.get.return_value = datasource

    body = OpenLineageIngestRequest(datasource_id=datasource_id, event=valid_event_payload())

    with pytest.raises(HTTPException) as exc_info:
        await ingest_openlineage_run_event(body, context=context, session=session)

    assert exc_info.value.status_code == 403
    assert session.added == []


@pytest.mark.asyncio
async def test_ingest_rejects_cross_organization_access() -> None:
    datasource_org = uuid4()
    caller_org = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=datasource_org)
    context = make_context(caller_org, roles=frozenset({"MetadataIngestor"}))

    session = make_session(scalar_results=[])
    session.get.return_value = datasource

    body = OpenLineageIngestRequest(datasource_id=datasource_id, event=valid_event_payload())

    with pytest.raises(HTTPException) as exc_info:
        await ingest_openlineage_run_event(body, context=context, session=session)

    assert exc_info.value.status_code == 403
    assert session.added == []


@pytest.mark.asyncio
async def test_ingest_returns_404_when_datasource_missing() -> None:
    context = make_context(uuid4())
    session = make_session(scalar_results=[])
    session.get.return_value = None

    body = OpenLineageIngestRequest(datasource_id=uuid4(), event=valid_event_payload())

    with pytest.raises(HTTPException) as exc_info:
        await ingest_openlineage_run_event(body, context=context, session=session)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Ingest: idempotency by fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_is_idempotent_and_does_not_duplicate_rows_on_replay() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id)

    existing_event = OpenLineageRunEvent(
        id=uuid4(),
        organization_id=org_id,
        datasource_id=datasource_id,
        event_fingerprint="deadbeef",
        event_type="COMPLETE",
        event_time=datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC),
        producer="https://github.com/apache/airflow",
        schema_url=None,
        job_namespace="airflow",
        job_name="orders_etl",
        run_id="d46e465b-d358-4d32-83d4-df660ff614dd",
        input_dataset_count=2,
        output_dataset_count=1,
        table_edge_count=2,
        column_edge_count=1,
        unresolved_dataset_count=3,
        imported_by=context.principal_id,
    )

    session = make_session(scalar_results=[make_policy(org_id), existing_event])
    session.get.return_value = datasource

    body = OpenLineageIngestRequest(datasource_id=datasource_id, event=valid_event_payload())

    with patch(
        "aida.openlineage_api._event_read", new=AsyncMock(return_value="EXISTING_SENTINEL")
    ) as event_read:
        result = await ingest_openlineage_run_event(body, context=context, session=session)

    assert result == "EXISTING_SENTINEL"
    # No new ORM rows created on replay of an identical event.
    assert session.added == []
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    event_read.assert_awaited_once_with(session, existing_event)


# ---------------------------------------------------------------------------
# List / get endpoints
# ---------------------------------------------------------------------------


def make_run_event(
    *, organization_id: UUID, datasource_id: UUID, fingerprint: str = "abc123"
) -> OpenLineageRunEvent:
    now = datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC)
    return OpenLineageRunEvent(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        event_fingerprint=fingerprint,
        event_type="COMPLETE",
        event_time=now,
        producer="https://github.com/apache/airflow",
        schema_url=None,
        job_namespace="airflow",
        job_name="orders_etl",
        run_id="d46e465b-d358-4d32-83d4-df660ff614dd",
        status="IMPORTED",
        input_dataset_count=2,
        output_dataset_count=1,
        table_edge_count=2,
        column_edge_count=1,
        unresolved_dataset_count=0,
        imported_by="tester@example.com",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_list_openlineage_run_events_filters_by_datasource() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id, roles=frozenset({"Viewer"}))

    rows = [
        make_run_event(organization_id=org_id, datasource_id=datasource_id, fingerprint="fp-1"),
        make_run_event(organization_id=org_id, datasource_id=datasource_id, fingerprint="fp-2"),
    ]

    session = make_session(scalar_results=[make_policy(org_id), 2])
    session.get.return_value = datasource
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    session.scalars = AsyncMock(return_value=scalars_result)

    page = await list_openlineage_run_events(
        datasource_id, limit=100, offset=0, context=context, session=session
    )

    assert page.total == 2
    assert len(page.items) == 2
    assert {item.event_fingerprint for item in page.items} == {"fp-1", "fp-2"}
    assert all(item.datasource_id == datasource_id for item in page.items)


@pytest.mark.asyncio
async def test_list_openlineage_run_events_empty_result() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=org_id)
    context = make_context(org_id, roles=frozenset({"Viewer"}))

    session = make_session(scalar_results=[make_policy(org_id), 0])
    session.get.return_value = datasource
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    session.scalars = AsyncMock(return_value=scalars_result)

    page = await list_openlineage_run_events(
        datasource_id, limit=100, offset=0, context=context, session=session
    )

    assert page.total == 0
    assert page.items == []


@pytest.mark.asyncio
async def test_list_openlineage_run_events_rejects_cross_organization_access() -> None:
    datasource_org = uuid4()
    caller_org = uuid4()
    datasource_id = uuid4()
    datasource = DataSource(id=datasource_id, organization_id=datasource_org)
    context = make_context(caller_org, roles=frozenset({"Viewer"}))

    session = make_session(scalar_results=[])
    session.get.return_value = datasource

    with pytest.raises(HTTPException) as exc_info:
        await list_openlineage_run_events(
            datasource_id, limit=100, offset=0, context=context, session=session
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_openlineage_run_event_returns_event_with_edges() -> None:
    org_id = uuid4()
    datasource_id = uuid4()
    event = make_run_event(organization_id=org_id, datasource_id=datasource_id)
    now = datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC)

    dataset = OpenLineageDataset(
        id=uuid4(),
        organization_id=org_id,
        run_event_id=event.id,
        direction="OUTPUT",
        namespace="snowflake://acct",
        name="analytics.order_summary",
        matched_table_id=None,
        schema_fields=["total_amount"],
        created_at=now,
        updated_at=now,
    )
    table_edge = OpenLineageTableEdge(
        id=uuid4(),
        organization_id=org_id,
        run_event_id=event.id,
        input_dataset_namespace="snowflake://acct",
        input_dataset_name="raw.orders",
        input_table_id=None,
        output_dataset_namespace="snowflake://acct",
        output_dataset_name="analytics.order_summary",
        output_table_id=None,
        edge_kind="ETL",
        created_at=now,
        updated_at=now,
    )
    column_edge = OpenLineageColumnEdge(
        id=uuid4(),
        organization_id=org_id,
        run_event_id=event.id,
        input_dataset_namespace="snowflake://acct",
        input_dataset_name="raw.orders",
        input_table_id=None,
        input_column_name="amount",
        output_dataset_namespace="snowflake://acct",
        output_dataset_name="analytics.order_summary",
        output_table_id=None,
        output_column_name="total_amount",
        transformation_type="AGGREGATION",
        transformation_subtype="SUM",
        edge_kind="ETL",
        created_at=now,
        updated_at=now,
    )

    context = make_context(org_id, roles=frozenset({"Viewer"}))
    session = make_session(scalar_results=[make_policy(org_id)])
    session.get.return_value = event

    dataset_scalars = MagicMock()
    dataset_scalars.all.return_value = [dataset]
    table_edge_scalars = MagicMock()
    table_edge_scalars.all.return_value = [table_edge]
    column_edge_scalars = MagicMock()
    column_edge_scalars.all.return_value = [column_edge]
    session.scalars = AsyncMock(
        side_effect=[dataset_scalars, table_edge_scalars, column_edge_scalars]
    )

    result = await get_openlineage_run_event(event.id, context=context, session=session)

    assert result.id == event.id
    assert result.event_fingerprint == event.event_fingerprint
    assert len(result.datasets) == 1
    assert result.datasets[0].name == "analytics.order_summary"
    assert len(result.table_edges) == 1
    assert result.table_edges[0].input_dataset_name == "raw.orders"
    assert len(result.column_edges) == 1
    assert result.column_edges[0].transformation_type == "AGGREGATION"


@pytest.mark.asyncio
async def test_get_openlineage_run_event_returns_404_when_missing() -> None:
    context = make_context(uuid4(), roles=frozenset({"Viewer"}))
    session = make_session(scalar_results=[])
    session.get.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await get_openlineage_run_event(uuid4(), context=context, session=session)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_openlineage_run_event_rejects_cross_organization_access() -> None:
    event_org = uuid4()
    caller_org = uuid4()
    event = make_run_event(organization_id=event_org, datasource_id=uuid4())
    context = make_context(caller_org, roles=frozenset({"Viewer"}))

    session = make_session(scalar_results=[])
    session.get.return_value = event

    with pytest.raises(HTTPException) as exc_info:
        await get_openlineage_run_event(event.id, context=context, session=session)

    assert exc_info.value.status_code == 403
