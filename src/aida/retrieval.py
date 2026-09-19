"""
Atlas Hybrid Retrieval Engine
==============================

Provides a significantly improved metadata retrieval strategy for the
GovernedAgentOrchestrator, replacing the simple lexical LIKE scan with
a two-stage hybrid BM25 + weighted scoring approach.

Architecture
------------
Stage 1: Candidate fetch
  Pull up to agent_retrieval_scan_limit rows from each object type (tables,
  columns, tools, business annotations, dbt resources, published semantic
  metrics, glossary terms bound to a semantic object, -- R11-FP11 --
  stored procedures and functions, and -- R11-FP09 -- concepts of each
  ontology's published version with a valid mapping here) using the existing
  org/datasource scope filters. SM-2: an ACTIVE glossary-term<->semantic-object
  binding folds the term's definition/synonyms into the metric's candidate
  text (and the metric's identity into the term's hit metadata), so the
  binding participates in scoring in both directions instead of being a
  static link nobody reads at query time. R11-FP08: a routine's *approved*
  Atlas-authored description is one of the words that fetches it and one of the
  words that scores it, so a procedure described in business language is
  reachable by a question asked in business language -- inside this stage, not
  as a channel of its own (R11-S3 defers new channels until retrieval quality
  is measured).

Stage 2: Hybrid scoring
  Score each candidate with three additive signals:

  a) BM25-style token overlap
     - Tokenise query and candidate text into lowercase tokens
     - Score = (matched tokens / total query tokens), boosted by IDF
       approximation (penalise very common words)
     - Avoids the need for a vector index at this stage; can be replaced
       with pgvector in Phase 2 when the embedding column is added

  b) Exact-phrase bonus (+0.2)
     - If the full query string appears verbatim (lowercased) in the
       candidate text, add a strong exact-match bonus

  c) Governed-tool priority boost (+0.25)
     - Published governed tools are strongly preferred over raw table hits;
       this matches the planner strategy priority order

Stage 3: Ranking & deduplication
  Sort by score descending, deduplicate by object_id, cap at
  agent_retrieval_limit (default 25).

This module is designed to be a drop-in replacement for the retrieval
block inside GovernedRetriever. The public interface is identical:

    hits = await hybrid_retrieve(session, datasource=ds, question=q, ...)

Usage
-----
Import and call from agent_intelligence.GovernedRetriever.retrieve() or
directly from GovernedAgentOrchestrator.

The enhanced pipeline
---------------------
`hybrid_retrieve` above is the lexical stage and is complete in itself.
`hybrid_retrieve_enhanced` is not a longer version of it -- it is a
*composition* of the stages defined in `aida.retrieval_stages`: authorized
candidates (which calls `hybrid_retrieve`), the vector channel, the graph
channel, the merge rule, trust, fusion, evidence. Each of those is one rule and
is documented where it lives. What this module keeps is the lexical scan itself,
the hit type every caller consumes, and the composition's order.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

import structlog
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import current_version_alias
from aida.config import Settings
from aida.envelope_models import (
    AVAILABLE,
    MetadataRoutine,
    MetadataRoutineParameter,
    MetadataTrigger,
    MetadataViewDefinition,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.ingest_screening import is_eligible_for_model_context
from aida.models import (
    BusinessDomain,
    BusinessEntity,
    DataSource,
    DbtArtifactImport,
    DbtProject,
    DbtResource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernedTool,
    GovernedToolVersion,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    QueryExecution,
    SemanticMetric,
    SemanticMetricVersion,
    TermSemanticBinding,
)
from aida.ontology_kinds import table_mapping_kind
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.procedure_lineage_models import DeepProcedureLineageEdge, TriggerLineageEdge
from aida.quality_coupling import resolve_table_ids
from aida.retrieval_metrics import RETRIEVAL_SECONDS
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

if TYPE_CHECKING:
    # Annotation-only (this module defers the real import to call time, to
    # break the cycle `retrieval_stages` -> `retrieval` -> `retrieval_stages`).
    from aida.retrieval_stages import CancellationToken

# ---------------------------------------------------------------------------
# Text normalisation & tokenisation
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "for",
        "from", "get", "how", "i", "in", "is", "it", "list", "me", "my",
        "of", "on", "or", "see", "show", "the", "to", "what", "which",
        "with", "you", "latest", "all", "give", "tell",
    }
)


logger = structlog.get_logger(__name__)


def _tokenise(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, strip stop words, split snake_case."""
    # Split camelCase / snake_case before lowercasing
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    expanded = expanded.replace("_", " ")
    tokens = re.findall(r"[a-z0-9]+", expanded.lower())
    return [t for t in tokens if len(t) > 1 and t not in _STOP_WORDS]


def _idf_weight(token: str) -> float:
    """
    Simple heuristic IDF — penalise very short and very common-looking tokens.
    A full IDF index would require corpus stats; this approximation is enough
    to rank 'revenue' higher than 'data'.
    """
    if len(token) <= 2:
        return 0.5
    if len(token) <= 4:
        return 0.8
    return 1.0


def _bm25_score(query_tokens: list[str], candidate_text: str) -> float:
    """
    Lightweight BM25-inspired score: IDF-weighted token overlap ratio.
    Returns a value in [0.0, 1.0].
    """
    if not query_tokens or not candidate_text:
        return 0.0
    lower_text = candidate_text.lower().replace("_", " ")
    total_weight = sum(_idf_weight(t) for t in query_tokens)
    if total_weight == 0:
        return 0.0
    matched_weight = sum(
        _idf_weight(t) for t in query_tokens if t in lower_text
    )
    return min(1.0, matched_weight / total_weight)


#: R11-FP11: a view definition match is weaker evidence than a name or description match --
#: it says what the view is built from, not what it is.
_DEFINITION_MATCH_WEIGHT = 0.6

#: R11-FP08: how much of the coverage an *approved* routine description adds is kept.
#:
#: Full weight, and the number is stated rather than left implicit because the two
#: neighbouring decisions in this file both went the other way and the difference is
#: the whole point:
#:
#: * It is **not** discounted like `_DEFINITION_MATCH_WEIGHT`. That discount is for a
#:   view's stored SQL, which says what the object is *built from*; a reviewed
#:   description says what the routine is *for*, which is exactly what a
#:   business-language question asks. Every other candidate in this module already
#:   folds an object's description into its candidate text undiscounted (a table's
#:   `source_description`, a metric's description, a concept's description), and an
#:   Atlas-authored, independently APPROVED description is stronger evidence than any
#:   of those -- it was reviewed, and no rescan can reword it.
#: * It is **not** boosted above a name match either. `hybrid_retrieve` has exactly one
#:   boost, and it belongs to published governed tools; a routine "is context for a
#:   question, never a governed tool, and must not outrank one" (the rule the ROUTINE
#:   candidate already carries below). Raising a description match above a name match
#:   would need a second boost, and a routine is the wrong candidate to invent one for.
#:
#: Applied to the *incremental* coverage the description contributes rather than to the
#: whole score, so this stays a real knob: at 0.6 an approved description would be
#: discounted exactly the way a view definition is, without touching anything else.
_APPROVED_DESCRIPTION_MATCH_WEIGHT = 1.0


