from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aida.main import app
from aida.models import ToolCertificationCase, ToolCertificationRun
from aida.schemas import (
    ToolCertificationDecisionRequest,
    ToolCertificationExpectation,
    ToolCertificationRunCreate,
    ToolParameterDefinition,
)
from aida.tool_certification import (
    certification_is_active,
    corpus_fingerprint,
    current_certification,
    evaluate_certification_case,
    run_certification_corpus,
)

SQL_TEMPLATE = (
    "SELECT customer_id FROM retail.customer "
    "WHERE state_code = :region AND customer_id >= :minimum_id"
)


def _definitions() -> list[ToolParameterDefinition]:
    return [
        ToolParameterDefinition(
            name="region",
            parameter_type="STRING",
            allowed_values=["NY", "TX"],
        ),
        ToolParameterDefinition(
            name="minimum_id",
            parameter_type="INTEGER",
            minimum=1,
            default=1,
        ),
    ]


def _case(
    case_key: str,
    *,
    parameters: dict[str, object],
    expect: str,
    sql_contains: list[str] | None = None,
    error_contains: str | None = None,
    tool_id: object | None = None,
) -> ToolCertificationCase:
    return ToolCertificationCase(
        organization_id=uuid4(),
        tool_id=tool_id or uuid4(),
        case_key=case_key,
        description=f"corpus case {case_key}",
        parameters=parameters,
        expectation={
            "expect": expect,
            "sql_contains": sql_contains or [],
            "error_contains": error_contains,
        },
        created_by="tool-developer@bank.example",
    )


# ---------------------------------------------------------------------------
# Corpus execution produces real pass/fail evidence (not a rubber stamp).
# ---------------------------------------------------------------------------


