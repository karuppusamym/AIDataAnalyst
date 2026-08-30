# Target Design 1 — Metadata → Business Graph → Wiki

Status: Proposal, clean-room. Depends on the positions in `00-design-brief.md`.

The pipeline this document specifies:

```
source registration → metadata harvest → document ingestion → structural analysis
   → meaning inference → business graph assignment → knowledge compilation → publication
```

Each stage is separately restartable, separately versioned, and produces evidence.
Nothing downstream of "meaning inference" may write to anything upstream of it.

---

## 1. Workspace and project

**Workspace** is the new primitive and the one the current model lacks.

> A **workspace** is a container of work with a membership list, a role assignment
> per member, a set of connected sources, a knowledge base, a tool set, and a budget.
> It is the unit of grant and the unit of blast radius.

A **project** becomes what it should always have been: a *scope of analysis inside a
workspace* — a named subset of the estate with a purpose, used to bound retrieval and
tool visibility. Projects are cheap and disposable. Workspaces are governed and
audited.

```
organization
└── workspace                     (grant boundary, membership, budget)
    ├── source_binding[]          (which datasources this workspace may reach, and how)
    ├── project[]                 (analysis scopes; bound subsets of the estate)
    ├── knowledge_base            (exactly one per workspace)
    ├── tool_set                  (tools published from/for this workspace)
    └── agent_binding[]           (agents allowed to operate here)
```

Why a source is *bound* to a workspace rather than owned by one: the same warehouse
serves many workspaces. A `source_binding` carries the workspace-specific
restriction — which catalogs/schemas are in scope, which classifications are
permitted, which masking profile applies, what the cost ceiling is. Two workspaces
on the same source can legitimately see different things, and the binding is where
that is expressed and audited.

---

## 2. Metadata harvest — what "all metadata" must actually mean

The current envelope (v1.0) carries `catalogs → schemas → tables → columns →
constraints`. That is a catalogue, not an understanding. For view/procedure parsing,
tool generation and lineage inference, the harvest must go further.

**Required object coverage, in priority order:**

| Tier | Objects | Why it is needed |
|---|---|---|
| 1 (have) | catalog, schema, table, column, PK, FK, unique, check, index, partition | Baseline inventory |
| 2 (**missing, load-bearing**) | **view + its DDL text**, **materialized view**, **stored procedure + body**, **function + body**, **trigger** | Without the *text*, there is no view→lineage, no procedure→lineage, no view→tool, no procedure→tool. This is the single highest-value envelope extension |
| 3 | sequence, synonym/alias, foreign table/external table, row-level-security policy, grant/ACL snapshot | Grants matter: the platform must never offer access the source would deny |
| 4 | table/column comments, extended properties, collation, default expressions, computed-column expressions | Free existing documentation — the highest-quality meaning signal available, and it is being left on the floor |
| 5 | statistics: row count, distinct-count estimates, null fraction, min/max **for non-sensitive columns only** | Feeds join inference and grain detection without reading values |

**Envelope v1.1** should add tier 2 and tier 4 together. Tier 4 is nearly free and
materially improves inference quality: an existing `COMMENT ON COLUMN` is worth more
than any LLM guess and should always win.

**Value-freedom under tier 5.** Statistics are aggregate and stay aggregate. Min/max
is the one edge: for a date column it is safe and useful; for a surname column it is
a source value. Rule: **min/max is captured only for columns whose inferred
classification is non-identifying and whose type is numeric or temporal.** Everything
else records presence and cardinality only. This keeps INV-6 intact.

---

## 3. Document ingestion — the missing input

Requirement: *"option on files upload, refine and map to the project or schema."*

Uploaded documents are a **different risk class** from source data and should be
modelled as such. A data-dictionary spreadsheet, a mapping document, a BRD or a
regulatory definition is *the customer's own documentation about their data*. It is
not regulated business data. Treating it as untouchable is why "unstructured
governance — skip this horizon" was the wrong call: it removes the best meaning
signal available.

### Object model

