import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  DataSourceRead,
  RelationshipCandidateBulkDecisionResultRead,
  RelationshipCandidateRead,
  RelationshipCandidateReviewQueueRead,
  RelationshipValidationRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-16: mocks the API boundary (`../lib/api`), the same pattern
   `ReviewQueueScreen.test.tsx` establishes for its own maker-checker screen
   — real payload shapes, asserting the exact endpoint/args called rather
   than a superficial snapshot.
--------------------------------------------------------------------------- */

const listOrgDatasources =
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
/* R11-C2: the decision-history section's own read. It was NOT mocked, so it
 * fell through `...actual` to the real client and answered from the bundled
 * demo estate. The screen's "lazy-loads decided candidates on expand" test
 * then raced that unmocked round trip against a 1000ms `waitFor` and failed
 * intermittently -- in a full parallel run, never on its own. Mocking it
 * makes the section's empty state a fact of the test rather than a fact of
 * how loaded the machine is. */
const fetchRelationshipCandidates = vi.fn<
  (datasourceId: string, opts?: { status?: string }, signal?: AbortSignal) => Promise<PageOf<RelationshipCandidateRead>>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      listOrgDatasources(organizationId, signal),
    fetchRelationshipCandidateReviewQueue: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchRelationshipCandidateReviewQueue(datasourceId, query, signal),
    decideRelationshipCandidate: (candidateId: string, body: unknown, signal?: AbortSignal) =>
      decideRelationshipCandidate(candidateId, body, signal),
    bulkDecideRelationshipCandidates: (body: unknown, signal?: AbortSignal) =>
      bulkDecideRelationshipCandidates(body, signal),
    fetchRelationshipCandidateCalibration: (datasourceId: string | null, signal?: AbortSignal) =>
      fetchRelationshipCandidateCalibration(datasourceId, signal),
    fetchRelationshipCandidates: (datasourceId: string, opts?: { status?: string }, signal?: AbortSignal) =>
      fetchRelationshipCandidates(datasourceId, opts, signal),
    fetchRelationshipCandidateValidation: (candidateId: string, signal?: AbortSignal) =>
      fetchRelationshipCandidateValidation(candidateId, signal),
  };
});

/* R11-FP06: the detail panel's join validation. Mocked for the same reason as the
 * decision history above -- left to `...actual` it would race the bundled demo estate. */
const fetchRelationshipCandidateValidation = vi.fn<
  (candidateId: string, signal?: AbortSignal) => Promise<RelationshipValidationRead>
>();

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

function validationOf(
  id: string,
  overrides: Partial<RelationshipValidationRead> = {},
): RelationshipValidationRead {
  return {
    subject_type: "RELATIONSHIP_CANDIDATE",
    subject_id: id,
    status: "PENDING",
    validation_version: "relationship-validation-v1",
    outcome: "CORROBORATED",
    approvable: true,
    evidence_classes: [
      {
        name: "DECLARED_KEY",
        corroborating: true,
        detail: "The target columns are unique by a declared primary or unique key.",
        sample_bounded: false,
      },
      { name: "NAME_MATCH", corroborating: false, detail: "Column names match exactly.", sample_bounded: false },
    ],
    source_key_columns: ["customer_id"],
    target_key_columns: ["customer_id"],
    join_condition: "source.customer_id = target.customer_id",
    cardinality: "MANY_TO_ONE",
    direction: "SOURCE_REFERENCES_TARGET",
    source_uniqueness: { unique: false, basis: null, sample_bounded: false },
    target_uniqueness: { unique: true, basis: "DECLARED_KEY", sample_bounded: false },
    referencing_side: "SOURCE",
    optionality: "OPTIONAL",
    optionality_columns: [
      { column_name: "customer_id", declared_nullable: true, observed_null_count: 12, observed_non_null_count: 988 },
    ],
    source_observation: {
      table_profile_id: "tp_1",
      profiled_at: "2026-09-14T00:00:00Z",
      sampled_row_count: 1000,
      row_count_estimate: 250000,
      scope: "SAMPLE",
    },
    target_observation: null,
    inclusion_check_status: "NOT_RUN",
    inclusion_check_reason:
      "Checking that every referencing value exists on the key side needs a query against the source, and validation runs none.",
    grain_warnings: [],
    source_queries_executed: 0,
    values_inspected: false,
    fingerprint: "f1",
    recorded_fingerprint: null,
    recorded_at: null,
    drift: "NOT_RECORDED",
    ...overrides,
  };
}

