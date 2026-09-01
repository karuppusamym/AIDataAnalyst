"""QG-3: per-LOB quotas + concurrency controller -- fair under contention.

Two levels, both proving the same property (LOB B's own quota-bounded load is
never delayed or starved by LOB A submitting far more concurrent work than
its own quota allows):

* `test_controller_keeps_lob_b_unaffected_by_lob_a_flood` exercises
  `aida.lob_concurrency.LobConcurrencyController` directly -- the pure
  fairness mechanism, with no database or connector involved.
* `test_query_gateway_wiring_keeps_lob_b_unaffected_by_lob_a_flood` exercises
  the real wiring: concurrent `QueryExecutionGateway.execute()` calls against
  two datasources in different lines of business, through the same doubles
  `tests/test_query_tokenization.py` already established for this gateway.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.lob_concurrency import LobConcurrencyController, LobConcurrencyDenied
from aida.models import DataSource
from aida.query_gateway import LobConcurrencyRejected, QueryExecutionGateway
from tests.support.doubles import CatalogSession, FakeSqlExecutor, security_context

# ---------------------------------------------------------------------------
# Level 1: the controller in isolation.
# ---------------------------------------------------------------------------


async def _hold_slot(
    controller: LobConcurrencyController,
    lob_key: str,
    work_seconds: float,
    completions: list[float],
    denials: list[LobConcurrencyDenied],
) -> None:
    started = time.monotonic()
    try:
        async with controller.slot(lob_key):
            await asyncio.sleep(work_seconds)
        completions.append(time.monotonic() - started)
    except LobConcurrencyDenied as exc:
        denials.append(exc)


async def test_controller_keeps_lob_b_unaffected_by_lob_a_flood() -> None:
    """LOB A floods its quota; LOB B's own normal load is untouched by it.

    Quota is 2 concurrent slots per LOB. LOB A submits 8 concurrent requests
    (4x its quota) that each hold a slot for 0.3s; LOB B submits 2 concurrent
    requests (exactly its own quota) that each hold a slot for 0.05s, at the
    same moment. Because slots are tracked per LOB key, LOB B's two requests
    can never be blocked by LOB A's queue -- they are proven to complete
    within their own hold time, not within anything that grows with LOB A's
    queue depth. LOB A's excess demand is proven bounded too: requests past
    the queue-timeout are rejected with a clear, distinguishable error rather
    than queuing forever.
    """
    controller = LobConcurrencyController(default_max_concurrent=2, queue_timeout_seconds=0.15)

    a_completions: list[float] = []
    a_denials: list[LobConcurrencyDenied] = []
    b_completions: list[float] = []
    b_denials: list[LobConcurrencyDenied] = []

    a_tasks = [
        asyncio.create_task(_hold_slot(controller, "lob-a", 0.3, a_completions, a_denials))
        for _ in range(8)
    ]
    b_tasks = [
        asyncio.create_task(_hold_slot(controller, "lob-b", 0.05, b_completions, b_denials))
        for _ in range(2)
    ]

    await asyncio.gather(*a_tasks, *b_tasks)

    # LOB B's own quota-sized load: nobody rejected, nobody delayed by LOB A's
    # 4x-over-quota flood. A generous margin (0.05s work, 0.3s bound) still
    # rules out LOB B having queued behind LOB A's holders (which release no
    # earlier than ~0.3s and ~0.45s).
    assert len(b_denials) == 0
    assert len(b_completions) == 2
    assert all(elapsed < 0.25 for elapsed in b_completions)

    # LOB A really was over its own quota: some of its 8 requests ran (up to
    # the 2-slot quota, in two waves), and the rest were rejected -- not
    # silently queued forever.
    assert len(a_completions) + len(a_denials) == 8
    assert len(a_denials) > 0
    assert len(a_completions) >= 2
    for denial in a_denials:
        assert denial.lob_key == "lob-a"
        assert denial.limit == 2


async def test_controller_rejects_with_a_distinguishable_error_not_a_silent_queue() -> None:
    """A single request past the queue-timeout bound is denied, not hung."""
    controller = LobConcurrencyController(default_max_concurrent=1, queue_timeout_seconds=0.05)

    async with controller.slot("lob-a"):
        with pytest.raises(LobConcurrencyDenied) as excinfo:
            await asyncio.wait_for(controller.acquire("lob-a"), timeout=1.0)
    assert excinfo.value.lob_key == "lob-a"
    assert excinfo.value.limit == 1
    assert "lob-a" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Level 2: the real gateway wiring.
# ---------------------------------------------------------------------------


def _datasource(*, line_of_business_id: object, credential_reference: str) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=line_of_business_id,
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="concurrency-source",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference=credential_reference,
        status="ACTIVE",
    )


class _SlowFakeSqlExecutor(FakeSqlExecutor):
    """`FakeSqlExecutor`, but the real dispatch call takes a configurable
    amount of wall-clock time -- what actually lets several `execute()` calls
    genuinely overlap in this test rather than racing through in one tick."""

    def __init__(self, rows: tuple[dict[str, object], ...], *, delay_seconds: float) -> None:
        super().__init__(rows)
        self._delay_seconds = delay_seconds

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> object:
        await asyncio.sleep(self._delay_seconds)
        return await super().execute_read_query(sql, timeout_seconds=timeout_seconds)


def _catalog_session() -> CatalogSession:
    return CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "customer_id")],
        sensitive_columns=[],
    )


async def _run_execution(
    gateway: QueryExecutionGateway,
    datasource: DataSource,
    *,
    completions: list[float],
    rejections: list[LobConcurrencyRejected],
) -> None:
    started = time.monotonic()
    session = _catalog_session()
    try:
        await gateway.execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=datasource.organization_id),
            correlation_id=f"corr-{uuid4()}",
            sql="SELECT customer_id FROM analytics.customers",
            requested_limit=10,
            semantic_version=None,
        )
        completions.append(time.monotonic() - started)
    except LobConcurrencyRejected as exc:
        rejections.append(exc)


async def test_query_gateway_wiring_keeps_lob_b_unaffected_by_lob_a_flood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real `QueryExecutionGateway.execute()` path, not just the controller.

    LOB A's datasource gets 8 concurrent `execute()` calls against a quota of
    2 (each call taking 0.3s to reach the source); LOB B's datasource gets 2
    concurrent `execute()` calls (its own quota, each taking 0.05s), at the
    same moment. LOB B's calls complete on their own schedule regardless of
    LOB A's 8-deep flood; some of LOB A's excess calls fail closed with
    `LobConcurrencyRejected` -- a `QueryRejected` distinguishable from every
    other rejection reason by `type(exc).__name__` -- and are recorded as
    REJECTED executions, not left hanging.
    """
    settings = Settings(
        _env_file=None,
        query_gateway_lob_max_concurrent=2,
        query_gateway_lob_queue_timeout_seconds=0.15,
    )
    gateway = QueryExecutionGateway(settings)

    datasource_a = _datasource(line_of_business_id=uuid4(), credential_reference="vault://source-a")
    datasource_b = _datasource(line_of_business_id=uuid4(), credential_reference="vault://source-b")

    executor_a = _SlowFakeSqlExecutor(({"customer_id": "C-1"},), delay_seconds=0.3)
    executor_b = _SlowFakeSqlExecutor(({"customer_id": "C-1"},), delay_seconds=0.05)
    # Keyed by the (already-resolved) dsn -- not by anything set from inside a
    # concurrently-running task -- so this stays race-free with several
    # `execute()` calls genuinely overlapping below.
    executors = {"vault://source-a": executor_a, "vault://source-b": executor_b}
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executors[dsn],
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: ref)}
        )(),
    )

    a_completions: list[float] = []
    a_rejections: list[LobConcurrencyRejected] = []
    b_completions: list[float] = []
    b_rejections: list[LobConcurrencyRejected] = []

    async def _run_a() -> None:
        await _run_execution(
            gateway, datasource_a, completions=a_completions, rejections=a_rejections
        )

    async def _run_b() -> None:
        await _run_execution(
            gateway, datasource_b, completions=b_completions, rejections=b_rejections
        )

    a_tasks = [asyncio.create_task(_run_a()) for _ in range(8)]
    b_tasks = [asyncio.create_task(_run_b()) for _ in range(2)]

    await asyncio.gather(*a_tasks, *b_tasks)

    assert len(b_rejections) == 0
    assert len(b_completions) == 2
    assert all(elapsed < 0.25 for elapsed in b_completions)

    assert len(a_completions) + len(a_rejections) == 8
    assert len(a_rejections) > 0
    for rejection in a_rejections:
        assert rejection.lob_key == str(datasource_a.line_of_business_id)
        assert "LOB_CONCURRENCY_LIMIT_EXCEEDED" in str(rejection)
