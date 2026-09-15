"""INV-6 for routine bodies and statements sqlglot does not model node by node.

Until 2026-09-15 `redact_for_storage` labelled text `PARSED` whenever `parse_one` returned
without raising, then replaced `exp.Literal` nodes. sqlglot does not always put values in
one: a PostgreSQL/Snowflake `AS $$ ... $$` body is a `Heredoc` or a `Block`, an unreadable
statement is an opaque `Command`, a BigQuery JavaScript body a `RawString`. Every one of
those rendered its literals back verbatim -- so every dollar-quoted PostgreSQL routine a
scan stored carried its source values. These tests drive the shapes a real connector
hands over (`pg_get_functiondef` tags its bodies `$function$`/`$procedure$`) and require
both halves of the contract: the value is gone, and the program a parser needs survived.
"""

# Every SQL string below is a fixed sample handed to the redactor; none is executed.
# ruff: noqa: S608

from __future__ import annotations

from pathlib import Path

import pytest

from aida.dbt_artifacts import _redact_compiled_sql
from aida.envelope_models import AVAILABLE, MetadataRoutine
from aida.ingest_screening import CLEAN
from aida.routine_lineage_edges import RoutineNotEligibleError, require_eligible_routine_body
from aida.sql_redaction import (
    VALUE_FREE_REDACTION_STATUSES,
    contains_value_shaped_text,
    redact_for_storage,
    redact_sql_literals,
)

SENTINEL = "ZQ-SENTINEL-4471"
SENTINEL_NUMBER = "998877665544"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "database_footprint"


@pytest.mark.parametrize(
    ("label", "sql", "dialect", "survives"),
    [
        (
            "PL/pgSQL body as pg_get_functiondef returns it",
            "CREATE OR REPLACE PROCEDURE s.flag_account()\n LANGUAGE plpgsql\n"
            "AS $procedure$\nBEGIN\n"
            f"    UPDATE s.account SET flag = 1 WHERE ssn = '{SENTINEL}' "
            f"AND id = {SENTINEL_NUMBER};\nEND;\n$procedure$",
            "postgres",
            ["UPDATE s.account SET flag", "$procedure$", "LANGUAGE plpgsql"],
        ),
        (
            "SQL-language function body keeps its positional parameter",
            "CREATE OR REPLACE FUNCTION s.orders_for(p integer)\n RETURNS SETOF integer\n"
            " LANGUAGE sql\nAS $function$ SELECT o.id FROM s.orders o"
            f" WHERE o.status = '{SENTINEL}'"
            " AND o.customer_id = $1 $function$",
            "postgres",
            ["FROM s.orders o", "o.customer_id = $1"],
        ),
        (
            "nested dollar-quoted string inside a body is a value",
            "CREATE FUNCTION s.note() RETURNS void LANGUAGE plpgsql AS $body$\nBEGIN\n"
            f"    RAISE NOTICE $msg${SENTINEL}$msg$;\nEND;\n$body$",
            "postgres",
            ["RAISE NOTICE", "$body$"],
        ),
        (
            "E-string with a backslash-escaped quote does not swallow the next statement",
            "CREATE PROCEDURE s.p() LANGUAGE plpgsql AS $$\nBEGIN\n"
            f"    UPDATE s.a SET note = E'it\\'s {SENTINEL}';\n"
            "    DELETE FROM s.b WHERE flag = 0;\nEND;\n$$",
            "postgres",
            ["DELETE FROM s.b WHERE flag"],
        ),
        (
            "an apostrophe in a comment does not swallow code",
            "CREATE PROCEDURE s.p() LANGUAGE plpgsql AS $$\nBEGIN\n"
            "    -- don't remove this step\n"
            f"    INSERT INTO s.audit SELECT '{SENTINEL}', id FROM s.account;\nEND;\n$$",
            "postgres",
            ["INSERT INTO s.audit SELECT", "FROM s.account"],
        ),
        (
            "Snowflake SQL script body",
            "CREATE OR REPLACE PROCEDURE p() RETURNS VARCHAR LANGUAGE SQL AS $$ BEGIN "
            f"UPDATE t SET s = '{SENTINEL}' WHERE id = {SENTINEL_NUMBER}; RETURN 'ok'; END; $$",
            "snowflake",
            ["UPDATE t SET s", "RETURN"],
        ),
        (
            "Snowflake JavaScript body quoted as a string",
            "CREATE OR REPLACE PROCEDURE p() RETURNS VARCHAR LANGUAGE JAVASCRIPT AS "
            f"' var who = \"{SENTINEL}\"; return who; '",
            "snowflake",
            ["return who"],
        ),
        (
            "statement sqlglot keeps as an opaque Command",
            "CREATE TEMP TABLE t ON COMMIT DROP AS SELECT a FROM s.x "
            f"WHERE z = '{SENTINEL}' AND n = {SENTINEL_NUMBER}",
            "postgres",
            ["ON COMMIT DROP AS SELECT a FROM s.x"],
        ),
        (
            "BigQuery double-quoted string is a value",
            f'CREATE PROCEDURE ds.p() BEGIN UPDATE ds.t SET s = "{SENTINEL}" '
            f"WHERE id = {SENTINEL_NUMBER}; END",
            "bigquery",
            ["UPDATE ds.t SET s"],
        ),
    ],
)
def test_program_bodies_lose_their_values_and_keep_their_program(
    label: str, sql: str, dialect: str, survives: list[str]
) -> None:
    prepared = redact_for_storage(sql, dialect=dialect)

    assert prepared is not None and prepared.redacted is not None, label
    assert SENTINEL not in prepared.redacted, f"{label}: string value stored"
    assert SENTINEL_NUMBER not in prepared.redacted, f"{label}: numeric value stored"
    # These shapes all hide values from the node-level pass, so a PARSED label would
    # claim a precision this text does not have.
    assert prepared.status == "LEXICAL", label
    for fragment in survives:
        assert fragment in prepared.redacted, f"{label}: {fragment!r} did not survive"


