# Module 08 - Glossary and Stewardship

> Layer L2 | Schema `glossary` | Owner: Data Governance

## 1. Purpose

Owns the human side of meaning: governed business language, accountable ownership, certification, disagreement resolution, and measurable stewardship coverage. The implemented vertical slice uses immutable term versions and the common maker-checker review path; approved state, audit evidence, and outbox events are committed atomically.

The operating principle is: **stewards curate proposals and evidence; they do not silently overwrite history.**

## 2. Jobs served

S1, S2 (conflicts), S3 (bulk ownership), S5 (coverage), R1, R3, and B2 (is this the official metric).

## 3. Implemented responsibilities

- Organization-scoped categories and stable business-term identities.
- Immutable term versions with definitions, owners, synonyms, draft/review/approved/superseded/deprecated states, and reviewed deprecation.
- Manual, bulk, and inferred term-to-table links with provenance and confidence.
- Individual and group ownership assignments, reusable pattern rules, and reviewed bulk application.
- Explicit definition and synonym conflicts, including auto-detection, review, rationale, and retention of both positions.
- Reviewed table certification with rationale and expiry.
- Six-dimensional coverage scoring at organization, source, domain, and line-of-business scope.
- Durable coverage snapshots and history, plus a bounded unowned-table backlog.
- One maker-checker bulk contract for ownership, linking, certification, and deprecation.
- Deterministic, evidence-scored table description drafting, reviewed through the common governance queue.
- A responsive Stewardship Control Center integrated with the Business Meaning and asset-intelligence workbenches.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Semantic model versions and metrics | 07 semantic-layer |
| Common approval mechanics | 17 policy-governance |
| Metadata semantic inference | 07 semantic-layer |
| Retrieval and ranking | 12 retrieval |

## 5. Domain model

```text
glossary_category
glossary_term -> glossary_term_version
glossary_term -> asset_term_link
glossary_link_proposal -> governance_review
glossary_conflict -> governance_review
ownership_rule -> bulk_stewardship_operation -> governance_review
ownership_assignment
asset_certification
coverage_snapshot
asset_description_draft -> governance_review
asset_description_draft -> asset_documentation_version (on approval)
```

Term synonyms are stored on immutable versions. An authoritative link records `MANUAL`, `BULK`, or `INFERRED`; inferred links retain the approved business annotation that supplied their evidence.

## 6. Conflict resolution

Atlas never uses last-write-wins for disputed meaning.

| Step | Implemented behavior |
|---|---|
| Detect | A steward can raise a conflict directly or run bounded exact synonym-collision detection |
| Surface | Both positions, source references, conflict type, status, and timestamps remain durable |
| Propose | The maker records a resolution, optional proposed definition, and rationale |
| Review | An independent checker approves or rejects through the common governance queue |
| Resolve | Approval records checker and timestamp; rejection reopens the conflict without deleting either position |

Future precedence learning may use resolution history, but the current implementation does not silently rewrite a term definition from a conflict decision.

## 7. Bulk operations

| Operation | Implemented form |
|---|---|
| Assign ownership | Explicit tables or tables selected by a reusable rule (table name, schema, business domain, or annotation tag glob); individual or group owner |
| Link terms | Explicit table selection linked to one approved active term |
| Certify | Explicit table selection with shared rationale and expiry |
| Deprecate | Explicit term selection with shared rationale |

All bulk operations are capped at 500 subjects and require independent review. Rule scans are capped at 10,000 tables and 500 matches. A decision applies the operation atomically; partial success is not treated as approval.

## 8. Coverage scoring

| Dimension | Definition |
|---|---|
| Documented | Latest asset documentation version is approved |
| Owned | At least one active ownership assignment exists |
| Classified | At least one table column has a non-public classification |
| Certified | An active, unexpired asset certification exists |
| Quality-monitored | An enabled source or table quality policy applies |
| Semantically mapped | An approved business annotation or authoritative glossary link exists |

The API computes a simple percentage for each dimension and their arithmetic mean. It supports organization-wide, data-source, domain, and line-of-business scopes, validates every scope against the tenant, can persist time-stamped snapshots, and returns up to 500 unowned table IDs for action. Field-completion percentage is deliberately excluded because it measures typing rather than trust.

## 9. Public interface

The `/v1/organizations/{organization_id}` API includes:

- `/glossary/categories`, `/glossary/terms`, term versions, reviewed deprecation, and asset links.
- `/ownership-rules`, rule application, and `/ownership-assignments`.
- `/stewardship/bulk-operations` for reviewed assign/link/certify/deprecate changes.
- `/glossary/conflicts`, conflict detection, and reviewed resolution.
- `/glossary/link-proposals` for bounded exact-label inference and review.
- `/stewardship/coverage`, snapshots, and snapshot history.
- `/asset-description-drafts/generate`, the confidence-ordered `/asset-description-drafts` list, and `/asset-description-drafts/{id}/submit` for evidence-scored description drafting.

