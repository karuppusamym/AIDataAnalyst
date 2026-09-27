"""R11-FP02, live on SQL Server: a refused catalog view costs one facet, judged by error number.

`tests/test_facet_refusal_adapters_live.py` proved the whole path on PostgreSQL, whose
driver reports a SQLSTATE. SQL Server's does not: `pytds` carries the server's error
*number* (the TDS ERROR token's `sys.messages.message_id`) and no SQLSTATE at all, so
until the classifier read that number a real SQL Server refusal classified as
UNAVAILABLE, `read_facet` re-raised it, and one `DENY` failed the whole scan.

This file is the live evidence for the other side of that: a least-privilege login, a
real `DENY SELECT` on one catalog view, the real `discover_datasource` activity driving
the real `SqlServerConnector` against the local sample container -- and the run
**completing**, with `grants` recorded PERMISSION_DENIED from `pytds`' own error number
229, every other facet captured, and the grant an earlier run captured still ACTIVE
rather than retired.

`sys.database_permissions` is the view denied because exactly one facet reads it
(`sqlserver._GRANT_SQL`), so the assertion "only `grants` was refused" is sharp.

**Everything created here is rolled back.** The private database, its user, its object
grant and the `DENY` all live inside the journey fixture's own database and are dropped
with it; the login is dropped by the same fixture's teardown. The last test asks the
server afterwards that no database or login this process created survives.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import unquote, urlsplit

import pytds
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.testing import ActivityEnvironment

import aida.workflows.activities as activities
from aida.capability_states import is_permission_refusal
from aida.connectors.sqlserver import _GRANT_SQL, SqlServerConnector
from aida.envelope_models import MetadataSourceGrant
from aida.models import AnalysisRun, DataSource, MetadataConstraint, MetadataTable
from tests.test_discover_datasource_streaming import (
    _patch_activity_plumbing,
    _seed_datasource_with_legacy_table,
)
from tests.test_discover_datasource_streaming import session as streaming_session  # noqa: F401
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    MSSQL_CONTAINER,
    JourneySource,
    _sqlserver,
)

SCHEMA = "footprint_context_sample"
#: The catalog view one facet -- and only one -- reads.
DENIED_VIEW = "sys.database_permissions"


def _login(source: JourneySource) -> str:
    login = urlsplit(source.dsn).username
    assert login is not None
    return unquote(login)


@pytest_asyncio.fixture
async def mssql_source(_sqlserver: JourneySource) -> JourneySource:  # noqa: F811
    """The journey's private SQL Server database, plus one object-level grant.

    The journey login already holds SELECT on the sample schema, VIEW DEFINITION and
    SHOWPLAN. The grants facet reads object-level permissions only (`dp.class = 1`), so
    one object grant is added to give it something real to capture -- which is what
    lets the refused run prove the captured grant is *not* retired.
    """
    await _sqlserver.execute(f"GRANT SELECT ON {SCHEMA}.orders TO [{_login(_sqlserver)}];")
    return _sqlserver


async def _deny(source: JourneySource) -> None:
    await source.execute(f"DENY SELECT ON {DENIED_VIEW} TO [{_login(source)}];")


async def _discover(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, datasource: DataSource, dsn: str
) -> tuple[dict[str, Any], AnalysisRun]:
    """One real `discover_datasource` run over the real connector at `dsn`.

    Stubbed, and neither is under test: the platform session factory (the run lands in
    an in-memory database) and the credential resolver (a login created seconds ago is
    in no vault). The connector, its queries, the source, the DENY, the classification,
    the receipt and the reconciliation are all real.
    """
    run = AnalysisRun(
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
        activities.connector_registry,
        "create",
        lambda connector_type, resolved_dsn: SqlServerConnector(dsn),
    )
    result = await ActivityEnvironment().run(activities.discover_datasource, str(run.id))
    persisted = await session.get(AnalysisRun, run.id)
    assert persisted is not None
    return result, persisted


async def _sqlserver_datasource(session: AsyncSession) -> DataSource:
    datasource, _legacy = await _seed_datasource_with_legacy_table(session)
    datasource.connector_type = "sqlserver"
    datasource.dialect = "tsql"
    await session.commit()
    return datasource


def _read_grants_as(dsn: str) -> BaseException | None:
    """Run the connector's own grant statement as the login, returning its failure."""
    parts = urlsplit(dsn)
    connection = pytds.connect(
        server=parts.hostname,
        port=parts.port,
        database=parts.path.lstrip("/"),
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        login_timeout=15,
        as_dict=True,
        autocommit=True,
    )
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(_GRANT_SQL)
            cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- the failure is what this returns
            return exc
        finally:
            cursor.close()
    finally:
        connection.close()
    return None


