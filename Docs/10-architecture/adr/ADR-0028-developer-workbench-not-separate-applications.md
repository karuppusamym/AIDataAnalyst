# ADR-0028 — One Application with a Developer Workbench, Not Several Applications

**Status:** Proposed | **Date:** 2026-09-05 | **Owner:** Architecture + Product

## Context

A September 2026 review of the whole surface — every route in `app.openapi()` against every screen in `ui-next` and the legacy portal — produced two findings that look unrelated and are not.

**Finding one: the estate was a set of islands.** 368 distinct API paths; 132 reachable from `ui-next`, 140 from the legacy portal, 188 from either. More telling than the coverage number was the *joins*: the shell had four cross-screen links in total. A person who found a table in the Catalog and wanted its lineage opened the Lineage screen, re-picked the datasource, and searched for the table again. Every screen kept its selection in the query string already (`useUrlState`); nothing passed a selection between screens.

**Finding two: one audience had no surface at all.** `src/aida/mcp_server.py` is a complete MCP server — `initialize`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`, every call through `QueryExecutionGateway`. Neither portal mentioned it. There was no endpoint page, no auth documentation, no way to see what an agent would be offered, and no way to see what one had read. The AT-7(b) consumer-binding endpoints — the staged-rollout control for pinning a named agent to a version — had no client at all, so a Context Product could be compiled and never actually operated. `portfolio-analytics` carried `mcp_operations` and `unique_mcp_consumers` and was unreachable.

The question raised in review was whether Atlas should therefore **split into several applications** behind a landing page: an ingestion application, a tool/context application, an administration application.

## Decision

**Atlas stays one application. The split is by *audience*, and it is expressed as a workbench inside the shell, not as a separate deployable.**

A new **Developer** workbench holds Context Products and a new **Agent gateway** screen. Everything else keeps its existing persona workbench.

### Why not split by module

1. **It does not split the backend.** ADR-0011 makes Atlas a modular monolith with explicit extraction triggers in `05-service-extraction-plan.md`; none of those triggers is "the UI has a lot of screens". Splitting the front end into applications over one API multiplies shells without moving a single boundary.

2. **The product's value is precisely the joins.** Catalog → lineage → quality → semantics → tool → context is the thesis. Separate applications turn every join into a cross-application link, which makes finding one permanent and unfixable rather than fixing it.

3. **It contradicts a decision already recorded.** `00-product/08` §6.1 concluded that thirty flat navigation items are a feature map, not a product, and that the fix is *fewer* workbenches. Splitting into applications is the same feature map with more chrome.

### Why a Developer workbench *is* a real boundary

The audience differs in a way the persona model already recognises. A **Consumer** browses the marketplace and requests access to a data product. A **Developer** packages context and points a non-human consumer at it. Those are different jobs, done by different people, against different objects, with a different failure mode — and keeping them in one group is exactly why the gateway had nowhere obvious to live.

The Developer workbench answers three questions and nothing else:

| Question | Where |
|---|---|
| What context is packaged, and who is pinned to which version? | Context products (registry, compiler, staged rollout) |
| How does an agent connect, and what will it be offered? | Agent gateway → Connect, What agents see |
| What has an agent already read — and what was refused? | Agent gateway → Consumption (CX-4 edges) |

Administration stays a Console inside the Operator workbench, per `06-product-surface-catalog.md`'s own taxonomy. Ingestion stays the Operator workbench. Neither is an application.

### The landing page

The front door stays a persona chooser plus the agent inbox, with one added entry point for the developer audience. It does not become an application grid: an application grid asks a user which internal module they want, and nobody arrives knowing that.

## Consequences

### Positive

* The MCP server becomes discoverable, with the endpoint, the deployment's *actual* auth scheme (`identity_provider` decides whether it prints a Bearer header or the development headers), a copyable client configuration, and the exposure list computed from what is genuinely `PUBLISHED`.
* Context Products become operable, not merely publishable: consumer bindings, the compiled artifact as a downloadable file, and governed references picked by name instead of pasted as UUIDs.
* Consumption — including refusals — is visible. Refusals are the evidence the boundary held; they were being recorded and never shown.
* The joins are a component (`CrossLinks`) rather than per-screen improvisation, so a link that drops the selection is a test failure rather than a habit.

### Negative

* One more workbench in a document whose §3 explicitly budgets surface count. This is a deliberate spend: the Developer audience previously had zero surfaces, so the budget was not being respected, it was being ignored.
* `Context products` moved out of the Consumer group. Screen ids are unchanged, so every existing deep link still resolves, but a bookmark to the Consumer *group* now lands one group over.
* Two portals still exist. This ADR does not retire the legacy portal; it makes the case stronger, since the Developer workbench has no legacy counterpart to keep in step.

### Reversal condition

Split into separate applications when a genuine deployment boundary appears — a developer portal that must be reachable from a network zone the governed portal must not be, or an audience that must be served without an Atlas seat. That is a placement requirement (T6 in the extraction plan), and it is the only kind of reason that justifies the cost. "It feels like a separate product" is not a trigger, for the same reason it is not one for services.

## References

* ADR-0011 — modular monolith over microservices
* ADR-0014 — value-free control plane (why the gateway can show metadata and never rows)
* `Docs/00-product/06-product-surface-catalog.md` §2.7 — programmatic surfaces
* `Docs/00-product/08-market-deep-dive-and-target-architecture-2026-09.md` §6.1, §6.3
* `src/aida/mcp_server.py`, `src/aida/context_product_api.py`, `src/aida/consumption_lineage_api.py`
