"""QG-2: source-native row/column policy synchronization.

Two layers, tested separately:

* `aida.policy_native_sync` -- pure DDL generation and policy resolution (no I/O),
  plus `apply_native_sync_plan` against an injected fake connection (never a real
  Postgres/SQL Server instance -- the same "verified only against a mock" posture
  QG-5's `VaultTransitSigningProvider` tests already carry in this codebase).
* `aida.policy_native_sync_api` -- the maker-checker HTTP surface, exercised the
  same way `tests/test_profiling_exception_policy.py` exercises its endpoints:
  calling the handler functions directly against an in-memory sqlite session with
  a hand-built `SecurityContext`, the pattern this codebase already uses for
  endpoints with no live-DB test harness.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.policy_native_sync_api as api_module
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    PolicyNativeSyncRequest,
    Project,
)
from aida.policy_engine import PolicyRecord
from aida.policy_native_sync import (
    NativeColumnPolicy,
    NativeRowPolicy,
    NativeSyncPlan,
    PolicyNativeSyncError,
    apply_native_sync_plan,
    build_native_sync_plan,
    postgres_row_policy_statements,
    resolve_native_table_policies,
    sqlserver_column_mask_statements,
)
from aida.schemas import Page
from aida.security import SecurityContext

# No module-level `pytestmark = pytest.mark.asyncio`: `asyncio_mode = "auto"`
# (pyproject.toml) already runs every `async def test_*` as an asyncio test, and
# this module -- unlike `test_profiling_exception_policy.py` -- mixes plain
# synchronous DDL-generation tests with the async HTTP-surface ones below, so a
# blanket mark would misfire on the synchronous majority.


def _policy(
    *,
    effect: str,
    resource_match: dict[str, Any] | None = None,
    subject_match: dict[str, Any] | None = None,
    transform: dict[str, Any] | None = None,
    condition: dict[str, Any] | None = None,
    priority: int = 100,
    code: str = "p",
    version: int = 1,
) -> PolicyRecord:
    return PolicyRecord(
        id=uuid4(),
        code=code,
        version=version,
        effect=effect,
        priority=priority,
        subject_match=subject_match or {},
        resource_match=resource_match or {},
        action_match=(),
        transform=transform or {},
        condition=condition or {},
    )


# ---------------------------------------------------------------------------
# Resolution: which governed policies are eligible for native sync
# ---------------------------------------------------------------------------


def test_resolve_finds_unconditional_row_filter_and_column_mask() -> None:
    ds = uuid4()
    row_policy = _policy(
        effect="FILTER",
        code="rls-tenant",
        resource_match={"resource_types": ["table"], "datasource_ids": [str(ds)]},
        transform={"row_filter": "tenant_id = current_setting('app.tenant_id')"},
    )
    mask_policy = _policy(
        effect="MASK",
        code="mask-pii",
        resource_match={"resource_types": ["column"], "classifications": ["PII"]},
        transform={"masking_profile": "PARTIAL"},
    )
    row_policies, column_policies = resolve_native_table_policies(
        (row_policy, mask_policy),
        datasource_id=ds,
        schema_name="public",
        table_name="customers",
        columns=[("ssn", "PII"), ("name", "UNCLASSIFIED"), ("email", "")],
    )
    assert [p.policy_code for p in row_policies] == ["rls-tenant"]
    assert row_policies[0].row_filter == "tenant_id = current_setting('app.tenant_id')"
    assert [p.column_name for p in column_policies] == ["ssn"]
    assert column_policies[0].masking_profile == "PARTIAL"


def test_resolve_excludes_subject_conditional_policies() -> None:
    """A policy scoped to a subject (role, purpose, principal kind) cannot become an
    unconditional native construct -- see the module docstring's whole argument."""
    ds = uuid4()
    policy = _policy(
        effect="MASK",
        resource_match={"resource_types": ["column"], "classifications": ["PII"]},
        subject_match={"principal_kind": "AGENT"},
        transform={"masking_profile": "DEFAULT"},
    )
    _row, column_policies = resolve_native_table_policies(
        (policy,),
        datasource_id=ds,
        schema_name="public",
        table_name="t",
        columns=[("ssn", "PII")],
    )
    assert column_policies == ()


