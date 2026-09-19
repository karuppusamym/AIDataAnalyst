# Metadata GraphQL reference

**Generated file. Do not edit by hand.** Regenerate with
`python scripts/generate_graphql_schema.py`; `--check` fails when this page or
the SDL beside it is stale, or when a breaking schema change keeps its version.

- Endpoint: `POST /graphql`. Schema version: **1**.
- The SDL is [graphql-schema.graphql](graphql-schema.graphql).
- `query` operations read catalog metadata and never execute against a source.
- One `mutation`, `executeGovernedTool` (R11-GQL02), runs an approved tool version
  through the same governed path as `POST /v1/tool-versions/{version_id}/execute`,
  once per caller-scoped `idempotencyKey`: the same key with the same inputs returns
  the first request's receipt and executes nothing (`replayed: true`), the same key
  with other inputs is `CONFLICT`, and an outcome the platform did not learn stays
  `PENDING` rather than being retried. A mutation selects exactly one execution field
  (`EXECUTION_ROOT_INVALID` otherwise). Rows appear only in that response; the
  `governedExecution` query returns the receipt, which never carries rows.
- Authenticated and role-gated like the REST catalog reads. Each field is
  decided exactly as the REST route it answers for decides it -- the mapping
  is in the docstring of `aida.graphql_reads`.
- One named operation per request (`operationName` is required); HTTP
  batching and multi-operation documents are refused.
- Cursors are the same opaque keyset cursors the REST list routes return.

## Demand limits

A limit checked before execution refuses the document before any resolver
runs, so a refused document costs its parse and nothing else.

| Limit | Value | Checked | Code |
|---|---|---|---|
| Request body | 32768 bytes | before execution | `REQUEST_TOO_LARGE` |
| Document tokens | 2000 | before execution | `DOCUMENT_TOO_LARGE` |
| Field depth | 6 | before execution | `DEPTH_LIMIT_EXCEEDED` |
| Aliases (fragments expanded) | 50 | before execution | `ALIAS_LIMIT_EXCEEDED` |
| Page size (`first`) | 1 to 100 | before execution | `PAGE_SIZE_EXCEEDED` |
| Returned objects (an upper bound, estimated) | 500 | before execution | `NODE_BUDGET_EXCEEDED` |
| String argument or variable | 512 characters | before execution | `ARGUMENT_TOO_LONG` |
| Selections visited while measuring | 5000 | before execution | `DOCUMENT_TOO_COMPLEX` |
| Introspection | disabled | before execution | `INTROSPECTION_DISABLED` |
| Datasources an organization-wide `tables` listing may decide | 200 | while resolving | `SCOPE_TOO_BROAD` |
| Resolver deadline | 10 seconds | while resolving | `DEADLINE_EXCEEDED` |
| Execution mutation row limit (`maxRows`, required) | 1 to 1000 | while resolving | `INVALID_ARGUMENT` |
| Execution mutation deadline | the gateway's statement timeout plus 15 seconds | while resolving | `DEADLINE_EXCEEDED` |
| Response body, and returned objects counted | 1048576 bytes | after execution | `RESPONSE_TOO_LARGE` |

## Refusal codes (document refused, no `data`)

| Code | HTTP status |
|---|---|
| `REQUEST_TOO_LARGE` | 413 |
| `REQUEST_INVALID` | 400 |
| `BATCHING_NOT_SUPPORTED` | 400 |
| `OPERATION_NAME_REQUIRED` | 400 |
| `DOCUMENT_TOO_LARGE` | 400 |
| `DOCUMENT_INVALID` | 400 |
| `MULTIPLE_OPERATIONS` | 400 |
| `OPERATION_NOT_FOUND` | 400 |
| `OPERATION_NOT_SUPPORTED` | 400 |
| `EXECUTION_ROOT_INVALID` | 400 |
| `RATE_LIMITED` | 429 |
| `FRAGMENT_CYCLE` | 400 |
| `INTROSPECTION_DISABLED` | 400 |
| `DEPTH_LIMIT_EXCEEDED` | 400 |
| `ALIAS_LIMIT_EXCEEDED` | 400 |
| `PAGE_SIZE_EXCEEDED` | 400 |
| `ARGUMENT_TOO_LONG` | 400 |
| `NODE_BUDGET_EXCEEDED` | 400 |
| `DOCUMENT_TOO_COMPLEX` | 400 |
| `VALIDATION_FAILED` | 400 |

## Execution codes (HTTP 200, in `errors[].extensions.code`)

Field-level codes null only the field that raised them. Messages are the
code itself: never SQL, a credential or an object name.

| Code | Meaning |
|---|---|
| `FORBIDDEN` | the caller may not read this object; `extensions.reason` carries the value-free reason code the equivalent REST route puts in its 403 |
| `NOT_FOUND` | no such object (the REST route answers 404) |
| `GONE` | the context product version was retired and this caller read it before; re-pin to the current published version (the REST route answers 410) |
| `INVALID_ARGUMENT` | an argument is out of range; `extensions.reason` says which rule |
| `INVALID_CURSOR` | `after` is not a cursor this field issued |
| `SCOPE_TOO_BROAD` | an organization-wide listing spans too many datasources; name one |
| `VALIDATION_FAILED` | a variable did not coerce to its declared type |
| `CONFLICT` | R11-GQL02: the idempotency key was already used with different inputs, or the tool cannot run now (a quality hold, an unpublished version); `extensions.reason` says which |
| `REJECTED` | R11-GQL02: the gateway or parameter binding refused the execution |
| `EXECUTION_FAILED` | R11-GQL02: the source failed the execution; the receipt says so |
| `INTERNAL_ERROR` | an unexpected failure; the message is withheld, the correlation id is not |
| `DEADLINE_EXCEEDED` | execution passed the resolver deadline; no data is returned |
| `RESPONSE_TOO_LARGE` | the response passed the byte or object budget; no data is returned |
