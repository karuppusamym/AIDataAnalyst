"""R11-D32 / R11-D33: two list routes that answered 500 for every caller, on real rows.

Found 2026-09-20 by a sweep of every read route as each role against the deployed stack. Neither
route had a single test, so nothing had ever built the response from a real row:

* `GET /v1/datasources/{id}/quality-incidents` built each item from *every column of the table*
  and validated it against `DataQualityIncidentRead`, which forbids extra fields. The incident's
  `fingerprint` (its dedup key, added with the freshness sink) is a column the response never
  exposed, so any datasource with an incident answered 500 -- and so did the transition endpoint,
  *after* it had committed the state change, so a client saw a failure for a change that happened.
* `GET /v1/notifications` (and its acknowledge action) validated rows against
  `NotificationEventRead`, whose `incident_id` and `rule_id` were required. NT-1 (2026-09-04) made
  both columns nullable because a governance notification has neither, and the read model was
  never updated: the deployed database holds 18 such rows.

These seed the same rows the live database has and call the real route functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import DataQualityIncident, NotificationEventRecord
from aida.notification_api import acknowledge_notification, list_notifications
from aida.quality_api import (
    _declared_fields,
    list_quality_incidents,
    transition_quality_incident,
)
from aida.schemas import (
    DataQualityIncidentRead,
    DataQualityIncidentTransition,
    DataQualityObservationRead,
)
from tests.support.doubles import security_context
from tests.test_asset_evidence import _seed_datasource, _seed_table
from tests.test_asset_evidence import session as evidence_session  # noqa: F401

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
ADMIN = frozenset({"PlatformAdmin"})


async def _incident(session: AsyncSession):
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_incident")
    incident = DataQualityIncident(
        id=uuid4(),
        organization_id=table.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        # The column the read model does not declare -- and the reason for the 500.
        fingerprint=uuid4().hex,
        anomaly_type="FRESHNESS_VIOLATION",
        severity="HIGH",
        status="OPEN",
        summary="Freshness watermark is stale.",
        first_observed_at=_NOW,
        last_observed_at=_NOW,
    )
    session.add(incident)
    await session.commit()
    return datasource, incident


async def test_an_incident_with_a_fingerprint_lists_and_does_not_expose_it(
    evidence_session: AsyncSession,  # noqa: F811 -- the imported fixture, used by name
) -> None:
    datasource, incident = await _incident(evidence_session)

    page = await list_quality_incidents(
        datasource.id,
        incident_status=None,
        severity=None,
        limit=100,
        offset=0,
        context=security_context(organization_id=datasource.organization_id, roles=ADMIN),
        session=evidence_session,
    )

    assert page.total == 1
    [item] = page.items
    assert item.id == incident.id and item.table_name == "t_incident"
    assert item.anomaly_type == "FRESHNESS_VIOLATION"
    # The response shape is unchanged: the dedup key is still not part of it.
    assert "fingerprint" not in item.model_dump()


async def test_transitioning_an_incident_answers_with_it_after_committing(
    evidence_session: AsyncSession,  # noqa: F811 -- the imported fixture, used by name
) -> None:
    datasource, incident = await _incident(evidence_session)

    read = await transition_quality_incident(
        incident.id,
        DataQualityIncidentTransition(status="ACKNOWLEDGED", reason="Looking into it."),
        context=security_context(
            organization_id=datasource.organization_id, principal_id="steward", roles=ADMIN
        ),
        session=evidence_session,
    )

    assert read.status == "ACKNOWLEDGED" and read.acknowledged_by == "steward"
    assert read.table_name == "t_incident"


def test_a_read_model_takes_only_the_fields_it_declares() -> None:
    """The property the fix is: an attribute the model does not declare -- a column added later --
    cannot break the response, because the response never reads it."""
    row = SimpleNamespace(
        id=uuid4(),
        fingerprint="not declared",
        a_column_added_next_year="also not declared",
        anomaly_type="X",
    )

    values = _declared_fields(row, DataQualityIncidentRead, table_name="t")

    assert values["table_name"] == "t" and values["anomaly_type"] == "X"
    assert "fingerprint" not in values and "a_column_added_next_year" not in values
    # A declared field the row lacks is left for validation to report, not invented.
    assert "severity" not in values
    assert "table_name" in DataQualityObservationRead.model_fields


async def _governance_notification(session: AsyncSession, organization_id):
    """What the deployed database holds: no incident, no rule."""
    event = NotificationEventRecord(
        id=uuid4(),
        organization_id=organization_id,
        incident_id=None,
        rule_id=None,
        channel="EMAIL",
        recipients=["stewards@bank.example"],
        status="PENDING",
        dedup_key=uuid4().hex,
    )
    session.add(event)
    await session.commit()
    return event


async def test_a_governance_notification_lists(
    evidence_session: AsyncSession,  # noqa: F811 -- the imported fixture, used by name
) -> None:
    datasource = await _seed_datasource(evidence_session)
    event = await _governance_notification(evidence_session, datasource.organization_id)

    page = await list_notifications(
        incident_id=None,
        notification_status=None,
        limit=100,
        offset=0,
        context=security_context(organization_id=datasource.organization_id, roles=ADMIN),
        session=evidence_session,
    )

    assert page.total == 1
    [item] = page.items
    assert item.id == event.id
    assert item.incident_id is None and item.rule_id is None


async def test_a_governance_notification_can_be_acknowledged(
    evidence_session: AsyncSession,  # noqa: F811 -- the imported fixture, used by name
) -> None:
    datasource = await _seed_datasource(evidence_session)
    event = await _governance_notification(evidence_session, datasource.organization_id)

    read = await acknowledge_notification(
        event.id,
        context=security_context(
            organization_id=datasource.organization_id, principal_id="ops", roles=ADMIN
        ),
        session=evidence_session,
    )

    assert read.acknowledged_by == "ops" and read.incident_id is None
