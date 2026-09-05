"""Multi-route provider failover (2026-09-03).

Two layers, matching how the fix is built:

* **Config-level unit test.** `Settings.model_route_fallback_keys` parses the
  comma-separated `AIDA_MODEL_ROUTE_FALLBACKS` env var into an ordered,
  deduplicated list -- with the primary `model_route` filtered out so the
  same key appearing in both settings doesn't cost a doubled retry.

* **Route-selection unit test.** `GovernedAgentOrchestrator._approved_model_routes`
  returns the ordered list of APPROVED routes to try (primary first, then
  fallbacks in preference order), and silently skips unapproved / disabled /
  uncapable entries -- so revoking a fallback route via governance is a
  no-op for callers that had it in their fallback list, not an outage.

Deliberately does NOT cover the end-to-end orchestrator iteration behavior
(which needs a full retrieval + planning scenario). That path is exercised by
existing orchestrator integration tests once they gain a two-route fixture,
or a purpose-built end-to-end test in a follow-up.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    ModelCallEvidence,
    ModelGatewayError,
    SqlGenerationOutput,
)
from aida.models import ModelRouteConfiguration, Organization

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 1. Config-level: `Settings.model_route_fallback_keys` parses correctly
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    """Build a Settings with the required-for-tests defaults locked in
    (matches how `test_config.py` constructs test-time Settings)."""
    defaults = {
        "environment": "test",
        "identity_provider": "development",
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_fallback_keys_are_empty_when_setting_is_none() -> None:
    s = _settings(model_route=None, model_route_fallbacks=None)
    assert s.model_route_fallback_keys == []


def test_fallback_keys_are_empty_when_setting_is_empty_string() -> None:
    s = _settings(model_route=None, model_route_fallbacks="")
    assert s.model_route_fallback_keys == []


def test_fallback_keys_preserve_order_and_deduplicate() -> None:
    s = _settings(
        model_route="primary-route",
        model_route_fallbacks="secondary-route, tertiary-route ,secondary-route",
    )
    assert s.model_route_fallback_keys == ["secondary-route", "tertiary-route"]


def test_fallback_keys_filter_out_the_primary() -> None:
    """If someone lists the primary in the fallback list too, it is silently
    dropped -- iteration always tries the primary first anyway; duplicating
    would double the retry cost on a real outage for no benefit."""
    s = _settings(
        model_route="primary-route",
        model_route_fallbacks="primary-route,alt-route",
    )
    assert s.model_route_fallback_keys == ["alt-route"]


def test_fallback_keys_trim_whitespace_and_skip_empty_entries() -> None:
    s = _settings(
        model_route=None,
        model_route_fallbacks="  a , , b ,,  c  ",
    )
    assert s.model_route_fallback_keys == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 2. Route selection: `_approved_model_routes` returns the ordered list
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


def _route_row(
    *,
    org_id,
    key: str,
    provider: str = "OPENAI",
    status: str = "APPROVED",
    capabilities: tuple[str, ...] = ("SQL_GENERATION",),
    credential_reference: str = "env://OPENAI_API_KEY",
) -> ModelRouteConfiguration:
    """Constructor kept in lockstep with `src/aida/models.py::ModelRouteConfiguration`
    -- corrected 2026-09-03 after an adversarial pass caught that the original
    used four field names that don't exist on the model (`residency_region`,
    `retention_days`, `input_cost_per_million`, `output_cost_per_million`) and
    omitted five NOT NULL columns it does have (`display_name`, `data_residency`,
    `retention_policy`, `fingerprint`, `created_by`). Cross-checked against
    `tests/test_ai_governance.py`'s working usage of the same model."""
    return ModelRouteConfiguration(
        organization_id=org_id,
        route_key=key,
        version=1,
        status=status,
        display_name=f"Test route {key}",
        provider_type=provider,
        model_id="gpt-4o-mini",
        endpoint_alias="test-endpoint",
        credential_reference=credential_reference,
        data_residency="US",
        retention_policy="ZERO_RETENTION",
        capabilities=list(capabilities),
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
        fingerprint="a" * 64,
        created_by="test-maker",
        approved_by="test-checker",
        approved_at=datetime.now(UTC),
    )


