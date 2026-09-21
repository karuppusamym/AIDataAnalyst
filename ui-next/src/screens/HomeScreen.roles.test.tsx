import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { MeRead } from "../lib/types";
import type { Session, SessionState } from "../lib/session";

/* ---------------------------------------------------------------------------
   Home and the review-queue count, by role (R11-AUD01, "two quiet mismatches").

   `GET /v1/governance/reviews/queue/summary` is admitted to DataSteward,
   PlatformAdmin, Reviewer and SemanticAdmin (surface-control matrix,
   `aida.review_queue_api.get_review_queue_summary`). Home asked for it on every
   load, for everyone; the demo rehearsal saw a 403 on it for `sam.agentdev`, and
   the page rendered "0 decisions waiting" -- a zero the role was simply never
   allowed to count.

   WHAT THESE PIN, and why it is a separate file from `HomeScreen.test.tsx`:
   that file runs the page against the bundled demo estate, where a "request"
   is a function call on fixtures. The property here is about the request the
   LIVE build sends, so `USE_FIXTURES` is false and `get` is the spy -- the
   assertion is on the URL that would have gone over the wire.

     1. A session outside the four roles sends NO request for the count, renders
        the tile as not applicable (not an error, not a zero), and does not show
        the "signals unavailable" notice, because nothing was unavailable.
     2. A session inside them still gets its count.
     3. While `/v1/me` is in flight the count is HELD, not asked for: the
        rehearsal still saw a 403 for `sam.agentdev` when the count was sent on
        a guess and withdrawn afterwards. When identity answers, a reviewer gets
        its count and anyone else never sends the request -- neither re-fetches
        the catalog. If identity never answers (the request failed) the count
        is asked for, because the server's 403 is the authority.
     4. A refusal or failure for an admitted role is a failure, shown as one --
        a dash and the notice, never a false zero.
--------------------------------------------------------------------------- */

const get = vi.fn<(path: string, signal?: AbortSignal) => Promise<unknown>>();
const fetchCatalogRows = vi.fn();
const listOrgDatasources = vi.fn();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    USE_FIXTURES: false,
    get: (path: string, signal?: AbortSignal) => get(path, signal),
    fetchCatalogRows: (...args: unknown[]) => fetchCatalogRows(...args),
    listOrgDatasources: (...args: unknown[]) => listOrgDatasources(...args),
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

const QUEUE_SUMMARY = "/v1/governance/reviews/queue/summary?status=PENDING";
const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});
const queueCalls = () => get.mock.calls.filter(([path]) => path.includes("/reviews/queue"));
/** Home's own catalog read -- `FirstSourceSetup` reads the same function with a different limit. */
const homeCatalogCalls = () =>
  fetchCatalogRows.mock.calls.filter(([query]) => (query as { limit?: number }).limit === 12);

