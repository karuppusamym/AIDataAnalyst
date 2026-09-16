"""R11-FP12: asking through a published context product scopes what the answer may stand on.

A context product names the tables an agent should read and the tool versions it declares
eligible, and MCP already scopes an agent's tool list to the product it is read through. Ask
scoped nothing: a curated product had no bearing on the answer, so publishing one and asking a
question were unrelated events. `context_product_key` on the request ties them together.

Driven through `GovernedAgentOrchestrator.run()` -- the object the Ask route constructs -- on the
retrieval-wiring scenario, whose governed tool needs a parameter the question never mentions, so a
run that reaches the tool lands on CLARIFICATION without a warehouse or a model route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_intelligence import RetrievalHit
from aida.agent_orchestrator import (
    CONTEXT_PRODUCT_FORBIDDEN,
    CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE,
    CONTEXT_PRODUCT_UNAVAILABLE,
    AgentClarificationRequired,
    AgentPolicyRejected,
    ContextProductScope,
    GovernedAgentOrchestrator,
)
from aida.config import Settings
from aida.models import AgentRun, ContextProduct, ContextProductVersion
from tests.support.doubles import security_context
from tests.test_agent_orchestrator_retrieval_wiring import _Scenario, db  # noqa: F401

QUESTION = "orders"


async def _product(
    scenario: _Scenario,
    *,
    key: str,
    table_ids: Iterable[UUID],
    tool_version_ids: Iterable[UUID],
    consumer_roles: Sequence[str] = ("Analyst",),
    status: str = "PUBLISHED",
) -> ContextProductVersion:
    session = scenario.db
    product = ContextProduct(
        organization_id=scenario.organization.id,
        project_id=scenario.project.id,
        product_key=key,
        created_by="steward-1",
    )
    session.add(product)
    await session.flush()
    version = ContextProductVersion(
        organization_id=scenario.organization.id,
        product_id=product.id,
        version=1,
        status=status,
        name="Order context",
        description="What an agent needs to answer questions about orders.",
        purpose="Answer order questions.",
        owner_type="INDIVIDUAL",
        owner_principal="steward-1",
        table_ids=[str(table_id) for table_id in table_ids],
        eligible_tool_version_ids=[str(tool_id) for tool_id in tool_version_ids],
        allowed_consumer_roles=list(consumer_roles),
        fingerprint=f"fp-{uuid4().hex[:8]}",
        created_by="steward-1",
    )
    session.add(version)
    await session.flush()
    return version


async def _ask(
    scenario: _Scenario,
    *,
    product_key: str | None,
    context: Any = None,
) -> None:
    await GovernedAgentOrchestrator(Settings(_env_file=None)).run(
        scenario.db,
        datasource=scenario.datasource,
        context=context or scenario.steward(),
        correlation_id=f"corr-{uuid4().hex[:8]}",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=scenario.tool_version.id,
        tool_parameters={},
        requested_limit=None,
        context_product_key=product_key,
    )


async def _ask_with_sql(scenario: _Scenario, *, product_key: str, sql: str) -> None:
    """Ask through a product with caller-supplied SQL, the cheapest generated-statement path."""
    await GovernedAgentOrchestrator(
        Settings(_env_file=None, allow_development_sql_override=True)
    ).run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.steward(),
        correlation_id=f"corr-{uuid4().hex[:8]}",
        question=QUESTION,
        candidate_sql=sql,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
        context_product_key=product_key,
    )


async def _latest_run(scenario: _Scenario) -> AgentRun:
    run = await scenario.db.scalar(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(1))
    assert run is not None
    return run


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> AsyncIterator[_Scenario]:  # noqa: F811
    yield await _Scenario(db).build()


async def test_a_product_that_declares_the_tool_admits_it(scenario: _Scenario) -> None:
    await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
    )

    # The tool is reached, so the run lands on the clarification its missing parameter causes.
    with pytest.raises(AgentClarificationRequired):
        await _ask(scenario, product_key="orders-context")

    run = await _latest_run(scenario)
    assert any(hit["object_type"] == "GOVERNED_TOOL" for hit in run.retrieval_evidence)
    resolved = [step for step in run.step_trace if step.get("stage") == "RESOLVED"]
    assert resolved and resolved[-1]["details"]["context_product_version"] == 1


async def test_a_tool_the_product_does_not_declare_is_not_reachable(scenario: _Scenario) -> None:
    await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[],
    )

    # Without the tool there is nothing governed left to plan, so the run ends some other way --
    # what matters is that the tool never became evidence.
    with pytest.raises(Exception):  # noqa: B017, PT011
        await _ask(scenario, product_key="orders-context")

    run = await _latest_run(scenario)
    assert not any(hit["object_type"] == "GOVERNED_TOOL" for hit in run.retrieval_evidence)


async def test_a_table_the_product_does_not_name_is_not_evidence(scenario: _Scenario) -> None:
    await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
    )

    with pytest.raises(AgentClarificationRequired):
        await _ask(scenario, product_key="orders-context")

    run = await _latest_run(scenario)
    table_ids = {
        hit["object_id"] for hit in run.retrieval_evidence if hit["object_type"] == "TABLE"
    }
    # `dim_customer` is reachable only by graph expansion from `fact_orders`, and the product
    # does not name it, so scoping drops it while the table it does name stays.
    assert table_ids == {str(scenario.fact_orders.id)}


async def test_generated_sql_may_not_read_a_table_the_product_does_not_name(
    scenario: _Scenario,
) -> None:
    """Scoping retrieval decides what the model saw, not what the statement reads."""
    await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
    )

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask_with_sql(
            scenario,
            product_key="orders-context",
            sql="SELECT c.customer_id FROM public.dim_customer AS c",
        )

    assert str(refused.value) == CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE


async def test_generated_sql_over_the_products_own_table_passes_the_scope_check(
    scenario: _Scenario,
) -> None:
    await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
    )

    # It still ends some other way -- there is no warehouse behind this scenario -- but the scope
    # check is not what stops it.
    with pytest.raises(Exception) as ended:  # noqa: B017, PT011
        await _ask_with_sql(
            scenario,
            product_key="orders-context",
            sql="SELECT o.order_id FROM public.fact_orders AS o",
        )

    assert str(ended.value) != CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE


def _scope(
    *,
    table_ids: Iterable[str] = (),
    tool_version_ids: Iterable[str] = (),
    routine_ids: Iterable[str] = (),
    ontology_version_ids: Iterable[str] = (),
    glossary_term_version_ids: Iterable[str] = (),
    semantic_model_version_ids: Iterable[str] = (),
) -> ContextProductScope:
    return ContextProductScope(
        version_id=uuid4(),
        version=1,
        table_ids=frozenset(table_ids),
        tool_version_ids=frozenset(tool_version_ids),
        routine_ids=frozenset(routine_ids),
        ontology_version_ids=frozenset(ontology_version_ids),
        glossary_term_version_ids=frozenset(glossary_term_version_ids),
        semantic_model_version_ids=frozenset(semantic_model_version_ids),
    )


def _hit(object_type: str, object_id: str, metadata: dict[str, Any]) -> RetrievalHit:
    return RetrievalHit(
        object_type=object_type,
        object_id=object_id,
        display_name="candidate",
        score=1.0,
        reason_codes=[],
        metadata=metadata,
    )


def test_a_routine_the_product_does_not_reference_is_not_evidence() -> None:
    """A routine reads tables of its own, and names none in `table_id`.

    An allowlist written in table ids therefore admitted every routine in the datasource, which
    is how evidence walked past a product that never referenced it.
    """
    selected, other = str(uuid4()), str(uuid4())
    scope = _scope(routine_ids=[selected])

    assert scope.admits(_hit("ROUTINE", selected, {"routine_id": selected}))
    assert not scope.admits(_hit("ROUTINE", other, {"routine_id": other}))


def test_a_product_naming_no_routine_admits_none() -> None:
    scope = _scope(table_ids=[str(uuid4())])
    routine_id = str(uuid4())

    assert not scope.admits(_hit("ROUTINE", routine_id, {"routine_id": routine_id}))


def test_meaning_is_pinned_where_the_product_pins_it() -> None:
    """The product's pinned versions are the meaning its answers stand on."""
    pinned, other = str(uuid4()), str(uuid4())
    scope = _scope(ontology_version_ids=[pinned])

    assert scope.admits(_hit("ONTOLOGY_CONCEPT", str(uuid4()), {"ontology_version_id": pinned}))
    assert not scope.admits(_hit("ONTOLOGY_CONCEPT", str(uuid4()), {"ontology_version_id": other}))


