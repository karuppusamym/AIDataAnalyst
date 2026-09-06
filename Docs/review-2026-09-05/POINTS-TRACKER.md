# Review 2026-09-05 — remediation points tracker

Live status of every point raised in [REVIEW.md](REVIEW.md), [UX-AND-JOURNEYS.md](UX-AND-JOURNEYS.md) and [ROADMAP.md](ROADMAP.md).

Remediation started: 5 September 2026. Branch: `feature/agent-os-v2`. Base revision: `03bedc1`.
**Last updated: 6 September 2026**, after the implementation pass described below.

**Scope decision taken at kickoff:** the legacy `ui/` frontend is **removed outright** rather than retired behind a parity gate (D05 / T27). The review's parity-matrix recommendation is superseded by that decision; nothing in the legacy portal is being fixed.

## How to read the status column

| Status | Meaning |
|---|---|
| ☐ Not started | No work has begun |
| ◐ In progress / partial | Landed in part; the remainder is named in the note |
| ☑ Done | Change landed **and** its stated gate ran green |
| ⊘ Deferred | Deliberately not done in this pass — reason recorded |
| ⚠ Blocked | Needs an external prerequisite (a real destination, an IdP, a deployed topology) |

"Done" means the code change landed and the local gate passed. It does **not** claim production verification: the review is explicit that a local success object is not service evidence, and points needing a real destination or a deployed topology stay ⚠ until that evidence exists.

### Correction to the previous revision of this file

The earlier revision recorded lane statuses as ◐ **before the work existed** — none of the lane A/B/C modules were present in the tree when those rows were written, and the repository's own `ruff`/`mypy` gates were red at the time. Two specific claims were wrong and are corrected here:

- The lane A row claimed "real filesystem **+ S3 object-lock** providers". There is no S3 provider. `boto3` is not a dependency and none was added; `s3`/`gcs`/`azure_blob` parse but resolve to an explicitly *unavailable* provider that refuses. That is the honest state and it is what F01 now records.
- Several rows implied delivered work that had not been written. Every ☑ below names the gate that proves it.

This correction is the point of the exercise: a tracker that reports progress nobody can reproduce is the same defect class as an archive that reports success without storing anything.

## Gates that were run for this pass

Against the integrated tree, not per-lane:

| Gate | Result |
|---|---|
| `ruff check .` | pass |
| `mypy src sdk/aida_tool_sdk` | pass, 353 source files |
| `lint-imports` | 10 contracts kept, 0 broken (2 contracts added this pass) |
| `alembic heads` | single head |
| `pytest tests/` | see the run recorded in the commit message |
| `scripts/openapi_diff.py` | no breaking changes; baseline accepted for additive fields |
| `scripts/generate_ui_types.py` | `types.ts` matches the schema, no drift |
| `scripts/quality_benchmark.py` | no regression beyond 5 points |
| `scripts/perf_baseline.py` | no regression beyond 20% |
| `tsc --noEmit`, `vitest run`, `vite build` (ui-next) | pass, 465 tests |
| `test_migration_orm_drift.py` | ran against a real PostgreSQL, not skipped |

`ruff format --check .` is **red repo-wide on unmodified HEAD** (≈325 files, across `src/`, `tests/`, `migrations/` and `scripts/`). It is not a CI gate — `.github/workflows/ci.yml` runs `ruff check` only — and was deliberately not addressed: reformatting a third of the repository would bury this pass's diff. Files added or edited in this pass are individually format-clean.

## 1. Engineering findings (REVIEW.md §1)

