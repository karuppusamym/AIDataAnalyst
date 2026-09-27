#!/usr/bin/env python3
"""Certify every connector's capability flags and write the result INV-9 derives them from.

INV-9: a connector advertises only behaviour that is implemented and passing its
certification, and "capability flags are derived from the certification result, not
hand-declared". This script *is* the certification. It runs one probe per (connector, flag)
and writes:

* `src/aida/connectors/capability_certification.json` -- the result. It lives under `src/`
  because the platform derives every advertised flag from it at import time and the image
  ships `src`, not `Docs`. Every row carries its status (CERTIFIED / NOT_CERTIFIED /
  NOT_APPLICABLE), the **evidence tier** it stands on (LIVE = probed against a real running
  engine; FIXTURE = proven against the connector's own driver double, never labelled live),
  what was probed, what came back, and the tests that produced it. Each connector also
  records a fingerprint of the code it was evaluated against.
* `Docs/90-reference/connector-capability-certification.md` -- the rendered page.

**Where the probes come from.**

* PostgreSQL and SQL Server are probed LIVE, by `tests/test_c14_live_capability_probes.py`,
  against the local sample containers (a private scratch database per engine, dropped after).
  There is no committed password: SQL Server is reached through `docker exec ... sqlcmd`
  with the container's own credentials, as `tests/test_footprint_journey.py` does.
* Oracle, Snowflake, BigQuery and Databricks have no reachable instance (tracker R11-B5), so
  they are certified at the FIXTURE tier by `tests/test_c14_fixture_capability_probes.py`,
  which names the existing driver-double tests it relies on and adds probes where none existed.

**A failing probe never lowers a flag.** Lowering `explain` makes the query gateway refuse
execution against that engine, which is an operator's decision. A flag the connector claims
but the probes did not certify is recorded, with what failed and its evidence, in the
result's `uncertified_claims`, reported below in capitals, and kept advertised. To lower it,
lower the claim in the connector's `DEFAULT_CAPABILITIES`.

Usage (from the repository root):

    .venv/Scripts/python.exe scripts/certify_connector_capabilities.py           # stale ones
    .venv/Scripts/python.exe scripts/certify_connector_capabilities.py --all
    .venv/Scripts/python.exe scripts/certify_connector_capabilities.py --connector postgres
    .venv/Scripts/python.exe scripts/certify_connector_capabilities.py --check   # CI-safe
    .venv/Scripts/python.exe scripts/certify_connector_capabilities.py --stdout

By default only connectors whose code fingerprint (or claim) no longer matches the committed
result are re-certified, so a machine without the sample containers can still refresh a
fixture-tier connector. `--check` needs no database: LIVE evidence is read from the committed
result and is only ever re-produced by running this script. It fails when a connector's code
has changed since it was certified, when the result contradicts itself or the current claims,
or when the rendered page is stale.

The probes run in a child pytest process with `AIDA_ENVIRONMENT` removed, exactly as CI runs
the suite. Nothing here calls a paid model.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from aida.connectors.base import ConnectorCapabilities  # noqa: E402
from aida.connectors.capability_certification import (  # noqa: E402
    CAPABILITY_FLAGS,
    CERTIFICATION_SCHEMA_VERSION,
    CERTIFICATION_SUITE,
    REASON_NOT_EXERCISED,
    REASON_NOT_IMPLEMENTED,
    REASON_PROBE_FAILED,
    RESULT_PATH,
    STATUS_CERTIFIED,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_CERTIFIED,
    TIER_FIXTURE,
    TIER_LIVE,
    CertificationResult,
    CertificationResultError,
    ConnectorCertification,
    FlagCertification,
    UncertifiedClaim,
    compute_fingerprint,
    derive_flags,
    render_markdown,
    result_to_json,
    stale_connectors,
    verify_result,
)
from aida.connectors.registry import connector_registry  # noqa: E402

_MD_PATH = REPO_ROOT / "Docs" / "90-reference" / "connector-capability-certification.md"
LIVE_MODULE = "tests/test_c14_live_capability_probes.py"
#: The engines with a reachable sample container. Everything else is FIXTURE-tier.
LIVE_CONNECTORS = ("postgres", "sqlserver")
WORKS = "WORKS"


class RunnerError(Exception):
    """The certification could not be produced honestly; nothing is written."""


@dataclass(frozen=True)
class Plan:
    """How one (connector, flag) cell is evidenced."""

    kind: str  # LIVE | PROBE | EXISTING | NOT_APPLICABLE | UNPROBED
    tier: str | None
    what: str
    tests: tuple[str, ...] = ()
    #: What a pass means: WORKS, or ABSENT for existing tests that assert a deliberate absence.
    verdict: str = WORKS
    basis: str = ""


def _implemented() -> list[str]:
    return sorted(
        d.connector_type
        for d in connector_registry.definitions
        if d.implementation_status == "IMPLEMENTED"
    )


def _claims() -> dict[str, ConnectorCapabilities]:
    return {
        d.connector_type: ConnectorCapabilities(**d.claimed_capabilities)
        for d in connector_registry.definitions
        if d.implementation_status == "IMPLEMENTED"
    }


def _load_committed() -> CertificationResult | None:
    try:
        from aida.connectors.capability_certification import load_certification_result

        return load_certification_result()
    except CertificationResultError:
        return None


# --- the plan: which test evidences which cell ---------------------------------------------------


def _plan(connectors: list[str]) -> dict[tuple[str, str], Plan]:
    # Imported here: it pulls in the driver-double helpers of four other test modules, which
    # `--check` and `--stdout` have no use for.
    from tests import test_c14_fixture_capability_probes as fixtures

    plan: dict[tuple[str, str], Plan] = {}
    for connector in connectors:
        for flag in CAPABILITY_FLAGS:
            key = (connector, flag)
            if connector in LIVE_CONNECTORS:
                node = f"{LIVE_MODULE}::test_live_probe[{connector}-{flag}]"
                plan[key] = Plan("LIVE", TIER_LIVE, "", (node,))
            elif key in fixtures.FIXTURE_PROBES:
                probe = fixtures.FIXTURE_PROBES[key]
                plan[key] = Plan("PROBE", TIER_FIXTURE, probe.what, (probe.node_id,))
            elif key in fixtures.EXISTING_EVIDENCE:
                existing = fixtures.EXISTING_EVIDENCE[key]
                plan[key] = Plan(
                    "EXISTING", TIER_FIXTURE, existing.what, existing.tests, existing.verdict
                )
            elif key in fixtures.NOT_APPLICABLE:
                plan[key] = Plan("NOT_APPLICABLE", None, "", basis=fixtures.NOT_APPLICABLE[key])
            elif key in fixtures.UNPROBED:
                plan[key] = Plan("UNPROBED", None, "", basis=fixtures.UNPROBED[key])
            else:
                raise RunnerError(f"no evidence source for {connector}.{flag}")
    return plan


def _run_pytest(node_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Run `node_ids` in a child pytest with `AIDA_ENVIRONMENT` absent; return their outcomes."""
    with tempfile.TemporaryDirectory(prefix="c14_certify_") as tmp:
        results_path = Path(tmp) / "results.json"
        args_path = Path(tmp) / "nodes.txt"
        args_path.write_text("\n".join(node_ids) + "\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "AIDA_ENVIRONMENT"}
        env["C14_RESULTS"] = str(results_path)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.support.c14_recorder",
            "-p",
            "no:cacheprovider",
            "--no-header",
            f"@{args_path}",
        ]
        completed = subprocess.run(  # noqa: S603 -- a fixed pytest command; node ids come from this repo
            command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
        )
        if not results_path.exists():
            raise RunnerError(
                "the probe run produced no results file:\n"
                + (completed.stdout[-1500:] + completed.stderr[-1500:])
            )
        loaded: dict[str, dict[str, Any]] = json.loads(results_path.read_text(encoding="utf-8"))
        return loaded


