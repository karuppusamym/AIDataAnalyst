# ADR-0017 — Domain-Complete Tenancy and Boundary-Aware Cross-Source Graph Traversal

**Status:** Superseded by [ADR-0018](ADR-0018-three-axis-tenancy-and-classification.md) | **Date:** 2026-08-29 | **Superseded:** 2026-08-30 | **Owner:** Architecture

> **Superseded before acceptance.** This ADR's own reversal condition — *"domain taxonomy turns
> out not to nest cleanly (a table genuinely needs two sibling domains)"* — is structurally met
> in a bank estate: a `customer` table belongs to both Retail Banking and Financial Crime. ADR-0018
> keeps this ADR's two real goals (cross-source traversal, cross-source relationship inference, and
> the `cross_boundary_grant` mechanism, which it retains) but reaches them by separating
> classification from tenancy rather than by deepening the tenancy path. The context and problem
> statement below remain accurate and worth reading; the proposed solution does not.

## Context

Today the knowledge graph and unified lineage graph are both hard-scoped to **one datasource**
(`GET /v1/datasources/{id}/knowledge-graph`, `GET /v1/datasources/{id}/unified-lineage/graph`).
Relationship inference (module 06) only ever compares tables within a single source. This was a
deliberate, documented boundary, not an oversight:

- ADR-0010 (bounded, lazy, value-free graph) explicitly defers the problem: *"Cross-source
  traversal needs explicit design rather than falling out of a global graph."*
- Module 10 (knowledge-graph) lists **KG-2 Cross-source traversal** as open, P1, "Not implemented."
- Module 06 (relationships) lists **RL-5 Cross-source relationship inference** as open, P1, "Not
  implemented — Required for a heterogeneous estate."
- ADR-0005 already decided the tenancy hierarchy should be six levels —
  `organization → legal_entity → line_of_business → data_domain → project → datasource` — but
  only four are implemented today (`organization`, `line_of_business`, `project`, `datasource`).
  `legal_entity` and `data_domain` exist in the ADR, not in the schema.

A bank's estate is not one flat pool of tables. A customer entity is assembled from tables that
live in different projects, sometimes different lines of business. Today the platform cannot show
that relationship at all — the moment a join crosses a datasource boundary, it is invisible to both
the relationship engine and the graph explorer. This ADR is the "explicit design" ADR-0010 deferred:
it decides how a relationship, and a traversal, are allowed to cross a boundary, and it completes
the tenancy hierarchy so "domain" is a real, governed level rather than a folder name.

This is a request to **redesign for organization scale** — a graph that can show relationships and
lineage across projects, sources, and domains, not just within one source — while keeping the two
non-negotiable properties already decided: INV-5 (a traversal cannot cross a tenant boundary without
an explicit, audited grant) and ADR-0010 (bounded, lazy, value-free — no unbounded global graph).

## Decision

**1. Complete the tenancy hierarchy with `data_domain`.**
Add `data_domain` as a real table between `line_of_business` and `project`
(`line_of_business → data_domain → project → datasource`), with `parent_domain_id` (nullable,
self-referencing) so a domain can have sub-domains to arbitrary depth. `legal_entity` is in scope
for a later ADR — it changes regulatory boundaries, not graph boundaries, and shouldn't ride on this
change. Every `project` and `datasource` row gains a `data_domain_id`; backfill assigns every
existing project to a default "Ungoverned" domain per LOB so nothing breaks on migration.

**2. Every graph node and edge carries its full tenancy path, not just its datasource.**
Neo4j nodes already carry a tenancy boundary (module 10, §5) for INV-5 enforcement. Extend that
boundary to the full path — `organization_id`, `lob_id`, `data_domain_id`, `project_id`,
`datasource_id` — as node and edge properties. This is what makes a bounded cross-boundary query
possible: the traversal can filter by domain or project *before* it walks edges, instead of walking
first and checking after.

