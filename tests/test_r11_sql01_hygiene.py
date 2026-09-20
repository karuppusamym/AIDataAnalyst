"""R11-SQL01 hygiene: what a receipt keeps beside the redacted shape, and what redaction leaves.

Four loose ends the row recorded, one section each:

* **A. The statement digest is keyed.** It is stored next to the redacted shape, and a pasted
  statement's text includes its literals, so an unkeyed hash let a reader of the row confirm a
  guessed literal. The digest is now the deployment signer's, as a parameter fingerprint is.
* **B. Booleans are values.** sqlglot models `TRUE`/`FALSE` as `exp.Boolean`, not `exp.Literal`,
  so the stored shape kept them. It does not now -- in either dialect the sample estate uses --
  while the statement that *runs* is untouched.
* **C. Comments are not stored.** A comment can hold a name, a literal or a secret, and sqlglot
  carries it into the rendered statement. The reviewed path's stored shapes (a receipt's
  `redacted_sql`, an execution's `normalized_sql`) drop them; ingestion still keeps them, because
  screening reads a definition's prose.
* **D. The renderer names its refusals.** `ToolParameterError` carries codes, and the workspace
  reads them instead of matching the renderer's message phrases.

HTTP tests drive the real routes against an in-memory database, with the connector doubled to
record the statements it is asked to execute: "the source received it" and "the platform stored it"
are both read off what actually happened.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from aida.connectors.base import QueryResult
from aida.main import app
from aida.models import AuditEvent, QueryExecution
from aida.sql_guard import SqlGuard
from aida.sql_redaction import redact_for_storage, redact_sql_literals
from aida.sql_workspace import (
    PARAMETER_INVALID,
    PARAMETER_TOO_LONG,
    PARAMETER_TYPE_MISMATCH,
    PARAMETER_UNDECLARED,
    PARAMETER_UNUSED,
    PARAMETER_VALUE_MAX_LENGTH,
    PARAMETER_VALUE_MISSING,
    DraftParameter,
    bind_parameters,
    statement_digest,
)
from aida.sql_workspace_models import SqlDraftReceipt
from aida.tool_rendering import ToolParameterCode, ToolParameterError, ToolParameterIssue
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.doubles import FakeSqlExecutor
from tests.test_f01_context_product_execution_boundary import _Scenario
from tests.test_r11_sql01_sql_workspace import _headers

ORDERS_SQL = "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = 'LITERAL-7731'"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


@pytest_asyncio.fixture
async def http(scenario: _Scenario) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield scenario.db

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sql01.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


class _Source(FakeSqlExecutor):
    """Records the statements the source is asked to execute."""

    def __init__(self, executed: list[str]) -> None:
        super().__init__(({"order_id": "O-1"},))
        self._executed = executed

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        self._executed.append(sql)
        return await super().execute_read_query(sql, timeout_seconds=timeout_seconds)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statements: list[str] = []
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: _Source(statements),
    )
    return statements


async def _draft(http: httpx.AsyncClient, scenario: _Scenario, **body: Any) -> httpx.Response:
    return await http.post(
        f"/v1/datasources/{scenario.datasource.id}/sql-drafts",
        json=body,
        headers=_headers(scenario),
    )


async def _run(
    http: httpx.AsyncClient, scenario: _Scenario, receipt_id: str, **body: Any
) -> httpx.Response:
    return await http.post(
        f"/v1/sql-drafts/{receipt_id}/run", json=body, headers=_headers(scenario)
    )


async def _receipt(http: httpx.AsyncClient, scenario: _Scenario, **body: Any) -> str:
    response = await _draft(http, scenario, **body)
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt is not None, response.json()["validation"]
    return str(receipt["id"])


async def _where_stored(scenario: _Scenario, needle: str) -> list[str]:
    """Every `table.column` of the platform database that holds `needle` -- all tables, all rows."""
    found: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for row in (await scenario.db.execute(select(table))).mappings().all():
            found.update(
                f"{table.name}.{column}" for column, value in row.items() if needle in repr(value)
            )
    return sorted(found)


# ---------------------------------------------------------------------------
# A. The statement digest is keyed
# ---------------------------------------------------------------------------


def _bare_sha256(payload: dict[str, Any]) -> str:
    """What the digest was before it was keyed: a hash anyone can compute from a guess."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_UNBOUND: dict[str, Any] = {
    "max_rows": None,
    "context_product_version_id": None,
    "workspace_id": None,
}


