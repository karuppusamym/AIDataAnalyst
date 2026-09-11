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

1. The notification arrives, or the pill shows on the screen.
2. Resolve pending samples until the count is below the bound. The agent picks up again on its next run, and there is nothing to re-enable.
3. **Do not raise the bound to restart the agent.** The bound is the enforceable part of condition (b), so raising it is a configuration change that needs its own review. If the sample volume is genuinely too high, the fix is fewer agent decisions: narrow `reviewer_agent_max_tier` to T0, or suspend the agent.

## 4. When a human disagrees with a decision

Resolving a sample as `DISAGREED` records the verdict and notifies. **It does not undo the decision.** The approval already applied the object's side effects through the same decision path a human approval uses. The correction goes through the object type's own path, the same route a human reviewer's mistake would take.

| Object type (tier) | What the agent's approval did | How to correct it |
|---|---|---|
| `ASSET_DESCRIPTION_DRAFT` (T0) | Published an asset documentation version | Publish a corrected description, which supersedes the wrong one. If there is no right text yet, file a description withdrawal: `DESCRIPTION_WITHDRAWAL`, T2, decided by a person. |
| `COLUMN_DESCRIPTION_DRAFT` (T0) | Published a column description version | Same as above: a corrected draft or a withdrawal. |
| `MODEL_IMPORT_BATCH` (T1, 10 changes or fewer) | Published the workbook's descriptions | Re-export, correct and re-import. The `*_version` columns stop a silent overwrite. Alternatively, withdraw the individual descriptions. |
| `METADATA_ENRICHMENT_PROPOSAL` (T0, rules engine only) | Wrote a business annotation | Propose a corrected annotation. **There is no withdrawal path for an annotation** (see §6). |
| `BULK_STEWARDSHIP_OPERATION` (T1, 10 subjects or fewer) | Applied ownership, term links or certifications | Submit a compensating operation where one exists, such as re-assigning ownership. **A term link or certification applied in bulk has no bulk reversal** (see §6). |

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

- **Downstream harm is not measured.** Nothing links a disputed decision to the answers, tools or context products that consumed its output in the meantime.
- **No automatic reversal, and some objects have no withdrawal at all:** business annotations from enrichment, and term links and certifications applied in bulk.
- **A correction is not linked back to the sample that prompted it.** Whether a disagreement was ever acted on is visible only by reading the object's history.
- **Targets are not enforced.** Only the backlog bound is. The time-to-verdict numbers are reported, not alarmed on.
