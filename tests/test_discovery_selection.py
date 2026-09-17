"""R11-FP01: a discovery selection narrows what a run takes in and never retires what it leaves out.

The hazard is concrete. A FULL run retires every existing object it did not see
(`_deprecate_missing`, `deprecate_missing_envelope_extensions`), and an object outside a
selection is never seen -- so without scoped reconciliation, excluding a schema would delete
everything earlier scans found in it. The activity tests below drive the real
`discover_datasource` body against in-memory SQLite, with the same run once *without* a
selection to prove the assertion would catch that deletion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.testing import ActivityEnvironment

import aida.task_tracking as task_tracking
import aida.workflows.activities as activities
from aida.config import Settings
from aida.connectors.base import (
    Connector,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredGrant,
    DiscoveredRoutine,
    DiscoveredSchema,
    DiscoveredTable,
    TableProfileSnapshot,
)
from aida.db import Base
from aida.discovery_selection import (
    DiscoverySelection,
    apply_selection,
    kind_capabilities,
    selection_for,
)
from aida.envelope_models import MetadataRoutine
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.security import SecurityContext
from atlas.modules.connectivity.router import (
    get_discovery_selection,
    preview_discovery_selection,
    put_discovery_selection,
)

# --- the selection itself ------------------------------------------------------------------


def _columns() -> tuple[DiscoveredColumn, ...]:
    return (
        DiscoveredColumn(name="id", ordinal_position=1, physical_type="bigint", nullable=False),
    )


def _table(name: str, object_type: str = "BASE_TABLE") -> DiscoveredTable:
    return DiscoveredTable(name=name, object_type=object_type, columns=_columns())


def _routine(name: str, routine_type: str = "PROCEDURE") -> DiscoveredRoutine:
    return DiscoveredRoutine(name=name, routine_type=routine_type)


def test_an_empty_selection_is_unrestricted_and_has_no_fingerprint() -> None:
    selection = DiscoverySelection()
    catalogs = (DiscoveredCatalog(name="bank", schemas=(DiscoveredSchema("s", (_table("t"),)),)),)

    assert selection.restricted is False
    assert selection.fingerprint() is None
    assert apply_selection(catalogs, selection).catalogs is catalogs


def test_the_fingerprint_ignores_order_and_case() -> None:
    first = DiscoverySelection(object_kinds=["VIEW", "TABLE"], exclude_schemas=["Scratch", "tmp"])
    second = DiscoverySelection(object_kinds=["TABLE", "VIEW"], exclude_schemas=["TMP", "scratch"])

    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != DiscoverySelection(object_kinds=["TABLE"]).fingerprint()


def test_matching_ignores_identifier_case_and_excludes_win() -> None:
    """Oracle and Snowflake fold identifiers to upper case, PostgreSQL to lower."""
    selection = DiscoverySelection(
        include_schemas=["sales*"],
        include_objects=["sales.fact_*"],
        exclude_objects=["*.FACT_TMP*"],
    )

    assert selection.object_in_scope("SALES", "FACT_ORDERS", "TABLE")
    assert not selection.object_in_scope("SALES", "FACT_TMP_1", "TABLE")
    assert not selection.object_in_scope("SALES", "DIM_CUSTOMER", "TABLE")
    assert not selection.object_in_scope("FINANCE", "FACT_ORDERS", "TABLE")


def test_applying_a_selection_drops_and_counts_by_kind() -> None:
    sales = DiscoveredSchema(
        name="sales",
        tables=(
            _table("orders"),
            _table("orders_v", "VIEW"),
            _table("orders_mv", "MATERIALIZED VIEW"),
            _table("tmp_orders"),
        ),
        routines=(_routine("refresh"), _routine("net", "FUNCTION"), _routine("pkg", "PACKAGE")),
        grants=(
            DiscoveredGrant("analyst", "ROLE", "SELECT", "VIEW", "orders_v", "sales"),
            DiscoveredGrant("analyst", "ROLE", "SELECT", "TABLE", "orders", "sales"),
            DiscoveredGrant("analyst", "ROLE", "USAGE", "SCHEMA", "sales", "sales"),
        ),
    )
    scratch = DiscoveredSchema(name="scratch", tables=(_table("sandbox"),))
    selection = DiscoverySelection(
        object_kinds=["TABLE", "PROCEDURE", "FUNCTION"],
        exclude_schemas=["scratch"],
        exclude_objects=["sales.tmp_*"],
    )

    outcome = apply_selection((DiscoveredCatalog("bank", (sales, scratch)),), selection)

    (catalog,) = outcome.catalogs
    (kept,) = catalog.schemas
    assert [table.name for table in kept.tables] == ["orders"]
    assert [routine.name for routine in kept.routines] == ["refresh", "net"]
    assert [(grant.object_type, grant.object_name) for grant in kept.grants] == [
        ("TABLE", "orders"),
        ("SCHEMA", "sales"),
    ]
    assert outcome.excluded == {
        "VIEW": 1,
        "MATERIALIZED_VIEW": 1,
        "TABLE": 2,
        "PACKAGE": 1,
        "SCHEMA": 1,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"object_kinds": ["SYNONYM"]},
        {"exclude_schemas": ["x" * 201]},
        {"include_objects": [f"s.t{index}" for index in range(101)]},
        {"include_schemas": ["bad\x00name"]},
        {"unknown_field": True},
    ],
)
def test_a_selection_is_bounded_and_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DiscoverySelection.model_validate(payload)


def test_capabilities_distinguish_unsupported_from_not_applicable() -> None:
    by_kind = {
        row.kind: row
        for row in kind_capabilities("sqlserver", {"views": True, "routines": True})
    }
    assert by_kind["MATERIALIZED_VIEW"].inventory == "NOT_APPLICABLE"
    assert by_kind["PROCEDURE"].definition == "SUPPORTED"

    databricks = {row.kind: row for row in kind_capabilities("databricks", {"views": False})}
    assert databricks["VIEW"].definition == "UNSUPPORTED"
    assert databricks["FUNCTION"].inventory == "UNSUPPORTED"
    assert databricks["MATERIALIZED_VIEW"].inventory == "UNSUPPORTED"


# --- the discovery activity ----------------------------------------------------------------


class _Connector(Connector):
    connector_type = "postgres"
    dialect = "postgres"

    def __init__(self, catalogs: tuple[DiscoveredCatalog, ...]) -> None:
        self._catalogs = catalogs

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(views=True, routines=True)

    async def test_connection(self) -> None:
        return None

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        return self._catalogs

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        yield self._catalogs

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


_SOURCE = (
    DiscoveredCatalog(
        name="bank",
        schemas=(
            DiscoveredSchema(
                name="retail",
                tables=(_table("orders"), _table("orders_view", "VIEW")),
                routines=(_routine("refresh"),),
            ),
            DiscoveredSchema(name="scratch", tables=(_table("new_sandbox"),)),
        ),
    ),
)
_SELECTION = {"object_kinds": ["TABLE", "PROCEDURE"], "exclude_schemas": ["scratch"]}


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


async def _seed(session: AsyncSession, selection: dict[str, object] | None) -> dict[str, UUID]:
    """Earlier scans found: retail.gone_table (source dropped it), retail.old_view (a kind the
    selection leaves out), scratch.sandbox with a column (a schema it leaves out), and a
    routine in each schema."""
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
        discovery_selection=selection,
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    retail = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="retail", fingerprint="fp"
    )
    scratch = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="scratch", fingerprint="fp"
    )
    session.add_all([retail, scratch])
    await session.flush()
    ids: dict[str, UUID] = {"datasource": datasource.id, "organization": org.id}
    for key, schema, name, object_type in (
        ("gone_table", retail, "gone_table", "BASE_TABLE"),
        ("old_view", retail, "old_view", "VIEW"),
        ("sandbox", scratch, "sandbox", "BASE_TABLE"),
    ):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            object_type=object_type,
            status="ACTIVE",
            fingerprint="fp",
        )
        session.add(table)
        ids[key] = table.id
    await session.flush()
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=ids["sandbox"],
        name="id",
        ordinal_position=1,
        physical_type="bigint",
        nullable=False,
        fingerprint="fp",
    )
    session.add(column)
    ids["sandbox_column"] = column.id
    for key, schema in (("proc_gone", retail), ("proc_sandbox", scratch)):
        routine = MetadataRoutine(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=key,
            signature="()",
            routine_type="PROCEDURE",
            body_sql_redacted="CREATE PROCEDURE p() LANGUAGE plpgsql AS $$ BEGIN NULL; END; $$",
            fingerprint="fp",
        )
        session.add(routine)
        ids[key] = routine.id
    ids["scratch_schema"] = scratch.id
    await session.commit()
    return ids


async def _run_full_discovery(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, ids: dict[str, UUID]
) -> tuple[dict[str, object], AnalysisRun]:
    run = AnalysisRun(
        id=uuid4(),
        organization_id=ids["organization"],
        datasource_id=ids["datasource"],
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.commit()
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(activities, "SecretResolver", _StubSecretResolver)
    monkeypatch.setattr(
        activities.connector_registry, "create", lambda connector_type, dsn: _Connector(_SOURCE)
    )
    result = await ActivityEnvironment().run(activities.discover_datasource, str(run.id))
    refreshed = await session.get(AnalysisRun, run.id)
    assert refreshed is not None
    return result, refreshed


async def _status(session: AsyncSession, model: type, object_id: UUID) -> str:
    row = await session.get(model, object_id)
    assert row is not None
    return row.status


@pytest.mark.asyncio
async def test_a_full_run_never_retires_what_the_selection_leaves_out(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _seed(session, _SELECTION)

    result, run = await _run_full_discovery(session, monkeypatch, ids)

    assert result["status"] == "COMPLETED"
    names = set((await session.scalars(select(MetadataTable.name))).all())
    assert "orders" in names
    assert {"orders_view", "new_sandbox"}.isdisjoint(names), "an excluded object was taken in"

    # Out of scope: never looked for, so still ACTIVE.
    assert await _status(session, MetadataTable, ids["old_view"]) == "ACTIVE"
    assert await _status(session, MetadataTable, ids["sandbox"]) == "ACTIVE"
    assert await _status(session, MetadataColumn, ids["sandbox_column"]) == "ACTIVE"
    assert await _status(session, MetadataSchema, ids["scratch_schema"]) == "ACTIVE"
    assert await _status(session, MetadataRoutine, ids["proc_sandbox"]) == "ACTIVE"
    # In scope and gone from the source: retired exactly as before.
    assert await _status(session, MetadataTable, ids["gone_table"]) == "DEPRECATED"
    assert await _status(session, MetadataRoutine, ids["proc_gone"]) == "DEPRECATED"

    # The run's scope receipt: the fingerprint it applied, and what the source returned that
    # it left out (the view, the scratch schema and the table inside it).
    assert run.discovery_selection_fingerprint == DiscoverySelection.model_validate(
        _SELECTION
    ).fingerprint()
    assert run.excluded_objects == 3
    assert result["excluded_objects"] == 3


@pytest.mark.asyncio
async def test_without_a_selection_the_same_run_retires_everything_unseen(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: proves the test above would catch scoped reconciliation going missing."""
    ids = await _seed(session, None)

    _, run = await _run_full_discovery(session, monkeypatch, ids)

    assert await _status(session, MetadataTable, ids["old_view"]) == "DEPRECATED"
    assert await _status(session, MetadataTable, ids["sandbox"]) == "DEPRECATED"
    assert await _status(session, MetadataRoutine, ids["proc_sandbox"]) == "DEPRECATED"
    assert run.discovery_selection_fingerprint is None
    assert run.excluded_objects == 0


