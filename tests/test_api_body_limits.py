"""The proxy's request-body bounds hold on the API's own port too (R11-AUD11).

FastAPI reads a route's whole JSON body before its dependencies run, the role guard included, so
before `RequestBodyLimitMiddleware` an unauthenticated caller on port 8000 could make the process
hold any amount before being told 401. These tests send real requests through the application.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import pytest_asyncio

from aida.main import app
from aida.request_body_limits import (
    DEFAULT_BODY_LIMIT,
    MIB,
    body_limit_for,
)

_REPO = Path(__file__).resolve().parent.parent
_ID = "00000000-0000-0000-0000-000000000000"


def _load_script(name: str) -> ModuleType:
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
NGINX = contract.strip_nginx_comments(contract.NGINX_CONF.read_text(encoding="utf-8"))


def _nginx_limit(path: str) -> float:
    locations = contract.parse_nginx_locations(NGINX)
    location = contract.resolve_location(locations, path)
    assert location is not None, path
    return contract.effective_body_limit(location, contract.server_level_body_limit(NGINX))


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        yield client


async def _chunked(total: int, chunk: int = 256 * 1024) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        size = min(chunk, total - sent)
        sent += size
        yield b"x" * size


# --- the numbers are the proxy's ------------------------------------------------------------


@pytest.mark.parametrize("path", list(contract.INGESTION_BODY_ROUTES) + ["/mcp"])
def test_a_route_with_a_larger_bound_has_the_proxy_s_bound(path: str) -> None:
    assert body_limit_for(path) == _nginx_limit(path)


@pytest.mark.parametrize("path", list(contract.GENERAL_API_ROUTES) + ["/v1/security/tokens/revoke"])
def test_every_other_route_has_the_proxy_s_default(path: str) -> None:
    assert body_limit_for(path) == DEFAULT_BODY_LIMIT == _nginx_limit(path)


@pytest.mark.parametrize("path", list(contract.UPLOAD_BODY_ROUTES) + ["/graphql"])
def test_routes_that_bound_their_own_body_are_left_to_their_handler(path: str) -> None:
    """Their refusals carry route-specific detail clients read (`ARCHIVE_TOO_LARGE`, GraphQL's
    document refusal), and `test_proxy_body_limits.py` already binds their handler constants to
    nginx's."""
    assert body_limit_for(path) is None


# --- declared and undeclared lengths --------------------------------------------------------


async def test_a_declared_length_over_the_bound_is_refused_before_authentication(
    http: httpx.AsyncClient,
) -> None:
    """No identity headers at all: a 401 would mean the body had been read first."""
    response = await http.post(
        "/v1/security/tokens/revoke",
        content=b"x" * (DEFAULT_BODY_LIMIT + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "the request body exceeds the 1 MiB limit for this route"}


async def test_a_chunked_body_over_the_bound_is_refused_once_it_passes_it(
    http: httpx.AsyncClient,
) -> None:
    response = await http.post(
        "/v1/security/tokens/revoke",
        content=_chunked(2 * MIB),
        headers={"Content-Type": "application/json"},
    )

    assert "content-length" not in {name.lower() for name in response.request.headers}
    assert response.status_code == 413
    assert "1 MiB" in response.json()["detail"]


async def test_a_body_inside_the_bound_reaches_the_route(http: httpx.AsyncClient) -> None:
    response = await http.post(
        "/v1/security/tokens/revoke",
        content=b"{" + b" " * (512 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code != 413


async def test_an_envelope_route_takes_a_body_ordinary_routes_may_not(
    http: httpx.AsyncClient,
) -> None:
    response = await http.post(
        f"/v1/datasources/{_ID}/metadata-ingestions",
        content=b"x" * (2 * MIB),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code != 413


async def test_mcp_is_bounded_at_the_proxy_s_eight_mib(http: httpx.AsyncClient) -> None:
    over = await http.post(
        "/mcp", content=b"x" * (8 * MIB + 1), headers={"Content-Type": "application/json"}
    )
    under = await http.post(
        "/mcp", content=b"x" * (2 * MIB), headers={"Content-Type": "application/json"}
    )

    assert over.status_code == 413
    assert "8 MiB" in over.json()["detail"]
    assert under.status_code != 413


async def test_a_handler_bounded_upload_keeps_its_own_refusal(http: httpx.AsyncClient) -> None:
    """The middleware stays out of the way: 2 MiB reaches the workbook import's own checks."""
    response = await http.post(
        f"/v1/datasources/{_ID}/model/import?filename=w.xlsx",
        content=b"x" * (2 * MIB),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code != 413


async def test_a_request_without_a_body_is_untouched(http: httpx.AsyncClient) -> None:
    response = await http.get("/health/live")

    assert response.status_code == 200