def test_resolve_excludes_business_node_scoped_policies() -> None:
    """`business_node_ids` needs a closure query this module does not perform, so a
    policy that sets it is left to application-level enforcement, not synced with a
    silently narrowed meaning."""
    ds = uuid4()
    policy = _policy(
        effect="FILTER",
        resource_match={
            "resource_types": ["table"],
            "business_node_ids": [str(uuid4())],
        },
        transform={"row_filter": "1=1"},
    )
    row_policies, _cols = resolve_native_table_policies(
        (policy,), datasource_id=ds, schema_name="public", table_name="t", columns=[]
    )
    assert row_policies == ()


def test_resolve_respects_datasource_and_schema_scoping() -> None:
    ds = uuid4()
    other_ds = uuid4()
    wrong_datasource = _policy(
        effect="FILTER",
        resource_match={"resource_types": ["table"], "datasource_ids": [str(other_ds)]},
        transform={"row_filter": "1=1"},
    )
    wrong_schema = _policy(
        effect="FILTER",
        resource_match={"resource_types": ["table"], "schema_pattern": "reporting_*"},
        transform={"row_filter": "1=1"},
    )
    matching = _policy(
        effect="FILTER",
        code="ok",
        resource_match={"resource_types": ["table"], "schema_pattern": "public"},
        transform={"row_filter": "1=1"},
    )
    row_policies, _cols = resolve_native_table_policies(
        (wrong_datasource, wrong_schema, matching),
        datasource_id=ds,
        schema_name="public",
        table_name="t",
        columns=[],
    )
    assert [p.policy_code for p in row_policies] == ["ok"]


def test_resolve_picks_highest_priority_column_policy_on_conflict() -> None:
    ds = uuid4()
    low = _policy(
        effect="MASK",
        code="low",
        priority=10,
        resource_match={"resource_types": ["column"]},
        transform={"masking_profile": "DEFAULT"},
    )
    high = _policy(
        effect="MASK",
        code="high",
        priority=90,
        resource_match={"resource_types": ["column"]},
        transform={"masking_profile": "EMAIL"},
    )
    _rows, column_policies = resolve_native_table_policies(
        (low, high),
        datasource_id=ds,
        schema_name="public",
        table_name="t",
        columns=[("email", "PII")],
    )
    assert len(column_policies) == 1
    assert column_policies[0].policy_code == "high"
    assert column_policies[0].masking_profile == "EMAIL"


# ---------------------------------------------------------------------------
# DDL generation -- correctness and safety
# ---------------------------------------------------------------------------


def test_postgres_row_policy_statements_are_real_rls_syntax() -> None:
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="accounts",
        policy_code="tenant-scope",
        policy_version=3,
        row_filter="tenant_id = current_setting('app.tenant_id')::uuid",
    )
    statements = postgres_row_policy_statements(policy)
    kinds = [s.kind for s in statements]
    assert kinds == [
        "ENABLE_ROW_LEVEL_SECURITY",
        "FORCE_ROW_LEVEL_SECURITY",
        "DROP_EXISTING_ROW_POLICY",
        "CREATE_ROW_POLICY",
    ]
    enable, force, drop, create = statements
    assert enable.sql == 'ALTER TABLE "public"."accounts" ENABLE ROW LEVEL SECURITY;'
    assert force.sql == 'ALTER TABLE "public"."accounts" FORCE ROW LEVEL SECURITY;'
    assert drop.sql == (
        'DROP POLICY IF EXISTS "atlas_rowpolicy_tenant_scope_v3" ON "public"."accounts";'
    )
    assert 'CREATE POLICY "atlas_rowpolicy_tenant_scope_v3" ON "public"."accounts"' in create.sql
    assert "FOR SELECT" in create.sql
    assert "USING (" in create.sql
    assert all(s.target_column is None for s in statements)
    assert all(s.policy_code == "tenant-scope" for s in statements)


