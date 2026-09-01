"""AT-20 -- lineage evidence export as a signed artifact.

`GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}/export`
(`aida.lineage_evidence_export_api.export_unified_lineage_impact`), composed
by `aida.lineage_evidence_export.compose_lineage_export_artifact`. Runs the
real endpoint bodies against an in-memory SQLite database, following
`test_unified_lineage.py`/`test_asset_evidence.py`'s own rationale: SQLite is
a real SQL engine enforcing the same row semantics the composed queries rely
on, and PostgreSQL is unreachable in this sandbox.

Sections:

1. content fidelity -- the exported artifact's node/edge set matches what the
   live `get_unified_lineage_graph`/`get_unified_lineage_impact` routes
   independently report for the same asset and depth, and AT-19's
   `transformation_reference`/`redaction_status` evidence rides through
   verbatim on a `VIEW_DEFINITION` edge;
2. the asserting principal -- a human-approved `SUGGESTED_RELATIONSHIP` edge
   carries `RelationshipCandidate.reviewed_by` as `asserting_principal`,
   while every mechanically-derived edge kind (`FOREIGN_KEY`,
   `VIEW_DEFINITION`) carries `None`, never a fabricated one;
3. permission parity -- the export route runs through the exact same
   `_load_datasource`/`UNIFIED_LINEAGE_READER_ROLES` gate object the live
   graph/impact routes use (not a separate or weaker check), and denies a
   cross-organization caller identically to the live impact route;
4. hash verification -- the `X-Artifact-SHA256` header matches a fresh
   SHA-256 of the exact bytes returned, and the artifact's own
   `graph_version.graph_content_fingerprint` matches a fresh recomputation
   over its own node/edge content using the same canonicalization AT-16
   established -- proving the artifact is tamper-evident at both layers.
"""

import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida import unified_lineage_api
from aida.answer_provenance import _canonical_json
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataViewDefinition
from aida.lineage_evidence_export_api import (
    export_unified_lineage_impact,
)
from aida.lineage_evidence_export_api import (
    router as lineage_evidence_export_router,
)
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    RelationshipCandidate,
    ViewLineageEdge,
)
from aida.unified_lineage_api import get_unified_lineage_graph, get_unified_lineage_impact
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings(_env_file=None)


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


