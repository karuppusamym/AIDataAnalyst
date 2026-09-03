# ADR-0020 — Where the Graph Lives: PostgreSQL, and What Would Change That

**Status:** Accepted | **Date:** 2026-08-30 | **Owner:** Architecture
**Supersedes the recommendation in** `Docs/review-2026-08/target/00-design-brief.md` §6,
which said "drop Neo4j" without qualifying it.

## Context

The August 2026 review recommended removing Neo4j on the grounds that traversal is
capped at 1–4 hops (ADR-0010) and a well-indexed PostgreSQL edge table serves that.
The recommendation was right about the cost side and **too glib about the capability
side**, and it was challenged on exactly the correct point:

> a graph database finds nearest nodes and traces lineage more easily than a
> relational one.

That is true in general, and it deserves a better answer than a cost argument.

The place a graph database genuinely wins is **deep, variable-length paths**. Every hop
in SQL is another join; index-free adjacency means a graph engine does not pay that.
Lineage is precisely where depth is real: source → landing → staging → ODS → warehouse →
mart → report is six hops before column-level derivations and intermediate views, and
tracing a regulatory number to its ultimate source under BCBS 239 can be much deeper.
So the honest form of the question is not "is a graph database better at graphs" — it
obviously is — but **"how deep does this product's traversal actually go, and does
PostgreSQL hold at that depth?"**

That is a measurement, and it had not been taken.

## Decision

**PostgreSQL, for both the classification tree and the lineage graph. Neo4j leaves the
topology.** Two separate arguments, because they are separate problems.

### 1. Classification tree — settled by measurement

13,548 nodes at depth 4, 5,000,000 assignments, PostgreSQL 16:

| Operation | Recursive CTE | Closure table | Materialised |
|---|---:|---:|---:|
| Descendants of an LOB | 3.3 ms | 1.5 ms | — |
| Ancestors of a leaf | 3.1 ms | — | — |
| Authorization scope for one table | 0.8 ms | 0.65 ms | — |
| Roll-up over a subtree | 3,147 ms | 915 ms | **0.4 ms** |

Traversal was never the problem; aggregation was, and materialisation solved it
(ADR-0018, migration `a7c3e91d4f28`). A graph database would have helped with none of
this, and would have put a network hop inside a 50 ms authorization budget.

### 2. Lineage graph — measured, and it holds

The case the challenge was really about. A bank-shaped column-level DAG — 12 layers,
40,000 columns per layer, fan-in 2, **880,000 column-level edges** — traversing upstream
from one report column on PostgreSQL 16:

| Depth cap | p50 | Nodes reached |
|---:|---:|---:|
| 2 | 0.7 ms | 7 |
| 4 | 0.7 ms | 31 |
| 6 | 0.7 ms | 127 |
| 8 | 1.6 ms | 511 |
| 10 | 4.4 ms | 1,957 |
| **12** | **10.8 ms** | **3,637** |

Twelve hops of branching column-level lineage, in eleven milliseconds. The join-per-hop
cost that makes deep traversal a graph-database argument does not bite at this depth and
this edge count. The 1–4 hop cap in ADR-0010 was a *product* decision about what to
render, not a performance ceiling — and there is headroom to raise it for lineage without
changing stores.

### 3. Hub-shaped fan-out — measured 2026-08-30, and it changed the requirement

The first version of this ADR listed hub fan-out as unmeasured and as the likeliest place
the decision would hurt. It has now been measured, and the result reframes the problem.

A single source column feeding many downstream columns, traversed **downstream** (impact
analysis, the expensive direction), on top of the same 880,000-edge DAG:

| Hub fan-out | Depth 4 | Depth 8 | Depth 12 | Nodes reached at depth 12 |
|---:|---:|---:|---:|---:|
| 100 | 4 ms | 62 ms | 486 ms | 150,515 |
| 1,000 | 37 ms | 374 ms | 1,127 ms | 280,142 |
| 10,000 | 297 ms | 983 ms | 1,707 ms | 424,228 |
| 50,000 | 854 ms | 2,220 ms | 3,402 ms | 480,000 |

So yes — unbounded impact analysis through a hub is not interactive. But read the last
column: **cost tracks the number of nodes reached, not the depth.** At roughly 100,000
nodes the query costs ~300 ms regardless of how it got there.

