"""R11-FP10: a refused description proposal does not come back unchanged.

Rejection suppression compared only `text_fingerprint`, and three things slipped past it:

* a steward edited a machine draft and a reviewer rejected it -- the edit rewrote the
  fingerprint, so the original machine text was proposed again on the next run;
* the drafting template's wording changed (R11-FP08 just changed every view's) -- the same facts
  in new words read as a new proposal;
* a description approved and then withdrawn (R11-C8) could be proposed again word for word.

These pin the one shared rule the steward agent and both generate routes now use.
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    REFUSED_EVIDENCE,
    REFUSED_TEXT,
    REFUSED_WITHDRAWN,
    TABLE_EVIDENCE_SIGNALS,
    refusal_reason,
    signals_fingerprint,
    table_refusal,
    text_fingerprint,
)
from aida.column_description_service import column_refusal
from aida.models import AssetDocumentation, AssetDocumentationVersion
from tests.support.task_agents import seed_estate, seed_table, task_agent_session


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def test_the_evidence_fingerprint_ignores_who_drafted_it_and_list_order() -> None:
    first = {"column_count": 3, "bound_term_ids": ["b", "a"], "agent_run": "r1", "origin": "X"}
    second = {"column_count": 3, "bound_term_ids": ["a", "b"], "agent_run": "r2", "editors": ["s"]}

    assert signals_fingerprint(first, TABLE_EVIDENCE_SIGNALS) == signals_fingerprint(
        second, TABLE_EVIDENCE_SIGNALS
    )
    assert signals_fingerprint({"agent_run": "r1"}, TABLE_EVIDENCE_SIGNALS) is None
    assert signals_fingerprint({"column_count": 4}, TABLE_EVIDENCE_SIGNALS) != signals_fingerprint(
        {"column_count": 3}, TABLE_EVIDENCE_SIGNALS
    )


def test_an_edited_then_rejected_draft_keeps_its_machine_text_out() -> None:
    machine = "accounts is a table in the public schema with 3 columns."
    rewrite = text_fingerprint("A steward's rewrite.")
    refused = [(rewrite, {"original_fingerprint": text_fingerprint(machine)})]

    assert (
        refusal_reason(
            drafted_text=machine, payload={}, refused=refused, signal_keys=TABLE_EVIDENCE_SIGNALS
        )
        == REFUSED_TEXT
    )


def test_new_words_on_unchanged_evidence_are_refused_and_new_evidence_is_not() -> None:
    payload = {"column_count": 3, "bound_term_ids": ["t1"]}
    refused = [(text_fingerprint("Old wording."), {**payload, "agent_run": "earlier"})]

    def reason(**overrides: Any) -> str | None:
        return refusal_reason(
            drafted_text="New wording.",
            payload={**payload, **overrides},
            refused=refused,
            signal_keys=TABLE_EVIDENCE_SIGNALS,
        )

    assert reason() == REFUSED_EVIDENCE
    assert reason(bound_term_ids=["t1", "t2"]) is None


def test_a_rejected_metadata_column_draft_does_not_suppress_a_model_rewrite() -> None:
    facts = {"column": "public.accounts.status", "physical_type": "text", "nullable": False}
    refused = [(text_fingerprint("Old metadata text."), {**facts, "origin": "METADATA"})]

    assert column_refusal("New text.", {**facts, "origin": "METADATA"}, refused) == REFUSED_EVIDENCE
    assert column_refusal("New text.", {**facts, "origin": "MODEL_INFERRED"}, refused) is None


async def test_words_approved_once_and_withdrawn_are_not_proposed_again(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    table = await seed_table(session, org, datasource, schema, name="accounts")
    documentation = AssetDocumentation(organization_id=org.id, table_id=table.id)
    session.add(documentation)
    await session.flush()
    withdrawn_text = "accounts holds one row per deposit account."
    session.add(
        AssetDocumentationVersion(
            organization_id=org.id,
            documentation_id=documentation.id,
            version=1,
            status="WITHDRAWN",
            readme=withdrawn_text,
            created_by="steward-1",
        )
    )
    await session.flush()

    assert (
        await table_refusal(session, table.id, drafted_text=withdrawn_text, payload={})
        == REFUSED_WITHDRAWN
    )
    assert await table_refusal(session, table.id, drafted_text="Other words.", payload={}) is None
