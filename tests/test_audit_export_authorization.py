"""R11-B9: audit export exists, and the platform's authorization decides it.

Before this row there was no audit *export* surface at all. The nearest thing,
`operational_api.list_audit_events`, is a 500-row browse whose only check is
`require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")`
-- a bare comparison against the role strings on the caller's token, which never
reaches `authorization_gate.gate` and therefore cannot be varied by policy at
all. "Authorized export", read strictly, was unmet twice over.

The file is organized around the three ways this could be fake:

**The gate could be absent.** A static reachability scan (the same
`tests.support.app_surface.reaches_call` machinery
`test_inv4_authorization_wiring.py` uses) asserts the handler reaches a
decision. Deleting the `gate` call fails here even if every behavioural test
below were somehow still green.

**The gate could be decorative.** `test_a_deny_policy_stops_the_export_and_not_the_browse`
is the load-bearing one. The same caller, with the same roles, in the same
workspace, is allowed `READ_METADATA` and refused `EXPORT`, because a DENY
policy names the action. No arrangement of role strings can express that, so a
green result here is only obtainable by actually consulting the policy engine.

**The test could bypass the surface.** Every behavioural case calls the real
handler with a real session, real `Settings`, real `Workspace`/`AccessPolicy`
rows and the real gate. Nothing is monkeypatched except the SQLite id quirk.

One honest limitation, stated rather than hidden: with the shipped default
`unresolved_workspace_posture="SHADOW"`, a caller who names no workspace is
*not* denied -- the gate records that it could not decide and proceeds. That is
the platform-wide ADR-0018 rollout posture, not something specific to this
surface, and `test_an_unnamed_workspace_proceeds_under_the_shipped_posture`
pins it so the behaviour is visible rather than assumed. Flipping the setting to
DENY is what makes this surface refuse an unscoped export, and that flip is a
deployment decision this row does not make.
"""

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.audit_export_api import export_audit_events
from aida.db import Base
from aida.models import (
    AccessPolicy,
    AuditEvent,
    Organization,
    Workspace,
    WorkspaceAccessRule,
)
from aida.security import require_roles
from aida.security_types import SecurityContext
from aida.workspace_access import ENFORCE
from atlas.platform.config import Settings
from tests.support.app_surface import reaches_call

_GATE_CALLS = frozenset({"gate", "authorize_enforced", "authorize"})

# Same sqlite `AuditEvent.id` workaround `tests/test_detokenization_api.py` and
# `tests/test_token_revocation.py` use: the column is a server-side identity on
# PostgreSQL and sqlite will not fill it in.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


def _settings(posture: str = "SHADOW") -> Settings:
    return Settings(_env_file=None, unresolved_workspace_posture=posture)


def _context(org: Organization, *, roles: frozenset[str] | None = None) -> SecurityContext:
    return SecurityContext(
        principal_id="auditor-1",
        principal_type="USER",
        organization_id=org.id,
        roles=roles if roles is not None else frozenset({"Auditor"}),
    )


async def _seed_organization(session: AsyncSession) -> Organization:
    org = Organization(id=uuid4(), name="Test Bank", slug=f"test-bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    return org


async def _seed_audit_events(session: AsyncSession, org: Organization, count: int) -> None:
    for i in range(count):
        session.add(
            AuditEvent(
                organization_id=org.id,
                principal_id="analyst-1",
                principal_type="USER",
                action="data_access",
                resource_type="table",
                resource_id=f"tbl-{i}",
                outcome="SUCCESS",
                correlation_id=f"corr-{i}",
                details={"rows": i},
                occurred_at=datetime(2026, 9, 12, 10, i, tzinfo=UTC),
            )
        )
    await session.commit()


async def _enforcing_workspace(
    session: AsyncSession, org: Organization, *, grant_role: str | None = None
) -> Workspace:
    """A workspace that actually enforces, optionally granting the caller membership.

    `workspace_owner` because it is the only workspace role that permits
    `EXPORT` (see `workspace_service._ROLE_ACTIONS`). Mapping an IdP `Auditor`
    onto it is how a deployment grants extraction today, and the comment there
    records why a narrower grant is not expressible yet.
    """
    workspace = Workspace(
        organization_id=org.id,
        name="Audit",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="audit",
        authorization_mode=ENFORCE,
    )
    session.add(workspace)
    await session.flush()
    if grant_role is not None:
        session.add(
            WorkspaceAccessRule(
                organization_id=org.id,
                code=f"rule-{uuid4().hex[:6]}",
                subject_role=grant_role,
                workspace_role="workspace_owner",
                created_by="test",
            )
        )
    # The attribute-based layer decides after the role ceiling, and its default
    # is to refuse: without an ALLOW policy every action answers
    # NO_APPLICABLE_ALLOW_POLICY. This is the parity policy the ADR-0018
    # migration seeds, mirrored from `test_inv4_authorization_wiring._org`.
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code=f"rbac-parity-{uuid4().hex[:6]}",
            name="parity",
            effect="ALLOW",
            subject_match={"roles": ["Auditor"]},
            action_match=[],
            created_by="test",
        )
    )
    await session.commit()
    return workspace