def _exact_phrase_bonus(query: str, candidate_text: str) -> float:
    """Return 0.2 if the lowercased query appears as a substring of the candidate."""
    return 0.2 if query.lower() in candidate_text.lower() else 0.0


# ---------------------------------------------------------------------------
# RetrievalHit (mirrors agent_intelligence.RetrievalHit for drop-in use)
# ---------------------------------------------------------------------------


class HybridRetrievalHit:
    """Scored retrieval result compatible with GovernedRetriever output."""

    __slots__ = (
        "object_type", "object_id", "display_name",
        "score", "reason_codes", "metadata",
    )

    def __init__(
        self,
        object_type: str,
        object_id: str,
        display_name: str,
        score: float,
        reason_codes: list[str],
        metadata: dict[str, Any],
    ) -> None:
        self.object_type = object_type
        self.object_id = object_id
        self.display_name = display_name
        self.score = score
        self.reason_codes = reason_codes
        self.metadata = metadata

    def evidence(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "display_name": self.display_name,
            "score": self.score,
            "reason_codes": self.reason_codes,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Shared dbt-project helpers
# ---------------------------------------------------------------------------


async def _latest_dbt_artifact_import_ids(
    session: AsyncSession, *, datasource: DataSource
) -> list[UUID]:
    """The most recent `DbtArtifactImport` id per ACTIVE `DbtProject` bound to
    ``datasource``. Factored out of `hybrid_retrieve`'s stage-5 dbt-resource
    block (RT-2) so `hybrid_retrieve_enhanced`'s graph stage can resolve the
    same "latest snapshot per project" scope for dbt `depends_on` edges
    without a second, drifting copy of this resolution logic.
    """
    dbt_project_ids = list(
        await session.scalars(
            select(DbtProject.id).where(
                DbtProject.datasource_id == datasource.id,
                DbtProject.organization_id == datasource.organization_id,
                DbtProject.status == "ACTIVE",
            )
        )
    )
    if not dbt_project_ids:
        return []
    artifact_rows = (
        await session.scalars(
            select(DbtArtifactImport)
            .where(DbtArtifactImport.dbt_project_id.in_(dbt_project_ids))
            .order_by(DbtArtifactImport.dbt_project_id, DbtArtifactImport.created_at.desc())
        )
    ).all()
    seen_projects: set[UUID] = set()
    latest_artifact_ids: list[UUID] = []
    for artifact in artifact_rows:
        if artifact.dbt_project_id not in seen_projects:
            latest_artifact_ids.append(artifact.id)
            seen_projects.add(artifact.dbt_project_id)
    return latest_artifact_ids


# ---------------------------------------------------------------------------
# Public retrieval function
# ---------------------------------------------------------------------------


def _concept_mappings(definition: dict[str, Any], concept_key: str) -> list[tuple[str, UUID]]:
    mappings: list[tuple[str, UUID]] = []
    for mapping in definition.get("mappings") or []:
        if not isinstance(mapping, dict) or mapping.get("concept") != concept_key:
            continue
        try:
            subject_id = UUID(str(mapping.get("subject_id")))
        except ValueError:
            continue
        mappings.append((str(mapping.get("subject_type")), subject_id))
    return mappings


async def _ontology_concept_hits(
    session: AsyncSession,
    *,
    datasource: DataSource,
    question: str,
    query_tokens: list[str],
    scan_limit: int,
) -> list[HybridRetrievalHit]:
    """R11-FP09: concepts of each ontology's *published* version that match the question.

    A concept is found by its name, aliases and description. It stands on the catalog objects
    it is mapped to that are still valid *in this datasource*: ACTIVE, and of the kind the
    mapping names (`ontology_kinds.table_mapping_kind`, the rule the ontology routes enforce on
    every write). A deprecated concept, a deprecated ontology, a draft, and a concept with
    nothing valid here are not offered. No boost -- a concept is meaning, never an answer. The
    approved version's id rides in the hit, so the grounding receipt built from the hits records
    which ontology meaning an answer used.
    """
    rows = (
        await session.execute(
            select(OntologyVersion, OntologyHead.ontology_key)
            .join(OntologyHead, OntologyHead.id == OntologyVersion.ontology_id)
            .where(
                OntologyHead.organization_id == datasource.organization_id,
                OntologyVersion.organization_id == datasource.organization_id,
                OntologyVersion.version == OntologyHead.published_version,
                OntologyVersion.status == "APPROVED",
            )
            .limit(scan_limit)
        )
    ).all()
    matches: list[tuple[OntologyVersion, str, str, str, float]] = []
    for version, ontology_key in rows:
        definition = version.definition or {}
        if definition.get("lifecycle") == "DEPRECATED":
            continue
        for concept in definition.get("concepts") or []:
            if not isinstance(concept, dict) or concept.get("deprecated"):
                continue
            aliases = concept.get("aliases") or []
            parts = (concept.get("name"), *aliases, concept.get("description"))
            candidate_text = " ".join(str(part) for part in parts if part)
            bm25 = _bm25_score(query_tokens, candidate_text)
            score = round(min(1.0, bm25 + _exact_phrase_bonus(question, candidate_text)), 4)
            if score > 0:
                key = str(concept.get("key"))
                matches.append((version, ontology_key, key, str(concept.get("name") or key), score))
    if not matches:
        return []

    wanted: dict[str, set[UUID]] = {}
    for version, _, concept_key, _, _ in matches:
        for subject_type, subject_id in _concept_mappings(version.definition, concept_key):
            wanted.setdefault(subject_type, set()).add(subject_id)
    in_datasource = (
        MetadataTable.datasource_id == datasource.id,
        MetadataTable.organization_id == datasource.organization_id,
        MetadataTable.status == "ACTIVE",
    )
    table_kinds: dict[UUID, str] = {}
    table_ids = wanted.get("TABLE", set()) | wanted.get("VIEW", set())
    if table_ids:
        table_rows = await session.execute(
            select(MetadataTable.id, MetadataTable.object_type).where(
                MetadataTable.id.in_(table_ids), *in_datasource
            )
        )
        table_kinds = {
            table_id: table_mapping_kind(object_type) for table_id, object_type in table_rows.all()
        }
    column_tables: dict[UUID, UUID] = {}
    if wanted.get("COLUMN"):
        column_rows = await session.execute(
            select(MetadataColumn.id, MetadataColumn.table_id)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataColumn.id.in_(wanted["COLUMN"]),
                MetadataColumn.status == "ACTIVE",
                *in_datasource,
            )
        )
        column_tables = {column_id: table_id for column_id, table_id in column_rows.all()}
    routine_ids: set[UUID] = set()
    if wanted.get("ROUTINE"):
        routine_ids = set(
            (
                await session.scalars(
                    select(MetadataRoutine.id).where(
                        MetadataRoutine.id.in_(wanted["ROUTINE"]),
                        MetadataRoutine.datasource_id == datasource.id,
                        MetadataRoutine.organization_id == datasource.organization_id,
                        MetadataRoutine.status == "ACTIVE",
                    )
                )
            ).all()
        )

    concept_hits: list[HybridRetrievalHit] = []
    for version, ontology_key, concept_key, concept_name, score in matches:
        tables: set[str] = set()
        columns: set[str] = set()
        routines: set[str] = set()
        for subject_type, subject_id in _concept_mappings(version.definition, concept_key):
            if subject_type in ("TABLE", "VIEW"):
                if table_kinds.get(subject_id) == subject_type:
                    tables.add(str(subject_id))
            elif subject_type == "COLUMN":
                parent = column_tables.get(subject_id)
                if parent is not None:
                    columns.add(str(subject_id))
                    tables.add(str(parent))
            elif subject_type == "ROUTINE" and subject_id in routine_ids:
                routines.add(str(subject_id))
        if not tables and not routines:
            continue
        concept_hits.append(
            HybridRetrievalHit(
                object_type="ONTOLOGY_CONCEPT",
                # A concept has no row of its own: its id is fixed by (approved version, key).
                object_id=str(uuid5(version.id, concept_key)),
                display_name=concept_name,
                score=score,
                reason_codes=["BM25_ONTOLOGY_CONCEPT", "ONTOLOGY_VERSION_APPROVED"],
                metadata={
                    "ontology_key": ontology_key,
                    "ontology_version_id": str(version.id),
                    "ontology_version": version.version,
                    "concept_key": concept_key,
                    "mapped_table_ids": sorted(tables),
                    "mapped_column_ids": sorted(columns),
                    "mapped_routine_ids": sorted(routines),
                },
            )
        )
    return concept_hits


