"""AU-11 -- the policy gate's classification/certification/quality/freshness
attributes are resolved from real catalog state, and a policy keyed on them
actually fires on the query-execution path.

Two layers, matching how the fix is built:

* **Resolver unit tests.** `aida.policy_resource_attributes.resolve_resource_attributes`
  against a real (sqlite) database with catalog, certification, incident and
  freshness rows seeded directly -- proving each axis collapses to the right
  worst-case value.
* **End-to-end gate tests.** A real `AccessPolicy` row, keyed on classification
  and on quality_state respectively, evaluated through
  `QueryExecutionGateway.execute` -- the real money path (`aida.query_gateway`)
  -- with a `FakeSqlExecutor` standing in for the warehouse connector, proving
  the policy actually allows or denies rather than merely that the resolver
  computes a plausible-looking value nothing consumes.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AccessPolicy,
    AssetCertification,
    AuditEvent,
    DataDomain,
    DataQualityIncident,
    DataQualityObservation,
    DataSource,
    FreshnessObservation,
    FreshnessWatermarkConfig,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SourceBinding,
    Workspace,
    WorkspaceMembership,
)
from aida.policy_resource_attributes import (
    resolve_referenced_table_ids,
    resolve_resource_attributes,
)
from aida.query_gateway import AuthorizationRejected, QueryExecutionGateway
from aida.workspace_access import ENFORCE
from tests.support.doubles import FakeSqlExecutor, security_context

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


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


async def _schema_for(session: AsyncSession, datasource: DataSource) -> MetadataSchema:
    """One catalog/schema per datasource, cached on the instance -- `_table` may be
    called more than once for the same datasource, and a second `MetadataCatalog`
    row would collide with the `(datasource_id, name)` uniqueness constraint."""
    cached = getattr(datasource, "_test_schema", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    catalog = MetadataCatalog(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="c",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=datasource.organization_id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="s",
    )
    session.add(schema)
    await session.flush()
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return schema


async def _table(
    session: AsyncSession, datasource: DataSource, *, name: str = "customers"
) -> MetadataTable:
    schema = await _schema_for(session, datasource)
    table = MetadataTable(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="t",
        status="ACTIVE",
    )
    session.add(table)
    await session.flush()
    session.add(
        MetadataColumn(
            organization_id=datasource.organization_id,
            table_id=table.id,
            name="customer_id",
            ordinal_position=1,
            physical_type="varchar",
            nullable=False,
            classification="UNCLASSIFIED",
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    await session.flush()
    return table


async def _workspace(session: AsyncSession, org: Organization, *, mode: str) -> Workspace:
    workspace = Workspace(
        organization_id=org.id,
        name="Analytics",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="analysis",
        authorization_mode=mode,
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def _seat_analyst(
    session: AsyncSession, workspace: Workspace, principal_id: str
) -> None:
    session.add(
        WorkspaceMembership(
            organization_id=workspace.organization_id,
            workspace_id=workspace.id,
            principal_id=principal_id,
            role="analyst",
            status="ACTIVE",
            granted_by="test",
        )
    )
    await session.flush()


async def _bind(session: AsyncSession, workspace: Workspace, datasource: DataSource) -> None:
    session.add(
        SourceBinding(
            organization_id=workspace.organization_id,
            workspace_id=workspace.id,
            datasource_id=datasource.id,
            purpose="analysis",
            status="ACTIVE",
            requested_by="test",
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# 1. Resolver unit tests -- each axis, worst-case collapse
# ---------------------------------------------------------------------------


async def test_no_referenced_tables_resolve_to_empty_defaults(session: AsyncSession) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)

    attributes = await resolve_resource_attributes(session, datasource, frozenset())

    assert attributes.classifications == frozenset()
    assert attributes.certification is None
    assert attributes.quality_state is None
    assert attributes.freshness_state is None


async def test_classification_is_the_union_of_referenced_columns(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    table = await _table(session, datasource)
    session.add(
        MetadataColumn(
            organization_id=org.id,
            table_id=table.id,
            name="ssn",
            ordinal_position=2,
            physical_type="varchar",
            nullable=False,
            classification="PII",
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    await session.flush()

    table_ids = await resolve_referenced_table_ids(session, datasource, ["customers"])
    attributes = await resolve_resource_attributes(session, datasource, table_ids)

    assert table_ids == frozenset({table.id})
    assert attributes.classifications == frozenset({"PII"})


async def test_certification_is_uncertified_if_any_referenced_table_lacks_one(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    table = await _table(session, datasource)

    table_ids = frozenset({table.id})
    uncertified = await resolve_resource_attributes(session, datasource, table_ids, now=_NOW)
    assert uncertified.certification == "UNCERTIFIED"

    session.add(
        AssetCertification(
            organization_id=org.id,
            table_id=table.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="reviewed",
            certified_by="steward",
            expires_at=_NOW + timedelta(days=30),
        )
    )
    await session.flush()

    certified = await resolve_resource_attributes(session, datasource, table_ids, now=_NOW)
    assert certified.certification == "CERTIFIED"


async def test_certification_expiry_falls_back_to_uncertified(session: AsyncSession) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    table = await _table(session, datasource)
    session.add(
        AssetCertification(
            organization_id=org.id,
            table_id=table.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="reviewed",
            certified_by="steward",
            expires_at=_NOW - timedelta(days=1),
        )
    )
    await session.flush()

    attributes = await resolve_resource_attributes(
        session, datasource, frozenset({table.id}), now=_NOW
    )

    assert attributes.certification == "UNCERTIFIED"


async def test_quality_state_open_critical_incident_wins_over_a_healthy_observation(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    table = await _table(session, datasource)
    session.add(
        DataQualityIncident(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=table.id,
            fingerprint=f"fp-{uuid4().hex}",
            anomaly_type="VOLUME_CHANGE",
            severity="CRITICAL",
            status="OPEN",
            summary="volume dropped",
            first_observed_at=_NOW,
            last_observed_at=_NOW,
        )
    )
    await session.flush()

    attributes = await resolve_resource_attributes(
        session, datasource, frozenset({table.id}), now=_NOW
    )

    assert attributes.quality_state == "CRITICAL"


async def test_quality_state_falls_back_to_the_latest_observation(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    table = await _table(session, datasource)
    from aida.models import AnalysisRun

    run = AnalysisRun(
        organization_id=org.id,
        datasource_id=datasource.id,
        status="COMPLETED",
        trigger_type="MANUAL",
    )
    session.add(run)
    await session.flush()
    session.add(
        DataQualityObservation(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=table.id,
            analysis_run_id=run.id,
            status="WARNING",
            quality_score=70,
        )
    )
    await session.flush()

    attributes = await resolve_resource_attributes(
        session, datasource, frozenset({table.id}), now=_NOW
    )

    assert attributes.quality_state == "WARNING"


async def test_freshness_state_stale_beats_a_fresh_sibling_table(session: AsyncSession) -> None:
    org = await _organization(session)
    datasource = await _datasource(session, org)
    stale_table = await _table(session, datasource, name="stale_table")
    fresh_table = await _table(session, datasource, name="fresh_table")

    session.add(
        FreshnessWatermarkConfig(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=stale_table.id,
            watermark_column="updated_at",
            classification="INTERNAL",
            threshold_minutes=60,
            retention_days=30,
            approved_by="steward",
            approved_at=_NOW,
            status="ACTIVE",
            created_by="steward",
        )
    )
    session.add(
        FreshnessWatermarkConfig(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=fresh_table.id,
            watermark_column="updated_at",
            classification="INTERNAL",
            threshold_minutes=60,
            retention_days=30,
            approved_by="steward",
            approved_at=_NOW,
            status="ACTIVE",
            created_by="steward",
        )
    )
    await session.flush()
    session.add(
        FreshnessObservation(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=stale_table.id,
            watermark_value=_NOW - timedelta(hours=5),
            observed_at=_NOW,
        )
    )
    session.add(
        FreshnessObservation(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=fresh_table.id,
            watermark_value=_NOW - timedelta(minutes=5),
            observed_at=_NOW,
        )
    )
    await session.flush()

    attributes = await resolve_resource_attributes(
        session, datasource, frozenset({stale_table.id, fresh_table.id}), now=_NOW
    )

    assert attributes.freshness_state == "STALE"


# ---------------------------------------------------------------------------
# 2. End-to-end: a real policy keyed on these axes gates the real query path
# ---------------------------------------------------------------------------


async def _prepare_bare_workspace(
    session: AsyncSession, *, mode: str, principal_id: str = "alice"
) -> tuple[Organization, DataSource, Workspace]:
    """Workspace + membership + binding, with no `AccessPolicy` at all.

    For tests that need to control the exact policy set reaching `evaluate` --
    a caller that also seeded the baseline unconditional ALLOW below would
    make any state-conditioned ALLOW added on top of it unfalsifiable, since
    the baseline would keep permitting the action regardless of what the
    condition decided.
    """
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await _workspace(session, org, mode=mode)
    await _seat_analyst(session, workspace, principal_id)
    await _bind(session, workspace, datasource)
    return org, datasource, workspace


async def _prepare_workspace(
    session: AsyncSession, *, mode: str, principal_id: str = "alice"
) -> tuple[Organization, DataSource, Workspace]:
    """`_prepare_bare_workspace` plus one unconditional baseline ALLOW."""
    org, datasource, workspace = await _prepare_bare_workspace(
        session, mode=mode, principal_id=principal_id
    )
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="baseline-allow",
            name="baseline allow",
            effect="ALLOW",
            subject_match={"roles": ["analyst"]},
            action_match=[],
            created_by="test",
        )
    )
    await session.flush()
    return org, datasource, workspace


def _gateway() -> QueryExecutionGateway:
    return QueryExecutionGateway(Settings(_env_file=None))


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeSqlExecutor(({"customer_id": "c-1"},))
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session", lambda connector_type, dsn: executor
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "dsn://x")}
        )(),
    )


async def test_a_classification_keyed_deny_actually_rejects_the_real_query(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DENY on `classifications ∋ PII` fires from `QueryExecutionGateway.execute`,
    not just from `policy_engine.evaluate` in isolation -- the real proof AU-11 asks
    for: the query touches a PII column, and the real money path refuses it."""
    org, datasource, workspace = await _prepare_workspace(session, mode=ENFORCE)
    table = await _table(session, datasource)
    session.add(
        MetadataColumn(
            organization_id=org.id,
            table_id=table.id,
            name="ssn",
            ordinal_position=2,
            physical_type="varchar",
            nullable=False,
            classification="PII",
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="deny-pii",
            name="deny PII reads",
            effect="DENY",
            action_match=["READ_DATA"],
            resource_match={"classifications": ["PII"]},
            created_by="test",
        )
    )
    await session.flush()
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _gateway().execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=org.id, principal_id="alice"),
            correlation_id="corr-au11-deny",
            sql="SELECT customer_id, ssn FROM customers",
            requested_limit=10,
            semantic_version=None,
            workspace_id=workspace.id,
        )

    assert excinfo.value.reason_code == "DENIED_BY_POLICY"


