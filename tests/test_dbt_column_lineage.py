"""Tests for dbt_column_lineage -- column-level dbt manifest lineage (LN-5).

Pure unit tests: no database, no network. Each test builds a
`ParsedDbtResource` (the same value-safe object `dbt_artifacts.parse_dbt_manifest`
produces) plus a small list of `DependencyResource` siblings, and exercises
`extract_column_lineage` directly.

NOTE ON FIXTURE SQL: `sql_lineage_parser` (LN-2; its table-resolution logic
is unchanged by AT-D2) attributes a selected column to a source table only
when the column reference is qualified by a table name or alias in the SQL
-- an unqualified column in a single-table `SELECT` cannot be resolved to
the sole table in the `FROM` clause, so the edge comes back with
`source_resolved=False` (displayed as the cosmetic `UNRESOLVED_TABLE`
label, never a magic string a real table name could collide with -- see
AT-D2). Guessing it back to "the one table in scope" would risk
mis-attributing lineage whenever a resource's compiled SQL happens not to
reference one of its declared dependencies at all, so
`extract_column_lineage` deliberately never does that (same "don't
fabricate an edge to an unresolved reference" rule as everywhere else in
this module). Every fixture below therefore qualifies its columns, exactly
as dbt's own compiled SQL commonly does for joins and as this feature is
designed to consume.
"""

from __future__ import annotations

from aida.dbt_artifacts import ParsedDbtResource
from aida.dbt_column_lineage import (
    MAX_COLUMN_EDGES_PER_RESOURCE,
    DependencyResource,
    extract_column_lineage,
)


def _resource(
    *,
    unique_id: str = "model.bank.customer_summary",
    compiled_sql_redacted: str | None,
    sql_parse_status: str = "PARSED",
    depends_on_unique_ids: list[str] | None = None,
) -> ParsedDbtResource:
    return ParsedDbtResource(
        unique_id=unique_id,
        resource_type="MODEL",
        package_name="bank",
        name="customer_summary",
        database_name="bank",
        schema_name="analytics",
        relation_name='"bank"."analytics"."customer_summary"',
        materialization="table",
        original_file_path="models/customer_summary.sql",
        description=None,
        compiled_sql_hash="deadbeef",
        compiled_sql_redacted=compiled_sql_redacted,
        sql_parse_status=sql_parse_status,
        column_names=["customer_id", "name"],
        tags=[],
        depends_on_unique_ids=depends_on_unique_ids or ["model.bank.stg_customers"],
    )


def _stg_customers_dependency(**overrides: object) -> DependencyResource:
    fields: dict[str, object] = {
        "unique_id": "model.bank.stg_customers",
        "relation_name": '"analytics"."staging"."stg_customers"',
        "database_name": "analytics",
        "schema_name": "staging",
        "name": "stg_customers",
    }
    fields.update(overrides)
    return DependencyResource(**fields)  # type: ignore[arg-type]


class TestResolvableReference:
    def test_relation_name_match_produces_column_edges_with_passthrough(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT c.id AS customer_id, c.name "
                "FROM analytics.staging.stg_customers AS c"
            )
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 2
        by_target = {edge.target_column: edge for edge in edges}
        assert by_target["customer_id"].source_column == "id"
        assert by_target["customer_id"].source_unique_id == dependency.unique_id
        assert by_target["customer_id"].transformation_type == "DIRECT"
        assert by_target["customer_id"].confidence == "FULL"
        assert by_target["name"].source_column == "name"
        assert by_target["name"].source_unique_id == dependency.unique_id

    def test_bare_name_match_when_relation_name_and_composite_are_absent(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT stg_customers.id AS customer_id FROM stg_customers"
            )
        )
        dependency = _stg_customers_dependency(
            relation_name=None, database_name=None, schema_name=None
        )

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 1
        assert edges[0].source_unique_id == dependency.unique_id
        assert edges[0].source_column == "id"
        assert edges[0].target_column == "customer_id"

    def test_composite_key_matches_when_relation_name_absent(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT c.id AS customer_id "
                "FROM analytics.staging.stg_customers AS c"
            )
        )
        dependency = _stg_customers_dependency(relation_name=None)

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 1
        assert edges[0].source_unique_id == dependency.unique_id

    def test_match_is_case_insensitive_and_quote_insensitive(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                'SELECT c.id AS customer_id '
                'FROM "ANALYTICS"."STAGING"."STG_CUSTOMERS" AS c'
            )
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 1
        assert edges[0].source_unique_id == dependency.unique_id


class TestUnresolvableReferenceIsDropped:
    def test_reference_outside_declared_dependencies_produces_no_edge(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT o.id FROM external_system.other_table AS o",
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert edges == []

    def test_unqualified_column_in_single_table_select_is_not_guessed(self) -> None:
        # sql_lineage_parser cannot attribute an unqualified column to its
        # source table on its own (see module docstring); extract_column_lineage
        # must not paper over that by assuming "the only declared dependency".
        resource = _resource(
            compiled_sql_redacted="SELECT id AS customer_id FROM analytics.staging.stg_customers",
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert edges == []

    def test_no_dependencies_at_all_produces_no_edges(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT c.id FROM analytics.staging.stg_customers AS c",
            depends_on_unique_ids=[],
        )

        edges = extract_column_lineage(resource, [], "postgres")

        assert edges == []


class TestUnparsedResourceProducesNoEdges:
    def test_unparseable_status_short_circuits(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT c.id FROM analytics.staging.stg_customers AS c",
            sql_parse_status="UNPARSEABLE",
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert edges == []

    def test_not_present_status_short_circuits(self) -> None:
        resource = _resource(compiled_sql_redacted=None, sql_parse_status="NOT_PRESENT")

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert edges == []

    def test_too_large_status_short_circuits(self) -> None:
        resource = _resource(compiled_sql_redacted=None, sql_parse_status="TOO_LARGE")

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert edges == []


class TestStarExpansionIsSkipped:
    def test_select_star_produces_no_fabricated_edge(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT * FROM analytics.staging.stg_customers AS c",
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        # sql_lineage_parser itself no longer silently drops a `SELECT *` --
        # it records honest table-level `TABLE_STAR` evidence (AT-D2). That
        # evidence still produces no *column-level* edge here: it is not a
        # column-to-column dependency, so extract_column_lineage filters it
        # out rather than fabricating one (see its module docstring).
        assert edges == []

    def test_mixed_star_and_explicit_columns_only_emits_the_explicit_one(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT *, c.id AS customer_id FROM analytics.staging.stg_customers AS c"
            )
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert len(edges) == 1
        assert edges[0].target_column == "customer_id"


class TestBounding:
    def test_edges_are_capped_per_resource(self) -> None:
        columns = ", ".join(f"c.col_{i}" for i in range(MAX_COLUMN_EDGES_PER_RESOURCE + 50))
        resource = _resource(
            compiled_sql_redacted=(
                f"SELECT {columns} FROM analytics.staging.stg_customers AS c"  # noqa: S608
            )
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert len(edges) == MAX_COLUMN_EDGES_PER_RESOURCE
