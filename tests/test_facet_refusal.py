"""R11-FP02: a facet the source refuses costs that facet, not the whole run.

The receipt could already *hold* a per-facet `PERMISSION_DENIED` (review 2026-09-16 §5), and
one read was already classified into it -- the invisible-object count. Every other facet was
still all-or-nothing: discovery reads a roster, then constraints, indexes, comments, grants,
view definitions and routine bodies, each its own statement against its own catalog relation,
and one refusal propagated out of `discover_streaming` and failed the run. A login missing
SELECT on one relation cost a whole scan, and the receipt said INTERRUPTED without saying why.

These tests pin the mechanism (`connectors.discovery.read_facet`), the wiring (the real
`discover_datasource` activity over in-memory SQLite, driven by a connector that adopts the
helper exactly as a real adapter would), the consequence that matters most -- a refused facet's
existing objects are *not* retired -- and the two surfaces that must read a refusal as a
refusal rather than as an empty facet.

The live half is `tests/test_facet_refusal_live.py`, which refuses the real
`PostgresConnector` view-definition query against real PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.testing import ActivityEnvironment

import aida.workflows.activities as activities
from aida.capability_states import (
    REASON_FACET_QUERY_FAILED,
    REASON_SOURCE_DENIED_READ,
    CapabilityState,
)
from aida.connectors.base import (
    Connector,
    ConnectorCapabilities,
    DiscoveredCatalog,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    FACET_GRANTS,
    FACET_INVENTORY,
    assemble_catalog,
    build_grants,
    build_table_map_from_column_rows,
    classify_read_failure,
    facet_read_scope,
    read_facet,
)
from aida.discovery_receipt import FACET_OBJECT_VISIBILITY, DiscoveryReceipt
from aida.envelope_models import MetadataSourceGrant
from aida.footprint_gap_detail import REFUSED_READS, footprint_gap_objects
from aida.footprint_gaps import GAP_DEFINITIONS, footprint_gaps
from aida.models import AnalysisRun, MetadataSchema, MetadataTable
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    task_agent_session,
)
from tests.test_discover_datasource_streaming import (
    _patch_activity_plumbing,
    _seed_datasource_with_legacy_table,
)
from tests.test_discover_datasource_streaming import session as streaming_session  # noqa: F401

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


class _PrivilegeError(Exception):
    """A driver error that reports the SQLSTATE a refusal reports.

    Deliberately carries a message that quotes a row value, because that is what a real
    driver does and what INV-6 forbids persisting: `tests/test_facet_refusal_live.py` shows
    the real asyncpg error this stands in for.
    """

    sqlstate = "42501"

    def __init__(self) -> None:
        super().__init__("permission denied for relation customer where ssn = '123-45-6789'")


class _TransportError(Exception):
    """A failure that is not a refusal: no SQLSTATE, and the next read will fail too."""


async def _raises(exc: BaseException) -> Sequence[Any]:
    raise exc


async def _rows(rows: Sequence[Any]) -> Sequence[Any]:
    return rows


# ---------------------------------------------------------------------------
# The mechanism.
# ---------------------------------------------------------------------------


async def test_a_refused_facet_yields_no_rows_and_is_recorded() -> None:
    with facet_read_scope() as scope:
        rows = await read_facet(FACET_GRANTS, _raises(_PrivilegeError()))

    assert list(rows) == []
    assert scope.outcomes == {
        FACET_GRANTS: (CapabilityState.PERMISSION_DENIED, REASON_SOURCE_DENIED_READ)
    }


async def test_a_failure_that_is_not_a_refusal_still_fails_loudly() -> None:
    """A timeout or a dropped connection is not one facet's problem: the next read will
    fail too, and absorbing it would let a FULL run reconcile against a source that had
    stopped answering. It is recorded -- as UNAVAILABLE, the under-claiming answer INV-9
    requires -- and then re-raised."""
    with facet_read_scope() as scope, pytest.raises(_TransportError):
        await read_facet(FACET_GRANTS, _raises(_TransportError("connection reset")))

    assert scope.outcomes == {
        FACET_GRANTS: (CapabilityState.UNAVAILABLE, REASON_FACET_QUERY_FAILED)
    }


async def test_a_refused_inventory_is_recorded_and_still_fails_the_run() -> None:
    """The inventory is the run. A refused roster returns no objects at all, and a FULL run
    that completed over no objects would retire every table it already held -- so this one
    refusal is recorded (the INTERRUPTED receipt names it) and then allowed to fail the run.
    `discovery_selection`'s rule cuts the other way for everything else: an object that was
    not looked at is not missing."""
    with facet_read_scope() as scope, pytest.raises(_PrivilegeError):
        await read_facet(FACET_INVENTORY, _raises(_PrivilegeError()))

    assert scope.outcomes == {
        FACET_INVENTORY: (CapabilityState.PERMISSION_DENIED, REASON_SOURCE_DENIED_READ)
    }


async def test_outside_a_scope_a_refusal_is_never_absorbed() -> None:
    """A refusal nobody is recording must keep failing: absorbed with no scope it would
    become an empty facet that no receipt explains."""
    with pytest.raises(_PrivilegeError):
        await read_facet(FACET_GRANTS, _raises(_PrivilegeError()))


async def test_a_facet_name_the_receipt_cannot_publish_is_refused() -> None:
    """The one failure mode worse than an unrecorded refusal is a recorded one that no
    surface publishes, so the name is checked where it is written, not where it is read."""
    unread = _rows([])
    with facet_read_scope(), pytest.raises(ValueError, match="unknown discovery facet"):
        await read_facet("grnats", unread)
    unread.close()  # the name is refused before the query is even run
    receipt = DiscoveryReceipt(mode="FULL", selection_fingerprint=None, capabilities={})
    with pytest.raises(ValueError, match="unknown receipt facet"):
        receipt.record_facet_outcome(
            "grnats",
            state=CapabilityState.PERMISSION_DENIED,
            reason=REASON_SOURCE_DENIED_READ,
        )


async def test_the_visibility_question_and_a_facet_read_are_classified_by_one_rule() -> None:
    """`_count_invisible` and `read_facet` share `classify_read_failure`: two copies of this
    rule would be two places for a driver's message to start leaking into an audit trail."""
    assert classify_read_failure(_PrivilegeError()) == (
        CapabilityState.PERMISSION_DENIED,
        REASON_SOURCE_DENIED_READ,
    )
    assert classify_read_failure(_TransportError()) == (
        CapabilityState.UNAVAILABLE,
        REASON_FACET_QUERY_FAILED,
    )
    outcome: dict[str, tuple[CapabilityState, str]] = {}

    class _Refusing:
        async def count_invisible_objects(self) -> dict[str, int] | None:
            raise _PrivilegeError

    assert await activities._count_invisible(_Refusing(), outcome) is None  # type: ignore[arg-type]
    assert outcome[FACET_OBJECT_VISIBILITY] == (
        CapabilityState.PERMISSION_DENIED,
        REASON_SOURCE_DENIED_READ,
    )


