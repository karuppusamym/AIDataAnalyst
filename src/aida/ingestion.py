"""Envelope validation, conversion, counting, and 1.1-axis persistence.

Envelope 1.0 (catalogs / schemas / tables / columns / constraints) is persisted by
`workflows.activities.persist_discovery_snapshot`. Envelope 1.1 adds view
definitions, routines, source descriptions and grants; `persist_envelope_extensions`
below writes those, keyed off the 1.0 rows that call already created.

The two are deliberately separate calls rather than one, because the 1.1 axes are
optional per producer and per connector: an estate that sends no views must cost
nothing extra, and a connector that does not implement routines must leave the
routine inventory *absent* rather than empty (INV-9). Both halves share the same
reconciliation contract -- `deprecate_missing=False` during chunk processing, one
authoritative reconciliation pass after every chunk has succeeded (INV-11).
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredConstraint,
    DiscoveredGrant,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredSchema,
    DiscoveredTable,
    DiscoveredViewDefinition,
)
from aida.connectors.registry import ConnectorDefinition
from aida.envelope_models import (
    AVAILABLE,
    UNAVAILABLE,
    MetadataObjectDescription,
    MetadataRoutine,
    MetadataRoutineParameter,
    MetadataSourceGrant,
    MetadataViewDefinition,
)
from aida.ingest_screening import CLEAN, screen_text
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
)
from aida.schemas import (
    MetadataCatalogEnvelope,
    MetadataIngestionChunkCreate,
    MetadataIngestionCreate,
)
from aida.sql_redaction import redact_for_storage

#: The version this build produces and documents. 1.0 stays accepted forever.
INGESTION_CONTRACT_VERSION = "1.1"

#: Every envelope version the push API accepts. Removing an entry here is a
#: breaking change to a T1 external contract and needs an ADR, not a commit.
SUPPORTED_ENVELOPE_VERSIONS = ("1.0", "1.1")

CERTIFICATION_SUITE_VERSION = "connector-contract-v1"


def envelope_fingerprint(envelope: MetadataIngestionCreate) -> str:
    canonical = json.dumps(envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def envelope_counts(envelope: MetadataIngestionCreate) -> dict[str, int]:
    return catalog_counts(envelope.catalogs)


def catalog_counts(catalogs: list[MetadataCatalogEnvelope]) -> dict[str, int]:
    """Count the declared inventory, including the 1.1 axes.

    The 1.1 keys are always present and are zero for a 1.0 envelope, so a
    consumer of `object_counts` never has to tell "the producer sent none" apart
    from "this build did not count them".

    Source descriptions are deliberately *not* counted. A description is a
    property of an object that is already counted, like `nullable`; counting it
    as inventory would make the same estate appear to grow when a DBA writes a
    comment.
    """
    schemas = tables = columns = constraints = 0
    views = routines = routine_parameters = grants = 0
    for catalog in catalogs:
        schemas += len(catalog.schemas)
        for schema in catalog.schemas:
            tables += len(schema.tables)
            routines += len(schema.routines)
            grants += len(schema.grants)
            for routine in schema.routines:
                routine_parameters += len(routine.parameters)
            for table in schema.tables:
                columns += len(table.columns)
                constraints += len(table.constraints)
                if table.view_definition is not None:
                    views += 1
    return {
        "catalogs": len(catalogs),
        "schemas": schemas,
        "tables": tables,
        "columns": columns,
        "constraints": constraints,
        "views": views,
        "routines": routines,
        "routine_parameters": routine_parameters,
        "grants": grants,
    }


def envelope_to_discovery(
    envelope: MetadataIngestionCreate,
) -> tuple[DiscoveredCatalog, ...]:
    return catalogs_to_discovery(envelope.catalogs)


def catalogs_to_discovery(
    catalogs: list[MetadataCatalogEnvelope],
) -> tuple[DiscoveredCatalog, ...]:
    return tuple(
        DiscoveredCatalog(
            name=catalog.name,
            attributes=dict(catalog.attributes),
            source_description=catalog.source_description,
            schemas=tuple(
                DiscoveredSchema(
                    name=schema.name,
                    attributes=dict(schema.attributes),
                    source_description=schema.source_description,
                    routines=tuple(
                        DiscoveredRoutine(
                            name=routine.name,
                            routine_type=routine.routine_type,
                            language=routine.language,
                            body_sql=routine.body_sql,
                            parameters=tuple(
                                DiscoveredRoutineParameter(
                                    name=parameter.name,
                                    ordinal_position=parameter.ordinal_position,
                                    mode=parameter.mode,
                                    physical_type=parameter.physical_type,
                                    default_expression=parameter.default_expression,
                                )
                                for parameter in routine.parameters
                            ),
                            return_type=routine.return_type,
                            is_deterministic=routine.is_deterministic,
                            security_mode=routine.security_mode,
                            source_description=routine.source_description,
                            truncated=routine.truncated,
                            unavailable_reason=routine.unavailable_reason,
                            attributes=dict(routine.attributes),
                        )
                        for routine in schema.routines
                    ),
                    grants=tuple(
                        DiscoveredGrant(
                            grantee=grant.grantee,
                            grantee_type=grant.grantee_type,
                            privilege=grant.privilege,
                            object_type=grant.object_type,
                            object_name=grant.object_name,
                            schema_name=grant.schema_name or schema.name,
                            is_grantable=grant.is_grantable,
                        )
                        for grant in schema.grants
                    ),
                    tables=tuple(
                        DiscoveredTable(
                            name=table.name,
                            object_type=table.object_type,
                            source_description=table.source_description,
                            attributes=dict(table.attributes),
                            view_definition=(
                                None
                                if table.view_definition is None
                                else DiscoveredViewDefinition(
                                    definition_sql=table.view_definition.definition_sql,
                                    is_materialized=table.view_definition.is_materialized,
                                    is_updatable=table.view_definition.is_updatable,
                                    check_option=table.view_definition.check_option,
                                    truncated=table.view_definition.truncated,
                                    unavailable_reason=(
                                        table.view_definition.unavailable_reason
                                    ),
                                )
                            ),
                            columns=tuple(
                                DiscoveredColumn(
                                    name=column.name,
                                    ordinal_position=column.ordinal_position,
                                    physical_type=column.physical_type,
                                    nullable=column.nullable,
                                    default_expression=column.default_expression,
                                    source_description=column.source_description,
                                    attributes=dict(column.attributes),
                                )
                                for column in table.columns
                            ),
                            constraints=tuple(
                                DiscoveredConstraint(
                                    name=constraint.name,
                                    constraint_type=constraint.constraint_type,
                                    columns=tuple(constraint.columns),
                                    referenced_schema=constraint.referenced_schema,
                                    referenced_table=constraint.referenced_table,
                                    referenced_columns=tuple(constraint.referenced_columns),
                                )
                                for constraint in table.constraints
                            ),
                        )
                        for table in schema.tables
                    ),
                )
                for schema in catalog.schemas
            ),
        )
        for catalog in catalogs
    )


def chunk_fingerprint(chunk: MetadataIngestionChunkCreate) -> str:
    canonical = json.dumps(chunk.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_batch_chunks(
    chunks: list[MetadataIngestionChunkCreate], expected_chunks: int
) -> dict[str, int]:
    numbers = [chunk.chunk_number for chunk in chunks]
    if sorted(numbers) != list(range(1, expected_chunks + 1)):
        raise ValueError("batch chunks must be complete and numbered consecutively from one")
    table_keys: set[tuple[str, str, str]] = set()
    totals: dict[str, int] = dict.fromkeys(catalog_counts([]), 0)
    for chunk in chunks:
        counts = catalog_counts(chunk.catalogs)
        for key, value in counts.items():
            totals[key] += value
        for catalog in chunk.catalogs:
            for schema in catalog.schemas:
                for table in schema.tables:
                    table_key = (catalog.name, schema.name, table.name)
                    if table_key in table_keys:
                        raise ValueError(
                            "a table may appear in only one chunk: " + ".".join(table_key)
                        )
                    table_keys.add(table_key)
    return totals


def connector_certification_evidence(
    datasource: DataSource,
    definition: ConnectorDefinition,
    *,
    active_catalogs: int,
    active_tables: int,
) -> tuple[str, int, list[dict[str, Any]]]:
    capabilities = datasource.capabilities or {}
    checks = [
        _check(
            "implementation",
            definition.implementation_status == "IMPLEMENTED",
            definition.version,
        ),
        _check(
            "opaque_secret_reference",
            "://" in datasource.credential_reference,
            "reference only",
        ),
        _check(
            "connection_evidence",
            datasource.status in {"CONNECTION_VERIFIED", "ACTIVE"},
            datasource.status,
        ),
        _check(
            "hierarchy_contract",
            bool(capabilities.get("catalogs")) and bool(capabilities.get("schemas")),
            "catalog and schema capabilities required",
        ),
        _check(
            "inventory_evidence",
            active_catalogs > 0 and active_tables > 0,
            f"{active_catalogs} catalogs / {active_tables} tables",
        ),
        _check(
            "canonical_push_contract",
            "PUSH" in definition.transports,
            INGESTION_CONTRACT_VERSION,
        ),
    ]
    passed = sum(1 for check in checks if check["status"] == "PASS")
    score = round(passed * 100 / len(checks))
    if passed == len(checks):
        status = "CERTIFIED"
    elif score >= 67:
        status = "CONDITIONAL"
    else:
        status = "FAILED"
    return status, score, checks


def connector_definition_payload(
    definition: ConnectorDefinition, capabilities: dict[str, bool] | None = None
) -> dict[str, Any]:
    return definition.as_dict(capabilities=capabilities)


def _check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "evidence": evidence,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def default_capabilities(definition: ConnectorDefinition) -> dict[str, bool]:
    if definition.implementation_status != "IMPLEMENTED":
        return {}
    return dict(definition.capabilities)


# --- envelope 1.1: version gate ---------------------------------------------


def _one_one_fields(catalogs: list[MetadataCatalogEnvelope]) -> list[str]:
    """Names of envelope-1.1 fields actually populated in this payload."""
    present: list[str] = []
    for catalog in catalogs:
        if catalog.source_description is not None:
            present.append("catalogs[].source_description")
        for schema in catalog.schemas:
            if schema.source_description is not None:
                present.append("schemas[].source_description")
            if schema.routines:
                present.append("schemas[].routines")
            if schema.grants:
                present.append("schemas[].grants")
            for table in schema.tables:
                if table.view_definition is not None:
                    present.append("tables[].view_definition")
                for column in table.columns:
                    if column.source_description is not None:
                        present.append("columns[].source_description")
    return sorted(set(present))


def validate_envelope_version(
    envelope_version: str, catalogs: list[MetadataCatalogEnvelope]
) -> None:
    """Reject a payload that carries 1.1 content while declaring 1.0.

    The alternative -- accept the envelope and drop the fields -- is the failure
    this check exists to prevent. A producer that ships view definitions and gets
    a 201 back has every reason to believe lineage will follow, and would find out
    otherwise only by noticing an absence months later. Declaring the version is
    one line for the producer and is the only way the platform can tell "did not
    send" from "sent and we ignored it".

    1.0 payloads with no 1.1 content stay valid forever; this raises only when
    the two disagree.
    """
    if envelope_version != "1.0":
        return
    populated = _one_one_fields(catalogs)
    if populated:
        raise ValueError(
            "envelope_version 1.0 does not carry these fields; declare "
            'envelope_version "1.1" to send them: ' + ", ".join(populated)
        )


# --- envelope 1.1: persistence ----------------------------------------------


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def routine_signature(parameters: tuple[DiscoveredRoutineParameter, ...]) -> str:
    """The overload-discriminating identity of a routine, derived from its parameters.

    Sources that permit overloading (PostgreSQL) reuse a routine name within a
    schema, so `(schema, name)` is not an identity there. Deriving the signature
    from the parameter types rather than storing a source-side identifier keeps
    the key stable across snapshots and works on sources that have no such
    identifier at all.
    """
    ordered = sorted(parameters, key=lambda parameter: parameter.ordinal_position)
    return "(" + ",".join(parameter.physical_type for parameter in ordered) + ")"


def grant_key(grant: DiscoveredGrant) -> str:
    """Stable identity for one privilege row, hashed to a fixed width."""
    return _fingerprint(
        [
            grant.grantee,
            grant.grantee_type,
            grant.privilege,
            grant.object_type,
            grant.schema_name or "",
            grant.object_name,
        ]
    )


@dataclass(slots=True)
class EnvelopeScope:
    """Envelope-1.1 object identities observed across one or many chunks.

    The 1.0 counterpart is `workflows.activities.SnapshotScope`, and the contract
    is identical and for the same reason (INV-11, contract §4): a FULL delivery
    accumulates identities across every chunk and reconciles omissions **only
    after all chunks have succeeded**, so a network failure halfway through can
    never soft-delete the view definitions that did not arrive.
    """

    view_definition_ids: set[UUID] = field(default_factory=set)
    routine_ids: set[UUID] = field(default_factory=set)
    routine_parameter_ids: set[UUID] = field(default_factory=set)
    object_description_ids: set[UUID] = field(default_factory=set)
    grant_ids: set[UUID] = field(default_factory=set)

    def object_counts(self) -> dict[str, int]:
        return {
            "views": len(self.view_definition_ids),
            "routines": len(self.routine_ids),
            "routine_parameters": len(self.routine_parameter_ids),
            "object_descriptions": len(self.object_description_ids),
            "grants": len(self.grant_ids),
        }


@dataclass(slots=True)
class _ExtensionTracker:
    created: int = 0
    changed: int = 0
    deprecated: int = 0

    def observe(self, existing: Any | None, new_fingerprint: str) -> None:
        if existing is None:
            self.created += 1
        elif existing.fingerprint != new_fingerprint or existing.status != "ACTIVE":
            self.changed += 1


def _availability(text: str | None) -> tuple[str, str | None]:
    """Split a nullable definition into the stored availability pair.

    `None` means the source would not give the text; `""` means it gave an empty
    one. Collapsing those into a single nullable column is precisely what the
    1.1 storage model refuses to do -- see `envelope_models` for the table.
    """
    if text is None:
        return UNAVAILABLE, "source did not provide the definition text"
    return AVAILABLE, None


def _catalog_carries_extensions(catalog: DiscoveredCatalog) -> bool:
    if catalog.source_description is not None:
        return True
    return any(_schema_carries_extensions(schema) for schema in catalog.schemas)


def _schema_carries_extensions(schema: DiscoveredSchema) -> bool:
    if schema.source_description is not None or schema.routines or schema.grants:
        return True
    return any(_table_carries_extensions(table) for table in schema.tables)


def _table_carries_extensions(table: DiscoveredTable) -> bool:
    # IN-5e: column descriptions are no longer this pass's concern -- they land
    # on `MetadataColumn.source_description` directly during the 1.0 pass
    # (`persist_discovery_snapshot`, which always runs first), so a table whose
    # only "extension" was a column comment has nothing left for this loop to do.
    return table.view_definition is not None


async def _upsert_description(
    session: AsyncSession,
    datasource: DataSource,
    tracker: _ExtensionTracker,
    *,
    object_type: str,
    description: str,
    catalog_id: UUID | None = None,
    schema_id: UUID | None = None,
) -> MetadataObjectDescription:
    """CATALOG/SCHEMA only -- COLUMN moved to `MetadataColumn.source_description`
    directly (IN-5e); see `persist_envelope_extensions`'s column loop below."""
    subject = {"catalog_id": catalog_id, "schema_id": schema_id}
    filters = [
        getattr(MetadataObjectDescription, name) == value
        for name, value in subject.items()
        if value is not None
    ]
    existing = await session.scalar(select(MetadataObjectDescription).where(*filters))
    row_fingerprint = _fingerprint({"object_type": object_type, "description": description})
    tracker.observe(existing, row_fingerprint)
    if existing is None:
        existing = MetadataObjectDescription(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            object_type=object_type,
            description=description,
            fingerprint=row_fingerprint,
            **subject,
        )
        session.add(existing)
    else:
        existing.status = "ACTIVE"
        existing.deprecated_at = None
        existing.description = description
        existing.fingerprint = row_fingerprint
    return existing


