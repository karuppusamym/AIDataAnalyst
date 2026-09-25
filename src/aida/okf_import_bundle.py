"""R11-OKF03 (design item 14C): read an edited OKF bundle as untrusted input, and say what changed.

The pure half of OKF import. `aida.okf_import` is the half that reads the database; this module
reads no database, no clock, no network and no model, and never writes a file. It does two things:

1. **`read_import_archive`** opens an uploaded ZIP in memory under hard limits and returns its
   members as text. Everything a hostile archive can do is refused *before* anything is
   decompressed: a member count read from the central directory itself (not from the parsed
   member list, which would already have cost the memory), path traversal, absolute and
   drive-lettered paths, backslashes and NUL bytes in a name, symbolic links, device files and
   FIFOs, encrypted members, unexpected compression methods, members Atlas never exports,
   duplicate names, and declared sizes over the limits. Decompression is then bounded again, per
   member and in total, against the bytes actually produced -- a header that lies about a size is
   a refusal, not a trusted number -- and a compression ratio no Atlas document reaches is refused
   as a bomb.
2. **`analyze_bundle_edits`** compares each uploaded document with the bytes Atlas itself stored
   for the publication the bundle was exported from, and reports what the editor changed as
   *edits* in the proposal families import feeds, plus a *note* for everything else: what
   is refused, what is unsupported and dropped, what is a claim that grants nothing, and what is
   tolerated without being stored. Nothing is dropped silently.

**YAML is loaded with the safe loader and nothing more permissive.** Before a mapping is
constructed at all, the event stream is walked: an anchor or alias is refused outright (the
billion-laughs expansion needs one, and Atlas never writes one), any explicit tag is refused (a
`!!python/...` tag cannot reach a constructor), and depth, node count and scalar length are
bounded. Duplicate keys are refused rather than resolved last-wins, because a hostile document
can use one to show a reviewer one value and the loader another.

**Links are counted and measured, never followed.** No code in this module or in
`aida.okf_import` opens a connection, resolves a URL or runs a process; the tests prove it
structurally (no such import) and behaviourally (a bundle full of external links is previewed
with the network patched to fail). An `executor` or `computation` field -- upstream's Attested
Computation, which is runnable code -- refuses the document.

**Verification is a claim.** A `verified`, `status` or `atlas.description.*` value in an imported
document is evidence supplied by whoever edited the file. It is reported as `CLAIM_NOT_AUTHORITY`
and grants nothing: every proposal import raises is authored by the importing principal and
decided by a different one through the ordinary review queue.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import struct
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

import yaml

from aida.okf_export import (
    _CODE_FENCE,
    _LINK,
    _RAW_MARKUP,
    _REFERENCE_LINK,
    _SAFE_SEGMENT,
    BUNDLE_ROOT,
    DESCRIPTION_APPROVED,
    DESCRIPTION_WITHHELD,
    MANIFEST_FILENAME,
    MAX_BUNDLE_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENTS,
    MAX_FRONTMATTER_BYTES,
    TYPE_COLUMN_SET,
    TYPE_CONCEPT,
    TYPE_MATERIALIZED_VIEW,
    TYPE_PACKAGE,
    TYPE_ROUTINE,
    TYPE_TABLE,
    TYPE_TOOL_VERSION,
    TYPE_VIEW,
    OkfColumnFacts,
    OkfConceptFacts,
    OkfDescription,
    OkfObjectFacts,
    OkfRoutineFacts,
    OkfSnapshot,
    _as_syntax,
    column_set_members,
    document_subjects,
)

# --- limits -----------------------------------------------------------------------------
#
# Conservative constants, derived from the export's own limits so any bundle Atlas produced can
# come back, and nothing much larger can. Constants rather than settings on purpose: a limit an
# operator can raise is a limit a hostile archive only has to wait for.

#: The uploaded archive itself, checked before a byte of it is parsed.
MAX_ARCHIVE_BYTES: Final = 32 * 1024 * 1024
#: Every document an export can hold, its manifest, and room for the directory entries a
#: re-zipping tool adds.
MAX_ARCHIVE_MEMBERS: Final = MAX_DOCUMENTS + 2_000
#: One document: the export's own per-document limit.
MAX_MEMBER_BYTES: Final = MAX_DOCUMENT_BYTES
#: The Atlas manifest lists every file and every source object, so it is allowed more.
MAX_MANIFEST_BYTES: Final = 16 * 1024 * 1024
#: Everything decompressed together: the export's bundle limit plus the manifest.
MAX_TOTAL_UNCOMPRESSED_BYTES: Final = MAX_BUNDLE_BYTES + MAX_MANIFEST_BYTES
#: Decompressed bytes per compressed byte, for a member large enough for the ratio to mean
#: something. Atlas Markdown compresses roughly 5-15x; a zip bomb compresses by thousands.
MAX_COMPRESSION_RATIO: Final = 100
RATIO_FLOOR_BYTES: Final = 64 * 1024
#: A member name, in characters. An Atlas path is under 200.
MAX_MEMBER_NAME_CHARS: Final = 1_024
#: Frontmatter: the export's own byte limit, and structural bounds no Atlas document approaches.
MAX_YAML_BYTES: Final = MAX_FRONTMATTER_BYTES
MAX_YAML_DEPTH: Final = 16
MAX_YAML_EVENTS: Final = 8_000
MAX_YAML_SCALAR_CHARS: Final = 16 * 1024
#: Links in one changed document (an index may list every document), and one link's target.
MAX_LINKS_PER_DOCUMENT: Final = MAX_DOCUMENTS
MAX_LINK_TARGET_CHARS: Final = 2_048
#: Rows of one `# Schema` table: an object splits into column sets of 100.
MAX_SCHEMA_ROWS: Final = 1_000
#: Documents that differ from their exported bytes. Each is a parse; bounded like the proposals.
MAX_CHANGED_DOCUMENTS: Final = 5_000
#: Edits one import may raise: the workbook import's own per-batch ceiling
#: (`model_import.MAX_CHANGES_PER_BATCH`), past which a review stops being one.
MAX_IMPORT_EDITS: Final = 5_000
#: Proposed text, per family: the browser worksheet's own description limit, and the ontology
#: model's own concept-description and alias limits.
MAX_DESCRIPTION_CHARS: Final = 16_000
MAX_CONCEPT_DEFINITION_CHARS: Final = 4_000
MAX_ALIAS_CHARS: Final = 200

# --- reason codes -----------------------------------------------------------------------
#
# Every refusal and every dropped change is named by one of these. They are the only thing a
# refusal writes to storage (INV-6): no member name, path or content value from the upload is
# ever persisted in a refusal.

# The whole archive is refused.
ARCHIVE_TOO_LARGE: Final = "ARCHIVE_TOO_LARGE"
ARCHIVE_NOT_A_ZIP: Final = "ARCHIVE_NOT_A_ZIP"
ARCHIVE_ZIP64_NOT_SUPPORTED: Final = "ARCHIVE_ZIP64_NOT_SUPPORTED"
ARCHIVE_TOO_MANY_MEMBERS: Final = "ARCHIVE_TOO_MANY_MEMBERS"
ARCHIVE_EXPANDS_TOO_LARGE: Final = "ARCHIVE_EXPANDS_TOO_LARGE"
ARCHIVE_COMPRESSION_RATIO: Final = "ARCHIVE_COMPRESSION_RATIO"
ARCHIVE_SIZE_MISMATCH: Final = "ARCHIVE_SIZE_MISMATCH"
ARCHIVE_CORRUPT: Final = "ARCHIVE_CORRUPT"
ARCHIVE_DUPLICATE_MEMBER: Final = "ARCHIVE_DUPLICATE_MEMBER"
PATH_TRAVERSAL: Final = "PATH_TRAVERSAL"
PATH_ABSOLUTE: Final = "PATH_ABSOLUTE"
PATH_UNSAFE: Final = "PATH_UNSAFE"
MEMBER_SYMLINK: Final = "MEMBER_SYMLINK"
MEMBER_SPECIAL_FILE: Final = "MEMBER_SPECIAL_FILE"
MEMBER_ENCRYPTED: Final = "MEMBER_ENCRYPTED"
MEMBER_COMPRESSION_UNSUPPORTED: Final = "MEMBER_COMPRESSION_UNSUPPORTED"
MEMBER_NOT_ALLOWED: Final = "MEMBER_NOT_ALLOWED"
MEMBER_TOO_LARGE: Final = "MEMBER_TOO_LARGE"
MANIFEST_MISSING: Final = "MANIFEST_MISSING"
MANIFEST_INVALID: Final = "MANIFEST_INVALID"
IMPORT_TOO_MANY_CHANGES: Final = "IMPORT_TOO_MANY_CHANGES"

# One document is refused; the rest of the bundle is still read.
ENCODING_INVALID: Final = "ENCODING_INVALID"
DOCUMENT_CONTROL_CHARACTERS: Final = "DOCUMENT_CONTROL_CHARACTERS"
FRONTMATTER_MISSING: Final = "FRONTMATTER_MISSING"
FRONTMATTER_TOO_LARGE: Final = "FRONTMATTER_TOO_LARGE"
FRONTMATTER_NOT_A_MAPPING: Final = "FRONTMATTER_NOT_A_MAPPING"
YAML_INVALID: Final = "YAML_INVALID"
YAML_ALIAS_NOT_ALLOWED: Final = "YAML_ALIAS_NOT_ALLOWED"
YAML_TAG_NOT_ALLOWED: Final = "YAML_TAG_NOT_ALLOWED"
YAML_DIRECTIVE_NOT_ALLOWED: Final = "YAML_DIRECTIVE_NOT_ALLOWED"
YAML_MULTIPLE_DOCUMENTS: Final = "YAML_MULTIPLE_DOCUMENTS"
YAML_TOO_DEEP: Final = "YAML_TOO_DEEP"
YAML_TOO_MANY_NODES: Final = "YAML_TOO_MANY_NODES"
YAML_SCALAR_TOO_LONG: Final = "YAML_SCALAR_TOO_LONG"
YAML_DUPLICATE_KEY: Final = "YAML_DUPLICATE_KEY"
YAML_KEY_NOT_STRING: Final = "YAML_KEY_NOT_STRING"
LINK_LIMIT_EXCEEDED: Final = "LINK_LIMIT_EXCEEDED"
LINK_TOO_LONG: Final = "LINK_TOO_LONG"
EXECUTABLE_FIELD_REFUSED: Final = "EXECUTABLE_FIELD_REFUSED"
IDENTITY_MISMATCH: Final = "IDENTITY_MISMATCH"
DUPLICATE_STABLE_ID: Final = "DUPLICATE_STABLE_ID"

# A change that is read and deliberately not imported.
DOCUMENT_NOT_IN_SOURCE_BUNDLE: Final = "DOCUMENT_NOT_IN_SOURCE_BUNDLE"
DOCUMENT_DERIVED: Final = "DOCUMENT_DERIVED"
FAMILY_NOT_SUPPORTED: Final = "FAMILY_NOT_SUPPORTED"
DOCUMENT_STRUCTURE_AMBIGUOUS: Final = "DOCUMENT_STRUCTURE_AMBIGUOUS"
SECTION_DERIVED: Final = "SECTION_DERIVED"
SECTION_UNKNOWN: Final = "SECTION_UNKNOWN"
FIELD_DERIVED: Final = "FIELD_DERIVED"
FIELD_REMOVAL_IGNORED: Final = "FIELD_REMOVAL_IGNORED"
UNKNOWN_FIELD_TOLERATED: Final = "UNKNOWN_FIELD_TOLERATED"
CLAIM_NOT_AUTHORITY: Final = "CLAIM_NOT_AUTHORITY"
SCHEMA_ROW_UNMATCHED: Final = "SCHEMA_ROW_UNMATCHED"
SCHEMA_ROW_MALFORMED: Final = "SCHEMA_ROW_MALFORMED"
SCHEMA_ROW_DUPLICATE: Final = "SCHEMA_ROW_DUPLICATE"
BLANK_IS_NOT_A_DELETION: Final = "BLANK_IS_NOT_A_DELETION"
BASE_TEXT_WITHHELD: Final = "BASE_TEXT_WITHHELD"
OS_METADATA_IGNORED: Final = "OS_METADATA_IGNORED"

# Proposed text Atlas would refuse to publish, or that screening refuses.
TEXT_LINK_NOT_ALLOWED: Final = "TEXT_LINK_NOT_ALLOWED"
TEXT_RAW_MARKUP_NOT_ALLOWED: Final = "TEXT_RAW_MARKUP_NOT_ALLOWED"
TEXT_CODE_FENCE_NOT_ALLOWED: Final = "TEXT_CODE_FENCE_NOT_ALLOWED"
TEXT_HEADING_NOT_ALLOWED: Final = "TEXT_HEADING_NOT_ALLOWED"
TEXT_CONTROL_CHARACTERS: Final = "TEXT_CONTROL_CHARACTERS"
TEXT_TOO_LONG: Final = "TEXT_TOO_LONG"
TEXT_SCREENING_REFUSED: Final = "TEXT_SCREENING_REFUSED"

# Decided against Atlas's current state by `aida.okf_import`, named here so one registry holds
# every code.
OKF_IMPORT_DISABLED: Final = "OKF_IMPORT_DISABLED"
MANIFEST_NOT_ATLAS: Final = "MANIFEST_NOT_ATLAS"
MANIFEST_SCOPE_MISMATCH: Final = "MANIFEST_SCOPE_MISMATCH"
BASE_PUBLICATION_NOT_RETAINED: Final = "BASE_PUBLICATION_NOT_RETAINED"
PREVIEW_STALE: Final = "PREVIEW_STALE"
IMPORT_ALREADY_PENDING: Final = "IMPORT_ALREADY_PENDING"
IMPORT_NOTHING_TO_PROPOSE: Final = "IMPORT_NOTHING_TO_PROPOSE"
SOURCE_CHANGED_SINCE_EXPORT: Final = "SOURCE_CHANGED_SINCE_EXPORT"
ALREADY_CURRENT: Final = "ALREADY_CURRENT"
TARGET_NOT_FOUND: Final = "TARGET_NOT_FOUND"
TARGET_NOT_ACTIVE: Final = "TARGET_NOT_ACTIVE"
TARGET_AMBIGUOUS: Final = "TARGET_AMBIGUOUS"
DATASOURCE_NOT_AUTHORIZED: Final = "DATASOURCE_NOT_AUTHORIZED"
MEANING_DEFINITION_INVALID: Final = "MEANING_DEFINITION_INVALID"
MEANING_MAPPING_INVALID: Final = "MEANING_MAPPING_INVALID"
# A routine's purpose, decided by the routine description workflow's own rules.
#: The routine's captured body moved since the export: the edit describes a body that is gone.
DEFINITION_CHANGED_SINCE_EXPORT: Final = "DEFINITION_CHANGED_SINCE_EXPORT"
#: A draft for this routine is already open; the workflow allows one at a time.
PROPOSAL_ALREADY_OPEN: Final = "PROPOSAL_ALREADY_OPEN"
#: A reviewer already rejected this exact text for this routine, or it was approved and withdrawn.
TEXT_PREVIOUSLY_REFUSED: Final = "TEXT_PREVIOUSLY_REFUSED"
#: The routine's catalog evidence is below the bar every routine draft must clear for review.
EVIDENCE_BELOW_REVIEW_THRESHOLD: Final = "EVIDENCE_BELOW_REVIEW_THRESHOLD"

# --- outcomes, families and edit kinds --------------------------------------------------

#: A note's outcome: what happened to something the editor changed.
OUTCOME_REFUSED: Final = "REFUSED"
OUTCOME_UNSUPPORTED: Final = "UNSUPPORTED"
OUTCOME_CLAIM: Final = "CLAIM"
OUTCOME_TOLERATED: Final = "TOLERATED"
OUTCOME_IGNORED: Final = "IGNORED"

EDIT_TABLE_PURPOSE: Final = "TABLE_PURPOSE"
EDIT_COLUMN_DESCRIPTION: Final = "COLUMN_DESCRIPTION"
EDIT_CONCEPT_DEFINITION: Final = "CONCEPT_DEFINITION"
EDIT_CONCEPT_ALIASES: Final = "CONCEPT_ALIASES"
EDIT_ROUTINE_PURPOSE: Final = "ROUTINE_PURPOSE"

#: The existing proposal families an edit lands in. Nothing else is written.
FAMILY_ASSET_DOCUMENTATION: Final = "ASSET_DOCUMENTATION"
FAMILY_COLUMN_DESCRIPTION: Final = "COLUMN_DESCRIPTION"
FAMILY_ONTOLOGY_MEANING: Final = "ONTOLOGY_MEANING"
#: R11-FP08's routine description drafts: the description family's routine member.
FAMILY_ROUTINE_DESCRIPTION: Final = "ROUTINE_DESCRIPTION"

EDIT_FAMILIES: Final[Mapping[str, str]] = {
    EDIT_TABLE_PURPOSE: FAMILY_ASSET_DOCUMENTATION,
    EDIT_COLUMN_DESCRIPTION: FAMILY_COLUMN_DESCRIPTION,
    EDIT_CONCEPT_DEFINITION: FAMILY_ONTOLOGY_MEANING,
    EDIT_CONCEPT_ALIASES: FAMILY_ONTOLOGY_MEANING,
    EDIT_ROUTINE_PURPOSE: FAMILY_ROUTINE_DESCRIPTION,
}

_OBJECT_TYPES: Final = frozenset({TYPE_TABLE, TYPE_VIEW, TYPE_MATERIALIZED_VIEW})

#: The round-trip contract, as code: which document type and section is imported into which
#: family. `Docs/90-reference/okf-import-contract.md` states it for people, and
#: `tests/test_okf_import.py` fails if the two disagree.
SUPPORTED_SECTIONS: Final[Mapping[tuple[str, str], str]] = {
    (TYPE_TABLE, "# Purpose"): FAMILY_ASSET_DOCUMENTATION,
    (TYPE_VIEW, "# Purpose"): FAMILY_ASSET_DOCUMENTATION,
    (TYPE_MATERIALIZED_VIEW, "# Purpose"): FAMILY_ASSET_DOCUMENTATION,
    (TYPE_TABLE, "# Schema"): FAMILY_COLUMN_DESCRIPTION,
    (TYPE_VIEW, "# Schema"): FAMILY_COLUMN_DESCRIPTION,
    (TYPE_MATERIALIZED_VIEW, "# Schema"): FAMILY_COLUMN_DESCRIPTION,
    (TYPE_COLUMN_SET, "# Schema"): FAMILY_COLUMN_DESCRIPTION,
    (TYPE_CONCEPT, "# Definition"): FAMILY_ONTOLOGY_MEANING,
    (TYPE_CONCEPT, "# Also called"): FAMILY_ONTOLOGY_MEANING,
    # A routine's purpose is a routine description draft, decided by that workflow's own rules:
    # the edit carries the description version and the captured-definition version the export
    # showed, which are exactly the two things the workflow's approval re-checks.
    (TYPE_ROUTINE, "# Purpose"): FAMILY_ROUTINE_DESCRIPTION,
}
#: Document types Atlas exports and deliberately does not import. Each reason is why no
#: existing proposal family could carry the edit safely -- not a gap waiting on parsing.
UNSUPPORTED_TYPES: Final[Mapping[str, str]] = {
    TYPE_PACKAGE: (
        "a package has no description family: the routine description workflow refuses a "
        "package by name (PACKAGE_NOT_DESCRIBABLE) and package documentation waits on R11-FP03, "
        "so an edit would need a new store and a new review type"
    ),
    TYPE_TOOL_VERSION: (
        "a tool version is a versioned, reviewed executable interface; its text changes only by "
        "authoring a new version under its own review, which a file edit cannot stand in for"
    ),
}

#: Frontmatter keys that name the document's subject. A change is a different document.
_SUBJECT_KEY_FIELDS: Final = ("atlas.object.key", "atlas.concept.key", "atlas.tool.key")
_IDENTITY_FIELDS: Final = frozenset(
    {"type", "resource", "atlas.part_of.key", *_SUBJECT_KEY_FIELDS}
)
#: Upstream's type for runnable embedded code (spec section 10). Atlas never exports one.
ATTESTED_COMPUTATION: Final = "Attested Computation"
#: Frontmatter prefixes that assert approval or verification: claims, never authority.
_CLAIM_PREFIXES: Final = ("verified", "status", "atlas.description", "atlas.statements")
#: Upstream's Attested Computation carries runnable code in these. Refused, never read.
_EXECUTABLE_FIELDS: Final = frozenset({"computation", "executor"})

#: The four sentences `okf_export._purpose_section` writes when there is no approved text,
#: recognized so an untouched placeholder is never proposed as a description.
_PURPOSE_PLACEHOLDER: Final = re.compile(r"^(Not established\.|Withheld\.)", re.IGNORECASE)
_SCHEMA_PLACEHOLDERS: Final = frozenset({"_not established_", "_withheld by export screening_"})
_FOOTNOTE_MARKER: Final = "[^approved-description]"

#: Control characters, zero-width characters and bidirectional overrides: never in proposed text.
_FORBIDDEN_TEXT_CHARACTERS: Final = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
#: Control characters that make a whole document unreadable as Markdown (tab and line feed
#: allowed; carriage returns are normalized before this runs).
_FORBIDDEN_DOCUMENT_CHARACTERS: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
_BARE_URL: Final = re.compile(r"(?i)\b(?:https?://|ftp://|www\.|mailto:|file:)")
_HEADING: Final = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)|^\s{0,3}(?:=+|-{3,})\s*$", re.MULTILINE)
_ESCAPED_PUNCTUATION: Final = re.compile(r"\\([!-/:-@\[-`{-~])")
_LEAF: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_DRIVE: Final = re.compile(r"^[A-Za-z]:")
_OS_METADATA_LEAVES: Final = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


class OkfImportRefused(Exception):
    """The whole archive is refused. Carries a reason code and never the offending value."""

    def __init__(self, reason_code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code


class _DocumentRefused(Exception):
    """One document is refused; the rest of the bundle is still read."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


