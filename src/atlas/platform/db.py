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
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from atlas.platform.config import get_settings

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


settings = get_settings()
engine = create_async_engine(
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
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
