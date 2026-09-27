import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type {
  GlossaryConflictCreate,
  GlossaryConflictRead,
  GlossaryConflictResolution,
  GovernanceReviewRead,
  MeRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import type { Session, SessionState } from "../lib/session";
import { resetLocationCacheForTests } from "../lib/location";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { GlossaryConflicts } from "./GlossaryConflicts";

/* ---------------------------------------------------------------------------
   Glossary review -> Conflicts (R11-AUD08).

   The properties, and why each was a real way to get a screen over a
   maker-checker write wrong:

     1. WHO SEES WHAT. Every role the list admits reads it; only the four write
        roles are offered Detect, Raise and Propose. A session outside the read
        list is not asked at all (it gets "not applicable", not the 403), and
        nothing is asked while `/v1/me` is in flight.
     2. A RESOLUTION IS A PROPOSAL, AND SAYS SO. The dialog states that a
        different reviewer decides, that neither term is edited, and will not
        confirm without a decision and a rationale the API accepts. The row is
        never described as fixed.
     3. NOTHING FIRES ON A CLICK. Detect and Propose each open a dialog that
        states the effect; no request is made until it is confirmed.
     4. A REFUSAL IS AN ANSWER, IN THE SERVER'S WORDS. The dialog stays open and
        shows the sentence the API sent; a 409 re-reads the list behind it, since
        the row on screen described a state that no longer holds.
     5. EVERY CONFLICT TYPE COMES THROUGH THE ONE ROUTE. A metric-formula
        collision (`term_id: null`, metric-shaped positions) renders from what it
        contains rather than as a broken glossary term.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

type Query = { status?: string | null; limit?: number; offset?: number };
const fetchGlossaryConflicts =
  vi.fn<(organizationId: string, query: Query, signal?: AbortSignal) => Promise<PageOf<GlossaryConflictRead>>>();
const detectGlossaryConflicts =
  vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<GlossaryConflictRead>>>();
const raiseGlossaryConflict =
  vi.fn<(organizationId: string, body: GlossaryConflictCreate, signal?: AbortSignal) => Promise<GlossaryConflictRead>>();
const submitGlossaryConflictResolution =
  vi.fn<(conflictId: string, body: GlossaryConflictResolution, signal?: AbortSignal) => Promise<GovernanceReviewRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchGlossaryConflicts: (organizationId: string, query: Query, signal?: AbortSignal) =>
      fetchGlossaryConflicts(organizationId, query, signal),
    detectGlossaryConflicts: (organizationId: string, signal?: AbortSignal) =>
      detectGlossaryConflicts(organizationId, signal),
    raiseGlossaryConflict: (organizationId: string, body: GlossaryConflictCreate, signal?: AbortSignal) =>
      raiseGlossaryConflict(organizationId, body, signal),
    submitGlossaryConflictResolution: (conflictId: string, body: GlossaryConflictResolution, signal?: AbortSignal) =>
      submitGlossaryConflictResolution(conflictId, body, signal),
  };
});

let sessionMe: MeRead | null = null;
let sessionState: SessionState = "connected";
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: sessionState,
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "live",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const conflict = (overrides: Partial<GlossaryConflictRead> = {}): GlossaryConflictRead => ({
  id: "c1000000-0000-4000-8000-000000000001",
  organization_id: ORG,
  term_id: "a1000000-0000-4000-8000-000000000001",
  conflict_type: "SYNONYM_COLLISION",
  status: "OPEN",
  position_a: {
    term_id: "a1000000-0000-4000-8000-000000000001",
    display_name: "Customer",
    definition: "A party that holds at least one open account.",
    colliding_label: "client",
  },
  position_b: {
    term_id: "a1000000-0000-4000-8000-000000000002",
    display_name: "Active customer",
    definition: "A customer with a posted transaction in the last 90 days.",
    colliding_label: "client",
  },
  assigned_owner: "risk-data-stewards@tenant.example",
  raised_by: "priya.steward",
  proposed_resolution: null,
  proposed_definition: null,
  resolution_rationale: null,
  resolved_by: null,
  resolved_at: null,
  created_at: "2026-09-18T09:12:00Z",
  updated_at: "2026-09-18T09:12:00Z",
  ...overrides,
});

const OPEN = conflict();
const MANUAL = conflict({
  id: "c1000000-0000-4000-8000-000000000002",
  term_id: null,
  conflict_type: "DEFINITION",
  position_a: { display_name: "Net revenue", definition: "After returns.", source: "Finance handbook" },
  position_b: { display_name: "Net revenue", definition: "After returns and rebates.", source: "Sales wiki" },
  raised_by: "morgan.covering",
  assigned_owner: null,
});
const METRIC = conflict({
  id: "c1000000-0000-4000-8000-000000000003",
  term_id: null,
  conflict_type: "METRIC_FORMULA_COLLISION",
  position_a: {
    metric_id: "d1000000-0000-4000-8000-000000000001", metric_name: "Monthly active customers",
    aggregation: "COUNT", grain: "month", match_kind: "EXACT_MATCH", created_by: "priya.steward",
  },
  position_b: {
    metric_id: "d1000000-0000-4000-8000-000000000002", metric_name: "MAC",
    aggregation: "COUNT", grain: "month", match_kind: "EXACT_MATCH", created_by: "sam.agentdev",
  },
});
const IN_REVIEW = conflict({
  id: "c1000000-0000-4000-8000-000000000004",
  status: "REVIEW_REQUIRED",
  conflict_type: "SOURCE_DISAGREEMENT",
  term_id: null,
  position_a: { display_name: "Settlement date", definition: "When funds are released.", source: "Core banking" },
  position_b: { display_name: "Settlement date", definition: "When the clearing house confirms.", source: "Payments hub" },
  proposed_resolution: "MERGE",
  proposed_definition: "Settlement date is when the clearing house confirms the payment.",
  resolution_rationale: "Both sources describe different events.",
});
const DONE = conflict({
  id: "c1000000-0000-4000-8000-000000000005",
  status: "RESOLVED",
  position_a: { term_id: "a1000000-0000-4000-8000-000000000003", display_name: "Balance", colliding_label: "ledger balance" },
  position_b: { term_id: "a1000000-0000-4000-8000-000000000004", display_name: "Ledger balance", colliding_label: "ledger balance" },
  proposed_resolution: "RETAIN_BOTH",
  resolution_rationale: "Both terms stay.",
  resolved_by: "riya.reviewer",
  resolved_at: "2026-09-10T15:20:00Z",
});

const page = (items: GlossaryConflictRead[], extra: Partial<PageOf<GlossaryConflictRead>> = {}): PageOf<GlossaryConflictRead> => ({
  items, limit: 25, offset: 0, total: items.length, ...extra,
});

const REVIEW: GovernanceReviewRead = {
  id: "9e000000-0000-4000-8000-000000000001", organization_id: ORG, object_type: "GLOSSARY_CONFLICT",
  object_id: OPEN.id, requested_action: "RESOLVE", status: "PENDING", requested_by: "someone",
  decided_by: null, decision_reason: null, decided_at: null,
  created_at: "2026-09-21T08:00:00Z", updated_at: "2026-09-21T08:00:00Z",
};

const WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];
const READ_ONLY_ROLES = ["Analyst", "Auditor", "DataAdmin", "Reviewer", "Viewer"];
const OUTSIDE_ROLES = ["AgentDeveloper", "ToolDeveloper", "Operations", "OrganizationAdmin", "MetadataReviewer"];

const detectButton = () => screen.queryByRole("button", { name: "Detect conflicts" });
const raiseButton = () => screen.queryByRole("button", { name: "Raise a conflict" });
const openRow = async (title: string) => {
  fireEvent.click(await screen.findByRole("button", { name: title }));
};

function mount(url = "/#/steward/glossary-review") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<GlossaryConflicts />);
}

beforeEach(() => {
  fetchGlossaryConflicts.mockReset();
  fetchGlossaryConflicts.mockResolvedValue(page([OPEN, MANUAL, METRIC, IN_REVIEW, DONE]));
  detectGlossaryConflicts.mockReset();
  raiseGlossaryConflict.mockReset();
  submitGlossaryConflictResolution.mockReset();
  sessionMe = asRoles("DataSteward");
  sessionState = "connected";
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Conflicts: who sees what", () => {
  it.each(READ_ONLY_ROLES)("shows the list to %s and offers no way to change it", async (role) => {
    sessionMe = asRoles(role);
    mount();

    expect(await screen.findByRole("button", { name: "Customer vs Active customer" })).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenCalledWith(ORG, { status: null, limit: 25, offset: 0 }, expect.any(AbortSignal));
    expect(detectButton()).not.toBeInTheDocument();
    expect(raiseButton()).not.toBeInTheDocument();
    // Told why, rather than left to wonder where the control is.
    expect(screen.getByText(/You can read conflicts\. Detecting, raising and resolving them is for sessions holding DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin\./)).toBeInTheDocument();
    // Not even on an open row.
    await openRow("Customer vs Active customer");
    expect(screen.queryByRole("button", { name: "Propose a resolution" })).not.toBeInTheDocument();
  });

  it.each(WRITE_ROLES)("offers %s Detect, Raise and Propose", async (role) => {
    sessionMe = asRoles(role);
    mount();

    expect(await screen.findByRole("button", { name: "Customer vs Active customer" })).toBeInTheDocument();
    expect(detectButton()).toBeEnabled();
    expect(raiseButton()).toBeEnabled();
    expect(screen.queryByText(/You can read conflicts/)).not.toBeInTheDocument();
    await openRow("Customer vs Active customer");
    expect(screen.getByRole("button", { name: "Propose a resolution" })).toBeEnabled();
  });

  it.each(OUTSIDE_ROLES)("asks for nothing as %s, and says it does not apply rather than showing an error", async (role) => {
    sessionMe = asRoles(role);
    mount();

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchGlossaryConflicts).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(detectButton()).not.toBeInTheDocument();
    expect(raiseButton()).not.toBeInTheDocument();
  });

  it("holds the read while identity is in flight, and offers no control until it has answered", async () => {
    sessionState = "connecting";
    sessionMe = null;
    mount();

    expect(await screen.findByText(/Loading glossary conflicts/)).toBeInTheDocument();
    expect(fetchGlossaryConflicts).not.toHaveBeenCalled();
    expect(detectButton()).not.toBeInTheDocument();
    expect(raiseButton()).not.toBeInTheDocument();
    // ... and it does not tell a steward-to-be that they cannot, or that it does not apply.
    expect(screen.queryByText(/You can read conflicts/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();
  });

  it("never sends the read when identity then says the session may not read it", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const view = mount();
    await screen.findByText(/Loading glossary conflicts/);

    sessionState = "connected";
    sessionMe = asRoles("AgentDeveloper");
    view.rerender(<GlossaryConflicts />);

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchGlossaryConflicts).not.toHaveBeenCalled();
  });

  it("sends the read once identity says the session may, and offers the controls it is admitted to", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const view = mount();
    expect(fetchGlossaryConflicts).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("SemanticAdmin");
    view.rerender(<GlossaryConflicts />);

    expect(await screen.findByRole("button", { name: "Customer vs Active customer" })).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(1);
    expect(detectButton()).toBeEnabled();
  });

  it("still reads when identity will not answer: the server stays the authority, and no write is offered", async () => {
    sessionState = "disconnected";
    sessionMe = null;
    mount();

    expect(await screen.findByRole("button", { name: "Customer vs Active customer" })).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(1);
    expect(detectButton()).not.toBeInTheDocument();
    expect(raiseButton()).not.toBeInTheDocument();
  });
});

describe("Conflicts: the list", () => {
  it("names each conflict, its type, its status, who raised it and who owns it", async () => {
    mount();

    const items = within(await screen.findByRole("list", { name: "Glossary conflicts" })).getAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(items[0]).toHaveTextContent("Customer vs Active customer");
    expect(items[0]).toHaveTextContent("Synonym collision");
    expect(items[0]).toHaveTextContent("open");
    expect(items[0]).toHaveTextContent("Raised by priya.steward at 2026-09-18 09:12 UTC · owner risk-data-stewards@tenant.example");
    // A metric collision is named from its metric names, and typed as what it is.
    expect(items[2]).toHaveTextContent("Monthly active customers vs MAC");
    expect(items[2]).toHaveTextContent("Metric formula collision");
    expect(items[3]).toHaveTextContent("review required");
    expect(items[4]).toHaveTextContent("resolved");
    expect(screen.getByText("1–5 of 5 conflicts")).toBeInTheDocument();
  });

  it("does not invent an owner for a conflict that has none", async () => {
    mount();
    const items = within(await screen.findByRole("list", { name: "Glossary conflicts" })).getAllByRole("listitem");

    expect(items[1]).toHaveTextContent("Raised by morgan.covering at 2026-09-18 09:12 UTC");
    expect(items[1]).not.toHaveTextContent("owner");
  });

  it("opens a conflict to BOTH positions and the reason it is one", async () => {
    mount();
    await openRow("Customer vs Active customer");

    const detail = await screen.findByRole("region", { name: "Detail of Customer vs Active customer" });
    expect(detail).toHaveTextContent("Two approved terms share the label “client” and define it differently.");
    const a = within(detail).getByRole("group", { name: "Position A" });
    const b = within(detail).getByRole("group", { name: "Position B" });
    expect(a).toHaveTextContent("Customer");
    expect(a).toHaveTextContent("A party that holds at least one open account.");
    expect(a).toHaveTextContent("Colliding label");
    expect(a).toHaveTextContent("client");
    expect(within(a).getByText("a1000000-0000-4000-8000-000000000001").tagName).toBe("CODE");
    expect(b).toHaveTextContent("Active customer");
    expect(b).toHaveTextContent("A customer with a posted transaction in the last 90 days.");
    // The toggle says it is open, and closes it again.
    const toggle = screen.getByRole("button", { name: "Customer vs Active customer" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(screen.queryByRole("region", { name: /Detail of/ })).not.toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("links each term that a position names to Business meaning, filtered to it", async () => {
    mount();
    await openRow("Customer vs Active customer");

    const links = within(await screen.findByRole("navigation", { name: "Read the terms" }));
    fireEvent.click(links.getByRole("button", { name: /Active customer/ }));

    expect(location.hash).toBe("#/steward/meaning");
    const params = new URLSearchParams(location.search);
    expect(params.get("view")).toBe("glossary");
    expect(params.get("q")).toBe("Active customer");
  });

  it("renders a steward-raised conflict from what it contains, and says only who raised it", async () => {
    mount();
    await openRow("Net revenue vs Net revenue");

    const detail = await screen.findByRole("region", { name: "Detail of Net revenue vs Net revenue" });
    expect(detail).toHaveTextContent("morgan.covering recorded two definitions that disagree.");
    expect(within(detail).getByRole("group", { name: "Position A" })).toHaveTextContent("Source");
    expect(within(detail).getByRole("group", { name: "Position A" })).toHaveTextContent("Finance handbook");
    expect(within(detail).getByRole("group", { name: "Position B" })).toHaveTextContent("Sales wiki");
    // No term id on either position: nothing to link.
    expect(screen.queryByRole("navigation", { name: "Read the terms" })).not.toBeInTheDocument();
  });

  it("renders a metric-formula collision as metrics, not as a broken glossary term", async () => {
    mount();
    await openRow("Monthly active customers vs MAC");

    const detail = await screen.findByRole("region", { name: "Detail of Monthly active customers vs MAC" });
    expect(detail).toHaveTextContent("Two published metrics compute the same thing under different names");
    expect(detail).toHaveTextContent("Every field of the formula is identical.");
    const a = within(detail).getByRole("group", { name: "Position A" });
    expect(a).toHaveTextContent("Monthly active customers");
    expect(a).toHaveTextContent("Aggregation");
    expect(a).toHaveTextContent("COUNT");
    expect(a).toHaveTextContent("Match kind");
    expect(a).toHaveTextContent("EXACT_MATCH");
    expect(within(a).getByText("d1000000-0000-4000-8000-000000000001").tagName).toBe("CODE");
    expect(screen.queryByRole("navigation", { name: "Read the terms" })).not.toBeInTheDocument();
  });

  it("shows a proposal that is waiting on a reviewer, and who cannot decide it", async () => {
    sessionMe = asRoles("DataSteward");
    mount();
    await openRow("Settlement date vs Settlement date");

    const detail = await screen.findByRole("region", { name: "Detail of Settlement date vs Settlement date" });
    expect(detail).toHaveTextContent("Proposed resolution");
    expect(detail).toHaveTextContent("Merge");
    expect(detail).toHaveTextContent("Settlement date is when the clearing house confirms the payment.");
    expect(detail).toHaveTextContent("Rationale: Both sources describe different events.");
    expect(detail).toHaveTextContent("Waiting for a decision in the Review queue. Whoever proposed this resolution cannot decide it");
    // Not resolvable again while it is in review.
    expect(within(detail).queryByRole("button", { name: "Propose a resolution" })).not.toBeInTheDocument();
    fireEvent.click(within(detail).getByRole("button", { name: "Glossary conflict reviews" }));
    expect(new URLSearchParams(location.search).get("type")).toBe("GLOSSARY_CONFLICT");
  });

  it("does not send a MetadataAdmin to a review queue that would refuse them", async () => {
    sessionMe = asRoles("MetadataAdmin");
    mount();
    await openRow("Settlement date vs Settlement date");

    const detail = await screen.findByRole("region", { name: "Detail of Settlement date vs Settlement date" });
    expect(detail).toHaveTextContent("Waiting for a decision in the Review queue");
    expect(within(detail).queryByRole("button", { name: "Glossary conflict reviews" })).not.toBeInTheDocument();
  });

  it("says a resolved conflict was resolved, by whom, and that neither term was edited", async () => {
    mount();
    await openRow("Balance vs Ledger balance");

    const detail = await screen.findByRole("region", { name: "Detail of Balance vs Ledger balance" });
    expect(detail).toHaveTextContent("Resolved by riya.reviewer at 2026-09-10 15:20 UTC.");
    expect(detail).toHaveTextContent("Both positions are still on the record; neither term was edited.");
    expect(within(detail).queryByRole("button", { name: "Propose a resolution" })).not.toBeInTheDocument();
  });

  it("filters by status on the server, in the URL, and reads an unknown status as every status", async () => {
    mount();
    await screen.findByRole("list", { name: "Glossary conflicts" });

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "REVIEW_REQUIRED" } });

    await waitFor(() =>
      expect(fetchGlossaryConflicts).toHaveBeenLastCalledWith(ORG, { status: "REVIEW_REQUIRED", limit: 25, offset: 0 }, expect.any(AbortSignal)),
    );
    expect(new URLSearchParams(location.search).get("status")).toBe("REVIEW_REQUIRED");
  });

  it("opens on the status a link names, and ignores one it does not know", async () => {
    const view = mount("/?status=RESOLVED#/steward/glossary-review");
    await screen.findByRole("list", { name: "Glossary conflicts" });
    expect(fetchGlossaryConflicts).toHaveBeenLastCalledWith(ORG, { status: "RESOLVED", limit: 25, offset: 0 }, expect.any(AbortSignal));
    expect(screen.getByLabelText("Status")).toHaveValue("RESOLVED");
    view.unmount();

    fetchGlossaryConflicts.mockClear();
    mount("/?status=BOGUS#/steward/glossary-review");
    await screen.findByRole("list", { name: "Glossary conflicts" });
    expect(fetchGlossaryConflicts).toHaveBeenLastCalledWith(ORG, { status: null, limit: 25, offset: 0 }, expect.any(AbortSignal));
    expect(screen.getByLabelText("Status")).toHaveValue("");
  });

  it("pages by the API's total, and goes back to page one when the filter changes", async () => {
    fetchGlossaryConflicts.mockImplementation(async (_org, query) => {
      const offset = query.offset ?? 0;
      const rows = Array.from({ length: Math.min(25, 60 - offset) }, (_, i) =>
        conflict({ id: `c-${offset + i}`, position_a: { display_name: `Term ${offset + i}` }, position_b: { display_name: "Other" } }),
      );
      return { items: rows, limit: 25, offset, total: 60 };
    });
    mount();

    expect(await screen.findByText("1–25 of 60 conflicts")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("26–50 of 60 conflicts")).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenLastCalledWith(ORG, { status: null, limit: 25, offset: 25 }, expect.any(AbortSignal));
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("51–60 of 60 conflicts")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "OPEN" } });
    await waitFor(() =>
      expect(fetchGlossaryConflicts).toHaveBeenLastCalledWith(ORG, { status: "OPEN", limit: 25, offset: 0 }, expect.any(AbortSignal)),
    );
  });

  it("steps back to the last page that exists when the current one has emptied", async () => {
    // Page 2 held one row. A detection run re-reads the list, and by then that row
    // has moved out of the filter: the server answers an empty page 2 with a total
    // of 25. That is not "no conflicts" -- it is a page past the end.
    detectGlossaryConflicts.mockResolvedValue(page([]));
    let moved = false;
    fetchGlossaryConflicts.mockImplementation(async (_org, query) => {
      const offset = query.offset ?? 0;
      const total = moved ? 25 : 26;
      if (offset === 0) {
        return { items: Array.from({ length: 25 }, (_, i) => conflict({ id: `c-${i}` })), limit: 25, offset: 0, total };
      }
      if (!moved) {
        moved = true; // this read shows the one row; the next finds it gone
        return { items: [conflict({ id: "c-last" })], limit: 25, offset: 25, total };
      }
      return { items: [], limit: 25, offset: 25, total };
    });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Next page" }));
    await screen.findByText("26–26 of 26 conflicts");

    fireEvent.click(screen.getByRole("button", { name: "Detect conflicts" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Detect conflicts" }));

    expect(await screen.findByText("1–25 of 25 conflicts")).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenLastCalledWith(ORG, { status: null, limit: 25, offset: 0 }, expect.any(AbortSignal));
    // Never the empty state: there are conflicts, this page just no longer exists.
    expect(screen.queryByText("No glossary conflicts")).not.toBeInTheDocument();
  });

  it("says there are no conflicts, and how to find some, to a steward", async () => {
    fetchGlossaryConflicts.mockResolvedValue(page([]));
    mount();

    expect(await screen.findByText("No glossary conflicts")).toBeInTheDocument();
    expect(screen.getByText(/Detect conflicts to look for approved terms/)).toBeInTheDocument();
  });

  it("says there are no conflicts, without offering to detect any, to a reader", async () => {
    sessionMe = asRoles("Viewer");
    fetchGlossaryConflicts.mockResolvedValue(page([]));
    mount();

    expect(await screen.findByText("No glossary conflicts")).toBeInTheDocument();
    expect(screen.getByText("No conflict has been detected or raised in this organization.")).toBeInTheDocument();
  });

  it("says a filter matched nothing, rather than that there are no conflicts", async () => {
    fetchGlossaryConflicts.mockResolvedValue(page([]));
    mount("/?status=RESOLVED#/steward/glossary-review");

    expect(await screen.findByText("No conflicts with status resolved")).toBeInTheDocument();
    expect(screen.queryByText("No glossary conflicts")).not.toBeInTheDocument();
  });

  it("says the list could not be loaded, with the server's words, and retries", async () => {
    fetchGlossaryConflicts.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    mount();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Glossary conflicts could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Customer vs Active customer" })).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(2);
  });
});

describe("Conflicts: detecting", () => {
  async function openDetect() {
    mount();
    await screen.findByRole("list", { name: "Glossary conflicts" });
    fireEvent.click(screen.getByRole("button", { name: "Detect conflicts" }));
    return await screen.findByRole("dialog", { name: "Detect glossary conflicts?" });
  }

  it("states the effect before it does anything, and sends nothing on the click", async () => {
    const dialog = await openDetect();

    expect(dialog).toHaveTextContent("up to 5,000 approved, active glossary terms");
    expect(dialog).toHaveTextContent("share a label (a display name or a synonym, ignoring case) but define it differently");
    expect(dialog).toHaveTextContent("raises an OPEN conflict for each new pair, at most 100 per run");
    expect(dialog).toHaveTextContent("Pairs already open or in review are skipped");
    // The honest consequence a steward would otherwise learn by surprise.
    expect(dialog).toHaveTextContent("a pair resolved earlier is raised again if its terms still collide, because a resolution edits neither term");
    expect(dialog).toHaveTextContent("Detection changes no term.");
    expect(detectGlossaryConflicts).not.toHaveBeenCalled();
  });

  it("runs once on confirm, says what it raised, and re-reads the list", async () => {
    detectGlossaryConflicts.mockResolvedValue(page([OPEN, MANUAL]));
    const dialog = await openDetect();

    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    await waitFor(() => expect(detectGlossaryConflicts).toHaveBeenCalledTimes(1));
    expect(detectGlossaryConflicts).toHaveBeenCalledWith(ORG, undefined);
    expect(await screen.findByText("Detection raised 2 new conflicts.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // What the list shows is the SERVER's answer to a second read, not an edit.
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(2);
  });

  it("says one conflict in the singular, and nothing new as nothing new", async () => {
    detectGlossaryConflicts.mockResolvedValueOnce(page([OPEN])).mockResolvedValueOnce(page([]));
    let dialog = await openDetect();
    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));
    expect(await screen.findByText("Detection raised 1 new conflict.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Detect conflicts" }));
    dialog = await screen.findByRole("dialog", { name: "Detect glossary conflicts?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));
    expect(await screen.findByText(/^Detection found nothing new: every colliding pair is already open or in review/)).toBeInTheDocument();
  });

  it("says when a run stopped at its limit, so a steward runs it again", async () => {
    detectGlossaryConflicts.mockResolvedValue(page(Array.from({ length: 100 }, (_, i) => conflict({ id: `c-${i}` }))));
    const dialog = await openDetect();

    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    expect(await screen.findByText(/Detection raised 100 new conflicts\. It stopped at its limit of 100 per run: run detection again for more\./)).toBeInTheDocument();
  });

  it("keeps the dialog open and shows a refusal in the server's own words, claiming nothing", async () => {
    detectGlossaryConflicts.mockRejectedValue(new ApiError(403, "requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin"));
    const dialog = await openDetect();

    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/Detection raised/)).not.toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(1);
  });

  it("admits one run at a time and shows the dialog busy meanwhile", async () => {
    let settle: (result: PageOf<GlossaryConflictRead>) => void = () => undefined;
    detectGlossaryConflicts.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openDetect();

    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));
    const working = await within(dialog).findByRole("button", { name: "Working…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);
    expect(detectGlossaryConflicts).toHaveBeenCalledTimes(1);
    settle(page([]));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("cancels without sending anything", async () => {
    const dialog = await openDetect();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(detectGlossaryConflicts).not.toHaveBeenCalled();
  });
});

describe("Conflicts: proposing a resolution", () => {
  async function openResolve(target = OPEN) {
    fetchGlossaryConflicts.mockResolvedValue(page([target]));
    mount();
    await openRow(`Customer vs Active customer`);
    fireEvent.click(await screen.findByRole("button", { name: "Propose a resolution" }));
    return await screen.findByRole("dialog", { name: "Propose a resolution" });
  }
  const decide = (dialog: HTMLElement, name: RegExp) => fireEvent.click(within(dialog).getByRole("radio", { name }));
  const rationale = (dialog: HTMLElement, value: string) =>
    fireEvent.change(within(dialog).getByLabelText("Rationale"), { target: { value } });
  const submit = (dialog: HTMLElement) => within(dialog).getByRole("button", { name: "Propose resolution" });

  it("is only offered on an OPEN conflict", async () => {
    for (const status of ["REVIEW_REQUIRED", "RESOLVED"] as const) {
      fetchGlossaryConflicts.mockResolvedValue(page([conflict({ status })]));
      const view = mount();
      await openRow("Customer vs Active customer");
      await screen.findByRole("region", { name: /Detail of/ });
      expect(screen.queryByRole("button", { name: "Propose a resolution" })).not.toBeInTheDocument();
      view.unmount();
    }
  });

  it("says a resolution is a proposal, offers exactly the decisions the API accepts, and sends nothing on the click", async () => {
    const dialog = await openResolve();

    expect(dialog).toHaveTextContent("This does not settle anything by itself");
    expect(dialog).toHaveTextContent("a different reviewer approves or rejects.");
    expect(dialog).toHaveTextContent("Neither edits either term's definition, and both positions stay on the record.");
    const radios = within(dialog).getAllByRole("radio");
    expect(radios.map((radio) => (radio as HTMLInputElement).value)).toEqual([
      "ACCEPT_POSITION_A", "ACCEPT_POSITION_B", "MERGE", "RETAIN_BOTH",
    ]);
    // No decision is pre-selected: choosing one is the act.
    expect(radios.every((radio) => !(radio as HTMLInputElement).checked)).toBe(true);
    expect(submit(dialog)).toBeDisabled();
    expect(submitGlossaryConflictResolution).not.toHaveBeenCalled();
    // Focus lands on the first decision, not on the close button.
    expect(document.activeElement).toBe(radios[0]);
  });

  it("will not confirm without a decision and a rationale of at least ten characters", async () => {
    const dialog = await openResolve();

    rationale(dialog, "long enough to be a reason");
    expect(submit(dialog)).toBeDisabled(); // no decision yet
    decide(dialog, /Retain both/);
    expect(submit(dialog)).toBeEnabled();
    rationale(dialog, "too short");
    expect(submit(dialog)).toBeDisabled(); // nine characters
    rationale(dialog, "          x          "); // whitespace does not count
    expect(submit(dialog)).toBeDisabled();
    rationale(dialog, "exactly 10");
    expect(submit(dialog)).toBeEnabled();
    expect(submitGlossaryConflictResolution).not.toHaveBeenCalled();
  });

  it("proposes an accepted position with the rationale, and no definition", async () => {
    submitGlossaryConflictResolution.mockResolvedValue(REVIEW);
    const dialog = await openResolve();

    decide(dialog, /Accept position B/);
    expect(within(dialog).queryByLabelText("Merged definition")).not.toBeInTheDocument();
    rationale(dialog, "  Active customer is the one finance reports.  ");
    fireEvent.click(submit(dialog));

    await waitFor(() => expect(submitGlossaryConflictResolution).toHaveBeenCalledTimes(1));
    expect(submitGlossaryConflictResolution).toHaveBeenCalledWith(
      OPEN.id,
      { resolution: "ACCEPT_POSITION_B", rationale: "Active customer is the one finance reports." },
      undefined,
    );
    expect(submitGlossaryConflictResolution.mock.calls[0]![1]).not.toHaveProperty("resolved_definition");
  });

  it("requires the merged definition for a merge, and sends it", async () => {
    submitGlossaryConflictResolution.mockResolvedValue(REVIEW);
    const dialog = await openResolve();

    decide(dialog, /Merge/);
    rationale(dialog, "Both describe the same customer at different times.");
    // A merge is decided on the definition it proposes.
    expect(submit(dialog)).toBeDisabled();
    expect(dialog).toHaveTextContent("it does not replace either term’s definition");
    fireEvent.change(within(dialog).getByLabelText("Merged definition"), { target: { value: "  A customer with an open account.  " } });
    expect(submit(dialog)).toBeEnabled();
    fireEvent.click(submit(dialog));

    await waitFor(() =>
      expect(submitGlossaryConflictResolution).toHaveBeenCalledWith(
        OPEN.id,
        {
          resolution: "MERGE",
          resolved_definition: "A customer with an open account.",
          rationale: "Both describe the same customer at different times.",
        },
        undefined,
      ),
    );
  });

  it("drops a definition typed for a merge if the steward then chooses something else", async () => {
    submitGlossaryConflictResolution.mockResolvedValue(REVIEW);
    const dialog = await openResolve();

    decide(dialog, /Merge/);
    fireEvent.change(within(dialog).getByLabelText("Merged definition"), { target: { value: "A merged text" } });
    decide(dialog, /Retain both/);
    rationale(dialog, "The difference is intended.");
    fireEvent.click(submit(dialog));

    await waitFor(() => expect(submitGlossaryConflictResolution).toHaveBeenCalledTimes(1));
    expect(submitGlossaryConflictResolution.mock.calls[0]![1]).toEqual({
      resolution: "RETAIN_BOTH", rationale: "The difference is intended.",
    });
  });

  it("says where it went, links to the review it opened, and re-reads the list", async () => {
    submitGlossaryConflictResolution.mockResolvedValue(REVIEW);
    const dialog = await openResolve();
    decide(dialog, /Retain both/);
    rationale(dialog, "The difference is intended.");

    fireEvent.click(submit(dialog));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const notice = await screen.findByText(/^Resolution proposed\. A governance review is waiting in the Review queue; someone other than you approves or rejects it/);
    expect(notice).toHaveTextContent("Approval marks the conflict resolved, rejection reopens it, and neither edits a term.");
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Open the Review queue" }));
    expect(location.hash).toBe("#/reviewer/governance");
    expect(new URLSearchParams(location.search).get("review")).toBe(REVIEW.id);
  });

  it("does not offer a MetadataAdmin the review queue, which would refuse them", async () => {
    sessionMe = asRoles("MetadataAdmin");
    submitGlossaryConflictResolution.mockResolvedValue(REVIEW);
    const dialog = await openResolve();
    decide(dialog, /Retain both/);
    rationale(dialog, "The difference is intended.");

    fireEvent.click(submit(dialog));

    expect(await screen.findByText(/^Resolution proposed\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open the Review queue" })).not.toBeInTheDocument();
  });

  it("keeps the dialog open on a refusal, shows it in the server's words, and does not claim a proposal", async () => {
    submitGlossaryConflictResolution.mockRejectedValue(new ApiError(403, "requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin"));
    const dialog = await openResolve();
    decide(dialog, /Retain both/);
    rationale(dialog, "The difference is intended.");

    fireEvent.click(submit(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/Resolution proposed/)).not.toBeInTheDocument();
    // A 403 changes nothing on the server, so the list is not re-read on its account.
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(1);
    // The steward can fix and retry from the same dialog, with what they typed.
    expect(submit(dialog)).toBeEnabled();
    expect(within(dialog).getByLabelText("Rationale")).toHaveValue("The difference is intended.");
  });

  it("shows the server's 409 verbatim and re-reads, because the row on screen no longer holds", async () => {
    submitGlossaryConflictResolution.mockRejectedValue(new ApiError(409, "only open conflicts can be resolved"));
    const dialog = await openResolve();
    decide(dialog, /Retain both/);
    rationale(dialog, "The difference is intended.");
    fetchGlossaryConflicts.mockResolvedValue(page([conflict({ status: "REVIEW_REQUIRED" })]));

    fireEvent.click(submit(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^only open conflicts can be resolved$/);
    await waitFor(() => expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(2));
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // The list behind it is now the truth: in review, so not resolvable.
    const list = await screen.findByRole("list", { name: "Glossary conflicts" });
    await waitFor(() => expect(within(list).getByText("review required")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Propose a resolution" })).not.toBeInTheDocument();
  });

  it("shows the server's validation sentence for a rationale it refused", async () => {
    submitGlossaryConflictResolution.mockRejectedValue(new ApiError(422, "body.rationale: String should have at least 10 characters"));
    const dialog = await openResolve();
    decide(dialog, /Retain both/);
    rationale(dialog, "long enough here");

    fireEvent.click(submit(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("body.rationale: String should have at least 10 characters");
  });

  it("admits one proposal at a time", async () => {
    let settle: (review: GovernanceReviewRead) => void = () => undefined;
    submitGlossaryConflictResolution.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openResolve();
    decide(dialog, /Retain both/);
    rationale(dialog, "The difference is intended.");

    fireEvent.click(submit(dialog));
    const working = await within(dialog).findByRole("button", { name: "Working…" });
    fireEvent.click(working);
    expect(submitGlossaryConflictResolution).toHaveBeenCalledTimes(1);
    settle(REVIEW);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not throw away a typed rationale on a click outside the dialog, and cancels without sending", async () => {
    const dialog = await openResolve();
    rationale(dialog, "The difference is intended.");

    fireEvent.mouseDown(dialog.parentElement!);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).getByLabelText("Rationale")).toHaveValue("The difference is intended.");

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(submitGlossaryConflictResolution).not.toHaveBeenCalled();
  });
});

describe("Conflicts: raising one", () => {
  async function openRaise() {
    mount();
    await screen.findByRole("list", { name: "Glossary conflicts" });
    fireEvent.click(screen.getByRole("button", { name: "Raise a conflict" }));
    return await screen.findByRole("dialog", { name: "Raise a conflict" });
  }
  const fill = (dialog: HTMLElement, label: string, value: string) =>
    fireEvent.change(within(dialog).getByLabelText(label), { target: { value } });

  it("states what raising does, offers the three types the API accepts, and will not confirm two empty positions", async () => {
    const dialog = await openRaise();

    expect(dialog).toHaveTextContent("It is created OPEN, assigned to the owner you name, and changes no term.");
    const types = within(dialog).getAllByRole("option").map((option) => (option as HTMLOptionElement).value);
    expect(types).toEqual(["DEFINITION", "SYNONYM_COLLISION", "SOURCE_DISAGREEMENT"]);
    const raise = within(dialog).getByRole("button", { name: "Raise conflict" });
    expect(raise).toBeDisabled();
    fill(dialog, "Position A name", "Net revenue");
    expect(raise).toBeDisabled(); // B says nothing yet
    fill(dialog, "Position B definition", "After returns and rebates.");
    expect(raise).toBeEnabled();
    expect(raiseGlossaryConflict).not.toHaveBeenCalled();
  });

  it("sends only what the steward wrote, then says it was raised and re-reads", async () => {
    raiseGlossaryConflict.mockResolvedValue(MANUAL);
    const dialog = await openRaise();

    fireEvent.change(within(dialog).getByLabelText("Conflict type"), { target: { value: "SOURCE_DISAGREEMENT" } });
    fill(dialog, "Position A name", "  Net revenue ");
    fill(dialog, "Position A definition", "After returns.");
    fill(dialog, "Position A source (optional)", "Finance handbook");
    fill(dialog, "Position B definition", "After returns and rebates.");
    fill(dialog, "Assigned owner (optional)", " finance-data@tenant.example ");
    fireEvent.click(within(dialog).getByRole("button", { name: "Raise conflict" }));

    await waitFor(() => expect(raiseGlossaryConflict).toHaveBeenCalledTimes(1));
    expect(raiseGlossaryConflict).toHaveBeenCalledWith(
      ORG,
      {
        conflict_type: "SOURCE_DISAGREEMENT",
        position_a: { display_name: "Net revenue", definition: "After returns.", source: "Finance handbook" },
        position_b: { definition: "After returns and rebates." },
        assigned_owner: "finance-data@tenant.example",
      },
      undefined,
    );
    expect(await screen.findByText("Raised “Net revenue vs Net revenue” as an open conflict.")).toBeInTheDocument();
    expect(fetchGlossaryConflicts).toHaveBeenCalledTimes(2);
  });

  it("omits the owner when none was given, and shows a refusal in the server's words", async () => {
    raiseGlossaryConflict.mockRejectedValue(new ApiError(403, "requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin"));
    const dialog = await openRaise();
    fill(dialog, "Position A name", "Net revenue");
    fill(dialog, "Position B name", "Net revenue");

    fireEvent.click(within(dialog).getByRole("button", { name: "Raise conflict" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin$/);
    expect(raiseGlossaryConflict.mock.calls[0]![1]).not.toHaveProperty("assigned_owner");
    expect(screen.queryByText(/as an open conflict/)).not.toBeInTheDocument();
  });
});

describe("Conflicts: accessibility", () => {
  it("has no WCAG A/AA violations with a conflict of each type open, and names every control", async () => {
    mount();
    await openRow("Customer vs Active customer");
    await openRow("Monthly active customers vs MAC");
    await openRow("Settlement date vs Settlement date");
    await screen.findByRole("region", { name: "Detail of Settlement date vs Settlement date" });

    const container = document.body;
    await expectNoAxeViolations(container);
    expect(
      unnamedFocusableElements(container).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no violations in the not-applicable and empty states", async () => {
    sessionMe = asRoles("AgentDeveloper");
    const outside = mount();
    await screen.findByText(/Not applicable to your roles/);
    await expectNoAxeViolations(outside.container);
    outside.unmount();

    sessionMe = asRoles("Viewer");
    fetchGlossaryConflicts.mockResolvedValue(page([]));
    const empty = mount();
    await screen.findByText("No glossary conflicts");
    await expectNoAxeViolations(empty.container);
  });

  it("has no violations with each dialog open", async () => {
    mount();
    await screen.findByRole("list", { name: "Glossary conflicts" });

    fireEvent.click(screen.getByRole("button", { name: "Detect conflicts" }));
    await screen.findByRole("dialog", { name: "Detect glossary conflicts?" });
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Raise a conflict" }));
    await screen.findByRole("dialog", { name: "Raise a conflict" });
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await openRow("Customer vs Active customer");
    fireEvent.click(await screen.findByRole("button", { name: "Propose a resolution" }));
    const dialog = await screen.findByRole("dialog", { name: "Propose a resolution" });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Merge/ }));
    await expectNoAxeViolations(document.body);
  });
});
