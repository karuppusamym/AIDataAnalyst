"""R11-FP02, live: a real discovery run completes with one facet the source refused.

`tests/test_facet_refusal_live.py` proves the *mechanism* against a real refusal -- it
calls `read_facet` around `PostgresConnector`'s view-definition statement by hand.
`tests/test_facet_refusal_adapters.py` proves the *adoption* per engine, with fakes.
This file is the one that proves the two together on a real source: the real
`discover_datasource` activity, driving the real `PostgresConnector` against real
PostgreSQL, with one grant genuinely revoked -- and the run **completing**, its receipt
naming `view_definitions` PERMISSION_DENIED while every other facet still captures.

That is the assertion the feature exists for. Before the adapters adopted `read_facet`
this same refusal raised out of `discover_streaming` and the run was marked FAILED with
nothing discovered: one missing grant costing a whole estate.

The refusal is made the way a real one happens, not simulated: a least-privilege login
that may inventory the source, with `EXECUTE` on the one `pg_get_viewdef` overload
`_VIEW_DEFINITION_SQL` calls revoked from PUBLIC. Everything created here is rolled
back -- the grant is restored and the role dropped in the fixture's teardown, and the
database is the journey fixture's own private one, dropped with it.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.testing import ActivityEnvironment

import aida.workflows.activities as activities
from aida.connectors.postgres import PostgresConnector
from aida.envelope_models import (
    MetadataObjectDescription,
    MetadataSourceGrant,
    MetadataViewDefinition,
)
from aida.models import AnalysisRun, MetadataConstraint, MetadataTable
from tests.test_discover_datasource_streaming import (
    _patch_activity_plumbing,
    _seed_datasource_with_legacy_table,
)
from tests.test_discover_datasource_streaming import session as streaming_session  # noqa: F401
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    JourneySource,
    _postgres,
)

SCHEMA = "footprint_context_sample"
#: The one overload `_VIEW_DEFINITION_SQL` calls: `pg_get_viewdef(c.oid, true)`.
VIEWDEF = "pg_catalog.pg_get_viewdef(oid, boolean)"


@pytest_asyncio.fixture
async def least_privilege(_postgres: JourneySource) -> AsyncIterator[tuple[JourneySource, str]]:  # noqa: F811
    """The journey's PostgreSQL source plus a login that may read it but not everything.

    The login is granted the sample schema, so it inventories the source and reads the
    catalog relations every role may read. `EXECUTE` on `pg_get_viewdef` stays granted
    here -- a test revokes it when it wants the refusal, so the control case and the
    refused case share one fixture and differ by one statement.
    """
    source = _postgres
    url = make_url(source.dsn)
    role = f"aida_facet_adoption_{os.getpid()}"
    password = secrets.token_hex(16)
    await source.execute(
        f"DROP ROLE IF EXISTS {role}; "
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}'; "
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}; "
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role};"
    )
    dsn = url.set(drivername="postgresql", username=role, password=password).render_as_string(
        hide_password=False
    )
    try:
        yield source, dsn
    finally:
        await source.execute(
            f"GRANT EXECUTE ON FUNCTION {VIEWDEF} TO PUBLIC; "
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {role}; "
            f"REVOKE ALL ON SCHEMA {SCHEMA} FROM {role}; "
            f"DROP ROLE IF EXISTS {role};"
        )


async def _run_real_discovery(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, dsn: str
) -> tuple[dict[str, Any], AnalysisRun]:
    """The real `discover_datasource` activity over the real connector at `dsn`.

    Only two things are stubbed, and neither is the thing under test: the platform-side
    session factory (the run and its metadata land in an in-memory database rather than
    the deployment's) and the credential resolver, because the DSN of a role created
    seconds ago is not in any vault. The connector, its queries, the source, the
    refusal, the receipt and the reconciliation are all real.
    """
    datasource, _ = await _seed_datasource_with_legacy_table(session)
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
        lambda connector_type, resolved_dsn: PostgresConnector(dsn),
    )
    result = await ActivityEnvironment().run(activities.discover_datasource, str(run.id))
    persisted = await session.get(AnalysisRun, run.id)
    assert persisted is not None
    return result, persisted


async def test_a_real_refused_facet_leaves_a_complete_run_that_names_it(
    least_privilege: tuple[JourneySource, str],
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline, live. One real revoke, and the run finishes.

    Every assertion here is about a real read of a real catalog: the tables came from
    `information_schema`, the constraints from `pg_constraint`, the grants from
    `information_schema.role_table_grants` (this login's own SELECT on the sample
    schema, which is why that facet is genuinely non-empty), and `view_definitions` is
    PERMISSION_DENIED because PostgreSQL answered `42501` when this login called
    `pg_get_viewdef`.
    """
    source, dsn = least_privilege
    await source.execute(f"REVOKE EXECUTE ON FUNCTION {VIEWDEF} FROM PUBLIC")

    result, run = await _run_real_discovery(streaming_session, monkeypatch, dsn)

    assert result["status"] == "COMPLETED"
    receipt = run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"]["state"] == "COMPLETE"
    view_definitions = receipt["facets"]["view_definitions"]
    assert view_definitions["support"] == "SUPPORTED"
    assert view_definitions["state"] == "PERMISSION_DENIED"
    assert view_definitions["reason"] == "SOURCE_DENIED_READ"
    # Not one definition arrived, and the view whose definition did not arrive is
    # counted -- so the facet is empty *and* says why, which is the pairing that keeps
    # a refusal from reading as "this source has no views".
    assert view_definitions["captured"] == 0
    assert view_definitions["withheld"] >= 1

    # ... and the rest of the scan is intact: no other facet was refused, the roster
    # came back, and every other facet's rows are in the catalog.
    refused = [
        facet
        for facet, outcome in receipt["facets"].items()
        if outcome.get("state") == "PERMISSION_DENIED"
    ]
    assert refused == ["view_definitions"]
    assert receipt["kinds"]["TABLE"]["discovered"] > 0
    names = set((await streaming_session.scalars(select(MetadataTable.name))).all())
    assert {"customers", "orders", "customer_revenue"} <= names
    for model in (MetadataConstraint, MetadataSourceGrant, MetadataObjectDescription):
        rows = (await streaming_session.scalars(select(model.id))).all()
        assert rows, model.__name__
    # The refused facet stored nothing at all: there were no rows to store, which is
    # exactly why the receipt entry above is the only thing that can tell a reader the
    # difference between a refusal and a source with no view bodies.
    assert not (await streaming_session.scalars(select(MetadataViewDefinition.id))).all()

    # INV-6, against a real driver's real message: PostgreSQL's error for this refusal
    # names the function it would not run. The receipt carries a state and a code.
    body = str(receipt)
    assert "pg_get_viewdef" not in body
    assert "permission denied" not in body.lower()


async def test_the_same_run_with_the_grant_in_place_captures_the_facet(
    least_privilege: tuple[JourneySource, str],
    streaming_session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control, and it is the one that makes the test above mean something.

    Same login, same activity, same source, one statement fewer: with `EXECUTE` still
    granted the facet reads SUPPORTED and the view's definition is actually stored. So
    the PERMISSION_DENIED above is the revoke, not this harness, and the adoption did
    not turn a working facet into a permanently absent one.
    """
    _source, dsn = least_privilege

    result, run = await _run_real_discovery(streaming_session, monkeypatch, dsn)

    assert result["status"] == "COMPLETED"
    assert run.discovery_receipt is not None
    facets = run.discovery_receipt["facets"]
    assert facets["view_definitions"]["state"] == "SUPPORTED"
    assert facets["view_definitions"]["captured"] >= 1
    assert not [
        facet for facet, outcome in facets.items() if outcome.get("state") == "PERMISSION_DENIED"
    ]
    stored = (
        await streaming_session.scalars(
            select(MetadataViewDefinition.definition_sql_redacted)
        )
    ).all()
    assert any(definition and "SELECT" in definition.upper() for definition in stored)
