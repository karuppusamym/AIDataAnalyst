"""R11-B1: the seeded estate can answer a question with model generation off.

The parameter half of this row (commit `8becc9c`) made the refusal a contract:
`AgentClarificationRequired` carries the parameter names and the tool version
they belong to, the endpoint returns them as a structured 409
(`MISSING_TOOL_PARAMETERS`), and Ask renders an input per name. What it could
not do was demonstrate the journey, because **the sample estate contained no
parameterised governed tool**. With generation off -- the default, and the only
state a fresh install has ever been in -- retrieval found no approved tool, the
planner fell to MODEL_GENERATION, and the run failed closed on the missing
model route. Every question the seeded estate could be asked refused.

`scripts/seed_sample_estate.py` now publishes one (`accounts_by_branch`), and
this file is its proof. Two things are proven, and they are different things:

1. **How it is published.** The script does not write a PUBLISHED row. It
   drafts through `POST /v1/projects/{id}/tools`, submits through
   `POST /v1/tool-versions/{id}/submit`, and the version becomes PUBLISHED
   only as the effect of an independent checker's
   `POST /v1/governance/reviews/{id}/decision` -- which the API refuses to the
   principal who requested it. `test_the_seeded_tool_cannot_be_published_by_its_own_author`
   asserts that refusal, so "it went through the maker-checker gate" is a
   measured fact rather than a claim about the code's shape.

2. **What it answers.** The seeded question returns `GOVERNED_TOOL` when its
   parameter is supplied, and the structured 409 naming `branch_code` when it
   is not -- both through the real
   `POST /v1/datasources/{id}/agent-analyses` route the Ask screen calls.

The seed script speaks HTTP to a running API. Here its `_request` is
redirected into the real FastAPI application in-process (ASGI transport,
sqlite), so the paths, bodies, headers and status handling under test are the
script's own -- no live stack, no reimplementation of what it sends. The
script's own `MAKER_HEADERS`/`CHECKER_HEADERS` identities are used unchanged,
which is what makes the maker-checker assertion meaningful.

Connector discovery is the one step that genuinely needs the live sample
databases, so the catalog rows it would produce (`bank_demo.customer.account`
and its columns, from `infra/sample-source/init.sql`) are inserted directly.
Everything the tool itself touches -- SQL validation against the catalog, the
allowed-tables check, publication, retrieval, planning, parameter rendering --
runs for real against them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import count
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings, get_settings
from aida.db import Base, get_session
from aida.models import (
    AnalysisRun,
    AuditEvent,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ModelRouteConfiguration,
)
from scripts import seed_sample_estate as seed
from tests.support.doubles import FakeSqlExecutor

# `AuditEvent.id` is a `BigInteger` autoincrement PK relying in production on
# Postgres's own sequence; sqlite only auto-populates a bare
# `INTEGER PRIMARY KEY`. Same workaround as
# `test_agent_orchestrator_retrieval_wiring.py` -- every endpoint below writes
# audit rows.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


#: The rows `customer.account` holds for the branch the seeded example names
#: (`infra/sample-source/init.sql`), returned by the fake connector so the
#: governed answer is a real, non-empty result set rather than an empty one
#: that would pass the same assertions.
BRANCH_ROWS: tuple[dict[str, Any], ...] = (
    {
        "account_id": 1001,
        "customer_id": 1,
        "account_type": "CHECKING",
        "currency_code": "USD",
        "status": "ACTIVE",
        "current_balance": 2500.00,
    },
    {
        "account_id": 1002,
        "customer_id": 1,
        "account_type": "SAVINGS",
        "currency_code": "USD",
        "status": "ACTIVE",
        "current_balance": 18400.55,
    },
)

#: Exactly what connector discovery catalogs for the Customer sample database.
ACCOUNT_COLUMNS = (
    ("account_id", "BIGINT"),
    ("customer_id", "BIGINT"),
    ("account_type", "TEXT"),
    ("currency_code", "CHAR"),
    ("branch_code", "TEXT"),
    ("status", "TEXT"),
    ("opened_at", "DATE"),
    ("current_balance", "NUMERIC"),
)


@dataclass
class _Estate:
    client: httpx.AsyncClient
    session_maker: async_sessionmaker[AsyncSession]
    org_id: str
    project_id: str
    datasource_id: str


def _ask_headers(principal: str, roles: str, org_id: str) -> dict[str, str]:
    """The Ask screen's own identity, not the seed script's -- a person asking
    a question is not the identity that published the tool."""
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": roles,
        "X-Business-Purpose": "Governed Ask journey",
        "X-Organization-Id": org_id,
    }


def _redirect_seed_requests_into_the_app(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop
) -> None:
    """Point `seed_sample_estate._request` at the in-process application.

    Same signature, same `(status, payload)` return, same `expect` semantics
    and same `SeedError` on anything else -- only the transport differs, so
    every call site in the script is exercised exactly as written. The seed
    functions themselves are synchronous and run in a worker thread; each
    request is handed back to the test's event loop, which is where the ASGI
    application and its sqlite sessions live.
    """

    def _request(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        org_id: str | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 201, 202),
    ) -> tuple[int, Any]:
        request_headers = dict(headers if headers is not None else seed.MAKER_HEADERS)
        if org_id:
            request_headers["X-Organization-Id"] = org_id
        future = asyncio.run_coroutine_threadsafe(
            client.request(method, path, json=body, headers=request_headers), loop
        )
        response = future.result(timeout=60)
        if response.status_code >= 400 and response.status_code not in expect:
            raise seed.SeedError(f"{method} {path} -> HTTP {response.status_code}: {response.text}")
        payload = response.json() if response.content else None
        return response.status_code, payload

    monkeypatch.setattr(seed, "_request", _request)


async def _seed_discovered_catalog(
    session: AsyncSession, *, organization_id: UUID, datasource_id: UUID
) -> None:
    """The catalog rows `DatasourceDiscoveryWorkflow` produces against the
    Customer sample database. Inserted rather than discovered because
    discovery is the one step that needs the live Postgres container; the
    tool's SQL is still validated against these rows by the real
    `QueryExecutionGateway.allowed_tables` check in the draft endpoint."""
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
        name="customer",
        fingerprint="fp-schema",
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=organization_id,
        datasource_id=datasource_id,
        schema_id=schema.id,
        name="account",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp-account",
        source_description="Customer accounts, one row per account",
    )
    session.add(table)
    await session.flush()
    for ordinal, (name, physical_type) in enumerate(ACCOUNT_COLUMNS, start=1):
        session.add(
            MetadataColumn(
                organization_id=organization_id,
                table_id=table.id,
                name=name,
                ordinal_position=ordinal,
                physical_type=physical_type,
                status="ACTIVE",
                nullable=False,
                fingerprint=f"fp-{name}",
            )
        )
    # `GovernedAgentOrchestrator.run` refuses a datasource whose metadata was
    # never analysed.
    session.add(
        AnalysisRun(
            organization_id=organization_id,
            datasource_id=datasource_id,
            status="COMPLETED",
        )
    )
    await session.commit()


@pytest_asyncio.fixture
async def estate(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Estate]:
    # Imported *before* `create_all`: mounting the routers is what imports the
    # last few model modules (the deep-procedure lineage tables the answer
    # stage reads, among them), and a schema created before that is missing
    # them -- which shows up only in whichever test runs first.
    from aida.main import app

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    settings = Settings(_env_file=None, temporal_enabled=False)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    # `app` is the process-wide application object, so the overrides are put
    # back exactly as they were found rather than cleared -- clearing would
    # silently remove anything another test had installed.
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://seed.test") as client:
        _redirect_seed_requests_into_the_app(monkeypatch, client, asyncio.get_running_loop())
        # A distinct slug per test run: the seed script is idempotent against
        # an existing organization, and these tests must not depend on which
        # one ran first.
        monkeypatch.setattr(seed, "ORG_SLUG", f"sample-bank-{uuid4().hex[:8]}")

        def _build_tenancy() -> tuple[str, str, str]:
            org = seed.ensure_organization()
            lob = seed.ensure_line_of_business(org["id"])
            domain = seed.ensure_data_domain(lob["id"], "Customer", "CUSTOMER", org["id"])
            project = seed.ensure_project(
                lob["id"], domain["id"], "Customer Master", "customer-master", org["id"]
            )
            datasource = seed.ensure_datasource(
                project["id"],
                org["id"],
                name="Customer Master (Postgres, sample)",
                connector_type="postgres",
                dialect="postgres",
                credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
            )
            return org["id"], project["id"], datasource["id"]

        org_id, project_id, datasource_id = await asyncio.to_thread(_build_tenancy)
        async with maker() as session:
            await _seed_discovered_catalog(
                session,
                organization_id=UUID(org_id),
                datasource_id=UUID(datasource_id),
            )

        yield _Estate(
            client=client,
            session_maker=maker,
            org_id=org_id,
            project_id=project_id,
            datasource_id=datasource_id,
        )

    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous_overrides)
    await engine.dispose()


@pytest.fixture
def fake_warehouse(monkeypatch: pytest.MonkeyPatch) -> FakeSqlExecutor:
    """The seeded Postgres container, stood in for. Everything above the
    connector -- guard, cost gate, masking, persistence, audit -- is the real
    path; only the rows come from here."""
    executor = FakeSqlExecutor(BRANCH_ROWS)
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://sample/bank_demo")}
        )(),
    )
    return executor


async def _publish_seeded_tool(estate: _Estate) -> dict[str, Any]:
    return await asyncio.to_thread(
        seed.ensure_governed_tool, estate.project_id, estate.datasource_id, estate.org_id
    )


async def _ask(
    estate: _Estate,
    *,
    tool_parameters: dict[str, Any],
    principal: str = "ask-analyst",
    roles: str = "Analyst",
) -> httpx.Response:
    return await estate.client.post(
        f"/v1/datasources/{estate.datasource_id}/agent-analyses",
        json={"question": seed.GOVERNED_TOOL_QUESTION, "tool_parameters": tool_parameters},
        headers=_ask_headers(principal, roles, estate.org_id),
    )


# ---------------------------------------------------------------------------
# 1. How the tool is published
# ---------------------------------------------------------------------------


async def test_seed_publishes_a_parameterised_tool_through_the_approval_path(
    estate: _Estate,
) -> None:
    published = await _publish_seeded_tool(estate)

    assert published["status"] == "PUBLISHED"
    assert published["slug"] == seed.GOVERNED_TOOL_SLUG
    assert [p["name"] for p in published["parameters"]] == [seed.GOVERNED_TOOL_PARAMETER]
    # STRING, because the Ask screen sends every value as a string and the
    # server coerces against this schema -- an INTEGER parameter would refuse
    # the browser's own input on a journey this seed exists to demonstrate.
    assert published["parameters"][0]["parameter_type"] == "STRING"
    assert published["parameters"][0]["required"] is True
    # Two identities, and the checker is the one the publication is attributed
    # to: the tool exists because somebody other than its author approved it.
    assert published["created_by"] == seed.MAKER_PRINCIPAL
    assert published["approved_by"] == seed.CHECKER_PRINCIPAL
    assert published["approved_at"] is not None

    # Re-running the seed is a no-op, not a second version.
    again = await _publish_seeded_tool(estate)
    assert again["id"] == published["id"]
    async with estate.session_maker() as session:
        versions = (await session.execute(select(GovernedToolVersion))).scalars().all()
    assert len(versions) == 1


async def test_the_seeded_tool_cannot_be_published_by_its_own_author(estate: _Estate) -> None:
    """The gate the seed goes through is real, not a code path it politely
    chooses: the maker's own approval of the review it filed is refused."""

    def _draft_and_submit() -> dict[str, Any]:
        _, version = seed._request(
            "POST",
            f"/v1/projects/{estate.project_id}/tools",
            {
                "slug": seed.GOVERNED_TOOL_SLUG,
                "name": seed.GOVERNED_TOOL_NAME,
                "description": seed.GOVERNED_TOOL_DESCRIPTION,
                "datasource_id": estate.datasource_id,
                "sql_template": seed.GOVERNED_TOOL_SQL,
                "parameters": [
                    {
                        "name": seed.GOVERNED_TOOL_PARAMETER,
                        "parameter_type": "STRING",
                        "required": True,
                        "max_length": 32,
                    }
                ],
                "allowed_roles": ["Analyst"],
            },
            org_id=estate.org_id,
        )
        seed._request(
            "POST", f"/v1/tool-versions/{version['id']}/submit", org_id=estate.org_id
        )
        review = seed._find_pending_review("GOVERNED_TOOL_VERSION", version["id"], estate.org_id)
        assert review is not None
        # The maker -- the identity that filed this review -- tries to decide it.
        status, payload = seed._request(
            "POST",
            f"/v1/governance/reviews/{review['id']}/decision",
            {"decision": "APPROVE", "reason": "self-approval attempt"},
            org_id=estate.org_id,
            headers=seed.MAKER_HEADERS,
            expect=(409,),
        )
        return {"version": version, "status": status, "payload": payload}

    outcome = await asyncio.to_thread(_draft_and_submit)

    assert outcome["status"] == 409
    assert "maker-checker" in str(outcome["payload"]).lower()
    async with estate.session_maker() as session:
        version = await session.get(GovernedToolVersion, UUID(outcome["version"]["id"]))
        assert version is not None
        assert version.status == "REVIEW_REQUIRED", (
            "a refused self-approval must leave the tool unpublished"
        )


