"""R11-GQL01 / R11-OKF02: stored OKF bundles over GraphQL (design sections 13 and 14).

`contextProductOkfBundle` and `datasourceOkfBundle` are a third door onto the *stored* publication
REST and MCP already read -- through `okf_store.read_published_bundle` and
`read_published_source_bundle`, never a renderer of their own. What these tests prove is what
makes that an interface and not a second bundle reader:

* **Parity.** For a version and for a datasource, GraphQL's inspect, documents, one document,
  publication history and findings equal REST's -- the same digests, counts, publication ids,
  file index (order, page boundaries, totals), bytes and history -- and a pinned publication is
  read as REST reads it.
* **The store's gates, not a copy.** The same refusals REST gives, with the same reason code, per
  role, tenant, envelope, consumer role, unknown and pinned-away publication -- and, for a
  datasource, the workspace's `READ_METADATA` refusal answered as the gate's own reason before
  anything is looked up, never an empty bundle, for the current read and a pinned one alike.
* **What a list carries.** Path, kind, digest, size and citation; never text. A document's text
  is one field, one path at a time, bounded as REST bounds it.
* **Priced and recorded.** Every list is a connection the admission prices before a statement
  runs; a read is recorded once per request, however many aliases ask, on GraphQL's own channels.
* **Value-free (INV-6).** No planted body, view definition, column default or source comment
  reaches any GraphQL answer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from graphql import build_schema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 - registers every mapped table on Base.metadata
from aida.graphql_limits import DEFAULT_LIMITS, admit_document
from aida.graphql_schema import OkfDocumentKind, metadata_schema
from aida.main import app
from aida.models import (
    AccessPolicy,
    AgentContract,
    AuditEvent,
    ContextProductConsumptionEdge,
    Organization,
    OutboxEvent,
    SourceBinding,
    WorkspaceMembership,
)
from aida.okf_export import DOCUMENT_KINDS, document_kind, export_okf_bundle
from aida.okf_read_model import OKF_ROLES
from aida.okf_store_models import OkfBundlePublication
from aida.workspace_service import approve_binding, create_workspace, request_binding
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.app_surface import reaches_call, references_name
from tests.test_graphql_api import _camel, _route_roles, _same_value
from tests.test_okf_context import _bank
from tests.test_okf_export import (
    _SENTINELS,
    _estate,
    _product,
    _snapshot,
)
from tests.test_okf_source_bundles import (
    HIDDEN_SCHEMA,
    HIDDEN_TABLE,
    _hidden_schema,
    _redefine_view,
)

# --- the world -------------------------------------------------------------------------


@dataclass
class World:
    db: AsyncSession
    estate: dict[str, Any]
    org: Organization
    version_id: UUID
    datasource_id: UUID
    other_org: Organization


@pytest_asyncio.fixture
async def world() -> AsyncIterator[World]:
    # StaticPool: the gate's durable shadow-record path opens a second session, and every
    # connection to an unpooled in-memory SQLite gets a database of its own.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        db.info["maker"] = maker
        estate = await _estate(db)
        _product_row, version = await _product(db, estate, include_far_source=False)
        other = Organization(id=uuid4(), name="Other bank", slug=f"other-{uuid4().hex[:8]}")
        db.add(other)
        await db.commit()
        yield World(
            db=db,
            estate=estate,
            org=estate["organization"],
            version_id=version.id,
            datasource_id=estate["datasources"]["warehouse"][0].id,
            other_org=other,
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def http(world: World) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield world.db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://okf.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@dataclass(frozen=True)
class Target:
    """One of the two stored bundles, and the REST route that answers for it."""

    name: str
    field: str
    id_argument: str
    id: UUID
    rest: str


def _targets(world: World) -> dict[str, Target]:
    return {
        "product": Target(
            "product",
            "contextProductOkfBundle",
            "versionId",
            world.version_id,
            f"/v1/context-product-versions/{world.version_id}/okf-bundle",
        ),
        "source": Target(
            "source",
            "datasourceOkfBundle",
            "datasourceId",
            world.datasource_id,
            f"/v1/datasources/{world.datasource_id}/okf-bundle",
        ),
    }


@pytest.fixture(params=["product", "source"])
def target(request: pytest.FixtureRequest, world: World) -> Target:
    return _targets(world)[str(request.param)]


def _headers(
    org: Organization,
    roles: str = "DataSteward",
    *,
    principal: str = "steward",
    principal_type: str = "USER",
) -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": principal_type,
        "X-Roles": roles,
        "X-Organization-Id": str(org.id),
    }


async def _gql(
    client: httpx.AsyncClient,
    query: str,
    headers: dict[str, str],
    operation: str,
    **variables: Any,
) -> httpx.Response:
    return await client.post(
        "/graphql",
        json={"query": query, "operationName": operation, "variables": variables},
        headers=headers,
    )


def _data(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert "errors" not in body, body
    data: dict[str, Any] = body["data"]
    return data


def _same_moment(rest_value: str, graphql_value: str) -> bool:
    """Two ISO-8601 instants that name the same moment. SQLite hands back naive datetimes and
    REST renders them with a `Z`; read both as UTC, as PostgreSQL's aware values already are."""

    def instant(text: str) -> datetime:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    return instant(rest_value) == instant(graphql_value)