# --- the routes ----------------------------------------------------------------------------


def _context(organization_id: UUID, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles or {"DataAdmin"}),
    )


@pytest.mark.asyncio
async def test_a_selection_is_stored_read_back_and_removed_by_an_empty_body(
    session: AsyncSession,
) -> None:
    ids = await _seed(session, None)
    context = _context(ids["organization"])
    body = DiscoverySelection.model_validate(_SELECTION)

    stored = await put_discovery_selection(ids["datasource"], body, context, session)

    assert stored.restricted and stored.fingerprint == body.fingerprint()
    read = await get_discovery_selection(ids["datasource"], context, session)
    assert read.selection == body
    datasource = await session.get(DataSource, ids["datasource"])
    assert datasource is not None and selection_for(datasource) == body

    cleared = await put_discovery_selection(
        ids["datasource"], DiscoverySelection(), context, session
    )
    assert cleared.restricted is False and cleared.fingerprint is None
    datasource = await session.get(DataSource, ids["datasource"])
    assert datasource is not None and datasource.discovery_selection is None


@pytest.mark.asyncio
async def test_the_preview_counts_the_last_scan_and_names_patterns_that_match_nothing(
    session: AsyncSession,
) -> None:
    ids = await _seed(session, None)
    selection = DiscoverySelection(
        object_kinds=["TABLE", "PROCEDURE"],
        exclude_schemas=["scratch"],
        include_objects=["retail.*", "retial.*"],
    )

    preview = await preview_discovery_selection(
        ids["datasource"], selection, _context(ids["organization"], "Viewer"), session
    )

    counts = {row.kind: (row.in_scope, row.excluded) for row in preview.kinds}
    assert counts["TABLE"] == (1, 1)  # gone_table in; sandbox out with its schema
    assert counts["VIEW"] == (0, 1)
    assert counts["PROCEDURE"] == (1, 1)
    assert (preview.schemas.in_scope, preview.schemas.excluded) == (1, 1)
    assert preview.unmatched_include_patterns == ["retial.*"]
    assert preview.basis == "LAST_SCAN" and preview.truncated is False
    assert preview.fingerprint == selection.fingerprint()
    # Nothing was stored by previewing.
    datasource = await session.get(DataSource, ids["datasource"])
    assert datasource is not None and datasource.discovery_selection is None


