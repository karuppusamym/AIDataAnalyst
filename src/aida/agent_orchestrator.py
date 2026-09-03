import hashlib
import hmac
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_intelligence import GovernedPlanner, GovernedRetriever, RetrievalHit
from aida.agent_runtime import RuntimeStage, RuntimeState
from aida.ai_decision_lineage import (
    DECISION_LINEAGE_VERSION,
    AiDecisionEdge,
    record_decision,
    record_decisions,
)
from aida.answer_provenance import compose_lineage_provenance
from aida.business_annotation_versions import (
    annotation_version_content_digest,
    resolve_annotation_version,
)
from aida.config import Settings
from aida.events import record_audit, record_outbox
from aida.model_gateway import (
    ApprovedModelRoute,
    ModelGatewayError,
    ProviderNeutralModelGateway,
    SqlGenerationOutput,
)
from aida.models import (
    AgentRun,
    AnalysisRun,
    DataSource,
    GovernedToolVersion,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    ModelRouteConfiguration,
    SemanticModelVersion,
    ToolExecution,
)
from aida.prompt_risk import DeterministicPromptRiskClassifier
from aida.quality_coupling import (
    check_quality_gate,
    check_tool_gate,
    demote_in_retrieval,
    fetch_open_incidents,
    get_trust_warning,
    resolve_table_ids,
)
from aida.query_gateway import GatewayResult, QueryExecutionGateway, QueryRejected
from aida.query_memory import MemoryMatch, find_query_memory_match, retrieved_table_ids_from_hits
from aida.schemas import ToolParameterDefinition
from aida.security import SecurityContext
from aida.semantic_inference import (
    format_ambiguous_definition_refusal,
    resolve_scoped_glossary_term,
)
from aida.tool_rendering import ToolParameterError, render_tool_sql
from aida.trust_scoring import AssetContext, compute_trust_score


class ModelRouteUnavailable(RuntimeError):
    pass


class AgentClarificationRequired(RuntimeError):
    pass


class AgentPolicyRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentOrchestrationResult:
    agent_run: AgentRun
    gateway_result: GatewayResult
    explanation: str


