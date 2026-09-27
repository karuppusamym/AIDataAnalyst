#!/usr/bin/env python3
"""Offline, free measurement of OKF context retrieval (tracker R11-OKF02, R11-FP13).

**RETRIEVAL ONLY -- NOT ANSWER QUALITY.** Given a question, does the stored knowledge bundle
put the right object in front of the model? That is all this measures. It never asks a model
anything, so it says nothing about whether an answer written from those documents is correct;
that needs paid model calls nobody has authorised, and this script has no way to make one
(there is no `--live`, no provider import and no embedding). Ranking in `aida.okf_context` is
lexical -- BM25-style idf over names, approved descriptions, column names and meanings, concept
aliases and tool inputs -- so this is a measurement of a deterministic function, and the same
input gives the same numbers on every machine.

What is measured, per question with known expected object(s):

  * hit@1, hit@3 and "delivered": is an expected object among the first k *distinct subjects*
    handed to the reader (column-set documents count as their parent object), or anywhere in the
    returned documents. `direct` says the ranker chose it, `via_link` says a one-hop link from a
    chosen document brought it (a concept to its mapped table, a routine to the table it
    writes). First-hit rank and MRR follow from the same order.
  * The status split: MATCHED, AMBIGUOUS (the API's `MATCHED` with a non-empty `ambiguous`
    list, i.e. two subjects of one kind the question cannot tell apart) and NO_MATCH, and what
    each was *supposed* to be. A NO_MATCH answer to a question the bundle cannot answer is a
    correct one; a MATCHED answer to it is a false match.
  * The false-ambiguity rate: among questions the corpus says have exactly one right object,
    how often the ranker reported a tie. And the correct-ambiguity rate for questions the corpus
    says two objects are equally right.
  * Gap preservation: an object the corpus says must NOT be delivered (its only path is lineage
    nobody approved) stays out.
  * Characters returned against the budget, and how often a section was cut.
  * Optionally, that a phrase the answer stands on is in the delivered text (a wide table's
    column meaning is delivered only when the right column set is).

Ablations, in-process only: the same questions ranked from a modified copy of the frozen
snapshot with a signal removed -- `no_descriptions` (every approved description, column meaning
and concept definition), `no_aliases`, and `names_only` (also columns, parameters and tool
inputs). The documents handed out are always the full bundle's, so a difference is a difference
in what the ranker could see and nothing else. Nothing is written to any store.

Two modes:

    # Default. Builds the estate from the committed fixture (the harness estate of
    # scripts/quality_benchmark.py plus the approved descriptions in the corpus), renders the
    # real bundle with aida.okf_export, and selects with aida.okf_context. No network, no
    # database, no settings, deterministic; this is what CI runs.
    python scripts/okf_context_benchmark.py

    # Against a running stack: reads the manifest, then POSTs each question to the source
    # bundle's context route (or a product version's) with development identity headers, exactly
    # as scripts/seed_sample_estate.py sends them. Reads only, but every call leaves the
    # platform's ordinary read audit record. AIDA_BASE_URL is the default base URL. A question
    # about an object this estate does not hold is skipped and listed, never counted as a miss.
    python scripts/okf_context_benchmark.py --api http://localhost:8000 --org sample-bank

No acceptance threshold is set for any number here, and the script exits 0 whatever it
measures: it reports, it does not judge. Nothing about the ranking was tuned to this corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aida.okf_context import (  # noqa: E402
    DEFAULT_MAX_CHARS,
    MAX_CHARS_LIMIT,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    OkfContext,
    assemble_context,
    hop_targets,
    parse_document,
    plan_context,
)
from aida.okf_export import (  # noqa: E402
    DESCRIPTION_APPROVED,
    TYPE_COLUMN_SET,
    TYPE_CONCEPT,
    TYPE_MATERIALIZED_VIEW,
    TYPE_ROUTINE,
    TYPE_TABLE,
    TYPE_TOOL_VERSION,
    TYPE_VIEW,
    OkfApproval,
    OkfColumnFacts,
    OkfConceptFacts,
    OkfDefinitionFacts,
    OkfDescription,
    OkfLink,
    OkfObjectFacts,
    OkfPolicyPartition,
    OkfRoutineFacts,
    OkfSchemaFacts,
    OkfScope,
    OkfSnapshot,
    OkfSourceFacts,
    OkfToolFacts,
    concept_key,
    document_subjects,
    export_okf_bundle,
    object_key,
    routine_key,
    schema_key,
    source_key,
    tool_version_key,
    validate_atlas_publish_policy,
)
from scripts.quality_benchmark import (  # noqa: E402
    FOOTPRINT_CONCEPT,
    FOOTPRINT_ROUTINE_DESCRIPTION,
    FOOTPRINT_ROUTINE_SEEDS,
    TABLE_SEEDS,
    TOOL_SEEDS,
    _fixed_id,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "quality_benchmark_corpus"
DEFAULT_CORPUS = CORPUS_DIR / "okf_context_corpus.json"

MEASURES: Final = "retrieval only, not answer quality"
BANNER: Final = (
    "RETRIEVAL ONLY -- not answer quality. No model, no embedding, no provider call; "
    "the in-process mode also makes no network call."
)
NO_THRESHOLD: Final = (
    "No acceptance threshold is set for any metric. This run reports what was measured and "
    "does not judge it; the ranking was not tuned to this corpus."
)

STATUS_AMBIGUOUS: Final = "AMBIGUOUS"
OBJECT_TYPES: Final = ("TABLE", "ROUTINE", "ONTOLOGY_CONCEPT", "GOVERNED_TOOL")
#: A source bundle holds no business concepts and no tools, so a case that needs either can only
#: be asked of a product bundle.
PRODUCT_ONLY_TYPES: Final = frozenset({"ONTOLOGY_CONCEPT", "GOVERNED_TOOL"})
VARIANTS: Final = ("full", "no_descriptions", "no_aliases", "names_only")
RANK_CUTOFFS: Final = (1, 3)
#: The REST route refuses less than this (`OkfContextRequest.max_chars`), so in-process asks the
#: same range and the two modes stay comparable.
MIN_MAX_CHARS: Final = 1_000
KIND_SOURCE: Final = "SOURCE"
KIND_PRODUCT: Final = "PRODUCT"
MODE_IN_PROCESS: Final = "in-process"
MODE_API: Final = "api"

CATALOG_NAME: Final = "warehouse"
SCHEMA_NAME: Final = "public"
_AT: Final = "2026-09-17T00:00:00+00:00"
_CAPTURED_AT: Final = "2026-09-19T00:00:00+00:00"
#: Wording as `aida.okf_snapshot._object_limitations` / `_routine_limitations` write it, so the
#: fixture documents carry the same honesty sections a frozen bundle would (they are delivered
#: text, so they count toward the character budget).
_NO_PURPOSE_OBJECT: Final = (
    "Atlas asserts no approved purpose for this object; nothing here is a reviewed statement "
    "of what it means."
)
_NO_PURPOSE_ROUTINE: Final = (
    "Atlas asserts no approved purpose for this routine; nothing here is a reviewed statement "
    "of what it does."
)
_PROPOSED_LINEAGE_ROUTINE: Final = (
    "Procedure lineage is proposed and undecided; it steers nothing and is not shown as a "
    "dependency."
)


class CorpusError(ValueError):
    """The corpus, or the estate it is measured against, is not something to measure."""


class ApiError(RuntimeError):
    """A running stack refused, or could not answer, a read this script made."""


# --- objects ----------------------------------------------------------------------------------


def normalise(name: str) -> str:
    """One spelling for an object across the corpus, a document title and a manifest name."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """An object in the corpus's vocabulary (`footprint_enrichment_corpus.json`)."""

    object_type: str
    object_key: str

    @staticmethod
    def of(object_type: str, object_key: str) -> ObjectRef:
        return ObjectRef(object_type, normalise(object_key))

    @property
    def label(self) -> str:
        return f"{self.object_type}:{self.object_key}"


