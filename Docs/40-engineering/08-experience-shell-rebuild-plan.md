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

---

# Revision — 2026-08-30, after `Docs/Atlan-context.docx`

A 43-screen capture of Atlan's actual product UI (not marketing crops) changes parts of
this plan. What follows is what changed, what did not, and one thing the earlier
analysis got wrong.

## 7. What the earlier analysis got wrong

It claimed Atlan has "no equivalent at all" to AI decision lineage. It does: a **Trace
Explorer** capturing every agent interaction with its context-retrieval path, the
semantic object and version resolved, and a correct/flagged status — plus a loop where
a user marking an answer wrong generates a suggested context fix, and marking it right
promotes it to a regression test.

What is absent from all 43 screens is a **refusal**. Every trace is an answer that was
given. None is an answer declined, with the control that declined it named. The
differentiator is therefore narrower than stated and still real: they trace what was
used; LN-3 also records what was refused. Do not position on "they have nothing here."

## 8. Patterns worth adopting, and what each changes

Ordered by how much they change. Each is a design decision, not a feature request.

### 8.1 A proposal is a diff + confidence + a reason — **implemented**

Their review surfaces never show a proposed change without a rendered diff, a numeric
confidence, and a **"Why:"** line citing the evidence it was derived from ("9 of 12
churn queries reference enterprise accounts only; the SMB exclusion is implicit in your
dashboards"), then symmetric Approve / Reject.

This is what ADR-0001 requires and the old portal never expressed. A model-proposed
change with no rationale is not reviewable — the reviewer is being asked to
rubber-stamp. Now a shell primitive (`ProposalCard`) where `rationale` and `evidence`
are **required fields on the type**, for the same reason `esc()` should never have been
optional: the one that gets skipped is the one that mattered.

### 8.2 The review queue is a run report, not a table — **implemented**

Their queue opens by stating what a run did: *44 passed · 8 applied automatically ·
3 need your review*, then shows only what needs judgment, with the auto-applied set one
click away.

A table of pending rows makes the reviewer reconstruct that themselves. Rebuilt
(`ReviewQueueScreen`), with one deliberate difference: the count of auto-applied changes
is as prominent as the count needing review, because a steward who cannot see what was
applied without them cannot audit the threshold that let it through. Counts are derived
from the list, never carried beside it.

### 8.3 Propagation states its mechanism — **implemented**

When quality or a classification spreads along lineage, they render a **propagation
log** where every hop names how it travelled: "Downstream impact detected: revenue_agg
depends on raw_sales *via column lineage*."

ADR-0016 has quality fail closed, so a tool call can be refused because of a check three
hops upstream. "Affected" is a claim; "affected via column lineage from raw_sales" is an
argument. Now a primitive (`PropagationLog`), and the review queue uses it to show the
chain from a failed rule to a refused tool call.

### 8.4 Lineage is agent-narrated, not panned — changes 2.3

Their Lineage Agent answers "why did revenue drop 12%" by streaming its traversal on the
left — each hop with the evidence found ("847 nulls in net_revenue since Jan 8") ending
at a root cause — while the graph highlights the path on the right and marks the culprit
node red.

This is the answer to LN-8 (large-DAG virtualization): for the common case you do not
render the DAG at all. Rebuild 2.3 as narrated traversal with the graph as the
supporting view, not a canvas the user pans. Atlas can narrate one thing they cannot —
the hop where the agent declined.

### 8.5 Blast radius is always visible on the artifact — changes 2.4

Every context object shows who consumes it: `consumed_by: 6 agents` inline in the
artifact, a "Consuming this repo — all on v3.1.2" footer, and a deploy view listing each
consumer with the protocol it uses and its synced version.

Atlas has this data in `consumption_lineage.py` (CX-4) and shows it nowhere. Editing a
semantic object without seeing what depends on it is the single most consequential blind
spot in the current portal. Add a consumer footer to every semantic and context-product
surface — this is a small change with a large effect and it should not wait for 2.4.

### 8.6 Plain-language layer beside the generated artifact — changes ST-A4

Their authoring screen is split: **HUMAN LAYER — PLAIN LANGUAGE** (labelled fields, each
with a `+ AI` button) beside **MACHINE LAYER — AUTO-GENERATED YAML** that updates live,
with AI-inferred lines marked inline (`# + AI inferred`) and a footer naming what can
parse it.

Better than the form ST-A4 currently implies: domain experts never write YAML, but the
artifact stays visible and the machine-written parts are marked. Adopt this shape for
the parameter-contract designer.

### 8.7 Agents publish their method, not just their name — changes the roster

Each agent has a screen with **PURPOSE**, a numbered **TASK PLAN** ("Scan SQL query
history → Identify top-queried assets → Rank by usage score → Auto-apply or route to
steward"), and live results.

The earlier advice was "name the pipelines." That was too weak. The task plan makes the
agent's method inspectable *before* you trust its output, which is a governance
affordance, not branding. Several plans end with "auto-apply or route to steward" — the
graduated-autonomy rule stated as part of the method.

### 8.8 Composite confidence has named dimensions — changes AG/TL scoring

They score every output on **accuracy, clarity, style and completeness** and composite
those. A single opaque number cannot be argued with; four can be tuned.

### 8.9 Conflicting definitions get a screen — new

Their Sage agent renders two definitions of the same metric side by side with both
formulas, states the risk ("agents querying MRR return different numbers depending on
which definition they hit"), and routes to **both** named stewards.

Atlas has maker-checker but no concept of "two domains disagree, and both owners must
sign." Worth a tracker row; the review queue now carries a worked example.

### 8.10 Evals are organised by persona — changes ST-A2

Their suite groups tests by who asks (Data Analyst / CEO / Analytics Engineer) and each
test carries provenance back to the dashboard or query it was mined from.

ST-A2 tests semantic correctness. Grouping by persona is a better frame for a steward
deciding whether the layer is ready, and the provenance chip makes a failing test
arguable.

### 8.11 Schedules are built, not typed — changes AT-1

Frequency + run time + timezone + day-of-week toggle pills. No cron string. The playbook
object (AT-1) should expose this, not a crontab field, and the connector wizard should
keep "Test authentication" as an explicit step before anything runs.

## 9. What does not change

The Catalog pattern in §3 holds — nothing in the capture contradicts it, and their grids
are less information-dense than ours. The stack decision (ADR-0021) is unaffected. The
read model (UX-12) is more clearly right than before: every screen here composes facts
from many modules into one view, which is exactly what 501 requests per page cannot do.

And the strategic read is unchanged. Six of the seven concepts remain packaging problems
where Atlas has the capability and no surface. This capture mostly shows, in detail, what
those surfaces should look like.
