"""R11-MP03: choosing the model route per purpose.

One `model_route` served Ask's SQL generation and every classification caller
alike. `model_routes_by_purpose` lets an operator name an approved route for
SQL_GENERATION and another for CLASSIFICATION. Naming a route approves
nothing: it must still be APPROVED, for that capability, in the caller's
organization, and the gateway admits a call only on a selected key.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.ai_governance_api import _route_read
from aida.column_description_model import ColumnDraftModelUnavailable, approved_drafting_route
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    DeterministicTestProvider,
    ModelRouteNotApproved,
    ProviderNeutralModelGateway,
    SqlGenerationOutput,
)
from aida.models import ModelRouteConfiguration, Organization
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider
from aida.semantic_inference import approved_classification_route

_BOTH = ("SQL_GENERATION", "CLASSIFICATION")


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "identity_provider": "development",
        "model_generation_enabled": True,
        "model_route": "default-route",
        "credential_provider": "vault",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_a_purpose_without_an_entry_uses_the_default_route() -> None:
    settings = _settings(model_routes_by_purpose={"CLASSIFICATION": "cheap-route"})
    assert settings.model_route_for("CLASSIFICATION") == "cheap-route"
    assert settings.model_route_for("SQL_GENERATION") == "default-route"
    assert settings.selected_model_route_keys == {"default-route", "cheap-route"}


def test_only_purposes_the_runtime_asks_for_can_be_named() -> None:
    with pytest.raises(ValidationError):
        _settings(model_routes_by_purpose={"EMBEDDINGS": "some-route"})


def test_fallbacks_are_selected_keys_too() -> None:
    settings = _settings(
        model_routes_by_purpose={"SQL_GENERATION": "strong-route"},
        model_route_fallbacks="backup-route",
    )
    assert settings.selected_model_route_keys == {
        "default-route",
        "strong-route",
        "backup-route",
    }


# ---------------------------------------------------------------------------
# Callers pick the purpose's route from the organization's approved rows
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> UUID:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org.id


def _row(
    org_id: UUID,
    key: str,
    capabilities: tuple[str, ...] = _BOTH,
    status: str = "APPROVED",
) -> ModelRouteConfiguration:
    return ModelRouteConfiguration(
        organization_id=org_id,
        route_key=key,
        version=1,
        status=status,
        display_name=f"Route {key}",
        provider_type="OPENAI",
        model_id="gpt-test",
        endpoint_alias="test-endpoint",
        credential_reference="vault://model-key",
        data_residency="US",
        retention_policy="ZERO_RETENTION",
        capabilities=list(capabilities),
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
        fingerprint="a" * 64,
        created_by="maker",
        approved_by="checker",
        approved_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_ask_tries_the_sql_purpose_route_first(session: AsyncSession) -> None:
    org_id = await _org(session)
    session.add_all([_row(org_id, "default-route"), _row(org_id, "strong-route")])
    await session.flush()
    orchestrator = GovernedAgentOrchestrator(
        _settings(
            model_routes_by_purpose={"SQL_GENERATION": "strong-route"},
            model_route_fallbacks="default-route",
        )
    )
    routes = await orchestrator._approved_model_routes(session, org_id)
    assert [route.route_key for route in routes] == ["strong-route", "default-route"]


@pytest.mark.asyncio
async def test_classification_callers_use_the_classification_route(
    session: AsyncSession,
) -> None:
    org_id = await _org(session)
    session.add_all([_row(org_id, "default-route"), _row(org_id, "cheap-route")])
    await session.flush()
    settings = _settings(model_routes_by_purpose={"CLASSIFICATION": "cheap-route"})

    inference = await approved_classification_route(session, org_id, settings)
    drafting = await approved_drafting_route(session, org_id, settings)
    assert inference is not None
    assert inference.route_key == "cheap-route"
    assert drafting.route_key == "cheap-route"


@pytest.mark.asyncio
async def test_a_purpose_route_still_needs_the_capability(session: AsyncSession) -> None:
    org_id = await _org(session)
    session.add(_row(org_id, "sql-only-route", capabilities=("SQL_GENERATION",)))
    await session.flush()
    settings = _settings(model_routes_by_purpose={"CLASSIFICATION": "sql-only-route"})

    assert await approved_classification_route(session, org_id, settings) is None
    with pytest.raises(ColumnDraftModelUnavailable, match="CLASSIFICATION"):
        await approved_drafting_route(session, org_id, settings)


@pytest.mark.asyncio
async def test_a_purpose_route_still_needs_approval(session: AsyncSession) -> None:
    org_id = await _org(session)
    session.add(_row(org_id, "pending-route", status="PENDING_REVIEW"))
    await session.flush()
    settings = _settings(model_routes_by_purpose={"CLASSIFICATION": "pending-route"})
    assert await approved_classification_route(session, org_id, settings) is None


def test_a_purpose_route_reads_as_selected_and_ready() -> None:
    settings = _settings(model_routes_by_purpose={"CLASSIFICATION": "cheap-route"})
    row = _row(uuid4(), "cheap-route")
    row.id = uuid4()
    row.created_at = row.updated_at = datetime.now(UTC)
    read = _route_read(row, settings)
    assert read.selected_by_runtime is True
    # Its credential is a vault reference the default resolver cannot read here, so
    # the status stops at the adapter check -- past APPROVED_NOT_SELECTED either way.
    assert read.activation_status in {"READY", "ADAPTER_REGISTRATION_REQUIRED"}


# ---------------------------------------------------------------------------
# The gateway admits exactly the selected keys
# ---------------------------------------------------------------------------


def _approved(key: str) -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key=key,
        provider_type="OPENAI",
        model_id="gpt-test",
        endpoint_alias="test-endpoint",
        credential_reference="vault://model-key",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


def _gateway(settings: Settings) -> ProviderNeutralModelGateway:
    resolver = SecretResolver(
        settings,
        {"vault": StaticTestSecretProvider({("model-key", None): ResolvedSecret("secret")})},
    )
    provider = DeterministicTestProvider(
        {"sql": "SELECT 1", "confidence": 0.9, "rationale_codes": ["X"]}
    )
    return ProviderNeutralModelGateway(settings, {"OPENAI": provider}, resolver)


@pytest.mark.asyncio
async def test_the_gateway_admits_a_purpose_route_and_nothing_unselected(
    session: AsyncSession,
) -> None:
    settings = _settings(model_routes_by_purpose={"SQL_GENERATION": "strong-route"})
    gateway = _gateway(settings)
    output, evidence = await gateway.structured_completion(
        session=session,
        organization_id=uuid4(),
        route=_approved("strong-route"),
        system_instruction="Generate SQL",
        payload={},
        output_schema=SqlGenerationOutput,
    )
    assert evidence.route == "strong-route"
    with pytest.raises(ModelRouteNotApproved):
        await gateway.structured_completion(
            session=session,
            organization_id=uuid4(),
            route=_approved("some-other-route"),
            system_instruction="Generate SQL",
            payload={},
            output_schema=SqlGenerationOutput,
        )
