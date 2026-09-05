# Session Addendum -- 2026-09-04 (part 2) -- the agent operating system, completed

> **Purpose.** New tracker rows and evidence for the second 2026-09-04 session
> on `feature/agent-os-v2`. Staged here rather than merged into
> `03-tracker.md` for the same reason the 09..17 addenda were. Fold these rows
> in on the next tracker rebase.
>
> This closes the six items `17-session-2026-09-04-agent-contract-and-suite-repair.md`
> listed under "What is not done".

## Headline

| | Before this session | After |
|---|---|---|
| Python suite | 0 failed | **0 failed** (+108 new tests) |
| `mypy --strict` | 304 files clean | **311 files clean** |
| Import contracts | 8 kept | **8 kept** |
| UI suite | 213 passed, 10 failed | **237 passed, 0 failed** |
| Alembic heads | 1 | **1** (`c4a8d2f61e93`) |

Five workstreams landed: the AG-10 tests and API, ADR-0027's reviewer agent,
the retrieval and learning loop, governance notifications, and the experience
shell. One planned item was deliberately **not** shipped -- see SW-1 below.

---

## Rows to add / update

### AG-11 / RV-1 -- ADR-0027 reviewer agent and risk tiers (P1)

Section: **F. Tools, model gateway, query gateway, governance**.

**ADR-0027 (Proposed)** argues that an independent reviewer agent may act as
checker for risk-tier T0/T1 only, under three hard conditions, and that this
satisfies INV-8's *intent* -- a second, independent judgement -- rather than
eroding it. The alternative it rejects is the tempting one: letting the
drafting agent auto-apply its own high-confidence output, which is one actor
approving its own work with an extra step.

- `review_risk_tiers.py` -- a pure function from object type to T0..T3.
  **Unknown types are T3**, so adding a governed object type defaults to
  human-only review. A test scans `src/` for real `GovernanceReview(...)`
  constructions and fails if any object type is unclassified, so the table
  cannot silently fall behind the code.
- `reviewer_agent.py` -- `pre_review_pending` attaches tier, blast radius,
  negative-knowledge hits, open quality incidents and a deterministic
  recommendation, and **decides nothing**; it is safe and useful with the
  agent disabled, which is the state every organization starts in.
  `auto_decide_tier0_tier1` acts on those through
  `_apply_governance_review_decision`, the same core a human checker uses, so
  no object type gets an agent-only path.
- **Nothing here is a model call.** The recommendation is a function of
  evidence the platform already holds. That is deliberate: it keeps the agent
  replayable, gives the eval corpus something to score, and means ADR-0027's
  risk argument does not depend on model behaviour. A model route can be
  added behind the same interface later; the tier ceiling still bounds it.
- Three guards: the decidable allowlist is derived *from the tier table*, so
  a misconfigured ceiling can narrow but never widen what an agent may touch;
  the agent never decides its own proposal; suspension is process-wide or
  per-organization and takes one action.

**Migration** `b7e3f19d5c24`: six nullable pre-review columns on
`governance_review`, plus `review_audit_sample` and `reviewer_agent_state`.
Additive; with the agent disabled nothing writes to any of it.

**Tests.** 43 in `tests/test_reviewer_agent.py`, including a static scan
proving `reviewer_agent.py` cannot reach a publish/activate/grant path, and
the determinism and 5%-floor properties the audit argument depends on.

---

### AG-10 (completion) -- contract tests, API, and the agent inbox read model (P1)

