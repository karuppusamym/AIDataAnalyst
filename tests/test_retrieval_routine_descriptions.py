"""R11-FP08: an *approved* routine description is one of the words retrieval searches.

R11-FP08 gave a routine a governed, reviewed description and left it unreachable: the
ROUTINE candidate in `hybrid_retrieve` was found by its name, its parameter names and the
**source's own** comment -- the one thing a rescan can reword -- so a procedure that a
steward had described in business language still could not be found by a question asked in
business language. These tests drive the real lexical stage against in-memory SQLite and
pin the rules that make the new signal safe:

* only the **published, APPROVED** version ranks -- never an open draft, never a
  superseded version, never a withdrawn one (`R11-FP09`'s ontology discipline);
* it happens **inside the lexical stage** -- no new retrieval channel (R11-S3);
* it says **which signal matched** -- a name match and a meaning match are different
  evidence, and the grounding receipt hashes what matched;
* a routine is still **never ranked by its body**, and still carries only ACTIVE lineage;
* the hit carries a **digest**, never the prose (INV-6's discipline for hit evidence);
* every read restates `organization_id` and `datasource_id` (INV-5), proven by a peer
  datasource holding an identically-described routine that must not be returned.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDescriptionDraft,
    RoutineDocumentationVersion,
)
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.retrieval import hybrid_retrieve
from aida.routine_description_service import publish_routine_documentation_version

#: The description-only routine's name shares no token with this question: if it comes
#: back at all, it came back through its approved description.
MEANING_QUESTION = "closing balance for deposit account"
#: This one's name matches; the approved description adds the one word it lacks.
NAME_AND_MEANING_QUESTION = "rebuild revenue rollup by customer"

APPROVED_TEXT = "Recomputes the closing balance held for every deposit account overnight."
ROLLUP_TEXT = "Rebuilds the revenue rollup for each customer."


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


def _settings() -> Settings:
    return Settings(_env_file=None)


def _datasource(
    org: Organization, lob: LineOfBusiness, domain: DataDomain, project: Project, name: str
) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )


async def _routine(
    session: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    name: str,
    *,
    source_description: str | None = None,
    body: str = "BEGIN NULL; END;",
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        signature="(date)",
        routine_type="PROCEDURE",
        language="plpgsql",
        body_sql_redacted=body,
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        source_description=source_description,
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return routine


async def _publish(
    session: AsyncSession, routine: MetadataRoutine, description: str
) -> RoutineDocumentationVersion:
    """Publish through the real approval path, not a hand-built row."""
    return await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description=description,
        created_by="steward@bank.example",
        approved_by="lead@bank.example",
        approved_at=datetime(2026, 9, 17, tzinfo=UTC),
    )


async def _seed(session: AsyncSession) -> dict[str, object]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Finance",
        code=f"F{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = _datasource(org, lob, domain, project, "primary")
    elsewhere = _datasource(org, lob, domain, project, "elsewhere")
    session.add_all([org, lob, domain, project, datasource, elsewhere])
    await session.flush()

    schemas: dict[object, MetadataSchema] = {}
    for source in (datasource, elsewhere):
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=source.id,
            name="bank",
            fingerprint="fp",
        )
        session.add(catalog)
        await session.flush()
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=org.id,
            catalog_id=catalog.id,
            name="ops",
            fingerprint="fp",
        )
        session.add(schema)
        await session.flush()
        schemas[source.id] = schema
    schema = schemas[datasource.id]

    tables: dict[str, MetadataTable] = {}
    for name in ("ledger_entries", "acct_positions", "undecided_audit"):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint="fp",
        )
        session.add(table)
        tables[name] = table
    await session.flush()

    # The routine whose *only* reachable words are its approved description's. Its body
    # is full of words the question uses, so a body-ranked hit would be visible.
    described = await _routine(
        session,
        datasource,
        schema,
        "sp_xk4_ovn",
        body="BEGIN closing balance deposit accounts; END;",
    )
    await _publish(session, described, APPROVED_TEXT)

    # ACTIVE lineage for the described routine; a PROPOSED edge that must steer nothing.
    for target, review_status in (("acct_positions", "ACTIVE"), ("undecided_audit", "PROPOSED")):
        session.add(
            DeepProcedureLineageEdge(
                id=uuid4(),
                organization_id=org.id,
                datasource_id=datasource.id,
                routine_id=described.id,
                statement_ordinal=1,
                source_table="ops.ledger_entries",
                source_column="amount",
                target_table=f"ops.{target}",
                target_column="amount",
                source_resolved=True,
                source_table_id=tables["ledger_entries"].id,
                target_table_id=tables[target].id,
                transformation_type="DIRECT",
                confidence="FULL",
                dialect="postgres",
                is_write=True,
                is_intermediate=False,
                sql_hash="h",
                review_status=review_status,
            )
        )

    # A routine found by its name, whose approved description supplies the one word the
    # name lacks -- the "the description strengthens a name match" case.
    rollup = await _routine(session, datasource, schema, "rebuild_revenue_rollup")

    # The same meaning, published in a *peer datasource*: INV-5's scope, proven.
    peer = await _routine(session, elsewhere, schemas[elsewhere.id], "sp_peer_ovn")
    await _publish(session, peer, APPROVED_TEXT)

    await session.commit()
    return {
        "datasource": datasource,
        "elsewhere": elsewhere,
        "described": described,
        "rollup": rollup,
        "peer": peer,
        "tables": tables,
        "schema": schema,
    }


async def _routine_hits(session: AsyncSession, datasource: DataSource, question: str) -> list:
    hits = await hybrid_retrieve(
        session, datasource=datasource, question=question, settings=_settings()
    )
    return [hit for hit in hits if hit.object_type == "ROUTINE"]


@pytest.mark.asyncio
async def test_an_approved_description_is_the_only_thing_that_finds_this_routine(
    session: AsyncSession,
) -> None:
    """`sp_xk4_ovn` shares no word with the question. Before R11-FP08 it was never
    fetched, so its reviewed description could not score however well it was written."""
    seeded = await _seed(session)

    hits = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)

    assert [hit.object_id for hit in hits] == [str(seeded["described"].id)]
    (hit,) = hits
    assert hit.display_name == "ops.sp_xk4_ovn"
    assert hit.score > 0


@pytest.mark.asyncio
async def test_the_hit_says_a_meaning_matched_not_a_name(session: AsyncSession) -> None:
    """A name match and a meaning match are different evidence. The routine reached only
    through its description must not claim `BM25_ROUTINE_NAME`."""
    seeded = await _seed(session)

    (hit,) = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)

    assert "BM25_ROUTINE_DESCRIPTION" in hit.reason_codes
    assert "ROUTINE_DESCRIPTION_APPROVED" in hit.reason_codes
    assert "BM25_ROUTINE_NAME" not in hit.reason_codes


@pytest.mark.asyncio
async def test_the_hit_carries_a_digest_of_the_description_never_the_prose(
    session: AsyncSession,
) -> None:
    """INV-6's discipline for hit evidence, the one `definition_digest` already carries:
    a receipt can prove *which* text matched without the text riding in the evidence."""
    seeded = await _seed(session)

    (hit,) = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)

    expected = hashlib.sha256(APPROVED_TEXT.encode("utf-8")).hexdigest()
    assert hit.metadata["description_digest"] == expected
    assert hit.metadata["description_version"] == 1
    assert UUID(hit.metadata["description_version_id"])
    rendered = " ".join(str(value) for value in hit.metadata.values())
    for fragment in ("Recomputes", "closing balance held", "deposit account"):
        assert fragment not in rendered


@pytest.mark.asyncio
async def test_a_description_only_hit_still_carries_only_active_lineage(
    session: AsyncSession,
) -> None:
    """The rule the ROUTINE candidate already carried has to survive the new way in:
    an agent's undecided PROPOSED edge steers nothing, whichever signal found the
    routine, and the routine itself is never offered as something to call."""
    seeded = await _seed(session)
    tables = seeded["tables"]

    (hit,) = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)

    assert hit.metadata["reads_table_ids"] == [str(tables["ledger_entries"].id)]
    assert hit.metadata["writes_table_ids"] == [str(tables["acct_positions"].id)]
    assert str(tables["undecided_audit"].id) not in hit.metadata["writes_table_ids"]
    assert "GOVERNED_TOOL_BOOST" not in hit.reason_codes


@pytest.mark.asyncio
async def test_a_routine_is_still_never_ranked_by_its_body(session: AsyncSession) -> None:
    """`sp_xk4_ovn`'s body contains the question's words verbatim. The only reason it is
    found is the approved description; a body-ranked platform would also return the
    routine below, whose body says the same thing and whose description does not exist."""
    seeded = await _seed(session)
    await _routine(
        session,
        seeded["datasource"],
        seeded["schema"],
        "sp_zz9_body_only",
        body="BEGIN closing balance deposit accounts; END;",
    )
    await session.commit()

    hits = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)

    assert [hit.object_id for hit in hits] == [str(seeded["described"].id)]


@pytest.mark.asyncio
async def test_an_approved_description_strengthens_a_routine_found_by_name(
    session: AsyncSession,
) -> None:
    """The other direction: `rebuild_revenue_rollup` already matched three of the four
    words. Publishing the description that supplies the fourth must raise its score --
    that is what "the description joins retrieval" means for a routine already found."""
    seeded = await _seed(session)
    rollup = seeded["rollup"]

    (before,) = [
        hit
        for hit in await _routine_hits(
            session, seeded["datasource"], NAME_AND_MEANING_QUESTION
        )
        if hit.object_id == str(rollup.id)
    ]
    assert before.reason_codes == ["BM25_ROUTINE_NAME"]

    await _publish(session, rollup, ROLLUP_TEXT)
    await session.commit()

    (after,) = [
        hit
        for hit in await _routine_hits(
            session, seeded["datasource"], NAME_AND_MEANING_QUESTION
        )
        if hit.object_id == str(rollup.id)
    ]
    assert after.score > before.score
    assert after.reason_codes == [
        "BM25_ROUTINE_NAME",
        "BM25_ROUTINE_DESCRIPTION",
        "ROUTINE_DESCRIPTION_APPROVED",
    ]


@pytest.mark.asyncio
async def test_an_open_draft_never_ranks(session: AsyncSession) -> None:
    """A draft awaiting review is a proposal nobody has decided. Ranking it would let an
    unreviewed sentence steer an answer -- exactly what `R11-FP09` refuses for a draft
    ontology, and what the review gate on `RoutineDescriptionDraft` exists for."""
    seeded = await _seed(session)
    undescribed = await _routine(session, seeded["datasource"], seeded["schema"], "sp_qq1_new")
    session.add(
        RoutineDescriptionDraft(
            id=uuid4(),
            organization_id=undescribed.organization_id,
            datasource_id=undescribed.datasource_id,
            routine_id=undescribed.id,
            drafted_text="Settles the pending wire remittance queue every hour.",
            text_fingerprint="f" * 64,
            accuracy_score=0.9,
            clarity_score=0.9,
            style_score=0.9,
            completeness_score=0.9,
            overall_score=0.9,
            evidence={},
            status="PENDING_APPROVAL",
            created_by="steward@bank.example",
        )
    )
    await session.commit()

    hits = await _routine_hits(session, seeded["datasource"], "pending wire remittance queue")

    assert hits == []


@pytest.mark.asyncio
async def test_a_superseded_version_stops_ranking_when_the_next_one_is_approved(
    session: AsyncSession,
) -> None:
    """Append-only means the old text is still there. It is not what Atlas asserts, so
    the words only the superseded version had must stop finding the routine, and the
    words the new one has must start."""
    seeded = await _seed(session)
    described = seeded["described"]

    await _publish(session, described, "Reconciles the custodian sweep instruction file.")
    await session.commit()

    assert await _routine_hits(session, seeded["datasource"], MEANING_QUESTION) == []
    (hit,) = await _routine_hits(session, seeded["datasource"], "custodian sweep instruction")
    assert hit.object_id == str(described.id)
    assert hit.metadata["description_version"] == 2


@pytest.mark.asyncio
async def test_a_withdrawn_description_stops_ranking(session: AsyncSession) -> None:
    """A reviewer retiring a description is a statement that the platform no longer
    asserts it. Retrieval has to stop reading it, or a withdrawal would remove the text
    from every read surface except the one that steers answers."""
    seeded = await _seed(session)
    # The state `description_withdrawal` leaves behind, set directly so this test pins
    # the retrieval rule rather than re-driving the withdrawal's own governance flow.
    await session.execute(
        update(RoutineDocumentationVersion)
        .where(RoutineDocumentationVersion.status == "APPROVED")
        .values(status="WITHDRAWN")
    )
    await session.commit()

    assert await _routine_hits(session, seeded["datasource"], MEANING_QUESTION) == []


@pytest.mark.asyncio
async def test_a_peer_datasources_description_is_out_of_scope(session: AsyncSession) -> None:
    """INV-5: every read restates `organization_id` *and* `datasource_id`. `sp_peer_ovn`
    carries the identical approved text in the other datasource; asking this one must not
    return it, and asking that one must not return this one's."""
    seeded = await _seed(session)

    here = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)
    there = await _routine_hits(session, seeded["elsewhere"], MEANING_QUESTION)

    assert [hit.object_id for hit in here] == [str(seeded["described"].id)]
    assert [hit.object_id for hit in there] == [str(seeded["peer"].id)]


@pytest.mark.asyncio
async def test_the_description_signal_adds_no_retrieval_channel(session: AsyncSession) -> None:
    """R11-S3 is DEFERRED to stop channels multiplying before retrieval quality is
    measured. The approved description is a lexical signal inside the lexical stage, so
    the enhanced pipeline's channel list is unchanged -- proven by the hit arriving from
    `hybrid_retrieve` itself, under a `BM25_*` reason code."""
    seeded = await _seed(session)

    (hit,) = await _routine_hits(session, seeded["datasource"], MEANING_QUESTION)

    assert [code for code in hit.reason_codes if code.startswith("BM25_")] == [
        "BM25_ROUTINE_DESCRIPTION"
    ]
