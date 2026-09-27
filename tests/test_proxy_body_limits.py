"""R11-AUD05, the proxy half -- the request-body limit on the ingestion routes.

`ui-next/nginx.conf` set `client_max_body_size` only on `/mcp` and `/graphql`, so `/v1/` kept
nginx's built-in 1 MiB. The two routes that carry a metadata envelope were capped with everything
else: a 2 MiB push answered 413 through the UI proxy while the same body reached authentication on
the API's own port, contradicting the contract's documented (and larger) proxy limit.

`scripts/check_proxy_contract.py` is the static gate CI runs (job `quality`); it is standard
library only, so it carries its numbers as constants. This file is what keeps those constants
honest, in four parts:

1. the real configuration satisfies the contract, and the intended limit of every kind of route is
   written out here as a table rather than inferred;
2. the gate's numbers are bound to the code: its route list to the application's own route table,
   its floor to the synchronous caps in the ingestion schema;
3. the gate is seen to FAIL on a broken copy of the config -- a check that has never been seen to
   fail is not known to work -- and its re-implementation of nginx's location matching is checked
   against the worked example in nginx's own documentation;
4. the live counterpart (the `ui-proxy` CI job and its stub upstream) is pinned, since it cannot be
   run here: it needs Docker.

What none of this can prove is that nginx itself behaves as the resolver says. That is the
`ui-proxy` job's, and it has not been run against this change.
"""

from __future__ import annotations

import http.client
import importlib.util
import re
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

import aida.schemas  # noqa: F401  -- must precede the atlas import: the schemas shim is circular
from aida.main import app
from aida.schemas import (
    MetadataColumnEnvelope,
    MetadataIngestionChunkCreate,
    MetadataIngestionCreate,
    MetadataTableEnvelope,
)
from atlas.modules.ingestion import schemas as ingestion_schemas

_REPO = Path(__file__).resolve().parent.parent
_MIB = 1024 * 1024


def _load_script(name: str) -> ModuleType:
    """Import a file from `scripts/` by path -- it is a script, not a package member.

    Registered in `sys.modules` while it executes because `@dataclass` looks its own module up
    by name to resolve string annotations, and removed again so the import leaves no trace.
    """
    spec = importlib.util.spec_from_file_location(name, _REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


contract = _load_script("check_proxy_contract")
stub = _load_script("proxy_contract_stub_api")

NGINX = contract.strip_nginx_comments(contract.NGINX_CONF.read_text(encoding="utf-8"))
VITE = contract.strip_js_comments(contract.VITE_CONFIG.read_text(encoding="utf-8"))

_ID = contract._PLACEHOLDER_ID  # the placeholder the gate's own paths use


def _limit_for(path: str, nginx_text: str = NGINX) -> float:
    locations = contract.parse_nginx_locations(nginx_text)
    location = contract.resolve_location(locations, path)
    assert location is not None, f"nginx serves nothing at {path}"
    return contract.effective_body_limit(location, contract.server_level_body_limit(nginx_text))


# --- 1. the real configuration -----------------------------------------------


def test_the_real_proxy_configuration_satisfies_the_contract() -> None:
    assert contract.contract_problems(NGINX, VITE) == []


def test_the_gate_script_passes_on_this_repository(capsys: pytest.CaptureFixture[str]) -> None:
    assert contract.main() == 0

    output = capsys.readouterr().out
    assert "envelope-carrying ingestion routes" in output
    assert "Proxy contract violation" not in output


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The two routes R11-AUD05 is about, and the limit the change gives them.
        (f"/v1/datasources/{_ID}/metadata-ingestions", 64 * _MIB),
        (f"/v1/metadata-ingestion-batches/{_ID}/chunks", 64 * _MIB),
        # With and without a trailing slash: the API redirects one to the other.
        (f"/v1/datasources/{_ID}/metadata-ingestions/", 64 * _MIB),
        # Everything else under /v1/ keeps nginx's default -- including the ingestion routes
        # that carry no envelope, which a loosely written pattern would have caught.
        (f"/v1/datasources/{_ID}/metadata-ingestion-batches", 1 * _MIB),
        (f"/v1/metadata-ingestion-batches/{_ID}/finalize", 1 * _MIB),
        (f"/v1/metadata-ingestion-batches/{_ID}", 1 * _MIB),
        (f"/v1/metadata-ingestion-batches/{_ID}/chunks/extra", 1 * _MIB),
        (f"/v1/projects/{_ID}/datasources", 1 * _MIB),
        (f"/v1/datasources/{_ID}/query-executions", 1 * _MIB),
        ("/v1/health", 1 * _MIB),
        # R11-AUD11: the three file uploads admit what the API admits, and their neighbours
        # (the import batch's own routes, the bundle download) keep the default.
        (f"/v1/datasources/{_ID}/model/import", 32 * _MIB),
        (f"/v1/datasources/{_ID}/model/import/", 32 * _MIB),
        (f"/v1/context-product-versions/{_ID}/okf-bundle/imports", 32 * _MIB),
        (f"/v1/context-product-versions/{_ID}/okf-bundle/imports/preview", 32 * _MIB),
        (f"/v1/model-imports/{_ID}/submit", 1 * _MIB),
        (f"/v1/datasources/{_ID}/model/import/extra", 1 * _MIB),
        (f"/v1/context-product-versions/{_ID}/okf-bundle/imports/preview/extra", 1 * _MIB),
        (f"/v1/context-product-versions/{_ID}/okf-bundle", 1 * _MIB),
        # The two other API prefixes are untouched by this change.
        ("/mcp", 8 * _MIB),
        ("/graphql", 128 * 1024),
    ],
)
def test_each_kind_of_route_gets_the_limit_it_is_meant_to(path: str, expected: int) -> None:
    assert _limit_for(path) == expected


