import hashlib
import json
from dataclasses import dataclass
from typing import Any

import yaml

from aida.models import ContextProduct, ContextProductVersion
from aida.platform_schemas import (
    ContextCompilationRead,
    ContextCompilationValidationRead,
    ContextCompilerTarget,
)


@dataclass(frozen=True, slots=True)
class ResolvedTableReference:
    table_id: str
    qualified_name: str


@dataclass(frozen=True, slots=True)
class ResolvedRoutineReference:
    """R11-FP12: a routine the product names, with what it stands on -- pre-serialized.

    `reads_table_ids`/`writes_table_ids` come from ACTIVE, non-intermediate procedure-lineage
    edges and are cut to the product's own `table_ids`: a routine that also reads a table the
    product does not cover says nothing about that table, not even that it exists.
    `definition_digest` is a SHA-256 of the stored value-free text, so a consumer can tell the
    definition changed without Atlas publishing a digest of the literal-bearing original.

    R11-FP08 added `description` and `description_state`. Until then a routine in a context
    product shipped a digest and no *meaning*: a reader was told that a procedure exists, what
    its signature is and which of the product's tables it touches, but nothing about what it is
    for -- while the tables beside it carried their approved descriptions. `description` is the
    approved `RoutineDocumentationVersion` text and nothing else: a pending draft is not what
    the platform asserts, and a source comment is the source speaking, so both resolve to
    `None` here with `description_state` saying which. Never the body, under any state.
    """

    routine_id: str
    qualified_name: str
    routine_type: str
    signature: str
    status: str
    definition_available: bool
    lineage: str
    fully_parsed: bool
    reads_table_ids: tuple[str, ...]
    writes_table_ids: tuple[str, ...]
    definition_digest: str | None
    #: The approved Atlas-authored description, or `None`. R11-FP08.
    description: str | None = None
    #: `APPROVED`, `PROPOSED` (a draft awaits review), `WITHDRAWN` (one was approved and
    #: retired) or `NONE`. Said rather than left to be inferred from a null `description`,
    #: because "nobody has described this yet" and "a reviewer retired the description" are
    #: different facts and a consumer acts differently on them.
    description_state: str = "NONE"


@dataclass(frozen=True, slots=True)
class ResolvedViewCoverage:
    """R11-FP12: how much Atlas holds about a view (or materialized view) in the product's
    table scope. Same serialization discipline as `ResolvedRoutineReference`."""

    table_id: str
    object_type: str
    status: str
    definition_available: bool
    truncated: bool
    lineage: str
    definition_digest: str | None


@dataclass(frozen=True, slots=True)
class ResolvedOntologyMeaning:
    """R11-FP09: the approved ontology meaning a product is pinned to, from that version itself.

    Read from the bound version and never from the ontology's head, so a later publication
    cannot change what a product says. Mappings are cut to the product's own tables, columns
    and routines; a concept with none left in scope still says what the word means. Deprecated
    concepts and relations are left out. Pre-serialized, like the other resolved references:
    free text egress screening withheld arrives as `None`, with the verdict in `withheld`.
    """

    version_id: str
    ontology_key: str
    version: int
    name: str | None
    lifecycle: str
    concepts: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, Any], ...] = ()
    withheld: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedSourceFreshness:
    """R11-FP12: when the source behind part of a product's table scope was last read.

    Coverage says what Atlas holds about a view or a routine and, by digest, whether it has
    changed -- but a digest reads the same whether the scan that took it ran this morning or
    last quarter. This is the time side of that question. It is per datasource because a
    discovery run's reach is a datasource: `last_scan_completed_at` is the last run of either
    mode that completed, `last_full_scan_completed_at` the last FULL one. Both modes read the
    whole catalog; only a FULL run reconciles what it did not see, so an object dropped at the
    source stays ACTIVE here until a FULL run completes. `None` means no such run has ever
    completed -- the state in which the orchestrator refuses to answer at all
    (`NO_COMPLETED_METADATA_ANALYSIS`).

    Pre-serialized like every resolved reference above: ISO-8601 strings, never a `datetime`.
    """

    datasource_id: str
    table_ids: tuple[str, ...]
    last_scan_completed_at: str | None
    last_full_scan_completed_at: str | None


