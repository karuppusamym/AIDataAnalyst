import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MeRead, SchedulerPassStatusListRead, SchedulerPassStatusRead } from "../lib/types";
import type { Session } from "../lib/session";
import { ApiError } from "../lib/http";
import { expectNoAxeViolations } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-VAL04: a scheduler pass failing on every iteration is visible on the
   Operations screen, and only to the roles `GET /v1/operations/scheduler-passes`
   admits.
--------------------------------------------------------------------------- */

const fetchSchedulerPasses = vi.fn<(signal?: AbortSignal) => Promise<SchedulerPassStatusListRead>>();
vi.mock("../lib/api/schedulerPasses", () => ({
  fetchSchedulerPasses: (signal?: AbortSignal) => fetchSchedulerPasses(signal),
}));

let sessionMe: MeRead | null = null;
let sessionState: Session["state"] = "demo";
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

const { SchedulerPasses } = await import("./OperationsSchedulerPasses");

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

function pass(name: string, overrides: Partial<SchedulerPassStatusRead> = {}): SchedulerPassStatusRead {
  return {
    pass_name: name,
    state: "OK",
    last_attempt_at: "2026-09-21T22:00:00+00:00",
    last_success_at: "2026-09-21T22:00:00+00:00",
    last_failure_at: null,
    last_error_class: null,
    consecutive_failures: 0,
    ...overrides,
  };
}

function list(items: SchedulerPassStatusRead[]): SchedulerPassStatusListRead {
  return {
    generated_at: "2026-09-21T22:00:05+00:00",
    stale_after_seconds: 300,
    failing: items.filter((item) => item.state === "FAILING").length,
    stale: items.filter((item) => item.state === "STALE").length,
    never_run: items.filter((item) => item.state === "NEVER_RUN").length,
    items,
  };
}

beforeEach(() => {
  fetchSchedulerPasses.mockReset();
  sessionMe = asRoles("Operations");
  sessionState = "demo";
});

describe("SchedulerPasses", () => {
  it("lists a failing pass with its error class and counts the healthy ones", async () => {
    fetchSchedulerPasses.mockResolvedValue(
      list([
        pass("reaper", {
          state: "FAILING",
          consecutive_failures: 7,
          last_failure_at: "2026-09-21T21:59:50+00:00",
          last_error_class: "OperationalError",
          last_success_at: "2026-09-20T03:00:00+00:00",
        }),
        pass("delivery_worker", { state: "STALE", last_attempt_at: "2026-09-21T21:40:00+00:00" }),
        pass("owner_routing"),
        pass("due_playbooks"),
      ]),
    );
    const { container } = render(<SchedulerPasses />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("reaper")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("failing")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("7")).toBeInTheDocument();
    expect(within(rows[0]!).getByText(/OperationalError/)).toBeInTheDocument();
    expect(within(rows[1]!).getByText("not attempted lately")).toBeInTheDocument();
    expect(screen.getByText("1 failing")).toBeInTheDocument();
    expect(screen.getByText(/2 of 4 passes ran cleanly/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("says so when every pass ran cleanly, with no table", async () => {
    fetchSchedulerPasses.mockResolvedValue(list([pass("reaper"), pass("owner_routing")]));
    render(<SchedulerPasses />);

    expect(await screen.findByText(/All 2 passes ran cleanly/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it.each([["OrganizationAdmin"], ["Auditor"], ["DataSteward"], ["Viewer"]])(
    "asks nothing and shows nothing for %s",
    async (role) => {
      sessionMe = asRoles(role);
      const { container } = render(<SchedulerPasses />);

      await Promise.resolve();
      expect(fetchSchedulerPasses).not.toHaveBeenCalled();
      expect(container).toBeEmptyDOMElement();
    },
  );

  it("waits for identity before asking", async () => {
    sessionState = "connecting";
    sessionMe = null;
    render(<SchedulerPasses />);

    await Promise.resolve();
    expect(fetchSchedulerPasses).not.toHaveBeenCalled();
  });

  it("shows the server's refusal and retries", async () => {
    fetchSchedulerPasses.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    fetchSchedulerPasses.mockResolvedValueOnce(list([pass("reaper")]));
    render(<SchedulerPasses />);

    expect(await screen.findByText(/database unavailable/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText(/All 1 passes ran cleanly/)).toBeInTheDocument());
    expect(fetchSchedulerPasses).toHaveBeenCalledTimes(2);
  });
});
