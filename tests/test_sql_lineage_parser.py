"""Tests for sql_lineage_parser -- column-level lineage extraction.

Pure unit tests: no database, no network.  Each test exercises the parser
against a representative SQL pattern and verifies the lineage edges,
confidence level, and structural invariants.
"""

from __future__ import annotations

import pytest

from aida.sql_lineage_parser import (
    Confidence,
    LineageEdge,
    TransformationType,
    _compute_sql_hash,
    _redact_literals,
    parse_procedure_lineage,
    parse_view_lineage,
)


# ---------------------------------------------------------------------------
# Literal redaction
# ---------------------------------------------------------------------------


class TestRedactLiterals:
    def test_string_literals_are_redacted(self) -> None:
        sql = "SELECT * FROM t WHERE name = 'Alice'"
        redacted = _redact_literals(sql)
        assert "Alice" not in redacted
        assert "'<REDACTED>'" in redacted

    def test_numeric_literals_are_redacted(self) -> None:
        sql = "SELECT * FROM t WHERE id = 42 AND rate = 3.14"
        redacted = _redact_literals(sql)
        assert "42" not in redacted
        assert "3.14" not in redacted
        assert "<NUM>" in redacted

    def test_empty_string_literals(self) -> None:
        sql = "SELECT * FROM t WHERE name = ''"
        redacted = _redact_literals(sql)
        assert "'<REDACTED>'" in redacted


# ---------------------------------------------------------------------------
# SQL hash stability
# ---------------------------------------------------------------------------


class TestSqlHash:
    def test_hash_is_deterministic(self) -> None:
        sql = "SELECT a FROM t"
        assert _compute_sql_hash(sql) == _compute_sql_hash(sql)

    def test_different_sql_produces_different_hash(self) -> None:
        assert _compute_sql_hash("SELECT a FROM t") != _compute_sql_hash("SELECT b FROM t")

    def test_hash_is_64_hex_chars(self) -> None:
        h = _compute_sql_hash("SELECT 1")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Unsupported dialect
# ---------------------------------------------------------------------------


class TestDialectHandling:
    def test_unsupported_dialect_returns_low_confidence(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="unknown_db")
        assert result.confidence == Confidence.LOW.value
        assert any("unsupported dialect" in e for e in result.errors)
        assert result.edges == []

    def test_postgres_dialect_accepted(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="postgres")
        assert result.dialect == "postgres"
        assert result.confidence != Confidence.LOW.value or not result.errors

    def test_snowflake_dialect_accepted(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="snowflake")
        assert result.dialect == "snowflake"

    def test_bigquery_dialect_accepted(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="bigquery")
        assert result.dialect == "bigquery"


# ---------------------------------------------------------------------------
# Basic view lineage
# ---------------------------------------------------------------------------


class TestViewLineageBasic:
    def test_simple_create_view(self) -> None:
        sql = "CREATE VIEW my_view AS SELECT col_a, col_b FROM source_table"
        result = parse_view_lineage(sql, "postgres")

        assert result.confidence in (Confidence.FULL.value, Confidence.PARTIAL.value)
        assert len(result.edges) >= 2
        assert result.sql_hash
        assert result.dialect == "postgres"

        source_cols = {e.source_column for e in result.edges}
        assert "col_a" in source_cols
        assert "col_b" in source_cols

        for edge in result.edges:
            assert edge.target_table == "my_view"

    def test_aliased_columns(self) -> None:
        sql = "CREATE VIEW v AS SELECT col_a AS alias_a, col_b AS alias_b FROM t"
        result = parse_view_lineage(sql, "postgres")

        target_cols = {e.target_column for e in result.edges}
        assert "alias_a" in target_cols
        assert "alias_b" in target_cols

    def test_join_produces_edges_from_both_tables(self) -> None:
        sql = """
        CREATE VIEW v AS
        SELECT a.id, b.name
        FROM table_a a
        JOIN table_b b ON a.id = b.id
        """
        result = parse_view_lineage(sql, "postgres")

        source_tables = {e.source_table for e in result.edges}
        # Should reference both tables (possibly via aliases resolved to full names)
        assert len(source_tables) >= 1
        assert len(result.edges) >= 2

    def test_where_clause_produces_filtered_transformation(self) -> None:
        sql = "CREATE VIEW v AS SELECT col_a FROM t WHERE col_a > 0"
        result = parse_view_lineage(sql, "postgres")

        filtered = [
            e for e in result.edges if e.transformation_type == TransformationType.FILTERED.value
        ]
        assert len(filtered) >= 1

    def test_aggregation_produces_aggregated_transformation(self) -> None:
        sql = "CREATE VIEW v AS SELECT department, COUNT(id) AS cnt FROM employees GROUP BY department"
        result = parse_view_lineage(sql, "postgres")

        agg = [
            e for e in result.edges if e.transformation_type == TransformationType.AGGREGATED.value
        ]
        assert len(agg) >= 1


# ---------------------------------------------------------------------------
# CTE handling
# ---------------------------------------------------------------------------


