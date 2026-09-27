"""R11-FP02: a discovery run's receipt says what kind of thing it took in, and how completely.

"We scanned the source" and "we scanned it, but every routine body was withheld from our
principal" read the same in a run's counters. These tests drive the real `discover_datasource`
activity against in-memory SQLite and pin the receipt that keeps them apart -- including after a
failure part-way through the stream, when the receipt must say INTERRUPTED rather than go on
claiming a stream in progress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.testing import ActivityEnvironment

import aida.task_tracking as task_tracking
import aida.workflows.activities as activities
from aida.capability_states import (
    REASON_FACET_QUERY_FAILED,
    REASON_SOURCE_DENIED_READ,
    CapabilityState,
)
from aida.config import Settings
from aida.connectors.base import (
    Connector,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredRoutine,
    DiscoveredSchema,
    DiscoveredTable,
    DiscoveredViewDefinition,
    TableProfileSnapshot,
)
from aida.db import Base
from aida.discovery_receipt import FACET_OBJECT_VISIBILITY, DiscoveryReceipt
from aida.models import AnalysisRun, DataDomain, DataSource, LineOfBusiness, Organization, Project


def _columns() -> tuple[DiscoveredColumn, ...]:
    return (
        DiscoveredColumn(name="id", ordinal_position=1, physical_type="bigint", nullable=False),
    )


def _view(name: str, sql: str | None, *, truncated: bool = False) -> DiscoveredTable:
    return DiscoveredTable(
        name=name,
        object_type="VIEW",
        columns=_columns(),
        view_definition=DiscoveredViewDefinition(
            definition_sql=sql,
            truncated=truncated,
            unavailable_reason=None if sql else "module is encrypted or not visible",
        ),
    )


def _batch(schema: str) -> tuple[DiscoveredCatalog, ...]:
    return (
        DiscoveredCatalog(
            name="bank",
            schemas=(
                DiscoveredSchema(
                    name=schema,
                    tables=(
                        DiscoveredTable(
                            name="orders", object_type="BASE_TABLE", columns=_columns()
                        ),
                        _view("v_open", "SELECT id FROM orders"),
                        _view("v_secret", None),
                        _view("v_long", "SELECT id FROM orders", truncated=True),
                    ),
                    routines=(
                        DiscoveredRoutine(name="refresh", routine_type="PROCEDURE", body_sql=None),
                        DiscoveredRoutine(
                            name="net", routine_type="FUNCTION", body_sql="SELECT 1"
                        ),
                    ),
                ),
            ),
        ),
    )


def test_the_receipt_counts_kinds_and_keeps_withheld_code_apart_from_captured() -> None:
    receipt = DiscoveryReceipt(
        mode="INCREMENTAL",
        selection_fingerprint=None,
        capabilities={"views": True, "routines": True, "grants": False},
    )

    receipt.observe_batch(_batch("retail"), {"VIEW": 2})
    body = receipt.as_json("COMPLETE")

    # R11-FP02: `invisible` is None here -- this source was never asked what it hides, which
    # is not the same claim as "nothing".
    assert body["kinds"]["TABLE"] == {"discovered": 1, "excluded": 0, "invisible": None}
    assert body["kinds"]["VIEW"] == {"discovered": 3, "excluded": 2, "invisible": None}
    assert body["kinds"]["PROCEDURE"] == {"discovered": 1, "excluded": 0, "invisible": None}
    # Review 2026-09-16 §5 added `state` and `reason` beside the counters, in the shared
    # vocabulary (`aida.capability_states`). The four keys that were here before keep their
    # names and their values, which is what makes receipt version 3 purely additive.
    assert body["facets"]["view_definitions"] == {
        "support": "SUPPORTED",
        "captured": 2,
        "withheld": 1,
        "truncated": 1,
        "state": "TRUNCATED",
        "reason": "SOURCE_TEXT_TRUNCATED",
    }
    assert body["facets"]["routine_bodies"]["withheld"] == 1
    assert body["facets"]["grants"] == {
        "support": "UNSUPPORTED",
        "state": "UNSUPPORTED",
        "reason": None,
    }
    assert body["reconciliation"] == {"performed": False, "reason": "INCREMENTAL_MODE"}


class _Connector(Connector):
    connector_type = "postgres"
    dialect = "postgres"

    def __init__(
        self, batches: list[tuple[DiscoveredCatalog, ...]], fail_after: int | None
    ) -> None:
        self._batches = batches
        self._fail_after = fail_after

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(views=True, routines=False)

    async def test_connection(self) -> None:
        return None

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        for index, batch in enumerate(self._batches, start=1):
            if self._fail_after is not None and index > self._fail_after:
                raise RuntimeError("source connection dropped mid-stream")
            yield batch

    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        return TableProfileSnapshot(None, 0, ())


class _StubSecretResolver:
    def resolve(self, reference: str) -> str:
        return "postgresql://irrelevant/irrelevant"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _run(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    fail_after: int | None = None,
) -> tuple[UUID, AnalysisRun]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(id=uuid4(), organization_id=org.id, name="R", code=f"R{uuid4().hex[:6]}")
    domain = DataDomain(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id, name="D",
        code=f"D{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id, data_domain_id=domain.id,
        name="P", slug=f"p-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id, data_domain_id=domain.id,
        project_id=project.id, name="primary", connector_type="postgres", dialect="postgres",
        environment="PROD", network_zone="default", credential_reference="env://TEST_DSN",
        capabilities={}, status="ACTIVE",
    )
    run = AnalysisRun(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, mode=mode,
        trigger_type="MANUAL", status="QUEUED",
    )
    session.add_all([org, lob, domain, project, datasource, run])
    await session.commit()
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(activities, "SecretResolver", _StubSecretResolver)
    connector = _Connector([_batch("retail"), _batch("finance")], fail_after)
    monkeypatch.setattr(activities.connector_registry, "create", lambda kind, dsn: connector)
    run_id = run.id
    try:
        await ActivityEnvironment().run(activities.discover_datasource, str(run_id))
    except RuntimeError:
        pass
    refreshed = await session.get(AnalysisRun, run_id)
    assert refreshed is not None
    return run_id, refreshed


@pytest.mark.asyncio
async def test_a_finished_full_run_records_a_complete_receipt_with_its_reconciliation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = await _run(session, monkeypatch, mode="FULL")

    receipt = run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"] == {"state": "COMPLETE", "batches": 2}
    assert receipt["kinds"]["VIEW"]["discovered"] == 6
    assert receipt["kinds"]["SCHEMA"]["discovered"] == 2
    # The connector reports it collects no routines: the receipt says so rather than
    # presenting two withheld bodies as a capture problem of the source.
    assert receipt["facets"]["routine_bodies"]["support"] == "UNSUPPORTED"
    assert receipt["facets"]["view_definitions"]["withheld"] == 2
    assert receipt["reconciliation"]["performed"] is True


@pytest.mark.asyncio
async def test_a_run_that_fails_mid_stream_keeps_an_interrupted_receipt(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = await _run(session, monkeypatch, mode="FULL", fail_after=1)

    assert run.status == "FAILED"
    receipt = run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"] == {"state": "INTERRUPTED", "batches": 1}
    assert receipt["kinds"]["VIEW"]["discovered"] == 3
    # A FULL run that never saw its whole stream reconciles nothing.
    assert receipt["reconciliation"] == {"performed": False, "reason": "STREAM_NOT_FINISHED"}


def test_what_the_login_may_not_see_is_counted_apart_from_what_it_returned() -> None:
    """R11-FP02: a source that can be asked says how much of itself it kept back."""
    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={"views": True, "routines": True},
        invisible={"TABLE": 412, "MATERIALIZED_VIEW": 3},
    )

    receipt.observe_batch(_batch("retail"), {})
    body = receipt.as_json("COMPLETE")

    assert body["kinds"]["TABLE"] == {"discovered": 1, "excluded": 0, "invisible": 412}
    # A kind this run returned none of still appears when the source hides some of it.
    assert body["kinds"]["MATERIALIZED_VIEW"] == {
        "discovered": 0,
        "excluded": 0,
        "invisible": 3,
    }
    # Asked and told none is zero, which is a different answer from never asked.
    assert body["kinds"]["VIEW"]["invisible"] == 0


# ---------------------------------------------------------------------------
# Review 2026-09-16 §5: a facet whose read did not complete is recorded rather
# than failing the run, and a refusal is told apart from a failure.
#
# Tracker R11-FP01 and R11-FP02 both name this as remaining: PERMISSION_DENIED
# existed nowhere in code, so a facet the source refused either failed the
# whole scan or was indistinguishable from one that came back empty. A login
# granted one schema of five should produce a receipt that says so.
# ---------------------------------------------------------------------------


def test_a_refused_facet_is_recorded_rather_than_failing_the_run() -> None:
    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={"views": True, "routines": True, "grants": True},
    )
    receipt.observe_batch(_batch("retail"), {})
    receipt.record_facet_outcome(
        "grants", state=CapabilityState.PERMISSION_DENIED, reason=REASON_SOURCE_DENIED_READ
    )

    body = receipt.as_json("COMPLETE")

    assert body["facets"]["grants"]["state"] == "PERMISSION_DENIED"
    assert body["facets"]["grants"]["reason"] == "SOURCE_DENIED_READ"
    # The run still finished, and everything else it read is still counted.
    assert body["stream"]["state"] == "COMPLETE"
    assert body["kinds"]["TABLE"]["discovered"] == 1


def test_a_refusal_is_not_confused_with_an_unimplemented_axis() -> None:
    """UNSUPPORTED outranks a recorded outcome: an adapter that does not collect
    the facet had no read to refuse, and saying PERMISSION_DENIED would send a
    source administrator to grant access that would change nothing."""
    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={"views": True, "routines": True, "grants": False},
    )
    receipt.record_facet_outcome(
        "grants", state=CapabilityState.PERMISSION_DENIED, reason=REASON_SOURCE_DENIED_READ
    )
    assert receipt.as_json("COMPLETE")["facets"]["grants"]["state"] == "UNSUPPORTED"


def test_a_free_text_reason_never_reaches_a_receipt() -> None:
    """INV-6: a source's own explanation of what it withheld routinely quotes a
    row, so the reason passes through a closed vocabulary first."""
    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={"views": True},
    )
    receipt.record_facet_outcome(
        "view_definitions",
        state=CapabilityState.PERMISSION_DENIED,
        reason="permission denied for relation customers where ssn = '123-45-6789'",
    )
    body = receipt.as_json("COMPLETE")
    assert body["facets"]["view_definitions"]["reason"] == "UNRECORDED"
    assert "123-45-6789" not in str(body)


def test_a_code_facets_state_follows_its_counters() -> None:
    """The state a reader sees is derived from the counts already there, by a
    documented precedence: refusal, then truncation, then a withheld share."""
    partial = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={"routines": True}
    )
    partial.routine_bodies.update({"captured": 3, "withheld": 1})
    assert partial.as_json("COMPLETE")["facets"]["routine_bodies"]["state"] == "PARTIAL"

    nothing = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={"routines": True}
    )
    nothing.routine_bodies.update({"withheld": 4})
    assert nothing.as_json("COMPLETE")["facets"]["routine_bodies"]["state"] == "UNAVAILABLE"

    whole = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={"routines": True}
    )
    whole.routine_bodies.update({"captured": 4})
    assert whole.as_json("COMPLETE")["facets"]["routine_bodies"]["state"] == "SUPPORTED"


def test_the_visibility_facet_separates_refused_from_unaskable() -> None:
    """`count_invisible_objects` answers None on every adapter but PostgreSQL's,
    which is "we cannot ask" -- not a refusal, and not a false zero."""
    unaskable = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={}, invisible=None
    )
    facet = unaskable.as_json("COMPLETE")["facets"]["object_visibility"]
    assert facet == {
        "state": "UNAVAILABLE",
        "reason": "ADAPTER_NOT_IMPLEMENTED",
        "asked": False,
    }

    asked = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={}, invisible={"TABLE": 0}
    )
    assert asked.as_json("COMPLETE")["facets"]["object_visibility"] == {
        "state": "SUPPORTED",
        "reason": None,
        "asked": True,
    }

    refused = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={}, invisible=None
    )
    refused.record_facet_outcome(
        FACET_OBJECT_VISIBILITY,
        state=CapabilityState.PERMISSION_DENIED,
        reason=REASON_SOURCE_DENIED_READ,
    )
    assert refused.as_json("COMPLETE")["facets"]["object_visibility"]["state"] == (
        "PERMISSION_DENIED"
    )


def test_the_receipt_version_records_the_additive_change() -> None:
    body = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={}
    ).as_json("COMPLETE")
    assert body["receipt_version"] == 3


async def test_a_source_that_refuses_the_visibility_question_does_not_fail_the_run() -> None:
    """The wiring, not just the receipt: `_count_invisible` classifies a refusal
    by SQLSTATE and records it, and the run goes on reading what it may."""
    outcome: dict[str, tuple[CapabilityState, str]] = {}

    class _Refusing:
        async def count_invisible_objects(self) -> dict[str, int] | None:
            raise _PrivilegeError

    assert await activities._count_invisible(_Refusing(), outcome) is None  # type: ignore[arg-type]
    assert outcome[FACET_OBJECT_VISIBILITY] == (
        CapabilityState.PERMISSION_DENIED,
        REASON_SOURCE_DENIED_READ,
    )


async def test_a_failure_that_is_not_a_refusal_is_recorded_as_unavailable() -> None:
    """Under-claiming is the correct direction (INV-9): a driver that reports no
    SQLSTATE gets "we did not get it", never a guess at the source's intent."""
    outcome: dict[str, tuple[CapabilityState, str]] = {}

    class _Failing:
        async def count_invisible_objects(self) -> dict[str, int] | None:
            raise TimeoutError("statement timeout")

    assert await activities._count_invisible(_Failing(), outcome) is None  # type: ignore[arg-type]
    assert outcome[FACET_OBJECT_VISIBILITY] == (
        CapabilityState.UNAVAILABLE,
        REASON_FACET_QUERY_FAILED,
    )


class _PrivilegeError(Exception):
    sqlstate = "42501"
