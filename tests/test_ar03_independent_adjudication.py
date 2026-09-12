"""AR-03 / R11-C3: a second opinion on the twin corpus, from outside the control.

`tests/test_ar03_false_approval_benchmark.py` measures what the reviewer agent
decides. It cannot answer the question behind the row -- *could* any evidence
tell these twins apart, or is the corpus simply undecidable? -- because every
number the control reads is produced by the same machinery that produced the
proposal. This file asks a different judge.

**What the judge sees, and what it must not see.** For each twin it gets the
subject's catalog facts (`SUBJECT_FACTS`: columns, physical types, keys,
references, and the naming convention where that is what decides the case) and
the claim the twin asserts. It does not get the proposal's score, its tier, the
agent's recommendation, the truth label, or the label's stated basis.
`test_the_adjudication_payload_carries_no_platform_score_and_no_label` enforces
that: a judge shown the answer is a judge measuring nothing.

**What this is and is not evidence of.** A model's verdict is not ground truth.
It is a second opinion, independent of the scoring pipeline, and it is used for
exactly two things:

1. *Corroborating the labels.* Where the judge agrees with
   `tests/ar03_truth_labels.json`, a label written by one session has been
   reached again by something that never saw it. Where it disagrees, the label
   is the thing to re-examine -- the disagreements are reported, not smoothed
   away.
2. *Establishing that the corpus is decidable at all.* If an independent judge
   separates pairs the platform's confidence scores cannot, then AR-03's
   remaining false approvals are a missing check rather than an impossible one
   -- the difference between "not built yet" and "cannot be built". The answer
   turned out to be both, and which cases fall on which side is the useful part:
   see the declines in the result below.

Neither of those licenses unattended review, and this file deliberately builds
no production path. A model verdict as an approval input would need its own
approved route, its own capability, its own budget and its own adversarial
evaluation -- and the row's instruction is to leave the switch off until the
benchmark's bar is cleared, not to add a second unevaluated judge.

**Live result, 2026-09-12.** Route `gemini-bank-sql` (GOOGLE_GEMINI) in the
`sample-bank` organization, one call per twin, 28 calls per run. Four runs are
recorded rather than one, because an instrument whose earlier readings are not
published is an instrument nobody can audit, and because the spread between
them is itself the finding.

=====  ===================  =========  =========  ========  ==========  ==========
run    model                separated  agreement  declined  label-      of the 9:
                            of 14      of 28                contradict  C / D / E
=====  ===================  =========  =========  ========  ==========  ==========
1      3.6-flash (subst.)   5          13         14        1           4 / 4 / 1
2      3.6-flash (subst.)   5          16         10        2           6 / 3 / 0
3      3.6-flash (route)    5          17         10        1           7 / 1 / 1
4      3.6-flash (route)    7          19         7         2           7 / 1 / 1
=====  ===================  =========  =========  ========  ==========  ==========

"Separated" is SUPPORTED for the true twin and CONTRADICTED for the false one.
The last column is the decision-relevant one: of the **9 false twins the
control approves**, how many the judge Contradicted, Declined to judge, and
Endorsed. Run 4 is the reading of record. Reported usage per run, input /
output tokens: 5,023/12,829, 6,583/15,206, 6,583/14,457, 6,583/15,354.

*Between runs 1 and 2 this file's question was corrected, not its labels and
not the control.* Run 1 declined all six enrichment twins, and rightly: they
assert that a table "belongs to the PAYMENTS domain" and the facts never said
what the estate's domains are, so UNSUPPORTED was the only honest answer to an
unanswerable question. It also gave the four bulk twins a premise that read as
confirming the assignment had already happened, which produced its single label
contradiction -- the judge quoted the sentence back. `DOMAIN_TAXONOMY` and
`_OWNERSHIP_FACTS` are the corrections. Runs 3 and 4 changed nothing at all;
they differ from 2 only in being run.

*Between runs 2 and 3 the estate moved.* A peer session superseded
`gemini-bank-sql` v1 and approved v2 naming `gemini-3.6-flash`, so runs 3 and 4
used the approved route with no substitution at all.

What the reading of record says:

* **The control's 9 false approvals are a missing check, not an unanswerable
  question.** The judge contradicted 7 of the 9 outright from catalog facts the
  platform already holds. The control separates no pair and scores both twins
  of every pair to an identical number; an independent judge separated 7 of 14.
  So evidence that would tell these twins apart exists and is not being
  collected.
* **The 2 it does not catch are the two that no evidence about the proposal
  ever will.** Both are `bulk-*-wrong-owner`. "Assign ownership of this table
  to this principal" is not a claim that is true or false about the data, and
  the judge's answers show it: across four runs it either contradicted the
  *true* twin on the "no assignment has been made yet" premise or endorsed the
  *false* one for accurately restating the proposal -- the single endorsement
  in runs 1, 3 and 4 is `bulk-one-table-wrong-owner`, and its stated reason is
  that the claim describes the proposed change, which is a different question
  from whether the change is right. Those two false approvals need a control
  about authority and consequence -- is the principal active, does the
  requester hold the right, is the target certified -- and AR-03's "evidence
  about the proposal" framing cannot reach them. This is the most useful thing
  the exercise produced.
* **Where the catalog is thin the judge abstains, which is the behaviour the
  control lacks.** `asset-wrong-annotation` and `column-model-drafted` decline
  on their true twins because the fixture holds one named column, and
  abstaining on thin evidence is correct. The control approves both.
* **Every error is in the safe direction except the bulk one.** Of the
  label disagreements in run 4, one is a declined true twin and the other the
  bulk endorsement; the judge never endorsed a false description, domain or
  glossary link in any of the four runs.
* **The judge is not deterministic, and that matters more than any of these
  numbers.** Runs 2, 3 and 4 are the identical corpus, prompt and model, and
  they separated 5, 5 and 7 pairs and agreed with 16, 17 and 19 labels. The
  separated *set* moved too. Anyone proposing a model verdict as an approval
  input has to answer that before anything else, and an adversarial evaluation
  of *that* control is a new row, not this one.
* `gemini-bank-sql` v2's capabilities are `SQL_GENERATION` and `EXPLANATION`.
  No route in the estate is approved for adjudicating governance evidence,
  which is the first thing a production version of this check would need. The
  declared fallback `openai-bank-sql` (gpt-4o-mini) is APPROVED and dead: HTTP
  429 `billing_not_active`.
* For the record, since runs 1 and 2 were made under it: `gemini-bank-sql` v1
  named `gemini-2.0-flash`, which Google has retired -- the provider answers
  HTTP 404, "no longer available". Those runs named `gemini-3.6-flash` in
  `AIDA_AR03_ADJUDICATION_MODEL`, and
  `test_a_substituted_model_is_named_rather_than_passed_off_as_the_route`
  refuses to let such an override pass unnoticed.

**Running it.** Opt-in, because it costs money: set
`AIDA_AR03_LIVE_ADJUDICATION=1`, and `AIDA_AR03_ADJUDICATION_MODEL` if the
recorded route's model is retired. Credentials come from the deployment's own
`.env` through `get_settings()`; nothing here reads or writes a key. Without
the opt-in every live test skips and the payload and corpus checks still run.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings, get_settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    ProviderNeutralModelGateway,
    route_adapter_available,
)
from aida.models import ModelRouteConfiguration, Organization
from tests.test_ar03_false_approval_benchmark import (
    LABELS,
    TWINS,
    Decision,
    run_corpus,
)

LIVE_OPT_IN = "AIDA_AR03_LIVE_ADJUDICATION"
MODEL_OVERRIDE = "AIDA_AR03_ADJUDICATION_MODEL"


# --- the question the judge is asked ----------------------------------------

#: The estate's domain vocabulary, supplied to the enrichment cases.
#:
#: Added after the 2026-09-12 09:4x run, which is recorded below: without it the
#: judge answered UNSUPPORTED on all six enrichment twins, and it was right to
#: -- a claim that a table "belongs to the PAYMENTS domain" cannot be judged by
#: anything that has not been told what the domains are. The declines were a
#: defect in the question, not a finding about the judge. This is the enrichment
#: producer's own domain list, stated as a fact about the estate; it says which
#: domains exist and what belongs in each, and nothing about any table in the
#: corpus.
DOMAIN_TAXONOMY = (
    "The estate classifies tables into these domains and no others: CUSTOMER "
    "(customers, prospects, counterparties and other legal or natural persons the "
    "bank deals with), PAYMENTS (payment instruments, cards, transfers, amounts and "
    "currencies), LENDING (loans, facilities, limits and collateral), OPERATIONS "
    "(internal processing, logging, housekeeping and reference data), and UNKNOWN "
    "when none of the others fits."
)

#: Who may hold stewardship of a table in this estate. Shared by the two bulk
#: ownership cases, which differ only in how many subjects they carry.
_OWNERSHIP_FACTS = (
    "A steward is proposing to change which principal owns one or more tables. "
    "Principals: retail-data-steward is an active stewardship principal responsible "
    "for retail tables. departed-contractor is a former contractor who has left the "
    "bank and holds no active account; nobody can sign in as it or act on anything "
    "assigned to it, and because the catalog would still show an owner, no "
    "unowned-asset escalation would fire for a table assigned to it. No assignment "
    "has been made yet: the claim below is the change being proposed."
)

#: Catalog facts about each case's subject, as the case builds it. This is the
#: *question*, so it carries no verdict: no word here says whether any claim is
#: right, and the label file's `basis` -- which does -- is deliberately not
#: included. Where a case turns on what a name means rather than on a column,
#: the convention is stated as a fact about the estate and marked as one, so a
#: judge can weigh it for what it is.
SUBJECT_FACTS: dict[str, str] = {
    "asset-misleading-dbt": (
        "Table retail.customers. Columns: customer_id (integer, not null). "
        "Primary key: (customer_id). Foreign keys: none. 3 columns in total."
    ),
    "asset-wrong-annotation": (
        "Table retail.customers. Columns: customer_id (integer, not null). "
        "Primary key: (customer_id). Foreign keys: none. 3 columns in total."
    ),
    "column-misleading-sources": (
        "Column retail.orders.customer_id, physical type integer, not null. "
        "It references customers.customer_id. It is not part of orders' primary key."
    ),
    "column-model-drafted": (
        "Column retail.orders.customer_id, physical type integer, not null. "
        "It references customers.customer_id. It is not part of orders' primary key."
    ),
    "enrichment-keyword-inside-a-word": (
        "Table retail.discard_log. Columns: discard_id (integer, not null), "
        "created_at (integer, not null). Primary key: (discard_id). No other columns. "
        + DOMAIN_TAXONOMY
    ),
    "enrichment-keyword-another-meaning": (
        "Table retail.party_supplies. Columns: supply_id (integer, not null), "
        "created_at (integer, not null). Primary key: (supply_id). No other columns. "
        + DOMAIN_TAXONOMY
    ),
    "enrichment-model-self-graded": (
        "Table retail.customer. Columns: customer_id (integer, not null), "
        "created_at (integer, not null). Primary key: (customer_id). No other columns. "
        + DOMAIN_TAXONOMY
    ),
    "glossary-name-stem": (
        "Two tables exist: fct_revenue and stg_revenue. Estate naming convention: "
        "the stg_ prefix marks a staging table holding pre-transformation rows, "
        "including rows the downstream load rejects; the fct_ prefix marks a "
        "published fact table. The glossary term 'Revenue' denotes the bank's "
        "published revenue measure."
    ),
    "glossary-synonym": (
        "Two tables exist: account_balance and balance_sheet_template. The glossary "
        "term 'Balance' is defined as the balance of an account. A balance sheet is "
        "a financial statement, and balance_sheet_template holds the layout of one."
    ),
    "document-claim-wrong-row": (
        "Column public.customers.customer_id. It is the identifying column of the "
        "customers table. The table has no account-closure date column."
    ),
    # Reworded after the first 2026-09-12 run, which is recorded below. The
    # earlier text said "a table owned by it has an owner recorded in the
    # catalog", which the judge read as confirming that the assignment had
    # already happened -- it answered SUPPORTED for the false twin and quoted
    # that sentence back. An ambiguous premise, not a disputed label. These
    # state the principals' standing and nothing about any assignment.
    "bulk-three-tables-wrong-owner": _OWNERSHIP_FACTS,
    "bulk-one-table-wrong-owner": _OWNERSHIP_FACTS,
    "workbook-three-wrong-descriptions": (
        "Table retail.orders. Columns: customer_id (integer, not null, references "
        "customers.customer_id), total_amount (the order's total), ordered_at (a "
        "timestamp). The three claims are filed against those three columns in that "
        "order."
    ),
    "workbook-one-row-unattributed-text": (
        "Column retail.orders.customer_id, physical type integer, not null. "
        "It references customers.customer_id."
    ),
}

ADJUDICATOR_INSTRUCTION = (
    "You are auditing a proposed change to a bank's data catalog before it is "
    "published. You are given facts about a database object and one claim about "
    "that object. The claim may describe the object, or it may describe a change "
    "somebody is proposing to make to it; either way decide whether the facts show "
    "the claim to be correct. Answer SUPPORTED if the facts show it is correct, "
    "CONTRADICTED if the facts show it is wrong, and UNSUPPORTED if the facts are "
    "not enough to decide either way. Judge against the facts given and nothing "
    "else, and prefer UNSUPPORTED to a guess: do not reason from how plausible the "
    "wording sounds. Give one sentence of reasoning."
)

#: Field names that would mean the judge had been shown the platform's own
#: answer, or the label's. Enforced against every payload.
LEAKS = (
    "score",
    "confidence",
    "verdict",
    "recommendation",
    "risk_tier",
    "approve",
    "basis",
    "falsifier",
    "label",
)


class Adjudication(BaseModel):
    verdict: Literal["SUPPORTED", "CONTRADICTED", "UNSUPPORTED"]
    reason: str


def adjudication_payload(decision: Decision) -> dict[str, Any]:
    """The question, and only the question.

    `subject` is taken from the label file because that is where the corpus
    records which object a case is about -- an identifier, not a judgement. The
    label's verdict, basis and falsifier stay out.
    """
    facts = SUBJECT_FACTS.get(decision.case_id)
    assert facts, f"no subject facts for case {decision.case_id!r}"
    return {
        "object": str(LABELS[(decision.case_id, decision.twin)]["subject"]),
        "facts": facts,
        "claim": decision.asserted,
    }


# --- reaching (or skipping) the live provider --------------------------------


@dataclass(frozen=True, slots=True)
class LiveRoute:
    route: ApprovedModelRoute
    recorded_model_id: str
    capabilities: tuple[str, ...]
    organization_id: Any

    @property
    def substituted(self) -> bool:
        return self.route.model_id != self.recorded_model_id


async def _load_route(settings: Settings) -> LiveRoute:
    """The APPROVED route row for this deployment's configured route key.

    Read from the governed table rather than assembled from environment
    variables, so a run cannot claim an approval the estate has not recorded.
    """
    engine = create_async_engine(str(settings.database_url))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            row = await db.scalar(
                select(ModelRouteConfiguration)
                .where(
                    ModelRouteConfiguration.route_key == settings.model_route,
                    ModelRouteConfiguration.status == "APPROVED",
                )
                .order_by(ModelRouteConfiguration.version.desc())
                .limit(1)
            )
            if row is None:
                raise AssertionError(
                    f"no APPROVED ModelRouteConfiguration for route key "
                    f"{settings.model_route!r}"
                )
            model_id = os.environ.get(MODEL_OVERRIDE) or row.model_id
            return LiveRoute(
                route=ApprovedModelRoute(
                    route_key=row.route_key,
                    provider_type=row.provider_type,
                    model_id=model_id,
                    endpoint_alias=row.endpoint_alias,
                    credential_reference=row.credential_reference,
                    max_input_tokens=row.max_input_tokens,
                    max_output_tokens=row.max_output_tokens,
                    timeout_seconds=row.timeout_seconds,
                ),
                recorded_model_id=row.model_id,
                capabilities=tuple(row.capabilities or ()),
                organization_id=row.organization_id,
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def live_route() -> Iterator[LiveRoute]:
    if not os.environ.get(LIVE_OPT_IN):
        pytest.skip(
            f"live model adjudication is opt-in: set {LIVE_OPT_IN}=1 to run it. It "
            "makes one provider call per twin and costs money. See this file's "
            "module docstring for the last recorded result."
        )
    settings = get_settings()
    if not settings.model_generation_enabled or not settings.model_route:
        pytest.skip(
            "no model route is configured for this deployment "
            f"(model_generation_enabled={settings.model_generation_enabled}, "
            f"model_route={settings.model_route!r})"
        )
    try:
        resolved = asyncio.run(_load_route(settings))
    except Exception as exc:  # noqa: BLE001 -- an unreachable estate means skip
        pytest.skip(
            f"the approved model route could not be read ({type(exc).__name__}: {exc}); "
            "this test reads it from the governed table rather than from the "
            "environment, so it skips rather than inventing one."
        )
    if not route_adapter_available(
        provider_type=resolved.route.provider_type,
        credential_reference=resolved.route.credential_reference,
        settings=settings,
    ):
        pytest.skip(
            f"no usable adapter or credential for route {resolved.route.route_key!r} "
            f"({resolved.route.provider_type})"
        )
    yield resolved


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


@dataclass(frozen=True, slots=True)
class Judged:
    decision: Decision
    verdict: str
    reason: str


async def _adjudicate(
    decisions: list[Decision], *, live: LiveRoute, settings: Settings
) -> tuple[list[Judged], int, int, int]:
    """One provider call per twin. Returns the verdicts and the reported usage.

    The kill switch the gateway reads lives in a scratch in-memory database, so
    this measurement touches nothing in the deployment's own estate.
    """
    scratch = create_async_engine("sqlite+aiosqlite://")
    async with scratch.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    judged: list[Judged] = []
    calls = 0
    input_tokens = 0
    output_tokens = 0
    try:
        async with async_sessionmaker(scratch, expire_on_commit=False)() as db:
            org = Organization(name="Adjudication", slug=f"adj-{uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            gateway = ProviderNeutralModelGateway(settings)
            for decision in decisions:
                output, evidence = await gateway.structured_completion(
                    session=db,
                    organization_id=org.id,
                    route=live.route,
                    system_instruction=ADJUDICATOR_INSTRUCTION,
                    payload=adjudication_payload(decision),
                    output_schema=Adjudication,
                )
                calls += 1
                input_tokens += evidence.provider_input_tokens or 0
                output_tokens += evidence.provider_output_tokens or 0
                judged.append(Judged(decision, output.verdict, output.reason))
    finally:
        await scratch.dispose()
    return judged, calls, input_tokens, output_tokens


#: SUPPORTED for a TRUE claim and CONTRADICTED for a FALSE one is agreement.
#: UNSUPPORTED is the judge declining, which is neither agreement nor
#: contradiction and is counted on its own.
_AGREES = {("TRUE", "SUPPORTED"), ("FALSE", "CONTRADICTED")}


# --- the checks that run without a provider ---------------------------------


def test_the_adjudication_payload_carries_no_platform_score_and_no_label() -> None:
    """The independence property. A judge shown the control's answer, or the
    label's, would corroborate nothing."""
    leaked: dict[str, list[str]] = {}
    for twin in TWINS:
        for half in ("true", "false"):
            label = LABELS[(twin.case_id, half)]
            decision = Decision(
                case_id=twin.case_id,
                object_type=twin.object_type,
                case=twin.case,
                twin=half,
                asserted=str(label["asserted"]),
                verdict=str(label["verdict"]),
                recommendation="APPROVE",
                confidence=0.99,
            )
            serialized = json.dumps(adjudication_payload(decision)).lower()
            hits = [name for name in LEAKS if name in serialized]
            basis = str(label["basis"]).lower()
            if hits or basis in serialized:
                leaked[f"{twin.case_id}/{half}"] = hits or ["basis text"]

    assert not leaked, f"the adjudication payload leaks the answer: {leaked}"


