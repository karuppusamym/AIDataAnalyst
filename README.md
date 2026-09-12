# Bank Data Intelligence Platform

> **Delivery planning, 2026-09-11:** [review reconciliation](Docs/60-delivery/23-review-reconciliation-2026-09-11.md) explains the dispositions from all prior reviews. [Tracker section P](Docs/60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) is the current work queue; older plans and completion counts are dated evidence.

Production-oriented foundation for a governed, agentic data analyst platform designed for a large banking organization.

The platform uses deterministic controls for discovery, profiling, authorization, pre-retrieval prompt-risk screening, SQL validation, execution, and audit. Model providers are optional, replaceable reasoning components and are never trusted execution boundaries.

Implemented vertical slices include a live AI analyst, governed metadata retrieval, approved-tool-first planning and execution, agent trace/evaluation evidence, concrete OpenAI Responses and Gemini GenerateContent adapters behind a provider-neutral structured model gateway, governed metadata-only business inference, dbt manifest intelligence, production OIDC/JWKS verification, enterprise secret-manager boundaries, value-free query lineage, PostgreSQL and SQL Server pull adapters, table-task scans, fleet scheduling, a canonical metadata contract with atomic synchronous and resumable checksum-addressed Temporal batch delivery, deterministic data quality, governed semantics/tools, maker-checker review, query memory, relationship review, impact analysis, a bounded value-free Graph Explorer, and operational/audit evidence. PostgreSQL remains authoritative; Temporal, Kafka, and Neo4j serve durable workflow and rebuildable transport/projection roles.

## Local quick start

1. Copy `.env.example` to `.env`. Put developer credentials only in the ignored `.env`; never put live credentials in `.env.example` or source control.
2. Start the platform:

   ```powershell
   docker compose up --build -d
   ```

   That starts **ten services** — `postgres`, `temporal`, `migrate`, `api`,
   `ui-next`, `metadata-worker`, `fleet-scheduler` and the three sample source
   containers. Redis, Neo4j, Redpanda, MinIO and their companion processes are
   **not** started, because with default settings none of them is reached: the
   graph backend defaults to `postgres`, the lineage cache and MCP budget
   default to off, and the audit archive destination defaults to `none`. Each
   is one `--profile` flag away.

   | Profile | Brings back | Off in the default stack |
   |---|---|---|
   | `cache` | `redis` | Redis-backed lineage cache and MCP rate-limit counters. The MCP limits themselves are unchanged and still enforced; without this profile there is no store to enforce them in, so set `AIDA_LINEAGE_CACHE_ENABLED=true` / `AIDA_MCP_BUDGET_ENABLED=true` alongside it. |
   | `graph` | `neo4j`, `graph-projector`, plus `redpanda` and `outbox-publisher` to feed them | **Neo4j graph reads.** Graph and lineage screens still work — they are served by the certified `postgres` graph adapter, which reads the same relational tables. Add `AIDA_LINEAGE_NEO4J_READ_ENABLED=true` to actually read from Neo4j. |
   | `events` | `redpanda`, `redpanda-console`, `outbox-publisher` | **Kafka projection and the topic browser.** Events are still written to the `outbox_event` table by every producer; they simply stay `PENDING` until a publisher runs. PostgreSQL is authoritative (INV-1), so nothing is lost and the backlog drains when the profile is enabled. |
   | `archive` | `minio` | **The audit archive destination.** `audit_archive_storage_backend` defaults to `none`, which refuses rather than reporting success. Set `AIDA_AUDIT_ARCHIVE_STORAGE_BACKEND=s3` alongside it. |
   | `temporal-ui` | `temporal-ui` | The Temporal Web UI. Temporal itself is in the default stack — only the web console is optional. |
   | `full` | all of the above | — |
   | `seed` | `seed` | Demonstration data (step 5 below). |

   ```powershell
   docker compose --profile graph up -d --build     # one optional group
   docker compose --profile full up -d --build      # the whole 18-service stack
   ```

   Profiles combine (`--profile cache --profile events`). Nothing has been
   removed: the Kafka consumers, the Redis-backed limits and Temporal are all
   still here — this only changes what a plain `docker compose up` starts.

   For local UI/API editing with automatic pickup, use the development overlay:

   ```powershell
   docker compose -f compose.yaml -f compose.dev.yaml up --build -d
   ```

   The Atlas portal (`ui-next`, React) is deployed by the default Compose file
   at `http://localhost:3001`. With the development overlay it is available at
   `http://localhost:5174` with hot-module reload, and API changes under
   `src/` reload automatically.

   Both of the above run the **development identity provider**: the browser
   sends `X-Principal-Id`/`X-Roles` headers and the API trusts them. That is
   the default and it stays the default. To run the stack the way a deployment
   runs it — the API verifying a signed bearer token, the browser obtaining one
   through an authorization-code + PKCE redirect — add the OIDC overlay, which
   also starts a local development issuer:

   ```powershell
   docker compose -f compose.yaml -f compose.oidc.yaml up --build -d
   ```

   Then open <http://localhost:3001>, press **Sign in**, and enter any subject
   plus a claims document that names the caller's roles, groups and
   organization, for example:

   ```json
   {"roles":["atlas-admin"],"groups":["atlas-stewards"],
    "organization_id":"<an organization id from /v1/organizations>"}
   ```

   `atlas-viewer` in place of `atlas-admin` produces a least-privilege
   principal. The issuer is `ghcr.io/navikt/mock-oauth2-server`: a real OIDC
   provider (RS256, JWKS, discovery, real expiry) with **no user directory,
   password, MFA or revocation** — it mints a token for whoever asks. It
   exercises the protocol integration; it is not a stand-in for a corporate
   IdP, and `compose.oidc.yaml` must never be deployed.

