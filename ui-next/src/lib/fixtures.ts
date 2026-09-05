import type {
  AgentAnalysisRequest,
  AgentAnalysisResponse,
  AgentEvaluationRunRead,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  AiDecisionRead,
  AiRuntimeStatusRead,
  AnalysisRunRead,
  AssetEvidenceRead,
  BusinessMapEdgeRead,
  BusinessMapNodeRead,
  BusinessMapRead,
  ConsumerFooterRead,
  DataQualityIncidentRead,
  DataQualityIncidentTransition,
  DataQualityIncidentTriageRead,
  DataQualitySummaryRead,
  DataSourceRead,
  EvidenceItemRead,
  FleetSummaryRead,
  GovernanceDecisionRequest,
  GovernanceReviewDiffRead,
  GovernanceReviewRead,
  LiftSuppressionRequest,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  MeRead,
  MetadataBusinessAnnotationRead,
  MetadataIngestionBatchRead,
  NegativeAssertionRead,
  ModelRouteConfigurationCreate,
  ModelRouteConfigurationRead,
  OrganizationRead,
  OutboxEventRead,
  PlaybookCreate,
  PlaybookRead,
  PlaybookRunResultRead,
  PlaybookUpdate,
  PortfolioAnalyticsSummaryRead,
  PortfolioAnalyticsTrendsRead,
  ProjectRead,
  ReviewQueueProposalRead,
  ReviewQueueRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioDiffRead,
  StudioImpactPreview,
  SourceBindingCreate,
  SourceBindingRead,
  UnifiedLineageGraphRead,
  UnifiedLineageImpactNodeRead,
  UnifiedLineageImpactRead,
  WorkspaceCreate,
  WorkspaceRead,
  AgentInboxRead,
  AgentRosterRead,
  DisagreementReportRead,
  ReviewAuditSampleRead,
  ReviewerAgentRunResult,
  ReviewerAgentStateRead,
} from "./types";
import type {
  AuditEventRead,
  CatalogRowRead,
  CertificationStatus,
  CursorPage,
  MarketplaceProductRead,
  PageOf,
  QualityState,
} from "./ui-types";
import type {
  AgentRunsQuery,
  AnalysisRunsQuery,
  AuditEventQuery,
  BusinessAnnotationsQuery,
  BusinessMapQuery,
  CatalogQuery,
  IngestionBatchesQuery,
  LineageImpactQuery,
  MarketplaceQuery,
  NegativeKnowledgeSearchQuery,
  NegativeKnowledgeSubjectQuery,
  OutboxEventsQuery,
  PlaybooksQuery,
  PortfolioAnalyticsSummaryQuery,
  PortfolioAnalyticsTrendsQuery,
  QualityIncidentsQuery,
  ReviewQueueQuery,
  SemanticPageQuery,
  StudioChangeSetQuery,
} from "./api";

/* UX-16: Relationships — a separate import block (not folded into the one
   above) so this addition stays easy to find and to lift out cleanly. */
import type {
  RelationshipCandidateBulkDecisionItemRead,
  RelationshipCandidateBulkDecisionRequest,
  RelationshipCandidateBulkDecisionResultRead,
  RelationshipCandidateCalibrationRead,
  RelationshipCandidateDecision,
  RelationshipCandidateDiffEntryRead,
  RelationshipCandidateImpactRead,
  RelationshipCandidateRead,
  RelationshipCandidateReviewItemRead,
  RelationshipCandidateReviewQueueRead,
} from "./types";

/* ---------------------------------------------------------------------------
   Deterministic fixtures standing in for the proposed read-model endpoint.

   These generate a 1,000,000-row catalog LAZILY — a page is computed from its
   index, never materialised as an array — because the point of this screen is
   to prove the shell holds at the scale Module 21 §6 specifies, and a fixture
   that allocates a million objects would prove the opposite.
--------------------------------------------------------------------------- */

export const TOTAL_ROWS = 1_000_000;

/** Small deterministic hash so every row is stable across reloads and paging. */
function h(n: number, salt: number): number {
  let x = (n ^ (salt * 0x9e3779b1)) >>> 0;
  x = Math.imul(x ^ (x >>> 16), 0x21f0aaad) >>> 0;
  x = Math.imul(x ^ (x >>> 15), 0x735a2d97) >>> 0;
  return (x ^ (x >>> 15)) >>> 0;
}
const pick = <T,>(arr: readonly T[], n: number, salt: number): T =>
  arr[h(n, salt) % arr.length]!;

const DOMAINS = ["fin", "risk", "retail", "treasury", "cards", "mortgage", "aml"] as const;
const ENTITIES = [
  "customer", "account", "transaction", "position", "exposure", "balance",
  "ledger_entry", "counterparty", "instrument", "settlement", "limit", "collateral",
] as const;
const SUFFIX = ["", "_dim", "_fact", "_hist", "_stg", "_agg", "_snapshot"] as const;
const SCHEMAS = ["core", "curated", "raw", "mart", "reporting"] as const;
const SOURCES = ["snowflake_prod", "oracle_core", "postgres_ops", "bigquery_mi"] as const;
const OWNERS = [
  "Finance Data", "Risk Analytics", "Retail Data Office", "Treasury Ops",
  "Fraud & AML", null,
] as const;
const TERMS = [
  "Net Revenue", "Exposure at Default", "Customer", "Settlement Date",
  "Notional", "Risk Weighted Asset", "Chargeback",
] as const;

function row_owner_count_estimate(i: number): number { return (h(i, 71) % 3) + 1; }

function rowAt(i: number): CatalogRowRead {
  const certRoll = h(i, 7) % 100;
  const certification: CertificationStatus =
    certRoll < 34 ? "CERTIFIED" : certRoll < 44 ? "EXPIRED" : certRoll < 48 ? "REVOKED" : "NONE";

  const qRoll = h(i, 11) % 100;
  const quality: QualityState =
    qRoll < 62 ? "PASSING" : qRoll < 74 ? "INCIDENT_OPEN" : qRoll < 86 ? "STALE" : "UNKNOWN";

  const hasDesc = h(i, 13) % 100 < 58;
  const proposed = hasDesc && h(i, 17) % 100 < 31;
  const entity = pick(ENTITIES, i, 3);
  const domain = pick(DOMAINS, i, 5);
  const termCount = h(i, 23) % 3;
  // `ds_<name>` matches `FIXTURE_DATASOURCES`' own id convention, so a row's
  // cross-links resolve to a datasource the fixture API actually knows about
  // rather than to an id nothing else recognises.
  const sourceName = pick(SOURCES, i, 37);

  return {
    id: `t_${i.toString(36).padStart(6, "0")}`,
    name: `${domain}_${entity}${pick(SUFFIX, i, 29)}`,
    schema_name: pick(SCHEMAS, i, 31),
    datasource_id: `ds_${sourceName}`,
    datasource_name: sourceName,
    object_type: h(i, 41) % 100 < 78 ? "TABLE" : h(i, 43) % 2 ? "VIEW" : "MATERIALIZED_VIEW",
    status: "ACTIVE",
    description: hasDesc
      ? `${entity.replace(/_/g, " ")} records for the ${domain} domain, one row per ${entity}.`
      : null,
    description_is_proposed: proposed,
    owner: pick(OWNERS, i, 47),
    certification,
    certification_expires_at:
      certification === "CERTIFIED" ? "2026-12-31T00:00:00Z" : null,
    // P3-09: sample summary; the real server returns null for legacy /
    // non-CERTIFIED rows -- mirror that shape so the fixture exercises both.
    certification_evidence_summary:
      certification === "CERTIFIED"
        ? {
            description_version_id: hasDesc ? "d0000000000000000000000000000000" : null,
            active_owner_count: row_owner_count_estimate(i),
            open_incident_count_at_certify: quality === "INCIDENT_OPEN" ? 1 : 0,
            glossary_term_count: termCount,
            backfilled: false,
          }
        : null,
    quality,
    glossary_terms: Array.from({ length: termCount }, (_, k) => pick(TERMS, i + k, 53)),
    row_count_estimate: h(i, 59) % 7 === 0 ? null : h(i, 61) % 40_000_000,
    updated_at: new Date(Date.UTC(2026, 7, 1 + (h(i, 67) % 29))).toISOString(),
  };
}

function matches(row: CatalogRowRead, q: CatalogQuery): boolean {
  if (q.q) {
    const needle = q.q.toLowerCase();
    const hay = `${row.name} ${row.description ?? ""} ${row.schema_name}`.toLowerCase();
    if (!hay.includes(needle)) return false;
  }
  if (q.objectType && q.objectType !== "ALL" && row.object_type !== q.objectType) return false;
  if (q.certification && q.certification !== "ALL" && row.certification !== q.certification)
    return false;
  return true;
}

/** Mirrors the server's keyset contract: an opaque cursor, and no `total` once
 *  a cursor is in play (schemas.py:2926 — `total: int | None`). */
export async function makeFixtureCatalog(
  q: CatalogQuery,
): Promise<CursorPage<CatalogRowRead>> {
  const limit = q.limit ?? 100;
  const start = q.cursor ? Number(atob(q.cursor)) : 0;
  const filtered = Boolean(q.q || (q.objectType && q.objectType !== "ALL") ||
    (q.certification && q.certification !== "ALL"));

  const items: CatalogRowRead[] = [];
  let i = start;
  // Scan forward until the page is full. Bounded so a filter matching nothing
  // cannot spin: the real endpoint has an index; this is a stand-in.
  const scanLimit = start + (filtered ? 60_000 : limit);
  while (i < TOTAL_ROWS && items.length < limit && i < scanLimit) {
    const row = rowAt(i);
    if (matches(row, q)) items.push(row);
    i += 1;
  }

  await new Promise((r) => setTimeout(r, 90)); // visible-but-not-annoying latency

  const exhausted = i >= TOTAL_ROWS || (filtered && i >= scanLimit && items.length < limit);
  return {
    items,
    limit,
    offset: start,
    total: q.cursor ? null : filtered ? null : TOTAL_ROWS,
    next_cursor: exhausted ? null : btoa(String(i)),
  };
}

/** Mirrors the real `AssetEvidenceRead` / `EvidenceItemRead` shape
 *  (UX-13, ./types.ts — generated from schemas.py) rather than the ad hoc
 *  label/value/kind shape this fixture used before UX-14: fixture mode and
 *  live mode now return the same wire shape, so `EvidencePane` renders
 *  identically regardless of `VITE_USE_FIXTURES`. */
export async function makeFixtureEvidence(tableId: string): Promise<AssetEvidenceRead> {
  const i = parseInt(tableId.replace("t_", ""), 36);
  const row = rowAt(i);
  await new Promise((r) => setTimeout(r, 70));

  const items: EvidenceItemRead[] = [
    {
      category: "STRUCTURE",
      claim: `${8 + (h(i, 71) % 40)} columns · fingerprint ${row.id.slice(2, 8)}`,
      source: "connector discovery · certified adapter",
    },
    {
      category: "BUSINESS_MEANING",
      claim: row.description
        ? row.description_is_proposed
          ? "Model-proposed, awaiting steward approval"
          : "Approved by steward"
        : "None",
      source: row.description_is_proposed
        ? "semantic_inference.py · ADR-0001: proposal only"
        : "governance review · maker-checker",
    },
    {
      category: "CERTIFICATION",
      claim:
        row.certification === "CERTIFIED"
          ? `Certified, expires ${row.certification_expires_at?.slice(0, 10)}`
          : row.certification === "EXPIRED"
            ? "Certification lapsed"
            : row.certification === "REVOKED"
              ? "Revoked after review"
              : "Never certified",
      source: "GL-5 bulk certification lifecycle",
    },
    {
      category: "DATA_QUALITY",
      claim:
        row.quality === "PASSING"
          ? "All checks passing"
          : row.quality === "INCIDENT_OPEN"
            ? "Open incident — freshness threshold breached"
            : row.quality === "STALE"
              ? "No observation in 14 days"
              : "No checks configured",
      source: "data_quality.py · ADR-0016 fails closed",
    },
    {
      category: "OWNERSHIP",
      claim: row.owner ?? "Unowned",
      source: row.owner ? "GL-2 ownership lifecycle" : "GL-6 unowned-asset backlog",
    },
    {
      category: "CONSUMPTION",
      claim: `${h(i, 73) % 40} tools · ${h(i, 79) % 12} context products`,
      source: "consumption_lineage.py · CX-4",
    },
  ];

  if (row.quality === "INCIDENT_OPEN") {
    items.push({
      category: "AI_DECISION",
      claim: "Declined for analytical use while the incident is open",
      source: "ai_decision_lineage.py · LN-3 refusal edge",
    });
  }

  return {
    table_id: tableId,
    table_name: row.name,
    generated_at: new Date().toISOString(),
    items,
  };
}

/* ---------------------------------------------------------------------------
   Review-queue fixtures. Same standing as the catalog ones: they stand in for
   a read model that composes GovernanceReview items with the evidence behind
   each proposal (tracker UX-17).
--------------------------------------------------------------------------- */

import type { Proposal } from "../components/ProposalCard";

export interface ReviewBatch {
  /** The agent run these proposals came out of. */
  runLabel: string;
  finishedAgo: string;
  passed: number;
  /** Counts of proposals are DERIVED from `proposals` in the screen, never
   *  carried alongside it — a summary that can disagree with the list it
   *  summarises is worse than no summary. */
  threshold: number;
  proposals: Proposal[];
}

export async function fetchReviewBatch(): Promise<ReviewBatch> {
  await new Promise((r) => setTimeout(r, 110));
  return {
    runLabel: "finance-revenue · semantic validation",
    finishedAgo: "9 minutes ago",
    passed: 44,
    threshold: 0.9,
    proposals: [
      {
        id: "p_4181",
        title: "Exclude intercompany transfers from net revenue",
        subject: "metric revenue · v3.1.1 → v3.1.2 · module 18",
        proposedBy: { kind: "agent", name: "semantic inference" },
        confidence: 0.87,
        state: "needs_review",
        diff: [
          { kind: "context", text: "metric: revenue" },
          { kind: "remove", text: "  filter: status != 'void'" },
          { kind: "add", text: "  filter: status NOT IN ('void','reversed')" },
          { kind: "add", text: "  exclude: [intercompany_transfers]" },
        ],
        rationale:
          "Three regression tests fail on the current filter: intercompany transfers inflate Q4 revenue by about 4%. The proposed filter matches how Finance actually closed Q3.",
        evidence:
          "tests ST-A2/q4-intercompany, q4-close-parity, revenue-grain · Q3 close workpaper referenced by 9 of 12 revenue queries",
      },
      {
        id: "p_4182",
        title: "Two definitions of MRR are both live",
        subject: "metric mrr · finance vs sales · module 18",
        proposedBy: { kind: "agent", name: "semantic inference" },
        confidence: 0.71,
        state: "needs_review",
        diff: [
          { kind: "context", text: "finance.mrr = SUM(net_new_arr) WHERE contract_type='new'" },
          { kind: "context", text: "sales.mrr   = SUM(new_bookings) + SUM(committed_arr)" },
          { kind: "add", text: "  → proposal: scope both, block the unqualified name 'mrr'" },
        ],
        rationale:
          "Same name, different formula. A tool resolving an unqualified 'mrr' returns a different number depending on which definition it reaches. Scoping both and refusing the bare name is the only resolution that cannot silently pick a winner.",
        evidence:
          "detected across 2 domains · 14 tools would resolve ambiguously · needs sign-off from both domain owners",
      },
      {
        id: "p_4183",
        title: "Certify orders_raw as Gold",
        subject: "table analytics.orders_raw · module 08",
        proposedBy: { kind: "human", name: "@priya" },
        confidence: 0.94,
        state: "needs_review",
        diff: [
          { kind: "remove", text: "certification: NONE" },
          { kind: "add", text: "certification: CERTIFIED  expires: 2027-02-28" },
        ],
        rationale:
          "Maker-checker requires a second approver. The asset passes all 15 quality checks and has a named owner, so the certification lifecycle can accept it once a checker other than the maker signs.",
        evidence: "GL-5 certification lifecycle · maker @priya · checker must differ",
      },
      {
        id: "p_4179",
        title: "Add synonym “topline” to net revenue",
        subject: "metric revenue · module 18",
        proposedBy: { kind: "agent", name: "semantic inference" },
        confidence: 0.96,
        state: "auto_applied",
        diff: [{ kind: "add", text: "  synonyms: [net sales, topline]" }],
        rationale:
          "Above this tenant's auto-apply threshold, so it was applied and recorded rather than queued. A synonym cannot change what a metric computes — only what resolves to it.",
        evidence: "term appears in 23 queries and 4 dashboards · no conflicting binding",
      },
    ],
  };
}

/**
 * UX-1: fixture mode has no backend behind it, so it can only ever stand in for
 * the *development* identity provider — never for OIDC, which requires a real
 * verified token to produce a real derived persona. `persona: null` here means
 * "the switcher decides", exactly as an unmapped OIDC principal's `null` means
 * "no persona was derived" — the shell renders both the same way, correctly.
 */
export function makeFixtureMe(): MeRead {
  return {
    principal_id: "dev-fixture-user",
    principal_type: "USER",
    organization_id: null,
    roles: ["Analyst", "DataSteward", "Viewer"],
    persona: null,
    identity_provider: "DEVELOPMENT",
  };
}

/** `GET /v1/organizations`. The single development organization every screen's
 *  fixtures are written against, so the shell's picker has one entry to show
 *  and `useOrgId()` keeps resolving to the id the other fixtures use. */
export async function makeFixtureOrganizations(): Promise<OrganizationRead[]> {
  await wait(40);
  return [
    {
      id: "00000000-0000-0000-0000-000000000001",
      name: "Atlas Demo Bank",
      slug: "atlas-demo",
      status: "ACTIVE",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    },
  ];
}

/* ---------------------------------------------------------------------------
   UX-15 / UX-20 fixtures — the same standing as everything above: a stand-in
   for `npm run dev`/`npm run test` with no backend running, wire-shape
   identical to the real endpoint (see the module docstring on the
   `AiDecisionRead`/`ReviewQueueRead`/etc. types this mirrors). Every one of
   these endpoints is real and merged (see the comment above each `fetch*` in
   `api.ts`) — nothing here stands in for an unbuilt route.
--------------------------------------------------------------------------- */

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

function diffRead(
  reviewId: string,
  objectType: string,
  objectId: string,
  entries: GovernanceReviewDiffRead["entries"],
): GovernanceReviewDiffRead {
  return { review_id: reviewId, object_type: objectType, object_id: objectId, diffable: true, entries };
}

const REVIEW_FIXTURE_PROPOSALS: ReviewQueueProposalRead[] = [
  {
    review_id: "rq_4181",
    organization_id: "00000000-0000-0000-0000-000000000001",
    object_type: "SEMANTIC_METRIC_VERSION",
    object_id: "metric:revenue",
    requested_action: "UPDATE",
    status: "PENDING",
    requested_by: "semantic_inference_agent",
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    created_at: "2026-09-01T14:02:00Z",
    confidence: 0.87,
    evidence: [
      {
        category: "DATA_QUALITY",
        claim: "3 regression tests fail on the current filter: intercompany transfers inflate Q4 revenue by ~4%",
        source: "studio_eval.py · mined question ST-A2/q4-intercompany",
      },
      {
        category: "CONSUMPTION",
        claim: "Q3 close workpaper referenced by 9 of 12 revenue queries",
        source: "consumption_lineage.py · CX-4",
      },
    ],
    diff: diffRead("rq_4181", "SEMANTIC_METRIC_VERSION", "metric:revenue", [
      { field: "filter", change: "changed", before: "status != 'void'", after: "status NOT IN ('void','reversed')" },
      { field: "exclude", change: "added", before: null, after: ["intercompany_transfers"] },
    ]),
  },
  {
    review_id: "rq_4182",
    organization_id: "00000000-0000-0000-0000-000000000001",
    object_type: "GLOSSARY_TERM_VERSION",
    object_id: "term:mrr",
    requested_action: "UPDATE",
    status: "PENDING",
    requested_by: "semantic_inference_agent",
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    created_at: "2026-09-01T13:40:00Z",
    confidence: 0.71,
    evidence: [
      {
        category: "BUSINESS_MEANING",
        claim: "finance.mrr and sales.mrr resolve to different formulas under the same unqualified name",
        source: "semantic_diff.py · duplicate-definition scan",
      },
    ],
    diff: diffRead("rq_4182", "GLOSSARY_TERM_VERSION", "term:mrr", [
      { field: "scope", change: "added", before: null, after: "finance.mrr, sales.mrr (unqualified name blocked)" },
    ]),
  },
  {
    review_id: "rq_4183",
    organization_id: "00000000-0000-0000-0000-000000000001",
    object_type: "ASSET_DESCRIPTION_DRAFT",
    object_id: "table:analytics.orders_raw",
    requested_action: "CERTIFY",
    status: "PENDING",
    requested_by: "priya@tenant.example",
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    created_at: "2026-09-01T11:15:00Z",
    confidence: null,
    evidence: [
      { category: "CERTIFICATION", claim: "Passes all 15 quality checks, has a named owner", source: "GL-5 certification lifecycle" },
    ],
    diff: diffRead("rq_4183", "ASSET_DESCRIPTION_DRAFT", "table:analytics.orders_raw", [
      { field: "certification", change: "changed", before: "NONE", after: "CERTIFIED" },
    ]),
  },
  {
    review_id: "rq_4179",
    organization_id: "00000000-0000-0000-0000-000000000001",
    object_type: "GLOSSARY_TERM_VERSION",
    object_id: "term:revenue",
    requested_action: "UPDATE",
    status: "APPROVED",
    requested_by: "semantic_inference_agent",
    decided_by: "priya@tenant.example",
    decision_reason: "Matches Finance's own usage.",
    decided_at: "2026-09-01T10:05:00Z",
    created_at: "2026-09-01T09:50:00Z",
    confidence: 0.96,
    evidence: [
      { category: "CONSUMPTION", claim: "Term appears in 23 queries and 4 dashboards, no conflicting binding", source: "consumption_lineage.py · CX-4" },
    ],
    diff: diffRead("rq_4179", "GLOSSARY_TERM_VERSION", "term:revenue", [
      { field: "synonyms", change: "added", before: null, after: ["net sales", "topline"] },
    ]),
  },
];

/** `GET /v1/governance/reviews/queue` (UX-17). Filters by `status`/`object_type`
 *  the same way the real endpoint does, purely client-side over this fixed set. */
export async function makeFixtureReviewQueue(
  query: ReviewQueueQuery,
): Promise<ReviewQueueRead> {
  await wait(90);
  const statusFilter = query.status === undefined ? "PENDING" : query.status;
  let proposals = REVIEW_FIXTURE_PROPOSALS;
  if (statusFilter) proposals = proposals.filter((p) => p.status === statusFilter);
  if (query.objectType) proposals = proposals.filter((p) => p.object_type === query.objectType);
  return {
    organization_id: "00000000-0000-0000-0000-000000000001",
    status_filter: statusFilter || null,
    object_type_filter: query.objectType ?? null,
    inference_run_id_filter: null,
    generated_at: new Date().toISOString(),
    proposals,
    total_proposals: proposals.length,
    by_status: proposals.reduce<Record<string, number>>((acc, p) => {
      acc[p.status] = (acc[p.status] ?? 0) + 1;
      return acc;
    }, {}),
    by_object_type: proposals.reduce<Record<string, number>>((acc, p) => {
      acc[p.object_type] = (acc[p.object_type] ?? 0) + 1;
      return acc;
    }, {}),
    diffable_count: proposals.filter((p) => p.diff.diffable).length,
  };
}

/** `POST /v1/governance/reviews/{id}/decision` — mutates the same in-memory
 *  fixture array `makeFixtureReviewQueue` reads, so a decide-then-refetch in
 *  fixture mode behaves like the real maker-checker endpoint. */
export async function makeFixtureDecideReview(
  reviewId: string,
  body: GovernanceDecisionRequest,
): Promise<GovernanceReviewRead> {
  await wait(80);
  const proposal = REVIEW_FIXTURE_PROPOSALS.find((p) => p.review_id === reviewId);
  if (!proposal) throw new Error(`fixture: no such review ${reviewId}`);
  if (proposal.status !== "PENDING") throw new Error("governance review is already decided");
  proposal.status = body.decision === "APPROVE" ? "APPROVED" : "REJECTED";
  proposal.decided_by = "dev-fixture-user";
  proposal.decision_reason = body.reason ?? null;
  proposal.decided_at = new Date().toISOString();
  return {
    id: proposal.review_id,
    organization_id: proposal.organization_id,
    object_type: proposal.object_type,
    object_id: proposal.object_id,
    requested_action: proposal.requested_action,
    status: proposal.status,
    requested_by: proposal.requested_by,
    decided_by: proposal.decided_by,
    decision_reason: proposal.decision_reason,
    decided_at: proposal.decided_at,
    created_at: proposal.created_at,
    updated_at: proposal.decided_at,
  };
}

const MARKETPLACE_FIXTURE_PRODUCTS: MarketplaceProductRead[] = [
  {
    id: "dpv_revenue", organization_id: "00000000-0000-0000-0000-000000000001",
    product_id: "dp_revenue", product_key: "finance-revenue-model", version: 3,
    name: "Finance revenue model", description: "Certified revenue metrics and the tables behind them, governed by Finance.",
    domain_name: "fin", owner_principal: "priya@tenant.example", usage_terms: "Internal use, Finance-approved queries only.",
    classification: "INTERNAL", certification_status: "CERTIFIED", quality_score: 0.97, lineage_coverage: 0.91,
    context_product_version_id: null, discoverable_roles: ["*"], consumer_roles: ["Analyst", "Viewer"],
    ports: [
      { port_key: "revenue_semantic", direction: "OUTPUT", name: "Revenue semantic model", description: "Metrics", asset_type: "SEMANTIC_MODEL", asset_id: "sm_revenue" },
      { port_key: "orders_raw", direction: "OUTPUT", name: "orders_raw", description: "Raw orders table", asset_type: "TABLE", asset_id: "t_orders_raw" },
    ],
    status: "PUBLISHED", fingerprint: "a1b2c3", created_by: "priya@tenant.example",
    approved_by: "steward@tenant.example", approved_at: "2026-08-15T00:00:00Z", published_at: "2026-08-16T00:00:00Z",
    created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-16T00:00:00Z",
    access_status: "ROLE_GRANTED", domain_affinity: true, role_affinity: true,
  },
  {
    id: "dpv_risk_exposure", organization_id: "00000000-0000-0000-0000-000000000001",
    product_id: "dp_risk_exposure", product_key: "risk-exposure-snapshot", version: 1,
    name: "Risk exposure snapshot", description: "Daily counterparty exposure, restricted to Risk Analytics.",
    domain_name: "risk", owner_principal: "risk-lead@tenant.example", usage_terms: "Restricted — Risk Analytics only.",
    classification: "RESTRICTED", certification_status: "CERTIFIED", quality_score: 0.94, lineage_coverage: 0.88,
    context_product_version_id: null, discoverable_roles: ["DataSteward", "Reviewer"], consumer_roles: ["Reviewer"],
    ports: [
      { port_key: "exposure_table", direction: "OUTPUT", name: "risk_exposure_snapshot", description: "Table", asset_type: "TABLE", asset_id: "t_risk_exposure" },
    ],
    status: "PUBLISHED", fingerprint: "d4e5f6", created_by: "risk-lead@tenant.example",
    approved_by: "steward@tenant.example", approved_at: "2026-08-20T00:00:00Z", published_at: "2026-08-21T00:00:00Z",
    created_at: "2026-08-10T00:00:00Z", updated_at: "2026-08-21T00:00:00Z",
    access_status: "NOT_REQUESTED", domain_affinity: false, role_affinity: false,
  },
  {
    id: "dpv_customer360", organization_id: "00000000-0000-0000-0000-000000000001",
    product_id: "dp_customer360", product_key: "retail-customer-360", version: 5,
    name: "Retail customer 360", description: "Curated customer context product for the retail data office.",
    domain_name: "retail", owner_principal: "retail-owner@tenant.example", usage_terms: "Internal use.",
    classification: "CONFIDENTIAL", certification_status: "CERTIFIED", quality_score: 0.9, lineage_coverage: 0.79,
    context_product_version_id: "cpv_customer360", discoverable_roles: ["*"], consumer_roles: ["Analyst", "Viewer"],
    ports: [
      { port_key: "customer_context", direction: "OUTPUT", name: "customer_360 context product", description: "Bundle", asset_type: "CONTEXT_PRODUCT", asset_id: "cp_customer360" },
    ],
    status: "PUBLISHED", fingerprint: "g7h8i9", created_by: "retail-owner@tenant.example",
    approved_by: "steward@tenant.example", approved_at: "2026-07-28T00:00:00Z", published_at: "2026-07-29T00:00:00Z",
    created_at: "2026-07-01T00:00:00Z", updated_at: "2026-07-29T00:00:00Z",
    access_status: "REQUEST_PENDING", domain_affinity: false, role_affinity: true,
  },
];

/** `GET /v1/marketplace/products` (CX-9). */
export async function makeFixtureMarketplaceProducts(
  query: MarketplaceQuery,
): Promise<PageOf<MarketplaceProductRead>> {
  await wait(90);
  let items = MARKETPLACE_FIXTURE_PRODUCTS;
  if (query.q) {
    const needle = query.q.toLowerCase();
    items = items.filter(
      (p) => p.name.toLowerCase().includes(needle) || p.description.toLowerCase().includes(needle),
    );
  }
  if (query.domain) items = items.filter((p) => p.domain_name === query.domain);
  if (query.classification) items = items.filter((p) => p.classification === query.classification);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `POST /v1/marketplace/products/{version_id}/access-requests`. */
export async function makeFixtureMarketplaceAccessRequest(
  versionId: string,
  body: MarketplaceAccessRequestCreate,
): Promise<MarketplaceAccessRequestRead> {
  await wait(80);
  const now = new Date().toISOString();
  const product = MARKETPLACE_FIXTURE_PRODUCTS.find((p) => p.id === versionId);
  if (product) product.access_status = "REQUEST_PENDING";
  return {
    id: `mar_${versionId}`,
    organization_id: "00000000-0000-0000-0000-000000000001",
    data_product_version_id: versionId,
    requested_by: "dev-fixture-user",
    purpose: body.purpose,
    duration_days: body.duration_days ?? 90,
    status: "PENDING",
    governance_review_id: `gr_${versionId}`,
    decided_by: null, decision_reason: null, decided_at: null, expires_at: null,
    revoked_by: null, revoked_at: null,
    fulfillment_status: "PENDING", fulfillment_provider: null, fulfillment_reference: null, fulfillment_error: null,
    fulfilled_at: null, created_at: now, updated_at: now,
  };
}

const PORTFOLIO_TOP_PRODUCTS_FIXTURE: PortfolioAnalyticsSummaryRead["top_products"] = [
  {
    data_product_version_id: "dpv_revenue", product_key: "finance-revenue-model", name: "Finance revenue model",
    domain_name: "fin", certification_status: "CERTIFIED", quality_score: 97, lineage_coverage: 91,
    access_request_count: 42, approved_access_count: 38, context_read_count: 5210,
  },
  {
    data_product_version_id: "dpv_customer360", product_key: "retail-customer-360", name: "Retail customer 360",
    domain_name: "retail", certification_status: "CERTIFIED", quality_score: 90, lineage_coverage: 79,
    access_request_count: 31, approved_access_count: 27, context_read_count: 3840,
  },
  {
    data_product_version_id: "dpv_risk_exposure", product_key: "risk-exposure-snapshot", name: "Risk exposure snapshot",
    domain_name: "risk", certification_status: "CERTIFIED", quality_score: 94, lineage_coverage: 88,
    access_request_count: 19, approved_access_count: 14, context_read_count: 1275,
  },
  {
    data_product_version_id: "dpv_supply_chain", product_key: "supply-chain-events", name: "Supply chain events",
    domain_name: "ops", certification_status: "REVIEW_REQUIRED", quality_score: 74, lineage_coverage: 61,
    access_request_count: 16, approved_access_count: 9, context_read_count: 902,
  },
  {
    data_product_version_id: "dpv_hr_headcount", product_key: "hr-headcount-summary", name: "HR headcount summary",
    domain_name: "hr", certification_status: "UNCERTIFIED", quality_score: 68, lineage_coverage: 55,
    access_request_count: 8, approved_access_count: 5, context_read_count: 340,
  },
  {
    data_product_version_id: "dpv_marketing_attr", product_key: "marketing-attribution", name: "Marketing attribution",
    domain_name: "marketing", certification_status: "CERTIFIED", quality_score: 88, lineage_coverage: 72,
    access_request_count: 12, approved_access_count: 11, context_read_count: 1560,
  },
  {
    data_product_version_id: "dpv_pricing_book", product_key: "pricing-rate-book", name: "Pricing rate book",
    domain_name: "fin", certification_status: "PENDING_CERTIFICATION", quality_score: 81, lineage_coverage: 68,
    access_request_count: 7, approved_access_count: 4, context_read_count: 415,
  },
];

/** `GET .../portfolio-analytics/summary` (`product_marketplace_api.py`). */
export async function makeFixturePortfolioAnalyticsSummary(
  query: PortfolioAnalyticsSummaryQuery,
): Promise<PortfolioAnalyticsSummaryRead> {
  await wait(90);
  const limit = query.topProductsLimit ?? 10;
  return {
    generated_at: new Date().toISOString(),
    window_days: query.windowDays ?? 30,
    low_quality_threshold: query.lowQualityThreshold ?? 80,
    lifecycle: {
      data_products_total: 58,
      data_products_active: 41,
      data_products_candidate: 12,
      data_products_retired: 5,
      data_product_versions_draft: 9,
      data_product_versions_review_required: 6,
      data_product_versions_published: 63,
      data_product_versions_retired: 8,
      data_contract_versions_draft: 7,
      data_contract_versions_review_required: 3,
      data_contract_versions_published: 51,
      context_products_total: 22,
      context_product_versions_draft: 4,
      context_product_versions_review_required: 2,
      context_product_versions_published: 19,
      context_product_versions_deprecated: 3,
    },
    access: {
      requests_created: 128,
      requests_pending: 14,
      requests_approved: 96,
      requests_rejected: 11,
      requests_revoked: 4,
      requests_expired: 3,
      active_grants: 214,
      grants_expiring_within_30_days: 17,
      fulfillment_pending: 6,
      fulfillment_provisioned: 201,
      fulfillment_failed: 2,
      fulfillment_revoked: 5,
    },
    usage: {
      unique_context_consumers: 47,
      unique_mcp_consumers: 33,
      unique_agent_principals: 19,
      context_product_reads: 18420,
      mcp_operations: 9640,
      mcp_resource_reads: 5210,
      mcp_prompt_reads: 1180,
      mcp_tool_calls: 2890,
      mcp_control_operations: 360,
      agent_runs: 3120,
      governed_tool_agent_runs: 2540,
      model_gateway_agent_runs: 480,
      development_override_agent_runs: 62,
      policy_blocked_agent_runs: 38,
      query_executions: 7460,
      governed_tool_executions: 6890,
    },
    quality: {
      published_products: 63,
      scored_products: 55,
      average_quality_score: 84.6,
      low_quality_products: 9,
      certified_products: 44,
      uncertified_products: 19,
      average_lineage_coverage: 76.2,
    },
    queues: {
      review_required_data_product_versions: 6,
      review_required_data_contract_versions: 3,
      review_required_context_product_versions: 2,
      pending_marketplace_access_requests: 14,
    },
    top_products: PORTFOLIO_TOP_PRODUCTS_FIXTURE.slice(0, limit),
  };
}

/** `GET .../portfolio-analytics/trends` (`product_marketplace_api.py`) -- a
 *  believable, gently upward series so the trend panel has something worth
 *  scaling a bar list against, not five identical rows. */
export async function makeFixturePortfolioAnalyticsTrends(
  query: PortfolioAnalyticsTrendsQuery,
): Promise<PortfolioAnalyticsTrendsRead> {
  await wait(80);
  const windowDays = query.windowDays ?? 30;
  const bucketDays = query.bucketDays ?? 7;
  const bucketCount = Math.max(1, Math.round(windowDays / bucketDays));
  const now = Date.now();
  const dayMs = 24 * 60 * 60 * 1000;
  const points = Array.from({ length: bucketCount }, (_, i) => {
    const rampUp = i / Math.max(1, bucketCount - 1);
    const bucketEndMs = now - (bucketCount - 1 - i) * bucketDays * dayMs;
    const bucketStartMs = bucketEndMs - bucketDays * dayMs;
    return {
      bucket_start: new Date(bucketStartMs).toISOString(),
      bucket_end: new Date(bucketEndMs).toISOString(),
      access_requests: Math.round(18 + rampUp * 14),
      context_reads: Math.round(2200 + rampUp * 1800),
      mcp_operations: Math.round(1100 + rampUp * 900),
      mcp_tool_calls: Math.round(340 + rampUp * 260),
      agent_runs: Math.round(380 + rampUp * 340),
      governed_tool_runs: Math.round(310 + rampUp * 260),
      model_gateway_runs: Math.round(60 + rampUp * 50),
      query_executions: Math.round(880 + rampUp * 520),
    };
  });
  return {
    generated_at: new Date().toISOString(),
    window_days: windowDays,
    bucket_days: bucketDays,
    points,
  };
}

const REFUSAL_FIXTURES: AiDecisionRead[] = [
  {
    id: "dec_r1", organization_id: "00000000-0000-0000-0000-000000000001", run_id: "run_9001",
    decision_type: "REFUSAL", source_node: "agent:revenue_analyst", target_node: "tool:tool_revenue_by_lob",
    reason: "tool_revenue_by_lob refused while the raw_sales quality incident is open",
    evidence: { rule_set: "dq_raw_sales@4", incident: "INC-4821" },
    control_version: "ADR-0016@2", decided_at: "2026-09-01T15:22:00Z",
  },
  {
    id: "dec_r2", organization_id: "00000000-0000-0000-0000-000000000001", run_id: "run_9002",
    decision_type: "REFUSAL", source_node: "agent:mrr_reporter", target_node: "metric:mrr",
    reason: "unqualified metric name 'mrr' is ambiguous between finance.mrr and sales.mrr",
    evidence: { domains: ["finance", "sales"] },
    control_version: "SM-9@1", decided_at: "2026-09-01T12:04:00Z",
  },
  {
    id: "dec_r3", organization_id: "00000000-0000-0000-0000-000000000001", run_id: "run_9003",
    decision_type: "REFUSAL", source_node: "agent:pricing_bot", target_node: "table:t_risk_exposure",
    reason: "requester's role has no DISCOVER binding for this RESTRICTED asset",
    evidence: { classification: "RESTRICTED", required_roles: ["DataSteward", "Reviewer"] },
    control_version: "GL-1@3", decided_at: "2026-08-31T09:47:00Z",
  },
];

const RUN_DECISION_FIXTURES: Record<string, AiDecisionRead[]> = {
  run_9001: [
    { id: "dec_r1a", organization_id: "00000000-0000-0000-0000-000000000001", run_id: "run_9001", decision_type: "RETRIEVAL_SELECTED", source_node: "agent:revenue_analyst", target_node: "table:t_orders_raw", reason: "selected as the direct dependency of tool_revenue_by_lob", evidence: {}, control_version: null, decided_at: "2026-09-01T15:21:40Z" },
    { id: "dec_r1", organization_id: "00000000-0000-0000-0000-000000000001", run_id: "run_9001", decision_type: "REFUSAL", source_node: "agent:revenue_analyst", target_node: "tool:tool_revenue_by_lob", reason: "tool_revenue_by_lob refused while the raw_sales quality incident is open", evidence: { rule_set: "dq_raw_sales@4", incident: "INC-4821" }, control_version: "ADR-0016@2", decided_at: "2026-09-01T15:22:00Z" },
  ],
};

/** `GET /v1/ai-decisions/refusals` (LN-3). */
export async function makeFixtureRefusals(
  opts: { limit?: number; offset?: number },
): Promise<PageOf<AiDecisionRead>> {
  await wait(90);
  const offset = opts.offset ?? 0;
  const limit = opts.limit ?? 50;
  return {
    items: REFUSAL_FIXTURES.slice(offset, offset + limit),
    limit,
    offset,
    total: REFUSAL_FIXTURES.length,
  };
}

/** `GET /v1/ai-decisions/{run_id}`. */
export async function makeFixtureRunDecisions(runId: string): Promise<AiDecisionRead[]> {
  await wait(70);
  return RUN_DECISION_FIXTURES[runId] ?? REFUSAL_FIXTURES.filter((d) => d.run_id === runId);
}

const STUDIO_CHANGE_SETS: StudioChangeSetRead[] = [
  {
    id: "cs_1001", organization_id: "00000000-0000-0000-0000-000000000001",
    name: "Exclude intercompany transfers from net revenue", author: "priya@tenant.example",
    status: "TESTING", base_version_hash: "0".repeat(64), conflict_status: "CLEAN",
    created_at: "2026-09-01T09:00:00Z", updated_at: "2026-09-01T09:40:00Z",
  },
  {
    id: "cs_1002", organization_id: "00000000-0000-0000-0000-000000000001",
    name: "Publish retail customer 360 context product v6", author: "retail-owner@tenant.example",
    status: "DRAFT", base_version_hash: "0".repeat(64), conflict_status: "CLEAN",
    created_at: "2026-08-30T11:00:00Z", updated_at: "2026-08-31T08:00:00Z",
  },
];

const STUDIO_ITEMS: Record<string, StudioChangeItemRead[]> = {
  cs_1001: [
    {
      id: "item_1", organization_id: "00000000-0000-0000-0000-000000000001", change_set_id: "cs_1001",
      object_type: "METRIC", object_id: "metric:revenue", operation: "UPDATE",
      before_snapshot: { filter: "status != 'void'" },
      after_snapshot: { filter: "status NOT IN ('void','reversed')", exclude: ["intercompany_transfers"] },
      diff: { filter: { before: "status != 'void'", after: "status NOT IN ('void','reversed')" } },
      test_status: "PASSED", created_at: "2026-09-01T09:05:00Z", updated_at: "2026-09-01T09:35:00Z",
    },
  ],
  cs_1002: [
    {
      id: "item_2", organization_id: "00000000-0000-0000-0000-000000000001", change_set_id: "cs_1002",
      object_type: "CONTEXT_PRODUCT", object_id: "cp_customer360", operation: "UPDATE",
      before_snapshot: { version: 5 }, after_snapshot: { version: 6, ports: ["customer_context", "loyalty_context"] },
      diff: { ports: { before: ["customer_context"], after: ["customer_context", "loyalty_context"] } },
      test_status: "UNTESTED", created_at: "2026-08-30T11:05:00Z", updated_at: "2026-08-30T11:05:00Z",
    },
  ],
};

/** `GET /v1/studio/change-sets` (ST-A7). */
export async function makeFixtureStudioChangeSets(
  query: StudioChangeSetQuery,
): Promise<StudioChangeSetRead[]> {
  await wait(80);
  let items = STUDIO_CHANGE_SETS;
  if (query.status) items = items.filter((cs) => cs.status === query.status);
  return items;
}

/** `POST /v1/studio/change-sets/{id}/submit`. */
export async function makeFixtureSubmitStudioChangeSet(
  changeSetId: string,
): Promise<StudioChangeSetRead> {
  await wait(90);
  const cs = STUDIO_CHANGE_SETS.find((c) => c.id === changeSetId);
  if (!cs) throw new Error(`fixture: no such change set ${changeSetId}`);
  const items = STUDIO_ITEMS[changeSetId] ?? [];
  const untested = items.filter((i) => i.test_status !== "PASSED");
  if (items.length === 0) throw new Error("cannot submit an empty change set");
  if (untested.length > 0) throw new Error(`${untested.length} item(s) have not passed testing`);
  cs.status = "SUBMITTED";
  cs.updated_at = new Date().toISOString();
  return cs;
}

/** `GET /v1/studio/change-sets/{id}/items`. */
export async function makeFixtureStudioChangeSetItems(
  changeSetId: string,
): Promise<StudioChangeItemRead[]> {
  await wait(70);
  return STUDIO_ITEMS[changeSetId] ?? [];
}

/** `GET /v1/studio/change-sets/{id}/diff`. */
export async function makeFixtureStudioDiff(changeSetId: string): Promise<StudioDiffRead> {
  await wait(70);
  const items = STUDIO_ITEMS[changeSetId] ?? [];
  return {
    change_set_id: changeSetId,
    items: items.map((i) => ({
      item_id: i.id,
      object_type: i.object_type,
      object_id: i.object_id,
      operation: i.operation,
      diff: i.diff,
    })),
  };
}

/** `GET /v1/studio/change-sets/{id}/impact` (`compute_impact`). */
export async function makeFixtureStudioImpact(changeSetId: string): Promise<StudioImpactPreview> {
  await wait(70);
  const items = STUDIO_ITEMS[changeSetId] ?? [];
  const affected = items.map((i) => ({
    object_type: i.object_type,
    object_id: i.object_id,
    reason: `${i.operation.toLowerCase()} via change set ${changeSetId}`,
  }));
  return {
    change_set_id: changeSetId,
    affected_object_count: affected.length,
    affected_objects: affected,
  };
}

const FIXTURE_DATASOURCES: DataSourceRead[] = [
  {
    id: "ds_snowflake_prod", organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_fin", data_domain_id: "dom_fin", project_id: "proj_core",
    name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PRODUCTION",
    network_zone: "default", credential_reference: "vault://ds/snowflake_prod", max_concurrency: 8,
    status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  },
  /* The catalog fixture spreads its million rows across four sources
     (`SOURCES` in `rowAt`), so listing only one here made every row from the
     other three link to a datasource this fixture API does not know. The
     three below exist so a cross-link from a catalog row resolves to a real
     picker entry, exactly as it does against a live backend. */
  {
    id: "ds_oracle_core", organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_fin", data_domain_id: "dom_fin", project_id: "proj_core",
    name: "oracle_core", connector_type: "ORACLE", dialect: "oracle", environment: "PRODUCTION",
    network_zone: "restricted", credential_reference: "vault://ds/oracle_core", max_concurrency: 4,
    status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  },
  {
    id: "ds_postgres_ops", organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_fin", data_domain_id: "dom_fin", project_id: "proj_core",
    name: "postgres_ops", connector_type: "POSTGRESQL", dialect: "postgresql", environment: "PRODUCTION",
    network_zone: "default", credential_reference: "vault://ds/postgres_ops", max_concurrency: 6,
    status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  },
  {
    id: "ds_bigquery_mi", organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_retail", data_domain_id: "dom_retail", project_id: "proj_retail",
    name: "bigquery_mi", connector_type: "BIGQUERY", dialect: "bigquery", environment: "PRODUCTION",
    network_zone: "default", credential_reference: "vault://ds/bigquery_mi", max_concurrency: 8,
    status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  },
];

const FIXTURE_WORKSPACES: WorkspaceRead[] = [
  {
    id: "ws_governed_analytics",
    organization_id: "00000000-0000-0000-0000-000000000001",
    isolation_boundary_id: null,
    name: "Governed analytics",
    slug: "governed-analytics",
    purpose: "Curated workspace for governed analysis across approved project sources.",
    status: "ACTIVE",
    monthly_cost_ceiling: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  },
];

const FIXTURE_SOURCE_BINDINGS: SourceBindingRead[] = [
  {
    id: "binding_governed_snowflake",
    organization_id: "00000000-0000-0000-0000-000000000001",
    workspace_id: "ws_governed_analytics",
    datasource_id: "ds_snowflake_prod",
    schema_scope: [],
    permitted_classifications: [],
    masking_profile: "DEFAULT",
    purpose: "Governed analytics",
    max_query_cost: null,
    status: "ACTIVE",
    requested_by: "fixture-admin",
    approved_by: "fixture-reviewer",
    approved_at: "2026-01-01T00:00:00Z",
    expires_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  /* The other three sources the catalog fixture draws rows from. Without an
     ACTIVE binding each one, `scope.tsx` correctly filters them out of every
     datasource picker -- which is the governance model working, but left the
     fixture estate self-contradictory: the catalog listed a million rows from
     four sources while the workspace could reach one. Binding them makes the
     demo estate coherent; the filtering behaviour itself is unchanged and
     still exercised by `binding_pending_bigquery` below. */
  ...(["ds_oracle_core", "ds_postgres_ops", "ds_bigquery_mi"] as const).map((datasourceId) => ({
    id: `binding_governed_${datasourceId}`,
    organization_id: "00000000-0000-0000-0000-000000000001",
    workspace_id: "ws_governed_analytics",
    datasource_id: datasourceId,
    schema_scope: [],
    permitted_classifications: [],
    masking_profile: "DEFAULT",
    purpose: "Governed analytics",
    max_query_cost: null,
    status: "ACTIVE",
    requested_by: "fixture-admin",
    approved_by: "fixture-reviewer",
    approved_at: "2026-01-01T00:00:00Z",
    expires_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  })),
];

/** `GET /v1/organizations/{id}/datasources`. */
export async function makeFixtureOrgDatasources(): Promise<PageOf<DataSourceRead>> {
  await wait(60);
  return { items: FIXTURE_DATASOURCES, limit: 500, offset: 0, total: FIXTURE_DATASOURCES.length };
}

export async function makeFixtureOrgWorkspaces(
  organizationId: string,
): Promise<PageOf<WorkspaceRead>> {
  await wait(40);
  const items = FIXTURE_WORKSPACES.filter((workspace) => workspace.organization_id === organizationId);
  return { items, limit: 200, offset: 0, total: items.length };
}

export async function makeFixtureWorkspaceSourceBindings(
  workspaceId: string,
): Promise<PageOf<SourceBindingRead>> {
  await wait(35);
  const items = FIXTURE_SOURCE_BINDINGS.filter((binding) => binding.workspace_id === workspaceId);
  return { items, limit: items.length || 1, offset: 0, total: items.length };
}

export async function makeFixtureCreateWorkspace(
  organizationId: string,
  body: WorkspaceCreate,
): Promise<WorkspaceRead> {
  await wait(60);
  const now = new Date().toISOString();
  const workspace: WorkspaceRead = {
    id: `ws_${body.slug}`,
    organization_id: organizationId,
    isolation_boundary_id: body.isolation_boundary_id ?? null,
    name: body.name,
    slug: body.slug,
    purpose: body.purpose ?? "",
    status: "ACTIVE",
    monthly_cost_ceiling: body.monthly_cost_ceiling ?? null,
    created_at: now,
    updated_at: now,
  };
  FIXTURE_WORKSPACES.push(workspace);
  return workspace;
}

export async function makeFixtureRequestSourceBinding(
  workspaceId: string,
  body: SourceBindingCreate,
): Promise<SourceBindingRead> {
  await wait(60);
  const workspace = FIXTURE_WORKSPACES.find((item) => item.id === workspaceId);
  const now = new Date().toISOString();
  const binding: SourceBindingRead = {
    id: `binding_${workspaceId}_${body.datasource_id}`,
    organization_id: workspace?.organization_id ?? "00000000-0000-0000-0000-000000000001",
    workspace_id: workspaceId,
    datasource_id: body.datasource_id,
    schema_scope: body.schema_scope ?? [],
    permitted_classifications: body.permitted_classifications ?? [],
    masking_profile: body.masking_profile ?? "DEFAULT",
    purpose: body.purpose,
    max_query_cost: body.max_query_cost ?? null,
    status: "PENDING_APPROVAL",
    requested_by: "fixture-admin",
    approved_by: null,
    approved_at: null,
    expires_at: null,
    created_at: now,
    updated_at: now,
  };
  FIXTURE_SOURCE_BINDINGS.push(binding);
  return binding;
}

function impactNode(
  id: string,
  kind: UnifiedLineageImpactNodeRead["node_kind"],
  label: string,
  qualifiedName: string,
  depth: number,
  sources: UnifiedLineageImpactNodeRead["contributing_edge_sources"],
): UnifiedLineageImpactNodeRead {
  return { node_id: id, node_kind: kind, label, qualified_name: qualifiedName, depth, contributing_edge_sources: sources };
}

/** `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}`. A small,
 *  hand-built two-hop graph (`raw_sales` -> `orders_raw` -> `revenue_agg`) so
 *  the narrated-traversal screen has more than one hop to narrate, matching
 *  the AT-D4 propagation story this same shell already tells in the review
 *  queue (see `PropagationLog`'s hard-coded steps) — but here fed by this
 *  fixture standing in for the *real* endpoint's shape, not invented prose. */
export async function makeFixtureLineageImpact(
  datasourceId: string,
  nodeId: string,
  _query: LineageImpactQuery,
): Promise<UnifiedLineageImpactRead> {
  await wait(120);
  const known: Record<string, { label: string; qualifiedName: string }> = {
    t_orders_raw: { label: "orders_raw", qualifiedName: "analytics.core.orders_raw" },
    t_raw_sales: { label: "raw_sales", qualifiedName: "analytics.raw.raw_sales" },
    t_revenue_agg: { label: "revenue_agg", qualifiedName: "analytics.mart.revenue_agg" },
  };
  const focus = known[nodeId] ?? { label: nodeId, qualifiedName: nodeId };
  return {
    datasource_id: datasourceId,
    focus_node_id: nodeId,
    focus_node_kind: "TABLE",
    focus_label: focus.qualifiedName,
    upstream: [
      impactNode("t_raw_sales", "TABLE", "raw_sales", "analytics.raw.raw_sales", 1, ["FOREIGN_KEY"]),
    ],
    downstream: [
      impactNode("t_revenue_agg", "TABLE", "revenue_agg", "analytics.mart.revenue_agg", 1, ["DBT_DEPENDENCY"]),
      impactNode("t_revenue_by_lob", "DBT_MODEL", "revenue_by_lob", "analytics.mart.revenue_by_lob", 2, ["DBT_DEPENDENCY", "VIEW_DEFINITION"]),
    ],
    requested_depth: _query.depth ?? 5,
    node_limit: _query.nodeLimit ?? 200,
    upstream_truncated: false,
    downstream_truncated: false,
  };
}


/* ---------------------------------------------------------------------------
   Ask (UX-15/UX-16) fixtures -- the real `run_agent_analysis`/`list_agent_runs`/
   `get_agent_run`/`get_agent_run_grounding_receipts` endpoints (`api.py:2912`
   onward), same standing as everything above: fixture mode mirrors their real
   wire shape, including AT-9's ambiguous-governed-term refusal, which the
   real endpoint returns as an HTTP 409 whose `detail` inlines every competing
   definition (`format_ambiguous_definition_refusal`, semantic_inference.py) --
   reproduced here byte-for-byte so `classifyAgentAskError` (./api.ts)
   exercises the exact same parse in fixture mode as it does against the real
   API. Import of `ApiError` from `./api` is a real circular module reference
   (api.ts imports these fixture functions the same way) -- safe because both
   sides only touch the other's export from inside a function body, never at
   module-evaluation time, exactly like every `makeFixtureX` call already in
   `api.ts`. */

import { ApiError } from "./api";

let agentRunSeq = 0;
function nextAgentRunId(): string {
  agentRunSeq += 1;
  return `run_ask_${agentRunSeq.toString().padStart(4, "0")}`;
}

function fragmentDigest(seed: string): string {
  const n = h(seed.length, 97) ^ h(seed.charCodeAt(0) || 1, 101);
  return `sha256:${(n >>> 0).toString(16).padStart(8, "0")}${"0".repeat(56)}`;
}

function groundingReceiptsFor(runId: string, tableName: string): AgentRunGroundingReceiptsRead {
  const fragments = [
    {
      object_type: "TABLE",
      object_id: `t_${tableName}`,
      fragment_digest: fragmentDigest(`${runId}:table`),
      annotation_version_id: `av_${tableName}_3`,
      annotation_version: 3,
      annotation_status: "APPROVED",
      business_name: tableName.replace(/_/g, " "),
      business_description: `Steward-approved business meaning for ${tableName}, used to ground this answer.`,
      digest_verified: true,
    },
    {
      object_type: "GLOSSARY_TERM",
      object_id: "term:net_revenue",
      fragment_digest: fragmentDigest(`${runId}:term`),
      annotation_version_id: "av_net_revenue_2",
      annotation_version: 2,
      annotation_status: "APPROVED",
      business_name: "Net Revenue",
      business_description:
        "Gross bookings less refunds and intercompany transfers, per the Q3 close workpaper.",
      digest_verified: true,
    },
  ];
  return { agent_run_id: runId, fragment_count: fragments.length, fragments };
}

function agentRunRead(
  runId: string,
  datasourceId: string,
  status: string,
  overrides: Partial<AgentRunRead> = {},
): AgentRunRead {
  const now = new Date().toISOString();
  return {
    id: runId,
    organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: datasourceId,
    principal_id: "dev-fixture-user",
    status,
    generation_source: "FREEFORM_SQL",
    model_route: "default/sql-planner",
    semantic_version: "sm_2026_09@4",
    policy_version: "pol_2026_09@2",
    query_execution_id: null,
    step_trace: [],
    retrieval_evidence: [],
    grounding_fragment_digests: [],
    plan_evidence: {},
    recommended_tool_version_id: null,
    failure_reason: null,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

const FIXTURE_GROUNDING_RECEIPTS: Record<string, AgentRunGroundingReceiptsRead> = {};

const FIXTURE_AGENT_RUNS: Record<string, AgentRunRead[]> = {
  ds_snowflake_prod: [
    agentRunRead("run_ask_0002", "ds_snowflake_prod", "SUCCEEDED", {
      generation_source: "FREEFORM_SQL",
      step_trace: [
        { stage: "RETRIEVED", strategy: "DETERMINISTIC" },
        { stage: "PLANNED", strategy: "FREEFORM_SQL" },
        { stage: "EXECUTED", strategy: "FREEFORM_SQL" },
      ],
      retrieval_evidence: [
        { object_type: "TABLE", object_id: "t_orders_raw", score: 0.94 },
      ],
      plan_evidence: { strategy: "FREEFORM_SQL", confidence: 0.91 },
      created_at: "2026-09-01T15:40:00Z",
      updated_at: "2026-09-01T15:40:04Z",
    }),
    agentRunRead("run_ask_0001", "ds_snowflake_prod", "FAILED", {
      generation_source: "FREEFORM_SQL",
      failure_reason: "AMBIGUOUS_DEFINITION",
      step_trace: [{ stage: "REJECTED", strategy: "DETERMINISTIC" }],
      created_at: "2026-09-01T12:05:00Z",
      updated_at: "2026-09-01T12:05:01Z",
    }),
  ],
};

FIXTURE_AGENT_RUNS.ds_snowflake_prod!.forEach((run) => {
  FIXTURE_GROUNDING_RECEIPTS[run.id] = groundingReceiptsFor(run.id, "orders_raw");
});

function buildAmbiguityDetail(term: string): string {
  return (
    `the term '${term}' resolves to 2 equally applicable governed ` +
    "definitions for this datasource's scope; specify which business area you mean:" +
    ` [business_node=bn_finance_revenue] 'Net Revenue (Finance)' (owner: priya@tenant.example) -- ` +
    "Gross bookings less refunds and intercompany transfers, per the Q3 close workpaper." +
    ` [business_node=bn_sales_revenue] 'Net Revenue (Sales)' (owner: sam@tenant.example) -- ` +
    "Recognized bookings net of discounts, excluding renewals."
  );
}

/** `POST /v1/datasources/{id}/agent-analyses` (`run_agent_analysis`).
 *  Question text is read for a handful of trigger words so every mapped
 *  failure mode (AT-9 ambiguity, disabled datasource, policy rejection,
 *  model-route unavailable, unhandled 502) is reachable in fixture mode,
 *  not only the success path -- matching the real endpoint's own
 *  `except` clauses (`api.py:2912`) status-for-status. Anything else
 *  succeeds and is appended to this datasource's history so the screen's
 *  own history list reflects what was just asked. */
export async function makeFixtureAgentAnalysis(
  datasourceId: string,
  body: AgentAnalysisRequest,
): Promise<AgentAnalysisResponse> {
  await wait(140);
  const question = body.question.toLowerCase();

  if (question.includes("disabled")) {
    throw new ApiError(409, "datasource is disabled");
  }
  if (question.includes("ambiguous") || question.includes("mrr")) {
    throw new ApiError(409, buildAmbiguityDetail(question.includes("mrr") ? "mrr" : "revenue"));
  }
  if (question.includes("policy")) {
    throw new ApiError(422, "generated query violates row-level masking policy POL-14");
  }
  if (question.includes("unavailable")) {
    throw new ApiError(503, "no model route available for risk tier HIGH");
  }
  if (question.includes("fail") || question.includes("502")) {
    throw new ApiError(502, "agent analysis execution failed");
  }

  // DQ-3 (module 11 §9): a question that touches a table with an open quality
  // incident still SUCCEEDS (a WARNING-severity incident only warns; only a
  // CRITICAL one blocks, and that is the 422/`policy` branch above) -- but
  // the answer's own retrieval evidence and plan evidence carry the same
  // machine-readable `quality_trust_demotion`/`trust.warnings` shapes
  // `retrieval.py`/`agent_orchestrator.py` attach for real, so this screen's
  // trust-warning banner and demotion-reason display are reachable in
  // fixture mode without a real backend.
  if (question.includes("quality") || question.includes("incident")) {
    const runId = nextAgentRunId();
    const execution = {
      execution_id: `qe_${runId}`,
      status: "SUCCEEDED",
      normalized_sql:
        "SELECT date_trunc('month', order_date) AS month, SUM(net_amount) AS net_revenue\nFROM analytics.core.orders_raw\nGROUP BY 1\nORDER BY 1",
      referenced_tables: ["analytics.core.orders_raw"],
      referenced_columns: ["order_date", "net_amount"],
      column_lineage: [],
      plan_cost: 12.4,
      warehouse_query_id: `wh_${runId}`,
      row_count: 3,
      elapsed_ms: 310,
      masked_columns: [],
      rows: [
        { month: "2026-06-01", net_revenue: 1_276_400 },
        { month: "2026-07-01", net_revenue: 1_190_200 },
        { month: "2026-08-01", net_revenue: 1_244_800 },
      ],
    };
    const response: AgentAnalysisResponse = {
      agent_run_id: runId,
      status: "SUCCEEDED",
      generation_source: "FREEFORM_SQL",
      semantic_version: "sm_2026_09@4",
      policy_version: "pol_2026_09@2",
      step_trace: [
        { stage: "RETRIEVED", strategy: "DETERMINISTIC", retrieval_evidence_count: 1 },
        { stage: "RESOLVED", semantic_version: "sm_2026_09@4" },
        { stage: "PLANNED", strategy: "FREEFORM_SQL", confidence: 0.87 },
        { stage: "EXECUTED", strategy: "FREEFORM_SQL" },
      ],
      retrieval_evidence: [
        {
          object_type: "TABLE",
          object_id: "t_orders_raw",
          score: 0.71,
          reason: "direct match on 'orders' and 'revenue' in the question",
          metadata: {
            quality_trust_demotion: {
              reason: "OPEN_QUALITY_INCIDENT",
              demoted_table_ids: ["t_orders_raw"],
              worst_factor: 0.3,
            },
          },
        },
      ],
      plan_evidence: {
        strategy: "FREEFORM_SQL",
        confidence: 0.87,
        trust: {
          trust_score: 58,
          trust_grade: "C",
          factors: [
            { factor: "quality", score: 30, weight: 0.4 },
            { factor: "freshness", score: 80, weight: 0.2 },
          ],
          warnings: [
            {
              asset_id: "t_orders_raw",
              message:
                "orders_raw has 1 active quality incident (highest severity: CRITICAL). Results may be unreliable.",
              severity: "CRITICAL",
              incident_ids: ["inc_orders_raw_volume"],
            },
          ],
        },
      },
      execution,
      explanation: `Net revenue by month for the last 3 months, computed from analytics.core.orders_raw using the governed "Net Revenue" definition. TRUST WARNING: orders_raw has an open CRITICAL quality incident -- treat this answer with caution.`,
    };
    const run = agentRunRead(runId, datasourceId, "SUCCEEDED", {
      step_trace: response.step_trace,
      retrieval_evidence: response.retrieval_evidence,
      plan_evidence: response.plan_evidence,
      query_execution_id: execution.execution_id,
    });
    FIXTURE_AGENT_RUNS[datasourceId] = [run, ...(FIXTURE_AGENT_RUNS[datasourceId] ?? [])];
    FIXTURE_GROUNDING_RECEIPTS[runId] = groundingReceiptsFor(runId, "orders_raw");
    return response;
  }

  const runId = nextAgentRunId();
  const execution = {
    execution_id: `qe_${runId}`,
    status: "SUCCEEDED",
    normalized_sql:
      "SELECT date_trunc('month', order_date) AS month, SUM(net_amount) AS net_revenue\nFROM analytics.core.orders_raw\nGROUP BY 1\nORDER BY 1",
    referenced_tables: ["analytics.core.orders_raw"],
    referenced_columns: ["order_date", "net_amount"],
    column_lineage: [],
    plan_cost: 12.4,
    warehouse_query_id: `wh_${runId}`,
    row_count: 6,
    elapsed_ms: 340,
    masked_columns: [],
    rows: [
      { month: "2026-04-01", net_revenue: 1_204_500 },
      { month: "2026-05-01", net_revenue: 1_318_900 },
      { month: "2026-06-01", net_revenue: 1_276_400 },
    ],
  };
  const response: AgentAnalysisResponse = {
    agent_run_id: runId,
    status: "SUCCEEDED",
    generation_source: "FREEFORM_SQL",
    semantic_version: "sm_2026_09@4",
    policy_version: "pol_2026_09@2",
    step_trace: [
      { stage: "RETRIEVED", strategy: "DETERMINISTIC", retrieval_evidence_count: 2 },
      { stage: "RESOLVED", semantic_version: "sm_2026_09@4" },
      { stage: "PLANNED", strategy: "FREEFORM_SQL", confidence: 0.91 },
      { stage: "EXECUTED", strategy: "FREEFORM_SQL" },
    ],
    retrieval_evidence: [
      {
        object_type: "TABLE",
        object_id: "t_orders_raw",
        score: 0.94,
        reason: "direct match on 'orders' and 'revenue' in the question",
      },
      {
        object_type: "GLOSSARY_TERM",
        object_id: "term:net_revenue",
        score: 0.88,
        reason: "governed definition of 'net revenue' for this datasource's scope",
      },
    ],
    plan_evidence: { strategy: "FREEFORM_SQL", confidence: 0.91, candidate_tools_considered: 0 },
    execution,
    explanation: `Net revenue by month for the last 3 months, computed from analytics.core.orders_raw using the governed "Net Revenue" definition (gross bookings less refunds and intercompany transfers).`,
  };

  const run = agentRunRead(runId, datasourceId, "SUCCEEDED", {
    step_trace: response.step_trace,
    retrieval_evidence: response.retrieval_evidence,
    plan_evidence: response.plan_evidence,
    query_execution_id: execution.execution_id,
  });
  FIXTURE_AGENT_RUNS[datasourceId] = [run, ...(FIXTURE_AGENT_RUNS[datasourceId] ?? [])];
  FIXTURE_GROUNDING_RECEIPTS[runId] = groundingReceiptsFor(runId, "orders_raw");

  return response;
}

/** `GET /v1/datasources/{id}/agent-runs` (`list_agent_runs`). */
export async function makeFixtureAgentRuns(
  datasourceId: string,
  query: AgentRunsQuery,
): Promise<PageOf<AgentRunRead>> {
  await wait(80);
  const all = FIXTURE_AGENT_RUNS[datasourceId] ?? [];
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** `GET /v1/agent-runs/{id}` (`get_agent_run`). */
export async function makeFixtureAgentRun(agentRunId: string): Promise<AgentRunRead> {
  await wait(70);
  for (const runs of Object.values(FIXTURE_AGENT_RUNS)) {
    const run = runs.find((r) => r.id === agentRunId);
    if (run) return run;
  }
  throw new Error(`fixture: no such agent run ${agentRunId}`);
}

/** `GET /v1/agent-runs/{id}/grounding-receipts` (`get_agent_run_grounding_receipts`). */
export async function makeFixtureAgentRunGroundingReceipts(
  agentRunId: string,
): Promise<AgentRunGroundingReceiptsRead> {
  await wait(70);
  return FIXTURE_GROUNDING_RECEIPTS[agentRunId] ?? { agent_run_id: agentRunId, fragment_count: 0, fragments: [] };
}
/* ---------------------------------------------------------------------------
   UX-16: Operations — fleet-summary, analysis-runs, outbox-events/requeue,
   and the per-datasource metadata-ingestion-batches drill-down. Same standing
   as everything above this banner: every one of these stands in for a real,
   already-merged route (`operational_api.py`/`ingestion_api.py`), not an
   unbuilt one. `makeFixtureFleetSummary`'s counts are derived from the same
   fixed arrays the list fixtures below read, so the dashboard tiles and the
   lists under them can never disagree in fixture mode -- exactly the
   real backend's two independent-but-consistent queries, reproduced here as
   two functions over one shared fixture dataset instead of two disconnected
   ones.
--------------------------------------------------------------------------- */

const OPS_ORG = "00000000-0000-0000-0000-000000000001";
const OPS_DS = "ds_snowflake_prod";

const ANALYSIS_RUN_FIXTURES: AnalysisRunRead[] = [
  {
    id: "run_a1", organization_id: OPS_ORG, datasource_id: OPS_DS, resumed_from_run_id: null,
    mode: "FULL", trigger_type: "SCHEDULED", priority: 5, status: "RUNNING",
    temporal_workflow_id: "wf-a1", discovered_catalogs: 1, discovered_schemas: 5,
    discovered_tables: 210, discovered_columns: 3100, discovered_constraints: 340,
    created_objects: 4, changed_objects: 12, deprecated_objects: 0,
    profiled_tables: 88, profiled_columns: 1204, error_class: null, error_message: null,
    created_at: "2026-09-02T08:00:00Z", updated_at: "2026-09-02T08:41:00Z",
  },
  {
    id: "run_a2", organization_id: OPS_ORG, datasource_id: OPS_DS, resumed_from_run_id: null,
    mode: "INCREMENTAL", trigger_type: "SCAN_POLICY", priority: 5, status: "SUCCEEDED",
    temporal_workflow_id: "wf-a2", discovered_catalogs: 1, discovered_schemas: 5,
    discovered_tables: 210, discovered_columns: 3100, discovered_constraints: 340,
    created_objects: 0, changed_objects: 3, deprecated_objects: 0,
    profiled_tables: 210, profiled_columns: 3100, error_class: null, error_message: null,
    created_at: "2026-09-02T02:00:00Z", updated_at: "2026-09-02T02:22:00Z",
  },
  {
    id: "run_a3", organization_id: OPS_ORG, datasource_id: OPS_DS, resumed_from_run_id: null,
    mode: "INCREMENTAL", trigger_type: "SCAN_POLICY", priority: 5, status: "FAILED",
    temporal_workflow_id: "wf-a3", discovered_catalogs: 1, discovered_schemas: 5,
    discovered_tables: 0, discovered_columns: 0, discovered_constraints: 0,
    created_objects: 0, changed_objects: 0, deprecated_objects: 0,
    profiled_tables: 0, profiled_columns: 0,
    error_class: "ConnectorTimeoutError", error_message: "connector did not respond within 30s",
    created_at: "2026-09-01T20:00:00Z", updated_at: "2026-09-01T20:00:31Z",
  },
  {
    id: "run_a4", organization_id: OPS_ORG, datasource_id: OPS_DS, resumed_from_run_id: "run_a3",
    mode: "INCREMENTAL", trigger_type: "MANUAL", priority: 8, status: "PENDING",
    temporal_workflow_id: null, discovered_catalogs: 0, discovered_schemas: 0,
    discovered_tables: 0, discovered_columns: 0, discovered_constraints: 0,
    created_objects: 0, changed_objects: 0, deprecated_objects: 0,
    profiled_tables: 0, profiled_columns: 0, error_class: null, error_message: null,
    created_at: "2026-09-02T08:55:00Z", updated_at: "2026-09-02T08:55:00Z",
  },
  {
    id: "run_a5", organization_id: OPS_ORG, datasource_id: OPS_DS, resumed_from_run_id: null,
    mode: "FULL", trigger_type: "MANUAL", priority: 5, status: "CANCELLED",
    temporal_workflow_id: "wf-a5", discovered_catalogs: 1, discovered_schemas: 2,
    discovered_tables: 40, discovered_columns: 600, discovered_constraints: 55,
    created_objects: 40, changed_objects: 0, deprecated_objects: 0,
    profiled_tables: 0, profiled_columns: 0, error_class: null, error_message: null,
    created_at: "2026-09-01T15:00:00Z", updated_at: "2026-09-01T15:10:00Z",
  },
  {
    id: "run_a6", organization_id: OPS_ORG, datasource_id: OPS_DS, resumed_from_run_id: null,
    mode: "INCREMENTAL", trigger_type: "SCAN_POLICY", priority: 5, status: "SUCCEEDED",
    temporal_workflow_id: "wf-a6", discovered_catalogs: 1, discovered_schemas: 5,
    discovered_tables: 210, discovered_columns: 3100, discovered_constraints: 340,
    created_objects: 1, changed_objects: 0, deprecated_objects: 0,
    profiled_tables: 210, profiled_columns: 3100, error_class: null, error_message: null,
    created_at: "2026-09-01T02:00:00Z", updated_at: "2026-09-01T02:19:00Z",
  },
];

/** `GET /v1/organizations/{organization_id}/analysis-runs`. */
export async function makeFixtureAnalysisRuns(
  query: AnalysisRunsQuery,
): Promise<PageOf<AnalysisRunRead>> {
  await wait(90);
  let items = ANALYSIS_RUN_FIXTURES;
  if (query.runStatus) items = items.filter((r) => r.status === query.runStatus);
  if (query.datasourceId) items = items.filter((r) => r.datasource_id === query.datasourceId);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

const OUTBOX_EVENT_FIXTURES: OutboxEventRead[] = [
  {
    id: "obx_1", organization_id: OPS_ORG, aggregate_type: "AnalysisRun", aggregate_id: "run_a2",
    event_type: "analysis_run.completed", status: "PENDING", attempt_count: 0,
    next_attempt_at: "2026-09-02T02:23:00Z", last_error: null,
    occurred_at: "2026-09-02T02:22:00Z", published_at: null,
  },
  {
    id: "obx_2", organization_id: OPS_ORG, aggregate_type: "AnalysisRun", aggregate_id: "run_a3",
    event_type: "analysis_run.failed", status: "DEAD_LETTER", attempt_count: 6,
    next_attempt_at: "2026-09-02T02:00:00Z",
    last_error: "webhook delivery: connection reset by peer (6 attempts)",
    occurred_at: "2026-09-01T20:00:31Z", published_at: null,
  },
  {
    id: "obx_3", organization_id: OPS_ORG, aggregate_type: "MetadataIngestionBatch", aggregate_id: "batch_3",
    event_type: "ingestion_batch.failed", status: "DEAD_LETTER", attempt_count: 5,
    next_attempt_at: "2026-09-01T09:00:00Z",
    last_error: "downstream consumer returned 503 (5 attempts)",
    occurred_at: "2026-09-01T08:40:00Z", published_at: null,
  },
  {
    id: "obx_4", organization_id: OPS_ORG, aggregate_type: "DataSource", aggregate_id: OPS_DS,
    event_type: "datasource.health_degraded", status: "PUBLISHED", attempt_count: 1,
    next_attempt_at: "2026-09-01T06:00:00Z", last_error: null,
    occurred_at: "2026-09-01T05:58:00Z", published_at: "2026-09-01T05:58:04Z",
  },
];

/** `GET /v1/organizations/{organization_id}/outbox-events`. */
export async function makeFixtureOutboxEvents(
  query: OutboxEventsQuery,
): Promise<PageOf<OutboxEventRead>> {
  await wait(80);
  let items = OUTBOX_EVENT_FIXTURES;
  if (query.status) items = items.filter((e) => e.status === query.status);
  if (query.eventType) items = items.filter((e) => e.event_type === query.eventType);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `POST /v1/outbox-events/{event_id}/requeue` — mutates the same in-memory
 *  fixture array `makeFixtureOutboxEvents` reads, the same convention
 *  `makeFixtureDecideReview` uses above, and rejects a non-DEAD_LETTER event
 *  exactly like the real route's 409. */
export async function makeFixtureRequeueOutboxEvent(eventId: string): Promise<OutboxEventRead> {
  await wait(70);
  const event = OUTBOX_EVENT_FIXTURES.find((e) => e.id === eventId);
  if (!event) throw new Error(`fixture: no such outbox event ${eventId}`);
  if (event.status !== "DEAD_LETTER") throw new Error("only dead-letter events can be requeued");
  event.status = "PENDING";
  event.attempt_count = 0;
  event.next_attempt_at = new Date().toISOString();
  event.last_error = null;
  return event;
}

/** `GET /v1/organizations/{organization_id}/fleet-summary`. Counts are
 *  derived from `ANALYSIS_RUN_FIXTURES`/`OUTBOX_EVENT_FIXTURES` above rather
 *  than restated by hand, so the tiles this feeds can never disagree with
 *  the lists underneath them. */
export async function makeFixtureFleetSummary(organizationId: string): Promise<FleetSummaryRead> {
  await wait(60);
  const analysisRunStatuses = ANALYSIS_RUN_FIXTURES.reduce<Record<string, number>>((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});
  return {
    organization_id: organizationId,
    datasource_statuses: { ACTIVE: FIXTURE_DATASOURCES.length },
    analysis_run_statuses: analysisRunStatuses,
    scan_policies_enabled: 6,
    scan_policies_due: 2,
    pending_outbox_events: OUTBOX_EVENT_FIXTURES.filter((e) => e.status === "PENDING").length,
    dead_letter_outbox_events: OUTBOX_EVENT_FIXTURES.filter((e) => e.status === "DEAD_LETTER").length,
    generated_at: new Date().toISOString(),
  };
}

const INGESTION_BATCH_FIXTURES: Record<string, MetadataIngestionBatchRead[]> = {
  [OPS_DS]: [
    {
      id: "batch_1", organization_id: OPS_ORG, datasource_id: OPS_DS, analysis_run_id: "run_a1",
      batch_key: "2026-09-02T08:00:00Z-full", envelope_version: "1.1", producer: "snowflake-connector@1.4",
      snapshot_type: "FULL", expected_chunks: 12, received_chunks: 12, processed_chunks: 9,
      status: "PROCESSING", temporal_workflow_id: "wf-a1",
      object_counts: { tables: 210 }, change_counts: { created: 4, changed: 12 },
      submitted_by: "svc-ingest", finalized_at: "2026-09-02T08:20:00Z", completed_at: null,
      error_class: null, error_message: null,
      created_at: "2026-09-02T08:00:00Z", updated_at: "2026-09-02T08:41:00Z",
    },
    {
      id: "batch_2", organization_id: OPS_ORG, datasource_id: OPS_DS, analysis_run_id: "run_a2",
      batch_key: "2026-09-02T02:00:00Z-incr", envelope_version: "1.1", producer: "snowflake-connector@1.4",
      snapshot_type: "INCREMENTAL", expected_chunks: 3, received_chunks: 3, processed_chunks: 3,
      status: "COMPLETE", temporal_workflow_id: "wf-a2",
      object_counts: { tables: 210 }, change_counts: { changed: 3 },
      submitted_by: "svc-ingest", finalized_at: "2026-09-02T02:05:00Z", completed_at: "2026-09-02T02:22:00Z",
      error_class: null, error_message: null,
      created_at: "2026-09-02T02:00:00Z", updated_at: "2026-09-02T02:22:00Z",
    },
    {
      id: "batch_3", organization_id: OPS_ORG, datasource_id: OPS_DS, analysis_run_id: "run_a3",
      batch_key: "2026-09-01T20:00:00Z-incr", envelope_version: "1.1", producer: "snowflake-connector@1.4",
      snapshot_type: "INCREMENTAL", expected_chunks: 3, received_chunks: 1, processed_chunks: 0,
      status: "FAILED", temporal_workflow_id: "wf-a3",
      object_counts: {}, change_counts: {},
      submitted_by: "svc-ingest", finalized_at: null, completed_at: null,
      error_class: "ChunkTimeoutError", error_message: "chunk 2 of 3 was never received within the batch window",
      created_at: "2026-09-01T20:00:00Z", updated_at: "2026-09-01T20:00:31Z",
    },
  ],
};

/** `GET /v1/datasources/{datasource_id}/metadata-ingestion-batches` — the
 *  screen's secondary, per-datasource-only drill-down (see this file's
 *  banner above and `OperationsScreen.tsx`'s module comment for why there is
 *  no org-wide equivalent to fetch here instead). */
export async function makeFixtureIngestionBatches(
  datasourceId: string,
  opts: IngestionBatchesQuery,
): Promise<PageOf<MetadataIngestionBatchRead>> {
  await wait(80);
  const items = INGESTION_BATCH_FIXTURES[datasourceId] ?? [];
  const offset = opts.offset ?? 0;
  const limit = opts.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}
/* ---------------------------------------------------------------------------
   Quality — UX-15/UX-16, `QualityScreen`. Scoped to the one fixture
   datasource (`FIXTURE_DATASOURCES` above), matching how the real endpoints
   are datasource-scoped. `severity` mirrors the real values `data_quality.py`'s
   `evaluate_quality` actually assigns (`CRITICAL`/`WARNING`); `status` mirrors
   the real lifecycle (`OPEN` default, `ACKNOWLEDGED`/`RESOLVED` only reachable
   via `transition_quality_incident`).
--------------------------------------------------------------------------- */

const QUALITY_FIXTURE_DATASOURCE_ID = "ds_snowflake_prod";

const QUALITY_FIXTURE_INCIDENTS: DataQualityIncidentRead[] = [
  {
    id: "inc_raw_sales_null", organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: QUALITY_FIXTURE_DATASOURCE_ID, table_id: "t_raw_sales", table_name: "raw_sales",
    policy_id: "pol_default", latest_observation_id: "obs_9001",
    anomaly_type: "NULL_RATE_SHIFT", severity: "CRITICAL", status: "OPEN", source: "INTERNAL",
    summary: "Detected null rate shift outside the governed baseline threshold.",
    evidence: { column: "amount", baseline_null_rate: 0.01, observed_null_rate: 0.18 },
    occurrence_count: 3,
    first_observed_at: "2026-08-30T04:00:00Z", last_observed_at: "2026-09-02T04:00:00Z",
    acknowledged_by: null, acknowledged_at: null,
    resolved_by: null, resolved_at: null, resolution_reason: null,
    created_at: "2026-08-30T04:00:00Z", updated_at: "2026-09-02T04:00:00Z",
  },
  {
    id: "inc_orders_raw_volume", organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: QUALITY_FIXTURE_DATASOURCE_ID, table_id: "t_orders_raw", table_name: "orders_raw",
    policy_id: "pol_default", latest_observation_id: "obs_9002",
    anomaly_type: "VOLUME_CHANGE", severity: "WARNING", status: "ACKNOWLEDGED", source: "INTERNAL",
    summary: "Detected volume change outside the governed baseline threshold.",
    evidence: { baseline_row_count: 482_000, observed_row_count: 351_000 },
    occurrence_count: 1,
    first_observed_at: "2026-09-01T04:00:00Z", last_observed_at: "2026-09-01T04:00:00Z",
    acknowledged_by: "priya@tenant.example", acknowledged_at: "2026-09-01T09:12:00Z",
    resolved_by: null, resolved_at: null, resolution_reason: null,
    created_at: "2026-09-01T04:00:00Z", updated_at: "2026-09-01T09:12:00Z",
  },
  {
    id: "inc_customer_dim_volume", organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: QUALITY_FIXTURE_DATASOURCE_ID, table_id: "t_customer_dim", table_name: "customer_dim",
    policy_id: "pol_default", latest_observation_id: "obs_9003",
    anomaly_type: "VOLUME_CHANGE", severity: "CRITICAL", status: "OPEN", source: "INTERNAL",
    summary: "Detected volume change outside the governed baseline threshold.",
    evidence: { baseline_row_count: 118_400, observed_row_count: 4_200 },
    occurrence_count: 2,
    first_observed_at: "2026-09-01T16:00:00Z", last_observed_at: "2026-09-02T04:00:00Z",
    acknowledged_by: null, acknowledged_at: null,
    resolved_by: null, resolved_at: null, resolution_reason: null,
    created_at: "2026-09-01T16:00:00Z", updated_at: "2026-09-02T04:00:00Z",
  },
  {
    id: "inc_settlement_fact_null", organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: QUALITY_FIXTURE_DATASOURCE_ID, table_id: "t_settlement_fact", table_name: "settlement_fact",
    policy_id: "pol_default", latest_observation_id: "obs_9004",
    anomaly_type: "NULL_RATE_SHIFT", severity: "WARNING", status: "OPEN", source: "EXTERNAL",
    summary: "Detected null rate shift outside the governed baseline threshold.",
    evidence: { column: "settlement_date", baseline_null_rate: 0.0, observed_null_rate: 0.06 },
    occurrence_count: 1,
    first_observed_at: "2026-09-02T04:00:00Z", last_observed_at: "2026-09-02T04:00:00Z",
    acknowledged_by: null, acknowledged_at: null,
    resolved_by: null, resolved_at: null, resolution_reason: null,
    created_at: "2026-09-02T04:00:00Z", updated_at: "2026-09-02T04:00:00Z",
  },
  {
    id: "inc_revenue_agg_schema", organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: QUALITY_FIXTURE_DATASOURCE_ID, table_id: "t_revenue_agg", table_name: "revenue_agg",
    policy_id: "pol_default", latest_observation_id: "obs_8990",
    anomaly_type: "SCHEMA_CHANGE", severity: "WARNING", status: "RESOLVED", source: "INTERNAL",
    summary: "Detected schema change outside the governed baseline threshold.",
    evidence: { added_columns: ["lob_code"], removed_columns: [] },
    occurrence_count: 1,
    first_observed_at: "2026-08-28T04:00:00Z", last_observed_at: "2026-08-28T04:00:00Z",
    acknowledged_by: "priya@tenant.example", acknowledged_at: "2026-08-28T10:00:00Z",
    resolved_by: "priya@tenant.example", resolved_at: "2026-08-29T08:00:00Z",
    resolution_reason: "Expected — lob_code was added by the finance dbt model release.",
    created_at: "2026-08-28T04:00:00Z", updated_at: "2026-08-29T08:00:00Z",
  },
];

/** `GET /v1/datasources/{id}/quality-summary`. Rolled up from the same fixture
 *  incidents `makeFixtureQualityIncidents` reads, so the tiles and the list
 *  agree in fixture mode the way the real read model's own queries would. */
export async function makeFixtureQualitySummary(
  datasourceId: string,
): Promise<DataQualitySummaryRead> {
  await wait(70);
  const incidents = QUALITY_FIXTURE_INCIDENTS.filter((i) => i.datasource_id === datasourceId);
  const open = incidents.filter((i) => i.status !== "RESOLVED");
  return {
    datasource_id: datasourceId,
    table_count: 46,
    observed_table_count: 41,
    status_counts: { HEALTHY: 33, WARNING: 6, CRITICAL: 2, NO_BASELINE: 5 },
    open_incident_count: open.length,
    critical_incident_count: open.filter((i) => i.severity === "CRITICAL").length,
    average_quality_score: 87.4,
    last_observed_at: "2026-09-02T04:00:00Z",
    metadata_scan_age_minutes: 42,
    metadata_scan_status: "CURRENT",
    source_freshness_status: "NOT_CONFIGURED",
  };
}

/** `GET /v1/datasources/{id}/quality-incidents`. Filters by `status`/`severity`
 *  the same way the real endpoint does, purely client-side over the fixed set
 *  above. */
export async function makeFixtureQualityIncidents(
  datasourceId: string,
  query: QualityIncidentsQuery,
): Promise<PageOf<DataQualityIncidentRead>> {
  await wait(90);
  let items = QUALITY_FIXTURE_INCIDENTS.filter((i) => i.datasource_id === datasourceId);
  if (query.status) items = items.filter((i) => i.status === query.status);
  if (query.severity) items = items.filter((i) => i.severity === query.severity);
  const limit = query.limit ?? 200;
  const offset = query.offset ?? 0;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `POST /v1/quality-incidents/{id}/transition` — mutates the same in-memory
 *  fixture array `makeFixtureQualityIncidents` reads, so a transition-then-
 *  refetch in fixture mode behaves like the real endpoint, including its
 *  refusal to transition an already-RESOLVED incident. */
export async function makeFixtureTransitionQualityIncident(
  incidentId: string,
  body: DataQualityIncidentTransition,
): Promise<DataQualityIncidentRead> {
  await wait(80);
  const incident = QUALITY_FIXTURE_INCIDENTS.find((i) => i.id === incidentId);
  if (!incident) throw new Error(`fixture: no such quality incident ${incidentId}`);
  if (incident.status === "RESOLVED") {
    throw new Error("resolved incidents cannot be transitioned");
  }
  const now = new Date().toISOString();
  incident.status = body.status;
  if (body.status === "ACKNOWLEDGED") {
    incident.acknowledged_by = "dev-fixture-user";
    incident.acknowledged_at = now;
  } else {
    incident.resolved_by = "dev-fixture-user";
    incident.resolved_at = now;
    incident.resolution_reason = body.reason;
  }
  incident.updated_at = now;
  return { ...incident };
}

/** `GET .../quality-incidents/{id}/triage` -- a lightweight client-side
 *  mirror of `dq_triage_agent.suggest_triage`'s branching (not a port of
 *  its full logic; fixture mode only needs a plausible, varied hint per
 *  anomaly type/source, not byte-identical text). */
export async function makeFixtureQualityIncidentTriage(
  incidentId: string,
): Promise<DataQualityIncidentTriageRead> {
  await wait(70);
  const incident = QUALITY_FIXTURE_INCIDENTS.find((i) => i.id === incidentId);
  if (!incident) throw new Error(`fixture: no such quality incident ${incidentId}`);
  if (incident.source === "EXTERNAL") {
    return {
      incident_id: incidentId,
      anomaly_type: incident.anomaly_type,
      likely_causes: [
        "This incident was reconciled from a third-party detector, not computed by Atlas.",
      ],
      recommended_next_steps: ["Open this incident in the originating vendor's console."],
      basis: [],
    };
  }
  const byType: Record<string, { causes: string[]; steps: string[]; basis: string[] }> = {
    VOLUME_CHANGE: {
      causes: ["Row count changed materially versus the rolling baseline for this table."],
      steps: ["Check the most recent ingestion batch or analysis run for a failure or re-run."],
      basis: ["volume_change_percent"],
    },
    NULL_RATE_SHIFT: {
      causes: ["At least one column's null rate shifted versus its baseline."],
      steps: ["Check whether the source added a new optional field or an upstream join started dropping matches."],
      basis: ["max_null_rate_change_percent", "affected_column_ids"],
    },
    SCHEMA_CHANGE: {
      causes: ["The table's column set, types, or ordering changed versus the last scan."],
      steps: ["Coordinate with the source system owner before this table is queried again."],
      basis: ["schema_fingerprint_changed"],
    },
  };
  const match = byType[incident.anomaly_type] ?? {
    causes: [`No structured triage rule is registered for anomaly type "${incident.anomaly_type}".`],
    steps: ["Review the incident's own evidence and summary directly."],
    basis: [],
  };
  const causes = [...match.causes];
  const basis = [...match.basis];
  if (incident.occurrence_count > 1) {
    causes.push(
      `This is a recurring incident -- it has fired ${incident.occurrence_count} times.`,
    );
    basis.push("occurrence_count");
  }
  return {
    incident_id: incidentId,
    anomaly_type: incident.anomaly_type,
    likely_causes: causes,
    recommended_next_steps: match.steps,
    basis,
  };
}

/* ---------------------------------------------------------------------------
   UX-16 fixtures — Business meaning. Same standing as everything above: a
   stand-in for `npm run dev`/`npm run test` with no backend running, shaped
   exactly like `list_business_annotations`/`get_table_business_annotation`/
   `get_business_map` (`semantic_intelligence_api.py`) really return. All
   eight rows live under the one fixture datasource (`ds_snowflake_prod`,
   above); a couple of table ids (`t_orders_raw`, `t_revenue_agg`) are shared
   with the lineage-impact fixture on purpose, so a table's business meaning
   and its lineage tell one consistent fixture-mode story rather than two
   unrelated ones.
--------------------------------------------------------------------------- */

const BIZ_ORG = "00000000-0000-0000-0000-000000000001";
const BIZ_DS = "ds_snowflake_prod";

const BIZ_DOMAINS = {
  fin: { id: "dom_fin", key: "finance", name: "Finance" },
  risk: { id: "dom_risk", key: "risk", name: "Risk" },
  retail: { id: "dom_retail", key: "retail", name: "Retail" },
} as const;

const BIZ_ENTITIES = {
  fin_customer: { id: "ent_fin_customer", key: "customer", name: "Customer", domain: BIZ_DOMAINS.fin },
  fin_account: { id: "ent_fin_account", key: "account", name: "Account", domain: BIZ_DOMAINS.fin },
  fin_transaction: { id: "ent_fin_transaction", key: "transaction", name: "Transaction", domain: BIZ_DOMAINS.fin },
  risk_exposure: { id: "ent_risk_exposure", key: "exposure", name: "Counterparty Exposure", domain: BIZ_DOMAINS.risk },
  risk_limit: { id: "ent_risk_limit", key: "limit", name: "Credit Limit", domain: BIZ_DOMAINS.risk },
  retail_order: { id: "ent_retail_order", key: "order", name: "Order", domain: BIZ_DOMAINS.retail },
} as const;

function bizAnnotation(
  tableId: string,
  schemaName: string,
  tableName: string,
  entity: (typeof BIZ_ENTITIES)[keyof typeof BIZ_ENTITIES],
  fields: Pick<
    MetadataBusinessAnnotationRead,
    | "business_name"
    | "business_description"
    | "table_role"
    | "grain_statement"
    | "synonyms"
    | "suggested_questions"
    | "tags"
    | "confidence"
    | "approved_by"
    | "approved_at"
  >,
): MetadataBusinessAnnotationRead {
  return {
    id: `ann_${tableId}`,
    organization_id: BIZ_ORG,
    datasource_id: BIZ_DS,
    table_id: tableId,
    schema_name: schemaName,
    table_name: tableName,
    domain_id: entity.domain.id,
    domain_key: entity.domain.key,
    domain_name: entity.domain.name,
    entity_id: entity.id,
    entity_key: entity.key,
    entity_name: entity.name,
    source_proposal_id: `prop_${tableId}`,
    version: 1,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: fields.approved_at,
    ...fields,
  };
}

const BUSINESS_ANNOTATION_FIXTURES: MetadataBusinessAnnotationRead[] = [
  bizAnnotation("t_customer_dim", "core", "customer_dim", BIZ_ENTITIES.fin_customer, {
    business_name: "Customer",
    business_description: "One row per customer the organization has a banking relationship with.",
    table_role: "DIMENSION",
    grain_statement: "One row per customer_id.",
    synonyms: ["client", "account holder"],
    suggested_questions: ["How many active customers do we have?", "Which customers opened an account this quarter?"],
    tags: ["pii", "core"],
    confidence: 0.93,
    approved_by: "priya@tenant.example",
    approved_at: "2026-08-14T00:00:00Z",
  }),
  bizAnnotation("t_account_dim", "core", "account_dim", BIZ_ENTITIES.fin_account, {
    business_name: "Account",
    business_description: "One row per open or closed deposit account.",
    table_role: "DIMENSION",
    grain_statement: "One row per account_id.",
    synonyms: ["deposit account"],
    suggested_questions: ["How many accounts were closed last month?"],
    tags: ["core"],
    confidence: 0.9,
    approved_by: "priya@tenant.example",
    approved_at: "2026-08-14T00:00:00Z",
  }),
  bizAnnotation("t_transaction_fact", "mart", "transaction_fact", BIZ_ENTITIES.fin_transaction, {
    business_name: "Transaction",
    business_description: "One row per posted ledger transaction, debit or credit.",
    table_role: "FACT",
    grain_statement: "One row per transaction_id.",
    synonyms: ["posting", "ledger entry"],
    suggested_questions: ["What is the average transaction value by product?"],
    tags: ["core", "finance"],
    confidence: 0.88,
    approved_by: "priya@tenant.example",
    approved_at: "2026-08-15T00:00:00Z",
  }),
  bizAnnotation("t_revenue_agg", "mart", "revenue_agg", BIZ_ENTITIES.fin_transaction, {
    business_name: "Net revenue, daily",
    business_description: "Net revenue aggregated per line of business per day, intercompany transfers excluded.",
    table_role: "FACT_AGGREGATE",
    grain_statement: "One row per line_of_business per day.",
    synonyms: ["daily revenue", "topline"],
    suggested_questions: ["What was net revenue for Retail last quarter?"],
    tags: ["finance", "certified"],
    confidence: 0.95,
    approved_by: "priya@tenant.example",
    approved_at: "2026-08-20T00:00:00Z",
  }),
  bizAnnotation("t_exposure_snapshot", "risk", "exposure_snapshot", BIZ_ENTITIES.risk_exposure, {
    business_name: "Counterparty exposure",
    business_description: "Daily snapshot of exposure at default per counterparty.",
    table_role: "FACT_SNAPSHOT",
    grain_statement: "One row per counterparty_id per snapshot_date.",
    synonyms: ["exposure at default", "EAD"],
    suggested_questions: ["Which counterparties exceed their credit limit today?"],
    tags: ["risk", "restricted"],
    confidence: 0.91,
    approved_by: "risk-lead@tenant.example",
    approved_at: "2026-08-18T00:00:00Z",
  }),
  bizAnnotation("t_limit_dim", "risk", "limit_dim", BIZ_ENTITIES.risk_limit, {
    business_name: "Credit limit",
    business_description: "One row per credit limit assigned to a counterparty, current and historical.",
    table_role: "DIMENSION",
    grain_statement: "One row per limit_id.",
    synonyms: ["credit line"],
    suggested_questions: ["Which counterparties had their limit reduced this month?"],
    tags: ["risk"],
    confidence: 0.86,
    approved_by: "risk-lead@tenant.example",
    approved_at: "2026-08-18T00:00:00Z",
  }),
  bizAnnotation("t_orders_raw", "raw", "orders_raw", BIZ_ENTITIES.retail_order, {
    business_name: "Order",
    business_description: "One row per customer order as captured at checkout, before fulfillment.",
    table_role: "FACT",
    grain_statement: "One row per order_id.",
    synonyms: ["purchase", "checkout"],
    suggested_questions: ["How many orders were placed yesterday?"],
    tags: ["retail"],
    confidence: 0.82,
    approved_by: "retail-owner@tenant.example",
    approved_at: "2026-08-19T00:00:00Z",
  }),
  bizAnnotation("t_customer360", "mart", "customer_360", BIZ_ENTITIES.retail_order, {
    business_name: "Customer 360 (retail)",
    business_description: "Curated, order-centric view of a retail customer's activity across channels.",
    table_role: "MART",
    grain_statement: "One row per customer_id.",
    synonyms: ["customer profile"],
    suggested_questions: ["Which customers have the highest lifetime order value?"],
    tags: ["retail", "certified"],
    confidence: 0.89,
    approved_by: "retail-owner@tenant.example",
    approved_at: "2026-08-21T00:00:00Z",
  }),
];

function matchesBusinessAnnotationQuery(
  a: MetadataBusinessAnnotationRead,
  datasourceId: string,
): boolean {
  return a.datasource_id === datasourceId;
}

/** `GET /v1/datasources/{id}/business-annotations`. Offset/limit paged, no
 *  server-side free-text filter -- mirrors the real route's own contract
 *  exactly (see this function's twin in `api.ts`). */
export async function makeFixtureBusinessAnnotations(
  query: BusinessAnnotationsQuery,
): Promise<PageOf<MetadataBusinessAnnotationRead>> {
  await wait(90);
  const items = BUSINESS_ANNOTATION_FIXTURES.filter((a) =>
    matchesBusinessAnnotationQuery(a, query.datasourceId),
  );
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `GET /v1/metadata/tables/{table_id}/business-annotation`. Resolves by
 *  table id alone -- same permalink contract as the real endpoint, and as
 *  `makeFixtureEvidence` gives `fetchAssetEvidence`. Throws (never returns
 *  `null`) for an unknown table id, matching the real endpoint's 404. */
export async function makeFixtureTableBusinessAnnotation(
  tableId: string,
): Promise<MetadataBusinessAnnotationRead> {
  await wait(70);
  const found = BUSINESS_ANNOTATION_FIXTURES.find((a) => a.table_id === tableId);
  if (!found) throw new Error("approved business annotation not found");
  return found;
}

/** `GET /v1/organizations/{id}/business-map`. Builds the same domain/entity/
 *  table node-and-edge shape `get_business_map` computes from the real
 *  `MetadataBusinessAnnotation` + `MetadataConstraint` rows -- including one
 *  real-shaped cross-domain edge (risk's exposure snapshot references fin's
 *  account dimension), the same kind of edge the real endpoint derives from
 *  an actual foreign key that crosses a `BusinessDomain` boundary. */
export async function makeFixtureBusinessMap(_query: BusinessMapQuery): Promise<BusinessMapRead> {
  await wait(100);
  const nodes = new Map<string, BusinessMapNodeRead>();
  const edges = new Map<string, BusinessMapEdgeRead>();

  for (const a of BUSINESS_ANNOTATION_FIXTURES) {
    const domainNode = `domain:${a.domain_id}`;
    const entityNode = `entity:${a.entity_id}`;
    const tableNode = `table:${a.table_id}`;
    nodes.set(domainNode, {
      id: domainNode, node_type: "DOMAIN", label: a.domain_name, parent_id: null,
      metadata: { domain_key: a.domain_key },
    });
    nodes.set(entityNode, {
      id: entityNode, node_type: "ENTITY", label: a.entity_name, parent_id: domainNode,
      metadata: { entity_key: a.entity_key },
    });
    nodes.set(tableNode, {
      id: tableNode, node_type: "TABLE", label: `${a.schema_name}.${a.table_name}`, parent_id: entityNode,
      metadata: { datasource_id: a.datasource_id, table_role: a.table_role, grain: a.grain_statement },
    });
    edges.set(`contains:${a.domain_id}:${a.entity_id}`, {
      id: `contains:${a.domain_id}:${a.entity_id}`, edge_type: "DOMAIN_CONTAINS_ENTITY",
      source_node_id: domainNode, target_node_id: entityNode, evidence: { status: "APPROVED" },
    });
    edges.set(`represents:${a.entity_id}:${a.table_id}`, {
      id: `represents:${a.entity_id}:${a.table_id}`, edge_type: "ENTITY_REPRESENTED_BY_TABLE",
      source_node_id: entityNode, target_node_id: tableNode, evidence: { annotation_version: a.version },
    });
  }

  edges.set("cross-domain:fk_exposure_account", {
    id: "cross-domain:fk_exposure_account",
    edge_type: "CROSS_DOMAIN_FOREIGN_KEY",
    source_node_id: "table:t_exposure_snapshot",
    target_node_id: "table:t_account_dim",
    evidence: {
      constraint_id: "fk_exposure_account",
      source_domain: BIZ_DOMAINS.risk.key,
      target_domain: BIZ_DOMAINS.fin.key,
      source_columns: ["account_id"],
      target_columns: ["account_id"],
    },
  });

  const nodeValues = [...nodes.values()];
  const edgeValues = [...edges.values()];
  return {
    organization_id: BIZ_ORG,
    nodes: nodeValues,
    edges: edgeValues,
    domain_count: nodeValues.filter((n) => n.node_type === "DOMAIN").length,
    entity_count: nodeValues.filter((n) => n.node_type === "ENTITY").length,
    table_count: nodeValues.filter((n) => n.node_type === "TABLE").length,
    cross_domain_edge_count: edgeValues.filter((e) => e.edge_type === "CROSS_DOMAIN_FOREIGN_KEY").length,
    truncated: false,
  };
}
/* ---------------------------------------------------------------------------
   Semantics (UX-15/UX-16) — project picker -> project-scoped semantic model
   versions -> their metric versions -> UX-18's consumer footer. `proj_core`
   below shares its id with `FIXTURE_DATASOURCES[0].project_id` above so the
   two fixture worlds agree with each other, the same way the real project id
   would tie a datasource and a semantic model together in production.
--------------------------------------------------------------------------- */

const FIXTURE_PROJECTS: ProjectRead[] = [
  {
    id: "proj_core", organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_fin", data_domain_id: "dom_fin",
    name: "Core Finance", slug: "core-finance", status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  },
  {
    id: "proj_retail", organization_id: "00000000-0000-0000-0000-000000000001",
    line_of_business_id: "lob_retail", data_domain_id: "dom_retail",
    name: "Retail Analytics", slug: "retail-analytics", status: "ACTIVE",
    created_at: "2026-02-01T00:00:00Z", updated_at: "2026-08-15T00:00:00Z",
  },
];

const FIXTURE_MODELS: Record<string, SemanticModelVersionRead[]> = {
  proj_core: [
    {
      id: "smv_core_3", organization_id: "00000000-0000-0000-0000-000000000001", project_id: "proj_core",
      version: 3, name: "Core Finance Semantic Model",
      change_summary: "Add exposure-at-default dimension",
      status: "PUBLISHED", created_by: "priya@tenant.example", approved_by: "fin-steward@tenant.example",
      approved_at: "2026-08-20T10:00:00Z", published_at: "2026-08-20T10:05:00Z",
      based_on_version_id: "smv_core_2", created_at: "2026-08-18T09:00:00Z", updated_at: "2026-08-20T10:05:00Z",
    },
    {
      id: "smv_core_4", organization_id: "00000000-0000-0000-0000-000000000001", project_id: "proj_core",
      version: 4, name: "Core Finance Semantic Model",
      change_summary: "Draft: exclude intercompany transfers from net revenue",
      status: "DRAFT", created_by: "priya@tenant.example", approved_by: null, approved_at: null,
      published_at: null, based_on_version_id: "smv_core_3",
      created_at: "2026-08-28T09:00:00Z", updated_at: "2026-08-28T09:00:00Z",
    },
  ],
  proj_retail: [
    {
      id: "smv_retail_1", organization_id: "00000000-0000-0000-0000-000000000001", project_id: "proj_retail",
      version: 1, name: "Retail Customer 360 Semantic Model",
      change_summary: "Initial published model",
      status: "PUBLISHED", created_by: "retail-owner@tenant.example",
      approved_by: "retail-steward@tenant.example",
      approved_at: "2026-07-01T10:00:00Z", published_at: "2026-07-01T10:05:00Z",
      based_on_version_id: null, created_at: "2026-06-28T09:00:00Z", updated_at: "2026-07-01T10:05:00Z",
    },
  ],
};

const FIXTURE_METRICS: Record<string, SemanticMetricVersionRead[]> = {
  smv_core_3: [
    {
      id: "smtv_net_revenue_1", semantic_model_version_id: "smv_core_3", metric_id: "sm_net_revenue",
      metric_slug: "net_revenue", metric_name: "Net Revenue", version: 1, status: "PUBLISHED",
      description: "Total revenue net of intercompany transfers and reversals.",
      aggregation: "SUM", grain: "daily", source_table_id: "t_ledger_entry",
      measure_column_id: "col_amount", default_time_column_id: "col_posted_at",
      allowed_dimension_column_ids: ["col_lob", "col_domain"],
      fingerprint: "fp_net_revenue_1", created_by: "priya@tenant.example",
      created_at: "2026-08-18T09:10:00Z",
    },
    {
      id: "smtv_ead_1", semantic_model_version_id: "smv_core_3", metric_id: "sm_ead",
      metric_slug: "exposure_at_default", metric_name: "Exposure at Default", version: 1, status: "PUBLISHED",
      description: "Sum of outstanding exposure at default across active positions.",
      aggregation: "SUM", grain: "daily", source_table_id: "t_position",
      measure_column_id: "col_exposure", default_time_column_id: "col_as_of_date",
      allowed_dimension_column_ids: ["col_counterparty", "col_instrument"],
      fingerprint: "fp_ead_1", created_by: "priya@tenant.example", created_at: "2026-08-19T09:10:00Z",
    },
  ],
  smv_core_4: [],
  smv_retail_1: [
    {
      id: "smtv_ltv_1", semantic_model_version_id: "smv_retail_1", metric_id: "sm_ltv",
      metric_slug: "customer_ltv", metric_name: "Customer Lifetime Value", version: 1, status: "PUBLISHED",
      description: "Projected lifetime value per customer, trailing 24 months of orders.",
      aggregation: "AVG", grain: "monthly", source_table_id: "t_customer",
      measure_column_id: "col_ltv", default_time_column_id: "col_snapshot_month",
      allowed_dimension_column_ids: ["col_segment"],
      fingerprint: "fp_ltv_1", created_by: "retail-owner@tenant.example",
      created_at: "2026-06-28T09:10:00Z",
    },
  ],
};

const FIXTURE_MODEL_CONSUMERS: Record<string, ConsumerFooterRead> = {
  smv_core_3: {
    resource_type: "semantic_model_version", resource_id: "smv_core_3", version: 3,
    generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 482,
    consumers: [
      { consumer_id: "agent:revenue_analyst", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 310, last_consumed_at: "2026-09-01T14:00:00Z" },
      { consumer_id: "context_product:customer360", consumer_type: "CONTEXT_PRODUCT", channel: "CONTEXT_PRODUCT", consumption_count: 172, last_consumed_at: "2026-08-31T09:00:00Z" },
    ],
    total_consumers: 2,
  },
  smv_core_4: {
    resource_type: "semantic_model_version", resource_id: "smv_core_4", version: 4,
    generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 0, consumers: [], total_consumers: 0,
  },
  smv_retail_1: {
    resource_type: "semantic_model_version", resource_id: "smv_retail_1", version: 1,
    generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 205,
    consumers: [
      { consumer_id: "agent:retail_reporter", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 205, last_consumed_at: "2026-09-01T08:30:00Z" },
    ],
    total_consumers: 1,
  },
};

const FIXTURE_METRIC_CONSUMERS: Record<string, ConsumerFooterRead> = {
  smtv_net_revenue_1: {
    resource_type: "semantic_metric_version", resource_id: "smtv_net_revenue_1", version: 1,
    generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 96,
    consumers: [
      { consumer_id: "agent:revenue_analyst", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 96, last_consumed_at: "2026-09-01T14:02:00Z" },
    ],
    total_consumers: 1,
  },
  smtv_ead_1: {
    resource_type: "semantic_metric_version", resource_id: "smtv_ead_1", version: 1,
    generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 40,
    consumers: [
      { consumer_id: "agent:risk_analyst", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 40, last_consumed_at: "2026-08-30T11:00:00Z" },
    ],
    total_consumers: 1,
  },
  smtv_ltv_1: {
    resource_type: "semantic_metric_version", resource_id: "smtv_ltv_1", version: 1,
    generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 58,
    consumers: [
      { consumer_id: "agent:retail_reporter", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 58, last_consumed_at: "2026-09-01T08:31:00Z" },
    ],
    total_consumers: 1,
  },
};

/** `GET /v1/organizations/{id}/projects` (`operational_api.py::list_organization_projects`). */
export async function makeFixtureOrgProjects(): Promise<PageOf<ProjectRead>> {
  await wait(60);
  return { items: FIXTURE_PROJECTS, limit: 500, offset: 0, total: FIXTURE_PROJECTS.length };
}

/** `GET /v1/projects/{id}/semantic-model-versions`. */
export async function makeFixtureSemanticModelVersions(
  projectId: string,
  opts: SemanticPageQuery,
): Promise<PageOf<SemanticModelVersionRead>> {
  await wait(80);
  const items = FIXTURE_MODELS[projectId] ?? [];
  const offset = opts.offset ?? 0;
  const limit = opts.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `GET /v1/semantic-model-versions/{id}/metrics`. */
export async function makeFixtureSemanticMetricVersions(
  modelVersionId: string,
  opts: SemanticPageQuery,
): Promise<PageOf<SemanticMetricVersionRead>> {
  await wait(70);
  const items = FIXTURE_METRICS[modelVersionId] ?? [];
  const offset = opts.offset ?? 0;
  const limit = opts.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `GET /v1/semantic-model-versions/{id}/consumers` (UX-18). */
export async function makeFixtureSemanticModelConsumers(
  modelVersionId: string,
): Promise<ConsumerFooterRead> {
  await wait(70);
  return (
    FIXTURE_MODEL_CONSUMERS[modelVersionId] ?? {
      resource_type: "semantic_model_version", resource_id: modelVersionId, version: null,
      generated_at: new Date().toISOString(), total_consumption_events: 0, consumers: [], total_consumers: 0,
    }
  );
}

/** `GET /v1/semantic-metric-versions/{id}/consumers` (UX-18). */
export async function makeFixtureSemanticMetricConsumers(
  metricVersionId: string,
): Promise<ConsumerFooterRead> {
  await wait(70);
  return (
    FIXTURE_METRIC_CONSUMERS[metricVersionId] ?? {
      resource_type: "semantic_metric_version", resource_id: metricVersionId, version: null,
      generated_at: new Date().toISOString(), total_consumption_events: 0, consumers: [], total_consumers: 0,
    }
  );
}

/* ---------------------------------------------------------------------------
   Sources — UX-15/UX-16 follow-on. `GET /v1/datasources/{id}/health`
   (`operational_api.py::get_datasource_health`) composes `ConnectorHealthScoreRead`
   from `aida.connector_health.compute_connector_health`'s five weighted
   factors (RUN_SUCCESS_RATE/35, STALENESS/25, FAILURE_STREAK/20,
   PROFILING_COVERAGE/10, DATASOURCE_ENABLEMENT/10) and the same
   score-to-status thresholds the real function uses (no runs -> UNKNOWN;
   any blocker -> DEGRADED at score>=60 else CRITICAL; otherwise
   HEALTHY>=85, DEGRADED>=60, else CRITICAL). Deterministic per `datasourceId`
   (reusing this file's own `h()` hash) so a given fixture source's score does
   not reshuffle on every reload, the same stability `rowAt` already gives the
   catalog fixtures. Works for ANY datasource id, not only the ones in
   `FIXTURE_DATASOURCES` above, since the real endpoint is scoped by id alone. */
import type { ConnectorHealthFactorRead, ConnectorHealthScoreRead } from "./types";

export async function makeFixtureDatasourceHealth(
  datasourceId: string,
): Promise<ConnectorHealthScoreRead> {
  await wait(65);
  const seed = Array.from(datasourceId).reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
  const hasRuns = h(seed, 101) % 100 >= 12; // most sources have run history
  const disabled = h(seed, 103) % 100 < 8;
  const streak = hasRuns ? h(seed, 107) % 5 : 0;

  const runSuccess = hasRuns ? Math.round((35 * (h(seed, 109) % 101)) / 100 * 100) / 100 : 17.5;
  const staleness = hasRuns ? Math.round((25 * (h(seed, 113) % 101)) / 100 * 100) / 100 : 12.5;
  const failureStreak = streak === 0 ? 20 : streak === 1 ? 12 : streak === 2 ? 6 : 0;
  const profiling = hasRuns ? Math.round((10 * (h(seed, 127) % 101)) / 100 * 100) / 100 : 5;
  const enablement = disabled ? 0 : 10;

  const factors: ConnectorHealthFactorRead[] = [
    {
      name: "RUN_SUCCESS_RATE",
      score: runSuccess,
      maximum: 35,
      reason: hasRuns
        ? `${Math.round((runSuccess / 35) * 100)}% of recent completed runs succeeded.`
        : "No completed or failed runs are recorded yet; score is neutral.",
      evidence: hasRuns
        ? { successful_runs: Math.round(runSuccess), terminal_runs: 35 }
        : { successful_runs: 0, terminal_runs: 0, success_rate: null },
    },
    {
      name: "STALENESS",
      score: staleness,
      maximum: 25,
      reason: hasRuns
        ? "Most recent run is within the expected scan interval."
        : "No run history to measure staleness against; score is neutral.",
      evidence: { minutes_since_last_run: hasRuns ? h(seed, 131) % 720 : null },
    },
    {
      name: "FAILURE_STREAK",
      score: failureStreak,
      maximum: 20,
      reason:
        streak === 0
          ? "The most recent run succeeded."
          : `The last ${streak} run${streak === 1 ? "" : "s"} failed consecutively.`,
      evidence: { current_failure_streak: streak },
    },
    {
      name: "PROFILING_COVERAGE",
      score: profiling,
      maximum: 10,
      reason: hasRuns
        ? `${Math.round((profiling / 10) * 100)}% of discovered tables have a recent profile.`
        : "No run history; profiling coverage is unknown.",
      evidence: { profiled_ratio: hasRuns ? Math.round((profiling / 10) * 100) / 100 : null },
    },
    {
      name: "DATASOURCE_ENABLEMENT",
      score: enablement,
      maximum: 10,
      reason: disabled
        ? "The datasource is administratively disabled."
        : "The datasource status is ACTIVE.",
      evidence: { datasource_status: disabled ? "DISABLED" : "ACTIVE" },
    },
  ];

  const blockers: string[] = [];
  if (!hasRuns) blockers.push("NO_RUN_HISTORY");
  else if (h(seed, 137) % 100 < 6) blockers.push("NO_SUCCESSFUL_RUN");
  if (disabled) blockers.push("DATASOURCE_DISABLED");
  if (streak >= 3) blockers.push("REPEATED_FAILURES");

  const rawScore = factors.reduce((acc, f) => acc + f.score, 0);
  const score = Math.round(Math.max(0, Math.min(100, rawScore)));

  const status = !hasRuns
    ? "UNKNOWN"
    : blockers.length > 0
      ? score >= 60
        ? "DEGRADED"
        : "CRITICAL"
      : score >= 85
        ? "HEALTHY"
        : score >= 60
          ? "DEGRADED"
          : "CRITICAL";

  return {
    datasource_id: datasourceId,
    score,
    status,
    factors,
    blockers,
    computed_at: new Date().toISOString(),
  };
}

/* ---------------------------------------------------------------------------
   UX-16: Relationships — fixtures for N4's review queue (`compose_
   relationship_candidate_review_queue`) plus RL-6's single/bulk decision and
   RL-7's confidence-calibration endpoints. Mutates the in-memory candidate
   array on decide, the same way `makeFixtureDecideReview` does for
   `REVIEW_FIXTURE_PROPOSALS`, so fixture-mode decide-then-refetch behaves
   like the real maker-checker endpoints.

   `diff` entries are built the same way the real endpoint builds them
   (`relationship_candidate_review.diff_relationship_candidate`, reusing
   SM-7's `diff_semantic_object` with `before=None`): one `"added"` entry per
   key of the flat snapshot, in the diff engine's own `sorted(keys)` order —
   confidence, confidence_signals, detection_rule, source_column,
   source_table, target_column, target_table — not source-first insertion
   order, so a screen that (wrongly) assumed field order would fail against
   fixtures the same way it would against the real endpoint.
--------------------------------------------------------------------------- */

interface FixtureRelationshipSignal {
  name: string;
  score: number;
  maximum: number;
  reason: string;
}

interface FixtureRelationshipCandidate {
  candidate: RelationshipCandidateRead;
  sourceTable: string;
  sourceColumn: string;
  targetTable: string;
  targetColumn: string;
  signals: FixtureRelationshipSignal[];
  impact: RelationshipCandidateImpactRead;
}

function relationshipCandidateRead(
  overrides: Partial<RelationshipCandidateRead> & { id: string },
): RelationshipCandidateRead {
  return {
    organization_id: "00000000-0000-0000-0000-000000000001",
    datasource_id: "ds_snowflake_prod",
    target_datasource_id: "ds_snowflake_prod",
    source_table_id: `t_${overrides.id}_src`,
    source_column_id: `c_${overrides.id}_src`,
    target_table_id: `t_${overrides.id}_tgt`,
    target_column_id: `c_${overrides.id}_tgt`,
    detection_rule: "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
    confidence: 0.9,
    evidence: {},
    status: "PENDING",
    created_by: "relationship_discovery_agent",
    reviewed_by: null,
    review_reason: null,
    reviewed_at: null,
    created_at: "2026-08-28T09:00:00Z",
    updated_at: "2026-08-28T09:00:00Z",
    ...overrides,
  };
}

/** Descending impact order on purpose — see this section's own note above:
 *  a screen must render this order as-is, never re-sort by confidence or id. */
const RELATIONSHIP_CANDIDATE_FIXTURES: FixtureRelationshipCandidate[] = [
  {
    candidate: relationshipCandidateRead({
      id: "rc_1",
      detection_rule: "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
      confidence: 0.9,
      evidence: { confidence_algorithm_version: "relationship-confidence-signals-v1" },
    }),
    sourceTable: "analytics.core.orders_raw",
    sourceColumn: "customer_id",
    targetTable: "analytics.core.customer_dim",
    targetColumn: "customer_id",
    signals: [
      { name: "primary_key_target", score: 0.7, maximum: 0.7, reason: "target column is a declared PRIMARY KEY" },
      { name: "column_name_match", score: 0.1, maximum: 0.1, reason: "exact, case-insensitive name match" },
      { name: "physical_type_match", score: 0.1, maximum: 0.1, reason: "exact dialect type match" },
    ],
    impact: { impact_score: 138, source_table_impact: 81, target_table_impact: 57, depth: 3, node_limit: 100, truncated: false },
  },
  {
    candidate: relationshipCandidateRead({
      id: "rc_2",
      detection_rule: "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
      confidence: 0.9,
      evidence: { confidence_algorithm_version: "relationship-confidence-signals-v1" },
    }),
    sourceTable: "analytics.core.settlement_instruction",
    sourceColumn: "counterparty_id",
    targetTable: "analytics.core.counterparty_dim",
    targetColumn: "counterparty_id",
    signals: [
      { name: "primary_key_target", score: 0.7, maximum: 0.7, reason: "target column is a declared PRIMARY KEY" },
      { name: "column_name_match", score: 0.1, maximum: 0.1, reason: "exact, case-insensitive name match" },
      { name: "physical_type_match", score: 0.1, maximum: 0.1, reason: "exact dialect type match" },
    ],
    impact: { impact_score: 91, source_table_impact: 40, target_table_impact: 51, depth: 3, node_limit: 100, truncated: false },
  },
  {
    candidate: relationshipCandidateRead({
      id: "rc_3",
      detection_rule: "CANONICAL_NAME_TYPE_FAMILY_TO_PRIMARY_KEY_CROSS_SOURCE_V1",
      confidence: 0.75,
      evidence: { confidence_algorithm_version: "relationship-confidence-signals-v1" },
    }),
    sourceTable: "analytics.raw.collateral_position",
    sourceColumn: "instrument_ref",
    targetTable: "analytics.core.instrument_dim",
    targetColumn: "instrument_id",
    signals: [
      { name: "primary_key_target", score: 0.55, maximum: 0.55, reason: "target column is a declared PRIMARY KEY (cross-source base)" },
      { name: "column_name_match", score: 0.1, maximum: 0.1, reason: "canonical/naming-convention-normalized match only" },
      { name: "physical_type_match", score: 0.1, maximum: 0.1, reason: "exact dialect type match" },
    ],
    impact: { impact_score: 47, source_table_impact: 22, target_table_impact: 25, depth: 3, node_limit: 100, truncated: false },
  },
  {
    candidate: relationshipCandidateRead({
      id: "rc_4",
      detection_rule: "CANONICAL_NAME_TYPE_FAMILY_TO_PRIMARY_KEY_CROSS_SOURCE_V1",
      confidence: 0.65,
      evidence: { confidence_algorithm_version: "relationship-confidence-signals-v1" },
    }),
    sourceTable: "analytics.raw.limit_utilization",
    sourceColumn: "acct_no",
    targetTable: "analytics.core.account_dim",
    targetColumn: "account_id",
    signals: [
      { name: "primary_key_target", score: 0.55, maximum: 0.55, reason: "target column is a declared PRIMARY KEY (cross-source base)" },
      { name: "column_name_match", score: 0.1, maximum: 0.1, reason: "exact, case-insensitive name match" },
      { name: "physical_type_match", score: 0, maximum: 0.1, reason: "type-family-only match, not dialect-exact" },
    ],
    impact: { impact_score: 12, source_table_impact: 9, target_table_impact: 3, depth: 3, node_limit: 100, truncated: false },
  },
  {
    candidate: relationshipCandidateRead({
      id: "rc_5",
      detection_rule: "CANONICAL_NAME_TYPE_FAMILY_TO_PRIMARY_KEY_CROSS_SOURCE_V1",
      confidence: 0.55,
      evidence: { confidence_algorithm_version: "relationship-confidence-signals-v1" },
    }),
    sourceTable: "analytics.raw.chargeback_event",
    sourceColumn: "txn_ref",
    targetTable: "analytics.core.transaction_dim",
    targetColumn: "transaction_id",
    signals: [
      { name: "primary_key_target", score: 0.55, maximum: 0.55, reason: "target column is a declared PRIMARY KEY (cross-source base)" },
      { name: "column_name_match", score: 0, maximum: 0.1, reason: "canonical/naming-convention-normalized match only" },
      { name: "physical_type_match", score: 0, maximum: 0.1, reason: "type-family-only match, not dialect-exact" },
    ],
    impact: { impact_score: 3, source_table_impact: 3, target_table_impact: 0, depth: 3, node_limit: 100, truncated: false },
  },
];

function relationshipCandidateDiff(f: FixtureRelationshipCandidate): RelationshipCandidateDiffEntryRead[] {
  const snapshot: Record<string, unknown> = {
    source_table: f.sourceTable,
    source_column: f.sourceColumn,
    target_table: f.targetTable,
    target_column: f.targetColumn,
    detection_rule: f.candidate.detection_rule,
    confidence: f.candidate.confidence,
    confidence_signals: f.signals,
  };
  return Object.keys(snapshot)
    .sort()
    .map((field) => ({ field, change: "added" as const, after: snapshot[field] }));
}

function relationshipReviewItem(f: FixtureRelationshipCandidate): RelationshipCandidateReviewItemRead {
  return { candidate: f.candidate, diff: relationshipCandidateDiff(f), impact: f.impact };
}

/** `GET /v1/datasources/{id}/relationship-candidates/review-queue` (N4). Only
 *  `ds_snowflake_prod` (the one datasource `makeFixtureOrgDatasources` lists)
 *  has fixture candidates; any other id returns an empty, valid queue rather
 *  than throwing, matching a datasource with nothing pending. */
export async function makeFixtureRelationshipCandidateReviewQueue(
  datasourceId: string,
  query: { limit?: number; offset?: number },
): Promise<RelationshipCandidateReviewQueueRead> {
  await wait(100);
  const all = datasourceId === "ds_snowflake_prod" ? RELATIONSHIP_CANDIDATE_FIXTURES : [];
  const pending = all.filter((f) => f.candidate.status === "PENDING");
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  const page = pending.slice(offset, offset + limit);
  return {
    datasource_id: datasourceId,
    items: page.map(relationshipReviewItem),
    limit,
    offset,
    scanned_count: pending.length,
    total_pending_count: pending.length,
    truncated: false,
  };
}

/** `GET /v1/datasources/{id}/relationship-candidates?candidate_status=` — the
 *  raw list, used here to show decision history (APPROVED/REJECTED) the
 *  PENDING-only review queue drops. A couple of already-decided candidates so
 *  the decided-history section has something to render in fixture mode. */
const DECIDED_RELATIONSHIP_CANDIDATES: RelationshipCandidateRead[] = [
  relationshipCandidateRead({
    id: "rc_approved_1",
    detection_rule: "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
    confidence: 0.92,
    status: "APPROVED",
    reviewed_by: "steward@tenant.example",
    review_reason: "Confirmed against the source's declared key.",
    reviewed_at: "2026-08-29T11:04:00Z",
  }),
  relationshipCandidateRead({
    id: "rc_rejected_1",
    detection_rule: "CANONICAL_NAME_TYPE_FAMILY_TO_PRIMARY_KEY_CROSS_SOURCE_V1",
    confidence: 0.71,
    status: "REJECTED",
    reviewed_by: "steward@tenant.example",
    review_reason: "Name collision only — these are unrelated identifiers.",
    reviewed_at: "2026-08-29T11:07:00Z",
  }),
];

export async function makeFixtureRelationshipCandidates(
  datasourceId: string,
  status?: string,
): Promise<PageOf<RelationshipCandidateRead>> {
  await wait(80);
  const all = datasourceId === "ds_snowflake_prod" ? DECIDED_RELATIONSHIP_CANDIDATES : [];
  const items = status && status !== "ALL" ? all.filter((c) => c.status === status) : all;
  return { items, limit: 200, offset: 0, total: items.length };
}

/** `POST /v1/relationship-candidates/{id}/decision` — mutates the same
 *  in-memory fixture array `makeFixtureRelationshipCandidateReviewQueue`
 *  reads, so a decide-then-refetch in fixture mode drops the decided
 *  candidate out of the PENDING queue exactly like the real endpoint. */
export async function makeFixtureDecideRelationshipCandidate(
  candidateId: string,
  body: RelationshipCandidateDecision,
): Promise<RelationshipCandidateRead> {
  await wait(80);
  const f = RELATIONSHIP_CANDIDATE_FIXTURES.find((x) => x.candidate.id === candidateId);
  if (!f) throw new Error(`fixture: no such relationship candidate ${candidateId}`);
  if (f.candidate.status !== "PENDING") {
    throw new Error("relationship candidate is already decided");
  }
  f.candidate = {
    ...f.candidate,
    status: body.decision === "APPROVE" ? "APPROVED" : "REJECTED",
    reviewed_by: "dev-fixture-user",
    review_reason: body.reason ?? null,
    reviewed_at: new Date().toISOString(),
  };
  return f.candidate;
}

/** `POST /v1/relationship-candidates/bulk-decision` (RL-6) — explicit
 *  `candidate_ids` only (the `filter` selection mode is a server-side
 *  convenience this fixture does not need to reproduce); each id not found
 *  or already decided is reported FAILED rather than aborting the batch,
 *  same partial-success contract as the real endpoint. */
export async function makeFixtureBulkDecideRelationshipCandidates(
  body: RelationshipCandidateBulkDecisionRequest,
): Promise<RelationshipCandidateBulkDecisionResultRead> {
  await wait(110);
  const ids = body.candidate_ids ?? [];
  const results: RelationshipCandidateBulkDecisionItemRead[] = [];
  let succeeded = 0;
  for (const id of ids) {
    const f = RELATIONSHIP_CANDIDATE_FIXTURES.find((x) => x.candidate.id === id);
    if (!f) {
      results.push({ candidate_id: id, status: "FAILED", reason: "relationship candidate not found" });
      continue;
    }
    if (f.candidate.status !== "PENDING") {
      results.push({ candidate_id: id, status: "FAILED", reason: "relationship candidate is already decided" });
      continue;
    }
    f.candidate = {
      ...f.candidate,
      status: body.decision === "APPROVE" ? "APPROVED" : "REJECTED",
      reviewed_by: "dev-fixture-user",
      review_reason: body.reason ?? null,
      reviewed_at: new Date().toISOString(),
    };
    results.push({ candidate_id: id, status: "SUCCEEDED" });
    succeeded += 1;
  }
  return {
    decision: body.decision,
    selection_mode: "EXPLICIT",
    requested_count: ids.length,
    succeeded_count: succeeded,
    failed_count: ids.length - succeeded,
    truncated: false,
    results,
  };
}

/** `GET /v1/relationship-candidates/confidence-calibration` (RL-7) — a
 *  small, fixed bucket set standing in for this org's real decision
 *  history; `datasourceId: null` (org-wide) and `"ds_snowflake_prod"` both
 *  return the same buckets in fixture mode, since there is only ever one
 *  fixture datasource. */
export async function makeFixtureRelationshipCandidateCalibration(
  datasourceId: string | null,
): Promise<RelationshipCandidateCalibrationRead> {
  await wait(70);
  return {
    datasource_id: datasourceId,
    bucket_width: 0.1,
    total_decided: 62,
    ground_truth_overrides_applied: 4,
    methodology_note:
      "Buckets are the observed steward approval rate from this organization's own decision " +
      "history, not a published calibration curve against a labelled corpus.",
    buckets: [
      { confidence_low: 0.5, confidence_high: 0.6, decided_count: 9, approved_count: 4, rejected_count: 5, observed_approval_rate: 4 / 9 },
      { confidence_low: 0.6, confidence_high: 0.7, decided_count: 11, approved_count: 7, rejected_count: 4, observed_approval_rate: 7 / 11 },
      { confidence_low: 0.7, confidence_high: 0.8, decided_count: 14, approved_count: 11, rejected_count: 3, observed_approval_rate: 11 / 14 },
      { confidence_low: 0.8, confidence_high: 0.9, decided_count: 15, approved_count: 14, rejected_count: 1, observed_approval_rate: 14 / 15 },
      { confidence_low: 0.9, confidence_high: 1.0, decided_count: 13, approved_count: 13, rejected_count: 0, observed_approval_rate: 1 },
    ],
  };
}

/* ---------------------------------------------------------------------------
   UX-16: audit ledger fixtures, standing in for the real, already-merged
   `GET /v1/organizations/{id}/audit-events` (`list_audit_events`,
   `operational_api.py:336`). Same standing as the fixtures above — the wire
   shape matches `AuditEventRead` (schemas.py:47) field-for-field, including
   `id` being an `int`, not a UUID like this app's other `*Read` ids.
--------------------------------------------------------------------------- */

const ORG_ID = "00000000-0000-0000-0000-000000000001";

const AUDIT_EVENT_FIXTURES: AuditEventRead[] = [
  {
    id: 5041, organization_id: ORG_ID, principal_id: "priya@tenant.example", principal_type: "USER",
    action: "governance_review.decide", resource_type: "GOVERNANCE_REVIEW", resource_id: "rq_4179",
    outcome: "SUCCESS", correlation_id: "corr_9f21a0", source_ip: "10.2.4.18",
    details: { decision: "APPROVE", object_type: "GLOSSARY_TERM_VERSION", object_id: "term:revenue" },
    occurred_at: "2026-09-01T10:05:00Z",
  },
  {
    id: 5040, organization_id: ORG_ID, principal_id: "semantic_inference_agent", principal_type: "SERVICE",
    action: "governance_review.create", resource_type: "GOVERNANCE_REVIEW", resource_id: "rq_4181",
    outcome: "SUCCESS", correlation_id: "corr_7b13c4", source_ip: null,
    details: { object_type: "SEMANTIC_METRIC_VERSION", object_id: "metric:revenue", confidence: 0.87 },
    occurred_at: "2026-09-01T14:02:00Z",
  },
  {
    id: 5039, organization_id: ORG_ID, principal_id: "retail-owner@tenant.example", principal_type: "USER",
    action: "marketplace.access_request.create", resource_type: "DATA_PRODUCT_VERSION", resource_id: "dpv_customer360",
    outcome: "SUCCESS", correlation_id: "corr_2ad91e", source_ip: "10.2.4.61",
    details: { purpose: "loyalty reporting refresh", duration_days: 90 },
    occurred_at: "2026-09-01T09:12:00Z",
  },
  {
    id: 5038, organization_id: ORG_ID, principal_id: "priya@tenant.example", principal_type: "USER",
    action: "catalog.certify", resource_type: "TABLE", resource_id: "t_000abc",
    outcome: "DENIED", correlation_id: "corr_5e6f10", source_ip: "10.2.4.18",
    details: { reason: "requester's role has no CERTIFY binding", required_roles: ["DataSteward"] },
    occurred_at: "2026-08-31T16:44:00Z",
  },
  {
    id: 5037, organization_id: ORG_ID, principal_id: "risk-lead@tenant.example", principal_type: "USER",
    action: "datasource.credential_rotate", resource_type: "DATA_SOURCE", resource_id: "ds_snowflake_prod",
    outcome: "SUCCESS", correlation_id: "corr_5e6f10", source_ip: "10.2.4.44",
    details: { vault_path: "vault://ds/snowflake_prod", rotated_by: "scheduled_rotation" },
    occurred_at: "2026-08-31T02:00:00Z",
  },
  {
    id: 5036, organization_id: ORG_ID, principal_id: "studio_eval_runner", principal_type: "SERVICE",
    action: "studio.change_set.submit", resource_type: "STUDIO_CHANGE_SET", resource_id: "cs_1001",
    outcome: "SUCCESS", correlation_id: "corr_1c88f2", source_ip: null,
    details: { author: "priya@tenant.example", item_count: 1 },
    occurred_at: "2026-09-01T09:40:00Z",
  },
  {
    id: 5035, organization_id: ORG_ID, principal_id: "pricing_bot", principal_type: "SERVICE",
    action: "ai_decision.refusal", resource_type: "TABLE", resource_id: "t_risk_exposure",
    outcome: "DENIED", correlation_id: "corr_0a4d77", source_ip: null,
    details: { classification: "RESTRICTED", required_roles: ["DataSteward", "Reviewer"] },
    occurred_at: "2026-08-31T09:47:00Z",
  },
  {
    id: 5034, organization_id: ORG_ID, principal_id: "dev-fixture-user", principal_type: "USER",
    action: "me.session_start", resource_type: "SESSION", resource_id: null,
    outcome: "SUCCESS", correlation_id: "corr_884b21", source_ip: "10.2.4.9",
    details: {},
    occurred_at: "2026-08-30T08:15:00Z",
  },
  {
    id: 5033, organization_id: ORG_ID, principal_id: "retail-owner@tenant.example", principal_type: "USER",
    action: "studio.change_set.create", resource_type: "STUDIO_CHANGE_SET", resource_id: "cs_1002",
    outcome: "SUCCESS", correlation_id: "corr_1c88f2", source_ip: "10.2.4.61",
    details: { name: "Publish retail customer 360 context product v6" },
    occurred_at: "2026-08-30T11:00:00Z",
  },
  {
    id: 5032, organization_id: ORG_ID, principal_id: "steward@tenant.example", principal_type: "USER",
    action: "marketplace.access_request.decide", resource_type: "MARKETPLACE_ACCESS_REQUEST", resource_id: "mar_dpv_risk_exposure",
    outcome: "FAILURE", correlation_id: "corr_ff3e02", source_ip: "10.2.4.7",
    details: { error: "fulfillment provider timeout", fulfillment_provider: "okta_entitlements" },
    occurred_at: "2026-08-29T13:26:00Z",
  },
];

function auditMatches(event: AuditEventRead, q: AuditEventQuery): boolean {
  if (q.action && event.action !== q.action) return false;
  if (q.resourceType && event.resource_type !== q.resourceType) return false;
  if (q.correlationId && event.correlation_id !== q.correlationId) return false;
  if (q.since && event.occurred_at < q.since) return false;
  if (q.until && event.occurred_at > q.until) return false;
  return true;
}

/** `GET /v1/organizations/{id}/audit-events` (UX-16). Filters client-side
 *  over this fixed set the same way `makeFixtureReviewQueue` does, and pages
 *  by `limit`/`offset` — the real route's own contract, not a cursor. */
export async function makeFixtureAuditEvents(
  query: AuditEventQuery,
): Promise<PageOf<AuditEventRead>> {
  await wait(90);
  const filtered = AUDIT_EVENT_FIXTURES.filter((e) => auditMatches(e, query));
  const limit = query.limit ?? 100;
  const offset = query.offset ?? 0;
  return {
    items: filtered.slice(offset, offset + limit),
    limit,
    offset,
    total: filtered.length,
  };
}

/* ---------------------------------------------------------------------------
   Negative knowledge (Phase E / EE.3, `negative_knowledge_api.py`) — the
   registry of previously rejected assertions ("this table is NOT the
   customer master") and their suppression state. Filtered client-side over
   one fixed set, the same idiom `makeFixtureAuditEvents` above uses for its
   own org-wide, filter-only endpoint. Two rows deliberately share a
   `subject_id` (`t_customer_master`) so the per-subject lookup has more than
   one result to prove it, and the set mixes both `suppression_active`
   states and several distinct `assertion_type` values.
--------------------------------------------------------------------------- */

const NEGATIVE_ASSERTION_FIXTURES: NegativeAssertionRead[] = [
  {
    id: "na_0001", organization_id: ORG_ID,
    assertion_type: "TABLE_NOT_ENTITY", subject_id: "t_customer_master",
    predicate: { claimed_entity: "customer", table: "raw_sales.customer_master" },
    evidence: { reviewer_note: "This is a staging copy, not the governed master.", confidence: 0.34 },
    rejected_by: "priya@tenant.example", rejected_at: "2026-08-20T10:05:00Z",
    suppression_active: true, material_change_hash: null,
    suppression_lifted_at: null, suppression_lifted_by: null, lift_reason: null,
    created_at: "2026-08-20T10:05:00Z", updated_at: "2026-08-20T10:05:00Z",
  },
  {
    id: "na_0002", organization_id: ORG_ID,
    assertion_type: "COLUMN_NOT_PII", subject_id: "t_customer_master.email_hash",
    predicate: { column: "email_hash", claimed_classification: "PII" },
    evidence: { reviewer_note: "Column is a salted, one-way hash, not reversible PII.", sample_size: 500 },
    rejected_by: "steward@tenant.example", rejected_at: "2026-08-18T14:30:00Z",
    suppression_active: true, material_change_hash: null,
    suppression_lifted_at: null, suppression_lifted_by: null, lift_reason: null,
    created_at: "2026-08-18T14:30:00Z", updated_at: "2026-08-18T14:30:00Z",
  },
  {
    id: "na_0003", organization_id: ORG_ID,
    assertion_type: "RELATIONSHIP_NOT_VALID", subject_id: "fk_orders_customer_id",
    predicate: { from_table: "orders", to_table: "customer_master", key: "customer_id" },
    evidence: { reason: "Overlap is coincidental, no referential intent found in dbt or FK constraints." },
    rejected_by: "risk-lead@tenant.example", rejected_at: "2026-07-30T09:00:00Z",
    suppression_active: false, material_change_hash: "sha256:9f2a...c771",
    suppression_lifted_at: "2026-08-25T11:00:00Z", suppression_lifted_by: "priya@tenant.example",
    lift_reason: "A new dbt model materialized the join explicitly; re-evaluate against current lineage.",
    created_at: "2026-07-30T09:00:00Z", updated_at: "2026-08-25T11:00:00Z",
  },
  {
    id: "na_0004", organization_id: ORG_ID,
    assertion_type: "TABLE_NOT_ENTITY", subject_id: "t_customer_master",
    predicate: { claimed_entity: "customer", table: "curated.customer_master_v2" },
    evidence: { reviewer_note: "Superseded by the governed model; this candidate table was renamed away." },
    rejected_by: "priya@tenant.example", rejected_at: "2026-08-22T08:00:00Z",
    suppression_active: true, material_change_hash: null,
    suppression_lifted_at: null, suppression_lifted_by: null, lift_reason: null,
    created_at: "2026-08-22T08:00:00Z", updated_at: "2026-08-22T08:00:00Z",
  },
  {
    id: "na_0005", organization_id: ORG_ID,
    assertion_type: "METRIC_NOT_EQUIVALENT", subject_id: "metric_net_revenue_v3",
    predicate: { candidate_metric: "net_revenue_v3", claimed_equivalent_to: "net_revenue_v2" },
    evidence: { reviewer_note: "v3 excludes chargebacks; not the same definition.", diff_pct: 4.1 },
    rejected_by: "finance-owner@tenant.example", rejected_at: "2026-08-05T16:20:00Z",
    suppression_active: true, material_change_hash: null,
    suppression_lifted_at: null, suppression_lifted_by: null, lift_reason: null,
    created_at: "2026-08-05T16:20:00Z", updated_at: "2026-08-05T16:20:00Z",
  },
  {
    id: "na_0006", organization_id: ORG_ID,
    assertion_type: "COLUMN_NOT_PII", subject_id: "t_risk_exposure.account_ref",
    predicate: { column: "account_ref", claimed_classification: "PII" },
    evidence: { reviewer_note: "Internal surrogate key with no external mapping." },
    rejected_by: "steward@tenant.example", rejected_at: "2026-07-12T12:00:00Z",
    suppression_active: false, material_change_hash: "sha256:11de...04af",
    suppression_lifted_at: "2026-08-01T09:30:00Z", suppression_lifted_by: "steward@tenant.example",
    lift_reason: "A new external partner feed now maps this key to a real account number.",
    created_at: "2026-07-12T12:00:00Z", updated_at: "2026-08-01T09:30:00Z",
  },
  {
    id: "na_0007", organization_id: ORG_ID,
    assertion_type: "DOMAIN_MISMATCH", subject_id: "t_mortgage_ledger_entry",
    predicate: { claimed_domain: "mortgage", observed_domain: "treasury" },
    evidence: { reviewer_note: "Table actually belongs to treasury reconciliation, not mortgage servicing." },
    rejected_by: "treasury-ops@tenant.example", rejected_at: "2026-08-27T13:45:00Z",
    suppression_active: true, material_change_hash: null,
    suppression_lifted_at: null, suppression_lifted_by: null, lift_reason: null,
    created_at: "2026-08-27T13:45:00Z", updated_at: "2026-08-27T13:45:00Z",
  },
];

function negativeAssertionMatches(
  a: NegativeAssertionRead,
  q: NegativeKnowledgeSearchQuery,
): boolean {
  if (q.assertionType && a.assertion_type !== q.assertionType) return false;
  if (
    q.suppressionActive !== undefined &&
    q.suppressionActive !== null &&
    a.suppression_active !== q.suppressionActive
  ) {
    return false;
  }
  return true;
}

/** `GET /v1/negative-knowledge/search`. Filters client-side over the fixed
 *  set above, the same idiom `makeFixtureAuditEvents` uses for its own
 *  org-wide, filter-only endpoint. */
export async function makeFixtureNegativeKnowledgeSearch(
  query: NegativeKnowledgeSearchQuery,
): Promise<PageOf<NegativeAssertionRead>> {
  await wait(90);
  const filtered = NEGATIVE_ASSERTION_FIXTURES.filter((a) => negativeAssertionMatches(a, query));
  const limit = query.limit ?? 50;
  const offset = query.offset ?? 0;
  return { items: filtered.slice(offset, offset + limit), limit, offset, total: filtered.length };
}

/** `GET /v1/negative-knowledge/{subject_id}` — every assertion recorded
 *  against one subject. */
export async function makeFixtureNegativeKnowledgeSubject(
  subjectId: string,
  query: NegativeKnowledgeSubjectQuery,
): Promise<PageOf<NegativeAssertionRead>> {
  await wait(90);
  const filtered = NEGATIVE_ASSERTION_FIXTURES.filter((a) => a.subject_id === subjectId);
  const limit = query.limit ?? 50;
  const offset = query.offset ?? 0;
  return { items: filtered.slice(offset, offset + limit), limit, offset, total: filtered.length };
}

/** `POST /v1/negative-knowledge/{id}/lift-suppression` — mutates the same
 *  in-memory fixture array the two reads above filter, so a lift-then-refetch
 *  in fixture mode behaves like the real endpoint. */
export async function makeFixtureLiftSuppression(
  assertionId: string,
  body: LiftSuppressionRequest,
): Promise<NegativeAssertionRead> {
  await wait(80);
  const assertion = NEGATIVE_ASSERTION_FIXTURES.find((a) => a.id === assertionId);
  if (!assertion) throw new Error(`fixture: no such negative assertion ${assertionId}`);
  const now = new Date().toISOString();
  assertion.suppression_active = false;
  assertion.suppression_lifted_at = now;
  assertion.suppression_lifted_by = "dev-fixture-user";
  assertion.lift_reason = body.reason;
  assertion.updated_at = now;
  return { ...assertion };
}

/* ---------------------------------------------------------------------------
   AI governance fixtures — AI registry, trust scoring and remediation loop.
   Wire-shape identical to ai_registry_api.py, so VITE_USE_FIXTURES=0 swaps to
   the real endpoints unchanged. The remediation store is mutable so an
   update-then-refetch reflects the new status, like the real endpoint.
--------------------------------------------------------------------------- */

import type {
  AiAssessmentTemplateRead,
  AiAssetVersionRead,
  AiRemediationRead,
  AiRemediationUpdate,
  AiTrustFactorRead,
  AiTrustScoreRead,
} from "./types";

const FIXTURE_AI_ASSET_ORG = "00000000-0000-0000-0000-000000000001";

const FIXTURE_AI_ASSETS: AiAssetVersionRead[] = [
  {
    name: "Revenue Analyst",
    description: "Governed SQL analyst agent for revenue questions.",
    intended_use: "Answer revenue-by-LOB questions over governed marts.",
    owner_principal: "priya@tenant.example",
    provider_type: "OPENAI",
    risk_tier: "MEDIUM",
    documentation_url: null,
    id: "aiv_revenue_analyst",
    organization_id: FIXTURE_AI_ASSET_ORG,
    asset_id: "aia_revenue_analyst",
    asset_key: "revenue-analyst",
    asset_kind: "AGENT",
    version: 3,
    status: "APPROVED",
    fingerprint: "sha256:revenue3",
    created_by: "priya@tenant.example",
    approved_by: "sam@tenant.example",
    approved_at: "2026-08-20T10:00:00Z",
    created_at: "2026-08-18T10:00:00Z",
    updated_at: "2026-08-20T10:00:00Z",
  },
  {
    name: "Fraud Scoring Model",
    description: "Third-party fraud propensity model integration.",
    intended_use: "Score transactions for fraud review triage.",
    owner_principal: "dev@tenant.example",
    provider_type: "CUSTOM",
    risk_tier: "HIGH",
    documentation_url: null,
    id: "aiv_fraud_model",
    organization_id: FIXTURE_AI_ASSET_ORG,
    asset_id: "aia_fraud_model",
    asset_key: "fraud-scoring",
    asset_kind: "MODEL",
    version: 1,
    status: "DRAFT",
    fingerprint: "sha256:fraud1",
    created_by: "dev@tenant.example",
    approved_by: null,
    approved_at: null,
    created_at: "2026-08-27T09:00:00Z",
    updated_at: "2026-08-27T09:00:00Z",
  },
];

const factor = (name: string, score: number, maximum: number, reason: string): AiTrustFactorRead => ({
  factor: name,
  score,
  maximum,
  reason,
});

const FIXTURE_AI_TRUST: Record<string, AiTrustScoreRead> = {
  aiv_revenue_analyst: {
    ai_asset_version_id: "aiv_revenue_analyst",
    score: 0.78,
    grade: "CONDITIONAL",
    factors: [
      factor("approval", 0.2, 0.2, "Version is approved by an independent checker."),
      factor("assessment", 0.28, 0.4, "NIST AI RMF assessment passed 7 of 10 controls."),
      factor("evaluation", 0.2, 0.2, "Prompt-risk and grounding evaluations recorded."),
      factor("open_findings", 0.1, 0.2, "One open remediation of MEDIUM severity."),
    ],
    blockers: [],
    computed_at: "2026-08-29T12:00:00Z",
  },
  aiv_fraud_model: {
    ai_asset_version_id: "aiv_fraud_model",
    score: 0.36,
    grade: "UNTRUSTED",
    factors: [
      factor("approval", 0, 0.2, "Version is still in DRAFT — not independently approved."),
      factor("assessment", 0.16, 0.4, "EU AI Act high-risk assessment failed 3 controls."),
      factor("evaluation", 0.1, 0.2, "No bias/robustness evaluation evidence recorded."),
      factor("open_findings", 0, 0.2, "Two open HIGH-severity remediations."),
    ],
    blockers: [
      "High-risk asset with an open HIGH-severity remediation",
      "No independent approval on the current version",
    ],
    computed_at: "2026-08-29T12:00:00Z",
  },
};

function remediation(overrides: Partial<AiRemediationRead> & { id: string; ai_asset_version_id: string }): AiRemediationRead {
  return {
    finding_key: "finding",
    title: "Finding",
    description: "",
    owner_principal: "dev@tenant.example",
    due_at: null,
    organization_id: FIXTURE_AI_ASSET_ORG,
    status: "OPEN",
    resolution_evidence: {},
    created_by: "assessor@tenant.example",
    resolved_by: null,
    resolved_at: null,
    created_at: "2026-08-27T10:00:00Z",
    updated_at: "2026-08-27T10:00:00Z",
    ...overrides,
  };
}

const FIXTURE_AI_REMEDIATIONS: Record<string, AiRemediationRead[]> = {
  aiv_revenue_analyst: [
    remediation({
      id: "rem_rev_1",
      ai_asset_version_id: "aiv_revenue_analyst",
      finding_key: "nist.govern.1",
      title: "Document escalation path for refused answers",
      description: "Add a documented human escalation path for policy refusals.",
      status: "OPEN",
      owner_principal: "priya@tenant.example",
    }),
  ],
  aiv_fraud_model: [
    remediation({
      id: "rem_fraud_1",
      ai_asset_version_id: "aiv_fraud_model",
      finding_key: "euaiact.bias",
      title: "Provide bias and fairness evaluation evidence",
      description: "High-risk model requires a documented bias evaluation before approval.",
      status: "OPEN",
      owner_principal: "dev@tenant.example",
    }),
    remediation({
      id: "rem_fraud_2",
      ai_asset_version_id: "aiv_fraud_model",
      finding_key: "euaiact.human_oversight",
      title: "Define human-oversight controls",
      description: "Document how a reviewer overrides an automated fraud score.",
      status: "IN_PROGRESS",
      owner_principal: "dev@tenant.example",
    }),
  ],
};

export async function makeFixtureAiAssets(organizationId: string): Promise<PageOf<AiAssetVersionRead>> {
  await wait(60);
  const items = organizationId === FIXTURE_AI_ASSET_ORG ? FIXTURE_AI_ASSETS : [];
  return { items, limit: 200, offset: 0, total: items.length };
}

export async function makeFixtureAiTrust(versionId: string): Promise<AiTrustScoreRead> {
  await wait(50);
  return (
    FIXTURE_AI_TRUST[versionId] ?? {
      ai_asset_version_id: versionId,
      score: 0,
      grade: "UNTRUSTED",
      factors: [],
      blockers: ["No trust snapshot for this version"],
      computed_at: "2026-08-29T12:00:00Z",
    }
  );
}

export async function makeFixtureAiRemediations(versionId: string): Promise<PageOf<AiRemediationRead>> {
  await wait(60);
  const items = FIXTURE_AI_REMEDIATIONS[versionId] ?? [];
  return { items, limit: 200, offset: 0, total: items.length };
}

export async function makeFixtureUpdateAiRemediation(
  remediationId: string,
  body: AiRemediationUpdate,
): Promise<AiRemediationRead> {
  await wait(80);
  for (const list of Object.values(FIXTURE_AI_REMEDIATIONS)) {
    const found = list.find((r) => r.id === remediationId);
    if (found) {
      found.status = body.status;
      found.updated_at = new Date().toISOString();
      if (body.status === "RESOLVED" || body.status === "ACCEPTED_RISK") {
        found.resolved_by = "reviewer@tenant.example";
        found.resolved_at = found.updated_at;
      } else {
        found.resolved_by = null;
        found.resolved_at = null;
      }
      if (body.resolution_evidence) found.resolution_evidence = body.resolution_evidence;
      return { ...found };
    }
  }
  throw new ApiError(404, "AI remediation not found");
}

export async function makeFixtureAiAssessmentTemplates(): Promise<AiAssessmentTemplateRead[]> {
  await wait(40);
  return [
    {
      template_key: "eu_ai_act_high_risk",
      framework: "EU_AI_ACT",
      framework_version: "2024",
      title: "EU AI Act — high-risk system",
      controls: [
        { control_key: "risk_management", title: "Risk management system", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "data_governance", title: "Data and data governance", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "human_oversight", title: "Human oversight", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "transparency", title: "Transparency to users", weight: 1, outcome: "NOT_APPLICABLE" },
      ],
    },
    {
      template_key: "nist_ai_rmf_core",
      framework: "NIST_AI_RMF",
      framework_version: "1.0",
      title: "NIST AI RMF — core functions",
      controls: [
        { control_key: "govern", title: "Govern", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "map", title: "Map", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "measure", title: "Measure", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "manage", title: "Manage", weight: 1, outcome: "NOT_APPLICABLE" },
      ],
    },
    {
      template_key: "ai_uc_1",
      framework: "AI_UC_1",
      framework_version: "1.0",
      title: "Enterprise AI use-case approval",
      controls: [
        { control_key: "business_purpose", title: "Documented business purpose", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "data_classification", title: "Data classification reviewed", weight: 1, outcome: "NOT_APPLICABLE" },
        { control_key: "owner_accountable", title: "Accountable owner assigned", weight: 1, outcome: "NOT_APPLICABLE" },
      ],
    },
  ];
}

/* ---------------------------------------------------------------------------
   Context products — fixtures for the `ContextProductsScreen` block added to
   `lib/api.ts`. See that block's file-top comment for the real endpoints
   these stand in for. `ContextProductRead.latest_version` is the whole
   version record embedded (that is what `list_context_products` actually
   returns, one join, not a separate fetch) so these generators build both
   together rather than modelling a product and a version as two tables. */

import type { ContextCompilationRead, ContextProductCreate, ContextProductRead } from "./types";

const fakeHex = (input: string, length = 64): string => {
  let h1 = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h1 ^= input.charCodeAt(i);
    h1 = Math.imul(h1, 0x01000193);
  }
  let out = "";
  let seed = h1 >>> 0;
  while (out.length < length) {
    seed = (Math.imul(seed ^ (seed >>> 15), 0x2545f491) + 0x9e3779b9) >>> 0;
    out += seed.toString(16).padStart(8, "0");
  }
  return out.slice(0, length);
};

const FIXTURE_CONTEXT_PRODUCTS: Record<string, ContextProductRead[]> = {
  proj_core: [
    {
      id: "cp_consumer_risk", organization_id: "00000000-0000-0000-0000-000000000001",
      project_id: "proj_core", product_key: "consumer-risk-context", lifecycle_status: "ACTIVE",
      created_by: "risk-data-stewards@tenant.example",
      latest_version: {
        id: "cpv_consumer_risk_2", organization_id: "00000000-0000-0000-0000-000000000001",
        product_id: "cp_consumer_risk", product_key: "consumer-risk-context", version: 2, status: "PUBLISHED",
        name: "Consumer risk analysis", description: "Bounded context for consumer credit-risk analysts.",
        purpose: "Explain drivers of consumer delinquency for the monthly risk committee packet.",
        owner_type: "GROUP", owner_principal: "risk-data-stewards",
        table_ids: ["6f2c1e10-2b1e-4a4a-9c2a-111111111111"],
        semantic_model_version_ids: ["smv_core_3"],
        glossary_term_version_ids: [],
        eligible_tool_version_ids: [],
        allowed_consumer_roles: ["Analyst", "Reviewer"],
        lineage_depth: 2,
        quality_requirements: { minimum_score: 85, deny_on_critical_incident: true },
        policy_summary: { source_values: "GATEWAY_ONLY", retention: "NO_RAW_CONTEXT", permitted_actions: ["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"] },
        support_window_days: null,
        fingerprint: fakeHex("cpv_consumer_risk_2"),
        created_by: "risk-data-stewards@tenant.example", approved_by: "steward@tenant.example",
        approved_at: "2026-08-10T00:00:00Z", published_at: "2026-08-11T00:00:00Z",
        based_on_version_id: "cpv_consumer_risk_1",
        created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
        superseded_at: null, support_window_ends_at: null, superseded_by_version_id: null,
      },
      created_at: "2026-07-01T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
    },
  ],
  proj_retail: [],
};

/** `GET /v1/projects/{project_id}/context-products`. */
export async function makeFixtureContextProducts(
  projectId: string,
  query: { limit?: number; offset?: number },
): Promise<PageOf<ContextProductRead>> {
  await wait(80);
  const items = FIXTURE_CONTEXT_PRODUCTS[projectId] ?? [];
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 200;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

export async function makeFixtureLineageGraph(
  datasourceId: string,
): Promise<UnifiedLineageGraphRead> {
  await wait(90);
  return {
    datasource_id: datasourceId,
    nodes: [
      { id: "t_raw_sales", node_kind: "TABLE", label: "raw_sales", qualified_name: "analytics.raw.raw_sales", resolved: true },
      { id: "t_orders_raw", node_kind: "TABLE", label: "orders_raw", qualified_name: "analytics.core.orders_raw", resolved: true },
      { id: "t_customer_dim", node_kind: "TABLE", label: "customer_dim", qualified_name: "analytics.curated.customer_dim", resolved: true },
      { id: "t_revenue_agg", node_kind: "DBT_MODEL", label: "revenue_agg", qualified_name: "analytics.mart.revenue_agg", resolved: true },
    ],
    edges: [
      { id: "fk_raw_orders", edge_source: "FOREIGN_KEY", source_node_id: "t_raw_sales", target_node_id: "t_orders_raw", source_label: "raw_sales", target_label: "orders_raw", status: "ACTIVE", confidence: 1 },
      { id: "fk_customer_orders", edge_source: "FOREIGN_KEY", source_node_id: "t_customer_dim", target_node_id: "t_orders_raw", source_label: "customer_dim", target_label: "orders_raw", status: "ACTIVE", confidence: 1 },
      { id: "dbt_orders_revenue", edge_source: "DBT_DEPENDENCY", source_node_id: "t_orders_raw", target_node_id: "t_revenue_agg", source_label: "orders_raw", target_label: "revenue_agg", status: "ACTIVE", confidence: 1 },
    ],
    counts_by_source: { FOREIGN_KEY: 2, DBT_DEPENDENCY: 1 },
    returned_node_count: 4,
    returned_edge_count: 3,
    node_limit: 200,
    edge_limit: 500,
    truncated: false,
    truncation_reasons: [],
  };
}

/** `POST /v1/projects/{project_id}/context-products` — mirrors
 *  `create_context_product`'s shape: a new product plus its version-1 DRAFT,
 *  with the server-assigned fields (`id`, `fingerprint`, `created_by`,
 *  timestamps) a real create response would carry. */
export async function makeFixtureCreateContextProduct(
  projectId: string,
  body: ContextProductCreate,
): Promise<ContextProductRead> {
  await wait(100);
  const items = (FIXTURE_CONTEXT_PRODUCTS[projectId] ??= []);
  if (items.some((p) => p.product_key === body.product_key)) {
    throw new ApiError(409, "context product key already exists");
  }
  const now = new Date().toISOString();
  const id = `cp_${body.product_key.replace(/[^a-z0-9]+/g, "_")}`;
  const versionId = `${id}_v1`;
  const product: ContextProductRead = {
    id, organization_id: "00000000-0000-0000-0000-000000000001", project_id: projectId,
    product_key: body.product_key, lifecycle_status: "ACTIVE", created_by: "local-ui-admin",
    latest_version: {
      id: versionId, organization_id: "00000000-0000-0000-0000-000000000001", product_id: id,
      product_key: body.product_key, version: 1, status: "DRAFT",
      name: body.name, description: body.description, purpose: body.purpose,
      owner_type: body.owner_type, owner_principal: body.owner_principal,
      table_ids: body.table_ids ?? [], semantic_model_version_ids: body.semantic_model_version_ids ?? [],
      glossary_term_version_ids: body.glossary_term_version_ids ?? [],
      eligible_tool_version_ids: body.eligible_tool_version_ids ?? [],
      allowed_consumer_roles: body.allowed_consumer_roles, lineage_depth: body.lineage_depth ?? 2,
      quality_requirements: body.quality_requirements ?? { minimum_score: 0, deny_on_critical_incident: false },
      policy_summary: body.policy_summary ?? { source_values: "GATEWAY_ONLY", retention: "NO_RAW_CONTEXT", permitted_actions: ["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"] },
      support_window_days: body.support_window_days ?? null,
      fingerprint: fakeHex(`${id}:1`), created_by: "local-ui-admin",
      approved_by: null, approved_at: null, published_at: null, based_on_version_id: null,
      created_at: now, updated_at: now, superseded_at: null, support_window_ends_at: null, superseded_by_version_id: null,
    },
    created_at: now, updated_at: now,
  };
  items.push(product);
  return product;
}

function findContextProductByVersionId(versionId: string): ContextProductRead | undefined {
  for (const items of Object.values(FIXTURE_CONTEXT_PRODUCTS)) {
    const found = items.find((p) => p.latest_version.id === versionId);
    if (found) return found;
  }
  return undefined;
}

/** `POST /v1/context-product-versions/{id}/submit`. */
export async function makeFixtureSubmitContextProductVersion(versionId: string): Promise<GovernanceReviewRead> {
  await wait(90);
  const product = findContextProductByVersionId(versionId);
  if (!product) throw new ApiError(404, "context product version not found");
  if (product.latest_version.status !== "DRAFT") {
    throw new ApiError(409, "only a draft context product can be submitted");
  }
  product.latest_version.status = "REVIEW_REQUIRED";
  product.latest_version.updated_at = new Date().toISOString();
  return {
    id: `gr_${versionId}`, organization_id: product.organization_id, object_type: "CONTEXT_PRODUCT_VERSION",
    object_id: versionId, requested_action: "PUBLISH", status: "PENDING", requested_by: "local-ui-admin",
    decided_by: null, decision_reason: null, decided_at: null,
    created_at: product.latest_version.updated_at, updated_at: product.latest_version.updated_at,
  };
}

/** `POST /v1/context-product-versions/{id}/deprecate`. */
export async function makeFixtureDeprecateContextProductVersion(versionId: string): Promise<GovernanceReviewRead> {
  await wait(90);
  const product = findContextProductByVersionId(versionId);
  if (!product) throw new ApiError(404, "context product version not found");
  if (product.latest_version.status !== "PUBLISHED" && product.latest_version.status !== "SUPPORTED") {
    throw new ApiError(409, "only a published context product can retire");
  }
  const now = new Date().toISOString();
  return {
    id: `gr_deprecate_${versionId}`, organization_id: product.organization_id, object_type: "CONTEXT_PRODUCT_VERSION",
    object_id: versionId, requested_action: "DEPRECATE", status: "PENDING", requested_by: "local-ui-admin",
    decided_by: null, decision_reason: null, decided_at: null, created_at: now, updated_at: now,
  };
}

/** `GET /v1/context-product-versions/{id}/compile` — deterministic per
 *  (version, target): the same inputs always produce the same fake hash,
 *  matching the real endpoint's own "repeat this and get the same hash"
 *  guarantee that the legacy screen's success message calls out. */
export async function makeFixtureCompileContextProductVersion(
  versionId: string,
  target: string,
): Promise<ContextCompilationRead> {
  await wait(120);
  const product = findContextProductByVersionId(versionId);
  if (!product) throw new ApiError(404, "context product version not found");
  const v = product.latest_version;
  const isYaml = target === "YAML";
  const content = isYaml
    ? `name: ${v.name}\nversion: ${v.version}\npurpose: ${v.purpose}\nallowed_consumer_roles:\n${v.allowed_consumer_roles.map((r) => `  - ${r}`).join("\n")}\n`
    : JSON.stringify(
        {
          target,
          name: v.name,
          version: v.version,
          purpose: v.purpose,
          allowed_consumer_roles: v.allowed_consumer_roles,
          lineage_depth: v.lineage_depth,
          quality_requirements: v.quality_requirements,
        },
        null,
        2,
      );
  return {
    target: target as ContextCompilationRead["target"],
    content_type: isYaml ? "application/yaml" : "application/json",
    content,
    artifact_hash: fakeHex(`${versionId}:${target}:artifact`),
    source_fingerprint: v.fingerprint,
    generated_from: { context_product_version_id: versionId, version: v.version },
  };
}

/* ---------------------------------------------------------------------------
   Administration fixtures -- the tenant-onboarding wizard's four real,
   already-merged routes (see `api.ts`'s own comment above these calls).
   `FIXTURE_LOBS` below is seeded to match `FIXTURE_PROJECTS`/
   `FIXTURE_DATASOURCES` above (`lob_fin`/`lob_retail`, `proj_core`/
   `proj_retail`, `ds_snowflake_prod`) so `AdministrationScreen`'s scope
   summary tells one coherent story in fixture mode, not three disconnected
   fixture sets. Every create function below mutates these same shared,
   module-scope arrays in place -- the identical pattern
   `makeFixtureDecideReview`/`makeFixtureSubmitStudioChangeSet` above already
   use -- so a freshly created line of business/project/datasource is
   immediately visible to `makeFixtureOrgLinesOfBusiness`/
   `makeFixtureOrgProjects`/`makeFixtureOrgDatasources` without a reload. */

import type {
  DataSourceCreate,
  LineOfBusinessCreate,
  LineOfBusinessRead,
  OrganizationCreate,
  ProjectCreate,
} from "./types";

const FIXTURE_LOBS: LineOfBusinessRead[] = [
  {
    id: "lob_fin", organization_id: "00000000-0000-0000-0000-000000000001",
    name: "Consumer Finance", code: "FINANCE", status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "lob_retail", organization_id: "00000000-0000-0000-0000-000000000001",
    name: "Retail Banking", code: "RETAIL", status: "ACTIVE",
    created_at: "2026-02-01T00:00:00Z", updated_at: "2026-02-01T00:00:00Z",
  },
];

let fixtureOrgSeq = 0;
let fixtureLobSeq = 0;
let fixtureProjectSeq = 0;
let fixtureDatasourceSeq = 0;

/** `POST /v1/organizations` (`create_organization`, `api.py:584`). Does not
 *  append to `makeFixtureOrganizations`'s own single-organization list
 *  above -- that fixture, like the real dev-mode shell, stands in for the
 *  one organization the rest of this app's fixtures assume; this returns a
 *  standalone, freshly "created" record instead, matching the real
 *  endpoint's own response shape (it does not return a list either). */
export async function makeFixtureCreateOrganization(
  body: OrganizationCreate,
): Promise<OrganizationRead> {
  await wait(90);
  fixtureOrgSeq += 1;
  const now = new Date().toISOString();
  return {
    id: `org_fixture_${fixtureOrgSeq.toString().padStart(4, "0")}`,
    name: body.name,
    slug: body.slug,
    status: "ACTIVE",
    created_at: now,
    updated_at: now,
  };
}

/** `GET /v1/organizations/{organization_id}/lines-of-business`
 *  (`list_lines_of_business`, `api.py:463`). */
export async function makeFixtureOrgLinesOfBusiness(
  organizationId: string,
): Promise<PageOf<LineOfBusinessRead>> {
  await wait(60);
  const items = FIXTURE_LOBS.filter((l) => l.organization_id === organizationId);
  return { items, limit: 500, offset: 0, total: items.length };
}

/** `POST /v1/organizations/{organization_id}/lines-of-business`
 *  (`create_line_of_business`, `api.py:677`). Mirrors the real handler's own
 *  `_commit_or_conflict("line-of-business code already exists")` so a
 *  duplicate code fails the same way here as against the live API. */
export async function makeFixtureCreateLineOfBusiness(
  organizationId: string,
  body: LineOfBusinessCreate,
): Promise<LineOfBusinessRead> {
  await wait(90);
  if (FIXTURE_LOBS.some((l) => l.organization_id === organizationId && l.code === body.code)) {
    throw new ApiError(409, "line-of-business code already exists");
  }
  fixtureLobSeq += 1;
  const now = new Date().toISOString();
  const lob: LineOfBusinessRead = {
    id: `lob_fixture_${fixtureLobSeq.toString().padStart(4, "0")}`,
    organization_id: organizationId,
    name: body.name,
    code: body.code,
    status: "ACTIVE",
    created_at: now,
    updated_at: now,
  };
  FIXTURE_LOBS.push(lob);
  return lob;
}

/** `POST /v1/lines-of-business/{lob_id}/projects` (`create_project`,
 *  `api.py:901`). `data_domain_id` mirrors the real handler's own
 *  `resolve_domain` fallback (`api.py:922`) -- a synthesized per-LOB default
 *  domain id, since this fixture set carries no separate data-domain list to
 *  draw from. */
export async function makeFixtureCreateProject(
  lobId: string,
  body: ProjectCreate,
): Promise<ProjectRead> {
  await wait(90);
  const lob = FIXTURE_LOBS.find((l) => l.id === lobId);
  if (!lob) throw new ApiError(404, "line of business not found");
  if (FIXTURE_PROJECTS.some((p) => p.slug === body.slug)) {
    throw new ApiError(409, "project slug already exists");
  }
  fixtureProjectSeq += 1;
  const now = new Date().toISOString();
  const project: ProjectRead = {
    id: `proj_fixture_${fixtureProjectSeq.toString().padStart(4, "0")}`,
    organization_id: lob.organization_id,
    line_of_business_id: lob.id,
    data_domain_id: body.data_domain_id ?? `dom_${lob.id}_default`,
    name: body.name,
    slug: body.slug,
    status: "ACTIVE",
    created_at: now,
    updated_at: now,
  };
  FIXTURE_PROJECTS.push(project);
  return project;
}

/** `POST /v1/projects/{project_id}/datasources` (`create_datasource`,
 *  `api.py:1021`). Mirrors `_validate_datasource_create`'s
 *  credential-reference check (`api.py:960`) against `"env"`, this dev
 *  config's `credential_provider` default (`atlas/platform/config.py:73`) --
 *  a pasted connection string or an unapproved provider is refused here
 *  exactly like it would be by the real API. */
export async function makeFixtureRegisterDatasource(
  projectId: string,
  body: DataSourceCreate,
): Promise<DataSourceRead> {
  await wait(110);
  const project = FIXTURE_PROJECTS.find((p) => p.id === projectId);
  if (!project) throw new ApiError(404, "project not found");
  if (!body.credential_reference.startsWith("env://")) {
    throw new ApiError(
      422,
      "credential_reference must use the configured secret provider, never a connection string or unapproved provider",
    );
  }
  if (FIXTURE_DATASOURCES.some((d) => d.project_id === projectId && d.name === body.name)) {
    throw new ApiError(409, "datasource name already exists in this project");
  }
  fixtureDatasourceSeq += 1;
  const now = new Date().toISOString();
  const datasource: DataSourceRead = {
    id: `ds_fixture_${fixtureDatasourceSeq.toString().padStart(4, "0")}`,
    organization_id: project.organization_id,
    line_of_business_id: project.line_of_business_id,
    data_domain_id: project.data_domain_id,
    project_id: project.id,
    name: body.name,
    connector_type: body.connector_type,
    dialect: body.dialect,
    environment: body.environment,
    network_zone: body.network_zone ?? "default",
    credential_reference: body.credential_reference,
    max_concurrency: body.max_concurrency ?? 4,
    status: "ACTIVE",
    capabilities: {},
    created_at: now,
    updated_at: now,
  };
  FIXTURE_DATASOURCES.push(datasource);
  return datasource;
}

import type {
  GovernedToolVersionCreate,
  GovernedToolVersionRead,
  QueryExecutionResponse,
  ToolExecutionRequest,
  ToolExecutionResponse,
} from "./types";

/* ---------------------------------------------------------------------------
   Tool registry fixtures -- `ToolRegistryScreen`'s real routes
   (`GET/POST /v1/projects/{project_id}/tools`,
   `POST /v1/tool-versions/{id}/submit`,
   `POST /v1/tool-versions/{id}/deprecation-submit`,
   `POST /v1/tool-versions/{id}/execute`). Seeded under `proj_core` against
   `ds_snowflake_prod` (the same project/datasource `FIXTURE_CONTEXT_PRODUCTS`
   above already uses) so fixture mode tells one coherent story: a DRAFT
   version (submittable) and a PUBLISHED version (executable), matching what
   `ToolRegistryScreen`'s own tests exercise. */

const FIXTURE_TOOLS: Record<string, GovernedToolVersionRead[]> = {
  proj_core: [
    {
      id: "tv_customer_lookup_2", tool_id: "t_customer_lookup", organization_id: "00000000-0000-0000-0000-000000000001",
      project_id: "proj_core", slug: "customer_lookup", version: 2, status: "PUBLISHED",
      name: "Customer lookup", description: "Look up governed customer records by state.",
      datasource_id: "ds_snowflake_prod", semantic_model_version_id: null,
      sql_template: "SELECT customer_id, state FROM public.customers WHERE state = :state",
      referenced_tables: ["public.customers"],
      parameters: [
        { name: "state", parameter_type: "STRING", required: true, sensitive: false, max_length: 2 },
      ],
      allowed_roles: ["Analyst", "ToolConsumer"],
      fingerprint: fakeHex("tv_customer_lookup_2"),
      created_by: "data-eng@tenant.example", approved_by: "steward@tenant.example", approved_at: "2026-08-05T00:00:00Z",
      created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-05T00:00:00Z", usage_count: 12,
    },
    {
      id: "tv_delinquency_1", tool_id: "t_delinquency", organization_id: "00000000-0000-0000-0000-000000000001",
      project_id: "proj_core", slug: "delinquency_by_month", version: 1, status: "DRAFT",
      name: "Delinquency by month", description: "Draft: monthly delinquency count above a threshold.",
      datasource_id: "ds_snowflake_prod", semantic_model_version_id: null,
      sql_template: "SELECT date_trunc('month', due_date) AS month, COUNT(*) AS delinquent_count\nFROM analytics.core.loans_raw\nWHERE days_past_due > :threshold_days\nGROUP BY 1\nORDER BY 1",
      referenced_tables: ["analytics.core.loans_raw"],
      parameters: [
        { name: "threshold_days", parameter_type: "INTEGER", required: true, sensitive: false, minimum: 1, maximum: 365 },
      ],
      allowed_roles: ["Analyst"],
      fingerprint: fakeHex("tv_delinquency_1"),
      created_by: "risk-data-stewards@tenant.example", approved_by: null, approved_at: null,
      created_at: "2026-08-20T00:00:00Z", updated_at: "2026-08-20T00:00:00Z", usage_count: 0,
    },
  ],
  proj_retail: [],
};

/** `GET /v1/projects/{project_id}/tools`. */
export async function makeFixtureTools(
  projectId: string,
  query: { status?: string | null; limit?: number; offset?: number },
): Promise<PageOf<GovernedToolVersionRead>> {
  await wait(80);
  const all = FIXTURE_TOOLS[projectId] ?? [];
  const items = query.status ? all.filter((t) => t.status === query.status) : all;
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 200;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

function findToolVersion(versionId: string): GovernedToolVersionRead | undefined {
  for (const items of Object.values(FIXTURE_TOOLS)) {
    const found = items.find((t) => t.id === versionId);
    if (found) return found;
  }
  return undefined;
}

/** `POST /v1/projects/{project_id}/tools` -- mirrors `_persist_tool_version_draft`
 *  (`tool_api.py:201`): reusing an existing `slug` in this project attaches
 *  the draft to that tool as its next version rather than creating a new
 *  one, exactly like the real endpoint. */
export async function makeFixtureCreateToolVersion(
  projectId: string,
  body: GovernedToolVersionCreate,
): Promise<GovernedToolVersionRead> {
  await wait(120);
  const items = (FIXTURE_TOOLS[projectId] ??= []);
  const existing = items.filter((t) => t.slug === body.slug);
  const toolId = existing[0]?.tool_id ?? `t_${body.slug}`;
  const nextVersion = existing.length ? Math.max(...existing.map((t) => t.version)) + 1 : 1;
  const now = new Date().toISOString();
  const version: GovernedToolVersionRead = {
    id: `tv_${body.slug}_${nextVersion}`, tool_id: toolId, organization_id: "00000000-0000-0000-0000-000000000001",
    project_id: projectId, slug: body.slug, version: nextVersion, status: "DRAFT",
    name: body.name, description: body.description,
    datasource_id: body.datasource_id, semantic_model_version_id: body.semantic_model_version_id ?? null,
    sql_template: body.sql_template,
    referenced_tables: [],
    parameters: (body.parameters ?? []).map((p) => ({ ...p, required: p.required ?? true, sensitive: p.sensitive ?? false })),
    allowed_roles: [...body.allowed_roles].sort(),
    fingerprint: fakeHex(`${toolId}:${nextVersion}:${body.sql_template}`),
    created_by: "local-ui-admin", approved_by: null, approved_at: null,
    created_at: now, updated_at: now, usage_count: 0,
  };
  items.push(version);
  return version;
}

/** `POST /v1/tool-versions/{id}/submit`. */
export async function makeFixtureSubmitToolForReview(versionId: string): Promise<GovernanceReviewRead> {
  await wait(90);
  const version = findToolVersion(versionId);
  if (!version) throw new ApiError(404, "tool version not found");
  if (version.status !== "DRAFT") {
    throw new ApiError(409, "only a draft tool version can be submitted for review");
  }
  version.status = "REVIEW_REQUIRED";
  version.updated_at = new Date().toISOString();
  return {
    id: `gr_${versionId}`, organization_id: version.organization_id, object_type: "GOVERNED_TOOL_VERSION",
    object_id: versionId, requested_action: "PUBLISH", status: "PENDING", requested_by: "local-ui-admin",
    decided_by: null, decision_reason: null, decided_at: null,
    created_at: version.updated_at, updated_at: version.updated_at,
  };
}

/** `POST /v1/tool-versions/{id}/deprecation-submit`. Mirrors the real
 *  endpoint: it opens a review but does not flip `status` itself -- that
 *  only happens once the review is decided. */
export async function makeFixtureRequestToolDeprecation(versionId: string): Promise<GovernanceReviewRead> {
  await wait(90);
  const version = findToolVersion(versionId);
  if (!version) throw new ApiError(404, "tool version not found");
  if (version.status === "DEPRECATED") throw new ApiError(409, "tool version is already deprecated");
  if (version.status !== "PUBLISHED") throw new ApiError(409, "only a published tool can be deprecated");
  const now = new Date().toISOString();
  return {
    id: `gr_deprecate_${versionId}`, organization_id: version.organization_id, object_type: "GOVERNED_TOOL_VERSION",
    object_id: versionId, requested_action: "DEPRECATE", status: "PENDING", requested_by: "local-ui-admin",
    decided_by: null, decision_reason: null, decided_at: null, created_at: now, updated_at: now,
  };
}

/** `POST /v1/tool-versions/{id}/execute` -- returns a `QueryExecutionResponse`
 *  shaped the same way `makeFixtureAgentAnalysis`'s embedded execution is,
 *  with rows synthesized from the supplied parameters so the result panel
 *  has something concrete to render. */
export async function makeFixtureExecuteToolVersion(
  versionId: string,
  body: ToolExecutionRequest,
): Promise<ToolExecutionResponse> {
  await wait(140);
  const version = findToolVersion(versionId);
  if (!version) throw new ApiError(404, "tool version not found");
  if (version.status !== "PUBLISHED") throw new ApiError(409, "only a published tool can execute");
  const params = body.parameters ?? {};
  const rows: Record<string, unknown>[] =
    version.slug === "customer_lookup"
      ? [
          { customer_id: "c_1001", state: params.state ?? "NY" },
          { customer_id: "c_1042", state: params.state ?? "NY" },
        ]
      : [{ month: "2026-07-01", delinquent_count: 41 }, { month: "2026-08-01", delinquent_count: 37 }];
  const execution: QueryExecutionResponse = {
    execution_id: `qe_${versionId}_${Date.now()}`, status: "SUCCEEDED",
    normalized_sql: version.sql_template,
    referenced_tables: version.referenced_tables,
    referenced_columns: version.parameters.map((p) => p.name),
    column_lineage: [], plan_cost: 3.6, warehouse_query_id: `wh_${versionId}`,
    row_count: rows.length, elapsed_ms: 96, masked_columns: [], rows,
  };
  return {
    tool_execution_id: `te_${versionId}_${Date.now()}`, tool_version_id: version.id,
    tool_slug: version.slug, tool_version: version.version, execution, quality_gate: null,
  };
}

/* ---------------------------------------------------------------------------
   Unified lineage -- `UnifiedLineageScreen`'s own
   `GET /v1/datasources/{id}/unified-lineage/graph` fixture. Reuses the same
   `raw_sales -> orders_raw -> revenue_agg` estate `makeFixtureLineageGraph`
   and `makeFixtureLineageImpact` already model above, extended with one edge
   per remaining legend category (SUGGESTED_RELATIONSHIP, OPENLINEAGE_ETL,
   VIEW_DEFINITION) and one synthetic `UNRESOLVED_DATASET` node -- an
   OpenLineage-only dataset never matched to a catalog table, the exact case
   `UnifiedLineageNodeRead`'s own doc comment (schemas.py) calls out -- so the
   fixture exercises every layer chip and every node-kind topology column the
   real endpoint can return, not just the FK/dbt pair the narrated-lineage
   fixture needed. */
import type { UnifiedLineageGraphQuery } from "./api";

export async function makeFixtureUnifiedLineageGraph(
  datasourceId: string,
  query: UnifiedLineageGraphQuery,
): Promise<UnifiedLineageGraphRead> {
  await wait(110);
  const allNodes: UnifiedLineageGraphRead["nodes"] = [
    { id: "t_raw_sales", node_kind: "TABLE", label: "raw_sales", qualified_name: "analytics.raw.raw_sales", resolved: true, inbound_edge_count: 0, outbound_edge_count: 1 },
    { id: "t_orders_raw", node_kind: "TABLE", label: "orders_raw", qualified_name: "analytics.core.orders_raw", resolved: true, inbound_edge_count: 2, outbound_edge_count: 2 },
    { id: "t_customer_dim", node_kind: "TABLE", label: "customer_dim", qualified_name: "analytics.curated.customer_dim", resolved: true, inbound_edge_count: 0, outbound_edge_count: 2 },
    { id: "t_revenue_agg", node_kind: "DBT_MODEL", label: "revenue_agg", qualified_name: "analytics.mart.revenue_agg", resolved: true, inbound_edge_count: 1, outbound_edge_count: 1 },
    { id: "t_revenue_by_lob", node_kind: "DBT_MODEL", label: "revenue_by_lob", qualified_name: "analytics.mart.revenue_by_lob", resolved: true, inbound_edge_count: 2, outbound_edge_count: 0 },
    { id: "ol_export_job", node_kind: "UNRESOLVED_DATASET", label: "s3://exports/revenue_agg", qualified_name: "openlineage.exports.revenue_agg", resolved: false, inbound_edge_count: 1, outbound_edge_count: 0 },
  ];
  const allEdges: UnifiedLineageGraphRead["edges"] = [
    { id: "fk_raw_orders", edge_source: "FOREIGN_KEY", source_node_id: "t_raw_sales", target_node_id: "t_orders_raw", source_label: "raw_sales", target_label: "orders_raw", status: "ACTIVE", confidence: 1, source_columns: ["sale_id"], target_columns: ["source_sale_id"] },
    { id: "fk_customer_orders", edge_source: "FOREIGN_KEY", source_node_id: "t_customer_dim", target_node_id: "t_orders_raw", source_label: "customer_dim", target_label: "orders_raw", status: "ACTIVE", confidence: 1, source_columns: ["customer_id"], target_columns: ["customer_id"] },
    { id: "sugg_customer_revenue", edge_source: "SUGGESTED_RELATIONSHIP", source_node_id: "t_customer_dim", target_node_id: "t_revenue_by_lob", source_label: "customer_dim", target_label: "revenue_by_lob", status: "PENDING", confidence: 0.62, source_columns: ["customer_id"], target_columns: ["customer_id"] },
    { id: "dbt_orders_revenue", edge_source: "DBT_DEPENDENCY", source_node_id: "t_orders_raw", target_node_id: "t_revenue_agg", source_label: "orders_raw", target_label: "revenue_agg", status: "ACTIVE", confidence: 1, source_columns: [], target_columns: [] },
    { id: "dbt_revenue_lob", edge_source: "DBT_DEPENDENCY", source_node_id: "t_revenue_agg", target_node_id: "t_revenue_by_lob", source_label: "revenue_agg", target_label: "revenue_by_lob", status: "ACTIVE", confidence: 1, source_columns: [], target_columns: [] },
    { id: "ol_revenue_export", edge_source: "OPENLINEAGE_ETL", source_node_id: "t_revenue_agg", target_node_id: "ol_export_job", source_label: "revenue_agg", target_label: "s3://exports/revenue_agg", status: "ACTIVE", confidence: 1, source_columns: [], target_columns: [] },
    { id: "view_orders_customer", edge_source: "VIEW_DEFINITION", source_node_id: "t_customer_dim", target_node_id: "t_orders_raw", source_label: "customer_dim", target_label: "orders_raw", status: "ACTIVE", confidence: 1, source_columns: [], target_columns: [] },
  ];

  const suggestionStatus = query.suggestionStatus ?? "APPROVED";
  const bySuggestion = allEdges.filter(
    (e) => e.edge_source !== "SUGGESTED_RELATIONSHIP" || suggestionStatus === "ALL" || e.status === suggestionStatus,
  );

  const nodeLimit = query.nodeLimit ?? 300;
  const edgeLimit = query.edgeLimit ?? 1500;
  const nodes = allNodes.slice(0, nodeLimit);
  const edges = bySuggestion.slice(0, edgeLimit);
  const nodeIds = new Set(nodes.map((n) => n.id));
  const boundedEdges = edges.filter((e) => nodeIds.has(e.source_node_id) && nodeIds.has(e.target_node_id));

  const truncationReasons: string[] = [];
  if (nodes.length < allNodes.length) truncationReasons.push("node_limit");
  if (edges.length < bySuggestion.length) truncationReasons.push("edge_limit");

  const countsBySource: Record<string, number> = {};
  for (const e of boundedEdges) countsBySource[e.edge_source] = (countsBySource[e.edge_source] ?? 0) + 1;

  return {
    datasource_id: datasourceId,
    nodes,
    edges: boundedEdges,
    counts_by_source: countsBySource,
    returned_node_count: nodes.length,
    returned_edge_count: boundedEdges.length,
    node_limit: nodeLimit,
    edge_limit: edgeLimit,
    truncated: truncationReasons.length > 0,
    truncation_reasons: truncationReasons,
  };
}


/* ---------------------------------------------------------------------------
   AI governance fixtures -- `AiGovernanceScreen`'s model-route registry,
   runtime-status rail, and repeatable control-evaluation suite. See
   `api.ts`'s "AI governance" block for the real endpoints these mirror.
--------------------------------------------------------------------------- */

import type { AgentEvaluationQuery, ModelRouteQuery } from "./api";

const FIXTURE_MODEL_ROUTES: ModelRouteConfigurationRead[] = [
  {
    id: "route_bank_sql_primary_1", organization_id: "00000000-0000-0000-0000-000000000001",
    route_key: "bank-sql-primary", version: 1, status: "APPROVED",
    display_name: "Bank SQL generation", provider_type: "OPENAI", model_id: "approved-deployment-alias",
    endpoint_alias: "private-ai-east-01", uses_credential_reference: true, data_residency: "US",
    retention_policy: "ZERO_RETENTION", capabilities: ["SQL_GENERATION", "CLASSIFICATION"],
    max_input_tokens: 8000, max_output_tokens: 2000, timeout_seconds: 30,
    fingerprint: fakeHex("bank-sql-primary:1"),
    created_by: "local-ui-admin", approved_by: "steward@tenant.example", approved_at: "2026-08-20T00:00:00Z",
    selected_by_runtime: true, adapter_available: true, activation_status: "READY",
    created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-20T00:00:00Z",
  },
  {
    id: "route_bank_explain_1", organization_id: "00000000-0000-0000-0000-000000000001",
    route_key: "bank-explain", version: 1, status: "DRAFT",
    display_name: "Bank explanation drafting", provider_type: "AZURE_OPENAI", model_id: "explain-deployment-alias",
    endpoint_alias: "private-ai-east-02", uses_credential_reference: true, data_residency: "US",
    retention_policy: "BANK_MANAGED", capabilities: ["EXPLANATION"],
    max_input_tokens: 6000, max_output_tokens: 1500, timeout_seconds: 25,
    fingerprint: fakeHex("bank-explain:1"),
    created_by: "local-ui-admin", approved_by: null, approved_at: null,
    selected_by_runtime: false, adapter_available: false, activation_status: "DRAFT",
    created_at: "2026-08-25T00:00:00Z", updated_at: "2026-08-25T00:00:00Z",
  },
];

/** `GET /v1/organizations/{organization_id}/model-routes`. */
export async function makeFixtureModelRoutes(
  organizationId: string,
  query: ModelRouteQuery,
): Promise<PageOf<ModelRouteConfigurationRead>> {
  await wait(80);
  void organizationId;
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return {
    items: FIXTURE_MODEL_ROUTES.slice(offset, offset + limit),
    limit, offset, total: FIXTURE_MODEL_ROUTES.length,
  };
}

/** `POST /v1/organizations/{organization_id}/model-routes` -- mirrors
 *  `create_model_route`'s shape: a new `DRAFT` version, with `version`
 *  auto-incremented per `route_key` exactly as the server does. */
export async function makeFixtureCreateModelRoute(
  organizationId: string,
  body: ModelRouteConfigurationCreate,
): Promise<ModelRouteConfigurationRead> {
  await wait(100);
  const priorVersions = FIXTURE_MODEL_ROUTES.filter((r) => r.route_key === body.route_key).map((r) => r.version);
  const version = priorVersions.length ? Math.max(...priorVersions) + 1 : 1;
  const now = new Date().toISOString();
  const route: ModelRouteConfigurationRead = {
    id: `route_${body.route_key.replace(/[^a-z0-9]+/g, "_")}_${version}`,
    organization_id: organizationId,
    route_key: body.route_key, version, status: "DRAFT",
    display_name: body.display_name, provider_type: body.provider_type, model_id: body.model_id,
    endpoint_alias: body.endpoint_alias, uses_credential_reference: Boolean(body.credential_reference),
    data_residency: body.data_residency, retention_policy: body.retention_policy,
    capabilities: [...new Set(body.capabilities)],
    max_input_tokens: body.max_input_tokens ?? 8000, max_output_tokens: body.max_output_tokens ?? 2000,
    timeout_seconds: body.timeout_seconds ?? 30,
    fingerprint: fakeHex(`${body.route_key}:${version}:${JSON.stringify(body)}`),
    created_by: "local-ui-admin", approved_by: null, approved_at: null,
    selected_by_runtime: false, adapter_available: false, activation_status: "DRAFT",
    created_at: now, updated_at: now,
  };
  FIXTURE_MODEL_ROUTES.unshift(route);
  return route;
}

/** `POST /v1/model-routes/{route_id}/submit`. */
export async function makeFixtureSubmitModelRoute(routeId: string): Promise<GovernanceReviewRead> {
  await wait(90);
  const route = FIXTURE_MODEL_ROUTES.find((r) => r.id === routeId);
  if (!route) throw new ApiError(404, "model route not found");
  if (route.status !== "DRAFT") {
    throw new ApiError(409, "only draft model routes can be submitted");
  }
  route.status = "PENDING_REVIEW";
  route.activation_status = "PENDING_REVIEW";
  route.updated_at = new Date().toISOString();
  return {
    id: `gr_route_${routeId}`, organization_id: route.organization_id, object_type: "MODEL_ROUTE_CONFIGURATION",
    object_id: routeId, requested_action: "APPROVE_MODEL_ROUTE", status: "PENDING", requested_by: "local-ui-admin",
    decided_by: null, decision_reason: null, decided_at: null,
    created_at: route.updated_at, updated_at: route.updated_at,
  };
}

/** `GET /v1/ai/runtime-status` -- mirrors `ai_runtime_status`'s real
 *  development-mode defaults (`api.py:181`) so fixture mode shows the same
 *  "Development headers only" identity posture a fresh dev compose stack
 *  actually returns. */
export async function makeFixtureAiRuntimeStatus(): Promise<AiRuntimeStatusRead> {
  await wait(50);
  return {
    orchestration_mode: "HYBRID",
    runtime: "FRAMEWORK_NEUTRAL_TYPED_STATE_MACHINE",
    runtime_version: "v2",
    model_route_status: "CONFIGURED",
    model_generation_enabled: true,
    available_model_providers: ["GOOGLE_GEMINI", "OPENAI"],
    development_sql_override_enabled: false,
    identity_provider: "DEVELOPMENT",
    identity_verification: "DEVELOPMENT_HEADERS_ONLY",
    oidc_configured: false,
    credential_provider: "ENV",
    credential_provider_available: true,
    enterprise_security_ready: false,
    deterministic_controls: [
      "authorization",
      "prompt_risk_classification",
      "governed_metadata_retrieval",
      "approved_tool_first_planning",
      "metadata_resolution",
      "semantic_version_resolution",
      "sql_ast_validation",
      "catalog_allowlisting",
      "query_cost_gate",
      "row_limit",
      "sensitive_data_masking",
      "audit_evidence",
      "repeatable_control_evaluation",
    ],
    optional_framework_adapters: ["LangGraph", "Google ADK"],
    data_retention_statement:
      "Raw analyst questions are not persisted; only an HMAC digest and bounded evidence are retained.",
  };
}

const FIXTURE_AGENT_EVALUATIONS: AgentEvaluationRunRead[] = [
  {
    id: "eval_2026_08", organization_id: "00000000-0000-0000-0000-000000000001",
    principal_id: "local-ui-admin", suite_version: "2026.08", status: "PASSED",
    scenario_count: 18, passed_count: 18, failed_count: 0, pass_rate: 1,
    findings: [],
    created_at: "2026-08-15T09:00:00Z", updated_at: "2026-08-15T09:00:00Z",
  },
  {
    id: "eval_2026_07", organization_id: "00000000-0000-0000-0000-000000000001",
    principal_id: "local-ui-admin", suite_version: "2026.07", status: "FAILED",
    scenario_count: 18, passed_count: 16, failed_count: 2, pass_rate: 0.888888888888889,
    findings: [
      { scenario: "masked_pii_leak_probe", detail: "one row exposed an unmasked column" },
      { scenario: "prompt_injection_refusal", detail: "refusal evidence missing correlation id" },
    ],
    created_at: "2026-07-15T09:00:00Z", updated_at: "2026-07-15T09:00:00Z",
  },
];

/** `GET /v1/organizations/{organization_id}/agent-evaluations`. */
export async function makeFixtureAgentEvaluations(
  organizationId: string,
  query: AgentEvaluationQuery,
): Promise<PageOf<AgentEvaluationRunRead>> {
  await wait(80);
  void organizationId;
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return {
    items: FIXTURE_AGENT_EVALUATIONS.slice(offset, offset + limit),
    limit, offset, total: FIXTURE_AGENT_EVALUATIONS.length,
  };
}

/** `POST /v1/organizations/{organization_id}/agent-evaluations` -- mirrors
 *  `run_agent_evaluation`'s shape: always a clean PASSED run in fixture
 *  mode (the deterministic suite it wraps, `run_control_evaluation`, has no
 *  frontend-observable inputs to vary). */
export async function makeFixtureRunAgentEvaluation(organizationId: string): Promise<AgentEvaluationRunRead> {
  await wait(150);
  const now = new Date().toISOString();
  const run: AgentEvaluationRunRead = {
    id: `eval_${Date.now()}`, organization_id: organizationId, principal_id: "local-ui-admin",
    suite_version: "2026.09", status: "PASSED", scenario_count: 18, passed_count: 18, failed_count: 0,
    pass_rate: 1, findings: [], created_at: now, updated_at: now,
  };
  FIXTURE_AGENT_EVALUATIONS.unshift(run);
  return run;
}

/* ---------------------------------------------------------------------------
   Transformations fixtures -- backs `TransformationsScreen` / the six dbt
   functions appended to `api.ts`. Seeded under `proj_core` /
   `ds_snowflake_prod` (the same project + the one datasource
   `makeFixtureOrgDatasources` lists) so the create -> browse flow works
   against the same fixture estate every other screen already uses, and the
   dependency story below deliberately mirrors `makeFixtureLineageGraph`'s
   own `orders_raw -> revenue_agg` DBT_DEPENDENCY edge -- same tables
   (`t_orders_raw`/`t_customer_dim`/`t_revenue_agg`), same shape, so a viewer
   who has looked at Unified lineage recognizes this estate rather than
   meeting a disconnected demo.
--------------------------------------------------------------------------- */

import type {
  DbtArtifactImportRead,
  DbtArtifactImportRequest,
  DbtLineageEdgeRead,
  DbtLineageNodeRead,
  DbtLineageRead,
  DbtProjectCreate,
  DbtProjectRead,
} from "./types";
import type { DbtResourceQuery, DbtResourceRead } from "./api";

const DBT_ORG_ID = "00000000-0000-0000-0000-000000000001";
const DBT_SEED_ARTIFACT_ID = "dbtimport_seed_1";

const FIXTURE_DBT_PROJECTS: Record<string, DbtProjectRead[]> = {
  proj_core: [
    {
      id: "dbtproj_consumer_analytics",
      organization_id: DBT_ORG_ID,
      project_id: "proj_core",
      datasource_id: "ds_snowflake_prod",
      project_key: "consumer_analytics",
      display_name: "Consumer analytics transformations",
      repository_url: "https://git.example/bank/consumer-analytics",
      target_name: "prod",
      status: "ACTIVE",
      created_by: "data-eng@tenant.example",
      created_at: "2026-07-01T00:00:00Z",
      updated_at: "2026-08-20T09:00:05Z",
    },
  ],
};

const FIXTURE_DBT_RESOURCES: Record<string, DbtResourceRead[]> = {
  [DBT_SEED_ARTIFACT_ID]: [
    {
      id: "dbtres_raw_orders", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "source.consumer_analytics.raw.orders_raw", resource_type: "SOURCE",
      package_name: "consumer_analytics", name: "orders_raw",
      database_name: "analytics", schema_name: "raw", relation_name: "analytics.raw.orders_raw",
      materialization: null, original_file_path: "models/staging/src_raw.yml",
      description: "Raw order events landed by the warehouse ingestion job.",
      compiled_sql_hash: null, compiled_sql_redacted: null, sql_parse_status: "NOT_PRESENT",
      column_names: ["order_id", "customer_id", "order_date", "amount"],
      column_descriptions: { order_id: "Primary key of the source order record." },
      column_types: { order_id: "NUMBER", customer_id: "NUMBER", order_date: "DATE", amount: "NUMBER(18,2)" },
      tags: ["raw"], depends_on_unique_ids: [], matched_table_id: "t_orders_raw",
      test_status: null, test_failures: null, test_execution_time: null, extra_metadata: {},
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
    {
      id: "dbtres_raw_customers", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "source.consumer_analytics.raw.customers", resource_type: "SOURCE",
      package_name: "consumer_analytics", name: "customers",
      database_name: "analytics", schema_name: "raw", relation_name: "analytics.raw.customers",
      materialization: null, original_file_path: "models/staging/src_raw.yml",
      description: "Raw customer records landed by the CRM extract.",
      compiled_sql_hash: null, compiled_sql_redacted: null, sql_parse_status: "NOT_PRESENT",
      column_names: ["customer_id", "full_name", "region", "signup_date"],
      column_descriptions: {},
      column_types: { customer_id: "NUMBER", full_name: "VARCHAR", region: "VARCHAR", signup_date: "DATE" },
      tags: ["raw"], depends_on_unique_ids: [], matched_table_id: null,
      test_status: null, test_failures: null, test_execution_time: null, extra_metadata: {},
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
    {
      id: "dbtres_stg_orders", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "model.consumer_analytics.stg_orders", resource_type: "MODEL",
      package_name: "consumer_analytics", name: "stg_orders",
      database_name: "analytics", schema_name: "staging", relation_name: "analytics.staging.stg_orders",
      materialization: "view", original_file_path: "models/staging/stg_orders.sql",
      description: "One row per order, typed and renamed from the raw source.",
      compiled_sql_hash: fakeHex("stg_orders:sql"),
      compiled_sql_redacted:
        "SELECT\n  order_id,\n  customer_id,\n  order_date,\n  amount AS order_amount\nFROM analytics.raw.orders_raw\nWHERE amount > ?",
      sql_parse_status: "PARSED",
      column_names: ["order_id", "customer_id", "order_date", "order_amount"],
      column_descriptions: { order_amount: "Order amount renamed from the raw `amount` column." },
      column_types: { order_id: "NUMBER", customer_id: "NUMBER", order_date: "DATE", order_amount: "NUMBER(18,2)" },
      tags: ["staging"], depends_on_unique_ids: ["source.consumer_analytics.raw.orders_raw"], matched_table_id: null,
      test_status: null, test_failures: null, test_execution_time: null, extra_metadata: {},
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
    {
      id: "dbtres_dim_customer", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "model.consumer_analytics.dim_customer", resource_type: "MODEL",
      package_name: "consumer_analytics", name: "dim_customer",
      database_name: "analytics", schema_name: "curated", relation_name: "analytics.curated.customer_dim",
      materialization: "table", original_file_path: "models/marts/dim_customer.sql",
      description: "One row per customer with region and tenure attributes.",
      compiled_sql_hash: fakeHex("dim_customer:sql"),
      compiled_sql_redacted: "SELECT\n  customer_id,\n  full_name,\n  region,\n  signup_date\nFROM analytics.raw.customers",
      sql_parse_status: "PARSED",
      column_names: ["customer_id", "full_name", "region", "signup_date"],
      column_descriptions: {},
      column_types: { customer_id: "NUMBER", full_name: "VARCHAR", region: "VARCHAR", signup_date: "DATE" },
      tags: ["marts", "curated"], depends_on_unique_ids: ["source.consumer_analytics.raw.customers"], matched_table_id: "t_customer_dim",
      test_status: null, test_failures: null, test_execution_time: null, extra_metadata: {},
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
    {
      id: "dbtres_revenue_agg", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "model.consumer_analytics.revenue_agg", resource_type: "MODEL",
      package_name: "consumer_analytics", name: "revenue_agg",
      database_name: "analytics", schema_name: "mart", relation_name: "analytics.mart.revenue_agg",
      materialization: "table", original_file_path: "models/marts/revenue_agg.sql",
      description: "Daily revenue rollup by customer region.",
      compiled_sql_hash: fakeHex("revenue_agg:sql"),
      compiled_sql_redacted:
        "SELECT\n  d.region,\n  o.order_date,\n  SUM(o.order_amount) AS revenue\nFROM analytics.staging.stg_orders o\nJOIN analytics.curated.customer_dim d ON d.customer_id = o.customer_id\nGROUP BY d.region, o.order_date",
      sql_parse_status: "PARSED",
      column_names: ["region", "order_date", "revenue"],
      column_descriptions: { revenue: "Sum of order_amount for the region and day." },
      column_types: { region: "VARCHAR", order_date: "DATE", revenue: "NUMBER(18,2)" },
      tags: ["marts"],
      depends_on_unique_ids: ["model.consumer_analytics.stg_orders", "model.consumer_analytics.dim_customer"],
      matched_table_id: "t_revenue_agg",
      test_status: null, test_failures: null, test_execution_time: null, extra_metadata: { owner: "risk-data-stewards" },
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
    {
      id: "dbtres_test_notnull_revenue", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "test.consumer_analytics.not_null_revenue_agg_region", resource_type: "TEST",
      package_name: "consumer_analytics", name: "not_null_revenue_agg_region",
      database_name: null, schema_name: null, relation_name: null,
      materialization: null, original_file_path: "models/marts/revenue_agg.yml",
      description: "Asserts revenue_agg.region is never null.",
      compiled_sql_hash: fakeHex("test_notnull_revenue:sql"),
      compiled_sql_redacted: "SELECT region\nFROM analytics.mart.revenue_agg\nWHERE region IS NULL",
      sql_parse_status: "PARSED",
      column_names: [], column_descriptions: {}, column_types: {},
      tags: [], depends_on_unique_ids: ["model.consumer_analytics.revenue_agg"], matched_table_id: null,
      test_status: "PASS", test_failures: 0, test_execution_time: 0.82, extra_metadata: {},
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
    {
      id: "dbtres_test_unique_stg_orders", artifact_import_id: DBT_SEED_ARTIFACT_ID,
      unique_id: "test.consumer_analytics.unique_stg_orders_order_id", resource_type: "TEST",
      package_name: "consumer_analytics", name: "unique_stg_orders_order_id",
      database_name: null, schema_name: null, relation_name: null,
      materialization: null, original_file_path: "models/staging/stg_orders.yml",
      description: "Asserts stg_orders.order_id is unique.",
      compiled_sql_hash: fakeHex("test_unique_stg_orders:sql"),
      compiled_sql_redacted: "SELECT order_id, COUNT(*)\nFROM analytics.staging.stg_orders\nGROUP BY order_id\nHAVING COUNT(*) > 1",
      sql_parse_status: "PARSED",
      column_names: [], column_descriptions: {}, column_types: {},
      tags: [], depends_on_unique_ids: ["model.consumer_analytics.stg_orders"], matched_table_id: null,
      test_status: "FAIL", test_failures: 3, test_execution_time: 0.41, extra_metadata: {},
      created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
    },
  ],
};

function dbtLineageFromResources(artifactId: string, resources: DbtResourceRead[]): DbtLineageRead {
  const byUniqueId = new Map(resources.map((r) => [r.unique_id, r]));
  const nodes: DbtLineageNodeRead[] = resources.map((r) => ({
    id: r.id, unique_id: r.unique_id, label: r.name, resource_type: r.resource_type,
    materialization: r.materialization, matched_table_id: r.matched_table_id, test_status: r.test_status,
  }));
  const edges: DbtLineageEdgeRead[] = [];
  for (const resource of resources) {
    for (const dependsOn of resource.depends_on_unique_ids) {
      const source = byUniqueId.get(dependsOn);
      if (!source) continue;
      edges.push({
        id: `${source.id}->${resource.id}`,
        source_resource_id: source.id, target_resource_id: resource.id,
        edge_type: "DEPENDS_ON", source_column: "", target_column: "",
      });
    }
  }
  const matched = resources.filter((r) => r.matched_table_id).length;
  return {
    artifact_import_id: artifactId, nodes, edges,
    resource_count: resources.length, edge_count: edges.length, catalog_match_count: matched,
  };
}

const FIXTURE_DBT_LINEAGE: Record<string, DbtLineageRead> = {
  [DBT_SEED_ARTIFACT_ID]: dbtLineageFromResources(DBT_SEED_ARTIFACT_ID, FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!),
};

const FIXTURE_DBT_IMPORTS: Record<string, DbtArtifactImportRead[]> = {
  dbtproj_consumer_analytics: [
    {
      id: DBT_SEED_ARTIFACT_ID,
      organization_id: DBT_ORG_ID,
      dbt_project_id: "dbtproj_consumer_analytics",
      manifest_fingerprint: fakeHex(`${DBT_SEED_ARTIFACT_ID}:manifest`),
      dbt_schema_version: "https://schemas.getdbt.com/dbt/manifest/v12.json",
      dbt_version: "1.8.3",
      invocation_id: "6f1e6b1a-3c9d-4a3f-9b7e-2c6a2e6f1a02",
      generated_at: "2026-08-20T09:00:00Z",
      status: "IMPORTED",
      resource_count: FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!.length,
      model_count: FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!.filter((r) => r.resource_type === "MODEL").length,
      source_count: FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!.filter((r) => r.resource_type === "SOURCE").length,
      test_count: FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!.filter((r) => r.resource_type === "TEST").length,
      lineage_edge_count: FIXTURE_DBT_LINEAGE[DBT_SEED_ARTIFACT_ID]!.edges.length,
      matched_resource_count: FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!.filter((r) => r.matched_table_id).length,
      unmatched_resource_count: FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID]!.filter(
        (r) => !r.matched_table_id && r.resource_type !== "TEST",
      ).length,
      imported_by: "local-ui-admin",
      created_at: "2026-08-20T09:00:05Z",
      updated_at: "2026-08-20T09:00:05Z",
    },
  ],
};

/** `GET /v1/projects/{id}/dbt-projects`. */
export async function makeFixtureDbtProjects(projectId: string): Promise<PageOf<DbtProjectRead>> {
  await wait(70);
  const items = FIXTURE_DBT_PROJECTS[projectId] ?? [];
  return { items, limit: 500, offset: 0, total: items.length };
}

/** `POST /v1/projects/{id}/dbt-projects` -- mirrors `create_dbt_project`'s
 *  shape: a new, `ACTIVE` project row with the server-assigned fields
 *  (`id`, `status`, `created_by`, timestamps) a real create response would
 *  carry, and a 409 on a duplicate `project_key` matching the real route's
 *  own unique-constraint conflict (`dbt_api.py:180-183`). */
export async function makeFixtureCreateDbtProject(
  projectId: string,
  body: DbtProjectCreate,
): Promise<DbtProjectRead> {
  await wait(120);
  const items = (FIXTURE_DBT_PROJECTS[projectId] ??= []);
  if (items.some((p) => p.project_key === body.project_key)) {
    throw new ApiError(409, "dbt project key already exists");
  }
  const now = new Date().toISOString();
  const id = `dbtproj_${body.project_key.replace(/[^a-z0-9]+/g, "_")}`;
  const project: DbtProjectRead = {
    id, organization_id: DBT_ORG_ID, project_id: projectId, datasource_id: body.datasource_id,
    project_key: body.project_key, display_name: body.display_name,
    repository_url: body.repository_url ?? null, target_name: body.target_name ?? "prod",
    status: "ACTIVE", created_by: "local-ui-admin", created_at: now, updated_at: now,
  };
  items.push(project);
  FIXTURE_DBT_IMPORTS[id] ??= [];
  return project;
}

/** `GET /v1/dbt-projects/{id}/artifact-imports`. */
export async function makeFixtureDbtArtifactImports(dbtProjectId: string): Promise<PageOf<DbtArtifactImportRead>> {
  await wait(70);
  const items = FIXTURE_DBT_IMPORTS[dbtProjectId] ?? [];
  return { items, limit: 100, offset: 0, total: items.length };
}

let dbtImportSequence = 0;

/** `POST /v1/dbt-projects/{id}/artifact-imports` -- fixture mode does not
 *  reimplement the server's dbt manifest parser (`dbt_artifacts.py`): it
 *  reuses the seed artifact's already-parsed resource/lineage payload for
 *  any newly "imported" manifest (the uploaded file's actual content is not
 *  read here), so the register -> import -> browse flow is real end to end
 *  without a second copy of manifest-parsing logic in the client. */
export async function makeFixtureImportDbtManifest(
  dbtProjectId: string,
  _body: DbtArtifactImportRequest,
): Promise<DbtArtifactImportRead> {
  await wait(160);
  const imports = (FIXTURE_DBT_IMPORTS[dbtProjectId] ??= []);
  const project = Object.values(FIXTURE_DBT_PROJECTS).flat().find((p) => p.id === dbtProjectId);
  if (!project) throw new ApiError(404, "dbt project not found");
  const seedResources = FIXTURE_DBT_RESOURCES[DBT_SEED_ARTIFACT_ID] ?? [];
  dbtImportSequence += 1;
  const now = new Date().toISOString();
  const id = `dbtimport_${dbtProjectId}_${dbtImportSequence}`;
  const resources = seedResources.map((r) => ({ ...r, id: `${r.id}_${dbtImportSequence}`, artifact_import_id: id }));
  const lineage = dbtLineageFromResources(id, resources);
  const artifact: DbtArtifactImportRead = {
    id, organization_id: DBT_ORG_ID, dbt_project_id: dbtProjectId,
    manifest_fingerprint: fakeHex(`${id}:manifest`),
    dbt_schema_version: "https://schemas.getdbt.com/dbt/manifest/v12.json",
    dbt_version: "1.8.3", invocation_id: fakeHex(`${id}:invocation`, 36),
    generated_at: now, status: "IMPORTED",
    resource_count: resources.length,
    model_count: resources.filter((r) => r.resource_type === "MODEL").length,
    source_count: resources.filter((r) => r.resource_type === "SOURCE").length,
    test_count: resources.filter((r) => r.resource_type === "TEST").length,
    lineage_edge_count: lineage.edges.length,
    matched_resource_count: resources.filter((r) => r.matched_table_id).length,
    unmatched_resource_count: resources.filter((r) => !r.matched_table_id && r.resource_type !== "TEST").length,
    imported_by: "local-ui-admin", created_at: now, updated_at: now,
  };
  imports.unshift(artifact);
  FIXTURE_DBT_RESOURCES[id] = resources;
  FIXTURE_DBT_LINEAGE[id] = lineage;
  return artifact;
}

/** `GET /v1/dbt-artifact-imports/{id}/resources`. */
export async function makeFixtureDbtResources(
  artifactImportId: string,
  query: DbtResourceQuery,
): Promise<PageOf<DbtResourceRead>> {
  await wait(80);
  let items = FIXTURE_DBT_RESOURCES[artifactImportId] ?? [];
  if (query.resourceType) items = items.filter((r) => r.resource_type === query.resourceType);
  if (query.matched === true) items = items.filter((r) => r.matched_table_id !== null);
  if (query.matched === false) items = items.filter((r) => r.matched_table_id === null);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 500;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `GET /v1/dbt-artifact-imports/{id}/lineage`. */
export async function makeFixtureDbtLineage(artifactImportId: string): Promise<DbtLineageRead> {
  await wait(90);
  return (
    FIXTURE_DBT_LINEAGE[artifactImportId] ?? {
      artifact_import_id: artifactImportId,
      nodes: [], edges: [], resource_count: 0, edge_count: 0, catalog_match_count: 0,
    }
  );
}

/* ---------------------------------------------------------------------------
   ABAC access policies + authorization simulation -- see `api.ts`'s matching
   section for the real routes these fixtures stand in for.
--------------------------------------------------------------------------- */

import type { AccessPolicyCreate, AccessPolicyRead, AuthorizationSimulationRead, AuthorizationSimulationRequest } from "./types";
import type { AccessPolicyQuery } from "./api";

const FIXTURE_ACCESS_POLICIES: Record<string, AccessPolicyRead[]> = {
  "00000000-0000-0000-0000-000000000001": [
    {
      id: "policy_pii_mask", organization_id: "00000000-0000-0000-0000-000000000001",
      code: "mask-pii-columns", version: 2, name: "Mask PII columns for analysts",
      description: "Masks direct identifiers for any subject without the DataSteward role.",
      effect: "MASK", priority: 50,
      subject_match: { roles_not_in: ["DataSteward", "PlatformAdmin"] },
      resource_match: { classifications: ["PII"] },
      action_match: ["READ_DATA", "EXPORT"],
      transform: { strategy: "HASH", columns: ["ssn", "email", "phone"] },
      condition: {},
      origin: "MANUAL", status: "ACTIVE",
      created_by: "local-ui-admin", created_at: "2026-02-01T00:00:00Z", updated_at: "2026-06-15T00:00:00Z",
    },
    {
      id: "policy_export_block", organization_id: "00000000-0000-0000-0000-000000000001",
      code: "deny-restricted-export", version: 1, name: "Deny export of restricted data",
      description: "Blocks EXPORT of RESTRICTED-classified resources outside the Auditor/PlatformAdmin roles.",
      effect: "DENY", priority: 10,
      subject_match: { roles_not_in: ["Auditor", "PlatformAdmin"] },
      resource_match: { classifications: ["RESTRICTED"] },
      action_match: ["EXPORT"],
      transform: {},
      condition: {},
      origin: "MANUAL", status: "DRAFT",
      created_by: "local-ui-admin", created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
    },
  ],
};

/** `GET /v1/organizations/{organization_id}/access-policies`. */
export async function makeFixtureAccessPolicies(
  organizationId: string,
  query: AccessPolicyQuery,
): Promise<PageOf<AccessPolicyRead>> {
  await wait(70);
  const all = FIXTURE_ACCESS_POLICIES[organizationId] ?? [];
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 200;
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** `POST /v1/organizations/{organization_id}/access-policies` -- mirrors the
 *  real endpoint's per-`code` version increment: creating again under a
 *  `code` already present for this organization appends a new row with
 *  `version` one higher, rather than replacing the existing one. */
export async function makeFixtureCreateAccessPolicy(
  organizationId: string,
  body: AccessPolicyCreate,
): Promise<AccessPolicyRead> {
  await wait(110);
  const items = (FIXTURE_ACCESS_POLICIES[organizationId] ??= []);
  const existing = items.filter((p) => p.code === body.code);
  const nextVersion = existing.length ? Math.max(...existing.map((p) => p.version)) + 1 : 1;
  const now = new Date().toISOString();
  const policy: AccessPolicyRead = {
    id: `policy_${body.code}_${nextVersion}`, organization_id: organizationId,
    code: body.code, version: nextVersion, name: body.name, description: body.description ?? "",
    effect: body.effect, priority: body.priority ?? 100,
    subject_match: body.subject_match ?? {}, resource_match: body.resource_match ?? {},
    action_match: body.action_match ?? [], transform: body.transform ?? {}, condition: body.condition ?? {},
    origin: "MANUAL", status: body.status ?? "DRAFT",
    created_by: "local-ui-admin", created_at: now, updated_at: now,
  };
  items.push(policy);
  return policy;
}

/** `POST /v1/workspaces/{workspace_id}/authorization-simulations` -- a
 *  lightweight stand-in for the real policy engine: each subject is denied
 *  when it carries none of the roles this fixture treats as privileged
 *  (`DataSteward`/`PlatformAdmin`/`Auditor`), masked when the request
 *  touches a `PII` classification, and allowed otherwise -- enough to
 *  exercise the screen's decision table without reimplementing ABAC
 *  evaluation client-side. */
export async function makeFixtureSimulateAuthorization(
  workspaceId: string,
  body: AuthorizationSimulationRequest,
): Promise<AuthorizationSimulationRead> {
  await wait(130);
  const privileged = new Set(["DataSteward", "PlatformAdmin", "Auditor"]);
  const touchesPii = (body.classifications ?? []).includes("PII");
  const decisions = body.subjects.map((subject) => {
    const roles = subject.roles ?? [];
    const isPrivileged = roles.some((r) => privileged.has(r));
    if (!isPrivileged && body.action === "EXPORT") {
      return {
        principal_kind: subject.principal_kind ?? "HUMAN", roles,
        allowed: false, reason_code: "POLICY_DENY",
        matched_policy_code: "deny-restricted-export",
        masked_classifications: [], row_filters: [],
      };
    }
    if (!isPrivileged && touchesPii) {
      return {
        principal_kind: subject.principal_kind ?? "HUMAN", roles,
        allowed: true, reason_code: "POLICY_MASK",
        matched_policy_code: "mask-pii-columns",
        masked_classifications: ["PII"], row_filters: [],
      };
    }
    return {
      principal_kind: subject.principal_kind ?? "HUMAN", roles,
      allowed: true, reason_code: "NO_MATCHING_POLICY",
      matched_policy_code: null,
      masked_classifications: [], row_filters: [],
    };
  });
  return { workspace_id: workspaceId, decisions };
}

/* ---------------------------------------------------------------------------
   Compliance packs fixtures -- wire-shape identical to compliance_api.py
   (module EE.4/OB-5), so VITE_USE_FIXTURES=0 swaps to the real endpoints
   unchanged. The store is mutable so a freshly generated pack shows up in
   the very next list fetch, the same idiom `makeFixtureSubmitStudioChangeSet`
   uses for its own mutable store above.
--------------------------------------------------------------------------- */

import type { CompliancePackRead, GeneratePackRequest } from "./types";

const COMPLIANCE_PACK_ORG = "00000000-0000-0000-0000-000000000001";

const COMPLIANCE_PACKS: CompliancePackRead[] = [
  {
    id: "pack_bcbs239_q2", organization_id: COMPLIANCE_PACK_ORG,
    name: "BCBS 239 Q2 2026 risk data aggregation", framework: "BCBS_239",
    period_start: "2026-04-01T00:00:00Z", period_end: "2026-06-30T23:59:59Z",
    sections: [
      { title: "Data lineage completeness", finding_count: 3 },
      { title: "Aggregation accuracy", finding_count: 0 },
    ],
    status: "COMPLETE", checksum: "sha256:9f2c1a7e4b6d8035a1c9e7f2b4d6a8035c9e7",
    generated_by: "compliance-officer@tenant.example",
    generated_at: "2026-07-02T09:14:00Z",
    created_at: "2026-07-02T09:14:00Z", updated_at: "2026-07-02T09:14:00Z",
  },
  {
    id: "pack_model_risk_h1", organization_id: COMPLIANCE_PACK_ORG,
    name: "Model risk management H1 2026", framework: "MODEL_RISK",
    period_start: "2026-01-01T00:00:00Z", period_end: "2026-06-30T23:59:59Z",
    sections: [{ title: "Model inventory", finding_count: 1 }],
    status: "COMPLETE", checksum: "sha256:1b3d5f7a9c1e3f5a7c9e1b3d5f7a9c1e3f5a7c",
    generated_by: "priya@tenant.example",
    generated_at: "2026-07-05T16:40:00Z",
    created_at: "2026-07-05T16:40:00Z", updated_at: "2026-07-05T16:40:00Z",
  },
  {
    id: "pack_access_review_aug", organization_id: COMPLIANCE_PACK_ORG,
    name: "Quarterly access review -- August 2026", framework: "ACCESS_REVIEW",
    period_start: "2026-08-01T00:00:00Z", period_end: "2026-08-31T23:59:59Z",
    sections: [], status: "PENDING", checksum: "",
    generated_by: "steward@tenant.example",
    generated_at: "2026-09-01T07:00:00Z",
    created_at: "2026-09-01T07:00:00Z", updated_at: "2026-09-01T07:00:00Z",
  },
];

const COMPLIANCE_FRAMEWORK_LABEL: Record<string, string> = {
  MODEL_RISK: "Model risk management",
  BCBS_239: "BCBS 239 risk data aggregation",
  ACCESS_REVIEW: "Quarterly access review",
  AI_USAGE: "AI usage and governance",
  CHANGE_CONTROL: "Change control",
};

/** `GET /v1/compliance/packs` (`list_compliance_packs`). */
export async function makeFixtureCompliancePacks(
  query: { framework?: string | null; limit?: number; offset?: number },
): Promise<PageOf<CompliancePackRead>> {
  await wait(90);
  const filtered = query.framework
    ? COMPLIANCE_PACKS.filter((p) => p.framework === query.framework)
    : COMPLIANCE_PACKS;
  const sorted = [...filtered].sort((a, b) => b.generated_at.localeCompare(a.generated_at));
  const limit = query.limit ?? 50;
  const offset = query.offset ?? 0;
  return { items: sorted.slice(offset, offset + limit), limit, offset, total: sorted.length };
}

/** `POST /v1/compliance/packs/generate` (`generate_compliance_pack`) --
 *  mirrors the real route's own validation (`period_end` must be after
 *  `period_start`, `compliance_api.py:74`) rather than always succeeding,
 *  and prepends the new pack so it appears at the top of the very next list
 *  fetch, like the real endpoint. */
export async function makeFixtureGenerateCompliancePack(
  body: GeneratePackRequest,
): Promise<CompliancePackRead> {
  await wait(140);
  if (new Date(body.period_end).getTime() <= new Date(body.period_start).getTime()) {
    throw new Error("period_end must be after period_start");
  }
  const now = new Date().toISOString();
  const record: CompliancePackRead = {
    id: `pack_${Math.random().toString(36).slice(2, 10)}`,
    organization_id: COMPLIANCE_PACK_ORG,
    name: body.name || `${COMPLIANCE_FRAMEWORK_LABEL[body.framework] ?? body.framework} pack`,
    framework: body.framework,
    period_start: body.period_start,
    period_end: body.period_end,
    sections: [{ title: "Evidence collection", finding_count: 0 }],
    status: "COMPLETE",
    checksum: `sha256:${Math.random().toString(16).slice(2, 10)}fixture`,
    generated_by: "local-ui-admin",
    generated_at: now,
    created_at: now,
    updated_at: now,
  };
  COMPLIANCE_PACKS.unshift(record);
  return record;
}

/** `GET /v1/compliance/packs/{pack_id}/download` (`download_compliance_pack`)
 *  -- the raw evidence body, `dict[str, Any]` on the wire (no dedicated
 *  Pydantic model), so the fixture returns the same plain-object shape
 *  rather than a typed `CompliancePackRead`. */
export async function makeFixtureDownloadCompliancePack(
  packId: string,
): Promise<Record<string, unknown>> {
  await wait(100);
  const pack = COMPLIANCE_PACKS.find((p) => p.id === packId);
  if (!pack) throw new Error(`fixture: no such compliance pack ${packId}`);
  return {
    id: pack.id,
    name: pack.name,
    framework: pack.framework,
    period_start: pack.period_start,
    period_end: pack.period_end,
    sections: pack.sections,
    checksum: pack.checksum,
    generated_by: pack.generated_by,
    generated_at: pack.generated_at,
    status: pack.status,
  };
}


/* ---------------------------------------------------------------------------
   Workspace membership, source-binding decisions, BI/Tableau lineage
   connections -- fixtures backing this file's `addWorkspaceMember`/
   `fetchWorkspaceMembers`/`decideSourceBinding`/`fetchProjectBiConnections`/
   `createBiConnection`/`importBiArtifact` (api.ts). Reuses
   `FIXTURE_WORKSPACES`/`FIXTURE_SOURCE_BINDINGS`/`FIXTURE_PROJECTS`/
   `FIXTURE_DATASOURCES` already defined above (workspace `ws_governed_analytics`,
   its pending-approval counterpart added here, project `proj_core`, datasource
   `ds_snowflake_prod`).
--------------------------------------------------------------------------- */

import type {
  BiArtifactImportRead,
  BiArtifactImportRequest,
  BiConnectionCreate,
  BiConnectionRead,
  SourceBindingDecision,
  WorkspaceMembershipCreate,
  WorkspaceMembershipRead,
} from "./types";

const FIXTURE_WORKSPACE_MEMBERSHIPS: WorkspaceMembershipRead[] = [
  {
    id: "member_governed_admin",
    organization_id: "00000000-0000-0000-0000-000000000001",
    workspace_id: "ws_governed_analytics",
    principal_id: "local-ui-admin",
    principal_kind: "HUMAN",
    role: "workspace_owner",
    granted_by: "local-ui-admin",
    expires_at: null,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "member_governed_analyst",
    organization_id: "00000000-0000-0000-0000-000000000001",
    workspace_id: "ws_governed_analytics",
    principal_id: "priya.iyer",
    principal_kind: "HUMAN",
    role: "analyst",
    granted_by: "local-ui-admin",
    expires_at: null,
    status: "ACTIVE",
    created_at: "2026-02-10T00:00:00Z",
    updated_at: "2026-02-10T00:00:00Z",
  },
];

/** A second, still-pending binding alongside `FIXTURE_SOURCE_BINDINGS`' one
 *  already-active row, so the pending-approval list this screen renders has
 *  something to show and decide on out of the box. */
const FIXTURE_PENDING_SOURCE_BINDING: SourceBindingRead = {
  id: "binding_governed_snowflake_pending",
  organization_id: "00000000-0000-0000-0000-000000000001",
  workspace_id: "ws_governed_analytics",
  datasource_id: "ds_snowflake_prod",
  schema_scope: [],
  permitted_classifications: [],
  masking_profile: "DEFAULT",
  purpose: "Quarterly reconciliation analysis",
  max_query_cost: null,
  status: "PENDING_APPROVAL",
  requested_by: "priya.iyer",
  approved_by: null,
  approved_at: null,
  expires_at: null,
  created_at: "2026-08-20T00:00:00Z",
  updated_at: "2026-08-20T00:00:00Z",
};
FIXTURE_SOURCE_BINDINGS.push(FIXTURE_PENDING_SOURCE_BINDING);

/** `POST /v1/workspaces/{workspace_id}/members` (`workspace_api.py:160`). */
export async function makeFixtureAddWorkspaceMember(
  workspaceId: string,
  body: WorkspaceMembershipCreate,
): Promise<WorkspaceMembershipRead> {
  await wait(60);
  const workspace = FIXTURE_WORKSPACES.find((item) => item.id === workspaceId);
  if (
    FIXTURE_WORKSPACE_MEMBERSHIPS.some(
      (item) => item.workspace_id === workspaceId && item.principal_id === body.principal_id,
    )
  ) {
    throw new ApiError(409, "principal already has a membership");
  }
  const now = new Date().toISOString();
  const membership: WorkspaceMembershipRead = {
    id: `member_${workspaceId}_${body.principal_id}`,
    organization_id: workspace?.organization_id ?? "00000000-0000-0000-0000-000000000001",
    workspace_id: workspaceId,
    principal_id: body.principal_id,
    principal_kind: body.principal_kind ?? "HUMAN",
    role: body.role,
    granted_by: "local-ui-admin",
    expires_at: body.expires_at ?? null,
    status: "ACTIVE",
    created_at: now,
    updated_at: now,
  };
  FIXTURE_WORKSPACE_MEMBERSHIPS.push(membership);
  return membership;
}

/** `GET /v1/workspaces/{workspace_id}/members` (`workspace_api.py:207`). */
export async function makeFixtureWorkspaceMembers(
  workspaceId: string,
): Promise<PageOf<WorkspaceMembershipRead>> {
  await wait(40);
  const items = FIXTURE_WORKSPACE_MEMBERSHIPS.filter((item) => item.workspace_id === workspaceId);
  return { items, limit: items.length || 1, offset: 0, total: items.length };
}

/** `POST /v1/source-bindings/{binding_id}/decision` (`workspace_api.py:293`).
 *  Mirrors the real handler's own guards: 404 if the binding does not exist,
 *  409 if it is no longer pending. Fixture mode has no notion of "the
 *  current principal" (see `identityHeaders`'s `USE_FIXTURES` branch), so
 *  unlike the live route this cannot also reject same-principal
 *  maker-checker attempts -- that guard is exercised against the live API. */
export async function makeFixtureDecideSourceBinding(
  bindingId: string,
  body: SourceBindingDecision,
): Promise<SourceBindingRead> {
  await wait(60);
  const binding = FIXTURE_SOURCE_BINDINGS.find((item) => item.id === bindingId);
  if (!binding) throw new ApiError(404, "source binding not found");
  if (binding.status !== "PENDING_APPROVAL") {
    throw new ApiError(409, "binding is not pending");
  }
  const now = new Date().toISOString();
  binding.status = body.decision === "APPROVE" ? "ACTIVE" : "REJECTED";
  binding.approved_by = "local-ui-admin";
  binding.approved_at = now;
  binding.updated_at = now;
  if (body.decision === "APPROVE") {
    const validForDays = body.valid_for_days ?? 365;
    const expires = new Date();
    expires.setDate(expires.getDate() + validForDays);
    binding.expires_at = expires.toISOString();
  }
  return binding;
}

/* ---------------------------------------------------------------------------
   PG-4: delegations. No screen has existed for this before -- see
   `DelegationsScreen.tsx`'s own module comment for the backend contract
   (`delegation_api.py`). Fixture roster covers every UI state the screen
   must render: genuinely current ACTIVE, ACTIVE but past `expires_at` (the
   client-computed "expired" state -- nothing flips the status column at
   expiry by design), and REVOKED, across different delegator/delegate pairs
   and role sets. */
import type { DelegationCreate, DelegationRead } from "./types";

function delegationIso(daysOffset: number): string {
  return new Date(Date.now() + daysOffset * 86_400_000).toISOString();
}

const FIXTURE_DELEGATIONS: DelegationRead[] = [
  {
    id: "delegation-aaaa1111",
    organization_id: "00000000-0000-0000-0000-000000000001",
    delegator_principal_id: "priya.steward",
    delegate_principal_id: "morgan.covering",
    delegated_roles: ["DataSteward", "Reviewer"],
    reason: "Parental leave coverage for the data steward review queue.",
    starts_at: delegationIso(-5),
    expires_at: delegationIso(9),
    status: "ACTIVE",
    created_by: "priya.steward",
    revoked_by: null,
    revoked_at: null,
    created_at: delegationIso(-5),
    updated_at: delegationIso(-5),
  },
  {
    id: "delegation-bbbb2222",
    organization_id: "00000000-0000-0000-0000-000000000001",
    delegator_principal_id: "sam.reviewer",
    delegate_principal_id: "jordan.backup",
    delegated_roles: ["Reviewer"],
    reason: "Conference travel; unavailable for governance review decisions this week.",
    starts_at: delegationIso(-10),
    expires_at: delegationIso(-3),
    status: "ACTIVE",
    created_by: "sam.reviewer",
    revoked_by: null,
    revoked_at: null,
    created_at: delegationIso(-10),
    updated_at: delegationIso(-10),
  },
  {
    id: "delegation-cccc3333",
    organization_id: "00000000-0000-0000-0000-000000000001",
    delegator_principal_id: "alex.admin",
    delegate_principal_id: "riley.deputy",
    delegated_roles: ["PlatformAdmin", "MetadataAdmin", "DataAdmin"],
    reason: "Sabbatical; full administrative coverage for the quarter.",
    starts_at: delegationIso(-30),
    expires_at: delegationIso(60),
    status: "REVOKED",
    created_by: "alex.admin",
    revoked_by: "alex.admin",
    revoked_at: delegationIso(-2),
    created_at: delegationIso(-30),
    updated_at: delegationIso(-2),
  },
  {
    id: "delegation-dddd4444",
    organization_id: "00000000-0000-0000-0000-000000000001",
    delegator_principal_id: "priya.steward",
    delegate_principal_id: "casey.semantic",
    delegated_roles: ["SemanticAdmin"],
    reason: "Short medical leave; delegating semantic layer sign-off authority.",
    starts_at: delegationIso(-1),
    expires_at: delegationIso(13),
    status: "ACTIVE",
    created_by: "priya.steward",
    revoked_by: null,
    revoked_at: null,
    created_at: delegationIso(-1),
    updated_at: delegationIso(-1),
  },
  {
    id: "delegation-eeee5555",
    organization_id: "00000000-0000-0000-0000-000000000001",
    delegator_principal_id: "morgan.covering",
    delegate_principal_id: "sam.reviewer",
    delegated_roles: ["DataSteward"],
    reason: "Cross-covering while the primary steward onboards a new datasource.",
    starts_at: delegationIso(-60),
    expires_at: delegationIso(-40),
    status: "REVOKED",
    created_by: "morgan.covering",
    revoked_by: "alex.admin",
    revoked_at: delegationIso(-45),
    created_at: delegationIso(-60),
    updated_at: delegationIso(-45),
  },
];

/** `GET /v1/organizations/{organization_id}/delegations` (PG-4,
 *  `delegation_api.py::list_delegations`) fixture. */
export async function makeFixtureDelegations(
  organizationId: string,
  query: {
    delegatePrincipalId?: string | null;
    delegatorPrincipalId?: string | null;
    status?: string | null;
    limit?: number;
    offset?: number;
  } = {},
): Promise<PageOf<DelegationRead>> {
  await wait(50);
  let items = FIXTURE_DELEGATIONS.filter((item) => item.organization_id === organizationId);
  if (query.delegatePrincipalId) {
    items = items.filter((item) => item.delegate_principal_id === query.delegatePrincipalId);
  }
  if (query.delegatorPrincipalId) {
    items = items.filter((item) => item.delegator_principal_id === query.delegatorPrincipalId);
  }
  if (query.status) {
    const wanted = query.status.toUpperCase();
    items = items.filter((item) => item.status === wanted);
  }
  items = [...items].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `POST /v1/organizations/{organization_id}/delegations` (PG-4,
 *  `delegation_api.py::grant_delegation`) fixture. Mirrors the real handler's
 *  own guards -- self-delegation forbidden, `expires_at` after `starts_at`,
 *  window capped at 180 days -- but fixture mode has no notion of "the roles
 *  the current principal holds", so unlike the live route this cannot also
 *  reject delegating a role the caller does not itself hold; that guard is
 *  exercised against the live API. */
export async function makeFixtureGrantDelegation(
  organizationId: string,
  body: DelegationCreate,
): Promise<DelegationRead> {
  await wait(70);
  const callerId = "local-ui-admin";
  if (body.delegate_principal_id === callerId) {
    throw new ApiError(422, "cannot delegate authority to yourself");
  }
  const now = new Date();
  const startsAt = body.starts_at ? new Date(body.starts_at) : now;
  const expiresAt = new Date(body.expires_at);
  if (expiresAt.getTime() <= startsAt.getTime()) {
    throw new ApiError(422, "expires_at must be after starts_at");
  }
  const maxWindowMs = 180 * 86_400_000;
  if (expiresAt.getTime() - startsAt.getTime() > maxWindowMs) {
    throw new ApiError(422, "delegation window exceeds the 180-day cap");
  }
  const nowIso = now.toISOString();
  const delegation: DelegationRead = {
    id: `delegation-${Math.random().toString(36).slice(2, 10)}`,
    organization_id: organizationId,
    delegator_principal_id: callerId,
    delegate_principal_id: body.delegate_principal_id,
    delegated_roles: [...new Set(body.delegated_roles)].sort(),
    reason: body.reason,
    starts_at: startsAt.toISOString(),
    expires_at: expiresAt.toISOString(),
    status: "ACTIVE",
    created_by: callerId,
    revoked_by: null,
    revoked_at: null,
    created_at: nowIso,
    updated_at: nowIso,
  };
  FIXTURE_DELEGATIONS.unshift(delegation);
  return delegation;
}

/** `POST /v1/delegations/{delegation_id}/revoke` (PG-4,
 *  `delegation_api.py::revoke_delegation`) fixture. */
export async function makeFixtureRevokeDelegation(delegationId: string): Promise<DelegationRead> {
  await wait(50);
  const delegation = FIXTURE_DELEGATIONS.find((item) => item.id === delegationId);
  if (!delegation) throw new ApiError(404, "delegation not found");
  if (delegation.status !== "ACTIVE") {
    throw new ApiError(409, "delegation is not active");
  }
  const now = new Date().toISOString();
  delegation.status = "REVOKED";
  delegation.revoked_by = "local-ui-admin";
  delegation.revoked_at = now;
  delegation.updated_at = now;
  return delegation;
}

const FIXTURE_BI_CONNECTIONS: BiConnectionRead[] = [
  {
    id: "bi_conn_finance_tableau",
    organization_id: "00000000-0000-0000-0000-000000000001",
    project_id: "proj_core",
    datasource_id: "ds_snowflake_prod",
    bi_tool: "TABLEAU",
    connection_key: "finance-tableau-prod",
    display_name: "Finance Tableau (Production)",
    site_or_workspace: "finance",
    status: "ACTIVE",
    created_by: "local-ui-admin",
    created_at: "2026-03-01T00:00:00Z",
    updated_at: "2026-03-01T00:00:00Z",
  },
];

const FIXTURE_BI_ARTIFACT_IMPORTS: Record<string, BiArtifactImportRead[]> = {};

/** `GET /v1/projects/{project_id}/bi-connections` (`bi_api.py:226`). */
export async function makeFixtureProjectBiConnections(
  projectId: string,
  opts: { limit?: number; offset?: number },
): Promise<PageOf<BiConnectionRead>> {
  await wait(50);
  const items = FIXTURE_BI_CONNECTIONS.filter((item) => item.project_id === projectId);
  const offset = opts.offset ?? 0;
  const limit = opts.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `POST /v1/projects/{project_id}/bi-connections` (`bi_api.py:171`). Mirrors
 *  the live route's own unique-key guard (`IntegrityError` -> 409). */
export async function makeFixtureCreateBiConnection(
  projectId: string,
  body: BiConnectionCreate,
): Promise<BiConnectionRead> {
  await wait(70);
  const project = FIXTURE_PROJECTS.find((item) => item.id === projectId);
  if (FIXTURE_BI_CONNECTIONS.some((item) => item.connection_key === body.connection_key)) {
    throw new ApiError(409, "bi connection key already exists");
  }
  const now = new Date().toISOString();
  const connection: BiConnectionRead = {
    id: `bi_conn_${body.connection_key}`,
    organization_id: project?.organization_id ?? "00000000-0000-0000-0000-000000000001",
    project_id: projectId,
    datasource_id: body.datasource_id,
    bi_tool: body.bi_tool,
    connection_key: body.connection_key,
    display_name: body.display_name,
    site_or_workspace: body.site_or_workspace ?? null,
    status: "ACTIVE",
    created_by: "local-ui-admin",
    created_at: now,
    updated_at: now,
  };
  FIXTURE_BI_CONNECTIONS.push(connection);
  return connection;
}

/** `POST /v1/bi-connections/{connection_id}/artifact-imports` (`bi_api.py:258`).
 *  `body.artifact` is the raw pasted JSON (already parsed client-side); this
 *  derives plausible counts from whatever shape it happens to carry, the
 *  same tolerant spirit as the legacy portal's own unstructured import. */
export async function makeFixtureImportBiArtifact(
  connectionId: string,
  body: BiArtifactImportRequest,
): Promise<BiArtifactImportRead> {
  await wait(90);
  const connection = FIXTURE_BI_CONNECTIONS.find((item) => item.id === connectionId);
  if (!connection) throw new ApiError(404, "bi connection not found");
  const artifact = (body.artifact ?? {}) as Record<string, unknown>;
  const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
  const reports = asArray(artifact.reports ?? artifact.workbooks);
  const metrics = asArray(artifact.metrics ?? artifact.fields);
  const now = new Date().toISOString();
  const matched = Math.round(metrics.length * 0.7);
  const importRead: BiArtifactImportRead = {
    id: `bi_import_${connectionId}_${FIXTURE_BI_ARTIFACT_IMPORTS[connectionId]?.length ?? 0}`,
    organization_id: connection.organization_id,
    connection_id: connectionId,
    artifact_fingerprint: `sha256:fixture-${Math.abs(JSON.stringify(artifact).length).toString(16)}`,
    bi_tool: body.bi_tool,
    generated_at: now,
    status: "COMPLETED",
    report_count: reports.length,
    metric_count: metrics.length,
    report_metric_edge_count: reports.length * metrics.length,
    metric_column_edge_count: matched,
    matched_column_count: matched,
    unmatched_column_count: Math.max(metrics.length - matched, 0),
    imported_by: "local-ui-admin",
    created_at: now,
    updated_at: now,
  };
  FIXTURE_BI_ARTIFACT_IMPORTS[connectionId] = [
    ...(FIXTURE_BI_ARTIFACT_IMPORTS[connectionId] ?? []),
    importRead,
  ];
  return importRead;
}


/* ---------------------------------------------------------------------------
   Catalog bulk actions + unowned-asset stewardship backlog fixtures. Mirrors
   `bulk_tag_tables`/`bulk_classify_tables`/`bulk_own_tables`/
   `bulk_certify_tables` (api.py) and `list_unowned_backlog`/
   `route_unowned_backlog` (stewardship_api.py) closely enough to exercise the
   real UI end to end, without reimplementing either endpoint's actual
   matching engine:

   - A `filter` (datasource + match field/pattern) resolves to a small,
     deterministic set of synthetic subject ids (`fixtureBulkMatchedSubjectIds`)
     rather than scanning the million-row `rowAt` catalog generator -- the
     real filter matches by SQL LIKE against live metadata this client has no
     equivalent of; determinism (same filter -> same subject count) is what
     this fixture actually needs to demo the results panel, not a literal
     reproduction of the server's pattern matching.
   - Each matched subject SUCCEEDS or FAILS independently (~1-in-7 fails, with
     a plausible reason), the same per-item semantics
     `CatalogBulkActionItemRead` carries for real.
   - The unowned backlog is a small, mutable seeded list (one entry per
     detection stage) so repeated "Route backlog" calls actually advance
     UNOWNED -> ROUTED -> ESCALATED -> ESCALATED_TIER_2 across calls, instead
     of returning the same static counts forever.
--------------------------------------------------------------------------- */

import type {
  CatalogBulkActionItemRead,
  CatalogBulkActionRunRead,
  CatalogBulkCertifyRequest,
  CatalogBulkClassifyRequest,
  CatalogBulkOwnRequest,
  CatalogBulkSelectionFilter,
  CatalogBulkTagRequest,
  UnownedAssetBacklogRouteRequest,
  UnownedAssetBacklogRouteResult,
  UnownedAssetEscalationRead,
} from "./types";
import type { DocumentationWorklistEntryRead } from "./ui-types";
import type { DocumentationWorklistQuery, UnownedAssetBacklogQuery } from "./api";

function fixtureBulkMatchedSubjectIds(
  filter: CatalogBulkSelectionFilter | null | undefined,
  explicitIds: string[] | null | undefined,
): string[] {
  if (explicitIds && explicitIds.length > 0) return explicitIds;
  if (!filter) return [];
  const seed = parseInt(
    fakeHex(`${filter.datasource_id}:${filter.match_field ?? "TABLE_NAME"}:${filter.match_pattern}`, 8),
    16,
  );
  const count = 2 + (seed % 5); // 2..6 matched subjects, stable for a given filter
  return Array.from(
    { length: count },
    (_, i) => `t_${fakeHex(`${filter.datasource_id}:${filter.match_pattern}:${i}`, 10)}`,
  );
}

let fixtureBulkActionRunSequence = 0;

function fixtureBulkActionRun(
  organizationId: string,
  action: string,
  selectionMode: string,
  parameters: Record<string, unknown>,
  subjectIds: string[],
): CatalogBulkActionRunRead {
  fixtureBulkActionRunSequence += 1;
  const runSeq = fixtureBulkActionRunSequence;
  const results: CatalogBulkActionItemRead[] = subjectIds.map((subjectId, i) => {
    const roll = h(runSeq * 97 + i, 41) % 7;
    return roll === 0
      ? {
          subject_id: subjectId,
          status: "FAILED",
          reason: "Concurrent modification detected on this subject; retry the action.",
        }
      : { subject_id: subjectId, status: "SUCCEEDED", reason: null };
  });
  const succeeded = results.filter((r) => r.status === "SUCCEEDED").length;
  return {
    id: `bulkrun_${runSeq}`,
    organization_id: organizationId,
    action,
    selection_mode: selectionMode,
    parameters,
    requested_count: subjectIds.length,
    succeeded_count: succeeded,
    failed_count: results.length - succeeded,
    results,
    requested_by: "local-ui-admin",
    created_at: new Date().toISOString(),
  };
}

/** `POST /v1/organizations/{id}/tables/bulk-tag`. */
export async function makeFixtureBulkTagCatalogTables(
  organizationId: string,
  body: CatalogBulkTagRequest,
): Promise<CatalogBulkActionRunRead> {
  await wait(180);
  const subjectIds = fixtureBulkMatchedSubjectIds(body.filter, body.table_ids);
  return fixtureBulkActionRun(
    organizationId,
    "BULK_TAG",
    body.filter ? "FILTER" : "TABLE_IDS",
    { tag_key: body.tag_key, tag_value: body.tag_value ?? null, filter: body.filter ?? null, table_ids: body.table_ids ?? null },
    subjectIds,
  );
}

/** `POST /v1/organizations/{id}/tables/bulk-classify`. Column-level: when no
 *  explicit `column_ids` are given, each matched table stands in for a
 *  handful of its columns (`_col0`.._col2`) so the results panel still shows
 *  column-shaped subject ids rather than table ids for this action. */
export async function makeFixtureBulkClassifyCatalogColumns(
  organizationId: string,
  body: CatalogBulkClassifyRequest,
): Promise<CatalogBulkActionRunRead> {
  await wait(180);
  const subjectIds =
    body.column_ids && body.column_ids.length > 0
      ? body.column_ids
      : fixtureBulkMatchedSubjectIds(body.filter, body.table_ids).flatMap((tableId) =>
          [0, 1, 2].map((n) => `${tableId}_col${n}`),
        );
  const selectionMode = body.column_ids ? "COLUMN_IDS" : body.filter ? "FILTER" : "TABLE_IDS";
  return fixtureBulkActionRun(
    organizationId,
    "BULK_CLASSIFY",
    selectionMode,
    {
      classification: body.classification,
      column_name_pattern: body.column_name_pattern ?? "*",
      filter: body.filter ?? null,
      table_ids: body.table_ids ?? null,
      column_ids: body.column_ids ?? null,
    },
    subjectIds,
  );
}

/** `POST /v1/organizations/{id}/tables/bulk-own`. */
export async function makeFixtureBulkOwnCatalogTables(
  organizationId: string,
  body: CatalogBulkOwnRequest,
): Promise<CatalogBulkActionRunRead> {
  await wait(180);
  const subjectIds = fixtureBulkMatchedSubjectIds(body.filter, body.table_ids);
  return fixtureBulkActionRun(
    organizationId,
    "BULK_OWN",
    body.filter ? "FILTER" : "TABLE_IDS",
    { owner_type: body.owner_type, owner_principal: body.owner_principal, filter: body.filter ?? null, table_ids: body.table_ids ?? null },
    subjectIds,
  );
}

/** `POST /v1/organizations/{id}/tables/bulk-certify`. */
export async function makeFixtureBulkCertifyCatalogTables(
  organizationId: string,
  body: CatalogBulkCertifyRequest,
): Promise<CatalogBulkActionRunRead> {
  await wait(180);
  const subjectIds = fixtureBulkMatchedSubjectIds(body.filter, body.table_ids);
  return fixtureBulkActionRun(
    organizationId,
    "BULK_CERTIFY",
    body.filter ? "FILTER" : "TABLE_IDS",
    { rationale: body.rationale, expires_at: body.expires_at, filter: body.filter ?? null, table_ids: body.table_ids ?? null },
    subjectIds,
  );
}

/** Seeded so the backlog panel opens with one escalation at every stage,
 *  matching this app's one fixture datasource (`ds_snowflake_prod`,
 *  `FIXTURE_DATASOURCES[0]`). Mutated in place by
 *  `makeFixtureRouteUnownedAssetBacklog` below -- unlike most list fixtures
 *  in this file, routing is a real state transition, not a fresh random
 *  sample, so repeated calls must see the previous call's effect. */
const FIXTURE_UNOWNED_BACKLOG: UnownedAssetEscalationRead[] = [
  {
    id: "unowned_1", organization_id: ORG_ID, table_id: "t_9f2a1c4d0e",
    first_detected_unowned_at: "2026-08-01T09:00:00Z", status: "UNOWNED",
    candidate_owner: null, notification_rule_id: null, channel: null, recipients: [],
    dedup_key: "ds_snowflake_prod:t_9f2a1c4d0e", routed_at: null, escalated_at: null,
    escalated_tier2_at: null, resolved_at: null,
    created_at: "2026-08-01T09:00:00Z", updated_at: "2026-08-01T09:00:00Z",
  },
  {
    id: "unowned_2", organization_id: ORG_ID, table_id: "t_3b7e5a91cc",
    first_detected_unowned_at: "2026-08-02T11:00:00Z", status: "UNOWNED",
    candidate_owner: "Finance Data", notification_rule_id: null, channel: null, recipients: [],
    dedup_key: "ds_snowflake_prod:t_3b7e5a91cc", routed_at: null, escalated_at: null,
    escalated_tier2_at: null, resolved_at: null,
    created_at: "2026-08-02T11:00:00Z", updated_at: "2026-08-02T11:00:00Z",
  },
  {
    id: "unowned_3", organization_id: ORG_ID, table_id: "t_6c1d8f22ab",
    first_detected_unowned_at: "2026-07-20T08:00:00Z", status: "ROUTED",
    candidate_owner: null, notification_rule_id: "nr_unowned_escalation", channel: "SLACK",
    recipients: ["data-governance-team@tenant.example"],
    dedup_key: "ds_snowflake_prod:t_6c1d8f22ab", routed_at: "2026-07-25T08:00:00Z",
    escalated_at: null, escalated_tier2_at: null, resolved_at: null,
    created_at: "2026-07-20T08:00:00Z", updated_at: "2026-07-25T08:00:00Z",
  },
  {
    id: "unowned_4", organization_id: ORG_ID, table_id: "t_0a4f6e17d9",
    first_detected_unowned_at: "2026-06-15T08:00:00Z", status: "ESCALATED",
    candidate_owner: null, notification_rule_id: "nr_unowned_escalation", channel: "EMAIL",
    recipients: ["risk-data-stewards@tenant.example"],
    dedup_key: "ds_snowflake_prod:t_0a4f6e17d9", routed_at: "2026-06-20T08:00:00Z",
    escalated_at: "2026-07-05T08:00:00Z", escalated_tier2_at: null, resolved_at: null,
    created_at: "2026-06-15T08:00:00Z", updated_at: "2026-07-05T08:00:00Z",
  },
  {
    id: "unowned_5", organization_id: ORG_ID, table_id: "t_5e8c2b40f1",
    first_detected_unowned_at: "2026-08-10T08:00:00Z", status: "RESOLVED",
    candidate_owner: "Retail Data Office", notification_rule_id: "nr_unowned_escalation", channel: "SLACK",
    recipients: ["data-governance-team@tenant.example"],
    dedup_key: "ds_snowflake_prod:t_5e8c2b40f1", routed_at: "2026-08-11T08:00:00Z",
    escalated_at: null, escalated_tier2_at: null, resolved_at: "2026-08-14T08:00:00Z",
    created_at: "2026-08-10T08:00:00Z", updated_at: "2026-08-14T08:00:00Z",
  },
];

/** `GET /v1/organizations/{id}/stewardship/unowned-backlog`. */
export async function makeFixtureUnownedAssetBacklog(
  organizationId: string,
  query: UnownedAssetBacklogQuery,
): Promise<PageOf<UnownedAssetEscalationRead>> {
  await wait(90);
  let items = FIXTURE_UNOWNED_BACKLOG.filter((item) => item.organization_id === organizationId);
  if (query.status) items = items.filter((item) => item.status === query.status);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `POST /v1/organizations/{id}/stewardship/unowned-backlog/route`. Advances
 *  every in-scope escalation one step (UNOWNED with a candidate owner already
 *  known resolves outright; otherwise UNOWNED -> ROUTED -> ESCALATED ->
 *  ESCALATED_TIER_2), mutating the seeded list so a second call continues
 *  from where the first left off. */
export async function makeFixtureRouteUnownedAssetBacklog(
  organizationId: string,
  body: UnownedAssetBacklogRouteRequest,
): Promise<UnownedAssetBacklogRouteResult> {
  await wait(220);
  const now = new Date().toISOString();
  const routed: UnownedAssetEscalationRead[] = [];
  const escalated: UnownedAssetEscalationRead[] = [];
  const escalatedTier2: UnownedAssetEscalationRead[] = [];
  let resolvedCount = 0;

  // This fixture seeds exactly one datasource (`ds_snowflake_prod`); an
  // explicit, non-matching `datasource_id` scopes the run to nothing --
  // matching the real endpoint's own "no rows in scope" outcome rather than
  // silently ignoring the filter. `domain_id`/`line_of_business_id` scope the
  // real *query* server-side and aren't fields this fixture's seeded
  // escalations carry, so they're accepted but left unscoped here rather
  // than faked against data that doesn't exist.
  const inScope = FIXTURE_UNOWNED_BACKLOG.filter(
    (item) =>
      item.organization_id === organizationId &&
      (!body.datasource_id || body.datasource_id === FIXTURE_DATASOURCES[0]?.id),
  );

  for (const item of inScope) {
    if (item.status === "UNOWNED") {
      if (item.candidate_owner) {
        item.status = "RESOLVED";
        item.resolved_at = now;
        resolvedCount += 1;
      } else {
        item.status = "ROUTED";
        item.routed_at = now;
        item.notification_rule_id = "nr_unowned_escalation";
        item.channel = "SLACK";
        item.recipients = ["data-governance-team@tenant.example"];
        routed.push(item);
      }
    } else if (item.status === "ROUTED") {
      item.status = "ESCALATED";
      item.escalated_at = now;
      escalated.push(item);
    } else if (item.status === "ESCALATED") {
      item.status = "ESCALATED_TIER_2";
      item.escalated_tier2_at = now;
      escalatedTier2.push(item);
    }
    item.updated_at = now;
  }

  return {
    organization_id: organizationId,
    routed,
    escalated,
    escalated_tier2: escalatedTier2,
    resolved_count: resolvedCount,
  };
}

/** `GET .../stewardship/documentation-worklist` (AT-5/SW-1) -- ranked by
 *  `score = usage x impact x deficit`, descending, matching
 *  `stewardship_worklist.compute_worklist`'s own deterministic tie-break
 *  (score desc, then id) so the fixture ordering matches what a real
 *  organization would actually see. */
export async function makeFixtureDocumentationWorklist(
  organizationId: string,
  query: DocumentationWorklistQuery,
): Promise<PageOf<DocumentationWorklistEntryRead>> {
  await wait(110);
  void organizationId;
  const all: DocumentationWorklistEntryRead[] = [
    {
      table_id: "wl_1", table_name: "customer_master", schema_name: "raw_sales", datasource_name: "snowflake_prod",
      rank: 1, query_execution_count: 812, consumption_read_count: 340, query_volume: 1152,
      last_queried_at: new Date(Date.now() - 2 * 3_600_000).toISOString(), last_consumed_at: new Date(Date.now() - 5 * 3_600_000).toISOString(),
      description_is_proposed: false, score: 0.612, usage: 1.0, impact: 0.85, deficit: 0.72,
      downstream_count: 17, missing: ["description", "certification", "quality_policy"],
    },
    {
      table_id: "wl_2", table_name: "transaction_fact", schema_name: "mart", datasource_name: "snowflake_prod",
      rank: 2, query_execution_count: 640, consumption_read_count: 210, query_volume: 850,
      last_queried_at: new Date(Date.now() - 1 * 3_600_000).toISOString(), last_consumed_at: null,
      description_is_proposed: true, score: 0.401, usage: 0.74, impact: 0.6, deficit: 0.6,
      downstream_count: 9, missing: ["owner", "certification", "quality_policy"],
    },
    {
      table_id: "wl_3", table_name: "account_dim", schema_name: "raw_sales", datasource_name: "snowflake_prod",
      rank: 3, query_execution_count: 305, consumption_read_count: 120, query_volume: 425,
      last_queried_at: new Date(Date.now() - 8 * 3_600_000).toISOString(), last_consumed_at: new Date(Date.now() - 20 * 3_600_000).toISOString(),
      description_is_proposed: false, score: 0.213, usage: 0.37, impact: 0.85, deficit: 0.4,
      downstream_count: 14, missing: ["owner", "quality_policy"],
    },
    {
      table_id: "wl_4", table_name: "exposure_snapshot", schema_name: "risk", datasource_name: "oracle_core",
      rank: 4, query_execution_count: 190, consumption_read_count: 40, query_volume: 230,
      last_queried_at: new Date(Date.now() - 30 * 3_600_000).toISOString(), last_consumed_at: null,
      description_is_proposed: false, score: 0.132, usage: 0.2, impact: 0.55, deficit: 0.8,
      downstream_count: 3, missing: ["description", "owner", "certification", "glossary_term"],
    },
    {
      table_id: "wl_5", table_name: "orders_stg", schema_name: "raw_retail", datasource_name: "postgres_events",
      rank: 5, query_execution_count: 42, consumption_read_count: 8, query_volume: 50,
      last_queried_at: new Date(Date.now() - 72 * 3_600_000).toISOString(), last_consumed_at: null,
      description_is_proposed: false, score: 0.031, usage: 0.04, impact: 0.25, deficit: 0.8,
      downstream_count: 0, missing: ["description", "owner", "certification", "glossary_term"],
    },
  ];
  const ranking = query.ranking ?? "priority";
  const sorted = [...all].sort((a, b) =>
    ranking === "query_volume" ? b.query_volume - a.query_volume : b.score - a.score,
  );
  const items = query.includeZeroVolume ? sorted : sorted.filter((item) => item.query_volume > 0);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return {
    items: items.slice(offset, offset + limit).map((item, index) => ({ ...item, rank: offset + index + 1 })),
    limit,
    offset,
    total: items.length,
  };
}

/* ---------------------------------------------------------------------------
   Reliability -- SLOs, notification rules, archive/WORM posture, and runtime
   data-contract evaluation. Mirrors `lib/api.ts`'s "Reliability" block
   field-for-field against the real `observability_api.py` /
   `notification_api.py` / `runtime_contracts_api.py` response shapes. */

import type {
  ArchiveStatusRead,
  EvaluationResponse,
  NotificationRuleCreate,
  NotificationRuleRead,
  SlaStatusResponse,
  SloBudgetRead,
  SloDefinitionCreate,
  SloDefinitionRead,
} from "./types";
import type { ViolationRead } from "./ui-types";
import type {
  ContractViolationsQuery,
  NotificationRuleQuery,
  SloDefinitionQuery,
} from "./api";

const RELIABILITY_ORG = "00000000-0000-0000-0000-000000000001";

const FIXTURE_SLOS: SloDefinitionRead[] = [
  {
    id: "slo_agent_answer_latency", organization_id: RELIABILITY_ORG,
    slo_key: "agent-answer-latency-p95", name: "Agent answer latency (p95)",
    target: 99, window_days: 30, threshold: 95, status: "ACTIVE",
    created_by: "local-ui-admin", created_at: "2026-07-01T00:00:00Z", updated_at: "2026-08-15T00:00:00Z",
  },
  {
    id: "slo_ingestion_freshness", organization_id: RELIABILITY_ORG,
    slo_key: "ingestion-freshness", name: "Metadata ingestion freshness",
    target: 99.5, window_days: 7, threshold: 97, status: "ACTIVE",
    created_by: "local-ui-admin", created_at: "2026-07-05T00:00:00Z", updated_at: "2026-08-20T00:00:00Z",
  },
  {
    id: "slo_governed_tool_success", organization_id: RELIABILITY_ORG,
    slo_key: "governed-tool-success-rate", name: "Governed tool execution success rate",
    target: 99.9, window_days: 30, threshold: 99, status: "ACTIVE",
    created_by: "local-ui-admin", created_at: "2026-06-15T00:00:00Z", updated_at: "2026-08-28T00:00:00Z",
  },
];

/** Keyed by `SloDefinitionRead.id`. Mirrors `get_slo_budget`'s own
 *  HEALTHY/AT_RISK/BREACHED/NO_DATA derivation (current vs. target/threshold)
 *  -- one of each, so the screen's status pill/tone logic gets exercised. */
const FIXTURE_SLO_BUDGETS: Record<string, SloBudgetRead> = {
  slo_agent_answer_latency: {
    slo_id: "slo_agent_answer_latency", slo_key: "agent-answer-latency-p95",
    name: "Agent answer latency (p95)", target: 99, current_value: 99.4,
    budget_remaining: 0.62, window_days: 30, status: "HEALTHY",
  },
  slo_ingestion_freshness: {
    slo_id: "slo_ingestion_freshness", slo_key: "ingestion-freshness",
    name: "Metadata ingestion freshness", target: 99.5, current_value: 97.8,
    budget_remaining: 0.18, window_days: 7, status: "AT_RISK",
  },
  slo_governed_tool_success: {
    slo_id: "slo_governed_tool_success", slo_key: "governed-tool-success-rate",
    name: "Governed tool execution success rate", target: 99.9, current_value: 98.1,
    budget_remaining: 0, window_days: 30, status: "BREACHED",
  },
};

/** `GET /v1/observability/slo`. */
export async function makeFixtureSloDefinitions(
  organizationId: string,
  query: SloDefinitionQuery,
): Promise<PageOf<SloDefinitionRead>> {
  await wait(70);
  void organizationId;
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return {
    items: FIXTURE_SLOS.slice(offset, offset + limit),
    limit, offset, total: FIXTURE_SLOS.length,
  };
}

/** `POST /v1/observability/slo` -- mirrors `create_slo_definition`'s own
 *  409 on a duplicate `slo_key` within the organization. A freshly created
 *  SLO has no measurement yet, so its budget resolves NO_DATA (no entry is
 *  seeded into `FIXTURE_SLO_BUDGETS` for it) -- the same "no measurement
 *  landed yet" path the real endpoint takes for a brand-new definition. */
export async function makeFixtureCreateSloDefinition(
  organizationId: string,
  body: SloDefinitionCreate,
): Promise<SloDefinitionRead> {
  await wait(90);
  if (FIXTURE_SLOS.some((s) => s.slo_key === body.slo_key)) {
    throw new ApiError(409, "slo_key already exists");
  }
  const now = new Date().toISOString();
  const slo: SloDefinitionRead = {
    id: `slo_${body.slo_key.replace(/[^a-z0-9]+/g, "_")}`,
    organization_id: organizationId,
    slo_key: body.slo_key, name: body.name, target: body.target,
    window_days: body.window_days, threshold: body.threshold, status: "ACTIVE",
    created_by: "local-ui-admin", created_at: now, updated_at: now,
  };
  FIXTURE_SLOS.unshift(slo);
  return slo;
}

/** `GET /v1/observability/slo/{slo_id}/budget`. */
export async function makeFixtureSloBudget(sloId: string): Promise<SloBudgetRead> {
  await wait(60);
  const seeded = FIXTURE_SLO_BUDGETS[sloId];
  if (seeded) return seeded;
  const slo = FIXTURE_SLOS.find((s) => s.id === sloId);
  if (!slo) throw new ApiError(404, "slo definition not found");
  return {
    slo_id: slo.id, slo_key: slo.slo_key, name: slo.name, target: slo.target,
    current_value: null, budget_remaining: null, window_days: slo.window_days,
    status: "NO_DATA",
  };
}

/** `GET /v1/observability/archive/status` -- legal hold active on one of
 *  twelve WORM archives, the same "mostly healthy, one hold to account for"
 *  posture the legacy screen's own fixtures favored elsewhere in this file. */
export async function makeFixtureArchiveStatus(): Promise<ArchiveStatusRead> {
  await wait(60);
  return {
    total_archives: 12,
    total_events_archived: 48213,
    latest_archive_id: "arch_2026_09_02",
    latest_checksum: fakeHex("arch_2026_09_02"),
    legal_hold_count: 1,
    status: "LEGAL_HOLD_ACTIVE",
  };
}

const FIXTURE_NOTIFICATION_RULES: NotificationRuleRead[] = [
  {
    id: "ntf_slo_breach_pager", organization_id: RELIABILITY_ORG,
    name: "SLO breach — page on-call",
    conditions: { event_type: "slo.breached", severity: ["CRITICAL"] },
    channel: "ITSM", recipients: ["oncall-data-platform@tenant.example"],
    escalation_after_minutes: 15, enabled: true,
    created_by: "local-ui-admin", created_at: "2026-07-10T00:00:00Z", updated_at: "2026-07-10T00:00:00Z",
  },
  {
    id: "ntf_contract_violation_email", organization_id: RELIABILITY_ORG,
    name: "Contract violation digest",
    conditions: { event_type: "contract.violations_detected" },
    channel: "EMAIL", recipients: ["data-governance@tenant.example", "steward@tenant.example"],
    escalation_after_minutes: null, enabled: true,
    created_by: "local-ui-admin", created_at: "2026-07-12T00:00:00Z", updated_at: "2026-07-12T00:00:00Z",
  },
  {
    id: "ntf_archive_legal_hold_webhook", organization_id: RELIABILITY_ORG,
    name: "Legal hold placed — notify compliance",
    conditions: { event_type: "archive.legal_hold.placed" },
    channel: "WEBHOOK", recipients: ["https://hooks.tenant.example/compliance"],
    escalation_after_minutes: 60, enabled: false,
    created_by: "local-ui-admin", created_at: "2026-06-30T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  },
];

/** `GET /v1/notification-rules`. */
export async function makeFixtureNotificationRules(
  organizationId: string,
  query: NotificationRuleQuery,
): Promise<PageOf<NotificationRuleRead>> {
  await wait(70);
  void organizationId;
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return {
    items: FIXTURE_NOTIFICATION_RULES.slice(offset, offset + limit),
    limit, offset, total: FIXTURE_NOTIFICATION_RULES.length,
  };
}

/** `POST /v1/notification-rules`. */
export async function makeFixtureCreateNotificationRule(
  organizationId: string,
  body: NotificationRuleCreate,
): Promise<NotificationRuleRead> {
  await wait(90);
  const now = new Date().toISOString();
  const rule: NotificationRuleRead = {
    id: `ntf_${fakeHex(body.name, 10)}`,
    organization_id: organizationId,
    name: body.name, conditions: body.conditions ?? {}, channel: body.channel,
    recipients: body.recipients, escalation_after_minutes: body.escalation_after_minutes ?? null,
    enabled: body.enabled ?? true,
    created_by: "local-ui-admin", created_at: now, updated_at: now,
  };
  FIXTURE_NOTIFICATION_RULES.unshift(rule);
  return rule;
}

/* Data contracts take no picker -- a contract id is free-text (see
   `ReliabilityScreen`'s own scope note), so these three generators are pure
   functions of the id string rather than a fixed lookup table: the same id
   always produces the same evaluation/violations/SLA status, via `fakeHex`,
   the same deterministic-hash approach `FIXTURE_CONTEXT_PRODUCTS` etc. use
   elsewhere in this file. */

const CONTRACT_VIOLATION_TYPES = ["SCHEMA_DRIFT", "FRESHNESS_BREACH", "QUALITY_THRESHOLD_BREACH"] as const;

function contractSeed(contractId: string): number {
  return parseInt(fakeHex(contractId, 8), 16);
}

function buildContractViolations(contractId: string): ViolationRead[] {
  const seed = contractSeed(contractId);
  const scenario = seed % 3;
  if (scenario === 0) return [];
  const now = new Date().toISOString();
  const violationType = CONTRACT_VIOLATION_TYPES[seed % CONTRACT_VIOLATION_TYPES.length] ?? CONTRACT_VIOLATION_TYPES[0];
  const severity = scenario === 2 ? "CRITICAL" : "WARNING";
  const violation: ViolationRead = {
    id: `viol_${fakeHex(contractId + ":0", 12)}`,
    organization_id: RELIABILITY_ORG,
    contract_id: contractId,
    violation_type: violationType,
    severity,
    evidence:
      violationType === "SCHEMA_DRIFT"
        ? { column: "settlement_amount", expected_type: "DECIMAL", observed_type: "FLOAT" }
        : violationType === "FRESHNESS_BREACH"
          ? { max_age_minutes: 60, observed_age_minutes: 60 + (seed % 240) }
          : { minimum_score: 95, observed_score: 95 - (1 + (seed % 15)) },
    detected_at: now,
    resolved_at: null,
    resolved_by: null,
    created_at: now,
    updated_at: now,
  };
  if (scenario === 1) return [violation];
  const second: ViolationRead = {
    ...violation,
    id: `viol_${fakeHex(contractId + ":1", 12)}`,
    violation_type: CONTRACT_VIOLATION_TYPES[(seed + 1) % CONTRACT_VIOLATION_TYPES.length] ?? CONTRACT_VIOLATION_TYPES[0],
    severity: "WARNING",
  };
  return [violation, second];
}

/** `POST /v1/data-contracts/{contract_id}/evaluate`. */
export async function makeFixtureEvaluateDataContract(contractId: string): Promise<EvaluationResponse> {
  await wait(140);
  const violations = buildContractViolations(contractId);
  const hasCritical = violations.some((v) => v.severity === "CRITICAL");
  const enforcement_action = hasCritical ? "BLOCK" : violations.length ? "WARN" : "ALLOW";
  return {
    contract_id: contractId,
    violations: violations.map((v) => ({
      violation_type: v.violation_type,
      severity: v.severity,
      evidence: v.evidence,
      detected_at: v.detected_at,
    })),
    enforcement_action,
    allowed: !hasCritical,
    reason: hasCritical
      ? "critical contract violation — query blocked pending remediation"
      : violations.length
        ? "non-blocking violation detected; monitor for recurrence"
        : null,
  };
}

/** `GET /v1/data-contracts/{contract_id}/violations`. */
export async function makeFixtureContractViolations(
  contractId: string,
  query: ContractViolationsQuery,
): Promise<PageOf<ViolationRead>> {
  await wait(90);
  const all = buildContractViolations(contractId);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** `GET /v1/data-contracts/{contract_id}/sla-status`. */
export async function makeFixtureContractSlaStatus(
  contractId: string,
  periodDays: number,
): Promise<SlaStatusResponse> {
  await wait(90);
  const violations = buildContractViolations(contractId);
  const seed = contractSeed(contractId);
  const compliant = violations.every((v) => v.severity !== "CRITICAL");
  const now = new Date();
  const periodStart = new Date(now.getTime() - periodDays * 24 * 60 * 60 * 1000);
  return {
    contract_id: contractId,
    compliant,
    uptime_percent: compliant ? 99.9 - (seed % 5) / 100 : 96.5 - (seed % 300) / 100,
    violations_in_period: violations.length,
    breach_minutes: compliant ? 0 : 15 + (seed % 180),
    period_start: periodStart.toISOString(),
    period_end: now.toISOString(),
  };
}


/* ---------------------------------------------------------------------------
   Tool plans -- fixtures backing `ToolPlansScreen`. A tiny in-memory store
   keyed by plan id, the same idiom `FIXTURE_TOOLS` above uses: `create`
   appends a `DRAFT` plan with its steps, and `validate`/`execute`/`cancel`
   mutate that same record in place so one fixture session sees a
   consistent lifecycle (status transitions, accumulating evidence) rather
   than resetting on every call.
--------------------------------------------------------------------------- */

import type {
  ExecutionRead,
  ToolPlanCreate,
  ToolPlanDetailRead,
  ToolPlanRead,
  ToolPlanStepRead,
  ValidationResponse,
} from "./types";

const FIXTURE_TOOL_PLAN_ORG_ID = "00000000-0000-0000-0000-000000000001";

interface FixtureToolPlanEntry {
  plan: ToolPlanRead;
  steps: ToolPlanStepRead[];
  executions: ExecutionRead[];
}

const FIXTURE_TOOL_PLANS: Record<string, FixtureToolPlanEntry> = {};
let fixtureToolPlanSeq = 0;
let fixtureToolPlanExecutionSeq = 0;

function findFixtureToolPlan(planId: string): FixtureToolPlanEntry {
  const entry = FIXTURE_TOOL_PLANS[planId];
  if (!entry) throw new ApiError(404, "tool plan not found");
  return entry;
}

/** `POST /v1/tool-plans` -- always starts `DRAFT`, budget defaults filled
 *  the same way the real `PlanBudgetCreate` Pydantic model does. */
export async function makeFixtureCreateToolPlan(body: ToolPlanCreate): Promise<ToolPlanRead> {
  await wait(120);
  fixtureToolPlanSeq += 1;
  const id = `plan_${fixtureToolPlanSeq}`;
  const now = new Date().toISOString();
  const budget = {
    max_steps: body.budget?.max_steps ?? 20,
    max_time_seconds: body.budget?.max_time_seconds ?? 600,
    max_tokens: body.budget?.max_tokens ?? 100_000,
    max_cost_units: body.budget?.max_cost_units ?? 100.0,
  };
  const plan: ToolPlanRead = {
    id,
    organization_id: FIXTURE_TOOL_PLAN_ORG_ID,
    name: body.name,
    budget,
    status: "DRAFT",
    created_by: "local-ui-admin",
    created_at: now,
    updated_at: now,
  };
  const steps: ToolPlanStepRead[] = body.steps.map((s) => ({
    id: `${id}_step_${s.sequence}`,
    plan_id: id,
    sequence: s.sequence,
    tool_id: s.tool_id,
    tool_version: s.tool_version,
    parameters: s.parameters ?? {},
    dependencies: s.dependencies ?? [],
    timeout_seconds: s.timeout_seconds ?? 300,
    expected_cost: s.expected_cost ?? 0,
    status: "PENDING",
    started_at: null,
    completed_at: null,
    evidence: {},
    error_message: null,
  }));
  FIXTURE_TOOL_PLANS[id] = { plan, steps, executions: [] };
  return plan;
}

/** `GET /v1/tool-plans/{plan_id}`. */
export async function makeFixtureToolPlan(planId: string): Promise<ToolPlanDetailRead> {
  await wait(80);
  const { plan, steps } = findFixtureToolPlan(planId);
  return { ...plan, steps };
}

/** `POST /v1/tool-plans/{plan_id}/validate` -- flags a step whose
 *  `dependencies` reference a sequence number no step in this plan carries,
 *  and warns when the steps' combined `expected_cost` exceeds
 *  `budget.max_cost_units`; enough surface to exercise the screen's issue
 *  list without reimplementing the real planner's validation. Moves the
 *  plan to `VALIDATED` only when no `ERROR`-severity issue was found,
 *  mirroring the real endpoint's own effect. */
export async function makeFixtureValidateToolPlan(planId: string): Promise<ValidationResponse> {
  await wait(100);
  const entry = findFixtureToolPlan(planId);
  const sequences = new Set(entry.steps.map((s) => s.sequence));
  const issues: ValidationResponse["issues"] = [];
  for (const step of entry.steps) {
    for (const dep of step.dependencies) {
      if (!sequences.has(dep)) {
        issues.push({
          step_sequence: step.sequence,
          issue: `depends on step ${dep}, which is not part of this plan`,
          severity: "ERROR",
        });
      }
    }
  }
  const maxCostUnits = (entry.plan.budget as { max_cost_units?: number }).max_cost_units;
  const totalCost = entry.steps.reduce((sum, s) => sum + s.expected_cost, 0);
  if (typeof maxCostUnits === "number" && totalCost > maxCostUnits) {
    issues.push({
      step_sequence: entry.steps[0]?.sequence ?? 1,
      issue: `combined expected_cost (${totalCost}) exceeds budget.max_cost_units (${maxCostUnits})`,
      severity: "WARNING",
    });
  }
  const valid = issues.every((i) => i.severity !== "ERROR");
  const now = new Date().toISOString();
  entry.plan = { ...entry.plan, status: valid ? "VALIDATED" : entry.plan.status, updated_at: now };
  return { valid, issues };
}

/** `POST /v1/tool-plans/{plan_id}/execute` -- 409s unless the plan is
 *  `DRAFT`/`VALIDATED`, same as the real endpoint. Marks every step
 *  `COMPLETED` and the plan `COMPLETED`, and appends one `ExecutionRead` to
 *  the plan's evidence history. */
export async function makeFixtureExecuteToolPlan(planId: string): Promise<ExecutionRead> {
  await wait(180);
  const entry = findFixtureToolPlan(planId);
  if (entry.plan.status !== "DRAFT" && entry.plan.status !== "VALIDATED") {
    throw new ApiError(409, `plan status ${entry.plan.status} cannot be executed`);
  }
  fixtureToolPlanExecutionSeq += 1;
  const startedAt = new Date().toISOString();
  const completedAt = new Date(Date.now() + entry.steps.length * 400).toISOString();
  const now = completedAt;
  for (const step of entry.steps) {
    step.status = "COMPLETED";
    step.started_at = startedAt;
    step.completed_at = completedAt;
    step.evidence = { simulated: true };
  }
  entry.plan = { ...entry.plan, status: "COMPLETED", updated_at: now };
  const execution: ExecutionRead = {
    id: `exec_${planId}_${fixtureToolPlanExecutionSeq}`,
    organization_id: entry.plan.organization_id,
    plan_id: planId,
    started_at: startedAt,
    completed_at: completedAt,
    budget_consumed: {
      steps_executed: entry.steps.length,
      time_seconds: entry.steps.reduce((sum, s) => sum + s.timeout_seconds, 0),
      tokens_used: 0,
      cost_units: entry.steps.reduce((sum, s) => sum + s.expected_cost, 0),
    },
    status: "COMPLETED",
    executed_by: "local-ui-admin",
    created_at: startedAt,
    updated_at: now,
  };
  entry.executions.push(execution);
  return execution;
}

/** `POST /v1/tool-plans/{plan_id}/cancel` -- 409s once the plan is already
 *  `COMPLETED`/`CANCELLED`, same as the real endpoint. */
export async function makeFixtureCancelToolPlan(planId: string): Promise<ToolPlanRead> {
  await wait(90);
  const entry = findFixtureToolPlan(planId);
  if (entry.plan.status === "COMPLETED" || entry.plan.status === "CANCELLED") {
    throw new ApiError(409, `plan status ${entry.plan.status} cannot be cancelled`);
  }
  entry.plan = { ...entry.plan, status: "CANCELLED", updated_at: new Date().toISOString() };
  for (const step of entry.steps) {
    if (step.status === "PENDING" || step.status === "RUNNING") step.status = "CANCELLED";
  }
  return entry.plan;
}

/** `GET /v1/tool-plans/{plan_id}/evidence` -- every `ExecutionRead` this
 *  fixture session has recorded for the plan, newest first, offset/limit
 *  applied client-side exactly like the other `PageOf` fixtures above. */
export async function makeFixtureToolPlanEvidence(
  planId: string,
  query: { limit?: number; offset?: number },
): Promise<PageOf<ExecutionRead>> {
  await wait(90);
  const entry = findFixtureToolPlan(planId);
  const all = [...entry.executions].reverse();
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** AG-10: an agent inbox with one of each state a reviewer must be able to
 *  tell apart -- a human proposal and an agent proposal, an item the reviewer
 *  agent recommends approving and one it recommends rejecting on prior
 *  rejections, an auto-applied task sampled for audit and one not, and an
 *  agent whose kill switch is engaged. */
export function makeFixtureAgentInbox(organizationId: string, persona: string): AgentInboxRead {
  const now = new Date();
  const iso = (minutesAgo: number) =>
    new Date(now.getTime() - minutesAgo * 60_000).toISOString();
  return {
    organization_id: organizationId,
    persona,
    generated_at: now.toISOString(),
    summary: {
      pending_decisions: 3,
      auto_applied_since: 2,
      sampled_for_audit: 1,
      agents_active: 2,
      kill_switch_engaged: true,
    },
    agents: [
      {
        ai_asset_id: "11111111-1111-1111-1111-111111111111",
        version_id: "aaaaaaaa-1111-1111-1111-111111111111",
        name: "Steward agent",
        risk_tier: "LOW",
        autonomy_tier: "T1",
        runs_recent: 148,
        success_rate: 0.96,
        budget: { daily_token_cap: 500_000, daily_tokens_estimated: 214_300 },
        kill_scope: "AGENT",
        kill_engaged: false,
        supervisor_persona: "STEWARD",
      },
      {
        ai_asset_id: "22222222-2222-2222-2222-222222222222",
        version_id: "aaaaaaaa-2222-2222-2222-222222222222",
        name: "Lineage agent",
        risk_tier: "MEDIUM",
        autonomy_tier: "T1",
        runs_recent: 32,
        success_rate: 0.88,
        budget: { daily_token_cap: 200_000, daily_tokens_estimated: null },
        kill_scope: "AGENT",
        kill_engaged: false,
        supervisor_persona: "STEWARD",
      },
      {
        ai_asset_id: "33333333-3333-3333-3333-333333333333",
        version_id: "aaaaaaaa-3333-3333-3333-333333333333",
        name: "Red-team agent",
        risk_tier: "HIGH",
        autonomy_tier: "T3",
        runs_recent: 0,
        success_rate: null,
        budget: { daily_token_cap: null, daily_tokens_estimated: null },
        kill_scope: "TIER",
        kill_engaged: true,
        supervisor_persona: "OPERATOR",
      },
    ],
    pending: [
      {
        review_id: "bbbbbbbb-1111-1111-1111-111111111111",
        object_type: "ASSET_DESCRIPTION_DRAFT",
        object_id: "cccccccc-1111-1111-1111-111111111111",
        title: "PUBLISH ASSET_DESCRIPTION_DRAFT",
        proposed_by: "agent:steward",
        proposed_by_kind: "AGENT",
        risk_tier: "T0",
        confidence: 0.91,
        blast_radius: 12,
        negative_knowledge_hits: 0,
        recommendation: "APPROVE",
        created_at: iso(35),
      },
      {
        review_id: "bbbbbbbb-2222-2222-2222-222222222222",
        object_type: "GLOSSARY_LINK_PROPOSAL",
        object_id: "cccccccc-2222-2222-2222-222222222222",
        title: "PUBLISH GLOSSARY_LINK_PROPOSAL",
        proposed_by: "agent:steward",
        proposed_by_kind: "AGENT",
        risk_tier: "T1",
        confidence: 0.42,
        blast_radius: 3,
        negative_knowledge_hits: 2,
        recommendation: "REJECT",
        created_at: iso(90),
      },
      {
        review_id: "bbbbbbbb-3333-3333-3333-333333333333",
        object_type: "SEMANTIC_MODEL_VERSION",
        object_id: "cccccccc-3333-3333-3333-333333333333",
        title: "PUBLISH SEMANTIC_MODEL_VERSION",
        proposed_by: "priya.steward",
        proposed_by_kind: "HUMAN",
        risk_tier: "T2",
        confidence: null,
        blast_radius: 47,
        negative_knowledge_hits: 0,
        recommendation: "NONE",
        created_at: iso(180),
      },
    ],
    auto_applied: [
      {
        task_id: "dddddddd-1111-1111-1111-111111111111",
        agent_name: "Steward agent",
        action: "agent.analysis",
        object_type: "ASSET_DESCRIPTION_DRAFT",
        object_id: "cccccccc-4444-4444-4444-444444444444",
        applied_at: iso(20),
        sampled_for_audit: true,
        audit_outcome: "PENDING",
      },
      {
        task_id: "dddddddd-2222-2222-2222-222222222222",
        agent_name: "Lineage agent",
        action: "agent.analysis",
        object_type: "VIEW_LINEAGE_EDGE",
        object_id: "cccccccc-5555-5555-5555-555555555555",
        applied_at: iso(55),
        sampled_for_audit: false,
        audit_outcome: null,
      },
    ],
    recent_tasks: [
      {
        task_id: "dddddddd-1111-1111-1111-111111111111",
        agent_name: "Steward agent",
        intent: "agent.analysis",
        status: "SAMPLED",
        started_at: iso(21),
        finished_at: iso(20),
      },
      {
        task_id: "dddddddd-3333-3333-3333-333333333333",
        agent_name: "Steward agent",
        intent: "agent.analysis",
        status: "REJECTED",
        started_at: iso(70),
        finished_at: iso(70),
      },
      {
        task_id: "dddddddd-2222-2222-2222-222222222222",
        agent_name: "Lineage agent",
        intent: "agent.analysis",
        status: "APPLIED",
        started_at: iso(56),
        finished_at: iso(55),
      },
    ],
  };
}

/** UX-19: `GET /v1/organizations/{org}/ai-agents/roster` fixture. Mirrors
 *  `aida.agent_roster.compose_agent_roster` — every registered `AGENT`-kind
 *  asset alongside the *same* organization-wide method summary and recent
 *  results window (the honesty note the real endpoint's docstring makes:
 *  `AgentRun` carries no per-agent identity today). */
export function makeFixtureAgentRoster(organizationId: string, windowDays = 30): AgentRosterRead {
  const now = new Date();
  const iso = (hoursAgo: number) => new Date(now.getTime() - hoursAgo * 3_600_000).toISOString();

  const method = {
    scope: "ORGANIZATION_WIDE" as const,
    note:
      "AgentRun carries no per-registered-agent identity today -- this summarizes this " +
      "organization's actual governed-agent run activity as a whole, not this specific " +
      "registered entity's own isolated execution history.",
    window_days: windowDays,
    sampled_runs: 214,
    by_strategy: {
      GOVERNED_TOOL: 132,
      MODEL_GENERATION: 61,
      DEVELOPMENT_SQL: 14,
      CLARIFICATION: 7,
    },
    average_confidence: 0.83,
    tool_first: {
      tool_first_executions: 132,
      freeform_executions: 61,
      total_executions: 193,
      rate: 0.684,
      by_source: {
        GOVERNED_TOOL: 132,
        MODEL_GATEWAY: 47,
        QUERY_MEMORY_ADAPTATION: 14,
      },
      target_rate: 0.6,
      meets_target: true,
    },
  };

  const recentResults = [
    {
      run_id: "eeeeeeee-1111-1111-1111-111111111111",
      status: "COMPLETED",
      strategy: "GOVERNED_TOOL",
      confidence: 0.94,
      generation_source: "GOVERNED_TOOL",
      created_at: iso(1),
      failure_reason: null,
    },
    {
      run_id: "eeeeeeee-2222-2222-2222-222222222222",
      status: "COMPLETED",
      strategy: "MODEL_GENERATION",
      confidence: 0.71,
      generation_source: "MODEL_GATEWAY",
      created_at: iso(3),
      failure_reason: null,
    },
    {
      run_id: "eeeeeeee-3333-3333-3333-333333333333",
      status: "REJECTED",
      strategy: "MODEL_GENERATION",
      confidence: 0.38,
      generation_source: "MODEL_GATEWAY",
      created_at: iso(6),
      failure_reason: "AMBIGUOUS_DEFINITION",
    },
  ];

  const autoApply = {
    has_auto_apply_branch: false,
    threshold: null,
    threshold_source: null,
    evidence:
      "No agent plan in this codebase reaches a branch that applies an AI-authored action " +
      "without a human decision. Every proposal-shaped output routes through the shared " +
      "GovernanceReview maker-checker queue.",
  };

  return {
    organization_id: organizationId,
    generated_at: now.toISOString(),
    window_days: windowDays,
    agents: [
      {
        purpose: {
          asset_id: "11111111-1111-1111-1111-111111111111",
          asset_key: "steward-agent",
          version: 4,
          status: "APPROVED",
          name: "Steward agent",
          description: "Drafts business-glossary links and asset descriptions for steward review.",
          intended_use: "Propose, never publish: metadata stewardship drafts for the catalog.",
          owner_principal: "priya.steward",
          provider_type: "PLATFORM_NATIVE",
          risk_tier: "LOW",
          documentation_url: null,
        },
        method,
        recent_results: recentResults,
        recent_results_total: 214,
        auto_apply: autoApply,
      },
      {
        purpose: {
          asset_id: "22222222-2222-2222-2222-222222222222",
          asset_key: "lineage-agent",
          version: 2,
          status: "APPROVED",
          name: "Lineage agent",
          description: "Answers lineage and impact questions from the unified lineage graph.",
          intended_use: "Read-only impact analysis for analysts and reviewers.",
          owner_principal: "priya.steward",
          provider_type: "PLATFORM_NATIVE",
          risk_tier: "MEDIUM",
          documentation_url: null,
        },
        method,
        recent_results: recentResults,
        recent_results_total: 214,
        auto_apply: autoApply,
      },
    ],
    total_agents: 2,
  };
}

/** ADR-0027: the reviewer agent's own state. Mirrors
 *  `aida.agent_contract_api.ReviewerAgentStateRead` field for field. */
export function makeFixtureReviewerAgentState(organizationId: string): ReviewerAgentStateRead {
  return {
    organization_id: organizationId,
    enabled: true,
    suspended: false,
    max_tier: "T1",
    sampling_rate: 0.1,
    agent_principal_id: "agent:reviewer",
  };
}

/** `POST .../reviewer-agent/pre-review` fixture -- annotates the pending
 *  queue with tier/evidence/recommendation but decides nothing. */
export function makeFixtureReviewerAgentPreReview(): ReviewerAgentRunResult {
  return { pre_reviewed: 14, decided: 0, approved: 0, rejected: 0, sampled_for_audit: 0 };
}

/** `POST .../reviewer-agent/run` fixture -- auto-decides T0/T1 items. */
export function makeFixtureReviewerAgentRun(): ReviewerAgentRunResult {
  return { pre_reviewed: 0, decided: 8, approved: 5, rejected: 3, sampled_for_audit: 2 };
}

/** `GET .../reviewer-agent/disagreement-rates` fixture. One breaching object
 *  type (GLOSSARY_LINK_PROPOSAL) so the "breaches revisit trigger" pill has
 *  something to render in fixture mode, and one object type below the
 *  minimum-resolved threshold so `sufficient_sample: false` also has a row. */
export function makeFixtureDisagreementRates(windowDays: number): DisagreementReportRead {
  return {
    window_days: windowDays,
    computed_at: new Date().toISOString(),
    measured: true,
    threshold: 0.05,
    minimum_resolved_for_signal: 20,
    breaching_object_types: ["GLOSSARY_LINK_PROPOSAL"],
    by_object_type: [
      {
        object_type: "ASSET_DESCRIPTION_DRAFT",
        sampled: 40,
        resolved: 38,
        agreed: 36,
        disagreed: 2,
        pending: 2,
        disagreement_rate: 0.0526,
        sufficient_sample: true,
        breaches_revisit_trigger: true,
      },
      {
        object_type: "GLOSSARY_LINK_PROPOSAL",
        sampled: 24,
        resolved: 24,
        agreed: 20,
        disagreed: 4,
        pending: 0,
        disagreement_rate: 0.1667,
        sufficient_sample: true,
        breaches_revisit_trigger: true,
      },
      {
        object_type: "VIEW_LINEAGE_EDGE",
        sampled: 9,
        resolved: 3,
        agreed: 3,
        disagreed: 0,
        pending: 6,
        disagreement_rate: 0.0,
        sufficient_sample: false,
        breaches_revisit_trigger: false,
      },
    ],
  };
}

export interface ReviewerAgentSamplesQuery {
  outcome?: string;
  limit?: number;
  offset?: number;
}

/** `GET .../reviewer-agent/samples` fixture. Filters by `outcome` the same
 *  way the real route does, so the screen's outcome picker has something
 *  visible to switch between. */
export function makeFixtureReviewerAgentSamples(
  query: ReviewerAgentSamplesQuery,
): PageOf<ReviewAuditSampleRead> {
  const now = new Date();
  const iso = (hoursAgo: number) => new Date(now.getTime() - hoursAgo * 3_600_000).toISOString();
  const all: ReviewAuditSampleRead[] = [
    {
      sample_id: "ffffffff-1111-1111-1111-111111111111",
      governance_review_id: "bbbbbbbb-1111-1111-1111-111111111111",
      agent_principal_id: "agent:steward",
      object_type: "ASSET_DESCRIPTION_DRAFT",
      risk_tier: "T0",
      decision: "APPROVED",
      sampled_at: iso(2),
      human_outcome: "PENDING",
      human_principal_id: null,
      human_rationale: null,
      resolved_at: null,
    },
    {
      sample_id: "ffffffff-2222-2222-2222-222222222222",
      governance_review_id: "bbbbbbbb-4444-4444-4444-444444444444",
      agent_principal_id: "agent:steward",
      object_type: "GLOSSARY_LINK_PROPOSAL",
      risk_tier: "T1",
      decision: "APPROVED",
      sampled_at: iso(6),
      human_outcome: "PENDING",
      human_principal_id: null,
      human_rationale: null,
      resolved_at: null,
    },
    {
      sample_id: "ffffffff-3333-3333-3333-333333333333",
      governance_review_id: "bbbbbbbb-5555-5555-5555-555555555555",
      agent_principal_id: "agent:steward",
      object_type: "ASSET_DESCRIPTION_DRAFT",
      risk_tier: "T0",
      decision: "APPROVED",
      sampled_at: iso(30),
      human_outcome: "AGREED",
      human_principal_id: "priya.steward",
      human_rationale: "Matches the source column comments.",
      resolved_at: iso(28),
    },
    {
      sample_id: "ffffffff-4444-4444-4444-444444444444",
      governance_review_id: "bbbbbbbb-6666-6666-6666-666666666666",
      agent_principal_id: "agent:steward",
      object_type: "GLOSSARY_LINK_PROPOSAL",
      risk_tier: "T1",
      decision: "REJECTED",
      sampled_at: iso(50),
      human_outcome: "DISAGREED",
      human_principal_id: "priya.steward",
      human_rationale: "The term does map to this column; the agent's rejection was too strict.",
      resolved_at: iso(48),
    },
  ];
  const outcome = query.outcome ?? "PENDING";
  const items = outcome === "ALL" ? all : all.filter((sample) => sample.human_outcome === outcome);
  const limit = query.limit ?? 50;
  const offset = query.offset ?? 0;
  return {
    items: items.slice(offset, offset + limit),
    limit,
    offset,
    total: items.length,
  };
}

/* ---------------------------------------------------------------------------
   AT-1: Playbooks -- saved, scheduled bulk-metadata automation rules
   (`playbooks_api.py`). A module-level, in-memory store keyed by org so
   create/toggle/delete/run are real round trips within one fixture session,
   mirroring the pattern `FIXTURE_DBT_PROJECTS` above uses for the same
   reason.
--------------------------------------------------------------------------- */

const FIXTURE_PLAYBOOKS: Record<string, PlaybookRead[]> = {};

function seedFixturePlaybooks(organizationId: string): PlaybookRead[] {
  if (FIXTURE_PLAYBOOKS[organizationId]) return FIXTURE_PLAYBOOKS[organizationId]!;
  const now = new Date();
  const iso = (daysAgo: number) => new Date(now.getTime() - daysAgo * 86_400_000).toISOString();
  const seeded: PlaybookRead[] = [
    {
      id: "77777777-0001-0001-0001-000000000001",
      organization_id: organizationId,
      name: "Tag PII-shaped staging tables",
      action: "TAG",
      datasource_id: "10000000-0000-0000-0000-000000000001",
      match_field: "TABLE_NAME",
      match_pattern: "stg_%",
      column_name_pattern: null,
      action_parameters: { tag_key: "needs-review" },
      schedule_interval_minutes: 60,
      auto_apply_max_items: 50,
      enabled: true,
      created_by: "priya.steward",
      last_run_at: iso(0.2),
      created_at: iso(30),
      updated_at: iso(0.2),
    },
    {
      id: "77777777-0002-0002-0002-000000000002",
      organization_id: organizationId,
      name: "Classify email columns as PII",
      action: "CLASSIFY",
      datasource_id: "10000000-0000-0000-0000-000000000001",
      match_field: "SCHEMA_NAME",
      match_pattern: "public",
      column_name_pattern: "%email%",
      action_parameters: { classification: "PII" },
      schedule_interval_minutes: 1440,
      auto_apply_max_items: 0,
      enabled: true,
      created_by: "priya.steward",
      last_run_at: iso(1),
      created_at: iso(60),
      updated_at: iso(1),
    },
    {
      id: "77777777-0003-0003-0003-000000000003",
      organization_id: organizationId,
      name: "Assign finance ownership",
      action: "OWN",
      datasource_id: "10000000-0000-0000-0000-000000000002",
      match_field: "QUALIFIED_NAME",
      match_pattern: "finance.%",
      column_name_pattern: null,
      action_parameters: { owner_type: "GROUP", owner_principal: "finance-data-team" },
      schedule_interval_minutes: 720,
      auto_apply_max_items: 10,
      enabled: false,
      created_by: "raj.admin",
      last_run_at: null,
      created_at: iso(14),
      updated_at: iso(7),
    },
    {
      id: "77777777-0004-0004-0004-000000000004",
      organization_id: organizationId,
      name: "Certify gold-layer reporting tables",
      action: "CERTIFY",
      datasource_id: "10000000-0000-0000-0000-000000000002",
      match_field: "TABLE_NAME",
      match_pattern: "gold_%",
      column_name_pattern: null,
      action_parameters: { rationale: "Automated quarterly gold-layer recertification", expires_after_days: 90 },
      schedule_interval_minutes: 10_080,
      auto_apply_max_items: 0,
      enabled: true,
      created_by: "priya.steward",
      last_run_at: iso(3),
      created_at: iso(90),
      updated_at: iso(3),
    },
    {
      id: "77777777-0005-0005-0005-000000000005",
      organization_id: organizationId,
      name: "Tag legacy archive tables",
      action: "TAG",
      datasource_id: "10000000-0000-0000-0000-000000000001",
      match_field: "TABLE_NAME",
      match_pattern: "%_archive",
      column_name_pattern: null,
      action_parameters: { tag_key: "legacy", tag_value: "true" },
      schedule_interval_minutes: 10_080,
      auto_apply_max_items: 0,
      enabled: false,
      created_by: "raj.admin",
      last_run_at: null,
      created_at: iso(5),
      updated_at: iso(5),
    },
  ];
  FIXTURE_PLAYBOOKS[organizationId] = seeded;
  return seeded;
}

/** `GET /v1/organizations/{organization_id}/playbooks`. */
export async function makeFixturePlaybooks(
  organizationId: string,
  query: PlaybooksQuery = {},
): Promise<PageOf<PlaybookRead>> {
  await wait(80);
  const all = seedFixturePlaybooks(organizationId);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** `POST /v1/organizations/{organization_id}/playbooks` -- 409 on a
 *  duplicate name within the org, mirroring the real routes own unique
 *  constraint. */
export async function makeFixtureCreatePlaybook(
  organizationId: string,
  body: PlaybookCreate,
): Promise<PlaybookRead> {
  await wait(120);
  const items = seedFixturePlaybooks(organizationId);
  if (items.some((p) => p.name === body.name)) {
    throw new ApiError(409, "a playbook with this name already exists");
  }
  const now = new Date().toISOString();
  const playbook: PlaybookRead = {
    id: `77777777-${Math.random().toString(16).slice(2, 6)}-0000-0000-${Date.now().toString(16).padStart(12, "0")}`,
    organization_id: organizationId,
    name: body.name,
    action: body.action,
    datasource_id: body.datasource_id,
    match_field: body.match_field ?? "TABLE_NAME",
    match_pattern: body.match_pattern,
    column_name_pattern: body.column_name_pattern ?? null,
    action_parameters: body.action_parameters,
    schedule_interval_minutes: body.schedule_interval_minutes,
    auto_apply_max_items: body.auto_apply_max_items ?? 0,
    enabled: body.enabled ?? true,
    created_by: "local-ui-admin",
    last_run_at: null,
    created_at: now,
    updated_at: now,
  };
  items.unshift(playbook);
  return playbook;
}

function findFixturePlaybook(playbookId: string): PlaybookRead | undefined {
  return Object.values(FIXTURE_PLAYBOOKS).flat().find((p) => p.id === playbookId);
}

/** `PATCH /v1/playbooks/{playbook_id}` -- every field optional; only the
 *  fields present in `body` are applied, mirroring the real routes
 *  `exclude_unset` semantics. */
export async function makeFixtureUpdatePlaybook(
  playbookId: string,
  body: PlaybookUpdate,
): Promise<PlaybookRead> {
  await wait(90);
  const playbook = findFixturePlaybook(playbookId);
  if (!playbook) throw new ApiError(404, "playbook not found");
  Object.assign(playbook, body, { updated_at: new Date().toISOString() });
  return playbook;
}

/** `DELETE /v1/playbooks/{playbook_id}` -- 204, no response body. */
export async function makeFixtureDeletePlaybook(playbookId: string): Promise<void> {
  await wait(90);
  for (const items of Object.values(FIXTURE_PLAYBOOKS)) {
    const index = items.findIndex((p) => p.id === playbookId);
    if (index !== -1) {
      items.splice(index, 1);
      return;
    }
  }
  throw new ApiError(404, "playbook not found");
}

/** `POST /v1/playbooks/{playbook_id}/run` -- 409 if the playbook is
 *  disabled, mirroring the real routes own check. */
export async function makeFixtureRunPlaybook(playbookId: string): Promise<PlaybookRunResultRead> {
  await wait(150);
  const playbook = findFixturePlaybook(playbookId);
  if (!playbook) throw new ApiError(404, "playbook not found");
  if (!playbook.enabled) throw new ApiError(409, "playbook is disabled");
  playbook.last_run_at = new Date().toISOString();
  const matchedCount = 3;
  const outcome = playbook.auto_apply_max_items > 0 && matchedCount <= playbook.auto_apply_max_items
    ? "AUTO_APPLIED"
    : "GOVERNANCE_REVIEW_QUEUED";
  return {
    playbook_id: playbookId,
    matched_count: matchedCount,
    outcome,
    bulk_action_run_id: outcome === "AUTO_APPLIED" ? `88888888-0000-0000-0000-${Date.now().toString(16).padStart(12, "0")}` : null,
    bulk_stewardship_operation_id: null,
    governance_review_id: outcome === "GOVERNANCE_REVIEW_QUEUED" ? `99999999-0000-0000-0000-${Date.now().toString(16).padStart(12, "0")}` : null,
  };
}

/* ---------------------------------------------------------------------------
   Agent contract requests -- the reviewed, eval-gated path alongside the
   direct-write contract PUT. In-memory, keyed by org, mirroring
   `FIXTURE_PLAYBOOKS` above so submit/list are real round trips within one
   fixture session.
--------------------------------------------------------------------------- */

import type { AgentContractRequestCreate, AgentContractRequestRead } from "./types";

const FIXTURE_AGENT_CONTRACT_REQUESTS: Record<string, AgentContractRequestRead[]> = {};

function seedFixtureAgentContractRequests(organizationId: string): AgentContractRequestRead[] {
  if (FIXTURE_AGENT_CONTRACT_REQUESTS[organizationId]) {
    return FIXTURE_AGENT_CONTRACT_REQUESTS[organizationId]!;
  }
  const now = new Date();
  const iso = (daysAgo: number) => new Date(now.getTime() - daysAgo * 86_400_000).toISOString();
  const seeded: AgentContractRequestRead[] = [
    {
      id: "acr-0001-0000-0000-000000000001",
      organization_id: organizationId,
      ai_asset_version_id: "aiv_revenue_analyst",
      requested_by: "dev@tenant.example",
      definition: {
        agent_principal_id: "agent-revenue-analyst",
        capability_envelope: { tool_slugs: ["revenue-by-lob"], context_product_ids: [], write_lanes: [] },
        autonomy_tier: "T1",
        supervisor_persona: "ANALYST",
        kill_scope: "AGENT",
        sampling_rate: 0.2,
      },
      status: "ACTIVATED",
      governance_review_id: "rev-0001-0000-0000-000000000001",
      eval_gate_verdict: "PASS",
      activated_at: iso(2),
      created_at: iso(5),
      updated_at: iso(2),
    },
    {
      id: "acr-0002-0000-0000-000000000002",
      organization_id: organizationId,
      ai_asset_version_id: "aiv_fraud_model",
      requested_by: "dev@tenant.example",
      definition: {
        agent_principal_id: "agent-fraud-triage",
        capability_envelope: { tool_slugs: ["fraud-score-lookup"], context_product_ids: [], write_lanes: [] },
        autonomy_tier: "T1",
        supervisor_persona: "REVIEWER",
        kill_scope: "AGENT",
        sampling_rate: 0.3,
      },
      status: "PENDING",
      governance_review_id: "rev-0002-0000-0000-000000000002",
      eval_gate_verdict: null,
      activated_at: null,
      created_at: iso(0.5),
      updated_at: iso(0.5),
    },
  ];
  FIXTURE_AGENT_CONTRACT_REQUESTS[organizationId] = seeded;
  return seeded;
}

/** `GET /v1/organizations/{organization_id}/agent-contract-requests`. */
export async function makeFixtureAgentContractRequests(
  organizationId: string,
  query: { status?: string; aiAssetVersionId?: string; limit?: number; offset?: number } = {},
): Promise<PageOf<AgentContractRequestRead>> {
  await wait(80);
  let all = seedFixtureAgentContractRequests(organizationId);
  if (query.status) all = all.filter((r) => r.status === query.status);
  if (query.aiAssetVersionId) all = all.filter((r) => r.ai_asset_version_id === query.aiAssetVersionId);
  all = [...all].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 50;
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** `POST /v1/organizations/{organization_id}/agent-contract-requests` --
 *  always lands `PENDING`, matching the real endpoint: submission opens a
 *  `GovernanceReview` rather than activating anything. */
export async function makeFixtureSubmitAgentContractRequest(
  organizationId: string,
  body: AgentContractRequestCreate,
): Promise<AgentContractRequestRead> {
  await wait(150);
  const items = seedFixtureAgentContractRequests(organizationId);
  const now = new Date().toISOString();
  const request: AgentContractRequestRead = {
    id: `acr-${Date.now().toString(16).padStart(12, "0")}`,
    organization_id: organizationId,
    ai_asset_version_id: body.ai_asset_version_id,
    requested_by: "local-ui-admin",
    definition: {
      agent_principal_id: body.agent_principal_id,
      capability_envelope: body.capability_envelope ?? { tool_slugs: [], context_product_ids: [], write_lanes: [] },
      autonomy_tier: body.autonomy_tier ?? "T0",
      supervisor_persona: body.supervisor_persona,
      kill_scope: body.kill_scope ?? "AGENT",
      sampling_rate: body.sampling_rate ?? 0.1,
      daily_token_cap: body.daily_token_cap ?? null,
      per_run_token_cap: body.per_run_token_cap ?? null,
      wall_clock_seconds_cap: body.wall_clock_seconds_cap ?? null,
      eval_gate_threshold: body.eval_gate_threshold ?? null,
    },
    status: "PENDING",
    governance_review_id: `rev-${Date.now().toString(16).padStart(12, "0")}`,
    eval_gate_verdict: null,
    activated_at: null,
    created_at: now,
    updated_at: now,
  };
  items.unshift(request);
  return request;
}

/* ---------------------------------------------------------------------------
   Context product staged rollout + agent consumption (P3-01).

   The three shapes the Agent gateway and the Context Products screen need
   that had no fixture: the version history behind `latest_version`, the
   AT-7(b) consumer-binding registry, and the CX-4 consumption edges the MCP
   server and the Context Product REST API record on every allowed read.
--------------------------------------------------------------------------- */

import type {
  ConsumptionRecordPage,
  ConsumptionRecordRead,
  ContextProductConsumerBindingRead,
  ContextProductVersionRead,
} from "./types";

/** The organization every fixture in this file belongs to (`org.tsx`'s
 *  `DEFAULT_ORG_ID`, spelled out here rather than imported so fixtures stay
 *  free of a runtime dependency on the React-facing module). */
const CX_ORG_ID = "00000000-0000-0000-0000-000000000001";

const FIXTURE_CONTEXT_PRODUCT_BINDINGS: Record<string, ContextProductConsumerBindingRead[]> = {
  cp_consumer_risk: [
    {
      id: "cpb_risk_copilot",
      organization_id: CX_ORG_ID,
      product_id: "cp_consumer_risk",
      consumer_principal_id: "risk-copilot@agents.tenant.example",
      bound_version_id: "cpv_consumer_risk_1",
      bound_version_number: 1,
      created_by: "risk-data-stewards@tenant.example",
      created_at: "2026-08-12T00:00:00Z",
      updated_at: "2026-08-12T00:00:00Z",
    },
  ],
};

/** The version history behind each product's `latest_version`. Only products
 *  whose fixture has more than one version need an entry; everything else
 *  falls back to "the latest version is the only version". */
const FIXTURE_CONTEXT_PRODUCT_VERSION_HISTORY: Record<string, ContextProductVersionRead[]> = {};

function contextProductById(productId: string): ContextProductRead | undefined {
  for (const items of Object.values(FIXTURE_CONTEXT_PRODUCTS)) {
    const found = items.find((p) => p.id === productId);
    if (found) return found;
  }
  return undefined;
}

/** Every version of one product, newest first. Synthesises the superseded
 *  version a `based_on_version_id` implies so the binding picker has more
 *  than one row to choose between — the whole point of a staged rollout. */
export async function makeFixtureContextProductVersions(
  productId: string,
): Promise<PageOf<ContextProductVersionRead>> {
  await wait(70);
  const product = contextProductById(productId);
  if (!product) throw new ApiError(404, "context product not found");
  const explicit = FIXTURE_CONTEXT_PRODUCT_VERSION_HISTORY[productId];
  const latest = product.latest_version;
  const items =
    explicit ??
    (latest.based_on_version_id
      ? [
          latest,
          {
            ...latest,
            id: latest.based_on_version_id,
            version: latest.version - 1,
            status: "SUPPORTED",
            based_on_version_id: null,
            superseded_by_version_id: latest.id,
            superseded_at: latest.published_at,
            fingerprint: fakeHex(latest.based_on_version_id),
          },
        ]
      : [latest]);
  return { items, limit: items.length, offset: 0, total: items.length };
}

export async function makeFixtureContextProductBindings(
  productId: string,
): Promise<PageOf<ContextProductConsumerBindingRead>> {
  await wait(70);
  const items = FIXTURE_CONTEXT_PRODUCT_BINDINGS[productId] ?? [];
  return { items: [...items], limit: 100, offset: 0, total: items.length };
}

export async function makeFixtureSetContextProductBinding(
  productId: string,
  consumerPrincipalId: string,
  boundVersionId: string,
): Promise<ContextProductConsumerBindingRead> {
  await wait(90);
  const product = contextProductById(productId);
  if (!product) throw new ApiError(404, "context product not found");
  const versions = await makeFixtureContextProductVersions(productId);
  const bound = versions.items.find((v) => v.id === boundVersionId);
  if (!bound) throw new ApiError(422, "bound_version_id is not a version of this context product");
  const list = (FIXTURE_CONTEXT_PRODUCT_BINDINGS[productId] ??= []);
  const now = new Date().toISOString();
  const existing = list.find((b) => b.consumer_principal_id === consumerPrincipalId);
  if (existing) {
    existing.bound_version_id = bound.id;
    existing.bound_version_number = bound.version;
    existing.updated_at = now;
    return { ...existing };
  }
  const created: ContextProductConsumerBindingRead = {
    id: `cpb_${consumerPrincipalId.replace(/[^a-z0-9]+/gi, "_")}`,
    organization_id: product.organization_id,
    product_id: productId,
    consumer_principal_id: consumerPrincipalId,
    bound_version_id: bound.id,
    bound_version_number: bound.version,
    created_by: "local-ui-admin",
    created_at: now,
    updated_at: now,
  };
  list.push(created);
  return { ...created };
}

export async function makeFixtureRemoveContextProductBinding(
  productId: string,
  consumerPrincipalId: string,
): Promise<void> {
  await wait(70);
  const list = FIXTURE_CONTEXT_PRODUCT_BINDINGS[productId];
  if (!list) return;
  const index = list.findIndex((b) => b.consumer_principal_id === consumerPrincipalId);
  if (index >= 0) list.splice(index, 1);
}

const FIXTURE_CONSUMPTION: ConsumptionRecordRead[] = [
  {
    id: "cx_1", organization_id: CX_ORG_ID,
    consumer_id: "risk-copilot@agents.tenant.example", consumer_type: "AGENT",
    resource_type: "CONTEXT_PRODUCT", resource_id: "consumer-risk-context",
    channel: "MCP", correlation_id: "corr-4411", policy_decision: "ALLOW",
    business_purpose: "Monthly risk committee packet",
    details: { method: "prompts/get", version: 2 },
    consumed_at: "2026-09-04T09:14:00Z",
  },
  {
    id: "cx_2", organization_id: CX_ORG_ID,
    consumer_id: "risk-copilot@agents.tenant.example", consumer_type: "AGENT",
    resource_type: "GOVERNED_TOOL", resource_id: "delinquency-by-segment",
    channel: "MCP", correlation_id: "corr-4412", policy_decision: "ALLOW",
    business_purpose: "Monthly risk committee packet",
    details: { method: "tools/call", masked_columns: 2 },
    consumed_at: "2026-09-04T09:14:22Z",
  },
  {
    id: "cx_3", organization_id: CX_ORG_ID,
    consumer_id: "notebook-svc@tenant.example", consumer_type: "SERVICE",
    resource_type: "CATALOG_TABLE", resource_id: "analytics.core.orders_raw",
    channel: "REST", correlation_id: "corr-4498", policy_decision: "ALLOW",
    business_purpose: null,
    details: { method: "resources/read" },
    consumed_at: "2026-09-03T16:02:10Z",
  },
  {
    id: "cx_4", organization_id: CX_ORG_ID,
    consumer_id: "unbound-agent@agents.tenant.example", consumer_type: "AGENT",
    resource_type: "CONTEXT_PRODUCT", resource_id: "consumer-risk-context",
    channel: "MCP", correlation_id: "corr-4501", policy_decision: "DENY",
    business_purpose: null,
    details: { method: "prompts/get", reason: "quality gate: minimum_score" },
    consumed_at: "2026-09-03T11:47:03Z",
  },
];

export async function makeFixtureConsumptionRecords(
  filter: { consumerId?: string; resourceType?: string; resourceId?: string },
  query: { limit?: number; offset?: number },
): Promise<ConsumptionRecordPage> {
  await wait(90);
  let items = FIXTURE_CONSUMPTION;
  if (filter.consumerId) items = items.filter((r) => r.consumer_id === filter.consumerId);
  if (filter.resourceType) items = items.filter((r) => r.resource_type === filter.resourceType);
  if (filter.resourceId) items = items.filter((r) => r.resource_id === filter.resourceId);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/* ---------------------------------------------------------------------------
   Ownership assignments (P2-07).

   The three ownership wrappers were among the handful in `api.ts` with no
   fixture branch, which did not matter while the expiry banner was mounted
   nowhere. It is mounted on Stewardship now, so in fixture mode the screen
   would open with "Could not load ownerships: 401" — an error about the
   absence of a backend, rendered as though the estate had a problem.

   Two rows are deliberately shaped to exercise both sides of the banner's own
   client-side filter: one inside the 14-day window (shown) and one well
   outside it (loaded, ignored). The principal matches `identityHeaders()`'
   default so the banner's `owner_principal === CURRENT_PRINCIPAL_ID` check
   passes without the fixture having to know how that value is derived.
--------------------------------------------------------------------------- */

import type {
  OwnershipAssignmentBulkReaffirmResult,
  OwnershipAssignmentRead,
} from "./api";

const OWNERSHIP_PRINCIPAL = "local-ui-admin";

/** Days from now, as an ISO instant — the banner compares against `Date.now()`,
 *  so a fixed date would silently stop being "expiring soon". */
const inDays = (days: number): string =>
  new Date(Date.now() + days * 86_400_000).toISOString();

const FIXTURE_OWNERSHIP_ASSIGNMENTS: OwnershipAssignmentRead[] = [
  {
    id: "own_orders_raw", organization_id: CX_ORG_ID,
    subject_type: "TABLE", subject_id: "t_000000",
    owner_type: "INDIVIDUAL", owner_principal: OWNERSHIP_PRINCIPAL,
    assignment_kind: "MANUAL", source_rule_id: null, status: "ACTIVE",
    assigned_by: "fixture-admin", expires_at: inDays(6),
    expiry_warning_emitted_at: null, reaffirmed_at: null, reaffirmed_by: null,
    created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
  },
  {
    id: "own_customer_dim", organization_id: CX_ORG_ID,
    subject_type: "TABLE", subject_id: "t_000001",
    owner_type: "INDIVIDUAL", owner_principal: OWNERSHIP_PRINCIPAL,
    assignment_kind: "RULE", source_rule_id: "rule_retail_tables", status: "ACTIVE",
    assigned_by: "fixture-admin", expires_at: inDays(11),
    expiry_warning_emitted_at: null, reaffirmed_at: null, reaffirmed_by: null,
    created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
  },
  {
    // Outside the warning window: loaded, and correctly not warned about.
    id: "own_treasury_snapshot", organization_id: CX_ORG_ID,
    subject_type: "TABLE", subject_id: "t_000002",
    owner_type: "GROUP", owner_principal: OWNERSHIP_PRINCIPAL,
    assignment_kind: "MANUAL", source_rule_id: null, status: "ACTIVE",
    assigned_by: "fixture-admin", expires_at: inDays(120),
    expiry_warning_emitted_at: null, reaffirmed_at: null, reaffirmed_by: null,
    created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
  },
];

export async function makeFixtureOwnershipAssignments(
  query: { limit?: number; offset?: number },
): Promise<PageOf<OwnershipAssignmentRead>> {
  await wait(70);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  const items = FIXTURE_OWNERSHIP_ASSIGNMENTS.slice(offset, offset + limit);
  return { items, limit, offset, total: FIXTURE_OWNERSHIP_ASSIGNMENTS.length };
}

/** Extends `expires_at` the way the server does (`ownership_reaffirm_days`,
 *  default 180) so the banner's post-action refresh genuinely drops the row
 *  rather than re-rendering it unchanged. */
export async function makeFixtureReaffirmOwnershipAssignment(
  assignmentId: string,
): Promise<OwnershipAssignmentRead> {
  await wait(80);
  const row = FIXTURE_OWNERSHIP_ASSIGNMENTS.find((item) => item.id === assignmentId);
  if (!row) throw new ApiError(404, "ownership assignment not found");
  row.expires_at = inDays(180);
  row.reaffirmed_at = new Date().toISOString();
  row.reaffirmed_by = OWNERSHIP_PRINCIPAL;
  return { ...row };
}

export async function makeFixtureBulkReaffirmOwnershipAssignments(
  assignmentIds: string[],
): Promise<OwnershipAssignmentBulkReaffirmResult> {
  await wait(110);
  const items = assignmentIds.map((assignment_id) => {
    const row = FIXTURE_OWNERSHIP_ASSIGNMENTS.find((item) => item.id === assignment_id);
    if (!row) {
      return { assignment_id, outcome: "NOT_FOUND" as const, detail: "no such assignment" };
    }
    row.expires_at = inDays(180);
    row.reaffirmed_at = new Date().toISOString();
    row.reaffirmed_by = OWNERSHIP_PRINCIPAL;
    return { assignment_id, outcome: "REAFFIRMED" as const, detail: null };
  });
  const reaffirmed = items.filter((item) => item.outcome === "REAFFIRMED").length;
  return { reaffirmed, skipped: items.length - reaffirmed, items };
}

/* ---------------------------------------------------------------------------
   Glossary terms and asset-term links (P1-03).

   `_api_append.ts` declared `USE_FIXTURES` and then used it only to decide
   whether to send identity headers, so every glossary call went to the live
   API even in fixture mode — where it 401s. That was invisible while the only
   caller was Business meaning's optional side panel. The Context Products
   picker reads approved terms too, so the gap now shows as a failed picker on
   a screen whose whole point is composing approved references.

   Kept in this file rather than in `_api_append.ts` so all fixture state for
   the app lives in one place, matching every other slice.
--------------------------------------------------------------------------- */

import type { AssetTermLinkRead, GlossaryTermVersionRead } from "./types";

const FIXTURE_GLOSSARY_TERMS: GlossaryTermVersionRead[] = [
  {
    id: "gtv_delinquency_2", organization_id: CX_ORG_ID, term_id: "gt_delinquency",
    term_key: "delinquency", category_id: "cat_risk", lifecycle_status: "ACTIVE",
    version: 2, status: "APPROVED",
    display_name: "Delinquency",
    definition: "An account is delinquent once a contractual payment is 30 or more days past due.",
    synonyms: ["past due", "arrears"], owner_principal: "risk-data-stewards",
    created_by: "risk-data-stewards@tenant.example", approved_by: "steward@tenant.example",
    approved_at: "2026-06-01T00:00:00Z",
    created_at: "2026-05-01T00:00:00Z", updated_at: "2026-06-01T00:00:00Z",
  },
  {
    id: "gtv_exposure_at_default_1", organization_id: CX_ORG_ID, term_id: "gt_ead",
    term_key: "exposure_at_default", category_id: "cat_risk", lifecycle_status: "ACTIVE",
    version: 1, status: "APPROVED",
    display_name: "Exposure at Default",
    definition: "The gross amount expected to be outstanding at the time a counterparty defaults.",
    synonyms: ["EAD"], owner_principal: "risk-data-stewards",
    created_by: "risk-data-stewards@tenant.example", approved_by: "steward@tenant.example",
    approved_at: "2026-04-11T00:00:00Z",
    created_at: "2026-04-01T00:00:00Z", updated_at: "2026-04-11T00:00:00Z",
  },
  {
    id: "gtv_net_revenue_1", organization_id: CX_ORG_ID, term_id: "gt_net_revenue",
    term_key: "net_revenue", category_id: "cat_finance", lifecycle_status: "ACTIVE",
    version: 1, status: "APPROVED",
    display_name: "Net Revenue",
    definition: "Gross revenue less returns, allowances and interchange paid.",
    synonyms: [], owner_principal: "finance-data",
    created_by: "finance-data@tenant.example", approved_by: "steward@tenant.example",
    approved_at: "2026-02-20T00:00:00Z",
    created_at: "2026-02-01T00:00:00Z", updated_at: "2026-02-20T00:00:00Z",
  },
  {
    // A draft, so a caller filtering on `status` gets a genuinely different
    // answer from a caller that does not.
    id: "gtv_settlement_date_1", organization_id: CX_ORG_ID, term_id: "gt_settlement_date",
    term_key: "settlement_date", category_id: "cat_finance", lifecycle_status: "ACTIVE",
    version: 1, status: "DRAFT",
    display_name: "Settlement Date",
    definition: "The date on which a trade's cash and securities legs are exchanged.",
    synonyms: [], owner_principal: "treasury-ops",
    created_by: "treasury-ops@tenant.example", approved_by: null, approved_at: null,
    created_at: "2026-08-20T00:00:00Z", updated_at: "2026-08-20T00:00:00Z",
  },
];

const FIXTURE_ASSET_TERM_LINKS: AssetTermLinkRead[] = [
  {
    id: "atl_1", organization_id: CX_ORG_ID, table_id: "t_000000",
    term_id: "gt_ead", term_key: "exposure_at_default", display_name: "Exposure at Default",
    definition: "The gross amount expected to be outstanding at the time a counterparty defaults.",
    linked_by: "risk-data-stewards@tenant.example", link_type: "MANUAL",
    confidence: 1, source_annotation_id: null, created_at: "2026-06-02T00:00:00Z",
  },
];

export async function makeFixtureGlossaryTerms(query: {
  status?: string;
  q?: string;
  limit?: number;
  offset?: number;
}): Promise<{ items: GlossaryTermVersionRead[]; limit: number; offset: number; total: number }> {
  await wait(70);
  let items = FIXTURE_GLOSSARY_TERMS;
  if (query.status) items = items.filter((t) => t.status === query.status);
  if (query.q) {
    const needle = query.q.toLowerCase();
    items = items.filter(
      (t) =>
        t.term_key.toLowerCase().includes(needle) ||
        t.display_name.toLowerCase().includes(needle),
    );
  }
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

export async function makeFixtureCreateGlossaryTerm(
  body: { term_key: string; display_name: string; definition: string; category_id?: string | null; synonyms?: string[]; owner_principal?: string | null },
): Promise<GlossaryTermVersionRead> {
  await wait(90);
  if (FIXTURE_GLOSSARY_TERMS.some((t) => t.term_key === body.term_key)) {
    throw new ApiError(409, `glossary term '${body.term_key}' already exists`);
  }
  const now = new Date().toISOString();
  const created: GlossaryTermVersionRead = {
    id: `gtv_${body.term_key}_1`, organization_id: CX_ORG_ID,
    term_id: `gt_${body.term_key}`, term_key: body.term_key,
    category_id: body.category_id ?? null, lifecycle_status: "ACTIVE",
    version: 1, status: "DRAFT",
    display_name: body.display_name, definition: body.definition,
    synonyms: body.synonyms ?? [], owner_principal: body.owner_principal ?? null,
    created_by: "local-ui-admin", approved_by: null, approved_at: null,
    created_at: now, updated_at: now,
  };
  FIXTURE_GLOSSARY_TERMS.push(created);
  return { ...created };
}

export async function makeFixtureSubmitGlossaryTermVersion(
  versionId: string,
): Promise<GovernanceReviewRead> {
  await wait(80);
  const version = FIXTURE_GLOSSARY_TERMS.find((t) => t.id === versionId);
  if (!version) throw new ApiError(404, "glossary term version not found");
  if (version.status !== "DRAFT") {
    throw new ApiError(409, "only a draft glossary term version can be submitted");
  }
  version.status = "REVIEW_REQUIRED";
  const now = new Date().toISOString();
  return {
    id: `rev_${versionId}`, organization_id: CX_ORG_ID,
    object_type: "GLOSSARY_TERM_VERSION", object_id: versionId,
    requested_action: "PUBLISH", status: "PENDING",
    requested_by: "local-ui-admin", decided_by: null, decision_reason: null,
    decided_at: null, created_at: now, updated_at: now,
  };
}

export async function makeFixtureLinkTermToTable(
  tableId: string,
  termId: string,
): Promise<AssetTermLinkRead> {
  await wait(80);
  const term = FIXTURE_GLOSSARY_TERMS.find((t) => t.term_id === termId);
  if (!term) throw new ApiError(404, "glossary term not found");
  if (term.status !== "APPROVED") {
    throw new ApiError(409, "only approved glossary terms can be linked to an asset");
  }
  const created: AssetTermLinkRead = {
    id: `atl_${tableId}_${termId}`, organization_id: CX_ORG_ID,
    table_id: tableId, term_id: term.term_id, term_key: term.term_key,
    display_name: term.display_name, definition: term.definition,
    linked_by: "local-ui-admin", link_type: "MANUAL",
    confidence: 1, source_annotation_id: null, created_at: new Date().toISOString(),
  };
  FIXTURE_ASSET_TERM_LINKS.push(created);
  return { ...created };
}

export async function makeFixtureUnlinkTermFromTable(linkId: string): Promise<void> {
  await wait(60);
  const index = FIXTURE_ASSET_TERM_LINKS.findIndex((l) => l.id === linkId);
  if (index >= 0) FIXTURE_ASSET_TERM_LINKS.splice(index, 1);
}

export async function makeFixtureAssetTermLinks(
  tableId: string,
  query: { limit?: number; offset?: number },
): Promise<{ items: AssetTermLinkRead[]; limit: number; offset: number; total: number }> {
  await wait(60);
  const matching = FIXTURE_ASSET_TERM_LINKS.filter((l) => l.table_id === tableId);
  const offset = query.offset ?? 0;
  const limit = query.limit ?? 100;
  return { items: matching.slice(offset, offset + limit), limit, offset, total: matching.length };
}
