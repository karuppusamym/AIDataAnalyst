# Event Catalog

> Status: Authoritative **as a target naming scheme**. Owner: Architecture.
> The named set of domain events Atlas publishes. Adding an event means adding a row here.

> **Implementation status (2026-08-30). Most event names in §2 are not the names the code
> emits.** Verified by extracting every `event_type=` argument passed to `record_outbox` across
> `src/aida/` and comparing it to this catalog:
>
> * The platform emits **~55 event types, all suffixed `.v1`** — e.g.
>   `datasource.registered.v1`, `metadata.discovery.snapshot.v1`, `query.execution.completed.v1`,
>   `context.product_consumed.v1`, `relationship_candidate.decided.v1`,
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
> Treat §2 as the intended vocabulary and the outbox rows as the current one. Reconciling the
> two — either renaming emitted events or restating this catalog — is unscheduled work the
> orchestrator should track; it is not a rename that can be done safely from the docs alone,
> because consumers key on the emitted names.

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
| `rename_candidate.decided.v1` | CT-4: steward approved/rejected a proposed rename | candidate_id, status |
| `cross_source_resolution_candidate.decided.v1` | CT-6: steward approved/rejected a proposed cross-source match | candidate_id, status |

### Profiling — topic `atlas.catalog.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `profile.completed` | Table profiled | table_id, run_id, statistics_summary |
| `profile.failed` | Profiling failed | table_id, run_id, reason_code |
| `classification.assigned` | Column classified | column_id, classification, confidence, rule_version |
| `key.inferred` | Key inferred | table_id, columns, confidence |
| `analysis_run.started` / `.completed` / `.cancelled` | Run lifecycle | run_id, scope, counts |

### Relationships — topic `atlas.catalog.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `relationship.candidate_generated` | Candidate scored | candidate_id, from, to, confidence |
| `relationship.approved` | Checker approved | candidate_id, checker, rationale_ref |
| `relationship.rejected` | Checker rejected | candidate_id, checker, rationale_ref |
| `table_family.detected` | Family identified | family_id, family_type, members |
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
| `glossary.conflict_resolved.v1` / `.conflict_resolution_rejected.v1` | Governed conflict-resolution decision | conflict_id, review_id, resolution_status |
| `glossary.link_proposal_approved.v1` / `.link_proposal_rejected.v1` | Governed inferred-link decision | proposal_id, table_id, term_id, review_id |
| `ownership.assigned.v1` | Approved bulk/rule ownership applied | operation_id, subject_type, applied_count |
| `glossary.term_linked_bulk.v1` | Approved bulk term links applied | operation_id, term_id, applied_count |
| `glossary.term_deprecated.v1` | Approved term deprecation applied | operation_id, applied_count |
| `certification.granted.v1` | Approved asset certification applied | operation_id, expires_at, applied_count |
| `stewardship.bulk_operation_rejected.v1` | Checker rejected a bulk stewardship request | operation_id, review_id |
| `stewardship.coverage_computed.v1` | Coverage snapshot persisted | snapshot_id, scope, overall_score |
| `stewardship.unowned_asset_routed.v1` | Unowned-asset backlog entry routed to a candidate owner | table_id, candidate_owner |
| `stewardship.unowned_asset_escalated.v1` | Backlog entry escalated (no owner found/accepted) | table_id |
| `stewardship.unowned_asset_resolved.v1` | Backlog entry resolved (ownership since assigned) | table_id |

### Lineage — topic `atlas.lineage.v1` (key: `datasource_id`)

| Event | Trigger | Key payload |
|---|---|---|
| `lineage.edge_created` | Edge persisted | edge_id, kind, from, to, confidence |
| `lineage.artifact_ingested` | dbt manifest / OpenLineage / DDL ingested | artifact_id, kind, counts |
| `lineage.impact_computed` | Impact computed | node_ref, affected_counts |
| `bi_artifact.imported.v1` | BI tool metadata (Tableau/Power BI/Looker) imported | artifact_import_id, connection_id, bi_tool, report_count, metric_count |
| `lineage.consumed.v1` | Unified lineage graph/impact read via a native MCP lineage tool | tool_slug, principal_id, channel |

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
| `model.kill_switch_engaged` / `.released` | Kill switch | scope, actor, reason |
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

### Audit — topic `atlas.audit.v1`

| Event | Trigger | Key payload |
|---|---|---|
| `audit.event_recorded` | Any governed mutation | actor, action, resource, correlation_id |

## 3. Adding an event

1. Add the row to this catalog.
2. Define the payload schema in the owning module's `events.py`.
3. Register with the schema registry (`BACKWARD` compatibility) — **planned; no schema registry exists (2026-08-30)**.
4. Confirm the payload carries no values or secrets — the publish-time validator enforces this.
5. Document consumers, or state explicitly that there are none yet.

CI is intended to assert that every published event type appears in this catalog. **It does not (2026-08-30)** — no such gate exists, which is why the drift documented in the status note at the top of this file was able to accumulate unnoticed.

## Related documents

- Event and messaging model: `10-architecture/07-event-and-messaging-model.md`
- Contract strategy: `30-contracts/01-contract-strategy.md`
