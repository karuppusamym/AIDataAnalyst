"""PG-5 exit-condition tests: edition entitlement evaluation.

Two halves, mirroring `tests/test_multi_table_blueprint.py`'s split:

* Pure `evaluate_entitlement` tests -- no database, no settings object, no
  FastAPI. Prove the ALLOW/DENY boundary at every edition, that a higher
  edition never loses a capability a lower edition already had (monotonic),
  that the reason names both the required and the actual edition, and that
  an unregistered capability id fails closed rather than silently passing.
* Real (in-memory sqlite) database integration tests against the two
  currently-ungated Enterprise-tier endpoints PG-5 wired the check into:
  `aida.tool_api.create_multi_table_tool_blueprint` (SM-5) and
  `aida.tool_plans_api.create_tool_plan` / `execute_tool_plan` -- proving a
  Foundation-edition organization is denied with a value-free 403 and an
  Enterprise-edition organization is allowed through unchanged.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.edition_entitlements import (
    ALLOWED,
    CAPABILITY_MIN_EDITION,
    DENIED_CAPABILITY_UNREGISTERED,
    DENIED_EDITION_INSUFFICIENT,
    evaluate_entitlement,
)
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
)
from aida.tool_api import MultiTableToolBlueprintRequest, create_multi_table_tool_blueprint
from aida.tool_plans_api import (
    PlanBudgetCreate,
    PlanStepCreate,
    ToolPlanCreate,
    create_tool_plan,
    execute_tool_plan,
)
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# Pure evaluator: no database, no I/O.
# ---------------------------------------------------------------------------


def test_foundation_organization_is_denied_an_enterprise_only_capability() -> None:
    decision = evaluate_entitlement(
        organization_edition="FOUNDATION", capability="multi_step_tool_plans"
    )
    assert decision.allowed is False
    assert decision.reason_code == DENIED_EDITION_INSUFFICIENT
    assert decision.organization_edition == "FOUNDATION"
    assert decision.required_edition == "ENTERPRISE"
    assert decision.capability == "multi_step_tool_plans"


def test_enterprise_organization_is_allowed_an_enterprise_capability() -> None:
    decision = evaluate_entitlement(
        organization_edition="ENTERPRISE", capability="multi_step_tool_plans"
    )
    assert decision.allowed is True
    assert decision.reason_code == ALLOWED
    assert decision.required_edition == "ENTERPRISE"


def test_regulated_organization_is_allowed_every_registered_capability() -> None:
    """The ceiling edition can use everything the matrix registers -- no
    capability in the doc's table is Regulated-exclusive-and-then-some."""
    for capability in CAPABILITY_MIN_EDITION:
        decision = evaluate_entitlement(organization_edition="REGULATED", capability=capability)
        assert decision.allowed is True, capability


def test_foundation_organization_is_allowed_a_foundation_capability() -> None:
    decision = evaluate_entitlement(
        organization_edition="FOUNDATION", capability="governed_tool_registry"
    )
    assert decision.allowed is True
    assert decision.reason_code == ALLOWED
    assert decision.required_edition == "FOUNDATION"


def test_edition_gate_is_monotonic() -> None:
    """A capability ALLOWed at one edition stays ALLOWed at every edition
    ranked at or above it -- an edition upgrade never revokes a capability
    a lower edition already had."""
    editions: list[str] = ["FOUNDATION", "ENTERPRISE", "REGULATED"]
    for capability in CAPABILITY_MIN_EDITION:
        seen_allowed = False
        for edition in editions:  # ranked low to high
            decision = evaluate_entitlement(organization_edition=edition, capability=capability)  # type: ignore[arg-type]
            if seen_allowed:
                assert decision.allowed is True, (capability, edition)
            seen_allowed = seen_allowed or decision.allowed


def test_unregistered_capability_fails_closed() -> None:
    decision = evaluate_entitlement(
        organization_edition="REGULATED", capability="not_a_real_capability"
    )
    assert decision.allowed is False
    assert decision.reason_code == DENIED_CAPABILITY_UNREGISTERED
    assert decision.required_edition is None


def test_decision_snapshot_carries_no_more_than_the_closed_vocabulary() -> None:
    """INV-6 discipline: the snapshot is exactly the dataclass fields --
    capability id and two edition names, both closed vocabulary defined in
    this module -- never anything resource- or request-derived."""
    decision = evaluate_entitlement(
        organization_edition="FOUNDATION", capability="mcp_context_products"
    )
    assert set(decision.snapshot()) == {
        "allowed",
        "capability",
        "reason_code",
        "organization_edition",
        "required_edition",
    }