def _store_source_sql(
    raw: str | None, *, dialect: str
) -> tuple[str | None, str | None, str, str, list[str]]:
    """Prepare source-supplied SQL for persistence: redact, fingerprint, screen.

    Returns `(redacted_text, fingerprint, redaction_status, screening_status, reasons)`.

    Both steps happen here, at the single write point, rather than at each of the several
    places that later read this text. Redaction keeps source values out of the control
    plane (INV-6); screening records whether the text is safe to put in model context
    (ADR-0013's unaddressed indirect-injection gap). Screening runs against the **raw**
    text, because an injection attempt hides in prose and comments, which redaction does
    not touch and must not.
    """
    prepared = redact_for_storage(raw, dialect=dialect)
    if prepared is None:
        return None, None, "PARSED", CLEAN, []
    verdict = screen_text(raw)
    return (
        prepared.redacted,
        prepared.fingerprint,
        prepared.status,
        verdict.status,
        verdict.reason_codes,
    )



async def _upsert_view_definition(
    session: AsyncSession,
    datasource: DataSource,
    table: MetadataTable,
    discovered: DiscoveredViewDefinition,
    tracker: _ExtensionTracker,
) -> MetadataViewDefinition:
    existing = await session.scalar(
        select(MetadataViewDefinition).where(MetadataViewDefinition.table_id == table.id)
    )
    row_fingerprint = _fingerprint(asdict(discovered))
    tracker.observe(existing, row_fingerprint)
    availability, default_reason = _availability(discovered.definition_sql)
    # The CHECK constraint ties availability to the *stored* column. Redaction almost
    # always yields text -- a lexical scrub needs no parser -- so this only fires when
    # nothing at all could be stored.
    _prepared_view = redact_for_storage(discovered.definition_sql, dialect=datasource.dialect)
    if _prepared_view is not None and _prepared_view.redacted is None:
        availability, default_reason = UNAVAILABLE, "DEFINITION_NOT_STORABLE"
    reason = discovered.unavailable_reason or (
        default_reason if availability == UNAVAILABLE else None
    )
    if existing is None:
        existing = MetadataViewDefinition(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=table.id,
            fingerprint=row_fingerprint,
        )
        session.add(existing)
    existing.status = "ACTIVE"
    existing.deprecated_at = None
    (
        existing.definition_sql_redacted,
        existing.definition_fingerprint,
        existing.redaction_status,
        existing.screening_status,
        existing.screening_reason_codes,
    ) = _store_source_sql(discovered.definition_sql, dialect=datasource.dialect)
    existing.is_materialized = discovered.is_materialized
    existing.is_updatable = discovered.is_updatable
    existing.check_option = discovered.check_option
    existing.truncated = discovered.truncated
    existing.availability = availability
    existing.unavailable_reason = reason
    existing.fingerprint = row_fingerprint
    return existing


