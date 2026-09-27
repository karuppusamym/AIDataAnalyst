# Atlas / AIDataAnalyst — comprehensive project review

> **Scope reconciled 2026-09-11.** [Tracker section P](../60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) owns current execution status; [reconciliation decisions](../60-delivery/23-review-reconciliation-2026-09-11.md) map the remaining work. This document retains design and dated evidence. Older priorities, partials and recommendations do not form a separate queue.

Review date: 5 September 2026. Initial source baseline: `3799457`. Workspace revision on resumption: `e4a3cfb`. This report restores the review documents referenced by the existing [remediation tracker](POINTS-TRACKER.md).

**Status convention:** findings below describe the inspected review baseline. The workspace advanced during the review, and the tracker records subsequent remediation plans/activity. A tracker entry is not independent proof that a fix is complete. The restored report does not overwrite that tracker or claim to have re-certified all subsequent changes. Source line numbers refer to the initial snapshot and can shift.

## Read this first

Atlas has a substantial governed-data foundation. Preserve its strongest ideas: deterministic authorization and SQL execution, models that propose rather than authorize, independent review, attributable evidence, versioned products, and rebuildable projections.

The next investment should be **finishing reliable user journeys and making capability claims match observable behavior**. The project already has 40 navigation routes and approximately 121,000 backend source lines. Adding more standalone screens before fixing continuity would increase complexity.

The most important findings are specific:

1. WORM archive success is recorded without an external archive write, and its checksum protects only a small portion of audit content.
2. Bulk review decisions omit the concurrency protection used by single-review decisions.
3. The supplied browser client does not complete the backend's OIDC authentication contract.
4. Many copied links omit the screen route, breaking sharing and returning to work.
5. SIEM delivery reports success without transport; failed governance notifications can also be considered processed.
6. Workspace authorization has a permissive migration posture that is not prohibited by production settings validation.
7. Modularization has moved files, but substantial behavior remains in large routers with cross-layer dependencies.

This is a promising engineering platform with incomplete production integration and product coherence. A successful local build does not resolve those gaps.

### Review package

| Document | Contents |
|---|---|
| This report | 22 findings, refactoring, dead-code candidates, architecture, security, performance, delivery |
| [Screens and journeys](UX-AND-JOURNEYS.md) | All 40 routes, navigation rearrangement, seven persona journeys, storytelling, new functionality |
| [Implementation roadmap](ROADMAP.md) | 28 sequenced work packages with dependencies and acceptance criteria |
| [Coverage and verification](COVERAGE.md) | Measurements, exclusions, checks actually run, limitations |
| [Existing remediation tracker](POINTS-TRACKER.md) | Subsequent implementation status; maintained separately from this review |

### Evidence and priorities

- **Confirmed:** directly supported by inspected source or a completed local check. This does not automatically mean a live incident was reproduced.
- **Risk:** source permits a problematic condition; target deployment or concurrent execution must establish its occurrence.
- **Recommendation:** an improvement, not a claim that existing functionality is absent.
- **Candidate:** verify consumers/usage before deleting or declaring the feature unused.

**P0** blocks a release promising the affected safety capability; **P1** precedes broader rollout; **P2** improves maintainability or recurring workflows; **P3** is optional expansion. These are delivery priorities, not vulnerability severity scores.

## 1. Highest-priority engineering findings

### F01 — WORM archival reports completion without storing the archive

**P0 · Confirmed implementation gap.** Evidence: [worm_archive.py](../../src/aida/worm_archive.py), `archive_audit_events` at line 78 and `archive_pending_audit_events` at line 122.

The archive function calculates a checksum, logs `audit_events_archived`, and returns an `ArchiveResult`. There is no S3/GCS/Azure upload, immutable object write, or destination acknowledgment. The caller persists an `AuditArchiveRecord` describing the purported archive. `bucket_name` does not cause an upload. Legal-hold helpers log and return dictionaries rather than applying provider retention or durable hold state.

**Impact:** operators can see archive evidence without an externally retained event payload. Database metadata about archival does not establish immutable storage.

