from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aida.config import Settings
from aida.glossary_owner_routing import (
    DEFAULT_ESCALATE_AFTER,
    DEFAULT_ROUTE_AFTER,
    TableFacts,
    sync_unowned_asset_backlog,
)
from aida.models import NotificationRuleRecord, ScanPolicy, UnownedAssetEscalation
from aida.schemas import DataSourceUpdate, ScanPolicyUpsert
from aida.workflows import scheduler
from aida.workflows.scheduler import (
    DEFAULT_UNOWNED_BACKLOG_RULE_NAME,
    ensure_default_unowned_backlog_notification_rule,
    maintenance_window_allows,
    next_maintenance_window,
    owner_routing_due,
    run_owner_routing_pass,
)


def _policy(start: int | None, end: int | None) -> ScanPolicy:
    return ScanPolicy(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        enabled=True,
        interval_minutes=60,
        mode="INCREMENTAL",
        priority=50,
        maintenance_start_hour_utc=start,
        maintenance_end_hour_utc=end,
        next_run_at=datetime(2026, 8, 25, tzinfo=UTC),
        created_by="test",
    )


def test_overnight_maintenance_window() -> None:
    policy = _policy(22, 6)

    assert maintenance_window_allows(policy, datetime(2026, 8, 25, 23, tzinfo=UTC))
    assert maintenance_window_allows(policy, datetime(2026, 8, 25, 5, tzinfo=UTC))
    assert not maintenance_window_allows(policy, datetime(2026, 8, 25, 12, tzinfo=UTC))


def test_next_maintenance_window_rolls_to_opening_hour() -> None:
    policy = _policy(8, 17)

    result = next_maintenance_window(policy, datetime(2026, 8, 25, 18, 42, tzinfo=UTC))

    assert result == datetime(2026, 8, 26, 8, tzinfo=UTC)


def test_scan_policy_requires_both_window_bounds() -> None:
    with pytest.raises(ValidationError, match="both maintenance-window hours"):
        ScanPolicyUpsert(interval_minutes=60, maintenance_start_hour_utc=8)


def test_scan_policy_rejects_zero_length_window() -> None:
    with pytest.raises(ValidationError, match="cannot be equal"):
        ScanPolicyUpsert(
            interval_minutes=60,
            maintenance_start_hour_utc=8,
            maintenance_end_hour_utc=8,
        )


def test_datasource_update_requires_a_material_change() -> None:
    with pytest.raises(ValidationError, match="at least one datasource field"):
        DataSourceUpdate()


# ---------------------------------------------------------------------------
# GL-6: unowned-asset backlog owner routing -- scheduled pass + default rule
# ---------------------------------------------------------------------------


def _apply_flush_defaults(instance: object) -> None:
    """Approximate what a real ``session.flush()`` populates via column
    defaults, mirroring the identically-named helper in
    test_glossary_stewardship.py -- the fake session below never touches a
    real engine, so SQLAlchemy's own default machinery (``id``,
    ``created_at``, ...) never runs.
    """
    table = getattr(type(instance), "__table__", None)
    if table is None:
        return
    for column in table.columns:
        if getattr(instance, column.name, None) is not None:
            continue
        default = column.default
        if default is None:
            continue
        value = default.arg(None) if default.is_callable else default.arg
        setattr(instance, column.name, value)


class _NotificationRuleSession:
    """Fake session for ensure_default_unowned_backlog_notification_rule:
    ``.scalar()`` returns a preset existing-rule lookup result, ``.add()``
    records what was created. No real engine/DB involved (this codebase's
    established fake-session test convention -- see _QueueSession in
    test_glossary_stewardship.py)."""

    def __init__(self, *, existing_rule: NotificationRuleRecord | None) -> None:
        self.existing_rule = existing_rule
        self.added: list[object] = []

    async def scalar(self, _statement: object) -> object:
        return self.existing_rule

    def add(self, value: object) -> None:
        _apply_flush_defaults(value)
        self.added.append(value)

    async def flush(self) -> None:
        return None


# --- owner_routing_due -------------------------------------------------


def test_owner_routing_due_when_never_swept() -> None:
    assert owner_routing_due(None, datetime.now(UTC), timedelta(hours=1)) is True


def test_owner_routing_not_due_before_interval_elapses() -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    last_run_at = now - timedelta(minutes=30)

    assert owner_routing_due(last_run_at, now, timedelta(hours=1)) is False


def test_owner_routing_due_once_interval_has_elapsed() -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    last_run_at = now - timedelta(hours=1)

    assert owner_routing_due(last_run_at, now, timedelta(hours=1)) is True


# --- ensure_default_unowned_backlog_notification_rule -------------------


