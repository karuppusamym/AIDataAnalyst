"""SM-2: Glossary term binding to semantic objects.

Module 08 (glossary) already links terms to *tables* (`AssetTermLink` /
`GlossaryLinkProposal`, GL-8). Module 07 (semantic layer) is a different kind
of object -- a governed, versioned `SemanticMetric` -- and nothing bound a
term to one before this. This file proves:

  1. Creation/approval: a binding is created PENDING_APPROVAL and only an
     independent reviewer (maker != checker) can activate it, mirroring the
     `CrossBoundaryGrant` shape (`request_cross_boundary_grant` in api.py)
     rather than GL-8's evidence-inference shape, since a binding here is a
     direct steward assertion, not something inferred from an annotation.
     Exercised with `ScriptedSession`/`RecordingSession`
     (`tests/support/doubles.py`) -- the same no-database pattern already
     used for behavioral coverage elsewhere (e.g. test_glossary_stewardship.py,
     the Tier-0 invariant suite).
  2. Bidirectional resolution: `GET .../glossary-terms/{id}/semantic-bindings`
     (term -> object) and `GET .../semantic-metrics/{id}/glossary-bindings`
     (object -> term) both return the same ACTIVE binding.
  3. The exit condition itself -- "terms resolve in retrieval" -- proven
     against a real `hybrid_retrieve` call over a real (in-memory sqlite)
     database: the bound term's definition/synonyms become part of what a
     semantic metric hit matches and ranks on, and a term hit surfaces the
     semantic object bound to it. A binding that never activated must NOT
     leak into retrieval -- the difference between a binding that actually
     participates and a static link nobody reads at query time.

(2) and (3) are read-only paths (no `record_audit` call), so they run
against a real engine with rows seeded directly through the ORM -- proving
the actual SQL joins, not a hand-simulated one.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.main import app
from aida.models import (
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
    TermSemanticBinding,
)
from aida.retrieval import hybrid_retrieve
from aida.schemas import GovernanceDecisionRequest, TermSemanticBindingCreate
from aida.semantic_api import (
    create_term_semantic_binding,
    decide_governance_review,
    delete_term_semantic_binding,
    list_metric_glossary_bindings,
    list_term_semantic_bindings,
)
from tests.support.doubles import ScriptedSession, security_context

# ---------------------------------------------------------------------------
# Contract: the new routes are actually registered
# ---------------------------------------------------------------------------


def test_sm2_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/semantic-metrics/{metric_id}/glossary-bindings" in paths
    assert "/v1/glossary-terms/{term_id}/semantic-bindings" in paths
    assert "/v1/term-semantic-bindings/{binding_id}" in paths


def test_term_semantic_binding_create_defaults_to_metric() -> None:
    body = TermSemanticBindingCreate(term_id=uuid4(), semantic_object_id=uuid4())
    assert body.semantic_object_type == "METRIC"


# ---------------------------------------------------------------------------
# 1. Creation / approval (maker-checker), scripted -- no database
# ---------------------------------------------------------------------------


def _project(organization_id: UUID) -> Project:
    return Project(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        name="Core Banking",
        slug="core-banking",
    )


def _metric(organization_id: UUID, project_id: UUID) -> SemanticMetric:
    return SemanticMetric(
        id=uuid4(), organization_id=organization_id, project_id=project_id, slug="net_sales"
    )


def _term(organization_id: UUID) -> GlossaryTerm:
    return GlossaryTerm(id=uuid4(), organization_id=organization_id, term_key="net-sales")


def _approved_version(term: GlossaryTerm) -> GlossaryTermVersion:
    return GlossaryTermVersion(
        id=uuid4(),
        organization_id=term.organization_id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net Sales",
        definition="Total sale value after adjustments.",
        synonyms=[],
        created_by="term-maker",
    )


async def test_create_term_semantic_binding_starts_pending_approval() -> None:
    organization_id = uuid4()
    project = _project(organization_id)
    metric = _metric(organization_id, project.id)
    term = _term(organization_id)
    approved = _approved_version(term)

    session = ScriptedSession(
        get_results={metric.id: metric, project.id: project, term.id: term},
        scalar_results=[approved, None],  # approved term version, then "no existing binding"
    )
    context = security_context(organization_id=organization_id, roles=frozenset({"DataSteward"}))
    body = TermSemanticBindingCreate(term_id=term.id, semantic_object_id=metric.id)

    result = await create_term_semantic_binding(metric.id, body, context, session)  # type: ignore[arg-type]

    assert result.status == "PENDING_APPROVAL"
    assert result.term_id == term.id
    assert result.semantic_object_id == metric.id
    assert result.semantic_object_name == "net_sales"
    assert result.governance_review_id is not None
    reviews = session.added_of(GovernanceReview)
    assert len(reviews) == 1
    assert reviews[0].object_type == "TERM_SEMANTIC_BINDING"
    assert reviews[0].requested_action == "BIND"
    assert session.commits == 1


async def test_create_term_semantic_binding_requires_an_approved_term() -> None:
    organization_id = uuid4()
    project = _project(organization_id)
    metric = _metric(organization_id, project.id)
    term = _term(organization_id)

    session = ScriptedSession(
        get_results={metric.id: metric, project.id: project, term.id: term},
        scalar_results=[None],  # no APPROVED version
    )
    context = security_context(organization_id=organization_id, roles=frozenset({"DataSteward"}))
    body = TermSemanticBindingCreate(term_id=term.id, semantic_object_id=metric.id)

    with pytest.raises(HTTPException) as denied:
        await create_term_semantic_binding(metric.id, body, context, session)  # type: ignore[arg-type]
    assert denied.value.status_code == 409


async def test_create_term_semantic_binding_rejects_a_mismatched_object_id() -> None:
    organization_id = uuid4()
    project = _project(organization_id)
    metric = _metric(organization_id, project.id)

    session = ScriptedSession(get_results={metric.id: metric, project.id: project})
    context = security_context(organization_id=organization_id, roles=frozenset({"DataSteward"}))
    body = TermSemanticBindingCreate(term_id=uuid4(), semantic_object_id=uuid4())

    with pytest.raises(HTTPException) as mismatched:
        await create_term_semantic_binding(metric.id, body, context, session)  # type: ignore[arg-type]
    assert mismatched.value.status_code == 422


async def test_create_term_semantic_binding_rejects_a_duplicate() -> None:
    organization_id = uuid4()
    project = _project(organization_id)
    metric = _metric(organization_id, project.id)
    term = _term(organization_id)
    approved = _approved_version(term)
    existing = TermSemanticBinding(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        semantic_object_type="METRIC",
        semantic_object_id=metric.id,
        status="ACTIVE",
        requested_by="someone-else",
    )

    session = ScriptedSession(
        get_results={metric.id: metric, project.id: project, term.id: term},
        scalar_results=[approved, existing],
    )
    context = security_context(organization_id=organization_id, roles=frozenset({"DataSteward"}))
    body = TermSemanticBindingCreate(term_id=term.id, semantic_object_id=metric.id)

    with pytest.raises(HTTPException) as conflict:
        await create_term_semantic_binding(metric.id, body, context, session)  # type: ignore[arg-type]
    assert conflict.value.status_code == 409


def _pending_review(
    organization_id: UUID, binding: TermSemanticBinding, *, requested_by: str
) -> GovernanceReview:
    return GovernanceReview(
        id=uuid4(),
        organization_id=organization_id,
        object_type="TERM_SEMANTIC_BINDING",
        object_id=str(binding.id),
        requested_action="BIND",
        status="PENDING",
        requested_by=requested_by,
    )


async def test_decide_governance_review_approves_a_binding() -> None:
    organization_id = uuid4()
    term = _term(organization_id)
    binding = TermSemanticBinding(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        semantic_object_type="METRIC",
        semantic_object_id=uuid4(),
        status="PENDING_APPROVAL",
        requested_by="binding-maker",
    )
    review = _pending_review(organization_id, binding, requested_by="binding-maker")

    session = ScriptedSession(scalar_results=[review], get_results={binding.id: binding})
    checker = security_context(
        organization_id=organization_id,
        principal_id="binding-checker",
        roles=frozenset({"Reviewer"}),
    )

    decided = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        checker,
        session,  # type: ignore[arg-type]
    )

    assert decided.status == "APPROVED"
    assert binding.status == "ACTIVE"
    assert binding.approved_by == "binding-checker"
    assert binding.approved_at is not None


async def test_decide_governance_review_rejects_a_binding() -> None:
    organization_id = uuid4()
    term = _term(organization_id)
    binding = TermSemanticBinding(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        semantic_object_type="METRIC",
        semantic_object_id=uuid4(),
        status="PENDING_APPROVAL",
        requested_by="binding-maker",
    )
    review = _pending_review(organization_id, binding, requested_by="binding-maker")

    session = ScriptedSession(scalar_results=[review], get_results={binding.id: binding})
    checker = security_context(
        organization_id=organization_id,
        principal_id="binding-checker",
        roles=frozenset({"Reviewer"}),
    )

    decided = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="REJECT", reason="Wrong metric."),
        checker,
        session,  # type: ignore[arg-type]
    )

    assert decided.status == "REJECTED"
    assert binding.status == "REJECTED"


async def test_decide_governance_review_blocks_self_approval_of_a_binding() -> None:
    organization_id = uuid4()
    term = _term(organization_id)
    binding = TermSemanticBinding(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        semantic_object_type="METRIC",
        semantic_object_id=uuid4(),
        status="PENDING_APPROVAL",
        requested_by="binding-maker",
    )
    review = _pending_review(organization_id, binding, requested_by="binding-maker")

    session = ScriptedSession(scalar_results=[review])
    maker = security_context(
        organization_id=organization_id,
        principal_id="binding-maker",
        roles=frozenset({"Reviewer"}),
    )

    with pytest.raises(HTTPException) as self_approval:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            maker,
            session,  # type: ignore[arg-type]
        )
    assert self_approval.value.status_code == 409


async def test_delete_term_semantic_binding_removes_it() -> None:
    organization_id = uuid4()
    binding = TermSemanticBinding(
        id=uuid4(),
        organization_id=organization_id,
        term_id=uuid4(),
        semantic_object_type="METRIC",
        semantic_object_id=uuid4(),
        status="ACTIVE",
        requested_by="binding-maker",
    )
    session = ScriptedSession(get_results={binding.id: binding})
    context = security_context(organization_id=organization_id, roles=frozenset({"DataSteward"}))

    await delete_term_semantic_binding(binding.id, context, session)  # type: ignore[arg-type]

    assert binding in session.deleted
    assert session.commits == 1


# ---------------------------------------------------------------------------
# 2 & 3. Bidirectional resolution and retrieval participation -- real engine
# ---------------------------------------------------------------------------


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
    """One organization's governance chain plus a published semantic metric
    and an approved glossary term, seeded directly through the ORM (bypassing
    the create/submit/approve endpoints, which write audit rows) against a
    real database -- the minimum needed to prove SM-2's read paths run real
    SQL, not a hand-simulated approximation of it.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Scenario":
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
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
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()

        self.measure_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="sale_amount",
            ordinal_position=1,
            physical_type="NUMERIC",
            nullable=False,
            fingerprint="fp-column",
        )
        db.add(self.measure_column)
        await db.flush()
        return self

    async def approved_term(
        self, *, term_key: str, display_name: str, definition: str, synonyms: list[str]
    ) -> GlossaryTerm:
        db = self.db
        term = GlossaryTerm(organization_id=self.organization.id, term_key=term_key)
        db.add(term)
        await db.flush()
        version = GlossaryTermVersion(
            organization_id=self.organization.id,
            term_id=term.id,
            version=1,
            status="APPROVED",
            display_name=display_name,
            definition=definition,
            synonyms=synonyms,
            created_by="term-maker",
            approved_by="term-checker",
            approved_at=datetime.now(UTC),
        )
        db.add(version)
        await db.flush()
        return term

    async def published_metric(self, *, slug: str, name: str, description: str) -> SemanticMetric:
        db = self.db
        model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=1,
            name=f"model-{slug}",
            change_summary="initial",
            status="PUBLISHED",
            created_by="metric-maker",
        )
        db.add(model)
        await db.flush()
        metric = SemanticMetric(
            organization_id=self.organization.id, project_id=self.project.id, slug=slug
        )
        db.add(metric)
        await db.flush()
        version = SemanticMetricVersion(
            organization_id=self.organization.id,
            semantic_model_version_id=model.id,
            metric_id=metric.id,
            version=1,
            status="PUBLISHED",
            name=name,
            description=description,
            aggregation="SUM",
            grain="daily",
            source_table_id=self.table.id,
            measure_column_id=self.measure_column.id,
            fingerprint=f"fp-{slug}",
            created_by="metric-maker",
        )
        db.add(version)
        await db.flush()
        return metric

    async def binding(
        self, *, term_id: UUID, metric_id: UUID, status: str = "ACTIVE"
    ) -> TermSemanticBinding:
        db = self.db
        binding = TermSemanticBinding(
            organization_id=self.organization.id,
            term_id=term_id,
            semantic_object_type="METRIC",
            semantic_object_id=metric_id,
            status=status,
            requested_by="binding-maker",
            approved_by="binding-checker" if status == "ACTIVE" else None,
            approved_at=datetime.now(UTC) if status == "ACTIVE" else None,
        )
        db.add(binding)
        await db.flush()
        return binding

    def steward(self):
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"DataSteward"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_binding_resolves_in_both_directions(scenario: _Scenario) -> None:
    term = await scenario.approved_term(
        term_key="bidirectional-term",
        display_name="Bidirectional Term",
        definition="Used to prove term->object and object->term both resolve.",
        synonyms=[],
    )
    metric = await scenario.published_metric(
        slug="bidirectional_metric",
        name="Bidirectional Metric",
        description="The metric the bidirectional-term binding targets.",
    )
    await scenario.binding(term_id=term.id, metric_id=metric.id)

    from_term = await list_term_semantic_bindings(
        term.id,
        binding_status="ACTIVE",
        limit=100,
        offset=0,
        context=scenario.steward(),
        session=scenario.db,
    )
    assert from_term.total == 1
    assert from_term.items[0].semantic_object_id == metric.id
    assert from_term.items[0].semantic_object_type == "METRIC"

    from_object = await list_metric_glossary_bindings(
        metric.id,
        binding_status="ACTIVE",
        limit=100,
        offset=0,
        context=scenario.steward(),
        session=scenario.db,
    )
    assert from_object.total == 1
    assert from_object.items[0].term_id == term.id
    assert from_object.items[0].term_display_name == "Bidirectional Term"


