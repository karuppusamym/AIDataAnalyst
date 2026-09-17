"""R11-FP17: per-tenant and per-source quotas, enforced atomically.

**What existed before this module, precisely.** Run *admission concurrency* and
nothing else. `fleet.reserve_analysis_run` locks the Organization then the
DataSource `FOR UPDATE` and refuses when the organization already has
`max_active_runs_per_organization` runs in flight, or when the source already
has one -- a literal `1` in the source, until `max_active_runs_per_datasource`
replaced it here. Everything else in `Settings` that looks like a limit is a
*bound* on one unit of work: `change_signal_processing_batch_size` bounds a
pass, `discovery_stream_batch_size` bounds a batch, `query_gateway_lob_max_
concurrent` bounds simultaneity, the `*_max_proposals_per_run` family bounds a
run. A bound says "not too much at once". A quota says "not too much per day",
which no amount of bounding gives you, because a bounded pass repeated a
thousand times consumes a thousand times the resource.

`AgentContract.daily_token_cap` is the one real quota that already existed, and
it is per agent contract, not per tenant and not per source. So this module
reuses its mechanism rather than inventing a second one.

**The mechanism, and why it is not a SELECT followed by an IF.**
`aida.agent_budget` wrote the reasoning once and it is the same here: two
callers that both read a day's consumption before either writes will both pass
a cap they jointly break, and no amount of care in the Python closes that
window. So the cap travels *inside* the UPDATE's `WHERE`:

    UPDATE ... SET used = used + :amount
     WHERE id = :window AND organization_id = :org
       AND used + :amount <= :cap

and a rowcount of zero from a row that is known to exist means the cap refused.
The database decides who fits, at the row, which is where the race is.

**INV-5 is restated in every predicate, and that is not redundancy.**
`SourceUsageWindow` rows are uniquely identified by their id, so matching on
`organization_id` as well buys nothing as a lookup. It buys the invariant: a
caller that passes a datasource id belonging to another tenant moves no row
instead of moving theirs. Tenant isolation is total (INV-5) means the boundary
is in the statement, not in the correctness of the caller.

**Fail closed, in the three ways that can actually happen here.**

* A refusal is a refusal. `QuotaRefused` is raised, not returned as a falsy
  value a caller can forget to check, and the caller's transaction has already
  been left unchanged for the dimension it was refused.
* A source consumption that succeeds followed by a tenant consumption that is
  refused releases the source's share before raising, so a refused request
  consumes nothing. The release is the same conditional UPDATE in reverse,
  clamped at zero. The order is deliberate: the source window moves first, so
  the common refusal -- one noisy source against its own cap -- has nothing to
  unwind.
* An amount of zero or less is a programming error and raises `ValueError`
  rather than silently succeeding. A metered call that thinks it consumed
  nothing has miscounted, and "nothing was consumed" must not be the way a
  quota is bypassed.

**No quota declared is not a quota of zero.** Every `*_daily_quota_*` setting
ships as `None`, meaning this estate has not declared one. `consume_quota` then
returns without touching the database -- no window row is created, no statement
is issued, and `fleet.reserve_analysis_run`'s call sequence is byte-for-byte
what it was. That is deliberate: a quota number is an operator's number, and
inventing one here would start refusing work on upgrade for a limit nobody
chose. What is *not* optional is the accumulation once a quota exists.

**`record_usage` is the same accumulation without a cap**, for the dimensions
that are metered for attribution rather than enforcement -- see
`aida.cost_metrics`, which uses it to carry per-source parser and model spend in
rows the database bounds instead of in a Prometheus label the cardinality does
not.

**Not money.** `used` counts the dimension's own unit: analysis runs, tokens,
parsed statements. `aida.cost_showback`'s `COST_BASIS` applies unchanged --
nothing in this platform meters a dollar, a connector-shaped `plan_cost` is not
comparable to a token, and neither is added to the other here or anywhere.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast
from uuid import UUID, uuid4

import structlog
from prometheus_client import Counter
from sqlalchemy import CursorResult, case, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import SourceUsageWindow, TenantUsageWindow

_log = structlog.get_logger(__name__)


class UsageDimension(StrEnum):
    """What a quota can be declared over. Closed, because it is a metric label.

    Every value here is something this platform can already count honestly at
    the moment it happens. There is deliberately no `DOLLARS` and no
    `PLAN_COST`: nothing meters a dollar, and `plan_cost` mixes bytes-scanned
    with a planner score across connectors, so a quota over it would refuse one
    tenant's work for a number that means something different than it does for
    the next tenant.
    """

    #: One admitted `AnalysisRun`. Metered in `fleet.reserve_analysis_run`.
    ANALYSIS_RUNS = "ANALYSIS_RUNS"
    #: Model tokens, input plus output. Provider-reported where the provider
    #: reported them and the platform's own estimate otherwise -- the quota
    #: bounds both alike, because a cap that only counted billed tokens could be
    #: walked past by a provider that reports nothing. Which basis a figure came
    #: from stays visible in `aida.cost_metrics`' `basis` label, where it
    #: belongs; it is not a second accumulator here.
    MODEL_TOKENS = "MODEL_TOKENS"
    #: SQL statements put through a lineage parser. The compute a change burst
    #: actually spends: a redefined view costs a parse of every statement in it.
    PARSER_STATEMENTS = "PARSER_STATEMENTS"


class QuotaScope(StrEnum):
    """Which window refused. Closed, and a metric label."""

    ORGANIZATION = "ORGANIZATION"
    DATASOURCE = "DATASOURCE"


#: Reason codes, carried the way `aida.agent_budget`'s are: stable, operator
#: facing, and written verbatim into whatever rejection record the caller keeps.
REASON_TENANT_QUOTA = "tenant_daily_quota_exhausted"
REASON_SOURCE_QUOTA = "source_daily_quota_exhausted"

QUOTA_DECISIONS = Counter(
    "aida_usage_quota_decisions_total",
    (
        "Quota admission decisions, by dimension, scope and outcome. No tenant "
        "or datasource label, by the same rule aida.footprint_metrics follows: "
        "an organization or datasource id is unbounded cardinality and would "
        "put a tenant identifier on a public surface. The refused tenant is in "
        "the usage_quota_refused log line and in the window row."
    ),
    labelnames=("dimension", "scope", "decision"),
)

DECISION_ADMITTED: Final = "ADMITTED"
DECISION_REFUSED: Final = "REFUSED"
#: No cap declared for this dimension, so nothing was decided and nothing was
#: written. Counted separately rather than as ADMITTED, because an estate
#: reading a dashboard of admissions should not see traffic it is not
#: actually bounding reported as traffic it is.
DECISION_UNMETERED: Final = "UNMETERED"


class QuotaRefused(RuntimeError):
    """A declared quota refused this consumption.

    `reason_code` is stable and operator-facing; `scope` and `dimension` say
    which window refused, so a caller can tell a tenant-wide exhaustion (every
    source is affected) from one noisy source (the rest of the tenant is fine).
    """

    def __init__(self, reason_code: str, *, scope: QuotaScope, dimension: UsageDimension) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.scope = scope
        self.dimension = dimension


@dataclass(frozen=True, slots=True)
class QuotaCaps:
    """The caps in force for one dimension. `None` means none declared."""

    tenant: int | None
    source: int | None

    @property
    def any_declared(self) -> bool:
        return self.tenant is not None or self.source is not None


def caps_for(settings: Settings, dimension: UsageDimension) -> QuotaCaps:
    """The tenant and source caps this deployment declares for `dimension`.

    Written out per dimension rather than as
    `getattr(settings, f"{...}_daily_quota_per_organization")`, which would be
    shorter. Two reasons, and neither is style: `scripts/generate_
    configuration_inventory.py` counts attribute reads to find settings nothing
    reads, and a `getattr` over an f-string reports as *dynamic* -- a category
    the ratchet accepts but an auditor cannot check. And `match` over a closed
    `StrEnum` makes a dimension added without settings a type error at this
    function rather than an `AttributeError` at the first call that uses it.
    """
    match dimension:
        case UsageDimension.ANALYSIS_RUNS:
            return QuotaCaps(
                tenant=settings.analysis_run_daily_quota_per_organization,
                source=settings.analysis_run_daily_quota_per_datasource,
            )
        case UsageDimension.MODEL_TOKENS:
            return QuotaCaps(
                tenant=settings.model_token_daily_quota_per_organization,
                source=settings.model_token_daily_quota_per_datasource,
            )
        case UsageDimension.PARSER_STATEMENTS:
            return QuotaCaps(
                tenant=settings.parser_statement_daily_quota_per_organization,
                source=settings.parser_statement_daily_quota_per_datasource,
            )


def _window_date(now: dt.datetime | None) -> dt.date:
    """The UTC day a consumption falls in.

    UTC rather than a tenant's local day, deliberately and matching
    `AgentBudgetWindow`: a window that rolled over per tenant timezone would
    need the timezone to be part of the unique constraint, and a tenant that
    changed it would silently get two windows for one day.
    """
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).date()


async def _ensure_tenant_window(
    session: AsyncSession, *, organization_id: UUID, dimension: str, window_date: dt.date
) -> UUID:
    """The tenant window row's id, creating it if this is the day's first event.

    The insert races every other caller starting the same day at the same
    moment; the unique constraint settles it and the loser re-reads. Wrapped in
    a savepoint so a lost race does not poison the caller's transaction -- the
    same recipe, for the same reason, as `agent_budget._ensure_window`.
    """
    existing = await session.scalar(
        select(TenantUsageWindow.id).where(
            TenantUsageWindow.organization_id == organization_id,
            TenantUsageWindow.dimension == dimension,
            TenantUsageWindow.window_date == window_date,
        )
    )
    if existing is not None:
        return existing
    window_id = uuid4()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(TenantUsageWindow).values(
                    id=window_id,
                    organization_id=organization_id,
                    dimension=dimension,
                    window_date=window_date,
                    used=0,
                    event_count=0,
                )
            )
        return window_id
    except IntegrityError:
        pass
    contended = await session.scalar(
        select(TenantUsageWindow.id).where(
            TenantUsageWindow.organization_id == organization_id,
            TenantUsageWindow.dimension == dimension,
            TenantUsageWindow.window_date == window_date,
        )
    )
    if contended is None:  # pragma: no cover -- the constraint said it exists
        raise QuotaRefused(
            REASON_TENANT_QUOTA,
            scope=QuotaScope.ORGANIZATION,
            dimension=UsageDimension(dimension),
        )
    return contended


async def _ensure_source_window(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    dimension: str,
    window_date: dt.date,
) -> UUID:
    """The source window row's id, creating it if this is the day's first event."""
    existing = await session.scalar(
        select(SourceUsageWindow.id).where(
            SourceUsageWindow.organization_id == organization_id,
            SourceUsageWindow.datasource_id == datasource_id,
            SourceUsageWindow.dimension == dimension,
            SourceUsageWindow.window_date == window_date,
        )
    )
    if existing is not None:
        return existing
    window_id = uuid4()
    try:
        async with session.begin_nested():
            await session.execute(
                insert(SourceUsageWindow).values(
                    id=window_id,
                    organization_id=organization_id,
                    datasource_id=datasource_id,
                    dimension=dimension,
                    window_date=window_date,
                    used=0,
                    event_count=0,
                )
            )
        return window_id
    except IntegrityError:
        pass
    contended = await session.scalar(
        select(SourceUsageWindow.id).where(
            SourceUsageWindow.organization_id == organization_id,
            SourceUsageWindow.datasource_id == datasource_id,
            SourceUsageWindow.dimension == dimension,
            SourceUsageWindow.window_date == window_date,
        )
    )
    if contended is None:  # pragma: no cover -- the constraint said it exists
        raise QuotaRefused(
            REASON_SOURCE_QUOTA,
            scope=QuotaScope.DATASOURCE,
            dimension=UsageDimension(dimension),
        )
    return contended


def _rowcount(result: object) -> int:
    return cast("CursorResult[Any]", result).rowcount


async def _consume_tenant(
    session: AsyncSession,
    *,
    window_id: UUID,
    organization_id: UUID,
    amount: int,
    cap: int | None,
) -> bool:
    """Move the tenant window by `amount`, refusing rather than passing the cap.

    Returns whether the row moved. `organization_id` is in the predicate
    alongside the primary key on purpose -- see the module docstring on INV-5.
    """
    statement = update(TenantUsageWindow).where(
        TenantUsageWindow.id == window_id,
        TenantUsageWindow.organization_id == organization_id,
    )
    if cap is not None:
        statement = statement.where(TenantUsageWindow.used + amount <= cap)
    result = await session.execute(
        statement.values(
            used=TenantUsageWindow.used + amount,
            event_count=TenantUsageWindow.event_count + 1,
        )
    )
    return _rowcount(result) > 0


async def _consume_source(
    session: AsyncSession,
    *,
    window_id: UUID,
    organization_id: UUID,
    datasource_id: UUID,
    amount: int,
    cap: int | None,
) -> bool:
    """Move the source window by `amount`, refusing rather than passing the cap."""
    statement = update(SourceUsageWindow).where(
        SourceUsageWindow.id == window_id,
        SourceUsageWindow.organization_id == organization_id,
        SourceUsageWindow.datasource_id == datasource_id,
    )
    if cap is not None:
        statement = statement.where(SourceUsageWindow.used + amount <= cap)
    result = await session.execute(
        statement.values(
            used=SourceUsageWindow.used + amount,
            event_count=SourceUsageWindow.event_count + 1,
        )
    )
    return _rowcount(result) > 0


async def _release_source(
    session: AsyncSession,
    *,
    window_id: UUID,
    organization_id: UUID,
    datasource_id: UUID,
    amount: int,
) -> None:
    """Give back a source consumption whose companion tenant consumption was refused.

    Clamped at zero in the statement rather than trusting the arithmetic to stay
    inside the CHECK constraint: a double release is a bug, and it should not
    take the caller's transaction down with it. `event_count` is *not* decreased
    -- the event happened and was refused, and a diagnostics counter that
    un-counted refusals would hide exactly the traffic an operator is looking
    for.

    There is no tenant-side counterpart, and that is a consequence of the order
    `consume_quota` charges in rather than an omission: the source window moves
    first, so a source refusal has nothing to release and a tenant refusal has
    exactly this one thing to release.
    """
    adjusted = SourceUsageWindow.used - amount
    await session.execute(
        update(SourceUsageWindow)
        .where(
            SourceUsageWindow.id == window_id,
            SourceUsageWindow.organization_id == organization_id,
            SourceUsageWindow.datasource_id == datasource_id,
        )
        .values(used=case((adjusted < 0, literal(0)), else_=adjusted))
    )


async def consume_quota(
    session: AsyncSession,
    settings: Settings,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    dimension: UsageDimension,
    amount: int,
    now: dt.datetime | None = None,
) -> bool:
    """Charge `amount` of `dimension` to this tenant and source, or refuse.

    Returns True when a declared quota admitted the consumption and False when
    no quota is declared for the dimension at all (nothing was written, and the
    caller is free to proceed -- see the module docstring on why that is not a
    quota of zero). Raises `QuotaRefused` when a declared cap refused.

    The source window is charged *first* and the tenant window second. The
    order matters for what a refusal costs: refusing at the source leaves the
    tenant untouched and needs no release, which is the common case on a fleet
    where one noisy source is the problem. Refusing at the tenant releases the
    source's share before raising.

    `datasource_id` may be None for work that is genuinely tenant-wide; the
    source cap is then not consulted and the tenant cap still is.
    """
    if amount <= 0:
        # A metered call that thinks it consumed nothing has miscounted, and
        # "nothing was consumed" must not become the way a quota is bypassed.
        raise ValueError("a quota consumption must be a positive amount")
    caps = caps_for(settings, dimension)
    if not caps.any_declared:
        QUOTA_DECISIONS.labels(
            dimension=dimension.value,
            scope=QuotaScope.ORGANIZATION.value,
            decision=DECISION_UNMETERED,
        ).inc()
        return False
    window_date = _window_date(now)

    source_window: UUID | None = None
    if datasource_id is not None and caps.source is not None:
        source_window = await _ensure_source_window(
            session,
            organization_id=organization_id,
            datasource_id=datasource_id,
            dimension=dimension.value,
            window_date=window_date,
        )
        admitted = await _consume_source(
            session,
            window_id=source_window,
            organization_id=organization_id,
            datasource_id=datasource_id,
            amount=amount,
            cap=caps.source,
        )
        if not admitted:
            QUOTA_DECISIONS.labels(
                dimension=dimension.value,
                scope=QuotaScope.DATASOURCE.value,
                decision=DECISION_REFUSED,
            ).inc()
            _log.warning(
                "usage_quota_refused",
                dimension=dimension.value,
                scope=QuotaScope.DATASOURCE.value,
                organization_id=str(organization_id),
                datasource_id=str(datasource_id),
                amount=amount,
                cap=caps.source,
            )
            raise QuotaRefused(
                REASON_SOURCE_QUOTA, scope=QuotaScope.DATASOURCE, dimension=dimension
            )

    if caps.tenant is not None:
        tenant_window = await _ensure_tenant_window(
            session,
            organization_id=organization_id,
            dimension=dimension.value,
            window_date=window_date,
        )
        admitted = await _consume_tenant(
            session,
            window_id=tenant_window,
            organization_id=organization_id,
            amount=amount,
            cap=caps.tenant,
        )
        if not admitted:
            if source_window is not None and datasource_id is not None:
                await _release_source(
                    session,
                    window_id=source_window,
                    organization_id=organization_id,
                    datasource_id=datasource_id,
                    amount=amount,
                )
            QUOTA_DECISIONS.labels(
                dimension=dimension.value,
                scope=QuotaScope.ORGANIZATION.value,
                decision=DECISION_REFUSED,
            ).inc()
            _log.warning(
                "usage_quota_refused",
                dimension=dimension.value,
                scope=QuotaScope.ORGANIZATION.value,
                organization_id=str(organization_id),
                datasource_id=str(datasource_id) if datasource_id else None,
                amount=amount,
                cap=caps.tenant,
            )
            raise QuotaRefused(
                REASON_TENANT_QUOTA, scope=QuotaScope.ORGANIZATION, dimension=dimension
            )

    QUOTA_DECISIONS.labels(
        dimension=dimension.value,
        scope=QuotaScope.DATASOURCE.value if datasource_id else QuotaScope.ORGANIZATION.value,
        decision=DECISION_ADMITTED,
    ).inc()
    return True


async def record_usage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    dimension: UsageDimension,
    amount: int,
    now: dt.datetime | None = None,
) -> None:
    """Accumulate `amount` against the windows without consulting any cap.

    The attribution half of the mechanism, for spend that is measured rather
    than bounded -- `aida.cost_metrics` calls this to carry per-source parser
    and model figures in rows, where the source dimension costs nothing, rather
    than in a Prometheus label, where a datasource id is unbounded cardinality.

    Separate from `consume_quota` rather than a flag on it, because the two
    answer different questions and mixing them produces the worst possible
    default: a caller that meant to record would start refusing work, or a
    caller that meant to enforce would silently stop.

    Never raises for a full window: there is no cap here to be full. A
    non-positive amount is still a caller bug and still raises.
    """
    if amount <= 0:
        raise ValueError("a usage record must be a positive amount")
    window_date = _window_date(now)
    if datasource_id is not None:
        source_window = await _ensure_source_window(
            session,
            organization_id=organization_id,
            datasource_id=datasource_id,
            dimension=dimension.value,
            window_date=window_date,
        )
        await _consume_source(
            session,
            window_id=source_window,
            organization_id=organization_id,
            datasource_id=datasource_id,
            amount=amount,
            cap=None,
        )
    tenant_window = await _ensure_tenant_window(
        session,
        organization_id=organization_id,
        dimension=dimension.value,
        window_date=window_date,
    )
    await _consume_tenant(
        session,
        window_id=tenant_window,
        organization_id=organization_id,
        amount=amount,
        cap=None,
    )


async def tenant_usage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    dimension: UsageDimension,
    window_date: dt.date,
) -> int:
    """What one tenant has consumed of one dimension on one UTC day.

    Read-only, for an operational surface and for tests -- never for
    enforcement. Reading this and then deciding is exactly the race the
    conditional UPDATE above exists to avoid.
    """
    total = await session.scalar(
        select(TenantUsageWindow.used).where(
            TenantUsageWindow.organization_id == organization_id,
            TenantUsageWindow.dimension == dimension.value,
            TenantUsageWindow.window_date == window_date,
        )
    )
    return int(total or 0)


async def source_usage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    dimension: UsageDimension,
    window_date: dt.date,
) -> int:
    """What one source has consumed of one dimension on one UTC day. Read-only."""
    total = await session.scalar(
        select(SourceUsageWindow.used).where(
            SourceUsageWindow.organization_id == organization_id,
            SourceUsageWindow.datasource_id == datasource_id,
            SourceUsageWindow.dimension == dimension.value,
            SourceUsageWindow.window_date == window_date,
        )
    )
    return int(total or 0)
