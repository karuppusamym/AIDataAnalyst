"""R11-OKF03 (design item 14C): an edited OKF bundle, turned into proposals in existing families.

The design's instruction is the whole shape of this module: "Wiki edits create proposals in
existing documentation/meaning workflows; approval regenerates the representation. Do not write
Markdown directly into canonical approved state." So import is two explicit steps and writes
nothing of its own:

1. **Preview** (`preview_okf_import`) reads the upload through `aida.okf_import_bundle` under its
   limits, finds the stored publication the bundle was exported from, and compares every document
   with Atlas's own stored bytes. It reports, per edit, which proposal it would raise in which
   family, and what it would not: a conflict when the object's approved version moved since the
   export, a refusal when the text is something Atlas would not publish or screening refuses, and
   every unsupported, derived, tolerated or claimed change by name. It persists nothing but an
   audit record of counts and reason codes.
2. **Apply** (`apply_okf_import`) repeats the preview on the same upload and refuses unless the
   result is exactly the preview the caller accepted (`PREVIEW_STALE`), then raises proposals --
   pending, never approved:

   * **Asset documentation and column descriptions** land in the description family's own batch
     store (`ModelImportBatch` / `ModelImportChange`), one batch per datasource, each change
     carrying the version the export showed as `expected_version`. Approval publishes through
     `model_import.apply_model_import_batch` -- the same append-only publish helpers and the same
     stale check a workbook edit uses -- so a description approved after the export is skipped,
     not overwritten. The batch's review is `OKF_IMPORT_BATCH`, not `MODEL_IMPORT_BATCH`: the
     content came from an untrusted file, so its tier is pinned at T2 (`review_risk_tiers`) and no
     agent may decide it at any size. The decision adapter is the workbook batch's own.
   * **Business meaning** (a concept's definition, or aliases added to it) lands as a new
     `OntologyVersion` on the version the bundle was exported from, submitted for its ordinary
     `ONTOLOGY_VERSION` review. `ontology_api.decide_ontology` refuses it if the ontology was
     published past that base in the meantime.

Nothing here approves, publishes or regenerates a bundle. A re-export shows an imported change
only after a different principal approved it in the review queue, because only then does the
approved state the exporter reads change.

**Authority never comes from the file.** The requester of every review is the importing
principal, so maker-checker separation holds against them. A `verified` or `status` value in the
file is reported as a claim and discarded. Scope comes from the caller's own authorization,
re-evaluated through `aida.okf_store.read_published_bundle` exactly as a read of the bundle
would be: the manifest's organization and product version are checked against it, never
trusted instead of it (INV-5), and the "before" of every comparison is the stored publication in
the caller's own lineage, found by the manifest's digest.

**Value-free refusals (INV-6).** A refusal persists a reason code. Proposed and current text
appear in the preview response for the importing user, and proposed text is persisted only where
the family already persists it: as a pending change's `new_value` or a pending ontology draft.
Text that screening refuses is never persisted and never echoed back.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.ingest_screening import CLEAN, screen_text
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    GovernanceReview,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ModelImportBatch,
    ModelImportChange,
)
from aida.okf_export import (
    DESCRIPTION_APPROVED,
    EXPORT_PROFILE,
    OKF_SPEC_REVISION,
    OKF_VERSION,
    OkfConceptFacts,
    OkfSnapshot,
    object_key,
    snapshot_from_document,
)
from aida.okf_import_bundle import (
    ALREADY_CURRENT,
    BASE_PUBLICATION_NOT_RETAINED,
    DATASOURCE_NOT_AUTHORIZED,
    EDIT_COLUMN_DESCRIPTION,
    EDIT_CONCEPT_DEFINITION,
    EDIT_TABLE_PURPOSE,
    FAMILY_ASSET_DOCUMENTATION,
    FAMILY_COLUMN_DESCRIPTION,
    FAMILY_ONTOLOGY_MEANING,
    IMPORT_ALREADY_PENDING,
    IMPORT_NOTHING_TO_PROPOSE,
    MANIFEST_INVALID,
    MANIFEST_NOT_ATLAS,
    MANIFEST_SCOPE_MISMATCH,
    MAX_ALIAS_CHARS,
    MAX_CONCEPT_DEFINITION_CHARS,
    MAX_DESCRIPTION_CHARS,
    MEANING_DEFINITION_INVALID,
    MEANING_MAPPING_INVALID,
    OUTCOME_CLAIM,
    OUTCOME_IGNORED,
    OUTCOME_REFUSED,
    OUTCOME_TOLERATED,
    OUTCOME_UNSUPPORTED,
    PREVIEW_STALE,
    SOURCE_CHANGED_SINCE_EXPORT,
    TARGET_AMBIGUOUS,
    TARGET_NOT_ACTIVE,
    TARGET_NOT_FOUND,
    TEXT_SCREENING_REFUSED,
    OkfImportAnalysis,
    OkfImportEdit,
    OkfImportNote,
    OkfImportRefused,
    analyze_bundle_edits,
    read_import_archive,
    text_refusal,
)
from aida.okf_store import OkfPublishedBundle, load_documents_by_path, read_published_bundle
from aida.okf_store_models import OkfBundlePublication
from aida.ontology_api import Concept, OntologyDefinition, validate_mappings
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.security_types import SecurityContext

#: Item outcomes, beside the note outcomes `aida.okf_import_bundle` defines.
OUTCOME_PROPOSE: Final = "PROPOSE"
OUTCOME_CONFLICT: Final = "CONFLICT"
OUTCOME_UNCHANGED: Final = "UNCHANGED"

#: The review an imported description batch waits in. Its own object type, so the tier table can
#: pin it at T2 whatever its size; decided by the workbook batch's adapter (`semantic_api`).
OKF_IMPORT_REVIEW_TYPE: Final = "OKF_IMPORT_BATCH"
OKF_IMPORT_ACTION: Final = "APPLY_OKF_IMPORT"
#: What a change row's `sheet_name` says, so a reviewer reading the batch knows its origin.
OKF_SHEET_NAME: Final = "OKF bundle"

TARGET_TABLE: Final = "TABLE"
TARGET_COLUMN: Final = "COLUMN"
TARGET_CONCEPT: Final = "ONTOLOGY_CONCEPT"

#: The field names `model_import` applies: the same two a workbook edits.
_MODEL_IMPORT_FIELDS: Final = {
    EDIT_TABLE_PURPOSE: ("TABLE", "readme"),
    EDIT_COLUMN_DESCRIPTION: ("COLUMN", "business_description"),
}
_SAFE_FILENAME: Final = re.compile(r"[^A-Za-z0-9._-]+")
#: Stored documents read per query, so a large bundle never becomes one oversized IN list.
_PATHS_PER_READ: Final = 1_000


# --- the preview ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OkfImportItem:
    """One edit, decided against Atlas's current state."""

    item_id: str
    path: str
    family: str
    kind: str
    field: str
    outcome: str
    reason_code: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    #: The target's name as Atlas holds it -- never a label read from the upload.
    target_label: str | None = None
    datasource_id: UUID | None = None
    ontology_key: str | None = None
    concept_key: str | None = None
    expected_version: int | None = None
    current_version: int | None = None
    #: For the importing user only. `None` when there is none, or screening withholds it.
    current_value: str | None = None
    proposed_value: str | None = None
    added_aliases: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _MeaningPlan:
    """One ontology's new version: its base, and the definition the import proposes."""

    head_id: UUID
    ontology_key: str
    base_version: int
    definition: dict[str, Any]
    item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OkfImportPreview:
    stored: OkfPublishedBundle
    base: OkfBundlePublication
    archive_sha256: str
    filename: str
    analysis: OkfImportAnalysis
    items: tuple[OkfImportItem, ...]
    digest: str
    #: What each proposed description replaces, as the family records it (`old_value`).
    replaced: Mapping[str, str | None] = field(default_factory=dict)
    meaning: tuple[_MeaningPlan, ...] = ()

    @property
    def notes(self) -> tuple[OkfImportNote, ...]:
        return self.analysis.notes

    def counts(self) -> dict[str, int]:
        items = Counter(item.outcome for item in self.items)
        notes = Counter(note.outcome for note in self.analysis.notes)
        return {
            "documents": self.analysis.documents,
            "unchanged_documents": self.analysis.unchanged,
            "changed_documents": self.analysis.changed,
            "proposals": items.get(OUTCOME_PROPOSE, 0),
            "conflicts": items.get(OUTCOME_CONFLICT, 0),
            "already_current": items.get(OUTCOME_UNCHANGED, 0),
            "refused": items.get(OUTCOME_REFUSED, 0) + notes.get(OUTCOME_REFUSED, 0),
            "unsupported": items.get(OUTCOME_UNSUPPORTED, 0) + notes.get(OUTCOME_UNSUPPORTED, 0),
            "claims": notes.get(OUTCOME_CLAIM, 0),
            "tolerated": notes.get(OUTCOME_TOLERATED, 0),
            "ignored": notes.get(OUTCOME_IGNORED, 0),
        }

    def reason_counts(self) -> dict[str, int]:
        """Reason code -> occurrences. Value-free: what the audit record carries."""
        codes = Counter(item.reason_code for item in self.items if item.reason_code)
        codes.update(note.reason_code for note in self.analysis.notes)
        return dict(sorted(codes.items()))


