"""ADR-0029: the steward agent, and the task-agent runtime it runs on.

The ADR rests on a handful of properties. Most belong to the shared runtime
(`aida.task_agent`) and are exercised here through the first agent built on it;
each gets a test against a real in-memory database rather than a double:

* **Authority is fail-closed.** No contract, no approved version, an
  ambiguous pair of contracts, another organization's contract, or a principal
  another agent is configured with is a refusal, and a refused run writes
  nothing.
* **The kill switch stops it** -- every scope, before the run, and at the next
  item when engaged mid-run; a run whose licence was withdrawn keeps nothing.
* **The autonomy tier is a ceiling.** T0 observes; T1, T2 and T3 propose
  identically; nothing is ever applied.
* **Maker != checker needs no special case.** A proposal is the agent's own
  request, so a human can decide it and the agent cannot.
* **It is reluctant and bounded.** Open drafts, rejected text and thin evidence
  are left alone; a run stops at its limit, its review backlog and its
  contract's wall clock.
* **It leaves a value-free ledger**, and an outcome measure that reports no
  rate rather than a zero when nothing has been decided.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
import aida.semantic_api  # noqa: F401 -- registers the decision target adapters
from aida import steward_agent, task_agent
from aida.agent_budget import REASON_WALL_CLOCK_CAP
from aida.agent_contracts import REASON_CONTRACT_MISSING, REASON_KILL_ENGAGED
from aida.asset_description_service import compose_draft_text, gather_evidence, text_fingerprint
from aida.db import Base
from aida.governance_decision_service import GovernanceDecisionRefused, decide_review
from aida.model_gateway import GLOBAL_KILL_SWITCH_SCOPE
from aida.models import (
    AGENT_SAMPLING_RATE_FLOOR,
    AgentContract,
    AgentTask,
    AiAsset,
    AiAssetVersion,
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    AssetTermLink,
    AuditEvent,
    DataDomain,
    DataSource,
    DbtResource,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    KillSwitchState,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.review_risk_tiers import HARD_MAX_AGENT_TIER, risk_tier_for, tier_at_or_below
from aida.security import SecurityContext
from aida.steward_agent import (
    SKIP_BELOW_EVIDENCE_BAR,
    SKIP_OPEN_DRAFT,
    SKIP_REJECTED_BEFORE,
    STEWARD_AGENT,
    run_steward_agent,
)
from aida.steward_agent_api import (
    StewardAgentRunRequest,
    get_steward_agent_state,
    start_steward_agent_run,
)
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    REASON_AUTONOMY_WITHDRAWN,
    REASON_CONTRACT_AMBIGUOUS,
    REASON_PRINCIPAL_RESERVED,
    REASON_VERSION_NOT_APPROVED,
    STOP_REVIEW_BACKLOG,
    TaskAgentOutcome,
    TaskAgentRefused,
    TaskAgentRunRequest,
    mode_for,
    reserved_principals,
    task_agent_outcomes,
)
from atlas.platform.config import Settings
from tests.support.doubles import security_context

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = "agent:steward"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite, with transactions made to behave like PostgreSQL's.

    The pysqlite driver defers BEGIN until a DML statement, so a SAVEPOINT
    issued before any write silently becomes the *outermost* transaction and
    its RELEASE commits. Every per-item savepoint the agent opens would then be
    committed as it closed, and the rollback a refused run depends on would
    have nothing left to undo -- a property of the driver, not of the agent.
    SQLAlchemy's documented aiosqlite recipe hands BEGIN back to SQLAlchemy.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _explicit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _seed_estate(
    session: AsyncSession, *, organization: Organization | None = None
) -> tuple[Organization, DataSource, MetadataSchema]:
    org = organization or Organization(
        id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}"
    )
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
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
    session.add_all(([] if organization else [org]) + [lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    return org, datasource, schema


async def _seed_table(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    columns: int = 3,
    dbt_description: str | None = "The customer master, one row per customer.",
) -> MetadataTable:
    """A table the worklist ranks. With the defaults its GL-9 draft scores
    about 0.48 -- over the 0.4 submission bar; with no columns and no dbt
    description it scores about 0.24, under it."""
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
    await session.flush()
    for position in range(columns):
        session.add(
            MetadataColumn(
                organization_id=org.id,
                table_id=table.id,
                name=f"col_{position}",
                ordinal_position=position + 1,
                physical_type="text",
                nullable=True,
                fingerprint="fp",
            )
        )
    if dbt_description is not None:
        session.add(
            DbtResource(
                organization_id=org.id,
                artifact_import_id=uuid4(),
                unique_id=f"model.bank.{name}",
                resource_type="model",
                package_name="bank",
                name=name,
                sql_parse_status="PARSED",
                description=dbt_description,
                matched_table_id=table.id,
            )
        )
    await session.flush()
    return table


async def _register(
    session: AsyncSession,
    org: Organization,
    *,
    tier: str = "T1",
    status: str = "APPROVED",
    principal: str = AGENT,
    kill_scope: str = "AGENT",
    kill_engaged: bool = False,
    wall_clock_seconds_cap: int | None = None,
) -> AgentContract:
    """What an administrator does once per organization: an AGENT-kind AI
    asset version and the contract the agent acts under."""
    asset = AiAsset(
        organization_id=org.id,
        asset_key=f"steward-{uuid4().hex[:8]}",
        asset_kind="AGENT",
        created_by="platform-admin",
    )
    session.add(asset)
    await session.flush()
    version = AiAssetVersion(
        organization_id=org.id,
        asset_id=asset.id,
        version=1,
        status=status,
        name="Steward agent",
        description="Drafts table descriptions and glossary links for review.",
        intended_use="Working the stewardship backlog.",
        owner_principal="steward-team",
        provider_type="INTERNAL",
        risk_tier="LOW",
        context_product_version_ids=[],
        model_route_ids=[],
        policy_control_ids=[],
        evaluation_evidence={},
        runtime_evidence={},
        fingerprint=uuid4().hex,
        created_by="platform-admin",
    )
    session.add(version)
    await session.flush()
    contract = AgentContract(
        organization_id=org.id,
        ai_asset_version_id=version.id,
        agent_principal_id=principal,
        capability_envelope={"tool_slugs": [], "context_product_ids": [], "write_lanes": []},
        autonomy_tier=tier,
        supervisor_persona="STEWARD",
        kill_scope=kill_scope,
        kill_engaged=kill_engaged,
        sampling_rate=AGENT_SAMPLING_RATE_FLOOR,
        wall_clock_seconds_cap=wall_clock_seconds_cap,
        created_by="platform-admin",
    )
    session.add(contract)
    await session.flush()
    return contract


async def _seed_label_match(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    label: str = "Customer",
) -> tuple[MetadataTable, GlossaryTerm, MetadataBusinessAnnotation]:
    """An approved term and an approved business annotation with the same label
    -- exactly what GL-8 turns into a link candidate."""
    table = await _seed_table(session, org, datasource, schema, name="customer_master")
    term = GlossaryTerm(
        id=uuid4(), organization_id=org.id, term_key="customer", lifecycle_status="ACTIVE"
    )
    session.add(term)
    await session.flush()
    session.add(
        GlossaryTermVersion(
            id=uuid4(),
            organization_id=org.id,
            term_id=term.id,
            version=1,
            status="APPROVED",
            display_name=label,
            definition="A party that holds at least one product with the bank.",
            synonyms=[],
            created_by="admin",
        )
    )
    annotation = MetadataBusinessAnnotation(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        table_id=table.id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=uuid4(),
    )
    session.add(annotation)
    await session.flush()
    session.add(
        MetadataBusinessAnnotationVersion(
            id=uuid4(),
            organization_id=org.id,
            annotation_id=annotation.id,
            version=1,
            status="APPROVED",
            business_name=label,
            business_description="Approved business context for this table.",
            table_role="FACT",
            grain_statement="One row per customer.",
            synonyms=[],
            suggested_questions=[],
            tags=[],
            confidence=0.9,
            approved_by="steward",
            approved_at=NOW,
        )
    )
    await session.flush()
    return table, term, annotation


def _draft(
    org: Organization, table: MetadataTable, *, status: str, created_by: str, text: str
) -> AssetDescriptionDraft:
    return AssetDescriptionDraft(
        organization_id=org.id,
        table_id=table.id,
        drafted_text=text,
        text_fingerprint=text_fingerprint(text),
        accuracy_score=0.5,
        clarity_score=0.5,
        style_score=0.5,
        completeness_score=0.5,
        overall_score=0.5,
        evidence={},
        status=status,
        created_by=created_by,
    )


def _human(org: Organization, principal_id: str = "steward-1") -> SecurityContext:
    return security_context(
        organization_id=org.id, principal_id=principal_id, roles=frozenset({"DataSteward"})
    )


async def _run(
    session: AsyncSession,
    org: Organization,
    *,
    settings: Settings | None = None,
    **request: Any,
) -> TaskAgentOutcome:
    return await run_steward_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(**request),
        settings=settings or _settings(),
        triggered_by=_human(org),
    )


async def _count(session: AsyncSession, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _engage_after_first_item(
    monkeypatch: pytest.MonkeyPatch, contract_id: Any, **values: Any
) -> list[int]:
    """Change the contract from 'another transaction' while the first item is
    being drafted: a Core UPDATE with no session synchronization, so the
    identity-map copy the run loaded at its start still says what it said.
    Only a per-item re-read from the database can see the change."""
    original = steward_agent.gather_evidence
    calls: list[int] = []

    async def engaging(active: AsyncSession, table: MetadataTable) -> Any:
        calls.append(1)
        if len(calls) == 1:
            await active.execute(
                update(AgentContract)
                .where(AgentContract.id == contract_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        return await original(active, table)

    monkeypatch.setattr(steward_agent, "gather_evidence", engaging)
    return calls


# ---------------------------------------------------------------------------
# Authority is fail-closed
# ---------------------------------------------------------------------------


async def test_an_unregistered_agent_is_refused_and_writes_nothing(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org)

    assert excinfo.value.reason_code == REASON_CONTRACT_MISSING
    assert await _count(session, AssetDescriptionDraft) == 0
    assert await _count(session, GovernanceReview) == 0


async def test_a_contract_on_an_unapproved_version_is_refused(session: AsyncSession) -> None:
    org, _datasource, _schema = await _seed_estate(session)
    await _register(session, org, status="REVIEW_REQUIRED")

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org)

    assert excinfo.value.reason_code == REASON_VERSION_NOT_APPROVED


async def test_two_approved_contracts_for_the_principal_are_ambiguous(
    session: AsyncSession,
) -> None:
    """Choosing one would let row order decide which authority the agent runs
    under."""
    org, _datasource, _schema = await _seed_estate(session)
    await _register(session, org)
    await _register(session, org)

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org)

    assert excinfo.value.reason_code == REASON_CONTRACT_AMBIGUOUS


async def test_another_organizations_contract_confers_nothing(session: AsyncSession) -> None:
    """INV-5: authority is per organization, like everything else."""
    org, datasource, schema = await _seed_estate(session)
    other, _other_datasource, _other_schema = await _seed_estate(session)
    await _register(session, other)
    await _seed_table(session, org, datasource, schema, name="customers")

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org)

    assert excinfo.value.reason_code == REASON_CONTRACT_MISSING


async def test_a_principal_equal_to_the_reviewer_agents_is_refused(
    session: AsyncSession,
) -> None:
    """The agent that drafts would be the agent that checks."""
    org, _datasource, _schema = await _seed_estate(session)
    await _register(session, org, principal="agent:reviewer")
    settings = _settings(
        steward_agent_principal_id="agent:reviewer", reviewer_agent_principal_id="agent:reviewer"
    )

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org, settings=settings)

    assert excinfo.value.reason_code == REASON_PRINCIPAL_RESERVED


def test_every_other_agent_principal_setting_is_reserved() -> None:
    """Reserved identities are read from the settings model, so an agent added
    later is reserved against this one the moment its setting exists."""
    reserved = reserved_principals(_settings(), STEWARD_AGENT)

    assert "agent:reviewer" in reserved
    assert AGENT not in reserved


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["AGENT", "TIER", "ALL", "MODEL_GATEWAY"])
async def test_every_kill_scope_refuses_the_run_before_any_work(
    session: AsyncSession, scope: str
) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    if scope == "AGENT":
        await _register(session, org, kill_engaged=True)
    else:
        await _register(session, org)
        if scope == "MODEL_GATEWAY":
            session.add(
                KillSwitchState(
                    organization_id=org.id,
                    route_key=GLOBAL_KILL_SWITCH_SCOPE,
                    engaged=True,
                    reason="incident",
                    engaged_by="operator",
                    engaged_at=NOW,
                )
            )
            await session.flush()
        else:
            # A different agent in the same organization whose engaged switch
            # covers this one: same tier for TIER, everything for ALL.
            await _register(
                session,
                org,
                principal=f"agent:peer-{scope.lower()}",
                kill_scope=scope,
                kill_engaged=True,
            )

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org)

    assert excinfo.value.reason_code == REASON_KILL_ENGAGED
    assert await _count(session, AssetDescriptionDraft) == 0


async def test_a_kill_switch_engaged_mid_run_stops_it_at_the_next_item(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, datasource, schema = await _seed_estate(session)
    for name in ("a_customers", "b_orders", "c_payments"):
        await _seed_table(session, org, datasource, schema, name=name)
    contract = await _register(session, org)
    calls = _engage_after_first_item(monkeypatch, contract.id, kill_engaged=True)

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org, capabilities=("TABLE_DESCRIPTION",))

    assert excinfo.value.reason_code == REASON_KILL_ENGAGED
    assert len(calls) == 1, "the run examined a table after the switch was engaged"


async def test_lowering_the_tier_mid_run_withdraws_the_licence_to_write(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, datasource, schema = await _seed_estate(session)
    for name in ("a_customers", "b_orders"):
        await _seed_table(session, org, datasource, schema, name=name)
    contract = await _register(session, org)
    _engage_after_first_item(monkeypatch, contract.id, autonomy_tier="T0")

    with pytest.raises(TaskAgentRefused) as excinfo:
        await _run(session, org, capabilities=("TABLE_DESCRIPTION",))

    assert excinfo.value.reason_code == REASON_AUTONOMY_WITHDRAWN


# ---------------------------------------------------------------------------
# The autonomy tier is a ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "mode"),
    [
        ("T0", "OBSERVE"),
        ("T1", "PROPOSE"),
        ("T2", "PROPOSE"),
        ("T3", "PROPOSE"),
        ("T9", "OBSERVE"),
        ("", "OBSERVE"),
    ],
)
def test_the_autonomy_tier_is_read_as_a_ceiling(tier: str, mode: str) -> None:
    assert mode_for(tier) == mode


async def test_a_t0_contract_observes_and_opens_no_proposal(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    await _register(session, org, tier="T0")

    outcome = await _run(session, org)

    assert outcome.mode == "OBSERVE"
    assert [item.action for item in outcome.items] == [ACTION_WOULD_PROPOSE]
    assert await _count(session, AssetDescriptionDraft) == 0
    assert await _count(session, GovernanceReview) == 0
    assert await _count(session, AgentTask) == 0


async def test_a_dry_run_observes_under_a_proposing_contract(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    await _register(session, org, tier="T1")

    outcome = await _run(session, org, dry_run=True)

    assert (outcome.mode, outcome.dry_run) == ("OBSERVE", True)
    assert outcome.count(ACTION_WOULD_PROPOSE) == 1
    assert await _count(session, GovernanceReview) == 0


@pytest.mark.parametrize("tier", ["T1", "T2", "T3"])
async def test_every_proposing_tier_proposes_the_same_and_applies_nothing(
    session: AsyncSession, tier: str
) -> None:
    """A T3 contract buys this agent nothing a T1 one does: it has no branch
    that applies its own output."""
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    await _register(session, org, tier=tier)

    outcome = await _run(session, org)

    assert outcome.count(ACTION_PROPOSED) == 1
    draft = await session.scalar(select(AssetDescriptionDraft))
    assert draft is not None and draft.status == "PENDING_APPROVAL"
    assert await _count(session, AssetDocumentationVersion) == 0


# ---------------------------------------------------------------------------
# A proposal is the agent's own request for review
# ---------------------------------------------------------------------------


async def test_a_proposal_is_requested_by_the_agent_and_ledgered(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    contract = await _register(session, org)

    outcome = await _run(session, org, capabilities=("TABLE_DESCRIPTION",))

    [item] = outcome.items
    assert item.action == ACTION_PROPOSED
    draft = await session.get(AssetDescriptionDraft, item.object_id)
    review = await session.get(GovernanceReview, item.review_id)
    task = await session.get(AgentTask, item.task_id)
    assert draft is not None and review is not None and task is not None
    assert (draft.created_by, draft.status) == (AGENT, "PENDING_APPROVAL")
    assert draft.governance_review_id == review.id
    assert (review.requested_by, review.status) == (AGENT, "PENDING")
    assert (review.object_type, review.object_id) == ("ASSET_DESCRIPTION_DRAFT", str(draft.id))
    assert review.requested_action == "PUBLISH"
    # The task stays PROPOSED: the decision lives on the review.
    assert task.status == "PROPOSED"
    assert (task.proposal_ref_type, task.proposal_ref_id) == ("GOVERNANCE_REVIEW", review.id)
    assert task.ai_asset_version_id == contract.ai_asset_version_id
    assert task.intent == "steward.propose_table_description"
    audits = {
        row.action: row
        for row in (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(["steward_agent.propose", "steward_agent.run"])
                )
            )
        ).all()
    }
    assert audits["steward_agent.propose"].principal_id == AGENT
    assert audits["steward_agent.run"].principal_id == "steward-1"
    assert audits["steward_agent.run"].details["proposed"] == 1


async def test_the_supervising_steward_may_decide_it_and_the_agent_may_not(
    session: AsyncSession,
) -> None:
    """INV-8 with no special case: the maker is the agent's identity, so the
    steward who started the run is an independent checker of text they did not
    write -- and the agent is refused as the checker of its own proposal."""
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    await _register(session, org)
    outcome = await _run(session, org, capabilities=("TABLE_DESCRIPTION",))
    review = await session.get(GovernanceReview, outcome.items[0].review_id)
    assert review is not None

    agent_context = SecurityContext(
        principal_id=AGENT,
        principal_type="AGENT",
        organization_id=org.id,
        roles=frozenset({"Reviewer"}),
    )
    with pytest.raises(GovernanceDecisionRefused):
        await decide_review(
            session, review, decision="APPROVE", reason="self", context=agent_context, now=NOW
        )

    await decide_review(
        session, review, decision="APPROVE", reason="accurate", context=_human(org), now=NOW
    )
    draft = await session.get(AssetDescriptionDraft, outcome.items[0].object_id)
    published = await session.scalar(select(AssetDocumentationVersion))
    assert draft is not None and published is not None
    assert draft.status == "APPROVED"
    assert published.readme == draft.drafted_text
    assert (published.created_by, published.approved_by) == (AGENT, "steward-1")


# ---------------------------------------------------------------------------
# Reluctance and bounds
# ---------------------------------------------------------------------------


async def test_it_leaves_open_drafts_rejected_text_and_thin_evidence_alone(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await _seed_estate(session)
    in_progress = await _seed_table(session, org, datasource, schema, name="a_in_progress")
    rejected = await _seed_table(session, org, datasource, schema, name="b_rejected")
    await _seed_table(
        session, org, datasource, schema, name="c_thin", columns=0, dbt_description=None
    )
    session.add(
        _draft(
            org,
            in_progress,
            status="DRAFT",
            created_by="auto-enqueue-drafter",
            text="An unsubmitted draft somebody is still editing.",
        )
    )
    rejected_text = compose_draft_text(await gather_evidence(session, rejected))
    session.add(_draft(org, rejected, status="REJECTED", created_by="agent:x", text=rejected_text))
    await session.flush()
    await _register(session, org)

    outcome = await _run(session, org, capabilities=("TABLE_DESCRIPTION",))

    assert {item.subject_name: (item.action, item.reason) for item in outcome.items} == {
        "a_in_progress": (ACTION_SKIPPED, SKIP_OPEN_DRAFT),
        "b_rejected": (ACTION_SKIPPED, SKIP_REJECTED_BEFORE),
        "c_thin": (ACTION_SKIPPED, SKIP_BELOW_EVIDENCE_BAR),
    }
    assert await _count(session, GovernanceReview) == 0
    assert await _count(session, AssetDescriptionDraft) == 2


async def test_configuration_clamps_the_limit_a_request_asks_for(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    for name in ("a", "b", "c", "d"):
        await _seed_table(session, org, datasource, schema, name=name)
    await _register(session, org)

    outcome = await _run(
        session,
        org,
        settings=_settings(steward_agent_max_proposals_per_run=2),
        limit=10,
        capabilities=("TABLE_DESCRIPTION",),
    )

    assert outcome.limit == 2
    assert outcome.count(ACTION_PROPOSED) == 2
    assert await _count(session, GovernanceReview) == 2


async def test_a_full_review_backlog_stops_the_run_and_keeps_what_it_did(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await _seed_estate(session)
    for name in ("a", "b", "c"):
        await _seed_table(session, org, datasource, schema, name=name)
    await _register(session, org)
    settings = _settings(steward_agent_max_pending_proposals=1)

    first = await _run(session, org, settings=settings, capabilities=("TABLE_DESCRIPTION",))
    second = await _run(session, org, settings=settings, capabilities=("TABLE_DESCRIPTION",))

    assert first.count(ACTION_PROPOSED) == 1
    assert first.stopped_reason == STOP_REVIEW_BACKLOG
    # The next run sees the first run's proposal still waiting and does not add
    # to the queue: the backlog counts the agent's proposals, not one run's.
    assert second.count(ACTION_PROPOSED) == 0
    assert second.stopped_reason == STOP_REVIEW_BACKLOG
    assert await _count(session, GovernanceReview) == 1


async def test_the_contracts_wall_clock_cap_ends_the_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    await _register(session, org, wall_clock_seconds_cap=30)
    caps_checked: list[int | None] = []

    def expired(contract: AgentContract, *, started_at: datetime, now: datetime) -> str:
        caps_checked.append(contract.wall_clock_seconds_cap)
        return REASON_WALL_CLOCK_CAP

    monkeypatch.setattr(task_agent, "wall_clock_violation", expired)

    outcome = await _run(session, org)

    assert outcome.stopped_reason == REASON_WALL_CLOCK_CAP
    assert outcome.count(ACTION_PROPOSED) == 0
    assert caps_checked == [30]


async def test_a_datasource_scope_keeps_the_run_inside_it(session: AsyncSession) -> None:
    org, in_scope, schema = await _seed_estate(session)
    _org, out_of_scope, other_schema = await _seed_estate(session, organization=org)
    await _seed_table(session, org, in_scope, schema, name="a_inside")
    await _seed_table(session, org, out_of_scope, other_schema, name="b_outside")
    await _register(session, org)

    outcome = await _run(session, org, datasource_id=in_scope.id)

    assert [item.subject_name for item in outcome.items] == ["a_inside"]


# ---------------------------------------------------------------------------
# Glossary links
# ---------------------------------------------------------------------------


async def test_an_exact_label_match_becomes_a_link_proposal_in_review(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await _seed_estate(session)
    table, term, _annotation = await _seed_label_match(session, org, datasource, schema)
    await _register(session, org)

    outcome = await _run(session, org)

    # The annotated table is documented, so only the link capability acts.
    [item] = outcome.items
    assert (item.capability, item.action) == ("GLOSSARY_LINK", ACTION_PROPOSED)
    assert (item.related_id, item.confidence) == (term.id, 1.0)
    proposal = await session.get(GlossaryLinkProposal, item.object_id)
    review = await session.get(GovernanceReview, item.review_id)
    assert proposal is not None and review is not None
    assert (proposal.status, proposal.created_by) == ("REVIEW_REQUIRED", AGENT)
    assert proposal.governance_review_id == review.id
    assert (review.object_type, review.requested_action, review.requested_by) == (
        "GLOSSARY_LINK_PROPOSAL",
        "APPROVE_LINK",
        AGENT,
    )

    await decide_review(
        session, review, decision="APPROVE", reason="right term", context=_human(org), now=NOW
    )
    link = await session.scalar(select(AssetTermLink))
    assert link is not None and (link.table_id, link.term_id) == (table.id, term.id)


async def test_a_link_a_human_rejected_is_never_raised_again(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    table, term, annotation = await _seed_label_match(session, org, datasource, schema)
    session.add(
        GlossaryLinkProposal(
            organization_id=org.id,
            table_id=table.id,
            term_id=term.id,
            source_annotation_id=annotation.id,
            confidence=1.0,
            evidence={},
            status="REJECTED",
            created_by="steward-2",
        )
    )
    await session.flush()
    await _register(session, org)

    outcome = await _run(session, org, capabilities=("GLOSSARY_LINK",))

    assert outcome.items == []


# ---------------------------------------------------------------------------
# Ledger and outcomes
# ---------------------------------------------------------------------------


async def test_the_ledger_carries_ids_and_hashes_never_the_drafted_text(
    session: AsyncSession,
) -> None:
    """INV-6's discipline for the task ledger: ids, hashes and scores."""
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    await _register(session, org)

    outcome = await _run(session, org, capabilities=("TABLE_DESCRIPTION",))

    task = await session.get(AgentTask, outcome.items[0].task_id)
    draft = await session.get(AssetDescriptionDraft, outcome.items[0].object_id)
    assert task is not None and draft is not None
    assert set(task.evidence) == {
        "run_id",
        "review_id",
        "object_type",
        "object_id",
        "confidence",
        "rank",
    }
    ledger = json.dumps({"evidence": task.evidence, "inputs": task.inputs_fingerprint})
    assert draft.drafted_text not in ledger
    assert "customer master" not in ledger.lower()


