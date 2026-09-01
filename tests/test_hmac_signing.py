"""QG-5: KMS-managed HMAC keys for query-gateway audit evidence.

The property worth protecting is not "a hash gets produced". It is that the
raw key material a signature could be forged with never lives in this
process when a KMS-backed provider is configured -- the application sends
data to be signed and gets back a signature, exactly the way `secrets.py`'s
`SecretProvider` resolves a value rather than owning it, one layer further
in: a `SigningProvider` never even hands the caller a value it could reuse
offline.

Three properties are exercised:

- The local provider (`LocalHmacSigningProvider`) matches the pre-QG-5
  `audit_sql_hash` behaviour exactly, so no existing caller sees a change.
- The KMS-backed provider (`VaultTransitSigningProvider`) never holds key
  material -- only a name of a key, a token, and an endpoint -- and speaks
  the documented Vault Transit wire format, verified here against a mocked
  HTTP transport (there is no live Vault or cloud KMS account reachable from
  this sandbox, the same standing limitation as the connector work).
- A KMS call that fails -- unreachable, times out, non-2xx, malformed body --
  fails closed: it raises rather than silently returning an unsigned or
  locally-forged hash.
"""

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aida.query_gateway import QueryExecutionGateway, audit_sql_hash
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider
from aida.signing import (
    LocalHmacSigningProvider,
    SigningError,
    SigningUnavailable,
    VaultTransitSigningProvider,
    resolve_signing_provider,
)
from atlas.platform.config import Settings

SQL = "SELECT customer_id FROM payments WHERE amount > 100"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


# --- local provider: byte-for-byte the pre-QG-5 behaviour -------------------


async def test_local_provider_matches_audit_sql_hash_exactly() -> None:
    """No behaviour change for a caller that still signs locally."""
    provider = LocalHmacSigningProvider("k" * 32)
    assert await provider.sign(SQL) == audit_sql_hash("k" * 32, SQL)


async def test_local_provider_is_deterministic_key_bound_and_tamper_evident() -> None:
    provider_a = LocalHmacSigningProvider("key-a")
    provider_b = LocalHmacSigningProvider("key-b")
    assert await provider_a.sign(SQL) == await provider_a.sign(SQL)
    assert await provider_a.sign(SQL) != await provider_b.sign(SQL)
    assert await provider_a.sign(SQL) != await provider_a.sign(SQL + " -- tampered")


async def test_local_provider_verify_round_trips() -> None:
    provider = LocalHmacSigningProvider("k" * 32)
    signature = await provider.sign(SQL)
    assert await provider.verify(SQL, signature) is True
    assert await provider.verify(SQL + " -- tampered", signature) is False
    assert await provider.verify(SQL, "0" * 64) is False


async def test_gateway_local_signing_matches_todays_audit_sql_hash() -> None:
    """End to end through `QueryExecutionGateway._sign_sql`, the call sites'
    entry point -- not just the provider in isolation."""
    settings = _settings(audit_hmac_key="a" * 32)
    gateway = QueryExecutionGateway(settings)
    assert await gateway._sign_sql(SQL) == audit_sql_hash("a" * 32, SQL)


# --- config-driven selection -------------------------------------------------


def test_default_settings_select_the_local_provider() -> None:
    settings = _settings(audit_hmac_key="a" * 32)
    provider = resolve_signing_provider(settings)
    assert isinstance(provider, LocalHmacSigningProvider)


def test_vault_transit_selection_requires_an_endpoint() -> None:
    settings = _settings(hmac_signing_provider="vault_transit")
    with pytest.raises(SigningUnavailable, match="HMAC_SIGNING_VAULT_URL_NOT_CONFIGURED"):
        resolve_signing_provider(settings)


def test_vault_transit_selection_requires_a_token_reference() -> None:
    settings = _settings(
        hmac_signing_provider="vault_transit",
        hmac_signing_vault_url="https://vault.internal:8200",
    )
    with pytest.raises(SigningUnavailable, match="HMAC_SIGNING_VAULT_TOKEN_NOT_CONFIGURED"):
        resolve_signing_provider(settings)


def test_vault_transit_selection_resolves_the_token_through_secret_resolver() -> None:
    """The token is resolved through the same provider-neutral path as every other
    credential -- not read straight off `Settings` -- so it inherits the same
    rotation and the same production refusal of `env://` as any other secret."""
    settings = _settings(
        hmac_signing_provider="vault_transit",
        hmac_signing_vault_url="https://vault.internal:8200",
        hmac_signing_vault_token_reference="env://VAULT_HMAC_TOKEN",  # noqa: S106
    )
    resolver = SecretResolver(
        settings,
        providers={
            "env": StaticTestSecretProvider(
                {("VAULT_HMAC_TOKEN", None): ResolvedSecret(value="s.vault-token")}
            )
        },
    )
    provider = resolve_signing_provider(settings, resolver)
    assert isinstance(provider, VaultTransitSigningProvider)