def _refused(reason_code: str, message: str, status_code: int = 422) -> OkfImportRefused:
    return OkfImportRefused(reason_code, message, status_code=status_code)


def _check_manifest(manifest: Mapping[str, Any]) -> tuple[str, str]:
    """The manifest's format and the two digests that name its publication.

    Returns (bundle content digest, content snapshot digest). The manifest is untrusted: these
    values are used only to *look up* a publication in the caller's own lineage.
    """
    specification = manifest.get("specification")
    compiler = manifest.get("compiler")
    scope = manifest.get("scope")
    bundle_digest = manifest.get("bundle_content_digest")
    snapshot_digest = manifest.get("content_snapshot_digest")
    if not (
        isinstance(specification, dict)
        and isinstance(compiler, dict)
        and isinstance(scope, dict)
        and isinstance(bundle_digest, str)
        and isinstance(snapshot_digest, str)
    ):
        raise _refused(MANIFEST_INVALID, "the Atlas manifest is missing required fields")
    if (
        manifest.get("okf_version") != OKF_VERSION
        or specification.get("revision") != OKF_SPEC_REVISION
        or compiler.get("profile") != EXPORT_PROFILE
    ):
        raise _refused(
            MANIFEST_NOT_ATLAS,
            "the manifest does not describe an Atlas OKF export at the pinned specification",
        )
    return bundle_digest, snapshot_digest