# --- the archive ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OkfImportDocument:
    """One uploaded bundle document: its bundle-relative path and its text.

    `text` is `None` when the bytes are not UTF-8; `refused` then names why. Line endings are
    normalized to `\\n` and a leading byte-order mark is dropped, so an editor that rewrote them
    has not "changed" every line.
    """

    path: str
    text: str | None
    sha256: str
    refused: str | None = None


@dataclass(frozen=True, slots=True)
class OkfImportArchive:
    sha256: str
    manifest: dict[str, Any]
    documents: tuple[OkfImportDocument, ...]
    #: Operating-system metadata a re-zipping tool adds (`__MACOSX/`, `.DS_Store`), skipped
    #: unread and counted, never silently.
    ignored_members: int = 0


_EOCD: Final = b"PK\x05\x06"
_EOCD_STRUCT: Final = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_CENTRAL_HEADER: Final = b"PK\x01\x02"
_CENTRAL_STRUCT: Final = struct.Struct("<4s6H3L5H2L")


def _end_of_central_directory(content: bytes) -> tuple[int, int, int]:
    """(declared members, central directory size, offset), read without trusting `zipfile`.

    `zipfile.ZipFile` builds one object per central-directory entry before any member can be
    inspected, so a 32 MiB upload of tiny entries is hundreds of megabytes of objects. Reading
    the end record here, and walking the directory below, bounds that before it is built.
    """
    tail_start = max(0, len(content) - (0xFFFF + _EOCD_STRUCT.size))
    position = content.rfind(_EOCD, tail_start)
    while position >= 0:
        if position + _EOCD_STRUCT.size <= len(content):
            fields = _EOCD_STRUCT.unpack_from(content, position)
            total, size, offset, comment = fields[4], fields[5], fields[6], fields[7]
            if position + _EOCD_STRUCT.size + comment == len(content):
                if (
                    total == 0xFFFF
                    or size == 0xFFFFFFFF
                    or offset == 0xFFFFFFFF
                    or (position >= 20 and content[position - 20 : position - 16] == _ZIP64_LOCATOR)
                ):
                    raise OkfImportRefused(
                        ARCHIVE_ZIP64_NOT_SUPPORTED,
                        "the archive uses ZIP64 records, which no Atlas bundle needs",
                    )
                return int(total), int(size), int(offset)
        position = content.rfind(_EOCD, tail_start, position)
    raise OkfImportRefused(ARCHIVE_NOT_A_ZIP, "the upload is not a ZIP archive")


