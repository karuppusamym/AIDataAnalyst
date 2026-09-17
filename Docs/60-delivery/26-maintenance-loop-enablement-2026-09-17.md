# Maintenance loop enabled and observed — 2026-09-17

Source: `713355e`. [Tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11)
remains the only work-status authority; this is dated evidence for the rows it names
(R11-FP15, R11-FP16, R11-D17), not a second queue.

The [2026-09-16 review](../review-2026-09-16/REVIEW.md)'s F04 asked for the maintenance loop to
be configured and observed in a real environment. The
[2026-09-16 reconciliation](25-review-reconciliation-2026-09-16.md) left it at configuration
templates, for two stated reasons: the pass opens CRITICAL incidents that setting the interval
back to `0` does not undo, and `metadata_change_signal` was empty, so the pass would have
consumed nothing. Both were true. The second is what this cycle changed: there is now something
to consume, produced by a real source change, so the loop could be watched rather than reasoned
about.

## What was changed, and where

Local development stack only (`compose.yaml` services against the bundled sample source). No
shipped default changed: the intervals still ship at `0` in `.env.example`,
`infra/k8s/base/configmap.yaml` and `Settings`, which the configuration decisions register
records as deliberate.

| Setting | Value | Why |
|---|---|---|
| `AIDA_CHANGE_SIGNAL_PROCESSING_INTERVAL_MINUTES` | `15` | Signals become holds |
| `AIDA_CONTEXT_REBUILD_INTERVAL_MINUTES` | `30` | Holds become drafts, and release when nothing is stale |
| `AIDA_SAMPLE_SOURCE_DSN` | the sample container's own local-only credential | The datasource's credential reference (`env://AIDA_SAMPLE_SOURCE_DSN`) resolved to nothing, so no scan could read the source |

The two intervals are the runbook's own documented values
([16-deployment-alignment-and-enablement-runbook.md](../40-engineering/16-deployment-alignment-and-enablement-runbook.md)).
They were run at `1` while the sequence below was observed, then set to the documented cadence.

## What was observed, end to end, through the deployed services

Every step ran against the running containers — the API on `:8000`, the metadata worker and the
fleet scheduler — not through a test harness.

1. **A view was created in the sample source** (`customer.v_open_account_balance`) and a scan was
   started through the real route, `POST /v1/datasources/{id}/analysis-runs`. The run completed
   with four objects discovered, the view among them. No signal was recorded, which is correct:
   a new object is not a change (R11-FP15).
2. **The view was redefined in the source** — its `WHERE a.status = 'OPEN'` filter dropped, so
   every answer over it moves — and the datasource was rescanned through the same route.
3. **The rescan recorded the change**: one `VIEW / DEFINITION_CHANGED / STRUCTURAL` signal,
   `PENDING`, alongside `GRANT / PERMISSION_CHANGED` signals for the grants the scan read.
4. **The change-signal pass acted on it**, unattended: the signal reached `PROCESSED` with
   `{"action": "VIEW_REDEFINED", "incident_id": …}`, written by
   `scheduler:change-signal-processor`, and a **CRITICAL** `SOURCE_CHANGE` incident opened on the
   view — the hold that fails governed tools over it closed. The permission signals ended
   `RECORDED`, placing no hold, as FP16 decided.
5. **The rebuild pass released the hold**, also unattended: the incident is `RESOLVED`, resolved
   by `scheduler:context-rebuild`, with the reason "Everything standing on the redefined view was
   rebuilt against its current definition and approved, or re-approved after the change."

## The drafting half, with a published tool standing on the view

The first sequence released its hold immediately, correctly: nothing stood on that view, so the
rebuild had nothing to draft. A second sequence gave it something to hold.

6. **A governed tool was published over the live view** through the real routes:
   `POST /v1/projects/{id}/tool-blueprints/from-view` produced a DRAFT bound to the view and its
   definition fingerprint, `POST /v1/tool-versions/{id}/submit` opened the review, and a
   *different* principal approved it — maker-checker, not a flag. v1 PUBLISHED.
7. **The view was redefined again** (a `branch_code` column added to its projection) and the
   datasource rescanned.
8. **The hold reopened CRITICAL**, and this time the rebuild pass had work: it created **v2
   itself** — `created_by = scheduler:context-rebuild`, status `REVIEW_REQUIRED`, bound to the
   *new* definition fingerprint — and opened a `GOVERNED_TOOL_VERSION` / `PUBLISH` review. The
   hold stayed open while that review waited, which is the point: the pass drafts, it does not
   publish.
9. **A reviewer approved v2.** v1 became `SUPERSEDED`, v2 `PUBLISHED`, and the hold was
   `RESOLVED` by `scheduler:context-rebuild`.

That is the whole loop on the deployment — change, signal, hold, regenerated draft, human
approval, release — with a person in exactly one place: the approval.

Two things are still proven only by tests rather than here: a redrafted description and a
re-pinned context product travelling the same path (`tests/test_context_rebuild.py`,
`tests/test_footprint_journey.py`), because this estate carries neither over a live view; and
Ask answering before and after the change, which needs an approved model route in this
organization.

The lineage agent's interval was left at `0`. It needs an approved agent version and contract per
organization (`scripts/seed_task_agent.py`); an interval alone does not start it, and registering
agents in this estate was not part of this change.

## Remaining, unchanged by this cycle

- **F05 posture and delivery. Unchanged, and an earlier draft of this document said otherwise.**
  Setting the source credential lets a scan read the source; it is not the binding the
  enforcement-readiness endpoint asks for. That endpoint's `NO_BINDING_FOR_DATASOURCE` is a
  *workspace* resolution (`workspace_access`), and re-running it after this change still reports
  `ready: false` with both datasources unbound, unresolved scope proceeding undecided, and no
  workspace enforcing. No notification destination is configured either, so the delivery worker
  still has nothing verifiable to deliver to.
- **F06.5 live answer evaluation.** Still blocked on thresholds nobody has signed off and on an
  enriched live datasource.