# ---------------------------------------------------------------------------
# The wiring: the real activity, and a connector that adopts the helper.
# ---------------------------------------------------------------------------


class _GrantRefusingConnector(Connector):
    """Adopts `read_facet` for its grants query, the way an adapter is meant to.

    The six real adapters are owned by another work stream this cycle, so the adoption is
    shown here through the same helper and the same assembly path a real connector uses --
    `read_facet` around the facet's own query, then `build_grants` and `assemble_catalog`
    over whatever came back. Nothing about the mechanism is connector-specific: the scope is
    ambient precisely so a connector needs no new argument to report a refused facet.
    """

    connector_type = "postgres"
    dialect = "postgres"

    def __init__(self, tables: Sequence[str], *, refuse_grants: bool) -> None:
        self._tables = tables
        self._refuse_grants = refuse_grants
        self.grant_reads = 0

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(views=True, routines=True, grants=True)

    async def test_connection(self) -> None:
        return None

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError("this connector only exercises discover_streaming")

    async def _read_grants(self) -> Sequence[dict[str, Any]]:
        self.grant_reads += 1
        if self._refuse_grants:
            raise _PrivilegeError
        return [
            {
                "schema_name": "retail",
                "grantee": "reporting",
                "privilege": "SELECT",
                "object_type": "TABLE",
                "object_name": self._tables[0],
            }
        ]

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        for table_name in self._tables:
            tables = build_table_map_from_column_rows(
                [
                    {
                        "table_schema": "retail",
                        "table_name": table_name,
                        "table_type": "BASE TABLE",
                        "column_name": "id",
                        "ordinal_position": 1,
                        "data_type": "bigint",
                        "is_nullable": "NO",
                    }
                ]
            )
            grant_rows = await read_facet(FACET_GRANTS, self._read_grants())
            yield assemble_catalog("bank", tables, grants=build_grants(grant_rows))

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


