"""R11-OKF02 (design item 14B): stored, incrementally rebuilt OKF bundles read through one door.

What this module proves, in the order the tracker row names it:

* **Durable, atomic publication.** A read publishes once and later reads serve the stored rows
  without freezing again; a publish that fails part-way leaves the previous head and its complete
  document set untouched; a concurrent publisher that wins the race is served rather than failed.
* **Incremental rebuild (OKF-C).** A redefined view regenerates its own document and the index
  that lists it, every other document keeps its hash *and* is carried rather than re-rendered, a
  no-op rescan publishes nothing, and an incremental rebuild is byte-identical to a full export
  across every kind of change -- which is what makes carrying a document safe.
* **One snapshot, every door.** REST and MCP are served the same publication and the same bytes,
  and every reading surface reaches the one store function and renders nothing itself.
* **Authorization change, not only source change.** A bundle built under a cross-boundary grant is
  not served once the grant is revoked -- not by the current read and not by a pinned read of its
  publication id -- because the lineage key is the reader's live admission.
* **INV-6 on the storage path.** Sentinel bodies, definitions, defaults, a tool's SQL and its
  parameter defaults are planted, and every stored row -- snapshot, document, head, audit,
  outbox, consumption edge -- is scanned for them after a publish *and* a rebuild.
* `log.md` per scope and tool-version documents; early refusal; a capture that races a change.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import yaml
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.okf_store as okf_store
from aida.change_signal_models import MetadataChangeSignal
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataViewDefinition
from aida.mcp_server import _handle_resources_read
from aida.models import (
    AssetDocumentationVersion,
    AuditEvent,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    CrossBoundaryGrant,
    GovernedTool,
    GovernedToolVersion,
    MetadataColumn,
    OutboxEvent,
)
from aida.okf_export import (
    DESCRIPTION_APPROVED,
    TRIGGER_INITIAL,
    TRIGGER_REVALIDATION,
    TRIGGER_SOURCE_CHANGE,
    TYPE_TOOL_VERSION,
    OkfApproval,
    OkfDescription,
    OkfPublicationStamp,
    OkfSchemaFacts,
    OkfSnapshot,
    OkfSourceFacts,
    OkfToolFacts,
    OkfToolInput,
    export_okf_bundle,
    export_okf_bundle_incremental,
    is_log_path,
    object_key,
    schema_key,
    snapshot_from_document,
    source_key,
    tool_version_key,
    validate_atlas_publish_policy,
    validate_okf_conformance,
)
from aida.okf_export_api import (
    download_okf_bundle,
    inspect_okf_bundle,
    list_okf_publications,
    read_object_okf_knowledge,
    read_okf_document,
)
from aida.okf_store import (
    RETAINED_PUBLICATIONS,
    OkfPublishedBundle,
    load_documents,
    read_published_bundle,
)
from aida.okf_store_models import OkfBundleDocument, OkfBundleHead, OkfBundlePublication
from tests.support.app_surface import reaches_call, references_name
from tests.test_inv6_value_freedom import _persisted_values
from tests.test_okf_export import (
    _SENTINELS,
    _context,
    _estate,
    _product,
    _snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Planted in a tool version's SQL template and its parameter declarations. Neither may reach a
#: stored row: a tool is exported by interface and invocation, never by its code or a literal.
SENTINEL_TOOL_SQL = "ZZQ-OKF02-SENTINEL-TOOLSQL-5e17"
SENTINEL_TOOL_DEFAULT = "ZZQ-OKF02-SENTINEL-TOOLDEFAULT-a930"
#: Planted in a *redefined* view, so the rebuild path is scanned as well as the first publish.
SENTINEL_REDEFINED = "ZZQ-OKF02-SENTINEL-REDEFINED-0c4b"
_ALL_SENTINELS = (*_SENTINELS, SENTINEL_TOOL_SQL, SENTINEL_TOOL_DEFAULT, SENTINEL_REDEFINED)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # StaticPool: the gate's durable shadow-record path opens a second session, and every
    # connection to an unpooled in-memory SQLite gets a database of its own.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


# --- helpers ----------------------------------------------------------------------------


async def _read(
    session: AsyncSession,
    settings: Settings,
    version: ContextProductVersion,
    *,
    now: datetime | None = None,
    publication_id: UUID | None = None,
) -> OkfPublishedBundle:
    return await read_published_bundle(
        session,
        version.id,
        _context(version.organization_id),
        settings,
        now=now,
        publication_id=publication_id,
    )


async def _documents(
    session: AsyncSession, publication: OkfBundlePublication
) -> dict[str, OkfBundleDocument]:
    return {row.path: row for row in await load_documents(session, publication)}


def _path_of(documents: dict[str, OkfBundleDocument], key: str) -> str:
    matches = [path for path, row in documents.items() if row.subject_key == key]
    assert len(matches) == 1, (key, matches)
    return matches[0]


def _warehouse_key(estate: dict[str, Any], table: str) -> str:
    datasource, catalog, schema = estate["datasources"]["warehouse"]
    return object_key(str(datasource.id), catalog.name, schema.name, table)


async def _signal(
    session: AsyncSession, estate: dict[str, Any], table: str, kind: str = "VIEW"
) -> None:
    subject = estate["tables"][table]
    session.add(
        MetadataChangeSignal(
            organization_id=estate["organization"].id,
            datasource_id=subject.datasource_id,
            subject_kind=kind,
            subject_id=subject.id,
            signal_type="DEFINITION_CHANGED" if kind == "VIEW" else "STRUCTURE_CHANGED",
            change_class="STRUCTURAL" if kind == "VIEW" else "COLUMNS_RETYPED",
        )
    )
    await session.flush()


async def _redefine_view(session: AsyncSession, estate: dict[str, Any], text: str) -> None:
    view = estate["tables"]["warehouse.orders_v"]
    definition = await session.scalar(
        select(MetadataViewDefinition).where(MetadataViewDefinition.table_id == view.id)
    )
    assert definition is not None
    definition.definition_sql_redacted = text
    await _signal(session, estate, "warehouse.orders_v")


async def _count(session: AsyncSession, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# --- the pure half: incremental rendering -----------------------------------------------


_STAMP_1 = OkfPublicationStamp(date="2026-09-17", sequence=1, trigger=TRIGGER_INITIAL)
_STAMP_2 = OkfPublicationStamp(date="2026-09-18", sequence=2, trigger=TRIGGER_SOURCE_CHANGE)


def _two_source_snapshot(**changes: Any) -> OkfSnapshot:
    """The OKF01 fixture plus a second source with one table and one tool on the first source,
    so per-source logs and tool documents have something to be scoped against."""
    base = _snapshot()
    other = source_key("22222222-2222-2222-2222-222222222222")
    other_schema = schema_key("22222222-2222-2222-2222-222222222222", "rep", "dbo")
    ledger = object_key("22222222-2222-2222-2222-222222222222", "rep", "dbo", "ledger")
    warehouse = base.sources[0].key
    tool = OkfToolFacts(
        key=tool_version_key("project-1", "revenue_by_day", 3),
        tool_version_id="33333333-3333-3333-3333-333333333333",
        slug="revenue_by_day",
        name="Revenue by day",
        version=3,
        lifecycle="PUBLISHED",
        source_key=warehouse,
        fingerprint="c" * 64,
        inputs=(OkfToolInput(name="day", physical_type="DATE", required=True),),
        description=OkfDescription(
            state=DESCRIPTION_APPROVED,
            text="Daily revenue for one channel. Used by finance.",
            version=3,
            approval=OkfApproval("reviewer", "2026-09-04T00:00:00+00:00", True),
        ),
    )
    values: dict[str, Any] = {
        "sources": (
            *base.sources,
            OkfSourceFacts(
                key=other,
                name="reporting",
                dialect="sqlserver",
                connector_type="sqlserver",
                environment="PROD",
                lifecycle="ACTIVE",
            ),
        ),
        "schemas": (
            *base.schemas,
            OkfSchemaFacts(
                key=other_schema,
                name="dbo",
                catalog_name="rep",
                qualified_name="rep.dbo",
                source_key=other,
                lifecycle="ACTIVE",
            ),
        ),
        "objects": (
            *base.objects,
            replace(
                base.objects[0],
                key=ledger,
                name="ledger",
                qualified_name="rep.dbo.ledger",
                schema_key=other_schema,
                source_key=other,
                links=(),
            ),
        ),
        "tools": (tool,),
    }
    values.update(changes)
    return replace(base, **values)


def _initial(snapshot: OkfSnapshot) -> tuple[dict[str, str], tuple[Any, ...]]:
    bundle, _report, history = export_okf_bundle_incremental(
        snapshot, prior_snapshot=None, prior_documents={}, stamp=_STAMP_1
    )
    return {document.path: document.text for document in bundle.documents}, history


def _mutations() -> dict[str, OkfSnapshot]:
    """One snapshot per kind of change a rebuild must handle, all against `_two_source_snapshot`."""
    base = _two_source_snapshot()
    orders, view = base.objects[0], base.objects[1]
    routine = base.routines[0]
    concept = base.concepts[0]
    tool = base.tools[0]
    renamed = replace(
        view,
        key=object_key("11111111-1111-1111-1111-111111111111", "bank", "sales", "orders_v2"),
        name="orders_v2",
        qualified_name="bank.sales.orders_v2",
    )
    return {
        "view redefined": replace(
            base,
            objects=(
                orders,
                replace(view, definition=replace(view.definition, digest="b" * 64)),  # type: ignore[arg-type]
                *base.objects[2:],
            ),
        ),
        "description approved": replace(
            base,
            objects=(
                replace(
                    orders,
                    description=replace(orders.description, text="A new approved purpose."),
                ),
                *base.objects[1:],
            ),
        ),
        "column reclassified": replace(
            base,
            objects=(
                replace(
                    orders,
                    columns=(replace(orders.columns[0], classification="CONFIDENTIAL"),),
                ),
                *base.objects[1:],
            ),
        ),
        "routine stops writing": replace(
            base,
            routines=(replace(routine, links=()), *base.routines[1:]),
            objects=(replace(orders, links=()), *base.objects[1:]),
        ),
        "view renamed": replace(base, objects=(orders, renamed, *base.objects[2:])),
        # Same identity, new path: every document that links to it prints a moved link while
        # its own facts are unchanged -- the case only link-identity tracking catches.
        "table reclassified as a view": replace(
            base,
            objects=(replace(orders, kind="VIEW", native_object_type="VIEW"), *base.objects[1:]),
        ),
        "view removed": replace(base, objects=(orders, *base.objects[2:])),
        "concept redefined": replace(
            base, concepts=(replace(concept, definition="A signed purchase agreement."),)
        ),
        "tool republished": replace(base, tools=(replace(tool, lifecycle="SUPERSEDED"),)),
        "source renamed": replace(
            base, sources=(replace(base.sources[0], name="warehouse-eu"), *base.sources[1:])
        ),
        "nothing": base,
    }


@pytest.mark.parametrize("change", sorted(_mutations()))
def test_an_incremental_rebuild_is_byte_identical_to_a_full_export(change: str) -> None:
    """The safety argument for carrying a document: across every kind of change, the bundle
    assembled from carried bytes plus re-rendered documents is exactly what a full export of the
    new snapshot (with the same history) produces. A dependency the planner missed would show
    up here as a stale carried document."""
    base = _two_source_snapshot()
    prior, history = _initial(base)
    mutated = _mutations()[change]
    bundle, report, new_history = export_okf_bundle_incremental(
        mutated,
        prior_snapshot=base,
        prior_documents=prior,
        prior_history=history,
        stamp=_STAMP_2,
    )
    full = export_okf_bundle(mutated, history=new_history)
    assert [(d.path, d.text) for d in bundle.documents] == [
        (d.path, d.text) for d in full.documents
    ]
    assert bundle.manifest == full.manifest
    assert validate_atlas_publish_policy(bundle).valid, validate_atlas_publish_policy(bundle)
    # Carrying is real: at least the untouched second source's table was never rendered.
    if change not in {"source renamed"}:
        ledger_path = next(path for path in prior if "table-" in path and path in report.carried)
        assert ledger_path


def test_a_redefined_view_rebuilds_only_its_document_and_the_index_that_lists_it() -> None:
    """OKF-C on the pure renderer: the view's document is the only content document whose bytes
    move; its schema index is re-derived (and keeps its bytes, because an index line names the
    view, not its digest); every other subject document is carried, not rendered."""
    base = _two_source_snapshot()
    prior, history = _initial(base)
    changed = _mutations()["view redefined"]
    bundle, report, _history = export_okf_bundle_incremental(
        changed,
        prior_snapshot=base,
        prior_documents=prior,
        prior_history=history,
        stamp=_STAMP_2,
    )
    view_key = base.objects[1].key
    view_path = next(path for path in prior if view_key in path)
    content_changes = [path for path in report.changed if not is_log_path(path)]
    assert content_changes == [view_path]
    assert report.changed_subjects == (view_key,)
    subject_rendered = {path for path in report.rendered if path.endswith(".md") and "-" in path}
    subject_rendered = {
        path
        for path in subject_rendered
        if any(marker in path for marker in ("/tables/", "/views/", "/routines/", "/packages/"))
        or path.startswith(("concepts/concept-", "tools/tool-version-"))
    }
    assert subject_rendered == {view_path}
    schema_index = view_path.rsplit("/views/", 1)[0] + "/index.md"
    assert schema_index in report.rendered
    assert schema_index not in report.changed
    texts = {document.path: document.text for document in bundle.documents}
    for path, text in prior.items():
        if path == view_path or is_log_path(path):
            continue
        assert texts[path] == text, path
    # The untouched source's log did not move; the root log and the view's source log did.
    other_log = f"sources/source-{base.sources[1].key}/log.md"
    assert texts[other_log] == prior[other_log]
    assert "log.md" in report.changed


def test_a_rebuild_of_unchanged_content_renders_no_subject_document() -> None:
    """The no-op half of OKF-C: nothing moved, so no object, routine, package, concept or tool
    document is rendered and none changes. Only the scope-wide indexes are re-derived."""
    base = _two_source_snapshot()
    prior, history = _initial(base)
    bundle, report, _history = export_okf_bundle_incremental(
        base, prior_snapshot=base, prior_documents=prior, prior_history=history, stamp=_STAMP_2
    )
    assert report.changed_subjects == ()
    assert [path for path in report.changed if not is_log_path(path)] == []
    assert all(
        path.endswith("index.md") or is_log_path(path) for path in report.rendered
    ), report.rendered
    assert {d.path: d.text for d in bundle.documents if not is_log_path(d.path)} == {
        path: text for path, text in prior.items() if not is_log_path(path)
    }


def test_log_md_is_conformant_newest_first_and_scoped_per_source() -> None:
    """Spec section 9: a `log.md` may appear at any level to record that scope's history, with
    ISO date headings. One at the bundle root and one per source directory; a source's log only
    lists publications that moved something under it."""
    base = _two_source_snapshot()
    prior, history = _initial(base)
    bundle, _report, _history = export_okf_bundle_incremental(
        _mutations()["view redefined"],
        prior_snapshot=base,
        prior_documents=prior,
        prior_history=history,
        stamp=_STAMP_2,
    )
    texts = {document.path: document.text for document in bundle.documents}
    assert validate_okf_conformance(texts).valid
    root = texts["log.md"]
    assert root.index("## 2026-09-18") < root.index("## 2026-09-17")
    assert "**Publication 2** (`SOURCE_CHANGE`): 1 changed, 0 added, 0 removed." in root
    assert "**Publication 1** (`INITIAL`): first published with" in root
    warehouse_log = texts[f"sources/source-{base.sources[0].key}/log.md"]
    reporting_log = texts[f"sources/source-{base.sources[1].key}/log.md"]
    assert "Publication 2" in warehouse_log
    assert "Publication 2" not in reporting_log
    # Paths are code spans, never links: an old entry may name a document since removed.
    assert "](" not in root


def test_a_tool_version_is_described_by_interface_and_never_shipped_as_code() -> None:
    """Per-tool-version concept documents. Deliberately not upstream's `Attested Computation`:
    its `computation` / `executor` fields are runnable embedded code, which the design forbids,
    so a tool document carries its interface and how to invoke it through Atlas, nothing to run.
    """
    snapshot = _two_source_snapshot()
    bundle = export_okf_bundle(snapshot)
    tool = snapshot.tools[0]
    document = bundle.document(f"tools/tool-version-{tool.key}.md")
    frontmatter = yaml.safe_load(document.text.split("---\n", 2)[1])
    assert frontmatter["type"] == TYPE_TOOL_VERSION
    assert frontmatter["status"] == "stable"
    assert "verified" not in frontmatter
    atlas = frontmatter["atlas"]
    assert atlas["tool"]["invocation"] == {
        "mcp_tool": "atlas__revenue_by_day",
        "rest": f"POST /v1/tool-versions/{tool.tool_version_id}/execute",
    }
    rendered = "\n".join(d.text for d in bundle.documents)
    assert "Attested Computation" not in rendered
    for forbidden in ("computation:", "executor:", "sql_template", "SELECT "):
        assert forbidden not in document.text, forbidden
    assert "| `day` | `DATE` | yes |" in document.text
    assert bundle.manifest["counts"]["tools"] == 1
    assert "tools/index.md" in {d.path for d in bundle.documents}
    assert validate_atlas_publish_policy(bundle).valid
    # Every `atlas.tool` key the renderer emits is documented in the export profile.
    reference = (REPO_ROOT / "Docs" / "90-reference" / "okf-export-profile.md").read_text(
        encoding="utf-8"
    )
    undocumented = sorted(key for key in atlas["tool"] if f"`tool.{key}`" not in reference)
    assert undocumented == []
    assert f"`{TYPE_TOOL_VERSION}`" in reference


# --- the stored half ---------------------------------------------------------------------


async def test_a_read_publishes_once_and_later_reads_serve_the_stored_rows(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer reads a *stored* approved snapshot rather than re-freezing on every request."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    assert first.published_now
    assert first.publication.sequence == 1
    assert first.publication.trigger == TRIGGER_INITIAL
    stored = await _documents(session, first.publication)
    assert len(stored) == first.publication.document_count
    assert "log.md" in stored
    # The stored rows are exactly a full export of the stored snapshot and history.
    full = export_okf_bundle(
        snapshot_from_document(first.publication.snapshot), history=first.history
    )
    assert {d.path: d.sha256 for d in full.documents} == {
        path: row.sha256 for path, row in stored.items()
    }

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a fresh stored bundle must be served without freezing")

    monkeypatch.setattr(okf_store, "freeze_snapshot", refuse)
    second = await _read(session, settings, version)
    assert not second.published_now
    assert second.publication.id == first.publication.id
    assert await _count(session, OkfBundlePublication) == 1


async def test_okf_c_a_redefined_view_rebuilds_only_its_document_in_storage(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-C against the database, driven by an FP15 change signal.

    Export, redefine one view and record the signal the scan would have recorded, read again:
    one new publication, triggered by the source change, in which the view's document is the
    only content document whose hash moved. Every other document kept its hash and still names
    publication 1 as the one that rendered its bytes; the untouched source's log is unchanged.
    """
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    before = await _documents(session, first.publication)

    await _redefine_view(session, estate, "SELECT order_id, channel FROM sales.orders")
    second = await _read(session, settings, version)
    assert second.published_now
    assert second.publication.sequence == 2
    assert second.publication.trigger == TRIGGER_SOURCE_CHANGE
    after = await _documents(session, second.publication)

    view_path = _path_of(after, _warehouse_key(estate, "orders_v"))
    moved = {path for path in after if path in before and after[path].sha256 != before[path].sha256}
    content_moved = {path for path in moved if not is_log_path(path)}
    assert content_moved == {view_path}
    for path, row in after.items():
        if path in moved:
            assert row.rendered_in_sequence == 2
            continue
        assert row.sha256 == before[path].sha256, path
        assert row.rendered_in_sequence == 1, path
    reporting = estate["datasources"]["reporting"][0]
    assert f"sources/source-{source_key(str(reporting.id))}/log.md" not in moved
    summary = second.publication.change_summary
    assert [path for path in summary["changed"] if not is_log_path(path)] == [view_path]
    subject_rendered = {
        path for path in summary["rendered"] if after[path].subject_key is not None
    }
    assert subject_rendered == {view_path}
    assert second.publication.carried_count == len(after) - len(summary["rendered"])
    assert str(estate["tables"]["warehouse.orders_v"].id) in summary["marked_subjects"]
    assert "**Publication 2** (`SOURCE_CHANGE`): 1 changed" in after["log.md"].content


