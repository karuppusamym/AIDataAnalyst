"""The authorization decision is wired into the paths that need it, and stays wired.

For several commits this platform had a complete attribute-based authorization system
-- a policy engine, workspace membership, expiring source bindings, rule-derived roles,
shadow mode -- and not one line of production traffic that reached any of it. Every test
passed. The accomplishment log said so in as many words. That gap is the specific thing
this module exists to make impossible to reintroduce.

Two halves, and both are needed:

* **Static.** The gate is reachable from the execution choke point and from the gated
  read handlers. A scan, because the alternative is a behavioural test per surface, and
  the surface somebody adds next week is exactly the one that would have no test.
* **Behavioural.** The gate decides what it claims to: resolution ignores who is asking,
  an unresolved workspace is its own state rather than a quiet allow, a SHADOW workspace
  proceeds and records, an ENFORCE workspace refuses.

The static half would pass against a `gate` that returned `True` unconditionally; the
behavioural half would pass against a perfect gate nothing called. Neither is worth much
alone.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401
from aida.authorization_gate import (
    AuthorizationDenied,
    gate,
    principal_kind_of,
)
from aida.db import Base
from aida.models import (
    AccessPolicy,
    AuthorizationShadowRecord,
    Organization,
    SourceBinding,
    Workspace,
    WorkspaceAccessRule,
)
from aida.security_types import SecurityContext
from aida.workspace_access import ENFORCE
from aida.workspace_resolution import (
    NO_BINDING_FOR_DATASOURCE,
    RESOLVED_EXPLICIT,
    RESOLVED_SOLE_BINDING,
    WORKSPACE_AMBIGUOUS,
    WORKSPACE_NOT_SPECIFIED,
    resolve_workspace,
)
from atlas.platform.config import Settings
from tests.support.app_surface import NON_GOVERNED_WRITERS, reaches_call

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

# Every name that means "a decision was taken". `gate` is the one surfaces call;
# `authorize_enforced` is what it calls; `authorize` is the decision itself. The scan
# accepts any of the three so that a surface which legitimately reaches the decision by
# a different route is not reported as ungated.
_GATE_CALLS = frozenset({"gate", "authorize_enforced", "authorize"})

# Modules allowed to call `authorize` without going through `authorize_enforced`.
# `workspace_api` hosts the authorization probe, which deliberately wants the
# unmodulated decision -- that is the endpoint's whole purpose. Anything else calling it
# would enforce against workspaces that are still in shadow mode.
_DIRECT_AUTHORIZE_CALLERS = frozenset({"aida.workspace_service", "aida.workspace_api"})


# --- static: the gate is reachable where it must be -------------------------


def test_the_execution_path_is_gated() -> None:
    """INV-2 says the gateway is the only way to a warehouse. This says it decides.

    Asserted on `execute` rather than on the four callers (two HTTP handlers, the MCP
    tool surface, the agent orchestrator) on purpose: gating callers would mean the
    gate is present on the ones somebody remembered.
    """
    assert reaches_call("aida.query_gateway", "QueryExecutionGateway.execute", _GATE_CALLS)


def test_the_validation_path_is_gated() -> None:
    """Validation returns table names, findings and a cost estimate. That is metadata a
    caller may not be entitled to, even though it is never a row."""
    assert reaches_call("aida.query_gateway", "QueryExecutionGateway.validate", _GATE_CALLS)


@pytest.mark.parametrize(
    "handler",
    ["list_tables", "list_columns", "list_constraints", "get_latest_table_profile"],
)
def test_the_catalog_read_handlers_are_gated(handler: str) -> None:
    assert reaches_call("aida.api", handler, _GATE_CALLS)


def test_the_retrieval_preview_is_gated() -> None:
    """The assembled context an agent would be handed, so `CONSUME_CONTEXT` rather than
    `READ_METADATA`."""
    assert reaches_call("aida.api", "preview_agent_retrieval", _GATE_CALLS)


def test_the_scan_would_notice_if_a_gate_were_removed() -> None:
    """The meta-test. Every assertion above is worth exactly what this one is.

    A scan that answered True for everything would make this module a decoration, and
    that failure is silent: the tests keep passing while the property they describe
    stops holding. `get_query_lineage` is a genuine read handler on the same module,
    with a tenancy check and no authorization gate, so a scan that cannot tell it apart
    from a gated handler is broken.

    If that handler is ever gated -- and it arguably should be, since query lineage
    describes a statement someone else may have run -- this test fails, and the fix is
    to point it at another ungated read rather than to delete it. Losing the
    discriminator would leave the rest of this module unable to fail.
    """
    assert not reaches_call("aida.api", "get_query_lineage", _GATE_CALLS)


def test_no_surface_calls_authorize_directly() -> None:
    """`authorize` ignores the workspace's enforcement mode, so a surface calling it
    enforces against workspaces the ADR-0018 migration left with no members at all.

    The probe endpoint is the deliberate exception, and the exception is per module
    rather than blanket so that adding a second direct caller is a visible act.
    """
    import ast
    import pathlib

    import aida

    root = pathlib.Path(aida.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        module = "aida." + ".".join(path.relative_to(root).with_suffix("").parts)
        module = module.removesuffix(".__init__")
        if module in _DIRECT_AUTHORIZE_CALLERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Both spellings. Matching only the bare name would leave
            # `workspace_service.authorize(...)` as a one-import bypass of the rule
            # this test exists to state.
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called == "authorize":
                offenders.append(f"{module}:{node.lineno}")
    assert offenders == [], (
        "these call `authorize` directly and so bypass shadow mode; call "
        f"`authorize_enforced` or `gate` instead: {offenders}"
    )


# --- fixtures ---------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # StaticPool because the durable shadow-record path opens a *second* session, and
    # every connection to `sqlite:///:memory:` otherwise gets a database of its own --
    # the row would be written to a second, empty database and the assertion would fail
    # for a reason that has nothing to do with the code under test.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="rbac-parity",
            name="parity",
            effect="ALLOW",
            subject_match={"roles": ["Analyst"]},
            action_match=[],
            created_by="seed",
        )
    )
    await session.flush()
    return org


def _datasource_id() -> UUID:
    """A datasource identity, not a row.

    Nothing on this path loads the datasource -- resolution and the gate take its id
    and join through `source_binding` -- so building a real `DataSource` here would
    mean seeding a line of business, a domain and a project to satisfy three foreign
    keys that have nothing to do with what is being tested.
    """
    return uuid4()


async def _workspace(
    session: AsyncSession, org: Organization, *, mode: str = "SHADOW"
) -> Workspace:
    workspace = Workspace(
        organization_id=org.id,
        name="Migrated",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="p",
        authorization_mode=mode,
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def _bind(
    session: AsyncSession,
    workspace: Workspace,
    datasource_id: UUID,
    *,
    expires_at: datetime | None = None,
) -> SourceBinding:
    binding = SourceBinding(
        organization_id=workspace.organization_id,
        workspace_id=workspace.id,
        datasource_id=datasource_id,
        purpose="grandfathered",
        status="ACTIVE",
        requested_by="migration",
        expires_at=expires_at,
    )
    session.add(binding)
    await session.flush()
    return binding


def _context(org: Organization, principal: str = "alice") -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Analyst"}),
    )


def _settings(posture: str = "SHADOW") -> Settings:
    return Settings(_env_file=None, unresolved_workspace_posture=posture)


# --- behaviour: resolution ignores the caller -------------------------------


async def test_resolution_gives_the_same_answer_to_every_principal(
    session: AsyncSession,
) -> None:
    """The property that stops resolution from being self-fulfilling.

    Resolving by "which workspace does this principal have access to" would pick the
    workspace by the answer and then ask the question, which is not a check. Two
    principals with nothing in common must resolve identically.
    """
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org)
    await _bind(session, workspace, datasource_id)

    first = await resolve_workspace(
        session, organization_id=org.id, datasource_id=datasource_id, now=_NOW
    )
    second = await resolve_workspace(
        session, organization_id=org.id, datasource_id=datasource_id, now=_NOW
    )
    assert first.workspace_id == second.workspace_id == workspace.id
    assert first.reason_code == RESOLVED_SOLE_BINDING


async def test_an_explicit_workspace_wins_over_the_binding(session: AsyncSession) -> None:
    org = await _org(session)
    datasource_id = _datasource_id()
    bound = await _workspace(session, org)
    await _bind(session, bound, datasource_id)
    named = await _workspace(session, org)

    resolution = await resolve_workspace(
        session,
        organization_id=org.id,
        requested_workspace_id=named.id,
        datasource_id=datasource_id,
        now=_NOW,
    )
    assert resolution.workspace_id == named.id
    assert resolution.reason_code == RESOLVED_EXPLICIT


async def test_two_live_bindings_are_ambiguous_rather_than_guessed(
    session: AsyncSession,
) -> None:
    """The case that makes "just derive it" wrong in general.

    Two workspaces legitimately sharing one warehouse is the situation source bindings
    exist to express. Picking either one would evaluate the request against a grant the
    caller did not invoke.
    """
    org = await _org(session)
    datasource_id = _datasource_id()
    await _bind(session, await _workspace(session, org), datasource_id)
    await _bind(session, await _workspace(session, org), datasource_id)

    resolution = await resolve_workspace(
        session, organization_id=org.id, datasource_id=datasource_id, now=_NOW
    )
    assert resolution.workspace_id is None
    assert resolution.reason_code == WORKSPACE_AMBIGUOUS


async def test_an_expired_binding_does_not_resolve(session: AsyncSession) -> None:
    """Expiry is the mechanism that stops entitlement creep; a resolver that ignored it
    would quietly restore every lapsed grant."""
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org)
    await _bind(session, workspace, datasource_id, expires_at=_NOW - timedelta(days=1))

    resolution = await resolve_workspace(
        session, organization_id=org.id, datasource_id=datasource_id, now=_NOW
    )
    assert resolution.reason_code == NO_BINDING_FOR_DATASOURCE


async def test_a_request_naming_no_workspace_and_no_datasource_resolves_to_nothing(
    session: AsyncSession,
) -> None:
    resolution = await resolve_workspace(session, organization_id=uuid4(), now=_NOW)
    assert resolution.workspace_id is None
    assert resolution.reason_code == WORKSPACE_NOT_SPECIFIED


# --- behaviour: the gate ----------------------------------------------------


async def test_an_unresolved_workspace_proceeds_under_the_shadow_posture(
    session: AsyncSession,
) -> None:
    """Today's default, and the reason wiring the gate was a non-event.

    `decided=False` is the honest part: the request proceeded, and no authorization
    decision was reached about it. A gate returning a bare `allowed=True` here would be
    claiming a check it did not perform.
    """
    org = await _org(session)
    datasource_id = _datasource_id()
    outcome = await gate(
        session,
        _context(org),
        settings=_settings("SHADOW"),
        action="READ_DATA",
        resource_type="datasource",
        resource_id=str(datasource_id),
        datasource_id=datasource_id,
        now=_NOW,
    )
    assert outcome.decided is False
    assert outcome.workspace_id is None
    assert outcome.reason_code == NO_BINDING_FOR_DATASOURCE


async def test_an_unresolved_workspace_is_refused_under_the_deny_posture(
    session: AsyncSession,
) -> None:
    """One setting, no code change. This is what finishing the rollout looks like."""
    org = await _org(session)
    datasource_id = _datasource_id()
    with pytest.raises(AuthorizationDenied) as refusal:
        await gate(
            session,
            _context(org),
            settings=_settings("DENY"),
            action="READ_DATA",
            resource_type="datasource",
            resource_id=str(datasource_id),
            datasource_id=datasource_id,
            now=_NOW,
        )
    assert refusal.value.reason_code == NO_BINDING_FOR_DATASOURCE


async def test_a_shadow_workspace_allows_and_records_what_it_would_have_denied(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole rollout strategy, in one assertion.

    The workspace has no members and no access rule, so the correct decision is a
    denial. Because it is in SHADOW, the request proceeds and the denial is written
    down instead -- which is the evidence a human reads before flipping it to ENFORCE.
    """
    monkeypatch.setattr("aida.workspace_access.session_factory", session.info["maker"])
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org, mode="SHADOW")
    await _bind(session, workspace, datasource_id)
    await session.commit()

    outcome = await gate(
        session,
        _context(org),
        settings=_settings("SHADOW"),
        action="READ_DATA",
        resource_type="datasource",
        resource_id=str(datasource_id),
        datasource_id=datasource_id,
        now=_NOW,
    )
    assert outcome.decided is True
    assert outcome.workspace_id == workspace.id

    records = (await session.scalars(select(AuthorizationShadowRecord))).all()
    assert len(records) == 1
    assert records[0].reason_code == "NO_WORKSPACE_MEMBERSHIP"
    assert records[0].shadow_allowed is False