@pytest.mark.parametrize(
    ("dialect", "path", "survives"),
    [
        ("tsql", "sqlserver/refresh.sql", "INTO #footprint_totals"),
        ("tsql", "sqlserver/view.sql", "SUM(o.amount - o.discount)"),
        ("postgres", "postgres/view.sql", "SUM(o.amount - o.discount)"),
    ],
)
def test_statements_the_parser_models_fully_stay_precise(
    dialect: str, path: str, survives: str
) -> None:
    prepared = redact_for_storage((FIXTURES / path).read_text(encoding="utf-8"), dialect=dialect)

    assert prepared is not None and prepared.redacted is not None
    assert prepared.status == "PARSED"
    assert survives in prepared.redacted


def test_a_modelled_literal_is_still_replaced_precisely() -> None:
    prepared = redact_for_storage(
        "CREATE OR REPLACE PROCEDURE p AS BEGIN "
        f"UPDATE t SET s = '{SENTINEL}' WHERE id = {SENTINEL_NUMBER}; END;",
        dialect="oracle",
    )

    assert prepared is not None and prepared.redacted is not None
    assert prepared.status == "PARSED"
    assert SENTINEL not in prepared.redacted and SENTINEL_NUMBER not in prepared.redacted


def test_digits_inside_identifiers_and_comments_are_not_counted_as_values() -> None:
    sql = 'SELECT "sales_2024".amount FROM "sales_2024" -- v2 of the report'

    assert contains_value_shaped_text(sql, dialect="postgres") is False
    prepared = redact_for_storage(sql, dialect="postgres")
    assert prepared is not None and prepared.status == "PARSED"
    assert '"sales_2024"' in (prepared.redacted or "")


def test_the_gateway_redaction_never_returns_command_text_verbatim() -> None:
    redacted = redact_sql_literals(
        f"CREATE TEMP TABLE t ON COMMIT DROP AS SELECT a FROM s WHERE z = '{SENTINEL}'",
        dialect="postgres",
    )

    assert SENTINEL not in redacted


def test_dbt_compiled_sql_that_hides_values_is_not_stored() -> None:
    fingerprint, redacted, status = _redact_compiled_sql(
        f"CREATE TEMP TABLE t ON COMMIT DROP AS SELECT a FROM s WHERE z = '{SENTINEL}'",
        "postgres",
    )

    assert fingerprint is not None
    assert redacted is None
    assert status == "UNPARSEABLE"
    _, plain, plain_status = _redact_compiled_sql(
        f"select id from analytics.orders where status = '{SENTINEL}'", "postgres"
    )
    assert plain_status == "PARSED"
    assert plain is not None and SENTINEL not in plain


def test_the_repair_rewrites_only_rows_that_still_hold_values() -> None:
    """`scripts/reredact_stored_sql.py`: rows stored before the fix carry sqlglot's rendering
    of the leak (`$$ ... 'value' ... $$`); correctly redacted rows must be left untouched."""
    from scripts.reredact_stored_sql import repaired_text

    leaked = (
        "CREATE PROCEDURE s.p()\nLANGUAGE plpgsql AS\n$$\nBEGIN\n"
        f"    UPDATE s.account SET flag = 1 WHERE ssn = '{SENTINEL}';\nEND;\n$$"
    )
    fixed = repaired_text(leaked, dialect="postgres")
    assert fixed is not None
    assert SENTINEL not in fixed
    assert "UPDATE s.account SET flag" in fixed

    correct = redact_for_storage(
        (FIXTURES / "sqlserver/refresh.sql").read_text(encoding="utf-8"), dialect="tsql"
    )
    assert correct is not None
    assert repaired_text(correct.redacted, dialect="tsql") is None
    assert repaired_text(fixed, dialect="postgres") is None


def _routine(redaction_status: str, body: str | None) -> MetadataRoutine:
    return MetadataRoutine(
        name="refresh_totals",
        signature="()",
        routine_type="PROCEDURE",
        status="ACTIVE",
        availability=AVAILABLE,
        redaction_status=redaction_status,
        screening_status=CLEAN,
        body_sql_redacted=body,
    )


def test_a_lexically_redacted_body_is_eligible_for_lineage() -> None:
    """The LEXICAL tier exists so that bodies sqlglot cannot read as one statement stay
    parseable; a gate that required PARSED would have dropped every PL/pgSQL routine."""
    assert VALUE_FREE_REDACTION_STATUSES == {"PARSED", "LEXICAL"}
    body = "CREATE PROCEDURE s.p() LANGUAGE plpgsql AS $$ BEGIN DELETE FROM s.t; END; $$"

    assert require_eligible_routine_body(_routine("LEXICAL", body)) == body


def test_a_body_that_could_not_be_stored_is_still_refused() -> None:
    with pytest.raises(RoutineNotEligibleError, match="not PARSED or LEXICAL"):
        require_eligible_routine_body(_routine("UNPARSED", None))
