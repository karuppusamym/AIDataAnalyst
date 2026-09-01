import pytest

from aida.connectors.base import DiscoveredRoutineParameter
from aida.connectors.oracle import (
    OracleConnector,
    _assemble_catalog,
    _build_routine_body,
    _build_view_definition,
    _normalize_argument_mode,
    _OracleEnvelopeRows,
    _parse_dsn,
    _profile_expressions,
    _quote_identifier,
)
from aida.connectors.registry import connector_registry

_VALID_DSN = "oracle://reader:s3cr3t@warehouse.internal:1521/FREEPDB1"


def test_registry_exposes_oracle_connector() -> None:
    assert "oracle" in connector_registry.supported_types
    definition = connector_registry.definition("oracle")
    assert definition.implementation_status == "IMPLEMENTED"
    assert definition.dialect == "oracle"
    assert definition.capabilities["constraints"] is True
    assert definition.capabilities["explain"] is False


def test_oracle_connector_capabilities_are_honest() -> None:
    connector = OracleConnector(_VALID_DSN)
    capabilities = connector.capabilities
    assert capabilities.constraints is True
    assert capabilities.approximate_statistics is True
    assert capabilities.explain is False


def test_quote_identifier_escapes_double_quote() -> None:
    assert _quote_identifier("plain") == '"plain"'
    assert _quote_identifier('odd"name') == '"odd""name"'


def test_parse_dsn_extracts_connection_parameters() -> None:
    params = _parse_dsn(_VALID_DSN)
    assert params.host == "warehouse.internal"
    assert params.port == 1521
    assert params.service_name == "FREEPDB1"
    assert params.user == "reader"
    assert params.password == "s3cr3t"  # noqa: S105 -- test fixture value, not a real credential


def test_parse_dsn_defaults_port_when_absent() -> None:
    params = _parse_dsn("oracle://reader:s3cr3t@warehouse.internal/FREEPDB1")
    assert params.port == 1521


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader:s3cr3t@warehouse.internal:1521/FREEPDB1",
        "oracle://warehouse.internal:1521/FREEPDB1",
        "oracle://reader@warehouse.internal:1521/FREEPDB1",
        "oracle://reader:s3cr3t@warehouse.internal:1521/",
        "not-a-url-at-all",
    ],
)
def test_parse_dsn_rejects_invalid_references(dsn: str) -> None:
    with pytest.raises(ValueError, match="Oracle connection reference|invalid Oracle"):
        _parse_dsn(dsn)


def test_connector_construction_rejects_invalid_reference() -> None:
    with pytest.raises(ValueError):
        OracleConnector("unused")


def test_assemble_catalog_groups_columns_and_primary_key() -> None:
    column_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "CUSTOMER",
            "table_type": "TABLE",
            "column_name": "CUSTOMER_ID",
            "ordinal_position": 1,
            "data_type": "NUMBER",
            "is_nullable": "N",
            "column_default": None,
        },
        {
            "table_schema": "RETAIL",
            "table_name": "CUSTOMER",
            "table_type": "TABLE",
            "column_name": "CUSTOMER_NAME",
            "ordinal_position": 2,
            "data_type": "VARCHAR2",
            "is_nullable": "Y",
            "column_default": None,
        },
    ]
    key_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "CUSTOMER",
            "constraint_name": "PK_CUSTOMER",
            "constraint_type": "P",
            "column_name": "CUSTOMER_ID",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("BANK", column_rows, key_rows, [])

    assert len(catalogs) == 1
    schema = catalogs[0].schemas[0]
    assert schema.name == "RETAIL"
    table = schema.tables[0]
    assert table.name == "CUSTOMER"
    assert table.object_type == "TABLE"
    assert [column.name for column in table.columns] == ["CUSTOMER_ID", "CUSTOMER_NAME"]
    assert table.columns[0].nullable is False
    assert table.columns[1].nullable is True
    assert len(table.constraints) == 1
    constraint = table.constraints[0]
    assert constraint.constraint_type == "PRIMARY_KEY"
    assert constraint.columns == ("CUSTOMER_ID",)