The common `/v1/governance-reviews/{review_id}/decision` endpoint applies or rejects every governed change.

## 10. Events

Implemented event types are cataloged in `30-contracts/04-event-catalog.md`. They cover reviewed bulk requests and decisions, ownership assignment, bulk linking, term deprecation, certification, conflict creation/resolution, inferred-link decisions, coverage snapshots, and description-draft submission/approval/rejection.

## 11. Safety boundaries

- Inference reads approved metadata annotations only; source rows never enter the workflow.
- Inference is deterministic exact matching over normalized approved term labels and synonyms. It is not fuzzy or model-generated matching.
- Generation is bounded to 5,000 active terms, 10,000 approved annotations, and 500 proposals per request.
- Conflict auto-detection is bounded to 5,000 active terms and 100 conflicts per request.
- Coverage and operation subjects are organization checked; cross-tenant IDs fail closed.
- Makers cannot approve their own proposed changes.
- Description drafting reads schema, lineage, dbt, annotation, and glossary-link metadata only — no source row values, and no external model or LLM call. A draft below the minimum evidence score can never be submitted for review, and every submitted draft (regardless of score) still requires an independent `decide_governance_review` approval before its text is published.

## 12. Current state and remaining work

| Aspect | Current state | Remaining work |
|---|---|---|
| Term lifecycle | Implemented vertical slice | Category edit/archive; scheduled lifecycle policy |
| Term-asset linkage | Manual, reviewed bulk, and reviewed exact inferred links | Fuzzy/model-assisted ranking and bank corpus calibration |
| Ownership | Individual/group, manual/rule (name, schema, domain, tag), reviewed bulk | Inheritance and dedicated leaver/vacate workflow |
| Conflicts | Manual and synonym detection with reviewed retained resolution | Definition-source precedence learning and richer impact preview |
| Certification | Reviewed bulk table certification with expiry | Automatic expiry state/event worker; additional asset types |
| Coverage | Six dimensions, four scopes, snapshots/history, unowned IDs | Scheduled trend computation, routing/escalation, bank-scale benchmarks |
| Description drafting | Deterministic evidence-scored drafts, minimum-evidence submission gate, reviewed publish/reject with retained negative knowledge | Column/table-type-specific templates, batch scan trigger, bank corpus calibration of the scoring weights |
| User experience | Responsive Stewardship Control Center and asset accountability actions | Interactive WCAG/usability certification and very-large-selection patterns |

## 13. Open work

| ID | Item | Status | Priority |
|---|---|---|---|
| GL-1 | Versioned term lifecycle, categories, synonyms, and reviewed deprecation | DONE | P0 |
| GL-2 | Individual/group ownership with rule-based and bulk assignment | DONE | P0 |
| GL-3 | Conflict detection and reviewed retained resolution | DONE | P0 |
| GL-4 | Scoped coverage scoring, dashboard, and history | DONE | P0 |
| GL-5 | Reviewed bulk table certification with expiry | DONE | P1 |
| GL-6 | Unowned-asset backlog with routing | DONE - bounded backlog, automated owner routing, and two-tier escalation | P1 |
| GL-7 | Dedicated leaver reassignment and ownership vacate workflow | TODO | P2 |
| GL-8 | Review-confirmed term-link inference from approved annotations | DONE | P1 |
| GL-9 | Evidence-scored table description drafting, routed through review | DONE | P1 |

Atlan's "Context Agents" auto-draft table/column descriptions and auto-*apply* high-confidence output with no human review. GL-9 takes the underlying idea — draft from real evidence, score the draft — and rejects the no-review auto-apply, which is wrong for a governed/bank platform: `AssetDescriptionDraft` composes a description deterministically from evidence already in the catalog (column and constraint counts, `OpenLineageTableEdge` lineage, a matched `DbtResource` description, an approved `MetadataBusinessAnnotation`, and bound `AssetTermLink` glossary terms — no external model call), scores it on four explainable dimensions (accuracy, clarity, style, completeness), and routes it through the same `governance_review` queue as every other governed object type. The score sets review priority — `GET .../asset-description-drafts` sorts by `overall_score` descending — and gates whether a draft may even be submitted (`MINIMUM_EVIDENCE_FOR_REVIEW`); it never skips or substitutes for `decide_governance_review`, and self-approval is denied by that endpoint's shared maker-checker guard exactly as for every other object type. Approval publishes the drafted text as a new `AssetDocumentationVersion` (superseding the prior approved version); rejection retains the draft as `REJECTED` — negative knowledge, per §6 — so an identical low-value draft is not silently regenerated on the next run.