def _codes(body: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [
        (error["extensions"]["code"], error["extensions"].get("reason"))
        for error in body.get("errors", [])
    ]


_PUBLICATION = (
    "publicationId sequence trigger capturedAt isCurrent bundleContentDigest "
    "contentSnapshotDigest documentCount renderedCount carriedCount valid addedCount "
    "changedCount removedCount changedSubjects markedSubjects fullRender"
)
_BUNDLE = (
    "scope contextProductVersionId productKey productVersion datasourceId okfVersion "
    "specRevision specConformance profile contentSnapshotDigest bundleContentDigest "
    "scopeDigest documentCount valid findingCount validatedAt "
    "counts { sources schemas tables views routines packages concepts tools documents } "
    f"publication {{ {_PUBLICATION} }}"
)
#: The kinds that are about a scope rather than one subject, so name no subject.
_SCOPE_KINDS = frozenset(
    {"BUNDLE_INDEX", "SOURCE_INDEX", "SCHEMA_INDEX", "LOG", "CONCEPT_INDEX", "TOOL_INDEX"}
)
_DOCUMENT = (
    "publicationId publicationSequence path kind citation sha256 bytes renderedInSequence "
    "subjectKey"
)


def _inspect(target: Target, extra: str = "") -> str:
    return (
        f"query Inspect($id: ID!, $pub: ID) {{ bundle: {target.field}"
        f"({target.id_argument}: $id, publicationId: $pub) {{ {_BUNDLE} {extra} }} }}"
    )


def _documents(target: Target) -> str:
    return (
        f"query Docs($id: ID!, $pub: ID, $first: Int!, $after: String) {{ bundle: {target.field}"
        f"({target.id_argument}: $id, publicationId: $pub) {{ "
        "documents(first: $first, after: $after) { totalCount "
        f"pageInfo {{ hasNextPage endCursor }} nodes {{ {_DOCUMENT} }} }} }} }}"
    )


def _document(target: Target) -> str:
    return (
        f"query Doc($id: ID!, $pub: ID, $path: String!) {{ bundle: {target.field}"
        f"({target.id_argument}: $id, publicationId: $pub) {{ document(path: $path) {{ "
        f"{_DOCUMENT} text }} }} }}"
    )


def _publications(target: Target) -> str:
    return (
        f"query History($id: ID!, $pub: ID, $first: Int!, $after: String) {{ bundle: "
        f"{target.field}({target.id_argument}: $id, publicationId: $pub) {{ "
        "publications(first: $first, after: $after) { totalCount "
        f"pageInfo {{ hasNextPage endCursor }} nodes {{ {_PUBLICATION} }} }} }} }}"
    )


def _findings(target: Target) -> str:
    return (
        f"query Findings($id: ID!, $first: Int!, $after: String) {{ bundle: {target.field}"
        f"({target.id_argument}: $id) {{ valid findingCount findings(first: $first, "
        "after: $after) { totalCount pageInfo { hasNextPage endCursor } "
        "nodes { text code detail } } } }"
    )


async def _rest(
    client: httpx.AsyncClient,
    target: Target,
    headers: dict[str, str],
    suffix: str = "",
    **params: Any,
) -> httpx.Response:
    sent = {name: value for name, value in params.items() if value is not None}
    return await client.get(f"{target.rest}{suffix}", headers=headers, params=sent or None)


async def _count(db: AsyncSession, model: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def _second_publication(world: World) -> None:
    """A redefined view: the next read of either bundle rebuilds it as publication 2."""
    await _redefine_view(world.db, world.estate, "SELECT order_id FROM sales.orders /* v2 */")
    await world.db.commit()


# --- wiring ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/context-product-versions/{version_id}/okf-bundle",
        "/v1/context-product-versions/{version_id}/okf-bundle/document",
        "/v1/context-product-versions/{version_id}/okf-bundle/publications",
        "/v1/datasources/{datasource_id}/okf-bundle",
        "/v1/datasources/{datasource_id}/okf-bundle/document",
        "/v1/datasources/{datasource_id}/okf-bundle/publications",
    ],
)
def test_the_okf_fields_require_the_roles_the_okf_routes_declare(path: str) -> None:
    """Read back from the closures FastAPI wired into the live routes: a route that narrows or
    widens its roles fails here until GraphQL follows."""
    assert _route_roles("GET", path) == tuple(sorted(OKF_ROLES))


_OKF_RESOLVERS = [
    "Query.context_product_okf_bundle",
    "Query.datasource_okf_bundle",
    "OkfBundle.documents",
    "OkfBundle.document",
    "OkfBundle.publications",
    "OkfBundle.findings",
]
_STORE_READS = frozenset({"read_published_bundle", "read_published_source_bundle"})
_GATE_CALLS = frozenset({"gate", "authorize_enforced", "authorize"})
_RENDERING = frozenset(
    {
        "freeze_snapshot",
        "freeze_source_snapshot",
        "export_okf_bundle",
        "export_okf_bundle_incremental",
        "_load_source",
    }
)
_SOURCE_EXECUTION_CALLS = frozenset(
    {"QueryExecutionGateway", "open_execution_session", "execute_read_query", "estimate_read_query"}
)


@pytest.mark.parametrize("resolver", _OKF_RESOLVERS)
def test_every_okf_resolver_reaches_the_one_store_and_the_gate(resolver: str) -> None:
    """Structural, in the manner of the REST doors' one-door tests: each resolver reaches the
    store's read -- and through it the admission decision -- and no resolver, nor anything a
    resolver calls in `graphql_okf`, freezes, resolves scope or renders on its own."""
    assert reaches_call("aida.graphql_schema", resolver, _STORE_READS), resolver
    assert reaches_call("aida.graphql_schema", resolver, _GATE_CALLS), resolver
    assert not reaches_call("aida.graphql_schema", resolver, _SOURCE_EXECUTION_CALLS), resolver


def test_each_root_field_reaches_its_own_scopes_store_read() -> None:
    assert reaches_call(
        "aida.graphql_schema", "Query.context_product_okf_bundle", {"read_published_bundle"}
    )
    assert reaches_call(
        "aida.graphql_schema", "Query.datasource_okf_bundle", {"read_published_source_bundle"}
    )


@pytest.mark.parametrize(
    "function",
    [
        "get_okf_bundle",
        "_read_bundle",
        "list_okf_documents",
        "read_okf_document",
        "_read_document",
        "list_okf_publications",
        "_read_history",
        "list_okf_findings",
    ],
)
def test_graphql_okf_renders_nothing_itself(function: str) -> None:
    assert not references_name("aida.graphql_okf", function, _RENDERING), function


def test_the_surface_matrix_derives_each_okf_fields_controls() -> None:
    """The matrix row of each resolver is derived from its call graph: the roles are the role
    set the read asks (`OKF_ROLES`, not a label), the tenant boundary and the workspace gate are
    reached through the store, and the field writes -- deliberately: a stored bundle is
    published on first read, and every read leaves an audit event."""
    from scripts.generate_surface_control_matrix import _graphql_rows

    rows = {row.surface: row for row in _graphql_rows()}
    for resolver in _OKF_RESOLVERS:
        owner, method = resolver.split(".")
        row = rows[f"`GRAPHQL {owner}.{_camel(method)}`"]
        assert row.roles == ", ".join(sorted(OKF_ROLES)), resolver
        assert row.tenant_check == "yes", resolver
        assert row.workspace_check == "yes", resolver
        assert (row.side_effects, row.writes_audit) == ("writes", "yes"), resolver


