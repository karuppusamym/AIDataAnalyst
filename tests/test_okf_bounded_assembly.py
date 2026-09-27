"""R11-OKF02: the bundle limits bound the *work*, not only the output.

`_assemble_bundle` checks the final document count and byte total, but it sees the documents
only after every one has been built in memory. These tests count renders: a plan over the
document limit renders nothing, and a bundle that crosses its byte budget stops at the document
that crossed it -- on the full path and the incremental one. The scope refusal before the
snapshot is loaded is tested in `tests/test_okf_store.py`
(`test_an_oversized_scope_is_refused_before_anything_is_frozen`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from aida import okf_export
from aida.okf_export import (
    OkfDocument,
    OkfExportError,
    OkfPublicationStamp,
    export_okf_bundle,
    export_okf_bundle_incremental,
)
from tests.test_okf_export import _snapshot


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every document the renderer builds, in order, by wrapping each planned item."""
    built: list[str] = []
    original_plan = okf_export._plan
    original_log_plan = okf_export._log_plan

    def counted(items: list[Any]) -> list[Any]:
        def wrap(item: Any) -> Any:
            render: Callable[[], OkfDocument | None] = item.render

            def counting() -> OkfDocument | None:
                built.append(item.path)
                return render()

            return replace(item, render=counting)

        return [wrap(item) for item in items]

    monkeypatch.setattr(okf_export, "_plan", lambda snapshot: counted(original_plan(snapshot)))
    monkeypatch.setattr(
        okf_export,
        "_log_plan",
        lambda snapshot, history: counted(original_log_plan(snapshot, history)),
    )
    return built


def _stamp() -> OkfPublicationStamp:
    return OkfPublicationStamp(date="2026-09-18", sequence=1, trigger="INITIAL")


def test_the_limits_leave_an_ordinary_bundle_alone(renders: list[str]) -> None:
    bundle = export_okf_bundle(_snapshot())

    assert len(renders) >= len(bundle.documents) > 3


def test_a_plan_over_the_document_limit_renders_nothing(
    renders: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(okf_export, "MAX_DOCUMENTS", 2)

    with pytest.raises(OkfExportError, match="Refused before any document was rendered"):
        export_okf_bundle(_snapshot())
    assert renders == []

    with pytest.raises(OkfExportError, match="Refused before any document was rendered"):
        export_okf_bundle_incremental(
            _snapshot(), prior_snapshot=None, prior_documents={}, stamp=_stamp()
        )
    assert renders == []


def test_the_byte_budget_stops_at_the_document_that_crosses_it(
    renders: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    full = export_okf_bundle(_snapshot())
    planned = len(renders)
    renders.clear()
    # Room for the first rendered document and not the second.
    first = next(document for document in full.documents if document.path == "index.md")
    monkeypatch.setattr(okf_export, "MAX_BUNDLE_BYTES", first.byte_length + 1)

    with pytest.raises(OkfExportError, match="while it was being rendered"):
        export_okf_bundle(_snapshot())
    assert 1 < len(renders) < planned, "stopped early, not after building everything"

    renders.clear()
    with pytest.raises(OkfExportError, match="while it was being rendered"):
        export_okf_bundle_incremental(
            _snapshot(), prior_snapshot=None, prior_documents={}, stamp=_stamp()
        )
    assert 1 < len(renders) < planned


def test_carried_documents_count_against_the_budget_too(
    renders: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incremental rebuild reuses stored bytes without rendering them; they are still part
    of the bundle, so they are still charged."""
    snapshot = _snapshot()
    bundle, _report, _history = export_okf_bundle_incremental(
        snapshot, prior_snapshot=None, prior_documents={}, stamp=_stamp()
    )
    prior = {document.path: document.text for document in bundle.documents}
    total = sum(document.byte_length for document in bundle.documents)
    monkeypatch.setattr(okf_export, "MAX_BUNDLE_BYTES", total // 2)
    renders.clear()

    with pytest.raises(OkfExportError, match="while it was being rendered"):
        export_okf_bundle_incremental(
            snapshot,
            prior_snapshot=snapshot,
            prior_documents=prior,
            stamp=OkfPublicationStamp(date="2026-09-18", sequence=2, trigger="REVALIDATION"),
        )
