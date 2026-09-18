#!/usr/bin/env python3
"""Seed the governed half of the answer-evaluation estate (tracker R11-FP13).

The answer-evaluation corpus
(`tests/fixtures/quality_benchmark_corpus/answer_evaluation_corpus.json`) scores
answers over an *enriched* footprint. `infra/sample-source/init.sql`'s
`warehouse` schema is the physical half of that footprint: the tables, the two
routines whose bodies the corpus's lineage is derived from, and their rows.
Three things the corpus needs are control-plane state no init script can
create, and a live run over an estate without them measures a different
product from the one the corpus was written against:

    1. the `end_of_day_position` ontology concept, published, carrying its
       `closing position` alias and an approved mapping to
       `fact_account_balances`;
    2. `nightly_settlement_rollup`'s discovered write lineage
       (`fact_payments` -> `fact_account_balances`) reviewed to ACTIVE --
       discovery lands every edge PROPOSED;
    3. that routine's Atlas-authored description, approved.

And one thing it needs *left alone*: `quarterly_fee_accrual`'s lineage must stay
an undecided proposal, because the corpus's gap case scores whether lineage
nobody approved steers an answer. This script proposes it (the same discovery
run proposes both routines) and never decides it.

**Nothing here writes an APPROVED or ACTIVE row.** Every item is proposed by one
identity and decided by another, through the platform's own decision path, so
the seed *exercises* maker-checker rather than working around it:

    concept       proposed by governed-enrichment-steward
                  decided by  governed-enrichment-reviewer
                  via POST /v1/governance/reviews/{id}/decision
    lineage       proposed by agent:lineage (the lineage agent, run by the steward)
                  decided by  governed-enrichment-reviewer
                  via POST /v1/lineage/parsed-edges/{id}/decision
    description   drafted by Atlas from catalog evidence, edited by the steward
                  decided by  governed-enrichment-reviewer
                  via POST /v1/governance/reviews/{id}/decision

`--same-identity` sends every decision as the item's own proposer instead, so
the refusals can be watched -- `scripts/seed_task_agent.py`'s precedent. For the
lineage that proposer is the agent itself: the steward who *ran* the agent did
not author its edges, and the parsed-edge queue's maker-checker is between the
edge's author and its reviewer.

The lineage is proposed by the lineage agent (ADR-0029), not by a person's
parse. That is deliberate: `POST .../procedures/{id}/lineage/parse` writes under
`lineage_parsed_edges_review_mode`, and at the shipped `auto_active` mode -- or at
`require_review` with a FULL-confidence edge over the 0.9 threshold -- it lands
the edge ACTIVE with no reviewer at all. The agent's edges are PROPOSED
whatever the mode, which is what makes the review below a real decision.

Idempotent. Each item is read back first and left untouched when it is already
governed; a proposal an interrupted run left behind (a DRAFT, or one awaiting
review) is carried forward rather than stacked beside a second one. Re-running
changes nothing. Anything this script did not propose -- a draft another
steward opened, a different approved description -- is reported, never decided
or overwritten.

Prerequisites, in order (a fresh stack runs `init.sql` on its empty volume, so
step 1 is automatic there; an existing volume needs the `warehouse` section
applied by hand, because an init script only ever runs once):

    1. `infra/sample-source/init.sql`'s `warehouse` schema, in `bank_demo`
    2. python scripts/seed_sample_estate.py
           registers `Customer Master (Postgres, sample)` and discovers it
    3. python scripts/seed_task_agent.py --org sample-bank --agent lineage --tier T1
           registers the lineage agent this script asks to propose the lineage
    4. python scripts/seed_governed_enrichment.py

Usage:
    python scripts/seed_governed_enrichment.py
    python scripts/seed_governed_enrichment.py --same-identity
    AIDA_BASE_URL=http://api:8000 python scripts/seed_governed_enrichment.py

Stdlib-only, like `seed_sample_estate.py`, whose HTTP helpers and environment
(`AIDA_BASE_URL`, `AIDA_SEED_SLUG`) it shares. Scoped to that one organization:
every request carries its id, and every object is looked up inside its
`Customer Master` datasource. Development and demonstration environments only.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import seed_sample_estate as estate  # noqa: E402
from scripts.seed_sample_estate import SeedError  # noqa: E402

# ---------------------------------------------------------------------------
# The specification. Each value is the corpus's own, pinned to its source by
# tests/test_seed_governed_enrichment.py: the concept and the description to
# `scripts/quality_benchmark.py`'s FOOTPRINT_* constants (the offline estate
# the same corpus runs over), the datasource to the corpus's
# `datasource_name_prefix`. Restated rather than imported so this script stays
# stdlib-only.
# ---------------------------------------------------------------------------

#: The datasource the corpus's live mode asks its questions against.
DATASOURCE_NAME_PREFIX = "Customer Master"

ONTOLOGY_KEY = "banking"
ONTOLOGY_NAME = "Banking"
CONCEPT_KEY = "end_of_day_position"
CONCEPT_NAME = "End of day position"
CONCEPT_ALIASES: tuple[str, ...] = ("closing position",)
CONCEPT_DESCRIPTION = "The ledger position a day closes on."
#: The table the concept's approved mapping names.
CONCEPT_TABLE = "fact_account_balances"

#: The schema both routines live in (`infra/sample-source/init.sql`).
ROUTINE_SCHEMA = "warehouse"
#: The routine whose write lineage is reviewed to ACTIVE, and the one edge
#: shape the review approves: it reads the first table and writes the second.
REVIEWED_ROUTINE = "nightly_settlement_rollup"
REVIEWED_ROUTINE_READS = "fact_payments"
REVIEWED_ROUTINE_WRITES = "fact_account_balances"
#: The routine whose lineage must stay an undecided proposal (the gap case).
UNDECIDED_ROUTINE = "quarterly_fee_accrual"
UNDECIDED_ROUTINE_WRITES = "fact_fraud_alerts"

#: The reviewed routine's Atlas-authored description.
ROUTINE_DESCRIPTION = (
    "Applies the cleared interbank drafts received each night to every depositor's holdings."
)

MAKER_PRINCIPAL = "governed-enrichment-steward"
CHECKER_PRINCIPAL = "governed-enrichment-reviewer"
MAKER_HEADERS = estate._headers(MAKER_PRINCIPAL)
CHECKER_HEADERS = estate._headers(CHECKER_PRINCIPAL)

_OWNER = "data-governance"
_PROVENANCE = (
    "Seeded by scripts/seed_governed_enrichment.py for the R11-FP13 answer-evaluation "
    "corpus (tests/fixtures/quality_benchmark_corpus/answer_evaluation_corpus.json)."
)
_UNPARSED = "UNPARSED"
_OPEN_DRAFT_STATUSES = ("DRAFT", "PENDING_APPROVAL")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

#: The item was already governed; nothing was written.
ALREADY = "ALREADY_GOVERNED"
#: This run carried the item through proposal and an independent decision.
GOVERNED = "GOVERNED"
#: `--same-identity`: the platform refused the proposer's own decision.
REFUSED = "REFUSED_AS_SELF_APPROVAL"
#: `--same-identity`: the platform accepted the proposer's own decision -- a
#: broken control, and a failed run.
SELF_APPROVED = "SELF_APPROVAL_ACCEPTED"
#: Something this script did not propose is in the way; nothing was decided.
CONFLICT = "CONFLICT"
#: The gap routine's lineage exists and nobody has decided it -- the state the
#: corpus's gap case needs, and one this script only ever checks.
UNDECIDED = "UNDECIDED_AS_REQUIRED"


@dataclass
class ItemOutcome:
    item: str
    state: str
    detail: str
    proposed_by: str | None = None
    decided_by: str | None = None
    refusals: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state in (ALREADY, GOVERNED, REFUSED, UNDECIDED)


@dataclass(frozen=True)
class Estate:
    org_id: str
    datasource_id: str
    datasource_name: str
    project_id: str
    table_id: str
    reviewed_routine_id: str
    undecided_routine_id: str


# ---------------------------------------------------------------------------
# Transport. Every call goes through `seed_sample_estate._request`, looked up
# at call time, so one redirect of that function (the in-process tests do it)
# carries every request this script makes.
# ---------------------------------------------------------------------------


def _get(path: str, org_id: str) -> Any:
    _, payload = estate._request("GET", path, org_id=org_id, headers=MAKER_HEADERS)
    return payload


def _post(
    path: str,
    body: dict[str, Any] | None,
    org_id: str,
    *,
    headers: dict[str, str] | None = None,
    expect: tuple[int, ...] = (200, 201, 202),
) -> tuple[int, Any]:
    status, payload = estate._request(
        "POST",
        path,
        body,
        org_id=org_id,
        headers=headers if headers is not None else MAKER_HEADERS,
        expect=expect,
    )
    return int(status), payload


def _bare(name: str | None) -> str:
    """A table's own name, whatever path the parser or the catalog spelled."""
    return str(name or "").strip().rsplit(".", 1)[-1].strip().strip('"').casefold()


