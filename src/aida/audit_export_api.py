"""`GET /v1/organizations/{organization_id}/audit-events/export.jsonl` -- the
audit ledger for one organization as a downloadable, hash-identified artifact.

**Why this exists as its own surface.** `operational_api.list_audit_events`
already returns audit events, but it is a *browse*: capped at 500 rows a page,
shaped for a screen, and authorized by a bare role-string comparison
(`require_roles`) that never reaches the platform's authorization decision. An
auditor who needs the ledger for a period -- the actual regulatory ask -- had
no surface at all, and the nearest one was ungated. Paging a browse endpoint
until it runs out is not an export: nothing identifies the resulting bytes,
nothing records that the extraction happened, and nothing decides whether the
caller was entitled to it beyond the role string on their token.

Three properties, each of which was a decision:

**Roles are necessary, never sufficient.** `require_roles` still guards the
route, because a caller with no audit-reading role should be refused before any
query runs. But the decision that matters is `authorization_gate.gate` with
action `EXPORT` -- the same primitive `query_gateway` and the catalog reads use,
and the reason `EXPORT` is in `policy_engine.ACTIONS` at all. A deployment can
therefore write a policy that lets `Auditor` browse but not extract, which a
role comparison cannot express.

**The export is itself an audited event.** Bulk extraction of the audit ledger
is exactly the act an audit ledger exists to record. `record_audit` is called
with the filters and the row count, so the export leaves a trace in the thing
it exported -- and, because `record_audit` is also the SIEM funnel (OB-2), that
trace leaves the box.

**Truncation is visible or it is a lie.** An export that silently stops at a
row cap is worse than one that refuses, because the recipient cannot tell a
quiet period from a truncated one. The row cap is explicit, and
`X-Export-Truncated` says which happened in a header a script reads -- the same
idiom `model_export_api` established.

Format is JSON Lines rather than a workbook: the ledger is one flat record
type, the volume is unbounded in a way a spreadsheet is not, and one JSON
object per line is what a SIEM or a `jq` pipeline ingests without a parser.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import AuditEvent, Organization
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["audit-export"])

_JSONL_MEDIA_TYPE = "application/x-ndjson"

#: The most rows one export will return. Large enough that an ordinary period
#: is whole, bounded because this handler materializes the result to hash it.
#: A caller who hits it narrows `since`/`until` and exports again -- which the
#: `X-Export-Truncated` header tells them to do.
MAX_EXPORT_ROWS = 50_000

#: The population allowed to reach the gate at all. Deliberately the same set
#: `operational_api.list_audit_events` admits for browsing: an export is a
#: delivery mode for records these roles can already read one page at a time,
#: not a new disclosure. Narrowing the *export* specifically is a policy
#: decision, and the gate below is where a deployment expresses it -- which is
#: the point of routing this through `EXPORT` rather than inventing a role.
_AUDIT_EXPORT_ROLES = (
    "PlatformAdmin",
    "OrganizationAdmin",
    "Auditor",
    "Operations",
)


def _row_to_json(row: AuditEvent) -> dict[str, Any]:
    """One ledger record, with every column the envelope also covers.

    Field names match `AuditEventRead` so an export and a browse describe the
    same record the same way; a consumer that learned the shape from the API
    does not need a second mapping for the file.
    """
    return {
        "id": row.id,
        "organization_id": str(row.organization_id) if row.organization_id else None,
        "principal_id": row.principal_id,
        "principal_type": row.principal_type,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "outcome": row.outcome,
        "correlation_id": row.correlation_id,
        "source_ip": row.source_ip,
        "details": row.details,
        "occurred_at": row.occurred_at.isoformat(),
    }


@router.get("/organizations/{organization_id}/audit-events/export.jsonl")
async def export_audit_events(
    organization_id: UUID,
    action: str | None = Query(default=None, max_length=150),
    resource_type: str | None = Query(default=None, max_length=100),
    correlation_id: str | None = Query(default=None, max_length=100),
    since: datetime | None = None,
    until: datetime | None = None,
    workspace_id: UUID | None = Query(
        default=None,
        description=(
            "Workspace to authorize this export against. Optional while "
            "`unresolved_workspace_posture` is SHADOW; required once it flips to DENY."
        ),
    ),
    context: SecurityContext = Depends(require_roles(*_AUDIT_EXPORT_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    enforce_organization(context, organization_id)
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")

    # Same timezone contract as the browse endpoint: a naive bound silently
    # means something different on every deployment, so it is refused rather
    # than interpreted.
    if since is not None and since.tzinfo is None:
        raise HTTPException(status_code=422, detail="since must include a timezone")
    if until is not None and until.tzinfo is None:
        raise HTTPException(status_code=422, detail="until must include a timezone")
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=422, detail="since cannot be later than until")

    try:
        # The decision, as opposed to the role check on the route. `EXPORT` is
        # a distinct verb in `policy_engine.ACTIONS` precisely so that
        # "may read the audit log" and "may extract the audit log" can be
        # different answers.
        await gate(
            session,
            context,
            settings=settings,
            action="EXPORT",
            resource_type="audit_event",
            resource_id=str(organization_id),
            workspace_id=workspace_id,
        )
    except AuthorizationDenied as exc:
        # Bare reason code, no policy text and no resource detail (INV-6).
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc

    filters: list[ColumnElement[bool]] = [AuditEvent.organization_id == organization_id]
    if action:
        filters.append(AuditEvent.action == action)
    if resource_type:
        filters.append(AuditEvent.resource_type == resource_type)
    if correlation_id:
        filters.append(AuditEvent.correlation_id == correlation_id)
    if since:
        filters.append(AuditEvent.occurred_at >= since)
    if until:
        filters.append(AuditEvent.occurred_at <= until)

    # One row past the cap, so truncation is detected rather than inferred
    # from a full page -- a result of exactly MAX_EXPORT_ROWS is otherwise
    # indistinguishable from a period that happens to hold that many.
    rows = list(
        await session.scalars(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
            .limit(MAX_EXPORT_ROWS + 1)
        )
    )
    truncated = len(rows) > MAX_EXPORT_ROWS
    if truncated:
        rows = rows[:MAX_EXPORT_ROWS]

    content = b"".join(
        json.dumps(_row_to_json(row), sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for row in rows
    )
    digest = hashlib.sha256(content).hexdigest()
    generated_at = datetime.now(UTC)

    # The export is itself an auditable act, recorded before the bytes leave.
    # Value-free in the sense that matters: the filters and the count, never
    # the exported records.
    record_audit(
        session,
        context,
        action="AUDIT_EVENTS_EXPORTED",
        resource_type="audit_event",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "row_count": len(rows),
            "truncated": truncated,
            "artifact_sha256": digest,
            "filter_action": action,
            "filter_resource_type": resource_type,
            "filter_correlation_id": correlation_id,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
    )
    await session.commit()

    filename = (
        f"audit-events-{organization_id}-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Artifact-SHA256": digest,
        "X-Export-Row-Count": str(len(rows)),
        "X-Export-Truncated": "true" if truncated else "false",
        "X-Export-Row-Limit": str(MAX_EXPORT_ROWS),
    }
    return Response(content=content, media_type=_JSONL_MEDIA_TYPE, headers=headers)