**3. Traversal default scope becomes the domain, not the datasource.**
`knowledge-graph` and `unified-lineage` traversal moves from
`/v1/datasources/{id}/...` to `/v1/domains/{id}/graph` (datasource-scoped endpoints stay, as a
narrower view). Within one `data_domain`, a bounded traversal (still 1–4 hops, still capped nodes
and edges, still explicit truncation reasons — ADR-0010 is unchanged) can now cross project and
datasource boundaries **by default**, because everything inside a domain is already inside the same
governed boundary a steward owns. This is the concrete fix for "the graph doesn't show relationships
across sources" for the common case: two sources that belong to the same bounded-context team.

**4. Crossing a domain, LOB, or org boundary requires an explicit, audited grant — never inherited.**
A `cross_boundary_grant` record (grantor, grantee scope, direction, edge kinds, expiry, audit trail)
is the only way a traversal or a relationship-candidate scan reaches outside its own domain. No
grant, no edge — the API returns the node as present but its cross-boundary edges as
`withheld: "no_grant"` rather than silently omitting them, so a user knows *something* was hidden
and can request access, consistent with ADR-0010's "truncation is honest" principle. This is INV-5
applied to graph traversal instead of row-level queries — it doesn't weaken the invariant, it gives
the invariant a concrete unit (the grant) at the one point (graph traversal) it didn't reach before.

**5. Relationship inference (module 06) runs within the same scope tiers.**
Candidate generation already prunes by name/type/cardinality before scoring (module 06, §6).
Extend the first pruning stage to also iterate tables across projects within one domain by default,
and across domains only under an active grant. The metadata-only, value-free evidence model,
maker-checker review, and negative-knowledge suppression are unchanged — this only widens *which
tables get compared*, not how.

**6. "Universal lineage" is a federated bounded view, not a global graph.**
There is no single query that returns "all lineage in the org" — that is exactly the unbounded
traversal ADR-0010 rejected. Instead, a domain-scoped or grant-scoped bounded traversal is how a
user reaches org-scale lineage: pick a node, expand outward, and the boundary you're allowed to
cross determines how far it goes. The Neo4j projector (module 10, §7) already rebuilds an
idempotent, tenancy-tagged projection from the outbox — it needs the added tenancy-path properties
from #2 and a scope/grant parameter on its read path; the ingestion and rebuild machinery does not
change.

**7. UI: a workspace hierarchy browser replaces the flat "pick one source" dropdown.**
`Organization → Line of Business → Domain → Project → Source`, as a left-hand tree with breadcrumbs,
matching the completed hierarchy. Selecting any level opens that level's bounded graph by default
(a domain node, not a source). This is the "drill from workspace to multiple projects, multiple
sources" the graph currently has no UI for, because the data model underneath it stopped at
datasource.

## 8. At estate scale — discovery cannot be "scan everything, all the time"

A single bank estate here means hundreds of databases across dozens of LOBs, tens of thousands of
tables. Nothing above changes at that scale, but the design is incomplete without saying how
lineage gets *established* in the first place without a human declaring every edge:

- **Declared constraints first, always.** FK/PK from source metadata costs nothing and is already
  trusted as fact (module 06 §6, step 1) — the majority of "lineage" in a well-modelled schema is
  free before any inference runs.
- **Pruning before scoring stays the scale lever.** Name/type/cardinality pruning (module 06 §6)
  is what keeps candidate generation sub-quadratic; extending it across projects in a domain (§5
  above) keeps the same property — it is still prune-then-score, just over a wider table set, never
  brute-force N×N across the estate.
- **Negative knowledge is what makes the review queue converge.** At estate scale the review queue
  would grow without bound if rejections were re-proposed; module 06 §8 already suppresses them.
  This property matters *more*, not less, as scope widens from one source to a domain.
- **`scan_policy` (existing table, module 03) should weight by retrieval usage, not treat every
  source equally.** Today `scan_policy` schedules by `next_run_at` alone. Add a priority factor
  informed by RT-6's planned usage/popularity signal (module 12) so heavily-queried sources get
  fresher lineage and rarely-touched archival sources scan on a slower cadence. This is the
  concrete answer to "how do we establish lineage for that many tables": establish it where it is
  used first, and let coverage of the long tail be eventually-consistent rather than uniform.

## 9. Two independent categorization axes — governance boundary vs. business meaning

