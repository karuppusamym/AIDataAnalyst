# Review 2026-09-05 — remediation points tracker

Live status of every point raised in [REVIEW.md](REVIEW.md), [UX-AND-JOURNEYS.md](UX-AND-JOURNEYS.md) and [ROADMAP.md](ROADMAP.md).

Remediation started: 5 September 2026. Branch: `feature/agent-os-v2`. Base revision: `03bedc1`.

**Scope decision taken at kickoff:** the legacy `ui/` frontend is **removed outright** rather than retired behind a parity gate (D05 / T27). The review's parity-matrix recommendation is superseded by that decision; nothing in the legacy portal is being fixed.

## How to read the status column

| Status | Meaning |
|---|---|
| ☐ Not started | No work has begun |
| ◐ In progress | A work lane is actively changing code |
| ☑ Done | Change landed **and** its stated gate ran green |
| ⊘ Deferred | Deliberately not done in this pass — reason recorded |
| ⚠ Blocked | Needs an external prerequisite (a real destination, an IdP, a deployed topology) |

"Done" here means the code change landed and the local gate passed. It does **not** claim production verification: the review is explicit that a local success object is not service evidence, and points needing a real destination or a deployed topology are marked ⚠ until that evidence exists.

## Work lanes

Seven lanes with disjoint file ownership, running concurrently.

| Lane | Area | Owns |
|---|---|---|
| A | Audit archive integrity | `worm_archive.py`, `audit_envelope.py`, `audit_archive_storage.py`, `observability_audit/` module |
| B | Delivery durability | `siem_routing.py`, `siem_delivery.py`, `delivery_intents.py`, governance notifications + relay |
| C | Governance decision concurrency | `semantic_api.py`, `governance_decision_service.py` |
| D | Platform operations | `main.py`, `platform/config.py`, `authorization_gate.py`, identity lifecycle, review-queue read model |
| E | Frontend shell + API client | `App.tsx`, `main.tsx`, `lib/api*`, `lib/org.tsx`, `lib/scope.tsx` |
| F | Screens + components | `screens/**`, `components/**`, `lib/ui-types.ts` |
| G | Infrastructure, hygiene, docs | compose, nginx, Dockerfile, `.gitignore`, `Docs/`, legacy `ui/` removal |
| — | Shared contracts (done first, in-session) | `lib/routes.ts`, `lib/location.ts`, `lib/navigate.ts`, `lib/useUrlState.ts`, `lib/http.ts`, `lib/session.tsx` |

## 1. Engineering findings (REVIEW.md §1)

| ID | P | Finding | Lane | Status | Evidence / note |
|---|---|---|---|---|---|
| F01 | P0 | WORM archival reports completion without storing the archive | A | ◐ | Two-phase PREPARED→UPLOADED→VERIFIED→FAILED lifecycle; real filesystem + S3 object-lock providers; `NullArchiveStorage` refuses rather than reporting success |
| F02 | P0 | Archive checksum excludes the fields auditors need protected | A | ◐ | Versioned canonical envelope serialization; v1 preserved and labelled narrower |
| F03 | P1 | Archive progress can permanently skip events | A | ◐ | `(occurred_at, id)` keyset cursor, explicit membership table, per-org lease |
| F04 | P0* | SIEM routing returns success without delivering | B | ◐ | Real webhook/syslog transports + typed outcome; `include_details` honoured. *P0 only when SIEM is a promised capability |
| F05 | P1 | Bulk governance decisions can race with another decision | C | ◐ | One decision service; atomic claim; loser gets CONFLICT + refreshed state |
| F06 | P1 | Browser OIDC integration is incomplete | E | ◐ | Explicit `AUTH_MODE` (development/oidc/proxy); dev headers no longer sent in OIDC mode. Full flow ⚠ on IdP choice |
| F07 | P1 | Gateway's copied MCP endpoint fails in production topology | G + F | ◐ | Nginx `/mcp` upstream (G) + on-screen connection diagnostic (F) |
| F08 | P1 | Copied permalinks do not include their screen | F | ◐ | `buildLink()` contract landed; 17 call sites migrating |
| F09 | P1 | URL state can drift from rendered state | — + E + F | ◐ | Location store landed and green; shell + screens migrating |
| F10 | P1 | Organization/scope state updated in separate phases | E | ◐ | Synchronous org mirror, immediate scope clear, response tagging, `ready` gate on writes |
| F11 | P1 | Workspace authorization can remain SHADOW in production | D | ◐ | Posture made explicit + production-validated; default deliberately unchanged |
| F12 | P1 | Failed governance notifications permanently considered processed | B | ◐ | Durable delivery intents + worker; watermark tied to intent creation |