```
document            id, workspace_id, filename, media_type, sha256, uploaded_by,
                    classification, retention_class, status
document_version    immutable; parse output pinned to a parser version
document_section    ordered, addressable (page/heading/anchor), text, embedding
document_mapping    document|section  ->  target (workspace | project | source |
                    schema | table | column | domain | glossary_term)
                    with: mapping_kind (MANUAL | SUGGESTED | CONFIRMED), confidence,
                    proposed_by, confirmed_by
claim               an extracted assertion: (subject_ref, predicate, object_value,
                    section_id, confidence, status)
```

### Flow

1. **Upload** → virus scan, media-type allowlist (pdf, docx, xlsx, csv, md, txt,
   html), size cap, checksum. Stored in object storage; never in a table.
2. **Parse** → structure-preserving extraction to ordered sections. Spreadsheets get
   special handling: a data dictionary is usually a *table*, and a table of
   `schema | table | column | description` should be recognised and offered as a
   direct mapping, not chunked as prose. This one case covers a large share of real
   bank documents.
3. **Map** → three routes, in decreasing preference:
   - **Explicit**: the user maps the document to a schema/project at upload.
   - **Structural**: a parsed dictionary table matches catalog objects by name —
     deterministic, high confidence, no model involved.
   - **Semantic**: embedding similarity between section text and catalog object
     names/paths proposes mappings for confirmation. Never auto-applied.
4. **Extract claims** → for mapped sections, propose typed claims:
   `column X means <description>`, `table Y grain is <grain>`, `term Z is defined as
   <definition>`, `column A is PII`. Claims are proposals. They enter the same
   review queue as everything else.
5. **Cite** → any wiki block or annotation derived from a document carries the
   `document_section` reference. A steward reading a generated description can click
   through to the paragraph in the BRD it came from. This is the single feature that
   converts scepticism into trust, and it costs almost nothing once sections are
   addressable.

**Precedence rule for meaning, highest first:** human-authored annotation → confirmed
document claim → source `COMMENT`/extended property → deterministic rule → model
inference. Never invert this. A model must not overwrite a `COMMENT ON COLUMN`.

---

## 4. Structural analysis — deterministic, before any model

Everything in this section is computed, never inferred by a model. It is the factual
substrate the rest depends on.

| Analysis | Method | Output |
|---|---|---|
| Declared keys | Read constraints | PK, FK, unique — certainty 1.0 |
| **Undeclared join candidates** | Name/type/cardinality/ordinal scoring against a bounded candidate set per table. Never N×N | Candidate + evidence + score |
| Table role | Column-shape heuristics: FK density and measure columns → fact; low cardinality + descriptive columns → dimension; two-FK-only → bridge; `valid_from`/`valid_to`/`is_current` → SCD; append-only pattern → event/history | Role + evidence |
| Grain | Derived from PK/unique constraints, else from a distinct-count-based candidate key search over metadata statistics | Grain statement + confidence |
| Table family | Name-pattern + schema-shape clustering: `orders`, `orders_hist`, `orders_stg`, `orders_v2` | Family + canonical member |
| **Dataflow (views)** | Parse view DDL with `sqlglot`; resolve projections to source columns; classify DIRECT / DERIVED / AGGREGATED / FILTERED | Column-level edges |
| **Dataflow (procedures)** | Parse procedure body; extract `INSERT ... SELECT`, `UPDATE ... FROM`, `MERGE`, `CREATE TABLE AS`; build per-statement column edges; mark dynamic SQL as an explicit `UNRESOLVED` node rather than silently dropping it | Column-level edges + unresolved markers |
| Usage | Query-log ingestion where the source exposes it | Popularity, co-access pairs, top consumers |

Two notes on this table.

**Table families and canonicalisation are the highest-leverage cost control in the
system.** A bank estate is full of `_stg`, `_hist`, `_bkp`, `_v2` variants. One
meaning inference per family instead of per table is the difference between an
affordable and an unaffordable LLM bill, and it improves quality because the family
gives the model more context than any single table.

**Usage data is the answer to "what do we document first."** Alation's oldest
differentiator is query-log-driven prioritisation, and it is correct: documenting in
alphabetical order produces shelfware. Order the inference queue by
`popularity × downstream_impact × documentation_deficit`.

