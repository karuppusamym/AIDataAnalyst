"""R11-OKF03: an editor who types under the exporter's placeholder proposes only what they typed.

When Atlas holds no approved text for an object, its exported `# Purpose` is one sentence
saying so ("Not established. No approved description of this view exists."). An editor who adds
their description *under* that sentence, instead of replacing it, used to propose a description
that opened with Atlas's own admission of ignorance -- and `_PURPOSE_PLACEHOLDER`, written for
exactly this, was never consulted. It is now: the sentence is dropped from the proposal, and
only what was added is proposed.
"""

from __future__ import annotations

import pytest

from aida.okf_import import OUTCOME_PROPOSE
from aida.okf_import_api import preview_okf_bundle_import
from aida.okf_import_bundle import _after_the_exporters_placeholder
from tests.test_okf_import import (  # noqa: F401 -- fixtures are found by name
    _context,
    _edited,
    _estate,
    _export,
    _meaningful_product,
    _paths,
    _request,
    session,
    settings,
)

PLACEHOLDER = "Not established. No approved description of this view exists."


def test_text_typed_after_the_placeholder_is_the_whole_proposal() -> None:
    assert (
        _after_the_exporters_placeholder(
            PLACEHOLDER, f"{PLACEHOLDER} Open orders joined to their customers."
        )
        == "Open orders joined to their customers."
    )


def test_a_rewrapped_placeholder_is_still_recognized() -> None:
    wrapped = "Not established.\nNo approved description of this   view exists.\n\nOpen orders."
    assert _after_the_exporters_placeholder(PLACEHOLDER, wrapped) == "Open orders."


def test_text_typed_over_the_placeholder_is_left_exactly_as_written() -> None:
    assert (
        _after_the_exporters_placeholder(PLACEHOLDER, "Open orders joined to their customers.")
        == "Open orders joined to their customers."
    )


def test_an_approved_base_is_never_treated_as_a_placeholder() -> None:
    # Approved text that happens to start "Not established." is still approved text: extending
    # it is a normal edit, and the whole extended text is what is proposed.
    base = "Not established practice, per the audit: orders are one row per payment."
    extended = base + " Refunds are separate rows."
    assert _after_the_exporters_placeholder(base, extended) == extended


def test_the_placeholder_alone_leaves_nothing_to_propose() -> None:
    assert _after_the_exporters_placeholder(PLACEHOLDER, PLACEHOLDER) == ""


@pytest.mark.asyncio
async def test_through_the_preview_only_the_added_text_is_proposed(
    session, settings  # noqa: F811
) -> None:
    estate = await _estate(session)
    version, _ontology = await _meaningful_product(session, estate)
    exported = await _export(session, settings, version)
    paths = _paths(estate, exported)
    texts = {document.path: document.text for document in exported.documents}
    view_text = texts[paths["view"]]
    assert "Not established." in view_text, "the view carries no approved description"

    marker = "# Purpose\n\n"
    start = view_text.index(marker) + len(marker)
    end = view_text.index("\n\n", start)
    section = view_text[start:end]
    assert section.startswith("Not established.")
    added = "Orders with their customer, for the open-orders dashboard."
    edited = view_text[:start] + section + " " + added + view_text[end:]

    preview = await preview_okf_bundle_import(
        version.id,
        _request(_edited(exported, {paths["view"]: edited})),
        context=_context(estate["organization"].id),
        session=session,
        settings=settings,
    )
    purposes = [
        item
        for item in preview.items
        if item.outcome == OUTCOME_PROPOSE and item.field == "purpose"
    ]
    assert len(purposes) == 1
    assert purposes[0].proposed_value == added
    assert "Not established" not in (purposes[0].proposed_value or "")