| ID | P | Finding | Status | Evidence / what is actually true now |
|---|---|---|---|---|
| F01 | P0 | WORM archival reports completion without storing the archive | ☑ / ⚠ | Two-phase PREPARED→UPLOADED→VERIFIED→FAILED lifecycle; progress advances only past bytes a destination acknowledged **and** handed back intact. `FilesystemArchiveStorage` is complete (fsync, read-only mode bit, sidecar manifest, read-back verification, retention, legal hold). `NullArchiveStorage` is the **default** and refuses. `s3`/`gcs`/`azure_blob` resolve to an explicitly unavailable provider — **no cloud provider was implemented**. ⚠ for a real bucket: cloud object-lock has never been exercised. Filesystem immutability is a guard rail, not a security boundary; put a real WORM volume behind the root |
| F02 | P0 | Archive checksum excludes the fields auditors need protected | ☑ | `audit_envelope.py`: versioned canonical, length-delimited serialization of the whole envelope. 12 parametrized tests, one per protected field; further tests assert a new field cannot silently escape the hash, and that field boundaries cannot be shifted. v1 preserved for legacy rows and labelled narrower |
| F03 | P1 | Archive progress can permanently skip events | ☑ | `(occurred_at, id)` keyset cursor, an explicit membership table (archived is a fact, not an inferred range), a bounded overlap re-scan for late commits, and a per-organization lease. Tests cover equal-timestamp batch boundaries, late arrivals, crash-after-upload idempotence and two workers. Lease is a row with an expiry, not an advisory lock — it must survive the gap between upload and verification |
| F04 | P0* | SIEM routing returns success without delivering | ☑ / ⚠ | Real transports: webhook over `httpx`, syslog RFC 5424 (UDP datagram; TCP with RFC 6587 octet counting, length computed from encoded bytes). Durable `DeliveryIntent` with a state machine; `DeliveryOutcome` replaces the bare `bool` and `DELIVERED` is unreachable from the enqueue path by construction. Receipt proven against **real loopback servers**, not mocks. `include_details` is applied once at enqueue, so both transports suppress. ⚠ for a real SOC collector. *P0 only when SIEM is a promised capability |
| F05 | P1 | Bulk governance decisions can race with another decision | ☑ / ⚠ | One decision service; the claim is a single `UPDATE … WHERE id=… AND status='PENDING'`, so atomicity does not rest on `FOR UPDATE`. All four surfaces (single, bulk, sample review, reviewer agent) route through it. Proven on **file-backed** SQLite with two real connections; the tests were verified to fail when the guard is removed. ⚠ concurrent PostgreSQL reproduction is still outstanding, as the review requires |
| F06 | P1 | Browser OIDC integration is incomplete | ◐ / ⚠ | Landed: explicit `authMode` (development/oidc/proxy) separate from data mode; dev identity headers sent **only** in development mode; a blocking explanatory screen instead of 40 screens each silently 401ing; session expiry/forbidden/sign-out states; no silent fallback to the dev principal. ⚠ the authorization-code/PKCE redirect and refresh need a real IdP. The seam is `adoptAccessToken(token, expiresIn)` — wiring a callback to it completes the flow with no other change |
| F07 | P1 | Gateway's copied MCP endpoint fails in production topology | ☑ / ◐ | Production nginx now proxies `/mcp` (prefix match, so `/mcp/` is covered; 300s timeouts for governed tool calls; buffering deliberately left on with a note to change it if a streaming transport is added). Two gates: a static contract check that fails if nginx and `vite.config.ts` disagree about API prefixes, and a CI job that runs the **real built image** against a stub upstream and asserts on response bodies — including the pre-fix negative case, which returns the SPA shell. On-screen diagnostic distinguishes reached / unauthorized / **spa-shell** / unexpected / unreachable. ◐ a full JSON-RPC `initialize` through nginx into a live FastAPI has not been run |
| F08 | P1 | Copied permalinks do not include their screen | ☑ | One `buildLink()` owns route, allowed query fields and selection, and always writes the screen hash. **13 hand-built share URLs** converted; `grep location.origin` across screens and components now returns only the centralized MCP endpoint. Clipboard failure is surfaced with a selectable fallback input; inaccessible targets render a 403/404 state with the correlation id |
| F09 | P1 | URL state can drift from rendered state | ☑ | One location store on `useSyncExternalStore`; the URL is the single source of truth. Six inline copies of the old hook deleted. Tests cover Back/Forward, same-screen drilldown, filter edits using `replaceState`, and no duplicate history entry |
| F10 | P1 | Organization/scope state updated in separate phases | ☑ | Org mirror set **synchronously in the setter**, not in an effect — closing the window where the header and the screen named different organizations. Scope emptied during the render that observes the change; every response tagged with the org/workspace it was requested for and discarded on mismatch; one `resolveSelection` adopting atomically; `ready` gate disabling writes until scope resolves. Backend enforcement remains mandatory and unchanged |
| F11 | P1 | Workspace authorization can remain SHADOW in production | ☑ | `/health/ready` reports `workspace_authorization` as `ENFORCING` / `OBSERVING` / `SCOPE_UNRESOLVED` — "not enforcing" and "cannot tell" kept deliberately distinct. `Settings` refuses to construct when the build claims ENFORCING while unresolved scope is still allowed through; startup fails if any ACTIVE workspace is not ENFORCE. **Defaults deliberately unchanged** (OBSERVING / SHADOW), pinned by a test, per the review's own instruction that tightening must be an intentional migration |
| F12 | P1 | Failed governance notifications permanently considered processed | ☑ | Intent created inside the business transaction; delivery and attempts recorded by an independent worker; `requested_at` / `attempted_at` / `delivered_at` separated. The watermark now means "an intent exists", not "a send happened". Commit-lifecycle bug fixed: callers already committed get their staged evidence committed, callers mid-transaction keep their own commit — both cases tested. Dedup is enforced by the worker, not a UNIQUE constraint, so a duplicate cannot abort the business transaction that staged it. At-least-once, documented |

