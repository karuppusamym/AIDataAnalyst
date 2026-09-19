import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  ChangeQueueItem,
  ChangeQueuePage,
  ReviewBatch,
  ReviewBatchDecision,
  ReviewBatchMemberOutcome,
} from "../lib/api";
import { expectNoAxeViolations } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-REV01: batch review. The API boundary (`../lib/api`) is mocked, the same
   pattern ReviewQueueScreen.test.tsx uses. What is pinned: pages arrive by
   keyset cursor into a windowed list; the selection survives paging and
   carries the fingerprint each row was shown with; a row the reviewer may not
   decide cannot be selected; the frozen batch's exclusions are shown before
   deciding; the decision's partial outcomes, reasons and corrections are shown
   per member; a rejection needs a rationale.
--------------------------------------------------------------------------- */

const fetchChangeQueue = vi.fn<(query: { cursor?: string | null }) => Promise<ChangeQueuePage>>();
const fetchChangeQueueDetails = vi.fn<(ids: string[]) => Promise<{ items: unknown[] }>>();
const freezeReviewBatch = vi.fn<(items: unknown[]) => Promise<ReviewBatch>>();
const decideReviewBatch =
  vi.fn<(id: string, body: { decision: string; reason: string | null }) => Promise<ReviewBatchDecision>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchChangeQueue: (query: { cursor?: string | null }) => fetchChangeQueue(query),
    fetchChangeQueueDetails: (ids: string[]) => fetchChangeQueueDetails(ids),
    freezeReviewBatch: (items: unknown[]) => freezeReviewBatch(items),
    decideReviewBatch: (id: string, body: { decision: string; reason: string | null }) =>
      decideReviewBatch(id, body),
  };
});

function row(id: string, overrides: Partial<ChangeQueueItem> = {}): ChangeQueueItem {
  return {
    review_id: id,
    object_type: "ASSET_DESCRIPTION_DRAFT",
    object_id: `draft-${id}`,
    review_family: "DESCRIPTION",
    change_kind: "PUBLISH_DESCRIPTION",
    status: "PENDING",
    requested_by: "steward-agent",
    created_at: "2026-09-01T00:00:00Z",
    risk_tier: "T0",
    confidence: 0.8,
    diffable: false,
    evidence_count: 2,
    evidence_preview: [
      { category: "DESCRIPTION_DRAFT", claim: `proposed_description: text ${id}`, source: `s:${id}` },
    ],
    evidence_fingerprint: `fp-${id}`,
    decide_blocker: null,
    approve_gate: null,
    target_unavailable: false,
    ...overrides,
  };
}

const PAGES: Record<string, ChangeQueuePage> = {
  "": {
    organization_id: "org",
    generated_at: "2026-09-19T00:00:00Z",
    limit: 50,
    next_cursor: "c1",
    total: 6,
    items: [row("a"), row("b"), row("own", { decide_blocker: "MAKER_CHECKER" })],
  },
  c1: {
    organization_id: "org",
    generated_at: "2026-09-19T00:00:00Z",
    limit: 50,
    next_cursor: "c2",
    total: 6,
    items: [
      row("d"),
      row("conflict", {
        object_type: "GLOSSARY_CONFLICT",
        review_family: "SEMANTIC",
        evidence_count: 0,
        evidence_preview: [],
        approve_gate: "EVIDENCE_NOT_SHOWN",
      }),
    ],
  },
  c2: {
    organization_id: "org",
    generated_at: "2026-09-19T00:00:00Z",
    limit: 50,
    next_cursor: null,
    total: 6,
    items: [row("f")],
  },
};

function frozen(overrides: Partial<ReviewBatch> = {}): ReviewBatch {
  return {
    id: "batch-1",
    status: "FROZEN",
    selection_mode: "EXPLICIT",
    selection_truncated: false,
    item_count: 4,
    eligible_count: 3,
    excluded_count: 1,
    selection_fingerprint: "0".repeat(64),
    decision: null,
    exclusion_counts: { STALE_EVIDENCE: 1 },
    approve_gate_counts: { EVIDENCE_NOT_SHOWN: 1 },
    outcome_counts: { PENDING: 4 },
    ...overrides,
  };
}

