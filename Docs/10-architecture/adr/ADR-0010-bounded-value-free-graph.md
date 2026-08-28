# ADR-0010 — Bounded, Lazy, Value-Free Graph Exploration

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

Graph exploration over an enterprise estate has two failure modes. The first is performance: a browser cannot download a graph with millions of nodes, and an unbounded traversal query can take a database down. The second is privacy: a graph view that renders values would put regulated customer, account, and transaction data into a browser and a screenshot.

## Decision

Graph exploration is **lazy, bounded, and value-free**:

- Traversal is server-side, focused on a selected node, with a configured depth of one to four hops.
- Server policy caps nodes and edges per response and returns **explicit truncation reasons** — never a silently partial result.
- The explorer renders **metadata and approved aggregate evidence only**. It never renders raw customer, account, or transaction values.
- Search is server-side; the client never receives an unfiltered object list.

## Consequences

### Positive

- A single user cannot take the graph service down with one click.
- Response size is bounded, so the browser stays responsive.
- The privacy property holds regardless of who is looking or what they screenshot.
- Truncation is honest — users know they are seeing a bounded view.

### Negative — costs accepted

- Users cannot see the whole graph at once, which is sometimes genuinely what they want.
- Multi-hop investigation requires several interactions rather than one render.
- Cross-source traversal needs explicit design rather than falling out of a global graph.
- Some competitor demos will look more impressive.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Client-side full graph | Browser lockup at enterprise scale; downloads more than the user may see |
| Unbounded server traversal | Denial-of-service on the graph store |
| Sampled full graph | Misleading — users cannot tell what is missing |
| Value rendering with masking | Masking bugs become data-leak incidents; value-free removes the class |

## Revisit trigger

Bank-scale performance and privacy certification permits raising caps. A virtualized rendering adapter may be added **without changing the API safety boundary**.

## Related

- `20-modules/10-knowledge-graph.md`
