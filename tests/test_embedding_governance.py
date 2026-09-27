"""R11-MP15: embedding calls pass the kill switch, route approval, quota and attribution.

The configured embedding provider used to be reached on settings alone: no
approved route, no kill switch, no token quota, no spend record.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.embedding_governance as governance
import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.embedding_governance import governed_embedding_provider
from aida.embedding_provider import EmbeddingBatch, EmbeddingUnavailable
from aida.model_gateway import GLOBAL_KILL_SWITCH_SCOPE
from aida.models import KillSwitchState, ModelRouteConfiguration
from aida.usage_quotas import UsageDimension, source_usage, tenant_usage

pytestmark = pytest.mark.asyncio


class _FakeProvider:
    provider = "openai"
    model_id = "text-embedding-3-small"
    dimensions = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(texts)
        return EmbeddingBatch(
            vectors=tuple((1.0, 0.0) for _ in texts),
            provider=self.provider,
            model_id=self.model_id,
            dimensions=self.dimensions,
        )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeProvider:
    provider = _FakeProvider()
    monkeypatch.setattr(governance, "resolve_embedding_provider", lambda *_a, **_k: provider)
    return provider


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "embedding_provider": "openai",
        "embedding_model_id": "text-embedding-3-small",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _route(
    org: UUID, *, capabilities: tuple[str, ...] = ("EMBEDDINGS",)
) -> ModelRouteConfiguration:
    return ModelRouteConfiguration(
        organization_id=org,
        route_key="bank-embeddings",
        version=1,
        status="APPROVED",
        display_name="Bank embeddings",
        provider_type="OPENAI",
        model_id="text-embedding-3-small",
        endpoint_alias="default",
        credential_reference="env://OPENAI_API_KEY",
        data_residency="EU",
        retention_policy="ZERO_RETENTION",
        capabilities=list(capabilities),
        max_input_tokens=8000,
        max_output_tokens=100,
        timeout_seconds=30,
        fingerprint="a" * 64,
        created_by="maker",
        approved_by="checker",
        approved_at=dt.datetime.now(dt.UTC),
    )


def _kill(org: UUID, scope: str) -> KillSwitchState:
    return KillSwitchState(
        organization_id=org, route_key=scope, engaged=True, reason="drill", engaged_by="ops"
    )


async def test_an_engaged_organization_kill_switch_stops_embeddings(
    session: AsyncSession, fake: _FakeProvider
) -> None:
    org = uuid4()
    session.add(_kill(org, GLOBAL_KILL_SWITCH_SCOPE))
    await session.flush()
    with pytest.raises(EmbeddingUnavailable, match="KILL_SWITCH_ENGAGED"):
        await governed_embedding_provider(session, _settings(), organization_id=org)


async def test_a_kill_switch_on_the_embeddings_route_stops_it(
    session: AsyncSession, fake: _FakeProvider
) -> None:
    org = uuid4()
    session.add_all([_route(org), _kill(org, "bank-embeddings")])
    await session.flush()
    with pytest.raises(EmbeddingUnavailable, match="KILL_SWITCH_ENGAGED"):
        await governed_embedding_provider(session, _settings(), organization_id=org)


async def test_where_approval_is_required_an_approved_embeddings_route_is(
    session: AsyncSession, fake: _FakeProvider
) -> None:
    org = uuid4()
    settings = _settings(embedding_route_required=True)
    with pytest.raises(EmbeddingUnavailable, match="EMBEDDING_ROUTE_NOT_APPROVED"):
        await governed_embedding_provider(session, settings, organization_id=org)

    session.add(_route(org, capabilities=("SQL_GENERATION",)))
    await session.flush()
    with pytest.raises(EmbeddingUnavailable, match="EMBEDDING_ROUTE_NOT_APPROVED"):
        await governed_embedding_provider(session, settings, organization_id=org)

    other = uuid4()
    session.add(_route(other))
    await session.flush()
    provider = await governed_embedding_provider(session, settings, organization_id=other)
    assert provider.model_id == "text-embedding-3-small"


async def test_every_embed_call_is_attributed_to_the_tenant_and_source(
    session: AsyncSession, fake: _FakeProvider
) -> None:
    org, ds = uuid4(), uuid4()
    provider = await governed_embedding_provider(
        session, _settings(), organization_id=org, datasource_id=ds
    )
    await provider.embed(["a" * 400, "b" * 400])
    today = dt.datetime.now(dt.UTC).date()
    tenant = await tenant_usage(
        session, organization_id=org, dimension=UsageDimension.MODEL_TOKENS, window_date=today
    )
    source = await source_usage(
        session,
        organization_id=org,
        datasource_id=ds,
        dimension=UsageDimension.MODEL_TOKENS,
        window_date=today,
    )
    assert (tenant, source) == (200, 200)


async def test_a_spent_quota_refuses_before_anything_is_sent(
    session: AsyncSession, fake: _FakeProvider
) -> None:
    org = uuid4()
    provider = await governed_embedding_provider(
        session, _settings(model_token_daily_quota_per_organization=10), organization_id=org
    )
    with pytest.raises(EmbeddingUnavailable, match="MODEL_TOKEN_QUOTA_EXHAUSTED"):
        await provider.embed(["x" * 400])
    assert fake.calls == []


def test_approval_is_required_by_default_only_in_staging_and_production() -> None:
    def required(**values: object) -> bool:
        return Settings.model_construct(**values).embedding_route_requires_approval

    assert required(environment="production", embedding_route_required=None)
    assert required(environment="staging", embedding_route_required=None)
    assert not required(environment="development", embedding_route_required=None)
    assert not required(environment="production", embedding_route_required=False)
