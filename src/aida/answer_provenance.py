"""AT-16: provenance block in the answer contract.

Extends `AgentRun.plan_evidence` (a free-form JSON `dict[str, Any]` column --
no schema or migration change needed, see the tracker row's hard constraint)
with a `lineage` section composed from EA.14's unified lineage graph
(`aida.unified_lineage_api.build_unified_lineage_graph_payload`) and AT-19's
per-edge `transformation_reference`/`redaction_status` evidence. Called once
by `aida.agent_orchestrator.GovernedAgentOrchestrator.run` at answer
completion time and never recomputed on read -- `GET /v1/agent-runs/{id}`
(`aida.api`) returns the persisted `AgentRun.plan_evidence` as-is, so the pin
below stays exactly what was true when the answer was produced even if the
live lineage graph changes afterwards.

Before this row, an answer's lineage citation was table names only
(`QueryExecution.referenced_tables`, surfaced via
`quality_coupling.resolve_table_ids` in
`agent_orchestrator._checkpoint_explained`'s quality-gate check) -- no
columns, no derivation method, no notion of which graph state was consulted.
For BCBS 239 that is the gap between an audit answer and an anecdote. This
module composes that richer block from EA.14/AT-19's *existing*
unified-lineage data; it does not re-derive lineage from scratch, and it does
not invent a parallel derivation taxonomy -- every relationship's
`edge_source` is one of `unified_lineage.UnifiedLink.edge_source`'s existing
values (`FOREIGN_KEY`, `SUGGESTED_RELATIONSHIP`, `DBT_DEPENDENCY`,
`OPENLINEAGE_ETL`, `VIEW_DEFINITION`, `PROCEDURE_DEFINITION`), and its
`evidence` dict is `UnifiedLineageEdgeRead.evidence` passed through verbatim
(including AT-19's `transformation_reference`/`redaction_status` when a
`VIEW_DEFINITION` edge carries them).

**On the "pinned graph version"**: no version, snapshot id, or timestamp
concept exists anywhere on this platform's lineage code (`unified_lineage.py`,
`unified_lineage_api.py` -- checked, neither has one). The pin here is a new,
narrow concept for this row rather than a surfaced existing field, built to
match the platform's own established idiom for "capture a fact once, let a
later read verify against it without recomputing it" -- AT-6's
`AgentRun.grounding_fragment_digests` (a SHA-256 digest per grounding
fragment, resolved and verified later by `agent_run_replay.py`, never
recomputed live). `graph_version` here is the same shape applied to lineage:
a UTC timestamp plus the exact traversal parameters used (so the *request*
that produced this state is reproducible), plus a SHA-256 fingerprint over
the cited tables and relationships actually composed into this answer (so
the *content* consulted is independently verifiable, not just timestamped).
Re-running the same traversal against a since-changed graph would produce a
different fingerprint; the one stored on this answer never does, because it
is written once at completion time and read back unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import DataSource
from aida.unified_lineage_api import build_unified_lineage_graph_payload

#: Default bounds for the graph build this module drives -- identical to
#: `build_unified_lineage_graph_payload`'s own defaults (kept explicit here,
#: not just inherited, so the exact values actually used are always the ones
#: recorded in `graph_version.traversal`, never at risk of drifting from a
#: default that changes elsewhere).
_DEFAULT_NODE_LIMIT = 300
_DEFAULT_EDGE_LIMIT = 1_500


def _canonical_json(value: Any) -> bytes:
    """Deterministic byte encoding for content hashing -- the same idiom
    `agent_orchestrator._canonical_json` (AT-6) and several other modules in
    this codebase already use: sorted keys, no incidental whitespace, so
    identical content always fingerprints identically.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


async def compose_lineage_provenance(
    session: AsyncSession,
    *,
    datasource: DataSource,
    answer_table_ids: dict[str, UUID],
    queried_columns: list[str] | None = None,
    node_limit: int = _DEFAULT_NODE_LIMIT,
    edge_limit: int = _DEFAULT_EDGE_LIMIT,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Compose the `lineage` section of the answer contract's provenance block.

    Returns `None` when the answer's executed SQL resolved no table this
    datasource's catalog knows about -- there is nothing to pin, mirroring
    the existing `trust_evidence` omission pattern in
    `agent_orchestrator._checkpoint_explained`. Otherwise:

    - `cited_tables`: every table the answer's executed SQL referenced,
      resolved to this datasource's catalog (id, name-as-queried, and the
      unified graph's qualified name when the table is a graph node).
    - `queried_columns`: the columns the executed SQL actually referenced
      (`QueryExecution.referenced_columns`, the same SQL-parsed evidence
      already persisted per run) -- deduplicated and sorted. These are the
      answer's own selected/joined columns, not resolved per-table (the
      parser does not qualify every reference), stated here rather than
      silently implied.
    - `relationships`: one entry per unified-lineage edge directly between
      two cited tables -- `edge_source` (the derivation method, verbatim),
      `status`, `confidence`, the specific `source_columns`/`target_columns`
      involved, and `evidence` (AT-19's `transformation_reference`/
      `redaction_status` included verbatim when present). Two tables with no
      direct edge between them in the unified graph simply have none, and
      that absence is itself informative for an audit answer, not hidden.
    - `graph_version`: the pin -- see this module's docstring.
    """
    if not answer_table_ids:
        return None

    graph = await build_unified_lineage_graph_payload(
        session,
        datasource,
        node_limit=node_limit,
        edge_limit=edge_limit,
        settings=settings,
    )
    node_by_id = {node.id: node for node in graph.nodes}
    cited_node_ids = {str(table_id) for table_id in answer_table_ids.values()}

    cited_tables = [
        {
            "table_id": str(table_id),
            "table_name": table_name,
            "qualified_name": (
                node_by_id[str(table_id)].qualified_name if str(table_id) in node_by_id else None
            ),
        }
        for table_name, table_id in sorted(answer_table_ids.items())
    ]

    relationships = sorted(
        (
            {
                "edge_id": edge.id,
                "edge_source": edge.edge_source,
                "status": edge.status,
                "confidence": edge.confidence,
                "source_table": {
                    "table_id": edge.source_node_id,
                    "qualified_name": edge.source_label,
                },
                "target_table": {
                    "table_id": edge.target_node_id,
                    "qualified_name": edge.target_label,
                },
                "source_columns": list(edge.source_columns),
                "target_columns": list(edge.target_columns),
                "evidence": edge.evidence,
            }
            for edge in graph.edges
            if edge.source_node_id in cited_node_ids and edge.target_node_id in cited_node_ids
        ),
        key=lambda relationship: str(relationship["edge_id"]),
    )

    fingerprint_payload = {"cited_tables": cited_tables, "relationships": relationships}
    graph_content_fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()

    return {
        "cited_tables": cited_tables,
        "queried_columns": sorted(dict.fromkeys(queried_columns or [])),
        "relationships": relationships,
        "graph_version": {
            "pinned_at": datetime.now(UTC).isoformat(),
            "datasource_id": str(datasource.id),
            "traversal": {
                "node_limit": node_limit,
                "edge_limit": edge_limit,
                "scope": "DIRECT_EDGES_BETWEEN_CITED_TABLES",
            },
            "graph_content_fingerprint": graph_content_fingerprint,
        },
    }
