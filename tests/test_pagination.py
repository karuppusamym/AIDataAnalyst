"""Unit tests for keyset (cursor) pagination -- no database required.

These verify the two things that make cursor pagination "flat cost regardless
of offset" rather than just a different API shape:

1. The cursor round-trips exactly (encode/decode), and a malformed or
   tampered cursor is rejected rather than silently mis-paginating.
2. The compiled SQL for a keyset query is a single row-value comparison plus
   an `ORDER BY`/`LIMIT` -- critically, it contains no `OFFSET` -- so a
   composite index whose leading columns match the equality filters and
   whose trailing columns match the `ORDER BY` can satisfy it with one index
   range seek, independent of how many pages preceded this one. This is
   checked against both the SQLite and PostgreSQL dialects since the real
   deployment target (PostgreSQL) is not reachable in this sandbox.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import Integer, String, Uuid, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aida.pagination import InvalidCursor, apply_keyset, decode_cursor, encode_cursor


class _Base(DeclarativeBase):
    pass


class _Widget(_Base):
    __tablename__ = "widget"
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    ordinal: Mapped[int] = mapped_column(Integer())


def test_encode_decode_round_trips_uuid_and_string() -> None:
    row_id = uuid4()
    cursor = encode_cursor("some-name", row_id)

    decoded = decode_cursor(cursor, arity=2)

    assert decoded == ("some-name", str(row_id))


def test_decode_rejects_wrong_arity() -> None:
    cursor = encode_cursor("a", "b", "c")
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, arity=2)


@pytest.mark.parametrize(
    "garbage",
    [
        "not-base64-!!!",
        "",
        "bm90LWpzb24tYXQtYWxs",  # base64("not-json-at-all")
    ],
)
def test_decode_rejects_malformed_cursor(garbage: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(garbage, arity=2)


def test_apply_keyset_rejects_arity_mismatch() -> None:
    statement = select(_Widget)
    with pytest.raises(ValueError, match="arity"):
        apply_keyset(statement, (_Widget.name, _Widget.id), ("only-one-value",))


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_keyset_predicate_compiles_with_order_columns(dialect: object) -> None:
    last_id = uuid4()
    statement = (
        select(_Widget)
        .where(_Widget.ordinal > 0)
        .order_by(_Widget.name, _Widget.id)
    )
    statement = apply_keyset(statement, (_Widget.name, _Widget.id), ("checkpoint", last_id)).limit(
        50
    )

    # The defining property of keyset pagination: `apply_keyset` never calls
    # `.offset(...)`, at any page depth -- unlike LIMIT/OFFSET pagination, whose
    # cost grows with how deep the caller has paged. (SQLite's dialect always
    # renders a trailing "OFFSET 0" whether or not one was requested, so the
    # dialect-independent check is on the statement's own offset clause, not
    # the compiled SQL text.)
    assert statement._offset_clause is None

    compiled = str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "LIMIT" in compiled.upper()
    # A single row-value comparison, not two ANDed scalar comparisons -- this is the
    # shape a composite (…, name, id) index satisfies with one range seek.
    assert "(widget.name, widget.id) >" in compiled.lower().replace("`", "")
    assert "order by widget.name, widget.id" in compiled.lower()


def test_postgresql_compiled_sql_has_no_offset_text() -> None:
    """PostgreSQL (the real deployment target) has no SQLite-style OFFSET-0
    quirk, so here the compiled text itself can assert the property directly.
    """
    last_id = uuid4()
    statement = (
        select(_Widget).where(_Widget.ordinal > 0).order_by(_Widget.name, _Widget.id)
    )
    statement = apply_keyset(statement, (_Widget.name, _Widget.id), ("checkpoint", last_id)).limit(
        50
    )

    compiled = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "OFFSET" not in compiled.upper()
