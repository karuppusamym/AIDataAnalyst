#!/usr/bin/env python3
"""Sign in as each OIDC bundle through the mock issuer's login form; check what the API makes of it.

Tracker R11-AUD07. `compose.oidc.yaml` defines the role bundles, `tests/test_oidc_demo_bundles.py`
proves a token carrying each claim set is verified and mapped correctly *in process*, and
`scripts/live_role_matrix.py` proves the running API's role guards with the development identity.
Nothing had yet done what a person does at the demo: type claims into the issuer's login form, get
a token back through the authorization-code + PKCE exchange, and use it against an API that is
verifying real signatures. This script does that, once per bundle, and records what came out.

    docker run -d --name atlas-oidc-verify-mock -p 127.0.0.1:8090:8080 \\
        -e 'JSON_CONFIG={"interactiveLogin":true}' ghcr.io/navikt/mock-oauth2-server:2.1.10
    python scripts/verify_oidc_bundles.py --spawn-api 8100 [--out report.json]

Per bundle it checks, from the token the issuer minted:

* `GET /v1/me` reports exactly the roles the overlay maps the bundle to, the persona the overlay
  maps its group to (or the default persona when it has no group), the organization typed into the
  form, and an OIDC identity provider;
* one route the matrix says the bundle is NOT admitted to answers 403 (a GET, so nothing is asked);
* one route it IS admitted to answers anything but 401/403 (a GET where one exists, otherwise a POST
  with a body no handler accepts, as `live_role_matrix.py` does for refused writes).

A token whose `roles` claim names a platform role that the mapping does not (`PlatformAdmin`) must
carry no roles at all. `--spawn-api PORT` starts a throwaway API from this checkout, in OIDC
mode with the overlay's own settings, on a scratch SQLite file, in a temporary directory so that
no `.env` (with its real keys) is read: it touches nothing else. Without it, `--api URL` checks a
stack that is already running in OIDC mode. The API and the issuer must be on loopback unless
`--allow-remote`.

Exit code 0 when every check matched, 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_role_matrix as matrix  # noqa: E402 - reads the contract the way the role sweep does

OVERLAY = REPO / "compose.oidc.yaml"
ISSUER = "http://localhost:8090/atlas"
CLIENT_ID = "atlas-ui-next"  # `VITE_OIDC_CLIENT_ID` in the overlay
REDIRECT = "http://localhost:3001/"
SCOPE = "openid profile email"
NORTHWIND = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87"

#: The group each bundle signs in with, as `Docs/walkthrough/roles-and-users.html` says and
#: `tests/test_oidc_demo_bundles.py` pins. `atlas-viewer` and `atlas-ingestor` carry no group, so
#: they open as the default persona; `atlas-operations` and `atlas-dataadmin` use the operator
#: group.
BUNDLE_GROUPS: dict[str, str | None] = {
    "atlas-admin": "atlas-admins",
    "atlas-steward": "atlas-stewards",
    "atlas-viewer": None,
    "atlas-reviewer": "atlas-reviewers",
    "atlas-auditor": "atlas-auditors",
    "atlas-analyst": "atlas-analysts",
    "atlas-operations": "atlas-admins",
    "atlas-dataadmin": "atlas-admins",
    "atlas-agentdev": "atlas-analysts",
    "atlas-ingestor": None,
}

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def load_overlay(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = document["services"]["api"]["environment"]
    return {
        "role_mappings": json.loads(env["AIDA_OIDC_ROLE_MAPPINGS"]),
        "persona_mappings": json.loads(env["AIDA_OIDC_PERSONA_MAPPINGS"]),
        "default_persona": env["AIDA_OIDC_DEFAULT_PERSONA"],
        "audience": env["AIDA_OIDC_AUDIENCE"],
        "env": env,
    }


def expected_persona(overlay: dict[str, Any], groups: list[str]) -> str:
    """The first mapped group in claim order picks the persona; none means the default."""
    for group in groups:
        if group in overlay["persona_mappings"]:
            return str(overlay["persona_mappings"][group])
    return str(overlay["default_persona"])


def sign_in(issuer: str, username: str, claims: dict[str, Any]) -> str:
    """The browser's flow, minus the browser: authorize -> the login form -> code -> token."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(8)
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPE,
        "state": state,
        "nonce": secrets.token_urlsafe(8),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with httpx.Client(follow_redirects=False, timeout=15) as client:
        page = client.get(f"{issuer}/authorize", params=params)
        page.raise_for_status()
        if 'name="claims"' not in page.text:
            raise RuntimeError(
                "the issuer's authorize page has no claims box: is interactive login on?"
            )
        submitted = client.post(
            f"{issuer}/authorize",
            params=params,
            data={"username": username, "claims": json.dumps(claims)},
        )
        location = submitted.headers.get("location", "")
        query = parse_qs(urlparse(location).query)
        if submitted.status_code != 302 or "code" not in query:
            raise RuntimeError(
                f"the login form did not redirect with a code ({submitted.status_code})"
            )
        if query.get("state", [""])[0] != state:
            raise RuntimeError("the issuer returned a different state")
        token = client.post(
            f"{issuer}/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": REDIRECT,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
        )
        token.raise_for_status()
        return str(token.json()["access_token"])