def _row(
    connector: str, flag: str, claimed: bool, plan: Plan, results: dict[str, dict[str, Any]]
) -> FlagCertification:
    if plan.kind == "NOT_APPLICABLE":
        if claimed:
            raise RunnerError(f"{connector}.{flag} is claimed but the engine has no such object")
        return FlagCertification(
            flag,
            claimed,
            STATUS_NOT_APPLICABLE,
            None,
            "engine fact: the engine has no such object",
            plan.basis,
        )
    if plan.kind == "UNPROBED":
        return FlagCertification(
            flag,
            claimed,
            STATUS_NOT_CERTIFIED,
            None,
            "not probed: nothing a test double can exercise",
            plan.basis,
            reason_code=REASON_NOT_EXERCISED,
        )

    records = [results.get(test) for test in plan.tests]
    what = next(
        (r["properties"].get("probe") for r in records if r and r["properties"].get("probe")), None
    )
    what = what or plan.what
    missing = [t for t, r in zip(plan.tests, records, strict=True) if r is None]
    if missing:
        return FlagCertification(
            flag,
            claimed,
            STATUS_NOT_CERTIFIED,
            plan.tier,
            what,
            f"the probe did not run: {missing}",
            plan.tests,
            REASON_PROBE_FAILED,
        )
    assert all(r is not None for r in records)
    failed = [
        (t, r) for t, r in zip(plan.tests, records, strict=True) if r and r["outcome"] == "failed"
    ]
    if failed:
        test, record = failed[0]
        return FlagCertification(
            flag,
            claimed,
            STATUS_NOT_CERTIFIED,
            plan.tier,
            what,
            f"{test.split('::')[-1]} failed: {record['detail']}",
            plan.tests,
            REASON_PROBE_FAILED,
        )
    skipped = [r for r in records if r and r["outcome"] == "skipped"]
    if skipped:
        if plan.kind == "LIVE":
            raise RunnerError(
                f"{connector} is probed LIVE and its probes were skipped ({skipped[0]['detail']}); "
                "start the sample containers, or certify only connectors that need no engine"
            )
        return FlagCertification(
            flag,
            claimed,
            STATUS_NOT_CERTIFIED,
            None,
            what,
            f"skipped: {skipped[0]['detail']}",
            plan.tests,
            REASON_NOT_EXERCISED,
        )

    last = records[-1]
    assert last is not None
    properties = last["properties"]
    verdict = properties.get("verdict", plan.verdict)
    evidence = properties.get("evidence") or (
        "passed: " + ", ".join(test.split("::")[-1] for test in plan.tests)
    )
    if verdict == WORKS:
        return FlagCertification(
            flag, claimed, STATUS_CERTIFIED, plan.tier, what, evidence, plan.tests
        )
    return FlagCertification(
        flag,
        claimed,
        STATUS_NOT_CERTIFIED,
        plan.tier,
        what,
        evidence,
        plan.tests,
        REASON_NOT_IMPLEMENTED,
    )


