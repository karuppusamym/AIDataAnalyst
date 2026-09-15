# Reviewer agent oversight and correction runbook

> Status: Authoritative for AR-11, written 2026-09-11. Applies to the ADR-0027 reviewer agent.
> Audience: reviewers, reviewer leads, object owners and the model risk manager.
> Related: `Docs/10-architecture/15-agent-architecture-critical-review.md` (AR-03, AR-11), `tests/test_ar11_oversight.py`.

When the reviewer agent is enabled, it decides T0 and T1 governance reviews without a person. ADR-0027 accepts that on one condition: a deterministic sample of its approvals goes to humans, and humans read it.

The platform measures and enforces part of that condition. The rest depends on people doing the steps below, and this runbook is where those steps are written down.

## 1. The numbers, and where they are

| Signal | Where it shows | Who acts |
|---|---|---|
| Unread sample and its bound | Reviewer agent screen, *Agent state*: "Unread audit sample N of M". API: `GET /v1/organizations/{org}/reviewer-agent` returns `unresolved_samples` and `max_unresolved_samples`. | Reviewers |
| Agent stopped by the backlog | Pill "stopped: audit sample unread". A run returns 409 `reviewer_agent_audit_backlog_exceeded`. Audit `reviewer_agent.run`, outcome `DENIED`. Outbox `reviewer_agent.audit_backlog_exceeded.v1`. Notification `REVIEWER_AGENT_AUDIT_BACKLOG`. | Reviewer lead |
| A disputed decision | Sample resolved `DISAGREED`. Audit `reviewer_agent.sample.resolve`. Outbox `reviewer_agent.sample_resolved.v1`. Notification `REVIEWER_AGENT_SAMPLE_DISAGREED`, which carries the review id. | Owner of the decided object |
| Disagreement by object type | *Disagreement rate* panel, and `GET .../reviewer-agent/disagreement-rates`. ADR-0027's revisit trigger is more than 5% on one type over 30 days, with at least 20 resolved samples. | Model risk manager |
| Disagreement by risk tier | Same panel, *By risk tier*. Only approvals are sampled, so each tier's rate is its sampled false-approval rate. | Model risk manager |
| Time to a verdict | Same panel, *Time to a human verdict*: median, 90th percentile, and the age of the oldest unread sample. | Reviewer lead |

### Defaults

These live in `atlas/platform/config.py`:

- **Sampling rate:** 5%. This is also the floor, and configuration cannot go below it.
- **Backlog bound:** 50 unread samples (`reviewer_agent_max_unresolved_samples`). 0 disables the check, which is a decision to stop enforcing condition (b).
- **Approval threshold:** 0.8.
- **Evidence age limit:** 60 minutes.

Notifications are off unless `governance_notifications_enabled` is set. Without them, every signal above is on the screen only.

## 2. Routine: reading the sample

1. Once each working day, open *Reviewer agent*, then *Sampled decisions*, filtered to `PENDING`.
2. For each sample, choose **Open the review**. Check the decided object against its own evidence, not against the agent's score.
   - `tests/test_ar03_false_approval_benchmark.py` shows that the score does not separate a true proposal from a false one. Treat it as a measure of how much evidence existed.
3. Record **Agree** or **Disagree** with a rationale.
   - A Disagree rationale should name what is wrong: "the dbt description is for the orders table", not "incorrect".
   - The agent that made a decision cannot resolve its sample; the API refuses it with 409.

The platform reports how long this takes but sets no target. As a working target, keep the 90th percentile under two working days, and keep the oldest unread sample younger than a week.

## 3. When the agent stops for the backlog

There are two bounds, and the refusal code says which one tripped.

| Reason code | What it means | What clears it |
|---|---|---|
| `reviewer_agent_audit_backlog_exceeded` | Unread samples reached `reviewer_agent_max_unresolved_samples` (default 50). Samples are arriving faster than they are read. | Resolve pending samples until the count is below the bound. |
| `reviewer_agent_sample_age_exceeded` | The *oldest* unread sample has waited `reviewer_agent_max_sample_age_hours` (default 168, i.e. seven days). One item has been skipped, whatever the queue depth. | Resolve that sample. The state endpoint reports `oldest_pending_sample_hours`. |

Both raise the same `REVIEWER_AGENT_AUDIT_BACKLOG` notification, because the operator's action is the same either way: go and read the sample. They emit different outbox events (`reviewer_agent.audit_backlog_exceeded.v1`, `reviewer_agent.sample_age_exceeded.v1`) so a dashboard can tell "we need more reviewers" from "one item is being skipped".

