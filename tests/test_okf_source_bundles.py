"""R11-OKF01/OKF02 (design item 14): source-scoped OKF bundles through the one store.

Design section 14: "Source bundles are scoped exports of discovered, authorized objects. Product
bundles contain only the selected approved references and permitted dependencies." Product
bundles have been stored, incrementally rebuilt and authority-keyed since R11-OKF02; this module
proves a *source* bundle -- one datasource's discovered objects -- is served by the same rules
rather than a copy of them:

* **One path.** A source bundle is frozen from the datasource's ACTIVE objects with the context
  compiler's own resolvers, rendered by the same renderer, stored in the same tables, and read
  through `read_published_source_bundle`, which hands the shared `_serve` its scope and nothing
  else. No business concept or tool is part of it, and the renderer refuses a source snapshot
  that carries one.
* **Authority-keyed (OKF-D).** The datasource's `READ_METADATA` decision is taken on every read,
  and per schema where a workspace decides. A schema a policy refuses is absent from the text,
  the links and every count; a cross-datasource dependency is never linked; a revoked binding or
  a workspace refusal removes the bundle -- the current read and a pinned read alike -- and a
  restored grant returns the very same lineage.
* **OKF-C, consistency and bounds.** A redefined view rebuilds only its document; a discovered or
  retired table moves only the documents that list it; a no-op signal publishes nothing; a
  capture racing a change is refused; an oversized source is refused before the freeze.
* **Doors.** Manifest, document, download, publications and question context over REST, each
  reaching the one store read and rendering nothing itself; the product bundle's bytes do not
  move because a second scope exists.
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
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.okf_store as okf_store
from aida.change_signal_models import MetadataChangeSignal
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataViewDefinition
from aida.models import (
    AccessPolicy,
    AuditEvent,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    OutboxEvent,
    SourceBinding,
    ViewLineageEdge,
)
from aida.okf_export import (
    SCOPE_DATASOURCE,
    TRIGGER_INITIAL,
    TRIGGER_SOURCE_CHANGE,
    OkfExportError,
    OkfSourceFacts,
    export_okf_bundle,
    is_log_path,
    object_key,
    snapshot_from_document,
    snapshot_to_document,
    source_key,
    validate_atlas_publish_policy,
    validate_okf_conformance,
)
from aida.okf_export_api import (
    download_source_okf_bundle,
    inspect_okf_bundle,
    inspect_source_okf_bundle,
    list_source_okf_publications,
    read_source_okf_document,
    select_source_okf_context,
)
from aida.okf_store import (
    OkfPublishedSourceBundle,
    load_documents,
    read_published_source_bundle,
)
from aida.okf_store_models import OkfBundleDocument, OkfBundleHead, OkfBundlePublication
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.schemas import OkfContextRequest
from aida.workspace_service import approve_binding, create_workspace, request_binding
from tests.support.app_surface import reaches_call, references_name
from tests.test_inv6_value_freedom import _persisted_values
from tests.test_okf_export import _SENTINELS, _context, _estate, _product, _snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Planted in the name of a table only a refused schema holds. The name is catalog text, so it
#: is exactly what a leak through a label, a link or an index line would carry.
HIDDEN_TABLE = "zzq_hidden_payroll"
HIDDEN_SCHEMA = "hr_private"


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


def _warehouse(estate: dict[str, Any]) -> Any:
    return estate["datasources"]["warehouse"][0]


async def _read(
    session: AsyncSession,
    settings: Settings,
    estate: dict[str, Any],
    *,
    now: datetime | None = None,
    publication_id: UUID | None = None,
) -> OkfPublishedSourceBundle:
    return await read_published_source_bundle(
        session,
        _warehouse(estate).id,
        _context(estate["organization"].id),
        settings,
        now=now,
        publication_id=publication_id,
    )


async def _documents(
    session: AsyncSession, publication: OkfBundlePublication
) -> dict[str, OkfBundleDocument]:
    return {row.path: row for row in await load_documents(session, publication)}


def _everything(stored: OkfPublishedSourceBundle, documents: dict[str, OkfBundleDocument]) -> str:
    """Every byte a reader of this publication could see: documents, manifest and the stored
    snapshot the documents were rendered from."""
    return (
        "\n".join(row.content for row in documents.values())
        + json.dumps(stored.publication.manifest)
        + json.dumps(stored.publication.snapshot)
    )


def _key(estate: dict[str, Any], table: str) -> str:
    datasource, catalog, schema = estate["datasources"]["warehouse"]
    return object_key(str(datasource.id), catalog.name, schema.name, table)


async def _signal(session: AsyncSession, estate: dict[str, Any], table: str) -> None:
    subject = estate["tables"][table]
    session.add(
        MetadataChangeSignal(
            organization_id=estate["organization"].id,
            datasource_id=subject.datasource_id,
            subject_kind="VIEW",
            subject_id=subject.id,
            signal_type="DEFINITION_CHANGED",
            change_class="STRUCTURAL",
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


async def _hidden_schema(session: AsyncSession, estate: dict[str, Any]) -> MetadataTable:
    """A second schema in the warehouse holding one table, wired into the visible schema's
    lineage from both directions: the visible view reads it and a visible routine writes it.
    A reader refused the schema must see neither end of either edge."""
    datasource, catalog, _schema = estate["datasources"]["warehouse"]
    organization_id = estate["organization"].id
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization_id,
        catalog_id=catalog.id,
        name=HIDDEN_SCHEMA,
        fingerprint="f",
    )
    session.add(schema)
    await session.flush()
    hidden = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=HIDDEN_TABLE,
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="f",
    )
    session.add(hidden)
    await session.flush()
    session.add(
        MetadataColumn(
            id=uuid4(),
            organization_id=organization_id,
            table_id=hidden.id,
            name="zzq_hidden_salary",
            ordinal_position=1,
            physical_type="numeric",
            nullable=True,
            classification="RESTRICTED",
            status="ACTIVE",
            fingerprint="f",
        )
    )
    session.add(
        ViewLineageEdge(
            id=uuid4(),
            organization_id=organization_id,
            datasource_id=datasource.id,
            source_table=f"{HIDDEN_SCHEMA}.{HIDDEN_TABLE}",
            source_column="zzq_hidden_salary",
            target_table="sales.orders_v",
            target_column="order_id",
            source_table_id=hidden.id,
            target_table_id=estate["tables"]["warehouse.orders_v"].id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h",
            review_status="ACTIVE",
            created_by="parser",
        )
    )
    writer = estate["routines"]["warehouse.rebuild_totals(integer)"]
    # One write into the hidden schema, and one into another datasource altogether: a source
    # bundle links neither, the first because the reader was refused it and the second because
    # it is outside the source whatever the reader may see.
    for ordinal, target in enumerate((hidden, estate["tables"]["people.salaries"]), start=1):
        session.add(
            DeepProcedureLineageEdge(
                id=uuid4(),
                organization_id=organization_id,
                datasource_id=datasource.id,
                routine_id=writer.id,
                statement_ordinal=ordinal,
                source_table="sales.orders",
                source_column="order_id",
                target_table=f"x.{target.name}",
                target_column="amount",
                source_table_id=estate["tables"]["warehouse.orders"].id,
                target_table_id=target.id,
                transformation_type="DIRECT",
                confidence="FULL",
                dialect="postgres",
                is_write=True,
                sql_hash="h",
                review_status="ACTIVE",
            )
        )
    await session.flush()
    return hidden


async def _enforcing_workspace(session: AsyncSession, estate: dict[str, Any]) -> SourceBinding:
    """An ENFORCE workspace the warehouse is bound to, with the reader as its owner.

    ENFORCE because a SHADOW workspace turns every denial into an allow by design, and a
    refusal test run in shadow mode would assert nothing.
    """
    organization_id = estate["organization"].id
    workspace = await create_workspace(
        session,
        organization_id=organization_id,
        name="Warehouse",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="p",
        owner_principal="steward",
    )
    workspace.authorization_mode = "ENFORCE"
    binding = await request_binding(
        session,
        organization_id=organization_id,
        workspace_id=workspace.id,
        datasource_id=_warehouse(estate).id,
        purpose="p",
        requested_by="steward",
    )
    await approve_binding(session, binding, approver_principal="reviewer")
    await session.flush()
    return binding


# --- the renderer's half ----------------------------------------------------------------


def _source_snapshot() -> Any:
    """The OKF01 fixture -- one source -- recast as that source's bundle: no product, no
    concepts, no tools."""
    base = _snapshot()
    datasource_id = "11111111-1111-1111-1111-111111111111"
    assert [source.key for source in base.sources] == [source_key(datasource_id)]
    return replace(
        base,
        scope=replace(
            base.scope,
            kind=SCOPE_DATASOURCE,
            policy_partition=replace(
                base.scope.policy_partition,
                allowed_consumer_roles=(),
                classifications=(),
                purpose=None,
            ),
            product_key=None,
            product_version=None,
            product_version_id=None,
            product_fingerprint=None,
            product_name=None,
            product_purpose=None,
            eligible_tool_version_ids=(),
            datasource_id=datasource_id,
        ),
        concepts=(),
        tools=(),
    )


def test_a_source_snapshot_holds_its_one_source_and_no_concept_or_tool() -> None:
    """Structural, not trusted to the freeze: a DATASOURCE snapshot naming a second source, a
    concept or a tool is a refused export -- each would be a count or a link reaching past the
    one datasource whose read decision admitted the reader."""
    snapshot = _source_snapshot()
    bundle = export_okf_bundle(snapshot)
    assert validate_atlas_publish_policy(bundle).valid
    assert validate_okf_conformance({d.path: d.text for d in bundle.documents}).valid
    base = _snapshot()
    stranger = OkfSourceFacts(
        key=source_key("99999999-9999-9999-9999-999999999999"),
        name="elsewhere",
        dialect="postgres",
        connector_type="postgres",
        environment="PROD",
        lifecycle="ACTIVE",
    )
    for refused in (
        replace(snapshot, sources=(*snapshot.sources, stranger)),
        replace(snapshot, concepts=base.concepts[:1]),
        replace(snapshot, scope=replace(snapshot.scope, datasource_id=None)),
    ):
        with pytest.raises(OkfExportError):
            export_okf_bundle(refused)


def test_a_source_bundle_says_what_it_is_and_what_it_is_not() -> None:
    """The root index names the scope and sends a reader after meaning to a context product;
    the manifest and every document's scope extension name the source, not an empty product."""
    snapshot = _source_snapshot()
    bundle = export_okf_bundle(snapshot)
    root = bundle.document("index.md").text
    assert "Scope: DATASOURCE." in root
    assert "Business concepts and approved tool versions are not part of a source bundle" in root
    assert "Context product" not in root
    assert bundle.manifest["scope"] == {
        "kind": SCOPE_DATASOURCE,
        "organization_id": snapshot.scope.organization_id,
        "datasource_id": snapshot.scope.datasource_id,
        "source_key": snapshot.sources[0].key,
    }
    assert "concepts/index.md" not in {document.path for document in bundle.documents}
    for document in bundle.documents:
        if "/tables/" not in document.path:
            continue
        atlas = yaml.safe_load(document.text.split("---\n")[1])["atlas"]
        assert atlas["scope"]["kind"] == SCOPE_DATASOURCE
        assert atlas["scope"]["source_key"] == snapshot.sources[0].key
        assert "product_key" not in atlas["scope"]
    # The round trip keeps the datasource, so a stored source snapshot re-renders the same bytes.
    again = snapshot_from_document(snapshot_to_document(snapshot))
    assert again == snapshot
    assert export_okf_bundle(again).content_digest == bundle.content_digest