async def hybrid_retrieve(
    session: AsyncSession,
    *,
    datasource: DataSource,
    question: str,
    settings: Settings,
    preferred_tool_version_id: UUID | None = None,
) -> list[HybridRetrievalHit]:
    """
    Two-stage hybrid retrieval returning the top-N scored metadata hits.

    Parameters
    ----------
    session               Async SQLAlchemy session
    datasource            The DataSource to scope retrieval within
    question              Natural-language question from the user / agent
    settings              Atlas Settings (governs limits)
    preferred_tool_version_id  Hint: if supplied and matches a published tool,
                          that tool receives an extra +0.35 priority boost

    Returns
    -------
    List of HybridRetrievalHit sorted by score descending, capped at
    settings.agent_retrieval_limit.
    """
    query_tokens = _tokenise(question)
    scan_limit = settings.agent_retrieval_scan_limit
    retrieval_limit = settings.agent_retrieval_limit

    hits: list[HybridRetrievalHit] = []
    seen_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Fetch all candidate objects concurrently
    # (sequential awaits — fine for typical catalog sizes)
    # ------------------------------------------------------------------

    # 1. Tables. R11-FP11: fetched by name *or* source description -- a table the source
    # describes in the question's words was never fetched, so its description could not score.
    name_filters = [func.lower(MetadataTable.name).contains(t) for t in query_tokens[:10]]
    description_filters = [
        func.lower(func.coalesce(MetadataTable.source_description, "")).contains(t)
        for t in query_tokens[:10]
    ]
    table_rows = (
        await session.scalars(
            select(MetadataTable)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                or_(*name_filters, *description_filters) if name_filters else true(),
            )
            .limit(scan_limit)
        )
    ).all()

    for table in table_rows:
        candidate_text = " ".join(
            filter(None, [table.name, table.source_description])
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"TABLE:{table.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                matched_name = any(token in table.name.lower() for token in query_tokens)
                hits.append(
                    HybridRetrievalHit(
                        object_type="TABLE",
                        object_id=str(table.id),
                        display_name=table.name,
                        score=score,
                        reason_codes=[
                            "BM25_TABLE_NAME" if matched_name else "BM25_TABLE_DESCRIPTION"
                        ],
                        metadata={"table_id": str(table.id)},
                    )
                )

    # 1b. Views whose *definition* names what was asked (R11-FP11). A view called `v_rev_ltd`
    # selecting `net_revenue` from `orders` answers "revenue by customer" and matched nothing
    # before. Scored on the stored value-free text only, and only where screening lets that text
    # be read at all; the hit carries a digest of it, never the text. A definition match is
    # weaker evidence than a name match -- it names what the view is built from, not what it is --
    # so it scores at `_DEFINITION_MATCH_WEIGHT` and never displaces a name match already found.
    definition_filters = [
        func.lower(MetadataViewDefinition.definition_sql_redacted).contains(t)
        for t in query_tokens[:10]
    ]
    definition_rows = (
        (
            await session.execute(
                select(MetadataViewDefinition, MetadataTable)
                .join(MetadataTable, MetadataTable.id == MetadataViewDefinition.table_id)
                .where(
                    MetadataViewDefinition.organization_id == datasource.organization_id,
                    MetadataViewDefinition.datasource_id == datasource.id,
                    MetadataViewDefinition.status == "ACTIVE",
                    MetadataViewDefinition.availability == AVAILABLE,
                    MetadataViewDefinition.redaction_status.in_(
                        sorted(VALUE_FREE_REDACTION_STATUSES)
                    ),
                    MetadataTable.status == "ACTIVE",
                    or_(*definition_filters),
                )
                .limit(scan_limit)
            )
        ).all()
        if definition_filters
        else []
    )

    for definition, table in definition_rows:
        stored = definition.definition_sql_redacted
        if not stored or not is_eligible_for_model_context(definition.screening_status):
            continue
        hit_id = f"TABLE:{table.id}"
        if hit_id in seen_ids:
            continue
        score = round(min(1.0, _bm25_score(query_tokens, stored) * _DEFINITION_MATCH_WEIGHT), 4)
        if score <= 0:
            continue
        seen_ids.add(hit_id)
        hits.append(
            HybridRetrievalHit(
                object_type="TABLE",
                object_id=str(table.id),
                display_name=table.name,
                score=score,
                reason_codes=["BM25_VIEW_DEFINITION"],
                metadata={
                    "table_id": str(table.id),
                    "object_type": table.object_type,
                    "definition_digest": hashlib.sha256(stored.encode("utf-8")).hexdigest(),
                },
            )
        )

    # 2. Columns
    col_filters = [func.lower(MetadataColumn.name).contains(t) for t in query_tokens[:10]]
    column_rows = (
        await session.scalars(
            select(MetadataColumn)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                MetadataColumn.status == "ACTIVE",
                or_(*col_filters) if col_filters else true(),
            )
            .limit(scan_limit)
        )
    ).all()

    for col in column_rows:
        candidate_text = " ".join(filter(None, [col.name, col.physical_type]))
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"COLUMN:{col.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="COLUMN",
                        object_id=str(col.id),
                        display_name=col.name,
                        score=score,
                        reason_codes=["BM25_COLUMN_NAME"],
                        metadata={
                            "column_id": str(col.id),
                            "table_id": str(col.table_id),
                        },
                    )
                )

    # 3. Published governed tools (highest priority — boosted)
    tool_rows = (
        await session.execute(
            select(GovernedToolVersion, GovernedTool)
            .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
            .where(
                GovernedToolVersion.datasource_id == datasource.id,
                GovernedToolVersion.organization_id == datasource.organization_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
            .limit(scan_limit)
        )
    ).all()

    for version, tool in tool_rows:
        candidate_text = " ".join(
            filter(None, [
                version.name,       # GovernedToolVersion.name is the human-readable display name
                tool.slug,          # slug is also useful for keyword matching
                version.description,
            ])
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        # Governing-tool priority boost
        tool_boost = 0.25
        # Extra boost if this is the caller's preferred tool
        preferred_boost = 0.35 if (
            preferred_tool_version_id and preferred_tool_version_id == version.id
        ) else 0.0
        score = round(min(1.0, bm25 + exact + tool_boost + preferred_boost), 4)
        # Object type and metadata shape here are load-bearing, not cosmetic:
        # GovernedPlanner.plan() (agent_intelligence.py) filters
        # `hit.object_type == "GOVERNED_TOOL"` to find tool candidates at all, then reads
        # `hit.metadata["allowed_roles"]` and `hit.metadata["required_parameters"]` to decide
        # eligibility and whether to ask for clarification. Diverging from that contract
        # silently makes every governed tool invisible to the planner.
        parameters = version.parameter_schema
        hit_id = f"GOVERNED_TOOL:{version.id}"
        if hit_id not in seen_ids:
            seen_ids.add(hit_id)
            hits.append(
                HybridRetrievalHit(
                    object_type="GOVERNED_TOOL",
                    object_id=str(version.id),
                    display_name=version.name,          # version.name is the display name
                    score=score,
                    reason_codes=["BM25_TOOL_NAME", "GOVERNED_TOOL_BOOST"],
                    metadata={
                        "tool_version_id": str(version.id),
                        "tool_id": str(tool.id),
                        "datasource_id": str(version.datasource_id),
                        # GovernedToolVersion has no primary_table_id; use referenced_tables list
                        "referenced_tables": version.referenced_tables or [],
                        "slug": tool.slug,
                        "version": version.version,
                        "allowed_roles": version.allowed_roles,
                        "required_parameters": [
                            item["name"]
                            for item in parameters
                            if item.get("required", True) and item.get("default") is None
                        ],
                    },
                )
            )

    # 4. Business annotations (approved semantic enrichments)
    # AT-6: content lives on the current `MetadataBusinessAnnotationVersion`
    # (append-only, never mutated in place -- `business_annotation_versions.py`),
    # not on `MetadataBusinessAnnotation` itself. The hit's `metadata` carries
    # `annotation_version_id` precisely so the orchestrator's grounding-fragment
    # digest (AT-6, `agent_orchestrator._compute_grounding_fragment_digests`)
    # hashes -- and the run's evidence can later resolve back to -- this exact
    # version, even after a later approval supersedes it.
    version_alias, version_ranked = current_version_alias()
    biz_rows = (
        await session.execute(
            select(
                MetadataBusinessAnnotation,
                version_alias,
                BusinessDomain,
                BusinessEntity,
                MetadataTable,
            )
            .join(version_alias, version_alias.annotation_id == MetadataBusinessAnnotation.id)
            .join(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
            .join(BusinessEntity, BusinessEntity.id == MetadataBusinessAnnotation.entity_id)
            .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
            .where(
                MetadataBusinessAnnotation.datasource_id == datasource.id,
                MetadataBusinessAnnotation.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                version_ranked.c.rn == 1,
            )
            .limit(scan_limit)
        )
    ).all()

    for annotation, version, domain, entity, table in biz_rows:
        candidate_text = " ".join(
            filter(None, [
                version.business_name,
                version.business_description,
                domain.display_name,
                entity.display_name,
                version.grain_statement,
                " ".join(version.synonyms or []),
                " ".join(version.suggested_questions or []),
            ])
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"BIZ_ANNOTATION:{annotation.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="BUSINESS_ANNOTATION",
                        object_id=str(annotation.id),
                        display_name=version.business_name or table.name,
                        score=score,
                        reason_codes=["BM25_BUSINESS_ANNOTATION"],
                        metadata={
                            "table_id": str(table.id),
                            "source_table_id": str(table.id),
                            "domain": domain.display_name,
                            "entity": entity.display_name,
                            "annotation_version_id": str(version.id),
                        },
                    )
                )

    # 5. dbt resources
    latest_artifact_ids = await _latest_dbt_artifact_import_ids(session, datasource=datasource)
    if latest_artifact_ids:
        dbt_rows = (
            await session.scalars(
                select(DbtResource)
                .where(DbtResource.artifact_import_id.in_(latest_artifact_ids))
                .limit(scan_limit)
            )
        ).all()
        for dbt_resource in dbt_rows:
            col_desc_text = (
                " ".join(dbt_resource.column_descriptions.values())
                if dbt_resource.column_descriptions
                else ""
            )
            candidate_text = " ".join(
                filter(None, [
                    dbt_resource.name,
                    dbt_resource.description,
                    dbt_resource.original_file_path,
                    col_desc_text,
                ])
            )
            bm25 = _bm25_score(query_tokens, candidate_text)
            exact = _exact_phrase_bonus(question, candidate_text)
            score = round(min(1.0, bm25 + exact), 4)
            if score > 0:
                hit_id = f"DBT_RESOURCE:{dbt_resource.id}"
                if hit_id not in seen_ids:
                    seen_ids.add(hit_id)
                    hits.append(
                        HybridRetrievalHit(
                            object_type="DBT_RESOURCE",
                            object_id=str(dbt_resource.id),
                            display_name=dbt_resource.name,     # correct field
                            score=score,
                            reason_codes=["BM25_DBT_RESOURCE"],
                            metadata={
                                "dbt_resource_id": str(dbt_resource.id),
                                "resource_type": dbt_resource.resource_type,
                                "table_id": (
                                    str(dbt_resource.matched_table_id)
                                    if dbt_resource.matched_table_id
                                    else None
                                ),
                            },
                        )
                    )

    # 6. Semantic metrics (SM-2: a bound, ACTIVE glossary term's definition and
    #    synonyms are folded into the metric's retrievable text, so the binding
    #    actually participates in scoring rather than sitting as a link nobody
    #    reads at query time)
    metric_term_rows = (
        await session.execute(
            select(SemanticMetricVersion, SemanticMetric, GlossaryTermVersion)
            .join(SemanticMetric, SemanticMetric.id == SemanticMetricVersion.metric_id)
            .join(MetadataTable, MetadataTable.id == SemanticMetricVersion.source_table_id)
            .outerjoin(
                TermSemanticBinding,
                (TermSemanticBinding.semantic_object_type == "METRIC")
                & (TermSemanticBinding.semantic_object_id == SemanticMetric.id)
                & (TermSemanticBinding.status == "ACTIVE"),
            )
            .outerjoin(
                GlossaryTermVersion,
                (GlossaryTermVersion.term_id == TermSemanticBinding.term_id)
                & (GlossaryTermVersion.status == "APPROVED"),
            )
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                SemanticMetricVersion.status == "PUBLISHED",
            )
            .limit(scan_limit)
        )
    ).all()

    metrics_by_id: dict[str, tuple[Any, Any, list[Any]]] = {}
    for metric_version, metric, bound_term_version in metric_term_rows:
        entry = metrics_by_id.setdefault(str(metric.id), (metric_version, metric, []))
        if bound_term_version is not None:
            entry[2].append(bound_term_version)

    for metric_version, metric, bound_term_versions in metrics_by_id.values():
        term_text_parts: list[str] = []
        bound_term_ids: list[str] = []
        for term_version in bound_term_versions:
            term_text_parts.append(term_version.display_name)
            term_text_parts.append(term_version.definition)
            term_text_parts.extend(term_version.synonyms or [])
            bound_term_ids.append(str(term_version.term_id))
        candidate_text = " ".join(
            filter(
                None,
                [metric_version.name, metric_version.description, metric.slug, *term_text_parts],
            )
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"SEMANTIC_METRIC:{metric.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                reason_codes = ["BM25_SEMANTIC_METRIC"]
                if bound_term_ids:
                    reason_codes.append("GLOSSARY_TERM_BOUND")
                hits.append(
                    HybridRetrievalHit(
                        object_type="SEMANTIC_METRIC",
                        object_id=str(metric.id),
                        display_name=metric_version.name,
                        score=score,
                        reason_codes=reason_codes,
                        metadata={
                            "metric_id": str(metric.id),
                            "metric_slug": metric.slug,
                            "bound_term_ids": bound_term_ids,
                            # _model_context (agent_orchestrator.py) reads table_id or
                            # source_table_id off every hit to decide which tables to hydrate
                            # into the model's SQL-generation context; without this a metric
                            # hit contributes no table context.
                            "source_table_id": str(metric_version.source_table_id),
                        },
                    )
                )

    # 7. Glossary terms (SM-2: the other retrieval direction -- a term hit
    #    surfaces the semantic objects bound to it, so a search that lands on
    #    the term itself can resolve to the metric it governs)
    term_binding_rows = (
        await session.execute(
            select(GlossaryTermVersion, GlossaryTerm, SemanticMetric)
            .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermVersion.term_id)
            .join(TermSemanticBinding, TermSemanticBinding.term_id == GlossaryTerm.id)
            .join(
                SemanticMetric,
                (TermSemanticBinding.semantic_object_type == "METRIC")
                & (SemanticMetric.id == TermSemanticBinding.semantic_object_id),
            )
            .where(
                GlossaryTermVersion.status == "APPROVED",
                GlossaryTerm.organization_id == datasource.organization_id,
                TermSemanticBinding.status == "ACTIVE",
                SemanticMetric.project_id == datasource.project_id,
            )
            .limit(scan_limit)
        )
    ).all()

    terms_by_id: dict[str, tuple[Any, Any, list[Any]]] = {}
    for term_version, term, bound_metric in term_binding_rows:
        entry = terms_by_id.setdefault(str(term.id), (term_version, term, []))
        entry[2].append(bound_metric)

    for term_version, term, bound_metrics in terms_by_id.values():
        term_text = [
            term_version.display_name,
            term_version.definition,
            *(term_version.synonyms or []),
        ]
        candidate_text = " ".join(filter(None, term_text))
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"GLOSSARY_TERM:{term.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="GLOSSARY_TERM",
                        object_id=str(term.id),
                        display_name=term_version.display_name,
                        score=score,
                        reason_codes=["BM25_GLOSSARY_TERM", "SEMANTIC_OBJECT_BOUND"],
                        metadata={
                            "term_id": str(term.id),
                            "term_key": term.term_key,
                            "bound_semantic_object_ids": [str(m.id) for m in bound_metrics],
                        },
                    )
                )

    # 8. Stored procedures and functions (R11-FP11). A routine is found by the
    # words in its name, its parameter names, the source's own description and
    # -- R11-FP08 -- its approved Atlas-authored description -- never by its
    # body, which is evidence to read on request (MCP
    # `get_transformation_detail`), not text to rank. What it stands on comes only
    # from ACTIVE procedure-lineage edges: an agent's PROPOSED edge nobody has
    # decided does not steer an answer. No boost -- a routine is context for a
    # question, never a governed tool, and must not outrank one.
    #
    # R11-FP08: the approved description also *widens the fetch*, for the reason the
    # table candidate's own `description_filters` were added above -- a routine
    # described in the question's words but named nothing like them was never fetched,
    # so its description could not score however well it was written. Only the
    # published, APPROVED version counts, the discipline the ontology candidate carries:
    # a DRAFT or PENDING_APPROVAL draft is a proposal nobody has decided, a SUPERSEDED
    # version is text the platform has replaced, and a WITHDRAWN one is text a reviewer
    # retired -- none of the three is what Atlas asserts, so none of them ranks.
    routine_filters = [func.lower(MetadataRoutine.name).contains(t) for t in query_tokens[:10]]
    routine_scope: Any = true()
    if routine_filters:
        approved_description_match = (
            select(RoutineDocumentationVersion.id)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id == MetadataRoutine.id,
                RoutineDocumentation.organization_id == datasource.organization_id,
                RoutineDocumentation.datasource_id == datasource.id,
                RoutineDocumentationVersion.organization_id == datasource.organization_id,
                RoutineDocumentationVersion.status == "APPROVED",
                or_(
                    *(
                        func.lower(RoutineDocumentationVersion.description).contains(t)
                        for t in query_tokens[:10]
                    )
                ),
            )
            .exists()
        )
        routine_scope = or_(*routine_filters, approved_description_match)
    routine_rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .where(
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.organization_id == datasource.organization_id,
                MetadataRoutine.status == "ACTIVE",
                routine_scope,
            )
            .limit(scan_limit)
        )
    ).all()
    routine_ids = [routine.id for routine, _schema_name in routine_rows]
    parameter_names: dict[UUID, list[str]] = {}
    reads_by_routine: dict[UUID, set[str]] = {}
    writes_by_routine: dict[UUID, set[str]] = {}
    triggers_by_routine: dict[UUID, set[str]] = {}
    approved_descriptions: dict[UUID, RoutineDocumentationVersion] = {}
    if routine_ids:
        # R11-FP08: the approved description of every routine that was fetched -- the
        # one widened into the candidate set above *and* the one found by its name,
        # whose description still has to score. `current_routine_descriptions`' shape
        # (ascending version, last write per routine wins) rather than a second rule.
        approved_rows = await session.execute(
            select(RoutineDocumentationVersion, RoutineDocumentation.routine_id)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id.in_(routine_ids),
                RoutineDocumentation.organization_id == datasource.organization_id,
                RoutineDocumentation.datasource_id == datasource.id,
                RoutineDocumentationVersion.organization_id == datasource.organization_id,
                RoutineDocumentationVersion.status == "APPROVED",
            )
            .order_by(RoutineDocumentationVersion.version)
        )
        approved_descriptions = {
            routine_id: version for version, routine_id in approved_rows.all()
        }
        parameter_rows = await session.execute(
            select(MetadataRoutineParameter.routine_id, MetadataRoutineParameter.name).where(
                MetadataRoutineParameter.routine_id.in_(routine_ids),
                MetadataRoutineParameter.status == "ACTIVE",
                MetadataRoutineParameter.name.is_not(None),
            )
        )
        for routine_id, parameter_name in parameter_rows.all():
            parameter_names.setdefault(routine_id, []).append(parameter_name)
        edge_rows = await session.execute(
            select(
                DeepProcedureLineageEdge.routine_id,
                DeepProcedureLineageEdge.source_table_id,
                DeepProcedureLineageEdge.target_table_id,
                DeepProcedureLineageEdge.is_write,
            ).where(
                DeepProcedureLineageEdge.routine_id.in_(routine_ids),
                DeepProcedureLineageEdge.organization_id == datasource.organization_id,
                DeepProcedureLineageEdge.review_status == "ACTIVE",
                DeepProcedureLineageEdge.is_intermediate.is_(False),
            )
        )
        for routine_id, source_table_id, target_table_id, is_write in edge_rows.all():
            if source_table_id is not None:
                reads_by_routine.setdefault(routine_id, set()).add(str(source_table_id))
            if target_table_id is not None and is_write:
                writes_by_routine.setdefault(routine_id, set()).add(str(target_table_id))
        # R11-FP01: a PostgreSQL trigger keeps no body -- its code is the function its
        # `action_routine` names, and the trigger axis reads that function with the
        # firing row bound, recording `routine_id` on each edge. So a trigger function's
        # reviewed trigger lineage *is* what that routine's body reads and writes, and
        # it joins the same two lists and so the graph stage's existing
        # ROUTINE_READS_TABLE / ROUTINE_WRITES_TABLE edges: "which function audits
        # orders" reaches the audit table no word of it names. The routine's own
        # parse cannot supply this -- inside a trigger function `NEW` names no table.
        # Same rules as the routine edges above: ACTIVE only, a temp-table hop left
        # out, and never the body. A trigger the source has dropped no longer runs
        # the function, so only an ACTIVE trigger's edges steer.
        trigger_edge_rows = await session.execute(
            select(
                TriggerLineageEdge.routine_id,
                TriggerLineageEdge.trigger_id,
                TriggerLineageEdge.source_table_id,
                TriggerLineageEdge.target_table_id,
                TriggerLineageEdge.is_write,
            )
            .join(MetadataTrigger, MetadataTrigger.id == TriggerLineageEdge.trigger_id)
            .where(
                TriggerLineageEdge.routine_id.in_(routine_ids),
                TriggerLineageEdge.organization_id == datasource.organization_id,
                TriggerLineageEdge.datasource_id == datasource.id,
                TriggerLineageEdge.review_status == "ACTIVE",
                TriggerLineageEdge.is_intermediate.is_(False),
                MetadataTrigger.organization_id == datasource.organization_id,
                MetadataTrigger.datasource_id == datasource.id,
                MetadataTrigger.status == "ACTIVE",
            )
        )
        for (
            trigger_routine_id,
            trigger_id,
            source_table_id,
            target_table_id,
            is_write,
        ) in trigger_edge_rows.all():
            if trigger_routine_id is None:
                continue
            triggers_by_routine.setdefault(trigger_routine_id, set()).add(str(trigger_id))
            if source_table_id is not None:
                reads_by_routine.setdefault(trigger_routine_id, set()).add(str(source_table_id))
            if target_table_id is not None and is_write:
                writes_by_routine.setdefault(trigger_routine_id, set()).add(
                    str(target_table_id)
                )

    for routine, schema_name in routine_rows:
        # The source's own words stay in the bag beside Atlas's: retrieval is about
        # *finding* the routine, and a source comment is still words the source uses,
        # even where `context_compiler` refuses to publish it as the platform's
        # description. What the approved version changes is the score and the reason
        # code, not whether the source comment is read.
        source_text = " ".join(
            filter(
                None,
                [routine.name, routine.source_description, *parameter_names.get(routine.id, [])],
            )
        )
        approved = approved_descriptions.get(routine.id)
        candidate_text = (
            f"{source_text} {approved.description}" if approved is not None else source_text
        )
        source_bm25 = _bm25_score(query_tokens, source_text)
        description_bm25 = (
            _bm25_score(query_tokens, approved.description) if approved is not None else 0.0
        )
        bm25 = source_bm25
        if approved is not None:
            # Keep `_APPROVED_DESCRIPTION_MATCH_WEIGHT` of the coverage the approved
            # description adds on top of what the source text already matched.
            added = (_bm25_score(query_tokens, candidate_text) - source_bm25) * (
                _APPROVED_DESCRIPTION_MATCH_WEIGHT
            )
            bm25 = min(1.0, source_bm25 + added)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score <= 0:
            continue
        hit_id = f"ROUTINE:{routine.id}"
        if hit_id in seen_ids:
            continue
        seen_ids.add(hit_id)
        # A name match and a meaning match are different evidence, and the grounding
        # receipt hashes what matched -- so an approved-description match says so in its
        # own code rather than arriving disguised as a name match. A routine reached
        # *only* through its description carries no `BM25_ROUTINE_NAME`: that code is
        # the existing claim about the routine's own identifiers (its name, its
        # parameters, the source's comment) and would be false here.
        routine_reason_codes: list[str] = []
        if source_bm25 > 0:
            routine_reason_codes.append("BM25_ROUTINE_NAME")
        description_metadata: dict[str, Any] = {}
        if approved is not None:
            description_metadata = {
                # The digest, never the prose: a hit's metadata is evidence a receipt
                # hashes, the discipline `definition_digest` already carries above.
                "description_digest": hashlib.sha256(
                    approved.description.encode("utf-8")
                ).hexdigest(),
                "description_version_id": str(approved.id),
                "description_version": approved.version,
            }
            if description_bm25 > 0:
                routine_reason_codes.extend(
                    ["BM25_ROUTINE_DESCRIPTION", "ROUTINE_DESCRIPTION_APPROVED"]
                )
        # R11-FP01: which triggers' reviewed lineage the two lists above include --
        # identifiers only, and only when there are some, so a routine no trigger
        # runs carries exactly the metadata it always did.
        trigger_metadata: dict[str, Any] = (
            {"trigger_ids": sorted(triggers_by_routine[routine.id])}
            if routine.id in triggers_by_routine
            else {}
        )
        hits.append(
            HybridRetrievalHit(
                object_type="ROUTINE",
                object_id=str(routine.id),
                display_name=f"{schema_name}.{routine.name}",
                score=score,
                reason_codes=routine_reason_codes,
                metadata={
                    "routine_id": str(routine.id),
                    "datasource_id": str(datasource.id),
                    "routine_type": routine.routine_type,
                    "signature": routine.signature,
                    "language": routine.language,
                    "reads_table_ids": sorted(reads_by_routine.get(routine.id, set())),
                    "writes_table_ids": sorted(writes_by_routine.get(routine.id, set())),
                    **trigger_metadata,
                    **description_metadata,
                    # Whether MCP `get_transformation_detail` would release the body:
                    # the same gate a person's parse applies.
                    "body_available": (
                        routine.availability == AVAILABLE
                        and routine.redaction_status in VALUE_FREE_REDACTION_STATUSES
                        and is_eligible_for_model_context(routine.screening_status)
                    ),
                },
            )
        )

    # 9. Ontology concepts (R11-FP09) -- see `_ontology_concept_hits`.
    for concept_hit in await _ontology_concept_hits(
        session,
        datasource=datasource,
        question=question,
        query_tokens=query_tokens,
        scan_limit=scan_limit,
    ):
        hit_id = f"{concept_hit.object_type}:{concept_hit.object_id}"
        if hit_id not in seen_ids:
            seen_ids.add(hit_id)
            hits.append(concept_hit)

    # ------------------------------------------------------------------
    # Sort by score desc, cap at retrieval_limit
    # ------------------------------------------------------------------
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:retrieval_limit]


