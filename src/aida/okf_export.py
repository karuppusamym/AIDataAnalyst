"""R11-OKF01 (design item 14A): governed Atlas knowledge, exported as an OKF v0.2 bundle.

Atlas already holds approved object descriptions (FP08), context products (FP12), captured
value-free definitions (FP03) and pinned ontology meaning (FP09). This module turns a
**frozen snapshot** of that content into Markdown concept documents with YAML frontmatter,
source/product indexes, and an Atlas manifest. It adds nothing: the exporter cannot draft,
cannot call a model, cannot read a clock and cannot read a database.

**The pinned specification.** `Docs/10-architecture/21-graphql-okf-and-workspace-design.md`
section 14 says to use the official GoogleCloudPlatform Open Knowledge Format and to "pin the
upstream revision and keep conformance fixtures when implementation starts". The pin is
`OKF_SPEC_REVISION` below, with the digest of the specification text it was read from, so a
later pass can prove which words this code was written against. What is *tested* is stated
exactly in `OKF_CONFORMANCE_STATUS`: this producer satisfies the v0.2 §11 conformance clauses
as checked by `validate_okf_conformance` in this module, and that checker is exercised against
upstream reference bundles as fixtures. There is no upstream conformance test suite, so no
certification is claimed. See `Docs/90-reference/okf-export-profile.md`.

**Why a frozen snapshot rather than a product-version pointer.** The design is explicit:
"OKF determinism requires a frozen content snapshot, not just a product version pointing to
mutable current catalog rows." A context product version pins *which* objects it covers; it
does not pin what the catalog says about them, and a discovery run can move a definition
digest, a column list or an approved description underneath a second export of the same
version. So the unit of determinism here is `OkfSnapshot`: an immutable value whose every
field is a JSON primitive, captured once under one read decision by
`aida.okf_export_api.freeze_okf_snapshot`, and from which `export_okf_bundle` is a pure
function. Two exports of one snapshot are byte-identical by construction, and a snapshot that
round-trips through `snapshot_to_document`/`snapshot_from_document` exports to the same bytes
again -- which is what makes "frozen" a property rather than a claim
(`tests/test_okf_export.py`).

**Two clock rules, and why they differ from the obvious ones.**

* The exporter never reads a clock. `OkfSnapshot.captured_at` is an input.
* No *discovery* timestamp enters a concept document. `last_scan_completed_at` moves every
  time a scan completes, including a scan that changed nothing, so a document carrying it
  could not satisfy acceptance OKF-C ("no-op scans reproduce hashes"). It rides in the
  manifest instead, beside the concept tree rather than inside it -- the same split
  `context_compiler.freshness_section` already makes for exactly the same reason, and the same
  reason the design warns against equating "a new export timestamp" with "fresh source
  evidence". A document's own `generated.at` is the last *meaningful content change* it can
  evidence: an approval, or a captured definition version. Both only move when content moves.

**Value freedom (INV-6) is structural, not a filter.** There is no field on any snapshot type
that can hold a routine body, a view definition, a column default expression or a sample row.
Definitions are represented by a digest of the stored value-free text plus availability,
truncation and parse state, exactly as `context_product_coverage` represents them. A column's
`default_expression` -- the most literal-bearing column in the catalog -- has no field here at
all, and a connector's free-text `unavailable_reason` is reduced to a bounded reason code
rather than exported verbatim. `validate_atlas_publish_policy` then refuses any document
carrying a fenced code block, so a future field that reintroduced code text fails the gate
rather than shipping.

**Authorization (OKF-D) is upstream of counting.** Every count this module renders -- in an
index, in the manifest -- is computed from the snapshot it was handed. The snapshot contains
only objects the caller was admitted to (see `aida.okf_export_api`), and references to
anything else are dropped before freezing, so there is no code path here that could count a
dependency the reader may not see. A reference that survives into the snapshot but names an
absent object is a build failure, not a silently broken link.

**Verification is never inferred.** OKF's `verified` family and Atlas's draft/approval/
withdrawal states are mapped separately (`_okf_status`, `_verification`). Document-level
`verified` is emitted only where every asserted statement in the document is covered by a
recorded Atlas approval event -- which for a catalog object it never is, because captured
columns, lineage and coverage are derived rather than reviewed. Those documents carry the
approval they *can* evidence per statement in the `atlas` extension and in footnote-attributed
`sources` entries, and no document-level verification at all. Publishing a bundle is not a
reviewer's signature on an inferred statement.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final

import yaml

# --- the pin ----------------------------------------------------------------------------

#: The OKF version this profile targets, as declared by the pinned specification text.
OKF_VERSION: Final = "0.2"
OKF_SPEC_REPOSITORY: Final = "https://github.com/GoogleCloudPlatform/open-knowledge-format"
OKF_SPEC_PATH: Final = "SPEC.md"
#: The upstream commit this implementation was written against. Pinned rather than tracking
#: `main` so "conformant with the specification" names a specific text.
OKF_SPEC_REVISION: Final = "0b87c52c6ef999286c745e19998fdfcd03d5dbee"
#: SHA-256 of `SPEC.md` at `OKF_SPEC_REVISION`, so a later fetch can prove the pin resolves to
#: the same words. Recorded here rather than vendoring upstream text into this repository.
OKF_SPEC_SHA256: Final = "26aa5da029278939f914e578107242d9607d4f2dc5fe153272b82f9ed1030101"
#: Exactly what is tested, so nothing downstream can read the pin as a certification. The
#: design's closing line is "Test against the pinned specification before claiming
#: conformance"; this is the honest state of that test.
OKF_CONFORMANCE_STATUS: Final = "SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES"

#: The Atlas export profile. Bumped when the rendered bytes for unchanged content change, so a
#: manifest records which renderer produced it.
EXPORT_PROFILE: Final = "atlas-okf-export"
#: "2" since R11-OKF02: tool-version concept documents and the refresh `log.md` files changed
#: the rendered tree for unchanged content, which is exactly what this number records.
#: "3": an object wider than `MAX_COLUMNS_PER_DOCUMENT` is split into column-set documents.
#: Narrower objects render as they did under "2"; the bump is for the wide ones, whose stored
#: single document must be re-rendered rather than served as current.
EXPORT_PROFILE_VERSION: Final = "3"
#: The actor convention of spec §7: `process:<id>` for an automated process.
EXPORTER_ACTOR: Final = "process:atlas-okf-export"
MANIFEST_VERSION: Final = "1"
#: Name of the Atlas manifest inside a materialized archive. It sits *outside* the concept
#: tree (`BUNDLE_ROOT`) because it is an Atlas extension: an OKF reader must be able to
#: consume the bundle without it.
MANIFEST_FILENAME: Final = "atlas-manifest.json"
#: Archive prefix holding the OKF bundle root, so `index.md` resolves absolute `/...` links.
BUNDLE_ROOT: Final = "bundle"

# --- concept types (spec §4.1: producer-chosen, never centrally registered) --------------

TYPE_SOURCE: Final = "Atlas Data Source"
TYPE_SCHEMA: Final = "Atlas Schema"
TYPE_TABLE: Final = "Atlas Table"
TYPE_VIEW: Final = "Atlas View"
TYPE_MATERIALIZED_VIEW: Final = "Atlas Materialized View"
TYPE_ROUTINE: Final = "Atlas Routine"
TYPE_PACKAGE: Final = "Atlas Routine Package"
TYPE_CONCEPT: Final = "Atlas Business Concept"
#: R11-OKF02: one approved governed-tool version. Deliberately *not* upstream's
#: `Attested Computation`: that type carries `computation`/`executor` fields, which are runnable
#: embedded code, and the design forbids "runnable embedded code auto-executed by import". An
#: Atlas tool is described -- its interface and how to invoke it through Atlas -- never shipped.
TYPE_TOOL_VERSION: Final = "Atlas Tool Version"
#: One ordinal range of a wide object's columns (`MAX_COLUMNS_PER_DOCUMENT`).
TYPE_COLUMN_SET: Final = "Atlas Column Set"

#: `MetadataTable` kinds, as `discovery_selection.table_kind` normalizes them.
KIND_TABLE: Final = "TABLE"
KIND_VIEW: Final = "VIEW"
KIND_MATERIALIZED_VIEW: Final = "MATERIALIZED_VIEW"

#: Atlas description states, as `context_product_coverage` reports them, plus the export-time
#: egress verdict. `WITHHELD` is not an Atlas lifecycle state: it means screening refused to
#: release text Atlas does have, and it is deliberately distinguishable from `NONE`.
DESCRIPTION_APPROVED: Final = "APPROVED"
DESCRIPTION_PROPOSED: Final = "PROPOSED"
DESCRIPTION_WITHDRAWN: Final = "WITHDRAWN"
DESCRIPTION_WITHHELD: Final = "WITHHELD"
DESCRIPTION_NONE: Final = "NONE"

#: Bounded reason codes replacing a connector's free-text `unavailable_reason`.
DEFINITION_WITHHELD: Final = "DEFINITION_WITHHELD"
DEFINITION_TRUNCATED: Final = "DEFINITION_TRUNCATED"
DEFINITION_ABSENT: Final = "DEFINITION_NOT_CAPTURED"

#: Explicit size/coverage failures, so a bundle is never silently truncated and called
#: complete (design section 14, "Publish").
#: R11-OKF02 consumption: the most columns one document carries. An object with more is
#: published as its own document -- purpose, dependencies, coverage and a names-only column
#: list -- plus column-set documents holding the full rows, so a reader (an agent above all)
#: can open the part a question needs instead of one document it cannot hold. Design §14:
#: "split unusually large definitions by stable structural section only when retrieval limits
#: require it" -- an object at or under the limit renders exactly as before.
MAX_COLUMNS_PER_DOCUMENT: Final = 100
MAX_DOCUMENT_BYTES: Final = 256 * 1024
MAX_FRONTMATTER_BYTES: Final = 32 * 1024
MAX_DOCUMENTS: Final = 20_000
MAX_BUNDLE_BYTES: Final = 64 * 1024 * 1024
#: R11-OKF02: how many refresh entries a `log.md` lists, and how many paths one entry names.
#: Older entries and further paths are *counted* in the log, never silently dropped.
MAX_LOG_ENTRIES: Final = 50
MAX_LOG_PATHS: Final = 20
MAX_LOG_STORED_PATHS: Final = 1000
#: Publication triggers, as the refresh history records them.
TRIGGER_INITIAL: Final = "INITIAL"
TRIGGER_SOURCE_CHANGE: Final = "SOURCE_CHANGE"
TRIGGER_REVALIDATION: Final = "REVALIDATION"
TRIGGER_RENDERER_CHANGE: Final = "RENDERER_CHANGE"

#: One safe path segment. Lower-case, digits and single hyphens only: no separator, no dot, no
#: drive letter, no case-collision on a case-insensitive filesystem, nothing a shell expands.
_SAFE_SEGMENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: The stable-identity digest length. 128 bits, and uniqueness is asserted at build time
#: anyway, so a collision is a refused export rather than a merged document.
_KEY_LENGTH: Final = 32
_KEY_PATTERN: Final = re.compile(rf"[0-9a-f]{{{_KEY_LENGTH}}}")

#: A fixed DOS timestamp for every archive member. `zipfile` would otherwise stamp the current
#: time into each local header, and two archives built from one snapshot would differ.
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


class OkfExportError(RuntimeError):
    """The snapshot cannot be exported honestly. Never a partial bundle."""


# --- identity ---------------------------------------------------------------------------


def _key(kind: str, parts: Sequence[str]) -> str:
    """An opaque, safe, stable path segment for one Atlas identity.

    Derived from the identity tuple rather than from a row's UUID on purpose. A UUID changes
    when an object is dropped and rediscovered, and differs between environments holding the
    same catalog, so a bundle keyed on UUIDs would report a content change where there is
    none. The tuple is JSON-encoded before hashing so `["a", "b"]` and `["ab"]` cannot
    collide through concatenation.
    """
    payload = json.dumps([kind, *parts], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_KEY_LENGTH]


def source_key(datasource_id: str) -> str:
    return _key("source", [datasource_id])


def schema_key(datasource_id: str, catalog: str, schema: str) -> str:
    return _key("schema", [datasource_id, catalog, schema])


def object_key(datasource_id: str, catalog: str, schema: str, name: str) -> str:
    """A table, view or materialized view. The kind is deliberately *not* part of the
    identity: they share one namespace in every supported engine, so a rescan that
    reclassifies a view as a table must not move the document."""
    return _key("object", [datasource_id, catalog, schema, name])


def routine_key(
    datasource_id: str, catalog: str, schema: str, package_name: str, name: str, signature: str
) -> str:
    """A routine, keyed on the identity `MetadataRoutine` itself is keyed on.

    `signature` keeps PostgreSQL overloads apart, `package_name` keeps a packaged member apart
    from a standalone routine of the same name (R11-FP03), and the leading `"routine"` keeps
    all of them apart from a table of the same name.
    """
    return _key("routine", [datasource_id, catalog, schema, package_name, name, signature])


def package_key(datasource_id: str, catalog: str, schema: str, package_name: str) -> str:
    return _key("package", [datasource_id, catalog, schema, package_name])


def concept_key(ontology_key: str, ontology_version: int, concept_name: str) -> str:
    return _key("concept", [ontology_key, str(ontology_version), concept_name])


def tool_version_key(project_id: str, slug: str, version: int) -> str:
    """One approved tool version. `GovernedTool` is unique on (project, slug), and a version
    number is immutable once assigned, so the tuple names exactly one reviewed interface."""
    return _key("tool-version", [project_id, slug, str(version)])


# --- the frozen snapshot ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OkfApproval:
    """A recorded Atlas approval event for one content version.

    `human` is decided by the loader from the principal that decided, never guessed here: spec
    §5.3 has consumers classify trust by the `human:` prefix, so writing that prefix where
    Atlas recorded an automated decider would manufacture a human review.
    """

    actor: str
    at: str
    human: bool


@dataclass(frozen=True, slots=True)
class OkfDescription:
    """What Atlas asserts an object is for, and on whose authority.

    `text` is the approved Atlas-authored description and nothing else. A pending draft is not
    what the platform asserts and a source comment is the source speaking, so both arrive here
    as `None` with `state` saying which -- the same rule `ResolvedRoutineReference` applies.
    """

    state: str = DESCRIPTION_NONE
    text: str | None = None
    version: int | None = None
    approval: OkfApproval | None = None
    withheld_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OkfDefinitionFacts:
    """How completely Atlas holds a definition. Never the definition."""

    available: bool
    digest: str | None
    truncated: bool
    lineage: str
    reason_codes: tuple[str, ...] = ()
    capture_version: int | None = None
    #: When the *content* was captured. Written only when a definition actually changed
    #: (`MetadataRoutineDefinitionVersion` is append-only and an identical rescan writes
    #: nothing), so unlike a scan-completion time it is safe inside a hashed document.
    captured_at: str | None = None


@dataclass(frozen=True, slots=True)
class OkfColumnFacts:
    """One column of one table. No default expression, by construction: it is the catalog's
    most literal-bearing text and has no field here."""

    name: str
    ordinal: int
    physical_type: str
    nullable: bool
    classification: str
    lifecycle: str
    description: OkfDescription = field(default_factory=OkfDescription)


@dataclass(frozen=True, slots=True)
class OkfLink:
    """A typed edge the body renders as a Markdown link plus prose.

    The relation is carried here and written into the prose because "a plain Markdown link is
    not proof of a foreign key or an approved join" -- the typed record stays in Atlas.
    """

    target_key: str
    relation: str


@dataclass(frozen=True, slots=True)
class OkfObjectFacts:
    """A table, view or materialized view, fully resolved and authorized."""

    key: str
    kind: str
    native_object_type: str
    name: str
    qualified_name: str
    schema_key: str
    source_key: str
    lifecycle: str
    columns: tuple[OkfColumnFacts, ...] = ()
    description: OkfDescription = field(default_factory=OkfDescription)
    definition: OkfDefinitionFacts | None = None
    links: tuple[OkfLink, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OkfParameterFacts:
    name: str
    ordinal: int
    mode: str
    physical_type: str


@dataclass(frozen=True, slots=True)
class OkfRoutineFacts:
    """A procedure or function. `signature` and `package_name` are identity, not decoration."""

    key: str
    name: str
    package_name: str
    signature: str
    qualified_name: str
    routine_type: str
    schema_key: str
    source_key: str
    lifecycle: str
    native_subtype: str | None = None
    language: str | None = None
    return_type: str | None = None
    is_deterministic: bool | None = None
    security_mode: str | None = None
    parameters: tuple[OkfParameterFacts, ...] = ()
    description: OkfDescription = field(default_factory=OkfDescription)
    definition: OkfDefinitionFacts | None = None
    links: tuple[OkfLink, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OkfPackageFacts:
    """A routine package: a container for member subprograms.

    Navigational only unless Atlas holds an approved description of the package itself. A
    source or schema summary "must not invent a business purpose", and neither does this.
    """

    key: str
    name: str
    qualified_name: str
    schema_key: str
    source_key: str
    lifecycle: str
    member_keys: tuple[str, ...] = ()
    description: OkfDescription = field(default_factory=OkfDescription)


@dataclass(frozen=True, slots=True)
class OkfSchemaFacts:
    key: str
    name: str
    catalog_name: str
    qualified_name: str
    source_key: str
    lifecycle: str


@dataclass(frozen=True, slots=True)
class OkfSourceFacts:
    """One datasource the caller was admitted to. Freshness is *not* here: it is a clock
    reading, so it lives in the manifest (`OkfSnapshot.freshness`)."""

    key: str
    name: str
    dialect: str
    connector_type: str
    environment: str
    lifecycle: str


@dataclass(frozen=True, slots=True)
class OkfConceptRelation:
    predicate: str
    target_name: str
    target_key: str | None


@dataclass(frozen=True, slots=True)
class OkfConceptFacts:
    """A business concept, read from the pinned approved ontology version and never from the
    ontology's head, so a later publication cannot change what a bundle says."""

    key: str
    name: str
    ontology_key: str
    ontology_version: int
    lifecycle: str
    label: str | None = None
    definition: str | None = None
    mapped_object_keys: tuple[str, ...] = ()
    mapped_routine_keys: tuple[str, ...] = ()
    relations: tuple[OkfConceptRelation, ...] = ()
    approval: OkfApproval | None = None
    withheld_reason_codes: tuple[str, ...] = ()
    #: The approved version's own aliases, screened like its definition. The words people
    #: actually use -- "closing position" for `end_of_day_position` -- which is how a reader,
    #: or Atlas's own context retrieval, gets from a question to the concept at all.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OkfPolicyPartition:
    """The partition a bundle belongs to. Two bundles with different partitions are not
    interchangeable even when their content digests match, which is why it is hashed into
    `scope_digest` rather than only printed."""

    allowed_consumer_roles: tuple[str, ...]
    source_values: str
    classifications: tuple[str, ...]
    purpose: str | None = None


