# Capability register — current state

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

**Scope of this pass.** Rows were checked by reading the code named in the Evidence column on
2026-09-06, and re-checked at the close of the `review-2026-09-05` remediation pass. The rows that
were previously marked *under remediation* now describe the landed implementation.

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
| REST API (FastAPI modular monolith) | Yes | Yes | Yes | Partial — local Compose only | 2026-09-06 | Platform | `src/aida/main.py`; `compose.yaml` `api` service with a `/health/live` healthcheck; CI `docker-build` job imports `aida.main` from the built image |
| Backend container image | Yes | Yes | Yes | Yes — `docker build` + import smoke run locally on 2026-09-06 | 2026-09-06 | Platform | `Dockerfile`; CI `docker-build` job; `scripts/check_image_packaging.py` |
| Public Tool SDK shipped in the image | Yes | n/a — a client library, not a server path | Yes | Yes — `import aida_tool_sdk` from the built image, 2026-09-06 | 2026-09-06 | Platform | `sdk/aida_tool_sdk/`; `pyproject.toml` `[tool.hatch.build.targets.wheel].packages`; `Dockerfile` `COPY sdk ./sdk`; `scripts/check_image_packaging.py` |
| Alembic migrations against real PostgreSQL | Yes | Yes | Yes | Yes — CI applies every migration to an empty Postgres 16 and diffs against `Base.metadata` | 2026-09-06 | Platform | `migrations/`; CI `migration-drift` job, `tests/test_migration_orm_drift.py` |
| Temporal durable workflows | Yes | Yes | Yes | Partial — local Compose only; no failover or continue-as-new at scale | 2026-09-06 | Platform | `compose.yaml` `temporal`, `metadata-worker`, `fleet-scheduler`; `src/aida/workflows/` |
| Kafka/Redpanda outbox publication | Yes | Yes | Yes | Partial — local Compose only; no database↔Kafka atomicity, per REVIEW §5 | 2026-09-06 | Platform | `compose.yaml` `outbox-publisher`; `src/aida/projectors/outbox_publisher.py` |
| Neo4j graph projection | Yes | Yes | Yes | **No** — INV-9 records the backend as uncertified; the projection-rebuild drill (E5) has never been run | 2026-09-06 | Platform | `compose.yaml` `graph-projector`; `src/aida/graph_store.py`; `00-status.md` §3 INV-9 |
| Disaster recovery / restore | No | No | No | **No** — needs a deployed topology and an approved RPO/RTO | 2026-09-06 | Bank decision | REVIEW §5 "Recovery"; POINTS-TRACKER T28 |

### Frontend and public endpoints

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| Atlas portal (`ui-next`) — the only portal | Yes | Yes | Yes | Partial — `vite build`, `tsc --noEmit` and `vitest` run in CI; no interactive or accessibility certification | 2026-09-06 | Frontend | `ui-next/`; `compose.yaml` `ui-next` on `:3001`; CI `ui-next` job |
| Legacy `ui/` portal | **Removed** | n/a | n/a | n/a | 2026-09-06 | — | `ls ui` returns nothing; no compose service, Dockerfile, CI job or script references it (checked 2026-09-06) |
| API reachable from the browser on one origin (`/v1/`) | Yes | Yes | Yes | Yes — nginx proxy exercised live against a stub upstream, 2026-09-06 | 2026-09-06 | Platform | `ui-next/nginx.conf`; `ui-next/vite.config.ts`; `scripts/check_proxy_contract.py`; CI `ui-proxy` job |
| MCP endpoint reachable at the URL the UI advertises (`/mcp`) | Yes | Yes | Yes | Partial — the **proxy hop** is verified live (nginx routes `/mcp` and `/mcp/` upstream with the Authorization header intact); a full JSON-RPC `initialize` through a deployed stack is **not** | 2026-09-06 | Platform | F07; `src/aida/mcp_server.py` `APIRouter(prefix="/mcp")`; `ui-next/nginx.conf`; CI `ui-proxy` job |
| Browser OIDC sign-in flow | Partial | Partial | **No** — no IdP is configured in this repository | **No** | 2026-09-06 | Frontend | F06; `src/aida/security.py` requires a bearer token in OIDC mode; POINTS-TRACKER F06 marks the full flow ⚠ blocked on IdP choice |

