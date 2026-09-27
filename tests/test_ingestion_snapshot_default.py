"""R11-AUD05 -- an omitted `snapshot_type` must not retire anything.

`Docs/30-contracts/05-metadata-ingestion-envelope.md` §3-4 calls INCREMENTAL the safe default and
FULL the authoritative snapshot that soft-deprecates every active object it does not mention. The
synchronous endpoint used to default the field to FULL, so a producer that left it out -- or a
hand-written curl -- sent a destructive snapshot, and nothing on the server asked for
confirmation. The batch manifest already defaulted to INCREMENTAL; the two entry points disagreed.

These tests pin three things, each at the level where it could regress:

1. the schema: the default, and that ONLY the default moved (the field is still optional and the
   enum is unchanged, so every body that validated before still does);
2. the wire: a POST whose JSON has no `snapshot_type` key, through the real FastAPI application,
   is recorded and applied as INCREMENTAL and retires nothing, while an explicit FULL still does
   -- the assertion that fails if the default is ever put back, or if the router stops honouring
   the field;
3. the one consequence of moving a default that a producer can observe: the payload fingerprint
   covers the field, so it is part of the idempotency contract.

SQLite in memory is sufficient -- no construct here is PostgreSQL-specific -- and follows
`tests/test_envelope_v11.py` and `tests/test_ask_through_context_product.py`.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.envelope_models  # noqa: F401  -- registers the 1.1 tables on the metadata
import aida.models  # noqa: F401  -- registers every 1.0 table on the metadata
from aida.config import Settings, get_settings
from aida.db import Base, get_session
from aida.ingestion import envelope_fingerprint
from aida.main import app
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataColumn,
    MetadataTable,
    Organization,
    Project,
)
from aida.schemas import MetadataIngestionBatchCreate, MetadataIngestionCreate

_EMITTED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC).isoformat()


def _column(name: str, ordinal: int) -> dict[str, Any]:
    return {
        "name": name,
        "ordinal_position": ordinal,
        "physical_type": "bigint",
        "nullable": False,
    }


def _table(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "object_type": "BASE_TABLE",
        "columns": [_column(f"{name}_id", 1), _column("created_by", 2)],
    }


def _body(*table_names: str, key: str, **extra: Any) -> dict[str, Any]:
    """A wire body, as a producer's JSON would be: only what the caller chose to send."""
    return {
        "idempotency_key": key,
        "producer": "bank-metadata-bridge",
        "emitted_at": _EMITTED_AT,
        "catalogs": [
            {
                "name": "bank",
                "schemas": [{"name": "customer", "tables": [_table(n) for n in table_names]}],
            }
        ],
        **extra,
    }


# --- 1. the schema ----------------------------------------------------------


def test_an_omitted_snapshot_type_is_incremental() -> None:
    body = _body("account", key="omitted-0001")
    assert "snapshot_type" not in body

    assert MetadataIngestionCreate.model_validate(body).snapshot_type == "INCREMENTAL"


def test_an_explicit_full_is_kept_as_full() -> None:
    """FULL has not been removed, only made deliberate."""
    envelope = MetadataIngestionCreate.model_validate(
        _body("account", key="explicit-0001", snapshot_type="FULL")
    )

    assert envelope.snapshot_type == "FULL"


def test_the_two_ingestion_entry_points_now_default_alike() -> None:
    synchronous = MetadataIngestionCreate.model_fields["snapshot_type"].default
    batch = MetadataIngestionBatchCreate.model_fields["snapshot_type"].default

    assert synchronous == batch == "INCREMENTAL"


def test_only_the_default_moved_the_field_is_still_optional_and_the_enum_unchanged() -> None:
    """The backward-compatibility half of the change, asserted on the published schema.

    `openapi_diff` treats a field becoming required, or an enum narrowing, as breaking; a
    default is not part of what it compares, which is exactly why this is pinned here instead.
    """
    schema = MetadataIngestionCreate.model_json_schema()
    field = schema["properties"]["snapshot_type"]

    assert "snapshot_type" not in schema["required"]
    assert field["default"] == "INCREMENTAL"
    assert field["enum"] == ["FULL", "INCREMENTAL"]


@pytest.mark.parametrize("value", ["full", "Full", "incremental", "", "SNAPSHOT", None])
def test_a_snapshot_type_that_is_not_one_of_the_two_is_refused_not_defaulted(
    value: object,
) -> None:
    """A typo must be a 422. An explicit null included: `null` says "I chose nothing", and the
    only place that is allowed to mean INCREMENTAL is the key being absent."""
    with pytest.raises(ValidationError):
        MetadataIngestionCreate.model_validate(
            _body("account", key="typo-0001", snapshot_type=value)
        )


# --- 3. the idempotency consequence -----------------------------------------


def test_the_fingerprint_covers_the_field_so_an_omission_is_an_incremental_body() -> None:
    """Why a replay of a key first used under the old default answers 409.

    A body that omitted the field fingerprints exactly as one that said INCREMENTAL, and
    differently from one that said FULL. Before this change the omitted body fingerprinted as
    FULL; a producer that retries such a key across the deployment gets 409 ("a different
    metadata envelope") instead of the original job -- refused, not re-applied as a snapshot it
    never asked for.
    """
    omitted = MetadataIngestionCreate.model_validate(_body("account", key="replay-0001"))
    incremental = MetadataIngestionCreate.model_validate(
        _body("account", key="replay-0001", snapshot_type="INCREMENTAL")
    )
    full = MetadataIngestionCreate.model_validate(
        _body("account", key="replay-0001", snapshot_type="FULL")
    )

    assert envelope_fingerprint(omitted) == envelope_fingerprint(incremental)
    assert envelope_fingerprint(omitted) != envelope_fingerprint(full)


