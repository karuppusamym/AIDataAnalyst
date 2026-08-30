# Competitive Feature Matrix

> Status: Authoritative. Owner: Product. Baseline: 2026-08-28.
> Legend: `●` strong / mature · `◐` partial or preview · `○` weak or absent · `—` not applicable to that product's model.
> Atlas column reflects **current implemented state** (see `60-delivery/00-status.md`), not roadmap.

## 1. How to read this

This matrix has one purpose: decide what to build next. It is scored against vendor-stated public capability, so it is generous to competitors and honest about Atlas. Two derived columns drive the roadmap:

- **Gate** — `ENTRY` means a buyer rejects Atlas without it; `DIFF` means it is where Atlas wins; `PARITY` means match-and-move-on; `SKIP` means deliberately not competing.
- **Priority** — P0/P1/P2 feeding `60-delivery/02-epic-backlog.md`.

## 2. Discovery and catalog

| Capability | Atlan | Collibra | Alation | Unity Catalog | Horizon | OpenMetadata | **Atlas** | Gate | Pri |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|---|---|
| Connector count (native) | ● 80+ | ● 100+ | ● | ◐ own estate | ◐ preview | ● | ○ 2 (PG, MSSQL) | ENTRY | P0 |
| Connector certification harness | ○ | ○ | ◐ open framework | — | — | ○ | ◐ control-plane suite | DIFF | P0 |
| Cross-source global search | ● | ● | ● | ◐ | ● universal search | ● | ◐ lexical only | ENTRY | P0 |
| Semantic / vector search | ● | ◐ | ● | ◐ | ● hybrid | ◐ | ○ | ENTRY | P0 |
| Policy-aware search (filter before rank) | ◐ | ◐ | ◐ | ● | ● | ○ | ● | DIFF | — |
| Usage/popularity ranking | ● | ◐ | ● behavioural | ● column-level | ● query-log | ◐ | ○ | PARITY | P1 |
| Asset certification badges | ● | ● | ● | ◐ | ◐ | ● | ◐ tools/metrics only | PARITY | P1 |
| Million-object UX (virtualization) | ● | ● | ● | ● | ● | ◐ | ○ | ENTRY | P1 |
| Bulk actions (tag, own, classify) | ● | ● | ● | ◐ | ◐ | ◐ | ○ | ENTRY | P1 |
| Data marketplace / data products | ● | ● | ● | ◐ | ◐ | ○ | ○ | PARITY | P2 |

## 3. Lineage

| Capability | Atlan | Collibra | Alation | Unity Catalog | Horizon | **Atlas** | Gate | Pri |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|---|
| Table-level lineage | ● | ● | ● | ● | ● | ● | ENTRY | — |
| Column-level lineage | ● | ● | ● | ● | ● | ◐ query SELECTs only | ENTRY | P0 |
| SQL/query-log lineage | ● | ● | ● | ● | ● | ● | — | — |
| View / stored-procedure lineage | ● | ● | ● | ● | ◐ | ○ | ENTRY | P0 |
| ETL / orchestrator lineage | ● | ● | ● | ● Lakeflow | ◐ Airflow | ○ | ENTRY | P0 |
| OpenLineage ingestion | ● | ● | ● | ● | ● preview | ○ | ENTRY | P0 |
| BI-tool lineage (Tableau/PBI/Looker) | ● | ● | ● | ● external GA | ● | ○ | ENTRY | P1 |
| dbt lineage | ● | ● | ● | ● | ● | ● manifest-based | — | — |
| Impact analysis | ● | ● | ● | ◐ | ◐ | ● | — | — |
| **AI decision lineage** (why the agent chose this) | ○ | ○ | ○ | ◐ agent tracing | ○ | ● | **DIFF** | P0 |
| Lineage + policy in one graph | ○ | ◐ | ○ | ◐ | ◐ | ● | **DIFF** | P1 |

## 4. Semantics, glossary, stewardship