## 2. Product and reliability defects (REVIEW.md §2)

| ID | P | Finding | Status | Evidence / what is actually true now |
|---|---|---|---|---|
| F13 | P1 | Shell always claims it is live and connected | ☑ | Seven states driven by real request outcomes: demo / connecting / connected / degraded / disconnected / session-expired / forbidden, with last-success time. Demo is violet, not green. Reconnect on degraded and disconnected; sign-in on expired; no retry offered on forbidden, because retrying a 403 changes nothing |
| F14 | P2 | Error handling discards structured errors and correlation IDs | ☑ | One decoder for every verb, preserving `code`, `correlation_id` (body or `X-Correlation-Id` header), 422 field errors and a `retryable` flag. Nothing retries automatically; 409 is explicitly not retryable, because a lost governance race must not be resubmitted blindly |
| F15 | P1 | Source/workspace pickers silently operate on capped first pages | ◐ | Client half done: bounded paging with an explicit budget, reported `total`/`truncated`, and deep-linked ids resolved by a bounded scan. **Server gap recorded, not papered over:** none of the four list routes has a `q=` parameter and there is no fetch-by-id route for organization/workspace/project/datasource, so search is client-side over loaded rows and labelled as such on screen. Closing it server-side turns a scan into one request |
| F16 | P2 | Review-queue composition still contains an N+1 query path | ☑ | Diff composition batched; measured **3 statements for 1 review and 3 for 16**, counted at the DBAPI cursor. New summary endpoint is exactly **1 statement** regardless of size, and Overview uses it instead of fetching 1,000 reviews for a count |
| F17 | P2 | Unmatched URL paths produce unbounded metric labels | ☑ | Normalized to `__unmatched__`; test walks 40 distinct unknown URLs and asserts exactly one added series. Real path retained in logs |
| F18 | P2 | Readiness can report stale Temporal availability | ☑ | Bounded, concurrent probes under an explicit timeout. Required (gates 503): PostgreSQL only. Optional (reported, never gating): Temporal, archive task, reconnect task, outbox backlog. Temporal is a real health RPC; where the client cannot provide one it degrades to an existence check **and says so** rather than claiming a check it did not do. **Behaviour change:** a Temporal outage no longer returns 503 — intended by F18, but it changes what an orchestrator does during that outage |
| F19 | P1 | Principal-leaver reconciliation has no production entry point | ☑ | Replay-safe pass wired to the fleet scheduler, **off by default**. Replay safety is a property of the handlers (they select only ACTIVE rows still owned by the departed principal), proven four ways including the real production sequence. Tenant scoping, merge-target collision and duplicate events tested |
| F20 | P1 | Ask Atlas presents metadata but not the returned result rows | ☑ | Bounded result panel: sticky-header table capped at 200 rows with an escape, per-column masked/derived tags, masked cells as a chip rather than the sentinel, empty state, truncation banner. **No result retention added** — rows render only while the session holds the response; a run reopened from history says so. **Honest gap on screen:** the response carries no `applied_row_limit`, so truncation is *inferred* from the SQL's `LIMIT` |
| F21 | P2 | Browser resilience and modal accessibility need shared infrastructure | ☑ / ⚠ | Route error boundary distinguishing a failed chunk (reload) from other errors (retry), printing the correlation id. Real `Dialog` with focus trap, focus restore, Escape and background inertness — replacing `window.prompt` on the screens being fixed. `AsyncState`, `Toast`, `ConfirmDialog`, `FormErrors`, unsaved-edit protection. Lineage graph gained a table alternative; virtual list gained keyboard traversal. ⚠ asserted under jsdom only — **not validated in a real browser with a screen reader**, so no conformance claim is made |
| F22 | P2 | Consumer/Developer navigation inconsistent with identity UX | ☑ | Persona (identity, from `/me`) and work area (navigation) separated into distinct vocabularies; Consumer is reachable; onboarding progress keyed by organization **and** principal rather than persona alone |

