import type {
  AiDecisionRead,
  AssetEvidenceRead,
  DataSourceRead,
  EvidenceItemRead,
  GovernanceDecisionRequest,
  GovernanceReviewDiffRead,
  GovernanceReviewRead,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  MeRead,
  ReviewQueueProposalRead,
  ReviewQueueRead,
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioDiffRead,
  StudioImpactPreview,
  UnifiedLineageImpactNodeRead,
  UnifiedLineageImpactRead,
} from "./types";
import type {
  CatalogRowRead,
  CertificationStatus,
  CursorPage,
  MarketplaceProductRead,
  PageOf,
  QualityState,
} from "./ui-types";
import type {
  CatalogQuery,
  LineageImpactQuery,
  MarketplaceQuery,
  ReviewQueueQuery,
  StudioChangeSetQuery,
} from "./api";

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
