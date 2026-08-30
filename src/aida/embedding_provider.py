"""Embeddings from an approved model route: OpenAI or Gemini (ADR-0019, decision N5).

`vector_store.py` decided *where* vectors are searched and needed no extension to do it.
This module decides *what produces them*, which was deliberately left unmade until an
owner chose a model, because the choice is close to irreversible: `index_signature` pins
`(model_id, model_version, dimensions, chunking_version)`, and an embedding is only
comparable to embeddings made by the same model. Changing it invalidates the index rather
than degrading it.

Two properties carry the weight here:

**A stand-in never silently becomes the real thing.** `vector_retrieval.HashEmbeddingProvider`
produces a deterministic vector from a SHA-256 digest. It is a test double, and it is
genuinely useful as one — but a hash has no semantic structure, so a "vector similarity"
score computed from it is noise wearing the name of a signal. Resolution therefore fails
closed (INV-4): with no provider configured, callers get `EmbeddingUnavailable` and are
expected to *drop the vector stage and say so*, never to substitute the hash. Reporting a
capability you do not have is the specific thing INV-9 forbids, and a fused ranking that
blends real lexical signal with hash noise is exactly that, one layer down.

**The credential never reaches this module as a value.** It arrives as a reference and is
resolved through the same path as every model credential, so an embedding key is subject
to the same rotation, the same provider registry and the same production refusal of
`env://` as the generation path.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from aida.model_gateway import ModelGatewayError, ModelOutputInvalid, post_with_retry
from aida.secrets import SecretResolver
from atlas.platform.config import Settings

# Providers that can produce embeddings today. Deliberately the same two the generation
# path supports -- a third embedding provider with no generation adapter would be a second
# credential, a second retry policy and a second failure mode for one capability.
SUPPORTED_EMBEDDING_PROVIDERS = frozenset({"openai", "gemini"})

# Sensible per-provider defaults, used only when `embedding_model_id` is left at `unset`.
# Both support dimension reduction, which is why `embedding_dimensions` is meaningful
# rather than dictated by the model.
DEFAULT_MODEL_IDS: dict[str, str] = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",
}

# One request carries at most this many texts. Both providers accept batches; the cap
# exists so a catalogue-wide backfill cannot build one enormous request whose failure
# costs the whole batch.
MAX_BATCH = 96


class EmbeddingUnavailable(ModelGatewayError):
    """No usable embedding provider. A refusal with a reason, never a degraded success."""


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Vectors plus the identity of what produced them.

    The identity travels with the vectors on purpose: a caller that stores an embedding
    without recording which model made it has stored something it can never safely
    compare again.
    """

    vectors: tuple[tuple[float, ...], ...]
    model_id: str
    provider: str
    dimensions: int