@dataclass(frozen=True, slots=True)
class OkfScope:
    """What was selected, and under whose authority it was frozen."""

    kind: str
    organization_id: str
    policy_partition: OkfPolicyPartition
    product_key: str | None = None
    product_version: int | None = None
    product_version_id: str | None = None
    product_fingerprint: str | None = None
    product_name: str | None = None
    product_purpose: str | None = None
    #: Approved tool versions the product makes eligible. References only: a data answer goes
    #: through Atlas execution, and no runnable code or credential is ever exported.
    eligible_tool_version_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OkfSourceFreshness:
    """When a source was last read, and last read in full. Manifest-only, by design."""

    source_key: str
    last_scan_completed_at: str | None
    last_full_scan_completed_at: str | None


@dataclass(frozen=True, slots=True)
class OkfToolInput:
    """One declared input of a tool version: its name, type and whether it is required.

    Nothing else from the parameter schema. A declared default, an enum or an example is a
    literal a steward or a generator wrote down, and a literal is where a source value hides
    (INV-6), so none of them has a field here.
    """

    name: str
    physical_type: str
    required: bool


@dataclass(frozen=True, slots=True)
class OkfToolFacts:
    """R11-OKF02: one approved tool version the product makes eligible, as a reference.

    What a consumer needs to *call* the tool through Atlas -- its interface and its invocation --
    and what Atlas approved about it. Never its SQL, its executor or a credential: a data answer
    goes through the query gateway, and an importer must find nothing to run.
    """

    key: str
    tool_version_id: str
    slug: str
    name: str
    version: int
    lifecycle: str
    source_key: str
    fingerprint: str
    inputs: tuple[OkfToolInput, ...] = ()
    description: OkfDescription = field(default_factory=OkfDescription)


@dataclass(frozen=True, slots=True)
class OkfSnapshot:
    """The frozen content snapshot. The only input `export_okf_bundle` has.

    Every field is a JSON primitive or a tuple of these value types, so the snapshot is
    hashable content rather than a view over live rows, and `snapshot_to_document` can write it
    out and read it back unchanged.
    """

    captured_at: str
    scope: OkfScope
    sources: tuple[OkfSourceFacts, ...] = ()
    schemas: tuple[OkfSchemaFacts, ...] = ()
    objects: tuple[OkfObjectFacts, ...] = ()
    routines: tuple[OkfRoutineFacts, ...] = ()
    packages: tuple[OkfPackageFacts, ...] = ()
    concepts: tuple[OkfConceptFacts, ...] = ()
    freshness: tuple[OkfSourceFreshness, ...] = ()
    #: R11-OKF02. Admitted tool versions only: a tool reading a datasource the reader was not
    #: admitted to is absent, not counted and not listed, exactly like the datasource itself.
    tools: tuple[OkfToolFacts, ...] = ()
    okf_version: str = OKF_VERSION
    profile: str = EXPORT_PROFILE
    profile_version: str = EXPORT_PROFILE_VERSION
    spec_revision: str = OKF_SPEC_REVISION

    def content_digest(self) -> str:
        """The frozen content's identity, excluding `captured_at`.

        `captured_at` is when the freeze happened, not what was frozen. Excluding it is what
        makes "a no-op re-capture reproduces the same digest" a meaningful assertion instead of
        a statement about two clocks.
        """
        payload = snapshot_to_document(self)
        payload.pop("captured_at", None)
        return _digest_text(_canonical_json(payload))


