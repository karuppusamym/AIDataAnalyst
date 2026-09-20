"""R11-OKF02: the Catalog's object knowledge read answers from the object's own datasource bundle
when no product bundle holds it.

`GET /v1/metadata/tables/{id}/okf-knowledge` used to answer only from product bundles, so a
steward opening an object no product covers was told nothing -- while the datasource's own stored
source bundle held that object's document. The read now carries a `source` entry, resolved on the
server because a document's path is a digest of catalog, schema and name that no client can form
(no table read exposes the catalog name). What this module holds in place:

* **Three answers, never blurred.** `DOCUMENT` (the document, its publication and coverage, the
  stored bytes), `NOT_IN_BUNDLE` and `REFUSED`. An absence reads the same whether the object was
  never discovered, is no longer ACTIVE or sits in a schema the reader's workspace refuses, and
  carries nothing about the bundle. A refusal is not an absence: it carries the bare reason code
  and nothing else, and reads nothing.
* **Product wins.** When a product bundle holds the object, `source` is null and the source
  bundle is not even consulted -- proven with a spy, not inferred from a row count.
* **The datasource's own decision.** A revoked binding or a policy refusal is the source routes'
  own 403, carried as the state; a bundle that cannot be built is an HTTP error and never a state;
  a 403 whose detail is not a reason code is never echoed.
* **The current publication's bytes.** After a rebuild the document is the current
  publication's row, rebuilt or carried, and never an older retained publication's.
* **One door to the store.** The handler reaches `read_published_source_bundle` and so the
  datasource's gate, renders nothing itself, and both reads of an object name it by one key.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.okf_store as okf_store
from aida.config import Settings
from aida.db import Base
from aida.models import AccessPolicy, AuditEvent, ContextProductConsumptionEdge, OutboxEvent
from aida.okf_export_api import read_object_okf_knowledge
from aida.okf_store_models import OkfBundleDocument, OkfBundleHead, OkfBundlePublication
from aida.schemas import OkfObjectKnowledgeRead, OkfObjectSourceRead
from aida.security import SecurityContext
from tests.support.app_surface import reaches_call, references_name
from tests.test_okf_export import _context, _estate, _product
from tests.test_okf_source_bundles import (
    HIDDEN_SCHEMA,
    HIDDEN_TABLE,
    _enforcing_workspace,
    _hidden_schema,
    _key,
    _redefine_view,
    _warehouse,
)

_ACTION = "datasource.okf_object_read"
_RENDERING = frozenset(
    {"freeze_snapshot", "export_okf_bundle", "export_okf_bundle_incremental", "_load_source"}
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # StaticPool: the gate's durable shadow-record path opens a second session.
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


async def _object(
    session: AsyncSession,
    settings: Settings,
    estate: dict[str, Any],
    table: str,
    *,
    context: SecurityContext | None = None,
) -> OkfObjectKnowledgeRead:
    """The route handler, called as the other OKF tests call theirs."""
    return await read_object_okf_knowledge(
        estate["tables"][table].id,
        context or _context(estate["organization"].id),
        session,
        settings,
    )


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _source_publications(session: AsyncSession) -> int:
    return await _count(
        session, OkfBundlePublication, OkfBundlePublication.datasource_id.is_not(None)
    )


async def _object_audits(session: AsyncSession) -> list[AuditEvent]:
    rows = await session.scalars(select(AuditEvent).where(AuditEvent.action == _ACTION))
    return list(rows.all())


def _source(read: OkfObjectKnowledgeRead) -> OkfObjectSourceRead:
    assert read.source is not None
    return read.source


# --- the three answers --------------------------------------------------------------------------


async def test_an_object_no_product_holds_is_read_from_its_datasources_own_bundle(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    read = await _object(session, settings, estate, "warehouse.orders")

    assert read.items == []
    source = _source(read)
    assert source.state == "DOCUMENT"
    assert source.reason is None
    assert source.datasource_id == _warehouse(estate).id
    assert source.datasource_name == _warehouse(estate).name
    document = source.document
    publication = source.publication
    coverage = source.coverage
    assert document is not None and publication is not None and coverage is not None
    assert document.subject_key == _key(estate, "orders")
    assert "bank.sales.orders" in document.content
    assert publication.is_current and publication.publication_id == document.publication_id
    assert coverage["key"] == document.subject_key
    assert "description_state" in coverage

    # The bytes a downloaded archive would hold: the stored row, not a re-render.
    stored = await session.scalar(
        select(OkfBundleDocument).where(
            OkfBundleDocument.publication_id == publication.publication_id,
            OkfBundleDocument.subject_key == document.subject_key,
        )
    )
    assert stored is not None
    assert (document.content, document.sha256, document.path) == (
        stored.content,
        stored.sha256,
        stored.path,
    )
    # A second read serves the same publication rather than building another.
    again = _source(await _object(session, settings, estate, "warehouse.orders"))
    assert again.publication is not None
    assert again.publication.publication_id == publication.publication_id
    assert await _source_publications(session) == 1


async def test_after_a_rebuild_the_document_is_the_current_publications_and_not_an_older_ones(
    session: AsyncSession, settings: Settings
) -> None:
    """Superseded publications are retained, and each holds its own row for every subject, so a
    lookup by subject alone can answer with an older publication's document while naming the
    current one. Both a rebuilt document and one a rebuild only carried must come from the
    current publication."""
    estate = await _estate(session)
    first_view = _source(await _object(session, settings, estate, "warehouse.orders_v"))
    first_table = _source(await _object(session, settings, estate, "warehouse.orders"))
    assert first_view.publication is not None and first_view.publication.sequence == 1

    await _redefine_view(session, estate, "SELECT order_id, channel FROM sales.orders")
    view = _source(await _object(session, settings, estate, "warehouse.orders_v"))
    table = _source(await _object(session, settings, estate, "warehouse.orders"))
    assert await _source_publications(session) == 2

    for after, before, rendered in ((view, first_view, 2), (table, first_table, 1)):
        assert after.publication is not None and after.document is not None
        assert before.document is not None
        assert after.publication.sequence == 2 and after.publication.is_current
        assert after.document.publication_id == after.publication.publication_id
        assert after.document.publication_sequence == 2
        # The changed view was rendered by publication 2; the table's bytes were only carried.
        assert after.document.rendered_in_sequence == rendered
        assert after.document.publication_id != before.document.publication_id


async def test_the_source_read_is_audited_on_its_own_channel_and_creates_no_consumption(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    read = await _object(session, settings, estate, "warehouse.orders")
    document = _source(read).document
    assert document is not None

    audits = await _object_audits(session)
    assert len(audits) == 1
    audit = audits[0]
    assert (audit.resource_type, audit.resource_id) == ("datasource", str(_warehouse(estate).id))
    assert audit.details["path"] == document.path
    assert audit.details["publication_id"] == str(document.publication_id)
    events = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "datasource.okf_bundle_exported.v1")
        )
    ).all()
    assert {event.payload["channel"] for event in events} == {"OKF_SOURCE_OBJECT"}
    # A datasource is not a context product: there is no consumption ledger to write to.
    assert await _count(session, ContextProductConsumptionEdge) == 0
    # The audit names the path and the digests, never the document's text.
    assert document.content not in json.dumps(audit.details)


async def test_an_object_the_bundle_does_not_hold_reads_the_same_whether_absent_or_not_admitted(
    session: AsyncSession, settings: Settings
) -> None:
    """A table no longer ACTIVE and a table in a schema the reader's workspace refuses are
    different facts the reader must not be able to tell apart -- and neither carries anything
    about the bundle, so the answer cannot be used to count what was left out."""
    estate = await _estate(session)
    hidden = await _hidden_schema(session, estate)
    await _enforcing_workspace(session, estate)
    session.add(
        AccessPolicy(
            organization_id=estate["organization"].id,
            code="no-hr-metadata",
            name="No HR schemas",
            effect="DENY",
            priority=1000,
            resource_match={"schema_pattern": "hr_*"},
            action_match=["READ_METADATA"],
            created_by="seed",
        )
    )
    estate["tables"]["warehouse.orders"].status = "DEPRECATED"
    await session.flush()

    retired = await _object(session, settings, estate, "warehouse.orders")
    refused_schema = await read_object_okf_knowledge(
        hidden.id, _context(estate["organization"].id), session, settings
    )

    for read in (retired, refused_schema):
        assert read.items == []
        source = _source(read)
        assert source.state == "NOT_IN_BUNDLE"
        assert source.datasource_id == _warehouse(estate).id
        assert (source.publication, source.document, source.coverage, source.reason) == (
            None,
            None,
            None,
            None,
        )
    assert retired.source == refused_schema.source
    # Not `table_id`: the response echoes the id the caller sent.
    everything = json.dumps(_source(refused_schema).model_dump(mode="json"))
    for leaked in (HIDDEN_TABLE, HIDDEN_SCHEMA, str(hidden.id)):
        assert leaked not in everything, leaked
    # The bundle was read, so the read is recorded -- with no path, because no document was.
    audits = await _object_audits(session)
    assert len(audits) == 2 and all("path" not in audit.details for audit in audits)


async def test_a_refused_reader_is_told_it_was_refused_with_only_the_code_and_reads_nothing(
    session: AsyncSession,
) -> None:
    settings = Settings(_env_file=None, unresolved_workspace_posture="DENY")
    estate = await _estate(session)
    binding = await _enforcing_workspace(session, estate)
    granted = await _object(session, settings, estate, "warehouse.orders")
    assert _source(granted).state == "DOCUMENT"
    publications = await _source_publications(session)
    audits = len(await _object_audits(session))

    binding.status = "REVOKED"
    await session.flush()
    revoked = await _object(session, settings, estate, "warehouse.orders")
    source = _source(revoked)
    assert (source.state, source.reason) == ("REFUSED", "NO_BINDING_FOR_DATASOURCE")
    assert (
        source.datasource_id,
        source.datasource_name,
        source.publication,
        source.document,
        source.coverage,
    ) == (None, None, None, None, None)

    binding.status = "ACTIVE"
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
    denied = _source(await _object(session, settings, estate, "warehouse.orders"))
    assert (denied.state, denied.reason) == ("REFUSED", "DENIED_BY_POLICY")

    # A refusal read nothing: no new publication, no new audit record, no outbox event.
    assert await _source_publications(session) == publications
    assert len(await _object_audits(session)) == audits
    assert revoked.source != denied  # two reasons, two answers -- and neither is an absence
    assert source.state != "NOT_IN_BUNDLE"


# --- the product bundle wins --------------------------------------------------------------------


async def test_a_product_bundle_wins_and_the_source_bundle_is_not_even_consulted(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await _estate(session)
    await _product(session, estate, include_far_source=False)
    consulted: list[str] = []
    real = okf_store.read_published_source_bundle

    async def spy(*args: Any, **kwargs: Any) -> Any:
        consulted.append("source")
        return await real(*args, **kwargs)

    monkeypatch.setattr(okf_store, "read_published_source_bundle", spy)
    read = await _object(session, settings, estate, "warehouse.orders")

    assert len(read.items) == 1 and read.items[0].product_key == "revenue_context"
    assert read.source is None
    assert consulted == []
    assert await _source_publications(session) == 0
    assert await _object_audits(session) == []


async def test_an_object_a_product_does_not_admit_is_still_read_from_its_own_source(
    session: AsyncSession, settings: Settings
) -> None:
    """`people.salaries` is across an ungranted domain boundary, so the product bundle does not
    admit its source and contributes nothing (`items` is empty). The object's own datasource
    still may be read by this reader -- so the source answers, from the bundle of the datasource
    that holds it and no other."""
    estate = await _estate(session)
    await _product(session, estate, include_far_source=True)
    read = await _object(session, settings, estate, "people.salaries")

    assert read.items == []
    source = _source(read)
    assert source.state == "DOCUMENT"
    assert source.datasource_id == estate["datasources"]["people"][0].id
    assert source.document is not None and "salaries" in source.document.content
    assert source.datasource_id != _warehouse(estate).id


# --- what is an error and what is not -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (409, "OKF bundle publication raced; retry"),
        (409, {"findings": ["FORBIDDEN_CODE_FENCE"]}),
        (404, "datasource not found"),
        (500, "boom"),
    ],
)
async def test_a_bundle_that_cannot_be_read_is_an_http_error_and_never_a_state(
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    detail: Any,
) -> None:
    estate = await _estate(session)

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=status, detail=detail)

    monkeypatch.setattr(okf_store, "read_published_source_bundle", failing)
    with pytest.raises(HTTPException) as raised:
        await _object(session, settings, estate, "warehouse.orders")
    assert raised.value.status_code == status
    assert await _object_audits(session) == []


@pytest.mark.parametrize(
    "detail",
    ["Organization mismatch for this caller", {"reason": "NO_BINDING"}, "lower_case", "", "A" * 90],
)
async def test_a_403_whose_detail_is_not_a_reason_code_is_a_refusal_and_is_never_echoed(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch, detail: Any
) -> None:
    estate = await _estate(session)

    async def refusing(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=403, detail=detail)

    monkeypatch.setattr(okf_store, "read_published_source_bundle", refusing)
    source = _source(await _object(session, settings, estate, "warehouse.orders"))
    assert (source.state, source.reason) == ("REFUSED", okf_store.GENERIC_REFUSAL)


async def test_an_unknown_table_is_a_404_and_another_tenants_table_is_a_403_not_a_state(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    context = _context(estate["organization"].id)
    with pytest.raises(HTTPException) as unknown:
        await read_object_okf_knowledge(uuid4(), context, session, settings)
    assert unknown.value.status_code == 404
    foreign = _context(uuid4())
    with pytest.raises(HTTPException) as across:
        await _object(session, settings, estate, "warehouse.orders", context=foreign)
    assert across.value.status_code == 403
    assert await _source_publications(session) == 0
    assert await _count(session, OkfBundleHead) == 0


# --- the response shape -------------------------------------------------------------------------


def test_a_state_carries_exactly_the_fields_it_may() -> None:
    """A refusal that also carried a publication would name a bundle; an absence that carried a
    document would not be one. The model refuses both at construction."""
    ok = OkfObjectSourceRead(state="REFUSED", reason="NO_BINDING_FOR_DATASOURCE")
    assert ok.datasource_id is None and ok.publication is None
    absent = OkfObjectSourceRead(state="NOT_IN_BUNDLE", datasource_id=uuid4(), datasource_name="w")
    assert absent.document is None
    for bad in (
        {"state": "REFUSED"},
        {"state": "REFUSED", "reason": "X", "datasource_id": uuid4()},
        {"state": "NOT_IN_BUNDLE", "datasource_id": uuid4()},
        {"state": "NOT_IN_BUNDLE", "datasource_id": uuid4(), "datasource_name": "w", "reason": "X"},
        {"state": "DOCUMENT", "datasource_id": uuid4(), "datasource_name": "w"},
        {"state": "MAYBE", "reason": "X"},
    ):
        with pytest.raises(ValidationError):
            OkfObjectSourceRead(**bad)


def test_the_source_entry_is_optional_so_a_product_only_answer_is_unchanged() -> None:
    read = OkfObjectKnowledgeRead(table_id=uuid4(), items=[])
    assert read.source is None
    assert json.loads(read.model_dump_json())["source"] is None


# --- structure ----------------------------------------------------------------------------------


def test_the_object_door_reaches_the_source_store_read_and_the_datasources_gate() -> None:
    handler = "read_object_okf_knowledge"
    assert reaches_call("aida.okf_export_api", handler, frozenset({"read_object_source_knowledge"}))
    assert reaches_call("aida.okf_export_api", handler, frozenset({"read_published_source_bundle"}))
    assert reaches_call(
        "aida.okf_export_api", handler, frozenset({"gate", "authorize_enforced", "authorize"})
    )
    assert not references_name("aida.okf_export_api", handler, _RENDERING)
    assert not references_name("aida.okf_store", "read_object_source_knowledge", _RENDERING)


def test_both_reads_of_an_object_name_it_by_one_key() -> None:
    """Sharing, not copying: the product read and the source read both take the object's
    identity from `_object_subject`, so how objects are keyed cannot change for one only."""
    for function in ("read_object_knowledge", "read_object_source_knowledge"):
        assert references_name("aida.okf_store", function, frozenset({"_object_subject"})), function
        assert not references_name("aida.okf_store", function, frozenset({"object_key"})), function
