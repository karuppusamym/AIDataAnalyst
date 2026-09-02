"""INV-1: the graph-store setting cannot reach authorization or the classification
roll-up (C7 / ADR-0020's 2026-08-30 amendment).

ADR-0020's amendment is explicit about the boundary: "The setting therefore
governs lineage and graph exploration reads only -- never the authorization scope
query, never the classification roll-up." Two independent proofs, because a
static check alone would pass against code that happens not to import
`aida.graph_store` today but could start deciding on it tomorrow without anyone
noticing, and a behavioural check alone would pass against a fixture that never
actually seeded a per-organization setting row.

**Static.** `aida.authorization_gate` (the ABAC evaluator `gate()` lives in) and
`aida.business_graph` (`rollup()`/`rebuild_rollup()`, the classification/ownership
roll-up) never import `aida.graph_store`, directly or transitively through any
`aida`/`atlas` module either of them imports. A BFS over the real `import`/`from`
statements in `src/`, not a hand-maintained allowlist -- a new import anywhere in
either module's dependency chain that reaches `aida.graph_store` fails this test
immediately.

**Behavioural.** A per-organization `GraphStoreOrganizationSetting` row is seeded
alongside the fixtures `gate()` and `rollup()` actually read, set to each of the
three backends in turn, and both functions are asserted to return byte-identical
outcomes regardless -- proving the setting is not merely unimported but inert to
these two decisions even when a row for the same organization exists in the same
database.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.authorization_gate import AuthorizationDenied, gate
from aida.business_graph import assign, rollup
from aida.db import Base
from aida.graph_store import GraphStoreBackend, GraphStoreOrganizationSetting
from aida.models import (
    AccessPolicy,
    BusinessNode,
    Organization,
    SourceBinding,
    Workspace,
    WorkspaceAccessRule,
)
from aida.security_types import SecurityContext
from aida.workspace_access import ENFORCE
from atlas.platform.config import Settings
from tests.support.app_surface import SRC_ROOT

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_ALL_BACKENDS: tuple[GraphStoreBackend, ...] = ("postgres", "neo4j", "disabled")


# --- static: never imported, directly or transitively -----------------------


def _module_imports(path: object) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))  # type: ignore[attr-defined]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    return imported


def _import_graph() -> dict[str, set[str]]:
    """Every `aida.*`/`atlas.*` module in `src/`, mapped to the `aida.*`/`atlas.*`
    modules it imports (submodule imports collapsed to their top-level package
    entry, e.g. `atlas.platform.db` counts as an edge to `atlas.platform.db`
    exactly, not merely `atlas`)."""
    graph: dict[str, set[str]] = {}
    roots = [SRC_ROOT, SRC_ROOT.parent / "atlas"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(root.parent).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            module = ".".join(parts)
            edges = {
                name
                for name in _module_imports(path)
                if name == "aida" or name == "atlas"
                or name.startswith("aida.") or name.startswith("atlas.")
            }
            graph[module] = edges
    return graph


def _reaches(graph: dict[str, set[str]], start: str, target: str) -> list[str] | None:
    """BFS from `start`; returns the import chain to `target` if one exists, else None.

    An edge like `aida.graph_store` matches a node either exactly or as a prefix
    (`aida.graph_store.submodule`), and a source module's own name is resolved the
    same way against `graph`'s keys so `import aida.foo.bar` reaches the real
    `aida.foo.bar` module entry even though nothing declares that literal key.
    """
    visited = {start}
    queue: list[list[str]] = [[start]]
    while queue:
        path = queue.pop(0)
        current = path[-1]
        for imported in graph.get(current, set()):
            if imported == target or imported.startswith(target + "."):
                return [*path, imported]
            # Resolve to the closest known module entry so the walk can continue
            # past re-export shims and submodule imports.
            next_node = imported if imported in graph else None
            if next_node is None:
                candidates = [m for m in graph if imported.startswith(m + ".")]
                next_node = max(candidates, key=len) if candidates else None
            if next_node is None or next_node in visited:
                continue
            visited.add(next_node)
            queue.append([*path, next_node])
    return None


def test_authorization_gate_never_imports_the_graph_store_port() -> None:
    graph = _import_graph()
    chain = _reaches(graph, "aida.authorization_gate", "aida.graph_store")
    assert chain is None, (
        "aida.authorization_gate can reach aida.graph_store through: "
        f"{' -> '.join(chain or [])} -- INV-1 forbids the graph-store setting from "
        "reaching the authorization decision"
    )


def test_business_graph_never_imports_the_graph_store_port() -> None:
    graph = _import_graph()
    chain = _reaches(graph, "aida.business_graph", "aida.graph_store")
    assert chain is None, (
        "aida.business_graph can reach aida.graph_store through: "
        f"{' -> '.join(chain or [])} -- INV-1 forbids the graph-store setting from "
        "reaching the classification roll-up"
    )


def test_the_import_graph_scan_is_not_vacuous() -> None:
    """Tripwire: if `_import_graph`/`_reaches` stopped parsing real edges, the two
    tests above would pass by examining nothing."""
    graph = _import_graph()
    assert len(graph) > 100, f"only {len(graph)} modules indexed -- the scan is broken"
    assert "aida.graph_store" in graph
    # A known, real, unrelated edge -- proves the walk actually follows imports.
    assert _reaches(graph, "aida.api", "aida.graph_store") is not None


# --- behavioural: seeding the setting changes nothing either function reads -


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


async def _seed_organization(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    session.add(
        AccessPolicy(
            organization_id=org.id, code="rbac-parity", name="parity", effect="ALLOW",
            subject_match={"roles": ["Analyst"]}, action_match=[], created_by="seed",
        )
    )
    await session.flush()
    return org


async def _set_graph_store_backend(
    session: AsyncSession, organization_id: UUID, backend: GraphStoreBackend
) -> None:
    session.add(GraphStoreOrganizationSetting(organization_id=organization_id, backend=backend))
    await session.flush()


async def test_the_classification_rollup_is_identical_across_every_graph_store_backend(
    session: AsyncSession,
) -> None:
    org = await _seed_organization(session)
    lob = BusinessNode(organization_id=org.id, kind="LOB", name="Retail", code="LOB:RTL")
    session.add(lob)
    await session.flush()
    await assign(
        session, organization_id=org.id, business_node_id=lob.id,
        target_type="TABLE", target_id="customers", assigned_by="steward",
    )
    await assign(
        session, organization_id=org.id, business_node_id=lob.id,
        target_type="TABLE", target_id="accounts", assigned_by="steward",
    )

    results = {}
    for backend in _ALL_BACKENDS:
        await _set_graph_store_backend(session, org.id, backend)
        results[backend] = await rollup(session, org.id, lob.id)
        # One row per organization (the setting's own unique constraint) -- clear it
        # so the next backend in the loop can seed its own without violating it.
        await session.execute(
            GraphStoreOrganizationSetting.__table__.delete().where(
                GraphStoreOrganizationSetting.organization_id == org.id
            )
        )
        await session.flush()

    assert results["postgres"] == results["neo4j"] == results["disabled"] == {"TABLE": 2}


async def _workspace(session: AsyncSession, org: Organization, *, mode: str) -> Workspace:
    workspace = Workspace(
        organization_id=org.id, name="Migrated", slug=f"w-{uuid4().hex[:6]}",
        purpose="p", authorization_mode=mode,
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def test_an_authorization_allow_is_identical_across_every_graph_store_backend(
    session: AsyncSession,
) -> None:
    org = await _seed_organization(session)
    datasource_id = uuid4()
    workspace = await _workspace(session, org, mode=ENFORCE)
    session.add(
        SourceBinding(
            organization_id=org.id, workspace_id=workspace.id, datasource_id=datasource_id,
            purpose="grandfathered", status="ACTIVE", requested_by="migration",
        )
    )
    session.add(
        WorkspaceAccessRule(
            organization_id=org.id, code="seed-analyst", subject_role="Analyst",
            workspace_role="analyst", created_by="migration",
        )
    )
    await session.flush()
    context = SecurityContext(
        principal_id="alice", principal_type="USER", organization_id=org.id,
        roles=frozenset({"Analyst"}),
    )

    outcomes = {}
    for backend in _ALL_BACKENDS:
        await _set_graph_store_backend(session, org.id, backend)
        outcomes[backend] = await gate(
            session, context, settings=Settings(_env_file=None),
            action="READ_DATA", resource_type="datasource",
            resource_id=str(datasource_id), datasource_id=datasource_id, now=_NOW,
        )
        await session.execute(
            GraphStoreOrganizationSetting.__table__.delete().where(
                GraphStoreOrganizationSetting.organization_id == org.id
            )
        )
        await session.flush()

    assert all(o.decided is True for o in outcomes.values())
    assert {o.workspace_id for o in outcomes.values()} == {workspace.id}
    assert {o.reason_code for o in outcomes.values()} == {
        outcomes["postgres"].reason_code
    }


async def test_an_authorization_denial_is_identical_across_every_graph_store_backend(
    session: AsyncSession,
) -> None:
    org = await _seed_organization(session)
    datasource_id = uuid4()
    workspace = await _workspace(session, org, mode=ENFORCE)
    session.add(
        SourceBinding(
            organization_id=org.id, workspace_id=workspace.id, datasource_id=datasource_id,
            purpose="grandfathered", status="ACTIVE", requested_by="migration",
        )
    )
    await session.flush()
    context = SecurityContext(
        principal_id="alice", principal_type="USER", organization_id=org.id,
        roles=frozenset({"Analyst"}),
    )

    reason_codes = set()
    for backend in _ALL_BACKENDS:
        await _set_graph_store_backend(session, org.id, backend)
        try:
            await gate(
                session, context, settings=Settings(_env_file=None),
                action="READ_DATA", resource_type="datasource",
                resource_id=str(datasource_id), datasource_id=datasource_id, now=_NOW,
            )
            raise AssertionError(f"expected a denial under backend={backend!r}")
        except AuthorizationDenied as denial:
            reason_codes.add(denial.reason_code)
        await session.execute(
            GraphStoreOrganizationSetting.__table__.delete().where(
                GraphStoreOrganizationSetting.organization_id == org.id
            )
        )
        await session.flush()

    assert reason_codes == {"NO_WORKSPACE_MEMBERSHIP"}
