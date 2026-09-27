"""The walkthrough's maker-checker loop, run in-process against the real application.

`Docs/walkthrough/demo-script.html#loop` gives a presenter a PowerShell script (three
identities, seven calls) and tells them what to expect. The page says of itself that it was
*not run against the live stack* and to dry-run it first. This is that dry-run, made
repeatable and free of any running stack: the same FastAPI application the API container
serves, the development identity headers the script sends, and an in-memory SQLite database
in place of PostgreSQL. Nothing here touches the dev database or the network.

What is proven, and what the page's own text is held to:

* The seven calls are sent **in the page's notation** (`/v1/organizations/$org/...`,
  `$($review.id)`, principal variable `$dana`) and `test_the_calls_sent_are_the_calls_on_the_page`
  compares them, method by method, with the calls parsed out of the page's `<pre>`: route
  template, principal variable, body field names, and every body value the page writes as a
  literal. Edit the page's script without editing this test and that test fails.
* The identities are held to the page's `As "..." "..."` lines the same way.
* The statuses the page says to expect (`dana: HTTP 409`, `omar: HTTP 403`) are read from the
  page and compared with what the API answers, not restated here.
* The 'Refused here' cells of `roles-and-users.html#users` that fall on the loop's routes are
  asserted as written: riya cannot propose a term or decide a relationship, ravi and ana cannot
  decide a review, ana and dana cannot read the ledger, ana is refused another tenant's path,
  and alex cannot approve his own proposal holding fifteen of the sixteen roles (all but the
  push-only MetadataIngestor). So are the Act 4 and Act 5 refusals of `demo-script.html`,
  including the sentence both pages quote verbatim.

**One claim on the page was not true, and this test is what found it.** The page said step 5
"lists the events with three different principals". The ledger for this loop holds four events
by two principals, `dana.steward` (category, term, submit) and `riya.reviewer` (the decision).
`omar.auditor` never appears: a refused request is not a mutation and writes no audit row
(INV-7 is about mutations), so neither dana's 409 nor omar's 403 is recorded, and omar's reading
the ledger is not recorded either. The page now says two, and says why;
`test_the_ledger_lists_as_many_principals_as_the_page_says` holds it to that, and would fail if
either the page or the ledger drifted (for instance if refusals started being audited). What
*is* recorded is pinned by `test_the_ledger_holds_the_loop_and_only_the_loop`
and `test_the_ledger_names_maker_and_checker_against_the_same_review`, which is what Act 3
step 4 of the page actually promises.

What this cannot show: PowerShell's own behaviour (`ConvertTo-Json`, `try/catch`, how
`Invoke-RestMethod` prints the ledger page on a console), the live stack's data, or any
middleware only the container adds. The routes, bodies, headers, status codes and rows are the
application's own.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.main import app
from aida.models import Organization
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SCRIPT = REPO_ROOT / "Docs" / "walkthrough" / "demo-script.html"
ROLES_PAGE = REPO_ROOT / "Docs" / "walkthrough" / "roles-and-users.html"

#: The organization the page hard-codes (`$org`), so the in-memory copy is the one a presenter
#: would use.
ORG = UUID("9b90b35f-dcf5-49d3-8f0e-2f269987ae87")

#: The page builds `$stamp` from the clock (`yyyyMMddHHmm`); a fixed one keeps the run repeatable.
STAMP = "202609201530"

#: Statuses a create/submit call must answer for the loop to be running at all.
_PROPOSED = {"category": 201, "term": 201, "review": 202}


@dataclass(frozen=True)
class Identity:
    """One `$dana = As "dana.steward" "DataSteward,..."` line of the page."""

    var: str
    principal: str
    roles: str

    @property
    def headers(self) -> dict[str, str]:
        # Exactly the three headers `As` sends: no X-Principal-Type, no X-Business-Purpose.
        return {
            "X-Principal-Id": self.principal,
            "X-Roles": self.roles,
            "X-Organization-Id": str(ORG),
        }


DANA = Identity("dana", "dana.steward", "DataSteward,MetadataReviewer,Analyst,Viewer")
RIYA = Identity("riya", "riya.reviewer", "Reviewer,Viewer")
OMAR = Identity("omar", "omar.auditor", "Auditor,Viewer")
IDENTITIES = (DANA, RIYA, OMAR)

# The other users of Docs/walkthrough/roles-and-users.html#users, with the roles the development
# roster (scripts/demo-users.ps1) sends for them.
VIC = Identity("vic", "vic.viewer", "Viewer")
ANA = Identity("ana", "ana.analyst", "Analyst,Viewer")
RAVI = Identity("ravi", "ravi.dataadmin", "DataAdmin,Viewer")
ALEX = Identity(
    "alex",
    "alex.operator",
    "PlatformAdmin,OrganizationAdmin,MetadataAdmin,DataAdmin,SemanticAdmin,DataSteward,Reviewer,"
    "MetadataReviewer,Auditor,Operations,Analyst,Viewer,ToolDeveloper,ToolConsumer,AgentDeveloper",
)

#: An id nothing has: a role refusal must come before the target is looked up.
NOWHERE = UUID("00000000-0000-4000-8000-000000000001")


# ---------------------------------------------------------------------------
# What the page says
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageCall:
    method: str
    template: str
    who: str
    keys: tuple[str, ...]
    literals: dict[str, Any]


@dataclass(frozen=True)
class LoopPage:
    org: str
    identities: dict[str, tuple[str, str]]
    calls: list[PageCall]
    expected_dana_status: int
    expected_omar_status: int
    expected_principals: int


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _page_text(path: Path) -> str:
    return html.unescape(path.read_text(encoding="utf-8"))


def _parse_loop_page() -> LoopPage:
    section = re.search(r'<h2 id="loop">(.*?)(?=<h2 )', _page_text(DEMO_SCRIPT), re.S)
    assert section, "demo-script.html no longer has a section with id=loop"
    script = re.search(r"<pre[^>]*>(.*?)</pre>", section.group(1), re.S)
    assert script, "the loop section no longer has a <pre> script"
    text = script.group(1)

    org = re.search(r'\$org\s*=\s*"([^"]+)"', text)
    assert org, "the script no longer sets $org"
    identities = {
        var: (principal, roles)
        for var, principal, roles in re.findall(
            r'^\$(\w+)\s*=\s*As\s+"([^"]+)"\s+"([^"]+)"', text, re.M
        )
    }
    calls: list[PageCall] = []
    for method, template, who, body in re.findall(
        r'Call\s+(GET|POST)\s+"([^"]+)"\s+\$(\w+)(?:\s+@\{(.*?)\})?', text
    ):
        literals: dict[str, Any] = {
            key: value for key, value in re.findall(r'(\w+)\s*=\s*"([^"$]*)"', body)
        }
        literals.update({key: None for key in re.findall(r"(\w+)\s*=\s*\$null", body)})
        literals.update({key: [] for key in re.findall(r"(\w+)\s*=\s*@\(\)", body)})
        calls.append(
            PageCall(
                method=method,
                template=template,
                who=who,
                keys=tuple(sorted(re.findall(r"(\w+)\s*=", body))),
                literals=literals,
            )
        )
    assert calls, "the script's Call lines were not recognised; the page changed shape"

    expected = re.search(r"<p>Expected:(.*?)</p>", section.group(1), re.S)
    assert expected, "the loop section no longer has an Expected line"
    prose = re.sub(r"<[^>]+>", "", expected.group(1))
    dana = re.search(r"dana: HTTP (\d{3})", prose)
    omar = re.search(r"omar: HTTP (\d{3})", prose)
    principals = re.search(r"by (\w+) principals", prose)
    assert dana and omar and principals, f"the Expected line changed shape: {prose!r}"
    return LoopPage(
        org=org.group(1),
        identities={var: pair for var, pair in identities.items()},
        calls=calls,
        expected_dana_status=int(dana.group(1)),
        expected_omar_status=int(omar.group(1)),
        expected_principals=_NUMBER_WORDS[principals.group(1)],
    )


@pytest.fixture(scope="module")
def page() -> LoopPage:
    return _parse_loop_page()


# ---------------------------------------------------------------------------
# The application, in-process
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    """The real `app` over ASGI, one in-memory database, and the organization the page names."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as seed:
        seed.add(Organization(id=ORG, name="Northwind", slug="sample-bank"))
        await seed.commit()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    # `app` is process-wide: put the overrides back exactly as they were found.
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _session
    # The development identity provider is what the script depends on, whatever the shell's
    # own AIDA_* variables say.
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, identity_provider="development"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://demo.test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    await engine.dispose()


