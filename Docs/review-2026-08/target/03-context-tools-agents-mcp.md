# Target Design 3 — Context products, tools, agents, MCP

Status: Proposal, clean-room.

This is the layer where the metadata work becomes revenue. Everything before it
produces understanding; this produces *capability an agent can safely use*.

---

## 1. The stance

Two models of agent access exist in the market, and the choice between them is the
product.

**The socket model**: give the agent SQL. Atlan's MCP server exposes `query_asset`;
Secoda's exposes `run_sql`. The agent writes SQL, the platform executes it, access
control is whatever the API token's persona grants. This is fast to ship, genuinely
useful, and completely inappropriate for a bank — every query is a novel artefact
whose correctness nobody has reviewed, and the audit answer to "why did it return
that number" is "the model wrote this SQL."

**The capability model**: give the agent *approved tools*, and give it enough context
to choose among them well. Databricks is closest — Unity Catalog functions become
governed agent tools that inherit the caller's permissions, and managed MCP servers
expose Genie, vector search and UC functions.

**Take the capability model, and close its two gaps.** Databricks' version works only
inside Databricks, and its Genie curation loop is per-space manual work. The design
below is heterogeneous by construction and generates most of its tools rather than
requiring them to be authored.

> An agent may execute only tools that a human approved. It may generate SQL freely
> — but generated SQL runs only after deterministic validation, and only for
> interactive analysis, never as a published capability.

---

## 2. Tools: four generators, one registry

The current registry supports one path — promote an executed analysis to a tool.
That is a good path and it is implemented. It is not enough: it only produces tools
for questions somebody already asked.

### Generator A — from an analysis (exists)

An executed, validated run is re-rendered deterministically into parameterised SQL.
The SQL is never model-authored, even when generation was involved upstream. Keep
this exactly as it is; it is the strongest thing in the current tool design.

### Generator B — from a view (new)

A view is already a curated, named, reviewed query. It is the highest-quality
tool candidate in any estate and it is being ignored.

```
view + parsed DDL
  → identify parameterisable predicates: comparisons in WHERE against
    literals or against columns with low cardinality / known reference sets
  → propose typed parameters (name, type, required, enum_source where the column
    has a reference table or a small distinct set)
  → propose the returns schema from the view's projection
  → propose name/description from the view's business meaning (already inferred)
  → emit a DRAFT tool
```

The generated tool's SQL is `SELECT <projection> FROM <view> WHERE <bound predicates>`
— deterministic, derived from the view definition, never written by a model. The model
contributes only the name and description, which are language fields.

**Reference-set detection** is what makes the parameters good rather than annoying:
a column with a declared FK to a small dimension gets `enum_source` pointing at that
dimension; a date column gets a date parameter with a bounded range; a high-cardinality
free-text column does not become a parameter at all.

### Generator C — from a stored procedure (new)

A read-only procedure with typed parameters is *already* a tool definition. The
mapping is nearly one-to-one:

```
procedure signature  → tool parameter schema (types come from the declaration — free)
procedure result set → returns schema (from the parsed final SELECT, or from
                        source metadata where the dialect exposes it)
procedure body       → dataflow (from design 2), which gives dependencies,
                        semantic version pin, and impact linkage
```

**Hard constraint: only procedures proven read-only are eligible.** Eligibility is
determined by the body parse, not by a name convention or a flag: any `INSERT`,
`UPDATE`, `DELETE`, `MERGE`, DDL, or dynamic SQL anywhere in the reachable call graph
disqualifies it. Unresolvable dynamic SQL disqualifies it — you cannot prove
read-only through a string you cannot read. This preserves the read-only property of
the whole platform, which is a stated non-goal boundary worth defending.

Execution still goes through the gateway: a procedure call is a statement type the
gateway must learn to validate (fixed callee, bound parameters, no dynamic
composition, result-set row cap enforced by the connector).

### Generator D — federated / multi-source (new)

The requirement: *"data pulled from multiple data sources, joined, and returned via
tools to an agent."*

The naive implementation destroys the execution choke point. This one preserves it:

```
federated tool definition
  = ordered leaf queries, each bound to exactly ONE datasource
  + a join plan over the leaf result sets
  + a projection and a row cap

execution:
  for each leaf:
      → Query Execution Gateway (validate, authorise, cost, mask, execute, bound)
      → bounded result set into an ephemeral in-process DuckDB relation
  join / aggregate / project in DuckDB
  → single bounded result
```

Properties that fall out of this:

- **One choke point survives.** Every source touch is still a gateway call against a
  single datasource. The join layer holds no connection and sees no credential.
