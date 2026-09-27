"""Source-shaped samples: useful lineage, explicit gaps and no automatic publication.

Analysis runs over the text ingestion stores (`redact_for_storage`), because that -- not the
raw fixture -- is what the lineage agent and a person's parse read.
"""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from aida.ontology_api import OntologyDefinition
from aida.procedure_lineage import UNPARSED_TRANSFORMATION_TYPE, parse_procedure_lineage
from aida.sql_lineage_parser import parse_view_lineage
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES
from scripts.database_footprint_sample import ENGINES, FIXTURES, build_report, stored_sql

PREFIX = "footprint_context_sample."


@pytest.fixture(scope="module")
def report():
    return build_report()


@pytest.mark.parametrize("engine,dialect", [("sqlserver", "tsql"), ("postgres", "postgres")])
def test_revenue_explanation_traces_both_amount_and_discount(engine, dialect):
    sql, _ = stored_sql(engine, "view")
    result = parse_view_lineage(sql, dialect)
    actual = {
        (edge.source_table, edge.source_column, edge.target_table, edge.target_column)
        for edge in result.edges
        if edge.source_resolved
    }
    assert actual >= {
        (PREFIX + "orders", "amount", PREFIX + "customer_revenue", "net_revenue"),
        (PREFIX + "orders", "discount", PREFIX + "customer_revenue", "net_revenue"),
        (PREFIX + "customers", "region", PREFIX + "customer_revenue", "region"),
    }


@pytest.mark.parametrize("engine", ["sqlserver", "postgres"])
def test_read_surface_has_source_evidence_but_is_never_published(report, engine):
    read = report["engines"][engine]["read"]
    assert read["analysis"]["is_fully_parsed"]
    assert read["unresolved_edge_count"] == 0
    assert read["tool_candidate"]["status"] == "BLUEPRINT_ONLY"
    assert read["tool_candidate"]["published"] is False
    assert PREFIX + "customer_revenue" in read["tool_candidate"]["sql_template"]
    assert Path(read["evidence_ref"]).is_file()


@pytest.mark.parametrize("engine", ["sqlserver", "postgres"])
def test_temp_flow_reaches_final_revenue_column(engine):
    sql, _ = stored_sql(engine, "refresh")
    result = parse_procedure_lineage(sql, ENGINES[engine])
    assert result.is_fully_parsed, result.errors
    assert any(
        edge.source_table == PREFIX + "orders"
        and edge.source_column == "discount"
        and edge.target_table == PREFIX + "customer_totals"
        and edge.target_column == "net_revenue"
        and edge.via_temp_table == "footprint_totals"
        for edge in result.edges
    )


@pytest.mark.parametrize("engine", ["sqlserver", "postgres"])
@pytest.mark.parametrize("name", ["refresh", "dynamic", "nested"])
def test_mutation_or_unknown_program_cannot_become_read_tool(report, engine, name):
    record = report["engines"][engine][name]
    assert record["tool_candidate"]["status"] == "BLOCKED"
    assert record["tool_candidate"]["reason"]
    assert record["analysis"]["is_read_only"] is False


@pytest.mark.parametrize("engine", ["sqlserver", "postgres"])
@pytest.mark.parametrize("name", ["dynamic", "nested"])
def test_unknown_code_survives_in_exported_analysis(report, engine, name):
    analysis = report["engines"][engine][name]["analysis"]
    assert analysis["is_fully_parsed"] is False
    assert any(edge["unparsed_reason"] for edge in analysis["edges"])


@pytest.mark.parametrize(
    "engine,name,reason",
    [
        ("sqlserver", "dynamic", "DYNAMIC_SQL"),
        ("postgres", "dynamic", "DYNAMIC_SQL"),
        ("sqlserver", "nested", "NESTED_PROCEDURE_CALL"),
        ("postgres", "nested", "NESTED_PROCEDURE_CALL"),
    ],
)
def test_each_gap_is_named_for_what_it_is_in_both_dialects(report, engine, name, reason):
    reasons = [
        edge["unparsed_reason"]
        for edge in report["engines"][engine][name]["analysis"]["edges"]
        if edge["transformation_type"] == UNPARSED_TRANSFORMATION_TYPE
    ]
    assert reasons and all(r.startswith(reason) for r in reasons), reasons


@pytest.mark.parametrize("engine", ["sqlserver", "postgres"])
@pytest.mark.parametrize("name", ["view", "read", "function", "refresh", "dynamic", "nested"])
def test_every_sample_is_stored_value_free_and_labelled(report, engine, name):
    _, status = stored_sql(engine, name)
    assert status in VALUE_FREE_REDACTION_STATUSES
    if name in report["engines"][engine]:
        assert report["engines"][engine][name]["stored_redaction_status"] == status


def test_ontology_accepts_new_business_concepts_but_validates_relationships():
    definition = OntologyDefinition.model_validate_json(
        (FIXTURES / "ontology.json").read_text(encoding="utf-8")
    )
    expanded = definition.model_dump(mode="json")
    expanded["concepts"].append(
        {
            "key": "region",
            "name": "Region",
            "description": "Customer reporting region",
        }
    )
    assert len(OntologyDefinition.model_validate(expanded).concepts) == 4
    invalid = deepcopy(expanded)
    invalid["relations"][0]["target"] = "invented_concept"
    with pytest.raises(ValidationError, match="endpoints"):
        OntologyDefinition.model_validate(invalid)


def test_an_ontology_maps_a_concept_to_a_routine_and_refuses_an_unknown_kind():
    definition = OntologyDefinition.model_validate_json(
        (FIXTURES / "ontology.json").read_text(encoding="utf-8")
    ).model_dump(mode="json")
    # R11-FP09: ROUTINE joined TABLE, VIEW and COLUMN as a mapping subject; the kind is still
    # checked against the catalog object on every write.
    definition["mappings"] = [
        {
            "concept": "customer_revenue",
            "subject_type": "ROUTINE",
            "subject_id": "00000000-0000-0000-0000-000000000001",
        }
    ]
    assert OntologyDefinition.model_validate(definition).mappings[0].subject_type == "ROUTINE"

    definition["mappings"][0]["subject_type"] = "SYNONYM"
    with pytest.raises(ValidationError):
        OntologyDefinition.model_validate(definition)
