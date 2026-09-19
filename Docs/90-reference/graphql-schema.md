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
- Two queries record the read, as their REST routes do: `contextProductVersion`
  (a consumer's consumption, audit and outbox event, channel `GRAPHQL`) and
  `contextProductCoverage` (decided as the compile route decides; an audit event,
  and for a PUBLISHED version a consumption on channel `GRAPHQL_COVERAGE`).
- One named operation per request (`operationName` is required); HTTP
  batching and multi-operation documents are refused.
- Cursors are the same opaque keyset cursors the REST list routes return.
- Worked, tested operations for agents and SDKs:
  [graphql-examples.md](graphql-examples.md).

## Demand limits

A limit checked before execution refuses the document before any resolver
runs, so a refused document costs its parse and nothing else. Each limit is a
setting (`AIDA_` + the name, upper-cased), validated between a floor and the
ceiling shown; strawberry's backstop limiters sit at the ceilings, so raising a
limit is never undone by a backstop.

| Limit | Default | Ceiling | Setting | Checked | Code |
|---|---|---|---|---|---|
| Request body | 32768 bytes | 1048576 | `graphql_max_request_bytes` | before execution | `REQUEST_TOO_LARGE` |
| Document tokens | 2000 | 20000 | `graphql_max_tokens` | before execution | `DOCUMENT_TOO_LARGE` |
| Field depth | 6 | 12 | `graphql_max_depth` | before execution | `DEPTH_LIMIT_EXCEEDED` |
| Aliases (fragments expanded) | 50 | 200 | `graphql_max_aliases` | before execution | `ALIAS_LIMIT_EXCEEDED` |
| Page size (`first`) | 1 to 100 | 500 | `graphql_max_page_size` | before execution | `PAGE_SIZE_EXCEEDED` |
| Returned objects (an upper bound, estimated) | 500 | 10000 | `graphql_max_nodes` | before execution | `NODE_BUDGET_EXCEEDED` |
| String argument or variable | 512 characters | 4096 | `graphql_max_string_argument_length` | before execution | `ARGUMENT_TOO_LONG` |
| Selections visited while measuring | 5000 | 50000 | `graphql_max_selection_visits` | before execution | `DOCUMENT_TOO_COMPLEX` |
| Introspection | disabled | see below | `graphql_introspection_enabled` | before execution | `INTROSPECTION_DISABLED` / `INTROSPECTION_FORBIDDEN` |
| Datasources an organization-wide `tables` listing may decide | 200 | 5000 | `graphql_max_scope_datasources` | while resolving | `SCOPE_TOO_BROAD` |
| Resolver deadline | 10 seconds | 120 | `graphql_deadline_seconds` | while resolving | `DEADLINE_EXCEEDED` |
| Execution mutation row limit (`maxRows`, required) | 1 to 1000 | 10000 | `graphql_max_execution_rows` | while resolving | `INVALID_ARGUMENT` |
| Execution mutation deadline | the gateway's statement timeout plus 15 seconds | - | `query_timeout_seconds` | while resolving | `DEADLINE_EXCEEDED` |
| Response body, and returned objects counted | 1048576 bytes | 16777216 | `graphql_max_response_bytes` | after execution | `RESPONSE_TOO_LARGE` |

## Introspection

Off by default in every environment: clients discover the schema from the
published SDL beside this page. With `graphql_introspection_enabled` on, it is
served in development, test and staging to `AgentDeveloper` and `PlatformAdmin` only;
anyone else is refused `INTROSPECTION_FORBIDDEN`, and production refuses the
setting at startup. An admitted `__schema`/`__type` answer is not counted
against the returned-object budget (it is the schema, not data); the response
byte ceiling still applies. Hiding the schema is not authorization: every
field is decided on its own either way.

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
| `INTROSPECTION_FORBIDDEN` | 403 |
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
| `NOT_FOUND` | no such object (the REST route answers 404); reason COVERAGE_NOT_MEASURED when the routine or trigger exists but no parse has measured it |
| `GONE` | the context product version was retired and this caller read it before; re-pin to the current published version (the REST route answers 410) |
| `INVALID_ARGUMENT` | an argument is out of range; `extensions.reason` says which rule |
| `INVALID_CURSOR` | `after` is not a cursor this field issued |
| `SCOPE_TOO_BROAD` | an organization-wide listing spans too many datasources; name one |
| `VALIDATION_FAILED` | a variable did not coerce to its declared type |
| `CONFLICT` | R11-GQL02: the idempotency key was already used with different inputs, or the tool cannot run now (a quality hold, an unpublished version); for `contextProductCoverage`, the version names a table, routine or ontology version that no longer resolves (the REST route answers 409); `extensions.reason` says which |
| `REJECTED` | R11-GQL02: the gateway or parameter binding refused the execution |
| `EXECUTION_FAILED` | R11-GQL02: the source failed the execution; the receipt says so |
| `INTERNAL_ERROR` | an unexpected failure; the message is withheld, the correlation id is not |
| `DEADLINE_EXCEEDED` | execution passed the resolver deadline; no data is returned |
| `RESPONSE_TOO_LARGE` | the response passed the byte or object budget; no data is returned |