async def _upsert_routine(
    session: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    discovered: DiscoveredRoutine,
    tracker: _ExtensionTracker,
) -> MetadataRoutine:
    signature = routine_signature(discovered.parameters)
    existing = await session.scalar(
        select(MetadataRoutine).where(
            MetadataRoutine.schema_id == schema.id,
            MetadataRoutine.name == discovered.name,
            MetadataRoutine.signature == signature,
        )
    )
    row_fingerprint = _fingerprint(asdict(discovered))
    tracker.observe(existing, row_fingerprint)
    availability, default_reason = _availability(discovered.body_sql)
    _prepared_body = redact_for_storage(discovered.body_sql, dialect=datasource.dialect)
    if _prepared_body is not None and _prepared_body.redacted is None:
        availability, default_reason = UNAVAILABLE, "BODY_NOT_STORABLE"
    reason = discovered.unavailable_reason or (
        default_reason if availability == UNAVAILABLE else None
    )
    if existing is None:
        existing = MetadataRoutine(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=discovered.name,
            signature=signature,
            routine_type=discovered.routine_type,
            fingerprint=row_fingerprint,
        )
        session.add(existing)
    existing.status = "ACTIVE"
    existing.deprecated_at = None
    existing.routine_type = discovered.routine_type
    existing.language = discovered.language
    (
        existing.body_sql_redacted,
        existing.body_fingerprint,
        existing.redaction_status,
        existing.screening_status,
        existing.screening_reason_codes,
    ) = _store_source_sql(discovered.body_sql, dialect=datasource.dialect)
    existing.return_type = discovered.return_type
    existing.is_deterministic = discovered.is_deterministic
    existing.security_mode = discovered.security_mode
    existing.source_description = discovered.source_description
    existing.truncated = discovered.truncated
    existing.availability = availability
    existing.unavailable_reason = reason
    existing.attributes = dict(discovered.attributes)
    existing.fingerprint = row_fingerprint
    return existing