function member(
  id: string,
  outcome: string,
  reasonCode: string | null,
  correction: Partial<ReviewBatchMemberOutcome["correction"]> = {},
): ReviewBatchMemberOutcome {
  return {
    review_id: id,
    position: 0,
    object_type: "ASSET_DESCRIPTION_DRAFT",
    review_family: "DESCRIPTION",
    eligibility: outcome === "SKIPPED" ? "EXCLUDED" : "ELIGIBLE",
    exclusion_code: outcome === "SKIPPED" ? reasonCode : null,
    outcome,
    reason_code: reasonCode,
    detail: null,
    correction: {
      kind: "NONE",
      available: false,
      method: null,
      path: null,
      subject_type: null,
      subject_id: null,
      reason_code: "NOT_APPLIED",
      ...correction,
    },
  };
}

async function renderScreen() {
  const { ReviewBatchQueue } = await import("./ReviewBatchQueue");
  const view = render(<ReviewBatchQueue />);
  // Every page arrives through the windowed list's reach-end callback.
  await screen.findByRole("checkbox", { name: "Select ASSET_DESCRIPTION_DRAFT draft-f" });
  return view.container;
}

beforeEach(() => {
  fetchChangeQueue.mockReset();
  fetchChangeQueueDetails.mockReset();
  freezeReviewBatch.mockReset();
  decideReviewBatch.mockReset();
  fetchChangeQueue.mockImplementation(async (query) => PAGES[query.cursor ?? ""]!);
});

