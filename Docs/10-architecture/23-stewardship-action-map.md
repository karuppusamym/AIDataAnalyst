# Stewardship action-to-destination map

Date: 2026-09-19. Inspected against `1295c63` plus the R11-S13 slice described in section 8.
This is dated evidence, not a queue. [Tracker section P](../60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11)
row R11-S13 owns status. The design this map serves is
[items 13–17, sections 15 and 17](21-graphql-okf-and-workspace-design.md#17-stewardship-merge-workflows-preserve-authority).

Design section 17 says: *"Before removing any screen/module, inventory its actions, APIs,
role visibility, URL parameters and tests."* This document is that inventory for the
Stewardship grouping (Work queue, Bulk actions, Automation) and for the two candidate queues
whose linking is in scope. It covers every action the UI offers on six destinations:
Stewardship, Playbooks, Task agents, Negative knowledge, Relationships and Cross-source.
Documentation, Business Meaning, the Review queue and Catalog are retained workspaces. They are
listed at entry-point level only (section 5), and none of their actions moved.

**Sources.** API roles come from the generated
[surface-to-control matrix](../50-security/surface-control-matrix.md) (the `require_roles` tuple
FastAPI wires into each route). `require_roles` has no role hierarchy, so a principal needs one
of the listed roles literally. Two entries were spot-checked against the handler source:
`aida.intelligence_api.list_relationship_candidates` and
`aida.intelligence_api.list_cross_source_object_resolution_candidates`. UI gates come from the
screen source. Deep links use the canonical `#/journey/screen` form that `buildLink` writes.
Every retired spelling still resolves (section 7).

## 1. Role sets used below

| Set | Roles | Where it applies |
|---|---|---|
| **BULK** | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | Catalog `bulk-*`, playbook writes and dry run, lineage agent run |
| **STEW** | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | `stewardship_api` writes, steward agent run |
| **ORG-READ** | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | Stewardship reads |
| **DECIDE** | DataSteward, MetadataReviewer, PlatformAdmin | Relationship and same-object decisions |
| **DISCOVER** | DataAdmin, MetadataAdmin, PlatformAdmin | Candidate discovery |
| **CAND-LIST** | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | Candidate list reads |

**UI gate.** No screen in this map hides or disables an action based on the caller's role. The
server's `require_roles` is the only role check. The UI gates below depend only on the state of
the data: input validity, server-reported agent state, and the server-computed per-row
`can_review` on Relationships. This slice left every one of them unchanged.

## 2. Stewardship workspace — `#/steward/stewardship`

### 2.1 Work queue (default view, `?view=` absent or `queue`)

| Action | API | API roles | UI gate | Was | Design home |
|---|---|---|---|---|---|
| Browse the unowned-table backlog (status filter and an exact candidate-owner filter, both sent as query parameters and applied before paging) | `GET /v1/organizations/{organization_id}/stewardship/unowned-backlog` | ORG-READ | none | Stewardship, right panel | Work queue |
| Route the backlog (optional one-source scope) | `POST …/stewardship/unowned-backlog/route` (`aida.stewardship_api.route_unowned_asset_backlog`) | STEW (DataAdmin may run bulk actions but **not** route) | disabled while routing | Stewardship, right panel | Work queue |
| See your own expiring ownerships (14-day window) | `GET /v1/organizations/{organization_id}/ownership-assignments` | ORG-READ | shows only rows owned by the session principal | Stewardship, top banner | Work queue |
| Reaffirm one ownership | `POST /v1/ownership-assignments/{assignment_id}/reaffirm` | STEW | none | banner | Work queue |
| Reaffirm all | `POST /v1/ownership-assignments/bulk-reaffirm` | STEW | none | banner | Work queue |

Contextual links (no action, navigation only): Documentation priorities (`#/steward/worklist`)
and Negative knowledge (`#/steward/negative-knowledge`).

### 2.2 Bulk actions (`?view=bulk`; a link with `action`/`field`/`pattern` and no `view` also opens it)

| Action | API | API roles | UI gate | Was | Design home |
|---|---|---|---|---|---|
| Tag tables | `POST /v1/organizations/{organization_id}/tables/bulk-tag` (`atlas.modules.catalog.router.bulk_tag_tables`) | BULK | valid filter + tag key | Stewardship, left panel, `?action=tag&ds=&field=&pattern=` | Bulk actions |
| Classify columns | `POST …/tables/bulk-classify` (`atlas.modules.catalog.router.bulk_classify_columns`) | BULK | valid filter | same, `?action=classify` | Bulk actions |
| Assign ownership | `POST …/tables/bulk-own` (`atlas.modules.catalog.router.bulk_assign_ownership`) | BULK | valid filter + owner | same, `?action=own` | Bulk actions |
| Certify tables | `POST …/tables/bulk-certify` (`atlas.modules.catalog.router.bulk_certify_tables`) | BULK | rationale ≥ 10 chars, future expiry | same, `?action=certify` | Bulk actions |

All four use the filter path only (one datasource plus a match field and pattern). None of the
four routes has a preview mode, so a run applies when it is submitted. Design section 17's
*"reuse the same selection/form from Catalog"* needs the explicit-id path (`table_ids` /
`column_ids`) from Catalog's row selection. Tracker slice 17B owns that work, and this slice
did not add it.

### 2.3 Automation (`?view=automation`; `#/playbooks` is a retired alias)

| Action | API | API roles | UI gate | Was | Design home |
|---|---|---|---|---|---|
| List playbooks | `GET /v1/organizations/{organization_id}/playbooks` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | none | `#/steward/playbooks` | Automation |
| Create playbook | `POST /v1/organizations/{organization_id}/playbooks` (`aida.playbooks_api.create_playbook`) | BULK | form validity | `#/steward/playbooks` | Automation |
| Enable / disable | `PATCH /v1/playbooks/{playbook_id}` | BULK | none | `#/steward/playbooks` | Automation |
| Delete (confirm dialog) | `DELETE /v1/playbooks/{playbook_id}` | BULK | confirmation | `#/steward/playbooks` | Automation |
| Run now | `POST /v1/playbooks/{playbook_id}/run` (`aida.playbooks_api.run_playbook_now`) | BULK | disabled while the playbook is disabled | `#/steward/playbooks` | Automation |
| Dry run | `GET /v1/playbooks/{playbook_id}/dry-run` (`aida.playbooks_api.dry_run_playbook_now`) | BULK | — | no `ui-next` caller at `1295c63`; a concurrent session is adding one inside `PlaybooksScreen` | Automation |

The Automation view renders `PlaybooksScreen` unchanged, so anything added inside that screen
appears in the Automation view without a route change.

Contextual links: the steward, lineage and quality agent consoles
(`#/steward/task-agents?agent=steward|lineage|quality`).

## 3. Destinations kept separate and linked from the workspace

### Task agents — `#/steward/task-agents?agent=` (retired: `#/steward-agent`, `#/lineage-agent`, `#/quality-agent`)

| Action | API | API roles | UI gate | Design home |
|---|---|---|---|---|
| Read agent state, tier, kill switch, outcomes | `GET /v1/organizations/{organization_id}/{kind}-agent` | AgentDeveloper, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, ModelRiskManager, Operations, PlatformAdmin, Reviewer, SemanticAdmin | none | own destination |
| Run / preview (`dry_run`) the steward agent | `POST …/steward-agent/run` | STEW | disabled while unregistered or blocked | own destination |
| Run / preview the lineage agent | `POST …/lineage-agent/run` | BULK | same | own destination |
| Run / preview the quality agent | `POST …/quality-agent/run` | DataAdmin, DataSteward, Operations, PlatformAdmin | same | own destination |

Design section 17 names *"bounded agent execution"* as distinct from human-authored playbook
configuration. For that reason it has an entry point from Automation, and it was not absorbed.

### Negative knowledge — `#/steward/negative-knowledge?assertion_type=&subject=&suppression=`

| Action | API | API roles | UI gate | Design home |
|---|---|---|---|---|
| Browse / filter rejected assertions | `GET /v1/negative-knowledge/search` | DataEngineer, DataSteward, PlatformAdmin, Viewer | none | contextual evidence view + advanced Work queue filter |
| Assertions for one subject | `GET /v1/negative-knowledge/{subject_id}` | same | none | same |
| Lift a suppression (confirm dialog) | `POST /v1/negative-knowledge/{assertion_id}/lift-suppression` | DataSteward, PlatformAdmin | confirmation | same |

This slice added only the Work queue link. The advanced Work queue filter is not built.

## 4. The two candidate queues: kept apart, linked on their own terms

The review's M2 merge was declined on 2026-09-16, and this slice keeps that decision. The two
queues use different scope axes: datasource read authorization on one side, ADR-0017
domain-grant authorization on the other. They have different read models and own different
writes. The contextual links added here carry only the scope the *target* reads. They grant
nothing.

### Relationships — `#/steward/relationships?ds=&candidate=`

| Action | API | API roles | UI gate |
|---|---|---|---|
| Review queue (impact-ordered) | `GET /v1/datasources/{datasource_id}/relationship-candidates/review-queue` | Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | none |
| Decide one | `POST /v1/relationship-candidates/{candidate_id}/decision` | DECIDE | per-row `can_review` (server maker-checker) |
| Bulk decide a checked set | `POST /v1/relationship-candidates/bulk-decision` | DECIDE | per-row failures reported |
| Decision history | `GET /v1/datasources/{datasource_id}/relationship-candidates` | CAND-LIST | none |
| Join validation | `GET /v1/relationship-candidates/{candidate_id}/validation` | Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | none |
| Confidence calibration tile | `GET /v1/relationship-candidates/confidence-calibration` | CAND-LIST | none, and never blocks the queue |

**Link added:** "Cross-source candidates in this source's domain" opens
`#/steward/cross-source?dom=<the selected source's data_domain_id>`. A source with no domain
gets an unscoped link.

### Cross-source — `#/steward/cross-source?dom=&status=`

| Action | API | API roles | UI gate |
|---|---|---|---|
| List domains | `GET /v1/organizations/{organization_id}/lines-of-business` → `GET /v1/lines-of-business/{lob_id}/data-domains` | DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer | none |
| List sources | `GET /v1/organizations/{organization_id}/datasources` | Analyst, DataAdmin, MetadataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | none |
| Discover cross-source relationships | `POST /v1/data-domains/{domain_id}/relationship-candidates/discover-cross-source` | DISCOVER | a 403 on a cross-domain scan becomes the grant request |
| Discover same-object tables | `POST /v1/data-domains/{domain_id}/cross-source-object-resolution-candidates/discover` | DISCOVER | same |
| List same-object candidates | `GET /v1/datasources/{datasource_id}/cross-source-object-resolution-candidates` | CAND-LIST | none |
| List cross-source relationship candidates | `GET /v1/datasources/{datasource_id}/relationship-candidates`, filtered client-side | CAND-LIST | none |
| Decide a same-object candidate | `POST /v1/cross-source-object-resolution-candidates/{candidate_id}/decision` | DECIDE | none |
| Decide a cross-source relationship | `POST /v1/relationship-candidates/{candidate_id}/decision` | DECIDE | none |
| See / request cross-boundary grants | `GET` / `POST /v1/data-domains/{domain_id}/cross-boundary-grants` | read: DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer; request: DataAdmin, DataSteward, OrganizationAdmin, PlatformAdmin | none |

**Links added:** once a domain is chosen, the page shows a "Same-source review" link for each
source in that domain: `#/steward/relationships?ds=<source>`. It shows at most six, and after
that one link to the Relationships picker. Each link opens the per-source queue. Nothing
merges the queues.

## 5. Retained workspaces (entry points only; nothing moved)

| Destination | Deep link | Relationship to Stewardship |
|---|---|---|
| Documentation | `#/steward/worklist?view=priorities\|drafts\|imports` (retired: `#/description-drafts`, `#/data-dictionaries`) | Linked from Work queue; its tab hierarchy is not nested here |
| Business Meaning | `#/steward/meaning` | Unchanged; one glossary/ontology authoring destination |
| Review queue | `#/reviewer/governance?queue=` (retired: `#/parsed-lineage-review`) | Independent; no Stewardship shortcut approves anything it owns |
| Catalog row selection | `#/analyst/catalog?asset=` | Its checked-row controller offers description drafts (`POST …/asset-description-drafts/generate`, STEW) and a disabled "Certify…" stub; see section 9 |

## 6. Stewardship server surfaces with no `ui-next` caller

A path search of `ui-next/src` on 2026-09-19 found no caller for any of the surfaces below.
Nothing was removed from the UI, so none of them is a lost capability. They are listed so that
a later slice does not mistake them for one.

| Surface | Write roles | Plausible home |
|---|---|---|
| `GET` / `POST /v1/organizations/{organization_id}/stewardship/bulk-operations` | STEW | Bulk actions. Needs a decision first (see below). |
| Ownership rules: `GET` / `POST …/ownership-rules`, `POST /v1/ownership-rules/{rule_id}/apply` | STEW | Work queue |
| Coverage: `GET …/stewardship/coverage`, `GET` / `POST …/coverage/snapshots` | STEW | Work queue |
| `POST …/stewardship/leaver-reassignment` | STEW | Work queue |
| Glossary categories, conflicts (detect, resolution) and link proposals (generate, submit) | STEW | Business Meaning |

Playbook runs already use two write paths, and the match count decides which one
(`aida.playbooks.evaluate_and_run_playbook`):

- **At or below the playbook's `auto_apply_max_items`:** the run applies immediately through
  the same per-item cores that `bulk-*` calls.
- **Above it:** the run queues a `BulkStewardshipOperation` behind a governance review.

So the platform already has both an immediate bulk path and a reviewed one. A Catalog-selection
bulk action could use either, and could switch between them by scale the way playbooks do.
Design section 17 says not to add a second endpoint, so that choice is a 17B decision.

## 7. Deep links, before and after this slice

| Link | Before | After |
|---|---|---|
| `#/steward/stewardship`, `#/stewardship` | both panels on one page | Work queue |
| `…/stewardship?action=&ds=&field=&pattern=` (no `view`) | bulk panel with that filter | Bulk actions with that filter (`stewardshipViewFrom`) |
| `…/stewardship?ds=` only | both panels | Work queue. `ds` is inherited estate context and does not imply a view. |
| `…/stewardship?view=queue\|bulk\|automation` | — | that view |
| `#/playbooks`, `#/steward/playbooks`, any stale journey prefix | Playbooks | Stewardship → Automation (`RETIRED_SCREEN_ALIASES`) |
| In-app `navigateTo("playbooks")`, `App.navigate("playbooks")` | Playbooks | Automation, through `resolveScreenRef` |
| `#/steward/task-agents?agent=`, `#/steward/negative-knowledge…`, `#/steward/relationships…`, `#/steward/cross-source…` | unchanged | unchanged |

The sidebar goes from 38 destinations to 37, and the Playbooks item is gone. The command
palette still finds "playbook", "scheduled" and "automation" under Stewardship.

## 8. What this slice changed, and what it did not

- **Permissions: unchanged.** No UI role gate was added or removed, and no endpoint, request
  body or control's enablement moved.
- **Unsaved-change protection.** A view switch is `patchQuery`, which bypasses the shell's
  navigation guard. The tab bar therefore asks `lib/unsavedChanges` itself, following the
  Documentation workspace's pattern. The bulk form now reports an edited, un-run action. It
  did not need to before, because only leaving the page could unmount it. The Playbooks create
  form does **not** report yet. Its file belongs to concurrent work, and the tab bar will cover
  it once it does.
- **Keyboard.** The tablist has one tab stop. Left and Right arrows move between views and
  wrap at the ends. Home and End jump to the first and last view. If the unsaved-change prompt
  is declined, both the view and the focus stay where they were.
- **Tests.** `ui-next/src/screens/StewardshipWorkspace.test.tsx` covers the view axis, legacy
  links, unsaved-change behaviour, keyboard and links. The other files cover these areas:
  - `StewardshipScreen.test.tsx`: bulk-form dirty reporting.
  - `lib/routes.test.ts`: the alias, and the destinations that were deliberately *not*
    aliased.
  - `App.test.tsx`: shell deep links and the palette.
  - `RelationshipsScreen.test.tsx` and `CrossSourceScreen.test.tsx`: the contextual links.
  - `a11y-sweep.test.tsx`: all three views.
- **Not verified here:** a real browser. The slice was checked in jsdom only.

## 9. Noticed, not changed

- **Deciders cannot list some candidates.** A principal whose only roles are DataSteward or
  MetadataReviewer (two of the three DECIDE roles) is outside CAND-LIST. For that principal:
  - Relationships' decision history returns 403.
  - Cross-source's relationship list catches each per-source 403 and shows an empty page, so
    the list looks empty rather than refused.
  - Cross-source's same-object list and domain list are also closed to that principal.

  This is a backend role-tuple question, and nothing in this slice changes it.
- **Catalog "Certify…" stub.** Catalog's disabled "Certify…" button says *"Bulk certify is not
  available yet — certification is managed per asset in stewardship."* Bulk certify by filter
  is available in Bulk actions. The explicit-id path from a Catalog selection is what 17B would
  add.
- **`navigateTo` skips the unsaved-change guard.** `components/CrossLinks.tsx` navigates with
  `navigateTo`, which does not ask `lib/unsavedChanges`, unlike the shell's `navigate`. No
  contextual link in this slice appears on a view that holds a dirty form. A link placed next
  to a dirty form elsewhere would discard it without asking.