async def _upsert_routine_parameter(
    session: AsyncSession,
    datasource: DataSource,
    routine: MetadataRoutine,
    discovered: DiscoveredRoutineParameter,
    tracker: _ExtensionTracker,
) -> MetadataRoutineParameter:
    existing = await session.scalar(
        select(MetadataRoutineParameter).where(
            MetadataRoutineParameter.routine_id == routine.id,
            MetadataRoutineParameter.ordinal_position == discovered.ordinal_position,
        )
    )
    row_fingerprint = _fingerprint(asdict(discovered))
    tracker.observe(existing, row_fingerprint)
    if existing is None:
        existing = MetadataRoutineParameter(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            ordinal_position=discovered.ordinal_position,
            physical_type=discovered.physical_type,
            fingerprint=row_fingerprint,
        )
        session.add(existing)
    existing.status = "ACTIVE"
    existing.deprecated_at = None
    existing.name = discovered.name
    existing.mode = discovered.mode
    existing.physical_type = discovered.physical_type
    existing.default_expression = discovered.default_expression
    existing.fingerprint = row_fingerprint
    return existing


async def _upsert_grant(
    session: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    discovered: DiscoveredGrant,
    tracker: _ExtensionTracker,
) -> MetadataSourceGrant:
    key = grant_key(discovered)
    existing = await session.scalar(
        select(MetadataSourceGrant).where(
            MetadataSourceGrant.schema_id == schema.id,
            MetadataSourceGrant.grant_key == key,
        )
    )
    row_fingerprint = _fingerprint(asdict(discovered))
    tracker.observe(existing, row_fingerprint)
    if existing is None:
        existing = MetadataSourceGrant(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            grant_key=key,
            grantee=discovered.grantee,
            grantee_type=discovered.grantee_type,
            privilege=discovered.privilege,
            object_type=discovered.object_type,
            object_name=discovered.object_name,
            fingerprint=row_fingerprint,
        )
        session.add(existing)
    existing.status = "ACTIVE"
    existing.deprecated_at = None
    existing.schema_name = discovered.schema_name
    existing.is_grantable = discovered.is_grantable
    existing.fingerprint = row_fingerprint
    return existing


