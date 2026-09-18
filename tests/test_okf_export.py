"""R11-OKF01: the OKF v0.2 exporter, and the properties it would be worthless without.

The acceptance criteria this file answers, from design section 14:

* **OKF-A** -- PostgreSQL *and* SQL Server fixtures produce per-table/view/routine documents and
  source/product indexes through one normalized path
  (`test_both_engines_produce_documents_through_one_path`).
* **OKF-B** -- deterministic export, safe names and overloads, approved versions, verified
  provenance, no private-value leakage (the determinism, identity, verification and sentinel
  tests below).
* **OKF-C** -- no-op re-capture reproduces hashes
  (`test_a_recapture_of_unchanged_content_reproduces_every_hash`, and its database twin).
* **OKF-D** -- an unauthorized dependency is absent from text, index, links *and counts*
  (`test_a_refused_datasource_is_absent_from_text_links_and_counts`).

What is deliberately *not* claimed anywhere here: conformance certification. The upstream
revision is pinned (`OKF_SPEC_REVISION`) and
`test_the_bundle_satisfies_the_pinned_conformance_clauses` checks the §11 clauses of that pinned
text, using fixtures written from it. There is no upstream conformance suite to run, so what is
proven is "satisfies the clauses as this repository reads them", which is what
`OKF_CONFORMANCE_STATUS` says and no more.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import AsyncIterator
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import yaml
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.envelope_models import (
    MetadataRoutine,
    MetadataRoutineDefinitionVersion,
    MetadataRoutineParameter,
    MetadataViewDefinition,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.models import (
    AccessPolicy,
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    ContextProduct,
    ContextProductVersion,
    CrossBoundaryGrant,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SourceBinding,
    ViewLineageEdge,
    Workspace,
)
from aida.okf_export import (
    DESCRIPTION_APPROVED,
    MANIFEST_FILENAME,
    OKF_CONFORMANCE_STATUS,
    OKF_SPEC_REVISION,
    OKF_SPEC_SHA256,
    OKF_VERSION,
    OkfApproval,
    OkfColumnFacts,
    OkfConceptFacts,
    OkfDefinitionFacts,
    OkfDescription,
    OkfExportError,
    OkfLink,
    OkfObjectFacts,
    OkfPackageFacts,
    OkfParameterFacts,
    OkfPolicyPartition,
    OkfRoutineFacts,
    OkfSchemaFacts,
    OkfScope,
    OkfSnapshot,
    OkfSourceFacts,
    OkfSourceFreshness,
    bundle_archive_bytes,
    concept_key,
    export_okf_bundle,
    object_key,
    package_key,
    routine_key,
    schema_key,
    snapshot_from_document,
    snapshot_to_document,
    source_key,
    validate_atlas_publish_policy,
    validate_okf_conformance,
)
from aida.okf_export_api import download_okf_bundle, inspect_okf_bundle
from aida.okf_snapshot import _admit_datasources, freeze_snapshot
from aida.security import SecurityContext

# Strings that cannot occur naturally, so any hit in an exported byte is a real leak rather
# than a coincidence -- the sentinel discipline `tests/test_inv6_value_freedom.py` uses.
SENTINEL_BODY = "ZZQ-OKF-SENTINEL-BODY-51ab"
SENTINEL_VIEW = "ZZQ-OKF-SENTINEL-VIEWSQL-7c02"
SENTINEL_DEFAULT = "ZZQ-OKF-SENTINEL-DEFAULT-9d41"
SENTINEL_COMMENT = "ZZQ-OKF-SENTINEL-SRCCOMMENT-2e88"
_SENTINELS = (SENTINEL_BODY, SENTINEL_VIEW, SENTINEL_DEFAULT, SENTINEL_COMMENT)

_CAPTURED = "2026-09-17T09:00:00+00:00"
_LATER = "2026-09-18T11:30:00+00:00"


# --- a hand-built snapshot, so the pure half is testable without a database --------------


def _snapshot(**changes: Any) -> OkfSnapshot:
    """One frozen snapshot covering every document kind the exporter renders.

    Built by hand rather than loaded, because the properties below -- determinism, identity,
    absence -- are properties of the rendering, and a fixture that needed a database to state
    them would also need one to explain a failure.
    """
    datasource = "11111111-1111-1111-1111-111111111111"
    src = source_key(datasource)
    sch = schema_key(datasource, "bank", "sales")
    orders = object_key(datasource, "bank", "sales", "orders")
    orders_view = object_key(datasource, "bank", "sales", "orders_v")
    rebuild_int = routine_key(datasource, "bank", "sales", "", "rebuild", "(integer)")
    rebuild_num = routine_key(datasource, "bank", "sales", "", "rebuild", "(numeric)")
    packaged = routine_key(datasource, "bank", "sales", "risk_pkg", "rebuild", "(integer)")
    package = package_key(datasource, "bank", "sales", "risk_pkg")
    values: dict[str, Any] = {
        "captured_at": _CAPTURED,
        "scope": OkfScope(
            kind="CONTEXT_PRODUCT",
            organization_id="org-1",
            policy_partition=OkfPolicyPartition(
                allowed_consumer_roles=("Analyst",),
                source_values="GATEWAY_ONLY",
                classifications=("INTERNAL",),
                purpose="revenue",
            ),
            product_key="revenue_context",
            product_version=2,
            product_version_id="ver-1",
            product_fingerprint="f" * 64,
            product_name="Revenue context",
            product_purpose="Support bounded revenue analysis.",
            eligible_tool_version_ids=("tool-a",),
        ),
        "sources": (
            OkfSourceFacts(
                key=src,
                name="warehouse",
                dialect="postgres",
                connector_type="postgres",
                environment="PROD",
                lifecycle="ACTIVE",
            ),
        ),
        "schemas": (
            OkfSchemaFacts(
                key=sch,
                name="sales",
                catalog_name="bank",
                qualified_name="bank.sales",
                source_key=src,
                lifecycle="ACTIVE",
            ),
        ),
        "objects": (
            OkfObjectFacts(
                key=orders,
                kind="TABLE",
                native_object_type="BASE_TABLE",
                name="orders",
                qualified_name="bank.sales.orders",
                schema_key=sch,
                source_key=src,
                lifecycle="ACTIVE",
                columns=(
                    OkfColumnFacts(
                        name="order_id",
                        ordinal=1,
                        physical_type="uuid",
                        nullable=False,
                        classification="INTERNAL",
                        lifecycle="ACTIVE",
                        description=OkfDescription(
                            state=DESCRIPTION_APPROVED,
                            text="The order's identifier.",
                            version=1,
                            approval=OkfApproval("steward", "2026-09-01T00:00:00+00:00", True),
                        ),
                    ),
                ),
                description=OkfDescription(
                    state=DESCRIPTION_APPROVED,
                    text="One row per completed order.",
                    version=3,
                    approval=OkfApproval("steward", "2026-09-02T00:00:00+00:00", True),
                ),
                links=(OkfLink(target_key=rebuild_int, relation="written by"),),
            ),
            OkfObjectFacts(
                key=orders_view,
                kind="VIEW",
                native_object_type="VIEW",
                name="orders_v",
                qualified_name="bank.sales.orders_v",
                schema_key=sch,
                source_key=src,
                lifecycle="ACTIVE",
                definition=OkfDefinitionFacts(
                    available=True,
                    digest="a" * 64,
                    truncated=False,
                    lineage="ACTIVE",
                ),
                links=(OkfLink(target_key=orders, relation="reads"),),
            ),
        ),
        "routines": (
            OkfRoutineFacts(
                key=rebuild_int,
                name="rebuild",
                package_name="",
                signature="(integer)",
                qualified_name="bank.sales.rebuild",
                routine_type="PROCEDURE",
                schema_key=sch,
                source_key=src,
                lifecycle="ACTIVE",
                language="plpgsql",
                parameters=(
                    OkfParameterFacts(
                        name="p_day", ordinal=1, mode="IN", physical_type="integer"
                    ),
                ),
                definition=OkfDefinitionFacts(
                    available=False,
                    digest=None,
                    truncated=False,
                    lineage="NONE",
                    reason_codes=("DEFINITION_WITHHELD",),
                ),
                links=(OkfLink(target_key=orders, relation="writes"),),
            ),
            OkfRoutineFacts(
                key=rebuild_num,
                name="rebuild",
                package_name="",
                signature="(numeric)",
                qualified_name="bank.sales.rebuild",
                routine_type="PROCEDURE",
                schema_key=sch,
                source_key=src,
                lifecycle="ACTIVE",
            ),
            OkfRoutineFacts(
                key=packaged,
                name="rebuild",
                package_name="risk_pkg",
                signature="(integer)",
                qualified_name="bank.sales.risk_pkg.rebuild",
                routine_type="PROCEDURE",
                schema_key=sch,
                source_key=src,
                lifecycle="ACTIVE",
            ),
        ),
        "packages": (
            OkfPackageFacts(
                key=package,
                name="risk_pkg",
                qualified_name="bank.sales.risk_pkg",
                schema_key=sch,
                source_key=src,
                lifecycle="ACTIVE",
                member_keys=(packaged,),
            ),
        ),
        "concepts": (
            OkfConceptFacts(
                key=concept_key("revenue", 2, "Order"),
                name="Order",
                ontology_key="revenue",
                ontology_version=2,
                lifecycle="APPROVED",
                label="Order",
                definition="A confirmed purchase agreement with a customer.",
                mapped_object_keys=(orders,),
                approval=OkfApproval("human:reviewer", "2026-09-03T00:00:00+00:00", True),
            ),
        ),
        "freshness": (
            OkfSourceFreshness(
                source_key=src,
                last_scan_completed_at="2026-09-16T00:00:00+00:00",
                last_full_scan_completed_at="2026-09-10T00:00:00+00:00",
            ),
        ),
    }
    values.update(changes)
    return OkfSnapshot(**values)


def _texts(snapshot: OkfSnapshot) -> dict[str, str]:
    bundle = export_okf_bundle(snapshot)
    return {document.path: document.text for document in bundle.documents}


# --- determinism ------------------------------------------------------------------------


def test_two_exports_of_one_snapshot_are_byte_identical() -> None:
    """The property everything else rests on, asserted on bytes rather than on a digest.

    A digest comparison would pass against a renderer that hashed its input and ignored it; the
    archive comparison also covers the ZIP, where the default `zipfile` behaviour of stamping
    the current time into every local header would otherwise make two identical bundles differ.
    """
    snapshot = _snapshot()
    first = export_okf_bundle(snapshot)
    second = export_okf_bundle(snapshot)
    assert [(d.path, d.text) for d in first.documents] == [
        (d.path, d.text) for d in second.documents
    ]
    assert first.manifest_json() == second.manifest_json()
    assert bundle_archive_bytes(first) == bundle_archive_bytes(second)


def test_the_frozen_snapshot_round_trips_and_renders_the_same_bytes() -> None:
    """The freeze is a value, not a phrase: write it down, read it back, render it again.

    This is what makes the determinism claim independent of the database. If the exporter read
    anything outside the snapshot -- a catalog row, a clock, a setting -- the reconstructed
    snapshot could not reproduce these bytes, because the reconstruction has none of them.
    """
    snapshot = _snapshot()
    document = snapshot_to_document(snapshot)
    # Survives a JSON round trip, which is what a durable snapshot store (OKF02) will need.
    restored = snapshot_from_document(json.loads(json.dumps(document)))
    assert restored == snapshot
    assert bundle_archive_bytes(export_okf_bundle(restored)) == bundle_archive_bytes(
        export_okf_bundle(snapshot)
    )
    assert restored.content_digest() == snapshot.content_digest()


def test_a_recapture_of_unchanged_content_reproduces_every_hash() -> None:
    """OKF-C at the rendering level: a later freeze of identical content changes nothing.

    The only field that differs is `captured_at`, and it appears only in the manifest -- so the
    concept documents, their per-file hashes, the bundle content digest and the content snapshot
    digest all hold. A document carrying `last_scan_completed_at` would fail this, which is why
    freshness lives in the manifest.
    """
    first = export_okf_bundle(_snapshot())
    second = export_okf_bundle(_snapshot(captured_at=_LATER))
    assert [(d.path, d.text) for d in first.documents] == [
        (d.path, d.text) for d in second.documents
    ]
    assert first.content_digest == second.content_digest
    assert first.manifest["content_snapshot_digest"] == second.manifest["content_snapshot_digest"]
    assert first.manifest["files"] == second.manifest["files"]
    assert first.manifest["captured_at"] != second.manifest["captured_at"]
    differing = {
        key
        for key in first.manifest
        if first.manifest[key] != second.manifest[key]
    }
    assert differing == {"captured_at"}


def test_a_changed_definition_moves_only_its_own_document() -> None:
    """The other half of OKF-C: a real change *does* move a hash, and only the affected one.

    Without this, the no-op test above would pass against a renderer that ignored its input
    entirely.
    """
    snapshot = _snapshot()
    view = snapshot.objects[1]
    assert view.definition is not None
    moved = replace(
        snapshot,
        objects=(
            snapshot.objects[0],
            replace(view, definition=replace(view.definition, digest="b" * 64)),
        ),
    )
    before = {d.path: d.sha256 for d in export_okf_bundle(snapshot).documents}
    after = {d.path: d.sha256 for d in export_okf_bundle(moved).documents}
    changed = {path for path in before if before[path] != after[path]}
    assert changed == {
        path for path in before if path.endswith(f"views/view-{view.key}.md")
    }


def test_the_archive_puts_the_atlas_manifest_outside_the_concept_tree() -> None:
    """The manifest is an Atlas extension, so an OKF reader must not need it.

    Asserted on the archive layout because that is the only place the boundary is visible: every
    concept document is under `bundle/`, and the manifest is not, so a consumer pointed at the
    bundle root sees a pure OKF tree.
    """
    archive = bundle_archive_bytes(export_okf_bundle(_snapshot()))
    with zipfile.ZipFile(__import__("io").BytesIO(archive)) as opened:
        names = opened.namelist()
    assert MANIFEST_FILENAME in names
    assert not MANIFEST_FILENAME.startswith("bundle/")
    assert all(name.startswith("bundle/") for name in names if name != MANIFEST_FILENAME)
    assert "bundle/index.md" in names


# --- identity ---------------------------------------------------------------------------


def test_overloads_source_qualified_names_and_package_members_do_not_collide() -> None:
    """The collision the design names twice, enumerated rather than argued.

    Each pair below differs in exactly one identity component -- the signature, the package, the
    schema, the source -- and each must produce a different key *and* a different document path.
    A collision would not raise; it would silently publish one document where there should be
    two, which reads as a smaller bundle rather than as an error.
    """
    other = "22222222-2222-2222-2222-222222222222"
    one = "11111111-1111-1111-1111-111111111111"
    keys = {
        "overload_int": routine_key(one, "bank", "sales", "", "rebuild", "(integer)"),
        "overload_num": routine_key(one, "bank", "sales", "", "rebuild", "(numeric)"),
        "packaged": routine_key(one, "bank", "sales", "risk_pkg", "rebuild", "(integer)"),
        "other_schema": routine_key(one, "bank", "risk", "", "rebuild", "(integer)"),
        "other_catalog": routine_key(one, "vault", "sales", "", "rebuild", "(integer)"),
        "other_source": routine_key(other, "bank", "sales", "", "rebuild", "(integer)"),
        # A routine and a table of the same name live in different namespaces here too.
        "same_named_table": object_key(one, "bank", "sales", "rebuild"),
        "package_container": package_key(one, "bank", "sales", "risk_pkg"),
    }
    assert len(set(keys.values())) == len(keys), keys

    paths = {document.path for document in export_okf_bundle(_snapshot()).documents}
    routine_paths = {path for path in paths if "/routines/" in path}
    assert len(routine_paths) == 3, routine_paths


def test_every_path_segment_is_an_opaque_safe_segment() -> None:
    """Paths are opaque; names live in the content. A source-qualified name in a filename would
    put a schema name, a case-insensitive collision and a shell metacharacter into a path a
    consumer unpacks."""
    bundle = export_okf_bundle(_snapshot())
    for document in bundle.documents:
        assert not document.path.startswith("/")
        assert ".." not in document.path.split("/")
        assert document.path == document.path.lower()
        assert "orders" not in document.path
        assert "sales" not in document.path
    # ...and the actual name is in the content, where a reader needs it.
    table = next(d for d in bundle.documents if "/tables/" in d.path)
    assert "bank.sales.orders" in table.text


def test_an_identity_collision_refuses_the_export() -> None:
    """Belt to the braces above: if two objects ever did share a key, the export stops.

    A refused export is recoverable; a bundle that quietly lost a document is not, because
    nothing downstream can tell it apart from a bundle whose scope was smaller.
    """
    snapshot = _snapshot()
    duplicated = replace(
        snapshot,
        objects=(snapshot.objects[0], replace(snapshot.objects[1], key=snapshot.objects[0].key)),
    )
    with pytest.raises(OkfExportError, match="identity collision"):
        export_okf_bundle(duplicated)


# --- OKF-D: absent from counts, not only from text --------------------------------------


def test_dropping_an_object_removes_it_from_every_count_and_refuses_a_stale_link() -> None:
    """OKF-D, stated as the two halves that actually matter.

    First half: the counts an index and a manifest render come from the snapshot, so an object
    that never entered is not counted -- there is no "1 withheld" anywhere, because a withheld
    count is itself the existence leak.

    Second half, and the one that makes the first trustworthy: a *link* to the dropped object is
    a refused export rather than a rendered dead end. Without this, the cheap way to satisfy the
    first half would be to filter the index and leave the link, and the reader would learn the
    object exists from a broken href.
    """
    full = export_okf_bundle(_snapshot())
    assert full.manifest["counts"]["routines"] == 3

    snapshot = _snapshot()
    dropped = snapshot.routines[0]
    without = replace(
        snapshot,
        routines=snapshot.routines[1:],
        # The table still names the dropped routine as a dependency.
    )
    with pytest.raises(OkfExportError, match="not in the bundle"):
        export_okf_bundle(without)

    # Dropped properly -- object, link and package membership together -- it is simply absent.
    clean = replace(
        snapshot,
        routines=snapshot.routines[1:],
        objects=(replace(snapshot.objects[0], links=()), snapshot.objects[1]),
    )
    bundle = export_okf_bundle(clean)
    rendered = "\n".join(document.text for document in bundle.documents)
    assert bundle.manifest["counts"]["routines"] == 2
    assert dropped.key not in rendered
    assert dropped.key not in json.dumps(bundle.manifest)
    assert len([row for row in bundle.manifest["source_objects"] if row["kind"] == "ROUTINE"]) == 2
    schema_index = f"schemas/schema-{snapshot.schemas[0].key}/index.md"
    index = next(d for d in bundle.documents if d.path.endswith(schema_index))
    assert dropped.key not in index.text


# --- verification and lifecycle ----------------------------------------------------------


def test_no_catalog_document_claims_a_verification_atlas_cannot_evidence() -> None:
    """A table with an approved description is still not a *verified document*.

    The design is explicit that "publishing a bundle does not mean a reviewer verified every
    inferred statement", and a table document's columns, dependencies and coverage are captured
    rather than reviewed. So document-level `verified` stays off, the approval that does exist is
    carried per statement in the `atlas` extension and as a `sources` credibility signal, and
    spec §5.3 reads the document as `unverified` -- which is the true answer.
    """
    bundle = export_okf_bundle(_snapshot())
    table = next(d for d in bundle.documents if "/tables/" in d.path)
    frontmatter = yaml.safe_load(table.text.split("---\n")[1])
    assert "verified" not in frontmatter
    assert frontmatter["atlas"]["description"]["state"] == DESCRIPTION_APPROVED
    assert frontmatter["atlas"]["description"]["approved_by"] == "human:steward"
    assert frontmatter["atlas"]["statements"] == {
        "approved": ["purpose"],
        "derived": ["columns", "coverage", "dependencies"],
    }
    assert frontmatter["sources"][0]["author"] == "human:steward"
    assert frontmatter["status"] == "stable"


def test_only_a_fully_approved_concept_carries_document_level_verification() -> None:
    """The one document kind whose whole content is approved content carries `verified`; the same
    concept without a recorded approval event carries none.

    An ontology version stores who approved it but not when, so the instant comes from the
    governance review that decided it. No review, no instant, no verification -- rather than a
    verification at an unknown time.
    """
    bundle = export_okf_bundle(_snapshot())
    concept = next(d for d in bundle.documents if d.path.startswith("concepts/concept-"))
    frontmatter = yaml.safe_load(concept.text.split("---\n")[1])
    assert frontmatter["verified"] == [
        {"by": "human:reviewer", "at": "2026-09-03T00:00:00+00:00"}
    ]

    snapshot = _snapshot()
    unapproved = replace(
        snapshot, concepts=(replace(snapshot.concepts[0], approval=None),)
    )
    other = export_okf_bundle(unapproved)
    document = next(d for d in other.documents if d.path.startswith("concepts/concept-"))
    assert "verified" not in yaml.safe_load(document.text.split("---\n")[1])
    assert yaml.safe_load(document.text.split("---\n")[1])["status"] == "draft"


@pytest.mark.parametrize(
    ("state", "expected_status", "expected_phrase"),
    [
        ("APPROVED", "stable", "One row per completed order."),
        ("PROPOSED", "draft", "awaiting review"),
        ("WITHDRAWN", "draft", "was retired"),
        ("WITHHELD", "draft", "screening refused"),
        ("NONE", "draft", "No approved description"),
    ],
)
def test_atlas_description_states_map_to_okf_lifecycle_separately(
    state: str, expected_status: str, expected_phrase: str
) -> None:
    """OKF `status` is mapped from Atlas state, never borrowed from it.

    The interesting row is `WITHDRAWN`: Atlas distinguishes "nobody has described this" from "a
    reviewer retired the description", and OKF has one `status` field for both. `draft` is the
    honest rendering of each, because in both cases there is no current approved statement -- and
    the difference a consumer might act on stays visible in the `atlas` extension and in the
    body.
    """
    snapshot = _snapshot()
    table = snapshot.objects[0]
    description = (
        table.description
        if state == "APPROVED"
        else OkfDescription(state=state, version=3, withheld_reason_codes=("INJECTION_DEFENSE",))
    )
    changed = replace(
        snapshot,
        objects=(replace(table, description=description), snapshot.objects[1]),
    )
    document = next(
        text for path, text in _texts(changed).items() if "/tables/" in path
    )
    frontmatter = yaml.safe_load(document.split("---\n")[1])
    assert frontmatter["status"] == expected_status
    assert expected_phrase in document
    assert frontmatter["atlas"]["description"]["state"] == state


def test_a_deprecated_object_is_deprecated_whatever_its_description_says() -> None:
    """Lifecycle beats description: an object retired at the source is `deprecated` even with an
    approved description, because a consumer's first question is whether it still exists."""
    snapshot = _snapshot()
    changed = replace(
        snapshot,
        objects=(replace(snapshot.objects[0], lifecycle="DEPRECATED"), snapshot.objects[1]),
    )
    document = next(text for path, text in _texts(changed).items() if "/tables/" in path)
    assert yaml.safe_load(document.split("---\n")[1])["status"] == "deprecated"


