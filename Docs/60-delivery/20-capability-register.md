# Capability register — current state

> **Reconciliation 2026-09-11:** this register owns dated capability evidence, not work status. [Tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) owns execution.
>
> **Refresh 2026-09-12 (R11-B14), completed.** Every row that still carried a 2026-09-06 measurement — 34 of them — was re-measured against the tree on 2026-09-12, one at a time, and now carries that date. Six rows had already been re-measured earlier the same day (the query execution gateway, Kafka/Redpanda outbox publication, Neo4j graph projection, and the three capabilities in the "added this cycle" section).
>
> **Four rows deliberately keep an earlier date, and were not re-measured in this pass:** the WORM audit archive (2026-09-09), the steward agent (2026-09-10), and the lineage and quality agents (2026-09-11). Each of those dates is the date of the dated run its Verified cell describes, and each is recent; re-stamping them 2026-09-12 without re-running that evidence would be exactly the drift this register exists to stop.
>
> The date column is the claim, not the row's presence here: a Yes means someone checked it on the date shown, not that it holds forever. What moved in this pass, and what was found to have been *wrong* rather than merely stale, is recorded in the [2026-09-12 re-measurement note](#appendix-what-the-2026-09-12-re-measurement-changed) at the foot of this file.

## September 11 source corrections

| Capability / property | Implemented / reachable | Configured / verified | Current work |
|---|---|---|---|
| Contracted-agent Ask and governed MCP tools | Yes: `api.py` and `mcp_server.py` pass resolved caller contract into orchestrator | Production configuration not inspected; boundary tests exist in `test_ar06_contract_on_live_paths.py`, not rerun in this documentation pass | X7 removal cancelled; R11-C6 retains other contract-boundary gaps |
| Safe unattended reviewer approvals | Guards and benchmark exist; truth discrimination remains insufficient | Recorded AR-03 result: 7 false twins approved in 12 pairs; no safe unattended approval claim | R11-C3 PARTIAL; keep unattended review off |
| Single source-SQL execution boundary | Gateway exists, but native-policy sync opens source drivers outside it | Direct source connections rechecked in `policy_native_sync.py`; no live execution performed | R11-D1 open |
| Ontology publication in running deployment | Code/lifecycle tests reported in five-feature implementation report | Last report has unapplied migration/lock checkpoint; database not rechecked in this pass | R11-C1 BLOCKED on fresh deployment verification |
| Legacy UI retirement | Removed; `ui/` absent in current tree | Source/filesystem confirmation 2026-09-11; no live deployment inspected | UX-16 closed; navigation consolidation is R11-S10 |



## September 12 — capabilities added this cycle

Same column rules as the main register: *Verified* stays strict, so a green test against
in-memory SQLite is **No**, not Yes.

| Capability / property | Implemented | Reachable | Configured | Verified | Measured | Owner | Evidence |
|---|---|---|---|---|---|---|---|
| Impact analysis reaches BI reports | Yes — BI joins the unified graph at table grain with provenance, report containment walked upward | Yes — `unified_lineage_builder` collectors run in the existing traversal | Yes — no setting required | **No** — bounds and tenant isolation are proven by mutation-tested unit tests, not against a live BI import in a running deployment | 2026-09-12 | Engineering | `src/aida/unified_lineage_builder.py`; `tests/test_unified_lineage.py` (R11-B13 tests) |
| Classification propagation produces derived values | Yes — collector builds column edges from reviewed view/procedure/OpenLineage lineage and stores derived rows | Yes — a scheduler pass calls it | **No by design** — `classification_propagation_interval_minutes` defaults to 0 (never), following the task-agent convention; the pass opens no session when off | **No** — proven against in-memory SQLite only; no estate has run it, so no derived row exists anywhere yet | 2026-09-12 | Engineering | `src/aida/classification_propagation.py`; `src/aida/workflows/scheduler.py`; `tests/test_at11_classification_propagation.py` |
| Ask collects a governed tool's parameters | Yes — structured `MISSING_TOOL_PARAMETERS` refusal carrying the parameter names and tool version; Ask renders a form and re-asks pinning that version | Yes — the live Ask endpoint and screen | Partial — works with generation off, which is the default, but the sample estate had no parameterised tool to demonstrate it until R11-B1's seeding half | **No** — not exercised against a running API; the demo fixtures answer every ask with success, so the refusal path cannot appear there | 2026-09-12 | Engineering | `src/aida/agent_orchestrator.py`; `src/aida/api.py`; `ui-next/src/screens/AskScreen.tsx` |
| Vector similarity contributes to retrieval ranking | Yes — the vector channel scores the authorized candidate set through `src/aida/retrieval_stages.py`, preferring the persisted index and embedding live what the index does not cover | Yes — inside `hybrid_retrieve_enhanced`, on the live Ask path | Yes as of 2026-09-12 — `gemini-embedding-001` at 768 dimensions; `compose.yaml` now passes the `AIDA_EMBEDDING_*` variables through, which it previously did not, so a container read `unset` however the host was configured | **Partial** — a real provider was called and returned 768-dimension vectors with the expected semantic ordering, and the persisted index was built (10 objects); not exercised against a bank-scale estate, and the channel **re-ranks rather than discovers**, so semantic retrieval of an object lexical never surfaced does not work — measured, 5 of 5 such corpus cases miss | 2026-09-12 | Engineering | `src/aida/retrieval_stages.py`; `src/aida/vector_index_service.py`; `Docs/10-architecture/19-embeddings-design.md`; `tests/test_retrieval_stages.py` |
| Persisted vector index is kept fresh without an operator | Yes — `run_vector_index_rebuild_pass` sweeps the stalest organizations on a cadence | Yes — called from `run_scheduler_iteration`; before 2026-09-12 the only caller was an operator endpoint whose UI does not exist, so the index went stale and retrieval silently paid a provider call per candidate per query | Yes — enabled by default and a no-op with no embedding provider, which is the shipped state | **No** — proven by unit tests including an AST check that the scheduler still calls it; no deployment has run the pass on a schedule | 2026-09-12 | Engineering | `src/aida/vector_index_service.py`; `src/aida/workflows/scheduler.py`; `tests/test_vector_index_schedule.py` |