async def _seed_org_and_datasource(session: AsyncSession) -> tuple[DataSource, MetadataSchema]:
    org = Organization(id=uuid4(), name=f"org-{uuid4().hex[:8]}", slug=f"org-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
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
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    return datasource, schema


async def _seed_table(
    session: AsyncSession, datasource: DataSource, schema: MetadataSchema, name: str
) -> MetadataTable:
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


async def _seed_column(
    session: AsyncSession, table: MetadataTable, name: str
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=1,
        physical_type="varchar",
        nullable=False,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


def _ctx(datasource: DataSource, **overrides: object) -> object:
    return security_context(organization_id=datasource.organization_id, **overrides)


async def _seed_lineage(session: AsyncSession) -> dict[str, object]:
    """One focus table with three edge kinds around it:

    - `upstream_fk` --FOREIGN_KEY--> `focus` (focus depends on upstream_fk;
      an upstream, mechanically-derived edge)
    - `focus` --VIEW_DEFINITION--> `upstream_fk`... no: kept simple, a
      *second*, independent VIEW_DEFINITION edge from a dedicated view table
      into `focus`, carrying AT-19's `transformation_reference`.
    - `focus` --SUGGESTED_RELATIONSHIP(APPROVED)--> `suggested_target`, a
      human-approved candidate with a real steward `reviewed_by`, distinct
      from `created_by` (maker-checker).
    - `downstream_fk` --FOREIGN_KEY--> `focus` (downstream_fk depends on
      focus; a downstream edge, so both traversal directions are exercised).
    """
    datasource, schema = await _seed_org_and_datasource(session)
    focus = await _seed_table(session, datasource, schema, "focus")
    upstream_fk = await _seed_table(session, datasource, schema, "upstream_fk")
    downstream_fk = await _seed_table(session, datasource, schema, "downstream_fk")
    suggested_target = await _seed_table(session, datasource, schema, "suggested_target")
    view_table = await _seed_table(session, datasource, schema, "vw_focus")

    session.add(
        MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=focus.id,
            name="fk_focus_upstream",
            constraint_type="FOREIGN_KEY",
            columns=["upstream_id"],
            referenced_table_id=upstream_fk.id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    session.add(
        MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=downstream_fk.id,
            name="fk_downstream_focus",
            constraint_type="FOREIGN_KEY",
            columns=["focus_id"],
            referenced_table_id=focus.id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp",
        )
    )

    focus_col = await _seed_column(session, focus, "join_key")
    target_col = await _seed_column(session, suggested_target, "join_key")
    candidate = RelationshipCandidate(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        target_datasource_id=datasource.id,
        source_table_id=focus.id,
        source_column_id=focus_col.id,
        target_table_id=suggested_target.id,
        target_column_id=target_col.id,
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.9,
        evidence={"column_name_match": "EXACT"},
        status="APPROVED",
        created_by="analyst-maker",
        reviewed_by="steward-checker",
        review_reason="Confirmed with the domain owner.",
    )
    session.add(candidate)

    session.add(
        ViewLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.focus",
            source_column="id",
            target_table="bank.public.vw_focus",
            target_column="focus_id",
            source_table_id=focus.id,
            target_table_id=view_table.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h1",
        )
    )
    session.add(
        MetadataViewDefinition(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=view_table.id,
            definition_sql_redacted="SELECT * FROM focus",
            definition_fingerprint="def-fp-1",
            redaction_status="PARSED",
            screening_status="CLEAN",
            availability="AVAILABLE",
            fingerprint="fp",
        )
    )
    await session.commit()
    return {
        "datasource": datasource,
        "focus": focus,
        "upstream_fk": upstream_fk,
        "downstream_fk": downstream_fk,
        "suggested_target": suggested_target,
        "view_table": view_table,
        "candidate": candidate,
    }


# ---------------------------------------------------------------------------
# 1 & 2. Content fidelity + asserting principal
# ---------------------------------------------------------------------------


async def test_export_matches_the_live_graph_and_impact_endpoints_for_the_same_asset_and_depth(
    session,
) -> None:
    seeded = await _seed_lineage(session)
    datasource: DataSource = seeded["datasource"]  # type: ignore[assignment]
    focus: MetadataTable = seeded["focus"]  # type: ignore[assignment]
    ctx = _ctx(datasource)

    live_graph = await get_unified_lineage_graph(
        datasource_id=datasource.id,
        node_limit=300,
        edge_limit=1_500,
        suggestion_status="APPROVED",
        context=ctx,
        session=session,
        settings=_SETTINGS,
    )
    live_impact = await get_unified_lineage_impact(
        datasource_id=datasource.id,
        node_id=str(focus.id),
        depth=5,
        node_limit=200,
        context=ctx,
        session=session,
        settings=_SETTINGS,
    )
    response = await export_unified_lineage_impact(
        datasource_id=datasource.id,
        node_id=str(focus.id),
        depth=5,
        node_limit=200,
        context=ctx,
        session=session,
        settings=_SETTINGS,
    )
    exported = json.loads(response.body)

    expected_node_ids = (
        {str(focus.id)}
        | {row.node_id for row in live_impact.upstream}
        | {row.node_id for row in live_impact.downstream}
    )
    assert {node["id"] for node in exported["nodes"]} == expected_node_ids
    # every included edge kind actually got traversed, not just a subset
    assert expected_node_ids == {
        str(seeded["upstream_fk"].id),  # type: ignore[union-attr]
        str(seeded["downstream_fk"].id),  # type: ignore[union-attr]
        str(seeded["suggested_target"].id),  # type: ignore[union-attr]
        str(seeded["view_table"].id),  # type: ignore[union-attr]
        str(focus.id),
    }

    live_nodes_by_id = {node.id: node for node in live_graph.nodes}
    for node in exported["nodes"]:
        live_node = live_nodes_by_id[node["id"]]
        assert node["node_kind"] == live_node.node_kind
        assert node["qualified_name"] == live_node.qualified_name
        assert node["resolved"] == live_node.resolved

    live_edges_by_id = {
        edge.id: edge
        for edge in live_graph.edges
        if edge.source_node_id in expected_node_ids and edge.target_node_id in expected_node_ids
    }
    assert {edge["edge_id"] for edge in exported["edges"]} == set(live_edges_by_id)
    assert live_edges_by_id  # the fixture actually produced edges to compare

    exported_by_id = {edge["edge_id"]: edge for edge in exported["edges"]}
    for edge_id, live_edge in live_edges_by_id.items():
        exported_edge = exported_by_id[edge_id]
        assert exported_edge["edge_source"] == live_edge.edge_source
        assert exported_edge["status"] == live_edge.status
        assert exported_edge["confidence"] == live_edge.confidence
        assert exported_edge["source_columns"] == list(live_edge.source_columns)
        assert exported_edge["target_columns"] == list(live_edge.target_columns)
        # AT-19's evidence (transformation_reference/redaction_status on the
        # VIEW_DEFINITION edge, verbatim) rides through unmodified.
        assert exported_edge["evidence"] == live_edge.evidence

    view_definition_edges = [
        edge for edge in exported["edges"] if edge["edge_source"] == "VIEW_DEFINITION"
    ]
    assert len(view_definition_edges) == 1
    assert view_definition_edges[0]["evidence"]["transformation_reference"] == {
        "tool": "get_transformation_detail",
        "entity_id": str(seeded["view_table"].id),  # type: ignore[union-attr]
        "kind": "VIEW_DEFINITION",
    }
    assert view_definition_edges[0]["evidence"]["redaction_status"] == "PARSED"
    assert view_definition_edges[0]["is_human_asserted"] is False
    assert view_definition_edges[0]["asserting_principal"] is None
    assert view_definition_edges[0]["human_assertion"] is None

    foreign_key_edges = [
        edge for edge in exported["edges"] if edge["edge_source"] == "FOREIGN_KEY"
    ]
    assert len(foreign_key_edges) == 2
    for edge in foreign_key_edges:
        assert edge["is_human_asserted"] is False
        assert edge["asserting_principal"] is None
        assert edge["human_assertion"] is None

    suggested_edges = [
        edge for edge in exported["edges"] if edge["edge_source"] == "SUGGESTED_RELATIONSHIP"
    ]
    assert len(suggested_edges) == 1
    suggested = suggested_edges[0]
    candidate: RelationshipCandidate = seeded["candidate"]  # type: ignore[assignment]
    assert suggested["is_human_asserted"] is True
    assert suggested["asserting_principal"] == "steward-checker"
    assert suggested["human_assertion"] == {
        "candidate_id": str(candidate.id),
        "status": "APPROVED",
        "created_by": "analyst-maker",
        "reviewed_by": "steward-checker",
        "reviewed_at": None,
        "review_reason": "Confirmed with the domain owner.",
    }
    # maker != checker, exactly as `intelligence_api.decide_relationship_candidate`
    # enforces at write time -- the export never conflates the two.
    assert suggested["human_assertion"]["created_by"] != suggested["human_assertion"]["reviewed_by"]

    assert exported["graph_version"]["datasource_id"] == str(datasource.id)
    assert exported["graph_version"]["traversal"]["focus_node_id"] == str(focus.id)
    assert exported["graph_version"]["traversal"]["depth"] == 5


# ---------------------------------------------------------------------------
# 3. Permission parity with the live endpoints
# ---------------------------------------------------------------------------


async def test_export_reuses_the_exact_same_gate_objects_as_the_live_lineage_routes() -> None:
    """Not a copy with the same *shape* -- the literal same function/tuple
    objects the live `unified_lineage_api` routes depend on, imported
    directly rather than reimplemented, so a future change to either can
    never silently diverge into a separate, weaker export-only path."""
    import aida.lineage_evidence_export_api as export_api

    assert export_api._load_datasource is unified_lineage_api._load_datasource
    assert (
        export_api.UNIFIED_LINEAGE_READER_ROLES is unified_lineage_api.UNIFIED_LINEAGE_READER_ROLES
    )
    assert lineage_evidence_export_router.prefix == unified_lineage_api.router.prefix


async def test_export_denies_a_foreign_organization_identically_to_the_live_impact_route(
    session,
) -> None:
    seeded = await _seed_lineage(session)
    datasource: DataSource = seeded["datasource"]  # type: ignore[assignment]
    focus: MetadataTable = seeded["focus"]  # type: ignore[assignment]
    foreign_ctx = security_context(organization_id=uuid4())

    with pytest.raises(HTTPException) as live_exc:
        await get_unified_lineage_impact(
            datasource_id=datasource.id,
            node_id=str(focus.id),
            depth=5,
            node_limit=200,
            context=foreign_ctx,
            session=session,
            settings=_SETTINGS,
        )
    with pytest.raises(HTTPException) as export_exc:
        await export_unified_lineage_impact(
            datasource_id=datasource.id,
            node_id=str(focus.id),
            depth=5,
            node_limit=200,
            context=foreign_ctx,
            session=session,
            settings=_SETTINGS,
        )

    assert live_exc.value.status_code == export_exc.value.status_code == 403


async def test_export_404s_for_an_unknown_node_identically_to_the_live_impact_route(
    session,
) -> None:
    datasource, _schema = await _seed_org_and_datasource(session)
    await session.commit()
    missing_node_id = str(uuid4())

    with pytest.raises(HTTPException) as live_exc:
        await get_unified_lineage_impact(
            datasource_id=datasource.id,
            node_id=missing_node_id,
            depth=5,
            node_limit=200,
            context=_ctx(datasource),
            session=session,
            settings=_SETTINGS,
        )
    with pytest.raises(HTTPException) as export_exc:
        await export_unified_lineage_impact(
            datasource_id=datasource.id,
            node_id=missing_node_id,
            depth=5,
            node_limit=200,
            context=_ctx(datasource),
            session=session,
            settings=_SETTINGS,
        )

    assert live_exc.value.status_code == export_exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 4. Hash verification -- tamper-evidence, not a cryptographic signature
# ---------------------------------------------------------------------------


async def test_artifact_hash_and_pinned_fingerprint_both_verify_against_fresh_content(
    session,
) -> None:
    seeded = await _seed_lineage(session)
    datasource: DataSource = seeded["datasource"]  # type: ignore[assignment]
    focus: MetadataTable = seeded["focus"]  # type: ignore[assignment]

    response = await export_unified_lineage_impact(
        datasource_id=datasource.id,
        node_id=str(focus.id),
        depth=5,
        node_limit=200,
        context=_ctx(datasource),
        session=session,
        settings=_SETTINGS,
    )

    assert response.media_type == "application/json"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="lineage-{datasource.id}-{focus.id}-depth5-export.json"'
    )
    # Layer 1: the whole delivered artifact's bytes are what the header
    # claims -- a recipient who saves this file and rehashes it independently
    # gets the same digest back, proving nothing was altered in transit.
    assert response.headers["X-Artifact-SHA256"] == hashlib.sha256(response.body).hexdigest()

    exported = json.loads(response.body)
    # Layer 2: the *content* pin (AT-16's fingerprint idiom, reused verbatim)
    # over just the composed nodes/edges also reproduces independently.
    fresh_fingerprint = hashlib.sha256(
        _canonical_json({"nodes": exported["nodes"], "edges": exported["edges"]})
    ).hexdigest()
    assert exported["graph_version"]["graph_content_fingerprint"] == fresh_fingerprint


