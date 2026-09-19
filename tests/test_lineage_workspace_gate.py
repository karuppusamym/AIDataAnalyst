"""R11-D28: every lineage surface asks the datasource's workspace gate.

The unified-lineage routes, the impact export, the domain graph and the MCP lineage tools
checked the caller's roles and tenant only. Every catalog read of the same datasource asks
the workspace gate (`READ_METADATA` on the datasource), so in an organization with an
enforcing workspace a caller refused a datasource's tables could read their names -- and
what depends on what -- off its lineage. These tests run each surface against a real
SQLite estate with such a workspace: a caller who is not its member is refused with the
gate's own reason before anything is built, a member is admitted, and a datasource with no
workspace behaves as before (the default SHADOW posture allows and records).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from aida import unified_lineage_api
from aida.lineage_evidence_export_api import export_unified_lineage_impact
from aida.mcp_server import _handle_native_lineage_tool_call
from aida.models import (
    AccessPolicy,
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    SourceBinding,
    WorkspaceMembership,
)
from aida.security_types import SecurityContext
from aida.unified_lineage_api import (
    get_domain_unified_lineage_graph,
    get_unified_lineage_graph,
    get_unified_lineage_impact,
)
from tests import test_graphql_lineage as lineage
from tests.test_graphql_lineage import CLOSED_TABLE, SETTINGS, Estate, _foreign_key, _table

# The GraphQL lineage estate: an open datasource with seven-node lineage, and a datasource
# bound to an enforcing workspace the default caller is not a member of.
estate = lineage.estate

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMBER = "member@bank.example"


def _caller(estate: Estate, principal_id: str = "analyst@bank.example") -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=estate.org.id,
        roles=frozenset({"Analyst"}),
    )


async def _admit_member(estate: Estate) -> None:
    """A member of the restricted workspace, under an unconditional ALLOW for its role --
    without one, an enforcing workspace default-denies (INV-4) even its members."""
    binding = await estate.db.scalar(
        select(SourceBinding).where(SourceBinding.datasource_id == estate.closed_ds.id)
    )
    assert binding is not None
    estate.db.add(
        WorkspaceMembership(
            organization_id=estate.org.id,
            workspace_id=binding.workspace_id,
            principal_id=MEMBER,
            role="analyst",
            status="ACTIVE",
            granted_by="test",
        )
    )
    estate.db.add(
        AccessPolicy(
            organization_id=estate.org.id,
            code="baseline-allow",
            name="baseline allow",
            effect="ALLOW",
            subject_match={"roles": ["analyst"]},
            action_match=[],
            created_by="test",
        )
    )
    await estate.db.commit()


async def _graph(estate: Estate, context: SecurityContext, datasource_id: UUID) -> Any:
    return await get_unified_lineage_graph(
        datasource_id,
        node_limit=300,
        edge_limit=1_500,
        suggestion_status="APPROVED",
        include_pending_edges=False,
        context=context,
        session=estate.db,
        settings=SETTINGS,
    )


async def _impact(estate: Estate, context: SecurityContext, datasource_id: UUID, node: str) -> Any:
    return await get_unified_lineage_impact(
        datasource_id,
        node,
        depth=5,
        node_limit=200,
        context=context,
        session=estate.db,
        settings=SETTINGS,
    )


async def _export(estate: Estate, context: SecurityContext, datasource_id: UUID, node: str) -> Any:
    return await export_unified_lineage_impact(
        datasource_id,
        node,
        depth=5,
        node_limit=200,
        context=context,
        session=estate.db,
        settings=SETTINGS,
    )


async def _call(surface: str, estate: Estate, context: SecurityContext, closed: bool) -> Any:
    datasource = estate.closed_ds if closed else estate.open_ds
    node = str(estate.closed_table.id if closed else estate.tables["raw_orders"].id)
    if surface == "graph":
        return await _graph(estate, context, datasource.id)
    if surface == "impact":
        return await _impact(estate, context, datasource.id, node)
    return await _export(estate, context, datasource.id, node)


# --- the REST routes and the export ------------------------------------------------------


@pytest.mark.parametrize("surface", ["graph", "impact", "export"])
async def test_a_route_refuses_a_datasource_the_callers_workspace_refuses(
    estate: Estate, surface: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _call(surface, estate, _caller(estate), closed=True)

    assert (refused.value.status_code, refused.value.detail) == (403, "NO_WORKSPACE_MEMBERSHIP")


@pytest.mark.parametrize("surface", ["graph", "impact", "export"])
async def test_a_member_of_that_workspace_reads_it(estate: Estate, surface: str) -> None:
    await _admit_member(estate)

    served = await _call(surface, estate, _caller(estate, MEMBER), closed=True)

    body = served.body.decode() if surface == "export" else served.model_dump_json()
    assert CLOSED_TABLE in body


@pytest.mark.parametrize("surface", ["graph", "impact", "export"])
async def test_a_datasource_with_no_workspace_reads_as_before(estate: Estate, surface: str) -> None:
    served = await _call(surface, estate, _caller(estate), closed=False)

    body = served.body.decode() if surface == "export" else served.model_dump_json()
    assert "raw_orders" in body


async def test_the_refusal_comes_before_the_graph_is_built(
    estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the graph was built for a caller the gate refuses")

    monkeypatch.setattr(unified_lineage_api, "build_unified_lineage_graph_payload", _explode)

    with pytest.raises(HTTPException) as refused:
        await _graph(estate, _caller(estate), estate.closed_ds.id)

    assert refused.value.status_code == 403


# --- the domain graph ---------------------------------------------------------------


async def _domain_with_an_open_neighbour(estate: Estate) -> DataSource:
    """A second datasource in the restricted datasource's domain, bound to no workspace."""
    closed = estate.closed_ds
    neighbour = DataSource(
        id=uuid4(),
        organization_id=closed.organization_id,
        line_of_business_id=closed.line_of_business_id,
        data_domain_id=closed.data_domain_id,
        project_id=closed.project_id,
        name="neighbour",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    estate.db.add(neighbour)
    await estate.db.flush()
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=neighbour.organization_id,
        datasource_id=neighbour.id,
        name="bank",
        fingerprint="fp",
    )
    estate.db.add(catalog)
    await estate.db.flush()
    catalog_schema = MetadataSchema(
        id=uuid4(),
        organization_id=neighbour.organization_id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    estate.db.add(catalog_schema)
    await estate.db.flush()
    parent = await _table(estate.db, neighbour, catalog_schema, "open_parent")
    child = await _table(estate.db, neighbour, catalog_schema, "open_child")
    estate.db.add(_foreign_key(child, parent))
    await estate.db.commit()
    return neighbour


async def _domain_graph(estate: Estate, context: SecurityContext) -> Any:
    return await get_domain_unified_lineage_graph(
        estate.closed_ds.data_domain_id,
        node_limit=600,
        edge_limit=3_000,
        suggestion_status="APPROVED",
        include_pending_edges=False,
        context=context,
        session=estate.db,
        settings=SETTINGS,
    )


async def test_the_domain_graph_withholds_a_datasource_the_gate_refuses_and_counts_it(
    estate: Estate,
) -> None:
    neighbour = await _domain_with_an_open_neighbour(estate)

    graph = await _domain_graph(estate, _caller(estate))

    assert graph.datasource_ids == [neighbour.id]
    assert graph.withheld_datasource_count == 1
    assert {node.label for node in graph.nodes} == {"open_parent", "open_child"}
    assert CLOSED_TABLE not in graph.model_dump_json()
    assert str(estate.closed_ds.id) not in graph.model_dump_json(), "withheld, never named"


async def test_a_member_sees_the_whole_domain(estate: Estate) -> None:
    await _domain_with_an_open_neighbour(estate)
    await _admit_member(estate)

    graph = await _domain_graph(estate, _caller(estate, MEMBER))

    assert graph.withheld_datasource_count == 0
    assert CLOSED_TABLE in {node.label for node in graph.nodes}


# --- the MCP lineage tools ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "arguments"),
    [
        ("get_lineage_graph", {}),
        ("get_lineage_impact", {"node_id": "NODE"}),
        ("resolve_entity", {"query": "zzq_secret"}),
    ],
)
async def test_an_mcp_lineage_tool_refuses_it_exactly_as_a_missing_datasource(
    estate: Estate, slug: str, arguments: dict[str, str]
) -> None:
    def _arguments(datasource_id: UUID) -> dict[str, str]:
        node = str(estate.closed_table.id)
        return {
            "datasource_id": str(datasource_id),
            **{key: node if value == "NODE" else value for key, value in arguments.items()},
        }

    refused = await _handle_native_lineage_tool_call(
        slug, _arguments(estate.closed_ds.id), estate.db, _caller(estate), settings=SETTINGS
    )
    missing = await _handle_native_lineage_tool_call(
        slug, _arguments(uuid4()), estate.db, _caller(estate), settings=SETTINGS
    )
    await _admit_member(estate)
    admitted = await _handle_native_lineage_tool_call(
        slug, _arguments(estate.closed_ds.id), estate.db, _caller(estate, MEMBER), settings=SETTINGS
    )

    assert refused == missing
    assert refused["isError"] is True
    assert CLOSED_TABLE not in repr(refused)
    assert not admitted.get("isError"), admitted
    assert CLOSED_TABLE in repr(admitted)


# --- the published controls -------------------------------------------------------------


@pytest.mark.parametrize(
    "surface",
    [
        "`GET /v1/datasources/{datasource_id}/unified-lineage/graph`",
        "`GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}`",
        "`GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}/export`",
        "`GET /v1/data-domains/{domain_id}/unified-lineage/graph`",
    ],
)
def test_the_surface_matrix_shows_the_workspace_check(surface: str) -> None:
    """The committed matrix is proven current by `tests/test_surface_control_matrix.py`;
    this pins what it must say for each lineage route."""
    matrix = (REPO_ROOT / "Docs" / "50-security" / "surface-control-matrix.md").read_text(
        encoding="utf-8"
    )
    row = next(line for line in matrix.splitlines() if line.startswith(f"| {surface} |"))
    cells = [cell.strip() for cell in re.split(r"\s\|\s", row.strip("| "))]
    # surface, family, handler, roles, tenant, workspace, ...
    assert (cells[4], cells[5]) == ("yes", "yes"), row
