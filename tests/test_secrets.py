import pytest

from aida.config import Settings
from aida.secrets import (
    ResolvedSecret,
    SecretResolutionError,
    SecretResolver,
    StaticTestSecretProvider,
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
