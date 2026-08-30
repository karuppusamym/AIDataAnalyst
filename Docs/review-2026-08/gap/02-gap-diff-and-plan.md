# Gap Diff and Plan — target design vs. current Atlas

Status: **Decision document. Nothing has been changed. Every row below awaits a
keep/correct/rewrite/drop call.**

Sizing is in engineer-weeks for one competent engineer, and is deliberately rough.
Risk is the risk of *doing it*, not of leaving it.

---

## 1. Headline recommendation

**Restructure and extend. Do not rebuild.**

The engine — validating query gateway, five real connectors, AST-safe tool binding,
deterministic prompt-risk screening, a real JSON-RPC MCP server, maker-checker
enforced in code and tested — is genuine, working, and expensive to reproduce. The
chassis — the 21-module decomposition the documents are written around — has 1 of 21
modules built and that one is a 69-line stub.

Rebuilding would discard the hardest, most defensible asset in order to fix the
easiest problem. See `target/00-design-brief.md` §7 for the full argument.

---

## 2. What the current design gets right — keep unchanged

These are not concessions; they are the parts of the architecture I would not
improve on.

| # | Keep | Why |
|---|---|---|
| K1 | **INV-2, one execution choke point** | An absence-property differentiator, genuinely implemented, verified: `execute_read_query` has exactly one call site |
| K2 | **INV-3, model output is never authority** | The typed-inert-proposal boundary is the correct architectural — not statistical — safety claim |
| K3 | **Confidence caps that make auto-publish structurally impossible for model-only inference** (0.70 cap vs 0.95 gate) | The cleanest single control in the design |
| K4 | **AST literal binding in tools** | Verified real. "Injection impossible by construction" holds |
| K5 | **PostgreSQL authoritative, everything else a rebuildable projection** | Correct, and prevents the second-source-of-truth failure |
| K6 | **One approval service, one unified review queue, maker ≠ checker** | Verified in code with a test. Resisting per-feature approval flows is the right instinct |
| K7 | **Value-free control plane (INV-6)** | Verified: `"value_scope": "METADATA_ONLY"` throughout the model payload builder |
| K8 | **Prompt-risk screening before retrieval** | Ordering verified in the orchestrator. Right sequencing |
| K9 | **`interpretation` before the number** in the answer contract | Small, unusual, and correct |
| K10 | **INV-9, honest capability reporting** | The right cultural invariant; extend it to lineage parsers |
| K11 | **Envelope idempotency + FULL-reconciles-only-after-all-chunks** | The hardest correctness property in ingestion, and it is right |
| K12 | **Refusals that do not leak which control fired** | Correct security posture |

---

## 3. Corrections — same intent, different design

| # | Item | Current | Proposed | Weeks | Risk |
|---|---|---|---|---|---|
| C1 | **Tenancy model** | LOB and domain are tenancy levels; ADR-0017 proposes deepening this | `org → workspace` for access; LOB/domain become a versioned classification tree; ABAC keys on it | 6–8 | **High** — touches the repository base class and every scoped query. Do it before more modules exist, not after |
| C2 | **`legal_entity`** | In ADR-0005 and module 01's domain model; **does not exist in code** | Do not build. It is either an isolation boundary or a classification attribute | 0 | None |
| C3 | **Agent runtime last 5 states** | Applied in one `for` loop after the gateway returned | Five independently-gated checkpoints that can refuse | 2 | Low |
| C4 | **Lineage ↔ gateway cycle (`ST-11`)** | Mutual calls; L2→L3 edges | Gateway emits, intelligence consumes. One direction | 1 | Low |
| C5 | **Data quality as a module** | Module 11 with no independent consumer | Baselines fold into profiling; gates become ABAC conditions. Delivers the D4/W1 "quality gates runtime" whitespace as a policy, not a subsystem | 2 | Low |
| C6 | **Module count** | 21 documented, 1 built | 16 with real boundaries (see `target/05` §3) | — | — |
| C7 | **Graph store** | Neo4j projection | Postgres edge tables + recursive CTE; keep the projection interface | 3 | Medium — removes a store, and with it 2 overdue drills |
| C8 | **Kafka** | In the topology | Defer; keep the outbox (the hard part) and the envelope | 1 | Low — removes a broker from a bank deployment |
| C9 | **Confidence model for lineage** | Single number | Store the derivation *method*; policy decides what each method may do | 1 | Low |
| C10 | **Documentation truth** | Architecture docs describe a structure that does not exist | Either build the structure or restate the docs in the present tense honestly. Both is better | 2 | None, but it is a prerequisite for onboarding anyone |

