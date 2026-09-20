"""Read-only sweep of the running stack: every GET route, as each role, looking for a server error.

Why it exists (tracker R11-D31, R11-D32, R11-D33). Every unit test of a list route runs on an
in-memory database as a caller holding every role, and a response model is only built from a real
row when a row exists. Three routes answered HTTP 500 for whole classes of caller on the deployed
stack -- a query PostgreSQL rejects and SQLite accepts (Analyst and Viewer only), a response built
from every column of a table, and a required field a nullable column does not fill -- and no test
saw any of them. Calling every read route as each least-privilege role, against real rows, did.

    python scripts/live_role_sweep.py [OUT.json]

What it does and does not do:

* It only sends GET requests, with a development identity per role (`X-Roles`), so it needs a stack
  that trusts those headers (the local development stack). It writes nothing.
* Path and query ids are looked up in the development database by `docker exec ... psql`. A route
  whose ids cannot be found -- almost always because its table holds no row -- is reported as not
  covered, never counted as clean: a 5xx from building a response out of a real row cannot be
  triggered where there is no row.
* It never calls a route that takes `q` or `question` (search and Ask may reach an embedding or
  model provider, which costs money), and it skips routes that write when read or that stream
  (compile, export, download, consumption reads, event streams, the MCP endpoint).
* 2xx, 403 and 404 are all expected: a role that may not read a route is refused. Only 5xx and
  transport failures are findings.

Override the container or the API address with AIDA_SWEEP_POSTGRES and AIDA_SWEEP_BASE_URL.
"""

# The SQL in this file is built from its own constants and from ids read back out of the
# development database, and runs read-only through `docker exec ... psql`; nothing in it takes
# input from a caller.
# ruff: noqa: S608
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Any

BASE = os.environ.get("AIDA_SWEEP_BASE_URL", "http://localhost:8000")
POSTGRES = os.environ.get("AIDA_SWEEP_POSTGRES", "aida-platform-postgres-1")
PSQL = ["docker", "exec", POSTGRES, "psql", "-U", "aida", "-d", "aida", "-tAc"]
if not BASE.startswith(("http://", "https://")):
    sys.exit(f"AIDA_SWEEP_BASE_URL must be an http(s) URL, not {BASE!r}")
ORG_SLUG = "sample-bank"
DATASOURCE_PREFIX = "Customer Master"

ROLES = [
    "PlatformAdmin",
    "Analyst",
    "Viewer",
    "DataSteward",
    "MetadataReviewer",
    "Reviewer",
    "Auditor",
    "DataAdmin",
    "MetadataAdmin",
    "Operations",
    "AgentDeveloper",
    "SemanticAdmin",
]

# Routes that write when read, or stream, or are not plain reads.
SKIP = re.compile(
    r"compile|/export|download|consumption|okf-bundle|/context-product-versions/\{version_id\}$"
    r"|/scope$|okf-knowledge|/prompts|/mcp|/stream|/events|/reaper|/health|/metrics|/ready",
    re.IGNORECASE,
)
# Search and Ask may reach an embedding or model provider (paid): never call them.
NEVER_PARAMS = {"q", "question"}

# Path parameter -> a table-name fragment; the first table containing it with a row supplies an id.
TABLE_KEYS = {
    "change_set_id": "change_set",
    "artifact_id": "artifact_import",
    "document_id": "document",
    "node_id": "business_node",
    "lob_id": "line_of_business",
    "contract_id": "data_contract",
    "plan_id": "tool_plan",
    "model_id": "semantic_model_version",
    "metric_id": "semantic_metric",
    "term_id": "glossary_term",
    "family_candidate_id": "table_family_candidate",
    "ai_asset_version_id": "ai_asset_version",
    "sample_id": "reviewer_agent_sample",
    "request_id": "agent_contract_request",
    "dbt_project_id": "dbt_project",
    "event_id": "openlineage_event",
    "connection_id": "bi_connection",
    "incident_id": "data_quality_incident",
    "rule_pack_id": "quality_rule_pack",
    "report_id": "access_review_report",
    "asset_id": "ai_asset",
    "pack_id": "compliance_pack",
    "task_id": "analysis_task",
    "group_id": "composite_relationship",
}


