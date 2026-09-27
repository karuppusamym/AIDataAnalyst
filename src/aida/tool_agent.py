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

**A hand-written tool over a routine's query is offered its binding (R11-FP14, 2026-09-18).** A
tool generated from a routine records the routine and its body's fingerprint, so R11-FP16 holds it
when the routine moves. A person who writes that same query by hand gets no such link, and their
tool keeps answering after the routine's logic has changed. The PROCEDURE_TOOL capability now
compares each hand-written tool's SQL with the routine's extracted result query *structurally*
(`procedure_tool_blueprint.structural_query_key`: same parse and render as `SqlGuard`, every value
position erased, never a literal compared). A match does not bind anything: the agent stages a new
version of the person's tool -- their SQL, name, parameters and roles unchanged -- bound to the
routine, and submits it to the same T2 `GOVERNED_TOOL_VERSION` review, where a person confirms or
rejects the binding. An agent proposes and a human approves, as everywhere else; binding
automatically would let a coincidental match hold someone's published tool with nobody having
decided it. A rejected binding is not proposed again for the same routine definition.

Nothing here calls a model, reads a source value, or publishes a tool.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Final
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.edition_entitlements import evaluate_entitlement
from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter, MetadataViewDefinition
from aida.events import record_audit
from aida.models import (
    AgentTask,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    MetadataSchema,
    MetadataTable,
    Project,
)
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    ProcedureToolBlueprintError,
    build_procedure_tool_blueprint,
    resolve_procedure_tool_source,
    routine_result_query_key,
    structural_query_key,
)
from aida.review_risk_tiers import TIER_T2
from aida.routine_lineage_edges import RoutineNotEligibleError
from aida.schemas import GovernedToolVersionCreate, ToolParameterDefinition
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
#: R11-FP14: something live already stands on this view or routine, under any slug.
SKIP_SOURCE_HAS_TOOL: Final = "source_already_has_a_tool"
#: R11-FP14: a hand-written tool is this routine's result query, and so is another routine's in
#: the same source -- which one it was written from cannot be told, so no binding is proposed.
SKIP_ROUTINE_MATCH_AMBIGUOUS: Final = "hand_written_tool_matches_several_routines"
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
#: R11-FP14: live hand-written tool versions compared per datasource per run, newest first. A
#: stated bound rather than the whole tool estate: parsing is cheap but not free, and a tool
#: beyond it is compared once a newer one no longer crowds it out, or when its routine changes.
_HAND_WRITTEN_TOOL_SCAN: Final = 1000
#: R11-FP14: routines parsed per hand-written tool when asking which one it was written from --
#: only routines whose stored body names every table the tool reads get this far.
_ROUTINES_PER_TOOL: Final = 25
#: Matching a hand-written tool: a live version's status before the binding may be offered. A
#: draft or one in review is still its author's to change; the binding is offered once it is
#: published, which moves its `updated_at` and so brings the routine back for examination.
_BINDABLE_STATUS: Final = "PUBLISHED"
_IN_FLIGHT_STATUSES: Final = ("DRAFT", "REVIEW_REQUIRED")

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
    # R11-FP14: the scan above only reaches a routine that changed since it was last examined,
    # and the case this exists for is usually the other way round -- generation declined the
    # routine (a literal in its result query, say), and *afterwards* a person wrote the tool by
    # hand with the value re-supplied. So each recently changed hand-written tool also brings back
    # the routine it structurally is, if that routine has not been examined since the tool moved.
    # Those go first: they are the ones known to have something to propose.
    reached = await _routines_reached_by_hand_written_tools(
        run,
        await _hand_written_tools(
            run, datasource_id=run.datasource_id, limit=run.outcome.limit * _EXAMINE_FACTOR
        ),
    )
    already = {candidate.subject_id for candidate in reached}
    candidates = [*reached, *(c for c in candidates if c.subject_id not in already)]
    await _propose_each(
        run,
        CAPABILITY_PROCEDURE_TOOL,
        candidates,
        partial(_propose_procedure_tool, hand_written=_HandWrittenToolIndex(run)),
    )


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


#: Statuses in which a tool version still stands on its source: a draft someone is writing, one
#: waiting for review, and the published one. A rejected or superseded version stands on nothing.
_LIVE_TOOL_STATUSES: Final = ("DRAFT", "REVIEW_REQUIRED", "PUBLISHED")


