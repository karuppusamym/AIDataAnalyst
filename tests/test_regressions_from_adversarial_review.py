"""Regression tests for three defects found by adversarially probing this codebase.

Each of these passed review, passed the type checker, and shipped inside a green
suite of 575 tests. They were found by deliberately trying to break the code rather
than by confirming it worked, which is the only way this class of defect surfaces.

* **Fail-open tenant isolation.** `authorize` skipped the cross-organization check for
  a caller that claimed no organization at all, written as
  `if context.organization_id is not None and <mismatch>`. Development identity makes
  the organization header optional, so `None` was reachable from outside. This is an
  INV-5 violation and the most serious of the three.
* **Allowlist matched on the wrong key.** The vector index filtered candidates by
  `owner_id` alone, but `owner_id` is unique only within an `owner_type` -- so an
  allowlist authorising a TABLE would also admit a COLUMN sharing its identifier.
* **Same-instant reassignment violated a constraint.** Superseding an assignment at the
  same timestamp it was created set `effective_to == effective_from` and then inserted a
  row colliding on the unique key. The fix needed timezone normalisation, because
  timestamps read back aware from PostgreSQL and naive from SQLite -- so the naive
  comparison was silently wrong on one backend only.
"""
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401
from aida.business_graph import assign
from aida.config import Settings
from aida.db import Base
from aida.models import Organization
from aida.security_types import SecurityContext
from aida.vector_store import EmbeddingRecord, EmbeddingRef, PostgresBruteForceIndex
from aida.workspace_service import authorize, create_workspace


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def test_a_principal_claiming_no_organization_is_denied(
    session: AsyncSession,
) -> None:
    """A SecurityContext with organization_id=None must not skip tenant isolation.

    Development identity makes X-Organization-Id optional, so None is reachable from
    outside. If the cross-organization check is written as
    `if context.organization_id is not None and ...` then omitting the header skips
    isolation entirely -- fail OPEN, on INV-5.
    """
    org = Organization(name="A", slug=f"a-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p",
        owner_principal="alice",
    )
    ghost = SecurityContext(
        principal_id="alice", principal_type="USER",
        organization_id=None,  # no organization claimed at all
        roles=frozenset({"PlatformAdmin"}),
    )
    result = await authorize(
        session, ghost, workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE",
    )
    assert result.allowed is False
    assert result.reason_code in {"NO_ORGANIZATION_CONTEXT", "CROSS_ORGANIZATION_DENIED"}


async def test_vector_candidates_match_on_owner_type_as_well_as_id(
    session: AsyncSession,
) -> None:
    """An allowlist of ("TABLE", "x") must not admit an embedding of ("COLUMN", "x").

    owner_id is only unique within an owner_type. Filtering on id alone leaks a
    different object that happens to share an identifier -- and the policy filter that
    produced the allowlist authorised the table, not the column.
    """
    org = Organization(name="A", slug=f"a-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    index = PostgresBruteForceIndex(Settings(_env_file=None))
    await index.upsert(
        session, org.id,
        (
            EmbeddingRecord(EmbeddingRef("TABLE", "shared_id"), (1.0, 0.0), "h1"),
            EmbeddingRecord(EmbeddingRef("COLUMN", "shared_id"), (1.0, 0.0), "h2"),
        ),
        signature="sig",
    )
    matches = await index.search(
        session, org.id, (1.0, 0.0), signature="sig",
        candidates=(EmbeddingRef("TABLE", "shared_id"),), limit=10,
    )
    assert [m.ref.owner_type for m in matches] == ["TABLE"]


async def test_reassigning_at_the_same_instant_is_idempotent(
    session: AsyncSession,
) -> None:
    """Two assignments at the same `as_of` collide on (node, target, effective_from).

    Reachable whenever a caller supplies an explicit timestamp, or when two writes land
    inside the same clock tick.
    """
    from aida.models import BusinessNode

    org = Organization(name="A", slug=f"a-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    node = BusinessNode(organization_id=org.id, kind="LOB", name="R", code="LOB:R")
    session.add(node)
    await session.flush()
    from datetime import UTC, datetime

    moment = datetime(2026, 8, 30, tzinfo=UTC)
    await assign(session, organization_id=org.id, business_node_id=node.id,
                 target_type="TABLE", target_id="t", assigned_by="s", as_of=moment)
    await assign(session, organization_id=org.id, business_node_id=node.id,
                 target_type="TABLE", target_id="t", assigned_by="s", as_of=moment)
