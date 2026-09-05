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
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.catalog_read_model import _latest_approved_documentation
from aida.column_documentation import current_descriptions_by_column_id
from aida.models import (
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


async def request_description_withdrawal(
    session: AsyncSession,
    *,
    organization_id: UUID,
    subject_type: str,
    subject_id: UUID,
    reason: str,
    requested_by: str,
) -> tuple[DescriptionWithdrawal, GovernanceReview]:
    """Raise a withdrawal for the subject's *current* approved description.

    Refuses when there is nothing approved to withdraw, rather than filing a
    review that would resolve to nothing -- a reviewer should never be handed a
    decision whose subject does not exist.
    """
    if subject_type not in ("TABLE", "COLUMN"):
        raise HTTPException(status_code=422, detail="subject_type must be TABLE or COLUMN")

    if subject_type == "COLUMN":
        column = await session.get(MetadataColumn, subject_id)
        if column is None or column.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="column not found")
        table = await session.get(MetadataTable, column.table_id)
        label = f"{table.name}.{column.name}" if table else column.name
        version = await _current_column_version(session, column.id)
        text = version.description if version else None
    else:
        table = await session.get(MetadataTable, subject_id)
        if table is None or table.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="table not found")
        label = table.name
        table_version = await _current_table_version(session, table.id)
        version = table_version
        text = table_version.readme if table_version else None

    if version is None or text is None:
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
        requested_action="WITHDRAW_DESCRIPTION",
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
    """Move the named version to `WITHDRAWN`.

    Called only from `semantic_api.decide_governance_review`, after its shared
    maker-checker guard. Returns the event type and whether the version was
    actually retired -- `False` when a newer version was published between the
    request and the decision, in which case nothing is touched: the reviewer
    approved the retraction of text that is no longer what the asset says.
    """
    if withdrawal.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="this withdrawal is no longer pending review")

    subject_id = UUID(withdrawal.subject_id)
    if withdrawal.subject_type == "COLUMN":
        current = await _current_column_version(session, subject_id)
    else:
        current = await _current_table_version(session, subject_id)

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


async def reject_description_withdrawal(
    withdrawal: DescriptionWithdrawal,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a withdrawal; the description stays published, untouched."""
    if withdrawal.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="this withdrawal is no longer pending review")
    withdrawal.status = "REJECTED"
    withdrawal.reviewed_by = reviewer
    withdrawal.reviewed_at = now
    return "description.withdrawal.rejected.v1"


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
