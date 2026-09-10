"""AG-10 / AR-05: runtime enforcement of an agent contract's budget caps.

`AgentContract` has carried `daily_token_cap`, `per_run_token_cap` and
`wall_clock_seconds_cap` since AG-10 shipped. They were validated on write,
stored, and read by nothing: the 2026-09-09 architecture review
(`Docs/10-architecture/15-agent-architecture-critical-review.md`, AR-05)
found no runtime consumer of any of them. A declared cap that nothing
enforces is worse than no cap, because it is quoted in a governance dossier
as though it bounds something.

This module is the consumer. Three caps, three different enforcement shapes,
because they answer different questions:

* **`per_run_token_cap`** -- checked twice. Once *before* the provider call
  against the input estimate, which is knowable in advance and is what
  actually gets billed on a refused-mid-stream request; once *after* against
  input + output, which is the number the contract means. The second check
  cannot prevent the spend it detects; it fails the run so the overrun is
  attributable rather than silent.
* **`daily_token_cap`** -- reserved before the call and reconciled after,
  through `AgentBudgetWindow`. The reservation is a conditional UPDATE that
  carries the cap in its own `WHERE`, so two concurrent runs cannot both pass
  a cap they jointly break. This is the "atomic budget reservation" AR-05
  asks for; it is atomic at the row, which is where the race is.
* **`wall_clock_seconds_cap`** -- a pure comparison against the run's own
  start, checked at each point the runtime is about to do something
  expensive. It bounds when work *starts*, not how long an already-issued
  provider call takes; the provider call has its own timeout
  (`Settings.model_timeout_seconds`, `route.timeout_seconds`).

**What this does not do, stated plainly.** Every number here is an
*estimate*, by the same 4-bytes-per-token heuristic the gateway uses. No
provider adapter in this codebase reports billable usage, so these caps bound
a modelled quantity. When an adapter starts returning real usage, the
reconciliation step below is the single place that has to change: pass the
provider's number to `reconcile_run_budget` instead of the estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, case, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AgentBudgetWindow, AgentContract

#: Reason codes, carried the same way `agent_contracts`' are: written verbatim
#: into the DENIED audit row's `details.reason`.
REASON_DAILY_TOKEN_CAP = "agent_daily_token_cap_exhausted"  # noqa: S105 -- reason code
REASON_PER_RUN_TOKEN_CAP = "agent_per_run_token_cap_exceeded"  # noqa: S105 -- reason code
REASON_WALL_CLOCK_CAP = "agent_wall_clock_cap_exceeded"


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """What one run took out of its agent's daily window.

    `amount` is what was actually added to `AgentBudgetWindow.reserved_tokens`
    and therefore what `reconcile_run_budget` must give back. `window_id` is
    `None` when the contract declares no daily cap -- there is nothing to
    reserve against, and no row is created for an agent nobody has budgeted.

    `known_input_tokens` is the part of the reservation the platform can still
    account for when a generation fails: the payload it demonstrably sent,
    across every attempt it planned. The rest of `amount` is an allowance for
    output that a failed run never produced. `settle_unresolved_run_budget`
    uses the split.
    """

    window_id: UUID | None
    amount: int
    daily_cap: int | None = None
    known_input_tokens: int = 0

    @property
    def is_reserved(self) -> bool:
        return self.window_id is not None and self.amount > 0


class AgentBudgetExceeded(RuntimeError):
    """A contract cap refused this run. `reason_code` is stable and
    operator-facing, matching `agent_contracts.AgentPolicyRejected`'s shape so
    the orchestrator can persist either through one rejection path."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def wall_clock_violation(
    contract: AgentContract | None, *, started_at: datetime, now: datetime
) -> str | None:
    """`REASON_WALL_CLOCK_CAP` when this run has already been going longer
    than its contract allows, else `None`.

    Naive timestamps are read as UTC rather than refused: `AgentRun.created_at`
    comes back naive from some drivers, and treating that as "no cap applies"
    would make the cap depend on the database driver.
    """
    if contract is None or contract.wall_clock_seconds_cap is None:
        return None
    start = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=UTC)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if (moment - start).total_seconds() > contract.wall_clock_seconds_cap:
        return REASON_WALL_CLOCK_CAP
    return None


