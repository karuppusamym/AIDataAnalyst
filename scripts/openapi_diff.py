#!/usr/bin/env python3
"""OpenAPI diff gate (tracker TS-4).

CI must fail when a change to the public HTTP API would break an existing
consumer. This script is the enforcement mechanism for that claim:

    1. Generate the current OpenAPI spec from ``app.openapi()``.
    2. Compare it against a committed baseline
       (``Docs/90-reference/openapi-baseline.json``).
    3. Classify every difference as either "breaking" or informational,
       using the standard OpenAPI-compatibility rules:

           BREAKING
             - a path or operation (method) is removed
             - a request parameter or request-body field that used to be
               required is removed, or a field/parameter becomes required
               that wasn't before (old clients that don't send it break)
             - a new *required* request parameter or field is introduced
             - a response field that used to be guaranteed is removed, or
               is no longer guaranteed present (required -> optional)
             - a response status code is removed
             - an enum is narrowed (a previously-valid value is removed)
             - a field's declared type changes

           NOT BREAKING (informational only)
             - a new path or operation is added
             - a new optional request parameter or field is added
             - a new response field, status code, or enum value is added
             - a request field/parameter that used to be required becomes
               optional (widening)

    4. Exit non-zero if any breaking change is found -- UNLESS the API
       version (``info.version``, i.e. ``aida.__version__``) was bumped
       relative to the baseline. A version bump is the explicit,
       auditable acknowledgment that a breaking change is deliberate: it
       shows up in the diff, requires a source change and review, and
       cannot happen by accident. This keeps the gate from being a wall
       that makes the API impossible to evolve.

Usage:
    # CI gate: compare the current app.openapi() output to the committed
    # baseline; exit 1 on an unacknowledged breaking change.
    uv run python scripts/openapi_diff.py

    # After a deliberate, reviewed, version-bumped breaking change:
    # regenerate the baseline from the current spec and commit it.
    uv run python scripts/openapi_diff.py --accept-baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "Docs" / "90-reference" / "openapi-baseline.json"

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

Severity = str  # "breaking" | "info"


@dataclass(frozen=True)
class Change:
    """One classified difference between two OpenAPI specs."""

    severity: Severity
    message: str

    def __str__(self) -> str:
        tag = "BREAKING" if self.severity == "breaking" else "info"
        return f"[{tag}] {self.message}"


def _breaking(message: str) -> Change:
    return Change("breaking", message)


def _info(message: str) -> Change:
    return Change("info", message)


def _resolve(
    schema: Any, spec: dict[str, Any], seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Resolve a local ``#/components/schemas/<Name>`` $ref, if present.

    Only local component-schema refs are supported (the only kind FastAPI /
    pydantic emit). A ``seen`` set guards against a $ref cycle.
    """
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        return {}
    name = ref[len(prefix) :]
    if name in seen:
        return {}
    target = spec.get("components", {}).get("schemas", {}).get(name, {})
    if isinstance(target, dict) and "$ref" in target:
        return _resolve(target, spec, seen | {name})
    return target if isinstance(target, dict) else {}


def _diff_schema(
    old: Any,
    new: Any,
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    location: str,
    *,
    is_request: bool,
    depth: int = 0,
) -> list[Change]:
    """Recursively compare two (possibly $ref'd) JSON schemas.

    ``is_request`` controls which direction of change is breaking: a
    request narrowing (new/newly-required fields) breaks old clients, while
    a response narrowing (removed/no-longer-guaranteed fields) breaks
    consumers reading the response.
    """
    changes: list[Change] = []
    if depth > 16:  # pragma: no cover - cycle/depth guard, not exercised by tests
        return changes

    old = _resolve(old, old_spec)
    new = _resolve(new, new_spec)
    if not old and not new:
        return changes

    old_type = old.get("type")
    new_type = new.get("type")
    if old_type and new_type and old_type != new_type:
        changes.append(_breaking(f"{location}: type changed from '{old_type}' to '{new_type}'"))

    old_enum = old.get("enum")
    new_enum = new.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        removed_values = [v for v in old_enum if v not in new_enum]
        if removed_values:
            changes.append(
                _breaking(f"{location}: enum narrowed, removed values {removed_values!r}")
            )
        added_values = [v for v in new_enum if v not in old_enum]
        if added_values:
            changes.append(_info(f"{location}: enum widened, added values {added_values!r}"))

    old_props = old.get("properties")
    new_props = new.get("properties")
    old_required = set(old.get("required") or [])
    new_required = set(new.get("required") or [])

    if isinstance(old_props, dict) and isinstance(new_props, dict):
        for name, old_prop in old_props.items():
            field_loc = f"{location}.{name}"
            if name not in new_props:
                if is_request:
                    if name in old_required:
                        changes.append(_breaking(f"{field_loc}: removed required request field"))
                    else:
                        changes.append(_info(f"{field_loc}: removed optional request field"))
                else:
                    changes.append(_breaking(f"{field_loc}: removed response field"))
                continue
            changes.extend(
                _diff_schema(
                    old_prop,
                    new_props[name],
                    old_spec,
                    new_spec,
                    field_loc,
                    is_request=is_request,
                    depth=depth + 1,
                )
            )
        for name in new_props:
            if name not in old_props:
                kind = "request" if is_request else "response"
                changes.append(_info(f"{location}.{name}: added new {kind} field"))

    for name in sorted(new_required - old_required):
        if is_request:
            changes.append(_breaking(f"{location}.{name}: field became required"))
        else:
            changes.append(_info(f"{location}.{name}: field is now guaranteed present in response"))

    for name in sorted(old_required - new_required):
        if is_request:
            changes.append(_info(f"{location}.{name}: field is no longer required"))
        else:
            changes.append(
                _breaking(f"{location}.{name}: response no longer guarantees this field")
            )

    return changes