# --- INV-6: no body, no definition, no value --------------------------------------------


def test_no_snapshot_field_can_hold_a_body_a_definition_or_a_default() -> None:
    """INV-6 structurally: the exporter cannot leak what it has no field for.

    Enumerated over the value types rather than asserted on one rendered bundle, because the way
    this invariant breaks is somebody adding a field -- and a field added tomorrow is exactly
    what a fixture-driven assertion would not see.
    """
    forbidden = (
        "body",
        "body_sql",
        "definition_sql",
        "default_expression",
        "sample",
        "rows",
        "source_description",
        "readme_source",
        "unavailable_reason",
    )
    types = (
        OkfSnapshot,
        OkfScope,
        OkfSourceFacts,
        OkfSchemaFacts,
        OkfObjectFacts,
        OkfColumnFacts,
        OkfRoutineFacts,
        OkfParameterFacts,
        OkfPackageFacts,
        OkfConceptFacts,
        OkfDefinitionFacts,
        OkfDescription,
    )
    offenders = [
        f"{value_type.__name__}.{field.name}"
        for value_type in types
        for field in dataclass_fields(value_type)
        if any(token in field.name for token in forbidden)
    ]
    assert offenders == [], offenders


def test_a_digest_is_exported_where_a_definition_would_be() -> None:
    """The positive half: coverage still says everything a consumer needs, without the text."""
    bundle = export_okf_bundle(_snapshot())
    view = next(d for d in bundle.documents if "/views/" in d.path)
    assert "a" * 64 in view.text
    assert "Definition digest" in view.text
    assert "SELECT" not in view.text.upper().replace("SELECTIVE", "")


