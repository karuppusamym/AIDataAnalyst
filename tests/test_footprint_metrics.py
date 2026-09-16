"""R11-FP17: the footprint register as gauges, and the shapes a gauge must not take.

The register answers "what does Atlas not know about this source" for someone looking at a
screen. The failure it exists against is the one nobody looks at: a pass an operator never
turned on, or one that has fallen behind, so the backlog grows while every request still
succeeds. These pin the export:

* every kind is published on every pass, zero included, because a series that disappears when a
  queue drains is indistinguishable from a scrape that failed;
* the only label is the gap kind -- an organization or datasource id in a label is the unbounded
  cardinality F17 records the cost of, and would put a tenant identifier on a public surface;
* one organization's failure leaves the others' readings and is visible as a smaller sweep;
* the pass declines the same three ways every other scheduled pass does: disabled, not yet due,
  and stamped so a skip does not retry every tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY

from aida.config import Settings
from aida.footprint_gaps import GAP_DEFINITIONS

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {"_env_file": None, "environment": "test"}
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _reset_cadence() -> Any:
    from aida import footprint_metrics

    footprint_metrics._last_run_at = None
    yield
    footprint_metrics._last_run_at = None


def _gauge(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels or None)


class _Register:
    """One organization's gap register, as `footprint_gaps` returns it."""

    def __init__(self, totals: dict[str, int], oldest: int | None) -> None:
        self.totals = totals
        self.datasources = [
            type("_Source", (), {"oldest_pending_signal_minutes": oldest})()
        ]


def _sweep(monkeypatch: pytest.MonkeyPatch, *registers: Any) -> list[Any]:
    """Drive the pass over one register per organization, with no database."""
    from aida import footprint_metrics

    organization_ids = [uuid4() for _ in registers]
    seen: list[Any] = []

    class _Scalars:
        def all(self) -> list[Any]:
            return organization_ids

    class _Session:
        async def scalars(self, _statement: object) -> _Scalars:
            return _Scalars()

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr("aida.db.session_factory", lambda: _Session())

    answers = list(registers)

    async def _gaps(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["organization_id"])
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(footprint_metrics, "footprint_gaps", _gaps)
    return seen


async def test_every_kind_is_published_including_the_ones_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aida.footprint_metrics import run_footprint_metrics_pass

    _sweep(monkeypatch, _Register({"CODE_WITHHELD": 3}, 30))

    read = await run_footprint_metrics_pass(_settings(), now=_NOW)

    assert read == 1
    assert _gauge("aida_footprint_gaps", kind="CODE_WITHHELD") == 3
    # A queue at zero is a reading, not an absence: an alert on "no gaps" must be able to fire.
    for kind in GAP_DEFINITIONS:
        assert _gauge("aida_footprint_gaps", kind=kind) is not None
    assert _gauge("aida_footprint_gaps", kind="SOURCE_CHANGE_HOLDS") == 0
    assert _gauge("aida_footprint_oldest_pending_change_signal_seconds") == 1800


async def test_the_fleet_total_sums_organizations_and_takes_the_longest_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aida.footprint_metrics import run_footprint_metrics_pass

    _sweep(
        monkeypatch,
        _Register({"CODE_WITHHELD": 2, "SOURCE_CHANGE_HOLDS": 1}, 10),
        _Register({"CODE_WITHHELD": 5}, 90),
    )

    read = await run_footprint_metrics_pass(_settings(), now=_NOW)

    assert read == 2
    assert _gauge("aida_footprint_gaps", kind="CODE_WITHHELD") == 7
    assert _gauge("aida_footprint_gaps", kind="SOURCE_CHANGE_HOLDS") == 1
    # The oldest wait anywhere, not an average: an average hides the tenant that is stuck.
    assert _gauge("aida_footprint_oldest_pending_change_signal_seconds") == 5400


async def test_no_gauge_carries_a_tenant_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from aida.footprint_metrics import run_footprint_metrics_pass

    organizations = _sweep(monkeypatch, _Register({"CODE_WITHHELD": 1}, None))
    await run_footprint_metrics_pass(_settings(), now=_NOW)

    families = {
        metric.name: {label for sample in metric.samples for label in sample.labels}
        for metric in REGISTRY.collect()
        if metric.name.startswith("aida_footprint")
    }
    assert families, "the footprint gauges must be registered"
    for labels in families.values():
        assert labels <= {"kind"}, "a tenant id in a label is unbounded cardinality (F17)"
    # It read a real organization id; that identifier stays in the log, not on the surface.
    assert organizations


async def test_one_organizations_failure_leaves_the_others_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aida.footprint_metrics import run_footprint_metrics_pass

    _sweep(
        monkeypatch,
        RuntimeError("that tenant's gate is misconfigured"),
        _Register({"CODE_WITHHELD": 4}, None),
    )

    read = await run_footprint_metrics_pass(_settings(), now=_NOW)

    # One of two: the sweep is visibly partial rather than silently short.
    assert read == 1
    assert _gauge("aida_footprint_metrics_organizations") == 1
    assert _gauge("aida_footprint_gaps", kind="CODE_WITHHELD") == 4
    assert _gauge("aida_footprint_oldest_pending_change_signal_seconds") == 0


async def test_a_disabled_pass_does_nothing_and_says_nothing_ran() -> None:
    from aida.footprint_metrics import run_footprint_metrics_pass

    assert await run_footprint_metrics_pass(
        _settings(footprint_metrics_enabled=False), now=_NOW
    ) is None


async def test_a_pass_inside_its_interval_is_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    from aida import footprint_metrics

    _sweep(monkeypatch, _Register({}, None))
    settings = _settings()

    first = await footprint_metrics.run_footprint_metrics_pass(settings, now=_NOW)
    second = await footprint_metrics.run_footprint_metrics_pass(
        settings, now=_NOW + timedelta(seconds=30)
    )

    assert (first, second) == (1, None)


async def test_the_scheduler_runs_the_pass() -> None:
    """The pass exists to run unattended; an unwired pass is a gauge nobody updates."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "src/aida/workflows/scheduler.py"
    ).read_text("utf-8")
    called = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "run_footprint_metrics_pass" in called
