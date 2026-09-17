"""Review 2026-09-16 §5: serve the engine capability matrix live.

The same shape `procedure_lineage_api` gives the parser capability matrix, and
for the same reason: a published reference page is worth more when a live,
callable source stands behind it, so the document cannot be the only place the
claim exists. `scripts/generate_engine_capability_matrix.py` writes
`Docs/90-reference/engine-capability-matrix.md` from
`aida.engine_capability_matrix.build_engine_capability_matrix`, and this route
calls that same function at request time.

Not datasource-scoped: which facets an adapter implements for which native
object kind is a property of the installed code, not of any one customer's
data, so no tenancy check applies. A `SecurityContext` is still required so an
unauthenticated caller cannot reach it -- the matrix is exactly what a
third-party risk assessment reads, and it should be answerable to a named
principal.

What a *scan* got, with this login, on this source is a different question with
a different answer, and it lives on the discovery receipt
(`analysis_run.discovery_receipt`) and the discovery-selection routes.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from aida.engine_capability_matrix import build_engine_capability_matrix
from aida.schemas import (
    EngineCapabilityEngineRead,
    EngineCapabilityFacetRead,
    EngineCapabilityMatrixRead,
    EngineCapabilityObjectKindRead,
    EngineDbtCoverageRead,
    EngineSourceMappingRead,
)
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["engine-capability"])

_READER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "Analyst",
    "Auditor",
    "Viewer",
)


@router.get("/engines/capability-matrix", response_model=EngineCapabilityMatrixRead)
async def get_engine_capability_matrix(
    context: SecurityContext = Depends(require_roles(*_READER_ROLES)),
) -> EngineCapabilityMatrixRead:
    """Per engine and native object kind, what each of the six review §5 facets
    can actually do -- derived from the connector registry, the adapters' own
    capability flags and method overrides, the lineage parsers' dialect map and
    construct matrix, and the blueprint generators' eligibility rules.

    `generated_at` is stamped here rather than baked into the published files,
    which carry no timestamp so their staleness gate can compare them byte for
    byte.
    """
    del context
    matrix = build_engine_capability_matrix()
    return EngineCapabilityMatrixRead(
        matrix_key=list(matrix.matrix_key),
        facets=list(matrix.facets),
        states=list(matrix.states),
        generated_at=datetime.now(UTC).isoformat(),
        engines=[
            EngineCapabilityEngineRead(
                engine=row.engine,
                display_name=row.display_name,
                dialect=row.dialect,
                adapter_version=row.adapter_version,
                implementation_status=row.implementation_status,
                maturity=row.maturity,
                parser_dialect_supported=row.parser_dialect_supported,
                live_validation=row.live_validation,
                flags=dict(row.flags),
                overridden_methods=list(row.overridden_methods),
                notes=row.notes,
            )
            for row in matrix.engines
        ],
        rows=[
            EngineCapabilityObjectKindRead(
                engine=row.engine,
                native_object_kind=row.native_object_kind,
                graph_category=row.graph_category,
                native_concept=row.native_concept,
                note=row.note,
                facets=[
                    EngineCapabilityFacetRead(
                        facet=cell.facet,
                        state=cell.state,
                        reason=cell.reason,
                        evidence=cell.evidence,
                    )
                    for cell in row.cells
                ],
            )
            for row in matrix.rows
        ],
        source_mapping=EngineSourceMappingRead(
            granularity=matrix.source_mapping.granularity,
            state=matrix.source_mapping.state,
            reason=matrix.source_mapping.reason,
            evidence=matrix.source_mapping.evidence,
            rationale=matrix.source_mapping.rationale,
        ),
        dbt_coverage=[
            EngineDbtCoverageRead(
                aspect=row.aspect,
                state=row.state,
                reason=row.reason,
                evidence=row.evidence,
            )
            for row in matrix.dbt_coverage
        ],
        parser_degradation_reasons=list(matrix.parser_degradation_reasons),
        declared_gaps=list(matrix.declared_gaps),
    )
