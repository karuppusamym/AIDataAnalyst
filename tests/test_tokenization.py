"""QG-6: dynamic masking / tokenization integration.

The property worth protecting is the same shape as QG-5's for signing, one
layer over: a `TokenizationProvider` never gives the caller anything it could
forge or replay offline, a KMS-backed call that fails never degrades into an
unmasked value or a locally-forged token, and the config-selected provider is
resolved fresh per call with no second caching story.

Four properties are exercised here:

- `LocalFpeTokenizationProvider` is deterministic (same value -> same token,
  every time), reversible (`detokenize(tokenize(x)) == x`), and
  format-preserving (digit positions and non-digit separators are unchanged).
- It is key-bound: two providers built from different keys tokenize the same
  value differently, so a token cannot be reversed without the key that
  produced it.
- `VaultTransformTokenizationProvider` never holds key material -- only a
  role *name*, a token, and an endpoint -- and speaks the documented Vault
  Transform wire format, verified against a mocked HTTP transport (no live
  Vault reachable from this sandbox, the same standing limitation as
  `VaultTransitSigningProvider`).
- Config-driven selection never falls back silently, and production forbids
  the local provider -- mirroring `test_hmac_signing.py`'s coverage of
  `hmac_signing_provider` exactly.
"""

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider
from aida.tokenization import (
    LocalFpeTokenizationProvider,
    TokenizationError,
    TokenizationUnavailable,
    VaultTransformTokenizationProvider,
    resolve_tokenization_provider,
)
from atlas.platform.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


# --- the local provider: deterministic, reversible, format-preserving -------


async def test_local_provider_round_trips_a_credit_card_number() -> None:
    provider = LocalFpeTokenizationProvider("k" * 32)
    original = "4111-1111-1111-1111"
    token = await provider.tokenize(original)
    assert token != original
    assert await provider.detokenize(token) == original


async def test_local_provider_round_trips_an_ssn() -> None:
    provider = LocalFpeTokenizationProvider("k" * 32)
    original = "123-45-6789"
    token = await provider.tokenize(original)
    assert token != original
    assert await provider.detokenize(token) == original


async def test_local_provider_is_deterministic() -> None:
    provider = LocalFpeTokenizationProvider("k" * 32)
    original = "4111111111111111"
    assert await provider.tokenize(original) == await provider.tokenize(original)


async def test_local_provider_preserves_format() -> None:
    """Non-digit characters stay at their original positions; the digit run
    stays the same length -- a tokenized card number still looks like one."""
    provider = LocalFpeTokenizationProvider("k" * 32)
    original = "4111-1111-1111-1111"
    token = await provider.tokenize(original)
    assert len(token) == len(original)
    assert [i for i, c in enumerate(token) if c == "-"] == [
        i for i, c in enumerate(original) if c == "-"
    ]
    digits = "".join(c for c in token if c.isdigit())
    assert len(digits) == sum(c.isdigit() for c in original)
    assert digits.isdigit()


async def test_local_provider_is_key_bound() -> None:
    """A token cannot be reversed under a different key than the one that produced it."""
    provider_a = LocalFpeTokenizationProvider("key-a" * 8)
    provider_b = LocalFpeTokenizationProvider("key-b" * 8)
    original = "4111111111111111"
    token_a = await provider_a.tokenize(original)
    token_b = await provider_b.tokenize(original)
    assert token_a != token_b
    # Detokenizing under the wrong key does not recover the original value.
    assert await provider_b.detokenize(token_a) != original


async def test_local_provider_different_values_tokenize_differently() -> None:
    provider = LocalFpeTokenizationProvider("k" * 32)
    assert await provider.tokenize("4111111111111111") != await provider.tokenize(
        "4222222222222222"
    )


async def test_local_provider_leaves_non_digit_values_unchanged() -> None:
    """Nothing in the value is a digit run to tokenize; the local scheme has no
    obfuscation to apply, and returns the value as-is rather than raising."""
    provider = LocalFpeTokenizationProvider("k" * 32)
    assert await provider.tokenize("no-digits-here") == "no-digits-here"


async def test_local_provider_handles_short_digit_runs_without_raising() -> None:
    """A single digit has no second half to derive a round function from
    (see `_feistel_transform`'s docstring) -- handled, not a crash."""
    provider = LocalFpeTokenizationProvider("k" * 32)
    token = await provider.tokenize("5")
    assert await provider.detokenize(token) == "5"


async def test_local_provider_round_trips_many_lengths_and_values() -> None:
    """Broader coverage than a single fixed example: every realistic PII shape
    this platform tokenizes (SSN, card, phone, account number lengths) round-trips."""
    provider = LocalFpeTokenizationProvider("bank-tokenization-key-32-bytes!!")
    samples = [
        "078051120",  # SSN, 9 digits
        "4532015112830366",  # card, 16 digits
        "2223000048410010",  # card, 16 digits (different BIN)
        "5555555555554444",
        "8005551234",  # phone, 10 digits
        "12345678901234567890",  # 20-digit account number
    ]
    for value in samples:
        token = await provider.tokenize(value)
        assert await provider.detokenize(token) == value


# --- config-driven selection -------------------------------------------------


def test_default_settings_select_the_local_provider() -> None:
    settings = _settings(tokenization_key="a" * 32)
    provider = resolve_tokenization_provider(settings)
    assert isinstance(provider, LocalFpeTokenizationProvider)


def test_vault_transform_selection_requires_an_endpoint() -> None:
    settings = _settings(tokenization_provider="vault_transform")
    with pytest.raises(TokenizationUnavailable, match="TOKENIZATION_VAULT_URL_NOT_CONFIGURED"):
        resolve_tokenization_provider(settings)