def test_a_product_snapshot_reads_exactly_as_before_source_bundles() -> None:
    """Why the export profile was not bumped: the source scope's one new field is omitted from
    a product snapshot's written form, and the manifest and scope extension keep their shape --
    so a product's `content_snapshot_digest`, `scope_digest` and document bytes are what the
    renderer produced before a second scope existed."""
    snapshot = _snapshot()
    assert "datasource_id" not in snapshot_to_document(snapshot)["scope"]
    bundle = export_okf_bundle(snapshot)
    assert set(bundle.manifest["scope"]) == {
        "kind",
        "organization_id",
        "product_key",
        "product_version",
        "product_version_id",
        "product_fingerprint",
        "eligible_tool_version_ids",
    }
    table = next(d for d in bundle.documents if "/tables/" in d.path)
    atlas = yaml.safe_load(table.text.split("---\n")[1])["atlas"]
    assert set(atlas["scope"]) == {
        "kind",
        "product_key",
        "product_version",
        "policy_partition_digest",
    }


# --- the stored half --------------------------------------------------------------------


async def test_a_source_bundle_is_the_datasources_discovered_objects_through_one_path(
    session: AsyncSession, settings: Settings
) -> None:
    """One datasource's ACTIVE tables, views, routines and packages, stored and served like a
    product bundle -- and nothing of any other datasource, even one the reader may read. A
    retired table is not a discovered object and is absent; the approved description and the
    definition coverage are the product bundle's own facts, by the same code."""
    estate = await _estate(session)
    datasource, catalog, schema = estate["datasources"]["warehouse"]
    retired = MetadataTable(
        id=uuid4(),
        organization_id=estate["organization"].id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="zzq_retired_orders",
        object_type="BASE_TABLE",
        status="DEPRECATED",
        fingerprint="f",
    )
    session.add(retired)
    await session.flush()

    first = await _read(session, settings, estate)
    assert first.published_now
    assert first.publication.sequence == 1
    assert first.publication.trigger == TRIGGER_INITIAL
    assert first.publication.datasource_id == datasource.id
    assert first.publication.context_product_version_id is None
    documents = await _documents(session, first.publication)
    counts = first.publication.manifest["counts"]
    assert counts["sources"] == 1
    assert (counts["tables"], counts["views"], counts["routines"]) == (1, 1, 3)
    assert (counts["concepts"], counts["tools"]) == (0, 0)
    assert len([path for path in documents if "/packages/" in path]) == 1
    assert not any(path.startswith(("concepts/", "tools/")) for path in documents)
    assert "log.md" in documents

    text = _everything(first, documents)
    assert "One row per completed order across all channels." in text
    assert "Globally unique order identifier." in text
    assert "zzq_retired_orders" not in text
    for other in ("revenue_daily", "revenue_mv", "salaries", "reporting", "people"):
        assert other not in "\n".join(row.content for row in documents.values()), other
    for sentinel in _SENTINELS:
        assert sentinel not in text, sentinel

    # The stored rows are exactly a full export of the stored snapshot and history.
    full = export_okf_bundle(
        snapshot_from_document(first.publication.snapshot), history=first.history
    )
    assert {d.path: d.sha256 for d in full.documents} == {
        path: row.sha256 for path, row in documents.items()
    }
    bundle_texts = {path: row.content for path, row in documents.items()}
    assert validate_okf_conformance(bundle_texts).valid
    assert first.validation.valid, first.validation.findings

    # Served from storage afterwards, not re-frozen.
    second = await _read(session, settings, estate)
    assert not second.published_now
    assert second.publication.id == first.publication.id
    assert await _count(session, OkfBundlePublication) == 1


