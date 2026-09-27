"""R11-FP08: the documentation worklist ranks routines, not only tables.

AT-5 is `table_id`-keyed from signal to response, so a stored procedure nobody has
described -- exactly the gap the worklist exists to rank -- could not appear on it. A
governed routine description that no worklist ever asks anyone to write is an artifact
nobody reads, so this closes the other half of R11-FP08's remainder.

Two halves, tested where each lives:

* the **pure ranker** (`documentation_worklist.rank_routine_documentation_worklist`),
  DB-free, following `test_documentation_worklist.py`'s own convention -- and pinning
  that a routine's terms are *not* the table's: usage is borrowed from the tables it
  writes, impact is write reach, deficit is its own one-field checklist;
* the **gather plus endpoint** (`documentation_worklist_signals`,
  `stewardship_api.list_documentation_worklist`) against in-memory SQLite, pinning that
  only ACTIVE lineage counts, that an approved description takes a routine off the list,
  that an open draft keeps it on with the flag, and that `PACKAGE` never reaches it.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.documentation_worklist import (
    PACKAGE_NOT_RANKABLE,
    ROUTINE_DEFICIT_FIELDS,
    RoutineDocumentationSignal,
    RoutineNotRankable,
    rank_routine_documentation_worklist,
)
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDescriptionDraft,
    RoutineDocumentationVersion,
)
from aida.models import (
    ConsumptionRecord,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QueryExecution,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_description_service import publish_routine_documentation_version
from aida.stewardship_api import list_documentation_worklist
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings()
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The pure ranking
# ---------------------------------------------------------------------------


def _signal(
    *,
    routine_id: UUID | None = None,
    routine_name: str = "sp_thing",
    routine_type: str = "PROCEDURE",
    written_table_query_volume: int = 0,
    writes_table_count: int = 0,
    reads_table_count: int = 0,
    is_documented: bool = False,
    description_is_proposed: bool = False,
) -> RoutineDocumentationSignal:
    return RoutineDocumentationSignal(
        routine_id=routine_id or uuid4(),
        routine_name=routine_name,
        schema_name="ops",
        datasource_name="warehouse",
        routine_type=routine_type,
        written_table_query_volume=written_table_query_volume,
        writes_table_count=writes_table_count,
        reads_table_count=reads_table_count,
        is_documented=is_documented,
        description_is_proposed=description_is_proposed,
    )


async def test_a_described_routine_is_excluded_whatever_it_writes() -> None:
    """This is meant to *be* the worklist, not a catalog view with a column -- the
    table ranking's own rule."""
    described = _signal(
        routine_name="sp_described", written_table_query_volume=500, is_documented=True
    )
    undescribed = _signal(routine_name="sp_undescribed", written_table_query_volume=1)

    entries, total = rank_routine_documentation_worklist([described, undescribed], limit=10)

    assert total == 1
    assert [entry.routine_name for entry in entries] == ["sp_undescribed"]


async def test_a_routine_is_ranked_by_the_traffic_of_what_it_writes() -> None:
    """The whole point of a routine-shaped signal. Neither routine is queried -- a
    table-shaped ranking would score both at zero and order them by name. What
    separates them is that one produces a table many questions read."""
    hot = _signal(
        routine_name="sp_writes_hot", written_table_query_volume=90, writes_table_count=1
    )
    cold = _signal(
        routine_name="sp_writes_cold", written_table_query_volume=2, writes_table_count=1
    )

    entries, _ = rank_routine_documentation_worklist([cold, hot], limit=10)

    assert [entry.routine_name for entry in entries] == ["sp_writes_hot", "sp_writes_cold"]
    assert entries[0].written_table_query_volume == 90


async def test_write_reach_is_the_impact_term_not_a_foreign_key_count() -> None:
    """A routine has no foreign keys pointed at it; what it has is fan-out. Same
    borrowed volume, more tables produced -> ranked first."""
    wide = _signal(
        routine_name="sp_wide", written_table_query_volume=30, writes_table_count=3
    )
    narrow = _signal(
        routine_name="sp_narrow", written_table_query_volume=30, writes_table_count=1
    )

    entries, _ = rank_routine_documentation_worklist([narrow, wide], limit=10)

    assert [entry.routine_name for entry in entries] == ["sp_wide", "sp_narrow"]
    assert entries[0].impact > entries[1].impact


