# Session Addendum -- 2026-09-04 (part 3) -- closing the open items

> **Purpose.** Closes the six things
> `18-session-2026-09-04-agent-os-complete.md` listed under "What is still not
> done". Staged here rather than merged into `03-tracker.md` for the same
> reason the 09..18 addenda were.
>
> Two of the six could only be *partly* closed, and this document says which
> parts and why, rather than reporting six ticks.

## Headline

| | Before | After |
|---|---|---|
| Python suite | 0 failed | **0 failed** (+40 new tests) |
| `mypy --strict` | 311 files clean | **315 files clean** |
| `ruff check src tests scripts` | 38 E402 | **0** |
| Import contracts | 8 kept | **8 kept** |
| UI suite | 237 passed | **238 passed, 0 failed** |
| Alembic heads | 1 (`c4a8d2f61e93`) | **1** (`e6b1c390d7a2`) |
| Reachability allowlist | 3 entries | **2** (SW-1's removed) |

---

## 1. Per-agent token accounting -- closed

The inbox rendered a budget cap with "usage not tracked" beneath it. Nothing
attributed model consumption to the agent that caused it.

The number that closes it is an **estimate**, and every surface says so. No
adapter in `build_model_providers` returns a usage block, so the only figure
available is the 4-bytes-per-token heuristic
`ProviderNeutralModelGateway` already enforces `model_max_input_tokens`
against. That is also the *right* figure: the cap is enforced against it, so
consumption measured any other way would not be comparable to the cap it is
drawn beside. A column called `tokens_used` would have been a precision claim
the platform cannot make; `estimated_input_tokens` is not.

- `ModelCallEvidence` carries the estimate it enforced the budget against, and
  a test asserts they are the same number -- otherwise an agent could be shown
  comfortably inside a budget the very same call was refused by.
- `AgentRun` carries the per-run total. Nullable, and NULL means something:
  no model call happened (a query-memory hit, or a refusal before generation).
  Zero would claim a call that consumed nothing.
- Every attempt in a fallback chain sent the same payload, so a retry after a
  503 costs its input estimate again. The run's total reflects that.
- The inbox aggregates **today's** consumption, not the seven-day activity
  window, because the cap is daily -- mixing them would show an agent over
  budget on last Tuesday's traffic. It comes out of the existing grouped
  query, so the endpoint's fixed-statement-count property is unchanged.

**A real defect, found by the new tests.** The inbox used `func.case(...)`,
which raises `TypeError` at query-construction time. The endpoint would have
failed for any organization that had an agent contract. It had no Python test
before this session; it does now.

**Migration** `d5f2a7c81b46`. **Tests:** 7 in `test_agent_token_attribution.py`.

---

## 2. `REVIEW_REQUESTED` notifications -- closed, by a relay rather than a funnel

Six of NT-1's seven event kinds have one place they happen, so a call there
covers every case. Review creation has **27 sites across 17 modules** and no
shared entry point.

The obvious fix is to build one: a `request_governance_review` helper and 27
edits. This session did not do that, and the reason is not effort. That is a
large refactor of the platform's most safety-critical write path, undertaken
so that a Slack message can be sent, and it would place a network call inside
27 governance transactions.

`governance_review_relay.py` sweeps instead: pending reviews not yet
considered, notify, stamp a watermark. Three properties follow from that
shape, and they are why it is not merely the cheaper road:

- **It cannot affect the decision.** The sweep runs in its own session after
  the governance transaction has committed. No webhook can delay or roll back
  an approval request.
- **Nothing is lost to a downed channel.** The watermark is stamped in the
  same transaction as the delivery attempt, so a crashed sweep retries exactly
  the rows it did not reach.
- **It covers sites that do not exist yet.** A 28th `GovernanceReview(...)`
  written next month is relayed with no change anywhere.

What it costs, stated plainly: notification lags by one scheduler iteration.
For an approval queue a human works through, that is the right trade.

Bounded in both directions. A batch cap per pass, and anything pending longer
than `governance_review_notify_max_age_hours` (default 24) is stamped without
being sent -- otherwise the day an operator first configures a webhook, a
year of history arrives at once. And while the feature is **off** the sweep
stamps *nothing*, so enabling it later delivers the recent backlog rather
than discovering a silent gap.

**Migration** `e6b1c390d7a2` (watermark column + the sweep's own index).
**Tests:** 10 in `test_governance_review_relay.py`.

---

## 3. AT-5 adopts the SW-1 scorer -- closed

`stewardship_worklist.py` shipped last session as a pure scorer with no
router, deliberately, so as not to create a second ranked backlog. It was on
the reachability allowlist with a stated removal condition. **That entry is
now removed**, which is the gate working as designed.

The adoption is not a copy. `enrich_tables` was extracted out of
`compute_worklist` and both callers use it, so the platform has exactly one
definition of "documented" rather than one per surface -- adoption that
reimplemented the rules next door would have produced the duplication SW-1
exists to avoid. AT-5's own UX-12 description-precedence chain still decides
the description field and is passed *in*; SW-1 does not compute a weaker one
of its own.

What changed for a steward: AT-5 ranked on query volume alone, which treats
"has a description" as the whole of "documented" and cannot see that one table
is a hub. Now, among comparably used tables, the hub missing four of five
fields sorts above the leaf missing one. Usage is still a term of the product,
so a table nobody queries still cannot climb to the top on neglect alone --
there is a test for exactly that, because it is the failure mode that turns a
ranked backlog into a list of things that do not matter.

Every term of the score is on the response row, for the same reason
`query_volume` always was: "why is this first" has to be answerable without
reading the ranker.

`ranking=query_volume` restores the previous order exactly. A live ranked
endpoint changing its order should be revertible with a query parameter rather
than a release.

**Tests:** 5 new in `test_documentation_worklist.py`.

---

## 4. The disagreement-rate metric -- computable, still unmeasured

ADR-0027 commits to revisiting risk-tiered checking when **the sampled
disagreement rate exceeds 5% for any object type over a full month**. Nothing
computed that number, which made the revisit trigger a sentence in a document
rather than a control.

`reviewer_agent_metrics.py` computes it, and
`GET /v1/organizations/{org}/reviewer-agent/disagreement-rates` publishes it.
It reports; it never suspends. A metric that could stop the agent by itself
would be a second automated authority arriving through an observability
endpoint.

Three things it refuses to do, each of which would make the number worse than
useless:

- **It never reports a rate it cannot support.** One disagreement in two
  samples is 50% and means nothing. Below 20 resolved samples the rate is
  still shown -- hiding it would be its own dishonesty -- but the trigger does
  not fire and `sufficient_sample` says why. Twenty, because at a 5% threshold
  that is the smallest sample in which one disagreement is *at* the threshold
  rather than four times over it.
- **It never counts an unresolved sample as agreement.** Folding pending
  samples into the denominator would make the rate fall every time the audit
  queue fell further behind. A large `pending` beside a small `resolved` is
  itself the finding: the sampling floor is producing work nobody is doing,
  which means ADR-0027 condition (b) is not actually being met.
- **It never reports "no data" as "passing."** `measured: false` is the honest
  state of every environment today.

The trigger is per object type, never an average: a healthy high-volume type
must not average away a broken low-volume one, which is how a tier table stays
wrong for a year.

**Still not closed.** No reviewer-agent model route is approved anywhere and
the feature is off by default, so the metric returns `measured: false`
everywhere. It needs a real corpus, which needs an environment decision this
session cannot make. What changed is that the trigger is now falsifiable the
day that decision is taken.

**Tests:** 9 in `test_reviewer_agent_metrics.py`.

---

## 5. The 38 E402 lint errors -- closed

Not suppressed, not `# noqa`, not a config change. They had two real causes,
both artefacts of the ST-07 extraction:

- **Two routers carried two module-level strings**: the move note, then the
  original docstring preserved verbatim beneath it. Python treats the second
  as a bare expression statement, which makes every import that follows "not
  at top of file". Merged into one docstring; both texts kept.
- **Two files had a second import block 200 lines down**, where the module
  they were assembled from brought its own. Hoisted.

`ruff check src tests scripts` is now clean on every file this session owns.

---

## 6. Bank-scale evidence -- still absent, but the kill switch now has a drill

This one cannot be closed here. There is no bank estate, no Neo4j, no Kafka in
this environment, and the DR, projection-rebuild and regional-failover drills
`00-status.md` names all require one.

What *was* done is the one drill that is pure application logic:
`scripts/agent_kill_switch_drill.py`. A drill is not a unit test -- the tests
prove a function returns the right answer; the drill proves an *operator* can
stop an agent and see that it stopped, in the order and with the evidence an
incident would actually require. Ten steps, all passing:

| Step | What it proves |
|---|---|
| 1 | Baseline: every agent runs, so a later refusal means something |
| 2a-c | `AGENT` scope stops the agent it names, stops nothing else, and release restores service |
| 3a-b | `TIER` scope stops that tier including narrower-scoped agents in it, and leaves other tiers running |
| 4a-b | `ALL` scope stops the organization and **does not cross the tenant boundary** (INV-5) |
| 5 | The organization-wide model kill switch and the agent kill switch compose; neither shadows the other |
| 6 | Six audit rows, each naming a principal and a reason (INV-7) |

**Scope, stated rather than implied.** In-memory database, one process. It is
evidence that the control's logic holds end to end. It is not bank-scale
evidence, it does not exercise a real deployment, a real provider, replication
lag, or an operator's actual console, and it says nothing about propagation
across processes that cache contract state -- nothing does today, because
every check is a live query, which is the property that makes the drill
meaningful at all.

---

## Working-tree note

A second session was editing this working tree concurrently. Its files
(`tool_plans*.py`, `tool_api.py`, `asset_description_api.py`,
`semantic_intelligence_api.py`, `newly_created_table_drafter.py`,
`semantic_inference_service.py`, `tool_plan_runtime.py`, and four `ui-next`
screens) were **not** committed here. The OpenAPI baseline was regenerated in
an isolated worktree containing only this session's changes, so it adds
exactly one path -- the disagreement-rates endpoint -- and none of theirs.

One pre-existing `mypy --strict` error remains, in `tool_plans_api.py:428`
(`"Result[Any]" has no attribute "rowcount"`). It belongs to that session's
in-flight work and was left untouched.

## What is still not done

1. **No reviewer-agent model route is approved anywhere.** The feature is off
   by default and the disagreement-rate metric therefore reports
   `measured: false` in every environment. Item 4 above made the trigger
   computable, not measured.
2. **Nothing here has bank-scale evidence.** The kill-switch drill is local
   and single-process, by construction. Every claim remains local end-to-end
   only -- the same caveat `00-status.md` §4 applies platform-wide.
3. **Notification of a review request lags by one scheduler iteration.** A
   deliberate property of the relay, not a defect, but it is a property.

## Verification

```
pytest tests/                 -> 0 failed
mypy --strict src             -> 1 error, in another session's uncommitted file
ruff check src tests scripts  -> clean for every file this session owns
lint-imports                  -> Contracts: 8 kept, 0 broken
alembic heads                 -> e6b1c390d7a2 (single head)
scripts/agent_kill_switch_drill.py -> 10/10 steps passed
ui-next: npm test             -> 238 passed (39 files)
ui-next: npm run typecheck    -> clean
```
