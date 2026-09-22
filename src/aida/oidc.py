import asyncio
import hashlib
import json
from time import monotonic
from typing import Any
from uuid import UUID

import httpx
import jwt
import structlog

from aida.config import Settings
from aida.security_types import SecurityContext

_log = structlog.get_logger(__name__)

PLATFORM_ROLES = frozenset(
    {
        "PlatformAdmin",
        "OrganizationAdmin",
        "MetadataAdmin",
        "DataAdmin",
        "SemanticAdmin",
        "MetadataIngestor",
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
#: The shortest gap, in seconds, between two fetches of the issuer's key set when the reason for
#: fetching is a token naming a `kid` the cached set does not hold (R11-AUD09).
#:
#: `verify` used to reload the key set for EVERY such token, and nothing bounded how often. The
#: `kid` is read from the token header before any signature is checked, so the caller needs no
#: key, no account and no valid signature to trigger it: an allowed `alg` and any string for `kid`
#: is enough to make this API call the identity provider once per request. The asyncio lock in
#: `OidcVerifier` only lined those calls up one behind another (each bounded by a 5 second
#: timeout); it never reduced them. While the provider was down the same requests kept retrying,
#: so an outage became a request-rate amplifier aimed at the service that was already struggling.
#:
#: Inside the window an unknown `kid` is answered from the key set already held and is refused
#: exactly as before ("bearer token signing key is unknown"). The window runs from the end of the
#: last attempt to load the key set, of any kind -- the cold load, a cache-expiry refresh, an
#: earlier forced refetch, a failed attempt -- because a key set fetched a moment ago is as fresh
#: as another fetch could make it, and because a failed attempt has to count or a provider outage
#: is exactly when the limit would stop working.
#:
#: The trade-off is deliberate, and this constant is its bound. When the provider rotates its
#: signing key, a token signed with the new key is refused until the first unknown-`kid` token
#: that arrives once this much time has passed since the last fetch; that token's request
#: performs the fetch, and every token signed with the new key is accepted after it. Worst-case
#: pickup delay for a legitimate rotation is therefore this many seconds. A provider that
#: publishes the new key at least `oidc_jwks_cache_seconds` before it starts signing with it never
#: meets the window at all, because the ordinary cache refresh has already picked the key up.
JWKS_UNKNOWN_KID_REFETCH_COOLDOWN_SECONDS = 30.0

#: The shortest gap, in seconds, between one attempt to load the issuer's key set that FAILED and
#: the next attempt, whatever the reason for loading it (R11-AUD10).
#:
#: R11-AUD09 limited the fetch an unknown `kid` could cause and left the ordinary one alone. Once
#: the cached set had outlived `oidc_jwks_cache_seconds` the next request refreshed it; if the
#: provider did not answer, the request after that tried again, and the one after that. Each attempt
#: is bounded by a 5 second timeout and the lock lines them up, but nothing reduced how many there
#: were, so an outage of the identity provider became a request-rate amplifier aimed at the service
#: that was already struggling -- and this one needs no unknown `kid` and no attacker: every
#: authenticated request is enough.
#:
#: After a failed attempt -- the cold load, a cache-expiry refresh or an unknown-`kid` refetch alike
#: -- no further attempt is made until this long after the failed one ENDED (a 5 second timeout must
#: not eat the window). Inside it a request is answered from the last good key set while that set
#: may still be served (`JWKS_STALE_KEY_SET_GRACE_SECONDS`), and otherwise refused with the failure
#: the last attempt met. So a worker asks a dead provider at most twice a minute, however many
#: requests arrive. The window is fixed rather than growing: it is also the longest a provider that
#: has recovered goes unnoticed, and a window short enough to wait out is already gentle enough for
#: the provider.
#:
#: A constant, not a `Settings` field, like the unknown-key cooldown above: it protects the
#: provider from this service, not a deployment choice, and the deployment surface (and the
#: configuration inventory generated from it) should not grow for a limit with one sensible value.
JWKS_REFRESH_FAILURE_BACKOFF_SECONDS = 30.0

#: How long past its cache expiry, in seconds, a key set that could not be refreshed may still be
#: used to verify tokens (R11-AUD10).
#:
#: Serving the last good key set while the provider is unreachable keeps every signed-in user
#: working through the outages that last minutes -- a restart, a failover, a network fault --
#: instead of turning each one into a platform-wide 401. The price is a security one, and this
#: constant is its bound: a signing key the issuer has WITHDRAWN because it was compromised goes on
#: verifying tokens for as long as this process cannot reach the issuer to learn that. Nothing else
#: ends that. A token's `exp` is chosen by whoever holds the key, and revocation
#: (`aida.token_revocation`) is per token, not per key. So the stale service has to end on its own.
#:
#: With a reachable provider a withdrawn key stops verifying after at most `oidc_jwks_cache_seconds`
#: -- exposure the deployment already accepted. "Reachable" means the provider ANSWERED: a 4xx, an
#: empty or malformed key set, or an oversized document is the provider saying something about its
#: keys, not an outage, so none of those is answered from the stale set (`JwksRejected`; before
#: 2026-09-21 every failed refresh was, so an issuer that withdrew its keys by publishing an empty
#: set kept them verifying through the whole grace). Only a transport failure, a timeout, a 5xx or a
#: 429 is an outage. With an unreachable provider the exposure is at most the cache time PLUS this
#: constant: 15 minutes at the shipped 300 second cache, and never more, because past it the key
#: set is not served at all. A request is then refused exactly as when no key set could ever be
#: loaded ("OIDC JWKS endpoint is unavailable"), which is INV-4: an identity that cannot be checked
#: is not trusted.
#:
#: Ten minutes is a judgement, not a measurement: long enough for a routine restart or failover of
#: an identity provider, short enough that the extra exposure is small beside the cache lifetime's
#: own. A constant for the same reason as the backoff above; shortening it (or reaching zero, which
#: makes expiry fail closed at once) is a code change on purpose. Pinned keys (`oidc_jwks_json`)
#: have no network behind them and a document that parsed once parses again, so once loaded their
#: refresh cannot fail and none of this applies to them.
JWKS_STALE_KEY_SET_GRACE_SECONDS = 600.0

#: The refusal for a key set that could not be loaded. One string in one place: it is what
#: `_fetch_jwks` raises when the endpoint does not answer, and what a request is refused with while
#: nothing may be served and the last attempt failed for a reason that is not itself a refusal
#: (an unexpected exception carries no message worth repeating).
_JWKS_UNAVAILABLE = "OIDC JWKS endpoint is unavailable"


class OidcVerificationError(RuntimeError):
    pass


class JwksRejected(OidcVerificationError):
    """The provider answered, and the answer holds no usable key set.

    A 4xx or a redirect, a body that is not JSON, one over the size limit, or a key set that is
    empty, oversized or malformed. Unlike an outage this is information about the keys, so the last
    good set is not served in its place (`OidcVerifier._servable_stale`).
    """


class OidcTokenExpired(OidcVerificationError):
    """The token verified in every respect except that it has expired.

    A subclass, so every existing `except OidcVerificationError` still catches
    it and still denies. It exists so the one refusal a caller can usefully act
    on -- sign in again -- can be told apart from the ones it cannot.

    Found by running the OIDC flow in a browser (R11-D6): a token whose
    audience the deployment did not accept produced the same 401 detail as an
    expired one, the shell reported "The session has expired. Sign in again",
    and signing in again could never fix it. Disclosing *expiry* alone is safe:
    anyone holding a JWT can already read its `exp` claim, so the server saying
    so tells them nothing new. Signature, audience, issuer and revocation stay
    generic, because those do tell a holder something -- whether a forged or
    stolen token came close.
    """


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
        # `oidc_role_mappings` is a CLOSED contract: an external role with no
        # entry grants nothing.
        #
        # This read `.get(role, [role])` -- defaulting to the role's own name --
        # and then kept whatever survived `in PLATFORM_ROLES`. Those two lines
        # together were a privilege escalation, not a convenience: a token whose
        # roles claim contained the literal string "PlatformAdmin" was granted
        # PlatformAdmin, with no entry for it anywhere in the deployment's
        # configuration. Verified end to end against a real OIDC issuer on
        # 2026-09-12: a token claiming `["totally-unmapped-group",
        # "PlatformAdmin"]` against a deployment mapping only `atlas-admin`,
        # `atlas-steward` and `atlas-viewer` came back from `/v1/me` as
        # `roles: ["PlatformAdmin"]`.
        #
        # In a bank the roles claim is a directory group name, and group names
        # are frequently something a delegated administrator, a self-service
        # group feature or an over-scoped app registration can influence. The
        # platform's own role vocabulary must not be reachable by naming it.
        #
        # `oidc_role_mappings` defaults to `{}`, so the old default meant a
        # deployment that configured no mapping at all accepted whatever the
        # IdP called a role. Closing it makes that deployment grant nothing,
        # which is loudly wrong rather than quietly dangerous (INV-4).
        #
        # A deployment that genuinely wants a name accepted as-is says so by
        # mapping it to itself -- `{"PlatformAdmin": ["PlatformAdmin"]}` -- which
        # is self-documenting and needs no new setting.
        for mapped in settings.oidc_role_mappings.get(role, ()):
            if mapped in PLATFORM_ROLES:
                mapped_roles.add(mapped)
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
    """Asynchronous JWKS verifier with bounded caching and mandatory issuer/audience checks.

    The issuer's key set is loaded again for one of two reasons, and each is limited:

    * The cached set has outlived `oidc_jwks_cache_seconds`. The next token refreshes it, as it
      always has -- unless a refresh has just FAILED (below).
    * A token names a `kid` the cached set does not hold -- normally a rotation the cache has not
      seen yet. This forced refetch happens at most once per
      `JWKS_UNKNOWN_KID_REFETCH_COOLDOWN_SECONDS` (R11-AUD09), measured from the end of the last
      attempt to load the key set, successful or not. Inside that window the token is answered
      from the cached set and refused as an unknown signing key.

    The price of that limit is a bounded delay for a legitimate rotation: a token signed with a
    new key is refused until the first unknown-`kid` token that arrives once the cooldown has
    elapsed since the last fetch, and that token's own request performs the fetch. Worst-case
    pickup delay is the cooldown. Why the limit exists is written on the constant.

    **When a load fails (R11-AUD10).** Either kind of load can fail because the provider is down.
    The failed attempt starts a backoff, `JWKS_REFRESH_FAILURE_BACKOFF_SECONDS`, during which no
    load of either kind is attempted, so a dead provider is asked at most twice a minute however
    many requests arrive. What a request gets in the meantime depends on what this verifier holds:

    * A key set that is within `JWKS_STALE_KEY_SET_GRACE_SECONDS` of its cache expiry is served,
      stale. Tokens signed with its keys keep verifying, and one that names a key it does not hold
      is refused as unknown, exactly as inside the unknown-`kid` cooldown.
    * Otherwise -- nothing was ever loaded, or the grace has run out -- the request is refused with
      the reason the last attempt failed for ("OIDC JWKS endpoint is unavailable" for a provider
      that does not answer): the same refusal a verifier with no key set gives. This is the
      fail-closed end of the bound.

    A load that succeeds replaces the key set, restarts its expiry, and ends the backoff.

    The security trade-off is the whole reason the stale service is bounded, and it is written on
    `JWKS_STALE_KEY_SET_GRACE_SECONDS`: while the provider is unreachable a withdrawn key keeps
    verifying for up to `oidc_jwks_cache_seconds` plus the grace, and not one second longer. A
    provider that recovers is noticed at the next attempt after the backoff, so up to
    `JWKS_REFRESH_FAILURE_BACKOFF_SECONDS` late.

    Pinned keys (`oidc_jwks_json`) take the same path with no network behind it. Their document
    parses the same way every time, so once loaded a refresh cannot fail: no backoff starts and no
    key set ever goes stale.

    Every window here is per verifier -- one per worker process and identity-provider
    configuration, shared by everything in the process that verifies a token (see
    `aida.security.shared_oidc_verifier`) -- so N workers can each make one attempt per window.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks: dict[str, Any] | None = None
        self._expires_at = 0.0
        # When the most recent attempt to load the key set ENDED, on the same monotonic clock as
        # `_expires_at`, whether it succeeded or not. `None` until the first attempt. It is
        # deliberately separate from `_expires_at`: a failed attempt must not move the cache's
        # expiry (that would either extend a stale set or discard a good one), but it must still
        # start the cooldown.
        self._last_fetch_at: float | None = None
        # R11-AUD10. While the last attempt FAILED and this moment has not come, no attempt is
        # made. `None` when the last attempt succeeded, and before the first. A cancelled attempt
        # sets neither field: the caller going away says nothing about the provider.
        self._retry_after: float | None = None
        # Why that attempt failed, so a request refused inside the window is refused for the same
        # reason and not for a vaguer one. Always one of this module's fixed messages -- never
        # text taken from the provider's answer.
        self._failure_reason: str | None = None
        # False after an attempt the provider ANSWERED with no usable key set (`JwksRejected`):
        # then the held set is not served past its expiry, because the provider has just said it
        # no longer vouches for it. True again after the next good load.
        self._stale_allowed = True
        self._lock = asyncio.Lock()

    def _servable_stale(self, now: float) -> dict[str, Any] | None:
        """The key set held, if it is still within `JWKS_STALE_KEY_SET_GRACE_SECONDS` of expiry.

        A key set that has not expired is trivially inside its grace, so this is also what "the
        held set may be used" means during a backoff that began before the set expired.
        """
        held = self._jwks
        if not self._stale_allowed and now >= self._expires_at:
            return None
        if held is not None and now < self._expires_at + JWKS_STALE_KEY_SET_GRACE_SECONDS:
            return held
        return None

    def _current_key_set(self, *, force: bool) -> dict[str, Any] | None:
        """The key set this call can answer from without a fetch, or `None` when one is due.

        A normal call is due once the cache has expired. A forced call (unknown `kid`) is due only
        once the cooldown has elapsed since the last attempt; until then the cached set stands in
        for the fetch. With nothing cached there is nothing to stand in, so a fetch is due.

        A fetch that would be due is still not made while a failed attempt is backing off: the
        set is served if it may still be (`_servable_stale`), and otherwise this raises the
        refusal the failed attempt met. Raising here, not returning `None`, is what keeps a
        caller from fetching in spite of the backoff; a forced call needs the cooldown AND the
        backoff to be over before a fetch is due.
        """
        now = monotonic()
        held = self._jwks
        if held is not None:
            if not force:
                if now < self._expires_at:
                    return held
            else:
                last = self._last_fetch_at
                if last is not None and now - last < JWKS_UNKNOWN_KID_REFETCH_COOLDOWN_SECONDS:
                    return held
        retry_after = self._retry_after
        if retry_after is not None and now < retry_after:
            stale = self._servable_stale(now)
            if stale is not None:
                return stale
            raise OidcVerificationError(self._failure_reason or _JWKS_UNAVAILABLE)
        return None

    async def _load_jwks(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._current_key_set(force=force)
        if cached is not None:
            return cached
        async with self._lock:
            # Looked at again under the lock, not just before it. The window is measured from the
            # END of the last attempt, so a caller that passed the check above while a fetch was
            # in flight was looking at the attempt before that one. Once the lock is released it
            # must see the fetch that just finished -- its key set and its timestamp, or the
            # backoff it started -- or a burst that arrives during one fetch would fetch one after
            # another, the exact amplification the cooldown and the backoff exist to end.
            cached = self._current_key_set(force=force)
            if cached is not None:
                return cached
            return await self._refresh(force=force)

    async def _refresh(self, *, force: bool) -> dict[str, Any]:
        """One attempt to load the key set, and what its outcome does to the state above.

        Called with the lock held and a load due. What the attempt learned is recorded before the
        lock is released, whatever the outcome, so the caller queued behind this one sees it.
        """
        try:
            jwks = await self._fetch_jwks()
        except Exception as exc:
            self._note_failure(exc)
            # An expiry-driven refresh that fails is answered from the last good set if it may
            # still be served: the request that paid for the failed attempt must not be the one
            # in each window that is refused while every other is not. A forced (unknown-`kid`)
            # refetch is different -- the set held is by definition missing the key the token
            # names, so there is nothing stale to serve it with, and the caller is told what it
            # would want to know (the provider could not be asked) rather than that the key is
            # unknown, as before.
            stale = None if force else self._servable_stale(monotonic())
            if stale is None:
                raise
            return stale
        else:
            self._note_success(jwks)
            return jwks
        finally:
            # Stamped when the attempt ends, and stamped on failure too: a provider that is down
            # must not be asked again by every unknown-`kid` request that follows. Also stamped if
            # the caller is cancelled mid-fetch (which `except Exception` does not see, so no
            # backoff starts and nothing is recorded as a failure) -- a client that hangs up must
            # not buy the next caller a fetch it would not otherwise get.
            self._last_fetch_at = monotonic()

    def _note_success(self, jwks: dict[str, Any]) -> None:
        recovered = self._retry_after is not None
        self._jwks = jwks
        self._expires_at = monotonic() + self.settings.oidc_jwks_cache_seconds
        self._retry_after = None
        self._failure_reason = None
        self._stale_allowed = True
        if recovered:
            _log.info("oidc_jwks_refresh_recovered", key_count=len(jwks["keys"]))

    def _note_failure(self, exc: Exception) -> None:
        """Start the backoff. `_jwks` and `_expires_at` are left describing the last good key set.

        Every `Exception` counts, not only this module's own: the backoff exists so that nothing
        the provider (or the network) does can make this process retry on every request, and an
        exception class this code did not foresee is exactly that.
        """
        now = monotonic()
        reason = str(exc) if isinstance(exc, OidcVerificationError) else _JWKS_UNAVAILABLE
        self._retry_after = now + JWKS_REFRESH_FAILURE_BACKOFF_SECONDS
        self._failure_reason = reason
        self._stale_allowed = not isinstance(exc, JwksRejected)
        held = self._jwks
        # One line per failed attempt, so at most one per backoff window per process: an operator
        # learns that the provider is unreachable and how long the stale key set has left, rather
        # than a quiet stretch of successful requests. The cause's type (`ConnectError`,
        # `ReadTimeout`, `HTTPStatusError`) says how it failed; nothing from the provider's answer
        # is logged.
        _log.warning(
            "oidc_jwks_refresh_failed",
            reason=reason,
            error_type=type(exc.__cause__ or exc).__name__,
            key_set_held=held is not None,
            stale_seconds_remaining=(
                round(max(0.0, self._expires_at + JWKS_STALE_KEY_SET_GRACE_SECONDS - now), 1)
                if held is not None
                else None
            ),
            retry_in_seconds=JWKS_REFRESH_FAILURE_BACKOFF_SECONDS,
        )

    async def _fetch_jwks(self) -> dict[str, Any]:
        """Read and validate the key set from wherever this deployment keeps it.

        Only the mechanism: when to call it is `_load_jwks`'s decision.
        """
        if self.settings.oidc_jwks_json:
            try:
                jwks = json.loads(self.settings.oidc_jwks_json)
            except json.JSONDecodeError as exc:
                raise JwksRejected("pinned OIDC JWKS JSON is invalid") from exc
        elif self.settings.oidc_jwks_url:
            try:
                async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                    response = await client.get(self.settings.oidc_jwks_url)
                    response.raise_for_status()
                    if len(response.content) > MAX_JWKS_BYTES:
                        raise JwksRejected("OIDC JWKS document exceeds the size limit")
                    jwks = response.json()
            except httpx.HTTPStatusError as exc:
                # A 5xx or a 429 is the provider failing or shedding load: an outage. Any other
                # non-2xx (a 404, a 410, a 401, a redirect) is an answer, and not a key set.
                status = exc.response.status_code
                if status >= 500 or status == 429:
                    raise OidcVerificationError(_JWKS_UNAVAILABLE) from exc
                raise JwksRejected(_JWKS_UNAVAILABLE) from exc
            except httpx.HTTPError as exc:
                # Connect, read and write errors and timeouts: the provider could not be asked.
                raise OidcVerificationError(_JWKS_UNAVAILABLE) from exc
            except ValueError as exc:
                raise JwksRejected(_JWKS_UNAVAILABLE) from exc
        else:
            raise OidcVerificationError("OIDC JWKS is not configured")
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise JwksRejected("OIDC JWKS document has an invalid shape")
        keys = jwks["keys"]
        if not keys or len(keys) > MAX_JWKS_KEYS or not all(isinstance(key, dict) for key in keys):
            raise JwksRejected("OIDC JWKS key set has an invalid shape")
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
            # An unknown `kid` is usually a rotation the cache has not seen, so look once more --
            # but only once per cooldown (R11-AUD09). The `kid` comes from the header of a token
            # nobody has authenticated yet, so an unlimited refetch here let any caller make this
            # API call the identity provider once per request. Inside the window `_load_jwks`
            # hands back the set already held, and the token is refused just below, as before.
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
        except jwt.ExpiredSignatureError as exc:
            # Before the generic clause: `ExpiredSignatureError` is a
            # `PyJWTError`, so the order is what keeps expiry distinguishable.
            raise OidcTokenExpired("bearer token has expired") from exc
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise OidcVerificationError("bearer token verification failed") from exc
        if not isinstance(claims, dict):
            raise OidcVerificationError("bearer token claims are invalid")
        return claims
