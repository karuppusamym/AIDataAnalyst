"""FP-07: PL/pgSQL routines, in the form the PostgreSQL connector actually stores.

The database-footprint sample pack measured these gaps on 2026-09-14/15 against a live
PostgreSQL 17 source. Each was silent or wrong rather than merely incomplete:

* `CREATE TEMP TABLE t ON COMMIT DROP AS SELECT ...` fell back to an opaque Command, and a
  plain `CREATE TEMP TABLE t AS SELECT ...` parsed but was never treated as an
  intermediate, so no end-to-end edge reached the table the procedure really writes;
* `EXECUTE format(...)` and `EXECUTE v_sql` were reported as nested calls to procedures
  named `format` and `v_sql`;
* `PERFORM` and `RETURN QUERY` did not parse at all, so every set-returning PL/pgSQL
  function lost its result query;
* `SELECT ... INTO v` was recorded as a write to a table named `v`.

No mocking: real bodies through `parse_procedure_lineage`, including the tagged
`$function$`/`$procedure$` form `pg_get_functiondef` returns and the literal-redacted text
ingestion persists.
"""

from __future__ import annotations

import pytest

from aida.lineage_agent import proposable_procedure_edges
from aida.procedure_lineage import (
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    parse_procedure_lineage,
)
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    find_single_read_only_result_statement,
)
from aida.routine_lineage_edges import persistable_table
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES, redact_for_storage


def _plpgsql(body: str, *, header: str = "PROCEDURE s.refresh()") -> str:
    """A routine exactly as `pg_get_functiondef` renders it."""
    return (
        f"CREATE OR REPLACE {header}\n LANGUAGE plpgsql\n"
        f"AS $procedure$\nBEGIN\n{body}\nEND;\n$procedure$\n"
    )


