/* ---------------------------------------------------------------------------
   Cross-source and cross-domain: the API surface `ui-next` never reached.

   Every lineage and relationship screen in this app is scoped to one
   datasource (`useDatasourcePicker`). The platform's cross-source capabilities
   have existed on the server the whole time, addressed by *data domain* rather
   than by datasource, and nothing here called them — `UnifiedLineageScreen`'s
   own header comment records the omission, and `DomainLineageGraphRead` has sat
   in `types.ts` unused since it was generated.

   What this file adds a client for:

     GET  /v1/data-domains/{id}/unified-lineage/graph          domain-wide graph
     GET  /v1/data-domains/{id}/cross-boundary-grants          who may see across
     POST /v1/data-domains/{id}/cross-boundary-grants          request access
     POST /v1/data-domains/{id}/relationship-candidates/discover-cross-source
     POST /v1/data-domains/{id}/cross-source-object-resolution-candidates/discover
     GET  /v1/datasources/{id}/cross-source-object-resolution-candidates
     POST /v1/cross-source-object-resolution-candidates/{id}/decision

   The governing rule, and the reason this is not just "swap the URL":
   ADR-0017 SS4 / INV-5 make cross-domain visibility deny-by-default and never
   inherited. A candidate reaching into another domain renders only when an
   ACTIVE `cross_boundary_grant` permits it; the server reports the domains it
   withheld in `withheld_cross_boundary_domain_ids` rather than dropping them
   silently, and this client surfaces that so the UI can offer the grant
   request instead of showing a quietly incomplete graph.

   Kept in its own append file, following `_api_append.ts`'s precedent, for the
   same reason: `get`/`postJson` are module-private in `api.ts`, and this slice
   reads better contiguous than scattered through a 4.5k-line file.
--------------------------------------------------------------------------- */

import { ApiError } from "./api";
import { getCurrentOrgId } from "./org";
import type {
  CrossBoundaryGrantCreate,
  CrossBoundaryGrantRead,
  DataDomainRead,
  DataSourceRead,
  DomainLineageGraphRead,
  LineOfBusinessRead,
} from "./types";
import type { PageOf } from "./ui-types";

const USE_FIXTURES = import.meta.env.VITE_USE_FIXTURES !== "0";
const DEV_PRINCIPAL_ID = import.meta.env.VITE_DEV_PRINCIPAL_ID || "local-ui-admin";
const DEV_ROLES =
  import.meta.env.VITE_DEV_ROLES ||
  "PlatformAdmin,OrganizationAdmin,ProjectAdmin,MetadataAdmin,MetadataIngestor,DataAdmin,SemanticAdmin,DataSteward,ToolDeveloper,ToolConsumer,AgentDeveloper,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer";

function identityHeaders(): Record<string, string> {
  if (USE_FIXTURES) return {};
  return {
    "X-Principal-Id": DEV_PRINCIPAL_ID,
    "X-Roles": DEV_ROLES,
    "X-Organization-Id": getCurrentOrgId(),
  };
}

async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    ...init,
    signal,
    headers: {
      Accept: "application/json",
      ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...identityHeaders(),
      ...(init.headers ?? {}),
    },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** `CrossSourceResolutionCandidateRead` -- `src/aida/schemas.py`.
 *
 *  The catalog-identity analogue of a relationship candidate: "these two
 *  tables in two different systems are the same real-world object", scored
 *  whole-table rather than column-to-key. */
