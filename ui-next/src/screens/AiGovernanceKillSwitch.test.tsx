import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { KillSwitchEngageRequest, KillSwitchReleaseRequest, KillSwitchStateRead, MeRead } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { KillSwitchPanel } from "./AiGovernanceKillSwitch";

/* ---------------------------------------------------------------------------
   The organization kill switch panel (MG-2, R11-AUD08).

   The properties, and why each was a real way to get a control this dangerous
   wrong:

     1. EVERY ROLE THE READ ADMITS SEES THE STATE; ONLY PLATFORMADMIN IS OFFERED
        A CHANGE. And a session outside the read's roles is not asked at all --
        it gets "not applicable", not the 403 a doomed request would earn.
     2. NOTHING FIRES ON THE CLICK. Engage and Release each open a confirmation
        that says what it does ("stops model use for the whole organization") and
        will not confirm without a reason; the endpoint is not called until it is
        confirmed.
     3. A REFUSAL IS AN ANSWER, IN THE SERVER'S WORDS. The dialog stays open and
        shows the sentence the API sent -- including the 422 for a too-short
        reason and the 409 "kill switch is not currently engaged" -- rather than
        closing as though the organization had been stopped.
     4. THE SERVER IS THE AUTHORITY AFTER A WRITE. The state is re-read, never
        edited in place; a 409 re-reads too, so the panel stops offering a Release
        that no longer applies.
     5. A CONTROL THAT STOPS THE ORGANIZATION'S AI IS NEVER OFFERED ON A GUESS.
        While `/v1/me` is in flight the read is HELD (`readDecision`: a session that
        turns out not to be admitted would take a 403 for it) and the controls are
        not offered (fail closed). If identity never answers, the read is made.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchModelKillSwitchState = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<KillSwitchStateRead[]>>();
const engageModelKillSwitch =
  vi.fn<(organizationId: string, body: KillSwitchEngageRequest, signal?: AbortSignal) => Promise<KillSwitchStateRead>>();
const releaseModelKillSwitch =
  vi.fn<(organizationId: string, body: KillSwitchReleaseRequest, signal?: AbortSignal) => Promise<KillSwitchStateRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchModelKillSwitchState: (organizationId: string, signal?: AbortSignal) =>
      fetchModelKillSwitchState(organizationId, signal),
    engageModelKillSwitch: (organizationId: string, body: KillSwitchEngageRequest, signal?: AbortSignal) =>
      engageModelKillSwitch(organizationId, body, signal),
    releaseModelKillSwitch: (organizationId: string, body: KillSwitchReleaseRequest, signal?: AbortSignal) =>
      releaseModelKillSwitch(organizationId, body, signal),
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

/** The row exactly as `GET .../kill-switch` shapes it (`KillSwitchStateRead`). */
const row = (overrides: Partial<KillSwitchStateRead> = {}): KillSwitchStateRead => ({
  id: "ks_org", organization_id: ORG, route_key: "*", scope: "ORGANIZATION", engaged: false,
  reason: null, engaged_by: null, engaged_at: null, released_by: null, released_at: null,
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
  ...overrides,
});
const ENGAGED = row({
  engaged: true, reason: "provider incident 4471", engaged_by: "pat.admin", engaged_at: "2026-09-19T08:30:12Z",
});
const RELEASED = row({
  engaged: false, reason: "provider incident 4471", engaged_by: "pat.admin", engaged_at: "2026-09-19T08:30:12Z",
  released_by: "sam.admin", released_at: "2026-09-19T09:10:44Z",
});
const STOPPED_ROUTE = row({
  id: "ks_route", route_key: "bank-sql-primary", scope: "ROUTE", engaged: true,
  reason: "adapter misbehaving", engaged_by: "pat.admin", engaged_at: "2026-09-19T07:00:00Z",
});
const CLEAR_ROUTE = row({ id: "ks_route2", route_key: "bank-sql-secondary", scope: "ROUTE", engaged: false });

const NOT_ENGAGED = "Not engaged. This switch is not blocking model use.";
const STOPPED = "Model use is stopped for the whole organization.";
const OPERATOR_ONLY = "Only a PlatformAdmin can engage or release the kill switch.";

const engageButton = () => screen.queryByRole("button", { name: "Engage kill switch" });
const releaseButton = () => screen.queryByRole("button", { name: "Release kill switch" });

