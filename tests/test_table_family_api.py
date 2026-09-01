"""RL-1: API-layer coverage for the table-family-candidate maker-checker flow.

`decide_table_family_candidate` (the actual `POST
/table-family-candidates/{id}/decision` handler) is called directly,
following this repo's established convention (see
`tests/test_dbt_run_results_integration.py`) of exercising async endpoint
functions with a hand-built `SecurityContext` rather than spinning up real
HTTP/DB infrastructure, which this repo does not have a fixture for.

`FakeAsyncSession` here is a minimal in-memory double scoped to exactly what
this one endpoint calls: `get`, `add` (used by `record_audit`/
`record_outbox`), and `commit`.
"""

from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from aida.main import app
from aida.models import TableFamilyCandidate
from aida.schemas import TableFamilyCandidateDecision
from aida.security import SecurityContext
from aida.table_family_api import decide_table_family_candidate

ORG_ID = uuid4()


class FakeAsyncSession:
    def __init__(self, seeded: TableFamilyCandidate) -> None:
        self._candidate = seeded
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: type, pk: Any) -> Any:
        assert model is TableFamilyCandidate
        return self._candidate if pk == self._candidate.id else None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


def _context(principal_id: str, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=ORG_ID,
        roles=frozenset(roles or {"DataSteward"}),
    )


def _candidate(
    created_by: str = "maker@example.com", status: str = "PENDING"
) -> TableFamilyCandidate:
    return TableFamilyCandidate(
        id=uuid4(),
        organization_id=ORG_ID,
        datasource_id=uuid4(),
        schema_id=uuid4(),
        family_type="HISTORY",
        member_table_ids=[str(uuid4()), str(uuid4())],
        base_table_id=None,
        detection_rule="HISTORY_SUFFIX_SIBLING_V1",
        confidence=0.9,
        evidence={"matched_suffix": "_history"},
        status=status,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Router registration -- the new endpoints are exposed on the app, not
# folded into api.py/intelligence_api.py.
# ---------------------------------------------------------------------------


def test_table_family_endpoints_are_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/schemas/{schema_id}/table-family-candidates/discover",
        "/v1/schemas/{schema_id}/table-family-candidates",
        "/v1/datasources/{datasource_id}/table-family-candidates",
        "/v1/table-family-candidates/{candidate_id}/decision",
    }
    assert expected <= paths.keys()
    assert "post" in paths["/v1/schemas/{schema_id}/table-family-candidates/discover"]
    assert "get" in paths["/v1/schemas/{schema_id}/table-family-candidates"]
    assert "get" in paths["/v1/datasources/{datasource_id}/table-family-candidates"]
    assert "post" in paths["/v1/table-family-candidates/{candidate_id}/decision"]


# ---------------------------------------------------------------------------
# Maker-checker decision endpoint
# ---------------------------------------------------------------------------


async def test_maker_cannot_review_their_own_candidate() -> None:
    candidate = _candidate(created_by="maker@example.com")
    session = FakeAsyncSession(candidate)
    context = _context("maker@example.com", "DataSteward")
    body = TableFamilyCandidateDecision(decision="APPROVE")

    with pytest.raises(HTTPException) as exc_info:
        await decide_table_family_candidate(candidate.id, body, context, session)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409
    assert "own candidate" in exc_info.value.detail
    assert candidate.status == "PENDING"


async def test_already_decided_candidate_cannot_be_decided_again() -> None:
    candidate = _candidate(created_by="maker@example.com", status="APPROVED")
    session = FakeAsyncSession(candidate)
    context = _context("reviewer@example.com", "DataSteward")
    body = TableFamilyCandidateDecision(decision="APPROVE")

    with pytest.raises(HTTPException) as exc_info:
        await decide_table_family_candidate(candidate.id, body, context, session)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409
    assert "already decided" in exc_info.value.detail


async def test_missing_candidate_returns_404() -> None:
    candidate = _candidate()
    session = FakeAsyncSession(candidate)
    context = _context("reviewer@example.com", "DataSteward")
    body = TableFamilyCandidateDecision(decision="APPROVE")

    with pytest.raises(HTTPException) as exc_info:
        await decide_table_family_candidate(uuid4(), body, context, session)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404


async def test_reviewer_can_approve_a_pending_candidate() -> None:
    candidate = _candidate(created_by="maker@example.com")
    session = FakeAsyncSession(candidate)
    context = _context("reviewer@example.com", "DataSteward")
    body = TableFamilyCandidateDecision(decision="APPROVE")

    result = await decide_table_family_candidate(candidate.id, body, context, session)  # type: ignore[arg-type]

    assert result.status == "APPROVED"
    assert result.reviewed_by == "reviewer@example.com"
    assert result.reviewed_at is not None
    assert session.committed


async def test_reviewer_can_reject_a_pending_candidate_with_reason() -> None:
    candidate = _candidate(created_by="maker@example.com")
    session = FakeAsyncSession(candidate)
    context = _context("reviewer@example.com", "DataSteward")
    body = TableFamilyCandidateDecision(decision="REJECT", reason="not a real family")

    result = await decide_table_family_candidate(candidate.id, body, context, session)  # type: ignore[arg-type]

    assert result.status == "REJECTED"
    assert result.review_reason == "not a real family"


def test_rejecting_without_a_reason_is_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="reason is required"):
        TableFamilyCandidateDecision(decision="REJECT")
