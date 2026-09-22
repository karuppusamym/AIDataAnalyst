import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { StudioPublishedState } from "../lib/api";
import { resetLocationCacheForTests } from "../lib/location";
import type { Session, SessionState } from "../lib/session";
import type {
  MeRead,
  StudioChangeItemCreate,
  StudioChangeItemRead,
  StudioChangeSetCreate,
  StudioChangeSetRead,
  StudioConflict,
  StudioContextProductValidateRequest,
  StudioContextProductValidateResult,
  StudioDiffRead,
  StudioEvalMiningResult,
  StudioEvalQuestionRead,
  StudioEvalRunRead,
  StudioImpactPreview,
  StudioParameterContractValidateRequest,
  StudioParameterContractValidateResult,
  StudioTestResultRead,
} from "../lib/types";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { StudioChangeSetsScreen } from "./StudioChangeSetsScreen";
import { ItemCheck } from "./StudioChecks";
import { EvalQuestionsDialog, EvalRunSection } from "./StudioEval";

/* ---------------------------------------------------------------------------
   Studio authoring (R11-AUD08): creating a change set, adding and removing items,
   running the tests, detecting conflicts, the eval run and mined questions, and the
   two definition checks -- the routes the read-only screen never called.

   The properties, and why each was a real way to get an authoring surface wrong:

     1. THE WRITE ROLES GET THE CONTROLS, NOBODY ELSE, AND NOT ON A GUESS.
        DataSteward, MetadataAdmin, PlatformAdmin and SemanticAdmin see New change
        set, Add item, Run tests, Detect conflicts and Submit; the four roles that
        may only READ Studio see none of them. `roleHolds` is fail-closed, so a
        session whose identity has not answered is offered nothing that writes.
     2. EVERY READ IS HELD UNTIL IDENTITY ANSWERS AND NEVER SENT TO A ROLE OUTSIDE
        THE LIST. The list used to be requested unconditionally.
     3. THE STATUS DECIDES WHAT MAY BE EDITED, FROM THE HANDLERS: items are added and
        removed only while DRAFT; tests and submission in DRAFT or TESTING; a
        SUBMITTED, MERGED or REJECTED change set is read-only.
     4. NOTHING FIRES ON A CLICK THAT CHANGES SOMETHING PERMANENT. Removing an item and
        running the tests (which LOCKS a DRAFT's items) each ask first, and say what
        they do.
     5. A REFUSAL IS AN ANSWER, IN THE SERVER'S WORDS. Every write keeps its dialog
        open and shows the sentence the API sent.
     6. THE SCREEN SHOWS WHAT THE API HOLDS. After a write it re-reads; a write the
        API acknowledged but the re-read does not show is reported, not claimed.
     7. WHAT THE API RETURNS IS WHAT IS SHOWN: the test run's totals, the conflicts
        with both values, the eval run and its per-question reasons, each validator's
        errors -- and where the API does not say (why an item failed), the screen says
        it does not.
--------------------------------------------------------------------------- */

const ORG = "org1";
const T = "2026-09-01T00:00:00Z";

const fetchStudioChangeSets = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<StudioChangeSetRead[]>>();
const fetchStudioChangeSetItems = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioChangeItemRead[]>>();
const fetchStudioDiff = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioDiffRead>>();
const fetchStudioImpact = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioImpactPreview>>();
const submitStudioChangeSet = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioChangeSetRead>>();
const createStudioChangeSet = vi.fn<(body: StudioChangeSetCreate, signal?: AbortSignal) => Promise<StudioChangeSetRead>>();
const addStudioChangeItem =
  vi.fn<(id: string, body: StudioChangeItemCreate, signal?: AbortSignal) => Promise<StudioChangeItemRead>>();
const removeStudioChangeItem = vi.fn<(id: string, itemId: string, signal?: AbortSignal) => Promise<void>>();
const runStudioTests = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioTestResultRead>>();
const detectStudioConflicts =
  vi.fn<(id: string, state: StudioPublishedState | null, signal?: AbortSignal) => Promise<StudioConflict[]>>();
const fetchStudioEvalRun = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioEvalRunRead>>();
const mineStudioEvalQuestions = vi.fn<(signal?: AbortSignal) => Promise<StudioEvalMiningResult>>();
const fetchStudioEvalQuestions =
  vi.fn<(query: { objectType?: string | null; limit?: number }, signal?: AbortSignal) => Promise<StudioEvalQuestionRead[]>>();
const validateStudioContextProduct =
  vi.fn<(body: StudioContextProductValidateRequest, signal?: AbortSignal) => Promise<StudioContextProductValidateResult>>();
const validateStudioParameterContract =
  vi.fn<(body: StudioParameterContractValidateRequest, signal?: AbortSignal) => Promise<StudioParameterContractValidateResult>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchStudioChangeSets: (query: unknown, signal?: AbortSignal) => fetchStudioChangeSets(query, signal),
    fetchStudioChangeSetItems: (id: string, signal?: AbortSignal) => fetchStudioChangeSetItems(id, signal),
    fetchStudioDiff: (id: string, signal?: AbortSignal) => fetchStudioDiff(id, signal),
    fetchStudioImpact: (id: string, signal?: AbortSignal) => fetchStudioImpact(id, signal),
    submitStudioChangeSet: (id: string, signal?: AbortSignal) => submitStudioChangeSet(id, signal),
    createStudioChangeSet: (body: StudioChangeSetCreate, signal?: AbortSignal) => createStudioChangeSet(body, signal),
    addStudioChangeItem: (id: string, body: StudioChangeItemCreate, signal?: AbortSignal) =>
      addStudioChangeItem(id, body, signal),
    removeStudioChangeItem: (id: string, itemId: string, signal?: AbortSignal) => removeStudioChangeItem(id, itemId, signal),
    runStudioTests: (id: string, signal?: AbortSignal) => runStudioTests(id, signal),
    detectStudioConflicts: (id: string, state: StudioPublishedState | null, signal?: AbortSignal) =>
      detectStudioConflicts(id, state, signal),
    fetchStudioEvalRun: (id: string, signal?: AbortSignal) => fetchStudioEvalRun(id, signal),
    mineStudioEvalQuestions: (signal?: AbortSignal) => mineStudioEvalQuestions(signal),
    fetchStudioEvalQuestions: (query: { objectType?: string | null; limit?: number }, signal?: AbortSignal) =>
      fetchStudioEvalQuestions(query, signal),
    validateStudioContextProduct: (body: StudioContextProductValidateRequest, signal?: AbortSignal) =>
      validateStudioContextProduct(body, signal),
    validateStudioParameterContract: (body: StudioParameterContractValidateRequest, signal?: AbortSignal) =>
      validateStudioParameterContract(body, signal),
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

const WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];
const READ_ONLY_ROLES = ["Analyst", "Auditor", "Reviewer", "Viewer"];

const changeSet = (over: Partial<StudioChangeSetRead> = {}): StudioChangeSetRead => ({
  id: "cs_1", organization_id: ORG, name: "Exclude intercompany transfers", author: "priya",
  status: "DRAFT", base_version_hash: "0".repeat(64), conflict_status: "CLEAN", created_at: T, updated_at: T,
  ...over,
});

const item = (over: Partial<StudioChangeItemRead> = {}): StudioChangeItemRead => ({
  id: "item_1", organization_id: ORG, change_set_id: "cs_1", object_type: "METRIC", object_id: "metric:revenue",
  operation: "UPDATE", before_snapshot: null, after_snapshot: null, diff: null, test_status: "UNTESTED",
  created_at: T, updated_at: T,
  ...over,
});

const NO_EVAL_RUN = "no eval run recorded for this change set";

