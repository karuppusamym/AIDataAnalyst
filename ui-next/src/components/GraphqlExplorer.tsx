import { useMemo, useState } from "react";

import { ApiError, executeGraphQL } from "../lib/api";
import { USE_FIXTURES } from "../lib/appConfig";
import { Button, Field, Pill } from "./primitives";

interface Example {
  readonly id: string;
  readonly label: string;
  readonly operationName: string;
  readonly query: string;
  variables(projectId: string | null): Record<string, unknown>;
}

const EXAMPLES: readonly Example[] = [
  {
    id: "datasources",
    label: "List data sources",
    operationName: "ListDataSources",
    query: `query ListDataSources($first: Int!) {
  datasources(first: $first) {
    totalCount
    nodes { id name connectorType dialect environment status }
    pageInfo { hasNextPage endCursor }
  }
}`,
    variables: () => ({ first: 20 }),
  },
  {
    id: "context-products",
    label: "List context products",
    operationName: "ListContextProducts",
    query: `query ListContextProducts($projectId: ID!, $first: Int!) {
  contextProducts(projectId: $projectId, askable: true, first: $first) {
    totalCount
    nodes {
      id productKey lifecycleStatus
      latestVersion { id version status name description }
    }
    pageInfo { hasNextPage endCursor }
  }
}`,
    variables: (projectId) => ({ projectId: projectId ?? "PROJECT_ID", first: 20 }),
  },
  {
    id: "lineage-impact",
    label: "Trace lineage impact",
    operationName: "TraceLineageImpact",
    query: `query TraceLineageImpact($datasourceId: ID!, $nodeId: String!) {
  lineageImpact(datasourceId: $datasourceId, nodeId: $nodeId, depth: 5, nodeLimit: 200) {
    focusNodeId focusNodeKind focusLabel upstreamTruncated downstreamTruncated
    upstream(first: 20) { nodes { nodeId nodeKind qualifiedName depth } pageInfo { hasNextPage endCursor } }
    downstream(first: 20) { nodes { nodeId nodeKind qualifiedName depth } pageInfo { hasNextPage endCursor } }
  }
}`,
    variables: () => ({ datasourceId: "DATASOURCE_ID", nodeId: "TABLE_OR_ROUTINE_NODE_ID" }),
  },
  {
    id: "execute-tool",
    label: "Execute governed tool",
    operationName: "ExecuteGovernedTool",
    query: `mutation ExecuteGovernedTool($request: ExecuteGovernedToolInput!) {
  executeGovernedTool(request: $request) {
    receipt { id status toolVersionId rowCount createdAt }
    result { columns rows maskedColumns appliedRowLimit }
    replayed
  }
}`,
    variables: () => ({
      request: {
        toolVersionId: "PUBLISHED_TOOL_VERSION_ID",
        idempotencyKey: "REPLACE_WITH_A_UNIQUE_KEY",
        parameters: {},
        maxRows: 100,
      },
    }),
  },
];

function variablesText(example: Example, projectId: string | null): string {
  return JSON.stringify(example.variables(projectId), null, 2);
}

function parseVariables(value: string): { value: Record<string, unknown> | null; error: string | null } {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { value: null, error: "Variables must be a JSON object." };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch (error) {
    return { value: null, error: `Variables are not valid JSON: ${(error as Error).message}` };
  }
}

