"""API-layer coverage for PR-1 (composite key inference).

Follows this repo's established convention (see
`tests/test_dbt_run_results_integration.py`) of exercising async endpoint
functions directly with a hand-built `SecurityContext`, backed by a small
in-memory `AsyncSession` double rather than real HTTP/DB infrastructure.

`FakeAsyncSession` here is a little more general than that file's -- it
interprets plain AND-of-(==, !=, IN) where-clauses, `ORDER BY`, `LIMIT` and
`OFFSET` generically by table name, since (unlike the dbt endpoint) none of
`composite_key_api`'s queries join across entities.
"""

from __future__ import annotations

import operator as _operator
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.sql import operators as sa_operators

from aida.composite_key_api import (
    decide_composite_key_candidate,
    discover_composite_key_candidates,
    list_composite_key_candidates,
)
from aida.composite_key_inference import MAX_CONFIDENCE
from aida.models import (
    AuditEvent,
    ColumnProfile,
    CompositeKeyCandidate,
    DataSource,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    OutboxEvent,
    TableProfile,
)
from aida.schemas import CompositeKeyCandidateDecision
from aida.security import SecurityContext

# ---------------------------------------------------------------------------
# Minimal, generic in-memory AsyncSession double (no joins needed here)
# ---------------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


def _table_name_of(model: type) -> str:
    return model.__table__.name


def _resolve_model(stmt: Any, registry: dict[str, type]) -> type:
    descriptions = stmt.column_descriptions
    entity_type = descriptions[0].get("type") if descriptions else None
    if entity_type is not None and hasattr(entity_type, "__table__"):
        return entity_type
    for frm in stmt.get_final_froms():
        name = getattr(frm, "name", None)
        if name in registry:
            return registry[name]
    raise AssertionError(f"FakeAsyncSession cannot resolve a model for: {stmt}")


def _extract_filters(whereclause: Any) -> list[tuple[str, str, str, Any]]:
    """Recursively pull (table_name, col_name, op, value) triples out of an AND-only whereclause."""
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None)
    if clauses is not None:
        out: list[tuple[str, str, str, Any]] = []
        for clause in clauses:
            out.extend(_extract_filters(clause))
        return out
    left = getattr(whereclause, "left", None)
    right = getattr(whereclause, "right", None)
    table = getattr(left, "table", None)
    col_name = getattr(left, "key", None) or getattr(left, "name", None)
    op = getattr(whereclause, "operator", None)
    if left is None or right is None or table is None or col_name is None:
        return []
    value = getattr(right, "value", right)
    if op is _operator.eq:
        return [(table.name, col_name, "eq", value)]
    if op is _operator.ne:
        return [(table.name, col_name, "ne", value)]
    if op is sa_operators.in_op:
        return [(table.name, col_name, "in", value)]
    return []


def _matches(obj: Any, table_name: str, filters: list[tuple[str, str, str, Any]]) -> bool:
    for ftable, col, op, value in filters:
        if ftable != table_name:
            continue
        actual = getattr(obj, col)
        if op == "eq" and actual != value:
            return False
        if op == "ne" and actual == value:
            return False
        if op == "in" and actual not in value:
            return False
    return True


def _order_specs(stmt: Any) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for clause in stmt._order_by_clauses:
        modifier = getattr(clause, "modifier", None)
        if modifier is sa_operators.desc_op:
            col_name = clause.element.key
            specs.append((col_name, True))
        else:
            col_name = getattr(clause, "key", None)
            specs.append((col_name, False))
    return specs