async def test_a_signal_that_changed_nothing_publishes_nothing(
    session: AsyncSession, settings: Settings
) -> None:
    """"No-op scans reproduce hashes": a mark with no content change re-freezes, finds the same
    snapshot, and writes no publication at all -- so not even the log moves."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    validated = first.head.validated_at
    await _signal(session, estate, "warehouse.orders", kind="TABLE")
    second = await _read(session, settings, version)
    assert not second.published_now
    assert second.publication.id == first.publication.id
    assert await _count(session, OkfBundlePublication) == 1
    assert second.head.validated_at >= validated
    # And the head now accounts for that mark: the next read is served without re-freezing.
    third = await _read(session, settings, version)
    assert third.publication.id == first.publication.id


async def test_revalidation_catches_a_change_that_left_no_mark(
    session: AsyncSession, settings: Settings
) -> None:
    """A column reclassified in place emits no FP15 signal. Once the head is older than the
    revalidation age it is re-frozen, and only the table whose column moved changes."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    before = await _documents(session, first.publication)
    column = await session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.table_id == estate["tables"]["warehouse.orders"].id,
            MetadataColumn.name == "channel",
        )
    )
    assert column is not None
    column.classification = "RESTRICTED"
    await session.flush()

    unchanged = await _read(session, settings, version)
    assert unchanged.publication.id == first.publication.id, "no mark, not yet aged"

    later = datetime.now(UTC) + okf_store.OKF_REVALIDATE_AFTER + timedelta(minutes=1)
    second = await _read(session, settings, version, now=later)
    assert second.published_now
    assert second.publication.trigger == TRIGGER_REVALIDATION
    after = await _documents(session, second.publication)
    orders_path = _path_of(after, _warehouse_key(estate, "orders"))
    moved = {
        path
        for path in after
        if not is_log_path(path) and path in before and after[path].sha256 != before[path].sha256
    }
    assert moved == {orders_path}
    assert "RESTRICTED" in after[orders_path].content