def fill(path: str, organization_id: str) -> str:
    """A concrete path for a route template: the organization where asked for, else a random id."""
    return re.sub(
        r"\{(\w+)\}",
        lambda m: organization_id if m.group(1) == "organization_id" else str(uuid.uuid4()),
        path,
    )


def pick(
    routes: list[dict[str, Any]], method: str, roles: set[str], admitted: bool
) -> dict[str, Any] | None:
    """The simplest route of that method the bundle is (not) admitted to: fewest path parameters."""
    wanted = [
        route
        for route in routes
        if route["method"] == method
        and bool(route["roles"] & roles) == admitted
        and (admitted or route["roles"].isdisjoint(roles))
    ]
    wanted.sort(key=lambda route: (route["path"].count("{"), route["path"]))
    return wanted[0] if wanted else None


def call(api: str, token: str, method: str, path: str, body: str | None = None) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"}
    content = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        content = body.encode()
    try:
        response = httpx.request(
            method, f"{api}{path}", headers=headers, content=content, timeout=20
        )
    except httpx.HTTPError as error:  # a timeout is a failed check, not a crash
        return 0, f"no answer: {type(error).__name__}"
    return response.status_code, response.text


def check_bundle(
    api: str,
    issuer: str,
    overlay: dict[str, Any],
    routes: list[dict[str, Any]],
    bundle: str,
    org: str,
) -> dict[str, Any]:
    group = BUNDLE_GROUPS.get(bundle)
    groups = [group] if group else []
    roles = set(overlay["role_mappings"][bundle])
    claims: dict[str, Any] = {"roles": [bundle], "organization_id": org}
    if groups:
        claims["groups"] = groups
    result: dict[str, Any] = {"bundle": bundle, "group": group, "expected_roles": sorted(roles)}
    try:
        token = sign_in(issuer, f"{bundle}.probe", claims)
    except (httpx.HTTPError, RuntimeError) as error:
        return {**result, "ok": False, "problem": f"sign-in failed: {error}"}

    status, text = call(api, token, "GET", "/v1/me")
    me = json.loads(text) if status == 200 else {}
    result["me_status"] = status
    result["me"] = {
        key: me.get(key) for key in ("roles", "persona", "organization_id", "identity_provider")
    }
    want_persona = expected_persona(overlay, groups)
    problems: list[str] = []
    if status != 200:
        problems.append(f"/v1/me answered {status}")
    else:
        if set(me.get("roles", [])) != roles:
            problems.append(f"roles {sorted(me.get('roles', []))} != {sorted(roles)}")
        if me.get("persona") != want_persona:
            problems.append(f"persona {me.get('persona')} != {want_persona}")
        if me.get("organization_id") != org:
            problems.append("organization does not match the claim typed into the form")
        if str(me.get("identity_provider", "")).upper() != "OIDC":
            problems.append(f"identity provider {me.get('identity_provider')}")

    refused = pick(routes, "GET", roles, admitted=False)
    if refused is None:
        result["refused"] = "n/a: the bundle is admitted to every GET route"
    else:
        path = fill(refused["path"], org)
        code, detail = call(api, token, "GET", path)
        result["refused"] = {"route": f"GET {refused['path']}", "status": code}
        if code != 403:
            problems.append(f"GET {refused['path']} answered {code}, wanted 403 ({detail[:80]})")

    method = "GET"
    admitted = pick(routes, "GET", roles, admitted=True)
    if admitted is None:
        method, admitted = "POST", pick(routes, "POST", roles, admitted=True)
    if admitted is None:
        result["admitted"] = "n/a: no route names any of the bundle's roles"
    else:
        path = fill(admitted["path"], org)
        code, detail = call(api, token, method, path, body='"probe"' if method == "POST" else None)
        result["admitted"] = {"route": f"{method} {admitted['path']}", "status": code}
        if code in {401, 403}:
            problems.append(f"{method} {admitted['path']} was refused ({code}): {detail[:80]}")
    return {**result, "ok": not problems, "problem": "; ".join(problems)}