def test_assemble_catalog_orders_foreign_key_columns_by_position() -> None:
    column_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "table_type": "TABLE",
            "column_name": "ACCOUNT_ID",
            "ordinal_position": 1,
            "data_type": "NUMBER",
            "is_nullable": "N",
            "column_default": None,
        },
    ]
    foreign_key_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "constraint_name": "FK_ACCOUNT_CUSTOMER",
            "referenced_schema": "RETAIL",
            "referenced_table": "CUSTOMER",
            "column_name": "CUSTOMER_ID",
            "referenced_column": "CUSTOMER_ID",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("BANK", column_rows, [], foreign_key_rows)

    table = catalogs[0].schemas[0].tables[0]
    assert len(table.constraints) == 1
    constraint = table.constraints[0]
    assert constraint.constraint_type == "FOREIGN_KEY"
    assert constraint.referenced_schema == "RETAIL"
    assert constraint.referenced_table == "CUSTOMER"
    assert constraint.columns == ("CUSTOMER_ID",)
    assert constraint.referenced_columns == ("CUSTOMER_ID",)


def test_profile_expressions_disable_distinct_and_lengths_for_unsupported_types() -> None:
    expressions = _profile_expressions('"PAYLOAD"', 2, "BLOB")
    assert any("CAST(0 AS NUMBER)" in expression for expression in expressions)
    assert any("CAST(NULL AS NUMBER)" in expression for expression in expressions)


# --- Envelope 1.1 (gap/02 N1) ------------------------------------------------
#
# Oracle hands three of the four axes back through LONG columns and through
# dictionary views a least-privilege reader may not hold. Every one of those refusals
# has a test here, because the failure this envelope exists to prevent is not "we got
# nothing" -- it is "we got nothing and it looked like an empty definition".


def _view_column_row(schema: str, table: str, table_type: str = "VIEW") -> dict[str, object]:
    return {
        "table_schema": schema,
        "table_name": table,
        "table_type": table_type,
        "column_name": "CUSTOMER_ID",
        "ordinal_position": 1,
        "data_type": "NUMBER",
        "is_nullable": "N",
        "column_default": None,
    }


def _single_table(catalogs: tuple[object, ...]) -> object:
    schema = catalogs[0].schemas[0]  # type: ignore[attr-defined]
    return schema.tables[0]


def test_oracle_capabilities_declare_every_implemented_envelope_axis() -> None:
    capabilities = OracleConnector(_VALID_DSN).capabilities
    assert capabilities.views is True
    assert capabilities.routines is True
    assert capabilities.object_comments is True
    assert capabilities.grants is True


def test_registry_advertises_the_envelope_axes_oracle_implements() -> None:
    advertised = connector_registry.definition("oracle").capabilities
    assert advertised["views"] is True
    assert advertised["routines"] is True
    assert advertised["object_comments"] is True
    assert advertised["grants"] is True


def test_view_definition_round_trips_from_all_views_text() -> None:
    definition_sql = "SELECT customer_id FROM retail.customer WHERE active = 'Y'"
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "ACTIVE_CUSTOMER")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            views=(
                {
                    "OWNER": "RETAIL",
                    "VIEW_NAME": "ACTIVE_CUSTOMER",
                    "TEXT": definition_sql,
                    "TEXT_LENGTH": len(definition_sql),
                },
            )
        ),
    )

    view = _single_table(catalogs).view_definition
    assert view is not None
    assert view.definition_sql == definition_sql
    assert view.truncated is False
    assert view.unavailable_reason is None
    assert view.is_materialized is False
    # Oracle's ALL_VIEWS carries neither column, so neither is guessed at.
    assert view.is_updatable is None
    assert view.check_option is None


