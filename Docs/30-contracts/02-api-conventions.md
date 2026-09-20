# API Conventions

> Status: Authoritative. Owner: Architecture.
> Applies to every HTTP route in the control plane. Consistency here is what makes the API's endpoints learnable (508 operations across 440 paths as of 2026-09-20; `Docs/90-reference/openapi-baseline.json` is the current inventory).

> **Implementation status (2026-09-20).** These are the conventions the API is meant to follow, and the code follows them unevenly. Where a section below does something different today it carries its own note, measured against `Docs/90-reference/openapi-baseline.json` (paths and operation counts there are the source of truth; counts typed here are dated).

## 1. Resource naming

| Rule | Example |
|---|---|
| Plural nouns | `/v1/datasources/{datasource_id}`, `/v1/tables/{table_id}/columns` |
| Kebab-case for multi-word | `/v1/metadata-ingestion-batches` |
| Nesting only for true ownership | `/v1/datasources/{id}/metadata-ingestions` |
| Max nesting depth 2 | Beyond that, use a top-level resource with a filter |
| Actions as sub-resources, not verbs | Target: `POST /v1/analysis-runs/{id}/cancellation` — not `/cancel` |

> **Implementation status (2026-09-20).** The last rule is not followed: there is no `/cancellation` route, and actions are verb-suffixed `POST` routes, for example `POST /v1/analysis-runs/{run_id}/cancel`, `POST /v1/analysis-runs/{run_id}/resume`, `POST /v1/metadata-ingestion-batches/{batch_id}/pause|resume|cancel|replay` and `POST /v1/tool-plans/{plan_id}/cancel`. Renaming them is a breaking change under [01-contract-strategy.md](01-contract-strategy.md) §2. Top-level collections such as `/v1/tables` and `/v1/datasources` do not exist either: lists hang off their parent (`GET /v1/projects/{project_id}/datasources`, `GET /v1/datasources/{datasource_id}/tables`).

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

Target: cursor-based everywhere, with no offset pagination.

```http
GET /v1/datasources/{datasource_id}/tables?limit=50&cursor=eyJpZCI6...
```

Target response envelope:

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

> **Implementation status (2026-09-20).** Cursor pagination is the target, not the current contract. Measured against `Docs/90-reference/openapi-baseline.json` as of 2026-09-20: 131 of 263 `GET` operations take an `offset` query parameter and only 8 take `cursor` — the catalog reads `GET /v1/datasources/{datasource_id}/tables` and `GET /v1/tables/{table_id}/columns|constraints|indexes|partitions`, `GET /v1/organizations/{organization_id}/catalog/rows`, and two governance review-batch reads. Most list endpoints return `Page` (`items`, `limit`, `offset`, `total`), so a total count is part of the ordinary response; the cursor-capable ones return `CursorPage` (`Page` plus `next_cursor`, with `total` null on the cursor path), and an invalid cursor is a 400 (`src/aida/schemas.py`, `src/aida/api.py`). No response carries `has_more`. Limits are set per route rather than by one rule: of the 144 `GET` operations with a `limit` parameter, 89 default to 100 and 25 to 50, and 95 cap at 500 and 19 at 200.

## 4. Filtering and sorting

```http
GET /v1/datasources/{datasource_id}/tables?object_type=VIEW&status=ACTIVE&q=order
```

| Rule | Detail |
|---|---|
| Filters are explicit query parameters | No generic query language in v1 |
| Every filter is indexed | An unindexed filter is a rejected feature |
| Sort with `-` prefix for descending | Target: whitelisted fields only |
| Unknown parameters are **rejected**, not ignored | Target: a silently ignored typo is a silently wrong result |

> **Implementation status (2026-09-20).** Filters are explicit query parameters, but the last two rules are not implemented. Unknown query parameters are ignored, not rejected: `GET /v1/organizations?bogus_param=1` returns 200 (checked against the running API 2026-09-20). No route takes a `-field` sort; the only `sort` parameter in `Docs/90-reference/openapi-baseline.json` is an enum (`personalized` or `catalog`) on `GET /v1/marketplace/products`. There is no top-level `GET /v1/tables`; the example above is the real filtered table list (`q`, `object_type`, `status`, `limit`, `offset`, `cursor`).

## 5. Identity and tenancy

| Header | Purpose |
|---|---|
| `Authorization: Bearer <token>` | OIDC token (production). The tenant and the declared purpose come from the token's claims (`organization_id` and `business_purpose` by default; the claim names are settings) |
| `X-Organization-Id` | Development identity only: the organization the principal acts in |
| `X-Business-Purpose` | Development identity only: declared purpose (cut to 200 characters), read by purpose-bound operations such as context-product reads |
| `X-Principal-Id`, `X-Principal-Type`, `X-Roles` | Development identity only: who the caller is (`X-Principal-Id` is required; the type defaults to `USER` and the roles to `Viewer`) |
| `X-Correlation-Id` | Client-supplied correlation; generated if absent, and echoed on every response |

The development identity headers are listed on every operation in the generated OpenAPI spec and are **refused in production** (INV-4): the API will not start with the development identity provider when `environment` is `production` (`src/atlas/platform/config.py`). `X-Atlas-Organization` and `X-Atlas-Purpose` were the original design names; no code reads them.

## 6. Idempotency

Target: every non-GET that creates a resource accepts `Idempotency-Key`.

| Case | Behaviour |
|---|---|
| Same key, same payload | Returns the original result |
| Same key, different payload | **409 Conflict** |
| No key on a create | Allowed but discouraged; documented per endpoint |
| Key scope | Per tenant + per endpoint |
| Key retention | 24 hours minimum |

