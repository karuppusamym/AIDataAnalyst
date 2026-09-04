import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { AssetDescriptionDraftRead, GovernanceReviewRead } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   P1-04: DescriptionDraftsScreen tests. Mocks the api.ts boundary the same
   way every other UX-15 screen test does
   (QualityScreen.test.tsx / AccessPolicyScreen.test.tsx). No live API is hit;
   the point is to prove the screen calls the right function with the right
   arguments and renders what the server would send back.
--------------------------------------------------------------------------- */

const listAssetDescriptionDrafts = vi.fn<
  (
    organizationId: string,
    filters: unknown,
    signal?: AbortSignal,
  ) => Promise<{
    drafts: AssetDescriptionDraftRead[];
    limit: number;
    offset: number;
    total: number;
    next_cursor?: string;
  }>
>();

const submitAssetDescriptionDraft = vi.fn<
  (draftId: string, signal?: AbortSignal) => Promise<GovernanceReviewRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    // Forwarded with a spread so the spy records exactly the arguments the
    // caller passed. Naming the parameters and re-passing them appended an
    // explicit `undefined` for every omitted optional, which made
    // `toHaveBeenCalledWith("d1")` fail against a one-argument call.
    listAssetDescriptionDrafts: (...args: unknown[]) =>
      (listAssetDescriptionDrafts as (...a: unknown[]) => unknown)(...args),
    submitAssetDescriptionDraft: (...args: unknown[]) =>
      (submitAssetDescriptionDraft as (...a: unknown[]) => unknown)(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

function makeDraft(overrides: Partial<AssetDescriptionDraftRead> = {}): AssetDescriptionDraftRead {
  return {
    id: overrides.id ?? "d1",
    organization_id: ORG,
    table_id: overrides.table_id ?? "t1",
    table_name: overrides.table_name ?? "orders_raw",
    drafted_text: overrides.drafted_text ?? "Raw orders from the storefront checkout pipeline.",
    accuracy_score: overrides.accuracy_score ?? 0.9,
    clarity_score: overrides.clarity_score ?? 0.8,
    style_score: overrides.style_score ?? 0.7,
    completeness_score: overrides.completeness_score ?? 0.85,
    overall_score: overrides.overall_score ?? 0.85,
    evidence: overrides.evidence ?? {},
    status: overrides.status ?? "DRAFT",
    governance_review_id: overrides.governance_review_id ?? null,
    published_version_id: overrides.published_version_id ?? null,
    created_by: overrides.created_by ?? "me@tenant.example",
    reviewed_by: overrides.reviewed_by ?? null,
    reviewed_at: overrides.reviewed_at ?? null,
    created_at: overrides.created_at ?? "2026-09-02T00:00:00Z",
    updated_at: overrides.updated_at ?? "2026-09-02T00:00:00Z",
  };
}

const REVIEW: GovernanceReviewRead = {
  id: "gr_1",
  organization_id: ORG,
  object_type: "ASSET_DESCRIPTION_DRAFT",
  object_id: "d1",
  requested_action: "PUBLISH",
  status: "PENDING",
  requested_by: "me@tenant.example",
  decided_by: null,
  decision_reason: null,
  decided_at: null,
  created_at: "2026-09-02T01:00:00Z",
  updated_at: "2026-09-02T01:00:00Z",
};

async function loadScreen() {
  const module = await import("./DescriptionDraftsScreen");
  return module.DescriptionDraftsScreen;
}

beforeEach(() => {
  listAssetDescriptionDrafts.mockReset();
  submitAssetDescriptionDraft.mockReset();
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DescriptionDraftsScreen", () => {
  it("renders the empty state with a link back to Catalog when no drafts exist", async () => {
    listAssetDescriptionDrafts.mockResolvedValue({ drafts: [], limit: 200, offset: 0, total: 0 });
    const Screen = await loadScreen();
    render(<Screen />);

    await waitFor(() => expect(screen.getByText("No drafts yet")).toBeInTheDocument());
    expect(screen.getByText(/Generate drafts from the Catalog screen/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Go to Catalog" }));
    expect(location.hash).toBe("#/catalog");
  });

  it("renders drafts and sorts by overall_score desc by default", async () => {
    listAssetDescriptionDrafts.mockResolvedValue({
      drafts: [
        makeDraft({ id: "d1", table_name: "aaa_table", overall_score: 0.5 }),
        makeDraft({ id: "d2", table_name: "bbb_table", overall_score: 0.9 }),
        makeDraft({ id: "d3", table_name: "ccc_table", overall_score: 0.35, status: "DRAFT" }),
      ],
      limit: 200,
      offset: 0,
      total: 3,
    });

    const Screen = await loadScreen();
    render(<Screen />);

    await waitFor(() => expect(screen.getByText("bbb_table")).toBeInTheDocument());

    const rows = document.querySelectorAll(".draftrow__tablename");
    expect(Array.from(rows).map((n) => n.textContent)).toEqual([
      "bbb_table", // 0.9
      "aaa_table", // 0.5
      "ccc_table", // 0.35
    ]);
  });

  it("disables Submit for review when overall_score < 0.4 (evidence gate)", async () => {
    listAssetDescriptionDrafts.mockResolvedValue({
      drafts: [makeDraft({ id: "d3", overall_score: 0.3, status: "DRAFT" })],
      limit: 200,
      offset: 0,
      total: 1,
    });

    const Screen = await loadScreen();
    render(<Screen />);

    const submitBtn = await screen.findByRole("button", { name: /submit for review/i });
    expect(submitBtn).toBeDisabled();
    expect(submitBtn.getAttribute("title")).toMatch(/not enough evidence/i);
  });

  it("optimistically flips a draft to PENDING_APPROVAL on a successful submit", async () => {
    listAssetDescriptionDrafts.mockResolvedValue({
      drafts: [makeDraft({ id: "d1", overall_score: 0.8, status: "DRAFT" })],
      limit: 200,
      offset: 0,
      total: 1,
    });
    let resolveSubmit!: (v: GovernanceReviewRead) => void;
    submitAssetDescriptionDraft.mockImplementation(
      () => new Promise((res) => { resolveSubmit = res; }),
    );

    const Screen = await loadScreen();
    render(<Screen />);

    const submitBtn = await screen.findByRole("button", { name: /submit for review/i });
    fireEvent.click(submitBtn);

    // Optimistic flip lands before the promise settles: the row switches
    // status pill without waiting on the server.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /open in review queue/i })).toBeInTheDocument(),
    );
    expect(submitAssetDescriptionDraft).toHaveBeenCalledWith("d1");

    resolveSubmit(REVIEW);
    await waitFor(() => expect(submitAssetDescriptionDraft).toHaveBeenCalledTimes(1));
  });

  it("shows the classified 'below evidence threshold' message when submit is refused", async () => {
    listAssetDescriptionDrafts.mockResolvedValue({
      drafts: [makeDraft({ id: "d1", overall_score: 0.8, status: "DRAFT" })],
      limit: 200,
      offset: 0,
      total: 1,
    });
    submitAssetDescriptionDraft.mockRejectedValue(
      new ApiError(422, "draft carries too little evidence for independent review"),
    );

    const Screen = await loadScreen();
    render(<Screen />);

    fireEvent.click(await screen.findByRole("button", { name: /submit for review/i }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/too little evidence/i)).toBeInTheDocument();
    // Optimistic flip was rolled back so the button reappears.
    expect(screen.getByRole("button", { name: /submit for review/i })).toBeInTheDocument();
  });

  it("filters drafts by status via the server, and by table name in the client", async () => {
    listAssetDescriptionDrafts.mockResolvedValue({
      drafts: [
        makeDraft({ id: "d1", table_name: "orders_raw" }),
        makeDraft({ id: "d2", table_name: "sessions_daily" }),
      ],
      limit: 200,
      offset: 0,
      total: 2,
    });

    const Screen = await loadScreen();
    render(<Screen />);
    await waitFor(() => expect(screen.getByText("orders_raw")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "PENDING_APPROVAL" } });
    await waitFor(() =>
      expect(listAssetDescriptionDrafts).toHaveBeenLastCalledWith(
        ORG,
        { status: "PENDING_APPROVAL", limit: 200 },
        expect.anything(),
      ),
    );

    fireEvent.change(screen.getByLabelText("Table name"), { target: { value: "sess" } });
    // The table-name filter is client-side; no new server call.
    expect(screen.queryByText("orders_raw")).not.toBeInTheDocument();
    expect(screen.getByText("sessions_daily")).toBeInTheDocument();
  });
});
