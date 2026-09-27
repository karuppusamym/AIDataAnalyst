"""review F03: the parity gate reports deployment drift, and reports it by name.

`scripts/check_deployment_parity.py` is the only check in this repository that
compares a *deployment* to the source. Its whole value is that it says which
path, which revision, which setting differs -- a gate that reported "405 vs
404" would have left the operator to find the missing route by hand, which is
how F03 stayed unnoticed while every source-side gate was green.

So the comparators are tested against fixtures, with no deployment involved:
the four shapes that mattered on 2026-09-16 (one migration behind, a baseline
path the deployment does not serve, two baseline schemas it does not serve, and
an image whose `Settings` predates `footprint_metrics_interval_seconds`), plus
the two failure modes a gate like this is most likely to get wrong -- calling
an unreachable deployment "parity", and failing a build merely because there
was nothing to reach.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_deployment_parity import (  # noqa: E402
    DEFAULT_BASELINE,
    DRIFT,
    MATCH,
    MAX_NAMED_FILES,
    SOURCE_DIGEST_SIGNAL,
    UNKNOWN,
    Finding,
    Report,
    compare_migrations,
    compare_openapi,
    compare_readiness,
    compare_settings,
    compare_source_identity,
    describe_build_commit,
    main,
    name_differing_files,
    read_deployed_settings,
    resolve_database_url,
    source_heads_and_unapplied,
    source_setting_names,
)

from aida.source_identity import manifest_digest, source_manifest  # noqa: E402

# The live/baseline shapes measured on 2026-09-16, reduced to what the
# comparators read. A minimal fixture is deliberate: a comparator that needs a
# 2.7MB document to say anything is a comparator nobody can reason about.
_BASELINE_SPEC: dict[str, Any] = {
    "info": {"version": "2.0.0"},
    "paths": {
        "/v1/organizations": {"get": {}},
        "/v1/datasources/{datasource_id}/footprint-gaps/{kind}": {"get": {}},
    },
    "components": {
        "schemas": {
            "OrganizationRead": {},
            "FootprintGapDetailRead": {},
            "FootprintGapObjectRead": {},
        }
    },
}
_LIVE_SPEC: dict[str, Any] = {
    "info": {"version": "2.0.0"},
    "paths": {"/v1/organizations": {"get": {}}},
    "components": {"schemas": {"OrganizationRead": {}}},
}

_READY_PAYLOAD: dict[str, Any] = {
    "status": "UP",
    "version": "2.0.0",
    "required": {"postgresql": "UP"},
    "controls": {"workspace_authorization": "OBSERVING"},
    "signals": {
        "postgresql.duration_ms": "6.0",
        "delivery_backlog.detail": "failed=0;queued=9;queued_notification=9;worker=disabled",
        "delivery_backlog.duration_ms": "6.0",
        "outbox_backlog.detail": "pending=0",
        "workspace_authorization.declared": "OBSERVING",
        "workspace_authorization.unresolved_scope": "PROCEEDS_UNDECIDED",
    },
}


def _by_name(findings: list[Finding], fragment: str) -> Finding:
    matches = [f for f in findings if fragment in f.name]
    assert len(matches) == 1, f"expected one finding matching {fragment!r}, got {len(matches)}"
    return matches[0]


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


def test_a_database_one_migration_behind_names_the_unapplied_revision() -> None:
    """The 2026-09-16 shape. "Behind" is not actionable; a revision id is."""
    finding = compare_migrations(
        frozenset({"b7e2d9c4f158"}),
        frozenset({"f4a8c1d7e236"}),
        ("f4a8c1d7e236",),
    )
    assert finding.outcome == DRIFT
    assert "b7e2d9c4f158" in finding.detail and "f4a8c1d7e236" in finding.detail
    assert finding.lines == ("unapplied 1/1: f4a8c1d7e236",)


def test_a_database_at_the_source_head_matches() -> None:
    finding = compare_migrations(
        frozenset({"f4a8c1d7e236"}), frozenset({"f4a8c1d7e236"}), ()
    )
    assert finding.outcome == MATCH


def test_a_revision_this_tree_does_not_contain_is_not_reported_as_behind() -> None:
    """A deployment built from a different history is diverged, not lagging.

    Saying "unapplied: none" for this case would read as parity, and telling an
    operator to run `alembic upgrade heads` against it would be wrong advice.
    """
    finding = compare_migrations(frozenset({"deadbeef1234"}), frozenset({"f4a8c1d7e236"}), ())
    assert finding.outcome == DRIFT
    assert any("different history" in line for line in finding.lines)


def test_an_unread_alembic_version_is_unknown_not_a_pass() -> None:
    finding = compare_migrations(None, frozenset({"f4a8c1d7e236"}), (), unreachable="refused")
    assert finding.outcome == UNKNOWN
    assert "refused" in finding.detail


def test_an_empty_alembic_version_table_is_drift() -> None:
    finding = compare_migrations(frozenset(), frozenset({"f4a8c1d7e236"}), ())
    assert finding.outcome == DRIFT
    assert "no migration has ever run" in finding.detail


def _script_directory() -> Any:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def test_the_source_heads_come_from_this_repositorys_migration_tree() -> None:
    """Read from `migrations/`, the directory `alembic upgrade heads` walks.

    Deliberately *not* asserting one head. That invariant is the CI
    `migrations` job's, and duplicating it here would turn this file red
    whenever a peer session has an in-flight revision -- which says nothing
    about whether the comparator works. What matters here is that the heads are
    real revisions in this tree, so a revision this gate names is a file a
    reviewer can open.
    """
    heads, unapplied = source_heads_and_unapplied(frozenset())
    assert heads, "no Alembic head found under migrations/"
    script = _script_directory()
    for head in heads:
        assert script.get_revision(head).revision == head
    assert unapplied == ()  # nothing to say about a database that was never read


def test_unapplied_revisions_are_listed_in_apply_order() -> None:
    """Alembic walks newest-first; an operator applies oldest-first.

    Asserted as the ordering property rather than against a fixed list, so it
    holds however many heads the tree happens to carry: no revision may be
    listed before the parent it depends on.
    """
    script = _script_directory()
    head = sorted(script.get_heads())[0]
    parent = script.get_revision(head).down_revision
    assert isinstance(parent, str), "the chosen head should have one parent revision"

    _, unapplied = source_heads_and_unapplied(frozenset({parent}))
    assert head in unapplied
    position = {revision: index for index, revision in enumerate(unapplied)}
    for revision in unapplied:
        down = script.get_revision(revision).down_revision
        if isinstance(down, str) and down in position:
            assert position[down] < position[revision], (
                f"{revision} is listed before its parent {down}; alembic walks "
                "newest-first and an operator applies oldest-first"
            )


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #


def test_a_path_the_baseline_promises_and_the_deployment_lacks_is_named() -> None:
    findings = compare_openapi(_LIVE_SPEC, _BASELINE_SPEC)
    paths = _by_name(findings, "OpenAPI paths")
    assert paths.outcome == DRIFT
    assert paths.detail == "live=1 baseline=2"
    assert paths.lines == (
        "in the baseline, missing from the deployment (path): "
        "/v1/datasources/{datasource_id}/footprint-gaps/{kind}",
    )


def test_schemas_missing_from_the_deployment_are_named_not_counted() -> None:
    schemas = _by_name(compare_openapi(_LIVE_SPEC, _BASELINE_SPEC), "OpenAPI schemas")
    assert schemas.outcome == DRIFT
    assert [line.rsplit(": ", 1)[1] for line in schemas.lines] == [
        "FootprintGapDetailRead",
        "FootprintGapObjectRead",
    ]


def test_a_surface_the_deployment_serves_and_the_baseline_lacks_is_also_drift() -> None:
    """Symmetric, not one-directional: an image *newer* than the baseline is
    drift too, and the operator needs to know which direction it is."""
    findings = compare_openapi(_BASELINE_SPEC, _LIVE_SPEC)
    paths = _by_name(findings, "OpenAPI paths")
    assert paths.outcome == DRIFT
    assert all("served by the deployment" in line for line in paths.lines)


def test_an_equal_version_with_an_unequal_surface_is_called_out() -> None:
    version = _by_name(compare_openapi(_LIVE_SPEC, _BASELINE_SPEC), "API version")
    assert version.outcome == MATCH  # the versions really are equal
    paths = _by_name(compare_openapi(_LIVE_SPEC, _BASELINE_SPEC), "OpenAPI paths")
    assert paths.outcome == DRIFT  # which is the point: equal version, unequal surface


def test_a_version_skew_is_drift() -> None:
    live = dict(_LIVE_SPEC) | {"info": {"version": "2.1.0"}}
    version = _by_name(compare_openapi(live, _BASELINE_SPEC), "API version")
    assert version.outcome == DRIFT
    assert "2.1.0" in version.detail and "2.0.0" in version.detail


def test_the_committed_baseline_does_not_drift_against_itself() -> None:
    """The comparator run against the real 2.7MB baseline, both sides.

    A false positive here would make the gate noise, and noise is what gets a
    gate switched off. This is the one test that uses the real document.
    """
    baseline = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))
    assert all(f.outcome == MATCH for f in compare_openapi(baseline, baseline))


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


def test_readiness_reports_controls_and_the_backlog_signals() -> None:
    findings = compare_readiness(_READY_PAYLOAD)
    assert _by_name(findings, "required dependency").outcome == MATCH
    assert _by_name(findings, "control posture").outcome == MATCH
    signals = _by_name(findings, "backlog and authorization signals")
    assert signals.outcome == MATCH
    # The durations are noise; the backlog and the posture are the decision inputs.
    assert not any("duration_ms" in line for line in signals.lines)
    assert any("worker=disabled" in line for line in signals.lines)
    assert any("PROCEEDS_UNDECIDED" in line for line in signals.lines)


def test_a_required_dependency_that_is_down_is_drift() -> None:
    payload = dict(_READY_PAYLOAD) | {"required": {"postgresql": "DOWN"}}
    assert _by_name(compare_readiness(payload), "required dependency").outcome == DRIFT


def test_readiness_with_no_controls_is_drift_not_a_pass() -> None:
    """F11: a 200 that reports dependencies but not controls is how a
    deployment satisfies every automated check while enforcing nothing."""
    payload = {k: v for k, v in _READY_PAYLOAD.items() if k != "controls"}
    assert _by_name(compare_readiness(payload), "control posture").outcome == DRIFT


# --------------------------------------------------------------------------- #
# Settings: is the image as new as the code?
# --------------------------------------------------------------------------- #


def test_an_image_missing_a_setting_the_source_declares_is_drift() -> None:
    """The check that surfaced F03's sharpest edge.

    The deployed image answered 200 to everything while its `Settings` had no
    `footprint_metrics_interval_seconds` -- so `run_footprint_metrics_pass` was
    not in the running scheduler at any configured value.
    """
    source = frozenset({"footprint_metrics_interval_seconds", "delivery_worker_enabled"})
    finding = compare_settings(source, frozenset({"delivery_worker_enabled"}))
    assert finding.outcome == DRIFT
    assert any("footprint_metrics_interval_seconds" in line for line in finding.lines)
    assert any("predates the code" in line for line in finding.lines)


def test_a_setting_the_image_has_and_the_source_dropped_is_also_reported() -> None:
    finding = compare_settings(frozenset({"a"}), frozenset({"a", "dq_itsm_webhook_enabled"}))
    assert finding.outcome == DRIFT
    assert any("no longer in the source" in line for line in finding.lines)


def test_an_image_that_declares_the_same_settings_matches() -> None:
    names = frozenset({"a", "b"})
    assert compare_settings(names, names).outcome == MATCH


def test_settings_that_could_not_be_read_are_unknown_not_a_pass() -> None:
    finding = compare_settings(frozenset({"a"}), None, unreachable="docker unavailable")
    assert finding.outcome == UNKNOWN
    assert "docker unavailable" in finding.detail


def test_the_source_setting_names_come_from_the_settings_class() -> None:
    """Shares the configuration inventory's parser, so the two cannot disagree
    about what "the source declares" means."""
    names = source_setting_names()
    assert len(names) >= 200, f"only {len(names)} settings parsed; the parser has stopped working"
    assert {
        "footprint_metrics_interval_seconds",
        "change_signal_processing_interval_minutes",
        "freshness_evaluation_interval_minutes",
        "context_rebuild_interval_minutes",
        "delivery_worker_enabled",
        "workspace_authorization_posture",
    } <= names


def test_a_captured_settings_list_replaces_docker_exec(tmp_path: Path) -> None:
    """The Kubernetes door: `kubectl exec > file`, then `--settings-json`."""
    captured = tmp_path / "settings.json"
    captured.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    names, why = read_deployed_settings(container="unused", settings_json=captured)
    assert names == frozenset({"a", "b"}) and why == ""


def test_an_unreadable_settings_capture_is_unknown(tmp_path: Path) -> None:
    broken = tmp_path / "settings.json"
    broken.write_text('{"not": "a list"}', encoding="utf-8")
    names, why = read_deployed_settings(container="unused", settings_json=broken)
    assert names is None and "not a JSON list" in why


# --------------------------------------------------------------------------- #
# Exit codes: the CI contract
# --------------------------------------------------------------------------- #


def _stub_report(*findings: Finding) -> Report:
    report = Report(base_url="http://localhost:8000")
    report.findings.extend(findings)
    return report


def test_drift_fails_the_build_in_both_modes(monkeypatch: Any) -> None:
    drift = _stub_report(Finding("x", DRIFT, "d"))
    monkeypatch.setattr("check_deployment_parity.run", lambda _args: drift)
    assert main([]) == 1
    assert main(["--check"]) == 1


def test_an_unreachable_deployment_does_not_fail_a_ci_build(monkeypatch: Any) -> None:
    """Requirement 4's hard edge: "cannot tell" is exit 2 for an operator and
    exit 0 under `--check`, because a CI job with no deployment to reach must
    not fail the build for that reason -- and must not claim parity either."""
    unknown = _stub_report(Finding("x", UNKNOWN, "nothing answered"))
    monkeypatch.setattr("check_deployment_parity.run", lambda _args: unknown)
    assert main([]) == 2
    assert main(["--check"]) == 0


def test_parity_is_exit_zero(monkeypatch: Any) -> None:
    ok = _stub_report(Finding("x", MATCH, "same"))
    monkeypatch.setattr("check_deployment_parity.run", lambda _args: ok)
    assert main([]) == 0
    assert main(["--check"]) == 0


def test_drift_wins_over_unknown(monkeypatch: Any) -> None:
    """One unmeasurable comparison must not hide a measured difference."""
    mixed = _stub_report(Finding("a", UNKNOWN, ""), Finding("b", DRIFT, ""))
    monkeypatch.setattr("check_deployment_parity.run", lambda _args: mixed)
    assert main(["--check"]) == 1


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def test_a_sync_postgres_url_is_rewritten_to_the_async_driver(monkeypatch: Any) -> None:
    """The same database; only the driver name differs. `psql`-shaped URLs are
    what an operator has to hand, so accepting one is not a convenience."""
    monkeypatch.setenv("AIDA_DATABASE_URL", "postgresql://aida:x@db.internal:5432/aida")
    assert resolve_database_url("") == "postgresql+asyncpg://aida:x@db.internal:5432/aida"


def test_an_explicit_database_url_wins_over_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("AIDA_DATABASE_URL", "postgresql://from-env/aida")
    resolved = resolve_database_url("postgresql+asyncpg://explicit/aida")
    assert resolved == "postgresql+asyncpg://explicit/aida"


# --------------------------------------------------------------------------- #
# R11-D17: code identity -- the comparison the first four could not make
# --------------------------------------------------------------------------- #

_LOCAL_DIGEST = "a" * 64


def test_an_equal_source_digest_is_a_match() -> None:
    finding = compare_source_identity(
        {SOURCE_DIGEST_SIGNAL: _LOCAL_DIGEST}, _LOCAL_DIGEST, commit_line="built from HEAD"
    )
    assert finding.outcome == MATCH
    assert finding.lines == ("built from HEAD",)


def test_a_different_source_digest_is_drift_and_names_the_files() -> None:
    finding = compare_source_identity(
        {SOURCE_DIGEST_SIGNAL: "b" * 64},
        _LOCAL_DIGEST,
        commit_line="built from b76842d, 2 commit(s) behind",
        differing_files=("differs between the image and this tree: src/aida/sql_redaction.py",),
    )
    assert finding.outcome == DRIFT
    assert finding.lines == (
        "built from b76842d, 2 commit(s) behind",
        "differs between the image and this tree: src/aida/sql_redaction.py",
    )


def test_drift_without_exec_access_says_how_to_get_the_file_list() -> None:
    finding = compare_source_identity({SOURCE_DIGEST_SIGNAL: "b" * 64}, _LOCAL_DIGEST)
    assert finding.outcome == DRIFT
    assert "docker exec" in finding.lines[-1]


def test_an_image_that_does_not_publish_its_digest_is_not_parity() -> None:
    """The 2026-09-19 image: schema, routes and settings all matched, and it was
    40 minutes stale. Without the signal the answer is "cannot tell"."""
    finding = compare_source_identity({"delivery_backlog.detail": "pending=0"}, _LOCAL_DIGEST)
    assert finding.outcome == UNKNOWN
    assert SOURCE_DIGEST_SIGNAL in finding.detail


def test_a_deployment_that_cannot_digest_itself_is_unknown() -> None:
    finding = compare_source_identity({SOURCE_DIGEST_SIGNAL: "unknown"}, _LOCAL_DIGEST)
    assert finding.outcome == UNKNOWN


def test_no_readiness_answer_is_unknown() -> None:
    finding = compare_source_identity(None, _LOCAL_DIGEST, unreachable="HTTP 0")
    assert finding.outcome == UNKNOWN and finding.detail == "HTTP 0"


def test_differing_files_are_named_by_path_and_capped() -> None:
    local = {"src/a.py": "1", "src/b.py": "2", "src/new.py": "3"}
    deployed = {"src/a.py": "1", "src/b.py": "9", "src/gone.py": "4"}
    assert name_differing_files(local, deployed) == (
        "differs between the image and this tree: src/b.py",
        "in this tree, not in the image: src/new.py",
        "in the image, not in this tree: src/gone.py",
    )
    many = {f"src/m{index:03}.py": "x" for index in range(MAX_NAMED_FILES + 5)}
    capped = name_differing_files(many, {})
    assert len(capped) == MAX_NAMED_FILES + 1
    assert capped[-1] == "... and 5 more"


def test_the_commit_label_places_the_image_in_this_history() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert "is this checkout's HEAD" in describe_build_commit(head)
    assert "ATLAS_BUILD_COMMIT" in describe_build_commit("unknown")
    assert "does not contain" in describe_build_commit("0" * 40)
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD~1"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if parent:  # a shallow CI checkout has no parent to place
        assert "1 commit(s) behind" in describe_build_commit(parent)


def _aligned_deployment(monkeypatch: Any, signals: dict[str, str]) -> None:
    """A deployment matching this tree on everything the first four comparisons read."""
    baseline = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))
    heads, _ = source_heads_and_unapplied(frozenset())
    ready = {
        "status": "UP",
        "version": "2.0.0",
        "required": {"postgresql": "UP"},
        "controls": {"workspace_authorization": "ENFORCE"},
        "signals": {"delivery_backlog.detail": "pending=0", **signals},
    }

    def fake_get(_base_url: str, path: str, *, timeout: int = 30) -> tuple[int, Any]:
        return 200, baseline if path == "/openapi.json" else ready

    monkeypatch.setattr("check_deployment_parity.http_get_json", fake_get)
    monkeypatch.setattr("check_deployment_parity.read_deployed_revisions", lambda _url: (heads, ""))
    monkeypatch.setattr(
        "check_deployment_parity.read_deployed_settings",
        lambda **_kwargs: (source_setting_names(), ""),
    )
    monkeypatch.setattr("check_deployment_parity.read_deployed_manifest", lambda **_kwargs: None)


def test_a_stale_image_with_matching_schema_routes_and_settings_is_not_parity(
    monkeypatch: Any,
) -> None:
    """The regression itself: on 2026-09-19 every earlier comparison matched and the
    script exited 0 with "running this tree". An image that cannot show its code
    now leaves the verdict at "cannot tell"; one showing other code is drift."""
    _aligned_deployment(monkeypatch, {})
    assert main([]) == 2

    _aligned_deployment(monkeypatch, {SOURCE_DIGEST_SIGNAL: "b" * 64})
    assert main([]) == 1

    here = manifest_digest(source_manifest(REPO_ROOT))
    _aligned_deployment(monkeypatch, {SOURCE_DIGEST_SIGNAL: here})
    assert main([]) == 0
