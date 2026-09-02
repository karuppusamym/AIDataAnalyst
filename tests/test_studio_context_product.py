"""ST-A7: context product builder -- a Studio authoring surface for module 19.

Three layers, mirroring `tests/test_studio_eval.py`'s split for the same
change-set-adjacent shape:

  1. Pure-function coverage of `validate_context_product_contract`
     (`aida.studio`) -- no database, matching `tests/test_studio.py`'s
     existing convention.
  2. Test-harness wiring: `_validate_context_product_item` (via `run_test`)
     now reuses the real `ContextProductDefinition` schema instead of a
     hand-rolled dict-shape check -- the same "reuse the real domain
     contract" property ST-A4 established for TOOL items.
  3. A real (in-memory sqlite) database scenario proving the actual exit
     condition: a validated CONTEXT_PRODUCT change-set item, once its change
     set is submitted, produces a real `ContextProduct`/`ContextProductVersion`
     and routes it through the *existing* module-19 maker-checker queue
     (`decide_governance_review`'s `CONTEXT_PRODUCT_VERSION` branch in
     `semantic_api.py`) -- not a parallel approval path. `create_change_set`/
     `add_item`/`run_tests`/`submit_change_set`/`decide_governance_review` are
     all called directly against one shared session, the same pattern
     `tests/test_studio_eval.py` established for real-write Studio coverage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from itertools import count
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.models import (
    AuditEvent,
    ContextProduct,
    ContextProductVersion,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    StudioChangeSet,
    StudioContextProductMaterialization,
)
from aida.schemas import (
    GovernanceDecisionRequest,
    StudioChangeItemCreate,
    StudioChangeSetCreate,
    StudioContextProductValidateRequest,
)
from aida.semantic_api import decide_governance_review
from aida.studio import ChangeItem, validate_context_product_contract
from aida.studio_api import (
    add_item,
    create_change_set,
    list_context_product_materializations,
    run_tests,
    submit_change_set,
    validate_context_product_contract_endpoint,
)
from aida.studio_test_harness import run_test
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# 1. validate_context_product_contract -- pure, no database
# ---------------------------------------------------------------------------


class TestValidateContextProductContract:
    def _valid_snapshot(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": "Revenue Context",
            "description": "Bounded context for revenue analysis.",
            "purpose": "Approved context for revenue analysis by risk analysts.",
            "owner_type": "GROUP",
            "owner_principal": "revenue-stewards",
            "table_ids": [str(uuid4())],
            "allowed_consumer_roles": ["Analyst"],
        }
        payload.update(overrides)
        return payload

    def test_create_requires_product_key_and_project_id(self) -> None:
        result = validate_context_product_contract(
            operation="CREATE",
            object_id="revenue-context",
            snapshot=self._valid_snapshot(),
        )
        assert result.valid is False
        assert any("product_key" in e for e in result.errors)
        assert any("project_id" in e for e in result.errors)

    def test_create_object_id_must_equal_product_key(self) -> None:
        snapshot = self._valid_snapshot(
            product_key="revenue-context", project_id=str(uuid4())
        )
        result = validate_context_product_contract(
            operation="CREATE", object_id="other-key", snapshot=snapshot
        )
        assert result.valid is False
        assert any("must equal after_snapshot.product_key" in e for e in result.errors)

    def test_create_valid_snapshot_passes_and_strips_key_fields(self) -> None:
        project_id = str(uuid4())
        snapshot = self._valid_snapshot(product_key="revenue-context", project_id=project_id)
        result = validate_context_product_contract(
            operation="CREATE", object_id="revenue-context", snapshot=snapshot
        )
        assert result.valid is True
        assert result.errors == []
        assert result.product_key == "revenue-context"
        assert result.project_id == project_id
        assert result.definition is not None
        assert "product_key" not in result.definition
        assert "project_id" not in result.definition

    def test_create_invalid_project_id_uuid_fails(self) -> None:
        snapshot = self._valid_snapshot(product_key="revenue-context", project_id="not-a-uuid")
        result = validate_context_product_contract(
            operation="CREATE", object_id="revenue-context", snapshot=snapshot
        )
        assert result.valid is False
        assert any("project_id is not a valid UUID" in e for e in result.errors)

    def test_create_reuses_real_pydantic_contract_for_missing_reference(self) -> None:
        """A context product with no governed reference at all is rejected by
        `ContextProductDefinition`'s own validator, not a Studio-side
        duplicate of that rule."""
        snapshot = self._valid_snapshot(product_key="k", project_id=str(uuid4()))
        snapshot["table_ids"] = []
        result = validate_context_product_contract(
            operation="CREATE", object_id="k", snapshot=snapshot
        )
        assert result.valid is False
        assert any("at least one governed reference" in e for e in result.errors)

    def test_create_reuses_real_pydantic_contract_for_bad_owner_type(self) -> None:
        snapshot = self._valid_snapshot(product_key="k", project_id=str(uuid4()))
        snapshot["owner_type"] = "TEAM"  # not INDIVIDUAL/GROUP
        result = validate_context_product_contract(
            operation="CREATE", object_id="k", snapshot=snapshot
        )
        assert result.valid is False
        assert any("owner_type" in e for e in result.errors)

    def test_update_requires_existing_uuid_object_id(self) -> None:
        result = validate_context_product_contract(
            operation="UPDATE", object_id="not-a-uuid", snapshot=self._valid_snapshot()
        )
        assert result.valid is False
        assert any("existing context product UUID" in e for e in result.errors)

    def test_update_valid_snapshot_passes(self) -> None:
        object_id = str(uuid4())
        result = validate_context_product_contract(
            operation="UPDATE", object_id=object_id, snapshot=self._valid_snapshot()
        )
        assert result.valid is True
        assert result.definition is not None
        assert result.product_key is None
        assert result.project_id is None

    def test_delete_requires_no_snapshot_but_needs_valid_uuid(self) -> None:
        good = validate_context_product_contract(
            operation="DELETE", object_id=str(uuid4()), snapshot=None
        )
        assert good.valid is True

        bad = validate_context_product_contract(
            operation="DELETE", object_id="not-a-uuid", snapshot=None
        )
        assert bad.valid is False

    def test_missing_snapshot_fails_for_create_and_update(self) -> None:
        for operation in ("CREATE", "UPDATE"):
            result = validate_context_product_contract(
                operation=operation,  # type: ignore[arg-type]
                object_id=str(uuid4()),
                snapshot=None,
            )
            assert result.valid is False
            assert any("no after_snapshot provided" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 2. Test-harness wiring: _validate_context_product_item via run_test
# ---------------------------------------------------------------------------


def _cp_item(
    *,
    object_id: str,
    operation: str = "CREATE",
    after_snapshot: dict[str, object] | None = None,
) -> ChangeItem:
    return ChangeItem(
        id=uuid4(),
        object_type="CONTEXT_PRODUCT",
        object_id=object_id,
        operation=operation,
        after_snapshot=after_snapshot,
    )


class TestContextProductTestHarnessWiring:
    def test_run_test_dispatches_to_real_contract_validator(self) -> None:
        """A snapshot that the *old* hand-rolled check (name/description/
        purpose/allowed_consumer_roles present) would have accepted, but that
        the real `ContextProductDefinition` contract rejects (purpose too
        short, no governed reference), now fails -- proving the harness
        reuses the real schema rather than a looser duplicate."""
        item = _cp_item(
            object_id="k",
            after_snapshot={
                "product_key": "k",
                "project_id": str(uuid4()),
                "name": "Revenue",
                "description": "d",
                "purpose": "too short",  # < 10 chars, real schema rejects
                "owner_type": "GROUP",
                "owner_principal": "stewards",
                "allowed_consumer_roles": ["Analyst"],
                # no table_ids/semantic_model_version_ids/... at all
            },
        )
        result = run_test(item)
        assert result.passed is False
        assert result.evidence["object_type"] == "CONTEXT_PRODUCT"

    def test_run_test_passes_a_real_valid_definition(self) -> None:
        item = _cp_item(
            object_id="k",
            after_snapshot={
                "product_key": "k",
                "project_id": str(uuid4()),
                "name": "Revenue Context",
                "description": "Bounded context for revenue analysis.",
                "purpose": "Approved context for revenue analysis by risk analysts.",
                "owner_type": "GROUP",
                "owner_principal": "revenue-stewards",
                "table_ids": [str(uuid4())],
                "allowed_consumer_roles": ["Analyst"],
            },
        )
        result = run_test(item)
        assert result.passed is True
        assert result.evidence["definition"]["name"] == "Revenue Context"

    def test_run_test_delete_is_accepted_without_snapshot(self) -> None:
        item = _cp_item(object_id=str(uuid4()), operation="DELETE")
        result = run_test(item)
        assert result.passed is True


# ---------------------------------------------------------------------------
# 3. Real-engine scenario: materialization through the module-19 maker-checker
# ---------------------------------------------------------------------------


_audit_id_counter = count(1)


def _assign_audit_id(mapper: object, connection: object, target: AuditEvent) -> None:
    """Same sqlite BIGINT-PK workaround as `tests/test_studio_eval.py` --
    `record_audit` relies on the database assigning `id`, which sqlite's
    in-memory engine cannot do for a non-single-INTEGER-PK BIGINT column."""
    if target.id is None:
        target.id = next(_audit_id_counter)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    event.listen(AuditEvent, "before_insert", _assign_audit_id)
    try:
        async with maker() as active:
            yield active
    finally:
        event.remove(AuditEvent, "before_insert", _assign_audit_id)
        await engine.dispose()


class _Scenario:
    """Seeds one organization with a project and one ACTIVE table -- the
    minimum a context product needs for a real, resolvable governed
    reference (`ContextProductDefinition.validate_bounded_definition`
    requires at least one)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
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
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()
        return self

    def maker(self) -> object:
        return security_context(
            organization_id=self.organization.id,
            principal_id="maker@example.com",
            roles=frozenset({"DataSteward"}),
        )

    def checker(self) -> object:
        return security_context(
            organization_id=self.organization.id,
            principal_id="checker@example.com",
            roles=frozenset({"Reviewer"}),
        )

    def definition(self) -> dict[str, object]:
        """`ContextProductDefinition` fields only -- the shape an UPDATE
        snapshot must be (no `product_key`/`project_id`; matching
        `ContextProductVersionUpdate` in `context_product_api.py`, which a
        draft's identity and project cannot change through)."""
        return {
            "name": "Revenue Context",
            "description": "Bounded context for revenue analysis.",
            "purpose": "Approved context for revenue analysis by risk analysts.",
            "owner_type": "GROUP",
            "owner_principal": "revenue-stewards",
            "table_ids": [str(self.table.id)],
            "allowed_consumer_roles": ["Analyst"],
        }

    def snapshot(self, *, product_key: str = "revenue-context") -> dict[str, object]:
        """CREATE-shaped snapshot: `definition()` plus `product_key`/
        `project_id`."""
        return {
            "product_key": product_key,
            "project_id": str(self.project.id),
            **self.definition(),
        }


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def _submit_single_item_change_set(
    scenario: _Scenario,
    *,
    name: str,
    object_id: str,
    operation: str,
    after_snapshot: dict[str, object] | None,
) -> UUID:
    context = scenario.maker()
    cs_read = await create_change_set(
        StudioChangeSetCreate(name=name), context=context, session=scenario.db
    )
    await add_item(
        cs_read.id,
        StudioChangeItemCreate(
            object_type="CONTEXT_PRODUCT",
            object_id=object_id,
            operation=operation,  # type: ignore[arg-type]
            after_snapshot=after_snapshot,
        ),
        context=context,
        session=scenario.db,
    )
    test_result = await run_tests(cs_read.id, context=context, session=scenario.db)
    assert test_result.passed is True, test_result.evidence
    submitted = await submit_change_set(cs_read.id, context=context, session=scenario.db)
    assert submitted.status == "SUBMITTED"
    return cs_read.id


