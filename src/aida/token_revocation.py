"""Token revocation and replay policy (ID-4, module 01 identity-and-tenancy).

Revocation is a security decision, so it fails closed (INV-4): a token whose
revocation status cannot be determined -- a database error, a session that has gone
away -- is treated exactly like a token that *is* revoked, never like one that
passed. Treating a failed lookup as "not revoked" would turn a transient database
blip into a silent bypass of every revocation ever issued.

**What "replay" means here.** AIDA's only bearer-token type today is the standard
OIDC JWT verified in `aida.oidc`; there is no separate single-use or nonce-bearing
token type yet (workload identity, ID-3, and break-glass, ID-5, are both still
open). So the replay scenario this module defends is the one that actually exists:
presenting a bearer token again after it has been revoked -- through an explicit
logout, an admin response to a compromised credential, or any other revocation
event recorded here. A revoked token is rejected on every subsequent use, not only
logged; that rejection *is* the replay defense. If a genuinely single-use token type
is added later, its issuance path should record a revocation-capable identifier
through this same table rather than inventing a second mechanism.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import RevokedToken
from aida.oidc import token_identifier


class TokenRevokedError(RuntimeError):
    """Raised both when a token is revoked and when revocation status could not be
    determined. Callers must not distinguish the two cases in what they tell the
    caller -- both deny (INV-4: fail closed without leaking which check failed)."""


async def enforce_not_revoked(session: AsyncSession, claims: dict[str, Any]) -> None:
    """Raise `TokenRevokedError` if `claims` belongs to a revoked token, or if that
    could not be determined. Call after signature/issuer/audience/expiry
    verification, before the claims are trusted for anything else."""
    identifier = token_identifier(claims)
    try:
        result = await session.execute(
            select(RevokedToken.id).where(RevokedToken.token_identifier == identifier).limit(1)
        )
        row = result.first()
    except SQLAlchemyError as exc:
        raise TokenRevokedError("token revocation status could not be verified") from exc
    if row is not None:
        raise TokenRevokedError("token has been revoked")


async def prune_expired_revocations(session: AsyncSession, *, now: datetime) -> int:
    """Delete revocation records for tokens that can never be replayed again.

    Bounded on the token's own `token_expires_at`, never on `revoked_at` -- a token
    still inside its original expiry window stays revoked no matter how long ago it
    was revoked. This only removes rows whose token has already failed, and will
    always fail, the verifier's own expiry check, so pruning can never un-revoke a
    token that is still live. Returns the number of rows removed.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(delete(RevokedToken).where(RevokedToken.token_expires_at < now)),
    )
    return result.rowcount or 0
