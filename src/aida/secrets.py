import os
import re
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

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
        if providers:
            self.providers.update(providers)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = RLock()

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