## 2. Product and reliability defects (REVIEW.md §2)

| ID | P | Finding | Lane | Status | Evidence / note |
|---|---|---|---|---|---|
| F13 | P1 | Shell always claims it is live and connected | E | ◐ | `session.tsx` landed: demo/connecting/connected/disconnected/session-expired/forbidden + last success time |
| F14 | P2 | Error handling discards structured errors and correlation IDs | — + E | ◐ | `http.ts` decoder landed (code, correlation id, field errors, retryability); api.ts migrating |
| F15 | P1 | Source/workspace pickers silently operate on capped first pages | E | ◐ | Server-side search + cursor + fetch-by-id + explicit truncation |
| F16 | P2 | Review-queue composition still contains an N+1 query path | D + F | ◐ | Aggregate summary read model (D); Overview stops fetching 1,000 reviews (F) |
| F17 | P2 | Unmatched URL paths produce unbounded metric labels | D | ◐ | Normalize to `__unmatched__` |
| F18 | P2 | Readiness can report stale Temporal availability | D | ◐ | Bounded live probes; optional subsystems reported, not gating |
| F19 | P1 | Principal-leaver reconciliation has no production entry point | D | ◐ | Replay-safe consumer wired to the scheduler, off by default |
| F20 | P1 | Ask Atlas presents metadata but not the returned result rows | F | ◐ | Bounded policy-aware result panel; no new value retention |
| F21 | P2 | Browser resilience and modal accessibility need shared infrastructure | E + F | ◐ | Route error boundary (E); Dialog/AsyncState/Toast/FormErrors primitives (F) |
| F22 | P2 | Consumer/Developer navigation inconsistent with identity UX | E + F | ◐ | Work areas separated from personas; onboarding keyed by user+org |

## 3. Refactoring (REVIEW.md §3)

| ID | Item | Lane | Status | Note |
|---|---|---|---|---|
| R01 | Finish the modular monolith before service extraction | C, D | ◐ | Behaviour moving by use case, not by file move. Multi-pass work |
| R02 | Split high-risk functions by invariants | C | ◐ | `_apply_governance_review_decision` (921 lines) first; the other five hotspots ⊘ this pass |
| R03 | Remove schema/service and router/router import cycles | C, E | ◐ | `api → org → api` cycle (E); semantic_api cycle avoided by a dependency-light service (C) |
| R04 | Reduce centralized ORM/schema coupling | — | ⊘ | Deferred: incremental by bounded context, and the models are already split under `src/atlas/modules/*`. Not started this pass |
| R05 | Split the frontend API client by domain | E | ◐ | Domain modules behind an unchanged re-export barrel |
| R06 | Break large screens into workflow sections | F | ◐ | Partial and deliberate — correctness of the interaction fixes comes first |
| R07 | Deduplicate verified copies | E, F | ◐ | Six inline URL-state hooks removed (F). AST-duplicate Python pairs ⊘ this pass |

## 4. Hygiene (REVIEW.md §4)

