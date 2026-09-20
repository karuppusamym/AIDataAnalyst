"""R11-OKF03: both OKF import review types are registered at T2, at every size.

`OKF_IMPORT_BATCH` was registered when the import landed; `OKF_IMPORT_ROUTINE_DESCRIPTION` was
added a slice later and was never registered, so `risk_tier_for` answered T3 for it -- the
fail-closed fallback for an unknown type. T3 sits above every agent's hard ceiling too, so no
agent could decide it either way; what the fallback got wrong is *reporting*: an unregistered
type is invisible to anything that counts or lists review types by tier, and it told an auditor
reading tiers that this path had not been classified at all.

Both import types carry text from a file anyone may have edited, so both are T2 whatever the
payload says (no count escalation, no de-escalation), and neither may be decided by an agent.
"""

from __future__ import annotations

import pytest

from aida.okf_import import (
    OKF_IMPORT_REVIEW_TYPE,
    OKF_IMPORT_REVIEW_TYPES,
    OKF_IMPORT_ROUTINE_REVIEW_TYPE,
)
from aida.review_risk_tiers import (
    _TIERS,
    HARD_MAX_AGENT_TIER,
    TIER_T2,
    agent_decidable_object_types,
    risk_tier_for,
)


@pytest.mark.parametrize("review_type", sorted(OKF_IMPORT_REVIEW_TYPES))
def test_every_okf_import_review_type_is_registered_not_left_to_the_fallback(
    review_type: str,
) -> None:
    # In the table itself, not merely T2 by coincidence of the fallback changing.
    assert _TIERS.get(review_type) == TIER_T2


@pytest.mark.parametrize("review_type", sorted(OKF_IMPORT_REVIEW_TYPES))
@pytest.mark.parametrize(
    "payload",
    [None, {}, {"item_count": 1}, {"item_count": 100_000}, {"change_count": 3}],
)
def test_the_tier_does_not_move_with_size(review_type: str, payload: dict[str, int] | None) -> None:
    assert risk_tier_for(review_type, payload) == TIER_T2


def test_the_two_import_types_share_one_tier() -> None:
    assert risk_tier_for(OKF_IMPORT_REVIEW_TYPE) == risk_tier_for(OKF_IMPORT_ROUTINE_REVIEW_TYPE)


def test_no_agent_may_decide_either_at_the_hard_ceiling() -> None:
    decidable = agent_decidable_object_types(HARD_MAX_AGENT_TIER)
    assert OKF_IMPORT_REVIEW_TYPE not in decidable
    assert OKF_IMPORT_ROUTINE_REVIEW_TYPE not in decidable