async def test_a_bundle_built_under_a_revoked_grant_is_not_served(
    session: AsyncSession, settings: Settings
) -> None:
    """Cache invalidation on *authorization* change, not only source change.

    The product reaches a datasource across a data-domain boundary. With an ACTIVE grant the
    stored bundle includes it; once the grant is revoked, the same reader's next request is
    admitted to less, computes a different lineage key, and is served a bundle without that
    source -- and asking for the old publication by id is refused as not found. The old rows
    still exist; nothing keyed without the caller's authority can reach them.
    """
    estate = await _estate(session)
    grant = CrossBoundaryGrant(
        id=uuid4(),
        organization_id=estate["organization"].id,
        source_data_domain_id=estate["far_domain"].id,
        target_data_domain_id=estate["domain"].id,
        reason="approved revenue-to-people crossing",
        status="ACTIVE",
        edge_kinds=[],
        requested_by="steward",
        approved_by="reviewer",
    )
    session.add(grant)
    await session.flush()
    _product_row, version = await _product(session, estate, include_far_source=True)
    granted = await _read(session, settings, version)
    assert granted.publication.manifest["counts"]["sources"] == 3
    granted_text = "\n".join(
        row.content for row in (await _documents(session, granted.publication)).values()
    )
    assert "salaries" in granted_text

    grant.status = "REVOKED"
    await session.flush()
    revoked = await _read(session, settings, version)
    assert revoked.authority_digest != granted.authority_digest
    assert revoked.publication.id != granted.publication.id
    assert revoked.publication.manifest["counts"]["sources"] == 2
    revoked_text = "\n".join(
        row.content for row in (await _documents(session, revoked.publication)).values()
    )
    assert "salaries" not in revoked_text
    assert "people" not in revoked_text
    assert "salaries" not in json.dumps(revoked.publication.manifest)
    with pytest.raises(HTTPException) as refused:
        await _read(session, settings, version, publication_id=granted.publication.id)
    assert refused.value.status_code == 404
    assert await session.get(OkfBundlePublication, granted.publication.id) is not None

    # Re-granting restores exactly the lineage the grant defines -- the key is the admission.
    grant.status = "ACTIVE"
    await session.flush()
    regranted = await _read(session, settings, version)
    assert regranted.authority_digest == granted.authority_digest
    assert regranted.publication.id == granted.publication.id


