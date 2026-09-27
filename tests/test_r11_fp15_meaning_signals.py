"""R11-FP15: a retired meaning is a change signal, the same as a redefined view.

Until this, `metadata_change_signal` recorded source changes and one meaning change (an ontology
version published). An approved table, column or routine description superseded or withdrawn, or
a semantic model or glossary term version superseded, left no record at all -- so nothing could
say which products had stood on the meaning that moved. These tests pin:

* what "changed" means: a retired version is a signal when the reader is now given something
  else -- different approved text (`MEANING_REPLACED`, naming the version that replaced it) or no
  approved text (`MEANING_WITHDRAWN`). A supersession that re-approves identical text records
  nothing, on the first sweep or any later one; a draft never records anything; a superseded
  semantic model version always does, because a model's pins are by version, not by text;
* each description signal belongs to its object's source, a semantic model's or glossary term's
  to none; and a signal carries ids and codes, never the text it compared;
* the sweep is idempotent without a key, and never records one organization's retirement under
  another (INV-5);
* the processing pass sweeps and then consumes them -- PROCESSED, recorded, no hold -- and the
  rebuild pass visits an organization whose only change is meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import publish_asset_documentation_version
from aida.change_signal_meaning import record_meaning_signals
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import ACTION_RECORDED, process_change_signals
from aida.column_documentation import publish_column_description
from aida.context_rebuild import organizations_needing_rebuild, run_context_rebuild
from aida.envelope_models import MetadataRoutine
from aida.models import (
    AssetDocumentationVersion,
    DataQualityIncident,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticModelVersion,
)
from aida.routine_description_service import publish_routine_documentation_version
from tests.support.task_agents import agent_settings, seed_estate, seed_table, task_agent_session

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


async def _signals(session: AsyncSession, org: Organization) -> list[MetadataChangeSignal]:
    return list(
        await session.scalars(
            select(MetadataChangeSignal)
            .where(MetadataChangeSignal.organization_id == org.id)
            .order_by(MetadataChangeSignal.subject_kind, MetadataChangeSignal.subject_id)
            .execution_options(populate_existing=True)
        )
    )


async def _sweep(session: AsyncSession, org: Organization) -> int:
    recorded = await record_meaning_signals(session, organization_id=org.id, limit=100)
    await session.commit()
    return recorded


async def _table_description(
    session: AsyncSession, org: Organization, table: MetadataTable, readme: str, hours: int
) -> UUID:
    version = await publish_asset_documentation_version(
        session,
        organization_id=org.id,
        table_id=table.id,
        readme=readme,
        created_by="steward-1",
        approved_by="reviewer-1",
        approved_at=_at(hours),
    )
    await session.commit()
    return version.id


async def _estate(
    session: AsyncSession,
) -> tuple[Organization, DataSource, MetadataSchema, MetadataTable]:
    org, datasource, schema = await seed_estate(session)
    orders = await seed_table(session, org, datasource, schema, name="orders")
    await session.commit()
    return org, datasource, schema, orders


async def test_a_superseded_table_description_is_a_signal_only_when_the_text_moved() -> None:
    async with task_agent_session() as session:
        org, datasource, _, orders = await _estate(session)
        first = await _table_description(session, org, orders, "Orders placed online.", 1)
        # A re-review that approves the same words: the reader is given exactly what they were.
        same = await _table_description(session, org, orders, "Orders placed online.", 2)
        assert await _sweep(session, org) == 0
        assert await _signals(session, org) == []

        replacement = await _table_description(session, org, orders, "Orders, any channel.", 3)
        assert await _sweep(session, org) == 1

        (signal,) = await _signals(session, org)
        assert (
            signal.subject_kind,
            signal.subject_id,
            signal.signal_type,
            signal.change_class,
            signal.related_subject_id,
            signal.datasource_id,
            signal.analysis_run_id,
            signal.status,
        ) == (
            "TABLE_DESCRIPTION",
            same,
            "MEANING_RETIRED",
            "MEANING_REPLACED",
            replacement,
            datasource.id,
            None,
            "PENDING",
        )
        # The first version was replaced by identical words and stays unsignalled for good, even
        # now that the words it carried are no longer the current ones.
        assert first not in {row.subject_id for row in await _signals(session, org)}
        # Idempotent without a key: a second sweep finds nothing new.
        assert await _sweep(session, org) == 0
        # Value-free: nothing on the row is the text that was compared.
        assert "online" not in repr(
            {column.name: getattr(signal, column.key) for column in signal.__table__.columns}
        )


async def test_a_withdrawn_description_is_withdrawn_meaning_and_names_no_replacement() -> None:
    async with task_agent_session() as session:
        org, _, _, orders = await _estate(session)
        version_id = await _table_description(session, org, orders, "Orders placed online.", 1)
        version = await session.get(AssetDocumentationVersion, version_id)
        assert version is not None
        version.status = "WITHDRAWN"
        version.updated_at = _at(2)
        await session.commit()

        assert await _sweep(session, org) == 1
        (signal,) = await _signals(session, org)
        assert (signal.subject_id, signal.change_class, signal.related_subject_id) == (
            version_id,
            "MEANING_WITHDRAWN",
            None,
        )


async def test_column_and_routine_descriptions_signal_under_their_own_kinds() -> None:
    async with task_agent_session() as session:
        org, datasource, schema, orders = await _estate(session)
        amount = MetadataColumn(
            organization_id=org.id,
            table_id=orders.id,
            name="amount",
            ordinal_position=1,
            physical_type="numeric",
            nullable=False,
            fingerprint="fp",
        )
        routine = MetadataRoutine(
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name="rebuild_totals",
            signature="()",
            routine_type="PROCEDURE",
            body_sql_redacted="BEGIN NULL; END",
            redaction_status="PARSED",
            screening_status="CLEAN",
            fingerprint="fp",
        )
        session.add_all([amount, routine])
        await session.flush()
        column_first = await publish_column_description(
            session,
            organization_id=org.id,
            table_id=orders.id,
            column_id=amount.id,
            description="Gross amount.",
            created_by="steward-1",
            approved_by="reviewer-1",
            approved_at=_at(1),
        )
        await publish_column_description(
            session,
            organization_id=org.id,
            table_id=orders.id,
            column_id=amount.id,
            description="Net amount after discounts.",
            created_by="steward-1",
            approved_by="reviewer-1",
            approved_at=_at(2),
        )
        routine_first = await publish_routine_documentation_version(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            description="Rebuilds the totals table.",
            created_by="steward-1",
            approved_by="reviewer-1",
            approved_at=_at(1),
        )
        await publish_routine_documentation_version(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            description="Rebuilds the totals table nightly.",
            created_by="steward-1",
            approved_by="reviewer-1",
            approved_at=_at(2),
        )
        await session.commit()

        assert await _sweep(session, org) == 2
        by_kind = {signal.subject_kind: signal for signal in await _signals(session, org)}
        assert set(by_kind) == {"COLUMN_DESCRIPTION", "ROUTINE_DESCRIPTION"}
        assert by_kind["COLUMN_DESCRIPTION"].subject_id == column_first.id
        assert by_kind["ROUTINE_DESCRIPTION"].subject_id == routine_first.id
        assert {signal.datasource_id for signal in by_kind.values()} == {datasource.id}
        assert {signal.change_class for signal in by_kind.values()} == {"MEANING_REPLACED"}


async def test_semantic_models_always_and_glossary_terms_on_new_definitions_signal() -> None:
    async with task_agent_session() as session:
        org, datasource, _, _ = await _estate(session)
        project = await session.get(Project, datasource.project_id)
        assert project is not None
        term = GlossaryTerm(organization_id=org.id, term_key="net_revenue")
        session.add(term)
        await session.flush()

        def model(number: int, status: str) -> SemanticModelVersion:
            return SemanticModelVersion(
                organization_id=org.id,
                project_id=project.id,
                version=number,
                name="Revenue model",
                change_summary=f"Version {number}.",
                status=status,
                created_by="modeller",
            )

        def definition(number: int, status: str, text: str) -> GlossaryTermVersion:
            return GlossaryTermVersion(
                organization_id=org.id,
                term_id=term.id,
                version=number,
                status=status,
                display_name="Net revenue",
                definition=text,
                created_by="steward-2",
            )

        first_model, second_model = model(1, "SUPERSEDED"), model(2, "PUBLISHED")
        # v1 -> v2 re-approves the same definition (a synonym edit); v2 -> v3 changes it.
        v1 = definition(1, "SUPERSEDED", "Revenue after discounts.")
        v2 = definition(2, "SUPERSEDED", "Revenue after discounts.")
        v3 = definition(3, "APPROVED", "Revenue after discounts and returns.")
        # A draft that never reached a reader is never a signal, however it ends.
        draft = model(3, "DRAFT")
        session.add_all([first_model, second_model, v1, v2, v3, draft])
        await session.commit()

        assert await _sweep(session, org) == 2
        signals = {signal.subject_kind: signal for signal in await _signals(session, org)}
        assert set(signals) == {"SEMANTIC_MODEL", "GLOSSARY_TERM"}
        semantic = signals["SEMANTIC_MODEL"]
        assert (semantic.subject_id, semantic.related_subject_id, semantic.datasource_id) == (
            first_model.id,
            second_model.id,
            None,
        )
        glossary = signals["GLOSSARY_TERM"]
        assert (glossary.subject_id, glossary.related_subject_id) == (v2.id, v3.id)


async def test_the_sweep_never_records_another_organizations_retirement() -> None:
    async with task_agent_session() as session:
        org, _, _, orders = await _estate(session)
        other, _, _, ledger = await _estate(session)
        await _table_description(session, other, ledger, "Ledger entries.", 1)
        await _table_description(session, other, ledger, "General ledger entries.", 2)

        assert await _sweep(session, org) == 0
        assert await _signals(session, org) == []
        assert await _sweep(session, other) == 1
        assert orders.organization_id != ledger.organization_id


async def test_the_processing_pass_sweeps_then_consumes_meaning_without_a_hold() -> None:
    async with task_agent_session() as session:
        org, _, _, orders = await _estate(session)
        await _table_description(session, org, orders, "Orders placed online.", 1)
        await _table_description(session, org, orders, "Orders, any channel.", 2)

        outcome = await process_change_signals(session, organization_id=org.id, limit=100)
        await session.commit()

        assert outcome.meaning_signals_recorded == 1
        assert outcome.actions == {ACTION_RECORDED: 1}
        (signal,) = await _signals(session, org)
        assert (signal.status, signal.outcome) == ("PROCESSED", {"action": ACTION_RECORDED})
        # Meaning cannot make a query answer differently, so it never holds a tool.
        incidents = await session.scalars(
            select(DataQualityIncident).where(DataQualityIncident.organization_id == org.id)
        )
        assert list(incidents) == []
        again = await process_change_signals(session, organization_id=org.id, limit=100)
        assert (again.meaning_signals_recorded, again.processed) == (0, 0)


async def test_the_rebuild_pass_visits_an_organization_whose_only_change_is_meaning() -> None:
    async with task_agent_session() as session:
        org, _, _, orders = await _estate(session)
        await _table_description(session, org, orders, "Orders placed online.", 1)
        assert org.id not in await organizations_needing_rebuild(session)

        await _table_description(session, org, orders, "Orders, any channel.", 2)
        assert org.id in await organizations_needing_rebuild(session)

        outcome = await run_context_rebuild(session, org.id, settings=agent_settings())
        await session.commit()
        assert outcome.meaning_signals_recorded == 1
        assert outcome.acted
        # Recorded once; with no product standing on it, nothing brings the organization back.
        assert org.id not in await organizations_needing_rebuild(session)
        assert not (await run_context_rebuild(session, org.id, settings=agent_settings())).acted
