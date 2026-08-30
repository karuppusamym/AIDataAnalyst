"""Embeddings come from an approved route, or not at all (N5, decided 2026-08-30).

The property worth protecting here is not "OpenAI returns a vector". It is that a
configuration which cannot embed produces a **refusal**, and never quietly substitutes the
deterministic hash double — because a hash has no semantic structure, so a similarity score
derived from one is noise wearing the name of a signal, and the fused ranking would rank on
it without anything looking wrong.
"""

from typing import Any

import httpx
import pytest

from aida.embedding_provider import (
    DEFAULT_MODEL_IDS,
    EmbeddingUnavailable,
    GeminiEmbeddingProvider,
    OpenAIEmbeddingProvider,
    index_signature,
    resolve_embedding_model_id,
    resolve_embedding_provider,
)
from aida.model_gateway import ModelOutputInvalid
from aida.secrets import SecretResolver
from atlas.platform.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "embedding_dimensions": 8,
        "model_provider_max_attempts": 1,
    }
    base.update(overrides)
    return Settings(**base)


# --- resolution fails closed ------------------------------------------------


def test_an_unconfigured_provider_refuses_rather_than_falling_back() -> None:
    """The whole point. `unset` is an unmade decision, not a disabled feature, and the
    hash double must never stand in for it."""
    settings = _settings()
    with pytest.raises(EmbeddingUnavailable) as refusal:
        resolve_embedding_provider(settings, SecretResolver(settings))
    assert "EMBEDDING_PROVIDER_NOT_CONFIGURED" in str(refusal.value)


def test_a_provider_without_a_credential_refuses() -> None:
    settings = _settings(embedding_provider="openai")
    with pytest.raises(EmbeddingUnavailable) as refusal:
        resolve_embedding_provider(settings, SecretResolver(settings))
    assert "EMBEDDING_CREDENTIAL_NOT_CONFIGURED" in str(refusal.value)


def test_a_placeholder_credential_refuses() -> None:
    """A key left at its `replace-me` default is not a configured key. Accepting it would
    move the failure from startup to the first retrieval call in production."""
    settings = _settings(
        embedding_provider="openai",
        embedding_credential_reference="env://OPENAI_API_KEY",
        openai_api_key="replace-me",
    )
    with pytest.raises(EmbeddingUnavailable):
        resolve_embedding_provider(settings, SecretResolver(settings))


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_configured_provider_resolves_with_the_documented_default_model(
    provider: str,
) -> None:
    settings = _settings(
        embedding_provider=provider,
        embedding_credential_reference=(
            "env://OPENAI_API_KEY" if provider == "openai" else "env://GEMINI_API_KEY"
        ),
        openai_api_key="sk-test",
        gemini_api_key="gm-test",
    )
    resolved = resolve_embedding_provider(settings, SecretResolver(settings))
    assert resolved.provider == provider
    assert resolved.model_id == DEFAULT_MODEL_IDS[provider]
    assert resolved.dimensions == 8


def test_an_explicit_model_id_overrides_the_default() -> None:
    settings = _settings(embedding_provider="openai", embedding_model_id="text-embedding-3-large")
    assert resolve_embedding_model_id(settings) == "text-embedding-3-large"


# --- the wire format --------------------------------------------------------


def _mock_transport(capture: dict[str, Any], payload: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["headers"] = dict(request.headers)
        import json

        capture["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    capture: dict[str, Any] = {}
    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = capture["transport"]
        return original(*args, **kwargs)

    monkeypatch.setattr("aida.embedding_provider.httpx.AsyncClient", factory)
    return capture


async def test_openai_orders_vectors_by_index_not_by_arrival(
    patched_client: dict[str, Any],
) -> None:
    """The API documents the order; relying on it would make a silent misalignment
    possible on any future change, and a misaligned vector cannot be detected downstream."""
    patched_client["transport"] = _mock_transport(
        patched_client,
        {
            "data": [
                {"index": 1, "embedding": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
            ]
        },
    )
    provider = OpenAIEmbeddingProvider(_settings(), "sk-test", "text-embedding-3-small")
    batch = await provider.embed(["first", "second"])
    assert batch.vectors[0] == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert batch.vectors[1] == (0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5)
    assert patched_client["body"]["dimensions"] == 8
    assert patched_client["headers"]["authorization"] == "Bearer sk-test"


async def test_gemini_sends_the_key_in_a_header_never_the_url(
    patched_client: dict[str, Any],
) -> None:
    """A credential in a query string ends up in access logs, proxies and history."""
    patched_client["transport"] = _mock_transport(
        patched_client, {"embeddings": [{"values": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]}]}
    )
    provider = GeminiEmbeddingProvider(_settings(), "gm-secret", "gemini-embedding-001")
    batch = await provider.embed(["only"])
    assert batch.vectors == ((0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8),)
    assert "gm-secret" not in patched_client["url"]
    assert patched_client["headers"]["x-goog-api-key"] == "gm-secret"


async def test_a_short_batch_is_a_refusal_not_a_partial_result(
    patched_client: dict[str, Any],
) -> None:
    """Two texts in, one vector out. Accepting that silently misaligns every vector
    with the text it describes, and nothing downstream can notice."""
    patched_client["transport"] = _mock_transport(
        patched_client,
        {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]}]},
    )
    provider = OpenAIEmbeddingProvider(_settings(), "sk-test", "text-embedding-3-small")
    with pytest.raises(ModelOutputInvalid):
        await provider.embed(["one", "two"])


async def test_a_wrong_width_vector_is_a_refusal(patched_client: dict[str, Any]) -> None:
    """The index pins its dimension. A vector of the wrong width is not comparable to
    anything already stored, so storing it would corrupt the index quietly."""
    patched_client["transport"] = _mock_transport(
        patched_client, {"embeddings": [{"values": [0.1, 0.2]}]}
    )
    provider = GeminiEmbeddingProvider(_settings(), "gm-test", "gemini-embedding-001")
    with pytest.raises(ModelOutputInvalid):
        await provider.embed(["only"])


async def test_an_empty_input_makes_no_network_call(patched_client: dict[str, Any]) -> None:
    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("embedding an empty batch must not reach the provider")

    patched_client["transport"] = httpx.MockTransport(explode)
    provider = OpenAIEmbeddingProvider(_settings(), "sk-test", "text-embedding-3-small")
    assert (await provider.embed([])).vectors == ()


# --- the signature that makes a change loud ---------------------------------


def test_changing_any_pinned_field_changes_the_index_signature() -> None:
    """An embedding is only comparable to embeddings from the same model. The signature
    is what turns "the model changed" into an invalidated index rather than quietly bad
    search over a mix of incomparable vectors."""
    base = _settings(embedding_provider="openai", embedding_model_id="text-embedding-3-small")
    baseline = index_signature(base)
    for field, value in (
        ("embedding_provider", "gemini"),
        ("embedding_model_id", "text-embedding-3-large"),
        ("embedding_model_version", "2"),
        ("embedding_dimensions", 16),
        ("embedding_chunking_version", 2),
    ):
        overrides: dict[str, object] = {
            "embedding_provider": "openai",
            "embedding_model_id": "text-embedding-3-small",
        }
        overrides[field] = value
        changed = _settings(**overrides)
        assert index_signature(changed) != baseline, field
