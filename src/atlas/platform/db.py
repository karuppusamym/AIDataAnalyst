"""Database session management (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`).

Moved from `aida.db`, with its internal `config` import repointed at
`atlas.platform.config`. `aida.db` now re-exports from here for backward
compatibility; new code should import from this module directly.

`TimestampMixin` and `utc_now` moved here from `aida.models` under tracker
ST-05 (Phase 3 of the refactor plan): every per-module `models.py` needs
them, including modules that have not been extracted yet, so they belong in
`platform/` rather than in any one module. `aida.models` re-exports both for
backward compatibility -- every existing `from aida.models import
TimestampMixin` caller keeps working unchanged.

The engine/session-factory/settings singletons below are built lazily, on
first real use, rather than at import time: a pure-local consumer that only
needs `Base`/`TimestampMixin` (or, transitively, a pydantic model / rendering
helper defined in `aida.models`/`aida.schemas`/`aida.tool_rendering`) must be
able to import this module without an `AIDA_ENVIRONMENT`-validated `Settings`
object or the `asyncpg` driver being importable -- see `sdk/aida_tool_sdk`'s
module docstring for the concrete case this unblocks.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from atlas.platform.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from atlas.platform.config import Settings

    # Type-only declarations (no runtime assignment -- this branch never
    # executes) so mypy resolves the real type of these lazily-provided
    # module attributes instead of __getattr__'s return annotation
    # (`object`), which otherwise makes every `engine`/`session_factory`/
    # `settings` call site across the codebase an "object not callable"
    # error. `__getattr__` below still supplies the actual value at runtime.
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


@lru_cache
def get_engine() -> "AsyncEngine":
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        # INV-6 / ADR-0014: without this, SQLAlchemy appends `[SQL: ...]
        # [parameters: (...)]` -- real bound values -- to any exception raised
        # during statement execution, and driver errors routinely quote row
        # data on top of that. Never disable this.
        hide_parameters=True,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def __getattr__(name: str) -> object:
    # Backward-compatible attribute access for callers that still do
    # `from atlas.platform.db import engine` / `session_factory` / `settings`
    # as plain module attributes. Resolved lazily on first access instead of
    # restored as eager module-level assignments, so importing this module
    # stays free of any DB/settings side effect.
    if name == "engine":
        return get_engine()
    if name == "session_factory":
        return get_session_factory()
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