### Connectors

Maturity values are read from `src/aida/connectors/registry.py`, which is authoritative. Six
database connectors are registered `BETA`; two are declared `PLANNED`.

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| PostgreSQL connector (`BETA`) | Yes | Yes | Yes | Yes — discovery run against live Postgres 16 **and** 14 service containers | 2026-09-06 | Connectors | `connectors/postgres.py`; CI `connector-version-fixtures` job, `tests/test_postgres_version_fixtures.py` |
| Microsoft SQL Server connector (`BETA`) | Yes | Yes | Yes | Partial — exercised against the `sample-mssql-source` Compose fixture; no multi-version, TLS or delegated-identity certification | 2026-09-06 | Connectors | `connectors/sqlserver.py`; `compose.yaml` `sample-mssql-source` |
| Oracle connector (`BETA`) | Yes | Yes | **No** — no Oracle source ships in Compose (the sample source was retired) | **No** — never run against a live Oracle | 2026-09-06 | Connectors | `connectors/oracle.py`; registry notes: query estimate fails closed with `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` |
| BigQuery connector (`BETA`) | Yes | Yes | **No** — needs a real GCP project | **No** — never run against a live BigQuery | 2026-09-06 | Connectors | `connectors/bigquery.py`; registry notes honestly omit foreign-key metadata |
| Snowflake connector (`BETA`) | Yes | Yes | **No** — needs a real account/warehouse | **No** — never run against a live Snowflake | 2026-09-06 | Connectors | `connectors/snowflake.py` |
| Databricks connector (`BETA`) | Yes | Yes | **No** — needs a real workspace | **No** — the registry note says so in the code: "Code complete; never exercised against a live Databricks workspace" | 2026-09-06 | Connectors | `connectors/databricks.py` |
| Teradata, IBM Db2 | **No** — canonical push ingestion only | n/a | n/a | n/a | 2026-09-06 | Connectors | `connectors/registry.py` `declare_planned(...)`, `implementation_status="PLANNED"` |

### Governance, audit and delivery

Every row in this section is the subject of an open P0/P1 review finding. They are listed with the
finding rather than omitted, because a capability register that quietly drops the contested rows is
the exact failure mode D06 describes.

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| Audit ledger and transactional outbox | Yes | Yes | Yes | Partial — local end-to-end only | 2026-09-06 | Audit | `src/aida/` audit/outbox modules; `compose.yaml` `outbox-publisher` |
| WORM audit archive to an external immutable store | Yes — two complete providers: `filesystem` (two-phase lifecycle, full-envelope versioned checksum, membership, lease) and `s3` over S3 Object Lock, signed by a standard-library SigV4 implementation with no SDK dependency; `gcs`/`azure_blob` still resolve to a provider that refuses | Yes — archive loop runs from `main.py`, which also creates the Object Lock bucket when the backend is `s3` | Yes — defaults to `none`, which refuses rather than reporting success; `s3` reads the existing `object_store_*` settings | Partial — **against MinIO, not AWS.** All four F01 properties were exercised against a live S3-compatible service on 2026-09-09: object retrieved and re-checksummed; a raw overwrite created a new version while the locked version stayed intact and re-verified; deleting the locked version before retain-until was refused by the service (MinIO: `400 InvalidRequest`, "Object is WORM protected"); a legal hold blocked deletion after retention had lapsed, and release restored expiry. Not yet exercised against a real AWS S3 bucket, where refusals are `403 AccessDenied` and IAM/bucket policy, not just Object Lock, is in play. Filesystem immutability remains a guard rail, not a security boundary | 2026-09-09 | Audit | F01/F02/F03; `src/aida/worm_archive.py`, `src/aida/audit_archive_storage.py`, `src/aida/audit_archive_s3.py`, `src/aida/aws_sigv4.py`, `src/aida/audit_envelope.py`, `tests/test_audit_archive_s3.py`, `tests/test_aws_sigv4.py` |
| SIEM / SOC event routing | Yes — real webhook and syslog transports (RFC 5424; RFC 6587 octet counting on TCP) behind durable delivery intents with retry, backoff and dedup | Yes — worker runs from the scheduler | Partial — the shipped `siem_endpoint` placeholder `internal://security-log-pipeline` resolves to NOT_CONFIGURED, and `delivery_worker_enabled` defaults off, so an upgrade sends nothing | **No** — receipt is proven against loopback stub servers, not a real SOC collector | 2026-09-06 | Delivery | F04; `src/aida/siem_routing.py`, `siem_delivery.py`, `delivery_intents.py` |
| Governance notification delivery | Yes — intent created in the business transaction, delivered by an independent worker; requested/attempted/delivered timestamps separated | Yes — worker runs from the scheduler | Partial — default-off, as above | **No** — outage and recovery are exercised against a stub server, not a real channel | 2026-09-06 | Delivery | F12; `src/aida/governance_review_relay.py`, `governance_notifications.py`, `delivery_intents.py` |
| Maker-checker review (single decision) | Yes | Yes | Yes | Partial — local end-to-end; concurrent-PostgreSQL contention not reproduced | 2026-09-06 | Governance | `src/aida/semantic_api.py` single-decision path uses `SELECT ... FOR UPDATE` |
| Maker-checker review (bulk decision) | Yes — single, bulk, sample-review and reviewer-agent decisions share one service whose claim is a compare-and-set `UPDATE … WHERE status='PENDING'` | Yes | Yes | **No** — the invariant is proven on file-backed SQLite with two real connections; a concurrent PostgreSQL reproduction is still outstanding | 2026-09-06 | Governance | F05; `src/aida/governance_decision_service.py` |
| Workspace ABAC authorization | Yes | Yes | Yes | **No as an enforcing control** — every workspace is `SHADOW` and the unresolved-workspace posture defaults to `SHADOW`, so the system measures and does not deny. Deliberate; see F11 | 2026-09-06 | Platform | F11; `src/atlas/platform/config.py`; `src/aida/authorization_gate.py`; `00-status.md` §3 INV-4 |
| Enterprise secret manager boundary | Yes | Yes | **No** — production rejects `env://` and no bank provider is registered here | **No** — no rotation or outage drill | 2026-09-06 | Platform | `00-status.md` §4 "Enterprise secrets and source identity" |

