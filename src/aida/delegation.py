"""PG-4: time-bounded, audited delegation of governance authority.

Deterministic, DB-free helpers for delegation grant validation and
time-window enforcement -- mirrors `aida.abac`'s split (a pure evaluation
engine, with `aida.delegation_api` doing persistence, auditing, and wiring
into the request path) rather than folding SQL and policy logic together.

The two invariants this module enforces:

  * A principal can only delegate authority it actually holds
    (`validate_delegated_roles`) -- never manufacture broader authority than
    the delegator's own current roles.
  * A delegation is only ever honored inside its own `[starts_at,
    expires_at)` window and while its status is ACTIVE
    (`is_delegation_active`) -- time-bounding is enforced here, at the one
    place every caller (`aida.security.require_roles_or_delegated`) goes
    through, not left to each call site to re-derive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DelegationGrant:
    """The bare facts of one delegation row needed to evaluate it."""

    id: str
    delegator_principal_id: str
    delegate_principal_id: str
    delegated_roles: frozenset[str]
    starts_at: datetime
    expires_at: datetime
    status: str


def validate_delegated_roles(delegated_roles: set[str], delegator_roles: set[str]) -> None:
    """Raise ``ValueError`` if any role being delegated is not one the
    delegator itself currently holds. Roles in this codebase are asserted
    per-request (header/OIDC claims), not read from a stored directory, so
    "held by the delegator" means "present on the delegator's own grant
    request" -- the only signal available; ``delegation_api.grant_delegation``
    passes the granting caller's own ``context.roles`` here.
    """
    excess = set(delegated_roles) - set(delegator_roles)
    if excess:
        raise ValueError(
            f"cannot delegate role(s) not held by the delegator: {sorted(excess)}"
        )


def is_delegation_active(grant: DelegationGrant, *, at: datetime) -> bool:
    """PG-4's time-bounding: ACTIVE status and strictly inside
    ``[starts_at, expires_at)`` at instant ``at``. A delegation that has
    passed its ``expires_at`` (or been explicitly REVOKED) is never honored,
    even though its row is retained as audited history.
    """
    return grant.status == "ACTIVE" and grant.starts_at <= at < grant.expires_at


def delegated_roles_available(
    grants: list[DelegationGrant],
    *,
    delegate_principal_id: str,
    at: datetime,
) -> frozenset[str]:
    """Every role `delegate_principal_id` may currently act with through an
    active delegation, unioned across every grant made to it that is active
    at instant ``at``. Pure set-reduction over already-loaded rows -- the
    caller (`aida.security.require_roles_or_delegated`) is responsible for
    scoping the query that produces `grants` to the right organization and
    delegate.
    """
    available: set[str] = set()
    for grant in grants:
        if grant.delegate_principal_id != delegate_principal_id:
            continue
        if is_delegation_active(grant, at=at):
            available |= set(grant.delegated_roles)
    return frozenset(available)
