import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import (
    BusinessDomain,
    BusinessEntity,
    DataSource,
    DbtArtifactImport,
    DbtProject,
    DbtResource,
    GovernedTool,
    GovernedToolVersion,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataTable,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.prompt_risk import PromptRiskAssessment

STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "by",
        "for",
        "from",
        "in",
        "is",
        "latest",
        "list",
        "of",
        "on",
        "show",
        "the",
        "to",
        "with",
    }
)


def normalized_terms(text: str) -> tuple[str, ...]:
    terms = re.findall(r"[a-z0-9]+", text.lower())
    return tuple(dict.fromkeys(term for term in terms if len(term) > 1 and term not in STOP_WORDS))


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    object_type: str
    object_id: str
    display_name: str
    score: float
    reason_codes: list[str]
    metadata: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentPlan:
    strategy: Literal[
        "GOVERNED_TOOL",
        "DEVELOPMENT_SQL",
        "MODEL_GENERATION",
        "CLARIFICATION",
        "BLOCKED",
    ]
    confidence: float
    reason_codes: list[str]
    selected_tool_version_id: str | None
    required_parameters: list[str]
    retrieval_object_ids: list[str]
    prompt_risk: dict[str, object] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        return asdict(self)


def _score(terms: tuple[str, ...], text: str, *, boost: float = 0.0) -> float:
    if not terms:
        return 0.0
    normalized = text.lower().replace("_", " ")
    matched = sum(term in normalized for term in terms)
    return min(1.0, round((matched / len(terms)) + boost, 4))