async def test_create_item_materializes_a_real_draft_context_product(
    scenario: _Scenario,
) -> None:
    change_set_id = await _submit_single_item_change_set(
        scenario,
        name="new-product",
        object_id="revenue-context",
        operation="CREATE",
        after_snapshot=scenario.snapshot(),
    )

    product = (
        await scenario.db.scalars(
            select(ContextProduct).where(
                ContextProduct.organization_id == scenario.organization.id,
                ContextProduct.product_key == "revenue-context",
            )
        )
    ).one()
    version = (
        await scenario.db.scalars(
            select(ContextProductVersion).where(ContextProductVersion.product_id == product.id)
        )
    ).one()
    assert version.version == 1
    assert version.status == "REVIEW_REQUIRED"
    assert version.name == "Revenue Context"

    review = (
        await scenario.db.scalars(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
                GovernanceReview.object_id == str(version.id),
            )
        )
    ).one()
    assert review.status == "PENDING"
    assert review.requested_action == "PUBLISH"
    assert review.requested_by == "maker@example.com"

    # ST-A7's traceability row links the change item to what it produced.
    materialization = (
        await scenario.db.scalars(
            select(StudioContextProductMaterialization).where(
                StudioContextProductMaterialization.change_set_id == change_set_id
            )
        )
    ).one()
    assert materialization.operation == "CREATE"
    assert materialization.context_product_id == product.id
    assert materialization.context_product_version_id == version.id
    assert materialization.governance_review_id == review.id

    # Not bypassed or duplicated: the *same* decide_governance_review that
    # approves a directly-authored context product approves this one.
    context = scenario.maker()
    with pytest.raises(HTTPException) as self_approve:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            context=context,
            session=scenario.db,
        )
    assert self_approve.value.status_code == 409
    assert "maker-checker separation" in str(self_approve.value.detail)

    approved = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=scenario.checker(),
        session=scenario.db,
    )
    assert approved.status == "APPROVED"
    await scenario.db.refresh(version)
    assert version.status == "PUBLISHED"
    assert version.approved_by == "checker@example.com"