def _diff_parameters(
    old_params: list[dict[str, Any]] | None,
    new_params: list[dict[str, Any]] | None,
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    location: str,
) -> list[Change]:
    changes: list[Change] = []

    def key(param: dict[str, Any]) -> tuple[Any, Any]:
        return (param.get("name"), param.get("in"))

    old_map = {key(p): p for p in old_params or []}
    new_map = {key(p): p for p in new_params or []}

    for param_key, old_param in old_map.items():
        name, where = param_key
        if param_key not in new_map:
            if old_param.get("required"):
                changes.append(
                    _breaking(f"{location}: removed required parameter '{name}' ({where})")
                )
            else:
                changes.append(_info(f"{location}: removed optional parameter '{name}' ({where})"))
            continue
        new_param = new_map[param_key]
        was_required = bool(old_param.get("required"))
        is_required = bool(new_param.get("required"))
        if not was_required and is_required:
            changes.append(_breaking(f"{location}: parameter '{name}' ({where}) became required"))
        elif was_required and not is_required:
            changes.append(_info(f"{location}: parameter '{name}' ({where}) is no longer required"))
        changes.extend(
            _diff_schema(
                old_param.get("schema", {}),
                new_param.get("schema", {}),
                old_spec,
                new_spec,
                f"{location} parameter '{name}' ({where})",
                is_request=True,
            )
        )

    for param_key, new_param in new_map.items():
        if param_key in old_map:
            continue
        name, where = param_key
        if new_param.get("required"):
            changes.append(
                _breaking(f"{location}: added new required parameter '{name}' ({where})")
            )
        else:
            changes.append(_info(f"{location}: added new optional parameter '{name}' ({where})"))

    return changes


def _diff_content(
    old_content: dict[str, Any],
    new_content: dict[str, Any],
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    location: str,
    *,
    is_request: bool,
) -> list[Change]:
    changes: list[Change] = []
    for media_type, old_media in old_content.items():
        if media_type not in new_content:
            changes.append(_breaking(f"{location}: removed content type '{media_type}'"))
            continue
        changes.extend(
            _diff_schema(
                old_media.get("schema", {}),
                new_content[media_type].get("schema", {}),
                old_spec,
                new_spec,
                f"{location} [{media_type}]",
                is_request=is_request,
            )
        )
    for media_type in new_content:
        if media_type not in old_content:
            changes.append(_info(f"{location}: added content type '{media_type}'"))
    return changes


def _diff_operation(
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    location: str,
) -> list[Change]:
    changes: list[Change] = []

    changes.extend(
        _diff_parameters(
            old_op.get("parameters"), new_op.get("parameters"), old_spec, new_spec, location
        )
    )

    old_body = old_op.get("requestBody", {}) or {}
    new_body = new_op.get("requestBody", {}) or {}
    changes.extend(
        _diff_content(
            old_body.get("content", {}) or {},
            new_body.get("content", {}) or {},
            old_spec,
            new_spec,
            f"{location} request body",
            is_request=True,
        )
    )
    if not old_body.get("required") and new_body.get("required"):
        changes.append(_breaking(f"{location}: request body became required"))
    elif old_body.get("required") and not new_body.get("required"):
        changes.append(_info(f"{location}: request body is no longer required"))

    old_responses = old_op.get("responses", {}) or {}
    new_responses = new_op.get("responses", {}) or {}
    for status, old_response in old_responses.items():
        if status not in new_responses:
            changes.append(_breaking(f"{location}: removed response status code {status}"))
            continue
        changes.extend(
            _diff_content(
                old_response.get("content", {}) or {},
                new_responses[status].get("content", {}) or {},
                old_spec,
                new_spec,
                f"{location} {status} response",
                is_request=False,
            )
        )
    for status in new_responses:
        if status not in old_responses:
            changes.append(_info(f"{location}: added response status code {status}"))

    return changes