# ---------------------------------------------------------------------------
# Enhanced hybrid retrieval with full-text, vector, graph, and fusion
# ---------------------------------------------------------------------------


@dataclass
class RetrievalEvidence:
    """Per-result evidence with factor breakdown.

    Every ranking factor is inspectable.  ``factors`` maps signal names
    (``lexical``, ``vector``, ``graph``, ``quality_trust``, ``usage_popularity``)
    to their weight/score detail.
    """

    object_type: str
    object_id: str
    display_name: str
    final_score: float
    fusion_method: str
    factors: list[dict[str, Any]]
    graph_expansion_path: list[str]
    source_signals: list[str]
    metadata: dict[str, Any]


async def _table_execution_counts(
    session: AsyncSession,
    *,
    datasource: DataSource,
    table_ids: set[UUID],
    scan_limit: int,
) -> dict[UUID, int]:
    """RT-6: how many of this datasource's recent completed `QueryExecution`
    rows referenced each of ``table_ids`` -- a real, already-persisted usage
    signal (the same rows AG-6 reads via ``gateway_result.execution.referenced_tables``
    once a query finishes), not a new tracking mechanism.

    `QueryExecution.referenced_tables` stores SQL-qualified name strings, not
    ids, so names are resolved back to `MetadataTable` ids with the same
    `quality_coupling.resolve_table_ids` helper TL-3/AG-6 use for the same
    name-shape ambiguity, keeping one canonical resolution path rather than a
    second hand-rolled one here.
    """
    if not table_ids:
        return {}
    rows = (
        await session.scalars(
            select(QueryExecution.referenced_tables)
            .where(
                QueryExecution.datasource_id == datasource.id,
                QueryExecution.organization_id == datasource.organization_id,
                QueryExecution.status == "COMPLETED",
            )
            .order_by(QueryExecution.created_at.desc())
            .limit(scan_limit)
        )
    ).all()
    if not rows:
        return {}

    all_names: set[str] = set()
    for referenced_tables in rows:
        all_names.update(referenced_tables or [])
    if not all_names:
        return {}

    name_to_id = await resolve_table_ids(
        session, datasource=datasource, table_names=sorted(all_names)
    )

    counts: dict[UUID, int] = {}
    for referenced_tables in rows:
        # A table referenced twice in one query counts once for that execution --
        # this measures how many past *queries* touched the table, not raw
        # token-occurrence count.
        touched = {
            table_id
            for name in (referenced_tables or [])
            if (table_id := name_to_id.get(name)) is not None and table_id in table_ids
        }
        for table_id in touched:
            counts[table_id] = counts.get(table_id, 0) + 1
    return counts