# --- conformance against the pinned specification ---------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "okf_conformance"


def test_the_pin_is_recorded_precisely_enough_to_be_rechecked() -> None:
    """The pin is a commit and a digest, not a version number.

    "v0.2" names a family of texts; a revision plus the SHA-256 of `SPEC.md` at that revision
    names one. This test does not fetch anything -- it asserts the pin is *stated* in the form a
    later pass can verify offline, and that the conformance status never reads as certification.
    """
    assert len(OKF_SPEC_REVISION) == 40 and all(
        character in "0123456789abcdef" for character in OKF_SPEC_REVISION
    )
    assert len(OKF_SPEC_SHA256) == 64
    assert OKF_VERSION == "0.2"
    assert OKF_CONFORMANCE_STATUS == "SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES"
    assert "CERTIFIED" not in OKF_CONFORMANCE_STATUS


def test_the_bundle_satisfies_the_pinned_conformance_clauses() -> None:
    """Spec §11, clause by clause, over a bundle this exporter actually produced."""
    bundle = export_okf_bundle(_snapshot())
    documents = {document.path: document.text for document in bundle.documents}
    assert validate_okf_conformance(documents) == validate_okf_conformance(documents)
    verdict = validate_okf_conformance(documents)
    assert verdict.valid, verdict.findings

    for path, text in documents.items():
        if path.rsplit("/", 1)[-1] == "index.md":
            # §8: no frontmatter, except `okf_version` at the bundle root.
            frontmatter = text.split("---\n")[1] if text.startswith("---\n") else None
            if path == "index.md":
                assert yaml.safe_load(frontmatter or "") == {"okf_version": OKF_VERSION}
            else:
                assert frontmatter is None
            continue
        # §11.1 and §11.2: parseable frontmatter with a non-empty `type`.
        assert text.startswith("---\n")
        parsed = yaml.safe_load(text.split("---\n")[1])
        assert isinstance(parsed["type"], str) and parsed["type"].strip()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("type-only.md", ()),
        ("unknown-type-and-keys.md", ()),
        ("bare-verified-mapping.md", ()),
        ("missing-type.md", ("TYPE_MISSING:missing-type.md",)),
        ("empty-type.md", ("TYPE_MISSING:empty-type.md",)),
        ("no-frontmatter.md", ("FRONTMATTER_MISSING:no-frontmatter.md",)),
        (
            "unparseable-frontmatter.md",
            ("FRONTMATTER_UNPARSEABLE:unparseable-frontmatter.md",),
        ),
    ],
)
def test_the_conformance_fixtures_are_judged_as_the_pinned_spec_says(
    name: str, expected: tuple[str, ...]
) -> None:
    """The conformance-fixture hook the row asks for, written from the pinned text.

    Each fixture isolates one clause. The three that must *pass* are the ones a naive validator
    gets wrong: a concept carrying only `type` is fully conformant (§4.1), an unknown `type` and
    unknown keys must not be rejected (§11), and a bare `verified` mapping must be read as a
    one-element list rather than as malformed (§5.2).
    """
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    assert validate_okf_conformance({name: text}).findings == expected


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("log-with-frontmatter.md", ()),
        ("log-with-a-non-iso-date-heading.md", ("LOG_DATE_HEADING_INVALID:log.md",)),
    ],
)
def test_a_reserved_log_file_is_judged_only_by_what_section_9_requires(
    fixture: str, expected: tuple[str, ...]
) -> None:
    """The regression guard for a real bug this exporter had, found by running the checker over
    the upstream reference bundles at the pinned revision.

    An earlier draft rejected frontmatter on a `log.md` by analogy with the index rule in §8.
    §9 forbids no such thing, §11 tells consumers not to reject unknown frontmatter, and
    `bundles/acme_retail/log.md` upstream carries `type` and `title` -- so the checker was
    rejecting a conformant bundle. The ISO date heading, which §9 *does* state as a MUST, is
    still checked, so the fix did not simply delete the rule.
    """
    text = (_FIXTURES / fixture).read_text(encoding="utf-8")
    assert validate_okf_conformance({"log.md": text}).findings == expected