def _check_scope(manifest: Mapping[str, Any], stored: OkfPublishedBundle) -> None:
    scope = manifest.get("scope")
    assert isinstance(scope, dict)
    if str(scope.get("organization_id")) != str(stored.version.organization_id) or str(
        scope.get("product_version_id")
    ) != str(stored.version.id):
        raise _refused(
            MANIFEST_SCOPE_MISMATCH,
            "this bundle was exported from a different product version or organization; "
            "import it against the version it came from",
        )


async def _base_publication(
    session: AsyncSession,
    stored: OkfPublishedBundle,
    bundle_digest: str,
    snapshot_digest: str,
) -> OkfBundlePublication:
    """The stored publication the bundle was exported from, in the caller's own lineage only.

    Found by digest among the publications `read_published_bundle` would serve this caller. A
    bundle exported under another authority -- a revoked grant, another reader's wider scope --
    is not in this lineage and is refused, exactly as a pinned read of it would be.
    """
    base: OkfBundlePublication | None = await session.scalar(
        select(OkfBundlePublication)
        .where(
            OkfBundlePublication.organization_id == stored.version.organization_id,
            OkfBundlePublication.context_product_version_id == stored.version.id,
            OkfBundlePublication.authority_digest == stored.authority_digest,
            OkfBundlePublication.bundle_content_digest == bundle_digest,
            OkfBundlePublication.content_snapshot_digest == snapshot_digest,
        )
        .order_by(OkfBundlePublication.sequence.desc())
        .limit(1)
    )
    if base is None:
        raise _refused(
            BASE_PUBLICATION_NOT_RETAINED,
            "the publication this bundle was exported from is not retained for this reader, so "
            "its edits cannot be told apart from what Atlas has changed since. Export the "
            "current bundle and re-apply the edits to it.",
            status_code=409,
        )
    return base


def _item_id(edit: OkfImportEdit) -> str:
    return hashlib.sha256(f"{edit.path}\x00{edit.kind}\x00{edit.field}".encode()).hexdigest()[:24]


def _screened(text: str | None, origin: str) -> str | None:
    """Current Atlas text for the preview, released only where export screening would."""
    if text is None:
        return None
    return text if screen_text(text, content_origin=origin).status == CLEAN else None


def _screening_refusal(text: str, origin: str) -> str | None:
    verdict = screen_text(text, content_origin=origin)
    if verdict.status == CLEAN:
        return None
    return ", ".join(sorted(verdict.reason_codes)) or TEXT_SCREENING_REFUSED


@dataclass(slots=True)
class _Catalog:
    """The product's tables and the columns the edits name, as Atlas holds them now."""

    tables: dict[str, tuple[MetadataTable, str]] = field(default_factory=dict)
    columns: dict[tuple[UUID, str], list[MetadataColumn]] = field(default_factory=dict)
    documentation: dict[UUID, AssetDocumentationVersion] = field(default_factory=dict)
    descriptions: dict[UUID, ColumnDocumentationVersion] = field(default_factory=dict)
    authorized: dict[UUID, bool] = field(default_factory=dict)


def _uuids(values: Sequence[Any] | None) -> list[UUID]:
    out: list[UUID] = []
    for value in values or []:
        try:
            out.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return out