async def hybrid_retrieve_enhanced(
    session: AsyncSession,
    *,
    datasource: DataSource,
    question: str,
    settings: Settings,
    preferred_tool_version_id: UUID | None = None,
    organization_id: UUID | None = None,
    fusion_method: str = "rrf",
    include_vector: bool = True,
    include_graph: bool = True,
    max_hops: int = 2,
    candidate_limit: int | None = None,
    cancel: CancellationToken | None = None,
) -> list[HybridRetrievalHit]:
    """Compose the hybrid retrieval stages; hold no rule of its own.

    Each stage is defined in `aida.retrieval_stages` and is independently
    readable there: authorized candidates, the vector channel, the graph
    channel, the merge rule, trust, fusion, evidence. What lives here is only
    the order they run in and the two cross-cutting properties that order
    makes possible --

    * a cancellation check at every stage boundary, so a retrieval whose
      caller has gone away (or whose deadline has passed) stops between
      stages rather than finishing work nobody will read; and
    * one per-retrieval log line carrying every stage's candidate count,
      latency and mean score, so "which channel is slow" and "which channel
      has stopped contributing" are answerable without a profiler.

    `candidate_limit` bounds the authorized set explicitly. `cancel` defaults
    to no cancellation, so every existing caller behaves exactly as before.

    Backward compatible: falls back gracefully when vector or graph data is
    not available, and every channel records why it was skipped.
    """
    # Deferred import, matching the pattern this function already used for
    # `fusion_ranking`/`graph_retrieval`/`vector_*`: `retrieval_stages` imports
    # `HybridRetrievalHit` and `hybrid_retrieve` from this module, so importing
    # it at module scope would be an import cycle.
    from aida.retrieval_stages import (
        NeverCancelled,
        RetrievalRequest,
        assemble_evidence,
        check_cancelled,
        fuse,
        merge_contributions,
        run_graph_channel,
        run_trust_channel,
        run_vector_channel,
        select_authorized_candidates,
    )

    started = time.perf_counter()
    request = RetrievalRequest(
        datasource=datasource,
        question=question,
        settings=settings,
        organization_id=organization_id or datasource.organization_id,
        preferred_tool_version_id=preferred_tool_version_id,
        fusion_method=fusion_method,
        include_vector=include_vector,
        include_graph=include_graph,
        max_hops=max_hops,
        candidate_limit=candidate_limit,
        cancel=cancel or NeverCancelled(),
    )

    check_cancelled(request, "lexical")
    pool = await select_authorized_candidates(session, request)

    check_cancelled(request, "vector")
    merge_contributions(pool, await run_vector_channel(session, request, pool))

    check_cancelled(request, "graph")
    merge_contributions(pool, await run_graph_channel(session, request, pool))

    check_cancelled(request, "trust")
    merge_contributions(pool, await run_trust_channel(session, request, pool))

    check_cancelled(request, "fusion")
    ranked, config = fuse(request, pool)

    check_cancelled(request, "evidence")
    hits = assemble_evidence(pool, ranked, config)

    elapsed = time.perf_counter() - started
    RETRIEVAL_SECONDS.observe(elapsed)
    logger.info(
        "retrieval_completed",
        datasource_id=str(datasource.id),
        candidates=len(pool.candidates),
        candidate_bound=request.authorized_limit,
        candidate_bound_applied=pool.truncated,
        returned=len(hits),
        fusion_method=config.method,
        seconds=round(elapsed, 4),
        stages=pool.evidence(),
    )
    return hits

