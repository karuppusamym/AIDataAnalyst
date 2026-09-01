import asyncio
import hashlib
import json
from time import monotonic
from typing import Any
from uuid import UUID

import httpx
import jwt

from aida.config import Settings
from aida.security_types import SecurityContext

PLATFORM_ROLES = frozenset(
    {
        "PlatformAdmin",
        "OrganizationAdmin",
        "MetadataAdmin",
        "DataAdmin",
        "SemanticAdmin",
        "DataSteward",
        "ToolDeveloper",
        "ToolConsumer",
        "AgentDeveloper",
        "Reviewer",
        "MetadataReviewer",
        "Auditor",
        "Operations",
        "Analyst",
        "Viewer",
    }
)
# UX-1 / module 21 SS5: the shell's persona-oriented navigation modes. Kept in sync by
# hand with `ui-next/src/lib/types.ts`'s `Persona` union (that file documents the same
# generate-don't-hand-maintain caveat as the rest of its API mirror -- see UX-14).
PERSONAS = frozenset({"Analyst", "Steward", "Reviewer", "Operator", "Auditor"})
ALLOWED_SIGNING_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256"})
MAX_JWKS_BYTES = 1_048_576
MAX_JWKS_KEYS = 100


class OidcVerificationError(RuntimeError):
    pass


