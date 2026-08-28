# Enterprise Gap Register

> Status: **Living document.** Owner: Engineering lead + Product.
> Records deliberate simplifications, open enterprise gaps with their current safe default, and the evidence required to close each. A gap with a documented safe default is a managed risk; a gap without one is an incident waiting.

**Last reviewed:** 2026-08-28

## Executive position

The architecture is sound and deliberately narrower than the original design direction, which was too broad to implement safely as one program increment. It is organized around a small number of hard platform boundaries: authoritative metadata, durable workflows, replayable events, isolated connectors, a mandatory query gateway, and a framework-neutral agent state machine.

**No further input is required to continue development.** Unknown bank-specific decisions are treated as explicit assumptions and fail-closed extension points rather than silent guesses.

## Deliberate simplifications

Each of these is a decision, not an omission.

| Area | Simplification | Reason |
|---|---|---|
| Agent framework | Typed state machine and model-gateway contract; no LangGraph/ADK in the core | Keeps policy, evidence, and workflow history portable (ADR-0008) |
| Workflow | Temporal owns durable workflows; Kafka owns event distribution | Avoids using a broker or an agent graph as a workflow database (ADR-0007) |
| System of record | PostgreSQL authoritative; Neo4j/search/vector are projections | Enables reconciliation and deterministic rebuild (ADR-0003) |
| Query execution | One gateway for validation, authorization, cost, execution, masking, lineage | Removes bypass paths (ADR-0004) |
| Metadata processing | Discovery and profiling deterministic; LLM enrichment optional and reviewable | Prevents model output becoming unverified truth (ADR-0001) |
| Multi-agent behaviour | Specialized capabilities share one explicit state and permission envelope | Avoids autonomous agents with hidden permissions or unbounded loops |
| Service decomposition | Modular monolith with four deployment units and a planned extraction path | Distributed cost before boundaries are proven (ADR-0011) |
| Delivery | Production vertical slices, not a throwaway POC | Exercises controls and operability from the first release |

## Open enterprise gaps

