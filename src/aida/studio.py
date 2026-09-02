"""Studio authoring environment core logic.

Provides change set management, conflict detection, diff computation,
and impact preview for semantic model objects (metrics, tools, glossary
terms, context products).

Change sets follow the lifecycle: DRAFT -> TESTING -> SUBMITTED -> MERGED/REJECTED.
Only tested change sets can be submitted for governance review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from aida.schemas import ContextProductDefinition, ToolParameterDefinition
from aida.tool_rendering import ToolParameterError, render_tool_sql, template_placeholders

ObjectType = Literal["METRIC", "TOOL", "TERM", "CONTEXT_PRODUCT"]
Operation = Literal["CREATE", "UPDATE", "DELETE"]
ChangeSetStatus = Literal["DRAFT", "TESTING", "SUBMITTED", "MERGED", "REJECTED"]
ConflictStatus = Literal["CLEAN", "CONFLICTED", "RESOLVED"]


@dataclass
class ChangeItem:
    """One proposed change within a change set."""

    id: UUID
    object_type: ObjectType
    object_id: str
    operation: Operation
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    test_status: str = "UNTESTED"


@dataclass
class ChangeSet:
    """A named collection of proposed changes to governed objects."""

    id: UUID
    name: str
    author: str
    base_version: str
    items: list[ChangeItem] = field(default_factory=list)
    status: ChangeSetStatus = "DRAFT"
    conflict_status: ConflictStatus = "CLEAN"


@dataclass
class Conflict:
    """A detected conflict between a change set item and published state."""

    object_type: str
    object_id: str
    field_name: str
    change_set_value: Any
    current_value: Any


@dataclass
class Diff:
    """Structured diff between two snapshots."""

    entries: list[DiffEntry] = field(default_factory=list)


@dataclass
class DiffEntry:
    """One field-level difference."""

    field: str
    before: Any
    after: Any


@dataclass
class ImpactPreview:
    """Preview of what would change if a change set merges."""

    change_set_id: UUID
    affected_object_count: int = 0
    affected_objects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TestFixture:
    """Synthetic test data matching the schema of a governed object."""

    object_type: str
    object_id: str
    synthetic_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult:
    """Result of testing one change item against fixtures."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _compute_version_hash(items: list[dict[str, Any]]) -> str:
    """Compute a deterministic hash over a set of published objects."""
    canonical = json.dumps(sorted(items, key=lambda x: str(x)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_change_set(name: str, author: str) -> ChangeSet:
    """Create a new change set in DRAFT status."""
    return ChangeSet(
        id=uuid4(),
        name=name,
        author=author,
        base_version=_compute_version_hash([]),
        status="DRAFT",
        conflict_status="CLEAN",
    )


def add_item(change_set: ChangeSet, item: ChangeItem) -> ChangeSet:
    """Add a change item to a change set.

    Computes the diff if both before and after snapshots are present.
    """
    if change_set.status != "DRAFT":
        raise ValueError("items can only be added to DRAFT change sets")

    if item.before_snapshot and item.after_snapshot:
        item.diff = compute_diff(item.before_snapshot, item.after_snapshot)

    change_set.items.append(item)
    return change_set


def remove_item(change_set: ChangeSet, item_id: UUID) -> ChangeSet:
    """Remove a change item from a change set."""
    if change_set.status != "DRAFT":
        raise ValueError("items can only be removed from DRAFT change sets")

    change_set.items = [i for i in change_set.items if i.id != item_id]
    return change_set


def compute_diff(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compute a structured diff between two snapshots.

    Returns a dict of {field: {"before": old_value, "after": new_value}}
    for all fields that differ between the two snapshots.
    """
    diff: dict[str, Any] = {}
    all_keys = set(before.keys()) | set(after.keys())

    for key in sorted(all_keys):
        before_val = before.get(key)
        after_val = after.get(key)
        if before_val != after_val:
            diff[key] = {"before": before_val, "after": after_val}

    return diff


def detect_conflicts(
    change_set: ChangeSet,
    current_state: dict[str, dict[str, Any]],
) -> list[Conflict]:
    """Detect conflicts between a change set and the current published state.

    `current_state` maps "object_type:object_id" -> current published snapshot.
    Conflicts arise when a field the change set modifies has also changed
    in the published state since the change set was created.
    """
    conflicts: list[Conflict] = []

    for item in change_set.items:
        key = f"{item.object_type}:{item.object_id}"
        current = current_state.get(key)

        if item.operation == "CREATE":
            if current is not None:
                conflicts.append(
                    Conflict(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        field_name="<exists>",
                        change_set_value="CREATE",
                        current_value="ALREADY_EXISTS",
                    )
                )
            continue

        if item.operation == "DELETE":
            if current is None:
                conflicts.append(
                    Conflict(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        field_name="<exists>",
                        change_set_value="DELETE",
                        current_value="ALREADY_DELETED",
                    )
                )
            continue

        # UPDATE: check field-level conflicts
        if current is None:
            conflicts.append(
                Conflict(
                    object_type=item.object_type,
                    object_id=item.object_id,
                    field_name="<exists>",
                    change_set_value="UPDATE",
                    current_value="NOT_FOUND",
                )
            )
            continue

        if item.before_snapshot is None:
            continue

        for field_name, before_val in item.before_snapshot.items():
            current_val = current.get(field_name)
            after_val = (item.after_snapshot or {}).get(field_name)
            # If the change set modifies this field AND the published
            # state has diverged from what the change set based on
            if before_val != after_val and current_val != before_val:
                conflicts.append(
                    Conflict(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        field_name=field_name,
                        change_set_value=after_val,
                        current_value=current_val,
                    )
                )

    return conflicts


@dataclass
class ParameterContractValidation:
    """Result of validating a typed, enum-bound tool parameter contract.

    Reuses ``ToolParameterDefinition`` (the module-14 tool-registry contract) and
    ``tool_rendering``'s placeholder/render machinery directly, so a Studio author
    gets exactly the type, bounds, enum, and sensitive-default checks the tool
    gateway enforces at publish time -- plus a cross-check against the SQL
    template's actual placeholders and a proof-of-render against one
    representative in-bounds value per parameter.
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    definitions: list[dict[str, Any]] = field(default_factory=list)
    sample_rendered_sql: str | None = None


def _synthetic_contract_value(definition: ToolParameterDefinition) -> Any:
    """Build one representative, in-bounds, allowed value for a dry-run render."""
    if definition.allowed_values:
        return definition.allowed_values[0]
    if definition.parameter_type == "STRING":
        value = "sample"
        if definition.max_length is not None:
            value = value[: max(definition.max_length, 1)]
        return value
    if definition.parameter_type == "INTEGER":
        low = int(definition.minimum) if definition.minimum is not None else 0
        high = int(definition.maximum) if definition.maximum is not None else low + 1
        return min(max(0, low), high)
    if definition.parameter_type == "NUMBER":
        num_low = float(definition.minimum) if definition.minimum is not None else 0.0
        num_high = float(definition.maximum) if definition.maximum is not None else num_low + 1.0
        return min(max(0.0, num_low), num_high)
    if definition.parameter_type == "BOOLEAN":
        return True
    if definition.parameter_type == "DATE":
        return "2026-01-01"
    raise ValueError(f"unsupported parameter type: {definition.parameter_type}")


def validate_parameter_contract(
    *,
    sql_template: str,
    raw_definitions: list[dict[str, Any]],
    dialect: str = "postgres",
) -> ParameterContractValidation:
    """Validate a governed tool's typed, enum-bound parameter contract (ST-A4).

    Each raw definition is parsed as a real ``ToolParameterDefinition`` -- not a
    loose dict-shape check -- so invalid types, non-enum values, inverted bounds,
    and sensitive-with-default conflicts are all caught the same way module 14's
    tool gateway catches them. Declared parameter names are then cross-checked
    against the SQL template's actual placeholders (missing/unused), and, once
    the contract is structurally sound, one representative value per parameter is
    substituted through the real renderer to prove the contract actually renders.
    """
    errors: list[str] = []
    definitions: list[ToolParameterDefinition] = []
    seen_names: set[str] = set()

    for index, raw in enumerate(raw_definitions):
        try:
            definition = ToolParameterDefinition.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors():
                field_path = ".".join(str(part) for part in error["loc"]) or "<root>"
                errors.append(
                    f"parameter[{index}].{field_path}: {error['msg']} "
                    f"(got {error.get('input')!r})"
                )
            continue
        if definition.name in seen_names:
            errors.append(f"duplicate parameter name: {definition.name}")
            continue
        seen_names.add(definition.name)
        definitions.append(definition)

    if errors:
        return ParameterContractValidation(valid=False, errors=errors)

    try:
        placeholders = template_placeholders(sql_template, dialect=dialect)
    except Exception as exc:  # sqlglot parse failure on a malformed template
        return ParameterContractValidation(
            valid=False,
            errors=[f"sql_template failed to parse for dialect {dialect!r}: {exc}"],
        )

    declared_names = {definition.name for definition in definitions}
    missing = sorted(placeholders - declared_names)
    unused = sorted(declared_names - placeholders)
    if missing:
        errors.append(f"undeclared placeholders: {', '.join(missing)}")
    if unused:
        errors.append(f"unused parameter definitions: {', '.join(unused)}")

    serialized = [definition.model_dump(mode="json") for definition in definitions]
    if errors:
        return ParameterContractValidation(valid=False, errors=errors, definitions=serialized)

    sample_values = {
        definition.name: _synthetic_contract_value(definition) for definition in definitions
    }
    try:
        rendered = render_tool_sql(
            sql_template, dialect=dialect, definitions=definitions, values=sample_values
        )
    except ToolParameterError as exc:
        return ParameterContractValidation(
            valid=False,
            errors=[f"contract does not render with a representative value set: {exc}"],
            definitions=serialized,
        )

    return ParameterContractValidation(
        valid=True, definitions=serialized, sample_rendered_sql=rendered.sql
    )


@dataclass
class ContextProductContractValidation:
    """Result of validating a Studio CONTEXT_PRODUCT change item's shape (ST-A7).

    Mirrors ``ParameterContractValidation`` (ST-A4): the snapshot is parsed as
    a real ``ContextProductDefinition`` -- module 19's own pydantic contract
    for a context product's editable fields -- instead of a hand-rolled
    dict-shape check, so a Studio author sees exactly the constraints
    ``context_product_api.py`` enforces at submission time. Reference
    existence (tables, semantic model versions, glossary terms, tool
    versions) is deliberately *not* checked here -- that requires an
    organization-scoped DB lookup the pure test harness does not have, and it
    is re-checked for real at materialization time
    (``aida.studio_context_product``, reusing
    ``validate_context_product_references`` unchanged) before anything is
    written, so nothing here is a shortcut around it.
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    definition: dict[str, Any] | None = None
    product_key: str | None = None
    project_id: str | None = None


def _pydantic_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for error in exc.errors():
        field_path = ".".join(str(part) for part in error["loc"]) or "<root>"
        errors.append(f"{field_path}: {error['msg']} (got {error.get('input')!r})")
    return errors


def validate_context_product_contract(
    *,
    operation: Operation,
    object_id: str,
    snapshot: dict[str, Any] | None,
) -> ContextProductContractValidation:
    """Validate a Studio CONTEXT_PRODUCT change item's shape (ST-A7).

    - ``DELETE``: no snapshot is required (matching every other object
      type's DELETE handling in the test harness); ``object_id`` must be the
      UUID of an existing ``ContextProduct`` so materialization can resolve
      what to request deprecation of.
    - ``CREATE``: ``snapshot`` must carry a string ``product_key`` (which
      must equal ``object_id`` -- the same "the item's own key names the
      object" convention every other change-item type follows) and a string
      ``project_id`` UUID (a context product does not exist without a
      project scope, and a Studio change item carries no project field of
      its own), plus every ``ContextProductDefinition`` field.
    - ``UPDATE``: ``object_id`` must be the UUID of an existing
      ``ContextProduct``; ``snapshot`` must carry every
      ``ContextProductDefinition`` field (the new draft version's content).
    """
    if operation == "DELETE":
        errors: list[str] = []
        try:
            UUID(object_id)
        except ValueError:
            errors.append(
                f"object_id must be an existing context product UUID for DELETE: {object_id!r}"
            )
        return ContextProductContractValidation(valid=not errors, errors=errors)

    if snapshot is None:
        return ContextProductContractValidation(
            valid=False,
            errors=["context product definition missing: no after_snapshot provided"],
        )

    if operation == "CREATE":
        payload = dict(snapshot)
        product_key = payload.pop("product_key", None)
        project_id = payload.pop("project_id", None)
        errors = []
        if not isinstance(product_key, str) or not product_key:
            errors.append("CREATE requires a non-empty string product_key in after_snapshot")
        elif product_key != object_id:
            errors.append(
                f"object_id ({object_id!r}) must equal after_snapshot.product_key "
                f"({product_key!r})"
            )
        if not isinstance(project_id, str) or not project_id:
            errors.append("CREATE requires a non-empty string project_id in after_snapshot")
        else:
            try:
                UUID(project_id)
            except ValueError:
                errors.append(f"project_id is not a valid UUID: {project_id!r}")

        try:
            definition = ContextProductDefinition.model_validate(payload)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(exc))
            return ContextProductContractValidation(valid=False, errors=errors)

        if errors:
            return ContextProductContractValidation(valid=False, errors=errors)

        return ContextProductContractValidation(
            valid=True,
            definition=definition.model_dump(mode="json"),
            product_key=product_key,
            project_id=project_id,
        )

    # UPDATE
    errors = []
    try:
        UUID(object_id)
    except ValueError:
        errors.append(
            f"object_id must be an existing context product UUID for UPDATE: {object_id!r}"
        )

    try:
        definition = ContextProductDefinition.model_validate(snapshot)
    except ValidationError as exc:
        errors.extend(_pydantic_errors(exc))
        return ContextProductContractValidation(valid=False, errors=errors)

    if errors:
        return ContextProductContractValidation(valid=False, errors=errors)

    return ContextProductContractValidation(
        valid=True, definition=definition.model_dump(mode="json")
    )


def compute_impact(change_set: ChangeSet) -> ImpactPreview:
    """Compute the impact preview of merging a change set.

    Lists all objects that would be created, updated, or deleted.
    """
    affected: list[dict[str, Any]] = []

    for item in change_set.items:
        entry: dict[str, Any] = {
            "object_type": item.object_type,
            "object_id": item.object_id,
            "operation": item.operation,
        }
        if item.diff:
            entry["changed_fields"] = list(item.diff.keys())
        affected.append(entry)

    return ImpactPreview(
        change_set_id=change_set.id,
        affected_object_count=len(affected),
        affected_objects=affected,
    )
