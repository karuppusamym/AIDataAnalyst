"""AT-16 -- provenance block in the answer contract.

Before this change, `AgentRun.plan_evidence` -- the answer contract's own
evidence field -- carried the answer's cited tables only via
`QueryExecution.referenced_tables` (bare names, used solely to gate on open
quality incidents in `_checkpoint_explained`). No column-level detail, no
derivation method, no notion of which lineage-graph state was consulted.

Two things this file proves, driving the real orchestrator
(`GovernedAgentOrchestrator.run`) against a real in-memory SQLite database,
the same harness `test_agent_orchestrator_checkpoints.py` uses:

1. `test_lineage_provenance_carries_columns_derivation_and_pinned_version`:
   a real completed answer's `plan_evidence["lineage"]` block carries
   specific columns and an `edge_source` derivation method per cited
   relationship, plus a pinned graph version -- not table names alone.
2. `test_pinned_graph_version_survives_a_later_graph_change`: re-fetching
   the SAME already-completed `AgentRun` returns the SAME pinned graph
   version even after the live lineage graph has since changed -- the pin
   was captured once at answer time, not recomputed on read.
"""

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.answer_provenance import compose_lineage_provenance
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AgentRun,
    AnalysisRun,
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.quality_coupling import resolve_table_ids
from tests.support.doubles import FakeSqlExecutor, security_context

pytestmark = pytest.mark.asyncio

QUESTION = "show account balance and party summary"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


class _Fixture:
    def __init__(
        self,
        organization: Organization,
        datasource: DataSource,
        ledger_table_id: object,
        party_table_id: object,
        tool_version_id: object,
    ):
        self.organization = organization
        self.datasource = datasource
        self.ledger_table_id = ledger_table_id
        self.party_table_id = party_table_id
        self.tool_version_id = tool_version_id


async def _seed(session: AsyncSession) -> _Fixture:
    """Two tables joined by a declared foreign key, and one governed tool that
    selects across both -- enough for a unified-lineage `FOREIGN_KEY` edge
    with real column-level detail to exist between the answer's cited tables.
    """
    organization = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=organization.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Retail Banking",
        code=f"RB{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core Banking",
        slug=f"core-banking-{uuid4().hex[:6]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="core-warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
    )
    session.add_all([organization, lob, domain, project, datasource])
    await session.flush()

    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=organization.id,
        datasource_id=datasource.id,
        name="core",
        fingerprint="fp-catalog",
    )
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization.id,
        catalog_id=catalog.id,
        name="retail",
        fingerprint="fp-schema",
    )
    ledger_table = MetadataTable(
        id=uuid4(),
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="settlement_ledger",
        object_type="TABLE",
        fingerprint="fp-ledger",
        source_description="Immutable record of settled transfers",
    )
    party_table = MetadataTable(
        id=uuid4(),
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="party",
        object_type="TABLE",
        fingerprint="fp-party",
        source_description="Counterparty master",
    )
    session.add_all([catalog, schema, ledger_table, party_table])
    await session.flush()
    session.add_all(
        [
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=ledger_table.id,
                name="party_ref",
                ordinal_position=1,
                physical_type="TEXT",
                nullable=False,
                fingerprint="fp-col-party-ref",
            ),
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=ledger_table.id,
                name="amount_value",
                ordinal_position=2,
                physical_type="NUMERIC",
                nullable=False,
                fingerprint="fp-col-amount-value",
            ),
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=party_table.id,
                name="id",
                ordinal_position=1,
                physical_type="TEXT",
                nullable=False,
                fingerprint="fp-col-party-id",
            ),
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=party_table.id,
                name="party_name",
                ordinal_position=2,
                physical_type="TEXT",
                nullable=False,
                fingerprint="fp-col-party-name",
            ),
        ]
    )

    session.add(
        MetadataConstraint(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            table_id=ledger_table.id,
            name="fk_ledger_party",
            constraint_type="FOREIGN_KEY",
            columns=["party_ref"],
            referenced_table_id=party_table.id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp-fk-ledger-party",
        )
    )

    # AU-5/C3's own exit criterion needs a COMPLETED analysis run for `run()`
    # to get past its "has metadata ever been analysed" gate.
    session.add(
        AnalysisRun(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            status="COMPLETED",
        )
    )

    tool = GovernedTool(
        id=uuid4(), organization_id=organization.id, project_id=project.id, slug="balance_tools"
    )
    session.add(tool)
    await session.flush()

    tool_a = GovernedToolVersion(
        id=uuid4(),
        organization_id=organization.id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Account Balance And Party Summary",
        description="Approved governed account balance and party summary report",
        datasource_id=datasource.id,
        sql_template=(
            "SELECT l.party_ref, l.amount_value, p.party_name "
            "FROM retail.settlement_ledger l "
            "JOIN retail.party p ON l.party_ref = p.id"
        ),
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint="fp-tool-a",
        created_by="tool-dev",
    )
    session.add(tool_a)
    await session.commit()
    return _Fixture(organization, datasource, ledger_table.id, party_table.id, tool_a.id)


def _orchestrator(monkeypatch: pytest.MonkeyPatch) -> GovernedAgentOrchestrator:
    settings = Settings(agent_retrieval_limit=5, _env_file=None)
    orchestrator = GovernedAgentOrchestrator(settings)
    executor = FakeSqlExecutor(
        (
            {"party_ref": "PARTY-1", "amount_value": 100, "party_name": "Acme Corp"},
        )
    )
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )
    return orchestrator