> Status: **Current state, not history.** Owner: Engineering lead.
> Created 2026-09-06 for finding D06 of [`../review-2026-09-05/REVIEW.md`](../review-2026-09-05/REVIEW.md).
> Every row carries a date, an owner and evidence a reader can go and check.

## Why this document exists

D06 found documentation that had drifted from the code, and the review named the cause rather than
just the symptom:

> Use one current register separating implemented, reachable, configured, and verified, with
> dates/owners/evidence. Accomplishment logs are history, not current state.

The four words are not synonyms, and collapsing them is how a project ends up believing it has a
capability it does not have. This register keeps them apart on purpose.

| Column | Question it answers | What does **not** earn a Yes |
|---|---|---|
| **Implemented** | Does committed source in this repository do the thing? | A schema, a settings field, a TODO, or a function that logs and returns success |
| **Reachable** | Does a process that actually runs invoke it? A mounted route, a `compose.yaml` service command, a Temporal worker/scheduler registration, or a CLI entry point | Being importable. Being covered by a unit test. Being called only from another unreachable module |
| **Configured** | Do the settings, credentials and destinations this repository *ships* let it do its job in the local topology? | A settings field that exists but defaults to off, empty, or a placeholder. "The bank will supply it" is a legitimate **No** |
| **Verified** | Has someone run it end-to-end against the real destination or the real topology and left evidence? | A green unit test against a mock or an in-memory SQLite. A local success object. A tracker row saying DONE |

**Verified is the strict one.** The review is explicit that a local success object is not
destination evidence, so almost every external-integration row below says **No**. That is the
honest answer, and a row that says No here is more useful than a row that says Yes and is wrong.

**Scope of the last pass.** Every row was re-measured on 2026-09-12 by reading the code named in
its Evidence column against the tree, and — where it could be done without infrastructure this
repository does not have — by running it. Four rows keep an earlier date on purpose; the banner at
the top of this file names them. The rows that the earlier remediation pass left *under
remediation* now describe the landed implementation.

Note what did and did not move. `Implemented` and `Reachable` changed for several rows.
`Verified` did **not** change for a single external integration, because nothing in that pass
could contact a real destination: an archive that reads its own bytes back off a local filesystem
and a webhook delivered to a loopback server are evidence of a working implementation, not of a
production receipt. That distinction is the entire reason this register has four columns instead
of one, and it is why F01 and F04 are recorded as implemented-and-unverified rather than done.

## Legend

* **Yes** — holds today, with the evidence cited.
* **Partial** — holds for a named slice; the row says which.
* **No** — does not hold. Not a defect by itself; often a deliberate default-off posture.
* **n/a** — the column does not apply (e.g. nothing to configure).

Owner values name the area, not a person: this repository has no CODEOWNERS file yet, and
inventing individual names would be its own truth drift.

## Register

