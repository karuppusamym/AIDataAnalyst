"""ingestion -- PRIVATE. Request/response models for `router.py`.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.schemas`, which now re-exports these classes for backward
compatibility -- every existing `from aida.schemas import X` caller keeps
working unchanged.

Covers the request/response DTOs for this module's owned models
(`atlas.modules.ingestion.models`): the metadata ingestion envelope
(1.0 and 1.1, gap/02 N1) at every axis -- catalog, schema, table, column,
constraint, view definition, routine, grant -- plus the job/batch/chunk
create and read DTOs.

The envelope schemas (`Metadata*Envelope`) describe the *wire format* a
producer sends, not the persisted catalog shape module 04 (catalog) owns
-- "envelopes" is explicitly this module's per
`04-module-decomposition.md` §4's register. What each envelope axis is
eventually persisted *as* (a `MetadataTable` row, a `MetadataViewDefinition`
row, etc.) is catalog's concern, reached through catalog's own service
layer, not this module's.

`ApiModel` stays defined in `aida.schemas` rather than moving here or to
`atlas.platform` -- it is the shared pydantic base for every module's
schemas, not ingestion-owned, and moving it is out of scope for this
pass. Importing it back from `aida.schemas` here works safely only
because `aida.schemas`' shim import of this module comes *after*
`ApiModel` is defined in that file -- see the comment there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from aida.schemas import ApiModel

MetadataAttribute = str | int | float | bool | None


class MetadataColumnEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    ordinal_position: int = Field(ge=1, le=100_000)
    physical_type: str = Field(min_length=1, max_length=255)
    nullable: bool
    default_expression: str | None = Field(default=None, max_length=4000)
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)


class MetadataConstraintEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    constraint_type: Literal["PRIMARY_KEY", "UNIQUE", "FOREIGN_KEY"]
    columns: list[str] = Field(min_length=1, max_length=1000)
    referenced_schema: str | None = Field(default=None, max_length=255)
    referenced_table: str | None = Field(default=None, max_length=255)
    referenced_columns: list[str] = Field(default_factory=list, max_length=1000)


# --- envelope 1.1 (gap/02 N1) -----------------------------------------------
#
# 1.1 is additive: every field below is optional, so a 1.0 payload validates
# unchanged and a 1.0 producer keeps working forever. What 1.1 buys is that the
# platform can tell "the producer sent no view definitions" apart from "the
# producer sent them and we dropped them" -- `ingestion.validate_envelope_version`
# rejects the second case rather than answering 201 to it.


class MetadataViewDefinitionEnvelope(ApiModel):
    """The text a view is defined by, and how much of it the source would give.

    `definition_sql is None` is a first-class state meaning *unavailable*, not
    *empty*, and it must be explained: the model refuses a null definition with
    no reason, and refuses a reason alongside a definition. That is deliberately
    stricter than a nullable string, because an unexplained NULL here becomes a
    permanently unexplainable gap in lineage coverage (gap/02 N2).
    """

    definition_sql: str | None = Field(default=None, max_length=1_000_000)
    is_materialized: bool = False
    is_updatable: bool | None = None
    check_option: str | None = Field(default=None, max_length=30)
    truncated: bool = False
    unavailable_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_availability(self) -> MetadataViewDefinitionEnvelope:
        if self.definition_sql is None and not self.unavailable_reason:
            raise ValueError(
                "a view definition without definition_sql must carry an "
                "unavailable_reason; an unexplained null is indistinguishable "
                "from an empty definition"
            )
        if self.definition_sql is not None and self.unavailable_reason:
            raise ValueError("unavailable_reason is only meaningful when definition_sql is null")
        if self.definition_sql is None and self.truncated:
            raise ValueError("a definition that was never returned cannot be truncated")
        return self


class MetadataRoutineParameterEnvelope(ApiModel):
    name: str | None = Field(default=None, max_length=255)
    ordinal_position: int = Field(ge=1, le=10_000)
    mode: Literal["IN", "OUT", "INOUT", "VARIADIC", "TABLE"] = "IN"
    physical_type: str = Field(min_length=1, max_length=255)
    default_expression: str | None = Field(default=None, max_length=4000)


class MetadataRoutineEnvelope(ApiModel):
    """A stored procedure or function, with its body when the source exposes it.

    Same availability rule as a view definition, for the same reason: procedure
    parsing and procedure-to-tool generation (gap/02 N3, N12) must never mistake
    "not allowed to read it" for "there is nothing to read".
    """

    name: str = Field(min_length=1, max_length=255)
    routine_type: Literal["FUNCTION", "PROCEDURE"]
    language: str | None = Field(default=None, max_length=50)
    body_sql: str | None = Field(default=None, max_length=1_000_000)
    parameters: list[MetadataRoutineParameterEnvelope] = Field(
        default_factory=list, max_length=1000
    )
    return_type: str | None = Field(default=None, max_length=255)
    is_deterministic: bool | None = None
    security_mode: Literal["DEFINER", "INVOKER"] | None = None
    source_description: str | None = Field(default=None, max_length=10_000)
    truncated: bool = False
    unavailable_reason: str | None = Field(default=None, max_length=500)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routine(self) -> MetadataRoutineEnvelope:
        ordinals = [parameter.ordinal_position for parameter in self.parameters]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("routine parameter ordinals must be unique within a routine")
        if self.body_sql is None and not self.unavailable_reason:
            raise ValueError(
                "a routine without body_sql must carry an unavailable_reason; an "
                "unexplained null is indistinguishable from an empty body"
            )
        if self.body_sql is not None and self.unavailable_reason:
            raise ValueError("unavailable_reason is only meaningful when body_sql is null")
        if self.body_sql is None and self.truncated:
            raise ValueError("a body that was never returned cannot be truncated")
        return self


class MetadataGrantEnvelope(ApiModel):
    """One privilege held by one grantee on one source object.

    Evidence about the estate, never authority in this platform: nothing here
    grants anything and the policy engine does not read it.
    """

    grantee: str = Field(min_length=1, max_length=255)
    grantee_type: Literal["USER", "ROLE", "GROUP", "PUBLIC"] = "ROLE"
    privilege: str = Field(pattern=r"^[A-Z][A-Z0-9_ ]{0,49}$")
    object_type: Literal["TABLE", "VIEW", "PROCEDURE", "FUNCTION", "SCHEMA", "SEQUENCE"] = "TABLE"
    object_name: str = Field(min_length=1, max_length=255)
    schema_name: str | None = Field(default=None, max_length=255)
    is_grantable: bool = False


class MetadataTableEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    object_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,29}$")
    source_description: str | None = Field(default=None, max_length=10_000)
    view_definition: MetadataViewDefinitionEnvelope | None = None
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    columns: list[MetadataColumnEnvelope] = Field(max_length=10_000)
    constraints: list[MetadataConstraintEnvelope] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_table_members(self) -> MetadataTableEnvelope:
        column_names = [column.name for column in self.columns]
        if len(column_names) != len(set(column_names)):
            raise ValueError("column names must be unique within a table")
        ordinals = [column.ordinal_position for column in self.columns]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("column ordinals must be unique within a table")
        available = set(column_names)
        for constraint in self.constraints:
            if not set(constraint.columns).issubset(available):
                raise ValueError(f"constraint {constraint.name} refers to an unknown local column")
            has_reference = bool(constraint.referenced_schema and constraint.referenced_table)
            if constraint.constraint_type == "FOREIGN_KEY" and not has_reference:
                raise ValueError("foreign keys require referenced_schema and referenced_table")
            if constraint.constraint_type == "FOREIGN_KEY" and (
                len(constraint.columns) != len(constraint.referenced_columns)
            ):
                raise ValueError("foreign-key local and referenced column counts must match")
        return self


class MetadataSchemaEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    tables: list[MetadataTableEnvelope] = Field(max_length=10_000)
    routines: list[MetadataRoutineEnvelope] = Field(default_factory=list, max_length=10_000)
    grants: list[MetadataGrantEnvelope] = Field(default_factory=list, max_length=100_000)


class MetadataCatalogEnvelope(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    source_description: str | None = Field(default=None, max_length=10_000)
    attributes: dict[str, MetadataAttribute] = Field(default_factory=dict)
    schemas: list[MetadataSchemaEnvelope] = Field(max_length=5000)


class MetadataIngestionCreate(ApiModel):
    # 1.1 is the current version; 1.0 stays accepted forever (contract §2.1) and
    # remains the *default*, so a producer that never sent the field keeps the
    # behaviour it has today. Opting in to 1.1 is explicit, because 1.1 also
    # opts a FULL snapshot in to reconciling the new axes -- and a producer that
    # was silently promoted would retire the estate's view definitions on its
    # next full scan. Declaring 1.0 while sending 1.1 content is rejected by
    # `ingestion.validate_envelope_version`, not silently stripped.
    envelope_version: Literal["1.0", "1.1"] = "1.0"
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    producer: str = Field(min_length=2, max_length=200)
    transport: Literal["PUSH", "STREAM"] = "PUSH"
    snapshot_type: Literal["FULL", "INCREMENTAL"] = "FULL"
    emitted_at: datetime
    catalogs: list[MetadataCatalogEnvelope] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_envelope(self) -> MetadataIngestionCreate:
        forbidden_fragments = ("sample", "row_value", "password", "secret", "token", "credential")
        total_tables = 0
        total_columns = 0
        total_routines = 0
        for catalog in self.catalogs:
            if len({schema.name for schema in catalog.schemas}) != len(catalog.schemas):
                raise ValueError("schema names must be unique within a catalog")
            self._validate_attributes(catalog.attributes, forbidden_fragments)
            for schema in catalog.schemas:
                if len({table.name for table in schema.tables}) != len(schema.tables):
                    raise ValueError("table names must be unique within a schema")
                self._validate_attributes(schema.attributes, forbidden_fragments)
                total_tables += len(schema.tables)
                # Envelope 1.1: a routine carries its own attribute bag, so it is
                # screened like every other object. An unscreened bag would be a
                # hole in INV-6 the moment 1.1 producers appear.
                total_routines += len(schema.routines)
                for routine in schema.routines:
                    self._validate_attributes(routine.attributes, forbidden_fragments)
                for table in schema.tables:
                    self._validate_attributes(table.attributes, forbidden_fragments)
                    for column in table.columns:
                        self._validate_attributes(column.attributes, forbidden_fragments)
                    total_columns += len(table.columns)
        if total_tables > 50_000 or total_columns > 250_000 or total_routines > 50_000:
            raise ValueError("envelope exceeds the synchronous ingestion safety boundary")
        return self

    @staticmethod
    def _validate_attributes(
        attributes: dict[str, MetadataAttribute], forbidden_fragments: tuple[str, ...]
    ) -> None:
        if len(attributes) > 50:
            raise ValueError("metadata attributes are limited to 50 entries per object")
        for key, value in attributes.items():
            normalized = key.lower()
            if any(fragment in normalized for fragment in forbidden_fragments):
                raise ValueError(
                    f"attribute key is not permitted by the value-free contract: {key}"
                )
            if len(key) > 100 or (isinstance(value, str) and len(value) > 2000):
                raise ValueError("metadata attribute key or value exceeds its size boundary")


class MetadataIngestionRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    analysis_run_id: UUID | None
    idempotency_key: str
    envelope_version: str
    producer: str
    transport: str
    snapshot_type: str
    payload_fingerprint: str
    status: str
    object_counts: dict[str, Any]
    change_counts: dict[str, Any]
    submitted_by: str
    error_class: str | None
    error_message: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MetadataIngestionBatchCreate(ApiModel):
    envelope_version: Literal["1.0", "1.1"] = "1.0"
    batch_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    producer: str = Field(min_length=2, max_length=200)
    snapshot_type: Literal["FULL", "INCREMENTAL"] = "INCREMENTAL"
    expected_chunks: int = Field(ge=1, le=1000)


class MetadataIngestionChunkCreate(ApiModel):
    chunk_number: int = Field(ge=1, le=1000)
    chunk_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    emitted_at: datetime
    catalogs: list[MetadataCatalogEnvelope] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_chunk_contract(self) -> MetadataIngestionChunkCreate:
        if self.emitted_at.tzinfo is None:
            raise ValueError("emitted_at must include a timezone")
        MetadataIngestionCreate(
            idempotency_key=self.chunk_key,
            producer="batch-chunk-validator",
            transport="PUSH",
            snapshot_type="INCREMENTAL",
            emitted_at=self.emitted_at,
            catalogs=self.catalogs,
        )
        return self


class MetadataIngestionBatchRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    analysis_run_id: UUID | None
    batch_key: str
    envelope_version: str
    producer: str
    snapshot_type: str
    expected_chunks: int
    received_chunks: int
    processed_chunks: int
    status: str
    temporal_workflow_id: str | None
    object_counts: dict[str, Any]
    change_counts: dict[str, Any]
    submitted_by: str
    finalized_at: datetime | None
    completed_at: datetime | None
    error_class: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class MetadataIngestionChunkRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    batch_id: UUID
    chunk_number: int
    chunk_key: str
    emitted_at: datetime
    payload_fingerprint: str
    object_counts: dict[str, Any]
    change_counts: dict[str, Any]
    status: str
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime
