"""R11-B16: notice when an approved model route's model disappears upstream.

An approved route is a governed object: a human decided, through maker-checker,
that this provider and this model may generate answers for this organization.
Nothing about that decision expires when the provider retires the model, and
providers retire models on their own schedule.

The failure this exists for happened here on 2026-09-12. A route was approved
for `gemini-2.0-flash`, Google had retired it, and the route looked entirely
healthy: APPROVED, credentialed, selected. Every generated answer failed with
a 404 that nothing connected back to the route. Two fixes went in the same day
-- `scripts/seed_model_route.py` now asks the provider before drafting, and 404
joined the statuses that reach the approved fallback -- and **neither is
detection**. Registration-time validation says nothing about the months
afterwards, and a fallback that rescues an answer still leaves a dead primary
nobody has been told about.

Three decisions worth stating:

**It lists, it never infers.** Asking the provider for its model list is free;
generating a token is not. A health check that cost money per route per sweep
would be switched off, and a switched-off check is worse than none because it
still appears in the configuration.

**It never changes a route's status.** `APPROVED` records a human decision, and
a background sweep must not revoke one -- that would be the platform quietly
overruling its own governance. The check records *reachability* beside the
approval and leaves the approval alone. What an operator does about an
unreachable route is an operator's decision, made with the fallback already
covering them.

**REACHABLE answers one question, not two.** It means the provider still lists
the model, and nothing more. Verified on 2026-09-12: an OpenAI route whose
account returns `billing_not_active` for every generation still lists
`gpt-4o-mini` perfectly well, so it reads REACHABLE while no answer can be
generated through it. That is the correct reading -- the check exists to catch
a *retired model*, which is invisible until an answer fails, and an account or
quota problem is already visible the first time anyone asks a question. Reading
REACHABLE as "generation works" is the one way to misuse this signal.

**Unknown is not unreachable.** A network failure, an expired credential, a
rate limit or a provider this module cannot probe all mean "we could not tell",
and that is recorded as its own state. Reporting a route dead because the
provider was briefly unreachable would train an operator to ignore the signal
-- and the signal is the whole deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select

from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit
from aida.models import ModelRouteConfiguration
from aida.security_types import SecurityContext

_log = structlog.get_logger(__name__)

#: What a check concluded. `UNKNOWN` is a real answer and is never collapsed
#: into `UNREACHABLE`.
REACHABLE = "REACHABLE"
UNREACHABLE = "UNREACHABLE"
UNKNOWN = "UNKNOWN"

MODEL_ROUTE_HEALTH_PRINCIPAL = "system:model-route-health"

_last_run_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RouteReachability:
    """One route's answer, and the reason behind it."""

    route_key: str
    status: str
    detail: str


def _context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=MODEL_ROUTE_HEALTH_PRINCIPAL,
        principal_type="SERVICE",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )


async def _served_models(provider_type: str, settings: Settings) -> set[str] | None:
    """Every model id this provider will serve, or `None` when unknowable.

    `None` covers a provider this module cannot probe, an absent credential and
    any transport or HTTP failure -- all of which mean "we could not tell", not
    "the model is gone". An OpenAI account with inactive billing cannot list
    models either, and that must not read as a retired model.
    """
    try:
        if provider_type == "GOOGLE_GEMINI":
            key = settings.gemini_api_key
            if key is None:
                return None
            async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
                response = await client.get(
                    f"{settings.gemini_base_url}/models",
                    params={"key": key.get_secret_value()},
                )
            if response.status_code != 200:
                return None
            names: set[str] = set()
            for model in response.json().get("models", []):
                name = str(model.get("name", ""))
                names.add(name)
                names.add(name.removeprefix("models/"))
            return names
        if provider_type == "OPENAI":
            key = settings.openai_api_key
            if key is None:
                return None
            async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
                response = await client.get(
                    f"{settings.openai_base_url}/models",
                    headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                )
            if response.status_code != 200:
                return None
            return {str(model.get("id", "")) for model in response.json().get("data", [])}
    except Exception:  # noqa: BLE001 - an unreachable provider is UNKNOWN, not UNREACHABLE
        return None
    return None