async def test_update_item_creates_a_new_draft_version_based_on_the_latest(
    scenario: _Scenario,
) -> None:
    await _submit_single_item_change_set(
        scenario,
        name="new-product",
        object_id="revenue-context",
        operation="CREATE",
        after_snapshot=scenario.snapshot(),
    )
    product = (
        await scenario.db.scalars(
            select(ContextProduct).where(
                ContextProduct.organization_id == scenario.organization.id,
                ContextProduct.product_key == "revenue-context",
            )
        )
    ).one()
    v1 = (
        await scenario.db.scalars(
            select(ContextProductVersion).where(ContextProductVersion.product_id == product.id)
        )
    ).one()

    updated_snapshot = scenario.definition()
    updated_snapshot["description"] = "Updated bounded context for revenue analysis."
    await _submit_single_item_change_set(
        scenario,
        name="update-product",
        object_id=str(product.id),
        operation="UPDATE",
        after_snapshot=updated_snapshot,
    )

    versions = (
        await scenario.db.scalars(
            select(ContextProductVersion)
            .where(ContextProductVersion.product_id == product.id)
            .order_by(ContextProductVersion.version)
        )
    ).all()
    assert [v.version for v in versions] == [1, 2]
    v2 = versions[1]
    assert v2.based_on_version_id == v1.id
    assert v2.status == "REVIEW_REQUIRED"
    assert v2.description == "Updated bounded context for revenue analysis."