class _RecordingSigner:
    """A signing provider that records what it was asked to sign and signs it its own way."""

    def __init__(self) -> None:
        self.signed: list[str] = []

    async def sign(self, data: str) -> str:
        self.signed.append(data)
        return "signer-says-" + hashlib.sha256(b"key" + data.encode("utf-8")).hexdigest()[:32]

    async def verify(self, data: str, signature: str) -> bool:  # pragma: no cover - unused
        return signature == await self.sign(data)


async def test_the_digest_is_what_the_deployments_signer_makes_of_the_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = _RecordingSigner()
    monkeypatch.setattr("aida.signing.resolve_signing_provider", lambda settings: signer)

    digest = await statement_digest(Settings(_env_file=None), sql=ORDERS_SQL, **_UNBOUND)

    assert digest.startswith("signer-says-"), "the deployment's signer made the digest"
    [signed] = signer.signed
    assert "LITERAL-7731" in signed, "the signer was given the whole statement, literal and all"


async def test_the_digest_depends_on_the_signing_key() -> None:
    one = await statement_digest(
        Settings(_env_file=None, audit_hmac_key="k" * 32), sql=ORDERS_SQL, **_UNBOUND
    )
    other = await statement_digest(
        Settings(_env_file=None, audit_hmac_key="j" * 32), sql=ORDERS_SQL, **_UNBOUND
    )

    assert one != other
    assert _bare_sha256({"sql": ORDERS_SQL, **_UNBOUND}) not in (one, other)


async def test_a_stored_digest_cannot_confirm_a_guessed_literal(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """The attack this closes. A reader of the row has the redacted shape, the limit, the product
    and the workspace -- everything but the literal -- so they hash a guess and compare. Under the
    old digest the right guess matched. It matches nothing now, and only the key holder's own
    recomputation does."""
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)
    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    assert receipt is not None
    settings = Settings(_env_file=None)

    payload = {"sql": ORDERS_SQL, **_UNBOUND}
    guesses = {
        "the digest as it was before it was keyed": _bare_sha256(payload),
        "the same, naming its purpose": _bare_sha256(
            {"purpose": "sql_draft.statement.v2", **payload}
        ),
    }
    for how, guess in guesses.items():
        assert receipt.statement_digest != guess, f"the right guess confirmed: {how}"
    # The holder of the key recomputes it -- which is exactly what Run does.
    assert receipt.statement_digest == await statement_digest(
        settings, sql=ORDERS_SQL, **_UNBOUND
    )


async def test_a_receipt_issued_before_the_digest_was_keyed_revalidates(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """The stated cost of the change. A receipt still unexpired at deploy carries the old bare
    hash, which no Run can match: it is REVALIDATION_REQUIRED -- not consumed, nothing executed --
    and validating the statement again gives a receipt that runs."""
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)
    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    assert receipt is not None
    receipt.statement_digest = _bare_sha256({"sql": ORDERS_SQL, **_UNBOUND})
    await scenario.db.commit()

    refused = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert executed == []
    await scenario.db.refresh(receipt)
    assert receipt.status == "VALIDATED", "a refusal spends nothing"
    denied = (
        await scenario.db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "sql_draft.run", AuditEvent.outcome == "DENIED"
            )
        )
    ).all()
    assert [audit.details["reason"] for audit in denied] == ["REVALIDATION_REQUIRED"]

    fresh = await _receipt(http, scenario, sql=ORDERS_SQL)
    ran = await _run(http, scenario, fresh, sql=ORDERS_SQL)
    assert ran.status_code == 200, ran.text
    assert len(executed) == 1


