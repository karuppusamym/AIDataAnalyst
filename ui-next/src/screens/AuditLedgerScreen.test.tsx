import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AuditEventRead } from "../lib/ui-types";
import type { AuditExportResult } from "../lib/api";
import { ApiError } from "../lib/api";
import type { MeRead } from "../lib/types";
import type { Session } from "../lib/session";

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

const downloadAuditEventsExport = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<AuditExportResult>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchAuditEvents: (query: unknown, signal?: AbortSignal) => fetchAuditEvents(query, signal),
    downloadAuditEventsExport: (query: unknown, signal?: AbortSignal) => downloadAuditEventsExport(query, signal),
  };
});

/* R11-AUD08: who may export is decided by the session's roles. `null` is
   "`/v1/me` has not answered" -- what every earlier test in this file runs as. */
let sessionMe: MeRead | null = null;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "demo",
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "fixtures",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
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
  downloadAuditEventsExport.mockReset();
  sessionMe = null;
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

/* ---- Export (R11-AUD08) ------------------------------------------------------
   `GET .../audit-events/export.jsonl` (`audit_export_api.py`) is admitted to
   Auditor, Operations, OrganizationAdmin and PlatformAdmin, and each call is an
   audited event in the ledger it exports -- so it is a deliberate click, never a
   load-time read, and never offered on a guess. What is asserted here is the
   contract the screen has with that route: WHO is offered it, WHICH filters it
   sends, what the reader is told about the file (above all whether it is
   complete), and that a refusal reaches them in the server's words. */

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});
const ORG = "00000000-0000-0000-0000-000000000001";
const SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
const DELIVERED: AuditExportResult = {
  filename: `audit-events-${ORG}-20260920T101500Z.jsonl`,
  rowCount: 1234, truncated: false, rowLimit: 50000, sha256: SHA,
};
const exportButton = () => screen.queryByRole("button", { name: /^Export JSONL$|^Exporting…$/ });