async def test_outcomes_report_no_rate_until_something_is_decided(session: AsyncSession) -> None:
    org, datasource, schema = await _seed_estate(session)
    for name in ("a", "b"):
        await _seed_table(session, org, datasource, schema, name=name)
    await _register(session, org)
    await _run(session, org, capabilities=("TABLE_DESCRIPTION",))

    [before] = await task_agent_outcomes(session, org.id, agent_principal_id=AGENT)
    assert (before.pending, before.approved, before.rejected) == (2, 0, 0)
    assert before.acceptance_rate is None

    first, second = (await session.scalars(select(GovernanceReview))).all()
    reviewer = _human(org, "reviewer-1")
    await decide_review(session, first, decision="APPROVE", reason="ok", context=reviewer, now=NOW)
    await decide_review(
        session, second, decision="REJECT", reason="wrong grain", context=reviewer, now=NOW
    )

    [after] = await task_agent_outcomes(session, org.id, agent_principal_id=AGENT)
    assert (after.pending, after.approved, after.rejected) == (0, 1, 1)
    assert after.acceptance_rate == 0.5


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_every_proposal_the_agent_can_open_is_inside_the_agent_tier_ceiling() -> None:
    for capability in STEWARD_AGENT.capabilities:
        assert tier_at_or_below(
            risk_tier_for(capability.object_type), HARD_MAX_AGENT_TIER
        ), capability.object_type


