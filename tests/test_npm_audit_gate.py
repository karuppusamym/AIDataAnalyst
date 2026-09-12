"""Gate logic for the frontend dependency scan (REVIEW.md section 7).

`scripts/check_npm_audit.py` is the `ui-next` counterpart of the backend
pip-audit job. These tests exercise its comparison logic against synthetic
`npm audit --json` reports rather than against the registry: a gate whose unit
tests need the network is a gate that goes red when npmjs.com has a bad
afternoon, and a red-for-unrelated-reasons gate gets disabled.

The live scan runs in CI (`.github/workflows/ci.yml`, job `frontend-dependency-scan`).
What is pinned here is that the comparison itself bites in all three directions:
a new advisory fails, a baselined advisory that moves into the runtime dependency
graph fails, and a baseline entry that no longer describes anything fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_npm_audit  # noqa: E402
from check_npm_audit import (  # noqa: E402
    BASELINE,
    BASELINE_DATE,
    LOCKFILE,
    BaselineEntry,
    advisories,
    evaluate,
)

#: A baseline with an entry in it, for the two cases that need one to exist.
#:
#: R11-D13 emptied the real `BASELINE` on 2026-09-12 -- vitest 2 -> 5 fixed all
#: six remaining advisories outright -- and these two tests used to draw their
#: fixture from it with `next(iter(BASELINE))`, which raises `StopIteration` the
#: moment it is empty. The assertions are unchanged; only where the entry comes
#: from has moved, so that the escalation and staleness branches stay tested
#: whether or not the repository currently has anything excepted. Monkeypatching
#: the module attribute is what `evaluate()` reads, since it resolves `BASELINE`
#: from module globals at call time.
_SYNTHETIC_BASELINE: dict[str, BaselineEntry] = {
    "GHSA-1111-2222-3333": BaselineEntry(
        "vitest", "moderate", "dev", "synthetic entry, for the gate's own tests"
    ),
}


@pytest.fixture
def baseline_with_one_entry(monkeypatch: pytest.MonkeyPatch) -> dict[str, BaselineEntry]:
    monkeypatch.setattr(check_npm_audit, "BASELINE", dict(_SYNTHETIC_BASELINE))
    return dict(_SYNTHETIC_BASELINE)


def _baseline_report(baseline: dict[str, BaselineEntry]) -> dict[str, Any]:
    return _report(
        *((identifier, entry.package, entry.severity) for identifier, entry in baseline.items())
    )


def _report(*entries: tuple[str, str, str]) -> dict[str, Any]:
    """A minimal `npm audit --json` shape: package -> via -> advisory records."""
    vulnerabilities: dict[str, Any] = {}
    for identifier, package, severity in entries:
        vulnerabilities.setdefault(package, {"via": []})["via"].append(
            {
                "url": f"https://github.com/advisories/{identifier}",
                "name": package,
                "severity": severity,
                "title": f"synthetic advisory {identifier}",
            }
        )
    return {"vulnerabilities": vulnerabilities}


def _current_baseline_report() -> dict[str, Any]:
    return _report(
        *((identifier, entry.package, entry.severity) for identifier, entry in BASELINE.items())
    )


def test_the_lockfile_this_gate_scans_actually_exists() -> None:
    """The gate scans a committed lockfile and deliberately will not generate
    one; if `ui-next/package-lock.json` ever disappears, that is a finding, not a
    reason to synthesize a dependency set nobody reviewed.
    """
    assert LOCKFILE.is_file(), (
        f"{LOCKFILE} is missing. `npm ci` and this gate both require a committed "
        "lockfile; do not let CI generate one."
    )
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    assert lock.get("lockfileVersion", 0) >= 2, "unexpectedly old npm lockfile format"


def test_advisory_ids_are_read_out_of_the_nested_via_records() -> None:
    """npm nests real advisories inside `via`, alongside plain-string
    cross-references to other packages in the same report. Only the dicts are
    advisories; treating the strings as ids would invent findings.
    """
    report = _report(("GHSA-aaaa-bbbb-cccc", "vite", "high"))
    report["vulnerabilities"]["vite"]["via"].append("esbuild")
    found = advisories(report)
    assert set(found) == {"GHSA-aaaa-bbbb-cccc"}
    assert found["GHSA-aaaa-bbbb-cccc"][0] == "vite"


def test_todays_findings_are_fully_baselined() -> None:
    """The baseline was measured, not guessed: replaying exactly what it claims
    is present must produce no unbaselined finding and no stale entry.
    """
    report = _current_baseline_report()
    unbaselined, escalated, stale, _ = evaluate(report, {"vulnerabilities": {}})
    assert not unbaselined and not escalated and not stale
    assert BASELINE_DATE, "the baseline must carry the date it was measured"
    assert all(entry.why.strip() for entry in BASELINE.values()), (
        "every baseline entry must say why it is baselined, not just that it is"
    )


def test_with_no_baseline_any_advisory_at_all_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state the repository is actually in since R11-D13 (2026-09-12).

    `BASELINE` is empty, so `ui-next` is held to the same zero-exception rule
    `e2e` always was. The test above is satisfied vacuously by an empty
    baseline -- replaying nothing finds nothing -- so this is the one that says
    what empty *means*: a single advisory, of any severity, in either scope,
    with nothing excepted, fails the job. Without this, emptying the baseline
    would have removed the only coverage of its own effect.
    """
    monkeypatch.setattr(check_npm_audit, "BASELINE", {})
    unbaselined, _, stale, _ = evaluate(
        _report(("GHSA-0000-0000-0001", "vite", "low")), {"vulnerabilities": {}}
    )
    assert len(unbaselined) == 1
    assert "GHSA-0000-0000-0001" in unbaselined[0]
    assert not stale, "an empty baseline cannot have a stale entry"


