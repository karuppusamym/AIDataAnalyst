"""AT-9 -- scope-aware term/metric definitions and refusal on ambiguity.

Proves each clause of the tracker's exit condition:

1. `glossary_term` uniqueness is `(organization_id, term_key, business_node_id)`
   with a nullable `business_node_id` standing in for an enterprise default --
   two definitions of the same term_key, scoped to different business nodes,
   coexist without violating any constraint
   (`test_two_node_scoped_definitions_of_the_same_term_key_coexist`).
2. Most-specific-wins resolution over N9's own business-graph primitives:
   a single node-scoped definition beats the enterprise default
   (`test_single_node_scoped_definition_beats_enterprise_default`), and a
   definition scoped to a more specific (descendant) node beats one scoped to
   an ancestor when both are in the caller's scope
   (`test_most_specific_node_wins_over_ancestor`).
3. Two equally-specific, incomparable node-scoped definitions in the
   caller's scope produce `AMBIGUOUS` with both as alternatives
   (`test_two_incomparable_node_scoped_definitions_are_ambiguous`).
4. End-to-end through the real orchestrator (same scaffolding as
   `test_at6_context_receipts.py`): a real grounded run whose evidence
   surfaces an ambiguous term refuses with `AgentClarificationRequired`
   carrying both definitions and both owners, persists the run `REJECTED`
   with reason `AMBIGUOUS_DEFINITION`, and records a `REFUSAL` decision-lineage
   edge (`test_orchestrator_refuses_on_ambiguous_definition`). A datasource
   scoped to only one of the two business nodes resolves cleanly and never
   raises for that reason (`test_orchestrator_does_not_refuse_when_scope_disambiguates`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_orchestrator import AgentClarificationRequired, GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AgentRun,
    AiDecisionRecord,
    AnalysisRun,
    BusinessAssignment,
    BusinessNode,
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    TermSemanticBinding,
)
from aida.semantic_inference import (
    ScopedDefinitionCandidate,
    format_ambiguous_definition_refusal,
    resolve_scoped_glossary_term,
)
from tests.support.doubles import security_context

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_PAST = _NOW - timedelta(days=365)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    """Two business nodes (Risk, Retail Banking), a datasource, and the
    glossary/semantic scaffolding `_Scenario.term` needs to seed a term_key
    with two node-scoped definitions.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.data_domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Risk & Finance",
            code="RISKFIN",
        )
        db.add(self.data_domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.data_domain.id,
            name="Core Risk & Finance",
            slug="core-risk-finance",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.data_domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        # ADR-0018 business graph: two unrelated top-level nodes.
        self.risk_node = BusinessNode(
            organization_id=self.organization.id,
            parent_id=None,
            kind="LOB",
            name="Risk",
            code="RISK",
            effective_from=_PAST,
        )
        self.retail_node = BusinessNode(
            organization_id=self.organization.id,
            parent_id=None,
            kind="LOB",
            name="Retail Banking",
            code="RETAIL_LOB",
            effective_from=_PAST,
        )
        db.add_all([self.risk_node, self.retail_node])
        await db.flush()

        # A descendant of risk_node, for the most-specific-wins test.
        self.risk_credit_node = BusinessNode(
            organization_id=self.organization.id,
            parent_id=self.risk_node.id,
            kind="SUB_LOB",
            name="Credit Risk",
            code="CREDIT_RISK",
            effective_from=_PAST,
        )
        db.add(self.risk_credit_node)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="warehouse",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()
        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="public",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()
        self.fact_exposures = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_exposures",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-fact-exposures",
        )
        db.add(self.fact_exposures)
        await db.flush()

        completed_analysis = AnalysisRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            status="COMPLETED",
        )
        db.add(completed_analysis)

        # A governed tool with a required parameter the non-ambiguous test
        # never supplies, so the run reaches (and persists) RESOLVED --
        # where the AT-9 ambiguity check runs -- without a real SQL warehouse
        # or model route. Same trick as test_at6_context_receipts.py.
        self.tool = GovernedTool(
            organization_id=self.organization.id, project_id=self.project.id, slug="risk_lookup"
        )
        db.add(self.tool)
        await db.flush()
        self.tool_version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=self.tool.id,
            version=1,
            status="PUBLISHED",
            name="Risk Lookup",
            description="Look up exposure by counterparty",
            datasource_id=self.datasource.id,
            sql_template="SELECT 1",
            referenced_tables=[],
            parameter_schema=[{"name": "counterparty_id", "type": "string", "required": True}],
            allowed_roles=["Analyst"],
            fingerprint="fp-risk-lookup",
            created_by="tool-dev",
        )
        db.add(self.tool_version)
        await db.flush()
        return self

    async def add_term(
        self,
        *,
        business_node_id,
        display_name: str,
        definition: str,
        owner_principal: str,
        term_key: str = "exposure",
    ) -> GlossaryTerm:
        term = GlossaryTerm(
            organization_id=self.organization.id,
            term_key=term_key,
            business_node_id=business_node_id,
            lifecycle_status="ACTIVE",
        )
        self.db.add(term)
        await self.db.flush()
        version = GlossaryTermVersion(
            organization_id=self.organization.id,
            term_id=term.id,
            version=1,
            status="APPROVED",
            display_name=display_name,
            definition=definition,
            owner_principal=owner_principal,
            created_by="steward",
            approved_by="steward",
            approved_at=_PAST,
        )
        self.db.add(version)
        await self.db.flush()
        return term

    async def bind_term_to_a_metric(self, term: GlossaryTerm, *, slug: str) -> None:
        """Only a term with an ACTIVE binding to a real semantic object
        surfaces as a `GLOSSARY_TERM` retrieval hit (`retrieval.hybrid_retrieve`)
        -- required for the end-to-end orchestrator tests, not the direct
        `resolve_scoped_glossary_term` unit tests.
        """
        metric = SemanticMetric(
            organization_id=self.organization.id, project_id=self.project.id, slug=slug
        )
        self.db.add(metric)
        await self.db.flush()
        binding = TermSemanticBinding(
            organization_id=self.organization.id,
            term_id=term.id,
            semantic_object_type="METRIC",
            semantic_object_id=metric.id,
            status="ACTIVE",
            requested_by="steward",
            approved_by="reviewer",
            approved_at=_PAST,
        )
        self.db.add(binding)
        await self.db.flush()

    async def assign_datasource_to(self, *node_ids) -> None:
        for node_id in node_ids:
            self.db.add(
                BusinessAssignment(
                    organization_id=self.organization.id,
                    business_node_id=node_id,
                    target_type="DATASOURCE",
                    target_id=str(self.datasource.id),
                    assignment_kind="MANUAL",
                    assigned_by="steward",
                    effective_from=_PAST,
                )
            )
        await self.db.flush()

    def steward(self):
        return security_context(organization_id=self.organization.id, roles=frozenset({"Analyst"}))


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


