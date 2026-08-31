"""Database session management (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`).

Moved from `aida.db`, with its internal `config` import repointed at
`atlas.platform.config`. `aida.db` now re-exports from here for backward
compatibility; new code should import from this module directly.
"""

from collections.abc import AsyncIterator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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
