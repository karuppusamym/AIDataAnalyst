"""R11-GQL01: the GraphQL demand limits are settings, and introspection has a policy.

* **Limits moved into `Settings`, behaviour unchanged.** Every `GraphQLLimits` number is a
  bounded `graphql_*` setting whose default is today's value -- default settings build exactly
  `DEFAULT_LIMITS` -- and the endpoint reads them per request: lowering one refuses what the
  default admits, and raising one admits what the default refuses, because strawberry's
  backstop limiters sit at the settings' ceilings rather than at the defaults.
* **Validated.** Each bound is enforced, the ceilings are the backstops' values, and the two
  cross-field rules (a page the node budget can admit; a response ceiling no smaller than the
  request's) refuse an incoherent configuration at startup.
* **Introspection policy.** Off by default; on, it is served only to PlatformAdmin and
  AgentDeveloper, never in production (the setting is refused there, and ignored if forced),
  an admitted schema answer is not counted as returned data, and the schema-level backstop
  honours the same per-request decision.
"""

from __future__ import annotations

from typing import Any

import annotated_types
import httpx
import pytest
from graphql import get_introspection_query
from pydantic import ValidationError
from strawberry.types.graphql import OperationType

from aida.graphql_limits import (
    DEFAULT_LIMITS,
    GRAPHQL_INTROSPECTION_ROLES,
    LIMIT_CEILINGS,
    REFUSAL_CODES,
    GraphQLLimits,
    introspection_decision,
    limits_from_settings,
)
from aida.graphql_reads import open_read_scope
from aida.graphql_schema import metadata_schema
from aida.main import app
from aida.security_types import SecurityContext
from atlas.platform.config import Settings, get_settings
from tests import test_graphql_api as api
from tests.test_graphql_api import Estate, _codes, _gql, _headers

# The catalog estate and the real application, from the endpoint's own suite.
estate = api.estate
http = api.http

#: `GraphQLLimits` field -> the setting that configures it.
_SETTINGS = {
    "max_request_bytes": "graphql_max_request_bytes",
    "max_tokens": "graphql_max_tokens",
    "max_depth": "graphql_max_depth",
    "max_aliases": "graphql_max_aliases",
    "max_page_size": "graphql_max_page_size",
    "max_nodes": "graphql_max_nodes",
    "max_string_argument_length": "graphql_max_string_argument_length",
    "max_selection_visits": "graphql_max_selection_visits",
    "max_response_bytes": "graphql_max_response_bytes",
    "deadline_seconds": "graphql_deadline_seconds",
    "max_scope_datasources": "graphql_max_scope_datasources",
    "max_execution_rows": "graphql_max_execution_rows",
}


def _bounds(setting: str) -> tuple[float, float]:
    """The (floor, ceiling) pydantic enforces on one setting."""
    floor = ceiling = None
    for constraint in Settings.model_fields[setting].metadata:
        if isinstance(constraint, annotated_types.Ge):
            floor = constraint.ge
        elif isinstance(constraint, annotated_types.Le):
            ceiling = constraint.le
    assert floor is not None and ceiling is not None, f"{setting} is not bounded"
    return float(floor), float(ceiling)


def _serve(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> Settings:
    """Serve the endpoint settings with `changes`, validated, for the rest of the test."""
    settings = Settings(_env_file=None, **changes)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    return settings


# --- limits are settings, defaults unchanged ------------------------------------------


def test_default_settings_build_exactly_todays_limits() -> None:
    assert limits_from_settings(Settings(_env_file=None)) == DEFAULT_LIMITS
    assert set(_SETTINGS) | {"allow_introspection", "introspection_refusal"} == set(
        GraphQLLimits.__dataclass_fields__
    ), "a limit was added without a setting"


@pytest.mark.parametrize(("limit", "setting"), sorted(_SETTINGS.items()))
def test_each_setting_is_bounded_and_its_ceiling_is_the_backstops(
    limit: str, setting: str
) -> None:
    floor, ceiling = _bounds(setting)
    default = getattr(DEFAULT_LIMITS, limit)
    assert floor <= default <= ceiling
    assert getattr(LIMIT_CEILINGS, limit) == ceiling
    kind = int if isinstance(default, int) else float
    Settings(_env_file=None, **{setting: kind(ceiling)})
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{setting: kind(ceiling) + 1})
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{setting: kind(floor) - 1})


def _chain(datasource_id: str, fields: int) -> str:
    """A document `fields + 1` levels deep: datasource -> tables -> nodes -> datasource ..."""
    steps = [f'datasource(id: "{datasource_id}")', "tables", "nodes"] + [
        "datasource",
        "tables",
        "nodes",
    ] * fields
    chosen = steps[:fields]
    leaf = {"datasource": "name", "tables": "totalCount", "nodes": "name"}[
        chosen[-1].split("(")[0]
    ]
    return "query Q { " + " { ".join(chosen) + " { " + leaf + " }" * fields + " }"


