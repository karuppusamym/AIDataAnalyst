"""R11-OKF02: the Catalog's object knowledge read, through the GraphQL door.

`objectOkfKnowledge(tableId)` answers what `GET /v1/metadata/tables/{id}/okf-knowledge`
answers -- the same store reads, so the same gates -- and records its reads on GraphQL's own
channels, once per request however many aliases ask.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from aida.config import Settings
from aida.models import OutboxEvent
from aida.okf_export_api import read_object_okf_knowledge
from aida.okf_read_model import OKF_ROLES
from tests.test_graphql_okf import (  # noqa: F401  # noqa: F401 -- fixtures
    World,
    _data,
    _gql,
    _headers,
    http,
    world,
)
from tests.test_okf_export import _context

QUERY = """
query Obj($id: ID!) {
  objectOkfKnowledge(tableId: $id) {
    tableId
    items { productKey productVersion publication { publicationId } document { path sha256 text } }
    source { state reason datasourceId document { path sha256 text } }
  }
}
"""


async def _channels(state: World) -> list[str]:
    rows = (await state.db.scalars(select(OutboxEvent))).all()
    return [str(r.payload.get("channel")) for r in rows if "channel" in (r.payload or {})]


@pytest.mark.asyncio
async def test_a_product_bundle_answers_as_the_rest_route_does(
    http: httpx.AsyncClient,  # noqa: F811
    world: World,  # noqa: F811
) -> None:
    table = world.estate["tables"]["warehouse.orders"]
    rest = await read_object_okf_knowledge(
        table.id, _context(world.org.id), world.db, Settings(_env_file=None)
    )
    answer = _data(await _gql(http, QUERY, _headers(world.org), "Obj", id=str(table.id)))[
        "objectOkfKnowledge"
    ]
    assert [i["productKey"] for i in answer["items"]] == [i.product_key for i in rest.items]
    assert answer["items"][0]["document"]["sha256"] == rest.items[0].document.sha256
    assert answer["items"][0]["document"]["text"] == rest.items[0].document.content
    assert answer["source"] is None
    assert "GRAPHQL_OKF_OBJECT" in await _channels(world)


@pytest.mark.asyncio
async def test_an_object_no_product_holds_reads_its_own_source_bundle(
    http: httpx.AsyncClient,  # noqa: F811
    world: World,  # noqa: F811
) -> None:
    # The world's product holds no table of the far (people) datasource.
    table = world.estate["tables"]["people.salaries"]
    body = _data(await _gql(http, QUERY, _headers(world.org), "Obj", id=str(table.id)))
    answer = body["objectOkfKnowledge"]
    assert answer["items"] == []
    source = answer["source"]
    assert source["state"] in {"DOCUMENT", "NOT_IN_BUNDLE"}
    assert source["reason"] is None and source["datasourceId"] == str(table.datasource_id)
    assert "GRAPHQL_OKF_SOURCE_OBJECT" in await _channels(world)


@pytest.mark.asyncio
async def test_a_role_without_okf_access_and_an_unknown_object_are_refused(
    http: httpx.AsyncClient,  # noqa: F811
    world: World,  # noqa: F811
) -> None:
    table = world.estate["tables"]["warehouse.orders"]
    outsider_role = "Viewer"
    assert outsider_role not in OKF_ROLES
    refused = (
        await _gql(http, QUERY, _headers(world.org, outsider_role), "Obj", id=str(table.id))
    ).json()
    assert refused["data"]["objectOkfKnowledge"] is None and refused["errors"]
    unknown = (
        await _gql(
            http,
            QUERY,
            _headers(world.org),
            "Obj",
            id="00000000-0000-0000-0000-000000000000",
        )
    ).json()
    assert unknown["data"]["objectOkfKnowledge"] is None
    assert unknown["errors"][0]["extensions"]["code"] == "NOT_FOUND"
    foreign = (await _gql(http, QUERY, _headers(world.other_org), "Obj", id=str(table.id))).json()
    assert foreign["data"]["objectOkfKnowledge"] is None and foreign["errors"]


@pytest.mark.asyncio
async def test_two_aliases_read_and_record_once(
    http: httpx.AsyncClient,  # noqa: F811
    world: World,  # noqa: F811
) -> None:
    table = world.estate["tables"]["warehouse.orders"]
    twice = """
    query Twice($id: ID!) {
      a: objectOkfKnowledge(tableId: $id) { tableId }
      b: objectOkfKnowledge(tableId: $id) { items { productKey } }
    }
    """
    _data(await _gql(http, twice, _headers(world.org), "Twice", id=str(table.id)))
    assert (await _channels(world)).count("GRAPHQL_OKF_OBJECT") == 1
