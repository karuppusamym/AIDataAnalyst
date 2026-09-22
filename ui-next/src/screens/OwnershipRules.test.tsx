import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { BulkStewardshipOperationRead, MeRead, OwnershipRuleCreate, OwnershipRuleRead } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import type { PageOf } from "../lib/ui-types";
import { resetUnsavedRegistryForTests, hasUnsavedChanges } from "../lib/unsavedChanges";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { OwnershipRules } from "./OwnershipRules";

/* ---------------------------------------------------------------------------
   Ownership -> Rules (R11-AUD08, part 2).

   The properties, and why each was a real way to get an ownership control wrong:

     1. WHO SEES WHAT. Every role the read admits sees the rules; only the four
        roles the writes admit are offered Create and Apply. A session outside the
        read's roles is not asked at all -- for the rules OR the requests beneath
        them -- and while `/v1/me` is in flight nothing is asked and no control is
        offered (fail closed).
     2. APPLY DOES NOT DO WHAT ITS NAME SAYS, AND THE CONFIRMATION SAYS SO. It opens
        a review; there is no preview; at most 500 tables; nothing changes until a
        different reviewer approves; an owner is ADDED. Nothing is sent on the
        click, only on the confirmation, and a refusal is shown as the server wrote it.
     3. THE RESULT IS NOT SUCCESS. A 202 is "review requested, 0 changed so far",
        never "ownership assigned"; and the outcome the steward actually wanted --
        how many were changed -- is read from the requests list once it exists.
     4. CREATING A RULE ASSIGNS NOTHING, and says so; a duplicate key is the
        server's 409, verbatim, and the typed rule is kept.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchOwnershipRules = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<OwnershipRuleRead>>>();
const createOwnershipRule =
  vi.fn<(organizationId: string, body: OwnershipRuleCreate, signal?: AbortSignal) => Promise<OwnershipRuleRead>>();
const applyOwnershipRule = vi.fn<(ruleId: string, signal?: AbortSignal) => Promise<BulkStewardshipOperationRead>>();
const fetchOwnershipOperations =
  vi.fn<(organizationId: string, query?: { limit?: number }, signal?: AbortSignal) => Promise<PageOf<BulkStewardshipOperationRead>>>();
const navigateTo = vi.fn<(screen: string, params?: Record<string, string>) => void>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOwnershipRules: (organizationId: string, signal?: AbortSignal) => fetchOwnershipRules(organizationId, signal),
    createOwnershipRule: (organizationId: string, body: OwnershipRuleCreate, signal?: AbortSignal) =>
      createOwnershipRule(organizationId, body, signal),
    applyOwnershipRule: (ruleId: string, signal?: AbortSignal) => applyOwnershipRule(ruleId, signal),
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

const rule = (overrides: Partial<OwnershipRuleRead> = {}): OwnershipRuleRead => ({
  id: "rule-1", organization_id: ORG, status: "ACTIVE", rule_key: "retail-tables", display_name: "Retail tables",
  match_field: "SCHEMA_NAME", match_pattern: "retail*", owner_type: "GROUP", owner_principal: "retail-data-stewards",
  created_by: "pat.admin", created_at: "2026-09-01T09:00:00Z", updated_at: "2026-09-01T09:00:00Z",
  ...overrides,
});
const RETAIL = rule();
const PII = rule({
  id: "rule-2", rule_key: "pii-tagged", display_name: "Anything tagged PII", match_field: "TAG", match_pattern: "pii*",
  owner_type: "INDIVIDUAL", owner_principal: "privacy.lead@tenant.example",
});

const operation = (overrides: Partial<BulkStewardshipOperationRead> = {}): BulkStewardshipOperationRead => ({
  id: "op-1", organization_id: ORG, operation_type: "ASSIGN_OWNERSHIP", subject_type: "TABLE",
  subject_ids: ["t1", "t2", "t3"],
  parameters: { owner_type: "GROUP", owner_principal: "retail-data-stewards", source_rule_id: "rule-1" },
  status: "REVIEW_REQUIRED", governance_review_id: "review-1", requested_by: "pat.admin",
  applied_by: null, applied_at: null, applied_count: 0, applied_subject_ids: [],
  reverses_operation_id: null, review_audit_sample_id: null,
  created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-20T10:00:00Z",
  ...overrides,
});
const page = <T,>(items: T[]): PageOf<T> => ({ items, limit: 500, offset: 0, total: items.length });

const WRITERS = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];
const READ_ONLY = ["Analyst", "Auditor", "DataAdmin", "Reviewer", "Viewer"];

const applyButton = (name = "Retail tables") => screen.queryByRole("button", { name: new RegExp(`^Apply ${name}`) });
const createForm = () => screen.queryByRole("region", { name: "Create an ownership rule" });

async function openApply(name = "Retail tables") {
  sessionMe = asRoles("DataSteward");
  render(<OwnershipRules />);
  await screen.findByText(name, { selector: "strong" });
  const trigger = screen.getByRole("button", { name: new RegExp(`^Apply ${name}`) });
  trigger.focus();
  fireEvent.click(trigger);
  return await screen.findByRole("dialog", { name: `Apply “${name}”?` });
}

beforeEach(() => {
  fetchOwnershipRules.mockReset();
  fetchOwnershipRules.mockResolvedValue(page([RETAIL, PII]));
  createOwnershipRule.mockReset();
  applyOwnershipRule.mockReset();
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

describe("Ownership rules: who sees what", () => {
  it.each(READ_ONLY)("shows the rules to %s and offers no way to create or apply one", async (role) => {
    sessionMe = asRoles(role);
    render(<OwnershipRules />);

    expect(await screen.findByText("Retail tables", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Anything tagged PII", { selector: "strong" })).toBeInTheDocument();
    expect(fetchOwnershipRules).toHaveBeenCalledWith(ORG, expect.any(AbortSignal));
    expect(createForm()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Apply / })).not.toBeInTheDocument();
    // Told why, rather than left to wonder where the controls are.
    expect(screen.getByText(/Creating or applying one needs DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin/)).toBeInTheDocument();
  });

  it.each(WRITERS)("offers %s the create form and an Apply on every rule", async (role) => {
    sessionMe = asRoles(role);
    render(<OwnershipRules />);

    expect(await screen.findByText("Retail tables", { selector: "strong" })).toBeInTheDocument();
    expect(createForm()).toBeInTheDocument();
    expect(applyButton("Retail tables")).toBeEnabled();
    expect(applyButton("Anything tagged PII")).toBeEnabled();
    expect(screen.queryByText(/Your roles can read ownership rules/)).not.toBeInTheDocument();
  });

  it.each(["AgentDeveloper", "ToolDeveloper", "OrganizationAdmin"])(
    "asks for nothing as %s and says the panel does not apply",
    async (role) => {
      sessionMe = asRoles(role);
      render(<OwnershipRules />);

      expect(await screen.findAllByText(/Not applicable to your roles/)).toHaveLength(2);
      // Neither the rules nor the requests beneath them were asked for.
      expect(fetchOwnershipRules).not.toHaveBeenCalled();
      expect(fetchOwnershipOperations).not.toHaveBeenCalled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(createForm()).not.toBeInTheDocument();
      expect(screen.queryByText(/Creating or applying one needs/)).not.toBeInTheDocument();
    },
  );

  it("holds every read while identity is in flight, and offers no control", async () => {
    sessionState = "connecting";
    render(<OwnershipRules />);

    expect(await screen.findByText(/Loading ownership rules/)).toBeInTheDocument();
    expect(fetchOwnershipRules).not.toHaveBeenCalled();
    expect(fetchOwnershipOperations).not.toHaveBeenCalled();
    expect(createForm()).not.toBeInTheDocument();
    // It does not tell a steward-to-be that they lack a role, or that the panel does not apply.
    expect(screen.queryByText(/Creating or applying one needs/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();
  });

  it("sends the reads once identity says the session may, and offers what it is admitted to", async () => {
    sessionState = "connecting";
    const view = render(<OwnershipRules />);
    expect(fetchOwnershipRules).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("PlatformAdmin");
    view.rerender(<OwnershipRules />);

    expect(await screen.findByText("Retail tables", { selector: "strong" })).toBeInTheDocument();
    expect(fetchOwnershipRules).toHaveBeenCalledTimes(1);
    expect(fetchOwnershipOperations).toHaveBeenCalledTimes(1);
    expect(createForm()).toBeInTheDocument();
  });

  it("never sends the reads when identity then says the session may not", async () => {
    sessionState = "connecting";
    const view = render(<OwnershipRules />);
    await screen.findByText(/Loading ownership rules/);

    sessionState = "connected";
    sessionMe = asRoles("AgentDeveloper");
    view.rerender(<OwnershipRules />);

    expect(await screen.findAllByText(/Not applicable to your roles/)).toHaveLength(2);
    expect(fetchOwnershipRules).not.toHaveBeenCalled();
    expect(fetchOwnershipOperations).not.toHaveBeenCalled();
  });

  it("still reads when identity will not answer -- and offers nothing that writes", async () => {
    sessionState = "disconnected";
    render(<OwnershipRules />);

    expect(await screen.findByText("Retail tables", { selector: "strong" })).toBeInTheDocument();
    expect(fetchOwnershipRules).toHaveBeenCalledTimes(1);
    expect(createForm()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Apply / })).not.toBeInTheDocument();
  });
});

describe("Ownership rules: what the list says", () => {
  it("shows what each rule matches and who it assigns to, and that rules cannot be edited or retired", async () => {
    sessionMe = asRoles("Viewer");
    render(<OwnershipRules />);

    const list = await screen.findByRole("list", { name: "Ownership rules" });
    const retail = within(list).getAllByRole("listitem")[0]!;
    expect(retail).toHaveTextContent("Retail tables");
    expect(retail).toHaveTextContent("retail-tables");
    expect(retail).toHaveTextContent("Tables whose schema name matches retail* are assigned to group retail-data-stewards.");
    expect(retail).toHaveTextContent("created by pat.admin at 2026-09-01 09:00 UTC");
    expect(within(list).getAllByRole("listitem")[1]).toHaveTextContent(
      "Tables whose business tag matches pii* are assigned to individual privacy.lead@tenant.example.",
    );
    expect(screen.getByText(/Rules cannot be edited or retired: the API has no route for either/)).toBeInTheDocument();
    expect(screen.getByText("2 active")).toBeInTheDocument();
  });

  it("says an organization with no rules has none, and where they come from", async () => {
    sessionMe = asRoles("DataSteward");
    fetchOwnershipRules.mockResolvedValue(page([]));
    render(<OwnershipRules />);

    expect(await screen.findByText("No ownership rules yet")).toBeInTheDocument();
    expect(screen.getByText(/Create one above/)).toBeInTheDocument();
  });

  it("tells a read-only session who can create one", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipRules.mockResolvedValue(page([]));
    render(<OwnershipRules />);

    expect(await screen.findByText("No ownership rules yet")).toBeInTheDocument();
    expect(screen.getByText("Creating a rule needs DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin.")).toBeInTheDocument();
  });

  it("says the read failed, verbatim, and retries", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipRules.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    render(<OwnershipRules />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Ownership rules could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    // Never an empty list that reads as "there are none".
    expect(screen.queryByText("No ownership rules yet")).not.toBeInTheDocument();

    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Retail tables", { selector: "strong" })).toBeInTheDocument();
  });
});

describe("Ownership rules: creating one", () => {
  const fill = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

  async function openForm() {
    sessionMe = asRoles("DataSteward");
    render(<OwnershipRules />);
    await screen.findByRole("region", { name: "Create an ownership rule" });
  }

  it("says creating a rule assigns nothing, and keeps the button off until the rule is valid", async () => {
    await openForm();

    expect(screen.getByText(/Creating one records it and assigns nothing/)).toBeInTheDocument();
    const create = screen.getByRole("button", { name: "Create rule" });
    expect(create).toBeDisabled();
    fill("Rule key", "retail-tables");
    fill("Display name", "Retail tables");
    fill("Match pattern", "retail*");
    expect(create).toBeDisabled();
    fill("Owner principal", "retail-data-stewards");
    expect(create).toBeEnabled();
  });

  it("mirrors the server's limits under the field once it has been typed in, not before", async () => {
    await openForm();
    expect(screen.queryByText(/Use lowercase letters/)).not.toBeInTheDocument();

    fill("Rule key", "Retail Tables");
    expect(screen.getByText("Use lowercase letters, digits, - and _, starting with a letter: 2 to 100 characters.")).toBeInTheDocument();
    expect(screen.getByLabelText("Rule key")).toHaveAttribute("aria-invalid", "true");
    fill("Rule key", "retail-tables");
    expect(screen.getByLabelText("Rule key")).toHaveAttribute("aria-invalid", "false");

    fill("Owner principal", "x");
    expect(screen.getByText("2 to 255 characters.")).toBeInTheDocument();
  });

  it("explains what each match field looks at", async () => {
    await openForm();

    fireEvent.change(screen.getByLabelText("Match field"), { target: { value: "DOMAIN_KEY" } });
    expect(screen.getByText(/A table with no domain never matches\./)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Match field"), { target: { value: "TAG" } });
    expect(screen.getByText("Matches when ANY of the table's approved business tags matches.")).toBeInTheDocument();
  });

  it("sends exactly what was typed, trimmed, then re-reads the list rather than editing it", async () => {
    createOwnershipRule.mockResolvedValue(rule({ id: "rule-9", rule_key: "treasury", display_name: "Treasury" }));
    fetchOwnershipRules.mockResolvedValueOnce(page([RETAIL])).mockResolvedValueOnce(page([RETAIL, rule({ id: "rule-9", display_name: "Treasury", rule_key: "treasury" })]));
    await openForm();
    await screen.findByText("Retail tables", { selector: "strong" });

    fill("Rule key", "treasury");
    fill("Display name", "  Treasury  ");
    fireEvent.change(screen.getByLabelText("Match field"), { target: { value: "QUALIFIED_NAME" } });
    fill("Match pattern", "  treasury.*  ");
    fireEvent.change(screen.getByLabelText("Owner type"), { target: { value: "INDIVIDUAL" } });
    fill("Owner principal", " morgan@tenant.example ");
    fireEvent.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(createOwnershipRule).toHaveBeenCalledTimes(1));
    expect(createOwnershipRule).toHaveBeenCalledWith(
      ORG,
      {
        rule_key: "treasury",
        display_name: "Treasury",
        match_field: "QUALIFIED_NAME",
        match_pattern: "treasury.*",
        owner_type: "INDIVIDUAL",
        owner_principal: "morgan@tenant.example",
      },
      undefined,
    );
    // The list on screen is the server's second answer, not our append.
    expect(await screen.findByText("Treasury", { selector: "strong" })).toBeInTheDocument();
    expect(fetchOwnershipRules).toHaveBeenCalledTimes(2);
    // It says what creating did and did not do, and the form is empty again.
    expect(screen.getByText(/Rule “Treasury” created\. Nothing has been assigned/)).toBeInTheDocument();
    expect(screen.getByLabelText("Rule key")).toHaveValue("");
    expect(applyOwnershipRule).not.toHaveBeenCalled();
  });

  it("shows a duplicate key as the server said it and keeps what was typed", async () => {
    createOwnershipRule.mockRejectedValue(new ApiError(409, "ownership rule key already exists"));
    await openForm();
    fill("Rule key", "retail-tables");
    fill("Display name", "Retail tables again");
    fill("Match pattern", "retail*");
    fill("Owner principal", "retail-data-stewards");

    fireEvent.click(screen.getByRole("button", { name: "Create rule" }));

    const refusal = await screen.findByText("ownership rule key already exists");
    // Verbatim -- not the shared 409 paraphrase about someone else having changed the record.
    expect(refusal.closest("[role=alert]")).toHaveTextContent(/^ownership rule key already exists$/);
    expect(screen.getByLabelText("Rule key")).toHaveValue("retail-tables");
    expect(screen.getByLabelText("Display name")).toHaveValue("Retail tables again");
    expect(screen.queryByText(/created\. Nothing has been assigned/)).not.toBeInTheDocument();
    expect(fetchOwnershipRules).toHaveBeenCalledTimes(1);
  });

  it("admits one create at a time", async () => {
    let settle: (created: OwnershipRuleRead) => void = () => undefined;
    createOwnershipRule.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    await openForm();
    fill("Rule key", "treasury");
    fill("Display name", "Treasury");
    fill("Match pattern", "treasury*");
    fill("Owner principal", "treasury-stewards");

    fireEvent.click(screen.getByRole("button", { name: "Create rule" }));
    const working = await screen.findByRole("button", { name: "Creating…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);
    expect(createOwnershipRule).toHaveBeenCalledTimes(1);
    settle(rule({ id: "rule-9" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create rule" })).toBeInTheDocument());
  });

  it("registers a typed rule as unsaved work, and stops once it is created", async () => {
    createOwnershipRule.mockResolvedValue(rule({ id: "rule-9" }));
    await openForm();
    expect(hasUnsavedChanges()).toBe(false);

    fill("Rule key", "treasury");
    expect(hasUnsavedChanges()).toBe(true);
    fill("Display name", "Treasury");
    fill("Match pattern", "treasury*");
    fill("Owner principal", "treasury-stewards");
    fireEvent.click(screen.getByRole("button", { name: "Create rule" }));

    await screen.findByText(/created\. Nothing has been assigned/);
    expect(hasUnsavedChanges()).toBe(false);
  });
});

describe("Ownership rules: applying one", () => {
  it("states what applying does before sending anything: a review, no preview, at most 500, an owner added", async () => {
    const dialog = await openApply();

    expect(dialog).toHaveTextContent("This asks for a review. It does not assign anyone yet.");
    expect(dialog).toHaveTextContent("active tables whose schema name matches retail*, ignoring capital letters");
    expect(dialog).toHaveTextContent("At most 500 tables go into one review");
    expect(dialog).toHaveTextContent("the server does not say when more matched");
    expect(dialog).toHaveTextContent("There is no preview");
    expect(dialog).toHaveTextContent("Nothing changes yet.");
    expect(dialog).toHaveTextContent("make retail-data-stewards (group) an owner of each table");
    expect(dialog).toHaveTextContent("you cannot approve your own request");
    expect(dialog).toHaveTextContent("this owner is added beside any owner a table already has; no existing owner is removed");
    expect(dialog).toHaveTextContent("Applying the rule again later opens a second review");
    expect(dialog).toHaveTextContent("recorded in the audit ledger");
    // Clicking Apply sent nothing.
    expect(applyOwnershipRule).not.toHaveBeenCalled();
  });

  it("cancels without sending anything and gives focus back to the button that opened it", async () => {
    const dialog = await openApply();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(applyOwnershipRule).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /^Apply Retail tables/ }));
  });

  it("does not dismiss on a click outside, so a stray click cannot cancel a request half read", async () => {
    const dialog = await openApply();

    fireEvent.mouseDown(dialog.parentElement!);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("sends the rule's id once, on the confirmation, and reports a review requested -- not an assignment", async () => {
    applyOwnershipRule.mockResolvedValue(operation());
    const dialog = await openApply();
    fetchOwnershipOperations.mockResolvedValue(page([operation()]));

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    await waitFor(() => expect(applyOwnershipRule).toHaveBeenCalledTimes(1));
    expect(applyOwnershipRule).toHaveBeenCalledWith("rule-1", undefined);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const result = await screen.findByRole("status", { name: "Review requested" });
    expect(result).toHaveTextContent("Review requested for “Retail tables”");
    expect(result).toHaveTextContent("waiting for review");
    expect(result).toHaveTextContent("3 tables in this request.");
    expect(result).toHaveTextContent("0 changed so far.");
    expect(result).toHaveTextContent("Nothing changes until a different reviewer approves it");
    // The rule's row says a review is out, so a second apply is visible as a second.
    expect(screen.getByText(/review requested at 2026-09-20 10:00 UTC for 3 tables/)).toBeInTheDocument();
    // And the requests list was re-read, so the new request is in it.
    await waitFor(() => expect(fetchOwnershipOperations).toHaveBeenCalledTimes(2));
  });

  it("opens the review it asked for in the Review queue", async () => {
    applyOwnershipRule.mockResolvedValue(operation({ governance_review_id: "review-77" }));
    const dialog = await openApply();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const result = await screen.findByRole("status", { name: "Review requested" });
    fireEvent.click(within(result).getByRole("button", { name: "Open this review" }));

    expect(navigateTo).toHaveBeenCalledWith("governance", { review: "review-77" });
  });

  it("says a request that reached the 500 cap may not cover every match", async () => {
    applyOwnershipRule.mockResolvedValue(operation({ subject_ids: Array.from({ length: 500 }, (_, index) => `t${index}`) }));
    const dialog = await openApply();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const result = await screen.findByRole("status", { name: "Review requested" });
    expect(result).toHaveTextContent("500 tables in this request.");
    expect(result).toHaveTextContent("500 is the most one apply puts in a review, and the server does not say whether more tables matched");
  });

  it("keeps the confirmation open and shows a refusal in the server's own words", async () => {
    applyOwnershipRule.mockRejectedValue(new ApiError(409, "ownership rule matched no active tables"));
    const dialog = await openApply();

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const refusal = await within(dialog).findByRole("alert");
    expect(refusal).toHaveTextContent(/^ownership rule matched no active tables$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // Nothing was requested, so no review is claimed and the rules were not re-read.
    expect(screen.queryByRole("status", { name: "Review requested" })).not.toBeInTheDocument();
    expect(fetchOwnershipRules).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByRole("button", { name: "Request review" })).toBeEnabled();
  });

  it("re-reads the rules when the server says the rule is gone", async () => {
    applyOwnershipRule.mockRejectedValue(new ApiError(404, "active ownership rule not found"));
    const dialog = await openApply();
    fetchOwnershipRules.mockResolvedValueOnce(page([PII]));

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^active ownership rule not found$/);
    await waitFor(() => expect(fetchOwnershipRules).toHaveBeenCalledTimes(2));
  });

  it("admits one apply at a time and shows the confirmation busy", async () => {
    let settle: (op: BulkStewardshipOperationRead) => void = () => undefined;
    applyOwnershipRule.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openApply();

    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const working = await within(dialog).findByRole("button", { name: "Working…" });
    expect(working).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.click(working);
    expect(applyOwnershipRule).toHaveBeenCalledTimes(1);
    settle(operation());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("says a second apply opens a second review, naming the first", async () => {
    applyOwnershipRule.mockResolvedValue(operation());
    const first = await openApply();
    fireEvent.click(within(first).getByRole("button", { name: "Request review" }));
    await screen.findByRole("status", { name: "Review requested" });

    fireEvent.click(screen.getByRole("button", { name: /^Apply Retail tables/ }));

    const second = await screen.findByRole("dialog", { name: "Apply “Retail tables”?" });
    expect(second).toHaveTextContent("You already requested this rule at 2026-09-20 10:00 UTC.");
    expect(second).toHaveTextContent("Applying it again opens a second review; it does not replace the first.");
  });

  it("does not carry an earlier refusal into the next time the confirmation opens", async () => {
    applyOwnershipRule.mockRejectedValue(new ApiError(409, "ownership rule matched no active tables"));
    const dialog = await openApply();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    await within(dialog).findByRole("alert");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /^Apply Retail tables/ }));

    const reopened = await screen.findByRole("dialog");
    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Ownership rules: the requests they opened", () => {
  const ruleRequest = (overrides: Partial<BulkStewardshipOperationRead> = {}) => operation(overrides);
  const leaverRequest = operation({
    id: "op-leaver", operation_type: "REASSIGN_LEAVER", subject_type: "OWNERSHIP_ASSIGNMENT",
    parameters: { leaving_principal: "priya", successor_principal: "morgan", owner_type: "INDIVIDUAL" },
  });
  const catalogOwn = operation({
    id: "op-manual", parameters: { owner_type: "GROUP", owner_principal: "someone" }, // ASSIGN_OWNERSHIP, but no rule
  });

  it("lists only what came from a rule -- not a leaver request, and not an assignment with no rule behind it", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(page([ruleRequest(), leaverRequest, catalogOwn]));
    render(<OwnershipRules />);

    const list = await screen.findByRole("list", { name: "Rule requests" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(list).toHaveTextContent("Rule “Retail tables”");
    expect(list).toHaveTextContent("requested by pat.admin at 2026-09-20 10:00 UTC");
    expect(list).toHaveTextContent("3 tables in the request");
    expect(list).not.toHaveTextContent("priya");
  });

  it("says how many an applied request changed, and that the rest were left as they were", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(
      page([ruleRequest({ status: "APPLIED", applied_count: 2, applied_by: "riya.reviewer", applied_at: "2026-09-21T08:15:00Z" })]),
    );
    render(<OwnershipRules />);

    const list = await screen.findByRole("list", { name: "Rule requests" });
    expect(list).toHaveTextContent("applied");
    expect(list).toHaveTextContent("2 of 3 tables changed");
    expect(list).toHaveTextContent("the other 1 were already as requested or had changed since the request, and were left as they were");
    expect(list).toHaveTextContent("Applied by riya.reviewer at 2026-09-21 08:15 UTC");
  });

  it("says a rejected request changed nothing, and offers the review of a pending one to a role the queue admits", async () => {
    sessionMe = asRoles("Reviewer");
    fetchOwnershipOperations.mockResolvedValue(
      page([ruleRequest({ id: "op-a", status: "REJECTED" }), ruleRequest({ id: "op-b", governance_review_id: "review-b" })]),
    );
    render(<OwnershipRules />);

    const list = await screen.findByRole("list", { name: "Rule requests" });
    const [rejected, pending] = within(list).getAllByRole("listitem");
    expect(rejected).toHaveTextContent("Rejected by the reviewer. Nothing was changed.");
    expect(pending).toHaveTextContent("Nothing has changed yet: a different reviewer has to approve this.");
    fireEvent.click(within(pending!).getByRole("button", { name: "Open this review" }));
    expect(navigateTo).toHaveBeenCalledWith("governance", { review: "review-b" });
  });

  it.each(["Viewer", "MetadataAdmin", "Analyst"])(
    "tells %s a request is waiting without sending them to a Review queue that would refuse them",
    async (role) => {
      sessionMe = asRoles(role);
      fetchOwnershipOperations.mockResolvedValue(page([ruleRequest({ id: "op-b", governance_review_id: "review-b" })]));
      render(<OwnershipRules />);

      const list = await screen.findByRole("list", { name: "Rule requests" });
      expect(list).toHaveTextContent("Nothing has changed yet: a different reviewer has to approve this.");
      expect(within(list).queryByRole("button", { name: "Open this review" })).not.toBeInTheDocument();
    },
  );

  it("does not offer a MetadataAdmin who applied a rule a link into the Review queue", async () => {
    applyOwnershipRule.mockResolvedValue(operation({ governance_review_id: "review-77" }));
    sessionMe = asRoles("MetadataAdmin");
    render(<OwnershipRules />);
    await screen.findByText("Retail tables", { selector: "strong" });
    fireEvent.click(screen.getByRole("button", { name: /^Apply Retail tables/ }));
    const dialog = await screen.findByRole("dialog", { name: "Apply “Retail tables”?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));

    const result = await screen.findByRole("status", { name: "Review requested" });
    expect(result).toHaveTextContent("Nothing changes until a different reviewer approves it");
    expect(within(result).queryByRole("button", { name: "Open this review" })).not.toBeInTheDocument();
  });

  it("names a rule that is no longer listed as such rather than inventing a name", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue(
      page([ruleRequest({ parameters: { owner_type: "GROUP", owner_principal: "x", source_rule_id: "rule-gone" } })]),
    );
    render(<OwnershipRules />);

    expect(await screen.findByText("Rule “no longer listed”")).toBeInTheDocument();
  });

  it("says nothing has been requested, and how far back it looked", async () => {
    sessionMe = asRoles("Viewer");
    render(<OwnershipRules />);

    expect(await screen.findByText("No rule has been applied yet")).toBeInTheDocument();
    expect(screen.getByText(/Looked at the newest 500 stewardship operations/)).toBeInTheDocument();
  });

  it("says when the newest 500 are not all of them", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockResolvedValue({ items: [ruleRequest()], limit: 500, offset: 0, total: 730 });
    render(<OwnershipRules />);

    expect(await screen.findByText(/Showing the newest 1 of 730 stewardship operations; an older request may not be listed\./)).toBeInTheDocument();
  });

  it("says the requests could not be loaded, verbatim, without hiding the rules", async () => {
    sessionMe = asRoles("Viewer");
    fetchOwnershipOperations.mockRejectedValue(new ApiError(503, "database unavailable"));
    render(<OwnershipRules />);

    expect(await screen.findByText("Requests could not be loaded")).toBeInTheDocument();
    expect(screen.getByText("database unavailable")).toBeInTheDocument();
    expect(screen.getByText("Retail tables", { selector: "strong" })).toBeInTheDocument();
  });
});

describe("Ownership rules: accessibility", () => {
  it("has no WCAG A/AA violations with the form, the list and a request on screen, and names every control", async () => {
    sessionMe = asRoles("PlatformAdmin");
    fetchOwnershipOperations.mockResolvedValue(page([operation()]));
    const view = render(<OwnershipRules />);
    await screen.findByText("Retail tables", { selector: "strong" });
    await screen.findByText("Rule “Retail tables”");
    fireEvent.change(screen.getByLabelText("Rule key"), { target: { value: "Bad Key" } });

    await expectNoAxeViolations(view.container);
    expect(
      unnamedFocusableElements(view.container).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no WCAG A/AA violations with the confirmation open and a refusal showing", async () => {
    applyOwnershipRule.mockRejectedValue(new ApiError(409, "ownership rule matched no active tables"));
    const dialog = await openApply();
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    await within(dialog).findByRole("alert");

    await expectNoAxeViolations(document.body);
  });

  it("has no WCAG A/AA violations for a read-only session", async () => {
    sessionMe = asRoles("Auditor");
    const view = render(<OwnershipRules />);
    await screen.findByText("Retail tables", { selector: "strong" });

    await expectNoAxeViolations(view.container);
  });
});
