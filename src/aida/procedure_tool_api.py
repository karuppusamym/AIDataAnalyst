"""N12: HTTP route for "tool generator C" (procedure -> governed tool).

A new, standalone router rather than an addition to `tool_api.py` itself --
that module is large, central, and a natural point of concurrent edit across
this Wave-2 pass; this route needs nothing from it beyond two already-shared
helpers, imported directly rather than duplicated so the two paths can never
silently diverge: `_load_project_and_datasource` (identity/tenancy
resolution) and `_persist_tool_version_draft` (the one shared draft-creation
tail every tool-blueprint generator -- hand-authored, SM-5's multi-table
join, N11's view-to-tool, and this one -- funnels through, so publication
still requires `submit_tool_for_review` and independent approval regardless
of which path created the draft; same maker-checker posture, INV-3/INV-10,
as everything else on this platform).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.edition_entitlements import evaluate_entitlement
from aida.events import record_audit
from aida.procedure_lineage_api import RoutineNotEligibleError
from aida.procedure_lineage_models import ProcedureToolGenerationRecord
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    ProcedureToolBlueprintError,
    build_procedure_tool_blueprint,
    resolve_procedure_tool_source,
)
from aida.schemas import ApiModel, GovernedToolVersionCreate, GovernedToolVersionRead
from aida.security import SecurityContext, require_roles
from aida.tool_api import _load_project_and_datasource, _persist_tool_version_draft

router = APIRouter(prefix="/v1", tags=["procedure-tool-blueprint"])


class ProcedureToolBlueprintRequest(ApiModel):
    """N12: request a deterministically-rendered procedure-to-tool draft.
    Mirrors N11's `ViewToolBlueprintRequest` fields exactly, replacing
    `table_id` with `routine_id` naming the `MetadataRoutine`."""

    slug: str
    name: str
    description: str
    datasource_id: UUID
    semantic_model_version_id: UUID | None = None
    routine_id: UUID
    allowed_roles: list[str]


@router.post(
    "/projects/{project_id}/tool-blueprints/from-procedure",
    response_model=GovernedToolVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_procedure_tool_blueprint(
    project_id: UUID,
    body: ProcedureToolBlueprintRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernedToolVersionRead:
    """N12: procedure -> tool ("tool generator C"). Eligible ONLY when N3's
    parse *proves* `routine_id` read-only -- fully parsed (zero UNPARSED
    statements: a construct this parser gave up on is never mistaken for
    "no write found"), zero INSERT/UPDATE/DELETE/MERGE/CREATE statements,
    and exactly one terminal result SELECT. Refuses (422) naming the exact
    reason otherwise. See `procedure_tool_blueprint.py`'s module docstring
    for why the generated SQL is the procedure's own reconstructed result
    statement (not a fresh query, unlike N11) and why a literal anywhere in
    it also refuses generation outright.
    """
    # PG-5: generative tool-blueprint authoring is "Studio (semantic + tool
    # authoring)" -- Enterprise floor, same gate N11/SM-5 apply.
    entitlement = evaluate_entitlement(
        organization_edition=settings.edition,
        capability="studio_semantic_and_tool_authoring",
    )
    if not entitlement.allowed:
        record_audit(
            session, context, action="tool_blueprint.entitlement_denied",
            resource_type="governed_tool_version", resource_id=None, outcome="DENIED",
            correlation_id=get_correlation_id(), details=entitlement.snapshot(),
        )
        await session.commit()
        raise HTTPException(status_code=403, detail=entitlement.reason_code)

    project, datasource = await _load_project_and_datasource(
        session, context, project_id, body.datasource_id
    )

    try:
        resolved = await resolve_procedure_tool_source(
            session,
            organization_id=project.organization_id,
            datasource_id=datasource.id,
            routine_id=body.routine_id,
            dialect=datasource.dialect,
        )
        routine, result_node, parse_result, routine_parameters = resolved
        blueprint = build_procedure_tool_blueprint(
            result_node,
            routine_parameters,
            dialect=datasource.dialect,
            statement_count=parse_result.statement_count,
            sql_hash=parse_result.sql_hash,
        )
    except RoutineNotEligibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ProcedureNotEligibleError, ProcedureToolBlueprintError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    create_body = GovernedToolVersionCreate(
        slug=body.slug,
        name=body.name,
        description=body.description,
        datasource_id=body.datasource_id,
        semantic_model_version_id=body.semantic_model_version_id,
        sql_template=blueprint.sql_template,
        parameters=list(blueprint.parameters),
        allowed_roles=body.allowed_roles,
    )
    tool_version = await _persist_tool_version_draft(
        project, datasource, create_body, context=context, session=session, settings=settings
    )

    session.add(
        ProcedureToolGenerationRecord(
            organization_id=project.organization_id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            tool_version_id=tool_version.id,
            sql_hash=blueprint.sql_hash,
            statement_count=blueprint.statement_count,
            created_by=context.principal_id,
        )
    )
    await session.commit()

    return tool_version