async def test_bound_term_definition_and_synonyms_are_retrievable_via_the_metric(
    scenario: _Scenario,
) -> None:
    """A search that only matches the *term's* synonym still surfaces the
    metric, because the ACTIVE binding folds the term's text into the
    metric's candidate text -- this is what makes the binding participate in
    retrieval rather than sit as a dead link."""
    term = await scenario.approved_term(
        term_key="topline-revenue",
        display_name="Topline Revenue",
        definition="The primary revenue figure reported to the board each quarter.",
        synonyms=["booked revenue figure"],
    )
    metric = await scenario.published_metric(
        slug="quarterly_net_sales",
        name="Quarterly Net Sales",
        description="Aggregated net sales total used for quarterly operational reporting.",
    )
    await scenario.binding(term_id=term.id, metric_id=metric.id)

    # "booked" appears only in the term's synonym -- never in the metric's own
    # name/description/slug.
    hits = await hybrid_retrieve(
        scenario.db,
        datasource=scenario.datasource,
        question="booked revenue figure",
        settings=Settings(_env_file=None),
    )
    metric_hits = [h for h in hits if h.object_type == "SEMANTIC_METRIC"]
    assert metric_hits, "bound term synonym did not surface the semantic metric in retrieval"
    metric_hit = metric_hits[0]
    assert metric_hit.object_id == str(metric.id)
    assert metric_hit.score > 0
    assert str(term.id) in metric_hit.metadata["bound_term_ids"]
    assert "GLOSSARY_TERM_BOUND" in metric_hit.reason_codes