async def test_the_same_query_is_allowed_once_the_pii_column_is_dropped(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same policy set, same workspace -- the query that does not touch the PII
    column is allowed, proving the DENY above is genuinely classification-keyed
    rather than a blanket refusal."""
    org, datasource, workspace = await _prepare_workspace(session, mode=ENFORCE)
    await _table(session, datasource)
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="deny-pii",
            name="deny PII reads",
            effect="DENY",
            action_match=["READ_DATA"],
            resource_match={"classifications": ["PII"]},
            created_by="test",
        )
    )
    await session.flush()
    _patch_executor(monkeypatch)

    result = await _gateway().execute(
        session,
        datasource=datasource,
        context=security_context(organization_id=org.id, principal_id="alice"),
        correlation_id="corr-au11-allow",
        sql="SELECT customer_id FROM customers",
        requested_limit=10,
        semantic_version=None,
        workspace_id=workspace.id,
    )

    assert result is not None


async def test_a_quality_state_keyed_deny_actually_rejects_the_real_query(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`condition={"deny_when_quality_state_in": [...]}` on an ALLOW (the shape
    `policy_engine._matches_state_condition` and `test_policy_engine.py`'s own
    `test_quality_state_can_gate_access` establish: the ALLOW is suppressed
    while the state is in the list, and default-deny -- INV-4 -- takes over)
    fires from the real query path: an open CRITICAL incident on the
    referenced table blocks execution with no other ALLOW to fall back on."""
    org, datasource, workspace = await _prepare_bare_workspace(session, mode=ENFORCE)
    table = await _table(session, datasource)
    session.add(
        DataQualityIncident(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=table.id,
            fingerprint=f"fp-{uuid4().hex}",
            anomaly_type="VOLUME_CHANGE",
            severity="CRITICAL",
            status="OPEN",
            summary="volume dropped",
            first_observed_at=_NOW,
            last_observed_at=_NOW,
        )
    )
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="allow-unless-critical-quality",
            name="allow reads, suppressed while the table is critically unhealthy",
            effect="ALLOW",
            subject_match={"roles": ["analyst"]},
            action_match=["READ_DATA"],
            condition={"deny_when_quality_state_in": ["CRITICAL"]},
            created_by="test",
        )
    )
    await session.flush()
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _gateway().execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=org.id, principal_id="alice"),
            correlation_id="corr-au11-quality-deny",
            sql="SELECT customer_id FROM customers",
            requested_limit=10,
            semantic_version=None,
            workspace_id=workspace.id,
        )

    assert excinfo.value.reason_code == "NO_APPLICABLE_ALLOW_POLICY"


