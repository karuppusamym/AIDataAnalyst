import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  DataSourceRead,
  RelationshipCandidateBulkDecisionResultRead,
  RelationshipCandidateRead,
  RelationshipCandidateReviewQueueRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-16: mocks the API boundary (`../lib/api`), the same pattern
   `ReviewQueueScreen.test.tsx` establishes for its own maker-checker screen
   — real payload shapes, asserting the exact endpoint/args called rather
   than a superficial snapshot.
--------------------------------------------------------------------------- */

const fetchOrgDatasources =
  vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchRelationshipCandidateReviewQueue = vi.fn<
  (datasourceId: string, query: unknown, signal?: AbortSignal) => Promise<RelationshipCandidateReviewQueueRead>
>();
const decideRelationshipCandidate = vi.fn<
  (candidateId: string, body: unknown, signal?: AbortSignal) => Promise<RelationshipCandidateRead>
>();
const bulkDecideRelationshipCandidates = vi.fn<
  (body: unknown, signal?: AbortSignal) => Promise<RelationshipCandidateBulkDecisionResultRead>
>();
const fetchRelationshipCandidateCalibration = vi.fn<
  (datasourceId: string | null, signal?: AbortSignal) => Promise<unknown>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
    fetchRelationshipCandidateReviewQueue: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchRelationshipCandidateReviewQueue(datasourceId, query, signal),
    decideRelationshipCandidate: (candidateId: string, body: unknown, signal?: AbortSignal) =>
      decideRelationshipCandidate(candidateId, body, signal),
    bulkDecideRelationshipCandidates: (body: unknown, signal?: AbortSignal) =>
      bulkDecideRelationshipCandidates(body, signal),
    fetchRelationshipCandidateCalibration: (datasourceId: string | null, signal?: AbortSignal) =>
      fetchRelationshipCandidateCalibration(datasourceId, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1",
  organization_id: "org1",
  line_of_business_id: "lob1",
  data_domain_id: "dom1",
  project_id: "proj1",
  name: "snowflake_prod",
  connector_type: "SNOWFLAKE",
  dialect: "snowflake",
  environment: "PRODUCTION",
  credential_reference: "vault://x",
  status: "ACTIVE",
  capabilities: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function candidate(id: string, overrides: Partial<RelationshipCandidateRead> = {}): RelationshipCandidateRead {
  return {
    id,
    organization_id: "org1",
    datasource_id: "ds_1",
    target_datasource_id: "ds_1",
    source_table_id: `${id}_st`,
    source_column_id: `${id}_sc`,
    target_table_id: `${id}_tt`,
    target_column_id: `${id}_tc`,
    detection_rule: "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
    confidence: 0.9,
    evidence: {},
    status: "PENDING",
    created_by: "relationship_discovery_agent",
    reviewed_by: null,
    review_reason: null,
    reviewed_at: null,
    created_at: "2026-08-28T00:00:00Z",
    updated_at: "2026-08-28T00:00:00Z",
    ...overrides,
  };
}

function reviewItem(
  id: string,
  edge: { sourceTable: string; sourceColumn: string; targetTable: string; targetColumn: string },
  impactScore: number,
  candidateOverrides: Partial<RelationshipCandidateRead> = {},
): RelationshipCandidateReviewQueueRead["items"][number] {
  return {
    candidate: candidate(id, candidateOverrides),
    diff: [
      { field: "confidence", change: "added", after: candidateOverrides.confidence ?? 0.9 },
      {
        field: "confidence_signals",
        change: "added",
        after: [
          { name: "primary_key_target", score: 0.7, maximum: 0.7, reason: "target column is a declared PRIMARY KEY" },
          { name: "column_name_match", score: 0.1, maximum: 0.1, reason: "exact, case-insensitive name match" },
        ],
      },
      { field: "detection_rule", change: "added", after: "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1" },
      { field: "source_column", change: "added", after: edge.sourceColumn },
      { field: "source_table", change: "added", after: edge.sourceTable },
      { field: "target_column", change: "added", after: edge.targetColumn },
      { field: "target_table", change: "added", after: edge.targetTable },
    ],
    impact: {
      impact_score: impactScore,
      source_table_impact: Math.floor(impactScore / 2),
      target_table_impact: Math.ceil(impactScore / 2),
      depth: 3,
      node_limit: 100,
      truncated: false,
    },
  };
}

function queueOf(items: RelationshipCandidateReviewQueueRead["items"]): RelationshipCandidateReviewQueueRead {
  return {
    datasource_id: "ds_1",
    items,
    limit: 200,
    offset: 0,
    scanned_count: items.length,
    total_pending_count: items.length,
    truncated: false,
  };
}

// Deliberately NOT sorted by confidence or id — "low_impact" has the
// highest confidence (0.95) and would sort first if the screen re-sorted
// client-side. The fixture-independent test below asserts it stays last.
const HIGH_IMPACT = reviewItem(
  "rc_high",
  { sourceTable: "core.orders_raw", sourceColumn: "customer_id", targetTable: "core.customer_dim", targetColumn: "customer_id" },
  138,
  { confidence: 0.75 },
);
const MID_IMPACT = reviewItem(
  "rc_mid",
  { sourceTable: "core.settlement", sourceColumn: "counterparty_id", targetTable: "core.counterparty_dim", targetColumn: "counterparty_id" },
  47,
  { confidence: 0.9 },
);
const LOW_IMPACT = reviewItem(
  "rc_low",
  { sourceTable: "core.limit_util", sourceColumn: "acct_no", targetTable: "core.account_dim", targetColumn: "account_id" },
  3,
  { confidence: 0.95 },
);

async function loadScreen() {
  const { RelationshipsScreen } = await import("./RelationshipsScreen");
  return RelationshipsScreen;
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  fetchRelationshipCandidateReviewQueue.mockReset();
  decideRelationshipCandidate.mockReset();
  bulkDecideRelationshipCandidates.mockReset();
  fetchRelationshipCandidateCalibration.mockReset();
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([]));
  fetchRelationshipCandidateCalibration.mockResolvedValue({
    datasource_id: null,
    bucket_width: 0.1,
    total_decided: 0,
    ground_truth_overrides_applied: 0,
    methodology_note: "",
    buckets: [],
  });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function pickDatasource() {
  const select = await screen.findByLabelText("Datasource");
  fireEvent.change(select, { target: { value: "ds_1" } });
}

describe("RelationshipsScreen", () => {
  it("shows a picker prompt with no datasource selected and does not fetch the queue", async () => {
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);

    await waitFor(() => expect(screen.getByText("Pick a datasource")).toBeInTheDocument());
    expect(fetchRelationshipCandidateReviewQueue).not.toHaveBeenCalled();
  });

  it("picking a datasource loads the review queue in the real impact order the API returns, unsorted client-side", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT, MID_IMPACT, LOW_IMPACT]));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);

    await pickDatasource();

    expect(fetchRelationshipCandidateReviewQueue).toHaveBeenCalledWith(
      "ds_1",
      expect.objectContaining({ limit: 200 }),
      expect.anything(),
    );

    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    const titles = screen.getAllByRole("button", { name: /→/ }).map((el) => el.textContent);
    // Impact-descending (138, 47, 3) even though confidence ascends the
    // opposite way (0.75, 0.9, 0.95) — proof there is no client-side
    // confidence or id re-sort hiding the API's real order.
    expect(titles).toEqual([
      "core.orders_raw.customer_id → core.customer_dim.customer_id",
      "core.settlement.counterparty_id → core.counterparty_dim.counterparty_id",
      "core.limit_util.acct_no → core.account_dim.account_id",
    ]);
  });

  it("approving one candidate calls the single-decision endpoint with its id and APPROVE", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    decideRelationshipCandidate.mockResolvedValue(candidate("rc_high", { status: "APPROVED" }));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();

    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    screen.getAllByRole("button", { name: "Approve" })[0]!.click();

    await waitFor(() =>
      expect(decideRelationshipCandidate).toHaveBeenCalledWith(
        "rc_high",
        { decision: "APPROVE", reason: null },
        undefined,
      ),
    );
    expect(decideRelationshipCandidate).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(fetchRelationshipCandidateReviewQueue).toHaveBeenCalledTimes(2));
  });

  it("requires a reason before calling the endpoint on reject, and skips the call if none is given", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    vi.spyOn(window, "prompt").mockReturnValue(null);
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();
    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    screen.getAllByRole("button", { name: "Reject" })[0]!.click();

    expect(window.prompt).toHaveBeenCalled();
    expect(decideRelationshipCandidate).not.toHaveBeenCalled();
  });

  it("a bulk-selected set calls the bulk-decision endpoint once with every selected id, not N single calls", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT, MID_IMPACT, LOW_IMPACT]));
    bulkDecideRelationshipCandidates.mockResolvedValue({
      decision: "APPROVE",
      selection_mode: "EXPLICIT",
      requested_count: 2,
      succeeded_count: 2,
      failed_count: 0,
      truncated: false,
      results: [
        { candidate_id: "rc_high", status: "SUCCEEDED" },
        { candidate_id: "rc_mid", status: "SUCCEEDED" },
      ],
    });
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();
    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByLabelText(/Select core\.orders_raw\.customer_id/));
    fireEvent.click(screen.getByLabelText(/Select core\.settlement\.counterparty_id/));

    screen.getByRole("button", { name: "Approve selected" }).click();

    await waitFor(() =>
      expect(bulkDecideRelationshipCandidates).toHaveBeenCalledWith(
        { candidate_ids: ["rc_high", "rc_mid"], decision: "APPROVE", reason: null },
        undefined,
      ),
    );
    expect(bulkDecideRelationshipCandidates).toHaveBeenCalledTimes(1);
    expect(decideRelationshipCandidate).not.toHaveBeenCalled();
    await waitFor(() => expect(fetchRelationshipCandidateReviewQueue).toHaveBeenCalledTimes(2));
  });

  it("opens a permalinkable detail panel showing the diff and confidence-signal breakdown", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();
    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    screen.getByRole("button", { name: "core.orders_raw.customer_id → core.customer_dim.customer_id" }).click();

    const panel = await screen.findByLabelText("Candidate detail");
    expect(panel).toBeInTheDocument();
    expect(screen.getAllByText(/primary key target/i).length).toBeGreaterThan(0);
    expect(new URLSearchParams(location.search).get("candidate")).toBe("rc_high");
  });

  it("shows an empty state when the datasource has nothing pending", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([]));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();

    await waitFor(() => expect(screen.getByText("Nothing pending")).toBeInTheDocument());
  });

  it("shows an error state with retry when the queue fails to load", async () => {
    const { ApiError } = await import("../lib/api");
    fetchRelationshipCandidateReviewQueue.mockRejectedValue(new ApiError(500, "boom"));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();

    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument());
  });
});
