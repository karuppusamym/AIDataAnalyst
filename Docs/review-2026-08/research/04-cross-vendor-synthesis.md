# Cross-Vendor Synthesis — where the market is, and where it isn't

Status: Synthesis of `01-collibra.md`, `02-atlan.md`,
`03-alation-purview-unity-ainative.md`. August 2026.

---

## 1. What happened to this category in 18 months

Every vendor repositioned from "governance" to "AI context," and they did it within
about a year of each other:

| Vendor | 2024 framing | 2026 framing |
|---|---|---|
| Collibra | Data Intelligence Platform | **"The Enterprise AI Control Plane"** |
| Atlan | Active metadata / data catalog | **"The Context Layer for AI"** |
| Alation | Data catalog / ADOP | **AIOS — "Intelligence Operating System"** (July 2026) |
| Microsoft | Purview Data Map + classic catalog | **Unified Catalog** — business-concept-driven |
| Databricks | Unity Catalog governance | UC + Genie + Agent Bricks + **Managed MCP** |

The repositioning is real, not cosmetic — all five shipped agent-facing surfaces —
but it is **shallower than the marketing implies**, and the gap between the pitch and
the shipped product is where the opportunity is. Concretely:

- Collibra's AI Copilot is **in preview**, cloud-only, unavailable on self-hosted or
  government deployments, and its own documentation lists hard limits: it cannot count
  assets, cannot list assets in full, cannot retrieve more than 100 relations per
  asset, and **cannot retrieve column-level information**. Its knowledge base
  refreshes daily.
- Atlan's "Context Lakehouse" (Iceberg + knowledge graph + vector search) does not
  reconcile with its own separately-documented Cassandra/Elasticsearch/Postgres
  Atlas-fork backend, and Atlan is not publicly reconciling them.
- Alation's AI Agent SDK is shipping breaking changes every minor version — the
  Context Tool deprecated in `1.0.0rc2`, `update_catalog_asset_metadata` and
  `check_job_status` removed in `1.0.0`.
- Purview has **no first-party MCP server for the catalogue at all**; its Copilot
  agents are DLP/insider-risk tools on a separate compute-unit billing stack.
- Databricks is the most genuinely shipped — and works only inside Databricks.

**Read:** the category has agreed on the destination and nobody has arrived. A
platform that is honest about its own state and actually delivers governed agent
capability across a heterogeneous estate is not competing against finished products.

---

## 2. Feature matrix

Legend: ● shipped and mature · ◐ shipped, limited or preview · ○ absent

| Capability | Collibra | Atlan | Alation | Purview | Unity Catalog | Secoda | Select Star |
|---|---|---|---|---|---|---|---|
| Catalog breadth / connectors | ● | ● | ● (120+ OCF) | ◐ Azure-centric | ◐ Databricks-only | ◐ | ◐ |
| Business glossary | ● | ● | ● | ● policy-carrying | ○ | ◐ | ◐ |
| Domains / data products | ● | ● | ● (ODPS) | ● | ○ | ○ | ○ |
| **Freeform structured knowledge layer (wiki)** | ○ | ○ (READMEs only) | **● Articles/Hubs/Templates** | ○ | ○ | ○ | ○ |
| **AI-generated documentation as the default path** | ◐ preview | ◐ accept/reject | ◐ Documentation Agent | ○ | ○ | **●** | **●** |
| **AI-generated *documents* (not just field descriptions)** | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Automated lineage — declared/connector | ● | ● | ● | ◐ | ● native runtime | ◐ | ◐ |
| Column-level lineage | ● | ● | ● | ◐ | ● | ◐ | ● |
| **Stored-procedure lineage** | ◐ partial, excluded on Db2/MySQL | ○ | ○ | ○ | ○ | ○ | ○ |
| **Lineage review-and-correct workflow** | ◐ manual/custom lineage | ○ (no manual lineage editing UI) | ○ | ○ | ○ | ○ | ○ |
| **Negative knowledge (what NOT to do)** | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Tool generation from SQL | ○ | ○ | ◐ data products | ○ | ● UC functions | ○ | ○ |
| **Tool generation from views/procedures** | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| **Governed cross-source federated query** | ○ | ○ | ○ | ○ | ◐ within-plane | ○ | ○ |
| First-party MCP server | ● (OSS, ~21 read + ~8 write tools) | ● | ● (churning) | **○** | ● managed, permission-inheriting | ● (incl. `run_sql`) | ● |
| Agent registry / governance | ● AI Command Center | ◐ | ◐ | ◐ security-focused | ● Agent Bricks | ○ | ○ |
| **Agent benchmark/eval as a publication gate** | ○ | ○ | ○ | ○ | ◐ Genie benchmarks (advisory) | ○ | ○ |
| ABAC / policy on future objects | ◐ classification-based | ● Purposes | ○ | ◐ policy-carrying terms | **● governed tags + row/column policies** | ○ | ○ |
| Enforcement at query time | ◐ pushes to platform | ◐ pushes to platform | ○ | ◐ | **● is the engine** | ○ | ○ |
| Self-hosted / in-VPC | ● CPSH (but lineage is cloud-only) | ◐ Secure Agent / SDR | ● | ○ Azure SaaS | ◐ | **● Docker/K8s** | ○ SaaS-only |
| Execution choke point | n/a | ○ | ○ | n/a | ◐ | ○ | ○ |