### Platform and runtime

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| REST API (FastAPI modular monolith) | Yes | Yes | Yes | Partial — local Compose only, but exercised there on 2026-09-12: the running stack answered `GET /health/ready` with `status: UP` (`postgresql` UP, `temporal` UP, `outbox_backlog` UP, `audit_archive_task` running), service `aida-control-plane` 1.2.0. No non-local topology, no load or failover certification | 2026-09-12 | Platform | `src/aida/main.py`; `compose.yaml` `api` service with a `/health/live` healthcheck; CI `docker-build` job imports `aida.main` from the built image |
| Backend container image | Yes | Yes | Yes | Yes — the image built from this `Dockerfile` is running in the local stack and answering `/health/ready` (checked 2026-09-12); CI `docker-build` imports `aida.main` from the built image | 2026-09-12 | Platform | `Dockerfile`; CI `docker-build` job; `scripts/check_image_packaging.py` |
| Public Tool SDK shipped in the image | Yes | n/a — a client library, not a server path | Yes | Yes — re-run 2026-09-12: `docker exec … python -c "import aida_tool_sdk"` inside the running `api` container imported it and reported 0.1.0 | 2026-09-12 | Platform | `sdk/aida_tool_sdk/`; `pyproject.toml` `[tool.hatch.build.targets.wheel].packages`; `Dockerfile` `COPY sdk ./sdk`; `scripts/check_image_packaging.py` |
| Alembic migrations against real PostgreSQL | Yes | Yes | Yes | Yes — CI `migration-drift` applies every migration to an empty Postgres 16 and diffs against `Base.metadata`. **CI is the evidence, not the local stack:** the long-lived local development database was found at revision `d41a7b8e6c02` on 2026-09-12, behind the tree's six heads, so the running stack's schema is not this tree's | 2026-09-12 | Platform | `migrations/`; CI `migration-drift` job, `tests/test_migration_orm_drift.py` |
| Temporal durable workflows | Yes | Yes | Yes | Partial — `temporal` reported UP through `/health/ready` in the running local stack on 2026-09-12, and the worker registers `DatasourceDiscoveryWorkflow` and `MetadataBatchIngestionWorkflow`. Still local Compose only; no failover, no continue-as-new at scale | 2026-09-12 | Platform | `compose.yaml` `temporal`, `metadata-worker`, `fleet-scheduler`; `src/aida/workflows/worker.py`; `src/aida/workflows/` |
| Kafka/Redpanda outbox publication | Yes | Yes | **No by default since 2026-09-12** — R11-X9 moved `redpanda`/`outbox-publisher` behind the `events` (and `graph`) Compose profile, because with default settings nothing consumed the topics. Producers still write `outbox_event` rows, which stay `PENDING` until a publisher runs | Partial — local Compose only, and only under a profile; no database↔Kafka atomicity, per REVIEW §5 | 2026-09-12 | Platform | `compose.yaml` profiles; `src/aida/projectors/outbox_publisher.py`; README step 2 |
| Neo4j graph projection | Yes | Yes | **No by default since 2026-09-12** — R11-X9 moved `neo4j`/`graph-projector` behind the `graph` profile; the default graph backend is `postgres`, which is the certified one. R11-D8 also stopped reconciliation dialling Neo4j regardless of backend | **No** — INV-9 still records the Neo4j backend as uncertified; the projection-rebuild drill (E5) has never been run | 2026-09-12 | Platform | `compose.yaml` profiles; `src/aida/graph_store.py`; `src/aida/graph_reconciliation.py`; `00-status.md` §3 INV-9 |
| Disaster recovery / restore | No | No | No | **No** — needs a deployed topology and an approved RPO/RTO | 2026-09-12 | Bank decision | Re-checked 2026-09-12: `scripts/` holds no backup, restore or failover script (the only drill script is `scripts/agent_kill_switch_drill.py`, which is unrelated), and no runbook covers one. REVIEW §5 "Recovery"; POINTS-TRACKER T28 |

