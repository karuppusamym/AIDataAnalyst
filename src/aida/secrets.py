"""Provider-neutral resolution of opaque secret references (tracker AU-10 closes
the gap this module was already built around).

`Datasource.credential_reference` and friends store an opaque provider URI --
never credential material -- and `SecretResolver` resolves it to a value at the
moment it is needed, caching per `Settings.secret_cache_ttl_seconds` and never
persisting or logging the resolved value. That shape was already right; what
was missing was a second provider. Before AU-10, `SecretResolver` registered
exactly one provider, `EnvironmentSecretProvider` (scheme `env`), and
`Settings.reject_insecure_production_configuration` forbids
`credential_provider == "env"` in production (the same "no application-managed
secret in production" posture `hmac_signing_provider`/`tokenization_provider`
enforce for their own local fallbacks) -- so in any production-valid
configuration `resolve()` had no provider to hand off to and nothing in the
platform could authenticate to any data source at all.

`VaultKvSecretProvider` -- calls HashiCorp Vault's KV v2 secrets engine
(https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2), registered
under scheme `vault`. Chosen for the same reason `aida.signing`'s
`VaultTransitSigningProvider` and `aida.tokenization`'s
`VaultTransformTokenizationProvider` chose Vault over a cloud KMS: its HTTP
API needs only a bearer token, not a request-signing scheme that would pull in
a cloud SDK this codebase does not otherwise depend on, and `httpx` is already
a dependency. Unlike those two, `SecretProvider.resolve` is synchronous (every
existing call site -- `workflows/activities.py`, `query_gateway.py`,
`model_gateway.py`, `policy_native_sync_api.py` -- calls `resolve()`
synchronously, not from an `async def`), so `VaultKvSecretProvider` uses
`httpx.Client`, not `httpx.AsyncClient`. **This adapter is untested against a
real Vault instance** -- there is no live Vault (or any cloud KMS) account
reachable from this sandbox, the identical standing limitation noted on
`VaultTransitSigningProvider`/`VaultTransformTokenizationProvider` and the
connector work (CN-1c/CN-2a). It is exercised in `tests/test_secrets.py` only
against a mocked HTTP transport that asserts the request/response shape this
module sends and expects; the wire contract itself is unverified against a
live KV v2 engine.

`SecretResolver` registers `vault` unconditionally alongside `env` whenever
`Settings.secrets_vault_url` and `Settings.secrets_vault_token` are both
configured (see the module docstring in `atlas.platform.config` for why the
bootstrap token is not itself a `SecretResolver` reference), the same
"always register, let `credential_provider` and the reference scheme decide
which one is actually used" shape `env` already had. When it is not
configured, `credential_provider == "vault"` fails closed exactly the way an
unimplemented provider (`cyberark`, `aws-sm`, ...) already does: `resolve()`
raises `SecretResolutionError` rather than silently falling back to `env`.
"""

import os
import re
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from aida.config import Settings, get_settings


class SecretResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    value: str
    version: str | None = None
    cache_seconds: int | None = None


class SecretProvider(Protocol):
    def resolve(self, *, location: str, key: str | None) -> ResolvedSecret: ...


class EnvironmentSecretProvider:
    def resolve(self, *, location: str, key: str | None) -> ResolvedSecret:
        if key is not None or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,254}", location):
            raise SecretResolutionError("invalid environment secret reference")
        value = os.getenv(location)
        if not value:
            raise SecretResolutionError("referenced environment secret is not available")
        return ResolvedSecret(value=value, cache_seconds=0)