def _settings() -> Settings:
    return Settings(_env_file=None)


# --- 1. glossary_term uniqueness allows two node-scoped definitions --------


async def test_two_node_scoped_definitions_of_the_same_term_key_coexist(
    scenario: _Scenario,
) -> None:
    risk_term = await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk)",
        definition="Potential loss if a counterparty defaults.",
        owner_principal="risk-steward",
    )
    retail_term = await scenario.add_term(
        business_node_id=scenario.retail_node.id,
        display_name="Exposure (Retail)",
        definition="Outstanding balance a customer owes across products.",
        owner_principal="retail-steward",
    )
    await scenario.db.commit()
    assert risk_term.id != retail_term.id
    assert risk_term.term_key == retail_term.term_key == "exposure"


async def test_enterprise_default_is_capped_at_one_row(scenario: _Scenario) -> None:
    await scenario.add_term(
        business_node_id=None,
        display_name="Exposure (enterprise)",
        definition="Default enterprise-wide definition.",
        owner_principal="enterprise-steward",
    )
    await scenario.db.commit()
    with pytest.raises(IntegrityError):
        await scenario.add_term(
            business_node_id=None,
            display_name="Exposure (enterprise, duplicate)",
            definition="A second enterprise default -- must be rejected.",
            owner_principal="someone-else",
        )
        await scenario.db.commit()
    await scenario.db.rollback()


# --- 2. most-specific-wins resolution ---------------------------------------


