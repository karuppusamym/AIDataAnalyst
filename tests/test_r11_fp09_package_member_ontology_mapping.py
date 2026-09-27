"""R11-FP09: package-member routines map to an ontology concept exactly like standalone ones.

The row's own "Remaining" text said package-member mappings "wait on FP03". FP03 has since
landed (`c6e62fe`, `cef4811` and later slices): a package member is captured as a plain
`MetadataRoutine` row with its package recorded in `package_name` -- "Each member is its own
routine, with its package in `package_name`" (FP03's own row text) -- and nothing else about
the row makes it a different kind of object.

This file checks concretely, the same way R11-FP08 was closed on 2026-09-22 (a package MEMBER
is `is_describable_routine() is True`, a plain routine row FP03's later slices gave it), whether
ontology mapping already carries a package member for free:

* `ontology_api._mapping_status` / `_load_targets` (used by create, submit and the version
  listing) resolve a `ROUTINE` mapping by loading `MetadataRoutine` rows by id and checking
  organization and `status` -- no `package_name` or `routine_type` filter anywhere in that path.
* `context_product_api.validate_context_product_references` and
  `list_context_product_routine_options` (naming a routine in a context product, and the picker
  that offers candidates) likewise filter only on organization, `status == "ACTIVE"` and project
  -- not `package_name`.
* `context_product_coverage.load_ontology_meaning` (what `resolve_pinned_references` calls, on
  the compile door) and `context_product_coverage.load_pinned_meaning` /
  `_ontology_coverage` (what `mcp_server`'s context-product resource read calls) both cut a
  `ROUTINE` mapping to the product's own `routine_ids` by plain set membership -- again no
  `package_name` filter.

So a package member is not a different kind of mapping target: it is a routine row, and every
door above already treats it as one. There is nothing left here to build; this test is the
evidence, run against the real production functions rather than a stand-in.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_product_coverage import load_ontology_meaning, load_pinned_meaning
from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataSchema, Organization
from aida.ontology_api import (
    OntologyCreate,
    OntologyDefinition,
    create_ontology_version,
    list_ontology_versions,
    submit_ontology_version,
)
from aida.ontology_models import OntologyVersion
from tests.support.task_agents import agent_settings, human, seed_estate, task_agent_session

ROLES = frozenset({"DataSteward"})


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _routine(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    package_name: str = "",
) -> MetadataRoutine:
    """A standalone routine when `package_name` is left empty, a package member otherwise --
    the only difference FP03 gives the two, per its row's own text."""
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        signature="(x NUMBER)",
        package_name=package_name,
        routine_type="PROCEDURE",
        body_sql_redacted="CREATE PROCEDURE p(x NUMBER) AS $$ BEGIN NULL; END; $$",
        redaction_status="LEXICAL",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return routine


def _definition(standalone_id: object, member_id: object) -> OntologyDefinition:
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
                }
            ],
            "mappings": [
                {
                    "concept": "net_revenue",
                    "subject_type": "ROUTINE",
                    "subject_id": str(standalone_id),
                },
                {
                    "concept": "net_revenue",
                    "subject_type": "ROUTINE",
                    "subject_id": str(member_id),
                },
            ],
        }
    )


async def test_a_package_member_routine_validates_identically_to_a_standalone_one(
    session: AsyncSession,
) -> None:
    """Round-trips the *validation* door: create, submit, and the version listing's per-mapping
    drift report (`resolve_mapping_targets` / `_mapping_status`)."""
    org, datasource, schema = await seed_estate(session)
    standalone = await _routine(session, org, datasource, schema, name="rebuild_net_revenue")
    member = await _routine(
        session, org, datasource, schema, name="rebuild_net_revenue", package_name="RISK_PKG"
    )
    await session.commit()

    created = await create_ontology_version(
        org.id,
        OntologyCreate(
            ontology_key="revenue", definition=_definition(standalone.id, member.id)
        ),
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
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
    by_subject = {entry.subject_id: entry for entry in row.mapping_validity}

    # The package member is not refused, not silently dropped, and not merely readable but
    # KIND_MISMATCH or TARGET_MISSING -- it is VALID, on the same datasource, exactly like the
    # standalone routine mapped in the same definition.
    assert by_subject[standalone.id].status == "VALID"
    assert by_subject[member.id].status == "VALID"
    assert by_subject[standalone.id].datasource_id == by_subject[member.id].datasource_id


async def test_a_package_member_routine_reaches_the_compile_and_mcp_doors_identically(
    session: AsyncSession,
) -> None:
    """Round-trips the *compile* door (`load_ontology_meaning`, what
    `resolve_pinned_references` calls) and the *MCP* door (`load_pinned_meaning`, what
    `mcp_server`'s context-product resource read calls) with a context product that names both
    routines -- the package member's mapping must reach both exactly as the standalone one does.
    """
    org, datasource, schema = await seed_estate(session)
    standalone = await _routine(session, org, datasource, schema, name="rebuild_net_revenue")
    member = await _routine(
        session, org, datasource, schema, name="rebuild_net_revenue", package_name="RISK_PKG"
    )
    await session.commit()

    created = await create_ontology_version(
        org.id,
        OntologyCreate(
            ontology_key="revenue", definition=_definition(standalone.id, member.id)
        ),
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
    )
    await submit_ontology_version(
        created.id,
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
    )
    approved = await session.get(OntologyVersion, created.id)
    assert approved is not None
    approved.status = "APPROVED"
    await session.commit()

    # A context product naming both routines and pinning this ontology version -- the scope a
    # real compiled or MCP-read version would carry.
    scope_routine_ids = [str(standalone.id), str(member.id)]

    # The compile door.
    (compiled,) = await load_ontology_meaning(
        session, org.id, [str(created.id)], [], scope_routine_ids
    )
    (compiled_concept,) = compiled.concepts
    compiled_subjects = {
        mapping["subject_id"] for mapping in compiled_concept["mappings"]
    }
    assert compiled_subjects == {str(standalone.id), str(member.id)}

    # The MCP door.
    (coverage,) = await load_pinned_meaning(
        session,
        org.id,
        ontology_version_ids=[str(created.id)],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        scope_table_ids=[],
        scope_routine_ids=scope_routine_ids,
    )
    assert set(coverage.routine_ids) == {str(standalone.id), str(member.id)}