"How do we categorize so drill-down and drill-up both work" has two different correct answers
depending on what's being asked, and conflating them into one hierarchy is the mistake to avoid:

| Axis | Answers | Structure | Owner |
|---|---|---|---|
| **Tenancy** (`data_domain`, §1 above) | *Where am I allowed to look?* | Strict tree, one parent — `org → LOB → data_domain → project → datasource → schema → table` | Steward per boundary |
| **Business** (`business_domain`, module 07 — already exists) | *What does this represent?* | Many-to-many tags — `business_domain → business_entity → table/column` | Data owner per domain |

A `customer_master` table lives in exactly one tenancy path (say, Retail Banking → project
`core-banking`) but can be tagged into several business domains at once (`Customer 360`, used by
Risk, Marketing, and Retail alike). **Drill-down** narrows the tenancy tree and shrinks what a
traversal is even allowed to see. **Drill-up / drill-across** follows a `business_domain` tag to
every table that means the same thing, regardless of which project or LOB physically holds it —
bounded and filtered by the caller's tenancy grants exactly as §4 describes, so meaning-based
discovery never becomes an access-control bypass. The graph explorer's UI should expose both as
independent pivots on the same focused node, not force a choice between them.

## 10. Workspace and project setup at scale is bootstrap-first, not form-first

The existing `#lob-form` / `#project-form` / `#datasource-form` flow is fine for one-off setup; it
does not scale to hundreds of databases. The setup workflow this design implies:

1. **Bulk import, not one-at-a-time creation.** An admin points at an existing inventory (a CMDB
   export, an AD/LDAP group tree, or a CSV of database → LOB mappings) and the platform creates
   `line_of_business` / `project` / `datasource` rows from it, rather than requiring N form
   submissions for N databases.
2. **Every newly connected source defaults into its LOB's "Ungoverned" `data_domain`** (§1's
   migration default, applied going forward too) — a source is immediately access-scoped and usable
   the moment it's connected. It is never blocked on someone first inventing a domain taxonomy.
3. **Domain assignment is a steward triage queue, not a blocking setup step.** Module 07 §5 already
   runs "one inference call per domain or table family, not per table" to propose business-semantic
   annotations. The same mechanism should propose a starting `data_domain` grouping for tables
   sitting in "Ungoverned" — maker-checker approval, same pattern as every other inference in this
   platform (relationship candidates, business annotations) — so a steward is refining a proposal,
   not authoring a taxonomy from a blank page.

## 11. This is what "context for agent skills" actually consumes

Module 19's `context_product.scope` (already designed: "domains, entities, assets — bounded") is
where this whole hierarchy cashes out for an agent. A context product's scope is expressed on
**both** axes at once, and they compose rather than substitute for each other: a `business_domain`
scope (e.g., "Customer 360") finds every table tagged into that meaning across the estate; the
caller's tenancy grants (§4) then filter that candidate set down to what they're actually allowed
to see. A Risk analyst's agent asking for "Customer 360" context gets the tables tagged into that
business domain **that also fall inside a data_domain Risk holds or has been granted** — never more.
Everything retrieved still goes through module 12's policy-filter-before-ranking and bounded
grounding budget, and — this is the part that makes it a *governed* agent context rather than just
a RAG pipeline — every product read and tool invocation is recorded as an AI-decision lineage edge
(module 09 §6). At estate scale, that is what turns "which context fed which agent answer" from an
unanswerable question into a queryable one.

## Phasing

This is too large to land as one change. A workable sequence:

1. `data_domain` table + migration + default-domain backfill (§1) — schema only, no behavior change.
2. Tenancy-path properties on Neo4j nodes/edges (§2) — projector change, still single-source reads.
3. Domain-scoped traversal endpoint + workspace tree UI (§3, §7) — the first user-visible win.
4. `cross_boundary_grant` model + enforcement (§4) — required before step 3 can cross a domain.
5. Cross-project/cross-domain relationship inference (§5), scan-priority weighting (§8).
6. Context-product scope on both axes (§11) — depends on 1–4 being in place.

Each step is independently shippable and independently testable against `test_cross_tenant_denial`.

## Consequences

### Positive