function isMutation(query: string): boolean {
  return /^\s*(?:#[^\n]*\n\s*)*mutation\b/i.test(query);
}

export function GraphqlExplorer({ projectId }: { projectId: string | null }) {
  const [selectedId, setSelectedId] = useState(EXAMPLES[0].id);
  const [operationName, setOperationName] = useState(EXAMPLES[0].operationName);
  const [query, setQuery] = useState(EXAMPLES[0].query);
  const [variables, setVariables] = useState(variablesText(EXAMPLES[0], projectId));
  const [confirmedMutation, setConfirmedMutation] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);

  const parsedVariables = useMemo(() => parseVariables(variables), [variables]);
  const mutation = isMutation(query);

  const selectExample = (id: string) => {
    const example = EXAMPLES.find((candidate) => candidate.id === id) ?? EXAMPLES[0];
    setSelectedId(example.id);
    setOperationName(example.operationName);
    setQuery(example.query);
    setVariables(variablesText(example, projectId));
    setConfirmedMutation(false);
    setResult(null);
    setError(null);
  };

  const run = async () => {
    if (!parsedVariables.value || parsedVariables.error || (mutation && !confirmedMutation)) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const response = await executeGraphQL({ query, operationName, variables: parsedVariables.value });
      setResult(response);
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.detail : (caught as Error).message;
      setError(detail || "The GraphQL request failed.");
    } finally {
      setRunning(false);
    }
  };

  const requestBody = JSON.stringify(
    { query, operationName, variables: parsedVariables.value ?? {} },
    null,
    2,
  );
  const fetchExample = `const response = await fetch("/graphql", {
  method: "POST",
  credentials: "same-origin",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(${requestBody}),
});
const result = await response.json();`;
  const pythonExample = `import json
import requests

payload = json.loads(${JSON.stringify(requestBody)})
response = requests.post(
    "https://ATLAS_HOST/graphql",
    headers={"Authorization": "Bearer " + access_token},
    json=payload,
    timeout=30,
)
response.raise_for_status()
result = response.json()`;

  return (
    <section className="agcard aggraphql" aria-labelledby="graphql-explorer-heading">
      <div className="agcard__head">
        <div>
          <h2 id="graphql-explorer-heading" className="agcard__h2">GraphQL query explorer</h2>
          <p className="agcard__lede">
            Run one named, bounded operation against <code>POST /graphql</code>. The endpoint uses
            this session&apos;s identity and applies the same tenant, workspace, policy, paging, and
            execution controls as REST.
          </p>
        </div>
        <Pill tone="info">Introspection disabled</Pill>
      </div>
      <p className="agcard__note">
        Schema discovery is intentionally unavailable at runtime. Use the versioned published
        schema and these examples; requests containing <code>__schema</code> or <code>__type</code>
        are refused before resolver work starts.
      </p>

      <div className="aggraphql__toolbar">
        <Field label="Example">
          <select value={selectedId} onChange={(event) => selectExample(event.target.value)}>
            {EXAMPLES.map((example) => <option key={example.id} value={example.id}>{example.label}</option>)}
          </select>
        </Field>
        <Field label="Operation name">
          <input value={operationName} onChange={(event) => setOperationName(event.target.value)} />
        </Field>
      </div>

      <div className="aggraphql__editors">
        <Field label="Operation">
          <textarea
            aria-label="GraphQL operation"
            value={query}
            maxLength={32768}
            spellCheck={false}
            onChange={(event) => {
              setQuery(event.target.value);
              setConfirmedMutation(false);
            }}
          />
        </Field>
        <Field label="Variables (JSON)">
          <textarea
            aria-label="GraphQL variables"
            value={variables}
            spellCheck={false}
            onChange={(event) => setVariables(event.target.value)}
          />
        </Field>
      </div>

      {parsedVariables.error ? <p className="agform__error" role="alert">{parsedVariables.error}</p> : null}
      {mutation ? (
        <label className="aggraphql__confirm">
          <input
            type="checkbox"
            checked={confirmedMutation}
            onChange={(event) => setConfirmedMutation(event.target.checked)}
          />
          Execute this governed tool mutation. It can read source data and records an audit event.
        </label>
      ) : null}
      <div className="aggraphql__actions">
        <Button
          variant="primary"
          onClick={() => void run()}
          disabled={
            USE_FIXTURES || running || !operationName.trim() || Boolean(parsedVariables.error) ||
            (mutation && !confirmedMutation)
          }
        >
          {running ? "Running…" : "Run operation"}
        </Button>
        {USE_FIXTURES ? <span className="agcard__note">Available when connected to a live API.</span> : null}
      </div>

      {error ? <p className="agform__error" role="alert">{error}</p> : null}
      {result !== null ? (
        <div className="aggraphql__result" aria-live="polite">
          <span className="agcopy__label">Response</span>
          <pre>{JSON.stringify(result, null, 2)}</pre>
        </div>
      ) : null}

      <details className="aggraphql__sdk">
        <summary>SDK request examples</summary>
        <div className="aggraphql__sdkgrid">
          <div><span className="agcopy__label">TypeScript / browser</span><pre>{fetchExample}</pre></div>
          <div><span className="agcopy__label">Python / service</span><pre>{pythonExample}</pre></div>
        </div>
      </details>
    </section>
  );
}