def _reads_only(referenced: Any, schema_name: str, object_name: str) -> bool:
    """Whether a tool's referenced tables are this one object and nothing else.

    A tool that joins the view with another table is a different tool, and this view still has
    none of its own; only a tool that reads exactly this object makes a proposal a duplicate.
    The three name shapes the gateway authorises -- `catalog.schema.object`, `schema.object` and
    an unambiguous bare name -- all resolve to the same object here.
    """
    qualified = f"{schema_name}.{object_name}".lower()
    names = {str(name).lower() for name in referenced or []}
    return bool(names) and all(
        name == object_name.lower() or name == qualified or name.endswith(f".{qualified}")
        for name in names
    )


async def _source_already_has_a_tool(
    session: AsyncSession, candidate: _Candidate, *, by_name: bool
) -> bool:
    """Whether a live tool version in this source already stands on this object.

    The slug check above catches the agent's own second proposal; this catches the one a person
    wrote first. A generated tool is matched by its source binding, whatever it was named. A
    hand-written one has no binding, so a view is matched by the names its SQL references --
    which is why `by_name` is false for a routine: a routine tool runs the routine's extracted
    result query, and the routine's own name never appears in what that query references. A
    hand-written tool over a routine is matched by *structure* instead, before this runs
    (`_HandWrittenToolIndex`, R11-FP14 2026-09-18).
    """
    rows = (
        await session.execute(
            select(
                GovernedToolVersion.referenced_tables,
                GovernedToolVersion.source_view_table_id,
                GovernedToolVersion.source_routine_id,
            ).where(
                GovernedToolVersion.datasource_id == candidate.datasource_id,
                GovernedToolVersion.status.in_(_LIVE_TOOL_STATUSES),
            )
        )
    ).all()
    for referenced, source_view_table_id, source_routine_id in rows:
        if candidate.subject_id in (source_view_table_id, source_routine_id):
            return True
        if by_name and _reads_only(referenced, candidate.schema_name, candidate.object_name):
            return True
    return False


# --------------------------------------------------------------------------- #
# R11-FP14: a hand-written tool that is a routine's extracted query
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _HandWrittenTool:
    """A live tool version that names no source, as plain values, with its structural key.

    Plain values for the reason `_Candidate` is: these are read outside any item's savepoint and
    used inside several, and a rolled-back savepoint can expire ORM state.
    """

    version_id: UUID
    tool_id: UUID
    slug: str
    status: str
    datasource_id: UUID
    referenced_tables: tuple[str, ...]
    updated_at: datetime
    key: str

    @property
    def table_leaves(self) -> tuple[str, ...]:
        """The bare names of what it reads -- `catalog.schema.table` to `table` -- lowercased."""
        leaves = {
            str(name).split(".")[-1].strip('"`[]').lower() for name in self.referenced_tables
        }
        return tuple(sorted(leaf for leaf in leaves if leaf))


async def _hand_written_tools(
    run: TaskAgentRun, *, datasource_id: UUID | None, limit: int
) -> list[_HandWrittenTool]:
    """Live tool versions in scope that no view or routine binding stands behind, newest first.

    A version whose SQL does not parse, or that is a bare projection, gets no key and is left
    out: `structural_query_key` refuses to match those at all.
    """
    filters: list[Any] = [
        GovernedToolVersion.organization_id == run.organization_id,
        DataSource.organization_id == run.organization_id,
        GovernedToolVersion.status.in_(_LIVE_TOOL_STATUSES),
        GovernedToolVersion.source_routine_id.is_(None),
        GovernedToolVersion.source_view_table_id.is_(None),
    ]
    if datasource_id is not None:
        filters.append(GovernedToolVersion.datasource_id == datasource_id)
    rows = await run.session.execute(
        select(
            GovernedToolVersion.id,
            GovernedToolVersion.tool_id,
            GovernedTool.slug,
            GovernedToolVersion.status,
            GovernedToolVersion.datasource_id,
            GovernedToolVersion.referenced_tables,
            GovernedToolVersion.updated_at,
            GovernedToolVersion.sql_template,
            DataSource.dialect,
        )
        .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
        .join(DataSource, DataSource.id == GovernedToolVersion.datasource_id)
        .where(*filters)
        .order_by(GovernedToolVersion.updated_at.desc(), GovernedToolVersion.id)
        .limit(limit)
    )
    tools: list[_HandWrittenTool] = []
    for (
        version_id,
        tool_id,
        slug,
        status,
        tool_datasource_id,
        referenced,
        updated_at,
        sql_template,
        dialect,
    ) in rows.all():
        key = structural_query_key(sql_template, dialect=dialect)
        if key is None:
            continue
        tools.append(
            _HandWrittenTool(
                version_id=version_id,
                tool_id=tool_id,
                slug=slug,
                status=status,
                datasource_id=tool_datasource_id,
                referenced_tables=tuple(str(name) for name in referenced or ()),
                updated_at=updated_at,
                key=key,
            )
        )
    return tools


