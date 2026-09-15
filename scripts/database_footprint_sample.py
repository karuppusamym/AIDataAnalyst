"""Analyze synthetic footprint fixtures using Atlas's current parsers; no source access.

Each object is analyzed in its *stored* form -- the literal-redacted text ingestion
persists (`redact_for_storage`) and the lineage agent later parses -- not the raw fixture,
so the report describes what Atlas actually knows. This produces a review artifact, not a
published context product or tool approval. Run from the repository root with the
project's Python environment.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from aida.ontology_api import OntologyDefinition
from aida.procedure_lineage import parse_procedure_lineage
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    build_procedure_tool_blueprint,
    find_single_read_only_result_statement,
)
from aida.sql_lineage_parser import parse_view_lineage
from aida.sql_redaction import redact_for_storage

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/database_footprint"
ENGINES = {"sqlserver": "tsql", "postgres": "postgres"}


def sample_sql(engine: str, name: str) -> str:
    return (FIXTURES / engine / f"{name}.sql").read_text(encoding="utf-8")


def stored_sql(engine: str, name: str) -> tuple[str, str]:
    """`(text, redaction_status)` as ingestion would persist this fixture."""
    prepared = redact_for_storage(sample_sql(engine, name), dialect=ENGINES[engine])
    if prepared is None or prepared.redacted is None:
        raise RuntimeError(f"{engine}/{name}.sql could not be stored")
    return prepared.redacted, prepared.status


def build_report() -> dict:
    ontology = OntologyDefinition.model_validate_json(
        (FIXTURES / "ontology.json").read_text(encoding="utf-8")
    )
    report = {
        "artifact_kind": "OFFLINE_SAMPLE_ANALYSIS",
        "analyzed_form": "STORED_LITERAL_REDACTED",
        "published_context_product": False,
        "ontology": {
            "status": "SCHEMA_VALIDATED_ONLY",
            "definition": ontology.model_dump(mode="json"),
            "mapping_note": (
                "Bind real catalog IDs after ingestion; no synthetic IDs are published."
            ),
        },
        "engines": {},
    }
    for engine, dialect in ENGINES.items():
        objects = {}
        view_sql, view_status = stored_sql(engine, "view")
        objects["view"] = {
            "evidence_ref": f"tests/fixtures/database_footprint/{engine}/view.sql",
            "stored_redaction_status": view_status,
            "analysis": asdict(parse_view_lineage(view_sql, dialect)),
        }
        for name in ("read", "refresh", "dynamic", "nested"):
            sql, status = stored_sql(engine, name)
            analysis = parse_procedure_lineage(sql, dialect)
            candidate = {"published": False, "execution_surface": "EXTRACTED_SELECT"}
            try:
                node, parsed = find_single_read_only_result_statement(sql, dialect)
                blueprint = build_procedure_tool_blueprint(
                    node,
                    [],
                    dialect=dialect,
                    statement_count=parsed.statement_count,
                    sql_hash=parsed.sql_hash,
                )
                candidate.update(status="BLUEPRINT_ONLY", sql_template=blueprint.sql_template)
            except ProcedureNotEligibleError as exc:
                candidate.update(status="BLOCKED", reason=str(exc))
            objects[name] = {
                "evidence_ref": f"tests/fixtures/database_footprint/{engine}/{name}.sql",
                "stored_redaction_status": status,
                "analysis": asdict(analysis),
                "unresolved_edge_count": sum(not edge.source_resolved for edge in analysis.edges),
                "tool_candidate": candidate,
            }
        report["engines"][engine] = objects
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_report(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote sample analysis to {args.output}")


if __name__ == "__main__":
    main()
