# Lineage, read across three vendors' own product pages

**Date.** 2026-08-30. **Sources.** `atlan.com/data-lineage`, `collibra.com/products/data-lineage`,
`alation.com/product/data-lineage`, fetched directly. (The Google image search that prompted
this is disallowed by robots.txt and was not fetched; the vendors' own pages are the better
source anyway — a marketing page is a commitment, an image result is a thumbnail.)

This is a companion to `03-lineage.md`, which reviewed Atlan's lineage screens in isolation.
Reading all three together changes two conclusions and adds four items.

---

## 1. What all three claim, and what none of them will say

| | Atlan | Collibra | Alation |
|---|---|---|---|
| Construction | SQL parsing over millions of warehouse queries · native API crawls · OpenLineage · REST/SDK/CSV/visual builder | "AI-powered lineage extraction", ~40 sources, ETL and BI tools | "automated technical data flow mapping" — **mechanism not stated** |
| Granularity | Column-level, stated as the differentiator | Column-level for *technical* lineage; table-level for *business* lineage | **Not stated** |
| Impact analysis | Blast radius surfaced **inside a GitHub/GitLab pull request** | Visualise propagation; trace root cause at table, column and report level | Trust Flags for root-cause |
| Propagation | Quality flags downstream; PII tags downstream **with bi-directional Snowflake/Databricks sync** | Not claimed on this page | Overlays for quality, trust, business metadata |
| Export | — | **PDF, PNG, CSV of the lineage state diagram** | — |
| Coverage numbers | 80+ systems; one customer at 18M+ assets | ~40 sources | none |
| Latency / depth limits / SLA | **none** | **none** | **none** |

**The last row is the finding.** Three vendors, three lineage pages, and not one publishes a
parse success rate, a dialect coverage matrix, a maximum traversal depth, a graph-size bound,
or a latency number. Atlan's own screens advertise "under 100 ms" traversal elsewhere but not
here. Nobody will tell a buyer *what fraction of their estate the parser actually resolved*.

That is the same whitespace INV-9 already puts us in, and this makes it a market-wide gap
rather than a quirk of one competitor. A per-dialect, per-construct capability matrix —
published, and derived from a certification corpus rather than hand-written — is a claim none
of these three can currently answer. It is also worthless if our own parser degrades silently,
which it does today (`AT-D2`). **Fix the parser first, then publish the matrix.** Making the
claim before `AT-D2` is closed would be exactly the marketed-versus-actual drift we criticise.

---

## 2. Two conclusions from `03-lineage.md` that this strengthens

**Declining bi-directional classification sync is now a positioning asset, not just a
constraint.** Atlan markets writing PII tags back into Snowflake and Databricks. Collibra —
the vendor with the deepest regulated-industry install base — does not claim it on its lineage
page. Our defence stands and gets sharper: *an inferred classification written back into an
independently audited system, with no maker–checker in the path, makes our inference
authoritative inside someone else's control environment.* We propagate into our own
enforcement plane (`AT-11`), we do not mutate the source's.

**Collibra's technical-versus-business lineage split validates keeping `INFLUENCES` edges out
of propagation.** They ship two graphs for two audiences at two granularities. Our equivalent
is one graph with typed derivation methods, which is better — but only if the type is honest,
which is `C9` plus `AT-D2`.

---

## 3. Four items this adds

| ID | Item | Why it earns its place | Est |
|---|---|---|---|
| AT-19 | **The transformation code on the edge.** Collibra renders the relevant table- and column-level code *inside* the lineage diagram. We now harvest view DDL and routine bodies (envelope 1.1) and redact literals, so we can show the exact fragment that produced an edge, with its redaction status | This is the single cheapest thing on this list and it answers the question a reviewer actually asks — not "does an edge exist" but "why do you say so". It also makes a wrong edge correctable instead of merely reportable, which is what `N4`'s review workflow needs to be useful | 1.5 w |
| AT-20 | **Lineage evidence export** — a point-in-time lineage state for a chosen asset and depth, as a signed artifact (PDF or PNG for the diagram, CSV or JSON for the edge set) carrying the pinned graph version, the derivation method per edge, and who asserted the human ones | Collibra ships plain diagram export. For a bank the artifact is the deliverable: it is what goes in a BCBS 239 pack or an audit response. Ours is worth more than theirs precisely because our edges carry method and provenance — but only if we can hand it over | 2 w |
| AT-21 | **Impact analysis as a pull-request gate.** A CI check on the customer's dbt or SQL repository that resolves a proposed schema change against the graph and fails, or comments, when the blast radius touches a certified metric, a published context product, or a regulatory report | Atlan's best idea on the page: the moment a schema change is cheapest to stop is before it merges, not after it breaks a dashboard. Depends on `AT-10` (one canonical graph) — impact analysis that cannot see view, procedure or BI edges gives a false all-clear, which is worse than no gate | 3 w, after AT-10 |
| AT-22 | **Published parser capability matrix**, derived from the certification corpus (`E12`), per dialect and per construct, with the unsupported list stated rather than implied | §1. Uncontested across all three vendors. Blocked on `AT-D2` — publishing a coverage matrix for a parser that fails silently would be the drift we criticise | 1 w after E12/AT-D2 |

Not taken: Alation's overlay toggles (quality, trust, business metadata over the graph) are a
UI pattern rather than a capability, and belong to the experience-shell track if anywhere.

---

## 4. One line for the positioning file

Every vendor in this market says lineage is automatic. None says how much of your estate it
actually resolved, or what it did when it could not. We publish the matrix, we mark the edges
it could not parse as unresolved rather than absent, and we show you the code we parsed.
