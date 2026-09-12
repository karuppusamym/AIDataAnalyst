"""R11-B3 and R11-D9 -- workspace enforcement actually refuses, and an operator
can find out what switching it on would break.

The two rows are paired in the tracker and they are proven together here,
because each is what makes the other meaningful: an enforcement mode nobody can
assess is not deployable, and a readiness report for a control that does not
deny is arithmetic about nothing.

**B3 -- the DENY suite.** One workspace in `ENFORCE`, one estate, and the three
refusals a pilot has to be able to show:

* a principal with the right *role* but no membership of the workspace is
  refused -- roles are claims from the identity provider and are not access;
* a principal who is a member of one workspace is refused on a datasource that
  belongs to another, which is the least-privilege property that makes a
  workspace a boundary rather than a label;
* the same principal, on their own workspace's datasource, is allowed -- the
  positive control, without which the two refusals above could be produced by
  any blanket denial.

Every refusal goes through `QueryExecutionGateway.execute`, the INV-2 choke
point, rather than through `authorize` directly: a test that proved the policy
engine denies while the query path never called it would be exactly the defect
that made the ADR-0018 rollout hard to trust.

**D9 -- the readiness surface.** `enforcement_readiness` has existed since that
rollout and `Settings.workspace_authorization_posture` instructs operators to
run it before flipping a workspace, but until now nothing called it outside
tests. These tests drive the real HTTP handler and pin both halves of what it
reports: the divergences a SHADOW workspace recorded (evidence from traffic)
and the datasources that cannot resolve a workspace at all (evidence from the
inventory, which no amount of quiet traffic can supply).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AccessPolicy,
    DataDomain,
    DataSource,
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
from aida.query_gateway import AuthorizationRejected, QueryExecutionGateway
from aida.workspace_access import ENFORCE, SHADOW, unresolved_scope
from aida.workspace_resolution import NO_BINDING_FOR_DATASOURCE, WORKSPACE_AMBIGUOUS
from atlas.modules.identity_tenancy.router import get_enforcement_readiness
from tests.support.doubles import FakeSqlExecutor, security_context


@pytest_asyncio.fixture
async def session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    """A file-backed SQLite, not the usual in-memory one, and that is load-bearing.

    `record_divergence_durably` writes the shadow record through the process's
    *own* session factory rather than the caller's session -- deliberately, so
    a divergence survives the rollback of the request that produced it. An
    in-memory database cannot show that: a second connection to
    `sqlite+aiosqlite:///:memory:` is a second, empty database, so the durable
    write would land nowhere, be swallowed by its own defensive `except`, and
    the divergence assertions below would fail for a reason that has nothing to
    do with the code under test.

    WAL because the durable write happens while the request's session still
    holds a read transaction; under SQLite's default journal that is a writer
    waiting on a reader in the same process, which is a deadlock, not a test.
    """
    url = f"sqlite+aiosqlite:///{(tmp_path / 'enforcement.db').as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("aida.workspace_access.session_factory", maker)
    async with maker() as active:
        yield active
    await engine.dispose()


class Estate:
    """One organization, two workspaces, and the datasources bound to them."""

    def __init__(self, organization: Organization, project: Project) -> None:
        self.organization = organization
        self.project = project
        self.datasources: dict[str, DataSource] = {}
        self.workspaces: dict[str, Workspace] = {}


async def _estate(session: AsyncSession) -> Estate:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:4]}")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Core", code="CORE"
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
    # One unconditional ALLOW for the analyst role. Without it every query is
    # refused by INV-4 default-deny and the suite could not tell an enforcement
    # refusal from having written no policy at all.
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
    return Estate(org, project)


async def _datasource(session: AsyncSession, estate: Estate, name: str) -> DataSource:
    datasource = DataSource(
        organization_id=estate.organization.id,
        line_of_business_id=estate.project.line_of_business_id,
        data_domain_id=estate.project.data_domain_id,
        project_id=estate.project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(
        organization_id=estate.organization.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="c",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=estate.organization.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="s",
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=estate.organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customers",
        object_type="BASE_TABLE",
        fingerprint="t",
        status="ACTIVE",
    )
    session.add(table)
    await session.flush()
    session.add(
        MetadataColumn(
            organization_id=estate.organization.id,
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
    estate.datasources[name] = datasource
    return datasource


async def _workspace(
    session: AsyncSession, estate: Estate, name: str, *, mode: str, members: tuple[str, ...] = ()
) -> Workspace:
    workspace = Workspace(
        organization_id=estate.organization.id,
        name=name,
        slug=f"{name.lower()}-{uuid4().hex[:6]}",
        purpose="analysis",
        authorization_mode=mode,
    )
    session.add(workspace)
    await session.flush()
    for principal_id in members:
        session.add(
            WorkspaceMembership(
                organization_id=estate.organization.id,
                workspace_id=workspace.id,
                principal_id=principal_id,
                role="analyst",
                status="ACTIVE",
                granted_by="test",
            )
        )
    await session.flush()
    estate.workspaces[name] = workspace
    return workspace


async def _bind(
    session: AsyncSession,
    estate: Estate,
    workspace: Workspace,
    datasource: DataSource,
) -> SourceBinding:
    binding = SourceBinding(
        organization_id=estate.organization.id,
        workspace_id=workspace.id,
        datasource_id=datasource.id,
        purpose="analysis",
        status="ACTIVE",
        requested_by="test",
    )
    session.add(binding)
    await session.flush()
    return binding


def _gateway() -> QueryExecutionGateway:
    return QueryExecutionGateway(Settings(_env_file=None))


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeSqlExecutor(({"customer_id": "c-1"},))
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session", lambda connector_type, dsn: executor
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type("_Resolver", (), {"resolve": staticmethod(lambda ref: "dsn://x")})(),
    )


async def _execute(
    session: AsyncSession,
    estate: Estate,
    datasource: DataSource,
    *,
    principal_id: str,
    workspace_id: object = None,
) -> object:
    """Execute without naming a workspace unless one is given.

    Not naming one is the realistic case and the one that matters: the
    workspace is then resolved from the datasource's own binding, so the
    caller cannot pick the workspace that suits them.
    """
    return await _gateway().execute(
        session,
        datasource=datasource,
        context=security_context(
            organization_id=estate.organization.id, principal_id=principal_id
        ),
        correlation_id=f"corr-{uuid4().hex[:8]}",
        sql="SELECT customer_id FROM customers",
        requested_limit=10,
        semantic_version=None,
        workspace_id=workspace_id,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 1. B3 -- the DENY suite, in one ENFORCE workspace
# --------------------------------------------------------------------------- #


async def _enforcing_estate(session: AsyncSession) -> Estate:
    """`retail` in ENFORCE with alice seated; `markets` in ENFORCE with bob."""
    estate = await _estate(session)
    retail_source = await _datasource(session, estate, "retail-warehouse")
    markets_source = await _datasource(session, estate, "markets-warehouse")
    retail = await _workspace(session, estate, "Retail", mode=ENFORCE, members=("alice",))
    markets = await _workspace(session, estate, "Markets", mode=ENFORCE, members=("bob",))
    await _bind(session, estate, retail, retail_source)
    await _bind(session, estate, markets, markets_source)
    return estate


async def test_a_member_of_the_workspace_is_allowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control. Without it the two refusals below would be
    satisfied by a gateway that denies everything."""
    estate = await _enforcing_estate(session)
    _patch_executor(monkeypatch)

    result = await _execute(
        session, estate, estate.datasources["retail-warehouse"], principal_id="alice"
    )

    assert result is not None


