"""UX-13 -- `GET /v1/metadata/tables/{table_id}/evidence`.

Runs the real endpoint body (`aida.asset_evidence_api.get_asset_evidence`) and
the real composition (`aida.asset_evidence.compose_asset_evidence`) against an
in-memory SQLite database, following `test_catalog_rows_read_model.py`'s own
rationale: PostgreSQL is unreachable in this sandbox, but SQLite is a real SQL
engine that enforces the same row semantics the composed queries rely on.

Sections:

1. every claim in the composed pane carries a `source` string, sourced from
   the module the tracker's exit criterion names (business meaning,
   GL-2/GL-5, data quality, CX-4 consumption, LN-3 AI decisions);
2. the refusal edge is surfaced only for a run that actually considered this
   table -- not for every refusal in the organization, and not silently
   dropped when one exists;
3. permission enforcement matches `list_catalog_rows` (UX-12): cross-org
   denied before the database is touched, and a policy-denied gate 403s;
4. UX-7's `.../evidence/export` route: the exported artifact matches the
   live endpoint's own composed output (no separate/stale derivation), and
   is denied identically -- through the same `gate` reference, not a
   separate or weaker check.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.asset_evidence_api import export_asset_evidence, get_asset_evidence
from aida.authorization_gate import AuthorizationDenied, GateOutcome
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AiDecisionRecord,
    AssetCertification,
    AssetDescriptionDraft,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    ConsumptionRecord,
    DataDomain,
    DataQualityIncident,
    DataSource,
    GlossaryTermVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OwnershipAssignment,
    Project,
)
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings()
_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


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
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return datasource


async def _seed_table(session: AsyncSession, datasource: DataSource, *, name: str) -> MetadataTable:
    schema = datasource._test_schema  # type: ignore[attr-defined]
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


def _context(datasource: DataSource, **overrides: object) -> object:
    return security_context(organization_id=datasource.organization_id, **overrides)


async def _evidence(table: MetadataTable, datasource: DataSource, session: AsyncSession):
    return await get_asset_evidence(
        table.id,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )


async def _export(table: MetadataTable, datasource: DataSource, session: AsyncSession):
    return await export_asset_evidence(
        table.id,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )


# ---------------------------------------------------------------------------
# 1. Every claim carries a source
# ---------------------------------------------------------------------------


async def test_business_meaning_ownership_and_certification_items_carry_sources(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="accounts")

    documentation = AssetDocumentation(
        id=uuid4(), organization_id=table.organization_id, table_id=table.id
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            documentation_id=documentation.id,
            version=1,
            status="APPROVED",
            readme="Deposit account records, one row per account.",
            owner_principal="Docs Fallback Owner",
            created_by="drafter",
            approved_by="reviewer",
            approved_at=_NOW,
        )
    )
    session.add(
        OwnershipAssignment(
            id=uuid4(),
            organization_id=table.organization_id,
            subject_type="TABLE",
            subject_id=str(table.id),
            owner_type="TEAM",
            owner_principal="Retail Data Office",
            assignment_kind="MANUAL",
            status="ACTIVE",
            assigned_by="steward",
        )
    )
    session.add(
        AssetCertification(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Reviewed against the certification checklist.",
            certified_by="reviewer",
            expires_at=_NOW + timedelta(days=180),
        )
    )
    term_id = uuid4()
    session.add(
        GlossaryTermVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            term_id=term_id,
            version=1,
            status="APPROVED",
            display_name="Deposit Account",
            definition="A customer's deposit account.",
            created_by="steward",
        )
    )
    session.add(
        AssetTermLink(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            term_id=term_id,
            linked_by="steward",
        )
    )
    await session.commit()

    evidence = await _evidence(table, datasource, session)

    assert evidence.table_id == table.id
    assert evidence.table_name == "accounts"

    by_category = {}
    for item in evidence.items:
        by_category.setdefault(item.category, []).append(item)

    description_items = by_category["BUSINESS_MEANING"]
    described = next(i for i in description_items if i.claim.startswith("Description:"))
    assert "Deposit account records" in described.claim
    assert described.source == "asset_documentation_version (GL-9, APPROVED)"

    term_item = next(i for i in description_items if "glossary term" in i.claim)
    assert "Deposit Account" in term_item.claim
    assert "GL-8/SM-2" in term_item.source

    (owner_item,) = by_category["OWNERSHIP"]
    assert owner_item.claim == "Owned by Retail Data Office"
    assert owner_item.source == "ownership_assignment (GL-2, ACTIVE)"

    (cert_item,) = by_category["CERTIFICATION"]
    assert cert_item.claim.startswith("Certification: CERTIFIED")
    assert cert_item.source == "asset_certification (GL-5/CT-5)"

    # Every single item, across every category, carries a non-empty source.
    assert all(item.source for item in evidence.items)


async def test_pending_draft_description_is_named_as_a_proposal_not_fact(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="pending")
    session.add(
        AssetDescriptionDraft(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            drafted_text="Deterministically drafted, awaiting review.",
            text_fingerprint="fp",
            accuracy_score=0.5,
            clarity_score=0.5,
            style_score=0.5,
            completeness_score=0.5,
            overall_score=0.5,
            evidence={},
            status="PENDING_APPROVAL",
            created_by="drafter",
        )
    )
    await session.commit()

    evidence = await _evidence(table, datasource, session)

    (item,) = [i for i in evidence.items if i.category == "BUSINESS_MEANING"]
    assert item.claim.startswith("Proposed description, awaiting review:")
    assert item.source == "asset_description_draft (GL-9, PENDING_APPROVAL)"


async def test_open_incident_produces_summary_and_itemized_claims(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_incident")
    incident = DataQualityIncident(
        id=uuid4(),
        organization_id=table.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        fingerprint=uuid4().hex,
        anomaly_type="VOLUME",
        severity="HIGH",
        status="OPEN",
        summary="Volume dropped below baseline.",
        first_observed_at=_NOW,
        last_observed_at=_NOW,
    )
    session.add(incident)
    await session.commit()

    evidence = await _evidence(table, datasource, session)
    dq_items = [i for i in evidence.items if i.category == "DATA_QUALITY"]

    summary_item = next(i for i in dq_items if i.claim.startswith("Overall quality state:"))
    assert "INCIDENT_OPEN" in summary_item.claim

    incident_item = next(i for i in dq_items if str(incident.id) in i.source)
    assert "HIGH VOLUME incident" in incident_item.claim
    assert "Volume dropped below baseline." in incident_item.claim
    assert incident_item.source == f"data_quality_incident:{incident.id}"


async def test_no_open_incident_reports_unknown_with_no_itemized_incident(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_clean")
    await session.commit()

    evidence = await _evidence(table, datasource, session)
    dq_items = [i for i in evidence.items if i.category == "DATA_QUALITY"]

    assert len(dq_items) == 1
    assert dq_items[0].claim == "Overall quality state: UNKNOWN"


# ---------------------------------------------------------------------------
# 2. Consumption lineage (CX-4)
# ---------------------------------------------------------------------------


async def test_consumption_records_are_scoped_to_this_table_newest_first(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_consumed")
    other_table = await _seed_table(session, datasource, name="t_other")

    older = ConsumptionRecord(
        id=uuid4(),
        organization_id=table.organization_id,
        consumer_id="agent-1",
        consumer_type="AGENT",
        resource_type="metadata_table",
        resource_id=str(table.id),
        channel="MCP",
        correlation_id="corr-1",
        policy_decision="ALLOW",
        consumed_at=_NOW - timedelta(hours=1),
    )
    newer = ConsumptionRecord(
        id=uuid4(),
        organization_id=table.organization_id,
        consumer_id="user-2",
        consumer_type="USER",
        resource_type="metadata_table",
        resource_id=str(table.id),
        channel="REST",
        correlation_id="corr-2",
        policy_decision="ALLOW",
        consumed_at=_NOW,
    )
    unrelated = ConsumptionRecord(
        id=uuid4(),
        organization_id=table.organization_id,
        consumer_id="agent-3",
        consumer_type="AGENT",
        resource_type="metadata_table",
        resource_id=str(other_table.id),
        channel="MCP",
        correlation_id="corr-3",
        policy_decision="ALLOW",
        consumed_at=_NOW,
    )
    session.add_all([older, newer, unrelated])
    await session.commit()

    evidence = await _evidence(table, datasource, session)
    consumption_items = [i for i in evidence.items if i.category == "CONSUMPTION"]

    total_item = next(i for i in consumption_items if i.claim.startswith("2 total"))
    assert total_item.source == "consumption_record (CX-4)"

    record_items = [i for i in consumption_items if i is not total_item]
    assert len(record_items) == 2
    assert record_items[0].source == f"consumption_record:{newer.id}"
    assert record_items[1].source == f"consumption_record:{older.id}"
    assert "user-2" in record_items[0].claim
    assert all(str(other_table.id) not in i.claim for i in record_items)


# ---------------------------------------------------------------------------
# 3. AI decision lineage (LN-3), including the refusal edge
# ---------------------------------------------------------------------------


async def test_ai_decision_and_refusal_for_the_same_run_both_appear(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_decided")
    run_id = uuid4()

    selection = AiDecisionRecord(
        id=uuid4(),
        organization_id=table.organization_id,
        run_id=run_id,
        decision_type="RETRIEVAL_SELECTED",
        source_node="governed_retriever",
        target_node=f"table:{table.id}",
        reason="ranked #1 of 4 candidates (score=0.95)",
        evidence={},
        decided_at=_NOW,
    )
    refusal = AiDecisionRecord(
        id=uuid4(),
        organization_id=table.organization_id,
        run_id=run_id,
        decision_type="REFUSAL",
        source_node="query_execution_gateway",
        target_node=f"agent_run:{run_id}",
        reason="cost ceiling exceeded",
        evidence={},
        decided_at=_NOW + timedelta(seconds=1),
    )
    session.add_all([selection, refusal])
    await session.commit()

    evidence = await _evidence(table, datasource, session)
    ai_items = [i for i in evidence.items if i.category == "AI_DECISION"]

    selection_item = next(i for i in ai_items if i.source == f"ai_decision_record:{selection.id}")
    assert "RETRIEVAL_SELECTED" in selection_item.claim
    assert "governed_retriever" in selection_item.claim

    refusal_item = next(i for i in ai_items if str(refusal.id) in i.source)
    assert refusal_item.claim.startswith("Run refused after considering this asset:")
    assert "cost ceiling exceeded" in refusal_item.claim
    assert str(run_id) in refusal_item.source


async def test_refusal_from_an_unrelated_run_is_not_surfaced(session) -> None:
    """A REFUSAL's own `target_node` is the agent run, not the asset -- this
    proves the honest scoping documented in `aida.asset_evidence`: a refusal
    only surfaces for a run that actually considered this table, not for
    every refusal in the organization.
    """
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_never_considered")
    unrelated_run_id = uuid4()
    session.add(
        AiDecisionRecord(
            id=uuid4(),
            organization_id=table.organization_id,
            run_id=unrelated_run_id,
            decision_type="REFUSAL",
            source_node="prompt_risk_classifier",
            target_node=f"agent_run:{unrelated_run_id}",
            reason="BLOCKED by prompt risk classifier",
            evidence={},
            decided_at=_NOW,
        )
    )
    await session.commit()

    evidence = await _evidence(table, datasource, session)
    ai_items = [i for i in evidence.items if i.category == "AI_DECISION"]

    assert ai_items == []


# ---------------------------------------------------------------------------
# 4. Permission enforcement (matches UX-12)
# ---------------------------------------------------------------------------


async def test_a_foreign_organization_is_denied_before_the_database_is_touched(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_secure")
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_asset_evidence(
            table.id,
            context=security_context(organization_id=uuid4()),  # a different organization
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code == 403


async def test_missing_table_is_404(session) -> None:
    datasource = await _seed_datasource(session)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_asset_evidence(
            uuid4(),
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code == 404


async def test_policy_denied_gate_is_a_403(session, monkeypatch: pytest.MonkeyPatch) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_gated")
    await session.commit()

    async def fake_gate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AuthorizationDenied("policy_denied")

    monkeypatch.setattr("aida.asset_evidence_api.gate", fake_gate)

    with pytest.raises(HTTPException) as exc_info:
        await get_asset_evidence(
            table.id,
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "policy_denied"


async def test_allowed_gate_still_returns_evidence(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_allowed")
    await session.commit()

    async def fake_gate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return GateOutcome(workspace_id=None, reason_code="ok", decided=True)

    monkeypatch.setattr("aida.asset_evidence_api.gate", fake_gate)

    evidence = await _evidence(table, datasource, session)
    assert evidence.table_id == table.id


# ---------------------------------------------------------------------------
# 5. UX-7 -- evidence export (`.../evidence/export`)
# ---------------------------------------------------------------------------


async def test_export_content_matches_the_live_evidence_endpoints_output(session) -> None:
    """The exported artifact is `compose_asset_evidence`'s own output,
    serialized verbatim -- not a separate/stale derivation. `generated_at` is
    excluded from the comparison because each call composes at its own
    instant (neither endpoint takes a frozen `now`); every other field,
    including every `items` entry, must match exactly.
    """
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_export")
    session.add(
        DataQualityIncident(
            id=uuid4(),
            organization_id=table.organization_id,
            datasource_id=datasource.id,
            table_id=table.id,
            fingerprint=uuid4().hex,
            anomaly_type="VOLUME",
            severity="HIGH",
            status="OPEN",
            summary="Volume dropped below baseline.",
            first_observed_at=_NOW,
            last_observed_at=_NOW,
        )
    )
    await session.commit()

    live = await _evidence(table, datasource, session)
    response = await _export(table, datasource, session)

    assert response.media_type == "application/json"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="table-{table.id}-evidence.json"'
    )
    assert response.headers["X-Artifact-SHA256"] == hashlib.sha256(response.body).hexdigest()

    exported = json.loads(response.body)
    live_dict = json.loads(live.model_dump_json())
    exported.pop("generated_at")
    live_dict.pop("generated_at")
    assert exported == live_dict
    assert exported["items"]  # not an empty export -- the seeded incident round-tripped


async def test_export_of_an_asset_with_no_evidence_is_still_well_formed(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_export_empty")
    await session.commit()

    response = await _export(table, datasource, session)
    exported = json.loads(response.body)

    assert exported["table_id"] == str(table.id)
    assert exported["table_name"] == "t_export_empty"
    assert response.headers["X-Artifact-SHA256"] == hashlib.sha256(response.body).hexdigest()


# ---------------------------------------------------------------------------
# 6. UX-7 -- export re-runs the SAME gate as the live endpoint, not a
#    separate or weaker one
# ---------------------------------------------------------------------------


async def test_export_denies_a_foreign_organization_identically_to_the_live_endpoint(
    session,
) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_export_secure")
    await session.commit()

    foreign_context = security_context(organization_id=uuid4())

    with pytest.raises(HTTPException) as live_exc:
        await get_asset_evidence(
            table.id, context=foreign_context, session=session, settings=_SETTINGS
        )
    with pytest.raises(HTTPException) as export_exc:
        await export_asset_evidence(
            table.id, context=foreign_context, session=session, settings=_SETTINGS
        )

    assert live_exc.value.status_code == export_exc.value.status_code == 403


async def test_export_and_live_endpoint_are_denied_by_the_same_policy_gate_identically(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the export path is not a separate, weaker check: patching the
    one `gate` reference both routes import (`aida.asset_evidence_api.gate`)
    denies both identically, with the same reason code.
    """
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_export_gated")
    await session.commit()

    async def fake_gate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AuthorizationDenied("policy_denied")

    monkeypatch.setattr("aida.asset_evidence_api.gate", fake_gate)

    with pytest.raises(HTTPException) as live_exc:
        await get_asset_evidence(
            table.id, context=_context(datasource), session=session, settings=_SETTINGS
        )
    with pytest.raises(HTTPException) as export_exc:
        await export_asset_evidence(
            table.id, context=_context(datasource), session=session, settings=_SETTINGS
        )

    assert live_exc.value.status_code == export_exc.value.status_code == 403
    assert live_exc.value.detail == export_exc.value.detail == "policy_denied"


async def test_export_allowed_gate_still_returns_a_downloadable_artifact(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_export_allowed")
    await session.commit()

    async def fake_gate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return GateOutcome(workspace_id=None, reason_code="ok", decided=True)

    monkeypatch.setattr("aida.asset_evidence_api.gate", fake_gate)

    response = await _export(table, datasource, session)
    exported = json.loads(response.body)
    assert exported["table_id"] == str(table.id)


async def test_export_missing_table_is_404(session) -> None:
    datasource = await _seed_datasource(session)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await export_asset_evidence(
            uuid4(), context=_context(datasource), session=session, settings=_SETTINGS
        )
    assert exc_info.value.status_code == 404