- 30 tests in `tests/test_agent_contract.py` covering the three guarantees
  the contract exists for: a named agent version with no contract is refused
  rather than run unconstrained; an engaged kill switch (its own, its tier's,
  the organization's) stops the run; a tool outside the envelope is refused.
  Plus the identity rules, the deterministic sampler, and value-freedom of
  the task ledger.
- `agent_contract_api.py` -- 12 endpoints: contract get/put, kill/release,
  agent-task listing, the agent inbox read model, reviewer-agent state,
  pre-review, run, suspend/resume, sample listing and resolution.
- The inbox is composed in a fixed number of queries with no per-row lookup.

**Two defects caught while building.** A missing `await` on
`resolve_audit_sample` (found by `mypy --strict`, would have silently done
nothing), and the kill/release handlers reading the request body before the
tenancy check, which INV-5 forbids and its Tier-0 test caught.

---

### RT-1 (closed) / AG-11 -- persisted vector index and exemplar few-shot (P0/P1)

**RT-1 closes its own named gap.** `vector_store.py` has implemented a
persisted, rebuildable index since RT-1 landed and *nothing ever called it*:
retrieval embedded the question **and every candidate** on each query. That
is correct, and it is why the vector stage was never wrong -- but it paid a
model call per candidate per query, so cost grew with the estate and with
traffic at the same time.

- `vector_index_service.py` -- `rebuild_vector_index` embeds catalog metadata
  once and upserts it, idempotent by `text_hash` so a scheduled run over an
  unchanged estate costs one query and no model calls. `index_freshness`
  names *why* the index is unusable (`DISABLED`, `EMPTY`, `STALE`,
  `STALE_CATALOG_MOVED`) because "why did my search fall back" has four
  different answers, and a model change makes vectors *unusable* rather than
  merely stale.
- `hybrid_retrieve` prefers the persisted index when fresh, falls back
  otherwise, and records which path ran per hit. **Policy still filters
  before ranking**: the candidate set handed to the index is the
  policy-narrowed lexical set, so the index can only reorder what the caller
  was already entitled to.
- **AG-11 exemplar few-shot.** Prior *confirmed* queries on the datasource go
  to generation as typed, clearly-labelled examples -- never in instruction
  position, literal-redacted SQL and table overlap only, no question text and
  no result values. `select_top_matches` shares the eligibility and staleness
  rules with the single-match adaptation path, so an exemplar can never come
  from memory the adaptation path would have refused as stale. Ids land in
  plan evidence. `exemplar_fewshot_k=0` disables the stage.

The reachability gate caught RT-1's own closure and required its allowlist
entry to be removed -- which is the gate working as designed.

**Tests.** 13 in `tests/test_vector_index_service.py`.

---

### SW-1 -- deliberately NOT a new endpoint (P2)

`stewardship_api.list_documentation_worklist` (AT-5) already owns "what
should a steward document next", ranking by real query volume. Shipping a
second ranked backlog would be the **two-catalogues seam** this platform's
own competitive research (`review-2026-08/research/04-cross-vendor-synthesis.md`
§5.3) names as a thing never to build.

`stewardship_worklist.py` therefore holds the richer `usage x impact x
deficit` scorer as a **pure function with no router**, for AT-5 to adopt. It
adds the two factors AT-5 lacks: downstream impact, and a five-field deficit
rather than description-only. It is on the reachability allowlist with that
reason and a removal condition.

This is a deliberate non-delivery, not an omission.

---

### NT-1 -- governance notifications to Slack and Teams (P1)

The adoption point the research makes repeatedly: a governance platform
nobody opens governs nothing.

- Seven event kinds, each with a deep link back into the portal.
- **Not a control surface.** Every message is a notification plus a link,
  never an action; the Teams renderer emits a `MessageCard` with no
  `potentialAction`, and a test asserts it.
- **Value-free by allowlist, not by convention.** This is the one place the
  platform sends data outward, so the renderer emits only known fields: a
  caller that mistakenly passes a sample row or SQL cannot get it onto the
  wire, and a test proves it.
- Off by default; a skipped attempt persists its reason so an operator can
  tell "not configured" from "delivered" without reading logs.
- `notify_safely` can never fail the caller: a downed Slack must not roll
  back the governance decision that triggered the notification.

**Hooked at four real funnels**: the review decision core, the agent kill
switch, quality incident routing, the certification expiry sweep.
**`REVIEW_REQUESTED` has no single creation funnel** (18 call sites) and is
currently reachable only through the test endpoint. Stated plainly rather
than claimed.

**Migration** `c4a8d2f61e93` makes `notification_event.incident_id`/`rule_id`
nullable. That table was built for DQ-1 where every notification is about an
incident matched by a rule; a governance event has neither. Nullable is the
smaller change than a second near-identical ledger.

**Tests.** 22 in `tests/test_governance_notifications.py`.

---

### UX-20 / UX-21 -- persona workbenches and the agent inbox (P1)

- **UX-20.** Navigation is organised by persona workbench, not feature area.
  Every screen id is unchanged, so every existing deep link still resolves.
  A persona lands in its own workbench when the URL names no screen; a deep
  link always wins.
- **UX-21.** The agent inbox: one call, five panels over one payload, so the
  summary cannot disagree with the list under it. Shows proposer kind (human
  or agent), the reviewer agent's recommendation *and whose it is*, prior
  rejections, and a per-agent kill switch with a mandatory reason. Server
  ordering is not re-sorted client-side.
- Two honest touches: the budget bar says "usage not tracked" rather than
  rendering a zero, because per-agent token consumption is not attributable
  yet; and the kill switch refuses in fixture mode rather than pretending.

**The UI suite went from 213 passed / 10 failed to 237 passed / 0 failed.**
The 10 pre-existing failures were all in test scaffolding or real small bugs,
none in the code under test:

| Failure | Cause |
|---|---|
| 4 spy assertions | Mocks re-passed named parameters, appending an explicit `undefined` for every omitted optional, so a two-argument call recorded as three. Spread-forwarded instead |
| `listAssetTermLinks` | Documented and returned a default limit of 100 but never sent it, so the two code paths disagreed about page size. **A real bug** |
| Glossary chips | `title` but no `aria-label`; `listitem` is not a name-from-content role, so a screen reader announced the instruction, not the term. **A real accessibility bug** |
| 3 assertions | Ambiguous because a single-item queue auto-opens its detail panel and the text legitimately appears twice |

---

## What is still not done

Stated plainly, as the previous addendum did.

1. **No reviewer-agent model route is approved anywhere**, and the feature is
   off by default, so nothing decides anything today. The disagreement-rate
   metric ADR-0027's revisit trigger depends on has never been measured
   because it needs a real corpus.
2. **`REVIEW_REQUESTED` notifications** are only reachable through the test
   endpoint; the review-creation path has 18 call sites and no funnel.
3. **Per-agent token accounting** does not exist. `AgentRun` carries no token
   count, so the inbox's budget bar shows the cap and says usage is not
   tracked.
4. **AT-5 has not adopted the richer worklist scorer.** Until it does,
   `stewardship_worklist.py` is unreached code with an allowlist entry.
5. **38 E402 lint errors remain**, a structural consequence of the ST-07
   extraction assembling files from moved code. Pre-existing and untouched.
6. **Nothing here has bank-scale evidence.** Every claim is local end-to-end
   only, the same caveat `00-status.md` §4 applies platform-wide. In
   particular no drill has been run against any of this session's code.

## Verification

```
pytest tests/                 -> 0 failed
mypy --strict src             -> Success: no issues found in 311 source files
ruff check src tests scripts  -> 38 errors, all pre-existing E402
lint-imports                  -> Contracts: 8 kept, 0 broken
alembic heads                 -> c4a8d2f61e93 (single head)
ui-next: npm test             -> 237 passed (39 files)
ui-next: npm run typecheck    -> clean
ui-next: npm run build        -> clean
```
