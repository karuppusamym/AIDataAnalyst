# Event Catalog

> Status: Authoritative **as a target naming scheme**. Owner: Architecture.
> The named set of domain events Atlas publishes. Adding an event means adding a row here.

> **Implementation status (2026-08-30). Most event names in §2 are not the names the code
> emits.** Verified by extracting every `event_type=` argument passed to `record_outbox` across
> `src/aida/` and comparing it to this catalog:
>
> * The platform emits **~55 event types, all suffixed `.v1`** — e.g.
>   `datasource.registered.v1`, `metadata.discovery.snapshot.v1`, `query.execution.completed.v1`,
>   `context.product_consumed.v1`, `relationship_candidate.approved.v1` /
>   `relationship_candidate.rejected.v1` (RL-4, 2026-08-30: split from a single
>   `relationship_candidate.decided.v1` because the graph projector already listened for
>   these two distinct names and they never matched — decided candidates were silently
>   never projected to Neo4j),
>   `governance.review_requested.v1`, `workspace.created.v1`.
> * **Most rows below match nothing in the code.** Spot-checked and absent:
>   `principal.created`, `tenant.created`, `ingestion.delivered`, `catalog.object.created`,
>   `catalog.object.changed`, `profile.completed`, `classification.assigned`, `key.inferred`,
>   `relationship.candidate_generated`, `relationship.approved`, `table_family.detected`,
>   `semantic.proposal_created`, `lineage.edge_created`, `quality.observation_recorded`,
>   `quality.sla_breached`, `agent.run_started`, `execution.requested`,
>   `model.route_version_created`, `model.kill_switch_engaged`, `policy.version_published`,
>   `audit.event_recorded`, `graph.rebuild.started`, `retrieval.index_lagging`.
> * **The Semantics-and-glossary section is the exception** and is broadly accurate: its `.v1`
>   rows were written against the code and match it.
> * **Topics are wrong.** All eight `atlas.*.v1` topic headings below are target. Everything
>   goes to the single topic `aida.platform.events.v1`, with the event type in a Kafka header
>   (`src/aida/projectors/outbox_publisher.py`).
> * **"publishing an uncatalogued event fails CI" is false.** There is no such check;
>   `.github/workflows/ci.yml` runs `ruff`, `mypy`, `lint-imports`, an Alembic head check and
>   `pytest`. The same applies to step 3 and the closing line of §3 below.
>
> **ST-14 update (2026-09-01): reconciled.** The two directions above are now joined. The
> "restate the catalog" resolution (U2) was chosen — consumers key on the emitted `.v1` names,
> so those are canonical — and every emitted `event_type=` is now documented, in the new
> **"Reconciled emitted events (ST-14)"** subsection at the end of §2. The remaining pre-`.v1`
> rows in §2 are retained as the historical intended vocabulary. `tests/test_event_catalog_gate.py`
> (TS-11) enforces the code→catalog direction going forward with an empty drift baseline. Treat
> §2's original rows as the intended vocabulary and the reconciled subsection as the current one.

## 1. Envelope

Every event carries the same envelope (see `10-architecture/07-event-and-messaging-model.md` §4). Payload shapes vary; the envelope never does.

**Payload rules, enforced at publish:**

- No source business values (INV-6).
- No credentials or secret material.
- Bounded size.
- Tenancy fields mandatory.

## 2. Catalog

### Identity and tenancy — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `principal.created` | Principal registered | principal_id, kind |
| `principal.role_changed` | Role mapping changed | principal_id, roles_before, roles_after |
| `tenant.created` | Any hierarchy level created | level, id, parent_id |
| `tenant.archived` | Level archived | level, id |
| `secret_reference.rotated` | Reference updated | reference_scheme, path_hash |
| `organization.integration_policy.updated.v1` | Organization integration policy updated | organization_id, transformation_metadata_integrations |
| `data_domain.created.v1` | Data domain created under a line of business | data_domain_id, line_of_business_id, parent_domain_id |
| `identity.principal.deleted.v1` | A principal was removed at the identity provider; consumed by the ownership leaver handler, which flips that principal's ACTIVE ownership rows to LAPSED and routes the ones it orphaned | principal_id, organization_id |
| `identity.principal.merged.v1` | Two identity-provider principals were merged; active ownership held by the source principal is redirected to the target | source_principal_id, target_principal_id, organization_id |

