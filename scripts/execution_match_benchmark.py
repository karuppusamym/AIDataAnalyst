#!/usr/bin/env python3
"""Score live governed answers by what they return, not by how they are written.

R11-B2's remaining clause: execution-match scoring of generated SQL against the
live route had never been run. `quality_benchmark.py` measures retrieval and
tool selection offline and deliberately makes no network call, so nothing in
the repository had ever asked the question that matters to a user of Ask --
*is the answer right?*

For each case in `tests/fixtures/quality_benchmark_corpus/execution_match_corpus.json`
this asks the question through Ask on a running deployment, runs the case's
gold SQL through the same governed query gateway, and compares the two result
sets. Both paths go through the same gateway, the same row limit and the same
masking, so a difference is a difference in the answer, not in the plumbing.

What counts as a match, and why:

- **Result sets, not SQL text.** Two different queries that return the same
  rows are both correct, and a text comparison would mark one of them wrong.
- **Order-insensitive**, unless the question asked for an order -- none of
  these cases does.
- **Column names ignored.** A model may alias `count(*)` as `n` or as
  `customer_count`; the value is the answer, the label is not.
- **Numbers normalised** to a rounded decimal string, so `5`, `5.0` and a
  `Decimal('5.00')` compare equal while `5` and `6` do not.

What it will not do, deliberately:

- **It stores no rows.** Result rows are compared in memory and discarded; the
  report holds counts and verdicts only (ADR-0014). A mismatch is reported as a
  row-count and shape difference, never as the values that differed.
- **It is not run by default anywhere.** Every case is a model call that costs
  money, which is exactly why `quality_benchmark.py` refuses to do this on its
  own. Run it deliberately.
- **A refusal is not a wrong answer.** If Ask declines a question (a governed
  refusal, or no model route), that is reported as `NOT_ANSWERED` and kept
  separate from `MISMATCH`, because conflating "did not answer" with "answered
  wrongly" would make the accuracy number mean nothing.

Usage:
    python scripts/execution_match_benchmark.py
    python scripts/execution_match_benchmark.py --org sample-bank \
        --report scratch/execution_match.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.verify_end_to_end import Api, _items  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = (
    REPO_ROOT / "tests" / "fixtures" / "quality_benchmark_corpus" / "execution_match_corpus.json"
)
ASKER = "execution-match-benchmark"
ROLES = "Analyst"
MAX_ROWS = 500


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    verdict: str  # MATCH | MISMATCH | NOT_ANSWERED | GOLD_FAILED
    generation_source: str | None
    generated_rows: int | None
    gold_rows: int | None
    detail: str


def _normalise_cell(value: Any) -> str:
    if value is None:
        return "<null>"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float | Decimal):
        try:
            return str(Decimal(str(value)).quantize(Decimal("0.0001")).normalize())
        except InvalidOperation:
            return str(value)
    text = str(value)
    try:
        return str(Decimal(text).quantize(Decimal("0.0001")).normalize())
    except InvalidOperation:
        return text


def normalise_result(rows: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    """Order-insensitive, alias-insensitive, number-normalised result set.

    Each row becomes the sorted tuple of its normalised values, so neither the
    column order nor the column names matter; the rows are then sorted so the
    row order does not either. A multiset, not a set: a duplicated row is a
    different answer.
    """
    return sorted(tuple(sorted(_normalise_cell(v) for v in row.values())) for row in rows)


def results_match(generated: list[dict[str, Any]], gold: list[dict[str, Any]]) -> bool:
    return normalise_result(generated) == normalise_result(gold)


def score_case(api: Api, org_id: str, ds_id: str, case: dict[str, Any]) -> CaseResult:
    status, gold = api.call(
        "POST",
        f"/v1/datasources/{ds_id}/query-executions",
        body={"sql": case["gold_sql"], "max_rows": MAX_ROWS},
        principal=ASKER,
        roles=ROLES,
        org_id=org_id,
    )
    if status != 200 or not isinstance(gold, dict):
        return CaseResult(case["id"], "GOLD_FAILED", None, None, None, f"gold SQL HTTP {status}")
    gold_rows = list(gold.get("rows") or [])

    status, answer = api.call(
        "POST",
        f"/v1/datasources/{ds_id}/agent-analyses",
        body={"question": case["question"], "max_rows": MAX_ROWS},
        principal=ASKER,
        roles=ROLES,
        org_id=org_id,
        timeout=180,
    )
    if status != 200 or not isinstance(answer, dict):
        # The refusal's own code and the parameter names it asked for are
        # value-free and are what make a NOT_ANSWERED diagnosable: a governed
        # tool asking for an input the question never mentioned is a routing
        # defect, not a model failure.
        reason = f"Ask HTTP {status}"
        refusal = answer.get("detail") if isinstance(answer, dict) else None
        if isinstance(refusal, dict) and refusal.get("code"):
            reason += f" {refusal['code']}"
            if refusal.get("required_parameters"):
                reason += f" requires {refusal['required_parameters']}"
        return CaseResult(case["id"], "NOT_ANSWERED", None, None, len(gold_rows), reason)
    execution = answer.get("execution") or {}
    generated_rows = list(execution.get("rows") or [])
    source = answer.get("generation_source")
    if results_match(generated_rows, gold_rows):
        return CaseResult(case["id"], "MATCH", source, len(generated_rows), len(gold_rows), "")
    shape = (
        f"row count {len(generated_rows)} vs gold {len(gold_rows)}"
        if len(generated_rows) != len(gold_rows)
        else "same row count, different values"
    )
    return CaseResult(
        case["id"], "MISMATCH", source, len(generated_rows), len(gold_rows), shape
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--org", default="sample-bank")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    api = Api(args.base_url)

    status, orgs = api.call("GET", "/v1/organizations?limit=200", roles="PlatformAdmin")
    org = next((o for o in _items(orgs) if o.get("slug") == args.org), None)
    if status != 200 or org is None:
        print(f"organization {args.org!r} not found (HTTP {status}); seed the estate first")
        return 2
    org_id = str(org["id"])
    status, sources = api.call(
        "GET",
        f"/v1/organizations/{org_id}/datasources?limit=50",
        org_id=org_id,
        roles="PlatformAdmin",
    )
    prefix = corpus["datasource_name_prefix"]
    source = next((d for d in _items(sources) if str(d.get("name", "")).startswith(prefix)), None)
    if source is None:
        print(f"no datasource named {prefix!r}...; seed the estate first")
        return 2
    ds_id = str(source["id"])

    print(f"Execution match against {source['name']}")
    results = [score_case(api, org_id, ds_id, case) for case in corpus["cases"]]
    for r in results:
        suffix = f" -- {r.detail}" if r.detail else ""
        print(f"  [{r.verdict:<12}] {r.case_id} (source={r.generation_source}){suffix}")

    answered = [r for r in results if r.verdict in {"MATCH", "MISMATCH"}]
    matched = [r for r in results if r.verdict == "MATCH"]
    not_answered = [r for r in results if r.verdict == "NOT_ANSWERED"]
    gold_failed = [r for r in results if r.verdict == "GOLD_FAILED"]
    accuracy = (len(matched) / len(answered)) if answered else None
    print(
        f"\n{len(matched)} of {len(answered)} answered cases matched"
        + (f" ({accuracy:.0%} execution accuracy)" if accuracy is not None else "")
        + f"; {len(not_answered)} not answered; {len(gold_failed)} gold failures"
    )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "datasource": source["name"],
                    "cases": len(results),
                    "answered": len(answered),
                    "matched": len(matched),
                    "not_answered": len(not_answered),
                    "gold_failed": len(gold_failed),
                    "execution_accuracy": accuracy,
                    # Verdicts and shapes only: no row values leave this process.
                    "results": [asdict(r) for r in results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"report written to {args.report}")
    return 1 if gold_failed else 0


if __name__ == "__main__":
    sys.exit(main())
