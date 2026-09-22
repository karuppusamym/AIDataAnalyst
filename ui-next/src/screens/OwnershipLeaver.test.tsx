import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { OwnershipAssignmentRead, OwnershipOwnerType, OwnershipPortfolio } from "../lib/api";
import type { BulkStewardshipOperationRead, LeaverReassignmentRequest, MeRead } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import type { PageOf } from "../lib/ui-types";
import { hasUnsavedChanges, resetUnsavedRegistryForTests } from "../lib/unsavedChanges";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { OwnershipLeaver } from "./OwnershipLeaver";

/* ---------------------------------------------------------------------------
   Ownership -> Leaver reassignment (R11-AUD08, part 2; GL-7).

   The properties, and why each was a real way to get a request this consequential wrong:

     1. WHO MAY ASK. Only the four roles the write admits are offered the form; a
        read-only session is told what it would take. Nothing is read for a session
        outside the read's roles, or while `/v1/me` is in flight, and no control is
        offered on a guess.
     2. THE CONFIRMATION SAYS WHAT A REQUEST IS. Nothing moves until a DIFFERENT
        reviewer approves; certifications are untouched; at most 500; and whether the
        steward saw the list. Nothing is sent on the click, only on the confirmation.
     3. THE PREVIEW IS A PREVIEW. It is the assignments listing filtered here, it says
        when it stopped before the end, it is dropped when it is no longer about the
        principal in the box, and the request does not depend on it.
     4. WHAT IS SENT IS WHAT WAS SHOWN. All rows ticked (or no preview) sends no ids and
        lets the server take the whole portfolio; unticking sends exactly the ticked ids;
        a subset the API would refuse (over 500) is not sent.
     5. THE RESULT IS NOT "DONE". A 202 is "review requested, 0 changed so far", with
        what the request leaves out said in words; a refusal is the server's, verbatim, in
        the dialog; the form is cleared so the same request cannot be sent twice.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchOwnershipPortfolio =
  vi.fn<(organizationId: string, principal: string, ownerType: OwnershipOwnerType, signal?: AbortSignal) => Promise<OwnershipPortfolio>>();
const requestLeaverReassignment =
  vi.fn<(organizationId: string, body: LeaverReassignmentRequest, signal?: AbortSignal) => Promise<BulkStewardshipOperationRead>>();
const fetchOwnershipOperations =
  vi.fn<(organizationId: string, query?: { limit?: number }, signal?: AbortSignal) => Promise<PageOf<BulkStewardshipOperationRead>>>();
const navigateTo = vi.fn<(screen: string, params?: Record<string, string>) => void>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOwnershipPortfolio: (organizationId: string, principal: string, ownerType: OwnershipOwnerType, signal?: AbortSignal) =>
      fetchOwnershipPortfolio(organizationId, principal, ownerType, signal),
    requestLeaverReassignment: (organizationId: string, body: LeaverReassignmentRequest, signal?: AbortSignal) =>
      requestLeaverReassignment(organizationId, body, signal),
    fetchOwnershipOperations: (organizationId: string, query?: { limit?: number }, signal?: AbortSignal) =>
      fetchOwnershipOperations(organizationId, query, signal),
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

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const holding = (id: string, overrides: Partial<OwnershipAssignmentRead> = {}): OwnershipAssignmentRead => ({
  id, organization_id: ORG, subject_type: "TABLE", subject_id: `t_${id}`,
  owner_type: "INDIVIDUAL", owner_principal: "priya", assignment_kind: "MANUAL", source_rule_id: null,
  status: "ACTIVE", assigned_by: "pat.admin", expires_at: null,
  expiry_warning_emitted_at: null, reaffirmed_at: null, reaffirmed_by: null,
  created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
  ...overrides,
});
const H1 = holding("h1");
const H2 = holding("h2");
const H3 = holding("h3", { subject_type: "TERM", subject_id: "term-3" });
const portfolio = (items: OwnershipAssignmentRead[], overrides: Partial<OwnershipPortfolio> = {}): OwnershipPortfolio => ({
  items, total: 40, scanned: 40, complete: true, ...overrides,
});

const operation = (overrides: Partial<BulkStewardshipOperationRead> = {}): BulkStewardshipOperationRead => ({
  id: "op-1", organization_id: ORG, operation_type: "REASSIGN_LEAVER", subject_type: "OWNERSHIP_ASSIGNMENT",
  subject_ids: ["h1", "h2", "h3"],
  parameters: {
    leaving_principal: "priya", successor_principal: "morgan", owner_type: "INDIVIDUAL",
    rationale: "Priya left the bank on 2026-09-18.", selection_mode: "FILTER", selection_truncated: false,
  },
  status: "REVIEW_REQUIRED", governance_review_id: "review-1", requested_by: "pat.admin",
  applied_by: null, applied_at: null, applied_count: 0, applied_subject_ids: [],
  reverses_operation_id: null, review_audit_sample_id: null,
  created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-20T10:00:00Z",
  ...overrides,
});
const page = <T,>(items: T[]): PageOf<T> => ({ items, limit: 500, offset: 0, total: items.length });

const WRITERS = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];
const READ_ONLY = ["Analyst", "Auditor", "DataAdmin", "Reviewer", "Viewer"];

const RATIONALE = "Priya left the bank on 2026-09-18.";
const fill = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const form = () => screen.queryByRole("region", { name: "Request a leaver reassignment" });
const requestButton = () => screen.getByRole("button", { name: "Request reassignment…" });

/** A valid form, ready to submit. */
async function fillForm(successor = "morgan") {
  sessionMe = asRoles("DataSteward");
  render(<OwnershipLeaver />);
  await screen.findByRole("button", { name: "Request reassignment…" });
  fill("Leaving principal", "priya");
  fill("Successor principal", successor);
  fill("Rationale", RATIONALE);
}

