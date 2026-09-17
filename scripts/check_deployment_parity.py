#!/usr/bin/env python3
"""Does the running deployment contain the reviewed source? (review F03)

Every existing gate in this repository compares source against source.
`scripts/openapi_diff.py` generates the spec in-process and diffs it against
the committed baseline; the CI `migrations` job counts `alembic heads` in the
tree. Both are green when the tree is self-consistent -- and both stay green
while a deployment runs an image built from a commit weeks older than the code
the review measured. That is exactly what happened: the committed baseline and
the working tree agreed on 405 paths / 503 schemas while the live
`/openapi.json` served 404 / 501, and the database sat one migration behind a
source head no gate looks at from the outside.

This script is the comparison nothing performed. It is read-only: it issues
`GET`s, one `SELECT version_num FROM alembic_version`, and (optionally) one
introspection command inside the running API container. It never migrates,
rebuilds, restarts or writes anything, and it never regenerates the OpenAPI
baseline -- the committed baseline is the promise being checked, so
regenerating it here would erase the finding instead of reporting it.

Four comparisons, each naming what differs rather than counting it:

    1. **Migrations.** The deployed `alembic_version` against the source
       heads, with every unapplied revision named in apply order.
    2. **HTTP surface.** The live `/openapi.json` path set and schema set
       against `Docs/90-reference/openapi-baseline.json`, reported as a
       symmetric difference by name. A path the baseline promises and the
       deployment does not serve is the operator-visible half of F03.
    3. **Readiness.** Every `required` dependency must be UP, and the
       `controls`, `delivery_backlog`, `outbox_backlog` and
       `workspace_authorization` signals are reported verbatim -- a readiness
       endpoint that is 200 while enforcing nothing is F11's failure mode.
    4. **Settings.** Every setting the source declares must exist in the
       deployed image's `Settings`. This is the check that catches an image
       older than the code *while the API answers 200 to everything*: the
       reviewed tree declares `footprint_metrics_interval_seconds`, so a
       deployed `Settings` without it cannot be running the reviewed
       scheduler, however healthy it looks.

**A skip is not a pass.** An unreachable deployment, an absent `docker`, a
container that cannot be introspected: each is UNKNOWN, never MATCH. The exit
code says which:

    0   parity -- every comparison ran and matched
    1   drift  -- at least one comparison ran and differed
    2   cannot tell -- something could not be measured, and nothing drifted

`--check` is the CI mode: it collapses 2 to 0, so a job that has no deployment
to reach reports "not measured" instead of failing the build, while real drift
still fails it. See the `deployment-parity` job in `.github/workflows/ci.yml`.

Usage:
    python scripts/check_deployment_parity.py
    python scripts/check_deployment_parity.py --base-url https://atlas.internal
    python scripts/check_deployment_parity.py --check            # CI mode
    python scripts/check_deployment_parity.py --json report.json

    # Kubernetes, where there is no local container to exec into: capture the
    # deployed field list once, then compare against it.
    kubectl exec deploy/aida-api -- python -c \
        'import json;from atlas.platform.config import Settings;\
         print(json.dumps(sorted(Settings.model_fields)))' > settings.json
    python scripts/check_deployment_parity.py --settings-json settings.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The source's own answer to "what settings does this code declare". Parsed from
# the AST of `Settings`, which is why it needs neither the application nor a
# configured environment to be importable -- and why one parser serves both this
# gate and the configuration inventory instead of two drifting lists.
from generate_configuration_inventory import _settings_fields  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_BASELINE = REPO_ROOT / "Docs" / "90-reference" / "openapi-baseline.json"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: The API container a local Compose stack names. Only used to read the deployed
#: `Settings` field list; `--settings-json` replaces it anywhere `docker exec`
#: is not the right door (Kubernetes, a remote host).
DEFAULT_CONTAINER = "aida-platform-api-1"

#: The local development URL that `.env.example` and `compose.yaml` already
#: publish on the host. Not a secret, and not a fallback that could reach a
#: production database by accident -- it names localhost.
FALLBACK_DATABASE_URL = "postgresql+asyncpg://aida:aida-local-only@localhost:5432/aida"

#: The readiness signals an operator has to see before deciding anything about a
#: deployment's maintenance loop or its outbound traffic. Reported verbatim, not
#: asserted: a queued backlog is not a failure, it is a fact with consequences.
REPORTED_SIGNAL_PREFIXES = (
    "delivery_backlog",
    "outbox_backlog",
    "workspace_authorization",
)

MATCH = "MATCH"
DRIFT = "DRIFT"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Finding:
    """One comparison between the deployment and the source."""

    name: str
    outcome: str  # MATCH | DRIFT | UNKNOWN
    detail: str = ""
    lines: tuple[str, ...] = ()


@dataclass
class Report:
    base_url: str
    findings: list[Finding] = field(default_factory=list)

    def record(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        print(f"  [{finding.outcome}] {finding.name}" + (f" -- {finding.detail}" if
                                                          finding.detail else ""), flush=True)
        for line in finding.lines:
            print(f"          {line}", flush=True)
        return finding

    @property
    def drifted(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == DRIFT]

    @property
    def unknown(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == UNKNOWN]


# --------------------------------------------------------------------------- #
# HTTP, reusing the verifier's client so the URL guard exists in one place
# --------------------------------------------------------------------------- #


def http_get_json(base_url: str, path: str, *, timeout: int = 30) -> tuple[int, Any]:
    """`GET` a JSON document, returning `(0, reason)` when nothing answered.

    Deliberately not `verify_end_to_end.Api`: this gate sends no identity
    headers, because `/health/ready` and `/openapi.json` need none and a parity
    check that only works with development headers would be useless against the
    production posture it most needs to be run against.
    """
    url = f"{base_url.rstrip('/')}{path}"
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"refusing to open non-HTTP URL: {url}")
    request = urllib.request.Request(  # noqa: S310 -- scheme checked above
        url, headers={"Accept": "application/json"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")[:400]
    except urllib.error.URLError as error:
        return 0, str(error.reason)
    except json.JSONDecodeError as error:
        return 0, f"response was not JSON: {error}"


# --------------------------------------------------------------------------- #
# 1. Migrations
# --------------------------------------------------------------------------- #


def source_heads_and_unapplied(deployed: frozenset[str]) -> tuple[frozenset[str], tuple[str, ...]]:
    """The source's migration heads, and what the deployment has not applied.

    Read from the same `migrations/` directory `alembic upgrade heads` walks, so
    a revision this names is a file a reviewer can open. A deployed revision the
    tree does not contain yields an empty unapplied tuple and is reported by the
    caller as the divergence it is -- the deployment is not ahead, it is
    somewhere else.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    heads = frozenset(script.get_heads())
    if not deployed or not deployed.issubset({r.revision for r in script.walk_revisions()}):
        return heads, ()
    unapplied = [
        revision.revision
        for revision in script.iterate_revisions(tuple(heads), tuple(deployed))
    ]
    unapplied.reverse()  # alembic walks newest-first; apply order reads better
    return heads, tuple(unapplied)