beforeEach(() => {
  fetchModelKillSwitchState.mockReset();
  fetchModelKillSwitchState.mockResolvedValue([]);
  engageModelKillSwitch.mockReset();
  releaseModelKillSwitch.mockReset();
  sessionMe = null;
  sessionState = "connected";
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("KillSwitchPanel: who sees what", () => {
  it.each(["AgentDeveloper", "Auditor", "DataSteward", "Reviewer", "Viewer"])(
    "shows the state to %s and offers no way to change it",
    async (role) => {
      sessionMe = asRoles(role);
      render(<KillSwitchPanel organizationId={ORG} />);

      expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
      expect(fetchModelKillSwitchState).toHaveBeenCalledWith(ORG, expect.any(AbortSignal));
      expect(screen.getByText("not engaged")).toBeInTheDocument();
      expect(engageButton()).not.toBeInTheDocument();
      expect(releaseButton()).not.toBeInTheDocument();
      // Told why, rather than left to wonder where the control is.
      expect(screen.getByText(OPERATOR_ONLY)).toBeInTheDocument();
    },
  );

  it("shows the state to a PlatformAdmin and offers Engage", async () => {
    sessionMe = asRoles("PlatformAdmin");
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
    expect(engageButton()).toBeEnabled();
    expect(releaseButton()).not.toBeInTheDocument();
    expect(screen.queryByText(OPERATOR_ONLY)).not.toBeInTheDocument();
  });

  it.each(["Analyst", "ToolDeveloper", "MetadataAdmin", "SemanticAdmin", "Operations", "OrganizationAdmin"])(
    "asks for nothing as %s, and renders not applicable rather than an error",
    async (role) => {
      sessionMe = asRoles(role);
      render(<KillSwitchPanel organizationId={ORG} />);

      expect(
        await screen.findByText(/Not applicable to your roles: only sessions holding AgentDeveloper, Auditor, DataSteward, PlatformAdmin, Reviewer or Viewer can see/),
      ).toBeInTheDocument();
      expect(fetchModelKillSwitchState).not.toHaveBeenCalled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByText("not engaged")).not.toBeInTheDocument();
      expect(engageButton()).not.toBeInTheDocument();
      expect(releaseButton()).not.toBeInTheDocument();
      expect(screen.queryByText(OPERATOR_ONLY)).not.toBeInTheDocument();
    },
  );

  it("holds the read while identity is in flight, and offers no control until it has", async () => {
    // `/v1/me` has not answered: the session is "connecting" and `me` is null. Unknown, not
    // "no roles" -- so nothing is asked for yet, and nothing that stops the AI is offered.
    sessionState = "connecting";
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(/Loading kill switch state/)).toBeInTheDocument();
    expect(fetchModelKillSwitchState).not.toHaveBeenCalled();
    expect(engageButton()).not.toBeInTheDocument();
    expect(releaseButton()).not.toBeInTheDocument();
    // ... and it does not tell a PlatformAdmin-to-be that they cannot, or that it does not apply.
    expect(screen.queryByText(OPERATOR_ONLY)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();
  });

  it("never sends the read when identity then says the session may not read it", async () => {
    // The read would have been refused with a 403 on every load (found for a session outside
    // the list); held while identity was in flight, it is simply never made.
    sessionState = "connecting";
    const view = render(<KillSwitchPanel organizationId={ORG} />);
    expect(await screen.findByText(/Loading kill switch state/)).toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("Analyst");
    view.rerender(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchModelKillSwitchState).not.toHaveBeenCalled();
  });

  it("sends the read once identity says the session may, and offers the controls it is admitted to", async () => {
    sessionState = "connecting";
    const view = render(<KillSwitchPanel organizationId={ORG} />);
    expect(fetchModelKillSwitchState).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("PlatformAdmin");
    view.rerender(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
    expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(1);
    expect(engageButton()).toBeInTheDocument();
  });

  it("still reads when identity will not answer: the server stays the authority", async () => {
    // `/v1/me` failed: `me` is null and the state is not "connecting". Holding the read would
    // leave it loading forever, so it is sent; the server decides.
    sessionState = "disconnected";
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
    expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(1);
    expect(engageButton()).not.toBeInTheDocument();
    expect(releaseButton()).not.toBeInTheDocument();
  });
});

describe("KillSwitchPanel: what the state says", () => {
  it("says an organization that never used the switch has never used it", async () => {
    sessionMe = asRoles("Viewer");
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText("This switch has never been used for this organization.")).toBeInTheDocument();
  });

  it("says who engaged it, when, and why, and offers Release to a PlatformAdmin", async () => {
    sessionMe = asRoles("PlatformAdmin");
    fetchModelKillSwitchState.mockResolvedValue([ENGAGED]);
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(STOPPED)).toBeInTheDocument();
    expect(screen.getByText("engaged")).toBeInTheDocument();
    const detail = screen.getByText(/Engaged by/);
    expect(detail).toHaveTextContent("Engaged by pat.admin at 2026-09-19 08:30 UTC.");
    expect(detail).toHaveTextContent("Reason: “provider incident 4471”");
    expect(releaseButton()).toBeEnabled();
    expect(engageButton()).not.toBeInTheDocument();
  });

  it("shows an engaged switch to a Viewer, with no way to release it", async () => {
    sessionMe = asRoles("Viewer");
    fetchModelKillSwitchState.mockResolvedValue([ENGAGED]);
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(STOPPED)).toBeInTheDocument();
    expect(releaseButton()).not.toBeInTheDocument();
    expect(screen.getByText(OPERATOR_ONLY)).toBeInTheDocument();
  });

  it("says a released switch was released, by whom, after whom", async () => {
    sessionMe = asRoles("Auditor");
    fetchModelKillSwitchState.mockResolvedValue([RELEASED]);
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
    const detail = screen.getByText(/Last engaged by/);
    expect(detail).toHaveTextContent(
      "Last engaged by pat.admin at 2026-09-19 08:30 UTC (“provider incident 4471”); released by sam.admin at 2026-09-19 09:10 UTC.",
    );
  });

  it("does not let an organization-wide 'not engaged' hide a route that is stopped", async () => {
    sessionMe = asRoles("Reviewer");
    fetchModelKillSwitchState.mockResolvedValue([RELEASED, STOPPED_ROUTE, CLEAR_ROUTE]);
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText("Not engaged for the whole organization.")).toBeInTheDocument();
    // Never the reassuring sentence while a route is halted.
    expect(screen.queryByText(NOT_ENGAGED)).not.toBeInTheDocument();
    const list = screen.getByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(list).toHaveTextContent("bank-sql-primary");
    expect(list).toHaveTextContent("engaged by pat.admin at 2026-09-19 07:00 UTC: “adapter misbehaving”");
    expect(list).not.toHaveTextContent("bank-sql-secondary");
    expect(screen.getByText(/read-only here/)).toBeInTheDocument();
  });

  it("lists a stopped route beside a switch that has never been used at the organization level", async () => {
    sessionMe = asRoles("Reviewer");
    fetchModelKillSwitchState.mockResolvedValue([STOPPED_ROUTE]);
    render(<KillSwitchPanel organizationId={ORG} />);

    expect(await screen.findByText("Not engaged for the whole organization.")).toBeInTheDocument();
    expect(screen.getByRole("list")).toHaveTextContent("bank-sql-primary");
  });

  it("says so, and still offers Engage but never Release, when the state could not be read", async () => {
    // The direction that stops AI is safe to offer blind (the server accepts it); the one that starts it again is not.
    sessionMe = asRoles("PlatformAdmin");
    fetchModelKillSwitchState.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    fetchModelKillSwitchState.mockResolvedValueOnce([ENGAGED]);
    render(<KillSwitchPanel organizationId={ORG} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Kill switch state could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    expect(engageButton()).toBeEnabled();
    expect(releaseButton()).not.toBeInTheDocument();

    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(STOPPED)).toBeInTheDocument();
    expect(releaseButton()).toBeEnabled();
  });
});

