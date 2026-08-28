# Module 21 — Experience Shell

> Layer L5 · Owner: Product Engineering

## 1. Purpose

The product frame: persona-derived navigation, global search, command palette, evidence panes, and the interaction patterns that make a governance platform feel like a product rather than an admin console.

`00-product/04-competitive-feature-matrix.md` scores Atlas `○` on million-object UX, bulk actions, and virtualization while every incumbent scores `●`. That is an entry-ticket gap, and it lives here.

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

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Any domain logic | The owning module |
| Policy decisions | 17 policy-governance |
| Data fetching rules | The module's API |

**Rule.** The shell contains no business rules. If a screen needs a decision, the decision is made server-side by the owning module. A UI that decides is a UI that can be bypassed.

## 5. Persona routing

| Persona | Landing shell | Derived from |
|---|---|---|
| Analyst | Ask console | OIDC group → role mapping |
| Business consumer | Tool catalog | " |
| Steward | Domain overview + coverage | " |
| Reviewer | Governance queue | " |
| Platform operator | Fleet and operations console | " |
| Auditor | Audit ledger | " |

Persona chosen in a browser dropdown is a **development convenience**. In production it is derived from identity (module 01). This matters: a persona that a user can select is a persona that grants nothing, so any capability gated on it would be a fake control.

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

Non-negotiable, and currently unaudited.

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
| Coverage | Atlas portal covers every user-facing API workflow in the current slice: onboarding, analyst, catalog/impact, dbt, business meaning, semantics, tools, graph explorer, quality, model routes, fleet, query memory, outbox, audit, governance queue | Retained |
| Persona navigation | Client-side selection only | **Derive from OIDC groups** |
| Global search / command palette | Not implemented | Entry-ticket gap |
| Virtualization | Not implemented | Entry-ticket gap |
| Bulk operations | Not implemented | Entry-ticket gap |
| Accessibility | **Not audited** | Full audit + remediation |
| Permalinks / export | Partial | Full evidence permalinks |
| Onboarding wizards | Partial (tenant onboarding) | Guided setup per persona |

## 10. Open work

| ID | Item | Priority |
|---|---|---|
| UX-1 | Bind persona navigation to the approved OIDC group contract | P0 |
| UX-2 | Global search and command palette | P0 |
| UX-3 | List virtualization | P1 |
| UX-4 | Bulk selection and background bulk execution | P1 |
| UX-5 | Accessibility audit and remediation | P1 |
| UX-6 | Graph level-of-detail rendering | P1 |
| UX-7 | Evidence permalinks and export | P1 |
| UX-8 | Guided onboarding per persona | P2 |
| UX-9 | Browser regression suite | P1 |