def _unparsed_reasons(sql: str) -> list[str]:
    result = parse_procedure_lineage(sql, dialect="postgres")
    return [
        edge.unparsed_reason or ""
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


def test_a_tagged_sql_function_body_is_extracted() -> None:
    sql = (
        "CREATE OR REPLACE FUNCTION s.order_ids()\n RETURNS TABLE(id integer)\n LANGUAGE sql\n"
        "AS $function$ SELECT o.id FROM s.orders o; $function$\n"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed and result.is_read_only
    assert any(
        edge.source_table == "s.orders" and edge.target_table == PROCEDURE_RESULT_TARGET
        for edge in result.edges
    )


@pytest.mark.parametrize("on_commit", ["ON COMMIT DROP ", "ON COMMIT PRESERVE ROWS ", ""])
def test_a_temp_table_carries_lineage_through_to_the_table_written(on_commit: str) -> None:
    sql = _plpgsql(
        f"    CREATE TEMP TABLE totals {on_commit}AS\n"  # noqa: S608 -- fixed sample, never run
        "    SELECT o.customer_id, SUM(o.amount - o.discount) AS net FROM s.orders o\n"
        "    GROUP BY o.customer_id;\n"
        "    INSERT INTO s.customer_totals (customer_id, net)\n"
        "    SELECT t.customer_id, t.net FROM totals t;"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, result.errors
    assert result.is_read_only is False
    assert any(
        edge.source_table == "s.orders"
        and edge.source_column == "discount"
        and edge.target_table == "s.customer_totals"
        and edge.target_column == "net"
        and edge.via_temp_table == "totals"
        for edge in result.edges
    )
    assert all(edge.is_intermediate for edge in result.edges if edge.target_table == "totals")


@pytest.mark.parametrize(
    "statement",
    [
        "    EXECUTE 'INSERT INTO s.t SELECT a FROM s.u';",
        "    EXECUTE format('INSERT INTO %I SELECT 1', target_name);",
        "    EXECUTE v_sql;",
        "    EXECUTE 'DELETE FROM s.t WHERE id = $1' USING 5;",
        "    RETURN QUERY EXECUTE v_sql;",
    ],
)
def test_execute_is_dynamic_sql_never_a_call_named_after_its_argument(statement: str) -> None:
    reasons = _unparsed_reasons(_plpgsql(statement))

    assert len(reasons) == 1
    assert reasons[0].startswith("DYNAMIC_SQL")
    assert "NESTED_PROCEDURE_CALL" not in reasons[0]


def test_perform_of_a_function_is_a_nested_call_gap_naming_the_function() -> None:
    reasons = _unparsed_reasons(_plpgsql("    PERFORM s.publish_totals(run_id);"))

    assert reasons == ["NESTED_PROCEDURE_CALL: s.publish_totals"]


def test_perform_of_a_query_keeps_its_read_and_writes_nothing() -> None:
    sql = _plpgsql(
        "    PERFORM 1 FROM s.orders o WHERE o.customer_id = p_customer;\n"
        "    IF NOT FOUND THEN\n        RAISE EXCEPTION 'missing';\n    END IF;",
        header="FUNCTION s.require_orders(p_customer integer) RETURNS void",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, result.errors
    assert result.is_read_only
    reads = [edge for edge in result.edges if edge.source_table == "s.orders"]
    assert reads
    assert all(
        edge.target_table == PROCEDURE_LOCAL_TARGET and edge.is_intermediate and not edge.is_write
        for edge in reads
    )


def test_select_into_a_variable_is_not_a_write_to_a_table_named_after_it() -> None:
    sql = _plpgsql(
        "    SELECT o.amount INTO v_amount FROM s.orders o WHERE o.id = p_id;\n"
        "    SELECT count(a.id) INTO STRICT v_count FROM s.adjustments a;\n"
        "    v_total := (SELECT sum(r.amount) FROM s.refunds r);\n"
        "    GET DIAGNOSTICS v_rows = ROW_COUNT;\n"
        "    RETURN v_amount;",
        header="FUNCTION s.amount_for(p_id integer) RETURNS numeric",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, result.errors
    assert result.is_read_only
    assert {"v_amount", "v_count", "v_total"}.isdisjoint(e.target_table for e in result.edges)
    assert {"s.orders", "s.adjustments", "s.refunds"} <= {e.source_table for e in result.edges}


def test_returning_into_keeps_the_write() -> None:
    sql = _plpgsql(
        "    INSERT INTO s.runs (started_by) SELECT u.name FROM s.users u RETURNING id INTO v_run;"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, result.errors
    assert any(edge.target_table == "s.runs" and edge.is_write for edge in result.edges)


def test_return_query_is_the_routine_result_and_a_tool_candidate() -> None:
    sql = _plpgsql(
        "    RETURN QUERY SELECT r.customer_id, r.net_revenue FROM s.customer_revenue r;",
        header="FUNCTION s.read_revenue() RETURNS TABLE(customer_id integer, net_revenue numeric)",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed and result.is_read_only
    assert any(
        edge.source_table == "s.customer_revenue" and edge.target_table == PROCEDURE_RESULT_TARGET
        for edge in result.edges
    )
    node, _ = find_single_read_only_result_statement(sql, "postgres")
    assert "customer_revenue" in node.sql()


def test_a_query_into_local_state_is_never_taken_for_the_tool_result() -> None:
    sql = _plpgsql(
        "    SELECT o.amount INTO v_amount FROM s.orders o;\n    RETURN v_amount;",
        header="FUNCTION s.amount() RETURNS numeric",
    )

    with pytest.raises(ProcedureNotEligibleError, match="no standalone result"):
        find_single_read_only_result_statement(sql, "postgres")


def test_an_exception_handler_is_walked_not_reported_as_a_parse_error() -> None:
    sql = _plpgsql(
        "    UPDATE s.account a SET flag = 1 FROM s.flags f WHERE f.id = a.id;\n"
        "EXCEPTION WHEN unique_violation THEN\n"
        "    RAISE NOTICE 'duplicate';\n"
        "    WHEN others THEN\n"
        "    INSERT INTO s.failures (account_id) SELECT a.id FROM s.account a"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, result.errors
    assert any(
        edge.target_table == "s.failures" and edge.control_flow_context == "CASE_BRANCH"
        for edge in result.edges
    )


def test_a_for_loop_over_a_query_keeps_the_query_as_a_read() -> None:
    sql = _plpgsql(
        "    FOR rec IN SELECT o.customer_id, o.amount FROM s.orders o WHERE o.open LOOP\n"
        "        UPDATE s.balances b SET due = b.due + 1 WHERE b.customer_id = 0;\n"
        "    END LOOP;\n"
        "    FOR rec IN EXECUTE v_sql LOOP\n        NULL;\n    END LOOP;"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    # Extracted as its own read, exactly as a PL/SQL `FOR rec IN (SELECT ...) LOOP` is.
    assert any(edge.source_table == "s.orders" for edge in result.edges), (
        "the loop's query was discarded"
    )
    assert any(edge.target_table == "s.balances" for edge in result.edges)
    assert [r for r in _unparsed_reasons(sql)] == [
        "DYNAMIC_SQL: PL/pgSQL EXECUTE runs a string built at runtime"
    ]


def test_local_state_is_never_proposed_or_resolved_as_a_table() -> None:
    sql = _plpgsql(
        "    SELECT o.amount INTO v FROM s.orders o;\n"
        "    INSERT INTO s.totals (amount) SELECT o.amount FROM s.orders o;"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert persistable_table(PROCEDURE_LOCAL_TARGET, True) is None
    proposed = proposable_procedure_edges(result)
    assert proposed
    assert all(edge.target_table != PROCEDURE_LOCAL_TARGET for edge in proposed)


def test_the_literal_redacted_text_ingestion_stores_has_the_same_lineage() -> None:
    """The lineage agent never sees the raw body: it parses what ingestion stored."""
    raw = _plpgsql(
        "    CREATE TEMP TABLE totals ON COMMIT DROP AS\n"
        "    SELECT o.customer_id, o.amount FROM s.orders o WHERE o.status = 'POSTED';\n"
        "    INSERT INTO s.customer_totals (customer_id, amount)\n"
        "    SELECT t.customer_id, t.amount FROM totals t;"
    )
    stored = redact_for_storage(raw, dialect="postgres")

    assert stored is not None and stored.redacted is not None
    assert stored.status in VALUE_FREE_REDACTION_STATUSES
    assert "POSTED" not in stored.redacted

    def keys(sql: str) -> set[tuple[str, str, str, str, str | None]]:
        return {
            (e.source_table, e.source_column, e.target_table, e.target_column, e.via_temp_table)
            for e in parse_procedure_lineage(sql, dialect="postgres").edges
            if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE
        }

    assert keys(stored.redacted) == keys(raw)
    assert ("s.orders", "amount", "s.customer_totals", "amount", "totals") in keys(stored.redacted)


def test_tsql_keeps_its_own_meaning_for_exec_and_select_into() -> None:
    sql = (
        "CREATE PROCEDURE dbo.p AS BEGIN\n"
        "    SELECT o.amount INTO #base FROM dbo.orders o;\n"
        "    EXEC dbo.publish @run = 1;\n"
        "END"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert any(e.target_table == "base" and e.is_intermediate and e.is_write for e in result.edges)
    assert [e.unparsed_reason for e in result.edges if e.unparsed_reason] == [
        "NESTED_PROCEDURE_CALL: dbo.publish"
    ]