1. The notification arrives, or the pill shows on the screen.
2. Clear the bound named by the reason code. The agent picks up again on its next run, and there is nothing to re-enable.
3. **Do not raise either bound to restart the agent.** They are the enforceable part of condition (b), so raising one is a configuration change that needs its own review. If the sample volume is genuinely too high, the fix is fewer agent decisions: narrow `reviewer_agent_max_tier` to T0, or suspend the agent.

## 4. When a human disagrees with a decision

Resolving a sample as `DISAGREED` records the verdict and notifies. **By itself it does not undo the decision** — the approval already applied the object's side effects through the same decision path a human approval uses, and disagreeing with a decision and undoing what it did are two judgements. A reviewer who thinks the agent was wrong may still want the change to stand while a person authors a better one.

For every object type the agent can approve, the resolve call can raise the undo at the same time: pass `reverse_applied_changes: true` alongside the `DISAGREED` verdict. That files the type's own correction — a **reversal** or a **withdrawal**, per the table below — carrying `review_audit_sample_id`, so the correction is reachable from the sample and the sampled decision is reachable from the correction. A person decides it, never the agent whose decision is in dispute: a withdrawal is T2 by type, and a reversal is pinned to T2 by `reverses_operation_id` (bulk stewardship) or `reverses_batch_id` (a workbook import). While it waits, the disputed sample still counts as unresolved (§3).

A bulk stewardship reversal acts on exactly the subjects the original operation *changed* (`applied_subject_ids`), never the subjects it was *asked* to change. A term link that existed before the operation ran is not removed by undoing it.

A steward can still correct any of these through the type's own path without a sample, the same route a human reviewer's mistake would take — but that correction carries no link to the sample.

| Object type (tier) | What the agent's approval did | How to correct it |
|---|---|---|
| `ASSET_DESCRIPTION_DRAFT` (T0) | Published an asset documentation version | Resolve `DISAGREED` with `reverse_applied_changes: true`. Files a withdrawal of exactly the version the draft published (`DESCRIPTION_WITHDRAWAL`, subject `TABLE`, T2) for a person to decide; approval moves it to `WITHDRAWN`, keeps its text, and the table reads as undescribed. Refused if someone has published a newer description since -- that one is theirs, so publish a corrected description instead. |
| `COLUMN_DESCRIPTION_DRAFT` (T0) | Published a column description version | Same, with subject `COLUMN`. |
| `MODEL_IMPORT_BATCH` (T1, 10 changes or fewer) | Published the workbook's descriptions and annotation fields | Resolve `DISAGREED` with `reverse_applied_changes: true`. Files a reversal batch (`REVERSE_WORKBOOK_EDITS`) that puts back what each applied change replaced, read from the version it replaced: a description or annotation field it overwrote is published again as a new version, and a description it added where there was none is withdrawn. A field someone has changed since is skipped as stale, not overwritten. An import applied before 2026-09-14 did not record the versions it published and is refused (409); correct it by re-importing. |
| `METADATA_ENRICHMENT_PROPOSAL` (T0, rules engine only) | Wrote a business annotation | Resolve `DISAGREED` with `reverse_applied_changes: true`. Files a withdrawal of the annotation version the agent approved (`DESCRIPTION_WITHDRAWAL` with subject `ANNOTATION`, T2) for a person to decide; approval moves the version to `WITHDRAWN` and keeps its content. Refused if someone has approved a newer version since -- that version is theirs, so correct it with a new proposal. |
| `BULK_STEWARDSHIP_OPERATION` — `LINK_TERM` (T1, 10 subjects or fewer) | Created asset/term links | Resolve `DISAGREED` with `reverse_applied_changes: true`. Files an `UNLINK_TERM` reversal over exactly the links this operation created, for a person to decide. |
| `BULK_STEWARDSHIP_OPERATION` — `CERTIFY_ASSET` (T1) | Granted table certifications | Same: files a `WITHDRAW_CERTIFICATION` reversal. The certifications move to `WITHDRAWN` and the asset reads as **uncertified** — deliberately not `REVOKED`, which is a standing refusal that would block use of the asset. What the certify superseded is not resurrected; re-grant it if it was right. |
| `BULK_STEWARDSHIP_OPERATION` — `TAG`, `CLASSIFY`, `ASSIGN_OWNERSHIP`, `DEPRECATE_TERM`, `REASSIGN_LEAVER` (T1) | Overwrote a tag value, a classification, or an ownership/lifecycle state | Same: files a `RESTORE_TAG`, `RESTORE_CLASSIFICATION`, `WITHDRAW_OWNERSHIP`, `RESTORE_TERM` or `RESTORE_LEAVER_OWNERSHIP` reversal. Each puts back what the operation overwrote, from the before-image it recorded when it applied: a tag it created is removed, an assignment it created is withdrawn, a term it deprecated is approved again with exactly the versions it deprecated, and a reassigned leaver is the owner again. A subject someone has changed since is skipped, not overwritten. An operation applied before its type recorded before-images is refused (409); correct it with a fresh operation. |