def test_a_conformant_bundle_can_still_fail_the_atlas_publish_policy() -> None:
    """"Keep general OKF parsing distinct from Atlas's stronger publish policy."

    The fixture is conformant OKF -- an external link and a fenced code block are both perfectly
    legal, and a consumer "MUST tolerate broken links". Atlas refuses all three on export, and
    the two verdicts being different is the point: a finding here is a statement about Atlas
    policy, not about the format.

    The third case, a link to a document that is not in the bundle, is appended here rather than
    stored in the fixture: `scripts/check_docs_links.py` walks every `.md` file in the repository,
    and a deliberately dead link committed as a file would fail that gate for the right reason.
    """
    text = (_FIXTURES / "conformant-but-unpublishable.md").read_text(encoding="utf-8")
    text += "\nAnd a [link to nowhere](/tables/missing.md), which section 6.1 tolerates.\n"
    assert validate_okf_conformance({"concept.md": text}).valid

    bundle = export_okf_bundle(_snapshot())
    poisoned = replace(
        bundle,
        documents=(*bundle.documents, type(bundle.documents[0])(path="concept.md", text=text)),
    )
    verdict = validate_atlas_publish_policy(poisoned)
    assert not verdict.valid
    assert any(finding.startswith("EXTERNAL_LINK:concept.md") for finding in verdict.findings)
    assert "FORBIDDEN_CODE_FENCE:concept.md" in verdict.findings
    assert any(finding.startswith("DANGLING_LINK:concept.md") for finding in verdict.findings)