| Pri | Gap | Current safe default | Production closure evidence |
|:--:|---|---|---|
| **P0** | Enterprise identity and authorization | Signed OIDC/JWKS boundary implemented and required in production; local headers are development-only | Bank issuer/claim/group activation; centralized ABAC/RBAC tests; revocation/replay policy; workload identity; break-glass |
| **P0** | Secrets and source identity | One configured provider, strict references, registered adapter boundary, bounded cache with rotation invalidation; production rejects `env://` | Register/certify the bank adapter; workload identity; rotation/outage tests; read-only delegated identities; access review |
| **P0** | Network and connector placement | Single local network | Zone topology; egress allowlists; private endpoints; connector-agent mTLS; firewall evidence |
| **P0** | Data entitlements and masking | Catalog allowlist plus conservative column masking with alias and derived-expression propagation | Source-aligned row/column policy; purpose and consent rules; dynamic masking test suite |
| **P0** | Production platform | Single-node Docker | Kubernetes/managed topology; multi-AZ; capacity model; IaC; image provenance |
| **P0** | DR and continuity | Durable local volumes only | Approved RPO/RTO; backup/restore drills; regional failover; Temporal/Kafka/PostgreSQL recovery |
| **P0** | Model route and AI governance | Provider-neutral structured gateway; bounded metadata grounding; OpenAI and Gemini adapters; bounded retry/timeout/token contracts; durable control evaluations; pre-retrieval deterministic prompt-risk gate; versioned maker-checker routes with opaque credential references. Generation stays fail-closed until an approved route is selected and its credential resolves | Rotate development keys; approve provider/model/route selection; replace environment credentials with workload identity and private routing; certify retention/residency; pass multilingual, obfuscated, and **indirect** injection plus bank-domain evaluations; connect monitoring; **exercise the kill switch** |
| **P0** | Glossary and stewardship | Governed table-stewardship slice: categories and immutable term synonyms/definitions; reviewed publication/deprecation; manual/bulk/exact-inferred links; individual/group/rule ownership; retained conflicts; expiring table certification; scoped coverage snapshots and bounded unowned backlog; audit/outbox and responsive control-center UI | Dedicated leaver/vacate and inherited ownership; automatic expiry and escalation workers; category administration; fuzzy/model inference calibration; broader asset types; bank-scale and interactive accessibility certification |
| **P0** | Context products and MCP | Real JSON-RPC 2.0 MCP server (`POST /mcp`) routing `tools/call` through the governed orchestrator/gateway stack; role-eligible tool exposure and invocation with anti-enumeration denial and audit/outbox evidence | Context products with maker-checker; per-read policy evaluation and consumption-lineage edges for `resources/read`; per-consumer budgets; `prompts/*` handlers |
| **P0** | Retrieval breadth | Lexical ranking with policy filtering before ranking; bounded evidence and selection reasons | Full-text index; vector projection; graph expansion; fusion ranking; large-catalog benchmarks |
| P1 | Connector fleet | PostgreSQL and SQL Server native pull plus canonical envelope `1.0`, atomic ingestion, resumable Temporal manifests/chunks; Oracle, BigQuery and Snowflake adapters present but each unverified against a live source; Databricks, Teradata, Db2 visibly `PLANNED` | Build Databricks, Teradata, Db2 adapters; live-verify Oracle/BigQuery/Snowflake; signed producers; Kafka/schema-registry intake; quotas/pause/cancel; version fixtures; maximum-scale recovery evidence; delegated source identities |
| P1 | Fleet scheduling | HA-safe polling, quotas, maintenance windows, backpressure, priorities, cancellation reconciliation, table-task concurrency | Prove fairness and capacity at bank scale; integrate enterprise maintenance calendars |
| P1 | Schema deletion and change handling | Tombstones, reactivation, drift counts, stable identity, impact APIs | Approve retention policy; add source-specific drift notification routing |
| P1 | Data-quality observability | Deterministic value-free baselines, source/table policies, immutable observations, deduplicated durable incidents, recovery reconciliation, scan age, audited operator transitions, Atlas workbench; source-row freshness fails closed as `NOT_CONFIGURED` | Approve connector watermark columns and classification/retention rules; alert/SLA routing; ownership escalation; custom rule packs; seasonality; incident-volume/load tests; induced anomaly and recovery certification; **runtime coupling** |
| P1 | Semantic governance | Versioned metrics plus governed metadata-only inference for domains, entities, descriptions, roles, grain, synonyms, questions; independent approval creates authoritative annotations and a cross-domain FK map; approved glossary terms can be linked to physical assets | Binding terms to semantic objects; ambiguity and conflict workflows; confidence calibration; bank stewardship operating model |
| P1 | Relationship and lineage evidence | Source constraints, durable value-free query column lineage, tool dependencies, bounded candidates, durable review, confidence, impact analysis, server-side graph search, policy-bounded 1–4 hop exploration | View/procedure and certified ETL/OpenLineage adapters; project approved relationships to Neo4j; cross-source and time-aware traversal; million-node virtualization certification |
| P1 | dbt transformation intelligence | Immutable manifest ingestion, bounded inventory, deterministic catalog matching, dependency lineage, raw-artifact exclusion, SQL redaction and fingerprints, impact integration, agent retrieval, Atlas workbench | Authenticated CI artifact push; `run_results.json` health/SLA evidence; dbt Cloud/Core job adapters; column-level manifest lineage; snapshot retention; very-large-DAG virtualization |
| P1 | Operations and compliance | Structured logs, metrics, audit/outbox, fleet evidence, retry/backoff, dead-letter visibility, requeue control | OpenTelemetry export; SIEM/SOC integration; SLO alerts; WORM audit retention; compliance packs |
| P1 | Software supply chain | Pinned dependencies, non-root image | SBOM; signing; vulnerability policy; SAST/DAST; admission controls; patch SLAs |
| P1 | Studio | Form-based authoring in the portal | Change sets, test harness, diff view, parameter designer, impact preview, Git binding |
| P2 | User experience | Atlas covers implemented workflows with a persona switcher, accessible command palette, table virtualization, and responsive stewardship control center | Bind persona to the bank OIDC group contract; complete interactive WCAG/usability, very-large bulk-selection, and million-node visual certification |
| P2 | Chargeback and quotas | Per-source query limits | LOB budgets; tenant quotas; showback; anomalous-spend controls |

## Newly identified gaps

Not present in the previous register; added in this revision.

| Pri | Gap | Why it matters |
|:--:|---|---|
| P0 | **Aggregate exfiltration detection** | Per-query bounds do not stop a thousand compliant queries extracting what one non-compliant query would not (threat T20) |
| P0 | **Indirect prompt injection through retrieved metadata** | A malicious column description reaching model context bypasses the pre-retrieval screen (threat T7 residual) |
| P1 | **MCP consumer threat surface** | Arrives with module 19; needs workload identity, per-read policy, budgets, and consumption lineage (threat T18) |
| P1 | **Privileged-operator misuse** | Operators are audited but not monitored; no access review or separation-of-duties enforcement (threat T19) |
| P1 | **Legal hold** | No mechanism to suspend retention for a matter under investigation |
| P2 | **Data contracts** | Expected in the segment; composes existing modules but is unbuilt |

## Decisions the bank will eventually supply

These change adapters and deployment policy, **not the core architecture**: approved cloud/on-prem regions; identity provider and claims; policy engine; vault; source priority list; residency classes; retention; RPO/RTO; LOB isolation tiers; model providers and routes; SIEM; ITSM; Kubernetes and managed-service standards.

Until supplied, production mode remains fail closed for identity, model generation, and development overrides.

## Related documents

- Tracker: `60-delivery/03-tracker.md`
- Status matrix: `60-delivery/04-status-matrix.md`
- Threat model: `50-security/02-threat-model.md`
- Compliance and evidence: `50-security/04-compliance-and-evidence.md`
