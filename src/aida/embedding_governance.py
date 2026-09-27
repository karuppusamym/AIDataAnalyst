"""R11-MP15: embedding calls under the governance model calls already have.

`embedding_provider.resolve_embedding_provider` picks OpenAI or Gemini from
settings alone. So the question and catalog text went to a hosted embedding API
outside every control a generation call passes: no approved route, no kill
switch, no token quota, no spend attribution. This module is the one place an
embedding call site gets its provider from, and it adds those four:

* **Kill switch.** An engaged organization-wide switch -- "stop model use now"
  -- stops embeddings too, and so does a switch scoped to the approved
  EMBEDDINGS route when there is one.
* **Approved route.** Where `Settings.embedding_route_requires_approval` holds
  (by default in staging and production), the organization needs an APPROVED
  model route with the EMBEDDINGS capability whose provider and model are the
  ones configured. Without one, nothing is embedded.
* **Quota.** Each `embed` call reserves its estimated tokens against the
  model-token quota before it is sent (R11-MP14's mechanism), and a spent
  window refuses it.
* **Attribution.** The tokens are settled into the tenant and source windows.

Every refusal is `EmbeddingUnavailable`, which the retrieval vector stage
already treats as "drop the vector signal and say so", and which the index
rebuild treats as "skip this pass". Nothing here makes an embedding call fail
a question.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.embedding_provider import (
    AsyncEmbeddingProvider,
    EmbeddingBatch,
    EmbeddingUnavailable,
    resolve_embedding_model_id,
    resolve_embedding_provider,
)
from aida.model_gateway import (
    BYTES_PER_ESTIMATED_TOKEN,
    GLOBAL_KILL_SWITCH_SCOPE,
    kill_switch_blocking_state,
)
from aida.models import ModelRouteConfiguration
from aida.secrets import SecretResolver
from aida.usage_quotas import QuotaRefused, UsageDimension, consume_quota, settle_quota

#: `Settings.embedding_provider` -> the `ModelRouteConfiguration.provider_type`
#: a route must declare to approve it.
_ROUTE_PROVIDER_TYPES: Final[dict[str, str]] = {
    "openai": "OPENAI",
    "gemini": "GOOGLE_GEMINI",
}
EMBEDDINGS_CAPABILITY: Final = "EMBEDDINGS"


async def approved_embedding_route(
    session: AsyncSession, organization_id: UUID, settings: Settings
) -> ModelRouteConfiguration | None:
    """The organization's newest APPROVED EMBEDDINGS route for the configured
    provider and model, or None."""
    provider_type = _ROUTE_PROVIDER_TYPES.get(settings.embedding_provider)
    if provider_type is None:
        return None
    model_id = resolve_embedding_model_id(settings)
    rows = (
        await session.scalars(
            select(ModelRouteConfiguration)
            .where(
                ModelRouteConfiguration.organization_id == organization_id,
                ModelRouteConfiguration.status == "APPROVED",
                ModelRouteConfiguration.provider_type == provider_type,
                ModelRouteConfiguration.model_id == model_id,
            )
            .order_by(ModelRouteConfiguration.version.desc())
        )
    ).all()
    for row in rows:
        if EMBEDDINGS_CAPABILITY in (row.capabilities or []):
            return row
    return None


class GovernedEmbeddingProvider:
    """An embedding provider whose every call is reserved, settled and attributed."""

    def __init__(
        self,
        inner: AsyncEmbeddingProvider,
        *,
        session: AsyncSession,
        settings: Settings,
        organization_id: UUID,
        datasource_id: UUID | None,
    ) -> None:
        self._inner = inner
        self._session = session
        self._settings = settings
        self._organization_id = organization_id
        self._datasource_id = datasource_id

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        estimate = max(1, sum(len(text) for text in texts) // BYTES_PER_ESTIMATED_TOKEN)
        try:
            reserved = await consume_quota(
                self._session,
                self._settings,
                organization_id=self._organization_id,
                datasource_id=self._datasource_id,
                dimension=UsageDimension.MODEL_TOKENS,
                amount=estimate,
            )
        except QuotaRefused as refused:
            raise EmbeddingUnavailable(
                f"MODEL_TOKEN_QUOTA_EXHAUSTED:{refused.reason_code}"
            ) from refused
        try:
            return await self._inner.embed(texts)
        finally:
            # Embedding APIs bill input only, and the input was sent whether the
            # call answered or not: the estimate is the charge either way.
            await settle_quota(
                self._session,
                self._settings,
                organization_id=self._organization_id,
                datasource_id=self._datasource_id,
                dimension=UsageDimension.MODEL_TOKENS,
                reserved=estimate if reserved else 0,
                actual=estimate,
            )


async def governed_embedding_provider(
    session: AsyncSession,
    settings: Settings,
    *,
    organization_id: UUID,
    datasource_id: UUID | None = None,
    inner: AsyncEmbeddingProvider | None = None,
) -> GovernedEmbeddingProvider:
    """The configured embedding provider, if governance admits it for this
    organization now; otherwise `EmbeddingUnavailable` with a reason code.

    `inner` is the provider a call site already resolved from settings; each call
    site keeps resolving its own, so the configuration refusal it has always
    raised (and tests patch) is unchanged, and this adds the governance on top.
    """
    blocking = await kill_switch_blocking_state(session, organization_id, None)
    if blocking is not None and blocking.route_key == GLOBAL_KILL_SWITCH_SCOPE:
        raise EmbeddingUnavailable("KILL_SWITCH_ENGAGED")
    if inner is None:
        inner = resolve_embedding_provider(settings, SecretResolver(settings))
    route = await approved_embedding_route(session, organization_id, settings)
    if route is None and settings.embedding_route_requires_approval:
        raise EmbeddingUnavailable("EMBEDDING_ROUTE_NOT_APPROVED")
    if route is not None:
        scoped = await kill_switch_blocking_state(session, organization_id, route.route_key)
        if scoped is not None:
            raise EmbeddingUnavailable("KILL_SWITCH_ENGAGED")
    return GovernedEmbeddingProvider(
        inner,
        session=session,
        settings=settings,
        organization_id=organization_id,
        datasource_id=datasource_id,
    )
