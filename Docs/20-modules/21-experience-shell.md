# Module 21 — Experience Shell

> Layer L5 · Owner: Product Engineering

## 1. Purpose

The product frame: persona-derived navigation, global search, command palette, evidence panes, and the interaction patterns that make a governance platform feel like a product rather than an admin console.

`00-product/04-competitive-feature-matrix.md` scores Atlas `○` on million-object UX, bulk actions, and virtualization while every incumbent scores `●`. That was an entry-ticket gap, and it lives here; section 9 says what has been built since.

## 2. Jobs served

All personas — this module is how every job is reached.

## 3. Responsibilities

- Persona derivation from identity claims and shell routing.
- Global search and command palette.
- Virtualized lists and level-of-detail graph rendering.
- Bulk selection and background bulk operations with progress.
- Evidence panes and permalinks.
- Empty states, setup wizards, and progressive disclosure.
- Accessibility.
- Export and sharing of permission-aware views.

> **Implementation status (2026-09-21).** The command palette (Ctrl+K) filters the shell's own screens (`ui-next/src/App.tsx`) and, since R11-AUD08, lists matching catalog tables under the page list with a "Search all" entry that opens the Search screen (`#/analyst/search`, over `GET /v1/search` and `GET /v1/search/suggest`: lexical, table and column names only, so a column hit cannot open its table). It does not search terms or tools. The fused `GET /v1/organizations/{organization_id}/global-search` route has no ui-next caller. The audit ledger export (`/v1/organizations/{organization_id}/audit-events/export.jsonl`) has an "Export JSONL" action on the Audit ledger.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Any domain logic | The owning module |
| Policy decisions | 17 policy-governance |
| Data fetching rules | The module's API |

**Rule.** The shell contains no business rules. If a screen needs a decision, the decision is made server-side by the owning module. A UI that decides is a UI that can be bypassed.

## 5. Persona routing

The shell has five personas. Consumer is a work area, not a persona: Marketplace and Portfolio analytics sit in it, and an Analyst has it one click away.

| Persona | Lands on | Derived from |
|---|---|---|
| Analyst | Ask Atlas | OIDC `groups` claim → persona (`oidc_persona_mappings`) |
| Steward | Stewardship | " |
| Reviewer | Review queue | " |
| Operator | Sources | " |
| Auditor | Audit ledger | " |

The persona comes from the verified `groups` claim: the first group, in claim order, that maps to a persona wins, and `oidc_default_persona` is the fallback for a principal in no mapped group (`src/aida/oidc.py`). Roles come separately, from the roles claim through `oidc_role_mappings`. Persona is navigation only. Every persona's sidebar lists every screen; the persona picks the landing screen (the first screen of its work area, when the URL names none) and the Overview checklist. What a person may do is decided by the backend on every request from their roles, never from persona. (The Agent inbox also takes a persona, chosen on that screen, only to decide whether its pending list is queue-wide or limited to the caller's own proposals.)

Persona chosen in a browser dropdown is a **development convenience**, and the dropdown exists only when the identity provider is the development one. A development build can also start in a given persona (`VITE_DEV_PERSONA`) and organization (`VITE_DEV_ORG_ID`); `scripts/demo-users.ps1` sets both so each demo user opens as their own. It changes the shell's presentation, not the identity or roles sent with requests. In production it is derived from identity (module 01). This matters: a persona that a user can select is a persona that grants nothing, so any capability gated on it would be a fake control.

## 6. Scale-safe UI requirements

| Requirement | Target |
|---|---|
| Table lists | Virtualized; 1M rows without lockup |
| Graph | Level-of-detail rendering; server-bounded neighbourhoods with explicit truncation |
| Search | First results < 1 s; progressive load |
| Bulk selection | 10,000 items without freeze |
| Bulk execution | Background with progress and cancellation |
| Large DAGs | Virtualized, collapsible |
| Time to interactive | < 3 s on a corporate-standard laptop |

## 7. Evidence-first interaction

Every result, semantic object, quality signal, and decision shows **why it exists**. This is the interaction-level expression of differentiator D3.

