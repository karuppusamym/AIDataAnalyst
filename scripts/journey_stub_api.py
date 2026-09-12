"""An identity-aware stub upstream for the browser journey suite. Not production code.

`scripts/proxy_contract_stub_api.py` is this file's smaller sibling: it answers
every request with one marker line, which is all the `ui-proxy` job needs to
prove that `/mcp` and `/v1/...` reach the API rather than the SPA shell. The
browser journey (tracker R11-B11) needs more than a marker -- the SPA has to
render, so the upstream has to answer each screen's calls with something
shaped like the real thing, and it has to refuse the calls a least-privilege
identity is not entitled to make.

WHAT THIS IS AND IS NOT
-----------------------
**It is not the application, and it proves nothing about the application's
own authorization.** The real gate is `require_roles` in `src/aida/security.py`
running inside FastAPI, exercised by the Python test suite. What this process
provides is a *seat* for an identity so the browser suite can ask a different
question, which no backend test can answer: given a 200 the screen renders its
authorized surface, and given a 403 **the screen says so on the page** rather
than showing a blank panel, an empty state that reads as "no data", or a
spinner that never resolves. A 403 the UI swallows is the defect the browser
suite exists to catch.

So that the seat is not fiction, the required-role tuple for every rule below
is copied from `Docs/50-security/surface-control-matrix.md`, which is generated
from the live FastAPI application's actual `require_roles` dependencies.
`tests/test_journey_stub_contract.py` fails if the two ever disagree, so this
file cannot drift into testing a permission model the application does not
have.

HOW AN IDENTITY ARRIVES
-----------------------
The SPA is built with `VITE_AUTH_MODE=proxy`, which is a mode the application
already supports (`ui-next/src/lib/appConfig.ts`): the browser asserts no
identity of its own, because an authenticating reverse proxy in front of the
app is the authority. This process plays that authority. It reads the
`atlas_journey_identity` cookie and maps it to a role set, exactly as a real
authenticating proxy maps a session to claims before setting `X-Principal-Id`
/ `X-Roles` for the backend's development identity provider, or minting the
bearer token its OIDC provider verifies.

Using a cookie rather than a request header is what makes per-identity testing
possible at all: `VITE_DEV_PRINCIPAL_ID` / `VITE_DEV_ROLES` are `import.meta.env`
values baked in by `vite build`, so seating six identities through the browser's
own headers would mean six images.

RESPONSE SHAPES
---------------
Synthesised from `Docs/90-reference/openapi-baseline.json` -- the committed
contract -- rather than hand-written, so a screen never receives a body of a
shape the real API could not send, and an endpoint this file never anticipated
still gets a type-correct answer instead of a 404 that breaks a render.
`_OVERRIDES` pins the handful of values the tests actually assert on.

Standard library only. Usage::

    python scripts/journey_stub_api.py [port] [--spec PATH]
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Every response carries this header. A test asserts on it to prove a call
# crossed the proxy hop and was answered HERE -- an SPA-fallback `index.html`
# with status 200 cannot carry it. This is the browser-side equivalent of the
# `ATLAS-STUB-UPSTREAM` body marker the `ui-proxy` job greps for.
UPSTREAM_HEADER = "X-Atlas-Stub-Upstream"
UPSTREAM_VALUE = "journey"

IDENTITY_COOKIE = "atlas_journey_identity"

ORG_ID = "00000000-0000-0000-0000-000000000001"


# --- THE LEAST-PRIVILEGE IDENTITIES ----------------------------------------
#
# One identity per journey step, each holding ONLY the roles that step needs.
# A PlatformAdmin performing all six would prove nothing about authorization,
# which is the whole reason the tracker row names least privilege.
#
# Note what these role sets deliberately do NOT include: none of the five
# working identities can list organizations (`GET /v1/organizations` requires
# Auditor / Operations / OrganizationAdmin / PlatformAdmin), so the shell's own
# bootstrap call is refused for most of them. That is not a flaw in the seating
# -- it is a real property of this application's role model, and the suite
# asserts the shell survives it rather than hiding it.
IDENTITIES: dict[str, frozenset[str]] = {
    # 1. connect a source, and 2. scan it.
    "connector": frozenset({"DataAdmin"}),
    # 3. describe.
    "steward": frozenset({"DataSteward"}),
    # 4. review.
    "reviewer": frozenset({"Reviewer"}),
    # 5. Ask. `Viewer` is here for a reason worth stating: the Ask screen's
    # datasource picker is fed by the shell's scope resolution, which loads
    # workspaces, projects and datasources in ONE `Promise.all`
    # (`ui-next/src/lib/scope.tsx`). A 403 on any of the three fails all
    # three, and `Analyst` alone cannot read
    # `GET /v1/organizations/{organization_id}/projects`. So `Analyst,Viewer`
    # IS the least privilege that can actually ask a question -- a narrower
    # seat does not test step 5, it tests the scope loader's failure mode.
    # Note it still cannot POST `agent-analyses` on `Viewer` alone.
    "analyst": frozenset({"Analyst", "Viewer"}),
    # 6. evidence.
    "auditor": frozenset({"Auditor"}),
    # Used only by the denied-access cases as the identity that holds nothing
    # at all beyond being authenticated.
    "bystander": frozenset({"Viewer"}),
}


class Rule:
    """One route, and the roles the real application requires for it."""

    __slots__ = ("method", "pattern", "roles", "surface")

    def __init__(self, method: str, pattern: str, roles: tuple[str, ...], surface: str) -> None:
        self.method = method
        self.pattern = re.compile(pattern)
        self.roles = roles
        # The `Surface` cell of the matrix row these roles were copied from.
        # `tests/test_journey_stub_contract.py` looks the row up by this string.
        self.surface = surface


# --- THE ROUTE TABLE -------------------------------------------------------
#
# `roles` is copied from the `Required roles` cell of the named surface in
# `Docs/50-security/surface-control-matrix.md`. Do not edit one without the
# other; the contract test fails if they diverge.
#
# An empty `roles` tuple means the matrix says `none declared` -- the route is
# gated by identity alone, not by a role tuple.
_UUID = r"[0-9a-fA-F-]{36}"
_SEG = r"[^/]+"

ROUTE_RULES: list[Rule] = [
    # --- the shell's bootstrap ---------------------------------------------
    Rule("GET", r"^/v1/me/?$", (), "GET /v1/me"),
    Rule(
        "GET",
        r"^/v1/organizations/?$",
        ("Auditor", "Operations", "OrganizationAdmin", "PlatformAdmin"),
        "GET /v1/organizations",
    ),
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/workspaces/?$",
        ("Analyst", "DataAdmin", "OrganizationAdmin", "PlatformAdmin", "Reviewer", "Steward"),
        "GET /v1/organizations/{organization_id}/workspaces",
    ),
    Rule(
        "GET",
        rf"^/v1/workspaces/{_SEG}/source-bindings/?$",
        ("Analyst", "DataAdmin", "OrganizationAdmin", "PlatformAdmin", "Reviewer", "Steward"),
        "GET /v1/workspaces/{workspace_id}/source-bindings",
    ),
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/projects/?$",
        (
            "DataAdmin",
            "Operations",
            "OrganizationAdmin",
            "PlatformAdmin",
            "ProjectAdmin",
            "Viewer",
        ),
        "GET /v1/organizations/{organization_id}/projects",
    ),
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/lines-of-business/?$",
        ("DataAdmin", "OrganizationAdmin", "PlatformAdmin", "Viewer"),
        "GET /v1/organizations/{organization_id}/lines-of-business",
    ),
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/catalog/rows/?$",
        ("Analyst", "MetadataAdmin", "PlatformAdmin", "Viewer"),
        "GET /v1/organizations/{organization_id}/catalog/rows",
    ),
    # --- step 1: connect a source ------------------------------------------
    Rule(
        "GET",
        rf"^/v1/projects/{_SEG}/datasources/?$",
        ("DataAdmin", "OrganizationAdmin", "PlatformAdmin", "Viewer"),
        "GET /v1/projects/{project_id}/datasources",
    ),
    Rule(
        "POST",
        rf"^/v1/projects/{_SEG}/datasources/?$",
        ("DataAdmin", "PlatformAdmin"),
        "POST /v1/projects/{project_id}/datasources",
    ),
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/datasources/?$",
        (
            "Analyst",
            "DataAdmin",
            "MetadataAdmin",
            "Operations",
            "OrganizationAdmin",
            "PlatformAdmin",
            "ProjectAdmin",
            "Viewer",
        ),
        "GET /v1/organizations/{organization_id}/datasources",
    ),
    # --- step 2: scan -------------------------------------------------------
    Rule(
        "POST",
        rf"^/v1/datasources/{_SEG}/analysis-runs/?$",
        ("DataAdmin", "MetadataAdmin", "PlatformAdmin"),
        "POST /v1/datasources/{datasource_id}/analysis-runs",
    ),
    Rule(
        "GET",
        rf"^/v1/datasources/{_SEG}/analysis-runs/?$",
        ("DataAdmin", "MetadataAdmin", "PlatformAdmin", "Viewer"),
        "GET /v1/datasources/{datasource_id}/analysis-runs",
    ),
    # --- step 3: describe ---------------------------------------------------
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/asset-description-drafts/?$",
        (
            "Analyst",
            "Auditor",
            "DataAdmin",
            "DataSteward",
            "MetadataAdmin",
            "PlatformAdmin",
            "Reviewer",
            "SemanticAdmin",
            "Viewer",
        ),
        "GET /v1/organizations/{organization_id}/asset-description-drafts",
    ),
    Rule(
        "POST",
        rf"^/v1/organizations/{_SEG}/asset-description-drafts/generate/?$",
        ("DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"),
        "POST /v1/organizations/{organization_id}/asset-description-drafts/generate",
    ),
    Rule(
        "POST",
        rf"^/v1/asset-description-drafts/{_SEG}/submit/?$",
        ("DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"),
        "POST /v1/asset-description-drafts/{draft_id}/submit",
    ),
    # --- step 4: review -----------------------------------------------------
    Rule(
        "GET",
        r"^/v1/governance/reviews/queue/?$",
        ("DataSteward", "PlatformAdmin", "Reviewer", "SemanticAdmin"),
        "GET /v1/governance/reviews/queue",
    ),
    Rule(
        "GET",
        r"^/v1/governance/reviews/queue/summary/?$",
        ("DataSteward", "PlatformAdmin", "Reviewer", "SemanticAdmin"),
        "GET /v1/governance/reviews/queue/summary",
    ),
    Rule(
        "POST",
        rf"^/v1/governance/reviews/{_SEG}/decision/?$",
        ("DataSteward", "PlatformAdmin", "Reviewer"),
        "POST /v1/governance/reviews/{review_id}/decision",
    ),
    # --- step 5: Ask --------------------------------------------------------
    Rule(
        "POST",
        rf"^/v1/datasources/{_SEG}/agent-analyses/?$",
        ("AgentDeveloper", "Analyst", "PlatformAdmin"),
        "POST /v1/datasources/{datasource_id}/agent-analyses",
    ),
    Rule(
        "GET",
        rf"^/v1/datasources/{_SEG}/agent-runs/?$",
        ("AgentDeveloper", "Analyst", "Auditor", "PlatformAdmin", "Viewer"),
        "GET /v1/datasources/{datasource_id}/agent-runs",
    ),
    Rule(
        "GET",
        rf"^/v1/agent-runs/{_SEG}/grounding-receipts/?$",
        ("AgentDeveloper", "Analyst", "Auditor", "PlatformAdmin", "Viewer"),
        "GET /v1/agent-runs/{agent_run_id}/grounding-receipts",
    ),
    Rule(
        "GET",
        rf"^/v1/agent-runs/{_SEG}/?$",
        ("AgentDeveloper", "Analyst", "Auditor", "PlatformAdmin", "Viewer"),
        "GET /v1/agent-runs/{agent_run_id}",
    ),
    Rule(
        "GET",
        rf"^/v1/datasources/{_SEG}/health/?$",
        (
            "Analyst",
            "DataAdmin",
            "MetadataAdmin",
            "Operations",
            "OrganizationAdmin",
            "PlatformAdmin",
            "ProjectAdmin",
            "Viewer",
        ),
        "GET /v1/datasources/{datasource_id}/health",
    ),
    Rule(
        "GET",
        rf"^/v1/datasources/{_SEG}/tables/?$",
        ("Analyst", "MetadataAdmin", "PlatformAdmin", "Viewer"),
        "GET /v1/datasources/{datasource_id}/tables",
    ),
    # --- step 6: evidence ---------------------------------------------------
    Rule(
        "GET",
        rf"^/v1/organizations/{_SEG}/audit-events/?$",
        ("Auditor", "Operations", "OrganizationAdmin", "PlatformAdmin"),
        "GET /v1/organizations/{organization_id}/audit-events",
    ),
    Rule(
        "GET",
        rf"^/v1/metadata/tables/{_SEG}/evidence/?$",
        (
            "Analyst",
            "Auditor",
            "DataAdmin",
            "DataSteward",
            "MetadataAdmin",
            "PlatformAdmin",
            "Reviewer",
            "SemanticAdmin",
            "Viewer",
        ),
        "GET /v1/metadata/tables/{table_id}/evidence",
    ),
]


def rule_for(method: str, path: str) -> Rule | None:
    for rule in ROUTE_RULES:
        if rule.method == method and rule.pattern.match(path):
            return rule
    return None


# --- RESPONSE SYNTHESIS ----------------------------------------------------


class SpecExamples:
    """Minimal valid response bodies, synthesised from the OpenAPI contract.

    Only the 2xx `application/json` response of the matching operation is
    used. Everything is generated once per (method, path template) and cached,
    so a screen that polls does not re-walk the schema.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
        self._cache: dict[tuple[str, str], Any] = {}
        # `/v1/organizations/{organization_id}/datasources` -> a regex, kept in
        # declaration order so a literal segment (`.../queue/summary`) is tried
        # before a templated one that would also match.
        self._routes: list[tuple[re.Pattern[str], str]] = []
        for template in sorted(spec.get("paths", {}), key=lambda t: (t.count("{"), -len(t))):
            pattern = re.escape(template)
            pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", pattern)
            self._routes.append((re.compile(rf"^{pattern}/?$"), template))

    def _template_for(self, path: str) -> str | None:
        for pattern, template in self._routes:
            if pattern.match(path):
                return template
        return None

    def body_for(self, method: str, path: str) -> Any:
        template = self._template_for(path)
        if template is None:
            return {}
        key = (method.lower(), template)
        if key in self._cache:
            return self._cache[key]
        operation = self._spec["paths"].get(template, {}).get(method.lower())
        body: Any = {}
        if isinstance(operation, dict):
            for status in ("200", "201", "202", "204"):
                response = operation.get("responses", {}).get(status)
                if not isinstance(response, dict):
                    continue
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if schema:
                    body = self._instance(schema, set(), 0)
                break
        self._cache[key] = body
        return body

    def _resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            return self._schemas.get(ref.rsplit("/", 1)[1], {})
        return schema

    def _instance(self, schema: Any, seen: set[str], depth: int) -> Any:
        """A minimal value satisfying `schema`.

        `seen` carries the `$ref` names on the current branch so a
        self-referential schema (a lineage node with children, a nested
        organization) terminates instead of recursing forever. `depth` is a
        second, blunter stop for deeply nested inline objects.
        """
        if not isinstance(schema, dict) or depth > 6:
            return None

        ref = schema.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[1]
            if name in seen:
                return None
            return self._instance(self._resolve(schema), seen | {name}, depth + 1)

        if "allOf" in schema:
            merged: dict[str, Any] = {}
            for part in schema["allOf"]:
                value = self._instance(part, seen, depth + 1)
                if isinstance(value, dict):
                    merged.update(value)
            return merged

        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                branches = [b for b in schema[keyword] if b.get("type") != "null"]
                if not branches:
                    return None
                return self._instance(branches[0], seen, depth + 1)

        if "const" in schema:
            return schema["const"]
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        if "default" in schema:
            return schema["default"]

        declared = schema.get("type")
        if isinstance(declared, list):
            declared = next((t for t in declared if t != "null"), None)
        if declared is None and "properties" in schema:
            declared = "object"

        if declared == "object":
            out: dict[str, Any] = {}
            properties = schema.get("properties")
            if isinstance(properties, dict):
                for name, sub in properties.items():
                    out[name] = self._instance(sub, seen, depth + 1)
            return out
        if declared == "array":
            items = schema.get("items")
            if items is None or depth >= 3:
                return []
            element = self._instance(items, seen, depth + 1)
            return [] if element is None else [element]
        if declared == "boolean":
            return False
        if declared in {"integer", "number"}:
            return 0
        if declared == "null":
            return None

        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-09-12T00:00:00Z"
        if fmt == "date":
            return "2026-09-12"
        if fmt == "uuid":
            return ORG_ID
        return ""


