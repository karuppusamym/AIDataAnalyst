"""OB-7: self-service entitlement reporting.

`aida.access_review.build_entitlement_report` answers "what am I entitled to
see" from real persisted `WorkspaceMembership` and `SourceBinding` rows --
the same tables `workspace_service.authorize` reads on the live
query-execution path -- plus a live access-policy overlay
(`aida.policy_engine.evaluate`, PG-8's engine) for the self-service case.
These tests seed a real in-memory
database with real grants and assert the report reflects them, and exercise
the real `access_review_api` endpoint functions end to end, including
persistence of the append-only `AccessReviewReportRecord`.
"""

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.access_review import ABAC_SELF_SERVICE_ONLY_NOTE, build_entitlement_report
from aida.access_review_api import (
    generate_entitlement_report,
    get_entitlement_report,
    list_entitlement_reports,
)
from aida.db import Base
from aida.models import (
    AccessPolicy,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
    SourceBinding,
    Workspace,
    WorkspaceMembership,
)
from aida.schemas import GenerateEntitlementReportRequest
from aida.security_types import SecurityContext

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

# `AuditEvent.id` is a BigInteger PK that SQLite's autoincrement does not
# reliably assign for every insert path -- mirrors the same fixture
# `tests/test_worm_archive_wiring.py` uses for the same reason, since the
# self-service generate endpoint records a real audit event on every call.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _organization(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _datasource(session: AsyncSession, org: Organization) -> DataSource:
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:4]}")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Ungoverned", code="UNGOVERNED"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()
    return datasource


async def _workspace_with_membership(
    session: AsyncSession,
    org: Organization,
    *,
    principal_id: str,
    role: str = "analyst",
    expires_at: datetime | None = None,
) -> Workspace:
    workspace = Workspace(
        organization_id=org.id, name="Risk Analytics", slug=f"risk-{uuid4().hex[:6]}", purpose="p"
    )
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMembership(
            organization_id=org.id,
            workspace_id=workspace.id,
            principal_id=principal_id,
            role=role,
            granted_by="owner-1",
            expires_at=expires_at,
        )
    )
    await session.flush()
    return workspace


async def _binding(
    session: AsyncSession,
    org: Organization,
    workspace: Workspace,
    datasource: DataSource,
    *,
    permitted_classifications: list[str],
    status: str = "ACTIVE",
    expires_at: datetime | None = None,
) -> SourceBinding:
    binding = SourceBinding(
        organization_id=org.id,
        workspace_id=workspace.id,
        datasource_id=datasource.id,
        permitted_classifications=permitted_classifications,
        masking_profile="DEFAULT",
        purpose="analysis",
        status=status,
        requested_by="analyst-1",
        approved_by="owner-1",
        expires_at=expires_at,
    )
    session.add(binding)
    await session.flush()
    return binding


def _context(
    org: Organization,
    principal_id: str = "analyst-1",
    *,
    roles: frozenset[str] = frozenset({"Analyst"}),
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id, principal_type="USER", organization_id=org.id, roles=roles
    )


# ---------------------------------------------------------------------------
# Report builder: real grant data
# ---------------------------------------------------------------------------


async def test_report_reflects_real_workspace_and_binding_grants(session: AsyncSession) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    await _binding(
        session, org, workspace, datasource, permitted_classifications=["INTERNAL", "PII"]
    )

    report = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="analyst-1",
        is_self_service=True,
        requester_roles=frozenset({"Analyst"}),
        now=_NOW,
    )

    assert len(report.workspace_memberships) == 1
    membership = report.workspace_memberships[0]
    assert membership.workspace_id == workspace.id
    assert membership.role == "analyst"

    assert len(report.source_entitlements) == 1
    entitlement = report.source_entitlements[0]
    assert entitlement.datasource_id == datasource.id
    assert entitlement.permitted_classifications == ["INTERNAL", "PII"]
    assert entitlement.line_of_business_code is not None


async def test_report_excludes_expired_membership_and_expired_binding(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    live_workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    expired_workspace = await _workspace_with_membership(
        session, org, principal_id="analyst-1", expires_at=_NOW - timedelta(days=1)
    )
    await _binding(
        session, org, live_workspace, datasource,
        permitted_classifications=["INTERNAL"], expires_at=_NOW - timedelta(days=1),
    )

    report = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="analyst-1",
        is_self_service=True,
        requester_roles=frozenset({"Analyst"}),
        now=_NOW,
    )

    # Only the live membership is reported; the expired one is not.
    assert {m.workspace_id for m in report.workspace_memberships} == {live_workspace.id}
    assert expired_workspace.id not in {m.workspace_id for m in report.workspace_memberships}
    # The one binding that exists is expired, so no source entitlement.
    assert report.source_entitlements == []


async def test_report_is_scoped_to_the_subject_principal_not_the_caller(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="someone-else")
    await _binding(session, org, workspace, datasource, permitted_classifications=["INTERNAL"])

    # A membership for "analyst-1" must not leak into a report for "someone-else"
    # and vice versa.
    report = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="analyst-1",
        is_self_service=True,
        requester_roles=frozenset({"Analyst"}),
        now=_NOW,
    )
    assert report.workspace_memberships == []
    assert report.source_entitlements == []


# ---------------------------------------------------------------------------
# Policy overlay: self-service only
# ---------------------------------------------------------------------------


