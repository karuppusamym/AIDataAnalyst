"""Retiring an approved description, through the same review every publish uses.

Publishing was governed from the start; un-publishing was not possible at all.
A steward who approved a wrong column description had no way to take it back:
the workbook path deliberately refuses to read a blank cell as a deletion, and
`publish_column_description` only ever adds. The only available remedy was to
publish a correction, which does not work when the right answer is that the
platform should say nothing.

Three properties, each of them the reason this is not just a `DELETE`:

**Withdrawal preserves the content.** The version row keeps its text and moves
to `WITHDRAWN`. An `AgentRun` that was grounded on it still resolves to exactly
the words it saw -- the append-only guarantee the whole versioning design rests
on would be worthless if a withdrawal could erase the evidence. What changes is
that the current-version resolvers (which filter `status == "APPROVED"`) stop
returning it, so the asset reads as undescribed again.

**It is reviewed, by someone else.** Removing a description an agent may be
grounding on is not a smaller decision than adding one, so it goes through
`GovernanceReview` and `decide_governance_review`'s maker-checker guard like
every other governed change.

**It is checked twice against the version it names.** A withdrawal records the
exact version id it was raised against. If someone publishes a newer version in
the window before approval, the request no longer refers to the text the
reviewer read, and it is refused rather than applied to content nobody looked
at -- the same lost-update reasoning `model_import`'s `expected_version` carries.

Reinstatement (`request_type="REINSTATE"`) is the inverse request, and it is
*not* the inverse operation: it republishes the withdrawn text as a **new**
version rather than flipping the WITHDRAWN row back to APPROVED. Flipping it
back would rewrite history -- the version chain would no longer record that the
description was ever retired, and a reader auditing why an agent run cited text
that "was always approved" would be misled. A reinstatement is a fresh publish
whose provenance happens to be an older version's words.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import publish_asset_documentation_version
from aida.catalog_read_model import _latest_approved_documentation
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    DescriptionWithdrawal,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
)

#: The status a withdrawn version carries. Deliberately not `SUPERSEDED`: that
#: means "a newer approved version replaced this one", and a reader (or a
#: future audit) must be able to tell a replacement from a retraction.
WITHDRAWN = "WITHDRAWN"


async def _current_column_version(
    session: AsyncSession, column_id: UUID
) -> ColumnDocumentationVersion | None:
    return (await current_descriptions_by_column_id(session, [column_id])).get(column_id)


async def _current_table_version(
    session: AsyncSession, table_id: UUID
) -> AssetDocumentationVersion | None:
    return (await _latest_approved_documentation(session, [table_id])).get(table_id)


async def _latest_withdrawn_version(
    session: AsyncSession, subject_type: str, subject_id: UUID
) -> ColumnDocumentationVersion | AssetDocumentationVersion | None:
    """The most recently withdrawn version for a subject, if any.

    What a reinstatement brings back. Ordered by version so the newest retired
    text wins -- reinstating anything older would be an edit dressed as an undo.
    """
    rows: Sequence[ColumnDocumentationVersion] | Sequence[AssetDocumentationVersion]
    if subject_type == "COLUMN":
        rows = (
            (
                await session.execute(
                    select(ColumnDocumentationVersion)
                    .join(
                        ColumnDocumentation,
                        ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
                    )
                    .where(
                        ColumnDocumentation.column_id == subject_id,
                        ColumnDocumentationVersion.status == WITHDRAWN,
                    )
                    .order_by(ColumnDocumentationVersion.version)
                )
            )
            .scalars()
            .all()
        )
    else:
        rows = (
            (
                await session.execute(
                    select(AssetDocumentationVersion)
                    .join(
                        AssetDocumentation,
                        AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
                    )
                    .where(
                        AssetDocumentation.table_id == subject_id,
                        AssetDocumentationVersion.status == WITHDRAWN,
                    )
                    .order_by(AssetDocumentationVersion.version)
                )
            )
            .scalars()
            .all()
        )
    return rows[-1] if rows else None


async def request_description_withdrawal(
    session: AsyncSession,
    *,
    organization_id: UUID,
    subject_type: str,
    subject_id: UUID,
    reason: str,
    requested_by: str,
    request_type: str = "WITHDRAW",
) -> tuple[DescriptionWithdrawal, GovernanceReview]:
    """Raise a withdrawal, or a reinstatement, for one asset's description.

    Refuses when there is nothing to act on -- no approved description to
    retire, or no retired one to bring back -- rather than filing a review that
    would resolve to nothing. A reviewer should never be handed a decision whose
    subject does not exist.
    """
    if subject_type not in ("TABLE", "COLUMN"):
        raise HTTPException(status_code=422, detail="subject_type must be TABLE or COLUMN")
    if request_type not in ("WITHDRAW", "REINSTATE"):
        raise HTTPException(
            status_code=422, detail="request_type must be WITHDRAW or REINSTATE"
        )

    if subject_type == "COLUMN":
        column = await session.get(MetadataColumn, subject_id)
        if column is None or column.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="column not found")
        table = await session.get(MetadataTable, column.table_id)
        label = f"{table.name}.{column.name}" if table else column.name
        column_version = await _current_column_version(session, column.id)
        version: ColumnDocumentationVersion | AssetDocumentationVersion | None = column_version
        text = column_version.description if column_version else None
    else:
        table = await session.get(MetadataTable, subject_id)
        if table is None or table.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="table not found")
        label = table.name
        table_version = await _current_table_version(session, table.id)
        version = table_version
        text = table_version.readme if table_version else None

    if request_type == "REINSTATE":
        # A reinstatement acts on the retired text, not the current one -- and
        # only when there is nothing current, because republishing over a live
        # description is a correction, which is authored rather than undone.
        if version is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "this asset already has an approved description; publish a correction "
                    "rather than reinstating an older one"
                ),
            )
        retired = await _latest_withdrawn_version(session, subject_type, subject_id)
        if retired is None:
            raise HTTPException(
                status_code=409,
                detail="there is no withdrawn description on this asset to reinstate",
            )
        version = retired
        text = (
            retired.description
            if isinstance(retired, ColumnDocumentationVersion)
            else retired.readme
        )
    elif version is None or text is None:
        raise HTTPException(
            status_code=409,
            detail="there is no approved description on this asset to withdraw",
        )

    existing = await session.scalar(
        select(DescriptionWithdrawal).where(
            DescriptionWithdrawal.subject_id == str(subject_id),
            DescriptionWithdrawal.status == "PENDING_REVIEW",
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="a withdrawal for this asset is already awaiting review",
        )

    withdrawal = DescriptionWithdrawal(
        organization_id=organization_id,
        request_type=request_type,
        subject_type=subject_type,
        subject_id=str(subject_id),
        subject_label=label[:600],
        version_id=version.id,
        withdrawn_text=text,
        reason=reason,
        status="PENDING_REVIEW",
        requested_by=requested_by,
    )
    session.add(withdrawal)
    await session.flush()
    review = GovernanceReview(
        organization_id=organization_id,
        object_type="DESCRIPTION_WITHDRAWAL",
        object_id=str(withdrawal.id),
        requested_action=(
            "WITHDRAW_DESCRIPTION" if request_type == "WITHDRAW" else "REINSTATE_DESCRIPTION"
        ),
        requested_by=requested_by,
    )
    session.add(review)
    await session.flush()
    withdrawal.governance_review_id = review.id
    await session.flush()
    return withdrawal, review


async def apply_description_withdrawal(
    session: AsyncSession,
    withdrawal: DescriptionWithdrawal,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, bool]:
    """Apply an approved request: retire the named version, or republish it.

    Called only from `semantic_api.decide_governance_review`, after its shared
    maker-checker guard. Returns the event type and whether the request actually
    took effect -- `False` when the asset moved on between the request and the
    decision, in which case nothing is touched: the reviewer decided about text
    that is no longer what the asset says.
    """
    if withdrawal.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="this request is no longer pending review")

    subject_id = UUID(withdrawal.subject_id)
    if withdrawal.request_type == "REINSTATE":
        return await _apply_reinstatement(
            session, withdrawal, subject_id=subject_id, reviewer=reviewer, now=now
        )

    # One union-typed local rather than two branches: the retirement below is
    # identical for both stores (flip `status`, stamp `updated_at`), and only
    # the lookup differs.
    current: ColumnDocumentationVersion | AssetDocumentationVersion | None = (
        await _current_column_version(session, subject_id)
        if withdrawal.subject_type == "COLUMN"
        else await _current_table_version(session, subject_id)
    )

    withdrawal.status = "APPROVED"
    withdrawal.reviewed_by = reviewer
    withdrawal.reviewed_at = now

    if current is None or current.id != withdrawal.version_id:
        # Someone published in the window. Refusing here rather than retiring
        # whatever is current now is the whole reason `version_id` is recorded:
        # the reviewer read one description and would otherwise remove another.
        return "description.withdrawal.superseded.v1", False

    current.status = WITHDRAWN
    current.updated_at = now
    await session.flush()
    return "description.withdrawal.approved.v1", True


async def _apply_reinstatement(
    session: AsyncSession,
    withdrawal: DescriptionWithdrawal,
    *,
    subject_id: UUID,
    reviewer: str,
    now: datetime,
) -> tuple[str, bool]:
    """Republish a withdrawn description as a new version.

    Never flips the WITHDRAWN row back to APPROVED: the version chain has to go
    on recording that the description was retired, or an audit of why an agent
    cited text that "was always approved" would be misled. This is a fresh
    publish whose provenance happens to be an older version's words.
    """
    withdrawal.status = "APPROVED"
    withdrawal.reviewed_by = reviewer
    withdrawal.reviewed_at = now

    # Someone described the asset again while this was pending; their text is
    # current and reinstating over it would silently replace it.
    current: ColumnDocumentationVersion | AssetDocumentationVersion | None = (
        await _current_column_version(session, subject_id)
        if withdrawal.subject_type == "COLUMN"
        else await _current_table_version(session, subject_id)
    )
    if current is not None:
        return "description.reinstatement.superseded.v1", False

    if withdrawal.subject_type == "COLUMN":
        column = await session.get(MetadataColumn, subject_id)
        if column is None:
            return "description.reinstatement.superseded.v1", False
        await publish_column_description(
            session,
            organization_id=withdrawal.organization_id,
            table_id=column.table_id,
            column_id=column.id,
            description=withdrawal.withdrawn_text,
            created_by=withdrawal.requested_by,
            approved_by=reviewer,
            approved_at=now,
        )
    else:
        table = await session.get(MetadataTable, subject_id)
        if table is None:
            return "description.reinstatement.superseded.v1", False
        await publish_asset_documentation_version(
            session,
            organization_id=withdrawal.organization_id,
            table_id=table.id,
            readme=withdrawal.withdrawn_text,
            created_by=withdrawal.requested_by,
            approved_by=reviewer,
            approved_at=now,
        )
    await session.flush()
    return "description.reinstatement.approved.v1", True


async def reject_description_withdrawal(
    withdrawal: DescriptionWithdrawal,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a request; the asset's description stays exactly as it is."""
    if withdrawal.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="this request is no longer pending review")
    withdrawal.status = "REJECTED"
    withdrawal.reviewed_by = reviewer
    withdrawal.reviewed_at = now
    return (
        "description.reinstatement.rejected.v1"
        if withdrawal.request_type == "REINSTATE"
        else "description.withdrawal.rejected.v1"
    )