def _label_table(label: str) -> str:
    """`warehouse.fact_payments.account_id` (a queue label) -> `fact_payments`."""
    return _bare(label.rsplit(".", 1)[0]) if "." in label else _bare(label)


def _decide_review(
    review_id: str, org_id: str, *, as_proposer: bool, reason: str
) -> tuple[bool, str]:
    """Decide one governance review. (accepted, detail).

    As the checker, anything but a 2xx stops the seed. As the proposer
    (`--same-identity`), a 409/403 is the expected answer and is returned.
    """
    status, payload = _post(
        f"/v1/governance/reviews/{review_id}/decision",
        {"decision": "APPROVE", "reason": reason},
        org_id,
        headers=MAKER_HEADERS if as_proposer else CHECKER_HEADERS,
        expect=(200, 201, 202, 403, 409) if as_proposer else (200, 201, 202),
    )
    if status >= 400:
        return False, f"HTTP {status}: {_detail(payload)}"
    return True, "approved"


def _detail(payload: Any) -> str:
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)


# ---------------------------------------------------------------------------
# Locating the estate
# ---------------------------------------------------------------------------


def resolve_estate(org_slug: str) -> Estate:
    """The organization, its Customer Master datasource and the catalog objects
    the three items name -- or a refusal that says which prerequisite is missing."""
    _, orgs = estate._request("GET", "/v1/organizations?limit=200", headers=MAKER_HEADERS)
    org = next((o for o in estate._items(orgs) if o.get("slug") == org_slug), None)
    if org is None:
        raise SeedError(
            f"no organization with slug {org_slug!r}; run scripts/seed_sample_estate.py first"
        )
    org_id = str(org["id"])
    datasources = estate._items(_get(f"/v1/organizations/{org_id}/datasources?limit=200", org_id))
    datasource = next(
        (d for d in datasources if str(d.get("name", "")).startswith(DATASOURCE_NAME_PREFIX)),
        None,
    )
    if datasource is None:
        raise SeedError(
            f"no datasource named {DATASOURCE_NAME_PREFIX!r}... in {org_slug!r}; "
            "run scripts/seed_sample_estate.py first"
        )
    datasource_id = str(datasource["id"])

    tables = [
        t
        for t in estate._items(
            _get(
                f"/v1/datasources/{datasource_id}/tables?q={CONCEPT_TABLE}&limit=100",
                org_id,
            )
        )
        if t.get("name") == CONCEPT_TABLE and t.get("status", "ACTIVE") == "ACTIVE"
    ]
    if not tables:
        raise SeedError(
            f"{datasource['name']} has no catalogued {CONCEPT_TABLE}. The `warehouse` "
            "schema is missing from the source or has not been discovered: apply "
            "infra/sample-source/init.sql's warehouse section to bank_demo (an init "
            "script only runs on an empty volume), then run scripts/seed_sample_estate.py"
        )
    if len(tables) > 1:
        raise SeedError(
            f"{datasource['name']} catalogues {len(tables)} tables named {CONCEPT_TABLE}; "
            "the concept's mapping would be ambiguous"
        )

    options = _get(
        f"/v1/projects/{datasource['project_id']}/context-product-routine-options?limit=500",
        org_id,
    )
    routines = {
        str(r["name"]): str(r["id"])
        for r in (options if isinstance(options, list) else [])
        if r.get("schema_name") == ROUTINE_SCHEMA and str(r.get("datasource_id")) == datasource_id
    }
    missing = [name for name in (REVIEWED_ROUTINE, UNDECIDED_ROUTINE) if name not in routines]
    if missing:
        raise SeedError(
            f"{datasource['name']} has no catalogued {ROUTINE_SCHEMA}.{', '.join(missing)}; "
            "discover the warehouse schema first (scripts/seed_sample_estate.py)"
        )
    return Estate(
        org_id=org_id,
        datasource_id=datasource_id,
        datasource_name=str(datasource["name"]),
        project_id=str(datasource["project_id"]),
        table_id=str(tables[0]["id"]),
        reviewed_routine_id=routines[REVIEWED_ROUTINE],
        undecided_routine_id=routines[UNDECIDED_ROUTINE],
    )


