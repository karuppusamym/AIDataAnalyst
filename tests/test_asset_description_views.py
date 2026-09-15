"""R11-FP08: a view's description draft is built from what Atlas holds about its definition.

Views were drafted as tables -- "open_accounts is a table" -- and the evidence never looked at
the captured definition. This drives `gather_evidence` against in-memory SQLite for each state a
definition can be in, and pins that the draft text never carries the definition itself.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import compose_draft_text, gather_evidence
from aida.envelope_models import MetadataViewDefinition
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