async def test_self_service_report_evaluates_policy_for_bound_classifications(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    await _binding(
        session, org, workspace, datasource, permitted_classifications=["INTERNAL", "RESTRICTED"]
    )
    session.add_all(
        [
            AccessPolicy(
                organization_id=org.id,
                code="allow-internal",
                version=1,
                name="Analysts may see INTERNAL",
                description="d",
                effect="ALLOW",
                subject_match={"roles": ["Analyst"]},
                resource_match={"classifications": ["INTERNAL"]},
                action_match=["READ_DATA"],
                priority=100,
                status="ACTIVE",
                created_by="steward-1",
            ),
            AccessPolicy(
                organization_id=org.id,
                code="deny-restricted",
                version=1,
                name="Analysts may not see RESTRICTED",
                description="d",
                effect="DENY",
                subject_match={"roles": ["Analyst"]},
                resource_match={"classifications": ["RESTRICTED"]},
                action_match=["READ_DATA"],
                priority=10,
                status="ACTIVE",
                created_by="steward-1",
            ),
            # Inactive policy: must be ignored.
            AccessPolicy(
                organization_id=org.id,
                code="retired",
                version=1,
                name="Retired policy",
                description="d",
                effect="DENY",
                subject_match={"roles": ["Analyst"]},
                resource_match={"classifications": ["INTERNAL"]},
                action_match=["READ_DATA"],
                priority=1,
                status="RETIRED",
                created_by="steward-1",
            ),
        ]
    )
    await session.flush()

    report = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="analyst-1",
        is_self_service=True,
        requester_roles=frozenset({"Analyst"}),
        now=_NOW,
    )

    decisions = {d.classification: d.decision for d in report.abac_classification_decisions}
    assert decisions == {"INTERNAL": "ALLOW", "RESTRICTED": "DENY"}
    assert "Evaluated 2 active access polic" in report.abac_note


async def test_on_behalf_of_report_never_runs_abac(session: AsyncSession) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    await _binding(session, org, workspace, datasource, permitted_classifications=["INTERNAL"])

    report = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="admin-1",
        is_self_service=False,
        requester_roles=None,
        now=_NOW,
    )

    assert report.abac_classification_decisions == []
    assert report.abac_note == ABAC_SELF_SERVICE_ONLY_NOTE
    # The grant data itself is still real and present.
    assert len(report.source_entitlements) == 1


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


async def test_checksum_is_reproducible_for_identical_state(session: AsyncSession) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    await _binding(session, org, workspace, datasource, permitted_classifications=["INTERNAL"])
    await session.flush()

    first = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="analyst-1",
        is_self_service=True,
        requester_roles=frozenset({"Analyst"}),
        now=_NOW,
    )
    second = await build_entitlement_report(
        session,
        organization_id=org.id,
        subject_principal_id="analyst-1",
        subject_principal_type="USER",
        requested_by="analyst-1",
        is_self_service=True,
        requester_roles=frozenset({"Analyst"}),
        now=_NOW + timedelta(hours=1),  # generated_at differs; checksum must not
    )
    assert first.checksum == second.checksum


# ---------------------------------------------------------------------------
# API: self-service generate, on-behalf-of gating, persistence, read-back
# ---------------------------------------------------------------------------


async def test_endpoint_self_service_generate_persists_and_reads_back(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    await _binding(session, org, workspace, datasource, permitted_classifications=["INTERNAL"])
    await session.commit()

    context = _context(org, "analyst-1", roles=frozenset({"Analyst"}))
    created = await generate_entitlement_report(
        GenerateEntitlementReportRequest(), context=context, session=session
    )

    assert created.subject_principal_id == "analyst-1"
    assert created.is_self_service is True
    assert len(created.source_entitlements) == 1

    fetched = await get_entitlement_report(created.id, context=context, session=session)
    assert fetched.id == created.id
    assert fetched.checksum == created.checksum


async def test_endpoint_refuses_a_non_elevated_caller_pulling_someone_elses_report(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    context = _context(org, "analyst-1", roles=frozenset({"Analyst"}))

    with pytest.raises(HTTPException) as excinfo:
        await generate_entitlement_report(
            GenerateEntitlementReportRequest(principal_id="someone-else"),
            context=context,
            session=session,
        )
    assert excinfo.value.status_code == 403


async def test_endpoint_lets_an_elevated_caller_pull_a_report_on_behalf_of_another(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace_with_membership(session, org, principal_id="analyst-1")
    await _binding(session, org, workspace, datasource, permitted_classifications=["PII"])
    await session.commit()

    admin_context = _context(org, "admin-1", roles=frozenset({"PlatformAdmin"}))
    created = await generate_entitlement_report(
        GenerateEntitlementReportRequest(principal_id="analyst-1"),
        context=admin_context,
        session=session,
    )

    assert created.subject_principal_id == "analyst-1"
    assert created.requested_by == "admin-1"
    assert created.is_self_service is False
    assert created.abac_classification_decisions == []
    assert len(created.source_entitlements) == 1


async def test_list_reports_scopes_a_non_elevated_caller_to_their_own_reports(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    await session.commit()

    analyst_context = _context(org, "analyst-1", roles=frozenset({"Analyst"}))
    other_context = _context(org, "analyst-2", roles=frozenset({"Analyst"}))
    await generate_entitlement_report(
        GenerateEntitlementReportRequest(), context=analyst_context, session=session
    )
    await generate_entitlement_report(
        GenerateEntitlementReportRequest(), context=other_context, session=session
    )

    page = await list_entitlement_reports(
        subject_principal_id=None, limit=50, offset=0, context=analyst_context, session=session
    )
    assert page.total == 1
    assert page.items[0].subject_principal_id == "analyst-1"