# ---------------------------------------------------------------------------
# 1. The ontology concept
# ---------------------------------------------------------------------------


def _concept() -> dict[str, Any]:
    return {
        "key": CONCEPT_KEY,
        "name": CONCEPT_NAME,
        "description": CONCEPT_DESCRIPTION,
        "aliases": list(CONCEPT_ALIASES),
        "deprecated": False,
    }


def _mapping(table_id: str) -> dict[str, Any]:
    return {"concept": CONCEPT_KEY, "subject_type": "TABLE", "subject_id": table_id}


def carries_concept(definition: dict[str, Any] | None, table_id: str) -> bool:
    """Whether a definition holds the concept as the corpus needs it: live, named
    as the harness matches it, with every alias, and mapped to *this* table."""
    if not isinstance(definition, dict) or definition.get("lifecycle") == "DEPRECATED":
        return False
    concept = next(
        (c for c in definition.get("concepts") or [] if c.get("key") == CONCEPT_KEY), None
    )
    if concept is None or concept.get("deprecated") or concept.get("name") != CONCEPT_NAME:
        return False
    aliases = {str(a).strip().casefold() for a in concept.get("aliases") or []}
    if not {a.casefold() for a in CONCEPT_ALIASES} <= aliases:
        return False
    return any(
        m.get("concept") == CONCEPT_KEY
        and m.get("subject_type") == "TABLE"
        and str(m.get("subject_id")) == table_id
        for m in definition.get("mappings") or []
    )