describe("KillSwitchPanel: engaging", () => {
  async function openEngage() {
    sessionMe = asRoles("PlatformAdmin");
    render(<KillSwitchPanel organizationId={ORG} />);
    await screen.findByText(NOT_ENGAGED);
    const trigger = screen.getByRole("button", { name: "Engage kill switch" });
    trigger.focus();
    fireEvent.click(trigger);
    return await screen.findByRole("dialog", { name: "Engage the organization kill switch?" });
  }

  it("states the effect and asks for a reason, and sends nothing on the click", async () => {
    const dialog = await openEngage();

    expect(dialog).toHaveTextContent("This stops model use for the whole organization");
    expect(dialog).toHaveTextContent("until a PlatformAdmin releases the switch");
    expect(dialog).toHaveTextContent("recorded in the audit ledger with your reason");
    const confirm = within(dialog).getByRole("button", { name: "Engage kill switch" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "   " } });
    expect(confirm).toBeDisabled();
    expect(engageModelKillSwitch).not.toHaveBeenCalled();
  });

  it("engages with the reason the operator wrote, then re-reads rather than editing the panel in place", async () => {
    engageModelKillSwitch.mockResolvedValue(ENGAGED);
    fetchModelKillSwitchState.mockResolvedValueOnce([]).mockResolvedValueOnce([ENGAGED]);
    const dialog = await openEngage();

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "  provider incident 4471  " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Engage kill switch" }));

    await waitFor(() =>
      expect(engageModelKillSwitch).toHaveBeenCalledWith(ORG, { reason: "provider incident 4471" }, undefined),
    );
    // Organization-wide: no route_key in the body.
    expect(engageModelKillSwitch.mock.calls[0]![1]).not.toHaveProperty("route_key");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // What the panel shows now is the SERVER's answer to a second read.
    expect(await screen.findByText(STOPPED)).toBeInTheDocument();
    expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Kill switch engaged. Model use is stopped for the whole organization.")).toBeInTheDocument();
    expect(releaseButton()).toBeEnabled();
    expect(engageButton()).not.toBeInTheDocument();
  });

  it("keeps the dialog open and shows a refusal in the server's own words", async () => {
    engageModelKillSwitch.mockRejectedValue(new ApiError(403, "requires PlatformAdmin"));
    const dialog = await openEngage();

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider incident 4471" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Engage kill switch" }));

    const refusal = await within(dialog).findByRole("alert");
    expect(refusal).toHaveTextContent(/^requires PlatformAdmin$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // Nothing was engaged, so nothing on the panel behind it changed, and no stale success is claimed.
    expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Kill switch engaged/)).not.toBeInTheDocument();
    // The operator can fix and retry from the same dialog.
    expect(within(dialog).getByRole("button", { name: "Engage kill switch" })).toBeEnabled();
  });

  it("shows the server's validation sentence when the reason is too short", async () => {
    engageModelKillSwitch.mockRejectedValue(
      new ApiError(422, "body.reason: String should have at least 3 characters"),
    );
    const dialog = await openEngage();

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "no" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Engage kill switch" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "body.reason: String should have at least 3 characters",
    );
  });

  it("admits one engage at a time and shows the dialog busy meanwhile", async () => {
    let settle: (state: KillSwitchStateRead) => void = () => undefined;
    engageModelKillSwitch.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openEngage();

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider incident 4471" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Engage kill switch" }));

    const working = await within(dialog).findByRole("button", { name: "Working…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);
    expect(engageModelKillSwitch).toHaveBeenCalledTimes(1);
    settle(ENGAGED);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("cancels without sending anything, and gives focus back to the button that opened it", async () => {
    const dialog = await openEngage();
    expect(document.activeElement).toBe(within(dialog).getByLabelText(/Reason/));

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(engageModelKillSwitch).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Engage kill switch" }));
  });

  it("does not throw away a typed reason on a click outside the dialog", async () => {
    const dialog = await openEngage();
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider incident 4471" } });

    fireEvent.mouseDown(dialog.parentElement!);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).getByLabelText(/Reason/)).toHaveValue("provider incident 4471");
  });

  it("does not carry an earlier refusal into the next time the dialog opens", async () => {
    engageModelKillSwitch.mockRejectedValue(new ApiError(403, "requires PlatformAdmin"));
    const dialog = await openEngage();
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider incident 4471" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Engage kill switch" }));
    await within(dialog).findByRole("alert");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));

    const reopened = await screen.findByRole("dialog");
    expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(reopened).getByLabelText(/Reason/)).toHaveValue("");
  });
});

