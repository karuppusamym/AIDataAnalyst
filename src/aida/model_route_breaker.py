"""R11-MP04: stop paying a timeout per question while a model route is down.

`GovernedAgentOrchestrator._generate_with_fallback` walks the approved routes
in preference order and falls through a failing one. Without memory between
runs, every question during a provider outage first waits on the broken
primary -- up to the route's timeout times the adapter's retry attempts --
before the fallback answers. This module is that memory: a circuit breaker per
(organization, route).

* **CLOSED** -- calls go through. `failure_threshold` consecutive failures
  that say something about the route (a fallback-worthy HTTP status, a
  timeout, a network failure) open it.
* **OPEN** -- the route is skipped for `cooldown_seconds`, and the skip is
  recorded in the run's `model_call_attempts` as `SKIPPED_CIRCUIT_OPEN`.
* **HALF-OPEN** -- once the cool-down has passed, the next run may try the
  route once. Success closes it; failure opens it for another cool-down.

What it deliberately does not do:

* **Change which routes may run.** It only skips routes that
  `_approved_model_routes` already returned; it never adds one. Governance,
  the kill switch and the approved fallback list are untouched.
* **Count a failure that is about the request.** A 400, 401 or 403, an
  invalid structured answer or an engaged kill switch is not a route outage,
  and the fallback loop already stops on those.
* **Share state across processes.** The state is per worker process, so each
  replica learns about an outage from its own first few failures. That bounds
  the cost of an outage to `failure_threshold` slow calls per replica per
  cool-down, which is the saving that matters, without a new shared store.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from uuid import UUID

#: HTTP statuses that say the route, not the request, is failing. The same set
#: the fallback loop falls through on.
ROUTE_FAILURE_STATUSES = frozenset({404, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class _RouteState:
    consecutive_failures: int = 0
    opened_at: float | None = None


class RouteCircuitBreaker:
    """Per-(organization, route) breaker. Thread-safe; no I/O."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._states: dict[tuple[UUID, str], _RouteState] = {}

    def seconds_until_retry(
        self, organization_id: UUID, route_key: str, *, cooldown_seconds: float
    ) -> float | None:
        """Seconds left before this route may be tried again, or None if it may
        be tried now (closed, or open with the cool-down passed: half-open)."""
        with self._lock:
            state = self._states.get((organization_id, route_key))
            if state is None or state.opened_at is None:
                return None
            remaining = state.opened_at + cooldown_seconds - self._clock()
            return remaining if remaining > 0 else None

    def record_success(self, organization_id: UUID, route_key: str) -> None:
        with self._lock:
            self._states.pop((organization_id, route_key), None)

    def record_failure(
        self, organization_id: UUID, route_key: str, *, failure_threshold: int
    ) -> bool:
        """Count one route failure. Returns True when this failure opened (or,
        from half-open, re-opened) the breaker."""
        with self._lock:
            state = self._states.setdefault((organization_id, route_key), _RouteState())
            state.consecutive_failures += 1
            if state.opened_at is not None or state.consecutive_failures >= failure_threshold:
                state.opened_at = self._clock()
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


def is_route_failure(provider_status_code: int | None, *, generic_gateway_error: bool) -> bool:
    """Whether a failed call says the route is unhealthy.

    A fallback-worthy status does. So does a failure with no status that is a
    plain `ModelGatewayError` -- a timeout or a network failure. Subclasses
    (route not approved, invalid output, kill switch engaged) and any other
    status are about the request or a deliberate refusal, not the route.
    """
    if provider_status_code is not None:
        return provider_status_code in ROUTE_FAILURE_STATUSES
    return generic_gateway_error


#: The process-wide breaker the orchestrator uses. Orchestrators are built per
#: request (`api.py`, `mcp_server.py`), so the state has to outlive them.
ROUTE_BREAKER = RouteCircuitBreaker()
