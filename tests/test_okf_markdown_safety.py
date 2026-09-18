"""R11-OKF01 remainder: a catalog label is literal text in every OKF document.

A name is chosen by the *source*, and a database that allows quoted identifiers allows a table
called ``x](https://outside.example) [y``, a column called ``amount|total`` or one with a line
break in it. Written into Markdown as-is, the first became a link out of the bundle -- which the
publish policy then refused, so one odd name made a whole product's bundle unpublishable -- and
the others split a schema row into extra cells or extra rows. What this module proves, by
rendering the real output with a CommonMark + GFM-table parser rather than by pattern-matching:

* every hostile label renders as the characters it is -- no foreign link, no HTML, one cell of
  one row -- and the bundle passes the publish policy;
* an ordinary identifier renders byte-for-byte as before, so the escaping moves nothing else;
* the policy is escape-aware without being weaker: a real link or real markup in approved prose,
  including one after an escaped backslash, is still refused.

`markdown-it-py` comes from the locked environment (a dependency of `rich`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from markdown_it import MarkdownIt

from aida.okf_export import (
    OkfBundle,
    OkfDescription,
    export_okf_bundle,
    md_code,
    md_text,
    validate_atlas_publish_policy,
    validate_okf_conformance,
)
from tests.test_okf_context import _approved, _bank, _column, _keys, _path

HOSTILE_TABLE = "evil](https://outside.example/t) [y"
HOSTILE_SOURCE = "<script>alert(1)</script>"
HOSTILE_ALIAS = "[click here](https://outside.example/a)"
HOSTILE_COLUMNS = ("amount|total", "line\nbreak", "tick`name", "_lead", "[bracket]")

_PARSER = MarkdownIt("commonmark").enable("table")


def _body(text: str) -> str:
    return text.split("---\n", 2)[2] if text.startswith("---\n") else text


def _hostile() -> Any:
    base = _bank()
    balances = replace(
        base.objects[0],
        name=HOSTILE_TABLE,
        qualified_name=f"bank.warehouse.{HOSTILE_TABLE}",
        columns=tuple(
            _column(name, ordinal, f"Column {ordinal}.")
            for ordinal, name in enumerate(HOSTILE_COLUMNS, 1)
        ),
    )
    concept = replace(base.concepts[0], aliases=(HOSTILE_ALIAS,))
    source = replace(base.sources[0], name=HOSTILE_SOURCE)
    return _bank(
        sources=(source,), objects=(balances, *base.objects[1:]), concepts=(concept,)
    )


def _links(html_tokens: list[Any]) -> list[str]:
    hrefs: list[str] = []
    for token in html_tokens:
        for child in [token, *(token.children or [])]:
            if child.type == "link_open":
                hrefs.append(str(child.attrs.get("href", "")))
    return hrefs


def test_ordinary_names_render_exactly_as_before() -> None:
    ordinary = (
        "fact_account_balances",
        "bank.warehouse.fact_account_balances",
        "End of day position",
        "$v-1",
    )
    for name in ordinary:
        assert md_text(name) == name
    assert md_code("order_id") == "`order_id`"
    assert md_code("numeric(18,2)", table_cell=True) == "`numeric(18,2)`"


def test_a_hostile_bundle_publishes_and_no_document_links_out_or_carries_markup() -> None:
    bundle = export_okf_bundle(_hostile())
    documents = {document.path: document.text for document in bundle.documents}
    assert validate_okf_conformance(documents).valid
    assert validate_atlas_publish_policy(bundle).valid, validate_atlas_publish_policy(bundle)
    for path, text in documents.items():
        tokens = _PARSER.parse(_body(text))
        for href in _links(tokens):
            assert "outside.example" not in href, (path, href)
            assert href.startswith(("/", "atlas://")) or not href.startswith("http"), (path, href)
        html = _PARSER.render(_body(text))
        assert "<script" not in html, path


def test_a_hostile_name_is_the_characters_it_is() -> None:
    snapshot = _hostile()
    documents = {d.path: d.text for d in export_okf_bundle(snapshot).documents}
    concept = _PARSER.render(_body(documents[_path(snapshot, _keys()["concept"])]))
    assert "[click here](https://outside.example/a)" in concept  # shown, not linked
    schema_index = next(
        text for path, text in documents.items() if path.endswith("index.md") and "evil" in text
    )
    rendered = _PARSER.render(_body(schema_index))
    assert "evil](https://outside.example/t) [y" in rendered.replace("&quot;", '"')


def test_a_hostile_column_name_is_one_cell_of_one_row() -> None:
    snapshot = _hostile()
    text = {d.path: d.text for d in export_okf_bundle(snapshot).documents}[
        _path(snapshot, _keys()["balances"])
    ]
    tokens = _PARSER.parse(_body(text))
    rows: list[list[str]] = []
    inside_body = False
    for token in tokens:
        if token.type == "tbody_open":
            inside_body = True
        elif token.type == "tbody_close":
            inside_body = False
        elif inside_body and token.type == "tr_open":
            rows.append([])
        elif inside_body and token.type == "inline" and rows:
            rows[-1].append(token.content)
    assert len(rows) == len(HOSTILE_COLUMNS)
    assert all(len(cells) == 5 for cells in rows), rows
    code = [
        child.content
        for token in tokens
        if token.type == "inline"
        for child in token.children or []
        if child.type == "code_inline"
    ]
    for name in ("amount|total", "line break", "tick`name", "_lead", "[bracket]"):
        assert name in code, (name, code)


def _with_description(text: str) -> OkfBundle:
    base = _bank()
    balances = replace(base.objects[0], description=_approved(text))
    return export_okf_bundle(_bank(objects=(balances, *base.objects[1:])))


def test_the_policy_still_refuses_a_real_link_or_real_markup_in_approved_prose() -> None:
    linked = validate_atlas_publish_policy(
        _with_description("See [the notes](https://outside.example/n).")
    )
    assert any(item.startswith("EXTERNAL_LINK:") for item in linked.findings)
    tagged = validate_atlas_publish_policy(
        _with_description('An <img src="https://outside.example/p"> row.')
    )
    assert any(item.startswith("FORBIDDEN_RAW_MARKUP:") for item in tagged.findings)
    # An escaped backslash is a pair: the link after it is a real link and is still refused.
    after_escape = validate_atlas_publish_policy(
        _with_description("A path \\\\[the notes](https://outside.example/n) here.")
    )
    assert any(item.startswith("EXTERNAL_LINK:") for item in after_escape.findings)
    # While an escaped bracket is text, and is not.
    escaped = validate_atlas_publish_policy(
        _with_description("Literally \\[the notes\\](https://outside.example/n) here.")
    )
    assert not any(item.startswith("EXTERNAL_LINK:") for item in escaped.findings), escaped


def test_an_unapproved_description_is_never_the_one_rendered() -> None:
    """Guard for the fixture above: only approved text reaches a document."""
    base = _bank()
    draft = replace(
        base.objects[0],
        description=OkfDescription(state="PROPOSED", text="[x](https://outside.example)"),
    )
    bundle = export_okf_bundle(_bank(objects=(draft, *base.objects[1:])))
    assert validate_atlas_publish_policy(bundle).valid