### AI and analytics

| Capability | Implemented | Reachable | Configured | Verified | Date | Owner | Evidence |
|---|:--:|:--:|:--:|:--:|---|---|---|
| Query execution gateway (single choke point) | Yes | Yes | Yes | Partial — INV-2 is enforced by the type system, an import contract and an AST scan; cancel propagation is uncertified | 2026-09-06 | Platform | ADR-0004; `tests/test_tier0_invariants.py`; CI `quality` job runs `lint-imports` |
| Deterministic prompt-risk screening and SQL guard | Yes | Yes | Yes | Partial — measured against a synthetic corpus (zero bypasses, zero false positives), not against real bank traffic | 2026-09-06 | AI governance | `Docs/90-reference/model-risk-benchmark-results.md`; CI `quality-baseline` job |
| Model generation (OpenAI / Gemini) | Yes | Yes | **No by design** — `AIDA_MODEL_GENERATION_ENABLED` defaults to `false` and an independently approved organization model-route version is required | **No** — no live approved route exists in this environment, so generated-answer quality is unmeasured | 2026-09-06 | AI governance | `compose.yaml` `AIDA_MODEL_GENERATION_ENABLED: ${...:-false}`; README "Model generation is also fail closed" |
| Hybrid retrieval (lexical + vector + graph fusion) | Partial | Yes | Yes | Partial — deterministic precision/recall/MRR baseline in CI; no large-catalog benchmark | 2026-09-06 | Retrieval | CI `quality-baseline` job; `Docs/90-reference/quality-benchmark-results.md` |
| Ask Atlas returns result rows to the user | Yes — bounded result panel with masking chips, column tags and a truncation banner; no result retention added | Yes | Yes | **No** — not validated in a real browser; truncation is inferred from the SQL's `LIMIT` because the response carries no `applied_row_limit` | 2026-09-06 | Frontend | F20; `ui-next/src/components/QueryResultTable.tsx` |
| Steward agent — contracted proposals of table descriptions and glossary links | Yes — deterministic GL-9/GL-8 producers run under an `AgentContract`; tier-gated, killable mid-run, bounded, ledgered; decides nothing | Yes — `GET`/`POST /v1/organizations/{org}/steward-agent[/run]` mounted in `aida.main`; a console screen in the Steward work area. Runs are started by a person, or by the scheduler once its interval setting is above 0 (it is 0, off, by default) | **No** — nothing ships registered: an organization must approve an `AGENT`-kind AI asset version and give it a contract for `agent:steward` | **No** — in-memory SQLite and a fixture-mode browser check only; the mid-run kill rollback is not reproduced on PostgreSQL, and no run against a real estate | 2026-09-10 | Governance | ADR-0029; `src/aida/steward_agent.py`, `tests/test_steward_agent.py` |
| Lineage agent — contracted parsing of the view definitions ingestion captured | Yes — `sql_lineage_parser` over eligible definitions (active, available, literal-redacted, screened clean) of views with no parsed lineage; edges written PROPOSED whatever the auto-activation settings say; tier-gated, killable mid-run, bounded by its pending edges, ledgered; decides nothing | Yes — `GET`/`POST /v1/organizations/{org}/lineage-agent[/run]` mounted in `aida.main`; a console screen in the Steward work area; its edges are decided in the parsed-lineage review queue (ADR-0026). Runs are started by a person, or by the scheduler once its interval setting is above 0 (it is 0, off, by default) | **No** — nothing ships registered: an organization must give an approved `AGENT`-kind AI asset version a contract for `agent:lineage` | **No** — in-memory SQLite only; no run against a real estate | 2026-09-11 | Governance | ADR-0029 amendment; `src/aida/lineage_agent.py`, `tests/test_lineage_agent.py` |
| Quality agent — contracted proposals of DQ-4 threshold rules | Yes — row-count floors and null-rate ceilings derived from each table's recent completed profiles, value-free; every proposal is a T2 `QUALITY_RULE_PROPOSAL` review a person decides, and approval creates the rule; a rule key already covered by a rule or a proposal is never proposed again | Yes — `GET`/`POST /v1/organizations/{org}/quality-agent[/run]` mounted in `aida.main`; a console screen in the Steward work area. Runs are started by a person, or by the scheduler once its interval setting is above 0 (it is 0, off, by default) | **No** — nothing ships registered: as above, for `agent:quality` | **No** — in-memory SQLite, and its migration applied to PostgreSQL by the ORM-drift gate; no run against a real estate | 2026-09-11 | Data quality | ADR-0029 amendment; `src/aida/quality_agent.py`, `src/aida/quality_rule_proposals.py`, `tests/test_quality_agent.py` |

