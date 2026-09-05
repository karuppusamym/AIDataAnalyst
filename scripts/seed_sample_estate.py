#!/usr/bin/env python3
"""Seed a small, value-free sample banking estate through the governed API.

A fresh install has no metadata, so the catalog, knowledge graph, unified
lineage and analyst surfaces all render empty and the platform reads as
"missing" rather than "not yet populated". This script populates a realistic
retail-and-risk estate so those surfaces have something to show on day one.

It talks only to the public HTTP API using development identity headers and
the canonical value-free metadata-ingestion envelope (envelope 1.0). It pushes
*structure only* — catalogs, schemas, tables, columns, and PK/FK constraints —
never business row values, so it honours the value-free control-plane invariant
(ADR-0014). After ingestion it discovers relationship candidates from the
declared foreign keys and approves them, because Unified Lineage shows APPROVED
suggestions by default.

The script is idempotent: every tenancy object is looked up before it is
created, ingestion is deduplicated by a stable idempotency key, and only
PENDING relationship candidates are approved. Re-running it is safe.

Usage:
    python scripts/seed_sample_estate.py
    AIDA_BASE_URL=http://api:8000 python scripts/seed_sample_estate.py

Environment:
    AIDA_BASE_URL   API base URL (default http://localhost:8000)
    AIDA_SEED_SLUG  Organization slug to create/reuse (default sample-bank)

Only ever point this at a development or demonstration environment. It creates
an organization and registers a datasource; it is not intended for production.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

BASE_URL = os.environ.get("AIDA_BASE_URL", "http://localhost:8000").rstrip("/")
ORG_SLUG = os.environ.get("AIDA_SEED_SLUG", "sample-bank")

# Every role, because the seed exercises tenancy, ingestion, relationship
# discovery and maker-checker approval in one pass. This is a development-only
# identity; production rejects header identities entirely.
BOOTSTRAP_ROLES = (
    "PlatformAdmin,MetadataAdmin,DataAdmin,MetadataIngestor,SemanticAdmin,"
    "DataSteward,MetadataReviewer,Auditor,Operations,Analyst,Viewer"
)
BASE_HEADERS = {
    "X-Principal-Id": "sample-estate-seed",
    "X-Principal-Type": "USER",
    "X-Roles": BOOTSTRAP_ROLES,
    "X-Business-Purpose": "Seed demonstration estate for local evaluation",
    "Content-Type": "application/json",
}


class SeedError(RuntimeError):
    """A non-recoverable failure while seeding."""


def _request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    org_id: str | None = None,
    expect: tuple[int, ...] = (200, 201),
) -> Any:
    url = f"{BASE_URL}{path}"
    # The scheme is validated here; the seed only ever calls a first-party
    # development API, so the S310 URL-audit warnings below are suppressed.
    if not url.startswith(("http://", "https://")):
        raise SeedError(f"refusing to open non-HTTP URL: {url}")
    headers = dict(BASE_HEADERS)
    if org_id:
        headers["X-Organization-Id"] = org_id
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else None
            return response.status, payload
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code in expect:
            payload = json.loads(detail) if detail else None
            return error.code, payload
        raise SeedError(f"{method} {path} -> HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise SeedError(f"{method} {path} -> {error.reason}") from error


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    if isinstance(payload, list):
        return payload
    return []


def wait_for_ready(attempts: int = 60) -> None:
    for attempt in range(1, attempts + 1):
        try:
            status, _ = _request("GET", "/health/ready", expect=(200, 503))
            if status == 200:
                return
        except SeedError:
            pass
        if attempt == 1:
            print(f"Waiting for the API at {BASE_URL} to become ready...")
        time.sleep(2)
    raise SeedError(f"API at {BASE_URL} did not become ready in time")


def _find(path: str, key: str, value: str, *, org_id: str | None = None) -> dict[str, Any] | None:
    _, payload = _request("GET", path, org_id=org_id)
    for item in _items(payload):
        if item.get(key) == value:
            return item
    return None


def ensure_organization() -> dict[str, Any]:
    existing = _find("/v1/organizations?limit=200", "slug", ORG_SLUG)
    if existing:
        print(f"  organization '{existing['name']}' already exists")
        return existing
    _, org = _request(
        "POST",
        "/v1/organizations",
        {"name": "Northwind Retail Bank (sample)", "slug": ORG_SLUG},
    )
    print(f"  created organization '{org['name']}'")
    return org


def ensure_line_of_business(org_id: str) -> dict[str, Any]:
    path = f"/v1/organizations/{org_id}/lines-of-business"
    existing = _find(f"{path}?limit=200", "code", "RETAIL", org_id=org_id)
    if existing:
        return existing
    _, lob = _request("POST", path, {"name": "Retail Banking", "code": "RETAIL"}, org_id=org_id)
    print(f"  created line of business '{lob['name']}'")
    return lob


def ensure_data_domain(lob_id: str, name: str, code: str, org_id: str) -> dict[str, Any]:
    path = f"/v1/lines-of-business/{lob_id}/data-domains"
    existing = _find(f"{path}?limit=200", "code", code, org_id=org_id)
    if existing:
        return existing
    _, domain = _request("POST", path, {"name": name, "code": code}, org_id=org_id)
    print(f"  created data domain '{domain['name']}'")
    return domain


def ensure_project(lob_id: str, data_domain_id: str, org_id: str) -> dict[str, Any]:
    path = f"/v1/lines-of-business/{lob_id}/projects"
    existing = _find(f"{path}?limit=200", "slug", "sample-estate", org_id=org_id)
    if existing:
        return existing
    _, project = _request(
        "POST",
        path,
        {"name": "Sample Estate", "slug": "sample-estate", "data_domain_id": data_domain_id},
        org_id=org_id,
    )
    print(f"  created project '{project['name']}'")
    return project


def ensure_datasource(project_id: str, org_id: str) -> dict[str, Any]:
    path = f"/v1/projects/{project_id}/datasources"
    existing = _find(f"{path}?limit=200", "name", "Core Banking (sample)", org_id=org_id)
    if existing:
        return existing
    _, datasource = _request(
        "POST",
        path,
        {
            "name": "Core Banking (sample)",
            "connector_type": "postgres",
            "dialect": "postgres",
            "environment": "SANDBOX",
            "network_zone": "sample",
            # Canonical push never resolves this reference; it only records the
            # source identity. A live pull would resolve it through the secret
            # provider (env:// is development-only).
            "credential_reference": "env://AIDA_SAMPLE_SOURCE",
            "max_concurrency": 2,
        },
        org_id=org_id,
    )
    print(f"  registered datasource '{datasource['name']}'")
    return datasource


def _column(
    name: str, ordinal: int, physical_type: str, *, nullable: bool = True, **attributes: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "ordinal_position": ordinal,
        "physical_type": physical_type,
        "nullable": nullable,
        "attributes": attributes,
    }


def _pk(name: str, columns: list[str]) -> dict[str, Any]:
    return {"name": name, "constraint_type": "PRIMARY_KEY", "columns": columns}


def _fk(
    name: str, columns: list[str], ref_schema: str, ref_table: str, ref_columns: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "constraint_type": "FOREIGN_KEY",
        "columns": columns,
        "referenced_schema": ref_schema,
        "referenced_table": ref_table,
        "referenced_columns": ref_columns,
    }


def build_envelope() -> dict[str, Any]:
    """A value-free retail-and-risk estate: structure and keys only."""
    retail_tables = [
        {
            "name": "customer",
            "object_type": "TABLE",
            "source_description": "Retail banking customers (sample structure).",
            "attributes": {"business_domain": "customer", "data_tier": "curated"},
            "columns": [
                _column("customer_id", 1, "bigint", nullable=False, classification="INTERNAL"),
                _column("first_name", 2, "varchar(120)", classification="PII"),
                _column("last_name", 3, "varchar(120)", classification="PII"),
                _column("email", 4, "varchar(320)", classification="PII"),
                _column("date_of_birth", 5, "date", classification="PII"),
                _column("segment_code", 6, "varchar(16)"),
                _column("status", 7, "varchar(16)"),
                _column("created_at", 8, "timestamptz", nullable=False),
            ],
            "constraints": [_pk("pk_customer", ["customer_id"])],
        },
        {
            "name": "account",
            "object_type": "TABLE",
            "source_description": "Deposit and lending accounts held by customers.",
            "attributes": {"business_domain": "customer", "data_tier": "curated"},
            "columns": [
                _column("account_id", 1, "bigint", nullable=False, classification="INTERNAL"),
                _column("customer_id", 2, "bigint", nullable=False, classification="INTERNAL"),
                _column("account_type", 3, "varchar(24)"),
                _column("currency_code", 4, "char(3)"),
                _column("branch_code", 5, "varchar(12)"),
                _column("status", 6, "varchar(16)"),
                _column("opened_at", 7, "timestamptz", nullable=False),
            ],
            "constraints": [
                _pk("pk_account", ["account_id"]),
                _fk("fk_account_customer", ["customer_id"], "retail", "customer", ["customer_id"]),
            ],
        },
        {
            "name": "card",
            "object_type": "TABLE",
            "source_description": "Payment cards issued against an account.",
            "attributes": {"business_domain": "customer", "data_tier": "curated"},
            "columns": [
                _column("card_id", 1, "bigint", nullable=False, classification="INTERNAL"),
                _column("account_id", 2, "bigint", nullable=False, classification="INTERNAL"),
                _column("card_network", 3, "varchar(24)"),
                _column("status", 4, "varchar(16)"),
                _column("issued_at", 5, "timestamptz", nullable=False),
            ],
            "constraints": [
                _pk("pk_card", ["card_id"]),
                _fk("fk_card_account", ["account_id"], "retail", "account", ["account_id"]),
            ],
        },
        {
            "name": "transaction_fact",
            "object_type": "TABLE",
            "source_description": "Posted account transactions (fact table).",
            "attributes": {
                "business_domain": "customer",
                "data_tier": "curated",
                "grain": "transaction",
            },
            "columns": [
                _column("transaction_id", 1, "bigint", nullable=False, classification="INTERNAL"),
                _column("account_id", 2, "bigint", nullable=False, classification="INTERNAL"),
                _column("posted_at", 3, "timestamptz", nullable=False),
                _column("amount_minor", 4, "bigint", nullable=False, classification="CONFIDENTIAL"),
                _column("currency_code", 5, "char(3)"),
                _column("direction", 6, "varchar(8)"),
                _column("merchant_category_code", 7, "varchar(8)"),
            ],
            "constraints": [
                _pk("pk_transaction_fact", ["transaction_id"]),
                _fk(
                    "fk_transaction_account",
                    ["account_id"],
                    "retail",
                    "account",
                    ["account_id"],
                ),
            ],
        },
    ]
    risk_tables = [
        {
            "name": "customer_risk_snapshot",
            "object_type": "TABLE",
            "source_description": "Point-in-time risk banding per customer.",
            "attributes": {"business_domain": "risk", "data_tier": "curated"},
            "columns": [
                _column("snapshot_id", 1, "bigint", nullable=False, classification="INTERNAL"),
                _column("customer_id", 2, "bigint", nullable=False, classification="INTERNAL"),
                _column("risk_band", 3, "varchar(8)"),
                _column("pd_score_bucket", 4, "varchar(16)", classification="CONFIDENTIAL"),
                _column("captured_at", 5, "timestamptz", nullable=False),
            ],
            "constraints": [
                _pk("pk_customer_risk_snapshot", ["snapshot_id"]),
                _fk(
                    "fk_risk_snapshot_customer",
                    ["customer_id"],
                    "retail",
                    "customer",
                    ["customer_id"],
                ),
            ],
        },
        {
            "name": "account_exposure",
            "object_type": "TABLE",
            "source_description": "Current credit exposure bucketed per account.",
            "attributes": {"business_domain": "risk", "data_tier": "curated"},
            "columns": [
                _column("exposure_id", 1, "bigint", nullable=False, classification="INTERNAL"),
                _column("account_id", 2, "bigint", nullable=False, classification="INTERNAL"),
                _column("exposure_bucket", 3, "varchar(16)", classification="CONFIDENTIAL"),
                _column("as_of_date", 4, "date", nullable=False),
            ],
            "constraints": [
                _pk("pk_account_exposure", ["exposure_id"]),
                _fk(
                    "fk_exposure_account",
                    ["account_id"],
                    "retail",
                    "account",
                    ["account_id"],
                ),
            ],
        },
    ]
    return {
        "envelope_version": "1.0",
        "idempotency_key": "seed-sample-estate-v1",
        "producer": "sample-estate-seed/1.0",
        "transport": "PUSH",
        "snapshot_type": "FULL",
        "emitted_at": datetime.now(UTC).isoformat(),
        "catalogs": [
            {
                "name": "core_banking",
                "attributes": {"environment": "sandbox"},
                "schemas": [
                    {"name": "retail", "attributes": {}, "tables": retail_tables},
                    {"name": "risk", "attributes": {}, "tables": risk_tables},
                ],
            }
        ],
    }


def ingest_estate(datasource_id: str, org_id: str) -> None:
    _, result = _request(
        "POST",
        f"/v1/datasources/{datasource_id}/metadata-ingestions",
        build_envelope(),
        org_id=org_id,
        expect=(200, 201),
    )
    counts = (result or {}).get("object_counts", {})
    print(
        "  ingested estate: "
        f"{counts.get('tables', '?')} tables, "
        f"{counts.get('columns', '?')} columns, "
        f"{counts.get('constraints', '?')} constraints"
    )


def discover_and_approve_relationships(datasource_id: str, org_id: str) -> None:
    list_path = f"/v1/datasources/{datasource_id}/relationship-candidates?limit=500"
    _, existing = _request("GET", list_path, org_id=org_id)
    if not _items(existing):
        _request(
            "POST",
            f"/v1/datasources/{datasource_id}/relationship-candidates/discover",
            {"max_candidates": 500},
            org_id=org_id,
        )
        _, existing = _request("GET", list_path, org_id=org_id)

    candidates = _items(existing)
    pending = [c for c in candidates if c.get("status") == "PENDING"]
    approved_already = len(candidates) - len(pending)
    for candidate in pending:
        _request(
            "POST",
            f"/v1/relationship-candidates/{candidate['id']}/decision",
            {"decision": "APPROVE", "reason": "Seeded sample estate: declared foreign key."},
            org_id=org_id,
        )
    print(
        f"  relationship candidates: {len(pending)} approved"
        + (f", {approved_already} already decided" if approved_already else "")
    )


def main() -> int:
    print(f"Seeding sample estate against {BASE_URL}")
    try:
        wait_for_ready()
        org = ensure_organization()
        org_id = org["id"]
        lob = ensure_line_of_business(org_id)
        customer_domain = ensure_data_domain(lob["id"], "Customer", "CUSTOMER", org_id)
        ensure_data_domain(lob["id"], "Risk", "RISK", org_id)
        project = ensure_project(lob["id"], customer_domain["id"], org_id)
        datasource = ensure_datasource(project["id"], org_id)
        ingest_estate(datasource["id"], org_id)
        discover_and_approve_relationships(datasource["id"], org_id)
    except SeedError as error:
        print(f"\nSeed failed: {error}", file=sys.stderr)
        return 1

    print("\nSample estate ready. Open the Atlas portal and pick 'Northwind Retail Bank (sample)'.")
    print("The catalog, knowledge graph and unified lineage now render a populated estate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