def test_a_long_column_that_fetched_empty_is_unavailable_not_empty() -> None:
    """The LONG quirk this envelope exists to survive.

    ``ALL_VIEWS.TEXT`` is a LONG. A session that cannot materialise it gets an empty
    value back while ``TEXT_LENGTH`` still reports the real size. Recording that as
    ``definition_sql=""`` would tell view-DDL lineage the estate has a view defined by
    nothing, when in fact this extraction failed.
    """
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "ACTIVE_CUSTOMER")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            views=(
                {
                    "OWNER": "RETAIL",
                    "VIEW_NAME": "ACTIVE_CUSTOMER",
                    "TEXT": "",
                    "TEXT_LENGTH": 512,
                },
            )
        ),
    )

    view = _single_table(catalogs).view_definition
    assert view is not None
    assert view.definition_sql is None
    assert view.unavailable_reason is not None
    assert "512" in view.unavailable_reason


def test_a_null_view_text_is_unavailable_with_a_reason() -> None:
    definition = _build_view_definition(None, 0, object_label="RETAIL.V")
    assert definition.definition_sql is None
    assert definition.unavailable_reason is not None
    assert "NULL" in definition.unavailable_reason


def test_an_empty_view_text_with_zero_declared_length_stays_an_empty_definition() -> None:
    """The other half of the rule: empty and unavailable must not collapse together."""
    definition = _build_view_definition("", 0, object_label="RETAIL.V")
    assert definition.definition_sql == ""
    assert definition.unavailable_reason is None
    assert definition.truncated is False


def test_a_short_fetch_against_a_longer_declared_length_is_flagged_as_a_prefix() -> None:
    definition = _build_view_definition("SELECT 1 FROM", 4000, object_label="RETAIL.V")
    assert definition.definition_sql == "SELECT 1 FROM"
    assert definition.truncated is True


def test_a_definition_over_the_cap_is_stored_as_a_flagged_prefix() -> None:
    definition = _build_view_definition(
        "SELECT " + "x" * 50, None, object_label="RETAIL.V", max_characters=10
    )
    assert definition.definition_sql == "SELECT xxx"
    assert definition.truncated is True


def test_a_view_with_no_all_views_row_records_why_rather_than_nothing() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "ACTIVE_CUSTOMER")],
        [],
        [],
        envelope=_OracleEnvelopeRows(),
    )

    view = _single_table(catalogs).view_definition
    assert view is not None
    assert view.definition_sql is None
    assert view.unavailable_reason is not None
    assert "ALL_VIEWS" in view.unavailable_reason


def test_a_refused_all_views_query_lands_on_every_view_and_on_the_catalog() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "ACTIVE_CUSTOMER")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            unavailable=(("views", "DatabaseError: ORA-00942 table or view does not exist"),)
        ),
    )

    view = _single_table(catalogs).view_definition
    assert view is not None
    assert view.definition_sql is None
    assert view.unavailable_reason is not None
    assert "ORA-00942" in view.unavailable_reason
    assert catalogs[0].attributes["envelope_v11_unavailable"]["views"].startswith("DatabaseError")


def test_a_base_table_carries_no_view_definition() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(),
    )
    assert _single_table(catalogs).view_definition is None


def test_a_materialized_view_definition_comes_from_all_mviews_query() -> None:
    query = "SELECT customer_id, SUM(amount) FROM retail.payment GROUP BY customer_id"
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "PAYMENT_ROLLUP", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            materialized_views=(
                {
                    "OWNER": "RETAIL",
                    "MVIEW_NAME": "PAYMENT_ROLLUP",
                    "QUERY": query,
                    "QUERY_LEN": len(query),
                },
            )
        ),
    )

    view = _single_table(catalogs).view_definition
    assert view is not None
    assert view.is_materialized is True
    assert view.definition_sql == query


