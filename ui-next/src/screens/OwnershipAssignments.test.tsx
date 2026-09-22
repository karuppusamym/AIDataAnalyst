import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { OwnershipAssignmentBulkReaffirmResult, OwnershipAssignmentRead } from "../lib/api";
import type { MeRead } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import type { PageOf } from "../lib/ui-types";
import { resetLocationCacheForTests } from "../lib/location";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { OwnershipAssignments } from "./OwnershipAssignments";

/* ---------------------------------------------------------------------------
   Ownership -> Assignments (R11-AUD08, part 2).

   The properties, and why each was a real way to get this list wrong:

     1. WHO SEES WHAT. Every role the read admits sees the list; only the four
        reaffirming roles are offered a control, and even they only on the rows the
        server would not refuse: the owner's own, or any row for a PlatformAdmin or
        MetadataAdmin. A DataSteward who is not the owner is told "not yours", not
        handed a button that earns a 403. A session outside the read's roles is not
        asked at all, and nothing is asked while `/v1/me` is in flight.
     2. A REAFFIRM IS THE SERVER'S ANSWER. The row on screen becomes the row the
        server returned; a refusal is shown as it was written and changes nothing.
     3. BULK IS PER ITEM, AND SAYS SO. The API takes 100 at most; a confirmation
        states the effect first; the result names every skipped row with the server's
        detail instead of a count that leaves the steward guessing which.
     4. THE LIST NEVER CLAIMS TO BE MORE THAN IT IS. Filters are the two the API has;
        "Load more" says how much is shown; a page boundary that repeats a row does
        not show it twice or stall the paging.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

type ListQuery = { subject_type?: string | null; subject_id?: string | null; limit?: number; offset?: number };
const fetchOwnershipAssignments =
  vi.fn<(organizationId: string, query?: ListQuery, signal?: AbortSignal) => Promise<PageOf<OwnershipAssignmentRead>>>();
const reaffirmOwnershipAssignment = vi.fn<(assignmentId: string, signal?: AbortSignal) => Promise<OwnershipAssignmentRead>>();
const bulkReaffirmOwnershipAssignments =
  vi.fn<(assignmentIds: string[], signal?: AbortSignal) => Promise<OwnershipAssignmentBulkReaffirmResult>>();
const navigateTo = vi.fn<(screen: string, params?: Record<string, string>) => void>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOwnershipAssignments: (organizationId: string, query?: ListQuery, signal?: AbortSignal) =>
      fetchOwnershipAssignments(organizationId, query, signal),
    reaffirmOwnershipAssignment: (assignmentId: string, signal?: AbortSignal) =>
      reaffirmOwnershipAssignment(assignmentId, signal),
    bulkReaffirmOwnershipAssignments: (assignmentIds: string[], signal?: AbortSignal) =>
      bulkReaffirmOwnershipAssignments(assignmentIds, signal),
  };
});
vi.mock("../lib/navigate", () => ({
  navigateTo: (screen: string, params?: Record<string, string>) => navigateTo(screen, params),
}));

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

const asPrincipal = (principal: string, ...roles: string[]): MeRead => ({
  principal_id: principal, principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});
const asRoles = (...roles: string[]): MeRead => asPrincipal("someone", ...roles);

const inDays = (days: number): string => new Date(Date.now() + days * 86_400_000).toISOString();

const row = (id: string, overrides: Partial<OwnershipAssignmentRead> = {}): OwnershipAssignmentRead => ({
  id, organization_id: ORG, subject_type: "TABLE", subject_id: `t_${id}`,
  owner_type: "INDIVIDUAL", owner_principal: "steward-1", assignment_kind: "MANUAL", source_rule_id: null,
  status: "ACTIVE", assigned_by: "pat.admin", expires_at: inDays(90),
  expiry_warning_emitted_at: null, reaffirmed_at: null, reaffirmed_by: null,
  created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
  ...overrides,
});
const MINE = row("a1");
const THEIRS = row("a2", { owner_principal: "morgan", owner_type: "GROUP", assignment_kind: "RULE", source_rule_id: "rule-1" });
const page = (items: OwnershipAssignmentRead[], total = items.length, offset = 0): PageOf<OwnershipAssignmentRead> => ({
  items, limit: 100, offset, total,
});

const READ_ONLY = ["Analyst", "Auditor", "DataAdmin", "Reviewer", "Viewer"];
const reaffirmButton = (subject: string) => screen.queryByRole("button", { name: new RegExp(`^(Reaffirm|Reaffirming…) TABLE ${subject}$`) });
const rowFor = (subject: string) => screen.getByText(subject).closest("tr")!;

beforeEach(() => {
  fetchOwnershipAssignments.mockReset();
  fetchOwnershipAssignments.mockResolvedValue(page([MINE, THEIRS]));
  reaffirmOwnershipAssignment.mockReset();
  bulkReaffirmOwnershipAssignments.mockReset();
  navigateTo.mockReset();
  sessionMe = null;
  sessionState = "connected";
  history.replaceState(null, "", "/#/steward/ownership");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Ownership assignments: who sees what", () => {
  it.each(READ_ONLY)("shows the list to %s and offers nothing to change", async (role) => {
    sessionMe = asRoles(role);
    render(<OwnershipAssignments />);

    expect(await screen.findByText("t_a1")).toBeInTheDocument();
    expect(fetchOwnershipAssignments).toHaveBeenCalledWith(
      ORG, { subject_type: null, subject_id: null, limit: 100 }, expect.any(AbortSignal),
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reaffirm/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Reaffirm" })).not.toBeInTheDocument();
    expect(screen.getByText(/Your roles can read ownership\. Reaffirming needs DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin/)).toBeInTheDocument();
  });

  it.each(["AgentDeveloper", "ToolDeveloper", "OrganizationAdmin"])(
    "asks for nothing as %s, and says so rather than showing an error",
    async (role) => {
      sessionMe = asRoles(role);
      render(<OwnershipAssignments />);

      expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
      expect(fetchOwnershipAssignments).not.toHaveBeenCalled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
    },
  );

  it("holds the read while identity is in flight, and sends it once identity admits the session", async () => {
    sessionState = "connecting";
    const view = render(<OwnershipAssignments />);

    expect(await screen.findByText(/Loading ownership assignments/)).toBeInTheDocument();
    expect(fetchOwnershipAssignments).not.toHaveBeenCalled();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();
    // Nothing that reaffirms is offered on a guess, and no "you cannot" is said on one either.
    expect(screen.queryByText(/Your roles can read ownership/)).not.toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("PlatformAdmin");
    view.rerender(<OwnershipAssignments />);

    expect(await screen.findByText("t_a1")).toBeInTheDocument();
    expect(fetchOwnershipAssignments).toHaveBeenCalledTimes(1);
  });

  it("never sends the read when identity then says the session may not", async () => {
    sessionState = "connecting";
    const view = render(<OwnershipAssignments />);
    await screen.findByText(/Loading ownership assignments/);

    sessionState = "connected";
    sessionMe = asRoles("AgentDeveloper");
    view.rerender(<OwnershipAssignments />);

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchOwnershipAssignments).not.toHaveBeenCalled();
  });

  it("still reads when identity will not answer, and offers no control", async () => {
    sessionState = "disconnected";
    render(<OwnershipAssignments />);

    expect(await screen.findByText("t_a1")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reaffirm/ })).not.toBeInTheDocument();
  });
});

describe("Ownership assignments: what each row says", () => {
  it("shows the subject, the owner and how it was assigned, expiry and last reaffirmation", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipAssignments.mockResolvedValue(
      page([
        MINE,
        row("a3", { expires_at: null }),
        row("a4", { expires_at: inDays(6), reaffirmed_at: "2026-08-01T10:30:00Z", reaffirmed_by: "steward-1" }),
        row("a5", { expires_at: inDays(-3) }),
      ]),
    );
    render(<OwnershipAssignments />);

    await screen.findByText("t_a1");
    expect(rowFor("t_a1")).toHaveTextContent("steward-1");
    expect(rowFor("t_a1")).toHaveTextContent("manual · assigned by pat.admin");
    expect(rowFor("t_a1")).toHaveTextContent(/expires \d{4}-\d{2}-\d{2} \(in 9\d days\)/);
    expect(rowFor("t_a1")).toHaveTextContent("never");
    // A row written before P2-07 has no expiry: said as that, not as a defect.
    expect(rowFor("t_a3")).toHaveTextContent("no expiry set");
    expect(rowFor("t_a4")).toHaveTextContent("expires");
    expect(rowFor("t_a4")).toHaveTextContent("(in 6 days)");
    expect(rowFor("t_a4")).toHaveTextContent("2026-08-01 10:30 UTC by steward-1");
    // Past its expiry, and still listed: inside the grace period, so not "expired" outright.
    expect(rowFor("t_a5")).toHaveTextContent("past its expiry");
    expect(rowFor("t_a5")).toHaveTextContent("3 days ago");
  });

  it("says which rows a rule assigned", async () => {
    sessionMe = asRoles("Viewer");
    render(<OwnershipAssignments />);

    await screen.findByText("t_a2");
    expect(rowFor("t_a2")).toHaveTextContent("rule (by a rule)");
    expect(rowFor("t_a2")).toHaveTextContent("morgan");
    expect(rowFor("t_a2")).toHaveTextContent("group");
  });

  it("opens a table in the Catalog, and offers no such link for a glossary term", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipAssignments.mockResolvedValue(page([MINE, row("a6", { subject_type: "TERM", subject_id: "term-1" })]));
    render(<OwnershipAssignments />);

    await screen.findByText("t_a1");
    fireEvent.click(within(rowFor("t_a1")).getByRole("button", { name: /Catalog/ }));
    expect(navigateTo).toHaveBeenCalledWith("catalog", { asset: "t_a1" });
    expect(within(rowFor("term-1")).queryByRole("button", { name: /Catalog/ })).not.toBeInTheDocument();
  });

  it("keeps the scrolling table reachable by keyboard, named, even when nothing in it is focusable", async () => {
    // A region that scrolls has to take focus (WCAG 2.1.1); a read-only list of glossary terms has no
    // checkbox and no Catalog link to be the thing that does.
    sessionMe = asRoles("Viewer");
    fetchOwnershipAssignments.mockResolvedValue(page([row("a6", { subject_type: "TERM", subject_id: "term-1" })]));
    render(<OwnershipAssignments />);

    const scroller = await screen.findByRole("region", { name: "Assignments table, scrollable" });

    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(within(scroller).getByRole("table", { name: "Ownership assignments" })).toBeInTheDocument();
    expect(within(scroller).queryAllByRole("button")).toHaveLength(0);
  });

  it("says an organization with no assignments has none", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipAssignments.mockResolvedValue(page([]));
    render(<OwnershipAssignments />);

    expect(await screen.findByText("No active ownership assignments")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says the read failed, verbatim, and retries -- never an empty list", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipAssignments.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    render(<OwnershipAssignments />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Ownership assignments could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    expect(screen.queryByText("No active ownership assignments")).not.toBeInTheDocument();
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("t_a1")).toBeInTheDocument();
  });
});

describe("Ownership assignments: filtering", () => {
  it("sends the two filters the API has, in the URL, and clears them", async () => {
    sessionMe = asRoles("Viewer");
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    fireEvent.change(screen.getByLabelText("Subject type"), { target: { value: " term " } });
    fireEvent.change(screen.getByLabelText("Subject id"), { target: { value: "term-1" } });
    fetchOwnershipAssignments.mockResolvedValue(page([row("a6", { subject_type: "TERM", subject_id: "term-1" })]));
    fireEvent.click(screen.getByRole("button", { name: "Apply filter" }));

    await screen.findByText("term-1");
    expect(fetchOwnershipAssignments).toHaveBeenLastCalledWith(
      ORG, { subject_type: "term", subject_id: "term-1", limit: 100 }, expect.any(AbortSignal),
    );
    const params = new URLSearchParams(location.search);
    expect(params.get("subject_type")).toBe("term");
    expect(params.get("subject_id")).toBe("term-1");

    fetchOwnershipAssignments.mockResolvedValue(page([MINE, THEIRS]));
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    await screen.findByText("t_a1");
    expect(fetchOwnershipAssignments).toHaveBeenLastCalledWith(
      ORG, { subject_type: null, subject_id: null, limit: 100 }, expect.any(AbortSignal),
    );
    expect(new URLSearchParams(location.search).get("subject_type")).toBeNull();
  });

  it("opens from a link that carries a filter", async () => {
    sessionMe = asRoles("Viewer");
    history.replaceState(null, "", "/?subject_type=TABLE&subject_id=t_a1#/steward/ownership");
    resetLocationCacheForTests();
    render(<OwnershipAssignments />);

    await screen.findByText("t_a1");
    expect(fetchOwnershipAssignments).toHaveBeenCalledWith(
      ORG, { subject_type: "TABLE", subject_id: "t_a1", limit: 100 }, expect.any(AbortSignal),
    );
    expect(screen.getByLabelText("Subject type")).toHaveValue("TABLE");
  });

  it("says a filter that matches nothing matched nothing, and how the two fields match", async () => {
    sessionMe = asRoles("Viewer");
    history.replaceState(null, "", "/?subject_id=nope#/steward/ownership");
    resetLocationCacheForTests();
    fetchOwnershipAssignments.mockResolvedValue(page([]));
    render(<OwnershipAssignments />);

    expect(await screen.findByText("No active ownership matches this filter")).toBeInTheDocument();
    expect(screen.getByText(/matched ignoring capitals; the subject id is matched exactly/)).toBeInTheDocument();
    expect(screen.queryByText("No active ownership assignments")).not.toBeInTheDocument();
  });
});

describe("Ownership assignments: paging", () => {
  it("says how much of the list is shown, appends the next page, and never shows a repeated row twice", async () => {
    sessionMe = asRoles("Viewer");
    const first = Array.from({ length: 3 }, (_, index) => row(`p${index}`));
    fetchOwnershipAssignments.mockResolvedValueOnce(page(first, 8));
    render(<OwnershipAssignments />);

    const more = await screen.findByRole("button", { name: "Load more (3 of 8 shown)" });
    expect(screen.getByText("3 of 8 shown")).toBeInTheDocument();
    // The listing pages by creation time, so a boundary can repeat a row: p2 comes again.
    fetchOwnershipAssignments.mockResolvedValueOnce(page([row("p2"), row("p3"), row("p4")], 8, 3));
    fireEvent.click(more);

    await screen.findByText("t_p4");
    expect(fetchOwnershipAssignments).toHaveBeenLastCalledWith(
      ORG, { subject_type: null, subject_id: null, limit: 100, offset: 3 }, undefined,
    );
    expect(screen.getAllByText("t_p2")).toHaveLength(1);
    expect(screen.getByText("5 of 8 shown")).toBeInTheDocument();

    // Six rows have been SENT for five distinct ones, so the next page starts at 6, not at 5: paging
    // by the deduplicated length would ask for the same page again.
    fetchOwnershipAssignments.mockResolvedValueOnce(page([row("p5"), row("p6"), row("p7")], 8, 6));
    fireEvent.click(screen.getByRole("button", { name: /Load more/ }));

    await screen.findByText("t_p7");
    expect(fetchOwnershipAssignments).toHaveBeenLastCalledWith(
      ORG, { subject_type: null, subject_id: null, limit: 100, offset: 6 }, undefined,
    );
    expect(screen.getByText("8 of 8 shown")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Load more/ })).not.toBeInTheDocument();
  });

  it("says the next page could not be loaded and keeps what is already shown", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipAssignments.mockResolvedValueOnce(page([MINE], 2));
    render(<OwnershipAssignments />);
    const more = await screen.findByRole("button", { name: /Load more/ });
    fetchOwnershipAssignments.mockRejectedValueOnce(new ApiError(500, "index unavailable"));

    fireEvent.click(more);

    expect(await screen.findByText("More results could not be loaded: index unavailable")).toBeInTheDocument();
    expect(screen.getByText("t_a1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Load more/ })).toBeEnabled();
  });
});

describe("Ownership assignments: who may reaffirm what", () => {
  it("offers a DataSteward a control on the assignments they own and 'not yours' on the rest", async () => {
    sessionMe = asPrincipal("steward-1", "DataSteward");
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    expect(reaffirmButton("t_a1")).toBeEnabled();
    expect(reaffirmButton("t_a2")).not.toBeInTheDocument();
    expect(rowFor("t_a2")).toHaveTextContent("not yours");
    expect(within(rowFor("t_a1")).getByRole("checkbox")).toBeEnabled();
    expect(within(rowFor("t_a2")).getByRole("checkbox")).toBeDisabled();
    expect(screen.getByText(/You can reaffirm the assignments you own\. A PlatformAdmin or MetadataAdmin can reaffirm any of them\./)).toBeInTheDocument();
  });

  it.each(["SemanticAdmin", "DataSteward"])("does not let %s reaffirm someone else's assignment", async (role) => {
    sessionMe = asPrincipal("not-the-owner", role);
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    expect(screen.queryByRole("button", { name: /^Reaffirm TABLE/ })).not.toBeInTheDocument();
    expect(screen.getAllByText("not yours")).toHaveLength(2);
  });

  it.each(["PlatformAdmin", "MetadataAdmin"])("lets %s reaffirm any assignment, owner or not", async (role) => {
    sessionMe = asPrincipal("not-the-owner", role);
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    expect(reaffirmButton("t_a1")).toBeEnabled();
    expect(reaffirmButton("t_a2")).toBeEnabled();
    expect(screen.queryByText("not yours")).not.toBeInTheDocument();
    expect(screen.queryByText(/You can reaffirm the assignments you own/)).not.toBeInTheDocument();
  });

  it("does not offer a control to a session with no principal, even holding a role", async () => {
    sessionMe = { ...asRoles("DataSteward"), principal_id: undefined as unknown as string };
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    expect(screen.queryByRole("button", { name: /^Reaffirm TABLE/ })).not.toBeInTheDocument();
  });
});

describe("Ownership assignments: reaffirming one", () => {
  it("sends the assignment's id and replaces the row with the server's answer", async () => {
    sessionMe = asPrincipal("steward-1", "DataSteward");
    reaffirmOwnershipAssignment.mockResolvedValue(
      row("a1", { expires_at: inDays(180), reaffirmed_at: "2026-09-21T09:00:00Z", reaffirmed_by: "steward-1" }),
    );
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    fireEvent.click(reaffirmButton("t_a1")!);

    await waitFor(() => expect(reaffirmOwnershipAssignment).toHaveBeenCalledTimes(1));
    expect(reaffirmOwnershipAssignment).toHaveBeenCalledWith("a1", undefined);
    expect(await screen.findByText(/Reaffirmed TABLE t_a1; expires \d{4}-\d{2}-\d{2} \(in 18\d days\)\./)).toBeInTheDocument();
    expect(rowFor("t_a1")).toHaveTextContent("2026-09-21 09:00 UTC by steward-1");
    // One reaffirm is one call: the list was not re-read on its account.
    expect(fetchOwnershipAssignments).toHaveBeenCalledTimes(1);
  });

  it("shows a refusal as the server wrote it and leaves the row as it was", async () => {
    sessionMe = asPrincipal("steward-1", "DataSteward");
    reaffirmOwnershipAssignment.mockRejectedValue(
      new ApiError(403, "only the owner or an admin may reaffirm this ownership assignment"),
    );
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    fireEvent.click(reaffirmButton("t_a1")!);

    const strip = await screen.findByText("only the owner or an admin may reaffirm this ownership assignment");
    expect(strip).toHaveAttribute("role", "status");
    expect(rowFor("t_a1")).toHaveTextContent("never");
  });

  it("admits one reaffirm at a time", async () => {
    sessionMe = asPrincipal("steward-1", "DataSteward");
    fetchOwnershipAssignments.mockResolvedValue(page([MINE, row("a7")]));
    let settle: (updated: OwnershipAssignmentRead) => void = () => undefined;
    reaffirmOwnershipAssignment.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    fireEvent.click(reaffirmButton("t_a1")!);

    expect(await screen.findByRole("button", { name: "Reaffirming… TABLE t_a1" })).toBeDisabled();
    expect(reaffirmButton("t_a7")).toBeDisabled();
    fireEvent.click(reaffirmButton("t_a7")!);
    expect(reaffirmOwnershipAssignment).toHaveBeenCalledTimes(1);
    settle(MINE);
    await waitFor(() => expect(reaffirmButton("t_a7")).toBeEnabled());
  });
});

describe("Ownership assignments: reaffirming many", () => {
  async function selectBoth() {
    sessionMe = asPrincipal("steward-1", "PlatformAdmin");
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");
    fireEvent.click(within(rowFor("t_a1")).getByRole("checkbox"));
    fireEvent.click(within(rowFor("t_a2")).getByRole("checkbox"));
  }
  const bulkTrigger = () => screen.getByRole("button", { name: "Reaffirm selected" });

  it("counts what is selected and keeps the button off until something is", async () => {
    sessionMe = asPrincipal("steward-1", "PlatformAdmin");
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    expect(bulkTrigger()).toBeDisabled();
    expect(screen.getByText("0 selected")).toBeInTheDocument();
    fireEvent.click(within(rowFor("t_a1")).getByRole("checkbox"));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(bulkTrigger()).toBeEnabled();
  });

  it("states the effect before sending anything, and sends nothing on the click or on Cancel", async () => {
    await selectBoth();

    bulkTrigger().focus();
    fireEvent.click(bulkTrigger());

    const dialog = await screen.findByRole("dialog", { name: "Reaffirm 2 ownerships?" });
    expect(dialog).toHaveTextContent("You attest that each owner still owns the asset it names.");
    expect(dialog).toHaveTextContent("180 days unless it was configured differently");
    expect(dialog).toHaveTextContent("The owners do not change");
    expect(dialog).toHaveTextContent("one that the server refuses is skipped and listed afterwards with its reason");
    expect(dialog).toHaveTextContent("recorded in the audit ledger, one entry per assignment");
    expect(bulkReaffirmOwnershipAssignments).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(bulkReaffirmOwnershipAssignments).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(bulkTrigger());
  });

  it("sends exactly the selected ids, names every skipped row with the server's detail, and re-reads the list", async () => {
    await selectBoth();
    bulkReaffirmOwnershipAssignments.mockResolvedValue({
      reaffirmed: 1,
      skipped: 1,
      items: [
        { assignment_id: "a1", outcome: "REAFFIRMED", detail: null },
        { assignment_id: "a2", outcome: "FORBIDDEN", detail: "only the owner or an admin may reaffirm" },
      ],
    });
    fireEvent.click(bulkTrigger());
    const dialog = await screen.findByRole("dialog");
    fetchOwnershipAssignments.mockResolvedValueOnce(page([row("a1", { expires_at: inDays(180) }), THEIRS]));

    fireEvent.click(within(dialog).getByRole("button", { name: "Reaffirm" }));

    await waitFor(() => expect(bulkReaffirmOwnershipAssignments).toHaveBeenCalledTimes(1));
    expect(bulkReaffirmOwnershipAssignments).toHaveBeenCalledWith(["a1", "a2"], undefined);
    const result = await screen.findByRole("status", { name: "Bulk reaffirm result" });
    expect(result).toHaveTextContent("1 reaffirmed, 1 skipped");
    expect(result).toHaveTextContent("some skipped");
    const skipped = within(result).getByRole("list", { name: "Skipped assignments" });
    expect(within(skipped).getAllByRole("listitem")).toHaveLength(1);
    expect(skipped).toHaveTextContent("TABLE t_a2");
    expect(skipped).toHaveTextContent("forbidden");
    expect(skipped).toHaveTextContent("only the owner or an admin may reaffirm");
    // The list is the server's second answer, and the selection is spent.
    await waitFor(() => expect(fetchOwnershipAssignments).toHaveBeenCalledTimes(2));
    expect(screen.getByText("0 selected")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("reports a clean run as one, with nothing listed as skipped", async () => {
    await selectBoth();
    bulkReaffirmOwnershipAssignments.mockResolvedValue({
      reaffirmed: 2,
      skipped: 0,
      items: [
        { assignment_id: "a1", outcome: "REAFFIRMED", detail: null },
        { assignment_id: "a2", outcome: "REAFFIRMED", detail: null },
      ],
    });
    fireEvent.click(bulkTrigger());
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Reaffirm" }));

    const result = await screen.findByRole("status", { name: "Bulk reaffirm result" });
    expect(result).toHaveTextContent("2 reaffirmed, 0 skipped");
    expect(result).toHaveTextContent("all done");
    expect(within(result).queryByRole("list")).not.toBeInTheDocument();
  });

  it("keeps the confirmation open on a refusal and shows the server's words", async () => {
    await selectBoth();
    bulkReaffirmOwnershipAssignments.mockRejectedValue(
      new ApiError(422, "body.assignment_ids: List should have at most 100 items after validation, not 101"),
    );
    fireEvent.click(bulkTrigger());
    const dialog = await screen.findByRole("dialog");

    fireEvent.click(within(dialog).getByRole("button", { name: "Reaffirm" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "body.assignment_ids: List should have at most 100 items after validation, not 101",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Bulk reaffirm result" })).not.toBeInTheDocument();
    // Still selected, so the steward can retry.
    expect(screen.getByText("2 selected")).toBeInTheDocument();
  });

  it("never selects more than the API takes, and says so", async () => {
    sessionMe = asPrincipal("someone", "PlatformAdmin");
    const many = Array.from({ length: 120 }, (_, index) => row(`m${index}`));
    fetchOwnershipAssignments.mockResolvedValue({ items: many, limit: 500, offset: 0, total: 120 });
    render(<OwnershipAssignments />);
    await screen.findByText("t_m0");

    fireEvent.click(screen.getByLabelText("Select every assignment you can reaffirm on this page"));

    expect(screen.getByText(/100 selected \(the API takes at most 100 at a time\)/)).toBeInTheDocument();
    expect(screen.getByLabelText("Select TABLE t_m0")).toBeChecked();
    expect(screen.getByLabelText("Select TABLE t_m99")).toBeChecked();
    // The 101st cannot be ticked.
    expect(screen.getByLabelText("Select TABLE t_m100")).toBeDisabled();
    fireEvent.click(bulkTrigger());
    expect(await screen.findByRole("dialog", { name: "Reaffirm 100 ownerships?" })).toBeInTheDocument();
    // A 120-row table is the slow case here: role queries walk every row, so this case takes its own budget
    // rather than the 5 s default, which a busy machine has been seen to exceed.
  }, 30_000);

  it("only ever puts the rows a steward could reaffirm into a request", async () => {
    sessionMe = asPrincipal("steward-1", "DataSteward");
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select every assignment you can reaffirm on this page" }));

    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(within(rowFor("t_a2")).getByRole("checkbox")).not.toBeChecked();
  });

  it("clears the selection when the filter changes", async () => {
    await selectBoth();
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Subject id"), { target: { value: "t_a1" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filter" }));

    await waitFor(() => expect(screen.getByText("0 selected")).toBeInTheDocument());
  });
});

describe("Ownership assignments: accessibility", () => {
  it("has no WCAG A/AA violations with reaffirm controls, a status and a bulk result on screen", async () => {
    sessionMe = asPrincipal("steward-1", "PlatformAdmin");
    reaffirmOwnershipAssignment.mockResolvedValue(row("a1", { expires_at: inDays(180) }));
    const view = render(<OwnershipAssignments />);
    await screen.findByText("t_a1");
    fireEvent.click(reaffirmButton("t_a1")!);
    await screen.findByText(/Reaffirmed TABLE t_a1/);

    await expectNoAxeViolations(view.container);
    expect(
      unnamedFocusableElements(view.container).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no WCAG A/AA violations with the confirmation open", async () => {
    sessionMe = asPrincipal("steward-1", "PlatformAdmin");
    render(<OwnershipAssignments />);
    await screen.findByText("t_a1");
    fireEvent.click(within(rowFor("t_a1")).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Reaffirm selected" }));
    await screen.findByRole("dialog");

    await expectNoAxeViolations(document.body);
  });

  it("has no WCAG A/AA violations for a read-only session", async () => {
    sessionMe = asRoles("Auditor");
    const view = render(<OwnershipAssignments />);
    await screen.findByText("t_a1");

    await expectNoAxeViolations(view.container);
  });
});