def test_a_kind_the_product_pins_nothing_of_is_not_narrowed() -> None:
    """Pinning nothing is not forbidding everything, or a product would have to re-pin the whole
    glossary to stay usable; current approved meaning applies."""
    scope = _scope(table_ids=[str(uuid4())])

    assert scope.admits(_hit("GLOSSARY_TERM", str(uuid4()), {"term_version_id": str(uuid4())}))


async def test_an_unknown_product_is_refused(scenario: _Scenario) -> None:
    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(scenario, product_key="no-such-product")

    assert str(refused.value) == CONTEXT_PRODUCT_UNAVAILABLE


async def test_a_product_with_no_published_version_is_refused(scenario: _Scenario) -> None:
    await _product(
        scenario,
        key="draft-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
        status="DRAFT",
    )

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(scenario, product_key="draft-context")

    assert str(refused.value) == CONTEXT_PRODUCT_UNAVAILABLE


async def test_a_caller_outside_the_products_consumer_roles_is_refused(
    scenario: _Scenario,
) -> None:
    await _product(
        scenario,
        key="orders-context",
        table_ids=[scenario.fact_orders.id],
        tool_version_ids=[scenario.tool_version.id],
        consumer_roles=["DataSteward"],
    )

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(
            scenario,
            product_key="orders-context",
            context=security_context(
                organization_id=scenario.organization.id, roles=frozenset({"Analyst"})
            ),
        )

    assert str(refused.value) == CONTEXT_PRODUCT_FORBIDDEN


async def test_asking_without_a_product_is_unchanged(scenario: _Scenario) -> None:
    await _product(
        scenario,
        key="orders-context",
        table_ids=[],
        tool_version_ids=[],
    )

    # A published product that names nothing scopes nothing when it is not asked through.
    with pytest.raises(AgentClarificationRequired):
        await _ask(scenario, product_key=None)

    run = await _latest_run(scenario)
    assert any(hit["object_type"] == "GOVERNED_TOOL" for hit in run.retrieval_evidence)