async def latest_withdrawn_table_version(
    session: AsyncSession, table_id: UUID
) -> AssetDocumentationVersion | None:
    """The most recently withdrawn documentation for one table, if any.

    The table-level counterpart to `withdrawn_column_versions`, so a read
    surface can say "this was documented, and the documentation was retired"
    rather than reverting the table to looking never-documented.
    """
    version = await _latest_withdrawn_version(session, "TABLE", table_id)
    return version if isinstance(version, AssetDocumentationVersion) else None


async def withdrawn_column_versions(
    session: AsyncSession, column_ids: list[UUID]
) -> dict[UUID, ColumnDocumentationVersion]:
    """The most recently withdrawn version per column, if any.

    Lets a read surface say "this was described, and the description was
    retired" instead of silently reverting to looking never-documented -- which
    would make a withdrawal indistinguishable from an asset nobody has reached
    yet.
    """
    if not column_ids:
        return {}
    rows = (
        await session.execute(
            select(ColumnDocumentationVersion, ColumnDocumentation.column_id)
            .join(
                ColumnDocumentation,
                ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
            )
            .where(
                ColumnDocumentation.column_id.in_(column_ids),
                ColumnDocumentationVersion.status == WITHDRAWN,
            )
            .order_by(ColumnDocumentationVersion.version)
        )
    ).all()
    # Ordered ascending, so the last write per column wins -- the newest
    # withdrawn version.
    return {column_id: version for version, column_id in rows}