- **Per-source policy and masking stay intact**, and the federated result inherits the
  **strictest** masking of any participating leaf. A column masked in source A stays
  masked after joining to source B. This is the correct default and it must not be
  configurable downward.
- **Cost is controlled per leaf**, before the expensive part. A leaf that would exceed
  its budget fails the whole tool before any data moves.
- **Join keys must be declared and validated** against the lineage/relationship graph
  at publish time — a federated tool joining on a pair with no evidence of a real
  relationship should not pass review.
- **Cardinality is bounded at every step.** Leaf row caps are part of the tool
  definition, not a runtime hope.
- **The ephemeral relation is destroyed with the request.** It is not a cache, not a
  materialisation, and never persisted — INV-6 holds because no source values land in
  platform state.

Federation is the single largest new engineering item in this design, and it is also
the one no competitor offers in a governed form. Databricks federates within its own
plane; Collibra and Atlan do not execute at all.

### One registry, one lifecycle

Regardless of generator: `DRAFT → TESTED → SUBMITTED → PUBLISHED → DEPRECATED →
RETIRED`, maker ≠ checker, immutable versions, semantic-version pinning, typed
parameters only, AST literal binding, no free-form SQL fragments or table names as
parameters. The existing design gets all of this right. The change is that the
registry now fills itself from views and procedures instead of waiting for someone to
ask a question first.

---

## 3. Context products

A context product is the unit an agent subscribes to. Not raw metadata — a compiled,
versioned, policy-scoped bundle.

```
context_product
  id, workspace_id, name, version, status
  scope:            projects / domains / sources it covers
  contents:
     glossary_subset          terms + synonyms in scope
     semantic_model_version   pinned
     asset_context[]          per table: purpose, grain, columns w/ meaning,
                              classifications, keys, verified join paths,
                              quality posture, freshness
     knowledge_pages[]        compiled wiki pages (design 1)
     tool_manifest[]          eligible tools, with parameter schemas
     exemplars[]              verified question -> tool/SQL pairs
     negative_knowledge[]     known-wrong joins, deprecated tables, pitfalls
  policy:           who may consume it, under what purpose, with what budget
  consumption_log:  every read, recorded as a lineage edge
```

Three of these deserve emphasis.

**Exemplars are the highest-leverage content in the bundle.** This is the Databricks
Genie insight and it is well-evidenced: their curated space carries verified
question→SQL pairs treated as ground truth, benchmark suites with expected answers,
and an accuracy threshold recommended before production rollout. Alation publishes a
case where a SQL agent went from 60% to 100% accuracy across two iterations from
metadata corrections alone, with no model change. **Agent accuracy is a curation
problem, not a model problem** — so make curation a first-class, measurable workflow
rather than a side effect.

Where exemplars come from: promoted analyses (a question that was asked, answered
correctly, and approved), review-confirmed agent runs, and steward-authored pairs.
They accumulate automatically, which is the point.