**Fix:** separate PREPARED, UPLOADED, VERIFIED, and FAILED. Implement one storage provider completely before advertising three. Persist object version/location, serialization/hash version, retention acknowledgment, and verification time. Advance progress only after acknowledged storage. Until then, describe the capability as archive preparation.

**Acceptance:** retrieve and verify the destination object; exercise overwrite/deletion restrictions and legal hold against the configured service; failed upload produces a retryable failure, not an archived count.

### F02 — Archive integrity excludes the fields auditors need protected

**P0 · Confirmed with an isolated executable reproduction.** Evidence: [worm_archive.py](../../src/aida/worm_archive.py), `_compute_checksum`, line 62.

The hash covers only event ID, action, and timestamp. Changing organization, principal, resource, or details leaves it unchanged. The review executed the actual extracted definitions with two different synthetic envelopes and obtained the same checksum. The envelope also omits some source fields, including outcome.

**Fix:** version a canonical serialization of the complete intended audit envelope: tenant, actor, resource, outcome, correlation, timestamp, and details. Hash canonical/length-delimited bytes rather than concatenating a few values. Preserve legacy verification while labeling its narrower coverage.

**Acceptance:** changing any protected field invalidates verification; order/encoding are deterministic; added fields cannot silently escape the contract.

### F03 — Archive progress can permanently skip events

**P1 · Confirmed algorithm defect; production incidence unmeasured.** Evidence: [worm_archive.py](../../src/aida/worm_archive.py), line 154; archive loop in [main.py](../../src/aida/main.py), line 111.

The next batch uses `occurred_at > last_event_range_end`, orders only by timestamp, and limits results. If the batch boundary splits equal-timestamp events, the remainder is never selected. Late commits carrying older timestamps present another omission case. Every API replica starts its archive loop without a visible per-organization claim/leader lock.

**Fix:** explicit event membership and idempotent archive manifests, with per-organization leases or durable workflow ownership. A composite `(occurred_at, id)` cursor fixes equal-timestamp pagination but does not alone solve late arrivals. Add an ingestion sequence or overlap/reconciliation strategy.

**Acceptance:** equal-timestamp batches, late commits, crash-after-upload, and simultaneous workers produce no omitted event or ambiguous archive membership.

### F04 — SIEM routing returns success without delivering

**P0 when SIEM integration is promised · Confirmed.** Evidence: [siem_routing.py](../../src/aida/siem_routing.py), line 113; callers in [security.py](../../src/aida/security.py) and [events.py](../../src/aida/events.py).

Both configured transport branches format a message, write a local log, and return `True`; neither sends the advertised webhook/syslog message. Although the docstring calls it a stub, the return value and routed log imply delivery. `include_details` also does not suppress webhook details in that implementation.

**Fix:** durable security-delivery records, asynchronous transport, destination acknowledgment/failure, retry/deduplication, and honored minimization settings. Use accurate unavailable/disabled states until transport exists.

**Acceptance:** destination receipt is demonstrated; timeout/retry paths work; disabled and delivered are distinguishable; configured detail suppression is enforced.

### F05 — Bulk governance decisions can race with another decision

**P1 · Confirmed missing guard; concurrent PostgreSQL reproduction remains required.** Evidence: [semantic_api.py](../../src/aida/semantic_api.py), single decision at line 2372, bulk at line 2514, shared mutation at line 1448.

Single review uses `SELECT ... FOR UPDATE` before checking PENDING. Bulk loads ordinary ORM objects and checks their in-memory status. Savepoints isolate failed-item writes but do not protect stale reads. No ORM version column or serializable isolation was found for this model.

**Scenario:** two checkers load one pending review; one approves while the other rejects. Both can act on previously read state. Some target-specific checks may prevent particular outcomes, but the shared review transition does not consistently enforce one winner.

**Fix:** one application service claims/reloads current review state for single, bulk, and automated decisions. Use ordered row locks or compare-and-set versioned transitions. Retain per-item savepoints for partial-success semantics.

