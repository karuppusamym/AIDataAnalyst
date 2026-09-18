"""R11-FP13: the governed half of the answer-evaluation estate is seeded through
the platform's decision paths, never written.

The answer-evaluation corpus scores answers over an *enriched* footprint. The
physical half of that footprint is `infra/sample-source/init.sql`'s `warehouse`
schema; the governed half -- a published ontology concept mapped to
`fact_account_balances`, `nightly_settlement_rollup`'s lineage reviewed to
ACTIVE, and its description approved -- is control-plane state, and until
`scripts/seed_governed_enrichment.py` nothing put it on a live estate. A live
run there would have scored an estate with no governed enrichment at all.

What these tests prove, and why each one matters:

1. **Every item is governed by a second identity.** The concept and the
   description are decided through `POST /v1/governance/reviews/{id}/decision`,
   the lineage through `POST /v1/lineage/parsed-edges/{id}/decision`, and the
   rows the platform writes as the effect record a proposer and a *different*
   decider. A seed that wrote an APPROVED row would produce the same end state
   with a fictional audit trail; the assertions on `requested_by`/`decided_by`,
   `created_by`/`approved_by` and `created_by`/`reviewed_by` are what tell the
   two apart.
2. **The gap stays a gap.** `quarterly_fee_accrual`'s lineage is proposed by the
   same agent run and must stay PROPOSED: the corpus's gap case scores whether
   lineage nobody approved steers an answer.
3. **A second run is a no-op** -- measured over every table in the schema, not
   over the three the seed is known to touch.
4. **`--same-identity` is refused on every item** and decides nothing, and a
   normal run afterwards carries those proposals forward rather than stacking
   a second set.
5. **The seed's specification is the corpus's** -- pinned to
   `scripts/quality_benchmark.py`'s FOOTPRINT_* constants and the corpus JSON,
   so the live estate cannot drift from the offline one.

As in `tests/test_seeded_governed_tool_journey.py`, the script speaks HTTP and
its `seed_sample_estate._request` is redirected into the real FastAPI
application in-process. The catalog rows connector discovery would produce --
the four `warehouse` tables and the two routines, with the bodies discovery
actually captured on the live stack -- are inserted directly, and the lineage
agent is registered the way `tests/support/task_agents.py` registers every
task agent; everything after that (the agent's parse, the proposals, the
reviews, the publication) runs for real.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aida.envelope_models  # noqa: F401 -- registers the routine tables
import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings, get_settings
from aida.db import Base, get_session
from aida.envelope_models import MetadataRoutine, RoutineDocumentationVersion
from aida.models import (
    AuditEvent,
    GovernanceReview,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.relationship_validation import NAME_MATCH_ONLY_CODE
from scripts import quality_benchmark
from scripts import seed_governed_enrichment as governed
from scripts import seed_sample_estate as estate
from tests.support.task_agents import register_agent, task_agent_maker

_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    # sqlite only auto-populates a bare INTEGER PRIMARY KEY; see
    # test_seeded_governed_tool_journey.py for the same workaround.
    if target.id is None:
        target.id = next(_audit_event_ids)


CORPUS_DIR = Path(__file__).parent / "fixtures" / "quality_benchmark_corpus"

#: The four `warehouse` tables the two routines read and write.
WAREHOUSE_TABLES = (
    "fact_payments",
    "fact_account_balances",
    "fact_loan_applications",
    "fact_fraud_alerts",
)

#: The bodies connector discovery captured for the two routines on the live
#: stack (`metadata_routine.body_sql_redacted`, 2026-09-17): literals already
#: redacted, which is the only form a routine body is ever stored in.
ROUTINE_BODIES = {
    "nightly_settlement_rollup": (
        "CREATE OR REPLACE PROCEDURE warehouse.nightly_settlement_rollup()\n"
        " LANGUAGE plpgsql\n"
        "AS $procedure$\n"
        "BEGIN\n"
        "    INSERT INTO warehouse.fact_account_balances\n"
        "        (account_id, gl_account_key, as_of_date, bal_amt_minor, currency_code, eod_ind)\n"
        "    SELECT p.account_id,\n"
        "           p.gl_account_key,\n"
        "           p.value_date,\n"
        "           SUM(p.settled_amt_minor - p.returned_amt_minor),\n"
        "           p.currency_code,\n"
        "           :redacted\n"
        "    FROM warehouse.fact_payments p\n"
        "    WHERE p.stp_ind = :redacted\n"
        "    GROUP BY p.account_id, p.gl_account_key, p.value_date, p.currency_code;\n"
        "END;\n"
        "$procedure$\n"
    ),
    "quarterly_fee_accrual": (
        "CREATE OR REPLACE PROCEDURE warehouse.quarterly_fee_accrual()\n"
        " LANGUAGE plpgsql\n"
        "AS $procedure$\n"
        "BEGIN\n"
        "    CREATE TEMP TABLE accrual_stage ON COMMIT DROP AS\n"
        "    SELECT la.application_id, la.account_id, la.origination_fee_minor, la.dq_bkt\n"
        "    FROM warehouse.fact_loan_applications la\n"
        "    WHERE la.decision_status = :redacted;\n"
        "\n"
        "    INSERT INTO warehouse.fact_fraud_alerts\n"
        "        (alert_event_id, account_id, alert_type, sev_lvl, raised_on,\n"
        "         detection_amt_minor, case_status)\n"
        "    SELECT s.application_id,\n"
        "           s.account_id,\n"
        "           :redacted,\n"
        "           :redacted,\n"
        "           DATE :redacted,\n"
        "           s.origination_fee_minor,\n"
        "           :redacted\n"
        "    FROM accrual_stage s\n"
        "    WHERE s.dq_bkt <> :redacted;\n"
        "END;\n"
        "$procedure$\n"
    ),
}

ROUTINE_COMMENTS = {
    "nightly_settlement_rollup": "Batch job. Owner: Ops Engineering. See runbook OPS-114.",
    "quarterly_fee_accrual": "Quarter-end job. Owner: Finance Systems. See runbook FIN-207.",
}


@dataclass
class _Estate:
    maker: async_sessionmaker[AsyncSession]
    slug: str
    org_id: UUID
    datasource_id: UUID
    table_ids: dict[str, UUID]
    routine_ids: dict[str, UUID]


def _redirect_seed_requests_into_the_app(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop
) -> None:
    """`seed_sample_estate._request`, pointed at the in-process application --
    same signature, same `(status, payload)`, same `expect` semantics. Both seed
    scripts call it through the module, so this one patch carries both."""

    def _request(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        org_id: str | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 201, 202),
    ) -> tuple[int, Any]:
        request_headers = dict(headers if headers is not None else estate.MAKER_HEADERS)
        if org_id:
            request_headers["X-Organization-Id"] = org_id
        future = asyncio.run_coroutine_threadsafe(
            client.request(method, path, json=body, headers=request_headers), loop
        )
        response = future.result(timeout=120)
        if response.status_code >= 400 and response.status_code not in expect:
            raise estate.SeedError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text}"
            )
        payload = response.json() if response.content else None
        return response.status_code, payload

    monkeypatch.setattr(estate, "_request", _request)


async def _seed_discovered_warehouse(
    session: AsyncSession, *, organization_id: UUID, datasource_id: UUID
) -> tuple[dict[str, UUID], dict[str, UUID]]:
    """What `DatasourceDiscoveryWorkflow` catalogues from the `warehouse` schema,
    as far as the three governed items reach: the tables the lineage resolves
    to and the two routines with their captured bodies."""
    catalog = MetadataCatalog(
        organization_id=organization_id,
        datasource_id=datasource_id,
        name="bank_demo",
        fingerprint="fp-catalog",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=organization_id,
        catalog_id=catalog.id,
        name="warehouse",
        fingerprint="fp-schema",
    )
    session.add(schema)
    await session.flush()
    tables: dict[str, UUID] = {}
    for name in WAREHOUSE_TABLES:
        table = MetadataTable(
            organization_id=organization_id,
            datasource_id=datasource_id,
            schema_id=schema.id,
            name=name,
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint=f"fp-{name}",
        )
        session.add(table)
        await session.flush()
        tables[name] = table.id
    routines: dict[str, UUID] = {}
    for name, body in ROUTINE_BODIES.items():
        routine = MetadataRoutine(
            organization_id=organization_id,
            datasource_id=datasource_id,
            schema_id=schema.id,
            name=name,
            signature="()",
            routine_type="PROCEDURE",
            language="plpgsql",
            body_sql_redacted=body,
            body_fingerprint=f"body-{name}",
            redaction_status="LEXICAL",
            screening_status="CLEAN",
            source_description=ROUTINE_COMMENTS[name],
            status="ACTIVE",
            fingerprint=f"fp-{name}",
        )
        session.add(routine)
        await session.flush()
        routines[name] = routine.id
    return tables, routines


@pytest_asyncio.fixture
async def warehouse(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Estate]:
    # Imported before the schema is created: mounting the routers imports the
    # last few model modules.
    from aida.main import app

    # `task_agent_maker`: sqlite with PostgreSQL's transaction semantics, which
    # the lineage agent's per-item savepoints need to mean what they say.
    async with task_agent_maker() as maker:
        settings = Settings(_env_file=None, environment="test", temporal_enabled=False)

        async def _session_override() -> AsyncIterator[AsyncSession]:
            async with maker() as session:
                yield session

        previous_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_session] = _session_override
        app.dependency_overrides[get_settings] = lambda: settings
        slug = f"sample-bank-{uuid4().hex[:8]}"
        monkeypatch.setattr(estate, "ORG_SLUG", slug)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://seed.test"
            ) as client:
                _redirect_seed_requests_into_the_app(
                    monkeypatch, client, asyncio.get_running_loop()
                )

                def _build_tenancy() -> tuple[str, str]:
                    org = estate.ensure_organization()
                    lob = estate.ensure_line_of_business(org["id"])
                    domain = estate.ensure_data_domain(lob["id"], "Customer", "CUSTOMER", org["id"])
                    project = estate.ensure_project(
                        lob["id"], domain["id"], "Customer Master", "customer-master", org["id"]
                    )
                    datasource = estate.ensure_datasource(
                        project["id"],
                        org["id"],
                        name="Customer Master (Postgres, sample)",
                        connector_type="postgres",
                        dialect="postgres",
                        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
                    )
                    return org["id"], datasource["id"]

                org_id, datasource_id = await asyncio.to_thread(_build_tenancy)
                async with maker() as session:
                    tables, routines = await _seed_discovered_warehouse(
                        session,
                        organization_id=UUID(org_id),
                        datasource_id=UUID(datasource_id),
                    )
                    organization = await session.get(Organization, UUID(org_id))
                    assert organization is not None
                    await register_agent(session, organization, principal="agent:lineage")
                    await session.commit()

                yield _Estate(
                    maker=maker,
                    slug=slug,
                    org_id=UUID(org_id),
                    datasource_id=UUID(datasource_id),
                    table_ids=tables,
                    routine_ids=routines,
                )
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)


async def _run(warehouse: _Estate, *, same_identity: bool = False) -> list[governed.ItemOutcome]:
    return await asyncio.to_thread(governed.seed, warehouse.slug, same_identity=same_identity)


def _states(outcomes: list[governed.ItemOutcome]) -> dict[str, str]:
    return {outcome.item: outcome.state for outcome in outcomes}


async def _edges(warehouse: _Estate, routine: str) -> list[DeepProcedureLineageEdge]:
    async with warehouse.maker() as session:
        return list(
            (
                await session.scalars(
                    select(DeepProcedureLineageEdge).where(
                        DeepProcedureLineageEdge.routine_id == warehouse.routine_ids[routine]
                    )
                )
            ).all()
        )


async def _table_state(warehouse: _Estate) -> dict[str, tuple[int, Any]]:
    """Every table's row count and, where it has one, its latest `updated_at` --
    so a re-run that re-decided or re-wrote a row is caught, not only one that
    inserted a new one."""
    state: dict[str, tuple[int, Any]] = {}
    async with warehouse.maker() as session:
        for table in Base.metadata.sorted_tables:
            rows = int(await session.scalar(select(func.count()).select_from(table)) or 0)
            touched = (
                await session.scalar(select(func.max(table.c.updated_at)))
                if "updated_at" in table.c
                else None
            )
            state[table.name] = (rows, touched)
    return state


# ---------------------------------------------------------------------------
# 1. Every item governed by a second identity
# ---------------------------------------------------------------------------


async def test_the_concept_is_published_by_an_independent_reviewer(warehouse: _Estate) -> None:
    outcomes = await _run(warehouse)

    assert _states(outcomes)[f"concept {governed.CONCEPT_KEY}"] == governed.GOVERNED
    async with warehouse.maker() as session:
        version = await session.scalar(select(OntologyVersion))
        head = await session.scalar(select(OntologyHead))
        review = await session.scalar(
            select(GovernanceReview).where(GovernanceReview.object_type == "ONTOLOGY_VERSION")
        )
    assert version is not None and head is not None and review is not None
    assert head.ontology_key == governed.ONTOLOGY_KEY
    assert version.status == "APPROVED"
    assert head.published_version == version.version, "approved but not the published head"
    assert version.created_by == governed.MAKER_PRINCIPAL
    assert version.approved_by == governed.CHECKER_PRINCIPAL
    assert review.requested_by == governed.MAKER_PRINCIPAL
    assert review.decided_by == governed.CHECKER_PRINCIPAL
    assert review.status == "APPROVED"
    # The concept as the corpus's alias cases need it, mapped to the table the
    # connector catalogued -- not to an id this test invented.
    concept = version.definition["concepts"][0]
    assert concept["key"] == governed.CONCEPT_KEY
    assert concept["name"] == governed.CONCEPT_NAME
    assert "closing position" in concept["aliases"]
    assert version.definition["mappings"] == [
        {
            "concept": governed.CONCEPT_KEY,
            "subject_type": "TABLE",
            "subject_id": str(warehouse.table_ids["fact_account_balances"]),
        }
    ]


async def test_the_rollup_lineage_is_proposed_by_the_agent_and_reviewed_by_a_person(
    warehouse: _Estate,
) -> None:
    outcomes = await _run(warehouse)

    assert _states(outcomes)[f"lineage {governed.REVIEWED_ROUTINE}"] == governed.GOVERNED
    edges = [
        e
        for e in await _edges(warehouse, "nightly_settlement_rollup")
        if e.is_write and not e.is_intermediate and e.transformation_type != "UNPARSED"
    ]
    assert edges, "the agent proposed no write edge from the rollup's own body"
    for edge in edges:
        # Derived from the body -- the only place fact_payments ->
        # fact_account_balances exists -- and resolved to the catalogued tables.
        assert edge.source_table_id == warehouse.table_ids["fact_payments"]
        assert edge.target_table_id == warehouse.table_ids["fact_account_balances"]
        assert edge.review_status == "ACTIVE"
        assert edge.created_by == "agent:lineage"
        assert edge.reviewed_by == governed.CHECKER_PRINCIPAL
        assert edge.reviewed_by != edge.created_by


async def test_the_gap_routines_lineage_is_proposed_and_left_undecided(
    warehouse: _Estate,
) -> None:
    outcomes = await _run(warehouse)

    assert _states(outcomes)[f"gap {governed.UNDECIDED_ROUTINE}"] == governed.UNDECIDED
    edges = await _edges(warehouse, "quarterly_fee_accrual")
    into_alerts = [
        e for e in edges if e.target_table_id == warehouse.table_ids["fact_fraud_alerts"]
    ]
    assert into_alerts, "the gap case needs a proposed path into fact_fraud_alerts to exist"
    assert {e.review_status for e in into_alerts} == {"PROPOSED"}
    assert all(e.reviewed_by is None for e in into_alerts)


async def test_the_description_is_drafted_by_atlas_edited_by_a_steward_approved_by_another(
    warehouse: _Estate,
) -> None:
    outcomes = await _run(warehouse)

    assert _states(outcomes)[f"description {governed.REVIEWED_ROUTINE}"] == governed.GOVERNED
    async with warehouse.maker() as session:
        versions = list((await session.scalars(select(RoutineDocumentationVersion))).all())
        review = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "ROUTINE_DESCRIPTION_DRAFT"
            )
        )
    assert len(versions) == 1
    version = versions[0]
    assert version.status == "APPROVED"
    assert version.description == governed.ROUTINE_DESCRIPTION
    assert version.created_by == governed.MAKER_PRINCIPAL
    assert version.approved_by == governed.CHECKER_PRINCIPAL
    assert review is not None
    assert (review.requested_by, review.decided_by) == (
        governed.MAKER_PRINCIPAL,
        governed.CHECKER_PRINCIPAL,
    )


# ---------------------------------------------------------------------------
# 2. Idempotence
# ---------------------------------------------------------------------------


async def test_a_second_run_writes_nothing_anywhere(warehouse: _Estate) -> None:
    first = await _run(warehouse)
    assert all(o.ok for o in first), first
    before = await _table_state(warehouse)

    second = await _run(warehouse)

    states = _states(second)
    assert states.pop(f"gap {governed.UNDECIDED_ROUTINE}") == governed.UNDECIDED
    assert set(states.values()) == {governed.ALREADY}, second
    after = await _table_state(warehouse)
    changed = {name: (before[name], after[name]) for name in before if before[name] != after[name]}
    assert changed == {}, f"a re-run wrote or updated rows: {changed}"


# ---------------------------------------------------------------------------
# 3. The control, watched refusing
# ---------------------------------------------------------------------------


async def test_same_identity_is_refused_on_every_item_and_decides_nothing(
    warehouse: _Estate,
) -> None:
    outcomes = await _run(warehouse, same_identity=True)

    states = _states(outcomes)
    assert states[f"concept {governed.CONCEPT_KEY}"] == governed.REFUSED
    assert states[f"lineage {governed.REVIEWED_ROUTINE}"] == governed.REFUSED
    assert states[f"description {governed.REVIEWED_ROUTINE}"] == governed.REFUSED
    for outcome in outcomes:
        if outcome.state == governed.REFUSED:
            assert outcome.refusals, outcome
    async with warehouse.maker() as session:
        approved_ontology = await session.scalar(
            select(func.count())
            .select_from(OntologyVersion)
            .where(OntologyVersion.status == "APPROVED")
        )
        published = await session.scalar(
            select(func.count()).select_from(RoutineDocumentationVersion)
        )
    assert approved_ontology == 0
    assert published == 0
    assert {e.review_status for e in await _edges(warehouse, "nightly_settlement_rollup")} == {
        "PROPOSED"
    }


async def test_a_normal_run_after_a_refused_one_carries_its_proposals_forward(
    warehouse: _Estate,
) -> None:
    await _run(warehouse, same_identity=True)

    outcomes = await _run(warehouse)

    assert all(o.ok for o in outcomes), outcomes
    async with warehouse.maker() as session:
        ontology_versions = await session.scalar(select(func.count()).select_from(OntologyVersion))
        reviews = list((await session.scalars(select(GovernanceReview))).all())
    assert ontology_versions == 1, "a second ontology version was stacked beside the first"
    assert sorted(r.object_type for r in reviews) == [
        "ONTOLOGY_VERSION",
        "ROUTINE_DESCRIPTION_DRAFT",
    ]
    assert {r.decided_by for r in reviews} == {governed.CHECKER_PRINCIPAL}


# ---------------------------------------------------------------------------
# 4. Refusals that name the missing prerequisite
# ---------------------------------------------------------------------------


async def test_an_unregistered_lineage_agent_is_named_with_the_command_that_registers_it(
    warehouse: _Estate,
) -> None:
    from aida.models import AgentContract

    async with warehouse.maker() as session:
        for contract in (await session.scalars(select(AgentContract))).all():
            await session.delete(contract)
        await session.commit()

    with pytest.raises(estate.SeedError) as excinfo:
        await _run(warehouse)

    assert "seed_task_agent.py" in str(excinfo.value)
    assert "--agent lineage" in str(excinfo.value)


async def test_an_undiscovered_warehouse_is_named_rather_than_half_seeded(
    warehouse: _Estate,
) -> None:
    async with warehouse.maker() as session:
        table = await session.get(MetadataTable, warehouse.table_ids["fact_account_balances"])
        assert table is not None
        table.status = "DEPRECATED"
        await session.commit()

    with pytest.raises(estate.SeedError) as excinfo:
        await _run(warehouse)

    assert "warehouse" in str(excinfo.value)
    assert "init.sql" in str(excinfo.value)
    async with warehouse.maker() as session:
        assert await session.scalar(select(func.count()).select_from(OntologyVersion)) == 0


# ---------------------------------------------------------------------------
# 5. The specification is the corpus's
# ---------------------------------------------------------------------------


def test_the_seeded_concept_and_description_are_the_offline_estates() -> None:
    concept_key, concept_name, aliases, table = quality_benchmark.FOOTPRINT_CONCEPT
    assert (governed.CONCEPT_KEY, governed.CONCEPT_NAME) == (concept_key, concept_name)
    assert governed.CONCEPT_ALIASES == aliases
    assert governed.CONCEPT_TABLE == table
    assert (governed.REVIEWED_ROUTINE, governed.ROUTINE_DESCRIPTION) == (
        quality_benchmark.FOOTPRINT_ROUTINE_DESCRIPTION
    )
    seeds = {
        key: (reads, writes, review)
        for key, reads, writes, review in (quality_benchmark.FOOTPRINT_ROUTINE_SEEDS)
    }
    assert seeds[governed.REVIEWED_ROUTINE] == (
        governed.REVIEWED_ROUTINE_READS,
        governed.REVIEWED_ROUTINE_WRITES,
        "ACTIVE",
    )
    assert seeds[governed.UNDECIDED_ROUTINE][1:] == (
        governed.UNDECIDED_ROUTINE_WRITES,
        "PROPOSED",
    )


def test_the_seeded_datasource_is_the_one_the_live_corpus_asks() -> None:
    corpus = json.loads((CORPUS_DIR / "answer_evaluation_corpus.json").read_text("utf-8"))
    assert corpus["datasource_name_prefix"] == governed.DATASOURCE_NAME_PREFIX
    # And every ROUTINE / ONTOLOGY_CONCEPT the corpus expects evidence from is
    # one this seed governs, so the list above cannot silently fall behind it.
    expected = {
        (ref["object_type"], ref["object_key"])
        for case in corpus["cases"]
        for ref in case["expected_evidence"]
        if ref["object_type"] in ("ROUTINE", "ONTOLOGY_CONCEPT")
    }
    assert expected == {
        ("ONTOLOGY_CONCEPT", governed.CONCEPT_KEY),
        ("ROUTINE", governed.REVIEWED_ROUTINE),
        ("ROUTINE", governed.UNDECIDED_ROUTINE),
    }


# ---------------------------------------------------------------------------
# seed_sample_estate: a join the platform will not approve is left for a person
# ---------------------------------------------------------------------------


def test_the_name_match_code_is_the_platforms() -> None:
    assert estate.RELATIONSHIP_NAME_MATCH_ONLY == NAME_MATCH_ONLY_CODE


def test_a_name_match_refusal_leaves_the_candidate_pending_and_the_seed_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Discovery over the `warehouse` schema proposes joins the platform refuses to
    approve as bare name matches (409 RELATIONSHIP_NAME_MATCH_ONLY) -- three of
    them on the live stack. Before this, the first refusal killed the whole seed
    after discovery: grants, the governed tool and cross-source never ran."""
    decisions: list[str] = []

    def _request(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        org_id: str | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 201, 202),
    ) -> tuple[int, Any]:
        if path.endswith("/relationship-candidates/discover"):
            return 202, {}
        if "/relationship-candidates?" in path:
            return 200, {
                "items": [
                    {"id": "declared", "status": "PENDING"},
                    {"id": "name-only", "status": "PENDING"},
                ]
            }
        decisions.append(path)
        if "/name-only/" in path:
            payload = {"detail": {"code": NAME_MATCH_ONLY_CODE, "outcome": "NAME_MATCH_ONLY"}}
            if 409 not in expect:
                raise estate.SeedError(f"{method} {path} -> HTTP 409: {payload}")
            return 409, payload
        return 200, {"status": "APPROVED"}

    monkeypatch.setattr(estate, "_request", _request)

    estate.discover_and_approve_same_source_relationships("ds", "org")

    assert decisions == [
        "/v1/relationship-candidates/declared/decision",
        "/v1/relationship-candidates/name-only/decision",
    ]
    assert "1 approved, 1 left PENDING" in capsys.readouterr().out


def test_any_other_refusal_still_stops_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the evidence refusal is tolerated. A maker-checker refusal, say,
    means the seed itself is wrong and must not be counted as 'left pending'."""

    def _request(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        org_id: str | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 201, 202),
    ) -> tuple[int, Any]:
        if path.endswith("/discover"):
            return 202, {}
        if "/relationship-candidates?" in path:
            return 200, {"items": [{"id": "c1", "status": "PENDING"}]}
        # `_request`'s own contract: a status the caller did not expect raises.
        payload = {"detail": "maker cannot review their own candidate"}
        if 409 not in expect:
            raise estate.SeedError(f"{method} {path} -> HTTP 409: {payload}")
        return 409, payload

    monkeypatch.setattr(estate, "_request", _request)

    with pytest.raises(estate.SeedError, match="maker cannot review"):
        estate.discover_and_approve_same_source_relationships("ds", "org")