---

## 5. Meaning inference — where the model is allowed in

Input to the model, and nothing else:

- object names and the path that contains them
- column names, types, nullability, key membership
- declared and inferred relationships within the family
- existing comments/extended properties
- confirmed document claims for these objects
- the domain assignment, if any
- **never**: values, sample rows, statistics that could identify, query results

Output from the model: a **typed proposal object**, schema-validated on receipt.

| Proposal field | Allowed | Notes |
|---|---|---|
| `business_name` | Yes | The "business name" requirement |
| `description` | Yes | Table, column, view |
| `synonyms[]` | Yes | Feeds retrieval |
| `grain_statement` | Yes, but only as prose restating the deterministic grain | The grain *fact* comes from keys |
| `analytical_questions[]` | Yes | Seeds the agent's example set |
| `domain_suggestion` | Yes | Proposal only |
| `pitfalls[]` | Yes | "This table has a soft-delete flag; filter it." Feeds negative knowledge |
| `sql` | **Never** | Structural refusal, existing INV-3 |
| `classification` | **Never** | PII decisions are deterministic + reviewed, not modelled |
| any fact field | **Never** | See design brief §4 |

**Confidence and routing** (keep the existing design, it is right):
`≥0.95` auto-publish for low-risk language fields only; `0.80–0.95` publish flagged;
`<0.80` human required; **model-only inference capped at 0.70**, so it can never
auto-publish. Tools, policies and model routes have no auto-publish path at any
confidence.

---

## 6. The business graph

The organisational axis, kept separate from tenancy per the design brief.

```
business_node        id, kind ∈ {LOB, SUB_LOB, DOMAIN, SUB_DOMAIN, CONCEPT},
                     parent_id (nullable, self-referencing), name, description,
                     owner_principal, effective_from, effective_to, version
business_assignment  business_node_id, target_ref (table | column | view | metric |
                     glossary_term | data_product | knowledge_page),
                     assignment_kind ∈ {MANUAL, RULE, INFERRED},
                     rule_id, confidence, assigned_by, confirmed_by, effective_from
```

Properties that fall out of this shape, all of which the current tenancy-fused model
cannot express:

- An asset belongs to **many** domains. A `customer` table is legitimately in both
  *Retail Banking* and *Financial Crime*.
- A domain **spans** workspaces and sources. That is the point of a domain.
- Assignments are **versioned with effective dates**, so a reorg is an update to the
  tree plus new assignments — and last quarter's audit record still resolves against
  last quarter's tree.
- Assignments can be **rule-driven**: `schema LIKE 'rtl_%' → Retail Banking`.
  Rules are governed objects; they are re-evaluated on drift and produce proposals,
  not silent reassignments.

**Roll-up and drill-down.** Because the tree is a real tree and assignments carry a
node reference, "show me everything under Retail Banking" is a recursive CTE over
`business_node` joined to `business_assignment`. Coverage, quality, documentation
completeness and lineage density all roll up the same way. This is the "business
graph at that level, go back to LOB, sub-LOB, domain" requirement, and it is a
query, not a subsystem.

**Deriving the initial tree.** Do not ask a bank to design its domain model before
seeing value — that is the Collibra failure mode. Instead: propose an initial tree
from schema names, source names, existing comments and (if uploaded) the org's own
data-governance document, present it as a draft for one steward session, and let it
be edited. First value in an afternoon; refinement forever after.

---

## 7. Knowledge compilation — the wiki

The design-brief position, made concrete: **pages are compiled build targets, not
documents somebody wrote.**

### Object model

```
knowledge_base      one per workspace
knowledge_page      id, kb_id, page_type, subject_ref, slug, title, status,
                    current_version_id
page_version        id, page_id, version_no, compiled_at, compiler_version,
                    input_fingerprint, status ∈ {DRAFT, PROPOSED, PUBLISHED, STALE}
page_block          id, page_version_id, ordinal, block_type, body,
                    generator ∈ {TEMPLATE, RULE, MODEL, HUMAN},
                    generator_ref (template id | rule id | model_route_version),
                    provenance ∈ {DERIVED, INFERRED, HUMAN},
                    input_refs[] (exact records + versions consumed),
                    pinned (bool), pinned_by, pinned_at
page_link           page -> page | asset | term | tool | document_section
```