async def deprecate_missing_envelope_extensions(
    session: AsyncSession, datasource: DataSource, scope: EnvelopeScope
) -> int:
    """Soft-deprecate 1.1 rows absent from an authoritative full snapshot.

    Callers must only reach this once every chunk of a FULL delivery has been
    persisted (INV-11), and only for a delivery that actually declared envelope
    1.1: a 1.0 producer is authoritative for the 1.0 inventory and says nothing
    at all about views, routines, descriptions or grants, so reconciling its
    silence would retire metadata on a producer downgrade.
    """
    now = datetime.now(UTC)
    deprecated = 0
    for model, observed in (
        (MetadataViewDefinition, scope.view_definition_ids),
        (MetadataRoutine, scope.routine_ids),
        (MetadataRoutineParameter, scope.routine_parameter_ids),
        (MetadataObjectDescription, scope.object_description_ids),
        (MetadataSourceGrant, scope.grant_ids),
    ):
        existing = set(
            await session.scalars(
                select(model.id).where(
                    model.datasource_id == datasource.id,
                    model.organization_id == datasource.organization_id,
                )
            )
        )
        missing = existing - observed
        if not missing:
            continue
        result = await session.execute(
            update(model)
            .where(model.id.in_(missing), model.status == "ACTIVE")
            .values(status="DEPRECATED", deprecated_at=now, updated_at=now)
        )
        deprecated += cast(CursorResult[Any], result).rowcount
    return deprecated


