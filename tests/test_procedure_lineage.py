"""N3 exit-condition tests: procedure-aware SQL lineage extraction.

No mocking of sqlglot or the parser internals -- every test feeds real
T-SQL/PL-SQL procedure bodies through `parse_procedure_lineage` and asserts
on the real edges it produces. Covers: AT-D5's own named gap (a `CREATE
PROCEDURE ... BEGIN ... END` body, not a flat DML sequence), control-flow
(IF/WHILE/CASE/cursor FOR loop), multi-statement bodies with temp-table
intermediate writes flowing to a final output table, INSERT ... SELECT
(including an explicit column list), UPDATE ... FROM, MERGE, and --the core
differentiator invariant (INV-9/AT-C4)-- dynamic SQL and other unresolvable
constructs always producing an explicit UNPARSED marker edge, never a
silent drop.
"""

from __future__ import annotations

from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    parse_procedure_lineage,
)

# ---------------------------------------------------------------------------
# AT-D5's own named gap: a real CREATE PROCEDURE ... BEGIN ... END body with
# control flow must not silently truncate.
# ---------------------------------------------------------------------------


def test_tsql_procedure_with_control_flow_is_not_silently_truncated() -> None:
    # sql_lineage_parser.parse_procedure_lineage (AT-D5) hands this whole
    # body to a single sqlglot.parse call, which falls back to an opaque
    # Command node at the first unsupported token (the IF/BEGIN) and drops
    # everything after it -- proven directly against that module below.
    sql = """
    CREATE PROCEDURE dbo.usp_refresh_customer_totals
    AS
    BEGIN
        SET NOCOUNT ON;
        DECLARE @cutoff DATE = GETDATE();

        SELECT c.customer_id, SUM(o.amount) AS total_amount
        INTO #tmp_totals
        FROM dbo.orders o
        JOIN dbo.customers c ON c.customer_id = o.customer_id
        WHERE o.order_date <= @cutoff
        GROUP BY c.customer_id;

        IF @cutoff IS NOT NULL
        BEGIN
            UPDATE t
            SET t.total_amount = s.total_amount
            FROM dbo.customer_totals t
            JOIN #tmp_totals s ON s.customer_id = t.customer_id;
        END

        INSERT INTO dbo.customer_totals_archive (customer_id, total_amount)
        SELECT customer_id, total_amount FROM #tmp_totals;
    END
    """
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert result.statement_count >= 4
    assert result.is_fully_parsed is True
    assert result.is_read_only is False  # it writes

    # The temp-table write is real, resolved, column-level lineage.
    temp_write_edges = [
        e for e in result.edges if e.target_table == "tmp_totals" and e.via_temp_table is None
    ]
    assert {(e.source_table, e.source_column, e.target_column) for e in temp_write_edges} >= {
        ("dbo.customers", "customer_id", "customer_id"),
        ("dbo.orders", "amount", "total_amount"),
    }

    # The IF-branch UPDATE is real, resolved, and tagged with its
    # control-flow context -- proving control flow does not stop extraction.
    update_edges = [e for e in result.edges if e.target_table == "dbo.customer_totals"]
    assert update_edges
    assert all(
        e.control_flow_context == "IF_BRANCH" for e in update_edges if e.via_temp_table is None
    )

    # The final INSERT ... SELECT with an explicit column list maps
    # positionally (customer_id<-customer_id, total_amount<-total_amount),
    # not by coincidence of matching names.
    archive_edges = {
        (e.source_column, e.target_column)
        for e in result.edges
        if e.target_table == "dbo.customer_totals_archive"
    }
    assert ("customer_id", "customer_id") in archive_edges
    assert ("total_amount", "total_amount") in archive_edges

    # Transitive hop propagation: the archive table's total_amount
    # ultimately traces back through #tmp_totals to dbo.orders.amount.
    transitive = [
        e for e in result.edges
        if e.target_table == "dbo.customer_totals" and e.via_temp_table == "tmp_totals"
    ]
    assert any(e.source_table == "dbo.orders" and e.source_column == "amount" for e in transitive)


def test_the_generic_flat_parser_silently_truncates_the_same_body() -> None:
    """Pins AT-D5's exact defect so a future change to the generic parser
    that accidentally "fixes" this without anyone noticing doesn't silently
    make this module's own docstring claims stale."""
    from aida.sql_lineage_parser import parse_procedure_lineage as generic_parse

    sql = """
    CREATE PROCEDURE dbo.usp_x AS
    BEGIN
        IF 1 = 1
        BEGIN
            INSERT INTO dbo.t (a) SELECT a FROM dbo.s;
        END
    END
    """
    result = generic_parse(sql, dialect="tsql")
    assert result.edges == []  # the INSERT is invisible to the generic parser