def test_a_produced_bundle_passes_the_atlas_publish_policy() -> None:
    verdict = validate_atlas_publish_policy(export_okf_bundle(_snapshot()))
    assert verdict.valid, verdict.findings


def test_an_oversized_document_is_an_explicit_failure_not_a_truncation() -> None:
    """"Produce an explicit size/coverage failure rather than a silently truncated complete
    bundle." A truncated bundle that still calls itself complete is the failure mode; a refusal
    is the recoverable one."""
    snapshot = _snapshot()
    huge = replace(
        snapshot,
        objects=(
            replace(
                snapshot.objects[0],
                description=OkfDescription(
                    state=DESCRIPTION_APPROVED,
                    text="x " * 200_000,
                    version=1,
                    approval=OkfApproval("steward", "2026-09-02T00:00:00+00:00", True),
                ),
            ),
            snapshot.objects[1],
        ),
    )
    with pytest.raises(OkfExportError, match="document limit"):
        export_okf_bundle(huge)


def test_the_documented_atlas_extension_matches_the_code() -> None:
    """The reference doc is the extension's contract, so it is asserted rather than trusted.

    `Docs/90-reference/okf-export-profile.md` is what an importer (OKF03) and any external
    consumer will read. A doc that drifted from the renderer would be worse than no doc, so
    every key the renderer emits must be named there.
    """
    reference = (
        Path(__file__).resolve().parents[1]
        / "Docs"
        / "90-reference"
        / "okf-export-profile.md"
    ).read_text(encoding="utf-8")
    bundle = export_okf_bundle(_snapshot())
    emitted: set[str] = set()
    for document in bundle.documents:
        if not document.text.startswith("---\n"):
            continue
        frontmatter = yaml.safe_load(document.text.split("---\n")[1])
        extension = frontmatter.get("atlas")
        if not isinstance(extension, dict):
            continue
        # Relative to the `atlas` mapping, as the reference spells them -- a backticked
        # `atlas.x.y` in `Docs/` is read as a Python module path by `tests/test_doc_claims.py`.
        for key, value in extension.items():
            emitted.add(key)
            if isinstance(value, dict):
                emitted.update(f"{key}.{inner}" for inner in value)
    undocumented = sorted(key for key in emitted if f"`{key}`" not in reference)
    assert undocumented == [], undocumented
    assert OKF_SPEC_REVISION in reference
    assert OKF_CONFORMANCE_STATUS in reference


# --- the database half ------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # StaticPool: the gate's durable shadow-record path opens a second session, and every
    # connection to an unpooled in-memory SQLite gets a database of its own.
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


def _context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        # A lifecycle reader, so the purpose and quality gates do not stand in for the
        # authorization this test is actually about.
        roles=frozenset({"DataSteward"}),
    )


