# Metadata GraphQL: Agent Gateway examples

Worked operations for agents and SDK clients of `POST /graphql` (R11-GQL01, R11-GQL02).
The schema is [graphql-schema.graphql](graphql-schema.graphql) and the limits, codes and
introspection policy are in [graphql-schema.md](graphql-schema.md).

**These examples are tested.** `tests/test_graphql_examples.py` reads this page, admits every
operation below against the served schema at the default limits, and runs each one, in page
order, through the real endpoint against a seeded estate with the roles shown. An example that
stops validating, stops being admitted, or starts returning an error fails that test, so what
you copy from here works.

## How to call it

Every request is one JSON object with a `query` holding exactly one named operation, its
`operationName`, and `variables`. There is no batching, no persisted query and no
subscription. The caller is authenticated like any REST call; each field then decides for
itself, exactly as the REST route it answers for.

```python
import httpx

response = httpx.post(
    "https://atlas.example/graphql",
    headers={"Authorization": "Bearer <token>"},
    json={
        "operationName": "CatalogOverview",
        "query": CATALOG_OVERVIEW,  # the document from the example below
        "variables": {"first": 5},
    },
)
body = response.json()
for error in body.get("errors", []):
    # `message` is the stable code; `extensions.reason` is value-free.
    print(error["extensions"]["code"], error["extensions"].get("reason"))
print(body["extensions"]["cost"])  # depth, aliases, estimatedNodes, returnedObjects
```

What an agent should rely on:

- **Refusals are per field.** A field the caller may not read is `null` with an error whose
  `extensions.code` is stable (`FORBIDDEN`, `NOT_FOUND`, ...) and whose message is that code.
  Siblings still answer.
- **A document is priced before it runs.** Every list is a connection; its `first` multiplies
  everything beneath its `nodes`, and a document that could return more than the object budget
  is refused `NODE_BUDGET_EXCEEDED` before any statement. `extensions.cost.estimatedNodes` is
  the price the admission check computed.
- **Cursors are opaque.** Pass `pageInfo.endCursor` as `after`; `totalCount` is computed on the
  first page only.
- **Queries never execute against a source.** The one mutation, `executeGovernedTool`, does, once
  per caller-scoped `idempotencyKey`.

Placeholders in `<angle-brackets>` stand for identifiers from your own estate; the test fills
them from its seeded one.

## Catalog

### CatalogOverview

