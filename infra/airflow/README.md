# Airflow OpenLineage smoke proof — a hand-run test, not an Airflow deployment

> **What this is not (R11-X10, 2026-09-11).** One DAG file, run by hand, to prove once
> that Atlas ingests a real Airflow-produced OpenLineage event. There is no Airflow
> deployment here and none is implied: no scheduler, no webserver, no triggerer, no
> metadata database, no `docker-compose`/Helm/manifest of any kind, and nothing in
> `compose.yaml` or `infra/k8s/` starts Airflow. The `airflow dags test` command below
> runs the task in-process in a throwaway container with Airflow's default SQLite
> metadata DB and exits.
>
> The DAG is a fixture, not a pipeline. It posts one hardcoded event describing two
> tables of the sample banking estate (`bank_demo.retail.customer` →
> `bank_demo.risk.customer_risk_snapshot`), authenticates with the development
> `X-Principal-Id` / `X-Roles` headers that `AIDA_IDENTITY_PROVIDER=oidc` rejects, and
> reads nothing from any real warehouse. Producing lineage from actual Airflow runs is
> the OpenLineage provider's job (`apache-airflow-providers-openlineage`), which this
> repository neither installs nor configures.

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