def _count_central_entries(content: bytes, size: int, offset: int) -> int:
    """Walk the central directory by its fixed headers, stopping past the member limit."""
    if offset + size > len(content):
        raise OkfImportRefused(ARCHIVE_CORRUPT, "the archive's central directory is truncated")
    position, end, count = offset, offset + size, 0
    while position < end:
        if content[position : position + 4] != _CENTRAL_HEADER or (
            position + _CENTRAL_STRUCT.size > end
        ):
            raise OkfImportRefused(ARCHIVE_CORRUPT, "the archive's central directory is corrupt")
        fields = _CENTRAL_STRUCT.unpack_from(content, position)
        name_length, extra_length, comment_length = fields[10], fields[11], fields[12]
        position += _CENTRAL_STRUCT.size + name_length + extra_length + comment_length
        count += 1
        if count > MAX_ARCHIVE_MEMBERS:
            raise OkfImportRefused(
                ARCHIVE_TOO_MANY_MEMBERS,
                f"the archive has more than {MAX_ARCHIVE_MEMBERS} members",
            )
    return count


def _check_member_name(raw: str) -> None:
    """Refuse a member name that could point anywhere but inside the bundle.

    Read from `ZipInfo.orig_filename`, the name as the archive spelled it: `zipfile` rewrites a
    backslash to `/` on Windows and cuts a name at its first NUL, which would hide both.
    """
    if not raw or len(raw) > MAX_MEMBER_NAME_CHARS:
        raise OkfImportRefused(PATH_UNSAFE, "an archive member has an unusable name")
    if "\\" in raw or "\x00" in raw or any(ord(char) < 0x20 for char in raw):
        raise OkfImportRefused(PATH_UNSAFE, "an archive member name holds a backslash or control")
    if raw.startswith("/") or _DRIVE.match(raw):
        raise OkfImportRefused(PATH_ABSOLUTE, "an archive member has an absolute path")
    segments = (raw[:-1] if raw.endswith("/") else raw).split("/")
    if ".." in segments:
        raise OkfImportRefused(PATH_TRAVERSAL, "an archive member path climbs out of the bundle")
    if any(segment in ("", ".") for segment in segments):
        raise OkfImportRefused(PATH_UNSAFE, "an archive member path has an empty segment")


