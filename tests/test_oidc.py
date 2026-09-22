import ast
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric import rsa
from structlog.testing import capture_logs

import aida.oidc as oidc_module
from aida.config import Settings
from aida.oidc import (
    JWKS_REFRESH_FAILURE_BACKOFF_SECONDS,
    JWKS_STALE_KEY_SET_GRACE_SECONDS,
    JWKS_UNKNOWN_KID_REFETCH_COOLDOWN_SECONDS,
    OidcTokenExpired,
    OidcVerificationError,
    OidcVerifier,
    context_from_claims,
)


def oidc_fixture() -> tuple[Settings, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "bank-key-1", "use": "sig", "alg": "RS256"})
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json=json.dumps({"keys": [jwk]}),
        oidc_role_mappings={"BANK_ANALYST": ["Analyst", "Viewer"]},
    )
    return settings, private_key


@pytest.mark.asyncio
async def test_oidc_verifies_signature_issuer_audience_and_claim_mapping() -> None:
    settings, private_key = oidc_fixture()
    organization_id = uuid4()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "roles": ["BANK_ANALYST", "UNRECOGNIZED_ROLE"],
            "organization_id": str(organization_id),
            "principal_type": "USER",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    claims = await OidcVerifier(settings).verify(token)
    context = context_from_claims(claims, settings)

    assert context.principal_id == "bank-user-123"
    assert context.organization_id == organization_id
    assert context.roles == frozenset({"Analyst", "Viewer"})


@pytest.mark.asyncio
async def test_oidc_rejects_wrong_audience() -> None:
    settings, private_key = oidc_fixture()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": "different-product",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(token)


@pytest.mark.asyncio
async def test_oidc_rejects_malformed_matching_jwk_without_internal_error() -> None:
    settings, private_key = oidc_fixture()
    settings = settings.model_copy(
        update={
            "oidc_jwks_json": json.dumps(
                {"keys": [{"kid": "bank-key-1", "kty": "RSA", "alg": "RS256"}]}
            )
        }
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(token)


def test_oidc_rejects_invalid_organization_claim() -> None:
    settings, _ = oidc_fixture()

    with pytest.raises(OidcVerificationError, match="not a UUID"):
        context_from_claims({"sub": "bank-user-123", "organization_id": "not-a-uuid"}, settings)


# ---------------------------------------------------------------------------
# The role mapping is a closed contract
# ---------------------------------------------------------------------------
#
# `context_from_claims` read `oidc_role_mappings.get(role, [role])` and kept
# whatever survived `in PLATFORM_ROLES`. Together those two lines let a token
# grant itself any platform role by naming it: the fallback handed the role's
# own string through, and the filter -- which reads like a guard -- was what
# admitted it, because `PlatformAdmin` is of course in `PLATFORM_ROLES`.
#
# Verified end to end against a real OIDC issuer before the fix: a token
# claiming `["totally-unmapped-group", "PlatformAdmin"]`, against a deployment
# whose mapping named only `atlas-admin`/`atlas-steward`/`atlas-viewer`, came
# back from `/v1/me` as `roles: ["PlatformAdmin"]`.
#
# The reason it survived this file is worth recording: the existing mapping
# test used `"UNRECOGNIZED_ROLE"`, a name that does not collide with the
# platform's vocabulary, so it proved only that *unknown* names are dropped --
# never that a *known* name cannot be claimed. These close that gap.


def _claims(roles: list[str]) -> dict[str, object]:
    return {
        "sub": "bank-user-123",
        "roles": roles,
        "organization_id": str(uuid4()),
        "principal_type": "USER",
    }


def test_a_token_cannot_grant_itself_a_platform_role_by_naming_it() -> None:
    settings, _ = oidc_fixture()  # maps BANK_ANALYST only

    context = context_from_claims(_claims(["PlatformAdmin"]), settings)

    assert context.roles == frozenset(), (
        "a token named a platform role and was granted it; the external role "
        "mapping must be a closed contract, not a passthrough with a filter"
    )


def test_a_platform_role_named_alongside_a_mapped_one_is_still_refused() -> None:
    """The escalation is most dangerous in this shape: a legitimate group plus
    an appended platform role name, where the principal genuinely has *some*
    access and the reviewer sees a plausible claim."""
    settings, _ = oidc_fixture()

    context = context_from_claims(_claims(["BANK_ANALYST", "PlatformAdmin"]), settings)

    assert context.roles == frozenset({"Analyst", "Viewer"})
    assert "PlatformAdmin" not in context.roles


def test_a_deployment_with_no_mapping_grants_nothing() -> None:
    """`oidc_role_mappings` defaults to `{}`, so this was the shipped default:
    with no mapping configured at all, every deployment accepted whatever its
    IdP called a role. Granting nothing is loudly wrong rather than quietly
    dangerous (INV-4)."""
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json='{"keys": []}',
    )

    context = context_from_claims(_claims(["PlatformAdmin", "Analyst"]), settings)

    assert context.roles == frozenset()


