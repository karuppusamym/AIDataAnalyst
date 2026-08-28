# 12 — Enterprise Gap Register

## Executive conclusion

The original design direction is sound but too broad to implement safely as one program increment. The architecture has been simplified around a small number of hard platform boundaries: authoritative metadata, durable workflows, replayable events, isolated connectors, a mandatory query gateway, and a framework-neutral agent state machine. This reduces accidental complexity without reducing bank-grade controls.

No additional answer is required to continue foundation development. Unknown bank-specific decisions are treated as explicit assumptions and fail-closed extension points rather than silent guesses.

## Simplifications made

| Area | Simplification | Reason |
|---|---|---|
| Agent framework | Typed state machine and model-gateway contract; no LangGraph/ADK dependency in the core | Keeps policy, evidence, and workflow history portable |
| Workflow | Temporal owns durable business workflows; Kafka owns event distribution | Avoids using an agent graph or broker as a workflow database |
| System of record | PostgreSQL is authoritative; Neo4j/search/vector are projections | Enables reconciliation and deterministic rebuilds |
| Query execution | One gateway for SQL validation, authorization, cost checks, execution, masking, and lineage | Removes bypass paths |
| Metadata processing | Discovery and bounded profiling are deterministic; LLM enrichment is optional and reviewable | Prevents model output from becoming unverified truth |
| Multi-agent behavior | Specialized capabilities share one explicit state and permission envelope | Avoids autonomous agents with hidden permissions or unbounded loops |
| Delivery | Production vertical slices instead of a throwaway POC | Exercises controls and operability from the first release |

## Open enterprise gaps

| Priority | Gap | Current safe default | Production closure evidence |
|---|---|---|---|
| P0 | Enterprise identity and authorization | Signed OIDC/JWKS boundary is implemented and production requires it; local headers remain development-only | Bank issuer/claim/group activation, centralized ABAC/RBAC tests, revocation/replay policy, workload identity and break-glass process |
| P0 | Secrets and source identity | One configured provider, strict references, registered adapter boundary and bounded cache/rotation invalidation; production rejects `env` | Register/certify bank Vault/CyberArk/cloud adapter, workload identity, rotation/outage tests, read-only/delegated identities and access review |
| P0 | Network and connector placement | Single local network | Zone topology, egress allowlists, private endpoints, connector-agent mTLS, firewall evidence |
| P0 | Data entitlements and masking | Catalog allowlist plus conservative column masking | Source-aligned row/column policy, purpose/consent rules, dynamic masking test suite |
| P0 | Production platform | Single-node Docker | Kubernetes/managed service topology, multi-AZ design, capacity model, IaC and image provenance |
| P0 | DR and continuity | Durable local volumes only | Approved RPO/RTO, backup/restore drills, regional failover and Temporal/Kafka/Postgres recovery |
| P0 | Model route and AI governance | Provider-neutral structured gateway, bounded metadata grounding, OpenAI Responses and Gemini adapters, bounded retry/timeout/token contracts, durable v2 control evaluations, a pre-retrieval deterministic direct-prompt risk gate and versioned maker-checker routes with opaque credential references; generation stays fail-closed until an approved route is selected and its credential resolves | Rotate exposed development keys, approve provider/model/route selection, replace environment credentials with workload identity/private routing, certify retention/residency, pass multilingual/obfuscated/indirect injection and bank-domain evaluations, connect monitoring, and exercise the kill switch |
| P1 | Connector fleet | PostgreSQL, Microsoft SQL Server, and Oracle native pull plus canonical envelope `1.0`, atomic synchronous ingestion and resumable Temporal manifests/chunks are implemented; full reconciliation waits for all chunks, successful payloads are cleared, and Atlas exposes progress/evidence; Oracle ships `explain=False` pending a least-privilege EXPLAIN PLAN review; other engines are visibly `PLANNED` | Build Snowflake/Databricks/Teradata/Db2 pull adapters; certify an Oracle EXPLAIN PLAN path and flip `explain=True`; add signed producers, Kafka/schema-registry intake, quotas/pause/cancel controls, version fixtures, maximum-scale/recovery evidence and source-delegated identities |
| P1 | Fleet scheduling | Implemented HA-safe polling, quotas, maintenance windows, backpressure, priorities, cancellation reconciliation, and table-task concurrency | Prove fairness and capacity at bank scale; integrate enterprise maintenance calendars |
| P1 | Schema deletion/change handling | Implemented tombstones, reactivation, drift counts, stable identity, and impact APIs | Approve retention policy and add source-specific drift notification routing |
| P1 | Data-quality observability | Deterministic value-free volume/null/schema baselines, source/table policies, immutable observations, durable deduplicated incidents, recovery reconciliation, scan age, audited operator transitions and an Atlas workbench are implemented; source-row freshness fails closed as `NOT_CONFIGURED` | Approve connector watermark columns and classification/retention rules, alert/SLA routing, ownership escalation, custom rule packs, seasonality, incident-volume/load tests and an induced anomaly/recovery certification fixture |
| P1 | Semantic governance | Implemented metric versions plus governed metadata-only inference for domains, entities, descriptions, table roles, grain, synonyms, questions and safe tool blueprints; independent approval creates authoritative annotations and a cross-domain FK map | Add glossary term lifecycle, steward ownership/assignment, ambiguity and conflict workflows, confidence calibration and the bank stewardship operating model |
| P1 | Relationship/lineage evidence | Implemented source constraints, durable value-free query column lineage, tool dependencies, bounded candidates, durable review, confidence, impact analysis, server-side graph search and policy-bounded directional one-to-four-hop UI/API exploration | Add view/procedure and certified ETL/OpenLineage adapters; project approved relationships to Neo4j; add cross-source/time-aware traversal and million-node virtualization certification |
| P1 | dbt transformation intelligence | Immutable manifest ingestion, bounded model/source/test inventory, deterministic catalog matching, dependency lineage, raw-artifact exclusion, SQL literal redaction/fingerprints, impact integration, agent retrieval and Atlas workbench | Add authenticated CI artifact push, `run_results.json` health/SLA evidence, dbt Cloud/Core job adapters, column-level manifest lineage where available, latest-snapshot retention policy and very-large-DAG virtualization |
| P1 | Operations and compliance | Structured logs, metrics, audit/outbox, fleet evidence, retry/backoff, dead-letter visibility, and requeue control | OpenTelemetry export, SIEM/SOC integration, SLO alerts, WORM audit retention |
| P1 | Software supply chain | Pinned Python dependencies and non-root image | SBOM, signing, vulnerability policy, SAST/DAST, admission controls, patch SLAs |
| P2 | User experience | Atlas covers analyst execution, feedback, current lineage, metadata, dbt transformation evidence, business-semantic inference/review/map/tool promotion, Graph Explorer search/focus/directional expansion/evidence inspection, model-route governance, metric/tool lifecycles, query memory, outbox recovery, fleet controls, audit, and AI/security runtime posture; the exact capability matrix is maintained in `15-ui-capability-coverage.md` | Bind bank OIDC session UX and persona navigation; complete accessibility, usability, bulk-stewardship and million-node visual certification |
| P2 | Chargeback and quotas | Per-source query limits | LOB budgets, tenant quotas, showback, anomalous-spend controls |

## Decisions the bank will eventually supply

These inputs change adapters and deployment policy, not the core architecture: approved cloud/on-prem regions; identity provider and claims; policy engine; vault; source priority list; residency classes; retention; RPO/RTO; LOB isolation tiers; model providers/routes; SIEM; ITSM; Kubernetes and managed-service standards.

Until supplied, production mode must remain fail closed for identity, model generation, and development overrides.
