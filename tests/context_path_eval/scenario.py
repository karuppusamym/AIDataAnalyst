"""Seeded environment the AT-8 eval cases run against.

Same seeding shape as `tests/test_at6_context_receipts.py`'s `_Scenario`
(one organization, one datasource, one table with a business annotation, one
governed tool requiring a parameter) plus an optional published
`SemanticModelVersion`, since AT-8 additionally needs a case that pins a
semantic-model version rather than falling back to the technical-metadata
default. Entities are exposed by stable slug so `cases.py` never encodes a
run-generated UUID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AnalysisRun,
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
    SemanticModelVersion,
)
from aida.security import SecurityContext
from tests.support.doubles import security_context

#: The tool slug every eval case in `cases.py` refers to.
ORDER_LOOKUP_TOOL_SLUG = "order_lookup"
#: The required parameter that tool's SQL template needs, never supplied by
#: the eval cases that expect a CLARIFICATION context path.
ORDER_LOOKUP_REQUIRED_PARAMETER = "customer_id"


@dataclass
class ContextPathEvalScenario:
    """A seeded environment plus a slug -> id lookup for eval cases."""

    db: AsyncSession
    organization: Organization
    project: Project
    datasource: DataSource
    fact_orders: MetadataTable
    annotation: MetadataBusinessAnnotation
    annotation_version: MetadataBusinessAnnotationVersion
    tool_version_by_slug: dict[str, GovernedToolVersion] = field(default_factory=dict)
    semantic_model_version: SemanticModelVersion | None = None

    def context(self, roles: frozenset[str]) -> SecurityContext:
        return security_context(organization_id=self.organization.id, roles=roles)

    def tool_version_id(self, slug: str) -> UUID:
        return self.tool_version_by_slug[slug].id


async def build_scenario(
    db: AsyncSession, *, publish_semantic_model_version: int | None = None
) -> ContextPathEvalScenario:
    """Seed one organization with a table, a business annotation, and a
    governed tool that requires a parameter no eval case supplies by default.

    `publish_semantic_model_version`, when given, additionally publishes a
    `SemanticModelVersion` at that version number -- the case this scenario
    powers pins `semantic-model:<id>:v<version>` instead of the
    `technical-metadata:<analysis_run_id>` fallback every other case sees.
    """
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(organization)
    await db.flush()

    lob = LineOfBusiness(organization_id=organization.id, name="Retail", code="RETAIL")
    db.add(lob)
    await db.flush()

    data_domain = DataDomain(
        organization_id=organization.id, line_of_business_id=lob.id, name="Commerce",
        code="COMMERCE",
    )
    db.add(data_domain)
    await db.flush()

    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=data_domain.id,
        name="Core Commerce",
        slug="core-commerce",
    )
    db.add(project)
    await db.flush()

    datasource = DataSource(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=data_domain.id,
        project_id=project.id,
        name="core-warehouse",
        connector_type="POSTGRES",
        dialect="postgres",
        environment="PRODUCTION",
        credential_reference="vault://core-warehouse",
    )
    db.add(datasource)
    await db.flush()

    catalog = MetadataCatalog(
        organization_id=organization.id,
        datasource_id=datasource.id,
        name="warehouse",
        fingerprint="fp-catalog",
    )
    db.add(catalog)
    await db.flush()

    schema = MetadataSchema(
        organization_id=organization.id, catalog_id=catalog.id, name="public",
        fingerprint="fp-schema",
    )
    db.add(schema)
    await db.flush()

    fact_orders = MetadataTable(
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="fact_orders",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp-fact-orders",
        source_description="Order fact table",
    )
    db.add(fact_orders)
    await db.flush()

    business_domain = BusinessDomain(
        organization_id=organization.id,
        domain_key="commerce",
        display_name="Commerce",
        description="Commerce domain.",
        approved_by="steward",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(business_domain)
    await db.flush()

    business_entity = BusinessEntity(
        organization_id=organization.id,
        domain_id=business_domain.id,
        entity_key="order",
        display_name="Order",
        description="A customer order.",
        approved_by="steward",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(business_entity)
    await db.flush()

    # `source_proposal_id` points at a `MetadataEnrichmentProposal` row that
    # would exist in production; sqlite does not enforce this FK -- the same
    # bare `uuid4()` `test_at6_context_receipts.py` uses for the same reason.
    annotation = MetadataBusinessAnnotation(
        organization_id=organization.id,
        datasource_id=datasource.id,
        table_id=fact_orders.id,
        domain_id=business_domain.id,
        entity_id=business_entity.id,
        source_proposal_id=uuid4(),
    )
    db.add(annotation)
    await db.flush()

    annotation_version = MetadataBusinessAnnotationVersion(
        organization_id=organization.id,
        annotation_id=annotation.id,
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
    db.add(annotation_version)
    await db.flush()

    completed_analysis = AnalysisRun(
        organization_id=organization.id, datasource_id=datasource.id, status="COMPLETED"
    )
    db.add(completed_analysis)
    await db.flush()

    # A governed tool with a required parameter no eval case supplies by
    # default, so the run reaches (and persists) RESOLVED/PLANNED -- where
    # retrieval evidence and the semantic-version pin are computed -- without
    # needing a real SQL warehouse or model route. Same trick
    # `test_agent_orchestrator_retrieval_wiring.py`/`test_at6_context_receipts.py`
    # use.
    tool = GovernedTool(
        organization_id=organization.id, project_id=project.id, slug=ORDER_LOOKUP_TOOL_SLUG
    )
    db.add(tool)
    await db.flush()
    tool_version = GovernedToolVersion(
        organization_id=organization.id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Order Lookup",
        description="Look up orders by customer",
        datasource_id=datasource.id,
        sql_template="SELECT 1",
        referenced_tables=[],
        parameter_schema=[
            {"name": ORDER_LOOKUP_REQUIRED_PARAMETER, "type": "string", "required": True}
        ],
        allowed_roles=["Analyst"],
        fingerprint="fp-order-lookup",
        created_by="tool-dev",
    )
    db.add(tool_version)
    await db.flush()

    semantic_model_version: SemanticModelVersion | None = None
    if publish_semantic_model_version is not None:
        semantic_model_version = SemanticModelVersion(
            organization_id=organization.id,
            project_id=project.id,
            version=publish_semantic_model_version,
            name="Core Commerce Semantic Model",
            change_summary="Initial published semantic model.",
            status="PUBLISHED",
            created_by="modeler",
            approved_by="steward",
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(semantic_model_version)
        await db.flush()

    return ContextPathEvalScenario(
        db=db,
        organization=organization,
        project=project,
        datasource=datasource,
        fact_orders=fact_orders,
        annotation=annotation,
        annotation_version=annotation_version,
        tool_version_by_slug={ORDER_LOOKUP_TOOL_SLUG: tool_version},
        semantic_model_version=semantic_model_version,
    )