def test_a_new_advisory_fails_the_gate() -> None:
    report = _current_baseline_report()
    report["vulnerabilities"]["react"] = {
        "via": [
            {
                "url": "https://github.com/advisories/GHSA-9999-9999-9999",
                "name": "react",
                "severity": "critical",
                "title": "synthetic new advisory",
            }
        ]
    }
    unbaselined, _, _, _ = evaluate(report, {"vulnerabilities": {}})
    assert len(unbaselined) == 1
    assert "GHSA-9999-9999-9999" in unbaselined[0]


def test_a_new_advisory_in_a_runtime_dependency_is_labelled_as_such() -> None:
    """A dev-toolchain advisory and a browser-shipped one are not the same
    finding, and the failure message has to say which it is.
    """
    entry = ("GHSA-9999-9999-9999", "react", "critical")
    unbaselined, _, _, _ = evaluate(_report(entry), _report(entry))
    assert unbaselined and "RUNTIME DEPENDENCY" in unbaselined[0]


def test_a_baselined_dev_advisory_moving_into_the_runtime_graph_fails(
    baseline_with_one_entry: dict[str, BaselineEntry],
) -> None:
    """The escalation case. Baselining `vitest` as dev-only is defensible; the
    same advisory reaching a package the browser loads is not, and it must not
    stay silently covered by its old baseline entry.
    """
    baseline = baseline_with_one_entry
    identifier = next(iter(baseline))
    report = _baseline_report(baseline)
    runtime = _report((identifier, baseline[identifier].package, baseline[identifier].severity))
    _, escalated, _, _ = evaluate(report, runtime)
    assert len(escalated) == 1
    assert identifier in escalated[0]


def test_a_baseline_entry_that_no_longer_applies_fails(
    baseline_with_one_entry: dict[str, BaselineEntry],
) -> None:
    """Same contract as the reachability allow-list: an entry nobody has to
    maintain becomes a list nobody reads. Removing the line is the whole fix.

    This is the branch that actually emptied the baseline: after the vitest
    upgrade all six entries stopped being reported, the gate failed naming each
    one, and deleting them was the fix.
    """
    baseline = baseline_with_one_entry
    report = _baseline_report(baseline)
    dropped = next(iter(baseline))
    package = baseline[dropped].package
    report["vulnerabilities"][package]["via"] = [
        via
        for via in report["vulnerabilities"][package]["via"]
        if dropped not in via.get("url", "")
    ]
    _, _, stale, _ = evaluate(report, {"vulnerabilities": {}})
    assert len(stale) == 1
    assert dropped in stale[0]


def test_baseline_scopes_are_one_of_two_known_values() -> None:
    assert {entry.scope for entry in BASELINE.values()} <= {"dev", "runtime"}