def _is_os_metadata(name: str) -> bool:
    return name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1] in _OS_METADATA_LEAVES


def _is_bundle_directory(name: str) -> bool:
    segments = name[:-1].split("/")
    return segments[0] == BUNDLE_ROOT and all(_SAFE_SEGMENT.match(item) for item in segments[1:])


def _bundle_path(name: str) -> str | None:
    """The bundle-relative path of an allowed document member, or `None`."""
    prefix = BUNDLE_ROOT + "/"
    if not name.startswith(prefix):
        return None
    segments = name[len(prefix) :].split("/")
    if not segments or not _LEAF.match(segments[-1]):
        return None
    if not all(_SAFE_SEGMENT.match(segment) for segment in segments[:-1]):
        return None
    return "/".join(segments)


def _normalized(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_manifest(data: bytes) -> dict[str, Any]:
    def refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key, value in pairs:
            if key in mapping:
                raise ValueError("duplicate key")
            mapping[key] = value
        return mapping

    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=refuse_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise OkfImportRefused(MANIFEST_INVALID, "the Atlas manifest is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise OkfImportRefused(MANIFEST_INVALID, "the Atlas manifest is not a JSON object")
    return parsed


def read_import_archive(content: bytes) -> OkfImportArchive:
    """Open an uploaded bundle archive under every limit above, or refuse it whole.

    Header checks run over every member before any member is decompressed, so a hostile entry
    anywhere refuses the archive without the rest having cost anything.
    """
    if len(content) > MAX_ARCHIVE_BYTES:
        raise OkfImportRefused(
            ARCHIVE_TOO_LARGE,
            f"the archive exceeds the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB import limit",
            status_code=413,
        )
    declared, size, offset = _end_of_central_directory(content)
    if declared > MAX_ARCHIVE_MEMBERS:
        raise OkfImportRefused(
            ARCHIVE_TOO_MANY_MEMBERS, f"the archive has more than {MAX_ARCHIVE_MEMBERS} members"
        )
    counted = _count_central_entries(content, size, offset)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError) as error:
        raise OkfImportRefused(ARCHIVE_NOT_A_ZIP, "the upload is not a readable ZIP") from error
    with archive:
        infos = archive.infolist()
        if len(infos) != counted:
            raise OkfImportRefused(ARCHIVE_CORRUPT, "the archive's member count is inconsistent")
        wanted, ignored = _admit_members(infos)
        manifest, documents = _read_members(archive, wanted)
    return OkfImportArchive(
        sha256=hashlib.sha256(content).hexdigest(),
        manifest=manifest,
        documents=tuple(sorted(documents, key=lambda item: item.path)),
        ignored_members=ignored,
    )


def _admit_members(
    infos: Sequence[zipfile.ZipInfo],
) -> tuple[list[tuple[zipfile.ZipInfo, str | None, int]], int]:
    """Every header check, over every member, before anything is decompressed.

    Returns the members to read -- (entry, bundle path or `None` for the manifest, byte limit)
    -- and how many operating-system metadata entries were skipped.
    """
    seen: set[str] = set()
    wanted: list[tuple[zipfile.ZipInfo, str | None, int]] = []
    ignored = 0
    declared_total = 0
    for info in infos:
        raw = info.orig_filename
        _check_member_name(raw)
        folded = raw.casefold()
        if folded in seen:
            raise OkfImportRefused(
                ARCHIVE_DUPLICATE_MEMBER, "two archive members have the same name"
            )
        seen.add(folded)
        kind = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
        if kind == stat.S_IFLNK:
            raise OkfImportRefused(MEMBER_SYMLINK, "the archive holds a symbolic link")
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise OkfImportRefused(
                MEMBER_SPECIAL_FILE, "the archive holds a device, FIFO or socket entry"
            )
        if info.flag_bits & 0x1:
            raise OkfImportRefused(MEMBER_ENCRYPTED, "the archive holds an encrypted member")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise OkfImportRefused(
                MEMBER_COMPRESSION_UNSUPPORTED,
                "an archive member uses a compression method other than stored or deflate",
            )
        if _is_os_metadata(raw):
            ignored += 1
            continue
        if raw.endswith("/") or kind == stat.S_IFDIR:
            if not raw.endswith("/") or info.file_size or not _is_bundle_directory(raw):
                raise OkfImportRefused(MEMBER_NOT_ALLOWED, "the archive holds an unexpected entry")
            continue
        path: str | None
        if raw == MANIFEST_FILENAME:
            path, limit = None, MAX_MANIFEST_BYTES
        else:
            path = _bundle_path(raw)
            if path is None:
                raise OkfImportRefused(
                    MEMBER_NOT_ALLOWED,
                    "the archive holds a member an Atlas bundle never contains; only "
                    f"{MANIFEST_FILENAME} and Markdown documents under {BUNDLE_ROOT}/ are read",
                )
            limit = MAX_MEMBER_BYTES
        if info.file_size > limit:
            raise OkfImportRefused(MEMBER_TOO_LARGE, "an archive member is over its size limit")
        declared_total += info.file_size
        if declared_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise OkfImportRefused(
                ARCHIVE_EXPANDS_TOO_LARGE, "the archive expands past the import limit"
            )
        wanted.append((info, path, limit))
    return wanted, ignored


_READ_ERRORS: Final = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)