beforeEach(() => {
  for (const mock of [
    fetchStudioChangeSets, fetchStudioChangeSetItems, fetchStudioDiff, fetchStudioImpact, submitStudioChangeSet,
    createStudioChangeSet, addStudioChangeItem, removeStudioChangeItem, runStudioTests, detectStudioConflicts,
    fetchStudioEvalRun, mineStudioEvalQuestions, fetchStudioEvalQuestions, validateStudioContextProduct,
    validateStudioParameterContract,
  ]) {
    mock.mockReset();
  }
  fetchStudioChangeSets.mockResolvedValue([]);
  fetchStudioChangeSetItems.mockResolvedValue([]);
  fetchStudioDiff.mockResolvedValue({ change_set_id: "cs_1", items: [] });
  fetchStudioImpact.mockResolvedValue({ change_set_id: "cs_1", affected_object_count: 0, affected_objects: [] });
  fetchStudioEvalRun.mockRejectedValue(new ApiError(404, NO_EVAL_RUN));
  fetchStudioEvalQuestions.mockResolvedValue([]);
  sessionMe = null;
  sessionState = "connected";
  history.replaceState(null, "", "/#/studio");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** The screen with one change set listed, opened, and its items loaded. */
async function openDetail(
  roles: string[],
  options: { cs?: Partial<StudioChangeSetRead>; items?: StudioChangeItemRead[] } = {},
) {
  sessionMe = asRoles(...roles);
  fetchStudioChangeSets.mockResolvedValue([changeSet(options.cs)]);
  fetchStudioChangeSetItems.mockResolvedValue(options.items ?? []);
  const view = render(<StudioChangeSetsScreen />);
  fireEvent.click(await screen.findByRole("button", { name: /Exclude intercompany transfers/ }));
  const pane = await screen.findByLabelText(/Detail for Exclude intercompany transfers/);
  await within(pane).findByText(/^Items \(/);
  return { view, pane };
}

const dialogNamed = (name: string) => screen.findByRole("dialog", { name });

/* ---------------------------------------------------------------------------
   1. Reads: held, and never sent to a role outside the list
--------------------------------------------------------------------------- */

describe("Studio reads: who is asked, and when", () => {
  it.each([...WRITE_ROLES, ...READ_ONLY_ROLES])("asks for the list as %s", async (role) => {
    sessionMe = asRoles(role);
    render(<StudioChangeSetsScreen />);

    await waitFor(() => expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("No change sets")).toBeInTheDocument();
  });

  it.each(["Operations", "AgentDeveloper", "ToolDeveloper", "DataAdmin"])(
    "asks for nothing as %s, and says the screen is not available rather than showing a 403",
    async (role) => {
      sessionMe = asRoles(role);
      render(<StudioChangeSetsScreen />);

      expect(await screen.findByText("Studio is not available to your roles")).toBeInTheDocument();
      expect(screen.getByText(/Only sessions holding Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin or Viewer/)).toBeInTheDocument();
      expect(fetchStudioChangeSets).not.toHaveBeenCalled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "New change set" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Eval questions" })).not.toBeInTheDocument();
    },
  );

  it("holds the list while identity is in flight, offers nothing that writes, then asks once identity says it may", async () => {
    sessionState = "connecting";
    const view = render(<StudioChangeSetsScreen />);

    expect(await screen.findByText("Loading change sets…")).toBeInTheDocument();
    expect(fetchStudioChangeSets).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "New change set" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Eval questions" })).not.toBeInTheDocument();
    expect(screen.queryByText("Studio is not available to your roles")).not.toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("DataSteward");
    view.rerender(<StudioChangeSetsScreen />);

    await waitFor(() => expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("button", { name: "New change set" })).toBeEnabled();
  });

  it("never sends the list when identity then says the session may not read it", async () => {
    sessionState = "connecting";
    const view = render(<StudioChangeSetsScreen />);
    await screen.findByText("Loading change sets…");

    sessionState = "connected";
    sessionMe = asRoles("Operations");
    view.rerender(<StudioChangeSetsScreen />);

    expect(await screen.findByText("Studio is not available to your roles")).toBeInTheDocument();
    expect(fetchStudioChangeSets).not.toHaveBeenCalled();
  });

  it("still asks when identity will not answer -- the server stays the authority -- and offers nothing that writes", async () => {
    sessionState = "disconnected";
    render(<StudioChangeSetsScreen />);

    await waitFor(() => expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1));
    await screen.findByText("No change sets");
    expect(screen.queryByRole("button", { name: "New change set" })).not.toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   2. Who is offered which control, and when
--------------------------------------------------------------------------- */

describe("Studio controls by role", () => {
  it.each(WRITE_ROLES)("offers New change set to %s", async (role) => {
    sessionMe = asRoles(role);
    render(<StudioChangeSetsScreen />);

    expect(await screen.findByRole("button", { name: "New change set" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Eval questions" })).toBeEnabled();
  });

  it.each(READ_ONLY_ROLES)("offers %s the eval questions to read and no way to create a change set", async (role) => {
    sessionMe = asRoles(role);
    render(<StudioChangeSetsScreen />);

    expect(await screen.findByRole("button", { name: "Eval questions" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "New change set" })).not.toBeInTheDocument();
    expect(screen.getByText("Nothing has been created yet.")).toBeInTheDocument();
  });

  it("points a write role at New change set from the empty list", async () => {
    sessionMe = asRoles("DataSteward");
    render(<StudioChangeSetsScreen />);

    expect(await screen.findByText("Use New change set to start one.")).toBeInTheDocument();
  });

  it.each(WRITE_ROLES)("offers %s every authoring control on a DRAFT, and a Remove on each item", async (role) => {
    const { pane } = await openDetail([role], { items: [item(), item({ id: "item_2", object_id: "metric:margin" })] });

    expect(within(pane).getByRole("button", { name: "Add item" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Run tests" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Detect conflicts" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Submit for review" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Remove METRIC metric:revenue" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Remove METRIC metric:margin" })).toBeEnabled();
  });

  it.each(READ_ONLY_ROLES)("offers %s no control that writes, and says why", async (role) => {
    const { pane } = await openDetail([role], { items: [item()] });

    for (const name of ["Add item", "Run tests", "Detect conflicts", "Submit for review"]) {
      expect(within(pane).queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(within(pane).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(within(pane).getByText(/read-only for your roles — editing and submitting need DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin/)).toBeInTheDocument();
    // What they may do -- read the change set -- is intact.
    expect(within(pane).getByText("Items (1)")).toBeInTheDocument();
  });

  it("offers nothing that writes, and says nothing about roles, while the session's roles are unknown", async () => {
    sessionState = "disconnected";
    sessionMe = null;
    fetchStudioChangeSets.mockResolvedValue([changeSet()]);
    fetchStudioChangeSetItems.mockResolvedValue([item()]);
    render(<StudioChangeSetsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /Exclude intercompany transfers/ }));
    const pane = await screen.findByLabelText(/Detail for Exclude intercompany transfers/);
    await within(pane).findByText("Items (1)");

    expect(within(pane).queryByRole("button", { name: "Add item" })).not.toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: "Submit for review" })).not.toBeInTheDocument();
    expect(within(pane).queryByText(/read-only for your roles/)).not.toBeInTheDocument();
  });

  it("locks a TESTING change set's items but still offers the tests, conflicts and submission", async () => {
    const { pane } = await openDetail(["DataSteward"], { cs: { status: "TESTING" }, items: [item({ test_status: "PASSED" })] });

    expect(within(pane).queryByRole("button", { name: "Add item" })).not.toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Run tests" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Detect conflicts" })).toBeEnabled();
    expect(within(pane).getByRole("button", { name: "Submit for review" })).toBeEnabled();
    expect(within(pane).getByText(/Items are locked: they can only be added or removed while a change set is DRAFT\. This one is testing\./)).toBeInTheDocument();
  });

  it.each(["SUBMITTED", "MERGED", "REJECTED"])(
    "makes a %s change set read-only even for a write role",
    async (status) => {
      const { pane } = await openDetail(["PlatformAdmin"], { cs: { status }, items: [item({ test_status: "PASSED" })] });

      for (const name of ["Add item", "Run tests", "Detect conflicts", "Submit for review"]) {
        expect(within(pane).queryByRole("button", { name })).not.toBeInTheDocument();
      }
      expect(within(pane).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
      expect(within(pane).getByText(`${status.toLowerCase()} — nothing left to submit`)).toBeInTheDocument();
      expect(within(pane).getByText(/Items are locked/)).toBeInTheDocument();
    },
  );

  it("tones an item by its test status: passed, failed, untested", async () => {
    const { pane } = await openDetail(["Viewer"], {
      items: [
        item({ id: "a", object_id: "a", test_status: "PASSED" }),
        item({ id: "b", object_id: "b", test_status: "FAILED" }),
        item({ id: "c", object_id: "c", test_status: "UNTESTED" }),
      ],
    });

    const tone = (id: string) => within(pane).getByText(id).closest("li")!.className;
    expect(tone("a")).toContain("evi--ok");
    expect(tone("b")).toContain("evi--bad");
    expect(tone("c")).toContain("evi--info");
  });

  it("says an empty change set has no items rather than showing an empty list", async () => {
    const { pane } = await openDetail(["Viewer"]);

    expect(within(pane).getByText("Items (0)")).toBeInTheDocument();
    expect(within(pane).getByText("This change set has no items yet.")).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   3. New change set
--------------------------------------------------------------------------- */

describe("New change set", () => {
  async function openDialog() {
    sessionMe = asRoles("DataSteward");
    const view = render(<StudioChangeSetsScreen />);
    const trigger = await screen.findByRole("button", { name: "New change set" });
    trigger.focus(); // a real click focuses its button; `fireEvent.click` does not
    fireEvent.click(trigger);
    return { view, dialog: await dialogNamed("New change set") };
  }

  it("creates the change set, closes, selects it, and re-reads the list", async () => {
    const created = changeSet({ id: "cs_new", name: "Fix revenue grain" });
    fetchStudioChangeSets.mockResolvedValueOnce([]).mockResolvedValueOnce([created]);
    createStudioChangeSet.mockResolvedValue(created);
    const { dialog } = await openDialog();

    const create = within(dialog).getByRole("button", { name: "Create change set" });
    expect(create).toBeDisabled(); // nothing typed yet, nothing to send
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "  Fix revenue grain  " } });
    fireEvent.click(create);

    await waitFor(() => expect(createStudioChangeSet).toHaveBeenCalledWith({ name: "Fix revenue grain" }, undefined));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByLabelText(/Detail for Fix revenue grain/)).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("cs")).toBe("cs_new");
    expect(fetchStudioChangeSets).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Created “Fix revenue grain” as a draft.")).toBeInTheDocument();
  });

  it("opens on the name field, so typing starts at once", async () => {
    const { dialog } = await openDialog();

    expect(document.activeElement).toBe(within(dialog).getByLabelText(/^Name/));
  });

  it("submits on Enter from the name field", async () => {
    createStudioChangeSet.mockResolvedValue(changeSet({ id: "cs_new", name: "Enter to create" }));
    const { dialog } = await openDialog();

    const name = within(dialog).getByLabelText(/^Name/);
    fireEvent.change(name, { target: { value: "Enter to create" } });
    fireEvent.keyDown(name, { key: "Enter" });

    await waitFor(() => expect(createStudioChangeSet).toHaveBeenCalledWith({ name: "Enter to create" }, undefined));
  });

  it("sends a name the server will refuse, and shows the server's own sentence in the dialog", async () => {
    // Length is the server's to judge: one character is sent, not pre-empted.
    createStudioChangeSet.mockRejectedValue(new ApiError(422, "body.name: String should have at least 2 characters"));
    const { dialog } = await openDialog();

    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "x" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create change set" }));

    const refusal = await within(dialog).findByRole("alert");
    expect(refusal).toHaveTextContent(/^body\.name: String should have at least 2 characters$/);
    expect(createStudioChangeSet).toHaveBeenCalledWith({ name: "x" }, undefined);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/as a draft/)).not.toBeInTheDocument();
    // Fixable in place.
    expect(within(dialog).getByRole("button", { name: "Create change set" })).toBeEnabled();
  });

  it("shows a 403 as the server sent it", async () => {
    createStudioChangeSet.mockRejectedValue(new ApiError(403, "requires one of: DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin"));
    const { dialog } = await openDialog();

    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "Fix revenue grain" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create change set" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /^requires one of: DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin$/,
    );
  });

  it("admits one create at a time and shows the dialog busy meanwhile", async () => {
    let settle: (created: StudioChangeSetRead) => void = () => undefined;
    createStudioChangeSet.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const { dialog } = await openDialog();

    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "Fix revenue grain" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create change set" }));

    const working = await within(dialog).findByRole("button", { name: "Creating…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);
    expect(createStudioChangeSet).toHaveBeenCalledTimes(1);
    settle(changeSet({ id: "cs_new", name: "Fix revenue grain" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("cancels without sending anything, and does not throw away a typed name on a click outside", async () => {
    const { dialog } = await openDialog();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "Fix revenue grain" } });

    fireEvent.mouseDown(dialog.parentElement!);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).getByLabelText(/^Name/)).toHaveValue("Fix revenue grain");

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(createStudioChangeSet).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "New change set" }));
  });

  it("clears a status filter that would hide the new DRAFT, and reads the list without it", async () => {
    history.replaceState(null, "", "/?status=SUBMITTED#/studio");
    resetLocationCacheForTests();
    const created = changeSet({ id: "cs_new", name: "Fix revenue grain" });
    fetchStudioChangeSets.mockResolvedValueOnce([]).mockResolvedValueOnce([created]);
    createStudioChangeSet.mockResolvedValue(created);
    const { dialog } = await openDialog();
    expect(fetchStudioChangeSets).toHaveBeenLastCalledWith({ status: "SUBMITTED", limit: 200 }, expect.anything());

    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "Fix revenue grain" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create change set" }));

    expect(await screen.findByLabelText(/Detail for Fix revenue grain/)).toBeInTheDocument();
    const query = new URLSearchParams(location.search);
    expect(query.get("status")).toBeNull();
    expect(query.get("cs")).toBe("cs_new");
    expect(fetchStudioChangeSets).toHaveBeenLastCalledWith({ status: null, limit: 200 }, expect.anything());
  });

  it("says so when the API accepted the change set but the list does not hold it", async () => {
    // Acknowledged and not kept: the screen must not select a row that is not there and call it created.
    createStudioChangeSet.mockResolvedValue(changeSet({ id: "cs_ghost", name: "Fix revenue grain" }));
    const { dialog } = await openDialog();

    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "Fix revenue grain" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create change set" }));

    expect(
      await screen.findByText(/The API accepted “Fix revenue grain” \(cs_ghost\) but the change set list does not include it\./),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/Detail for/)).not.toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   4. Add item
--------------------------------------------------------------------------- */

describe("Add item", () => {
  async function openAdd(options: Parameters<typeof openDetail>[1] = {}) {
    const opened = await openDetail(["DataSteward"], options);
    fireEvent.click(within(opened.pane).getByRole("button", { name: "Add item" }));
    return { ...opened, dialog: await dialogNamed("Add item") };
  }
  const fill = (dialog: HTMLElement, label: RegExp, value: string) =>
    fireEvent.change(within(dialog).getByLabelText(label), { target: { value } });

  it("opens on the object type, the first thing to choose", async () => {
    const { dialog } = await openAdd();

    expect(document.activeElement).toBe(within(dialog).getByLabelText(/^Object type/));
  });

  it("posts the item with its snapshots parsed, then re-reads the items and says what was added", async () => {
    const added = item({ id: "item_9", object_id: "metric:net_revenue", operation: "UPDATE" });
    addStudioChangeItem.mockResolvedValue(added);
    const { dialog, pane } = await openAdd();
    fetchStudioChangeSetItems.mockResolvedValue([added]); // what the re-read finds

    fill(dialog, /^Object type/, "METRIC");
    fill(dialog, /^Operation/, "UPDATE");
    fill(dialog, /^Object id/, "  metric:net_revenue  ");
    fill(dialog, /^Before snapshot/, '{"grain": "day"}');
    fill(dialog, /^After snapshot/, '{"name": "net_revenue", "aggregation": "SUM", "grain": "month"}');
    fireEvent.click(within(dialog).getByRole("button", { name: "Add item" }));

    await waitFor(() =>
      expect(addStudioChangeItem).toHaveBeenCalledWith(
        "cs_1",
        {
          object_type: "METRIC",
          object_id: "metric:net_revenue",
          operation: "UPDATE",
          before_snapshot: { grain: "day" },
          after_snapshot: { name: "net_revenue", aggregation: "SUM", grain: "month" },
        },
        undefined,
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await within(pane).findByText("Items (1)")).toBeInTheDocument();
    expect(within(pane).getByText("metric:net_revenue")).toBeInTheDocument();
    expect(within(pane).getByText("Added METRIC metric:net_revenue (update).")).toBeInTheDocument();
    expect(fetchStudioChangeSetItems).toHaveBeenCalledTimes(2);
  });

  it("leaves an empty snapshot out of the request", async () => {
    addStudioChangeItem.mockResolvedValue(item({ id: "item_9", object_id: "term:churn", object_type: "TERM", operation: "DELETE" }));
    const { dialog } = await openAdd();

    fill(dialog, /^Object type/, "TERM");
    fill(dialog, /^Operation/, "DELETE");
    fill(dialog, /^Object id/, "term:churn");
    fireEvent.click(within(dialog).getByRole("button", { name: "Add item" }));

    await waitFor(() => expect(addStudioChangeItem).toHaveBeenCalledTimes(1));
    expect(addStudioChangeItem.mock.calls[0]![1]).toEqual({ object_type: "TERM", object_id: "term:churn", operation: "DELETE" });
  });

  it("holds Add item back until there is an object id", async () => {
    const { dialog } = await openAdd();

    const add = within(dialog).getByRole("button", { name: "Add item" });
    expect(add).toBeDisabled();
    fill(dialog, /^Object id/, "   ");
    expect(add).toBeDisabled();
    fill(dialog, /^Object id/, "metric:revenue");
    expect(add).toBeEnabled();
  });

  it.each([
    ["After snapshot", /^After snapshot/, "{oops", /^After snapshot is not valid JSON: /],
    ["Before snapshot", /^Before snapshot/, "{oops", /^Before snapshot is not valid JSON: /],
    ["After snapshot", /^After snapshot/, "[1, 2]", /^After snapshot must be a JSON object/],
    ["Before snapshot", /^Before snapshot/, '"text"', /^Before snapshot must be a JSON object/],
  ])("reports a %s that is not a JSON object itself, and sends nothing", async (_name, field, text, message) => {
    const { dialog } = await openAdd();
    fill(dialog, /^Object id/, "metric:revenue");
    fill(dialog, field, text);

    fireEvent.click(within(dialog).getByRole("button", { name: "Add item" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(addStudioChangeItem).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps the dialog open on a refusal and shows the server's own sentence", async () => {
    addStudioChangeItem.mockRejectedValue(new ApiError(409, "items can only be added to DRAFT change sets"));
    const { dialog } = await openAdd();
    fill(dialog, /^Object id/, "metric:revenue");

    fireEvent.click(within(dialog).getByRole("button", { name: "Add item" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^items can only be added to DRAFT change sets$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // Nothing was added, so nothing is re-read on its account and no success is claimed.
    expect(fetchStudioChangeSetItems).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/^Added /)).not.toBeInTheDocument();
  });

  it("shows a 422 from the server as it is sent", async () => {
    addStudioChangeItem.mockRejectedValue(new ApiError(422, "body.object_id: String should have at most 100 characters"));
    const { dialog } = await openAdd();
    fill(dialog, /^Object id/, "x".repeat(101));

    fireEvent.click(within(dialog).getByRole("button", { name: "Add item" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /^body\.object_id: String should have at most 100 characters$/,
    );
  });

  it("says so when the API accepted the item but the re-read does not list it", async () => {
    addStudioChangeItem.mockResolvedValue(item({ id: "item_ghost", object_id: "metric:ghost" }));
    const { dialog, pane } = await openAdd();
    fill(dialog, /^Object id/, "metric:ghost");

    fireEvent.click(within(dialog).getByRole("button", { name: "Add item" }));

    expect(
      await within(pane).findByText(/The API accepted METRIC metric:ghost but it is not in this change set's item list\./),
    ).toBeInTheDocument();
    expect(within(pane).queryByText(/^Added /)).not.toBeInTheDocument();
  });

  it.each([
    ["METRIC", /aggregation \(SUM, COUNT, AVG, MIN or MAX\) and grain/],
    ["TOOL", /name, sql_template and allowed_roles/],
    ["TERM", /display_name and a definition of at least 10 characters/],
    ["CONTEXT_PRODUCT", /must equal after_snapshot\.product_key/],
  ])("tells the author what a test will ask of a %s", async (type, hint) => {
    const { dialog } = await openAdd();

    fill(dialog, /^Object type/, type);

    expect(within(dialog).getByText(hint)).toBeInTheDocument();
    expect(within(dialog).getByText(/A DELETE needs no snapshot\./)).toBeInTheDocument();
  });

  it("offers a Check only for the two types that have a validator", async () => {
    const { dialog } = await openAdd();

    expect(within(dialog).queryByRole("button", { name: /^Check/ })).not.toBeInTheDocument(); // METRIC
    fill(dialog, /^Object type/, "TERM");
    expect(within(dialog).queryByRole("button", { name: /^Check/ })).not.toBeInTheDocument();
    fill(dialog, /^Object type/, "TOOL");
    expect(within(dialog).getByRole("button", { name: "Check contract" })).toBeEnabled();
    fill(dialog, /^Operation/, "DELETE"); // the test accepts a tool DELETE without looking
    expect(within(dialog).queryByRole("button", { name: /^Check/ })).not.toBeInTheDocument();
    fill(dialog, /^Object type/, "CONTEXT_PRODUCT");
    expect(within(dialog).getByRole("button", { name: "Check definition" })).toBeEnabled();
  });

  it("checks a drafted TOOL's parameter contract with what the snapshot says, and shows the sample render", async () => {
    validateStudioParameterContract.mockResolvedValue({
      valid: true, errors: [], definitions: [{ name: "region", parameter_type: "STRING" }],
      sample_rendered_sql: "SELECT * FROM sales WHERE region = 'sample'",
    });
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "TOOL");
    fill(dialog, /^Operation/, "CREATE");
    fill(dialog, /^After snapshot/, JSON.stringify({
      name: "sales_by_region", sql_template: "SELECT * FROM sales WHERE region = :region", allowed_roles: ["Analyst"],
      parameters: [{ name: "region", parameter_type: "STRING" }], dialect: "postgres",
    }));

    fireEvent.click(within(dialog).getByRole("button", { name: "Check contract" }));

    await waitFor(() =>
      expect(validateStudioParameterContract).toHaveBeenCalledWith(
        {
          sql_template: "SELECT * FROM sales WHERE region = :region",
          parameters: [{ name: "region", parameter_type: "STRING" }],
          dialect: "postgres",
        },
        undefined,
      ),
    );
    const outcome = await within(dialog).findByText("valid");
    expect(outcome).toBeInTheDocument();
    expect(within(dialog).getByText("SELECT * FROM sales WHERE region = 'sample'")).toBeInTheDocument();
    // A check is not a test, and is not an add.
    expect(addStudioChangeItem).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/A test also needs name, sql_template and allowed_roles\./)).toBeInTheDocument();
  });

  it("leaves dialect out of the contract check when the snapshot names none, and defaults parameters to a list", async () => {
    validateStudioParameterContract.mockResolvedValue({ valid: true, errors: [], definitions: [] });
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "TOOL");
    fill(dialog, /^After snapshot/, '{"sql_template": "SELECT 1"}');

    fireEvent.click(within(dialog).getByRole("button", { name: "Check contract" }));

    await waitFor(() => expect(validateStudioParameterContract).toHaveBeenCalledTimes(1));
    expect(validateStudioParameterContract.mock.calls[0]![0]).toEqual({ sql_template: "SELECT 1", parameters: [] });
  });

  it("shows every error a contract check returns, as the server words them", async () => {
    validateStudioParameterContract.mockResolvedValue({
      valid: false,
      errors: ["undeclared placeholders: region", "unused parameter definitions: country"],
      definitions: [],
    });
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "TOOL");
    fill(dialog, /^After snapshot/, '{"sql_template": "SELECT :region"}');

    fireEvent.click(within(dialog).getByRole("button", { name: "Check contract" }));

    expect(await within(dialog).findByText("not valid")).toBeInTheDocument();
    expect(within(dialog).getByText("undeclared placeholders: region")).toBeInTheDocument();
    expect(within(dialog).getByText("unused parameter definitions: country")).toBeInTheDocument();
    expect(within(dialog).queryByText(/A test also needs/)).not.toBeInTheDocument();
  });

  it.each([
    ["no after snapshot", "", "There is no after snapshot to check."],
    ["no sql_template", '{"name": "t"}', "The after snapshot has no sql_template to check."],
    ["a parameters value that is not a list", '{"sql_template": "SELECT 1", "parameters": "x"}', "The after snapshot's parameters is not a list."],
  ])("says why a contract cannot be checked yet (%s), without asking the API", async (_case, snapshot, message) => {
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "TOOL");
    fill(dialog, /^After snapshot/, snapshot);

    fireEvent.click(within(dialog).getByRole("button", { name: "Check contract" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(validateStudioParameterContract).not.toHaveBeenCalled();
  });

  it("checks a drafted CONTEXT_PRODUCT with its operation, id and snapshot", async () => {
    validateStudioContextProduct.mockResolvedValue({
      valid: true, errors: [], definition: { name: "Customer 360" }, product_key: "cp_customer360",
      project_id: "3f0c1c9e-0000-4000-8000-000000000001",
    });
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "CONTEXT_PRODUCT");
    fill(dialog, /^Operation/, "CREATE");
    fill(dialog, /^Object id/, " cp_customer360 ");
    fill(dialog, /^After snapshot/, '{"product_key": "cp_customer360", "name": "Customer 360"}');

    fireEvent.click(within(dialog).getByRole("button", { name: "Check definition" }));

    await waitFor(() =>
      expect(validateStudioContextProduct).toHaveBeenCalledWith(
        { operation: "CREATE", object_id: "cp_customer360", snapshot: { product_key: "cp_customer360", name: "Customer 360" } },
        undefined,
      ),
    );
    expect(await within(dialog).findByText("valid")).toBeInTheDocument();
    expect(within(dialog).getByText(/product key cp_customer360/)).toBeInTheDocument();
    // What "valid" does not cover.
    expect(within(dialog).getByText(/references are not looked up here; they are checked when the change set is submitted/i)).toBeInTheDocument();
  });

  it("checks a CONTEXT_PRODUCT DELETE with no snapshot at all", async () => {
    validateStudioContextProduct.mockResolvedValue({ valid: false, errors: ["object_id must be an existing context product UUID for DELETE: 'cp_1'"] });
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "CONTEXT_PRODUCT");
    fill(dialog, /^Operation/, "DELETE");
    fill(dialog, /^Object id/, "cp_1");

    fireEvent.click(within(dialog).getByRole("button", { name: "Check definition" }));

    await waitFor(() => expect(validateStudioContextProduct).toHaveBeenCalledWith({ operation: "DELETE", object_id: "cp_1" }, undefined));
    expect(await within(dialog).findByText("object_id must be an existing context product UUID for DELETE: 'cp_1'")).toBeInTheDocument();
  });

  it("asks for the object id before checking a context product", async () => {
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "CONTEXT_PRODUCT");

    fireEvent.click(within(dialog).getByRole("button", { name: "Check definition" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Enter the object id to check it.");
    expect(validateStudioContextProduct).not.toHaveBeenCalled();
  });

  it("shows a validator's own refusal, and reports a snapshot that is not JSON without asking", async () => {
    validateStudioContextProduct.mockRejectedValue(new ApiError(422, "body.object_id: String should have at least 1 character"));
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "CONTEXT_PRODUCT");
    fill(dialog, /^Object id/, "cp_1");
    fill(dialog, /^After snapshot/, "{oops");

    fireEvent.click(within(dialog).getByRole("button", { name: "Check definition" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^After snapshot is not valid JSON: /);
    expect(validateStudioContextProduct).not.toHaveBeenCalled();

    fill(dialog, /^After snapshot/, '{"name": "x"}');
    fireEvent.click(within(dialog).getByRole("button", { name: "Check definition" }));
    expect(await within(dialog).findByText(/^body\.object_id: String should have at least 1 character$/)).toBeInTheDocument();
  });

  it("does not carry a check's answer over to a different type", async () => {
    validateStudioParameterContract.mockResolvedValue({ valid: true, errors: [], definitions: [] });
    const { dialog } = await openAdd();
    fill(dialog, /^Object type/, "TOOL");
    fill(dialog, /^After snapshot/, '{"sql_template": "SELECT 1"}');
    fireEvent.click(within(dialog).getByRole("button", { name: "Check contract" }));
    await within(dialog).findByText("valid");

    fill(dialog, /^Object type/, "CONTEXT_PRODUCT");

    expect(within(dialog).queryByText("valid")).not.toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   5. Remove item
--------------------------------------------------------------------------- */

describe("Remove item", () => {
  async function openRemove() {
    const opened = await openDetail(["DataSteward"], {
      items: [item(), item({ id: "item_2", object_id: "metric:margin" })],
    });
    fireEvent.click(within(opened.pane).getByRole("button", { name: "Remove METRIC metric:revenue" }));
    return { ...opened, dialog: await dialogNamed("Remove METRIC metric:revenue?") };
  }

  it("says what it removes and sends nothing on the click", async () => {
    const { dialog } = await openRemove();

    expect(dialog).toHaveTextContent("This deletes the item from the change set");
    expect(dialog).toHaveTextContent("The governed object it named is not touched");
    expect(dialog).toHaveTextContent("records the removal in the audit ledger");
    expect(removeStudioChangeItem).not.toHaveBeenCalled();
  });

  it("removes the item on confirmation, then re-reads and says so", async () => {
    removeStudioChangeItem.mockResolvedValue(undefined);
    const { dialog, pane } = await openRemove();
    fetchStudioChangeSetItems.mockResolvedValue([item({ id: "item_2", object_id: "metric:margin" })]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove item" }));

    await waitFor(() => expect(removeStudioChangeItem).toHaveBeenCalledWith("cs_1", "item_1", undefined));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await within(pane).findByText("Items (1)")).toBeInTheDocument();
    expect(within(pane).queryByText("metric:revenue")).not.toBeInTheDocument();
    expect(within(pane).getByText("Removed METRIC metric:revenue.")).toBeInTheDocument();
  });

  it("cancels without removing anything", async () => {
    const { dialog, pane } = await openRemove();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(removeStudioChangeItem).not.toHaveBeenCalled();
    expect(within(pane).getByText("Items (2)")).toBeInTheDocument();
  });

  it("keeps the confirmation open on a refusal, in the server's words, and claims nothing", async () => {
    removeStudioChangeItem.mockRejectedValue(new ApiError(409, "items can only be removed from DRAFT change sets"));
    const { dialog, pane } = await openRemove();

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove item" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^items can only be removed from DRAFT change sets$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(fetchStudioChangeSetItems).toHaveBeenCalledTimes(1);
    expect(within(pane).queryByText(/^Removed /)).not.toBeInTheDocument();
  });

  it("shows a 404 for an item that is already gone as the server sent it", async () => {
    removeStudioChangeItem.mockRejectedValue(new ApiError(404, "change item not found"));
    const { dialog } = await openRemove();

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove item" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^change item not found$/);
  });

  it("says so when the API accepted the removal but the item is still listed", async () => {
    removeStudioChangeItem.mockResolvedValue(undefined);
    const { dialog, pane } = await openRemove();
    // the re-read still returns both items

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove item" }));

    expect(
      await within(pane).findByText(/The API accepted the removal of METRIC metric:revenue but the item is still listed\./),
    ).toBeInTheDocument();
    expect(within(pane).queryByText(/^Removed /)).not.toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   6. Run tests
--------------------------------------------------------------------------- */

const PASSED_RUN: StudioTestResultRead = {
  change_set_id: "cs_1", started_at: "2026-09-02T10:00:00Z", completed_at: "2026-09-02T10:00:01Z", passed: true,
  evidence: { total_items: 2, passed_items: 2, failed_items: 0, eval_regression_checked: 1, eval_regression_failed: 0 },
};

describe("Run tests", () => {
  async function openRun(cs: Partial<StudioChangeSetRead> = {}) {
    const opened = await openDetail(["DataSteward"], {
      cs, items: [item(), item({ id: "item_2", object_id: "metric:margin" })],
    });
    fireEvent.click(within(opened.pane).getByRole("button", { name: "Run tests" }));
    return { ...opened, dialog: await dialogNamed("Run the tests?") };
  }

  it("on a DRAFT, says it moves the change set to TESTING and locks the items, and sends nothing on the click", async () => {
    const { dialog } = await openRun();

    expect(dialog).toHaveTextContent("Runs each item's checks and the eval-regression gate");
    expect(dialog).toHaveTextContent("moves the change set to TESTING");
    expect(dialog).toHaveTextContent("items can no longer be added or removed");
    expect(dialog).toHaveTextContent("records the outcome in the audit ledger");
    expect(runStudioTests).not.toHaveBeenCalled();
  });

  it("on a TESTING change set, says it is a re-run and does not claim to lock anything", async () => {
    const { dialog } = await openRun({ status: "TESTING" });

    expect(dialog).toHaveTextContent("already TESTING");
    expect(dialog).not.toHaveTextContent("no longer be added or removed");
  });

  it("runs the tests, shows the API's verdict and every count it returned, and reads the results back", async () => {
    runStudioTests.mockResolvedValue(PASSED_RUN);
    const { dialog, pane } = await openRun();
    // What the reads find afterwards: the change set is TESTING and the items carry their status.
    fetchStudioChangeSets.mockResolvedValue([changeSet({ status: "TESTING" })]);
    fetchStudioChangeSetItems.mockResolvedValue([
      item({ test_status: "PASSED" }), item({ id: "item_2", object_id: "metric:margin", test_status: "PASSED" }),
    ]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Run tests" }));

    await waitFor(() => expect(runStudioTests).toHaveBeenCalledWith("cs_1", undefined));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const result = await within(pane).findByRole("region", { name: "Test run" });
    expect(within(result).getByText("passed")).toBeInTheDocument();
    expect(result).toHaveTextContent("started 2026-09-02 10:00:00 UTC · completed 2026-09-02 10:00:01 UTC");
    for (const [key, value] of Object.entries(PASSED_RUN.evidence)) {
      const row = within(result).getByText(key).closest("div")!;
      expect(row).toHaveTextContent(`${key}${String(value)}`);
    }
    // Each item's own status is READ BACK, not inferred from the totals.
    await waitFor(() => expect(within(pane).getAllByText("test status: passed")).toHaveLength(2));
    // ...and the list, because the change set's own status moved.
    await waitFor(() => expect(fetchStudioChangeSets.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(await within(pane).findByText(/Items are locked/)).toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: "Add item" })).not.toBeInTheDocument();
    // The tests wrote an eval run; the section reads it.
    await waitFor(() => expect(fetchStudioEvalRun).toHaveBeenCalledWith("cs_1", expect.anything()));
  });

  it("does not leave an earlier action's notice standing over the result it has just produced", async () => {
    const added = item({ id: "item_9", object_id: "metric:net_revenue", operation: "CREATE" });
    addStudioChangeItem.mockResolvedValue(added);
    runStudioTests.mockResolvedValue(PASSED_RUN);
    const { pane } = await openDetail(["DataSteward"], { items: [] });
    fetchStudioChangeSetItems.mockResolvedValue([added]);
    fireEvent.click(within(pane).getByRole("button", { name: "Add item" }));
    const add = await dialogNamed("Add item");
    fireEvent.change(within(add).getByLabelText(/^Object id/), { target: { value: "metric:net_revenue" } });
    fireEvent.click(within(add).getByRole("button", { name: "Add item" }));
    await within(pane).findByText("Added METRIC metric:net_revenue (create).");

    fireEvent.click(within(pane).getByRole("button", { name: "Run tests" }));
    fireEvent.click(within(await dialogNamed("Run the tests?")).getByRole("button", { name: "Run tests" }));

    await within(pane).findByRole("region", { name: "Test run" });
    expect(within(pane).queryByText(/^Added /)).not.toBeInTheDocument();
  });

  it("shows a failed run as failed, and says the API gives totals and not the reason an item failed", async () => {
    runStudioTests.mockResolvedValue({
      ...PASSED_RUN, passed: false,
      evidence: { total_items: 2, passed_items: 1, failed_items: 1, eval_regression_checked: 0, eval_regression_failed: 0 },
    });
    const { dialog, pane } = await openRun();

    fireEvent.click(within(dialog).getByRole("button", { name: "Run tests" }));

    const result = await within(pane).findByRole("region", { name: "Test run" });
    expect(within(result).getByText("failed")).toBeInTheDocument();
    expect(result).toHaveTextContent("failed_items1");
    expect(result).toHaveTextContent("The API returns totals for a test run, not the reason an item failed.");
  });

  it("does not add the explanation to a run that passed", async () => {
    runStudioTests.mockResolvedValue(PASSED_RUN);
    const { dialog, pane } = await openRun();

    fireEvent.click(within(dialog).getByRole("button", { name: "Run tests" }));

    await within(pane).findByRole("region", { name: "Test run" });
    expect(within(pane).queryByText(/not the reason an item failed/)).not.toBeInTheDocument();
  });

  it("keeps the confirmation open on a refusal and shows the server's sentence", async () => {
    runStudioTests.mockRejectedValue(new ApiError(409, "tests can only be run on DRAFT or TESTING change sets"));
    const { dialog, pane } = await openRun();

    fireEvent.click(within(dialog).getByRole("button", { name: "Run tests" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^tests can only be run on DRAFT or TESTING change sets$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(pane).queryByRole("region", { name: "Test run" })).not.toBeInTheDocument();
    expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1);
  });

  it("admits one run at a time", async () => {
    let settle: (result: StudioTestResultRead) => void = () => undefined;
    runStudioTests.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const { dialog } = await openRun();

    fireEvent.click(within(dialog).getByRole("button", { name: "Run tests" }));
    const working = await within(dialog).findByRole("button", { name: "Working…" });
    fireEvent.click(working);

    expect(runStudioTests).toHaveBeenCalledTimes(1);
    settle(PASSED_RUN);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("cancels without running anything", async () => {
    const { dialog } = await openRun();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(runStudioTests).not.toHaveBeenCalled();
  });
});

/* ---------------------------------------------------------------------------
   7. Detect conflicts
--------------------------------------------------------------------------- */

describe("Detect conflicts", () => {
  async function openDetect(items: StudioChangeItemRead[] = [item()], cs: Partial<StudioChangeSetRead> = {}) {
    const opened = await openDetail(["DataSteward"], { items, cs });
    fireEvent.click(within(opened.pane).getByRole("button", { name: "Detect conflicts" }));
    return { ...opened, dialog: await dialogNamed("Detect conflicts") };
  }

  it("opens on the published state field", async () => {
    const { dialog } = await openDetect();

    expect(document.activeElement).toBe(within(dialog).getByLabelText(/^Published state/));
  });

  it("lists the key the API looks each item up by, and warns that an empty state manufactures conflicts for an UPDATE", async () => {
    const { dialog } = await openDetect([item(), item({ id: "item_2", object_type: "TOOL", object_id: "tool:margin", operation: "CREATE" })]);

    expect(within(dialog).getByText("METRIC:metric:revenue")).toBeInTheDocument();
    expect(within(dialog).getByText("TOOL:tool:margin")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Without a published snapshot for its key, an UPDATE item is reported as NOT_FOUND and a DELETE item as ALREADY_DELETED");
    expect(detectStudioConflicts).not.toHaveBeenCalled();
  });

  it("does not warn when every item is a CREATE", async () => {
    const { dialog } = await openDetect([item({ operation: "CREATE" })]);

    expect(dialog).not.toHaveTextContent("Without a published snapshot");
  });

  it("with no state supplied, sends none, and reports each conflict with both values and what it was compared with", async () => {
    detectStudioConflicts.mockResolvedValue([
      { object_type: "METRIC", object_id: "metric:revenue", field_name: "<exists>", change_set_value: "UPDATE", current_value: "NOT_FOUND" },
    ]);
    const { dialog, pane } = await openDetect();
    fetchStudioChangeSets.mockResolvedValue([changeSet({ conflict_status: "CONFLICTED" })]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    await waitFor(() => expect(detectStudioConflicts).toHaveBeenCalledWith("cs_1", null, undefined));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const result = await within(pane).findByRole("region", { name: "Conflicts" });
    expect(result).toHaveTextContent("Conflicts (1)");
    expect(result).toHaveTextContent("1 conflict found, compared with an empty published state (none was supplied).");
    expect(result).toHaveTextContent("METRIC · metric:revenue");
    expect(result).toHaveTextContent("field <exists>");
    expect(result).toHaveTextContent("change set: UPDATE");
    expect(result).toHaveTextContent("published: NOT_FOUND");
    // The recorded conflict status is re-read onto the list row.
    expect(await screen.findByText("conflicted")).toBeInTheDocument();
  });

  it("sends the published state the author supplied, and says how many snapshots it compared with", async () => {
    detectStudioConflicts.mockResolvedValue([]);
    const { dialog, pane } = await openDetect();
    const state = { "METRIC:metric:revenue": { grain: "day" }, "TOOL:tool:margin": { name: "margin" } };

    fireEvent.change(within(dialog).getByLabelText(/^Published state/), { target: { value: JSON.stringify(state) } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    await waitFor(() => expect(detectStudioConflicts).toHaveBeenCalledWith("cs_1", state, undefined));
    const result = await within(pane).findByRole("region", { name: "Conflicts" });
    expect(result).toHaveTextContent("Conflicts (0)");
    expect(result).toHaveTextContent("No conflicts found, compared with 2 published snapshots you supplied.");
  });

  it("shows a field-level conflict with the change set's value and the published one", async () => {
    detectStudioConflicts.mockResolvedValue([
      { object_type: "METRIC", object_id: "metric:revenue", field_name: "filter", change_set_value: { op: "not in" }, current_value: "status != 'void'" },
    ]);
    const { dialog, pane } = await openDetect();

    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    const result = await within(pane).findByRole("region", { name: "Conflicts" });
    expect(result).toHaveTextContent('change set: {"op":"not in"}');
    expect(result).toHaveTextContent("published: status != 'void'");
  });

  it.each([
    ["not valid JSON", "{oops", /^Published state is not valid JSON: /],
    ["a JSON array", "[1]", /^Published state must be a JSON object/],
  ])("reports a published state that is %s, and sends nothing", async (_case, text, message) => {
    const { dialog } = await openDetect();

    fireEvent.change(within(dialog).getByLabelText(/^Published state/), { target: { value: text } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
    expect(detectStudioConflicts).not.toHaveBeenCalled();
  });

  it("keeps the dialog open on a refusal, in the server's words, and shows no result", async () => {
    detectStudioConflicts.mockRejectedValue(new ApiError(422, "body.METRIC:metric:revenue: Input should be a valid dictionary"));
    const { dialog, pane } = await openDetect();

    fireEvent.change(within(dialog).getByLabelText(/^Published state/), { target: { value: '{"METRIC:metric:revenue": 1}' } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Detect conflicts" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /^body\.METRIC:metric:revenue: Input should be a valid dictionary$/,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(pane).queryByRole("region", { name: "Conflicts" })).not.toBeInTheDocument();
  });

  it("says so, rather than offering nothing to compare, for a change set with no items", async () => {
    const { dialog } = await openDetect([]);

    expect(dialog).toHaveTextContent("This change set has no items, so there is nothing to compare.");
  });

  it("is not offered once a change set is SUBMITTED", async () => {
    const { pane } = await openDetail(["DataSteward"], { cs: { status: "SUBMITTED" }, items: [item()] });

    expect(within(pane).queryByRole("button", { name: "Detect conflicts" })).not.toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   8. The eval run
--------------------------------------------------------------------------- */

const EVAL_RUN: StudioEvalRunRead = {
  id: "run_1", change_set_id: "cs_1", started_at: "2026-09-02T10:00:00Z", completed_at: "2026-09-02T10:00:02Z",
  passed: false, evidence: { checked: 2, failed: 1, failed_question_ids: ["q_2"] },
  results: [
    {
      eval_question_id: "q_1", object_type: "METRIC", object_id: "metric:revenue", label: "metric:net_revenue", passed: true,
      evidence: { object_type: "METRIC", object_id: "metric:revenue", label: "metric:net_revenue", failures: [] },
    },
    {
      eval_question_id: "q_2", object_type: "TOOL", object_id: "tool:margin", label: "tool:margin_by_region", passed: false,
      evidence: { object_type: "TOOL", object_id: "tool:margin", label: "tool:margin_by_region", failures: ["missing required tool field: allowed_roles"] },
    },
  ],
};

describe("The eval run on a change set", () => {
  it("does not ask for an eval run on a DRAFT, which cannot have one, and says so", async () => {
    const { pane } = await openDetail(["DataSteward"], { items: [item()] });

    const section = within(pane).getByRole("region", { name: "Eval run" });
    expect(section).toHaveTextContent("No tests have been run on this change set, so there is no eval run yet.");
    expect(fetchStudioEvalRun).not.toHaveBeenCalled();
  });

  it.each([...WRITE_ROLES, ...READ_ONLY_ROLES])("shows %s the latest run, its verdict and each question's reasons", async (role) => {
    fetchStudioEvalRun.mockResolvedValue(EVAL_RUN);
    const { pane } = await openDetail([role], { cs: { status: "TESTING" }, items: [item({ test_status: "PASSED" })] });

    const section = await within(pane).findByRole("region", { name: "Eval run" });
    // the verdict pill: "failed" is also an evidence key, and that is not what is being asked
    expect(await within(section).findByText("failed", { selector: ".pill" })).toBeInTheDocument();
    expect(fetchStudioEvalRun).toHaveBeenCalledWith("cs_1", expect.anything());
    expect(section).toHaveTextContent("started 2026-09-02 10:00:00 UTC · completed 2026-09-02 10:00:02 UTC");
    expect(section).toHaveTextContent("checked2");
    expect(section).toHaveTextContent("failed_question_ids" + JSON.stringify(["q_2"]));
    expect(section).toHaveTextContent("tool:margin_by_region");
    expect(section).toHaveTextContent("missing required tool field: allowed_roles");
    // each question's own verdict is on its row: one passed, one failed
    const rows = within(section).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("METRIC · passed");
    expect(rows[1]).toHaveTextContent("TOOL · failed");
  });

  it("says a passed run with no question checked nothing", async () => {
    fetchStudioEvalRun.mockResolvedValue({ ...EVAL_RUN, passed: true, evidence: { checked: 0, failed: 0, failed_question_ids: [] }, results: [] });
    const { pane } = await openDetail(["Viewer"], { cs: { status: "TESTING" } });

    const section = await within(pane).findByRole("region", { name: "Eval run" });
    expect(await within(section).findByText(/No mined question covers the objects this change set touches, so the gate had nothing to check\./)).toBeInTheDocument();
  });

  it("shows the server's own sentence, not an error, when a tested change set has no run recorded", async () => {
    const { pane } = await openDetail(["Viewer"], { cs: { status: "TESTING" } });

    const section = within(pane).getByRole("region", { name: "Eval run" });
    expect(await within(section).findByText(NO_EVAL_RUN)).toBeInTheDocument();
    expect(within(section).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a real failure to load as one, with a retry that asks again", async () => {
    fetchStudioEvalRun.mockRejectedValueOnce(new ApiError(503, "database unavailable")).mockResolvedValueOnce(EVAL_RUN);
    const { pane } = await openDetail(["Viewer"], { cs: { status: "TESTING" } });

    const alert = await within(pane).findByText("database unavailable");
    expect(alert.closest('[role="alert"]')).not.toBeNull();
    fireEvent.click(within(pane).getByRole("button", { name: "Try again" }));

    expect(await within(pane).findByText("tool:margin_by_region")).toBeInTheDocument();
    expect(fetchStudioEvalRun).toHaveBeenCalledTimes(2);
  });
});

/* ---------------------------------------------------------------------------
   9. Eval questions and mining
--------------------------------------------------------------------------- */

const QUESTIONS: StudioEvalQuestionRead[] = [
  {
    id: "q_1", organization_id: ORG, object_type: "METRIC", object_id: "metric:revenue", evidence_source: "BI",
    evidence_edge_id: "edge_1", label: "metric:net_revenue", mined_at: "2026-09-02T09:00:00Z", created_at: T, updated_at: T,
  },
  {
    id: "q_2", organization_id: ORG, object_type: "TOOL", object_id: "tool:margin", evidence_source: "CONSUMPTION",
    evidence_edge_id: "edge_2", label: "tool:margin_by_region", mined_at: "2026-09-01T09:00:00Z", created_at: T, updated_at: T,
  },
];

describe("Eval questions and mining", () => {
  async function openQuestions(role: string) {
    sessionMe = asRoles(role);
    render(<StudioChangeSetsScreen />);
    const trigger = await screen.findByRole("button", { name: "Eval questions" });
    trigger.focus(); // a real click focuses its button; `fireEvent.click` does not
    fireEvent.click(trigger);
    return await dialogNamed("Eval questions");
  }

  it.each(READ_ONLY_ROLES)("lists the mined questions for %s, with no way to mine and the roles that can", async (role) => {
    fetchStudioEvalQuestions.mockResolvedValue(QUESTIONS);
    const dialog = await openQuestions(role);

    const list = await within(dialog).findByRole("list", { name: "Eval questions" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(list).toHaveTextContent("metric:net_revenue");
    expect(list).toHaveTextContent("METRIC · bi");
    expect(list).toHaveTextContent("tool:margin · mined 2026-09-01");
    expect(within(dialog).queryByRole("button", { name: "Mine eval questions" })).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("Mining questions needs DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin.");
    expect(mineStudioEvalQuestions).not.toHaveBeenCalled();
  });

  it.each(WRITE_ROLES)("offers %s the mining, and says what it does and does not store", async (role) => {
    const dialog = await openQuestions(role);

    expect(within(dialog).getByRole("button", { name: "Mine eval questions" })).toBeEnabled();
    expect(dialog).toHaveTextContent("It is safe to repeat: an object already mined is left alone.");
    expect(dialog).toHaveTextContent("Only which object was used is stored, never a query or its result.");
    expect(mineStudioEvalQuestions).not.toHaveBeenCalled(); // nothing on open
  });

  it("asks for the corpus once, newest 200, and filters by object type on the server", async () => {
    fetchStudioEvalQuestions.mockResolvedValue(QUESTIONS);
    const dialog = await openQuestions("Viewer");
    await within(dialog).findByRole("list", { name: "Eval questions" });
    expect(fetchStudioEvalQuestions).toHaveBeenCalledTimes(1);
    expect(fetchStudioEvalQuestions).toHaveBeenLastCalledWith({ objectType: null, limit: 200 }, expect.anything());

    fireEvent.change(within(dialog).getByLabelText(/^Object type/), { target: { value: "TOOL" } });

    await waitFor(() => expect(fetchStudioEvalQuestions).toHaveBeenLastCalledWith({ objectType: "TOOL", limit: 200 }, expect.anything()));
  });

  it("says nothing has been mined rather than showing an empty list", async () => {
    const dialog = await openQuestions("Viewer");

    expect(await within(dialog).findByText("No eval questions")).toBeInTheDocument();
    expect(within(dialog).getByText("None have been mined yet.")).toBeInTheDocument();
  });

  it("shows a failure to load as one, with a retry", async () => {
    fetchStudioEvalQuestions.mockRejectedValueOnce(new ApiError(503, "database unavailable")).mockResolvedValueOnce(QUESTIONS);
    const dialog = await openQuestions("Viewer");

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Eval questions could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    expect(await within(dialog).findByRole("list", { name: "Eval questions" })).toBeInTheDocument();
  });

  it("mines, shows every count the API returned, and re-reads the corpus", async () => {
    fetchStudioEvalQuestions.mockResolvedValueOnce([]).mockResolvedValueOnce(QUESTIONS);
    mineStudioEvalQuestions.mockResolvedValue({
      consumption_edges_scanned: 12, bi_edges_scanned: 7, questions_created: 2, questions_already_mined: 1, truncated: false,
    });
    const dialog = await openQuestions("DataSteward");
    await within(dialog).findByText("No eval questions");

    fireEvent.click(within(dialog).getByRole("button", { name: "Mine eval questions" }));

    await waitFor(() => expect(mineStudioEvalQuestions).toHaveBeenCalledTimes(1));
    expect(await within(dialog).findByText(/Scanned 12 consumption and 7 BI lineage edges: created 2 questions, 1 already mined\./)).toBeInTheDocument();
    expect(within(dialog).queryByText(/scan reached its limit/)).not.toBeInTheDocument();
    expect(await within(dialog).findByRole("list", { name: "Eval questions" })).toBeInTheDocument();
    expect(fetchStudioEvalQuestions).toHaveBeenCalledTimes(2);
  });

  it("says a truncated scan did not look at older usage", async () => {
    mineStudioEvalQuestions.mockResolvedValue({
      consumption_edges_scanned: 500, bi_edges_scanned: 3, questions_created: 1, questions_already_mined: 0, truncated: true,
    });
    const dialog = await openQuestions("DataSteward");

    fireEvent.click(await within(dialog).findByRole("button", { name: "Mine eval questions" }));

    expect(await within(dialog).findByText(/created 1 question, 0 already mined\./)).toBeInTheDocument();
    expect(dialog).toHaveTextContent("The scan reached its limit, so older usage was not looked at");
  });

  it("shows a refusal in the server's words and re-reads nothing", async () => {
    mineStudioEvalQuestions.mockRejectedValue(new ApiError(403, "requires one of: DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin"));
    const dialog = await openQuestions("DataSteward");
    await within(dialog).findByText("No eval questions");

    fireEvent.click(within(dialog).getByRole("button", { name: "Mine eval questions" }));

    expect(await within(dialog).findByText(/^requires one of: DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin$/)).toBeInTheDocument();
    expect(fetchStudioEvalQuestions).toHaveBeenCalledTimes(1);
    expect(within(dialog).queryByText(/Scanned/)).not.toBeInTheDocument();
  });

  it("admits one mining pass at a time", async () => {
    let settle: (result: StudioEvalMiningResult) => void = () => undefined;
    mineStudioEvalQuestions.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openQuestions("DataSteward");

    fireEvent.click(await within(dialog).findByRole("button", { name: "Mine eval questions" }));
    const working = await within(dialog).findByRole("button", { name: "Mining…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);

    expect(mineStudioEvalQuestions).toHaveBeenCalledTimes(1);
    settle({ consumption_edges_scanned: 0, bi_edges_scanned: 0, questions_created: 0, questions_already_mined: 0, truncated: false });
    await within(dialog).findByText(/Scanned 0 consumption/);
  });

  it("closes, and gives focus back to the button that opened it", async () => {
    const dialog = await openQuestions("Viewer");

    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Eval questions" }));
  });
});

/* ---------------------------------------------------------------------------
   10. Checking an item that is already in the change set
--------------------------------------------------------------------------- */

const TOOL_ITEM = item({
  id: "item_t", object_type: "TOOL", object_id: "tool:margin", operation: "CREATE",
  after_snapshot: {
    name: "margin", sql_template: "SELECT * FROM m WHERE r = :r", allowed_roles: ["Analyst"],
    parameters: [{ name: "r", parameter_type: "STRING" }],
  },
});
const CP_ITEM = item({
  id: "item_c", object_type: "CONTEXT_PRODUCT", object_id: "cp_customer360", operation: "CREATE",
  after_snapshot: { product_key: "cp_customer360", name: "Customer 360" },
});

describe("Checking an item already in the change set", () => {
  it.each([...WRITE_ROLES, ...READ_ONLY_ROLES])(
    "lets %s check a TOOL item's contract, because the check is stateless and read-only",
    async (role) => {
      validateStudioParameterContract.mockResolvedValue({ valid: false, errors: ["undeclared placeholders: r"], definitions: [] });
      const { pane } = await openDetail([role], { items: [TOOL_ITEM] });

      fireEvent.click(within(pane).getByRole("button", { name: "Check contract for tool:margin" }));

      await waitFor(() =>
        expect(validateStudioParameterContract).toHaveBeenCalledWith(
          { sql_template: "SELECT * FROM m WHERE r = :r", parameters: [{ name: "r", parameter_type: "STRING" }] },
          undefined,
        ),
      );
      expect(await within(pane).findByText("not valid")).toBeInTheDocument();
      expect(within(pane).getByText("undeclared placeholders: r")).toBeInTheDocument();
    },
  );

  it("checks a CONTEXT_PRODUCT item's definition with its operation, id and after snapshot", async () => {
    validateStudioContextProduct.mockResolvedValue({ valid: true, errors: [] });
    const { pane } = await openDetail(["Viewer"], { items: [CP_ITEM] });

    fireEvent.click(within(pane).getByRole("button", { name: "Check definition for cp_customer360" }));

    await waitFor(() =>
      expect(validateStudioContextProduct).toHaveBeenCalledWith(
        { operation: "CREATE", object_id: "cp_customer360", snapshot: { product_key: "cp_customer360", name: "Customer 360" } },
        undefined,
      ),
    );
    expect(await within(pane).findByText("valid")).toBeInTheDocument();
  });

  it("offers no Check on a METRIC, a TERM or a TOOL being deleted", async () => {
    const { pane } = await openDetail(["DataSteward"], {
      items: [
        item({ id: "m", object_id: "metric:a" }),
        item({ id: "t", object_type: "TERM", object_id: "term:b" }),
        item({ id: "d", object_type: "TOOL", object_id: "tool:c", operation: "DELETE" }),
      ],
    });

    expect(within(pane).queryByRole("button", { name: /^Check/ })).not.toBeInTheDocument();
  });

  it("says a TOOL item with no sql_template cannot be checked, without asking the API", async () => {
    const { pane } = await openDetail(["Viewer"], { items: [item({ ...TOOL_ITEM, after_snapshot: { name: "margin" } })] });

    fireEvent.click(within(pane).getByRole("button", { name: "Check contract for tool:margin" }));

    expect(await within(pane).findByText("The after snapshot has no sql_template to check.")).toBeInTheDocument();
    expect(validateStudioParameterContract).not.toHaveBeenCalled();
  });

  it("shows a validator's refusal in the server's words", async () => {
    validateStudioContextProduct.mockRejectedValue(new ApiError(403, "requires one of: Analyst, Auditor"));
    const { pane } = await openDetail(["Viewer"], { items: [CP_ITEM] });

    fireEvent.click(within(pane).getByRole("button", { name: "Check definition for cp_customer360" }));

    expect(await within(pane).findByText(/^requires one of: Analyst, Auditor$/)).toBeInTheDocument();
  });

  it("checks each item on its own", async () => {
    validateStudioParameterContract.mockResolvedValue({ valid: true, errors: [], definitions: [] });
    validateStudioContextProduct.mockResolvedValue({ valid: false, errors: ["project_id is not a valid UUID: 'x'"] });
    const { pane } = await openDetail(["Viewer"], { items: [TOOL_ITEM, CP_ITEM] });

    fireEvent.click(within(pane).getByRole("button", { name: "Check definition for cp_customer360" }));

    expect(await within(pane).findByText("project_id is not a valid UUID: 'x'")).toBeInTheDocument();
    expect(within(pane).queryByText("valid")).not.toBeInTheDocument();
    expect(validateStudioParameterContract).not.toHaveBeenCalled();
  });
});

/* ---------------------------------------------------------------------------
   The reads behind the detail pane and the dialogs are gated on their own too.

   The screen never renders them for a session outside the read list (the list is held or
   skipped first), so through the screen these branches cannot be reached. They are a second
   line, and a second line that nothing exercises is a line nobody knows is still there:
   each is rendered directly here, because a component reused somewhere the screen's gate
   does not stand in front of it must still not send a request the session is refused.
--------------------------------------------------------------------------- */

describe("The reads and checks behind the pane gate themselves", () => {
  const testing = changeSet({ status: "TESTING" });

  it("holds the eval run while identity is in flight, and never asks for a session outside the list", async () => {
    sessionState = "connecting";
    const view = render(<EvalRunSection changeSet={testing} refreshKey={0} />);

    expect(await screen.findByText("Loading eval run…")).toBeInTheDocument();
    expect(fetchStudioEvalRun).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("Operations");
    view.rerender(<EvalRunSection changeSet={testing} refreshKey={0} />);

    expect(await screen.findByText(/Not applicable to your roles: only sessions holding Analyst, Auditor, DataSteward/)).toBeInTheDocument();
    expect(fetchStudioEvalRun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("asks for the eval run once identity says the session may", async () => {
    sessionState = "connecting";
    const view = render(<EvalRunSection changeSet={testing} refreshKey={0} />);
    expect(fetchStudioEvalRun).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("Viewer");
    view.rerender(<EvalRunSection changeSet={testing} refreshKey={0} />);

    await waitFor(() => expect(fetchStudioEvalRun).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(NO_EVAL_RUN)).toBeInTheDocument();
  });

  it("holds the eval questions while identity is in flight, and never asks for a session outside the list", async () => {
    sessionState = "connecting";
    const view = render(<EvalQuestionsDialog onClose={() => undefined} />);

    expect(await screen.findByText("Loading eval questions…")).toBeInTheDocument();
    expect(fetchStudioEvalQuestions).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Mine eval questions" })).not.toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("AgentDeveloper");
    view.rerender(<EvalQuestionsDialog onClose={() => undefined} />);

    expect(await screen.findByText(/Not applicable to your roles: only sessions holding Analyst, Auditor, DataSteward/)).toBeInTheDocument();
    expect(fetchStudioEvalQuestions).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Mine eval questions" })).not.toBeInTheDocument();
    // Neither the filter nor a list is offered for a corpus this session may not read.
    expect(screen.queryByLabelText(/^Object type/)).not.toBeInTheDocument();
  });

  it("offers mining to nobody while roles are unknown, and says nothing about roles", async () => {
    sessionState = "disconnected";
    sessionMe = null;
    render(<EvalQuestionsDialog onClose={() => undefined} />);

    await screen.findByText("No eval questions");
    expect(screen.queryByRole("button", { name: "Mine eval questions" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Mining questions needs/)).not.toBeInTheDocument();
  });

  it("offers no Check to a session outside the read list, and holds it while identity is in flight", () => {
    const tool = item({ object_type: "TOOL", operation: "CREATE", after_snapshot: { sql_template: "SELECT 1" } });
    sessionState = "connecting";
    const view = render(<ItemCheck item={tool} />);
    expect(screen.queryByRole("button", { name: /^Check/ })).not.toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("Operations");
    view.rerender(<ItemCheck item={tool} />);
    expect(screen.queryByRole("button", { name: /^Check/ })).not.toBeInTheDocument();

    sessionMe = asRoles("Viewer");
    view.rerender(<ItemCheck item={tool} />);
    expect(screen.getByRole("button", { name: "Check contract for metric:revenue" })).toBeEnabled();
    expect(validateStudioParameterContract).not.toHaveBeenCalled();
  });
});

/* ---------------------------------------------------------------------------
   11. Submit is a write control too
--------------------------------------------------------------------------- */

describe("Submit for review", () => {
  it("re-reads the list after a submission, and a Viewer is not offered it to be refused", async () => {
    submitStudioChangeSet.mockResolvedValue(changeSet({ status: "SUBMITTED" }));
    const { pane } = await openDetail(["SemanticAdmin"], { cs: { status: "TESTING" }, items: [item({ test_status: "PASSED" })] });
    fetchStudioChangeSets.mockResolvedValue([changeSet({ status: "SUBMITTED" })]);

    fireEvent.click(within(pane).getByRole("button", { name: "Submit for review" }));

    await waitFor(() => expect(submitStudioChangeSet).toHaveBeenCalledWith("cs_1", undefined));
    await waitFor(() => expect(fetchStudioChangeSets).toHaveBeenCalledTimes(2));
    expect(await within(pane).findByText("submitted — nothing left to submit")).toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: "Run tests" })).not.toBeInTheDocument();
  });

  it("shows the API's test-gate refusal verbatim", async () => {
    submitStudioChangeSet.mockRejectedValue(new ApiError(409, "1 item(s) have not passed testing; 1 mined eval question(s) regressed: ['q_2']"));
    const { pane } = await openDetail(["DataSteward"], { items: [item()] });

    fireEvent.click(within(pane).getByRole("button", { name: "Submit for review" }));

    expect(await within(pane).findByText("1 item(s) have not passed testing; 1 mined eval question(s) regressed: ['q_2']")).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   12. Accessibility
--------------------------------------------------------------------------- */

describe("Studio authoring: accessibility", () => {
  it("has no WCAG A/AA violation in a DRAFT's detail with every authoring control, results and checks showing", async () => {
    fetchStudioEvalRun.mockResolvedValue(EVAL_RUN);
    validateStudioParameterContract.mockResolvedValue({ valid: true, errors: [], definitions: [{ name: "r" }], sample_rendered_sql: "SELECT 1" });
    detectStudioConflicts.mockResolvedValue([
      { object_type: "METRIC", object_id: "metric:revenue", field_name: "<exists>", change_set_value: "UPDATE", current_value: "NOT_FOUND" },
    ]);
    runStudioTests.mockResolvedValue(PASSED_RUN);
    const { view, pane } = await openDetail(["DataSteward"], { items: [item(), TOOL_ITEM, CP_ITEM] });

    fireEvent.click(within(pane).getByRole("button", { name: "Check contract for tool:margin" }));
    await within(pane).findByText("Sample render");
    fireEvent.click(within(pane).getByRole("button", { name: "Detect conflicts" }));
    fireEvent.click(within(await dialogNamed("Detect conflicts")).getByRole("button", { name: "Detect conflicts" }));
    await within(pane).findByRole("region", { name: "Conflicts" });
    fireEvent.click(within(pane).getByRole("button", { name: "Run tests" }));
    fireEvent.click(within(await dialogNamed("Run the tests?")).getByRole("button", { name: "Run tests" }));
    await within(pane).findByRole("region", { name: "Test run" });

    await expectNoAxeViolations(view.container);
    expect(
      unnamedFocusableElements(view.container).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no WCAG A/AA violation in the eval run, and names every control in it", async () => {
    fetchStudioEvalRun.mockResolvedValue(EVAL_RUN);
    const { view, pane } = await openDetail(["Viewer"], { cs: { status: "TESTING" }, items: [item({ test_status: "FAILED" })] });
    await within(pane).findByText("tool:margin_by_region");

    await expectNoAxeViolations(view.container);
  });

  it.each([
    ["New change set", async () => {
      fireEvent.click(await screen.findByRole("button", { name: "New change set" }));
    }],
    ["Eval questions", async () => {
      fetchStudioEvalQuestions.mockResolvedValue(QUESTIONS);
      fireEvent.click(await screen.findByRole("button", { name: "Eval questions" }));
      await screen.findByRole("list", { name: "Eval questions" });
    }],
  ])("has no WCAG A/AA violation with the %s dialog open", async (name, open) => {
    sessionMe = asRoles("DataSteward");
    render(<StudioChangeSetsScreen />);
    await open();
    await dialogNamed(name);

    await expectNoAxeViolations(document.body);
  });

  it.each([
    ["Add item", "Add item"],
    ["Detect conflicts", "Detect conflicts"],
    ["Run tests", "Run the tests?"],
  ])("has no WCAG A/AA violation with the %s dialog open", async (button, dialogName) => {
    const { pane } = await openDetail(["DataSteward"], { items: [item(), TOOL_ITEM] });
    fireEvent.click(within(pane).getByRole("button", { name: button }));
    await dialogNamed(dialogName);

    await expectNoAxeViolations(document.body);
  });

  it("has no WCAG A/AA violation with the Add item dialog showing a check's errors, and with the removal confirmation open", async () => {
    validateStudioParameterContract.mockResolvedValue({ valid: false, errors: ["undeclared placeholders: r"], definitions: [] });
    const { pane } = await openDetail(["DataSteward"], { items: [item()] });
    fireEvent.click(within(pane).getByRole("button", { name: "Add item" }));
    const dialog = await dialogNamed("Add item");
    fireEvent.change(within(dialog).getByLabelText(/^Object type/), { target: { value: "TOOL" } });
    fireEvent.change(within(dialog).getByLabelText(/^After snapshot/), { target: { value: '{"sql_template": "SELECT :r"}' } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check contract" }));
    await within(dialog).findByText("undeclared placeholders: r");
    await expectNoAxeViolations(document.body);

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(within(pane).getByRole("button", { name: "Remove METRIC metric:revenue" }));
    await dialogNamed("Remove METRIC metric:revenue?");
    await expectNoAxeViolations(document.body);
  });
});
