"""API-layer coverage for KG-5 (saved Graph Explorer perspectives).

Follows this repo's established convention (see
`tests/test_composite_key_api.py`, itself following `tests/test_dbt_run_results_integration.py`)
of exercising async endpoint functions directly with a hand-built `SecurityContext`, backed by a
small in-memory `AsyncSession` double rather than real HTTP/DB infrastructure.

`FakeAsyncSession` here only needs to interpret plain AND-of-`==` where-clauses against a single
entity type -- `graph_perspectives_api`'s queries never join, order, or paginate at the SQL
level (list-time ordering/pagination happens in Python, after the role-visibility filter -- see
that module's `list_graph_perspectives` for why).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aida.graph_perspectives_api import (
    create_graph_perspective,
    delete_graph_perspective,
    get_graph_perspective,
    list_graph_perspectives,
    update_graph_perspective,
)
from aida.models import GraphPerspective, Organization
from aida.schemas import (
    GRAPH_PERSPECTIVE_MAX_VIEW_STATE_BYTES,
    GraphPerspectiveCreate,
    GraphPerspectiveUpdate,
)
from aida.security import SecurityContext

# ---------------------------------------------------------------------------
# Minimal, generic in-memory AsyncSession double (no joins/order/limit needed here)
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


def _extract_eq_filters(whereclause: Any) -> list[tuple[str, str, Any]]:
    """Recursively pull (table_name, col_name, value) triples out of an AND-only whereclause."""
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None)
    if clauses is not None:
        out: list[tuple[str, str, Any]] = []
        for clause in clauses:
            out.extend(_extract_eq_filters(clause))
        return out
    left = getattr(whereclause, "left", None)
    right = getattr(whereclause, "right", None)
    table = getattr(left, "table", None)
    col_name = getattr(left, "key", None) or getattr(left, "name", None)
    if left is None or right is None or table is None or col_name is None:
        return []
    value = getattr(right, "value", right)
    return [(table.name, col_name, value)]


def _matches(obj: Any, table_name: str, filters: list[tuple[str, str, Any]]) -> bool:
    for ftable, col, value in filters:
        if ftable != table_name:
            continue
        if getattr(obj, col) != value:
            return False
    return True


class FakeAsyncSession:
    """Minimal in-memory AsyncSession double for `graph_perspectives_api`."""

    def __init__(self) -> None:
        self._store: dict[type, dict[Any, Any]] = defaultdict(dict)
        self._table_registry: dict[str, type] = {}
        self.added: list[Any] = []
        self.deleted: list[Any] = []
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
        # Mimic the ORM's python-side TimestampMixin defaults, which only fire on a real
        # flush -- see the equivalent note in `tests/test_composite_key_api.py`.
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

    async def delete(self, obj: Any) -> None:
        self._store.get(type(obj), {}).pop(obj.id, None)
        self.deleted.append(obj)

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
        filters = _extract_eq_filters(stmt.whereclause)
        return [
            obj for obj in self._store.get(model, {}).values() if _matches(obj, table_name, filters)
        ]

    async def scalar(self, stmt: Any) -> Any | None:
        return len(self._rows_for(stmt))

    async def scalars(self, stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(self._rows_for(stmt))

    async def execute(self, stmt: Any) -> Any:  # pragma: no cover - unused by this endpoint set
        raise AssertionError("graph_perspectives_api does not issue multi-entity execute() queries")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Scenario:
    def __init__(self) -> None:
        self.organization_id = uuid4()
        self.session = FakeAsyncSession()
        self.session.seed(Organization(id=self.organization_id, name="Bank", slug="bank"))

        self.owner = SecurityContext(
            principal_id="owner@bank.internal",
            principal_type="USER",
            roles=frozenset({"Analyst"}),
            organization_id=self.organization_id,
        )
        self.shared_viewer = SecurityContext(
            principal_id="steward@bank.internal",
            principal_type="USER",
            roles=frozenset({"DataSteward"}),
            organization_id=self.organization_id,
        )
        self.stranger = SecurityContext(
            principal_id="stranger@bank.internal",
            principal_type="USER",
            roles=frozenset({"Viewer"}),
            organization_id=self.organization_id,
        )

    async def create(self, **overrides: Any) -> GraphPerspective:
        payload = {
            "name": "Fraud Investigation View",
            "description": "Center on the flagged account, two hops out.",
            "allowed_viewer_roles": [],
            "view_state": {"centerNodeId": str(uuid4()), "depth": 2, "layout": "dagre"},
        }
        payload.update(overrides)
        return await create_graph_perspective(
            self.organization_id,
            GraphPerspectiveCreate(**payload),
            context=self.owner,
            session=self.session,
        )


# ---------------------------------------------------------------------------
# Owner CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_can_create_and_read_their_own_perspective() -> None:
    scenario = _Scenario()

    created = await scenario.create()

    assert created.owner_principal == scenario.owner.principal_id
    assert created.organization_id == scenario.organization_id
    assert scenario.session.committed is True

    fetched = await get_graph_perspective(
        created.id, context=scenario.owner, session=scenario.session
    )
    assert fetched.id == created.id
    assert fetched.view_state == created.view_state


@pytest.mark.asyncio
async def test_owner_can_update_and_delete_their_own_perspective() -> None:
    scenario = _Scenario()
    created = await scenario.create()

    updated = await update_graph_perspective(
        created.id,
        GraphPerspectiveUpdate(name="PII Data Flow Overview"),
        context=scenario.owner,
        session=scenario.session,
    )
    assert updated.name == "PII Data Flow Overview"

    await delete_graph_perspective(created.id, context=scenario.owner, session=scenario.session)
    assert await scenario.session.get(GraphPerspective, created.id) is None


# ---------------------------------------------------------------------------
# Visibility / sharing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_owner_without_a_shared_role_gets_404_and_is_absent_from_their_list() -> None:
    scenario = _Scenario()
    created = await scenario.create(allowed_viewer_roles=[])

    with pytest.raises(HTTPException) as exc_info:
        await get_graph_perspective(
            created.id, context=scenario.stranger, session=scenario.session
        )
    assert exc_info.value.status_code == 404

    page = await list_graph_perspectives(
        scenario.organization_id,
        datasource_id=None,
        limit=100,
        offset=0,
        context=scenario.stranger,
        session=scenario.session,
    )
    assert created.id not in {item.id for item in page.items}


@pytest.mark.asyncio
async def test_non_owner_with_an_allowed_role_can_read_but_not_write() -> None:
    scenario = _Scenario()
    created = await scenario.create(allowed_viewer_roles=["DataSteward"])

    fetched = await get_graph_perspective(
        created.id, context=scenario.shared_viewer, session=scenario.session
    )
    assert fetched.id == created.id

    page = await list_graph_perspectives(
        scenario.organization_id,
        datasource_id=None,
        limit=100,
        offset=0,
        context=scenario.shared_viewer,
        session=scenario.session,
    )
    assert created.id in {item.id for item in page.items}

    with pytest.raises(HTTPException) as exc_info:
        await update_graph_perspective(
            created.id,
            GraphPerspectiveUpdate(name="hijacked"),
            context=scenario.shared_viewer,
            session=scenario.session,
        )
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as exc_info:
        await delete_graph_perspective(
            created.id, context=scenario.shared_viewer, session=scenario.session
        )
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Payload bound + pagination
# ---------------------------------------------------------------------------


def test_oversized_view_state_is_rejected() -> None:
    oversized = {"padding": "x" * (GRAPH_PERSPECTIVE_MAX_VIEW_STATE_BYTES + 1)}
    with pytest.raises(ValidationError, match="exceeds"):
        GraphPerspectiveCreate(name="Too Big", view_state=oversized)


@pytest.mark.asyncio
async def test_list_pagination_behaves_normally() -> None:
    scenario = _Scenario()
    for index in range(5):
        await scenario.create(name=f"View {index}")

    page = await list_graph_perspectives(
        scenario.organization_id,
        datasource_id=None,
        limit=2,
        offset=1,
        context=scenario.owner,
        session=scenario.session,
    )
    assert page.total == 5
    assert page.limit == 2
    assert page.offset == 1
    assert len(page.items) == 2


@pytest.mark.asyncio
async def test_get_returns_404_for_unknown_perspective() -> None:
    scenario = _Scenario()
    with pytest.raises(HTTPException) as exc_info:
        await get_graph_perspective(uuid4(), context=scenario.owner, session=scenario.session)
    assert exc_info.value.status_code == 404
