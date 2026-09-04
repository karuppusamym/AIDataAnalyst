# ADR-0025 — Auto-Approve Escape Hatch for AI-Drafted Metadata

**Status:** Proposed | **Date:** 2026-09-04 | **Owner:** Architecture + Data Governance

## Context

Atlas has three AI-drafted artifact loops today, each fully governed:

| Artifact | Draft producer | Approval gate |
|---|---|---|
| Business annotation | `semantic_inference.model_enrich_batch` (LLM_ASSISTED or RULES) → `MetadataEnrichmentProposal` | `GovernanceReview` decide + maker≠checker (`semantic_api.decide_governance_review`) |
| Asset description | `asset_description_service.compose_draft_text` (deterministic evidence-scored) → `AssetDescriptionDraft` | `GovernanceReview` decide + maker≠checker (`semantic_api._apply_governance_review_decision` `ASSET_DESCRIPTION_DRAFT` branch) |
| Glossary link | `stewardship_api.generate_glossary_link_proposals` (label-match against annotations) → `GlossaryLinkProposal` | `GovernanceReview` decide + maker≠checker (`stewardship_service.apply_link_proposal`) |

Human approval is the correct default posture for a bank workload. Every proposal reaches `APPROVED` only through a person distinct from the drafter, and the write path is single-source (`business_annotation_versions.write_annotation_version` is called from exactly one place, likewise `apply_asset_description_draft`). This is by design and does not change.

**But it does not scale linearly with catalog size.** At the ~5,000 tables per-workspace scope of the current bank pilots, a steward can drain the queue on a weekly cadence. At the 50,000–150,000-table scope of the full bank catalog, queue drain rate falls behind queue growth rate for the shapes of proposal that most reviewers just click through — a description draft with `overall_score > 0.85` and no lineage or PII involvement, a business-annotation proposal whose `business_name` matches the connector-sourced column name, a glossary link where a term's display name is a case-insensitive exact match against a column name. Operators running the larger deployments have asked whether the runtime can auto-approve those specific shapes if no human has touched them for a stated waiting period, so the queue does not silently grow into a permanent backlog that hides the proposals that actually need attention.

The narrow question this ADR answers: **is there a form of auto-approve that preserves ADR-0001's deterministic-choke-point discipline and ADR-0009's approval-is-not-activation posture, and lets a bank auditor answer "who authored this" as clearly as human-approved content does today?**

## Decision

**Adopt a per-organization, per-artifact-type, per-scope auto-approve policy — off by default; explicit opt-in per organization; audit-preserving; reversible.** The policy is a declarative row, not code, and is itself under governance.

### Policy shape

`AutoApprovePolicy` (new model in `src/aida/models.py`, migration required):

```
id                        UUID
organization_id           UUID   -- policy is per-org
artifact_type             STR    -- one of:
                                 --   METADATA_ENRICHMENT_PROPOSAL
                                 --   ASSET_DESCRIPTION_DRAFT
                                 --   GLOSSARY_LINK_PROPOSAL
enabled                   BOOL   -- default False
min_confidence            FLOAT  -- e.g. 0.90 for enrichment, 0.85 for description overall_score
min_days_waiting          INT    -- proposal must sit unreviewed at least this long
max_daily_auto_approvals  INT    -- hard cap per (org, artifact_type) per day
scope_include             JSONB  -- optional include filter (domain_ids, datasource_ids)
scope_exclude             JSONB  -- optional exclude filter (same shape)
category_exclude          JSONB  -- table-classifier tags that DISQUALIFY:
                                 --   PII_HIGH, REGULATED_FINANCIAL,
                                 --   CROSS_BORDER, SOX_RELEVANT
approved_by               STR    -- the human who approved the POLICY itself
approved_at               TS
revoked_at                TS?
policy_version            INT    -- append-only; supersede rather than mutate
```

The policy itself is a governed artifact: creating or changing one goes through `GovernanceReview(object_type="AUTO_APPROVE_POLICY")` with maker≠checker. This is deliberate — a bad policy is much higher-risk than a bad individual approval. Two humans must vouch for the shape before any proposal is auto-approved.

### The sweep

`src/aida/auto_approve_sweep.py` (new module, wired into `workflows/scheduler.py` alongside the reaper, hourly cadence, configurable):

