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
| WORM archive | Audit records are mutable at the storage layer | P0 |
| SIEM routing | Security events do not reach the SOC | P0 |
| Retention enforcement | Policy exists; enforcement does not | P0 |
| Compliance pack generation | Evidence must be assembled by hand | P1 |
| Access review reporting | No self-service entitlement report | P1 |
| Policy decision logging | Partial — auditors need complete inputs | P0 |
| Privileged-access monitoring | Operators are audited but not monitored | P1 |
| Legal hold | No mechanism to suspend retention for a matter | P1 |
| Drill evidence retention | Drills are not run, so no evidence exists | P0 |

## 7. What a buyer's due diligence will find

An honest self-assessment, because a surprise in due diligence is worse than a known gap.

**Strong:** architectural trust boundaries, fail-closed design, value-freedom, maker-checker as a platform primitive, attributable audit, deterministic execution control.

**Weak:** no certifications, no penetration test, no WORM, no SIEM integration, no DR drill evidence, no performance benchmarks, no accessibility audit.

**The pattern.** Atlas's *design* is ahead of the market on trust; its *operational evidence* is behind. Closing that gap is Phase D of the roadmap, and it is a product feature, not a QA afterthought.

## Related documents

- Security architecture: `50-security/01-security-architecture.md`
- Observability and audit: `20-modules/20-observability-and-audit.md`
- Roadmap: `60-delivery/01-roadmap.md`
