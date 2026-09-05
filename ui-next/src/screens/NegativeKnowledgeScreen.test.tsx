import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { NegativeAssertionRead } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Negative knowledge (Phase E / EE.3) against the real
   `negative_knowledge_api.py` endpoints. Mocks the API boundary, matching
   `AuditLedgerScreen.test.tsx`/`QualityScreen.test.tsx`'s established pattern
   -- real payload shapes, assertions on the exact endpoint/args called, not
   superficial snapshots.
--------------------------------------------------------------------------- */

type Page = { items: NegativeAssertionRead[]; limit: number; offset: number; total: number };

const searchNegativeKnowledge = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<Page>>();
const fetchNegativeKnowledgeForSubject = vi.fn<
  (subjectId: string, query: unknown, signal?: AbortSignal) => Promise<Page>
>();
const liftNegativeAssertionSuppression = vi.fn<
  (
    assertionId: string,
    body: { reason: string },
    signal?: AbortSignal,
  ) => Promise<NegativeAssertionRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    searchNegativeKnowledge: (query: unknown, signal?: AbortSignal) =>
      searchNegativeKnowledge(query, signal),
    fetchNegativeKnowledgeForSubject: (subjectId: string, query: unknown, signal?: AbortSignal) =>
      fetchNegativeKnowledgeForSubject(subjectId, query, signal),
    liftNegativeAssertionSuppression: (
      assertionId: string,
      body: { reason: string },
      signal?: AbortSignal,
    ) => liftNegativeAssertionSuppression(assertionId, body, signal),
  };
});

const ASSERTION: NegativeAssertionRead = {
  id: "na_0001",
  organization_id: "00000000-0000-0000-0000-000000000001",
  assertion_type: "TABLE_NOT_ENTITY",
  subject_id: "t_customer_master",
  predicate: { claimed_entity: "customer" },
  evidence: { reviewer_note: "Staging copy, not the governed master." },
  rejected_by: "priya@tenant.example",
  rejected_at: "2026-08-20T10:05:00Z",
  suppression_active: true,
  material_change_hash: null,
  suppression_lifted_at: null,
  suppression_lifted_by: null,
  lift_reason: null,
  created_at: "2026-08-20T10:05:00Z",
  updated_at: "2026-08-20T10:05:00Z",
};

async function loadScreen() {
  const { NegativeKnowledgeScreen } = await import("./NegativeKnowledgeScreen");
  return NegativeKnowledgeScreen;
}