def compare_migrations(
    deployed: frozenset[str] | None,
    heads: frozenset[str],
    unapplied: tuple[str, ...],
    *,
    unreachable: str = "",
) -> Finding:
    name = "database schema is at the source head"
    if deployed is None:
        return Finding(name, UNKNOWN, f"could not read alembic_version: {unreachable}")
    if not deployed:
        return Finding(
            name,
            DRIFT,
            "alembic_version is empty: no migration has ever run against this database",
            tuple(f"unapplied: {revision}" for revision in sorted(heads)),
        )
    if deployed == heads:
        return Finding(name, MATCH, f"deployed={sorted(deployed)} == source heads")
    detail = f"deployed={sorted(deployed)} source heads={sorted(heads)}"
    if not unapplied:
        return Finding(
            name,
            DRIFT,
            detail,
            (
                "the deployed revision is not in this tree: the deployment was built "
                "from a different history, so it is not simply behind",
            ),
        )
    return Finding(
        name,
        DRIFT,
        detail,
        tuple(
            f"unapplied {index}/{len(unapplied)}: {revision}"
            for index, revision in enumerate(unapplied, start=1)
        ),
    )


async def _read_alembic_version(database_url: str) -> frozenset[str]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=False)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return frozenset(str(value) for value in rows.scalars().all())
    finally:
        await engine.dispose()