def test_a_document_kind_is_a_published_enum_value() -> None:
    assert [member.name for member in OkfDocumentKind] == list(DOCUMENT_KINDS)
    assert [member.value for member in OkfDocumentKind] == list(DOCUMENT_KINDS)


def test_a_list_node_has_no_text_field() -> None:
    """A document's text is one field on one type, reached one path at a time. No type a list
    returns carries it, so no list query can be made to."""
    schema = build_schema(metadata_schema.as_str())
    summary = schema.type_map["OkfDocumentSummary"]
    assert "text" not in summary.fields  # type: ignore[union-attr]
    assert "text" in schema.type_map["OkfDocument"].fields  # type: ignore[union-attr]
    bundle_fields = schema.type_map["OkfBundle"].fields  # type: ignore[union-attr]
    assert {"documents", "document", "publications", "findings"} <= set(bundle_fields)
    # Nothing beneath the bundle returns a document's text but `document`.
    returning_text = {
        f"{name}.{field}"
        for name, type_ in schema.type_map.items()
        if name.startswith("Okf") and hasattr(type_, "fields")
        for field in type_.fields  # type: ignore[union-attr]
        if field == "text" and name != "OkfFinding"
    }
    assert returning_text == {"OkfDocument.text"}


# --- document kinds ---------------------------------------------------------------------------


def _kind_counts(paths: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        counts[document_kind(path)] = counts.get(document_kind(path), 0) + 1
    return counts


@pytest.mark.parametrize("build", [_snapshot, _bank], ids=["hand-built", "wide-bank"])
def test_every_path_the_renderer_produces_has_a_kind_that_agrees_with_the_manifests_counts(
    build: Any,
) -> None:
    """The classifier is the renderer's layout read back, so it is held to the renderer: no
    path a real bundle holds is `OTHER`, and each kind's count is what the manifest itself
    counts -- tables, views, routines, packages, concepts, tools, sources and schemas -- across
    both fixtures, which between them hold every kind a bundle can hold (a wide object's column
    sets among them)."""
    bundle = export_okf_bundle(build())
    paths = [document.path for document in bundle.documents]
    counted = _kind_counts(paths)
    assert "OTHER" not in counted, [p for p in paths if document_kind(p) == "OTHER"]
    counts = dict(bundle.manifest["counts"])
    assert counted.get("TABLE", 0) == counts["tables"]
    assert counted.get("VIEW", 0) == counts["views"]
    assert counted.get("ROUTINE", 0) == counts["routines"]
    assert counted.get("PACKAGE", 0) == counts["packages"]
    assert counted.get("CONCEPT", 0) == counts["concepts"]
    assert counted.get("TOOL", 0) == counts["tools"]
    assert counted.get("SOURCE_INDEX", 0) == counts["sources"]
    assert counted.get("SCHEMA_INDEX", 0) == counts["schemas"]
    assert counted.get("BUNDLE_INDEX", 0) == 1
    # A refresh log exists only in a *stored* bundle (its history is the store's); the
    # database-backed tests below hold that kind to the same standard.
    assert "LOG" not in counted
    assert sum(counted.values()) == counts["documents"]


def test_the_two_fixtures_between_them_exercise_every_kind_a_renderer_produces() -> None:
    seen: set[str] = set()
    for build in (_snapshot, _bank):
        seen.update(
            document_kind(document.path) for document in export_okf_bundle(build()).documents
        )
    # LOG is a stored bundle's alone; OTHER is what no renderer produces.
    assert seen == set(DOCUMENT_KINDS) - {"OTHER", "LOG"}


def test_a_path_the_renderer_does_not_produce_is_other() -> None:
    for path in ("notes.md", "sources/source-1/schemas/schema-2/tables/table-3.md/extra", ""):
        assert document_kind(path) == "OTHER"


# --- parity with REST ---------------------------------------------------------------------------


async def test_inspect_is_the_rest_manifest(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    headers = _headers(world.org)
    rest = (await _rest(http, target, headers)).json()
    read = _data(await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id)))[
        "bundle"
    ]

    for key in (
        "okf_version",
        "spec_revision",
        "spec_conformance",
        "profile",
        "content_snapshot_digest",
        "bundle_content_digest",
        "scope_digest",
        "document_count",
        "valid",
    ):
        assert _same_value(rest[key], read[_camel(key)]), key
    assert read["findingCount"] == len(rest["findings"])
    assert _same_moment(rest["validated_at"], read["validatedAt"])
    assert read["counts"] == {
        _camel(name): value for name, value in rest["manifest"]["counts"].items()
    }
    publication, rest_publication = read["publication"], rest["publication"]
    for key in (
        "sequence",
        "trigger",
        "is_current",
        "bundle_content_digest",
        "content_snapshot_digest",
        "document_count",
        "rendered_count",
        "carried_count",
        "valid",
    ):
        assert _same_value(rest_publication[key], publication[_camel(key)]), key
    assert _same_moment(rest_publication["captured_at"], publication["capturedAt"])
    assert publication["publicationId"] == rest_publication["publication_id"]
    changes = rest_publication["changes"]
    assert (
        publication["addedCount"],
        publication["changedCount"],
        publication["removedCount"],
        publication["changedSubjects"],
        publication["markedSubjects"],
        publication["fullRender"],
    ) == (
        len(changes["added"]),
        len(changes["changed"]),
        len(changes["removed"]),
        changes["changed_subjects"],
        changes["marked_subjects"],
        changes["full_render"],
    )
    if target.name == "product":
        assert read["scope"] == "CONTEXT_PRODUCT_VERSION"
        assert read["contextProductVersionId"] == str(world.version_id)
        assert (read["productKey"], read["productVersion"]) == ("revenue_context", 2)
        assert read["datasourceId"] is None
    else:
        assert read["scope"] == "DATASOURCE"
        assert read["datasourceId"] == str(world.datasource_id)
        assert (read["contextProductVersionId"], read["productKey"]) == (None, None)
    # The manifest's own text is not among the fields: a summary, not the manifest.
    assert "manifest" not in read and "files" not in read