def test_postgres_identifiers_containing_quotes_are_escaped_not_broken_out_of() -> None:
    policy = NativeRowPolicy(
        schema_name='pub"lic',
        table_name='cust"omers',
        policy_code="p",
        policy_version=1,
        row_filter="1=1",
    )
    statements = postgres_row_policy_statements(policy)
    for statement in statements:
        # The escaped identifier appears as a doubled quote, and the statement
        # never contains a *lone*, unescaped quote that could terminate the
        # identifier early.
        assert '"pub""lic"' in statement.sql
        assert '"cust""omers"' in statement.sql


def test_sqlserver_column_mask_statements_map_known_profiles() -> None:
    for profile, expected_function in (
        ("DEFAULT", "default()"),
        ("EMAIL", "email()"),
        ("PARTIAL", 'partial(1, "XXXXXXX", 0)'),
        ("RANDOM", "random(1, 100)"),
        ("SOMETHING_UNKNOWN", "default()"),
    ):
        policy = NativeColumnPolicy(
            schema_name="dbo",
            table_name="customers",
            column_name="ssn",
            classification="PII",
            policy_code="mask-ssn",
            policy_version=1,
            masking_profile=profile,
        )
        statements = sqlserver_column_mask_statements(policy)
        kinds = [s.kind for s in statements]
        assert kinds == ["DROP_EXISTING_COLUMN_MASK", "ADD_COLUMN_MASK"]
        add_mask = statements[1]
        assert add_mask.sql == (
            "ALTER TABLE [dbo].[customers] ALTER COLUMN [ssn] "
            f"ADD MASKED WITH (FUNCTION = '{expected_function}');"
        )
        assert "mc.is_masked = 1" in statements[0].sql
        assert (
            "EXEC(N'ALTER TABLE [dbo].[customers] ALTER COLUMN [ssn] DROP MASKED;')"
            in statements[0].sql
        )
        assert statements[0].target_column == "ssn"


def test_sqlserver_identifiers_and_literals_containing_quotes_are_escaped() -> None:
    policy = NativeColumnPolicy(
        schema_name="dbo",
        table_name="cust]omers",
        column_name="ss'n",
        classification="PII",
        policy_code="m",
        policy_version=1,
        masking_profile="DEFAULT",
    )
    statements = sqlserver_column_mask_statements(policy)
    drop_check, add_mask = statements
    assert "[cust]]omers]" in drop_check.sql
    assert "[ss'n]" in add_mask.sql
    assert "t.name = 'cust]omers'" in drop_check.sql
    assert "mc.name = 'ss''n'" in drop_check.sql


@pytest.mark.parametrize(
    "malicious_predicate",
    [
        "1=1; DROP TABLE customers;--",
        "tenant_id IN (SELECT id FROM other); DROP TABLE x",
    ],
)
def test_row_filter_rejects_multiple_statements(malicious_predicate: str) -> None:
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="t",
        policy_code="p",
        policy_version=1,
        row_filter=malicious_predicate,
    )
    with pytest.raises(PolicyNativeSyncError, match="single expression"):
        postgres_row_policy_statements(policy)


@pytest.mark.parametrize(
    "dangerous_predicate",
    [
        "(SELECT 1 FROM pg_sleep(5)) = 1",
        "tenant_id = (SELECT dblink_connect('evil'))",
        "1 = (SELECT pg_read_file('/etc/passwd') IS NOT NULL)",
    ],
)
def test_row_filter_rejects_dangerous_functions_even_inside_a_subquery(
    dangerous_predicate: str,
) -> None:
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="t",
        policy_code="p",
        policy_version=1,
        row_filter=dangerous_predicate,
    )
    with pytest.raises(PolicyNativeSyncError, match="forbidden function"):
        postgres_row_policy_statements(policy)


def test_row_filter_rejects_ddl_and_dml_nodes() -> None:
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="t",
        policy_code="p",
        policy_version=1,
        row_filter="1=1) OR (SELECT 1 WHERE EXISTS(DELETE FROM t",
    )
    with pytest.raises(PolicyNativeSyncError):
        postgres_row_policy_statements(policy)


