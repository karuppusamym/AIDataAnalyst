import pytest

from aida.agent_runtime import RuntimeStage, RuntimeState


def test_runtime_state_allows_only_declared_transitions() -> None:
    state = RuntimeState(request_id="request-1")
    state = state.transition(RuntimeStage.AUTHORIZED, policy_version="policy-1")
    state = state.transition(RuntimeStage.SCREENED)
    state = state.transition(RuntimeStage.RESOLVED, semantic_version="semantic-1")

    assert state.stage is RuntimeStage.RESOLVED
    assert state.step_count == 3
    assert state.policy_version == "policy-1"


def test_runtime_state_rejects_execution_bypass() -> None:
    state = RuntimeState(request_id="request-1")

    with pytest.raises(ValueError, match="invalid runtime transition"):
        state.transition(RuntimeStage.EXECUTED)
