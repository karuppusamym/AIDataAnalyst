"""MG-2: Kill-switch drill (module 15, model-gateway).

`Docs/20-modules/15-model-gateway.md` §7 declared five things about the kill switch:
scope (org-wide or per route), no effect on deterministic paths, latency ("full stop
within 60 seconds"), audited authorization for both engagement and reversal, and a
quarterly timed drill with retained evidence. Before this file, none of that was
real: `grep -rn "kill_switch" src/aida` found nothing but a docstring mention in
`compliance_packs.py`, and `model.kill_switch_engaged` / `.released` existed only as
catalog rows in `Docs/30-contracts/04-event-catalog.md` (confirmed by
`tests/test_event_catalog_gate.py`'s own "no current emitter" report) -- the module
doc's claim of "Designed, not drilled" overstated what existed; nothing was designed
in code either. "An undrilled kill switch is not a kill switch" (same doc, §7) --
this file is both the missing mechanism's tests and the drill itself.

The mechanism (`aida.models.KillSwitchState`, `aida.model_gateway.
kill_switch_blocking_state`, and the `engage_kill_switch` / `release_kill_switch`
governed endpoints in `aida.ai_governance_api`) is exercised through its real path
end to end: a FastAPI route handler function, a PlatformAdmin `SecurityContext`, and
a real (in-memory sqlite) database -- never a direct `KillSwitchState(engaged=True)`
row construction bypassing the API, which would prove nothing about the actual
control surface an operator uses under pressure.

`ProviderNeutralModelGateway.structured_completion` is the single choke point every
generation request passes through regardless of caller (`agent_orchestrator.py`'s SQL
generation, `semantic_inference.py`'s classification enrichment) -- see its own
docstring and module 15's charter, "The only path from Atlas to a language model".
The kill-switch check runs there, first, ahead of every other activation condition
(route approval, selection, credentials, adapter, budget), as a live per-request
query against `KillSwitchState` -- not a cached flag, not eventually consistent.

What this drill measures and what it does not: the timed assertion below bounds the
in-process latency between a committed kill-switch engagement and the next
`structured_completion` call observing it and raising `KillSwitchEngaged` -- which,
for an in-memory sqlite session with no network hop, is near-instant and asserted
under a generous 5s bound, leaving enormous margin against the tracker's 60s
requirement. It does NOT measure network or infrastructure propagation time to a
real deployed gateway process, connection-pool handoff under load, or a production
Postgres round trip -- those are out of scope for a test at this level and would
need a live-environment timed exercise to certify. That distinction is carried
into `Docs/60-delivery/03-tracker.md` row MG-2 rather than glossed over.
"""

from collections.abc import AsyncIterator
from itertools import count
from time import perf_counter
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.ai_governance_api import (
    engage_kill_switch,
    list_kill_switch_state,
    release_kill_switch,
)
from aida.config import Settings
from aida.db import Base
from aida.main import app
from aida.model_gateway import (
    ApprovedModelRoute,
    DeterministicTestProvider,
    KillSwitchEngaged,
    ProviderNeutralModelGateway,
    SqlGenerationOutput,
)
from aida.models import AuditEvent, Organization, OutboxEvent
from aida.schemas import KillSwitchEngageRequest, KillSwitchReleaseRequest
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider
from aida.security import require_roles
from aida.security_types import SecurityContext

# Generous bound for an in-process, in-memory-sqlite drill: the mechanism's own
# latency, with wide margin against the tracker's 60s production requirement --
# see the module docstring's "What this drill measures" section.
DRILL_LATENCY_BOUND_SECONDS = 5.0

# `AuditEvent.id` is a `BigInteger` autoincrement PK relying in production on
# Postgres's own sequence; sqlite only auto-populates a bare `INTEGER PRIMARY KEY`.
# Same workaround as `test_bulk_governance_decisions.py` / `test_relationship_
# intelligence_review.py`.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


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