---

## 3. The four uncontested spaces

Reading down the ○ columns, four capabilities are absent from **every** vendor.

### 3.1 Compiled, provenance-tracked knowledge

Alation has the structure — Articles, Article Groups, Document Hubs, templates with
typed custom fields, per-hub permissions — but generation is not AI-native. Secoda and
Select Star have AI-native generation but only at *field* level: they write a column
description, not a domain page. Nobody compiles a document from catalog + semantics +
lineage + glossary + uploaded documents with per-block provenance and staleness.

Why nobody has it: it requires all the upstream layers to be good *and* versioned. It
is a late-stage capability, which is exactly why it is defensible.

### 3.2 Negative knowledge

Not one vendor ships "here is what not to do." Every review workflow in this category
generates rejection data — rejected relationship candidates, rejected lineage edges,
deprecated assets — and every vendor throws it away. For an agent, "do not join on
`cust_ref`, it is not a key" is worth as much as any positive fact, and the data is a
free by-product of governance that is already happening.

### 3.3 Transformation-source parsing at bank depth

Collibra's supported-source matrix explicitly excludes stored procedures on Db2 and
MySQL, and excludes Power BI DirectQuery entirely. Nobody else attempts procedure
bodies at all. In a bank, a large share of real transformation logic lives in T-SQL and
PL/SQL procedures written over fifteen years. Whoever parses those owns the lineage
conversation — and, per design 3, owns a tool-generation source nobody else has.

### 3.4 Governed cross-source federation

Databricks federates inside its own plane. Collibra, Atlan, Alation, Purview, Secoda
and Select Star do not execute at all — they are metadata systems that hand you a link.
A governed tool that joins two sources under one policy, one cost ceiling and one audit
record does not exist in this market.

---

## 4. The seven things worth copying

| # | From | What | Why it is worth the effort |
|---|---|---|---|
| 1 | Databricks UC | **ABAC**: governed tags with hierarchical inheritance, row-filter and column-mask policies via UDF, `has_tag()` / `MATCH COLUMNS` conditions, one policy covering every current *and future* matching object | The only access model in the category that survives estate growth without administrative linear scaling |
| 2 | Databricks Genie | **The curated knowledge store**: instructions, synonyms with sampled values, join context, certified metrics, **trusted assets** (verified question→SQL pairs treated as ground truth), automated knowledge mining from lineage + query history, in-session knowledge extraction proposed for admin approval, and **benchmark suites with expected SQL** | This is the reference architecture for a SQL agent, and its lesson is that accuracy is a curation loop, not a model choice |
| 3 | Alation | **Behavioural analysis**: query-log-derived popularity driving *both* stewardship prioritisation and DQ rule suggestion from one signal; Analytics Stewardship showing stewards the downstream effect of their curation | Answers "what do we document first," which is the difference between a used catalogue and shelfware. Their published case — a SQL agent going 60%→100% on metadata corrections alone, no model change — is the strongest available evidence for the whole thesis |
| 4 | Alation | **Articles / Article Groups / Document Hubs / typed templates** with per-hub permissions and reference/people-set/object-set field types | The proven object model for a structured knowledge layer; do not reinvent it |
| 5 | Atlan | **Personas vs Purposes**: role bundles separate from tag-driven policy that automatically covers future assets, with explicit deny as a hard ceiling above admin | Cleanest separation of "who you are" from "what the data is" |
| 6 | Select Star | **Visual provenance** — AI-suggested text rendered differently from human-confirmed text | Trivial to build, disproportionate effect on steward trust. Generalise from field to block |
| 7 | Purview | **Glossary terms that carry access policy**, cascading to every data product they are attached to | Makes the glossary load-bearing instead of decorative |