def test_a_routine_round_trips_with_its_parameters_and_return_type() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            routines=(
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "RISK_SCORE",
                    "OBJECT_TYPE": "FUNCTION",
                    "DETERMINISTIC": "YES",
                    "AUTHID": "CURRENT_USER",
                },
            ),
            routine_source=(
                {
                    "OWNER": "RETAIL",
                    "NAME": "RISK_SCORE",
                    "TYPE": "FUNCTION",
                    "LINE": 1,
                    "TEXT": "FUNCTION risk_score(p_id NUMBER) RETURN NUMBER IS\n",
                },
                {
                    "OWNER": "RETAIL",
                    "NAME": "RISK_SCORE",
                    "TYPE": "FUNCTION",
                    "LINE": 2,
                    "TEXT": "BEGIN RETURN p_id; END;\n",
                },
            ),
            arguments=(
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "RISK_SCORE",
                    "PACKAGE_NAME": None,
                    "ARGUMENT_NAME": None,
                    "POSITION": 0,
                    "DATA_TYPE": "NUMBER",
                    "IN_OUT": "OUT",
                },
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "RISK_SCORE",
                    "PACKAGE_NAME": None,
                    "ARGUMENT_NAME": "P_ID",
                    "POSITION": 1,
                    "DATA_TYPE": "NUMBER",
                    "IN_OUT": "IN",
                },
            ),
        ),
    )

    schema = catalogs[0].schemas[0]
    assert len(schema.routines) == 1
    routine = schema.routines[0]
    assert routine.name == "RISK_SCORE"
    assert routine.routine_type == "FUNCTION"
    assert routine.language == "PLSQL"
    assert routine.body_sql is not None
    assert routine.body_sql.startswith("FUNCTION risk_score")
    assert "BEGIN RETURN p_id; END;" in routine.body_sql
    assert routine.return_type == "NUMBER"
    assert routine.is_deterministic is True
    assert routine.security_mode == "INVOKER"
    assert routine.truncated is False
    assert routine.unavailable_reason is None
    assert routine.parameters == (
        DiscoveredRoutineParameter(
            name="P_ID", ordinal_position=1, mode="IN", physical_type="NUMBER"
        ),
    )


def test_a_packaged_subprograms_arguments_are_not_attached_to_the_package() -> None:
    """ALL_ARGUMENTS keys a packaged subprogram's arguments to the subprogram.

    Attaching them to the package object would invent a parameter list no PL/SQL
    caller could use, so the package says so instead.
    """
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            routines=(
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "RISK_PKG",
                    "OBJECT_TYPE": "PACKAGE",
                    "DETERMINISTIC": None,
                    "AUTHID": "DEFINER",
                },
            ),
            routine_source=(
                {
                    "OWNER": "RETAIL",
                    "NAME": "RISK_PKG",
                    "TYPE": "PACKAGE",
                    "LINE": 1,
                    "TEXT": "PACKAGE risk_pkg IS\n",
                },
                {
                    "OWNER": "RETAIL",
                    "NAME": "RISK_PKG",
                    "TYPE": "PACKAGE BODY",
                    "LINE": 1,
                    "TEXT": "PACKAGE BODY risk_pkg IS END;\n",
                },
            ),
            arguments=(
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "SCORE",
                    "PACKAGE_NAME": "RISK_PKG",
                    "ARGUMENT_NAME": "P_ID",
                    "POSITION": 1,
                    "DATA_TYPE": "NUMBER",
                    "IN_OUT": "IN",
                },
            ),
        ),
    )

    routine = catalogs[0].schemas[0].routines[0]
    assert routine.routine_type == "PACKAGE"
    assert routine.parameters == ()
    assert "packaged_subprogram_parameters" in routine.attributes
    assert routine.body_sql is not None
    # The spec and the body are both part of what procedure-body parsing has to read.
    assert "PACKAGE risk_pkg IS" in routine.body_sql
    assert "PACKAGE BODY risk_pkg IS END;" in routine.body_sql
    assert routine.security_mode == "DEFINER"


def test_a_routine_with_no_all_source_rows_records_why_rather_than_an_empty_body() -> None:
    body, truncated, reason = _build_routine_body((), object_label="RETAIL.RISK_SCORE")
    assert body is None
    assert truncated is False
    assert reason is not None
    assert "ALL_SOURCE" in reason