| ID | Item | Lane | Status | Note |
|---|---|---|---|---|
| D01 | Safe cleanup candidates after usage verification | F | ◐ | `ProposalCard`/`OrgPicker` — integrate or delete, with an import-trace as evidence. `injection_corpus.py` explicitly kept (offline eval tooling) |
| D02 | Inactive feature code should be explicitly owned | D, F | ◐ | Principal lifecycle wired (D); `orders_raw` propagation story kept demo-only with a visible label (F) |
| D03 | Do not remove compatibility shims prematurely | — | ⊘ | Deliberately unchanged. Shims are migration seams; each needs an owner and a removal condition, which is a separate change |
| D04 | 60 tracked Vitest timestamp artifacts | G | ◐ | `.gitignore` rule **plus** `git rm --cached` — the ignore alone does not untrack |
| D05 | Legacy UI retirement | G | ◐ | **Removed outright** per explicit approval. Parity gate not required |
| D06 | Documentation truth drift | G | ◐ | Broken links fixed, status doc corrected, capability register created with implemented/reachable/configured/verified as four distinct fields |

## 5. Operational improvements (REVIEW.md §5)

| Area | Lane | Status | Note |
|---|---|---|---|
| Graph projection memory bounds | — | ⊘ | Deferred. Needs a measured large-source rebuild to target; not a blind change |
| Outbox delivery vs. Kafka atomicity | — | ⊘ | Deferred pending contention measurement, per the review's own framing |
| Review pages: summary/list/detail split | D, F | ◐ | Summary read model this pass; p95 budgets ⊘ |
| Global scope discoverability | E | ◐ | F15 |
| Tenant fairness / per-tenant budgets | — | ⊘ | Deferred; needs product input on budget policy |
| Connection pool math | — | ⊘ | Deferred; documentation + measurement task, not a code change |
| Search/retrieval stage metrics | — | ⊘ | Deferred with R02's remaining hotspots |
| Observability: success vs. configuration | A, B, D | ◐ | Archive receipts, delivery outcomes, readiness last-success — the three places the review named |
| Retention / legal hold | A | ◐ | Legal hold now executes against the provider; broader asset retention ⊘ pending security requirements |
| Disaster recovery | — | ⚠ | Needs a deployed topology and an RPO/RTO owner. T28 |

## 6. Security and AI governance (REVIEW.md §6)

| # | Item | Status | Note |
|---|---|---|---|
| 1 | Surface-to-control matrix | ⊘ | Deferred — an audit artefact, best produced after the enforcement-posture work (F11) lands |
| 2 | Separate persona / roles / membership / delegation / agent identity | ◐ | F22 starts this in UI language; the code contracts remain partly conflated |
| 3 | One decision service for human, delegated, bulk and automated approval | ◐ | F05 / lane C |
| 4 | Verify direct IDs, links, projections and exports enforce list-page policy | ⊘ | Deferred — full authorization certification is separate work, as the review states |
| 5 | Classify model/integration output as untrusted until validated | ⊘ | Already partly present; no change this pass |
| 6 | Evaluation quality on representative tasks | ⊘ | Deferred |
| 7 | External destination and credential inventory | ◐ | Partially served by the archive/SIEM/notification state work; no unified inventory yet |
| 8 | Confirm real destination enforcement | ⚠ | Blocked on real destinations. This is the point F01/F04 are honest about rather than papering over |

## 7. Build, CI and developer experience (REVIEW.md §7)

| Item | Lane | Status |
|---|---|---|
| Production-proxy contract check (`/mcp`) | G | ◐ |
| Fresh-browser authentication check | E | ⚠ Needs a deployed topology |
| Deep-link correctness checks | F | ◐ |
| Archive/delivery receipt gates | A, B | ◐ |
| Review-decision concurrency gate | C | ◐ |
| Reachability review across both namespaces | — | ⊘ |
| Migration execution against PostgreSQL | — | ⚠ Needs a database; static head analysis only this pass |
| Frontend dependency scanning | — | ⊘ |
| SDK packaging alignment | G | ◐ |
| Domain READMEs / generated architecture map | G | ◐ Partial (capability register) |