async def test_okf_d_a_refused_schema_is_absent_from_text_links_and_counts(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-D inside one source. The reader's workspace refuses `READ_METADATA` on one schema by
    pattern; its table is read by a visible view and written by a visible routine. None of it --
    the table, its column, the edges' far ends, the schema -- reaches a document, a link, an
    index line, a count, the manifest or the stored snapshot. The same routine also writes a
    table in another datasource, which a source bundle never links whatever the reader may see.

    Then the policy is removed: the next read computes a different lineage and the schema, its
    table and the routine's link to it all appear -- proving the absence was the decision, not
    the loader.
    """
    estate = await _estate(session)
    hidden = await _hidden_schema(session, estate)
    await _enforcing_workspace(session, estate)
    deny = AccessPolicy(
        organization_id=estate["organization"].id,
        code="no-hr-metadata",
        name="No HR schemas",
        effect="DENY",
        priority=1000,
        resource_match={"schema_pattern": "hr_*"},
        action_match=["READ_METADATA"],
        created_by="seed",
    )
    session.add(deny)
    await session.flush()

    refused = await _read(session, settings, estate)
    assert refused.admission.decided
    documents = await _documents(session, refused.publication)
    text = _everything(refused, documents)
    for leaked in (HIDDEN_TABLE, HIDDEN_SCHEMA, "zzq_hidden_salary", str(hidden.id), "salaries"):
        assert leaked not in text, leaked
    hidden_key = object_key(
        str(hidden.datasource_id), estate["datasources"]["warehouse"][1].name, HIDDEN_SCHEMA,
        HIDDEN_TABLE,
    )
    assert hidden_key not in text
    counts = refused.publication.manifest["counts"]
    assert (counts["schemas"], counts["tables"], counts["views"]) == (1, 1, 1)
    assert "Objects in scope: 2 " in documents["index.md"].content
    assert "Schemas in scope: 1." in documents["index.md"].content
    assert refused.validation.valid

    await session.execute(delete(AccessPolicy).where(AccessPolicy.id == deny.id))
    await session.flush()
    admitted = await _read(session, settings, estate)
    assert admitted.authority_digest != refused.authority_digest
    assert admitted.publication.id != refused.publication.id
    visible = _everything(admitted, await _documents(session, admitted.publication))
    assert HIDDEN_TABLE in visible
    assert admitted.publication.manifest["counts"]["schemas"] == 2
    routine_doc = next(
        row.content
        for row in (await _documents(session, admitted.publication)).values()
        if "rebuild_totals" in row.content and "- writes" in row.content
    )
    assert HIDDEN_TABLE in routine_doc
    # The other datasource's table stays out even for a reader who may see all of this one.
    assert "salaries" not in visible.replace("zzq_hidden_salary", "")


async def test_a_revoked_binding_or_a_workspace_refusal_removes_the_source_bundle(
    session: AsyncSession
) -> None:
    """Authorization change, not only source change. Under the DENY posture the reader is
    admitted through their workspace's binding; revoking the binding refuses the read -- and a
    pinned read of the publication built under it -- with the gate's reason code, while the
    stored rows remain and nothing new is published. Restoring the binding returns the same
    lineage and the same publication. A workspace policy refusing the datasource does the same.
    """
    settings = Settings(_env_file=None, unresolved_workspace_posture="DENY")
    estate = await _estate(session)
    binding = await _enforcing_workspace(session, estate)
    granted = await _read(session, settings, estate)
    assert granted.admission.decided
    publications = await _count(session, OkfBundlePublication)

    binding.status = "REVOKED"
    await session.flush()
    for pinned in (None, granted.publication.id):
        with pytest.raises(HTTPException) as refused:
            await _read(session, settings, estate, publication_id=pinned)
        assert refused.value.status_code == 403
        assert refused.value.detail == "NO_BINDING_FOR_DATASOURCE"
    with pytest.raises(HTTPException):
        await inspect_source_okf_bundle(
            _warehouse(estate).id, _context(estate["organization"].id), session, settings
        )
    assert await session.get(OkfBundlePublication, granted.publication.id) is not None
    assert await _count(session, OkfBundlePublication) == publications

    binding.status = "ACTIVE"
    await session.flush()
    restored = await _read(session, settings, estate)
    assert restored.authority_digest == granted.authority_digest
    assert restored.publication.id == granted.publication.id
    assert not restored.published_now

    session.add(
        AccessPolicy(
            organization_id=estate["organization"].id,
            code="no-warehouse-metadata",
            name="No warehouse metadata",
            effect="DENY",
            priority=1000,
            resource_match={"datasource_ids": [str(_warehouse(estate).id)]},
            action_match=["READ_METADATA"],
            created_by="seed",
        )
    )
    await session.flush()
    with pytest.raises(HTTPException) as denied:
        await _read(session, settings, estate, publication_id=granted.publication.id)
    assert denied.value.status_code == 403
    assert denied.value.detail == "DENIED_BY_POLICY"


async def test_a_reader_never_admitted_publishes_nothing(session: AsyncSession) -> None:
    """An unbound datasource under the DENY posture: refused before any lookup, so no lineage,
    no row and no count exists for that reader -- and a product bundle's publication cannot be
    reached through the source door by id either."""
    settings = Settings(_env_file=None, unresolved_workspace_posture="DENY")
    estate = await _estate(session)
    with pytest.raises(HTTPException) as refused:
        await _read(session, settings, estate)
    assert refused.value.status_code == 403
    assert await _count(session, OkfBundlePublication) == 0

    open_settings = Settings(_env_file=None)
    _product_row, version = await _product(session, estate, include_far_source=False)
    product = await inspect_okf_bundle(
        version.id, _context(estate["organization"].id), session, open_settings
    )
    with pytest.raises(HTTPException) as foreign:
        await _read(
            session, open_settings, estate, publication_id=product.publication.publication_id
        )
    assert foreign.value.status_code == 404


async def test_okf_c_a_redefined_view_rebuilds_only_its_document_in_the_source_bundle(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-C on the source scope, driven by the FP15 signal a scan records.

    The view's document is the only content document whose hash moves; every other document
    keeps its hash and still names publication 1 as the one that rendered it. Then a signal that
    changed nothing publishes nothing at all, not even a log entry.
    """
    estate = await _estate(session)
    first = await _read(session, settings, estate)
    before = await _documents(session, first.publication)

    await _redefine_view(session, estate, "SELECT order_id, channel FROM sales.orders")
    second = await _read(session, settings, estate)
    assert second.published_now
    assert (second.publication.sequence, second.publication.trigger) == (
        2,
        TRIGGER_SOURCE_CHANGE,
    )
    after = await _documents(session, second.publication)
    view_path = next(
        path for path, row in after.items() if row.subject_key == _key(estate, "orders_v")
    )
    moved = {path for path in after if path in before and after[path].sha256 != before[path].sha256}
    assert {path for path in moved if not is_log_path(path)} == {view_path}
    for path, row in after.items():
        if path in moved:
            assert row.rendered_in_sequence == 2
        else:
            assert row.sha256 == before[path].sha256, path
            assert row.rendered_in_sequence == 1, path
    summary = second.publication.change_summary
    assert {path for path in summary["rendered"] if after[path].subject_key} == {view_path}
    assert str(estate["tables"]["warehouse.orders_v"].id) in summary["marked_subjects"]

    await _signal(session, estate, "warehouse.orders_v")
    third = await _read(session, settings, estate)
    assert not third.published_now
    assert third.publication.id == second.publication.id
    assert await _count(session, OkfBundlePublication) == 2


async def test_a_discovered_table_moves_only_the_documents_that_list_it(
    session: AsyncSession, settings: Settings
) -> None:
    """A source's membership is content. A table the next scan discovers leaves no FP15 signal
    of its own, but its row is a change mark on the source scope, so the next read publishes:
    the new table's document is added, the indexes that count or list it change, and every
    other object's document is carried with its old hash."""
    estate = await _estate(session)
    first = await _read(session, settings, estate)
    before = await _documents(session, first.publication)
    datasource, _catalog, schema = estate["datasources"]["warehouse"]
    session.add(
        MetadataTable(
            id=uuid4(),
            organization_id=estate["organization"].id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name="refunds",
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint="f",
        )
    )
    await session.flush()
    second = await _read(session, settings, estate)
    assert second.publication.trigger == TRIGGER_SOURCE_CHANGE
    after = await _documents(session, second.publication)
    refunds = next(
        path for path, row in after.items() if row.subject_key == _key(estate, "refunds")
    )
    added = {path for path in after if path not in before and not is_log_path(path)}
    assert added == {refunds}
    changed = {
        path
        for path in after
        if path in before and after[path].sha256 != before[path].sha256 and not is_log_path(path)
    }
    assert all(path.endswith("index.md") for path in changed), changed
    for path, row in after.items():
        if row.subject_key and path != refunds:
            assert row.sha256 == before[path].sha256, path
    assert second.publication.manifest["counts"]["tables"] == 2


async def test_a_capture_that_races_a_source_change_is_refused_not_published(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-consistent capture on the source scope: a mark committed while the snapshot is read
    refuses the publication with a retryable 409; the next read captures a consistent state."""
    estate = await _estate(session)
    real = okf_store.freeze_source_snapshot

    async def racing(*args: Any, **kwargs: Any) -> Any:
        snapshot = await real(*args, **kwargs)
        await _signal(session, estate, "warehouse.orders")
        return snapshot

    monkeypatch.setattr(okf_store, "freeze_source_snapshot", racing)
    with pytest.raises(HTTPException) as refused:
        await _read(session, settings, estate)
    assert refused.value.status_code == 409
    assert "changed while the OKF snapshot was being captured" in str(refused.value.detail)
    assert await _count(session, OkfBundlePublication) == 0
    monkeypatch.undo()
    assert (await _read(session, settings, estate)).publication.sequence == 1


async def test_an_oversized_source_is_refused_before_anything_is_frozen(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Early refusal: the source's objects and columns are counted, not loaded, and the freeze
    is never reached when either is over its bound."""
    estate = await _estate(session)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an oversized source must be refused before the freeze")

    monkeypatch.setattr(okf_store, "freeze_source_snapshot", refuse)
    monkeypatch.setattr(okf_store, "MAX_SCOPE_SUBJECTS", 2)
    with pytest.raises(HTTPException) as subjects:
        await _read(session, settings, estate)
    assert subjects.value.status_code == 409
    assert "Refused before any document was built" in str(subjects.value.detail)
    monkeypatch.setattr(okf_store, "MAX_SCOPE_SUBJECTS", 10_000)
    monkeypatch.setattr(okf_store, "MAX_SCOPE_COLUMNS", 1)
    with pytest.raises(HTTPException) as columns:
        await _read(session, settings, estate)
    assert "Refused before any column was loaded" in str(columns.value.detail)
    assert await _count(session, OkfBundlePublication) == 0


async def test_the_source_routes_serve_one_stored_publication_and_pin_it(
    session: AsyncSession, settings: Settings
) -> None:
    """Manifest, document, download, publications and question context all name the same
    stored publication; a download pinned to an inspected manifest returns those bytes after a
    newer publication; a path outside the bundle is not found; `NO_MATCH` is an answer."""
    estate = await _estate(session)
    datasource_id = _warehouse(estate).id
    context = _context(estate["organization"].id)
    inspected = await inspect_source_okf_bundle(datasource_id, context, session, settings)
    assert inspected.valid, inspected.findings
    assert inspected.manifest["scope"]["kind"] == SCOPE_DATASOURCE
    assert inspected.manifest["scope"]["datasource_id"] == str(datasource_id)
    publication_id = inspected.publication.publication_id

    path = next(entry.path for entry in inspected.files if "/views/" in entry.path)
    document = await read_source_okf_document(datasource_id, path, context, session, settings)
    assert document.publication_id == publication_id
    assert document.sha256 == next(e.sha256 for e in inspected.files if e.path == path)
    with pytest.raises(HTTPException) as missing:
        await read_source_okf_document(
            datasource_id, "concepts/index.md", context, session, settings
        )
    assert missing.value.status_code == 404

    selected = await select_source_okf_context(
        datasource_id,
        OkfContextRequest(question="which table holds completed orders by channel?"),
        context,
        session,
        settings,
    )
    assert selected.status == "MATCHED"
    assert selected.publication.publication_id == publication_id
    assert selected.datasource_id == datasource_id
    assert any(item.path.endswith(".md") and "/tables/" in item.path for item in selected.documents)
    assert "data source warehouse" in selected.markdown
    nothing = await select_source_okf_context(
        datasource_id, OkfContextRequest(question="zebra quokka"), context, session, settings
    )
    assert nothing.status == "NO_MATCH"
    audits = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "datasource.okf_context_read")
        )
    ).all()
    assert audits and not any("completed orders" in json.dumps(a.details) for a in audits)

    await _redefine_view(session, estate, "SELECT 1 AS one FROM sales.orders")
    newer = await inspect_source_okf_bundle(datasource_id, context, session, settings)
    assert newer.publication.sequence == 2
    pinned = await download_source_okf_bundle(
        datasource_id, context, session, settings, publication_id=publication_id
    )
    assert pinned.headers["X-Atlas-OKF-Publication-Id"] == str(publication_id)
    assert pinned.headers["X-Atlas-Bundle-Content-SHA256"] == inspected.bundle_content_digest
    assert f"datasource-{datasource_id}-okf-bundle.zip" in pinned.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(pinned.body)) as archive:
        manifest = json.loads(archive.read("atlas-manifest.json"))
        members = set(archive.namelist())
    assert manifest["bundle_content_digest"] == inspected.bundle_content_digest
    assert {entry.path for entry in inspected.files} == {
        name.removeprefix("bundle/") for name in members if name.startswith("bundle/")
    }
    history = await list_source_okf_publications(datasource_id, context, session, settings)
    assert [item.sequence for item in history.items] == [2, 1]
    assert [item.is_current for item in history.items] == [True, False]
    outbox = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "datasource.okf_bundle_exported.v1")
        )
    ).all()
    assert {event.payload["channel"] for event in outbox} >= {
        "OKF_SOURCE_MANIFEST",
        "OKF_SOURCE_DOCUMENT",
        "OKF_SOURCE_CONTEXT",
        "OKF_SOURCE_DOWNLOAD",
        "OKF_SOURCE_HISTORY",
    }