def test_row_filter_accepts_a_legitimate_subquery() -> None:
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="t",
        policy_code="p",
        policy_version=1,
        row_filter="tenant_id IN (SELECT id FROM allowed_tenants WHERE active)",
    )
    statements = postgres_row_policy_statements(policy)
    create = statements[-1]
    assert "IN (SELECT id FROM allowed_tenants WHERE active)" in create.sql


def test_row_filter_comment_smuggling_cannot_break_out_of_the_using_clause() -> None:
    """`sqlglot` re-renders the parsed AST rather than passing the input through, so
    a `--`-style trailing comment becomes an inert, balanced `/* ... */` comment
    inside the generated `USING (...)` clause -- it cannot terminate the clause
    early or splice in new SQL."""
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="t",
        policy_code="p",
        policy_version=1,
        row_filter="tenant_id = 1 -- ) OR (1=1",
    )
    statements = postgres_row_policy_statements(policy)
    create = statements[-1]
    # The clause is still syntactically closed -- exactly one opening and one
    # closing paren around the USING predicate's own subtree -- and the smuggled
    # text is neutralised as a comment rather than live SQL.
    assert create.sql.count("USING (") == 1
    assert create.sql.rstrip(";").endswith(")")
    assert "/*" in create.sql  # the -- comment survived only as an inert comment


def test_unparseable_predicate_is_rejected() -> None:
    policy = NativeRowPolicy(
        schema_name="public",
        table_name="t",
        policy_code="p",
        policy_version=1,
        row_filter="tenant_id = = =",
    )
    with pytest.raises(PolicyNativeSyncError, match="unparseable"):
        postgres_row_policy_statements(policy)


# ---------------------------------------------------------------------------
# build_native_sync_plan -- per-connector-type shape
# ---------------------------------------------------------------------------


def test_build_plan_rejects_unsupported_connector_type() -> None:
    with pytest.raises(PolicyNativeSyncError, match="snowflake"):
        build_native_sync_plan(
            (),
            datasource_id=uuid4(),
            connector_type="snowflake",
            schema_name="public",
            table_name="t",
            columns=[],
        )


def test_build_plan_postgres_syncs_rows_and_reports_columns_unsupported() -> None:
    ds = uuid4()
    row = _policy(
        effect="FILTER",
        code="rls",
        resource_match={"resource_types": ["table"]},
        transform={"row_filter": "1=1"},
    )
    mask = _policy(
        effect="MASK",
        code="mask",
        resource_match={"resource_types": ["column"]},
        transform={"masking_profile": "DEFAULT"},
    )
    plan = build_native_sync_plan(
        (row, mask),
        datasource_id=ds,
        connector_type="postgres",
        schema_name="public",
        table_name="t",
        columns=[("ssn", "PII")],
    )
    assert len(plan.row_policies) == 1
    assert plan.column_policies == ()
    assert any(s.kind == "CREATE_ROW_POLICY" for s in plan.statements)
    assert len(plan.unsupported) == 1
    assert "column-masking" in plan.unsupported[0]


def test_build_plan_sqlserver_syncs_columns_and_reports_rows_unsupported() -> None:
    ds = uuid4()
    row = _policy(
        effect="FILTER",
        code="rls",
        resource_match={"resource_types": ["table"]},
        transform={"row_filter": "1=1"},
    )
    mask = _policy(
        effect="MASK",
        code="mask",
        resource_match={"resource_types": ["column"]},
        transform={"masking_profile": "EMAIL"},
    )
    plan = build_native_sync_plan(
        (row, mask),
        datasource_id=ds,
        connector_type="sqlserver",
        schema_name="dbo",
        table_name="t",
        columns=[("email", "PII")],
    )
    assert plan.row_policies == ()
    assert len(plan.column_policies) == 1
    assert any(s.kind == "ADD_COLUMN_MASK" for s in plan.statements)
    assert len(plan.unsupported) == 1
    assert "row-level security" in plan.unsupported[0]


