"""R11-FP11: a view is reachable by what its definition is built from.

`hybrid_retrieve` scored a table on its name and source description, and a view on its name alone,
while the definition Atlas already holds -- value-free and screened -- said exactly which columns
and tables the view stands on. A view named `v_rev_ltd` answered nothing. Worse, a table whose
description used the question's words was never even fetched: the candidate query filtered on name.

These tests drive the real retrieval against in-memory SQLite and pin:

* a view whose definition names the question's words is found, scored below a name match, and
  carries a digest of the definition rather than its text;
* a definition the screening quarantined, or one the source withheld, is never scored;
* a table found only by its description is fetched and says so in its reason code.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from aida.envelope_models import MetadataViewDefinition
from aida.models import DataSource, MetadataTable
from aida.retrieval import hybrid_retrieve
from tests.support.task_agents import agent_settings, seed_estate, seed_table, task_agent_session

QUESTION = "net revenue by customer"
DEFINITION = (
    "SELECT c.customer_id, SUM(o.net_revenue) AS net_revenue "
    "FROM public.customers c JOIN public.orders o ON o.customer_id = c.customer_id "
    "GROUP BY c.customer_id"
)


async def _definition(
    session: object,
    datasource: DataSource,
    table: MetadataTable,
    *,
    screening_status: str = "CLEAN",
    availability: str = "AVAILABLE",
    redaction_status: str = "PARSED",
) -> MetadataViewDefinition:
    withheld = availability != "AVAILABLE"
    definition = MetadataViewDefinition(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        # The model refuses text on a definition the source withheld, as it should.
        definition_sql_redacted=None if withheld else DEFINITION,
        unavailable_reason="the source withheld this definition" if withheld else None,
        definition_fingerprint="a" * 64,
        redaction_status=redaction_status,
        screening_status=screening_status,
        availability=availability,
        fingerprint="fp",
    )
    session.add(definition)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    return definition


async def test_a_view_is_found_by_what_its_definition_is_built_from() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        view = await seed_table(
            session, org, datasource, schema, name="v_rev_ltd", object_type="VIEW"
        )
        named = await seed_table(session, org, datasource, schema, name="net_revenue_by_customer")
        await _definition(session, datasource, view)
        await session.commit()

        hits = await hybrid_retrieve(
            session, datasource=datasource, question=QUESTION, settings=agent_settings()
        )

    by_id = {hit.object_id: hit for hit in hits}
    definition_hit = by_id[str(view.id)]
    assert definition_hit.reason_codes == ["BM25_VIEW_DEFINITION"]
    assert definition_hit.metadata["definition_digest"] == hashlib.sha256(
        DEFINITION.encode("utf-8")
    ).hexdigest()
    assert "SELECT" not in str(definition_hit.metadata), "the text itself never travels"
    assert definition_hit.score < by_id[str(named.id)].score, "a name match is stronger evidence"


async def test_a_definition_that_may_not_be_read_is_never_scored() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        quarantined = await seed_table(
            session, org, datasource, schema, name="v_one", object_type="VIEW"
        )
        withheld = await seed_table(
            session, org, datasource, schema, name="v_two", object_type="VIEW"
        )
        await _definition(session, datasource, quarantined, screening_status="QUARANTINED")
        await _definition(session, datasource, withheld, availability="UNAVAILABLE")
        await session.commit()

        hits = await hybrid_retrieve(
            session, datasource=datasource, question=QUESTION, settings=agent_settings()
        )

    assert {hit.object_id for hit in hits} == set()


async def test_a_table_is_fetched_by_its_source_description_alone() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        described = await seed_table(session, org, datasource, schema, name="t_2f7")
        described.source_description = "Net revenue by customer, as the source describes it"
        await session.flush()
        await session.commit()

        hits = await hybrid_retrieve(
            session, datasource=datasource, question=QUESTION, settings=agent_settings()
        )

    (hit,) = [item for item in hits if item.object_id == str(described.id)]
    assert hit.reason_codes == ["BM25_TABLE_DESCRIPTION"]
    assert hit.score > 0


async def test_the_definition_of_a_view_outside_the_datasource_is_not_read() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        _, elsewhere, elsewhere_schema = await seed_estate(session, organization=org)
        foreign = await seed_table(
            session, org, elsewhere, elsewhere_schema, name="v_rev_ltd", object_type="VIEW"
        )
        await _definition(session, elsewhere, foreign)
        await session.commit()

        hits = await hybrid_retrieve(
            session, datasource=datasource, question=QUESTION, settings=agent_settings()
        )
        remaining = (await session.scalars(select(MetadataViewDefinition))).all()

    assert hits == [] and len(remaining) == 1