async def test_the_schema_backstops_sit_at_the_ceilings(estate: Estate) -> None:
    """Behind the endpoint, strawberry's own limiters: set at `LIMIT_CEILINGS`, so a setting
    raised to its ceiling is never refused again by a backstop still holding the default --
    and they are still there, refusing past the ceiling."""
    scope = open_read_scope(
        session=estate.db,
        context=SecurityContext(
            principal_id="direct",
            principal_type="USER",
            organization_id=estate.org.id,
            roles=frozenset({"PlatformAdmin"}),
        ),
        settings=Settings(_env_file=None),
        organization_id=estate.org.id,
    )

    async def backstop_errors(query: str) -> list[str]:
        result = await metadata_schema.execute(
            query,
            context_value=scope,
            operation_name="Q",
            allowed_operation_types=(OperationType.QUERY,),
        )
        return [error.message for error in result.errors or () if error.original_error is None]

    ceiling = LIMIT_CEILINGS.max_aliases
    aliases = " ".join(f"a{index}: __typename" for index in range(ceiling))
    assert await backstop_errors(f"query Q {{ {aliases} }}") == []
    assert await backstop_errors(f"query Q {{ {aliases} extra: __typename }}")

    ds = str(estate.open_ds.id)
    assert await backstop_errors(_chain(ds, LIMIT_CEILINGS.max_depth - 1)) == []
    too_deep = await backstop_errors(_chain(ds, LIMIT_CEILINGS.max_depth + 1))
    assert any("depth" in message for message in too_deep), too_deep


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"graphql_max_page_size": 200, "graphql_max_nodes": 150}, "graphql_max_page_size"),
        (
            {"graphql_max_request_bytes": 200_000, "graphql_max_response_bytes": 100_000},
            "graphql_max_response_bytes",
        ),
        (
            {"environment": "production", "graphql_introspection_enabled": True},
            "introspection is forbidden in production",
        ),
    ],
    ids=["page-above-node-budget", "response-below-request", "introspection-in-production"],
)
def test_an_incoherent_configuration_is_refused_at_startup(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **changes)


# --- the endpoint reads them per request ----------------------------------------------

_DEPTH_5 = (
    "query Q($id: ID!) { datasource(id: $id) { tables(first: 1) { nodes { columns(first: 1) "
    "{ totalCount } } } } }"
)