def test_a_name_can_be_accepted_as_is_by_mapping_it_to_itself() -> None:
    """The documented escape, so closing the default costs no capability.

    A deployment whose IdP deliberately emits platform role names says so
    explicitly. That is self-documenting in the configuration a reviewer reads,
    which is the whole difference from an implicit passthrough.
    """
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json='{"keys": []}',
        oidc_role_mappings={"Analyst": ["Analyst"]},
    )

    context = context_from_claims(_claims(["Analyst", "PlatformAdmin"]), settings)

    assert context.roles == frozenset({"Analyst"})


# --------------------------------------------------------------------------- #
# R11-D6: an expired token is not the same refusal as a rejected one
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_expired_token_is_reported_as_expired() -> None:
    """Found in a browser, not by reading: a token the deployment rejected for
    its audience produced the same detail as an expired one, and the shell told
    the operator to sign in again -- which could never work. Expiry is the one
    refusal a caller can act on, and disclosing it is safe because the holder
    can read `exp` themselves."""
    settings, private_key = oidc_fixture()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            # Ten minutes past, well beyond the 30-second default leeway.
            "iat": now - timedelta(minutes=20),
            "exp": now - timedelta(minutes=10),
            "principal_type": "USER",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    with pytest.raises(OidcTokenExpired) as excinfo:
        await OidcVerifier(settings).verify(token)

    # Still a verification failure: every existing handler still denies it.
    assert isinstance(excinfo.value, OidcVerificationError)


@pytest.mark.asyncio
async def test_a_rejected_token_is_not_reported_as_expired() -> None:
    """The other direction, and the one that matters for INV-4: a token that
    failed for any reason other than time must not be called expired, or the
    distinction would leak why a forged or misdirected token was refused."""
    settings, private_key = oidc_fixture()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": "some-other-service",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "principal_type": "USER",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    with pytest.raises(OidcVerificationError) as excinfo:
        await OidcVerifier(settings).verify(token)

    assert not isinstance(excinfo.value, OidcTokenExpired)


# --------------------------------------------------------------------------- #
# R11-AUD09: an unknown key id must not turn into a fetch from the identity provider
# --------------------------------------------------------------------------- #
#
# `verify` reloaded the issuer's key set for every token whose `kid` the cached set did not
# hold, and nothing limited how often. A `kid` is read from the header before the signature is
# checked, so anyone who could send a request -- no key, no account -- could make the API call
# the identity provider once per request. While the provider was down, each of those requests
# retried the fetch as well.
#
# The provider here is a counting fake behind `httpx.MockTransport`, so the tests count real
# fetches through the real `httpx` call, and time is `aida.oidc.monotonic` replaced by a clock
# the test moves. Nothing sleeps.

_ISSUER = "https://identity.bank.example"
_JWKS_URL = "https://identity.bank.example/.well-known/jwks.json"
COOLDOWN = JWKS_UNKNOWN_KID_REFETCH_COOLDOWN_SECONDS


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