async def test_glossary_term_hit_surfaces_its_bound_semantic_object(scenario: _Scenario) -> None:
    """The other direction: searching on the term's own text surfaces a
    GLOSSARY_TERM hit whose metadata names the semantic object it is bound
    to, so a term result can resolve forward to the metric it governs."""
    term = await scenario.approved_term(
        term_key="board-metric",
        display_name="Board Reported Figure",
        definition="A figure specifically escalated to the board each quarter.",
        synonyms=[],
    )
    metric = await scenario.published_metric(
        slug="board_metric",
        name="Escalation Total",
        description="An unrelated metric name and description on purpose.",
    )
    await scenario.binding(term_id=term.id, metric_id=metric.id)

    hits = await hybrid_retrieve(
        scenario.db,
        datasource=scenario.datasource,
        question="board reported figure",
        settings=Settings(_env_file=None),
    )
    term_hits = [h for h in hits if h.object_type == "GLOSSARY_TERM"]
    assert term_hits, "the term's own text did not surface a GLOSSARY_TERM hit"
    term_hit = term_hits[0]
    assert term_hit.object_id == str(term.id)
    assert str(metric.id) in term_hit.metadata["bound_semantic_object_ids"]


async def test_a_binding_that_never_activated_does_not_leak_into_retrieval(
    scenario: _Scenario,
) -> None:
    """A PENDING_APPROVAL (never approved) binding must not affect retrieval
    -- otherwise "reviewable" would be theater. Proves the participation
    above is gated on the binding actually being ACTIVE."""
    term = await scenario.approved_term(
        term_key="pending-term",
        display_name="Pending Term",
        definition="A definition that should not leak into retrieval unapproved.",
        synonyms=["unapproved leakage phrase"],
    )
    metric = await scenario.published_metric(
        slug="pending_binding_metric",
        name="Pending Binding Metric",
        description="Placeholder metric text with no organic overlap with any glossary term.",
    )
    await scenario.binding(term_id=term.id, metric_id=metric.id, status="PENDING_APPROVAL")

    hits = await hybrid_retrieve(
        scenario.db,
        datasource=scenario.datasource,
        question="unapproved leakage phrase",
        settings=Settings(_env_file=None),
    )
    metric_hits = [h for h in hits if h.object_type == "SEMANTIC_METRIC"]
    assert not metric_hits, "a PENDING_APPROVAL binding must not participate in retrieval"