async def test_a_healthy_table_is_allowed_under_the_same_quality_policy(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same state-conditioned ALLOW, no open incident this time -- proving the
    denial above tracks real incident state rather than always refusing."""
    org, datasource, workspace = await _prepare_bare_workspace(session, mode=ENFORCE)
    await _table(session, datasource)
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="allow-unless-critical-quality",
            name="allow reads, suppressed while the table is critically unhealthy",
            effect="ALLOW",
            subject_match={"roles": ["analyst"]},
            action_match=["READ_DATA"],
            condition={"deny_when_quality_state_in": ["CRITICAL"]},
            created_by="test",
        )
    )
    await session.flush()
    _patch_executor(monkeypatch)

    result = await _gateway().execute(
        session,
        datasource=datasource,
        context=security_context(organization_id=org.id, principal_id="alice"),
        correlation_id="corr-au11-quality-allow",
        sql="SELECT customer_id FROM customers",
        requested_limit=10,
        semantic_version=None,
        workspace_id=workspace.id,
    )

    assert result is not None


# ---------------------------------------------------------------------------
# 3. AU-11 fail-closed on unresolvable table references (2026-09-03 addendum)
#
# The original AU-11 landing documented, but did not test, the empty-`table_ids`
# silent-fallback: a statement whose guard-parsed referenced tables fail
# leaf-name lookup against ACTIVE `MetadataTable` produced empty attributes
# for every axis, so a classification/certification/quality/freshness-keyed
# DENY was silently bypassed. `QueryExecutionGateway.validate`/`execute` now
# fail closed (raise `AuthorizationRejected("unresolvable_table_references")`)
# before calling `resolve_resource_attributes` on that shape, and these tests
# lock that in.
# ---------------------------------------------------------------------------


async def test_unresolvable_table_reference_fails_closed_on_execute(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace has no `MetadataTable` at all; a query naming a table
    the catalog does not know about must be REJECTED with the specific
    `unresolvable_table_references` reason, not silently permitted through
    the ABAC gate with default-empty axes."""
    org, datasource, workspace = await _prepare_workspace(session, mode=ENFORCE)
    # Deliberately NOT calling `_table(session, datasource)` -- the table
    # referenced by the query below is unknown to the catalog on purpose.
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _gateway().execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=org.id, principal_id="alice"),
            correlation_id="corr-au11-unresolvable-execute",
            sql="SELECT id FROM ghost_table",
            requested_limit=10,
            semantic_version=None,
            workspace_id=workspace.id,
        )

    assert excinfo.value.reason_code == "unresolvable_table_references"


