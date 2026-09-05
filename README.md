# Bank Data Intelligence Platform

Production-oriented foundation for a governed, agentic data analyst platform designed for a large banking organization.

The platform uses deterministic controls for discovery, profiling, authorization, pre-retrieval prompt-risk screening, SQL validation, execution, and audit. Model providers are optional, replaceable reasoning components and are never trusted execution boundaries.

Implemented vertical slices include a live AI analyst, governed metadata retrieval, approved-tool-first planning and execution, agent trace/evaluation evidence, concrete OpenAI Responses and Gemini GenerateContent adapters behind a provider-neutral structured model gateway, governed metadata-only business inference, dbt manifest intelligence, production OIDC/JWKS verification, enterprise secret-manager boundaries, value-free query lineage, PostgreSQL and SQL Server pull adapters, table-task scans, fleet scheduling, a canonical metadata contract with atomic synchronous and resumable checksum-addressed Temporal batch delivery, deterministic data quality, governed semantics/tools, maker-checker review, query memory, relationship review, impact analysis, a bounded value-free Graph Explorer, and operational/audit evidence. PostgreSQL remains authoritative; Temporal, Kafka, and Neo4j serve durable workflow and rebuildable transport/projection roles.

## Local quick start

1. Copy `.env.example` to `.env`. Put developer credentials only in the ignored `.env`; never put live credentials in `.env.example` or source control.
2. Start the platform:

   ```powershell
   docker compose up --build -d
   ```

   For local UI/API editing with automatic pickup, use the development overlay:

   ```powershell
   docker compose -f compose.yaml -f compose.dev.yaml up --build -d
   ```

   The complete legacy portal remains at `http://localhost:3000`. The React
   rebuild is also deployed by the default Compose file at
   `http://localhost:3001`; both use the same API service. With the development
   overlay, the React rebuild is available at `http://localhost:5174` with
   hot-module reload, and API changes under `src/` reload automatically.

3. Open:

   - API documentation: <http://localhost:8000/docs>
   - Legacy Atlas portal: <http://localhost:3000>
   - React Atlas portal: <http://localhost:3001> (or <http://localhost:5174> with the development overlay)
   - Temporal UI: <http://localhost:8080>
   - Neo4j browser: <http://localhost:7474>
   - MinIO console: <http://localhost:9001>
   - Redpanda console: <http://localhost:8081>

4. Verify the API:

   ```powershell
   Invoke-RestMethod http://localhost:8000/health/live
   Invoke-RestMethod http://localhost:8000/health/ready
   ./scripts/verify-local.ps1
   ```

5. Load a sample estate (optional, recommended for a first look). A fresh
   install has no metadata, so the catalog, knowledge graph and unified lineage
   render empty. Populate a value-free retail-and-risk estate — structure and
   keys only, never row values — through the governed API:

   ```powershell
   docker compose --profile seed run --rm seed
   ```

   Or run it directly against a locally running API:

   ```powershell
   python scripts/seed_sample_estate.py            # defaults to http://localhost:8000
   ```

   The seed is idempotent and safe to re-run. It creates a demonstration
   organization, registers a canonical-push datasource, ingests the estate,
   discovers relationship candidates from the declared foreign keys, and
   approves them so **Knowledge graph** and **Unified lineage** render a
   populated, connected estate. Point it only at a development or demonstration
   environment.

Development authentication is deliberately explicit. API examples must include identity headers documented in the generated OpenAPI specification. Production requires configured OIDC issuer/audience/JWKS verification and a registered non-environment credential provider; it refuses development authentication and `env` secret resolution.

Model generation is also fail closed. For local development, create and independently approve an organization model-route version whose provider is `OPENAI` or `GOOGLE_GEMINI`, whose route key matches `AIDA_MODEL_ROUTE`, and whose credential reference is `env://OPENAI_API_KEY` or `env://GEMINI_API_KEY`. Enable `AIDA_MODEL_GENERATION_ENABLED` only after the credential is valid and the route's residency, retention, capabilities, budgets, and model ID are approved. Route approval alone never activates model traffic.

dbt remains the warehouse transformation compiler and executor. In Atlas, open **Transformations**, register a dbt project against its governed datasource, then import dbt's generated `target/manifest.json`. Atlas stores bounded resource/lineage metadata, SQL hashes, and literal-redacted compiled SQL; it does not persist the raw artifact or execute artifact SQL. Source ingestion into a warehouse remains a connector/ELT responsibility rather than a dbt or Atlas model-execution responsibility.

Atlas goes beyond source metadata collection in **Business meaning**. After a completed scan, deterministic rules and an optional approved model route infer candidate business domains, entities, descriptions, table roles, grain, synonyms, analytical questions, and safe tool blueprints. Only metadata structure is supplied to the model. A checker must approve each proposal before it becomes authoritative; Atlas renders any promoted SQL deterministically and creates only a draft governed tool that follows the normal publication workflow.

Use **Knowledge graph** to search tables, schemas and catalogs, inspect classified metadata and impact, and focus a table into a policy-bounded one-to-four-hop neighborhood. The explorer intentionally displays metadata and approved aggregate evidence only; it never renders raw customer, account or transaction values.

Use **Data quality** to configure source or table baseline thresholds, inspect immutable profile comparisons, and acknowledge or resolve durable incidents. The first profile establishes a baseline; later scans compare volume, null-rate and schema fingerprints without retaining source values. Metadata scan age is reported separately. Source-row freshness remains `NOT_CONFIGURED` until a connector receives an approved watermark contract, so the portal never misrepresents scan time as business-data freshness.

Use **Source fleet** to inspect the honest connector matrix, run conformance certification, deliver envelope `1.0` synchronously or as a resumable manifest with numbered checksum-addressed chunks, inspect workflow/progress/change evidence, and configure durable pull schedules. Incremental delivery is the safe default. A full batch reconciles omissions only after every chunk succeeds and requires confirmation. PostgreSQL and Microsoft SQL Server pull are `BETA`; remaining database types are visibly planned rather than represented as complete.

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
| [Delivery](Docs/60-delivery/03-tracker.md) | Roadmap, epic backlog, tracker, status matrix, gap register, accomplishment log, connector backlog |
| [Reference](Docs/90-reference/01-glossary.md) | Glossary, decision log, research sources, analysis algorithms |
| [Competitors](Docs/competitors/00-application-planning-roadmap.md) | Per-vendor deep dives |

Four things to understand first:

1. **Deterministic services hold all authority; models only propose** ([ADR-0001](Docs/10-architecture/adr/ADR-0001-hybrid-deterministic-llm.md)).
2. **One execution choke point** — no code path reaches a source except through the query gateway ([ADR-0004](Docs/10-architecture/adr/ADR-0004-execution-choke-point.md)).
3. **PostgreSQL is authoritative; everything else is a rebuildable projection** ([ADR-0003](Docs/10-architecture/adr/ADR-0003-authoritative-state-and-projections.md)).
4. **The control plane is value-free** — metadata and bounded approved results leave a source; business data does not ([ADR-0014](Docs/10-architecture/adr/ADR-0014-value-free-control-plane.md)).

Current state is tracked honestly in the [status matrix](Docs/60-delivery/04-status-matrix.md) and the [tracker](Docs/60-delivery/03-tracker.md).

## Developer commands

```powershell
python -m pip install -e ".[dev]"
alembic upgrade heads
pytest
ruff check .
mypy src
```
