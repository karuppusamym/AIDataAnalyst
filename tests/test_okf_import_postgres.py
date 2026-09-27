"""R11-OKF03: the OKF import suites, run again against a real PostgreSQL.

The import, routine and review-preview suites run on in-memory SQLite, which does not enforce
foreign keys here, compiles `FOR UPDATE` away and treats a JSON column as text. This module is
the small harness that runs the same test bodies against PostgreSQL instead: it imports the
database-backed tests from those modules, so pytest collects them here and resolves their
`session` and `settings` fixtures from this module -- a private scratch database, emptied before
each test. No test is rewritten for PostgreSQL; a test that passes on SQLite and fails here is a
finding about the code or the fixture, not about this file.

**Database.** A private scratch database per run, `<app db>_okf03_import_<random>`, created
here and dropped when the module finishes -- never a fixed name another suite or a peer session
could wipe mid-run (`AIDA_OKF_IMPORT_POSTGRES_TEST_DATABASE_URL` overrides it and is then left
in place). Built from `Base.metadata`, the convention of every DB-backed suite except the
migration drift gate. Only a failure to *reach* PostgreSQL skips; anything after that fails.

The pure suite (`tests/test_okf_import_hostile.py`) reads no database and is not repeated.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aida import (  # noqa: F401 -- registers every ORM table on Base.metadata
    change_signal_models,
    envelope_models,
    governed_execution_models,
    graph_store,
    models,
    okf_store_models,
    ontology_models,
    procedure_lineage_models,
    quality_rule_proposal_model,
    review_batch_models,
    sql_workspace_models,
)
from aida.config import Settings
from aida.db import Base
from atlas.platform.config import get_settings

# The database-backed tests, collected here so they run with this module's fixtures.
from tests.test_okf_import import (  # noqa: F401
    test_a_bundle_from_another_tenant_is_a_scope_mismatch,
    test_a_bundle_whose_publication_is_not_retained_is_refused,
    test_a_description_approved_after_the_apply_is_skipped_at_approval,
    test_a_description_approved_since_the_export_is_a_conflict_not_an_overwrite,
    test_a_preview_that_went_stale_refuses_the_apply,
    test_an_agent_principal_cannot_decide_an_import,
    test_another_tenants_reader_is_refused_before_anything_is_read,
    test_imported_verification_and_status_grant_nothing,
    test_preview_apply_review_and_approved_re_export,
    test_screened_text_is_refused_and_never_persisted,
    test_the_same_bundle_is_not_proposed_twice,
)
from tests.test_okf_import_review import (  # noqa: F401
    test_a_batch_preview_shows_each_document_with_its_before_and_after_text,
    test_a_conflict_is_predicted_as_approval_decides_it,
    test_a_reader_who_may_not_read_the_source_is_refused_by_reason,
    test_a_routine_preview_shows_the_routine_and_a_moved_body,
    test_another_tenant_and_other_reviews_are_not_found,
    test_documents_are_paged_and_counts_cover_every_page,
    test_text_screening_withholds_is_never_shown,
)
from tests.test_okf_import_routines import (  # noqa: F401
    test_a_body_that_changed_since_the_export_is_a_conflict,
    test_a_capture_version_rewritten_in_the_file_vouches_for_nothing,
    test_a_change_after_the_apply_refuses_the_approval,
    test_a_change_between_preview_and_apply_is_a_stale_preview,
    test_a_description_approved_since_the_export_is_a_conflict,
    test_a_package_document_is_listed_and_never_proposed,
    test_a_rejected_import_is_retained_and_its_text_not_proposed_again,
    test_a_retired_routine_is_not_proposed,
    test_a_routine_below_the_evidence_bar_is_refused,
    test_a_routine_purpose_is_proposed_reviewed_and_re_exported,
    test_a_routine_with_an_open_draft_is_a_conflict,
    test_an_agent_principal_cannot_decide_it,
    test_text_atlas_would_not_publish_is_refused,
    test_the_adapter_decides_only_the_draft_its_review_was_raised_for,
    test_the_importer_is_refused_as_approver_by_the_editor_stamp,
)
from tests.test_okf_import_routines import (  # noqa: F401
    test_screened_text_is_refused_and_never_persisted as test_routine_text_screening,
)
from tests.test_okf_import_routines import (  # noqa: F401
    test_the_same_bundle_is_not_proposed_twice as test_a_routine_bundle_is_not_proposed_twice,
)

_OVERRIDE = "AIDA_OKF_IMPORT_POSTGRES_TEST_DATABASE_URL"


def _scratch_url() -> str:
    override = os.environ.get(_OVERRIDE)
    if override:
        return override
    root, _, dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{dbname}_okf03_import_{uuid4().hex[:10]}"


def _maintenance_url(db_url: str) -> str:
    root, _, _ = db_url.rpartition("/")
    _, _, app_dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{app_dbname}"


async def _create(db_url: str) -> None:
    _, _, dbname = db_url.rpartition("/")
    admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Enforced at commit, not per statement. The OKF01 estate fixture the suites share
            # (`tests/test_okf_export._estate`) adds parents and children in one flush, and these
            # models carry plain foreign-key columns with no `relationship()` for the unit of
            # work to order by -- a per-statement check fails on insert order alone, which
            # SQLite (no FK enforcement here) never sees. Deferred, every reference must still
            # hold when each transaction commits, so a dangling one still fails the test.
            constraints = (
                await conn.execute(
                    text(
                        "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                        "WHERE contype = 'f' AND connamespace = 'public'::regnamespace"
                    )
                )
            ).all()
            for table, name in constraints:
                await conn.execute(
                    text(
                        f'ALTER TABLE {table} ALTER CONSTRAINT "{name}" '
                        "DEFERRABLE INITIALLY DEFERRED"
                    )
                )
    finally:
        await engine.dispose()


async def _drop(db_url: str) -> None:
    _, _, dbname = db_url.rpartition("/")
    admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    db_url = _scratch_url()
    try:
        asyncio.run(_create(db_url))
    except Exception as exc:  # noqa: BLE001 -- unreachable PostgreSQL means skip, not fail
        pytest.skip(
            f"PostgreSQL is not reachable for the OKF import scratch database "
            f"({type(exc).__name__}); the SQLite suites still run."
        )
    try:
        yield db_url
    finally:
        if not os.environ.get(_OVERRIDE):
            asyncio.run(_drop(db_url))


@pytest_asyncio.fixture
async def session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    """The imported tests' `session`, on PostgreSQL, over an emptied schema.

    Emptied rather than recreated: the tests assert on "no batch was written" and sweep every
    row for a sentinel, so each must start from nothing, and truncating is far cheaper than
    building four hundred tables per test. The gate's durable shadow-record path opens a
    second session from `info["maker"]`, as the SQLite fixtures provide.
    """
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, okf_import_enabled=True)  # type: ignore[call-arg]
