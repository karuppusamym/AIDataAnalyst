"""AT-20: lineage evidence export as a signed artifact.

Point-in-time lineage for one chosen asset and traversal depth -- diagram
shape (nodes) plus edge set -- packaged as one downloadable artifact instead
of a live-only view. `Docs/60-delivery/03-tracker.md`'s AT-20 row: "For a
bank the artifact is the deliverable -- it goes in a BCBS 239 pack. Collibra
exports a plain diagram; ours is worth more only if we can hand it over."

This module composes the artifact; `aida.lineage_evidence_export_api` serves
it over HTTP through the exact same authorization gate the live unified-
lineage routes already use (`require_roles(*UNIFIED_LINEAGE_READER_ROLES)` +
`_load_datasource`'s `enforce_organization` -- no separate/weaker export-only
path). Everything the artifact reports is reused verbatim from what already
landed on this branch today, not re-derived:

- **Node/edge set**: `unified_lineage_api.build_unified_lineage_graph_payload`
  (EA.14) for the full per-datasource graph, filtered down to the node
  id set `build_unified_lineage_impact_payload`'s own bounded upstream/
  downstream traversal reports for the chosen focus node and depth -- the
  same traversal the live `.../impact/{node_id}` route runs, not a second
  depth-bounding algorithm invented for export. An edge is included when
  both its endpoints are in that traversal-bounded node set, the same
  "direct edges between the included set" convention AT-16's
  `answer_provenance.compose_lineage_provenance` already uses for cited
  tables.
- **Derivation method per edge**: `UnifiedLineageEdgeRead.edge_source`
  passed through verbatim (`FOREIGN_KEY`, `SUGGESTED_RELATIONSHIP`,
  `DBT_DEPENDENCY`, `OPENLINEAGE_ETL`, `VIEW_DEFINITION`,
  `PROCEDURE_DEFINITION`) -- the same taxonomy AT-16 reuses, not a parallel
  one.
- **Per-edge transformation reference**: AT-19's `evidence.
  transformation_reference` / `evidence.redaction_status` on `VIEW_DEFINITION`
  edges, carried through `evidence` verbatim (never re-resolved or
  re-derived here).
- **Asserting principal for human edges**: the only edge kind in this graph
  that is a human assertion rather than a mechanical read of a database
  constraint, a dbt manifest, an OpenLineage run event, or parsed SQL is
  `SUGGESTED_RELATIONSHIP` once a steward has actually decided on it --
  `RelationshipCandidate.reviewed_by` (`models.py`, read-only), set only by
  `intelligence_api.decide_relationship_candidate`'s explicit maker-checker
  approval endpoint (never automatic, and never the same principal as
  `created_by` -- "maker cannot review their own candidate" is enforced
  there). `build_unified_lineage_graph_payload`'s default
  `suggestion_status="APPROVED"` means every `SUGGESTED_RELATIONSHIP` edge
  this export can include already has a non-null `reviewed_by`; the field is
  still looked up per edge rather than assumed, so a graph built with a
  wider `suggestion_status` never fabricates a principal for a still-PENDING
  candidate. Every other edge kind's `asserting_principal` is `None` --
  correctly: a foreign key is not a human's assertion, and fabricating one
  would misrepresent the evidence.
- **Pinned graph version**: AT-16's own pin shape and construction algorithm,
  reused rather than reinvented -- `answer_provenance._canonical_json`
  (sorted-key, whitespace-free JSON, the same canonicalization
  `agent_orchestrator._canonical_json` (AT-6) established) imported and
  called here verbatim, over `{"nodes": ..., "edges": ...}` exactly as AT-16
  fingerprints `{"cited_tables": ..., "relationships": ...}`. `pinned_at` +
  the exact traversal parameters + the SHA-256 `graph_content_fingerprint`
  let a later reader independently verify the *content* this export
  consulted, not just trust a timestamp.

**On "signed"**: no cryptographic signing or key-management infrastructure
exists anywhere on this platform (checked: no `cryptography`/`pyjwt`-style
asymmetric-signing usage on any artifact-producing path, no key store, no
`models.py` table for one) -- and this row's hard constraints forbid adding
one. What this module (and the export route wrapping it) actually provides
is **hash-verified integrity**, the same honest idiom `context_compiler_api`
(EE.9) and UX-7's `asset_evidence_api` already established for "produce a
downloadable file artifact": a SHA-256 content hash a recipient can
recompute over the exact bytes they received and compare against the
`X-Artifact-SHA256` header the export route returns alongside it, proving
the artifact was not altered between composition and receipt. That is
tamper-evidence, not non-repudiation -- it does not prove *who* produced the
artifact the way an asymmetric digital signature would, and this module does
not claim otherwise. It is still worth more than Collibra's "plain diagram"
comparison cares about: a verifiable, resolvable evidence chain (pinned
graph state, per-edge derivation method, the human steward accountable for
every non-mechanical edge), not a picture.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.answer_provenance import _canonical_json
from aida.config import Settings
from aida.models import DataSource, RelationshipCandidate
from aida.unified_lineage_api import (
    LineageNodeNotFoundError,
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
)

#: Same defaults `build_unified_lineage_graph_payload` and
#: `build_unified_lineage_impact_payload` already use -- kept explicit here
#: (not just inherited) so the exact bound values actually applied are
#: always the ones this module records on `graph_version.traversal`.
_DEFAULT_DEPTH = 5
_DEFAULT_IMPACT_NODE_LIMIT = 200
_DEFAULT_GRAPH_NODE_LIMIT = 300
_DEFAULT_GRAPH_EDGE_LIMIT = 1_500

#: `SUGGESTED_RELATIONSHIP` edges in a single-datasource graph
#: (`unified_lineage_api._build_unified_graph`) always carry an id of this
#: shape (`f"candidate:{candidate.id}"`) -- the federated domain-wide graph's
#: `cross-source-candidate:`/`cross-boundary-candidate:` prefixes never occur
#: here, since this export composes from the single-datasource builder only.
_CANDIDATE_EDGE_ID_PREFIX = "candidate:"


async def compose_lineage_export_artifact(
    session: AsyncSession,
    datasource: DataSource,
    node_id: str,
    *,
    depth: int = _DEFAULT_DEPTH,
    node_limit: int = _DEFAULT_IMPACT_NODE_LIMIT,
    graph_node_limit: int = _DEFAULT_GRAPH_NODE_LIMIT,
    graph_edge_limit: int = _DEFAULT_GRAPH_EDGE_LIMIT,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Compose the AT-20 lineage evidence export artifact for one asset.

    Raises `LineageNodeNotFoundError` (the same exception the live
    `.../impact/{node_id}` route translates to a 404) when `node_id` is not
    part of `datasource`'s unified graph. The caller is responsible for
    loading and authorizing `datasource` -- this function does no access
    control itself, exactly like `build_unified_lineage_graph_payload` and
    `build_unified_lineage_impact_payload`, which it calls verbatim.
    """
    graph = await build_unified_lineage_graph_payload(
        session,
        datasource,
        node_limit=graph_node_limit,
        edge_limit=graph_edge_limit,
        settings=settings,
    )
    node_by_id = {node.id: node for node in graph.nodes}
    if node_id not in node_by_id:
        raise LineageNodeNotFoundError(
            f"lineage node '{node_id}' not found in this datasource's graph"
        )

    impact = await build_unified_lineage_impact_payload(
        session,
        datasource,
        node_id,
        depth=depth,
        node_limit=node_limit,
        settings=settings,
    )

    # depth_by_node_id also carries the traversal direction the live impact
    # route reports (UPSTREAM/DOWNSTREAM), plus FOCUS for the chosen asset
    # itself -- the node set an export "diagram" needs is exactly this
    # traversal's reachable set, never re-computed by a second algorithm.
    depth_by_node_id: dict[str, tuple[str, int]] = {node_id: ("FOCUS", 0)}
    for row in impact.upstream:
        depth_by_node_id.setdefault(row.node_id, ("UPSTREAM", row.depth))
    for row in impact.downstream:
        depth_by_node_id.setdefault(row.node_id, ("DOWNSTREAM", row.depth))
    included_node_ids = set(depth_by_node_id)

    nodes = sorted(
        (
            {
                "id": info.id,
                "node_kind": info.node_kind,
                "label": info.label,
                "qualified_name": info.qualified_name,
                "matched_table_id": (
                    str(info.matched_table_id) if info.matched_table_id is not None else None
                ),
                "resolved": info.resolved,
                "traversal_direction": depth_by_node_id[info.id][0],
                "depth": depth_by_node_id[info.id][1],
            }
            for info in (node_by_id[included_id] for included_id in included_node_ids)
        ),
        key=lambda node: (node["depth"], str(node["qualified_name"])),
    )

    included_edges = sorted(
        (
            edge
            for edge in graph.edges
            if edge.source_node_id in included_node_ids
            and edge.target_node_id in included_node_ids
        ),
        key=lambda edge: edge.id,
    )

    # --- Asserting principal for human (SUGGESTED_RELATIONSHIP) edges ---
    candidate_ids_by_edge_id: dict[str, UUID] = {}
    for edge in included_edges:
        if edge.edge_source != "SUGGESTED_RELATIONSHIP" or not edge.id.startswith(
            _CANDIDATE_EDGE_ID_PREFIX
        ):
            continue
        raw_id = edge.id[len(_CANDIDATE_EDGE_ID_PREFIX) :]
        try:
            candidate_ids_by_edge_id[edge.id] = UUID(raw_id)
        except ValueError:
            # Not a real single-datasource candidate edge id -- leave
            # unasserted rather than guessing.
            continue

    candidates_by_id: dict[UUID, RelationshipCandidate] = {}
    if candidate_ids_by_edge_id:
        rows = (
            await session.scalars(
                select(RelationshipCandidate).where(
                    RelationshipCandidate.id.in_(candidate_ids_by_edge_id.values())
                )
            )
        ).all()
        candidates_by_id = {row.id: row for row in rows}

    edges = []
    for edge in included_edges:
        human_assertion: dict[str, Any] | None = None
        asserting_principal: str | None = None
        candidate_id = candidate_ids_by_edge_id.get(edge.id)
        if candidate_id is not None:
            candidate = candidates_by_id.get(candidate_id)
            if candidate is not None:
                asserting_principal = candidate.reviewed_by
                human_assertion = {
                    "candidate_id": str(candidate.id),
                    "status": candidate.status,
                    "created_by": candidate.created_by,
                    "reviewed_by": candidate.reviewed_by,
                    "reviewed_at": (
                        candidate.reviewed_at.isoformat()
                        if candidate.reviewed_at is not None
                        else None
                    ),
                    "review_reason": candidate.review_reason,
                }
        edges.append(
            {
                "edge_id": edge.id,
                "edge_source": edge.edge_source,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "source_label": edge.source_label,
                "target_label": edge.target_label,
                "status": edge.status,
                "confidence": edge.confidence,
                "source_columns": list(edge.source_columns),
                "target_columns": list(edge.target_columns),
                # AT-19's transformation_reference/redaction_status ride
                # through here verbatim -- this is UnifiedLineageEdgeRead's
                # own `evidence` dict, never rebuilt.
                "evidence": edge.evidence,
                "is_human_asserted": edge.edge_source == "SUGGESTED_RELATIONSHIP",
                "asserting_principal": asserting_principal,
                "human_assertion": human_assertion,
            }
        )

    # AT-16's exact pin shape/algorithm, reused verbatim: canonical JSON of
    # the composed content, SHA-256'd, plus the traversal parameters that
    # produced it and a UTC capture instant.
    fingerprint_payload = {"nodes": nodes, "edges": edges}
    graph_content_fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()

    focus = node_by_id[node_id]
    return {
        "artifact_type": "LINEAGE_EVIDENCE_EXPORT",
        "datasource_id": str(datasource.id),
        "focus_node_id": node_id,
        "focus_node_kind": focus.node_kind,
        "focus_label": focus.qualified_name,
        "requested_depth": depth,
        "node_limit": node_limit,
        "upstream_truncated": impact.upstream_truncated,
        "downstream_truncated": impact.downstream_truncated,
        "nodes": nodes,
        "edges": edges,
        "graph_version": {
            "pinned_at": datetime.now(UTC).isoformat(),
            "datasource_id": str(datasource.id),
            "traversal": {
                "focus_node_id": node_id,
                "depth": depth,
                "node_limit": node_limit,
                "graph_node_limit": graph_node_limit,
                "graph_edge_limit": graph_edge_limit,
                "scope": "IMPACT_BOUNDED_SUBGRAPH",
            },
            "graph_content_fingerprint": graph_content_fingerprint,
        },
    }
