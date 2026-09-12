#!/usr/bin/env python3
"""Seed a real, cross-database sample estate through the governed API.

A fresh install has no metadata, so the catalog, knowledge graph and unified
lineage all render empty. This script builds one organization with three data
domains, each backed by a real live datasource on a different engine:

    Customer  -- Postgres    (sample-source,       bank_demo)
    Payments  -- SQL Server  (sample-mssql-source,  bank_demo_mssql)
    Risk      -- Postgres    (sample-source,        risk_demo -- a second
                              database on the same server, registered as its
                              own DataSource so this stays a genuine
                              cross-source relationship, not a same-source one)

Unlike a pushed metadata envelope, this registers each sample database as a
real DataSource and triggers the platform's own connector-based discovery
(DatasourceDiscoveryWorkflow) against it -- the catalog reflects what the
connector actually introspects, not a hand-written fixture. `customer_id`/
`account_id` values were seeded to overlap across all three (see the
infra/*/init.sql files), so the platform's cross-source relationship detector
finds real matches instead of nothing.

It also exercises the platform's cross-domain governance (ADR-0017): it
requests and approves cross-boundary grants letting Payments and Risk each
see into Customer (the natural system-of-record hub), then runs cross-source
relationship and object-resolution discovery across those boundaries and
approves what is found. Payments<->Risk is deliberately left ungranted, so
Unified Lineage shows a real `withheld_cross_boundary_domain_ids` case rather
than everything being trivially connected.

Finally it publishes one parameterised governed tool (`accounts_by_branch`)
on the Customer project, through draft -> submit -> independent approval. A
fresh install runs with model generation off, so an approved tool is the only
path to an answer: without one, every question Ask can be asked on this estate
refuses. With it, the question printed at the end of the run first refuses
naming the input it needs, then answers from the tool once that input is
supplied.

Every discovery/proposal step is decided by a *second* development identity
from the one that triggered it: the API enforces maker != checker on
relationship candidates, cross-source candidates, and governance reviews
(a single self-approving identity gets a 409 on every one of these).

The script is idempotent: every tenancy object is looked up before it is
created, and only PENDING candidates/reviews are decided. Re-running it is
safe.

Usage:
    python scripts/seed_sample_estate.py
    AIDA_BASE_URL=http://api:8000 python scripts/seed_sample_estate.py

Environment:
    AIDA_BASE_URL   API base URL (default http://localhost:8000)
    AIDA_SEED_SLUG  Organization slug to create/reuse (default sample-bank)

Only ever point this at a development or demonstration environment. It
creates an organization and registers datasources; it is not intended for
production.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = os.environ.get("AIDA_BASE_URL", "http://localhost:8000").rstrip("/")
ORG_SLUG = os.environ.get("AIDA_SEED_SLUG", "sample-bank")

# Every role, because the seed exercises tenancy, live discovery, relationship
# review and cross-domain governance in one pass. These are development-only
# identities; production rejects header identities entirely.
BOOTSTRAP_ROLES = (
    "PlatformAdmin,MetadataAdmin,DataAdmin,MetadataIngestor,SemanticAdmin,"
    "DataSteward,MetadataReviewer,Auditor,Operations,Analyst,Viewer,Reviewer"
)

# Two distinct principals: the platform enforces maker != checker on every
# proposal this script decides (relationship candidates, cross-source
# candidates, governance reviews) -- one identity triggers discovery/requests,
# the other decides what came out of it.
MAKER_PRINCIPAL = "sample-estate-seed"
CHECKER_PRINCIPAL = "sample-estate-seed-reviewer"


def _headers(principal: str) -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": BOOTSTRAP_ROLES,
        "X-Business-Purpose": "Seed demonstration estate for local evaluation",
        "Content-Type": "application/json",
    }


MAKER_HEADERS = _headers(MAKER_PRINCIPAL)
CHECKER_HEADERS = _headers(CHECKER_PRINCIPAL)


class SeedError(RuntimeError):
    """A non-recoverable failure while seeding."""


def _request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    org_id: str | None = None,
    headers: dict[str, str] | None = None,
    expect: tuple[int, ...] = (200, 201, 202),
) -> Any:
    url = f"{BASE_URL}{path}"
    # The scheme is validated here; the seed only ever calls a first-party
    # development API, so the S310 URL-audit warnings below are suppressed.
    if not url.startswith(("http://", "https://")):
        raise SeedError(f"refusing to open non-HTTP URL: {url}")
    request_headers = dict(headers if headers is not None else MAKER_HEADERS)
    if org_id:
        request_headers["X-Organization-Id"] = org_id
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)  # noqa: S310
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


def ensure_project(
    lob_id: str, data_domain_id: str, name: str, slug: str, org_id: str
) -> dict[str, Any]:
    path = f"/v1/lines-of-business/{lob_id}/projects"
    existing = _find(f"{path}?limit=200", "slug", slug, org_id=org_id)
    if existing:
        return existing
    _, project = _request(
        "POST",
        path,
        {"name": name, "slug": slug, "data_domain_id": data_domain_id},
        org_id=org_id,
    )
    print(f"  created project '{project['name']}'")
    return project


def ensure_datasource(
    project_id: str,
    org_id: str,
    *,
    name: str,
    connector_type: str,
    dialect: str,
    credential_reference: str,
) -> dict[str, Any]:
    path = f"/v1/projects/{project_id}/datasources"
    existing = _find(f"{path}?limit=200", "name", name, org_id=org_id)
    if existing:
        return existing
    _, datasource = _request(
        "POST",
        path,
        {
            "name": name,
            "connector_type": connector_type,
            "dialect": dialect,
            "environment": "SANDBOX",
            "network_zone": "sample",
            "credential_reference": credential_reference,
            "max_concurrency": 2,
        },
        org_id=org_id,
    )
    print(f"  registered datasource '{datasource['name']}' ({connector_type})")
    return datasource


def run_discovery(datasource_id: str, org_id: str, *, timeout_seconds: int = 180) -> None:
    """Trigger real connector introspection and wait for it to finish.

    This is the live-pull path (`DatasourceDiscoveryWorkflow` via
    `POST /v1/datasources/{id}/analysis-runs`) -- the same one
    `scripts/verify-local.ps1` uses to prove the connectors work -- not a
    pushed metadata envelope.
    """
    _, run = _request(
        "POST",
        f"/v1/datasources/{datasource_id}/analysis-runs",
        {"mode": "INCREMENTAL"},
        org_id=org_id,
    )
    run_id = run["id"]
    deadline = time.monotonic() + timeout_seconds
    while True:
        _, run = _request("GET", f"/v1/analysis-runs/{run_id}", org_id=org_id)
        status = run.get("status")
        if status == "COMPLETED":
            print(f"  analysis run completed for datasource {datasource_id}")
            return
        if status == "FAILED":
            raise SeedError(f"analysis run {run_id} failed: {run.get('error_class')}")
        if time.monotonic() > deadline:
            raise SeedError(f"analysis run {run_id} did not complete within {timeout_seconds}s")
        time.sleep(1)


def discover_and_approve_same_source_relationships(datasource_id: str, org_id: str) -> None:
    """Discover FK-based relationship candidates within one datasource and
    approve them. Discovery and decision use different principals -- the API
    refuses a maker deciding their own candidate."""
    list_path = f"/v1/datasources/{datasource_id}/relationship-candidates?limit=500"
    _request(
        "POST",
        f"/v1/datasources/{datasource_id}/relationship-candidates/discover",
        {"max_candidates": 500},
        org_id=org_id,
    )
    _, existing = _request("GET", list_path, org_id=org_id)
    candidates = _items(existing)
    pending = [c for c in candidates if c.get("status") == "PENDING"]
    for candidate in pending:
        _request(
            "POST",
            f"/v1/relationship-candidates/{candidate['id']}/decision",
            {"decision": "APPROVE", "reason": "Seeded sample estate: declared foreign key."},
            org_id=org_id,
            headers=CHECKER_HEADERS,
        )
    print(f"  same-source relationship candidates: {len(pending)} approved")


def _find_pending_review(object_type: str, object_id: str, org_id: str) -> dict[str, Any] | None:
    _, payload = _request(
        "GET",
        "/v1/governance/reviews?status=PENDING&limit=200",
        org_id=org_id,
        headers=CHECKER_HEADERS,
    )
    for review in _items(payload):
        if review.get("object_type") == object_type and review.get("object_id") == object_id:
            return review
    return None


def request_and_approve_grant(
    source_domain_id: str, target_domain_id: str, org_id: str, *, reason: str
) -> None:
    """Request `target_domain_id` visibility into `source_domain_id`, then
    approve the governance review it files (ADR-0017 SS4) -- with the checker
    identity, since the requester cannot approve their own request."""
    _, grants = _request(
        "GET",
        f"/v1/data-domains/{source_domain_id}/cross-boundary-grants?limit=200",
        org_id=org_id,
    )
    existing = next(
        (
            g
            for g in _items(grants)
            if g.get("target_data_domain_id") == target_domain_id and g.get("status") != "REJECTED"
        ),
        None,
    )
    if existing and existing.get("status") == "ACTIVE":
        print(
            f"  cross-boundary grant {source_domain_id[:8]}->{target_domain_id[:8]} already ACTIVE"
        )
        return
    if not existing:
        _, existing = _request(
            "POST",
            f"/v1/data-domains/{source_domain_id}/cross-boundary-grants",
            {
                "target_data_domain_id": target_domain_id,
                "edge_kinds": ["SUGGESTED_RELATIONSHIP"],
                "reason": reason,
            },
            org_id=org_id,
        )
    review = _find_pending_review("CROSS_BOUNDARY_GRANT", existing["id"], org_id)
    if review is None:
        print(
            f"  cross-boundary grant {source_domain_id[:8]}->{target_domain_id[:8]}"
            " has no pending review"
        )
        return
    _request(
        "POST",
        f"/v1/governance/reviews/{review['id']}/decision",
        {
            "decision": "APPROVE",
            "reason": "Seeded sample estate: expected cross-domain resolution need.",
        },
        org_id=org_id,
        headers=CHECKER_HEADERS,
    )
    print(f"  cross-boundary grant {source_domain_id[:8]}->{target_domain_id[:8]} approved")


def discover_and_approve_cross_source(domain_id: str, target_domain_id: str, org_id: str) -> None:
    """Infer and approve cross-source relationships and object-resolution
    candidates across an already-granted domain boundary."""
    for kind, discover_path, decision_path in (
        (
            "relationship",
            f"/v1/data-domains/{domain_id}/relationship-candidates/discover-cross-source",
            "/v1/relationship-candidates",
        ),
        (
            "object-resolution",
            f"/v1/data-domains/{domain_id}/cross-source-object-resolution-candidates/discover",
            "/v1/cross-source-object-resolution-candidates",
        ),
    ):
        _request("POST", discover_path, {"target_data_domain_id": target_domain_id}, org_id=org_id)
        # Candidates land per-datasource, not per-domain -- list every
        # datasource in this domain and collect what discovery just proposed.
        approved = 0
        _, domain_datasources = _request(
            "GET", f"/v1/organizations/{org_id}/datasources?limit=200", org_id=org_id
        )
        for datasource in _items(domain_datasources):
            if datasource.get("data_domain_id") not in (domain_id, target_domain_id):
                continue
            list_kind_path = (
                f"/v1/datasources/{datasource['id']}/relationship-candidates?limit=500"
                if kind == "relationship"
                else (
                    f"/v1/datasources/{datasource['id']}"
                    "/cross-source-object-resolution-candidates?limit=500"
                )
            )
            _, existing = _request("GET", list_kind_path, org_id=org_id)
            for candidate in _items(existing):
                if candidate.get("status") != "PENDING":
                    continue
                if candidate.get("datasource_id") == candidate.get("target_datasource_id"):
                    continue
                _request(
                    "POST",
                    f"{decision_path}/{candidate['id']}/decision",
                    {"decision": "APPROVE", "reason": "Seeded sample estate: cross-source match."},
                    org_id=org_id,
                    headers=CHECKER_HEADERS,
                )
                approved += 1
        print(f"  cross-source {kind} candidates: {approved} approved")


# ---------------------------------------------------------------------------
# The governed tool the Ask journey answers from.
#
# With model generation off -- the default, and the only state a fresh install
# has ever been in -- an approved governed tool is the *only* path to an
# answer. Until one exists, every question this estate can be asked refuses:
# retrieval finds no published tool, the planner falls to MODEL_GENERATION and
# the run fails closed on the missing model route. That made the product's
# core journey undemonstrable on a seeded estate, which is what this tool
# fixes (tracker R11-B1).
#
# It is deliberately *parameterised*, and its one parameter is deliberately a
# string: the Ask screen sends parameter values as strings and the server
# coerces them against this schema, so a STRING parameter is the shape the
# browser journey can actually satisfy. `branch_code` is a real TEXT column on
# `customer.account` (infra/sample-source/init.sql), carrying values like
# BR-101 -- so the rendered SQL is valid Postgres against the seeded data,
# not a demo that only type-checks.
# ---------------------------------------------------------------------------

#: Slug of the seeded governed tool, on the Customer domain's project.
GOVERNED_TOOL_SLUG = "accounts_by_branch"

#: Its single required parameter. A caller who omits it gets the structured
#: 409 (`MISSING_TOOL_PARAMETERS`) naming exactly this, which is what the Ask
#: screen renders an input for.
GOVERNED_TOOL_PARAMETER = "branch_code"

#: A value present in the seeded `customer.account` rows, so the demonstrated
#: answer is a non-empty result set.
GOVERNED_TOOL_PARAMETER_EXAMPLE = "BR-101"

#: The question this estate can be asked with generation off. Its content
#: tokens ("accounts", "booked", "branch") all appear in the tool's
#: name/slug/description, so lexical retrieval scores it above
#: `agent_tool_match_threshold` (0.55) without the caller having to pin a tool
#: version by hand.
GOVERNED_TOOL_QUESTION = "Which accounts are booked at a branch?"

GOVERNED_TOOL_NAME = "Accounts booked at a branch"

GOVERNED_TOOL_DESCRIPTION = (
    "Accounts booked at a given branch, with the account type, currency, "
    "status and current balance held there. Ask for one branch at a time by "
    "its branch code."
)

#: Schema-qualified exactly as connector discovery catalogs it; the draft
#: endpoint refuses any table the catalog does not hold for this datasource.
#: The placeholder is spelled out rather than interpolated from
#: `GOVERNED_TOOL_PARAMETER` because the draft endpoint refuses (422) any
#: template whose placeholders do not exactly match the declared parameters,
#: so the two cannot drift apart unnoticed.
GOVERNED_TOOL_SQL = (
    "SELECT account_id, customer_id, account_type, currency_code, status, current_balance "
    "FROM customer.account "
    "WHERE branch_code = :branch_code"
)


def ensure_governed_tool(project_id: str, datasource_id: str, org_id: str) -> dict[str, Any]:
    """Publish one parameterised governed tool through the real approval path.

    Draft (maker) -> submit for review (maker) -> independent approval
    (checker), the same three steps a tool a person authored goes through.
    Nothing here writes a PUBLISHED row directly: publication happens only as
    the effect of `POST /v1/governance/reviews/{id}/decision`, and the API
    refuses that decision to the principal who requested it (409,
    "maker-checker separation is required"), so the second identity this
    script already carries for relationship and grant reviews is required
    here too rather than decorative.

    Idempotent, like everything else in this script: an already-published
    version is returned untouched, and a draft or in-review version left
    behind by an interrupted run is carried forward instead of stacking up a
    second one.
    """
    list_path = f"/v1/projects/{project_id}/tools?limit=500"
    _, payload = _request("GET", list_path, org_id=org_id)
    versions = [item for item in _items(payload) if item.get("slug") == GOVERNED_TOOL_SLUG]
    published = next((v for v in versions if v.get("status") == "PUBLISHED"), None)
    if published:
        print(f"  governed tool '{GOVERNED_TOOL_SLUG}' already PUBLISHED")
        return published

    version = next(
        (v for v in versions if v.get("status") in ("DRAFT", "REVIEW_REQUIRED")),
        None,
    )
    if version is None:
        _, version = _request(
            "POST",
            f"/v1/projects/{project_id}/tools",
            {
                "slug": GOVERNED_TOOL_SLUG,
                "name": GOVERNED_TOOL_NAME,
                "description": GOVERNED_TOOL_DESCRIPTION,
                "datasource_id": datasource_id,
                "sql_template": GOVERNED_TOOL_SQL,
                "parameters": [
                    {
                        "name": GOVERNED_TOOL_PARAMETER,
                        "parameter_type": "STRING",
                        "required": True,
                        "max_length": 32,
                    }
                ],
                "allowed_roles": ["Analyst"],
            },
            org_id=org_id,
        )
        print(f"  drafted governed tool '{GOVERNED_TOOL_SLUG}' v{version['version']}")

    if version.get("status") == "DRAFT":
        _request("POST", f"/v1/tool-versions/{version['id']}/submit", org_id=org_id)

    review = _find_pending_review("GOVERNED_TOOL_VERSION", version["id"], org_id)
    if review is None:
        raise SeedError(
            f"governed tool {version['id']} has no pending review to approve; "
            "it cannot be published without one"
        )
    _request(
        "POST",
        f"/v1/governance/reviews/{review['id']}/decision",
        {
            "decision": "APPROVE",
            "reason": "Seeded sample estate: parameterised tool for the governed Ask journey.",
        },
        org_id=org_id,
        headers=CHECKER_HEADERS,
    )

    _, payload = _request("GET", list_path, org_id=org_id)
    approved = next(
        (
            item
            for item in _items(payload)
            if item.get("id") == version["id"] and item.get("status") == "PUBLISHED"
        ),
        None,
    )
    if approved is None:
        raise SeedError(f"governed tool {version['id']} did not reach PUBLISHED after approval")
    print(f"  governed tool '{GOVERNED_TOOL_SLUG}' approved and PUBLISHED")
    return approved


DOMAINS = (
    {
        "code": "CUSTOMER",
        "name": "Customer",
        "project_name": "Customer Master",
        "project_slug": "customer-master",
        "datasource_name": "Customer Master (Postgres, sample)",
        "connector_type": "postgres",
        "dialect": "postgres",
        "credential_reference": "env://AIDA_SAMPLE_SOURCE_DSN",
    },
    {
        "code": "PAYMENTS",
        "name": "Payments",
        "project_name": "Payments & Transactions",
        "project_slug": "payments-transactions",
        "datasource_name": "Payments & Transactions (SQL Server, sample)",
        "connector_type": "sqlserver",
        "dialect": "tsql",
        "credential_reference": "env://AIDA_SAMPLE_MSSQL_SOURCE_DSN",
    },
    {
        "code": "RISK",
        "name": "Risk",
        "project_name": "Risk & Compliance",
        "project_slug": "risk-compliance",
        "datasource_name": "Risk & Compliance (Postgres, sample)",
        "connector_type": "postgres",
        "dialect": "postgres",
        "credential_reference": "env://AIDA_SAMPLE_RISK_SOURCE_DSN",
    },
)

# Customer is the system-of-record hub: Payments and Risk each get a grant to
# see into it (both directions, so Unified Lineage renders fully connected
# viewed from any of the three). Payments<->Risk is deliberately left
# ungranted -- Unified Lineage should show a real withheld-boundary case.
GRANT_PAIRS = (
    (
        "CUSTOMER",
        "PAYMENTS",
        "Payments needs account/customer id resolution against the customer master.",
    ),
    (
        "PAYMENTS",
        "CUSTOMER",
        "Customer stewardship needs to see which accounts have posted activity.",
    ),
    (
        "CUSTOMER",
        "RISK",
        "Risk needs customer/account id resolution for risk snapshots and exposure.",
    ),
    (
        "RISK",
        "CUSTOMER",
        "Customer stewardship needs to see which customers carry an open risk case.",
    ),
)


def main() -> int:
    print(f"Seeding cross-database sample estate against {BASE_URL}")
    try:
        wait_for_ready()
        org = ensure_organization()
        org_id = org["id"]
        lob = ensure_line_of_business(org_id)

        domains: dict[str, dict[str, Any]] = {}
        projects: dict[str, dict[str, Any]] = {}
        datasources: dict[str, dict[str, Any]] = {}
        for spec in DOMAINS:
            domain = ensure_data_domain(lob["id"], spec["name"], spec["code"], org_id)
            domains[spec["code"]] = domain
            project = ensure_project(
                lob["id"], domain["id"], spec["project_name"], spec["project_slug"], org_id
            )
            projects[spec["code"]] = project
            datasource = ensure_datasource(
                project["id"],
                org_id,
                name=spec["datasource_name"],
                connector_type=spec["connector_type"],
                dialect=spec["dialect"],
                credential_reference=spec["credential_reference"],
            )
            datasources[spec["code"]] = datasource

        for code, datasource in datasources.items():
            print(f"Discovering {code} ({datasource['name']})...")
            run_discovery(datasource["id"], org_id)
            discover_and_approve_same_source_relationships(datasource["id"], org_id)

        print("Requesting and approving cross-boundary grants...")
        for source_code, target_code, reason in GRANT_PAIRS:
            request_and_approve_grant(
                domains[source_code]["id"], domains[target_code]["id"], org_id, reason=reason
            )

        # After discovery, because the draft endpoint validates the tool's SQL
        # against the tables the connector actually catalogued for this
        # datasource -- an unknown table is a 422, not a tool.
        print("Publishing the governed tool the Ask journey answers from...")
        ensure_governed_tool(
            projects["CUSTOMER"]["id"], datasources["CUSTOMER"]["id"], org_id
        )

        print("Discovering cross-source relationships and object resolutions...")
        for source_code, target_code, _ in GRANT_PAIRS[
            ::2
        ]:  # one direction per pair is enough to discover both ways
            discover_and_approve_cross_source(
                domains[source_code]["id"], domains[target_code]["id"], org_id
            )

    except SeedError as error:
        print(f"\nSeed failed: {error}", file=sys.stderr)
        return 1

    print("\nSample estate ready. Open ui-next and pick 'Northwind Retail Bank (sample)'.")
    print(
        "Catalog, Sources, Relationships, Cross-source, Knowledge graph and Unified "
        "lineage now render a real, cross-database estate spanning two Postgres "
        "databases and SQL Server."
    )
    print(
        "\nAsk, against the Customer datasource, with model generation off:\n"
        f'    "{GOVERNED_TOOL_QUESTION}"\n'
        f"  It refuses first, naming the input it needs ({GOVERNED_TOOL_PARAMETER}); "
        f"answer {GOVERNED_TOOL_PARAMETER_EXAMPLE} and the same question returns a "
        "governed result from the approved tool."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
