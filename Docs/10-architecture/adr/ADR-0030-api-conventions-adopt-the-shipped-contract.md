# ADR-0030 — API Conventions: Adopt the Shipped Contract, Not the Documented Target

**Status:** Accepted | **Date:** 2026-09-21 | **Owner:** Architecture + Product

> **Accepted 2026-09-21**, on the product owner's instruction to proceed with the open R11 rows (tracker R11-AUD06). It was
> drafted as Proposed on 2026-09-20; the Context, Decision and Revisit sections below are unchanged. Nothing in the code
> changes: this records the contract the API already has. It is reversed the way the register says, by an ADR that
> supersedes it, and the three revisit triggers below are what makes writing one correct.

## Context

[`02-api-conventions.md`](../../30-contracts/02-api-conventions.md) and [`01-contract-strategy.md`](../../30-contracts/01-contract-strategy.md) describe seven conventions as design targets:

1. cursor pagination everywhere, with no offset, no default total count and a `has_more` flag;
2. one error envelope, `{"error": {"code", "message", "correlation_id", "details", "retryable"}}`;
3. an `Idempotency-Key` header on every create;
4. an `X-RateLimit-Reset` header, on rate-limited routes generally;
5. `Deprecation` and `Sunset` headers on deprecated endpoints;
6. publish-time validation of event payloads (no source values, no secrets, bounded size, tenancy fields mandatory);
7. an `event_version` field on every event.

Each section carries a dated **Implementation status** callout saying the code does something else, and tracker row R11-AUD06 (TODO, found 2026-09-20) asks for each convention to be built or retired in this register before the first external SDK consumer. What the code does, measured 2026-09-20 (`Docs/90-reference/openapi-baseline.json` is the API inventory; the counts are dated and will move):