## 8. UX and journeys (UX-AND-JOURNEYS.md)

| Item | Lane | Status | Note |
|---|---|---|---|
| §2 Navigation rearrangement into six work areas | E | ◐ | Work areas separated from personas (F22). Full six-area regrouping ⊘ — it depends on T11 landing first, per the roadmap |
| §2 Consolidate overlapping destinations | — | ⊘ | Deferred. Screen-id aliases must be preserved; a large move is its own reviewable change |
| §3 Reusable screen arrangement + precise state labels | F | ◐ | Delivery/authorization/content state families reflected in the new primitives |
| §4 Per-route improvements (40 routes) | F | ◐ | The confirmed defects among them (Ask results, gateway endpoint, permalinks, Overview counts) are in scope this pass; the rest are product work |
| §5 Persona journeys A–G | — | ⊘ | Journey completeness is the T15/T16 milestone, not this correctness pass |
| §6 New functionality | F | ◐ | Only "policy-aware answer results" (F20) is in scope now |
| §7 Accessibility and interaction | F | ◐ | Dialog focus management, keyboard row actions, graph table alternative, unsaved-edit handling |
| §8 Measurement baseline | — | ⊘ | Deferred; needs telemetry decisions with a retention policy |

## 9. Roadmap task mapping (ROADMAP.md)

| Task | Covered by | Status |
|---|---|---|
| T01 Canonical serialization + checksum versioning | F02 / lane A | ◐ |
| T02 Real archive provider with receipts | F01 / lane A | ◐ (⚠ for a real cloud bucket) |
| T03 Idempotent archive membership | F03 / lane A | ◐ |
| T04 Durable SIEM delivery | F04 / lane B | ◐ |
| T05 Shared atomic review transition | F05 / lane C | ◐ |
| T06 Durable notification intents | F12 / lane B | ◐ |
| T07 Complete browser authentication | F06 / lane E | ◐ partial, ⚠ on IdP choice |
| T08 Proxy `/mcp` in production | F07 / lane G | ◐ |
| T09 Workspace enforcement readiness checks | F11 / lane D | ◐ |
| T10 Honest demo/live/session states | F13 / lane E | ◐ |
| T11 Typed routes, canonical links, one location store | F08/F09 | ◐ contract done |
| T12 Atomic scoped client/query state | F10 / lane E | ◐ |
| T13 Searchable paginated pickers | F15 / lane E | ◐ |
| T14 Bounded approved result grid | F20 / lane F | ◐ |
| T15 Resumable first-source setup | — | ⊘ Phase 2 product work |
| T16 Reorganize navigation and task handoffs | — | ⊘ Phase 2 |
| T17 Shared async/error/dialog primitives | F21 / lanes E+F | ◐ |
| T18 Common review detail | — | ⊘ Phase 2, depends on T05 |
| T19 Principal lifecycle source | F19 / lane D | ◐ |
| T20–T27 Phase 3 | R01–R07, D04–D06 | ◐ partial |
| T28 Production certification | — | ⚠ Needs a deployable target topology |

## Deliberate non-goals for this pass

Recorded so they are decisions rather than omissions:

- **The legacy `ui/` portal is deleted, not fixed.** Explicitly approved.
- **No default is flipped to a stricter posture** (F11's SHADOW default, fixture-mode default). The review is explicit that tightening must be an intentional migration; this pass makes each posture explicit, checkable and visible instead.
- **No migration rewriting or baseline regeneration** to make a check green.
- **No new business-value retention** was added to improve history or analytics.
- **No wholesale visual redesign.** The review states it would not fix the interaction defects found.
- **Compatibility shims kept** (`aida.db`, `aida.config`, models/schemas re-exports, route aliases). They are migration seams with consumers.