def test_the_ingestion_location_proxies_exactly_as_v1_does() -> None:
    """A copy that drifted -- a dropped forwarding header -- would fail only for these routes."""
    locations = contract.parse_nginx_locations(NGINX)
    v1 = next(loc for loc in locations if loc.pattern == "/v1/")
    ingestion = contract.resolve_location(locations, f"/v1/datasources/{_ID}/metadata-ingestions")
    assert ingestion is not None and ingestion.is_regex

    def directives(body: str) -> set[str]:
        # The two blocks name their upstream variable differently; the variable is not the point.
        # The body limit and the timeouts are what the block exists to change (R11-AUD05, AUD11).
        own = ("set ", "client_max_body_size", "proxy_read_timeout", "proxy_send_timeout")
        return {
            re.sub(r"\$\w+_upstream", "$upstream", re.sub(r"\s+", " ", line.strip()))
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith(own)
        }

    assert directives(ingestion.body) == directives(v1.body)


# --- 2. bound to the code ------------------------------------------------------


def _envelope_carrying_post_routes() -> set[str]:
    """Every POST route in the application whose request body is an envelope or a chunk.

    Read from the published OpenAPI document, not from the router object: it names what a client
    can actually send, and it is unaffected by how the framework nests included routers.
    """
    models = {"MetadataIngestionCreate", "MetadataIngestionChunkCreate"}
    found: set[str] = set()
    for path, item in app.openapi()["paths"].items():
        post = item.get("post")
        if post is None:
            continue
        schema = post.get("requestBody", {}).get("content", {}).get("application/json", {})
        reference = schema.get("schema", {}).get("$ref", "")
        if reference.rsplit("/", 1)[-1] in models:
            found.add(re.sub(r"\{[^}]+\}", _ID, path))
    return found


def test_the_gates_route_list_is_exactly_the_envelope_carrying_post_routes() -> None:
    """A new route that takes an envelope must be added to the gate, or this fails.

    Without it the gate could stay green while a third envelope route fell back to 1 MiB.
    """
    assert _envelope_carrying_post_routes() == set(contract.INGESTION_BODY_ROUTES)


def _segment_pattern(part: str) -> str:
    """One OpenAPI path segment as a regex: `{name}` matches any single segment."""
    return "[^/]+" if part.startswith("{") else re.escape(part)


def test_every_route_the_gate_calls_ordinary_is_real_and_not_an_envelope_route() -> None:
    templates = [
        re.compile("^" + "/".join(_segment_pattern(part) for part in path.split("/")) + "$")
        for path in app.openapi()["paths"]
    ]
    for probe in contract.GENERAL_API_ROUTES:
        assert any(template.match(probe) for template in templates), (
            f"{probe} is not a route of the application; a probe that names no real route "
            "would keep the gate green while checking nothing"
        )
        assert probe not in _envelope_carrying_post_routes()