class _HandWrittenToolIndex:
    """Each datasource's hand-written tools, read and parsed once per run, looked up by key."""

    def __init__(self, run: TaskAgentRun) -> None:
        self._run = run
        self._by_datasource: dict[UUID, list[_HandWrittenTool]] = {}

    async def matching(self, datasource_id: UUID, key: str) -> list[_HandWrittenTool]:
        if datasource_id not in self._by_datasource:
            self._by_datasource[datasource_id] = await _hand_written_tools(
                self._run, datasource_id=datasource_id, limit=_HAND_WRITTEN_TOOL_SCAN
            )
        return [tool for tool in self._by_datasource[datasource_id] if tool.key == key]


async def _in_parameter_names(
    session: AsyncSession, routine_ids: Iterable[UUID]
) -> dict[UUID, list[str]]:
    """Declared IN/INOUT parameter names per routine -- the filter tool generation reads."""
    ids = list(routine_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(MetadataRoutineParameter.routine_id, MetadataRoutineParameter.name).where(
            MetadataRoutineParameter.routine_id.in_(ids),
            MetadataRoutineParameter.status == "ACTIVE",
            MetadataRoutineParameter.mode.in_(("IN", "INOUT")),
            MetadataRoutineParameter.name.is_not(None),
        )
    )
    names: dict[UUID, list[str]] = {}
    for routine_id, name in rows.all():
        names.setdefault(routine_id, []).append(name)
    return names


async def _routines_reading(
    run: TaskAgentRun,
    tool: _HandWrittenTool,
    *,
    not_examined_since: datetime | None = None,
    excluding: UUID | None = None,
) -> list[tuple[MetadataRoutine, str, UUID | None, str]]:
    """ACTIVE routines in the tool's source whose stored body names every table the tool reads.

    A cheap, conservative prefilter in SQL before anything is parsed: a routine whose body never
    mentions one of the tool's tables cannot have the tool's query as its result statement.
    `(routine, schema name, project id, dialect)`, capped at `_ROUTINES_PER_TOOL`.
    """
    filters: list[Any] = [
        MetadataRoutine.organization_id == run.organization_id,
        MetadataRoutine.datasource_id == tool.datasource_id,
        MetadataRoutine.status == "ACTIVE",
        MetadataRoutine.body_sql_redacted.is_not(None),
        *(
            func.lower(MetadataRoutine.body_sql_redacted).contains(leaf, autoescape=True)
            for leaf in tool.table_leaves
        ),
    ]
    if not_examined_since is not None:
        filters.append(
            ~_examined_since_changed(
                run, _ROUTINE_REF_TYPE, MetadataRoutine.id, not_examined_since
            )
        )
    if excluding is not None:
        filters.append(MetadataRoutine.id != excluding)
    rows = await run.session.execute(
        select(MetadataRoutine, MetadataSchema.name, DataSource.project_id, DataSource.dialect)
        .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
        .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
        .where(*filters)
        .order_by(MetadataSchema.name, MetadataRoutine.name, MetadataRoutine.id)
        .limit(_ROUTINES_PER_TOOL)
    )
    return [(routine, schema, project, dialect) for routine, schema, project, dialect in rows.all()]


async def _routines_reached_by_hand_written_tools(
    run: TaskAgentRun, tools: list[_HandWrittenTool]
) -> list[_Candidate]:
    """Routines a hand-written tool structurally *is*, not examined since that tool changed."""
    found: dict[UUID, _Candidate] = {}
    for tool in tools:
        if not tool.table_leaves:
            continue
        routines = await _routines_reading(run, tool, not_examined_since=tool.updated_at)
        parameters = await _in_parameter_names(
            run.session, (routine.id for routine, *_rest in routines)
        )
        for routine, schema_name, project_id, dialect in routines:
            if routine.id in found:
                continue
            key = routine_result_query_key(
                routine, parameters.get(routine.id, ()), dialect=dialect
            )
            if key != tool.key:
                continue
            found[routine.id] = _Candidate(
                ref_id=routine.id,
                subject_id=routine.id,
                schema_name=schema_name,
                object_name=routine.name,
                fingerprint=routine.body_fingerprint or routine.fingerprint,
                signature=routine.signature,
                datasource_id=routine.datasource_id,
                project_id=project_id,
            )
    return list(found.values())