# --- OVERRIDES -------------------------------------------------------------
#
# The values the browser tests actually read. Everything not listed here is
# whatever the contract-shaped synthesiser produced, which is enough for a
# screen to render but not something a test should assert on.

NOW = "2026-09-12T00:00:00Z"

PROJECT_ID = "00000000-0000-0000-0000-0000000000a1"
LOB_ID = "00000000-0000-0000-0000-0000000000b1"
DOMAIN_ID = "00000000-0000-0000-0000-0000000000c1"
DATASOURCE_ID = "00000000-0000-0000-0000-0000000000d1"
TABLE_ID = "00000000-0000-0000-0000-0000000000e1"
DRAFT_ID = "00000000-0000-0000-0000-0000000000f1"
REVIEW_ID = "00000000-0000-0000-0000-00000000a001"
AGENT_RUN_ID = "00000000-0000-0000-0000-00000000b001"

DATASOURCE_NAME = "Journey warehouse"

# The principal each seat presents as. `requested_by` on the review proposal
# below is deliberately none of these: `ReviewQueueScreen` hides Approve/Reject
# on a proposal the viewer raised themselves (maker-checker), so a proposal
# attributed to the reviewer would make step 4 untestable.
PRINCIPALS = {
    "connector": "journey-connector",
    "steward": "journey-steward",
    "reviewer": "journey-reviewer",
    "analyst": "journey-analyst",
    "auditor": "journey-auditor",
    "bystander": "journey-bystander",
}
PROPOSER = "journey-describe-agent"

