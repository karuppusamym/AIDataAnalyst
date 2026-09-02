#!/usr/bin/env python3
"""AT-22: regenerate the procedure lineage parser capability matrix.

Writes `Docs/90-reference/procedure-lineage-capability-matrix.json` (the raw
data, for programmatic consumers) and `.md` (the published, human-readable
page) from `aida.procedure_capability_matrix.build_capability_matrix`, which
derives every fact in it directly from `sql_lineage_parser.py`'s and
`procedure_lineage.py`'s own dispatch code -- see that module's docstring.

Usage:
    AIDA_ENVIRONMENT=development uv run python \
        scripts/generate_procedure_capability_matrix.py

Regenerate this after any change to either parser's dispatch logic (a new
statement shape handled, a new `UnparsedReason`, a new control-flow regex)
so the published matrix never drifts from what the code actually does --
exactly the "marketed vs actual" drift AT-22 exists to avoid.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from aida.procedure_capability_matrix import build_capability_matrix, render_markdown  # noqa: E402

_JSON_PATH = REPO_ROOT / "Docs" / "90-reference" / "procedure-lineage-capability-matrix.json"
_MD_PATH = REPO_ROOT / "Docs" / "90-reference" / "procedure-lineage-capability-matrix.md"


def main() -> int:
    matrix = build_capability_matrix()

    payload = {
        "generated_at": matrix.generated_at,
        "dialects": list(matrix.dialects),
        "constructs": [asdict(row) for row in matrix.constructs],
        "unparsed_reasons": list(matrix.unparsed_reasons),
    }
    _JSON_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _MD_PATH.write_text(render_markdown(matrix))

    print(f"Wrote {_JSON_PATH.relative_to(REPO_ROOT)}")
    print(f"Wrote {_MD_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
