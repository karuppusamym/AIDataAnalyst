# Module 20 — Observability and Audit

> Layer L1 (cross-cutting) · Schema `audit` · Owner: Platform + Compliance

## 1. Purpose

Holds the evidence: the attributable audit ledger, the transactional outbox, telemetry, SLO state, and compliance packs. This is the second of the two modules callable from any layer (with 17), because audit must be writable everywhere.

Its distinguishing property: **the ledger is evidence, not logs.** Value-free by construction, append-only, replayable, and safe to retain for seven years.

## 2. Jobs served

U1 (every action on this asset), U2 (prove constraints), U4 (evidence pack), P2, P3, P6 (cost).

## 3. Responsibilities

- Append-only audit ledger with attribution and correlation.
- Transactional outbox: write, publish, retry, dead-letter, authorized requeue.
- Structured logging with tenant and correlation context.
- OpenTelemetry traces and metrics.
- SLO definition and error-budget tracking.
- Compliance pack generation from runtime evidence.
- WORM archive export.
- SIEM routing.
- Cost and showback aggregation.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Deciding what is auditable | Every module — via the platform unit-of-work |
| Being the SIEM | Enterprise SOC |
| Being the metrics store | Enterprise observability |
| Policy decisions | 17 policy-governance |

## 5. Domain model

```text
audit_event (actor, action, resource, tenancy, correlation_id, occurred_at, detail)
outbox_event, dead_letter
slo_definition, slo_state, error_budget
compliance_pack, pack_artifact
cost_record (dimension, tenancy, quantity, period)
```

## 6. The audit contract

| Property | Requirement |
|---|---|
| Atomicity | Written **in the same transaction** as the mutation (INV-7) |
| Attribution | Actor identity, kind (user/workload/system), tenancy, correlation ID |
| Immutability | Append-only. Never updated, never deleted. |
| Value-freedom | No source values, no credentials, no raw question text (INV-6) |
| Completeness | Every mutation of a governed table produces one — enforced by the unit-of-work commit path |
| Retention | 7 years hot + WORM archive |
| Export | SIEM routing and auditor-facing export |

The atomicity requirement is what distinguishes an audit ledger from a log. A log can be missing an entry after a crash; a ledger written in the mutation's transaction cannot.

## 7. Compliance packs — whitespace W5

Generated **from runtime evidence**, not authored by hand. Collibra ships BCBS 239 *controls*; nobody generates the evidence automatically.

| Pack | Contents |
|---|---|
| Model risk (SR 11-7 style) | Route inventory, approval chains, evaluation results, refusal statistics, kill-switch drill evidence, generation evidence summary |
| BCBS 239 | Lineage coverage, ownership coverage, quality posture, timeliness evidence, change control |
| Access review | Principal-to-entitlement mapping, delegation history, cross-tenant denial evidence |
| AI usage | Consumption by consumer, purpose, tenant; denials with reason codes |
| Change control | All approvals in period with maker, checker, rationale, and version deltas |

Each pack is reproducible: same period, same inputs, same output — and is WORM-archived on generation.

## 8. SLOs

| SLO | Target | Error budget |
|---|---|---|
| Control-plane API availability | 99.95% monthly | 21.6 min/month |
| Interactive query orchestration | 99.9% monthly, excl. source outages | 43.2 min/month |
| Metadata RPO | 15 minutes | — |
| Metadata RTO | 4 hours | — |
| Audit RPO | ~0 | — |
| Discovery task success after retry | ≥ 99.5% for healthy sources | — |
| Projection lag | < 5 minutes p95 | — |
| **Unauthorized query execution** | **Zero** | **Zero** |
| **Cross-LOB data leakage** | **Zero** | **Zero** |

The last two have no error budget. A single occurrence is an incident, not a budget draw.

## 9. Public interface

```python
# observability_audit/api.py
def record(event: AuditEvent) -> None                    # same transaction as the mutation
def search_audit(scope, filt, page) -> Page[AuditEventDTO]
def publish_outbox(batch_size: int) -> PublishResult
def requeue_dead_letter(scope, dead_letter_id) -> RequeueResult   # authorized only
def generate_pack(scope, pack_type, period) -> CompliancePackDTO
def get_slo_state(scope) -> SLOStateDTO
def get_cost_report(scope, period, dimension) -> CostReportDTO
```

## 10. Telemetry

| Signal | Contents |
|---|---|
| Traces | Correlation ID propagated across API, workers, projectors; span per state transition |
| Metrics | Latency histograms per operation, task counters, queue depth, projection lag, bound-hit counters, refusal rates |
| Logs | Structured, tenant and correlation context, **scrubbed of values and secrets** |

Log scrubbing is a middleware, not a coding convention. A convention fails the first time someone logs an exception containing a row.

## 11. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Audit ledger | Implemented — attributable, correlation IDs, bounded detail | WORM archive, retention enforcement |
| Outbox | Implemented — transactional, idempotent publication, retry/backoff, dead-letter, authorized requeue | Broker ACLs, event signatures |
| Structured logging | Implemented | Scrubbing middleware verification |
| OpenTelemetry export | **Not implemented** | Entry-ticket gap |
| SIEM routing | **Not implemented** | Entry-ticket gap |
| SLO alerting | Not implemented | Required for operations |
| Compliance packs | **Not implemented** | Differentiator W5 |
| Cost / showback | Not implemented | Required for LOB chargeback |
| Access review | Not implemented | Required for production |

## 12. Open work

| ID | Item | Priority |
|---|---|---|
| OB-1 | OpenTelemetry export | P0 |
| OB-2 | SIEM routing | P0 |
| OB-3 | WORM archive with retention enforcement | P0 |
| OB-4 | SLO definitions with alerting and error budgets | P0 |
| OB-5 | Compliance pack generation | P1 |
| OB-6 | Cost and showback aggregation | P1 |
| OB-7 | Access review reporting | P1 |
| OB-8 | Log-scrubbing verification test (sentinel scan) | P0 |