def merged_definition(base: dict[str, Any] | None, table_id: str) -> dict[str, Any]:
    """The next version's definition: the published one with the concept added.

    Nothing else in a published definition is dropped -- the ontology route
    refuses a version that removes a published key, and another steward's
    concepts are theirs. The concept's own aliases are kept and extended, and
    its mappings become exactly the one table: a mapping to a table that has
    since been re-discovered under a new id is no longer valid, and every write
    refuses a definition carrying an invalid mapping.
    """
    concept = _concept()
    if base is None:
        return {
            "name": ONTOLOGY_NAME,
            "owner": _OWNER,
            "provenance": _PROVENANCE,
            "lifecycle": "ACTIVE",
            "concepts": [concept],
            "relations": [],
            "mappings": [_mapping(table_id)],
        }
    existing = next((c for c in base.get("concepts") or [] if c.get("key") == CONCEPT_KEY), None)
    if existing is not None:
        kept = [str(a) for a in existing.get("aliases") or []]
        folded = {a.strip().casefold() for a in kept}
        concept["aliases"] = kept + [a for a in CONCEPT_ALIASES if a.casefold() not in folded]
    return {
        **base,
        "lifecycle": "ACTIVE",
        "concepts": [c for c in base.get("concepts") or [] if c.get("key") != CONCEPT_KEY]
        + [concept],
        "mappings": [m for m in base.get("mappings") or [] if m.get("concept") != CONCEPT_KEY]
        + [_mapping(table_id)],
    }


def _ontology_versions(org_id: str) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _get(
            f"/v1/organizations/{org_id}/ontology-versions?limit=100&offset={offset}", org_id
        )
        rows = page if isinstance(page, list) else []
        versions.extend(r for r in rows if r.get("ontology_key") == ONTOLOGY_KEY)
        if len(rows) < 100:
            return versions
        offset += 100


def govern_concept(ctx: Estate, *, same_identity: bool) -> ItemOutcome:
    item = f"concept {CONCEPT_KEY}"
    versions = _ontology_versions(ctx.org_id)
    published_version = max((int(v.get("published_version") or 0) for v in versions), default=0)
    published = next(
        (
            v
            for v in versions
            if v.get("status") == "APPROVED" and int(v.get("version", -1)) == published_version
        ),
        None,
    )
    if published is not None and carries_concept(published.get("definition"), ctx.table_id):
        return ItemOutcome(
            item,
            ALREADY,
            f"{ONTOLOGY_KEY} v{published_version} is published and carries it",
            proposed_by=published.get("created_by"),
            decided_by=published.get("approved_by"),
        )

    in_flight = [v for v in versions if v.get("status") in ("DRAFT", "PENDING_APPROVAL")]
    ours = next(
        (
            v
            for v in in_flight
            if v.get("created_by") == MAKER_PRINCIPAL
            and int(v.get("base_version") or 0) == published_version
            and carries_concept(v.get("definition"), ctx.table_id)
        ),
        None,
    )
    others = [v for v in in_flight if v.get("status") == "PENDING_APPROVAL" and v is not ours]
    if ours is None and others:
        return ItemOutcome(
            item,
            CONFLICT,
            f"{ONTOLOGY_KEY} v{others[0].get('version')} by {others[0].get('created_by')} is "
            "awaiting review; decide it before a new version can be based on the published one",
        )

    if ours is None:
        _, ours = _post(
            f"/v1/organizations/{ctx.org_id}/ontology-versions",
            {
                "ontology_key": ONTOLOGY_KEY,
                "base_version": published_version,
                "definition": merged_definition(
                    published.get("definition") if published else None, ctx.table_id
                ),
            },
            ctx.org_id,
        )
        print(f"  drafted {ONTOLOGY_KEY} v{ours['version']} carrying {CONCEPT_KEY}")
    if ours.get("status") == "DRAFT":
        _, ours = _post(f"/v1/ontology-versions/{ours['id']}/submit", None, ctx.org_id)
        print(f"  submitted {ONTOLOGY_KEY} v{ours['version']} for review")
    review_id = ours.get("governance_review_id")
    if not review_id:
        raise SeedError(f"ontology version {ours['id']} is awaiting review but names none")

    accepted, detail = _decide_review(
        str(review_id),
        ctx.org_id,
        as_proposer=same_identity,
        reason="Seeded answer-evaluation estate: the concept the corpus's alias cases stand on.",
    )
    if same_identity:
        return ItemOutcome(
            item,
            SELF_APPROVED if accepted else REFUSED,
            detail,
            proposed_by=MAKER_PRINCIPAL,
            decided_by=MAKER_PRINCIPAL if accepted else None,
            refusals=[] if accepted else [detail],
        )
    return ItemOutcome(
        item,
        GOVERNED,
        f"{ONTOLOGY_KEY} v{ours['version']} published",
        proposed_by=MAKER_PRINCIPAL,
        decided_by=CHECKER_PRINCIPAL,
    )