> **Implementation status (2026-09-20).** No REST operation accepts an `Idempotency-Key` header (0 of 508 operations in `Docs/90-reference/openapi-baseline.json` as of 2026-09-20). Idempotency is per endpoint and carried in the body: `POST /v1/datasources/{datasource_id}/metadata-ingestions` takes a required `idempotency_key` (a repeat with the same envelope returns the original job, a repeat with a different envelope is a 409), and `POST /v1/datasources/{datasource_id}/metadata-ingestion-batches` takes a `batch_key` with the same 409 behaviour (`src/atlas/modules/ingestion/router.py`). Both keys are unique per datasource and live as long as the job or batch row; there is no general per-tenant key store with a 24-hour retention. The GraphQL governed-execution mutation also takes a caller-chosen `idempotencyKey`.

## 7. Long-running operations

Anything that may exceed 5 seconds returns 202 with a job resource.

Target job resource:

```json
{
  "job_id": "job_...",
  "status": "RUNNING",
  "progress": {"completed": 340, "total": 1200, "unit": "tables"},
  "links": {"self": "/v1/jobs/job_...", "result": null}
}
```

Polling, not long-lived connections. Progress is real, not a spinner — a batch reports chunks processed, a scan reports tables completed.

> **Implementation status (2026-09-20).** There is no generic job resource and no `/v1/jobs` route. A long-running `POST` returns 202 with the domain resource itself, which is then polled: `POST /v1/datasources/{datasource_id}/analysis-runs` returns an analysis run (`status` plus discovered and profiled counts; poll `GET /v1/analysis-runs/{run_id}`), and `POST /v1/metadata-ingestion-batches/{batch_id}/finalize` returns the batch (`status`, `expected_chunks`, `received_chunks`, `processed_chunks`; poll `GET /v1/metadata-ingestion-batches/{batch_id}`). The progress the last sentence asks for is real on both.

## 8. Bulk operations

```json
POST /v1/organizations/{organization_id}/tables/bulk-tag
{
  "filter": {"datasource_id": "<uuid>", "match_field": "SCHEMA_NAME", "match_pattern": "sales"},
  "tag_key": "pii-reviewed",
  "tag_value": "Q3 PII review"
}
```

| Rule | Detail |
|---|---|
| Selection by filter **or** explicit IDs | Filter selection avoids sending 10,000 IDs |
| Async above a threshold | Target: returns a job resource |
| Partial success reported per item | Never silently partial |
| Rationale required for governed operations | Feeds the audit ledger |
| Same authorization per item | Bulk is not a privilege escalation |

> **Implementation status (2026-09-20).** The catalog bulk routes (`bulk-tag`, `bulk-classify`, `bulk-own` and `bulk-certify` under `/v1/organizations/{organization_id}/tables/`) take exactly one selection, either `filter` or `table_ids` (1 to 500 ids), and report per-item `SUCCEEDED` or `FAILED` results in the returned run record. They are synchronous: the response is 200 with the finished run, not a job resource, and the 500-id cap stands in for the async threshold. A `rationale` is required on `bulk-certify` (minimum 10 characters); the tag, classify and own bodies have no rationale field. There is no `POST /v1/tables/bulk-tag`, and no `selection`/`operation` request shape.

## 9. Response envelope

Single resources are returned bare (no wrapper). Collections use the pagination envelope. Errors are meant to use the error envelope from `01-contract-strategy.md` §6.

Timestamps are RFC 3339 UTC with `Z`. Durations are ISO 8601. Money and precise decimals are strings, never floats. IDs are prefixed opaque strings (`tbl_`, `ds_`, `run_`) so a misrouted ID is caught immediately.

> **Implementation status (2026-09-20).** Errors do not yet use that envelope: handled errors return FastAPI's `{"detail": ...}` body (see the note in `01-contract-strategy.md` §6). Resource IDs are UUIDs (nearly every `*_id` path parameter is `format: uuid` in `Docs/90-reference/openapi-baseline.json`), not prefixed strings.

## 10. Rate limiting

Target headers:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Window limit |
| `X-RateLimit-Remaining` | Remaining |
| `X-RateLimit-Reset` | Reset epoch |
| `Retry-After` | On 429 |

Limits are per principal, per tenant, and per endpoint class. Expensive endpoints (search, graph, impact) have their own class.

> **Implementation status (2026-09-20).** No `/v1` route rate-limits a caller. Rate-limit headers and 429s exist only on the `/mcp` and `/graphql` endpoints, and only when their budgets are switched on (`mcp_budget_enabled` and `graphql_budget_enabled` both default to false in `src/atlas/platform/config.py`). Where they run, `budget_headers` in `src/aida/request_budget.py` sends `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Bucket` and, on a refusal, `Retry-After`; `X-RateLimit-Reset` is never emitted. The other 429s on `/v1` relay a model provider's own throttle on the Ask and draft routes.

## 11. Documentation requirements

Every endpoint in the generated OpenAPI spec must carry: summary and description, required roles, tenancy behaviour, all error codes it can return, at least one example request and response, rate-limit class, and deprecation status. An endpoint missing any of these is intended to fail the docs lint in CI. **Planned, not wired (2026-08-30):** there is no docs-lint step in `.github/workflows/ci.yml` and no tool for it in the `dev` extras, so this requirement is enforced by review only.

## Related documents

- Contract strategy: `30-contracts/01-contract-strategy.md`
- Coding standards: `40-engineering/03-coding-standards.md`