That is not a graph-database problem. Neo4j must also materialise 480,000 nodes;
index-free adjacency does not make half a million rows free. It is a *result-size*
problem, and the fix is to stop enumerating:

| Same worst case (50,000 fan-out, depth 12) | p50 |
|---|---:|
| Enumerate everything reachable | 3,402 ms |
| **Bounded to 1,000 nodes** | **1.5 ms** |
| Bounded to 5,000 nodes | 3.2 ms |
| Degree check — "is this column a hub?" | 8.8 ms |

Bounding the traversal — which **ADR-0010 already mandates**: capped nodes, explicit
truncation reasons — turns the worst measured case into 1.5 ms. The mitigation was already
the design; it simply had never been shown to be load-bearing.

**Requirements this makes binding rather than advisory:**

1. Every impact traversal is node-capped as well as depth-capped, and reports truncation
   explicitly. An impact answer that silently stops is worse than one that says it stopped.
2. A cheap degree pre-check (8.8 ms) runs first, so a hub is *known* to be a hub before
   traversal. The honest answer for a hub column is "50,000 direct dependents — here is the
   summary by domain and by certified metric", not a list nobody can read.
3. High-degree nodes get precomputed impact summaries, the same materialisation trick that
   took roll-up from 3,147 ms to 0.4 ms.

### What is still not measured, and where Neo4j would still win

- **All-paths enumeration** between two nodes, as opposed to reachability. Exponential in
  the worst case, and genuinely a graph-database strength.
- **Graph algorithms** — shortest path, centrality, community detection. Not currently a
  product requirement; if relationship-inference ever wants clustering, this changes.
- **Cypher's expressiveness** on heterogeneous edge patterns. A recursive CTE over seven
  edge kinds with per-kind rules is writable but not pleasant.

None of these is on the current roadmap. All of them are reasons this ADR could be
reversed rather than reasons it is wrong today. Hub fan-out has moved off this list --
it was measured, and the answer was a bounding requirement rather than a store change.

## Consequences

### Positive