### Certification not held

These are listed so their absence is a recorded state rather than an omission. All four columns are
**No** and the owner is a decision the bank has not yet supplied.

| Capability | Date | Evidence |
|---|---|---|
| Bank-scale load, soak, spike and chaos testing | 2026-09-06 | REVIEW §5: "No bank-scale load, soak, recovery, or connector certification was run" |
| Penetration test / SAST / DAST | 2026-09-06 | REVIEW §6: "This is a technical review, not a claim of regulatory or penetration-test certification" |
| Backup, restore and regional failover | 2026-09-06 | REVIEW §5 "Recovery"; POINTS-TRACKER T28 |
| Interactive WCAG AA accessibility conformance | 2026-09-06 | F21: "ARIA alone does not establish working interaction" |
| Fresh-browser authentication through the real topology | 2026-09-06 | F06; POINTS-TRACKER §7 marks this ⚠ "Needs a deployed topology" |

## Keeping this register true

1. **A row changes when the code changes, in the same change.** If a pull request makes something
   reachable, it moves the Reachable cell in that pull request.
2. **Verified never moves without new evidence a reader can open.** A tracker row saying DONE is
   not evidence. A CI job name, a recorded run, or a retrieved object is.
3. **Removing a row requires removing the capability.** A contested or embarrassing row stays.
4. **The accomplishment log is not this document.** `06-accomplishment-log.md` is append-only
   history and records what was true on the day it was written. This register records what is true
   now, and is edited in place.

## Related documents

* [`00-status.md`](00-status.md) — the narrative status page; this register is its current-state companion
* [`03-tracker.md`](03-tracker.md) — item-level open work, one row per ID
* [`../review-2026-09-05/REVIEW.md`](../review-2026-09-05/REVIEW.md) — the findings referenced above
* [`../review-2026-09-05/POINTS-TRACKER.md`](../review-2026-09-05/POINTS-TRACKER.md) — remediation status for those findings