async def test_rest_and_mcp_are_served_the_same_publication_and_bytes(
    session: AsyncSession, settings: Settings
) -> None:
    """One snapshot, every door: the REST manifest, the REST document read and the MCP resource
    read of the same version name the same publication and return the same bytes."""
    estate = await _estate(session)
    product, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    manifest = await inspect_okf_bundle(version.id, context, session, settings)
    uri = f"atlas://context-products/{product.product_key}/versions/{version.version}/okf"
    listed = await _handle_resources_read({"uri": uri}, session, context, "corr", settings)
    payload = json.loads(listed["contents"][0]["text"])
    assert payload["publication"]["publication_id"] == str(manifest.publication.publication_id)
    assert payload["files"] == [entry.model_dump() for entry in manifest.files]

    path = next(entry.path for entry in manifest.files if "/views/" in entry.path)
    rest_document = await read_okf_document(version.id, path, context, session, settings)
    mcp_document = await _handle_resources_read(
        {"uri": f"{uri}/{path}"}, session, context, "corr", settings
    )
    content = mcp_document["contents"][0]
    assert content["mimeType"] == "text/markdown"
    assert content["text"] == rest_document.content
    assert content["_meta"]["sha256"] == rest_document.sha256
    assert content["_meta"]["publication_id"] == str(rest_document.publication_id)

    # A change rebuilt through one door is what the other door serves next.
    await _redefine_view(session, estate, "SELECT order_id FROM sales.orders WHERE 1 = 1")
    rebuilt = json.loads(
        (await _handle_resources_read({"uri": uri}, session, context, "corr", settings))[
            "contents"
        ][0]["text"]
    )
    assert rebuilt["publication"]["sequence"] == 2
    again = await inspect_okf_bundle(version.id, context, session, settings)
    assert str(again.publication.publication_id) == rebuilt["publication"]["publication_id"]
    assert again.publication.changes.changed == rebuilt["publication"]["changes"]["changed"]