3. Open:

   - API documentation: <http://localhost:8000/docs>
   - Atlas portal: <http://localhost:3001> (or <http://localhost:5174> with the development overlay)
   - MCP endpoint for external agents: `POST http://localhost:3001/mcp` — the same origin as the
     portal, which is what the **Agent gateway** screen copies. nginx proxies it to the API
     (`ui-next/nginx.conf`), so the URL the screen shows is the URL that works. It requires the
     same bearer token as the REST API.
   The four consoles below belong to optional services, so each needs its
   profile from step 2 before the URL resolves:

   - Temporal UI: <http://localhost:8080> — `--profile temporal-ui`
   - Neo4j browser: <http://localhost:7474> — `--profile graph`
   - MinIO console: <http://localhost:9001> — `--profile archive`
   - Redpanda console: <http://localhost:8081> — `--profile events`

4. Verify the API:

   ```powershell
   Invoke-RestMethod http://localhost:8000/health/live
   Invoke-RestMethod http://localhost:8000/health/ready
   ./scripts/verify-local.ps1
   ```

5. Load the sample estate (optional, recommended for a first look). A fresh
   install has no metadata, so the catalog, knowledge graph and unified lineage
   render empty. The `sample-source` container runs two Postgres databases
   (`bank_demo` for Customer, `risk_demo` for Risk) and `sample-mssql-source`
   runs SQL Server (`bank_demo_mssql` for Payments) — three real business
   domains, each registered as its own datasource so cross-source lineage
   stays genuine, with overlapping `customer_id`/`account_id` values across
   all three. The seed script registers all three as real datasources, runs
   live discovery against each (not a pushed fixture), and requests/approves
   the cross-boundary grants and cross-source relationship candidates that
   connect them:

   ```powershell
   docker compose --profile seed run --rm seed
   ```

   Or run it directly against a locally running API:

   ```powershell
   python scripts/seed_sample_estate.py            # defaults to http://localhost:8000
   ```

   The seed is idempotent and safe to re-run. It creates a demonstration
   organization with one Customer, one Payments, and one Risk data domain;
   registers and discovers each domain's real datasource; approves the
   same-source relationship candidates FK introspection finds; grants
   Customer↔Payments and Customer↔Risk cross-boundary visibility (deliberately
   leaving Payments↔Risk ungranted, so **Unified lineage** shows a real
   `withheld_cross_boundary_domain_ids` case); and approves the resulting
   cross-source relationship and object-resolution candidates so **Knowledge
   graph**, **Cross-source**, and **Unified lineage** all render a populated,
   genuinely cross-database estate. Point it only at a development or
   demonstration environment.

Development authentication is deliberately explicit. API examples must include identity headers documented in the generated OpenAPI specification. Production requires configured OIDC issuer/audience/JWKS verification and a registered non-environment credential provider; it refuses development authentication and `env` secret resolution.

Model generation is also fail closed. For local development, create and independently approve an organization model-route version whose provider is `OPENAI` or `GOOGLE_GEMINI`, whose route key matches `AIDA_MODEL_ROUTE`, and whose credential reference is `env://OPENAI_API_KEY` or `env://GEMINI_API_KEY`. Enable `AIDA_MODEL_GENERATION_ENABLED` only after the credential is valid and the route's residency, retention, capabilities, budgets, and model ID are approved. Route approval alone never activates model traffic.

dbt remains the warehouse transformation compiler and executor. In Atlas, open **Transformations**, register a dbt project against its governed datasource, then import dbt's generated `target/manifest.json`. Atlas stores bounded resource/lineage metadata, SQL hashes, and literal-redacted compiled SQL; it does not persist the raw artifact or execute artifact SQL. Source ingestion into a warehouse remains a connector/ELT responsibility rather than a dbt or Atlas model-execution responsibility.

Atlas goes beyond source metadata collection in **Business meaning**. After a completed scan, deterministic rules and an optional approved model route infer candidate business domains, entities, descriptions, table roles, grain, synonyms, analytical questions, and safe tool blueprints. Only metadata structure is supplied to the model. A checker must approve each proposal before it becomes authoritative; Atlas renders any promoted SQL deterministically and creates only a draft governed tool that follows the normal publication workflow.