async def test_single_node_scoped_definition_beats_enterprise_default(
    scenario: _Scenario,
) -> None:
    await scenario.add_term(
        business_node_id=None,
        display_name="Exposure (enterprise)",
        definition="Default enterprise-wide definition.",
        owner_principal="enterprise-steward",
    )
    risk_term = await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk)",
        definition="Potential loss if a counterparty defaults.",
        owner_principal="risk-steward",
    )
    await scenario.assign_datasource_to(scenario.risk_node.id)
    await scenario.db.commit()

    resolution = await resolve_scoped_glossary_term(
        scenario.db,
        organization_id=scenario.organization.id,
        term_key="exposure",
        datasource_id=scenario.datasource.id,
    )
    assert resolution.status == "RESOLVED"
    assert resolution.resolved is not None
    assert resolution.resolved.term_id == risk_term.id
    assert resolution.resolved.owner_principal == "risk-steward"


async def test_most_specific_node_wins_over_ancestor(scenario: _Scenario) -> None:
    ancestor_term = await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk, general)",
        definition="Risk-wide definition of exposure.",
        owner_principal="risk-steward",
    )
    descendant_term = await scenario.add_term(
        business_node_id=scenario.risk_credit_node.id,
        display_name="Exposure (Credit Risk)",
        definition="Credit-risk-specific definition of exposure.",
        owner_principal="credit-risk-steward",
    )
    # Scoped to the more specific (descendant) node only, plus its ancestors
    # via classification_scope -- both definitions are technically "in scope"
    # (the ancestor's node is in the descendant node's ancestor closure), but
    # the descendant is strictly more specific and must win outright.
    await scenario.assign_datasource_to(scenario.risk_credit_node.id)
    await scenario.db.commit()

    resolution = await resolve_scoped_glossary_term(
        scenario.db,
        organization_id=scenario.organization.id,
        term_key="exposure",
        datasource_id=scenario.datasource.id,
    )
    assert resolution.status == "RESOLVED"
    assert resolution.resolved is not None
    assert resolution.resolved.term_id == descendant_term.id
    assert ancestor_term.id != resolution.resolved.term_id


# --- 3. incomparable node-scoped definitions are ambiguous ------------------


async def test_two_incomparable_node_scoped_definitions_are_ambiguous(
    scenario: _Scenario,
) -> None:
    risk_term = await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk)",
        definition="Potential loss if a counterparty defaults.",
        owner_principal="risk-steward",
    )
    retail_term = await scenario.add_term(
        business_node_id=scenario.retail_node.id,
        display_name="Exposure (Retail)",
        definition="Outstanding balance a customer owes across products.",
        owner_principal="retail-steward",
    )
    await scenario.assign_datasource_to(scenario.risk_node.id, scenario.retail_node.id)
    await scenario.db.commit()

    resolution = await resolve_scoped_glossary_term(
        scenario.db,
        organization_id=scenario.organization.id,
        term_key="exposure",
        datasource_id=scenario.datasource.id,
    )
    assert resolution.status == "AMBIGUOUS"
    assert resolution.resolved is None
    alternative_ids = {c.term_id for c in resolution.alternatives}
    assert alternative_ids == {risk_term.id, retail_term.id}
    owners = {c.owner_principal for c in resolution.alternatives}
    assert owners == {"risk-steward", "retail-steward"}

    message = format_ambiguous_definition_refusal("exposure", resolution.alternatives)
    assert "risk-steward" in message
    assert "retail-steward" in message
    assert "Potential loss if a counterparty defaults." in message
    assert "Outstanding balance a customer owes across products." in message


async def test_no_scoped_definition_is_not_found_not_ambiguous(scenario: _Scenario) -> None:
    await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk)",
        definition="Potential loss if a counterparty defaults.",
        owner_principal="risk-steward",
    )
    # The datasource is never assigned to any business node, so the
    # node-scoped definition above never applies and there is no enterprise
    # default either.
    await scenario.db.commit()

    resolution = await resolve_scoped_glossary_term(
        scenario.db,
        organization_id=scenario.organization.id,
        term_key="exposure",
        datasource_id=scenario.datasource.id,
    )
    assert resolution.status == "NOT_FOUND"
    assert resolution.resolved is None
    assert resolution.alternatives == ()


