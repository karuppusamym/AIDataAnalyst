"""The persisted vector index gets rebuilt on a cadence, or it is not an index.

RT-1 built `rebuild_vector_index` and nothing scheduled it: the only caller was
an operator endpoint whose UI does not exist (R11-X5 records that cluster as
missing one). The consequence is not an outage, which is what makes it the kind
of defect a dashboard never surfaces — the index simply goes stale, the
freshness check correctly stops trusting it, and the vector channel falls back
to embedding **every candidate on every query**. Retrieval keeps returning good
answers at a provider bill that grows with the estate and the traffic at the
same time.

These pin the pass and, deliberately, the four ways it must decline to run.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.embedding_provider import EmbeddingUnavailable

_SCHEDULER = Path(__file__).resolve().parent.parent / "src/aida/workflows/scheduler.py"
_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {"_env_file": None, "environment": "test"}
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _reset_cadence() -> Any:
    """The last-run stamp is module state, so one test must not arm the next."""
    from aida import vector_index_service

    vector_index_service._index_rebuild_last_run_at = None
    yield
    vector_index_service._index_rebuild_last_run_at = None


async def test_a_disabled_pass_does_nothing_and_says_nothing_ran() -> None:
    from aida.vector_index_service import run_vector_index_rebuild_pass

    result = await run_vector_index_rebuild_pass(
        _settings(vector_index_rebuild_enabled=False), now=_NOW
    )

    assert result is None


async def test_an_unconfigured_provider_is_a_skip_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`embedding_provider` defaults to `unset`, which is the shipped state.

    An unconfigured deployment must not log an exception on every scheduler
    tick -- it must do nothing and say why. Anything noisier trains operators to
    ignore the scheduler's log, which is where the real failures appear.
    """
    from aida import vector_index_service

    def _refuse(*args: object, **kwargs: object) -> None:
        raise EmbeddingUnavailable("EMBEDDING_PROVIDER_NOT_CONFIGURED")

    monkeypatch.setattr(vector_index_service, "resolve_embedding_provider", _refuse)
    called: list[object] = []
    monkeypatch.setattr(
        vector_index_service,
        "rebuild_vector_index",
        lambda *a, **k: called.append(a),
    )

    result = await vector_index_service.run_vector_index_rebuild_pass(_settings(), now=_NOW)

    assert result is None
    assert called == [], "a pass with no provider must not reach the rebuild"


async def test_an_unconfigured_provider_is_asked_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip stamps the clock too.

    Without that, an unconfigured deployment resolves a provider it does not
    have on every single tick -- cheap individually, and exactly the kind of
    per-tick work that makes a scheduler's cost impossible to reason about.
    """
    from aida import vector_index_service

    attempts = 0

    def _refuse(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise EmbeddingUnavailable("EMBEDDING_PROVIDER_NOT_CONFIGURED")

    monkeypatch.setattr(vector_index_service, "resolve_embedding_provider", _refuse)
    settings = _settings()

    await vector_index_service.run_vector_index_rebuild_pass(settings, now=_NOW)
    await vector_index_service.run_vector_index_rebuild_pass(
        settings, now=_NOW + timedelta(minutes=1)
    )

    assert attempts == 1


async def test_a_pass_inside_its_interval_is_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    from aida import vector_index_service

    vector_index_service._index_rebuild_last_run_at = _NOW

    result = await vector_index_service.run_vector_index_rebuild_pass(
        _settings(), now=_NOW + timedelta(hours=1)
    )

    assert result is None


def test_the_scheduler_iteration_actually_calls_the_rebuild_pass() -> None:
    """The regression this row is about is a correct function with no caller.

    A behavioural test of `run_scheduler_iteration` needs a live Temporal
    client, so this reads the call out of the source instead. It fails the
    moment the call is dropped -- which is the exact state RT-1's index was
    found in, months after it was built.
    """
    tree = ast.parse(_SCHEDULER.read_text(encoding="utf-8"), filename=str(_SCHEDULER))
    iteration = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_scheduler_iteration"
    )
    called = {
        node.func.id
        for node in ast.walk(iteration)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "run_vector_index_rebuild_pass" in called, (
        "run_scheduler_iteration no longer calls run_vector_index_rebuild_pass; the "
        "persisted vector index has no scheduled writer again, and retrieval will "
        "silently pay a provider call per candidate per query instead"
    )


def test_the_pass_is_reachable_from_a_real_entry_point() -> None:
    """`tests/test_reachability_gate.py` walks the import graph from the five
    real entry points. The scheduler is one of them, so importing the pass
    there is what makes this code reachable rather than merely importable --
    the distinction the capability register's `Reachable` column turns on."""
    source = _SCHEDULER.read_text(encoding="utf-8")

    assert "from aida.vector_index_service import run_vector_index_rebuild_pass" in source


async def test_one_organizations_failure_does_not_abort_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-organization isolation, and the cost of a skip is bounded.

    A failed rebuild leaves that organization's index exactly as stale as it
    already was and the vector channel answering by the live path, so the right
    behaviour is to log it and carry on to the next tenant rather than lose the
    whole sweep to one bad estate.
    """
    from aida import vector_index_service

    org_ids = [uuid4(), uuid4(), uuid4()]
    seen: list[object] = []

    class _Result:
        considered = 3
        embedded = 3
        skipped_unchanged = 0
        backend = "postgres_bruteforce"

    async def _rebuild(session: object, organization_id: object, **kwargs: object) -> _Result:
        seen.append(organization_id)
        if organization_id == org_ids[1]:
            raise RuntimeError("index backend refused")
        return _Result()

    monkeypatch.setattr(
        vector_index_service, "resolve_embedding_provider", lambda *a, **k: object()
    )
    monkeypatch.setattr(vector_index_service, "rebuild_vector_index", _rebuild)
    # `session_factory` is imported *inside* the pass (a local import keeps the
    # module graph acyclic), so it resolves from `aida.db` at call time and
    # patching the service module does nothing -- the first version of this test
    # silently ran against the real development database and listed its four
    # real organizations.
    from aida import db as aida_db

    monkeypatch.setattr(aida_db, "session_factory", _session_factory_yielding(org_ids))

    result = await vector_index_service.run_vector_index_rebuild_pass(_settings(), now=_NOW)

    assert seen == org_ids, "the sweep stopped at the failing organization"
    assert result == 2, "the two healthy organizations should still count as rebuilt"


def _session_factory_yielding(org_ids: list[Any]) -> Any:
    """A `session_factory()` stand-in whose first session lists these orgs.

    The pass opens one session to choose the batch and one per organization to
    rebuild it, so the double answers the `scalars` call once and then behaves
    as an inert session.
    """

    class _Scalars:
        def __init__(self, values: list[Any]) -> None:
            self._values = values

        def all(self) -> list[Any]:
            return self._values

    class _Session:
        def __init__(self, values: list[Any]) -> None:
            self._values = values

        async def scalars(self, statement: object) -> _Scalars:
            return _Scalars(self._values)

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    first = [True]

    def _factory() -> _Session:
        if first[0]:
            first[0] = False
            return _Session(org_ids)
        return _Session([])

    return _factory
