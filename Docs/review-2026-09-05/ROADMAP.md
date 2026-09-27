# Atlas — prioritized implementation plan

> **Scope reconciled 2026-09-11.** [Tracker section P](../60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) owns current execution status; [reconciliation decisions](../60-delivery/23-review-reconciliation-2026-09-11.md) map the remaining work. This document retains design and dated evidence. Older priorities, partials and recommendations do not form a separate queue.

Proposed backlog from [REVIEW.md](REVIEW.md) and [UX-AND-JOURNEYS.md](UX-AND-JOURNEYS.md). Restored on 6 September 2026. Consult [POINTS-TRACKER.md](POINTS-TRACKER.md) before implementing: later remediation activity may already address an item. This document preserves the review's stable IDs; it does not reset tracker status.

Sizes are relative: S = localized; M = several components/services; L = cross-cutting workflow/integration. They are not calendar commitments. P0 blocks a release promising the affected safety capability; P1 precedes broader rollout; P2 is maintainability/workflow improvement.

## 1. Delivery sequence

### Phase 1 — Correctness and truthful capability states

An unfinished feature can remain explicitly disabled/unavailable while integration is completed. It must not report successful external delivery.

| ID | Priority / size | Owner | Concrete work | Dependencies | Acceptance evidence |
|---|---|---|---|---|---|
| T01 | P0 / M | Audit/backend | Canonical full-event serialization and versioned checksums | Agreed audit envelope | Protected-field changes alter hash; legacy coverage labeled; deterministic bytes |
| T02 | P0 / L | Platform/audit | Real archive provider, verify receipts, retention/hold operations | T01, destination config | Object retrieved/verified; failed writes not counted as archived |
| T03 | P1 / M | Platform/audit | Idempotent event membership/claims and late/equal-timestamp recovery | T01/T02 interfaces | No omissions on batch boundaries, late commits, crashes, concurrent replicas |
| T04 | P0 if SIEM promised / M | Security/platform | Durable SIEM delivery and honest states | Destination/minimization contract | Receipt, retries, deduplication, detail suppression |
| T05 | P1 / M | Governance/backend | Shared atomic review transition for single/bulk/automation | None | One winner under concurrency; consistent versions/audit/outbox |
| T06 | P1 / M | Platform/backend | Durable notification intents and worker; correct transaction lifecycle | Event conventions | Outage recovery; no false watermark; durable attempts |
| T07 | P1 / L | Identity/frontend/platform | Complete deployed browser auth/session flow | Identity integration choice | Fresh login, expiry/logout and least-privilege journeys |
| T08 | P1 / S | Frontend/platform | Production `/mcp` proxy and generated URL verification | Current MCP server | Initialize/list through exact copied production URL |
| T09 | P1 / M | Security/backend | Workspace rollout/enforcement readiness checks | T07, T12 | Ambiguous scope denied when enforcement promised; authorized journeys usable |
| T10 | P1 / S | Frontend | Truthful demo/live/session/dependency state | Explicit app configuration | Fixture mode unmistakable; auth/disconnection failures visible |

T01/T05/T08/T10 can begin independently. Define honest unavailable states immediately while external storage/SIEM/identity prerequisites are arranged. Do not infer production verification from a local implementation.

### Phase 2 — Complete the core user journey

| ID | Priority / size | Owner | Concrete work | Dependencies | Acceptance evidence |
|---|---|---|---|---|---|
| T11 | P1 / M | Frontend | Typed routes, canonical links, location store; replace hook copies | None | All copy links, Back/Forward, same-screen navigation work |
| T12 | P1 / M | Frontend/identity | Atomic scoped request/query state and tenant isolation | T11 preferred | Old responses cannot populate new scope; writes wait for readiness |
| T13 | P1 / M | Frontend/backend | Searchable paginated pickers and selected-ID lookup | T12 | Resources past first page discoverable; deep links select correctly |
| T14 | P1 / M | Analyst/frontend | Approved result grid and answer-first layout | T07/T11/T12 | Values/units/limits/evidence usable; retention behavior explicit |
| T15 | P1 / M | Product/frontend/backend | Resumable first-source setup with actual readiness | T07/T12/T13 | Empty workspace reaches scan/catalog/first consumer workflow |
| T16 | P1/P2 / L | Product/frontend | Task-oriented navigation and context-preserving handoffs | T11 | Operator/analyst/steward/reviewer/developer/auditor journeys complete |
| T17 | P2 / M | Frontend | Shared async/error/dialog primitives, unsaved edits | T11 | Keyboard/focus/errors consistent; correlation IDs retained |
| T18 | P2 / M | Governance/frontend/backend | Common review detail: diff, impact, assignment, conflict | T05/T11 | Current complete evidence available without extra lookup |
| T19 | P1/P2 / M | Identity/backend | Authoritative principal lifecycle trigger/reconciliation | Authenticated IAM/event contract | Removed/merged owner handled with audit/replay safety |

Phase 2 is complete when the journey works, not merely when pages render.

### Phase 3 — Reduce architecture and operational cost