async def test_a_routines_deficit_is_its_own_one_field_checklist() -> None:
    """SW-1's five `DEFICIT_FIELDS` are table-shaped. A routine has no ownership
    assignment, certification, glossary link or quality policy, so claiming four more
    missing fields would inflate every routine's urgency against every table's."""
    entries, _ = rank_routine_documentation_worklist(
        [_signal(written_table_query_volume=10, writes_table_count=1)], limit=10
    )

    assert ROUTINE_DEFICIT_FIELDS == ("description",)
    assert entries[0].deficit == pytest.approx(0.2)


async def test_a_package_is_refused_by_name_not_skipped() -> None:
    """`PACKAGE` is out of scope everywhere else in this codebase and is out of scope
    here. A steward who asks why packages never appear and gets an empty result has
    learned nothing about why."""
    package = _signal(routine_name="pkg_settlement", routine_type="PACKAGE")
    procedure = _signal(routine_name="sp_ok", written_table_query_volume=5)

    with pytest.raises(RoutineNotRankable) as refusal:
        rank_routine_documentation_worklist([procedure, package], limit=10)

    assert refusal.value.code == PACKAGE_NOT_RANKABLE
    assert refusal.value.routine_ids == (package.routine_id,)
    assert "container for subprograms" in str(refusal.value)


async def test_a_lowercase_package_kind_is_refused_too() -> None:
    """The comparison a connector's casing must not get past -- the rule
    `is_describable_routine` applies to the same column."""
    with pytest.raises(RoutineNotRankable):
        rank_routine_documentation_worklist([_signal(routine_type="package")], limit=10)


async def test_a_routine_whose_written_tables_are_untouched_is_held_back() -> None:
    """No real signal to rank it by, the reason a zero-volume table is held back. Opting
    in sorts it last with no special case: usage is a term of a product."""
    quiet = _signal(routine_name="sp_quiet", writes_table_count=2)
    busy = _signal(routine_name="sp_busy", written_table_query_volume=7, writes_table_count=1)

    excluded, excluded_total = rank_routine_documentation_worklist([quiet, busy], limit=10)
    included, included_total = rank_routine_documentation_worklist(
        [quiet, busy], limit=10, include_zero_volume=True
    )

    assert (excluded_total, [e.routine_name for e in excluded]) == (1, ["sp_busy"])
    assert included_total == 2
    assert [entry.routine_name for entry in included] == ["sp_busy", "sp_quiet"]
    assert included[1].score == 0.0


async def test_an_open_draft_keeps_the_routine_on_the_list_and_says_so() -> None:
    """The table rule, unchanged: nothing has been approved, so the gap is real and a
    steward should still see it. The flag is what the consumer that must not open a
    *second* draft reads -- `uq_routine_description_draft_open` allows exactly one."""
    proposed = _signal(written_table_query_volume=4, description_is_proposed=True)

    entries, total = rank_routine_documentation_worklist([proposed], limit=10)

    assert total == 1
    assert entries[0].description_is_proposed is True


async def test_ties_break_deterministically_for_stable_pagination() -> None:
    first = _signal(
        routine_id=UUID(int=1),
        routine_name="sp_same",
        written_table_query_volume=5,
        writes_table_count=1,
    )
    second = _signal(
        routine_id=UUID(int=2),
        routine_name="sp_same",
        written_table_query_volume=5,
        writes_table_count=1,
    )

    forwards, _ = rank_routine_documentation_worklist([first, second], limit=10)
    backwards, _ = rank_routine_documentation_worklist([second, first], limit=10)

    assert [entry.routine_id for entry in forwards] == [UUID(int=1), UUID(int=2)]
    assert [entry.routine_id for entry in backwards] == [UUID(int=1), UUID(int=2)]


async def test_every_ranking_factor_is_on_the_entry() -> None:
    """TL-6/CN-7's convention: "why is this first" is answerable from the response."""
    entries, _ = rank_routine_documentation_worklist(
        [
            _signal(
                written_table_query_volume=12, writes_table_count=2, reads_table_count=4
            )
        ],
        limit=10,
    )

    entry = entries[0]
    assert (entry.rank, entry.written_table_query_volume) == (1, 12)
    assert (entry.writes_table_count, entry.reads_table_count) == (2, 4)
    assert entry.score == pytest.approx(entry.usage * entry.impact * entry.deficit, abs=1e-6)