# ---------------------------------------------------------------------------
# 2. The routine lineage
# ---------------------------------------------------------------------------


def _routine_edges(ctx: Estate, routine_id: str) -> list[dict[str, Any]]:
    payload = _get(
        f"/v1/datasources/{ctx.datasource_id}/procedures/{routine_id}/lineage?limit=2000",
        ctx.org_id,
    )
    return payload if isinstance(payload, list) else []


def is_corpus_edge(edge: dict[str, Any], *, reads: str, writes: str) -> bool:
    """A table-to-table write edge from `reads` into `writes` -- the shape the
    corpus's lineage cases stand on. Markers, plumbing through a temp table and
    edges between any other pair are not it."""
    return (
        bool(edge.get("is_write"))
        and not edge.get("is_intermediate")
        and edge.get("transformation_type") != _UNPARSED
        and _bare(edge.get("source_table")) == reads
        and _bare(edge.get("target_table")) == writes
    )


def _pending_routine_items(ctx: Estate, routine_id: str) -> list[dict[str, Any]]:
    """This routine's PROPOSED edges, from the parsed-lineage review queue --
    the one read that carries an edge's id and its author."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _get(
            f"/v1/lineage/parsed-edges/review-queue?edge_type=ROUTINE&limit=500&offset={offset}",
            ctx.org_id,
        )
        rows = page.get("items", []) if isinstance(page, dict) else []
        items.extend(
            r for r in rows if (r.get("source_sql_reference") or {}).get("routine_id") == routine_id
        )
        if len(rows) < 500:
            return items
        offset += 500


def _propose_lineage(ctx: Estate) -> None:
    """Ask the lineage agent to propose, once, for the datasource. It parses
    every eligible routine body there with no lineage yet -- which is both of
    the corpus's routines -- and lands every edge PROPOSED."""
    state = _get(f"/v1/organizations/{ctx.org_id}/lineage-agent", ctx.org_id)
    if not isinstance(state, dict) or not state.get("registered"):
        reason = state.get("refusal_reason") if isinstance(state, dict) else None
        raise SeedError(
            "the lineage agent is not registered in this organization"
            + (f" ({reason})" if reason else "")
            + "; run: python scripts/seed_task_agent.py --org "
            f"{estate.ORG_SLUG} --agent lineage --tier T1"
        )
    if state.get("mode") != "PROPOSE":
        raise SeedError(
            f"the lineage agent runs in {state.get('mode')} mode and would propose nothing; "
            "register it at tier T1 or above"
        )
    _, run = _post(
        f"/v1/organizations/{ctx.org_id}/lineage-agent/run",
        {
            "capabilities": ["PROCEDURE_LINEAGE"],
            "datasource_id": ctx.datasource_id,
            "limit": 25,
        },
        ctx.org_id,
    )
    subjects = sorted(
        f"{i.get('subject_name')}={i.get('action')}"
        for i in (run.get("items") or [])
        if isinstance(i, dict)
    )
    print(
        f"  lineage agent run {run.get('run_id')}: proposed from {run.get('proposed')} "
        f"routine(s), skipped {run.get('skipped')} ({', '.join(subjects) or 'nothing examined'})"
    )


