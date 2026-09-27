"""R11-FP17: per-tenant and per-source quotas, and the shapes a quota must not take.

Before `aida.usage_quotas` the platform had run admission *concurrency* and
nothing else -- how much may be in flight, never how much may be consumed in a
day. These pin the mechanism and the four properties that are easy to lose:

* **no quota declared is not a quota of zero** -- with the shipped defaults
  `consume_quota` touches the database not at all, so admission behaves exactly
  as it did before the settings existed, and an upgrade refuses nothing;
* **the cap lives in the UPDATE's predicate**, so a row at its cap refuses the
  next consumption rather than being read, compared and then overwritten;
* **a refused request consumes nothing** -- a source consumption that succeeded
  is released when the tenant window then refuses;
* **INV-5 is in the statement** -- a consumption naming another tenant's source
  moves no row of that tenant's, because `organization_id` is restated in every
  predicate rather than trusted to be right in the caller.

Against a real in-memory SQLite engine (`tests/support/task_agents`), including
that module's aiosqlite BEGIN recipe, because `_ensure_window`'s savepoint has
to behave like PostgreSQL's for the contended-insert path to mean anything.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.fleet import RunAdmissionRejected, reserve_analysis_run
from aida.models import SourceUsageWindow, TenantUsageWindow
from aida.usage_quotas import (
    REASON_SOURCE_QUOTA,
    REASON_TENANT_QUOTA,
    QuotaRefused,
    QuotaScope,
    UsageDimension,
    caps_for,
    consume_quota,
    record_usage,
    source_usage,
    tenant_usage,
)
from tests.support.task_agents import agent_settings, seed_estate, task_agent_session

_DAY = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
async def session() -> AsyncSession:  # type: ignore[misc]
    async with task_agent_session() as active:
        yield active


class _ExplodingSession:
    """Any database access at all is a failure.

    Used for the one property the other tests cannot show: that with no quota
    declared, `consume_quota` does not merely write nothing -- it issues
    nothing. A version that created a window row and then skipped the cap check
    would pass an assertion about row counts being unchanged and still have
    added a statement to every admission on every deployment.
    """

    async def scalar(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("consume_quota touched the database with no quota declared")

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("consume_quota touched the database with no quota declared")


# --- no quota declared -------------------------------------------------------


async def test_no_declared_quota_issues_no_statement() -> None:
    admitted = await consume_quota(
        _ExplodingSession(),  # type: ignore[arg-type]
        agent_settings(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        dimension=UsageDimension.MODEL_TOKENS,
        amount=10_000,
        now=_DAY,
    )

    # False means "no quota is declared", which the caller reads as "proceed" --
    # deliberately distinct from True ("a declared quota admitted this").
    assert admitted is False


def test_every_dimension_has_settings_and_defaults_to_none() -> None:
    settings = agent_settings()

    for dimension in UsageDimension:
        caps = caps_for(settings, dimension)
        assert caps.tenant is None, f"{dimension} ships with a tenant cap nobody chose"
        assert caps.source is None, f"{dimension} ships with a source cap nobody chose"
        assert caps.any_declared is False


# --- the cap is enforced in the predicate ------------------------------------


async def test_source_quota_admits_to_the_cap_then_refuses(session: AsyncSession) -> None:
    org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(parser_statement_daily_quota_per_datasource=10)

    for _ in range(5):
        assert (
            await consume_quota(
                session,
                settings,
                organization_id=org.id,
                datasource_id=datasource.id,
                dimension=UsageDimension.PARSER_STATEMENTS,
                amount=2,
                now=_DAY,
            )
            is True
        )

    with pytest.raises(QuotaRefused) as refused:
        await consume_quota(
            session,
            settings,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.PARSER_STATEMENTS,
            amount=1,
            now=_DAY,
        )

    assert refused.value.reason_code == REASON_SOURCE_QUOTA
    assert refused.value.scope is QuotaScope.DATASOURCE
    assert refused.value.dimension is UsageDimension.PARSER_STATEMENTS
    # Exactly at the cap, not past it: the refused consumption added nothing.
    assert (
        await source_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.PARSER_STATEMENTS,
            window_date=_DAY.date(),
        )
        == 10
    )


async def test_a_single_consumption_larger_than_the_cap_is_refused(
    session: AsyncSession,
) -> None:
    """The cap is in the predicate, so this needs no separate pre-check: the
    UPDATE simply matches nothing. Pinned because a version that compared a
    *read* total against the cap would admit this one whenever the window was
    still empty."""
    org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(model_token_daily_quota_per_datasource=100)

    with pytest.raises(QuotaRefused):
        await consume_quota(
            session,
            settings,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            amount=101,
            now=_DAY,
        )

    assert (
        await source_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=_DAY.date(),
        )
        == 0
    )


async def test_tenant_quota_applies_across_sources(session: AsyncSession) -> None:
    org, first, _schema = await seed_estate(session)
    _org, second, _other_schema = await seed_estate(session, organization=org)
    settings = agent_settings(model_token_daily_quota_per_organization=100)

    await consume_quota(
        session,
        settings,
        organization_id=org.id,
        datasource_id=first.id,
        dimension=UsageDimension.MODEL_TOKENS,
        amount=60,
        now=_DAY,
    )
    # A second, different source in the same tenant. A per-source cap would let
    # this through; the tenant cap is what stops it.
    with pytest.raises(QuotaRefused) as refused:
        await consume_quota(
            session,
            settings,
            organization_id=org.id,
            datasource_id=second.id,
            dimension=UsageDimension.MODEL_TOKENS,
            amount=60,
            now=_DAY,
        )

    assert refused.value.reason_code == REASON_TENANT_QUOTA
    assert refused.value.scope is QuotaScope.ORGANIZATION


# --- a refused request consumes nothing --------------------------------------


async def test_a_tenant_refusal_releases_the_source_share(session: AsyncSession) -> None:
    """Both caps declared, the source cap roomy and the tenant cap tight. The
    source window moves first and must be put back, or a tenant that is out of
    budget would still burn its sources' allowances every time it was refused.
    """
    org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(
        model_token_daily_quota_per_datasource=1_000,
        model_token_daily_quota_per_organization=50,
    )

    with pytest.raises(QuotaRefused) as refused:
        await consume_quota(
            session,
            settings,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            amount=80,
            now=_DAY,
        )

    assert refused.value.scope is QuotaScope.ORGANIZATION
    assert (
        await source_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=_DAY.date(),
        )
        == 0
    )
    # The event is still counted even though its amount was released: a refusal
    # that un-counted itself would hide the traffic an operator is looking for.
    row = await session.scalar(
        select(SourceUsageWindow).where(SourceUsageWindow.datasource_id == datasource.id)
    )
    assert row is not None
    assert row.event_count == 1
    assert row.used == 0


@pytest.mark.parametrize("amount", [0, -1])
async def test_a_non_positive_amount_is_a_caller_bug(
    session: AsyncSession, amount: int
) -> None:
    org, datasource, _schema = await seed_estate(session)

    with pytest.raises(ValueError, match="positive amount"):
        await consume_quota(
            session,
            agent_settings(model_token_daily_quota_per_datasource=10),
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            amount=amount,
            now=_DAY,
        )


# --- INV-5 -------------------------------------------------------------------


async def test_a_consumption_naming_another_tenants_source_leaves_that_tenant_alone(
    session: AsyncSession,
) -> None:
    """INV-5 in the statement rather than in the caller.

    `victim`'s source already has a window at its cap. `attacker` consumes
    against that same datasource id. Because every predicate restates
    `organization_id`, the victim's row is not the row that moves -- the
    attacker gets a window of its own, scoped to itself, and the victim's
    figure is untouched.
    """
    victim_org, victim_source, _s1 = await seed_estate(session)
    attacker_org, _attacker_source, _s2 = await seed_estate(session)
    settings = agent_settings(parser_statement_daily_quota_per_datasource=100)

    await consume_quota(
        session,
        settings,
        organization_id=victim_org.id,
        datasource_id=victim_source.id,
        dimension=UsageDimension.PARSER_STATEMENTS,
        amount=100,
        now=_DAY,
    )

    await consume_quota(
        session,
        settings,
        organization_id=attacker_org.id,
        datasource_id=victim_source.id,
        dimension=UsageDimension.PARSER_STATEMENTS,
        amount=7,
        now=_DAY,
    )

    assert (
        await source_usage(
            session,
            organization_id=victim_org.id,
            datasource_id=victim_source.id,
            dimension=UsageDimension.PARSER_STATEMENTS,
            window_date=_DAY.date(),
        )
        == 100
    )
    assert (
        await tenant_usage(
            session,
            organization_id=victim_org.id,
            dimension=UsageDimension.PARSER_STATEMENTS,
            window_date=_DAY.date(),
        )
        == 0
    )


# --- windows ----------------------------------------------------------------


async def test_windows_are_per_utc_day(session: AsyncSession) -> None:
    org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(parser_statement_daily_quota_per_datasource=5)

    await consume_quota(
        session,
        settings,
        organization_id=org.id,
        datasource_id=datasource.id,
        dimension=UsageDimension.PARSER_STATEMENTS,
        amount=5,
        now=dt.datetime(2026, 9, 16, 23, 59, tzinfo=dt.UTC),
    )
    # One minute later is a different UTC day, so the cap starts again. Pinned
    # because a window keyed on anything but the UTC date -- a tenant's local
    # day, say -- would need the timezone in the unique constraint.
    assert (
        await consume_quota(
            session,
            settings,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.PARSER_STATEMENTS,
            amount=5,
            now=dt.datetime(2026, 9, 17, 0, 0, tzinfo=dt.UTC),
        )
        is True
    )


async def test_a_naive_timestamp_is_read_as_utc(session: AsyncSession) -> None:
    """Rather than raising. `datetime.now()` without a timezone reaching this
    module should not make a quota depend on how the caller built its clock."""
    org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(parser_statement_daily_quota_per_datasource=5)

    await consume_quota(
        session,
        settings,
        organization_id=org.id,
        datasource_id=datasource.id,
        dimension=UsageDimension.PARSER_STATEMENTS,
        amount=1,
        now=dt.datetime(2026, 9, 16, 12, 0),  # noqa: DTZ001 -- the point of the test
    )

    assert (
        await source_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.PARSER_STATEMENTS,
            window_date=dt.date(2026, 9, 16),
        )
        == 1
    )


# --- record_usage: accumulation without a cap --------------------------------


async def test_record_usage_accumulates_and_never_refuses(session: AsyncSession) -> None:
    """The attribution half. No cap is consulted, so a source can accumulate
    past any number and nothing raises -- this is how per-source cost figures
    are carried, and a cost record that refused to record would be useless."""
    org, datasource, _schema = await seed_estate(session)

    for _ in range(3):
        await record_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            amount=1_000_000,
            now=_DAY,
        )

    assert (
        await source_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=_DAY.date(),
        )
        == 3_000_000
    )
    assert (
        await tenant_usage(
            session,
            organization_id=org.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=_DAY.date(),
        )
        == 3_000_000
    )


async def test_record_usage_with_no_source_records_the_tenant_only(
    session: AsyncSession,
) -> None:
    org, _datasource, _schema = await seed_estate(session)

    await record_usage(
        session,
        organization_id=org.id,
        datasource_id=None,
        dimension=UsageDimension.MODEL_TOKENS,
        amount=500,
        now=_DAY,
    )

    assert (
        await tenant_usage(
            session,
            organization_id=org.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=_DAY.date(),
        )
        == 500
    )
    assert (await session.scalars(select(SourceUsageWindow))).all() == []


# --- fleet admission: the concurrency/quota distinction ----------------------


async def _complete_every_run(session: AsyncSession) -> None:
    """Terminate the runs admitted so far, so the next admission is decided by
    the daily quota rather than by the concurrency limit."""
    from aida.models import AnalysisRun

    for run in (await session.scalars(select(AnalysisRun))).all():
        run.status = "COMPLETED"
    await session.flush()


async def test_reserve_analysis_run_still_admits_with_no_quota_declared(
    session: AsyncSession,
) -> None:
    org, datasource, _schema = await seed_estate(session)

    run = await reserve_analysis_run(
        session,
        agent_settings(),
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
    )

    assert run.organization_id == org.id
    assert (await session.scalars(select(TenantUsageWindow))).all() == []
    assert (await session.scalars(select(SourceUsageWindow))).all() == []


async def test_max_active_runs_per_datasource_is_a_setting_not_a_literal_one(
    session: AsyncSession,
) -> None:
    """It was a hard-coded `1` in `fleet.reserve_analysis_run`. Raising it must
    actually admit a second concurrent run on one source."""
    _org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(max_active_runs_per_datasource=2)

    first = await reserve_analysis_run(
        session,
        settings,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
    )
    second = await reserve_analysis_run(
        session,
        settings,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
    )

    assert first.id != second.id
    with pytest.raises(RunAdmissionRejected, match="already has an active analysis run"):
        await reserve_analysis_run(
            session,
            settings,
            datasource_id=datasource.id,
            mode="FULL",
            trigger_type="MANUAL",
        )


async def test_a_daily_quota_bounds_runs_the_concurrency_limit_would_allow(
    session: AsyncSession,
) -> None:
    """The gap this row exists to close. One run at a time, twice over, is two
    runs -- and a concurrency limit cannot see that. The daily quota can."""
    _org, datasource, _schema = await seed_estate(session)
    settings = agent_settings(analysis_run_daily_quota_per_datasource=2)

    for _ in range(2):
        await reserve_analysis_run(
            session,
            settings,
            datasource_id=datasource.id,
            mode="INCREMENTAL",
            trigger_type="SCHEDULED",
        )
        await _complete_every_run(session)

    # Nothing is in flight, so concurrency admits it; the day's quota does not.
    with pytest.raises(RunAdmissionRejected, match="quota is exhausted") as rejected:
        await reserve_analysis_run(
            session,
            settings,
            datasource_id=datasource.id,
            mode="INCREMENTAL",
            trigger_type="SCHEDULED",
        )

    # The reason code survives the translation into this module's own type, so a
    # refusal is still attributable to which window refused.
    assert REASON_SOURCE_QUOTA in str(rejected.value)