def test_an_unsupported_signing_provider_refuses_rather_than_falling_back() -> None:
    """Mirrors `resolve_embedding_provider`'s refusal shape: an unrecognised or
    unbuildable provider is a hard refusal, never a silent substitution.

    `Settings.hmac_signing_provider` is a `Literal`, so an invalid value can
    never reach this point through real configuration -- this exercises
    `resolve_signing_provider`'s own defensive branch via a minimal stand-in
    carrying just the attributes it reads, the same way a duck-typed settings
    object would arrive from a caller that skipped pydantic validation.
    """

    class _BadSettings:
        hmac_signing_provider = "unknown-provider"

    with pytest.raises(SigningUnavailable, match="HMAC_SIGNING_PROVIDER_UNSUPPORTED"):
        resolve_signing_provider(_BadSettings())  # type: ignore[arg-type]


# --- the raw key never enters the process for the KMS-backed provider -------


def test_vault_transit_provider_holds_no_key_material() -> None:
    """The whole point of QG-5. A conforming KMS provider has no attribute that
    could leak the HMAC key -- only a key *name* (Vault-side reference), an
    endpoint, and a bearer token that authenticates the request, not the key."""
    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
    )
    attributes = vars(provider)
    assert not hasattr(provider, "key")
    assert not any("secret" in name.lower() for name in attributes)
    # What it does hold is a reference (key *name*) and a request credential
    # (token), neither of which is HMAC key material.
    assert attributes["_key_name"] == "audit-hmac"
    assert attributes["_token"] == "s.vault-token"  # noqa: S105


# --- the wire format, against a mocked HTTP transport ------------------------


def _mock_transport(
    capture: dict[str, Any], response_payload: dict[str, Any]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["headers"] = dict(request.headers)
        capture["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_payload)

    return httpx.MockTransport(handler)


async def test_vault_transit_sign_posts_base64_input_and_a_bearer_token() -> None:
    capture: dict[str, Any] = {}
    transport = _mock_transport(capture, {"data": {"hmac": "vault:v1:c2lnbmVk"}})
    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=transport),
    )
    signature = await provider.sign(SQL)
    assert signature == "vault:v1:c2lnbmVk"
    assert capture["url"] == "https://vault.internal:8200/v1/transit/hmac/audit-hmac"
    assert capture["headers"]["x-vault-token"] == "s.vault-token"
    assert base64.b64decode(capture["body"]["input"]).decode("utf-8") == SQL


async def test_vault_transit_verify_posts_the_signature_alongside_the_input() -> None:
    capture: dict[str, Any] = {}
    transport = _mock_transport(capture, {"data": {"valid": True}})
    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=transport),
    )
    assert await provider.verify(SQL, "vault:v1:c2lnbmVk") is True
    assert capture["url"] == "https://vault.internal:8200/v1/transit/verify/audit-hmac"
    assert capture["body"]["hmac"] == "vault:v1:c2lnbmVk"


async def test_vault_transit_verify_reports_an_invalid_signature() -> None:
    transport = _mock_transport({}, {"data": {"valid": False}})
    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=transport),
    )
    assert await provider.verify(SQL, "vault:v1:not-the-real-signature") is False


# --- fail closed: a broken KMS never degrades to an unsigned hash -----------


async def test_a_network_failure_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SigningError):
        await provider.sign(SQL)


async def test_a_non_2xx_response_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": ["permission denied"]})

    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SigningError):
        await provider.sign(SQL)


async def test_a_malformed_response_body_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})  # no "hmac" key

    provider = VaultTransitSigningProvider(
        base_url="https://vault.internal:8200",
        key_name="audit-hmac",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SigningError):
        await provider.sign(SQL)


async def test_gateway_signing_fails_closed_when_kms_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call-site level assertion: `QueryExecutionGateway._sign_sql` must not
    catch a KMS failure and substitute an unsigned or locally-forged hash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("aida.signing.httpx.AsyncClient", factory)

    settings = _settings(
        hmac_signing_provider="vault_transit",
        hmac_signing_vault_url="https://vault.internal:8200",
        hmac_signing_vault_token_reference="env://VAULT_HMAC_TOKEN",  # noqa: S106
        credential_provider="env",
    )
    monkeypatch.setenv("VAULT_HMAC_TOKEN", "s.vault-token")
    gateway = QueryExecutionGateway(settings)
    with pytest.raises(SigningError):
        await gateway._sign_sql(SQL)


# --- production posture ------------------------------------------------------


def test_production_forbids_the_local_signing_provider() -> None:
    """QG-5's exit condition, at the config layer: an application-managed local
    key can never be the signer a production deployment relies on."""
    with pytest.raises(ValidationError, match="application-managed local HMAC signer"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys": []}',
            credential_provider="vault",
            allow_development_sql_override=False,
            audit_hmac_key="a" * 32,
            hmac_signing_provider="local",
            _env_file=None,
        )