def read_deployed_revisions(database_url: str) -> tuple[frozenset[str] | None, str]:
    """`SELECT version_num FROM alembic_version`, or why it could not be read."""
    try:
        return asyncio.run(_read_alembic_version(database_url)), ""
    except Exception as error:  # noqa: BLE001 -- any driver/connect failure is "cannot tell"
        return None, f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"


def resolve_database_url(explicit: str) -> str:
    """`--database-url`, else `AIDA_DATABASE_URL`, else `.env`, else localhost.

    `.env` is read rather than imported so this works with `AIDA_ENVIRONMENT`
    unset and without constructing `Settings` -- which is the point, since a
    `Settings` this tree can construct says nothing about the deployment.
    """
    for candidate in (explicit, os.environ.get("AIDA_DATABASE_URL", "")):
        if candidate:
            return _async_driver(candidate)
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("AIDA_DATABASE_URL="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return _async_driver(value)
    return FALLBACK_DATABASE_URL


def _async_driver(url: str) -> str:
    """`postgresql://` names the same database; only the driver differs."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


# --------------------------------------------------------------------------- #
# 2. The HTTP surface, against the committed baseline
# --------------------------------------------------------------------------- #


def _named_difference(label: str, promised: set[str], served: set[str]) -> tuple[str, ...]:
    lines = [f"in the baseline, missing from the deployment ({label}): {name}"
             for name in sorted(promised - served)]
    lines += [f"served by the deployment, absent from the baseline ({label}): {name}"
              for name in sorted(served - promised)]
    return tuple(lines)


def compare_openapi(live: dict[str, Any], baseline: dict[str, Any]) -> list[Finding]:
    """Paths, schemas and the declared API version, each named not counted."""
    findings: list[Finding] = []

    live_paths = set(live.get("paths") or {})
    baseline_paths = set(baseline.get("paths") or {})
    path_lines = _named_difference("path", baseline_paths, live_paths)
    findings.append(
        Finding(
            "live OpenAPI paths match the committed baseline",
            MATCH if not path_lines else DRIFT,
            f"live={len(live_paths)} baseline={len(baseline_paths)}",
            path_lines,
        )
    )

    live_schemas = set((live.get("components") or {}).get("schemas") or {})
    baseline_schemas = set((baseline.get("components") or {}).get("schemas") or {})
    schema_lines = _named_difference("schema", baseline_schemas, live_schemas)
    findings.append(
        Finding(
            "live OpenAPI schemas match the committed baseline",
            MATCH if not schema_lines else DRIFT,
            f"live={len(live_schemas)} baseline={len(baseline_schemas)}",
            schema_lines,
        )
    )

    live_version = str((live.get("info") or {}).get("version"))
    baseline_version = str((baseline.get("info") or {}).get("version"))
    findings.append(
        Finding(
            "live API version matches the baseline's",
            MATCH if live_version == baseline_version else DRIFT,
            f"live={live_version} baseline={baseline_version}",
            ()
            if live_version == baseline_version
            else (
                "`info.version` is `aida.__version__`; equal versions with a different "
                "surface means the image is older than the code, not that the API evolved",
            ),
        )
    )
    return findings


# --------------------------------------------------------------------------- #
# 3. Readiness: dependencies, controls, and the backlog signals
# --------------------------------------------------------------------------- #


def compare_readiness(payload: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []

    required = payload.get("required") or {}
    down = {name: state for name, state in required.items() if state != "UP"}
    findings.append(
        Finding(
            "every required dependency is UP",
            MATCH if required and not down else DRIFT,
            str(required) if required else "readiness reported no required dependencies",
        )
    )

    controls = payload.get("controls") or {}
    findings.append(
        Finding(
            "readiness states its control posture",
            MATCH if controls else DRIFT,
            str(controls) if controls else "no controls reported",
        )
    )

    signals = payload.get("signals") or {}
    reported = tuple(
        f"{key} = {value}"
        for key, value in sorted(signals.items())
        if key.startswith(REPORTED_SIGNAL_PREFIXES) and not key.endswith(".duration_ms")
    )
    findings.append(
        Finding(
            "backlog and authorization signals are readable",
            MATCH if reported else UNKNOWN,
            f"status={payload.get('status')} version={payload.get('version')}",
            reported or ("readiness reported none of the signals this gate reports",),
        )
    )
    return findings


# --------------------------------------------------------------------------- #
# 4. Settings: is the image as new as the code?
# --------------------------------------------------------------------------- #


def source_setting_names() -> frozenset[str]:
    return frozenset(name for name, _annotation, _default in _settings_fields())


def read_deployed_settings(
    *, container: str, settings_json: Path | None, timeout: int = 60
) -> tuple[frozenset[str] | None, str]:
    """The deployed image's `Settings` field names, or why they are unknown.

    `docker exec` is one read-only introspection in the process that is already
    running; it constructs no `Settings`, touches no database and mutates
    nothing. Where there is no local container -- Kubernetes, a remote host --
    `--settings-json` takes a list captured with `kubectl exec` instead.
    """
    if settings_json is not None:
        try:
            data = json.loads(settings_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return None, f"could not read {settings_json}: {error}"
        if isinstance(data, dict):
            data = data.get("settings") or data.get("fields") or []
        # An empty list is refused rather than accepted: it would compare as
        # "the image declares nothing", which is a false drift report, and the
        # only way to get one is a capture that did not work.
        if not isinstance(data, list) or not data or not all(isinstance(i, str) for i in data):
            return None, f"{settings_json} is not a JSON list of setting names"
        return frozenset(data), ""

    program = (
        "import json;from atlas.platform.config import Settings;"
        "print(json.dumps(sorted(Settings.model_fields)))"
    )
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no interpolation
            ["docker", "exec", container, "python", "-c", program],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"docker exec {container} unavailable: {type(error).__name__}: {error}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        last = detail[-1][:200] if detail else "no output"
        return None, f"docker exec {container} failed: {last}"
    try:
        names = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return None, f"docker exec {container} returned no field list: {error}"
    if not isinstance(names, list) or not names:
        return None, f"docker exec {container} returned no usable field list"
    return frozenset(str(name) for name in names), ""


def compare_settings(
    source: frozenset[str],
    deployed: frozenset[str] | None,
    *,
    unreachable: str = "",
) -> Finding:
    name = "the deployed image declares every setting the source declares"
    if deployed is None:
        return Finding(name, UNKNOWN, unreachable or "not measured")
    missing = sorted(source - deployed)
    extra = sorted(deployed - source)
    detail = f"source={len(source)} deployed={len(deployed)}"
    if not missing and not extra:
        return Finding(name, MATCH, detail)
    lines = [f"declared in the source, absent from the image: {item}" for item in missing]
    lines += [f"present in the image, no longer in the source: {item}" for item in extra]
    if missing:
        lines.append(
            "a missing setting means the image predates the code that reads it, so the "
            "feature it configures is not in the running process at any value"
        )
    return Finding(name, DRIFT, detail, tuple(lines))


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def run(args: argparse.Namespace) -> Report:
    report = Report(base_url=args.base_url.rstrip("/"))

    print(f"\nComparing {report.base_url} against this working tree\n")

    print("Migrations")
    database_url = resolve_database_url(args.database_url)
    deployed_revisions, why = read_deployed_revisions(database_url)
    heads, unapplied = source_heads_and_unapplied(deployed_revisions or frozenset())
    report.record(compare_migrations(deployed_revisions, heads, unapplied, unreachable=why))

    print("\nHTTP surface")
    if not args.baseline.exists():
        report.record(
            Finding(
                "live OpenAPI matches the committed baseline",
                UNKNOWN,
                f"no baseline at {args.baseline}",
            )
        )
    else:
        status, live = http_get_json(report.base_url, "/openapi.json", timeout=args.timeout)
        if status != 200 or not isinstance(live, dict):
            report.record(
                Finding(
                    "live OpenAPI matches the committed baseline",
                    UNKNOWN,
                    f"/openapi.json did not answer: HTTP {status}: {str(live)[:200]}",
                )
            )
        else:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            for finding in compare_openapi(live, baseline):
                report.record(finding)

    print("\nReadiness")
    status, ready = http_get_json(report.base_url, "/health/ready", timeout=args.timeout)
    if status != 200 or not isinstance(ready, dict):
        report.record(
            Finding(
                "readiness is 200",
                UNKNOWN,
                f"HTTP {status}: {str(ready)[:200]}",
            )
        )
    else:
        for finding in compare_readiness(ready):
            report.record(finding)

    print("\nSettings")
    if args.skip_settings:
        report.record(
            Finding(
                "the deployed image declares every setting the source declares",
                UNKNOWN,
                "--skip-settings",
            )
        )
    else:
        deployed_settings, why = read_deployed_settings(
            container=args.container,
            settings_json=args.settings_json,
            timeout=args.timeout,
        )
        report.record(
            compare_settings(source_setting_names(), deployed_settings, unreachable=why)
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=(
            "The committed OpenAPI baseline to compare the live spec against "
            f"(default: {DEFAULT_BASELINE}). Never regenerated here."
        ),
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="Overrides AIDA_DATABASE_URL / .env for the alembic_version read.",
    )
    parser.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help=f"API container to introspect Settings in (default: {DEFAULT_CONTAINER}).",
    )
    parser.add_argument(
        "--settings-json",
        type=Path,
        default=None,
        help="A JSON list of the deployed Settings field names, instead of `docker exec`.",
    )
    parser.add_argument(
        "--skip-settings",
        action="store_true",
        help="Do not read the deployed Settings at all (reported UNKNOWN, never MATCH).",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--json", type=str, default="", help="write the findings as JSON here")
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "CI mode: exit non-zero only on observed drift. A deployment that could not be "
            "reached exits 0 with a NOT MEASURED banner, so a job with no deployment to "
            "check cannot fail the build for that reason."
        ),
    )
    args = parser.parse_args(argv)

    report = run(args)

    print(
        f"\n{len(report.findings) - len(report.drifted) - len(report.unknown)} matched, "
        f"{len(report.drifted)} drifted, {len(report.unknown)} not measured"
    )
    if report.drifted:
        print("\nDrift (the deployment is not running this tree):")
        for finding in report.drifted:
            print(f"  - {finding.name}: {finding.detail}")
    if report.unknown:
        print("\nNot measured (this is 'cannot tell', not 'parity'):")
        for finding in report.unknown:
            print(f"  - {finding.name}: {finding.detail}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "base_url": report.base_url,
                    "findings": [
                        {
                            "name": f.name,
                            "outcome": f.outcome,
                            "detail": f.detail,
                            "lines": list(f.lines),
                        }
                        for f in report.findings
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nfindings written to {args.json}")

    if report.drifted:
        print(
            "\n::error::the running deployment does not match this working tree. "
            "Deploying the reviewed commit is one action with the migration it "
            "carries -- see Docs/40-engineering/16-deployment-alignment-and-"
            "enablement-runbook.md."
        )
        return 1
    if report.unknown:
        if args.check:
            print(
                "\nNOT MEASURED: no reachable deployment for at least one comparison. "
                "Not failing the build for that -- run this against a deployment to "
                "get an answer."
            )
            return 0
        return 2
    print("\nParity: the deployment is running this tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
