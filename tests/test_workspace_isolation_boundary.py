"""R11-X2: a workspace cannot be created against an isolation boundary that
does not exist.

`Workspace.isolation_boundary_id` is a real foreign key with
`ondelete=RESTRICT`, and the create route forwarded whatever the caller sent
straight into it. Nothing in this platform ever inserts an `IsolationBoundary`
row -- the table has a model, an FK pointing at it and no writer anywhere -- so
**every** non-NULL value a client could send was a guaranteed IntegrityError,
surfacing as a server fault rather than as a refusal naming the bad field.

Found by re-running the unused-table analysis rather than by anything failing:
the table is unused, which is dull, but the column referencing it is reachable
from a public API, which is not. Reachable and unusable is a worse state than
unused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.db import Base
from aida.models import IsolationBoundary, Organization
from atlas.modules.identity_tenancy.router import create_workspace_route
from atlas.modules.identity_tenancy.schemas import WorkspaceCreate
from tests.support.doubles import security_context


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
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    return organization


def _body(**overrides: object) -> WorkspaceCreate:
    defaults: dict[str, object] = {
        "name": "Governed analytics",
        "slug": f"ws-{uuid4().hex[:8]}",
        "purpose": "analysis",
    }
    defaults.update(overrides)
    return WorkspaceCreate(**defaults)  # type: ignore[arg-type]


async def _create(session: AsyncSession, organization: Organization, body: WorkspaceCreate):
    return await create_workspace_route(
        organization_id=organization.id,
        body=body,
        context=security_context(
            organization_id=organization.id,
            principal_id="admin",
            roles=frozenset({"OrganizationAdmin"}),
        ),
        session=session,
        correlation_id="corr-isolation-boundary",
    )


async def test_an_unknown_isolation_boundary_is_refused_with_422(session: AsyncSession) -> None:
    """A 422 naming the field, not an IntegrityError shaped like a server fault.

    The caller's request is wrong and can be corrected; telling them that is
    the difference between an API and a database error message.
    """
    organization = await _organization(session)

    with pytest.raises(HTTPException) as excinfo:
        await _create(session, organization, _body(isolation_boundary_id=uuid4()))

    assert excinfo.value.status_code == 422
    assert "isolation_boundary_id" in str(excinfo.value.detail)


async def test_another_organizations_boundary_is_not_usable(session: AsyncSession) -> None:
    """The check is tenant-scoped, which is the whole point of the field.

    An isolation boundary is a hard wall between tenants' workspaces. Accepting
    another organization's boundary id would let a caller attach their
    workspace to a wall somebody else owns -- so this must fail for the same
    reason an unknown id does, and not merely because the row is missing.
    """
    organization = await _organization(session)
    other = await _organization(session)
    boundary = IsolationBoundary(
        id=uuid4(),
        organization_id=other.id,
        name="Other bank ringfence",
        code=f"RING{uuid4().hex[:4].upper()}",
    )
    session.add(boundary)
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await _create(session, organization, _body(isolation_boundary_id=boundary.id))

    assert excinfo.value.status_code == 422


async def test_a_boundary_in_this_organization_is_accepted(session: AsyncSession) -> None:
    """The positive control, so the check above is a validation and not a ban.

    It also documents what the field needs in order to become usable: a row.
    Nothing in the platform creates one yet, which is why R11-X2 keeps this
    table open rather than treating the validation as the whole fix.
    """
    organization = await _organization(session)
    boundary = IsolationBoundary(
        id=uuid4(),
        organization_id=organization.id,
        name="Retail ringfence",
        code=f"RING{uuid4().hex[:4].upper()}",
    )
    session.add(boundary)
    await session.flush()

    workspace = await _create(session, organization, _body(isolation_boundary_id=boundary.id))

    assert workspace.isolation_boundary_id == boundary.id


async def test_no_boundary_is_the_ordinary_case(session: AsyncSession) -> None:
    """Most workspaces need no hard wall, and the model's own docstring says
    the column is normally NULL -- so the common path must not have acquired a
    new way to fail."""
    organization = await _organization(session)

    workspace = await _create(session, organization, _body())

    assert workspace.isolation_boundary_id is None
