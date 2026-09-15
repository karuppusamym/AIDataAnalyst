"""R11-FP09: an ontology concept can be mapped to a view or a routine, and mappings survive drift.

Two defects closed here. Mappings accepted only TABLE and COLUMN, so "net revenue" could name the
table a procedure fills but not the procedure, and a view could only be mapped by pretending it
was a table. And the version listing re-validated every mapping on every read and raised on the
first bad one, so deprecating one mapped table made the whole ontology history answer 422.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataColumn, MetadataSchema, MetadataTable, Organization
from aida.ontology_api import (
    OntologyCreate,
    OntologyDefinition,
    create_ontology_version,
    list_ontology_versions,
    submit_ontology_version,
)
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    seed_table,
    task_agent_session,
)

ROLES = frozenset({"DataSteward"})


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _definition(*mappings: tuple[str, str, object]) -> OntologyDefinition:
    return OntologyDefinition.model_validate(
        {
            "name": "Revenue",
            "owner": "finance-governance",
            "provenance": "Agreed at the 2026-09 finance data council.",
            "concepts": [
                {
                    "key": "net_revenue",
                    "name": "Net revenue",
                    "description": "Revenue after discounts and returns.",
                    "aliases": ["net sales"],
                }
            ],
            "mappings": [
                {"concept": concept, "subject_type": subject_type, "subject_id": str(subject_id)}
                for concept, subject_type, subject_id in mappings
            ],
        }
    )


async def _create(
    session: AsyncSession, org: Organization, *mappings: tuple[str, str, object]
) -> Any:
    return await create_ontology_version(
        org.id,
        OntologyCreate(ontology_key="revenue", definition=_definition(*mappings)),
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
    )


async def _routine(
    session: AsyncSession, org: Organization, datasource: DataSource, schema: MetadataSchema
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="rebuild_net_revenue",
        signature="()",
        routine_type="PROCEDURE",
        body_sql_redacted="CREATE PROCEDURE p() AS $$ BEGIN NULL; END; $$",
        redaction_status="LEXICAL",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return routine


async def _estate(session: AsyncSession) -> dict[str, Any]:
    org, datasource, schema = await seed_estate(session)
    table = await seed_table(session, org, datasource, schema, name="net_revenue")
    successor = await seed_table(session, org, datasource, schema, name="net_revenue_v2")
    view = await seed_table(
        session, org, datasource, schema, name="net_revenue_v", object_type="VIEW"
    )
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name="amount",
        ordinal_position=1,
        physical_type="numeric",
        nullable=False,
        fingerprint="fp",
    )
    session.add(column)
    routine = await _routine(session, org, datasource, schema)
    await session.commit()
    return {
        "org": org,
        "table": table,
        "successor": successor,
        "view": view,
        "column": column,
        "routine": routine,
    }


async def test_a_concept_maps_to_a_view_and_a_routine_and_every_mapping_reads_valid(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    org = estate["org"]

    created = await _create(
        session,
        org,
        ("net_revenue", "TABLE", estate["table"].id),
        ("net_revenue", "VIEW", estate["view"].id),
        ("net_revenue", "COLUMN", estate["column"].id),
        ("net_revenue", "ROUTINE", estate["routine"].id),
    )
    await submit_ontology_version(
        created.id,
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
    )

    (row,) = await list_ontology_versions(
        org.id, 50, 0, human(org, "reader-1", ROLES), session, agent_settings()
    )
    assert [(entry.subject_type, entry.status) for entry in row.mapping_validity] == [
        ("TABLE", "VALID"),
        ("VIEW", "VALID"),
        ("COLUMN", "VALID"),
        ("ROUTINE", "VALID"),
    ]
    datasources = {entry.datasource_id for entry in row.mapping_validity}
    assert datasources == {estate["table"].datasource_id}


@pytest.mark.parametrize(("subject", "written_as"), [("view", "TABLE"), ("table", "VIEW")])
async def test_a_mapping_must_name_the_kind_the_catalog_holds(
    session: AsyncSession, subject: str, written_as: str
) -> None:
    estate = await _estate(session)

    with pytest.raises(HTTPException) as refused:
        await _create(session, estate["org"], ("net_revenue", written_as, estate[subject].id))

    assert refused.value.status_code == 422
    assert "kind" in str(refused.value.detail)


async def test_another_organizations_routine_cannot_be_mapped(session: AsyncSession) -> None:
    estate = await _estate(session)
    stranger, stranger_source, stranger_schema = await seed_estate(session)
    foreign = await _routine(session, stranger, stranger_source, stranger_schema)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await _create(session, estate["org"], ("net_revenue", "ROUTINE", foreign.id))

    assert refused.value.status_code == 422


async def test_history_stays_readable_and_reports_drift_when_targets_are_retired(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    org = estate["org"]
    await _create(
        session,
        org,
        ("net_revenue", "TABLE", estate["table"].id),
        ("net_revenue", "ROUTINE", estate["routine"].id),
    )
    table: MetadataTable = estate["table"]
    table.status = "DEPRECATED"
    table.superseded_by_table_id = estate["successor"].id
    estate["routine"].status = "DEPRECATED"
    await session.commit()

    (row,) = await list_ontology_versions(
        org.id, 50, 0, human(org, "reader-1", ROLES), session, agent_settings()
    )

    by_type = {entry.subject_type: entry for entry in row.mapping_validity}
    assert by_type["TABLE"].status == "TARGET_DEPRECATED"
    assert by_type["TABLE"].superseded_by_id == estate["successor"].id
    assert by_type["ROUTINE"].status == "TARGET_DEPRECATED"
    # ...but a new draft may not carry a retired target forward.
    with pytest.raises(HTTPException) as refused:
        await _create(session, org, ("net_revenue", "TABLE", table.id))
    assert refused.value.status_code == 422