async def _estate(session: AsyncSession) -> dict[str, Any]:
    """One organization, two engines, and every shape a document has to render.

    Two datasources with different dialects because OKF-A requires PostgreSQL *and* SQL Server
    fixtures to come out of one normalized path. The sentinels are planted in exactly the columns
    a body, a view definition, a column default and a source comment live in, so the INV-6 test
    below is checking the real egress path rather than a mock of it.
    """
    organization = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=organization.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Finance",
        code=f"F{uuid4().hex[:6]}",
    )
    far_domain = DataDomain(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="HR",
        code=f"H{uuid4().hex[:6]}",
    )
    session.add_all([organization, lob, domain, far_domain])
    await session.flush()
    session.add(
        AccessPolicy(
            organization_id=organization.id,
            code="rbac-parity",
            name="parity",
            effect="ALLOW",
            subject_match={"roles": ["DataSteward"]},
            action_match=[],
            created_by="seed",
        )
    )
    project = Project(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="warehouse",
        slug=f"w-{uuid4().hex[:8]}",
    )
    session.add(project)
    await session.flush()

    estate: dict[str, Any] = {
        "organization": organization,
        "project": project,
        "domain": domain,
        "far_domain": far_domain,
        "tables": {},
        "routines": {},
        "datasources": {},
    }
    for name, dialect, connector, catalog_name, schema_name, domain_id in (
        ("warehouse", "postgres", "postgres", "bank", "sales", domain.id),
        ("reporting", "sqlserver", "sqlserver", "BANK", "dbo", domain.id),
        ("people", "postgres", "postgres", "hr", "staff", far_domain.id),
    ):
        datasource = DataSource(
            id=uuid4(),
            organization_id=organization.id,
            line_of_business_id=lob.id,
            data_domain_id=domain_id,
            project_id=project.id,
            name=name,
            connector_type=connector,
            dialect=dialect,
            environment="PROD",
            network_zone="default",
            credential_reference="env://TEST_DSN",
            capabilities={},
            status="ACTIVE",
        )
        session.add(datasource)
        await session.flush()
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            name=catalog_name,
            fingerprint="f",
        )
        session.add(catalog)
        await session.flush()
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=organization.id,
            catalog_id=catalog.id,
            name=schema_name,
            fingerprint="f",
        )
        session.add(schema)
        await session.flush()
        estate["datasources"][name] = (datasource, catalog, schema)

    for source_name, table_name, object_type in (
        ("warehouse", "orders", "BASE_TABLE"),
        ("warehouse", "orders_v", "VIEW"),
        ("reporting", "revenue_daily", "BASE_TABLE"),
        ("reporting", "revenue_mv", "MATERIALIZED VIEW"),
        ("people", "salaries", "BASE_TABLE"),
    ):
        datasource, _catalog, schema = estate["datasources"][source_name]
        table = MetadataTable(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=table_name,
            object_type=object_type,
            status="ACTIVE",
            # The source's own comment: evidence, never authority, and never exported.
            source_description=f"source comment {SENTINEL_COMMENT}",
            fingerprint="f",
        )
        session.add(table)
        estate["tables"][f"{source_name}.{table_name}"] = table
    await session.flush()

    session.add_all(
        [
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=estate["tables"]["warehouse.orders"].id,
                name="order_id",
                ordinal_position=1,
                physical_type="uuid",
                nullable=False,
                classification="INTERNAL",
                status="ACTIVE",
                fingerprint="f",
            ),
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=estate["tables"]["warehouse.orders"].id,
                name="channel",
                ordinal_position=2,
                physical_type="varchar(20)",
                nullable=True,
                # A default expression is the most literal-bearing column in the catalog.
                default_expression=f"'{SENTINEL_DEFAULT}'",
                classification="INTERNAL",
                status="ACTIVE",
                fingerprint="f",
            ),
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=estate["tables"]["reporting.revenue_daily"].id,
                name="AmountUsd",
                ordinal_position=1,
                physical_type="decimal(18,2)",
                nullable=True,
                classification="CONFIDENTIAL",
                status="ACTIVE",
                fingerprint="f",
            ),
        ]
    )
    session.add_all(
        [
            MetadataViewDefinition(
                id=uuid4(),
                organization_id=organization.id,
                datasource_id=estate["datasources"]["warehouse"][0].id,
                table_id=estate["tables"]["warehouse.orders_v"].id,
                # A stored, already-redacted definition, not a query anything executes.
                definition_sql_redacted=(
                    f"SELECT order_id FROM sales.orders /* {SENTINEL_VIEW} */"  # noqa: S608
                ),
                definition_fingerprint="d" * 64,
                redaction_status="PARSED",
                screening_status="CLEAN",
                availability="AVAILABLE",
                status="ACTIVE",
                fingerprint="f",
            ),
            MetadataViewDefinition(
                id=uuid4(),
                organization_id=organization.id,
                datasource_id=estate["datasources"]["reporting"][0].id,
                table_id=estate["tables"]["reporting.revenue_mv"].id,
                definition_sql_redacted=None,
                availability="UNAVAILABLE",
                unavailable_reason=f"not visible to the scanning principal {SENTINEL_COMMENT}",
                status="ACTIVE",
                fingerprint="f",
            ),
        ]
    )
    await session.flush()
    session.add(
        ViewLineageEdge(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=estate["datasources"]["warehouse"][0].id,
            source_table="sales.orders",
            source_column="order_id",
            target_table="sales.orders_v",
            target_column="order_id",
            source_table_id=estate["tables"]["warehouse.orders"].id,
            target_table_id=estate["tables"]["warehouse.orders_v"].id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h",
            review_status="ACTIVE",
            created_by="parser",
        )
    )

    for source_name, name, package, signature, routine_type in (
        ("warehouse", "rebuild_totals", "", "(integer)", "PROCEDURE"),
        ("warehouse", "rebuild_totals", "", "(numeric)", "PROCEDURE"),
        ("warehouse", "rebuild_totals", "risk_pkg", "(integer)", "PROCEDURE"),
        ("reporting", "fn_revenue", "", "(date)", "FUNCTION"),
    ):
        datasource, _catalog, schema = estate["datasources"][source_name]
        routine = MetadataRoutine(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            package_name=package,
            signature=signature,
            routine_type=routine_type,
            language="plpgsql" if source_name == "warehouse" else "tsql",
            body_sql_redacted=f"BEGIN /* {SENTINEL_BODY} */ NULL; END",
            body_fingerprint="b" * 64,
            redaction_status="PARSED",
            screening_status="CLEAN",
            availability="AVAILABLE",
            status="ACTIVE",
            fingerprint="f",
        )
        session.add(routine)
        estate["routines"][f"{source_name}.{name}{package}{signature}"] = routine
    await session.flush()

    warehouse_routine = estate["routines"]["warehouse.rebuild_totals(integer)"]
    session.add_all(
        [
            MetadataRoutineParameter(
                id=uuid4(),
                organization_id=organization.id,
                datasource_id=warehouse_routine.datasource_id,
                routine_id=warehouse_routine.id,
                name="p_day",
                ordinal_position=1,
                mode="IN",
                physical_type="integer",
                default_expression=f"'{SENTINEL_DEFAULT}'",
                status="ACTIVE",
                fingerprint="f",
            ),
            MetadataRoutineDefinitionVersion(
                id=uuid4(),
                organization_id=organization.id,
                datasource_id=warehouse_routine.datasource_id,
                routine_id=warehouse_routine.id,
                version_number=4,
                body_sql_redacted=f"BEGIN /* {SENTINEL_BODY} */ NULL; END",
                body_fingerprint="b" * 64,
                availability="AVAILABLE",
                truncated=False,
                redaction_status="PARSED",
                screening_status="CLEAN",
                captured_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
        ]
    )

    documentation = AssetDocumentation(
        id=uuid4(),
        organization_id=organization.id,
        table_id=estate["tables"]["warehouse.orders"].id,
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=organization.id,
            documentation_id=documentation.id,
            version=3,
            status="APPROVED",
            readme="One row per completed order across all channels.",
            created_by="drafter",
            approved_by="reviewer",
            approved_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    order_id_column = await session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.organization_id == organization.id,
            MetadataColumn.table_id == estate["tables"]["warehouse.orders"].id,
            MetadataColumn.name == "order_id",
        )
    )
    assert order_id_column is not None
    column_documentation = ColumnDocumentation(
        id=uuid4(),
        organization_id=organization.id,
        table_id=estate["tables"]["warehouse.orders"].id,
        column_id=order_id_column.id,
    )
    session.add(column_documentation)
    await session.flush()
    session.add(
        ColumnDocumentationVersion(
            id=uuid4(),
            organization_id=organization.id,
            documentation_id=column_documentation.id,
            version=1,
            status="APPROVED",
            description="Globally unique order identifier.",
            created_by="drafter",
            approved_by="reviewer",
            approved_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    routine_documentation = RoutineDocumentation(
        id=uuid4(),
        organization_id=organization.id,
        datasource_id=warehouse_routine.datasource_id,
        routine_id=warehouse_routine.id,
    )
    session.add(routine_documentation)
    await session.flush()
    session.add(
        RoutineDocumentationVersion(
            id=uuid4(),
            organization_id=organization.id,
            documentation_id=routine_documentation.id,
            version=2,
            status="APPROVED",
            description="Rebuilds the daily order totals for one day.",
            created_by="drafter",
            approved_by="reviewer",
            approved_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
    )
    await session.flush()
    return estate


async def _product(
    session: AsyncSession,
    estate: dict[str, Any],
    *,
    include_far_source: bool,
    product_key: str = "revenue_context",
) -> tuple[ContextProduct, ContextProductVersion]:
    organization = estate["organization"]
    table_ids = [
        str(estate["tables"]["warehouse.orders"].id),
        str(estate["tables"]["warehouse.orders_v"].id),
        str(estate["tables"]["reporting.revenue_daily"].id),
        str(estate["tables"]["reporting.revenue_mv"].id),
    ]
    if include_far_source:
        table_ids.append(str(estate["tables"]["people.salaries"].id))
    routine_ids = [str(routine.id) for routine in estate["routines"].values()]
    product = ContextProduct(
        id=uuid4(),
        organization_id=organization.id,
        project_id=estate["project"].id,
        product_key=product_key,
        lifecycle_status="ACTIVE",
        created_by="maker",
    )
    session.add(product)
    await session.flush()
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=organization.id,
        product_id=product.id,
        version=2,
        status="PUBLISHED",
        name="Revenue context",
        description="Approved revenue metadata.",
        purpose="Support bounded revenue analysis.",
        owner_type="GROUP",
        owner_principal="revenue-owner",
        table_ids=table_ids,
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=["tool-a"],
        routine_ids=routine_ids,
        ontology_version_ids=[],
        allowed_consumer_roles=["Analyst", "DataSteward"],
        lineage_depth=2,
        quality_requirements={"minimum_score": 0},
        policy_summary={"source_values": "GATEWAY_ONLY", "classifications": ["INTERNAL"]},
        fingerprint="a" * 64,
        created_by="maker",
        approved_by="reviewer",
        approved_at=datetime(2026, 9, 6, tzinfo=UTC),
        published_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    session.add(version)
    await session.flush()
    return product, version


async def _freeze(
    session: AsyncSession,
    estate: dict[str, Any],
    settings: Settings,
    *,
    include_far_source: bool = False,
    captured_at: datetime | None = None,
    product_key: str = "revenue_context",
    version: ContextProductVersion | None = None,
) -> OkfSnapshot:
    """Freeze through the compiler's own scope resolver, as the routes do.

    `version` lets a caller freeze the *same* published version twice, which is what a no-op
    re-capture actually is: a second read of unchanged rows under an unchanged scope.
    """
    from aida.context_compiler_api import _load_source

    if version is None:
        _created, version = await _product(
            session, estate, include_far_source=include_far_source, product_key=product_key
        )
    context = _context(estate["organization"].id)
    (
        loaded_product,
        loaded_version,
        _tables,
        _negative,
        _exemplars,
        routines,
        views,
        ontology,
        freshness,
        _quality,
    ) = await _load_source(session, version.id, context)
    return await freeze_snapshot(
        session,
        context,
        settings,
        product=loaded_product,
        version=loaded_version,
        routines=routines,
        views=views,
        ontology=ontology,
        freshness=freshness,
        captured_at=captured_at or datetime(2026, 9, 17, 9, tzinfo=UTC),
    )


async def test_both_engines_produce_documents_through_one_path(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-A: a PostgreSQL and a SQL Server source, one exporter, per-object documents and
    source/schema indexes for both -- with each engine's own type spellings preserved rather
    than normalized into a portable lie.
    """
    estate = await _estate(session)
    snapshot = await _freeze(session, estate, settings)
    bundle = export_okf_bundle(snapshot)
    paths = {document.path for document in bundle.documents}

    assert {source.dialect for source in snapshot.sources} == {"postgres", "sqlserver"}
    # One root index, one per source, one per schema.
    assert "index.md" in paths
    assert len([path for path in paths if path.endswith("/index.md")]) == 2 + 2
    assert len([path for path in paths if "/tables/" in path]) == 2
    assert len([path for path in paths if "/views/" in path]) == 2
    assert len([path for path in paths if "/routines/" in path]) == 4
    assert validate_atlas_publish_policy(bundle).valid

    rendered = "\n".join(document.text for document in bundle.documents)
    # Native types and native object kinds, verbatim.
    assert "`decimal(18,2)`" in rendered
    assert "`varchar(20)`" in rendered
    assert "MATERIALIZED VIEW" in rendered
    assert "Atlas Materialized View" in rendered
    # The approved description reached the document; the source's own comment did not.
    assert "One row per completed order across all channels." in rendered
    assert "Globally unique order identifier." in rendered
    assert "Rebuilds the daily order totals for one day." in rendered


async def test_no_sentinel_body_definition_default_or_source_comment_reaches_the_bundle(
    session: AsyncSession, settings: Settings
) -> None:
    """INV-6 on the real egress path, with sentinels in every column that could leak one.

    A routine body, a view definition, a column default, a routine-parameter default, a source
    comment and a connector's free-text `unavailable_reason` are all planted and all absent --
    from the documents, from the manifest and from the archive bytes, which is where a
    compressed leak would still be findable.
    """
    estate = await _estate(session)
    snapshot = await _freeze(session, estate, settings)
    bundle = export_okf_bundle(snapshot)
    haystacks = [
        "\n".join(document.text for document in bundle.documents),
        bundle.manifest_json(),
        json.dumps(snapshot_to_document(snapshot)),
        bundle_archive_bytes(bundle).decode("latin-1"),
    ]
    for sentinel in _SENTINELS:
        for haystack in haystacks:
            assert sentinel not in haystack, sentinel

    # The withheld definition is still *reported*, by bounded code rather than by free text.
    rendered = haystacks[0]
    assert "DEFINITION_WITHHELD" in rendered
    assert "withheld" in rendered


async def test_a_refused_datasource_is_absent_from_text_links_and_counts(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-D: a source across a data-domain boundary with no grant is not in the bundle at all.

    The product names a table in another data domain. With no ACTIVE `CrossBoundaryGrant`, that
    datasource is refused by `_admit_datasources`, and the assertions below are deliberately
    about *counts* and the manifest as well as about the text: a bundle that said "5 tables, one
    withheld" would satisfy a text-only reading of OKF-D and still tell the reader that a table
    they may not see exists.

    Then the grant is added and the same product exports the same source, which proves the
    absence was the authorization decision and not a bug in the loader.
    """
    estate = await _estate(session)
    snapshot = await _freeze(session, estate, settings, include_far_source=True)
    bundle = export_okf_bundle(snapshot)
    rendered = "\n".join(document.text for document in bundle.documents) + bundle.manifest_json()

    assert {source.name for source in snapshot.sources} == {"warehouse", "reporting"}
    assert bundle.manifest["counts"]["sources"] == 2
    assert bundle.manifest["counts"]["tables"] == 2
    assert bundle.manifest["counts"]["schemas"] == 2
    assert "salaries" not in rendered
    assert "staff" not in rendered
    assert "people" not in rendered
    assert "withheld" not in bundle.manifest_json()
    assert str(estate["tables"]["people.salaries"].id) not in rendered
    root = bundle.document("index.md").text
    assert "Sources in scope: 2." in root
    assert "Objects in scope: 4" in root

    session.add(
        CrossBoundaryGrant(
            id=uuid4(),
            organization_id=estate["organization"].id,
            source_data_domain_id=estate["far_domain"].id,
            target_data_domain_id=estate["domain"].id,
            reason="approved revenue-to-people crossing",
            status="ACTIVE",
            edge_kinds=[],
            requested_by="steward",
            approved_by="reviewer",
        )
    )
    await session.flush()
    granted = await _freeze(
        session, estate, settings, include_far_source=True, product_key="revenue_context_granted"
    )
    assert {source.name for source in granted.sources} == {
        "warehouse",
        "reporting",
        "people",
    }
    assert export_okf_bundle(granted).manifest["counts"]["tables"] == 3


async def test_the_gate_refusing_a_datasource_leaves_it_out(
    session: AsyncSession, settings: Settings
) -> None:
    """The other refusal path in `_admit_datasources`, driven directly.

    The cross-boundary test above never reaches the gate for the refused source, so this one
    exercises the gate itself: with `DENY` posture, a datasource with no workspace binding is
    unresolved and refused, while one bound to a workspace resolves and is admitted. Both
    branches fail closed to *omission*, never to a signal.
    """
    estate = await _estate(session)
    bound, _catalog, _schema = estate["datasources"]["warehouse"]
    unbound, _catalog2, _schema2 = estate["datasources"]["reporting"]
    workspace = Workspace(
        id=uuid4(),
        organization_id=estate["organization"].id,
        name="Migrated",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="p",
        authorization_mode="SHADOW",
    )
    session.add(workspace)
    await session.flush()
    session.add(
        SourceBinding(
            id=uuid4(),
            organization_id=estate["organization"].id,
            workspace_id=workspace.id,
            datasource_id=bound.id,
            purpose="grandfathered",
            status="ACTIVE",
            requested_by="migration",
        )
    )
    await session.flush()

    admitted = await _admit_datasources(
        session,
        _context(estate["organization"].id),
        Settings(_env_file=None, unresolved_workspace_posture="DENY"),
        organization_id=estate["organization"].id,
        seed_domain_id=estate["domain"].id,
        datasource_ids=[bound.id, unbound.id],
    )
    assert set(admitted) == {bound.id}


async def test_a_recapture_from_the_same_database_reproduces_every_document_hash(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-C against a real database: freeze twice, minutes apart, nothing changed.

    Every concept document, every per-file hash and both digests hold. The second freeze is a new
    read of the same rows -- which is exactly the situation a completed no-op scan produces -- so
    a document that had picked up `last_scan_completed_at` or an export timestamp would fail
    here.
    """
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    first = await _freeze(
        session,
        estate,
        settings,
        version=version,
        captured_at=datetime(2026, 9, 17, 9, tzinfo=UTC),
    )
    second = await _freeze(
        session,
        estate,
        settings,
        version=version,
        captured_at=datetime(2026, 9, 17, 23, 45, tzinfo=UTC),
    )
    left = export_okf_bundle(first)
    right = export_okf_bundle(second)
    assert [(d.path, d.sha256) for d in left.documents] == [
        (d.path, d.sha256) for d in right.documents
    ]
    assert left.content_digest == right.content_digest
    assert first.content_digest() == second.content_digest()
    assert left.manifest["captured_at"] != right.manifest["captured_at"]
    # The same snapshot twice is byte-identical all the way through the archive.
    assert bundle_archive_bytes(export_okf_bundle(first)) == bundle_archive_bytes(
        export_okf_bundle(first)
    )


async def test_the_manifest_route_reports_the_pin_the_digests_and_the_file_index(
    session: AsyncSession, settings: Settings
) -> None:
    """The inspect route: manifest, file index, verdict -- and no document bodies."""
    estate = await _estate(session)
    product, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    read = await inspect_okf_bundle(version.id, context, session, settings)
    assert read.okf_version == OKF_VERSION
    assert read.spec_revision == OKF_SPEC_REVISION
    assert read.spec_conformance == OKF_CONFORMANCE_STATUS
    assert read.valid, read.findings
    assert read.document_count == len(read.files)
    assert all(len(entry.sha256) == 64 for entry in read.files)
    assert read.manifest["scope"]["product_key"] == product.product_key
    assert read.manifest["policy_partition"]["source_values"] == "GATEWAY_ONLY"
    assert read.manifest["source_objects"]
    assert read.manifest["source_freshness"] is not None
    # The index carries paths and digests, never text.
    assert not any("Purpose" in json.dumps(entry.model_dump()) for entry in read.files)


async def test_the_download_route_returns_one_deterministic_archive(
    session: AsyncSession, settings: Settings
) -> None:
    """The download route, and the header a caller compares a stored bundle against."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    first = await download_okf_bundle(version.id, context, session, settings)
    second = await download_okf_bundle(version.id, context, session, settings)
    assert first.media_type == "application/zip"
    assert first.headers["X-Atlas-OKF-Spec-Revision"] == OKF_SPEC_REVISION
    assert first.headers["X-Atlas-Bundle-Content-SHA256"] == second.headers[
        "X-Atlas-Bundle-Content-SHA256"
    ]
    with zipfile.ZipFile(__import__("io").BytesIO(first.body)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(MANIFEST_FILENAME))
    assert "bundle/index.md" in names
    assert manifest["specification"]["revision"] == OKF_SPEC_REVISION
    assert manifest["atlas_extension"] is True


async def test_a_foreign_organization_is_refused_before_any_bundle_is_built(
    session: AsyncSession, settings: Settings
) -> None:
    """INV-5 on the new surface: the tenant boundary is the compiler's resolver, reached first."""
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    foreign = SecurityContext(
        principal_id="intruder",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )
    with pytest.raises(HTTPException) as refused:
        await inspect_okf_bundle(version.id, foreign, session, settings)
    assert refused.value.status_code == 403
    assert "cross-organization" in str(refused.value.detail)