def test_vault_transform_selection_requires_a_token_reference() -> None:
    settings = _settings(
        tokenization_provider="vault_transform",
        tokenization_vault_url="https://vault.internal:8200",
    )
    with pytest.raises(TokenizationUnavailable, match="TOKENIZATION_VAULT_TOKEN_NOT_CONFIGURED"):
        resolve_tokenization_provider(settings)


def test_vault_transform_selection_resolves_the_token_through_secret_resolver() -> None:
    settings = _settings(
        tokenization_provider="vault_transform",
        tokenization_vault_url="https://vault.internal:8200",
        tokenization_vault_token_reference="env://VAULT_TRANSFORM_TOKEN",  # noqa: S106
    )
    resolver = SecretResolver(
        settings,
        providers={
            "env": StaticTestSecretProvider(
                {("VAULT_TRANSFORM_TOKEN", None): ResolvedSecret(value="s.vault-token")}
            )
        },
    )
    provider = resolve_tokenization_provider(settings, resolver)
    assert isinstance(provider, VaultTransformTokenizationProvider)


def test_an_unsupported_tokenization_provider_refuses_rather_than_falling_back() -> None:
    class _BadSettings:
        tokenization_provider = "unknown-provider"

    with pytest.raises(TokenizationUnavailable, match="TOKENIZATION_PROVIDER_UNSUPPORTED"):
        resolve_tokenization_provider(_BadSettings())  # type: ignore[arg-type]


# --- the raw key never enters the process for the KMS-backed provider -------


def test_vault_transform_provider_holds_no_key_material() -> None:
    provider = VaultTransformTokenizationProvider(
        base_url="https://vault.internal:8200",
        role_name="pii-tokens",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
    )
    attributes = vars(provider)
    assert not hasattr(provider, "key")
    assert not any("secret" in name.lower() for name in attributes)
    assert attributes["_role_name"] == "pii-tokens"
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


async def test_vault_transform_tokenize_posts_the_value_and_a_bearer_token() -> None:
    capture: dict[str, Any] = {}
    transport = _mock_transport(capture, {"data": {"encoded_value": "9999-9999-9999-1111"}})
    provider = VaultTransformTokenizationProvider(
        base_url="https://vault.internal:8200",
        role_name="pii-tokens",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=transport),
    )
    token = await provider.tokenize("4111-1111-1111-1111")
    assert token == "9999-9999-9999-1111"  # noqa: S105
    assert capture["url"] == "https://vault.internal:8200/v1/transform/encode/pii-tokens"
    assert capture["headers"]["x-vault-token"] == "s.vault-token"
    assert capture["body"]["value"] == "4111-1111-1111-1111"


async def test_vault_transform_detokenize_posts_the_token() -> None:
    capture: dict[str, Any] = {}
    transport = _mock_transport(capture, {"data": {"decoded_value": "4111-1111-1111-1111"}})
    provider = VaultTransformTokenizationProvider(
        base_url="https://vault.internal:8200",
        role_name="pii-tokens",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=transport),
    )
    value = await provider.detokenize("9999-9999-9999-1111")
    assert value == "4111-1111-1111-1111"
    assert capture["url"] == "https://vault.internal:8200/v1/transform/decode/pii-tokens"
    assert capture["body"]["value"] == "9999-9999-9999-1111"


# --- fail closed: a broken KMS never degrades to an unmasked or forged value -


async def test_a_network_failure_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = VaultTransformTokenizationProvider(
        base_url="https://vault.internal:8200",
        role_name="pii-tokens",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TokenizationError):
        await provider.tokenize("4111111111111111")


async def test_a_non_2xx_response_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": ["permission denied"]})

    provider = VaultTransformTokenizationProvider(
        base_url="https://vault.internal:8200",
        role_name="pii-tokens",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TokenizationError):
        await provider.tokenize("4111111111111111")


async def test_a_malformed_response_body_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})  # no "encoded_value" key

    provider = VaultTransformTokenizationProvider(
        base_url="https://vault.internal:8200",
        role_name="pii-tokens",
        token="s.vault-token",  # noqa: S106
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TokenizationError):
        await provider.tokenize("4111111111111111")


# --- production posture ------------------------------------------------------


def test_production_forbids_the_local_tokenization_provider() -> None:
    """QG-6's exit condition, at the config layer: an application-managed local
    tokenization key can never be the tokenizer a production deployment relies on."""
    with pytest.raises(ValidationError, match="application-managed local tokenization provider"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys": []}',
            credential_provider="vault",
            allow_development_sql_override=False,
            audit_hmac_key="a" * 32,
            hmac_signing_provider="vault_transit",
            hmac_signing_vault_url="https://vault.internal:8200",
            hmac_signing_vault_token_reference="vault://hmac-token",  # noqa: S106
            tokenization_key="a" * 32,
            tokenization_provider="local",
            _env_file=None,
        )


def test_production_requires_a_long_tokenization_key_even_before_provider_selection() -> None:
    """The key-length floor applies independently of `tokenization_provider` --
    mirrors `audit_hmac_key`'s length check, which fires whether or not the
    signing provider that would use it is even the local one."""
    with pytest.raises(ValidationError, match="production tokenization key"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys": []}',
            credential_provider="vault",
            allow_development_sql_override=False,
            audit_hmac_key="a" * 32,
            hmac_signing_provider="vault_transit",
            hmac_signing_vault_url="https://vault.internal:8200",
            hmac_signing_vault_token_reference="vault://hmac-token",  # noqa: S106
            tokenization_key="short",
            tokenization_provider="vault_transform",
            tokenization_vault_url="https://vault.internal:8200",
            tokenization_vault_token_reference="vault://tokenization-token",  # noqa: S106
            _env_file=None,
        )
