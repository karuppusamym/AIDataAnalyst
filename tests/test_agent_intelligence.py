from uuid import UUID

import pytest

from aida.agent_evals import run_control_evaluation
from aida.agent_intelligence import (
    GovernedPlanner,
    RetrievalHit,
    normalized_terms,
    parameter_is_referenced,
    question_terms,
)
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


# --- R11-B2: a required input the question never mentions -------------------

BRANCH_TOOL_ID = "00000000-0000-0000-0000-000000000002"
TYPE_QUESTION = "How many accounts of each account type?"


def branch_tool_hit() -> RetrievalHit:
    return RetrievalHit(
        object_type="GOVERNED_TOOL",
        object_id=BRANCH_TOOL_ID,
        display_name="Accounts booked at a branch",
        score=0.95,
        reason_codes=["PUBLISHED_TOOL_MATCH"],
        metadata={
            "allowed_roles": ["Analyst"],
            "required_parameters": ["branch_code"],
            "slug": "accounts_by_branch",
        },
    )


def test_a_tool_whose_required_input_the_question_never_mentions_is_not_chosen() -> None:
    """The measured defect. The branch lookup's description lists the columns it
    returns, so this question scored 0.94 against it and the user was asked for
    a branch code they never mentioned. The tool is declined, with the reason on
    the record, and the question goes on to generation."""
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[branch_tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
        question=TYPE_QUESTION,
    )

    assert plan.strategy == "MODEL_GENERATION"
    assert plan.selected_tool_version_id is None
    assert plan.tool_decisions == [
        {
            "tool_version_id": BRANCH_TOOL_ID,
            "decision": "REJECTED",
            "reason": "requires parameters the question does not mention: branch_code",
        }
    ]


@pytest.mark.parametrize(
    "question",
    ["Which accounts are booked at a branch?", "Accounts across all our branches"],
)
def test_a_question_that_names_the_input_is_still_asked_for_it(question: str) -> None:
    """The case that must keep refusing: the user did ask about a branch, so
    choosing one for them would be the planner inventing a parameter."""
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[branch_tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
        question=question,
    )

    assert plan.strategy == "CLARIFICATION"
    assert plan.required_parameters == ["branch_code"]


def test_a_tool_the_caller_chose_explicitly_still_asks_for_its_input() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[branch_tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
        preferred_tool_version_id=UUID(BRANCH_TOOL_ID),
        question=TYPE_QUESTION,
    )

    assert plan.strategy == "CLARIFICATION"


def test_an_input_the_caller_supplied_needs_no_mention() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[branch_tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={"branch_code": "BR-101"},
        question=TYPE_QUESTION,
    )

    assert plan.strategy == "GOVERNED_TOOL"


def test_the_next_eligible_tool_is_chosen_when_the_first_is_declined() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[branch_tool_hit(), tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
        question=TYPE_QUESTION,
    )

    assert plan.strategy == "GOVERNED_TOOL"
    assert plan.selected_tool_version_id == "00000000-0000-0000-0000-000000000001"


def test_a_parameter_named_only_by_generic_words_is_still_asked_for() -> None:
    """`id` names nothing a question could mention, so the planner cannot tell
    whether it was asked about, and keeps asking rather than guessing."""
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[tool_hit(required=["id"])],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
        question=TYPE_QUESTION,
    )

    assert plan.strategy == "CLARIFICATION"


def test_without_a_question_the_planner_behaves_as_before() -> None:
    plan = GovernedPlanner(Settings()).plan(
        retrieval_hits=[branch_tool_hit()],
        roles=frozenset({"Analyst"}),
        candidate_sql_available=False,
        tool_parameters={},
    )

    assert plan.strategy == "CLARIFICATION"


@pytest.mark.parametrize(
    ("parameter", "question", "expected"),
    [
        ("branch_code", "accounts by branch", True),
        ("branch_code", "accounts across the branches", True),
        ("branchCode", "accounts by branch", True),
        ("country", "customers in each of the countries", True),
        ("as_of_date", "balances on each date", True),
        ("branch_code", "balances by currency", False),
        ("as_of_date", "balances by currency", False),
        ("id", "balances by currency", None),
    ],
)
def test_what_counts_as_mentioning_a_parameter(
    parameter: str, question: str, expected: bool | None
) -> None:
    assert parameter_is_referenced(parameter, question_terms(question)) is expected