# --- snapshot serialization -------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_document(value: Any) -> Any:
    """Recursively render a snapshot value as JSON primitives, dropping nothing."""
    if isinstance(value, tuple | list):
        return [_as_document(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _as_document(getattr(value, name))
            for name in type(value).__dataclass_fields__
        }
    return value


def snapshot_to_document(snapshot: OkfSnapshot) -> dict[str, Any]:
    """The snapshot as a plain JSON-able mapping. Lossless, so the round trip below is real."""
    document = _as_document(snapshot)
    assert isinstance(document, dict)
    return document


def _build(target: type[Any], payload: Mapping[str, Any]) -> Any:
    """Rebuild one frozen dataclass from `snapshot_to_document` output.

    Written out rather than delegated to a validation library because the snapshot is Atlas's
    own artifact, and because an unknown key must fail loudly: a snapshot serialized by a newer
    profile is not something this renderer may quietly ignore half of.
    """
    fields = target.__dataclass_fields__
    unknown = sorted(set(payload) - set(fields))
    if unknown:
        raise OkfExportError(f"unknown snapshot field(s) for {target.__name__}: {unknown}")
    kwargs: dict[str, Any] = {}
    for name, spec in fields.items():
        if name not in payload:
            continue
        kwargs[name] = _coerce(spec.type, payload[name])
    return target(**kwargs)


_VALUE_TYPES: Final[dict[str, type[Any]]] = {}


def _coerce(annotation: Any, value: Any) -> Any:
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    for name, target in _VALUE_TYPES.items():
        if name not in text:
            continue
        if text.startswith("tuple["):
            return tuple(_build(target, item) for item in value)
        return None if value is None else _build(target, value)
    if text.startswith("tuple[") and value is not None:
        return tuple(value)
    return value


def snapshot_from_document(payload: Mapping[str, Any]) -> OkfSnapshot:
    """Rebuild a frozen snapshot written by `snapshot_to_document`.

    This is the reason the freeze is more than a phrase: the bytes a bundle is rendered from
    can be written down, handed back, and rendered again to the same bytes, with no database in
    the path at all.
    """
    return _build(OkfSnapshot, payload)  # type: ignore[no-any-return]


_VALUE_TYPES.update(
    {
        "OkfApproval": OkfApproval,
        "OkfDescription": OkfDescription,
        "OkfDefinitionFacts": OkfDefinitionFacts,
        "OkfColumnFacts": OkfColumnFacts,
        "OkfLink": OkfLink,
        "OkfObjectFacts": OkfObjectFacts,
        "OkfParameterFacts": OkfParameterFacts,
        "OkfRoutineFacts": OkfRoutineFacts,
        "OkfPackageFacts": OkfPackageFacts,
        "OkfSchemaFacts": OkfSchemaFacts,
        "OkfSourceFacts": OkfSourceFacts,
        "OkfConceptRelation": OkfConceptRelation,
        "OkfConceptFacts": OkfConceptFacts,
        "OkfPolicyPartition": OkfPolicyPartition,
        "OkfScope": OkfScope,
        "OkfSourceFreshness": OkfSourceFreshness,
        "OkfToolFacts": OkfToolFacts,
        "OkfToolInput": OkfToolInput,
    }
)


# --- rendered output --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OkfDocument:
    """One file in the OKF bundle tree, with the bytes a reader will see."""

    path: str
    text: str

    @property
    def sha256(self) -> str:
        return _digest_text(self.text)

    @property
    def byte_length(self) -> int:
        return len(self.text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class OkfBundle:
    documents: tuple[OkfDocument, ...]
    manifest: dict[str, Any]

    @property
    def content_digest(self) -> str:
        """A digest over the concept tree alone: paths and their content, nothing else.

        Deliberately excludes the manifest, whose `captured_at` moves with every freeze. This
        is the number acceptance OKF-C is about -- "no-op scans reproduce hashes" -- and it can
        only hold for a digest that does not include when the export ran.
        """
        return _digest_text(
            _canonical_json([[document.path, document.sha256] for document in self.documents])
        )

    def manifest_json(self) -> str:
        return json.dumps(self.manifest, sort_keys=True, indent=2, ensure_ascii=True) + "\n"

    def document(self, path: str) -> OkfDocument:
        for candidate in self.documents:
            if candidate.path == path:
                return candidate
        raise KeyError(path)


@dataclass(frozen=True, slots=True)
class OkfValidation:
    valid: bool
    findings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OkfLogEntry:
    """R11-OKF02: one publication in a bundle's refresh history, as `log.md` renders it.

    An input, like the snapshot, so a bundle carrying a log is still a pure function of what it
    was handed. Paths are the bundle's own opaque paths; no object name, no text and no value.
    `date` is the publication's UTC day, the ISO `YYYY-MM-DD` spec section 9 requires.
    """

    date: str
    sequence: int
    trigger: str
    #: For the first publication only: how many documents it published. Its paths are not
    #: listed -- every path in the bundle would be -- so the log states the count instead.
    documents: int = 0
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: int = 0
    #: True when a list above was cut at `MAX_LOG_STORED_PATHS`; the log then says so.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class OkfPublicationStamp:
    """R11-OKF02: the publication a rebuild is producing, so it can write its own log entry.

    The entry depends on what the rebuild changed, and the log is part of the bundle, so the
    renderer computes the entry after the content documents and before the logs.
    """

    date: str
    sequence: int
    trigger: str


@dataclass(frozen=True, slots=True)
class OkfRebuildReport:
    """R11-OKF02: what an incremental rebuild did, against the bytes of the prior bundle.

    `rendered` and `carried` say what the rebuild *did*: a carried path's stored bytes were
    reused without its builder running. `added`/`changed`/`removed` say what *moved*, by
    comparing bytes. `changed_subjects` are the identity keys whose frozen facts differ between
    the two snapshots -- the dependency roots every rendered document traces back to.
    """

    rendered: tuple[str, ...]
    carried: tuple[str, ...]
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    changed_subjects: tuple[str, ...]
    full: bool


# --- frontmatter and body rendering -----------------------------------------------------


def _yaml_block(payload: Mapping[str, Any]) -> str:
    """Frontmatter, serialized the one way this profile serializes it.

    `sort_keys` and `allow_unicode=False` are what make two renders of one value equal; the
    same options `context_compiler` uses for its YAML target, so the two artifacts cannot drift
    apart in serialization discipline.
    """
    return yaml.safe_dump(
        dict(payload),
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=False,
        width=4096,
    )


def _document_text(frontmatter: Mapping[str, Any], body: Sequence[str]) -> str:
    rendered = _yaml_block(frontmatter)
    if len(rendered.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise OkfExportError(
            f"frontmatter exceeds {MAX_FRONTMATTER_BYTES} bytes; "
            "refuse rather than ship a truncated document"
        )
    lines = [line.rstrip() for line in body]
    while lines and not lines[-1]:
        lines.pop()
    return "---\n" + rendered + "---\n\n" + "\n".join(lines) + "\n"


def _okf_status(lifecycle: str, description: OkfDescription) -> str:
    """Spec §5.4 `status` from Atlas state, mapped rather than borrowed.

    Atlas's lifecycle answers "does this object still exist at the source"; its description
    state answers "has a reviewer approved what we say about it". OKF's single `status` has to
    carry both, so: a retired object is `deprecated`; an object Atlas makes no approved
    statement about is `draft`, including one whose description a reviewer *withdrew* (there is
    no current approved statement, which is what a consumer needs to know); anything else is
    `stable`.
    """
    if lifecycle.upper() != "ACTIVE":
        return "deprecated"
    if description.state == DESCRIPTION_APPROVED:
        return "stable"
    return "draft"


def _actor(approval: OkfApproval) -> str:
    """One recorded principal, in the spec §7 actor convention, spelled the same way everywhere.

    A single helper on purpose: §5.3 has consumers derive "human-reviewed" from the `human:`
    prefix, so a second place that prefixed differently would give two readings of one approval.
    An actor already carrying a convention prefix, or a `producer/version` tool spelling, is
    left alone.
    """
    if approval.actor.startswith(("human:", "process:")) or "/" in approval.actor:
        return approval.actor
    return ("human:" if approval.human else "process:") + approval.actor


def _verification(*approvals: OkfApproval | None) -> list[dict[str, str]]:
    """Spec §5.2 `verified` entries, from recorded Atlas approval events only.

    Callers pass the approvals covering *every* statement in the document. An empty result
    means the document carries no `verified` key at all, which spec §5.3 reads as `unverified`
    -- the correct reading when Atlas cannot evidence a verification of the whole document.
    """
    entries = [
        {"by": _actor(approval), "at": approval.at}
        for approval in approvals
        if approval is not None
    ]
    return sorted(entries, key=lambda entry: (entry["at"], entry["by"]))


def _content_moment(*moments: str | None) -> str | None:
    """The latest content change any input can evidence, or `None`.

    Only approval and capture instants are ever passed in. A discovery completion time is not a
    content change and never reaches here (see the module docstring).
    """
    present = sorted(moment for moment in moments if moment)
    return present[-1] if present else None


def _first_sentence(text: str, *, limit: int = 240) -> str:
    """A one-line `description` for indexes and previews, taken from approved text.

    Truncates on a sentence boundary where there is one and never invents a summary: spec §4.1
    wants a single sentence, and the honest single sentence is the author's first one.
    """
    collapsed = " ".join(text.split())
    match = re.search(r"(?<=[.!?])\s", collapsed)
    sentence = collapsed[: match.start()] if match else collapsed
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def _absolute(path: str) -> str:
    """Spec §6.1's recommended bundle-relative link form."""
    return "/" + path


def _purpose_section(description: OkfDescription, subject: str) -> list[str]:
    """What the object is for, or an explicit statement that Atlas does not know.

    "Unknown when not established" is a content rule from the design's own table. An empty
    section, or a sentence assembled from a name, would read as knowledge.
    """
    lines = ["# Purpose", ""]
    if description.state == DESCRIPTION_APPROVED and description.text:
        lines.extend([f"{description.text.strip()}[^approved-description]", ""])
        return lines
    if description.state == DESCRIPTION_PROPOSED:
        lines.append(
            f"Not established. A description of this {subject} is drafted and awaiting "
            "review; Atlas does not assert an unreviewed draft."
        )
    elif description.state == DESCRIPTION_WITHDRAWN:
        lines.append(
            f"Not established. An approved description of this {subject} was retired and "
            "none has replaced it."
        )
    elif description.state == DESCRIPTION_WITHHELD:
        codes = ", ".join(description.withheld_reason_codes) or "screening"
        lines.append(
            f"Withheld. Atlas holds an approved description of this {subject}, and export "
            f"screening refused to release its text ({codes})."
        )
    else:
        lines.append(f"Not established. No approved description of this {subject} exists.")
    lines.append("")
    return lines


def _description_footnote(description: OkfDescription) -> list[str]:
    if description.state != DESCRIPTION_APPROVED:
        return []
    version = f" version {description.version}" if description.version is not None else ""
    approval = description.approval
    attribution = (
        f", approved by {_actor(approval)} at {approval.at}" if approval is not None else ""
    )
    return [f"[^approved-description]: Approved Atlas description{version}{attribution}.", ""]


def _sources_entries(
    *,
    object_resource: str,
    description: OkfDescription,
    definition: OkfDefinitionFacts | None,
) -> list[dict[str, Any]]:
    """Spec §5.1 `sources`: the Atlas records this document derives from.

    Keyed entries, because §5.1 is explicit that footnote labels are the join key and a
    positional index misattributes the moment a list is reordered. `author` and `last_modified`
    are the spec's own credibility signals, filled from the approval and capture events Atlas
    recorded -- never invented, and omitted where there is no record.
    """
    entries: list[dict[str, Any]] = []
    if description.state == DESCRIPTION_APPROVED:
        entry: dict[str, Any] = {
            "id": "approved-description",
            "resource": f"{object_resource}/description",
            "title": "Approved Atlas description"
            + (f" version {description.version}" if description.version is not None else ""),
        }
        if description.approval is not None:
            entry["author"] = _actor(description.approval)
            entry["last_modified"] = description.approval.at
        entries.append(entry)
    if definition is not None and definition.digest is not None:
        entry = {
            "id": "captured-definition",
            "resource": f"{object_resource}/definition",
            "title": "Captured value-free definition"
            + (
                f" version {definition.capture_version}"
                if definition.capture_version is not None
                else ""
            ),
        }
        if definition.captured_at is not None:
            entry["last_modified"] = definition.captured_at
        entries.append(entry)
    return entries


def _definition_lines(
    definition: OkfDefinitionFacts | None, *, defines_itself: bool = True
) -> list[str]:
    """Coverage of a definition, said plainly and without the definition.

    "Represent unsupported kinds and unavailable definitions honestly" cuts both ways: a
    withheld definition is reported as withheld, and a digest is reported as a digest rather
    than dressed up as the text. `defines_itself` is `False` for a base table, which has no
    stored definition to hold -- reporting that as "not captured" would read as a gap in Atlas's
    coverage rather than as a property of the object kind.
    """
    if definition is None:
        if not defines_itself:
            return ["* Definition: a base table has none; its shape is the schema above."]
        return [f"* Definition: not captured (`{DEFINITION_ABSENT}`)."]
    lines: list[str] = []
    if definition.available:
        lines.append("* Definition: captured and releasable through Atlas; not included here.")
    else:
        codes = ", ".join(f"`{code}`" for code in definition.reason_codes) or (
            f"`{DEFINITION_WITHHELD}`"
        )
        lines.append(f"* Definition: withheld ({codes}).")
    if definition.digest is not None:
        lines.append(
            f"* Definition digest (SHA-256 of the stored value-free text): "
            f"`{definition.digest}`."
        )
    else:
        lines.append("* Definition digest: none, so a change cannot be detected from here.")
    if definition.capture_version is not None:
        captured = f" captured at {definition.captured_at}" if definition.captured_at else ""
        lines.append(f"* Capture version: {definition.capture_version}{captured}.")
    if definition.truncated:
        lines.append(
            f"* The captured text was truncated at the source boundary "
            f"(`{DEFINITION_TRUNCATED}`); coverage below is partial."
        )
    lines.append(f"* Reviewed lineage: {definition.lineage}.")
    return lines


def _limitations_section(limitations: Sequence[str]) -> list[str]:
    if not limitations:
        return []
    lines = ["# Limitations", ""]
    lines.extend(f"* {limitation}" for limitation in sorted(set(limitations)))
    lines.append("")
    return lines


# --- document builders ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ColumnSet:
    """One column-set document of a wide object: an ordinal range and the columns in it."""

    path: str
    label: str
    first_ordinal: int
    last_ordinal: int
    columns: tuple[OkfColumnFacts, ...]


def _column_sets(obj: OkfObjectFacts, document_path: str) -> tuple[_ColumnSet, ...]:
    """Split a wide object's columns into documents of at most `MAX_COLUMNS_PER_DOCUMENT`.

    Grouped by *ordinal range*, not by position in the list: set `n` holds ordinals
    `(n-1)*limit+1 .. n*limit`. A column appended at the source lands in the last set and a
    dropped one leaves a gap, so neither renames or rewrites the sets before it -- the stable
    structural split the design asks for, and what keeps an incremental rebuild small. A range
    that is over the limit anyway (ordinals the source did not report, or reported twice) is
    split again by position, as `columns-<n>-<m>`, so no document ever exceeds the limit.
    """
    if len(obj.columns) <= MAX_COLUMNS_PER_DOCUMENT:
        return ()
    ordered = sorted(obj.columns, key=lambda column: (column.ordinal, column.name))
    ranges: dict[int, list[OkfColumnFacts]] = {}
    for column in ordered:
        ranges.setdefault(max(column.ordinal - 1, 0) // MAX_COLUMNS_PER_DOCUMENT, []).append(
            column
        )
    stem = document_path.removesuffix(".md")
    sets: list[_ColumnSet] = []
    for index in sorted(ranges):
        members = ranges[index]
        chunks = [
            members[start : start + MAX_COLUMNS_PER_DOCUMENT]
            for start in range(0, len(members), MAX_COLUMNS_PER_DOCUMENT)
        ]
        for position, chunk in enumerate(chunks, start=1):
            suffix = f"{index + 1}" if len(chunks) == 1 else f"{index + 1}-{position}"
            first, last = chunk[0].ordinal, chunk[-1].ordinal
            sets.append(
                _ColumnSet(
                    path=f"{stem}-columns-{suffix}.md",
                    label=f"Columns {first}-{last}",
                    first_ordinal=first,
                    last_ordinal=last,
                    columns=tuple(chunk),
                )
            )
    return tuple(sets)


@dataclass(slots=True)
class _Paths:
    """Every document path in the bundle, computed once so links and indexes agree."""

    sources: dict[str, str] = field(default_factory=dict)
    schemas: dict[str, str] = field(default_factory=dict)
    objects: dict[str, str] = field(default_factory=dict)
    routines: dict[str, str] = field(default_factory=dict)
    packages: dict[str, str] = field(default_factory=dict)
    concepts: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    #: Object key -> its column sets, empty for an object that fits in one document.
    column_sets: dict[str, tuple[_ColumnSet, ...]] = field(default_factory=dict)

    def any_path(self, key: str) -> str | None:
        for table in (self.objects, self.routines, self.packages, self.concepts, self.tools):
            if key in table:
                return table[key]
        return None


def _source_dir(key: str) -> str:
    return f"sources/source-{key}"


def _schema_dir(source: str, schema: str) -> str:
    return f"{_source_dir(source)}/schemas/schema-{schema}"


def _resolve_paths(snapshot: OkfSnapshot) -> _Paths:
    paths = _Paths()
    for source in snapshot.sources:
        paths.sources[source.key] = f"{_source_dir(source.key)}/index.md"
    for schema in snapshot.schemas:
        paths.schemas[schema.key] = f"{_schema_dir(schema.source_key, schema.key)}/index.md"
    for obj in snapshot.objects:
        folder = "tables" if obj.kind == KIND_TABLE else "views"
        prefix = "table" if obj.kind == KIND_TABLE else "view"
        base = _schema_dir(obj.source_key, obj.schema_key)
        paths.objects[obj.key] = f"{base}/{folder}/{prefix}-{obj.key}.md"
        paths.column_sets[obj.key] = _column_sets(obj, paths.objects[obj.key])
    for routine in snapshot.routines:
        base = _schema_dir(routine.source_key, routine.schema_key)
        paths.routines[routine.key] = f"{base}/routines/routine-{routine.key}.md"
    for package in snapshot.packages:
        base = _schema_dir(package.source_key, package.schema_key)
        paths.packages[package.key] = f"{base}/packages/package-{package.key}.md"
    for concept in snapshot.concepts:
        paths.concepts[concept.key] = f"concepts/concept-{concept.key}.md"
    for tool in snapshot.tools:
        paths.tools[tool.key] = f"tools/tool-version-{tool.key}.md"
    return paths


def _object_resource(obj: OkfObjectFacts) -> str:
    kind = obj.kind.lower()
    return f"atlas://source/{obj.source_key}/schema/{obj.schema_key}/{kind}/{obj.key}"


def _routine_resource(routine: OkfRoutineFacts) -> str:
    base = f"atlas://source/{routine.source_key}/schema/{routine.schema_key}"
    return f"{base}/routine/{routine.key}"


def _link_lines(
    links: Sequence[OkfLink], paths: _Paths, labels: Mapping[str, str], heading: str
) -> list[str]:
    """Dependencies as links plus the relation in prose.

    A link whose target is not in the bundle is a build failure rather than a rendered dead
    end: OKF consumers must tolerate broken links, but Atlas must not publish one, and a
    reference to an object the reader was not admitted to was dropped long before this.
    """
    if not links:
        return []
    lines = [heading, ""]
    rendered: list[str] = []
    for link in links:
        path = paths.any_path(link.target_key)
        if path is None:
            raise OkfExportError(
                f"link target {link.target_key!r} is not in the bundle; drop the reference "
                "when freezing the snapshot rather than publishing a dead link"
            )
        label = labels.get(link.target_key, link.target_key)
        rendered.append(f"* [{label}]({_absolute(path)}) - {link.relation}")
    lines.extend(sorted(set(rendered)))
    lines.append("")
    return lines


def _object_document(
    obj: OkfObjectFacts,
    snapshot: OkfSnapshot,
    paths: _Paths,
    labels: Mapping[str, str],
    dialect: str,
) -> OkfDocument:
    concept_type = {
        KIND_TABLE: TYPE_TABLE,
        KIND_VIEW: TYPE_VIEW,
        KIND_MATERIALIZED_VIEW: TYPE_MATERIALIZED_VIEW,
    }.get(obj.kind, TYPE_TABLE)
    resource = _object_resource(obj)
    frontmatter: dict[str, Any] = {
        "type": concept_type,
        "title": obj.qualified_name,
        "resource": resource,
        "status": _okf_status(obj.lifecycle, obj.description),
        "tags": sorted({"atlas", obj.kind.lower().replace("_", "-"), dialect.lower()}),
    }
    if obj.description.state == DESCRIPTION_APPROVED and obj.description.text:
        frontmatter["description"] = _first_sentence(obj.description.text)
    moment = _content_moment(
        obj.description.approval.at if obj.description.approval else None,
        obj.definition.captured_at if obj.definition else None,
    )
    generated: dict[str, str] = {"by": EXPORTER_ACTOR}
    if moment is not None:
        generated["at"] = moment
    frontmatter["generated"] = generated
    sources = _sources_entries(
        object_resource=resource, description=obj.description, definition=obj.definition
    )
    if sources:
        frontmatter["sources"] = sources
    frontmatter["atlas"] = _atlas_extension(
        snapshot,
        object_kind=obj.kind,
        native_object_type=obj.native_object_type,
        key=obj.key,
        qualified_name=obj.qualified_name,
        lifecycle=obj.lifecycle,
        dialect=dialect,
        description=obj.description,
        definition=obj.definition,
        approved_statements=("purpose",) if obj.description.state == DESCRIPTION_APPROVED else (),
        derived_statements=("columns", "dependencies", "coverage"),
    )
    column_sets = paths.column_sets.get(obj.key, ())
    if column_sets:
        frontmatter["atlas"]["column_sets"] = [
            {
                "path": item.path,
                "first_ordinal": item.first_ordinal,
                "last_ordinal": item.last_ordinal,
                "columns": len(item.columns),
            }
            for item in column_sets
        ]
    body: list[str] = []
    body.extend(_purpose_section(obj.description, "object"))
    body.extend(
        _column_set_index(obj, column_sets) if column_sets else _schema_section(obj)
    )
    body.extend(_link_lines(obj.links, paths, labels, "# Dependencies"))
    body.extend(["# Coverage", ""])
    body.append(f"* Columns captured: {len(obj.columns)}.")
    if column_sets:
        body.append(
            f"* Published as {len(column_sets)} column sets of at most "
            f"{MAX_COLUMNS_PER_DOCUMENT} columns each."
        )
    body.extend(_definition_lines(obj.definition, defines_itself=obj.kind != KIND_TABLE))
    body.extend(
        [
            "",
            "Source read times are recorded in the Atlas manifest beside this bundle, not "
            "here: a completed scan that changed nothing must not change this document.",
            "",
        ]
    )
    body.extend(_limitations_section(obj.limitations))
    body.extend(_description_footnote(obj.description))
    return OkfDocument(path=paths.objects[obj.key], text=_document_text(frontmatter, body))


def _schema_section(obj: OkfObjectFacts) -> list[str]:
    """Spec §4.2's conventional `# Schema` heading, one row per column.

    Columns stay sections of their owning table, as the design requires. No default
    expression, no sample value, no statistic: the column's shape and its approved meaning.
    """
    if not obj.columns:
        return ["# Schema", "", "No columns are captured for this object.", ""]
    return ["# Schema", "", *_column_rows(obj.columns), ""]


def _column_set_index(obj: OkfObjectFacts, column_sets: Sequence[_ColumnSet]) -> list[str]:
    """A wide object's `# Schema`: every column *name*, grouped by the set that describes it.

    Names only, so the object's own document stays readable at any width, and every name, so a
    reader browsing files -- rather than asking Atlas for context -- can still tell which set to
    open for the column it needs. Types, classifications and approved meanings are in the sets.
    """
    lines = [
        "# Schema",
        "",
        f"This {obj.kind.lower().replace('_', ' ')} has {len(obj.columns)} columns, published "
        f"in {len(column_sets)} column sets so each can be read on its own. Open the set that "
        "names a column for its type, classification and approved description.",
        "",
    ]
    for item in column_sets:
        names = ", ".join(f"`{column.name}`" for column in item.columns)
        lines.append(
            f"* [{item.label}]({_absolute(item.path)}) - {len(item.columns)} columns: {names}"
        )
    lines.append("")
    return lines


def _column_rows(columns: Sequence[OkfColumnFacts]) -> list[str]:
    """Spec §4.2's schema table rows, shared by an object's own document and its column sets."""
    lines = [
        "| Column | Type | Nullable | Classification | Description |",
        "|---|---|---|---|---|",
    ]
    for column in sorted(columns, key=lambda item: (item.ordinal, item.name)):
        if column.description.state == DESCRIPTION_APPROVED and column.description.text:
            meaning = " ".join(column.description.text.split())
        elif column.description.state == DESCRIPTION_WITHHELD:
            meaning = "_withheld by export screening_"
        else:
            meaning = "_not established_"
        lifecycle = "" if column.lifecycle.upper() == "ACTIVE" else f" ({column.lifecycle})"
        lines.append(
            f"| `{column.name}`{lifecycle} | `{column.physical_type}` | "
            f"{'yes' if column.nullable else 'no'} | {column.classification} | "
            f"{meaning.replace('|', '\\|')} |"
        )
    return lines


def _column_set_document(
    obj: OkfObjectFacts,
    column_set: _ColumnSet,
    snapshot: OkfSnapshot,
    paths: _Paths,
    dialect: str,
) -> OkfDocument:
    """One column set of a wide object: its rows in full, and the way back to the object.

    Self-contained on purpose. An agent handed this one document -- by Atlas's own context
    retrieval, or by opening the file -- learns which object, which part of how many, and which
    ordinal range it is reading, and can reach the object's purpose and the neighbouring sets
    by link, without having read the object's document first.
    """
    sets = paths.column_sets[obj.key]
    number = sets.index(column_set) + 1
    frontmatter: dict[str, Any] = {
        "type": TYPE_COLUMN_SET,
        "title": f"{obj.qualified_name}: {column_set.label.lower()}",
        # The set's own count only, never the object's total: a column appended elsewhere must
        # not rewrite this document (the stable split `_column_sets` promises).
        "description": (
            f"{column_set.label} of {obj.qualified_name}: {len(column_set.columns)} columns."
        ),
        "status": _okf_status(obj.lifecycle, obj.description),
        "tags": sorted(
            {"atlas", "column-set", obj.kind.lower().replace("_", "-"), dialect.lower()}
        ),
    }
    moment = _content_moment(
        *(
            column.description.approval.at
            for column in column_set.columns
            if column.description.approval is not None
        )
    )
    generated: dict[str, str] = {"by": EXPORTER_ACTOR}
    if moment is not None:
        generated["at"] = moment
    frontmatter["generated"] = generated
    frontmatter["atlas"] = {
        "profile": f"{snapshot.profile}/{snapshot.profile_version}",
        "part_of": {
            "key": obj.key,
            "path": paths.objects[obj.key],
            "qualified_name": obj.qualified_name,
            "set": number,
            "sets": len(sets),
            "first_ordinal": column_set.first_ordinal,
            "last_ordinal": column_set.last_ordinal,
        },
        "statements": {"approved": [], "derived": ["columns"]},
        "scope": _scope_extension(snapshot),
    }
    kind = obj.kind.lower().replace("_", " ")
    body = [
        f"Column set {number} of {len(sets)} of the {kind} "
        f"[{obj.qualified_name}]({_absolute(paths.objects[obj.key])}), which carries its "
        "purpose, dependencies and coverage.",
        "",
        "# Schema",
        "",
        *_column_rows(column_set.columns),
        "",
    ]
    # Neighbours by set number, not by their ordinal ranges: a column appended to the next set
    # changes its range, and must not rewrite this document.
    neighbours: list[str] = []
    if number > 1:
        before = sets[number - 2]
        neighbours.append(f"* Previous: [Column set {number - 1}]({_absolute(before.path)})")
    if number < len(sets):
        after = sets[number]
        neighbours.append(f"* Next: [Column set {number + 1}]({_absolute(after.path)})")
    if neighbours:
        body.extend(["# Other column sets", "", *neighbours, ""])
    return OkfDocument(path=column_set.path, text=_document_text(frontmatter, body))


def _routine_document(
    routine: OkfRoutineFacts,
    snapshot: OkfSnapshot,
    paths: _Paths,
    labels: Mapping[str, str],
    dialect: str,
) -> OkfDocument:
    resource = _routine_resource(routine)
    frontmatter: dict[str, Any] = {
        "type": TYPE_ROUTINE,
        "title": routine.qualified_name,
        "resource": resource,
        "status": _okf_status(routine.lifecycle, routine.description),
        "tags": sorted({"atlas", "routine", routine.routine_type.lower(), dialect.lower()}),
    }
    if routine.description.state == DESCRIPTION_APPROVED and routine.description.text:
        frontmatter["description"] = _first_sentence(routine.description.text)
    moment = _content_moment(
        routine.description.approval.at if routine.description.approval else None,
        routine.definition.captured_at if routine.definition else None,
    )
    generated: dict[str, str] = {"by": EXPORTER_ACTOR}
    if moment is not None:
        generated["at"] = moment
    frontmatter["generated"] = generated
    sources = _sources_entries(
        object_resource=resource,
        description=routine.description,
        definition=routine.definition,
    )
    if sources:
        frontmatter["sources"] = sources
    extension = _atlas_extension(
        snapshot,
        object_kind="ROUTINE",
        native_object_type=routine.native_subtype or routine.routine_type,
        key=routine.key,
        qualified_name=routine.qualified_name,
        lifecycle=routine.lifecycle,
        dialect=dialect,
        description=routine.description,
        definition=routine.definition,
        approved_statements=(
            ("purpose",) if routine.description.state == DESCRIPTION_APPROVED else ()
        ),
        derived_statements=("interface", "reads-and-writes", "coverage"),
    )
    extension["routine"] = {
        "routine_type": routine.routine_type,
        "signature": routine.signature,
        "package_name": routine.package_name,
        "language": routine.language,
        "return_type": routine.return_type,
        "is_deterministic": routine.is_deterministic,
        "security_mode": routine.security_mode,
    }
    frontmatter["atlas"] = extension
    body: list[str] = []
    body.extend(_purpose_section(routine.description, "routine"))
    body.extend(_interface_section(routine))
    body.extend(_link_lines(routine.links, paths, labels, "# Reads and writes"))
    body.extend(["# Coverage", ""])
    body.extend(_definition_lines(routine.definition))
    body.extend(
        [
            "",
            "The routine body is never exported. Atlas releases code text only through its "
            "own authorized read, and a bundle is a file that cannot be revoked.",
            "",
        ]
    )
    body.extend(_limitations_section(routine.limitations))
    body.extend(_description_footnote(routine.description))
    return OkfDocument(path=paths.routines[routine.key], text=_document_text(frontmatter, body))


def _interface_section(routine: OkfRoutineFacts) -> list[str]:
    lines = ["# Interface", ""]
    lines.append(f"* Kind: {routine.routine_type}.")
    if routine.native_subtype:
        lines.append(f"* Native subtype: `{routine.native_subtype}`.")
    lines.append(f"* Signature: `{routine.signature or '()'}`.")
    if routine.package_name:
        lines.append(f"* Package member of `{routine.package_name}`.")
    if routine.language:
        lines.append(f"* Language: `{routine.language}`.")
    if routine.return_type:
        lines.append(f"* Returns: `{routine.return_type}`.")
    if routine.is_deterministic is not None:
        lines.append(f"* Deterministic: {'yes' if routine.is_deterministic else 'no'}.")
    if routine.security_mode:
        lines.append(f"* Security mode: `{routine.security_mode}`.")
    lines.append("")
    if routine.parameters:
        lines.extend(
            [
                "| Parameter | Mode | Type |",
                "|---|---|---|",
            ]
        )
        for parameter in sorted(routine.parameters, key=lambda item: item.ordinal):
            lines.append(
                f"| `{parameter.name}` | {parameter.mode} | `{parameter.physical_type}` |"
            )
        lines.append("")
    else:
        lines.extend(["No parameters are captured for this routine.", ""])
    return lines


def _package_document(
    package: OkfPackageFacts,
    snapshot: OkfSnapshot,
    paths: _Paths,
    labels: Mapping[str, str],
    dialect: str,
) -> OkfDocument:
    frontmatter: dict[str, Any] = {
        "type": TYPE_PACKAGE,
        "title": package.qualified_name,
        "resource": (
            f"atlas://source/{package.source_key}/schema/{package.schema_key}"
            f"/package/{package.key}"
        ),
        "status": _okf_status(package.lifecycle, package.description),
        "tags": sorted({"atlas", "package", dialect.lower()}),
        "generated": (
            {"by": EXPORTER_ACTOR, "at": package.description.approval.at}
            if package.description.approval
            else {"by": EXPORTER_ACTOR}
        ),
    }
    if package.description.state == DESCRIPTION_APPROVED and package.description.text:
        frontmatter["description"] = _first_sentence(package.description.text)
    frontmatter["atlas"] = _atlas_extension(
        snapshot,
        object_kind="PACKAGE",
        native_object_type="PACKAGE",
        key=package.key,
        qualified_name=package.qualified_name,
        lifecycle=package.lifecycle,
        dialect=dialect,
        description=package.description,
        definition=None,
        approved_statements=(
            ("purpose",) if package.description.state == DESCRIPTION_APPROVED else ()
        ),
        derived_statements=("members",),
    )
    body = list(_purpose_section(package.description, "package"))
    members = tuple(
        OkfLink(target_key=key, relation="member subprogram") for key in package.member_keys
    )
    body.extend(_link_lines(members, paths, labels, "# Members"))
    body.extend(_description_footnote(package.description))
    return OkfDocument(path=paths.packages[package.key], text=_document_text(frontmatter, body))


def _concept_document(
    concept: OkfConceptFacts,
    snapshot: OkfSnapshot,
    paths: _Paths,
    labels: Mapping[str, str],
) -> OkfDocument:
    """A business concept, and the one document kind that can carry document-level `verified`.

    Every statement in it -- the definition, the mappings, the relations -- is content of the
    approved ontology version the product is pinned to, so a recorded approval of that version
    covers the whole document. A catalog object's document never qualifies, because its columns
    and lineage are captured rather than reviewed.
    """
    # `lifecycle` is the ontology definition's own -- ACTIVE or DEPRECATED (`ontology_api`) --
    # while the version is APPROVED by construction: `load_ontology_meaning` reads no other, and
    # `approval` is its recorded governance decision. Until 2026-09-18 this demanded
    # lifecycle == "APPROVED", a value a definition never holds, so every concept a real product
    # exported read as draft and unverified however it had been approved.
    fully_approved = (
        concept.approval is not None and concept.lifecycle.upper() != "DEPRECATED"
    )
    frontmatter: dict[str, Any] = {
        "type": TYPE_CONCEPT,
        "title": concept.label or concept.name,
        "resource": (
            f"atlas://ontology/{concept.ontology_key}/v{concept.ontology_version}"
            f"/concept/{concept.key}"
        ),
        "status": "stable" if fully_approved else "draft",
        "tags": sorted({"atlas", "business-concept", concept.ontology_key.lower()}),
        "generated": (
            {"by": EXPORTER_ACTOR, "at": concept.approval.at}
            if concept.approval
            else {"by": EXPORTER_ACTOR}
        ),
    }
    if concept.definition:
        frontmatter["description"] = _first_sentence(concept.definition)
    if fully_approved:
        verified = _verification(concept.approval)
        if verified:
            frontmatter["verified"] = verified
    statements = ["definition", "mappings", "relations", *(["aliases"] if concept.aliases else [])]
    atlas: dict[str, Any] = {
        "profile": f"{snapshot.profile}/{snapshot.profile_version}",
        "concept": {
            "key": concept.key,
            "name": concept.name,
            "ontology_key": concept.ontology_key,
            "ontology_version": concept.ontology_version,
            "lifecycle": concept.lifecycle,
        },
        "statements": {
            "approved": sorted(statements) if fully_approved else [],
            "derived": [] if fully_approved else sorted(statements),
        },
        "scope": _scope_extension(snapshot),
    }
    if concept.withheld_reason_codes:
        atlas["withheld"] = {"reason_codes": list(concept.withheld_reason_codes)}
    frontmatter["atlas"] = atlas
    body = ["# Definition", ""]
    if concept.definition:
        body.extend([concept.definition.strip(), ""])
    elif concept.withheld_reason_codes:
        codes = ", ".join(f"`{code}`" for code in concept.withheld_reason_codes)
        body.extend([f"Withheld by export screening ({codes}).", ""])
    else:
        body.extend(["Not established. The approved ontology version carries no "
                     "definition for this concept.", ""])
    if concept.aliases:
        body.extend(["# Also called", ""])
        body.extend(f"* {' '.join(alias.split())}" for alias in concept.aliases)
        body.append("")
    mapped = tuple(
        OkfLink(target_key=key, relation="mapped object")
        for key in (*concept.mapped_object_keys, *concept.mapped_routine_keys)
    )
    body.extend(_link_lines(mapped, paths, labels, "# Mapped objects"))
    if concept.relations:
        body.extend(["# Related concepts", ""])
        for relation in sorted(
            concept.relations, key=lambda item: (item.predicate, item.target_name)
        ):
            path = paths.concepts.get(relation.target_key or "")
            if path is not None:
                body.append(
                    f"* {relation.predicate}: [{relation.target_name}]({_absolute(path)})"
                )
            else:
                body.append(
                    f"* {relation.predicate}: {relation.target_name} "
                    "(not in this bundle's scope)"
                )
        body.append("")
    return OkfDocument(path=paths.concepts[concept.key], text=_document_text(frontmatter, body))


def _scope_extension(snapshot: OkfSnapshot) -> dict[str, Any]:
    scope = snapshot.scope
    return {
        "kind": scope.kind,
        "product_key": scope.product_key,
        "product_version": scope.product_version,
        "policy_partition_digest": _policy_partition_digest(scope.policy_partition),
    }


def _atlas_extension(
    snapshot: OkfSnapshot,
    *,
    object_kind: str,
    native_object_type: str,
    key: str,
    qualified_name: str,
    lifecycle: str,
    dialect: str,
    description: OkfDescription,
    definition: OkfDefinitionFacts | None,
    approved_statements: Sequence[str],
    derived_statements: Sequence[str],
) -> dict[str, Any]:
    """The documented `atlas` extension (spec §4.1 permits producer keys; §11 forbids
    consumers rejecting them). Its contract is `Docs/90-reference/okf-export-profile.md`, and
    `tests/test_okf_export.py` fails if the two disagree.

    `statements` is the field that keeps this honest: it names which parts of the document an
    Atlas approval covers and which are derived, which is the per-statement answer OKF's
    single document-level `verified` cannot give.
    """
    extension: dict[str, Any] = {
        "profile": f"{snapshot.profile}/{snapshot.profile_version}",
        "object": {
            "key": key,
            "kind": object_kind,
            "native_object_type": native_object_type,
            "qualified_name": qualified_name,
            "engine": dialect,
            "lifecycle": lifecycle,
        },
        "description": {
            "state": description.state,
            "version": description.version,
        },
        "statements": {
            "approved": sorted(approved_statements),
            "derived": sorted(derived_statements),
        },
        "scope": _scope_extension(snapshot),
    }
    if description.approval is not None:
        extension["description"]["approved_by"] = _actor(description.approval)
        extension["description"]["approved_at"] = description.approval.at
    if description.withheld_reason_codes:
        extension["description"]["withheld_reason_codes"] = list(
            description.withheld_reason_codes
        )
    if definition is not None:
        extension["definition"] = {
            "available": definition.available,
            "digest": definition.digest,
            "truncated": definition.truncated,
            "lineage": definition.lineage,
            "capture_version": definition.capture_version,
            "captured_at": definition.captured_at,
            "reason_codes": list(definition.reason_codes),
        }
    return extension


# --- indexes (spec §8) -------------------------------------------------------------------


def _index_document(
    path: str, sections: Sequence[tuple[str, Sequence[str]]], *, root: bool = False
) -> OkfDocument:
    """An `index.md`. No frontmatter, except `okf_version` at the bundle root (spec §8/§12)."""
    lines: list[str] = []
    for heading, entries in sections:
        lines.extend([f"# {heading}", ""])
        lines.extend(entries if entries else ["_Nothing in scope._"])
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    body = "\n".join(lines) + "\n"
    if root:
        return OkfDocument(
            path=path,
            text="---\n" + _yaml_block({"okf_version": OKF_VERSION}) + "---\n\n" + body,
        )
    return OkfDocument(path=path, text=body)


def _entry(label: str, target: str, description: str) -> str:
    return f"* [{label}]({target}) - {description}"


def _root_index(snapshot: OkfSnapshot, paths: _Paths) -> OkfDocument:
    scope = snapshot.scope
    overview = [
        f"* Scope: {scope.kind}.",
        f"* Sources in scope: {len(snapshot.sources)}.",
        f"* Objects in scope: {len(snapshot.objects)} "
        f"(routines {len(snapshot.routines)}, packages {len(snapshot.packages)}).",
        f"* Business concepts: {len(snapshot.concepts)}.",
    ]
    if scope.product_key is not None:
        overview.insert(
            1, f"* Context product: `{scope.product_key}` version {scope.product_version}."
        )
    if scope.product_purpose:
        overview.append(f"* Approved purpose: {scope.product_purpose}")
    overview.extend(
        [
            "* Every count on this page is over the objects the reader was authorized to "
            "see. Nothing outside that authority is counted, linked or named anywhere in "
            "this bundle.",
            "* Source values are not in this bundle. A current figure needs an approved "
            "Atlas tool through the query gateway, and its execution receipt.",
        ]
    )
    if snapshot.tools:
        # R11-OKF02: counted from the admitted tool facts, not from the product's raw pin list,
        # so a tool over a datasource the reader was refused is neither listed nor counted.
        overview.append(
            f"* Approved tool versions in scope: {len(snapshot.tools)}, each described by its "
            "interface and how to invoke it through Atlas. No SQL, executor, runnable code or "
            "credential is exported."
        )
    source_entries = [
        _entry(
            source.name,
            f"{_source_dir(source.key)}/",
            f"{source.dialect} source, "
            f"{sum(1 for schema in snapshot.schemas if schema.source_key == source.key)} "
            f"schema(s) in scope",
        )
        for source in sorted(snapshot.sources, key=lambda item: (item.name, item.key))
    ]
    sections: list[tuple[str, Sequence[str]]] = [
        ("Bundle", overview),
        ("Sources", source_entries),
    ]
    if snapshot.concepts:
        sections.append(
            (
                "Business meaning",
                [_entry("Concepts", "concepts/", f"{len(snapshot.concepts)} approved concept(s)")],
            )
        )
    if snapshot.tools:
        sections.append(
            (
                "Tools",
                [
                    _entry(
                        "Tool versions",
                        "tools/",
                        f"{len(snapshot.tools)} approved tool version(s)",
                    )
                ],
            )
        )
    return _index_document("index.md", sections, root=True)


def _source_index(source: OkfSourceFacts, snapshot: OkfSnapshot, paths: _Paths) -> OkfDocument:
    schemas = sorted(
        (schema for schema in snapshot.schemas if schema.source_key == source.key),
        key=lambda item: (item.qualified_name, item.key),
    )
    overview = [
        f"* Engine: {source.dialect} (connector `{source.connector_type}`).",
        f"* Environment: {source.environment}.",
        f"* Lifecycle: {source.lifecycle}.",
        "* This page is navigational. It describes what Atlas holds about the source, and "
        "does not assert a business purpose for it.",
    ]
    entries = [
        _entry(
            schema.qualified_name,
            f"schemas/schema-{schema.key}/",
            f"{sum(1 for obj in snapshot.objects if obj.schema_key == schema.key)} object(s), "
            f"{sum(1 for routine in snapshot.routines if routine.schema_key == schema.key)} "
            f"routine(s) in scope",
        )
        for schema in schemas
    ]
    return _index_document(
        paths.sources[source.key], [("Source", overview), ("Schemas", entries)]
    )


def _schema_index(schema: OkfSchemaFacts, snapshot: OkfSnapshot, paths: _Paths) -> OkfDocument:
    def label(text: str) -> str:
        return text

    tables = [
        _entry(obj.qualified_name, f"tables/table-{obj.key}.md", _one_line(obj))
        for obj in sorted(snapshot.objects, key=lambda item: item.qualified_name)
        if obj.schema_key == schema.key and obj.kind == KIND_TABLE
    ]
    views = [
        _entry(obj.qualified_name, f"views/view-{obj.key}.md", _one_line(obj))
        for obj in sorted(snapshot.objects, key=lambda item: item.qualified_name)
        if obj.schema_key == schema.key and obj.kind != KIND_TABLE
    ]
    routines = [
        _entry(
            f"{routine.qualified_name}{routine.signature}",
            f"routines/routine-{routine.key}.md",
            _one_line_routine(routine),
        )
        for routine in sorted(
            snapshot.routines, key=lambda item: (item.qualified_name, item.signature)
        )
        if routine.schema_key == schema.key
    ]
    packages = [
        _entry(
            package.qualified_name,
            f"packages/package-{package.key}.md",
            f"{len(package.member_keys)} member(s) in scope",
        )
        for package in sorted(snapshot.packages, key=lambda item: item.qualified_name)
        if package.schema_key == schema.key
    ]
    sections: list[tuple[str, Sequence[str]]] = [
        (
            "Schema",
            [
                f"* Catalog: `{schema.catalog_name}`.",
                f"* Lifecycle: {schema.lifecycle}.",
                "* Navigational only: no business purpose is asserted for a schema.",
            ],
        )
    ]
    if tables:
        sections.append(("Tables", tables))
    if views:
        sections.append(("Views", views))
    if routines:
        sections.append(("Routines", routines))
    if packages:
        sections.append(("Packages", packages))
    return _index_document(paths.schemas[schema.key], sections)


def _one_line(obj: OkfObjectFacts) -> str:
    if obj.description.state == DESCRIPTION_APPROVED and obj.description.text:
        return _first_sentence(obj.description.text, limit=160)
    return f"{obj.native_object_type}, no approved description"


def _one_line_routine(routine: OkfRoutineFacts) -> str:
    if routine.description.state == DESCRIPTION_APPROVED and routine.description.text:
        return _first_sentence(routine.description.text, limit=160)
    return f"{routine.routine_type}, no approved description"


def _concepts_index(snapshot: OkfSnapshot, paths: _Paths) -> OkfDocument:
    entries = [
        _entry(
            concept.label or concept.name,
            f"concept-{concept.key}.md",
            _first_sentence(concept.definition, limit=160)
            if concept.definition
            else f"{concept.ontology_key} v{concept.ontology_version}, no definition released",
        )
        for concept in sorted(snapshot.concepts, key=lambda item: (item.name, item.key))
    ]
    return _index_document("concepts/index.md", [("Business concepts", entries)])


# --- tool versions (R11-OKF02) ----------------------------------------------------------


def _tool_resource(tool: OkfToolFacts) -> str:
    return f"atlas://tool-version/{tool.tool_version_id}"


def _tool_status(tool: OkfToolFacts) -> str:
    """Spec section 5.4 `status` from the tool version's own lifecycle: a published version is
    `stable`, a superseded or deprecated one `deprecated`, anything unreviewed `draft`."""
    lifecycle = tool.lifecycle.upper()
    if lifecycle == "PUBLISHED":
        return "stable"
    if lifecycle in {"SUPERSEDED", "DEPRECATED", "RETIRED", "SUPPORTED"}:
        return "deprecated"
    return "draft"


def _tool_document(
    tool: OkfToolFacts,
    snapshot: OkfSnapshot,
    paths: _Paths,
    source_names: Mapping[str, str],
    dialect: str,
) -> OkfDocument:
    """One approved tool version: what it is for, what it takes, and how to call it via Atlas.

    Never the tool's SQL, and never an `executor` or `computation` field: those are what make
    upstream's attested-computation type runnable on import, and the design is explicit that an
    exported tool is "invocation through Atlas; never credentials or runnable embedded code
    auto-executed by import". A reader learns the contract and is told where to execute it.
    """
    resource = _tool_resource(tool)
    frontmatter: dict[str, Any] = {
        "type": TYPE_TOOL_VERSION,
        "title": f"{tool.name} (version {tool.version})",
        "resource": resource,
        "status": _tool_status(tool),
        "tags": sorted({"atlas", "tool-version", dialect.lower()}),
    }
    if tool.description.state == DESCRIPTION_APPROVED and tool.description.text:
        frontmatter["description"] = _first_sentence(tool.description.text)
    generated: dict[str, str] = {"by": EXPORTER_ACTOR}
    if tool.description.approval is not None:
        generated["at"] = tool.description.approval.at
    frontmatter["generated"] = generated
    if tool.description.state == DESCRIPTION_APPROVED:
        entry: dict[str, Any] = {
            "id": "approved-tool-version",
            "resource": resource,
            "title": f"Approved Atlas tool version {tool.version}",
        }
        if tool.description.approval is not None:
            entry["author"] = _actor(tool.description.approval)
            entry["last_modified"] = tool.description.approval.at
        frontmatter["sources"] = [entry]
    approved = tool.description.state == DESCRIPTION_APPROVED
    atlas: dict[str, Any] = {
        "profile": f"{snapshot.profile}/{snapshot.profile_version}",
        "tool": {
            "key": tool.key,
            "tool_version_id": tool.tool_version_id,
            "slug": tool.slug,
            "version": tool.version,
            "lifecycle": tool.lifecycle,
            "fingerprint": tool.fingerprint,
            "invocation": {
                "mcp_tool": f"atlas__{tool.slug}",
                "rest": f"POST /v1/tool-versions/{tool.tool_version_id}/execute",
            },
        },
        "description": {"state": tool.description.state, "version": tool.description.version},
        "statements": {
            "approved": ["interface", "purpose"] if approved else [],
            "derived": ["source"] if approved else ["interface", "purpose", "source"],
        },
        "scope": _scope_extension(snapshot),
    }
    if tool.description.approval is not None:
        atlas["description"]["approved_by"] = _actor(tool.description.approval)
        atlas["description"]["approved_at"] = tool.description.approval.at
    if tool.description.withheld_reason_codes:
        atlas["description"]["withheld_reason_codes"] = list(
            tool.description.withheld_reason_codes
        )
    frontmatter["atlas"] = atlas
    body: list[str] = []
    body.extend(_purpose_section(tool.description, "tool version"))
    body.extend(["# Interface", ""])
    if tool.inputs:
        body.extend(["| Input | Type | Required |", "|---|---|---|"])
        for item in sorted(tool.inputs, key=lambda entry: entry.name):
            body.append(
                f"| `{item.name}` | `{item.physical_type}` | {'yes' if item.required else 'no'} |"
            )
        body.append("")
    else:
        body.extend(["This tool version declares no inputs.", ""])
    body.extend(
        [
            "# Invocation",
            "",
            f"* Through Atlas only: the MCP tool `atlas__{tool.slug}`, or "
            f"`POST /v1/tool-versions/{tool.tool_version_id}/execute`.",
            "* Every call goes through the Atlas query gateway, which applies the caller's "
            "authorization, masking, row limits, cost gates and audit, and returns an execution "
            "receipt to cite.",
            "* This document contains no SQL, no executor and no credential. There is nothing "
            "in it to run.",
            "",
        ]
    )
    source_path = paths.sources.get(tool.source_key)
    if source_path is not None:
        label = source_names.get(tool.source_key, tool.source_key)
        body.extend(["# Source", "", f"* [{label}]({_absolute(source_path)}) - queries", ""])
    if tool.description.state == DESCRIPTION_APPROVED:
        approval = tool.description.approval
        attribution = (
            f", approved by {_actor(approval)} at {approval.at}" if approval is not None else ""
        )
        footnote = f"Approved Atlas tool version {tool.version}{attribution}."
        body.extend([f"[^approved-description]: {footnote}", ""])
    return OkfDocument(path=paths.tools[tool.key], text=_document_text(frontmatter, body))


def _tools_index(snapshot: OkfSnapshot) -> OkfDocument:
    entries = [
        _entry(
            f"{tool.name} (version {tool.version})",
            f"tool-version-{tool.key}.md",
            _first_sentence(tool.description.text, limit=160)
            if tool.description.state == DESCRIPTION_APPROVED and tool.description.text
            else f"{tool.lifecycle.lower()} tool version, no approved description",
        )
        for tool in sorted(snapshot.tools, key=lambda item: (item.name, item.version, item.key))
    ]
    return _index_document("tools/index.md", [("Tool versions", entries)])


# --- refresh history (R11-OKF02) --------------------------------------------------------


def _log_paths(label: str, paths: Sequence[str]) -> list[str]:
    """Paths as code spans, never links: an older entry names documents a later publication may
    have removed, and a link to one would be exactly the dead end the publish policy refuses."""
    if not paths:
        return []
    shown = sorted(paths)[:MAX_LOG_PATHS]
    lines = [f"  - {label}: " + ", ".join(f"`{path}`" for path in shown)]
    if len(paths) > len(shown):
        lines.append(f"  - {label}, not listed: {len(paths) - len(shown)} more")
    return lines


def _scoped_entry(entry: OkfLogEntry, prefix: str) -> OkfLogEntry | None:
    """The part of one entry that moved a document under `prefix`, or None if nothing did."""
    added = tuple(item for item in entry.added if item.startswith(prefix))
    changed = tuple(item for item in entry.changed if item.startswith(prefix))
    removed = tuple(item for item in entry.removed if item.startswith(prefix))
    if entry.trigger != TRIGGER_INITIAL and not (added or changed or removed):
        return None
    return OkfLogEntry(
        date=entry.date,
        sequence=entry.sequence,
        trigger=entry.trigger,
        added=added,
        changed=changed,
        removed=removed,
        truncated=entry.truncated,
    )


def _log_document(
    path: str, title: str, entries: Sequence[OkfLogEntry], *, prefix: str | None
) -> OkfDocument | None:
    """A spec section 9 `log.md`: ISO date headings, newest first, one bullet per publication.

    `prefix` scopes the log to one source directory: an entry appears there only if it moved a
    document under that directory, so a source untouched by a change keeps the same log bytes --
    and so the same hash -- across a publication that changed a different source.
    """
    scoped: list[OkfLogEntry] = []
    for item in sorted(entries, key=lambda entry: entry.sequence, reverse=True):
        kept = item if prefix is None else _scoped_entry(item, prefix)
        if kept is not None:
            scoped.append(kept)
    if not scoped:
        return None
    listed = scoped[:MAX_LOG_ENTRIES]
    lines = [f"# {title}", ""]
    current_date: str | None = None
    for entry in listed:
        if entry.date != current_date:
            if current_date is not None:
                lines.append("")
            lines.extend([f"## {entry.date}", ""])
            current_date = entry.date
        if entry.trigger == TRIGGER_INITIAL:
            count = f" with {entry.documents} document(s)" if entry.documents else ""
            lines.append(
                f"- **Publication {entry.sequence}** (`{TRIGGER_INITIAL}`): first published"
                f"{count}."
            )
            continue
        lines.append(
            f"- **Publication {entry.sequence}** (`{entry.trigger}`): "
            f"{len(entry.changed)} changed, {len(entry.added)} added, "
            f"{len(entry.removed)} removed."
        )
        lines.extend(_log_paths("changed", entry.changed))
        lines.extend(_log_paths("added", entry.added))
        lines.extend(_log_paths("removed", entry.removed))
        if entry.truncated:
            lines.append(
                f"  - Path lists were cut at {MAX_LOG_STORED_PATHS} entries when stored; the "
                "counts above cover only the listed paths."
            )
    if len(scoped) > len(listed):
        lines.extend(["", f"{len(scoped) - len(listed)} earlier publication(s) not listed."])
    return OkfDocument(path=path, text="\n".join(lines).rstrip() + "\n")


def is_log_path(path: str) -> bool:
    """A refresh history file: the bundle root's `log.md` or a source directory's."""
    return path == "log.md" or path.endswith("/log.md")


def log_entry(
    stamp: OkfPublicationStamp,
    *,
    added: Sequence[str],
    changed: Sequence[str],
    removed: Sequence[str],
    unchanged: int,
    documents: int,
) -> OkfLogEntry:
    """The history entry for one publication. Logs themselves are never listed as changes --
    every publication moves the root log, and listing it would say nothing."""

    def bounded(paths: Sequence[str]) -> tuple[str, ...]:
        return tuple(sorted(path for path in paths if not is_log_path(path))[:MAX_LOG_STORED_PATHS])

    if stamp.trigger == TRIGGER_INITIAL:
        return OkfLogEntry(
            date=stamp.date,
            sequence=stamp.sequence,
            trigger=TRIGGER_INITIAL,
            documents=documents,
        )
    lists = [bounded(added), bounded(changed), bounded(removed)]
    truncated = any(
        len([path for path in source if not is_log_path(path)]) > len(kept)
        for source, kept in zip((added, changed, removed), lists, strict=True)
    )
    return OkfLogEntry(
        date=stamp.date,
        sequence=stamp.sequence,
        trigger=stamp.trigger,
        added=lists[0],
        changed=lists[1],
        removed=lists[2],
        unchanged=unchanged,
        truncated=truncated,
    )


# --- manifest ---------------------------------------------------------------------------


def _policy_partition_digest(partition: OkfPolicyPartition) -> str:
    return _digest_text(_canonical_json(_as_document(partition)))


def _scope_digest(snapshot: OkfSnapshot) -> str:
    """What was in scope and under what policy, as one number.

    Over the authorized keys rather than the rendered bytes: two bundles whose documents happen
    to match but whose scope differs are not the same export, and a reader comparing digests
    needs to see that.
    """
    return _digest_text(
        _canonical_json(
            {
                "kind": snapshot.scope.kind,
                "organization_id": snapshot.scope.organization_id,
                "product_key": snapshot.scope.product_key,
                "product_version": snapshot.scope.product_version,
                "product_fingerprint": snapshot.scope.product_fingerprint,
                "policy_partition": _as_document(snapshot.scope.policy_partition),
                "sources": sorted(source.key for source in snapshot.sources),
                "schemas": sorted(schema.key for schema in snapshot.schemas),
                "objects": sorted(obj.key for obj in snapshot.objects),
                "routines": sorted(routine.key for routine in snapshot.routines),
                "packages": sorted(package.key for package in snapshot.packages),
                "concepts": sorted(concept.key for concept in snapshot.concepts),
                "tools": sorted(tool.key for tool in snapshot.tools),
            }
        )
    )


def _source_object_versions(snapshot: OkfSnapshot) -> list[dict[str, Any]]:
    """The source-object versions this bundle was built from.

    A digest says a definition changed; a capture version says *which* captured definition the
    document describes. Both are needed to tell "the bundle is stale" from "the bundle
    describes an older version on purpose".
    """
    rows: list[dict[str, Any]] = []
    for obj in snapshot.objects:
        rows.append(
            {
                "key": obj.key,
                "kind": obj.kind,
                "qualified_name": obj.qualified_name,
                "lifecycle": obj.lifecycle,
                "definition_digest": obj.definition.digest if obj.definition else None,
                "definition_capture_version": (
                    obj.definition.capture_version if obj.definition else None
                ),
                "description_state": obj.description.state,
                "description_version": obj.description.version,
            }
        )
    for routine in snapshot.routines:
        rows.append(
            {
                "key": routine.key,
                "kind": "ROUTINE",
                "qualified_name": f"{routine.qualified_name}{routine.signature}",
                "lifecycle": routine.lifecycle,
                "definition_digest": routine.definition.digest if routine.definition else None,
                "definition_capture_version": (
                    routine.definition.capture_version if routine.definition else None
                ),
                "description_state": routine.description.state,
                "description_version": routine.description.version,
            }
        )
    return sorted(rows, key=lambda row: (str(row["kind"]), str(row["key"])))


def _manifest(snapshot: OkfSnapshot, documents: Sequence[OkfDocument]) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "atlas_extension": True,
        "okf_version": snapshot.okf_version,
        "bundle_root": BUNDLE_ROOT,
        "specification": {
            "repository": OKF_SPEC_REPOSITORY,
            "path": OKF_SPEC_PATH,
            "revision": snapshot.spec_revision,
            "sha256": OKF_SPEC_SHA256,
            "conformance": OKF_CONFORMANCE_STATUS,
        },
        "compiler": {
            "module": "aida.okf_export",
            "profile": snapshot.profile,
            "profile_version": snapshot.profile_version,
        },
        "captured_at": snapshot.captured_at,
        "scope": {
            "kind": snapshot.scope.kind,
            "organization_id": snapshot.scope.organization_id,
            "product_key": snapshot.scope.product_key,
            "product_version": snapshot.scope.product_version,
            "product_version_id": snapshot.scope.product_version_id,
            "product_fingerprint": snapshot.scope.product_fingerprint,
            "eligible_tool_version_ids": sorted(snapshot.scope.eligible_tool_version_ids),
        },
        "policy_partition": {
            **_as_document(snapshot.scope.policy_partition),
            "digest": _policy_partition_digest(snapshot.scope.policy_partition),
        },
        "scope_digest": _scope_digest(snapshot),
        "content_snapshot_digest": snapshot.content_digest(),
        "bundle_content_digest": _digest_text(
            _canonical_json([[document.path, document.sha256] for document in documents])
        ),
        "counts": {
            "sources": len(snapshot.sources),
            "schemas": len(snapshot.schemas),
            "tables": sum(1 for obj in snapshot.objects if obj.kind == KIND_TABLE),
            "views": sum(1 for obj in snapshot.objects if obj.kind != KIND_TABLE),
            "routines": len(snapshot.routines),
            "packages": len(snapshot.packages),
            "concepts": len(snapshot.concepts),
            "tools": len(snapshot.tools),
            "documents": len(documents),
        },
        "files": [
            {"path": document.path, "sha256": document.sha256, "bytes": document.byte_length}
            for document in documents
        ],
        "source_objects": _source_object_versions(snapshot),
        "source_freshness": [
            _as_document(row)
            for row in sorted(snapshot.freshness, key=lambda item: item.source_key)
        ],
    }


# --- export -----------------------------------------------------------------------------


def _labels(snapshot: OkfSnapshot) -> dict[str, str]:
    labels = {obj.key: obj.qualified_name for obj in snapshot.objects}
    labels.update(
        {
            routine.key: f"{routine.qualified_name}{routine.signature}"
            for routine in snapshot.routines
        }
    )
    labels.update({package.key: package.qualified_name for package in snapshot.packages})
    labels.update({concept.key: concept.label or concept.name for concept in snapshot.concepts})
    labels.update({tool.key: f"{tool.name} (version {tool.version})" for tool in snapshot.tools})
    return labels


def _dialects(snapshot: OkfSnapshot) -> dict[str, str]:
    return {source.key: source.dialect for source in snapshot.sources}


#: Plan kinds. A subject document and a schema index are rendered only when something they
#: depend on moved; the bundle root, source and concept/tool indexes and the logs summarize
#: counts across the whole scope, are few, and are always re-derived -- their bytes, and so
#: their hashes, still only move when what they summarize moved.
_PLAN_SUBJECT: Final = "subject"
_PLAN_SCHEMA_INDEX: Final = "schema-index"
_PLAN_ALWAYS: Final = "always"


@dataclass(frozen=True, slots=True)
class _Planned:
    """One document the bundle will contain, and what its bytes depend on.

    `owns` are identity keys whose *facts* feed the document; `links` are keys whose *label or
    path* it prints. An incremental rebuild re-renders a document only when one of those moved.
    """

    path: str
    subject: str | None
    kind: str
    owns: tuple[str, ...]
    links: tuple[str, ...]
    render: Callable[[], OkfDocument | None]


def _plan(snapshot: OkfSnapshot) -> list[_Planned]:
    """Every content document the snapshot renders to, unrendered. Logs are `_log_plan`."""
    paths = _resolve_paths(snapshot)
    labels = _labels(snapshot)
    dialects = _dialects(snapshot)
    source_names = {source.key: source.name for source in snapshot.sources}
    plan: list[_Planned] = [
        _Planned("index.md", None, _PLAN_ALWAYS, (), (), partial(_root_index, snapshot, paths))
    ]
    for source in snapshot.sources:
        plan.append(
            _Planned(
                paths.sources[source.key],
                None,
                _PLAN_ALWAYS,
                (),
                (),
                partial(_source_index, source, snapshot, paths),
            )
        )
    for schema in snapshot.schemas:
        children = tuple(
            sorted(
                [obj.key for obj in snapshot.objects if obj.schema_key == schema.key]
                + [item.key for item in snapshot.routines if item.schema_key == schema.key]
                + [item.key for item in snapshot.packages if item.schema_key == schema.key]
            )
        )
        plan.append(
            _Planned(
                paths.schemas[schema.key],
                None,
                _PLAN_SCHEMA_INDEX,
                (schema.key, *children),
                (),
                partial(_schema_index, schema, snapshot, paths),
            )
        )
    for obj in snapshot.objects:
        plan.append(
            _Planned(
                paths.objects[obj.key],
                obj.key,
                _PLAN_SUBJECT,
                (obj.key, obj.source_key),
                tuple(link.target_key for link in obj.links),
                partial(_object_document, obj, snapshot, paths, labels, dialects[obj.source_key]),
            )
        )
        # A column set prints only its own object's facts and path, so it moves exactly when
        # the object's document does. It is not a subject document of its own: the object's
        # document stays the one `document_subjects` names.
        for column_set in paths.column_sets.get(obj.key, ()):
            plan.append(
                _Planned(
                    column_set.path,
                    None,
                    _PLAN_SUBJECT,
                    (obj.key, obj.source_key),
                    (),
                    partial(
                        _column_set_document,
                        obj,
                        column_set,
                        snapshot,
                        paths,
                        dialects[obj.source_key],
                    ),
                )
            )
    for routine in snapshot.routines:
        plan.append(
            _Planned(
                paths.routines[routine.key],
                routine.key,
                _PLAN_SUBJECT,
                (routine.key, routine.source_key),
                tuple(link.target_key for link in routine.links),
                partial(
                    _routine_document,
                    routine,
                    snapshot,
                    paths,
                    labels,
                    dialects[routine.source_key],
                ),
            )
        )
    for package in snapshot.packages:
        plan.append(
            _Planned(
                paths.packages[package.key],
                package.key,
                _PLAN_SUBJECT,
                (package.key, package.source_key),
                package.member_keys,
                partial(
                    _package_document,
                    package,
                    snapshot,
                    paths,
                    labels,
                    dialects[package.source_key],
                ),
            )
        )
    if snapshot.concepts:
        plan.append(
            _Planned(
                "concepts/index.md",
                None,
                _PLAN_ALWAYS,
                (),
                (),
                partial(_concepts_index, snapshot, paths),
            )
        )
        for concept in snapshot.concepts:
            related = tuple(
                relation.target_key
                for relation in concept.relations
                if relation.target_key is not None
            )
            plan.append(
                _Planned(
                    paths.concepts[concept.key],
                    concept.key,
                    _PLAN_SUBJECT,
                    (concept.key,),
                    (*concept.mapped_object_keys, *concept.mapped_routine_keys, *related),
                    partial(_concept_document, concept, snapshot, paths, labels),
                )
            )
    if snapshot.tools:
        plan.append(
            _Planned("tools/index.md", None, _PLAN_ALWAYS, (), (), partial(_tools_index, snapshot))
        )
        for tool in snapshot.tools:
            plan.append(
                _Planned(
                    paths.tools[tool.key],
                    tool.key,
                    _PLAN_SUBJECT,
                    (tool.key, tool.source_key),
                    (),
                    partial(
                        _tool_document,
                        tool,
                        snapshot,
                        paths,
                        source_names,
                        dialects[tool.source_key],
                    ),
                )
            )
    return plan


def _log_plan(snapshot: OkfSnapshot, history: Sequence[OkfLogEntry]) -> list[_Planned]:
    """The refresh-history documents: the bundle root's `log.md` and one per source.

    Empty without history -- a bundle rendered straight from a snapshot, as R11-OKF01 did, has
    no refresh history to report, and inventing a first entry would put a clock in it.
    """
    if not history:
        return []
    plan: list[_Planned] = []
    plan.append(
        _Planned(
            "log.md",
            None,
            _PLAN_ALWAYS,
            (),
            (),
            partial(_log_document, "log.md", "Refresh history", history, prefix=None),
        )
    )
    for source in snapshot.sources:
        prefix = f"{_source_dir(source.key)}/"
        plan.append(
            _Planned(
                f"{prefix}log.md",
                None,
                _PLAN_ALWAYS,
                (),
                (),
                partial(
                    _log_document,
                    f"{prefix}log.md",
                    f"Refresh history: {source.name}",
                    history,
                    prefix=prefix,
                ),
            )
        )
    return plan


def document_subjects(snapshot: OkfSnapshot) -> dict[str, str]:
    """Path -> identity key for every document that is *about* one subject.

    Indexes and logs are about a scope rather than a subject, and are absent. Used by the store
    to find "the document about this table" without parsing any document.
    """
    paths = _resolve_paths(snapshot)
    subjects: dict[str, str] = {}
    for table in (paths.objects, paths.routines, paths.packages, paths.concepts, paths.tools):
        subjects.update({path: key for key, path in table.items()})
    return subjects


def column_set_members(snapshot: OkfSnapshot) -> dict[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Object key -> (column-set path, column names) for every object split into column sets.

    Objects that fit in one document are absent. Lets a reader of the bundle -- Atlas's own
    context retrieval -- go from a column name to the one set that describes it without
    opening the object's document or guessing the path scheme.
    """
    paths = _resolve_paths(snapshot)
    return {
        key: tuple((item.path, tuple(column.name for column in item.columns)) for item in sets)
        for key, sets in paths.column_sets.items()
        if sets
    }


def _fact_digests(snapshot: OkfSnapshot) -> dict[str, str]:
    """Identity key -> digest of that subject's frozen facts, for every kind that has a key."""
    digests: dict[str, str] = {}
    for items in (
        snapshot.sources,
        snapshot.schemas,
        snapshot.objects,
        snapshot.routines,
        snapshot.packages,
        snapshot.concepts,
        snapshot.tools,
    ):
        for item in items:
            digests[item.key] = _digest_text(_canonical_json(_as_document(item)))
    return digests


def _identities(snapshot: OkfSnapshot) -> dict[str, tuple[str, str]]:
    """Identity key -> (label, path): what another document prints when it links here."""
    paths = _resolve_paths(snapshot)
    labels = _labels(snapshot)
    identities: dict[str, tuple[str, str]] = {}
    for table in (paths.objects, paths.routines, paths.packages, paths.concepts, paths.tools):
        for key, path in table.items():
            identities[key] = (labels.get(key, key), path)
    return identities


def _renderer_frame(snapshot: OkfSnapshot) -> str:
    """Everything every document carries regardless of its subject: the renderer pin and the
    scope extension. A change here moves every document, so it forces a full render."""
    return _canonical_json(
        {
            "okf_version": snapshot.okf_version,
            "profile": snapshot.profile,
            "profile_version": snapshot.profile_version,
            "spec_revision": snapshot.spec_revision,
            "scope": _scope_extension(snapshot),
        }
    )


def export_okf_bundle(
    snapshot: OkfSnapshot, *, history: Sequence[OkfLogEntry] = ()
) -> OkfBundle:
    """Render one frozen snapshot as an OKF bundle plus an Atlas manifest.

    Pure: no clock, no database, no network, no model. Given the same snapshot (and the same
    refresh `history`, when a stored bundle carries one) it returns the same bytes, which is the
    whole point of the snapshot being a value.

    Raises `OkfExportError` rather than returning a partial bundle -- a colliding identity, a
    dangling reference, an oversized document or a missing parent is a refused export. "Produce
    an explicit size/coverage failure rather than a silently truncated complete bundle" is the
    design's instruction, and the same reasoning covers the other three.
    """
    _validate_snapshot_shape(snapshot)
    plan = [*_plan(snapshot), *_log_plan(snapshot, history)]
    documents = [document for item in plan if (document := item.render()) is not None]
    return _assemble_bundle(snapshot, documents)


def export_okf_bundle_incremental(
    snapshot: OkfSnapshot,
    *,
    prior_snapshot: OkfSnapshot | None,
    prior_documents: Mapping[str, str],
    prior_history: Sequence[OkfLogEntry] = (),
    stamp: OkfPublicationStamp,
) -> tuple[OkfBundle, OkfRebuildReport, tuple[OkfLogEntry, ...]]:
    """R11-OKF02: rebuild a stored bundle, rendering only what a change can have moved.

    Acceptance OKF-C: "a changed view regenerates only affected documents; no-op scans reproduce
    hashes". The dependency roots are the subjects whose frozen facts differ between the prior
    snapshot and this one. From them:

    * a subject document is rendered when its own facts or its source's facts moved, or when a
      document it links to changed label or path -- a link prints both;
    * a schema index is rendered when the schema, or any object, routine or package it lists
      (before or after), moved;
    * the root, source, concept and tool indexes are re-derived every time -- a handful of
      documents that summarize counts over the whole scope;
    * everything else is **carried**: its stored bytes are reused and its builder never runs, so
      its hash is the prior hash by construction rather than by re-rendering to the same bytes.

    The logs come last: the new history entry is computed from what the content documents did,
    then the root and per-source `log.md` are rendered from it. A renderer change (profile,
    spec pin) or a scope change moves every document, so either one forces a full render.

    Returns the bundle, the report and the history the bundle's logs were rendered from. The
    bundle is byte-identical to `export_okf_bundle(snapshot, history=<that history>)`;
    `tests/test_okf_store.py` asserts that across every kind of change, which is what makes
    carrying a document safe rather than hopeful.
    """
    _validate_snapshot_shape(snapshot)
    full = prior_snapshot is None or _renderer_frame(prior_snapshot) != _renderer_frame(
        snapshot
    )
    moved_facts: set[str] = set()
    moved_links: set[str] = set()
    prior_plan: dict[str, _Planned] = {}
    if prior_snapshot is not None and not full:
        before, after = _fact_digests(prior_snapshot), _fact_digests(snapshot)
        moved_facts = {
            key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
        }
        was, now = _identities(prior_snapshot), _identities(snapshot)
        moved_links = {key for key in was.keys() | now.keys() if was.get(key) != now.get(key)}
        # Dependency bookkeeping only: none of the prior plan's builders is ever called.
        prior_plan = {item.path: item for item in _plan(prior_snapshot)}
    documents: list[OkfDocument] = []
    rendered: list[str] = []
    carried: list[str] = []
    for item in _plan(snapshot):
        stored = prior_documents.get(item.path)
        earlier = prior_plan.get(item.path)
        must_render = (
            full
            or item.kind == _PLAN_ALWAYS
            or stored is None
            or earlier is None
            or bool((set(item.owns) | set(earlier.owns)) & moved_facts)
            or bool((set(item.links) | set(earlier.links)) & moved_links)
        )
        if must_render:
            document = item.render()
            if document is None:
                continue
            rendered.append(item.path)
        else:
            assert stored is not None
            document = OkfDocument(path=item.path, text=stored)
            carried.append(item.path)
        documents.append(document)

    content = {document.path: document.text for document in documents}
    prior_content = {
        path: text for path, text in prior_documents.items() if not is_log_path(path)
    }
    added = sorted(path for path in content if path not in prior_content)
    changed = sorted(
        path
        for path, text in content.items()
        if path in prior_content and prior_content[path] != text
    )
    removed = sorted(path for path in prior_content if path not in content)
    entry = log_entry(
        stamp,
        added=added,
        changed=changed,
        removed=removed,
        unchanged=len(content) - len(added) - len(changed),
        documents=len(content),
    )
    history = tuple(
        sorted(
            [entry, *(item for item in prior_history if item.sequence != stamp.sequence)],
            key=lambda item: item.sequence,
            reverse=True,
        )[:MAX_LOG_ENTRIES]
    )
    for item in _log_plan(snapshot, history):
        document = item.render()
        if document is not None:
            documents.append(document)
            rendered.append(item.path)
    bundle = _assemble_bundle(snapshot, documents)
    final = {document.path: document.text for document in bundle.documents}
    report = OkfRebuildReport(
        rendered=tuple(sorted(rendered)),
        carried=tuple(sorted(carried)),
        added=tuple(sorted(path for path in final if path not in prior_documents)),
        changed=tuple(
            sorted(
                path
                for path, text in final.items()
                if path in prior_documents and prior_documents[path] != text
            )
        ),
        removed=tuple(sorted(path for path in prior_documents if path not in final)),
        changed_subjects=tuple(sorted(moved_facts)),
        full=full,
    )
    return bundle, report, history


def _assemble_bundle(snapshot: OkfSnapshot, documents: Sequence[OkfDocument]) -> OkfBundle:
    """Bound, order and seal the rendered documents. Shared by the full and incremental paths
    so the size limits and the manifest cannot differ between them."""
    if len(documents) > MAX_DOCUMENTS:
        raise OkfExportError(
            f"{len(documents)} documents exceeds the {MAX_DOCUMENTS}-document bundle limit"
        )
    for document in documents:
        if document.byte_length > MAX_DOCUMENT_BYTES:
            raise OkfExportError(
                f"{document.path} is {document.byte_length} bytes, over the "
                f"{MAX_DOCUMENT_BYTES}-byte document limit"
            )
    ordered = tuple(sorted(documents, key=lambda document: document.path))
    seen: set[str] = set()
    for document in ordered:
        if document.path in seen:
            raise OkfExportError(f"duplicate bundle path {document.path!r}")
        seen.add(document.path)
    total = sum(document.byte_length for document in ordered)
    if total > MAX_BUNDLE_BYTES:
        raise OkfExportError(f"bundle is {total} bytes, over the {MAX_BUNDLE_BYTES}-byte limit")
    return OkfBundle(documents=ordered, manifest=_manifest(snapshot, ordered))


def _validate_snapshot_shape(snapshot: OkfSnapshot) -> None:
    """Refuse a snapshot that cannot render an honest bundle.

    Identity collisions first, because that is the failure the design names twice ("routine
    overloads, source-qualified names and package members must not collide") and the one whose
    symptom -- one document silently overwriting another -- looks like a smaller bundle rather
    than like an error.
    """
    keys: dict[str, str] = {}
    for kind, items in (
        ("source", snapshot.sources),
        ("schema", snapshot.schemas),
        ("object", snapshot.objects),
        ("routine", snapshot.routines),
        ("package", snapshot.packages),
        ("concept", snapshot.concepts),
        ("tool", snapshot.tools),
    ):
        for item in items:
            key = item.key
            if not re.fullmatch(_KEY_PATTERN, key):
                raise OkfExportError(f"{kind} key {key!r} is not a safe opaque segment")
            if key in keys:
                raise OkfExportError(
                    f"identity collision: {kind} and {keys[key]} share the key {key!r}"
                )
            keys[key] = kind
    source_keys = {source.key for source in snapshot.sources}
    schema_keys = {schema.key for schema in snapshot.schemas}
    for schema in snapshot.schemas:
        if schema.source_key not in source_keys:
            raise OkfExportError(f"schema {schema.key!r} names a source not in the snapshot")
    for obj in snapshot.objects:
        if obj.schema_key not in schema_keys or obj.source_key not in source_keys:
            raise OkfExportError(f"object {obj.key!r} names a parent not in the snapshot")
        if obj.kind not in {KIND_TABLE, KIND_VIEW, KIND_MATERIALIZED_VIEW}:
            raise OkfExportError(f"object {obj.key!r} has unsupported kind {obj.kind!r}")
    for routine in snapshot.routines:
        if routine.schema_key not in schema_keys or routine.source_key not in source_keys:
            raise OkfExportError(f"routine {routine.key!r} names a parent not in the snapshot")
    for package in snapshot.packages:
        if package.schema_key not in schema_keys or package.source_key not in source_keys:
            raise OkfExportError(f"package {package.key!r} names a parent not in the snapshot")
    for tool in snapshot.tools:
        if tool.source_key not in source_keys:
            # The freeze admits a tool only when its datasource was admitted; a tool naming an
            # absent source here would be a count of something the reader may not see.
            raise OkfExportError(f"tool {tool.key!r} names a source not in the snapshot")
    for freshness in snapshot.freshness:
        if freshness.source_key not in source_keys:
            raise OkfExportError(
                f"freshness names source {freshness.source_key!r}, which is not in the snapshot"
            )


# --- validation: general OKF conformance, then Atlas's stronger publish policy ----------

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_RESERVED: Final = frozenset({"index.md", "log.md"})
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_REFERENCE_LINK = re.compile(
    r"^[ ]{0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(?:\n[ \t]*)?(?:<([^>\n]+)>|(\S+))",
    re.MULTILINE,
)
# Atlas exports portable text, not active HTML or autolinks whose destinations bypass
# the Markdown link checks. This is a publish policy, not a general OKF restriction.
_RAW_MARKUP = re.compile(r"<(?:/?[A-Za-z][^>]*|![^>]*|\?[^>]*)>", re.DOTALL)
_CODE_FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)
_LOG_DATE = re.compile(r"^## \d{4}-\d{2}-\d{2}$")


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        return None, text
    return match.group(1), text[match.end() :]


def validate_okf_conformance(documents: Mapping[str, str]) -> OkfValidation:
    """The pinned specification's §11 conformance clauses, and nothing stricter.

    Deliberately separate from `validate_atlas_publish_policy`: "Keep general OKF parsing
    distinct from Atlas's stronger publish policy." An upstream reference bundle full of
    external links and code fences is perfectly conformant and must pass here; it would fail
    the Atlas publish policy, and that is a statement about Atlas, not about OKF.

    Checks, in the spec's own order:
      1. every non-reserved `.md` file parses a YAML frontmatter block;
      2. every such block carries a non-empty `type`;
      3. `index.md` and `log.md` follow §8 and §9 -- an index carries no frontmatter except
         `okf_version` at the bundle root, and a log's date headings are ISO `YYYY-MM-DD`.

    Nothing beyond those three, on purpose. An earlier draft also rejected frontmatter on a
    `log.md`, by analogy with the index rule; running this function over the upstream reference
    bundles at the pinned revision showed that was wrong -- `bundles/acme_retail/log.md` carries
    `type` and `title`, §9 forbids no such thing, and §11 tells consumers not to reject unknown
    frontmatter. Verified against all four upstream bundles (78 documents), which now pass.
    """
    findings: list[str] = []
    for path in sorted(documents):
        text = documents[path]
        name = path.rsplit("/", 1)[-1]
        raw, _ = _split_frontmatter(text)
        if name not in _RESERVED:
            if not path.endswith(".md"):
                continue
            if raw is None:
                findings.append(f"FRONTMATTER_MISSING:{path}")
                continue
            try:
                parsed = yaml.safe_load(raw)
            except yaml.YAMLError:
                findings.append(f"FRONTMATTER_UNPARSEABLE:{path}")
                continue
            if not isinstance(parsed, dict):
                findings.append(f"FRONTMATTER_NOT_A_MAPPING:{path}")
                continue
            concept_type = parsed.get("type")
            if not isinstance(concept_type, str) or not concept_type.strip():
                findings.append(f"TYPE_MISSING:{path}")
            continue
        if name == "index.md":
            if raw is None:
                continue
            try:
                parsed = yaml.safe_load(raw)
            except yaml.YAMLError:
                findings.append(f"INDEX_FRONTMATTER_UNPARSEABLE:{path}")
                continue
            if not isinstance(parsed, dict):
                findings.append(f"INDEX_FRONTMATTER_NOT_A_MAPPING:{path}")
                continue
            allowed = {"okf_version"} if path == "index.md" else set()
            extra = sorted(set(parsed) - allowed)
            if extra:
                findings.append(f"INDEX_FRONTMATTER_NOT_ALLOWED:{path}:{','.join(extra)}")
        if name == "log.md":
            # Deliberately *not* checking for absent frontmatter here. §8 forbids it on an
            # index; §9 says nothing of the kind about a log, and §11 tells consumers not to
            # reject unknown frontmatter keys. Checking it was this module's own invention, and
            # the upstream `acme_retail` reference bundle at the pinned revision ships a
            # `log.md` with `type`/`title` frontmatter -- a conformant bundle this checker used
            # to reject. Only the one thing §9 states as a MUST is checked.
            for line in text.splitlines():
                if line.startswith("## ") and not _LOG_DATE.match(line):
                    findings.append(f"LOG_DATE_HEADING_INVALID:{path}")
                    break
    return OkfValidation(valid=not findings, findings=tuple(findings[:200]))


def validate_atlas_publish_policy(bundle: OkfBundle) -> OkfValidation:
    """Atlas's stronger export rules, on top of conformance.

    Everything here is a rule the specification does not impose and Atlas does. A consumer
    "MUST tolerate broken links"; a *producer* of governed knowledge must not ship one. OKF
    says nothing about code fences; Atlas must never emit one, because the only text that would
    need fencing is a definition body (INV-6). OKF permits any path; Atlas permits only opaque
    safe segments, so a bundle cannot escape its own directory when a consumer unpacks it.
    """
    findings: list[str] = []
    documents = {document.path: document.text for document in bundle.documents}
    conformance = validate_okf_conformance(documents)
    findings.extend(conformance.findings)
    for path in sorted(documents):
        segments = path.split("/")
        for segment in segments[:-1]:
            if not _SAFE_SEGMENT.match(segment):
                findings.append(f"UNSAFE_PATH_SEGMENT:{path}:{segment}")
        leaf = segments[-1]
        if not leaf.endswith(".md") or not _SAFE_SEGMENT.match(leaf[:-3]):
            findings.append(f"UNSAFE_PATH_LEAF:{path}")
        if path.startswith("/") or ".." in segments or "\\" in path:
            findings.append(f"UNSAFE_PATH:{path}")
        text = documents[path]
        _frontmatter, body = _split_frontmatter(text)
        if _RAW_MARKUP.search(body):
            findings.append(f"FORBIDDEN_RAW_MARKUP:{path}")
        if _CODE_FENCE.search(text):
            # Defence in depth for INV-6. No snapshot field can hold a body today; this makes
            # a field that could be added tomorrow fail the gate instead of shipping.
            findings.append(f"FORBIDDEN_CODE_FENCE:{path}")
        targets = _LINK.findall(text)
        targets.extend(angle or plain for angle, plain in _REFERENCE_LINK.findall(body))
        for target in targets:
            if target.startswith(("http://", "https://", "mailto:", "//")):
                findings.append(f"EXTERNAL_LINK:{path}:{target}")
                continue
            if target.startswith("atlas://"):
                continue
            resolved = target[1:] if target.startswith("/") else _join(path, target)
            if target.endswith("/"):
                # Spec §8's own index form links a subdirectory. A directory "exists" when it
                # holds at least one document -- there is no directory entry to point at.
                prefix = resolved.rstrip("/") + "/"
                if not any(candidate.startswith(prefix) for candidate in documents):
                    findings.append(f"DANGLING_LINK:{path}:{target}")
                continue
            if resolved not in documents:
                findings.append(f"DANGLING_LINK:{path}:{target}")
    if "index.md" not in documents:
        findings.append("ROOT_INDEX_MISSING")
    manifest = bundle.manifest
    if manifest.get("okf_version") != OKF_VERSION:
        findings.append("MANIFEST_OKF_VERSION_MISMATCH")
    if manifest.get("bundle_content_digest") != bundle.content_digest:
        findings.append("MANIFEST_CONTENT_DIGEST_MISMATCH")
    return OkfValidation(valid=not findings, findings=tuple(sorted(set(findings))[:200]))


def _join(path: str, target: str) -> str:
    parts = path.split("/")[:-1]
    for segment in target.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


# --- archive ----------------------------------------------------------------------------


def bundle_archive_bytes(bundle: OkfBundle) -> bytes:
    """The bundle as a deterministic ZIP: `bundle/` for the concept tree, manifest beside it.

    Every member carries `_ZIP_EPOCH` rather than a real mtime, because `zipfile` otherwise
    stamps the current time into each local header and two archives built from one snapshot
    would differ in bytes while being identical in content. `ZIP_DEFLATED` at a fixed level,
    and members in sorted order, for the same reason.

    The manifest sits at the archive root, outside `bundle/`, so a reader pointed at the OKF
    bundle root never needs an Atlas extension to consume it.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for document in bundle.documents:
            info = zipfile.ZipInfo(f"{BUNDLE_ROOT}/{document.path}", date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, document.text.encode("utf-8"))
        info = zipfile.ZipInfo(MANIFEST_FILENAME, date_time=_ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, bundle.manifest_json().encode("utf-8"))
    return buffer.getvalue()


def bundle_index(bundle: OkfBundle) -> list[dict[str, Any]]:
    """The file index a caller can read without downloading the archive."""
    return [
        {"path": document.path, "sha256": document.sha256, "bytes": document.byte_length}
        for document in bundle.documents
    ]


def iter_document_texts(bundle: OkfBundle) -> Iterable[tuple[str, str]]:
    for document in bundle.documents:
        yield document.path, document.text