def check_unmapped_claim(api: str, issuer: str, org: str) -> dict[str, Any]:
    """A token that NAMES a platform role the closed mapping does not map is granted nothing."""
    result: dict[str, Any] = {
        "bundle": "(a token naming PlatformAdmin)",
        "group": None,
        "expected_roles": [],
    }
    try:
        token = sign_in(
            issuer, "unmapped.probe", {"roles": ["PlatformAdmin"], "organization_id": org}
        )
    except (httpx.HTTPError, RuntimeError) as error:
        return {**result, "ok": False, "problem": f"sign-in failed: {error}"}
    status, text = call(api, token, "GET", "/v1/me")
    roles = json.loads(text).get("roles") if status == 200 else None
    result["me_status"] = status
    result["me"] = {"roles": roles}
    problems = []
    if status != 200 or roles != []:
        problems.append(f"expected an authenticated principal with no roles, got {status} {roles}")
    code, _ = call(api, token, "GET", fill("/v1/organizations/{organization_id}/datasources", org))
    result["refused"] = {
        "route": "GET /v1/organizations/{organization_id}/datasources",
        "status": code,
    }
    if code != 403:
        problems.append(f"an admin route answered {code}, wanted 403")
    return {**result, "ok": not problems, "problem": "; ".join(problems)}


