"""KMS-backed tokenization for query-gateway masking (tracker QG-6).

`query_gateway.py`'s masking pass (module 16 responsibility "column masking and
redaction by classification") had exactly one strategy before QG-6: a sensitive
output column's value is replaced with the literal string ``"***MASKED***"``.
That is irreversible redaction -- correct for most sensitive columns, but wrong
for the shape of PII a bank's own workflows routinely need to *reverse*: a case
worker resolving a fraud dispute needs the real card number back, a compliance
reviewer confirming a customer's identity needs the real SSN back. Redaction
throws that need away entirely; a workflow that needs it either bypasses the
gateway (the one thing INV-2 exists to prevent) or the platform never gets
adopted for that workload.

Tokenization is the answer already used industry-wide for this: a sensitive
value is replaced with a token that has the *same shape* (same length, same
character class -- a 16-digit token looks like a 16-digit card number) so it
survives downstream validation and display logic unchanged, and the token can
be reversed back to the original value only through an explicit, audited,
role-gated call (`aida.detokenization_api.detokenize_value`) -- never as a side
effect of running a query.

This module is `aida.signing` restructured for a second operation shape:

- `TokenizationProvider` is `SigningProvider`'s protocol shape (`sign`/`verify`)
  applied to a different pair of operations (`tokenize`/`detokenize`). Same
  reasons apply: async so a KMS-backed implementation is free to be a network
  call, one protocol (not a sync/async split) so a call site written against it
  is correct under either provider, resolved fresh per call
  (`resolve_tokenization_provider`) so there is no second caching story next to
  `SecretResolver`'s.
- `LocalFpeTokenizationProvider` -- the development-only fallback, forbidden in
  production by `Settings.reject_insecure_production_configuration` the same
  way `hmac_signing_provider == "local"` is. Unlike `LocalHmacSigningProvider`
  it is not a "pre-QG-6 behaviour kept unchanged" -- there was no tokenization
  before QG-6 -- so it exists purely so a local/dev deployment has a working,
  self-contained tokenizer with no external dependency, and is exercised end to
  end by the test suite the way production traffic never will be.
- `VaultTransformTokenizationProvider` -- calls HashiCorp Vault's *Transform*
  secrets engine (https://developer.hashicorp.com/vault/api-docs/secret/transform),
  which does exactly this -- FPE tokenization with role-scoped encode/decode --
  over the same bearer-token HTTP shape `VaultTransitSigningProvider` already
  uses, for the same reason: no cloud-SDK dependency, `httpx` is enough. **This
  adapter is untested against a real Vault instance**, the identical standing
  limitation noted on `VaultTransitSigningProvider` and the connector work
  (CN-1c/CN-2a) -- exercised here only against a mocked HTTP transport that
  asserts the request/response shape this module sends and expects.

## The local scheme, precisely

`LocalFpeTokenizationProvider` implements format-preserving tokenization for
the digit-run inside a value: non-digit characters (dashes in a formatted card
number, for instance) are left untouched at their original positions, and the
digit run is passed through a keyed, deterministic, invertible transform --
an unbalanced two-sided construction in the same family as NIST FF1/FF3-1
(alternating keyed modular updates to each half, each round's update using an
HMAC-SHA256 of the *other*, untouched half as its round function) but **not**
a validated implementation of either standard; nothing here has been checked
against the FF1/FF3-1 test vectors or reviewed by a cryptographer. That is the
honest boundary already drawn around `LocalHmacSigningProvider` and
`VaultTransitSigningProvider`, stated here for the same reason: this is a
certified *local/dev* provider, not a claim that the scheme is bank-grade on
its own. A production deployment configures `VaultTransformTokenizationProvider`
(or an equivalent bank-approved KMS-backed tokenization service satisfying this
same `TokenizationProvider` protocol) instead.

Determinism is the property the masking pipeline actually needs: the same
input value under the same key always tokenizes to the same token, so a
customer's card number reads as the same token across every row and every
query that projects it -- joins on the tokenized column, GROUP BY, and visual
consistency across results all keep working, which a random per-call token
would break.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol

import httpx

from aida.secrets import SecretResolutionError, SecretResolver
from atlas.platform.config import Settings

# Rounds in the local Feistel-style construction. Even, so the two halves are
# updated an equal number of times; more rounds mix the halves more thoroughly,
# at linear cost -- 10 is comfortably past the point of diminishing returns for
# the digit-run lengths this platform tokenizes (9-19 digits: SSNs, card
# numbers, account numbers).
_ROUNDS = 10


class TokenizationError(RuntimeError):
    """A tokenize or detokenize call failed. Callers must fail closed.

    Never caught by `query_gateway.py` to fall back to plain redaction, and
    never caught by `detokenization_api.py` to fall back to returning the
    token unchanged -- either would silently misrepresent what the platform
    actually did with a sensitive value.
    """


class TokenizationUnavailable(TokenizationError):
    """No usable tokenization provider is configured, or it could not be built.

    Raised instead of substituting a fallback provider, for the same reason
    `SigningUnavailable` is never swapped for a locally-forged signature: a
    tokenization-configured column silently falling back to a different
    provider than the one an operator configured is a worse failure than
    refusing the query.
    """


class TokenizationProvider(Protocol):
    """Tokenize a sensitive value, or reverse a token back to that value.

    See the module docstring for why this mirrors `aida.signing.SigningProvider`
    protocol-for-protocol rather than introducing a new shape.
    """

    async def tokenize(self, value: str) -> str: ...

    async def detokenize(self, token: str) -> str: ...


def _digit_run(value: str) -> tuple[list[int], str]:
    """Positions of every digit character in `value`, and the digits themselves.

    Only the digit run is transformed; everything else (formatting dashes,
    spaces, a leading currency symbol) is reinserted at its original position
    unchanged, so a formatted value like ``4111-1111-1111-1111`` tokenizes to
    another string that is still dash-grouped the same way.
    """
    positions = [index for index, char in enumerate(value) if char.isdigit()]
    digits = "".join(value[index] for index in positions)
    return positions, digits


def _reinsert(template: str, positions: list[int], digits: str) -> str:
    chars = list(template)
    for position, digit in zip(positions, digits, strict=True):
        chars[position] = digit
    return "".join(chars)


def _round_function(key: bytes, round_index: int, side: str, modulus: int) -> int:
    """A keyed pseudorandom integer in `[0, modulus)`, derived from `side`.

    `side` is the untouched half of the digit run for this round -- the round
    function never depends on the half it is about to update, which is what
    keeps the construction invertible (see `_feistel_transform`).
    """
    if modulus <= 1:
        # A single-digit half has no room to permute (`x mod 1 == 0` always);
        # treated as a no-op rather than raising, since the caller already
        # decided this length is short enough that `_feistel_transform` leaves
        # it untouched.
        return 0
    message = f"{round_index}:{side}".encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return int.from_bytes(digest, "big") % modulus


def _feistel_transform(key: bytes, digits: str, *, invert: bool) -> str:
    """Deterministic, invertible, format-preserving transform over a digit string.

    Splits `digits` into two halves `a` (length `u = n // 2`) and `b` (length
    `v = n - u`), then alternates modular updates: even rounds replace `a`
    using a keyed function of `b`, odd rounds replace `b` using a keyed
    function of `a`. Because each round updates only one side using the
    *other*, currently-unchanged side, every round is invertible on its own --
    subtract instead of add -- and running the rounds in reverse order exactly
    undoes the forward pass, which is what `invert=True` does.

    Digit runs shorter than 2 digits are returned unchanged: a single digit
    has no second half to derive a round function from, so there is nothing
    this construction can do to it that would also be invertible.
    """
    n = len(digits)
    if n < 2:
        return digits
    u = n // 2
    v = n - u
    a, b = digits[:u], digits[u:]
    modulus_a, modulus_b = 10**u, 10**v
    rounds = range(_ROUNDS) if not invert else range(_ROUNDS - 1, -1, -1)
    for round_index in rounds:
        if round_index % 2 == 0:
            offset = _round_function(key, round_index, b, modulus_a)
            value = (int(a) - offset) % modulus_a if invert else (int(a) + offset) % modulus_a
            a = str(value).zfill(u)
        else:
            offset = _round_function(key, round_index, a, modulus_b)
            value = (int(b) - offset) % modulus_b if invert else (int(b) + offset) % modulus_b
            b = str(value).zfill(v)
    return a + b


class LocalFpeTokenizationProvider:
    """Development-only fallback: the key is held in process config.

    Not a certified KMS tokenizer -- the same "application-managed key" shape
    `LocalHmacSigningProvider` is, kept only so a local/dev deployment has a
    working tokenizer with no external dependency.
    `Settings.tokenization_provider` can only be `"local"` when `environment`
    is not `"production"` (`reject_insecure_production_configuration`
    enforces it), so this class can never be the tokenizer a production
    deployment relies on to protect a real card number or SSN.
    """

    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")

    async def tokenize(self, value: str) -> str:
        positions, digits = _digit_run(value)
        tokenized_digits = _feistel_transform(self._key, digits, invert=False)
        return _reinsert(value, positions, tokenized_digits)

    async def detokenize(self, token: str) -> str:
        positions, digits = _digit_run(token)
        original_digits = _feistel_transform(self._key, digits, invert=True)
        return _reinsert(token, positions, original_digits)


class VaultTransformTokenizationProvider:
    """KMS-backed tokenizer: HashiCorp Vault's Transform secrets engine over HTTP.

    Holds no key material -- only a `role_name` (which Transform role to use,
    not the underlying tokenization key), a `base_url`, and a bearer `token`
    used to authenticate the *request*. Every `tokenize`/`detokenize` call is a
    round trip to Vault, which performs the FPE transform.

    See https://developer.hashicorp.com/vault/api-docs/secret/transform for
    the wire format this implements:
      POST {base_url}/v1/transform/encode/{role_name}  {"value": "<value>"}
        -> {"data": {"encoded_value": "<token>"}}
      POST {base_url}/v1/transform/decode/{role_name}  {"value": "<token>"}
        -> {"data": {"decoded_value": "<value>"}}
    """

    def __init__(
        self,
        *,
        base_url: str,
        role_name: str,
        token: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._role_name = role_name
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def tokenize(self, value: str) -> str:
        body = await self._call(f"/v1/transform/encode/{self._role_name}", {"value": value})
        encoded = _response_field(body, "encoded_value")
        if not isinstance(encoded, str) or not encoded:
            raise TokenizationError("KMS tokenization response was malformed")
        return encoded

    async def detokenize(self, token: str) -> str:
        body = await self._call(f"/v1/transform/decode/{self._role_name}", {"value": token})
        decoded = _response_field(body, "decoded_value")
        if not isinstance(decoded, str):
            raise TokenizationError("KMS detokenization response was malformed")
        return decoded

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
            # request must never fall back to an unmasked value, a locally-forged
            # token, or a token returned as though it detokenized correctly.
            raise TokenizationError(f"KMS request to {path} failed: {exc}") from exc
        try:
            decoded = response.json()
        except ValueError as exc:
            raise TokenizationError("KMS response was not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise TokenizationError("KMS response was not a JSON object")
        return decoded


def _response_field(body: dict[str, object], field: str) -> object:
    """`body["data"][field]`, tolerating a malformed shape rather than raising
    a `KeyError`/`TypeError` the caller would have to distinguish from a
    genuinely bad value -- both collapse to the same `TokenizationError`."""
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return data.get(field)


def resolve_tokenization_provider(
    settings: Settings, resolver: SecretResolver | None = None
) -> TokenizationProvider:
    """The configured tokenization provider, or `TokenizationUnavailable` with a reason.

    Never returns a fallback: a caller that cannot obtain the configured
    provider must refuse the request that needed tokenization, not tokenize
    (or detokenize) under a different key than the deployment configured.
    """
    provider = settings.tokenization_provider
    if provider == "local":
        return LocalFpeTokenizationProvider(settings.tokenization_key)
    if provider == "vault_transform":
        if not settings.tokenization_vault_url:
            raise TokenizationUnavailable("TOKENIZATION_VAULT_URL_NOT_CONFIGURED")
        if not settings.tokenization_vault_token_reference:
            raise TokenizationUnavailable("TOKENIZATION_VAULT_TOKEN_NOT_CONFIGURED")
        resolver = resolver or SecretResolver(settings)
        try:
            token = resolver.resolve(settings.tokenization_vault_token_reference)
        except SecretResolutionError as exc:
            raise TokenizationUnavailable("TOKENIZATION_VAULT_TOKEN_UNRESOLVABLE") from exc
        return VaultTransformTokenizationProvider(
            base_url=settings.tokenization_vault_url,
            role_name=settings.tokenization_vault_role_name,
            token=token,
            timeout_seconds=settings.tokenization_timeout_seconds,
        )
    raise TokenizationUnavailable(f"TOKENIZATION_PROVIDER_UNSUPPORTED:{provider}")