**Acceptance:** bulk/bulk and single/bulk contention produce one terminal decision and one set of side effects; the losing caller receives conflict/refreshed state. Apply the same invariant to reviewer automation.

### F06 — Browser OIDC integration is incomplete

**P1 · Confirmed in supplied client/proxy; an external auth proxy could change deployment behavior.** Evidence: [api.ts](../../ui-next/src/lib/api.ts), identity headers at line 184; [security.py](../../src/aida/security.py), line 62; [nginx.conf](../../ui-next/nginx.conf).

Live client requests send development headers, while OIDC backend mode explicitly requires a bearer token. The inspected shell has no login/callback, token acquisition/refresh, or bearer injection. `credentials: same-origin` does not satisfy a backend that reads the bearer header only. Supplied Nginx does not bridge that contract.

**Fix:** choose a supported browser OIDC flow or server-side session/authentication proxy. Add session loading, expiry, forbidden, logout, and reauthentication states. Make development mode explicit.

**Acceptance:** fresh-browser sign-in works through the actual topology; expiry/logout behave correctly; Viewer and Reviewer accounts work without the all-role development principal.

### F07 — The copied MCP endpoint fails in the production UI topology

**P1 · Confirmed configuration mismatch.** Evidence: [AgentGatewayScreen.tsx](../../ui-next/src/screens/AgentGatewayScreen.tsx), line 83; [nginx.conf](../../ui-next/nginx.conf); [vite.config.ts](../../ui-next/vite.config.ts).

The screen advertises `${location.origin}/mcp`. Vite correctly proxies `/mcp` in development. Production Nginx proxies only `/v1/`, leaving `/mcp` to frontend handling rather than FastAPI.

**Fix:** add the production MCP upstream route; centralize the public endpoint configuration; add an authenticated read-only diagnostic to the connection screen.

**Acceptance:** initialize/list permitted tools through the exact URL copied from the production-built UI, with correct JSON-RPC and authorization behavior.

### F08 — Copied permalinks omit their screen

**P1 · Confirmed.** Examples: [EvidencePane.tsx](../../ui-next/src/components/EvidencePane.tsx), line 90; [AskScreen.tsx](../../ui-next/src/screens/AskScreen.tsx), line 206; [ReviewQueueScreen.tsx](../../ui-next/src/screens/ReviewQueueScreen.tsx), line 468.

Screens are selected by `#/screen`, but many copy actions construct only origin, pathname, and selection query. Fresh tabs therefore land on Overview/persona default rather than the intended object. The pattern also appears in audit, marketplace, refusals, quality, relationships, sources, semantics, and Studio.

**Fix:** one typed link builder owns route, allowed query fields, selected object, and context. Include the screen hash. Validate tenant/workspace context on load; URL data is never authorization.

**Acceptance:** all copied links reopen the same authorized object, including after sign-in; missing/inaccessible objects and clipboard failures have clear feedback.

### F09 — URL state can drift from rendered state

**P1 · Confirmed limitation.** Evidence: [useUrlState.ts](../../ui-next/src/lib/useUrlState.ts), line 11; [App.tsx](../../ui-next/src/App.tsx), line 274; six inline hook copies.

The hook initializes from `location.search` but does not subscribe to popstate or same-screen navigation. Shell state/key uses the route ID; parameter-only navigation need not remount the screen. Emitting hashchange does not update the hook's private state. Shell navigation also carries previous-page parameters when none are supplied.

**Fix:** one router/location store, typed per-screen query schemas, and one navigation API. Persist scope intentionally and discard unrelated filters. Replace inline hook copies.

**Acceptance:** same-screen links, Back/Forward, shared links, and drilldowns render the record/filter stated in the URL.

### F10 — Organization/scope state updates in separate phases

**P1 · Risk with source support; no cross-tenant disclosure reproduced.** Evidence: [org.tsx](../../ui-next/src/lib/org.tsx), lines 56/76; [scope.tsx](../../ui-next/src/lib/scope.tsx), lines 63/95/145.

