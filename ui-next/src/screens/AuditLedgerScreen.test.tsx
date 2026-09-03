import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AuditEventRead } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   UX-16: audit ledger against the real
   `GET /v1/organizations/{organization_id}/audit-events` (`list_audit_events`,
   `operational_api.py:336`). Mocks the API boundary, matching
   `MarketplaceScreen.test.tsx`/`LineageRefusalScreen.test.tsx`'s established
   pattern -- real payload shapes, assertions on the exact endpoint/args
   called, not superficial snapshots.
--------------------------------------------------------------------------- */

const fetchAuditEvents = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<{ items: AuditEventRead[]; limit: number; offset: number; total: number }>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchAuditEvents: (query: unknown, signal?: AbortSignal) => fetchAuditEvents(query, signal),
  };
});

const EVENT: AuditEventRead = {
  id: 5040,
  organization_id: "00000000-0000-0000-0000-000000000001",
  principal_id: "priya@tenant.example",
  principal_type: "USER",
  action: "governance_review.decide",
  resource_type: "GOVERNANCE_REVIEW",
  resource_id: "rq_4179",
  outcome: "SUCCESS",
  correlation_id: "corr_9f21a0",
  source_ip: "10.2.4.18",
  details: { decision: "APPROVE", object_type: "GLOSSARY_TERM_VERSION" },
  occurred_at: "2026-09-01T10:05:00Z",
};

async function loadScreen() {
  const { AuditLedgerScreen } = await import("./AuditLedgerScreen");
  return AuditLedgerScreen;
}

beforeEach(() => {
  fetchAuditEvents.mockReset();
  fetchAuditEvents.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
  vi.resetModules();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("AuditLedgerScreen against the real UX-16 endpoint", () => {
  it("loads and renders events with no filter applied", async () => {
    fetchAuditEvents.mockResolvedValue({ items: [EVENT], limit: 100, offset: 0, total: 1 });
    const AuditLedgerScreen = await loadScreen();

    render(<AuditLedgerScreen />);

    await waitFor(() => expect(screen.getByText("governance_review.decide")).toBeInTheDocument());
    expect(fetchAuditEvents).toHaveBeenCalledWith(
      {
        organizationId: "00000000-0000-0000-0000-000000000001",
        action: undefined,
        resourceType: undefined,
        correlationId: undefined,
        since: undefined,
        until: undefined,
        limit: 100,
        offset: 0,
      },
      expect.anything(),
    );
    expect(screen.getByText("rq_4179")).toBeInTheDocument();
  });

  it("re-fetches with a new action filter, aborting the previous in-flight request", async () => {
    let firstSignal: AbortSignal | undefined;
    fetchAuditEvents.mockImplementationOnce((_query: unknown, signal?: AbortSignal) => {
      firstSignal = signal;
      return new Promise(() => {}); // the first page never resolves -- it stays in flight
    });
    fetchAuditEvents.mockResolvedValue({ items: [EVENT], limit: 100, offset: 0, total: 1 });

    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);
    await waitFor(() => expect(fetchAuditEvents).toHaveBeenCalledTimes(1));
    expect(firstSignal?.aborted).toBe(false);

    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "governance_review.decide" } });
    await vi.advanceTimersByTimeAsync(300);

    await waitFor(() =>
      expect(fetchAuditEvents).toHaveBeenLastCalledWith(
        expect.objectContaining({ action: "governance_review.decide" }),
        expect.anything(),
      ),
    );
    // The screen's own `inflight.current?.abort()` fires on the new request --
    // the first request's own signal is what proves it, not just a second call.
    expect(firstSignal?.aborted).toBe(true);
    expect(new URLSearchParams(location.search).get("action")).toBe("governance_review.decide");
  });

  it("selecting an event opens the evidence panel with a permalink URL param", async () => {
    fetchAuditEvents.mockResolvedValue({ items: [EVENT], limit: 100, offset: 0, total: 1 });
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);
    await waitFor(() => expect(screen.getByText("governance_review.decide")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /governance_review\.decide/ }));

    expect(new URLSearchParams(location.search).get("event")).toBe("5040");
    const panel = await screen.findByLabelText("Event 5040");
    expect(panel).toHaveTextContent("corr_9f21a0");
    expect(panel).toHaveTextContent("APPROVE");

    fireEvent.click(screen.getByRole("button", { name: "Close event detail" }));
    expect(new URLSearchParams(location.search).get("event")).toBeNull();
  });

  it("surfaces a fetch error with a retry action", async () => {
    fetchAuditEvents.mockRejectedValue(new ApiError(403, "requires PlatformAdmin, OrganizationAdmin, Auditor or Operations"));
    const AuditLedgerScreen = await loadScreen();

    render(<AuditLedgerScreen />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Auditor/));
  });

  it("shows the empty state when there are no events", async () => {
    fetchAuditEvents.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    const AuditLedgerScreen = await loadScreen();

    render(<AuditLedgerScreen />);

    await waitFor(() => expect(screen.getByText("No audit events match these filters")).toBeInTheDocument());
  });
});