def per_run_violation(contract: AgentContract | None, *, tokens: int) -> str | None:
    """`REASON_PER_RUN_TOKEN_CAP` when `tokens` breaks the per-run cap."""
    if contract is None or contract.per_run_token_cap is None:
        return None
    if tokens > contract.per_run_token_cap:
        return REASON_PER_RUN_TOKEN_CAP
    return None


async def _ensure_window(
    session: AsyncSession,
    *,
    organization_id: UUID,
    ai_asset_version_id: UUID,
    window_date: date,
) -> UUID:
    """The window row's id, creating it if this is the day's first run.

    The insert races against every other run starting the same day at the same
    moment; the unique constraint settles it and the loser re-reads. Wrapped in
    a savepoint so a lost race does not poison the caller's transaction.
    """
    existing = await session.scalar(
        select(AgentBudgetWindow.id).where(
            AgentBudgetWindow.organization_id == organization_id,
            AgentBudgetWindow.ai_asset_version_id == ai_asset_version_id,
            AgentBudgetWindow.window_date == window_date,
        )
    )
    if existing is not None:
        return existing
    window_id = uuid4()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(AgentBudgetWindow).values(
                    id=window_id,
                    organization_id=organization_id,
                    ai_asset_version_id=ai_asset_version_id,
                    window_date=window_date,
                    reserved_tokens=0,
                    run_count=0,
                )
            )
        return window_id
    except IntegrityError:
        pass
    contended = await session.scalar(
        select(AgentBudgetWindow.id).where(
            AgentBudgetWindow.organization_id == organization_id,
            AgentBudgetWindow.ai_asset_version_id == ai_asset_version_id,
            AgentBudgetWindow.window_date == window_date,
        )
    )
    if contended is None:  # pragma: no cover -- the constraint said it exists
        raise AgentBudgetExceeded(REASON_DAILY_TOKEN_CAP)
    return contended


async def reserve_run_budget(
    session: AsyncSession,
    contract: AgentContract | None,
    *,
    estimated_input_tokens: int,
    estimated_output_tokens: int = 0,
    now: datetime | None = None,
) -> BudgetReservation:
    """Take this run's worst case out of the agent's daily window, or refuse.

    Reserves the *per-run cap* when one is declared, not the input estimate:
    the point of a reservation is to hold what the run could still cost, and
    the output has not been generated yet. With no per-run cap the input
    estimate is the only forward-looking number available, so it is used and
    the reconciliation step corrects it.

    Raises `AgentBudgetExceeded(REASON_DAILY_TOKEN_CAP)` when the conditional
    UPDATE matches no row, which -- given the row is known to exist by then --
    means the reservation would have taken the day past its cap.
    """
    if contract is None or contract.daily_token_cap is None:
        return BudgetReservation(window_id=None, amount=0)
    if estimated_input_tokens < 0 or estimated_output_tokens < 0:
        raise ValueError("token estimates must be nonnegative")
    planned = estimated_input_tokens + estimated_output_tokens
    violation = per_run_violation(contract, tokens=planned)
    if violation:
        raise AgentBudgetExceeded(violation)
    amount = max(1, contract.per_run_token_cap or planned)
    if amount > contract.daily_token_cap:
        # A single run that cannot fit inside the whole day never will; refuse
        # before touching the ledger rather than leaving a row at its cap.
        raise AgentBudgetExceeded(REASON_DAILY_TOKEN_CAP)
    moment = now or datetime.now(UTC)
    window_id = await _ensure_window(
        session,
        organization_id=contract.organization_id,
        ai_asset_version_id=contract.ai_asset_version_id,
        window_date=moment.date(),
    )
    result = await session.execute(
        update(AgentBudgetWindow)
        .where(
            AgentBudgetWindow.id == window_id,
            # The cap lives in the predicate, not in a Python comparison the
            # caller made against a value it read earlier. This is the whole
            # reason the table exists.
            AgentBudgetWindow.reserved_tokens + amount <= contract.daily_token_cap,
        )
        .values(
            reserved_tokens=AgentBudgetWindow.reserved_tokens + amount,
            run_count=AgentBudgetWindow.run_count + 1,
        )
    )
    # No row matched means the row exists (it was just ensured) but the cap
    # predicate refused -- this reservation would take the day past its cap.
    if cast("CursorResult[Any]", result).rowcount == 0:
        raise AgentBudgetExceeded(REASON_DAILY_TOKEN_CAP)
    return BudgetReservation(
        window_id=window_id,
        amount=amount,
        daily_cap=contract.daily_token_cap,
        known_input_tokens=estimated_input_tokens,
    )


