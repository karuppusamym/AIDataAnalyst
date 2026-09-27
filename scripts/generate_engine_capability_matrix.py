#!/usr/bin/env python3
"""Regenerate the engine capability matrix (review 2026-09-16 §5).

Writes `Docs/90-reference/engine-capability-matrix.json` (the raw data, for
programmatic consumers) and `.md` (the published page) from
`aida.engine_capability_matrix.build_engine_capability_matrix`, which derives
every status in them from live code -- the connector registry's definitions,
`ConnectorCapabilities`' own fields, method identity against the `Connector`
base class, the lineage parsers' own dialect map and construct matrix, and a
targeted scan of each adapter's source for the catalog objects a facet would
have to read.

Neither file carries a generation timestamp, so `--check` compares them byte
for byte and a stale document is a failing gate rather than a diff that is
always dirty. `GET /v1/engines/capability-matrix` serves the same data live and
stamps its own response.

Usage:
    AIDA_ENVIRONMENT=development uv run python \
        scripts/generate_engine_capability_matrix.py
    uv run python scripts/generate_engine_capability_matrix.py --check
    uv run python scripts/generate_engine_capability_matrix.py --stdout

Regenerate after any change to a connector's `DEFAULT_CAPABILITIES`, to the
connector registry, to either parser's dialect map, or to a blueprint
generator's eligibility rules, so the published matrix never drifts from what
the adapters actually do.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from aida.engine_capability_matrix import (  # noqa: E402
    build_engine_capability_matrix,
    render_markdown,
)

_JSON_PATH = REPO_ROOT / "Docs" / "90-reference" / "engine-capability-matrix.json"
_MD_PATH = REPO_ROOT / "Docs" / "90-reference" / "engine-capability-matrix.md"


def _payload() -> tuple[str, str]:
    """(json text, markdown text) for the current code."""
    matrix = build_engine_capability_matrix(tests_root=REPO_ROOT / "tests")
    data = {
        "matrix_key": list(matrix.matrix_key),
        "facets": list(matrix.facets),
        "states": list(matrix.states),
        "engines": [
            {**asdict(row), "flags": dict(row.flags), "overridden_methods": list(
                row.overridden_methods
            )}
            for row in matrix.engines
        ],
        "rows": [
            {
                "engine": row.engine,
                "native_object_kind": row.native_object_kind,
                "graph_category": row.graph_category,
                "native_concept": row.native_concept,
                "note": row.note,
                "facets": {cell.facet: asdict(cell) for cell in row.cells},
            }
            for row in matrix.rows
        ],
        "source_mapping": asdict(matrix.source_mapping),
        "dbt_coverage": [asdict(row) for row in matrix.dbt_coverage],
        "parser_degradation_reasons": list(matrix.parser_degradation_reasons),
        "declared_gaps": list(matrix.declared_gaps),
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n", render_markdown(matrix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when a committed file differs from what would be generated.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args(argv)

    json_text, markdown = _payload()

    if args.stdout:
        print(markdown)
        return 0

    if args.check:
        stale: list[str] = []
        for path, content in ((_JSON_PATH, json_text), (_MD_PATH, markdown)):
            if not path.exists():
                print(f"{path.relative_to(REPO_ROOT)} does not exist; run without --check.")
                return 1
            if path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(REPO_ROOT)))
        if stale:
            print(
                "the engine capability matrix is out of date with the code; regenerate it: "
                + ", ".join(stale)
            )
            return 1
        print(f"{_MD_PATH.relative_to(REPO_ROOT)} and its .json are up to date.")
        return 0

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(json_text, encoding="utf-8")
    _MD_PATH.write_text(markdown, encoding="utf-8")
    print(f"Wrote {_JSON_PATH.relative_to(REPO_ROOT)}")
    print(f"Wrote {_MD_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