The caller's datasources, each with a page of its tables -- `GET /v1/organizations/{id}/
datasources` and `GET /v1/datasources/{id}/tables`, decided the same way (a datasource whose
tables the caller may not read answers its `tables` with `FORBIDDEN`). Priced before it runs at
`2 + first * (2 + 20)` objects -- 112 at `first: 5`, against a budget of 500 -- so keep pages
small, or page. Columns are one level further down than the default depth of 6 allows from
here; read them per table, as the next example does.

**Caller roles:** `Analyst`

```graphql
query CatalogOverview($first: Int!) {
  datasources(first: $first) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      dialect
      tables(first: 20) {
        totalCount
        nodes { id name objectType status }
      }
    }
  }
}
```

```json
{"first": 5}
```

### TableWithMeaning

One table by id with its approved (maker-checker reviewed) description, its columns' approved
descriptions and its keys. A foreign key's referenced table is authorized on its own: a table
in a datasource the caller may not read comes back `null` with `FORBIDDEN`, never its name.

**Caller roles:** `Analyst`

```graphql
query TableWithMeaning($tableId: ID!) {
  table(id: $tableId) {
    name
    objectType
    description { readme readmeVersion approvedBy approvedAt }
    columns(first: 20) {
      nodes { name physicalType businessDescription { description version } }
    }
    constraints(first: 10) {
      nodes { name constraintType columns referencedTable { id name } }
    }
  }
}
```

```json
{"tableId": "<table-id>"}
```

## Lineage

### LineageImpact

What a table depends on and what depends on it, across every merged edge kind, bounded by
`depth` and `nodeLimit` exactly as `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}`
bounds it. A catalog table's node id is its table id.

**Caller roles:** `Analyst`

```graphql
query LineageImpact($datasourceId: ID!, $nodeId: String!) {
  lineageImpact(datasourceId: $datasourceId, nodeId: $nodeId, depth: 3, nodeLimit: 100) {
    focusLabel
    upstreamTruncated
    downstreamTruncated
    upstream(first: 20) { totalCount nodes { nodeId label depth qualityState } }
    downstream(first: 20) { totalCount nodes { nodeId label depth qualityState } }
  }
}
```

```json
{"datasourceId": "<datasource-id>", "nodeId": "<table-id>"}
```

### LineageGraph

A datasource's merged lineage graph, a page of nodes and edges at a time. `truncated` and
`truncationReasons` say whether the route's own bounds cut anything.

**Caller roles:** `Analyst`

```graphql
query LineageGraph($datasourceId: ID!, $after: String) {
  lineageGraph(datasourceId: $datasourceId, nodeLimit: 300, edgeLimit: 1500) {
    returnedNodeCount
    returnedEdgeCount
    truncated
    truncationReasons
    nodes(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id nodeKind label qualifiedName }
    }
    edges(first: 50) { nodes { edgeSource sourceNodeId targetNodeId status confidence } }
  }
}
```

```json
{"datasourceId": "<datasource-id>", "after": null}
```

## Context products

### AskableContextProducts

The published products the caller could ask through (`askable: true`) -- for a contracted
agent, only those its capability envelope names.

**Caller roles:** `Analyst`

```graphql
query AskableContextProducts($projectId: ID!) {
  contextProducts(projectId: $projectId, askable: true, first: 20) {
    totalCount
    nodes {
      productKey
      lifecycleStatus
      latestVersion { id version status name purpose tableIds eligibleToolVersionIds }
    }
  }
}
```

```json
{"projectId": "<project-id>"}
```

### ReadContextProductVersion

The governed version read: envelope, consumer role, purpose and quality decide, and a
consumer's read is recorded as a consumption on channel `GRAPHQL`. A version retired after this
caller read it answers `GONE`: re-pin to the current published version.

**Caller roles:** `Analyst`

```graphql
query ReadContextProductVersion($versionId: ID!) {
  contextProductVersion(id: $versionId) {
    productKey
    version
    status
    fingerprint
    tableIds
    routineIds
    eligibleToolVersionIds
    allowedConsumerRoles
    supportWindowEndsAt
  }
}
```

```json
{"versionId": "<context-product-version-id>"}
```

## Coverage

### ContextProductCoverage

What a version stands on and how completely Atlas understands it -- the compiled product's
`coverage` section and the source freshness beside it, decided as
`GET /v1/context-product-versions/{id}/compile` decides. Recorded as a read (an audit event,
and for a PUBLISHED version a consumption on channel `GRAPHQL_COVERAGE`). A non-empty
`changedSincePublished` is the product saying it is stale.

**Caller roles:** `Analyst`

```graphql
query ContextProductCoverage($versionId: ID!) {
  contextProductCoverage(versionId: $versionId) {
    productKey
    version
    routines(first: 20) {
      nodes { qualifiedName lineage fullyParsed definitionAvailable readsTableIds descriptionState }
    }
    views(first: 20) { nodes { tableId objectType definitionAvailable lineage } }
    meaning(first: 20) { nodes { kind key version current } }
    changedSincePublished(first: 20) { totalCount nodes { subjectKind subjectId change } }
    sourceFreshness(first: 20) { nodes { datasourceId lastScanCompletedAt lastFullScanCompletedAt } }
  }
}
```

```json
{"versionId": "<context-product-version-id>"}
```

### RoutineParseCoverage

How completely one routine's body was parsed, as last measured. `NOT_FOUND` with reason
`COVERAGE_NOT_MEASURED` means no parse has measured it -- which is not "fully understood".

**Caller roles:** `Analyst`

```graphql
query RoutineParseCoverage($datasourceId: ID!, $routineId: ID!) {
  routineParseCoverage(datasourceId: $datasourceId, routineId: $routineId) {
    state
    parseCompleted
    statementCount
    unparsedStatementCount
    unparsedReasonCodes
    parsedAt
  }
}
```

```json
{"datasourceId": "<datasource-id>", "routineId": "<routine-id>"}
```

## Governed execution

### ExecuteGovernedTool

Run one published tool version, once, through the same governed path as
`POST /v1/tool-versions/{version_id}/execute`. `maxRows` is required. Choose the
`idempotencyKey` yourself and reuse it on a retry: the same key with the same inputs returns
the first receipt with `replayed: true` and executes nothing; the same key with other inputs is
`CONFLICT`. Rows appear in this response only. If the response is lost, read the receipt
instead of executing again.

**Caller roles:** `Analyst`

```graphql
mutation ExecuteGovernedTool($request: ExecuteGovernedToolInput!) {
  executeGovernedTool(request: $request) {
    replayed
    qualityGateAction
    receipt { id status rowCount outcomeCode }
    result { columns rows maskedColumns appliedRowLimit }
  }
}
```

```json
{
  "request": {
    "toolVersionId": "<tool-version-id>",
    "idempotencyKey": "orders-lookup-2026-09-19-0001",
    "maxRows": 10,
    "parameters": {}
  }
}
```

### MyExecutionReceipts

The caller's own execution receipts, newest first (every caller's in the organization for
`PlatformAdmin` and `Auditor`). A receipt never carries rows. A `PENDING` receipt whose outcome
the platform never learned is settled from its recorded execution as it is read.

**Caller roles:** `Analyst`

```graphql
query MyExecutionReceipts {
  governedExecutions(first: 10) {
    totalCount
    nodes { id status toolVersionId rowCount outcomeCode createdAt completedAt }
  }
}
```

```json
{}
```