@dataclass
class Sent:
    method: str
    template: str
    who: str
    body: dict[str, Any] | None


@dataclass
class Script:
    """The page's `Call` function: same notation in, an HTTP request out, a log of what was sent."""

    client: httpx.AsyncClient
    variables: dict[str, Any] = field(default_factory=lambda: {"org": str(ORG)})
    sent: list[Sent] = field(default_factory=list)

    def _resolve(self, template: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            if match.group(1):  # $($term.id)
                return str(self.variables[match.group(1)][match.group(2)])
            return str(self.variables[match.group(3)])  # $org

        return re.sub(r"\$\(\$(\w+)\.(\w+)\)|\$(\w+)", substitute, template)

    async def call(
        self,
        method: str,
        template: str,
        who: Identity,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self.sent.append(Sent(method, template, who.var, body))
        # The page's `Call` sets `ContentType = "application/json"` on every request, the
        # body-less submit and the GET included.
        headers = {**who.headers, "Content-Type": "application/json"}
        return await self.client.request(
            method, self._resolve(template), headers=headers, json=body
        )


def _proposed(name: str, response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == _PROPOSED[name], (
        f"step 1 ({name}) answered {response.status_code}: {response.text}"
    )
    body: dict[str, Any] = response.json()
    return body


async def propose(
    script: Script, maker: Identity = DANA
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Step 1 of the page: the maker proposes a category, a term and a review."""
    category = _proposed(
        "category",
        await script.call(
            "POST",
            "/v1/organizations/$org/glossary-categories",
            maker,
            {
                "category_key": f"demo_{STAMP}",
                "display_name": f"Demo {STAMP}",
                "description": "Walkthrough demo",
                "parent_id": None,
            },
        ),
    )
    script.variables["cat"] = category
    term = _proposed(
        "term",
        await script.call(
            "POST",
            "/v1/organizations/$org/glossary-terms",
            maker,
            {
                "term_key": f"demo_term_{STAMP}",
                "display_name": f"Demo term {STAMP}",
                "definition": "Created live to show maker and checker.",
                "category_id": category["id"],
                "synonyms": [],
                "owner_principal": "dana.steward",
            },
        ),
    )
    script.variables["term"] = term
    review = _proposed(
        "review",
        await script.call("POST", "/v1/glossary-term-versions/$($term.id)/submit", maker),
    )
    script.variables["review"] = review
    return category, term, review


_DECISION = "/v1/governance/reviews/$($review.id)/decision"


async def _pending_reviews(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get(
        "/v1/governance/reviews", params={"status": "PENDING"}, headers=RIYA.headers
    )


@dataclass
class DemoRun:
    script: Script
    category: dict[str, Any]
    term: dict[str, Any]
    review: dict[str, Any]
    dana_attempt: httpx.Response
    omar_attempt: httpx.Response
    pending_after_refusals: httpx.Response
    terms_after_refusals: httpx.Response
    riya_decision: httpx.Response
    ledger: httpx.Response

    @property
    def client(self) -> httpx.AsyncClient:
        return self.script.client

    @property
    def ledger_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.ledger.json()["items"]
        return items


@pytest_asyncio.fixture
async def demo(http: httpx.AsyncClient) -> DemoRun:
    """The page's five steps, in the page's order."""
    script = Script(http)
    category, term, review = await propose(script)

    approve_own = {"decision": "APPROVE", "reason": "my own change"}
    dana_attempt = await script.call("POST", _DECISION, DANA, approve_own)  # 2. expect 409
    omar_attempt = await script.call(  # 3. expect 403
        "POST", _DECISION, OMAR, {"decision": "APPROVE", "reason": "auditor"}
    )
    # Read-only probes between steps 3 and 4 (not part of the page's script, so they are sent
    # around `Script.call`): the refused attempts must have decided nothing.
    pending = await _pending_reviews(http)
    terms = await http.get(f"/v1/organizations/{ORG}/glossary-terms", headers=DANA.headers)

    riya_decision = await script.call(  # 4. the checker approves
        "POST", _DECISION, RIYA, {"decision": "APPROVE", "reason": "Independent check"}
    )
    ledger = await script.call(  # 5. the auditor reads what happened
        "GET", "/v1/organizations/$org/audit-events?limit=8", OMAR
    )
    return DemoRun(
        script=script,
        category=category,
        term=term,
        review=review,
        dana_attempt=dana_attempt,
        omar_attempt=omar_attempt,
        pending_after_refusals=pending,
        terms_after_refusals=terms,
        riya_decision=riya_decision,
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# The page and this test say the same thing
# ---------------------------------------------------------------------------


def test_the_page_holds_the_identities_and_the_organization_the_test_uses(
    page: LoopPage,
) -> None:
    assert page.org == str(ORG)
    assert page.identities == {
        identity.var: (identity.principal, identity.roles) for identity in IDENTITIES
    }


async def test_the_calls_sent_are_the_calls_on_the_page(demo: DemoRun, page: LoopPage) -> None:
    """Route template, principal, body fields and every literal body value, call by call."""
    sent = demo.script.sent
    assert [(call.method, call.template, call.who) for call in sent] == [
        (call.method, call.template, call.who) for call in page.calls
    ]
    for index, (mine, theirs) in enumerate(zip(sent, page.calls, strict=True), start=1):
        assert tuple(sorted(mine.body or {})) == theirs.keys, f"call {index}: body fields differ"
        for key, value in theirs.literals.items():
            assert (mine.body or {})[key] == value, f"call {index}: the page sends {key}={value!r}"


# ---------------------------------------------------------------------------
# 1. The maker proposes
# ---------------------------------------------------------------------------


async def test_step_1_the_category_is_created_and_owned_by_the_maker(demo: DemoRun) -> None:
    category = demo.category

    assert category["category_key"] == f"demo_{STAMP}"
    assert category["display_name"] == f"Demo {STAMP}"
    assert category["description"] == "Walkthrough demo"
    assert category["parent_id"] is None
    assert category["status"] == "ACTIVE"
    assert category["created_by"] == "dana.steward"
    assert category["organization_id"] == str(ORG)


async def test_step_1_the_term_is_a_draft_whose_id_is_the_version_the_review_will_target(
    demo: DemoRun,
) -> None:
    """The script submits `$term.id`. That is the term *version*'s id, not `term_id`; the two
    differ, and the submit route is keyed by the version."""
    term = demo.term

    assert term["term_key"] == f"demo_term_{STAMP}"
    assert term["category_id"] == demo.category["id"]
    assert term["status"] == "DRAFT"
    assert term["version"] == 1
    assert term["lifecycle_status"] == "ACTIVE"
    assert term["synonyms"] == []
    assert term["owner_principal"] == "dana.steward"
    assert term["created_by"] == "dana.steward"
    assert term["approved_by"] is None and term["approved_at"] is None
    assert term["id"] != term["term_id"]


async def test_step_1_the_submit_opens_one_pending_review_of_that_version(demo: DemoRun) -> None:
    review = demo.review

    assert review["object_type"] == "GLOSSARY_TERM_VERSION"
    assert review["object_id"] == demo.term["id"]
    assert review["requested_action"] == "PUBLISH"
    assert review["status"] == "PENDING"
    assert review["requested_by"] == "dana.steward"
    assert review["decided_by"] is None and review["decided_at"] is None
    assert review["organization_id"] == str(ORG)


# ---------------------------------------------------------------------------
# 2 and 3. Neither the maker nor the auditor may decide
# ---------------------------------------------------------------------------


async def test_step_2_the_maker_cannot_approve_her_own_change(
    demo: DemoRun, page: LoopPage
) -> None:
    assert page.expected_dana_status == 409, "the page no longer tells the presenter to expect 409"

    assert demo.dana_attempt.status_code == 409
    assert demo.dana_attempt.json() == {"detail": "maker-checker separation is required"}


async def test_step_3_the_auditor_cannot_decide_at_all(demo: DemoRun, page: LoopPage) -> None:
    assert page.expected_omar_status == 403, "the page no longer tells the presenter to expect 403"

    assert demo.omar_attempt.status_code == 403
    assert demo.omar_attempt.json() == {
        "detail": (
            "one of these roles (directly or via an active delegation) is required: "
            "DataSteward, PlatformAdmin, Reviewer"
        )
    }


async def test_the_two_refusals_decided_nothing(demo: DemoRun) -> None:
    """The 409 and the 403 must leave the review pending and the term awaiting review, or the
    approval in step 4 would be approving something already touched."""
    pending = demo.pending_after_refusals.json()
    assert demo.pending_after_refusals.status_code == 200
    assert [item["id"] for item in pending["items"]] == [demo.review["id"]]
    assert pending["items"][0]["decided_by"] is None
    assert pending["items"][0]["status"] == "PENDING"

    terms = demo.terms_after_refusals.json()
    assert [(item["id"], item["status"]) for item in terms["items"]] == [
        (demo.term["id"], "REVIEW_REQUIRED")
    ]


# ---------------------------------------------------------------------------
# 4. The checker approves
# ---------------------------------------------------------------------------


async def test_step_4_the_checker_approves_and_the_review_records_who(demo: DemoRun) -> None:
    decision = demo.riya_decision

    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["id"] == demo.review["id"]
    assert body["status"] == "APPROVED"
    assert body["requested_by"] == "dana.steward"
    assert body["decided_by"] == "riya.reviewer"
    assert body["decision_reason"] == "Independent check"
    assert body["decided_at"] is not None


async def test_step_4_the_term_is_approved_and_names_the_checker(demo: DemoRun) -> None:
    listed = await demo.client.get(f"/v1/organizations/{ORG}/glossary-terms", headers=DANA.headers)

    assert listed.status_code == 200
    [term] = listed.json()["items"]
    assert term["id"] == demo.term["id"]
    assert term["status"] == "APPROVED"
    assert term["approved_by"] == "riya.reviewer"
    assert term["approved_at"] is not None
    assert term["created_by"] == "dana.steward"
    assert term["lifecycle_status"] == "ACTIVE"


async def test_the_loop_writes_one_category_one_term_and_one_review_all_demo_named(
    demo: DemoRun,
) -> None:
    """The page's warning box: 'one glossary category, one glossary term and one review, all
    named demo_*'."""
    categories = await demo.client.get(
        f"/v1/organizations/{ORG}/glossary-categories", headers=DANA.headers
    )
    terms = await demo.client.get(f"/v1/organizations/{ORG}/glossary-terms", headers=DANA.headers)
    reviews = {
        review_status: (
            await demo.client.get(
                "/v1/governance/reviews", params={"status": review_status}, headers=RIYA.headers
            )
        ).json()
        for review_status in ("PENDING", "APPROVED", "REJECTED")
    }

    assert [item["category_key"] for item in categories.json()["items"]] == [f"demo_{STAMP}"]
    assert [item["term_key"] for item in terms.json()["items"]] == [f"demo_term_{STAMP}"]
    assert [reviews[s]["total"] for s in ("PENDING", "APPROVED", "REJECTED")] == [0, 1, 0]


# ---------------------------------------------------------------------------
# 5. The auditor reads what happened
# ---------------------------------------------------------------------------

#: Newest first, which is the order the route returns.
_LOOP_EVENTS = [
    ("riya.reviewer", "governance.review.decide", "governance_review"),
    ("dana.steward", "glossary.term.submit", "governance_review"),
    ("dana.steward", "glossary.term.create", "glossary_term_version"),
    ("dana.steward", "glossary.category.create", "glossary_category"),
]


async def test_step_5_the_auditor_can_read_the_ledger_and_the_page_shape_is_the_one_returned(
    demo: DemoRun,
) -> None:
    assert demo.ledger.status_code == 200
    page_body = demo.ledger.json()
    assert (page_body["limit"], page_body["offset"]) == (8, 0)
    assert {
        "id",
        "principal_id",
        "principal_type",
        "action",
        "resource_type",
        "resource_id",
        "outcome",
        "correlation_id",
        "details",
        "occurred_at",
    } <= set(demo.ledger_items[0])


async def test_the_ledger_holds_the_loop_and_only_the_loop(demo: DemoRun) -> None:
    """Four events, one per mutation, attributed by `principal_id`, all successes, all in
    the organization. The refused attempts and the read left none."""
    assert demo.ledger.json()["total"] == 4
    assert [
        (event["principal_id"], event["action"], event["resource_type"])
        for event in demo.ledger_items
    ] == _LOOP_EVENTS
    assert {event["outcome"] for event in demo.ledger_items} == {"SUCCESS"}
    assert {event["organization_id"] for event in demo.ledger_items} == {str(ORG)}
    assert {event["principal_type"] for event in demo.ledger_items} == {"USER"}


async def test_the_ledger_names_maker_and_checker_against_the_same_review(demo: DemoRun) -> None:
    """Act 3, step 4: 'the ledger now names dana.steward and riya.reviewer against the same
    review'."""
    against_the_review = {
        event["principal_id"]: event["action"]
        for event in demo.ledger_items
        if event["resource_id"] == demo.review["id"]
    }

    assert against_the_review == {
        "dana.steward": "glossary.term.submit",
        "riya.reviewer": "governance.review.decide",
    }
    decided = next(e for e in demo.ledger_items if e["action"] == "governance.review.decide")
    assert decided["details"]["decision"] == "APPROVE"
    assert decided["details"]["object_id"] == demo.term["id"]


async def test_reading_the_ledger_is_not_itself_recorded(demo: DemoRun) -> None:
    """Omar's read at step 5, and a second one now, leave no row: the ledger still holds the
    four mutations and still names no auditor."""
    again = await demo.script.call("GET", "/v1/organizations/$org/audit-events?limit=8", OMAR)

    assert again.status_code == 200
    assert again.json()["total"] == 4
    assert {event["principal_id"] for event in again.json()["items"]} == {
        "dana.steward",
        "riya.reviewer",
    }


async def test_the_ledger_lists_as_many_principals_as_the_page_says(
    demo: DemoRun, page: LoopPage
) -> None:
    """The page's Expected line says how many principals step 5 lists. It once said three,
    counting omar, whose refused 403 and read write no audit row; it now says two."""
    principals = {event["principal_id"] for event in demo.ledger_items}

    assert len(principals) == page.expected_principals, sorted(principals)


# ---------------------------------------------------------------------------
# What the page says around the loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "roles",
    ["PlatformAdmin,DataSteward", "PlatformAdmin,DataSteward,Reviewer,Auditor", "Reviewer,Viewer"],
    ids=["platform-admin", "every-deciding-role", "the-checkers-own-roles"],
)
async def test_the_maker_is_refused_under_any_role_she_sends(
    http: httpx.AsyncClient, roles: str
) -> None:
    """Act 3: 'enforced on the server by principal id, for every role including PlatformAdmin'.
    The same principal id with other roles is still the maker."""
    script = Script(http)
    _, _, review = await propose(script)

    response = await script.call(
        "POST",
        _DECISION,
        Identity("dana", "dana.steward", roles),
        {"decision": "APPROVE", "reason": "same person, other hat"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "maker-checker separation is required"}
    still = await _pending_reviews(http)
    assert [item["id"] for item in still.json()["items"]] == [review["id"]]


async def test_it_is_the_principal_id_that_separates_not_the_role(http: httpx.AsyncClient) -> None:
    """The converse: a different principal holding the maker's *own* roles may decide."""
    script = Script(http)
    _, term, review = await propose(script)

    response = await script.call(
        "POST",
        _DECISION,
        Identity("dana", "someone.else", DANA.roles),
        {"decision": "APPROVE", "reason": "a second steward"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["decided_by"] == "someone.else"
    assert response.json()["requested_by"] == "dana.steward"
    assert response.json()["object_id"] == term["id"] == review["object_id"]


async def test_a_409_at_step_4_can_also_mean_the_review_was_already_decided(
    demo: DemoRun,
) -> None:
    """The page's trouble table names two causes for 'HTTP 409 where you expected success': the
    same principal made and approved the change, or a step already ran. Re-running step 4
    answers 409 too, for the second reason, and the two are told apart only by `detail`."""
    again = await demo.script.call(
        "POST", _DECISION, RIYA, {"decision": "APPROVE", "reason": "twice"}
    )

    assert again.status_code == 409
    assert again.json() == {"detail": "governance review is already decided"}
    assert demo.dana_attempt.json()["detail"] != again.json()["detail"]


async def test_rerunning_the_loop_with_the_same_stamp_collides_on_the_category_key(
    http: httpx.AsyncClient,
) -> None:
    """`$stamp` is the category key's only variable part. It was taken to the minute
    (`yyyyMMddHHmm`), so the dry-run the page asks for and a rehearsal in the same minute
    collided; the page now takes it to the second (`yyyyMMddHHmmss`), and the trouble table
    names this cause. A second run with an identical stamp still answers 409 at step 1."""
    script = Script(http)
    await propose(script)

    second = await script.call(
        "POST",
        "/v1/organizations/$org/glossary-categories",
        DANA,
        {
            "category_key": f"demo_{STAMP}",
            "display_name": f"Demo {STAMP}",
            "description": "Walkthrough demo",
            "parent_id": None,
        },
    )

    assert second.status_code == 409
    assert second.json() == {"detail": "glossary category key already exists"}


# ---------------------------------------------------------------------------
# Acts 4 and 5 lean on the same refusals
# ---------------------------------------------------------------------------

_REFUSED_TO_AN_AUDITOR = [
    ("POST", "/v1/governance/reviews/{id}/decision"),
    ("POST", "/v1/relationship-candidates/{id}/decision"),
]

_A_TERM = {
    "term_key": "no_such_term",
    "display_name": "No such term",
    "definition": "Refused before it is read.",
    "category_id": None,
    "synonyms": [],
    "owner_principal": None,
}
_A_DECISION = {"decision": "APPROVE", "reason": "refused before it is read"}

#: The 'Refused here' cells of the users table that fall on routes this loop touches.
_REFUSED_ON_THE_USERS_PAGE = [
    pytest.param(
        RIYA, "POST", "/v1/organizations/{org}/glossary-terms", _A_TERM, id="riya-propose-a-term"
    ),
    pytest.param(
        RIYA,
        "POST",
        "/v1/relationship-candidates/{nowhere}/decision",
        _A_DECISION,
        id="riya-decide-a-relationship",
    ),
    pytest.param(
        RAVI, "POST", "/v1/organizations/{org}/glossary-terms", _A_TERM, id="ravi-propose-a-term"
    ),
    pytest.param(
        RAVI,
        "POST",
        "/v1/governance/reviews/{nowhere}/decision",
        _A_DECISION,
        id="ravi-decide-a-review",
    ),
    pytest.param(
        ANA,
        "POST",
        "/v1/governance/reviews/{nowhere}/decision",
        _A_DECISION,
        id="ana-decide-a-review",
    ),
    pytest.param(
        ANA, "GET", "/v1/organizations/{org}/audit-events", None, id="ana-read-the-ledger"
    ),
    pytest.param(
        DANA, "GET", "/v1/organizations/{org}/audit-events", None, id="dana-read-the-ledger"
    ),
]


@pytest.mark.parametrize(("who", "method", "template", "body"), _REFUSED_ON_THE_USERS_PAGE)
async def test_the_users_page_refusals_on_the_loops_routes_are_403_on_the_role(
    http: httpx.AsyncClient, who: Identity, method: str, template: str, body: dict[str, Any] | None
) -> None:
    """'Refused here' on roles-and-users.html for riya, ravi, ana and dana. Each is a role
    refusal, not a tenant refusal or a 404 for an id that is not there."""
    response = await http.request(
        method,
        template.format(org=ORG, nowhere=NOWHERE),
        headers=who.headers,
        json=body,
    )

    assert response.status_code == 403
    assert response.json()["detail"].startswith("one of these roles")


async def test_alex_cannot_approve_his_own_proposal_holding_fifteen_roles(
    http: httpx.AsyncClient,
) -> None:
    """The users page, alex.operator: 'Approve his own proposal. The server answers 409: maker
    is not checker, and PlatformAdmin is not exempt.' Fifteen of the sixteen roles, one
    principal."""
    script = Script(http)
    _, _, review = await propose(script, maker=ALEX)

    response = await script.call(
        "POST", _DECISION, ALEX, {"decision": "APPROVE", "reason": "my own proposal"}
    )

    assert "PlatformAdmin" in ALEX.roles.split(",")
    assert response.status_code == 409
    assert response.json() == {"detail": "maker-checker separation is required"}
    still = await _pending_reviews(http)
    assert [item["id"] for item in still.json()["items"]] == [review["id"]]


async def test_ana_crossing_into_another_organization_gets_the_tenant_403(
    http: httpx.AsyncClient,
) -> None:
    """The users page, ana.analyst: 'Cross into another organization: the tenant check answers
    403.' A role that is admitted is still refused another tenant's path."""
    response = await http.get(f"/v1/organizations/{uuid4()}/glossary-terms", headers=ANA.headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "cross-organization access denied"}


@pytest.mark.parametrize(("method", "template"), _REFUSED_TO_AN_AUDITOR)
async def test_act_4_omars_bundle_is_refused_on_both_decision_routes_the_page_names(
    http: httpx.AsyncClient, method: str, template: str
) -> None:
    """'The review-decision and relationship-decision routes refuse his bundle.' The role
    check runs before the target is looked up, so an id that does not exist is still a 403,
    not a 404: the refusal is about who is asking."""
    response = await http.request(
        method,
        template.format(id=uuid4()),
        headers=OMAR.headers,
        json={"decision": "APPROVE", "reason": "auditor"},
    )

    assert response.status_code == 403
    assert "is required" in response.json()["detail"]


async def test_act_5_a_viewer_is_refused_the_ledger_with_the_sentence_the_pages_quote(
    http: httpx.AsyncClient,
) -> None:
    sentence = (
        "one of these roles is required: Auditor, Operations, OrganizationAdmin, PlatformAdmin"
    )

    response = await http.get(f"/v1/organizations/{ORG}/audit-events", headers=VIC.headers)

    assert response.status_code == 403
    assert response.json() == {"detail": sentence}
    assert sentence in _page_text(DEMO_SCRIPT)
    assert sentence in _page_text(ROLES_PAGE)