async def test_approved_model_routes_returns_primary_only_when_no_fallback_configured(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    session.add(_route_row(org_id=org.id, key="primary-route"))
    await session.flush()

    settings = _settings(
        model_generation_enabled=True,
        model_route="primary-route",
        model_route_fallbacks=None,
        openai_api_key="test-key",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    routes = await orchestrator._approved_model_routes(session, org.id)
    assert [r.route_key for r in routes] == ["primary-route"]


async def test_approved_model_routes_returns_primary_then_fallbacks_in_order(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    session.add(_route_row(org_id=org.id, key="primary-route", provider="OPENAI"))
    session.add(_route_row(org_id=org.id, key="fallback-a", provider="GOOGLE_GEMINI"))
    session.add(_route_row(org_id=org.id, key="fallback-b", provider="OPENAI"))
    await session.flush()

    settings = _settings(
        model_generation_enabled=True,
        model_route="primary-route",
        model_route_fallbacks="fallback-a,fallback-b",
        openai_api_key="test-key",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    routes = await orchestrator._approved_model_routes(session, org.id)
    assert [r.route_key for r in routes] == [
        "primary-route",
        "fallback-a",
        "fallback-b",
    ]


async def test_approved_model_routes_silently_skips_unapproved_fallback(
    session: AsyncSession,
) -> None:
    """A revoked or draft fallback is skipped rather than raising -- so
    governance can retire a route without breaking callers that had it in
    their fallback list."""
    org = await _org(session)
    session.add(_route_row(org_id=org.id, key="primary-route"))
    session.add(_route_row(org_id=org.id, key="revoked-fallback", status="REVOKED"))
    session.add(_route_row(org_id=org.id, key="good-fallback"))
    await session.flush()

    settings = _settings(
        model_generation_enabled=True,
        model_route="primary-route",
        model_route_fallbacks="revoked-fallback,good-fallback",
        openai_api_key="test-key",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    routes = await orchestrator._approved_model_routes(session, org.id)
    # revoked-fallback is missing; good-fallback still reached.
    assert [r.route_key for r in routes] == ["primary-route", "good-fallback"]


async def test_approved_model_routes_returns_empty_when_nothing_configured(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    # Route exists in the DB but neither setting names it.
    session.add(_route_row(org_id=org.id, key="unused-route"))
    await session.flush()

    settings = _settings(
        model_generation_enabled=False,
        model_route=None,
        model_route_fallbacks=None,
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    routes = await orchestrator._approved_model_routes(session, org.id)
    assert routes == []


async def test_approved_model_routes_skips_route_without_sql_generation_capability(
    session: AsyncSession,
) -> None:
    """A route approved for embeddings-only (say) is not usable for SQL
    generation and is silently dropped from the returned list -- same rule
    the singular `_approved_model_route` has enforced since AU-5."""
    org = await _org(session)
    session.add(
        _route_row(
            org_id=org.id, key="embeddings-only", capabilities=("EMBEDDINGS",)
        )
    )
    session.add(_route_row(org_id=org.id, key="sql-fallback"))
    await session.flush()

    settings = _settings(
        model_generation_enabled=True,
        model_route="embeddings-only",
        model_route_fallbacks="sql-fallback",
        openai_api_key="test-key",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    routes = await orchestrator._approved_model_routes(session, org.id)
    assert [r.route_key for r in routes] == ["sql-fallback"]



# ---------------------------------------------------------------------------
# 3. E2E: `_generate_with_fallback` iterates approved routes correctly
#
# Isolated at the method level -- swaps `orchestrator.model_gateway` for a
# deterministic queue-of-responses mock -- so the fallback loop's semantics
# (retryable vs non-retryable, attempt bookkeeping, chain-on-exception) are
# testable without standing up a full retrieval + planning + gateway scenario.
# ---------------------------------------------------------------------------



def _approved_route(key: str, provider: str = "OPENAI") -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key=key,
        provider_type=provider,
        model_id="test-model",
        endpoint_alias="",
        credential_reference=f"env://{provider}_API_KEY",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


def _fake_evidence(route_key: str) -> ModelCallEvidence:
    return ModelCallEvidence(
        route=route_key,
        provider_type="fake",
        model_id="test-model",
        endpoint_alias="",
        input_fingerprint="fp-in",
        output_fingerprint="fp-out",
        input_size_bytes=1,
        output_size_bytes=1,
        schema_name="SqlGenerationOutput",
    )


def _fake_output() -> SqlGenerationOutput:
    return SqlGenerationOutput(sql="SELECT 1", confidence=0.9, rationale_codes=["FAKE"])


class _QueuedGateway:
    """A stand-in for `ProviderNeutralModelGateway.structured_completion` that
    returns / raises each element of `responses` in order, keyed by call
    ordinal, not by route -- exactly what the fallback loop needs to
    reproduce a "primary fails, secondary succeeds" chain deterministically."""

    def __init__(self, responses: list[Any]) -> None:
        # Each entry is either a ModelGatewayError instance (raised) or a
        # tuple (output, evidence) (returned).
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def structured_completion(self, **kwargs: Any) -> Any:
        self.calls.append(
            {
                "route_key": kwargs["route"].route_key,
                "provider_type": kwargs["route"].provider_type,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


async def test_generate_with_fallback_primary_succeeds_no_attempts_recorded_extra(
    session: AsyncSession,
) -> None:
    """Baseline: primary returns cleanly on the first call -- one success
    entry in attempts, no fallback fired, no failure entries."""
    settings = _settings(
        model_generation_enabled=True,
        model_route="primary",
        openai_api_key="test",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    orchestrator.model_gateway = _QueuedGateway(
        [(_fake_output(), _fake_evidence("primary"))]
    )
    org = await _org(session)
    output, evidence, attempts = await orchestrator._generate_with_fallback(
        session=session,
        organization_id=org.id,
        approved_routes=[_approved_route("primary")],
        system_instruction="ignored by mock",
        payload={},
    )
    assert output.sql == "SELECT 1"
    assert evidence.route == "primary"
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "SUCCEEDED"
    assert attempts[0]["route_key"] == "primary"


async def test_generate_with_fallback_primary_429_then_fallback_succeeds(
    session: AsyncSession,
) -> None:
    """The scenario the operator asked about: primary is throttled, the
    fallback route answers, and the chain is recorded so an auditor can
    see both attempts."""
    settings = _settings(
        model_generation_enabled=True,
        model_route="primary",
        model_route_fallbacks="fallback",
        openai_api_key="test",
        gemini_api_key="test",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    orchestrator.model_gateway = _QueuedGateway(
        [
            ModelGatewayError(
                "model provider request failed with HTTP 429",
                provider_status_code=429,
            ),
            (_fake_output(), _fake_evidence("fallback")),
        ]
    )
    org = await _org(session)
    output, evidence, attempts = await orchestrator._generate_with_fallback(
        session=session,
        organization_id=org.id,
        approved_routes=[
            _approved_route("primary", provider="OPENAI"),
            _approved_route("fallback", provider="GOOGLE_GEMINI"),
        ],
        system_instruction="ignored",
        payload={},
    )
    assert output.sql == "SELECT 1"
    assert evidence.route == "fallback"
    assert [a["outcome"] for a in attempts] == ["FAILED", "SUCCEEDED"]
    assert attempts[0]["route_key"] == "primary"
    assert attempts[0]["provider_status_code"] == 429
    assert attempts[1]["route_key"] == "fallback"


async def test_generate_with_fallback_all_routes_429_raises_with_attempts(
    session: AsyncSession,
) -> None:
    """Both routes 429 -- iteration exhausts, ModelGatewayError re-raises
    carrying the full attempt chain on the .model_call_attempts attribute so
    the caller can persist it before recording the refusal."""
    settings = _settings(
        model_generation_enabled=True,
        model_route="primary",
        model_route_fallbacks="fallback",
        openai_api_key="test",
        gemini_api_key="test",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    orchestrator.model_gateway = _QueuedGateway(
        [
            ModelGatewayError("primary 429", provider_status_code=429),
            ModelGatewayError("fallback 429", provider_status_code=429),
        ]
    )
    org = await _org(session)
    with pytest.raises(ModelGatewayError) as excinfo:
        await orchestrator._generate_with_fallback(
            session=session,
            organization_id=org.id,
            approved_routes=[
                _approved_route("primary"),
                _approved_route("fallback"),
            ],
            system_instruction="ignored",
            payload={},
        )
    assert excinfo.value.provider_status_code == 429
    attached = getattr(excinfo.value, "model_call_attempts", None)
    assert attached is not None
    assert [a["outcome"] for a in attached] == ["FAILED", "FAILED"]
    assert [a["route_key"] for a in attached] == ["primary", "fallback"]


async def test_generate_with_fallback_401_on_primary_does_not_fall_back(
    session: AsyncSession,
) -> None:
    """401 (broken credential) is NOT a busy provider -- fallback would just
    move the failure. The iteration must short-circuit and re-raise on the
    primary, with only ONE attempt recorded."""
    settings = _settings(
        model_generation_enabled=True,
        model_route="primary",
        model_route_fallbacks="fallback",
        openai_api_key="test",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    # If the loop wrongly tried the fallback, the mock would run out of
    # responses and pop from an empty list -- so the assert here catches
    # both the wrong-status behavior and any silent fallback on 401.
    orchestrator.model_gateway = _QueuedGateway(
        [ModelGatewayError("primary 401", provider_status_code=401)]
    )
    org = await _org(session)
    with pytest.raises(ModelGatewayError) as excinfo:
        await orchestrator._generate_with_fallback(
            session=session,
            organization_id=org.id,
            approved_routes=[
                _approved_route("primary"),
                _approved_route("fallback"),
            ],
            system_instruction="ignored",
            payload={},
        )
    assert excinfo.value.provider_status_code == 401
    attached = getattr(excinfo.value, "model_call_attempts", None)
    assert attached is not None
    assert len(attached) == 1
    assert attached[0]["route_key"] == "primary"


async def test_generate_with_fallback_empty_routes_raises(
    session: AsyncSession,
) -> None:
    """Defensive: an empty approved-routes list raises `ModelGatewayError`
    with the same "no approved model route" message the singular path
    surfaced before this refactor."""
    settings = _settings(
        model_generation_enabled=False,
        model_route=None,
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    orchestrator.model_gateway = _QueuedGateway([])
    org = await _org(session)
    with pytest.raises(ModelGatewayError) as excinfo:
        await orchestrator._generate_with_fallback(
            session=session,
            organization_id=org.id,
            approved_routes=[],
            system_instruction="ignored",
            payload={},
        )
    assert "no approved model route" in str(excinfo.value).lower()


async def test_generate_with_fallback_502_and_503_are_also_retryable(
    session: AsyncSession,
) -> None:
    """The retryable set covers 429/502/503/504, not just 429. Prove 502 on
    primary + 503 on fallback still exhausts through, and a following third
    route that succeeds is reached."""
    settings = _settings(
        model_generation_enabled=True,
        model_route="primary",
        model_route_fallbacks="fallback-a,fallback-b",
        openai_api_key="test",
        gemini_api_key="test",
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    orchestrator.model_gateway = _QueuedGateway(
        [
            ModelGatewayError("primary 502", provider_status_code=502),
            ModelGatewayError("fallback-a 503", provider_status_code=503),
            (_fake_output(), _fake_evidence("fallback-b")),
        ]
    )
    org = await _org(session)
    output, evidence, attempts = await orchestrator._generate_with_fallback(
        session=session,
        organization_id=org.id,
        approved_routes=[
            _approved_route("primary"),
            _approved_route("fallback-a"),
            _approved_route("fallback-b"),
        ],
        system_instruction="ignored",
        payload={},
    )
    assert output.sql == "SELECT 1"
    assert evidence.route == "fallback-b"
    assert [a["outcome"] for a in attempts] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert [a["provider_status_code"] for a in attempts[:2]] == [502, 503]