async def test_a_lowered_setting_refuses_what_the_default_admits(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    variables = {"id": str(estate.open_ds.id)}
    admitted = await _gql(http, _DEPTH_5, _headers(estate.org), variables=variables)
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["extensions"]["cost"]["depth"] == 5

    _serve(monkeypatch, graphql_max_depth=4)
    estate.statements.clear()
    refused = await _gql(http, _DEPTH_5, _headers(estate.org), variables=variables)
    assert refused.status_code == 400
    assert _codes(refused.json()) == [("DEPTH_LIMIT_EXCEEDED", None)]
    assert estate.statements == []


def _aliases(estate: Estate, count: int) -> str:
    ds = str(estate.open_ds.id)
    return "query Q { " + " ".join(
        f'a{index}: datasource(id: "{ds}") {{ name }}' for index in range(count)
    ) + " }"


async def test_a_raised_setting_admits_what_the_default_refuses(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60 aliases: refused at the default of 50, served at 60 -- and not then refused by
    strawberry's alias limiter, which would answer with an unstable error of its own had it
    been left at the default."""
    query = _aliases(estate, 60)
    refused = await _gql(http, query, _headers(estate.org))
    assert _codes(refused.json()) == [("ALIAS_LIMIT_EXCEEDED", None)]

    _serve(monkeypatch, graphql_max_aliases=60)
    served = await _gql(http, query, _headers(estate.org))
    body = served.json()
    assert served.status_code == 200, body
    assert "errors" not in body, body
    assert body["extensions"]["cost"]["aliases"] == 60
    assert body["data"]["a59"]["name"] == estate.open_ds.name


async def test_the_page_ceiling_is_the_setting(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = "query Q { datasources(first: 150) { totalCount } }"
    assert _codes((await _gql(http, query, _headers(estate.org))).json()) == [
        ("PAGE_SIZE_EXCEEDED", None)
    ]
    _serve(monkeypatch, graphql_max_page_size=150)
    body = (await _gql(http, query, _headers(estate.org))).json()
    assert "errors" not in body, body
    assert body["data"]["datasources"]["totalCount"] == 5


# --- introspection policy ---------------------------------------------------------------

_INTROSPECTION = get_introspection_query(descriptions=True)


def test_the_policy_decision_table() -> None:
    off = Settings(_env_file=None)
    on = Settings(_env_file=None, graphql_introspection_enabled=True)
    # A production object built around the validator must still not introspect.
    forced = Settings(_env_file=None).model_copy(
        update={"environment": "production", "graphql_introspection_enabled": True}
    )
    assert introspection_decision(off, {"PlatformAdmin"}) == (False, "INTROSPECTION_DISABLED")
    assert introspection_decision(forced, {"PlatformAdmin"}) == (False, "INTROSPECTION_DISABLED")
    assert introspection_decision(on, {"Viewer"}) == (False, "INTROSPECTION_FORBIDDEN")
    for role in GRAPHQL_INTROSPECTION_ROLES:
        assert introspection_decision(on, {role, "Viewer"})[0] is True
    assert set(GRAPHQL_INTROSPECTION_ROLES) == {"AgentDeveloper", "PlatformAdmin"}
    assert REFUSAL_CODES["INTROSPECTION_FORBIDDEN"] == 403


async def test_introspection_is_off_by_default_even_for_an_administrator(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    estate.statements.clear()
    response = await _gql(
        http, _INTROSPECTION, _headers(estate.org, roles="PlatformAdmin"),
        operation="IntrospectionQuery",
    )
    assert response.status_code == 400
    assert _codes(response.json()) == [("INTROSPECTION_DISABLED", None)]
    assert estate.statements == []


@pytest.mark.parametrize("role", GRAPHQL_INTROSPECTION_ROLES)
async def test_an_enabled_deployment_serves_the_schema_to_its_builders(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The standard introspection query, whole: over 1,000 objects of schema, which is not
    data and is not held to the 500-object budget -- only to the byte ceiling."""
    _serve(monkeypatch, graphql_introspection_enabled=True)
    response = await _gql(
        http, _INTROSPECTION, _headers(estate.org, roles=role), operation="IntrospectionQuery"
    )
    body = response.json()
    assert response.status_code == 200, body
    assert "errors" not in body, body
    names = {entry["name"] for entry in body["data"]["__schema"]["types"]}
    assert {"Query", "Mutation", "ContextProductCoverage", "RoutineParseCoverage"} <= names
    assert body["extensions"]["cost"]["returnedObjects"] == 0


async def test_an_enabled_deployment_refuses_everyone_else_before_any_work(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, graphql_introspection_enabled=True)
    estate.statements.clear()
    response = await _gql(
        http, _INTROSPECTION, _headers(estate.org, roles="Viewer"), operation="IntrospectionQuery"
    )
    assert response.status_code == 403
    assert _codes(response.json()) == [("INTROSPECTION_FORBIDDEN", None)]
    assert estate.statements == []


async def test_data_beside_introspection_is_still_counted(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the introspection fields' own answer is exempt from the object count."""
    _serve(monkeypatch, graphql_introspection_enabled=True)
    query = (
        "query Q { __schema { queryType { name } } "
        "listing: datasources(first: 3) { nodes { id } } }"
    )
    body = (await _gql(http, query, _headers(estate.org, roles="PlatformAdmin"))).json()
    assert "errors" not in body, body
    # The connection object and its three nodes; not `__schema` or `queryType`.
    assert body["extensions"]["cost"]["returnedObjects"] == 4


async def test_the_schema_backstop_honours_the_request_decision(estate: Estate) -> None:
    """`metadata_schema.execute` reached any other way: introspection only where the request's
    own limits allow it, and never for a context that is not a request scope."""
    assert estate.org.id is not None
    context = SecurityContext(
        principal_id="direct",
        principal_type="USER",
        organization_id=estate.org.id,
        roles=frozenset({"PlatformAdmin"}),
    )

    async def run(limits: GraphQLLimits | None) -> Any:
        scope: Any = (
            open_read_scope(
                session=estate.db,
                context=context,
                settings=Settings(_env_file=None),
                organization_id=estate.org.id,
                limits=limits,
            )
            if limits is not None
            else object()
        )
        return await metadata_schema.execute(
            "query Q { __schema { queryType { name } } }",
            context_value=scope,
            operation_name="Q",
            allowed_operation_types=(OperationType.QUERY,),
        )

    refused = await run(DEFAULT_LIMITS)
    assert refused.data is None and refused.errors
    allowed = await run(GraphQLLimits(allow_introspection=True))
    assert allowed.errors is None
    assert allowed.data == {"__schema": {"queryType": {"name": "Query"}}}
    foreign = await run(None)
    assert foreign.data is None and foreign.errors