class FakeAsyncSession:
    """Minimal in-memory AsyncSession double for `composite_key_api`."""

    def __init__(self) -> None:
        self._store: dict[type, dict[Any, Any]] = defaultdict(dict)
        self._table_registry: dict[str, type] = {}
        self.added: list[Any] = []
        self.committed = False

    def seed(self, obj: Any) -> Any:
        self._assign_id(obj)
        self._register(obj)
        self._store[type(obj)][obj.id] = obj
        return obj

    def added_of(self, cls: type) -> list[Any]:
        return [obj for obj in self.added if isinstance(obj, cls)]

    def _register(self, obj: Any) -> None:
        self._table_registry[_table_name_of(type(obj))] = type(obj)

    @staticmethod
    def _assign_id(obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        # Mimic the ORM's python-side TimestampMixin defaults, which only fire
        # on a real flush -- see the equivalent note in
        # `tests/test_dbt_run_results_integration.py`.
        now = datetime.now(UTC)
        if hasattr(obj, "created_at") and obj.created_at is None:
            obj.created_at = now
        if hasattr(obj, "updated_at") and obj.updated_at is None:
            obj.updated_at = now

    def add(self, obj: Any) -> None:
        self._assign_id(obj)
        self._register(obj)
        self._store[type(obj)][obj.id] = obj
        self.added.append(obj)

    async def get(self, model: type, pk: Any) -> Any | None:
        return self._store.get(model, {}).get(pk)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:
        return None

    def _rows_for(self, stmt: Any) -> list[Any]:
        model = _resolve_model(stmt, self._table_registry)
        table_name = _table_name_of(model)
        filters = _extract_filters(stmt.whereclause)
        rows = [
            obj for obj in self._store.get(model, {}).values() if _matches(obj, table_name, filters)
        ]
        for col_name, descending in reversed(_order_specs(stmt)):
            rows.sort(key=lambda r: getattr(r, col_name), reverse=descending)
        offset = getattr(stmt, "_offset", None)
        limit = getattr(stmt, "_limit", None)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def scalar(self, stmt: Any) -> Any | None:
        # Only `select(func.count()).select_from(Model).where(...)` is used here.
        return len(self._rows_for(stmt))

    async def scalars(self, stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(self._rows_for(stmt))

    async def execute(self, stmt: Any) -> Any:  # pragma: no cover - unused by this endpoint set
        raise AssertionError("composite_key_api does not issue multi-entity execute() queries")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLED_ROWS = 1000


class _Scenario:
    def __init__(self) -> None:
        self.organization_id = uuid4()
        self.session = FakeAsyncSession()

        self.datasource = self.session.seed(
            DataSource(organization_id=self.organization_id, status="ACTIVE")
        )
        self.table = self.session.seed(
            MetadataTable(
                organization_id=self.organization_id,
                datasource_id=self.datasource.id,
                name="customers",
                object_type="TABLE",
                status="ACTIVE",
            )
        )
        self.region = self.session.seed(
            MetadataColumn(
                organization_id=self.organization_id,
                table_id=self.table.id,
                name="region_code",
                ordinal_position=1,
                physical_type="varchar",
                nullable=True,
                status="ACTIVE",
            )
        )
        self.sequence = self.session.seed(
            MetadataColumn(
                organization_id=self.organization_id,
                table_id=self.table.id,
                name="sequence_no",
                ordinal_position=2,
                physical_type="int",
                nullable=False,
                status="ACTIVE",
            )
        )
        self.noise = self.session.seed(
            MetadataColumn(
                organization_id=self.organization_id,
                table_id=self.table.id,
                name="status_flag",
                ordinal_position=3,
                physical_type="varchar",
                nullable=False,
                status="ACTIVE",
            )
        )
        self.profile = self.session.seed(
            TableProfile(
                organization_id=self.organization_id,
                datasource_id=self.datasource.id,
                table_id=self.table.id,
                sampled_row_count=SAMPLED_ROWS,
                row_count_estimate=SAMPLED_ROWS * 5,
                status="COMPLETED",
                created_at=datetime.now(UTC),
            )
        )
        self.session.seed(
            ColumnProfile(
                organization_id=self.organization_id,
                table_profile_id=self.profile.id,
                column_id=self.region.id,
                null_count=0,
                non_null_count=SAMPLED_ROWS,
                approximate_distinct_count=950,
            )
        )
        self.session.seed(
            ColumnProfile(
                organization_id=self.organization_id,
                table_profile_id=self.profile.id,
                column_id=self.sequence.id,
                null_count=0,
                non_null_count=SAMPLED_ROWS,
                approximate_distinct_count=1000,
            )
        )
        self.session.seed(
            ColumnProfile(
                organization_id=self.organization_id,
                table_profile_id=self.profile.id,
                column_id=self.noise.id,
                null_count=0,
                non_null_count=SAMPLED_ROWS,
                approximate_distinct_count=2,
            )
        )

        self.maker = SecurityContext(
            principal_id="maker@bank.internal",
            principal_type="USER",
            roles=frozenset({"MetadataAdmin"}),
            organization_id=self.organization_id,
        )
        self.reviewer = SecurityContext(
            principal_id="reviewer@bank.internal",
            principal_type="USER",
            roles=frozenset({"DataSteward"}),
            organization_id=self.organization_id,
        )

    async def discover(self) -> Any:
        return await discover_composite_key_candidates(
            self.table.id, context=self.maker, session=self.session
        )


# ---------------------------------------------------------------------------
# Discovery + persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_persists_a_pending_composite_key_candidate() -> None:
    scenario = _Scenario()

    page = await scenario.discover()

    created = scenario.session.added_of(CompositeKeyCandidate)
    assert created, "expected at least one persisted candidate"
    pair = next(c for c in created if len(c.column_ids) == 2)
    assert set(pair.column_ids) == {str(scenario.region.id), str(scenario.sequence.id)}
    assert pair.status == "PENDING"
    assert pair.created_by == scenario.maker.principal_id
    assert 0.0 < pair.confidence <= MAX_CONFIDENCE
    assert scenario.session.committed is True
    assert pair.id in {item.id for item in page.items}
    assert len(page.items) == len(created)

    audit_actions = {evt.action for evt in scenario.session.added_of(AuditEvent)}
    assert "composite_key_candidates.discover" in audit_actions


@pytest.mark.asyncio
async def test_discover_excludes_column_already_declared_as_a_key() -> None:
    scenario = _Scenario()
    scenario.session.seed(
        MetadataConstraint(
            organization_id=scenario.organization_id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.table.id,
            name="pk_customers",
            constraint_type="PRIMARY_KEY",
            columns=[scenario.sequence.name],
            status="ACTIVE",
            fingerprint="fp",
        )
    )

    await scenario.discover()

    touched = {
        column_id
        for candidate in scenario.session.added_of(CompositeKeyCandidate)
        for column_id in candidate.column_ids
    }
    assert str(scenario.sequence.id) not in touched


@pytest.mark.asyncio
async def test_discover_is_idempotent_on_repeat_run() -> None:
    scenario = _Scenario()

    await scenario.discover()
    first_count = len(scenario.session.added_of(CompositeKeyCandidate))
    assert first_count > 0

    second_page = await scenario.discover()

    assert len(scenario.session.added_of(CompositeKeyCandidate)) == first_count
    assert second_page.items == []


@pytest.mark.asyncio
async def test_discover_returns_nothing_without_profile_evidence() -> None:
    scenario = _Scenario()
    bare_table = scenario.session.seed(
        MetadataTable(
            organization_id=scenario.organization_id,
            datasource_id=scenario.datasource.id,
            name="unprofiled",
            object_type="TABLE",
            status="ACTIVE",
        )
    )

    page = await discover_composite_key_candidates(
        bare_table.id, context=scenario.maker, session=scenario.session
    )

    assert page.items == []
    assert scenario.session.added_of(CompositeKeyCandidate) == []


@pytest.mark.asyncio
async def test_discover_returns_404_for_unknown_table() -> None:
    scenario = _Scenario()
    with pytest.raises(HTTPException) as exc_info:
        await discover_composite_key_candidates(
            uuid4(), context=scenario.maker, session=scenario.session
        )
    assert getattr(exc_info.value, "status_code", None) == 404


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_composite_key_candidates_filters_by_status_and_table() -> None:
    scenario = _Scenario()
    await scenario.discover()

    page = await list_composite_key_candidates(
        scenario.datasource.id,
        table_id=scenario.table.id,
        candidate_status="PENDING",
        limit=100,
        offset=0,
        context=scenario.reviewer,
        session=scenario.session,
    )

    assert page.total == len(scenario.session.added_of(CompositeKeyCandidate))
    assert all(item.status == "PENDING" for item in page.items)
    assert all(item.table_id == scenario.table.id for item in page.items)


# ---------------------------------------------------------------------------
# Maker-checker decision (mirrors decide_relationship_candidate's discipline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_rejects_self_review_by_the_maker() -> None:
    scenario = _Scenario()
    await scenario.discover()
    candidate = scenario.session.added_of(CompositeKeyCandidate)[0]

    with pytest.raises(HTTPException) as exc_info:
        await decide_composite_key_candidate(
            candidate.id,
            CompositeKeyCandidateDecision(decision="APPROVE"),
            context=scenario.maker,  # same principal that created it
            session=scenario.session,
        )
    assert getattr(exc_info.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_decide_rejects_an_already_decided_candidate() -> None:
    scenario = _Scenario()
    await scenario.discover()
    candidate = scenario.session.added_of(CompositeKeyCandidate)[0]
    candidate.status = "APPROVED"

    with pytest.raises(HTTPException) as exc_info:
        await decide_composite_key_candidate(
            candidate.id,
            CompositeKeyCandidateDecision(decision="APPROVE"),
            context=scenario.reviewer,
            session=scenario.session,
        )
    assert getattr(exc_info.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_decide_approve_by_a_different_reviewer_succeeds() -> None:
    scenario = _Scenario()
    await scenario.discover()
    candidate = scenario.session.added_of(CompositeKeyCandidate)[0]

    result = await decide_composite_key_candidate(
        candidate.id,
        CompositeKeyCandidateDecision(decision="APPROVE"),
        context=scenario.reviewer,
        session=scenario.session,
    )

    assert result.status == "APPROVED"
    assert result.reviewed_by == scenario.reviewer.principal_id
    assert result.reviewed_at is not None
    outbox_events = {evt.event_type for evt in scenario.session.added_of(OutboxEvent)}
    assert "composite_key_candidate.decided.v1" in outbox_events


@pytest.mark.asyncio
async def test_decide_reject_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        CompositeKeyCandidateDecision(decision="REJECT")


@pytest.mark.asyncio
async def test_decide_returns_404_for_unknown_candidate() -> None:
    scenario = _Scenario()
    with pytest.raises(HTTPException) as exc_info:
        await decide_composite_key_candidate(
            uuid4(),
            CompositeKeyCandidateDecision(decision="APPROVE"),
            context=scenario.reviewer,
            session=scenario.session,
        )
    assert getattr(exc_info.value, "status_code", None) == 404
