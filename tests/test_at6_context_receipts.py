"""AT-6: context receipts -- fragment digests on `AgentRun`, and
`MetadataBusinessAnnotation` versioned rather than mutated in place.

Three layers, matching the tracker exit criterion:

1. `test_business_annotation_versions.py`-shaped unit coverage:
   `write_annotation_version` supersedes the prior `APPROVED` row instead of
   mutating it (`test_write_annotation_version_supersedes_instead_of_mutating`),
   and `semantic_inference.apply_enrichment_proposal`'s write path no longer
   touches `MetadataBusinessAnnotation`'s (now nonexistent) content columns
   across a re-approval (`test_apply_enrichment_proposal_versions_on_reapproval`).
2. End-to-end through the real orchestrator (same scaffolding as
   `test_agent_orchestrator_retrieval_wiring.py`): a real grounded run over a
   table with a business annotation produces a `BUSINESS_ANNOTATION` fragment
   digest on the persisted `AgentRun`, tied to the annotation version live at
   that moment (`test_orchestrator_run_hashes_business_annotation_grounding_fragment`).
3. The replay proof itself: after that run, the annotation is re-approved
   (new version, old one superseded) -- the run's stored digest still
   resolves, via `agent_run_replay.resolve_grounding`, to the *original*
   (now-superseded) content, not the live one
   (`test_resolve_grounding_replays_against_superseded_version_not_current_one`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from itertools import count
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_orchestrator import AgentClarificationRequired, GovernedAgentOrchestrator
from aida.agent_run_replay import resolve_grounding
from aida.business_annotation_versions import (
    AnnotationVersionContent,
    write_annotation_version,
)
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AgentRun,
    AnalysisRun,
    AuditEvent,
    BusinessDomain,
    BusinessEntity,
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.semantic_inference import (
    MetadataEnrichmentProposal,
    TableSemanticOutput,
    ToolBlueprintOutput,
    apply_enrichment_proposal,
)
from tests.support.doubles import security_context

# sqlite only auto-populates a bare `INTEGER PRIMARY KEY` for `AuditEvent.id`
# (a `BigInteger` relying on a real sequence in Postgres) -- same workaround as
# `test_agent_orchestrator_retrieval_wiring.py`.
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


class _Scenario:
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
            name="Commerce",
            code="COMMERCE",
        )
        db.add(self.data_domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.data_domain.id,
            name="Core Commerce",
            slug="core-commerce",
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

        self.fact_orders = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_orders",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-fact-orders",
            source_description="Order fact table",
        )
        db.add(self.fact_orders)
        await db.flush()

        self.business_domain = BusinessDomain(
            organization_id=self.organization.id,
            domain_key="commerce",
            display_name="Commerce",
            description="Commerce domain.",
            approved_by="steward",
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(self.business_domain)
        await db.flush()

        self.business_entity = BusinessEntity(
            organization_id=self.organization.id,
            domain_id=self.business_domain.id,
            entity_key="order",
            display_name="Order",
            description="A customer order.",
            approved_by="steward",
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(self.business_entity)
        await db.flush()

        # `source_proposal_id` points at a `MetadataEnrichmentProposal` row that
        # would exist in production; sqlite does not enforce this FK, and other
        # test files (`test_glossary_stewardship.py`, `test_catalog_rows_read_model.py`)
        # already use a bare `uuid4()` here for the same reason.
        self.annotation = MetadataBusinessAnnotation(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=self.fact_orders.id,
            domain_id=self.business_domain.id,
            entity_id=self.business_entity.id,
            source_proposal_id=uuid4(),
        )
        db.add(self.annotation)
        await db.flush()

        self.original_version = MetadataBusinessAnnotationVersion(
            organization_id=self.organization.id,
            annotation_id=self.annotation.id,
            version=1,
            status="APPROVED",
            business_name="Orders",
            business_description="One row per customer order.",
            table_role="FACT",
            grain_statement="One row per order.",
            synonyms=["orders"],
            suggested_questions=["How many orders were placed last month?"],
            tags=[],
            confidence=0.95,
            approved_by="steward",
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(self.original_version)
        await db.flush()

        completed_analysis = AnalysisRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            status="COMPLETED",
        )
        db.add(completed_analysis)

        # A governed tool with a required parameter the test never supplies, so
        # `GovernedPlanner` lands on CLARIFICATION -- the run reaches (and
        # persists) RESOLVED, where grounding-fragment digests are computed,
        # without needing a real SQL warehouse or model route. Same trick as
        # `test_agent_orchestrator_retrieval_wiring.py`.
        self.tool = GovernedTool(
            organization_id=self.organization.id, project_id=self.project.id, slug="order_lookup"
        )
        db.add(self.tool)
        await db.flush()
        self.tool_version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=self.tool.id,
            version=1,
            status="PUBLISHED",
            name="Order Lookup",
            description="Look up orders by customer",
            datasource_id=self.datasource.id,
            sql_template="SELECT 1",
            referenced_tables=[],
            parameter_schema=[{"name": "customer_id", "type": "string", "required": True}],
            allowed_roles=["Analyst"],
            fingerprint="fp-order-lookup",
            created_by="tool-dev",
        )
        db.add(self.tool_version)
        await db.flush()
        return self

    def steward(self):
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"Analyst"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


def _settings() -> Settings:
    return Settings(_env_file=None)


# --- 1. `write_annotation_version` supersedes instead of mutating -----------


async def test_write_annotation_version_supersedes_instead_of_mutating(
    scenario: _Scenario,
) -> None:
    new_version = await write_annotation_version(
        scenario.db,
        organization_id=scenario.organization.id,
        annotation_id=scenario.annotation.id,
        content=AnnotationVersionContent(
            business_name="Customer Orders",
            business_description="Revised: one row per order, including cancellations.",
            table_role="FACT",
            grain_statement="One row per order (including cancelled).",
            synonyms=["orders", "purchases"],
            suggested_questions=[],
            tags=["revised"],
            confidence=0.97,
        ),
        approved_by="reviewer-2",
        approved_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    await scenario.db.commit()

    assert new_version.version == 2
    assert new_version.status == "APPROVED"

    # The original row is superseded, not deleted or overwritten -- its content
    # is exactly what it always was.
    original = await scenario.db.get(
        MetadataBusinessAnnotationVersion, scenario.original_version.id
    )
    assert original is not None
    assert original.status == "SUPERSEDED"
    assert original.business_name == "Orders"
    assert original.business_description == "One row per customer order."

    rows = (
        await scenario.db.scalars(
            select(MetadataBusinessAnnotationVersion).where(
                MetadataBusinessAnnotationVersion.annotation_id == scenario.annotation.id
            )
        )
    ).all()
    assert len(rows) == 2, "re-approval must append a new row, never edit the old one in place"


# --- 1b. the real write path (semantic_inference.apply_enrichment_proposal) -


async def test_apply_enrichment_proposal_versions_on_reapproval(scenario: _Scenario) -> None:
    proposal = MetadataEnrichmentProposal(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        inference_run_id=uuid4(),
        table_id=scenario.fact_orders.id,
        governance_review_id=uuid4(),
        engine_type="RULES",
        engine_version="v1",
        confidence=0.9,
        payload=TableSemanticOutput(
            table_id=scenario.fact_orders.id,
            domain_key="COMMERCE",
            domain_name="Commerce",
            domain_description="Commerce domain, re-approved.",
            entity_key="ORDER",
            entity_name="Order",
            entity_description="A customer order, re-approved.",
            business_name="Orders (re-approved)",
            business_description="Re-approved description.",
            table_role="FACT",
            grain_statement="One row per order.",
            synonyms=[],
            suggested_questions=[],
            tags=[],
            confidence=0.9,
            evidence_ids=["ev-1"],
            tool_blueprint=ToolBlueprintOutput(
                recommended=False,
                slug="orders_lookup",
                name="Orders Lookup",
                description="Look up orders.",
                output_columns=[],
                allowed_roles=["Analyst"],
            ),
        ).model_dump(mode="json"),
        evidence={},
        fingerprint="fp-reapproval",
        proposed_by="engine",
    )
    scenario.db.add(proposal)
    await scenario.db.flush()

    returned = await apply_enrichment_proposal(
        scenario.db,
        proposal=proposal,
        reviewer="reviewer-2",
        approved_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    await scenario.db.commit()

    # Same identity row (get-or-create by table_id) -- it carries no content of
    # its own to have mutated.
    assert returned.id == scenario.annotation.id
    assert not hasattr(returned, "business_name")

    versions = (
        await scenario.db.scalars(
            select(MetadataBusinessAnnotationVersion)
            .where(MetadataBusinessAnnotationVersion.annotation_id == scenario.annotation.id)
            .order_by(MetadataBusinessAnnotationVersion.version)
        )
    ).all()
    assert [v.version for v in versions] == [1, 2]
    assert [v.status for v in versions] == ["SUPERSEDED", "APPROVED"]
    assert versions[0].business_name == "Orders"
    assert versions[1].business_name == "Orders (re-approved)"


# --- 2 & 3. end-to-end through the real orchestrator, then the replay proof -


async def test_orchestrator_run_hashes_business_annotation_grounding_fragment(
    scenario: _Scenario,
) -> None:
    settings = _settings()
    orchestrator = GovernedAgentOrchestrator(settings)

    with pytest.raises(AgentClarificationRequired):
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id="corr-at6",
            question="orders",
            candidate_sql=None,
            preferred_tool_version_id=scenario.tool_version.id,
            tool_parameters={},
            requested_limit=None,
        )

    agent_run = (
        await scenario.db.execute(
            select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
        )
    ).scalar_one()
    assert agent_run.status == "REJECTED"

    digests_by_type: dict[str, list[dict]] = {}
    for entry in agent_run.grounding_fragment_digests:
        digests_by_type.setdefault(entry["object_type"], []).append(entry)

    assert "BUSINESS_ANNOTATION" in digests_by_type, (
        "the seeded business annotation matches the question lexically and must "
        "appear as a hashed grounding fragment on the run"
    )
    annotation_entries = digests_by_type["BUSINESS_ANNOTATION"]
    assert len(annotation_entries) == 1
    entry = annotation_entries[0]
    assert entry["object_id"] == str(scenario.annotation.id)
    assert entry["annotation_version_id"] == str(scenario.original_version.id)
    assert entry["fragment_digest"].startswith("sha256:")

    # Every other fragment type still gets a real digest too (not just business
    # annotations) -- e.g. the governed tool planned for this question.
    assert "GOVERNED_TOOL" in digests_by_type
    assert digests_by_type["GOVERNED_TOOL"][0]["fragment_digest"].startswith("sha256:")
    assert digests_by_type["GOVERNED_TOOL"][0]["annotation_version_id"] is None


async def test_resolve_grounding_replays_against_superseded_version_not_current_one(
    scenario: _Scenario,
) -> None:
    """The AT-6 replay proof end-to-end: run a real grounded query, capture its
    digests, change the underlying annotation, and prove the original run's
    digests still resolve to the original (now-superseded) version, not the
    new one.
    """
    settings = _settings()
    orchestrator = GovernedAgentOrchestrator(settings)

    with pytest.raises(AgentClarificationRequired):
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id="corr-at6-replay",
            question="orders",
            candidate_sql=None,
            preferred_tool_version_id=scenario.tool_version.id,
            tool_parameters={},
            requested_limit=None,
        )

    agent_run = (
        await scenario.db.execute(
            select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
        )
    ).scalar_one()

    # Change the underlying annotation *after* the run -- a real re-approval,
    # through the real write path, not a direct mutation.
    await write_annotation_version(
        scenario.db,
        organization_id=scenario.organization.id,
        annotation_id=scenario.annotation.id,
        content=AnnotationVersionContent(
            business_name="Orders (corrected)",
            business_description="Corrected: excludes test orders.",
            table_role="FACT",
            grain_statement="One row per real (non-test) order.",
            synonyms=["orders"],
            suggested_questions=[],
            tags=["corrected"],
            confidence=0.99,
        ),
        approved_by="reviewer-3",
        approved_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    await scenario.db.commit()

    # The live annotation now says something different than what the run saw.
    current = await scenario.db.get(
        MetadataBusinessAnnotationVersion, scenario.original_version.id
    )
    assert current is not None
    assert current.status == "SUPERSEDED"

    resolved = await resolve_grounding(scenario.db, agent_run)
    annotation_fragments = [f for f in resolved if f.object_type == "BUSINESS_ANNOTATION"]
    assert len(annotation_fragments) == 1
    fragment = annotation_fragments[0]

    # Resolves to the *original* content the run was actually grounded on...
    assert fragment.resolved_annotation_version is not None
    assert fragment.resolved_annotation_version.id == scenario.original_version.id
    assert fragment.resolved_annotation_version.business_name == "Orders"
    assert (
        fragment.resolved_annotation_version.business_description
        == "One row per customer order."
    )
    # ...not the live one.
    assert fragment.resolved_annotation_version.business_name != "Orders (corrected)"
    # ...and is now known to be superseded, which is exactly why replay had to
    # resolve by the stored version id rather than "the current annotation".
    assert fragment.current_status == "SUPERSEDED"
    # The stored digest still matches the (untouched) superseded row's content.
    assert fragment.digest_verified is True