async def test_a_real_deny_answers_error_229_which_the_classifier_reads_as_a_refusal(
    mssql_source: JourneySource,
) -> None:
    """The driver fact the whole change rests on, taken from the live server.

    `pytds` reports the refusal's number as a field; there is no SQLSTATE to read. The
    classifier judges it from that number, never from the message -- which, as the
    assertion on it shows, names the object and the database.
    """
    assert await asyncio.to_thread(_read_grants_as, mssql_source.dsn) is None

    await _deny(mssql_source)
    refused = await asyncio.to_thread(_read_grants_as, mssql_source.dsn)

    assert isinstance(refused, pytds.tds_base.DatabaseError)
    assert refused.number == 229
    assert not hasattr(refused, "sqlstate")
    assert is_permission_refusal(refused) is True
    # The message is exactly the kind of text INV-6 keeps out of storage: it names the
    # object and the database. Nothing below it reads it.
    assert "database_permissions" in str(refused)


async def test_a_real_denied_catalog_view_leaves_a_complete_run_that_names_it(
    mssql_source: JourneySource,
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline, live: capture, DENY, rescan -- and the rescan completes.

    The first run is the control (grants SUPPORTED and captured). The second runs after
    one real `DENY` and must complete with `grants` PERMISSION_DENIED and every other
    facet as it was. The grant the first run stored must still be ACTIVE afterwards:
    a refused read is a read that did not happen, and a FULL run may not retire what it
    was not allowed to look at (`workflows.activities.refused_facet_existing`).
    """
    datasource = await _sqlserver_datasource(streaming_session)

    first, first_run = await _discover(
        streaming_session, monkeypatch, datasource, mssql_source.dsn
    )
    assert first["status"] == "COMPLETED"
    assert first_run.discovery_receipt is not None
    control = first_run.discovery_receipt["facets"]
    assert control["grants"]["state"] == "SUPPORTED"
    assert not [f for f, o in control.items() if o.get("state") == "PERMISSION_DENIED"]
    captured = (
        await streaming_session.scalars(
            select(MetadataSourceGrant).where(MetadataSourceGrant.object_name == "orders")
        )
    ).all()
    assert captured, "the control run must capture the object grant the fixture added"
    captured_ids = {grant.id for grant in captured}

    await _deny(mssql_source)
    second, second_run = await _discover(
        streaming_session, monkeypatch, datasource, mssql_source.dsn
    )

    assert second["status"] == "COMPLETED"
    receipt = second_run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"]["state"] == "COMPLETE"
    grants = receipt["facets"]["grants"]
    assert grants["support"] == "SUPPORTED"
    assert grants["state"] == "PERMISSION_DENIED"
    assert grants["reason"] == "SOURCE_DENIED_READ"
    refused = [f for f, o in receipt["facets"].items() if o.get("state") == "PERMISSION_DENIED"]
    assert refused == ["grants"]

    # The rest of the scan is intact: the roster, the constraints read from
    # INFORMATION_SCHEMA and the view definitions read from sys.sql_modules all landed.
    names = set((await streaming_session.scalars(select(MetadataTable.name))).all())
    assert {"customers", "orders", "customer_revenue"} <= names
    assert (await streaming_session.scalars(select(MetadataConstraint.id))).all()
    assert receipt["facets"]["view_definitions"]["state"] == "SUPPORTED"
    assert receipt["facets"]["view_definitions"]["captured"] >= 1

    # Retirement safety: the grant the control run captured was not looked at this
    # time, so it is not missing -- it stays ACTIVE.
    await streaming_session.commit()
    kept = (
        await streaming_session.scalars(
            select(MetadataSourceGrant).where(MetadataSourceGrant.id.in_(captured_ids))
        )
    ).all()
    assert {grant.status for grant in kept} == {"ACTIVE"}

    # INV-6 against the real driver's real message: it names the view and the database.
    body = str(receipt)
    assert "database_permissions" not in body
    assert "permission was denied" not in body.lower()


def _survivors() -> str:
    """Databases and logins this process's journey fixture could have left behind."""
    pid = os.getpid()
    # The only value interpolated is this process's own pid.
    return (
        "SET NOCOUNT ON; "  # noqa: S608
        f"SELECT COUNT(*) FROM sys.databases WHERE name = N'aida_footprint_journey_{pid}'; "
        f"SELECT COUNT(*) FROM sys.server_principals WHERE name = N'aida_journey_reader_{pid}';"
    )


async def test_nothing_this_file_created_survives_on_the_server() -> None:
    """Runs after the fixtures above have torn down: the private database -- and with it
    the user, the object grant and the DENY -- and the login are all gone."""
    import subprocess

    command = [
        "docker",
        "exec",
        "-i",
        MSSQL_CONTAINER,
        "sh",
        "-c",
        'export SQLCMDPASSWORD="${MSSQL_SA_PASSWORD:-$SA_PASSWORD}"; '
        "if [ -x /opt/mssql-tools18/bin/sqlcmd ]; then s=/opt/mssql-tools18/bin/sqlcmd; "
        "else s=/opt/mssql-tools/bin/sqlcmd; fi; "
        'exec "$s" -S localhost -U sa -d master -C -b -h -1 -W',
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            input=f"{_survivors()}\nGO\n",
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"no reachable SQL Server sample container: {type(exc).__name__}")
    if result.returncode:
        pytest.skip("no reachable SQL Server sample container")
    counts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert counts == ["0", "0"], counts