async def test_mcp_refuses_what_the_rest_resolver_refuses(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    product, version = await _product(session, estate, include_far_source=False)
    foreign = replace(_context(estate["organization"].id), organization_id=uuid4())
    uri = f"atlas://context-products/{product.product_key}/versions/{version.version}/okf"
    result = await _handle_resources_read({"uri": uri}, session, foreign, "corr", settings)
    assert result["contents"][0]["text"] == "Resource not found or not accessible."
    assert await _count(session, OkfBundlePublication) == 0


_REST_DOORS = (
    "inspect_okf_bundle",
    "download_okf_bundle",
    "read_okf_document",
    "list_okf_publications",
    "read_object_okf_knowledge",
)
_RENDERING = frozenset(
    {"freeze_snapshot", "export_okf_bundle", "export_okf_bundle_incremental", "_load_source"}
)


@pytest.mark.parametrize(
    ("module", "handler"),
    [
        *(("aida.okf_export_api", name) for name in _REST_DOORS),
        ("aida.mcp_server", "_read_okf_resource"),
    ],
)
def test_every_okf_door_reads_the_one_store_and_renders_nothing_itself(
    module: str, handler: str
) -> None:
    """Structural, not coincidental: every reading surface reaches the store's read, and no
    handler freezes, resolves scope or renders on its own -- the second path through which two
    surfaces could hand one consumer different knowledge for the same product version."""
    assert reaches_call(
        module, handler, frozenset({"read_published_bundle", "read_object_knowledge"})
    )
    assert not references_name(module, handler, _RENDERING)


def test_only_the_store_freezes_or_renders_for_a_consumer() -> None:
    """No module in `src/` but the store (and the renderer/snapshot modules that define them)
    calls the freeze or the renderers. A new surface -- GraphQL is next -- has to come through
    `read_published_bundle` or this fails."""
    allowed = {"okf_export.py", "okf_snapshot.py", "okf_store.py"}
    offenders = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in ("freeze_snapshot(", "export_okf_bundle(", "export_okf_bundle_incremental("):
            if name in text and path.name not in allowed:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{name}")
    assert offenders == []


async def test_a_pinned_download_returns_the_bytes_its_manifest_described(
    session: AsyncSession, settings: Settings
) -> None:
    """The re-review pickup: manifest inspection and download must not recapture independently.
    A manifest names its publication; downloading that publication after a newer one was
    published returns the inspected bytes, and the unpinned download returns the newer ones."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    inspected = await inspect_okf_bundle(version.id, context, session, settings)
    await _redefine_view(session, estate, "SELECT 1 AS one FROM sales.orders")
    newer = await inspect_okf_bundle(version.id, context, session, settings)
    assert newer.publication.sequence == 2

    pinned = await download_okf_bundle(
        version.id, context, session, settings, publication_id=inspected.publication.publication_id
    )
    assert pinned.headers["X-Atlas-OKF-Publication-Id"] == str(inspected.publication.publication_id)
    assert pinned.headers["X-Atlas-Bundle-Content-SHA256"] == inspected.bundle_content_digest
    with zipfile.ZipFile(io.BytesIO(pinned.body)) as archive:
        manifest = json.loads(archive.read("atlas-manifest.json"))
        members = {name: archive.read(name) for name in archive.namelist()}
    assert manifest["bundle_content_digest"] == inspected.bundle_content_digest
    assert {entry.path for entry in inspected.files} == {
        name.removeprefix("bundle/") for name in members if name.startswith("bundle/")
    }
    current = await download_okf_bundle(version.id, context, session, settings)
    assert current.headers["X-Atlas-Bundle-Content-SHA256"] == newer.bundle_content_digest
    assert current.headers["X-Atlas-Bundle-Content-SHA256"] != inspected.bundle_content_digest

    history = await list_okf_publications(version.id, context, session, settings)
    assert [item.sequence for item in history.items] == [2, 1]
    assert [item.is_current for item in history.items] == [True, False]


async def test_a_publish_that_fails_part_way_leaves_the_previous_bundle_whole(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic publication. The failure is injected after the publication row, every document
    row and the head update have been flushed -- the worst moment -- and the savepoint takes
    all of it back: the head still names publication 1, whose document set is complete."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    first_count = first.publication.document_count

    async def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("storage failed after the head moved")

    monkeypatch.setattr(okf_store, "_prune", explode)
    await _redefine_view(session, estate, "SELECT 2 AS two FROM sales.orders")
    with pytest.raises(RuntimeError):
        await _read(session, settings, version)
    monkeypatch.undo()

    head = await session.scalar(select(OkfBundleHead))
    assert head is not None
    assert head.publication_id == first.publication.id
    assert await _count(session, OkfBundlePublication) == 1
    assert await _count(session, OkfBundleDocument) == first_count
    # And the next read publishes normally.
    second = await _read(session, settings, version)
    assert second.publication.sequence == 2


async def test_a_concurrent_publisher_that_wins_is_served_not_failed(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two readers rebuild the same lineage at once. The one whose insert loses the unique
    (version, authority, sequence) race rolls back its savepoint and serves the winner."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    real = okf_store.export_okf_bundle_incremental
    winner_id = uuid4()

    def race(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        # The other reader got there first: same lineage, same next sequence, head moved.
        winner = OkfBundlePublication(
            id=winner_id,
            organization_id=first.publication.organization_id,
            context_product_version_id=first.publication.context_product_version_id,
            authority_digest=first.publication.authority_digest,
            sequence=2,
            trigger=TRIGGER_SOURCE_CHANGE,
            captured_at=datetime.now(UTC),
            snapshot=first.publication.snapshot,
            content_snapshot_digest=first.publication.content_snapshot_digest,
            bundle_content_digest=first.publication.bundle_content_digest,
            scope_digest=first.publication.scope_digest,
            manifest=first.publication.manifest,
            document_count=0,
            rendered_count=0,
            carried_count=0,
            change_summary={},
            history=[],
            built_by="other-reader",
        )
        session.add(winner)
        head = next(
            item for item in session.identity_map.values() if isinstance(item, OkfBundleHead)
        )
        head.publication_id = winner_id
        return result

    monkeypatch.setattr(okf_store, "export_okf_bundle_incremental", race)
    await _redefine_view(session, estate, "SELECT 3 AS three FROM sales.orders")
    await session.flush()
    served = await _read(session, settings, version)
    assert not served.published_now
    assert served.publication.id == winner_id
    # Ours was rolled back with its savepoint; the winner's and the first survive.
    assert await _count(session, OkfBundlePublication) == 2
    assert await _count(session, OkfBundleDocument) == first.publication.document_count


async def test_a_capture_that_races_a_source_change_is_refused_not_published(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-consistent capture: a change mark that commits while the snapshot is being read
    means the snapshot may mix two catalog states, so nothing is published and the read is
    refused with a retryable 409. The next request captures a consistent state."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    real = okf_store.freeze_snapshot

    async def racing(*args: Any, **kwargs: Any) -> Any:
        snapshot = await real(*args, **kwargs)
        await _signal(session, estate, "warehouse.orders", kind="TABLE")
        return snapshot

    monkeypatch.setattr(okf_store, "freeze_snapshot", racing)
    with pytest.raises(HTTPException) as refused:
        await _read(session, settings, version)
    assert refused.value.status_code == 409
    assert "changed while the OKF snapshot was being captured" in str(refused.value.detail)
    assert await _count(session, OkfBundlePublication) == 0
    monkeypatch.undo()
    assert (await _read(session, settings, version)).publication.sequence == 1


async def test_an_oversized_scope_is_refused_before_anything_is_frozen(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Early refusal: the cardinality bound is checked from the version's own pins, and the
    freeze -- which is what would materialize every column -- is never reached."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an oversized scope must be refused before the freeze")

    monkeypatch.setattr(okf_store, "freeze_snapshot", refuse)
    monkeypatch.setattr(okf_store, "MAX_SCOPE_SUBJECTS", 3)
    with pytest.raises(HTTPException) as refused:
        await _read(session, settings, version)
    assert refused.value.status_code == 409
    assert "Refused before any document was built" in str(refused.value.detail)
    monkeypatch.setattr(okf_store, "MAX_SCOPE_SUBJECTS", 10_000)
    monkeypatch.setattr(okf_store, "MAX_SCOPE_COLUMNS", 1)
    with pytest.raises(HTTPException) as columns:
        await _read(session, settings, version)
    assert "Refused before any column was loaded" in str(columns.value.detail)


async def test_retention_keeps_the_newest_publications_and_never_the_head(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    description = await session.scalar(
        select(AssetDocumentationVersion).where(
            AssetDocumentationVersion.organization_id == estate["organization"].id
        )
    )
    assert description is not None
    for round_ in range(RETAINED_PUBLICATIONS + 1):
        description.readme = f"One row per completed order, revision {round_}."
        await session.flush()
        await _read(session, settings, version)
    sequences = sorted(
        (await session.scalars(select(OkfBundlePublication.sequence))).all()
    )
    last = RETAINED_PUBLICATIONS + 2
    assert sequences == list(range(last - RETAINED_PUBLICATIONS + 1, last + 1))
    head = await session.scalar(select(OkfBundleHead))
    assert head is not None
    current = await session.get(OkfBundlePublication, head.publication_id)
    assert current is not None and current.sequence == last
    documents = await session.scalars(select(OkfBundleDocument.publication_id).distinct())
    assert set(documents.all()) == set(
        (await session.scalars(select(OkfBundlePublication.id))).all()
    )
    with pytest.raises(HTTPException) as pruned:
        await _read(session, settings, version, publication_id=first.publication.id)
    assert pruned.value.status_code == 404
    # The log still carries the pruned publications: history outlives retention.
    assert "**Publication 1** (`INITIAL`)" in (
        await _documents(session, current)
    )["log.md"].content


async def test_the_catalog_door_returns_an_objects_document_only_where_admitted(
    session: AsyncSession, settings: Settings
) -> None:
    """The Catalog knowledge view: the object's own document from each product bundle the
    caller may read. A table across an ungranted domain boundary has no such document -- the
    bundle does not admit its source -- so the view returns nothing, not a placeholder."""
    estate = await _estate(session)
    product, _version = await _product(session, estate, include_far_source=True)
    context = _context(estate["organization"].id)
    orders = estate["tables"]["warehouse.orders"]
    read = await read_object_okf_knowledge(orders.id, context, session, settings)
    assert len(read.items) == 1
    item = read.items[0]
    assert item.product_key == product.product_key
    assert "bank.sales.orders" in item.document.content
    assert item.coverage["description_state"] == "APPROVED"
    assert item.coverage["key"] == item.document.subject_key
    salaries = estate["tables"]["people.salaries"]
    refused = await read_object_okf_knowledge(salaries.id, context, session, settings)
    assert refused.items == []


async def test_tool_versions_are_admitted_like_their_datasource_and_never_leak_code(
    session: AsyncSession, settings: Settings
) -> None:
    """A tool over an admitted source gets a document; a tool over a refused source is absent
    from the documents, the counts and the manifest's pin list (OKF-D)."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    near, far = await _tools(session, estate)
    version.eligible_tool_version_ids = [str(near.id), str(far.id), "not-a-tool"]
    await session.flush()
    stored = await _read(session, settings, version)
    documents = await _documents(session, stored.publication)
    tool_docs = [path for path in documents if path.startswith("tools/tool-version-")]
    assert len(tool_docs) == 1
    text = documents[tool_docs[0]].content
    assert "Revenue by day" in text
    assert "atlas__revenue_by_day" in text
    assert stored.publication.manifest["counts"]["tools"] == 1
    assert stored.publication.manifest["scope"]["eligible_tool_version_ids"] == [str(near.id)]
    everything = json.dumps(stored.publication.manifest) + "".join(
        row.content for row in documents.values()
    )
    assert "Salary bands" not in everything
    assert str(far.id) not in everything


async def _tools(
    session: AsyncSession, estate: dict[str, Any]
) -> tuple[GovernedToolVersion, GovernedToolVersion]:
    made: list[GovernedToolVersion] = []
    for source, slug, name in (
        ("warehouse", "revenue_by_day", "Revenue by day"),
        ("people", "salary_bands", "Salary bands"),
    ):
        tool = GovernedTool(
            id=uuid4(),
            organization_id=estate["organization"].id,
            project_id=estate["project"].id,
            slug=slug,
        )
        session.add(tool)
        await session.flush()
        tool_version = GovernedToolVersion(
            id=uuid4(),
            organization_id=estate["organization"].id,
            tool_id=tool.id,
            version=1,
            status="PUBLISHED",
            name=name,
            description=f"{name} for one day.",
            datasource_id=estate["datasources"][source][0].id,
            sql_template=f"SELECT amount FROM t WHERE code = '{SENTINEL_TOOL_SQL}'",  # noqa: S608
            referenced_tables=[],
            parameter_schema=[
                {
                    "name": "day",
                    "parameter_type": "DATE",
                    "required": True,
                    "default": SENTINEL_TOOL_DEFAULT,
                    "allowed_values": [SENTINEL_TOOL_DEFAULT],
                }
            ],
            allowed_roles=["Analyst"],
            fingerprint="t" * 64,
            created_by="maker",
            approved_by="reviewer",
            approved_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
        session.add(tool_version)
        made.append(tool_version)
    await session.flush()
    return made[0], made[1]


async def test_no_sentinel_reaches_any_row_the_storage_path_writes(
    session: AsyncSession, settings: Settings
) -> None:
    """INV-6, driven through the storage path itself.

    Sentinels sit in a routine body, a view definition, column and parameter defaults, a source
    comment, a connector's free-text reason, a tool's SQL template and its parameter defaults.
    The bundle is published, a view is then redefined *with a new sentinel in its text* and the
    bundle rebuilt through the REST routes, and every row of every table the path wrote -- the
    stored snapshot, each document, the head, and the audit, outbox and consumption evidence --
    is scanned. The first assertion makes sure the scan is looking at a real rebuild.
    """
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    near, _far = await _tools(session, estate)
    version.eligible_tool_version_ids = [str(near.id)]
    await session.flush()
    context = _context(estate["organization"].id)
    await inspect_okf_bundle(version.id, context, session, settings)
    await _redefine_view(
        session,
        estate,
        f"SELECT order_id FROM sales.orders /* {SENTINEL_REDEFINED} */",  # noqa: S608
    )
    rebuilt = await inspect_okf_bundle(version.id, context, session, settings)
    assert rebuilt.publication.sequence == 2
    await download_okf_bundle(version.id, context, session, settings)

    scanned = 0
    for model in (
        OkfBundlePublication,
        OkfBundleDocument,
        OkfBundleHead,
        AuditEvent,
        OutboxEvent,
        ContextProductConsumptionEdge,
    ):
        for row in (await session.scalars(select(model))).all():
            scanned += 1
            for value in _persisted_values(row):
                for sentinel in _ALL_SENTINELS:
                    assert sentinel not in value, (model.__tablename__, sentinel)
    assert scanned > 50


async def test_a_superseded_version_of_the_renderer_republishes_in_the_same_lineage(
    session: AsyncSession, settings: Settings
) -> None:
    """A stored bundle rendered by an older profile is re-rendered in full, as the next
    publication of the same lineage, rather than served with bytes the current renderer would
    not produce."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _read(session, settings, version)
    old = dict(first.publication.snapshot)
    old["profile_version"] = "1"
    first.publication.snapshot = old
    await session.flush()
    second = await _read(session, settings, version)
    assert second.publication.sequence == 2
    assert second.publication.trigger == "RENDERER_CHANGE"
    assert second.publication.change_summary["full_render"] is True