def _trace(
    state: RuntimeState, control_type: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    trace: dict[str, object] = {
        "sequence": state.step_count,
        "stage": state.stage.value,
        "control_type": control_type,
    }
    if details:
        trace["details"] = details
    return trace


def _record_retrieval_decisions(
    session: AsyncSession,
    organization_id: UUID,
    run_id: UUID,
    selected: list[RetrievalHit],
    rejected: list[RetrievalHit],
) -> None:
    """AU-5: RETRIEVAL_SELECTED for the hits handed to the planner, RETRIEVAL_REJECTED
    for candidates ranked below ``agent_retrieval_limit``. Value-free: identifiers,
    scores and reason codes only, never the question or matched content.
    """
    total = len(selected) + len(rejected)
    edges = [
        AiDecisionEdge(
            run_id=run_id,
            decision_type="RETRIEVAL_SELECTED",
            source_node="governed_retriever",
            target_node=f"{hit.object_type.lower()}:{hit.object_id}",
            reason=f"ranked #{rank} of {total} candidates (score={hit.score})",
            evidence={
                "score": hit.score,
                "reason_codes": hit.reason_codes,
                "object_type": hit.object_type,
                "rank": rank,
            },
            control_version=DECISION_LINEAGE_VERSION,
        )
        for rank, hit in enumerate(selected, start=1)
    ]
    edges.extend(
        AiDecisionEdge(
            run_id=run_id,
            decision_type="RETRIEVAL_REJECTED",
            source_node="governed_retriever",
            target_node=f"{hit.object_type.lower()}:{hit.object_id}",
            reason=f"ranked below the retrieval limit ({len(selected)}); score={hit.score}",
            evidence={
                "score": hit.score,
                "reason_codes": hit.reason_codes,
                "object_type": hit.object_type,
            },
            control_version=DECISION_LINEAGE_VERSION,
        )
        for hit in rejected
    )
    if edges:
        record_decisions(session, organization_id, edges)


async def _check_definition_ambiguity(
    session: AsyncSession,
    *,
    datasource: DataSource,
    retrieval_hits: list[RetrievalHit],
) -> str | None:
    """Group K / AT-9: the refusal check itself, wired into the real grounded
    run. Every distinct term_key this run's own retrieval evidence surfaced
    (a `GLOSSARY_TERM` hit -- `retrieval.hybrid_retrieve`'s own metadata
    shape, unchanged by this hook) is resolved against this datasource's
    business-graph scope; the first ambiguous one becomes this run's refusal
    message. Returns `None` when nothing surfaced is ambiguous -- including
    when nothing surfaced is a glossary term at all, the common case.
    """
    term_keys = {
        hit.metadata["term_key"]
        for hit in retrieval_hits
        if hit.object_type == "GLOSSARY_TERM" and "term_key" in hit.metadata
    }
    for term_key in sorted(term_keys):
        resolution = await resolve_scoped_glossary_term(
            session,
            organization_id=datasource.organization_id,
            term_key=term_key,
            datasource_id=datasource.id,
        )
        if resolution.status == "AMBIGUOUS":
            return format_ambiguous_definition_refusal(term_key, resolution.alternatives)
    return None


def _canonical_json(value: Any) -> bytes:
    """Deterministic byte encoding for content hashing (AT-6): sorted keys, no
    incidental whitespace, so the same content always digests identically.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


async def _compute_grounding_fragment_digests(
    session: AsyncSession, retrieval_hits: list[RetrievalHit]
) -> list[dict[str, Any]]:
    """AT-6: hash every grounding fragment assembled into this run's context --
    the same set of retrieval hits `retrieval_evidence` already records -- and
    return one value-free entry per fragment for `AgentRun.grounding_fragment_digests`.

    A `BUSINESS_ANNOTATION` hit's fragment is the exact content of the current
    `MetadataBusinessAnnotationVersion` it resolved to at retrieval time
    (`retrieval.py` stamps `metadata["annotation_version_id"]`), so the digest
    is computed from that versioned content and the version id is recorded
    alongside it -- letting `agent_run_replay.resolve_grounding` point back at
    precisely this content even after a later approval supersedes it
    (`business_annotation_versions.write_annotation_version` never mutates a
    superseded row, so it stays resolvable by id). Every other hit type has no
    separately versioned content in this codebase yet, so its fragment is its
    own value-free identifiers (`object_type`, `object_id`, `display_name`,
    `metadata`) -- still a real digest of what was assembled, just not one that
    survives a change to the underlying object's free text.
    """
    entries: list[dict[str, Any]] = []
    for hit in retrieval_hits:
        annotation_version_id: str | None = None
        fragment_digest: str | None = None
        if hit.object_type == "BUSINESS_ANNOTATION":
            raw_version_id = hit.metadata.get("annotation_version_id")
            version = (
                await resolve_annotation_version(session, UUID(str(raw_version_id)))
                if raw_version_id
                else None
            )
            if version is not None:
                annotation_version_id = str(version.id)
                fragment_digest = annotation_version_content_digest(version)
        if fragment_digest is None:
            content: dict[str, Any] = {
                "object_type": hit.object_type,
                "object_id": hit.object_id,
                "display_name": hit.display_name,
                "metadata": hit.metadata,
            }
            fragment_digest = f"sha256:{hashlib.sha256(_canonical_json(content)).hexdigest()}"
        entries.append(
            {
                "object_type": hit.object_type,
                "object_id": hit.object_id,
                "fragment_digest": fragment_digest,
                "annotation_version_id": annotation_version_id,
            }
        )
    return entries


class GovernedAgentOrchestrator:
    """Framework-neutral orchestrator with deterministic gates around model output."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.query_gateway = QueryExecutionGateway(settings)
        self.retriever = GovernedRetriever(settings)
        self.planner = GovernedPlanner(settings)
        self.prompt_risk_classifier = DeterministicPromptRiskClassifier()
        self.model_gateway = ProviderNeutralModelGateway(settings)

    async def _approved_model_route(
        self, session: AsyncSession, organization_id: UUID
    ) -> ApprovedModelRoute | None:
        if not self.settings.model_route:
            return None
        route = await session.scalar(
            select(ModelRouteConfiguration)
            .where(
                ModelRouteConfiguration.organization_id == organization_id,
                ModelRouteConfiguration.route_key == self.settings.model_route,
                ModelRouteConfiguration.status == "APPROVED",
            )
            .order_by(ModelRouteConfiguration.version.desc())
            .limit(1)
        )
        if (
            route is None
            or "SQL_GENERATION" not in route.capabilities
            or not route.credential_reference
        ):
            return None
        return ApprovedModelRoute(
            route_key=route.route_key,
            provider_type=route.provider_type,
            model_id=route.model_id,
            endpoint_alias=route.endpoint_alias,
            credential_reference=route.credential_reference,
            max_input_tokens=route.max_input_tokens,
            max_output_tokens=route.max_output_tokens,
            timeout_seconds=route.timeout_seconds,
        )

    async def _model_context(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        retrieval_hits: list[Any],
    ) -> dict[str, Any]:
        table_ids: set[UUID] = set()
        for hit in retrieval_hits:
            if hit.object_type == "TABLE":
                table_ids.add(UUID(hit.object_id))
            table_id = hit.metadata.get("table_id") or hit.metadata.get("source_table_id")
            if table_id:
                table_ids.add(UUID(str(table_id)))
        bounded_ids = list(sorted(table_ids, key=str))[:25]
        if not bounded_ids:
            return {"dialect": datasource.dialect, "tables": [], "constraints": []}
        table_rows = (
            await session.execute(
                select(MetadataTable, MetadataSchema)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .where(
                    MetadataTable.id.in_(bounded_ids),
                    MetadataTable.datasource_id == datasource.id,
                    MetadataTable.status == "ACTIVE",
                )
                .order_by(MetadataSchema.name, MetadataTable.name)
            )
        ).all()
        active_ids = [table.id for table, _schema in table_rows]
        columns = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.table_id.in_(active_ids),
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
                .limit(1000)
            )
        ).all()
        constraints = (
            await session.scalars(
                select(MetadataConstraint)
                .where(
                    MetadataConstraint.table_id.in_(active_ids),
                    MetadataConstraint.status == "ACTIVE",
                )
                .order_by(MetadataConstraint.table_id, MetadataConstraint.name)
                .limit(500)
            )
        ).all()
        columns_by_table: dict[UUID, list[dict[str, Any]]] = {}
        for column in columns:
            columns_by_table.setdefault(column.table_id, []).append(
                {
                    "id": str(column.id),
                    "name": column.name,
                    "physical_type": column.physical_type,
                    "nullable": column.nullable,
                    "classification": column.classification,
                }
            )
        table_names = {table.id: f"{schema.name}.{table.name}" for table, schema in table_rows}
        return {
            "dialect": datasource.dialect,
            "tables": [
                {
                    "id": str(table.id),
                    "qualified_name": table_names[table.id],
                    "object_type": table.object_type,
                    "columns": columns_by_table.get(table.id, []),
                }
                for table, _schema in table_rows
            ],
            "constraints": [
                {
                    "id": str(constraint.id),
                    "type": constraint.constraint_type,
                    "source_table": table_names.get(constraint.table_id),
                    "source_columns": constraint.columns,
                    "target_table": table_names.get(constraint.referenced_table_id),
                    "target_columns": constraint.referenced_columns,
                }
                for constraint in constraints
            ],
        }

    async def run(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        context: SecurityContext,
        correlation_id: str,
        question: str,
        candidate_sql: str | None,
        preferred_tool_version_id: UUID | None,
        tool_parameters: dict[str, Any],
        requested_limit: int | None,
    ) -> AgentOrchestrationResult:
        agent_run = AgentRun(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            principal_id=context.principal_id,
            question_hash=hmac.new(
                self.settings.audit_hmac_key.encode("utf-8"),
                question.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
            generation_source="PENDING",
        )
        session.add(agent_run)
        await session.flush()

        state = RuntimeState(request_id=str(agent_run.id))
        trace = [_trace(state, "DETERMINISTIC")]
        state = state.transition(RuntimeStage.AUTHORIZED, policy_version=agent_run.policy_version)
        trace.append(_trace(state, "DETERMINISTIC"))

        prompt_risk = self.prompt_risk_classifier.assess(question)
        state = state.transition(RuntimeStage.SCREENED)
        trace.append(
            _trace(
                state,
                "DETERMINISTIC",
                {
                    "decision": prompt_risk.decision,
                    "risk_score": prompt_risk.score,
                    "reason_codes": prompt_risk.reason_codes,
                    "classifier_version": prompt_risk.classifier_version,
                },
            )
        )
        if prompt_risk.decision == "BLOCK":
            plan = self.planner.plan(
                retrieval_hits=[],
                roles=context.roles,
                candidate_sql_available=candidate_sql is not None,
                tool_parameters=tool_parameters,
                preferred_tool_version_id=preferred_tool_version_id,
                prompt_risk=prompt_risk,
            )
            agent_run.generation_source = "POLICY_BLOCK"
            agent_run.plan_evidence = plan.evidence()
            await self._persist_rejection(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                "PROMPT_POLICY_DENIED",
            )
            raise AgentPolicyRejected("request rejected by deterministic prompt safety controls")

        latest_analysis = await session.scalar(
            select(AnalysisRun)
            .where(
                AnalysisRun.datasource_id == datasource.id,
                AnalysisRun.organization_id == datasource.organization_id,
                AnalysisRun.status == "COMPLETED",
            )
            .order_by(AnalysisRun.updated_at.desc())
            .limit(1)
        )
        if latest_analysis is None:
            return await self._reject(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                "NO_COMPLETED_METADATA_ANALYSIS",
            )
        published_semantic_model = await session.scalar(
            select(SemanticModelVersion)
            .where(
                SemanticModelVersion.project_id == datasource.project_id,
                SemanticModelVersion.organization_id == datasource.organization_id,
                SemanticModelVersion.status == "PUBLISHED",
            )
            .order_by(SemanticModelVersion.version.desc())
            .limit(1)
        )
        semantic_version = (
            f"semantic-model:{published_semantic_model.id}:v{published_semantic_model.version}"
            if published_semantic_model
            else f"technical-metadata:{latest_analysis.id}"
        )
        agent_run.semantic_version = semantic_version
        # `score_candidates` (unbounded, sorted, read-only) rather than `retrieve`
        # (its bounded public wrapper) so the candidates the `agent_retrieval_limit`
        # cap discards are visible here too, as RETRIEVAL_REJECTED evidence -- the
        # recording itself lives here, not in `agent_intelligence.py`, so that
        # module (also used by the read-only retrieval-preview endpoint) stays free
        # of any write the INV-7 read-only-route gate would trip on.
        scored_candidates = await self.retriever.score_candidates(
            session,
            datasource=datasource,
            question=question,
            preferred_tool_version_id=preferred_tool_version_id,
        )
        retrieval_hits = scored_candidates[: self.settings.agent_retrieval_limit]
        rejected_candidates = scored_candidates[self.settings.agent_retrieval_limit :]
        _record_retrieval_decisions(
            session,
            datasource.organization_id,
            agent_run.id,
            retrieval_hits,
            rejected_candidates,
        )
        retrieval_evidence = [hit.evidence() for hit in retrieval_hits]
        agent_run.retrieval_evidence = retrieval_evidence
        # AT-6: fragment-level content receipts, computed at the moment these
        # hits are assembled as this run's grounding -- see
        # `_compute_grounding_fragment_digests` for what gets hashed and why.
        agent_run.grounding_fragment_digests = await _compute_grounding_fragment_digests(
            session, retrieval_hits
        )
        # Group K / AT-9: where a term/metric this question's evidence surfaced
        # resolves to more than one governed definition for this datasource's
        # business-graph scope, refuse with both definitions and both owners
        # rather than silently picking one. See
        # `semantic_inference.resolve_scoped_glossary_term` for the
        # most-specific-wins resolution this checks.
        ambiguity_reason = await _check_definition_ambiguity(
            session, datasource=datasource, retrieval_hits=retrieval_hits
        )
        if ambiguity_reason is not None:
            await self._persist_rejection(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                "AMBIGUOUS_DEFINITION",
            )
            raise AgentClarificationRequired(ambiguity_reason)
        state = state.transition(RuntimeStage.RESOLVED, semantic_version=semantic_version)
        trace.append(
            _trace(
                state,
                "DETERMINISTIC",
                {
                    "semantic_version": semantic_version,
                    "retrieval_evidence_count": len(retrieval_evidence),
                },
            )
        )

        plan = self.planner.plan(
            retrieval_hits=retrieval_hits,
            roles=context.roles,
            candidate_sql_available=candidate_sql is not None,
            tool_parameters=tool_parameters,
            preferred_tool_version_id=preferred_tool_version_id,
            prompt_risk=prompt_risk,
        )
        plan_evidence = plan.evidence()
        agent_run.plan_evidence = plan_evidence
        if plan.tool_decisions:
            record_decisions(
                session,
                datasource.organization_id,
                [
                    AiDecisionEdge(
                        run_id=agent_run.id,
                        decision_type=(
                            "TOOL_SELECTED"
                            if decision["decision"] == "SELECTED"
                            else "TOOL_REJECTED"
                        ),
                        source_node="governed_planner",
                        target_node=f"tool:{decision['tool_version_id']}",
                        reason=decision["reason"],
                        control_version=DECISION_LINEAGE_VERSION,
                    )
                    for decision in plan.tool_decisions
                ],
            )
        agent_run.recommended_tool_version_id = (
            UUID(plan.selected_tool_version_id) if plan.selected_tool_version_id else None
        )
        state = state.transition(
            RuntimeStage.PLANNED,
            logical_plan={
                "datasource_id": str(datasource.id),
                "strategy": plan.strategy,
                "confidence": plan.confidence,
                "retrieval_evidence_count": len(retrieval_evidence),
                "selected_tool_version_id": plan.selected_tool_version_id,
            },
        )
        trace.append(
            _trace(
                state,
                "HYBRID_BOUNDARY",
                {
                    "strategy": plan.strategy,
                    "confidence": plan.confidence,
                    "reason_codes": plan.reason_codes,
                    "selected_tool_version_id": plan.selected_tool_version_id,
                },
            )
        )

        if plan.strategy == "CLARIFICATION":
            reason = f"MISSING_TOOL_PARAMETERS:{','.join(plan.required_parameters)}"
            await self._persist_rejection(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                reason,
            )
            raise AgentClarificationRequired(
                f"approved tool requires parameters: {', '.join(plan.required_parameters)}"
            )

        tool_execution: ToolExecution | None = None
        generation_source: str
        generated_sql: str
        if plan.strategy == "GOVERNED_TOOL" and plan.selected_tool_version_id:
            version = await session.get(GovernedToolVersion, UUID(plan.selected_tool_version_id))
            if version is None or version.status != "PUBLISHED":
                await self._persist_rejection(
                    session,
                    agent_run,
                    state,
                    trace,
                    context,
                    correlation_id,
                    "PLANNED_TOOL_UNAVAILABLE",
                )
                raise ModelRouteUnavailable("planned governed tool is unavailable")
            # DQ-3/TL-3 parity: `tool_api.py::execute_tool` blocks a governed
            # tool's HTTP execution route on its own dependency's open quality
            # incidents *before* rendering or executing any SQL. Every path
            # that can execute a governed tool version -- the MCP tool-call
            # handler routes here via `GovernedAgentOrchestrator.run`, not
            # through `execute_tool` -- must reach the identical fail-closed
            # gate, or the same tool version answers differently depending on
            # which surface asked for it (ADR-0016: no ambiguity/missing
            # signal silently passes). Checked on the tool's own declared
            # `referenced_tables`, the same dependency set `execute_tool`
            # gates on, not the post-execution `referenced_tables` the
            # gateway later reports -- catching this before a single row is
            # read from the source, not after.
            dependency_table_ids = await resolve_table_ids(
                session, datasource=datasource, table_names=version.referenced_tables
            )
            dependency_incidents = await fetch_open_incidents(
                session, datasource=datasource, table_ids=list(dependency_table_ids.values())
            )
            tool_quality_gate = check_tool_gate(
                tool_id=str(version.tool_id),
                dependency_asset_ids=[str(t) for t in dependency_table_ids.values()],
                incidents=dependency_incidents,
            )
            if tool_quality_gate.action == "BLOCK":
                await self._persist_rejection(
                    session,
                    agent_run,
                    state,
                    trace,
                    context,
                    correlation_id,
                    f"QUALITY_INCIDENT_BLOCK:{','.join(tool_quality_gate.affected_assets)}",
                )
                raise AgentPolicyRejected(tool_quality_gate.message)
            if tool_quality_gate.action == "WARN":
                plan_evidence["tool_quality_gate"] = {
                    "action": tool_quality_gate.action,
                    "affected_assets": tool_quality_gate.affected_assets,
                    "message": tool_quality_gate.message,
                }
                agent_run.plan_evidence = plan_evidence
            try:
                rendered = render_tool_sql(
                    version.sql_template,
                    dialect=datasource.dialect,
                    definitions=[
                        ToolParameterDefinition.model_validate(item)
                        for item in version.parameter_schema
                    ],
                    values=tool_parameters,
                )
            except ToolParameterError as exc:
                await self._persist_rejection(
                    session,
                    agent_run,
                    state,
                    trace,
                    context,
                    correlation_id,
                    "INVALID_TOOL_PARAMETERS",
                )
                raise AgentClarificationRequired(str(exc)) from exc
            fingerprint = hmac.new(
                self.settings.audit_hmac_key.encode(),
                json.dumps(
                    rendered.normalized_parameters, sort_keys=True, separators=(",", ":")
                ).encode(),
                hashlib.sha256,
            ).hexdigest()
            tool_execution = ToolExecution(
                organization_id=datasource.organization_id,
                tool_version_id=version.id,
                principal_id=context.principal_id,
                parameter_fingerprint=fingerprint,
            )
            session.add(tool_execution)
            await session.flush()
            generated_sql = rendered.sql
            generation_source = "GOVERNED_TOOL"
        elif plan.strategy == "DEVELOPMENT_SQL" and candidate_sql:
            if not self.settings.allow_development_sql_override:
                await self._persist_rejection(
                    session,
                    agent_run,
                    state,
                    trace,
                    context,
                    correlation_id,
                    "DEVELOPMENT_SQL_OVERRIDE_DISABLED",
                )
                raise ModelRouteUnavailable("development SQL override is disabled")
            generated_sql = candidate_sql
            generation_source = "DEVELOPMENT_OVERRIDE"
        else:
            # AG-7: look for a version-checked, structurally similar prior
            # successful query *before* asking the model to generate anything.
            # This never bypasses generation or validation -- it only changes
            # what grounding the same `structured_completion` call below
            # receives, and the SQL it returns still reaches the identical
            # `self.query_gateway.execute(...)` guard call every other
            # strategy uses (see query_memory.py's module docstring for why
            # the match is offered as a redacted structural shape, never as
            # literal-bearing SQL to replay directly).
            memory_match: MemoryMatch | None = None
            if self.settings.agent_query_memory_enabled:
                memory_match = await find_query_memory_match(
                    session,
                    datasource=datasource,
                    current_semantic_version=semantic_version,
                    retrieved_table_ids=retrieved_table_ids_from_hits(retrieval_hits),
                    min_similarity=self.settings.agent_query_memory_min_similarity,
                    scan_limit=self.settings.agent_query_memory_scan_limit,
                )
            try:
                approved_route = await self._approved_model_route(
                    session, datasource.organization_id
                )
                model_context = await self._model_context(
                    session,
                    datasource=datasource,
                    retrieval_hits=retrieval_hits,
                )
                system_instruction = (
                    "Return exactly one read-only SQL SELECT statement for the supplied "
                    "dialect. "
                    "Use only qualified tables, columns, and joins present in the supplied "
                    "metadata context. Never invent an identifier or include source values."
                )
                payload: dict[str, Any] = {
                    "question": question,
                    "datasource_id": str(datasource.id),
                    "semantic_version": semantic_version,
                    "retrieval_evidence": retrieval_evidence,
                    "metadata_context": model_context,
                }
                if memory_match is not None:
                    system_instruction += (
                        " A structurally similar prior successful query is supplied as "
                        "query_memory_template, with its literal values already redacted. "
                        "Adapt its shape to this question where it genuinely fits; "
                        "otherwise generate fresh SQL from the metadata context alone."
                    )
                    payload["query_memory_template"] = memory_match.normalized_sql
                output, model_evidence = await self.model_gateway.structured_completion(
                    session=session,
                    organization_id=datasource.organization_id,
                    route=approved_route,
                    system_instruction=system_instruction,
                    payload=payload,
                    output_schema=SqlGenerationOutput,
                )
                generated_sql = output.sql
                generation_source = (
                    "QUERY_MEMORY_ADAPTATION" if memory_match is not None else "MODEL_GATEWAY"
                )
                agent_run.model_route = model_evidence.route
                plan_evidence["model_call_evidence"] = {
                    "route": model_evidence.route,
                    "provider_type": model_evidence.provider_type,
                    "model_id": model_evidence.model_id,
                    "endpoint_alias": model_evidence.endpoint_alias,
                    "input_fingerprint": model_evidence.input_fingerprint,
                    "output_fingerprint": model_evidence.output_fingerprint,
                    "schema_name": model_evidence.schema_name,
                }
                if memory_match is not None:
                    plan_evidence["query_memory_match"] = memory_match.evidence()
                agent_run.plan_evidence = plan_evidence
            except ModelGatewayError as exc:
                await self._persist_rejection(
                    session,
                    agent_run,
                    state,
                    trace,
                    context,
                    correlation_id,
                    "MODEL_ROUTE_NOT_CONFIGURED",
                )
                raise ModelRouteUnavailable(str(exc)) from exc

        agent_run.generation_source = generation_source
        state = state.transition(RuntimeStage.GENERATED, generated_sql=generated_sql)
        trace.append(
            _trace(
                state,
                generation_source,
                {"selected_tool_version_id": plan.selected_tool_version_id},
            )
        )
        try:
            gateway_result = await self.query_gateway.execute(
                session,
                datasource=datasource,
                context=context,
                correlation_id=correlation_id,
                sql=generated_sql,
                requested_limit=requested_limit,
                semantic_version=semantic_version,
            )
        except QueryRejected as exc:
            state = state.transition(RuntimeStage.REJECTED, failure_reason=str(exc))
            trace.append(_trace(state, "DETERMINISTIC"))
            agent_run.status = state.stage.value
            agent_run.failure_reason = str(exc)[:1000]
            agent_run.query_execution_id = exc.execution_id
            agent_run.step_trace = trace
            if tool_execution:
                tool_execution.status = "REJECTED"
                tool_execution.query_execution_id = exc.execution_id
                tool_execution.error_message = str(exc)[:1000]
            record_decision(
                session,
                agent_run.organization_id,
                AiDecisionEdge(
                    run_id=agent_run.id,
                    decision_type="REFUSAL",
                    source_node="query_execution_gateway",
                    target_node=f"agent_run:{agent_run.id}",
                    reason=str(exc)[:1000] or "QUERY_GATEWAY_DENIED",
                    evidence={
                        "stage": state.stage.value,
                        "correlation_id": correlation_id,
                        "datasource_id": str(agent_run.datasource_id),
                        "query_execution_id": (
                            str(exc.execution_id) if exc.execution_id else None
                        ),
                    },
                    control_version=DECISION_LINEAGE_VERSION,
                ),
            )
            await session.commit()
            raise

        # C3: VALIDATED, COSTED, EXECUTED, EXPLAINED and COMPLETED are five
        # independently-gated checkpoints, each able to refuse the run in its
        # own right, rather than a single `for` loop stamping the trace after
        # `query_gateway.execute()` had already returned. The work each state
        # names (AST/allowlist validation, the cost ceiling, read-only bounded
        # masked execution) genuinely already happened inside that one
        # `execute()` call -- INV-2 keeps SQL execution to that single choke
        # point, so it cannot be re-run five times -- but until now the
        # orchestrator never independently checked any of it, and had no way
        # to refuse on any of the five separately. Every checkpoint below is
        # the orchestrator's own re-verification of that work's *result*
        # against policy it holds independently of the gateway, so a defect
        # in the gateway's internal enforcement does not silently pass
        # through as a governed answer. See `Docs/20-modules/13-agent-runtime.md`
        # section 3 for the target this closes.
        validated_failure = await self._checkpoint_validated(
            session, datasource=datasource, gateway_result=gateway_result
        )
        if validated_failure:
            await self._deny_after_execution(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                gateway_result=gateway_result,
                tool_execution=tool_execution,
                target_stage=RuntimeStage.REJECTED,
                checkpoint="VALIDATED",
                reason=validated_failure,
            )
        state = state.transition(RuntimeStage.VALIDATED)
        trace.append(_trace(state, "CHECKPOINT_VALIDATED"))

        costed_failure = self._checkpoint_costed(gateway_result=gateway_result)
        if costed_failure:
            await self._deny_after_execution(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                gateway_result=gateway_result,
                tool_execution=tool_execution,
                target_stage=RuntimeStage.REJECTED,
                checkpoint="COSTED",
                reason=costed_failure,
            )
        state = state.transition(RuntimeStage.COSTED)
        trace.append(_trace(state, "CHECKPOINT_COSTED"))

        executed_failure = self._checkpoint_executed(
            gateway_result=gateway_result, requested_limit=requested_limit
        )
        if executed_failure:
            await self._deny_after_execution(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                gateway_result=gateway_result,
                tool_execution=tool_execution,
                target_stage=RuntimeStage.REJECTED,
                checkpoint="EXECUTED",
                reason=executed_failure,
            )
        state = state.transition(RuntimeStage.EXECUTED)
        trace.append(_trace(state, "CHECKPOINT_EXECUTED"))

        # AG-6/EXPLAINED: assemble the answer's quality/trust signals and --
        # new in this change -- actually gate on them. TL-3 already blocks a
        # *governed tool* before it runs when a dependency has an open
        # CRITICAL incident (`check_quality_gate`); a model-generated or
        # development-override answer had no equivalent, only a warning after
        # the fact. This checkpoint closes that gap by applying the same
        # gate TL-3 uses to the tables the answer actually came from.
        explained_failure, trust_evidence = await self._checkpoint_explained(
            session, datasource=datasource, gateway_result=gateway_result
        )
        if explained_failure:
            await self._deny_after_execution(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                gateway_result=gateway_result,
                tool_execution=tool_execution,
                target_stage=RuntimeStage.FAILED,
                checkpoint="EXPLAINED",
                reason=explained_failure,
            )
        state = state.transition(RuntimeStage.EXPLAINED)
        trace.append(_trace(state, "CHECKPOINT_EXPLAINED"))
        if trust_evidence:
            plan_evidence["trust"] = trust_evidence
            agent_run.plan_evidence = plan_evidence

        # AT-16: the answer's own lineage provenance -- columns, derivation
        # method (edge_source) and a pinned graph version for every unified-
        # lineage relationship directly between the tables this answer's
        # executed SQL referenced. Independent of EXPLAINED's quality-gate
        # early return above (that returns early absent an open incident;
        # this runs whenever the answer resolved at least one cited table),
        # so it is composed here rather than folded into `_checkpoint_explained`.
        lineage_evidence = await self._compose_lineage_provenance(
            session, datasource=datasource, gateway_result=gateway_result
        )
        if lineage_evidence:
            plan_evidence["lineage"] = lineage_evidence
            agent_run.plan_evidence = plan_evidence

        completed_failure = self._checkpoint_completed(
            agent_run=agent_run, gateway_result=gateway_result
        )
        if completed_failure:
            await self._deny_after_execution(
                session,
                agent_run,
                state,
                trace,
                context,
                correlation_id,
                gateway_result=gateway_result,
                tool_execution=tool_execution,
                target_stage=RuntimeStage.FAILED,
                checkpoint="COMPLETED",
                reason=completed_failure,
            )
        state = state.transition(RuntimeStage.COMPLETED)
        trace.append(_trace(state, "CHECKPOINT_COMPLETED"))
        agent_run.status = state.stage.value
        agent_run.query_execution_id = gateway_result.execution.id
        agent_run.step_trace = trace
        if tool_execution:
            tool_execution.status = "COMPLETED"
            tool_execution.query_execution_id = gateway_result.execution.id

        explanation = self._deterministic_explanation(gateway_result)
        if trust_evidence and trust_evidence["warnings"]:
            explanation += (
                f" TRUST WARNING (grade {trust_evidence['trust_grade']}, "
                f"score {trust_evidence['trust_score']}/100): "
                + " ".join(warning["message"] for warning in trust_evidence["warnings"])
            )
        record_audit(
            session,
            context,
            action="agent.analysis.complete",
            resource_type="agent_run",
            resource_id=str(agent_run.id),
            outcome="SUCCESS",
            correlation_id=correlation_id,
            details={
                "query_execution_id": str(gateway_result.execution.id),
                "semantic_version": semantic_version,
                "generation_source": generation_source,
                "plan_strategy": plan.strategy,
                "recommended_tool_version_id": plan.selected_tool_version_id,
            },
        )
        record_outbox(
            session,
            organization_id=datasource.organization_id,
            aggregate_type="agent_run",
            aggregate_id=str(agent_run.id),
            event_type="agent.analysis.completed.v1",
            payload={
                "agent_run_id": str(agent_run.id),
                "query_execution_id": str(gateway_result.execution.id),
                "datasource_id": str(datasource.id),
            },
        )
        await session.commit()
        return AgentOrchestrationResult(agent_run, gateway_result, explanation)

    async def _reject(
        self,
        session: AsyncSession,
        agent_run: AgentRun,
        state: RuntimeState,
        trace: list[dict[str, object]],
        context: SecurityContext,
        correlation_id: str,
        reason: str,
    ) -> AgentOrchestrationResult:
        await self._persist_rejection(
            session, agent_run, state, trace, context, correlation_id, reason
        )
        raise ModelRouteUnavailable(reason)

    async def _persist_rejection(
        self,
        session: AsyncSession,
        agent_run: AgentRun,
        state: RuntimeState,
        trace: list[dict[str, object]],
        context: SecurityContext,
        correlation_id: str,
        reason: str,
    ) -> None:
        state = state.transition(RuntimeStage.REJECTED, failure_reason=reason)
        trace.append(_trace(state, "DETERMINISTIC", {"reason_code": reason}))
        agent_run.status = state.stage.value
        agent_run.failure_reason = reason
        agent_run.step_trace = trace
        record_decision(
            session,
            agent_run.organization_id,
            AiDecisionEdge(
                run_id=agent_run.id,
                decision_type="REFUSAL",
                source_node="governed_agent_orchestrator",
                target_node=f"agent_run:{agent_run.id}",
                reason=reason,
                evidence={
                    "stage": state.stage.value,
                    "correlation_id": correlation_id,
                    "datasource_id": str(agent_run.datasource_id),
                },
                control_version=DECISION_LINEAGE_VERSION,
            ),
        )
        record_audit(
            session,
            context,
            action="agent.analysis",
            resource_type="agent_run",
            resource_id=str(agent_run.id),
            outcome="DENIED",
            correlation_id=correlation_id,
            details={"reason": reason},
        )
        await session.commit()

    async def _checkpoint_validated(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        gateway_result: GatewayResult,
    ) -> str | None:
        """VALIDATED: independently re-derive the table allowlist and confirm
        every table the executed statement actually touched is still in it.

        `QueryExecutionGateway.execute()` already ran the deterministic
        AST/allowlist pass internally (`_run_validation`) before the
        connector was ever opened -- this calls the same public
        `allowed_tables` it used, again, from the orchestrator, against the
        table list the execution actually recorded. A defect that let the
        gateway's internal enforcement drift from what `allowed_tables`
        itself reports (or a stale/mutated allowlist between validation and
        this point) is caught here rather than trusted silently -- "the
        model's influence ends here" holds even if the first check had a
        bug.
        """
        allowed = await self.query_gateway.allowed_tables(session, datasource)
        unauthorized = sorted(
            {
                table
                for table in gateway_result.execution.referenced_tables
                if table.lower() not in allowed
            }
        )
        if unauthorized:
            return f"VALIDATED_TABLE_NOT_ALLOWLISTED:{','.join(unauthorized)}"
        return None

    def _checkpoint_costed(self, *, gateway_result: GatewayResult) -> str | None:
        """COSTED: independently re-verify the persisted cost evidence.

        `execute()` already gated the estimate against whichever budget
        applied -- cost-shaped (`max_query_estimate_cost`) or byte-shaped
        (`max_query_estimate_bytes`), selected by `gate_query_estimate`
        structurally from the connector's own estimate shape. `QueryExecution`
        does not persist which shape applied, so re-deriving the *exact*
        budget here is not possible without a second connector call, which
        INV-2 forbids. This checkpoint instead independently re-checks the
        failure modes that would matter regardless of shape: the evidence
        must be a finite, non-negative number, and it must never exceed the
        more permissive of the two configured ceilings -- a plan cost above
        that is wrong under any interpretation of the estimate.
        """
        plan_cost = gateway_result.execution.plan_cost
        if plan_cost is None:
            return None
        if not math.isfinite(plan_cost) or plan_cost < 0:
            return f"COSTED_EVIDENCE_INVALID:{plan_cost}"
        ceiling = max(self.settings.max_query_estimate_cost, self.settings.max_query_estimate_bytes)
        if plan_cost > ceiling:
            return f"COSTED_PLAN_COST_EXCEEDS_POLICY:{plan_cost}>{ceiling}"
        return None

    def _checkpoint_executed(
        self, *, gateway_result: GatewayResult, requested_limit: int | None
    ) -> str | None:
        """EXECUTED: independently re-verify the row bound held.

        `SqlGuard` already computed and applied a `LIMIT` clause for exactly
        this bound before the statement reached the connector -- this is the
        orchestrator's own check that the rows which actually came back
        respect it, the same defence-in-depth shape as VALIDATED and COSTED:
        a source that ignores its own `LIMIT` clause, or a future bug in how
        the bound is threaded through, is caught here rather than handed to
        the caller as a governed, bounded answer.
        """
        row_count = gateway_result.execution.row_count
        if row_count is None:
            return "EXECUTED_ROW_COUNT_MISSING"
        cap = min(
            requested_limit or self.settings.default_query_row_limit,
            self.settings.hard_query_row_limit,
        )
        if row_count > cap:
            return f"EXECUTED_ROW_COUNT_EXCEEDS_BOUND:{row_count}>{cap}"
        return None

    async def _checkpoint_explained(
        self, session: AsyncSession, *, datasource: DataSource, gateway_result: GatewayResult
    ) -> tuple[str | None, dict[str, Any] | None]:
        """EXPLAINED: assemble quality/trust signals for the answer's own
        source tables (AG-6), and gate on them.

        Reads the same `IncidentSummary` rows TL-3's tool gate checks, via
        the shared `quality_coupling` wiring helpers, so gating and warning
        cannot silently disagree about which incidents are active. Unlike
        the pre-existing AG-6 behaviour (warning only), an open CRITICAL
        incident on a table the answer actually came from now refuses the
        run via the same `check_quality_gate` TL-3 already uses to block a
        governed tool before it runs -- closing the gap where a
        model-generated or development-override answer could surface data
        from a critically incident-affected table with nothing stronger than
        a warning appended after the fact.
        """
        answer_table_ids = await resolve_table_ids(
            session,
            datasource=datasource,
            table_names=gateway_result.execution.referenced_tables,
        )
        if not answer_table_ids:
            return None, None
        answer_incidents = await fetch_open_incidents(
            session, datasource=datasource, table_ids=list(answer_table_ids.values())
        )
        if not answer_incidents:
            return None, None
        distinct_asset_ids = {str(table_id) for table_id in answer_table_ids.values()}
        blocking = sorted(
            asset_id
            for asset_id in distinct_asset_ids
            if (gate_result := check_quality_gate(asset_id, answer_incidents)) is not None
            and gate_result.gate_action == "BLOCK"
        )
        if blocking:
            return f"EXPLAINED_QUALITY_INCIDENT_BLOCK:{','.join(blocking)}", None
        warnings = [
            warning
            for asset_id in sorted(distinct_asset_ids)
            if (warning := get_trust_warning(asset_id, answer_incidents)) is not None
        ]
        worst_factor = min(
            (demote_in_retrieval(asset_id, answer_incidents) for asset_id in distinct_asset_ids),
            default=1.0,
        )
        trust_score = compute_trust_score(AssetContext(quality_score=round(worst_factor * 100)))
        trust_evidence = {
            "trust_score": trust_score.overall_score,
            "trust_grade": trust_score.grade,
            "factors": [asdict(factor) for factor in trust_score.factors],
            "warnings": [asdict(warning) for warning in warnings],
        }
        return None, trust_evidence

    async def _compose_lineage_provenance(
        self, session: AsyncSession, *, datasource: DataSource, gateway_result: GatewayResult
    ) -> dict[str, Any] | None:
        """AT-16: resolve the answer's own referenced tables and hand them to
        `aida.answer_provenance.compose_lineage_provenance` -- see that
        module for the composed shape and the graph-version pin's rationale.

        Resolves `answer_table_ids` independently of `_checkpoint_explained`
        (which also resolves it, but only as a means to its own quality-gate
        check and returns early when there is no open incident) so this
        block is composed for every answer that cites a resolvable table,
        not only ones with an active quality incident.
        """
        answer_table_ids = await resolve_table_ids(
            session,
            datasource=datasource,
            table_names=gateway_result.execution.referenced_tables,
        )
        return await compose_lineage_provenance(
            session,
            datasource=datasource,
            answer_table_ids=answer_table_ids,
            queried_columns=list(gateway_result.execution.referenced_columns),
            settings=self.settings,
        )

    def _checkpoint_completed(
        self, *, agent_run: AgentRun, gateway_result: GatewayResult
    ) -> str | None:
        """COMPLETED: the run may only be marked complete, and its evidence
        handed back as the system of record, once every field that evidence
        depends on is actually present. A future coding error that reaches
        this point with a hollow record is refused here rather than silently
        persisted as a governed, auditable success.
        """
        if not gateway_result.execution.sql_hash:
            return "COMPLETED_EVIDENCE_MISSING:sql_hash"
        if not agent_run.semantic_version:
            return "COMPLETED_EVIDENCE_MISSING:semantic_version"
        if not agent_run.plan_evidence:
            return "COMPLETED_EVIDENCE_MISSING:plan_evidence"
        return None

    async def _deny_after_execution(
        self,
        session: AsyncSession,
        agent_run: AgentRun,
        state: RuntimeState,
        trace: list[dict[str, object]],
        context: SecurityContext,
        correlation_id: str,
        *,
        gateway_result: GatewayResult,
        tool_execution: ToolExecution | None,
        target_stage: RuntimeStage,
        checkpoint: str,
        reason: str,
    ) -> NoReturn:
        """Shared denial path for a post-execution checkpoint that refuses.

        Mirrors the bookkeeping the pre-execution `_persist_rejection` and
        the `QueryRejected` handler above both do -- run status, failure
        reason, trace, tool-execution status, a `REFUSAL` decision-lineage
        edge, an audit record, commit -- but attributes the refusal to the
        specific checkpoint that fired (via `source_node` and the
        `checkpoint` audit detail) and keeps `query_execution_id` set,
        because unlike a pre-execution rejection, the query genuinely ran.
        `target_stage` is caller-supplied rather than always `REJECTED`
        because the runtime state machine only allows `REJECTED` from
        `GENERATED`/`VALIDATED`/`COSTED`; `EXECUTED`/`EXPLAINED` may only
        advance to `FAILED`.
        """
        state = state.transition(target_stage, failure_reason=reason)
        trace.append(_trace(state, f"CHECKPOINT_{checkpoint}", {"reason_code": reason}))
        agent_run.status = state.stage.value
        agent_run.failure_reason = reason[:1000]
        agent_run.query_execution_id = gateway_result.execution.id
        agent_run.step_trace = trace
        if tool_execution:
            tool_execution.status = "REJECTED"
            tool_execution.query_execution_id = gateway_result.execution.id
            tool_execution.error_message = reason[:1000]
        outcome = "DENIED" if target_stage == RuntimeStage.REJECTED else "FAILURE"
        record_decision(
            session,
            agent_run.organization_id,
            AiDecisionEdge(
                run_id=agent_run.id,
                decision_type="REFUSAL",
                source_node=f"checkpoint:{checkpoint.lower()}",
                target_node=f"agent_run:{agent_run.id}",
                reason=reason,
                evidence={
                    "stage": state.stage.value,
                    "checkpoint": checkpoint,
                    "correlation_id": correlation_id,
                    "datasource_id": str(agent_run.datasource_id),
                    "query_execution_id": str(gateway_result.execution.id),
                },
                control_version=DECISION_LINEAGE_VERSION,
            ),
        )
        record_audit(
            session,
            context,
            action="agent.analysis",
            resource_type="agent_run",
            resource_id=str(agent_run.id),
            outcome=outcome,
            correlation_id=correlation_id,
            details={"reason": reason, "checkpoint": checkpoint},
        )
        await session.commit()
        rejected = QueryRejected(reason)
        rejected.execution_id = gateway_result.execution.id
        raise rejected

    @staticmethod
    def _deterministic_explanation(result: GatewayResult) -> str:
        masked = ", ".join(result.masked_columns) if result.masked_columns else "none"
        tables = ", ".join(result.execution.referenced_tables)
        return (
            f"Returned {result.execution.row_count or 0} governed rows from {tables}. "
            f"Masked sensitive output columns: {masked}. "
            f"Execution evidence: {result.execution.id}."
        )