def sql_lines(query: str) -> list[str]:
    out = subprocess.run(  # noqa: S603 -- a fixed docker exec command; the query is this file's own
        PSQL + [query], capture_output=True, text=True, check=False
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def sql(query: str) -> str | None:
    lines = sql_lines(query)
    return lines[0] if lines else None


def resolve_ids() -> tuple[dict[str, str], str, dict[str, str]]:
    org = sql(f"select id from organization where slug='{ORG_SLUG}'")
    if not org:
        sys.exit(
            f"no organization {ORG_SLUG!r}: is the development estate seeded, and is {POSTGRES} up?"
        )
    ds = sql(
        f"select id from datasource where organization_id='{org}' "
        f"and name like '{DATASOURCE_PREFIX}%' limit 1"
    )
    by_org = lambda table: sql(f"select id from {table} where organization_id='{org}' limit 1")  # noqa: E731
    ids: dict[str, str | None] = {
        "organization_id": org,
        "datasource_id": ds,
        "project_id": by_org("project"),
        "table_id": sql(
            f"select id from metadata_table where datasource_id='{ds}' and status='ACTIVE' limit 1"
        ),
        "column_id": sql(
            "select c.id from metadata_column c join metadata_table t on t.id=c.table_id "
            f"where t.datasource_id='{ds}' limit 1"
        ),
        "routine_id": sql(
            "select id from metadata_routine "
            f"where datasource_id='{ds}' and status='ACTIVE' limit 1"
        ),
        "schema_id": sql(
            "select s.id from metadata_schema s join metadata_catalog c on c.id=s.catalog_id "
            f"where c.datasource_id='{ds}' limit 1"
        ),
        "catalog_id": sql(f"select id from metadata_catalog where datasource_id='{ds}' limit 1"),
        "product_id": by_org("context_product"),
        "version_id": by_org("context_product_version"),
        "review_id": by_org("governance_review"),
        "domain_id": by_org("data_domain"),
        "line_of_business_id": by_org("line_of_business"),
        "workspace_id": by_org("workspace"),
        "candidate_id": by_org("relationship_candidate"),
        "draft_id": by_org("asset_description_draft"),
        "agent_run_id": by_org("agent_run"),
        "trigger_id": sql(f"select id from metadata_trigger where datasource_id='{ds}' limit 1"),
        "playbook_id": by_org("playbook"),
        "batch_id": by_org("review_batch"),
        "receipt_id": by_org("sql_draft_receipt"),
        "execution_id": by_org("query_execution"),
        "tool_id": by_org("governed_tool"),
        "tool_version_id": by_org("governed_tool_version"),
        "run_id": by_org("analysis_run"),
        "publication_id": sql("select id from okf_bundle_publication limit 1"),
    }
    ids["focus_table_id"] = ids["table_id"]
    ids["subject_id"] = ids["table_id"]  # a negative-knowledge subject is keyed by a table here
    ids["period_start"] = "2026-09-01"
    ids["period_end"] = "2026-09-20"

    tables = set(
        sql_lines("select table_name from information_schema.tables where table_schema='public'")
    )
    resolved_from: dict[str, str] = {}
    for param, key in TABLE_KEYS.items():
        candidates = sorted(
            (t for t in tables if key in t), key=lambda t: (t != key, not t.endswith(key), len(t))
        )
        for table in candidates:
            value = sql(f'select id from "{table}" limit 1')
            if value:
                ids[param] = value
                resolved_from[param] = table
                break
    return {k: v for k, v in ids.items() if v}, org, resolved_from


def enum_value(operation: dict[str, Any], name: str) -> str | None:
    for parameter in operation.get("parameters", []):
        if parameter["name"] == name and parameter.get("schema", {}).get("enum"):
            return str(parameter["schema"]["enum"][0])
    return None


def fill(
    path: str, operation: dict[str, Any], ids: dict[str, str]
) -> tuple[str | None, str | None]:
    """The URL to call, or (None, the reason it cannot be called)."""
    url = path
    for name in re.findall(r"\{([^}]+)\}", path):
        value = ids.get(name) or enum_value(operation, name)
        if not value:
            return None, f"no id for {name}"
        url = url.replace("{" + name + "}", value)
    query = []
    for parameter in operation.get("parameters", []):
        if parameter["in"] == "query" and parameter.get("required"):
            if parameter["name"] in NEVER_PARAMS:
                return None, f"{parameter['name']} (search or Ask: may reach a paid provider)"
            value = ids.get(parameter["name"])
            if value is None:
                return None, f"no value for query {parameter['name']}"
            query.append(f"{parameter['name']}={value}")
    return url + ("?" + "&".join(query) if query else ""), None


def call(url: str, role: str, org: str) -> tuple[int, str]:
    request = urllib.request.Request(  # noqa: S310 -- scheme checked above
        BASE + url,
        headers={
            "X-Principal-Id": f"role-sweep-{role.lower()}",
            "X-Principal-Type": "USER",
            "X-Roles": role,
            "X-Organization-Id": org,
            "X-Business-Purpose": "read-only role sweep",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:  # noqa: S310 -- scheme checked above
            return response.status, ""
    except urllib.error.HTTPError as error:
        return error.code, ""
    except Exception as error:  # noqa: BLE001 -- a transport failure is a finding, not a crash
        return -1, str(error)[:80]


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "role_sweep.json"
    ids, org, resolved_from = resolve_ids()
    spec = json.load(
        urllib.request.urlopen(f"{BASE}/openapi.json", timeout=30)  # noqa: S310 -- scheme checked above
    )
    status: dict[str, dict[str, int]] = defaultdict(dict)
    findings: list[tuple[str, str, int, str]] = []
    not_covered: dict[str, str] = {}
    skipped = gets = 0
    for path, item in spec["paths"].items():
        operation = item.get("get")
        if not operation:
            continue
        gets += 1
        if SKIP.search(path):
            skipped += 1
            continue
        url, why = fill(path, operation, ids)
        if url is None:
            not_covered[path] = why or "unresolved"
            continue
        for role in ROLES:
            code, detail = call(url, role, org)
            status[path][role] = code
            if code >= 500 or code == -1:
                findings.append((path, role, code, detail))

    calls = sum(len(v) for v in status.values())
    print(
        f"{gets} GET routes: {len(status)} called as {len(ROLES)} roles ({calls} calls), "
        f"{skipped} skipped on purpose, {len(not_covered)} not covered"
    )
    print(
        "status distribution:",
        dict(sorted(Counter(c for v in status.values() for c in v.values()).items())),
    )
    print(f"5xx / transport failures: {len(findings)}")
    by_route: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path, role, code, _ in findings:
        by_route[path].append((role, code))
    for path, hits in sorted(by_route.items()):
        served = [r for r, c in status[path].items() if c < 400]
        refused_with = ", ".join(f"{r}={c}" for r, c in hits)
        print(f"  {path}\n     5xx for: {refused_with}\n     2xx for: {served}")
    if not_covered:
        reasons = Counter(not_covered.values())
        print("not covered (their tables are usually empty in the development estate):")
        for reason, count in reasons.most_common():
            print(f"  {count:3d}  {reason}")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "status": status,
                "findings": findings,
                "not_covered": not_covered,
                "resolved_from": resolved_from,
            },
            handle,
            indent=1,
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
