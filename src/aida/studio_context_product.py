"""ST-A7: materialize a validated Studio CONTEXT_PRODUCT change item into a
real module-19 ``ContextProduct``/``ContextProductVersion`` (defined and
normally authored through ``context_product_api.py``), submitted through the
exact same maker-checker ``GovernanceReview`` queue a directly-authored
context product uses.

This module never approves or publishes anything itself -- it only ever
*requests* review, by creating a ``GovernanceReview`` row with
``object_type="CONTEXT_PRODUCT_VERSION"``, exactly like a human author calling
``POST /v1/context-product-versions/{id}/submit`` or
``POST /v1/context-product-versions/{id}/deprecate`` directly. Approval and
publication remain the exclusive job of ``decide_governance_review``'s
existing ``CONTEXT_PRODUCT_VERSION`` branch (``semantic_api.py``) -- this
module does not bypass it, duplicate its logic, or grant itself any shortcut
around maker-checker separation (the requester here is still whichever
principal called ``submit_change_set``, and self-approval is still rejected
by ``decide_governance_review`` the same way for every other caller).

``studio_api.py::submit_change_set`` calls ``materialize_context_product_item``
once per CONTEXT_PRODUCT item in a change set, only after the existing test
gate (shape validation via ``validate_context_product_contract``, plus the
ST-A8 regression gate) has already passed -- so every snapshot reaching this
module has already been shape-validated. Reference existence (tables,
semantic model versions, glossary terms, tool versions) is re-validated for
real here, against the database, by reusing
``validate_context_product_references`` unchanged: the pure test harness has
no database to check them against, so this is not a duplicate check, it is
the first time this specific snapshot's references are actually checked.

Traceability: each successful materialization persists one
``StudioContextProductMaterialization`` row linking the originating change
item to the real ``ContextProduct``/``ContextProductVersion``/
``GovernanceReview`` it produced -- durable evidence that a specific
Studio-authored edit produced a specific governed object, without adding any
column to the shared ``StudioChangeItem``/``StudioChangeSet`` models.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.context_product_api import (
    apply_context_product_definition,
    replace_context_product_role_bindings,
    validate_context_product_references,
)
from aida.events import record_audit, record_outbox
from aida.models import (
    ContextProduct,
    ContextProductVersion,
    GovernanceReview,
    Project,
    StudioContextProductMaterialization,
)
from aida.schemas import ContextProductDefinition
from aida.security import SecurityContext, enforce_organization
from aida.studio import ChangeItem, validate_context_product_contract


async def _request_review(
    session: AsyncSession,
    *,
    context: SecurityContext,
    change_set_id: UUID,
    product: ContextProduct,
    version: ContextProductVersion,
    requested_action: str,
) -> GovernanceReview:
    """Create the *request* half of maker-checker for one context product
    version -- identical in shape to ``context_product_api.py``'s own
    ``submit_context_product_version``/``request_context_product_deprecation``.
    Approval remains ``decide_governance_review``'s job alone.
    """
    review = GovernanceReview(
        organization_id=product.organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action=requested_action,
        requested_by=context.principal_id,
    )
    session.add(review)
    if requested_action == "PUBLISH":
        version.status = "REVIEW_REQUIRED"
    await session.flush()

    audit_context = replace(context, organization_id=product.organization_id)
    record_audit(
        session,
        audit_context,
        action=f"studio.context_product.{requested_action.lower()}_request",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "product_key": product.product_key,
            "context_product_version_id": str(version.id),
            "change_set_id": str(change_set_id),
        },
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
            "requested_action": requested_action,
            "source": "studio_change_set",
            "change_set_id": str(change_set_id),
        },
    )
    return review


def _shape_error(item: ChangeItem, errors: list[str]) -> HTTPException:
    joined = "; ".join(errors) or "invalid contract"
    return HTTPException(
        status_code=422,
        detail=(
            f"CONTEXT_PRODUCT item {item.object_id!r} failed shape validation at "
            f"materialization: {joined}"
        ),
    )


async def _materialize_create(
    session: AsyncSession,
    *,
    context: SecurityContext,
    change_set_id: UUID,
    item: ChangeItem,
) -> StudioContextProductMaterialization:
    contract = validate_context_product_contract(
        operation="CREATE", object_id=item.object_id, snapshot=item.after_snapshot
    )
    if not contract.valid or contract.project_id is None or contract.product_key is None:
        raise _shape_error(item, contract.errors)

    try:
        project_id = UUID(contract.project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="project_id is not a valid UUID") from exc

    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)

    definition = ContextProductDefinition.model_validate(contract.definition)
    await validate_context_product_references(session, project, definition)

    existing = await session.scalar(
        select(ContextProduct.id).where(
            ContextProduct.organization_id == project.organization_id,
            ContextProduct.product_key == contract.product_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="context product key already exists")

    product = ContextProduct(
        organization_id=project.organization_id,
        project_id=project.id,
        product_key=contract.product_key,
        created_by=context.principal_id,
    )
    session.add(product)
    await session.flush()

    version = apply_context_product_definition(
        ContextProductVersion(
            organization_id=project.organization_id,
            product_id=product.id,
            version=1,
            created_by=context.principal_id,
        ),
        definition,
    )
    session.add(version)
    await session.flush()
    await replace_context_product_role_bindings(session, version)

    review = await _request_review(
        session,
        context=context,
        change_set_id=change_set_id,
        product=product,
        version=version,
        requested_action="PUBLISH",
    )

    materialization = StudioContextProductMaterialization(
        organization_id=product.organization_id,
        change_set_id=change_set_id,
        change_item_id=item.id,
        operation="CREATE",
        context_product_id=product.id,
        context_product_version_id=version.id,
        governance_review_id=review.id,
        created_by=context.principal_id,
    )
    session.add(materialization)
    await session.flush()
    return materialization


async def _materialize_update(
    session: AsyncSession,
    *,
    context: SecurityContext,
    change_set_id: UUID,
    item: ChangeItem,
) -> StudioContextProductMaterialization:
    try:
        product_id = UUID(item.object_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "UPDATE requires an existing context product UUID as object_id: "
                f"{item.object_id!r}"
            ),
        ) from exc

    product = await session.get(ContextProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="context product not found")
    enforce_organization(context, product.organization_id)
    if product.lifecycle_status != "ACTIVE":
        raise HTTPException(status_code=409, detail="context product is not active")

    project = await session.get(Project, product.project_id)
    if project is None:
        raise HTTPException(status_code=409, detail="context product project is unavailable")

    contract = validate_context_product_contract(
        operation="UPDATE", object_id=item.object_id, snapshot=item.after_snapshot
    )
    if not contract.valid:
        raise _shape_error(item, contract.errors)

    definition = ContextProductDefinition.model_validate(contract.definition)
    await validate_context_product_references(session, project, definition)

    latest_version_row = (
        await session.execute(
            select(ContextProductVersion.id, ContextProductVersion.version)
            .where(ContextProductVersion.product_id == product.id)
            .order_by(ContextProductVersion.version.desc())
            .limit(1)
        )
    ).first()
    based_on_version_id = latest_version_row[0] if latest_version_row is not None else None
    next_version_number = (
        (latest_version_row[1] if latest_version_row is not None else 0) or 0
    ) + 1

    version = apply_context_product_definition(
        ContextProductVersion(
            organization_id=product.organization_id,
            product_id=product.id,
            version=next_version_number,
            created_by=context.principal_id,
            based_on_version_id=based_on_version_id,
        ),
        definition,
    )
    session.add(version)
    await session.flush()
    await replace_context_product_role_bindings(session, version)

    review = await _request_review(
        session,
        context=context,
        change_set_id=change_set_id,
        product=product,
        version=version,
        requested_action="PUBLISH",
    )

    materialization = StudioContextProductMaterialization(
        organization_id=product.organization_id,
        change_set_id=change_set_id,
        change_item_id=item.id,
        operation="UPDATE",
        context_product_id=product.id,
        context_product_version_id=version.id,
        governance_review_id=review.id,
        created_by=context.principal_id,
    )
    session.add(materialization)
    await session.flush()
    return materialization


async def _materialize_delete(
    session: AsyncSession,
    *,
    context: SecurityContext,
    change_set_id: UUID,
    item: ChangeItem,
) -> StudioContextProductMaterialization:
    """A Studio DELETE on a context product requests deprecation of its
    current PUBLISHED/SUPPORTED version -- there is no hard delete of an
    immutable, already-published context product version, matching
    ``request_context_product_deprecation`` in ``context_product_api.py``.
    """
    try:
        product_id = UUID(item.object_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "DELETE requires an existing context product UUID as object_id: "
                f"{item.object_id!r}"
            ),
        ) from exc

    product = await session.get(ContextProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="context product not found")
    enforce_organization(context, product.organization_id)

    version = await session.scalar(
        select(ContextProductVersion)
        .where(
            ContextProductVersion.product_id == product.id,
            ContextProductVersion.status.in_(("PUBLISHED", "SUPPORTED")),
        )
        .order_by(ContextProductVersion.version.desc())
        .limit(1)
    )
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="no published context product version to deprecate",
        )

    existing_review = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
            GovernanceReview.object_id == str(version.id),
            GovernanceReview.requested_action == "DEPRECATE",
            GovernanceReview.status == "PENDING",
        )
    )
    review = existing_review or await _request_review(
        session,
        context=context,
        change_set_id=change_set_id,
        product=product,
        version=version,
        requested_action="DEPRECATE",
    )

    materialization = StudioContextProductMaterialization(
        organization_id=product.organization_id,
        change_set_id=change_set_id,
        change_item_id=item.id,
        operation="DELETE",
        context_product_id=product.id,
        context_product_version_id=version.id,
        governance_review_id=review.id,
        created_by=context.principal_id,
    )
    session.add(materialization)
    await session.flush()
    return materialization


async def materialize_context_product_item(
    session: AsyncSession,
    *,
    context: SecurityContext,
    change_set_id: UUID,
    item: ChangeItem,
) -> StudioContextProductMaterialization:
    """Materialize one already-tested CONTEXT_PRODUCT change item.

    Dispatches on ``item.operation``:

    * ``CREATE`` -- a brand-new ``ContextProduct`` + version 1, submitted for
      review.
    * ``UPDATE`` -- a new ``DRAFT`` version of an existing ``ContextProduct``,
      submitted for review.
    * ``DELETE`` -- a ``DEPRECATE`` review request against the product's
      current ``PUBLISHED``/``SUPPORTED`` version.

    Raises ``HTTPException`` (404/409/422, matching ``context_product_api.py``'s
    own status codes for the same failures) if the item cannot be
    materialized -- an unknown project, a duplicate ``product_key``, a
    reference that does not resolve, or a product/version in the wrong
    state. The caller (``submit_change_set``) does not catch this: a
    materialization failure fails the whole submission, exactly like a
    failing test gate.
    """
    if item.operation == "CREATE":
        return await _materialize_create(
            session, context=context, change_set_id=change_set_id, item=item
        )
    if item.operation == "UPDATE":
        return await _materialize_update(
            session, context=context, change_set_id=change_set_id, item=item
        )
    return await _materialize_delete(
        session, context=context, change_set_id=change_set_id, item=item
    )