@pytest.mark.asyncio
async def test_another_organization_cannot_read_or_set_the_selection(
    session: AsyncSession,
) -> None:
    ids = await _seed(session, _SELECTION)
    outsider = _context(uuid4())

    with pytest.raises(HTTPException):
        await get_discovery_selection(ids["datasource"], outsider, session)
    with pytest.raises(HTTPException):
        await put_discovery_selection(ids["datasource"], DiscoverySelection(), outsider, session)
    datasource = await session.get(DataSource, ids["datasource"])
    assert datasource is not None and datasource.discovery_selection == _SELECTION


# ---------------------------------------------------------------------------
# Review 2026-09-16 §5: a kind this selection leaves out reads NOT_SELECTED.
#
# The design target asks the source configuration UI to distinguish
# NOT_APPLICABLE, UNSUPPORTED and NOT_SELECTED
# (`Docs/10-architecture/20-database-footprint-and-agent-context.md` §5.2). Only
# the first two existed: a kind a selection excluded still reported the support
# the adapter would have had, which is true about the adapter and wrong about
# the source.
# ---------------------------------------------------------------------------


def test_a_kind_the_selection_excludes_reads_not_selected() -> None:
    from aida.discovery_selection import DiscoverySelection, kind_capabilities

    selection = DiscoverySelection(object_kinds=["TABLE", "VIEW"])
    by_kind = {
        read.kind: read
        for read in kind_capabilities(
            "postgres", {"views": True, "routines": True}, selection
        )
    }
    assert by_kind["TABLE"].inventory == "SUPPORTED"
    assert by_kind["VIEW"].definition == "SUPPORTED"
    assert by_kind["PROCEDURE"].inventory == "NOT_SELECTED"
    assert by_kind["PROCEDURE"].definition == "NOT_SELECTED"
    assert by_kind["FUNCTION"].inventory == "NOT_SELECTED"
    assert by_kind["MATERIALIZED_VIEW"].inventory == "NOT_SELECTED"