def _read_members(
    archive: zipfile.ZipFile, wanted: Sequence[tuple[zipfile.ZipInfo, str | None, int]]
) -> tuple[dict[str, Any], list[OkfImportDocument]]:
    """Decompress the admitted members, bounded against the bytes actually produced."""
    manifest: dict[str, Any] | None = None
    documents: list[OkfImportDocument] = []
    actual_total = 0
    for info, path, limit in wanted:
        try:
            with archive.open(info) as handle:
                data = handle.read(limit + 1)
        except _READ_ERRORS as error:
            raise OkfImportRefused(ARCHIVE_CORRUPT, "an archive member cannot be read") from error
        if len(data) > limit:
            raise OkfImportRefused(MEMBER_TOO_LARGE, "an archive member is over its size limit")
        if len(data) != info.file_size:
            raise OkfImportRefused(
                ARCHIVE_SIZE_MISMATCH, "an archive member's size does not match its header"
            )
        actual_total += len(data)
        if actual_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise OkfImportRefused(
                ARCHIVE_EXPANDS_TOO_LARGE, "the archive expands past the import limit"
            )
        if len(data) >= RATIO_FLOOR_BYTES and len(data) > MAX_COMPRESSION_RATIO * max(
            info.compress_size, 1
        ):
            raise OkfImportRefused(
                ARCHIVE_COMPRESSION_RATIO,
                "an archive member compresses far more than any Atlas document; refused as a bomb",
            )
        if path is None:
            manifest = _load_manifest(data)
            continue
        documents.append(_document(path, data))
    if manifest is None:
        raise OkfImportRefused(
            MANIFEST_MISSING,
            f"the archive has no {MANIFEST_FILENAME}; import reads only a bundle Atlas exported, "
            "because the manifest is what names the publication it was exported from",
        )
    return manifest, documents


def _document(path: str, data: bytes) -> OkfImportDocument:
    try:
        text = _normalized(data.decode("utf-8"))
    except UnicodeDecodeError:
        return OkfImportDocument(
            path=path,
            text=None,
            sha256=hashlib.sha256(data).hexdigest(),
            refused=ENCODING_INVALID,
        )
    refused = DOCUMENT_CONTROL_CHARACTERS if _FORBIDDEN_DOCUMENT_CHARACTERS.search(text) else None
    return OkfImportDocument(path=path, text=text, sha256=_digest(text), refused=refused)


# --- frontmatter ------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """The safe loader, refusing duplicate and non-string keys instead of resolving them."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _value in node.value:
            key = self.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
            if not isinstance(key, str):
                raise _DocumentRefused(YAML_KEY_NOT_STRING)
            if key in seen:
                raise _DocumentRefused(YAML_DUPLICATE_KEY)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _check_yaml_events(raw: str) -> None:
    """Walk the event stream before anything is constructed."""
    events = 0
    depth = 0
    documents = 0
    for event in yaml.parse(raw, Loader=yaml.SafeLoader):
        events += 1
        if events > MAX_YAML_EVENTS:
            raise _DocumentRefused(YAML_TOO_MANY_NODES)
        if isinstance(event, yaml.AliasEvent):
            raise _DocumentRefused(YAML_ALIAS_NOT_ALLOWED)
        if isinstance(event, yaml.DocumentStartEvent):
            documents += 1
            if documents > 1:
                raise _DocumentRefused(YAML_MULTIPLE_DOCUMENTS)
            if event.version is not None or event.tags:
                raise _DocumentRefused(YAML_DIRECTIVE_NOT_ALLOWED)
        if isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            raise _DocumentRefused(YAML_ALIAS_NOT_ALLOWED)
        if isinstance(event, yaml.ScalarEvent | yaml.CollectionStartEvent) and (
            event.tag is not None
        ):
            raise _DocumentRefused(YAML_TAG_NOT_ALLOWED)
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                raise _DocumentRefused(YAML_TOO_DEEP)
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
        elif isinstance(event, yaml.ScalarEvent) and len(event.value) > MAX_YAML_SCALAR_CHARS:
            raise _DocumentRefused(YAML_SCALAR_TOO_LONG)


def _plain(value: Any) -> Any:
    """JSON-shaped values only: a timestamp becomes its ISO text."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def load_frontmatter(raw: str) -> dict[str, Any]:
    """An imported document's frontmatter, or a `_DocumentRefused` naming the rule it broke."""
    if len(raw.encode("utf-8")) > MAX_YAML_BYTES:
        raise _DocumentRefused(FRONTMATTER_TOO_LARGE)
    try:
        _check_yaml_events(raw)
        parsed = yaml.load(raw, Loader=_StrictLoader)  # noqa: S506 -- a SafeLoader subclass
    except _DocumentRefused:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, TypeError) as error:
        raise _DocumentRefused(YAML_INVALID) from error
    if not isinstance(parsed, dict):
        raise _DocumentRefused(FRONTMATTER_NOT_A_MAPPING)
    plain = _plain(parsed)
    assert isinstance(plain, dict)
    return plain


_FRONTMATTER_BLOCK: Final = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def split_document(text: str) -> tuple[str | None, str]:
    match = _FRONTMATTER_BLOCK.match(text)
    if match is None:
        return None, text
    return match.group(1), text[match.end() :]


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """`atlas.object.key`-style paths to leaf values; a list is one leaf."""
    flat: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}{key}"
        if isinstance(item, dict) and item:
            flat.update(_flatten(item, path + "."))
        else:
            flat[path] = item
    return flat


# --- text rules for anything proposed ---------------------------------------------------


def text_refusal(text: str, *, limit: int) -> str | None:
    """Why Atlas would refuse to publish `text` as approved prose, or `None`.

    The publish policy's own patterns (`okf_export.validate_atlas_publish_policy`), applied to
    the text before it can become a proposal: an approved description is written verbatim into
    every later export, so a link, raw markup or a code fence in it would make the whole
    product's bundle unpublishable. Stricter than export in two ways, both for untrusted input:
    a bare URL is refused too (a GFM renderer autolinks it), and so is a heading, which would
    split the document an export writes it into.
    """
    if len(text) > limit:
        return TEXT_TOO_LONG
    if _FORBIDDEN_TEXT_CHARACTERS.search(text):
        return TEXT_CONTROL_CHARACTERS
    if _CODE_FENCE.search(text):
        return TEXT_CODE_FENCE_NOT_ALLOWED
    syntax = _as_syntax(text)
    if _RAW_MARKUP.search(syntax):
        return TEXT_RAW_MARKUP_NOT_ALLOWED
    if _LINK.search(syntax) or _REFERENCE_LINK.search(syntax) or _BARE_URL.search(text):
        return TEXT_LINK_NOT_ALLOWED
    if _HEADING.search(text):
        return TEXT_HEADING_NOT_ALLOWED
    return None


def _collapsed(text: str) -> str:
    return " ".join(text.split())


# --- analysis ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OkfImportEdit:
    """One change an editor made that an existing proposal family can express."""

    path: str
    kind: str
    #: The object or concept key the export derived the document from.
    subject_key: str
    field: str
    proposed: str
    column_name: str | None = None
    #: What the export showed, and the approved version it showed. `base_version` is the
    #: version a proposal expects to replace: a different current version is a conflict.
    base_text: str | None = None
    base_version: int | None = None
    #: Aliases added to a concept.
    added_aliases: tuple[str, ...] = ()
    #: R11-OKF03, decided 2026-09-25: aliases the editor struck from a list that still has
    #: entries, spelled as Atlas holds them. They become a removal in the same pending ontology
    #: version a reviewer approves; an emptied list is blank and removes nothing.
    removed_aliases: tuple[str, ...] = ()
    #: A routine's captured-definition version as the export showed it (`None`: none captured).
    #: Read from Atlas's stored snapshot, never from the upload, so an editor cannot vouch for
    #: a body they never saw by rewriting a frontmatter number.
    base_definition_version: int | None = None

    @property
    def family(self) -> str:
        return EDIT_FAMILIES[self.kind]


@dataclass(frozen=True, slots=True)
class OkfImportNote:
    """Something the editor changed that is not proposed, and why.

    `detail` is for the importing user's preview only; it is never persisted.
    """

    path: str | None
    outcome: str
    reason_code: str
    field: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class OkfImportAnalysis:
    documents: int
    unchanged: int
    changed: int
    edits: tuple[OkfImportEdit, ...]
    notes: tuple[OkfImportNote, ...]


