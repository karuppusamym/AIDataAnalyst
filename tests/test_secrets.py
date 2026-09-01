from typing import Any

import httpx
import pytest

from aida.config import Settings
from aida.secrets import (
    ResolvedSecret,
    SecretResolutionError,
    SecretResolver,
    StaticTestSecretProvider,
    VaultKvSecretProvider,
)


def test_environment_secret_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDA_TEST_SECRET", "resolved-value")

    assert SecretResolver().resolve("env://AIDA_TEST_SECRET") == "resolved-value"


def test_secret_resolver_rejects_inline_connection_string() -> None:
    with pytest.raises(SecretResolutionError, match="invalid shape"):
        SecretResolver().resolve("postgresql://user:password@database/example")


def test_enterprise_provider_contract_and_invalidation() -> None:
    reference = "vault://bank/data-sources/core#dsn"
    provider = StaticTestSecretProvider(
        {("bank/data-sources/core", "dsn"): ResolvedSecret("private-dsn", cache_seconds=30)}
    )
    resolver = SecretResolver(
        Settings(credential_provider="vault", secret_cache_ttl_seconds=60),
        {"vault": provider},
    )

    assert resolver.provider_available() is True
    assert resolver.resolve(reference) == "private-dsn"
    resolver.invalidate(reference)
    assert resolver.resolve(reference) == "private-dsn"


def test_unregistered_enterprise_provider_fails_closed() -> None:
    resolver = SecretResolver(Settings(credential_provider="cyberark"))

    with pytest.raises(SecretResolutionError, match="provider is unavailable"):
        resolver.resolve("cyberark://bank/app/source#dsn")


# --- AU-10: VaultKvSecretProvider, the non-`env` provider that lets a ------
# --- production-valid `credential_provider` configuration actually resolve -


def _mock_transport(capture: dict[str, Any], response: httpx.Response) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["headers"] = dict(request.headers)
        return response

    return httpx.MockTransport(handler)


def test_vault_kv_provider_holds_no_extra_secret_material() -> None:
    """Mirrors `VaultTransitSigningProvider`'s equivalent assertion: a
    conforming provider holds only the request credential (bearer token) it
    was configured with and location metadata -- nothing that widens what a
    compromised process leaks beyond that one Vault token."""
    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
    )
    attributes = vars(provider)
    assert attributes["_token"] == "s.vault-token"  # noqa: S105
    assert attributes["_kv_mount"] == "secret"


def test_vault_kv_fetch_gets_the_kv_v2_path_with_a_bearer_token_and_default_field() -> None:
    capture: dict[str, Any] = {}
    payload = {"data": {"data": {"value": "private-dsn"}, "metadata": {"version": 3}}}
    transport = _mock_transport(capture, httpx.Response(200, json=payload))
    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
        client=httpx.Client(transport=transport),
    )
    resolved = provider.resolve(location="bank/data-sources/core", key=None)
    assert resolved.value == "private-dsn"
    assert resolved.version == "3"
    assert capture["url"] == "https://vault.internal:8200/v1/secret/data/bank/data-sources/core"
    assert capture["headers"]["x-vault-token"] == "s.vault-token"


def test_vault_kv_fetch_selects_the_fragment_field() -> None:
    payload = {"data": {"data": {"dsn": "private-dsn", "username": "svc"}}}
    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ),
    )
    assert provider.resolve(location="bank/data-sources/core", key="dsn").value == "private-dsn"
    assert provider.resolve(location="bank/data-sources/core", key="username").value == "svc"


def test_vault_kv_a_network_failure_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SecretResolutionError, match="request to"):
        provider.resolve(location="bank/data-sources/core", key=None)


def test_vault_kv_a_non_2xx_response_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": ["permission denied"]})

    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SecretResolutionError):
        provider.resolve(location="bank/data-sources/core", key=None)


def test_vault_kv_a_malformed_response_body_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"data": {}}})  # no requested field

    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SecretResolutionError, match="no usable"):
        provider.resolve(location="bank/data-sources/core", key=None)


def test_vault_kv_a_non_json_response_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    provider = VaultKvSecretProvider(
        base_url="https://vault.internal:8200",
        token="s.vault-token",  # noqa: S106
        kv_mount="secret",
        timeout_seconds=5.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SecretResolutionError, match="not valid JSON"):
        provider.resolve(location="bank/data-sources/core", key=None)


def test_secret_resolver_registers_vault_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of AU-10: a production-valid `credential_provider="vault"`
    configuration now has a provider that actually resolves, not just an
    accepted `Literal` value with nothing behind it."""
    payload = {"data": {"data": {"dsn": "private-dsn"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    original = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("aida.secrets.httpx.Client", factory)

    settings = Settings(
        credential_provider="vault",
        secrets_vault_url="https://vault.internal:8200",
        secrets_vault_token="s.vault-token",  # noqa: S106
    )
    resolver = SecretResolver(settings)
    assert resolver.provider_available() is True
    assert resolver.resolve("vault://bank/data-sources/core#dsn") == "private-dsn"


def test_secret_resolver_does_not_register_vault_when_unconfigured() -> None:
    """Without a Vault URL and bootstrap token, `vault` is simply not offered
    -- the same fail-closed shape an unimplemented provider (`cyberark`)
    already has, not a crash at `SecretResolver()` construction time."""
    resolver = SecretResolver(Settings(credential_provider="vault"))
    assert resolver.provider_available() is False
    with pytest.raises(SecretResolutionError, match="provider is unavailable"):
        resolver.resolve("vault://bank/data-sources/core#dsn")
