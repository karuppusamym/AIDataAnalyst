import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# `envelope_models`/`graph_store`/`procedure_lineage_models` are imported for their
# side effect of registering additional tables on `aida.db.Base.metadata` (Group J's
# `GraphStoreOrganizationSetting`, Group I's `DeepProcedureLineageEdge`/
# `ProcedureToolGenerationRecord`, same pattern envelope_models already used) so
# autogenerate/create_all see them.
from aida import envelope_models, graph_store, models, procedure_lineage_models  # noqa: F401
from aida.config import get_settings
from aida.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata

# AU-8: objects created by raw `op.execute` DDL rather than SQLAlchemy
# constructs, because the ORM has no clean way to declare them (functional/
# trigram GIN expression indexes). They exist in every real database but are,
# by design, absent from Base.metadata -- exclude them here so autogenerate
# (and tests/test_migration_orm_drift.py, which keeps its own copy of this
# set in sync) doesn't propose spuriously dropping them.
RAW_SQL_ONLY_INDEXES = frozenset(
    {
        "ix_metadata_table_catalog_page",  # f9a2b3c4d5e6_catalog_scale_indexes.py
        "ix_metadata_table_name_trgm",  # f9a2b3c4d5e6_catalog_scale_indexes.py
        "ix_metadata_table_description_trgm",  # f9a2b3c4d5e6_catalog_scale_indexes.py
    }
)


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    if type_ == "index" and name in RAW_SQL_ONLY_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