async def test_no_sentinel_reaches_any_row_the_source_path_writes(
    session: AsyncSession, settings: Settings
) -> None:
    """INV-6 on the source storage path: routine bodies, view definitions, column and parameter
    defaults, source comments and a connector's free-text reason are planted by the estate, a
    view is redefined around a new sentinel and the bundle rebuilt, and every row the path wrote
    is scanned."""
    estate = await _estate(session)
    context = _context(estate["organization"].id)
    datasource_id = _warehouse(estate).id
    await inspect_source_okf_bundle(datasource_id, context, session, settings)
    fresh = "ZZQ-OKF02-SOURCE-SENTINEL-REDEFINED-6d21"
    await _redefine_view(session, estate, f"SELECT order_id FROM sales.orders /* {fresh} */")  # noqa: S608
    rebuilt = await inspect_source_okf_bundle(datasource_id, context, session, settings)
    assert rebuilt.publication.sequence == 2
    await download_source_okf_bundle(datasource_id, context, session, settings)
    scanned = 0
    for model in (OkfBundlePublication, OkfBundleDocument, OkfBundleHead, AuditEvent, OutboxEvent):
        for row in (await session.scalars(select(model))).all():
            scanned += 1
            for value in _persisted_values(row):
                for sentinel in (*_SENTINELS, fresh):
                    assert sentinel not in value, (model.__tablename__, sentinel)
    assert scanned > 20