| ID | Priority / size | Owner | Concrete work | Dependencies | Acceptance evidence |
|---|---|---|---|---|---|
| T20 | P2 / L | Backend/domain owners | Governance application service and typed target adapters | Stable T05 | All callers share preserved transition/audit/event semantics |
| T21 | P2 / L | Backend/domain owners | Finish bounded contexts and remove cycles incrementally | T20/contracts | Smaller routers, explicit transactions, enforceable privacy boundaries |
| T22 | P2 / M | Frontend | Domain API clients/demo adapter/generated response types | T12/T17 | No API/org cycle; boundary decoding/schema drift controlled |
| T23 | P2 / M | Backend/performance | Review aggregates plus batched/lazy diffs | Review service | Overview avoids full-review fetch; bounded queries and measured p95 |
| T24 | P2 / M | Platform/performance | Bounded graph projection/fair sweeps/lag metrics | Existing projection semantics | Large-source rebuild fits measured limits and reconciles deletions |
| T25 | P2 / S–M | Platform | Normalize unmatched metric labels; live dependency/task health | None | Unknown URLs do not explode series; failures visible |
| T26 | P2 / S | Engineering | Untrack generated timestamps, fix docs, verify SDK packaging | None | Ignore policy, green lint, valid links, intended artifacts |
| T27 | P2 / M | Frontend/product/platform | Complete legacy retirement and references/redirects cleanup | Current product scope | Required journeys supported; old deployment/docs paths handled |
| T28 | P1 for production certification / L | Platform/security | Upgrade/load/restore/connector/enforcement verification | Deployed topology | Recorded measurements and real receipts against release criteria |

**T27 scope update:** the existing tracker records explicit outright legacy removal, superseding the original parity-gated recommendation. Follow that recorded decision; do not reintroduce a confirmation/parity gate on the basis of this restored plan.

### Phase 4 — Expand from actual usage

Prioritize saved investigations, effective-access explanations, publish readiness, change subscriptions, and adoption analytics on the completed foundation. Add connectors, BI integrations, or autonomy only with a named need and certification owner. Avoid implementing an isolated function and marking the feature complete before its trigger/user flow/operation exist.

## 2. Suggested reviewable changesets

Check tracker status and actual code before starting each:

1. Canonical links/location state and per-screen query schemas, without a visual redesign.
2. Production MCP proxy contract and read-only diagnostic.
3. Accurate unavailable/disabled/delivery states while real transports are implemented.
4. Atomic bulk decision semantics with focused concurrent database verification.
5. Notification intent, independent delivery, durable outcome, retry/deduplication.
6. Archive serialization, manifest/claim, provider receipt, reconciliation.
7. Scoped authenticated client and migration of request consumers.

Keep hygiene/formatting changes separate. Do not let them displace these functional fixes.

## 3. A complete feature crosses five boundaries

| Boundary | Required evidence |
|---|---|
| Definition | User problem, role, allowed input/output, explicit unsupported cases |
| Implementation | Domain rules, concurrency/transaction behavior, auth and safe errors |
| Reachability | Mounted route, real event/worker trigger, or supported SDK entry |
| Experience | Discoverable action, prerequisites, async/empty/error states, next step, working link |
| Operation | Config, actual successful operation, receipts/audit, retry/recovery, monitoring, owner |

Import reachability does not establish invocation or delivery. A returned success object does not establish destination receipt. Test existence does not establish a usable journey.

## 4. Verification matrix for implementation

Test sources were excluded from the review. These are checks needed when implementing; they are not claims that no existing tests cover the areas.

| Scenario | Required outcome |
|---|---|
| Two reviewers decide one item | Exactly one final decision and consistent dependent versions/events |
| Bulk overlaps bulk/single | Correct per-item conflicts; no lost updates/failed-item partial writes |
| Tenant changes during slow request | Correct scope, ignored stale reads, no stale write context |
| Fresh tab opens each copied link | Correct route/object after auth and scope validation |
| Same route gets new selection | Correct rerender and Back/Forward behavior |
| Token expires | Clear recovery; no fallback to development identity |
| Production MCP URL | JSON-RPC through actual built proxy |
| Destination outage | Durable failure/retry, eventual receipt, deduplication |
| Equal-timestamp/late audit events | Complete manifest membership with no omissions |
| Crash after archive upload | Safe idempotent recovery and unambiguous membership |
| Audit principal/details altered | New full-envelope integrity check fails |
| Unresolved/shadow workspace | Declared release posture actually enforced and visible |
| IAM removes/merges user | Defined ownership effects, replay safety, audit |
| Estate exceeds picker caps | Search/select and direct-ID resolution still work |
| Approved query returns rows | Correct result/units/masking/limits, no unintended retention |
| Keyboard/zoom/narrow screen | Usable dialogs, forms, virtualized lists and graph alternative |
| Restore DB/rebuild projections | Measured recovery and event/workflow reconciliation |

## 5. Scope and cost guardrails

- Keep the modular monolith unless measured scaling/ownership needs justify extraction.
- Wire useful inactive logic or retire it deliberately; do not delete by import count alone.
- Preserve migration history, APIs, and supported route aliases; deprecate explicitly.
- Do not regenerate broad baselines just to turn checks green.
- Do not add business-value retention incidentally to improve history.
- Reuse existing quality/review/contract/evidence primitives.
- Assign feature flags an owner, default, production eligibility, disclosure, and retirement condition.
- Keep current capability state separate from historical accomplishment logs.

## 6. Next successful milestone

An ordinary user signs in, selects an authorized workspace, finds an asset or completes setup, obtains a useful governed answer/product, submits or receives independent review where needed, shares a working link, and inspects truthful evidence. Operators recover processing/delivery without losing decisions/events/scope. These journeys work through the actual production proxy and configured services.

Use that outcome to determine readiness and the next investment.