async function renderHome() {
  const { HomeScreen } = await import("./HomeScreen");
  const view = render(<HomeScreen persona="Analyst" onNavigate={() => undefined} />);
  return { HomeScreen, ...view };
}

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue({ total: 7 });
  fetchCatalogRows.mockReset();
  fetchCatalogRows.mockResolvedValue({ items: [], total: 0, limit: 12, offset: 0 });
  listOrgDatasources.mockReset();
  listOrgDatasources.mockResolvedValue({ items: [], total: 0, limit: 500, offset: 0 });
  sessionMe = null;
  sessionState = "connected";
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Home's review-queue count, by role", () => {
  it.each(["AgentDeveloper", "ToolDeveloper", "Analyst", "Viewer"])(
    "does not ask for the count as %s, and renders the tile as not applicable",
    async (role) => {
      sessionMe = asRoles(role);
      await renderHome();

      // The rest of the page still loads: this is one signal, not the page.
      await waitFor(() => expect(homeCatalogCalls()).toHaveLength(1));
      const tile = await screen.findByText("Not applicable");
      expect(tile.closest(".homekpi")).toHaveTextContent("Decisions waiting");
      expect(queueCalls()).toHaveLength(0);

      // Not an error, not a zero, and not a link into a queue it cannot open.
      expect(screen.queryByText(/temporarily unavailable/)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Decisions waiting/ })).not.toBeInTheDocument();
      expect(screen.queryByText("Review decisions")).not.toBeInTheDocument();
      expect(tile.closest(".homekpi")).not.toHaveTextContent(/\d/);
    },
  );

  it.each(["DataSteward", "PlatformAdmin", "Reviewer", "SemanticAdmin"])(
    "asks for, and shows, the count as %s",
    async (role) => {
      sessionMe = asRoles(role);
      await renderHome();

      const tile = await screen.findByRole("button", { name: /Decisions waiting/ });
      await waitFor(() => expect(tile).toHaveTextContent("7"));
      expect(queueCalls()).toEqual([[QUEUE_SUMMARY, expect.any(AbortSignal)]]);
      expect(screen.getByText("Review decisions")).toBeInTheDocument();
      expect(screen.queryByText("Not applicable")).not.toBeInTheDocument();
    },
  );

  it("counts a session as admitted when ANY of its roles is", async () => {
    sessionMe = asRoles("Viewer", "Reviewer");
    await renderHome();

    await waitFor(() => expect(queueCalls()).toHaveLength(1));
    expect(screen.queryByText("Not applicable")).not.toBeInTheDocument();
  });

  it("treats an empty role list as holding none of them", async () => {
    sessionMe = asRoles();
    await renderHome();

    expect(await screen.findByText("Not applicable")).toBeInTheDocument();
    expect(queueCalls()).toHaveLength(0);
  });

  it("holds the count while identity is in flight, then decides without re-reading the catalog", async () => {
    // `/v1/me` has not answered: the session is "connecting" and `me` is null. Nothing is sent
    // yet -- a session that turns out not to be a reviewer would take a 403 for it -- and the
    // rest of the page still loads.
    sessionState = "connecting";
    const { HomeScreen, rerender } = await renderHome();
    await waitFor(() => expect(homeCatalogCalls()).toHaveLength(1));
    expect(queueCalls()).toHaveLength(0);
    expect(screen.queryByText("Not applicable")).not.toBeInTheDocument();

    // Identity arrives: an AgentDeveloper. The request was never made, and never will be.
    sessionState = "connected";
    sessionMe = asRoles("AgentDeveloper");
    rerender(<HomeScreen persona="Analyst" onNavigate={() => undefined} />);

    expect(await screen.findByText("Not applicable")).toBeInTheDocument();
    expect(queueCalls()).toHaveLength(0);
    // Nothing else was re-fetched for the answer.
    expect(homeCatalogCalls()).toHaveLength(1);
  });

  it("asks once identity has answered, when the session is a reviewer", async () => {
    sessionState = "connecting";
    const { HomeScreen, rerender } = await renderHome();
    await waitFor(() => expect(homeCatalogCalls()).toHaveLength(1));
    expect(queueCalls()).toHaveLength(0);

    sessionState = "connected";
    sessionMe = asRoles("Reviewer");
    rerender(<HomeScreen persona="Analyst" onNavigate={() => undefined} />);

    const tile = await screen.findByRole("button", { name: /Decisions waiting/ });
    await waitFor(() => expect(tile).toHaveTextContent("7"));
    expect(queueCalls()).toEqual([[QUEUE_SUMMARY, expect.any(AbortSignal)]]);
    expect(homeCatalogCalls()).toHaveLength(1);
  });

  it("still asks when identity will not answer: the server stays the authority", async () => {
    // `/v1/me` failed: `me` is null and the state is not "connecting". Holding the read would
    // leave it loading forever, so it is sent and the server's answer decides.
    sessionState = "disconnected";
    await renderHome();

    await waitFor(() => expect(queueCalls()).toHaveLength(1));
  });

  it("paints a known non-reviewer's tile as not applicable on the very first frame, before any effect runs", async () => {
    // A client render flushes effects before a test can look, so the first frame is read from a
    // server render, which runs none: state seeded from "loading" would print a clickable dash here.
    sessionMe = asRoles("AgentDeveloper");
    const { HomeScreen } = await import("./HomeScreen");
    const { renderToString } = await import("react-dom/server");

    const html = renderToString(<HomeScreen persona="Analyst" onNavigate={() => undefined} />);

    expect(html).toContain("Not applicable");
    expect(html).not.toContain("Review decisions");
    expect(queueCalls()).toHaveLength(0);
  });

  it("shows a refused count as unavailable, with the notice -- never as zero", async () => {
    sessionMe = asRoles("Reviewer");
    get.mockRejectedValue(new ApiError(403, "requires DataSteward, PlatformAdmin, Reviewer or SemanticAdmin"));
    await renderHome();

    expect(await screen.findByText(/temporarily unavailable/)).toBeInTheDocument();
    const tile = screen.getByRole("button", { name: /Decisions waiting/ });
    expect(tile).toHaveTextContent("—");
    expect(tile).not.toHaveTextContent("0");
    // The attention row carries the same dash, not a "0" that reads as an empty queue.
    expect(screen.getByText("Review decisions").closest("button")).toHaveTextContent("—");
    expect(screen.queryByText("Not applicable")).not.toBeInTheDocument();
  });
});