def test_build_plan_empty_when_no_obligations_apply() -> None:
    plan = build_native_sync_plan(
        (), datasource_id=uuid4(), connector_type="postgres",
        schema_name="public", table_name="t", columns=[],
    )
    assert plan.statements == ()
    assert plan.unsupported == ()


# ---------------------------------------------------------------------------
# apply_native_sync_plan -- against an injected fake connection (never a real DB)
# ---------------------------------------------------------------------------


@dataclass
class _FakeTransaction:
    entered: bool = False

    async def __aenter__(self) -> _FakeTransaction:
        self.entered = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@dataclass
class _FakePostgresConnection:
    executed: list[str] = field(default_factory=list)
    closed: bool = False

    async def execute(self, sql: str) -> str:
        self.executed.append(sql)
        return "OK"

    async def close(self) -> None:
        self.closed = True

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


async def test_apply_postgres_executes_every_statement_in_order_and_closes() -> None:
    connection = _FakePostgresConnection()

    async def fake_connect(dsn: str, *, timeout_seconds: float) -> _FakePostgresConnection:
        assert dsn == "postgres://u:p@host/db"
        return connection

    plan = NativeSyncPlan(
        datasource_id=uuid4(),
        connector_type="postgres",
        schema_name="public",
        table_name="t",
        row_policies=(),
        column_policies=(),
        statements=(
            postgres_row_policy_statements(
                NativeRowPolicy(
                    schema_name="public",
                    table_name="t",
                    policy_code="p",
                    policy_version=1,
                    row_filter="1=1",
                )
            )
        ),
    )
    await apply_native_sync_plan(
        plan, dsn="postgres://u:p@host/db", postgres_connect=fake_connect
    )
    assert connection.executed == [s.sql for s in plan.statements]
    assert connection.closed is True


async def test_apply_is_a_noop_for_an_empty_plan() -> None:
    calls = []

    async def fake_connect(dsn: str, *, timeout_seconds: float) -> _FakePostgresConnection:
        calls.append(dsn)
        return _FakePostgresConnection()

    plan = NativeSyncPlan(
        datasource_id=uuid4(),
        connector_type="postgres",
        schema_name="public",
        table_name="t",
        row_policies=(),
        column_policies=(),
        statements=(),
    )
    await apply_native_sync_plan(plan, dsn="postgres://u:p@host/db", postgres_connect=fake_connect)
    assert calls == []


async def test_apply_sqlserver_executes_every_statement_and_commits() -> None:
    executed: list[str] = []
    committed = {"value": False}
    closed = {"value": False}

    class _FakeCursor:
        def execute(self, sql: str) -> None:
            executed.append(sql)

        def close(self) -> None:
            pass

    class _FakeSqlServerConnection:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def commit(self) -> None:
            committed["value"] = True

        def rollback(self) -> None:  # pragma: no cover - not exercised on success
            pass

        def close(self) -> None:
            closed["value"] = True

    def fake_connect(params: Any, *, timeout_seconds: float) -> _FakeSqlServerConnection:
        assert params.host == "host"
        assert params.database == "db"
        return _FakeSqlServerConnection()

    plan = NativeSyncPlan(
        datasource_id=uuid4(),
        connector_type="sqlserver",
        schema_name="dbo",
        table_name="t",
        row_policies=(),
        column_policies=(),
        statements=sqlserver_column_mask_statements(
            NativeColumnPolicy(
                schema_name="dbo",
                table_name="t",
                column_name="ssn",
                classification="PII",
                policy_code="p",
                policy_version=1,
                masking_profile="DEFAULT",
            )
        ),
    )
    await apply_native_sync_plan(
        plan, dsn="mssql://u:p@host:1433/db", sqlserver_connect=fake_connect
    )
    assert executed == [s.sql for s in plan.statements]
    assert committed["value"] is True
    assert closed["value"] is True