### Connectivity — topic `atlas.operational.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `datasource.registered` | Source registered | datasource_id, connector_type, network_zone |
| `datasource.connection_verified` | Connectivity test passed | datasource_id, dialect, version |
| `datasource.disabled` | Source disabled | datasource_id, reason |
| `datasource.updated.v1` | Datasource fields patched (status, concurrency, etc.) | datasource_id, status, max_concurrency |
| `scan_policy.updated.v1` | Datasource scan policy created or updated | scan_policy_id, datasource_id, enabled |
| `certification.completed` | Certification run finished | datasource_id, score, status, check_results |
| `connector_agent.registered` | Agent registered | agent_id, zone |
| `connector_agent.heartbeat_lost` | Agent stopped heartbeating | agent_id, last_seen |

### Ingestion — topic `atlas.operational.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `ingestion.delivered` | Envelope applied | job_id, snapshot_type, counts, fingerprint |
| `ingestion.rejected` | Validation failed | job_id, reason_code |
| `batch.created` | Manifest created | batch_id, expected_chunks |
| `batch.chunk_received` | Chunk accepted | batch_id, chunk_number, checksum |
| `batch.finalized` | Manifest sealed and submitted | batch_id, workflow_id |
| `batch.failed` | Batch failed | batch_id, reason_code, chunks_processed |
| `metadata.ingestion.batch.paused.v1` | Operator paused a running batch (IN-2) | batch_id, datasource_id, previous_status |
| `metadata.ingestion.batch.cancelled.v1` | Operator cancelled a non-terminal batch (IN-2) | batch_id, datasource_id, previous_status |
| `metadata.ingestion.batch.resumed.v1` | Operator resumed a paused batch; fresh run re-queued (IN-2) | batch_id, run_id, datasource_id |
| `metadata.ingestion.batch.replayed.v1` | Operator replayed a terminal batch; fresh run re-queued (IN-2) | batch_id, run_id, datasource_id |
| `fleet.admission_rejected` | Source at capacity | datasource_id, queue_depth |
| `fleet.backpressure_engaged` | Downstream saturation | scope, signal |

### Catalog — topic `atlas.catalog.v1` (key: `datasource_id`)