## 3. Refactoring (REVIEW.md §3)

| ID | Item | Status | Note |
|---|---|---|---|
| R01 | Finish the modular monolith before service extraction | ◐ | Real progress by use case: governance decisions, lineage/graph bounds, knowledge-graph neighbourhood, portfolio analytics and retrieval/orchestration stages all moved from routers into services. Not finished — this is multi-pass work by design |
| R02 | Split high-risk functions by invariants | ☑ | **All six hotspots done.** `_apply_governance_review_decision` 921 → ~12-line wrapper over a service plus 25 target adapters. `agent_orchestrator.run` and `retrieval.hybrid_retrieve_enhanced` split into named stages with per-stage metrics. `_build_unified_graph` 465 → 0 in the router. `get_knowledge_graph_neighborhood` 455 → 63. `portfolio_analytics_summary` 407 → 25. Two of these had **zero behavioural coverage** beforehand; characterization tests were written against the unmodified handlers first |
| R03 | Remove schema/service and router/router import cycles | ☑ | A Tarjan SCC pass over the whole `aida` import graph reports **zero multi-module cycles**. The `api → org → api` cycle in the frontend is gone too. Two import-linter contracts added to keep the new boundaries enforced |
| R04 | Reduce centralized ORM/schema coupling | ⊘ | Deferred deliberately. `models.py`/`schemas.py` remain large; relocating a bounded context at a time is its own reviewable change and this pass already touches most of the tree |
| R05 | Split the frontend API client by domain | ◐ | Partial: transport, identity, glossary, cross-source and column-documentation extracted behind an unchanged barrel. **It was a correctness fix, not cosmetics** — all three `_*_api.ts` modules had copied their own transport, so the F06 header rule and F14 correlation ids never reached those calls, and each reinstated the `api → org → api` cycle. Catalog/governance/quality/agents/products remain in `api.ts` |
| R06 | Break large screens into workflow sections | ◐ | Deliberately limited to screens already being changed for a correctness fix, per the review's "correctness first, not a visual rewrite". Agent gateway 844→707 and Business meaning 990→741, each with an extracted module. Transformations, Context products, Tool registry, Administration and Workspace access left alone |
| R07 | Deduplicate verified copies | ☑ | Six inline URL-state hooks removed. **Zero cross-file exact-AST duplicate function bodies remain in `src/aida`** (re-ran the review's own scan). Both pairs the review named are consolidated, plus three groups it did not: `_project_scope` ×3 and `_load_datasource` ×3 — both **tenant-authorization** helpers, where a drifting copy is a route that has quietly stopped enforcing the org boundary — and the connector row mapper ×2 |

## 4. Hygiene (REVIEW.md §4)

| ID | Item | Status | Note |
|---|---|---|---|
| D01 | Safe cleanup candidates after usage verification | ☑ | `ProposalCard` traced properly (literal, dynamic, tests, stories, docs): component dead, **CSS live** (`ReviewQueueScreen` renders those classes), types reachable only from a fixture with no consumer. Component and fixture chain deleted; CSS kept and renamed to match what actually renders it. `OrgPicker` left in place with its status recorded rather than deleted on import count alone. `injection_corpus.py` explicitly kept (offline eval tooling) |
| D02 | Inactive feature code should be explicitly owned | ☑ | Principal lifecycle wired with owner/default/production-eligibility/retirement recorded in the module docstrings. `orders_raw` kept and made **visibly** demo-only: a "worked example · not your data" pill, a dashed border, and the disclaimer in the section's accessible name |
| D03 | Do not remove compatibility shims prematurely | ⊘ | Deliberately unchanged. Shims are migration seams; each needs an owner and a removal condition, which is a separate change |
| D04 | 60 tracked Vitest timestamp artifacts | ☑ | Ignore rule added beside the existing Vite one **and** all 60 untracked with `git rm --cached` — the ignore alone does not untrack |
| D05 | Legacy UI retirement | ☑ | Already removed. Remaining references cleaned from docs; the obsolete `tests/test_ui_accessibility.py`, whose subject no longer exists, removed — its guarantees are covered for the React app by `primitives.test.tsx` and `tokens.css`. History documents citing it are handled by a documented, staleness-guarded baseline rather than by rewriting history |
| D06 | Documentation truth drift | ☑ | Both broken README links fixed; a link checker over 199 markdown files added as a CI job. Status doc corrected against code (ADR count, portal state, fixture default). Connector prose corrected: **six** BETA registrations, not two, with the distinction that BETA ≠ met a real source. New capability register with implemented / reachable / configured / verified as four separate columns, conservative by default |

## 5. Operational improvements (REVIEW.md §5)

| Area | Status | Note |
|---|---|---|
| Graph projection memory bounds | ☑ | Chunked/incremental projection with deletion reconciliation and lag metrics, plus a measurement harness so the improvement is a number rather than a claim |
| Outbox delivery vs. Kafka atomicity | ⊘ | Deferred pending contention measurement, per the review's own framing |
| Review pages: summary/list/detail split | ☑ | Summary read model landed and measured page-size-independent; p95 budgets still ⊘ |
| Global scope discoverability | ◐ | F15 — client half done, server `q=`/fetch-by-id gap recorded |
| Tenant fairness / per-tenant budgets | ☑ | Conservatively-defaulted, documented budget setting plus an oldest-backlog metric, so the policy can later be chosen from data rather than invented now |
| Connection pool math | ☑ | Documented per process type against the database limit, stating the formula and the inputs an operator must supply rather than inventing replica counts |
| Search/retrieval stage metrics | ☑ | Per-stage candidate counts, latency, mean score and skip reason for lexical / vector / graph / trust / fusion / evidence; candidate set explicitly bounded |
| Observability: success vs. configuration | ☑ | Archive receipts, delivery outcomes, readiness last-success and enforcement posture — the places the review named |
| Retention / legal hold | ◐ | Legal hold executes against the provider and persists state. Broader asset retention ⊘ pending security requirements |
| Disaster recovery | ⚠ | Needs a deployed topology and an RPO/RTO owner. T28 |

## 6. Security and AI governance (REVIEW.md §6)

| # | Item | Status | Note |
|---|---|---|---|
| 1 | Surface-to-control matrix | ☑ | **Generated from the app**, not hand-written, and re-runnable. Cells it cannot determine are emitted as `unknown` rather than guessed — the honest gap list is the useful output |
| 2 | Separate persona / roles / membership / delegation / agent identity | ◐ | F22 separates persona from work area in the UI vocabulary; the code contracts remain partly conflated |
| 3 | One decision service for human, delegated, bulk and automated approval | ☑ | F05. **A latent bug surfaced here:** the reviewer agent passed a terminal status where a verdict was expected, and the old expression read anything unrecognised as a rejection — so every reviewer-agent auto-approval was actually rejecting the proposal and reporting success. No test covered it. Now raises on a non-verdict, with a regression test |
| 4 | Verify direct IDs, links, projections and exports enforce list-page policy | ◐ | Improved concretely by R07's consolidation of the two tenant-scope helpers and by policy tests on the extracted lineage/neighbourhood/portfolio paths (cross-source grants, wrong-direction grants, cross-organization candidates, refusal indistinguishable from absence). Full authorization certification remains separate work |
| 5 | Classify model/integration output as untrusted until validated | ⊘ | Already partly present; no change this pass |
| 6 | Evaluation quality on representative tasks | ⊘ | Deferred. The deterministic quality benchmark is a regression gate, not answer-quality proof |
| 7 | External destination and credential inventory | ◐ | Archive, SIEM and notification destinations now have explicit configured/unavailable/delivered state; no unified inventory yet |
| 8 | Confirm real destination enforcement | ⚠ | Blocked on real destinations. This is the point F01/F04 are honest about rather than papering over |

## 7. Build, CI and developer experience (REVIEW.md §7)

| Item | Status | Note |
|---|---|---|
| Production-proxy contract check (`/mcp`) | ☑ | Static contract check **and** a live job against the real built image, including the pre-fix negative case |
| Fresh-browser authentication check | ⚠ | Needs a deployed topology and an IdP |
| Deep-link correctness checks | ☑ | Covered by the route-table and location-store suites |
| Archive/delivery receipt gates | ☑ | Archive read-back verification; delivery receipt against real loopback servers |
| Review-decision concurrency gate | ☑ | File-backed SQLite with two real connections; verified to fail without the guard |
| Reachability review across both namespaces | ◐ | Backend gate green and two stale allow-list entries removed; frontend import reachability still manual |
| Migration execution against PostgreSQL | ☑ | ORM-drift gate ran against a real PostgreSQL, not skipped |
| Frontend dependency scanning | ⊘ | Not addressed this pass |
| SDK packaging alignment | ☑ | **The defect was worse than described:** the image built fine but `import aida_tool_sdk` raised `ModuleNotFoundError`, because hatchling wrote `.pth` entries for the roots it could see and silently skipped the missing one. Fixed, plus a manifest/`COPY` consistency check and a runtime import smoke test in the built image |
| Domain READMEs / generated architecture map | ◐ | Capability register and generated surface-control matrix landed; per-domain READMEs not written |

## 8. UX and journeys (UX-AND-JOURNEYS.md)

| Item | Status | Note |
|---|---|---|
| §2 Navigation rearrangement into six work areas | ◐ | Personas and work areas separated (F22), every area reachable. The full six-area regrouping is now **unblocked** (it depended on T11, which landed) but not done — it is a product change, not a correctness fix |
| §2 Consolidate overlapping destinations | ⊘ | Deferred. Screen-id aliases must be preserved; a large move is its own reviewable change |
| §3 Reusable screen arrangement + precise state labels | ☑ | Delivery / authorization / content / execution state families reflected in the new primitives and the session badge |
| §4 Per-route improvements (40 routes) | ◐ | The confirmed defects among them are done (Ask results, gateway endpoint, permalinks, Overview counts). The rest are product work |
| §5 Persona journeys A–G | ⊘ | Journey completeness is the T15/T16 milestone, not this correctness pass |
| §6 New functionality | ◐ | Only "policy-aware answer results" (F20) was in scope |
| §7 Accessibility and interaction | ☑ / ⚠ | Dialog focus management, keyboard traversal, graph table alternative, unsaved-edit handling. ⚠ no conformance claim — not validated interactively |
| §8 Measurement baseline | ⊘ | Deferred; needs telemetry decisions with a retention policy |

## 9. Roadmap task mapping (ROADMAP.md)

| Task | Covered by | Status |
|---|---|---|
| T01 Canonical serialization + checksum versioning | F02 | ☑ |
| T02 Real archive provider with receipts | F01 | ☑ filesystem; ⚠ cloud |
| T03 Idempotent archive membership | F03 | ☑ |
| T04 Durable SIEM delivery | F04 | ☑; ⚠ real collector |
| T05 Shared atomic review transition | F05 | ☑; ⚠ PostgreSQL concurrency reproduction |
| T06 Durable notification intents | F12 | ☑ |
| T07 Complete browser authentication | F06 | ◐; ⚠ IdP |
| T08 Proxy `/mcp` in production | F07 | ☑ |
| T09 Workspace enforcement readiness checks | F11 | ☑ |
| T10 Honest demo/live/session states | F13 | ☑ |
| T11 Typed routes, canonical links, one location store | F08/F09 | ☑ |
| T12 Atomic scoped client/query state | F10 | ☑ |
| T13 Searchable paginated pickers | F15 | ◐ server gap |
| T14 Bounded approved result grid | F20 | ☑ |
| T15 Resumable first-source setup | — | ⊘ Phase 2 product work |
| T16 Reorganize navigation and task handoffs | — | ⊘ Phase 2; now unblocked |
| T17 Shared async/error/dialog primitives | F21 | ☑ |
| T18 Common review detail | — | ⊘ Phase 2 |
| T19 Principal lifecycle source | F19 | ☑ |
| T20–T27 Phase 3 | R01–R07, D04–D06 | ◐ / ☑ per row above |
| T28 Production certification | — | ⚠ Needs a deployable target topology |

## Deliberate non-goals for this pass

Recorded so they are decisions rather than omissions:

- **The legacy `ui/` portal is deleted, not fixed.** Explicitly approved.
- **No default is flipped to a stricter posture.** F11's OBSERVING/SHADOW defaults are unchanged; the SIEM and delivery workers default to sending nothing; principal reconciliation defaults to off; the archive defaults to a provider that refuses. The review is explicit that tightening must be an intentional migration — this pass makes each posture explicit, checkable and visible instead. The one default that *did* change is the archive backend, from `s3` (which never uploaded) to `none` (which refuses): that is a correction, not a tightening.
- **No cloud storage client was added.** An unimplemented provider says so.
- **No migration rewriting or baseline regeneration to make a check green.** The OpenAPI baseline was accepted only after confirming every change is classified additive.
- **No new business-value retention** was added to improve history or analytics.
- **No wholesale visual redesign.** The review states it would not fix the interaction defects found.
- **Compatibility shims kept** (`aida.db`, `aida.config`, models/schemas re-exports, route aliases). They are migration seams with consumers.
- **`ruff format` left red repo-wide.** Not a CI gate; reformatting a third of the repository would bury this diff.

## What still needs a real environment

Nothing below can be closed from this machine. They are the review's own §5 list, narrowed to what remains:

- A real OIDC issuer, to finish F06's redirect and refresh.
- A real archive destination with object-lock, to make F01 a verified capability rather than a verified implementation.
- A real SOC collector, for F04.
- A concurrent PostgreSQL reproduction for F05.
- A deployed topology, for fresh-browser auth, the full JSON-RPC path through nginx, disaster recovery and T28.
- A browser with a screen reader, before any accessibility conformance is claimed.