def test_default_settings_edition_does_not_restrict_existing_behaviour() -> None:
    """`Settings.edition` defaults to the ceiling so this field's mere
    existence changes nothing for a deployment that has not configured it --
    the same non-event property `authorization_gate.py` documents for its
    own rollout posture."""
    settings = Settings()
    for capability in CAPABILITY_MIN_EDITION:
        decision = evaluate_entitlement(
            organization_edition=settings.edition, capability=capability
        )
        assert decision.allowed is True, capability


# ---------------------------------------------------------------------------
# Integration: real in-memory database, real endpoints.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Org:
    """One organization with a project and datasource -- enough scaffolding
    for both the tool-plan endpoints (organization only) and the multi-table
    blueprint endpoint (project + datasource + a resolvable-but-irrelevant
    table set is not needed here, since the entitlement check runs before
    any table resolution)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Org":
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Commerce",
            code="COMMERCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Retail Platform",
            slug="retail-platform",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="retail-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://retail-warehouse",
        )
        db.add(self.datasource)
        await db.flush()
        return self

    def maker(self, roles: frozenset[str] = frozenset({"ToolDeveloper"})) -> object:
        return security_context(organization_id=self.organization.id, roles=roles)


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> _Org:
    return await _Org(db).build()


async def test_multi_table_blueprint_denied_on_foundation_edition(org: _Org) -> None:
    request = MultiTableToolBlueprintRequest(
        slug="customer_orders_lookup",
        name="Customer orders lookup",
        description="Blueprint request that should never reach table resolution.",
        datasource_id=org.datasource.id,
        table_ids=[uuid4(), uuid4()],
        allowed_roles=["Analyst"],
    )

    with pytest.raises(HTTPException) as excinfo:
        await create_multi_table_tool_blueprint(
            org.project.id,
            request,
            context=org.maker(),
            session=org.db,
            settings=Settings(edition="FOUNDATION"),
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == DENIED_EDITION_INSUFFICIENT


async def test_multi_table_blueprint_reaches_table_resolution_on_enterprise_edition(
    org: _Org,
) -> None:
    """Enterprise clears the entitlement gate and reaches real business logic
    -- proven by the request failing for an *unrelated* reason (no declared
    relationship between two random table ids), not by a 403."""
    request = MultiTableToolBlueprintRequest(
        slug="customer_orders_lookup",
        name="Customer orders lookup",
        description="Two table ids with no metadata rows at all.",
        datasource_id=org.datasource.id,
        table_ids=[uuid4(), uuid4()],
        allowed_roles=["Analyst"],
    )

    with pytest.raises(HTTPException) as excinfo:
        await create_multi_table_tool_blueprint(
            org.project.id,
            request,
            context=org.maker(),
            session=org.db,
            settings=Settings(edition="ENTERPRISE"),
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail != DENIED_EDITION_INSUFFICIENT


async def test_create_tool_plan_denied_on_foundation_edition(org: _Org) -> None:
    body = ToolPlanCreate(
        name="Nightly reconciliation",
        steps=[PlanStepCreate(sequence=1, tool_id="tool-a", tool_version="1.0")],
        budget=PlanBudgetCreate(),
    )

    with pytest.raises(HTTPException) as excinfo:
        await create_tool_plan(
            body,
            context=org.maker(),
            session=org.db,
            settings=Settings(edition="FOUNDATION"),
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == DENIED_EDITION_INSUFFICIENT


async def test_create_tool_plan_allowed_on_enterprise_edition(org: _Org) -> None:
    body = ToolPlanCreate(
        name="Nightly reconciliation",
        steps=[PlanStepCreate(sequence=1, tool_id="tool-a", tool_version="1.0")],
        budget=PlanBudgetCreate(),
    )

    created = await create_tool_plan(
        body,
        context=org.maker(),
        session=org.db,
        settings=Settings(edition="ENTERPRISE"),
    )
    assert created.name == "Nightly reconciliation"
    assert created.status == "DRAFT"


async def test_execute_tool_plan_denied_on_foundation_edition_even_for_a_pre_existing_plan(
    org: _Org,
) -> None:
    """A plan created under an Enterprise edition must not stay executable
    after the deployment is reconfigured down to Foundation -- the
    entitlement check runs again at execute, not only at create."""
    created = await create_tool_plan(
        ToolPlanCreate(
            name="Nightly reconciliation",
            steps=[PlanStepCreate(sequence=1, tool_id="tool-a", tool_version="1.0")],
            budget=PlanBudgetCreate(),
        ),
        context=org.maker(),
        session=org.db,
        settings=Settings(edition="ENTERPRISE"),
    )

    with pytest.raises(HTTPException) as excinfo:
        await execute_tool_plan(
            created.id,
            context=org.maker(),
            session=org.db,
            settings=Settings(edition="FOUNDATION"),
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == DENIED_EDITION_INSUFFICIENT