async def persist_envelope_extensions(
    session: AsyncSession,
    datasource: DataSource,
    catalogs: tuple[DiscoveredCatalog, ...],
    *,
    scope: EnvelopeScope | None = None,
    deprecate_missing: bool = False,
) -> dict[str, int]:
    """Persist the envelope-1.1 axes for an already-persisted 1.0 snapshot.

    Must be called *after* `persist_discovery_snapshot` on the same session and
    the same `catalogs`: every row written here hangs off a catalog, schema,
    table or column that call created, and is looked up by name rather than
    passed in so that the two persistence halves stay independently callable and
    independently testable.

    An object the 1.0 pass did not create is skipped rather than invented. That
    can only happen when the two calls are given different trees, and inventing a
    parent would put a view definition under a table that does not exist.
    """
    working_scope = scope if scope is not None else EnvelopeScope()
    tracker = _ExtensionTracker()
    counts = {
        "views": 0,
        "routines": 0,
        "routine_parameters": 0,
        "object_descriptions": 0,
        "grants": 0,
    }

    for discovered_catalog in catalogs:
        if not _catalog_carries_extensions(discovered_catalog):
            continue
        catalog = await session.scalar(
            select(MetadataCatalog).where(
                MetadataCatalog.datasource_id == datasource.id,
                MetadataCatalog.name == discovered_catalog.name,
            )
        )
        if catalog is None:
            continue
        if discovered_catalog.source_description is not None:
            description = await _upsert_description(
                session,
                datasource,
                tracker,
                object_type="CATALOG",
                description=discovered_catalog.source_description,
                catalog_id=catalog.id,
            )
            await session.flush()
            working_scope.object_description_ids.add(description.id)
            counts["object_descriptions"] += 1

        for discovered_schema in discovered_catalog.schemas:
            if not _schema_carries_extensions(discovered_schema):
                continue
            schema = await session.scalar(
                select(MetadataSchema).where(
                    MetadataSchema.catalog_id == catalog.id,
                    MetadataSchema.name == discovered_schema.name,
                )
            )
            if schema is None:
                continue
            if discovered_schema.source_description is not None:
                description = await _upsert_description(
                    session,
                    datasource,
                    tracker,
                    object_type="SCHEMA",
                    description=discovered_schema.source_description,
                    schema_id=schema.id,
                )
                await session.flush()
                working_scope.object_description_ids.add(description.id)
                counts["object_descriptions"] += 1

            for discovered_routine in discovered_schema.routines:
                routine = await _upsert_routine(
                    session, datasource, schema, discovered_routine, tracker
                )
                await session.flush()
                working_scope.routine_ids.add(routine.id)
                counts["routines"] += 1
                for discovered_parameter in discovered_routine.parameters:
                    parameter = await _upsert_routine_parameter(
                        session, datasource, routine, discovered_parameter, tracker
                    )
                    await session.flush()
                    working_scope.routine_parameter_ids.add(parameter.id)
                    counts["routine_parameters"] += 1

            for discovered_grant in discovered_schema.grants:
                grant = await _upsert_grant(
                    session, datasource, schema, discovered_grant, tracker
                )
                await session.flush()
                working_scope.grant_ids.add(grant.id)
                counts["grants"] += 1

            for discovered_table in discovered_schema.tables:
                if not _table_carries_extensions(discovered_table):
                    continue
                table = await session.scalar(
                    select(MetadataTable).where(
                        MetadataTable.schema_id == schema.id,
                        MetadataTable.name == discovered_table.name,
                    )
                )
                if table is None:
                    continue
                if discovered_table.view_definition is not None:
                    view = await _upsert_view_definition(
                        session, datasource, table, discovered_table.view_definition, tracker
                    )
                    await session.flush()
                    working_scope.view_definition_ids.add(view.id)
                    counts["views"] += 1

    if deprecate_missing:
        tracker.deprecated = await deprecate_missing_envelope_extensions(
            session, datasource, working_scope
        )
    return {
        **counts,
        "created_objects": tracker.created,
        "changed_objects": tracker.changed,
        "deprecated_objects": tracker.deprecated,
    }