async def test_apply_sqlserver_rolls_back_and_propagates_on_failure() -> None:
    rolled_back = {"value": False}

    class _FailingCursor:
        def execute(self, sql: str) -> None:
            raise RuntimeError("boom")

        def close(self) -> None:
            pass

    class _FakeSqlServerConnection:
        def cursor(self) -> _FailingCursor:
            return _FailingCursor()

        def commit(self) -> None:  # pragma: no cover - not reached on failure
            pass

        def rollback(self) -> None:
            rolled_back["value"] = True

        def close(self) -> None:
            pass

    def fake_connect(params: Any, *, timeout_seconds: float) -> _FakeSqlServerConnection:
        return _FakeSqlServerConnection()

    plan = NativeSyncPlan(
        datasource_id=uuid4(),
        connector_type="sqlserver",
        schema_name="dbo",
        table_name="t",
        row_policies=(),
        column_policies=(),
        statements=sqlserver_column_mask_statements(
            NativeColumnPolicy(
                schema_name="dbo",
                table_name="t",
                column_name="ssn",
                classification="PII",
                policy_code="p",
                policy_version=1,
                masking_profile="DEFAULT",
            )
        ),
    )
    with pytest.raises(RuntimeError, match="boom"):
        await apply_native_sync_plan(
            plan, dsn="mssql://u:p@host:1433/db", sqlserver_connect=fake_connect
        )
    assert rolled_back["value"] is True


# ---------------------------------------------------------------------------
# HTTP surface -- maker-checker, audit evidence
# ---------------------------------------------------------------------------

_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    # Same sqlite BigInteger-autoincrement workaround `test_profiling_exception_policy.py`
    # uses -- sqlite does not auto-populate a bare `BIGINT` primary key.
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


def _context(
    organization_id, principal_id="requester", roles=("PlatformAdmin",)
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles),
    )


async def _seed_table_with_policy(
    session: AsyncSession, monkeypatch
) -> tuple[DataSource, str, str]:
    """A datasource with one table, one PII column, and an unconditional row-filter
    ABAC policy that applies to it -- enough for `preview`/`request` to find a
    real, non-empty plan."""
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        name="Ungoverned", code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, name="Warehouse", slug=f"wh-{uuid4().hex[:8]}",
    )
    monkeypatch.setenv("QG2_TEST_DSN", "postgres://u:p@host/db")
    datasource = DataSource(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, project_id=project.id, name="primary",
        connector_type="postgres", dialect="postgres", environment="PROD",
        network_zone="default", credential_reference="env://QG2_TEST_DSN",
        capabilities={}, status="ACTIVE",
    )
    catalog = MetadataCatalog(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id,
        name="db", fingerprint="f",
    )
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="f",
    )
    table = MetadataTable(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, schema_id=schema.id,
        name="customers", object_type="TABLE", fingerprint="f",
    )
    column = MetadataColumn(
        id=uuid4(), organization_id=org.id, table_id=table.id, name="ssn", ordinal_position=1,
        physical_type="text", nullable=True, classification="PII", fingerprint="f",
    )
    session.add_all([org, lob, domain, project, datasource, catalog, schema, table, column])
    await session.commit()

    from aida.models import AccessPolicy

    policy = AccessPolicy(
        id=uuid4(), organization_id=org.id, code="rls-tenant", version=1, name="Tenant RLS",
        effect="FILTER", priority=100, subject_match={}, action_match=[],
        resource_match={"resource_types": ["table"], "schema_pattern": "public"},
        transform={"row_filter": "tenant_id = current_setting('app.tenant_id')"},
        condition={}, status="ACTIVE", created_by="steward",
    )
    session.add(policy)
    await session.commit()
    return datasource, "public", "customers"


async def test_preview_returns_a_plan_without_persisting_a_request(session, monkeypatch) -> None:
    datasource, schema_name, table_name = await _seed_table_with_policy(session, monkeypatch)
    plan = await api_module.preview_native_policy_sync(
        datasource.id,
        api_module.NativePolicySyncTableRequest(schema_name=schema_name, table_name=table_name),
        context=_context(datasource.organization_id),
        session=session,
    )
    assert plan.row_policy_count == 1
    assert any(s.kind == "CREATE_ROW_POLICY" for s in plan.statements)
    remaining = (await session.execute(PolicyNativeSyncRequest.__table__.select())).all()
    assert remaining == []