async def _load_catalog(
    session: AsyncSession,
    stored: OkfPublishedBundle,
    edits: Sequence[OkfImportEdit],
) -> _Catalog:
    """Resolve the object keys the edits name to the product's own tables, in this tenant."""
    catalog = _Catalog()
    wanted = {
        edit.subject_key
        for edit in edits
        if edit.kind in (EDIT_TABLE_PURPOSE, EDIT_COLUMN_DESCRIPTION)
    }
    if not wanted:
        return catalog
    organization_id = stored.version.organization_id
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.id.in_(_uuids(stored.version.table_ids)),
                MetadataTable.organization_id == organization_id,
                MetadataSchema.organization_id == organization_id,
                MetadataCatalog.organization_id == organization_id,
            )
        )
    ).all()
    for table, schema, source_catalog in rows:
        key = object_key(str(table.datasource_id), source_catalog.name, schema.name, table.name)
        if key in wanted:
            label = f"{source_catalog.name}.{schema.name}.{table.name}"
            catalog.tables[key] = (table, label)
    table_ids = [table.id for table, _label in catalog.tables.values()]
    if not table_ids:
        return catalog
    documentation = (
        await session.execute(
            select(AssetDocumentationVersion, AssetDocumentation.table_id)
            .join(
                AssetDocumentation,
                AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
            )
            .where(
                AssetDocumentation.table_id.in_(table_ids),
                AssetDocumentation.organization_id == organization_id,
                AssetDocumentationVersion.organization_id == organization_id,
                AssetDocumentationVersion.status == DESCRIPTION_APPROVED,
            )
            .order_by(AssetDocumentationVersion.version)
        )
    ).all()
    # Ascending, so the newest approved version per table wins -- the version the workbook
    # apply path's stale check compares against.
    catalog.documentation = {table_id: version for version, table_id in documentation}
    names = {
        edit.column_name
        for edit in edits
        if edit.kind == EDIT_COLUMN_DESCRIPTION and edit.column_name is not None
    }
    if names:
        columns = (
            await session.scalars(
                select(MetadataColumn).where(
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.organization_id == organization_id,
                    MetadataColumn.name.in_(sorted(names)),
                )
            )
        ).all()
        for column in columns:
            catalog.columns.setdefault((column.table_id, column.name), []).append(column)
        column_ids = [column.id for column in columns]
        described = (
            await session.execute(
                select(ColumnDocumentationVersion, ColumnDocumentation.column_id)
                .join(
                    ColumnDocumentation,
                    ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
                )
                .where(
                    ColumnDocumentation.column_id.in_(column_ids),
                    ColumnDocumentation.organization_id == organization_id,
                    ColumnDocumentationVersion.organization_id == organization_id,
                    ColumnDocumentationVersion.status == DESCRIPTION_APPROVED,
                )
                .order_by(ColumnDocumentationVersion.version)
            )
        ).all()
        catalog.descriptions = {column_id: version for version, column_id in described}
    return catalog


async def _authorized(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    catalog: _Catalog,
    datasource_id: UUID,
) -> bool:
    """The gate the workbook upload runs: whoever may not read a source's model may not propose
    edits to it either. Decided once per datasource."""
    if datasource_id not in catalog.authorized:
        try:
            await gate(
                session,
                context,
                settings=settings,
                action="READ_METADATA",
                resource_type="datasource",
                resource_id=str(datasource_id),
                datasource_id=datasource_id,
            )
            catalog.authorized[datasource_id] = True
        except AuthorizationDenied:
            catalog.authorized[datasource_id] = False
    return catalog.authorized[datasource_id]


async def _description_item(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    catalog: _Catalog,
    edit: OkfImportEdit,
    replaced: dict[str, str | None],
) -> OkfImportItem:
    item_id = _item_id(edit)
    base = OkfImportItem(
        item_id=item_id,
        path=edit.path,
        family=edit.family,
        kind=edit.kind,
        field=edit.field,
        outcome=OUTCOME_UNSUPPORTED,
        expected_version=edit.base_version,
    )
    found = catalog.tables.get(edit.subject_key)
    if found is None:
        return _with(base, reason_code=TARGET_NOT_FOUND)
    table, label = found
    target_type, target_id = TARGET_TABLE, str(table.id)
    target: MetadataTable | MetadataColumn = table
    if edit.kind == EDIT_COLUMN_DESCRIPTION:
        label = f"{label}.{edit.column_name}"
        candidates = catalog.columns.get((table.id, edit.column_name or ""), [])
        active = [column for column in candidates if column.status == "ACTIVE"]
        if len(active) > 1:
            return _with(base, reason_code=TARGET_AMBIGUOUS, target_label=label)
        if not candidates:
            return _with(base, reason_code=TARGET_NOT_FOUND, target_label=label)
        column = active[0] if active else candidates[0]
        target_type, target_id, target = TARGET_COLUMN, str(column.id), column
    base = _with(
        base,
        target_type=target_type,
        target_id=target_id,
        target_label=label,
        datasource_id=table.datasource_id,
    )
    if table.status != "ACTIVE" or target.status != "ACTIVE":
        return _with(base, reason_code=TARGET_NOT_ACTIVE)
    if not await _authorized(session, context, settings, catalog, table.datasource_id):
        return _with(base, outcome=OUTCOME_REFUSED, reason_code=DATASOURCE_NOT_AUTHORIZED)
    refusal = text_refusal(edit.proposed, limit=MAX_DESCRIPTION_CHARS)
    if refusal is not None:
        return _with(base, outcome=OUTCOME_REFUSED, reason_code=refusal)
    origin = f"okf_import:{edit.kind.lower()}"
    screened = _screening_refusal(edit.proposed, origin)
    if screened is not None:
        return _with(
            base, outcome=OUTCOME_REFUSED, reason_code=TEXT_SCREENING_REFUSED, detail=screened
        )
    if isinstance(target, MetadataColumn):
        described = catalog.descriptions.get(target.id)
        current_text = described.description if described else None
        current_version = described.version if described else None
    else:
        documented = catalog.documentation.get(table.id)
        current_text = documented.readme if documented else None
        current_version = documented.version if documented else None
    base = _with(
        base,
        current_version=current_version,
        current_value=_screened(current_text, origin),
        proposed_value=edit.proposed,
    )
    if current_version != edit.base_version:
        return _with(base, outcome=OUTCOME_CONFLICT, reason_code=SOURCE_CHANGED_SINCE_EXPORT)
    if current_text is not None and " ".join(current_text.split()) == " ".join(
        edit.proposed.split()
    ):
        return _with(base, outcome=OUTCOME_UNCHANGED, reason_code=ALREADY_CURRENT)
    replaced[item_id] = current_text
    return _with(base, outcome=OUTCOME_PROPOSE)


