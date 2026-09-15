"""ADR-0029 / R11-FP14: the tool agent.

A task agent (`aida.task_agent`) -- `agent:tool` by default -- that works a backlog nothing else
worked: views and stored routines the platform has captured and parsed, with no governed tool
over them. The two tool generators existed (`view_tool_blueprint`, `procedure_tool_blueprint`),
but only a person who already knew which object to point them at could use them.

Two capabilities:

* VIEW_TOOL -- an ACTIVE view whose captured definition passes the view tool gate becomes a
  parameterised read over its own output columns.
* PROCEDURE_TOOL -- an ACTIVE routine whose stored body is *proven* read-only, with exactly one
  result statement, becomes a tool that runs that statement. It is SQL extracted from the
  routine, never a call to it: native invocation is R11-FP18.

**A proposal is a DRAFT `GovernedToolVersion` submitted into the existing `GOVERNED_TOOL_VERSION`
PUBLISH review.** That object type is T2, so a person decides every one -- the reviewer agent's
ceiling is T1 -- and the maker-checker rule refuses the agent as its own approver. Drafts are
staged by `tool_drafts.stage_tool_version_draft`, the same validation a hand-authored draft gets:
placeholders, `SqlGuard`, and the gateway's table allowlist.

**Idempotent without a table of its own.** Each source object has a deterministic slug
(`tool_slug`), so a tool that already carries it in the project -- proposed by an earlier run, or
authored by a person -- is not proposed again. An object that could not become a tool is declined
with a stable blocker code (`ViewNotEligibleError.code` and friends, never the SQL-bearing message)
and, like the lineage agent's dead ends, is not re-examined until the object itself changes.

Nothing here calls a model, reads a source value, or publishes a tool.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Final
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.edition_entitlements import evaluate_entitlement
from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.events import record_audit
from aida.models import AgentTask, DataSource, GovernedTool, MetadataSchema, MetadataTable, Project
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    ProcedureToolBlueprintError,
    build_procedure_tool_blueprint,
    resolve_procedure_tool_source,
)
from aida.review_risk_tiers import TIER_T2
from aida.routine_lineage_edges import RoutineNotEligibleError
from aida.schemas import GovernedToolVersionCreate
from aida.security import SecurityContext
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_WOULD_PROPOSE,
    CapabilityWork,
    TaskAgentCapability,
    TaskAgentItem,
    TaskAgentOutcome,
    TaskAgentRefused,
    TaskAgentRun,
    TaskAgentRunRequest,
    TaskAgentSpec,
    run_task_agent,
)
from aida.tool_drafts import ToolDraftRefused, stage_tool_version_draft
from aida.view_tool_blueprint import (
    ViewNotEligibleError,
    ViewToolBlueprintError,
    build_view_tool_blueprint,
    resolve_view_tool_source,
)
from atlas.platform.config import Settings

CAPABILITY_VIEW_TOOL: Final = "VIEW_TOOL"
CAPABILITY_PROCEDURE_TOOL: Final = "PROCEDURE_TOOL"
TOOL_VERSION_OBJECT_TYPE: Final = "GOVERNED_TOOL_VERSION"

# Declines that are not a generator's own blocker code.
SKIP_TOOL_EXISTS: Final = "tool_already_exists"
SKIP_NO_PROJECT: Final = "datasource_has_no_project"
SKIP_BLUEPRINT_REFUSED: Final = "BLUEPRINT_REFUSED"

#: The whole run is refused, not an item declined: the edition does not include tool authoring.
REASON_NOT_ENTITLED: Final = "agent_capability_not_entitled"
_AUTHORING_CAPABILITY: Final = "studio_semantic_and_tool_authoring"

#: Who may call a published tool unless the reviewer changes it before approving -- the roles
#: the platform's own generated tools already use (`semantic_inference`, `tool_api`).
DEFAULT_ALLOWED_ROLES: Final = ("Analyst", "ToolConsumer")

#: What the ledger links a decline to, so a dead end is not re-examined until it changes.
_VIEW_REF_TYPE: Final = "VIEW_DEFINITION"
_ROUTINE_REF_TYPE: Final = "ROUTINE"
#: Objects examined per run, as a multiple of the proposal limit.
_EXAMINE_FACTOR: Final = 4

TOOL_AGENT: Final = TaskAgentSpec(
    key="tool",
    audit_roles=frozenset({"ToolDeveloper"}),
    capabilities=(
        TaskAgentCapability(
            key=CAPABILITY_VIEW_TOOL,
            object_type=TOOL_VERSION_OBJECT_TYPE,
            intent="tool.propose_view_tool",
            producer="view_tool_blueprint: a parameterised read over a view's output columns",
        ),
        TaskAgentCapability(
            key=CAPABILITY_PROCEDURE_TOOL,
            object_type=TOOL_VERSION_OBJECT_TYPE,
            intent="tool.propose_procedure_tool",
            producer=(
                "procedure_tool_blueprint: the one result query of a routine proven read-only "
                "(extracted SQL, not a call to the routine)"
            ),
        ),
    ),
    # T2: a published tool is callable by agents and people. A person decides every proposal.
    max_proposal_tier=TIER_T2,
)

_SLUG_UNSAFE = re.compile(r"[^a-z0-9_]+")


def tool_slug(kind: str, schema: str, name: str, signature: str = "") -> str:
    """Deterministic per source object, and always valid for `GovernedToolVersionCreate.slug`.

    The digest keeps `a_b.c` and `a.b_c` apart, and an overloaded routine's signatures apart.
    """
    base = _SLUG_UNSAFE.sub("_", f"{kind}_{schema}_{name}".lower()).strip("_")
    digest = hashlib.sha256(f"{schema}.{name}{signature}".lower().encode()).hexdigest()[:8]
    return f"{base[:90]}_{digest}"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A source object as plain values -- read before any item's savepoint, which can expire ORM
    state on rollback."""

    ref_id: UUID
    subject_id: UUID
    schema_name: str
    object_name: str
    fingerprint: str
    signature: str
    datasource_id: UUID
    project_id: UUID | None

    @property
    def subject_name(self) -> str:
        return f"{self.schema_name}.{self.object_name}"