@dataclass(frozen=True, slots=True)
class ResolvedExemplar:
    """A promoted exemplar (N17), pre-scoped and pre-serialized by the caller.

    Mirrors `ResolvedNegativeAssertion` exactly: `compile_context_product`
    stays a pure function of its arguments (no DB access, no clock reads),
    so every field here is already a JSON primitive -- no `AgentRun` object,
    no live-derived value -- keeping the artifact hash reproducible. Built
    from `aida.exemplar_store.ExemplarCase` by
    `context_compiler_api._load_exemplars`, the same "resolve, then
    pre-serialize" split `_load_negative_knowledge` uses for negative
    knowledge.
    """

    case_id: str
    source: str
    resolved_object_types: tuple[str, ...]
    selected_tool_slug: str | None
    semantic_version_kind: str
    policy_status: str
    policy_reason_code: str | None
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class ResolvedNegativeAssertion:
    """A negative-knowledge record, pre-scoped and pre-serialized by the caller.

    `compile_context_product` stays a pure function of its arguments (no DB
    access, no clock reads) -- the same discipline `tables` already follows --
    so `rejected_at` arrives as an ISO-8601 string rather than a `datetime`,
    keeping every field JSON-primitive and the artifact hash reproducible.
    """

    subject_id: str
    assertion_type: str
    predicate: dict[str, Any]
    rejected_by: str
    rejected_at: str
    suppression_active: bool
    lift_reason: str | None = None


# Targets whose payload carries an Atlas-native `context`/`spec` envelope
# (built from `common`) rather than conforming to an external vendor
# schema. The negative-knowledge and exemplar sections are Atlas-specific
# content -- Snowflake Semantic Views, Databricks Metric Views, OSI, and ODCS
# have no field for "what we decided is not true" or "a confirmed-correct
# context path to imitate", and validating those artifacts against their
# vendor spec would reject an unrecognized extra key -- so only these targets
# carry either.
_NEGATIVE_KNOWLEDGE_TARGETS = frozenset({"MCP", "REST", "YAML"})
#: Deliberately the same set as `_NEGATIVE_KNOWLEDGE_TARGETS` (see above) --
#: kept as its own name so a future target-selection divergence between the
#: two sections stays a one-line, easy-to-review change rather than a shared
#: constant silently governing both.
_EXEMPLAR_TARGETS = frozenset({"MCP", "REST", "YAML"})


def _negative_knowledge_section(assertions: list[ResolvedNegativeAssertion]) -> dict[str, Any]:
    items = [
        {
            "subject_id": assertion.subject_id,
            "assertion_type": assertion.assertion_type,
            "predicate": assertion.predicate,
            "rejected_by": assertion.rejected_by,
            "rejected_at": assertion.rejected_at,
            "suppression_active": assertion.suppression_active,
            "lift_reason": assertion.lift_reason,
        }
        for assertion in assertions
    ]
    items.sort(
        key=lambda item: (
            str(item["subject_id"]),
            str(item["assertion_type"]),
            str(item["rejected_at"]),
        )
    )
    return {"count": len(items), "assertions": items}


def _exemplars_section(exemplars: list[ResolvedExemplar]) -> dict[str, Any]:
    items = [
        {
            "case_id": exemplar.case_id,
            "source": exemplar.source,
            "resolved_object_types": sorted(exemplar.resolved_object_types),
            "selected_tool_slug": exemplar.selected_tool_slug,
            "semantic_version_kind": exemplar.semantic_version_kind,
            "policy_status": exemplar.policy_status,
            "policy_reason_code": exemplar.policy_reason_code,
            "artifact_hash": exemplar.artifact_hash,
        }
        for exemplar in exemplars
    ]
    items.sort(key=lambda item: str(item["case_id"]))
    return {"count": len(items), "exemplars": items}


def coverage_section(
    routines: list[ResolvedRoutineReference], views: list[ResolvedViewCoverage]
) -> dict[str, Any]:
    """R11-FP12: the routines and views a product covers, and how completely. Public: MCP's
    context-product read renders the same section, so the two doors cannot disagree.

    R11-FP08 added `description`/`description_state` to each routine here rather than only to
    the compiled payload, for exactly that reason: MCP's resource read renders this same
    function's output, so a routine's meaning appears at both doors or at neither.
    """
    return {
        "routines": sorted(
            (
                {
                    "id": routine.routine_id,
                    "qualified_name": routine.qualified_name,
                    "routine_type": routine.routine_type,
                    "signature": routine.signature,
                    "status": routine.status,
                    "definition_available": routine.definition_available,
                    "lineage": routine.lineage,
                    "fully_parsed": routine.fully_parsed,
                    "reads_table_ids": sorted(routine.reads_table_ids),
                    "writes_table_ids": sorted(routine.writes_table_ids),
                    "definition_digest": routine.definition_digest,
                    # R11-FP08: what the routine is *for*, when a reviewer has approved a
                    # statement of it. Never the body.
                    "description": routine.description,
                    "description_state": routine.description_state,
                }
                for routine in routines
            ),
            key=lambda item: str(item["id"]),
        ),
        "views": sorted(
            (
                {
                    "table_id": view.table_id,
                    "object_type": view.object_type,
                    "status": view.status,
                    "definition_available": view.definition_available,
                    "truncated": view.truncated,
                    "lineage": view.lineage,
                    "definition_digest": view.definition_digest,
                }
                for view in views
            ),
            key=lambda item: str(item["table_id"]),
        ),
    }


