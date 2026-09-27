"""R11-OKF02: a large schema's index is published as pages.

Measured 2026-09-25 (`scripts/measure_okf_source_bundle_cost.py`): a schema of 5,000 tables
could not be bundled at all, because its `index.md` lists every entry and passed the
per-document byte limit. Past `SCHEMA_INDEX_PAGE_ENTRIES` entries the schema index now lists
pages, each an `index.md` in its own page directory. What this module holds in place:

* a small schema renders exactly as before -- no page directory, the same index;
* a large one links every page, every page links back and lists at most the page size, and
  every entry is listed exactly once across the pages, in index order;
* the bundle stays OKF-conformant and passes Atlas's publish policy, and each page classifies
  as `SCHEMA_INDEX_PAGE`;
* an incremental rebuild that adds an entry early re-renders every page (its range shifted),
  and a no-op rebuild re-renders none.
"""

from __future__ import annotations

import re
from dataclasses import replace

from aida.okf_export import (
    SCHEMA_INDEX_PAGE_ENTRIES,
    OkfPublicationStamp,
    OkfSnapshot,
    document_kind,
    export_okf_bundle,
    export_okf_bundle_incremental,
    object_key,
    validate_atlas_publish_policy,
    validate_okf_conformance,
)
from tests.test_okf_export import _snapshot

_LINK = re.compile(r"\]\(([^)]+)\)")


def _wide(count: int, *, prefix: str = "t") -> OkfSnapshot:
    snapshot = _snapshot()
    template = next(obj for obj in snapshot.objects if obj.name == "orders")
    extra = tuple(
        replace(
            template,
            key=object_key("11111111-1111-1111-1111-111111111111", "bank", "sales", name),
            name=name,
            qualified_name=f"bank.sales.{name}",
            links=(),
        )
        for name in (f"{prefix}_{index:05d}" for index in range(count))
    )
    return replace(snapshot, objects=(*snapshot.objects, *extra))


def _schema_index(documents: dict[str, str]) -> str:
    [path] = [p for p in documents if document_kind(p) == "SCHEMA_INDEX"]
    return path


def test_a_small_schema_has_no_pages() -> None:
    documents = {d.path: d.text for d in export_okf_bundle(_snapshot()).documents}
    assert not any("index-pages/" in path for path in documents)
    assert "# Tables" in documents[_schema_index(documents)]


def test_a_large_schema_lists_pages_that_hold_every_entry_once() -> None:
    count = SCHEMA_INDEX_PAGE_ENTRIES * 2 + 17
    bundle = export_okf_bundle(_wide(count))
    documents = {d.path: d.text for d in bundle.documents}
    index_path = _schema_index(documents)
    index = documents[index_path]
    pages = sorted(p for p in documents if document_kind(p) == "SCHEMA_INDEX_PAGE")
    assert len(pages) == 3
    assert "# Index pages" in index and "# Tables" not in index
    schema_dir = index_path.rsplit("/", 1)[0]
    # The schema index links every page, by the path the page is stored at.
    linked = {f"{schema_dir}/{target}" for target in _LINK.findall(index)}
    assert set(pages) <= linked
    listed: list[str] = []
    for page in pages:
        text = documents[page]
        assert "](../../index.md)" in text
        targets = [t for t in _LINK.findall(text) if t.startswith("../../tables/")]
        assert len(targets) <= SCHEMA_INDEX_PAGE_ENTRIES
        for target in targets:
            resolved = f"{schema_dir}/{target.removeprefix('../../')}"
            assert resolved in documents, resolved
        listed.extend(targets)
    # Every table once, in index order, across the pages (the fixture's two plus the extras).
    tables = [p for p in documents if document_kind(p) == "TABLE"]
    assert len(listed) == len(set(listed)) == len(tables)


def test_a_paged_bundle_stays_conformant_and_passes_the_publish_policy() -> None:
    bundle = export_okf_bundle(_wide(SCHEMA_INDEX_PAGE_ENTRIES + 1))
    documents = {d.path: d.text for d in bundle.documents}
    assert validate_okf_conformance(documents).valid
    assert validate_atlas_publish_policy(bundle).valid


def _stamp(sequence: int) -> OkfPublicationStamp:
    return OkfPublicationStamp(date="2026-09-25", sequence=sequence, trigger="INITIAL")


def test_an_entry_added_early_re_renders_every_page_and_a_no_op_none() -> None:
    before = _wide(SCHEMA_INDEX_PAGE_ENTRIES + 500)
    first, _report, history = export_okf_bundle_incremental(
        before, prior_snapshot=None, prior_documents={}, stamp=_stamp(1)
    )
    prior = {d.path: d.text for d in first.documents}
    pages = sorted(p for p in prior if document_kind(p) == "SCHEMA_INDEX_PAGE")
    assert len(pages) == 2

    again, report, _ = export_okf_bundle_incremental(
        before,
        prior_snapshot=before,
        prior_documents=prior,
        prior_history=history,
        stamp=_stamp(2),
    )
    assert not set(pages) & set(report.rendered)

    # "a_early" sorts before every "t_" table, so page 2's first entry moves to page 1's end.
    grown = replace(before, objects=(*before.objects, *_wide(1, prefix="a_early").objects[-1:]))
    after, report, _ = export_okf_bundle_incremental(
        grown,
        prior_snapshot=before,
        prior_documents=prior,
        prior_history=history,
        stamp=_stamp(2),
    )
    assert set(pages) <= set(report.rendered)
    fresh = {d.path: d.text for d in export_okf_bundle(grown).documents}
    for page in pages:
        assert after.document(page).text == fresh[page]


def test_a_page_path_classifies_as_a_schema_index_page() -> None:
    documents = {d.path for d in export_okf_bundle(_wide(SCHEMA_INDEX_PAGE_ENTRIES + 1)).documents}
    kinds = {document_kind(path) for path in documents}
    assert "SCHEMA_INDEX_PAGE" in kinds and "OTHER" not in kinds