- Closes KG-2 and RL-5 (both already-open P1 backlog items) with one coherent design instead of two
  point fixes that would have disagreed with each other on tenancy semantics.
- Completes ADR-0005's hierarchy instead of leaving `data_domain` as a documented-but-absent level.
- Cross-boundary access becomes a first-class, auditable object (`cross_boundary_grant`) instead of
  an implicit capability of whoever has API access — this is *more* restrictive than today's
  single-source scoping, not less, because today there is no cross-source view to restrict.
- The bounded/lazy/value-free contract (ADR-0010) and the deny-by-default tenancy invariant (INV-5)
  both hold at every scope level — this ADR is an extension of both, not an exception to either.

### Negative — costs accepted

- A schema migration touching `project` and `datasource` (new FK column) plus a new
  `data_domain` and `cross_boundary_grant` table — in a system whose own tenancy invariant requires
  every query to carry tenancy predicates. This is exactly the "six levels is more than most
  deployments need" cost ADR-0005 already accepted; this ADR spends it.
- Every existing single-datasource graph/relationship query needs a scope-widening review, since the
  pruning and traversal logic now needs a domain- or grant-aware branch, not just a datasource_id.
- The grant model is new product surface: someone has to design the approval workflow, not just the
  data model, before #4 is usable rather than theoretical.
- Backfilling every project into a default domain is a one-time migration with no ambiguity, but a
  bank rolling this out will still need to spend real time re-organizing that default bucket into
  meaningful domains — the platform can't infer domain boundaries the org hasn't decided yet.

### Neutral

- `legal_entity` remains undecided by this ADR. It's the correct next hierarchy gap to close, but it
  changes regulatory/residency boundaries rather than graph boundaries, so it gets its own ADR when
  it's needed rather than being bundled here.

## Alternatives considered

| Option | Why rejected |
|---|---|
| One global graph query across the whole org | Exactly what ADR-0010 already rejected — unbounded traversal is a denial-of-service risk on the graph store and a privacy risk on regulated data; "some competitor demos will look more impressive" was already accepted as a cost there |
| Leave traversal datasource-scoped; add a client-side "switch source" picker that re-queries per source and stitches results in the browser | Pushes cross-source joins into the browser, which cannot see tenancy grants and would either over- or under-show edges; also reintroduces the client-side-full-graph failure mode ADR-0010 rejected |
| Make every traversal org-wide by default, restrict per query with a policy filter | Inverts INV-5's deny-by-default posture — a missed filter becomes a cross-tenant leak instead of a missing feature; the current architecture explicitly rejected "row-level security only" for this reason in ADR-0005 |
| Skip `data_domain` and scope cross-source traversal at the `project` level instead | Doesn't match what the user relationships actually are in a bank estate — two sources inside the *same* project already traverse together today; the gap is specifically for sources in *different* projects that share a domain, which is exactly what ADR-0005 named this level for |

## Revisit trigger

The bank's actual domain taxonomy turns out not to nest cleanly (a table genuinely belongs to two
sibling domains) — at that point `data_domain` needs to become a many-to-many tag rather than a
single parent, which is a bigger change than this ADR should absorb speculatively.

## Enforcement

- INV-5 in `10-architecture/01-principles-and-invariants.md` — extended, not modified: this ADR is
  the "explicit, audited grant path" INV-5 already required for cross-LOB access, made concrete.
- Test: `test_cross_tenant_denial` gains cases for `data_domain` and for traversal with an expired or
  absent `cross_boundary_grant`.
- ADR-0010's bounded/lazy/value-free contract applies unchanged at every new scope level — a domain-
  or grant-scoped traversal still returns `truncated` and a reason, still caps nodes/edges, still
  never renders values.

## Related

- `10-architecture/adr/ADR-0005-tenancy-hierarchy.md` — hierarchy this ADR completes
- `10-architecture/adr/ADR-0010-bounded-value-free-graph.md` — traversal contract this ADR extends
- `20-modules/10-knowledge-graph.md` — KG-2, closed by this decision
- `20-modules/06-relationship-intelligence.md` — RL-5, closed by this decision
- `20-modules/09-lineage.md` — LN-9's per-source unified graph becomes the datasource-level view
  beneath domain-level traversal