async def _binding_may_be_offered(
    session: AsyncSession, tool: _HandWrittenTool, routine: MetadataRoutine
) -> bool:
    """Whether proposing `tool`'s binding to `routine` is new, uncontested and the tool's to take.

    Not when the matched version is not yet published (its author is still writing it), when
    another version of the tool is in flight (the proposal would race it), when the tool is or
    was bound to a different source (a tool's source never changes -- `tool_drafts` refuses that
    anyway), or when this exact binding -- this routine, at this definition -- was proposed
    before, whatever became of it: a rejection is a person's answer and is not asked again until
    the routine's definition moves.
    """
    if tool.status != _BINDABLE_STATUS:
        return False
    rows = await session.execute(
        select(
            GovernedToolVersion.id,
            GovernedToolVersion.status,
            GovernedToolVersion.source_routine_id,
            GovernedToolVersion.source_view_table_id,
            GovernedToolVersion.source_definition_fingerprint,
        ).where(GovernedToolVersion.tool_id == tool.tool_id)
    )
    for version_id, status, routine_id, view_table_id, fingerprint in rows.all():
        if version_id != tool.version_id and status in _IN_FLIGHT_STATUSES:
            return False
        if view_table_id is not None:
            return False
        if routine_id is not None and routine_id != routine.id:
            return False
        if routine_id == routine.id and fingerprint == routine.body_fingerprint:
            return False
    return True


async def _another_routine_has_this_query(
    run: TaskAgentRun, tool: _HandWrittenTool, routine: MetadataRoutine
) -> bool:
    """Whether a second routine in the source has the same result query, making the match a guess.

    Two copies of one procedure are common in a real estate (a `_v2`, a per-region clone). A tool
    matching both was written from one of them or from neither, and binding it to the wrong one
    would hold it when that one changes and leave it unheld when the other does.
    """
    others = await _routines_reading(run, tool, excluding=routine.id)
    parameters = await _in_parameter_names(run.session, (other.id for other, *_rest in others))
    return any(
        routine_result_query_key(other, parameters.get(other.id, ()), dialect=dialect) == tool.key
        for other, _schema, _project, dialect in others
    )


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

    async def locate(self) -> tuple[Project, DataSource] | TaskAgentItem:
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
        return project, datasource

    async def target(self) -> tuple[Project, DataSource] | TaskAgentItem:
        """`locate`, then the decline that says the object already has a tool, if it does."""
        located = await self.locate()
        if isinstance(located, TaskAgentItem):
            return located
        project, _datasource = located
        spoken_for = await self.spoken_for(project)
        return spoken_for if spoken_for is not None else located

    async def spoken_for(self, project: Project) -> TaskAgentItem | None:
        """The decline for an object a tool already stands on -- by slug, or by binding."""
        session = self.run.session
        taken = await session.scalar(
            select(GovernedTool.id).where(
                GovernedTool.project_id == project.id, GovernedTool.slug == self.slug
            )
        )
        if taken is not None:
            return await self.decline(SKIP_TOOL_EXISTS)
        # R11-FP14: the same source under a different slug -- a tool a person wrote first, or a
        # generated one somebody renamed. Proposing a second tool over it would put two callables
        # with the same answer in front of every agent, and a reviewer would have to notice.
        if await _source_already_has_a_tool(
            session, self.candidate, by_name=self.capability == CAPABILITY_VIEW_TOOL
        ):
            return await self.decline(SKIP_SOURCE_HAS_TOOL)
        return None

    async def draft_and_submit(
        self,
        project: Project,
        datasource: DataSource,
        body: GovernedToolVersionCreate,
        details: dict[str, Any],
        *,
        source_routine: MetadataRoutine | None = None,
        source_view: MetadataViewDefinition | None = None,
        related_id: UUID | None = None,
        related_name: str | None = None,
    ) -> TaskAgentItem:
        run = self.run
        if not run.proposing:
            return run.item(
                self.capability,
                action=ACTION_WOULD_PROPOSE,
                subject_id=self.candidate.subject_id,
                subject_name=self.candidate.subject_name,
                related_id=related_id,
                related_name=related_name,
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
                source_view=source_view,
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
            related_id=related_id,
            related_name=related_name,
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
        source_view=await run.session.scalar(
            select(MetadataViewDefinition).where(
                MetadataViewDefinition.table_id == candidate.subject_id
            )
        ),
    )