def _with(item: OkfImportItem, **changes: Any) -> OkfImportItem:
    return replace(item, **changes)


@dataclass(slots=True)
class _Ontology:
    head: OntologyHead
    base: OntologyVersion
    definition: OntologyDefinition


async def _ontology(
    session: AsyncSession, organization_id: UUID, facts: OkfConceptFacts
) -> _Ontology | None:
    head = await session.scalar(
        select(OntologyHead).where(
            OntologyHead.organization_id == organization_id,
            OntologyHead.ontology_key == facts.ontology_key,
        )
    )
    if head is None:
        return None
    base = await session.scalar(
        select(OntologyVersion).where(
            OntologyVersion.organization_id == organization_id,
            OntologyVersion.ontology_id == head.id,
            OntologyVersion.version == facts.ontology_version,
            OntologyVersion.status == "APPROVED",
        )
    )
    if base is None:
        return None
    try:
        definition = OntologyDefinition.model_validate(base.definition)
    except ValidationError:
        return None
    return _Ontology(head=head, base=base, definition=definition)


def _concept_index(definition: OntologyDefinition, name: str) -> list[int]:
    """The concepts the export printed as `name`: by display name, or by key where the display
    name was withheld (`okf_snapshot._concepts` falls back to the key)."""
    return [
        index
        for index, concept in enumerate(definition.concepts)
        if concept.name == name or concept.key == name
    ]


async def _meaning_items(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    stored: OkfPublishedBundle,
    snapshot: OkfSnapshot,
    edits: Sequence[OkfImportEdit],
) -> tuple[list[OkfImportItem], list[_MeaningPlan]]:
    """Concept edits, decided per concept and then validated per ontology as one new version."""
    concepts = {concept.key: concept for concept in snapshot.concepts}
    organization_id = stored.version.organization_id
    ontologies: dict[str, _Ontology | None] = {}
    items: list[OkfImportItem] = []
    edited: dict[str, dict[int, Concept]] = {}
    for edit in edits:
        item = OkfImportItem(
            item_id=_item_id(edit),
            path=edit.path,
            family=edit.family,
            kind=edit.kind,
            field=edit.field,
            outcome=OUTCOME_UNSUPPORTED,
            expected_version=edit.base_version,
            added_aliases=edit.added_aliases,
        )
        facts = concepts.get(edit.subject_key)
        if facts is None:
            items.append(_with(item, reason_code=TARGET_NOT_FOUND))
            continue
        if facts.ontology_key not in ontologies:
            ontologies[facts.ontology_key] = await _ontology(session, organization_id, facts)
        ontology = ontologies[facts.ontology_key]
        if ontology is None:
            items.append(_with(item, reason_code=TARGET_NOT_FOUND))
            continue
        matches = _concept_index(ontology.definition, facts.name)
        if len(matches) != 1:
            items.append(
                _with(item, reason_code=TARGET_AMBIGUOUS if matches else TARGET_NOT_FOUND)
            )
            continue
        index = matches[0]
        concept = edited.get(facts.ontology_key, {}).get(index) or ontology.definition.concepts[
            index
        ]
        item = _with(
            item,
            target_type=TARGET_CONCEPT,
            target_id=f"{facts.ontology_key}:{concept.key}",
            target_label=f"{facts.ontology_key}.{concept.key}",
            ontology_key=facts.ontology_key,
            concept_key=concept.key,
            current_version=ontology.head.published_version,
        )
        if concept.deprecated:
            items.append(_with(item, reason_code=TARGET_NOT_ACTIVE))
            continue
        origin = f"okf_import:{edit.kind.lower()}"
        if edit.kind == EDIT_CONCEPT_DEFINITION:
            refusal = text_refusal(edit.proposed, limit=MAX_CONCEPT_DEFINITION_CHARS)
            texts = [edit.proposed]
        else:
            refusal = next(
                (
                    reason
                    for alias in edit.added_aliases
                    if (reason := text_refusal(alias, limit=MAX_ALIAS_CHARS)) is not None
                ),
                None,
            )
            texts = list(edit.added_aliases)
        if refusal is not None:
            items.append(_with(item, outcome=OUTCOME_REFUSED, reason_code=refusal))
            continue
        screened = next(
            (reason for text in texts if (reason := _screening_refusal(text, origin))), None
        )
        if screened is not None:
            items.append(
                _with(
                    item,
                    outcome=OUTCOME_REFUSED,
                    reason_code=TEXT_SCREENING_REFUSED,
                    detail=screened,
                )
            )
            continue
        if edit.kind == EDIT_CONCEPT_DEFINITION:
            item = _with(
                item,
                current_value=_screened(concept.description, origin),
                proposed_value=edit.proposed,
            )
            already = " ".join(concept.description.split()) == " ".join(edit.proposed.split())
            updated = concept.model_copy(update={"description": edit.proposed.strip()})
        else:
            present = {alias.casefold() for alias in concept.aliases}
            added = [alias for alias in edit.added_aliases if alias.casefold() not in present]
            item = _with(
                item,
                current_value="\n".join(
                    alias for alias in concept.aliases if _screened(alias, origin)
                )
                or None,
                proposed_value="\n".join(added) or None,
                added_aliases=tuple(added),
            )
            already = not added
            updated = concept.model_copy(update={"aliases": [*concept.aliases, *added]})
        if ontology.head.published_version != facts.ontology_version:
            items.append(
                _with(item, outcome=OUTCOME_CONFLICT, reason_code=SOURCE_CHANGED_SINCE_EXPORT)
            )
            continue
        if already:
            items.append(_with(item, outcome=OUTCOME_UNCHANGED, reason_code=ALREADY_CURRENT))
            continue
        edited.setdefault(facts.ontology_key, {})[index] = updated
        items.append(_with(item, outcome=OUTCOME_PROPOSE))

    plans: list[_MeaningPlan] = []
    for ontology_key, changes in sorted(edited.items()):
        ontology = ontologies[ontology_key]
        assert ontology is not None
        members = [
            item.item_id
            for item in items
            if item.ontology_key == ontology_key and item.outcome == OUTCOME_PROPOSE
        ]
        concepts_now = [
            changes.get(index, concept)
            for index, concept in enumerate(ontology.definition.concepts)
        ]
        invalid: str | None = None
        try:
            definition = OntologyDefinition.model_validate(
                {**ontology.definition.model_dump(mode="json"), "concepts": [
                    concept.model_dump(mode="json") for concept in concepts_now
                ]}
            )
        except ValidationError:
            invalid = MEANING_DEFINITION_INVALID
        else:
            try:
                await validate_mappings(session, definition, context, settings)
            except (HTTPException, AuthorizationDenied):
                invalid = MEANING_MAPPING_INVALID
        if invalid is not None:
            items = [
                _with(item, outcome=OUTCOME_REFUSED, reason_code=invalid)
                if item.item_id in members
                else item
                for item in items
            ]
            continue
        plans.append(
            _MeaningPlan(
                head_id=ontology.head.id,
                ontology_key=ontology_key,
                base_version=ontology.base.version,
                definition=definition.model_dump(mode="json"),
                item_ids=tuple(members),
            )
        )
    return items, plans