def _claim(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _string_list_claim(claims: dict[str, Any], path: str, *, claim_name: str) -> list[str]:
    raw = _claim(claims, path)
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return raw
    if raw is None:
        return []
    raise OidcVerificationError(f"OIDC {claim_name} claim has an invalid shape")


def _persona_from_groups(groups: list[str], settings: Settings) -> str | None:
    """UX-1: derive the shell's persona from the same verified groups claim used for
    role mapping, via `oidc_persona_mappings` -- the configurable claim-path mechanism
    module 01 already uses for roles, extended rather than duplicated. The first group
    (in claim order) that maps to a recognized persona wins; a principal in no mapped
    group falls back to `oidc_default_persona` when one is configured. Never
    client-selected: this is the only persona a production/OIDC principal gets.
    """
    for group in groups:
        candidate = settings.oidc_persona_mappings.get(group)
        if candidate in PERSONAS:
            return candidate
    if settings.oidc_default_persona in PERSONAS:
        return settings.oidc_default_persona
    return None


def context_from_claims(claims: dict[str, Any], settings: Settings) -> SecurityContext:
    subject = _claim(claims, settings.oidc_subject_claim)
    if not isinstance(subject, str) or not subject.strip():
        raise OidcVerificationError("OIDC subject claim is missing")
    external_roles = _string_list_claim(claims, settings.oidc_roles_claim, claim_name="roles")
    mapped_roles: set[str] = set()
    for role in external_roles:
        mappings = settings.oidc_role_mappings.get(role, [role])
        mapped_roles.update(mapped for mapped in mappings if mapped in PLATFORM_ROLES)
    external_groups = _string_list_claim(claims, settings.oidc_groups_claim, claim_name="groups")
    persona = _persona_from_groups(external_groups, settings)
    raw_organization = _claim(claims, settings.oidc_organization_claim)
    organization_id: UUID | None = None
    if raw_organization is not None:
        try:
            organization_id = UUID(str(raw_organization))
        except ValueError as exc:
            raise OidcVerificationError("OIDC organization claim is not a UUID") from exc
    principal_type = _claim(claims, settings.oidc_principal_type_claim) or "USER"
    if not isinstance(principal_type, str) or principal_type not in {
        "USER",
        "SERVICE_ACCOUNT",
        "AGENT",
        "WORKER",
    }:
        raise OidcVerificationError("OIDC principal type is invalid")
    business_purpose = _claim(claims, settings.oidc_business_purpose_claim)
    if business_purpose is not None and (
        not isinstance(business_purpose, str) or not business_purpose.strip()
    ):
        raise OidcVerificationError("OIDC business purpose claim is invalid")
    return SecurityContext(
        principal_id=subject,
        principal_type=principal_type,
        organization_id=organization_id,
        roles=frozenset(mapped_roles),
        business_purpose=(business_purpose.strip()[:200] if business_purpose else None),
        persona=persona,
    )


def token_identifier(claims: dict[str, Any]) -> str:
    """A stable per-token identifier for revocation and replay checks (ID-4).

    Prefers the registered `jti` claim (RFC 7519). Many external issuers omit it, so a
    token without one still gets a deterministic identifier derived from
    (subject, issued-at, expiry) -- for any correctly implemented issuer, re-issuing a
    token to the same subject in the same `iat` second would also have to reuse the
    same `exp` to collide here, which is not a distinction worth losing revocation
    coverage over. Called only after `OidcVerifier.verify` has already required
    `sub`, `iat` and `exp` to be present, so the fallback never sees a missing claim.
    """
    jti = claims.get("jti")
    if isinstance(jti, str) and jti.strip():
        return f"jti:{jti.strip()}"
    fingerprint = f"{claims.get('sub')}|{claims.get('iat')}|{claims.get('exp')}"
    return "fp:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


class OidcVerifier:
    """Asynchronous JWKS verifier with bounded caching and mandatory issuer/audience checks."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _load_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._jwks is not None and monotonic() < self._expires_at:
            return self._jwks
        async with self._lock:
            if not force and self._jwks is not None and monotonic() < self._expires_at:
                return self._jwks
            if self.settings.oidc_jwks_json:
                try:
                    jwks = json.loads(self.settings.oidc_jwks_json)
                except json.JSONDecodeError as exc:
                    raise OidcVerificationError("pinned OIDC JWKS JSON is invalid") from exc
            elif self.settings.oidc_jwks_url:
                try:
                    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                        response = await client.get(self.settings.oidc_jwks_url)
                        response.raise_for_status()
                        if len(response.content) > MAX_JWKS_BYTES:
                            raise OidcVerificationError("OIDC JWKS document exceeds the size limit")
                        jwks = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise OidcVerificationError("OIDC JWKS endpoint is unavailable") from exc
            else:
                raise OidcVerificationError("OIDC JWKS is not configured")
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                raise OidcVerificationError("OIDC JWKS document has an invalid shape")
            keys = jwks["keys"]
            if (
                not keys
                or len(keys) > MAX_JWKS_KEYS
                or not all(isinstance(key, dict) for key in keys)
            ):
                raise OidcVerificationError("OIDC JWKS key set has an invalid shape")
            self._jwks = jwks
            self._expires_at = monotonic() + self.settings.oidc_jwks_cache_seconds
            return jwks

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise OidcVerificationError("bearer token header is invalid") from exc
        kid = header.get("kid")
        algorithm = header.get("alg")
        if not isinstance(kid, str) or algorithm not in ALLOWED_SIGNING_ALGORITHMS:
            raise OidcVerificationError("bearer token key or algorithm is not allowed")
        jwks = await self._load_jwks()
        key_data = next((key for key in jwks["keys"] if key.get("kid") == kid), None)
        if key_data is None:
            jwks = await self._load_jwks(force=True)
            key_data = next((key for key in jwks["keys"] if key.get("kid") == kid), None)
        if key_data is None:
            raise OidcVerificationError("bearer token signing key is unknown")
        if key_data.get("use") not in {None, "sig"}:
            raise OidcVerificationError("bearer token key is not a signing key")
        if key_data.get("alg") not in {None, algorithm}:
            raise OidcVerificationError("bearer token algorithm does not match its key")
        key_operations = key_data.get("key_ops")
        if key_operations is not None and (
            not isinstance(key_operations, list) or "verify" not in key_operations
        ):
            raise OidcVerificationError("bearer token key does not permit verification")
        try:
            key = jwt.PyJWK.from_dict(key_data, algorithm=algorithm).key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                leeway=self.settings.oidc_clock_skew_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise OidcVerificationError("bearer token verification failed") from exc
        if not isinstance(claims, dict):
            raise OidcVerificationError("bearer token claims are invalid")
        return claims
