import { postJson } from "./transport";

export interface GraphQLRequest {
  readonly query: string;
  readonly operationName: string;
  readonly variables: Readonly<Record<string, unknown>>;
}

export interface GraphQLErrorPayload {
  readonly message: string;
  readonly path?: readonly (string | number)[];
  readonly extensions?: Readonly<Record<string, unknown>>;
}

export interface GraphQLResponse {
  readonly data?: unknown;
  readonly errors?: readonly GraphQLErrorPayload[];
}

/** Run one named operation through Atlas's authenticated GraphQL endpoint. */
export function executeGraphQL(
  request: GraphQLRequest,
  signal?: AbortSignal,
): Promise<GraphQLResponse> {
  return postJson<GraphQLResponse>("/graphql", request, signal);
}