def govern_lineage(ctx: Estate, *, same_identity: bool) -> ItemOutcome:
    item = f"lineage {REVIEWED_ROUTINE}"

    def corpus(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            e
            for e in edges
            if is_corpus_edge(e, reads=REVIEWED_ROUTINE_READS, writes=REVIEWED_ROUTINE_WRITES)
        ]

    reviewed = _routine_edges(ctx, ctx.reviewed_routine_id)
    undecided = _routine_edges(ctx, ctx.undecided_routine_id)
    if not reviewed or not undecided:
        _propose_lineage(ctx)
        reviewed = _routine_edges(ctx, ctx.reviewed_routine_id)
    edges = corpus(reviewed)
    if not edges:
        raise SeedError(
            f"no {REVIEWED_ROUTINE_READS} -> {REVIEWED_ROUTINE_WRITES} write edge exists for "
            f"{ROUTINE_SCHEMA}.{REVIEWED_ROUTINE}: the lineage agent proposed none, or a "
            "reviewer rejected them (a rejected edge is not re-proposed)"
        )
    if all(e.get("review_status") == "ACTIVE" for e in edges):
        # The lineage read names no author or reviewer, and this script does
        # not claim one it cannot see; the audit trail holds both.
        return ItemOutcome(
            item,
            ALREADY,
            f"{len(edges)} {REVIEWED_ROUTINE_READS} -> {REVIEWED_ROUTINE_WRITES} edge(s) ACTIVE",
        )
    rejected = [e for e in edges if e.get("review_status") == "REJECTED"]
    if rejected:
        return ItemOutcome(
            item,
            CONFLICT,
            f"{len(rejected)} of its {len(edges)} corpus edge(s) were REJECTED by a reviewer; "
            "that decision stands and is not overridden here",
        )

    pending = [
        i
        for i in _pending_routine_items(ctx, ctx.reviewed_routine_id)
        if _label_table(str(i.get("source_label", ""))) == REVIEWED_ROUTINE_READS
        and _label_table(str(i.get("target_label", ""))) == REVIEWED_ROUTINE_WRITES
    ]
    if not pending:
        raise SeedError(
            f"{REVIEWED_ROUTINE}'s corpus edge(s) are not all ACTIVE, yet none of them is in "
            "the parsed-lineage review queue"
        )
    proposers = sorted({str(i.get("created_by")) for i in pending})
    accepted_by_self: list[str] = []
    refusals: list[str] = []
    for queued in pending:
        author = queued.get("created_by")
        if same_identity:
            if not author:
                refusals.append(f"edge {queued['edge_id']}: no recorded author to decide as")
                continue
            headers = estate._headers(str(author))
        else:
            headers = CHECKER_HEADERS
        status, payload = _post(
            f"/v1/lineage/parsed-edges/{queued['edge_id']}/decision",
            {
                "edge_type": "ROUTINE",
                "decision": "APPROVED",
                "reason": (
                    f"Seeded answer-evaluation estate: {REVIEWED_ROUTINE} writes "
                    f"{REVIEWED_ROUTINE_WRITES} from {REVIEWED_ROUTINE_READS}, as its body states."
                ),
            },
            ctx.org_id,
            headers=headers,
            expect=(200, 201, 202, 403, 409) if same_identity else (200, 201, 202),
        )
        if same_identity:
            if status >= 400:
                refusals.append(f"HTTP {status}: {_detail(payload)}")
            else:
                accepted_by_self.append(str(queued["edge_id"]))
    if same_identity:
        return ItemOutcome(
            item,
            SELF_APPROVED if accepted_by_self else REFUSED,
            refusals[0] if refusals else f"{len(accepted_by_self)} self-approved",
            proposed_by=", ".join(proposers) or None,
            decided_by=", ".join(proposers) if accepted_by_self else None,
            refusals=refusals,
        )

    after = corpus(_routine_edges(ctx, ctx.reviewed_routine_id))
    still = [e for e in after if e.get("review_status") != "ACTIVE"]
    if still:
        raise SeedError(
            f"{len(still)} of {REVIEWED_ROUTINE}'s corpus edge(s) are still "
            f"{sorted({str(e.get('review_status')) for e in still})} after review"
        )
    return ItemOutcome(
        item,
        GOVERNED,
        f"{len(pending)} {REVIEWED_ROUTINE_READS} -> {REVIEWED_ROUTINE_WRITES} edge(s) "
        "reviewed to ACTIVE",
        proposed_by=", ".join(proposers) or None,
        decided_by=CHECKER_PRINCIPAL,
    )


