"""AT-11 -- classification propagation along lineage, derived kept separate from
asserted.

Proves each clause of the tracker's exit condition, in the order it states them:

1. ``classification_derived`` is stored separately from ``classification_asserted``
   -- a raised value lands in ``ColumnDerivedClassification`` and never touches
   ``MetadataColumn.classification`` (the asserted, policy-enforced value).
2. Propagation is raise-only: it moves a column toward a more-restrictive
   classification and never lowers one.
3. Propagation flows along ``DECLARED`` / ``VIEW_DDL`` / ``EXECUTED_QUERY`` /
   ``OPENLINEAGE`` edges and never along ``INFLUENCES``.
4. The edge chain and graph version are recorded as the derived value's evidence.
5. A derived classification does not become asserted without going through the
   maker-checker review path.

The pure-engine clauses are proven against ``propagate`` directly; the storage,
separation and review-gate clauses run the real DB-facing functions and the real
governance-review dispatcher against the in-memory SQLite harness from
``tests/test_asset_evidence.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.classification_propagation import (
    EDGE_SOURCE_TO_PROPAGATION_KIND,
    NON_PROPAGATING_EDGE_KINDS,
    PROPAGATING_EDGE_KINDS,
    PropagationEdge,
    collect_propagation_inputs,
    get_current_derived_classification,
    graph_fingerprint,
    is_more_restrictive,
    propagate,
    propagate_for_datasource,
    propagation_kind_for_edge_source,
    sensitivity_rank,
    store_derived_classifications,
    submit_classification_promotion,
)
from aida.db import Base
from aida.models import (
    ClassificationEvidence,
    ColumnDerivedClassification,
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    ViewLineageEdge,
)
from aida.semantic_api import _apply_governance_review_decision
from tests.support.doubles import security_context

# asyncio_mode = "auto" (pyproject) auto-detects the async tests here; the sync
# pure-engine tests in this file must stay unmarked, so there is no global mark.

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_table(session: AsyncSession) -> tuple[Organization, MetadataTable]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name=f"src-{uuid4().hex[:8]}",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="accounts",
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return org, table


async def _seed_column(
    session: AsyncSession,
    org: Organization,
    table: MetadataTable,
    *,
    name: str,
    ordinal: int,
    classification: str = "UNCLASSIFIED",
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name=name,
        ordinal_position=ordinal,
        physical_type="text",
        nullable=True,
        classification=classification,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


# --------------------------------------------------------------------------- #
# Clause 2 -- raise-only ordering
# --------------------------------------------------------------------------- #


def test_sensitivity_order_is_a_documented_total_order() -> None:
    assert sensitivity_rank("UNCLASSIFIED") < sensitivity_rank("CONFIDENTIAL")
    assert sensitivity_rank("CONFIDENTIAL") < sensitivity_rank("PII")
    assert sensitivity_rank("PII") < sensitivity_rank("PCI")
    assert sensitivity_rank("PCI") < sensitivity_rank("PHI")
    assert sensitivity_rank("PHI") < sensitivity_rank("SECRET")
    # Unknown labels never outrank anything -- they cannot raise a column.
    assert sensitivity_rank("SOMETHING_NEW") == 0
    assert is_more_restrictive("PII", "UNCLASSIFIED")
    assert not is_more_restrictive("UNCLASSIFIED", "PII")


def test_propagation_raises_but_never_lowers() -> None:
    edges = [PropagationEdge("up", "down", "DECLARED", edge_ref="e1")]

    # A more-sensitive origin raises the downstream column.
    raised = propagate(asserted={"up": "PII", "down": "UNCLASSIFIED"}, edges=edges)
    assert [(a.node_id, a.classification) for a in raised] == [("down", "PII")]

    # A less-sensitive origin never lowers the downstream column: no assignment.
    lowered = propagate(asserted={"up": "UNCLASSIFIED", "down": "PII"}, edges=edges)
    assert lowered == []

    # An origin less sensitive than the target's existing value is a no-op too.
    no_raise = propagate(asserted={"up": "CONFIDENTIAL", "down": "PII"}, edges=edges)
    assert no_raise == []


# --------------------------------------------------------------------------- #
# Clause 3 -- edge-kind restriction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", sorted(PROPAGATING_EDGE_KINDS))
def test_propagates_along_each_authoritative_edge_kind(kind: str) -> None:
    edges = [PropagationEdge("up", "down", kind, edge_ref="e1")]
    raised = propagate(asserted={"up": "PII"}, edges=edges)
    assert [(a.node_id, a.classification) for a in raised] == [("down", "PII")]


def test_never_propagates_along_influences() -> None:
    assert "INFLUENCES" in NON_PROPAGATING_EDGE_KINDS
    assert "INFLUENCES" not in PROPAGATING_EDGE_KINDS
    edges = [PropagationEdge("up", "down", "INFLUENCES", edge_ref="e1")]
    assert propagate(asserted={"up": "PII"}, edges=edges) == []


def test_mixed_graph_only_follows_authoritative_edges() -> None:
    # up --DECLARED--> mid --INFLUENCES--> down. PII reaches mid but must stop:
    # the second hop is an inferred relationship and cannot carry classification.
    edges = [
        PropagationEdge("up", "mid", "DECLARED", edge_ref="e1"),
        PropagationEdge("mid", "down", "INFLUENCES", edge_ref="e2"),
    ]
    raised = {a.node_id: a.classification for a in propagate(asserted={"up": "PII"}, edges=edges)}
    assert raised == {"mid": "PII"}
    assert "down" not in raised


def test_edge_source_literal_mapping() -> None:
    # The documented mapping from the unified graph's edge_source literals.
    assert EDGE_SOURCE_TO_PROPAGATION_KIND["FOREIGN_KEY"] == "DECLARED"
    assert EDGE_SOURCE_TO_PROPAGATION_KIND["VIEW_DEFINITION"] == "VIEW_DDL"
    assert EDGE_SOURCE_TO_PROPAGATION_KIND["OPENLINEAGE_ETL"] == "OPENLINEAGE"
    assert EDGE_SOURCE_TO_PROPAGATION_KIND["SUGGESTED_RELATIONSHIP"] == "INFLUENCES"
    # SUGGESTED_RELATIONSHIP (an inferred candidate) is non-propagating, and an
    # unknown edge_source is treated conservatively as non-propagating too.
    assert propagation_kind_for_edge_source("SUGGESTED_RELATIONSHIP") in NON_PROPAGATING_EDGE_KINDS
    assert propagation_kind_for_edge_source("SOMETHING_UNKNOWN") in NON_PROPAGATING_EDGE_KINDS


# --------------------------------------------------------------------------- #
# Clause 4 -- edge chain + graph version are the evidence
# --------------------------------------------------------------------------- #


def test_edge_chain_and_graph_version_are_recorded() -> None:
    edges = [
        PropagationEdge("origin", "mid", "DECLARED", edge_ref="e1"),
        PropagationEdge("mid", "leaf", "VIEW_DDL", edge_ref="e2"),
    ]
    raised = propagate(asserted={"origin": "PHI"}, edges=edges)
    (assignment,) = [a for a in raised if a.node_id == "leaf"]
    # The ordered chain of edges the classification travelled, origin -> leaf.
    assert [e["edge_ref"] for e in assignment.edge_chain] == ["e1", "e2"]
    assert [e["kind"] for e in assignment.edge_chain] == ["DECLARED", "VIEW_DDL"]
    assert assignment.path_nodes == ("origin", "mid", "leaf")
    assert assignment.origin_node_id == "origin"

    # The graph version is a stable fingerprint, order-independent.
    v1 = graph_fingerprint(edges)
    v2 = graph_fingerprint(list(reversed(edges)))
    assert v1 == v2 and len(v1) == 64


# --------------------------------------------------------------------------- #
# Clause 1 -- derived stored separately from asserted
# --------------------------------------------------------------------------- #


async def test_derived_is_stored_separately_from_asserted(session) -> None:
    org, table = await _seed_table(session)
    src = await _seed_column(session, org, table, name="ssn", ordinal=1, classification="PII")
    dst = await _seed_column(session, org, table, name="ssn_copy", ordinal=2)
    await session.flush()

    edges = [PropagationEdge(str(src.id), str(dst.id), "DECLARED", edge_ref="fk1")]
    stored = await store_derived_classifications(
        session,
        organization_id=org.id,
        asserted={str(src.id): "PII", str(dst.id): "UNCLASSIFIED"},
        edges=edges,
        created_by="propagation-engine",
    )

    # A derived row exists for the downstream column, carrying its evidence...
    assert len(stored) == 1
    derived = stored[0]
    assert derived.column_id == dst.id
    assert derived.classification == "PII"
    assert derived.origin_column_id == src.id
    assert derived.graph_version == graph_fingerprint(edges)
    assert [e["edge_ref"] for e in derived.edge_chain] == ["fk1"]
    assert derived.status == "DERIVED"

    # ...but the asserted classification on the column is untouched. The two are
    # stored in different tables; the derived value never merged into the asserted.
    await session.refresh(dst)
    assert dst.classification == "UNCLASSIFIED"
    assert dst.classification_source == "RULE"


async def test_rerun_supersedes_prior_current_derived_row(session) -> None:
    org, table = await _seed_table(session)
    src = await _seed_column(session, org, table, name="ssn", ordinal=1, classification="PII")
    dst = await _seed_column(session, org, table, name="ssn_copy", ordinal=2)
    await session.flush()
    edges = [PropagationEdge(str(src.id), str(dst.id), "DECLARED", edge_ref="fk1")]
    kwargs = dict(organization_id=org.id, edges=edges, created_by="engine")
    asserted = {str(src.id): "PII", str(dst.id): "UNCLASSIFIED"}

    await store_derived_classifications(session, asserted=asserted, **kwargs)
    await store_derived_classifications(session, asserted=asserted, **kwargs)

    rows = (
        await session.scalars(
            select(ColumnDerivedClassification).where(
                ColumnDerivedClassification.column_id == dst.id
            )
        )
    ).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.is_current) == 1


# --------------------------------------------------------------------------- #
# Clause 5 -- derived never becomes asserted without the review queue
# --------------------------------------------------------------------------- #


async def _seed_derived(session: AsyncSession):
    org, table = await _seed_table(session)
    src = await _seed_column(session, org, table, name="ssn", ordinal=1, classification="PII")
    dst = await _seed_column(session, org, table, name="ssn_copy", ordinal=2)
    await session.flush()
    edges = [PropagationEdge(str(src.id), str(dst.id), "DECLARED", edge_ref="fk1")]
    (derived,) = await store_derived_classifications(
        session,
        organization_id=org.id,
        asserted={str(src.id): "PII", str(dst.id): "UNCLASSIFIED"},
        edges=edges,
        created_by="engine",
    )
    return org, dst, derived


async def test_submitting_promotion_does_not_assert_until_approved(session) -> None:
    org, dst, derived = await _seed_derived(session)
    maker = security_context(organization_id=org.id, principal_id="maker")

    review = await submit_classification_promotion(
        session, maker, derived=derived, correlation_id="corr-1"
    )

    # A pending review now exists; the derived row is marked pending; and crucially
    # the column's asserted classification is STILL unchanged.
    assert review.object_type == "COLUMN_CLASSIFICATION_PROMOTION"
    assert review.status == "PENDING"
    assert review.requested_by == "maker"
    assert derived.status == "PROMOTION_PENDING"
    await session.refresh(dst)
    assert dst.classification == "UNCLASSIFIED"


async def test_approval_through_the_review_dispatcher_promotes_to_asserted(session) -> None:
    org, dst, derived = await _seed_derived(session)
    maker = security_context(organization_id=org.id, principal_id="maker")
    review = await submit_classification_promotion(
        session, maker, derived=derived, correlation_id="corr-1"
    )

    # The real governance-review dispatcher (the one the decide endpoint calls
    # after enforcing maker != checker) applies the decision.
    checker = security_context(organization_id=org.id, principal_id="checker")
    event_type, aggregate_type, aggregate_id, payload = await _apply_governance_review_decision(
        session,
        review,
        decision="APPROVE",
        reason="reviewed and correct",
        context=checker,
        now=_NOW,
    )

    assert event_type == "classification.derived.promoted.v1"
    assert aggregate_type == "column_derived_classification"
    assert aggregate_id == str(derived.id)
    assert payload["column_id"] == str(dst.id)
    await session.flush()

    # Only now is the derived value asserted on the column, sourced as promoted.
    assert dst.classification == "PII"
    assert dst.classification_source == "DERIVED_PROMOTED"
    assert derived.status == "PROMOTED"
    assert derived.promoted_by == "checker"

    # And the promotion left an auditable ClassificationEvidence row carrying the
    # derived provenance (edge chain + graph version).
    evidence = await session.scalar(
        select(ClassificationEvidence).where(
            ClassificationEvidence.column_id == dst.id,
            ClassificationEvidence.is_current.is_(True),
        )
    )
    assert evidence is not None
    assert evidence.source_type == "DERIVED_PROMOTED"
    assert evidence.matched_signal["graph_version"] == derived.graph_version
    assert evidence.matched_signal["edge_chain"] == derived.edge_chain


async def test_rejection_through_the_review_dispatcher_leaves_asserted_untouched(session) -> None:
    org, dst, derived = await _seed_derived(session)
    maker = security_context(organization_id=org.id, principal_id="maker")
    review = await submit_classification_promotion(
        session, maker, derived=derived, correlation_id="corr-1"
    )

    checker = security_context(organization_id=org.id, principal_id="checker")
    event_type, _, _, _ = await _apply_governance_review_decision(
        session,
        review,
        decision="REJECT",
        reason="not warranted",
        context=checker,
        now=_NOW,
    )

    assert event_type == "classification.derived.promotion_rejected.v1"
    assert derived.status == "PROMOTION_REJECTED"
    await session.flush()
    assert dst.classification == "UNCLASSIFIED"  # asserted value never changed


async def test_read_side_returns_current_derived_with_evidence(session) -> None:
    org, dst, derived = await _seed_derived(session)
    read = await get_current_derived_classification(
        session, organization_id=org.id, column_id=dst.id
    )
    assert read is not None
    assert read.id == derived.id
    assert read.classification == "PII"
    assert read.edge_chain and read.graph_version

    # Organization-scoped: a foreign tenant reads nothing.
    assert (
        await get_current_derived_classification(
            session, organization_id=uuid4(), column_id=dst.id
        )
        is None
    )


# ---------------------------------------------------------------------------
# R11-B17: the producer. Until this, nothing built propagation edges from real
# lineage, so an estate had AT-11's review and apply half and never a derived
# row. These prove the collector's three rules against real edge tables.
# ---------------------------------------------------------------------------


async def _seed_second_table(
    session: AsyncSession, org: Organization, first: MetadataTable, name: str
) -> MetadataTable:
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=first.datasource_id,
        schema_id=first.schema_id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


def _view_edge(
    org: Organization,
    source: MetadataTable,
    source_column: str,
    target: MetadataTable,
    target_column: str,
    *,
    review_status: str = "ACTIVE",
) -> ViewLineageEdge:
    return ViewLineageEdge(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=source.datasource_id,
        source_table=source.name,
        source_column=source_column,
        target_table=target.name,
        target_column=target_column,
        source_table_id=source.id,
        target_table_id=target.id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="postgres",
        sql_hash="h" * 64,
        review_status=review_status,
    )


async def test_r11b17_a_reviewed_edge_derives_a_classification_without_asserting_it(
    session: AsyncSession,
) -> None:
    org, source_table = await _seed_table(session)
    target_table = await _seed_second_table(session, org, source_table, "accounts_v")
    upstream = await _seed_column(
        session, org, source_table, name="ssn", ordinal=1, classification="PII"
    )
    downstream = await _seed_column(
        session, org, target_table, name="ssn", ordinal=1, classification="UNCLASSIFIED"
    )
    session.add(_view_edge(org, source_table, "ssn", target_table, "ssn"))
    await session.flush()

    written = await propagate_for_datasource(
        session,
        organization_id=org.id,
        datasource_id=source_table.datasource_id,
        created_by="scheduler",
    )
    await session.flush()

    assert [row.column_id for row in written] == [downstream.id]
    assert written[0].classification == "PII"
    assert written[0].origin_column_id == upstream.id
    # The whole point of AT-11: the enforced value is untouched.
    refreshed = await session.get(MetadataColumn, downstream.id)
    assert refreshed is not None
    assert refreshed.classification == "UNCLASSIFIED"


async def test_r11b17_an_unreviewed_edge_never_raises_an_enforcement_input(
    session: AsyncSession,
) -> None:
    """A PROPOSED edge is a parser's guess nobody confirmed. Deriving from it
    would raise an ABAC input off unreviewed evidence -- the same mistake the
    engine already refuses for INFLUENCES edges."""
    org, source_table = await _seed_table(session)
    target_table = await _seed_second_table(session, org, source_table, "accounts_v")
    await _seed_column(
        session, org, source_table, name="ssn", ordinal=1, classification="PII"
    )
    await _seed_column(
        session, org, target_table, name="ssn", ordinal=1, classification="UNCLASSIFIED"
    )
    session.add(
        _view_edge(org, source_table, "ssn", target_table, "ssn", review_status="PROPOSED")
    )
    await session.flush()

    written = await propagate_for_datasource(
        session,
        organization_id=org.id,
        datasource_id=source_table.datasource_id,
        created_by="scheduler",
    )

    assert written == []


async def test_r11b17_a_column_name_shared_across_tables_is_not_a_false_edge(
    session: AsyncSession,
) -> None:
    """Column names are unique within a table, not across one. Resolving an
    edge end by name alone would merge two unrelated columns and propagate a
    classification along an edge that does not exist."""
    org, source_table = await _seed_table(session)
    target_table = await _seed_second_table(session, org, source_table, "accounts_v")
    unrelated = await _seed_second_table(session, org, source_table, "customers")
    await _seed_column(
        session, org, source_table, name="ssn", ordinal=1, classification="PII"
    )
    await _seed_column(
        session, org, target_table, name="ssn", ordinal=1, classification="UNCLASSIFIED"
    )
    # Same column name, different table, no edge to it.
    bystander = await _seed_column(
        session, org, unrelated, name="ssn", ordinal=1, classification="UNCLASSIFIED"
    )
    session.add(_view_edge(org, source_table, "ssn", target_table, "ssn"))
    await session.flush()

    written = await propagate_for_datasource(
        session,
        organization_id=org.id,
        datasource_id=source_table.datasource_id,
        created_by="scheduler",
    )

    assert bystander.id not in {row.column_id for row in written}


async def test_r11b17_the_collector_bound_truncates_and_says_so(
    session: AsyncSession,
) -> None:
    org, source_table = await _seed_table(session)
    target_table = await _seed_second_table(session, org, source_table, "accounts_v")
    for index in range(3):
        await _seed_column(
            session, org, source_table, name=f"c{index}", ordinal=index, classification="PII"
        )
        await _seed_column(
            session, org, target_table, name=f"c{index}", ordinal=index
        )
        session.add(_view_edge(org, source_table, f"c{index}", target_table, f"c{index}"))
    await session.flush()

    inputs = await collect_propagation_inputs(
        session,
        organization_id=org.id,
        datasource_id=source_table.datasource_id,
        max_edges=2,
    )

    assert len(inputs.edges) == 2
    assert inputs.truncated is True


async def test_r11b17_a_label_outside_the_lattice_never_raises_anything(
    session: AsyncSession,
) -> None:
    """A classification the lattice does not know ranks as UNCLASSIFIED, so it
    cannot raise a downstream column. Worth pinning at the producer level: the
    collector reads whatever labels an estate's feeds happen to write, and an
    unrecognised one erring toward under-propagation is the safe direction."""
    org, source_table = await _seed_table(session)
    target_table = await _seed_second_table(session, org, source_table, "accounts_v")
    await _seed_column(
        session, org, source_table, name="ssn", ordinal=1, classification="RESTRICTED"
    )
    await _seed_column(
        session, org, target_table, name="ssn", ordinal=1, classification="UNCLASSIFIED"
    )
    session.add(_view_edge(org, source_table, "ssn", target_table, "ssn"))
    await session.flush()

    written = await propagate_for_datasource(
        session,
        organization_id=org.id,
        datasource_id=source_table.datasource_id,
        created_by="scheduler",
    )

    assert written == []


async def test_r11b17_the_scheduler_pass_is_off_until_an_operator_sets_an_interval() -> None:
    """Default 0 means never, the same convention the task agents use. Asserted
    by proving it opens no session at all: a pass that returned 0 after querying
    every datasource would still be a per-iteration cost on every deployment
    that never asked for it."""
    from aida.config import Settings
    from aida.workflows import scheduler

    def fail_session() -> object:
        raise AssertionError("the propagation pass must not open a session when off")

    original = scheduler.session_factory
    scheduler.session_factory = fail_session  # type: ignore[assignment]
    try:
        settings = Settings(_env_file=None)
        assert settings.classification_propagation_interval_minutes == 0
        swept = await scheduler.run_classification_propagation_pass(settings)
    finally:
        scheduler.session_factory = original  # type: ignore[assignment]

    assert swept == 0