async def test_a_stored_row_is_one_scope_never_both_and_never_neither(
    session: AsyncSession, settings: Settings
) -> None:
    """The `one_scope` check constraint the migration adds, exercised: a head naming both a
    product version and a datasource, or neither, is refused by the database itself."""
    estate = await _estate(session)
    stored = await _read(session, settings, estate)
    _product_row, version = await _product(session, estate, include_far_source=False)
    for scope in (
        {"context_product_version_id": version.id, "datasource_id": _warehouse(estate).id},
        {"context_product_version_id": None, "datasource_id": None},
    ):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    OkfBundleHead(
                        organization_id=estate["organization"].id,
                        authority_digest="0" * 64,
                        publication_id=stored.publication.id,
                        validated_at=datetime.now(UTC),
                        marks_window_start=datetime.now(UTC) - timedelta(hours=1),
                        marks_digest="0" * 64,
                        **scope,
                    )
                )
                await session.flush()


# --- structure: one store, and nothing renders beside it --------------------------------


_SOURCE_DOORS = (
    "inspect_source_okf_bundle",
    "download_source_okf_bundle",
    "read_source_okf_document",
    "list_source_okf_publications",
    "select_source_okf_context",
)
_RENDERING = frozenset(
    {
        "freeze_snapshot",
        "freeze_source_snapshot",
        "export_okf_bundle",
        "export_okf_bundle_incremental",
        "_load_source",
    }
)


