"""AU-8: migration <-> ORM drift gate.

`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` Section 5: "84
migrations, zero tests apply them. All 19 DB-backed test files build schema
from the ORM [`Base.metadata.create_all` against SQLite in-memory -- see
e.g. `tests/test_tier0_invariants.py`], so ORM<->migration drift is
structurally invisible. The tracker records this bug firing once already
(DQ-1) -- the instance was fixed, no gate was added."

This is that gate. It is deliberately **not** part of the fast fixture path
every other DB-backed test uses: running all of Alembic's migrations against
a real Postgres database once, per session, is slow (seconds, not the
milliseconds an in-memory-SQLite `create_all()` costs) and would slow down
every other test file if folded into a shared fixture. It runs on its own,
here, exactly once.

What it does, matching the tracker's AU-8 exit criterion verbatim ("One test
applies all 84 migrations to an empty database and diffs against
`Base.metadata`"):

  1. Resets a real Postgres database to a genuinely empty `public` schema.
  2. Runs `alembic upgrade head` through Alembic's Python API (not a
     subprocess) against that database.
  3. Reflects the resulting schema and diffs it against `Base.metadata`
     using `alembic.autogenerate.compare_metadata` -- the same machinery
     `alembic revision --autogenerate` uses, run in the opposite direction
     (comparing a fully-migrated database back against the models, instead
     of proposing a migration).
  4. Fails on any drift at all: a column the ORM declares that no migration
     created, a migration-created column the ORM doesn't know about, type
     mismatches, index/constraint mismatches -- everything `compare_metadata`
     can see.

Why Postgres, not SQLite: `get_settings().database_url` defaults to
`postgresql+asyncpg://...`, `migrations/env.py` is written against a single
async Postgres connection, and multiple migrations use Postgres-only DDL
(`CREATE EXTENSION pg_trgm`, `USING gin (... gin_trgm_ops)`) that has no
SQLite equivalent -- applying the real migration set requires a real
Postgres. If one is not reachable, this test skips with the connection
error as the reason rather than failing CI on infrastructure absence; see
`.github/workflows/ci.yml`'s `migration-drift` job for where a real one is
provided in CI via a Postgres service container.

Running it locally: point `AIDA_MIGRATION_DRIFT_TEST_DATABASE_URL` at a
scratch Postgres database (any role that owns the database it names is
enough -- `CREATEDB` is not required, since this test resets the `public`
schema in-place rather than creating a new database), or start Postgres
locally with credentials matching `Settings.database_url`'s default
(`postgresql+asyncpg://aida:aida-local-only@localhost:5432/aida`) and this
test will default to `.../aida_migration_drift_test` on the same server.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aida import envelope_models, models  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.db import Base
from atlas.platform.config import get_settings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, "alembic.ini")

# AU-8: objects created by raw `op.execute` DDL rather than SQLAlchemy
# constructs, because the ORM has no clean way to declare them (functional/
# trigram GIN expression indexes -- see f9a2b3c4d5e6_catalog_scale_indexes.py).
# They exist in every real database but are, by design, absent from
# Base.metadata. Keep this set in sync with the identical filter in
# `migrations/env.py` (`RAW_SQL_ONLY_INDEXES`) -- duplicated rather than
# imported because `migrations/env.py` runs Alembic's migration commands as
# a module-level side effect on import, which this test must not trigger
# just to read a constant.
RAW_SQL_ONLY_INDEXES = frozenset(
    {
        "ix_metadata_table_catalog_page",
        "ix_metadata_table_name_trgm",
        "ix_metadata_table_description_trgm",
    }
)


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    if type_ == "index" and name in RAW_SQL_ONLY_INDEXES:
        return False
    return True


def _test_database_url() -> str:
    """The Postgres database this test resets and migrates.

    An explicit override always wins; otherwise this derives a dedicated
    scratch database name from the app's own default connection so the test
    doesn't have to duplicate host/port/credential defaults, and never
    touches the database an app instance would actually use.
    """
    override = os.environ.get("AIDA_MIGRATION_DRIFT_TEST_DATABASE_URL")
    if override:
        return override
    default_url = get_settings().database_url
    root, _, dbname = default_url.rpartition("/")
    if not root or not dbname:
        raise AssertionError(
            f"Settings.database_url {default_url!r} doesn't look like a "
            "'.../<dbname>' URL; cannot derive a scratch database name from it."
        )
    return f"{root}/{dbname}_migration_drift_test"


def _compare_against_orm(sync_conn, metadata):
    ctx = MigrationContext.configure(sync_conn, opts={"include_object": _include_object})
    return compare_metadata(ctx, metadata)


async def _reset_schema(db_url: str) -> None:
    """Wipe `public` to a genuinely empty schema.

    Resetting the schema (rather than requiring `CREATEDB`) is deliberate:
    it works for any role that owns the target database -- the common case
    for a Postgres service container's default user -- without needing
    superuser or database-creation privileges.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def _upgrade_head(db_url: str) -> None:
    """Run `alembic upgrade head` via the Python API. Must be called from a
    plain sync context: `migrations/env.py` drives the async migration run
    with its own top-level `asyncio.run(...)`, which raises if one is
    already active -- so this cannot be awaited from inside our own loop.
    """
    # `migrations/env.py` reads `get_settings().database_url` itself (not
    # whatever URL is passed to the Alembic `Config`), so the only way to
    # point a real `alembic upgrade` at the scratch database is through the
    # same env var + settings cache the app reads at import time.
    prior_url_env = os.environ.get("AIDA_DATABASE_URL")
    os.environ["AIDA_DATABASE_URL"] = db_url
    get_settings.cache_clear()
    try:
        cfg = Config(ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", db_url)
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")
    finally:
        if prior_url_env is None:
            os.environ.pop("AIDA_DATABASE_URL", None)
        else:
            os.environ["AIDA_DATABASE_URL"] = prior_url_env
        get_settings.cache_clear()


async def _diff_against_orm(db_url: str) -> list:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_compare_against_orm, Base.metadata)
    finally:
        await engine.dispose()


