"""Self-service entitlement reporting (OB-7).

Answers "what am I entitled to see" (or, for an elevated role, "what is
principal X entitled to see") from real persisted grant data rather than a
hand-maintained spreadsheet -- the same "generated from runtime evidence, not
authored by hand" bar module 20 SS7 sets for OB-5's compliance packs.

Two real, already-wired-into-production tables carry the entitlement:

* `WorkspaceMembership` -- a principal's role inside one workspace
  (`workspace_service.membership_roles` is the same table `authorize` reads
  on the real query-execution path).
* `SourceBinding` -- a workspace's scoped, expiring permission to reach one
  datasource, carrying the classifications, schema scope and masking profile
  that binding permits (`workspace_service.active_binding`, same real path).

Both are queried directly here rather than through `workspace_service`'s
per-workspace helpers, because a report needs every live membership and
binding for one principal across *all* their workspaces in one pass, not a
single (workspace, datasource) pair.

`abac.evaluate` (PG-8's policy engine) is layered on top as a **self-service
only** overlay: it shows, for each classification a principal's bindings
nominally permit, whether an ACTIVE `AbacPolicyRecord` policy would actually
grant it given their real, live role attributes. It cannot run for a
report pulled *about* another principal -- this platform persists no role
assignment for anyone but the caller making the current request (roles
arrive per-request from the identity provider and are never stored; see
`WorkspaceAccessRule`'s module docstring for why). An on-behalf-of report is
therefore grant data only, honestly labelled as such in `abac_note`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.abac import AbacPolicy, evaluate
from aida.models import (
    AbacPolicyRecord,
    AccessReviewReportRecord,
    DataSource,
    LineOfBusiness,
    SourceBinding,
    Workspace,
    WorkspaceMembership,
)
from aida.timeutil import is_expired

ABAC_SELF_SERVICE_ONLY_NOTE = (
    "ABAC policy overlay is evaluated for self-service reports only: this "
    "platform persists no role assignment for a principal other than the one "
    "making the current request (roles arrive per-request from the identity "
    "provider and are never stored). This report reflects only the persisted "
    "workspace-membership and source-binding grants below."
)


@dataclass(frozen=True, slots=True)
class WorkspaceEntitlement:
    workspace_id: UUID
    workspace_name: str
    workspace_slug: str
    role: str
    granted_by: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SourceEntitlement:
    workspace_id: UUID
    datasource_id: UUID
    datasource_name: str
    line_of_business_code: str | None
    line_of_business_name: str | None
    schema_scope: list[str]
    permitted_classifications: list[str]
    masking_profile: str
    purpose: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClassificationDecision:
    classification: str
    decision: str
    reasons: list[str]
    contributing_policy_ids: list[str]


@dataclass(frozen=True, slots=True)
class EntitlementReport:
    organization_id: UUID
    subject_principal_id: str
    subject_principal_type: str
    requested_by: str
    is_self_service: bool
    generated_at: datetime
    workspace_memberships: list[WorkspaceEntitlement] = field(default_factory=list)
    source_entitlements: list[SourceEntitlement] = field(default_factory=list)
    abac_classification_decisions: list[ClassificationDecision] = field(default_factory=list)
    abac_note: str = ""
    checksum: str = ""


def _compute_checksum(payload: dict[str, Any]) -> str:
    """Deterministic checksum: same grants + same policies => same checksum."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _load_active_policies(session: AsyncSession, organization_id: UUID) -> list[AbacPolicy]:
    rows = (
        await session.scalars(
            select(AbacPolicyRecord)
            .where(
                AbacPolicyRecord.organization_id == organization_id,
                AbacPolicyRecord.status == "ACTIVE",
            )
            .order_by(AbacPolicyRecord.priority)
        )
    ).all()
    return [
        AbacPolicy(
            id=str(row.id),
            policy_key=row.policy_key,
            version=row.version,
            name=row.name,
            effect="DENY" if row.effect == "DENY" else "PERMIT",
            subject_conditions=row.subject_conditions,
            resource_conditions=row.resource_conditions,
            environment_conditions=row.environment_conditions,
            priority=row.priority,
        )
        for row in rows
    ]