async def test_the_right_role_without_membership_is_refused(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carol carries the same `Analyst` claim the baseline policy allows and is
    seated in no workspace. Roles arrive from the identity provider and are not
    access; if this passed, every authenticated user in the bank would reach
    every ENFORCE workspace."""
    estate = await _enforcing_estate(session)
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _execute(
            session, estate, estate.datasources["retail-warehouse"], principal_id="carol"
        )

    assert excinfo.value.reason_code
    assert excinfo.value.reason_code != "unresolvable_table_references"


async def test_a_member_of_another_workspace_is_refused_least_privilege(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alice is a seated, allowed analyst -- in Retail. Against Markets' own
    datasource she is refused, and the refusal comes from the binding, not from
    anything she asked for: she named no workspace, so `resolve_workspace`
    resolved Markets from its sole live binding and evaluated her there."""
    estate = await _enforcing_estate(session)
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected):
        await _execute(
            session, estate, estate.datasources["markets-warehouse"], principal_id="alice"
        )


async def test_naming_a_workspace_you_belong_to_does_not_reach_another_ones_source(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attack the previous test leaves open: alice names Retail -- which she
    genuinely belongs to -- while pointing at Markets' datasource. Membership of
    the named workspace must not carry access to a source that workspace has no
    binding for."""
    estate = await _enforcing_estate(session)
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected):
        await _execute(
            session,
            estate,
            estate.datasources["markets-warehouse"],
            principal_id="alice",
            workspace_id=estate.workspaces["Retail"].id,
        )


# --------------------------------------------------------------------------- #
# 2. D9 -- unresolved scope, read from the inventory
# --------------------------------------------------------------------------- #


async def test_an_unbound_datasource_is_reported_as_a_blocker(session: AsyncSession) -> None:
    """A datasource no workspace binds records no divergence however long it is
    observed -- nothing can reach it to diverge. It still breaks the moment
    unresolved scope is set to DENY, which is why readiness cannot be built on
    traffic alone."""
    estate = await _estate(session)
    orphan = await _datasource(session, estate, "orphan-warehouse")

    scope = await unresolved_scope(session, organization_id=estate.organization.id)

    assert scope.unbound == 1
    assert scope.resolvable == 0
    assert not scope.ready
    assert [(d.datasource_id, d.reason_code) for d in scope.datasources] == [
        (orphan.id, NO_BINDING_FOR_DATASOURCE)
    ]


async def test_two_live_bindings_are_reported_as_ambiguous(session: AsyncSession) -> None:
    """Two live bindings is not "twice as authorized" -- `resolve_workspace`
    refuses to guess, so the request is undecided and a DENY posture refuses
    it."""
    estate = await _estate(session)
    shared = await _datasource(session, estate, "shared-warehouse")
    retail = await _workspace(session, estate, "Retail", mode=ENFORCE)
    markets = await _workspace(session, estate, "Markets", mode=ENFORCE)
    await _bind(session, estate, retail, shared)
    await _bind(session, estate, markets, shared)

    scope = await unresolved_scope(session, organization_id=estate.organization.id)

    assert (scope.unbound, scope.ambiguous) == (0, 1)
    assert scope.datasources[0].reason_code == WORKSPACE_AMBIGUOUS
    assert scope.datasources[0].live_bindings == 2


async def test_an_expired_binding_does_not_count_as_live(session: AsyncSession) -> None:
    """The report mirrors `resolve_workspace`'s own liveness rule. A binding
    that expired last night leaves its datasource unresolvable this morning,
    and a readiness report that still counted it would say the estate is ready
    on the morning it stopped being so."""
    estate = await _estate(session)
    source = await _datasource(session, estate, "retail-warehouse")
    retail = await _workspace(session, estate, "Retail", mode=ENFORCE)
    binding = await _bind(session, estate, retail, source)
    binding.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()

    scope = await unresolved_scope(session, organization_id=estate.organization.id)

    assert scope.unbound == 1
    assert scope.resolvable == 0


async def test_the_blocker_list_is_capped_and_says_so(session: AsyncSession) -> None:
    """A large estate degrades to "the first N blockers", never to an unbounded
    response -- but the counts stay true, because an operator deciding whether
    to flip needs the real size even when the list is clipped."""
    estate = await _estate(session)
    for index in range(5):
        await _datasource(session, estate, f"orphan-{index}")

    scope = await unresolved_scope(session, organization_id=estate.organization.id, limit=2)

    assert scope.unbound == 5
    assert len(scope.datasources) == 2
    assert scope.truncated


# --------------------------------------------------------------------------- #
# 3. D9 -- the operator surface, driven through the real handler
# --------------------------------------------------------------------------- #


async def _readiness(session: AsyncSession, estate: Estate, **kwargs: object) -> object:
    return await get_enforcement_readiness(
        organization_id=estate.organization.id,
        window_days=int(kwargs.get("window_days", 7)),
        limit=int(kwargs.get("limit", 100)),
        context=security_context(
            organization_id=estate.organization.id,
            principal_id="operator",
            roles=frozenset({"OrganizationAdmin"}),
        ),
        session=session,
    )


async def test_readiness_names_every_blocker_it_found(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of D9: not a score, a list of things to fix. An estate
    with an orphaned datasource, an ambiguous one and a SHADOW workspace must
    say all three, because fixing one of them and re-reading a single number
    would look like progress while two blockers remained."""
    estate = await _estate(session)
    await _datasource(session, estate, "orphan-warehouse")
    shared = await _datasource(session, estate, "shared-warehouse")
    retail = await _workspace(session, estate, "Retail", mode=SHADOW)
    markets = await _workspace(session, estate, "Markets", mode=ENFORCE)
    await _bind(session, estate, retail, shared)
    await _bind(session, estate, markets, shared)

    report = await _readiness(session, estate)

    assert report.ready is False  # type: ignore[attr-defined]
    blockers = " | ".join(report.blockers)  # type: ignore[attr-defined]
    assert "no live source binding" in blockers
    assert "more than one live source binding" in blockers
    assert "still in SHADOW" in blockers
    assert report.datasources_unbound == 1  # type: ignore[attr-defined]
    assert report.datasources_ambiguous == 1  # type: ignore[attr-defined]
    assert report.workspaces_observing == 1  # type: ignore[attr-defined]
    assert report.workspaces_enforcing == 1  # type: ignore[attr-defined]


async def test_readiness_surfaces_the_divergence_a_shadow_workspace_recorded(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The traffic half, end to end: a SHADOW workspace lets a non-member's
    query through, records that ENFORCE would have refused it, and the operator
    surface reports that as a would-be denial against a named workspace. This
    is the evidence `Settings.workspace_authorization_posture` tells an
    operator to read, and until now nothing could read it."""
    estate = await _estate(session)
    source = await _datasource(session, estate, "retail-warehouse")
    retail = await _workspace(session, estate, "Retail", mode=SHADOW, members=("alice",))
    await _bind(session, estate, retail, source)
    _patch_executor(monkeypatch)

    # Carol is not a member. SHADOW means this succeeds and is written down.
    assert await _execute(session, estate, source, principal_id="carol") is not None

    report = await _readiness(session, estate)

    rows = {row.name: row for row in report.workspaces}  # type: ignore[attr-defined]
    assert rows["Retail"].would_be_denials >= 1
    assert rows["Retail"].distinct_principals_affected == 1
    assert rows["Retail"].ready is False
    assert rows["Retail"].top_reason_codes
    assert any("would-be denial" in blocker for blocker in report.blockers)  # type: ignore[attr-defined]


async def test_a_clean_enforcing_estate_still_reports_the_posture_blocker(
    session: AsyncSession,
) -> None:
    """Honest by construction. Every workspace enforcing and every datasource
    resolvable is still not "ready" while unresolved-workspace requests proceed
    undecided, because a request that names nothing bypasses all of it. The
    report says so rather than rounding up to green."""
    estate = await _estate(session)
    source = await _datasource(session, estate, "retail-warehouse")
    retail = await _workspace(session, estate, "Retail", mode=ENFORCE, members=("alice",))
    await _bind(session, estate, retail, source)

    report = await _readiness(session, estate)

    assert report.datasources_unbound == 0  # type: ignore[attr-defined]
    assert report.datasources_ambiguous == 0  # type: ignore[attr-defined]
    assert report.workspaces_observing == 0  # type: ignore[attr-defined]
    assert report.ready is False  # type: ignore[attr-defined]
    assert report.blockers == [  # type: ignore[attr-defined]
        "unresolved-workspace requests still proceed undecided "
        "(unresolved_workspace_posture is not DENY)"
    ]


async def test_readiness_refuses_another_organizations_id(session: AsyncSession) -> None:
    """The report names datasources and workspaces. An admin of one tenant
    asking for another's must be refused before any of that is read."""
    estate = await _estate(session)
    other = await _estate(session)

    with pytest.raises(Exception) as excinfo:
        await get_enforcement_readiness(
            organization_id=other.organization.id,
            window_days=7,
            limit=100,
            context=security_context(
                organization_id=estate.organization.id,
                principal_id="operator",
                roles=frozenset({"OrganizationAdmin"}),
            ),
            session=session,
        )

    assert getattr(excinfo.value, "status_code", None) in {403, 404}