async def test_delete_item_requests_deprecation_of_the_published_version(
    scenario: _Scenario,
) -> None:
    await _submit_single_item_change_set(
        scenario,
        name="new-product",
        object_id="revenue-context",
        operation="CREATE",
        after_snapshot=scenario.snapshot(),
    )
    product = (
        await scenario.db.scalars(
            select(ContextProduct).where(
                ContextProduct.organization_id == scenario.organization.id,
                ContextProduct.product_key == "revenue-context",
            )
        )
    ).one()
    version = (
        await scenario.db.scalars(
            select(ContextProductVersion).where(ContextProductVersion.product_id == product.id)
        )
    ).one()
    publish_review = (
        await scenario.db.scalars(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
                GovernanceReview.object_id == str(version.id),
                GovernanceReview.requested_action == "PUBLISH",
            )
        )
    ).one()
    await decide_governance_review(
        publish_review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=scenario.checker(),
        session=scenario.db,
    )
    await scenario.db.refresh(version)
    assert version.status == "PUBLISHED"

    await _submit_single_item_change_set(
        scenario,
        name="retire-product",
        object_id=str(product.id),
        operation="DELETE",
        after_snapshot=None,
    )

    deprecate_review = (
        await scenario.db.scalars(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
                GovernanceReview.object_id == str(version.id),
                GovernanceReview.requested_action == "DEPRECATE",
            )
        )
    ).one()
    assert deprecate_review.status == "PENDING"

    await decide_governance_review(
        deprecate_review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=scenario.checker(),
        session=scenario.db,
    )
    await scenario.db.refresh(version)
    assert version.status == "DEPRECATED"