def _operator_context(org: Organization, principal: str = "platform-operator-1") -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"PlatformAdmin"}),
    )


def _viewer_context(org: Organization) -> SecurityContext:
    return SecurityContext(
        principal_id="read-only-viewer",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Viewer"}),
    )


def _gateway_and_route(
    route_key: str = "bank-sql-primary",
) -> tuple[ProviderNeutralModelGateway, ApprovedModelRoute]:
    settings = Settings(
        model_generation_enabled=True,
        model_route=route_key,
        credential_provider="vault",
        _env_file=None,
    )
    resolver = SecretResolver(
        settings,
        {"vault": StaticTestSecretProvider({("model-key", None): ResolvedSecret("secret")})},
    )
    gateway = ProviderNeutralModelGateway(
        settings,
        {
            "OPENAI": DeterministicTestProvider(
                {
                    "sql": "SELECT account_id FROM retail.account",
                    "confidence": 0.9,
                    "rationale_codes": ["GROUNDED_IN_CATALOG"],
                    "referenced_evidence_ids": [],
                }
            )
        },
        resolver,
    )
    route = ApprovedModelRoute(
        route_key=route_key,
        provider_type="OPENAI",
        model_id="approved-model",
        endpoint_alias="private-endpoint",
        credential_reference="vault://model-key",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )
    return gateway, route


async def _generate(
    gateway: ProviderNeutralModelGateway,
    route: ApprovedModelRoute,
    session: AsyncSession,
    organization_id: object,
) -> str:
    output, _ = await gateway.structured_completion(
        session=session,
        organization_id=organization_id,  # type: ignore[arg-type]
        route=route,
        system_instruction="Generate read-only SQL",
        payload={"question": "count accounts"},
        output_schema=SqlGenerationOutput,
    )
    return output.sql


@pytest.mark.asyncio
async def test_kill_switch_drill_stops_generation_within_bound_with_retained_evidence(
    session: AsyncSession,
) -> None:
    """The drill itself, all five module-15 kill-switch requirements in one run:
    (1) normal generation-enabled baseline, (2) engagement through the real governed
    API path, (3) the very next generation request denied, (4) engagement-to-denial
    latency measured and bounded, (5) durable audit/outbox evidence retained and
    queryable -- plus reversal through the same authorization, also audited.
    """
    org = await _org(session)
    gateway, route = _gateway_and_route()
    context = _operator_context(org)

    # 1. Baseline: generation-enabled, no kill switch engaged.
    sql = await _generate(gateway, route, session, org.id)
    assert sql.startswith("SELECT")

    # 2. Engage through the real governed API path (not a direct DB write).
    engaged_state = await engage_kill_switch(
        org.id,
        KillSwitchEngageRequest(reason="MG-2 quarterly drill", route_key=None),
        context=context,
        session=session,
    )
    assert engaged_state.engaged is True
    assert engaged_state.scope == "ORGANIZATION"
    assert engaged_state.engaged_by == "platform-operator-1"
    assert engaged_state.engaged_at is not None

    # 3 & 4. Immediately attempt generation; measure engagement-to-denial latency.
    start = perf_counter()
    with pytest.raises(KillSwitchEngaged, match="kill switch engaged"):
        await _generate(gateway, route, session, org.id)
    elapsed = perf_counter() - start

    assert elapsed < DRILL_LATENCY_BOUND_SECONDS, (
        f"kill-switch enforcement took {elapsed:.3f}s in-process; expected well "
        f"under the {DRILL_LATENCY_BOUND_SECONDS}s test bound (itself far under "
        f"the tracker's 60s production requirement)"
    )

    # 5. Durable evidence: queryable audit + outbox rows, not just in-memory state.
    audit_rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "model.kill_switch_engage")
        )
    ).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].outcome == "SUCCESS"
    assert audit_rows[0].principal_id == "platform-operator-1"
    assert audit_rows[0].details["reason"] == "MG-2 quarterly drill"
    assert audit_rows[0].details["scope"] == "*"

    outbox_rows = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "model.kill_switch_engaged")
        )
    ).all()
    assert len(outbox_rows) == 1
    assert outbox_rows[0].payload["scope"] == "*"
    assert outbox_rows[0].payload["actor"] == "platform-operator-1"
    assert outbox_rows[0].payload["reason"] == "MG-2 quarterly drill"

    # Reversal requires the same authorization and is itself audited (§7 "Reversal").
    released_state = await release_kill_switch(
        org.id,
        KillSwitchReleaseRequest(reason="drill complete", route_key=None),
        context=context,
        session=session,
    )
    assert released_state.engaged is False
    assert released_state.released_by == "platform-operator-1"

    sql_after_release = await _generate(gateway, route, session, org.id)
    assert sql_after_release.startswith("SELECT")

    release_audit_rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "model.kill_switch_release")
        )
    ).all()
    assert len(release_audit_rows) == 1
    release_outbox_rows = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "model.kill_switch_released")
        )
    ).all()
    assert len(release_outbox_rows) == 1