_DOCUMENT_TYPE_TO_OBJECT: Final = {
    TYPE_TABLE: "TABLE",
    TYPE_VIEW: "TABLE",
    TYPE_MATERIALIZED_VIEW: "TABLE",
    TYPE_COLUMN_SET: "TABLE",
    TYPE_ROUTINE: "ROUTINE",
    TYPE_CONCEPT: "ONTOLOGY_CONCEPT",
    TYPE_TOOL_VERSION: "GOVERNED_TOOL",
}


def identify(document_type: str, title: str) -> ObjectRef | None:
    """The object a bundle document is about, from its OKF `type` and `title` alone.

    One rule for both modes, because an API response carries no more than these. A view or
    materialized view is a TABLE (as retrieval hits are); a column set is its parent object, whose
    title it prefixes; a tool's title has its version appended. Indexes and logs are about no
    object and identify as None.
    """
    object_type = _DOCUMENT_TYPE_TO_OBJECT.get(document_type)
    if object_type is None:
        return None
    name = title
    if document_type == TYPE_COLUMN_SET:
        name = title.split(":", 1)[0]
    if object_type == "GOVERNED_TOOL":
        name = re.sub(r"\s*\(version \d+\)\s*$", "", name)
    if object_type in {"TABLE", "ROUTINE"}:
        name = name.rsplit(".", 1)[-1]
    return ObjectRef.of(object_type, name)


# --- corpus -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    category: str
    question: str
    expected_status: str
    expected: tuple[ObjectRef, ...]
    forbidden: tuple[ObjectRef, ...] = ()
    expected_text: tuple[str, ...] = ()
    exactly_one_answer: bool = False
    expect_ambiguous: bool = False
    product_only: bool = False
    #: The ground truth exists only in the fixture estate -- approved text the corpus's overlay
    #: authored, or an object only the fixture holds -- so the API mode skips the case rather
    #: than score a running estate's absence of it as a ranking miss.
    fixture_only: bool = False
    #: "own", or `<corpus file>#<case id>` for a case whose question and expected object are read
    #: from another corpus at load time.
    provenance: str = "own"

    @property
    def needs_product(self) -> bool:
        named = (*self.expected, *self.forbidden)
        return self.product_only or any(ref.object_type in PRODUCT_ONLY_TYPES for ref in named)


@dataclass(frozen=True, slots=True)
class Corpus:
    description: str
    estate: Mapping[str, Any]
    cases: tuple[Case, ...]
    path: str


def _as_mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{where}: expected an object, found {type(value).__name__}")
    return value