class _Clock:
    """Stands in for `aida.oidc.monotonic`: a test moves time instead of waiting for it."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _IdentityProvider:
    """The JWKS endpoint. Counts how often it is asked; can be down, slow, or publish new keys."""

    def __init__(self, keys: list[dict[str, Any]], clock: _Clock) -> None:
        self.keys = keys
        self.clock = clock
        self.fetches = 0
        self.down = False
        self.fetch_takes = 0.0
        # While set, an arriving request is held until the test opens it: a fetch that is still in
        # flight, and so can be cancelled.
        self.hold: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        self.clock.advance(self.fetch_takes)
        if self.hold is not None:
            await self.hold.wait()
        # A real network read yields to the event loop. Yielding here lets a caller that arrives
        # while this fetch is still in flight run now, and meet the lock.
        await asyncio.sleep(0)
        if self.down:
            raise httpx.ConnectError("identity provider is down", request=request)
        return httpx.Response(200, json={"keys": self.keys})


@dataclass
class _Rig:
    settings: Settings
    verifier: OidcVerifier
    idp: _IdentityProvider
    clock: _Clock
    current_key: rsa.RSAPrivateKey
    rotated_key: rsa.RSAPrivateKey

    def token(self, kid: str, key: rsa.RSAPrivateKey | None = None) -> str:
        """A well-formed token. Unless `key` is given it is signed with the provider's current
        key -- which an unknown `kid` never gets far enough to check."""
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": "bank-user-123",
                "iss": _ISSUER,
                "aud": "atlas",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            key or self.current_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

    def rotate(self) -> None:
        """The provider starts signing with a second key and publishes it beside the first."""
        self.idp.keys = [
            _public_jwk(self.current_key, "bank-key-1"),
            _public_jwk(self.rotated_key, "bank-key-2"),
        ]

    async def refused_as_unknown(self, token: str) -> None:
        with pytest.raises(OidcVerificationError, match="signing key is unknown"):
            await self.verifier.verify(token)


@pytest.fixture(scope="module")
def signing_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    """Two issuer keys, made once: RSA key generation dominates these tests' run time."""
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