---

## 4. New — no foundation exists

| # | Item | Weeks | Risk | Why it matters |
|---|---|---|---|---|
| N1 | **Envelope v1.1: views + DDL, procedures + body, functions, comments, grants** | 3 | Low | Unblocks N2, N3, N6, N7. Nothing else can start without it |
| N2 | **View DDL parsing → column-level lineage** | 3 | Low | Largest single lineage coverage win; `sqlglot` already in the stack |
| N3 | **Procedure body parsing (T-SQL, PL/SQL first)** | 8–10 | **High** | Uncontested in the market; genuinely hard; degrade explicitly rather than silently |
| N4 | **Lineage proposal / review / negative-knowledge workflow** | 5 | Medium | The stated requirement. Diff-based, impact-ordered, bulk decisions |
| N5 | **pgvector + hybrid retrieval (lexical ∪ vector ∪ graph, policy before ranking)** | 4 | Medium | Everything downstream — wiki search, document mapping, agent context — depends on it |
| N6 | **Workspace primitive + source bindings with expiry** | 4 | Medium | Ships with C1 |
| N7 | **ABAC policy engine** | 6 | Medium | `principal_kind = AGENT` alone justifies it |
| N8 | **Document ingestion: upload → parse → section → map → claims** | 6 | Medium | Data-dictionary spreadsheets are the highest-value special case; build that path first |
| N9 | **Business graph (nodes, assignments, rules, effective dates, roll-up)** | 4 | Low | LOB/sub-LOB/domain requirement; a recursive CTE, not a subsystem |
| N10 | **Knowledge compilation: pages, blocks, provenance, staleness, pinning, diff proposals** | 10–12 | Medium | The differentiator. Largest new build, and the one nobody else has |
| N11 | **Tool generator B — view → tool** | 3 | Low | Views are pre-curated queries; highest quality-per-effort tool source |
| N12 | **Tool generator C — procedure → tool (read-only proven by parse)** | 4 | Medium | Depends on N3 |
| N13 | **Federation planner + DuckDB join layer** | 8 | **High** | The other uncontested capability. Must preserve INV-2 — leaf-per-source through the gateway |
| N14 | **`validate_sql` MCP tool** | 2 | Low | **Highest value per line of code in this plan.** Everything needed already exists in the gateway; it needs splitting validation from execution |
| N15 | **Agent registry + evaluation-gated publication** | 5 | Medium | Makes "production-grade agent" evidenced rather than asserted |
| N16 | **Negative knowledge as a first-class context-product section** | 2 | Low | Nearly free — the data is a by-product of review workflows already running |
| N17 | **Exemplar store (verified question→tool/SQL pairs) + benchmark suites** | 4 | Low | The Genie lesson: accuracy is a curation loop |
| N18 | **Ingestion-time prompt-risk screening for all model-reachable text** | 2 | Low | Closes the indirect-injection gap flagged in four documents and unaddressed everywhere |
| N19 | **UI rebuild on a real framework** | 12+ | Medium | The one place "start from scratch" is right |

---

## 5. Engineering debt that blocks "production-grade"

None of this is architecture. All of it is currently absent, and a bank's third-party
risk process stops at several of these rows.

| # | Item | Weeks | Note |
|---|---|---|---|
| E1 | **CI pipeline** | 1 | Nothing enforces anything today. **Do this first, before any item above** |
| E2 | **Import-linter: gateway exclusivity (`QG-7`)** | 0.5 | Converts the most-marketed invariant from convention to proof. Best value/effort ratio in the entire backlog |
| E3 | **Import-linter: module boundary contracts** | 2 | Ships with the restructure |
| E4 | **Tier-0 invariant suite, all 10** | 4 | 4 of 9 formalised; the rest need harnesses that do not exist |
| E5 | **Projection rebuild drill** | 1 | Never run. INV-1 is untested. C7 makes this much cheaper |
| E6 | **PITR restore drill** | 1 | Never run |
| E7 | **Temporal failover drill** | 1 | Never run |
| E8 | **Credential rotation drill** | 1 | Never run |
| E9 | **Kill-switch drill** | 0.5 | Never run — and the AI-safety argument depends on it |
| E10 | **Load/soak at 1M objects** | 3 | p95 targets are published and unmeasured |
| E11 | **Penetration test** | external | Not run |
| E12 | **Connector + lineage-parser certification corpus** | 3 | INV-9 is unverifiable without it |
| E13 | **Repo hygiene** — `scratch/*.tar.gz` (~5.4MB) in git history, `scratch/` not ignored | 0.5 | Touches shared history; needs a decision |