def spawn_api(
    port: int, overlay: dict[str, Any], issuer: str
) -> tuple[subprocess.Popen[bytes], str]:
    """A throwaway API from this checkout: OIDC on, the overlay's mappings, a scratch database."""
    scratch = tempfile.mkdtemp(prefix="atlas-oidc-verify-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIDA_")}
    env.update(
        {
            "AIDA_ENVIRONMENT": "development",
            "AIDA_IDENTITY_PROVIDER": "oidc",
            "AIDA_OIDC_ISSUER": issuer,
            "AIDA_OIDC_JWKS_URL": f"{issuer}/jwks",
            "AIDA_OIDC_AUDIENCE": overlay["audience"],
            "AIDA_OIDC_ROLE_MAPPINGS": json.dumps(overlay["role_mappings"]),
            "AIDA_OIDC_PERSONA_MAPPINGS": json.dumps(overlay["persona_mappings"]),
            "AIDA_OIDC_DEFAULT_PERSONA": overlay["default_persona"],
            "AIDA_DATABASE_URL": f"sqlite+aiosqlite:///{Path(scratch, 'verify.db').as_posix()}",
            "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        }
    )
    # The scratch database needs its tables: the API looks a token's id up in the revocation table
    # before it admits it, and against an empty file that lookup fails and reads as a refused
    # token.
    subprocess.run(  # noqa: S603 - our own interpreter, a fixed program
        [
            sys.executable,
            "-c",
            "import asyncio, os\n"
            "from sqlalchemy.ext.asyncio import create_async_engine\n"
            "import aida.models  # noqa: F401 - registers every table on the metadata\n"
            "from aida.db import Base\n"
            "async def go():\n"
            "    engine = create_async_engine(os.environ['AIDA_DATABASE_URL'])\n"
            "    async with engine.begin() as connection:\n"
            "        await connection.run_sync(Base.metadata.create_all)\n"
            "    await engine.dispose()\n"
            "asyncio.run(go())\n",
        ],
        cwd=scratch,
        env=env,
        check=True,
        capture_output=True,
    )
    # Its output goes to a FILE, never a pipe: the API's console tracing exporter writes JSON for
    # every request, a Windows pipe holds about 4 KB, and a process blocked on a full pipe answers
    # nothing.
    log_path = Path(scratch, "api.log")
    log = log_path.open("wb")
    process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aida.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=scratch,  # not the repo: no `.env`, so no real provider keys reach this process
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    api = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"the throwaway API exited early:\n{output[-1500:]}")
        try:
            if httpx.get(f"{api}/health/live", timeout=2).status_code == 200:
                return process, api
        except httpx.HTTPError:
            time.sleep(0.5)
    process.terminate()
    raise RuntimeError("the throwaway API did not answer /health/live within 240 s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--issuer", default=ISSUER, help="the issuer URL the BROWSER uses")
    parser.add_argument("--api", help="an API already running in OIDC mode")
    parser.add_argument("--spawn-api", type=int, metavar="PORT", help="start a throwaway API here")
    parser.add_argument("--org", default=NORTHWIND, help="organization id typed into the form")
    parser.add_argument("--overlay", type=Path, default=OVERLAY)
    parser.add_argument("--out", type=Path, help="write the full result as JSON to this file")
    parser.add_argument("--allow-remote", action="store_true", help="permit non-loopback hosts")
    args = parser.parse_args(argv)
    if bool(args.api) == bool(args.spawn_api):
        parser.error("give exactly one of --api URL or --spawn-api PORT")
    for url in (args.issuer, args.api):
        if url and urlparse(url).hostname not in LOOPBACK and not args.allow_remote:
            sys.exit(f"{url} is not on loopback; pass --allow-remote if that is intended")

    overlay = load_overlay(args.overlay)
    routes = matrix.declared_routes()
    process = None
    api = args.api
    try:
        if args.spawn_api:
            process, api = spawn_api(args.spawn_api, overlay, args.issuer)
        assert api is not None
        api = api.rstrip("/")
        results = [
            check_bundle(api, args.issuer, overlay, routes, bundle, args.org)
            for bundle in overlay["role_mappings"]
        ]
        results.append(check_unmapped_claim(api, args.issuer, args.org))
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=20)

    print(f"issuer {args.issuer}; API {api}; {len(results)} sign-ins\n")
    print(f"{'bundle':38} {'persona':9} {'me':>4} {'refused':>8} {'admitted':>9}  verdict")
    for row in results:
        me = row.get("me", {})
        refused = row.get("refused")
        admitted = row.get("admitted")
        print(
            f"{row['bundle']:38} {str(me.get('persona', '-')):9} {row.get('me_status', '-')!s:>4} "
            f"{(refused['status'] if isinstance(refused, dict) else '-')!s:>8} "
            f"{(admitted['status'] if isinstance(admitted, dict) else '-')!s:>9}  "
            f"{'ok' if row['ok'] else 'FAIL: ' + row['problem']}"
        )
    failures = [row for row in results if not row["ok"]]
    print(f"\n{len(results) - len(failures)} of {len(results)} sign-ins matched")
    if args.out:
        args.out.write_text(
            json.dumps({"issuer": args.issuer, "results": results}, indent=2), encoding="utf-8"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