@pytest.mark.asyncio
async def test_kill_switch_scoped_to_one_route_leaves_others_generating(
    session: AsyncSession,
) -> None:
    """Scope table row 1: "Halts all model traffic, organization-wide OR PER ROUTE" --
    engaging a route-scoped switch must not halt an unrelated route.
    """
    org = await _org(session)
    context = _operator_context(org)
    gateway_a, route_a = _gateway_and_route("route-a")
    gateway_b, route_b = _gateway_and_route("route-b")

    await engage_kill_switch(
        org.id,
        KillSwitchEngageRequest(reason="route A misbehaving", route_key="route-a"),
        context=context,
        session=session,
    )

    with pytest.raises(KillSwitchEngaged):
        await _generate(gateway_a, route_a, session, org.id)

    # route-b's own gateway/settings select route-b, unaffected by route-a's switch.
    sql_b = await _generate(gateway_b, route_b, session, org.id)
    assert sql_b.startswith("SELECT")


@pytest.mark.asyncio
async def test_kill_switch_engage_denied_without_platform_admin_role(
    session: AsyncSession,
) -> None:
    """Authorization row: "Platform operator; audited" -- not any authenticated role."""
    org = await _org(session)
    dependency = require_roles("PlatformAdmin")
    with pytest.raises(Exception) as exc_info:
        await dependency(context=_viewer_context(org))
    assert getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_kill_switch_release_rejected_when_not_engaged(session: AsyncSession) -> None:
    org = await _org(session)
    context = _operator_context(org)
    with pytest.raises(Exception) as exc_info:
        await release_kill_switch(
            org.id,
            KillSwitchReleaseRequest(reason="nothing to release", route_key=None),
            context=context,
            session=session,
        )
    assert getattr(exc_info.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_list_kill_switch_state_reports_current_scopes(session: AsyncSession) -> None:
    org = await _org(session)
    context = _operator_context(org)
    await engage_kill_switch(
        org.id,
        KillSwitchEngageRequest(reason="org-wide drill", route_key=None),
        context=context,
        session=session,
    )
    await engage_kill_switch(
        org.id,
        KillSwitchEngageRequest(reason="route drill", route_key="route-a"),
        context=context,
        session=session,
    )

    states = await list_kill_switch_state(org.id, context=context, session=session)

    by_scope = {state.scope: state for state in states}
    assert by_scope["ORGANIZATION"].engaged is True
    assert by_scope["ROUTE"].route_key == "route-a"
    assert by_scope["ROUTE"].engaged is True


def test_kill_switch_endpoints_are_published() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/kill-switch/engage" in paths
    assert "/v1/organizations/{organization_id}/kill-switch/release" in paths
    assert "/v1/organizations/{organization_id}/kill-switch" in paths
