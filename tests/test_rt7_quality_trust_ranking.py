"""RT-7: quality trust factor genuinely demotes candidates in retrieval ranking.

`retrieval.py`'s `hybrid_retrieve_enhanced` Stage 4 used to append a hardcoded
`SignalScore(signal="quality_trust", raw_score=0.5)` to every candidate,
regardless of any real `DataQualityIncident` -- a placeholder DQ-3's own
tracker row called out explicitly (`03-tracker.md` RT-7/DQ-3). This file
proves the placeholder is gone and the factor is real: it resolves each
candidate to its underlying `MetadataTable` id(s) and scores it against real
OPEN/ACKNOWLEDGED `DataQualityIncident` rows via
`quality_coupling.demote_in_retrieval` -- the same helper TL-3 (tool gating)
and AG-6 (answer trust warnings) already wire into live paths, so all three
DQ-3 wiring points now resolve incidents identically.

Driven through the actual live retrieval path, `agent_intelligence
.GovernedRetriever.retrieve()` -- the same object `GovernedAgentOrchestrator`
uses -- rather than a direct call into `quality_coupling.demote_in_retrieval`
or `hybrid_retrieve_enhanced` in isolation. That direct-unit-test-only shape
is the exact failure mode `04-end-to-end-audit-2026-08-30.md` found across
this codebase (real modules, zero live callers) and the RT-1..RT-3 wiring
work already tested against for this same call chain
(`tests/test_agent_orchestrator_retrieval_wiring.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_intelligence import GovernedRetriever
from aida.config import Settings
from aida.db import Base
from aida.models import (
    DataDomain,
    DataQualityIncident,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)

# Fixed, deterministic ids -- not `uuid4()`-random -- plus table names chosen so
# "widgets_aaa_flagged" sorts before "widgets_zzz_clean". `metadata_table` has a
# composite index (`ix_metadata_table_ds_status_name_id`) leading on
# `(datasource_id, status, name, id)`, so an unordered `SELECT` over two rows tied on
# every scoring signal comes back ordered by *name* (confirmed empirically: swapping
# which row has the lexicographically-earlier id had no effect on result order, only
# swapping which has the lexicographically-earlier name did) -- not by insertion
# (rowid) order and not by id. That name-driven order is what RRF's stable sort then
# uses to break ties on every signal both rows score identically on. Pinning it
# explicitly keeps the tie-break identical between the `with_incident=False`/
# `with_incident=True` worlds `_fresh_scenario` builds below, so a score difference
# between the two runs can only come from the quality-trust signal, not from an
# unrelated, unstated ordering artifact.
_FLAGGED_TABLE_ID = UUID("00000000-0000-0000-0000-0000000a1a99")
_CLEAN_TABLE_ID = UUID("00000000-0000-0000-0000-0000000f1ea4")


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    """Two otherwise-identical tables, both lexically matching the query
    equally (same BM25/exact-phrase score) -- the only difference is an open
    CRITICAL incident on one of them. Any score/rank gap between the two can
    only be explained by the quality-trust factor, not by lexical scoring.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Commerce",
            code="COMMERCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Commerce",
            slug="core-commerce",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="warehouse",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="public",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        # Both tables start with the token "widgets" so `_bm25_score` and
        # `_exact_phrase_bonus` score them identically against the query
        # "widgets" -- neither name shares any other *query* token, so neither
        # gets extra lexical credit from its "aaa"/"zzz" disambiguator (see
        # `_FLAGGED_TABLE_ID`'s comment above for why that disambiguator is there).
        self.flagged_table = MetadataTable(
            id=_FLAGGED_TABLE_ID,
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="widgets_aaa_flagged",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-widgets-flagged",
            source_description="Widgets table with a known quality incident.",
        )
        db.add(self.flagged_table)
        self.clean_table = MetadataTable(
            id=_CLEAN_TABLE_ID,
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="widgets_zzz_clean",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-widgets-clean",
            source_description="Widgets table with no open incidents.",
        )
        db.add(self.clean_table)
        await db.flush()
        return self

    async def incident(
        self, table_id, *, severity: str = "CRITICAL", status: str = "OPEN"
    ) -> DataQualityIncident:
        db = self.db
        incident = DataQualityIncident(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=table_id,
            fingerprint=f"fp-incident-{uuid4().hex[:8]}",
            anomaly_type="NULL_RATE_SHIFT",
            severity=severity,
            status=status,
            summary="Null rate spiked outside the governed baseline.",
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
        db.add(incident)
        await db.flush()
        return incident


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


def _hits_by_table_id(hits, table_id):
    for hit in hits:
        if hit.object_type == "TABLE" and hit.object_id == str(table_id):
            return hit
    raise AssertionError(f"no TABLE hit for {table_id}")


async def _fresh_scenario(*, with_incident: bool) -> tuple[_Scenario, list]:
    """Build a scenario against its own fresh in-memory database and run the
    real live retrieval path once against it, returning the scenario plus the
    hits. A separate engine per call (rather than sharing the `db` fixture)
    is deliberate: it lets the "with an open incident" and "without one"
    worlds be compared while every other input -- schema, row insertion
    order, RRF's own tie-breaking of the identically-scored lexical/usage
    signals -- stays exactly the same, isolating the quality-trust factor as
    the only thing that can move `flagged_table`'s own fused score between
    the two runs.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        scenario = await _Scenario(session).build()
        if with_incident:
            await scenario.incident(scenario.flagged_table.id, severity="CRITICAL", status="OPEN")
        hits = await GovernedRetriever(Settings()).retrieve(
            session,
            datasource=scenario.datasource,
            question="widgets",
        )
    await engine.dispose()
    return scenario, hits


async def test_governed_retriever_demotes_table_with_open_critical_incident() -> None:
    """DoD #1: a candidate touching a table with an open quality incident is
    measurably demoted relative to an identical candidate on a clean table,
    with the demotion reason visible in the ranking evidence -- driven
    through `GovernedRetriever.retrieve()`, the live orchestrator's own
    retrieval entry point (DoD #2)."""
    baseline_scenario, baseline_hits = await _fresh_scenario(with_incident=False)
    baseline_flagged = _hits_by_table_id(baseline_hits, baseline_scenario.flagged_table.id)
    baseline_final_score = baseline_flagged.metadata["retrieval_evidence"]["final_score"]

    scenario, hits = await _fresh_scenario(with_incident=True)
    flagged_hit = _hits_by_table_id(hits, scenario.flagged_table.id)
    clean_hit = _hits_by_table_id(hits, scenario.clean_table.id)

    flagged_evidence = flagged_hit.metadata["retrieval_evidence"]
    clean_evidence = clean_hit.metadata["retrieval_evidence"]

    def _quality_trust_factor(evidence):
        for factor in evidence["factors"]:
            if factor["signal"] == "quality_trust":
                return factor
        raise AssertionError("no quality_trust factor in evidence")

    flagged_factor = _quality_trust_factor(flagged_evidence)
    clean_factor = _quality_trust_factor(clean_evidence)

    # CRITICAL open incident -> `demote_in_retrieval` returns 0.3; a clean
    # table with no active incidents returns the neutral 1.0. This factor
    # comparison is unaffected by RRF's rank tie-breaking on the other,
    # identically-scored signals -- 0.3 vs 1.0 is a real difference in
    # `demote_in_retrieval`'s own output, not an ordering artifact.
    assert flagged_factor["raw_score"] == 0.3
    assert clean_factor["raw_score"] == 1.0
    assert flagged_factor["weighted_score"] < clean_factor["weighted_score"]

    # The demotion is measurable in the overall fused score too -- not just
    # relative to another candidate (where a genuine lexical tie between two
    # otherwise-identical rows would leave RRF's own rank tie-break, an
    # unrelated implementation detail, dominating the much larger-weighted
    # lexical signal), but for `flagged_table` against *itself* in an
    # otherwise-identical world with no open incident at all. Only the
    # quality_trust signal's rank can differ between these two runs.
    assert flagged_evidence["final_score"] < baseline_final_score

    # The reason is visible in the hit's own metadata, not just a bare
    # number nobody can explain (RT-3's "every factor inspectable"
    # convention).
    demotion = flagged_hit.metadata["quality_trust_demotion"]
    assert demotion["reason"] == "OPEN_QUALITY_INCIDENT"
    assert str(scenario.flagged_table.id) in demotion["demoted_table_ids"]
    assert demotion["worst_factor"] == 0.3
    assert "quality_trust_demotion" not in clean_hit.metadata


async def test_governed_retriever_does_not_demote_on_resolved_incident(
    scenario: _Scenario,
) -> None:
    """A RESOLVED incident must not gate/demote -- only OPEN/ACKNOWLEDGED do,
    matching `demote_in_retrieval`'s own contract and TL-3/AG-6's existing
    behaviour for the same incident-status filter."""
    await scenario.incident(scenario.flagged_table.id, severity="CRITICAL", status="RESOLVED")

    retriever = GovernedRetriever(Settings())
    hits = await retriever.retrieve(
        scenario.db,
        datasource=scenario.datasource,
        question="widgets",
    )

    flagged_hit = _hits_by_table_id(hits, scenario.flagged_table.id)
    assert "quality_trust_demotion" not in flagged_hit.metadata
    factor = next(
        f
        for f in flagged_hit.metadata["retrieval_evidence"]["factors"]
        if f["signal"] == "quality_trust"
    )
    assert factor["raw_score"] == 1.0


async def test_governed_retriever_demotes_governed_tool_via_referenced_tables(
    scenario: _Scenario,
) -> None:
    """A GOVERNED_TOOL candidate carries no `table_id`/`source_table_id`
    directly -- only `referenced_tables` (SQL-qualified name strings). Prove
    those get resolved to real `MetadataTable` ids (via
    `quality_coupling.resolve_table_ids`, same as TL-3) and demoted the same
    way, not silently skipped because the shape differs from a bare table
    hit."""
    from aida.models import GovernedTool, GovernedToolVersion

    await scenario.incident(scenario.flagged_table.id, severity="CRITICAL", status="OPEN")

    tool = GovernedTool(
        organization_id=scenario.organization.id,
        project_id=scenario.project.id,
        slug="widgets-flagged-lookup",
    )
    scenario.db.add(tool)
    await scenario.db.flush()
    version = GovernedToolVersion(
        organization_id=scenario.organization.id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Widgets Flagged Lookup",
        description="Reads from the flagged widgets table.",
        datasource_id=scenario.datasource.id,
        sql_template="SELECT 1 FROM public.widgets_aaa_flagged",
        referenced_tables=["public.widgets_aaa_flagged"],
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint="fp-tool-widgets-flagged",
        created_by="tool-dev",
    )
    scenario.db.add(version)
    await scenario.db.flush()

    retriever = GovernedRetriever(Settings())
    hits = await retriever.retrieve(
        scenario.db,
        datasource=scenario.datasource,
        question="widgets",
    )

    tool_hit = next(
        hit
        for hit in hits
        if hit.object_type == "GOVERNED_TOOL" and hit.object_id == str(version.id)
    )
    demotion = tool_hit.metadata["quality_trust_demotion"]
    assert demotion["reason"] == "OPEN_QUALITY_INCIDENT"
    assert str(scenario.flagged_table.id) in demotion["demoted_table_ids"]
    factor = next(
        f
        for f in tool_hit.metadata["retrieval_evidence"]["factors"]
        if f["signal"] == "quality_trust"
    )
    assert factor["raw_score"] == 0.3