@pytest.fixture
def rig(
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> _Rig:
    current_key, rotated_key = signing_keys
    clock = _Clock()
    idp = _IdentityProvider([_public_jwk(current_key, "bank-key-1")], clock)
    real_client = httpx.AsyncClient

    def client_with_fake_provider(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(idp.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("aida.oidc.httpx.AsyncClient", client_with_fake_provider)
    monkeypatch.setattr("aida.oidc.monotonic", clock)
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer=_ISSUER,
        oidc_audience="atlas",
        oidc_jwks_url=_JWKS_URL,
        oidc_role_mappings={"BANK_ANALYST": ["Analyst"]},
    )
    return _Rig(settings, OidcVerifier(settings), idp, clock, current_key, rotated_key)


@pytest.mark.asyncio
async def test_a_burst_of_unknown_key_ids_makes_one_fetch(rig: _Rig) -> None:
    """The finding itself. Twenty tokens, each naming a key id the provider never issued: before
    the limit, every one of them was a fetch from the identity provider."""
    for index in range(20):
        await rig.refused_as_unknown(rig.token(f"attacker-{index}"))

    # One fetch: the cold load the first token needed anyway. None of the twenty forced a
    # second, because the key set had been fetched a moment earlier and is as fresh as another
    # fetch could make it.
    assert rig.idp.fetches == 1


@pytest.mark.asyncio
async def test_a_concurrent_burst_makes_one_fetch_not_one_each(rig: _Rig) -> None:
    """Sequential callers are the easy case. Here every caller is inside `verify` at once, the
    window has elapsed so the burst IS entitled to a refetch, and the first fetch is still in
    flight when the rest arrive. The lock lines them up; each must then find the finished
    fetch's result waiting rather than start its own."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.clock.advance(COOLDOWN + 1)

    results = await asyncio.gather(
        *(rig.verifier.verify(rig.token(f"attacker-{index}")) for index in range(20)),
        return_exceptions=True,
    )

    assert len(results) == 20
    assert all(
        isinstance(result, OidcVerificationError) and "signing key is unknown" in str(result)
        for result in results
    )
    # The warm-up, plus one for the whole burst.
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_a_caller_queued_behind_a_slow_fetch_does_not_start_another(rig: _Rig) -> None:
    """The case that fixes WHEN the window is measured from. The second caller passed the
    "has the cooldown elapsed?" check long before the first fetch finished -- that fetch took
    twice the cooldown -- so only the fetch that just ended says another would be pointless. A
    window measured from the START of the last fetch would let it fetch again immediately."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.clock.advance(COOLDOWN + 1)
    rig.idp.fetch_takes = 2 * COOLDOWN

    results = await asyncio.gather(
        rig.verifier.verify(rig.token("attacker-0")),
        rig.verifier.verify(rig.token("attacker-1")),
        return_exceptions=True,
    )

    assert all(
        isinstance(result, OidcVerificationError) and "signing key is unknown" in str(result)
        for result in results
    )
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_the_next_refetch_is_allowed_only_once_the_cooldown_has_elapsed(rig: _Rig) -> None:
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 1

    # One second short of the window: answered from the key set already held.
    rig.clock.advance(COOLDOWN - 1)
    await rig.refused_as_unknown(rig.token("attacker-a"))
    assert rig.idp.fetches == 1

    # The window has elapsed: exactly one refetch. (The key is still unknown afterwards -- the
    # provider never issued it -- so the token is refused as before.)
    rig.clock.advance(1)
    await rig.refused_as_unknown(rig.token("attacker-b"))
    assert rig.idp.fetches == 2

    # That fetch restarted the window, so what follows is limited again ...
    rig.clock.advance(1)
    await rig.refused_as_unknown(rig.token("attacker-c"))
    assert rig.idp.fetches == 2

    # ... until it elapses in turn.
    rig.clock.advance(COOLDOWN)
    await rig.refused_as_unknown(rig.token("attacker-d"))
    assert rig.idp.fetches == 3


@pytest.mark.asyncio
async def test_a_legitimate_rotation_is_refused_inside_the_cooldown_and_accepted_after_it(
    rig: _Rig,
) -> None:
    """The price of the limit, pinned so it cannot grow unnoticed: a token signed with a key
    the provider has just published is refused until the window since the last fetch has
    elapsed, and the first token after that pays for the fetch and is accepted."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.rotate()
    new_key_token = rig.token("bank-key-2", rig.rotated_key)

    rig.clock.advance(5)
    await rig.refused_as_unknown(new_key_token)
    assert rig.idp.fetches == 1

    rig.clock.advance(COOLDOWN - 5)
    claims = await rig.verifier.verify(new_key_token)
    assert claims["sub"] == "bank-user-123"
    assert rig.idp.fetches == 2

    # Picked up for good: both keys verify now, and neither costs another fetch.
    await rig.verifier.verify(rig.token("bank-key-2", rig.rotated_key))
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_a_failed_refetch_counts_as_an_attempt_and_keeps_the_cached_keys(
    rig: _Rig,
) -> None:
    """A provider outage must not become a request-rate amplifier: the failed attempt starts the
    window like a successful one, and it must not cost the cache the good key set it holds."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.idp.down = True
    rig.clock.advance(COOLDOWN + 1)

    # The one request entitled to a refetch makes it, and learns the provider is down.
    with pytest.raises(OidcVerificationError, match="endpoint is unavailable"):
        await rig.verifier.verify(rig.token("attacker-0"))
    assert rig.idp.fetches == 2

    # Everything after it, inside the window, is answered from the cached set rather than with
    # another attempt on a provider that is not answering.
    for index in range(1, 11):
        await rig.refused_as_unknown(rig.token(f"attacker-{index}"))
    assert rig.idp.fetches == 2

    # The failure did not poison the cache: the key already held still verifies, with no fetch.
    claims = await rig.verifier.verify(rig.token("bank-key-1"))
    assert claims["sub"] == "bank-user-123"
    assert rig.idp.fetches == 2

    # Once the window has passed and the provider is back, a rotation is picked up.
    rig.idp.down = False
    rig.rotate()
    rig.clock.advance(COOLDOWN)
    await rig.verifier.verify(rig.token("bank-key-2", rig.rotated_key))
    assert rig.idp.fetches == 3


@pytest.mark.asyncio
async def test_cache_expiry_still_refreshes_the_key_set(rig: _Rig) -> None:
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 1

    rig.clock.advance(rig.settings.oidc_jwks_cache_seconds + 1)
    await rig.verifier.verify(rig.token("bank-key-1"))

    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_the_cache_expiry_refresh_is_not_held_back_by_the_cooldown(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cooldown belongs to the unknown-key path alone. The shipped numbers hide that -- 30
    seconds against a 30 second floor on the cache lifetime -- so this makes the cooldown four
    times the cache lifetime to show the ordinary refresh never consults it. Otherwise a later
    edit to either number could leave an expired key set (say, one holding a withdrawn key)
    being served past its expiry."""
    monkeypatch.setattr("aida.oidc.JWKS_UNKNOWN_KID_REFETCH_COOLDOWN_SECONDS", 120.0)
    verifier = OidcVerifier(rig.settings.model_copy(update={"oidc_jwks_cache_seconds": 30}))
    await verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 1

    rig.clock.advance(31)
    await verifier.verify(rig.token("bank-key-1"))

    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_a_known_key_id_never_causes_a_fetch_while_the_cache_is_current(rig: _Rig) -> None:
    """The cooldown is about unknown key ids. A token whose `kid` is held is answered from the
    cache however much time has passed since the last fetch, until the cache itself expires."""
    await rig.verifier.verify(rig.token("bank-key-1"))

    for _ in range(5):
        rig.clock.advance(COOLDOWN + 1)
        await rig.verifier.verify(rig.token("bank-key-1"))

    assert rig.clock.now - 1_000.0 < rig.settings.oidc_jwks_cache_seconds
    assert rig.idp.fetches == 1


@pytest.mark.asyncio
async def test_an_unknown_key_id_against_pinned_keys_is_still_refused_as_unknown() -> None:
    """Pinned keys go through the same load path with no network behind it: the outcome for an
    unknown `kid` is unchanged."""
    settings, private_key = oidc_fixture()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "not-pinned"},
    )

    with pytest.raises(OidcVerificationError, match="signing key is unknown"):
        await OidcVerifier(settings).verify(token)


# --------------------------------------------------------------------------- #
# R11-AUD10: a provider that is down is not asked again by every request
# --------------------------------------------------------------------------- #
#
# R11-AUD09 limited the refetch an unknown key id can cause and left the ordinary refresh alone:
# once the cached key set had expired, every request retried the fetch while the provider did not
# answer. A failed attempt now starts a backoff during which no attempt is made, the last good key
# set is served for a bounded time meanwhile, and past that bound a request is refused as if no key
# set had ever loaded. Same rig, same counting fake provider, same clock the test moves.

BACKOFF = JWKS_REFRESH_FAILURE_BACKOFF_SECONDS
GRACE = JWKS_STALE_KEY_SET_GRACE_SECONDS


def _cache_seconds(rig: _Rig) -> float:
    return float(rig.settings.oidc_jwks_cache_seconds)


async def _expired_with_the_provider_down(rig: _Rig) -> None:
    """A verifier holding a good key set whose cache just expired, and a provider that is down."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 1
    rig.idp.down = True
    rig.clock.advance(_cache_seconds(rig) + 1)


@pytest.mark.asyncio
async def test_a_burst_after_expiry_against_a_dead_provider_makes_one_fetch_per_window(
    rig: _Rig,
) -> None:
    """The finding itself. No unknown key id anywhere: every request names the key the provider
    published. Before the backoff each of them retried a fetch that could not succeed."""
    await _expired_with_the_provider_down(rig)

    for _ in range(20):
        claims = await rig.verifier.verify(rig.token("bank-key-1"))
        assert claims["sub"] == "bank-user-123"
    # The warm-up, plus ONE attempt for the whole burst: the request that found the cache expired.
    # The other nineteen were answered from the last good key set.
    assert rig.idp.fetches == 2

    # Inside the window, however long the requests keep coming, still no attempt ...
    rig.clock.advance(BACKOFF - 1)
    for _ in range(20):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2

    # ... and once it has elapsed, exactly one, which starts the next window.
    rig.clock.advance(1)
    for _ in range(20):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 3


@pytest.mark.asyncio
async def test_a_cold_start_against_a_dead_provider_makes_one_fetch_per_window(rig: _Rig) -> None:
    """The same amplification with nothing cached yet: a worker that starts while the provider is
    down has no key set to serve, so it refuses -- but it must not ask again on every request."""
    rig.idp.down = True

    for _ in range(20):
        with pytest.raises(OidcVerificationError, match="^OIDC JWKS endpoint is unavailable$"):
            await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 1

    rig.clock.advance(BACKOFF - 1)
    with pytest.raises(OidcVerificationError, match="endpoint is unavailable"):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 1

    rig.clock.advance(1)
    with pytest.raises(OidcVerificationError, match="endpoint is unavailable"):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_a_refusal_inside_the_window_repeats_the_reason_the_attempt_failed_for(
    rig: _Rig,
) -> None:
    """Nothing is held and the provider answers, but not with a key set. Every request in the
    window is refused for that reason, not for a vaguer one, and none asks again."""
    rig.idp.keys = []

    for _ in range(5):
        with pytest.raises(OidcVerificationError, match="^OIDC JWKS key set has an invalid shape$"):
            await rig.verifier.verify(rig.token("bank-key-1"))

    assert rig.idp.fetches == 1


@pytest.mark.asyncio
async def test_stale_keys_verify_inside_the_limit_and_are_refused_after_it(rig: _Rig) -> None:
    """The bound that makes serving stale keys acceptable: a withdrawn key must not go on verifying
    for as long as the provider stays down. Past the limit the key set is not served at all, and
    the refusal is exactly the one a verifier that never loaded one gives."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.idp.down = True

    # The last second the limit allows: the key set expired GRACE - 1 seconds ago.
    rig.clock.advance(_cache_seconds(rig) + GRACE - 1)
    claims = await rig.verifier.verify(rig.token("bank-key-1"))
    assert claims["sub"] == "bank-user-123"
    # The request that found the cache expired made the one attempt, and was served regardless.
    assert rig.idp.fetches == 2

    # One second on, the key set is past the limit. It is refused, with no new attempt: the
    # backoff that attempt started is still running.
    rig.clock.advance(1)
    with pytest.raises(OidcVerificationError, match="^OIDC JWKS endpoint is unavailable$"):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2

    # It stays refused as the windows go by, with one attempt in each.
    for attempt in range(1, 4):
        rig.clock.advance(BACKOFF)
        with pytest.raises(OidcVerificationError, match="^OIDC JWKS endpoint is unavailable$"):
            await rig.verifier.verify(rig.token("bank-key-1"))
        assert rig.idp.fetches == 2 + attempt


@pytest.mark.asyncio
async def test_a_failed_attempt_beyond_the_limit_is_refused_not_served(rig: _Rig) -> None:
    """The limit is enforced by the key set's age, not by whether a backoff happens to be running:
    the request that pays for the attempt past the limit gets the failure, not the stale set."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.idp.down = True
    rig.clock.advance(_cache_seconds(rig) + GRACE + 1)

    with pytest.raises(OidcVerificationError, match="endpoint is unavailable"):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2


def test_the_stale_limit_and_the_backoff_are_short_fixed_bounds() -> None:
    """Pinned so they cannot grow unnoticed. Raising the stale limit widens, one for one, how long
    a signing key withdrawn for compromise keeps verifying while the provider is unreachable."""
    assert 0 < JWKS_REFRESH_FAILURE_BACKOFF_SECONDS <= 120
    assert 0 < JWKS_STALE_KEY_SET_GRACE_SECONDS <= 3600


@pytest.mark.asyncio
async def test_an_unknown_key_id_during_the_stale_period_is_refused_as_unknown_without_a_fetch(
    rig: _Rig,
) -> None:
    """The unknown-key cooldown works exactly as before while a stale key set is being served."""
    await _expired_with_the_provider_down(rig)
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2

    for index in range(10):
        await rig.refused_as_unknown(rig.token(f"attacker-{index}"))
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_the_backoff_holds_back_the_unknown_key_refetch_after_its_own_cooldown(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cooldown and the backoff are two windows and a refetch needs both to be over. The
    shipped numbers are equal, which hides that, so this makes the backoff four times the cooldown:
    otherwise a provider that just failed would be asked again as soon as an unknown key id came
    along after the (shorter) cooldown."""
    monkeypatch.setattr("aida.oidc.JWKS_REFRESH_FAILURE_BACKOFF_SECONDS", 4 * COOLDOWN)
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.idp.down = True
    rig.clock.advance(COOLDOWN + 1)

    # A refetch is due (the cooldown is over), fails, and is reported as the outage it is.
    with pytest.raises(OidcVerificationError, match="endpoint is unavailable"):
        await rig.verifier.verify(rig.token("attacker-0"))
    assert rig.idp.fetches == 2

    # Another cooldown on, the cooldown is over again but the backoff is not: no attempt.
    rig.clock.advance(COOLDOWN + 1)
    await rig.refused_as_unknown(rig.token("attacker-1"))
    assert rig.idp.fetches == 2

    # The backoff has elapsed: now the attempt is made.
    rig.clock.advance(3 * COOLDOWN)
    with pytest.raises(OidcVerificationError, match="endpoint is unavailable"):
        await rig.verifier.verify(rig.token("attacker-2"))
    assert rig.idp.fetches == 3


@pytest.mark.asyncio
async def test_recovery_is_noticed_at_the_end_of_the_window_and_ends_the_backoff(
    rig: _Rig,
) -> None:
    await _expired_with_the_provider_down(rig)
    await rig.verifier.verify(rig.token("bank-key-1"))  # the failed attempt, served stale
    assert rig.idp.fetches == 2

    # The provider comes back and publishes a rotation, but the window is still open: it is not
    # asked yet, so the new key is unknown and the old one keeps verifying from the stale set.
    rig.idp.down = False
    rig.rotate()
    rig.clock.advance(BACKOFF - 1)
    await rig.refused_as_unknown(rig.token("bank-key-2", rig.rotated_key))
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 2

    # The window has elapsed: the next request refreshes, and both keys verify from then on.
    rig.clock.advance(1)
    await rig.verifier.verify(rig.token("bank-key-2", rig.rotated_key))
    assert rig.idp.fetches == 3
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 3

    # The failure is forgotten with the backoff it started ...
    assert rig.verifier._retry_after is None
    assert rig.verifier._failure_reason is None

    # ... the key set is fresh for a whole cache lifetime again, and when that ends the refresh
    # is immediate: no window is left over from the outage.
    rig.clock.advance(_cache_seconds(rig) - 1)
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 3
    rig.clock.advance(2)
    await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 4


@pytest.mark.asyncio
async def test_a_second_outage_after_recovery_starts_a_window_of_its_own(rig: _Rig) -> None:
    await _expired_with_the_provider_down(rig)
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.idp.down = False
    rig.clock.advance(BACKOFF)
    await rig.verifier.verify(rig.token("bank-key-1"))  # recovered
    assert rig.idp.fetches == 3

    rig.idp.down = True
    rig.clock.advance(_cache_seconds(rig) + 1)
    for _ in range(10):
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert rig.idp.fetches == 4


@pytest.mark.asyncio
async def test_a_concurrent_burst_after_expiry_with_a_dead_provider_makes_one_attempt(
    rig: _Rig,
) -> None:
    """Concurrent callers still cause one fetch, on the failure path too. Every caller is inside
    `verify` when the first attempt is still in flight; the lock lines them up, and each must then
    find the failed attempt's backoff waiting rather than start an attempt of its own."""
    await _expired_with_the_provider_down(rig)

    results = await asyncio.gather(
        *(rig.verifier.verify(rig.token("bank-key-1")) for _ in range(20))
    )

    assert all(claims["sub"] == "bank-user-123" for claims in results)
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_a_concurrent_cold_burst_against_a_dead_provider_makes_one_attempt(
    rig: _Rig,
) -> None:
    rig.idp.down = True

    results = await asyncio.gather(
        *(rig.verifier.verify(rig.token("bank-key-1")) for _ in range(20)),
        return_exceptions=True,
    )

    assert all(
        isinstance(result, OidcVerificationError) and "endpoint is unavailable" in str(result)
        for result in results
    )
    assert rig.idp.fetches == 1


@pytest.mark.asyncio
async def test_an_empty_key_set_is_a_failed_refresh_and_does_not_replace_the_good_one(
    rig: _Rig,
) -> None:
    """A provider answering, but with nothing usable, is a failed refresh like one that does not
    answer: the good key set is kept and served stale, and the answer is not asked for again."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.idp.keys = []
    rig.clock.advance(_cache_seconds(rig) + 1)

    for _ in range(5):
        claims = await rig.verifier.verify(rig.token("bank-key-1"))
        assert claims["sub"] == "bank-user-123"
    assert rig.idp.fetches == 2


@pytest.mark.asyncio
async def test_a_cancelled_refresh_is_not_a_provider_failure(rig: _Rig) -> None:
    """A caller that goes away mid-fetch learned nothing about the provider, so it starts no
    backoff: the next request makes its own attempt straight away."""
    await rig.verifier.verify(rig.token("bank-key-1"))
    rig.clock.advance(_cache_seconds(rig) + 1)
    rig.idp.hold = asyncio.Event()

    task = asyncio.ensure_future(rig.verifier.verify(rig.token("bank-key-1")))
    for _ in range(100):
        if rig.idp.fetches == 2:
            break
        await asyncio.sleep(0)
    assert rig.idp.fetches == 2, "the refresh should be in flight"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rig.idp.hold = None
    claims = await rig.verifier.verify(rig.token("bank-key-1"))
    assert claims["sub"] == "bank-user-123"
    assert rig.idp.fetches == 3


@pytest.mark.asyncio
async def test_pinned_keys_are_unaffected_by_the_backoff_and_the_stale_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned key set has no network behind it and no failure to back off from. However far past
    expiry, the stale limit and the backoff the clock runs, it is re-read and keeps verifying."""
    clock = _Clock()
    monkeypatch.setattr("aida.oidc.monotonic", clock)

    def no_network(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        raise AssertionError("a pinned key set must not touch the network")

    monkeypatch.setattr("aida.oidc.httpx.AsyncClient", no_network)
    settings, private_key = oidc_fixture()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )
    verifier = OidcVerifier(settings)

    for _ in range(3):
        claims = await verifier.verify(token)
        assert claims["sub"] == "bank-user-123"
        clock.advance(settings.oidc_jwks_cache_seconds + GRACE + BACKOFF + 1)

    assert verifier._retry_after is None


@pytest.fixture
def fresh_oidc_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module logger with no history, so `capture_logs` sees it: the application's logging setup
    caches loggers on first use, and a logger an earlier test drove keeps its old processors."""
    monkeypatch.setattr(oidc_module, "_log", structlog.get_logger("aida.oidc"))


@pytest.mark.asyncio
async def test_a_failed_refresh_is_logged_once_per_window_and_recovery_is_logged(
    rig: _Rig, fresh_oidc_logger: None
) -> None:
    """Serving stale keys must not be silent. One line per failed attempt (so one per window),
    saying how long the stale key set has left, and one line when the provider is back."""
    await _expired_with_the_provider_down(rig)

    with capture_logs() as logs:
        for _ in range(10):
            await rig.verifier.verify(rig.token("bank-key-1"))

    failures = [entry for entry in logs if entry["event"] == "oidc_jwks_refresh_failed"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["log_level"] == "warning"
    assert failure["reason"] == "OIDC JWKS endpoint is unavailable"
    assert failure["error_type"] == "ConnectError"
    assert failure["key_set_held"] is True
    assert failure["stale_seconds_remaining"] == GRACE - 1  # the cache expired one second ago
    assert failure["retry_in_seconds"] == BACKOFF

    rig.idp.down = False
    rig.clock.advance(BACKOFF)
    with capture_logs() as logs:
        await rig.verifier.verify(rig.token("bank-key-1"))
    assert [entry["event"] for entry in logs] == ["oidc_jwks_refresh_recovered"]


def test_the_verifier_identity_names_every_setting_the_verifier_reads() -> None:
    """`shared_oidc_verifier` keeps one verifier per identity. If the class starts reading a
    setting the identity does not name, two configurations that differ only in it would share a
    verifier, and one would be verified with the other's value. Read from the two source files
    rather than imported, so it sees exactly what is written there."""
    oidc_path = Path(oidc_module.__file__)
    oidc_tree = ast.parse(oidc_path.read_text(encoding="utf-8"))
    security_tree = ast.parse(oidc_path.with_name("security.py").read_text(encoding="utf-8"))
    verifier_class = next(
        node
        for node in oidc_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OidcVerifier"
    )
    identity_function = next(
        node
        for node in security_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "shared_oidc_verifier"
    )

    read = {
        node.attr
        for node in ast.walk(verifier_class)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "settings"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    }
    named = {node.attr for node in ast.walk(identity_function) if isinstance(node, ast.Attribute)}

    assert read, "the scan found nothing: the check has stopped looking at the right thing"
    assert read <= named, f"the verifier reads {sorted(read - named)} but its identity omits them"