@pytest.mark.parametrize("handler", _SOURCE_DOORS)
def test_every_source_bundle_door_reads_the_one_store_and_renders_nothing_itself(
    handler: str,
) -> None:
    """Mirrors the product doors' structural test: each source route reaches the store's one
    source read -- and through it the datasource's gate decision -- and never freezes, resolves
    or renders on its own."""
    assert reaches_call(
        "aida.okf_export_api", handler, frozenset({"read_published_source_bundle"})
    )
    assert reaches_call(
        "aida.okf_export_api", handler, frozenset({"gate", "authorize_enforced", "authorize"})
    )
    assert not references_name("aida.okf_export_api", handler, _RENDERING)


def test_both_scopes_are_served_by_the_same_store_rules() -> None:
    """Sharing, not copying: the product read and the source read both hand their scope to
    `_serve`, which alone reaches the incremental rebuild and the atomic publish -- so neither
    read can publish, prune or skip the consistency check by a path of its own."""
    for read in ("read_published_bundle", "read_published_source_bundle"):
        assert reaches_call("aida.okf_store", read, frozenset({"_serve"}))
        assert not references_name(
            "aida.okf_store", read, frozenset({"_publish", "export_okf_bundle_incremental"})
        )
    assert reaches_call("aida.okf_store", "_serve", frozenset({"export_okf_bundle_incremental"}))


