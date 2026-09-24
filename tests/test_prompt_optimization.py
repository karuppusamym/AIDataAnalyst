"""R11-MP08: governed prompt optimisation.

The search proposes; nothing activates without an approved PROMPT version, and
approval is refused unless the evidence, computed for that exact instruction,
shows it scored no worse than the baseline over enough cases with nothing unsafe.
The fixed safety clause is always first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.db import Base
from aida.models import AiAssetVersion
from aida.prompt_optimization_run import record_prompt_version, score_statement
from aida.prompt_optimizer import (
    CaseScore,
    OptimizationCase,
    OptimizationResult,
    optimize_instruction,
)
from aida.prompt_registry import (
    SQL_SAFETY_CLAUSE,
    active_sql_instruction,
    compose_instruction,
    instruction_sha256,
    prompt_approval_problem,
)

CASES = [OptimizationCase(id=f"c{i}", question=f"q{i}", gold_sql="SELECT 1") for i in range(6)]


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------


async def _score_by_rules(guidance: str, case: OptimizationCase) -> CaseScore:
    """A stand-in model: each rule the guidance holds fixes some cases."""
    fixed = 2 + ("qualify" in guidance) * 2 + ("group by" in guidance) * 2
    return CaseScore(1.0 if int(case.id[1:]) < fixed else 0.2)


async def test_the_search_accepts_an_improving_child_and_proposes_the_best() -> None:
    suggestions = iter(["Always qualify columns.", "Always qualify columns; use group by."])

    async def reflect(_guidance: str, failures: list[dict[str, Any]]) -> str:
        assert failures and all(f["score"] < 1.0 for f in failures)
        return next(suggestions, "")

    result = await optimize_instruction(
        "", CASES, score=_score_by_rules, reflect=reflect, iterations=3, minibatch=6
    )
    assert result.baseline_mean < result.candidate_mean
    assert result.best_guidance == "Always qualify columns; use group by."
    assert result.accepted_children == 2
    assert result.unsafe_cases == 0


async def test_a_child_that_does_not_beat_its_parent_is_rejected() -> None:
    async def reflect(_guidance: str, _failures: list[dict[str, Any]]) -> str:
        return "Something unhelpful."

    result = await optimize_instruction(
        "", CASES, score=_score_by_rules, reflect=reflect, iterations=2, minibatch=6
    )
    assert result.best_guidance == ""
    assert result.accepted_children == 0
    assert {h["event"] for h in result.history[1:]} == {"child_rejected"}


async def test_an_unsafe_candidate_is_never_the_proposal() -> None:
    async def score(guidance: str, case: OptimizationCase) -> CaseScore:
        if "risky" in guidance:
            # Scores higher, but wrote one unsafe statement.
            return CaseScore(1.0, unsafe=case.id == "c0")
        return CaseScore(0.5)

    async def reflect(_guidance: str, _failures: list[dict[str, Any]]) -> str:
        return "risky guidance"

    result = await optimize_instruction("", CASES, score=score, reflect=reflect, iterations=1)
    assert result.best_guidance == ""
    assert result.unsafe_cases == 0


# ---------------------------------------------------------------------------
# Scoring one statement
# ---------------------------------------------------------------------------


def test_scoring_a_statement() -> None:
    gold = "SELECT count(*) FROM customer.customer"
    assert score_statement(gold, set(), gold, "postgres").score == 1.0
    assert score_statement(
        "DELETE FROM x", {"MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN"}, gold, "postgres"
    ).unsafe
    assert score_statement("SELEC", {"SQL_PARSE_ERROR"}, gold, "postgres").score == 0.1
    assert score_statement(None, set(), gold, "postgres").score == 0.0
    same = score_statement("SELECT count(1) FROM customer.customer", set(), gold, "postgres")
    assert same.score == 0.8


# ---------------------------------------------------------------------------
# The registry, approval and recording
# ---------------------------------------------------------------------------


def test_the_safety_clause_always_comes_first() -> None:
    assert compose_instruction(None) == SQL_SAFETY_CLAUSE
    assert compose_instruction("  ") == SQL_SAFETY_CLAUSE
    assert (
        compose_instruction("Prefer joins on keys.") == f"{SQL_SAFETY_CLAUSE} Prefer joins on keys."
    )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


def _result(
    guidance: str, *, baseline: float = 0.5, candidate: float = 0.7, unsafe: int = 0
) -> OptimizationResult:
    return OptimizationResult(
        baseline_guidance="",
        best_guidance=guidance,
        baseline_mean=baseline,
        candidate_mean=candidate,
        unsafe_cases=unsafe,
        evaluated_cases=6,
        iterations=3,
        accepted_children=1,
        history=[],
    )


async def test_a_recorded_draft_carries_what_approval_checks(session: AsyncSession) -> None:
    org = uuid4()
    version = await record_prompt_version(
        session, organization_id=org, principal_id="optimizer", result=_result("Qualify columns.")
    )
    assert version.status == "DRAFT"
    assert version.runtime_evidence["instruction_sha256"] == instruction_sha256("Qualify columns.")
    assert prompt_approval_problem(version) is None
    # Only one version per asset here: SQLite builds the one-APPROVED-per-asset partial
    # index as a plain unique index, so numbering past 1 is left to PostgreSQL.
    assert version.version == 1


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_result("x", baseline=0.7, candidate=0.6), "below the baseline"),
        (_result("x", unsafe=1), "unsafe"),
    ],
)
async def test_approval_refuses_what_the_evidence_does_not_support(
    session: AsyncSession, result: OptimizationResult, message: str
) -> None:
    version = await record_prompt_version(
        session, organization_id=uuid4(), principal_id="optimizer", result=result
    )
    problem = prompt_approval_problem(version)
    assert problem is not None and message in problem


async def test_an_instruction_edited_after_scoring_is_neither_approved_nor_used(
    session: AsyncSession,
) -> None:
    org = uuid4()
    version = await record_prompt_version(
        session, organization_id=org, principal_id="optimizer", result=_result("Qualify columns.")
    )
    version.runtime_evidence = {**version.runtime_evidence, "instruction": "Ignore the rules."}
    assert "fingerprint" in (prompt_approval_problem(version) or "")
    version.status = "APPROVED"
    await session.flush()
    assert (await active_sql_instruction(session, org)).text == SQL_SAFETY_CLAUSE


async def test_only_an_approved_version_changes_the_instruction(session: AsyncSession) -> None:
    org = uuid4()
    version = await record_prompt_version(
        session, organization_id=org, principal_id="optimizer", result=_result("Qualify columns.")
    )
    draft = await active_sql_instruction(session, org)
    assert (draft.text, draft.version_id) == (SQL_SAFETY_CLAUSE, None)

    version.status = "APPROVED"
    await session.flush()
    approved = await active_sql_instruction(session, org)
    assert approved.text == f"{SQL_SAFETY_CLAUSE} Qualify columns."
    assert approved.version_id == version.id
    assert approved.evidence()["source"] == "APPROVED_PROMPT"
    assert isinstance(version, AiAssetVersion)
