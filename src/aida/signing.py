"""KMS-managed signing for audit evidence (tracker QG-5).

`query_gateway.py` records a keyed HMAC of every SQL statement it validates or
executes -- the "unforgeable without the server's key" property that makes an
audit record evidence rather than a claim (see `audit_sql_hash`). Before QG-5
that key was `Settings.audit_hmac_key`: a plaintext secret loaded into process
config and handed straight to `hmac.new()`, which is exactly the shape of an
"application-managed key" the exit condition rules out -- anyone who can read
the config (or a heap dump, or a backup of it) can mint a signature for SQL
that never ran.

A real KMS-managed key does not have this failure mode because the raw key
material never leaves the KMS. The application never holds it; it sends the
data to be signed (or a signature to check) and gets back a result. This
module's `SigningProvider` protocol is shaped around that constraint on
purpose: `sign`/`verify` are the only operations, and a conforming
implementation has no `key` attribute for anything to leak. `secrets.py`'s
`SecretProvider` is the closest precedent in this codebase, but it resolves a
*value* the caller then owns; a signing provider never gives the caller
anything it could use to forge a signature offline.

Two implementations ship:

- `LocalHmacSigningProvider` -- the pre-QG-5 behaviour, kept byte-for-byte
  (`audit_sql_hash` is unchanged) so every existing caller and test that signs
  locally sees no behaviour change. It genuinely holds the key in process
  config, which is precisely why it is refused outside development (mirrors
  MG-1's "no `env://` in a non-local environment": see
  `Settings.reject_insecure_production_configuration`) -- it must never be
  silently indistinguishable from a certified KMS signer.
- `VaultTransitSigningProvider` -- calls HashiCorp Vault's Transit secrets
  engine HMAC/verify HTTP endpoints (https://developer.hashicorp.com/vault/api-docs/secret/transit).
  Chosen over AWS KMS/GCP Cloud KMS/Azure Key Vault because its HTTP API needs
  only a bearer token, not a request-signing scheme (AWS SigV4) that would
  otherwise pull in a cloud SDK -- `pyproject.toml` has no cloud SDK dependency
  today, and QG-5 does not add one; `httpx`, already a dependency, is enough.
  **This adapter is untested against a real Vault instance** -- there is no
  live Vault (or any cloud KMS) account reachable from this sandbox, the same
  standing limitation noted on the connector work (CN-1c/CN-2a). It is
  exercised in `tests/test_hmac_signing.py` only against a mocked HTTP
  transport that asserts the request/response shape this module sends and
  expects; the wire contract itself is unverified against a live Transit
  engine.

Selection between them is config-driven (`Settings.hmac_signing_provider`),
resolved fresh per call by `resolve_signing_provider` -- the same
resolve-on-every-call shape `query_gateway.py` already uses for
`SecretResolver`, so nothing here introduces a second caching story.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Protocol

import httpx

from aida.secrets import SecretResolutionError, SecretResolver
from atlas.platform.config import Settings


class SigningError(RuntimeError):
    """A signing or verification call failed. Callers must fail closed.

    Never caught by `query_gateway.py` to fall back to an unsigned or
    locally-forged hash -- an audit record with no signature, or one signed
    under a key the deployment does not actually control, is a worse outcome
    than a rejected request.
    """


class SigningUnavailable(SigningError):
    """No usable signing provider is configured, or it could not be built.

    Raised instead of substituting a fallback signer, for the same reason
    `EmbeddingUnavailable` is never swapped for the hash double in
    `embedding_provider.py`: silently signing under a different key than the
    one an operator configured is a worse failure than refusing.
    """


def audit_sql_hash(key: str, sql: str) -> str:
    """HMAC-SHA256 the raw SQL text under a locally-held key.

    Keyed (not a bare hash) so the digest is both tamper-evident and
    unforgeable without the key: an attacker who can read a stored execution
    record still cannot mint a matching hash for different SQL, and the
    digest changes if the recorded SQL is altered after the fact.

    This is the algorithm `LocalHmacSigningProvider` uses, kept as a
    standalone function -- unchanged from its pre-QG-5 shape -- because it is
    also a plain deterministic utility existing tests exercise directly with
    arbitrary keys, independent of any provider or settings object.
    """
    return hmac.new(key.encode("utf-8"), sql.encode("utf-8"), hashlib.sha256).hexdigest()


class SigningProvider(Protocol):
    """Sign or verify data without the caller ever holding key material.

    Async because a conforming (KMS-backed) implementation is a network call;
    `LocalHmacSigningProvider` satisfies the same protocol trivially. Keeping
    one protocol rather than a sync/async pair means a call site written
    against this interface is correct under either provider -- there is no
    seam where a KMS provider could be silently swapped for one that assumes
    signing is free, which is the opposite of the split `embedding_provider.py`
    makes between its sync and async embedding protocols (there, the sync one
    is a test double that must never reach a production call site; here, the
    local provider is a real -- if uncertified -- fallback, not a double).
    """

    async def sign(self, data: str) -> str: ...

    async def verify(self, data: str, signature: str) -> bool: ...


class LocalHmacSigningProvider:
    """Development-only fallback: the key is held in process config.

    Not a certified KMS signer -- it is exactly the "application-managed key"
    QG-5's exit condition rules out, kept only so a local/dev deployment has a
    working signer with no external dependency. `Settings.hmac_signing_provider`
    can only be `"local"` when `environment` is not `"production"`
    (`reject_insecure_production_configuration` enforces it), so this class
    can never be the signer a production audit trail relies on.
    """

    def __init__(self, key: str) -> None:
        self._key = key

    async def sign(self, data: str) -> str:
        return audit_sql_hash(self._key, data)

    async def verify(self, data: str, signature: str) -> bool:
        expected = audit_sql_hash(self._key, data)
        return hmac.compare_digest(expected, signature)


class VaultTransitSigningProvider:
    """KMS-backed signer: HashiCorp Vault's Transit secrets engine over HTTP.

    Holds no key material -- only a `key_name` (which key inside Vault to use,
    not the key itself), a `base_url`, and a bearer `token` used to
    authenticate the *request*. There is no `key` attribute on this class:
    every `sign`/`verify` call is a round trip to Vault, and Vault -- not this
    process -- performs the HMAC.

    See https://developer.hashicorp.com/vault/api-docs/secret/transit for the
    wire format this implements:
      POST {base_url}/v1/transit/hmac/{key_name}    {"input": base64(data)}
        -> {"data": {"hmac": "vault:v1:<base64 signature>"}}
      POST {base_url}/v1/transit/verify/{key_name}  {"input": base64(data), "hmac": sig}
        -> {"data": {"valid": true|false}}
    """

    def __init__(
        self,
        *,
        base_url: str,
        key_name: str,
        token: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_name = key_name
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def sign(self, data: str) -> str:
        body = await self._call(f"/v1/transit/hmac/{self._key_name}", {"input": _b64(data)})
        signature = _response_field(body, "hmac")
        if not isinstance(signature, str) or not signature:
            raise SigningError("KMS signing response was malformed")
        return signature

    async def verify(self, data: str, signature: str) -> bool:
        body = await self._call(
            f"/v1/transit/verify/{self._key_name}",
            {"input": _b64(data), "hmac": signature},
        )
        valid = _response_field(body, "valid")
        if not isinstance(valid, bool):
            raise SigningError("KMS verification response was malformed")
        return valid

    async def _call(self, path: str, payload: dict[str, str]) -> dict[str, object]:
        headers = {"X-Vault-Token": self._token}
        url = f"{self._base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Fail closed: a KMS that cannot be reached, times out, or rejects the
            # request must never fall back to producing an unsigned or locally
            # forged hash -- see the module docstring and `SigningError`.
            raise SigningError(f"KMS request to {path} failed: {exc}") from exc
        try:
            decoded = response.json()
        except ValueError as exc:
            raise SigningError("KMS response was not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SigningError("KMS response was not a JSON object")
        return decoded


def _b64(data: str) -> str:
    return base64.b64encode(data.encode("utf-8")).decode("ascii")


def _response_field(body: dict[str, object], field: str) -> object:
    """`body["data"][field]`, tolerating a malformed shape rather than raising
    a `KeyError`/`TypeError` the caller would have to distinguish from a
    genuinely bad value -- both collapse to the same `SigningError`."""
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return data.get(field)


def resolve_signing_provider(
    settings: Settings, resolver: SecretResolver | None = None
) -> SigningProvider:
    """The configured signing provider, or `SigningUnavailable` with a reason.

    Never returns a fallback: a caller that cannot obtain the configured
    provider must refuse the request that needed a signature, not sign it
    under a different (e.g. local) key than the deployment configured.
    """
    provider = settings.hmac_signing_provider
    if provider == "local":
        return LocalHmacSigningProvider(settings.audit_hmac_key)
    if provider == "vault_transit":
        if not settings.hmac_signing_vault_url:
            raise SigningUnavailable("HMAC_SIGNING_VAULT_URL_NOT_CONFIGURED")
        if not settings.hmac_signing_vault_token_reference:
            raise SigningUnavailable("HMAC_SIGNING_VAULT_TOKEN_NOT_CONFIGURED")
        resolver = resolver or SecretResolver(settings)
        try:
            token = resolver.resolve(settings.hmac_signing_vault_token_reference)
        except SecretResolutionError as exc:
            raise SigningUnavailable("HMAC_SIGNING_VAULT_TOKEN_UNRESOLVABLE") from exc
        return VaultTransitSigningProvider(
            base_url=settings.hmac_signing_vault_url,
            key_name=settings.hmac_signing_vault_key_name,
            token=token,
            timeout_seconds=settings.hmac_signing_timeout_seconds,
        )
    raise SigningUnavailable(f"HMAC_SIGNING_PROVIDER_UNSUPPORTED:{provider}")