def test_accept_case_passes_when_rendered_sql_matches_expected_fragments() -> None:
    case = _case(
        "ny_accepts",
        parameters={"region": "NY"},
        expect="ACCEPT",
        sql_contains=["'NY'"],
    )
    result = evaluate_certification_case(
        case, sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert result == {
        "case_key": "ny_accepts",
        "status": "PASS",
        "evidence": "rendered and matched expected SQL fragments",
        "evaluated_at": result["evaluated_at"],
    }


def test_accept_case_fails_when_expected_sql_fragment_is_missing() -> None:
    case = _case(
        "wrong_expectation",
        parameters={"region": "TX"},
        expect="ACCEPT",
        sql_contains=["this fragment never renders"],
    )
    result = evaluate_certification_case(
        case, sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert result["status"] == "FAIL"
    assert "missing expected fragments" in result["evidence"]


def test_reject_case_passes_when_parameter_contract_rejects_injection_attempt() -> None:
    case = _case(
        "rejects_injection",
        parameters={"region": "NY' OR TRUE --"},
        expect="REJECT",
        error_contains="not allowed",
    )
    result = evaluate_certification_case(
        case, sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert result["status"] == "PASS"
    assert "rejected as expected" in result["evidence"]


def test_reject_case_fails_when_parameters_are_unexpectedly_accepted() -> None:
    case = _case(
        "should_have_been_rejected",
        parameters={"region": "NY"},
        expect="REJECT",
    )
    result = evaluate_certification_case(
        case, sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert result["status"] == "FAIL"
    assert "expected rejection" in result["evidence"]


def test_corpus_run_is_pending_review_only_when_every_case_passes() -> None:
    passing_cases = [
        _case("ny_accepts", parameters={"region": "NY"}, expect="ACCEPT", sql_contains=["'NY'"]),
        _case(
            "rejects_injection",
            parameters={"region": "NY' OR TRUE --"},
            expect="REJECT",
            error_contains="not allowed",
        ),
    ]
    status, score, passed, total, results = run_certification_corpus(
        passing_cases, sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert (status, score, passed, total) == ("PENDING_REVIEW", 100, 2, 2)
    assert {result["case_key"] for result in results} == {"ny_accepts", "rejects_injection"}


def test_corpus_run_fails_certification_when_any_case_fails() -> None:
    cases = [
        _case("ny_accepts", parameters={"region": "NY"}, expect="ACCEPT", sql_contains=["'NY'"]),
        _case(
            "wrong_expectation",
            parameters={"region": "TX"},
            expect="ACCEPT",
            sql_contains=["absent fragment"],
        ),
    ]
    status, score, passed, total, _results = run_certification_corpus(
        cases, sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert status == "CERTIFICATION_FAILED"
    assert (passed, total, score) == (1, 2, 50)


def test_corpus_run_with_no_cases_cannot_be_certified() -> None:
    status, score, passed, total, results = run_certification_corpus(
        [], sql_template=SQL_TEMPLATE, dialect="postgres", definitions=_definitions()
    )
    assert (status, score, passed, total, results) == ("CERTIFICATION_FAILED", 0, 0, 0, [])


def test_corpus_fingerprint_is_deterministic_and_order_independent() -> None:
    tool_id = uuid4()
    case_a = _case(
        "a_case",
        parameters={"region": "NY"},
        expect="ACCEPT",
        sql_contains=["'NY'"],
        tool_id=tool_id,
    )
    case_b = _case(
        "b_case",
        parameters={"region": "NY' OR TRUE --"},
        expect="REJECT",
        error_contains="not allowed",
        tool_id=tool_id,
    )
    assert corpus_fingerprint([case_a, case_b]) == corpus_fingerprint([case_b, case_a])

    mutated = _case(
        "a_case",
        parameters={"region": "TX"},
        expect="ACCEPT",
        sql_contains=["'TX'"],
        tool_id=tool_id,
    )
    assert corpus_fingerprint([mutated, case_b]) != corpus_fingerprint([case_a, case_b])


# ---------------------------------------------------------------------------
# Expiry causes a certified tool version to read back as uncertified, and
# recertification (a fresh run) preserves every prior run as history.
# ---------------------------------------------------------------------------


def _certified_run(
    *, expires_at: datetime, status: str = "CERTIFIED", issued_at: datetime | None = None
) -> ToolCertificationRun:
    tool_id = uuid4()
    version_id = uuid4()
    return ToolCertificationRun(
        organization_id=uuid4(),
        tool_id=tool_id,
        tool_version_id=version_id,
        suite_version="tool-certification-corpus-v1",
        corpus_fingerprint="fingerprint",
        status=status,
        total_cases=2,
        passed_cases=2,
        score=100,
        results=[],
        rationale="Certified against the approved corpus.",
        executed_by="tool-developer@bank.example",
        certified_by="reviewer@bank.example",
        issued_at=issued_at or (expires_at - timedelta(days=90)),
        expires_at=expires_at,
    )


def test_active_certification_counts_as_certified_before_expiry() -> None:
    run = _certified_run(expires_at=datetime.now(UTC) + timedelta(days=1))
    assert certification_is_active(run) is True


def test_expired_certification_stops_counting_without_mutating_the_row() -> None:
    run = _certified_run(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert certification_is_active(run) is False
    # The evidence row itself is untouched -- status still literally says
    # CERTIFIED; only the query-time projection treats it as inactive.
    assert run.status == "CERTIFIED"


def test_rejected_run_never_counts_as_certified_regardless_of_expiry() -> None:
    run = _certified_run(expires_at=datetime.now(UTC) + timedelta(days=365), status="REJECTED")
    assert certification_is_active(run) is False


def test_recertification_supersedes_expired_run_while_preserving_history() -> None:
    expired = _certified_run(expires_at=datetime.now(UTC) - timedelta(days=1))
    recertified = _certified_run(expires_at=datetime.now(UTC) + timedelta(days=180))
    history = [recertified, expired]  # newest first, mirrors issued_at desc ordering

    current = current_certification(history)

    assert current is recertified
    # The expired run is still present in history -- recertification is a new
    # row, not an overwrite.
    assert expired in history
    assert expired.status == "CERTIFIED"


def test_current_certification_is_none_when_only_expired_runs_exist() -> None:
    expired = _certified_run(expires_at=datetime.now(UTC) - timedelta(days=1))
    assert current_certification([expired]) is None


# ---------------------------------------------------------------------------
# Schema contracts: maker-checker shape, expectation validation, API surface.
# ---------------------------------------------------------------------------


def test_tool_certification_api_contract_is_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/tools/{tool_id}/certification-cases",
        "/v1/tool-versions/{version_id}/certification-runs",
        "/v1/tool-certification-runs/{run_id}/decision",
        "/v1/tools/{tool_id}/certification-runs",
        "/v1/tools/{tool_id}/certification-status",
    }
    assert expected <= paths.keys()


def test_certification_run_create_requires_an_expiry() -> None:
    with pytest.raises(ValidationError):
        ToolCertificationRunCreate(rationale="Certified against the approved corpus.")
    run = ToolCertificationRunCreate(
        rationale="Certified against the approved corpus.",
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    assert run.expires_at.tzinfo is not None


def test_certification_decision_requires_a_reason_when_rejecting() -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        ToolCertificationDecisionRequest(decision="REJECT")
    decision = ToolCertificationDecisionRequest(decision="REJECT", reason="Corpus coverage weak.")
    assert decision.reason == "Corpus coverage weak."
    approve = ToolCertificationDecisionRequest(decision="APPROVE")
    assert approve.reason is None


def test_certification_expectation_rejects_incoherent_shapes() -> None:
    with pytest.raises(ValidationError, match="sql_contains only applies"):
        ToolCertificationExpectation(expect="REJECT", sql_contains=["should not be here"])
    with pytest.raises(ValidationError, match="error_contains only applies"):
        ToolCertificationExpectation(expect="ACCEPT", error_contains="should not be here")
    accept = ToolCertificationExpectation(expect="ACCEPT", sql_contains=["'NY'"])
    assert accept.expect == "ACCEPT"