def ontology_section(meanings: list[ResolvedOntologyMeaning]) -> list[dict[str, Any]]:
    """R11-FP09: the meaning each bound ontology version gives the product. Public: MCP's
    context-product read renders the same section, so the two doors cannot disagree."""
    return sorted(
        (
            {
                "version_id": meaning.version_id,
                "ontology_key": meaning.ontology_key,
                "version": meaning.version,
                "name": meaning.name,
                "lifecycle": meaning.lifecycle,
                "concepts": list(meaning.concepts),
                "relations": list(meaning.relations),
                "withheld": list(meaning.withheld),
            }
            for meaning in meanings
        ),
        key=lambda item: str(item["version_id"]),
    )


def freshness_section(sources: list[ResolvedSourceFreshness]) -> list[dict[str, Any]]:
    """R11-FP12: when each source behind the product was last read. Public: MCP's
    context-product read renders the same section, so the two doors cannot disagree.

    Deliberately *not* part of the compiled artifact. `compile_context_product` is a pure
    function of its arguments precisely so one version compiles to the same bytes every time,
    and a clock reading inside the hashed content would make every completed scan look like
    deployment drift. It rides in `generated_from` instead: beside the artifact, not in it.
    """
    return sorted(
        (
            {
                "datasource_id": source.datasource_id,
                "table_ids": list(source.table_ids),
                "last_scan_completed_at": source.last_scan_completed_at,
                "last_full_scan_completed_at": source.last_full_scan_completed_at,
            }
            for source in sources
        ),
        key=lambda item: str(item["datasource_id"]),
    )


def _canonical_json(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
        indent=2 if pretty else None,
        ensure_ascii=True,
    )