beforeEach(() => {
  searchNegativeKnowledge.mockReset();
  fetchNegativeKnowledgeForSubject.mockReset();
  liftNegativeAssertionSuppression.mockReset();
  searchNegativeKnowledge.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
  fetchNegativeKnowledgeForSubject.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
  vi.resetModules();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("NegativeKnowledgeScreen against the real EE.3 endpoints", () => {
  it("loads and renders assertions with no filter applied", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [ASSERTION], limit: 50, offset: 0, total: 1 });
    const NegativeKnowledgeScreen = await loadScreen();

    render(<NegativeKnowledgeScreen />);

    await waitFor(() => expect(screen.getByText("t_customer_master")).toBeInTheDocument());
    expect(searchNegativeKnowledge).toHaveBeenCalledWith(
      { assertionType: undefined, suppressionActive: undefined, limit: 50, offset: 0 },
      expect.anything(),
    );
    expect(screen.getByText("TABLE_NOT_ENTITY")).toBeInTheDocument();
    expect(screen.getByText("suppression active")).toBeInTheDocument();
  });

  it("re-fetches with a new assertion-type filter, debounced", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [ASSERTION], limit: 50, offset: 0, total: 1 });
    const NegativeKnowledgeScreen = await loadScreen();
    render(<NegativeKnowledgeScreen />);
    await waitFor(() => expect(searchNegativeKnowledge).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Assertion type"), {
      target: { value: "COLUMN_NOT_PII" },
    });
    await vi.advanceTimersByTimeAsync(300);

    await waitFor(() =>
      expect(searchNegativeKnowledge).toHaveBeenLastCalledWith(
        expect.objectContaining({ assertionType: "COLUMN_NOT_PII" }),
        expect.anything(),
      ),
    );
    expect(new URLSearchParams(location.search).get("assertion_type")).toBe("COLUMN_NOT_PII");
  });

  it("re-fetches immediately with the suppression tri-state filter", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [ASSERTION], limit: 50, offset: 0, total: 1 });
    const NegativeKnowledgeScreen = await loadScreen();
    render(<NegativeKnowledgeScreen />);
    await waitFor(() => expect(searchNegativeKnowledge).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Suppression"), { target: { value: "LIFTED" } });

    await waitFor(() =>
      expect(searchNegativeKnowledge).toHaveBeenLastCalledWith(
        expect.objectContaining({ suppressionActive: false }),
        expect.anything(),
      ),
    );
  });

  it("looking up by subject calls the per-subject endpoint instead of search", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    fetchNegativeKnowledgeForSubject.mockResolvedValue({
      items: [ASSERTION],
      limit: 50,
      offset: 0,
      total: 1,
    });
    const NegativeKnowledgeScreen = await loadScreen();
    render(<NegativeKnowledgeScreen />);
    await waitFor(() => expect(searchNegativeKnowledge).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Look up by subject ID"), {
      target: { value: "t_customer_master" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Look up" }));

    await waitFor(() =>
      expect(fetchNegativeKnowledgeForSubject).toHaveBeenCalledWith(
        "t_customer_master",
        { limit: 50, offset: 0 },
        expect.anything(),
      ),
    );
    expect(new URLSearchParams(location.search).get("subject")).toBe("t_customer_master");
    await waitFor(() => expect(screen.getByText("t_customer_master")).toBeInTheDocument());
  });

  it("lifting suppression prompts for a reason, calls the endpoint, and updates the row in place", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [ASSERTION], limit: 50, offset: 0, total: 1 });
    liftNegativeAssertionSuppression.mockResolvedValue({
      ...ASSERTION,
      suppression_active: false,
      suppression_lifted_at: "2026-09-04T00:00:00Z",
      suppression_lifted_by: "dev-fixture-user",
      lift_reason: "False positive, table was renamed.",
    });
    vi.spyOn(window, "prompt").mockReturnValue("False positive, table was renamed.");

    const NegativeKnowledgeScreen = await loadScreen();
    render(<NegativeKnowledgeScreen />);
    await waitFor(() => expect(screen.getByText("t_customer_master")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Lift suppression" }));

    await waitFor(() =>
      expect(liftNegativeAssertionSuppression).toHaveBeenCalledWith(
        "na_0001",
        { reason: "False positive, table was renamed." },
        undefined,
      ),
    );
    await waitFor(() => expect(screen.getByText("suppression lifted")).toBeInTheDocument());
  });

  it("requires a reason before lifting suppression, and skips the call if none is given", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [ASSERTION], limit: 50, offset: 0, total: 1 });
    vi.spyOn(window, "prompt").mockReturnValue(null);

    const NegativeKnowledgeScreen = await loadScreen();
    render(<NegativeKnowledgeScreen />);
    await waitFor(() => expect(screen.getByText("t_customer_master")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Lift suppression" }));

    expect(window.prompt).toHaveBeenCalled();
    expect(liftNegativeAssertionSuppression).not.toHaveBeenCalled();
  });

  it("surfaces a fetch error with a retry action", async () => {
    searchNegativeKnowledge.mockRejectedValue(
      new ApiError(403, "requires PlatformAdmin, DataSteward, DataEngineer or Viewer"),
    );
    const NegativeKnowledgeScreen = await loadScreen();

    render(<NegativeKnowledgeScreen />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Viewer/));
  });

  it("shows the empty state when there are no assertions", async () => {
    searchNegativeKnowledge.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    const NegativeKnowledgeScreen = await loadScreen();

    render(<NegativeKnowledgeScreen />);

    await waitFor(() =>
      expect(screen.getByText("No assertions match this filter")).toBeInTheDocument(),
    );
  });
});