### Frontend and public endpoints

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| Atlas portal (`ui-next`) — the only portal | Yes | Yes | Yes | Partial — more than the 2026-09-06 cell credited. CI runs `vite build`, `tsc --noEmit`, `vitest`, an axe-core WCAG 2.1 A/AA sweep of all 44 navigable screens with an accessible-name floor, and a six-step Playwright journey in real Chromium through the production nginx image. Two gaps remain: the journey's upstream is a stub synthesised from the OpenAPI baseline, not the real backend, and the human half of accessibility (screen reader, real-rendering contrast, 100% zoom) is unexecuted | 2026-09-12 | Frontend | `ui-next/`; `compose.yaml` `ui-next` on `:3001`; CI `ui-next`, `ui-journey` jobs; `ui-next/src/a11y-sweep.test.tsx`; `24-accessibility-acceptance-2026-09-12.md` |
| Legacy `ui/` portal | **Removed** | n/a | n/a | n/a | 2026-09-12 | — | `ls ui` returns nothing; no compose service, Dockerfile, CI job or script references it (re-checked 2026-09-12) |
| API reachable from the browser on one origin (`/v1/`) | Yes | Yes | Yes | Yes — CI `ui-proxy` exercises the nginx proxy against a stub upstream, and on 2026-09-12 a request through the running `ui-next` nginx on `:3001` reached the real API and was answered by it (the API's own `401 X-Principal-Id is required` refusal, then `200` once the header was supplied) | 2026-09-12 | Platform | `ui-next/nginx.conf`; `ui-next/vite.config.ts`; `scripts/check_proxy_contract.py`; CI `ui-proxy` job |
| MCP endpoint reachable at the URL the UI advertises (`/mcp`) | Yes | Yes | Yes | **Yes as of 2026-09-12** — the gap this row named is closed. A full JSON-RPC `initialize` through the deployed stack succeeded: `POST http://localhost:3001/mcp` (through the `ui-next` nginx, with `X-Principal-Id`) returned HTTP 200 with `protocolVersion 2025-03-26`, `serverInfo.name atlas-governed-data-platform` and the `tools`/`resources`/`prompts` capability set. The proxy hop was already verified | 2026-09-12 | Platform | F07; `src/aida/mcp_server.py` `APIRouter(prefix="/mcp")`; `ui-next/nginx.conf`; CI `ui-proxy` job |
| Browser OIDC sign-in flow | Yes — real JWT verification against a JWKS endpoint, with issuer/audience checks and role and persona mapping | Yes — `security.py` is on every request path and selects the OIDC verifier when `identity_provider == "oidc"` | Partial — **the 2026-09-06 cell's "no IdP is configured in this repository" is out of date.** `compose.oidc.yaml` (added 2026-09-10) ships a mock IdP — `mock-oauth2-server` — wired to `AIDA_OIDC_ISSUER`, `AIDA_OIDC_JWKS_URL`, `AIDA_OIDC_AUDIENCE` and role/persona mappings, with a `ui-next` image built against it. It is a development overlay and a *mock*: the default `compose.yaml` still runs development principal-header mode, and no real bank IdP is configured | **No** — the overlay was not brought up in this pass, so no sign-in has been driven through it | 2026-09-12 | Frontend | F06; `compose.oidc.yaml`; `src/aida/security.py`; `src/aida/oidc.py`; POINTS-TRACKER F06 |

### Connectors

Maturity values are read from `src/aida/connectors/registry.py`, which is authoritative. Six
database connectors are registered `BETA`; two are declared `PLANNED`.

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| PostgreSQL connector (`BETA`) | Yes | Yes | Yes | Yes — discovery run against live Postgres 16 **and** 14 service containers in CI `connector-version-fixtures`; the `sample-source` Postgres fixture was also healthy in the running local stack on 2026-09-12 | 2026-09-12 | Connectors | `connectors/postgres.py`; CI `connector-version-fixtures` job, `tests/test_postgres_version_fixtures.py` |
| Microsoft SQL Server connector (`BETA`) | Yes | Yes | Yes | Partial — exercised against the `sample-mssql-source` Compose fixture, which was healthy in the running local stack on 2026-09-12; no multi-version, TLS or delegated-identity certification | 2026-09-12 | Connectors | `connectors/sqlserver.py`; `compose.yaml` `sample-mssql-source` |
| Oracle connector (`BETA`) | Yes | Yes | **No** — re-checked 2026-09-12: `compose.yaml` still defines no Oracle service (the sample source was retired) | **No** — never run against a live Oracle | 2026-09-12 | Connectors | `connectors/oracle.py`; registry notes: query estimate fails closed with `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` |
| BigQuery connector (`BETA`) | Yes | Yes | **No** — needs a real GCP project | **No** — never run against a live BigQuery | 2026-09-12 | Connectors | `connectors/bigquery.py`; registry notes honestly omit foreign-key metadata |
| Snowflake connector (`BETA`) | Yes | Yes | **No** — needs a real account/warehouse | **No** — never run against a live Snowflake | 2026-09-12 | Connectors | `connectors/snowflake.py` |
| Databricks connector (`BETA`) | Yes | Yes | **No** — needs a real workspace | **No** — the registry note says so in the code: "Code complete; never exercised against a live Databricks workspace" | 2026-09-12 | Connectors | `connectors/databricks.py` |
| Teradata, IBM Db2 | **No** — canonical push ingestion only | n/a | n/a | n/a | 2026-09-12 | Connectors | `connectors/registry.py` `declare_planned(...)`, `implementation_status="PLANNED"` (re-read 2026-09-12: six connectors `BETA`, these two `PLANNED`) |

### Governance, audit and delivery

Every row in this section is the subject of an open P0/P1 review finding. They are listed with the
finding rather than omitted, because a capability register that quietly drops the contested rows is
the exact failure mode D06 describes.

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| Audit ledger and transactional outbox | Yes | Yes | Partial — the ledger and the outbox *writes* are default-on, but *publication* is not: R11-X9 moved `outbox-publisher` behind the `events`/`graph` profile, so on a default stack `outbox_event` rows accumulate `PENDING`. See the Kafka/Redpanda row above | Partial — local Compose only, and no database↔Kafka atomicity (REVIEW §5). Measured live on 2026-09-12 with the profile enabled: 478 `audit_event` rows, 411 `outbox_event` rows, **0 PENDING**, and `/health/ready` reporting `outbox_backlog: UP` | 2026-09-12 | Audit | `src/aida/events.py`; `src/aida/projectors/outbox_publisher.py`; `compose.yaml` profiles |
| WORM audit archive to an external immutable store | Yes — two complete providers: `filesystem` (two-phase lifecycle, full-envelope versioned checksum, membership, lease) and `s3` over S3 Object Lock, signed by a standard-library SigV4 implementation with no SDK dependency; `gcs`/`azure_blob` still resolve to a provider that refuses | Yes — archive loop runs from `main.py`, which also creates the Object Lock bucket when the backend is `s3` | Yes — defaults to `none`, which refuses rather than reporting success; `s3` reads the existing `object_store_*` settings | Partial — **against MinIO, not AWS.** All four F01 properties were exercised against a live S3-compatible service on 2026-09-09: object retrieved and re-checksummed; a raw overwrite created a new version while the locked version stayed intact and re-verified; deleting the locked version before retain-until was refused by the service (MinIO: `400 InvalidRequest`, "Object is WORM protected"); a legal hold blocked deletion after retention had lapsed, and release restored expiry. Not yet exercised against a real AWS S3 bucket, where refusals are `403 AccessDenied` and IAM/bucket policy, not just Object Lock, is in play. Filesystem immutability remains a guard rail, not a security boundary | 2026-09-09 | Audit | F01/F02/F03; `src/aida/worm_archive.py`, `src/aida/audit_archive_storage.py`, `src/aida/audit_archive_s3.py`, `src/aida/aws_sigv4.py`, `src/aida/audit_envelope.py`, `tests/test_audit_archive_s3.py`, `tests/test_aws_sigv4.py` |
| SIEM / SOC event routing | Yes — real webhook and syslog transports (RFC 5424; RFC 6587 octet counting on TCP) behind durable delivery intents with retry, backoff and dedup | Yes — worker runs from the scheduler | Partial — re-confirmed against the shipped defaults on 2026-09-12: `siem_endpoint` is still the placeholder `internal://security-log-pipeline`, which `parse_siem_endpoint` resolves to NOT_CONFIGURED, and `delivery_worker_enabled` still defaults `False`, so an upgrade sends nothing | **No** — receipt is proven against loopback stub servers, not a real SOC collector | 2026-09-12 | Delivery | F04; `src/aida/siem_routing.py`, `siem_delivery.py`, `delivery_intents.py` |
| Governance notification delivery | Yes — intent created in the business transaction, delivered by an independent worker; requested/attempted/delivered timestamps separated | Yes — worker runs from the scheduler | Partial — default-off, as above (`delivery_worker_enabled=False`, re-confirmed 2026-09-12) | **No** — outage and recovery are exercised against a stub server, not a real channel | 2026-09-12 | Delivery | F12; `src/aida/governance_review_relay.py`, `governance_notifications.py`, `delivery_intents.py` |
| Maker-checker review (single decision) | Yes | Yes | Yes | **Yes as of 2026-09-12** — the contention gap this row named is closed. `tests/test_governance_decision_postgres_concurrency.py` races the single-decision router against a **real PostgreSQL** with separate sessions on separate connections released from a shared barrier, at READ COMMITTED *and* REPEATABLE READ; the loser blocks on the winner's `FOR UPDATE` row lock and is refused. Run on 2026-09-12: 14 passed (command in the appendix) | 2026-09-12 | Governance | `src/aida/semantic_api.py` single-decision path uses `SELECT ... FOR UPDATE`; `tests/test_governance_decision_postgres_concurrency.py` |
| Maker-checker review (bulk decision) | Yes — single, bulk, sample-review and reviewer-agent decisions share one service whose claim is a compare-and-set `UPDATE … WHERE status='PENDING'` | Yes | Yes | **Yes as of 2026-09-12** — the 2026-09-06 cell said "a concurrent PostgreSQL reproduction is still outstanding"; **it already existed on that date and the cell was wrong.** It was run on 2026-09-12 against real PostgreSQL — 14 passed — covering single-vs-single, single-vs-bulk, bulk-vs-bulk, the lock-free service claim, a three-way race and a bulk sweep against a lock-free decider, each at two isolation levels, with the falsifying assertion that a contended review enters its target's side-effect path exactly once. Recorded finding: at REPEATABLE READ the loser is refused as SQLSTATE 40001, not as 409 CONFLICT, so raising isolation needs a retry loop | 2026-09-12 | Governance | F05; `src/aida/governance_decision_service.py`; `tests/test_governance_decision_postgres_concurrency.py` |
| Workspace ABAC authorization | Yes | Yes | Yes | **No as an enforcing control** — confirmed live on 2026-09-12, which is stronger than the reading it replaces: `/health/ready` reports `workspace_authorization: OBSERVING` with `workspaces_total=0`, `workspaces_enforcing=0` and `unresolved_scope: PROCEEDS_UNDECIDED`. The system measures and does not deny, and there is not yet a single workspace to enforce over. Deliberate; see F11 | 2026-09-12 | Platform | F11; `src/atlas/platform/config.py`; `src/aida/authorization_gate.py`; `00-status.md` §3 INV-4 |
| Enterprise secret manager boundary | Yes | Yes | **No** — re-confirmed 2026-09-12: `Settings.reject_insecure_production_configuration` forbids `credential_provider == "env"` in production, and no bank provider is registered in this repository, so the production-valid path has no resolver behind it | **No** — no rotation or outage drill | 2026-09-12 | Platform | `src/aida/secrets.py`; `00-status.md` §4 "Enterprise secrets and source identity" |

### AI and analytics

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| Query execution gateway (single choke point) | Yes | Yes | Yes | Partial — INV-2 is enforced by four layers as of 2026-09-12: the type system, an import contract, an AST scan for the executor's methods, and a scan for driver `connect` calls outside `aida.connectors`. The fourth was added because the first three could not see `policy_native_sync` dialling a source directly (R11-D1); that apply path is removed. Cancel propagation remains uncertified, and no live execution was performed in this pass | 2026-09-12 | Platform | ADR-0004; `tests/test_tier0_invariants.py`; CI `quality` job runs `lint-imports` |
| Deterministic prompt-risk screening and SQL guard | Yes | Yes | Yes | Partial — measured against a synthetic corpus (zero bypasses, zero false positives), not against real bank traffic; re-confirmed 2026-09-12 that CI `quality-baseline` still runs `scripts/quality_benchmark.py` on every push | 2026-09-12 | AI governance | `src/aida/injection_defense.py`; `Docs/90-reference/model-risk-benchmark-results.md`; CI `quality-baseline` job |
| Model generation (OpenAI / Gemini) | Yes | Yes | Partial — **this moved on 2026-09-12, but less far than it looks.** The repository still ships generation off: `.env.example` and `compose.yaml` carry `AIDA_MODEL_GENERATION_ENABLED=false` and default `AIDA_MODEL_ROUTE` to `openai-bank-sql`, so a fresh clone generates nothing. What exists now is local, gitignored operator configuration: two routes APPROVED in `sample-bank` through the real maker-checker path (`gemini-bank-sql` → `gemini-2.0-flash`, `openai-bank-sql` → `gpt-4o-mini`), credentials resolving, and no kill switch engaged. `compose.yaml` does pass `AIDA_MODEL_*` and both API keys through from the host, so the local topology can be pointed at a route | **No — and now for a concrete, live-proven reason rather than an absence.** Both approved routes were called end to end through `ProviderNeutralModelGateway.structured_completion` on 2026-09-12 and **both failed at the provider**: `gemini-bank-sql` returned HTTP 404 — *"This model models/gemini-2.0-flash is no longer available"*, i.e. the maker-checker-approved model identifier has been retired upstream — and `openai-bank-sql` returned HTTP 429 `billing_not_active`. A 404 is not in the gateway's retryable set `{408, 409, 429, 500, 502, 503, 504}` and the orchestrator only falls back on `{429, 502, 503, 504}`, so with `gemini-bank-sql` primary the fallback is never tried and every generated-SQL Ask fails closed. Generated-answer quality stays unmeasured because no approved route can currently answer | 2026-09-12 | AI governance | `src/aida/model_gateway.py`; `src/aida/agent_orchestrator.py`; `scripts/seed_model_route.py` (R11-B2, which registered the route through the real maker-checker path); `compose.yaml` `AIDA_MODEL_GENERATION_ENABLED: ${...:-false}`; README "Model generation is also fail closed" |
| Hybrid retrieval (lexical + vector + graph fusion) | Partial — and the limit is now measured rather than assumed: the vector channel **re-ranks the policy-authorized candidate set and cannot discover**, so it can reorder what lexical retrieval found but never surface a table it missed | Yes | Partial — **the 2026-09-06 "Yes" was wrong.** The vector third of the fusion is still not configured by anything this repository *ships*: `embedding_provider` defaults to `unset`, which `resolve_embedding_provider` turns into a refusal, and `compose.yaml` defines **no `AIDA_EMBEDDING_*` variable at all**, so no host `.env` can reach the containers. Confirmed live on 2026-09-12: `AIDA_EMBEDDING_PROVIDER` is empty inside the running `api` container, so `run_vector_channel` skips on `EmbeddingUnavailable` and the fusion there is lexical + graph. A process run directly against this machine's gitignored `.env` does reach a provider — which is how the Verified evidence was produced, and is not the same thing as the shipped topology being configured | Partial — the vector signal has now been exercised for the first time. R11-B2's 2026-09-12 benchmark run reports it "available and exercised" against live `gemini-embedding-001`, and the provider was separately confirmed to answer (one call, 768-dimension non-zero vector). **The measured result is the point:** hit@1 `0.5882`, recall-within-bound `0.7059` and MRR `0.6373` — unchanged from the vector-absent baseline — with all five semantic-paraphrase cases still not found, for the re-rank-not-discover reason above. No large-catalog benchmark; and CI `quality-baseline` supplies no embedding credential, so the CI gate still measures the vector-absent path | 2026-09-12 | Retrieval | `src/aida/retrieval.py`; `src/aida/retrieval_stages.py`; `src/aida/embedding_provider.py`; CI `quality-baseline` job; `Docs/90-reference/quality-benchmark-results.md`; `Docs/10-architecture/19-embeddings-design.md` §2 |
| Ask Atlas returns result rows to the user | Yes — bounded result panel with masking chips, column tags and a truncation banner; no result retention added | Yes | Yes | Partial — **both halves of the 2026-09-06 cell are now false.** It said "not validated in a real browser": step 5 of the CI `ui-journey` job drives Ask in real Chromium through the production nginx image and asserts the answer panel renders. It also said "the response carries no `applied_row_limit`": the response has carried `applied_row_limit` *and* `row_limit_source` since 2026-09-06, and `QueryResultTable.tsx` decides truncation from them via `truncatedByGatewayCap` instead of pattern-matching the SQL — that clause was wrong on the day it was written. What is genuinely unverified is the destination: the journey's upstream is a stub synthesised from the OpenAPI baseline, so no **real** query result has been rendered in a browser | 2026-09-12 | Frontend | F20; `ui-next/src/components/QueryResultTable.tsx`; `src/aida/query_execution_view.py`; `e2e/tests/journey.spec.ts`; CI `ui-journey` job |
| Steward agent — contracted proposals of table and column descriptions and glossary links | Yes — the deterministic GL-9, GL-8 and column-drafter producers run under an `AgentContract`; tier-gated, killable mid-run, bounded, ledgered; decides nothing. Where it is registered, the ingest side-car drafts under the same contract | Yes — `GET`/`POST /v1/organizations/{org}/steward-agent[/run]` mounted in `aida.main`; a console screen in the Steward work area. Runs are started by a person, or by the scheduler once its interval setting is above 0 (it is 0, off, by default) | **No** — nothing ships registered: an organization must approve an `AGENT`-kind AI asset version and give it a contract for `agent:steward` | **No** — in-memory SQLite and a fixture-mode browser check only; the mid-run kill rollback is not reproduced on PostgreSQL, and no run against a real estate | 2026-09-10 | Governance | ADR-0029; `src/aida/steward_agent.py`, `tests/test_steward_agent.py` |
| Lineage agent — contracted parsing of the view definitions and routine bodies ingestion captured | Yes — `sql_lineage_parser` over eligible definitions (active, available, literal-redacted, screened clean) of views with no parsed lineage, and `procedure_lineage` over eligible routine bodies with no routine-aware lineage, proposing only their table-to-table edges; edges written PROPOSED whatever the auto-activation settings say; tier-gated, killable mid-run, bounded by its pending edges, ledgered; decides nothing | Yes — `GET`/`POST /v1/organizations/{org}/lineage-agent[/run]` mounted in `aida.main`; a console screen in the Steward work area; its edges are decided in the parsed-lineage review queue (ADR-0026). Runs are started by a person, or by the scheduler once its interval setting is above 0 (it is 0, off, by default) | **No** — nothing ships registered: an organization must give an approved `AGENT`-kind AI asset version a contract for `agent:lineage` | **No** — in-memory SQLite, and the routine table's review migration applied to PostgreSQL by the ORM-drift gate; no run against a real estate | 2026-09-11 | Governance | ADR-0029 amendments; `src/aida/lineage_agent.py`, `src/aida/routine_lineage_edges.py`, `tests/test_lineage_agent.py` |
| Quality agent — contracted proposals of DQ-4 threshold rules | Yes — row-count floors and null-rate ceilings derived from each table's recent completed profiles, value-free; every proposal is a T2 `QUALITY_RULE_PROPOSAL` review a person decides, and approval creates the rule; a rule key already covered by a rule or a proposal is never proposed again | Yes — `GET`/`POST /v1/organizations/{org}/quality-agent[/run]` mounted in `aida.main`; a console screen in the Steward work area. Runs are started by a person, or by the scheduler once its interval setting is above 0 (it is 0, off, by default) | **No** — nothing ships registered: as above, for `agent:quality` | **No** — in-memory SQLite, and its migration applied to PostgreSQL by the ORM-drift gate; no run against a real estate | 2026-09-11 | Data quality | ADR-0029 amendment; `src/aida/quality_agent.py`, `src/aida/quality_rule_proposals.py`, `tests/test_quality_agent.py` |
### Certification not held

These are listed so their absence is a recorded state rather than an omission. All four columns are
**No** and the owner is a decision the bank has not yet supplied.

| Capability | Date | Evidence |
|---|---|---|
| Bank-scale load, soak, spike and chaos testing | 2026-09-12 | REVIEW §5: "No bank-scale load, soak, recovery, or connector certification was run". Re-checked 2026-09-12: CI's `perf-baseline` is a regression gate on a committed baseline, not a load, soak or chaos run |
| Penetration test / SAST / DAST | 2026-09-12 | REVIEW §6: "This is a technical review, not a claim of regulatory or penetration-test certification". Re-checked 2026-09-12: CI holds `dependency-scan`, `frontend-dependency-scan` and `secret-scan` — dependency and secret scanning, which are not SAST, DAST or a penetration test; no CodeQL, Semgrep or ZAP job exists |
| Backup, restore and regional failover | 2026-09-12 | REVIEW §5 "Recovery"; POINTS-TRACKER T28. Re-checked 2026-09-12: no backup, restore or failover script or runbook exists in the tree |
| Interactive WCAG AA accessibility conformance | 2026-09-12 | Still not held, and now precisely scoped rather than merely asserted. [`24-accessibility-acceptance-2026-09-12.md`](24-accessibility-acceptance-2026-09-12.md) splits the row: its §1 automated half **is** done and gated in CI (axe-core WCAG 2.1 A/AA across all 44 navigable screens, a real Tab driver, focus-containment and accessible-name floors); its §2 human half — screen reader, real-rendering colour contrast, 100% zoom and multiple screens — needs a person and has not been executed. `color-contrast` is deliberately not run in the automated harness because jsdom lays nothing out. F21: "ARIA alone does not establish working interaction" |
| Fresh-browser authentication through the real topology | 2026-09-12 | F06; POINTS-TRACKER §7 marks this ⚠ "Needs a deployed topology". Re-checked 2026-09-12: CI `ui-journey` does drive a real browser through the production nginx image, but the seat is a proxy-injected identity in front of a stub upstream, and `compose.oidc.yaml`'s mock-IdP overlay was not brought up — so no fresh browser has authenticated through a real issuer against the real backend |

## Keeping this register true

1. **A row changes when the code changes, in the same change.** If a pull request makes something
   reachable, it moves the Reachable cell in that pull request.
2. **Verified never moves without new evidence a reader can open.** A tracker row saying DONE is
   not evidence. A CI job name, a recorded run, or a retrieved object is.
3. **Removing a row requires removing the capability.** A contested or embarrassing row stays.
4. **The accomplishment log is not this document.** `06-accomplishment-log.md` is append-only
   history and records what was true on the day it was written. This register records what is true
   now, and is edited in place.

## Appendix: what the 2026-09-12 re-measurement changed

Recorded here because the value of a register is in what moved and why, and because two cells
turned out to have been **wrong on the day they were written** rather than merely out of date.
That distinction is the one worth keeping.

**Wrong when measured, now corrected.**

1. *Maker-checker review (bulk decision)* said "a concurrent PostgreSQL reproduction is still
   outstanding". `tests/test_governance_decision_postgres_concurrency.py` was added on
   2026-09-06 — the same day the row was measured — and is exactly that reproduction. The row
   understated the project for six days.
2. *Ask Atlas returns result rows* said "the response carries no `applied_row_limit`". It has
   carried `applied_row_limit` and `row_limit_source` since 2026-09-06, and the UI already
   decided truncation from them.
3. *Hybrid retrieval* said `Configured: Yes`. The vector third of the fusion has never been
   configured by anything this repository ships, and `compose.yaml` still defines no
   `AIDA_EMBEDDING_*` variable, so the cell claimed a capability the shipped topology could not
   exercise. Corrected to Partial.

**Stale, not wrong** (true when written, overtaken since): the browser OIDC row's "no IdP is
configured in this repository" — `compose.oidc.yaml` landed 2026-09-10; the `ui-next` row's "no
interactive or accessibility certification"; and the MCP row's missing JSON-RPC `initialize`.

**Verified moved on evidence produced in this pass** — three rows, each with the run recorded:

* *MCP endpoint* → Yes. `POST http://localhost:3001/mcp` through the running `ui-next` nginx with
  an `X-Principal-Id` header returned HTTP 200 and a JSON-RPC `initialize` result
  (`protocolVersion 2025-03-26`).
* *Maker-checker, single and bulk* → Yes. Run against the real PostgreSQL in the local stack,
  into a scratch database so no shared data was touched:

  ```
  AIDA_DECISION_CONCURRENCY_TEST_DATABASE_URL=postgresql+asyncpg://…/aida_r11b14_decision_race \
    python -m pytest tests/test_governance_decision_postgres_concurrency.py -p no:randomly
  # 14 passed in 10.07s
  ```

**A new defect this pass found, which belongs in tracker section P rather than here.** The
`gemini-bank-sql` route is APPROVED through maker-checker but its approved model identifier,
`gemini-2.0-flash`, has been retired by the provider and answers HTTP 404. Because 404 is not in
the orchestrator's fallback set, a deployment with that route primary fails every generated-SQL
Ask closed and never tries its approved fallback. Approving a route does not keep the upstream
model alive, and nothing in the platform currently notices when one disappears. R11-B2's
benchmark deliberately places no provider call ("kept out of a routine benchmark run to avoid an
unbounded-cost, unbounded-network side effect"), so the two calls made in this pass appear to be
the first live generation attempts against either approved route — which is why a retired model
had gone unnoticed.

**What could not be measured, and what it would take.** Disaster recovery, backup/restore and
regional failover need a deployed topology and an approved RPO/RTO. Oracle, BigQuery, Snowflake
and Databricks need real accounts. The SOC and notification destinations need a real collector and
a real channel. The WORM archive against real AWS S3 needs an AWS bucket, where refusals are
`403 AccessDenied` and IAM is in play rather than Object Lock alone. The human half of
accessibility needs a person with assistive technology. Generated-answer quality needs an approved
route whose model the provider still serves. Each of these is a legitimate **No**, not a gap in
this pass.

## Related documents

* [`00-status.md`](00-status.md) — the narrative status page; this register is its current-state companion
* [`03-tracker.md`](03-tracker.md) — item-level open work, one row per ID
* [`../review-2026-09-05/REVIEW.md`](../review-2026-09-05/REVIEW.md) — the findings referenced above
* [`../review-2026-09-05/POINTS-TRACKER.md`](../review-2026-09-05/POINTS-TRACKER.md) — remediation status for those findings