```
for each active AutoApprovePolicy:
    for each candidate proposal matching (org, artifact_type,
                                          confidence >= min_confidence,
                                          waiting_days >= min_days_waiting,
                                          scope_include, NOT scope_exclude,
                                          NOT category_exclude):
        if daily_cap_remaining_for(org, artifact_type) <= 0: break
        with per-item SAVEPOINT:
            decide_governance_review(
                review_id=proposal.review_id,
                decision=APPROVED,
                actor=SystemPrincipal.AUTO_APPROVE,
                rationale=f"Auto-approved by policy {policy.id} v{policy.version}"
                          f" — confidence {conf:.3f} >= threshold {policy.min_confidence:.3f},"
                          f" waited {days} days.",
                policy_id=policy.id,
                policy_version=policy.version,
            )
            record_audit(action="AUTO_APPROVED_BY_POLICY", ...)
            emit outbox event: governance.review.auto_approved.v1
```

Auto-approve reuses `decide_governance_review` verbatim — same single write path, same supersede semantics, same version bumps. The only differences are the actor identity (`SystemPrincipal.AUTO_APPROVE`, a reserved principal id that never appears in the identity provider) and two new columns on `GovernanceReview` (`auto_approve_policy_id`, `auto_approve_policy_version`) so the audit trail names *which policy* did it and *at what version*. Maker≠checker is trivially satisfied — `SystemPrincipal.AUTO_APPROVE` is never the requester of a proposal.

### Reversibility

Auto-approved proposals are indistinguishable in storage from human-approved ones — same `APPROVED` version, same supersede history. So the existing "propose a new version, review, approve" loop is the natural rollback: a steward sees an auto-approved change they disagree with, drafts a new version, submits, another steward approves. Nothing special.

A **fast-revert** path is added on top: `POST /v1/governance/reviews/{id}/revert-auto-approval` reachable within a 7-day cooldown window (configurable, `AIDA_AUTO_APPROVE_REVERT_WINDOW_DAYS`). If invoked, the auto-approved version is superseded by the prior APPROVED version (or the connector-sourced value if there was none), an audit `AUTO_APPROVAL_REVERTED` is recorded, and the policy that fired gets a strike. Three strikes and the policy auto-disables (`enabled=False, revoked_at=now, revoked_reason="three_reverts_in_seven_days"`), requires a fresh governance review to re-enable.

### Global safety rails

- Default OFF at every level. Turning it on requires an `AutoApprovePolicy` INSERT, which itself goes through `GovernanceReview`.
- Hard cap: `max_daily_auto_approvals` per (org, artifact_type). If a bad policy would try to auto-approve 10,000 proposals in a day, only the first N happen; the rest stay in the queue for human review.
- Category exclusion is by-classifier, not by table id, so a newly-classified table becomes excluded on the next sweep automatically — no per-table maintenance.
- ALL auto-approvals emit `governance.review.auto_approved.v1` on the outbox. A downstream monitor can alert on rate, per-policy, per-org.
- The three-strike auto-disable rule is enforced by the sweep itself, not by an external monitor — no dependency on an alerting system being up.

### What auto-approve is NOT

- **Not per-request.** No request-time "if the model is very confident, skip review." That would change the write path in the request lifecycle, which ADR-0001 explicitly locks down.
- **Not for anything but the three listed artifact types.** Ownership, certification, lineage-relationship-candidate, and glossary-*term* creation stay strictly human-only. Each has a different failure mode; extending later requires its own ADR.
- **Not a queue-priority mechanism.** Existing per-proposal `overall_score` already sets queue order; that stays.

## Consequences

### Positive

- Steward review time refocuses on the proposals that actually need judgment. High-confidence auto-approvals stop crowding the queue.
- Governance is preserved end-to-end: the policy itself is a `GovernanceReview` object; the auto-approval is a `GovernanceReview` decision; the actor is a well-known system principal; the write path is the same single function; the storage is indistinguishable from human-approved. A bank auditor answering "who approved this and under what authority" gets a clean chain — the specific policy id + version — instead of "the LLM."
- Fully reversible with a fast path (7-day revert window) and an idempotent supersede-loop for anything older. Three-strike auto-disable prevents a bad policy from doing sustained damage.
- Opt-in, per-org, per-artifact-type. A bank department can turn it on for descriptions but not for annotations, or on for one datasource but not another.
- Extends the existing ADR-0001 deterministic-choke-point pattern rather than adding a new one — same `decide_governance_review` function, same supersede semantics, same maker≠checker guard.