async def test_request_endpoint_rejects_a_table_with_no_obligations(session, monkeypatch) -> None:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        name="Ungoverned", code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, name="Warehouse", slug=f"wh-{uuid4().hex[:8]}",
    )
    monkeypatch.setenv("QG2_TEST_DSN_EMPTY", "postgres://u:p@host/db")
    datasource = DataSource(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, project_id=project.id, name="primary",
        connector_type="postgres", dialect="postgres", environment="PROD",
        network_zone="default", credential_reference="env://QG2_TEST_DSN_EMPTY",
        capabilities={}, status="ACTIVE",
    )
    catalog = MetadataCatalog(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id,
        name="db", fingerprint="f",
    )
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="f",
    )
    table = MetadataTable(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, schema_id=schema.id,
        name="orders", object_type="TABLE", fingerprint="f",
    )
    session.add_all([org, lob, domain, project, datasource, catalog, schema, table])
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await api_module.request_native_policy_sync(
            datasource.id,
            api_module.PolicyNativeSyncRequestCreate(
                schema_name="public", table_name="orders", reason="testing"
            ),
            context=_context(datasource.organization_id),
            session=session,
        )
    assert excinfo.value.status_code == 422


async def test_request_then_decide_maker_checker_and_apply(session, monkeypatch) -> None:
    datasource, schema_name, table_name = await _seed_table_with_policy(session, monkeypatch)
    request = await api_module.request_native_policy_sync(
        datasource.id,
        api_module.PolicyNativeSyncRequestCreate(
            schema_name=schema_name, table_name=table_name, reason="sync tenant RLS"
        ),
        context=_context(datasource.organization_id, principal_id="alice"),
        session=session,
    )
    assert request.status == "PENDING"
    assert request.row_policy_count == 1

    # Self-approval is refused.
    with pytest.raises(HTTPException) as excinfo:
        await api_module.decide_native_policy_sync_request(
            request.id,
            api_module.NativePolicySyncDecisionRequest(decision="APPROVE"),
            context=_context(
                datasource.organization_id, principal_id="alice", roles=("DataSteward",)
            ),
            session=session,
            settings=Settings(),
        )
    assert excinfo.value.status_code == 409

    applied_plans: list[Any] = []

    async def fake_apply(plan, *, dsn, timeout_seconds):
        applied_plans.append((plan, dsn))

    monkeypatch.setattr(api_module, "apply_native_sync_plan", fake_apply)

    decided = await api_module.decide_native_policy_sync_request(
        request.id,
        api_module.NativePolicySyncDecisionRequest(decision="APPROVE"),
        context=_context(datasource.organization_id, principal_id="bob", roles=("DataSteward",)),
        session=session,
        settings=Settings(),
    )
    assert decided.status == "APPLIED"
    assert decided.decided_by == "bob"
    assert decided.applied_at is not None
    assert len(applied_plans) == 1
    applied_plan, dsn = applied_plans[0]
    assert dsn == "postgres://u:p@host/db"
    stored_request = await session.get(PolicyNativeSyncRequest, request.id)
    assert stored_request is not None
    assert [s.sql for s in applied_plan.statements] == [
        item["sql"] for item in stored_request.statements
    ]

    # Deciding an already-decided request is refused.
    with pytest.raises(HTTPException) as excinfo:
        await api_module.decide_native_policy_sync_request(
            request.id,
            api_module.NativePolicySyncDecisionRequest(decision="APPROVE"),
            context=_context(
                datasource.organization_id, principal_id="carol", roles=("DataSteward",)
            ),
            session=session,
            settings=Settings(),
        )
    assert excinfo.value.status_code == 409

    audit_rows = (await session.execute(AuditEvent.__table__.select())).all()
    audit_actions = {row.action for row in audit_rows}
    assert "policy_native_sync.request" in audit_actions
    assert "policy_native_sync.decide" in audit_actions
    assert "policy_native_sync.apply" in audit_actions
    apply_audit = next(row for row in audit_rows if row.action == "policy_native_sync.apply")
    assert apply_audit.outcome == "SUCCESS"
    assert "statements_hash" in apply_audit.details
    assert apply_audit.details["statements_hash"]