async def _probe_reachable(db_url: str) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def test_migration_orm_drift() -> None:
    """Applying every Alembic migration must produce exactly `Base.metadata`.

    Any diff here means either a migration is missing (the ORM declares
    something no migration created) or a migration created something the
    ORM has since stopped declaring -- both are drift the fast, ORM-built
    SQLite fixtures every other DB-backed test uses can never catch, per
    Docs/60-delivery/04-end-to-end-audit-2026-08-30.md Section 5.
    """
    db_url = _test_database_url()

    # Only a failure to even reach Postgres is a skip. A failure anywhere
    # after this -- a broken migration, a real drift diff -- must fail the
    # test, not be swallowed as "environment unavailable".
    try:
        asyncio.run(_probe_reachable(db_url))
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip", not "fail"
        pytest.skip(
            f"Postgres is not reachable at {db_url!r} ({type(exc).__name__}: {exc}); "
            "this gate needs a real Postgres instance -- see this file's module "
            "docstring for how to point it at one, and "
            "`.github/workflows/ci.yml`'s migration-drift job for the CI service "
            "container that provides one there."
        )

    asyncio.run(_reset_schema(db_url))
    _upgrade_head(db_url)  # not awaited: drives its own asyncio.run() internally
    diffs = asyncio.run(_diff_against_orm(db_url))

    if diffs:
        rendered = "\n".join(f"  {i + 1}. {diff!r}" for i, diff in enumerate(diffs))
        pytest.fail(
            f"Migration/ORM drift: {len(diffs)} difference(s) between what "
            "`alembic upgrade head` builds and what `Base.metadata` declares. "
            "Every migration lands in the same shared branch; add the "
            "migration that creates what's missing (or, if the ORM model is "
            "the one that's wrong, fix that instead) -- see "
            "Docs/60-delivery/03-tracker.md row AU-8.\n"
            f"{rendered}"
        )
