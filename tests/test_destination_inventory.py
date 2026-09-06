"""Gate for the generated external destination and credential inventory.

Review 2026-09-05 section 6 item 7. `scripts/generate_destination_inventory.py`
derives the inventory from `atlas.platform.config.Settings`; this keeps the
committed document from drifting away from it, and pins the two properties that
matter more than the table's contents:

* **No setting's value is ever written into the document.** The generator emits
  names, characterizations and hosts. Several of these settings are credentials,
  and `database_url`'s shipped default embeds a password in its userinfo, so a
  generator that leaked one into a committed Markdown file would be a worse
  defect than the missing inventory it was written to fix.
* **The five state columns stay five distinct things.** A change that quietly
  made `configured` and `active` the same string would defeat the entire point of
  the item, and nothing else in the repository would notice.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from generate_destination_inventory import (  # noqa: E402
    DEFAULT_OUTPUT,
    _scheme_and_host,
    classify,
    collect_rows,
    describe_default,
    render,
)

from atlas.platform.config import Settings  # noqa: E402


def _document() -> str:
    return render(collect_rows())


def test_generated_document_is_not_stale() -> None:
    """The `--check` mode, as a test: a `Settings` change that adds or renames a
    destination must regenerate this file, not silently diverge from it.
    """
    assert DEFAULT_OUTPUT.exists(), (
        f"{DEFAULT_OUTPUT} is missing. Run "
        "`python scripts/generate_destination_inventory.py` to create it."
    )
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == _document(), (
        f"{DEFAULT_OUTPUT} is out of date with atlas.platform.config.Settings. "
        "Regenerate it with `python scripts/generate_destination_inventory.py`."
    )


def test_no_setting_value_appears_in_the_generated_document() -> None:
    """The rule the whole generator is built around.

    Checks the committed document, not just a freshly rendered one, so a value
    that got in by any route is caught. Defaults shorter than eight characters
    are skipped: `aida`, `neo4j` and `unset` are words that legitimately appear
    inside setting names and prose, and none of them is a secret.
    """
    document = DEFAULT_OUTPUT.read_text(encoding="utf-8")
    leaked: list[str] = []
    for name, field in Settings.model_fields.items():
        # Only the fields this generator actually emits. `environment`'s default
        # ("development") is not in the table and appears in prose as an ordinary
        # English word; scoping to the inventoried fields keeps the check about
        # values that could have been written out rather than about vocabulary.
        if classify(name, field.annotation) is None:
            continue
        default = field.default_factory() if field.default_factory else field.default
        if not isinstance(default, str) or len(default) < 8:
            continue
        # The generator publishes host[:port] deliberately. Where the whole value
        # IS a bare host:port (`temporal_address`, `kafka_bootstrap_servers`) the
        # two coincide and there is nothing else to withhold; any value carrying
        # a path, query, userinfo or credential beyond the host still fails.
        if _scheme_and_host(default)[1] == default.strip():
            continue
        if re.search(rf"\b{re.escape(default)}\b", document):
            leaked.append(f"{name}: its default value appears verbatim in the document")
        # A URL's userinfo is the part that is actually a credential.
        userinfo = re.match(r"[a-zA-Z0-9+.\-]+://([^/@\s]+)@", default)
        if userinfo and userinfo.group(1) in document:
            leaked.append(f"{name}: the `user:password@` of its default appears in the document")
    assert not leaked, (
        "scripts/generate_destination_inventory.py wrote configuration values into a "
        "committed document:\n" + "\n".join(f"  - {entry}" for entry in leaked)
    )


def test_credential_defaults_are_never_characterized_by_content() -> None:
    """`describe_default` must return a characterization, never the value, for
    every credential-shaped field -- including ones added later.
    """
    for name, field in Settings.model_fields.items():
        if classify(name, field.annotation) != "credential":
            continue
        default = field.default_factory() if field.default_factory else field.default
        if not isinstance(default, str) or not default:
            continue
        text, _ = describe_default(default, "credential")
        assert default not in text, (
            f"describe_default leaked {name}'s value into its own description"
        )


def test_the_five_states_are_distinct_columns() -> None:
    """Configured, approved, active, healthy and verified are five different
    questions. If two of them ever produce the same answer for every row, they
    have been collapsed into synonyms and the table has stopped saying anything.
    """
    rows = collect_rows()
    assert rows, "no destinations or credentials were inventoried at all"
    columns = {
        "configured": [row.configured for row in rows],
        "approved": [row.approved for row in rows],
        "active": [row.active for row in rows],
        "healthy": [row.healthy for row in rows],
        "verified": [row.verified for row in rows],
    }
    collapsed = [
        f"{a} and {b}"
        for i, a in enumerate(columns)
        for b in list(columns)[i + 1 :]
        if columns[a] == columns[b]
    ]
    assert not collapsed, (
        "these state columns produce an identical answer on every row, so they are "
        f"no longer distinguishing anything: {collapsed}"
    )


def test_verified_is_unknown_everywhere_and_says_why() -> None:
    """Verification is runtime evidence. A future change that starts filling this
    column in from static analysis is claiming a destination acknowledged traffic
    on the strength of reading source code -- exactly the defect F01 and F04
    describe. If real receipt evidence is ever wired in, delete this test
    deliberately rather than letting the column drift.
    """
    rows = collect_rows()
    assert {row.verified for row in rows} == {"unknown"}
    document = DEFAULT_OUTPUT.read_text(encoding="utf-8")
    assert "Verification is runtime evidence" in document


def test_every_row_names_a_consumer_or_says_it_found_none() -> None:
    """An empty cell would read as "no answer"; the generator must say which."""
    for row in collect_rows():
        assert row.consumed_by.strip(), f"{row.setting} has an empty consumer cell"
