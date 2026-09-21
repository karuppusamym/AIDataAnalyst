# Compliance and Evidence

> Status: Authoritative. Owner: Compliance + Platform.
> How Atlas produces the evidence a regulated buyer's audit, risk, and compliance functions require — and where that evidence does not yet exist.

## 1. The differentiating idea

Most governance platforms let you *record* that a control exists. Atlas can *generate the evidence that it operated*, because the platform is in the execution path.

| Question an auditor asks | Elsewhere | In Atlas |
|---|---|---|
| "Show me every action on this asset in Q3" | Query several systems' logs | One attributable ledger, searchable, exportable |
| "Prove the model could not run unapproved SQL" | A policy document and a log review | Architectural invariant + per-run replayable evidence |
| "Show approval chains for this metric" | Workflow tool history | Version-pinned maker-checker history with rationale |
| "Which data feeds this regulatory report?" | Manual lineage tracing | Lineage graph across query, dbt, ETL, BI |
| "Who can see this sensitive column?" | Access-review spreadsheet | Policy simulation (planned) |
| "Was this control operating all quarter?" | Attestation | Runtime evidence, continuously recorded |

That is whitespace **W5**: compliance packs **generated from runtime evidence** rather than authored.

## 2. The audit ledger

| Property | Requirement |
|---|---|
| Atomicity | Written in the **same transaction** as the mutation (INV-7) |
| Attribution | Actor identity and kind, tenancy, correlation ID, timestamp |
| Immutability | Append-only; never updated, never deleted |
| Value-freedom | No source values, no credentials, no raw question text |
| Completeness | Every governed mutation — enforced at the unit-of-work commit path |
| Retention | 7 years hot + WORM archive |
| Export | SIEM routing and auditor-facing export |

The atomicity requirement is what makes it a ledger rather than a log. A log can be missing an entry after a crash; a ledger written inside the mutation's transaction cannot.

## 3. Compliance packs

Reproducible bundles generated from runtime evidence for a named period, WORM-archived on generation.

| Pack | Contents | Audience |
|---|---|---|
| **Model risk** (SR 11-7 style) | Route inventory and versions, approval chains, evaluation results, refusal statistics, kill-switch drill evidence, non-content generation summary, activation posture history | Model Risk Management |
| **BCBS 239** | Lineage coverage by report, ownership coverage, quality posture and incident history, timeliness evidence, change control records | Risk data aggregation |
| **Access review** | Principal-to-entitlement mapping, delegation history, cross-tenant denial evidence, privileged-action log | Internal Audit |
| **AI usage** | Consumption by consumer, purpose, and tenant; denials with reason codes; budget consumption | Compliance |
| **Change control** | All approvals in period with maker, checker, rationale, and version deltas | Internal Audit |
| **Data protection** | Classification coverage, masking decisions, retention compliance, value-freedom test results | Privacy |

Each pack is reproducible: same period, same inputs, same output.

> **Implementation status (2026-09-20).** Five of the six packs are generated (`MODEL_RISK`, `BCBS_239`, `ACCESS_REVIEW`, `AI_USAGE`, `CHANGE_CONTROL`; `src/aida/compliance_packs.py`, served by `src/aida/compliance_api.py`); there is no Data protection pack. A pack is stored as a checksummed database row and is not handed to the WORM archive provider, so "WORM-archived on generation" is the target, not the behaviour. Generating or downloading one needs `PlatformAdmin`, `ComplianceOfficer` or `DataSteward`; `ComplianceOfficer` cannot be granted under OIDC and `Auditor` is refused (`00-product/02-personas-and-jobs.md` §2.6).

## 4. Regulatory mapping

Illustrative, not legal advice. Each control is mapped to the module that implements it.

| Requirement area | Atlas control | Module |
|---|---|---|
| Data lineage for risk reporting (BCBS 239) | Lineage across query, dbt, ETL, BI | 09 |
| Data quality and accuracy (BCBS 239) | Quality policies, incidents, SLAs | 11 |
| Timeliness (BCBS 239) | Freshness contracts, scan-age posture | 11 |
| Model inventory and approval (SR 11-7) | Model routes, versions, maker-checker | 15 |
| Model validation (SR 11-7) | Evaluation suites, refusal statistics | 13, 15 |
| Model change control (SR 11-7) | Immutable versions, activation posture | 15 |
| Access control (SOX / ISO 27001) | OIDC, RBAC/ABAC, tenancy | 01, 17 |
| Segregation of duties | Maker ≠ checker, platform-enforced | 17 |
| Audit trail | Append-only ledger, WORM | 20 |
| Data minimization (GDPR) | Value-free control plane | All |
| Purpose limitation (GDPR) | Purpose-bound authorization (planned) | 17 |
| Retention | Per-class retention policy | 20 |
| Right to know processing | Consumption lineage | 09, 19 |

