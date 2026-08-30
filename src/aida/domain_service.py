from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import CrossBoundaryGrant, DataDomain, LineOfBusiness


async def ensure_default_domain(session: AsyncSession, lob: LineOfBusiness) -> DataDomain:
    """Return the LOB's default ("Ungoverned") data domain, creating it if missing.

    Every line of business gets exactly one is_default=True domain, lazily created on
    first use (mirrors ensure_organization_integration_policy). This is what lets a
    project or datasource be created before anyone has designed a domain taxonomy —
    it is scoped and access-controlled immediately, and domain assignment becomes a
    later steward triage step rather than a blocking setup step (ADR-0017 §10).
    """
    domain = await session.scalar(
        select(DataDomain).where(
            DataDomain.line_of_business_id == lob.id,
            DataDomain.is_default.is_(True),
     )
    )
    if domain is None:
        domain = DataDomain(
            organization_id=lob.organization_id,
            line_of_business_id=lob.id,
            name="Ungoverned",
            code="UNGOVERNED",
            is_default=True,
        )
        session.add(domain)
        await session.flush()
    return domain


async def resolve_domain(
    session: AsyncSession, lob: LineOfBusiness, data_domain_id: UUID | None
) -> DataDomain:
    """Resolve the domain a new project/datasource should belong to.

    Explicit `data_domain_id` must already belong to this LOB (enforced by the caller
    via a 404/422, not here) — omitted, it falls back to the LOB's default domain.
    """
    if data_domain_id is None:
        return await ensure_default_domain(session, lob)
    domain = await session.get(DataDomain, data_domain_id)
    if domain is None:
        # INV-4 (fail closed): an unresolvable domain reference is a denial, never a
        # silent `None` that downstream code dereferences into a 500. The caller is
        # expected to have validated the id; if it did not, refuse here rather than
        # proceed with no domain scope.
        raise ValueError(f"unknown data domain: {data_domain_id}")
    return domain


async def check_cross_boundary_grant(
    session: AsyncSession,
    organization_id: UUID,
    source_data_domain_id: UUID,
    target_data_domain_id: UUID,
    edge_kind: str | None = None,
) -> bool:
    """Can `target_data_domain_id` see across the boundary into `source_data_domain_id`?

    Traversal within one domain never calls this — it always returns True for
    source == target so callers can apply it uniformly. Otherwise an ACTIVE,
    unexpired CrossBoundaryGrant naming this exact ordered pair must exist;
    `edge_kinds` on the grant further restricts it to specific relationship
    kinds unless the grant's list is empty (meaning "all kinds"). This is the
    ADR-0017 SS4 enforcement primitive — Phase 5's cross-domain relationship
    inference and traversal call this per candidate edge and must report a
    withheld edge as `withheld:"no_grant"` rather than silently dropping it.
    """
    if source_data_domain_id == target_data_domain_id:
        return True
    now = datetime.now(UTC)
    grants = await session.scalars(
        select(CrossBoundaryGrant).where(
            CrossBoundaryGrant.organization_id == organization_id,
            CrossBoundaryGrant.source_data_domain_id == source_data_domain_id,
            CrossBoundaryGrant.target_data_domain_id == target_data_domain_id,
            CrossBoundaryGrant.status == "ACTIVE",
        )
    )
    for grant in grants:
        if grant.expires_at is not None and grant.expires_at <= now:
            continue
        if not grant.edge_kinds or edge_kind is None or edge_kind in grant.edge_kinds:
            return True
    return False