async def test_reject_never_attempts_apply(session, monkeypatch) -> None:
    datasource, schema_name, table_name = await _seed_table_with_policy(session, monkeypatch)
    request = await api_module.request_native_policy_sync(
        datasource.id,
        api_module.PolicyNativeSyncRequestCreate(
            schema_name=schema_name, table_name=table_name, reason="sync tenant RLS"
        ),
        context=_context(datasource.organization_id, principal_id="alice"),
        session=session,
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("apply must never be attempted on a REJECT decision")

    monkeypatch.setattr(api_module, "apply_native_sync_plan", explode)

    decided = await api_module.decide_native_policy_sync_request(
        request.id,
        api_module.NativePolicySyncDecisionRequest(decision="REJECT", reason="not ready"),
        context=_context(datasource.organization_id, principal_id="bob", roles=("DataSteward",)),
        session=session,
        settings=Settings(),
    )
    assert decided.status == "REJECTED"
    assert decided.applied_at is None


async def test_a_failed_apply_is_recorded_durably_and_does_not_raise(session, monkeypatch) -> None:
    datasource, schema_name, table_name = await _seed_table_with_policy(session, monkeypatch)
    request = await api_module.request_native_policy_sync(
        datasource.id,
        api_module.PolicyNativeSyncRequestCreate(
            schema_name=schema_name, table_name=table_name, reason="sync tenant RLS"
        ),
        context=_context(datasource.organization_id, principal_id="alice"),
        session=session,
    )

    async def failing_apply(plan, *, dsn, timeout_seconds):
        raise ConnectionRefusedError("could not reach the source")

    monkeypatch.setattr(api_module, "apply_native_sync_plan", failing_apply)

    decided = await api_module.decide_native_policy_sync_request(
        request.id,
        api_module.NativePolicySyncDecisionRequest(decision="APPROVE"),
        context=_context(datasource.organization_id, principal_id="bob", roles=("DataSteward",)),
        session=session,
        settings=Settings(),
    )
    assert decided.status == "APPLY_FAILED"
    assert decided.apply_error == "ConnectionRefusedError"
    assert decided.applied_at is None

    audit_rows = (await session.execute(AuditEvent.__table__.select())).all()
    apply_audit = next(row for row in audit_rows if row.action == "policy_native_sync.apply")
    assert apply_audit.outcome == "FAILURE"
    assert apply_audit.details["error_class"] == "ConnectionRefusedError"
    # INV-6: never the raw driver error text, which could carry source-side values.
    assert "could not reach the source" not in str(apply_audit.details)


async def test_list_requests_filters_by_status(session, monkeypatch) -> None:
    datasource, schema_name, table_name = await _seed_table_with_policy(session, monkeypatch)
    await api_module.request_native_policy_sync(
        datasource.id,
        api_module.PolicyNativeSyncRequestCreate(
            schema_name=schema_name, table_name=table_name, reason="first"
        ),
        context=_context(datasource.organization_id, principal_id="alice"),
        session=session,
    )
    page = await api_module.list_native_policy_sync_requests(
        datasource.id,
        request_status="PENDING",
        limit=100,
        offset=0,
        context=_context(datasource.organization_id, roles=("Viewer",)),
        session=session,
    )
    assert isinstance(page, Page)
    assert page.total == 1
    assert page.items[0].status == "PENDING"

    page_approved = await api_module.list_native_policy_sync_requests(
        datasource.id,
        request_status="APPROVED",
        limit=100,
        offset=0,
        context=_context(datasource.organization_id, roles=("Viewer",)),
        session=session,
    )
    assert page_approved.total == 0
