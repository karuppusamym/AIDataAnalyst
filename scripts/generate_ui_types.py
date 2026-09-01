#!/usr/bin/env python3
"""ui-next OpenAPI type generation gate (tracker UX-14).

`ui-next/src/lib/types.ts` used to be hand-maintained against `schemas.py`
("Kept hand-written and small on purpose: the moment this file drifts, the
right fix is to generate it from the FastAPI OpenAPI document"). This script
is that fix, and it follows the exact idiom `scripts/openapi_diff.py` (TS-4)
and `scripts/perf_baseline.py` (PF-3) already established in this repo:

    1. Generate the current artifact from a live source of truth
       (here: `app.openapi()`'s `components.schemas`, the same call
       `openapi_diff.py` uses).
    2. Compare it against the committed file
       (`ui-next/src/lib/types.ts`).
    3. Exit non-zero on any difference -- a drift between `schemas.py` and
       the committed TypeScript types fails the build instead of surfacing
       as a runtime `undefined` in the browser.
    4. `--accept-baseline` regenerates and writes the file deliberately, the
       explicit, auditable path for evolving the generated types (matching
       `openapi_diff.py`'s and `perf_baseline.py`'s own `--accept-baseline`).

Conversion notes (JSON Schema -> TypeScript), covering every shape actually
present in this API's `components.schemas` (verified by walking all ~360
schemas while writing this):

    - `$ref`                              -> the referenced schema's name
    - `enum` / `const`                    -> a string/number/null literal union
    - `anyOf` / `oneOf`                   -> a `|` union of the converted members
      (this is how pydantic v2 expresses `Optional[X]`: `anyOf: [X, {type: null}]`)
    - `type: array`, `items: <schema>`    -> `<converted items>[]`
    - `type: object` with `properties`    -> an inline `{ ... }` object type
    - `type: object`, `additionalProperties: <schema>` -> `Record<string, <converted>>`
    - `type: object` otherwise            -> `Record<string, unknown>`
    - `type: string/integer/number/boolean` -> `string`/`number`/`number`/`boolean`
      (format such as `uuid` or `date-time` stays `string`: these are JSON
      wire values, not `Date` objects, on both sides of this API)
    - anything unrecognised               -> `unknown` (fails closed: a
      shape this script doesn't understand becomes a type callers must
      narrow, never a silent `any`)

One documented special case: `CursorPage.items` is `list[Any]` in
`schemas.py` by design (see that class's own docstring) -- `response_model=
CursorPage` on every endpoint that returns one is not parameterized, so nothing
in the OpenAPI document itself says what fills a given endpoint's `items`.
Rather than mechanically emit `items: unknown[]` and push a type-erasing cast
onto every caller, this generator keeps `CursorPage` generic (`items: T[]`)
while still rendering every other field (`limit`, `offset`, `total`,
`next_cursor`) mechanically from the live schema -- so a real change to any
of THOSE fields is still caught as drift.

Two names that were on the hand-written `types.ts` are deliberately NOT
produced by this generator, because they are not in `app.openapi()`'s
`components.schemas` at all today: `CatalogRowRead` and `MetadataTableRead`.
Both endpoints that return them (`GET /v1/organizations/{org}/catalog/rows`,
`GET /v1/datasources/{id}/tables`, `src/aida/api.py`) declare
`response_model=CursorPage` un-parameterized, so FastAPI's schema walker
never reaches either model. That is a real, pre-existing gap in the API's
own OpenAPI document -- fixing it means changing a route's `response_model`
in `src/aida/api.py`, which is backend business logic outside UX-14's scope
(ui-next + CI only). Tracked as a follow-up, not silently papered over: see
`ui-next/src/lib/ui-types.ts` for where those two types now live by hand,
with this same note.

Usage:
    # CI gate: compare the current app.openapi() output to the committed
    # ui-next/src/lib/types.ts; exit 1 on drift.
    uv run python scripts/generate_ui_types.py

    # After a deliberate schemas.py change: regenerate types.ts and commit it.
    uv run python scripts/generate_ui_types.py --accept-baseline
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "ui-next" / "src" / "lib" / "types.ts"

# See the module docstring: schemas.py's CursorPage declares `items: list[Any]`
# on purpose, so the live OpenAPI document cannot say what an endpoint's page
# actually holds. This is the one schema this generator keeps parameterized
# rather than rendering item-erased.
_GENERIC_ITEMS_SCHEMAS: dict[str, str] = {"CursorPage": "T = unknown"}

_HEADER = """\
/* ---------------------------------------------------------------------------
   AUTO-GENERATED -- DO NOT EDIT BY HAND.

   Generated from `app.openapi()`'s `components.schemas` by
   scripts/generate_ui_types.py (tracker UX-14). CI
   (`.github/workflows/ci.yml`'s `ui-types-diff` job) fails the build if this
   file drifts from what that script produces against the current
   `src/aida/schemas.py` / `src/aida/platform_schemas.py`.

   To pick up a schema change:
       uv run python scripts/generate_ui_types.py --accept-baseline
   then commit the result. Do not hand-edit -- the next `--accept-baseline`
   run overwrites any manual change silently.

   Two types the rest of ui-next still needs are deliberately NOT here
   because they are not in the live OpenAPI document yet (`CatalogRowRead`,
   `MetadataTableRead` -- see this script's module docstring for why); they
   live by hand in `./ui-types.ts` instead.
--------------------------------------------------------------------------- */
"""


def _load_current_spec() -> dict[str, Any]:
    # Imported lazily, same reasoning as scripts/openapi_diff.py's
    # `_load_current_spec`: `--help` and anything that doesn't need the full
    # app shouldn't have to import it (and its settings/DB dependencies).
    from aida.main import app

    spec = app.openapi()
    # Round-trip through JSON so this generator only ever sees plain
    # str/int/float/bool/list/dict/None -- exactly what's on the wire.
    result: dict[str, Any] = json.loads(json.dumps(spec))
    return result


def _resolve_ref_name(ref: str) -> str:
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        # Every $ref FastAPI/pydantic emit is a local component-schema ref;
        # anything else is a shape this generator doesn't understand.
        return "unknown"
    return ref[len(prefix) :]


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _ts_type(node: Any) -> str:
    """Convert one (possibly $ref'd) JSON Schema node to a TS type expression."""
    if not isinstance(node, dict) or not node:
        return "unknown"

    if "$ref" in node:
        return _resolve_ref_name(node["$ref"])

    if "const" in node:
        value = node["const"]
        return "null" if value is None else json.dumps(value)

    if "enum" in node:
        literals = ["null" if v is None else json.dumps(v) for v in node["enum"]]
        literals = _dedupe(literals)
        return " | ".join(literals) if literals else "unknown"

    for combinator in ("anyOf", "oneOf"):
        members = node.get(combinator)
        if isinstance(members, list) and members:
            parts = _dedupe([_ts_type(m) for m in members])
            return " | ".join(parts) if parts else "unknown"

    schema_type = node.get("type")

    if schema_type == "array":
        item_schema = node.get("items")
        inner = _ts_type(item_schema) if item_schema else "unknown"
        if "|" in inner:
            inner = f"({inner})"
        return f"{inner}[]"

    if schema_type == "object":
        properties = node.get("properties")
        if isinstance(properties, dict) and properties:
            return _inline_object(node)
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            return f"Record<string, {_ts_type(additional)}>"
        return "Record<string, unknown>"

    if schema_type == "string":
        return "string"
    if schema_type in ("integer", "number"):
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"

    return "unknown"


def _inline_object(node: dict[str, Any]) -> str:
    properties: dict[str, Any] = node.get("properties") or {}
    required = set(node.get("required") or [])
    fields = []
    for prop_name, prop_schema in properties.items():
        optional = "" if prop_name in required else "?"
        fields.append(f"{prop_name}{optional}: {_ts_type(prop_schema)}")
    if not fields:
        return "Record<string, unknown>"
    return "{ " + "; ".join(fields) + " }"


def _summary(schema: dict[str, Any]) -> str | None:
    description = schema.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    first_line = description.strip().splitlines()[0].strip()
    return first_line or None


def _render_interface(name: str, schema: dict[str, Any]) -> str:
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    generic = _GENERIC_ITEMS_SCHEMAS.get(name)

    lines: list[str] = []
    summary = _summary(schema)
    if summary:
        lines.append(f"/** {summary} */")

    header = f"export interface {name}"
    if generic:
        header += f"<{generic}>"
    header += " {"
    lines.append(header)

    for prop_name, prop_schema in properties.items():
        optional = "" if prop_name in required else "?"
        if generic and prop_name == "items":
            type_text = "T[]"
        else:
            type_text = _ts_type(prop_schema)
        lines.append(f"  {prop_name}{optional}: {type_text};")

    lines.append("}")
    return "\n".join(lines)


def generate(spec: dict[str, Any]) -> str:
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {}) or {}
    blocks = [_render_interface(name, schemas[name]) for name in sorted(schemas)]
    body = "\n\n".join(blocks)
    return _HEADER + "\n" + body + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Path to the generated types file (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--accept-baseline",
        action="store_true",
        help=(
            "Regenerate the types file from the current app.openapi() output and write it. "
            "Run this deliberately -- after a schemas.py change -- then commit the result. "
            "This is the explicit, auditable path for evolving the generated types, matching "
            "scripts/openapi_diff.py's and scripts/perf_baseline.py's own --accept-baseline."
        ),
    )
    args = parser.parse_args(argv)

    spec = _load_current_spec()
    current = generate(spec)

    if args.accept_baseline:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(current)
        schema_count = len(spec.get("components", {}).get("schemas", {}) or {})
        print(f"{args.output} regenerated ({schema_count} schemas).")
        print("Review `git diff` for that file before committing it.")
        return 0

    if not args.output.exists():
        print(f"::error::No generated types file found at {args.output}.")
        print("Run `uv run python scripts/generate_ui_types.py --accept-baseline` to create one.")
        return 1

    committed = args.output.read_text()

    if committed == current:
        schema_count = len(spec.get("components", {}).get("schemas", {}) or {})
        print(f"{args.output} matches the current OpenAPI schema ({schema_count} schemas).")
        return 0

    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile=str(args.output),
        tofile="generated from current app.openapi()",
    )
    sys.stdout.writelines(diff)
    print(
        f"\n::error::{args.output} is stale against the current OpenAPI schema.\n"
        "Run `uv run python scripts/generate_ui_types.py --accept-baseline`, review the diff, "
        "and commit the regenerated file."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
