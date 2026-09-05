"""ADR-0027: the sampled disagreement rate its revisit trigger watches.

The ADR promises to revisit risk-tiered agent checking when the sampled
disagreement rate exceeds 5% for any object type over a full month. Until
something computed that number the promise was unfalsifiable.

These tests hold the three ways a metric like this normally goes wrong:

* it reports a rate off two samples and calls it a signal;
* it counts a sample nobody has looked at as agreement;
* it reports "no data" as if it were "passing".
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.db import Base
from aida.models import GovernanceReview, Organization, ReviewAuditSample
from aida.reviewer_agent_metrics import (
    MINIMUM_RESOLVED_FOR_SIGNAL,
    REVISIT_TRIGGER_DISAGREEMENT_RATE,
    disagreement_rates,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _seed_samples(
    session: AsyncSession,
    org: Organization,
    *,
    object_type: str = "ASSET_DESCRIPTION_DRAFT",
    agreed: int = 0,
    disagreed: int = 0,
    pending: int = 0,
    age: timedelta = timedelta(days=1),
) -> None:
    sampled_at = datetime.now(UTC) - age
    for outcome, count in (("AGREED", agreed), ("DISAGREED", disagreed), ("PENDING", pending)):
        for _ in range(count):
            review = GovernanceReview(
                organization_id=org.id,
                object_type=object_type,
                object_id=str(uuid4()),
                requested_action="PUBLISH",
                requested_by="agent:steward",
            )
            session.add(review)
            await session.flush()
            session.add(
                ReviewAuditSample(
                    organization_id=org.id,
                    governance_review_id=review.id,
                    agent_principal_id="agent:reviewer",
                    object_type=object_type,
                    risk_tier="T1",
                    decision="APPROVED",
                    sampled_at=sampled_at,
                    human_outcome=outcome,
                    resolved_at=None if outcome == "PENDING" else sampled_at,
                )
            )
    await session.flush()


# ---------------------------------------------------------------------------
# No data is not a pass
# ---------------------------------------------------------------------------


async def test_an_organization_with_no_samples_reports_unmeasured(
    session: AsyncSession,
) -> None:
    """This is every environment today: the feature is off, so nothing has
    been sampled. The report must not read as a clean bill of health."""
    org = await _seed_org(session)

    report = await disagreement_rates(session, org.id)

    assert report.measured is False
    assert report.by_object_type == ()
    assert report.breaching_object_types == ()


async def test_samples_that_are_all_pending_are_still_unmeasured(
    session: AsyncSession,
) -> None:
    """The sampler is doing its job and nobody is doing theirs. That is a
    finding about ADR-0027 condition (b), not evidence about the agent."""
    org = await _seed_org(session)
    await _seed_samples(session, org, pending=40)

    report = await disagreement_rates(session, org.id)

    assert report.measured is False
    row = report.by_object_type[0]
    assert row.pending == 40
    assert row.resolved == 0
    assert row.disagreement_rate is None, "None, not 0.0 -- nothing was measured"
    assert row.breaches_revisit_trigger is False


async def test_a_pending_sample_is_never_counted_as_agreement(
    session: AsyncSession,
) -> None:
    """Folding unexamined samples into the denominator would make the rate
    fall every time the audit queue got further behind."""
    org = await _seed_org(session)
    await _seed_samples(session, org, agreed=1, disagreed=1, pending=98)

    row = (await disagreement_rates(session, org.id)).by_object_type[0]

    assert row.resolved == 2
    assert row.disagreement_rate == 0.5
    assert row.sampled == 100


# ---------------------------------------------------------------------------
# The trigger itself
# ---------------------------------------------------------------------------


async def test_a_high_rate_on_too_few_samples_does_not_trip_the_trigger(
    session: AsyncSession,
) -> None:
    """One disagreement out of two is 50% and means nothing. The rate is
    still reported -- hiding it would be its own dishonesty -- but it is
    marked as insufficient rather than tripping a revisit."""
    org = await _seed_org(session)
    await _seed_samples(session, org, agreed=1, disagreed=1)

    row = (await disagreement_rates(session, org.id)).by_object_type[0]

    assert row.disagreement_rate == 0.5
    assert row.sufficient_sample is False
    assert row.breaches_revisit_trigger is False


async def test_a_rate_above_the_threshold_on_enough_samples_trips_it(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    # 3 of 30 == 10%, twice the ADR's threshold, on well over the floor.
    await _seed_samples(session, org, agreed=27, disagreed=3)

    report = await disagreement_rates(session, org.id)

    row = report.by_object_type[0]
    assert row.resolved == 30
    assert row.disagreement_rate == pytest.approx(0.1)
    assert row.sufficient_sample is True
    assert row.breaches_revisit_trigger is True
    assert report.breaching_object_types == ("ASSET_DESCRIPTION_DRAFT",)


async def test_a_rate_at_the_threshold_does_not_trip_it(session: AsyncSession) -> None:
    """The ADR says *exceeds* 5%. Exactly 5% is the boundary it drew, and a
    trigger that fires at the boundary would make the stated condition and
    the implemented one disagree."""
    org = await _seed_org(session)
    await _seed_samples(session, org, agreed=19, disagreed=1)  # exactly 5%

    row = (await disagreement_rates(session, org.id)).by_object_type[0]

    assert row.resolved == MINIMUM_RESOLVED_FOR_SIGNAL
    assert row.disagreement_rate == pytest.approx(REVISIT_TRIGGER_DISAGREEMENT_RATE)
    assert row.breaches_revisit_trigger is False


async def test_the_trigger_is_per_object_type_not_an_average(
    session: AsyncSession,
) -> None:
    """A healthy high-volume type must not average away a broken low-volume
    one -- that is exactly how a tier table stays wrong for a year."""
    org = await _seed_org(session)
    await _seed_samples(session, org, object_type="ASSET_DESCRIPTION_DRAFT", agreed=200)
    await _seed_samples(session, org, object_type="GLOSSARY_TERM", agreed=20, disagreed=5)

    report = await disagreement_rates(session, org.id)

    assert report.breaching_object_types == ("GLOSSARY_TERM",)
    healthy = next(r for r in report.by_object_type if r.object_type == "ASSET_DESCRIPTION_DRAFT")
    assert healthy.breaches_revisit_trigger is False


async def test_the_window_bounds_the_measurement(session: AsyncSession) -> None:
    """"Over a full month" is part of the trigger, not decoration: an agent
    that was bad in March and fixed in April must not keep tripping it."""
    org = await _seed_org(session)
    await _seed_samples(session, org, agreed=1, disagreed=29, age=timedelta(days=120))
    await _seed_samples(session, org, agreed=30, age=timedelta(days=2))

    report = await disagreement_rates(session, org.id)

    row = report.by_object_type[0]
    assert row.resolved == 30, "the old, bad month is outside the window"
    assert row.disagreement_rate == 0.0
    assert report.breaching_object_types == ()


async def test_another_organizations_samples_are_never_counted(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    other = await _seed_org(session)
    await _seed_samples(session, other, agreed=1, disagreed=29)

    report = await disagreement_rates(session, org.id)

    assert report.measured is False
