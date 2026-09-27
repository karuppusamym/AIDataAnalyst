"""`scripts/live_role_sweep.py` reads its one argument before it touches the stack.

Its first version took `sys.argv[1]` as the output path, so `--help` ran every request the sweep
makes -- 2,412 of them against whatever stack was up -- and then wrote the report to a file named
`--help` in the repository root. The sweep itself needs a running stack and is not run here; this
holds only the part that must never reach one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import live_role_sweep  # noqa: E402


def _refuse_the_stack(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the sweep reached for the stack before its arguments were read")


def test_help_prints_usage_and_exits_without_touching_the_stack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(live_role_sweep, "resolve_ids", _refuse_the_stack)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as stopped:
        live_role_sweep.main(["--help"])

    assert stopped.value.code == 0
    assert "usage:" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_flag_is_an_error_and_never_a_file_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(live_role_sweep, "resolve_ids", _refuse_the_stack)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as stopped:
        live_role_sweep.main(["--no-such-flag"])

    assert stopped.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_the_report_goes_where_asked_and_otherwise_to_the_working_directory() -> None:
    assert live_role_sweep.parse_args([]).output == "role_sweep.json"
    assert live_role_sweep.parse_args(["reports/sweep.json"]).output == "reports/sweep.json"
