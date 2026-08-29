from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status


@dataclass(frozen=True, slots=True)
class SecurityContext:
    principal_id: str
    principal_type: str
    organization_id: UUID | None
    roles: frozenset[str]
    source_ip: str | None = None
    business_purpose: str | None = None

    def require_organization(self) -> UUID:
        if self.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="organization context is required for this operation",
            )
        return self.organization_id