def check_gap_lineage(ctx: Estate) -> ItemOutcome:
    """The gap case needs the *other* routine's write lineage to exist and to be
    undecided. Never decided here; only checked."""
    item = f"gap {UNDECIDED_ROUTINE}"
    edges = [
        e
        for e in _routine_edges(ctx, ctx.undecided_routine_id)
        if e.get("is_write")
        and e.get("transformation_type") != _UNPARSED
        and _bare(e.get("target_table")) == UNDECIDED_ROUTINE_WRITES
    ]
    decided = [e for e in edges if e.get("review_status") != "PROPOSED"]
    authors = sorted(
        {
            str(i.get("created_by"))
            for i in _pending_routine_items(ctx, ctx.undecided_routine_id)
            if _label_table(str(i.get("target_label", ""))) == UNDECIDED_ROUTINE_WRITES
        }
    )
    if not edges:
        return ItemOutcome(
            item,
            CONFLICT,
            f"no proposed write edge into {UNDECIDED_ROUTINE_WRITES}; the gap case has no "
            "undecided path to be steered by",
        )
    if decided:
        return ItemOutcome(
            item,
            CONFLICT,
            f"{len(decided)} of its {len(edges)} edge(s) into {UNDECIDED_ROUTINE_WRITES} were "
            f"decided ({sorted({str(e.get('review_status')) for e in decided})}); the gap case "
            "needs them undecided",
        )
    return ItemOutcome(
        item,
        UNDECIDED,
        f"{len(edges)} edge(s) into {UNDECIDED_ROUTINE_WRITES} PROPOSED, undecided on purpose",
        proposed_by=", ".join(authors) or None,
    )


# ---------------------------------------------------------------------------
# 3. The routine description
# ---------------------------------------------------------------------------


def _routine_drafts(ctx: Estate, routine_id: str) -> list[dict[str, Any]]:
    payload = _get(
        f"/v1/organizations/{ctx.org_id}/routine-description-drafts"
        f"?datasource_id={ctx.datasource_id}&limit=1000",
        ctx.org_id,
    )
    return [d for d in estate._items(payload) if d.get("routine_id") == routine_id]