export interface CrossSourceResolutionCandidateRead {
  id: string;
  organization_id: string;
  source_datasource_id: string;
  source_table_id: string;
  target_datasource_id: string;
  target_table_id: string;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CrossSourceDiscoveryRequest {
  max_candidates?: number;
  max_datasource_pairs?: number;
  /** Pair against a second domain instead of scanning within this one.
   *  Requires an ACTIVE cross_boundary_grant; the server answers 403 without
   *  one, which is the case the UI turns into a grant request. */
  target_data_domain_id?: string | null;
}

/** An empty page, for the per-item `catch` in the fan-out reads below: one
 *  domain or datasource the caller cannot read must not blank the whole view,
 *  it should just contribute nothing. */
function emptyPage<T>(): PageOf<T> {
  return { items: [], limit: 0, offset: 0, total: 0 };
}

const WRITE_NOTICE =
  "This runs on the server. Point the app at a live API (VITE_USE_FIXTURES=0) to use it.";

/* ---------------------------------------------------------------------------
   Fixtures

   Shaped to make the one thing this feature exists for visible without a
   backend: the fixture datasources already span two domains (`dom_fin` has
   three sources, `dom_retail` has one), so the domain graph below reports
   `dom_retail` as withheld for want of a grant. A fixture where everything
   resolved would hide exactly the state the UI most needs to render well.
--------------------------------------------------------------------------- */

const FIXTURE_DOMAINS: DataDomainRead[] = [
  {
    id: "dom_fin",
    organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_fin",
    parent_domain_id: null,
    name: "Finance",
    code: "FIN",
    is_default: false,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "dom_retail",
    organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_retail",
    parent_domain_id: null,
    name: "Retail",
    code: "RET",
    is_default: false,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

function fixtureDomainGraph(domainId: string): DomainLineageGraphRead {
  const nodes = [
    { id: "ds_snowflake_prod:t_customer", label: "customer_dim", qualified: "retail.customer_dim" },
    { id: "ds_snowflake_prod:t_account", label: "account_fact", qualified: "retail.account_fact" },
    { id: "ds_oracle_core:t_party", label: "party_master", qualified: "core.party_master" },
    { id: "ds_postgres_ops:t_ledger", label: "ledger_entry", qualified: "ops.ledger_entry" },
  ].map((n) => ({
    id: n.id,
    node_kind: "TABLE" as const,
    label: n.label,
    qualified_name: n.qualified,
    matched_table_id: null,
    resolved: true,
    inbound_edge_count: 1,
    outbound_edge_count: 1,
  }));
  return {
    data_domain_id: domainId,
    datasource_ids: ["ds_snowflake_prod", "ds_oracle_core", "ds_postgres_ops"],
    nodes,
    edges: [
      {
        id: "e1",
        edge_source: "FOREIGN_KEY",
        source_node_id: "ds_snowflake_prod:t_account",
        target_node_id: "ds_snowflake_prod:t_customer",
        source_label: "account_fact",
        target_label: "customer_dim",
        status: "ACTIVE",
        confidence: 1,
      },
      {
        // The edge that only exists because this view is domain-scoped: it
        // joins two different datasources.
        id: "e2",
        edge_source: "SUGGESTED_RELATIONSHIP",
        source_node_id: "ds_oracle_core:t_party",
        target_node_id: "ds_snowflake_prod:t_customer",
        source_label: "party_master",
        target_label: "customer_dim",
        status: "APPROVED",
        confidence: 0.91,
      },
      {
        id: "e3",
        edge_source: "SUGGESTED_RELATIONSHIP",
        source_node_id: "ds_postgres_ops:t_ledger",
        target_node_id: "ds_snowflake_prod:t_account",
        source_label: "ledger_entry",
        target_label: "account_fact",
        status: "APPROVED",
        confidence: 0.87,
      },
    ],
    counts_by_source: { FOREIGN_KEY: 1, SUGGESTED_RELATIONSHIP: 2 },
    returned_node_count: 4,
    returned_edge_count: 3,
    node_limit: 600,
    edge_limit: 3000,
    truncated: false,
    truncation_reasons: [],
    // The whole point: Retail has candidates reaching into this domain and no
    // grant covers them, so they are named rather than dropped.
    withheld_cross_boundary_domain_ids: domainId === "dom_fin" ? ["dom_retail"] : [],
  } as DomainLineageGraphRead;
}

const FIXTURE_GRANTS: CrossBoundaryGrantRead[] = [
  {
    id: "grant_pending_1",
    organization_id: "00000000-0000-0000-0000-000000000001",
    source_data_domain_id: "dom_retail",
    target_data_domain_id: "dom_fin",
    edge_kinds: ["SUGGESTED_RELATIONSHIP"],
    reason: "Finance needs party resolution against the retail customer master.",
    status: "PENDING_APPROVAL",
    requested_by: "steward@example.com",
    approved_by: null,
    approved_at: null,
    expires_at: null,
    created_at: "2026-09-01T09:00:00Z",
    updated_at: "2026-09-01T09:00:00Z",
  },
];

const FIXTURE_RESOLUTION_CANDIDATES: CrossSourceResolutionCandidateRead[] = [
  {
    id: "xsr_1",
    organization_id: "00000000-0000-0000-0000-000000000001",
    source_datasource_id: "ds_snowflake_prod",
    source_table_id: "t_customer",
    target_datasource_id: "ds_oracle_core",
    target_table_id: "t_party",
    detection_rule: "NAME_AND_COLUMN_OVERLAP",
    confidence: 0.88,
    evidence: { shared_column_count: 7, name_similarity: 0.72 },
    status: "PENDING",
    created_by: "discovery",
    reviewed_by: null,
    review_reason: null,
    reviewed_at: null,
    created_at: "2026-09-02T08:00:00Z",
    updated_at: "2026-09-02T08:00:00Z",
  },
  {
    id: "xsr_2",
    organization_id: "00000000-0000-0000-0000-000000000001",
    source_datasource_id: "ds_snowflake_prod",
    source_table_id: "t_account",
    target_datasource_id: "ds_postgres_ops",
    target_table_id: "t_ledger",
    detection_rule: "COLUMN_OVERLAP",
    confidence: 0.54,
    evidence: { shared_column_count: 3, name_similarity: 0.31 },
    status: "PENDING",
    created_by: "discovery",
    reviewed_by: null,
    review_reason: null,
    reviewed_at: null,
    created_at: "2026-09-02T08:00:00Z",
    updated_at: "2026-09-02T08:00:00Z",
  },
];

/* ------------------------------------------------------------------------ */

/** Every data domain in the organization.
 *
 *  Domains are listed per line of business, so this walks
 *  organization -> lines of business -> domains. Bounded: an organization has
 *  a handful of lines of business, and the alternative (an org-wide domain
 *  endpoint) does not exist on the server. */
export async function fetchOrgDataDomains(
  organizationId: string,
  signal?: AbortSignal,
): Promise<DataDomainRead[]> {
  if (USE_FIXTURES) return FIXTURE_DOMAINS;
  const lobs = await request<PageOf<LineOfBusinessRead>>(
    `/v1/organizations/${encodeURIComponent(organizationId)}/lines-of-business?limit=500`,
    {},
    signal,
  );
  const pages = await Promise.all(
    (lobs.items ?? []).map((lob: LineOfBusinessRead) =>
      request<PageOf<DataDomainRead>>(
        `/v1/lines-of-business/${encodeURIComponent(lob.id)}/data-domains?limit=500`,
        {},
        signal,
      ).catch(() => emptyPage<DataDomainRead>()),
    ),
  );
  const seen = new Set<string>();
  const domains: DataDomainRead[] = [];
  for (const page of pages) {
    for (const domain of page.items ?? []) {
      if (seen.has(domain.id)) continue;
      seen.add(domain.id);
      domains.push(domain);
    }
  }
  return domains.sort((a, b) => a.name.localeCompare(b.name));
}

/** Which domains actually have datasources.
 *
 *  A picker listing every domain in the organization would mostly offer empty
 *  graphs. Derived from the datasource list the app has already loaded, so it
 *  costs nothing extra. */
export function domainsWithDatasources(
  domains: readonly DataDomainRead[],
  datasources: readonly DataSourceRead[],
): DataDomainRead[] {
  const populated = new Set(datasources.map((d) => d.data_domain_id));
  return domains.filter((domain) => populated.has(domain.id));
}

/** The merged graph across every datasource in one domain.
 *
 *  Same node/edge shape as the single-datasource graph, plus
 *  `withheld_cross_boundary_domain_ids` -- the domains that have candidates
 *  reaching in here but no grant covering them. */
export async function fetchDomainLineageGraph(
  domainId: string,
  options: { nodeLimit?: number; edgeLimit?: number; suggestionStatus?: string } = {},
  signal?: AbortSignal,
): Promise<DomainLineageGraphRead> {
  if (USE_FIXTURES) return fixtureDomainGraph(domainId);
  const params = new URLSearchParams({
    node_limit: String(options.nodeLimit ?? 600),
    edge_limit: String(options.edgeLimit ?? 3000),
    suggestion_status: options.suggestionStatus ?? "APPROVED",
  });
  return request<DomainLineageGraphRead>(
    `/v1/data-domains/${encodeURIComponent(domainId)}/unified-lineage/graph?${params}`,
    {},
    signal,
  );
}

/** Grants where this domain is either the source (owning) or target (asking)
 *  side. Both directions matter to a steward looking at one domain. */
export async function fetchCrossBoundaryGrants(
  domainId: string,
  signal?: AbortSignal,
): Promise<CrossBoundaryGrantRead[]> {
  if (USE_FIXTURES) {
    return FIXTURE_GRANTS.filter(
      (g) => g.source_data_domain_id === domainId || g.target_data_domain_id === domainId,
    );
  }
  const page = await request<PageOf<CrossBoundaryGrantRead>>(
    `/v1/data-domains/${encodeURIComponent(domainId)}/cross-boundary-grants?limit=200`,
    {},
    signal,
  );
  return page.items ?? [];
}

/** Ask for permission for `body.target_data_domain_id` to see into
 *  `sourceDomainId`.
 *
 *  Creates the grant PENDING_APPROVAL and files a `CROSS_BOUNDARY_GRANT`
 *  governance review. It becomes ACTIVE only when a *different* principal
 *  approves that review, which happens on the Review queue screen -- there is
 *  deliberately no approve button here. */
export async function requestCrossBoundaryGrant(
  sourceDomainId: string,
  body: CrossBoundaryGrantCreate,
  signal?: AbortSignal,
): Promise<CrossBoundaryGrantRead> {
  if (USE_FIXTURES) throw new Error(WRITE_NOTICE);
  return request<CrossBoundaryGrantRead>(
    `/v1/data-domains/${encodeURIComponent(sourceDomainId)}/cross-boundary-grants`,
    { method: "POST", body: JSON.stringify(body) },
    signal,
  );
}

/** Infer column relationships across the datasources in one domain, or across
 *  the boundary into a second domain when `target_data_domain_id` is set. */
export async function discoverCrossSourceRelationships(
  domainId: string,
  body: CrossSourceDiscoveryRequest = {},
  signal?: AbortSignal,
): Promise<number> {
  if (USE_FIXTURES) throw new Error(WRITE_NOTICE);
  const page = await request<PageOf<unknown>>(
    `/v1/data-domains/${encodeURIComponent(domainId)}/relationship-candidates/discover-cross-source`,
    { method: "POST", body: JSON.stringify(body) },
    signal,
  );
  return page.items?.length ?? 0;
}

/** The catalog-identity counterpart: "are these two tables the same object?" */
export async function discoverCrossSourceObjectResolutions(
  domainId: string,
  body: CrossSourceDiscoveryRequest = {},
  signal?: AbortSignal,
): Promise<number> {
  if (USE_FIXTURES) throw new Error(WRITE_NOTICE);
  const page = await request<PageOf<unknown>>(
    `/v1/data-domains/${encodeURIComponent(domainId)}/cross-source-object-resolution-candidates/discover`,
    { method: "POST", body: JSON.stringify(body) },
    signal,
  );
  return page.items?.length ?? 0;
}

/** Resolution candidates are listed per datasource, not per domain -- so a
 *  domain-scoped view fans out over the domain's datasources. */
export async function fetchCrossSourceResolutionCandidates(
  datasourceIds: readonly string[],
  candidateStatus: string | null,
  signal?: AbortSignal,
): Promise<CrossSourceResolutionCandidateRead[]> {
  if (USE_FIXTURES) {
    return FIXTURE_RESOLUTION_CANDIDATES.filter(
      (c) =>
        datasourceIds.includes(c.source_datasource_id) &&
        (!candidateStatus || c.status === candidateStatus),
    );
  }
  const pages = await Promise.all(
    datasourceIds.map((id) => {
      const params = new URLSearchParams({ limit: "200" });
      if (candidateStatus) params.set("candidate_status", candidateStatus);
      return request<PageOf<CrossSourceResolutionCandidateRead>>(
        `/v1/datasources/${encodeURIComponent(id)}/cross-source-object-resolution-candidates?${params}`,
        {},
        signal,
        // A datasource the caller cannot read must not fail the whole view --
        // the other sources' candidates are still worth showing.
      ).catch(() => emptyPage<CrossSourceResolutionCandidateRead>());
    }),
  );
  const seen = new Set<string>();
  const candidates: CrossSourceResolutionCandidateRead[] = [];
  for (const page of pages) {
    for (const candidate of page.items ?? []) {
      if (seen.has(candidate.id)) continue;
      seen.add(candidate.id);
      candidates.push(candidate);
    }
  }
  return candidates.sort((a, b) => b.confidence - a.confidence);
}

/** Approve or reject one resolution candidate.
 *
 *  The server refuses a maker reviewing their own candidate (409), the same
 *  maker-checker rule every other decision on this platform carries. */
export async function decideCrossSourceResolutionCandidate(
  candidateId: string,
  decision: "APPROVE" | "REJECT",
  reason: string | null,
  signal?: AbortSignal,
): Promise<CrossSourceResolutionCandidateRead> {
  if (USE_FIXTURES) throw new Error(WRITE_NOTICE);
  return request<CrossSourceResolutionCandidateRead>(
    `/v1/cross-source-object-resolution-candidates/${encodeURIComponent(candidateId)}/decision`,
    { method: "POST", body: JSON.stringify({ decision, reason }) },
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Cross-source relationship candidates.

   Discovery for these already lives on the cross-source screen, but their
   review did not: the results landed on the per-datasource Relationships
   screen, which is precisely the scope that cannot express "this column points
   into another system". That left a loop open -- propose here, decide
   somewhere else, under a filter that hides the interesting rows. These close
   it.
--------------------------------------------------------------------------- */

/** `src/aida/schemas.py::RelationshipCandidateRead`. */
export interface RelationshipCandidateRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  target_datasource_id: string;
  source_table_id: string;
  source_column_id: string;
  target_table_id: string;
  target_column_id: string;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

const FIXTURE_RELATIONSHIP_CANDIDATES: RelationshipCandidateRead[] = [
  {
    id: "xrc_1",
    organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: "ds_oracle_core",
    target_datasource_id: "ds_snowflake_prod",
    source_table_id: "t_party",
    source_column_id: "party_id",
    target_table_id: "t_customer",
    target_column_id: "customer_id",
    detection_rule: "NAME_AND_TYPE_AND_INCLUSION",
    confidence: 0.91,
    evidence: { inclusion_ratio: 0.97, distinct_overlap: 41822 },
    status: "PENDING",
    created_by: "discovery",
    reviewed_by: null,
    review_reason: null,
    reviewed_at: null,
    created_at: "2026-09-02T08:00:00Z",
    updated_at: "2026-09-02T08:00:00Z",
  },
];

/** Cross-source relationship candidates across a domain's datasources.
 *
 *  Filtered to genuinely cross-source rows (`datasource_id !==
 *  target_datasource_id`) client-side: the list endpoint is per datasource and
 *  returns same-source candidates too, and those already have a home on the
 *  Relationships screen. Showing them here as well would make this screen a
 *  second, differently-filtered copy of that one. */
export async function fetchCrossSourceRelationshipCandidates(
  datasourceIds: readonly string[],
  candidateStatus: string | null,
  signal?: AbortSignal,
): Promise<RelationshipCandidateRead[]> {
  if (USE_FIXTURES) {
    return FIXTURE_RELATIONSHIP_CANDIDATES.filter(
      (c) =>
        datasourceIds.includes(c.datasource_id) &&
        (!candidateStatus || c.status === candidateStatus),
    );
  }
  const pages = await Promise.all(
    datasourceIds.map((id) => {
      const params = new URLSearchParams({ limit: "200" });
      if (candidateStatus) params.set("candidate_status", candidateStatus);
      return request<PageOf<RelationshipCandidateRead>>(
        `/v1/datasources/${encodeURIComponent(id)}/relationship-candidates?${params}`,
        {},
        signal,
      ).catch(() => emptyPage<RelationshipCandidateRead>());
    }),
  );
  const seen = new Set<string>();
  const candidates: RelationshipCandidateRead[] = [];
  for (const page of pages) {
    for (const candidate of page.items ?? []) {
      if (candidate.datasource_id === candidate.target_datasource_id) continue;
      if (seen.has(candidate.id)) continue;
      seen.add(candidate.id);
      candidates.push(candidate);
    }
  }
  return candidates.sort((a, b) => b.confidence - a.confidence);
}

/** Approve or reject one relationship candidate.
 *
 *  Same endpoint the Relationships screen uses, and the same maker-checker
 *  rule: the server refuses a principal deciding a candidate they created. */
export async function decideRelationshipCandidate(
  candidateId: string,
  decision: "APPROVE" | "REJECT",
  reason: string | null,
  signal?: AbortSignal,
): Promise<RelationshipCandidateRead> {
  if (USE_FIXTURES) throw new Error(WRITE_NOTICE);
  return request<RelationshipCandidateRead>(
    `/v1/relationship-candidates/${encodeURIComponent(candidateId)}/decision`,
    { method: "POST", body: JSON.stringify({ decision, reason }) },
    signal,
  );
}
