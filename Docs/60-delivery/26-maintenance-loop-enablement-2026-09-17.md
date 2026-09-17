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
| `AIDA_SAMPLE_SOURCE_DSN` | the sample container's own local-only credential | `Sample Bank Source` had no live binding, so nothing could scan it — the prerequisite F05 names |

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

## What this does and does not prove

It proves the loop runs on its own in a deployed environment: a source change is observed by a
scan, becomes a signal, becomes a hold, and the hold is released by the pass rather than by a
person — the half that had only ever been exercised by tests driving the functions directly.

It does not prove the drafting half on this estate, and the distinction matters. Nothing stood on
this view — no governed tool, no approved description, no context product — so the rebuild had
nothing to draft and released the hold on its first pass. The drafting chain (a regenerated tool
version, a redrafted description, a re-pinned product, each into its review queue, and the hold
held until a reviewer approves them) is proven by `tests/test_footprint_journey.py` against live
PostgreSQL and SQL Server, and by `tests/test_context_rebuild.py` case by case. Proving it here
as well needs an estate with published artifacts over a live view, which the sample source's
three tables do not yet carry.

The lineage agent's interval was left at `0`. It needs an approved agent version and contract per
organization (`scripts/seed_task_agent.py`); an interval alone does not start it, and registering
agents in this estate was not part of this change.

## Remaining, unchanged by this cycle

- **F05 posture and delivery.** Binding `Sample Bank Source` removes one of the three blockers the
  enforcement-readiness endpoint reports. Two datasources remain unbound, unresolved-workspace
  requests still proceed undecided, and the one ACTIVE workspace is still in SHADOW. No
  notification destination is configured, so the delivery worker still has nothing verifiable to
  deliver to.
- **F06.5 live answer evaluation.** Still blocked on thresholds nobody has signed off and on an
  enriched live datasource.