def test_wrapped_plsql_is_reported_as_unavailable_rather_than_as_a_body() -> None:
    """A wrapped body is present but obfuscated, which is not a parseable body.

    Handing the wrap blob to procedure-body parsing (N3) would produce a confident
    wrong answer; the envelope says the body could not be obtained instead.
    """
    wrapped = "PACKAGE BODY risk_pkg wrapped\na000000\nb2\nabcd\n"
    body, truncated, reason = _build_routine_body(
        (wrapped,), object_label="RETAIL.RISK_PKG"
    )
    assert body is None
    assert truncated is False
    assert reason is not None
    assert "wrapped" in reason


def test_a_wrapped_routine_is_flagged_on_the_assembled_routine() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            routines=(
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "RISK_PKG",
                    "OBJECT_TYPE": "PACKAGE",
                    "DETERMINISTIC": None,
                    "AUTHID": None,
                },
            ),
            routine_source=(
                {
                    "OWNER": "RETAIL",
                    "NAME": "RISK_PKG",
                    "TYPE": "PACKAGE BODY",
                    "LINE": 1,
                    "TEXT": "PACKAGE BODY risk_pkg wrapped\na000000\n",
                },
            ),
        ),
    )

    routine = catalogs[0].schemas[0].routines[0]
    assert routine.body_sql is None
    assert routine.attributes["wrapped"] is True
    assert routine.language is None


def test_a_body_over_the_cap_is_stored_as_a_flagged_prefix() -> None:
    body, truncated, reason = _build_routine_body(
        ("BEGIN " + "x" * 100,), object_label="RETAIL.P", max_characters=8
    )
    assert body == "BEGIN xx"
    assert truncated is True
    assert reason is None


def test_a_refused_all_source_query_replaces_the_per_object_reason() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            routines=(
                {
                    "OWNER": "RETAIL",
                    "OBJECT_NAME": "RISK_SCORE",
                    "OBJECT_TYPE": "FUNCTION",
                    "DETERMINISTIC": None,
                    "AUTHID": None,
                },
            ),
            unavailable=(("routine_source", "DatabaseError: ORA-01031 insufficient privileges"),),
        ),
    )

    routine = catalogs[0].schemas[0].routines[0]
    assert routine.body_sql is None
    assert routine.unavailable_reason is not None
    assert "ORA-01031" in routine.unavailable_reason


def test_table_and_column_comments_land_on_the_envelope() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            table_comments=(
                {
                    "OWNER": "RETAIL",
                    "TABLE_NAME": "CUSTOMER",
                    "COMMENTS": "Customer master",
                },
            ),
            column_comments=(
                {
                    "OWNER": "RETAIL",
                    "TABLE_NAME": "CUSTOMER",
                    "COLUMN_NAME": "CUSTOMER_ID",
                    "COMMENTS": "Surrogate key",
                },
            ),
        ),
    )

    table = _single_table(catalogs)
    assert table.source_description == "Customer master"
    assert table.columns[0].source_description == "Surrogate key"


def test_a_blank_comment_is_normalized_to_absent() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            table_comments=({"OWNER": "RETAIL", "TABLE_NAME": "CUSTOMER", "COMMENTS": "   "},)
        ),
    )
    assert _single_table(catalogs).source_description is None


