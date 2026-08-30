# Experience Shell Rebuild — Implementation Plan

> Decision: `10-architecture/adr/ADR-0021`. Spec: `20-modules/21-experience-shell.md`.
> Tracker: section M of `60-delivery/03-tracker.md`.

## 1. What is actually wrong

The backend is not the problem. 69,180 lines across 39 API modules and 320 endpoints,
governed by nineteen ADRs and a Tier-0 invariant suite. It does not need redefining.

The portal is 3,655 lines of imperative DOM assembly with no component boundary, no
build, and no tests. It cannot express virtualization, cannot compose an evidence pane,
and applies HTML escaping as a per-call-site discipline.

And there is one real backend gap, which is not a defect in the API but the absence of
a layer above it: `MetadataTableRead` returns eight fields, so a governed catalog row
costs five extra requests per asset. **One hundred rows is 501 requests.**

## 2. Phasing

Each phase ends with something shippable. Nothing here requires a cutover.

### Phase 1 — Foundation and the reference screen

| # | Work | Exit |
|---|---|---|
| 1.1 | `ui-next/` scaffold: React 18, TypeScript strict, Vite, pinned lockfile | `npm run build` green; `dist/` served by the existing nginx |
| 1.2 | Token layer — colour, type, spacing, semantic state ramp separate from accent | Both themes AA at every text size; focus visible globally; reduced-motion honoured |
| 1.3 | Primitives — pill, state marker, button, field, empty, error | Errors state what happened and what to do next |
| 1.4 | Shell — persona-aware nav, `legacy` markers, strangle seam | Unrebuilt screens still reachable |
| 1.5 | **Catalog screen** (reference implementation) | Below |
| 1.6 | `CatalogRowRead` read-model endpoint | 501 requests → 1 |
| 1.7 | Evidence endpoint with a `source` on every claim | Pane renders without a second call per field |

**Delivered.** 1.1–1.5 and 1.7 exist in `ui-next/`, running against fixtures.
Verified: 1,000,000 rows with 30–42 row elements in the DOM at any scroll depth;
keyset paging on approach to the end of the loaded window; no console errors in either
theme; no page-level horizontal scroll at 700px.

**Remaining.** 1.6 — the server side of the read model. The client is already typed
against it (`ui-next/src/lib/types.ts`); set `VITE_USE_FIXTURES=0` when it lands.

### Phase 2 — Screens where only the surface is missing

Ordered by how much already-built capability each one exposes.

| # | Screen | Exposes | Notes |
|---|---|---|---|
| 2.1 | Review queue | GovernanceReview, maker-checker | Highest daily use; a change set is one proposal, not eight queue items |
| 2.2 | Marketplace | CX-1…CX-6, delivered and API-only today | Storefront over a governed read path — see AT-3 |
| 2.3 | Lineage + refusal view | LN-2, LN-3, LN-4 | The refusal edge is built and has no screen |
| 2.4 | Studio change sets | ST-A1, ST-A2, ST-A3, ST-A5 | Diff, test results, impact preview in one view |

### Phase 3 — Spec completion

UX-1 (persona from OIDC), UX-4 (bulk execution with progress and cancellation),
UX-5 (accessibility audit), UX-6 (graph level-of-detail), UX-8 (onboarding),
UX-9 (browser regression suite). UX-3 and UX-7 are satisfied by the Catalog pattern
as each screen adopts it.

### Phase 4 — Retire `ui/`

When no nav entry is marked `legacy`, delete `ui/` and its nginx route.

## 3. The Catalog pattern

Every screen that moves across copies this. It is the deliverable of Phase 1, more
than the screen itself.

1. **State that a colleague can be sent lives in the URL.** Filters and the selected
   asset are query parameters, not component state. This is what makes UX-7 a
   property of the shell rather than a feature bolted onto one screen.
2. **One request in flight per view, and it is abortable.** A newer filter aborts the
   older request and a sequence guard discards any late response. Racing writes to the
   same state is the most common cause of a UI showing rows that do not match the
   filter on screen.
3. **Fixed row height, windowed rendering.** Only the visible window is in the DOM.
   A column that would wrap gets truncated with a `title`; nothing is allowed to
   change row height, because measurement is what breaks at a million rows.
4. **The grid is a real grid.** `aria-rowcount` carries the true total and each row its
   absolute `aria-rowindex` — otherwise a screen reader announces "row 3 of 40" two
   hundred thousand rows down.
5. **Proposed is never rendered as established.** ADR-0001: models propose. A
   model-written description carries a `proposed` marker that is never truncated —
   a half-rendered claim is worse than no claim.
6. **State has shape as well as colour.** Quality carries a stripe alongside its pill;
   colour alone fails for roughly 8% of male users.
7. **Progressive disclosure by priority.** Columns drop by container width, not window
   width — the table narrows when the evidence pane opens, which a media query cannot
   see. Certification and quality are the last columns to go, because they are what
   decide whether an agent may use the asset.
8. **Errors carry the server's own detail** and a retry. Never "something went wrong".

## 4. Read-model layer

A projection, not a decision point (ADR-0003). It fans out to the owning modules and
joins; it holds no business logic and performs no writes, so ADR-0004 is untouched.

| Endpoint | Composes from | Replaces |
|---|---|---|
| `GET /v1/organizations/{org}/catalog/rows` | catalog, business meaning, GL-2 ownership, GL-5 certification, data quality, glossary links, profile | 1 + 5n requests |
| `GET /v1/metadata/tables/{id}/evidence` | the same, plus consumption lineage (CX-4) and AI decision lineage (LN-3) | per-field fetches |

Both preserve the CT-2 `CursorPage` keyset contract, including `total: null` whenever
a cursor is in play. Both are permission-filtered by the same gate as the underlying
module reads — the read model must never widen what a principal can see.

## 5. Known limitations

- **Narrow viewports stack the navigation full-height above the content.** Functional,
  not good. A desktop-first governance console for a bank; a collapsible nav is
  deferred rather than forgotten.
- **`ui-next/src/lib/types.ts` is hand-written.** Correct against `schemas.py` as of
  2026-08-30 and will drift. The fix is generation from the FastAPI OpenAPI document
  (UX-11), not hand-patching.
- **No frontend tests yet.** UX-9 covers the browser regression suite; component-level
  testing should land with Phase 2, not after it.

## 6. What this plan deliberately does not do

- It does not restructure the backend. ST-05…ST-10 are tracked separately and are not
  prerequisites.
- It does not change any API contract. The read model is additive.
- It does not move business rules into the client. Module 21 §4 holds: a UI that
  decides is a UI that can be bypassed.