def _artifact_payload(
    product: ContextProduct,
    version: ContextProductVersion,
    target: ContextCompilerTarget,
    tables: list[ResolvedTableReference],
    negative_knowledge: list[ResolvedNegativeAssertion],
    exemplars: list[ResolvedExemplar],
    routines: list[ResolvedRoutineReference],
    views: list[ResolvedViewCoverage],
    ontology: list[ResolvedOntologyMeaning],
) -> dict[str, Any]:
    references: dict[str, Any] = {
        "tables": [
            {"id": table.table_id, "qualified_name": table.qualified_name} for table in tables
        ],
        "semantic_model_version_ids": sorted(version.semantic_model_version_ids),
        "glossary_term_version_ids": sorted(version.glossary_term_version_ids),
        "eligible_tool_version_ids": sorted(version.eligible_tool_version_ids),
    }
    # R11-FP12: present only when there is something to say, so a product that names no
    # routine and holds no view compiles to the byte-identical artifact it always did.
    if routines:
        references["routines"] = sorted(
            (
                {"id": routine.routine_id, "qualified_name": routine.qualified_name}
                for routine in routines
            ),
            key=lambda item: item["id"],
        )
    # R11-FP09: the approved ontology meaning the product is bound to, by pinned version.
    if version.ontology_version_ids:
        references["ontology_version_ids"] = sorted(version.ontology_version_ids)
    common = {
        "product_key": product.product_key,
        "version": version.version,
        "fingerprint": version.fingerprint,
        "name": version.name,
        "description": version.description,
        "purpose": version.purpose,
        "owner": version.owner_principal,
        "owner_type": version.owner_type,
        "references": references,
        "quality_requirements": version.quality_requirements,
        "policy_summary": version.policy_summary,
    }
    # Only the Atlas-native envelope carries negative knowledge or exemplars
    # (see `_NEGATIVE_KNOWLEDGE_TARGETS`/`_EXEMPLAR_TARGETS`); vendor-standard
    # targets below embed `common` unchanged.
    atlas_common = {
        **common,
        "negative_knowledge": _negative_knowledge_section(negative_knowledge),
        "exemplars": _exemplars_section(exemplars),
    }
    if routines or views:
        atlas_common["coverage"] = coverage_section(routines, views)
    # R11-FP09: present only when the product binds an ontology version, as `coverage` is.
    if ontology:
        atlas_common["ontology"] = ontology_section(ontology)
    if target == "MCP":
        return {
            "kind": "AtlasMcpContext",
            "apiVersion": "atlas.aida/v1",
            "resourceUri": (
                f"atlas://context-products/{product.product_key}/versions/{version.version}"
            ),
            "prompt": f"atlas__context__{product.product_key}__v{version.version}",
            "eligibleToolVersionIds": references["eligible_tool_version_ids"],
            "context": atlas_common,
        }
    if target == "REST":
        return {
            "kind": "AtlasRestContext",
            "apiVersion": "atlas.aida/v1",
            "resource": f"/v1/context-product-versions/{version.id}",
            "etag": version.fingerprint,
            "context": atlas_common,
        }
    if target == "OSI":
        return {
            "specification": "OpenSemanticInterchange",
            "specificationVersion": "1.0",
            "semanticContext": common,
        }
    if target == "ODCS":
        return {
            "apiVersion": "v3.1.0",
            "kind": "DataContract",
            "id": product.product_key,
            "version": str(version.version),
            "status": "active",
            "description": {"purpose": version.purpose, "usage": version.description},
            "team": [{"username": version.owner_principal, "role": "Owner"}],
            "servers": [
                {
                    "server": table.qualified_name,
                    "type": "data-platform",
                    "tableId": table.table_id,
                }
                for table in tables
            ],
            "customProperties": [
                {"property": "atlasContextFingerprint", "value": version.fingerprint},
                {"property": "atlasPolicy", "value": version.policy_summary},
            ],
        }
    if target == "SNOWFLAKE_SEMANTIC_VIEW":
        return {
            "kind": "SnowflakeSemanticViewSpec",
            "specVersion": "1",
            "name": product.product_key,
            "comment": version.description,
            "tables": [
                {
                    "logicalName": f"table_{index + 1}",
                    "physicalName": table.qualified_name,
                    "atlasTableId": table.table_id,
                }
                for index, table in enumerate(tables)
            ],
            "semanticModelVersionIds": references["semantic_model_version_ids"],
            "governance": {
                "fingerprint": version.fingerprint,
                "purpose": version.purpose,
                "sourceValues": "GATEWAY_ONLY",
            },
        }
    if target == "DATABRICKS_METRIC_VIEW":
        return {
            "version": "1.1",
            "kind": "DatabricksMetricViewSpec",
            "name": product.product_key,
            "sourceTables": [
                {
                    "name": table.qualified_name,
                    "atlasTableId": table.table_id,
                }
                for table in tables
            ],
            "semanticModelVersionIds": references["semantic_model_version_ids"],
            "comment": version.description,
            "governance": {
                "fingerprint": version.fingerprint,
                "purpose": version.purpose,
                "sourceValues": "GATEWAY_ONLY",
            },
        }
    return {"apiVersion": "atlas.aida/v1", "kind": "ContextProduct", "spec": atlas_common}