def test_format_ambiguous_definition_refusal_names_both_business_nodes() -> None:
    a = ScopedDefinitionCandidate(
        term_id=uuid4(),
        business_node_id=uuid4(),
        display_name="Exposure (Risk)",
        definition="def-a",
        owner_principal="owner-a",
    )
    b = ScopedDefinitionCandidate(
        term_id=uuid4(),
        business_node_id=uuid4(),
        display_name="Exposure (Retail)",
        definition="def-b",
        owner_principal="owner-b",
    )
    message = format_ambiguous_definition_refusal("exposure", (a, b))
    assert "exposure" in message
    assert str(a.business_node_id) in message
    assert str(b.business_node_id) in message


# --- 4. end-to-end through the real orchestrator ----------------------------


async def test_orchestrator_refuses_on_ambiguous_definition(scenario: _Scenario) -> None:
    risk_term = await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk)",
        definition="Potential loss if a counterparty defaults.",
        owner_principal="risk-steward",
    )
    retail_term = await scenario.add_term(
        business_node_id=scenario.retail_node.id,
        display_name="Exposure (Retail)",
        definition="Outstanding balance a customer owes across products.",
        owner_principal="retail-steward",
    )
    await scenario.bind_term_to_a_metric(risk_term, slug="risk-exposure-metric")
    await scenario.bind_term_to_a_metric(retail_term, slug="retail-exposure-metric")
    await scenario.assign_datasource_to(scenario.risk_node.id, scenario.retail_node.id)
    await scenario.db.commit()

    orchestrator = GovernedAgentOrchestrator(_settings())
    with pytest.raises(AgentClarificationRequired) as excinfo:
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id="corr-at9-ambiguous",
            question="what is our exposure",
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    message = str(excinfo.value)
    assert "risk-steward" in message
    assert "retail-steward" in message

    agent_run = (
        await scenario.db.scalars(
            select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
        )
    ).one()
    assert agent_run.status == "REJECTED"
    assert agent_run.failure_reason == "AMBIGUOUS_DEFINITION"

    refusal_edges = (
        await scenario.db.scalars(
            select(AiDecisionRecord).where(
                AiDecisionRecord.run_id == agent_run.id,
                AiDecisionRecord.decision_type == "REFUSAL",
            )
        )
    ).all()
    assert len(refusal_edges) == 1
    assert refusal_edges[0].reason == "AMBIGUOUS_DEFINITION"


async def test_orchestrator_does_not_refuse_when_scope_disambiguates(scenario: _Scenario) -> None:
    risk_term = await scenario.add_term(
        business_node_id=scenario.risk_node.id,
        display_name="Exposure (Risk)",
        definition="Potential loss if a counterparty defaults.",
        owner_principal="risk-steward",
    )
    retail_term = await scenario.add_term(
        business_node_id=scenario.retail_node.id,
        display_name="Exposure (Retail)",
        definition="Outstanding balance a customer owes across products.",
        owner_principal="retail-steward",
    )
    await scenario.bind_term_to_a_metric(risk_term, slug="risk-exposure-metric")
    await scenario.bind_term_to_a_metric(retail_term, slug="retail-exposure-metric")
    # Scoped to Risk only -- the Retail definition never applies, so
    # resolution is RESOLVED, not AMBIGUOUS.
    await scenario.assign_datasource_to(scenario.risk_node.id)
    await scenario.db.commit()

    orchestrator = GovernedAgentOrchestrator(_settings())
    with pytest.raises(AgentClarificationRequired) as excinfo:
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id="corr-at9-resolved",
            question="what is our exposure",
            candidate_sql=None,
            preferred_tool_version_id=scenario.tool_version.id,
            tool_parameters={},
            requested_limit=None,
        )
    # This run still raises AgentClarificationRequired -- but for the
    # unrelated missing-tool-parameter reason (the AT-6 scaffolding trick),
    # never because the AT-9 ambiguity check fired.
    assert "counterparty_id" in str(excinfo.value)

    agent_run = (
        await scenario.db.scalars(
            select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
        )
    ).one()
    assert agent_run.status == "REJECTED"
    assert agent_run.failure_reason != "AMBIGUOUS_DEFINITION"

    ambiguous_refusals = (
        await scenario.db.scalars(
            select(AiDecisionRecord).where(
                AiDecisionRecord.run_id == agent_run.id,
                AiDecisionRecord.decision_type == "REFUSAL",
                AiDecisionRecord.reason == "AMBIGUOUS_DEFINITION",
            )
        )
    ).all()
    assert ambiguous_refusals == []