async def _seeded_grant(session: AsyncSession, datasource: Any) -> MetadataSourceGrant:
    """One grant an earlier, better-privileged run captured."""
    schema_id = await session.scalar(
        select(MetadataSchema.id).where(MetadataSchema.name == "retail")
    )
    grant = MetadataSourceGrant(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema_id,
        grant_key="a" * 64,
        grantee="reporting",
        grantee_type="ROLE",
        privilege="SELECT",
        object_type="TABLE",
        object_name="legacy_table",
        schema_name="retail",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(grant)
    await session.commit()
    return grant


async def _run_discovery(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    connector: Connector,
    datasource: Any,
) -> tuple[dict[str, Any], AnalysisRun]:
    run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.commit()
    _patch_activity_plumbing(monkeypatch, session)
    monkeypatch.setattr(
        activities.connector_registry, "create", lambda connector_type, dsn: connector
    )
    result = await ActivityEnvironment().run(activities.discover_datasource, str(run.id))
    persisted = await session.get(AnalysisRun, run.id)
    assert persisted is not None
    return result, persisted


async def test_a_refused_grants_read_records_the_facet_and_the_run_completes(
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline: one facet refused, every other facet intact, and a COMPLETE run.

    Before this change the same refusal raised out of `discover_streaming` and the run was
    marked FAILED with nothing discovered -- one missing grant losing a whole estate.
    """
    datasource, _ = await _seed_datasource_with_legacy_table(streaming_session)
    connector = _GrantRefusingConnector(["account", "customer"], refuse_grants=True)

    result, run = await _run_discovery(streaming_session, monkeypatch, connector, datasource)

    assert result["status"] == "COMPLETED"
    receipt = run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"] == {"state": "COMPLETE", "batches": 2}
    assert receipt["facets"]["grants"] == {
        "support": "SUPPORTED",
        "state": "PERMISSION_DENIED",
        "reason": "SOURCE_DENIED_READ",
    }
    # The facets that did come back are untouched by another facet's refusal.
    assert receipt["facets"]["view_definitions"]["state"] == "SUPPORTED"
    assert receipt["kinds"]["TABLE"]["discovered"] == 2
    names = set((await streaming_session.scalars(select(MetadataTable.name))).all())
    assert {"account", "customer"} <= names


async def test_a_driver_message_never_reaches_the_receipt(
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6. The refusal this run absorbed carries a message quoting a row value; the
    receipt carries a state and a reason code, and the run's own error text is a constant."""
    datasource, _ = await _seed_datasource_with_legacy_table(streaming_session)
    connector = _GrantRefusingConnector(["account"], refuse_grants=True)

    _, run = await _run_discovery(streaming_session, monkeypatch, connector, datasource)

    assert "123-45-6789" not in str(run.discovery_receipt)
    assert "permission denied" not in str(run.discovery_receipt).lower()


async def test_a_refused_facet_retires_nothing_it_could_not_read(
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consequence that matters more than the receipt.

    A FULL run retires what it did not see, so a refused grants read -- which returns no
    rows at all -- would tombstone every grant an earlier run captured: the refusal costing
    the estate rather than the facet. `refused_facet_existing` counts them as seen, on the
    same reasoning as `out_of_scope_existing` (R11-FP01: an object that was not looked for
    is not missing). Reconciliation still runs, which the retired legacy table proves.
    """
    datasource, legacy_table = await _seed_datasource_with_legacy_table(streaming_session)
    grant = await _seeded_grant(streaming_session, datasource)
    connector = _GrantRefusingConnector(["account"], refuse_grants=True)

    await _run_discovery(streaming_session, monkeypatch, connector, datasource)

    kept = await streaming_session.get(MetadataSourceGrant, grant.id)
    assert kept is not None
    assert kept.status == "ACTIVE"
    # ... and the pass that would have retired it did run: a table this snapshot genuinely
    # did not carry is tombstoned, so the grant's survival is the refusal rule, not a
    # reconciliation that never happened.
    retired = await streaming_session.get(MetadataTable, legacy_table.id)
    assert retired is not None
    assert retired.status == "DEPRECATED"


async def test_a_facet_that_was_read_is_still_reconciled(
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same rule: when the grants read succeeds, a grant the source
    no longer reports is retired exactly as before. The refusal rule must not become a
    blanket amnesty for an axis a run really did read."""
    datasource, _ = await _seed_datasource_with_legacy_table(streaming_session)
    grant = await _seeded_grant(streaming_session, datasource)
    connector = _GrantRefusingConnector(["account"], refuse_grants=False)

    _, run = await _run_discovery(streaming_session, monkeypatch, connector, datasource)

    retired = await streaming_session.get(MetadataSourceGrant, grant.id)
    assert retired is not None
    assert retired.status == "DEPRECATED"
    assert run.discovery_receipt is not None
    assert run.discovery_receipt["facets"]["grants"]["state"] == "SUPPORTED"


# ---------------------------------------------------------------------------
# The consumers: a refusal has to read as a refusal.
# ---------------------------------------------------------------------------


@pytest.fixture
async def gap_session() -> AsyncIterator[AsyncSession]:
    async with task_agent_session() as active:
        yield active


def _receipt_with_refusal(org: Any, datasource: Any, *, facet: str, state: str) -> AnalysisRun:
    return AnalysisRun(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        mode="FULL",
        status="COMPLETED",
        created_at=NOW,
        updated_at=NOW,
        discovery_receipt={
            "kinds": {"TABLE": {"discovered": 4, "excluded": 0, "invisible": None}},
            "facets": {facet: {"support": "SUPPORTED", "state": state, "reason": "X"}},
        },
    )


async def test_a_refused_facet_is_its_own_gap_with_a_route(gap_session: AsyncSession) -> None:
    """R11-FP02. Every other gap counts objects, and a refused read returns none -- so
    without this kind one missing grant presents as a clean source. `invisible: null` on the
    same receipt is deliberately no gap at all ("we could not ask"), which is exactly why
    the refusal needs its own."""
    org, datasource, _ = await seed_estate(gap_session)
    gap_session.add(
        _receipt_with_refusal(org, datasource, facet="grants", state="PERMISSION_DENIED")
    )
    await gap_session.commit()

    result = await footprint_gaps(
        gap_session,
        context=human(org, "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=org.id,
        now=NOW,
    )

    (listed,) = result.datasources
    (gap,) = [item for item in listed.gaps if item.kind == "SOURCE_READS_REFUSED"]
    assert gap.count == 1
    assert (gap.resolution, gap.owner) == ("SOURCE_ACCESS", "source administrator")
    assert (gap.resolution, gap.owner, gap.explanation) == GAP_DEFINITIONS[gap.kind]
    # The invisible count on the same run is null: could not ask, which is not a gap.
    assert [item for item in listed.gaps if item.kind == "SOURCE_OBJECTS_INVISIBLE"] == []


async def test_a_facet_that_merely_came_back_empty_is_not_a_refusal(
    gap_session: AsyncSession,
) -> None:
    """A SUPPORTED facet with nothing in it means the source has none of them. Counting it
    here would send a source administrator to grant access that would change nothing."""
    org, datasource, _ = await seed_estate(gap_session)
    gap_session.add(_receipt_with_refusal(org, datasource, facet="grants", state="SUPPORTED"))
    await gap_session.commit()

    result = await footprint_gaps(
        gap_session,
        context=human(org, "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=org.id,
        now=NOW,
    )

    (listed,) = result.datasources
    assert [item for item in listed.gaps if item.kind == "SOURCE_READS_REFUSED"] == []


async def test_the_refused_read_gap_says_why_it_lists_no_objects(
    gap_session: AsyncSession,
) -> None:
    """The detail route expands a count into the objects behind it. A refused read has none
    -- nothing came back to name -- so an empty list without a note would read as "none",
    the same false-clean reading the count exists to prevent."""
    org, datasource, _ = await seed_estate(gap_session)

    detail = await footprint_gap_objects(
        gap_session,
        organization_id=org.id,
        datasource_id=datasource.id,
        kind=REFUSED_READS,
    )

    assert detail.objects == []
    assert detail.note is not None
    assert detail.resolution == "SOURCE_ACCESS"