def test_grants_land_on_the_schema_with_their_grantee_kind() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            grants=(
                {
                    "GRANTEE": "REPORTING_ROLE",
                    "TABLE_SCHEMA": "RETAIL",
                    "TABLE_NAME": "CUSTOMER",
                    "PRIVILEGE": "SELECT",
                    "GRANTABLE": "YES",
                    "OBJECT_TYPE": "TABLE",
                    "GRANTEE_TYPE": "ROLE",
                },
                {
                    "GRANTEE": "PUBLIC",
                    "TABLE_SCHEMA": "RETAIL",
                    "TABLE_NAME": "CUSTOMER",
                    "PRIVILEGE": "SELECT",
                    "GRANTABLE": "NO",
                    "OBJECT_TYPE": "TABLE",
                    "GRANTEE_TYPE": "PUBLIC",
                },
            )
        ),
    )

    grants = catalogs[0].schemas[0].grants
    assert len(grants) == 2
    assert grants[0].grantee == "REPORTING_ROLE"
    assert grants[0].grantee_type == "ROLE"
    assert grants[0].privilege == "SELECT"
    assert grants[0].schema_name == "RETAIL"
    assert grants[0].object_name == "CUSTOMER"
    assert grants[0].is_grantable is True
    assert grants[1].grantee_type == "PUBLIC"
    assert grants[1].is_grantable is False


def test_a_refused_grant_query_is_recorded_rather_than_read_as_no_grants() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [_view_column_row("RETAIL", "CUSTOMER", table_type="BASE TABLE")],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            unavailable=(("grants", "DatabaseError: ORA-00942 table or view does not exist"),)
        ),
    )

    assert catalogs[0].schemas[0].grants == ()
    recorded = catalogs[0].attributes["envelope_v11_unavailable"]
    assert "ORA-00942" in recorded["grants"]


def test_a_schema_known_only_through_a_routine_still_surfaces() -> None:
    """A schema holding only a stored package has no row in `ALL_TAB_COLUMNS`.

    `assemble_catalog` unions schema names across every 1.1 axis precisely for this
    case (INV-9); the schema must not silently vanish just because Oracle's routine
    inventory outruns its table inventory.
    """
    catalogs = _assemble_catalog(
        "BANK",
        [],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            routines=(
                {
                    "OWNER": "BATCH",
                    "OBJECT_NAME": "NIGHTLY_CLOSE",
                    "OBJECT_TYPE": "PROCEDURE",
                    "DETERMINISTIC": None,
                    "AUTHID": "DEFINER",
                },
            ),
        ),
    )

    assert len(catalogs[0].schemas) == 1
    schema = catalogs[0].schemas[0]
    assert schema.name == "BATCH"
    assert schema.tables == ()
    assert len(schema.routines) == 1
    assert schema.routines[0].name == "NIGHTLY_CLOSE"


def test_a_schema_known_only_through_a_grant_still_surfaces() -> None:
    catalogs = _assemble_catalog(
        "BANK",
        [],
        [],
        [],
        envelope=_OracleEnvelopeRows(
            grants=(
                {
                    "GRANTEE": "REPORTING_ROLE",
                    "TABLE_SCHEMA": "AUDIT",
                    "TABLE_NAME": "LOG_TABLE",
                    "PRIVILEGE": "SELECT",
                    "GRANTABLE": "NO",
                    "OBJECT_TYPE": "TABLE",
                    "GRANTEE_TYPE": "ROLE",
                },
            ),
        ),
    )

    assert len(catalogs[0].schemas) == 1
    schema = catalogs[0].schemas[0]
    assert schema.name == "AUDIT"
    assert schema.tables == ()
    assert len(schema.grants) == 1
    assert schema.grants[0].grantee == "REPORTING_ROLE"


def test_an_absent_envelope_leaves_the_v10_catalog_untouched() -> None:
    """The v1.0 assembly path stays byte-for-byte what it was.

    `_assemble_catalog` is called without an envelope by every existing test; a
    connector that has not collected the new axes must produce exactly the old graph
    rather than a graph full of empty envelope objects.
    """
    catalogs = _assemble_catalog("BANK", [_view_column_row("RETAIL", "V")], [], [])
    schema = catalogs[0].schemas[0]
    assert schema.routines == ()
    assert schema.grants == ()
    assert schema.tables[0].view_definition is None
    assert catalogs[0].attributes == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("IN", "IN"), ("OUT", "OUT"), ("IN/OUT", "INOUT"), (None, "IN")],
)
def test_argument_modes_are_normalized(raw: str | None, expected: str) -> None:
    assert _normalize_argument_mode(raw) == expected