def _environment(
    connector: str, plan: dict[tuple[str, str], Plan], results: dict[str, Any]
) -> dict[str, str]:
    if connector not in LIVE_CONNECTORS:
        return {}
    found: dict[str, str] = {}
    for flag in CAPABILITY_FLAGS:
        for test in plan[(connector, flag)].tests:
            for key, value in (results.get(test) or {}).get("properties", {}).items():
                if key.startswith("environment."):
                    found[key.removeprefix("environment.")] = value
    return found


_REASON_TEXT = {
    REASON_PROBE_FAILED: "the probe ran and the connector's answer was wrong",
    REASON_NOT_IMPLEMENTED: "the probe ran and the connector does not provide this behaviour",
    REASON_NOT_EXERCISED: "no probe could exercise it here",
}


def _uncertified_claims(
    connectors: dict[str, ConnectorCertification],
) -> tuple[UncertifiedClaim, ...]:
    claims: list[UncertifiedClaim] = []
    for connector_type in sorted(connectors):
        for flag in CAPABILITY_FLAGS:
            row = connectors[connector_type].flags[flag]
            if row.claimed and row.status != STATUS_CERTIFIED:
                claims.append(
                    UncertifiedClaim(
                        connector_type=connector_type,
                        flag=flag,
                        reason_code=row.reason_code or REASON_NOT_EXERCISED,
                        what_failed=f"{row.probe} -- {_REASON_TEXT.get(row.reason_code or '', '')}",
                        evidence=row.evidence,
                    )
                )
    return tuple(claims)


