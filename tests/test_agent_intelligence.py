from uuid import UUID

from aida.agent_evals import run_control_evaluation
from aida.agent_intelligence import GovernedPlanner, RetrievalHit, normalized_terms
from aida.config import Settings


def tool_hit(*, required: list[str] | None = None, score: float = 0.9) -> RetrievalHit:
    return RetrievalHit(
        object_type="GOVERNED_TOOL",
        object_id="00000000-0000-0000-0000-000000000001",
        display_name="Active customer states",
        score=score,
        reason_codes=["PUBLISHED_TOOL_MATCH"],
        metadata={
            "allowed_roles": ["Analyst"],
            "required_parameters": required or [],
            "slug": "active_customer_states",
        },
    )


def test_normalized_terms_are_value_free_and_stable() -> None:
    assert normalized_terms("Show the active customer states") == (
        "active",
        "customer",
        "states",
    )


def test_planner_prefers_published_role_bound_tool_over_candidate_sql() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=True,
        tool_parameters={},
    )

    assert plan.strategy == "GOVERNED_TOOL"
    assert plan.selected_tool_version_id == str(UUID("00000000-0000-0000-0000-000000000001"))


def test_planner_requests_missing_tool_parameters() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[tool_hit(required=["as_of_date"])],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
    )

    assert plan.strategy == "CLARIFICATION"
    assert plan.required_parameters == ["as_of_date"]


def test_planner_enforces_tool_role_binding() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[tool_hit()],
        roles=frozenset({"Viewer"}),
        candidate_sql_available=True,
        tool_parameters={},
    )

    assert plan.strategy == "DEVELOPMENT_SQL"


def test_governed_agent_control_suite_passes() -> None:
    summary = run_control_evaluation(Settings())

    assert summary.scenario_count >= 8
    assert summary.failed_count == 0
    assert summary.pass_rate == 1.0