beforeEach(() => {
  fetchRelationshipCandidateValidation.mockReset();
  fetchRelationshipCandidateValidation.mockResolvedValue(validationOf("rc_high"));
  listOrgDatasources.mockReset();
  fetchRelationshipCandidateReviewQueue.mockReset();
  decideRelationshipCandidate.mockReset();
  bulkDecideRelationshipCandidates.mockReset();
  fetchRelationshipCandidateCalibration.mockReset();
  fetchRelationshipCandidates.mockReset();
  listOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([]));
  fetchRelationshipCandidates.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
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

  it("offers a decision-history section that lazy-loads decided candidates on expand", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();

    // The section is present but not yet loaded (lazy).
    const summary = await screen.findByText("Decision history");
    fireEvent.click(summary);

    // ds_1 has no decided candidates, so the section resolves to its empty
    // state rather than staying blank. `findByText` and not
    // `waitFor(getByText)`: the element appears only after the expand's own
    // fetch resolves, and the awaited finder is the form that says so.
    expect(await screen.findByText("No decisions yet")).toBeInTheDocument();
    expect(fetchRelationshipCandidates).toHaveBeenCalledWith(
      "ds_1",
      { status: "APPROVED" },
      undefined,
    );
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

  /* The rationale moved out of `window.prompt` and into a focus-trapped
     dialog (review 2026-09-05, F21): a blocked prompt returned `null`, which
     was indistinguishable from "the reviewer cancelled", so the decision was
     dropped without anyone being told. */
  it("will not submit a rejection until a rationale is typed into the dialog", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();
    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    screen.getAllByRole("button", { name: "Reject" })[0]!.click();

    const dialog = await screen.findByRole("dialog", { name: "Reject this relationship" });
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(decideRelationshipCandidate).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Coincidental name match across unrelated domains." },
    });
    within(dialog).getByRole("button", { name: "Reject" }).click();

    await waitFor(() =>
      expect(decideRelationshipCandidate).toHaveBeenCalledWith(
        "rc_high",
        { decision: "REJECT", reason: "Coincidental name match across unrelated domains." },
        undefined,
      ),
    );
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

  /* R11-FP06: the panel shows what supports the join, and says before the reviewer tries
     that an approval resting on names alone will be refused. */
  it("shows the join validation in the detail panel and warns that a name-only approval will be refused", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    fetchRelationshipCandidateValidation.mockResolvedValue(
      validationOf("rc_high", {
        outcome: "NAME_MATCH_ONLY",
        approvable: false,
        evidence_classes: [
          { name: "NAME_MATCH", corroborating: false, detail: "Column names match exactly.", sample_bounded: false },
        ],
        cardinality: "UNKNOWN",
        direction: "UNDETERMINED",
        target_uniqueness: { unique: false, basis: null, sample_bounded: false },
        grain_warnings: ["FAN_OUT_POSSIBLE"],
      }),
    );
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();
    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    screen.getByRole("button", { name: "core.orders_raw.customer_id → core.customer_dim.customer_id" }).click();

    const section = await screen.findByLabelText("Join validation");
    expect(await within(section).findByText("name match only")).toBeInTheDocument();
    expect(within(section).getByText(/Approval will be refused/)).toBeInTheDocument();
    expect(within(section).getByText(/fan out possible/)).toBeInTheDocument();
    expect(within(section).getByText(/sampled 1000 of ~250000 rows/)).toBeInTheDocument();
    expect(within(section).getByText(/Referential inclusion not checked/)).toBeInTheDocument();
    expect(fetchRelationshipCandidateValidation).toHaveBeenCalledWith("rc_high", expect.anything());
  });

  it("shows the server's refusal when an approval rests on a name match alone", async () => {
    fetchRelationshipCandidateReviewQueue.mockResolvedValue(queueOf([HIGH_IMPACT]));
    const { ApiError } = await import("../lib/api");
    decideRelationshipCandidate.mockRejectedValue(
      new ApiError(409, "This join rests only on matching column names and types.", {
        details: { code: "RELATIONSHIP_NAME_MATCH_ONLY" },
      }),
    );
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();
    await waitFor(() =>
      expect(screen.getByText("core.orders_raw.customer_id → core.customer_dim.customer_id")).toBeInTheDocument(),
    );

    screen.getAllByRole("button", { name: "Approve" })[0]!.click();

    expect(
      await screen.findByText(/Decision was not fully recorded: This join rests only on matching column names/),
    ).toBeInTheDocument();
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

/* ---------------------------------------------------------------------------
   R11-S13 (items 15/17) — a contextual link to the OTHER queue, not a merge.

   The review's M2 merge of this queue with Cross-source was declined: the two
   are scoped by different authorizations (datasource read vs ADR-0017 domain
   grants) and own different writes (bulk decision here, cross-domain
   discovery there). What the steward was missing was the way across. These
   pin that the way across exists and that it carries the scope the target
   reads -- the selected source's domain -- rather than this queue's own.
--------------------------------------------------------------------------- */
describe("the way across to the cross-source queue", () => {
  it("opens Cross-source on the selected source's own domain", async () => {
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await pickDatasource();

    const related = within(await screen.findByRole("navigation", { name: "Related queue" }));
    fireEvent.click(
      await related.findByRole("button", { name: "Cross-source candidates in this source's domain" }),
    );

    await waitFor(() => expect(location.hash).toBe("#/steward/cross-source"));
    const params = new URLSearchParams(location.search);
    expect(params.get("dom")).toBe("dom1");
    // Cross-source is scoped by domain and does not read `ds`; carrying it
    // would put a field in the URL that the target silently ignores.
    expect(params.get("ds")).toBeNull();
  });

  it("offers the unscoped link before a source is chosen", async () => {
    const RelationshipsScreen = await loadScreen();
    render(<RelationshipsScreen />);
    await waitFor(() => expect(screen.getByText("Pick a datasource")).toBeInTheDocument());

    const related = within(screen.getByRole("navigation", { name: "Related queue" }));
    fireEvent.click(related.getByRole("button", { name: "Cross-source candidates" }));

    await waitFor(() => expect(location.hash).toBe("#/steward/cross-source"));
    expect(new URLSearchParams(location.search).get("dom")).toBeNull();
  });
});