def _assemble(certified: dict[str, ConnectorCertification], today: str) -> CertificationResult:
    """`certified` plus the uncertified-claim list and each connector's derived flags."""
    claims = _uncertified_claims(certified)
    interim = CertificationResult(
        CERTIFICATION_SCHEMA_VERSION, CERTIFICATION_SUITE, today, certified, claims
    )
    final: dict[str, ConnectorCertification] = {}
    for connector_type, cert in certified.items():
        derived = {
            item.flag: item.derived
            for item in derive_flags(
                connector_type,
                ConnectorCapabilities(**{f: cert.claimed[f] for f in CAPABILITY_FLAGS}),
                interim,
            )
        }
        final[connector_type] = ConnectorCertification(
            connector_type, cert.fingerprint, cert.environment, cert.claimed, cert.flags, derived
        )
    return CertificationResult(
        CERTIFICATION_SCHEMA_VERSION, CERTIFICATION_SUITE, today, final, claims
    )


def certify(targets: list[str], previous: CertificationResult | None) -> CertificationResult:
    claims = _claims()
    plan = _plan(targets)
    node_ids = sorted({test for p in plan.values() for test in p.tests})
    print(f"running {len(node_ids)} probe tests for: {', '.join(targets)}")
    results = _run_pytest(node_ids)

    certified: dict[str, ConnectorCertification] = dict(previous.connectors) if previous else {}
    for connector in targets:
        claimed = {f: bool(getattr(claims[connector], f)) for f in CAPABILITY_FLAGS}
        flags = {
            flag: _row(connector, flag, claimed[flag], plan[(connector, flag)], results)
            for flag in CAPABILITY_FLAGS
        }
        certified[connector] = ConnectorCertification(
            connector_type=connector,
            fingerprint=compute_fingerprint(connector),
            environment=_environment(connector, plan, results),
            claimed=claimed,
            flags=flags,
            derived={},
        )
    # A connector no longer registered has no business in the result.
    certified = {k: v for k, v in certified.items() if k in claims}
    return _assemble(certified, datetime.now().astimezone().date().isoformat())


# --- reporting -----------------------------------------------------------------------------------


def _report(result: CertificationResult, targets: list[str]) -> None:
    print()
    for connector in targets:
        cert = result.connectors[connector]
        rows = list(cert.flags.values())
        live = sum(1 for r in rows if r.status == STATUS_CERTIFIED and r.tier == TIER_LIVE)
        fixture = sum(1 for r in rows if r.status == STATUS_CERTIFIED and r.tier == TIER_FIXTURE)
        na = sum(1 for r in rows if r.status == STATUS_NOT_APPLICABLE)
        nc = sum(1 for r in rows if r.status == STATUS_NOT_CERTIFIED)
        env = ", ".join(f"{k} {v}" for k, v in sorted(cert.environment.items()))
        print(
            f"{connector:11s} certified LIVE {live:2d}  FIXTURE {fixture:2d}  "
            f"not certified {nc:2d}  not applicable {na}" + (f"   [{env}]" if env else "")
        )
    underclaims = [
        (c, f)
        for c, cert in sorted(result.connectors.items())
        for f, row in cert.flags.items()
        if not row.claimed and row.status == STATUS_CERTIFIED
    ]
    if underclaims:
        print("\nCERTIFIED BUT NOT CLAIMED (the probe passes; the connector declares it False):")
        for connector, flag in underclaims:
            row = result.connectors[connector].flags[flag]
            print(f"  {connector}.{flag} [{row.tier}]: {row.evidence}")
    if result.uncertified_claims:
        print("\n" + "!" * 78)
        print("UNCERTIFIED CLAIMS: claimed by the connector, NOT certified by its probe,")
        print("and still advertised (never silently lowered; lowering is your decision):")
        for claim in result.uncertified_claims:
            print(f"  {claim.connector_type}.{claim.flag}  [{claim.reason_code}]")
            print(f"      {claim.what_failed}")
            print(f"      evidence: {claim.evidence}")
        print("!" * 78)
    else:
        print("\nevery claimed flag is certified.")


