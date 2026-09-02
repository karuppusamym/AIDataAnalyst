# Airflow OpenLineage smoke proof

This isolated command runs a real Apache Airflow DAG once. The DAG emits a
value-free OpenLineage `COMPLETE` event to Atlas and requires the target
organization's OpenLineage integration to be enabled.

From the repository root, set `ATLAS_DATASOURCE_ID` and
`ATLAS_ORGANIZATION_ID` for an active sample datasource, then run:

```powershell
docker run --rm --network aida-platform_default `
  -v "${PWD}/infra/airflow/dags:/opt/airflow/dags:ro" `
  -e AIRFLOW__CORE__LOAD_EXAMPLES=False `
  -e ATLAS_API_URL=http://api:8000 `
  -e ATLAS_DATASOURCE_ID `
  -e ATLAS_ORGANIZATION_ID `
  apache/airflow:2.10.5-python3.12 airflow dags test atlas_openlineage_smoke 2026-09-02
```

The task fails unless Atlas returns an event containing exactly one table edge
and one column edge. This is a smoke proof, not a production Airflow topology.