@pytest.mark.parametrize("module", ["steward_agent.py", "task_agent.py"])
def test_the_agent_cannot_reach_a_decision_or_publish_path(module: str) -> None:
    """The ADR's 'it may decide none' as a property of the source, the same
    static check `test_reviewer_agent` makes of the reviewer agent."""
    source = (REPO_ROOT / "src" / "aida" / module).read_text(encoding="utf-8")
    for forbidden in (
        "decide_review",
        "_apply_governance_review_decision",
        "claim_review",
        "apply_asset_description_draft",
        "publish_asset_documentation_version",
        "apply_column_description_draft",
        "publish_column_description",
        "apply_link_proposal",
        "auto_decide",
    ):
        assert forbidden not in source, forbidden


def test_a_run_request_refuses_duplicate_capabilities() -> None:
    with pytest.raises(ValidationError):
        StewardAgentRunRequest(capabilities=["TABLE_DESCRIPTION", "TABLE_DESCRIPTION"])


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


async def test_the_run_endpoint_refuses_with_409_and_audits_the_refusal(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    org_id, context = org.id, _human(org)
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await start_steward_agent_run(
            org_id, StewardAgentRunRequest(), context=context, session=session, settings=_settings()
        )

    assert (excinfo.value.status_code, excinfo.value.detail) == (409, REASON_CONTRACT_MISSING)
    denied = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "steward_agent.run")
    )
    assert denied is not None
    assert (denied.outcome, denied.details["reason"]) == ("DENIED", REASON_CONTRACT_MISSING)