@dataclass(frozen=True, slots=True)
class _Base:
    """What the export derived each stored document from, keyed by its path."""

    subjects: dict[str, str]
    objects: dict[str, OkfObjectFacts]
    concepts: dict[str, OkfConceptFacts]
    routines: dict[str, OkfRoutineFacts]
    #: Column-set path -> its object and, in row order, its columns. Empty columns when the
    #: set's names cannot be matched to exactly one column each.
    column_sets: dict[str, tuple[OkfObjectFacts, tuple[OkfColumnFacts, ...]]]
    wide_objects: frozenset[str]


def _base(snapshot: OkfSnapshot) -> _Base:
    objects = {obj.key: obj for obj in snapshot.objects}
    column_sets: dict[str, tuple[OkfObjectFacts, tuple[OkfColumnFacts, ...]]] = {}
    members_by_object = column_set_members(snapshot)
    for key, sets in members_by_object.items():
        obj = objects[key]
        by_name: dict[str, list[OkfColumnFacts]] = {}
        for column in obj.columns:
            by_name.setdefault(column.name, []).append(column)
        for path, names in sets:
            members = [by_name[name][0] for name in names if len(by_name.get(name, [])) == 1]
            column_sets[path] = (obj, tuple(members) if len(members) == len(names) else ())
    return _Base(
        subjects=document_subjects(snapshot),
        objects=objects,
        concepts={concept.key: concept for concept in snapshot.concepts},
        routines={routine.key: routine for routine in snapshot.routines},
        column_sets=column_sets,
        wide_objects=frozenset(members_by_object),
    )


def _sections(body: str) -> list[tuple[str, str]]:
    """The body cut at every level-one heading: (heading, text) in order; "" is the preamble."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in body.split("\n"):
        if line.startswith("# "):
            sections.append((line.rstrip(), []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(lines).strip()) for heading, lines in sections]


def _section_map(body: str) -> tuple[dict[str, str], set[str]]:
    """Heading -> text for headings that occur once, and the set that repeat (ambiguous)."""
    counts: dict[str, int] = {}
    texts: dict[str, str] = {}
    for heading, text in _sections(body):
        counts[heading] = counts.get(heading, 0) + 1
        texts[heading] = text
    repeated = {heading for heading, count in counts.items() if count > 1}
    return {heading: text for heading, text in texts.items() if heading not in repeated}, repeated


def _purpose_text(section: str) -> str:
    text = section.strip()
    if text.endswith(_FOOTNOTE_MARKER):
        text = text[: -len(_FOOTNOTE_MARKER)].rstrip()
    return text


def _after_the_exporters_placeholder(base_text: str, upload_text: str) -> str:
    """What an editor added *after* the exporter's own "Not established." sentence.

    When Atlas holds no approved text, the exported purpose is one sentence saying so
    (`okf_export._purpose_section`). An editor who types their description under that
    sentence, rather than over it, would otherwise propose a description that opens with "Not
    established. No approved description of this table exists." -- Atlas's statement about
    itself, published as the object's meaning. Only what they added is theirs to propose; the
    sentence is dropped, matched whitespace-insensitively (an editor may re-wrap it), and an
    upload that does not begin with it is left exactly as written. A base that is not a
    placeholder is never touched: approved text an editor extends is a normal edit.
    """
    if not _PURPOSE_PLACEHOLDER.match(base_text):
        return upload_text
    lead = re.compile(r"\s+".join(re.escape(word) for word in base_text.split()))
    match = lead.match(upload_text)
    if match is None:
        return upload_text
    return upload_text[match.end() :].strip()


def _split_cells(line: str) -> list[str]:
    """One GFM table row's cells: split on `|` not escaped by a backslash."""
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            current.append(line[index : index + 2])
            index += 2
            continue
        if char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current))
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def _schema_rows(section: str) -> list[list[str]] | None:
    """The data rows of a `# Schema` table, or `None` when the section has no table."""
    lines = [line for line in section.split("\n") if line.startswith("|")]
    if len(lines) < 2:
        return None
    return [_split_cells(line) for line in lines[2:]]


def _cell_description(cell: str) -> str | None:
    """A Description cell as the text it asserts, or `None` for a placeholder or a blank."""
    text = _collapsed(cell.replace("\\|", "|"))
    if not text or text in _SCHEMA_PLACEHOLDERS:
        return None
    return text


def _alias_items(section: str) -> list[str]:
    items: list[str] = []
    for line in section.split("\n"):
        if line.startswith("* "):
            alias = _collapsed(_ESCAPED_PUNCTUATION.sub(r"\1", line[2:]))
            if alias:
                items.append(alias)
    return items


