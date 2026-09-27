"""Probe the running stack as each platform role; compare every answer with the declared contract.

Why it exists. `Docs/50-security/surface-control-matrix.md` is generated from the FastAPI app: for
each REST route it records the roles the `require_roles` dependency admits. That is a statement
about the code. This script is the matching statement about the *running* stack -- it sends the
same route as each of the platform's roles and checks that a role in the tuple is let through and
every other role is refused. A demo that says "an Auditor cannot approve" is then backed by a
measurement rather than by a table.

    python scripts/live_role_matrix.py [--base-url URL] [--org UUID] [--out FILE.json]

It writes nothing to the stack, by construction:

* A GET is sent with a random resource id. The role dependency runs before any handler logic, so an
  admitted role gets 404/422 (or 200 on a collection) and a refused role gets 403.
* A write (POST/PUT/PATCH/DELETE) is probed only when its OpenAPI operation declares a JSON request
  body, only as a role the matrix says is NOT admitted, and with the JSON string ``"probe"`` as the
  body. That is invalid for any object or list schema, so a role wrongly admitted would get 422,
  never a run of the handler. Write routes without a JSON body are not probed at all.
* It needs the development identity provider (the local stack), which trusts `X-Roles`; it checks
  `GET /v1/me` first and stops if the stack is running OIDC. It refuses a non-loopback host unless
  `--allow-remote` is given.

Outcomes per probe: an admitted role is expected to be answered by anything but 401/403/5xx; a
refused role is expected to get 401/403. A 403 for an admitted role on a GET is re-tried with the
scoping parameter the refusal names ("pass table_id to list one table's drafts"): if that succeeds
the route is reported as NARROWED, meaning the handler is stricter than the dependency for the
unscoped form. That is deliberate behaviour the matrix cannot see, not a failure. Anything else --
a refused role let through, an admitted role refused, a 5xx, a transport error -- is a FAILURE and
makes the exit code 1.

A demo user usually holds several roles. `--roles` therefore takes comma-separated identities, and
an identity may be a `+`-joined bundle (`--roles "DataSteward+Analyst+Viewer,Auditor+Viewer"`):
it is sent as `X-Roles: DataSteward,Analyst,Viewer` and is admitted where any member role is.

Routes whose roles are checked inside the handler body, and MCP and GraphQL surfaces, are outside
this contract ("none declared" in the matrix) and are not probed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
MATRIX = REPO / "Docs" / "50-security" / "surface-control-matrix.md"
OIDC = REPO / "src" / "aida" / "oidc.py"
SAMPLE_ORG_SLUG = "sample-bank"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ROW = re.compile(r"^\| `([A-Z]+) (/[^`]*)` \| (\w+) \| `[^`]*` \| ([^|]*) \|")
PASS_PARAM = re.compile(r"\bpass (\w+) to\b")


def platform_roles() -> list[str]:
    """The role catalog, read out of `aida.oidc.PLATFORM_ROLES` without importing the package."""
    for node in ast.walk(ast.parse(OIDC.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PLATFORM_ROLES" for t in node.targets
        ):
            literals = [n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant)]
            return [value for value in literals if isinstance(value, str)]
    sys.exit(f"PLATFORM_ROLES not found in {OIDC}")


def declared_routes() -> list[dict[str, Any]]:
    """Every REST route whose matrix cell names roles, as {method, path, roles}."""
    routes: list[dict[str, Any]] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        method, path, family, roles = match.groups()
        roles = roles.strip()
        if family != "REST" or roles in {"none declared", "unknown"}:
            continue
        routes.append(
            {"method": method, "path": path, "roles": {r.strip() for r in roles.split(",")}}
        )
    return routes


class Stack:
    def __init__(self, base_url: str, organization_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.organization_id = organization_id

    def call(
        self, method: str, path: str, role: str, body: str | None = None
    ) -> tuple[int, str]:
        headers = {
            "X-Principal-Id": f"probe-{role}",
            "X-Roles": role.replace("+", ","),
            "X-Organization-Id": self.organization_id,
        }
        data = None
        if body is not None:
            data = body.encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - scheme checked in main()
            self.base_url + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return response.status, response.read(4000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(400).decode("utf-8", "replace")
        except OSError as exc:
            return 0, str(exc)

    def fill(self, path: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name == "organization_id":
                return self.organization_id
            return str(uuid.uuid4()) if name.endswith("_id") or name == "id" else "probe"

        return re.sub(r"\{([^}]+)\}", substitute, path)


def json_body_routes(stack: Stack) -> set[tuple[str, str]]:
    url = stack.base_url + "/openapi.json"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        spec = json.loads(response.read())
    found: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            content = operation.get("requestBody", {}).get("content", {})
            if "application/json" in content:
                found.add((method.upper(), path))
    return found


def resolve_organization(base_url: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    request = urllib.request.Request(  # noqa: S310 - scheme checked in main()
        base_url.rstrip("/") + "/v1/organizations",
        headers={"X-Principal-Id": "probe-lookup", "X-Roles": "PlatformAdmin"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        organizations = json.loads(response.read())["items"]
    for organization in organizations:
        if organization["slug"] == SAMPLE_ORG_SLUG:
            return str(organization["id"])
    sys.exit(f"no organization with slug {SAMPLE_ORG_SLUG!r}; pass --org")


def classify(stack: Stack, job: dict[str, Any], status: int, detail: str) -> str:
    """`ok`, `narrowed`, or a failure kind."""
    denied = status in (401, 403)
    if status == 0 or status >= 500:
        return "error"
    if job["expected"] == "deny":
        return "ok" if denied else "leak"
    if not denied:
        return "ok"
    named = PASS_PARAM.search(detail)
    if job["method"] == "GET" and named:
        retry = f"{stack.fill(job['path'])}?{named.group(1)}={uuid.uuid4()}"
        retried, _ = stack.call("GET", retry, job["role"])
        if retried not in (0, 401, 403) and retried < 500:
            return "narrowed"
    return "refused"


def run(stack: Stack, roles: list[str], workers: int) -> dict[str, Any]:
    bodies = json_body_routes(stack)
    jobs: list[dict[str, Any]] = []
    skipped_writes = 0
    for route in declared_routes():
        is_write = route["method"] in WRITE_METHODS
        if is_write and (route["method"], route["path"]) not in bodies:
            skipped_writes += 1
            continue
        for role in roles:
            admitted = not set(role.split("+")).isdisjoint(route["roles"])
            if is_write and admitted:
                continue  # never send a write an admitted role could act on
            jobs.append(
                {
                    "method": route["method"],
                    "path": route["path"],
                    "role": role,
                    "expected": "allow" if admitted else "deny",
                    "body": '"probe"' if is_write else None,
                }
            )

    def probe(job: dict[str, Any]) -> dict[str, Any]:
        status, detail = stack.call(
            job["method"], stack.fill(job["path"]), job["role"], job["body"]
        )
        outcome = classify(stack, job, status, detail)
        record = {k: v for k, v in job.items() if k != "body"}
        return {**record, "status": status, "outcome": outcome}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, jobs))
    return {"results": results, "skipped_writes_without_json_body": skipped_writes}


def summarise(roles: list[str], results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {role: defaultdict(int) for role in roles}
    for item in results:
        kind = "read" if item["method"] == "GET" else "write"
        table[item["role"]][f"{kind}_{item['expected']}_{item['outcome']}"] += 1
    return {role: dict(counts) for role, counts in table.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--org", help="organization UUID; default: the sample-bank organization")
    parser.add_argument(
        "--roles", help="comma-separated identities (a+b bundles allowed); default: each role"
    )
    parser.add_argument("--out", type=Path, help="write the full result as JSON to this file")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--allow-remote", action="store_true", help="permit a non-loopback host")
    args = parser.parse_args(argv)

    if not args.base_url.startswith(("http://", "https://")):
        sys.exit(f"--base-url must be an http(s) URL, not {args.base_url!r}")
    host = urllib.parse.urlparse(args.base_url).hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"} and not args.allow_remote:
        sys.exit(f"{host} is not loopback and this sends ~5,000 requests; use --allow-remote")

    catalog = platform_roles()
    roles = [r.strip() for r in args.roles.split(",")] if args.roles else catalog
    unknown = [part for r in roles for part in r.split("+") if part not in catalog]
    if unknown:
        sys.exit(f"not in PLATFORM_ROLES: {', '.join(unknown)}")

    organization_id = resolve_organization(args.base_url, args.org)
    stack = Stack(args.base_url, organization_id)
    status, detail = stack.call("GET", "/v1/me", "PlatformAdmin")
    if status != 200 or json.loads(detail).get("identity_provider") != "DEVELOPMENT":
        sys.exit(
            f"GET /v1/me answered {status}; the stack must run the development identity provider"
        )

    outcome = run(stack, roles, args.workers)
    results = outcome["results"]
    per_role = summarise(roles, results)
    failures = [r for r in results if r["outcome"] not in {"ok", "narrowed"}]
    narrowed = [r for r in results if r["outcome"] == "narrowed"]

    total = len(declared_routes())
    print(f"organization {organization_id}; {total} declared REST routes; {len(results)} probes")
    skipped = outcome["skipped_writes_without_json_body"]
    print(f"write routes with no JSON body (not probed): {skipped}")
    header = ("role", "read ok", "read refused ok", "write refused ok", "narrowed", "FAIL")
    width = max(len(name) for name in roles)
    print(f"{header[0]:{width}s} " + " ".join(f"{h:>16s}" for h in header[1:]))
    for role in roles:
        c = per_role[role]
        bad = sum(v for k, v in c.items() if not k.endswith(("_ok", "_narrowed")))
        cells = (
            c.get("read_allow_ok", 0),
            c.get("read_deny_ok", 0),
            c.get("write_deny_ok", 0),
            c.get("read_allow_narrowed", 0),
            bad,
        )
        print(f"{role:{width}s} " + " ".join(f"{n:16d}" for n in cells))
    for item in narrowed[:10]:
        print(f"NARROWED {item['role']}: {item['method']} {item['path']} (needs a scoping param)")
    for item in failures[:40]:
        where = f"{item['role']} {item['method']} {item['path']}"
        print(f"FAIL {item['outcome']}: {where} -> {item['status']}")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "organization_id": organization_id,
                    "probes": len(results),
                    "per_role": per_role,
                    "narrowed": narrowed,
                    "failures": failures,
                    "skipped_writes_without_json_body": outcome["skipped_writes_without_json_body"],
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
