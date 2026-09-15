"""Execute synthetic fixtures in the local sample sources, then roll back all DDL/data.

Uses credentials already inside the containers; never prints or exports them.
No platform catalog ingestion, tool publication, or business-data modification.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/database_footprint"
COMMANDS = {
    "postgres": [
        "docker",
        "exec",
        "-i",
        "aida-platform-sample-source-1",
        "sh",
        "-c",
        'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    ],
    "sqlserver": [
        "docker",
        "exec",
        "-i",
        "aida-platform-sample-mssql-source-1",
        "sh",
        "-c",
        'export SQLCMDPASSWORD="${MSSQL_SA_PASSWORD:-$SA_PASSWORD}"; '
        "if [ -x /opt/mssql-tools18/bin/sqlcmd ]; then "
        "exec /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -d bank_demo_mssql -C -b; "
        "else exec /opt/mssql-tools/bin/sqlcmd -S localhost -U sa -d bank_demo_mssql -C -b; fi",
    ],
}

PG_ASSERTIONS = """
DO $$ BEGIN
    IF (SELECT count(*) FROM footprint_context_sample.customer_revenue) <> 2
       OR (SELECT net_revenue FROM footprint_context_sample.customer_revenue
           WHERE customer_id = 1) <> 150 THEN
        RAISE EXCEPTION 'view output mismatch';
    END IF;
    IF COALESCE((SELECT net_revenue FROM footprint_context_sample.customer_net(2)), -1) <> 70
       OR (SELECT count(*) FROM footprint_context_sample.customer_net(2, 100)) <> 0 THEN
        RAISE EXCEPTION 'overload output mismatch';
    END IF;
    IF (SELECT count(*) FROM footprint_context_sample.read_revenue()) <> 2 THEN
        RAISE EXCEPTION 'read function mismatch';
    END IF;
    IF (SELECT count(*) FROM footprint_context_sample.revenue_snapshot) <> 2 THEN
        RAISE EXCEPTION 'materialized view mismatch';
    END IF;
    IF (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='footprint_context_sample' AND p.proname='customer_net') <> 2 THEN
        RAISE EXCEPTION 'overload inventory mismatch';
    END IF;
END $$;
CALL footprint_context_sample.refresh_totals();
DO $$ BEGIN
    IF (SELECT count(*) FROM footprint_context_sample.customer_totals) <> 2
       OR (SELECT net_revenue FROM footprint_context_sample.customer_totals
           WHERE customer_id=1) <> 150
       OR (SELECT net_revenue FROM footprint_context_sample.customer_totals
           WHERE customer_id=2) <> 70 THEN
        RAISE EXCEPTION 'procedure output mismatch';
    END IF;
END $$;
SELECT 'FOOTPRINT_NATIVE_ASSERTIONS_PASSED';
ROLLBACK;
"""

MSSQL_ASSERTIONS = """
IF (SELECT COUNT(*) FROM footprint_context_sample.customer_revenue) <> 2
    THROW 51000, 'view row count mismatch', 1;
IF (SELECT net_revenue FROM footprint_context_sample.customer_revenue WHERE customer_id=1) <> 150
    THROW 51000, 'view value mismatch', 1;
IF COALESCE((SELECT net_revenue FROM footprint_context_sample.customer_net(2)), -1) <> 70
    THROW 51000, 'function output mismatch', 1;
CREATE TABLE #read_result (customer_id int, region varchar(20), net_revenue decimal(12,2));
INSERT INTO #read_result EXEC footprint_context_sample.read_revenue;
IF (SELECT COUNT(*) FROM #read_result) <> 2
    THROW 51000, 'read procedure output mismatch', 1;
EXEC footprint_context_sample.refresh_totals;
IF (SELECT COUNT(*) FROM footprint_context_sample.customer_totals) <> 2
    THROW 51000, 'refresh count mismatch', 1;
IF (SELECT net_revenue FROM footprint_context_sample.customer_totals WHERE customer_id=1) <> 150
    THROW 51000, 'refresh customer one mismatch', 1;
IF (SELECT net_revenue FROM footprint_context_sample.customer_totals WHERE customer_id=2) <> 70
    THROW 51000, 'refresh customer two mismatch', 1;
IF (SELECT COUNT(*) FROM sys.procedures WHERE schema_id=SCHEMA_ID('footprint_context_sample')) <> 4
    THROW 51000, 'routine inventory mismatch', 1;
PRINT 'FOOTPRINT_NATIVE_ASSERTIONS_PASSED';
ROLLBACK TRANSACTION;
GO
"""


def run_sql(engine: str, sql: str) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed local Docker commands, SQL via stdin
        COMMANDS[engine],
        input=sql,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        # Diagnostics contain only SQL from synthetic fixtures; command has no secret values.
        raise RuntimeError(f"{engine} verification failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main() -> None:
    results = {}
    for engine in ("postgres", "sqlserver"):
        version_sql = (
            "SELECT 'SOURCE_VERSION:' || current_setting('server_version');"
            if engine == "postgres"
            else (
                "SELECT 'SOURCE_VERSION:' + "
                "CONVERT(varchar(100), SERVERPROPERTY('ProductVersion'));"
            )
        )
        version_match = re.search(r"SOURCE_VERSION:([^\r\n]+)", run_sql(engine, version_sql))
        assert version_match is not None
        separator = "\nGO\n" if engine == "sqlserver" else "\n"
        names = ["setup", "view", "read", "function", "refresh", "dynamic", "nested"]
        if engine == "postgres":
            names.append("materialized")
        sql = separator.join(
            (FIXTURES / engine / f"{name}.sql").read_text(encoding="utf-8") for name in names
        )
        if engine == "postgres":
            sql = "BEGIN;\n" + sql + PG_ASSERTIONS
            cleanup = (
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_namespace "
                "WHERE nspname='footprint_context_sample') THEN 'LEAKED' "
                "ELSE 'FOOTPRINT_CLEANUP_PASSED' END;"
            )
        else:
            sql = "SET XACT_ABORT ON;\nBEGIN TRANSACTION;\nGO\n" + sql
            sql += separator + MSSQL_ASSERTIONS
            cleanup = (
                "SELECT CASE WHEN SCHEMA_ID('footprint_context_sample') IS NULL "
                "THEN 'FOOTPRINT_CLEANUP_PASSED' ELSE 'LEAKED' END;\nGO\n"
            )
        output = run_sql(engine, sql)
        assert "FOOTPRINT_NATIVE_ASSERTIONS_PASSED" in output
        assert "FOOTPRINT_CLEANUP_PASSED" in run_sql(engine, cleanup)
        results[engine] = {
            "engine_version": version_match.group(1).strip(),
            "verified_at": datetime.now(UTC).isoformat(),
            "native_assertions": "PASSED",
            "rollback_verified": True,
            "scope": "Synthetic native SQL in local sample container; no Atlas ingestion",
        }
        print(f"{engine}: native assertions and rollback verification passed", flush=True)
    path = FIXTURES / "live-results.json"
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
