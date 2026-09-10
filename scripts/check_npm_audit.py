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


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    package: str
    severity: str
    scope: str  # "dev" or "runtime"
    why: str


# --- THE BASELINE ----------------------------------------------------------
#
# Dated 2026-09-06. Measured, not copied: produced by running
# `npm audit --json --package-lock-only` in `ui-next` against the committed
# `package-lock.json` and, separately, `npm audit --json --package-lock-only
# --omit=dev` to establish which of them reach the runtime dependency graph.
#
# WHAT IT CONTAINS AND WHY, in full:
#
#   14 advisories across 3 packages -- esbuild 0.21.5, vite 5.4.11 and
#   vitest 2.1.9 -- every one of them reached only through `devDependencies`.
#   The runtime scan (`--omit=dev`, covering react 18.3.1, react-dom 18.3.1 and
#   @tanstack/react-virtual 3.13.6) returns **zero** advisories, so nothing here
#   is present in the bundle a browser loads.
#
#   Thirteen of the fourteen are the same class of defect: Vite's dev server
#   (`server.fs.deny` bypasses, dev-server request forgery, a dev-server path
#   traversal). Vite's dev server is not run in any deployed configuration of
#   this repository -- production serves the built SPA from nginx
#   (`ui-next/nginx.conf`, the image the `ui-proxy` CI job exercises) -- so their
#   exposure here is a developer's own workstation, not a deployment. The
#   fourteenth (GHSA-5xrq-8626-4rwp, critical) is the Vitest **UI** server, which
#   this repository never starts: `npm run test` is `vitest run`, one-shot and
#   headless, with no `--ui` anywhere in `package.json` or `ci.yml`.
#
#   They are baselined rather than fixed because fixing them means major version
#   bumps -- vite 5 -> 7, vitest 2 -> 3 -- and a dependency bump is a change with
#   its own build, test and review, not a side effect of adding the gate that
#   found it. That is the same reasoning, and the same shape, as the backend's
#   2026-08-31 pip-audit baseline of 16 pre-existing CVEs. Recorded as debt with
#   an owner: REVIEW.md section 7, "Frontend dependency scanning".
#
# THE POINT OF THE BASELINE: a *new* advisory, a new vulnerable package, or any
# advisory that reaches the runtime dependency graph fails this job immediately.
# The gate blocks regressions from today; it does not red-line every push over
# pre-existing, out-of-scope debt, because a gate that is red on arrival gets
# turned off and then proves nothing.
BASELINE_DATE = "2026-09-06"
BASELINE: dict[str, BaselineEntry] = {
    "GHSA-67mh-4wv8-2f99": BaselineEntry(
        "esbuild", "moderate", "dev", "dev server accepts any origin's requests"
    ),
    "GHSA-vg6x-rcgg-rjx6": BaselineEntry(
        "vite", "moderate", "dev", "dev server accepts any origin's requests"
    ),
    "GHSA-x574-m823-4x7w": BaselineEntry(
        "vite", "moderate", "dev", "server.fs.deny bypass via ?raw"
    ),
    "GHSA-356w-63v5-8wf4": BaselineEntry(
        "vite", "moderate", "dev", "server.fs.deny bypass via invalid request-target"
    ),
    "GHSA-859w-5945-r5v3": BaselineEntry(
        "vite", "moderate", "dev", "server.fs.deny bypass via /. under project root"
    ),
    "GHSA-xcj6-pq6g-qj4x": BaselineEntry(
        "vite", "moderate", "dev", "server.fs.deny bypass via .svg or relative paths"
    ),
    "GHSA-93m4-6634-74q7": BaselineEntry(
        "vite", "moderate", "dev", "server.fs.deny bypass via backslash on Windows"
    ),
    "GHSA-4r4m-qw57-chr8": BaselineEntry(
        "vite", "moderate", "dev", "server.fs.deny bypass for inline/raw with ?import"
    ),
    "GHSA-4w7w-66w2-5vf9": BaselineEntry(
        "vite", "moderate", "dev", "path traversal in optimized-deps .map handling"
    ),
    "GHSA-fx2h-pf6j-xcff": BaselineEntry(
        "vite", "high", "dev", "server.fs.deny bypass on Windows alternate paths"
    ),
    "GHSA-v6wh-96g9-6wx3": BaselineEntry(
        "vite", "moderate", "dev", "launch-editor NTLMv2 hash disclosure via UNC paths"
    ),
    "GHSA-g4jq-h2w9-997c": BaselineEntry(
        "vite", "low", "dev", "middleware may serve same-prefix files"
    ),
    "GHSA-jqfw-vq24-v9c3": BaselineEntry(
        "vite", "low", "dev", "server.fs settings not applied to HTML files"
    ),
    "GHSA-5xrq-8626-4rwp": BaselineEntry(
        "vitest",
        "critical",
        "dev",
        "Vitest UI server arbitrary file read/exec -- the UI server is never started "
        "here (`npm run test` is `vitest run`, headless, no --ui)",
    ),
}


def _run_npm_audit(omit_dev: bool) -> dict[str, Any]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise SystemExit(
            "npm was not found on PATH. This gate needs npm to read "
            f"{LOCKFILE.relative_to(REPO_ROOT)}; install Node or run with --input."
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
        command, cwd=UI_ROOT, capture_output=True, text=True, encoding="utf-8", check=False
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

    failed = False
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