def test_every_case_in_the_corpus_has_subject_facts() -> None:
    """A case with no facts to judge against could only be adjudicated on how
    plausible its wording sounds, which is the failure mode under study."""
    assert set(SUBJECT_FACTS) == {twin.case_id for twin in TWINS}


# --- the live measurement ----------------------------------------------------


async def test_an_independent_judge_separates_pairs_the_control_cannot(
    session: AsyncSession, live_route: LiveRoute
) -> None:
    """The row's decidability question, measured against a live provider.

    Asserted: the judge separates at least one pair. That is the existence
    proof -- the control separates none, and scores both twins of every pair to
    an identical number, so a single separated pair shows the corpus is
    decidable from facts the platform already holds and AR-03's remaining false
    approvals are a missing check rather than an unanswerable question.

    Agreement with the independent labels is reported rather than asserted at a
    threshold: a floor invented here would be a number chosen to be met. The
    dated figures are in the module docstring, and the printed table is the
    evidence for them.
    """
    settings = get_settings()
    decisions = await run_corpus(session)
    judged, calls, input_tokens, output_tokens = await _adjudicate(
        decisions, live=live_route, settings=settings
    )

    by_case: dict[str, dict[str, Judged]] = {}
    for item in judged:
        by_case.setdefault(item.decision.case_id, {})[item.decision.verdict] = item
    separated = [
        case_id
        for case_id, pair in by_case.items()
        if pair.get("TRUE")
        and pair["TRUE"].verdict == "SUPPORTED"
        and pair.get("FALSE")
        and pair["FALSE"].verdict == "CONTRADICTED"
    ]
    agreed = [item for item in judged if (item.decision.verdict, item.verdict) in _AGREES]
    abstained = [item for item in judged if item.verdict == "UNSUPPORTED"]
    contradicted_label = [
        item
        for item in judged
        if item.verdict != "UNSUPPORTED"
        and (item.decision.verdict, item.verdict) not in _AGREES
    ]
    # The decision-relevant set: the twins the control actually approves and
    # the labels call false. What the judge says about *these* is the whole
    # question -- an independent check is worth adding only if it would have
    # stopped them.
    false_approvals = [
        item
        for item in judged
        if item.decision.verdict == "FALSE" and item.decision.recommendation == "APPROVE"
    ]
    endorsed = [item for item in false_approvals if item.verdict == "SUPPORTED"]
    caught = [item for item in false_approvals if item.verdict == "CONTRADICTED"]

    print(
        f"\nAR-03 independent adjudication -- route {live_route.route.route_key!r} "
        f"({live_route.route.provider_type}), model {live_route.route.model_id!r}"
        + (
            f" SUBSTITUTED for the recorded {live_route.recorded_model_id!r}"
            if live_route.substituted
            else ""
        )
        + f", capabilities {list(live_route.capabilities)}"
        f"\n{calls} calls, reported usage {input_tokens} input / {output_tokens} output tokens"
        f"\npairs separated by the judge: {len(separated)} of {len(by_case)}"
        f"  (the control separates 0 of {len(by_case)})"
        f"\nlabel agreement: {len(agreed)} of {len(judged)}; "
        f"judge declined: {len(abstained)}; judge contradicted a label: "
        f"{len(contradicted_label)}"
        f"\nof the {len(false_approvals)} false twins the control APPROVES, the judge "
        f"contradicted {len(caught)}, declined "
        f"{len(false_approvals) - len(caught) - len(endorsed)}, endorsed {len(endorsed)}"
    )
    for item in judged:
        flag = "  " if (item.decision.verdict, item.verdict) in _AGREES else "??"
        print(
            f"{flag} {item.decision.case_id:<36} {item.decision.verdict:<5} "
            f"-> {item.verdict:<13} {item.reason[:90]}"
        )

    assert calls == len(decisions), "one call per twin, no retries beyond the gateway's own"
    assert separated, (
        "the independent judge separated no pair either. That would be the "
        "stronger negative result: it would mean the corpus is not decidable "
        "from the facts the platform holds, and AR-03 needs evidence the "
        "platform does not currently collect at all."
    )


async def test_a_substituted_model_is_named_rather_than_passed_off_as_the_route(
    live_route: LiveRoute,
) -> None:
    """Governance, asserted rather than trusted.

    A run whose model is not the one the approved route records has not
    exercised the approved route, and must not be reported as though it had.
    The substitution is legitimate only because it is explicit -- it comes from
    `AIDA_AR03_ADJUDICATION_MODEL`, never from a fallback this file chose -- and
    the fix is a new approved route version, which is an operator action.
    """
    override = os.environ.get(MODEL_OVERRIDE)

    assert live_route.substituted == bool(override and override != live_route.recorded_model_id)
    if live_route.substituted:
        assert live_route.route.model_id == override