def _preview_digest(
    base: OkfBundlePublication, archive_sha256: str, items: Sequence[OkfImportItem]
) -> str:
    """What an apply must reproduce exactly: the base, the file, and every item's decision
    against the state Atlas was in -- so an approval, a new column or a moved ontology between
    preview and apply is a `PREVIEW_STALE` refusal rather than a proposal nobody previewed."""
    payload = {
        "base_publication_id": str(base.id),
        "archive_sha256": archive_sha256,
        "items": [
            [
                item.item_id,
                item.outcome,
                item.reason_code,
                item.target_id,
                item.expected_version,
                item.current_version,
                hashlib.sha256((item.proposed_value or "").encode("utf-8")).hexdigest(),
            ]
            for item in items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def safe_filename(filename: str) -> str:
    cleaned = _SAFE_FILENAME.sub("-", filename).strip(".-")[:120]
    return cleaned or "okf-bundle.zip"


async def preview_okf_import(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
    settings: Settings,
    content: bytes,
    *,
    filename: str = "okf-bundle.zip",
) -> OkfImportPreview:
    """What importing this bundle would propose, and everything it would not. Writes nothing
    but what a read of the bundle writes; the caller records the audit.

    Order matters: the archive is bounded and parsed before anything is read from the database,
    and scope is decided by `read_published_bundle` -- the same door every read of the bundle
    uses -- before anything the manifest says is looked up.
    """
    archive = read_import_archive(content)
    bundle_digest, snapshot_digest = _check_manifest(archive.manifest)
    stored = await read_published_bundle(session, version_id, context, settings)
    _check_scope(archive.manifest, stored)
    base = await _base_publication(session, stored, bundle_digest, snapshot_digest)
    snapshot = snapshot_from_document(base.snapshot)
    paths = [document.path for document in archive.documents]
    stored_documents: dict[str, tuple[str, str]] = {}
    for start in range(0, len(paths), _PATHS_PER_READ):
        stored_documents.update(
            await load_documents_by_path(session, base, paths[start : start + _PATHS_PER_READ])
        )
    analysis = analyze_bundle_edits(
        archive,
        snapshot=snapshot,
        base_documents={path: text for path, (text, _sha) in stored_documents.items()},
    )
    description_edits = [
        edit
        for edit in analysis.edits
        if edit.family in (FAMILY_ASSET_DOCUMENTATION, FAMILY_COLUMN_DESCRIPTION)
    ]
    meaning_edits = [edit for edit in analysis.edits if edit.family == FAMILY_ONTOLOGY_MEANING]
    catalog = await _load_catalog(session, stored, description_edits)
    replaced: dict[str, str | None] = {}
    items = [
        await _description_item(session, context, settings, catalog, edit, replaced)
        for edit in description_edits
    ]
    meaning_items, plans = await _meaning_items(
        session, context, settings, stored, snapshot, meaning_edits
    )
    items.extend(meaning_items)
    return OkfImportPreview(
        stored=stored,
        base=base,
        archive_sha256=archive.sha256,
        filename=safe_filename(filename),
        analysis=analysis,
        items=tuple(items),
        digest=_preview_digest(base, archive.sha256, items),
        replaced={
            key: value
            for key, value in replaced.items()
            if any(item.item_id == key and item.outcome == OUTCOME_PROPOSE for item in items)
        },
        meaning=tuple(plans),
    )


def record_import_audit(
    session: AsyncSession,
    context: SecurityContext,
    *,
    version_id: UUID,
    action: str,
    outcome: str,
    details: dict[str, Any],
    organization_id: UUID | None = None,
) -> None:
    """One audit record per preview, apply or refusal: ids, digests, counts and reason codes.

    Never a path from the upload, a filename, a field value or proposed text (INV-6). Filed
    under the product's organization once it is known, so a PlatformAdmin importing across the
    boundary does not file the record under their own.
    """
    record_audit(
        session,
        replace(context, organization_id=organization_id or context.organization_id),
        action=action,
        resource_type="context_product_version",
        resource_id=str(version_id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details=details,
    )


def preview_audit_details(preview: OkfImportPreview) -> dict[str, Any]:
    return {
        "archive_sha256": preview.archive_sha256,
        "base_publication_id": str(preview.base.id),
        "base_publication_sequence": preview.base.sequence,
        "preview_digest": preview.digest,
        "counts": preview.counts(),
        "reason_codes": preview.reason_counts(),
    }


# --- apply ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OkfImportBatch:
    batch_id: UUID
    datasource_id: UUID
    governance_review_id: UUID
    change_count: int


@dataclass(frozen=True, slots=True)
class OkfImportMeaning:
    ontology_version_id: UUID
    ontology_key: str
    version: int
    base_version: int
    governance_review_id: UUID
    concept_count: int


@dataclass(frozen=True, slots=True)
class OkfImportApplied:
    preview: OkfImportPreview
    batches: tuple[OkfImportBatch, ...]
    meaning: tuple[OkfImportMeaning, ...]


async def _refuse_repeat(
    session: AsyncSession,
    context: SecurityContext,
    preview: OkfImportPreview,
) -> None:
    """The same file, already waiting for review, is not proposed a second time."""
    organization_id = preview.stored.version.organization_id
    pending = await session.scalar(
        select(ModelImportBatch.id)
        .join(GovernanceReview, GovernanceReview.id == ModelImportBatch.governance_review_id)
        .where(
            ModelImportBatch.organization_id == organization_id,
            GovernanceReview.organization_id == organization_id,
            ModelImportBatch.content_sha256 == preview.archive_sha256,
            ModelImportBatch.status == "PENDING_REVIEW",
            GovernanceReview.object_type == OKF_IMPORT_REVIEW_TYPE,
        )
        .limit(1)
    )
    duplicate = pending is not None
    for plan in preview.meaning:
        drafts = (
            await session.scalars(
                select(OntologyVersion).where(
                    OntologyVersion.organization_id == organization_id,
                    OntologyVersion.ontology_id == plan.head_id,
                    OntologyVersion.base_version == plan.base_version,
                    OntologyVersion.status == "PENDING_APPROVAL",
                    OntologyVersion.created_by == context.principal_id,
                )
            )
        ).all()
        duplicate = duplicate or any(draft.definition == plan.definition for draft in drafts)
    if duplicate:
        raise _refused(
            IMPORT_ALREADY_PENDING,
            "these edits are already waiting for review from an earlier import of this bundle",
            status_code=409,
        )


async def _propose_descriptions(
    session: AsyncSession,
    context: SecurityContext,
    preview: OkfImportPreview,
) -> list[OkfImportBatch]:
    """One pending batch per datasource, in the description family's own store."""
    organization_id = preview.stored.version.organization_id
    by_source: dict[UUID, list[OkfImportItem]] = {}
    held_back: Counter[UUID] = Counter()
    for item in preview.items:
        if item.family not in (FAMILY_ASSET_DOCUMENTATION, FAMILY_COLUMN_DESCRIPTION):
            continue
        if item.datasource_id is None:
            continue
        if item.outcome == OUTCOME_PROPOSE:
            by_source.setdefault(item.datasource_id, []).append(item)
        else:
            held_back[item.datasource_id] += 1
    batches: list[OkfImportBatch] = []
    for datasource_id, items in sorted(by_source.items(), key=lambda entry: str(entry[0])):
        batch = ModelImportBatch(
            organization_id=organization_id,
            datasource_id=datasource_id,
            filename=preview.filename,
            content_sha256=preview.archive_sha256,
            # Straight to review: an import batch is never a DRAFT, so the workbook routes that
            # edit or submit a draft cannot reach it and raise a review below T2.
            status="PENDING_REVIEW",
            change_count=len(items),
            rejected_row_count=held_back[datasource_id],
            uploaded_by=context.principal_id,
        )
        session.add(batch)
        await session.flush()
        for number, item in enumerate(items, start=1):
            subject_type, field_name = _MODEL_IMPORT_FIELDS[item.kind]
            assert item.target_id is not None and item.proposed_value is not None
            session.add(
                ModelImportChange(
                    organization_id=organization_id,
                    batch_id=batch.id,
                    sheet_name=OKF_SHEET_NAME,
                    row_number=number,
                    subject_type=subject_type,
                    subject_id=item.target_id,
                    subject_label=(item.target_label or item.target_id)[:600],
                    field=field_name,
                    old_value=preview.replaced.get(item.item_id),
                    new_value=item.proposed_value,
                    expected_version=item.expected_version,
                    status="PENDING",
                )
            )
        review = GovernanceReview(
            organization_id=organization_id,
            object_type="OKF_IMPORT_BATCH",
            object_id=str(batch.id),
            requested_action=OKF_IMPORT_ACTION,
            requested_by=context.principal_id,
        )
        session.add(review)
        await session.flush()
        batch.governance_review_id = review.id
        await session.flush()
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="model_import_batch",
            aggregate_id=str(batch.id),
            event_type="model_import.submitted.v1",
            payload={
                "batch_id": str(batch.id),
                "datasource_id": str(datasource_id),
                "review_id": str(review.id),
                "change_count": batch.change_count,
            },
        )
        batches.append(
            OkfImportBatch(
                batch_id=batch.id,
                datasource_id=datasource_id,
                governance_review_id=review.id,
                change_count=batch.change_count,
            )
        )
    return batches


async def _propose_meaning(
    session: AsyncSession,
    context: SecurityContext,
    preview: OkfImportPreview,
) -> list[OkfImportMeaning]:
    """One new ontology version per ontology, submitted for its ordinary review.

    The same shape `ontology_api.create_ontology_version` and `submit_ontology_version` write,
    in one step because the importer is both author and submitter: the head is locked, the base
    is re-checked, and the version is written against it. Approval re-checks the base again.
    """
    organization_id = preview.stored.version.organization_id
    proposed: list[OkfImportMeaning] = []
    for plan in preview.meaning:
        head = await session.scalar(
            select(OntologyHead)
            .where(
                OntologyHead.id == plan.head_id,
                OntologyHead.organization_id == organization_id,
            )
            .with_for_update()
        )
        if head is None or head.published_version != plan.base_version:
            raise _refused(
                PREVIEW_STALE,
                "an ontology was published after the preview; preview the bundle again",
                status_code=409,
            )
        head.last_version += 1
        version = OntologyVersion(
            organization_id=organization_id,
            ontology_id=head.id,
            version=head.last_version,
            base_version=plan.base_version,
            definition=plan.definition,
            status="PENDING_APPROVAL",
            created_by=context.principal_id,
        )
        session.add(version)
        await session.flush()
        review = GovernanceReview(
            organization_id=organization_id,
            object_type="ONTOLOGY_VERSION",
            object_id=str(version.id),
            requested_action="PUBLISH",
            status="PENDING",
            requested_by=context.principal_id,
        )
        session.add(review)
        await session.flush()
        version.governance_review_id = review.id
        record_audit(
            session,
            context,
            action="ontology.submit",
            resource_type="ontology_version",
            resource_id=str(version.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={
                "origin": "okf_import",
                "archive_sha256": preview.archive_sha256,
                "base_version": plan.base_version,
                "items": len(plan.item_ids),
            },
        )
        proposed.append(
            OkfImportMeaning(
                ontology_version_id=version.id,
                ontology_key=plan.ontology_key,
                version=version.version,
                base_version=plan.base_version,
                governance_review_id=review.id,
                concept_count=len(plan.item_ids),
            )
        )
    return proposed


async def apply_okf_import(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
    settings: Settings,
    content: bytes,
    *,
    preview_digest: str,
    filename: str = "okf-bundle.zip",
) -> OkfImportApplied:
    """Raise the proposals an accepted preview listed -- pending review, never applied.

    The preview is recomputed from the same upload and must reproduce `preview_digest`
    exactly; anything that moved in between refuses the apply with `PREVIEW_STALE`.
    """
    preview = await preview_okf_import(
        session, version_id, context, settings, content, filename=filename
    )
    if preview.digest != preview_digest:
        raise _refused(
            PREVIEW_STALE,
            "Atlas or the bundle changed since this preview was taken; preview it again and "
            "apply the preview you accept",
            status_code=409,
        )
    if not any(item.outcome == OUTCOME_PROPOSE for item in preview.items):
        raise _refused(
            IMPORT_NOTHING_TO_PROPOSE,
            "this bundle proposes nothing: every edit is unsupported, refused, in conflict or "
            "already current",
            status_code=409,
        )
    await _refuse_repeat(session, context, preview)
    batches = await _propose_descriptions(session, context, preview)
    meaning = await _propose_meaning(session, context, preview)
    return OkfImportApplied(preview=preview, batches=tuple(batches), meaning=tuple(meaning))