async def test_ensure_default_rule_creates_one_when_none_exists() -> None:
    organization_id = uuid4()
    session = _NotificationRuleSession(existing_rule=None)

    rule = await ensure_default_unowned_backlog_notification_rule(session, organization_id)

    assert len(session.added) == 1
    assert session.added[0] is rule
    assert rule.organization_id == organization_id
    assert rule.name == DEFAULT_UNOWNED_BACKLOG_RULE_NAME
    assert rule.conditions == {}
    assert rule.channel == "EMAIL"
    assert rule.recipients == []
    assert rule.enabled is True
    assert rule.escalation_after_minutes == int(DEFAULT_ESCALATE_AFTER.total_seconds() // 60)


async def test_ensure_default_rule_is_idempotent_when_one_already_exists() -> None:
    organization_id = uuid4()
    existing = NotificationRuleRecord(
        id=uuid4(),
        organization_id=organization_id,
        name=DEFAULT_UNOWNED_BACKLOG_RULE_NAME,
        conditions={},
        channel="EMAIL",
        recipients=[],
        enabled=True,
        created_by="fleet-scheduler",
    )
    session = _NotificationRuleSession(existing_rule=existing)

    rule = await ensure_default_unowned_backlog_notification_rule(session, organization_id)

    assert rule is existing
    assert session.added == []  # a second rule was not created


def test_default_rule_conditions_actually_match_unowned_backlog_routing() -> None:
    """Not just "a row with some JSON exists" -- constructs a real aged-unowned-
    table routing scenario and proves the default rule (conditions={}) is the
    one route_notification actually fires, exercised the same way the
    scheduler pass would use it: as the only enabled NotificationRuleRecord
    for the organization.
    """
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    default_rule = NotificationRuleRecord(
        id=uuid4(),
        organization_id=organization_id,
        name=DEFAULT_UNOWNED_BACKLOG_RULE_NAME,
        conditions={},
        channel="EMAIL",
        recipients=[],
        escalation_after_minutes=int(DEFAULT_ESCALATE_AFTER.total_seconds() // 60),
        enabled=True,
        created_by="fleet-scheduler",
    )
    # Aged past DEFAULT_ROUTE_AFTER, with no business-domain annotation --
    # the common case the default rule's docstring calls out: a rule keyed on
    # `domain` would miss this exact entry.
    facts = TableFacts(
        table_id=table_id,
        datasource_id=uuid4(),
        table_name="stg_unmapped",
        schema_name="raw",
        domain_key=None,
        tags=(),
    )
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER - timedelta(hours=1),
        status="PENDING",
        recipients=[],
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: facts},
        ownership_rules=[],
        notification_rules=[default_rule],
        now=now,
    )

    assert len(result.routed) == 1
    routed = result.routed[0]
    assert routed is existing
    assert routed.status == "ROUTED"
    assert routed.notification_rule_id == default_rule.id
    assert routed.channel == "EMAIL"
    assert routed.dedup_key


# --- run_owner_routing_pass: per-organization fault isolation -----------


async def test_run_owner_routing_pass_isolates_one_organizations_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler, "_owner_routing_last_run_at", {})
    failing_org, healthy_org = uuid4(), uuid4()
    synced: list[UUID] = []

    async def fake_sync(organization_id: UUID, *, now: datetime) -> None:
        if organization_id == failing_org:
            raise RuntimeError("simulated bad rule / transient DB error")
        synced.append(organization_id)

    monkeypatch.setattr(scheduler, "_sync_owner_routing_for_organization", fake_sync)
    settings = Settings(owner_routing_interval_minutes=60, _env_file=None)
    now = datetime.now(UTC)

    swept = await run_owner_routing_pass(
        settings, now=now, organization_ids=[failing_org, healthy_org]
    )

    assert swept == 1
    assert synced == [healthy_org]
    # the failing organization is not marked as swept, so it is retried next
    # iteration rather than waiting a full interval
    assert failing_org not in scheduler._owner_routing_last_run_at
    assert scheduler._owner_routing_last_run_at[healthy_org] == now


async def test_run_owner_routing_pass_skips_organizations_not_yet_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        scheduler, "_owner_routing_last_run_at", {organization_id: now - timedelta(minutes=10)}
    )
    synced: list[UUID] = []

    async def fake_sync(org_id: UUID, *, now: datetime) -> None:
        synced.append(org_id)

    monkeypatch.setattr(scheduler, "_sync_owner_routing_for_organization", fake_sync)
    settings = Settings(owner_routing_interval_minutes=60, _env_file=None)

    swept = await run_owner_routing_pass(settings, now=now, organization_ids=[organization_id])

    assert swept == 0
    assert synced == []


async def test_run_owner_routing_pass_runs_once_interval_has_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        scheduler, "_owner_routing_last_run_at", {organization_id: now - timedelta(minutes=61)}
    )
    synced: list[UUID] = []

    async def fake_sync(org_id: UUID, *, now: datetime) -> None:
        synced.append(org_id)

    monkeypatch.setattr(scheduler, "_sync_owner_routing_for_organization", fake_sync)
    settings = Settings(owner_routing_interval_minutes=60, _env_file=None)

    swept = await run_owner_routing_pass(settings, now=now, organization_ids=[organization_id])

    assert swept == 1
    assert synced == [organization_id]
    assert scheduler._owner_routing_last_run_at[organization_id] == now