async def check_route(route: ModelRouteConfiguration, settings: Settings) -> RouteReachability:
    """Whether this route's provider still serves its model."""
    served = await _served_models(route.provider_type, settings)
    if served is None:
        return RouteReachability(
            route_key=route.route_key,
            status=UNKNOWN,
            detail=f"could not list models for provider {route.provider_type}",
        )
    if route.model_id in served:
        return RouteReachability(
            route_key=route.route_key, status=REACHABLE, detail=route.model_id
        )
    return RouteReachability(
        route_key=route.route_key,
        status=UNREACHABLE,
        detail=(
            f"the provider no longer serves {route.model_id!r}; "
            "generation on this route fails until it is superseded"
        ),
    )


async def run_model_route_reachability_pass(
    settings: Settings, *, now: datetime | None = None
) -> int | None:
    """Scheduler entry: check every APPROVED route's model against its provider.

    Returns `None` when the pass was skipped (disabled or not yet due) and the
    number of routes checked when it ran, matching the shape of the other
    scheduled passes rather than inventing a third convention.

    Only a *change* of status is audited. A route that was unreachable an hour
    ago and is unreachable now is not news, and an audit trail that repeats
    itself hourly is one nobody reads.
    """
    from aida.db import session_factory

    global _last_run_at
    if not settings.model_route_health_enabled:
        return None
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.model_route_health_interval_seconds)
    if _last_run_at is not None and (effective_now - _last_run_at) < interval:
        return None

    checked = 0
    async with session_factory() as session:
        routes = list(
            (
                await session.scalars(
                    select(ModelRouteConfiguration)
                    .where(ModelRouteConfiguration.status == "APPROVED")
                    .order_by(ModelRouteConfiguration.route_key)
                    .limit(settings.model_route_health_batch_size)
                )
            ).all()
        )
        for route in routes:
            try:
                result = await check_route(route, settings)
            except Exception:  # noqa: BLE001 - one route must not lose the sweep
                _log.exception("model_route_health_failed", route_key=route.route_key)
                continue
            checked += 1
            changed = route.reachability_status != result.status
            route.reachability_status = result.status
            route.reachability_detail = result.detail[:1000]
            route.reachability_checked_at = effective_now
            if changed:
                # The status is recorded beside the approval and the approval is
                # left alone: a background sweep must not revoke what a human
                # decided through maker-checker.
                record_audit(
                    session,
                    _context(route.organization_id),
                    action="model_route.reachability_changed",
                    resource_type="model_route_configuration",
                    resource_id=str(route.id),
                    outcome="SUCCESS" if result.status == REACHABLE else "FAILURE",
                    correlation_id=get_correlation_id(),
                    details={
                        "route_key": route.route_key,
                        "provider_type": route.provider_type,
                        "status": result.status,
                        "detail": result.detail,
                    },
                )
                log = _log.warning if result.status == UNREACHABLE else _log.info
                log(
                    "model_route_reachability_changed",
                    route_key=route.route_key,
                    provider_type=route.provider_type,
                    status=result.status,
                    detail=result.detail,
                )
        await session.commit()

    _last_run_at = effective_now
    return checked


async def unreachable_route_summary(settings: Settings) -> dict[str, Any]:
    """A cheap, DB-only summary for `/health/ready`.

    Deliberately reads the recorded state rather than probing: a readiness
    scrape must not make a provider call, or the endpoint's latency and cost
    become the provider's to decide.
    """
    from aida.db import session_factory

    async with session_factory() as session:
        routes = list(
            (
                await session.scalars(
                    select(ModelRouteConfiguration).where(
                        ModelRouteConfiguration.status == "APPROVED"
                    )
                )
            ).all()
        )
    unreachable = [r.route_key for r in routes if r.reachability_status == UNREACHABLE]
    unchecked = [r.route_key for r in routes if r.reachability_status is None]
    return {
        "approved": len(routes),
        "unreachable": sorted(unreachable),
        "never_checked": len(unchecked),
        "enabled": settings.model_route_health_enabled,
    }