The organization header is read from a module-global mirror updated in an effect, while paths use React context. Old scope arrays, workspace selection, and bindings remain until separate requests/effects finish. Persistence effects can write previous selections under a newly chosen organization key.

**Fix:** stable explicit request context; immediate old-scope invalidation; query/page keys containing organization/workspace; atomic adoption of validated selections; writes disabled until scope resolves. Bindings need a tenant-aware lifecycle too.

**Acceptance:** out-of-order responses during rapid tenant switching cannot populate the wrong view or drive stale-scope writes. Backend enforcement remains mandatory. Recheck this finding against the newer org-context module before coding another fix.

### F11 — Workspace authorization can remain SHADOW in production

**P1 · Confirmed posture; actual deployment must be checked.** Evidence: [config.py](../../src/atlas/platform/config.py), line 565 and production validation at 821; [authorization_gate.py](../../src/aida/authorization_gate.py).

An unresolved workspace defaults to SHADOW: log and return an undecided outcome without denial. Production settings validation rejects several insecure choices but not this posture. Resolved workspaces can also have shadow/enforcement modes.

This does not mean all authorization is missing: organization, role, SQL, and other controls exist. It means successful production configuration validation is not proof of workspace enforcement.

**Fix:** explicit deployment/control-health checks. For a release promising enforcement, require DENY for unresolved workspaces and verified ENFORCE for relevant workspaces. First complete context propagation and examine shadow divergences.

**Acceptance:** unresolved/ambiguous scope fails as intended; authorized journeys still work; operators can distinguish enforcing from observing controls.

### F12 — Failed governance notifications can be considered processed

**P1 · Confirmed path.** Evidence: [governance_review_relay.py](../../src/aida/governance_review_relay.py), lines 139–150; [governance_notifications.py](../../src/aida/governance_notifications.py), line 283.

`notify_safely` returns no delivery outcome and catches errors. The relay stamps `review_requested_notified_at` anyway, and later sweeps select only unstamped reviews. Short in-call retries do not provide durable outage recovery. No durable retry consumer for those failures was found in the inspected paths.

Related persistence gap: single review decisions commit, then call notification logic that stages notification/audit rows, without another commit before return. The ordinary dependency yields/closes its session rather than committing automatically. A network send can therefore lack its staged database evidence.

**Fix:** create a notification intent in the business transaction; deliver and record attempts in an independent worker. Separate requested/attempted/delivered timestamps; add destination/channel deduplication and backoff.

**Acceptance:** destination outage/recovery eventually delivers, attempts are durable, and business decisions are independent of transport availability.

## 2. Product and reliability defects

### F13 — The shell always claims it is live and connected

**P1 · Confirmed.** [App.tsx](../../ui-next/src/App.tsx), lines 226/331/352. Platform connected and Live are static; `/me` errors are discarded. Fixture mode defaults on unless explicitly `0` in the client, while Docker explicitly defaults it to `0`.

Use configuration-backed demo/live/session states plus recent successful requests. Show degraded/disconnected/expired states and last successful update. A green shell should not imply every subsystem is healthy.

### F14 — Error handling discards structured server errors and correlation IDs

**P2 · Confirmed.** [api.ts](../../ui-next/src/lib/api.ts), line 189 onward; [main.py](../../src/aida/main.py), line 375.

The wrapper understands `detail`, but middleware emits `error: {code, message, correlation_id}` and a correlation response header. Useful troubleshooting context is reduced to a status string. Success JSON is cast to `T` without runtime validation.

Create one decoder for all verbs that preserves safe message, code, request ID, field errors, and retry guidance. Generate/validate critical response boundaries. Do not automatically retry non-idempotent writes.

### F15 — Shared pickers use capped first pages

**P1 at enterprise scale · Confirmed.** [api.ts](../../ui-next/src/lib/api.ts), lines 674/687/1227; [scope.tsx](../../ui-next/src/lib/scope.tsx).

Sources/projects are requested with limit 500, workspaces/organizations with 200, then `.items` is used without a common search-beyond-page or truncation workflow. Valid resources beyond the cap can become unselectable.

