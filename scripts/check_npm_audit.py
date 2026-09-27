#!/usr/bin/env python3
"""Frontend dependency-vulnerability gate -- the `ui-next` counterpart of the
backend `pip-audit` job.

`Docs/review-2026-09-05/REVIEW.md` section 7 and the AU-13 tracker row gave the
backend a scan: `pip-audit` against the exact `uv.lock`-locked, non-dev
dependency set, failing on any *unbaselined* known vulnerability, with a small,
dated, explicitly-named baseline for what was already there. `ui-next` had
nothing at all -- 231 resolved packages, a committed `package-lock.json`, and no
gate of any kind. This is that missing scan, built to the same design.

Two deliberate differences from the backend gate, both stated rather than
assumed:

**It scans the whole lockfile, dev dependencies included.** The backend scan is
restricted to the non-dev set because that is what the runtime image installs.
The frontend's dev set is not analogous: `vite`, `esbuild` and `vitest` are what
*produce* the artifact the browser executes, so a compromised build tool is a
supply-chain path into production even though it ships in nothing. The gate
therefore reports both scopes and gates on both -- and the runtime (`--omit=dev`)
scope is derived from a second `npm audit` run, not guessed, so an advisory that
moves from dev-only into the runtime graph is caught by
`scope escalation` below rather than sitting quietly inside its baseline entry.

**The baseline is checked for staleness.** `pip-audit --ignore-vuln` silently
accepts an entry for a vulnerability that no longer exists; this one fails, the
same way `tests/test_reachability_gate.py`'s allow-list does, because a baseline
nobody has to maintain becomes a list of things nobody looks at. Removing a line
is the whole fix, and the failure message names the line.

Usage
-----
    python scripts/check_npm_audit.py               # report only
    python scripts/check_npm_audit.py --check       # fail CI on a regression
    python scripts/check_npm_audit.py --report out.json   # save the raw report
    python scripts/check_npm_audit.py --input a.json --input-omit-dev b.json

Standard library only. It shells out to `npm audit --json --package-lock-only`,
which resolves straight from `ui-next/package-lock.json` and needs no
`npm ci` -- so this job stays fast and cannot be blocked by an install problem
it has nothing to do with.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = REPO_ROOT / "ui-next"
LOCKFILE = UI_ROOT / "package-lock.json"

#: R11-B11's browser-journey suite committed a second lockfile, and this gate
#: scanned only `ui-next` -- so a dependency set that installs and runs in CI was
#: covered by nothing. It is held to a **stricter** rule than `ui-next`: no
#: baseline at all. `ui-next` has one because it inherited advisories predating
#: the gate; `e2e` starts clean, and the moment to refuse a first exception is
#: before there is one.
E2E_ROOT = REPO_ROOT / "e2e"
E2E_LOCKFILE = E2E_ROOT / "package-lock.json"


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    package: str
    severity: str
    scope: str  # "dev" or "runtime"
    why: str


# --- THE BASELINE ----------------------------------------------------------
#
# **It is empty, and that is the finished state, not an unfilled stub.**
#
# The baseline existed because this gate was added to a dependency set that
# already had advisories in it: 14 across esbuild 0.21.5, vite 5.4.11 and
# vitest 2.1.9 on 2026-09-06, all dev-scope, which R11-D13 cut to 6 on
# 2026-09-11 by pinning vite to 5.4.21. The 2026-09-06 note reasoned that the
# rest needed "vite 5 -> 7, vitest 2 -> 3", and that a major bump is a change
# with its own build, test and review rather than a side effect of adding the
# gate that found it. That was right, and this is that change: vitest 2.1.9 ->
# 5.0.0, which forces vite 5.4.21 -> 7.3.6 (vitest 5 peers on vite >= 6.4) and
# @vitejs/plugin-react 4.3.4 -> 5.2.0. All six remaining advisories are fixed
# by it -- not suppressed, *fixed*, by versions that no longer contain the
# defect -- and both scans now report zero:
#
#   esbuild  GHSA-67mh-4wv8-2f99   dev server accepts any origin  (0.21.5 -> 0.28.2)
#   vite     GHSA-4w7w-66w2-5vf9   optimized-deps .map traversal  (5.4.21 -> 7.3.6)
#   vite     GHSA-fx2h-pf6j-xcff   server.fs.deny bypass, Windows       "
#   vite     GHSA-v6wh-96g9-6wx3   launch-editor NTLMv2 via UNC         "
#   vitest   GHSA-82fw-gwwq-j7x9   @vitest/mocker redirect traversal (2.1.9 -> 5.0.0)
#   vitest   GHSA-5xrq-8626-4rwp   Vitest UI arbitrary file read/exec      "
#
# Measured, not copied: `npm audit --json --package-lock-only` in `ui-next`
# against the committed `package-lock.json`, and separately with `--omit=dev`,
# both returning 0 of 228 resolved dependencies. The 795-test suite passes
# unchanged across all three majors.
#
# THE POINT OF THE BASELINE, unchanged: a *new* advisory, a new vulnerable
# package, or any advisory reaching the runtime dependency graph fails this job
# immediately. What has changed is that there is now nothing to except, so
# `ui-next` is held to the same rule `e2e` above already was. Adding an entry
# here is a deliberate act that needs the package, severity, scope and a
# reason -- and `evaluate()` will fail the job the moment an entry stops being
# reported, so an exception cannot outlive the advisory it was written for.
BASELINE_DATE = "2026-09-12"
BASELINE: dict[str, BaselineEntry] = {}


def _run_npm_audit(omit_dev: bool, root: Path = UI_ROOT) -> dict[str, Any]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise SystemExit(
            "npm was not found on PATH. This gate needs npm to read "
            f"{(root / 'package-lock.json').relative_to(REPO_ROOT)}; "
            "install Node or run with --input."
        )
    command = [npm, "audit", "--json", "--package-lock-only"]
    if omit_dev:
        command.append("--omit=dev")
    # `npm audit` exits non-zero *because* it found something, so the return
    # code carries no signal here; the report body is the signal. A genuinely
    # broken invocation shows up as unparsable output, handled below.
    # S603: the argument vector is built entirely from constants in this file
    # plus `shutil.which("npm")`; no caller input reaches it, and `shell=False`
    # (the default) means nothing here is interpreted by a shell.
    completed = subprocess.run(  # noqa: S603
        command, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
    )
    try:
        return json.loads(completed.stdout)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"`{' '.join(command[1:])}` did not produce JSON (exit {completed.returncode}).\n"
            f"stdout: {completed.stdout[:2000]}\nstderr: {completed.stderr[:2000]}"
        ) from exc


def advisories(report: dict[str, Any]) -> dict[str, tuple[str, str, str]]:
    """GHSA id -> (package, severity, title), from an `npm audit --json` report.

    npm nests the real advisory records inside each vulnerable package's `via`
    list; entries there that are plain strings are cross-references to another
    package in the same report, not advisories, so only the dicts are read.
    """
    found: dict[str, tuple[str, str, str]] = {}
    for record in report.get("vulnerabilities", {}).values():
        for via in record.get("via", []):
            if not isinstance(via, dict):
                continue
            url = via.get("url", "")
            identifier = url.rsplit("/", 1)[-1] if url else ""
            if not identifier:
                continue
            found[identifier] = (
                via.get("name", "unknown"),
                via.get("severity", "unknown"),
                via.get("title", ""),
            )
    return found


def evaluate(
    full: dict[str, Any], runtime_only: dict[str, Any]
) -> tuple[list[str], list[str], list[str], dict[str, tuple[str, str, str]]]:
    """Returns (unbaselined, scope escalations, stale baseline entries, all found)."""
    found = advisories(full)
    runtime = set(advisories(runtime_only))

    unbaselined = [
        f"{identifier}  {found[identifier][0]}  {found[identifier][1]}"
        f"{'  [RUNTIME DEPENDENCY]' if identifier in runtime else '  [dev only]'}"
        f"  {found[identifier][2][:90]}"
        for identifier in sorted(found)
        if identifier not in BASELINE
    ]
    escalated = [
        f"{identifier}  {BASELINE[identifier].package}: baselined as "
        f"{BASELINE[identifier].scope}-only, but it now reaches the runtime "
        "dependency graph"
        for identifier in sorted(runtime & set(BASELINE))
        if BASELINE[identifier].scope == "dev"
    ]
    stale = [
        f"{identifier}  ({BASELINE[identifier].package}) is no longer reported -- "
        "delete this entry from BASELINE in scripts/check_npm_audit.py"
        for identifier in sorted(BASELINE)
        if identifier not in found
    ]
    return unbaselined, escalated, stale, found


def _summary(report: dict[str, Any], label: str) -> str:
    metadata = report.get("metadata", {})
    counts = metadata.get("vulnerabilities", {})
    deps = metadata.get("dependencies", {})
    return (
        f"{label}: {counts.get('total', 0)} vulnerable packages "
        f"(critical {counts.get('critical', 0)}, high {counts.get('high', 0)}, "
        f"moderate {counts.get('moderate', 0)}, low {counts.get('low', 0)}) "
        f"across {deps.get('total', 'unknown')} resolved dependencies"
    )


def _check_e2e_lockfile(*, offline: bool) -> bool:
    """Audit the browser-journey lockfile, with no baseline. True when it fails.

    Skipped under `--input`, which exists so this gate can run from a saved
    report with no network: auditing a second project would need a second
    report and the flag takes one. Saying it was skipped beats reporting a pass
    for a scan that did not happen.
    """
    if not E2E_LOCKFILE.is_file():
        return False
    relative = E2E_LOCKFILE.relative_to(REPO_ROOT)
    if offline:
        print(f"\nskipped {relative}: --input supplies one report only")
        return False
    report = _run_npm_audit(omit_dev=False, root=E2E_ROOT)
    found = advisories(report)
    print("\n" + _summary(report, "e2e, full lockfile (dev included)"))
    if not found:
        return False
    for identifier in sorted(found):
        package, severity, title = found[identifier]
        print(f"  {identifier}  {package}  {severity}  [NOT BASELINED]  {title[:80]}")
    print(
        f"\nERROR: advisories in {relative}. This project has no baseline by design -- it "
        "started clean, so the fix is to upgrade the package rather than to open an "
        "exception list here.",
        file=sys.stderr,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero on an unbaselined finding."
    )
    parser.add_argument("--report", type=Path, help="Write the full npm audit JSON here.")
    parser.add_argument("--input", type=Path, help="Read the full report from a file, not npm.")
    parser.add_argument(
        "--input-omit-dev", type=Path, help="Read the runtime-only report from a file."
    )
    args = parser.parse_args()

    if not LOCKFILE.is_file():
        print(
            f"ERROR: {LOCKFILE} does not exist. This gate scans a committed lockfile and "
            "will not generate one: a lockfile produced by CI pins whatever the registry "
            "served that minute, which is not the dependency set anybody reviewed.",
            file=sys.stderr,
        )
        return 1

    if args.input:
        full = json.loads(args.input.read_text(encoding="utf-8"))
        runtime_only = (
            json.loads(args.input_omit_dev.read_text(encoding="utf-8"))
            if args.input_omit_dev
            else {"vulnerabilities": {}}
        )
    else:
        full = _run_npm_audit(omit_dev=False)
        runtime_only = _run_npm_audit(omit_dev=True)

    if args.report:
        args.report.write_text(json.dumps(full, indent=2), encoding="utf-8")

    unbaselined, escalated, stale, found = evaluate(full, runtime_only)

    print(_summary(full, "ui-next, full lockfile (dev included)"))
    print(_summary(runtime_only, "ui-next, runtime dependencies only"))
    print(f"distinct advisories: {len(found)}; baselined {BASELINE_DATE}: {len(BASELINE)}")
    for identifier in sorted(found):
        package, severity, title = found[identifier]
        state = "baselined" if identifier in BASELINE else "NOT BASELINED"
        print(f"  {identifier}  {package}  {severity}  [{state}]  {title[:80]}")

    failed = _check_e2e_lockfile(offline=bool(args.input))

    if unbaselined:
        print(
            "\nERROR: unbaselined advisories in ui-next/package-lock.json:\n"
            + "\n".join(f"  - {line}" for line in unbaselined)
            + "\n\nFix it by upgrading the package. Only if the fix is genuinely out of "
            "scope, add the advisory id to BASELINE in scripts/check_npm_audit.py with "
            "the package, severity, scope and the reason -- the way the existing entries "
            "are written. An advisory reaching a RUNTIME dependency ships to browsers and "
            "should not be baselined at all.",
            file=sys.stderr,
        )
        failed = True
    if escalated:
        print(
            "\nERROR: a baselined dev-only advisory now reaches the runtime dependency "
            "graph:\n" + "\n".join(f"  - {line}" for line in escalated),
            file=sys.stderr,
        )
        failed = True
    if stale:
        print(
            "\nERROR: stale BASELINE entries:\n" + "\n".join(f"  - {line}" for line in stale),
            file=sys.stderr,
        )
        failed = True

    if not args.check:
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