async def _run_to_completion(
    session: AsyncSession, fixture: _Fixture, orchestrator: GovernedAgentOrchestrator
) -> AgentRun:
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )
    result = await orchestrator.run(
        session,
        datasource=fixture.datasource,
        context=context,
        correlation_id="corr-provenance",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=fixture.tool_version_id,
        tool_parameters={},
        requested_limit=None,
    )
    assert result.agent_run.status == "COMPLETED"
    return result.agent_run


async def test_lineage_provenance_carries_columns_derivation_and_pinned_version(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await _seed(session)
    orchestrator = _orchestrator(monkeypatch)

    agent_run = await _run_to_completion(session, fixture, orchestrator)

    lineage = agent_run.plan_evidence.get("lineage")
    assert lineage is not None, "expected a lineage provenance block on a completed answer"

    # -- cited tables, by qualified name (not bare names alone) --
    cited_qualified_names = {row["qualified_name"] for row in lineage["cited_tables"]}
    assert cited_qualified_names == {"core.retail.settlement_ledger", "core.retail.party"}

    # -- the answer's own queried columns, not just table names --
    assert lineage["queried_columns"], "expected the answer's queried columns to be recorded"
    assert any("party_ref" in column for column in lineage["queried_columns"])

    # -- the relationship between the cited tables carries columns and a
    # derivation method (edge_source), reusing AT-19's existing taxonomy --
    relationships = lineage["relationships"]
    assert len(relationships) == 1
    relationship = relationships[0]
    assert relationship["edge_source"] == "FOREIGN_KEY"
    assert relationship["source_columns"] == ["party_ref"]
    assert relationship["target_columns"] == ["id"]
    assert relationship["status"] == "DECLARED"
    assert relationship["source_table"]["qualified_name"] == "core.retail.settlement_ledger"
    assert relationship["target_table"]["qualified_name"] == "core.retail.party"

    # -- the pinned graph version: not a live-recomputable value, a captured one --
    graph_version = lineage["graph_version"]
    assert graph_version["datasource_id"] == str(fixture.datasource.id)
    assert graph_version["graph_content_fingerprint"]
    assert len(graph_version["graph_content_fingerprint"]) == 64  # sha256 hex digest
    pinned_at = datetime.fromisoformat(graph_version["pinned_at"])
    assert pinned_at.tzinfo is not None
    assert graph_version["traversal"]["node_limit"] > 0
    assert graph_version["traversal"]["edge_limit"] > 0


async def test_pinned_graph_version_survives_a_later_graph_change(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin is captured once at answer-completion time; re-fetching the
    same completed `AgentRun` later must return the identical pin even after
    the live lineage graph has changed -- proving the value is stored, not
    recomputed on read.
    """
    fixture = await _seed(session)
    orchestrator = _orchestrator(monkeypatch)

    agent_run = await _run_to_completion(session, fixture, orchestrator)
    run_id = agent_run.id
    original_lineage = agent_run.plan_evidence["lineage"]
    original_fingerprint = original_lineage["graph_version"]["graph_content_fingerprint"]
    original_pinned_at = original_lineage["graph_version"]["pinned_at"]
    original_relationship_count = len(original_lineage["relationships"])

    # Mutate the live lineage graph: add a second, brand-new declared FK
    # between the same two cited tables. A fresh traversal now sees a
    # different graph than the one this answer was pinned against.
    session.add(
        MetadataConstraint(
            id=uuid4(),
            organization_id=fixture.organization.id,
            datasource_id=fixture.datasource.id,
            table_id=fixture.ledger_table_id,
            name="fk_ledger_party_secondary",
            constraint_type="FOREIGN_KEY",
            columns=["amount_value"],
            referenced_table_id=fixture.party_table_id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp-fk-ledger-party-2",
        )
    )
    await session.commit()

    # Re-fetch the SAME already-completed AgentRun the way `GET
    # /v1/agent-runs/{id}` does (`aida.api`): load the persisted row and
    # return its stored `plan_evidence` -- no recomputation.
    refetched = await session.get(AgentRun, run_id)
    assert refetched is not None
    refetched_lineage = refetched.plan_evidence["lineage"]
    assert (
        refetched_lineage["graph_version"]["graph_content_fingerprint"] == original_fingerprint
    ), "the stored pin must not change just because the live graph changed"
    assert refetched_lineage["graph_version"]["pinned_at"] == original_pinned_at
    assert len(refetched_lineage["relationships"]) == original_relationship_count

    # Prove the mutation was real: composing the block LIVE against the
    # current (changed) graph now produces a different fingerprint and an
    # extra relationship -- so the stored pin's stability above is not an
    # accident of nothing having changed.
    answer_table_ids = await resolve_table_ids(
        session,
        datasource=fixture.datasource,
        table_names=["retail.settlement_ledger", "retail.party"],
    )
    live_lineage = await compose_lineage_provenance(
        session,
        datasource=fixture.datasource,
        answer_table_ids=answer_table_ids,
        queried_columns=["party_ref", "amount_value", "party_name"],
        settings=orchestrator.settings,
    )
    assert live_lineage is not None
    assert len(live_lineage["relationships"]) == original_relationship_count + 1
    assert (
        live_lineage["graph_version"]["graph_content_fingerprint"] != original_fingerprint
    ), "a live recompute against the changed graph must diverge from the stored pin"