class GovernedRetriever:
    """Value-free lexical retrieval over organization-scoped governed metadata."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        question: str,
        preferred_tool_version_id: UUID | None = None,
    ) -> list[RetrievalHit]:
        terms = normalized_terms(question)
        if not terms:
            return []
        name_filters = [func.lower(MetadataTable.name).contains(term) for term in terms]
        table_rows = (
            await session.scalars(
                select(MetadataTable)
                .where(
                    MetadataTable.datasource_id == datasource.id,
                    MetadataTable.organization_id == datasource.organization_id,
                    MetadataTable.status == "ACTIVE",
                    or_(*name_filters),
                )
                .limit(self.settings.agent_retrieval_scan_limit)
            )
        ).all()
        column_filters = [func.lower(MetadataColumn.name).contains(term) for term in terms]
        column_rows = list(
            await session.scalars(
                select(MetadataColumn)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(
                    MetadataTable.datasource_id == datasource.id,
                    MetadataTable.organization_id == datasource.organization_id,
                    MetadataTable.status == "ACTIVE",
                    MetadataColumn.status == "ACTIVE",
                    or_(*column_filters),
                )
                .limit(self.settings.agent_retrieval_scan_limit)
            )
        )

        metric_rows = (
            await session.execute(
                select(SemanticMetricVersion, SemanticMetric)
                .join(SemanticMetric, SemanticMetric.id == SemanticMetricVersion.metric_id)
                .join(
                    SemanticModelVersion,
                    SemanticModelVersion.id == SemanticMetricVersion.semantic_model_version_id,
                )
                .join(MetadataTable, MetadataTable.id == SemanticMetricVersion.source_table_id)
                .where(
                    SemanticModelVersion.project_id == datasource.project_id,
                    SemanticModelVersion.status == "PUBLISHED",
                    SemanticMetricVersion.status == "PUBLISHED",
                    MetadataTable.datasource_id == datasource.id,
                )
                .limit(self.settings.agent_retrieval_scan_limit)
            )
        ).all()
        tool_rows = (
            await session.execute(
                select(GovernedToolVersion, GovernedTool)
                .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
                .where(
                    GovernedToolVersion.datasource_id == datasource.id,
                    GovernedToolVersion.organization_id == datasource.organization_id,
                    GovernedToolVersion.status == "PUBLISHED",
                )
                .limit(self.settings.agent_retrieval_scan_limit)
            )
        ).all()
        dbt_project_ids = list(
            await session.scalars(
                select(DbtProject.id).where(
                    DbtProject.datasource_id == datasource.id,
                    DbtProject.organization_id == datasource.organization_id,
                    DbtProject.status == "ACTIVE",
                )
            )
        )
        latest_artifact_ids: list[UUID] = []
        if dbt_project_ids:
            artifact_rows = (
                await session.scalars(
                    select(DbtArtifactImport)
                    .where(DbtArtifactImport.dbt_project_id.in_(dbt_project_ids))
                    .order_by(
                        DbtArtifactImport.dbt_project_id,
                        DbtArtifactImport.created_at.desc(),
                    )
                )
            ).all()
            seen_projects: set[UUID] = set()
            for artifact in artifact_rows:
                if artifact.dbt_project_id not in seen_projects:
                    latest_artifact_ids.append(artifact.id)
                    seen_projects.add(artifact.dbt_project_id)
        dbt_rows = (
            list(
                await session.scalars(
                    select(DbtResource)
                    .where(DbtResource.artifact_import_id.in_(latest_artifact_ids))
                    .limit(self.settings.agent_retrieval_scan_limit)
                )
            )
            if latest_artifact_ids
            else []
        )
        business_rows = (
            await session.execute(
                select(
                    MetadataBusinessAnnotation,
                    BusinessDomain,
                    BusinessEntity,
                    MetadataTable,
                )
                .join(
                    BusinessDomain,
                    BusinessDomain.id == MetadataBusinessAnnotation.domain_id,
                )
                .join(
                    BusinessEntity,
                    BusinessEntity.id == MetadataBusinessAnnotation.entity_id,
                )
                .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
                .where(
                    MetadataBusinessAnnotation.datasource_id == datasource.id,
                    MetadataBusinessAnnotation.organization_id == datasource.organization_id,
                    MetadataTable.status == "ACTIVE",
                )
                .limit(self.settings.agent_retrieval_scan_limit)
            )
        ).all()

        hits: list[RetrievalHit] = []
        for annotation, domain, entity, table in business_rows:
            score = _score(
                terms,
                " ".join(
                    [
                        annotation.business_name,
                        annotation.business_description,
                        domain.display_name,
                        entity.display_name,
                        annotation.grain_statement,
                        " ".join(annotation.synonyms),
                        " ".join(annotation.suggested_questions),
                    ]
                ),
                boost=0.16,
            )
            if score > 0:
                hits.append(
                    RetrievalHit(
                        "BUSINESS_ENTITY",
                        str(annotation.id),
                        annotation.business_name,
                        score,
                        ["APPROVED_BUSINESS_SEMANTIC_MATCH", "VALUE_FREE_METADATA"],
                        {
                            "table_id": str(table.id),
                            "domain_key": domain.domain_key,
                            "domain_name": domain.display_name,
                            "entity_key": entity.entity_key,
                            "entity_name": entity.display_name,
                            "table_role": annotation.table_role,
                            "grain": annotation.grain_statement,
                            "annotation_version": annotation.version,
                        },
                    )
                )
        for table_row in table_rows:
            score = _score(
                terms,
                f"{table_row.name} {table_row.source_description or ''}",
                boost=0.05,
            )
            if score > 0:
                hits.append(
                    RetrievalHit(
                        "TABLE",
                        str(table_row.id),
                        table_row.name,
                        score,
                        ["LEXICAL_NAME_MATCH", "ACTIVE_METADATA"],
                        {"object_type": table_row.object_type},
                    )
                )
        for column_row in column_rows:
            score = _score(terms, column_row.name)
            if score > 0:
                hits.append(
                    RetrievalHit(
                        "COLUMN",
                        str(column_row.id),
                        column_row.name,
                        score,
                        ["LEXICAL_NAME_MATCH", "CLASSIFICATION_AWARE"],
                        {
                            "classification": column_row.classification,
                            "table_id": str(column_row.table_id),
                        },
                    )
                )
        for version, metric in metric_rows:
            score = _score(
                terms,
                f"{metric.slug} {version.name} {version.description} {version.grain}",
                boost=0.12,
            )
            if score > 0:
                hits.append(
                    RetrievalHit(
                        "SEMANTIC_METRIC",
                        str(version.id),
                        version.name,
                        score,
                        ["PUBLISHED_SEMANTIC_MATCH"],
                        {
                            "slug": metric.slug,
                            "aggregation": version.aggregation,
                            "grain": version.grain,
                            "source_table_id": str(version.source_table_id),
                            "measure_column_id": (
                                str(version.measure_column_id)
                                if version.measure_column_id
                                else None
                            ),
                            "default_time_column_id": (
                                str(version.default_time_column_id)
                                if version.default_time_column_id
                                else None
                            ),
                        },
                    )
                )
        for version, tool in tool_rows:
            score = _score(
                terms,
                f"{tool.slug} {version.name} {version.description}",
                boost=0.2,
            )
            if score > 0 or version.id == preferred_tool_version_id:
                parameters = version.parameter_schema
                hits.append(
                    RetrievalHit(
                        "GOVERNED_TOOL",
                        str(version.id),
                        version.name,
                        max(score, 1.0 if version.id == preferred_tool_version_id else 0.0),
                        [
                            "EXPLICIT_TOOL_SELECTION"
                            if version.id == preferred_tool_version_id
                            else "PUBLISHED_TOOL_MATCH"
                        ],
                        {
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
        for resource in dbt_rows:
            score = _score(
                terms,
                " ".join(
                    [
                        resource.name,
                        resource.description or "",
                        " ".join(resource.column_names),
                        " ".join(resource.tags),
                    ]
                ),
                boost=0.10 if resource.resource_type in {"MODEL", "SOURCE"} else 0.0,
            )
            if score > 0:
                hits.append(
                    RetrievalHit(
                        f"DBT_{resource.resource_type}",
                        str(resource.id),
                        resource.name,
                        score,
                        ["LATEST_DBT_ARTIFACT_MATCH", "VALUE_SAFE_TRANSFORMATION_METADATA"],
                        {
                            "unique_id": resource.unique_id,
                            "materialization": resource.materialization,
                            "table_id": (
                                str(resource.matched_table_id)
                                if resource.matched_table_id
                                else None
                            ),
                            "depends_on": resource.depends_on_unique_ids[:50],
                            "sql_fingerprint": resource.compiled_sql_hash,
                        },
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.object_type, hit.display_name))
        return hits[: self.settings.agent_retrieval_limit]


class GovernedPlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def plan(
        self,
        *,
        retrieval_hits: list[RetrievalHit],
        roles: frozenset[str],
        candidate_sql_available: bool,
        tool_parameters: dict[str, Any],
        preferred_tool_version_id: UUID | None = None,
        prompt_risk: PromptRiskAssessment | None = None,
    ) -> AgentPlan:
        risk_evidence = prompt_risk.evidence() if prompt_risk else {}
        if prompt_risk and prompt_risk.decision == "BLOCK":
            return AgentPlan(
                "BLOCKED",
                prompt_risk.score,
                ["PROMPT_POLICY_DENIED", *prompt_risk.reason_codes],
                None,
                [],
                [],
                risk_evidence,
            )
        tools = [hit for hit in retrieval_hits if hit.object_type == "GOVERNED_TOOL"]
        eligible: list[RetrievalHit] = []
        for hit in tools:
            allowed = set(hit.metadata["allowed_roles"])
            role_allowed = "PlatformAdmin" in roles or not roles.isdisjoint(allowed)
            explicitly_selected = str(preferred_tool_version_id) == hit.object_id
            if role_allowed and (
                explicitly_selected or hit.score >= self.settings.agent_tool_match_threshold
            ):
                eligible.append(hit)
        selected = eligible[0] if eligible else None
        if selected:
            required = list(selected.metadata["required_parameters"])
            missing = sorted(set(required) - set(tool_parameters))
            if not missing:
                return AgentPlan(
                    "GOVERNED_TOOL",
                    selected.score,
                    ["APPROVED_TOOL_FIRST", "ROLE_BINDING_SATISFIED"],
                    selected.object_id,
                    [],
                    [hit.object_id for hit in retrieval_hits],
                    risk_evidence,
                )
            if not candidate_sql_available or preferred_tool_version_id:
                return AgentPlan(
                    "CLARIFICATION",
                    selected.score,
                    ["APPROVED_TOOL_REQUIRES_PARAMETERS"],
                    selected.object_id,
                    missing,
                    [hit.object_id for hit in retrieval_hits],
                    risk_evidence,
                )
        if candidate_sql_available:
            return AgentPlan(
                "DEVELOPMENT_SQL",
                1.0,
                ["CONTROLLED_DEVELOPMENT_OVERRIDE"],
                None,
                [],
                [hit.object_id for hit in retrieval_hits],
                risk_evidence,
            )
        return AgentPlan(
            "MODEL_GENERATION",
            max((hit.score for hit in retrieval_hits), default=0.0),
            ["NO_EXECUTABLE_APPROVED_TOOL", "MODEL_ROUTE_REQUIRED"],
            None,
            [],
            [hit.object_id for hit in retrieval_hits],
            risk_evidence,
        )