async def reconcile_run_budget(
    session: AsyncSession,
    reservation: BudgetReservation,
    *,
    actual_tokens: int,
) -> None:
    """Replace a reservation with what the run actually cost.

    Called on the paths where the run's cost is *known*. A generation that
    failed does not know its cost, and goes through
    `settle_unresolved_run_budget` instead -- which calls this function with
    the input estimate rather than with zero, because a timeout can follow
    work the provider already billed.

    `actual_tokens` is an estimate today (see this module's docstring). It is
    the single place a provider-reported figure would enter.
    """
    if actual_tokens < 0:
        raise ValueError("actual tokens must be nonnegative")
    if not reservation.is_reserved:
        return
    delta = actual_tokens - reservation.amount
    if delta == 0:
        return
    adjusted = AgentBudgetWindow.reserved_tokens + delta
    await session.execute(
        update(AgentBudgetWindow)
        .where(AgentBudgetWindow.id == reservation.window_id)
        .values(
            # Clamped at zero in the statement rather than trusting the
            # arithmetic to stay inside the CHECK constraint: a double
            # reconciliation is a bug, but it should not take the caller's
            # transaction down with it.
            reserved_tokens=case((adjusted < 0, literal(0)), else_=adjusted)
        )
    )
    # Preserve overage evidence instead of hiding already-incurred spend.
    # Admission reserves the complete planned route chain before any call;
    # unexpected provider/estimator overruns must still fail visibly.
    used = await session.scalar(
        select(AgentBudgetWindow.reserved_tokens).where(
            AgentBudgetWindow.id == reservation.window_id
        )
    )
    if reservation.daily_cap is not None and int(used or 0) > reservation.daily_cap:
        raise AgentBudgetExceeded(REASON_DAILY_TOKEN_CAP)


async def settle_unresolved_run_budget(
    session: AsyncSession, reservation: BudgetReservation
) -> int:
    """Close out a reservation whose provider usage will never be known.

    The generation-failure path. A timeout or an unparseable response can
    follow work the provider already billed, so releasing the reservation in
    full would treat failure as free -- but *holding* it in full is worse than
    it looks: nothing ever reconciles it, so a run of provider timeouts
    consumes the whole day and the agent is locked out until the UTC window
    rolls over, having produced nothing. Both extremes are wrong for the same
    reason: they substitute a guess for the number the platform actually has.

    That number is the input. The payload was serialized and sent, once per
    planned attempt, and `estimate_payload_tokens` measured it -- so
    `known_input_tokens` is charged. The remainder of the reservation is an
    allowance for output the failed run never produced, and it is released.

    Returns what was charged, for the caller's evidence. Never raises: it runs
    while an exception is already unwinding, and masking that exception with a
    budget error would lose the reason the run failed. Charging can only lower
    the window -- `known_input_tokens` is always <= `amount`, since a reservation
    holds the per-run cap or input+output, and a run whose input alone broke the
    per-run cap was refused before it reserved -- so there is no overage to
    detect here.
    """
    if not reservation.is_reserved:
        return 0
    charged = max(0, min(reservation.known_input_tokens, reservation.amount))
    try:
        await reconcile_run_budget(session, reservation, actual_tokens=charged)
    except Exception:  # noqa: BLE001 -- never mask the failure being unwound
        return charged
    return charged


async def daily_reserved_tokens(
    session: AsyncSession,
    *,
    organization_id: UUID,
    ai_asset_version_id: UUID,
    window_date: date,
) -> int:
    """What one agent version has reserved on one UTC day. Read-only; exists
    for the operational surface and for tests, not for enforcement -- reading
    this and then deciding is exactly the race the conditional UPDATE avoids.
    """
    total = await session.scalar(
        select(AgentBudgetWindow.reserved_tokens).where(
            AgentBudgetWindow.organization_id == organization_id,
            AgentBudgetWindow.ai_asset_version_id == ai_asset_version_id,
            AgentBudgetWindow.window_date == window_date,
        )
    )
    return int(total or 0)