async def test_create_with_duplicate_product_key_fails_submission(scenario: _Scenario) -> None:
    await _submit_single_item_change_set(
        scenario,
        name="first-product",
        object_id="revenue-context",
        operation="CREATE",
        after_snapshot=scenario.snapshot(),
    )

    context = scenario.maker()
    cs_read = await create_change_set(
        StudioChangeSetCreate(name="second-product"), context=context, session=scenario.db
    )
    await add_item(
        cs_read.id,
        StudioChangeItemCreate(
            object_type="CONTEXT_PRODUCT",
            object_id="revenue-context",
            operation="CREATE",
            after_snapshot=scenario.snapshot(),
        ),
        context=context,
        session=scenario.db,
    )
    test_result = await run_tests(cs_read.id, context=context, session=scenario.db)
    assert test_result.passed is True  # shape is fine; the key collision is a DB-level fact

    with pytest.raises(HTTPException) as exc_info:
        await submit_change_set(cs_read.id, context=context, session=scenario.db)
    assert exc_info.value.status_code == 409
    assert "already exists" in str(exc_info.value.detail)

    # The failed submission must not have flipped the change set's status.
    stored = await scenario.db.get(StudioChangeSet, cs_read.id)
    assert stored is not None
    assert stored.status != "SUBMITTED"


async def test_create_with_unresolvable_table_reference_fails_submission(
    scenario: _Scenario,
) -> None:
    context = scenario.maker()
    cs_read = await create_change_set(
        StudioChangeSetCreate(name="bad-reference"), context=context, session=scenario.db
    )
    bad_snapshot = scenario.snapshot()
    bad_snapshot["table_ids"] = [str(uuid4())]  # does not exist
    await add_item(
        cs_read.id,
        StudioChangeItemCreate(
            object_type="CONTEXT_PRODUCT",
            object_id="revenue-context",
            operation="CREATE",
            after_snapshot=bad_snapshot,
        ),
        context=context,
        session=scenario.db,
    )
    test_result = await run_tests(cs_read.id, context=context, session=scenario.db)
    assert test_result.passed is True  # shape alone is fine; only the DB knows the ref is bad

    with pytest.raises(HTTPException) as exc_info:
        await submit_change_set(cs_read.id, context=context, session=scenario.db)
    assert exc_info.value.status_code == 422
    assert "tables" in str(exc_info.value.detail)


async def test_shape_invalid_item_never_reaches_materialization(scenario: _Scenario) -> None:
    """The test gate blocks submission before materialization is ever
    attempted -- a malformed snapshot fails at `run_tests`, not with a
    confusing DB-layer error from inside `materialize_context_product_item`."""
    context = scenario.maker()
    cs_read = await create_change_set(
        StudioChangeSetCreate(name="malformed"), context=context, session=scenario.db
    )
    malformed = scenario.snapshot()
    del malformed["purpose"]
    await add_item(
        cs_read.id,
        StudioChangeItemCreate(
            object_type="CONTEXT_PRODUCT",
            object_id="revenue-context",
            operation="CREATE",
            after_snapshot=malformed,
        ),
        context=context,
        session=scenario.db,
    )
    test_result = await run_tests(cs_read.id, context=context, session=scenario.db)
    assert test_result.passed is False

    with pytest.raises(HTTPException) as exc_info:
        await submit_change_set(cs_read.id, context=context, session=scenario.db)
    assert exc_info.value.status_code == 409
    assert "have not passed testing" in str(exc_info.value.detail)

    materializations = (
        await scenario.db.scalars(
            select(StudioContextProductMaterialization).where(
                StudioContextProductMaterialization.change_set_id == cs_read.id
            )
        )
    ).all()
    assert materializations == []


async def test_standalone_validate_endpoint_matches_the_test_gate(scenario: _Scenario) -> None:
    context = scenario.maker()
    result = await validate_context_product_contract_endpoint(
        StudioContextProductValidateRequest(
            operation="CREATE",
            object_id="revenue-context",
            snapshot=scenario.snapshot(),
        ),
        context=context,
    )
    assert result.valid is True
    assert result.product_key == "revenue-context"
    assert result.project_id == str(scenario.project.id)


async def test_list_materializations_endpoint_returns_the_evidence_trail(
    scenario: _Scenario,
) -> None:
    change_set_id = await _submit_single_item_change_set(
        scenario,
        name="new-product",
        object_id="revenue-context",
        operation="CREATE",
        after_snapshot=scenario.snapshot(),
    )
    context = scenario.maker()
    rows = await list_context_product_materializations(
        change_set_id, context=context, session=scenario.db
    )
    assert len(rows) == 1
    assert rows[0].operation == "CREATE"
    assert rows[0].change_set_id == change_set_id