# --- 2. the wire ------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest_asyncio.fixture
async def datasource(session: AsyncSession) -> DataSource:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    lob = LineOfBusiness(
        organization_id=organization.id, name="Retail", code=f"RTL{uuid4().hex[:4]}"
    )
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Deposits",
        code=f"DEP{uuid4().hex[:4]}",
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    source = DataSource(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="Consumer warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://consumer",
        status="ACTIVE",
    )
    session.add(source)
    await session.flush()
    return source


@pytest_asyncio.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """The real application, talking to the test's own session.

    `app` is process-wide, so the overrides are restored as they were found rather than cleared:
    clearing would silently remove whatever another test had installed.
    """
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingest.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


async def _post(
    http: httpx.AsyncClient, datasource: DataSource, body: dict[str, Any]
) -> httpx.Response:
    return await http.post(
        f"/v1/datasources/{datasource.id}/metadata-ingestions",
        json=body,
        headers={
            "X-Principal-Id": "ingest-bridge",
            "X-Roles": "MetadataAdmin",
            "X-Organization-Id": str(datasource.organization_id),
        },
    )


async def _table_statuses(session: AsyncSession, datasource: DataSource) -> dict[str, str]:
    # Column selects, not entities: retirement is a bulk UPDATE, which does not refresh an
    # entity already in the session's identity map, and a stale "ACTIVE" here would make the
    # FULL assertion below pass or fail for the wrong reason.
    rows = await session.execute(
        select(MetadataTable.name, MetadataTable.status).where(
            MetadataTable.datasource_id == datasource.id
        )
    )
    return {name: status for name, status in rows.all()}


async def _column_statuses(session: AsyncSession, datasource: DataSource) -> set[str]:
    rows = await session.scalars(
        select(MetadataColumn.status)
        .join(MetadataTable, MetadataColumn.table_id == MetadataTable.id)
        .where(MetadataTable.datasource_id == datasource.id)
    )
    return set(rows)


async def _run_mode(session: AsyncSession, response: httpx.Response) -> str:
    """The mode of the analysis run a delivery was applied under.

    Read through the run the response names, not by ordering runs: SQLite's `created_at` has
    one-second resolution, and three deliveries inside a second tie.
    """
    run = await session.get(AnalysisRun, UUID(response.json()["analysis_run_id"]))
    assert run is not None
    return run.mode


async def test_a_post_that_omits_snapshot_type_retires_nothing_and_explicit_full_still_does(
    http: httpx.AsyncClient, session: AsyncSession, datasource: DataSource
) -> None:
    # The estate: two tables, delivered by an explicit FULL (the first delivery has nothing to
    # retire, so this also shows FULL is still accepted when spelled out).
    first = await _post(
        http,
        datasource,
        _body("account", "customer", key="estate:0001", snapshot_type="FULL"),
    )
    assert first.status_code == 201, first.text
    assert first.json()["snapshot_type"] == "FULL"
    assert await _table_statuses(session, datasource) == {
        "account": "ACTIVE",
        "customer": "ACTIVE",
    }

    # The same estate re-sent with `customer` left out, and the field left out too. This is the
    # request that used to be an authoritative snapshot and tombstone `customer`.
    omitting = _body("account", key="estate:0002")
    assert "snapshot_type" not in omitting
    second = await _post(http, datasource, omitting)

    assert second.status_code == 201, second.text
    recorded = second.json()
    assert recorded["snapshot_type"] == "INCREMENTAL"
    assert recorded["change_counts"]["deprecated_objects"] == 0
    assert await _table_statuses(session, datasource) == {
        "account": "ACTIVE",
        "customer": "ACTIVE",
    }
    assert await _column_statuses(session, datasource) == {"ACTIVE"}

    # Saying FULL is still what asks for the retirement, and it still gets it -- the objects it
    # omits are soft-deprecated (never deleted), and the one it names stays active.
    third = await _post(
        http, datasource, _body("account", key="estate:0003", snapshot_type="FULL")
    )

    assert third.status_code == 201, third.text
    retired = third.json()
    assert retired["snapshot_type"] == "FULL"
    assert retired["change_counts"]["deprecated_objects"] > 0
    assert await _table_statuses(session, datasource) == {
        "account": "ACTIVE",
        "customer": "DEPRECATED",
    }
    # The run row records what was applied, not what the producer typed.
    assert [await _run_mode(session, r) for r in (first, second, third)] == [
        "FULL",
        "INCREMENTAL",
        "FULL",
    ]


async def test_replaying_an_omitting_body_is_idempotent_and_a_full_replay_is_a_conflict(
    http: httpx.AsyncClient, session: AsyncSession, datasource: DataSource
) -> None:
    """The idempotency contract survives the default moving, and shows its one seam.

    The same omitting body sent twice returns the original job. Sending the SAME key with the
    field now spelled FULL is a different envelope and is refused 409 -- which is what a
    producer replaying a pre-change key would see, and why the refusal is the safe direction.
    """
    omitting = _body("account", key="replay:0001")
    original = await _post(http, datasource, omitting)
    replay = await _post(http, datasource, copy.deepcopy(omitting))
    assert original.status_code == replay.status_code == 201
    assert original.json()["id"] == replay.json()["id"]
    assert replay.json()["snapshot_type"] == "INCREMENTAL"

    upgraded = await _post(http, datasource, {**omitting, "snapshot_type": "FULL"})

    assert upgraded.status_code == 409
    assert "different metadata envelope" in upgraded.json()["detail"]
    # The refused request applied nothing: no second run exists, and the original stays
    # INCREMENTAL.
    assert await _run_mode(session, original) == "INCREMENTAL"
    assert len((await session.scalars(select(AnalysisRun.id))).all()) == 1