describe("KillSwitchPanel: releasing", () => {
  async function openRelease() {
    sessionMe = asRoles("PlatformAdmin");
    fetchModelKillSwitchState.mockResolvedValueOnce([ENGAGED]);
    render(<KillSwitchPanel organizationId={ORG} />);
    await screen.findByText(STOPPED);
    fireEvent.click(screen.getByRole("button", { name: "Release kill switch" }));
    return await screen.findByRole("dialog", { name: "Release the organization kill switch?" });
  }

  it("states the effect and asks for a reason, and sends nothing on the click", async () => {
    const dialog = await openRelease();

    expect(dialog).toHaveTextContent("This lets model calls resume for the whole organization");
    expect(dialog).toHaveTextContent("wherever a route is otherwise approved and active");
    expect(dialog).toHaveTextContent("recorded in the audit ledger with your reason");
    expect(within(dialog).getByRole("button", { name: "Release kill switch" })).toBeDisabled();
    expect(releaseModelKillSwitch).not.toHaveBeenCalled();
  });

  it("releases with the reason, then re-reads", async () => {
    releaseModelKillSwitch.mockResolvedValue(RELEASED);
    const dialog = await openRelease();
    fetchModelKillSwitchState.mockResolvedValueOnce([RELEASED]); // the re-read after the release

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider recovered" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Release kill switch" }));

    await waitFor(() =>
      expect(releaseModelKillSwitch).toHaveBeenCalledWith(ORG, { reason: "provider recovered" }, undefined),
    );
    expect(releaseModelKillSwitch.mock.calls[0]![1]).not.toHaveProperty("route_key");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
    expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(2);
    expect(
      screen.getByText("Kill switch released. Model calls can resume wherever a route is approved and active."),
    ).toBeInTheDocument();
    expect(engageButton()).toBeEnabled();
    expect(releaseButton()).not.toBeInTheDocument();
  });

  it("shows the server's 409 verbatim, and re-reads so the panel stops offering a release that no longer applies", async () => {
    // Someone else released it first. There is no maker-checker on release: this is the only state refusal.
    releaseModelKillSwitch.mockRejectedValue(new ApiError(409, "kill switch is not currently engaged"));
    const dialog = await openRelease();
    fetchModelKillSwitchState.mockResolvedValueOnce([RELEASED]); // what the 409's re-read finds

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider recovered" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Release kill switch" }));

    const refusal = await within(dialog).findByRole("alert");
    expect(refusal).toHaveTextContent(/^kill switch is not currently engaged$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await waitFor(() => expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(2));

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // The panel behind is now the truth: not engaged, so Engage is what is offered.
    expect(await screen.findByText(NOT_ENGAGED)).toBeInTheDocument();
    expect(releaseButton()).not.toBeInTheDocument();
    expect(engageButton()).toBeEnabled();
    // A refusal is not a release: no success is claimed.
    expect(screen.queryByText(/Kill switch released/)).not.toBeInTheDocument();
  });

  it("shows a 403 on release as the server sent it", async () => {
    releaseModelKillSwitch.mockRejectedValue(new ApiError(403, "requires PlatformAdmin"));
    const dialog = await openRelease();

    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: "provider recovered" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Release kill switch" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^requires PlatformAdmin$/);
    // A 403 changes nothing, so the state is not re-read on its account.
    expect(fetchModelKillSwitchState).toHaveBeenCalledTimes(1);
  });
});

