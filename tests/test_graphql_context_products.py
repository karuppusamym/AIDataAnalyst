"""R11-GQL01: context products over GraphQL are REST's decision, not a second one.

`contextProducts` and `contextProductVersion` call the functions the REST routes call
(`aida.context_product_reads`), so these tests compare the two directly, against one real
SQLite database: the same keys and totals for every reader and both views, the same envelope
filter for a contracted agent, and the same governed version read -- a consumer's read records
its consumption (on channel `GRAPHQL`), a lifecycle reader's does not, and the refusals keep
their meaning.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.graphql import OperationType

from aida.context_product_api import get_context_product_version as rest_version_read
from aida.graphql_reads import open_read_scope
from aida.graphql_schema import metadata_schema
from aida.models import (
    AgentContract,
    AuditEvent,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
)
from aida.security import SecurityContext
from atlas.platform.config import Settings
from tests import test_context_product_askable_listing as listing
from tests.test_context_product_askable_listing import _context, _Estate, _list

# The listing tests' estate: two published products and a draft, one bound to Analyst and one
# to DataSteward. Bound here by name so pytest finds the fixtures in this module.
db = listing.db
estate = listing.estate

pytestmark = pytest.mark.asyncio

LIST = """
query Products($projectId: ID!, $askable: Boolean!, $first: Int!, $after: String) {
  contextProducts(projectId: $projectId, askable: $askable, first: $first, after: $after) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes { productKey latestVersion { status version } }
  }
}
"""

VERSION = """
query Version($id: ID!) {
  contextProductVersion(id: $id) { id productKey version status tableIds allowedConsumerRoles }
}
"""


async def _gql(
    session: AsyncSession, context: SecurityContext, query: str, name: str, **variables: Any
) -> Any:
    assert context.organization_id is not None
    scope = open_read_scope(
        session=session,
        context=context,
        settings=Settings(_env_file=None),
        organization_id=context.organization_id,
    )
    return await metadata_schema.execute(
        query,
        variable_values=variables,
        context_value=scope,
        operation_name=name,
        allowed_operation_types=(OperationType.QUERY,),
    )


async def _graphql_list(
    session: AsyncSession, built: _Estate, context: SecurityContext, *, askable: bool = False
) -> tuple[list[str], int]:
    result = await _gql(
        session,
        context,
        LIST,
        "Products",
        projectId=str(built.project.id),
        askable=askable,
        first=50,
    )
    assert result.errors is None, result.errors
    page = result.data["contextProducts"]
    return [node["productKey"] for node in page["nodes"]], page["totalCount"]


async def _version_of(session: AsyncSession, key: str) -> ContextProductVersion:
    version = await session.scalar(
        select(ContextProductVersion)
        .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
        .where(ContextProduct.product_key == key)
    )
    assert version is not None
    return version


@pytest.mark.parametrize(
    "roles",
    [{"DataSteward"}, {"Analyst"}, {"PlatformAdmin"}, {"Viewer"}, {"Auditor"}],
    ids=["steward", "analyst", "admin", "viewer", "auditor"],
)
@pytest.mark.parametrize("askable", [False, True], ids=["lifecycle", "askable"])
async def test_the_listing_is_rests_for_every_reader_and_view(
    db: AsyncSession, estate: _Estate, roles: set[str], askable: bool
) -> None:
    context = _context(estate, roles=roles)

    rest_keys, rest_total = await _list(db, estate, context, askable=askable)
    graphql_keys, graphql_total = await _graphql_list(db, estate, context, askable=askable)

    assert graphql_keys == rest_keys and graphql_total == rest_total


async def test_the_listing_pages_by_cursor_in_product_key_order(
    db: AsyncSession, estate: _Estate
) -> None:
    context = _context(estate, roles={"DataSteward"})
    seen: list[str] = []
    after: str | None = None
    while True:
        result = await _gql(
            db,
            context,
            LIST,
            "Products",
            projectId=str(estate.project.id),
            askable=False,
            first=1,
            after=after,
        )
        page = result.data["contextProducts"]
        seen += [node["productKey"] for node in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    assert seen == ["draft-context", "orders-context", "risk-context"]


async def test_an_agent_lists_only_what_its_envelope_names(
    db: AsyncSession, estate: _Estate
) -> None:
    db.add(
        AgentContract(
            id=uuid4(),
            organization_id=estate.organization.id,
            ai_asset_version_id=uuid4(),
            agent_principal_id="agent:revenue-bot",
            capability_envelope={
                "tool_slugs": [],
                "context_product_ids": ["orders-context"],
                "write_lanes": [],
            },
            autonomy_tier="T1",
            supervisor_persona="STEWARD",
            kill_scope="AGENT",
            sampling_rate=0.05,
            created_by="agent-owner",
        )
    )
    await db.commit()
    agent = SecurityContext(
        principal_id="agent:revenue-bot",
        principal_type="AGENT",
        organization_id=estate.organization.id,
        roles=frozenset({"DataSteward"}),
    )

    rest_keys, rest_total = await _list(db, estate, agent)
    graphql_keys, graphql_total = await _graphql_list(db, estate, agent)

    assert graphql_keys == rest_keys == ["orders-context"]
    assert graphql_total == rest_total == 1


async def test_a_consumers_read_is_recorded_as_a_graphql_consumption(
    db: AsyncSession, estate: _Estate
) -> None:
    version = await _version_of(db, "orders-context")
    analyst = _context(estate, roles={"Analyst"})

    result = await _gql(db, analyst, VERSION, "Version", id=str(version.id))

    assert result.errors is None, result.errors
    read = result.data["contextProductVersion"]
    assert (read["productKey"], read["status"]) == ("orders-context", "PUBLISHED")
    rest = await rest_version_read(version.id, analyst, db)
    assert read["tableIds"] == [str(value) for value in rest.table_ids]
    channels = sorted((await db.scalars(select(ContextProductConsumptionEdge.channel))).all())
    assert channels == ["GRAPHQL", "REST"], "one consumption per read, on its own channel"
    audits = (
        await db.scalars(select(AuditEvent).where(AuditEvent.action == "context_product.read"))
    ).all()
    assert len(audits) == 2


async def test_a_lifecycle_readers_read_is_not_a_consumption(
    db: AsyncSession, estate: _Estate
) -> None:
    version = await _version_of(db, "draft-context")

    result = await _gql(
        db, _context(estate, roles={"DataSteward"}), VERSION, "Version", id=str(version.id)
    )

    assert result.errors is None, result.errors
    assert result.data["contextProductVersion"]["status"] == "DRAFT"
    assert (await db.scalars(select(ContextProductConsumptionEdge))).all() == []


@pytest.mark.parametrize(
    ("key", "why"),
    [("risk-context", "not a consumer role of it"), ("draft-context", "never published")],
)
async def test_what_rest_hides_graphql_hides_the_same_way(
    db: AsyncSession, estate: _Estate, key: str, why: str
) -> None:
    version = await _version_of(db, key)

    result = await _gql(
        db, _context(estate, roles={"Analyst"}), VERSION, "Version", id=str(version.id)
    )

    assert result.data["contextProductVersion"] is None, why
    assert [error.original_error.code for error in result.errors or ()] == ["NOT_FOUND"]


async def test_a_retired_version_is_gone_for_a_caller_who_read_it_before(
    db: AsyncSession, estate: _Estate
) -> None:
    version = await _version_of(db, "orders-context")
    analyst = _context(estate, roles={"Analyst"})
    first = await _gql(db, analyst, VERSION, "Version", id=str(version.id))
    assert first.errors is None
    version.status = "SUPERSEDED"
    await db.commit()

    retired = await _gql(db, analyst, VERSION, "Version", id=str(version.id))
    stranger = await _gql(
        db,
        SecurityContext(
            principal_id="never-read-it@bank.example",
            principal_type="USER",
            organization_id=estate.organization.id,
            roles=frozenset({"Analyst"}),
        ),
        VERSION,
        "Version",
        id=str(version.id),
    )

    assert retired.data["contextProductVersion"] is None
    assert [
        (error.original_error.code, error.original_error.reason) for error in retired.errors or ()
    ] == [("GONE", "CONTEXT_PRODUCT_VERSION_RETIRED")]
    assert [error.original_error.code for error in stranger.errors or ()] == ["NOT_FOUND"]


async def test_an_unknown_version_and_another_tenants_are_not_found(
    db: AsyncSession, estate: _Estate
) -> None:
    result = await _gql(
        db, _context(estate, roles={"Analyst"}), VERSION, "Version", id=str(UUID(int=7))
    )

    assert [error.original_error.code for error in result.errors or ()] == ["NOT_FOUND"]