def compile_context_product(
    product: ContextProduct,
    version: ContextProductVersion,
    target: ContextCompilerTarget,
    tables: list[ResolvedTableReference],
    negative_knowledge: list[ResolvedNegativeAssertion] | None = None,
    exemplars: list[ResolvedExemplar] | None = None,
    routines: list[ResolvedRoutineReference] | None = None,
    views: list[ResolvedViewCoverage] | None = None,
    ontology: list[ResolvedOntologyMeaning] | None = None,
    sources: list[ResolvedSourceFreshness] | None = None,
) -> ContextCompilationRead:
    """Compile a version-pinned product without time- or environment-dependent fields.

    `negative_knowledge` is pre-scoped by the caller (see
    `aida.negative_knowledge.query_negatives_for_scope`) to assertions
    touching this version's own bounded table scope -- never the whole
    organization's negative-knowledge surface -- and is only rendered into
    targets in `_NEGATIVE_KNOWLEDGE_TARGETS`. Defaults to no negative
    knowledge so existing callers compiling without it keep producing the
    same artifact they always did.

    `exemplars` (N17) mirrors that exact pattern: pre-scoped by the caller
    (see `context_compiler_api._load_exemplars`) to promoted context paths
    whose own resolved objects touch this version's table scope, rendered
    only into targets in `_EXEMPLAR_TARGETS`, and defaulting to none so
    existing callers are unaffected.

    `routines` and `views` (R11-FP12) are pre-resolved by
    `aida.context_product_coverage`. Routine references reach every target that embeds
    `common`; the coverage section -- availability, lineage state, digests -- reaches only the
    Atlas-native targets, like the two sections above. Both default to none.

    `sources` (R11-FP12) is when each source behind the product's tables was last read,
    pre-resolved by `aida.context_product_coverage.load_source_freshness`. It is the one
    argument that never reaches the artifact -- see `freshness_section` for why -- and
    travels in `generated_from` beside it. It defaults to none.

    `ontology` (R11-FP09) is the meaning of each bound ontology version, pre-resolved by
    `aida.context_product_coverage.load_ontology_meaning` from the pinned versions, and
    reaches only the Atlas-native targets. It defaults to none.
    """
    payload = _artifact_payload(
        product,
        version,
        target,
        sorted(tables, key=lambda item: item.table_id),
        negative_knowledge or [],
        exemplars or [],
        routines or [],
        views or [],
        ontology or [],
    )
    content = (
        yaml.safe_dump(payload, sort_keys=True, allow_unicode=False, width=100)
        if target == "YAML"
        else _canonical_json(payload)
    )
    artifact_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ContextCompilationRead(
        target=target,
        content_type="application/yaml" if target == "YAML" else "application/json",
        content=content,
        artifact_hash=artifact_hash,
        source_fingerprint=version.fingerprint,
        generated_from={
            "context_product_id": str(product.id),
            "context_product_version_id": str(version.id),
            "product_key": product.product_key,
            "version": version.version,
            # R11-FP12: beside the artifact, never inside it, so the hash a
            # deployment is compared against does not move when a scan finishes.
            "source_freshness": freshness_section(sources or []),
        },
    )


def validate_compiled_artifact(
    target: ContextCompilerTarget, content: str
) -> ContextCompilationValidationRead:
    """Perform bounded structural conformance checks before external deployment."""
    try:
        parsed = yaml.safe_load(content) if target == "YAML" else json.loads(content)
    except (yaml.YAMLError, json.JSONDecodeError):
        return ContextCompilationValidationRead(
            target=target, valid=False, findings=["CONTENT_PARSE_FAILED"]
        )
    if not isinstance(parsed, dict):
        return ContextCompilationValidationRead(
            target=target, valid=False, findings=["ROOT_OBJECT_REQUIRED"]
        )
    required: dict[str, tuple[str, ...]] = {
        "MCP": ("kind", "apiVersion", "resourceUri", "context"),
        "REST": ("kind", "apiVersion", "resource", "etag", "context"),
        "YAML": ("apiVersion", "kind", "spec"),
        "OSI": ("specification", "specificationVersion", "semanticContext"),
        "ODCS": ("apiVersion", "kind", "id", "version", "servers"),
        "SNOWFLAKE_SEMANTIC_VIEW": ("kind", "specVersion", "name", "tables"),
        "DATABRICKS_METRIC_VIEW": ("version", "kind", "name", "sourceTables"),
    }
    findings = [f"MISSING_REQUIRED_FIELD:{key}" for key in required[target] if key not in parsed]
    if target == "ODCS" and parsed.get("kind") != "DataContract":
        findings.append("ODCS_KIND_INVALID")
    if target == "SNOWFLAKE_SEMANTIC_VIEW" and not isinstance(parsed.get("tables"), list):
        findings.append("SNOWFLAKE_TABLES_INVALID")
    if target == "DATABRICKS_METRIC_VIEW" and not isinstance(
        parsed.get("sourceTables"), list
    ):
        findings.append("DATABRICKS_SOURCE_TABLES_INVALID")
    return ContextCompilationValidationRead(
        target=target, valid=not findings, findings=findings[:100]
    )


def compilation_drift_paths(expected_content: str, deployed_content: str) -> list[str]:
    try:
        expected = yaml.safe_load(expected_content)
        deployed = yaml.safe_load(deployed_content)
    except yaml.YAMLError:
        return [] if expected_content == deployed_content else ["$content"]

    changed: list[str] = []

    def compare(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            changed.append(path)
            return
        if isinstance(left, dict):
            for key in sorted(left.keys() | right.keys()):
                child_path = f"{path}.{key}"
                if key not in left or key not in right:
                    changed.append(child_path)
                else:
                    compare(left[key], right[key], child_path)
            return
        if isinstance(left, list):
            if left != right:
                changed.append(path)
            return
        if left != right:
            changed.append(path)

    compare(expected, deployed, "$")
    return changed[:1000]