# Identities that have POSTed an analysis run. See the scan branch below.
_SCAN_STARTED: set[str] = set()


def _page(items: list[Any], limit: int = 500) -> dict[str, Any]:
    return {"items": items, "limit": limit, "offset": 0, "total": len(items)}


def _datasource() -> dict[str, Any]:
    return {
        "id": DATASOURCE_ID,
        "organization_id": ORG_ID,
        "line_of_business_id": LOB_ID,
        "data_domain_id": DOMAIN_ID,
        "project_id": PROJECT_ID,
        "name": DATASOURCE_NAME,
        "connector_type": "POSTGRES",
        "dialect": "postgresql",
        "environment": "DEV",
        "network_zone": "default",
        "status": "ACTIVE",
        "max_concurrency": 4,
        "capabilities": {},
        "credential_reference": "env://AIDA_SAMPLE_SOURCE_DSN",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _analysis_run(status: str) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-00000000c001",
        "organization_id": ORG_ID,
        "datasource_id": DATASOURCE_ID,
        "mode": "FULL",
        "status": status,
        "discovered_tables": 12,
        "created_objects": 12,
        "changed_objects": 0,
        "error_class": None,
        "error_message": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _overrides(method: str, path: str, identity: str) -> Any | None:
    """A hand-written body for a route the browser suite asserts on."""
    # --- test control ------------------------------------------------------
    #
    # The scan step is a state change, so the suite must be able to put this
    # process back to "nothing has been scanned". Without it the scan test
    # passes on a fresh container and fails on the second run against the same
    # one -- which is exactly the kind of order dependence that makes a browser
    # job flaky, and the reason it is reset explicitly rather than hoped about.
    if method == "POST" and re.fullmatch(r"/v1/__journey/reset/?", path):
        _SCAN_STARTED.clear()
        return {"reset": True}
    # --- the shell's bootstrap ---------------------------------------------
    if method == "GET" and re.fullmatch(r"/v1/me/?", path):
        return {
            "principal_id": PRINCIPALS.get(identity, identity),
            "principal_type": "USER",
            "organization_id": ORG_ID,
            "roles": sorted(IDENTITIES.get(identity, frozenset())),
            "persona": None,
            "identity_provider": "DEVELOPMENT",
        }
    if method == "GET" and re.fullmatch(r"/v1/organizations/?", path):
        return _page(
            [
                {
                    "id": ORG_ID,
                    "name": "Journey Bank",
                    "slug": "journey-bank",
                    "status": "ACTIVE",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
    # An empty workspace page is deliberate: `lib/scope.tsx` filters the
    # visible datasources by ACTIVE source bindings only when a workspace is
    # selected, so returning none makes every datasource in the tenant
    # selectable and keeps the Ask screen's picker populated.
    if method == "GET" and re.fullmatch(rf"/v1/organizations/{_SEG}/workspaces/?", path):
        return _page([], limit=200)
    if method == "GET" and re.fullmatch(rf"/v1/organizations/{_SEG}/projects/?", path):
        return _page(
            [
                {
                    "id": PROJECT_ID,
                    "organization_id": ORG_ID,
                    "line_of_business_id": LOB_ID,
                    "data_domain_id": DOMAIN_ID,
                    "name": "Retail analytics",
                    "slug": "retail-analytics",
                    "status": "ACTIVE",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
    if method == "GET" and re.fullmatch(rf"/v1/organizations/{_SEG}/lines-of-business/?", path):
        return _page(
            [
                {
                    "id": LOB_ID,
                    "organization_id": ORG_ID,
                    "name": "Retail bank",
                    "slug": "retail-bank",
                    "status": "ACTIVE",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )

    # --- step 1: connect a source ------------------------------------------
    if method == "GET" and re.fullmatch(rf"/v1/organizations/{_SEG}/datasources/?", path):
        return _page([_datasource()])
    if method == "GET" and re.fullmatch(rf"/v1/projects/{_SEG}/datasources/?", path):
        return _page([_datasource()])
    if method == "POST" and re.fullmatch(rf"/v1/projects/{_SEG}/datasources/?", path):
        return _datasource()

    # --- step 2: scan -------------------------------------------------------
    #
    # The ONE piece of state in this process, and it exists because the step is
    # a state change: `FirstSourceSetup` labels its button "Start the first
    # scan" only while no run exists, so a stub that always reported a
    # completed run would make the step unclickable, and one that always
    # reported none would make the click unobservable. After the POST the
    # history reports a QUEUED run and the step moves to "in progress".
    #
    # Keyed by identity, and only the scan test's identity ever writes it.
    if method == "GET" and re.fullmatch(rf"/v1/datasources/{_SEG}/analysis-runs/?", path):
        if identity in _SCAN_STARTED:
            return _page([_analysis_run("QUEUED")], limit=5)
        return _page([], limit=5)
    if method == "POST" and re.fullmatch(rf"/v1/datasources/{_SEG}/analysis-runs/?", path):
        _SCAN_STARTED.add(identity)
        return _analysis_run("QUEUED")
    if method == "GET" and re.fullmatch(rf"/v1/datasources/{_SEG}/health/?", path):
        return {
            "datasource_id": DATASOURCE_ID,
            "score": 87,
            "status": "HEALTHY",
            "factors": [
                {
                    "name": "run_success_rate",
                    "score": 30,
                    "maximum": 30,
                    "reason": "12 of 12 recent runs succeeded",
                    "evidence": {"window_days": 30},
                }
            ],
            "blockers": [],
            "computed_at": NOW,
        }

    # --- step 3: describe ---------------------------------------------------
    if method == "GET" and re.fullmatch(
        rf"/v1/organizations/{_SEG}/asset-description-drafts/?", path
    ):
        return _page(
            [
                {
                    "id": DRAFT_ID,
                    "organization_id": ORG_ID,
                    "table_id": TABLE_ID,
                    "table_name": "public.orders",
                    "drafted_text": "Order headers, one row per customer order.",
                    "accuracy_score": 0.8,
                    "clarity_score": 0.7,
                    "style_score": 0.6,
                    "completeness_score": 0.9,
                    # Must clear MINIMUM_EVIDENCE_FOR_REVIEW (0.4) or
                    # `DescriptionDraftsScreen` disables "Submit for review".
                    "overall_score": 0.75,
                    "status": "DRAFT",
                    "governance_review_id": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ],
            limit=200,
        )
    if method == "POST" and re.fullmatch(rf"/v1/asset-description-drafts/{_SEG}/submit/?", path):
        return _governance_review("PENDING")

    # --- step 4: review -----------------------------------------------------
    if method == "GET" and re.fullmatch(r"/v1/governance/reviews/queue/?", path):
        return {
            "organization_id": ORG_ID,
            "status_filter": "PENDING",
            "object_type_filter": None,
            "inference_run_id_filter": None,
            "generated_at": NOW,
            "total_proposals": 1,
            "by_status": {"PENDING": 1, "APPROVED": 0, "REJECTED": 0},
            "by_object_type": {"ASSET_DESCRIPTION_DRAFT": 1},
            "diffable_count": 1,
            "proposals": [
                {
                    "review_id": REVIEW_ID,
                    "organization_id": ORG_ID,
                    "object_type": "ASSET_DESCRIPTION_DRAFT",
                    "object_id": DRAFT_ID,
                    "requested_action": "APPROVE_DESCRIPTION",
                    "status": "PENDING",
                    "requested_by": PROPOSER,
                    "decided_by": None,
                    "decision_reason": None,
                    "decided_at": None,
                    "created_at": NOW,
                    "confidence": 0.91,
                    "evidence": [
                        {
                            "category": "source_evidence",
                            "claim": "column comments present on 9 of 12 columns",
                            "source": "metadata_scan",
                        }
                    ],
                    "diff": {
                        "review_id": REVIEW_ID,
                        "object_type": "ASSET_DESCRIPTION_DRAFT",
                        "object_id": DRAFT_ID,
                        "diffable": True,
                        "entries": [
                            {
                                "field": "description",
                                "change": "modified",
                                "before": "",
                                "after": "Order headers, one row per customer order.",
                            }
                        ],
                    },
                }
            ],
        }
    if method == "POST" and re.fullmatch(rf"/v1/governance/reviews/{_SEG}/decision/?", path):
        return _governance_review("APPROVED")

    # --- step 5: Ask --------------------------------------------------------
    if method == "GET" and re.fullmatch(rf"/v1/datasources/{_SEG}/agent-runs/?", path):
        return _page([_agent_run()], limit=50)
    if method == "GET" and re.fullmatch(rf"/v1/agent-runs/{_SEG}/grounding-receipts/?", path):
        # Pinned rather than synthesised: the answer panel renders each
        # fragment's business name and status, and a contract-shaped body with
        # nulls in those places crashes the route into its error boundary.
        return {
            "agent_run_id": AGENT_RUN_ID,
            "fragment_count": 1,
            "fragments": [
                {
                    "object_type": "BUSINESS_ANNOTATION",
                    "object_id": TABLE_ID,
                    "fragment_digest": "sha256:" + "0" * 64,
                    "annotation_version_id": None,
                    "annotation_version": None,
                    "annotation_status": None,
                    "business_name": "Orders",
                    "business_description": "One row per customer order.",
                    "digest_verified": True,
                }
            ],
        }
    if method == "GET" and re.fullmatch(rf"/v1/agent-runs/{_SEG}/?", path):
        return _agent_run()
    if method == "POST" and re.fullmatch(rf"/v1/datasources/{_SEG}/agent-analyses/?", path):
        return {
            "agent_run_id": AGENT_RUN_ID,
            "status": "COMPLETED",
            "generation_source": "GOVERNED_TOOL",
            "semantic_version": None,
            "policy_version": "v1",
            "step_trace": [],
            "retrieval_evidence": [],
            "plan_evidence": {},
            "execution": {
                "query_execution_id": "00000000-0000-0000-0000-00000000d001",
                "row_count": 1,
                "elapsed_ms": 42,
                "referenced_tables": ["public.orders"],
                "referenced_columns": ["public.orders.order_total"],
                "masked_columns": [],
                "normalized_sql": "SELECT sum(order_total) FROM public.orders",
                "rows": [{"sum": 1234}],
                "truncated": False,
                # `QueryResultTable` iterates this without a guard, so it must
                # be present and iterable even when empty.
                "column_lineage": [
                    {
                        "output_column": "sum",
                        "source_columns": [{"table": "public.orders", "column": "order_total"}],
                        "transformations": ["sum"],
                        "derived": True,
                    }
                ],
            },
            "explanation": "Summed the governed order_total measure over public.orders.",
        }

    # --- step 6: evidence ---------------------------------------------------
    if method == "GET" and re.fullmatch(rf"/v1/organizations/{_SEG}/audit-events/?", path):
        return _page(
            [
                {
                    # UX-16: this id is an integer, not a UUID.
                    "id": 101,
                    "organization_id": ORG_ID,
                    "principal_id": PROPOSER,
                    "principal_type": "USER",
                    "action": "governance_review.decide",
                    "resource_type": "GOVERNANCE_REVIEW",
                    "resource_id": REVIEW_ID,
                    "outcome": "SUCCESS",
                    "correlation_id": "journey-stub-correlation",
                    "source_ip": "10.0.0.4",
                    "details": {"decision": "APPROVE"},
                    "occurred_at": NOW,
                }
            ],
            limit=100,
        )
    return None


def _governance_review(status: str) -> dict[str, Any]:
    return {
        "id": REVIEW_ID,
        "organization_id": ORG_ID,
        "object_type": "ASSET_DESCRIPTION_DRAFT",
        "object_id": DRAFT_ID,
        "requested_action": "APPROVE_DESCRIPTION",
        "status": status,
        "requested_by": PROPOSER,
        "decided_by": None,
        "decision_reason": None,
        "decided_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _agent_run() -> dict[str, Any]:
    return {
        "id": AGENT_RUN_ID,
        "organization_id": ORG_ID,
        "datasource_id": DATASOURCE_ID,
        "principal_id": PRINCIPALS["analyst"],
        "status": "COMPLETED",
        "generation_source": "GOVERNED_TOOL",
        "model_route": None,
        "semantic_version": None,
        "policy_version": "v1",
        "query_execution_id": None,
        "step_trace": [],
        "retrieval_evidence": [],
        "grounding_fragment_digests": [],
        "plan_evidence": {},
        "recommended_tool_version_id": None,
        "failure_reason": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    examples: SpecExamples

    # --- request plumbing --------------------------------------------------

    def _identity(self) -> tuple[str, frozenset[str]] | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is simply no identity
            return None
        morsel = jar.get(IDENTITY_COOKIE)
        if morsel is None:
            return None
        name = morsel.value
        roles = IDENTITIES.get(name)
        if roles is None:
            return None
        return name, roles

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # The application's own middleware sets this on every response
        # (`src/aida/main.py`, `request_context`), and `ui-next/src/lib/http.ts`
        # reads it to build the support reference shown on a failure.
        self.send_header("X-Correlation-Id", "journey-stub-correlation")
        self.send_header(UPSTREAM_HEADER, UPSTREAM_VALUE)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        path = self.path.split("?", 1)[0]

        identity = self._identity()
        if identity is None:
            # No seat: the authenticating proxy in front did not establish one.
            self._send(401, {"detail": "authentication is required"})
            return
        name, roles = identity

        rule = rule_for(method, path)
        if rule is not None and rule.roles and roles.isdisjoint(rule.roles):
            # The exact shape `require_roles` raises in `src/aida/security.py`,
            # so the client decodes the same message a real refusal carries.
            self._send(
                403,
                {"detail": f"one of these roles is required: {', '.join(sorted(rule.roles))}"},
            )
            return

        override = _overrides(method, path, name)
        if override is not None:
            self._send(200, override)
            return
        self._send(200, self.examples.body_for(method, path))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("journey-stub: " + (format % args) + "\n")


def build_handler(spec_path: Path) -> type[_Handler]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return type("JourneyHandler", (_Handler,), {"examples": SpecExamples(spec)})


def main(argv: list[str]) -> int:
    port = 8000
    spec_path = Path(__file__).resolve().parents[1] / "Docs/90-reference/openapi-baseline.json"
    positional = [a for a in argv if not a.startswith("--")]
    if positional:
        port = int(positional[0])
    if "--spec" in argv:
        spec_path = Path(argv[argv.index("--spec") + 1])

    handler = build_handler(spec_path)
    # S104: binding every interface is the point -- this runs inside a
    # throwaway CI container that nginx reaches by its Docker network alias.
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)  # noqa: S104
    server.daemon_threads = True
    sys.stderr.write(f"journey-stub: listening on 0.0.0.0:{port} (spec {spec_path})\n")
    sys.stderr.flush()
    threading.current_thread().name = "journey-stub"
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