# ---------------------------------------------------------------------------
# INV-9/AT-C4: dynamic SQL and other unresolvable constructs always produce
# an explicit UNPARSED marker edge -- never a silent drop.
# ---------------------------------------------------------------------------


def test_dynamic_sql_exec_produces_an_explicit_unparsed_marker() -> None:
    sql = """
    CREATE PROCEDURE dbo.usp_dynamic AS
    BEGIN
        SELECT a FROM t;
        EXEC(@dynamic_sql);
    END
    """
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert result.is_fully_parsed is False
    unparsed_edges = [
        e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]
    assert len(unparsed_edges) == 1
    assert unparsed_edges[0].source_resolved is False
    assert unparsed_edges[0].unparsed_reason is not None
    assert "DYNAMIC_SQL" in unparsed_edges[0].unparsed_reason
    assert any(reason and "DYNAMIC_SQL" in reason for reason in result.errors)


def test_sp_executesql_produces_an_explicit_unparsed_marker() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN EXEC sp_executesql @sql; END"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is False
    assert any(
        e.transformation_type == UNPARSED_TRANSFORMATION_TYPE
        and e.unparsed_reason is not None
        and "DYNAMIC_SQL" in e.unparsed_reason
        for e in result.edges
    )


def test_plsql_execute_immediate_produces_an_explicit_unparsed_marker() -> None:
    sql = """
    CREATE OR REPLACE PROCEDURE refresh AS
    BEGIN
        EXECUTE IMMEDIATE 'TRUNCATE TABLE stg';
    END;
    """
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert result.is_fully_parsed is False
    assert any(
        e.transformation_type == UNPARSED_TRANSFORMATION_TYPE
        and e.unparsed_reason is not None
        and "DYNAMIC_SQL" in e.unparsed_reason
        for e in result.edges
    )


