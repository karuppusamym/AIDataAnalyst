"""R11-FP12's recorded remainder: a run names the product, not just its version.

The tracker row says it plainly -- "a run records the product *version* id and
number but not its key, so an answer reopened from history can name the version
and not the product". Two different products both have a v2, and they are
unrelated; "version 2" on its own identifies nothing. The RESOLVED stage detail
now carries `context_product_key` beside the two it already carried.

Driven on the same scenario `tests/test_ask_through_context_product.py` uses --
its governed tool needs a parameter the question never mentions, so a run that
reaches the tool lands on CLARIFICATION without a warehouse or a model route,
and the RESOLVED stage is already behind it by then.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_orchestrator import AgentClarificationRequired, GovernedAgentOrchestrator
from aida.config import Settings
from aida.models import ContextProductVersion
from tests.test_agent_orchestrator_retrieval_wiring import _Scenario, db  # noqa: F401
from tests.test_ask_through_context_product import QUESTION, _latest_run, _product


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> AsyncIterator[_Scenario]:  # noqa: F811
    yield await _Scenario(db).build()


async def _ask(
    scenario: _Scenario,
    *,
    product_key: str | None = None,
    product_version: ContextProductVersion | None = None,
) -> None:
    await GovernedAgentOrchestrator(Settings(_env_file=None)).run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.steward(),
        correlation_id=f"corr-{uuid4().hex[:8]}",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=scenario.tool_version.id,
        tool_parameters={},
        requested_limit=None,
        context_product_key=product_key,
        context_product_version=product_version,
    )


def _resolved(run: Any) -> dict[str, Any]:
    steps = [step for step in run.step_trace if step.get("stage") == "RESOLVED"]
    assert steps, "the run never reached RESOLVED"
    details: dict[str, Any] = steps[-1]["details"]
    return details


async def test_a_run_records_which_product_answered(scenario: _Scenario) -> None:
    """The version number and the key together, because the number alone is
    ambiguous across products and the key alone does not pin the contents."""
    version = await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
    )

    with pytest.raises(AgentClarificationRequired):
        await _ask(scenario, product_key="orders-context")

    details = _resolved(await _latest_run(scenario))
    assert details["context_product_key"] == "orders-context"
    assert details["context_product_version"] == 1
    assert details["context_product_version_id"] == str(version.id)


async def test_the_key_comes_from_the_version_that_answered(scenario: _Scenario) -> None:
    """F01 lets a surface hand over an already-resolved version and no key at
    all. The key recorded has to be the one belonging to *that* version, so it
    is read from `product_id` rather than echoed back off the request -- a run
    whose trace named a product it did not use would be worse than one naming
    none.
    """
    version = await _product(
        scenario,
        key="ledger-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
    )

    with pytest.raises(AgentClarificationRequired):
        await _ask(scenario, product_version=version)

    details = _resolved(await _latest_run(scenario))
    assert details["context_product_key"] == "ledger-context"
    assert details["context_product_version_id"] == str(version.id)


async def test_a_run_not_asked_through_a_product_records_no_key(scenario: _Scenario) -> None:
    """The three keys travel together: a run with no product has none of them,
    so a provenance panel reading one of them is never left guessing whether the
    other two were dropped or never applied."""
    with pytest.raises(AgentClarificationRequired):
        await _ask(scenario)

    details = _resolved(await _latest_run(scenario))
    assert "context_product_key" not in details
    assert "context_product_version" not in details
    assert "context_product_version_id" not in details