async def test_documents_are_the_manifests_file_index_in_its_own_order_and_pages(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    headers = _headers(world.org)
    rest = (await _rest(http, target, headers)).json()
    expected = [(item["path"], item["sha256"], item["bytes"]) for item in rest["files"]]
    assert len(expected) >= 8

    walked: list[dict[str, Any]] = []
    after: str | None = None
    totals: list[int | None] = []
    pages = 0
    while True:
        page = _data(
            await _gql(
                http, _documents(target), headers, "Docs", id=str(target.id), first=3, after=after
            )
        )["bundle"]["documents"]
        walked.extend(page["nodes"])
        totals.append(page["totalCount"])
        pages += 1
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    assert [(node["path"], node["sha256"], node["bytes"]) for node in walked] == expected
    assert totals[0] == len(expected) and set(totals[1:]) == {None}
    assert pages == -(-len(expected) // 3)

    publication_id = rest["publication"]["publication_id"]
    for node in walked:
        assert node["kind"] != "OTHER", node["path"]
        assert node["publicationId"] == publication_id
        assert node["publicationSequence"] == rest["publication"]["sequence"]
        assert node["citation"] == f"okf:{publication_id}:{node['path']}@{node['sha256']}"
        assert node["kind"] == document_kind(node["path"])
        # Subject documents name their subject; indexes and logs are about a scope.
        assert (node["subjectKey"] is None) == (node["kind"] in _SCOPE_KINDS), node["path"]
    kinds = {node["kind"] for node in walked}
    assert {
        "BUNDLE_INDEX",
        "SOURCE_INDEX",
        "SCHEMA_INDEX",
        "TABLE",
        "VIEW",
        "ROUTINE",
        "LOG",
    } <= kinds


async def test_one_document_is_the_rest_document_and_its_bytes(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    headers = _headers(world.org)
    files = (await _rest(http, target, headers)).json()["files"]
    for path in (item["path"] for item in files if document_kind(item["path"]) != "LOG"):
        rest = (await _rest(http, target, headers, "/document", path=path)).json()
        read = _data(
            await _gql(http, _document(target), headers, "Doc", id=str(target.id), path=path)
        )["bundle"]["document"]
        assert read["text"] == rest["content"], path
        assert read["sha256"] == rest["sha256"] == hashlib.sha256(read["text"].encode()).hexdigest()
        assert read["bytes"] == rest["bytes"] == len(read["text"].encode())
        assert (read["renderedInSequence"], read["subjectKey"]) == (
            rest["rendered_in_sequence"],
            rest["subject_key"],
        )
        assert read["publicationId"] == rest["publication_id"]
        assert read["publicationSequence"] == rest["publication_sequence"]
        assert read["kind"] == document_kind(path)


async def test_a_document_path_is_bounded_and_looked_up_only_in_this_publication(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    headers = _headers(world.org)
    files = (await _rest(http, target, headers)).json()["files"]
    path = next(item["path"] for item in files if document_kind(item["path"]) == "TABLE")

    async def read(candidate: str) -> dict[str, Any]:
        response = await _gql(
            http, _document(target), headers, "Doc", id=str(target.id), path=candidate
        )
        body: dict[str, Any] = response.json()
        return body

    # A leading slash is ignored, exactly as the REST route ignores it.
    slashed = await read("/" + path)
    assert slashed["data"]["bundle"]["document"]["path"] == path

    missing = await read("sources/nothing/here.md")
    assert missing["data"]["bundle"]["document"] is None
    assert _codes(missing) == [("NOT_FOUND", "OKF_DOCUMENT_NOT_FOUND")]
    assert (
        await _rest(http, target, headers, "/document", path="sources/nothing/here.md")
    ).status_code == 404

    for bad in ("", "x" * 513):
        # REST answers 422 to 1..512 characters; the endpoint's own argument bound (512) refuses
        # the long one first, before any resolver runs.
        refused = await read(bad)
        data = refused.get("data")
        assert data is None or data["bundle"]["document"] is None
        assert refused.get("errors"), bad
        assert (
            await _rest(http, target, headers, "/document", path=bad)
        ).status_code == 422


async def test_publication_history_is_the_rest_history_a_page_at_a_time(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    headers = _headers(world.org)
    first = (await _rest(http, target, headers)).json()["publication"]
    await _second_publication(world)
    second = (await _rest(http, target, headers)).json()["publication"]
    assert second["sequence"] == first["sequence"] + 1
    rest = (await _rest(http, target, headers, "/publications")).json()["items"]
    assert [item["sequence"] for item in rest] == [2, 1]

    walked: list[dict[str, Any]] = []
    after: str | None = None
    totals: list[int | None] = []
    while True:
        page = _data(
            await _gql(
                http,
                _publications(target),
                headers,
                "History",
                id=str(target.id),
                first=1,
                after=after,
            )
        )["bundle"]["publications"]
        walked.extend(page["nodes"])
        totals.append(page["totalCount"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    assert [node["publicationId"] for node in walked] == [
        item["publication_id"] for item in rest
    ]
    assert [(node["sequence"], node["isCurrent"]) for node in walked] == [(2, True), (1, False)]
    assert totals == [2, None]
    for node, item in zip(walked, rest, strict=True):
        assert (node["trigger"], node["renderedCount"], node["carriedCount"]) == (
            item["trigger"],
            item["rendered_count"],
            item["carried_count"],
        )
        assert node["changedCount"] == len(item["changes"]["changed"])


async def test_a_pinned_publication_is_read_as_rest_reads_it(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    """Naming `publicationId` reads that retained publication of the caller's own lineage: its
    manifest, its documents and its bytes -- not the newer one -- and a publication that is not
    in the lineage (another bundle's, or none) is not found."""
    headers = _headers(world.org)
    first = (await _rest(http, target, headers)).json()
    first_id = first["publication"]["publication_id"]
    await _second_publication(world)
    current = (await _rest(http, target, headers)).json()
    assert current["publication"]["publication_id"] != first_id

    pinned = _data(
        await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id), pub=first_id)
    )["bundle"]
    assert pinned["publication"]["publicationId"] == first_id
    assert pinned["publication"]["isCurrent"] is False
    assert pinned["bundleContentDigest"] == first["bundle_content_digest"]
    assert pinned["bundleContentDigest"] != current["bundle_content_digest"]

    # Its documents are the first publication's, and one document read returns its bytes.
    walked = _data(
        await _gql(
            http, _documents(target), headers, "Docs", id=str(target.id), pub=first_id, first=100
        )
    )["bundle"]["documents"]["nodes"]
    assert [(node["path"], node["sha256"]) for node in walked] == [
        (item["path"], item["sha256"]) for item in first["files"]
    ]
    changed = next(
        item["path"]
        for item in current["files"]
        if item not in first["files"] and document_kind(item["path"]) == "VIEW"
    )
    old = (
        await _rest(http, target, headers, "/document", path=changed, publication_id=first_id)
    ).json()
    read = _data(
        await _gql(
            http, _document(target), headers, "Doc", id=str(target.id), pub=first_id, path=changed
        )
    )["bundle"]["document"]
    assert read["text"] == old["content"]
    assert read["publicationId"] == first_id
    newest = (await _rest(http, target, headers, "/document", path=changed)).json()
    assert read["text"] != newest["content"]

    # History beneath a pinned read is still the lineage's, newest first, with the current one
    # marked current.
    history = _data(
        await _gql(
            http,
            _publications(target),
            headers,
            "History",
            id=str(target.id),
            pub=first_id,
            first=10,
        )
    )["bundle"]["publications"]["nodes"]
    assert [(node["sequence"], node["isCurrent"]) for node in history] == [(2, True), (1, False)]

    for unknown in (str(uuid4()),):
        refused = await _gql(
            http, _inspect(target), headers, "Inspect", id=str(target.id), pub=unknown
        )
        assert refused.json()["data"]["bundle"] is None
        assert _codes(refused.json()) == [("NOT_FOUND", None)]
        assert (await _rest(http, target, headers, publication_id=unknown)).status_code == 404

    other = next(item for name, item in _targets(world).items() if name != target.name)
    foreign = (await _rest(http, other, headers)).json()["publication"]["publication_id"]
    refused = await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id), pub=foreign)
    assert _codes(refused.json()) == [("NOT_FOUND", None)]
    assert (await _rest(http, target, headers, publication_id=foreign)).status_code == 404


async def test_findings_are_a_connection_of_the_policys_verdict(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    headers = _headers(world.org)
    rest = (await _rest(http, target, headers)).json()
    assert rest["valid"] is True and rest["findings"] == []
    empty = _data(
        await _gql(http, _findings(target), headers, "Findings", id=str(target.id), first=5)
    )
    assert empty["bundle"]["findings"] == {
        "totalCount": 0,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    }

    # A publication the publish policy found fault with: the verdict is stored with it, and is
    # what a reader is told -- REST's list and GraphQL's pages are the same findings.
    publication = await world.db.scalar(select(OkfBundlePublication))
    assert publication is not None
    findings = [
        "DANGLING_LINK:a/index.md:b.md",
        "EXTERNAL_LINK:a/x.md:https://example.test",
        "ROOT_INDEX_MISSING",
    ]
    publication.change_summary = {
        **dict(publication.change_summary),
        "validation": {"valid": False, "findings": findings},
    }
    await world.db.commit()
    rest = (await _rest(http, target, headers)).json()
    assert (rest["valid"], rest["findings"]) == (False, findings)

    nodes: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        page = _data(
            await _gql(
                http,
                _findings(target),
                headers,
                "Findings",
                id=str(target.id),
                first=2,
                after=after,
            )
        )["bundle"]
        assert (page["valid"], page["findingCount"]) == (False, 3)
        nodes.extend(page["findings"]["nodes"])
        if not page["findings"]["pageInfo"]["hasNextPage"]:
            break
        after = page["findings"]["pageInfo"]["endCursor"]
    assert [node["text"] for node in nodes] == findings
    assert [(node["code"], node["detail"]) for node in nodes] == [
        ("DANGLING_LINK", "a/index.md:b.md"),
        ("EXTERNAL_LINK", "a/x.md:https://example.test"),
        ("ROOT_INDEX_MISSING", None),
    ]


# --- the same refusals REST gives -----------------------------------------------------------


async def test_the_same_refusals_as_rest_with_rests_meaning(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    async def both(
        headers: dict[str, str], identifier: UUID
    ) -> tuple[int, list[tuple[str, str | None]], Any]:
        rest_target = Target(
            target.name,
            target.field,
            target.id_argument,
            identifier,
            target.rest.replace(str(target.id), str(identifier)),
        )
        rest = await _rest(http, rest_target, headers)
        response = await _gql(
            http, _inspect(rest_target), headers, "Inspect", id=str(identifier)
        )
        body = response.json()
        return rest.status_code, _codes(body), body["data"]["bundle"] if body["data"] else None

    # A role that may not read a bundle at all: the route's 403, ROLE_REQUIRED here.
    status, codes, data = await both(_headers(world.org, "Viewer"), target.id)
    assert (status, data) == (403, None)
    assert codes == [("FORBIDDEN", "ROLE_REQUIRED")]

    # An unknown id: the route's 404.
    status, codes, data = await both(_headers(world.org), uuid4())
    assert (status, codes, data) == (404, [("NOT_FOUND", None)], None)

    # Another tenant's caller: the tenant boundary.
    status, codes, data = await both(_headers(world.other_org), target.id)
    assert (status, data) == (403, None)
    assert codes == [("FORBIDDEN", "CROSS_ORGANIZATION")]

    # Nothing was published for any of the refused readers.
    assert await _count(world.db, OkfBundlePublication) == 0


async def test_a_context_product_bundle_is_refused_where_the_compile_route_refuses(
    http: httpx.AsyncClient, world: World
) -> None:
    """The product's own gates -- consumer roles, and a contracted agent's capability envelope
    -- are the compiler's scope resolver's, reached through the store: what the route 404s,
    the field answers NOT_FOUND, and nothing is published for the refused reader."""
    target = _targets(world)["product"]
    world.db.add(
        AgentContract(
            id=uuid4(),
            organization_id=world.org.id,
            ai_asset_version_id=uuid4(),
            agent_principal_id="agent:okf-bot",
            capability_envelope={
                "tool_slugs": [],
                "context_product_ids": ["some_other_context"],
                "write_lanes": [],
            },
            autonomy_tier="T1",
            supervisor_persona="STEWARD",
            kill_scope="AGENT",
            sampling_rate=0.05,
            created_by="agent-owner",
        )
    )
    await world.db.commit()

    cases = {
        # AgentDeveloper reads bundles, but is not among the version's consumer roles.
        "not-a-consumer-role": _headers(world.org, "AgentDeveloper"),
        "outside-the-agents-envelope": _headers(
            world.org, "DataSteward", principal="agent:okf-bot", principal_type="AGENT"
        ),
    }
    for name, headers in cases.items():
        rest = await _rest(http, target, headers)
        body = (
            await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id))
        ).json()
        assert rest.status_code == 404, name
        assert body["data"]["bundle"] is None, name
        assert _codes(body) == [("NOT_FOUND", None)], name
    assert await _count(world.db, OkfBundlePublication) == 0
    assert await _count(world.db, ContextProductConsumptionEdge) == 0


async def test_an_agent_whose_envelope_names_the_product_reads_it(
    http: httpx.AsyncClient, world: World
) -> None:
    target = _targets(world)["product"]
    world.db.add(
        AgentContract(
            id=uuid4(),
            organization_id=world.org.id,
            ai_asset_version_id=uuid4(),
            agent_principal_id="agent:okf-bot",
            capability_envelope={
                "tool_slugs": [],
                "context_product_ids": ["revenue_context"],
                "write_lanes": [],
            },
            autonomy_tier="T1",
            supervisor_persona="STEWARD",
            kill_scope="AGENT",
            sampling_rate=0.05,
            created_by="agent-owner",
        )
    )
    await world.db.commit()
    headers = _headers(world.org, "DataSteward", principal="agent:okf-bot", principal_type="AGENT")
    read = _data(await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id)))
    assert read["bundle"]["productKey"] == "revenue_context"


# --- the workspace gate: a datasource's bundle names its tables ----------------------------------


async def _bind_warehouse(world: World, *, owner: str) -> SourceBinding:
    """An ENFORCE workspace the warehouse is bound to, owned by someone other than the reader
    unless `owner` says otherwise -- ENFORCE because a SHADOW workspace turns every denial into
    an allow by design, and a refusal test run in shadow mode would assert nothing."""
    workspace = await create_workspace(
        world.db,
        organization_id=world.org.id,
        name="Restricted warehouse",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="p",
        owner_principal=owner,
    )
    workspace.authorization_mode = "ENFORCE"
    binding = await request_binding(
        world.db,
        organization_id=world.org.id,
        workspace_id=workspace.id,
        datasource_id=world.datasource_id,
        purpose="p",
        requested_by=owner,
    )
    await approve_binding(world.db, binding, approver_principal="reviewer")
    await world.db.commit()
    return binding


async def test_a_reader_the_workspace_refuses_gets_the_gates_refusal_not_an_empty_bundle(
    http: httpx.AsyncClient, world: World
) -> None:
    """R11-D28 / R11-D30: a source bundle names tables, views and routines, so every read of it
    asks the datasource's workspace gate (`READ_METADATA`) before anything is looked up -- and a
    reader it refuses is answered with the gate's own reason. Not a bundle with nothing in it:
    an empty bundle would say the datasource has no objects, which is the leak the gate exists
    to prevent. Nothing is published for them, and no child field answers either."""
    target = _targets(world)["source"]
    await _bind_warehouse(world, owner="workspace-owner")
    headers = _headers(world.org)

    rest = await _rest(http, target, headers)
    assert (rest.status_code, rest.json()["detail"]) == (403, "NO_WORKSPACE_MEMBERSHIP")

    everything = (
        f"query Everything($id: ID!) {{ bundle: datasourceOkfBundle(datasourceId: $id) {{ "
        f"{_BUNDLE} documents(first: 5) {{ totalCount nodes {{ path }} }} "
        "publications(first: 5) { totalCount } findings(first: 5) { totalCount } } }"
    )
    body = (await _gql(http, everything, headers, "Everything", id=str(target.id))).json()
    assert body["data"]["bundle"] is None
    assert _codes(body) == [("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP")]
    # No object name, count or path of the datasource reached the answer.
    text = json.dumps(body)
    for name in ("orders", "orders_v", "rebuild_totals", "sales", "bank"):
        assert name not in text.replace("okf-bundle", ""), name

    assert await _count(world.db, OkfBundlePublication) == 0
    refused_audits = (
        await world.db.scalars(
            select(AuditEvent).where(AuditEvent.action.like("graphql.datasource.%"))
        )
    ).all()
    assert refused_audits == []


async def test_a_workspace_member_is_admitted_and_a_revoked_binding_removes_the_bundle(
    http: httpx.AsyncClient, world: World
) -> None:
    """The gate decides on every read: the member is admitted (to the bundle REST serves);
    revoke the binding and the reader is refused -- the current read and a pinned read of the
    publication built under it alike -- while the stored rows remain and nothing new is
    published. Restore it and the same publication is served again."""
    target = _targets(world)["source"]
    settings = Settings(_env_file=None, unresolved_workspace_posture="DENY")
    app.dependency_overrides[get_settings] = lambda: settings
    binding = await _bind_warehouse(world, owner="steward")
    headers = _headers(world.org)

    granted = _data(await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id)))[
        "bundle"
    ]
    rest = (await _rest(http, target, headers)).json()
    assert granted["publication"]["publicationId"] == rest["publication"]["publication_id"]
    publications = await _count(world.db, OkfBundlePublication)
    assert publications == 1

    binding.status = "REVOKED"
    await world.db.commit()
    for pinned in (None, granted["publication"]["publicationId"]):
        body = (
            await _gql(
                http, _inspect(target), headers, "Inspect", id=str(target.id), pub=pinned
            )
        ).json()
        assert body["data"]["bundle"] is None
        assert _codes(body) == [("FORBIDDEN", "NO_BINDING_FOR_DATASOURCE")]
        assert (await _rest(http, target, headers, publication_id=pinned)).status_code == 403
    assert await _count(world.db, OkfBundlePublication) == publications
    kept = await world.db.get(OkfBundlePublication, UUID(granted["publication"]["publicationId"]))
    assert kept is not None

    binding.status = "ACTIVE"
    await world.db.commit()
    restored = _data(await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id)))[
        "bundle"
    ]
    assert restored["publication"]["publicationId"] == granted["publication"]["publicationId"]