**Negative knowledge is uncontested.** No vendor ships "here is what not to do."
Sources: rejected relationship candidates, rejected lineage edges, deprecated assets,
failed agent runs with root cause, and steward-authored warnings ("`cust_master` has
soft deletes — always filter `is_deleted`"). Telling a model what is wrong is at
least as valuable as telling it what is right, and it is nearly free because the
review workflows already generate the data.

**Compilation and staleness** work exactly like wiki pages: an input fingerprint, a
pinned version, and a stale marker when inputs move. An agent holds a version; a new
version is published; the agent's next session picks it up. No agent ever reads a
half-updated context.

---

## 4. Agent registry

Agents need to be governed objects, not implicit callers. This does not exist today.

```
agent
  id, workspace_id, name, kind ∈ {INTERACTIVE, SCHEDULED, EXTERNAL_MCP, CODING_ASSISTANT}
  principal_id                    a real workload identity, not a shared key
  purposes[]                      declared purpose, checked at every call
  context_product_bindings[]      pinned versions
  tool_bindings[]                 explicit; never "all tools in the workspace"
  budgets                         steps, wall-time, tokens, spend, rows, invocations
  model_route_pin                 which approved route, at which version
  evaluation_suite_ref            the benchmark this agent must pass to be published
  status                          DRAFT -> EVALUATED -> PUBLISHED -> SUSPENDED -> RETIRED
  kill_switch                     per-agent, in addition to the global one
```

Two design points.

**Publication requires passing an evaluation suite.** An agent is not published
because someone configured it; it is published because it answered a benchmark set
correctly at a threshold the workspace set. This is Genie's benchmark concept made
mandatory rather than advisory, and it is what makes "production-grade agent" a
statement with evidence behind it.

**Every agent action is attributable to a workload identity**, not to a shared API
key. Both Atlan's and Secoda's MCP servers authenticate with a workspace API token,
which means the audit trail says "the token did it." For a bank that is not an audit
trail.

---

## 5. MCP surface

The existing 1,776-line JSON-RPC server is real and is the right foundation. What it
should expose:

| Tool | Purpose | Notes |
|---|---|---|
| `describe_workspace` | Orientation: domains, sources, coverage, what this agent may do | The first call any agent should make |
| `search_assets(query, filters)` | Hybrid retrieval, policy-filtered before ranking | Returns metadata, never values |
| `get_asset_context(ref)` | The compiled per-asset bundle: purpose, grain, columns, classifications, keys, join paths, quality, pitfalls | This is the wiki table page, in machine form |
| `get_join_path(a, b)` | Verified join paths with evidence and confidence | Prevents the single most common agent SQL error |
| `explain_lineage(ref, direction, depth)` | Bounded traversal with evidence | |
| `get_negative_knowledge(scope)` | Known-wrong things | Uncontested |
| `list_tools()` / `call_tool(id, params)` | Approved capability | The primary execution path |
| `validate_sql(sql, datasource)` | **Run the gateway's full deterministic validation and return structured findings — without executing** | See below |
| `propose_tool(from_analysis_id)` | Agent proposes; human disposes | Write path, draft only |

### `validate_sql` is the one to build first for coding agents

The requirement: *"so code agents get context from database so they can generate SQL,
flow/graph."*

A coding agent writing SQL against an unfamiliar bank schema fails in predictable
ways: wrong table (staging instead of curated), missing soft-delete filter, wrong
join key, unbounded scan, referencing a column that was dropped. Handing it more
documentation helps a little. Handing it a **compiler** helps enormously.

`validate_sql` runs the existing deterministic pipeline — AST parse, read-only check,
reference extraction, catalog resolution, per-object authorisation, structural rules,
cost estimate — and returns findings without touching the source:

```
{ "valid": false,
  "findings": [
    {"code":"UNKNOWN_COLUMN","ref":"rtl.cust_master.email_addr",
     "hint":"column renamed to email_address on 2026-06-14"},
    {"code":"UNAUTHORIZED_OBJECT","ref":"fin.gl_entries",
     "hint":"not in this workspace's source binding"},
    {"code":"MISSING_SOFT_DELETE_FILTER","ref":"rtl.cust_master",
     "hint":"negative knowledge: always filter is_deleted = false"},
    {"code":"UNVERIFIED_JOIN","refs":["a.cust_id","b.customer_ref"],
     "hint":"no relationship evidence; verified path is a.cust_id = c.cust_id = b.cust_id"},
    {"code":"COST_CEILING_EXCEEDED","estimate_bytes":8.2e11}
  ]}
```

The agent iterates against deterministic feedback instead of guessing. Everything
needed to build this already exists inside the gateway; it is a matter of splitting
validation from execution and exposing the validator. **It is the highest
value-per-line-of-code item in this entire design.**

### Exposure and permission

- MCP consumers authenticate as **workload identities bound to a registered agent**.
- Every call is authorised against workspace binding, purpose, tool binding, and
  classification policy — the same path as a human. There is no agent bypass and no
  agent-only privilege.
- `resources/read` must be policy-evaluated per read, not only at handoff. The
  current implementation evaluates `tools/call` fully and has a known gap on
  `resources/read`; close it.
- Budgets are enforced atomically per agent per window, and exceeding one is a
  refusal with a reason code, never a truncated answer.
- **Every consumption is recorded as a lineage edge**, which is what makes "which
  agent read which context before producing which answer" answerable a year later.
  This is genuinely novel and worth protecting.

---

## 6. Answer contract

Whatever the agent returns, the response shape matters more than people expect:

```
interpretation      what the system understood the question to mean, BEFORE any number
sql                 literals redacted
tool_used           id + version, or null if interactive generation
versions            semantic model, policy, context product, prompt-risk classifier
trust               confidence, quality warnings, freshness state
evidence            retrieved assets and why they were chosen; what was rejected and why
execution           row count, cost, duration, warehouse query id (the real backend id)
```

`interpretation` first is the correct call and the current design already makes it —
a user who disagrees with the interpretation stops reading before the number
persuades them of something wrong. Keep it.

Refusals carry `control`, `classifier_version`, `reason_codes` and `remediation`, and
deliberately do not name the specific rule that fired. Also correct; keep it.