def test_nested_procedure_call_produces_an_explicit_unparsed_marker_naming_the_callee() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN EXEC dbo.other_proc @a, @b; END"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is False
    unparsed = [e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    assert len(unparsed) == 1
    assert unparsed[0].unparsed_reason is not None
    assert "NESTED_PROCEDURE_CALL" in unparsed[0].unparsed_reason
    assert "other_proc" in unparsed[0].unparsed_reason


def test_a_statement_shape_sqlglot_cannot_parse_at_all_is_unparsed_not_dropped() -> None:
    # A statement shape neither this module's control-flow peeling nor
    # sqlglot's own grammar recognises at all (sqlglot falls back to its own
    # Command node) -- proving the parser never mistakes "sqlglot gave up"
    # for "there was nothing here".
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT t.a FROM t; THIS IS NOT SQL AT ALL; END"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is False
    assert any(e.transformation_type == UNPARSED_TRANSFORMATION_TYPE for e in result.edges)
    # The real statement before the garbage is still extracted.
    assert any(e.source_table == "t" and e.source_column == "a" for e in result.edges)


# ---------------------------------------------------------------------------
# N12's exact eligibility shape: a genuinely read-only procedure parses
# fully with zero writes and is honestly marked read-only.
# ---------------------------------------------------------------------------


def test_pure_read_only_procedure_is_marked_fully_parsed_and_read_only() -> None:
    sql = """
    CREATE PROCEDURE dbo.usp_customer_orders_report
        @start_date DATE,
        @end_date DATE
    AS
    BEGIN
        SELECT c.customer_id, c.name, SUM(o.amount) AS total_amount
        FROM dbo.orders o
        JOIN dbo.customers c ON c.customer_id = o.customer_id
        WHERE o.order_date BETWEEN @start_date AND @end_date
        GROUP BY c.customer_id, c.name;
    END
    """
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is True
    assert result.is_read_only is True
    # PARTIAL, not FULL: the WHERE-clause filter evidence is honestly
    # PARTIAL confidence even though every SELECT-list column resolved
    # cleanly (AT-D2's per-edge confidence rollup, reused as-is).
    assert result.confidence == "PARTIAL"
    columns = {(e.source_table, e.source_column, e.transformation_type) for e in result.edges}
    assert ("dbo.customers", "customer_id", "DIRECT") in columns
    assert ("dbo.orders", "amount", "AGGREGATED") in columns


def test_a_procedure_that_gave_up_parsing_is_never_mistaken_for_read_only() -> None:
    """The exact invariant N12 depends on: "no write statement found"
    because the parser gave up must never look identical to a proven
    read-only procedure."""
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a FROM t; EXEC(@dynamic_sql); END"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is False
    # not fully parsed => never read-only, regardless of writes
    assert result.is_read_only is False
    assert not any(
        e.is_write for e in result.edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE
    )


def test_a_procedure_with_a_real_write_is_never_read_only() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN INSERT INTO t (a) SELECT a FROM s; END"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is True
    assert result.is_read_only is False


# ---------------------------------------------------------------------------
# PL/SQL: MERGE (column-level per WHEN branch), a cursor FOR loop's own
# SELECT extracted as a real read, and CASE-statement peeling.
# ---------------------------------------------------------------------------


def test_plsql_merge_produces_column_level_edges_for_both_when_branches() -> None:
    sql = """
    CREATE OR REPLACE PROCEDURE refresh_customer_totals AS
    BEGIN
      MERGE INTO customer_totals_archive a
      USING stg_customer_totals s
      ON (a.customer_id = s.customer_id)
      WHEN MATCHED THEN UPDATE SET a.total_amount = s.total_amount
      WHEN NOT MATCHED THEN INSERT (customer_id, total_amount)
        VALUES (s.customer_id, s.total_amount);
    END;
    """
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert result.is_fully_parsed is True
    assert result.is_read_only is False
    edges = {(e.source_table, e.source_column, e.target_column) for e in result.edges}
    assert ("stg_customer_totals", "total_amount", "total_amount") in edges  # WHEN MATCHED
    assert ("stg_customer_totals", "customer_id", "customer_id") in edges  # WHEN NOT MATCHED
    assert all(e.target_table == "customer_totals_archive" for e in result.edges)
    assert all(e.is_write for e in result.edges)


def test_plsql_cursor_for_loop_select_source_is_extracted_as_a_real_read() -> None:
    sql = """
    CREATE OR REPLACE PROCEDURE walk_active_customers AS
    BEGIN
      FOR rec IN (SELECT c.customer_id, c.name FROM customers c WHERE c.active = 1) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.customer_id);
      END LOOP;
    END;
    """
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert result.is_fully_parsed is True
    assert result.is_read_only is True  # the loop body's DBMS_OUTPUT call carries no table lineage
    edges = {(e.source_table, e.source_column, e.transformation_type) for e in result.edges}
    assert ("customers", "customer_id", "DIRECT") in edges
    assert ("customers", "name", "DIRECT") in edges
    assert ("customers", "active", "FILTERED") in edges


def test_plsql_if_elsif_else_branches_are_all_walked() -> None:
    sql = """
    CREATE OR REPLACE PROCEDURE classify AS
    BEGIN
      IF 1 = 1 THEN
        INSERT INTO high (id) SELECT id FROM src;
      ELSIF 2 = 2 THEN
        INSERT INTO medium (id) SELECT id FROM src;
      ELSE
        INSERT INTO low (id) SELECT id FROM src;
      END IF;
    END;
    """
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert result.is_fully_parsed is True
    targets = {e.target_table for e in result.edges}
    assert targets == {"high", "medium", "low"}


# ---------------------------------------------------------------------------
# UPDATE ... FROM (T-SQL alias-target shape) and DELETE.
# ---------------------------------------------------------------------------


def test_tsql_update_from_resolves_the_alias_target_through_the_from_clause() -> None:
    sql = (
        "CREATE PROCEDURE dbo.usp_x AS BEGIN "
        "UPDATE t SET t.total = s.total FROM dbo.totals t JOIN dbo.staging s "
        "ON s.id = t.id WHERE s.ready = 1; END"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is True
    edges = {
        (e.source_table, e.source_column, e.target_table, e.target_column) for e in result.edges
    }
    assert ("dbo.staging", "total", "dbo.totals", "total") in edges
    filtered = [e for e in result.edges if e.transformation_type == "FILTERED"]
    assert any(e.source_table == "dbo.staging" and e.source_column == "ready" for e in filtered)


def test_delete_is_a_write_even_with_no_column_level_edges() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN DELETE FROM dbo.stg WHERE loaded = 1; END"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is True
    assert result.is_read_only is False
    write_statements_present = any(e.is_write for e in result.edges)
    assert write_statements_present  # the FILTERED evidence edge is itself is_write=True