def _require_authoring_entitlement(run: TaskAgentRun) -> None:
    entitlement = evaluate_entitlement(
        organization_edition=run.settings.edition, capability=_AUTHORING_CAPABILITY
    )
    if not entitlement.allowed:
        raise TaskAgentRefused(REASON_NOT_ENTITLED)


def _examined_since_changed(run: TaskAgentRun, ref_type: str, ref_id: Any, updated_at: Any) -> Any:
    return exists().where(
        AgentTask.organization_id == run.organization_id,
        AgentTask.agent_principal_id == run.principal_id,
        AgentTask.proposal_ref_type == ref_type,
        AgentTask.proposal_ref_id == ref_id,
        AgentTask.started_at >= updated_at,
    )


async def _view_tools(run: TaskAgentRun) -> None:
    _require_authoring_entitlement(run)
    filters: list[Any] = [
        MetadataViewDefinition.organization_id == run.organization_id,
        MetadataViewDefinition.status == "ACTIVE",
        MetadataTable.status == "ACTIVE",
        ~_examined_since_changed(
            run, _VIEW_REF_TYPE, MetadataViewDefinition.id, MetadataViewDefinition.updated_at
        ),
    ]
    if run.datasource_id is not None:
        filters.append(MetadataViewDefinition.datasource_id == run.datasource_id)
    rows = await run.session.execute(
        select(
            MetadataViewDefinition.id,
            MetadataViewDefinition.definition_fingerprint,
            MetadataViewDefinition.fingerprint,
            MetadataTable.id,
            MetadataTable.name,
            MetadataSchema.name,
            DataSource.id,
            DataSource.project_id,
        )
        .join(MetadataTable, MetadataTable.id == MetadataViewDefinition.table_id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(DataSource, DataSource.id == MetadataViewDefinition.datasource_id)
        .where(*filters)
        .order_by(MetadataSchema.name, MetadataTable.name, MetadataViewDefinition.id)
        .limit(run.outcome.limit * _EXAMINE_FACTOR)
    )
    candidates = [
        _Candidate(
            ref_id=definition_id,
            subject_id=table_id,
            schema_name=schema_name,
            object_name=table_name,
            fingerprint=definition_fingerprint or row_fingerprint,
            signature="",
            datasource_id=datasource_id,
            project_id=project_id,
        )
        for (
            definition_id,
            definition_fingerprint,
            row_fingerprint,
            table_id,
            table_name,
            schema_name,
            datasource_id,
            project_id,
        ) in rows.all()
    ]
    await _propose_each(run, CAPABILITY_VIEW_TOOL, candidates, _propose_view_tool)


async def _procedure_tools(run: TaskAgentRun) -> None:
    _require_authoring_entitlement(run)
    filters: list[Any] = [
        MetadataRoutine.organization_id == run.organization_id,
        MetadataRoutine.status == "ACTIVE",
        ~_examined_since_changed(
            run, _ROUTINE_REF_TYPE, MetadataRoutine.id, MetadataRoutine.updated_at
        ),
    ]
    if run.datasource_id is not None:
        filters.append(MetadataRoutine.datasource_id == run.datasource_id)
    rows = await run.session.execute(
        select(
            MetadataRoutine.id,
            MetadataRoutine.body_fingerprint,
            MetadataRoutine.fingerprint,
            MetadataRoutine.name,
            MetadataRoutine.signature,
            MetadataSchema.name,
            DataSource.id,
            DataSource.project_id,
        )
        .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
        .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
        .where(*filters)
        .order_by(MetadataSchema.name, MetadataRoutine.name, MetadataRoutine.id)
        .limit(run.outcome.limit * _EXAMINE_FACTOR)
    )
    candidates = [
        _Candidate(
            ref_id=routine_id,
            subject_id=routine_id,
            schema_name=schema_name,
            object_name=routine_name,
            fingerprint=body_fingerprint or row_fingerprint,
            signature=signature,
            datasource_id=datasource_id,
            project_id=project_id,
        )
        for (
            routine_id,
            body_fingerprint,
            row_fingerprint,
            routine_name,
            signature,
            schema_name,
            datasource_id,
            project_id,
        ) in rows.all()
    ]
    await _propose_each(run, CAPABILITY_PROCEDURE_TOOL, candidates, _propose_procedure_tool)


ProposeOne = Callable[[TaskAgentRun, _Candidate], Awaitable[TaskAgentItem]]


async def _propose_each(
    run: TaskAgentRun, capability: str, candidates: list[_Candidate], propose: ProposeOne
) -> None:
    proposed = 0
    for candidate in candidates:
        if proposed >= run.outcome.limit:
            return
        if not await run.may_continue():
            return
        item = run.add(
            await run.guarded(
                capability,
                subject_id=candidate.subject_id,
                subject_name=candidate.subject_name,
                work=partial(propose, run, candidate),
            )
        )
        if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
            proposed += 1


def _inputs(capability: str, candidate: _Candidate, slug: str) -> dict[str, Any]:
    """Value-free: which object, at which version of its definition, as which tool slug."""
    return {
        "capability": capability,
        "source_id": str(candidate.ref_id),
        "definition_fingerprint": candidate.fingerprint,
        "tool_slug": slug,
    }


class _Proposal:
    """One candidate's decline and draft-and-submit paths, sharing its ledger reference."""

    def __init__(
        self, run: TaskAgentRun, capability: str, candidate: _Candidate, ref_type: str, slug: str
    ) -> None:
        self.run = run
        self.capability = capability
        self.candidate = candidate
        self.ref_type = ref_type
        self.slug = slug
        self.inputs = _inputs(capability, candidate, slug)

    async def decline(self, reason: str) -> TaskAgentItem:
        return await self.run.declined(
            self.capability,
            subject_id=self.candidate.subject_id,
            subject_name=self.candidate.subject_name,
            reason=reason,
            inputs=self.inputs,
            proposal_ref_type=self.ref_type,
            proposal_ref_id=self.candidate.ref_id,
        )

    async def target(self) -> tuple[Project, DataSource] | TaskAgentItem:
        """The project the tool would belong to, or the decline that says why there is none."""
        session = self.run.session
        datasource = await session.get(DataSource, self.candidate.datasource_id)
        project = (
            await session.get(Project, self.candidate.project_id)
            if self.candidate.project_id is not None
            else None
        )
        if datasource is None or project is None:
            return await self.decline(SKIP_NO_PROJECT)
        taken = await session.scalar(
            select(GovernedTool.id).where(
                GovernedTool.project_id == project.id, GovernedTool.slug == self.slug
            )
        )
        if taken is not None:
            return await self.decline(SKIP_TOOL_EXISTS)
        return project, datasource

    async def draft_and_submit(
        self,
        project: Project,
        datasource: DataSource,
        body: GovernedToolVersionCreate,
        details: dict[str, Any],
        *,
        source_routine: MetadataRoutine | None = None,
    ) -> TaskAgentItem:
        run = self.run
        if not run.proposing:
            return run.item(
                self.capability,
                action=ACTION_WOULD_PROPOSE,
                subject_id=self.candidate.subject_id,
                subject_name=self.candidate.subject_name,
            )
        try:
            tool, version = await stage_tool_version_draft(
                run.session,
                project,
                datasource,
                body,
                audit_context=run.agent_context,
                settings=run.settings,
                source_routine=source_routine,
            )
        except ToolDraftRefused as exc:
            return await self.decline(exc.code)
        # What `tool_api.submit_tool_for_review` does for a person's draft, as the agent's
        # own request: the version waits in the one tool review queue.
        version.status = "REVIEW_REQUIRED"
        review = await run.open_review(
            self.capability,
            object_id=version.id,
            requested_action="PUBLISH",
            details={"tool_slug": tool.slug, **details},
        )
        record_audit(
            run.session,
            run.agent_context,
            action="governed_tool.version.generated_by_agent",
            resource_type="governed_tool_version",
            resource_id=str(version.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"capability": self.capability, **self.inputs, **details},
        )
        return await run.proposed(
            self.capability,
            review=review,
            object_id=version.id,
            subject_id=self.candidate.subject_id,
            subject_name=self.candidate.subject_name,
            inputs={**self.inputs, "tool_version_id": str(version.id)},
        )


async def _propose_view_tool(run: TaskAgentRun, candidate: _Candidate) -> TaskAgentItem:
    proposal = _Proposal(
        run,
        CAPABILITY_VIEW_TOOL,
        candidate,
        _VIEW_REF_TYPE,
        tool_slug("view", candidate.schema_name, candidate.object_name),
    )
    target = await proposal.target()
    if isinstance(target, TaskAgentItem):
        return target
    project, datasource = target
    try:
        source = await resolve_view_tool_source(
            run.session,
            organization_id=run.organization_id,
            datasource_id=datasource.id,
            table_id=candidate.subject_id,
        )
        blueprint = build_view_tool_blueprint(source, dialect=datasource.dialect)
    except ViewNotEligibleError as exc:
        return await proposal.decline(exc.code)
    except ViewToolBlueprintError:
        return await proposal.decline(SKIP_BLUEPRINT_REFUSED)
    body = GovernedToolVersionCreate(
        slug=proposal.slug,
        name=candidate.subject_name[:200],
        description=(
            f"Reads the output columns of the view {candidate.subject_name}, optionally filtered "
            "by any of its filterable columns. Proposed by the tool agent from the view's "
            "captured definition; review the SQL and the allowed roles before publishing."
        ),
        datasource_id=datasource.id,
        sql_template=blueprint.sql_template,
        parameters=list(blueprint.parameters),
        allowed_roles=list(DEFAULT_ALLOWED_ROLES),
    )
    return await proposal.draft_and_submit(
        project,
        datasource,
        body,
        {"source_kind": "VIEW", "table_id": str(candidate.subject_id)},
    )


async def _propose_procedure_tool(run: TaskAgentRun, candidate: _Candidate) -> TaskAgentItem:
    proposal = _Proposal(
        run,
        CAPABILITY_PROCEDURE_TOOL,
        candidate,
        _ROUTINE_REF_TYPE,
        tool_slug("routine", candidate.schema_name, candidate.object_name, candidate.signature),
    )
    target = await proposal.target()
    if isinstance(target, TaskAgentItem):
        return target
    project, datasource = target
    try:
        routine, result_node, parse_result, parameters = await resolve_procedure_tool_source(
            run.session,
            organization_id=run.organization_id,
            datasource_id=datasource.id,
            routine_id=candidate.subject_id,
            dialect=datasource.dialect,
        )
        blueprint = build_procedure_tool_blueprint(
            result_node,
            parameters,
            dialect=datasource.dialect,
            statement_count=parse_result.statement_count,
            sql_hash=parse_result.sql_hash,
        )
    except (RoutineNotEligibleError, ProcedureNotEligibleError) as exc:
        return await proposal.decline(exc.code)
    except ProcedureToolBlueprintError:
        return await proposal.decline(SKIP_BLUEPRINT_REFUSED)
    body = GovernedToolVersionCreate(
        slug=proposal.slug,
        name=candidate.subject_name[:200],
        description=(
            f"Runs the one result query of {candidate.subject_name} as a read-only tool: the "
            "query extracted from the routine's stored body, not a call to the routine. "
            "Proposed by the tool agent; review the SQL and the allowed roles before publishing."
        ),
        datasource_id=datasource.id,
        sql_template=blueprint.sql_template,
        parameters=list(blueprint.parameters),
        allowed_roles=list(DEFAULT_ALLOWED_ROLES),
    )
    return await proposal.draft_and_submit(
        project,
        datasource,
        body,
        {
            "source_kind": "ROUTINE",
            "routine_id": str(candidate.subject_id),
            "sql_hash": blueprint.sql_hash,
            "statement_count": blueprint.statement_count,
        },
        source_routine=routine,
    )


TOOL_WORK: Final[Mapping[str, CapabilityWork]] = {
    CAPABILITY_VIEW_TOOL: _view_tools,
    CAPABILITY_PROCEDURE_TOOL: _procedure_tools,
}


async def run_tool_agent(
    session: AsyncSession,
    organization_id: UUID,
    *,
    request: TaskAgentRunRequest,
    settings: Settings,
    triggered_by: SecurityContext,
) -> TaskAgentOutcome:
    """One bounded tool-agent run; see `task_agent.run_task_agent`."""
    return await run_task_agent(
        session,
        organization_id,
        spec=TOOL_AGENT,
        work=TOOL_WORK,
        request=request,
        settings=settings,
        triggered_by=triggered_by,
    )