async def build_entitlement_report(
    session: AsyncSession,
    *,
    organization_id: UUID,
    subject_principal_id: str,
    subject_principal_type: str,
    requested_by: str,
    is_self_service: bool,
    requester_roles: frozenset[str] | None = None,
    now: datetime | None = None,
) -> EntitlementReport:
    moment = now or datetime.now(UTC)

    membership_rows = (
        await session.execute(
            select(WorkspaceMembership, Workspace)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                WorkspaceMembership.organization_id == organization_id,
                WorkspaceMembership.principal_id == subject_principal_id,
                WorkspaceMembership.status == "ACTIVE",
            )
            .order_by(Workspace.name)
        )
    ).all()

    workspace_memberships = [
        WorkspaceEntitlement(
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_slug=workspace.slug,
            role=membership.role,
            granted_by=membership.granted_by,
            expires_at=membership.expires_at,
        )
        for membership, workspace in membership_rows
        if not is_expired(membership.expires_at, moment)
    ]
    live_workspace_ids = [m.workspace_id for m in workspace_memberships]

    source_entitlements: list[SourceEntitlement] = []
    if live_workspace_ids:
        binding_rows = (
            await session.execute(
                select(SourceBinding, DataSource, LineOfBusiness)
                .join(DataSource, DataSource.id == SourceBinding.datasource_id)
                .outerjoin(
                    LineOfBusiness, LineOfBusiness.id == DataSource.line_of_business_id
                )
                .where(
                    SourceBinding.organization_id == organization_id,
                    SourceBinding.workspace_id.in_(live_workspace_ids),
                    SourceBinding.status == "ACTIVE",
                )
                .order_by(DataSource.name)
            )
        ).all()
        source_entitlements = [
            SourceEntitlement(
                workspace_id=binding.workspace_id,
                datasource_id=datasource.id,
                datasource_name=datasource.name,
                line_of_business_code=lob.code if lob is not None else None,
                line_of_business_name=lob.name if lob is not None else None,
                schema_scope=list(binding.schema_scope or []),
                permitted_classifications=list(binding.permitted_classifications or []),
                masking_profile=binding.masking_profile,
                purpose=binding.purpose,
                expires_at=binding.expires_at,
            )
            for binding, datasource, lob in binding_rows
            if not is_expired(binding.expires_at, moment)
        ]

    abac_decisions: list[ClassificationDecision] = []
    if is_self_service and requester_roles is not None:
        classifications = sorted(
            {
                c
                for entitlement in source_entitlements
                for c in entitlement.permitted_classifications
            }
        )
        if classifications:
            policies = await _load_active_policies(session, organization_id)
            subject_attrs: dict[str, Any] = {
                "role": sorted(requester_roles),
                "principal_type": subject_principal_type,
            }
            for classification in classifications:
                result = evaluate(
                    subject_attrs,
                    {"classification": classification, "resource_type": "metadata_column"},
                    {},
                    policies,
                )
                abac_decisions.append(
                    ClassificationDecision(
                        classification=classification,
                        decision=result.decision,
                        reasons=result.reasons,
                        contributing_policy_ids=result.contributing_policies,
                    )
                )
            abac_note = (
                f"Evaluated {len(policies)} active ABAC polic"
                f"{'y' if len(policies) == 1 else 'ies'} against the caller's live role "
                f"attributes for the {len(classifications)} distinct classification"
                f"{'' if len(classifications) == 1 else 's'} named in this principal's "
                "active source bindings."
            )
        else:
            abac_note = "No active source bindings to evaluate classifications against."
    else:
        abac_note = ABAC_SELF_SERVICE_ONLY_NOTE

    payload = {
        "organization_id": str(organization_id),
        "subject_principal_id": subject_principal_id,
        "subject_principal_type": subject_principal_type,
        "is_self_service": is_self_service,
        "workspace_memberships": [asdict(m) for m in workspace_memberships],
        "source_entitlements": [asdict(s) for s in source_entitlements],
        "abac_classification_decisions": [asdict(d) for d in abac_decisions],
        "abac_note": abac_note,
    }
    checksum = _compute_checksum(payload)

    return EntitlementReport(
        organization_id=organization_id,
        subject_principal_id=subject_principal_id,
        subject_principal_type=subject_principal_type,
        requested_by=requested_by,
        is_self_service=is_self_service,
        generated_at=moment,
        workspace_memberships=workspace_memberships,
        source_entitlements=source_entitlements,
        abac_classification_decisions=abac_decisions,
        abac_note=abac_note,
        checksum=checksum,
    )


def _json_safe(value: Any) -> Any:
    """Round-trip through `json` with `default=str` so a `UUID` or `datetime`
    embedded in a dataclass (both common in these entitlement rows) survives
    the JSON column's own, stricter default encoder rather than raising at
    insert time.
    """
    return json.loads(json.dumps(value, default=str))


def persist_entitlement_report(
    session: AsyncSession,
    *,
    organization_id: UUID,
    report: EntitlementReport,
) -> AccessReviewReportRecord:
    """WORM-archive a generated entitlement report (append-only, never updated)."""
    record = AccessReviewReportRecord(
        organization_id=organization_id,
        subject_principal_id=report.subject_principal_id,
        subject_principal_type=report.subject_principal_type,
        is_self_service=report.is_self_service,
        requested_by=report.requested_by,
        entitlements=_json_safe(
            {
                "workspace_memberships": [asdict(m) for m in report.workspace_memberships],
                "source_entitlements": [asdict(s) for s in report.source_entitlements],
                "abac_classification_decisions": [
                    asdict(d) for d in report.abac_classification_decisions
                ],
                "abac_note": report.abac_note,
            }
        ),
        checksum=report.checksum,
        generated_at=report.generated_at,
    )
    session.add(record)
    return record