class _Document:
    """The analysis of one changed document, accumulating edits and notes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.edits: list[OkfImportEdit] = []
        self.notes: list[OkfImportNote] = []
        self.identity: str | None = None

    def note(
        self, outcome: str, reason_code: str, field: str | None = None, detail: str | None = None
    ) -> None:
        self.notes.append(
            OkfImportNote(
                path=self.path,
                outcome=outcome,
                reason_code=reason_code,
                field=field[:200] if field else None,
                detail=detail[:200] if detail else None,
            )
        )


def _compare_frontmatter(
    document: _Document, base: Mapping[str, Any], upload: Mapping[str, Any]
) -> None:
    """Classify every frontmatter difference. Identity changes refuse the document."""
    base_flat, upload_flat = _flatten(base), _flatten(upload)
    for key in sorted(set(base_flat) | set(upload_flat)):
        if key in base_flat and key in upload_flat and base_flat[key] == upload_flat[key]:
            continue
        if key in _IDENTITY_FIELDS:
            raise _DocumentRefused(IDENTITY_MISMATCH)
        if key.startswith(_CLAIM_PREFIXES):
            if key in upload_flat:
                document.note(
                    OUTCOME_CLAIM,
                    CLAIM_NOT_AUTHORITY,
                    key,
                    json.dumps(upload_flat[key], sort_keys=True, default=str),
                )
            continue
        if key not in upload_flat:
            document.note(OUTCOME_IGNORED, FIELD_REMOVAL_IGNORED, key)
        elif key not in base_flat:
            document.note(OUTCOME_TOLERATED, UNKNOWN_FIELD_TOLERATED, key)
        else:
            document.note(OUTCOME_IGNORED, FIELD_DERIVED, key)


def _object_edits(
    document: _Document,
    obj: OkfObjectFacts,
    columns: Sequence[OkfColumnFacts],
    base_sections: Mapping[str, str],
    upload_sections: Mapping[str, str],
    *,
    purpose: bool,
    schema: bool,
) -> set[str]:
    """Edits to an object's purpose and its schema rows' descriptions. Returns handled headings."""
    handled: set[str] = set()
    if purpose and "# Purpose" in base_sections and "# Purpose" in upload_sections:
        handled.add("# Purpose")
        _purpose_edit(document, obj, base_sections["# Purpose"], upload_sections["# Purpose"])
    if schema and columns and "# Schema" in base_sections and "# Schema" in upload_sections:
        handled.add("# Schema")
        _schema_edits(
            document, obj, columns, base_sections["# Schema"], upload_sections["# Schema"]
        )
    return handled


def _purpose_edit(
    document: _Document,
    obj: OkfObjectFacts | OkfRoutineFacts,
    base: str,
    upload: str,
    *,
    kind: str = EDIT_TABLE_PURPOSE,
) -> None:
    """An edit to `# Purpose`, for a table or view and -- R11-OKF03 -- for a routine.

    One rule for both, because the exporter writes both sections with one renderer
    (`okf_export._purpose_section`): the same footnote, the same placeholders, the same
    withheld sentence. A routine edit also carries the captured-definition version the export
    showed, so the routine workflow's body check has an export-time baseline to hold it to.
    """
    base_text, upload_text = _purpose_text(base), _purpose_text(upload)
    if _collapsed(base_text) == _collapsed(upload_text):
        return
    upload_text = _after_the_exporters_placeholder(base_text, upload_text)
    if _collapsed(base_text) == _collapsed(upload_text):
        return
    description: OkfDescription = obj.description
    approved = description.state == DESCRIPTION_APPROVED and description.text
    if approved and _collapsed(base_text) != _collapsed(description.text or ""):
        # The stored document does not split where the renderer wrote it -- approved text that
        # itself holds a heading. Proposing from it could propose half a description.
        document.note(OUTCOME_UNSUPPORTED, DOCUMENT_STRUCTURE_AMBIGUOUS, "# Purpose")
        return
    if not upload_text:
        document.note(OUTCOME_IGNORED, BLANK_IS_NOT_A_DELETION, "# Purpose")
        return
    if description.state == DESCRIPTION_WITHHELD:
        document.note(OUTCOME_UNSUPPORTED, BASE_TEXT_WITHHELD, "# Purpose")
        return
    definition = obj.definition if isinstance(obj, OkfRoutineFacts) else None
    document.edits.append(
        OkfImportEdit(
            path=document.path,
            kind=kind,
            subject_key=obj.key,
            field="purpose",
            proposed=upload_text,
            base_text=description.text if approved else None,
            base_version=description.version if approved else None,
            base_definition_version=definition.capture_version if definition else None,
        )
    )


def _schema_edits(
    document: _Document,
    obj: OkfObjectFacts,
    columns: Sequence[OkfColumnFacts],
    base: str,
    upload: str,
) -> None:
    base_rows = _schema_rows(base)
    ordered = sorted(columns, key=lambda item: (item.ordinal, item.name))
    if base_rows is None or len(base_rows) != len(ordered):
        document.note(OUTCOME_UNSUPPORTED, DOCUMENT_STRUCTURE_AMBIGUOUS, "# Schema")
        return
    by_cell: dict[str, tuple[OkfColumnFacts, list[str]]] = {}
    repeated: set[str] = set()
    for row, column in zip(base_rows, ordered, strict=True):
        if len(row) != 5:
            document.note(OUTCOME_UNSUPPORTED, DOCUMENT_STRUCTURE_AMBIGUOUS, "# Schema")
            return
        if row[0] in by_cell:
            repeated.add(row[0])
        by_cell[row[0]] = (column, row)
    upload_rows = _schema_rows(upload)
    if upload_rows is None:
        document.note(OUTCOME_IGNORED, FIELD_REMOVAL_IGNORED, "# Schema")
        return
    if len(upload_rows) > MAX_SCHEMA_ROWS:
        raise _DocumentRefused(SCHEMA_ROW_MALFORMED)
    seen: dict[str, int] = {}
    for row in upload_rows:
        if row and row[0] in by_cell:
            seen[row[0]] = seen.get(row[0], 0) + 1
    for number, row in enumerate(upload_rows, start=1):
        if len(row) != 5:
            document.note(OUTCOME_UNSUPPORTED, SCHEMA_ROW_MALFORMED, f"# Schema row {number}")
            continue
        match = by_cell.get(row[0])
        if match is None:
            document.note(OUTCOME_UNSUPPORTED, SCHEMA_ROW_UNMATCHED, f"# Schema row {number}")
            continue
        column, base_row = match
        field = f"column:{column.name}"
        if row[0] in repeated or seen.get(row[0], 0) > 1:
            document.note(OUTCOME_UNSUPPORTED, SCHEMA_ROW_DUPLICATE, field)
            continue
        if row[1:4] != base_row[1:4]:
            document.note(OUTCOME_IGNORED, SECTION_DERIVED, f"{field}:type-nullable-classification")
        proposed = _cell_description(row[4])
        shown = _cell_description(base_row[4])
        if proposed == shown or _collapsed(row[4]) == _collapsed(base_row[4]):
            continue
        if proposed is None:
            document.note(OUTCOME_IGNORED, BLANK_IS_NOT_A_DELETION, field)
            continue
        description = column.description
        if description.state == DESCRIPTION_WITHHELD:
            document.note(OUTCOME_UNSUPPORTED, BASE_TEXT_WITHHELD, field)
            continue
        approved = description.state == DESCRIPTION_APPROVED and description.text
        document.edits.append(
            OkfImportEdit(
                path=document.path,
                kind=EDIT_COLUMN_DESCRIPTION,
                subject_key=obj.key,
                field=field,
                proposed=proposed,
                column_name=column.name,
                base_text=description.text if approved else None,
                base_version=description.version if approved else None,
            )
        )


def _concept_edits(
    document: _Document,
    concept: OkfConceptFacts,
    base_sections: Mapping[str, str],
    upload_sections: Mapping[str, str],
) -> set[str]:
    handled: set[str] = set()
    if "# Definition" in base_sections and "# Definition" in upload_sections:
        handled.add("# Definition")
        base_text = base_sections["# Definition"].strip()
        upload_text = upload_sections["# Definition"].strip()
        if _collapsed(base_text) != _collapsed(upload_text):
            if concept.definition and _collapsed(base_text) != _collapsed(concept.definition):
                document.note(OUTCOME_UNSUPPORTED, DOCUMENT_STRUCTURE_AMBIGUOUS, "# Definition")
            elif not upload_text:
                document.note(OUTCOME_IGNORED, BLANK_IS_NOT_A_DELETION, "# Definition")
            elif concept.withheld_reason_codes and not concept.definition:
                document.note(OUTCOME_UNSUPPORTED, BASE_TEXT_WITHHELD, "# Definition")
            else:
                document.edits.append(
                    OkfImportEdit(
                        path=document.path,
                        kind=EDIT_CONCEPT_DEFINITION,
                        subject_key=concept.key,
                        field="definition",
                        proposed=upload_text,
                        base_text=concept.definition,
                        base_version=concept.ontology_version,
                    )
                )
    handled.add("# Also called")
    base_aliases = {_collapsed(alias).casefold() for alias in concept.aliases}
    upload_aliases = (
        _alias_items(upload_sections["# Also called"])
        if "# Also called" in upload_sections
        else None
    )
    if upload_aliases is not None:
        added: list[str] = []
        for alias in upload_aliases:
            if alias.casefold() not in base_aliases and alias.casefold() not in {
                item.casefold() for item in added
            }:
                added.append(alias)
        kept = {alias.casefold() for alias in upload_aliases}
        removed: tuple[str, ...] = ()
        if base_aliases - kept:
            if not upload_aliases:
                # Every item struck: the list is blank, and blank never means delete.
                document.note(OUTCOME_IGNORED, BLANK_IS_NOT_A_DELETION, "# Also called")
            else:
                removed = tuple(
                    alias
                    for alias in concept.aliases
                    if _collapsed(alias).casefold() not in kept
                )
        if added or removed:
            document.edits.append(
                OkfImportEdit(
                    path=document.path,
                    kind=EDIT_CONCEPT_ALIASES,
                    subject_key=concept.key,
                    field="aliases",
                    proposed="\n".join(added),
                    added_aliases=tuple(added),
                    removed_aliases=removed,
                    base_version=concept.ontology_version,
                )
            )
    elif concept.aliases:
        document.note(OUTCOME_IGNORED, FIELD_REMOVAL_IGNORED, "# Also called")
    return handled


def _links(text: str) -> None:
    """Count and measure every Markdown link. Nothing is resolved or fetched."""
    syntax = _as_syntax(text)
    targets = _LINK.findall(syntax)
    targets.extend(angle or plain for angle, plain in _REFERENCE_LINK.findall(syntax))
    if len(targets) > MAX_LINKS_PER_DOCUMENT:
        raise _DocumentRefused(LINK_LIMIT_EXCEEDED)
    if any(len(target) > MAX_LINK_TARGET_CHARS for target in targets):
        raise _DocumentRefused(LINK_TOO_LONG)


def _executable(frontmatter: Mapping[str, Any]) -> bool:
    """Upstream's Attested Computation, or anything carrying its runnable fields."""
    if frontmatter.get("type") == ATTESTED_COMPUTATION or _EXECUTABLE_FIELDS & set(frontmatter):
        return True
    atlas = frontmatter.get("atlas")
    return isinstance(atlas, dict) and bool(_EXECUTABLE_FIELDS & set(atlas))