async function previewIt(items = [H1, H2, H3], overrides: Partial<OwnershipPortfolio> = {}) {
  fetchOwnershipPortfolio.mockResolvedValue(portfolio(items, overrides));
  fireEvent.click(screen.getByRole("button", { name: "Preview what they hold" }));
  await screen.findByLabelText("Preview of the leaver's ownerships");
}

async function openConfirm() {
  requestButton().focus();
  fireEvent.click(requestButton());
  return await screen.findByRole("dialog");
}

beforeEach(() => {
  fetchOwnershipPortfolio.mockReset();
  requestLeaverReassignment.mockReset();
  fetchOwnershipOperations.mockReset();
  fetchOwnershipOperations.mockResolvedValue(page([]));
  navigateTo.mockReset();
  sessionMe = null;
  sessionState = "connected";
  resetUnsavedRegistryForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetUnsavedRegistryForTests();
});

describe("Leaver reassignment: who may ask", () => {
  it.each(WRITERS)("offers %s the form and the preview", async (role) => {
    sessionMe = asRoles(role);
    render(<OwnershipLeaver />);

    expect(await screen.findByRole("button", { name: "Request reassignment…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview what they hold" })).toBeInTheDocument();
    expect(screen.queryByText(/Requesting one needs/)).not.toBeInTheDocument();
  });

  it.each(READ_ONLY)("offers %s no form, says what it takes, and still shows the requests", async (role) => {
    sessionMe = asRoles(role);
    fetchOwnershipOperations.mockResolvedValue(page([operation()]));
    render(<OwnershipLeaver />);

    expect(await screen.findByText("priya → morgan")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request reassignment…" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Leaving principal")).not.toBeInTheDocument();
    expect(screen.getByText("Requesting one needs DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin. Your roles can read the requests below.")).toBeInTheDocument();
    expect(form()).toBeInTheDocument(); // the panel itself is there, explaining
  });

  it.each(["AgentDeveloper", "ToolDeveloper"])("asks for nothing as %s", async (role) => {
    sessionMe = asRoles(role);
    render(<OwnershipLeaver />);

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchOwnershipOperations).not.toHaveBeenCalled();
    expect(fetchOwnershipPortfolio).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Request reassignment…" })).not.toBeInTheDocument();
  });

  it("holds every read and offers no control while identity is in flight, then offers the form once it says the session may", async () => {
    sessionState = "connecting";
    const view = render(<OwnershipLeaver />);

    expect(await screen.findByText(/Loading requests/)).toBeInTheDocument();
    expect(fetchOwnershipOperations).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Request reassignment…" })).not.toBeInTheDocument();
    // Neither "you cannot" nor "does not apply" is said on a guess.
    expect(screen.queryByText(/Requesting one needs/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("DataSteward");
    view.rerender(<OwnershipLeaver />);

    expect(await screen.findByRole("button", { name: "Request reassignment…" })).toBeInTheDocument();
    await waitFor(() => expect(fetchOwnershipOperations).toHaveBeenCalledTimes(1));
  });

  it("still reads the requests when identity will not answer, and offers no form", async () => {
    sessionState = "disconnected";
    fetchOwnershipOperations.mockResolvedValue(page([operation()]));
    render(<OwnershipLeaver />);

    expect(await screen.findByText("priya → morgan")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request reassignment…" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Requesting one needs/)).not.toBeInTheDocument();
  });

  it("says a request is not a transfer, before asking for anything", async () => {
    sessionMe = asRoles("DataSteward");
    render(<OwnershipLeaver />);

    await screen.findByRole("button", { name: "Request reassignment…" });
    expect(screen.getByText(/This is a request, not a transfer\. It asks a reviewer to make the successor the owner of what the leaver owns/)).toBeInTheDocument();
    expect(screen.getByText(/nothing moves until a different reviewer approves it\. Certifications the leaver granted are not changed/)).toBeInTheDocument();
  });
});

describe("Leaver reassignment: the form", () => {
  it("keeps the request off until it is complete, and says what is still needed", async () => {
    sessionMe = asRoles("DataSteward");
    render(<OwnershipLeaver />);
    await screen.findByRole("button", { name: "Request reassignment…" });

    expect(requestButton()).toBeDisabled();
    // An untouched form is not covered in complaints.
    expect(screen.queryByRole("list", { name: "What is still needed" })).not.toBeInTheDocument();

    fill("Leaving principal", "priya");
    const needed = screen.getByRole("list", { name: "What is still needed" });
    expect(needed).toHaveTextContent("Name the successor.");
    expect(needed).toHaveTextContent("Give a rationale of at least 10 characters.");
    fill("Successor principal", "morgan");
    fill("Rationale", "too short");
    expect(requestButton()).toBeDisabled();
    fill("Rationale", RATIONALE);
    expect(requestButton()).toBeEnabled();
    expect(screen.queryByRole("list", { name: "What is still needed" })).not.toBeInTheDocument();
  });

  it("refuses a successor who is the leaver, as the server would", async () => {
    await fillForm("priya");

    expect(requestButton()).toBeDisabled();
    expect(screen.getByLabelText("Successor principal")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getAllByText("The successor must be a different principal.").length).toBeGreaterThan(0);
    // ...whatever the padding around the names.
    fill("Successor principal", "  priya  ");
    expect(requestButton()).toBeDisabled();
  });

  it("registers typed work as unsaved, and stops once the request is made", async () => {
    requestLeaverReassignment.mockResolvedValue(operation());
    sessionMe = asRoles("DataSteward");
    render(<OwnershipLeaver />);
    await screen.findByRole("button", { name: "Request reassignment…" });
    expect(hasUnsavedChanges()).toBe(false);

    fill("Leaving principal", "priya");
    expect(hasUnsavedChanges()).toBe(true);
    fill("Successor principal", "morgan");
    fill("Rationale", RATIONALE);
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    await screen.findByRole("status", { name: "Review requested" });
    expect(hasUnsavedChanges()).toBe(false);
  });
});

describe("Leaver reassignment: the preview", () => {
  it("cannot be run until a leaver is named, then reads with the trimmed name and the owner type", async () => {
    await fillForm();
    fill("Leaving principal", "");
    expect(screen.getByRole("button", { name: "Preview what they hold" })).toBeDisabled();

    fill("Leaving principal", "  priya  ");
    fireEvent.change(screen.getByLabelText("Owner type they hold"), { target: { value: "GROUP" } });
    await previewIt();

    expect(fetchOwnershipPortfolio).toHaveBeenCalledWith(ORG, "priya", "GROUP", expect.any(AbortSignal));
  });

  it("lists what the leaver holds, says how much of the organization it read, and starts with every row ticked", async () => {
    await fillForm();
    await previewIt();

    const preview = screen.getByLabelText("Preview of the leaver's ownerships");
    expect(preview).toHaveTextContent("priya holds 3 active individual ownerships");
    expect(preview).toHaveTextContent("read all 40 assignments");
    expect(preview).toHaveTextContent("Every row is ticked, which sends no ids and lets the server take everything");
    const table = within(preview).getByRole("table", { name: "Ownerships the leaver holds" });
    expect(within(table).getAllByRole("checkbox", { checked: true })).toHaveLength(4); // 3 rows + the header
    expect(table).toHaveTextContent("t_h1");
    expect(table).toHaveTextContent("term-3");
    expect(preview).toHaveTextContent("3 of 3 selected.");
  });

  it("keeps the preview table reachable by keyboard, and named", async () => {
    await fillForm();
    await previewIt();

    const scroller = screen.getByRole("region", { name: "Ownerships to move, scrollable" });

    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(within(scroller).getByRole("table", { name: "Ownerships the leaver holds" })).toBeInTheDocument();
  });

  it("says so when the leaver holds nothing, and to check the spelling", async () => {
    await fillForm();
    await previewIt([]);

    const preview = screen.getByLabelText("Preview of the leaver's ownerships");
    expect(preview).toHaveTextContent("priya holds no active individual ownerships");
    expect(preview).toHaveTextContent("Check the spelling of the principal and the owner type");
    expect(within(preview).queryByRole("table")).not.toBeInTheDocument();
  });

  it("says when it stopped before the end, and that the request takes the whole portfolio from the server", async () => {
    await fillForm();
    await previewIt([H1], { total: 12000, scanned: 10000, complete: false });

    const preview = screen.getByLabelText("Preview of the leaver's ownerships");
    expect(preview).toHaveTextContent("read 10000 of 12000 assignments");
    expect(preview).toHaveTextContent("The preview stops at 10000 assignments, so priya may hold more than this list");
    expect(preview).toHaveTextContent("A request that names no ids takes the whole portfolio from the server");
  });

  it("says a preview that found nothing on an incomplete read is not proof of nothing", async () => {
    await fillForm();
    await previewIt([], { total: 12000, scanned: 10000, complete: false });

    expect(screen.getByLabelText("Preview of the leaver's ownerships")).toHaveTextContent(
      "priya holds no active individual ownerships among the assignments read",
    );
  });

  it("shows a failed preview as the server said it, and does not treat it as an empty portfolio", async () => {
    await fillForm();
    fetchOwnershipPortfolio.mockRejectedValue(new ApiError(503, "database unavailable"));

    fireEvent.click(screen.getByRole("button", { name: "Preview what they hold" }));

    expect(await screen.findByText("database unavailable")).toBeInTheDocument();
    expect(screen.queryByLabelText("Preview of the leaver's ownerships")).not.toBeInTheDocument();
  });

  it("drops a preview that is no longer about the principal or owner type in the box", async () => {
    await fillForm();
    await previewIt();

    fill("Leaving principal", "someone-else");
    expect(screen.queryByLabelText("Preview of the leaver's ownerships")).not.toBeInTheDocument();
    fill("Leaving principal", "priya");
    expect(screen.getByLabelText("Preview of the leaver's ownerships")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Owner type they hold"), { target: { value: "GROUP" } });
    expect(screen.queryByLabelText("Preview of the leaver's ownerships")).not.toBeInTheDocument();
  });

  it("reads nothing until the preview is asked for", async () => {
    sessionMe = asRoles("DataSteward");
    render(<OwnershipLeaver />);
    await screen.findByRole("button", { name: "Preview what they hold" });

    fill("Leaving principal", "priya");

    expect(fetchOwnershipPortfolio).not.toHaveBeenCalled();
  });
});

describe("Leaver reassignment: the confirmation", () => {
  it("states what a request does before sending anything, and sends nothing on the click or on Cancel", async () => {
    await fillForm();

    const dialog = await openConfirm();

    expect(dialog).toHaveAccessibleName("Request that morgan take over from priya?");
    expect(dialog).toHaveTextContent("This asks for a review. It does not move anything yet.");
    expect(dialog).toHaveTextContent("every active individual ownership priya holds when the server reads them (you have not previewed the list)");
    expect(dialog).toHaveTextContent("One request carries at most 500 ownerships");
    expect(dialog).toHaveTextContent("Nothing moves yet.");
    expect(dialog).toHaveTextContent("A different reviewer has to approve it in the Review queue; you cannot approve your own request");
    expect(dialog).toHaveTextContent("each ownership is marked reassigned and morgan is recorded as its individual owner");
    expect(dialog).toHaveTextContent("Certifications priya granted are not changed");
    expect(dialog).toHaveTextContent("recorded in the audit ledger with your rationale");
    expect(requestLeaverReassignment).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(requestLeaverReassignment).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(requestButton());
    // Cancelling keeps what was typed.
    expect(screen.getByLabelText("Leaving principal")).toHaveValue("priya");
  });

  it("says what was previewed when it was", async () => {
    await fillForm();
    await previewIt();

    const dialog = await openConfirm();

    expect(dialog).toHaveTextContent("all 3 ownerships the preview found for priya");
    expect(dialog).not.toHaveTextContent("you have not previewed");
  });

  it("says a preview that stopped early may not have found everything", async () => {
    await fillForm();
    await previewIt([H1], { total: 12000, scanned: 10000, complete: false });

    const dialog = await openConfirm();

    expect(dialog).toHaveTextContent("all 1 ownerships the preview found for priya, and any more the server finds beyond what the preview read");
  });

  it("says how many of the previewed ownerships were chosen", async () => {
    await fillForm();
    await previewIt();
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TABLE t_h2" }));

    const dialog = await openConfirm();

    expect(dialog).toHaveTextContent("2 of the 3 ownerships the preview found for priya");
  });

  it("does not dismiss on a click outside, so a rationale typed for a governed request is not thrown away", async () => {
    await fillForm();
    const dialog = await openConfirm();

    fireEvent.mouseDown(dialog.parentElement!);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("Leaver reassignment: sending it", () => {
  it("sends no ids when nothing was previewed, so the server discovers the whole portfolio", async () => {
    requestLeaverReassignment.mockResolvedValue(operation());
    await fillForm();
    const dialog = await openConfirm();

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    await waitFor(() => expect(requestLeaverReassignment).toHaveBeenCalledTimes(1));
    expect(requestLeaverReassignment).toHaveBeenCalledWith(
      ORG,
      { leaving_principal: "priya", successor_principal: "morgan", owner_type: "INDIVIDUAL", rationale: RATIONALE },
      undefined,
    );
    expect(requestLeaverReassignment.mock.calls[0]![1]).not.toHaveProperty("assignment_ids");
  });

  it("sends no ids when every previewed row is still ticked", async () => {
    requestLeaverReassignment.mockResolvedValue(operation());
    await fillForm();
    await previewIt();
    const dialog = await openConfirm();

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    await waitFor(() => expect(requestLeaverReassignment).toHaveBeenCalledTimes(1));
    expect(requestLeaverReassignment.mock.calls[0]![1]).not.toHaveProperty("assignment_ids");
  });

  it("sends exactly the ticked ids when the steward narrowed the preview, and the owner type it was run for", async () => {
    requestLeaverReassignment.mockResolvedValue(operation({ subject_ids: ["h1", "h3"] }));
    await fillForm();
    fireEvent.change(screen.getByLabelText("Owner type they hold"), { target: { value: "GROUP" } });
    await previewIt([holding("h1", { owner_type: "GROUP" }), holding("h2", { owner_type: "GROUP" }), holding("h3", { owner_type: "GROUP" })]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TABLE t_h2" }));
    const dialog = await openConfirm();

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    await waitFor(() => expect(requestLeaverReassignment).toHaveBeenCalledTimes(1));
    expect(requestLeaverReassignment).toHaveBeenCalledWith(
      ORG,
      { leaving_principal: "priya", successor_principal: "morgan", owner_type: "GROUP", rationale: RATIONALE, assignment_ids: ["h1", "h3"] },
      undefined,
    );
  });

  it("will not send an empty selection, or one the API would refuse for being over 500", async () => {
    await fillForm();
    const many = Array.from({ length: 502 }, (_, index) => holding(`m${index}`));
    await previewIt(many);

    // All 502 ticked: no ids are named, so the server takes the first 500 itself. Allowed.
    expect(requestButton()).toBeEnabled();
    // 501 of 502 ticked: 501 named ids -- more than the schema takes.
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TABLE t_m0" }));
    expect(requestButton()).toBeDisabled();
    expect(screen.getByRole("list", { name: "What is still needed" })).toHaveTextContent(
      "Tick at most 500 ownerships, or keep every row ticked and let the server take the first 500.",
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TABLE t_m1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TABLE t_m2" }));
    expect(requestButton()).toBeEnabled();

    // Everything unticked: nothing to move. (The header box selects all, then clears all.)
    fireEvent.click(screen.getByRole("checkbox", { name: "Move every ownership listed" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Move every ownership listed" }));
    expect(requestButton()).toBeDisabled();
    expect(screen.getByRole("list", { name: "What is still needed" })).toHaveTextContent("Tick at least one ownership, or preview again.");
    // A 502-row table re-renders on every click; the budget is sized for that, not for a small list.
  }, 30_000);
});

describe("Leaver reassignment: the result", () => {
  async function requestIt(op = operation(), preview?: Parameters<typeof previewIt>) {
    requestLeaverReassignment.mockResolvedValue(op);
    await fillForm();
    if (preview) await previewIt(...preview);
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    return await screen.findByRole("status", { name: "Review requested" });
  }

  it("reports a review requested -- not a transfer -- and that nothing has changed yet", async () => {
    const result = await requestIt();

    expect(result).toHaveTextContent("Review requested: priya → morgan");
    expect(result).toHaveTextContent("waiting for review");
    expect(result).toHaveTextContent("3 ownerships in this request.");
    expect(result).toHaveTextContent("0 changed so far.");
    expect(result).toHaveTextContent("Nothing changes until a different reviewer approves it");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("clears the form so the same request cannot be sent twice, and re-reads the requests", async () => {
    fetchOwnershipOperations.mockResolvedValueOnce(page([])).mockResolvedValueOnce(page([operation()]));
    await requestIt();

    expect(screen.getByLabelText("Leaving principal")).toHaveValue("");
    expect(screen.getByLabelText("Successor principal")).toHaveValue("");
    expect(screen.getByLabelText("Rationale")).toHaveValue("");
    expect(requestButton()).toBeDisabled();
    // The list is the server's second answer, and it now holds the request.
    expect(await screen.findByRole("list", { name: "Leaver requests" })).toHaveTextContent("priya → morgan");
    expect(fetchOwnershipOperations).toHaveBeenCalledTimes(2);
    expect(requestLeaverReassignment).toHaveBeenCalledTimes(1);
  });

  it("opens the review it asked for in the Review queue", async () => {
    const result = await requestIt(operation({ governance_review_id: "review-42" }));

    fireEvent.click(within(result).getByRole("button", { name: "Open this review" }));

    expect(navigateTo).toHaveBeenCalledWith("governance", { review: "review-42" });
  });

  it("says what was left out when the leaver held more than one request carries", async () => {
    const result = await requestIt(
      operation({
        subject_ids: Array.from({ length: 500 }, (_, index) => `h${index}`),
        parameters: { leaving_principal: "priya", successor_principal: "morgan", owner_type: "INDIVIDUAL", selection_truncated: true },
      }),
    );

    expect(result).toHaveTextContent("500 ownerships in this request.");
    expect(result).toHaveTextContent("priya holds more than 500 ownerships. This request covers the first 500; the rest stay with priya");
    expect(result).toHaveTextContent("Request again once this review is decided to move them.");
  });

  it("says how many of the previewed ownerships were left out when the steward chose a subset", async () => {
    requestLeaverReassignment.mockResolvedValue(operation({ subject_ids: ["h1", "h2"] }));
    await fillForm();
    await previewIt();
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TERM term-3" }));
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const result = await screen.findByRole("status", { name: "Review requested" });

    expect(requestLeaverReassignment.mock.calls[0]![1]).toHaveProperty("assignment_ids", ["h1", "h2"]);
    expect(result).toHaveTextContent("2 ownerships in this request.");
    expect(result).toHaveTextContent("1 of the 3 ownerships the preview found is not in this request and stays with priya.");
  });

  it("says nothing about what was left out when nothing was", async () => {
    const result = await requestIt(operation(), [[H1, H2, H3]]);

    expect(result).not.toHaveTextContent("not in this request");
    expect(result).not.toHaveTextContent("stay with");
  });

  it("says a preview that stopped early may have left more, and can be dismissed", async () => {
    const result = await requestIt(operation({ subject_ids: ["h1"] }), [[H1], { total: 12000, scanned: 10000, complete: false }]);

    expect(result).toHaveTextContent("The preview stopped after reading 10000 of 12000 assignments, so priya may hold more than it found");
    fireEvent.click(within(result).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status", { name: "Review requested" })).not.toBeInTheDocument();
  });
});

describe("Leaver reassignment: refusals", () => {
  async function requestRefused(refusal: ApiError) {
    requestLeaverReassignment.mockRejectedValue(refusal);
    await fillForm();
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    return dialog;
  }

  it("keeps the confirmation open and shows a 409 in the server's own words", async () => {
    const dialog = await requestRefused(new ApiError(409, "leaving_principal has no active ownership assignments to reassign"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /^leaving_principal has no active ownership assignments to reassign$/,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // Nothing was requested: no review is claimed and what was typed is kept for the retry.
    expect(screen.queryByRole("status", { name: "Review requested" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Leaving principal")).toHaveValue("priya");
    expect(within(dialog).getByRole("button", { name: "Request review" })).toBeEnabled();
  });

  it("shows the refusal of an explicit id verbatim", async () => {
    requestLeaverReassignment.mockRejectedValue(
      new ApiError(409, "one or more assignment_ids are not active ownership assignments currently held by leaving_principal"),
    );
    await fillForm();
    await previewIt();
    fireEvent.click(screen.getByRole("checkbox", { name: "Move TABLE t_h2" }));
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /^one or more assignment_ids are not active ownership assignments currently held by leaving_principal$/,
    );
  });

  it("shows a validation refusal verbatim", async () => {
    const dialog = await requestRefused(new ApiError(422, "body: Value error, successor_principal must differ from leaving_principal"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /^body: Value error, successor_principal must differ from leaving_principal$/,
    );
  });

  it("lets the steward retry from the same dialog once the cause is fixed", async () => {
    requestLeaverReassignment.mockRejectedValueOnce(new ApiError(409, "leaving_principal has no active ownership assignments to reassign"));
    requestLeaverReassignment.mockResolvedValueOnce(operation());
    await fillForm();
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    await within(dialog).findByRole("alert");

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    await screen.findByRole("status", { name: "Review requested" });
    expect(requestLeaverReassignment).toHaveBeenCalledTimes(2);
  });

  it("admits one request at a time and shows the confirmation busy", async () => {
    let settle: (op: BulkStewardshipOperationRead) => void = () => undefined;
    requestLeaverReassignment.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    await fillForm();
    const dialog = await openConfirm();

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const working = await within(dialog).findByRole("button", { name: "Working…" });
    expect(working).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.click(working);
    expect(requestLeaverReassignment).toHaveBeenCalledTimes(1);
    settle(operation());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not carry an earlier refusal into the next time the confirmation opens", async () => {
    const dialog = await requestRefused(new ApiError(409, "leaving_principal has no active ownership assignments to reassign"));
    await within(dialog).findByRole("alert");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const reopened = await openConfirm();

    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Leaver reassignment: the requests it opened", () => {
  it("lists only leaver requests, with what they were for", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(
      page([
        operation(),
        operation({ id: "op-rule", operation_type: "ASSIGN_OWNERSHIP", parameters: { owner_type: "GROUP", owner_principal: "x", source_rule_id: "rule-1" } }),
      ]),
    );
    render(<OwnershipLeaver />);

    const list = await screen.findByRole("list", { name: "Leaver requests" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(list).toHaveTextContent("priya → morgan");
    expect(list).toHaveTextContent("individual ownerships");
    expect(list).toHaveTextContent("3 ownerships in the request");
    expect(list).toHaveTextContent("requested by pat.admin at 2026-09-20 10:00 UTC");
  });

  it("says how many an applied request moved, and how many were left as they were -- the answer a 202 cannot give", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(
      page([operation({ status: "APPLIED", applied_count: 2, applied_by: "riya.reviewer", applied_at: "2026-09-21T08:15:00Z" })]),
    );
    render(<OwnershipLeaver />);

    const list = await screen.findByRole("list", { name: "Leaver requests" });
    expect(list).toHaveTextContent("2 of 3 ownerships changed");
    expect(list).toHaveTextContent("the other 1 were already as requested or had changed since the request, and were left as they were");
    expect(list).toHaveTextContent("Applied by riya.reviewer at 2026-09-21 08:15 UTC");
  });

  it("says a request that hit the cap left the rest with the leaver", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(
      page([operation({ parameters: { leaving_principal: "priya", successor_principal: "morgan", owner_type: "GROUP", selection_truncated: true } })]),
    );
    render(<OwnershipLeaver />);

    const list = await screen.findByRole("list", { name: "Leaver requests" });
    expect(list).toHaveTextContent("more than 500 held: only the first 500 are in this request");
    expect(list).toHaveTextContent("group ownerships");
  });

  it("says a rejected request changed nothing, and a pending one is still waiting", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(page([operation({ id: "op-a", status: "REJECTED" }), operation({ id: "op-b" })]));
    render(<OwnershipLeaver />);

    const list = await screen.findByRole("list", { name: "Leaver requests" });
    const [rejected, pending] = within(list).getAllByRole("listitem");
    expect(rejected).toHaveTextContent("Rejected by the reviewer. Nothing was changed.");
    expect(pending).toHaveTextContent("Nothing has changed yet: a different reviewer has to approve this.");
  });

  it("says nothing has been requested, and how far back it looked", async () => {
    sessionMe = asRoles("Viewer");
    render(<OwnershipLeaver />);

    expect(await screen.findByText("No leaver reassignment has been requested yet")).toBeInTheDocument();
    expect(screen.getByText(/Looked at the newest 500 stewardship operations/)).toBeInTheDocument();
  });
});

describe("Leaver reassignment: accessibility", () => {
  it("has no WCAG A/AA violations with the form, a preview and an invalid successor on screen, and names every control", async () => {
    await fillForm();
    await previewIt();
    fill("Successor principal", "priya");

    await expectNoAxeViolations(document.body);
    expect(
      unnamedFocusableElements(document.body).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no WCAG A/AA violations with the confirmation open and a refusal showing", async () => {
    requestLeaverReassignment.mockRejectedValue(new ApiError(409, "leaving_principal has no active ownership assignments to reassign"));
    await fillForm();
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    await within(dialog).findByRole("alert");

    await expectNoAxeViolations(document.body);
  });

  it("has no WCAG A/AA violations with a result on screen", async () => {
    requestLeaverReassignment.mockResolvedValue(operation());
    await fillForm();
    const dialog = await openConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    await screen.findByRole("status", { name: "Review requested" });

    await expectNoAxeViolations(document.body);
  });

  it("has no WCAG A/AA violations for a read-only session", async () => {
    sessionMe = asRoles("Auditor");
    fetchOwnershipOperations.mockResolvedValue(page([operation()]));
    render(<OwnershipLeaver />);
    await screen.findByText("priya → morgan");

    await expectNoAxeViolations(document.body);
  });
});
