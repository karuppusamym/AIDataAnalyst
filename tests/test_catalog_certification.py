"""CT-5: asset certification lifecycle with expiry (module 04 catalog / 08 stewardship).

Same testing posture as CT-1 (`tests/test_catalog_bulk_actions.py`) and GL-5
(`tests/test_glossary_stewardship.py`): this repo has no live/fake-DB endpoint
test harness at all (a pre-existing, systemic gap, not specific to this item),
so behaviour that would otherwise need a session is exercised directly against
plain, unflushed ORM objects and the pure helper functions -- no session or
fixtures required, matching `AssetCertification(id=uuid4(), ...)` usage already
established in both of those files.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Table

from aida.asset_certification import (
    asset_certification_is_active,
    current_asset_certification,
)
from aida.main import app
from aida.models import AssetCertification
from aida.schemas import CertificationDecisionRequest
from aida.stewardship_service import active_certified_table_ids

# ---------------------------------------------------------------------------
# API contract: the catalog HTTP endpoint from module 04 section 8 is exposed
# ---------------------------------------------------------------------------


def test_table_certification_endpoint_is_exposed_for_get_and_post() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/tables/{table_id}/certification" in paths
    operations = paths["/v1/tables/{table_id}/certification"]
    assert "post" in operations
    assert "get" in operations


# ---------------------------------------------------------------------------
# Request contract: a decision certifies either the table, or exactly one column
# ---------------------------------------------------------------------------


def test_certification_decision_defaults_to_the_table_itself() -> None:
    decision = CertificationDecisionRequest(
        rationale="Certified against the approved quarterly data contract.",
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    assert decision.asset_type == "TABLE"
    assert decision.column_id is None


def test_certification_decision_accepts_a_column_target() -> None:
    column_id = uuid4()
    decision = CertificationDecisionRequest(
        asset_type="COLUMN",
        column_id=column_id,
        rationale="Certified against the approved data dictionary entry.",
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    assert decision.column_id == column_id


def test_certification_decision_requires_column_id_for_a_column_target() -> None:
    with pytest.raises(ValidationError, match="requires column_id"):
        CertificationDecisionRequest(
            asset_type="COLUMN",
            rationale="Certified against the approved data dictionary entry.",
            expires_at=datetime.now(UTC) + timedelta(days=90),
        )


def test_certification_decision_rejects_a_column_id_on_a_table_target() -> None:
    with pytest.raises(ValidationError, match="only meaningful when asset_type is COLUMN"):
        CertificationDecisionRequest(
            asset_type="TABLE",
            column_id=uuid4(),
            rationale="Certified against the approved quarterly data contract.",
            expires_at=datetime.now(UTC) + timedelta(days=90),
        )


def test_certification_decision_requires_a_substantive_rationale() -> None:
    with pytest.raises(ValidationError):
        CertificationDecisionRequest(
            rationale="short",
            expires_at=datetime.now(UTC) + timedelta(days=90),
        )


# ---------------------------------------------------------------------------
# Model shape: the schema extension is consistent with what the migration adds
# ---------------------------------------------------------------------------


def test_asset_certification_model_carries_the_column_scope_fields() -> None:
    table: Table = AssetCertification.__table__
    assert "asset_type" in table.columns
    assert "column_id" in table.columns
    assert table.columns["column_id"].nullable is True
    assert table.columns["asset_type"].nullable is False
    check_texts = " | ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    )
    assert "asset_type IN ('TABLE', 'COLUMN')" in check_texts
    assert "column_id IS NOT NULL" in check_texts


# ---------------------------------------------------------------------------
# Expiry enforcement: the core CT-5 exit condition, "expired records stop
# counting", proven at the shared helper both new endpoints and GL-5's
# coverage scoring now use.
# ---------------------------------------------------------------------------


def _certification(
    *, status: str = "ACTIVE", expires_at: datetime, asset_type: str = "TABLE"
) -> AssetCertification:
    return AssetCertification(
        id=uuid4(),
        organization_id=uuid4(),
        table_id=uuid4(),
        asset_type=asset_type,
        status=status,
        rationale="Certified against the approved data contract.",
        certified_by="steward@example.com",
        expires_at=expires_at,
    )


def test_asset_certification_is_active_requires_active_status_and_future_expiry() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    active = _certification(expires_at=now + timedelta(days=1))
    expired = _certification(expires_at=now - timedelta(seconds=1))
    revoked = _certification(status="REVOKED", expires_at=now + timedelta(days=1))
    superseded = _certification(status="SUPERSEDED", expires_at=now + timedelta(days=1))

    assert asset_certification_is_active(active, at=now) is True
    assert asset_certification_is_active(expired, at=now) is False
    assert asset_certification_is_active(revoked, at=now) is False
    assert asset_certification_is_active(superseded, at=now) is False


def test_asset_certification_is_active_defaults_to_the_current_time() -> None:
    long_expired = _certification(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    still_valid = _certification(expires_at=datetime(2999, 1, 1, tzinfo=UTC))
    assert asset_certification_is_active(long_expired) is False
    assert asset_certification_is_active(still_valid) is True


def test_current_asset_certification_skips_expired_rows_newest_first() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    expired_newest = _certification(expires_at=now - timedelta(days=1))
    active_older = _certification(expires_at=now + timedelta(days=30))
    # Ordered newest-first, as callers are expected to hand it (created_at desc).
    result = current_asset_certification([expired_newest, active_older], at=now)
    assert result is active_older


def test_current_asset_certification_returns_none_when_nothing_is_active() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    only_expired = [_certification(expires_at=now - timedelta(days=1))]
    assert current_asset_certification(only_expired, at=now) is None


def test_certifying_a_column_does_not_certify_its_table() -> None:
    """CT-5's own model extension must not silently widen GL-4's "certified"
    coverage dimension: a column-scoped certification denormalizes `table_id`
    for lookup, but it is not evidence the *table* was certified.
    """
    now = datetime(2026, 8, 30, tzinfo=UTC)
    table_id = uuid4()
    column_only = AssetCertification(
        id=uuid4(),
        organization_id=uuid4(),
        table_id=table_id,
        column_id=uuid4(),
        asset_type="COLUMN",
        status="ACTIVE",
        rationale="Certified against the approved data dictionary entry.",
        certified_by="steward@example.com",
        expires_at=now + timedelta(days=30),
    )
    assert active_certified_table_ids([column_only], now=now) == set()


def test_certifying_a_table_still_counts_towards_coverage() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    table_id = uuid4()
    table_cert = AssetCertification(
        id=uuid4(),
        organization_id=uuid4(),
        table_id=table_id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Certified against the approved data contract.",
        certified_by="steward@example.com",
        expires_at=now + timedelta(days=30),
    )
    assert active_certified_table_ids([table_cert], now=now) == {table_id}