async def test_a_member_of_an_enforcing_workspace_is_admitted_under_an_allow_policy(
    http: httpx.AsyncClient, world: World
) -> None:
    """The lineage precedent's estate shape: the non-member is refused, the same principal once
    admitted as a member (with an ALLOW policy, without which an enforcing workspace default-
    denies even its members) reads the bundle, and a workspace-free datasource is unchanged."""
    target = _targets(world)["source"]
    binding = await _bind_warehouse(world, owner="workspace-owner")
    headers = _headers(world.org)
    refused = (await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id))).json()
    assert _codes(refused) == [("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP")]

    world.db.add(
        WorkspaceMembership(
            organization_id=world.org.id,
            workspace_id=binding.workspace_id,
            principal_id="steward",
            role="analyst",
            status="ACTIVE",
            granted_by="test",
        )
    )
    world.db.add(
        AccessPolicy(
            organization_id=world.org.id,
            code="baseline-allow",
            name="baseline allow",
            effect="ALLOW",
            subject_match={"roles": ["analyst"]},
            action_match=[],
            created_by="test",
        )
    )
    await world.db.commit()
    admitted = _data(await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id)))
    assert admitted["bundle"]["datasourceId"] == str(world.datasource_id)

    # Another datasource, bound to no workspace, is read as before (the default SHADOW posture).
    other_id = world.estate["datasources"]["reporting"][0].id
    free = Target(
        "source", target.field, target.id_argument, other_id, target.rest.replace(
            str(world.datasource_id), str(other_id)
        ),
    )
    assert (await _rest(http, free, headers)).status_code == 200
    read = _data(await _gql(http, _inspect(free), headers, "Inspect", id=str(other_id)))
    assert read["bundle"]["datasourceId"] == str(other_id)


