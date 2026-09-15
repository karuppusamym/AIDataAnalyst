"""R11-FP08: a view's description draft is built from what Atlas holds about its definition.

Views were drafted as tables -- "open_accounts is a table" -- and the evidence never looked at
the captured definition. This drives `gather_evidence` against in-memory SQLite for each state a
definition can be in, and pins that the draft text never carries the definition itself.

A view's draft is also published only while the definition it was written against stands: once
the definition moves, approval is refused with `DEFINITION_MOVED`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    DEFINITION_MOVED,
    apply_asset_description_draft,
    compose_draft_text,
    evidence_payload,
    gather_evidence,
)
from aida.envelope_models import MetadataViewDefinition
from aida.models import AssetDescriptionDraft, MetadataTable
from tests.support.task_agents import seed_estate, seed_table, task_agent_session

STORED = "SELECT account_id FROM public.accounts WHERE status = ?"


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


@pytest.mark.parametrize(
    ("definition", "state"),
    [
        ({"definition_sql_redacted": STORED}, "CAPTURED"),
        ({"definition_sql_redacted": STORED, "truncated": True}, "TRUNCATED"),
        ({"definition_sql_redacted": STORED, "screening_status": "QUARANTINED"}, "QUARANTINED"),
        (
            {
                "definition_sql_redacted": None,
                "availability": "UNAVAILABLE",
                "unavailable_reason": "module is encrypted",
            },
            "WITHHELD",
        ),
        (None, "NOT_CAPTURED"),
    ],
)
async def test_the_definition_state_reaches_the_evidence_and_the_draft_never_quotes_it(
    session: AsyncSession, definition: dict[str, Any] | None, state: str
) -> None:
    org, datasource, schema = await seed_estate(session)
    view = await seed_table(
        session, org, datasource, schema, name="open_accounts", object_type="VIEW"
    )
    if definition is not None:
        session.add(
            MetadataViewDefinition(
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=view.id,
                redaction_status="LEXICAL",
                fingerprint="fp",
                **definition,
            )
        )
    await session.flush()

    evidence = await gather_evidence(session, view)
    text = compose_draft_text(evidence)

    assert (evidence.object_kind, evidence.definition_state) == ("VIEW", state)
    if definition is not None and definition.get("definition_sql_redacted"):
        assert evidence.definition_digest == hashlib.sha256(STORED.encode("utf-8")).hexdigest()
    else:
        assert evidence.definition_digest is None
    assert text.startswith("open_accounts is a view")
    assert "SELECT" not in text and "status" not in text


async def test_a_table_gathers_no_definition_facts(session: AsyncSession) -> None:
    org, datasource, schema = await seed_estate(session)
    table = await seed_table(session, org, datasource, schema, name="accounts")

    evidence = await gather_evidence(session, table)

    assert (evidence.object_kind, evidence.definition_state, evidence.definition_digest) == (
        "TABLE",
        None,
        None,
    )


async def _pending_draft(session: AsyncSession, table: MetadataTable) -> AssetDescriptionDraft:
    evidence = await gather_evidence(session, table)
    draft = AssetDescriptionDraft(
        organization_id=table.organization_id,
        table_id=table.id,
        drafted_text=compose_draft_text(evidence),
        text_fingerprint="f" * 64,
        accuracy_score=0.8,
        clarity_score=0.8,
        style_score=0.8,
        completeness_score=0.8,
        overall_score=0.8,
        evidence=evidence_payload(evidence),
        status="PENDING_APPROVAL",
        created_by="agent:steward",
    )
    session.add(draft)
    await session.flush()
    return draft


async def _view_with_definition(
    session: AsyncSession,
) -> tuple[MetadataTable, MetadataViewDefinition]:
    org, datasource, schema = await seed_estate(session)
    view = await seed_table(
        session, org, datasource, schema, name="open_accounts", object_type="VIEW"
    )
    definition = MetadataViewDefinition(
        organization_id=org.id,
        datasource_id=datasource.id,
        table_id=view.id,
        definition_sql_redacted=STORED,
        redaction_status="LEXICAL",
        fingerprint="fp",
    )
    session.add(definition)
    await session.flush()
    return view, definition


async def test_a_view_draft_is_not_published_once_its_definition_has_moved(
    session: AsyncSession,
) -> None:
    view, definition = await _view_with_definition(session)
    draft = await _pending_draft(session, view)
    definition.definition_sql_redacted = "SELECT account_id, branch_id FROM public.accounts"
    await session.flush()

    with pytest.raises(HTTPException) as refused:
        await apply_asset_description_draft(
            session, draft, reviewer="reviewer", now=datetime.now(UTC)
        )

    assert refused.value.status_code == 409
    detail = refused.value.detail
    assert isinstance(detail, dict) and detail["code"] == DEFINITION_MOVED
    assert str(detail).startswith("The view's definition changed")
    assert (draft.status, draft.published_version_id) == ("PENDING_APPROVAL", None)


async def test_a_view_or_table_draft_whose_facts_stand_is_published(
    session: AsyncSession,
) -> None:
    view, _ = await _view_with_definition(session)
    view_draft = await _pending_draft(session, view)
    org, datasource, schema = await seed_estate(session)
    table = await seed_table(session, org, datasource, schema, name="accounts")
    table_draft = await _pending_draft(session, table)

    for draft in (view_draft, table_draft):
        event_type, version = await apply_asset_description_draft(
            session, draft, reviewer="reviewer", now=datetime.now(UTC)
        )
        assert (event_type, draft.status) == ("asset_description.approved.v1", "APPROVED")
        assert version.readme == draft.drafted_text
