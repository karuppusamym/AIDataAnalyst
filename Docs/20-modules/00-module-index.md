# Module Index

> Status: Authoritative. Owner: Architecture.
> One spec per bounded context defined in `10-architecture/04-module-decomposition.md`.

## Spec template

Every module spec follows the same sections, so a reader can find the same fact in the same place in any module:

1. **Purpose** — one paragraph; why this module exists as a separate context.
2. **Jobs served** — persona job IDs from `00-product/02-personas-and-jobs.md`.
3. **Responsibilities / Not responsibilities** — the boundary, stated both ways.
4. **Domain model** — owned entities.
5. **Public interface** — what `<module>/api.py` exposes.
6. **HTTP surface** — routes owned by this module.
7. **Events** — emitted and consumed.
8. **Dependencies** — modules this one may call.
9. **Workers** — background work owned here.
10. **Controls and invariants** — which invariants this module enforces.
11. **Current state → target** — honest gap.
12. **Open work** — tracked items.

## Index

| # | Module | Layer | Purpose in one line |
|---|---|---|---|
| [01](01-identity-and-tenancy.md) | identity-tenancy | L1 | Who is asking, on behalf of which part of the bank |
| [02](02-connectivity.md) | connectivity | L1 | Reaching sources safely, with honest capabilities |
| [03](03-ingestion.md) | ingestion | L1 | Getting metadata in, idempotently, at any scale |
| [04](04-catalog.md) | catalog | L2 | The authoritative inventory of the estate |
| [05](05-profiling-and-classification.md) | profiling | L2 | What the data looks like, without looking at it |
| [06](06-relationship-intelligence.md) | relationships | L2 | How tables connect, with evidence and negative knowledge |
| [07](07-semantic-layer.md) | semantic-layer | L2 | What the data means, versioned and approved |
| [08](08-glossary-and-stewardship.md) | glossary-stewardship | L2 | Who owns meaning, and how disagreement is resolved |
| [09](09-lineage.md) | lineage | L2 | Where data came from — and why the agent chose it |
| [10](10-knowledge-graph.md) | knowledge-graph | L2 | Bounded, value-free traversal of the estate |
| [11](11-data-quality.md) | data-quality | L2 | Whether the data can be trusted right now |
| [12](12-retrieval-and-search.md) | retrieval | L3 | Finding the right context, policy-filtered before ranking |
| [13](13-agent-runtime.md) | agent-runtime | L3 | The governed analytical state machine |
| [14](14-tool-registry.md) | tool-registry | L3 | Turning analysis into reusable governed capability |
| [15](15-model-gateway.md) | model-gateway | L3 | Provider-neutral, budgeted, fail-closed model access |
| [16](16-query-gateway.md) | query-gateway | L3 | The one path to a source |
| [17](17-policy-and-governance.md) | policy-governance | L1 | Policy, entitlement, and maker-checker as primitives |
| [18](18-studio.md) | studio | L5 | Authoring semantics and tools with tests and version control |
| [19](19-context-products-and-mcp.md) | context-products-mcp | L4 | Governed context for external agents |
| [20](20-observability-and-audit.md) | observability-audit | L1 | Evidence, telemetry, and the ledger |
| [21](21-experience-shell.md) | experience-shell | L5 | Persona-derived navigation and the product frame |

## Reading order

- **New engineer:** 01 → 04 → 16 → 13. These four explain the trust model end to end.
- **Product:** 13 → 14 → 19 → 11. These four are the differentiation.
- **Security review:** 01 → 17 → 16 → 15 → 13.
- **Operations:** 02 → 03 → 20 → 10.