class VaultKvSecretProvider:
    """KMS-backed secret store: HashiCorp Vault's KV v2 secrets engine over HTTP.

    Holds no secret material of its own -- only a `base_url`, `kv_mount`, and a
    bearer `token` that authenticates the *request* to Vault, the same
    request-credential/secret-material distinction
    `aida.signing.VaultTransitSigningProvider` documents. `location` (from the
    reference's netloc+path, e.g. `bank/data-sources/core` out of
    `vault://bank/data-sources/core#dsn`) is the KV v2 secret path below the
    mount; `key` (the reference's `#fragment`) selects one field from that
    secret's data map, defaulting to `"value"` when the reference carries no
    fragment -- the common case of a Vault secret holding one opaque value
    (a DSN, an API key) under the conventional `"value"` field.

    See https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2 for the
    wire format this implements:
      GET {base_url}/v1/{kv_mount}/data/{location}
        -> {"data": {"data": {<key>: "<value>", ...}, "metadata": {"version": N}}}

    The response's `metadata.version` (KV v2 versions every write) is carried
    through as `ResolvedSecret.version` -- informational provenance for
    whichever value was actually fetched, the same field `StaticTestSecretProvider`
    fixtures already populate.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        kv_mount: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._kv_mount = kv_mount.strip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    def resolve(self, *, location: str, key: str | None) -> ResolvedSecret:
        field = key or "value"
        url = f"{self._base_url}/v1/{self._kv_mount}/data/{location}"
        headers = {"X-Vault-Token": self._token}
        try:
            if self._client is not None:
                response = self._client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Fail closed: a Vault that cannot be reached, times out, or rejects the
            # request must never fall back to a cached-forever or locally-fabricated
            # value -- see the module docstring and `SecretResolutionError`.
            raise SecretResolutionError(f"vault secret request to {url} failed: {exc}") from exc
        try:
            decoded = response.json()
        except ValueError as exc:
            raise SecretResolutionError("vault secret response was not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SecretResolutionError("vault secret response was not a JSON object")
        outer = decoded.get("data")
        fields = outer.get("data") if isinstance(outer, dict) else None
        if not isinstance(fields, dict):
            raise SecretResolutionError("vault secret response was malformed")
        value = fields.get(field)
        if not isinstance(value, str) or not value:
            raise SecretResolutionError(f"vault secret has no usable '{field}' field")
        version = None
        metadata = outer.get("metadata") if isinstance(outer, dict) else None
        if isinstance(metadata, dict) and isinstance(metadata.get("version"), int):
            version = str(metadata["version"])
        return ResolvedSecret(value=value, version=version)


class StaticTestSecretProvider:
    """Explicitly injected provider used by tests; never registered by the application."""

    def __init__(self, values: dict[tuple[str, str | None], ResolvedSecret]) -> None:
        self.values = values

    def resolve(self, *, location: str, key: str | None) -> ResolvedSecret:
        try:
            return self.values[(location, key)]
        except KeyError as exc:
            raise SecretResolutionError("referenced enterprise secret is unavailable") from exc


class SecretResolver:
    """Provider-neutral reference resolver; secret material is never persisted or logged."""

    def __init__(
        self,
        settings: Settings | None = None,
        providers: dict[str, SecretProvider] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.providers: dict[str, SecretProvider] = {"env": EnvironmentSecretProvider()}
        vault_provider = self._build_vault_provider()
        if vault_provider is not None:
            self.providers["vault"] = vault_provider
        if providers:
            self.providers.update(providers)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = RLock()

    def _build_vault_provider(self) -> SecretProvider | None:
        """`VaultKvSecretProvider`, or `None` if it isn't configured.

        Not configured is not an error here -- registration only makes `vault`
        *available*; `resolve()`'s existing scheme check still refuses any
        reference whose scheme is not `self.settings.credential_provider`, and
        `provider_available()`/`resolve()` already fail closed (the same way
        they do for `cyberark`/`aws-sm`/...) when `credential_provider ==
        "vault"` but no `VaultKvSecretProvider` could be built.
        """
        url = self.settings.secrets_vault_url
        token = self.settings.secrets_vault_token
        if not url or token is None:
            return None
        secret_value = token.get_secret_value()
        if not secret_value:
            return None
        return VaultKvSecretProvider(
            base_url=url,
            token=secret_value,
            kv_mount=self.settings.secrets_vault_kv_mount,
            timeout_seconds=self.settings.secrets_vault_timeout_seconds,
        )

    def provider_available(self) -> bool:
        return self.settings.credential_provider in self.providers

    def _parse_reference(self, reference: str) -> tuple[str, str, str | None]:
        parsed = urlsplit(reference)
        scheme = parsed.scheme.lower()
        if not scheme or parsed.username or parsed.password or parsed.query:
            raise SecretResolutionError("secret reference has an invalid shape")
        if scheme != self.settings.credential_provider:
            raise SecretResolutionError(
                "secret reference provider is not approved by configuration"
            )
        provider = self.providers.get(scheme)
        if provider is None:
            raise SecretResolutionError("configured enterprise secret provider is unavailable")
        location = f"{parsed.netloc}{parsed.path}".strip("/")
        key = parsed.fragment or None
        if (
            not location
            or ".." in location.split("/")
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,500}", location)
            or (key is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", key))
        ):
            raise SecretResolutionError("secret reference location is invalid")
        return scheme, location, key

    def validate_reference(self, reference: str) -> None:
        """Validate an opaque reference without accessing the secret provider."""
        self._parse_reference(reference)

    def resolve(self, reference: str) -> str:
        scheme, location, key = self._parse_reference(reference)
        provider = self.providers.get(scheme)
        if provider is None:
            raise SecretResolutionError("configured enterprise secret provider is unavailable")
        now = monotonic()
        with self._lock:
            cached = self._cache.get(reference)
            if cached and cached[0] > now:
                return cached[1]
        resolved = provider.resolve(location=location, key=key)
        if not resolved.value:
            raise SecretResolutionError("secret provider returned an empty value")
        provider_ttl = (
            resolved.cache_seconds
            if resolved.cache_seconds is not None
            else self.settings.secret_cache_ttl_seconds
        )
        ttl = min(provider_ttl, self.settings.secret_cache_ttl_seconds)
        if ttl > 0:
            with self._lock:
                self._cache[reference] = (now + ttl, resolved.value)
        return resolved.value

    def invalidate(self, reference: str | None = None) -> None:
        with self._lock:
            if reference is None:
                self._cache.clear()
            else:
                self._cache.pop(reference, None)
