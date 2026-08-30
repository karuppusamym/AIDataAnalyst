"""API-layer coverage for RL-2 (canonical table resolution).

Follows this repo's established convention (see
`tests/test_dbt_run_results_integration.py`) of exercising async endpoint
functions directly with a hand-built `SecurityContext`, backed by a small
in-memory `AsyncSession` double rather than real HTTP/DB infrastructure.

`FakeAsyncSession` is copied (not imported) from
`tests/test_composite_key_api.py` -- same generic, multi-entity, AND-of-
(==, !=, IN) + ORDER BY/LIMIT/OFFSET double, since `canonical_table_api`'s
discover endpoint fans out across several entity types (MetadataTable,
MetadataSchema, MetadataCatalog, MetadataColumn, TableProfile) the same way.
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

from aida.canonical_table_api import (
    decide_canonical_table_group,
    discover_canonical_table_groups,
    list_canonical_table_groups,
)
from aida.canonical_table_resolution import MAX_CONFIDENCE
from aida.main import app
from aida.models import (
    AuditEvent,
    CanonicalTableGroup,
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    TableProfile,
)
from aida.schemas import CanonicalTableGroupDecision, CanonicalTableGroupDiscoveryRequest
from aida.security import SecurityContext

# ---------------------------------------------------------------------------
# Minimal, generic in-memory AsyncSession double (copied from
# tests/test_composite_key_api.py -- see that file's module docstring)
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
    """Minimal in-memory AsyncSession double for `canonical_table_api`."""

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
        return len(self._rows_for(stmt))

    async def scalars(self, stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(self._rows_for(stmt))

    async def execute(self, stmt: Any) -> Any:  # pragma: no cover - unused by this endpoint set
        raise AssertionError("canonical_table_api does not issue multi-entity execute() queries")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_COLUMNS = [
    ("id", "int"),
    ("name", "varchar(50)"),
    ("created_at", "timestamp"),
    ("status", "varchar"),
]


class _Scenario:
    def __init__(self) -> None:
        self.session = FakeAsyncSession()
        self.organization = self.session.seed(Organization(name="Bank Co", slug="bank-co"))
        self.organization_id = self.organization.id

        self.prod_datasource = self.session.seed(
            DataSource(organization_id=self.organization_id, status="ACTIVE")
        )
        self.reporting_datasource = self.session.seed(
            DataSource(organization_id=self.organization_id, status="ACTIVE")
        )
        self.prod_catalog = self.session.seed(
            MetadataCatalog(
                organization_id=self.organization_id,
                datasource_id=self.prod_datasource.id,
                name="analytics",
                status="ACTIVE",
                fingerprint="cat-fp-1",
            )
        )
        self.reporting_catalog = self.session.seed(
            MetadataCatalog(
                organization_id=self.organization_id,
                datasource_id=self.reporting_datasource.id,
                name="reporting",
                status="ACTIVE",
                fingerprint="cat-fp-2",
            )
        )
        self.prod_schema = self.session.seed(
            MetadataSchema(
                organization_id=self.organization_id,
                catalog_id=self.prod_catalog.id,
                name="public",
                status="ACTIVE",
                fingerprint="schema-fp-1",
            )
        )
        self.reporting_schema = self.session.seed(
            MetadataSchema(
                organization_id=self.organization_id,
                catalog_id=self.reporting_catalog.id,
                name="public",
                status="ACTIVE",
                fingerprint="schema-fp-2",
            )
        )
        self.prod_table = self.session.seed(
            MetadataTable(
                organization_id=self.organization_id,
                datasource_id=self.prod_datasource.id,
                schema_id=self.prod_schema.id,
                name="orders",
                object_type="TABLE",
                status="ACTIVE",
                fingerprint="table-fp-1",
            )
        )
        self.reporting_table = self.session.seed(
            MetadataTable(
                organization_id=self.organization_id,
                datasource_id=self.reporting_datasource.id,
                schema_id=self.reporting_schema.id,
                name="orders",
                object_type="TABLE",
                status="ACTIVE",
                fingerprint="table-fp-2",
            )
        )
        for table in (self.prod_table, self.reporting_table):
            for position, (name, physical_type) in enumerate(BASE_COLUMNS, start=1):
                self.session.seed(
                    MetadataColumn(
                        organization_id=self.organization_id,
                        table_id=table.id,
                        name=name,
                        ordinal_position=position,
                        physical_type=physical_type,
                        nullable=True,
                        status="ACTIVE",
                    )
                )
        self.session.seed(
            TableProfile(
                organization_id=self.organization_id,
                datasource_id=self.prod_datasource.id,
                table_id=self.prod_table.id,
                sampled_row_count=1000,
                row_count_estimate=500_000,
                status="COMPLETED",
                created_at=datetime.now(UTC),
            )
        )
        self.session.seed(
            TableProfile(
                organization_id=self.organization_id,
                datasource_id=self.reporting_datasource.id,
                table_id=self.reporting_table.id,
                sampled_row_count=1000,
                row_count_estimate=480_000,
                status="COMPLETED",
                created_at=datetime.now(UTC),
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
        return await discover_canonical_table_groups(
            self.organization_id,
            CanonicalTableGroupDiscoveryRequest(),
            context=self.maker,
            session=self.session,
        )


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def test_canonical_table_endpoints_are_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/organizations/{organization_id}/canonical-table-groups/discover",
        "/v1/organizations/{organization_id}/canonical-table-groups",
        "/v1/canonical-table-groups/{group_id}/decision",
    }
    assert expected <= paths.keys()
    assert "post" in paths["/v1/organizations/{organization_id}/canonical-table-groups/discover"]
    assert "get" in paths["/v1/organizations/{organization_id}/canonical-table-groups"]
    assert "post" in paths["/v1/canonical-table-groups/{group_id}/decision"]


# ---------------------------------------------------------------------------
# Discovery + persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_persists_a_pending_group_with_default_canonical() -> None:
    scenario = _Scenario()

    page = await scenario.discover()

    created = scenario.session.added_of(CanonicalTableGroup)
    assert len(created) == 1
    group = created[0]
    assert set(group.member_table_ids) == {
        str(scenario.prod_table.id),
        str(scenario.reporting_table.id),
    }
    assert group.canonical_table_id is None  # never auto-set -- steward confirms it
    assert group.status == "PENDING"
    assert group.created_by == scenario.maker.principal_id
    assert 0.0 < group.confidence <= MAX_CONFIDENCE
    assert group.evidence["default_canonical_table_id"] == str(scenario.prod_table.id)
    assert scenario.session.committed is True
    assert group.id in {item.id for item in page.items}

    audit_actions = {evt.action for evt in scenario.session.added_of(AuditEvent)}
    assert "canonical_table_groups.discover" in audit_actions


@pytest.mark.asyncio
async def test_discover_is_idempotent_on_repeat_run() -> None:
    scenario = _Scenario()

    await scenario.discover()
    first_count = len(scenario.session.added_of(CanonicalTableGroup))
    assert first_count == 1

    second_page = await scenario.discover()

    assert len(scenario.session.added_of(CanonicalTableGroup)) == first_count
    assert second_page.items == []


@pytest.mark.asyncio
async def test_discover_narrows_to_requested_datasource_ids() -> None:
    scenario = _Scenario()
    unrelated_datasource = scenario.session.seed(
        DataSource(organization_id=scenario.organization_id, status="ACTIVE")
    )

    page = await discover_canonical_table_groups(
        scenario.organization_id,
        CanonicalTableGroupDiscoveryRequest(datasource_ids=[unrelated_datasource.id]),
        context=scenario.maker,
        session=scenario.session,
    )

    assert page.items == []
    assert scenario.session.added_of(CanonicalTableGroup) == []


@pytest.mark.asyncio
async def test_discover_returns_404_for_unknown_organization() -> None:
    scenario = _Scenario()
    missing_org_id = uuid4()
    # A context whose own organization_id *is* the missing id -- so
    # enforce_organization (which INV-5 requires to fire before any session
    # access) passes, and the 404 below is reached and exercised on its own.
    context = SecurityContext(
        principal_id="maker@bank.internal",
        principal_type="USER",
        roles=frozenset({"MetadataAdmin"}),
        organization_id=missing_org_id,
    )
    with pytest.raises(HTTPException) as exc_info:
        await discover_canonical_table_groups(
            missing_org_id,
            CanonicalTableGroupDiscoveryRequest(),
            context=context,
            session=scenario.session,
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_canonical_table_groups_filters_by_status() -> None:
    scenario = _Scenario()
    await scenario.discover()

    page = await list_canonical_table_groups(
        scenario.organization_id,
        candidate_status="PENDING",
        limit=100,
        offset=0,
        context=scenario.reviewer,
        session=scenario.session,
    )

    assert page.total == 1
    assert all(item.status == "PENDING" for item in page.items)


# ---------------------------------------------------------------------------
# Maker-checker decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_rejects_self_review_by_the_maker() -> None:
    scenario = _Scenario()
    await scenario.discover()
    group = scenario.session.added_of(CanonicalTableGroup)[0]

    canonical_choice = group.canonical_table_id or scenario.prod_table.id
    with pytest.raises(HTTPException) as exc_info:
        await decide_canonical_table_group(
            group.id,
            CanonicalTableGroupDecision(decision="APPROVE", canonical_table_id=canonical_choice),
            context=scenario.maker,
            session=scenario.session,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_decide_rejects_an_already_decided_group() -> None:
    scenario = _Scenario()
    await scenario.discover()
    group = scenario.session.added_of(CanonicalTableGroup)[0]
    group.status = "APPROVED"

    with pytest.raises(HTTPException) as exc_info:
        await decide_canonical_table_group(
            group.id,
            CanonicalTableGroupDecision(
                decision="APPROVE", canonical_table_id=scenario.prod_table.id
            ),
            context=scenario.reviewer,
            session=scenario.session,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_decide_approve_persists_the_stewards_canonical_choice() -> None:
    scenario = _Scenario()
    await scenario.discover()
    group = scenario.session.added_of(CanonicalTableGroup)[0]

    # Steward overrides the detector's default (prod_table) and picks the
    # reporting mirror instead -- an override must be honored, not ignored.
    result = await decide_canonical_table_group(
        group.id,
        CanonicalTableGroupDecision(
            decision="APPROVE", canonical_table_id=scenario.reporting_table.id
        ),
        context=scenario.reviewer,
        session=scenario.session,
    )

    assert result.status == "APPROVED"
    assert result.canonical_table_id == scenario.reporting_table.id
    assert result.reviewed_by == scenario.reviewer.principal_id
    assert result.reviewed_at is not None
    outbox_events = {evt.event_type for evt in scenario.session.added_of(OutboxEvent)}
    assert "canonical_table_group.decided.v1" in outbox_events


@pytest.mark.asyncio
async def test_decide_approve_rejects_a_canonical_choice_outside_the_membership() -> None:
    scenario = _Scenario()
    await scenario.discover()
    group = scenario.session.added_of(CanonicalTableGroup)[0]

    with pytest.raises(HTTPException) as exc_info:
        await decide_canonical_table_group(
            group.id,
            CanonicalTableGroupDecision(decision="APPROVE", canonical_table_id=uuid4()),
            context=scenario.reviewer,
            session=scenario.session,
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_decide_reject_does_not_require_a_canonical_choice() -> None:
    scenario = _Scenario()
    await scenario.discover()
    group = scenario.session.added_of(CanonicalTableGroup)[0]

    result = await decide_canonical_table_group(
        group.id,
        CanonicalTableGroupDecision(decision="REJECT", reason="false positive"),
        context=scenario.reviewer,
        session=scenario.session,
    )

    assert result.status == "REJECTED"
    assert result.canonical_table_id is None


def test_decide_reject_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        CanonicalTableGroupDecision(decision="REJECT")


def test_decide_approve_requires_a_canonical_choice() -> None:
    with pytest.raises(ValidationError, match="canonical_table_id is required"):
        CanonicalTableGroupDecision(decision="APPROVE")


@pytest.mark.asyncio
async def test_decide_returns_404_for_unknown_group() -> None:
    scenario = _Scenario()
    with pytest.raises(HTTPException) as exc_info:
        await decide_canonical_table_group(
            uuid4(),
            CanonicalTableGroupDecision(
                decision="APPROVE", canonical_table_id=scenario.prod_table.id
            ),
            context=scenario.reviewer,
            session=scenario.session,
        )
    assert exc_info.value.status_code == 404