def _claimed_identity(frontmatter: Mapping[str, Any]) -> str | None:
    flat = _flatten(frontmatter)
    for key in _SUBJECT_KEY_FIELDS:
        value = flat.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _new_document(document: _Document, text: str) -> None:
    """A document the source publication never held. Never imported: import does not create
    objects or concepts. Its frontmatter is still read, under the same limits, so a copy of an
    existing document cannot slip an edit past the duplicate-identity check."""
    raw, _body = split_document(text)
    if raw is not None:
        try:
            _links(text)
            frontmatter = load_frontmatter(raw)
            if _executable(frontmatter):
                raise _DocumentRefused(EXECUTABLE_FIELD_REFUSED)
        except _DocumentRefused as refusal:
            document.note(OUTCOME_REFUSED, refusal.reason_code)
            return
        document.identity = _claimed_identity(frontmatter)
    document.note(OUTCOME_UNSUPPORTED, DOCUMENT_NOT_IN_SOURCE_BUNDLE)


def _analyze_document(document: _Document, upload: str, base_text: str, base: _Base) -> None:
    """Everything one changed document asks for. Raises `_DocumentRefused` to refuse it."""
    path = document.path
    subject = base.subjects.get(path)
    column_set = base.column_sets.get(path)
    if subject is None and column_set is None:
        # An index or a log: every byte is re-derived by Atlas on the next publication.
        document.note(OUTCOME_IGNORED, DOCUMENT_DERIVED)
        return
    _links(upload)
    raw_upload, upload_body = split_document(upload)
    raw_base, base_body = split_document(base_text)
    if raw_upload is None:
        raise _DocumentRefused(FRONTMATTER_MISSING)
    upload_frontmatter = load_frontmatter(raw_upload)
    if _executable(upload_frontmatter):
        raise _DocumentRefused(EXECUTABLE_FIELD_REFUSED)
    loaded = yaml.safe_load(raw_base) if raw_base else {}
    base_frontmatter: dict[str, Any] = _plain(loaded) if isinstance(loaded, dict) else {}
    _compare_frontmatter(document, base_frontmatter, upload_frontmatter)
    concept_type = str(base_frontmatter.get("type") or "")
    if concept_type in UNSUPPORTED_TYPES:
        if _collapsed(upload_body) != _collapsed(base_body):
            document.note(OUTCOME_UNSUPPORTED, FAMILY_NOT_SUPPORTED, concept_type)
        return
    base_sections, base_repeated = _section_map(base_body)
    upload_sections, upload_repeated = _section_map(upload_body)
    for heading in sorted(upload_repeated - base_repeated):
        document.note(OUTCOME_UNSUPPORTED, DOCUMENT_STRUCTURE_AMBIGUOUS, heading or "preamble")
    handled: set[str] = set()
    if column_set is not None:
        obj, columns = column_set
        if columns:
            handled |= _object_edits(
                document, obj, columns, base_sections, upload_sections, purpose=False, schema=True
            )
        elif base_sections.get("# Schema") != upload_sections.get("# Schema"):
            handled.add("# Schema")
            document.note(OUTCOME_UNSUPPORTED, DOCUMENT_STRUCTURE_AMBIGUOUS, "# Schema")
    elif concept_type in _OBJECT_TYPES and subject in base.objects:
        obj = base.objects[subject]
        # A wide object's own `# Schema` lists names only; its rows live in its column sets.
        wide = subject in base.wide_objects
        handled |= _object_edits(
            document,
            obj,
            () if wide else obj.columns,
            base_sections,
            upload_sections,
            purpose=True,
            schema=not wide,
        )
    elif concept_type == TYPE_CONCEPT and subject in base.concepts:
        handled |= _concept_edits(document, base.concepts[subject], base_sections, upload_sections)
    elif concept_type == TYPE_ROUTINE and subject in base.routines:
        # Only the purpose: Interface, Reads and writes, Coverage and Limitations are catalog
        # and lineage facts Atlas re-derives, and fall through to SECTION_DERIVED below.
        if "# Purpose" in base_sections and "# Purpose" in upload_sections:
            handled.add("# Purpose")
            _purpose_edit(
                document,
                base.routines[subject],
                base_sections["# Purpose"],
                upload_sections["# Purpose"],
                kind=EDIT_ROUTINE_PURPOSE,
            )
    for heading in sorted(set(base_sections) | set(upload_sections)):
        if heading in handled or base_sections.get(heading) == upload_sections.get(heading):
            continue
        if heading not in base_sections:
            document.note(OUTCOME_UNSUPPORTED, SECTION_UNKNOWN, heading)
        elif heading not in upload_sections:
            document.note(OUTCOME_IGNORED, FIELD_REMOVAL_IGNORED, heading or "preamble")
        else:
            document.note(OUTCOME_IGNORED, SECTION_DERIVED, heading or "preamble")


def analyze_bundle_edits(
    archive: OkfImportArchive,
    *,
    snapshot: OkfSnapshot,
    base_documents: Mapping[str, str],
) -> OkfImportAnalysis:
    """Compare every uploaded document with the stored bytes of the publication it came from.

    `snapshot` and `base_documents` are Atlas's own stored publication -- never anything read
    from the upload -- so the "before" of every comparison is governed content. A document whose
    bytes equal what Atlas stored is unchanged and is not parsed at all.

    **Colliding identities.** Every uploaded document occupies the identity its path held in
    the source publication, and a document at a path the publication never held occupies the
    identity its frontmatter claims. Two documents on one identity -- a table's document copied
    to a second path and edited there -- cannot both be what the editor meant, so every document
    on that identity is refused and none of their edits is proposed.
    """
    base = _base(snapshot)
    unchanged = 0
    changed = 0
    analysed: list[_Document] = []
    for item in archive.documents:
        document = _Document(item.path)
        analysed.append(document)
        stored = base_documents.get(item.path)
        if item.refused is not None or item.text is None:
            document.note(OUTCOME_REFUSED, item.refused or ENCODING_INVALID)
            document.identity = base.subjects.get(item.path)
            continue
        if stored is None:
            _new_document(document, item.text)
            continue
        document.identity = base.subjects.get(item.path)
        if item.text == stored:
            unchanged += 1
            continue
        changed += 1
        if changed > MAX_CHANGED_DOCUMENTS:
            raise OkfImportRefused(
                IMPORT_TOO_MANY_CHANGES,
                f"more than {MAX_CHANGED_DOCUMENTS} documents were edited; a review cannot "
                "meaningfully cover that. Split the edit into smaller imports.",
            )
        try:
            _analyze_document(document, item.text, stored, base)
        except _DocumentRefused as refusal:
            document.edits.clear()
            document.notes.clear()
            document.note(OUTCOME_REFUSED, refusal.reason_code)
    holders: dict[str, list[_Document]] = {}
    for document in analysed:
        if document.identity is not None:
            holders.setdefault(document.identity, []).append(document)
    for sharing in holders.values():
        if len(sharing) > 1:
            for document in sharing:
                document.edits.clear()
                document.note(OUTCOME_REFUSED, DUPLICATE_STABLE_ID)
    edits = [edit for document in analysed for edit in document.edits]
    if len(edits) > MAX_IMPORT_EDITS:
        raise OkfImportRefused(
            IMPORT_TOO_MANY_CHANGES,
            f"the bundle makes {len(edits)} edits, over the {MAX_IMPORT_EDITS} one review can "
            "meaningfully cover. Split the edit into smaller imports.",
        )
    notes = [note for document in analysed for note in document.notes]
    if archive.ignored_members:
        notes.append(
            OkfImportNote(
                path=None,
                outcome=OUTCOME_IGNORED,
                reason_code=OS_METADATA_IGNORED,
                detail=f"{archive.ignored_members} operating-system metadata member(s) skipped",
            )
        )
    return OkfImportAnalysis(
        documents=len(archive.documents),
        unchanged=unchanged,
        changed=changed,
        edits=tuple(edits),
        notes=tuple(notes),
    )


#: Every reason code, from the archive's own refusals to a proposal's conflict: the names a
#: refusal is allowed to persist, and the set the round-trip contract document must name.
REASON_CODES: Final = frozenset(
    value
    for name, value in list(globals().items())
    if name.isupper() and isinstance(value, str) and value == name
)