def test_the_upload_limit_is_the_one_the_api_enforces() -> None:
    """R11-AUD11: the proxy admits exactly the bytes the upload handlers accept.

    Lower, and a file the API would take gets a bare nginx 413; higher, and nginx buffers bytes
    the API will refuse anyway. Raising either constant fails this until nginx follows.
    """
    from aida.model_import import MAX_UPLOAD_BYTES
    from aida.okf_import_bundle import MAX_ARCHIVE_BYTES

    assert contract.UPLOAD_BODY_BYTES == MAX_UPLOAD_BYTES == MAX_ARCHIVE_BYTES
    for route in contract.UPLOAD_BODY_ROUTES:
        assert _limit_for(route) == contract.UPLOAD_BODY_BYTES


def test_every_envelope_and_upload_route_waits_for_the_api_long_enough() -> None:
    """R11-AUD11: these requests parse and record a large body before answering, and nginx's 60 s
    default would turn a slow success into a 504 for a batch the API goes on to record."""
    locations = contract.parse_nginx_locations(NGINX)
    for route in (*contract.INGESTION_BODY_ROUTES, *contract.UPLOAD_BODY_ROUTES):
        location = contract.resolve_location(locations, route)
        assert location is not None, route
        timeout = contract._read_timeout_from(location.body)
        assert timeout is not None, f"{route} relies on nginx's 60 s default"
        assert timeout >= contract.LONG_REQUEST_READ_TIMEOUT_SECONDS, (route, timeout)


@pytest.mark.parametrize(
    ("directive", "seconds"),
    [
        ("proxy_read_timeout 300s;", 300),
        ("proxy_read_timeout 300;", 300),
        ("proxy_read_timeout 5m;", 300),
        ("proxy_read_timeout 300000ms;", 300),
        ("proxy_send_timeout 300s;", None),
    ],
)
def test_the_timeout_parser_reads_nginx_s_units(directive: str, seconds: float | None) -> None:
    assert contract._read_timeout_from(directive) == seconds


def test_every_upload_route_the_gate_names_is_a_real_post_route() -> None:
    posts = {
        re.sub(r"\{[^}]+\}", _ID, path)
        for path, operations in app.openapi()["paths"].items()
        if "post" in operations
    }
    for route in contract.UPLOAD_BODY_ROUTES:
        assert route in posts, f"{route} is not a POST route of the application"


def _bytes_at_the_synchronous_caps() -> int:
    """The JSON size of a body at the synchronous caps, with realistic identifiers.

    250,000 columns and 50,000 tables, each serialised the way the platform's own models write
    them (every optional field present, as null), with 32-character names -- wide but ordinary.
    Routines are left out on purpose: a routine body is bounded only by its own 1,000,000
    characters, so there is no honest per-routine figure, and a body carrying routine text is
    what more chunks are for.
    """
    name = "customer_account_identifier_cd".ljust(32, "x")
    column = MetadataColumnEnvelope(
        name=name, ordinal_position=100, physical_type="character varying(255)", nullable=False
    )
    table = MetadataTableEnvelope(name=name, object_type="BASE_TABLE", columns=[])
    per_column = len(column.model_dump_json()) + 1  # the separating comma
    per_table = len(table.model_dump_json()) + 1
    return (
        ingestion_schemas.SYNC_MAX_COLUMNS * per_column
        + ingestion_schemas.SYNC_MAX_TABLES * per_table
    )


def test_the_limit_covers_a_body_at_the_synchronous_caps_with_room_to_spare() -> None:
    """The derivation, recomputed. If a cap or a field is added, this fails until it is re-derived.

    The margin is what descriptions, whitespace and a pretty-printing client need; 10 % is the
    least this asserts, and the configured 64 MiB gives about 19 %.
    """
    worst = _bytes_at_the_synchronous_caps()

    assert contract.INGESTION_MIN_BODY_BYTES >= worst * 1.10
    for route in contract.INGESTION_BODY_ROUTES:
        assert _limit_for(route) >= worst * 1.10
    # And the figure the docs quote is the one this computes.
    assert round(worst / _MIB) == 54