Use server search, pagination, and fetch-by-ID for current/deep-linked values. Show totals/truncation. Do not preload the whole estate as the solution.

### F16 — Review composition contains an N+1 query path

**P2 · Confirmed query shape; latency unmeasured.** [review_queue_read_model.py](../../src/aida/review_queue_read_model.py), lines 223/302; [semantic_api.py](../../src/aida/semantic_api.py), line 1356.

Confidence/evidence queries are batched, but every review calls a database-backed diff composer. Semantic/glossary snapshots add reads per item. The complete endpoint therefore is not page-size-independent. Overview requests up to 1,000 reviews merely to show a count.

Add a lightweight aggregate summary; defer detailed evidence or batch snapshots by type. Expand structured diffs beyond the two supported types, prioritizing tools, access, model routes, and context products.

### F17 — Unmatched paths generate unbounded metric labels

**P2 · Confirmed.** [main.py](../../src/aida/main.py), line 404. With no route template, the metrics path label falls back to the raw URL. Arbitrarily distinct 404 URLs create distinct metric series.

Normalize unmatched paths to a constant; retain actual paths only in appropriately controlled logs. Verify that many different unknown URLs do not grow series count proportionally.

### F18 — Readiness can report stale Temporal availability

**P2 · Confirmed limitation.** [main.py](../../src/aida/main.py), line 426. PostgreSQL is actively queried, but Temporal is considered up when its startup client object exists. That is not a current connectivity check. Background task health/projection lag are not represented there.

Keep liveness simple; define required readiness dependencies by service responsibility. Use bounded probes and last-success/lag metrics. Report optional subsystem failures separately rather than turning each into a universal outage.

### F19 — Principal-leaver reconciliation has no discovered production trigger

**P1 · Integration candidate.** [identity_events.py](../../src/aida/identity_events.py), line 36; [ownership_principal_lifecycle.py](../../src/aida/ownership_principal_lifecycle.py), line 63.

The handlers contain useful reconciliation, but their emitters were unreachable from the five inspected process entry points. Source/scripts/SDK searches found mutual references rather than an IAM consumer or mounted administrative trigger. A docstring describes an outbox consumer not found in those paths.

Treat this as unfinished integration, not deletion material. Connect an authenticated replay-safe identity event source or authoritative scheduled reconciliation. Verify tenant, merge-target, duplicate-event, and reassignment rules.

### F20 — Ask Atlas omits the returned result rows

**P1 · Confirmed screen gap.** [AskScreen.tsx](../../ui-next/src/screens/AskScreen.tsx), lines 267–312; `QueryExecutionResponse` in [schemas.py](../../src/aida/schemas.py), line 627.

The response supports `rows`, but the inspected view renders count, timing, tables, SQL, explanation, and evidence without an `execution.rows` result grid. History intentionally does not retain the fresh explanation/results.

Add a bounded policy-aware current-result panel with units, dimensions, freshness, masking, limits, and suitable charts. Distinguish a saved value-free analysis definition from stored results; rerun under current policy where necessary. Do not introduce result retention incidentally to improve history.

### F21 — Resilience and modal accessibility need shared infrastructure

**P2 · Structural gaps; interactive validation pending.** [App.tsx](../../ui-next/src/App.tsx), line 381; [main.tsx](../../ui-next/src/main.tsx); [primitives.tsx](../../ui-next/src/components/primitives.tsx).

Lazy routes use Suspense without a discovered route error boundary. The command palette declares a modal with autofocus/Escape, but has no corresponding focus containment/restoration/background inertness in the component. ARIA alone does not establish working interaction.

Share dialog/drawer, async/error state, toast, confirmation, field errors, and route-failure handling. Add route focus, skip navigation, clipboard failure feedback, and unsaved-work protection. Validate interactively before claiming accessibility conformance.

### F22 — Consumer/developer work areas and persona UX are inconsistent