# ---------------------------------------------------------------------------
# GROUP A: RT-9 cross-source retrieval + RT-5 global-search support
# ---------------------------------------------------------------------------


async def hybrid_retrieve_cross_source(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasources: list[DataSource],
    question: str,
    settings: Settings,
    fusion_method: str = "rrf",
    include_vector: bool = True,
    include_graph: bool = True,
    max_hops: int = 2,
    limit: int | None = None,
) -> list[HybridRetrievalHit]:
    """RT-9: one query, genuinely spanning every datasource in ``datasources``.

    Before this function, `hybrid_retrieve`/`hybrid_retrieve_enhanced` both took
    a single `DataSource` and scoped every candidate query to it -- real hybrid
    (lexical + vector + graph + fusion) retrieval never crossed a datasource
    boundary, only the separate, lexical-only `search_api.py::global_search`
    surface did (03-tracker.md RT-9's own honesty note: "the cross-*source*
    half remains a separate ... surface, not this one"). This closes that
    specific gap for the hybrid pipeline.

    Approach, stated plainly: each datasource's candidates are independently
    policy-scoped and fused by `hybrid_retrieve_enhanced` (unchanged -- FK/dbt
    graph edges and business annotations never cross a datasource's own
    boundary regardless), then the per-datasource fused results are merged and
    re-sorted by their already-computed `final_score`. This is a merge-and-sort
    over independently-fused rankings, not a second joint RRF pass across the
    combined candidate pool -- because every datasource run uses the same
    `fusion_method`/weights (RRF's rank-based score or the weighted-linear sum,
    both computed the same way regardless of pool size), so the resulting
    scores are on a comparable scale. Doing a true joint fusion would require
    running every signal (lexical scan, embedding batch, graph BFS) against
    the union of all datasources' candidates in one pass, which is a larger
    restructuring than this row's scope covers -- named here rather than
    silently presented as identical to a joint pass.

    Every hit keeps its full per-datasource `retrieval_evidence` (RT-3:
    every ranking factor stays inspectable) plus the originating
    `datasource_id`, so a caller can always tell which source a result came
    from and exactly why it ranked where it did.
    """
    if not datasources:
        return []

    per_source_limit = limit or settings.agent_retrieval_limit
    merged: list[HybridRetrievalHit] = []
    for ds in datasources:
        # Sequential, not `asyncio.gather`: all datasources share one
        # `AsyncSession`, which is not safe for concurrent use across
        # coroutines.
        source_hits = await hybrid_retrieve_enhanced(
            session,
            datasource=ds,
            question=question,
            settings=settings,
            organization_id=organization_id,
            fusion_method=fusion_method,
            include_vector=include_vector,
            include_graph=include_graph,
            max_hops=max_hops,
        )
        for hit in source_hits:
            hit.metadata = {**hit.metadata, "datasource_id": str(ds.id)}
            merged.append(hit)

    merged.sort(key=lambda h: h.score, reverse=True)
    return merged[:per_source_limit]