---

## 5. The five things to refuse

1. **Collibra's operating-model burden.** Its flexibility is a curation tax:
   reviewers cite a steep learning curve on hierarchies and lineage configuration;
   third-party estimates put run-rate operating staffing at a multiple of licence
   cost; time-to-ROI is reported around 25 months; implementation partners describe
   4–6 months to MVP and 12–18 months to enterprise rollout, with adoption failing
   without visible wins in the first 30–60 days. Ship an opinionated default model
   and let it be extended.

2. **Conflating taxonomy with permission.** Collibra's Community/Domain hierarchy is
   both an org container and a governance taxonomy, and banks routinely model
   "Community = LOB" for permissions and "Line of Business asset" for taxonomy and
   then cannot reconcile them. The current Atlas design repeats this mistake by making
   LOB and domain tenancy levels. See `target/00-design-brief.md` §2.

3. **Two catalogues.** Microsoft ships both a Fabric OneLake catalogue and Purview
   Unified Catalog and tells customers they need both. Never create an internal
   equivalent seam.

4. **Marketing architecture.** Atlan's Context Lakehouse story and its documented
   backend do not reconcile. Whatever is in the architecture documents should be the
   thing that runs — a discipline the current doc set has partly lost and which
   `gap/01-baseline-reality.md` documents.

5. **Unbounded agent SQL.** Atlan's `query_asset` and Secoda's `run_sql` hand agents a
   live SQL socket. Defensible for them, wrong here: the differentiator is that an
   agent gets approved capability, not a connection.

---

## 6. Pricing and deployment context

Not a design input, but it frames what "good enough to replace" means.

- **Collibra**: quote-only. Third-party estimates: base tiers ~$123k / ~$193k / ~$295k
  annually for 20–30 creator users, with metadata connectors ~$10.4k each, BI/ETL
  integrations ~$26k each, Data Quality ~$156k, AI Governance ~$104k. Real deployments
  cited at ~$176k year-one mid-market to ~$2.04M year-one Fortune 500 including
  implementation services at 35–45%. Median annual spend reported around $197k.
- **Atlan**: quote-only; third-party estimates $20k–$50k small team, $120k–$300k+
  enterprise, roughly $1,000–$2,500 per user per year plus implementation and
  per-connector costs.
- **Purview**: consumption-based — capacity units for the data map, vCore-hours for
  scanning/classification, and per-governed-asset for Unified Catalog. Cost scales
  with estate size and scan frequency, not headcount. Cheaper for small teams, and a
  documented source of cost surprises on large frequently-rescanned estates.
- **Deployment for a regulated bank**: Collibra self-hosted exists but **Collibra Data
  Lineage is explicitly cloud-only** and AI Copilot is unavailable self-hosted. Atlan
  offers a Secure Agent and Self-Deployed Runtime for private sources. Select Star is
  SaaS-only on AWS. Secoda offers genuine self-hosting via Docker/K8s/ECS.

The relevant conclusion: **an internal platform's competition is not the sticker
price, it is the implementation programme.** A commercial deployment costs 12–18
months and a multiple of licence in staffing before it produces value. That is the bar
to beat, and it is beatable by being opinionated where the vendors are configurable.