| Convention | The shipped contract |
|---|---|
| Pagination | 131 of 263 `GET` operations take `offset` and 8 take `cursor`: `GET /v1/datasources/{datasource_id}/tables`, `GET /v1/tables/{table_id}/columns`, `constraints`, `indexes` and `partitions`, `GET /v1/organizations/{organization_id}/catalog/rows`, and two governance review reads. List routes return `Page` (`items`, `limit`, `offset`, `total`); the cursor-capable ones return `CursorPage` (`next_cursor`, with `total` null on the cursor path). No response carries `has_more` (`src/aida/schemas.py`) |
| Errors | Handled errors are FastAPI's `{"detail": ...}`: a string, a list for a 422, or an object with its own `code` on a few routes. The `error` object appears only for an unhandled 500, as `{"error": {"code", "message", "correlation_id"}}` (`src/aida/main.py`). Every response carries an `X-Correlation-Id` header |
| Idempotency | 0 of 508 operations accept an `Idempotency-Key` header. Ingestion carries its key in the body: a required `idempotency_key` on `POST /v1/datasources/{datasource_id}/metadata-ingestions` and a `batch_key` on `POST /v1/datasources/{datasource_id}/metadata-ingestion-batches`, each a 409 when reused with different content (`src/atlas/modules/ingestion/router.py`). GraphQL's `executeGovernedTool` takes its own `idempotencyKey` |
| Rate limiting | No `/v1` route rate-limits a caller (the 429s it does return relay a model provider's own throttle). `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Bucket` and `Retry-After` are sent only by `/mcp` and `/graphql`, and only while `mcp_budget_enabled` or `graphql_budget_enabled` is on (both default to false). `X-RateLimit-Reset` is emitted nowhere (`src/aida/request_budget.py`) |
| Deprecation | `Deprecation` and `Sunset` are emitted nowhere in `src/`. Three operations, the `/api/v1/organizations/{organization_id}/consumption-lineage/...` aliases, carry `deprecated: true` in the OpenAPI spec and send no header |
| Event payloads | `record_outbox` (`src/aida/events.py`) checks no payload and sets no size bound, and `organization_id` is nullable. `tests/test_event_catalog_gate.py` checks that a literal `event_type=` is documented in the event catalog, not what its payload holds; 13 computed call sites are outside even that |
| Event versioning | No `event_version` field. `serialize_event` (`src/aida/projectors/outbox_publisher.py`) sends `event_id`, `event_type`, `aggregate_type`, `aggregate_id`, `organization_id`, `occurred_at` and `payload`. The only version marker is the `.v1` suffix in the event type, which 10 of 129 literal event types lack. One Kafka topic carries every event, and there is no schema registry |

Two forces make the gap worth a decision rather than a footnote. The REST API is a T1 contract ("backward-compatible within a major version", `01-contract-strategy.md` §1), and the OpenAPI diff gate (`scripts/openapi_diff.py` against the committed baseline) holds the *shipped* shape, so the target text and the contract disagree inside the same documents, and a callout is all that says which is which. And three of the seven are breaking to adopt against what exists: cursor-only pagination removes `offset` and `total`, the error envelope replaces every handled error body, and publish-time validation would refuse payloads that publish today. Each is "removing a field" or "changing default behaviour" under `01-contract-strategy.md` §2, and so a new major version. The other four (`Idempotency-Key`, `X-RateLimit-Reset`, `Deprecation` and `Sunset`, `event_version`) are additive and compatible.

The question: **would adopting the targets now buy a consumer anything, or only move the ones that exist?**

## Decision

**The shipped contract is the contract.** The seven documented target conventions are not adopted for the current API.

1. **What a consumer may rely on is the table above.** Limit and offset pagination, with `cursor` on the few routes that have it; `{"detail": ...}` errors; a body-field `idempotency_key` on ingestion; rate-limit headers on `/mcp` and `/graphql` only. `Docs/90-reference/openapi-baseline.json` and its diff gate are where that is enforced.
2. **New routes follow the shipped conventions**, so the API stays one contract rather than two. A list returns `Page`; a new high-volume list may reuse the `CursorPage` shape the eight routes above already use, rather than invent another. An error is an `HTTPException` with `detail`, an object with its own `code` where a consumer has to branch. A create that needs a safe retry carries its key in the body, as ingestion does.
3. **The target text stays, as direction.** The target sections of the two convention documents remain beside their implementation-status callouts. They are not a review criterion: code review does not reject a change for following the shipped shape. A change that adopts a target is a contract change under `01-contract-strategy.md` §2 and needs an ADR that supersedes this one.
4. **Additive adoption is deferred, not forbidden.** `Idempotency-Key`, `X-RateLimit-Reset`, `Deprecation` and `Sunset`, and `event_version` can each be added later without a major version. Until the revisit trigger fires, none is promised to anyone.

## Consequences

### Positive

* The contract documents make one claim. The T1 promise attaches to a shape that exists and that the OpenAPI diff gate already protects; nothing has to be migrated for it to become true.
* No breaking change is scheduled. Converting the offset-paged routes, or replacing every handled error body, would need a new major version and a change to the shipped UI, which decodes exactly the two error shapes the API emits (`ui-next/src/lib/http.ts`).
* A reviewer has one standard for a new route: the routes next to it.
* Tracker R11-AUD06 gets its "retire" answer for all seven conventions, in the register where it asked for one. The row is section P's to close.

### Negative

* **No safe retry for a create outside ingestion.** A client that times out on a `POST` cannot retry it with a guarantee that it ran once, and a bank integrator will ask for exactly that.
* **No stable error code for most errors.** A consumer of a handled error has the status and a `detail` message that nothing promises to keep. `01-contract-strategy.md` §6's "codes are stable identifiers" holds only on the few routes that return a coded object.
* **Offset paging degrades with depth, and `Page` counts `total` on every request.** Tracker CT-2 measured, on a 100,000-table proxy catalog, a depth-1000 read at p50 153.22 ms by offset against 4.09 ms by keyset. That is why the high-volume catalog routes have a cursor; an offset-paged list elsewhere pays that cost as its table grows.
* **No back-off signal on `/v1`.** A caller has no rate-limit header to slow down by, because no `/v1` route limits a caller.
* **Deprecation is invisible on the wire.** A consumer learns of one from the OpenAPI flag (three operations today) or the changelog, not from a response header.
* **Events carry no version, no registry and no publish-time check.** A consumer of `aida.platform.events.v1` cannot negotiate payload shape by version. The payload rules (no source values, bounded size, tenancy mandatory) rest on review and on INV-6's in-process scans of the paths it drives (`tests/test_inv6_value_freedom.py`), so the publisher will not refuse a value-bearing or tenant-less event.

### Neutral

* The MCP surface, GraphQL, the ingestion envelope and the SDKs are separate contracts with their own versioning (`01-contract-strategy.md` §3). This ADR changes none of them.
* The measured numbers are dated 2026-09-20. The convention documents' callouts, the OpenAPI baseline and the event catalog gate are where they are kept current.

## Alternatives considered

| Option | Why not |
|---|---|
| Build the targets now | Three of the seven are breaking (cursor-only removes `offset` and `total`; the envelope replaces every handled error body; publish-time validation refuses payloads that publish today), each needing a new major version and a change to the UI that reads today's shapes. The cost is certain and immediate. The benefit accrues to consumers who do not yet exist, and the revisit trigger says when they do. |
| Adopt the four additive ones now (`Idempotency-Key`, `X-RateLimit-Reset`, `Deprecation` and `Sunset`, `event_version`) | Each is compatible under §2 and each is the natural first step when the trigger fires. Shipping one is still a promise: `Sunset` states a removal date the platform then owes, and `Idempotency-Key` states a retry guarantee that needs the per-tenant, per-endpoint key store with 24-hour retention that `02-api-conventions.md` §6 describes and that does not exist. Half a convention promises a guarantee the platform does not keep. |
| Adopt the targets for new routes only | Two conventions in one API. The convention document's own argument is that consistency is what makes the endpoints learnable, and a consumer would have to learn which routes follow which. |
| Delete the target text from the convention documents | It records where the design was heading, and the trigger below says when to go there. Deleting it discards the design work. It stays, beside its callouts. |
| Leave it as it is: callouts only, no ADR | Nothing then binds the two readings together. R11-AUD06 stays a TODO with no decision behind it, and a reviewer can cite the target text as the rule. Deciding it is what this register is for. |

## Revisit trigger

Revisit when any one of these happens:

* The first external SDK consumer of the REST API is planned or named. That is the point at which the shape becomes a promise to someone outside this repository, and it is the tracker's own exit condition for R11-AUD06.
* A regulator or bank customer requires idempotent retries for creates.
* A regulator or bank customer requires a stable error envelope: stable codes, a `retryable` flag and a correlation id in the body.

## Implementation status (2026-09-20)

**Nothing to implement: this records the current contract.** Every figure in the Context table was re-derived on 2026-09-20 from `Docs/90-reference/openapi-baseline.json` (508 operations across 440 paths, 263 of them `GET`; 131 with `offset`, 8 with `cursor`, none with an `Idempotency-Key` header, 3 `deprecated`) and from the code the table names. The event counts (129 literal event types, 10 without `.v1`, 13 computed call sites) come from the scan that `tests/test_event_catalog_gate.py` runs. If this is accepted, the follow-up is to point the two convention documents at it beside their callouts and let R11-AUD06 close on it.