**P2 · Confirmed mismatch.** [App.tsx](../../ui-next/src/App.tsx); [ui-types.ts](../../ui-next/src/lib/ui-types.ts), line 109; [PersonaNav.tsx](../../ui-next/src/components/PersonaNav.tsx); [OnboardingWizard.tsx](../../ui-next/src/components/OnboardingWizard.tsx).

Navigation contains Consumer/Developer work areas, but the persona union/switcher/onboarding has only Analyst, Steward, Reviewer, Operator, Auditor. The Consumer landing-map entry cannot be chosen through that union. Development persona switching changes presentation, not all-role API identity.

Define personas versus work areas explicitly; share the recognized identity vocabulary; allow multiple work areas per user. Test least-privilege accounts. Scope onboarding progress by principal and organization, rather than persona alone in local storage.

## 3. Refactoring strategy

### R01 — Finish the modular monolith before extracting services

`src/aida` contains 111,879 lines versus 9,420 under `src/atlas` in the inventory. Five bounded-context directories exist, but several service/repository files remain scaffolds while routers hold substantial logic. File movement alone is not a service boundary.

Move behavior by transaction/use case: governance decisions; workspace authorization; catalog/diff read models; connector/ingestion lifecycle; delivery/observability. Routers should translate input/output, application services own transactions, and repositories expose tenant-scoped operations. Preserve one SQL execution authority.

### R02 — Split large functions by invariant and state transition

| Hotspot | Initial lines | Useful boundaries |
|---|---:|---|
| `semantic_api._apply_governance_review_decision` | 921 | Shared claim/preconditions; target-specific adapters; common audit/outbox result |
| `agent_orchestrator.run` | 832 | Screen, retrieve, plan, validate, execute, explain; typed stage inputs/outputs |
| `retrieval.hybrid_retrieve_enhanced` | 578 | Authorized candidates; lexical/vector/graph providers; fusion; trust; evidence |
| `unified_lineage_api._build_unified_graph` | 465 | Graph service, edge providers, policy, bounds, response composition |
| `intelligence_api.get_knowledge_graph_neighborhood` | 455 | Neighborhood service and projection adapter |
| `product_marketplace_api.portfolio_analytics_summary` | 407 | Aggregate queries and portfolio read model |

Do not extract helpers merely to reduce line count. Aim for independently understandable rules and one authoritative implementation per invariant. Use meaningful characterization/concurrency checks when implementing, despite excluding test sources from this review.

### R03 — Remove schema/service and router/router dependency cycles

Static analysis found an eight-module cycle involving `aida.schemas`, catalog services/shims, and module schemas, plus a four-module cycle involving agent contract APIs, reviewer agent, and semantic API. These are import-graph cycles, not proof of import crashes; deferred imports can avoid immediate runtime failure.

Move shared DTOs/enums into dependency-light contracts. Services should not obtain business models from routers. Reviewer automation should call the application service. Extend import-linter contracts as boundaries become enforceable.

### R04 — Reduce central ORM/schema coupling incrementally

`models.py` has 5,744 lines and 155 direct importers; `schemas.py` has 3,936 lines and 89. Relocate one bounded context at a time with compatibility exports. Separate persistence, API DTOs, and domain values. Retain database constraints and explicit tenant-scoped repository inputs.

Preserve historical migration identities; do not rewrite migrations for appearance. One static head was found, but migration execution/schema drift were not verified here.

### R05 — Split frontend API clients by domain

`api.ts` is 4,576 lines with repeated HTTP decoding/header handling. `_api_append`, `_cross_source_api`, and `_column_documentation_api` are live modules despite temporary-looking names.

Use a dependency-light transport and catalog/governance/identity/sources/quality/agents/products clients. Remove API/org coupling. Put demo fixtures behind an explicit adapter. Keep generated types; parameterize page response schemas so critical types can be generated rather than handwritten.

### R06 — Break large screens into workflow components

Business meaning, Transformations, Context products, Tool registry, Administration, Workspace access, and Agent gateway span roughly 844–995 lines each. Extract controllers/query logic and focused editor/list/detail sections. Share version history, submit-for-review, parameter authoring, evidence, and grants. Build on current tokens/primitives instead of starting an unrelated visual rewrite.