describe("ReviewBatchQueue", () => {
  it("pages by cursor and keeps the selection, with the versions seen, across pages", async () => {
    const container = await renderScreen();
    expect(fetchChangeQueue.mock.calls.map(([query]) => query.cursor ?? null)).toEqual([
      null,
      "c1",
      "c2",
    ]);
    // A reviewer's own proposal cannot be selected: maker-checker, shown as such.
    expect(screen.getByRole("checkbox", { name: "Select ASSET_DESCRIPTION_DRAFT draft-own" })).toBeDisabled();
    expect(screen.getAllByText("You proposed this (maker-checker)").length).toBeGreaterThan(0);

    for (const id of ["a", "d", "f"]) {
      fireEvent.click(screen.getByRole("checkbox", { name: `Select ASSET_DESCRIPTION_DRAFT draft-${id}` }));
    }
    fireEvent.click(screen.getByRole("checkbox", { name: "Select GLOSSARY_CONFLICT draft-co…" }));
    expect(container.querySelector(".rbq__summary")).toHaveTextContent(
      "6 matching · 6 loaded · 4 selected across pages",
    );

    freezeReviewBatch.mockResolvedValue(frozen());
    fireEvent.click(screen.getByRole("button", { name: "Freeze batch of 4" }));
    await waitFor(() => expect(freezeReviewBatch).toHaveBeenCalledTimes(1));
    expect(freezeReviewBatch.mock.calls[0]![0]).toEqual([
      { review_id: "a", evidence_fingerprint: "fp-a" },
      { review_id: "d", evidence_fingerprint: "fp-d" },
      { review_id: "f", evidence_fingerprint: "fp-f" },
      { review_id: "conflict", evidence_fingerprint: "fp-conflict" },
    ]);

    // The preview says what was excluded and what can only be rejected, before deciding.
    const panel = await screen.findByRole("region", { name: "Frozen batch" });
    expect(within(panel).getByText(/Changed since you reviewed it/)).toBeInTheDocument();
    expect(within(panel).getByText(/No evidence shown/)).toBeInTheDocument();
    // Selection is locked once frozen.
    expect(screen.getByRole("checkbox", { name: "Select ASSET_DESCRIPTION_DRAFT draft-b" })).toBeDisabled();

    decideReviewBatch.mockResolvedValue({
      batch: frozen({ status: "DECIDED", decision: "APPROVE" }),
      overall: "PARTIAL_SUCCESS",
      applied_count: 1,
      refused_count: 2,
      skipped_count: 1,
      members: [
        member("a", "APPLIED", null, {
          kind: "WITHDRAW_DESCRIPTION",
          available: true,
          method: "POST",
          path: "/v1/descriptions/withdrawals",
          subject_type: "TABLE",
          subject_id: "table-a",
          reason_code: null,
        }),
        member("d", "REFUSED", "ALREADY_DECIDED"),
        member("conflict", "REFUSED", "EVIDENCE_NOT_SHOWN"),
        member("f", "SKIPPED", "STALE_EVIDENCE"),
      ],
    });
    fireEvent.click(within(panel).getByRole("button", { name: "Approve 2 eligible" }));
    await waitFor(() => expect(decideReviewBatch).toHaveBeenCalledWith("batch-1", { decision: "APPROVE", reason: null }));

    const outcome = await screen.findByRole("region", { name: "Batch outcome" });
    expect(within(outcome).getByText(/Partly applied/)).toBeInTheDocument();
    expect(within(outcome).getByText("Decided by someone else first")).toBeInTheDocument();
    expect(within(outcome).getByText("Changed since you reviewed it")).toBeInTheDocument();
    expect(within(outcome).getByText("POST /v1/descriptions/withdrawals")).toBeInTheDocument();
  });

  it("requires a rationale before rejecting a frozen batch", async () => {
    await renderScreen();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select ASSET_DESCRIPTION_DRAFT draft-a" }));
    freezeReviewBatch.mockResolvedValue(frozen({ item_count: 1, eligible_count: 1, exclusion_counts: {}, approve_gate_counts: {} }));
    fireEvent.click(screen.getByRole("button", { name: "Freeze batch of 1" }));
    const panel = await screen.findByRole("region", { name: "Frozen batch" });
    fireEvent.click(within(panel).getByRole("button", { name: "Reject batch…" }));

    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Reject batch" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "overstates the grain" } });
    decideReviewBatch.mockResolvedValue({
      batch: frozen({ status: "DECIDED", decision: "REJECT" }),
      overall: "SUCCESS",
      applied_count: 1,
      refused_count: 0,
      skipped_count: 0,
      members: [member("a", "APPLIED", null, { kind: "REPROPOSE", reason_code: "NO_REOPEN_PATH" })],
    });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(decideReviewBatch).toHaveBeenCalledWith("batch-1", { decision: "REJECT", reason: "overstates the grain" }),
    );
    const outcome = await screen.findByRole("region", { name: "Batch outcome" });
    expect(within(outcome).getByText("Rejected: propose it again to change it")).toBeInTheDocument();
  });

  it("loads a row's full evidence only when it is opened", async () => {
    await renderScreen();
    expect(fetchChangeQueueDetails).not.toHaveBeenCalled();
    fetchChangeQueueDetails.mockResolvedValue({
      items: [
        {
          item: PAGES[""]!.items[0],
          evidence: [
            { category: "DESCRIPTION_DRAFT", claim: "proposed_description: text a", source: "s:a" },
            { category: "DESCRIPTION_DRAFT", claim: "column_count: 3", source: "s:a.evidence" },
          ],
        },
      ],
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Show all 2 evidence items" })[0]!);
    await screen.findByText("column_count: 3");
    expect(fetchChangeQueueDetails).toHaveBeenCalledWith(["a"]);
  });

  it("has no WCAG A/AA violations with a selection and a frozen batch on screen", async () => {
    const container = await renderScreen();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select ASSET_DESCRIPTION_DRAFT draft-a" }));
    freezeReviewBatch.mockResolvedValue(frozen());
    fireEvent.click(screen.getByRole("button", { name: "Freeze batch of 1" }));
    await screen.findByRole("region", { name: "Frozen batch" });
    await expectNoAxeViolations(container);
  });

  it("says why the queue did not load", async () => {
    fetchChangeQueue.mockRejectedValue(new Error("INVALID_CURSOR"));
    const { ReviewBatchQueue } = await import("./ReviewBatchQueue");
    render(<ReviewBatchQueue />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The change queue could not be loaded");
    expect(alert).toHaveTextContent("INVALID_CURSOR");
  });
});