class AsyncEmbeddingProvider(Protocol):
    """Embedding is a network call, so the protocol is async.

    `vector_retrieval.EmbeddingProvider` is the synchronous in-process protocol and stays
    as it is; the hash double satisfies that one. Keeping them separate is what stops a
    remote provider being dropped into a call site that assumes embedding is free.
    """

    @property
    def provider(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> EmbeddingBatch: ...


def _validate(vectors: list[list[float]], expected_count: int, dimensions: int) -> None:
    """Every failure here is a refusal, because a short or wrong-width batch silently
    misaligns vectors with the texts they describe -- and nothing downstream can detect it."""
    if len(vectors) != expected_count:
        raise ModelOutputInvalid(
            f"embedding provider returned {len(vectors)} vectors for {expected_count} inputs"
        )
    for vector in vectors:
        if len(vector) != dimensions:
            raise ModelOutputInvalid(
                f"embedding provider returned a {len(vector)}-dimension vector, "
                f"expected {dimensions}"
            )


class OpenAIEmbeddingProvider:
    """`POST /embeddings`. The 3-series models accept `dimensions`, so the configured
    width is honoured rather than dictated by the model."""

    def __init__(self, settings: Settings, api_key: str, model_id: str) -> None:
        self.settings = settings
        self._api_key = api_key
        self._model_id = model_id

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self.settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch((), self._model_id, "openai", self.dimensions)
        body: dict[str, Any] = {
            "model": self._model_id,
            "input": texts,
            "dimensions": self.dimensions,
        }
        async with httpx.AsyncClient(timeout=self.settings.model_timeout_seconds) as client:
            payload = await post_with_retry(
                client=client,
                url=f"{self.settings.openai_base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                body=body,
                attempts=self.settings.model_provider_max_attempts,
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise ModelOutputInvalid("openai embeddings response has an invalid shape")
        # Ordered by `index`, not by arrival. The API documents the order but relying on
        # it would make a silent misalignment possible on any future change.
        try:
            ordered = sorted(data, key=lambda item: int(item["index"]))
            vectors = [[float(v) for v in item["embedding"]] for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelOutputInvalid("openai embeddings response has an invalid shape") from exc
        _validate(vectors, len(texts), self.dimensions)
        return EmbeddingBatch(
            tuple(tuple(v) for v in vectors), self._model_id, "openai", self.dimensions
        )


class GeminiEmbeddingProvider:
    """`:batchEmbedContents`, with the key in a header rather than a query parameter --
    a credential in a URL ends up in access logs, proxies and browser history."""

    def __init__(self, settings: Settings, api_key: str, model_id: str) -> None:
        self.settings = settings
        self._api_key = api_key
        self._model_id = model_id

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self.settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch((), self._model_id, "gemini", self.dimensions)
        model_path = f"models/{self._model_id}"
        body: dict[str, Any] = {
            "requests": [
                {
                    "model": model_path,
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": self.dimensions,
                }
                for text in texts
            ]
        }
        base = self.settings.gemini_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=self.settings.model_timeout_seconds) as client:
            payload = await post_with_retry(
                client=client,
                url=f"{base}/{model_path}:batchEmbedContents",
                headers={"x-goog-api-key": self._api_key},
                body=body,
                attempts=self.settings.model_provider_max_attempts,
            )
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise ModelOutputInvalid("gemini embeddings response has an invalid shape")
        try:
            vectors = [[float(v) for v in item["values"]] for item in embeddings]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelOutputInvalid("gemini embeddings response has an invalid shape") from exc
        _validate(vectors, len(texts), self.dimensions)
        return EmbeddingBatch(
            tuple(tuple(v) for v in vectors), self._model_id, "gemini", self.dimensions
        )


def resolve_embedding_model_id(settings: Settings) -> str:
    """The configured model, or the provider's default when none was named."""
    configured = settings.embedding_model_id
    if configured and configured != "unset":
        return configured
    return DEFAULT_MODEL_IDS.get(settings.embedding_provider, "unset")


def resolve_embedding_provider(
    settings: Settings, resolver: SecretResolver
) -> AsyncEmbeddingProvider:
    """The configured provider, or `EmbeddingUnavailable` with a reason.

    Never returns a fallback. A caller that cannot embed must drop the vector stage and
    report that it did, because a retrieval result that quietly stopped using half its
    signals still looks like a complete answer.
    """
    provider = settings.embedding_provider
    if provider == "unset":
        raise EmbeddingUnavailable("EMBEDDING_PROVIDER_NOT_CONFIGURED")
    if provider not in SUPPORTED_EMBEDDING_PROVIDERS:
        raise EmbeddingUnavailable(f"EMBEDDING_PROVIDER_UNSUPPORTED:{provider}")

    reference = settings.embedding_credential_reference
    if not reference:
        raise EmbeddingUnavailable("EMBEDDING_CREDENTIAL_NOT_CONFIGURED")
    try:
        api_key = _resolve_embedding_credential(reference, settings, resolver)
    except Exception as exc:  # noqa: BLE001 - re-raised as a refusal with a reason code
        raise EmbeddingUnavailable("EMBEDDING_CREDENTIAL_UNRESOLVABLE") from exc

    model_id = resolve_embedding_model_id(settings)
    if model_id == "unset":
        raise EmbeddingUnavailable("EMBEDDING_MODEL_NOT_CONFIGURED")

    if provider == "openai":
        return OpenAIEmbeddingProvider(settings, api_key, model_id)
    return GeminiEmbeddingProvider(settings, api_key, model_id)


def _resolve_embedding_credential(
    reference: str, settings: Settings, resolver: SecretResolver
) -> str:
    """Same shape as the generation path's resolution, and deliberately so: one place
    where a model credential can be a placeholder, one production refusal of `env://`."""
    local_keys = {
        "env://OPENAI_API_KEY": settings.openai_api_key,
        "env://GEMINI_API_KEY": settings.gemini_api_key,
    }
    configured = local_keys.get(reference)
    if configured is not None:
        value = configured.get_secret_value()
        if value and not value.startswith("replace-"):
            return value
        raise EmbeddingUnavailable("EMBEDDING_CREDENTIAL_IS_A_PLACEHOLDER")
    return resolver.resolve(reference)


def index_signature(settings: Settings) -> str:
    """What an index built with this configuration is comparable to.

    Stored alongside vectors so that a configuration change invalidates the index loudly
    instead of mixing incomparable vectors, which fails as quietly bad search.
    """
    return ":".join(
        [
            settings.embedding_provider,
            resolve_embedding_model_id(settings),
            settings.embedding_model_version,
            str(settings.embedding_dimensions),
            str(settings.embedding_chunking_version),
        ]
    )