@pytest.mark.parametrize(
    ("cap", "kind"),
    [
        ("SYNC_MAX_TABLES", "tables"),
        ("SYNC_MAX_COLUMNS", "columns"),
        ("SYNC_MAX_ROUTINES", "routines"),
    ],
)
def test_the_named_caps_are_the_ones_the_validator_enforces(
    monkeypatch: pytest.MonkeyPatch, cap: str, kind: str
) -> None:
    """The constants the derivation reads must not be decorative.

    The same two-object body validates at the real caps and is refused the moment the named
    constant is lowered to one, so raising a literal somewhere else could not move the boundary
    without this noticing.
    """

    def column(n: int) -> dict[str, object]:
        return {
            "name": f"c{n}",
            "ordinal_position": n,
            "physical_type": "bigint",
            "nullable": False,
        }

    def table(n: int) -> dict[str, object]:
        return {
            "name": f"t{n}",
            "object_type": "BASE_TABLE",
            "columns": [column(1), column(2)] if kind == "columns" else [column(1)],
        }

    routine = {"name": "r", "routine_type": "PROCEDURE", "body_sql": "BEGIN NULL; END;"}
    schema: dict[str, object] = {"name": "s", "tables": [table(1)]}
    if kind == "tables":
        schema["tables"] = [table(1), table(2)]
    if kind == "routines":
        schema["routines"] = [routine, {**routine, "name": "r2"}]
    body = {
        "idempotency_key": "caps-0001",
        "producer": "cap-probe",
        "emitted_at": "2026-09-20T12:00:00+00:00",
        "catalogs": [{"name": "c", "schemas": [schema]}],
    }

    MetadataIngestionCreate.model_validate(body)  # within the real caps
    monkeypatch.setattr(ingestion_schemas, cap, 1)
    with pytest.raises(ValueError, match="synchronous ingestion safety boundary"):
        MetadataIngestionCreate.model_validate(body)


def test_a_chunk_can_be_no_larger_than_a_synchronous_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """Why one ceiling serves both routes: a chunk is validated through the push model."""
    table = {
        "name": "t",
        "object_type": "BASE_TABLE",
        "columns": [{"name": "c", "ordinal_position": 1, "physical_type": "int", "nullable": True}],
    }
    chunk = {
        "chunk_number": 1,
        "chunk_key": "chunk-cap-0001",
        "emitted_at": "2026-09-20T12:00:00+00:00",
        "catalogs": [
            {
                "name": "c",
                "schemas": [
                    {"name": "s", "tables": [{**table, "name": "a"}, {**table, "name": "b"}]}
                ],
            }
        ],
    }

    MetadataIngestionChunkCreate.model_validate(chunk)
    monkeypatch.setattr(ingestion_schemas, "SYNC_MAX_TABLES", 1)
    with pytest.raises(ValueError, match="synchronous ingestion safety boundary"):
        MetadataIngestionChunkCreate.model_validate(chunk)


# --- 3. the gate is seen to fail ------------------------------------------------------


def _without_ingestion_location(text: str) -> str:
    start = text.index("location ~")
    return text[:start] + text[text.index("}", start) + 1 :]


def _replace(old: str, new: str) -> Callable[[str], str]:
    def transform(text: str) -> str:
        assert old in text, f"the real nginx.conf no longer contains {old!r}; update this case"
        return text.replace(old, new, 1)

    return transform


_INGESTION_LIMIT = "client_max_body_size 64m;"
_UPLOAD_LIMIT = "client_max_body_size 32m;"


def _in_block(limit: str, old: str, new: str) -> Callable[[str], str]:
    """Edit only the location block that sets `limit`, since `/mcp` and both long-request
    blocks carry the same timeout line."""

    def transform(text: str) -> str:
        start = text.rindex("location", 0, text.index(limit))
        end = text.index("}", start)
        block = text[start:end]
        assert old in block, f"the block setting {limit!r} no longer contains {old!r}"
        return text[:start] + block.replace(old, new, 1) + text[end:]

    return transform
_V1_OPEN = "location /v1/ {"
_REGEX = "^/v1/(datasources/[^/]+/metadata-ingestions|metadata-ingestion-batches/[^/]+/chunks)/?$"