> **Implementation status (2026-09-20).** The segregation-of-duties row holds for reviews, not yet for every approval. Review decisions share one maker != checker check (`check_decision_permitted` in `src/aida/governance_decision_service.py`), but the decision routes outside the review queue (candidate, parsed-lineage, tool-certification, source-binding and profiling-exception decisions among them) each carry their own principal-equality test, mostly answering 409 and, for freshness-config approval, 403. `test_self_approval_denied` (`tests/test_tier0_invariants.py`) parametrizes 12 of the 33 registered review adapters (as of 2026-09-20). Access policies and workspace memberships, both classed as the highest review risk tier, now open a review that a second principal decides (R11-AUD02, covered by tests and not yet exercised on the deployed stack), so a policy is created as a draft and a member as pending and neither takes effect on the proposer's say-so; see `20-modules/01-identity-and-tenancy.md` §10.

## 5. Certification status — the honest position

| Certification | Status | Note |
|---|---|---|
| SOC 2 Type II | **Not started** | Competitors have it; procurement asks for it |
| ISO 27001 | **Not started** | " |
| ISO 27701 | Not started | " |
| FedRAMP | Not applicable | Collibra is FedRAMP-ready for the public sector |
| Penetration test | **Not run** | P0 |
| Accessibility (WCAG AA) | **Not audited** | P1 |

For a self-hosted deployment inside a bank, the bank's own certification perimeter covers much of this — but a buyer will still ask, and "not started" is the current answer.

## 6. Evidence gaps

| Gap | Impact | Priority |
|---|---|---|
| WORM archive | Implemented (`src/aida/worm_archive.py`) but off by default (backend `none`) and verified only against a local Object Lock service, not AWS S3 or a bank store; until one is chosen and proven, audit records are mutable at the storage layer | P0 |
| SIEM routing | Implemented (webhook and syslog) but verified only against loopback stubs; the shipped endpoint is a placeholder and the delivery worker is off by default, so security events do not reach the SOC | P0 |
| Retention enforcement | Policy exists; enforcement does not | P0 |
| Compliance pack generation | Five of six packs are generated (`src/aida/compliance_packs.py`); no Data protection pack, and packs are not WORM-archived (see the status note in section 3) | P1 |
| Access review reporting | Implemented: a self-service entitlement report (`src/aida/access_review_api.py`); a report for another principal needs `PlatformAdmin`, `DataAdmin` or `ComplianceOfficer`, and the last cannot be granted under OIDC | P1 |
| Policy decision logging | Partial — auditors need complete inputs | P0 |
| Privileged-access monitoring | Operators are audited but not monitored | P1 |
| Legal hold | The archive can place and release a hold (`apply_legal_hold` and `release_legal_hold` in `src/aida/worm_archive.py`, exercised against a local Object Lock service); no API route or operator workflow calls them | P1 |
| Drill evidence retention | One in-process kill-switch drill exists (`tests/test_kill_switch_drill.py`); no drill has been run against a deployed stack, so no drill evidence is retained | P0 |

## 7. What a buyer's due diligence will find

An honest self-assessment, because a surprise in due diligence is worse than a known gap.

**Strong:** architectural trust boundaries, fail-closed design, value-freedom, maker-checker as a platform primitive, attributable audit, deterministic execution control.

**Weak:** no certifications, no penetration test, a WORM archive and SIEM delivery that are implemented but off by default and unproven against real destinations, no DR drill evidence, no performance benchmarks, no accessibility audit.

**The pattern.** Atlas's *design* is ahead of the market on trust; its *operational evidence* is behind. Closing that gap is Phase D of the roadmap, and it is a product feature, not a QA afterthought.

## Related documents

- Security architecture: `50-security/01-security-architecture.md`
- Observability and audit: `20-modules/20-observability-and-audit.md`
- Roadmap: `60-delivery/01-roadmap.md`