describe("KillSwitchPanel: accessibility", () => {
  it("has no WCAG A/AA violations in the engaged, released and not-applicable states", async () => {
    sessionMe = asRoles("PlatformAdmin");
    fetchModelKillSwitchState.mockResolvedValue([ENGAGED, STOPPED_ROUTE]);
    const engaged = render(<KillSwitchPanel organizationId={ORG} />);
    await screen.findByText(STOPPED);
    await expectNoAxeViolations(engaged.container);
    expect(
      unnamedFocusableElements(engaged.container).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
    engaged.unmount();

    fetchModelKillSwitchState.mockResolvedValue([RELEASED]);
    const released = render(<KillSwitchPanel organizationId={ORG} />);
    await screen.findByText(NOT_ENGAGED);
    await expectNoAxeViolations(released.container);
    released.unmount();

    sessionMe = asRoles("Analyst");
    const notApplicable = render(<KillSwitchPanel organizationId={ORG} />);
    await screen.findByText(/Not applicable to your roles/);
    await expectNoAxeViolations(notApplicable.container);
  });

  it("has no WCAG A/AA violations with the confirmation open", async () => {
    sessionMe = asRoles("PlatformAdmin");
    fetchModelKillSwitchState.mockResolvedValue([ENGAGED]);
    render(<KillSwitchPanel organizationId={ORG} />);
    await screen.findByText(STOPPED);
    fireEvent.click(screen.getByRole("button", { name: "Release kill switch" }));
    await screen.findByRole("dialog");

    await expectNoAxeViolations(document.body);
  });
});