async def _export(
    session: AsyncSession,
    org: Organization,
    *,
    context: SecurityContext | None = None,
    workspace_id: object = None,
    settings: Settings | None = None,
    **kwargs: object,
) -> object:
    return await export_audit_events(
        organization_id=org.id,
        action=kwargs.get("action"),  # type: ignore[arg-type]
        resource_type=kwargs.get("resource_type"),  # type: ignore[arg-type]
        correlation_id=kwargs.get("correlation_id"),  # type: ignore[arg-type]
        since=kwargs.get("since"),  # type: ignore[arg-type]
        until=kwargs.get("until"),  # type: ignore[arg-type]
        workspace_id=workspace_id,  # type: ignore[arg-type]
        context=context if context is not None else _context(org),
        session=session,
        settings=settings if settings is not None else _settings(),
    )


# --- static: the decision is reachable from the handler ----------------------


def test_the_export_handler_reaches_the_gate() -> None:
    """Removing the gate call must fail a test, not merely widen access silently."""
    assert reaches_call("aida.audit_export_api", "export_audit_events", _GATE_CALLS)


def test_the_browse_endpoint_is_still_only_role_checked() -> None:
    """The discriminator, and a standing description of what was found.

    `list_audit_events` is role-checked and ungated. This test exists so the
    scan above is known to be capable of answering False, and so that gating
    the browse endpoint later is a deliberate act that updates this file rather
    than a change nothing notices.
    """
    assert not reaches_call("aida.operational_api", "list_audit_events", _GATE_CALLS)


# --- the gate actually decides -----------------------------------------------


async def test_an_enforcing_workspace_refuses_the_export(session: AsyncSession) -> None:
    """No membership, workspace enforcing: the export is refused with a reason code."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)
    workspace = await _enforcing_workspace(session, org)

    with pytest.raises(HTTPException) as refusal:
        await _export(session, org, workspace_id=workspace.id)

    assert refusal.value.status_code == 403
    assert refusal.value.detail == "NO_WORKSPACE_MEMBERSHIP"


async def test_an_enforcing_workspace_allows_a_member(session: AsyncSession) -> None:
    """The other half: a gate that denied everything would pass the test above."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)
    workspace = await _enforcing_workspace(session, org, grant_role="Auditor")

    response = await _export(session, org, workspace_id=workspace.id)

    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response.headers["X-Export-Row-Count"] == "3"  # type: ignore[attr-defined]


async def test_a_deny_policy_stops_the_export_and_not_the_browse(
    session: AsyncSession,
) -> None:
    """The property a role string cannot express, which is why the gate is here.

    One caller, one workspace, one set of roles. `READ_METADATA` is allowed and
    `EXPORT` is refused, because the policy names the action. A surface guarded
    only by `require_roles` has no way to reach this outcome: the role set is
    identical in both cases.
    """
    from aida.authorization_gate import AuthorizationDenied, gate

    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)
    workspace = await _enforcing_workspace(session, org, grant_role="Auditor")
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="no-bulk-audit-extraction",
            name="Auditors may read the ledger but not extract it",
            effect="DENY",
            priority=10,
            subject_match={"roles": ["Auditor"]},
            action_match=["EXPORT"],
            created_by="test",
        )
    )
    await session.commit()

    # Reading is still permitted...
    outcome = await gate(
        session,
        _context(org),
        settings=_settings(),
        action="READ_METADATA",
        resource_type="audit_event",
        resource_id=str(org.id),
        workspace_id=workspace.id,
    )
    assert outcome.decided is True

    # ...and the same caller cannot export.
    with pytest.raises(AuthorizationDenied):
        await gate(
            session,
            _context(org),
            settings=_settings(),
            action="EXPORT",
            resource_type="audit_event",
            resource_id=str(org.id),
            workspace_id=workspace.id,
        )

    # And that refusal reaches the surface as a 403, not an exception.
    with pytest.raises(HTTPException) as refusal:
        await _export(session, org, workspace_id=workspace.id)
    assert refusal.value.status_code == 403