def diff_specs(old_spec: dict[str, Any], new_spec: dict[str, Any]) -> list[Change]:
    """Classify every difference between two OpenAPI documents.

    ``old_spec`` is the baseline (what consumers were promised);
    ``new_spec`` is the candidate spec being checked.
    """
    changes: list[Change] = []
    old_paths: dict[str, Any] = old_spec.get("paths", {}) or {}
    new_paths: dict[str, Any] = new_spec.get("paths", {}) or {}

    for path, old_item in old_paths.items():
        if path not in new_paths:
            changes.append(_breaking(f"removed path '{path}'"))
            continue
        new_item = new_paths[path]
        for method, old_op in old_item.items():
            if method not in _HTTP_METHODS:
                continue
            location = f"{method.upper()} {path}"
            if method not in new_item:
                changes.append(_breaking(f"removed operation {location}"))
                continue
            changes.extend(_diff_operation(old_op, new_item[method], old_spec, new_spec, location))
        for method in new_item:
            if method in _HTTP_METHODS and method not in old_item:
                changes.append(_info(f"added operation {method.upper()} {path}"))

    for path in new_paths:
        if path not in old_paths:
            changes.append(_info(f"added path '{path}'"))

    return changes


def breaking_changes(changes: list[Change]) -> list[Change]:
    return [c for c in changes if c.severity == "breaking"]


def _load_current_spec() -> dict[str, Any]:
    # Imported lazily so `--help` and unit tests that only exercise
    # `diff_specs` don't need the full application (and its settings/DB
    # imports) to be importable.
    from aida.main import app

    spec = app.openapi()
    # Round-trip through JSON so the result is exactly what gets committed
    # to the baseline file and compared against it (plain str/int/float/
    # bool/list/dict -- no lingering Python-only types).
    result: dict[str, Any] = json.loads(json.dumps(spec))
    return result


def _load_baseline(path: Path) -> dict[str, Any]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"baseline at {path} is not a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"Path to the committed baseline OpenAPI spec (default: {DEFAULT_BASELINE}).",
    )
    parser.add_argument(
        "--accept-baseline",
        action="store_true",
        help=(
            "Regenerate the baseline from the current app.openapi() output and write it. "
            "Run this deliberately -- after reviewing the reported diff -- then commit the "
            "updated baseline file. This is the explicit, auditable path for evolving the API."
        ),
    )
    args = parser.parse_args(argv)

    current = _load_current_spec()

    if args.accept_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Baseline regenerated at {args.baseline}.")
        print("Review `git diff` for that file before committing it.")
        return 0

    if not args.baseline.exists():
        print(f"::error::No OpenAPI baseline found at {args.baseline}.")
        print("Run `uv run python scripts/openapi_diff.py --accept-baseline` to create one.")
        return 1

    baseline = _load_baseline(args.baseline)
    changes = diff_specs(baseline, current)
    breaking = breaking_changes(changes)

    for change in changes:
        print(change)

    if not breaking:
        print(f"\nNo breaking OpenAPI changes detected against {args.baseline}.")
        return 0

    baseline_version = baseline.get("info", {}).get("version")
    current_version = current.get("info", {}).get("version")
    version_bumped = baseline_version is not None and baseline_version != current_version

    print(
        f"\n{len(breaking)} breaking OpenAPI change(s) detected "
        f"against {args.baseline} (API version {baseline_version} -> {current_version})."
    )

    if version_bumped:
        print(
            "The API version was bumped, acknowledging the breaking change(s) above. "
            "Passing the gate -- but you must still regenerate and commit the baseline: "
            "`uv run python scripts/openapi_diff.py --accept-baseline`."
        )
        return 0

    print(
        "::error::Breaking OpenAPI change(s) with no version bump to acknowledge them.\n"
        "If this is deliberate: bump `__version__` in `src/aida/__init__.py`, get the change "
        "reviewed, then run `uv run python scripts/openapi_diff.py --accept-baseline` and "
        "commit the refreshed baseline. If it wasn't deliberate, fix the API instead."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