| Surface | Evidence shown |
|---|---|
| Analyst answer | Interpretation, semantic version, policy version, lineage, confidence, quality warnings, masking applied |
| Refusal | The control that fired, its version, reason codes, remediation path |
| Semantic annotation | Inference evidence, confidence, approver, approval date |
| Relationship | Contributing signals with weights, confidence, decision history |
| Quality incident | Observation history, baseline, threshold, fingerprint |
| Governance decision | Maker, checker, rationale, version delta |

Evidence panes are **permalinkable** so a user can send a colleague the evidence, not a screenshot.

## 8. Accessibility

Non-negotiable. Automated checks exist (section 9); human acceptance is still open (tracker row R11-C2, [accessibility acceptance](../60-delivery/24-accessibility-acceptance-2026-09-12.md)).

| Requirement | Standard |
|---|---|
| Keyboard navigation | All interactive elements reachable and operable |
| Focus management | Visible, logical order, restored after modals |
| ARIA | Correct roles, labels, live regions for async updates |
| Contrast | WCAG AA minimum |
| Screen reader | Validated on the primary flows |
| Motion | Respects reduced-motion preference |

## 9. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Coverage | Atlas portal covers the user-facing API workflows in the current slice: onboarding, analyst, catalog/impact, dbt, business meaning, semantics, tools, graph explorer, quality, model routes, fleet, query memory, outbox, audit, governance queue. Not all of them: some API workflows still have no screen (tracker row R11-X5 lists clusters with owners and dates; the [product surface catalog](../00-product/06-product-surface-catalog.md) notes several of them) | Retained |
| Persona navigation | Derived from OIDC groups (UX-1); the dropdown is development-only and changes presentation, not identity | Retained |
| Global search / command palette | Ctrl+K palette that jumps between screens and lists matching tables; a Search screen over the lexical `/v1/search` routes (R11-AUD08); the fused `global-search` route has no caller | Fused lexical, vector and graph search in the UI; a column hit that opens its table |
| Virtualization | `VirtualList` (`ui-next/src/components/VirtualList.tsx`), used in 17 files as of 2026-09-20 | Retained |
| Bulk operations | Stewardship Bulk actions (tag, classify, own, certify) and a Review batch queue | Background execution with progress and cancellation (section 6) |
| Accessibility | Automated: a jsdom axe sweep of every navigation screen, plus 27 Playwright cases in a real browser (axe with contrast in the light and dark themes, and 320 px reflow) as of 2026-09-20. Human acceptance is open (R11-C2) | Human acceptance: screen reader, contrast, zoom, multi-screen |
| Permalinks / export | Every link is built by one `buildLink` (`ui-next/src/lib/routes.ts`) and carries a screen's own filters, never the tenant; asset evidence export exists; the audit ledger export has no ui-next caller | Full evidence permalinks |
| Onboarding wizards | Per-persona checklists on Overview (`ui-next/src/components/OnboardingWizard.tsx`, UX-8); first-source setup | Retained |

## 10. Open work

*Status as of 2026-09-20 from the UX rows in [tracker section P](../60-delivery/03-tracker.md) and its sources; the tracker wins if they disagree. The table is the original scope, so read the Status column, not the row's presence, as what is still open.*

| ID | Item | Priority | Status |
|---|---|---|---|
| UX-1 | Bind persona navigation to the approved OIDC group contract | P0 | DONE |
| UX-2 | Global search and command palette | P0 | Palette DONE; lexical Search screen delivered (R11-AUD08); the fused route has no caller |
| UX-3 | List virtualization | P1 | DONE |
| UX-4 | Bulk selection and background bulk execution | P1 | DONE for selection and the Stewardship and Review batch surfaces; a bulk request answers synchronously with a per-item result, with no progress or cancellation |
| UX-5 | Accessibility audit and remediation | P1 | Automated half DONE; human acceptance open (R11-C2, PARTIAL) |
| UX-6 | Graph level-of-detail rendering | P1 | DONE |
| UX-7 | Evidence permalinks and export | P1 | DONE for permalinks and asset evidence export; audit ledger export has no ui-next caller |
| UX-8 | Guided onboarding per persona | P2 | DONE |
| UX-9 | Browser regression suite | P1 | DONE (R11-B11): 46 Playwright tests as of 2026-09-20 in `e2e/tests/` |