async def test_paging_reports_the_total_across_the_whole_candidate_set() -> None:
    signals = [
        _signal(routine_name=f"sp_{index}", written_table_query_volume=index + 1)
        for index in range(5)
    ]

    page, total = rank_routine_documentation_worklist(signals, limit=2, offset=2)

    assert total == 5
    assert [entry.rank for entry in page] == [3, 4]


# ---------------------------------------------------------------------------
# The gather and the endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_datasource(session: AsyncSession) -> DataSource:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ops",
        code=f"OPS{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"src-{uuid4().hex[:8]}",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
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
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return datasource


async def _seed_table(session: AsyncSession, datasource: DataSource, name: str) -> MetadataTable:
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=datasource._test_schema.id,  # type: ignore[attr-defined]
        name=name,
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


async def _seed_routine(
    session: AsyncSession,
    datasource: DataSource,
    name: str,
    *,
    routine_type: str = "PROCEDURE",
    status: str = "ACTIVE",
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=datasource._test_schema.id,  # type: ignore[attr-defined]
        name=name,
        signature="()",
        routine_type=routine_type,
        language="plpgsql",
        body_sql_redacted="BEGIN NULL; END;",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        status=status,
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return routine


async def _seed_write_edge(
    session: AsyncSession,
    routine: MetadataRoutine,
    source: MetadataTable,
    target: MetadataTable,
    *,
    review_status: str = "ACTIVE",
    is_intermediate: bool = False,
) -> None:
    session.add(
        DeepProcedureLineageEdge(
            id=uuid4(),
            organization_id=routine.organization_id,
            datasource_id=routine.datasource_id,
            routine_id=routine.id,
            statement_ordinal=1,
            source_table=f"ops.{source.name}",
            source_column="amount",
            target_table=f"ops.{target.name}",
            target_column="amount",
            source_resolved=True,
            source_table_id=source.id,
            target_table_id=None if is_intermediate else target.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            is_write=True,
            is_intermediate=is_intermediate,
            sql_hash="h",
            review_status=review_status,
        )
    )
    await session.flush()


async def _seed_reads(
    session: AsyncSession, datasource: DataSource, table: MetadataTable, *, times: int
) -> None:
    """Real MCP consumption reads -- `resource_id` is already the table id."""
    for _index in range(times):
        session.add(
            ConsumptionRecord(
                id=uuid4(),
                organization_id=datasource.organization_id,
                consumer_id="mcp-client",
                consumer_type="AGENT",
                resource_type="metadata_table",
                resource_id=str(table.id),
                channel="MCP",
                correlation_id=uuid4().hex,
                policy_decision="ALLOW",
                consumed_at=_NOW,
            )
        )
    await session.flush()


async def _seed_execution(
    session: AsyncSession, datasource: DataSource, table_name: str, *, times: int
) -> None:
    for _ in range(times):
        session.add(
            QueryExecution(
                id=uuid4(),
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                principal_id="analyst@bank.example",
                status="COMPLETED",
                dialect=datasource.dialect,
                sql_hash="deadbeef" * 8,
                referenced_tables=[f"ops.{table_name}"],
                created_at=_NOW,
            )
        )
    await session.flush()


def _context(datasource: DataSource) -> object:
    return security_context(organization_id=datasource.organization_id)


async def _routines(
    session: AsyncSession,
    datasource: DataSource,
    *,
    include_zero_volume: bool = False,
    limit: int = 100,
):
    page = await list_documentation_worklist(
        datasource.organization_id,
        subject_type="ROUTINE",
        limit=limit,
        offset=0,
        include_zero_volume=include_zero_volume,
        ranking="priority",  # type: ignore[arg-type]
        context=_context(datasource),  # type: ignore[arg-type]
        session=session,
        settings=_SETTINGS,
    )
    return page


async def _tables(session: AsyncSession, datasource: DataSource):
    return await list_documentation_worklist(
        datasource.organization_id,
        limit=100,
        offset=0,
        include_zero_volume=False,
        ranking="priority",  # type: ignore[arg-type]
        context=_context(datasource),  # type: ignore[arg-type]
        session=session,
        settings=_SETTINGS,
    )


async def _seed_estate(session: AsyncSession) -> dict[str, object]:
    """One hot table produced by one procedure, one quiet table produced by another."""
    datasource = await _seed_datasource(session)
    ledger = await _seed_table(session, datasource, "ledger_entries")
    positions = await _seed_table(session, datasource, "acct_positions")
    archive = await _seed_table(session, datasource, "acct_archive")

    # `acct_positions` is read by real questions; `acct_archive` by nobody.
    await _seed_execution(session, datasource, "acct_positions", times=6)
    await _seed_reads(session, datasource, positions, times=4)

    producer = await _seed_routine(session, datasource, "sp_nightly_positions")
    await _seed_write_edge(session, producer, ledger, positions)
    archiver = await _seed_routine(session, datasource, "sp_archive_sweep")
    await _seed_write_edge(session, archiver, ledger, archive)
    await session.commit()
    return {
        "datasource": datasource,
        "producer": producer,
        "archiver": archiver,
        "positions": positions,
        "ledger": ledger,
    }


async def test_the_endpoint_ranks_the_routine_that_produces_a_hot_table(
    session: AsyncSession,
) -> None:
    """Nobody queries either procedure. The one that writes the table real questions
    read is the one a steward should describe next -- and before R11-FP08 the worklist
    could not say so, because it had no row shape for a routine at all."""
    seeded = await _seed_estate(session)

    page = await _routines(session, seeded["datasource"])

    assert page.total == 1
    (item,) = page.items
    assert item.subject_type == "ROUTINE"
    assert item.routine_id == seeded["producer"].id
    assert item.routine_name == "sp_nightly_positions"
    assert item.schema_name == "ops"
    assert item.written_table_query_volume == 10
    assert item.writes_table_count == 1
    assert item.reads_table_count == 1
    assert item.rank == 1
    assert item.score > 0


async def test_the_table_list_is_unchanged_and_carries_no_routines(
    session: AsyncSession,
) -> None:
    """`subject_type` defaults to `TABLE`, so every existing caller sees exactly what it
    saw before: table rows, ranked by their own measured volume."""
    seeded = await _seed_estate(session)

    page = await _tables(session, seeded["datasource"])

    assert [item.table_name for item in page.items] == ["acct_positions"]
    assert all(hasattr(item, "table_id") for item in page.items)
    assert not any(hasattr(item, "routine_id") for item in page.items)


async def test_only_active_lineage_lends_a_routine_its_usage(session: AsyncSession) -> None:
    """An agent's PROPOSED edge is a proposal nobody has decided. It must not decide what
    a steward is told to document next any more than it steers an answer."""
    seeded = await _seed_estate(session)
    datasource = seeded["datasource"]
    proposer = await _seed_routine(session, datasource, "sp_proposed_only")
    await _seed_write_edge(
        session,
        proposer,
        seeded["ledger"],
        seeded["positions"],
        review_status="PROPOSED",
    )
    await session.commit()

    page = await _routines(session, datasource)

    assert [item.routine_name for item in page.items] == ["sp_nightly_positions"]


async def test_an_intermediate_edge_lends_nothing(session: AsyncSession) -> None:
    """An intermediate edge names a temp table, which is not a thing to describe or to
    borrow traffic from."""
    seeded = await _seed_estate(session)
    datasource = seeded["datasource"]
    temp_writer = await _seed_routine(session, datasource, "sp_temp_writer")
    await _seed_write_edge(
        session, temp_writer, seeded["ledger"], seeded["positions"], is_intermediate=True
    )
    await session.commit()

    page = await _routines(session, datasource)

    assert [item.routine_name for item in page.items] == ["sp_nightly_positions"]


async def test_an_approved_description_takes_the_routine_off_the_worklist(
    session: AsyncSession,
) -> None:
    seeded = await _seed_estate(session)
    producer = seeded["producer"]

    await publish_routine_documentation_version(
        session,
        organization_id=producer.organization_id,
        datasource_id=producer.datasource_id,
        routine_id=producer.id,
        description="Rebuilds each account's closing position from the ledger overnight.",
        created_by="steward@bank.example",
        approved_by="lead@bank.example",
        approved_at=_NOW,
    )
    await session.commit()

    page = await _routines(session, seeded["datasource"])

    assert page.total == 0


async def test_withdrawing_a_description_puts_the_routine_back(session: AsyncSession) -> None:
    """That is the point of retiring a description. "Documented" is one APPROVED version
    and nothing else, so a WITHDRAWN one brings the gap back with no second rule."""
    seeded = await _seed_estate(session)
    producer = seeded["producer"]
    await publish_routine_documentation_version(
        session,
        organization_id=producer.organization_id,
        datasource_id=producer.datasource_id,
        routine_id=producer.id,
        description="Rebuilds each account's closing position from the ledger overnight.",
        created_by="steward@bank.example",
        approved_by="lead@bank.example",
        approved_at=_NOW,
    )
    await session.execute(
        update(RoutineDocumentationVersion)
        .where(RoutineDocumentationVersion.status == "APPROVED")
        .values(status="WITHDRAWN")
    )
    await session.commit()

    page = await _routines(session, seeded["datasource"])

    assert [item.routine_id for item in page.items] == [producer.id]


async def test_an_open_draft_stays_on_the_worklist_flagged(session: AsyncSession) -> None:
    """Nothing has been approved, so the gap is real. The flag is what the consumer that
    would otherwise open a second draft against
    `uq_routine_description_draft_open` reads."""
    seeded = await _seed_estate(session)
    producer = seeded["producer"]
    session.add(
        RoutineDescriptionDraft(
            id=uuid4(),
            organization_id=producer.organization_id,
            datasource_id=producer.datasource_id,
            routine_id=producer.id,
            drafted_text="Rebuilds each account's closing position from the ledger.",
            text_fingerprint="a" * 64,
            accuracy_score=0.8,
            clarity_score=0.8,
            style_score=0.8,
            completeness_score=0.8,
            overall_score=0.8,
            evidence={},
            status="PENDING_APPROVAL",
            created_by="steward@bank.example",
        )
    )
    await session.commit()

    page = await _routines(session, seeded["datasource"])

    (item,) = page.items
    assert item.routine_id == producer.id
    assert item.description_is_proposed is True


async def test_a_package_never_reaches_the_worklist(session: AsyncSession) -> None:
    """Excluded in SQL on a discovery surface, the way `footprint_gaps` excludes it --
    and refused by name in the pure ranker, so the exclusion is provable rather than a
    silent skip."""
    seeded = await _seed_estate(session)
    datasource = seeded["datasource"]
    package = await _seed_routine(
        session, datasource, "pkg_settlement", routine_type="PACKAGE"
    )
    await _seed_write_edge(session, package, seeded["ledger"], seeded["positions"])
    await session.commit()

    page = await _routines(session, datasource, include_zero_volume=True)

    assert package.id not in [item.routine_id for item in page.items]
    assert "pkg_settlement" not in [item.routine_name for item in page.items]


async def test_a_deprecated_routine_never_reaches_the_worklist(session: AsyncSession) -> None:
    """Only ACTIVE routines: asking a steward to describe a routine the source has
    dropped is work with no object."""
    seeded = await _seed_estate(session)
    datasource = seeded["datasource"]
    gone = await _seed_routine(session, datasource, "sp_retired", status="DEPRECATED")
    await _seed_write_edge(session, gone, seeded["ledger"], seeded["positions"])
    await session.commit()

    page = await _routines(session, datasource, include_zero_volume=True)

    assert gone.id not in [item.routine_id for item in page.items]


async def test_zero_volume_routines_are_opt_in(session: AsyncSession) -> None:
    seeded = await _seed_estate(session)

    default_page = await _routines(session, seeded["datasource"])
    opted_in = await _routines(session, seeded["datasource"], include_zero_volume=True)

    assert [item.routine_name for item in default_page.items] == ["sp_nightly_positions"]
    assert [item.routine_name for item in opted_in.items] == [
        "sp_nightly_positions",
        "sp_archive_sweep",
    ]
    assert opted_in.items[1].written_table_query_volume == 0


async def test_another_organizations_routines_are_out_of_scope(session: AsyncSession) -> None:
    """The cross-org isolation every stewardship read carries (INV-5)."""
    seeded = await _seed_estate(session)
    other = await _seed_datasource(session)
    other_ledger = await _seed_table(session, other, "ledger_entries")
    other_positions = await _seed_table(session, other, "acct_positions")
    await _seed_execution(session, other, "acct_positions", times=9)
    other_routine = await _seed_routine(session, other, "sp_other_org")
    await _seed_write_edge(session, other_routine, other_ledger, other_positions)
    await session.commit()

    here = await _routines(session, seeded["datasource"])
    there = await _routines(session, other)

    assert [item.routine_id for item in here.items] == [seeded["producer"].id]
    assert [item.routine_id for item in there.items] == [other_routine.id]
