"""Seeding shared by the task-agent tests (ADR-0029).

Every task agent runs against the same estate shape -- an organization, a
datasource with one catalog and schema, catalog tables -- and is registered
the same way: an APPROVED `AGENT`-kind AI asset version carrying an
`AgentContract` for the agent's principal. `tests/test_steward_agent.py`
predates this module and seeds inline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.envelope_models  # noqa: F401 -- registers metadata_view_definition
import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.db import Base
from aida.models import (
    AGENT_SAMPLING_RATE_FLOOR,
    AgentContract,
    AiAsset,
    AiAssetVersion,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.security import SecurityContext
from atlas.platform.config import Settings
from tests.support.doubles import security_context


@asynccontextmanager
async def task_agent_session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite, with transactions made to behave like PostgreSQL's.

    The pysqlite driver defers BEGIN until a DML statement, so a SAVEPOINT
    issued before any write becomes the outermost transaction and its RELEASE
    commits -- and a refused run's rollback would have nothing left to undo.
    SQLAlchemy's documented aiosqlite recipe hands BEGIN back to SQLAlchemy.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _explicit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as active:
            yield active
    finally:
        await engine.dispose()


def agent_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def seed_estate(
    session: AsyncSession,
    *,
    organization: Organization | None = None,
    dialect: str = "postgres",
) -> tuple[Organization, DataSource, MetadataSchema]:
    org = organization or Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
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
        name=f"src-{uuid4().hex[:8]}",
        connector_type="postgres",
        dialect=dialect,
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all(([] if organization else [org]) + [lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    return org, datasource, schema


async def seed_table(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    object_type: str = "BASE_TABLE",
) -> MetadataTable:
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
    await session.flush()
    return table


async def register_agent(
    session: AsyncSession,
    org: Organization,
    *,
    principal: str,
    tier: str = "T1",
    status: str = "APPROVED",
    kill_engaged: bool = False,
    supervisor_persona: str = "STEWARD",
) -> AgentContract:
    """What an administrator does once per organization: an AGENT-kind AI
    asset version and the contract the agent acts under."""
    asset = AiAsset(
        organization_id=org.id,
        asset_key=f"agent-{uuid4().hex[:8]}",
        asset_kind="AGENT",
        created_by="platform-admin",
    )
    session.add(asset)
    await session.flush()
    version = AiAssetVersion(
        organization_id=org.id,
        asset_id=asset.id,
        version=1,
        status=status,
        name=f"{principal.removeprefix('agent:').title()} agent",
        description="A task agent under test.",
        intended_use="Working a backlog under review.",
        owner_principal="agent-owners",
        provider_type="INTERNAL",
        risk_tier="LOW",
        context_product_version_ids=[],
        model_route_ids=[],
        policy_control_ids=[],
        evaluation_evidence={},
        runtime_evidence={},
        fingerprint=uuid4().hex,
        created_by="platform-admin",
    )
    session.add(version)
    await session.flush()
    contract = AgentContract(
        organization_id=org.id,
        ai_asset_version_id=version.id,
        agent_principal_id=principal,
        capability_envelope={"tool_slugs": [], "context_product_ids": [], "write_lanes": []},
        autonomy_tier=tier,
        supervisor_persona=supervisor_persona,
        kill_scope="AGENT",
        kill_engaged=kill_engaged,
        sampling_rate=AGENT_SAMPLING_RATE_FLOOR,
        created_by="platform-admin",
    )
    session.add(contract)
    await session.flush()
    return contract


def human(
    org: Organization,
    principal_id: str = "steward-1",
    roles: frozenset[str] = frozenset({"DataSteward"}),
) -> SecurityContext:
    return security_context(organization_id=org.id, principal_id=principal_id, roles=roles)


async def count_rows(session: AsyncSession, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)
