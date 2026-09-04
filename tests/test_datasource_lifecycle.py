"""Regression coverage for the datasource connectivity-test endpoint's status transitions.

Found during a full UI end-to-end validation pass: re-testing connectivity on an
already-ACTIVE datasource silently demoted it to CONNECTION_VERIFIED, which made the
Home/Sources "Active sources" tile (driven by datasource_statuses.ACTIVE) drop to zero
even though nothing about the source's admission for work had actually changed.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.connectors.base import ConnectorCapabilities
from aida.connectors.registry import connector_registry
from aida.models import DataSource
from aida.security_types import SecurityContext
from atlas.modules.connectivity.router import test_datasource as call_test_datasource


@dataclass
class _FakeSession:
    datasource: DataSource
    committed: bool = field(default=False, init=False)
    added: list[Any] = field(default_factory=list, init=False)

    async def get(self, _model: type[DataSource], _id: Any) -> DataSource:
        return self.datasource

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


class _FakeConnector:
    connector_type = "sqlserver"
    dialect = "tsql"

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(catalogs=True, schemas=True)

    async def test_connection(self) -> None:
        return None

    async def discover(self):  # pragma: no cover - unused by this endpoint
        raise NotImplementedError


def _context(organization_id: Any) -> SecurityContext:
    return SecurityContext(
        principal_id="platform-admin",
        principal_type="user",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


def _datasource(*, status: str, organization_id: Any) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="fixture",
        connector_type="sqlserver",
        dialect="tsql",
        environment="DEVELOPMENT",
        credential_reference="env://AIDA_TEST_DATASOURCE_SECRET",
        status=status,
    )


@pytest.fixture(autouse=True)
def _fake_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connector_registry, "create", lambda *_args, **_kwargs: _FakeConnector())


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDA_TEST_DATASOURCE_SECRET", "irrelevant-for-the-fake-connector")


async def test_successful_test_does_not_demote_an_active_datasource() -> None:
    organization_id = uuid4()
    datasource = _datasource(status="ACTIVE", organization_id=organization_id)
    session = _FakeSession(datasource)

    result = await call_test_datasource(
        datasource.id,
        context=_context(organization_id),
        session=session,  # type: ignore[arg-type]
        settings=Settings(),
    )

    assert result.status == "ACTIVE"
    assert session.committed is True


async def test_successful_test_still_promotes_a_registered_datasource() -> None:
    organization_id = uuid4()
    datasource = _datasource(status="REGISTERED", organization_id=organization_id)
    session = _FakeSession(datasource)

    result = await call_test_datasource(
        datasource.id,
        context=_context(organization_id),
        session=session,  # type: ignore[arg-type]
        settings=Settings(),
    )

    assert result.status == "CONNECTION_VERIFIED"