_BROKEN_CONFIGS: list[tuple[str, Callable[[str], str], str]] = [
    (
        "the ingestion location is removed, so both routes fall back to /v1/'s 1 MiB",
        _without_ingestion_location,
        "needs at least 64 MiB",
    ),
    (
        "the limit is lowered below what a body at the synchronous caps needs",
        _replace(_INGESTION_LIMIT, "client_max_body_size 32m;"),
        "body limit of 32 MiB",
    ),
    (
        "the limit is 0, which nginx reads as 'do not check'",
        _replace(_INGESTION_LIMIT, "client_max_body_size 0;"),
        "turns the check off",
    ),
    (
        "the limit is 1g, a typo for 1m or an intentional loosening",
        _replace(_INGESTION_LIMIT, "client_max_body_size 1g;"),
        "above the 128 MiB ceiling",
    ),
    (
        "/v1/ itself is loosened, which is the thing the row said not to do",
        _replace(_V1_OPEN, _V1_OPEN + "\n    client_max_body_size 64m;"),
        "not an envelope route",
    ),
    (
        "the whole server is loosened",
        _replace("server_name _;", "server_name _;\n  client_max_body_size 100m;"),
        "not an envelope route",
    ),
    (
        "the pattern is loose enough to catch the batch manifest and finalize",
        _replace(_REGEX, "^/v1/.*metadata-ingestion"),
        "'/v1/datasources/00000000-0000-0000-0000-000000000000/metadata-ingestion-batches' is "
        "not an envelope route",
    ),
    (
        "the pattern forgets the chunk route",
        _replace("|metadata-ingestion-batches/[^/]+/chunks", ""),
        "metadata-ingestion-batches/00000000-0000-0000-0000-000000000000/chunks' is served by",
    ),
    (
        "/v1/ becomes ^~, which stops nginx trying the regex location at all",
        _replace(_V1_OPEN, "location ^~ /v1/ {"),
        "needs at least 64 MiB",
    ),
    (
        "the upload pattern forgets the bundle preview, so it falls back to /v1/'s 1 MiB",
        _replace("okf-bundle/imports(/preview)?)", "okf-bundle/imports)"),
        "okf-bundle/imports/preview' is served by location '/v1/' with a body limit of 1 MiB",
    ),
    (
        "the upload limit is lowered below what the API accepts",
        _replace("client_max_body_size 32m;", "client_max_body_size 16m;"),
        "the proxy should admit exactly that much",
    ),
    (
        "the upload limit is raised past what the API accepts",
        _replace("client_max_body_size 32m;", "client_max_body_size 48m;"),
        "the proxy should admit exactly that much",
    ),
    (
        "the upload location drops its read timeout and falls back to nginx's 60 s",
        _in_block(_UPLOAD_LIMIT, "proxy_read_timeout 300s;", ""),
        "with a read timeout of nginx's 60 s default",
    ),
    (
        "the envelope location's read timeout is cut back to 60 s",
        _in_block(_INGESTION_LIMIT, "proxy_read_timeout 300s;", "proxy_read_timeout 60s;"),
        "with a read timeout of 60 s",
    ),
    (
        "a location is nested, which the gate refuses to guess about",
        _replace(_V1_OPEN, _V1_OPEN + "\n    location /v1/inner/ { return 204; }"),
        "nested location",
    ),
]


@pytest.mark.parametrize(
    ("transform", "expected"),
    [pytest.param(t, e, id=name) for name, t, e in _BROKEN_CONFIGS],
)
def test_the_gate_fails_on_a_broken_copy_of_the_config(
    transform: Callable[[str], str], expected: str
) -> None:
    mutated = transform(NGINX)

    assert mutated != NGINX
    problems = contract.contract_problems(mutated, VITE)
    assert any(expected in problem for problem in problems), problems


def test_a_regex_location_is_not_mistaken_for_a_new_api_prefix() -> None:
    """The dev/production comparison is about public prefixes; the ingestion regex is neither."""
    prefixes = contract.nginx_proxied_prefixes(NGINX)

    assert prefixes == {"/v1", "/mcp", "/graphql"}
    assert not any("metadata-ingestion" in prefix for prefix in prefixes)


# nginx's own worked example for `location` (ngx_http_core_module), whose answers the
# documentation states. The gate's resolver must agree with all five, or its verdicts about the
# ingestion routes mean nothing.
_NGINX_DOCS_EXAMPLE = r"""
server {
  location = / { return 200 A; }
  location / { return 200 B; }
  location /documents/ { return 200 C; }
  location ^~ /images/ { return 200 D; }
  location ~* \.(gif|jpg|jpeg)$ { return 200 E; }
}
"""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "A"),
        ("/index.html", "B"),
        ("/documents/document.html", "C"),
        ("/images/1.gif", "D"),
        ("/documents/1.jpg", "E"),
    ],
)
def test_the_resolver_agrees_with_the_worked_example_in_nginxs_documentation(
    path: str, expected: str
) -> None:
    locations = contract.parse_nginx_locations(_NGINX_DOCS_EXAMPLE)

    resolved = contract.resolve_location(locations, path)

    assert resolved is not None and f"return 200 {expected};" in resolved.body