| Event | Trigger | Key payload |
|---|---|---|
| `catalog.object.created` | New object | object_id, object_type, qualified_name |
| `catalog.object.changed` | Fingerprint changed | object_id, fingerprint_before, fingerprint_after |
| `catalog.object.deprecated` | Soft-deleted | object_id, tombstone_id |
| `catalog.object.reactivated` | Reappeared | object_id |
| `catalog.drift.detected` | Run completed with drift | run_id, created, changed, deprecated |
| `catalog.asset.certified` | Certification granted | object_id, certifier, expires_at |
| `catalog.asset.certification_expired.v1` | DQ-3: a table's certification expired because it crossed `quality_certification_sustained_threshold` unresolved quality incidents (off by default -- `quality_certification_expiry_enabled`) | table_id, certification_id |
| `catalog.asset.certification_revoked.v1` | P2-08: a steward manually revoked an active certification. Maker != checker applies -- the principal who certified cannot revoke. Downstream readers (`asset_usage_decision`'s `REVOKED -> BLOCKED`) invalidate promptly | certification_id, table_id, column_id, revoked_by, reason |
| `catalog.asset.certification_expiry_warning.v1` | A certification inside the expiry-warning window, emitted once per cooldown so a steward can re-certify before it lapses | certification_id, table_id, expires_at, days_until |
| `rename_candidate.decided.v1` | CT-4: steward approved/rejected a proposed rename | candidate_id, status |
| `cross_source_resolution_candidate.decided.v1` | CT-6: steward approved/rejected a proposed cross-source match | candidate_id, status |
| `catalog.table.newly_created.v1` | ING-4 / P0-01: `persist_discovery_snapshot` observed a table this call actually created (as opposed to reactivated/updated); consumed by `aida.newly_created_table_drafter.run_newly_created_table_drafter_consumer` to auto-enqueue an asset-description draft and unblock a semantic-inference proposal without a steward manually POSTing each drafter endpoint | organization_id, datasource_id, table_id, analysis_run_id |
| `asset_description.draft.auto_enqueued.v1` | ING-4 / P0-01: `handle_newly_created_table` created an `AssetDescriptionDraft` for a table that had none. Downstream analytics / notification consumers pick this up as "a draft is now waiting for the steward" | asset_description_draft_id, table_id, datasource_id, overall_score |
| `business_semantics.inference.auto_enqueue_deferred.v1` | ING-4 / P0-01: `handle_newly_created_table` deferred kicking off semantic inference because no `AnalysisRun` has reached `COMPLETED` yet for the datasource (mirrors the HTTP 409 gate in `create_semantic_inference_run`). Advisory; a later profiling-completion pass picks the table back up | table_id, datasource_id, reason |

### Profiling — topic `atlas.catalog.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `profile.completed` | Table profiled | table_id, run_id, statistics_summary |
| `profile.failed` | Profiling failed | table_id, run_id, reason_code |
| `classification.assigned` | Column classified | column_id, classification, confidence, rule_version |
| `classification.derived.promoted.v1` | AT-11: a lineage-derived column classification was promoted to the asserted (policy-enforced) value after an independent review approval | derived_classification_id, column_id, classification, review_id |
| `classification.derived.promotion_rejected.v1` | AT-11: a reviewer rejected promoting a lineage-derived column classification to the asserted value | derived_classification_id, column_id, classification, review_id |
| `key.inferred` | Key inferred | table_id, columns, confidence |
| `composite_key_candidate.decided.v1` | Checker approved or rejected a composite-key candidate | candidate_id, status |
| `analysis_run.started` / `.completed` / `.cancelled` | Run lifecycle | run_id, scope, counts |
| `profiling_exception_policy.requested.v1` | PR-2: maker requested a value-bearing profiling exception for one classification | policy_id, datasource_id, classification |
| `profiling_exception_policy.decided.v1` | PR-2: checker approved or rejected the exception | policy_id, datasource_id, classification, status |
| `profiling_exception_policy.revoked.v1` | PR-2: an approved exception's authority was withdrawn | policy_id, datasource_id, classification |
| `profiling.value_artifact_purged.v1` | PR-2: a `ColumnValueProfileArtifact` was hard-deleted once its pinned retention expired | artifact_id, column_id, table_id, policy_id |
| `policy_native_sync.requested.v1` | QG-2: maker froze a preview into a durable, reviewable source-native row/column policy sync request | request_id, datasource_id, schema_name, table_name |
| `policy_native_sync.decided.v1` | QG-2: checker approved or rejected the request (maker != checker) | request_id, status |
| `policy_native_sync.applied.v1` | QG-2: an approved request's DDL was executed against the live source | request_id, datasource_id, statements_hash |

### Relationships — topic `atlas.catalog.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `relationship.candidate_generated` | Candidate scored | candidate_id, from, to, confidence |
| `relationship.approved` | Checker approved | candidate_id, checker, rationale_ref |
| `relationship.rejected` | Checker rejected | candidate_id, checker, rationale_ref |
| `table_family.detected` | Family identified | family_id, family_type, members |
| `table_family_candidate.decided.v1` | Checker approved or rejected a table-family candidate | candidate_id, status |
| `canonical_table.resolved` | Canonical chosen | entity_ref, table_id, evidence_ref |

### Semantics and glossary — topic `atlas.semantics.v1` (key: `organization_id`)

| Event | Trigger | Key payload |
|---|---|---|
| `semantic.inference_completed` | Inference run finished | run_id, proposal_count |
| `semantic.proposal_created` | Proposal queued | proposal_id, object_type |
| `business_semantics.tool_blueprint_promoted.v1` | Enrichment proposal promoted into a governed tool blueprint draft | proposal_id, governed_tool_version_id |
| `semantic.annotation_published` | Approved and published | annotation_id, table_id, version |
| `semantic.version_published` | Model version published | version_id, supersedes |
| `semantic_model.draft_created.v1` | New semantic model version drafted | semantic_model_version_id, project_id |
| `semantic_model.cloned.v1` | Semantic model version cloned from an existing one | semantic_model_version_id, based_on_version_id, project_id |
| `metric.published` / `metric.superseded` | Metric lifecycle | metric_id, version |
| `glossary.term_published` / `.term_deprecated` | Term lifecycle | term_id, version |
| `glossary.term.approved.v1` / `.rejected.v1` | Governed term-version decision | term_version_id, term_id, version, review_id |
| `asset.documentation.approved.v1` / `.rejected.v1` | Governed asset documentation decision | documentation_version_id, documentation_id, version, review_id |
| `glossary.conflict_raised.v1` | Manual or detected conflict persisted | conflict_id, conflict_type, source_refs |
| `semantic.metric_conflict_raised.v1` | AT-17: detected metric-formula collision persisted as a `GlossaryConflict` row (`term_id=null`, `conflict_type="METRIC_FORMULA_COLLISION"`) | conflict_id, conflict_type |
| `glossary.conflict_resolved.v1` / `.conflict_resolution_rejected.v1` | Governed conflict-resolution decision (also used for `semantic.metric_conflict_raised.v1` conflicts -- same `GlossaryConflict` row, same resolution path) | conflict_id, review_id, resolution_status |
| `glossary.link_proposal_approved.v1` / `.link_proposal_rejected.v1` | Governed inferred-link decision | proposal_id, table_id, term_id, review_id |
| `ownership.assigned.v1` | Approved bulk/rule ownership applied | operation_id, subject_type, applied_count |
| `glossary.term_linked_bulk.v1` | Approved bulk term links applied | operation_id, term_id, applied_count |
| `glossary.term_deprecated.v1` | Approved term deprecation applied | operation_id, applied_count |
| `certification.granted.v1` | Approved asset certification applied | operation_id, expires_at, applied_count |
| `ownership.leaver_reassigned.v1` | Approved leaver-reassignment bulk operation applied | operation_id, applied_count |
| `ownership.assignment.expiry_warning.v1` | An ownership assignment is inside its expiry-warning window, emitted once per cooldown so the owner can reaffirm before it lapses | assignment_id, notify_principal, expires_at, days_until |
| `ownership.assignment.lapsed.v1` | An ownership assignment passed its `expires_at` without being reaffirmed and was flipped to LAPSED; routed for reassignment when it was the subject's last owner | assignment_id, subject_type, subject_id, was_last_owner |
| `ownership.assignment.reaffirmed.v1` | An owner reaffirmed an assignment before it lapsed, extending `expires_at` by the configured ownership term | assignment_id, subject_type, subject_id, expires_at |
| `ownership.assignment.lapsed_leaver.v1` | An `identity.principal.deleted.v1` event flipped this principal's ACTIVE ownership to LAPSED. Distinct from `ownership.assignment.lapsed.v1`, which is time-based: this one is identity-driven and carries no grace period | assignment_id, principal_id, subject_type, subject_id |
| `ownership.assignment.merged.v1` | An `identity.principal.merged.v1` event redirected an active ownership assignment from the source principal to the target | assignment_id, source_principal_id, target_principal_id |
| `metadata.playbook.created.v1` | AT-1: a saved, scheduled bulk-metadata playbook was created | playbook_id, name, action |
| `document.uploaded.v1` | N8: a data-dictionary document was uploaded and parsed into sections | document_id, project_id, section_count |
| `document.mapped.v1` | N8: a document's sections were resolved against the live catalog | document_id, matched_count, unmatched_count |
| `document.claims_extracted.v1` | N8: reviewable description claims were created from a document's structural mappings | document_id, claim_count |
| `document.claim.approved.v1` / `.rejected.v1` | N8: governed decision on one extracted document claim | claim_id, document_section_id, subject_type, subject_id, review_id |
| `stewardship.bulk_operation_rejected.v1` | Checker rejected a bulk stewardship request | operation_id, review_id |
| `stewardship.coverage_computed.v1` | Coverage snapshot persisted | snapshot_id, scope, overall_score |
| `stewardship.unowned_asset_routed.v1` | Unowned-asset backlog entry routed to a candidate owner | table_id, candidate_owner |
| `stewardship.unowned_asset_escalated.v1` | Backlog entry escalated (no owner found/accepted) | table_id |
| `stewardship.unowned_asset_escalated_tier2.v1` | Backlog entry still unaddressed after tier-1 escalation; opened as an ITSM ticket unconditionally (GL-6) | table_id |
| `stewardship.unowned_asset_resolved.v1` | Backlog entry resolved (ownership since assigned) | table_id |
| `asset_description.approved.v1` / `.rejected.v1` | Governed description-draft decision | draft_id, table_id, overall_score, published_version_id, review_id |

### Lineage — topic `atlas.lineage.v1` (key: `datasource_id`)

| Event | Trigger | Key payload |
|---|---|---|
| `lineage.edge_created` | Edge persisted | edge_id, kind, from, to, confidence |
| `lineage.artifact_ingested` | dbt manifest / OpenLineage / DDL ingested | artifact_id, kind, counts |
| `lineage.impact_computed` | Impact computed | node_ref, affected_counts |
| `bi_artifact.imported.v1` | BI tool metadata (Tableau/Power BI/Looker) imported | artifact_import_id, connection_id, bi_tool, report_count, metric_count |
| `lineage.consumed.v1` | Unified lineage graph/impact read via a native MCP lineage tool | tool_slug, principal_id, channel |
| `asset_context.consumed.v1` | Composite `get_asset_context` MCP tool read (AT-13) | tool_slug, principal_id, channel |

### Quality — topic `atlas.quality.v1` (key: `datasource_id`)

| Event | Trigger | Key payload |
|---|---|---|
| `quality.observation_recorded` | Check evaluated | observation_id, table_id, check, value, baseline |
| `data_quality.analysis.evaluated.v1` | An analysis run's quality checks all evaluated | analysis_run_id, datasource_id, observations, healthy, warning, critical, no_baseline, incidents_opened, incidents_resolved |
| `data_quality.policy.changed.v1` | Data-quality policy created or updated for a datasource/scope | datasource_id, scope_key, enabled |
| `quality.incident_opened` | Threshold breached | incident_id, fingerprint, severity |
| `quality.incident_reopened` | Re-detected | incident_id |
| `quality.incident_acknowledged` / `.resolved` | Operator action | incident_id, actor, rationale_ref |
| `quality.incident_auto_recovered` | Signal normalized | incident_id |
| `quality.sla_breached` | SLA missed | sla_id, table_id |
| `data_quality.freshness_config.changed.v1` | Watermark freshness config created or updated for a table | datasource_id, table_id, watermark_column |
| `data_quality.freshness_config.approved.v1` | DQ-2: a maker-checker approval activated a table's freshness watermark config (moves it out of PENDING_APPROVAL) | datasource_id, table_id |
| `data_quality.rule_pack.created.v1` | DQ-4: a custom quality rule pack created | datasource_id, name |
| `data_quality.custom_rule_pack.evaluated.v1` | DQ-4: a rule pack's own-cadence sweep evaluated all its rules | rule_pack_id, datasource_id, rules_evaluated, skipped_no_data, incidents_opened, incidents_resolved |
| `data_quality.external_signal.ingested.v1` | DQ-8: a third-party detector (Monte Carlo, Anomalo, ...) quality signal was ingested and reconciled into the incident lifecycle | signal_id, datasource_id, table_id, detector_vendor, detector_native_id, severity, signal_status, incident_id, incident_opened, incident_resolved |
| `data_quality.incident.notification_routed.v1` | DQ-1: a newly-opened/reopened quality incident matched at least one notification rule and produced `NotificationEventRecord`(s) | incident_id, severity, events_routed, channels |
| `data_quality.incident.itsm_payload.v1` | DQ-1: an ITSM-channel notification match produced a formatted incident payload; also records the real webhook emitter's outcome | short_description, description, urgency, impact, correlation_id, webhook_status, webhook_error |
| `contract.violations_detected` | Runtime data contract evaluation found violations | contract_id, violation_count, enforcement_action |

### Runtime — topic `atlas.execution.v1` (key: `organization_id`)

| Event | Trigger | Key payload |
|---|---|---|
| `agent.run_started` / `.run_completed` / `.run_denied` | Run lifecycle | run_id, final_state, denial_reason_code |
| `agent.evaluation.completed.v1` | Agent evaluation suite run finished | evaluation_run_id, status, suite_version |
| `screening.blocked` | Prompt-risk denial | run_id, classifier_version, score, reason_codes |
| `agent.tool_selected` | Tool bound | run_id, tool_id, version |
| `agent.generation_requested` | Model invoked | run_id, route_key |
| `agent.feedback_recorded` | Feedback submitted | run_id, verdict |
| `tool.drafted` / `.submitted` / `.published` / `.deprecated` | Tool lifecycle | tool_id, version |
| `tool.invoked` | Tool executed | tool_id, version, execution_id |
| `model.route_version_created` / `.approved` | Route lifecycle | route_id, version |
| `model.generation_denied` | Generation blocked | route_key, reason_code |
| `model.budget_exceeded` | Budget hit | route_key, period |
| `model.kill_switch_engaged` / `model.kill_switch_released` | Kill switch | scope, actor, reason |
| `execution.requested` / `.denied` / `.completed` / `.cancelled` | Execution lifecycle | execution_id, datasource_id, denial_reason_code |
| `execution.cost_exceeded` | Cost ceiling hit | execution_id, estimate, ceiling |
| `execution.masking_applied` | Masking applied | execution_id, masked_column_count |
| `tool.certification_run.executed.v1` | Tool-version certification run scored | certification_run_id, tool_id, tool_version_id, status, score |
| `tool.certification_completed.v1` / `.certification_rejected.v1` | Reviewer decided a tool certification run | certification_run_id, tool_id, tool_version_id, status, expires_at |
| `tool_plan.execution_completed` | Multi-step tool plan execution finished | plan_id, status, steps_executed |

### Governance — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `policy.version_published` | Policy published | policy_id, version |
| `governance.review_requested.v1` | A governed object (tool version, model route, glossary term, semantic model, stewardship bulk operation, ...) submitted for review | review_id, object_type, object_id, requested_action |
| `proposal.submitted` / `.assigned` | Proposal lifecycle | proposal_id, object_type, maker |
| `decision.made` | Checker decided | proposal_id, checker, outcome, rationale_ref |
| `delegation.granted` / `.revoked` | Delegation | from, to, scope, until |
| `policy.decision_denied` | Authorization denial | principal, action, resource_type, reason_code |
| `mcp.tool_invocation_denied.v1` | MCP caller's role is not bound to an otherwise-existing governed tool | tool_slug, principal_id |

### Context products — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `context.product_published` / `.deprecated` | Product lifecycle | product_id, version |
| `context.product_consumed` | External read | product_id, version, consumer, purpose |
| `context.consumption_denied` | Read denied | product_id, consumer, reason_code |
| `context.budget_exceeded` | Consumer budget hit | consumer, period |
| `context.product_draft_created.v1` | New context product version drafted | context_product_id, context_product_version_id, version |
| `context.product_compiled.v1` | Context product compiled for a target consumer surface | context_product_version_id, target, artifact_hash |
| `context.product_tool_consumed.v1` | Governed tool invoked while scoped to a published context product | product_key, version, tool_version_id, principal_id |
| `context.product_consumer_binding_set.v1` | Consumer pinned (or moved) to a specific version for staged rollout (AT-7b) | product_key, consumer_principal_id, bound_version |
| `context.product_consumer_binding_removed.v1` | Consumer unpinned; falls back to the current published version (AT-7b) | product_key, consumer_principal_id |

### Agent workforce (AG-10 / ADR-0027) — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `agent.contract_published.v1` | AG-10: an agent version's contract was created or replaced -- its workload identity, capability envelope, autonomy tier, budget caps and kill scope. The contract is the agent's authority, so a change here is the change an auditor most wants to see | ai_asset_version_id, agent_principal_id, autonomy_tier |
| `agent.kill_switch_engaged.v1` | AG-10: an agent's kill switch was engaged. Takes effect on the agent's very next run -- the orchestrator queries the switch live rather than caching it | ai_asset_version_id, kill_scope, agent_principal_id |
| `agent.kill_switch_released.v1` | AG-10: an agent's kill switch was released and its runs may resume | ai_asset_version_id, kill_scope, agent_principal_id |
| `reviewer_agent.sample_resolved.v1` | ADR-0027 condition (b): a human resolved one sampled agent decision. The DISAGREED rate per object type is the metric ADR-0027's revisit trigger watches | sample_id, human_outcome, object_type, risk_tier |

### AI registry — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `ai_registry.asset_draft_created.v1` | New AI asset version drafted | ai_asset_id, asset_kind |
| `ai_registry.assessment_completed.v1` | Risk/compliance assessment run against an asset version | ai_asset_version_id, score, status |
| `ai_registry.remediation_opened.v1` | Remediation opened against an assessment finding | ai_asset_version_id, finding_key |
| `ai_registry.provider_evidence_synced.v1` | Third-party provider evidence synced onto an asset version | provider_type |

### Marketplace — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `data_product.draft_created.v1` | New data product version drafted | data_product_id, version |
| `data_product.access_requested.v1` | Maker-checker access request created for a published product version | review_id, data_product_version_id |
| `data_product.access_revoked.v1` | Access entitlement revoked | data_product_version_id |

### Notifications — topic `atlas.operational.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `notification.rule.created.v1` | Notification routing rule created | channel |

### Observability — topic `atlas.operational.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `observability.slo.created.v1` | SLO definition created | slo_key, target |

### Workspace — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `workspace.created.v1` | Workspace created under an organization | workspace_id, slug |
| `source_binding.requested.v1` | Datasource binding requested for a workspace, pending approval | binding_id, workspace_id, datasource_id |

### Studio — topic `atlas.governance.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `studio.change_set.submitted` | Change set submitted for review | change_set_id, name, author, item_count |

### Graph and retrieval — topic `atlas.operational.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `graph.projection.lagging` | Lag threshold exceeded | projection, lag_seconds |
| `graph.rebuild.started` / `.completed` | Rebuild | projection, duration_seconds |
| `retrieval.index_lagging` | Index lag | index, lag_seconds |
| `retrieval.reindex_completed` | Reindex done | index, duration_seconds |
| `knowledge_graph.drift_detected.v1` | KG-7 scheduled reconciliation found Postgres/Neo4j drift for a datasource | datasource_id, source, severity, fingerprint, missing_nodes, orphaned_nodes, missing_edges, orphaned_edges |
| `knowledge_graph.drift_alert_routed.v1` | Drift finding matched a `NotificationRuleRecord` and was routed through DQ-1's engine | datasource_id, notification_id, rule_id, channel, severity, dedup_key |
| `knowledge_graph.drift_itsm_payload.v1` | Drift alert routed to an ITSM-channel rule | datasource_id, correlation_id (see `notification_routing.format_itsm_payload`) |

### Audit — topic `atlas.audit.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `audit.event_recorded` | Any governed mutation | actor, action, resource, correlation_id |

### Reconciled emitted events (ST-14) — topic `aida.platform.events.v1`

> Added by **ST-14** (2026-09-01). The rows above in §2 are the *target* naming scheme; every
> row here is a name the code **actually emits today** via `record_outbox(event_type=...)`. The
> U2 authorial question (`Docs/review-2026-08/gap/04-documentation-truth-pass.md` §3 — "rename
> the code, or restate the catalog") is resolved here in favour of **restating the catalog**:
> the repo made a deliberate, repo-wide switch to `.v1`-versioned event names, live consumers
> (e.g. `aida.projectors.graph_projector`) key on the literal `.v1` strings, and renaming ~44
> emitted events back to their pre-`.v1` spelling would break those consumers. So the emitted
> `.v1` names are canonical; the pre-`.v1` rows in §2 are retained only as the historical
> intended vocabulary each one renames. This section is what `tests/test_event_catalog_gate.py`
> (TS-11) now checks `src/` against — the `KNOWN_ST14_DRIFT` baseline it used to carry is now
> empty, so a *new* undocumented `event_type=` fails the build instead of being absorbed.

| Event | Trigger | Key payload |
|---|---|---|
| `organization.created.v1` | same event as documented `tenant.created` (org level) | same envelope + payload as the event it renames (see Trigger) |
| `line_of_business.created.v1` | same event as documented `tenant.created` (LOB level) | same envelope + payload as the event it renames (see Trigger) |
| `project.created.v1` | same event as documented `tenant.created` (project level) | same envelope + payload as the event it renames (see Trigger) |
| `datasource.registered.v1` | same event as documented `datasource.registered` | same envelope + payload as the event it renames (see Trigger) |
| `catalog.asset.certified.v1` | same event as documented `catalog.asset.certified` | same envelope + payload as the event it renames (see Trigger) |
| `connector.certification.completed.v1` | same event as documented `certification.completed` | same envelope + payload as the event it renames (see Trigger) |
| `model_route.created.v1` | same event as documented `model.route_version_created` | same envelope + payload as the event it renames (see Trigger) |
| `model_route.approved.v1` | same event as documented `model.route_version_created / .approved` | same envelope + payload as the event it renames (see Trigger) |
| `model_route.rejected.v1` | reject sibling of the model-route-approval rename above | same envelope + payload as the event it renames (see Trigger) |
| `tool.version.draft_created.v1` | same event as documented `tool.drafted` | same envelope + payload as the event it renames (see Trigger) |
| `tool.version.published.v1` | same event as documented `tool.drafted / .published` | same envelope + payload as the event it renames (see Trigger) |
| `tool.version.deprecated.v1` | same event as documented `tool.drafted / .deprecated` | same envelope + payload as the event it renames (see Trigger) |
| `tool.version.rejected.v1` | reject sibling with no documented counterpart; same family as the tool-lifecycle rename above | same envelope + payload as the event it renames (see Trigger) |
| `tool.version.deprecation_rejected.v1` | reject sibling with no documented counterpart; same family as the tool-lifecycle rename above | same envelope + payload as the event it renames (see Trigger) |
| `tool.execution.completed.v1` | same event as documented `tool.invoked` | same envelope + payload as the event it renames (see Trigger) |
| `semantic_model.published.v1` | same event as documented `semantic.version_published` | same envelope + payload as the event it renames (see Trigger) |
| `semantic_model.rejected.v1` | reject sibling with no documented counterpart; same family as the semantic-model-publish rename above | same envelope + payload as the event it renames (see Trigger) |
| `agent.analysis.completed.v1` | same aggregate/lifecycle as documented `agent.run_started / .run_completed / .run_denied` | same envelope + payload as the event it renames (see Trigger) |
| `query.execution.completed.v1` | same event as documented `execution.completed` | same envelope + payload as the event it renames (see Trigger) |
| `query.feedback.updated.v1` | same event as documented `agent.feedback_recorded` | same envelope + payload as the event it renames (see Trigger) |
| `relationship_candidate.approved.v1` | same event as documented `relationship.approved` | same envelope + payload as the event it renames (see Trigger) |
| `relationship_candidate.rejected.v1` | same event as documented `relationship.rejected` | same envelope + payload as the event it renames (see Trigger) |
| `business_semantics.proposals_created.v1` | same event as documented `semantic.inference_completed` (run_id/proposal_count payload matches) | same envelope + payload as the event it renames (see Trigger) |
| `business_semantics.approved.v1` | same event as documented `semantic.annotation_published` (annotation_id/table_id payload matches) | same envelope + payload as the event it renames (see Trigger) |
| `business_semantics.rejected.v1` | reject sibling with no documented counterpart; same family as the annotation-publish rename above | same envelope + payload as the event it renames (see Trigger) |
| `dbt_artifact.imported.v1` | same event as documented `lineage.artifact_ingested` (dbt manifest case) | same envelope + payload as the event it renames (see Trigger) |
| `openlineage.run_event.ingested.v1` | same event as documented `lineage.artifact_ingested` (OpenLineage case) | same envelope + payload as the event it renames (see Trigger) |
| `metadata.discovery.snapshot.v1` | same event as documented `ingestion.delivered` | same envelope + payload as the event it renames (see Trigger) |
| `metadata.ingestion.batch.queued.v1` | same event as documented `batch.finalized` | same envelope + payload as the event it renames (see Trigger) |
| `data_quality.incident.transitioned.v1` | consolidates documented `quality.incident_opened` / `.incident_reopened` / `.incident_acknowledged` / `.resolved` / `.incident_auto_recovered` into one event with a status field | same envelope + payload as the event it renames (see Trigger) |
| `analysis_run.requested.v1` | pre-`started` state of the documented `analysis_run.started / .completed / .cancelled` lifecycle | same envelope + payload as the event it renames (see Trigger) |
| `analysis_run.scheduled.v1` | pre-`started` state of the documented `analysis_run.started / .completed / .cancelled` lifecycle | same envelope + payload as the event it renames (see Trigger) |
| `analysis_run.resumed.v1` | additional state of the documented `analysis_run.started / .completed / .cancelled` lifecycle | same envelope + payload as the event it renames (see Trigger) |
| `analysis_run.cancellation_requested.v1` | additional state of the documented `analysis_run.started / .completed / .cancelled` lifecycle | same envelope + payload as the event it renames (see Trigger) |
| `metadata.analysis.completed.v1` | renamed `analysis_run.completed` | same envelope + payload as the event it renames (see Trigger) |
| `metadata.analysis.cancelled.v1` | renamed `analysis_run.cancelled` | same envelope + payload as the event it renames (see Trigger) |
| `metadata.analysis.failed.v1` | additional terminal state of the same renamed analysis_run lifecycle | same envelope + payload as the event it renames (see Trigger) |
| `metadata.analysis.cancellation_race_completed.v1` | additional terminal state of the same renamed analysis_run lifecycle | same envelope + payload as the event it renames (see Trigger) |
| `context.product_consumed.v1` | same event as documented `context.product_consumed`, emitted via the MCP/REST read paths that were added after that row was written | same envelope + payload as the event it renames (see Trigger) |
| `context.product_consumption_denied.v1` | same event as documented `context.consumption_denied` | same envelope + payload as the event it renames (see Trigger) |
| `data_quality.incident_opened` | same event as documented `quality.incident_opened` | same envelope + payload as the event it renames (see Trigger) |
| `data_quality.incident_resolved` | same event as documented `quality.incident_acknowledged` / `.resolved` | same envelope + payload as the event it renames (see Trigger) |
| `canonical_table.resolved.v1` | same event as documented `canonical_table.resolved` | same envelope + payload as the event it renames (see Trigger) |
| `composite_relationship_candidate.decided.v1` | composite-candidate sibling of the already-documented-as-drift `relationship_candidate.decided.v1` (itself a consolidation of `relationship.approved` / `.rejected`); same decided-with-status-field shape, multi-column candidates instead of single-column | same envelope + payload as the event it renames (see Trigger) |

## 3. Adding an event

1. Add the row to this catalog.
2. Define the payload schema in the owning module's `events.py`.
3. Register with the schema registry (`BACKWARD` compatibility) — **planned; no schema registry exists (2026-08-30)**.
4. Confirm the payload carries no values or secrets — the publish-time validator enforces this.
5. Document consumers, or state explicitly that there are none yet.

CI asserts that every published event type appears in this catalog. **This gate now exists (ST-14 / TS-11, `tests/test_event_catalog_gate.py`):** it scans every `event_type=` passed to `record_outbox` in `src/` and fails the build if one is neither documented here nor named in the (now-empty) `KNOWN_ST14_DRIFT` baseline. Before it existed, the drift documented in the status note at the top of this file was able to accumulate unnoticed.

## Related documents

- Event and messaging model: `10-architecture/07-event-and-messaging-model.md`
- Contract strategy: `30-contracts/01-contract-strategy.md`