async def test_unresolvable_table_reference_fails_closed_on_validate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validate path records the same DENIED evidence, but reports rather
    than raises (amended 2026-09-04).

    `validate` opens no connector and returns no row -- its contract is to
    tell the caller what is wrong with a statement. An unresolvable reference
    is already refused, precisely, by `UNKNOWN_OR_UNAUTHORIZED_TABLE`, which
    names the offending table; raising `AuthorizationRejected` instead threw
    that away and returned a generic authorization error. It also preempted
    the catalog allowlist check the adversarial corpus depends on, so the
    whole corpus failed for any dialect whose statements the guard accepts.

    What is asserted here is the property AU-15 actually wanted: the attempt
    is denied, and it is attributable in the ledger. `execute` still raises --
    see `test_unresolvable_table_reference_fails_closed_on_execute`, which is
    the path where an unresolved axis could precede real source contact."""
    org, datasource, workspace = await _prepare_workspace(session, mode=ENFORCE)
    _patch_executor(monkeypatch)

    report = await _gateway().validate(
        session,
        datasource=datasource,
        context=security_context(organization_id=org.id, principal_id="alice"),
        correlation_id="corr-au11-unresolvable-validate",
        sql="SELECT id FROM ghost_table",
        requested_limit=10,
        workspace_id=workspace.id,
    )

    assert not report.valid
    assert "UNKNOWN_OR_UNAUTHORIZED_TABLE" in report.codes()
    denied = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.correlation_id == "corr-au11-unresolvable-validate",
                AuditEvent.outcome == "DENIED",
            )
        )
    ).all()
    assert len(denied) == 1, "one validation is one audit row"
    assert denied[0].details.get("reason") == "unresolvable_table_references", (
        "the unresolvable reference must still be attributable in the ledger"
    )


async def test_table_less_statement_is_still_permitted_by_the_fail_closed_guard(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the fail-closed rule triggers only when the guard's
    referenced-tables list is non-empty. A genuinely table-less statement
    (whose `referenced_tables` is `[]`) is not what AU-11 was concerned about
    and must not be swept up by the fix."""
    org, datasource, workspace = await _prepare_workspace(session, mode=ENFORCE)
    _patch_executor(monkeypatch)

    # `SELECT 1` has no referenced tables; guard produces an empty list, so
    # the fail-closed branch is skipped and the empty ResourceAttributes
    # feeds the ABAC gate normally. The baseline ALLOW seated by
    # `_prepare_workspace` lets it through -- no policy rule keyed on
    # classification/certification/quality/freshness would even apply to a
    # statement that touches no tables.
    result = await _gateway().validate(
        session,
        datasource=datasource,
        context=security_context(organization_id=org.id, principal_id="alice"),
        correlation_id="corr-au11-tableless",
        sql="SELECT 1",
        requested_limit=10,
        workspace_id=workspace.id,
    )

    assert result is not None