`input_fingerprint` is the hash of the ordered set of `(record_id, record_version)`
consumed by the page. Recompilation compares fingerprints; if unchanged, nothing
happens. This makes "regenerate the wiki" cheap and idempotent at estate scale.

### Page types

| Page type | Subject | Compiled from |
|---|---|---|
| Workspace overview | workspace | Source inventory, domain map, coverage stats, top assets by usage, open review counts |
| Domain page | business_node | Assigned assets, owners, glossary terms, metrics, key flows, quality posture |
| Source page | datasource | Connection facts, scan history, schema inventory, drift, freshness contract state |
| Schema page | schema | Table inventory grouped by role, entity-relationship summary, families |
| **Table page** | table | Purpose, grain, columns with meaning + classification, keys, join paths, upstream/downstream lineage, quality signals, pitfalls, related tools, example questions |
| View page | view | Everything on a table page plus the transformation explanation and source columns |
| Procedure page | procedure | Inputs/outputs, tables touched, dataflow summary, unresolved dynamic SQL flagged |
| Metric page | metric | Definition, formula, dimensions, owning domain, certified status, tools that expose it |
| Term page | glossary term | Definition, synonyms, linked assets, conflicts, owner |
| Runbook page | question pattern | "How do I answer X" — verified query, tools, caveats. Compiled from promoted analyses |

### Compilation rules

1. **Deterministic blocks recompile automatically** on input change. A column table
   is a projection of catalog state; it should never be stale and never require review.
2. **Inferred blocks recompile into a proposal** when inputs change materially.
   "Materially" is defined per block type — a new column changes the description
   block; a row-count change does not.
3. **A human edit pins the block.** Recompilation then produces a diff proposal
   against the pin. The system never overwrites a human silently. This is the
   trust-critical property.
4. **Every block renders its provenance.** Derived, inferred and human blocks are
   visually distinct. An inferred block shows its confidence and its source records;
   a document-derived block links to the section.
5. **Staleness is visible, not hidden.** A page whose inputs moved is marked stale
   with a list of what changed. Silently-current-looking stale documentation is worse
   than absent documentation.
6. **Publication is governed.** A page version reaching `PUBLISHED` follows the same
   maker≠checker path as any governed object, at a granularity the steward chooses
   (per page, or per domain in bulk with per-item rationale).

### Why this is the differentiator

Alation has the document structure but not AI-native generation. Secoda and Select
Star have AI-native generation but only at field level — they will write you a
column description, not a domain page. Collibra, Purview and Unity Catalog have no
freeform knowledge layer at all. **Structured + compiled + provenance-tracked +
review-gated is unoccupied**, and it is the natural output of a metadata platform
that already knows structure, meaning, flow and quality.

It is also, not coincidentally, exactly the artefact an agent should be reading.
See `03-context-tools-agents-mcp.md`.

---

## 8. Retrieval

The wiki, the business graph and the agent context all fail without this, and it is
currently lexical-only.

**Hybrid, with policy applied before ranking** (the existing design's rule, which is
correct and worth restating: filtering after ranking leaks the existence of assets
through result-count and ordering side channels).

```
candidate generation:   BM25 lexical  ∪  pgvector ANN  ∪  graph expansion (1-2 hops)
        ↓
policy filter:          per-object authorisation, workspace binding, classification
        ↓
fusion ranking:         reciprocal-rank fusion, then weighted by
                        certification × quality × popularity × canonicality × recency
        ↓
bounded result
```

**What gets embedded:** object names and paths, business names, descriptions,
synonyms, glossary terms, wiki block text, document sections. **Never** source values.
The value-free invariant survives intact because everything embedded is either
metadata or the customer's own prose.

Every ranking factor must be inspectable — a steward asking "why is this table
ranked first" gets the multiplied factors, not a black box. That is cheap to build
and it is what makes the ranking tunable in practice.