def _as_list(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise CorpusError(f"{where}: expected a list, found {type(value).__name__}")
    return value


def _refs(items: object, where: str) -> tuple[ObjectRef, ...]:
    refs: list[ObjectRef] = []
    for index, raw in enumerate(_as_list(items, where)):
        item = _as_mapping(raw, f"{where}[{index}]")
        object_type = str(item.get("object_type", ""))
        if object_type not in OBJECT_TYPES:
            raise CorpusError(f"{where}[{index}]: unsupported object_type {object_type!r}")
        key = str(item.get("object_key", ""))
        if not normalise(key):
            raise CorpusError(f"{where}[{index}]: empty object_key")
        refs.append(ObjectRef.of(object_type, key))
    return tuple(refs)


def _source_case(corpus_dir: Path, corpus_file: str, case_id: str) -> Mapping[str, Any]:
    path = corpus_dir / corpus_file
    if not path.is_file():
        raise CorpusError(f"reuse: corpus file {corpus_file!r} not found in {corpus_dir}")
    data = _as_mapping(json.loads(path.read_text(encoding="utf-8")), corpus_file)
    for raw in _as_list(data.get("cases"), f"{corpus_file}.cases"):
        case = _as_mapping(raw, f"{corpus_file}.cases[]")
        if case.get("id") == case_id:
            return case
    raise CorpusError(f"reuse: {corpus_file} has no case {case_id!r}")


def _reused_objects(
    source: Mapping[str, Any], where: str
) -> tuple[tuple[ObjectRef, ...], tuple[ObjectRef, ...]]:
    """(expected, forbidden) from either of the two existing corpora's case shapes."""
    if "expected_evidence" in source:
        return (
            _refs(source["expected_evidence"], f"{where}.expected_evidence"),
            _refs(source.get("forbidden_evidence", []), f"{where}.forbidden_evidence"),
        )
    if "expected_object_type" in source:
        ref = ObjectRef.of(str(source["expected_object_type"]), str(source["expected_object_key"]))
        if ref.object_type not in OBJECT_TYPES:
            raise CorpusError(f"{where}: unsupported object_type {ref.object_type!r}")
        # `expect_absent` is the footprint corpus's gap: the object must stay unreached.
        return ((), (ref,)) if source.get("expect_absent") else ((ref,), ())
    raise CorpusError(f"{where}: neither expected_evidence nor expected_object_type")


def _build_case(entry: Mapping[str, Any], corpus_dir: Path) -> Case:
    provenance = "own"
    expected: tuple[ObjectRef, ...] = ()
    forbidden: tuple[ObjectRef, ...] = ()
    reuse = entry.get("reuse")
    if reuse is not None:
        spec = _as_mapping(reuse, "reuse")
        corpus_file, source_id = str(spec.get("corpus", "")), str(spec.get("id", ""))
        source = _source_case(corpus_dir, corpus_file, source_id)
        provenance = f"{corpus_file}#{source_id}"
        case_id = str(entry.get("id") or source_id)
        question = str(source.get("question", ""))
        expected, forbidden = _reused_objects(source, provenance)
    else:
        case_id = str(entry.get("id", ""))
        question = str(entry.get("question", ""))
        expected = _refs(entry.get("expected", []), f"{case_id}.expected")
    if "expected" in entry and reuse is not None:
        expected = _refs(entry["expected"], f"{case_id}.expected")
    if "forbidden" in entry:
        forbidden = _refs(entry["forbidden"], f"{case_id}.forbidden")
    if not case_id:
        raise CorpusError("a case has no id")
    if not question.strip() or len(question) > 2_000:
        raise CorpusError(f"{case_id}: the question is empty or over 2,000 characters")
    category = str(entry.get("category", ""))
    if not category:
        raise CorpusError(f"{case_id}: no category")
    status = entry.get("expected_status")
    if status is None:
        if not expected:
            raise CorpusError(
                f"{case_id}: no expected object, and no explicit expected_status NO_MATCH -- "
                "a question with no ground truth is refused, never scored"
            )
        status = STATUS_MATCHED
    if status not in (STATUS_MATCHED, STATUS_NO_MATCH):
        raise CorpusError(f"{case_id}: expected_status must be MATCHED or NO_MATCH")
    if status == STATUS_MATCHED and not expected:
        raise CorpusError(f"{case_id}: expected_status MATCHED but no expected object")
    if status == STATUS_NO_MATCH and expected:
        raise CorpusError(f"{case_id}: expected_status NO_MATCH but expected objects are named")
    exactly_one = bool(entry.get("exactly_one_answer", False))
    ambiguous = bool(entry.get("expect_ambiguous", False))
    if exactly_one and ambiguous:
        raise CorpusError(f"{case_id}: exactly_one_answer and expect_ambiguous contradict")
    return Case(
        id=case_id,
        category=category,
        question=question,
        expected_status=str(status),
        expected=expected,
        forbidden=forbidden,
        expected_text=tuple(
            str(item) for item in _as_list(entry.get("expected_text", []), case_id)
        ),
        exactly_one_answer=exactly_one,
        expect_ambiguous=ambiguous,
        product_only=bool(entry.get("product_only", False)),
        fixture_only=bool(entry.get("fixture_only", False)),
        provenance=provenance,
    )


def load_corpus(path: Path = DEFAULT_CORPUS) -> Corpus:
    """Load and validate the corpus. Refuses rather than scores anything ill-defined: a question
    with no expected object and no explicit NO_MATCH, a duplicate id, an unknown object type, a
    reused case that does not exist."""
    data = _as_mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
    cases: list[Case] = []
    seen: set[str] = set()
    for index, raw in enumerate(_as_list(data.get("cases"), "cases")):
        case = _build_case(_as_mapping(raw, f"cases[{index}]"), path.parent)
        if case.id in seen:
            raise CorpusError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise CorpusError("the corpus holds no cases")
    return Corpus(
        description=str(data.get("description", "")),
        estate=_as_mapping(data.get("estate", {}), "estate"),
        cases=tuple(cases),
        path=str(path),
    )


# --- the fixture estate -----------------------------------------------------------------------


def _approved(text: str) -> OkfDescription:
    return OkfDescription(
        state=DESCRIPTION_APPROVED,
        text=text,
        version=1,
        approval=OkfApproval("steward", _AT, True),
    )


def _columns(spec: Mapping[str, Any], where: str) -> tuple[OkfColumnFacts, ...]:
    named = [_as_mapping(item, where) for item in _as_list(spec.get("named", []), where)]
    total = max(int(spec.get("total", len(named))), len(named))
    placed: dict[int, Mapping[str, Any]] = {}
    unplaced: list[Mapping[str, Any]] = []
    for item in named:
        if item.get("ordinal") is None:
            unplaced.append(item)
            continue
        ordinal = int(item["ordinal"])
        if not 1 <= ordinal <= total or ordinal in placed:
            raise CorpusError(f"{where}: ordinal {ordinal} is out of range or used twice")
        placed[ordinal] = item
    free = (number for number in range(1, total + 1) if number not in placed)
    for item, number in zip(unplaced, free, strict=False):
        placed[number] = item
    columns: list[OkfColumnFacts] = []
    for ordinal in range(1, total + 1):
        given = placed.get(ordinal)
        name = str(given["name"]) if given is not None else f"attr_{ordinal:03d}"
        meaning = str(given["meaning"]) if given is not None and given.get("meaning") else None
        columns.append(
            OkfColumnFacts(
                name=name,
                ordinal=ordinal,
                physical_type="text",
                nullable=True,
                classification="INTERNAL",
                lifecycle="ACTIVE",
                description=_approved(meaning) if meaning else OkfDescription(),
            )
        )
    return tuple(columns)


def build_fixture_snapshot(estate: Mapping[str, Any]) -> OkfSnapshot:
    """The harness estate as a frozen OKF snapshot: what `aida.okf_snapshot` would freeze from it.

    Names, routines, lineage review states, the concept and its alias, the approved routine
    description and the tool are `scripts/quality_benchmark.py`'s own constants, imported, so the
    two harnesses measure one estate. The corpus's `estate` overlay adds what that harness's
    tables lack -- approved descriptions and column meanings, for a subset. Lineage follows the
    freeze's rule (`aida.context_product_coverage`): only ACTIVE edges become reads and writes, so
    a routine whose lineage is PROPOSED carries no link and says so in its limitations.
    """
    datasource = str(_fixed_id("datasource"))
    src = source_key(datasource)
    schema = schema_key(datasource, CATALOG_NAME, SCHEMA_NAME)
    descriptions = _as_mapping(estate.get("table_descriptions", {}), "estate.table_descriptions")
    column_specs = _as_mapping(estate.get("columns", {}), "estate.columns")
    table_keys = {
        name: object_key(datasource, CATALOG_NAME, SCHEMA_NAME, name) for name, _ in TABLE_SEEDS
    }
    routine_keys = {
        name: routine_key(datasource, CATALOG_NAME, SCHEMA_NAME, "", name, "()")
        for name, *_ in FOOTPRINT_ROUTINE_SEEDS
    }
    active = {
        name: (reads, writes)
        for name, reads, writes, review in FOOTPRINT_ROUTINE_SEEDS
        if review == "ACTIVE"
    }

    objects: list[OkfObjectFacts] = []
    for name, _source_comment in TABLE_SEEDS:
        text = descriptions.get(name)
        description = _approved(str(text)) if text else OkfDescription()
        links = [
            OkfLink(target_key=routine_keys[routine], relation="written by")
            for routine, (_reads, writes) in sorted(active.items())
            if writes == name
        ] + [
            OkfLink(target_key=routine_keys[routine], relation="read by")
            for routine, (reads, _writes) in sorted(active.items())
            if reads == name
        ]
        objects.append(
            OkfObjectFacts(
                key=table_keys[name],
                kind="TABLE",
                native_object_type="BASE TABLE",
                name=name,
                qualified_name=f"{CATALOG_NAME}.{SCHEMA_NAME}.{name}",
                schema_key=schema,
                source_key=src,
                lifecycle="ACTIVE",
                columns=_columns(
                    _as_mapping(column_specs.get(name, {}), name), f"estate.columns.{name}"
                ),
                description=description,
                links=tuple(sorted(set(links), key=lambda link: (link.relation, link.target_key))),
                limitations=() if text else (_NO_PURPOSE_OBJECT,),
            )
        )

    described, described_text = FOOTPRINT_ROUTINE_DESCRIPTION
    routines: list[OkfRoutineFacts] = []
    for name, reads, writes, review in FOOTPRINT_ROUTINE_SEEDS:
        is_active = review == "ACTIVE"
        routine_links = (
            (
                OkfLink(target_key=table_keys[reads], relation="reads"),
                OkfLink(target_key=table_keys[writes], relation="writes"),
            )
            if is_active
            else ()
        )
        limitations: list[str] = []
        if not is_active:
            limitations.append(_PROPOSED_LINEAGE_ROUTINE)
        if name != described:
            limitations.append(_NO_PURPOSE_ROUTINE)
        routines.append(
            OkfRoutineFacts(
                key=routine_keys[name],
                name=name,
                package_name="",
                signature="()",
                qualified_name=f"{CATALOG_NAME}.{SCHEMA_NAME}.{name}",
                routine_type="PROCEDURE",
                schema_key=schema,
                source_key=src,
                lifecycle="ACTIVE",
                language="plpgsql",
                description=_approved(described_text) if name == described else OkfDescription(),
                definition=OkfDefinitionFacts(
                    available=True,
                    digest=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                    truncated=False,
                    lineage="ACTIVE" if is_active else "PROPOSED",
                ),
                links=tuple(
                    sorted(routine_links, key=lambda link: (link.relation, link.target_key))
                ),
                limitations=tuple(limitations),
            )
        )

    concept_id, concept_name, aliases, mapped_table = FOOTPRINT_CONCEPT
    del concept_id  # the ontology's own key; the freeze names a concept by its `name`
    concepts = (
        OkfConceptFacts(
            key=concept_key("banking", 1, concept_name),
            name=concept_name,
            ontology_key="banking",
            ontology_version=1,
            lifecycle="ACTIVE",
            definition=str(estate.get("concept_definition") or "") or None,
            mapped_object_keys=(table_keys[mapped_table],),
            approval=OkfApproval("quality-benchmark-reviewer", _AT, True),
            aliases=tuple(sorted(aliases)),
        ),
    )
    project = str(_fixed_id("project"))
    tools = tuple(
        OkfToolFacts(
            key=tool_version_key(project, slug, 1),
            tool_version_id=str(_fixed_id("tool-version", slug, "1")),
            slug=slug,
            name=name,
            version=1,
            lifecycle="PUBLISHED",
            source_key=src,
            fingerprint="t" * 64,
            description=_approved(description),
        )
        for slug, name, description, _referenced_table in TOOL_SEEDS
    )
    return OkfSnapshot(
        captured_at=_CAPTURED_AT,
        scope=OkfScope(
            kind="CONTEXT_PRODUCT",
            organization_id=str(_fixed_id("org")),
            policy_partition=OkfPolicyPartition(
                allowed_consumer_roles=("Analyst",),
                source_values="GATEWAY_ONLY",
                classifications=("INTERNAL",),
            ),
            product_key="core_warehouse_context",
            product_version=1,
        ),
        sources=(
            OkfSourceFacts(
                key=src,
                name="core-warehouse",
                dialect="postgres",
                connector_type="POSTGRES",
                environment="PRODUCTION",
                lifecycle="ACTIVE",
            ),
        ),
        schemas=(
            OkfSchemaFacts(
                key=schema,
                name=SCHEMA_NAME,
                catalog_name=CATALOG_NAME,
                qualified_name=f"{CATALOG_NAME}.{SCHEMA_NAME}",
                source_key=src,
                lifecycle="ACTIVE",
            ),
        ),
        objects=tuple(objects),
        routines=tuple(routines),
        concepts=concepts,
        tools=tools,
    )


@dataclass(frozen=True, slots=True)
class FixtureBundle:
    """The rendered bundle a stored publication would hold, and what is in it."""

    snapshot: OkfSnapshot
    documents: Mapping[str, tuple[str, str]]
    inventory: frozenset[ObjectRef]
    facts: Mapping[str, int]


def render_fixture(estate: Mapping[str, Any]) -> FixtureBundle:
    """Render the fixture with the real exporter, and refuse one the platform would not publish."""
    snapshot = build_fixture_snapshot(estate)
    bundle = export_okf_bundle(snapshot)
    verdict = validate_atlas_publish_policy(bundle)
    if not verdict.valid:
        raise CorpusError(f"the fixture bundle is not publishable: {list(verdict.findings)}")
    documents = {item.path: (item.text, item.sha256) for item in bundle.documents}
    inventory: set[ObjectRef] = set()
    for path in document_subjects(snapshot):
        parsed = parse_document(path, *documents[path])
        ref = identify(parsed.type, parsed.title)
        if ref is not None:
            inventory.add(ref)
    approved = sum(1 for obj in snapshot.objects if obj.description.state == DESCRIPTION_APPROVED)
    facts = {
        "documents": len(documents),
        "objects": len(snapshot.objects),
        "objects_with_approved_description": approved,
        "routines": len(snapshot.routines),
        "concepts": len(snapshot.concepts),
        "tools": len(snapshot.tools),
        "wide_objects": sum(1 for obj in snapshot.objects if len(obj.columns) > 100),
    }
    return FixtureBundle(snapshot, documents, frozenset(inventory), facts)


# --- ablations --------------------------------------------------------------------------------


def _without_descriptions(snapshot: OkfSnapshot) -> OkfSnapshot:
    return replace(
        snapshot,
        objects=tuple(
            replace(
                obj,
                description=OkfDescription(),
                columns=tuple(replace(col, description=OkfDescription()) for col in obj.columns),
            )
            for obj in snapshot.objects
        ),
        routines=tuple(replace(item, description=OkfDescription()) for item in snapshot.routines),
        packages=tuple(replace(item, description=OkfDescription()) for item in snapshot.packages),
        concepts=tuple(replace(item, definition=None) for item in snapshot.concepts),
        tools=tuple(replace(item, description=OkfDescription()) for item in snapshot.tools),
    )


def _without_aliases(snapshot: OkfSnapshot) -> OkfSnapshot:
    return replace(
        snapshot, concepts=tuple(replace(item, aliases=()) for item in snapshot.concepts)
    )


def _names_only(snapshot: OkfSnapshot) -> OkfSnapshot:
    stripped = _without_aliases(_without_descriptions(snapshot))
    return replace(
        stripped,
        objects=tuple(replace(obj, columns=()) for obj in stripped.objects),
        routines=tuple(replace(item, parameters=()) for item in stripped.routines),
        tools=tuple(replace(item, inputs=()) for item in stripped.tools),
    )


def ablate(snapshot: OkfSnapshot, variant: str) -> OkfSnapshot:
    """A modified *copy* of the snapshot for the ranker to read. Nothing is written anywhere."""
    if variant == "full":
        return snapshot
    if variant == "no_descriptions":
        return _without_descriptions(snapshot)
    if variant == "no_aliases":
        return _without_aliases(snapshot)
    if variant == "names_only":
        return _names_only(snapshot)
    raise ValueError(f"unknown variant {variant!r}")


def select_with_ranking(
    ranking: OkfSnapshot,
    documents: Mapping[str, tuple[str, str]],
    question: str,
    *,
    max_chars: int,
) -> OkfContext:
    """`aida.okf_context.select_context`, with the ranker reading `ranking` and the documents
    handed out being the full bundle's. For `full` the two are the same snapshot."""
    plan = plan_context(ranking, question)
    loaded = {path: documents[path] for path in plan.paths if path in documents}
    hops = {path: documents[path] for path in hop_targets(plan, loaded) if path in documents}
    return assemble_context(plan, loaded, hops, max_chars=max_chars)


# --- what one question got --------------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


@dataclass(frozen=True, slots=True)
class DeliveredDocument:
    path: str
    type: str
    title: str
    hop: int
    score: float
    ref: ObjectRef | None


@dataclass(frozen=True, slots=True)
class Selection:
    """A context answer in the fields both modes have: what `OkfContextRead` carries."""

    status: str
    documents: tuple[DeliveredDocument, ...]
    ambiguous: tuple[str, ...]
    max_chars: int
    used_chars: int
    omitted_count: int
    text: str


def observed_status(status: str, ambiguous: Sequence[str]) -> str:
    """The API says MATCHED and lists the tied documents; three-way, that is AMBIGUOUS."""
    return STATUS_AMBIGUOUS if status == STATUS_MATCHED and ambiguous else status


def selection_from_context(context: OkfContext) -> Selection:
    return Selection(
        status=observed_status(context.status, context.ambiguous),
        documents=tuple(
            DeliveredDocument(
                path=item.path,
                type=item.type,
                title=item.title,
                hop=item.hop,
                score=item.score,
                ref=identify(item.type, item.title),
            )
            for item in context.documents
        ),
        ambiguous=tuple(context.ambiguous),
        max_chars=context.max_chars,
        used_chars=context.used_chars,
        omitted_count=context.omitted_count,
        text=_squash(" ".join(part.text for item in context.documents for part in item.sections)),
    )


def selection_from_response(payload: Mapping[str, Any]) -> Selection:
    """The same projection, from the JSON of `POST .../okf-bundle/context`."""
    documents: list[DeliveredDocument] = []
    texts: list[str] = []
    for raw in _as_list(payload.get("documents", []), "documents"):
        item = _as_mapping(raw, "document")
        kind, title = str(item.get("type", "")), str(item.get("title", ""))
        documents.append(
            DeliveredDocument(
                path=str(item.get("path", "")),
                type=kind,
                title=title,
                hop=int(item.get("hop", 0)),
                score=float(item.get("score", 0.0)),
                ref=identify(kind, title),
            )
        )
        for part in _as_list(item.get("sections", []), "sections"):
            texts.append(str(_as_mapping(part, "section").get("text", "")))
    ambiguous = tuple(str(value) for value in _as_list(payload.get("ambiguous", []), "ambiguous"))
    return Selection(
        status=observed_status(str(payload.get("status", "")), ambiguous),
        documents=tuple(documents),
        ambiguous=ambiguous,
        max_chars=int(payload.get("max_chars", 0)),
        used_chars=int(payload.get("used_chars", 0)),
        omitted_count=int(payload.get("omitted_count", 0)),
        text=_squash(" ".join(texts)),
    )


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: Case
    observed_status: str
    #: Distinct delivered objects in the order the reader gets them (column sets fold into their
    #: parent; directly ranked subjects first, then those a link brought).
    delivered: tuple[str, ...]
    rank: int | None
    #: "direct", "via_link", "missed", or "n/a" for a case expecting NO_MATCH.
    reached: str
    complete: bool
    forbidden_delivered: tuple[str, ...]
    text_present: bool | None
    used_chars: int
    max_chars: int
    omitted_count: int
    documents: int


def score_case(case: Case, selection: Selection) -> CaseResult:
    order: list[ObjectRef] = []
    hops: dict[ObjectRef, int] = {}
    for document in selection.documents:
        if document.ref is None:
            continue
        if document.ref not in hops:
            order.append(document.ref)
            hops[document.ref] = document.hop
        else:
            hops[document.ref] = min(hops[document.ref], document.hop)
    expected = set(case.expected)
    rank = next((index for index, ref in enumerate(order, 1) if ref in expected), None)
    if case.expected_status == STATUS_NO_MATCH:
        reached = "n/a"
    elif rank is None:
        reached = "missed"
    else:
        reached = "direct" if hops[order[rank - 1]] == 0 else "via_link"
    text_present = (
        all(_squash(phrase) in selection.text for phrase in case.expected_text)
        if case.expected_text
        else None
    )
    return CaseResult(
        case=case,
        observed_status=selection.status,
        delivered=tuple(ref.label for ref in order),
        rank=rank,
        reached=reached,
        complete=bool(expected) and expected <= set(order),
        forbidden_delivered=tuple(ref.label for ref in order if ref in set(case.forbidden)),
        text_present=text_present,
        used_chars=selection.used_chars,
        max_chars=selection.max_chars,
        omitted_count=selection.omitted_count,
        documents=len(selection.documents),
    )


# --- aggregation ------------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    """Counts and a rate; the rate is None -- not 0.0 -- when nothing was measured."""
    return {
        "n": numerator,
        "of": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def summarise(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Every number the report prints, from the per-case results alone."""
    wanting = [item for item in results if item.case.expected_status == STATUS_MATCHED]
    silent = [item for item in results if item.case.expected_status == STATUS_NO_MATCH]
    summary: dict[str, Any] = {"cases": len(results), "cases_expecting_objects": len(wanting)}
    for cutoff in RANK_CUTOFFS:
        hits = sum(1 for item in wanting if item.rank is not None and item.rank <= cutoff)
        summary[f"hit_at_{cutoff}"] = _ratio(hits, len(wanting))
    summary["delivered"] = _ratio(sum(1 for item in wanting if item.rank is not None), len(wanting))
    summary["mrr"] = (
        round(sum(1 / item.rank for item in wanting if item.rank) / len(wanting), 4)
        if wanting
        else None
    )
    summary["reached"] = {
        kind: sum(1 for item in wanting if item.reached == kind)
        for kind in ("direct", "via_link", "missed")
    }
    multi = [item for item in wanting if len(item.case.expected) > 1]
    summary["all_expected_delivered"] = _ratio(
        sum(1 for item in multi if item.complete), len(multi)
    )
    summary["no_match_correct"] = _ratio(
        sum(1 for item in silent if item.observed_status == STATUS_NO_MATCH), len(silent)
    )
    summary["false_matches"] = [
        item.case.id for item in silent if item.observed_status != STATUS_NO_MATCH
    ]
    summary["refused_when_answerable"] = [
        item.case.id for item in wanting if item.observed_status == STATUS_NO_MATCH
    ]
    summary["status_when_objects_expected"] = dict(
        sorted(Counter(item.observed_status for item in wanting).items())
    )
    summary["status_when_no_match_expected"] = dict(
        sorted(Counter(item.observed_status for item in silent).items())
    )
    single = [item for item in results if item.case.exactly_one_answer]
    summary["false_ambiguity"] = _ratio(
        sum(1 for item in single if item.observed_status == STATUS_AMBIGUOUS), len(single)
    )
    tied = [item for item in results if item.case.expect_ambiguous]
    summary["correct_ambiguity"] = _ratio(
        sum(1 for item in tied if item.observed_status == STATUS_AMBIGUOUS), len(tied)
    )
    gaps = [item for item in results if item.case.forbidden]
    summary["gap_preserved"] = _ratio(
        sum(1 for item in gaps if not item.forbidden_delivered), len(gaps)
    )
    texts = [item for item in results if item.text_present is not None]
    summary["evidence_text_delivered"] = _ratio(
        sum(1 for item in texts if item.text_present), len(texts)
    )
    used = [item.used_chars for item in results]
    summary["chars"] = {
        "mean_used": round(sum(used) / len(used), 1) if used else None,
        "max_used": max(used) if used else None,
        "mean_share_of_budget": (
            round(
                sum(item.used_chars / item.max_chars for item in results if item.max_chars)
                / len(results),
                4,
            )
            if results
            else None
        ),
        "cases_at_90_percent_of_budget": sum(
            1 for item in results if item.max_chars and item.used_chars >= 0.9 * item.max_chars
        ),
        "cases_with_sections_left_out": sum(1 for item in results if item.omitted_count),
        "mean_documents": round(sum(item.documents for item in results) / len(results), 2)
        if results
        else None,
    }
    return summary


# --- reports ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Report:
    mode: str
    corpus_path: str
    budgets: tuple[int, ...]
    #: variant -> budget -> per-case results. Only `full` outside the in-process mode.
    variants: Mapping[str, Mapping[int, Sequence[CaseResult]]]
    estate: Mapping[str, Any]
    skipped: Sequence[Mapping[str, str]]
    cases_total: int
    cases_reused: int

    def to_json(self) -> dict[str, Any]:
        variants: dict[str, Any] = {}
        for name, by_budget in self.variants.items():
            variants[name] = {
                str(budget): {
                    "summary": summarise(results),
                    "cases": [_case_json(item) for item in results],
                }
                for budget, results in by_budget.items()
            }
        return {
            "measures": MEASURES,
            "mode": self.mode,
            "no_threshold": NO_THRESHOLD,
            "corpus": {
                "path": Path(self.corpus_path).name,
                "cases": self.cases_total,
                "reused_from_other_corpora": self.cases_reused,
            },
            "estate": dict(self.estate),
            "budgets": list(self.budgets),
            "skipped": [dict(item) for item in self.skipped],
            "variants": variants,
        }


def _case_json(item: CaseResult) -> dict[str, Any]:
    return {
        "id": item.case.id,
        "category": item.case.category,
        "provenance": item.case.provenance,
        "expected_status": item.case.expected_status,
        "expected": [ref.label for ref in item.case.expected],
        "forbidden": [ref.label for ref in item.case.forbidden],
        "observed_status": item.observed_status,
        "rank": item.rank,
        "reached": item.reached,
        "complete": item.complete,
        "forbidden_delivered": list(item.forbidden_delivered),
        "text_present": item.text_present,
        "delivered": list(item.delivered),
        "used_chars": item.used_chars,
        "max_chars": item.max_chars,
        "omitted_count": item.omitted_count,
        "documents": item.documents,
    }


def check_budgets(budgets: Sequence[int]) -> tuple[int, ...]:
    for value in budgets:
        if not MIN_MAX_CHARS <= value <= MAX_CHARS_LIMIT:
            raise CorpusError(
                f"--max-chars {value} is outside {MIN_MAX_CHARS}..{MAX_CHARS_LIMIT}, the range "
                "the REST route accepts"
            )
    return tuple(dict.fromkeys(budgets))


def run_in_process(
    corpus: Corpus,
    *,
    budgets: Sequence[int] = (DEFAULT_MAX_CHARS,),
    only: Sequence[str] = (),
) -> Report:
    """Every case against every ablation of the fixture bundle. No network, no store."""
    limits = check_budgets(budgets)
    fixture = render_fixture(corpus.estate)
    cases = _select_cases(corpus, only)
    for case in cases:
        missing = [
            ref.label for ref in (*case.expected, *case.forbidden) if ref not in fixture.inventory
        ]
        if missing:
            raise CorpusError(
                f"{case.id}: names object(s) the fixture estate does not hold: {missing}"
            )
    variants: dict[str, dict[int, list[CaseResult]]] = {}
    for variant in VARIANTS:
        ranking = ablate(fixture.snapshot, variant)
        variants[variant] = {
            budget: [
                score_case(
                    case,
                    selection_from_context(
                        select_with_ranking(
                            ranking, fixture.documents, case.question, max_chars=budget
                        )
                    ),
                )
                for case in cases
            ]
            for budget in limits
        }
    return Report(
        mode=MODE_IN_PROCESS,
        corpus_path=corpus.path,
        budgets=limits,
        variants=variants,
        estate={
            "kind": "fixture",
            "catalog": (
                "core-warehouse (scripts/quality_benchmark.py's estate, plus the corpus overlay)"
            ),
            **fixture.facts,
        },
        skipped=(),
        cases_total=len(cases),
        cases_reused=sum(1 for case in cases if case.provenance != "own"),
    )


def _select_cases(corpus: Corpus, only: Sequence[str]) -> tuple[Case, ...]:
    if not only:
        return corpus.cases
    known = {case.id for case in corpus.cases}
    unknown = sorted(set(only) - known)
    if unknown:
        raise CorpusError(f"--case names no such case: {unknown}")
    return tuple(case for case in corpus.cases if case.id in set(only))


# --- against a running stack ------------------------------------------------------------------

#: (method, url, headers, JSON body) -> (status, JSON payload). Injected so the whole API mode is
#: exercised in tests with no network.
Transport = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None], tuple[int, Any]]


def http_transport(
    method: str, url: str, headers: Mapping[str, str], body: Mapping[str, Any] | None
) -> tuple[int, Any]:
    if not url.startswith(("http://", "https://")):
        raise ApiError(f"refusing to open non-HTTP URL: {url}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(detail) if detail else None
        except json.JSONDecodeError:
            return error.code, detail
    except urllib.error.URLError as error:
        raise ApiError(f"{method} {url} -> {error.reason}") from error


def api_headers(principal: str, roles: str, organization_id: str | None) -> dict[str, str]:
    """Development identity headers, exactly as `scripts/seed_sample_estate.py` sends them. The
    default role is the least that reaches the OKF routes; production rejects header identities
    entirely, so this is for a development or demonstration stack only."""
    headers = {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": roles,
        "X-Business-Purpose": "Read-only retrieval-quality measurement of stored OKF bundles",
        "Content-Type": "application/json",
    }
    if organization_id:
        headers["X-Organization-Id"] = organization_id
    return headers


_UUID: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True, slots=True)
class ApiTarget:
    kind: str
    label: str
    bundle_path: str
    headers: Mapping[str, str]


def _items(payload: Any) -> list[Mapping[str, Any]]:
    rows = payload.get("items", []) if isinstance(payload, dict) else payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _get(
    transport: Transport,
    base: str,
    path: str,
    headers: Mapping[str, str],
    *,
    hint: str = "",
) -> Any:
    status, payload = transport("GET", f"{base}{path}", headers, None)
    if status != 200:
        note = f" ({hint})" if hint and status in (400, 403) else ""
        raise ApiError(f"GET {path} -> HTTP {status}: {_detail(payload)}{note}")
    return payload


def _detail(payload: Any) -> str:
    return str(payload.get("detail", payload)) if isinstance(payload, dict) else str(payload)


def discover_target(
    transport: Transport,
    base: str,
    *,
    org: str,
    datasource: str | None,
    product_version: str | None,
    principal: str,
    roles: str,
) -> ApiTarget:
    """Find the bundle to ask, by id or by name, using reads only."""
    org_id = org
    if not _UUID.match(org):
        found = [
            row
            for row in _items(
                _get(
                    transport,
                    base,
                    "/v1/organizations?limit=200",
                    api_headers(principal, roles, None),
                    hint=(
                        "looking an organization up by name needs PlatformAdmin; "
                        "pass its id to --org to stay at the role you are reading as"
                    ),
                )
            )
            if org in (row.get("slug"), row.get("name"))
        ]
        if len(found) != 1:
            raise ApiError(f"organization {org!r} matched {len(found)} organizations; pass its id")
        org_id = str(found[0]["id"])
    headers = api_headers(principal, roles, org_id)
    if product_version:
        return ApiTarget(
            KIND_PRODUCT,
            f"context product version {product_version}",
            f"/v1/context-product-versions/{product_version}/okf-bundle",
            headers,
        )
    wanted = datasource or "Customer Master"
    if _UUID.match(wanted):
        return ApiTarget(
            KIND_SOURCE, f"data source {wanted}", f"/v1/datasources/{wanted}/okf-bundle", headers
        )
    rows = _items(
        _get(transport, base, f"/v1/organizations/{org_id}/datasources?limit=200", headers)
    )
    matches = [row for row in rows if str(row.get("name", "")).lower().startswith(wanted.lower())]
    if len(matches) != 1:
        names = sorted(str(row.get("name")) for row in rows)
        raise ApiError(
            f"data source {wanted!r} matched {len(matches)} of {names}; pass --datasource"
        )
    chosen = matches[0]
    return ApiTarget(
        KIND_SOURCE,
        f"data source {chosen.get('name')}",
        f"/v1/datasources/{chosen['id']}/okf-bundle",
        headers,
    )


def inventory_from_manifest(
    manifest: Mapping[str, Any],
) -> tuple[frozenset[ObjectRef], dict[str, int]]:
    """Objects and routines the stored bundle holds, and how many have an approved description.

    From the manifest's `source_objects`, which name every table, view and routine. Concepts and
    tools are not listed there, so a case that needs one cannot be checked for presence.
    """
    refs: set[ObjectRef] = set()
    approved = 0
    rows = [
        _as_mapping(row, "source_objects[]")
        for row in _as_list(manifest.get("source_objects", []), "source_objects")
    ]
    for row in rows:
        qualified = re.sub(r"\(.*\)$", "", str(row.get("qualified_name", "")))
        kind = "ROUTINE" if row.get("kind") == "ROUTINE" else "TABLE"
        refs.add(ObjectRef.of(kind, qualified.rsplit(".", 1)[-1]))
        approved += 1 if row.get("description_state") == DESCRIPTION_APPROVED else 0
    return frozenset(refs), {
        "objects_and_routines": len(rows),
        "with_approved_description": approved,
    }


def run_api(
    corpus: Corpus,
    target: ApiTarget,
    transport: Transport,
    base: str,
    *,
    budgets: Sequence[int] = (DEFAULT_MAX_CHARS,),
    only: Sequence[str] = (),
) -> Report:
    """The full ranking, as the running stack serves it. No ablations: the stack's ranker cannot
    be re-run on a modified snapshot from outside, and this mode writes nothing.

    A case is skipped, and listed, when it needs a product bundle and this is a source bundle, or
    when an object it names is not in this estate -- a question about an object that is not there
    is a fact about the estate, not a miss by the ranker.
    """
    limits = check_budgets(budgets)
    # `OkfBundleRead`: the publication and counts are top-level fields; the stored manifest, which
    # names every object and routine in `source_objects`, is nested under `manifest`.
    body = _as_mapping(_get(transport, base, target.bundle_path, target.headers), "bundle")
    inventory, coverage = inventory_from_manifest(_as_mapping(body.get("manifest", {}), "manifest"))
    publication = _as_mapping(body.get("publication", {}), "publication")
    skipped: list[dict[str, str]] = []
    chosen: list[Case] = []
    for case in _select_cases(corpus, only):
        if case.fixture_only:
            skipped.append(
                {
                    "id": case.id,
                    "reason": (
                        "ground truth exists only in the fixture estate (text or an object "
                        "authored for it), which a running stack does not carry"
                    ),
                }
            )
            continue
        if case.needs_product and target.kind == KIND_SOURCE:
            skipped.append(
                {
                    "id": case.id,
                    "reason": (
                        "needs a product bundle (a concept or a tool); "
                        "a source bundle holds neither"
                    ),
                }
            )
            continue
        absent = [
            ref.label
            for ref in (*case.expected, *case.forbidden)
            if ref.object_type not in PRODUCT_ONLY_TYPES and ref not in inventory
        ]
        if absent:
            skipped.append({"id": case.id, "reason": f"not in this estate: {', '.join(absent)}"})
            continue
        chosen.append(case)
    results: dict[int, list[CaseResult]] = {budget: [] for budget in limits}
    for budget in limits:
        for case in chosen:
            status, payload = transport(
                "POST",
                f"{base}{target.bundle_path}/context",
                target.headers,
                {"question": case.question, "max_chars": budget},
            )
            if status != 200:
                hint = (
                    " (try --roles with a role the datasource read decision admits)"
                    if status == 403
                    else ""
                )
                raise ApiError(
                    f"{case.id}: POST context -> HTTP {status}: {_detail(payload)}{hint}"
                )
            results[budget].append(
                score_case(case, selection_from_response(_as_mapping(payload, "context response")))
            )
    return Report(
        mode=MODE_API,
        corpus_path=corpus.path,
        budgets=limits,
        variants={"full": results},
        estate={
            "kind": target.kind.lower(),
            "target": target.label,
            "profile": body.get("profile"),
            "publication_sequence": publication.get("sequence"),
            "publication_id": publication.get("publication_id"),
            "documents": body.get("document_count"),
            **coverage,
        },
        skipped=skipped,
        cases_total=len(chosen),
        cases_reused=sum(1 for case in chosen if case.provenance != "own"),
    )


# --- output -----------------------------------------------------------------------------------


def _fmt(cell: Mapping[str, Any]) -> str:
    return "n/a" if cell["rate"] is None else f"{cell['n']}/{cell['of']} ({cell['rate']:.3f})"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], *, markdown: bool) -> list[str]:
    if markdown:
        return [
            "| " + " | ".join(header) + " |",
            "|" + "|".join("---" for _ in header) + "|",
            *("| " + " | ".join(row) + " |" for row in rows),
        ]
    widths = [
        max(len(str(line[index])) for line in (header, *rows)) for index in range(len(header))
    ]
    return [
        "  ".join(str(cell).ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in (header, *rows)
    ]


def render(report: Report, *, markdown: bool = False) -> str:
    """The report as text, or as the Markdown the results document embeds."""
    payload = report.to_json()
    first = report.budgets[0]
    heading = "## " if markdown else ""
    out = [
        f"{heading}OKF context retrieval benchmark ({report.mode})",
        "",
        BANNER,
        NO_THRESHOLD,
        "",
        "Estate: " + ", ".join(f"{key}={value}" for key, value in payload["estate"].items()),
        f"Cases: {report.cases_total} ({report.cases_reused} reused from other corpora); "
        f"budget {', '.join(str(b) for b in report.budgets)} characters",
    ]
    if report.skipped:
        out.append(f"Skipped {len(report.skipped)}:")
        out.extend(f"  {item['id']}: {item['reason']}" for item in report.skipped)
    out += [
        "",
        f"{heading}Ablations at budget {first}"
        if len(report.variants) > 1
        else f"{heading}Result at budget {first}",
        "",
    ]
    retrieval: list[list[str]] = []
    behaviour: list[list[str]] = []
    for name, by_budget in report.variants.items():
        s = summarise(by_budget[first])
        retrieval.append(
            [
                name,
                _fmt(s["hit_at_1"]),
                _fmt(s["hit_at_3"]),
                _fmt(s["delivered"]),
                "n/a" if s["mrr"] is None else f"{s['mrr']:.3f}",
                f"{s['reached']['direct']}/{s['reached']['via_link']}/{s['reached']['missed']}",
            ]
        )
        behaviour.append(
            [
                name,
                _fmt(s["no_match_correct"]),
                _fmt(s["false_ambiguity"]),
                _fmt(s["correct_ambiguity"]),
                _fmt(s["gap_preserved"]),
                _fmt(s["evidence_text_delivered"]),
                "n/a" if s["chars"]["mean_used"] is None else f"{s['chars']['mean_used']:.0f}",
            ]
        )
    out += _table(
        ["variant", "hit@1", "hit@3", "delivered", "MRR", "direct/link/missed"],
        retrieval,
        markdown=markdown,
    )
    out += [""]
    out += _table(
        [
            "variant",
            "NO_MATCH correct",
            "false ambiguity",
            "correct ambiguity",
            "gap kept",
            "text delivered",
            "mean chars",
        ],
        behaviour,
        markdown=markdown,
    )
    if len(report.budgets) > 1 and "full" in report.variants:
        out += ["", f"{heading}Budget sweep (full ranking)", ""]
        sweep = []
        for budget in report.budgets:
            s = summarise(report.variants["full"][budget])
            sweep.append(
                [
                    str(budget),
                    _fmt(s["hit_at_1"]),
                    _fmt(s["delivered"]),
                    _fmt(s["evidence_text_delivered"]),
                    f"{s['chars']['mean_used']:.0f}",
                    str(s["chars"]["cases_with_sections_left_out"]),
                ]
            )
        out += _table(
            [
                "budget",
                "hit@1",
                "delivered",
                "text delivered",
                "mean chars",
                "cases with sections cut",
            ],
            sweep,
            markdown=markdown,
        )
    full = report.variants.get("full", {}).get(first, ())
    others = [name for name in report.variants if name != "full"]
    out += ["", f"{heading}Per case (full ranking, budget {first})", ""]
    case_rows = []
    for item in full:
        moved = [
            name
            for name in others
            if _outcome(report.variants[name][first], item.case.id) != _outcome(full, item.case.id)
        ]
        case_rows.append(
            [
                item.case.id,
                item.case.category,
                item.observed_status,
                "-" if item.rank is None else str(item.rank),
                item.reached,
                str(item.used_chars),
                ",".join(moved) or "-",
            ]
        )
    out += _table(
        ["case", "category", "status", "rank", "reached", "chars", "differs under"],
        case_rows,
        markdown=markdown,
    )
    return "\n".join(out) + "\n"


def _outcome(results: Sequence[CaseResult], case_id: str) -> tuple[str, int | None]:
    item = next(result for result in results if result.case.id == case_id)
    return item.observed_status, item.rank


# --- command line -----------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieval-only measurement of stored OKF knowledge bundles. Not answer quality; "
            "no model, embedding or provider is ever called."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--in-process",
        action="store_true",
        help="Default. Build the estate from the committed fixture; no network, no database.",
    )
    mode.add_argument(
        "--api",
        nargs="?",
        const="",
        metavar="BASE_URL",
        help="Ask a running stack's context route instead (read-only; leaves ordinary read "
        "audit records). BASE_URL defaults to AIDA_BASE_URL, then http://localhost:8000.",
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--check-corpus",
        action="store_true",
        help="Validate the corpus against the fixture and stop.",
    )
    parser.add_argument(
        "--case", action="append", default=[], metavar="ID", help="Only this case (repeatable)."
    )
    parser.add_argument(
        "--max-chars",
        action="append",
        type=int,
        metavar="N",
        help=(
            f"Character budget (repeatable; {MIN_MAX_CHARS}..{MAX_CHARS_LIMIT}). "
            f"Default {DEFAULT_MAX_CHARS}."
        ),
    )
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    parser.add_argument("--output", type=Path, help="Write the report here instead of stdout.")
    api = parser.add_argument_group("--api only")
    api.add_argument(
        "--org",
        default=os.environ.get("AIDA_SEED_SLUG", "sample-bank"),
        help="Organization slug, name or id (default AIDA_SEED_SLUG, then sample-bank).",
    )
    api.add_argument(
        "--datasource", help="Data source id, or a name prefix (default 'Customer Master')."
    )
    api.add_argument(
        "--product-version",
        help="Ask this context product version's bundle instead of a data source's.",
    )
    api.add_argument(
        "--roles",
        default="Analyst",
        help="X-Roles to send (default Analyst, the least role the OKF routes accept).",
    )
    api.add_argument("--principal", default="okf-context-benchmark")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, transport: Transport = http_transport) -> int:
    args = parse_args(argv)
    budgets = args.max_chars or [DEFAULT_MAX_CHARS]
    try:
        corpus = load_corpus(args.corpus)
        if args.check_corpus:
            render_fixture(corpus.estate)
            report = run_in_process(corpus, budgets=budgets[:1], only=args.case)
            print(f"corpus ok: {report.cases_total} cases resolve against the fixture estate.")
            return 0
        if args.api is None:
            report = run_in_process(corpus, budgets=budgets, only=args.case)
        else:
            base = (args.api or os.environ.get("AIDA_BASE_URL", "http://localhost:8000")).rstrip(
                "/"
            )
            target = discover_target(
                transport,
                base,
                org=args.org,
                datasource=args.datasource,
                product_version=args.product_version,
                principal=args.principal,
                roles=args.roles,
            )
            report = run_api(corpus, target, transport, base, budgets=budgets, only=args.case)
    except (CorpusError, ApiError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        text = json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n"
    else:
        text = render(report, markdown=args.format == "markdown")
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
