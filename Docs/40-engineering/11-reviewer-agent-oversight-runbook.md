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

For a sampled `BULK_STEWARDSHIP_OPERATION`, the resolve call can raise the undo at the same time: pass `reverse_applied_changes: true` alongside the `DISAGREED` verdict. That files a **reversal** — an ordinary bulk operation that undoes the original, carrying `reverses_operation_id` and `review_audit_sample_id`, so the correction is reachable from the sample and the sampled decision is reachable from the correction. It is `REVIEW_REQUIRED` like any other bulk operation and is pinned to T2 by `reverses_operation_id`, so a person decides it — never the agent whose decision is in dispute.

A reversal acts on exactly the subjects the original operation *changed* (`applied_subject_ids`), never the subjects it was *asked* to change. A term link that existed before the operation ran is not removed by undoing it.

For every other object type the correction still goes through that type's own path, the same route a human reviewer's mistake would take.

| Object type (tier) | What the agent's approval did | How to correct it |
|---|---|---|
| `ASSET_DESCRIPTION_DRAFT` (T0) | Published an asset documentation version | Publish a corrected description, which supersedes the wrong one. If there is no right text yet, file a description withdrawal: `DESCRIPTION_WITHDRAWAL`, T2, decided by a person. |
| `COLUMN_DESCRIPTION_DRAFT` (T0) | Published a column description version | Same as above: a corrected draft or a withdrawal. |
| `MODEL_IMPORT_BATCH` (T1, 10 changes or fewer) | Published the workbook's descriptions | Re-export, correct and re-import. The `*_version` columns stop a silent overwrite. Alternatively, withdraw the individual descriptions. |
| `METADATA_ENRICHMENT_PROPOSAL` (T0, rules engine only) | Wrote a business annotation | Propose a corrected annotation. **There is no withdrawal path for an annotation** (see §6). |
| `BULK_STEWARDSHIP_OPERATION` — `LINK_TERM` (T1, 10 subjects or fewer) | Created asset/term links | Resolve `DISAGREED` with `reverse_applied_changes: true`. Files an `UNLINK_TERM` reversal over exactly the links this operation created, for a person to decide. |
| `BULK_STEWARDSHIP_OPERATION` — `CERTIFY_ASSET` (T1) | Granted table certifications | Same: files a `WITHDRAW_CERTIFICATION` reversal. The certifications move to `WITHDRAWN` and the asset reads as **uncertified** — deliberately not `REVOKED`, which is a standing refusal that would block use of the asset. What the certify superseded is not resurrected; re-grant it if it was right. |
| `BULK_STEWARDSHIP_OPERATION` — `TAG`, `CLASSIFY`, `ASSIGN_OWNERSHIP`, `DEPRECATE_TERM`, `REASSIGN_LEAVER` (T1) | Overwrote a tag value, a classification, or an ownership/lifecycle state | **No reversal**; the request is refused with `no compensating action`. These overwrite a previous value nothing recorded, so an automatic undo would delete rather than restore it. Correct by submitting a fresh operation setting the right value (see §6). |

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
- **Only the two additive bulk operations can be reversed.** `LINK_TERM` and `CERTIFY_ASSET` add a row that did not exist, so undoing them needs only the list of rows they added. `TAG`, `CLASSIFY`, `ASSIGN_OWNERSHIP`, `DEPRECATE_TERM` and `REASSIGN_LEAVER` overwrite a previous value that nothing captures a before-image of; they are refused by name rather than half-undone. Capturing before-images is the work that would close this.
- **Only operations applied since 2026-09-12 can be reversed at all.** `applied_subject_ids` was added then and was deliberately not backfilled: for an older row the empty list means "not recorded", not "changed nothing", and guessing from `subject_ids` would let a reversal remove links and certifications that predated the operation. Those are refused.
- **Business annotations from enrichment still have no withdrawal path.** A wrong `METADATA_ENRICHMENT_PROPOSAL` is corrected by proposing a better annotation; requesting a reversal for one is refused by name.
- **The sample-to-correction link exists only for bulk stewardship.** A description corrected through `DESCRIPTION_WITHDRAWAL` or a re-published draft still carries no reference to the sample that prompted it, so for those types "was this disagreement acted on?" is still answered by reading the object's history.
- **Only the oldest-sample age is enforced, not the distribution.** §3's age bound stops the agent when one sample has waited too long. The median, 90th-percentile and slowest time-to-verdict in the report are still reported and not alarmed on — a team resolving every sample on day six, forever, breaches nothing.
- **Nothing checks that a filed reversal is ever decided.** A reversal sits in the review queue like any other item; if nobody decides it, the original change stands and the sample still reads as resolved.