def test_not_selected_never_overwrites_a_more_specific_answer() -> None:
    """An engine without packages does not gain one by being excluded, and
    excluding an axis the adapter cannot read is not what kept it out. Both keep
    the more specific answer."""
    from aida.discovery_selection import DiscoverySelection, kind_capabilities

    selection = DiscoverySelection(object_kinds=["TABLE"])
    postgres = {
        read.kind: read
        for read in kind_capabilities(
            "postgres", {"views": True, "routines": True}, selection
        )
    }
    assert postgres["PACKAGE"].inventory == "NOT_APPLICABLE"

    databricks = {
        read.kind: read
        for read in kind_capabilities(
            "databricks", {"views": False, "routines": False}, selection
        )
    }
    assert databricks["PROCEDURE"].inventory == "UNSUPPORTED"
    assert databricks["VIEW"].definition == "UNSUPPORTED"
    # ... while a kind the adapter *can* read is honestly NOT_SELECTED.
    assert databricks["VIEW"].inventory == "NOT_SELECTED"


def test_an_unrestricted_selection_changes_nothing() -> None:
    """A source that never set a selection, or set one that names no kinds, must
    read exactly as it did before."""
    from aida.discovery_selection import DiscoverySelection, kind_capabilities

    capabilities = {"views": True, "routines": True}
    baseline = kind_capabilities("postgres", capabilities)
    assert kind_capabilities("postgres", capabilities, None) == baseline
    assert (
        kind_capabilities("postgres", capabilities, DiscoverySelection()) == baseline
    )
    assert (
        kind_capabilities(
            "postgres", capabilities, DiscoverySelection(include_schemas=["sales"])
        )
        == baseline
    ), "a schema scope excludes no kind"
