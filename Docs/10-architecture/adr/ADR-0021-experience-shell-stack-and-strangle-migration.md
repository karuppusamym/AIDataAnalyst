# ADR-0021 — Experience Shell Stack and Strangle Migration

**Status:** Accepted | **Date:** 2026-08-30 | **Owner:** Product Engineering

## Context

Module 21 specifies the experience shell: persona routing derived from OIDC claims,
evidence-first panes, permalinks, virtualized lists holding a million rows, bulk
execution over ten thousand items, WCAG AA. Nine items — UX-1 through UX-9 — track
against it. One is done.

The portal that exists cannot carry that spec, and the reason is structural rather
than a matter of effort. `ui/` is 3,655 lines: a 1,561-line `app.js` of imperative
`render*` functions, all twenty screens' markup in a single 330-line `index.html`,
and six feature files. State is DOM state. Rendering is template strings concatenated
and assigned through `setHtml`, with escaping applied by hand at each call site.
There is no component boundary, no package manifest, no build step, no test.

Three consequences follow directly, and each one is a spec requirement it blocks:

- **Virtualization (UX-3) has nowhere to live.** Windowing a list means owning the
  relationship between scroll offset, a data window, and the DOM. A `render()` that
  rebuilds an entire `<tbody>` from a string cannot express that.
- **Evidence panes (UX-7) cannot be composed.** An evidence pane is the same component
  against a different subject on eight screens. Without a component boundary it is
  eight copies that drift.
- **Escaping is a per-call-site discipline.** Every `setHtml` is a place `esc()` can be
  forgotten. The failure is silent and the blast radius is a metadata platform for a
  bank.

Separately, the API is not shaped for these screens. `MetadataTableRead`
(`schemas.py:781`) returns eight fields: ids, name, object type, status, fingerprint.
A catalog row that shows what actually governs whether an agent may use an asset —
description and whether it is model-proposed, owner, certification, quality, glossary
links, row estimate — needs five further endpoints keyed by table id. **Rendering one
hundred rows costs 1 + (100 x 5) = 501 requests.** That is not an argument against
the API, which is correctly resource-shaped and correctly governed; it is an argument
that no read model exists between it and a screen.

## Decision

**Build the shell as a React 18 + TypeScript + Vite application in `ui-next/`, migrate
screen by screen behind it, and add a read-model layer for screen-shaped reads. The
existing API keeps its contract.**

### Stack

| Choice | Why this and not the alternative |
|---|---|
| **React 18 + TypeScript** | The hard requirements here are a virtualized million-row grid, a large DAG, and complex governed forms. That ecosystem is deepest in React, and TypeScript is what makes the API contract checkable at the boundary rather than discovered in production. |
| **Vite** | Sub-second HMR and a static `dist/` that the existing nginx serves unchanged. No runtime, no SSR, no server to operate — the deployment story does not change. |
| **`@tanstack/react-virtual`** | ~4 kB, headless, no opinion about markup. Rejected the full data-grid packages: they own the DOM, and the row is where certification, quality and proposal state get encoded. |
| **No component framework** | Tokens plus a small set of primitives, in-repo. A bank's governance console has an accessibility and contrast bar to meet; inheriting a third-party design language means fighting it. |
| **No CSS-in-JS** | Plain CSS with custom properties. Themes are token redefinitions, and the tokens are readable by a designer without a build. |

Dependencies are vendored through the lockfile and pinned exactly, consistent with how
`ui/vendor/` treats Cytoscape today. Nothing is fetched from a CDN at runtime.

### Migration — strangle, not rewrite

The new shell owns routing, navigation, persona and chrome from the first commit.
Screens not yet rebuilt are marked `legacy` in the nav and continue to be served by
`ui/`. A screen moves when it is rebuilt; there is no cutover.

This is chosen over a full rewrite for one reason: twenty screens rebuilt in one
branch is a long period during which nothing ships and the two versions diverge. It
costs a period of dual maintenance, which is the price of never being unable to ship.

**Order.** Catalog first — it is the entry-ticket gap (`00-product/04` scores Atlas
`○` against `●` for every incumbent on million-object UX, bulk actions and
virtualization) and it exercises every pattern the other screens need. Then the screens
where a surface is the only thing missing: marketplace over the delivered CX-1…CX-6
path, the lineage refusal view over LN-3, the review queue.

### Read model

A new read layer composes screen-shaped rows from the modules that own each fact. It
adds no business logic: it fans out to existing services and joins the result.

- `GET /v1/organizations/{org}/catalog/rows` → `CatalogRowRead`, one row per asset
  with description, proposal state, owner, certification, quality, glossary terms and
  row estimate composed server-side. 501 requests become 1.
- `GET /v1/metadata/tables/{id}/evidence` → the evidence pane's payload, with a
  `source` string on every claim.

Both keep the `CursorPage` keyset contract from CT-2 unchanged. Both are read-only, so
neither touches ADR-0004's execution choke point.

## Consequences

**Accepted.**

- Two rendering models coexist during the migration. Bounded by finishing it.
- An npm build step enters the pipeline. Pinned, lockfile-committed, no runtime fetches.
- The read model duplicates field definitions already expressed in module schemas. It
  stays a projection: it never becomes the place a fact is decided (ADR-0003).

**Rejected.**

- *Keep vanilla, add discipline.* Structure is the problem, not tidiness.
- *A full rewrite.* No shippable increment for months.
- *Server-rendered HTML.* A million-row virtualized grid with an evidence pane is a
  client application; pretending otherwise means reinventing one badly.

**Invariants unaffected.** The shell contains no business rules (Module 21 §4). The
persona selector remains a development convenience and grants nothing — in production
persona derives from OIDC groups (UX-1). A UI that decides is a UI that can be bypassed.