- Two fewer stateful services in a regulated deployment (Neo4j here, Kafka under
  ADR-0018's stack review), and with them two of the eight overdue operational drills.
- No projection-lag failure mode between the authoritative store and the graph, and no
  class of bug where PostgreSQL and Neo4j disagree.
- Lineage traversal, authorization and roll-up are one transaction boundary, so a
  traversal cannot observe a half-applied lineage publish.
- One backup, one restore drill, one HA story, one set of credentials to rotate.

### Negative — costs accepted

- **Hub-shaped lineage is unmeasured and is the likeliest place this hurts.** It should
  be measured on the real estate once view and procedure parsing land and the graph has
  a realistic degree distribution. That is a scheduled measurement, not a hope.
- Deep path *enumeration* stays expensive. If the product ever offers "show me every
  path from this report to source" rather than "show me what this depends on", this needs
  revisiting.
- Writing traversal in recursive SQL is more work than writing it in Cypher, and the
  cartesian-product bug found during ADR-0018 implementation — a predicate built against
  the un-aliased table inside the recursive term, which returned correct results on a
  small tree and would have returned wrong ones on a real estate — is exactly the class
  of mistake this trade invites. It was caught by a warning, not a test, which is luck
  rather than process.
- The projection interface stays in place precisely so this is reversible. Removing the
  abstraction to "simplify" would be the change that makes the reversal expensive.

## Reversal condition

Reintroduce a graph store — for **lineage only**, never for the classification tree or
the authorization path — when any one of these is measured true on a real estate:

1. p95 lineage traversal at the depth users actually request exceeds ~200 ms, after
   indexing and **after** the node caps and hub pre-check described above — the caps are
   the mitigation, so a measurement taken without them is not evidence for a store change;
2. all-paths enumeration between two assets becomes a product requirement;
3. graph algorithms (clustering, centrality) become a relationship-inference requirement.

Until one of those is a number rather than an intuition, the extra store is cost without
a case.

## Amendment, 2026-08-30 — configurable rather than removed

The owner's decision, taken after reading this ADR: **keep Neo4j, and make it a
per-organization setting** — an administrator chooses PostgreSQL or Neo4j for the graph
read path. The measurement above is unchanged and the default is unchanged; what changes
is that "reversal" stops being a project and becomes a setting.

**This makes the ADR stronger, not weaker.** The reversal condition above named three
measurements that would justify a graph store. Under a removal, meeting one of them meant
reintroducing a store — weeks of work, at exactly the moment the estate was proving the
need. Under a switch, meeting one of them means changing a value for one organization and
measuring the result. A decision that can be tested is worth more than a decision that has
to be defended.

### Shape

The port already has a precedent in this codebase and should copy it exactly:
`aida/vector_store.py` resolves one of four adapters from a setting, defaults to the one
that needs no extension, and **probes rather than trusts** the optional backend (ADR-0019).
The graph port is the same pattern, second application:

| Backend | Meaning |
|---|---|
| `postgres` | Default. Edge tables plus recursive CTEs, as measured above |
| `neo4j` | The existing projection, promoted from a read-through cache to a selectable backend |
| `disabled` | Lineage exploration returns a refusal with a reason code rather than a degraded answer (INV-4) |

The surface is small, which is the main reason this is cheap: three modules read Neo4j
today (`api.py`, lineage_graph_store.py -- since implemented as `graph_store.py`, see
the status note below — `unified_lineage_api.py`), and a boolean
(`lineage_neo4j_read_enabled`, default `false`) already gates the read path. This
amendment turns that boolean into a three-valued per-organization setting and gives it a
conformance suite.

### Three constraints that are not optional

1. **INV-1 bounds where the switch may apply.** PostgreSQL is authoritative and Neo4j is
   a rebuildable projection that is never read for an authorization, approval or
   correctness decision. The setting therefore governs **lineage and graph exploration
   reads only** — never the authorization scope query, never the classification roll-up.
   That is a real constraint and also the good news: a wrong answer from the graph path
   is a worse answer, never a security incident.
2. **Both backends must answer identically, and that is the actual work.** Not "both
   return something" — identical node sets, identical ordering and tie-breaks, identical
   cap behaviour and identical truncation reasons. Two backends that disagree turn a
   config flag into a correctness surface, and the user who hits it has no way to know
   why. One conformance suite, run against both, is the deliverable that makes the
   setting safe. This is a week of work and it is the week that matters.
3. **A backend nothing tests is a backend that breaks silently (INV-9).** No Neo4j runs
   in the suite today — INV-1's test says so itself. Either Neo4j joins CI as a service
   container, or the `neo4j` backend ships advertised as uncertified. It must not be
   offered in an admin console as though it were equally proven.

E5, the projection rebuild drill, stops being deferrable. A projection that has never been
proven rebuildable should not be offered as a selectable backend.

### Consequence for C7 and E5

Tracker `C7` changes from "remove Neo4j" to "make the graph store a configurable port".
`E5` is promoted from a deferred drill to a prerequisite of shipping the `neo4j` backend.
The two overdue drills that removal would have eliminated are back on the list — that is
the price of the option, and it is worth naming rather than absorbing quietly.

### Implementation status, 2026-09-02 — C7 built, E5 still open

`src/aida/graph_store.py` is the port: `PostgresGraphStore` (default, certified),
`Neo4jGraphStore` (uncertified per INV-9 below) and `DisabledGraphStore`, plus
`GraphStoreOrganizationSetting` (migration `8396592b30e0`) for the per-organization
choice. lineage_graph_store.py no longer exists — its Neo4j read logic moved into
`Neo4jGraphStore` and `api.py`'s inline Neo4j query moved into the same adapter's
`graph_summary`, so every Cypher statement on the request-path read surface now lives
in this one module (`tests/test_inv1_single_authoritative_store.py`'s closed reader
list was updated to match). Conformance suite: `tests/test_graph_store_conformance.py`
— building it found the `neo4j` adapter's UPSTREAM/DOWNSTREAM Cypher pattern swapped
relative to `PostgresGraphStore`'s direction convention, fixed in the same pass (see
that module's docstring and `Neo4jGraphStore.lineage_impact`'s inline comment). E5 has
still not run — no Neo4j reachable in this environment or in CI — so the `neo4j`
backend remains uncertified and is only reachable when both an organization's setting
and the process-wide `lineage_neo4j_read_enabled` operator flag select it. See tracker
row `C7` for the full exit-criteria accounting.