### Negative — costs accepted

- New operational surface: someone has to write and maintain the `AutoApprovePolicy` rows. A wrong policy causes exactly the failure mode this proposal exists to prevent — quiet accumulation of bad data — just faster than the manual-review-only path did. Mitigated by the three-strike rule, the category exclusions, the daily cap, and the fact that the policy itself needs two approvers, but not eliminated.
- Category-exclusion classifiers (PII_HIGH, REGULATED_FINANCIAL, etc.) become load-bearing. If a table isn't correctly classified, auto-approve might touch content that a human never would have approved. The classifiers already exist for other reasons; auto-approve adds a new dependency on their correctness.
- Auditors will want a dashboard: "how many proposals were auto-approved by which policy, and how many were reverted." This ADR adds the event stream that makes that possible but does not build the dashboard. That is a follow-up.
- Reversal via governance-review superseding is loud (a new version row, an audit event, an outbox event). If a bank runs auto-approve at scale, and a bad policy needs to be reverted across thousands of proposals, the fast-revert path becomes a bulk operation. Bulk revert is not in this ADR.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Never auto-approve (status quo) | Doesn't scale to the 50k+ table catalog; steward backlog grows unbounded on high-confidence proposals that get rubber-stamped anyway. |
| Global auto-approve above a fixed confidence | Violates per-org governance. A bank has multiple LOBs with different risk postures; the SOX-relevant LOB may want auto-approve OFF entirely while the internal-analytics LOB wants it aggressive. |
| Auto-approve gated on a *second* LLM's judgment ("LLM-as-judge") | Adds a dependency without changing the fundamental risk. If the drafting LLM is confident, a judging LLM is likely to agree — correlated failures. And it hides accountability behind another opaque model. Human review is what accountability requires; auto-approve above a threshold is what scale requires; a second model in the middle answers neither. |
| Per-proposal auto-approve at draft time (skip the review row entirely) | Would collapse the write path — auto-approved proposals would go directly to APPROVED without a `GovernanceReview` audit row. Undermines the "who approved this" answer. The current design intentionally keeps the review row so it can name the policy. |
| Trust confidence alone; no waiting period | The waiting period gives a steward the chance to catch a systemic issue before it accumulates. Every batch of proposals waits at least `min_days_waiting` days; if a bad drafter release lands on Monday, stewards notice it on Tuesday, before Wednesday's sweep auto-approves it. |
| No hard daily cap | A runaway policy at scale = a runaway problem at scale. Hard cap turns a policy misconfiguration from a catastrophe into a bounded incident. |

## Revisit trigger

- **Revert rate above 2% of auto-approvals per policy** for two consecutive weeks: the policy is too aggressive — either raise `min_confidence`, lengthen `min_days_waiting`, or narrow the scope. If revert rate is above 5% policy-wide, the ADR itself needs revisiting.
- **Auto-disable triggered more than once per quarter across the org**: the three-strike rule is misfiring, or drafters are producing systematically-bad output; either way it's worth revisiting whether the threshold or the strike count is right.
- **Regulator guidance requires per-decision named human approver** for the affected artifact type: this ADR must be re-scoped to exclude that artifact type in that jurisdiction. `SystemPrincipal.AUTO_APPROVE` is not a person, and pretending otherwise would be worse than the original queue backlog.
- **Extension to a fourth artifact type requested**: a new ADR revisits the ownership / certification / lineage cases, each of which has different failure modes and cannot be added to this policy without independent analysis.

## Related

- `10-architecture/adr/ADR-0001-hybrid-deterministic-llm.md` — models propose, deterministic services decide. Auto-approve is a deterministic policy deciding.
- `10-architecture/adr/ADR-0009-route-approval-is-not-activation.md` — same discipline: policy approval is not policy activation; the policy row must exist AND be enabled AND have a candidate proposal AND cap remaining before any auto-approval happens.
- `10-architecture/adr/ADR-0023-deterministic-jobs-vs-generative-producers.md` — auto-approve sweep is a deterministic job over generative-producer output.
- `20-modules/09-review-queue.md` — the queue this drains.
- `60-delivery/12-session-2026-09-04-reaper.md` (RP-1) — the reaper is the peer job that removes stale REJECTED and PENDING proposals; auto-approve is the peer job that approves stale high-confidence ones.
- `60-delivery/03-tracker.md` — filed under the auto-approve escape hatch follow-up (row to be added).
