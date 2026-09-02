"""AT-8: context-path eval suite (pull forward from N17/ST-8).

Proves the mechanism `tests/context_path_eval/` builds actually works against
real orchestrator runs over real seeded data, not just against scaffolding:

1. `test_default_scenario_cases_match_expected_context_path` and
   `test_published_semantic_model_case_matches_expected_context_path` run
   every stored eval case (`tests/context_path_eval/cases.py`) end to end and
   assert the derived context path matches its stored expectation exactly --
   the real proof this row asks for.
2. `test_replaying_the_same_case_reaches_the_same_context_path` is the
   "replayable" proof: the same case, run twice against the same governed
   state, reaches the byte-identical context path both times.
3. `test_replay_reports_structural_drift_when_the_tool_is_unpublished` is the
   "reports drift, not necessarily a failure" proof: after the first replay,
   the governed tool is withdrawn (a real write through its own status field,
   not a direct mutation of the run) and the identical eval case is replayed
   again -- the runner's comparison flags the resulting mismatch as a named
   structural drift rather than silently passing or crashing.

Never asserts a final answer or business value anywhere in this file, per
INV-6/ADR-0014 -- every assertion below is over `ContextPath` fields
(strategy, tool/object identifiers, a semantic-version string, a policy
status/reason code), never over a result row or generated SQL's content.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from itertools import count

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.models import AuditEvent
from tests.context_path_eval.cases import (
    DEFAULT_SCENARIO_CASES,
    PUBLISHED_SEMANTIC_MODEL_SCENARIO_CASES,
    TOOL_MISSING_PARAMETER_REACHES_CLARIFICATION,
)
from tests.context_path_eval.runner import run_eval_case
from tests.context_path_eval.scenario import ORDER_LOOKUP_TOOL_SLUG, build_scenario

# sqlite only auto-populates a bare `INTEGER PRIMARY KEY` for `AuditEvent.id`
# (a `BigInteger` relying on a real sequence in Postgres) -- same workaround
# `test_at6_context_receipts.py` / `test_agent_orchestrator_retrieval_wiring.py` use.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


# --- 1. every stored case matches its stored expectation, for real ----------


async def test_default_scenario_cases_match_expected_context_path(db: AsyncSession) -> None:
    scenario = await build_scenario(db)
    for case in DEFAULT_SCENARIO_CASES:
        result = await run_eval_case(db, scenario, case)
        assert result.matched, f"{case.case_id}: {result.drift}"


async def test_published_semantic_model_case_matches_expected_context_path(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db, publish_semantic_model_version=2)
    for case in PUBLISHED_SEMANTIC_MODEL_SCENARIO_CASES:
        result = await run_eval_case(db, scenario, case)
        assert result.matched, f"{case.case_id}: {result.drift}"
        # The pin names the actual published version, not just "some semantic
        # model" -- the exact fact a glossary/semantic-model consumer needs.
        assert scenario.semantic_model_version is not None
        expected_prefix = (
            f"semantic-model:{scenario.semantic_model_version.id}:"
            f"v{scenario.semantic_model_version.version}"
        )
        assert result.actual.semantic_version == expected_prefix


# --- 2. replayable: the same case, run twice, reaches the same context path -


async def test_replaying_the_same_case_reaches_the_same_context_path(db: AsyncSession) -> None:
    scenario = await build_scenario(db)
    case = TOOL_MISSING_PARAMETER_REACHES_CLARIFICATION

    first = await run_eval_case(db, scenario, case)
    second = await run_eval_case(db, scenario, case)

    assert first.matched and second.matched
    # Two independent AgentRun rows (different ids, different timestamps) --
    # replay proves the *context path*, not row identity, so what must be
    # identical is the derived structural fact set, field by field.
    assert first.actual == second.actual


# --- 3. replay reports structural drift as information, not a hard crash ---


async def test_replay_reports_structural_drift_when_the_tool_is_unpublished(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db)
    case = TOOL_MISSING_PARAMETER_REACHES_CLARIFICATION

    baseline = await run_eval_case(db, scenario, case)
    assert baseline.matched
    assert baseline.actual.strategy == "CLARIFICATION"

    # A real governance write -- the tool is withdrawn -- not a direct
    # mutation of anything the first run already persisted. Retrieval only
    # ever surfaces PUBLISHED tool versions (`retrieval.py`), so this also
    # removes the tool from the *resolved objects* fact, not just the plan.
    tool_version = scenario.tool_version_by_slug[ORDER_LOOKUP_TOOL_SLUG]
    tool_version.status = "DEPRECATED"
    await db.flush()

    replayed = await run_eval_case(db, scenario, case)

    # The context path legitimately changed -- the planner never selects an
    # unpublished tool, so with nothing eligible and no candidate SQL offered
    # it falls to MODEL_GENERATION, which then fails closed on the (still)
    # unconfigured model route -- a different plan strategy AND a different
    # policy reason than the baseline run over the identical question. The
    # runner reports this as named field-level drift rather than passing
    # silently or raising, which is the whole point: a context path can
    # evolve as governed content changes, and that evolution must be visible.
    assert not replayed.matched
    drift_fields = {entry.split(":", 1)[0] for entry in replayed.drift}
    assert {"strategy", "selected_tool_version_id", "resolved_object_types",
            "policy_reason_code"} <= drift_fields
    assert replayed.actual.strategy == "MODEL_GENERATION"
    assert replayed.actual.selected_tool_version_id is None
    assert "GOVERNED_TOOL" not in replayed.actual.resolved_object_types
    assert replayed.actual.policy_reason_code == "MODEL_ROUTE_NOT_CONFIGURED"
    assert replayed.actual.policy_reason_code != baseline.actual.policy_reason_code