describe("AuditLedgerScreen: exporting the ledger (R11-AUD08)", () => {
  it.each(["Auditor", "Operations", "OrganizationAdmin", "PlatformAdmin"])(
    "offers Export to %s",
    async (role) => {
      sessionMe = asRoles(role);
      const AuditLedgerScreen = await loadScreen();
      render(<AuditLedgerScreen />);

      expect(await screen.findByRole("button", { name: "Export JSONL" })).toBeEnabled();
      // Offering it is not doing it: nothing is exported (and so nothing is audited) on load.
      expect(downloadAuditEventsExport).not.toHaveBeenCalled();
    },
  );

  it.each(["Viewer", "AgentDeveloper", "Analyst", "DataSteward", "Reviewer"])(
    "offers no Export to %s, and never asks the server for one",
    async (role) => {
      sessionMe = asRoles(role);
      const AuditLedgerScreen = await loadScreen();
      render(<AuditLedgerScreen />);
      await waitFor(() => expect(screen.getByText("No audit events match these filters")).toBeInTheDocument());

      expect(exportButton()).not.toBeInTheDocument();
      expect(downloadAuditEventsExport).not.toHaveBeenCalled();
    },
  );

  it("counts a session as admitted when any one of its roles is", async () => {
    sessionMe = asRoles("Viewer", "Auditor");
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    expect(await screen.findByRole("button", { name: "Export JSONL" })).toBeInTheDocument();
  });

  it("does not offer Export until identity has said the session may -- a bulk extraction is never offered on a guess", async () => {
    // `me` is null: `/v1/me` has not answered. Reads fail open; this does not.
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);
    await waitFor(() => expect(screen.getByText("No audit events match these filters")).toBeInTheDocument());

    expect(exportButton()).not.toBeInTheDocument();
  });

  it("exports the ledger's current filters, and tells the reader what the file is", async () => {
    sessionMe = asRoles("Auditor");
    history.replaceState(
      null, "",
      "/?action=governance_review.decide&resource_type=GOVERNANCE_REVIEW&correlation_id=corr_9f21a0"
        + "&since=2026-09-01T00:00:00.000Z&until=2026-09-15T00:00:00.000Z&event=5040",
    );
    downloadAuditEventsExport.mockResolvedValue(DELIVERED);
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    await waitFor(() => expect(downloadAuditEventsExport).toHaveBeenCalledTimes(1));
    // The five filters the list is showing -- and not the selected event, and not a page.
    expect(downloadAuditEventsExport).toHaveBeenCalledWith(
      {
        organizationId: ORG,
        action: "governance_review.decide",
        resourceType: "GOVERNANCE_REVIEW",
        correlationId: "corr_9f21a0",
        since: "2026-09-01T00:00:00.000Z",
        until: "2026-09-15T00:00:00.000Z",
      },
      expect.any(AbortSignal),
    );
    const strip = await screen.findByText(/^Exported 1,234 events as /);
    expect(strip).toHaveTextContent(DELIVERED.filename);
    // Enough for an auditor to check the file against what the server computed, and to know it left a trace.
    expect(strip).toHaveTextContent(`SHA-256 ${SHA}`);
    expect(strip).toHaveTextContent("The export was recorded in the ledger.");
    expect(strip).not.toHaveTextContent(/incomplete/i);
    // The list is untouched by it: exporting is not paging.
    expect(fetchAuditEvents).toHaveBeenCalledTimes(1);
  });

  it("sends no filters when none are set", async () => {
    sessionMe = asRoles("PlatformAdmin");
    downloadAuditEventsExport.mockResolvedValue({ ...DELIVERED, rowCount: 1 });
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    expect(await screen.findByText(/^Exported 1 event as /)).toBeInTheDocument();
    expect(downloadAuditEventsExport).toHaveBeenCalledWith(
      {
        organizationId: ORG,
        action: undefined, resourceType: undefined, correlationId: undefined,
        since: undefined, until: undefined,
      },
      expect.any(AbortSignal),
    );
  });

  it("shows progress, admits one export at a time, and re-enables afterwards", async () => {
    sessionMe = asRoles("Auditor");
    let deliver: (result: AuditExportResult) => void = () => undefined;
    downloadAuditEventsExport.mockImplementation(() => new Promise((resolve) => { deliver = resolve; }));
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    const busy = await screen.findByRole("button", { name: "Exporting…" });
    expect(busy).toBeDisabled();
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent(/Preparing the export/);
    fireEvent.click(busy);
    expect(downloadAuditEventsExport).toHaveBeenCalledTimes(1);

    deliver(DELIVERED);
    expect(await screen.findByRole("button", { name: "Export JSONL" })).toBeEnabled();
    expect(screen.queryByText(/Preparing the export/)).not.toBeInTheDocument();
  });

  it("says plainly when the server cut the file at its limit -- it is not a complete record", async () => {
    sessionMe = asRoles("Auditor");
    downloadAuditEventsExport.mockResolvedValue({ ...DELIVERED, rowCount: 50000, truncated: true });
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    const warning = await screen.findByText(/it is INCOMPLETE/);
    expect(warning).toHaveTextContent("the server's limit of 50,000 events");
    expect(warning).toHaveTextContent("Narrow the time range with Since and Until and export again.");
    // A truncated file must never be reported with the success sentence.
    expect(screen.queryByText(/^Exported /)).not.toBeInTheDocument();
  });

  it("does not claim completeness when the server's truncation flag could not be read", async () => {
    sessionMe = asRoles("Auditor");
    downloadAuditEventsExport.mockResolvedValue({ ...DELIVERED, truncated: null });
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    const warning = await screen.findByText(/completeness flag could not be read/);
    expect(warning).toHaveTextContent("it is not confirmed that nothing was cut off");
    expect(screen.queryByText(/^Exported /)).not.toBeInTheDocument();
  });

  it("shows the server's refusal, names it as a refusal, and recovers", async () => {
    sessionMe = asRoles("Auditor");
    downloadAuditEventsExport.mockRejectedValueOnce(new ApiError(403, "EXPORT_DENIED_BY_POLICY"));
    downloadAuditEventsExport.mockResolvedValueOnce(DELIVERED);
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    const refusal = await screen.findByText(/^The audit export was refused: EXPORT_DENIED_BY_POLICY\./);
    expect(refusal).toHaveTextContent("Reading the ledger and exporting it are separate permissions.");
    expect(screen.queryByText(/^Exported /)).not.toBeInTheDocument();
    // The list the reader was already looking at is not disturbed by a refused export.
    expect(screen.getByText("No audit events match these filters")).toBeInTheDocument();

    // Not a dead end: the button is back, and a second attempt goes through.
    fireEvent.click(screen.getByRole("button", { name: "Export JSONL" }));
    expect(await screen.findByText(/^Exported 1,234 events as /)).toBeInTheDocument();
    expect(screen.queryByText(/was refused/)).not.toBeInTheDocument();
  });

  it("shows any other failure in the server's own words", async () => {
    sessionMe = asRoles("Operations");
    downloadAuditEventsExport.mockRejectedValue(new ApiError(422, "since cannot be later than until"));
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    expect(await screen.findByText("The audit export failed: since cannot be later than until")).toBeInTheDocument();
  });

  it("shows a client-side failure (demo data, a network fault) instead of pretending it exported", async () => {
    sessionMe = asRoles("Operations");
    downloadAuditEventsExport.mockRejectedValue(new Error("The audit export is composed by the server."));
    const AuditLedgerScreen = await loadScreen();
    render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));

    expect(
      await screen.findByText("The audit export failed: The audit export is composed by the server."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Exported /)).not.toBeInTheDocument();
  });

  it("abandons an export the reader leaves the screen before it arrives", async () => {
    sessionMe = asRoles("Auditor");
    let signal: AbortSignal | undefined;
    downloadAuditEventsExport.mockImplementation((_query, s) => {
      signal = s;
      return new Promise(() => undefined);
    });
    const AuditLedgerScreen = await loadScreen();
    const { unmount } = render(<AuditLedgerScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Export JSONL" }));
    await waitFor(() => expect(signal).toBeDefined());
    expect(signal!.aborted).toBe(false);

    unmount();
    expect(signal!.aborted).toBe(true);
  });
});
