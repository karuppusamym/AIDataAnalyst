from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aida.models import ScanPolicy
from aida.schemas import DataSourceUpdate, ScanPolicyUpsert
from aida.workflows.scheduler import maintenance_window_allows, next_maintenance_window


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
