# API Conventions

> Status: Authoritative. Owner: Architecture.
> Applies to every HTTP route in the control plane. Consistency here is what makes 200+ endpoints learnable.

## 1. Resource naming

| Rule | Example |
|---|---|
| Plural nouns | `/v1/datasources`, `/v1/tables` |
| Kebab-case for multi-word | `/v1/metadata-ingestion-batches` |
| Nesting only for true ownership | `/v1/datasources/{id}/metadata-ingestions` |
| Max nesting depth 2 | Beyond that, use a top-level resource with a filter |
| Actions as sub-resources, not verbs | `POST /v1/analysis-runs/{id}/cancellation` — not `/cancel` |

## 2. Methods and status codes

| Method | Semantics | Success |
|---|---|---|
| GET | Safe, idempotent | 200 |
| POST | Create or action | 201 (created) / 202 (accepted) / 200 (action complete) |
| PUT | Full idempotent replace | 200 |
| PATCH | Partial update | 200 |
| DELETE | Soft delete / deprecate | 204 |

| Status | Used for |
|---|---|
| 400 | Malformed request |
| 401 | Missing or invalid identity |
| 403 | Authenticated but not permitted (**includes cross-tenant** — never 404, which would confirm existence) |
| 404 | Resource does not exist within the caller's scope |
| 409 | Conflict — idempotency key reuse with different content; version conflict |
| 422 | Semantically invalid (validation) |
| 429 | Rate or quota exceeded |
| 503 | Dependency unavailable, fail-closed denial |

**On 403 vs 404 for cross-tenant.** Returning 404 would leak existence through a timing or enumeration side channel in some flows and 403 in others. Atlas returns **403 consistently** for authorization failures on resources that exist outside scope, and 404 only when the resource does not exist in the caller's scope at all — so the two responses do not distinguish "exists elsewhere" from "does not exist."

## 3. Pagination

Cursor-based everywhere. Offset pagination is not offered.

```http
GET /v1/tables?limit=50&cursor=eyJpZCI6...
```

```json
{
  "items": [ ],
  "next_cursor": "eyJpZCI6...",
  "has_more": true
}
```

| Rule | Reason |
|---|---|
| Cursor, not offset | Stable under concurrent inserts; performs at millions of rows |
| Default limit 50, max 200 | Bounded responses (P3) |
| Cursors are opaque | Encoding may change |
| **No total count by default** | Counting 30M rows per request is a self-inflicted outage; available as an explicit, estimated, separately-priced query |

## 4. Filtering and sorting

```http
GET /v1/tables?datasource_id=ds_1&object_type=BASE_TABLE&updated_after=2026-08-01&sort=-updated_at
```

| Rule | Detail |
|---|---|
| Filters are explicit query parameters | No generic query language in v1 |
| Every filter is indexed | An unindexed filter is a rejected feature |
| Sort with `-` prefix for descending | Whitelisted fields only |
| Unknown parameters are **rejected**, not ignored | A silently ignored typo is a silently wrong result |

## 5. Identity and tenancy

| Header | Purpose |
|---|---|
| `Authorization: Bearer <token>` | OIDC token (production) |
| `X-Atlas-Organization` | Explicit organization when the principal spans several |
| `X-Atlas-Purpose` | Declared purpose, required for purpose-bound operations |
| `X-Correlation-Id` | Client-supplied correlation; generated if absent |

Development identity headers are documented in the generated OpenAPI spec and **refused in production** (INV-4).

## 6. Idempotency

Every non-GET that creates a resource accepts `Idempotency-Key`.

| Case | Behaviour |
|---|---|
| Same key, same payload | Returns the original result |
| Same key, different payload | **409 Conflict** |
| No key on a create | Allowed but discouraged; documented per endpoint |
| Key scope | Per tenant + per endpoint |
| Key retention | 24 hours minimum |

## 7. Long-running operations

Anything that may exceed 5 seconds returns 202 with a job resource.

```json
{
  "job_id": "job_...",
  "status": "RUNNING",
  "progress": {"completed": 340, "total": 1200, "unit": "tables"},
  "links": {"self": "/v1/jobs/job_...", "result": null}
}
```

Polling, not long-lived connections. Progress is real, not a spinner — a batch reports chunks processed, a scan reports tables completed.

## 8. Bulk operations

```json
POST /v1/tables/bulk-tag
{
  "selection": {"filter": {"schema_id": "sch_1"}},
  "operation": {"add_tags": ["pii-reviewed"]},
  "rationale": "Q3 PII review"
}
```

| Rule | Detail |
|---|---|
| Selection by filter **or** explicit IDs | Filter selection avoids sending 10,000 IDs |
| Async above a threshold | Returns a job resource |
| Partial success reported per item | Never silently partial |
| Rationale required for governed operations | Feeds the audit ledger |
| Same authorization per item | Bulk is not a privilege escalation |

## 9. Response envelope

Single resources are returned bare (no wrapper). Collections use the pagination envelope. Errors use the error envelope from `01-contract-strategy.md` §6.

Timestamps are RFC 3339 UTC with `Z`. Durations are ISO 8601. Money and precise decimals are strings, never floats. IDs are prefixed opaque strings (`tbl_`, `ds_`, `run_`) so a misrouted ID is caught immediately.

## 10. Rate limiting

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Window limit |
| `X-RateLimit-Remaining` | Remaining |
| `X-RateLimit-Reset` | Reset epoch |
| `Retry-After` | On 429 |

Limits are per principal, per tenant, and per endpoint class. Expensive endpoints (search, graph, impact) have their own class.

## 11. Documentation requirements

Every endpoint in the generated OpenAPI spec must carry: summary and description, required roles, tenancy behaviour, all error codes it can return, at least one example request and response, rate-limit class, and deprecation status. An endpoint missing any of these is intended to fail the docs lint in CI. **Planned, not wired (2026-08-30):** there is no docs-lint step in `.github/workflows/ci.yml` and no tool for it in the `dev` extras, so this requirement is enforced by review only.

## Related documents

- Contract strategy: `30-contracts/01-contract-strategy.md`
- Coding standards: `40-engineering/03-coding-standards.md`