async def test_a_schema_the_workspace_refuses_is_absent_from_every_document_and_count(
    http: httpx.AsyncClient, world: World
) -> None:
    """OKF-D through GraphQL: a policy refusing `READ_METADATA` on the `hr_*` schemas removes
    that schema, its table and its column from every count and every document's text -- the
    store decided per schema, and no field beneath the bundle can name what it did not admit."""
    target = _targets(world)["source"]
    hidden = await _hidden_schema(world.db, world.estate)
    await _bind_warehouse(world, owner="steward")
    world.db.add(
        AccessPolicy(
            organization_id=world.org.id,
            code="no-hr-metadata",
            name="No HR schemas",
            effect="DENY",
            priority=1000,
            resource_match={"schema_pattern": "hr_*"},
            action_match=["READ_METADATA"],
            created_by="seed",
        )
    )
    await world.db.commit()
    headers = _headers(world.org)

    read = _data(await _gql(http, _inspect(target), headers, "Inspect", id=str(target.id)))[
        "bundle"
    ]
    counts = read["counts"]
    assert (counts["schemas"], counts["tables"], counts["views"]) == (1, 1, 1)
    docs = _data(
        await _gql(http, _documents(target), headers, "Docs", id=str(target.id), first=100)
    )["bundle"]["documents"]["nodes"]
    seen = []
    for node in docs:
        text = _data(
            await _gql(
                http, _document(target), headers, "Doc", id=str(target.id), path=node["path"]
            )
        )["bundle"]["document"]["text"]
        seen.append(text)
    joined = "\n".join(seen) + json.dumps(read) + json.dumps(docs)
    for leaked in (HIDDEN_TABLE, HIDDEN_SCHEMA, "zzq_hidden_salary", str(hidden.id)):
        assert leaked not in joined, leaked


# --- what a list carries, and what a read records ----------------------------------------------


async def test_a_list_answer_holds_no_document_text_and_no_planted_value(
    http: httpx.AsyncClient, world: World, target: Target
) -> None:
    """INV-6, twice. A page of documents carries paths and digests and none of the words a
    document says; and across the widest thing the schema allows -- the manifest, every
    document's text, the history, the findings -- none of the sentinels planted in a routine
    body, a view definition, a column default and a source comment appears."""
    headers = _headers(world.org)
    page = (
        await _gql(http, _documents(target), headers, "Docs", id=str(target.id), first=100)
    ).json()
    listing = json.dumps(page)
    for phrase in ("One row per completed order", "Rebuilds the daily order totals"):
        assert phrase not in listing

    walked = page["data"]["bundle"]["documents"]["nodes"]
    answers = [
        _data(
            await _gql(
                http, _document(target), headers, "Doc", id=str(target.id), path=node["path"]
            )
        )
        for node in walked
    ]
    everything = json.dumps(answers) + listing
    assert "One row per completed order" in everything
    wide = _data(
        await _gql(
            http,
            _inspect(
                target,
                "publications(first: 5) { nodes { publicationId } } findings(first: 5) "
                "{ nodes { text } }",
            ),
            headers,
            "Inspect",
            id=str(target.id),
        )
    )
    everything += json.dumps(wide)
    for sentinel in _SENTINELS:
        assert sentinel not in everything, sentinel


async def test_admission_prices_every_okf_connection_at_its_page_size(
    http: httpx.AsyncClient, world: World
) -> None:
    """Each list is a connection, so the estimate multiplies its `first`: the bundle, its counts
    and publication, and three connections each with its pageInfo and one page. An oversized
    page, or aliases that would exceed the 500-object budget, are refused before a statement --
    so before anything is published."""
    target = _targets(world)["product"]
    headers = _headers(world.org)

    def query(first: int, aliases: int = 1) -> str:
        body = " ".join(
            f"b{index}: {target.field}({target.id_argument}: $id) {{ counts {{ tables }} "
            f"publication {{ sequence }} "
            f"documents(first: {first}) {{ pageInfo {{ hasNextPage }} nodes {{ path }} }} "
            f"publications(first: {first}) {{ pageInfo {{ hasNextPage }} nodes {{ sequence }} }} "
            f"findings(first: {first}) {{ pageInfo {{ hasNextPage }} nodes {{ text }} }} }}"
            for index in range(aliases)
        )
        return f"query Priced($id: ID!) {{ {body} }}"

    admitted = await _gql(http, query(20), headers, "Priced", id=str(target.id))
    assert admitted.status_code == 200, admitted.text
    # bundle + counts + publication, then per connection: itself, its pageInfo and 20 nodes.
    assert admitted.json()["extensions"]["cost"]["estimatedNodes"] == 3 + 3 * (1 + 1 + 20)

    before = (await _count(world.db, AuditEvent), await _count(world.db, OkfBundlePublication))
    too_big = await _gql(http, query(101), headers, "Priced", id=str(target.id))
    assert too_big.status_code == 400
    assert _codes(too_big.json()) == [("PAGE_SIZE_EXCEEDED", None)]
    over_budget = await _gql(http, query(100, aliases=2), headers, "Priced", id=str(target.id))
    assert over_budget.status_code == 400
    assert _codes(over_budget.json()) == [("NODE_BUDGET_EXCEEDED", None)]
    after = (await _count(world.db, AuditEvent), await _count(world.db, OkfBundlePublication))
    assert after == before

    plain = admit_document(
        query=query(20),
        operation_name="Priced",
        variables={"id": str(target.id)},
        schema=metadata_schema._schema,
        limits=DEFAULT_LIMITS,
    )
    assert plain[1].estimated_nodes == 3 + 3 * 22