def test_only_the_store_freezes_a_source_for_a_consumer() -> None:
    """The product rule, extended to the source freeze: no module in `src/` but the store (and
    the snapshot module that defines it) calls `freeze_source_snapshot`."""
    allowed = {"okf_snapshot.py", "okf_store.py"}
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "src").rglob("*.py"))
        if "freeze_source_snapshot(" in path.read_text(encoding="utf-8")
        and path.name not in allowed
    ]
    assert offenders == []


def test_the_source_scope_and_its_surfaces_are_documented() -> None:
    """The export profile is the external contract; a source bundle's scope keys and routes
    must be in it, as the product's are."""
    reference = (REPO_ROOT / "Docs" / "90-reference" / "okf-export-profile.md").read_text(
        encoding="utf-8"
    )
    assert "`scope.source_key`" in reference
    assert "`DATASOURCE`" in reference
    for route in (
        "GET /v1/datasources/{datasource_id}/okf-bundle`",
        "GET /v1/datasources/{datasource_id}/okf-bundle/download`",
        "GET /v1/datasources/{datasource_id}/okf-bundle/document?path=`",
        "GET /v1/datasources/{datasource_id}/okf-bundle/publications`",
        "POST /v1/datasources/{datasource_id}/okf-bundle/context`",
    ):
        assert route in reference, route
