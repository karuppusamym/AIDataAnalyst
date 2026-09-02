"""Minimal real-Airflow smoke DAG for Atlas OpenLineage ingestion.

Run with the documented `docker run ... airflow dags test` command in
`infra/airflow/README.md`.  The task produces one value-free COMPLETE event
from Airflow and sends it through Atlas's governed HTTP ingestion endpoint.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import UTC, datetime
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator


def emit_openlineage_event(**context: Any) -> None:
    """Send an Airflow-run event with table and column lineage evidence."""
    datasource_id = os.environ["ATLAS_DATASOURCE_ID"]
    organization_id = os.environ["ATLAS_ORGANIZATION_ID"]
    api_url = os.environ.get("ATLAS_API_URL", "http://api:8000").rstrip("/")
    dag_run = context["dag_run"]
    dag = context["dag"]
    run_id = str(dag_run.run_id)
    dag_id = str(dag.dag_id)
    namespace = "postgres://sample-source:5432"
    event = {
        "eventType": "COMPLETE",
        "eventTime": datetime.now(UTC).isoformat(),
        "producer": "https://github.com/OpenLineage/OpenLineage/tree/1.24.0/integration/airflow",
        "schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/definitions/RunEvent",
        "job": {"namespace": "airflow", "name": dag_id},
        "run": {"runId": run_id},
        "inputs": [
            {
                "namespace": namespace,
                "name": "bank_demo.retail.customer",
                "facets": {"schema": {"fields": [{"name": "customer_id"}, {"name": "email"}]}},
            }
        ],
        "outputs": [
            {
                "namespace": namespace,
                "name": "bank_demo.risk.customer_risk_snapshot",
                "facets": {
                    "schema": {"fields": [{"name": "customer_id"}, {"name": "risk_score"}]},
                    "columnLineage": {
                        "fields": {
                            "customer_id": {
                                "inputFields": [
                                    {
                                        "namespace": namespace,
                                        "name": "bank_demo.retail.customer",
                                        "field": "customer_id",
                                        "transformations": [{"type": "IDENTITY"}],
                                    }
                                ]
                            }
                        }
                    },
                },
            }
        ],
    }
    payload = json.dumps({"datasource_id": datasource_id, "event": event}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 -- configured local Atlas endpoint
        f"{api_url}/v1/lineage/openlineage",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Principal-Id": "airflow-openlineage-smoke",
            "X-Organization-Id": organization_id,
            "X-Roles": "MetadataIngestor",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- local Atlas URL
        if response.status not in {200, 201}:
            raise RuntimeError(f"Atlas OpenLineage ingest returned HTTP {response.status}")
        body = json.loads(response.read().decode("utf-8"))
    if body.get("table_edge_count") != 1 or body.get("column_edge_count") != 1:
        raise RuntimeError("Atlas did not persist the expected table and column lineage edges")


with DAG(
    dag_id="atlas_openlineage_smoke",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    schedule=None,
    catchup=False,
    tags=["atlas", "openlineage", "smoke"],
) as dag:
    PythonOperator(task_id="emit_openlineage_event", python_callable=emit_openlineage_event)