| Capability | Atlan | Collibra | Alation | Unity Catalog | Horizon | **Atlas** | Gate | Pri |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|---|
| Business glossary lifecycle | ● | ● best-in-class | ● | ◐ preview | ◐ | ○ | ENTRY | P0 |
| Term ↔ asset linkage | ● auto | ● | ● | ◐ | ◐ | ◐ annotations | ENTRY | P0 |
| Ownership / stewardship assignment | ● | ● | ● | ◐ domains | ◐ | ○ | ENTRY | P0 |
| Conflict resolution across sources | ● | ● workflow engine | ◐ | ○ | ○ | ○ | ENTRY | P1 |
| Metric / measure definitions | ● generator | ● | ◐ | ● LOD, parameterized | ● advanced | ● versioned | — | — |
| Semantic authoring IDE | ● Context Eng. Studio | ◐ | ○ | ◐ | ● Semantic Studio + Git | ○ | PARITY | P1 |
| Automatic semantics from existing assets | ● ontology generator | ◐ | ● ALLIE | ● Genie drafts | ● Autopilot from SQL/BI | ● metadata-only inference | — | — |
| **Maker-checker on every semantic object** | ◐ certification | ● workflow | ◐ | ○ | ○ | ● enforced | **DIFF** | — |
| Negative knowledge (rejected inferences retained) | ○ | ○ | ○ | ○ | ○ | ● | **DIFF** | — |
| Semantic versioning + rollback | ◐ | ● | ◐ | ◐ | ◐ | ● immutable versions | DIFF | — |
| Open semantic interchange (OSI) | ◐ | ◐ | ○ | ◐ | ● originator | ○ | PARITY | P2 |

## 5. Data quality and observability

| Capability | Collibra | Alation | Monte Carlo | Anomalo | Soda | **Atlas** | Gate | Pri |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|---|
| Rule-based checks | ● | ● | ● | ● | ● | ◐ thresholds only | ENTRY | P0 |
| ML anomaly detection | ◐ | ◐ | ● | ● unsupervised | ◐ | ○ | SKIP → integrate | P2 |
| Freshness monitoring | ● | ● | ● | ● | ● | ○ fails closed as NOT_CONFIGURED | ENTRY | P0 |
| Volume / schema drift | ● | ● | ● | ● | ● | ● | — | — |
| Incident lifecycle | ● | ● | ● | ● | ◐ | ● audited | — | — |
| Notification / escalation routing | ● | ● | ● | ● | ● | ○ | ENTRY | P0 |
| SLA / SLO on data | ● | ◐ | ● | ◐ | ● | ○ | PARITY | P1 |
| **Quality signal gates runtime execution** | ○ | ○ | ○ | ○ | ○ | ○ → planned | **DIFF** | P1 |
| **Quality signal demotes retrieval ranking** | ○ | ○ | ○ | ○ | ○ | ○ → planned | **DIFF** | P1 |
| Value-free evidence (no row retention) | ○ | ○ | ○ | ○ | ◐ | ● | DIFF | — |

## 6. AI, agents, and execution — the decisive section

| Capability | Atlan | Collibra | Alation | Unity Catalog | Cortex/Genie | Spotter | **Atlas** | Gate | Pri |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|---|---|
| NL → analytical answer | ◐ via 3rd party | ◐ | ◐ | ● | ● | ● | ● | ENTRY | — |
| Grounded in governed semantics | ● context | ● compiler | ● | ● | ● | ● | ● | — | — |
| AI documentation generation | ● | ● | ● agent | ● | ● | ◐ | ● | — | — |
| MCP server for external agents | ● | ● | ◐ SDK | ● AI Gateway | ● | ◐ | ○ | ENTRY | P0 |
| Agent/model registry | ◐ | ● AI Cmd Ctr | ◐ | ● AI Gateway | ◐ | ○ | ● route versions | — | — |
| Model spend budgets | ○ | ◐ | ○ | ● hard caps | ◐ | ○ | ● budget contract | — | — |
| Agent-vs-human access distinction | ◐ | ◐ | ◐ | ● context attrs | ◐ | ○ | ◐ workload identity planned | ENTRY | P0 |
| **Deterministic SQL validation before execution** | — | — | — | ○ | ○ | ○ | ● SQLGlot AST + allowlist | **DIFF** | — |
| **Single mandatory execution gateway** | — | — | — | ○ | ○ | ○ | ● | **DIFF** | — |
| **Pre-retrieval prompt-risk screening** | ○ | ◐ | ○ | ○ | ○ | ○ | ● versioned classifier | **DIFF** | — |
| Indirect / retrieved-context injection defence | ○ | ○ | ○ | ◐ | ○ | ○ | ○ → planned | **DIFF** | P0 |
| **Approved-tool-first execution** | ○ | ○ | ◐ products | ○ | ○ | ○ | ● | **DIFF** | — |
| **Analysis → governed tool promotion** | ○ | ◐ data products | ◐ builder agent | ○ | ○ | ○ | ● with maker-checker | **DIFF** | — |
| Multi-step tool plans | ◐ agent skills | ◐ | ◐ SDK | ● | ◐ | ◐ | ○ | PARITY | P1 |
| Agent execution tracing | ◐ | ◐ | ◐ | ● unified tracing | ◐ | ○ | ● value-free traces | — | — |
| Model kill switch (drilled) | ○ | ◐ | ○ | ◐ | ○ | ○ | ◐ designed, undrilled | DIFF | P0 |
| Model-risk evaluation corpus | ○ | ◐ | ○ | ◐ | ○ | ○ | ◐ control evals only | DIFF | P0 |

