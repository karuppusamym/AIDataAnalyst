from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.integration_catalog import normalized_transformation_metadata_integrations
from aida.models import OrganizationIntegrationPolicy


async def ensure_organization_integration_policy(
    session: AsyncSession, organization_id: UUID
) -> OrganizationIntegrationPolicy:
    policy = await session.scalar(
        select(OrganizationIntegrationPolicy).where(
            OrganizationIntegrationPolicy.organization_id == organization_id
        )
    )
    if policy is None:
        policy = OrganizationIntegrationPolicy(organization_id=organization_id)
        session.add(policy)
        await session.flush()
    policy.transformation_metadata_integrations = normalized_transformation_metadata_integrations(
        policy.transformation_metadata_integrations
    )
    return policy