---

## 6. Drop

| # | Drop | Reason |
|---|---|---|
| D1 | **Neo4j** | Bounded 1–4 hop metadata traversal does not need a graph database. Removes a store, a rebuild SLO, a projection-lag failure mode, and 2 overdue drills |
| D2 | **Kafka (for now)** | All consumers are internal; the outbox already does the hard part |
| D3 | **`legal_entity`** | Does not exist; not needed |
| D4 | **Module 11 as a standalone module** | Folds into profiling + policy |
| D5 | **ADR-0017 as drafted** | Its two goals — cross-source traversal and cross-source relationship inference — are better served by C1 + N9 than by deepening the tenancy path. Supersede rather than accept |
| D6 | **"Whitespace W10: skip unstructured this horizon"** | Directly contradicts the document-upload requirement. Uploaded customer documentation is a different risk class from source data and is the best meaning signal available |

---

## 7. Sequence

Four phases. The ordering constraint that matters: **E1/E2 before anything, C1 before
more modules exist, N1 before any lineage or tool work.**

### Phase 0 — Make the invariants true (3–4 weeks)
`E1` CI · `E2` gateway-exclusivity contract · `E13` repo hygiene · `C10` documentation
truth pass · `C4` cycle fix.
*Exit: every architectural claim is either enforced by a check or restated honestly.*

### Phase 1 — Foundations that everything else needs (10–14 weeks)
`C1`+`N6` workspace and tenancy · `N9` business graph · `N7` ABAC · `N1` envelope v1.1
· `N5` pgvector + hybrid retrieval · `C7` drop Neo4j · `C8` defer Kafka · `E3` boundary
contracts · `E5` rebuild drill.
*Exit: a workspace can be created, granted, bound to a source, and searched
semantically, under attribute-based policy.*

### Phase 2 — Understanding (14–18 weeks)
`N2` view parsing · `N3` procedure parsing · `N4` lineage review workflow · `N8`
document ingestion · `N16` negative knowledge · `N18` ingestion-time screening ·
`C9` derivation methods · `E12` certification corpus.
*Exit: lineage covers views and procedures, is reviewable, and uploaded documentation
contributes meaning with citations.*

### Phase 3 — Capability (16–20 weeks)
`N10` knowledge compilation · `N11`/`N12` view and procedure tool generators · `N14`
`validate_sql` · `N13` federation · `N15` agent registry · `N17` exemplars and
benchmarks · `C3` runtime checkpoints.
*Exit: a wiki compiles itself, tools generate themselves from the estate, and an
agent is published only after passing a benchmark.*

### Continuous
`E4` invariant suite · `E6`–`E9` drills · `E10` load testing · `E11` pen test ·
`N19` UI rebuild (parallel track).

**Rough total: 45–60 engineer-weeks to Phase 3 exit, excluding the UI track.** With
two engineers and honest parallelism, roughly two to three quarters. The estimate
assumes no rebuild, which is the entire argument.

---

## 8. If you take only three things from this review

1. **Write the import-linter gateway-exclusivity contract and stand up CI.** Half a
   week. It converts the platform's central claim from something the documents assert
   into something the build proves.
2. **Decide the tenancy question before writing another module.** Whether LOB and
   domain are tenancy levels or classification labels determines the shape of every
   table, every query and every audit record. It is cheap now and expensive later, and
   ADR-0017 is currently proposing to make it more expensive.
3. **Ship `validate_sql` next.** Two weeks, almost entirely reuse, and it turns the
   existing gateway into a compiler that coding agents can iterate against — which is
   the single most concrete answer to "how do code agents get context to generate
   correct SQL."