## 7. Enterprise trust and operations

| Capability | Atlan | Collibra | Alation | Unity Catalog | **Atlas** | Gate | Pri |
|---|:--:|:--:|:--:|:--:|:--:|---|---|
| OIDC / enterprise SSO | ● | ● | ● | ● | ● verified JWKS | — | — |
| ABAC / attribute policy | ◐ | ● | ◐ | ● grant policies, identity+context attrs | ○ RBAC only | ENTRY | P0 |
| Row/column-level policy enforcement | ◐ | ● | ◐ | ● | ◐ conservative masking | ENTRY | P0 |
| Delegated source identity | ○ | ◐ | ○ | ● | ○ | DIFF | P0 |
| Secret manager integration | ● | ● | ● | ● | ◐ adapter contract, unregistered | ENTRY | P0 |
| Tamper-evident / WORM audit | ◐ | ● | ◐ | ◐ | ◐ ledger, no WORM | ENTRY | P0 |
| SIEM routing | ● | ● | ● | ● | ○ | ENTRY | P0 |
| Compliance certifications (SOC2/ISO/FedRAMP) | ● | ● FedRAMP-ready | ● | ● | ○ | ENTRY | P1 |
| BCBS 239 / regulatory control packs | ◐ | ● | ◐ | ○ | ○ | DIFF for banking | P1 |
| Multi-region / DR with failover | ● | ● | ● | ● managed DR | ○ | ENTRY | P1 |
| Published performance benchmarks | ◐ | ◐ | ◐ | ● | ○ | DIFF | P1 |
| **Fail-closed by construction** | ○ | ◐ | ○ | ◐ | ● | **DIFF** | — |

## 8. Scorecard summary

| Dimension | Atlas vs. market | Direction |
|---|---|---|
| Connector breadth | **Far behind** (2 vs 80–100) | Close to ~15 certified — never chase count |
| Catalog & search | **Behind** | Close: vector + graph + policy-aware ranking |
| Lineage | **Behind on coverage, ahead on AI-decision lineage** | Close ETL/view/BI; extend the AI lineage lead |
| Glossary & stewardship | **Far behind** | Close — this is the biggest single functional gap |
| Data quality | **Behind on breadth, uniquely positioned on coupling** | Close basics; then build the runtime coupling nobody has |
| Semantics | **At parity, ahead on governance** | Hold; add authoring IDE (Studio) |
| Governed AI execution | **Clearly ahead** | Extend aggressively — the window is 12–24 months |
| Enterprise trust posture | **Architecturally ahead, operationally behind** | Convert design into certified, drilled evidence |
| Ecosystem / MCP | **Behind** | Close fast — MCP is now the distribution channel |
| Proof at scale | **Behind** | Benchmarks are a product feature, not a QA task |

## 9. The seven capabilities nobody else has

These are the entries marked **DIFF** where Atlas scores `●` and every competitor scores `○` or `—`. They are the product.

1. A **single mandatory execution gateway** every source query must pass — including tools, profilers, and admin queries.
2. **Deterministic AST validation** of all SQL before execution, with catalog allowlists derived from parsed references.
3. **Pre-retrieval prompt-risk screening** — malicious input is blocked before it can influence retrieval, model context, or tool selection.
4. **Approved-tool-first execution** — reuse a governed capability before generating anything.
5. **Analysis → governed tool promotion** with maker-checker and deterministic SQL rendering.
6. **AI decision lineage** — not just where data came from, but why the agent chose this path and what it refused.
7. **Negative knowledge** — rejected inferences are retained so the system does not re-propose them.

Every one of these follows from the same architectural commitment: *deterministic services hold authority; models propose.* Competitors cannot copy them individually — they would have to adopt the commitment, which contradicts their "let the agent do it" positioning.

## Related documents

- Market landscape: `00-product/03-market-landscape.md`
- Per-vendor deep dives with UI-surface detail: `review-2026-08/research/02-atlan.md`, `review-2026-08/research/01-collibra.md`, `review-2026-08/research/03-alation-purview-unity-ainative.md`, `review-2026-08/research/03-alation-purview-unity-ainative.md`
- Differentiation and whitespace: `00-product/05-differentiation-and-whitespace.md`
- Roadmap: `60-delivery/01-roadmap.md`
- Status matrix: `60-delivery/00-status.md`