Since AR-03 (2026-09-11), the agent abstains on `GLOSSARY_LINK_PROPOSAL`, `DOCUMENT_CLAIM` and model-inferred enrichment. `QUERY_HISTORY_METRIC_CANDIDATE` is T2, outside its reach, so none of these can be agent decisions.

Steps:

1. The reviewer records the disagreement with its rationale.
2. The notification reaches the channel the object's owner watches. It is value-free: it carries the object type, the review id and the tier, never the text.
3. The owner corrects the object through the path in the table. Recommended: the same working day for T1, and within three working days for T0.
4. If a type's disagreements repeat, go to §5. A pattern is not a string of individual mistakes to correct one by one.

## 5. When a rate breaches

The metric reports and never acts; a person acts.

- **One object type over the revisit trigger:** the model risk manager suspends the agent for the organization (*Suspend*, with a reason) and reopens ADR-0027's decision for that type. Suspension takes effect immediately and applies to a run already in progress (AR-04).
- **One risk tier's false-approval rate rising, with no single type breaching:** treat it as a signal about the tier ceiling. Narrowing the ceiling to T0 is the proportionate response.
- **Resuming** is also one human action with a reason, and it is audited.

## 6. What this runbook cannot do yet

These are recorded here so that nobody assumes they exist:

- **Downstream harm is not measured.** Nothing links a disputed decision to the answers, tools or context products that consumed its output in the meantime. A reversal undoes the catalog change; it does not find or re-issue an answer that cited the wrong term link while it stood.
- **An overwriting bulk operation is reversible only if it recorded a before-image.** Every bulk operation type now has a compensating action. `LINK_TERM` and `CERTIFY_ASSET` add rows, so undoing them needs only the rows they added; the five that overwrite a value (`TAG`, `CLASSIFY`, `ASSIGN_OWNERSHIP`, `DEPRECATE_TERM`, `REASSIGN_LEAVER`) are undone from a record of what they replaced, taken when they applied. One applied before that record existed is refused rather than restored from a guess. A reversal also restores only what is still as the operation left it, so a later human change survives it -- and so does anything else that moved on in between, such as term links the reaper removed after a deprecation.
- **Only operations applied since 2026-09-12 can be reversed at all.** `applied_subject_ids` was added then and was deliberately not backfilled: for an older row the empty list means "not recorded", not "changed nothing", and guessing from `subject_ids` would let a reversal remove links and certifications that predated the operation. Those are refused.
- **A withdrawn business annotation is not reinstated.** Withdrawing the version an agent approved leaves the table with no approved annotation; the better annotation is a new proposal, not a revival of the withdrawn one.
- **Only a correction raised from the sample carries the link to it.** Since 2026-09-14 every type the agent can approve has a correction reachable from the sample. A steward who corrects a disputed decision directly instead — publishing a better description, or withdrawing one through the ordinary endpoint — files a correction with no reference to the sample, so the sample does not stay unresolved while it waits, and "was this disagreement acted on?" is answered by reading the object's history.
- **Only workbook imports applied since 2026-09-14 can be reversed.** `published_version` was added then and deliberately not backfilled, for the reason `applied_subject_ids` was not: without it a reversal cannot tell the import's version from a later one. An older import is refused; correct it by re-importing. A reversal also brings back nothing older than what the import replaced: an import that described an undescribed column is undone by leaving the column undescribed.
- **Only the oldest-sample age is enforced, not the distribution.** §3's age bound stops the agent when one sample has waited too long. The median, 90th-percentile and slowest time-to-verdict in the report are still reported and not alarmed on — a team resolving every sample on day six, forever, breaches nothing.
- **Nothing *notifies* anyone that a reversal is waiting**, beyond the ordinary review queue. What changed on 2026-09-13 is that an undecided reversal no longer hides: a sample resolved as `DISAGREED` whose reversal is still awaiting a decision counts as unresolved, so both bounds in §3 apply to it -- the count, and the oldest age, measured from when the reversal was filed. Before that, the original change could stand indefinitely behind a sample that read as resolved.
