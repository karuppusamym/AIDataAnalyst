"""R11-AUD11: an upload larger than its limit is refused before it is held in memory.

The workbook import (`POST /v1/datasources/{id}/model/import`) and the OKF bundle preview and apply
(`POST /v1/context-product-versions/{id}/okf-bundle/imports[/preview]`) each refused a declared
`Content-Length` over the limit before reading. A body sent chunked declares none, and they read it
whole with `await request.body()` before looking at its size, so on the API's own port that one case
cost whatever memory the sender chose to send. They now read it through
`aida.request_body.read_body_within`, which stops at the first chunk that would pass the limit.

These go through the real application with `httpx.ASGITransport`, where the body is an async
generator: the request is chunked and has no `Content-Length`, and the generator counts what the
server pulled from it. The size check is what is under test, so the parsers behind it are replaced
by a stub that records what reached them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import ClientDisconnect

import aida.models  # noqa: F401 - registers every mapped table on Base.metadata
from aida.main import app
from aida.model_import import MAX_UPLOAD_BYTES
from aida.models import DataDomain, DataSource, LineOfBusiness, Organization, Project
from aida.okf_import_bundle import ARCHIVE_TOO_LARGE, MAX_ARCHIVE_BYTES
from aida.request_body import read_body_within
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session

# One event loop for the module, because the schema is built once for all of it: `create_all` over
# every table costs about a second, and no test here writes a row.
pytestmark = pytest.mark.asyncio(loop_scope="module")

MIB = 1024 * 1024
REACHED_THE_PARSER = 418


# --------------------------------------------------------------------------- #
# The helper on its own
# --------------------------------------------------------------------------- #


def _request(
    receive: Callable[[], Any], headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": headers or []}, receive
    )


def _messages(*bodies: bytes) -> Callable[[], Any]:
    """An ASGI `receive` that delivers `bodies` as successive chunks of one request."""
    queue = list(bodies)

    async def receive() -> dict[str, Any]:
        body = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    return receive


async def test_a_body_within_the_limit_is_returned_whole_and_in_order() -> None:
    request = _request(_messages(b"abc", b"def", b"g"))

    assert await read_body_within(request, 7, detail="too large") == b"abcdefg"


async def test_a_body_of_exactly_the_limit_is_accepted() -> None:
    assert await read_body_within(_request(_messages(b"x" * 5, b"y" * 5)), 10, detail="") == (
        b"x" * 5 + b"y" * 5
    )


async def test_an_empty_body_is_returned_for_the_caller_to_judge() -> None:
    assert await read_body_within(_request(_messages(b"")), 10, detail="too large") == b""


async def test_the_first_chunk_that_would_pass_the_limit_is_refused_unread() -> None:
    """Ten chunks of a thousand bytes fit a limit of ten thousand exactly; the eleventh is the
    first to pass it, and it is the last one pulled: an endless body is cut off there."""
    pulled = 0

    async def endless() -> dict[str, Any]:
        nonlocal pulled
        pulled += 1
        # A helper that does not stop would otherwise read this forever, and fill the machine's
        # memory while it does: fail the test instead, a hundred chunks past where it must stop.
        assert pulled < 100, "the helper kept reading a body that is over the limit"
        return {"type": "http.request", "body": b"x" * 1_000, "more_body": True}

    with pytest.raises(HTTPException) as refused:
        await read_body_within(_request(endless), 10_000, detail="too large")

    assert refused.value.status_code == 413
    assert refused.value.detail == "too large"
    assert pulled == 11


async def test_the_declared_length_is_checked_before_any_of_the_body_is_read() -> None:
    async def never() -> dict[str, Any]:
        raise AssertionError("an over-limit declared length must not read the body")

    request = _request(never, [(b"content-length", b"11")])

    with pytest.raises(HTTPException) as refused:
        await read_body_within(request, 10, detail={"reason_code": "TOO_LARGE"})

    assert refused.value.status_code == 413
    assert refused.value.detail == {"reason_code": "TOO_LARGE"}


async def test_a_declared_length_that_is_not_a_number_does_not_switch_the_check_off() -> None:
    request = _request(_messages(b"x" * 6, b"y" * 6), [(b"content-length", b"lots")])

    with pytest.raises(HTTPException) as refused:
        await read_body_within(request, 10, detail="too large")

    assert refused.value.status_code == 413


async def test_a_body_longer_than_it_declared_is_still_refused() -> None:
    request = _request(_messages(b"x" * 6, b"y" * 6), [(b"content-length", b"5")])

    with pytest.raises(HTTPException) as refused:
        await read_body_within(request, 10, detail="too large")

    assert refused.value.status_code == 413


async def test_a_client_that_hangs_up_mid_upload_is_reported_as_before() -> None:
    queue: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive() -> dict[str, Any]:
        return queue.pop(0)

    with pytest.raises(ClientDisconnect):
        await read_body_within(_request(receive), 10, detail="too large")


# --------------------------------------------------------------------------- #
# Through the real application
# --------------------------------------------------------------------------- #


@dataclass
class _Estate:
    session: AsyncSession
    organization_id: UUID
    datasource_id: UUID


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def estate() -> AsyncIterator[_Estate]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        lob = LineOfBusiness(
            id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
        )
        domain = DataDomain(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            name="Retail",
            code=f"RET{uuid4().hex[:6]}",
        )
        project = Project(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            data_domain_id=domain.id,
            name="Core",
            slug=f"core-{uuid4().hex[:8]}",
        )
        db.add_all([org, lob, domain, project])
        await db.flush()
        datasource = DataSource(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            data_domain_id=domain.id,
            project_id=project.id,
            name="warehouse",
            connector_type="postgres",
            dialect="postgres",
            environment="PROD",
            network_zone="default",
            credential_reference="env://X",
            capabilities={},
        )
        db.add(datasource)
        await db.commit()
        yield _Estate(db, org.id, datasource.id)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def http(estate: _Estate) -> AsyncIterator[httpx.AsyncClient]:
    """The real application on the estate's session, OKF import switched on. Overrides are
    restored as found."""
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield estate.session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,  # type: ignore[call-arg]
        okf_import_enabled=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://upload.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(estate: _Estate) -> dict[str, str]:
    return {
        "X-Principal-Id": "upload-steward",
        "X-Roles": "DataSteward",
        "X-Organization-Id": str(estate.organization_id),
    }


class _Upload:
    """A body the test hands to httpx as an async generator, so the request is chunked and has no
    `Content-Length`, and which counts how much of it the server pulled."""

    def __init__(self, total: int, chunk: int = MIB) -> None:
        self.total = total
        self.chunk = chunk
        self.pulled_bytes = 0
        self.pulled_chunks = 0

    async def stream(self) -> AsyncIterator[bytes]:
        block = b"\0" * self.chunk
        remaining = self.total
        while remaining > 0:
            size = min(self.chunk, remaining)
            self.pulled_chunks += 1
            self.pulled_bytes += size
            remaining -= size
            yield block if size == self.chunk else block[:size]


@dataclass(frozen=True)
class _Route:
    name: str
    url: str
    limit: int
    too_large: str | dict[str, str]
    stub_target: str
    content_position: int | None  # where the parser takes `content` (None: as a keyword)


def _routes(estate: _Estate) -> dict[str, _Route]:
    version_id = uuid4()
    okf_too_large = {"reason_code": ARCHIVE_TOO_LARGE, "detail": "the archive is too large"}
    return {
        "workbook": _Route(
            "workbook",
            f"/v1/datasources/{estate.datasource_id}/model/import",
            MAX_UPLOAD_BYTES,
            f"workbook exceeds the {MAX_UPLOAD_BYTES // MIB}MB upload limit",
            "aida.model_import_api.parse_and_diff_workbook",
            None,
        ),
        "okf-preview": _Route(
            "okf-preview",
            f"/v1/context-product-versions/{version_id}/okf-bundle/imports/preview",
            MAX_ARCHIVE_BYTES,
            okf_too_large,
            "aida.okf_import_api.preview_okf_import",
            4,
        ),
        "okf-apply": _Route(
            "okf-apply",
            f"/v1/context-product-versions/{version_id}/okf-bundle/imports"
            f"?preview_digest={'0' * 64}",
            MAX_ARCHIVE_BYTES,
            okf_too_large,
            "aida.okf_import_api.apply_okf_import",
            4,
        ),
    }


ROUTES = ["workbook", "okf-preview", "okf-apply"]


def _stub_the_parser(monkeypatch: pytest.MonkeyPatch, route: _Route) -> list[int]:
    """Replace what runs after the size check with one that records the length of the body it was
    given and answers 418, so a request that got past the check is told apart from a refused one."""
    reached: list[int] = []

    async def parser(*args: Any, **kwargs: Any) -> Any:
        position = route.content_position
        content = kwargs["content"] if position is None else args[position]
        reached.append(len(content))
        raise HTTPException(status_code=REACHED_THE_PARSER, detail="reached the parser")

    monkeypatch.setattr(route.stub_target, parser)
    return reached


async def _send_chunked(
    http: httpx.AsyncClient, estate: _Estate, route: _Route, upload: _Upload
) -> httpx.Response:
    request = http.build_request(
        "POST", route.url, content=upload.stream(), headers=_headers(estate)
    )
    # The premise of every test below: what is sent is a chunked body with no declared length.
    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"
    return await http.send(request)


@pytest.mark.parametrize("chunk", [64 * 1024, MIB, 5 * MIB], ids=["64KiB", "1MiB", "5MiB"])
@pytest.mark.parametrize("route_name", ROUTES)
async def test_a_chunked_body_over_the_limit_is_refused_having_read_no_more_than_limit_plus_a_chunk(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
    chunk: int,
) -> None:
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)
    # Twice the limit on offer: a server that read it all would pull it all.
    upload = _Upload(2 * route.limit, chunk)

    response = await _send_chunked(http, estate, route, upload)

    assert response.status_code == 413
    assert response.json()["detail"] == route.too_large
    assert reached == [], "the parser must never be handed an over-limit body"
    # It had to look at the chunk that crossed the limit to know it did, and at nothing after it.
    assert route.limit < upload.pulled_bytes <= route.limit + chunk


@pytest.mark.parametrize("route_name", ROUTES)
async def test_a_chunked_body_of_exactly_the_limit_passes_the_size_check(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)

    response = await _send_chunked(http, estate, route, _Upload(route.limit))

    assert response.status_code == REACHED_THE_PARSER
    assert reached == [route.limit]


@pytest.mark.parametrize("route_name", ROUTES)
async def test_a_chunked_body_one_byte_over_the_limit_is_refused(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)

    response = await _send_chunked(http, estate, route, _Upload(route.limit + 1))

    assert response.status_code == 413
    assert response.json()["detail"] == route.too_large
    assert reached == []


@pytest.mark.parametrize("route_name", ROUTES)
async def test_a_declared_length_over_the_limit_is_refused_before_any_of_the_body_is_read(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    """Unchanged behaviour, pinned so the new path cannot displace it: a body that declares its
    size is refused on the declaration, having pulled nothing."""
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)
    upload = _Upload(route.limit + 1)

    response = await http.post(
        route.url,
        content=upload.stream(),
        headers={**_headers(estate), "content-length": str(route.limit + 1)},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == route.too_large
    assert upload.pulled_chunks == 0
    assert reached == []


@pytest.mark.parametrize("route_name", ROUTES)
async def test_a_declared_length_of_exactly_the_limit_passes_the_size_check(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)

    response = await http.post(
        route.url,
        content=b"x" * 1024,
        headers={**_headers(estate), "content-length": str(route.limit)},
    )

    assert response.status_code == REACHED_THE_PARSER
    assert reached == [1024]


@pytest.mark.parametrize("route_name", ROUTES)
async def test_an_empty_chunked_body_is_still_an_empty_body_error(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)

    response = await _send_chunked(http, estate, route, _Upload(0))

    assert response.status_code == 422
    assert response.json()["detail"] == "the request body is empty"
    assert reached == []


async def test_the_workbook_route_looks_the_datasource_up_before_it_reads_the_body(
    http: httpx.AsyncClient, estate: _Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check order is unchanged: authorization and the datasource come first, so a caller who
    may not import here is not even read from."""
    route = _routes(estate)["workbook"]
    reached = _stub_the_parser(monkeypatch, route)
    upload = _Upload(2 * route.limit)
    missing = route.url.replace(str(estate.datasource_id), str(uuid4()))
    request = http.build_request(
        "POST", missing, content=upload.stream(), headers=_headers(estate)
    )

    response = await http.send(request)

    assert response.status_code == 404
    assert upload.pulled_chunks == 0
    assert reached == []


@pytest.mark.parametrize("route_name", ["okf-preview", "okf-apply"])
async def test_a_disabled_okf_import_refuses_before_it_reads_the_body(
    http: httpx.AsyncClient,
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    route = _routes(estate)[route_name]
    reached = _stub_the_parser(monkeypatch, route)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,  # type: ignore[call-arg]
        okf_import_enabled=False,
    )
    upload = _Upload(2 * route.limit)

    response = await _send_chunked(http, estate, route, upload)

    assert response.status_code == 403
    assert response.json()["detail"]["reason_code"] == "OKF_IMPORT_DISABLED"
    assert upload.pulled_chunks == 0
    assert reached == []
