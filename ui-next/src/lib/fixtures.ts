import type {
  AiDecisionRead,
  AnalysisRunRead,
  AssetEvidenceRead,
  BusinessMapEdgeRead,
  BusinessMapNodeRead,
  BusinessMapRead,
  ConsumerFooterRead,
  DataQualityIncidentRead,
  DataQualityIncidentTransition,
  DataQualitySummaryRead,
  DataSourceRead,
  EvidenceItemRead,
  FleetSummaryRead,
  GovernanceDecisionRequest,
  GovernanceReviewDiffRead,
  GovernanceReviewRead,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  MeRead,
  MetadataBusinessAnnotationRead,
  MetadataIngestionBatchRead,
  OutboxEventRead,
  ProjectRead,
  ReviewQueueProposalRead,
  ReviewQueueRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioDiffRead,
  StudioImpactPreview,
  UnifiedLineageImpactNodeRead,
  UnifiedLineageImpactRead,
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
  AnalysisRunsQuery,
  AuditEventQuery,
  BusinessAnnotationsQuery,
  BusinessMapQuery,
  CatalogQuery,
  IngestionBatchesQuery,
  LineageImpactQuery,
  MarketplaceQuery,
  OutboxEventsQuery,
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

  return {
    id: `t_${i.toString(36).padStart(6, "0")}`,
    name: `${domain}_${entity}${pick(SUFFIX, i, 29)}`,
    schema_name: pick(SCHEMAS, i, 31),
    datasource_name: pick(SOURCES, i, 37),
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
];

/** `GET /v1/organizations/{id}/datasources`. */
export async function makeFixtureOrgDatasources(): Promise<PageOf<DataSourceRead>> {
  await wait(60);
  return { items: FIXTURE_DATASOURCES, limit: 500, offset: 0, total: FIXTURE_DATASOURCES.length };
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
