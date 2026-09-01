import type {
  CatalogAssetEvidence,
  CatalogRowRead,
  CertificationStatus,
  CursorPage,
  MeRead,
  QualityState,
} from "./types";
import type { CatalogQuery } from "./api";

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

export async function makeFixtureEvidence(tableId: string): Promise<CatalogAssetEvidence> {
  const i = parseInt(tableId.replace("t_", ""), 36);
  const row = rowAt(i);
  await new Promise((r) => setTimeout(r, 70));

  const items: CatalogAssetEvidence["items"] = [
    {
      label: "Structure",
      value: `${8 + (h(i, 71) % 40)} columns · fingerprint ${row.id.slice(2, 8)}`,
      source: "connector discovery · certified adapter",
      kind: "info",
    },
    {
      label: "Description",
      value: row.description
        ? row.description_is_proposed
          ? "Model-proposed, awaiting steward approval"
          : "Approved by steward"
        : "None",
      source: row.description_is_proposed
        ? "semantic_inference.py · ADR-0001: proposal only"
        : "governance review · maker-checker",
      kind: row.description ? (row.description_is_proposed ? "warn" : "ok") : "bad",
    },
    {
      label: "Certification",
      value:
        row.certification === "CERTIFIED"
          ? `Certified, expires ${row.certification_expires_at?.slice(0, 10)}`
          : row.certification === "EXPIRED"
            ? "Certification lapsed"
            : row.certification === "REVOKED"
              ? "Revoked after review"
              : "Never certified",
      source: "GL-5 bulk certification lifecycle",
      kind:
        row.certification === "CERTIFIED" ? "ok" : row.certification === "NONE" ? "info" : "bad",
    },
    {
      label: "Quality",
      value:
        row.quality === "PASSING"
          ? "All checks passing"
          : row.quality === "INCIDENT_OPEN"
            ? "Open incident — freshness threshold breached"
            : row.quality === "STALE"
              ? "No observation in 14 days"
              : "No checks configured",
      source: "data_quality.py · ADR-0016 fails closed",
      kind: row.quality === "PASSING" ? "ok" : row.quality === "UNKNOWN" ? "info" : "bad",
    },
    {
      label: "Ownership",
      value: row.owner ?? "Unowned",
      source: row.owner ? "GL-2 ownership lifecycle" : "GL-6 unowned-asset backlog",
      kind: row.owner ? "ok" : "warn",
    },
    {
      label: "Used by",
      value: `${h(i, 73) % 40} tools · ${h(i, 79) % 12} context products`,
      source: "consumption_lineage.py · CX-4",
      kind: "info",
    },
  ];

  if (row.quality === "INCIDENT_OPEN") {
    items.push({
      label: "Agent behaviour",
      value: "Declined for analytical use while the incident is open",
      source: "ai_decision_lineage.py · LN-3 refusal edge",
      kind: "bad",
    });
  }

  return { table_id: tableId, items };
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