### R07 — Deduplicate verified copies

Exact AST duplicates include `api._query_execution_response` / `tool_api._query_response` and `bi_lineage._parse_generated_at` / `dbt_artifacts._parse_generated_at`. Six screens retain inline URL-state hooks beside the shared one. Agree the correct contract first, then consolidate; sharing a bug is not a fix.

## 4. Dead code, inactive code, and hygiene

### D01 — Cleanup candidates need consumer verification

The browser import graph from main did not reach `ProposalCard.tsx` or `OrgPicker.tsx`. Verify nonliteral imports, stories, and tests before removal. Active navigation uses ScopePicker.

`injection_corpus.py` is not reached by ordinary server processes but legitimately serves offline evaluation. Package markers, migrations, public SDK exports, and tooling entry points are not dead code merely because main does not import them.

### D02 — Give inactive functionality explicit ownership

Principal lifecycle is an unfinished trigger integration (F19). Service/repository scaffolds are incomplete extraction. The hard-coded `orders_raw` propagation story is explicitly gated off by default; it is not an unconditional fabricated production result. Keep it visibly demo-only or replace it with real evidence-backed traversal.

### D03 — Retain compatibility shims until supported callers migrate

DB/config/models/schema re-exports and moved API aliases are intentional seams recognized by import contracts. Assign replacement paths, owners, caller counts, and removal conditions. Do not remove them solely because they look redundant.

### D04 — Git tracks generated Vitest artifacts

The initial inventory found **60 tracked** `vitest.config.ts.timestamp-*.mjs` files. Root ignore rules covered Vite timestamps but not this pattern. Contents were not reviewed. Add the matching ignore and untrack existing generated files separately; ignore rules do not untrack files. Excluding tests from review is not permission to delete test source.

### D05 — Legacy retirement recommendation and later scope decision

At the review baseline both frontends were documented/deployed, so legacy `ui/` was not dead code. The original recommendation was parity verification, redirects, usage review, and then retirement.

**Subsequent recorded decision:** the existing remediation tracker explicitly supersedes that recommendation with outright legacy removal. Preserve that decision; this restored review is not a request to reintroduce a parity approval gate. Ensure required consumer journeys remain supported and remove obsolete deployment/docs references consistently.

### D06 — Documentation truth drift

README referenced missing `Docs/60-delivery/04-status-matrix.md` and a missing competitor planning document. `00-status.md` lagged several delivered features and React migration. README connector prose lagged the registry's six BETA database registrations.

Use one current register separating implemented, reachable, configured, and verified, with dates/owners/evidence. Accomplishment logs are history, not current state. Validate local links and generate inventories where practical.

## 5. Performance, data architecture, and operations

### Preserve the existing strengths

- Keyset pagination on major metadata reads.
- Protected SQL execution access through the query gateway.
- Outbox locking with SKIP LOCKED, retry/backoff, and dead-letter states.
- Temporal heartbeat/retry distinctions and cooperative batch pause/cancel checkpoints.
- Connector capability/maturity metadata.
- React lazy routes and virtualized inventories.
- PostgreSQL authority with rebuildable projections.

### Improvements and the evidence to obtain

| Area | Observation / risk | Improvement / validation |
|---|---|---|
| Graph projection | `load_projection` materializes catalogs/schemas/tables/columns | Chunked/incremental projection, bounded memory, deletion reconciliation; measure large-source rebuild |
| Outbox/Kafka | Sends occur while batch row locks remain held; no database/Kafka atomic transaction | Bound batch/timeouts; prove consumer idempotence using event IDs; adopt leases only if measured contention warrants |
| Review pages | Per-review diffs and large Overview requests | Summary/list/detail split, query-count and p95 budgets |
| Global scope | Capped first pages | Remote search and selected-ID lookup |
| Tenant fairness | Large tenants can dominate shared sweeps | Tenant budgets, fair scheduling, oldest-backlog metrics |
| Connection pools | Multiple process types consume connections | Document total pool math and measure against actual database limits |
| Retrieval | Long functions mix filtering/ranking/evidence/providers | Stage metrics, bounded candidates, cancellation, per-channel quality/latency |
| Health | Config/client existence sometimes substitutes for real success | Last success, lag, failure, disabled, and enforcement-state signals |
| Retention | Audit/run/proposal/document/event history grows | Asset-specific retention/holds, safe purge/rebuild, cost forecasts; obtain requirements before deletion |
| Recovery | Local volumes/templates do not demonstrate restore | Agree RPO/RTO; restore authoritative DB, rebuild projections, reconcile workflows; record measured recovery |