# ---------------------------------------------------------------------------
# 2. What it answers, with model generation off
# ---------------------------------------------------------------------------


async def test_seeded_question_refuses_by_naming_the_input_it_needs(
    estate: _Estate, fake_warehouse: FakeSqlExecutor
) -> None:
    published = await _publish_seeded_tool(estate)

    response = await _ask(estate, tool_parameters={})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "MISSING_TOOL_PARAMETERS"
    assert detail["required_parameters"] == [seed.GOVERNED_TOOL_PARAMETER]
    # The tool version the retry must pin -- selected by retrieval from the
    # question alone, since the request named no preferred tool.
    assert detail["tool_version_id"] == published["id"]
    assert not fake_warehouse.statements, "nothing may reach the warehouse on a refusal"


async def test_seeded_question_returns_a_governed_result_when_the_input_is_supplied(
    estate: _Estate, fake_warehouse: FakeSqlExecutor
) -> None:
    published = await _publish_seeded_tool(estate)

    response = await _ask(
        estate,
        tool_parameters={seed.GOVERNED_TOOL_PARAMETER: seed.GOVERNED_TOOL_PARAMETER_EXAMPLE},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["generation_source"] == "GOVERNED_TOOL"
    assert body["execution"]["row_count"] == len(BRANCH_ROWS)
    tool_hits = [
        hit for hit in body["retrieval_evidence"] if hit["object_type"] == "GOVERNED_TOOL"
    ]
    assert [hit["object_id"] for hit in tool_hits] == [published["id"]]
    # The parameter reached the warehouse as a rendered literal in the tool's
    # own SQL, not as caller-supplied SQL.
    assert any(
        seed.GOVERNED_TOOL_PARAMETER_EXAMPLE in statement for statement in fake_warehouse.statements
    )

    # Model generation is off throughout: no approved route exists to
    # generate from, so `GOVERNED_TOOL` above is the only thing that could
    # have produced this answer.
    async with estate.session_maker() as session:
        routes = (await session.execute(select(ModelRouteConfiguration))).scalars().all()
    assert routes == []


async def test_the_same_question_has_no_fallback_for_a_role_the_tool_excludes(
    estate: _Estate, fake_warehouse: FakeSqlExecutor
) -> None:
    """The counterweight to the test above: with generation off, a caller the
    tool's `allowed_roles` excludes gets no answer at all. The governed result
    is the tool's doing, not something the platform would have produced anyway.
    """
    await _publish_seeded_tool(estate)

    response = await _ask(
        estate,
        tool_parameters={seed.GOVERNED_TOOL_PARAMETER: seed.GOVERNED_TOOL_PARAMETER_EXAMPLE},
        principal="ask-agent-developer",
        roles="AgentDeveloper",
    )

    assert response.status_code == 503
    assert not fake_warehouse.statements


# ---------------------------------------------------------------------------
# 3. Propose-as-tool, from the answer
# ---------------------------------------------------------------------------


async def test_an_answer_can_be_proposed_as_a_tool_and_still_needs_the_gate(
    estate: _Estate, fake_warehouse: FakeSqlExecutor
) -> None:
    """The other half of the journey: an answer you want again becomes a tool
    proposal, and the proposal is a DRAFT -- proposing is not publishing.

    `POST /v1/agent-runs/{id}/tool-blueprint` renders the run's own stored SQL
    into a `GovernedToolVersionCreate`; posting that straight back to the
    drafting endpoint is the whole path from an answer to a candidate tool. It
    lands in DRAFT and needs the same submit-and-independent-approval the
    seeded tool went through, which is what stops the Ask screen from being a
    way to mint governed tools without a reviewer.
    """
    await _publish_seeded_tool(estate)
    # The proposer must be the run's own author (the endpoint refuses anyone
    # else) and hold an authoring role, so this asker carries both.
    answer = await _ask(
        estate,
        tool_parameters={seed.GOVERNED_TOOL_PARAMETER: seed.GOVERNED_TOOL_PARAMETER_EXAMPLE},
        principal="ask-analyst-author",
        roles="Analyst,ToolDeveloper",
    )
    assert answer.status_code == 200, answer.text
    agent_run_id = answer.json()["agent_run_id"]

    proposal = await estate.client.post(
        f"/v1/agent-runs/{agent_run_id}/tool-blueprint",
        headers=_ask_headers("ask-analyst-author", "Analyst,ToolDeveloper", estate.org_id),
    )

    assert proposal.status_code == 200, proposal.text
    blueprint = proposal.json()
    assert blueprint["project_id"] == estate.project_id
    definition = blueprint["definition"]
    assert "customer.account" in definition["sql_template"].lower()

    drafted = await estate.client.post(
        f"/v1/projects/{estate.project_id}/tools",
        json=definition,
        headers=_ask_headers("ask-analyst-author", "Analyst,ToolDeveloper", estate.org_id),
    )

    assert drafted.status_code == 201, drafted.text
    assert drafted.json()["status"] == "DRAFT", (
        "a tool proposed from an answer must still be reviewed before anyone can run it"
    )
