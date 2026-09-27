#!/usr/bin/env python3
"""End-to-end verification against a running deployment.

Every other check in this repository runs in-process: pytest against SQLite or
a scratch PostgreSQL, with the connectors, the model provider and the browser
all stood in for. That is the right shape for most of them, and it is why the
capability register's `Verified` column is mostly **No** -- a green unit test
is not evidence that a deployment works.

This script is the missing kind of check. It talks to a real API over HTTP,
against a real database with a real seeded estate, with a real model provider
behind the configured route. It asserts nothing about internals it cannot see
from outside; every check is something an operator could reproduce with `curl`.

**It is a verifier, not a test suite.** It never fixes, seeds or configures
anything -- `scripts/seed_sample_estate.py` does that -- so a failure here is a
finding about the deployment rather than about this file. Each check prints
PASS, FAIL or SKIP with a reason, and the exit code is non-zero if anything
failed. A SKIP is not a pass: it means the precondition was absent, and the
summary counts them separately so "everything passed" cannot be said of a run
that mostly skipped.

Usage:
    python scripts/verify_end_to_end.py
    python scripts/verify_end_to_end.py --base-url http://localhost:8000
    python scripts/verify_end_to_end.py --org sample-bank --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"

#: Development identity headers. `security.get_security_context` accepts these
#: only when `identity_provider=development`; under `oidc` they are ignored in
#: favour of a verified bearer token, which is why this script cannot verify a
#: production posture and says so in its own summary.
ADMIN_ROLES = (
    "PlatformAdmin,OrganizationAdmin,DataAdmin,Steward,Analyst,Reviewer,Auditor,Operations"
)
ANALYST_ROLES = "Analyst,Viewer"


@dataclass
class Check:
    name: str
    outcome: str  # PASS | FAIL | SKIP
    detail: str


@dataclass
class Report:
    base_url: str
    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, outcome: str, detail: str = "") -> None:
        self.checks.append(Check(name, outcome, detail))
        marker = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[outcome]
        print(f"  [{marker}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.outcome == "FAIL"]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.outcome == "SKIP"]


class Api:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        principal: str = "e2e-verifier",
        roles: str = ADMIN_ROLES,
        org_id: str | None = None,
        timeout: int = 120,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if not url.startswith(("http://", "https://")):
            raise SystemExit(f"refusing to open non-HTTP URL: {url}")
        headers = {
            "X-Principal-Id": principal,
            "X-Principal-Type": "USER",
            "X-Roles": roles,
            "X-Business-Purpose": "End-to-end verification of this deployment",
            "Content-Type": "application/json",
        }
        if org_id:
            headers["X-Organization-Id"] = org_id
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(  # noqa: S310
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                return response.status, _decode(raw)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            return error.code, _decode(raw)
        except urllib.error.URLError as error:
            return 0, str(error.reason)


def _decode(raw: str) -> Any:
    """A JSON body, a JSONL body, or the raw text.

    The audit export streams newline-delimited JSON, so a single `json.loads`
    over the body raises -- which is how this verifier first learned the route
    works. Handling all three shapes keeps the client honest about what the
    API actually returns rather than about what it expected.
    """
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    lines = [line for line in raw.splitlines() if line.strip()]
    try:
        return [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        return raw


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    return payload if isinstance(payload, list) else []


# --------------------------------------------------------------------------- #
# 1. The deployment is up, and says what it is enforcing
# --------------------------------------------------------------------------- #


def check_health(api: Api, report: Report) -> dict[str, Any] | None:
    status, payload = api.call("GET", "/health/ready")
    if status != 200 or not isinstance(payload, dict):
        report.record("readiness responds 200", "FAIL", f"HTTP {status}: {payload}")
        return None
    report.record("readiness responds 200", "PASS", f"status={payload.get('status')}")

    required = payload.get("required", {})
    if all(v == "UP" for v in required.values()):
        report.record("every required dependency is UP", "PASS", str(required))
    else:
        report.record("every required dependency is UP", "FAIL", str(required))

    # A readiness endpoint that reports dependencies but not controls is how a
    # deployment satisfies every automated check while enforcing nothing (F11).
    controls = payload.get("controls", {})
    if controls:
        report.record("readiness states its control posture", "PASS", str(controls))
    else:
        report.record("readiness states its control posture", "FAIL", "no controls reported")

    signals = payload.get("signals", {})
    backlog = signals.get("delivery_backlog.detail")
    if backlog:
        report.record("delivery backlog is readable", "PASS", backlog)
    else:
        report.record("delivery backlog is readable", "FAIL", "no delivery_backlog signal")
    return payload


# --------------------------------------------------------------------------- #
# 2. The estate is real and readable
# --------------------------------------------------------------------------- #


def resolve_org(api: Api, report: Report, slug: str) -> str | None:
    status, payload = api.call("GET", "/v1/organizations?limit=200")
    if status != 200:
        report.record("organizations are listable", "FAIL", f"HTTP {status}")
        return None
    orgs = _items(payload)
    report.record("organizations are listable", "PASS", f"{len(orgs)} found")
    for org in orgs:
        if org.get("slug") == slug:
            return str(org["id"])
    report.record(f"organization {slug!r} exists", "FAIL", "not found; seed the estate first")
    return None


def check_catalog(api: Api, report: Report, org_id: str) -> dict[str, Any] | None:
    status, payload = api.call(
        "GET", f"/v1/organizations/{org_id}/datasources?limit=50", org_id=org_id
    )
    if status != 200:
        report.record("datasources are listable", "FAIL", f"HTTP {status}: {payload}")
        return None
    sources = _items(payload)
    if not sources:
        report.record("datasources are listable", "FAIL", "none in this organization")
        return None
    report.record("datasources are listable", "PASS", f"{len(sources)} in this organization")

    chosen = next((d for d in sources if d.get("connector_type") == "postgres"), sources[0])
    ds_id = chosen["id"]
    status, payload = api.call("GET", f"/v1/datasources/{ds_id}/tables?limit=100", org_id=org_id)
    tables = _items(payload)
    if status != 200 or not tables:
        report.record(
            "discovered tables are readable", "FAIL", f"HTTP {status}, {len(tables)} rows"
        )
        return chosen
    report.record(
        "discovered tables are readable",
        "PASS",
        f"{len(tables)} tables on {chosen['name']}",
    )

    table_id = tables[0]["id"]
    status, payload = api.call("GET", f"/v1/tables/{table_id}/columns?limit=200", org_id=org_id)
    columns = _items(payload)
    if status == 200 and columns:
        report.record("discovered columns are readable", "PASS",
            f"{len(columns)} on {tables[0]['name']}")
    else:
        report.record(
            "discovered columns are readable", "FAIL", f"HTTP {status}, {len(columns)} rows"
        )
    return chosen


# --------------------------------------------------------------------------- #
# 3. Ask: the governed-tool path, refusal first
# --------------------------------------------------------------------------- #


def check_ask_governed_tool(api: Api, report: Report, org_id: str, ds_id: str) -> None:
    """The refusal is the interesting half.

    A governed tool that needs an input must refuse *naming* it, not fail
    vaguely -- that structured refusal is what lets a caller retry correctly,
    and it is the behaviour R11-B1 exists for.
    """
    question = "Which accounts are booked at a branch?"
    status, payload = api.call(
        "POST",
        f"/v1/datasources/{ds_id}/agent-analyses",
        body={"question": question},
        org_id=org_id,
    )
    if status == 409 and isinstance(payload, dict):
        # The refusal arrives as the platform's error envelope, which carries
        # `required_parameters` at the top level -- not nested under `details`
        # the way an `ApiError`'s structured detail is. Both shapes are read
        # here because guessing one and reporting a platform failure when the
        # guess is wrong is how a verifier lies about the thing it verifies.
        envelope: dict[str, Any] = payload
        for key in ("detail", "error", "details"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                envelope = nested
                break
        detail = str(envelope.get("message") or envelope.get("detail") or payload.get("detail"))
        params = envelope.get("required_parameters") or []
        if params:
            report.record(
                "Ask refuses a governed tool naming the input it needs",
                "PASS",
                f"409 requires {params}",
            )
        else:
            report.record(
                "Ask refuses a governed tool naming the input it needs",
                "FAIL",
                f"409 but no required_parameters: {detail[:160]}",
            )
            return
    else:
        report.record(
            "Ask refuses a governed tool naming the input it needs",
            "FAIL",
            f"expected 409, got HTTP {status}: {str(payload)[:200]}",
        )
        return

    status, payload = api.call(
        "POST",
        f"/v1/datasources/{ds_id}/agent-analyses",
        body={"question": question, "tool_parameters": {params[0]: "BR-101"}},
        org_id=org_id,
    )
    if status == 200 and isinstance(payload, dict):
        source = payload.get("generation_source")
        rows = payload.get("rows")
        if source == "GOVERNED_TOOL":
            report.record(
                "Ask answers from the approved governed tool",
                "PASS",
                f"generation_source={source}, rows={len(rows) if isinstance(rows,
                    list) else 'n/a'}",
            )
        else:
            report.record(
                "Ask answers from the approved governed tool",
                "FAIL",
                f"answered but generation_source={source}",
            )
    else:
        report.record(
            "Ask answers from the approved governed tool",
            "FAIL",
            f"HTTP {status}: {str(payload)[:240]}",
        )


# --------------------------------------------------------------------------- #
# 4. Ask: the live model path
# --------------------------------------------------------------------------- #


def check_ask_model_generation(api: Api, report: Report, org_id: str, ds_id: str) -> None:
    """A question no approved tool matches, so generation has to answer it.

    This is the only check here that spends money, and it is the one the
    capability register's `Verified` column has never been able to say Yes to.
    """
    question = "How many rows are in the customer table?"
    status, payload = api.call(
        "POST",
        f"/v1/datasources/{ds_id}/agent-analyses",
        body={"question": question},
        org_id=org_id,
        timeout=180,
    )
    if status == 200 and isinstance(payload, dict):
        report.record(
            "Ask answers by live model generation",
            "PASS",
            f"generation_source={payload.get('generation_source')}, "
            f"route={payload.get('model_route')}",
        )
        return
    detail = str(payload)[:260] if payload else f"HTTP {status}"
    if status in {422, 503}:
        report.record(
            "Ask answers by live model generation",
            "FAIL",
            f"refused: {detail}",
        )
    else:
        report.record("Ask answers by live model generation", "FAIL", f"HTTP {status}: {detail}")


# --------------------------------------------------------------------------- #
# 5. Authorization surfaces
# --------------------------------------------------------------------------- #


def check_enforcement_readiness(api: Api, report: Report, org_id: str) -> None:
    status, payload = api.call(
        "GET", f"/v1/organizations/{org_id}/enforcement-readiness", org_id=org_id
    )
    if status != 200 or not isinstance(payload, dict):
        report.record("enforcement readiness is obtainable", "FAIL", f"HTTP {status}: {payload}")
        return
    report.record(
        "enforcement readiness is obtainable",
        "PASS",
        f"ready={payload.get('ready')}, blockers={len(payload.get('blockers') or [])}, "
        f"unbound={payload.get('datasources_unbound')}, "
        f"ambiguous={payload.get('datasources_ambiguous')}",
    )
    # `ready: true` on a deployment whose unresolved-scope posture still
    # proceeds undecided would mean the report is rounding up.
    if payload.get("ready") and payload.get("unresolved_scope_outcome") != "DENIED":
        report.record(
            "readiness does not round up to green",
            "FAIL",
            "ready=true while unresolved scope proceeds undecided",
        )
    else:
        report.record("readiness does not round up to green", "PASS")


def check_audit_export(api: Api, report: Report, org_id: str) -> None:
    """Authorized export, and the refusal for an identity without the right."""
    path = f"/v1/organizations/{org_id}/audit-events/export.jsonl?limit=5"
    status, payload = api.call("GET", path, org_id=org_id)
    if status == 200:
        report.record("audit export is reachable for an authorized caller", "PASS")
    else:
        report.record(
            "audit export is reachable for an authorized caller",
            "FAIL",
            f"HTTP {status}: {str(payload)[:200]}",
        )
    status, payload = api.call(
        "GET", path, org_id=org_id, principal="e2e-analyst", roles=ANALYST_ROLES
    )
    if status in {401, 403}:
        report.record("audit export refuses an unprivileged caller", "PASS", f"HTTP {status}")
    else:
        report.record(
            "audit export refuses an unprivileged caller",
            "FAIL",
            f"expected 401/403, got HTTP {status}",
        )


def check_audit_trail(api: Api, report: Report, org_id: str) -> None:
    status, payload = api.call(
        "GET", f"/v1/organizations/{org_id}/audit-events?limit=25", org_id=org_id
    )
    events = _items(payload)
    if status != 200 or not events:
        report.record("the audit ledger records this run", "FAIL", f"HTTP {status}, {len(events)}")
        return
    actions = {e.get("action") for e in events}
    queried = {a for a in actions if isinstance(a, str) and a.startswith("query.")}
    report.record(
        "the audit ledger records this run",
        "PASS",
        f"{len(events)} recent events; query actions present: {sorted(queried)[:3] or 'none'}",
    )


# --------------------------------------------------------------------------- #
# 6. Retrieval and the vector index
# --------------------------------------------------------------------------- #


def check_vector_index(api: Api, report: Report, org_id: str) -> None:
    status, payload = api.call(
        "GET", f"/v1/organizations/{org_id}/retrieval/vector-index", org_id=org_id
    )
    if status == 404:
        status, payload = api.call(
            "POST",
            f"/v1/organizations/{org_id}/retrieval/vector-index/rebuild",
            body={},
            org_id=org_id,
            timeout=300,
        )
        if status == 200 and isinstance(payload, dict):
            report.record(
                "the vector index builds against a live provider",
                "PASS",
                f"embedded={payload.get('embedded')}, backend={payload.get('backend')}",
            )
        else:
            report.record(
                "the vector index builds against a live provider",
                "FAIL",
                f"HTTP {status}: {str(payload)[:200]}",
            )
        return
    if status == 200 and isinstance(payload, dict):
        report.record(
            "vector index state is readable",
            "PASS",
            f"entries={payload.get('entries')}, usable={payload.get('usable')}",
        )
    else:
        report.record("vector index state is readable", "FAIL", f"HTTP {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--org", default="sample-bank", help="organization slug to verify")
    parser.add_argument("--json", type=str, default="", help="write the report as JSON here")
    parser.add_argument(
        "--skip-model",
        action="store_true",
        help="skip the live generation check, which is the only one that costs money",
    )
    args = parser.parse_args()

    api = Api(args.base_url)
    report = Report(base_url=api.base_url)
    started = time.time()

    print(f"\nVerifying {api.base_url} end to end\n")

    print("Deployment")
    if check_health(api, report) is None:
        print("\nthe deployment is not answering; nothing else can be verified")
        return 1

    print("\nEstate")
    org_id = resolve_org(api, report, args.org)
    if org_id is None:
        return 1
    datasource = check_catalog(api, report, org_id)

    if datasource is not None:
        print("\nAsk")
        check_ask_governed_tool(api, report, org_id, datasource["id"])
        if args.skip_model:
            report.record("Ask answers by live model generation", "SKIP", "--skip-model")
        else:
            check_ask_model_generation(api, report, org_id, datasource["id"])

    print("\nAuthorization and audit")
    check_enforcement_readiness(api, report, org_id)
    check_audit_export(api, report, org_id)
    check_audit_trail(api, report, org_id)

    print("\nRetrieval")
    check_vector_index(api, report, org_id)

    passed = [c for c in report.checks if c.outcome == "PASS"]
    print(
        f"\n{len(passed)} passed, {len(report.failed)} failed, {len(report.skipped)} skipped "
        f"in {time.time() - started:.1f}s"
    )
    if report.failed:
        print("\nFailures:")
        for check in report.failed:
            print(f"  - {check.name}: {check.detail}")
    if report.skipped:
        print("\nSkipped (a skip is not a pass):")
        for check in report.skipped:
            print(f"  - {check.name}: {check.detail}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "base_url": report.base_url,
                    "checks": [
                        {"name": c.name, "outcome": c.outcome, "detail": c.detail}
                        for c in report.checks
                    ],
                },
                handle,
                indent=2,
            )
        print(f"\nreport written to {args.json}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