def govern_description(ctx: Estate, *, same_identity: bool) -> ItemOutcome:
    item = f"description {REVIEWED_ROUTINE}"
    routine_id = ctx.reviewed_routine_id
    current = _get(f"/v1/routines/{routine_id}/description", ctx.org_id)
    drafts = _routine_drafts(ctx, routine_id)
    # An approved Atlas-authored description is the one that is neither a
    # pending proposal nor the source system's own comment.
    authored = bool(
        isinstance(current, dict)
        and current.get("description")
        and not current.get("description_is_proposed")
        and not current.get("description_is_source_comment")
    )
    if authored and current.get("description") == ROUTINE_DESCRIPTION:
        approved = next(
            (
                d
                for d in drafts
                if d.get("status") == "APPROVED" and d.get("drafted_text") == ROUTINE_DESCRIPTION
            ),
            None,
        )
        return ItemOutcome(
            item,
            ALREADY,
            "approved and current",
            proposed_by=(approved or {}).get("created_by"),
            decided_by=(approved or {}).get("reviewed_by"),
        )

    draft = next((d for d in drafts if d.get("status") in _OPEN_DRAFT_STATUSES), None)
    if draft is not None and draft.get("created_by") != MAKER_PRINCIPAL:
        return ItemOutcome(
            item,
            CONFLICT,
            f"a {draft.get('status')} draft by {draft.get('created_by')} is open for it; "
            "it is theirs to finish or withdraw",
        )
    if draft is None and authored:
        return ItemOutcome(
            item,
            CONFLICT,
            "it already carries a different approved description; replacing reviewed text "
            "is a steward's decision, not a seed's",
        )
    if (
        draft is not None
        and draft.get("status") == "PENDING_APPROVAL"
        and (draft.get("drafted_text") != ROUTINE_DESCRIPTION)
    ):
        return ItemOutcome(
            item,
            CONFLICT,
            f"draft {draft.get('id')} is awaiting review with other text; decide it first",
        )

    if draft is None:
        # Atlas drafts from catalog evidence -- the reviewed lineage above is
        # part of it -- and the steward then edits in the corpus's text. The
        # edit is what makes the steward its author (and ineligible to approve).
        _, result = _post(
            f"/v1/organizations/{ctx.org_id}/routine-description-drafts/generate",
            {"routine_ids": [routine_id]},
            ctx.org_id,
        )
        created = result.get("drafts") or []
        if not created:
            raise SeedError(f"Atlas drafted nothing for {REVIEWED_ROUTINE}: {result}")
        draft = created[0]
        print(
            f"  Atlas drafted a description of {REVIEWED_ROUTINE} "
            f"(evidence score {draft.get('overall_score')})"
        )
    if draft.get("status") == "DRAFT":
        if draft.get("drafted_text") != ROUTINE_DESCRIPTION:
            _, draft = estate._request(
                "PUT",
                f"/v1/routine-description-drafts/{draft['id']}",
                {"drafted_text": ROUTINE_DESCRIPTION, "expected_text": draft["drafted_text"]},
                org_id=ctx.org_id,
                headers=MAKER_HEADERS,
            )
            print("  the steward edited the draft to the corpus's text")
        _, review = _post(f"/v1/routine-description-drafts/{draft['id']}/submit", None, ctx.org_id)
        review_id = str(review["id"])
        print(f"  submitted the description for review ({review_id})")
    else:
        review_id = str(draft.get("governance_review_id") or "")
        if not review_id:
            raise SeedError(f"draft {draft['id']} is awaiting review but names none")

    accepted, detail = _decide_review(
        review_id,
        ctx.org_id,
        as_proposer=same_identity,
        reason="Seeded answer-evaluation estate: the routine's steward-reviewed description.",
    )
    if same_identity:
        return ItemOutcome(
            item,
            SELF_APPROVED if accepted else REFUSED,
            detail,
            proposed_by=MAKER_PRINCIPAL,
            decided_by=MAKER_PRINCIPAL if accepted else None,
            refusals=[] if accepted else [detail],
        )
    after = _get(f"/v1/routines/{routine_id}/description", ctx.org_id)
    if not isinstance(after, dict) or after.get("description") != ROUTINE_DESCRIPTION:
        raise SeedError(f"{REVIEWED_ROUTINE}'s description is not the approved text: {after}")
    return ItemOutcome(
        item,
        GOVERNED,
        "approved and current",
        proposed_by=MAKER_PRINCIPAL,
        decided_by=CHECKER_PRINCIPAL,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def seed(org_slug: str, *, same_identity: bool = False) -> list[ItemOutcome]:
    """Govern the three items, in dependency order, and check the gap.

    Lineage first: Atlas's description draft is composed from the routine's
    ACTIVE lineage, so reviewing it first is what lets the draft say what the
    routine writes.
    """
    ctx = resolve_estate(org_slug)
    print(f"Governing the answer-evaluation enrichment on {ctx.datasource_name}")
    outcomes = [govern_lineage(ctx, same_identity=same_identity)]
    outcomes.append(check_gap_lineage(ctx))
    outcomes.append(govern_concept(ctx, same_identity=same_identity))
    outcomes.append(govern_description(ctx, same_identity=same_identity))
    return outcomes


def _report(outcomes: list[ItemOutcome]) -> None:
    print()
    for outcome in outcomes:
        who = ""
        if outcome.proposed_by or outcome.decided_by:
            who = (
                f" [proposed by {outcome.proposed_by or '?'}; "
                f"decided by {outcome.decided_by or '-'}]"
            )
        print(f"  {outcome.item:<42} {outcome.state:<24} {outcome.detail}{who}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--org", default=estate.ORG_SLUG, help="organization slug")
    parser.add_argument(
        "--same-identity",
        action="store_true",
        help="decide every item as its own proposer, to watch maker-checker refuse it",
    )
    args = parser.parse_args(argv)
    try:
        estate.wait_for_ready()
        outcomes = seed(args.org, same_identity=args.same_identity)
    except SeedError as error:
        print(f"\nSeed failed: {error}", file=sys.stderr)
        return 1
    _report(outcomes)
    if args.same_identity:
        if any(o.state == SELF_APPROVED for o in outcomes):
            print("\nA proposer approved their own item: maker-checker is NOT enforced.")
            return 1
        if not any(o.state == REFUSED for o in outcomes):
            print(
                "\nNothing was left to decide -- every item is already governed -- so there "
                "was no self-decision to refuse."
            )
        else:
            print(
                "\nmaker-checker is live: every self-decision was refused. Re-run without "
                "--same-identity to complete the enrichment."
            )
        return 0 if all(o.ok for o in outcomes) else 1
    if not all(o.ok for o in outcomes):
        print("\nThe estate does not carry the governed enrichment the corpus needs (above).")
        return 1
    print(
        "\nThe answer-evaluation corpus's governed enrichment is in place. The live run "
        "(paid) is:\n    uv run python scripts/answer_evaluation_benchmark.py --live "
        f"--i-understand-this-costs-money --base-url {estate.BASE_URL} --org {args.org}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
