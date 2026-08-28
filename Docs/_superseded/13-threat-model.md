# 13 — Foundation Threat Model

## Scope and trust boundaries

Protected assets are source credentials, bank data, metadata/classifications, semantic definitions, query results, identity claims, policy decisions, workflow histories, audit evidence, and model prompts/responses.

Primary trust boundaries:

```text
User / Agent
  -> API identity and organization boundary
  -> Agent orchestration boundary
  -> Query Execution Gateway
  -> Connector / source network boundary

Control-plane transaction
  -> PostgreSQL authoritative state and outbox
  -> Kafka event boundary
  -> Neo4j/search/vector projections

Metadata context
  -> Model Gateway
  -> Approved private model route
```

## Priority threats and controls

| Threat | Current control | Required production reinforcement |
|---|---|---|
| Cross-LOB/organization access | Signed OIDC issuer/audience/JWKS verification, configurable validated organization/role claims, organization IDs on protected entities, enforced resource ownership, role gates, live 403 isolation test; local headers prohibited in production | Bank claim/group certification, centralized ABAC, database RLS defense in depth, revocation/replay policy and policy decision logs |
| SQL mutation or administrative access | SQLGlot AST deny rules, one read-only statement, connector read-only transaction | Source read-only roles, database resource groups, engine-specific certification and adversarial corpus |
| Unauthorized table access | Catalog-derived allowlist after AST extraction | Row/column/purpose entitlements synchronized from source and policy engine |
| Expensive/denial queries | forced limits, timeout, EXPLAIN cost ceiling, cross/unbounded join denial | per-LOB quotas, warehouse workload groups, concurrency controller and kill/cancel support |
| Sensitive result disclosure | deterministic classification, conservative masking, alias/derived-expression propagation, durable value-free output-to-source lineage | authoritative classification feeds, dynamic masking/tokenization, download and model-context policies |
| Sensitive values in control-plane evidence | raw questions not stored; keyed HMAC fingerprints; persisted SQL literals redacted; profiles contain statistics only | KMS-managed HMAC keys, encrypted parameter vault if replay is required, retention/legal-hold policy |
| Prompt injection/model tool abuse | no active model route by default; explicit typed state includes a pre-retrieval `SCREENED` gate; versioned deterministic rules block direct instruction override, system-prompt/credential extraction, security/masking bypass, privilege escalation and unbounded data extraction; only value-free reason codes/scores persist; all generated SQL crosses the deterministic gateway | indirect-injection scanning for retrieved metadata, multilingual/obfuscation corpus, bank-approved semantic classifier as defense in depth, signed prompts/policies, continuous model/tool evaluation and runtime kill switch |
| Governance approval confused with model activation | maker-checker route records expose an explicit activation status; approval does not select a runtime route, register an adapter, or enable generation | bank change control for route selection, private adapter registration, evaluation evidence, monitored canary and kill-switch drill |
| Hallucinated relationships/semantics | PK/FK relationships are source-derived; LLM output cannot directly publish | evidence scores, maker-checker workflow, negative knowledge and versioned semantic approvals |
| Credential theft | only strict references persisted; inline DSNs rejected; exactly one configured and explicitly registered provider; bounded cache and invalidation; production rejects `env` | Register/certify bank Vault/CyberArk/cloud adapter, workload identity, rotation/outage drills and no-secret telemetry tests |
| SSRF/lateral movement through connectors | connector type allowlist and credential-reference indirection | zone-local connector agents, egress allowlists, private endpoints, mTLS and destination policy |
| Event spoofing/replay | transactional outbox, stable event IDs, idempotent graph MERGE, committed consumer offsets | broker ACLs/mTLS, schema registry, event signatures where required, generic consumer deduplication |
| Projection corruption/staleness | PostgreSQL authoritative state, replayable events, graph reconciliation status and lag | scheduled reconciliation, replay runbook, SLO/alert on projection lag |
| Workflow loss/duplicate work | Temporal history, stable workflow IDs, activity retries/heartbeats, idempotent profile persistence | HA Temporal, namespace isolation, retry classification, cancellation and recovery drills |
| Audit tampering | attributable append-style audit rows plus correlation IDs | immutable/WORM export, SIEM integration, retention, cryptographic integrity and privileged-access monitoring |
| Dependency/image compromise | pinned application dependencies and non-root runtime user | image digests, SBOM, signatures, provenance, vulnerability admission policy and patch SLA |
| Resource exhaustion at fleet scale | bounded profile rows/column batches/table count; sequential source pressure by default | sharded fair scheduler, per-source concurrency, maintenance windows and tested backpressure |

## Fail-closed invariants

- Production configuration cannot use development identity, environment credential resolution, development SQL injection, weak audit keys, or an insecure remote JWKS URL.
- OIDC tokens that fail signature, issuer, audience, time, subject, organization, role, or algorithm validation are denied without detailed verification leakage.
- No approved and independently activated model route means natural-language generation returns an explicit denial.
- A direct prompt-risk denial stops before metadata retrieval, model context construction, tool selection, or SQL execution.
- An unknown, ambiguous, or cross-tenant object is denied.
- A query that fails parsing, policy, catalog, cost, or source read-only enforcement is not executed.
- A projection is never treated as authoritative; lag is visible through reconciliation counts.
- Connector capability flags advertise only behavior actually implemented.

## Security validation backlog

1. Property-based and adversarial SQL corpus across every certified dialect.
2. Tenant-isolation integration suite covering every list/read/write endpoint and background worker.
3. OIDC/JWKS rotation, claim-confusion, expired-token, audience, issuer, and replay tests.
4. Expand the passing direct prompt-risk suite with multilingual, obfuscated and indirect metadata/tool injection plus human-red-team evaluation before enabling a model route.
5. Secret/log scanning and proof that source values do not enter logs, traces, events, or profiles.
6. Load/chaos tests for source timeouts, Temporal retries, Kafka duplication, projection rebuild, and partial outages.
7. Backup restore, RPO/RTO, break-glass, and incident-response exercises with retained evidence.
