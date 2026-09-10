"""The orchestrator's stage contract holds even where the run refuses.

`GovernedAgentOrchestrator.run` is a composition of six stages (R02). The
existing `test_agent_orchestrator_*` suites are the characterization tests for
what the run *does*; these are the tests for the contract that makes the
decomposition safe -- the ledger invariant that a stage transition always leaves
a trace entry, the frozen request that stops a stage acquiring a hidden input,
and the stage set itself.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.agent_runtime import RuntimeStage, RuntimeState
from aida.models import AgentRun, DataSource
from aida.orchestration_stages import OrchestrationRequest, RunLedger, trace_entry
from tests.support.doubles import security_context


def _datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="stage-fixture",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://stages",
        status="ACTIVE",
    )


def _ledger() -> RunLedger:
    run = AgentRun(
        id=uuid4(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        principal_id="p1",
        question_hash="0" * 64,
        generation_source="PENDING",
    )
    return RunLedger(agent_run=run, state=RuntimeState(request_id=str(run.id)))


def test_advancing_a_stage_always_leaves_a_trace_entry() -> None:
    """The invariant the ledger exists for.

    Before, `state = state.transition(...)` and `trace.append(_trace(state, ...))`
    were two independent lines repeated fourteen times through one 830-line
    function, and nothing stopped the second from being forgotten -- which would
    leave a run whose persisted trace claims it never reached the stage it
    actually reached.
    """
    ledger = _ledger()
    ledger.record("DETERMINISTIC")
    before = len(ledger.trace)

    ledger.advance(RuntimeStage.AUTHORIZED, control_type="DETERMINISTIC")
    ledger.advance(
        RuntimeStage.SCREENED, control_type="DETERMINISTIC", details={"decision": "ALLOW"}
    )

    assert len(ledger.trace) == before + 2
    assert [entry["stage"] for entry in ledger.trace[-2:]] == ["AUTHORIZED", "SCREENED"]
    assert ledger.state.stage is RuntimeStage.SCREENED
    assert ledger.trace[-1]["details"] == {"decision": "ALLOW"}


def test_the_trace_sequence_cannot_drift_from_the_state() -> None:
    """The sequence number is read off the state rather than counted by the
    caller, so an entry recorded twice or skipped is visible as a repeated or
    missing sequence rather than silently renumbered."""
    ledger = _ledger()
    ledger.record("DETERMINISTIC")
    ledger.advance(RuntimeStage.AUTHORIZED, control_type="DETERMINISTIC")
    ledger.advance(RuntimeStage.SCREENED, control_type="DETERMINISTIC")

    sequences = [entry["sequence"] for entry in ledger.trace]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_a_trace_entry_omits_empty_details() -> None:
    """`step_trace` is persisted on every run; an always-present empty `details`
    key would be noise in every audit export."""
    state = RuntimeState(request_id="r1")
    assert "details" not in trace_entry(state, "DETERMINISTIC")
    assert "details" not in trace_entry(state, "DETERMINISTIC", {})
    assert trace_entry(state, "DETERMINISTIC", {"a": 1})["details"] == {"a": 1}


def test_plan_evidence_is_published_on_the_run_not_only_held() -> None:
    """A run that refuses partway through must persist the evidence explaining
    the refusal, not an empty dict -- so evidence is pushed onto the row at each
    mutation rather than once at the end."""
    ledger = _ledger()
    ledger.plan_evidence["strategy"] = "GOVERNED_TOOL"
    assert ledger.agent_run.plan_evidence in (None, {})
    ledger.publish_plan_evidence()
    assert ledger.agent_run.plan_evidence == {"strategy": "GOVERNED_TOOL"}


def test_the_request_is_frozen_for_the_life_of_the_run() -> None:
    """A stage that could rewrite the caller's question, roles or datasource
    would make every earlier stage's decision unreproducible from the persisted
    evidence."""
    request = OrchestrationRequest(
        datasource=_datasource(),
        context=security_context(organization_id=uuid4()),
        correlation_id="c1",
        question="how many settlements failed",
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        request.question = "something else"  # type: ignore[misc]
    assert request.organization_id == request.datasource.organization_id


def test_run_is_a_composition_of_the_six_named_stages() -> None:
    """R02 names them: screen, retrieve, plan, validate, execute, explain. The
    point of the split was invariants, not line count -- but a stage silently
    removed from the composition would move its rule back into whatever calls
    it, which is the thing the split undid.
    """
    for name in (
        "_stage_screen",
        "_stage_retrieve",
        "_stage_plan",
        "_stage_validate",
        "_stage_execute",
        "_stage_explain",
    ):
        assert hasattr(GovernedAgentOrchestrator, name), f"{name} is missing"

    source = inspect.getsource(GovernedAgentOrchestrator.run)
    for name in (
        "_stage_screen",
        "_stage_retrieve",
        "_stage_plan",
        "_stage_validate",
        "_stage_execute",
        "_stage_explain",
    ):
        assert name in source, f"run() no longer calls {name}"


def test_every_pre_execution_refusal_funnels_through_one_writer() -> None:
    """`_persist_rejection` is the single place a refused run is written down:
    run status, trace, REFUSAL decision edge, audit record, agent-task close,
    commit. A new refusal path that wrote three of those five would produce a
    run that looks refused to one reader and unfinished to another.

    Asserted by source inspection rather than by driving every refusal: the
    refusal paths are exercised end-to-end by
    `tests/test_agent_orchestrator_retrieval_wiring.py` and
    `tests/test_agent_orchestrator_checkpoints.py`; what this adds is that no
    *new* path bypasses the writer.
    """
    stage_sources = "".join(
        inspect.getsource(getattr(GovernedAgentOrchestrator, name))
        for name in (
            "_stage_screen",
            "_stage_retrieve",
            "_stage_plan",
            "_stage_validate",
            "_validate_governed_tool",
            "_validate_development_sql",
            "_generate_statement",
        )
    )
    writers = ("_persist_rejection", "self._reject")
    for raise_call in ("AgentPolicyRejected(", "AgentClarificationRequired("):
        if raise_call in stage_sources:
            assert any(writer in stage_sources for writer in writers), (
                f"a pre-execution stage raises {raise_call} without reaching a refusal writer"
            )

    post = inspect.getsource(GovernedAgentOrchestrator._stage_execute) + inspect.getsource(
        GovernedAgentOrchestrator._stage_explain
    )
    assert "_deny_after_execution" in post, (
        "a post-execution checkpoint refusal must keep its execution id, which "
        "only _deny_after_execution does"
    )
    assert "_persist_rejection" not in post, (
        "a post-execution refusal must not use the pre-execution writer; that "
        "path drops the query_execution_id of a query that genuinely ran"
    )