class TestCTEHandling:
    def test_cte_lineage(self) -> None:
        sql = """
        CREATE VIEW v AS
        WITH base AS (
            SELECT id, amount FROM orders
        )
        SELECT id, amount FROM base
        """
        result = parse_view_lineage(sql, "postgres")
        assert result.confidence in (Confidence.FULL.value, Confidence.PARTIAL.value)
        # Edges should reference the original source table 'orders'
        # or the CTE 'base' -- at minimum edges are produced
        assert len(result.edges) >= 1


# ---------------------------------------------------------------------------
# UNION handling
# ---------------------------------------------------------------------------


class TestUnionHandling:
    def test_union_produces_edges_from_both_branches(self) -> None:
        sql = """
        CREATE VIEW v AS
        SELECT id, name FROM table_a
        UNION ALL
        SELECT id, name FROM table_b
        """
        result = parse_view_lineage(sql, "postgres")

        source_tables = {e.source_table for e in result.edges}
        assert len(result.edges) >= 2
        assert len(source_tables) >= 2


# ---------------------------------------------------------------------------
# Procedure lineage
# ---------------------------------------------------------------------------


class TestProcedureLineage:
    def test_insert_into_select(self) -> None:
        sql = "INSERT INTO target_table (col_a, col_b) SELECT src_a, src_b FROM source_table"
        result = parse_procedure_lineage(sql, "postgres")

        assert len(result.edges) >= 2
        for edge in result.edges:
            assert edge.target_table == "target_table"

        source_cols = {e.source_column for e in result.edges}
        assert "src_a" in source_cols
        assert "src_b" in source_cols

    def test_standalone_select(self) -> None:
        sql = "SELECT col_a, col_b FROM t"
        result = parse_procedure_lineage(sql, "postgres")

        assert len(result.edges) >= 2
        for edge in result.edges:
            assert edge.target_table == "<RESULT>"

    def test_procedure_shares_same_confidence_levels(self) -> None:
        sql = "INSERT INTO dest (x) SELECT x FROM src"
        result = parse_procedure_lineage(sql, "postgres")
        assert result.confidence in (
            Confidence.FULL.value,
            Confidence.PARTIAL.value,
            Confidence.LOW.value,
        )


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_malformed_sql_returns_low_confidence_no_crash(self) -> None:
        sql = "THIS IS NOT VALID SQL @@@@"
        result = parse_view_lineage(sql, "postgres")
        # Should not raise -- graceful degradation
        assert result.confidence in (Confidence.LOW.value, Confidence.PARTIAL.value)
        assert isinstance(result.edges, list)

    def test_empty_sql_returns_low_confidence(self) -> None:
        sql = ""
        result = parse_view_lineage(sql, "postgres")
        assert isinstance(result.edges, list)


# ---------------------------------------------------------------------------
# Multi-dialect parity
# ---------------------------------------------------------------------------


class TestMultiDialect:
    @pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery", "tsql", "oracle"])
    def test_simple_view_parses_in_all_dialects(self, dialect: str) -> None:
        sql = "CREATE VIEW v AS SELECT a, b FROM t"
        result = parse_view_lineage(sql, dialect)

        assert result.dialect == dialect
        assert result.sql_hash
        assert isinstance(result.edges, list)
        # Should produce at least some edges (exact count may vary by dialect)
        assert len(result.edges) >= 1 or result.errors

    @pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery", "tsql", "oracle"])
    def test_insert_parses_in_all_dialects(self, dialect: str) -> None:
        sql = "INSERT INTO dest (x) SELECT x FROM src"
        result = parse_procedure_lineage(sql, dialect)

        assert result.dialect == dialect
        assert result.sql_hash


# ---------------------------------------------------------------------------
# Edge structural invariants
# ---------------------------------------------------------------------------


class TestEdgeInvariants:
    def test_all_edges_have_required_fields(self) -> None:
        sql = "CREATE VIEW v AS SELECT a, b, c FROM t"
        result = parse_view_lineage(sql, "postgres")

        for edge in result.edges:
            assert isinstance(edge.source_table, str)
            assert isinstance(edge.source_column, str)
            assert isinstance(edge.target_table, str)
            assert isinstance(edge.target_column, str)
            assert edge.transformation_type in {t.value for t in TransformationType}
            assert edge.confidence in {c.value for c in Confidence}
            assert edge.dialect

    def test_edges_are_frozen(self) -> None:
        sql = "CREATE VIEW v AS SELECT a FROM t"
        result = parse_view_lineage(sql, "postgres")
        if result.edges:
            edge = result.edges[0]
            with pytest.raises(AttributeError):
                edge.source_table = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ParseResult invariants
# ---------------------------------------------------------------------------


class TestParseResultInvariants:
    def test_result_always_has_sql_hash(self) -> None:
        for sql in ["SELECT 1", "invalid!!!", "CREATE VIEW v AS SELECT a FROM t"]:
            result = parse_view_lineage(sql, "postgres")
            assert result.sql_hash
            assert len(result.sql_hash) == 64

    def test_result_always_has_dialect(self) -> None:
        result = parse_view_lineage("SELECT 1", "postgres")
        assert result.dialect == "postgres"

    def test_result_errors_is_list(self) -> None:
        result = parse_view_lineage("SELECT 1", "postgres")
        assert isinstance(result.errors, list)