async def test_a_signers_output_fits_the_column_the_digest_is_kept_in() -> None:
    """No migration: the local provider's 64 hex characters, and Vault Transit's `vault:v<n>:`
    plus 44 base64 characters for a SHA-256 HMAC (`query_execution.sql_hash` keeps the same)."""
    column = SqlDraftReceipt.__table__.c.statement_digest.type  # type: ignore[attr-defined]
    local = await statement_digest(Settings(_env_file=None), sql=ORDERS_SQL, **_UNBOUND)
    vault_shaped = "vault:v12:" + "A" * 44

    assert len(local) <= column.length and len(vault_shaped) <= column.length
    assert re.fullmatch(r"[0-9a-f]{64}", local)


# ---------------------------------------------------------------------------
# B. Booleans are values
# ---------------------------------------------------------------------------

#: (dialect, statement). The two dialects the sample estate uses. T-SQL has no boolean literal, but
#: sqlglot accepts `TRUE` there and renders it as `1`, `0` or `(1 = 1)` -- a number the lexical
#: value scan then sent to the scrub, so the same statement used to be stored as LEXICAL.
BOOLEAN_STATEMENTS = [
    (
        "postgres",
        "SELECT o.order_id FROM retail.orders AS o WHERE o.active = TRUE AND o.void = FALSE",
    ),
    (
        "postgres",
        "SELECT o.order_id FROM retail.orders AS o WHERE o.a IS TRUE OR o.b IS NOT FALSE",
    ),
    ("postgres", "SELECT CASE WHEN o.vip THEN TRUE ELSE FALSE END AS v FROM retail.orders AS o"),
    ("postgres", "SELECT o.order_id FROM retail.orders AS o WHERE TRUE"),
    ("tsql", "SELECT o.order_id FROM retail.orders AS o WHERE o.active = TRUE AND o.void = FALSE"),
    ("tsql", "SELECT o.order_id FROM retail.orders AS o WHERE o.a IS TRUE"),
    ("tsql", "SELECT CASE WHEN o.vip = 1 THEN TRUE ELSE FALSE END AS v FROM retail.orders AS o"),
    ("tsql", "SELECT o.order_id FROM retail.orders AS o WHERE TRUE"),
]
_BOOLEAN_WORD = re.compile(r"\b(?:TRUE|FALSE)\b", re.IGNORECASE)
#: What sqlglot writes for a boolean in T-SQL: `1`, `0`, `(1 = 1)`, `(1 = 0)`.
_BIT = re.compile(r"(?<![\w.])[01](?![\w.])")
_PLACEHOLDER_TEXT = re.compile(r":redacted|%\(redacted\)s")


def _value_nodes(sql: str, dialect: str) -> int:
    return sum(
        1
        for node in parse_one(sql, read=dialect).walk()
        if isinstance(node, exp.Literal | exp.Boolean)
    )


@pytest.mark.parametrize(("dialect", "sql"), BOOLEAN_STATEMENTS)
def test_the_stored_shape_holds_no_boolean_value(dialect: str, sql: str) -> None:
    stored = redact_for_storage(sql, dialect=dialect)

    assert stored is not None and stored.redacted is not None
    assert stored.status == "PARSED", "a boolean is replaced as a node, not scrubbed as text"
    assert not _BOOLEAN_WORD.search(stored.redacted), stored.redacted
    assert not _BIT.search(stored.redacted), stored.redacted
    assert len(_PLACEHOLDER_TEXT.findall(stored.redacted)) == _value_nodes(sql, dialect), (
        "every value in the statement became one placeholder"
    )
    parse_one(stored.redacted, read=dialect)  # the shape is still a statement a parser can read
    # The gateway's function, which is what `normalized_sql` is made with.
    gateway_shape = redact_sql_literals(sql, dialect=dialect)
    assert not _BOOLEAN_WORD.search(gateway_shape) and not _BIT.search(gateway_shape)


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_a_number_and_a_string_are_still_replaced_beside_a_boolean(dialect: str) -> None:
    stored = redact_for_storage(
        "SELECT o.order_id FROM retail.orders AS o "
        "WHERE o.active = TRUE AND o.qty > 40 AND o.code = 'LITERAL-7731'",
        dialect=dialect,
    )

    assert stored is not None and stored.redacted is not None
    assert "LITERAL-7731" not in stored.redacted and "40" not in stored.redacted
    assert len(_PLACEHOLDER_TEXT.findall(stored.redacted)) == 3