async def test_two_exports_of_an_unchanged_graph_are_byte_identical_apart_from_the_timestamp(
    session,
) -> None:
    """Same asset, same depth, same (unchanged) underlying graph: the
    content-derived hash must be stable across two independent exports --
    only `pinned_at` may differ -- so a recipient re-deriving the fingerprint
    from the artifact's own nodes/edges always gets the value the artifact
    itself records, never a moving target."""
    seeded = await _seed_lineage(session)
    datasource: DataSource = seeded["datasource"]  # type: ignore[assignment]
    focus: MetadataTable = seeded["focus"]  # type: ignore[assignment]

    first = await export_unified_lineage_impact(
        datasource_id=datasource.id,
        node_id=str(focus.id),
        depth=5,
        node_limit=200,
        context=_ctx(datasource),
        session=session,
        settings=_SETTINGS,
    )
    second = await export_unified_lineage_impact(
        datasource_id=datasource.id,
        node_id=str(focus.id),
        depth=5,
        node_limit=200,
        context=_ctx(datasource),
        session=session,
        settings=_SETTINGS,
    )

    first_body = json.loads(first.body)
    second_body = json.loads(second.body)
    assert (
        first_body["graph_version"]["graph_content_fingerprint"]
        == second_body["graph_version"]["graph_content_fingerprint"]
    )
    first_body["graph_version"].pop("pinned_at")
    second_body["graph_version"].pop("pinned_at")
    assert first_body == second_body