def _write(result: CertificationResult) -> None:
    for path, content in (
        (RESULT_PATH, result_to_json(result)),
        (_MD_PATH, render_markdown(result)),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
        print(f"wrote {path.relative_to(REPO_ROOT)}")


def check(result: CertificationResult | None) -> list[str]:
    """Every reason the committed result cannot be trusted. Needs no database."""
    if result is None:
        return [f"{RESULT_PATH.relative_to(REPO_ROOT)} is missing or unreadable; run the script"]
    implemented = _implemented()
    problems = verify_result(result, live_probe_modules=[LIVE_MODULE], claims=_claims())
    problems += [
        f"{connector}: implemented but has no certification"
        for connector in implemented
        if connector not in result.connectors
    ]
    problems += [
        f"stale: {item.describe()}"
        for item in stale_connectors(result, connector_types=implemented)
        if item.connector_type in result.connectors
    ]
    expected = render_markdown(result)
    if not _MD_PATH.exists() or _MD_PATH.read_text(encoding="utf-8") != expected:
        problems.append(f"{_MD_PATH.relative_to(REPO_ROOT)} is out of date with the result")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify the committed result; no DB")
    parser.add_argument("--stdout", action="store_true", help="print the page; write nothing")
    parser.add_argument("--all", action="store_true", help="re-certify every connector")
    parser.add_argument(
        "--connector", action="append", default=[], help="re-certify only this connector"
    )
    args = parser.parse_args(argv)

    committed = _load_committed()
    if args.stdout:
        if committed is None:
            print("no committed certification result")
            return 1
        print(render_markdown(committed))
        return 0
    if args.check:
        problems = check(committed)
        if problems:
            print("the connector certification is not trustworthy:")
            for problem in problems:
                print(f"  - {problem}")
            print(
                "re-run scripts/certify_connector_capabilities.py (LIVE rows need the containers)."
            )
            return 1
        print(f"{RESULT_PATH.relative_to(REPO_ROOT)} and its page are current.")
        return 0

    implemented = _implemented()
    unknown = sorted(set(args.connector) - set(implemented))
    if unknown:
        print(f"unknown connector(s): {unknown}; implemented: {implemented}")
        return 2
    if args.all:
        targets = implemented
    elif args.connector:
        targets = sorted(set(args.connector))
    else:
        stale = (
            {s.connector_type for s in stale_connectors(committed, connector_types=implemented)}
            if committed
            else set(implemented)
        )
        drifted = {
            c
            for c, claim in _claims().items()
            if committed is not None
            and c in committed.connectors
            and dict(committed.connectors[c].claimed)
            != {f: bool(getattr(claim, f)) for f in CAPABILITY_FLAGS}
        }
        targets = sorted(stale | drifted)
    if not targets:
        print("every connector's code and claim match its committed certification; nothing to do.")
        print("(use --all to re-certify, or --check to verify.)")
        return 0
    try:
        result = certify(targets, committed)
    except RunnerError as exc:
        print(f"cannot certify: {exc}")
        return 2
    _write(result)
    _report(result, targets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