async def test_a_run_whose_licence_is_withdrawn_mid_run_keeps_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first table was proposed before the switch was engaged. The run is
    rolled back anyway: nothing produced by a run whose licence was withdrawn
    survives it."""
    org, datasource, schema = await _seed_estate(session)
    for name in ("a_customers", "b_orders"):
        await _seed_table(session, org, datasource, schema, name=name)
    contract = await _register(session, org)
    org_id, context, contract_id = org.id, _human(org), contract.id
    await session.commit()
    _engage_after_first_item(monkeypatch, contract_id, kill_engaged=True)

    with pytest.raises(HTTPException) as excinfo:
        await start_steward_agent_run(
            org_id, StewardAgentRunRequest(), context=context, session=session, settings=_settings()
        )

    assert (excinfo.value.status_code, excinfo.value.detail) == (409, REASON_KILL_ENGAGED)
    assert await _count(session, AssetDescriptionDraft) == 0
    assert await _count(session, GovernanceReview) == 0
    assert await _count(session, AgentTask) == 0


async def test_a_datasource_in_another_organization_is_not_found(session: AsyncSession) -> None:
    org, _datasource, _schema = await _seed_estate(session)
    _other, foreign_datasource, _foreign_schema = await _seed_estate(session)
    org_id, context, foreign_id = org.id, _human(org), foreign_datasource.id
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await start_steward_agent_run(
            org_id,
            StewardAgentRunRequest(datasource_id=foreign_id),
            context=context,
            session=session,
            settings=_settings(),
        )

    assert excinfo.value.status_code == 404


async def test_the_state_endpoint_reports_an_unregistered_agent_honestly(
    session: AsyncSession,
) -> None:
    org, _datasource, _schema = await _seed_estate(session)

    state = await get_steward_agent_state(
        org.id, context=_human(org), session=session, settings=_settings()
    )

    assert state.agent_key == "steward"
    assert (state.registered, state.refusal_reason) == (False, REASON_CONTRACT_MISSING)
    assert (state.method, state.uses_model) == ("DETERMINISTIC", False)
    assert {(c.capability, c.object_type, c.risk_tier) for c in state.capabilities} == {
        ("TABLE_DESCRIPTION", "ASSET_DESCRIPTION_DRAFT", "T0"),
        ("COLUMN_DESCRIPTION", "COLUMN_DESCRIPTION_DRAFT", "T0"),
        ("GLOSSARY_LINK", "GLOSSARY_LINK_PROPOSAL", "T1"),
    }
    assert state.outcomes == []


async def test_the_state_endpoint_reports_mode_kill_state_and_outcomes(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await _seed_estate(session)
    await _seed_table(session, org, datasource, schema, name="customers")
    contract = await _register(session, org)
    await _run(session, org, capabilities=("TABLE_DESCRIPTION",))
    contract.kill_engaged = True
    await session.flush()

    state = await get_steward_agent_state(
        org.id, context=_human(org), session=session, settings=_settings()
    )

    assert state.registered is True
    assert (state.autonomy_tier, state.mode) == ("T1", "PROPOSE")
    assert (state.kill_engaged, state.blocking_reason) == (True, REASON_KILL_ENGAGED)
    assert state.pending_proposals == 1
    [row] = state.outcomes
    assert (row.object_type, row.pending, row.acceptance_rate) == (
        "ASSET_DESCRIPTION_DRAFT",
        1,
        None,
    )
