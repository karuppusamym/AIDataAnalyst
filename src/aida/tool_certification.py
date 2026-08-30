"""TL-1: tool certification corpus execution (module 14, tool registry).

Mirrors ``aida.ingestion.connector_certification_evidence`` (module 09's
connector "100-point" certification): a corpus of deterministic cases is
executed against the tool's real invocation path and pass/fail evidence is
produced, not a rubber stamp. For a governed tool that real invocation path
is ``aida.tool_rendering.render_tool_sql`` -- the AST literal-binding step
that module 14 owns. Executing the rendered SQL against a warehouse belongs
to module 16 query-gateway (see module 14's "not responsibilities" table),
so certification exercises exactly the boundary module 14 is accountable
for: does this tool version's parameter contract accept what it should and
reject what it should, deterministically, with no dynamic SQL.

Every function here is a pure, DB-free function over plain data so it can be
unit tested the same way ``connector_certification_evidence`` is: construct
inputs directly, assert on outputs, no session or fixtures required.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from aida.schemas import ToolParameterDefinition
from aida.tool_rendering import ToolParameterError, render_tool_sql

CERTIFICATION_SUITE_VERSION = "tool-certification-corpus-v1"


class CertificationCaseLike(Protocol):
    case_key: str
    parameters: dict[str, Any]
    expectation: dict[str, Any]


class CertificationRunLike(Protocol):
    status: str
    expires_at: datetime | None


def corpus_fingerprint(cases: list[CertificationCaseLike]) -> str:
    """Deterministic fingerprint of the exact case set a run was executed against."""
    payload = [
        {
            "case_key": case.case_key,
            "parameters": case.parameters,
            "expectation": case.expectation,
        }
        for case in sorted(cases, key=lambda case: case.case_key)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _case_result(case: CertificationCaseLike, outcome: str, evidence: str) -> dict[str, Any]:
    return {
        "case_key": case.case_key,
        "status": outcome,
        "evidence": evidence,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def evaluate_certification_case(
    case: CertificationCaseLike,
    *,
    sql_template: str,
    dialect: str,
    definitions: list[ToolParameterDefinition],
) -> dict[str, Any]:
    """Run one corpus case through the tool's real parameter-binding path."""
    expect = case.expectation.get("expect", "ACCEPT")
    try:
        rendered = render_tool_sql(
            sql_template,
            dialect=dialect,
            definitions=definitions,
            values=case.parameters,
        )
    except ToolParameterError as exc:
        if expect == "REJECT":
            error_contains = case.expectation.get("error_contains")
            if not error_contains or error_contains in str(exc):
                return _case_result(case, "PASS", f"rejected as expected: {exc}")
            return _case_result(case, "FAIL", f"rejected with unexpected error: {exc}")
        return _case_result(case, "FAIL", f"unexpectedly rejected: {exc}")

    if expect == "REJECT":
        return _case_result(
            case, "FAIL", "expected rejection but parameters were accepted and rendered"
        )
    missing = [
        fragment
        for fragment in case.expectation.get("sql_contains", [])
        if fragment not in rendered.sql
    ]
    if missing:
        return _case_result(
            case,
            "FAIL",
            f"rendered SQL missing expected fragments: {', '.join(missing)}",
        )
    return _case_result(case, "PASS", "rendered and matched expected SQL fragments")


def run_certification_corpus(
    cases: list[CertificationCaseLike],
    *,
    sql_template: str,
    dialect: str,
    definitions: list[ToolParameterDefinition],
) -> tuple[str, int, int, int, list[dict[str, Any]]]:
    """Execute every case in the corpus and roll the evidence up into a status.

    Returns ``(status, score, passed_cases, total_cases, results)``. ``status``
    is ``PENDING_REVIEW`` only when the corpus is non-empty and every case
    passed -- a corpus that fails, or a tool with no corpus defined yet,
    cannot be certified regardless of who signs off (``CERTIFICATION_FAILED``).
    """
    ordered = sorted(cases, key=lambda case: case.case_key)
    results = [
        evaluate_certification_case(
            case, sql_template=sql_template, dialect=dialect, definitions=definitions
        )
        for case in ordered
    ]
    total = len(results)
    passed = sum(1 for result in results if result["status"] == "PASS")
    score = round(passed * 100 / total) if total else 0
    status = "PENDING_REVIEW" if total > 0 and passed == total else "CERTIFICATION_FAILED"
    return status, score, passed, total, results


def certification_is_active(run: CertificationRunLike, *, at: datetime | None = None) -> bool:
    """Whether a certification run currently counts as an active certification.

    A run that was CERTIFIED still reads back that way in storage after its
    ``expires_at`` passes -- the row is audit evidence and is never mutated by
    expiry. This is the query-time projection (mirroring ``AssetCertification``
    in module 08) that makes an expired certification stop counting.
    """
    moment = at or datetime.now(UTC)
    return run.status == "CERTIFIED" and run.expires_at is not None and run.expires_at > moment


def current_certification(
    runs: list[CertificationRunLike], *, at: datetime | None = None
) -> CertificationRunLike | None:
    """The single active certification among a tool version's runs, if any.

    ``runs`` should already be ordered newest-first by ``issued_at``/``created_at``;
    this returns the first one that is still active, so a superseding
    recertification naturally wins without needing to touch older rows.
    """
    for run in runs:
        if certification_is_active(run, at=at):
            return run
    return None