async def test_the_statement_that_runs_keeps_its_boolean_and_the_record_does_not(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    sql = "SELECT o.order_id FROM retail.orders AS o WHERE o.active = TRUE AND o.void = FALSE"
    drafted = await _draft(http, scenario, sql=sql)
    assert drafted.status_code == 200, drafted.text
    receipt_id = drafted.json()["receipt"]["id"]

    ran = await _run(http, scenario, receipt_id, sql=sql)

    assert ran.status_code == 200, ran.text
    [statement] = executed
    assert _BOOLEAN_WORD.findall(statement) == ["TRUE", "FALSE"], "the source received both"
    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    execution = (await scenario.db.scalars(select(QueryExecution))).one()
    for where, stored in (
        ("receipt.redacted_sql", receipt.redacted_sql if receipt else None),
        ("execution.normalized_sql", execution.normalized_sql),
        ("validation.normalized_sql", drafted.json()["validation"]["normalized_sql"]),
    ):
        assert stored, f"{where} is what this test reads"
        assert not _BOOLEAN_WORD.search(stored), f"{where}: {stored}"


# ---------------------------------------------------------------------------
# C. Comments are not stored
# ---------------------------------------------------------------------------

SENTINEL = "ZZ-COMMENT-SENTINEL-6120"

#: `@@` marks where the sentinel goes; it is filled in below, so no SQL is built by interpolation.
#: (dialect, statement, whether the comment sits right after a literal). sqlglot hangs a comment on
#: the token before it, and when that is a literal the redactor replaces the node and the comment
#: goes with it: those statements lose it with or without `strip_comments`.
_COMMENTED_TEMPLATES = [
    ("postgres", "SELECT o.order_id FROM retail.orders AS o -- @@\nWHERE o.qty > 3", False),
    ("postgres", "SELECT /* @@ */ o.order_id FROM retail.orders AS o WHERE o.qty > 3", False),
    ("postgres", "-- @@\nSELECT o.order_id FROM retail.orders AS o", False),
    ("postgres", "SELECT o.order_id FROM retail.orders AS o WHERE o.qty > 3 /* @@ */", True),
    (
        "postgres",
        "SELECT CASE WHEN o.qty > 3 /* @@ */ THEN 1 ELSE 0 END FROM retail.orders AS o",
        True,
    ),
    ("tsql", "SELECT o.order_id FROM retail.orders AS o -- @@\nWHERE o.qty > 3", False),
    ("tsql", "SELECT /* @@ */ o.order_id FROM retail.orders AS o WHERE o.qty > 3", False),
    ("tsql", "SELECT o.order_id FROM retail.orders AS o WHERE o.qty > 3 -- @@", True),
]
COMMENTED_STATEMENTS = [
    (dialect, template.replace("@@", SENTINEL)) for dialect, template, _ in _COMMENTED_TEMPLATES
]
#: The statements whose comment sits on a node that survives redaction.
KEPT_BY_DEFAULT = [
    (dialect, template.replace("@@", SENTINEL))
    for dialect, template, on_a_literal in _COMMENTED_TEMPLATES
    if not on_a_literal
]


@pytest.mark.parametrize(("dialect", "sql"), COMMENTED_STATEMENTS)
def test_a_comment_is_dropped_from_the_stored_shape_when_asked(dialect: str, sql: str) -> None:
    stored = redact_for_storage(sql, dialect=dialect, strip_comments=True)

    assert stored is not None and stored.redacted is not None and stored.status == "PARSED"
    assert SENTINEL not in stored.redacted, stored.redacted
    assert SENTINEL not in redact_sql_literals(sql, dialect=dialect, strip_comments=True)
    parse_one(stored.redacted, read=dialect)


@pytest.mark.parametrize(("dialect", "sql"), KEPT_BY_DEFAULT)
def test_ingestion_keeps_a_definitions_comments(dialect: str, sql: str) -> None:
    """The default is unchanged: screening reads a stored definition's prose for injected
    instructions, and the ingestion callers of `redact_for_storage` do not ask for it to go."""
    stored = redact_for_storage(sql, dialect=dialect)

    assert stored is not None and stored.redacted is not None
    assert SENTINEL in stored.redacted


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_no_comment_survives_wherever_it_sits_in_a_statement(dialect: str) -> None:
    """A comment inserted at every space of a statement, both spellings, none stored."""
    base = (
        "WITH recent AS (SELECT o.order_id, o.total FROM retail.orders AS o "
        "WHERE o.placed_on >= '2024-01-01') "
        "SELECT c.name, COUNT(*) AS n, SUM(r.total) AS revenue, "
        "CASE WHEN c.vip = 1 THEN 'V' ELSE 'N' END AS tier "
        "FROM retail.customer AS c LEFT JOIN recent AS r ON r.order_id = c.customer_id "
        "WHERE c.region IN ('EMEA', 'APAC') AND c.qty BETWEEN 2 AND 9 "
        "GROUP BY c.name, c.vip HAVING COUNT(*) > 2 ORDER BY revenue DESC"
    )
    checked = 0
    for position in (m.start() for m in re.finditer(" ", base)):
        for comment in (f" /* {SENTINEL} */ ", f" -- {SENTINEL}\n"):
            sql = base[:position] + comment + base[position + 1 :]
            try:
                parse_one(sql, read=dialect)
            except ParseError:
                continue  # sqlglot cannot read a comment between `GROUP` and `BY`; nothing to store
            stored = redact_for_storage(sql, dialect=dialect, strip_comments=True)
            assert stored is not None and stored.redacted is not None
            assert SENTINEL not in stored.redacted, (position, comment, stored.redacted)
            assert SENTINEL not in redact_sql_literals(sql, dialect=dialect, strip_comments=True)
            checked += 1
    assert checked > 100, "the sweep covered the statement"


def test_the_lexical_scrub_drops_comments_and_keeps_tokens_apart() -> None:
    """A statement sqlglot cannot read is scrubbed as text; its comments go the same way."""
    unreadable = "SELEC o.a FRM retail.orders -- @@\nWHERE o.b = 7 AND/* @@ */o.c = 'x'".replace(
        "@@", SENTINEL
    )

    stored = redact_for_storage(unreadable, dialect="postgres", strip_comments=True)

    assert stored is not None and stored.status == "LEXICAL" and stored.redacted is not None
    assert SENTINEL not in stored.redacted
    assert "\nWHERE" in stored.redacted, "a line comment ends at its newline, which stays"
    assert "AND o.c" in stored.redacted, "a block comment leaves a space, not a joined token"
    kept = redact_for_storage(unreadable, dialect="postgres")
    # Kept, digits aside: the scrub masks a comment's numbers whether or not it keeps the comment.
    assert kept is not None and kept.redacted is not None
    assert SENTINEL.rsplit("-", 1)[0] in kept.redacted


def test_a_routine_bodys_comments_go_with_the_rest() -> None:
    routine = (
        "CREATE FUNCTION f() RETURNS int AS $$ BEGIN -- @@\n RETURN 1; /* @@ */ END $$ "
        "LANGUAGE plpgsql"
    ).replace("@@", SENTINEL)

    stored = redact_for_storage(routine, dialect="postgres", strip_comments=True)

    assert stored is not None and stored.redacted is not None
    assert SENTINEL not in stored.redacted and "RETURN" in stored.redacted


async def test_a_comment_reaches_no_stored_row_audit_record_or_response(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """INV-6, read off the whole platform database rather than off the columns we expect."""
    email = "jane.doe@example.com"
    sql = (
        "-- @@-LEAD ssn 123-45-6789\n"
        "SELECT o.order_id /* @@-INLINE EMAIL */ FROM retail.orders AS o "
        "WHERE o.order_id = 'LITERAL-7731' -- @@-TAIL"
    ).replace("@@", SENTINEL).replace("EMAIL", email)
    drafted = await _draft(http, scenario, sql=sql)
    assert drafted.status_code == 200, drafted.text
    receipt_id = drafted.json()["receipt"]["id"]
    ran = await _run(http, scenario, receipt_id, sql=sql)
    assert ran.status_code == 200, ran.text
    history = await http.get(
        f"/v1/datasources/{scenario.datasource.id}/sql-drafts", headers=_headers(scenario)
    )
    lineage = await http.get(
        f"/v1/query-executions/{ran.json()['execution']['execution_id']}/lineage",
        headers=_headers(scenario),
    )
    assert history.status_code == 200 and lineage.status_code == 200, lineage.text

    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    execution = (await scenario.db.scalars(select(QueryExecution))).one()
    assert receipt is not None and receipt.redacted_sql, "the receipt kept a shape to read"
    assert execution.normalized_sql, "the execution kept a shape to read"
    audits = (await scenario.db.scalars(select(AuditEvent))).all()
    assert audits, "the scan covers the audit trail"
    for needle in (SENTINEL, email, "123-45-6789"):
        assert needle not in (receipt.redacted_sql or ""), f"{needle} in the receipt's shape"
        assert needle not in (execution.normalized_sql or ""), f"{needle} in the execution's shape"
        assert await _where_stored(scenario, needle) == [], f"{needle} is stored"
        assert all(needle not in str(audit.details) for audit in audits), f"{needle} in an audit"
        for response in (drafted, ran, history, lineage):
            assert needle not in response.text, f"{needle} was returned"


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_a_referenced_column_is_its_name_without_the_comment_on_it(dialect: str) -> None:
    """`referenced_columns` is stored and returned. sqlglot hangs a comment on the column node,
    which used to make the entry `o.order_id /* ... */` -- a leak, and a second name for a column
    the statement names once. The text that *executes* is not this list and keeps its comment."""
    guard = SqlGuard(default_row_limit=5000, hard_row_limit=100_000)
    sql = "SELECT o.order_id /* @@ */, o.qty FROM retail.orders AS o WHERE o.order_id > 7 -- @@"

    result = guard.validate(sql.replace("@@", SENTINEL), dialect=dialect)

    assert result.valid, result.violations
    assert result.referenced_columns == ("o.order_id", "o.qty")
    assert result.normalized_sql is not None and SENTINEL in result.normalized_sql


# ---------------------------------------------------------------------------
# D. The renderer names its refusals, and the workspace reads the names
# ---------------------------------------------------------------------------

_TEMPLATE = "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = :order_id"

#: Every code the renderer has, and what the workspace tells a person for it. Deliberately whole:
#: a code added to the renderer is missing from this table until someone decides what it should say.
WORKSPACE_CODE = {
    ToolParameterCode.UNCLASSIFIED: PARAMETER_INVALID,
    ToolParameterCode.UNDECLARED_PLACEHOLDER: PARAMETER_UNDECLARED,
    ToolParameterCode.UNUSED_DEFINITION: PARAMETER_UNUSED,
    ToolParameterCode.UNKNOWN_PARAMETER: PARAMETER_INVALID,
    ToolParameterCode.REQUIRED_MISSING: PARAMETER_INVALID,
    ToolParameterCode.REQUIRED_NULL: PARAMETER_VALUE_MISSING,
    ToolParameterCode.NOT_A_STRING: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_AN_INTEGER: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_NUMERIC: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_FINITE: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_A_BOOLEAN: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_AN_ISO_DATE: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.TOO_LONG: PARAMETER_TOO_LONG,
    ToolParameterCode.UNSUPPORTED_TYPE: PARAMETER_INVALID,
    ToolParameterCode.NOT_ALLOWED: PARAMETER_INVALID,
    ToolParameterCode.BELOW_MINIMUM: PARAMETER_INVALID,
    ToolParameterCode.ABOVE_MAXIMUM: PARAMETER_INVALID,
}


def _renderer_raising(
    monkeypatch: pytest.MonkeyPatch, refusal: ToolParameterError
) -> list[tuple[str, str | None]]:
    """What the workspace reports when the renderer refuses with `refusal`."""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise refusal

    monkeypatch.setattr("aida.sql_workspace.render_tool_sql", _raise)
    binding = bind_parameters(
        _TEMPLATE, dialect="postgres", parameters=[DraftParameter("order_id", "STRING", "O-1")]
    )
    assert binding.executable_sql is None
    return [(finding.code, finding.ref) for finding in binding.findings]


def test_the_table_names_every_code_the_renderer_has() -> None:
    assert set(WORKSPACE_CODE) == set(ToolParameterCode)


@pytest.mark.parametrize("code", list(ToolParameterCode), ids=lambda code: code.name)
def test_the_workspace_reads_each_code_and_names_the_parameter_only_where_it_says_something(
    monkeypatch: pytest.MonkeyPatch, code: ToolParameterCode
) -> None:
    """The message shares no words with any refusal the renderer ever made: only the code counts."""
    refusal = ToolParameterError(
        "words that mean nothing to the workspace", issues=[ToolParameterIssue(code, ("order_id",))]
    )

    findings = _renderer_raising(monkeypatch, refusal)

    expected = WORKSPACE_CODE[code]
    assert findings == [(expected, None if expected == PARAMETER_INVALID else "order_id")]


def test_the_workspace_does_not_read_the_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old bridge matched message phrases. A message in the old words with no code is not a
    type mismatch any more -- it is a refusal the workspace has no word for -- and nothing of it
    is echoed."""
    refusal = ToolParameterError("parameter must be an integer: order_id")

    assert _renderer_raising(monkeypatch, refusal) == [(PARAMETER_INVALID, None)]


def test_one_refusal_can_name_several_parameters_and_several_things(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = ToolParameterError.refusing(
        ToolParameterIssue(ToolParameterCode.UNDECLARED_PLACEHOLDER, ("alpha", "beta")),
        ToolParameterIssue(ToolParameterCode.UNUSED_DEFINITION, ("gamma",)),
    )

    assert _renderer_raising(monkeypatch, refusal) == [
        (PARAMETER_UNDECLARED, "alpha"),
        (PARAMETER_UNDECLARED, "beta"),
        (PARAMETER_UNUSED, "gamma"),
    ]


def test_a_name_that_is_not_a_parameter_name_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = ToolParameterError(
        "x", issues=[ToolParameterIssue(ToolParameterCode.NOT_AN_INTEGER, ("Not A Name; DROP",))]
    )

    assert _renderer_raising(monkeypatch, refusal) == [(PARAMETER_TYPE_MISMATCH, None)]


def test_the_real_renderers_refusals_reach_the_workspace_as_before() -> None:
    """End to end through the real renderer, no double: each reachable refusal's code and name."""
    cases = [
        ([DraftParameter("order_id", "STRING", None)], [(PARAMETER_VALUE_MISSING, "order_id")]),
        ([DraftParameter("order_id", "INTEGER", "x")], [(PARAMETER_TYPE_MISMATCH, "order_id")]),
        (
            [DraftParameter("order_id", "DATE", "2024-13-01")],
            [(PARAMETER_TYPE_MISMATCH, "order_id")],
        ),
        (
            [DraftParameter("order_id", "NUMBER", math.nan)],
            [(PARAMETER_TYPE_MISMATCH, "order_id")],
        ),
        (
            [DraftParameter("order_id", "STRING", "x" * (PARAMETER_VALUE_MAX_LENGTH + 1))],
            [(PARAMETER_TOO_LONG, "order_id")],
        ),
        (
            [DraftParameter("region", "STRING", "E")],
            [(PARAMETER_UNDECLARED, "order_id"), (PARAMETER_UNUSED, "region")],
        ),
    ]
    for parameters, expected in cases:
        binding = bind_parameters(_TEMPLATE, dialect="postgres", parameters=parameters)
        assert [(f.code, f.ref) for f in binding.findings] == expected, parameters