async def _propose_binding(
    run: TaskAgentRun,
    candidate: _Candidate,
    routine: MetadataRoutine,
    datasource: DataSource,
    matches: list[_HandWrittenTool],
) -> TaskAgentItem:
    """Offer the binding of a hand-written tool to the routine whose extracted query it is.

    Offered, never made: the result is a new DRAFT version of the person's own tool -- their SQL,
    name, description, parameters, roles and semantic model, unchanged -- bound to the routine at
    its current definition, in the T2 `GOVERNED_TOOL_VERSION` review a person decides. Approving it
    publishes the bound version in place of the unbound one, and from then on R11-FP16 holds the
    tool when the routine moves (and `context_rebuild` drafts its regeneration for review, as for
    any routine-bound tool). Rejecting it leaves the tool exactly as it was, and
    `_binding_may_be_offered` does not ask again until the routine's definition changes.

    However many hand-written tools match, the generated tool is not proposed: the routine already
    has a tool standing on it, which is the duplicate R11-FP14's source check exists to stop.
    """
    decline = _Proposal(
        run,
        CAPABILITY_PROCEDURE_TOOL,
        candidate,
        _ROUTINE_REF_TYPE,
        tool_slug("routine", candidate.schema_name, candidate.object_name, candidate.signature),
    ).decline
    offerable = [
        tool
        for tool in sorted(matches, key=lambda item: (item.slug, str(item.version_id)))
        if await _binding_may_be_offered(run.session, tool, routine)
    ]
    if not offerable:
        return await decline(SKIP_SOURCE_HAS_TOOL)
    chosen = offerable[0]
    if await _another_routine_has_this_query(run, chosen, routine):
        return await decline(SKIP_ROUTINE_MATCH_AMBIGUOUS)
    session = run.session
    version = await session.get(GovernedToolVersion, chosen.version_id)
    tool = await session.get(GovernedTool, chosen.tool_id)
    project = await session.get(Project, tool.project_id) if tool is not None else None
    if version is None or tool is None or project is None:
        return await decline(SKIP_NO_PROJECT)
    body = GovernedToolVersionCreate(
        slug=tool.slug,
        name=version.name,
        description=version.description,
        datasource_id=version.datasource_id,
        semantic_model_version_id=version.semantic_model_version_id,
        sql_template=version.sql_template,
        parameters=[
            ToolParameterDefinition.model_validate(parameter)
            for parameter in version.parameter_schema or []
        ],
        allowed_roles=list(version.allowed_roles),
    )
    binding = _Proposal(run, CAPABILITY_PROCEDURE_TOOL, candidate, _ROUTINE_REF_TYPE, tool.slug)
    return await binding.draft_and_submit(
        project,
        datasource,
        body,
        {
            "source_kind": "ROUTINE",
            "routine_id": str(candidate.subject_id),
            # What the reviewer is deciding: not new SQL, a binding for a tool that exists.
            "binds_hand_written_version_id": str(version.id),
            "match": "STRUCTURAL_RESULT_QUERY",
        },
        source_routine=routine,
        related_id=tool.id,
        related_name=tool.slug,
    )


async def _propose_procedure_tool(
    run: TaskAgentRun, candidate: _Candidate, *, hand_written: _HandWrittenToolIndex
) -> TaskAgentItem:
    proposal = _Proposal(
        run,
        CAPABILITY_PROCEDURE_TOOL,
        candidate,
        _ROUTINE_REF_TYPE,
        tool_slug("routine", candidate.schema_name, candidate.object_name, candidate.signature),
    )
    located = await proposal.locate()
    if isinstance(located, TaskAgentItem):
        return located
    project, datasource = located
    try:
        routine, result_node, parse_result, parameters = await resolve_procedure_tool_source(
            run.session,
            organization_id=run.organization_id,
            datasource_id=datasource.id,
            routine_id=candidate.subject_id,
            dialect=datasource.dialect,
        )
    except (RoutineNotEligibleError, ProcedureNotEligibleError) as exc:
        return await proposal.decline(exc.code)
    except ProcedureToolBlueprintError:
        return await proposal.decline(SKIP_BLUEPRINT_REFUSED)
    # R11-FP14: is a hand-written tool in this source this routine's result query? Asked before
    # the slug and binding checks, because a routine that already has a generated tool can still
    # have a hand-written copy of its query that nothing holds -- and before the blueprint,
    # because a literal in the result query refuses *generation* but is exactly why a person
    # wrote the tool by hand; the structural key erases values, so it still matches.
    key = structural_query_key(
        result_node,
        dialect=datasource.dialect,
        parameter_names=[parameter.name for parameter in parameters],
    )
    if key is not None:
        matches = await hand_written.matching(datasource.id, key)
        if matches:
            return await _propose_binding(run, candidate, routine, datasource, matches)
    spoken_for = await proposal.spoken_for(project)
    if spoken_for is not None:
        return spoken_for
    try:
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
