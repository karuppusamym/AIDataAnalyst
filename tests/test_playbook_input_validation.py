"""Reject malformed playbooks before persistence or scheduled execution."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from aida.playbooks_api import PlaybookCreate, PlaybookUpdate


@pytest.mark.parametrize(
    "field",
    [
        "match_pattern",
        "action_parameters",
        "schedule_interval_minutes",
        "auto_apply_max_items",
        "enabled",
    ],
)
def test_patch_rejects_explicit_null_for_required_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        PlaybookUpdate.model_validate({field: None})


def test_patch_preserves_omission_and_nullable_column_pattern() -> None:
    assert PlaybookUpdate().model_dump(exclude_unset=True) == {}
    assert PlaybookUpdate(column_name_pattern=None).model_dump(exclude_unset=True) == {
        "column_name_pattern": None
    }
    assert PlaybookUpdate(enabled=False).model_dump(exclude_unset=True) == {"enabled": False}


@pytest.mark.parametrize("days", [True, False, 0, -1, 1.5, "30"])
def test_certification_expiry_requires_positive_integer(days: object) -> None:
    with pytest.raises(ValidationError):
        _certification(days)


def test_certification_accepts_positive_integer_expiry() -> None:
    assert _certification(30).action_parameters["expires_after_days"] == 30


def _certification(days: object) -> PlaybookCreate:
    return PlaybookCreate(
        name="Monthly certification",
        action="CERTIFY",
        datasource_id=uuid4(),
        match_pattern="*",
        action_parameters={"rationale": "Quarterly evidence review", "expires_after_days": days},
        schedule_interval_minutes=60,
    )