Use **Knowledge graph** to search tables, schemas and catalogs, inspect classified metadata and impact, and focus a table into a policy-bounded one-to-four-hop neighborhood. The explorer intentionally displays metadata and approved aggregate evidence only; it never renders raw customer, account or transaction values.

Use **Data quality** to configure source or table baseline thresholds, inspect immutable profile comparisons, and acknowledge or resolve durable incidents. The first profile establishes a baseline; later scans compare volume, null-rate and schema fingerprints without retaining source values. Metadata scan age is reported separately. Source-row freshness remains `NOT_CONFIGURED` until a connector receives an approved watermark contract, so the portal never misrepresents scan time as business-data freshness.

Use **Source fleet** to inspect the honest connector matrix, run conformance certification, deliver envelope `1.0` synchronously or as a resumable manifest with numbered checksum-addressed chunks, inspect workflow/progress/change evidence, and configure durable pull schedules. Incremental delivery is the safe default. A full batch reconciles omissions only after every chunk succeeds and requires confirmation.

`src/aida/connectors/registry.py` is the authoritative maturity list, and this paragraph is kept
equal to it. **Six** native pull connectors are registered `BETA` — PostgreSQL, Oracle, Microsoft
SQL Server, Google BigQuery, Snowflake and Databricks SQL. **Teradata and IBM Db2** are declared
`PLANNED` with `implementation_status="PLANNED"`: canonical push ingestion works, there is no native
pull adapter, and they are shown as planned rather than represented as complete.

`BETA` means the adapter is implemented and reachable, not that it has met a real source. Only
PostgreSQL has run discovery against live database servers (two majors, in CI) and only SQL Server
has a Compose fixture. Oracle, BigQuery, Snowflake and Databricks have **never been exercised
against a live instance** — the Databricks registration says so in the code, and the
[capability register](Docs/60-delivery/20-capability-register.md) records the same distinction for
every row.

## Documentation

Full documentation lives in [`Docs/`](Docs/README.md) — start there for navigation by role.

| Area | Contents |
|---|---|
| [Product](Docs/00-product/01-vision-and-goals.md) | Vision, personas, market landscape, competitive matrix, differentiation, surfaces, packaging |
| [Architecture](Docs/10-architecture/01-principles-and-invariants.md) | Nine invariants, logical architecture, module decomposition, data and event models, deployment, capacity, runtime sequences, 16 ADRs |
| [Modules](Docs/20-modules/00-module-index.md) | One spec per bounded context (21 modules) |
| [Contracts](Docs/30-contracts/01-contract-strategy.md) | API conventions, module interfaces, event catalog, ingestion envelope, lineage, tools and agents |
| [Engineering](Docs/40-engineering/01-development-spec.md) | Development spec, repo layout, coding standards, testing, CI/CD, refactor plan, local runbook |
| [Security](Docs/50-security/01-security-architecture.md) | Security architecture, threat model, AI safety controls, compliance and evidence |
| [Delivery](Docs/60-delivery/00-status.md) | Status, capability register, roadmap, epic backlog, tracker, accomplishment log, connector backlog |
| [Reference](Docs/90-reference/01-glossary.md) | Glossary, decision log, research sources, analysis algorithms |
| [Competitors](Docs/review-2026-08/research/04-cross-vendor-synthesis.md) | Per-vendor deep dives (Collibra, Atlan, Alation/Purview/Unity) and the cross-vendor synthesis |

Four things to understand first:

1. **Deterministic services hold all authority; models only propose** ([ADR-0001](Docs/10-architecture/adr/ADR-0001-hybrid-deterministic-llm.md)).
2. **One execution choke point** — no code path reaches a source except through the query gateway ([ADR-0004](Docs/10-architecture/adr/ADR-0004-execution-choke-point.md)).
3. **PostgreSQL is authoritative; everything else is a rebuildable projection** ([ADR-0003](Docs/10-architecture/adr/ADR-0003-authoritative-state-and-projections.md)).
4. **The control plane is value-free** — metadata and bounded approved results leave a source; business data does not ([ADR-0014](Docs/10-architecture/adr/ADR-0014-value-free-control-plane.md)).

Current state is tracked in three places, which answer different questions:

- [Capability register](Docs/60-delivery/20-capability-register.md) — **what is true right now**, with
  *implemented*, *reachable*, *configured* and *verified* kept as four separate columns, plus a date,
  an owner and evidence per row. Read this before believing any capability claim elsewhere.
- [Delivery status](Docs/60-delivery/00-status.md) — the narrative summary and capability matrix.
- [Tracker](Docs/60-delivery/03-tracker.md) — item-level open work, one row per ID.

The [accomplishment log](Docs/60-delivery/06-accomplishment-log.md) is append-only history. Something
appearing there is not evidence that it is still true.

## Developer commands

```powershell
python -m pip install -e ".[dev]"
alembic upgrade heads
pytest
ruff check .
mypy src
```