async def test_an_unnamed_workspace_proceeds_under_the_shipped_posture(
    session: AsyncSession,
) -> None:
    """The honest statement of today's default, pinned so it cannot drift silently.

    `unresolved_workspace_posture` ships as SHADOW, so a caller who names no
    workspace is logged, not refused. This is the platform's rollout posture,
    identical on every gated surface.
    """
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)

    response = await _export(session, org, settings=_settings("SHADOW"))

    assert response.status_code == 200  # type: ignore[attr-defined]


async def test_the_deny_posture_refuses_an_unscoped_export(session: AsyncSession) -> None:
    """And the one setting that makes an unscoped export a refusal, with no code change."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)

    with pytest.raises(HTTPException) as refusal:
        await _export(session, org, settings=_settings("DENY"))

    assert refusal.value.status_code == 403
    assert refusal.value.detail == "WORKSPACE_NOT_SPECIFIED"


# --- the role guard, which is necessary and not sufficient -------------------


async def test_a_caller_without_an_audit_role_never_reaches_the_gate() -> None:
    """`require_roles` refuses first, so an unprivileged caller runs no query."""
    dependency = require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    context = SecurityContext(
        principal_id="analyst-1",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Analyst"}),
    )

    with pytest.raises(HTTPException) as refusal:
        await dependency(context)
    assert refusal.value.status_code == 403


async def test_a_cross_organization_export_is_refused(session: AsyncSession) -> None:
    """Tenancy is checked before anything is read (INV-5)."""
    org = await _seed_organization(session)
    other = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)

    with pytest.raises(HTTPException) as refusal:
        await _export(session, org, context=_context(other))

    assert refusal.value.status_code == 403
    assert refusal.value.detail == "cross-organization access denied"


# --- what the artifact says about itself -------------------------------------


async def test_the_export_is_itself_recorded_in_the_ledger(session: AsyncSession) -> None:
    """Bulk extraction of the audit log is exactly what an audit log is for."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=4)

    response = await _export(session, org)

    recorded = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "AUDIT_EVENTS_EXPORTED")
        )
    ).all()
    assert len(recorded) == 1
    assert recorded[0].organization_id == org.id
    assert recorded[0].principal_id == "auditor-1"
    assert recorded[0].outcome == "SUCCESS"
    assert recorded[0].details["row_count"] == 4
    assert recorded[0].details["truncated"] is False
    # The trace names the bytes, so an export produced later can be told apart
    # from the one attached to a ticket.
    assert recorded[0].details["artifact_sha256"] == response.headers["X-Artifact-SHA256"]  # type: ignore[attr-defined]


async def test_the_artifact_hash_covers_the_bytes_returned(session: AsyncSession) -> None:
    import hashlib

    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)

    response = await _export(session, org)

    body = response.body  # type: ignore[attr-defined]
    assert hashlib.sha256(body).hexdigest() == response.headers["X-Artifact-SHA256"]  # type: ignore[attr-defined]
    assert len(body.decode("utf-8").strip().splitlines()) == 3


async def test_truncation_is_reported_rather_than_silent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated export that looks complete is worse than one that refuses."""
    monkeypatch.setattr("aida.audit_export_api.MAX_EXPORT_ROWS", 2)
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=5)

    response = await _export(session, org)

    assert response.headers["X-Export-Truncated"] == "true"  # type: ignore[attr-defined]
    assert response.headers["X-Export-Row-Count"] == "2"  # type: ignore[attr-defined]
    assert len(response.body.decode("utf-8").strip().splitlines()) == 2  # type: ignore[attr-defined]


async def test_a_naive_timestamp_bound_is_refused(session: AsyncSession) -> None:
    """A naive bound means something different on every deployment."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)

    with pytest.raises(HTTPException) as refusal:
        await _export(session, org, since=datetime(2026, 9, 12, 10, 0))

    assert refusal.value.status_code == 422


async def test_the_filters_narrow_the_artifact(session: AsyncSession) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=4)

    response = await _export(session, org, correlation_id="corr-2")

    assert response.headers["X-Export-Row-Count"] == "1"  # type: ignore[attr-defined]
