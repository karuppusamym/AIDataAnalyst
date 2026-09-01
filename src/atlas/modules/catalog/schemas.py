"""catalog -- PRIVATE. Request/response models for `router.py`.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.schemas`, which now re-exports these classes for backward
compatibility -- every existing `from aida.schemas import X` caller keeps
working unchanged.

Covers the read DTOs for this module's owned models
(`atlas.modules.catalog.models`). Catalog objects arrive only through
ingestion (module 03), never created directly via this module's own API,
so there are no `*Create`/`*Update` DTOs here -- only `*Read`. There is
also no `MetadataCatalogRead`/`MetadataSchemaRead`: the catalog and
schema levels of the hierarchy are not exposed as their own read
endpoints today: only tables, columns, constraints, indexes and
partitions are.

`ApiModel` stays defined in `aida.schemas` rather than moving here or to
`atlas.platform` -- it is the shared pydantic base for every module's
schemas, not catalog-owned, and moving it is out of scope for this pass.
Importing it back from `aida.schemas` here works safely only because
`aida.schemas`' shim import of this module comes *after* `ApiModel` is
defined in that file -- see the comment there.
"""

from __future__ import annotations

from uuid import UUID

from aida.schemas import ApiModel


class MetadataColumnRead(ApiModel):
    id: UUID
    name: str
    ordinal_position: int
    physical_type: str
    nullable: bool
    classification: str
    classification_source: str
    status: str
    source_description: str | None = None


class MetadataConstraintRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    constraint_type: str
    columns: list[str]
    referenced_table_id: UUID | None
    referenced_columns: list[str]
    status: str


class MetadataIndexRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    index_type: str
    columns: list[str]
    is_unique: bool
    is_primary: bool
    status: str


class MetadataPartitionRead(ApiModel):
    id: UUID
    table_id: UUID
    name: str
    partition_type: str
    ordinal_position: int
    key_columns: list[str]
    high_value: str | None
    status: str


class MetadataTableRead(ApiModel):
    id: UUID
    datasource_id: UUID
    schema_id: UUID
    name: str
    object_type: str
    status: str
    fingerprint: str