def test_the_first_matching_regex_wins_so_their_order_matters() -> None:
    text = "location ~ ^/a/ { return 200 first; } location ~ ^/a/b { return 200 second; }"
    locations = contract.parse_nginx_locations(text)

    resolved = contract.resolve_location(locations, "/a/b")

    assert resolved is not None and "first" in resolved.body


@pytest.mark.parametrize(
    ("directive", "expected"),
    [
        ("client_max_body_size 8m;", 8 * _MIB),
        ("client_max_body_size 128k;", 128 * 1024),
        ("client_max_body_size 1G;", 1024 * _MIB),
        ("client_max_body_size 2048;", 2048),
        ("client_max_body_size 0;", float("inf")),
        ("proxy_pass http://x;", None),
    ],
)
def test_size_directives_are_read_the_way_nginx_reads_them(
    directive: str, expected: float | None
) -> None:
    assert contract._limit_from(directive) == expected


def test_a_location_inherits_the_server_limit_and_otherwise_nginxs_default() -> None:
    location = "  location /x/ {\n    proxy_pass http://u;\n  }\n"
    inherit = "server {\n  client_max_body_size 5m;\n" + location + "}\n"
    default = "server {\n" + location + "}\n"

    assert _limit_for("/x/y", inherit) == 5 * _MIB
    assert _limit_for("/x/y", default) == 1 * _MIB


# --- 4. the live counterpart, which cannot be run here ---------------------------------


def _ui_proxy_job() -> str:
    workflow = (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"^  ui-proxy:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$)", workflow, re.S | re.M)
    assert match is not None, "the ui-proxy job is gone from .github/workflows/ci.yml"
    return match.group(1)


def test_the_live_proxy_job_posts_oversize_bodies_to_the_real_nginx_image() -> None:
    """The steps exist and carry the numbers the config does.

    This cannot prove the job passes -- that needs Docker -- but it can stop the steps being
    deleted, or their limit drifting from the config's: the job's limit is read off `nginx.conf`
    and looked for in the job text.
    """
    job = _ui_proxy_job()
    limit_mib = int(_limit_for(contract.INGESTION_BODY_ROUTES[0]) // _MIB)

    for route in ("metadata-ingestions", "/chunks", "metadata-ingestion-batches", "/finalize"):
        assert route in job
    assert "query-executions" in job
    assert "body_bytes=" in job
    assert f"$(({limit_mib} * mib))" in job, "the job never posts a body of exactly the limit"
    assert f"$(({limit_mib} * mib + 1))" in job, "the job never posts a body one byte over it"
    assert "!= 413" in job

    upload_mib = int(_limit_for(contract.UPLOAD_BODY_ROUTES[0]) // _MIB)
    for route in contract.UPLOAD_BODY_ROUTES:
        assert f'"{route.replace(_ID, "$id")}"' in job, f"the job never posts to {route}"
    assert f"$(({upload_mib} * mib))" in job, "the job never posts an upload of exactly the limit"
    assert f"$(({upload_mib} * mib + 1))" in job, "the job never posts an upload one byte over it"


@pytest.fixture
def stub_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str, body: bytes | None = None) -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body=body, headers={"Authorization": "Bearer t"})
        return connection.getresponse().read().decode()
    finally:
        connection.close()


def test_the_stub_upstream_reports_how_many_body_bytes_arrived(stub_server: int) -> None:
    """What lets the live job tell a whole body from a truncated one that was still answered 200."""
    size = 3 * _MIB + 17
    path = f"/v1/metadata-ingestion-batches/{_ID}/chunks"

    reply = _request(stub_server, "POST", path, b"x" * size)

    assert f"body_bytes={size}" in reply
    assert f"path={path}" in reply
    assert "method=POST" in reply


def test_the_stub_keeps_the_markers_the_existing_live_assertions_match(stub_server: int) -> None:
    reply = _request(stub_server, "GET", "/mcp")

    # These three substrings are what the `/mcp` and `/v1/` steps of the job glob for.
    assert stub.MARKER in reply
    assert "path=/mcp" in reply
    assert "authorization=present" in reply
    assert "body_bytes=0" in reply