async def test_a_read_is_recorded_once_per_request_however_many_aliases_ask(
    http: httpx.AsyncClient, world: World
) -> None:
    """As REST's routes record theirs -- an audit event, an outbox event and, for a PUBLISHED
    version, a consumption edge -- on GraphQL's own channels, and once: sibling aliases and
    child fields decide and record together. A document read is recorded per path, with the
    path, as the document route records it."""
    target = _targets(world)["product"]
    headers = _headers(world.org)
    files = (await _rest(http, target, headers)).json()["files"]
    tables = [item["path"] for item in files if document_kind(item["path"]) == "TABLE"]
    assert len(tables) >= 2
    query = (
        "query Twice($id: ID!, $one: String!, $two: String!) { "
        "a: contextProductOkfBundle(versionId: $id) { publication { sequence } } "
        "b: contextProductOkfBundle(versionId: $id) { "
        "documents(first: 1) { totalCount } publications(first: 5) { totalCount } "
        "findings(first: 1) { totalCount } "
        "d1: document(path: $one) { path } d2: document(path: $one) { sha256 } "
        "d3: document(path: $two) { path } } }"
    )
    body = (
        await _gql(
            http, query, headers, "Twice", id=str(target.id), one=tables[0], two=tables[1]
        )
    ).json()
    assert "errors" not in body, body

    audits = (
        await world.db.scalars(
            select(AuditEvent).where(AuditEvent.action.like("graphql.%")).order_by(AuditEvent.id)
        )
    ).all()
    actions = sorted(event.action for event in audits)
    assert actions == [
        "graphql.context_product.okf_bundle_read",
        "graphql.context_product.okf_document_read",
        "graphql.context_product.okf_document_read",
        "graphql.context_product.okf_publications_read",
    ]
    assert sorted(
        str(event.details["path"]) for event in audits if "document" in event.action
    ) == sorted(tables[:2])
    for event in audits:
        assert event.resource_id == str(world.version_id)
        assert event.details["publication_sequence"] == 1
        assert "question" not in json.dumps(event.details)

    edges = (
        await world.db.scalars(
            select(ContextProductConsumptionEdge).where(
                ContextProductConsumptionEdge.channel.like("GRAPHQL_%")
            )
        )
    ).all()
    assert sorted(edge.channel for edge in edges) == [
        "GRAPHQL_OKF_DOCUMENT",
        "GRAPHQL_OKF_DOCUMENT",
        "GRAPHQL_OKF_HISTORY",
        "GRAPHQL_OKF_MANIFEST",
    ]
    assert {edge.principal_id for edge in edges} == {"steward"}
    events = (
        await world.db.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "context.okf_bundle_exported.v1")
        )
    ).all()
    assert sorted(
        str(dict(event.payload)["channel"])
        for event in events
        if str(dict(event.payload)["channel"]).startswith("GRAPHQL_")
    ) == [
        "GRAPHQL_OKF_DOCUMENT",
        "GRAPHQL_OKF_DOCUMENT",
        "GRAPHQL_OKF_HISTORY",
        "GRAPHQL_OKF_MANIFEST",
    ]


async def test_a_source_bundle_read_is_recorded_without_a_consumption_edge(
    http: httpx.AsyncClient, world: World
) -> None:
    """A datasource is not a context product and has no consumption ledger: the source reads
    leave the audit and outbox evidence REST's source routes leave, on their own channels."""
    target = _targets(world)["source"]
    headers = _headers(world.org)
    files = (await _rest(http, target, headers)).json()["files"]
    path = next(item["path"] for item in files if document_kind(item["path"]) == "TABLE")
    query = (
        "query Once($id: ID!, $path: String!) { bundle: datasourceOkfBundle(datasourceId: $id) { "
        "publications(first: 5) { totalCount } document(path: $path) { path } } }"
    )
    body = (await _gql(http, query, headers, "Once", id=str(target.id), path=path)).json()
    assert "errors" not in body, body
    audits = (
        await world.db.scalars(select(AuditEvent).where(AuditEvent.action.like("graphql.%")))
    ).all()
    assert sorted(event.action for event in audits) == [
        "graphql.datasource.okf_bundle_read",
        "graphql.datasource.okf_document_read",
        "graphql.datasource.okf_publications_read",
    ]
    assert {event.resource_type for event in audits} == {"datasource"}
    assert {event.resource_id for event in audits} == {str(world.datasource_id)}
    assert await _count(world.db, ContextProductConsumptionEdge) == 0
    channels = sorted(
        str(dict(event.payload)["channel"])
        for event in (
            await world.db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "datasource.okf_bundle_exported.v1"
                )
            )
        ).all()
        if str(dict(event.payload)["channel"]).startswith("GRAPHQL_")
    )
    assert channels == [
        "GRAPHQL_OKF_SOURCE_DOCUMENT",
        "GRAPHQL_OKF_SOURCE_HISTORY",
        "GRAPHQL_OKF_SOURCE_MANIFEST",
    ]


async def test_an_id_that_is_not_a_uuid_is_an_invalid_argument(
    http: httpx.AsyncClient, world: World
) -> None:
    headers = _headers(world.org)
    for field, argument in (
        ("contextProductOkfBundle", "versionId"),
        ("datasourceOkfBundle", "datasourceId"),
    ):
        body = (
            await _gql(
                http,
                f"query Bad {{ bundle: {field}({argument}: \"not-a-uuid\") {{ okfVersion }} }}",
                headers,
                "Bad",
            )
        ).json()
        assert _codes(body) == [("INVALID_ARGUMENT", "INVALID_ID")], field