async def test_a_divergence_survives_a_read_that_never_commits(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the shadow record gets a transaction of its own.

    A read handler never commits and a rejected execution rolls back, so a divergence
    written into the caller's session would be discarded exactly when it is most
    interesting. That would bias the readiness report towards agreement -- and the
    readiness report is what a human uses to decide the workspace is safe to enforce.
    """
    monkeypatch.setattr("aida.workspace_access.session_factory", session.info["maker"])
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org, mode="SHADOW")
    await _bind(session, workspace, datasource_id)
    await session.commit()

    await gate(
        session,
        _context(org),
        settings=_settings("SHADOW"),
        action="READ_METADATA",
        resource_type="datasource",
        resource_id=str(datasource_id),
        datasource_id=datasource_id,
        now=_NOW,
    )
    await session.rollback()

    records = (await session.scalars(select(AuthorizationShadowRecord))).all()
    assert len(records) == 1


async def test_an_enforcing_workspace_refuses(session: AsyncSession) -> None:
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org, mode=ENFORCE)
    await _bind(session, workspace, datasource_id)

    with pytest.raises(AuthorizationDenied) as refusal:
        await gate(
            session,
            _context(org),
            settings=_settings("SHADOW"),
            action="READ_DATA",
            resource_type="datasource",
            resource_id=str(datasource_id),
            datasource_id=datasource_id,
            now=_NOW,
        )
    assert refusal.value.reason_code == "NO_WORKSPACE_MEMBERSHIP"
    assert refusal.value.workspace_id == workspace.id


async def test_an_enforcing_workspace_allows_a_principal_a_rule_covers(
    session: AsyncSession,
) -> None:
    """The other half of the same mechanism: enforcement that permits the right people.

    A test that only proved the gate can deny would be satisfied by a gate that denies
    everything, which is the second way to take a platform down.
    """
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org, mode=ENFORCE)
    await _bind(session, workspace, datasource_id)
    session.add(
        WorkspaceAccessRule(
            organization_id=org.id,
            code="seed-analyst",
            subject_role="Analyst",
            workspace_role="analyst",
            created_by="migration",
        )
    )
    await session.flush()

    outcome = await gate(
        session,
        _context(org),
        settings=_settings("SHADOW"),
        action="READ_DATA",
        resource_type="datasource",
        resource_id=str(datasource_id),
        datasource_id=datasource_id,
        now=_NOW,
    )
    assert outcome.decided is True
    assert outcome.workspace_id == workspace.id


async def test_a_missing_tenant_claim_refuses_to_resolve(session: AsyncSession) -> None:
    """INV-5. An absent organization is not a licence to search every organization's
    bindings for a match, and the development identity provider makes it reachable."""
    org = await _org(session)
    datasource_id = _datasource_id()
    await _bind(session, await _workspace(session, org), datasource_id)

    resolution = await resolve_workspace(
        session, organization_id=None, datasource_id=datasource_id, now=_NOW
    )
    assert resolution.workspace_id is None
    assert resolution.reason_code == "NO_ORGANIZATION_CONTEXT"


# --- the subject kind -------------------------------------------------------


@pytest.mark.parametrize(
    ("principal_type", "expected"),
    [("USER", "HUMAN"), ("agent", "AGENT"), ("SERVICE", "SERVICE"), ("robot", "SERVICE")],
)
def test_an_unknown_principal_type_lands_in_the_most_constrained_kind(
    principal_type: str, expected: str
) -> None:
    """A principal type nobody has classified yet must not inherit a person's
    permissions on its way through the identity layer."""
    context = SecurityContext(
        principal_id="p",
        principal_type=principal_type,
        organization_id=uuid4(),
        roles=frozenset(),
    )
    assert principal_kind_of(context) == expected


# --- the exclusion in the mutation scan stays honest ------------------------


async def test_the_excluded_writers_only_ever_write_attributable_shadow_records(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backs the `NON_GOVERNED_WRITERS` exclusion in the INV-7 mutation scan.

    That exclusion says these two functions do not mutate governed state, and it is
    what stops every gated read from being classified as a write. If it stopped being
    true -- if either function grew a second `session.add` -- INV-7 would go quiet
    about a real mutation. So the claim is asserted rather than trusted: after a
    divergence, exactly one row exists, it is a shadow record, and it names who did
    what and why.
    """
    assert NON_GOVERNED_WRITERS == {"record_divergence", "record_divergence_durably"}
    monkeypatch.setattr("aida.workspace_access.session_factory", session.info["maker"])
    org = await _org(session)
    datasource_id = _datasource_id()
    workspace = await _workspace(session, org, mode="SHADOW")
    await _bind(session, workspace, datasource_id)
    await session.commit()

    await gate(
        session,
        _context(org, principal="carol"),
        settings=_settings("SHADOW"),
        action="READ_DATA",
        resource_type="datasource",
        resource_id=str(datasource_id),
        datasource_id=datasource_id,
        now=_NOW,
    )
    record = (await session.scalars(select(AuthorizationShadowRecord))).one()
    assert record.principal_id == "carol"
    assert record.principal_kind == "HUMAN"
    assert record.action == "READ_DATA"
    assert record.resource_type == "datasource"
    assert record.resource_id == str(datasource_id)
    assert record.reason_code
    assert isinstance(record.workspace_id, UUID)