No bank-scale load, soak, recovery, or connector certification was run. Algorithmic patterns above are source observations; proposed resource/latency targets require measurement.

## 6. Security and AI governance

Meaningful fail-closed design exists in auth settings, SQL validation, execution limits, and model activation. Make its coverage auditable:

1. Map every REST/MCP/export/bulk/job/SDK surface to tenant/workspace checks, roles, side effects, audit, and cancellation behavior.
2. Separate persona, role, workspace membership, delegated authority, and agent identity in code and product language.
3. Reuse one decision service across human, delegated, bulk, and automated review. Explain automation eligibility, evidence, and safe reversal.
4. Verify policy parity for direct IDs, links, cached projections, and exports. No successful tenant bypass was demonstrated; full authorization certification remains separate.
5. Treat model/integration output as proposed/untrusted until deterministic validation. Preserve route/version, retrieval receipts, tool-first rationale, and refusal evidence.
6. Evaluate representative business questions and difficult refusals. Deterministic benchmark success is not live-model answer-quality proof.
7. Inventory destination/credential references; distinguish configured, approved, active, healthy, and verified.
8. Verify retention, native policies, revocation, and kill switches against real services. Local success objects are not destination evidence.

This is a technical review, not a claim of regulatory or penetration-test certification.

## 7. Build, packaging, CI, and developer experience

Initial local checks: React production build passed; mypy passed on 333 source files. Ruff initially found two formatting errors in semantic_api; independent edits corrected them and the rerun passed. Those formatting changes shifted some later line references by five. One static Alembic head was found. No test suite was run. These checks are timestamped review evidence, not a claim that every later edit was retested.

CI already declares valuable lint/type/import, migration, OpenAPI/type drift, UI, benchmark, reachability, dependency/secret, and container gates. Improve what they prove:

- Add production-proxy, fresh-browser auth, deep-link, and least-privilege journey verification.
- Add actual archive/delivery receipts and review concurrency to integration gates.
- Extend reachability review across both Python namespaces and frontend imports, with explicit offline/scaffold reasons. Imports do not establish triggering or completion.
- Verify upgrades/restores on PostgreSQL, beyond fresh ORM schema construction.
- Confirm frontend dependency scanning/provenance if not provided by external pipelines; no current vulnerability assessment was performed here.
- Inspect packaging: pyproject declares `sdk/aida_tool_sdk`, but the backend Dockerfile copies src/migrations rather than sdk. Decide whether the image should include it and align manifests/smoke checks. No Docker build was run here; this is a packaging gap to verify, not a reproduced image-build failure.
- Reduce historical task narratives embedded in code. Keep enduring invariants near implementations; put obsolete history in Git/ADRs. Add concise domain guides and generated architecture maps.
- Keep the SDK a supported public contract; future independent distribution should reuse dependency-light validation rather than pull server ORM/application dependencies into client imports.

## 8. Product direction

Complete one trustworthy journey:

**Connect → inspect ingestion → establish ownership/meaning → review → consume a governed answer/product → inspect evidence and operational health.**

Fix the findings relevant to the release, repair session/navigation continuity, and consolidate screens around tasks. Then add the capabilities that complete the journey: result inspection, resumable setup, durable delivery receipts, saved investigations, and guided refusal recovery. The companion documents specify each route and the implementation order.
