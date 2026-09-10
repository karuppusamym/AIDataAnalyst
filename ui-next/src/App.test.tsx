import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { MeRead } from "./lib/types";

/* ---------------------------------------------------------------------------
   UX-1 end-to-end through the shell: whatever `GET /v1/me` reports is what
   decides the switcher's presence in the actually-rendered app, not just in
   PersonaNav's own unit tests.
--------------------------------------------------------------------------- */

const fetchMe = vi.fn<() => Promise<MeRead>>();
vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    fetchMe: () => fetchMe(),
  };
});

async function loadApp() {
  const { default: App } = await import("./App");
  return App;
}

beforeEach(() => {
  fetchMe.mockReset();
  vi.resetModules();
  history.replaceState(null, "", "/");
});

describe("App shell persona gating", () => {
  it("keeps the sidebar compact and reveals each workbench on demand", async () => {
    history.replaceState(null, "", "/#/catalog");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    render(<App />);
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    expect(nav.getByRole("button", { name: "Analyst" })).toHaveAttribute("aria-expanded", "true");
    expect(nav.queryByRole("button", { name: /Operations/ })).not.toBeInTheDocument();
    fireEvent.click(nav.getByRole("button", { name: "Operator" }));
    expect(nav.queryByRole("button", { name: /Catalog/ })).not.toBeInTheDocument();
    fireEvent.click(nav.getByRole("button", { name: /Operations/ }));
    expect(location.hash).toBe("#/operations");
    expect(nav.getByRole("button", { name: "Operator" })).toHaveAttribute("aria-expanded", "true");
  });

  it("removes the persona switcher once /v1/me reports the OIDC identity provider", async () => {
    fetchMe.mockResolvedValue({
      principal_id: "bank-user-123",
      principal_type: "USER",
      organization_id: null,
      roles: ["DataSteward"],
      persona: "Steward",
      identity_provider: "OIDC",
    });
    const App = await loadApp();

    render(<App />);

    await waitFor(() => expect(screen.getByTestId("persona-nav")).toHaveAttribute("data-mode", "oidc"));
    expect(screen.queryByTestId("persona-select")).not.toBeInTheDocument();
    expect(screen.getByTestId("persona-value")).toHaveTextContent("Steward");
  });

  it("keeps the persona switcher when /v1/me reports the development identity provider", async () => {
    fetchMe.mockResolvedValue({
      principal_id: "dev-fixture-user",
      principal_type: "USER",
      organization_id: null,
      roles: ["Analyst"],
      persona: null,
      identity_provider: "DEVELOPMENT",
    });
    const App = await loadApp();

    render(<App />);

    await waitFor(() =>
      expect(screen.getByTestId("persona-nav")).toHaveAttribute("data-mode", "development"),
    );
    expect(screen.getByTestId("persona-select")).toBeInTheDocument();
  });

  it("renders no persona nav before /v1/me resolves", async () => {
    fetchMe.mockReturnValue(new Promise(() => {})); // never resolves within the test
    const App = await loadApp();

    render(<App />);

    expect(screen.queryByTestId("persona-nav")).not.toBeInTheDocument();
  });

  it("offers keyboard-friendly quick navigation across the full product", async () => {
    fetchMe.mockResolvedValue({
      principal_id: "dev-fixture-user",
      principal_type: "USER",
      organization_id: null,
      roles: ["Analyst"],
      persona: null,
      identity_provider: "DEVELOPMENT",
    });
    const App = await loadApp();
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /Jump to/ }));
    const input = screen.getByRole("textbox", { name: "Search pages" });
    fireEvent.change(input, { target: { value: "context compile" } });
    fireEvent.click(within(screen.getByRole("dialog", { name: "Quick navigation" })).getByRole("button", { name: /Context products/ }));

    expect(location.hash).toBe("#/context");
    expect(screen.queryByRole("dialog", { name: "Quick navigation" })).not.toBeInTheDocument();
  });

  it("provides an ordered in-page menu for the active product section", async () => {
    history.replaceState(null, "", "/#/catalog");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    render(<App />);

    // UX-20: sections are persona workbenches, so Catalog sits in the
    // Analyst workbench alongside the rest of an analyst's jobs.
    const section = screen.getByRole("navigation", { name: "Analyst pages" });
    expect(within(section).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Ask Atlas",
      "Catalog",
      "Semantic layer",
      "Tool registry",
      "Tool plans",
      "Lineage",
      "Unified lineage",
    ]);
    expect(within(section).getByRole("button", { name: "Catalog" })).toHaveAttribute("aria-current", "page");

    fireEvent.click(within(section).getByRole("button", { name: "Semantic layer" }));
    expect(location.hash).toBe("#/semantics");
  });

  it("restores the correct page on browser history navigation", async () => {
    history.replaceState(null, "", "/#/catalog");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    render(<App />);

    history.replaceState(null, "", "/#/operations");
    fireEvent(window, new PopStateEvent("popstate"));

    await waitFor(() => expect(screen.getByRole("navigation", { name: "Operator pages" })).toBeInTheDocument());
    expect(screen.getByText("Operations", { selector: ".topbar__trail strong" })).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   F13/T10 and F21/T17 in the assembled shell. The badge must report the
   build's real state rather than the two hard-coded strings it replaced, and
   the route outlet must survive a screen that throws.
--------------------------------------------------------------------------- */

describe("shell status", () => {
  it("reports demo data instead of the old static 'Platform connected' / 'Live'", async () => {
    fetchMe.mockResolvedValue({
      principal_id: "dev-fixture-user",
      principal_type: "USER",
      organization_id: null,
      roles: ["Analyst"],
      persona: null,
      identity_provider: "DEVELOPMENT",
    });
    const App = await loadApp();
    render(<App />);

    const badge = await screen.findByTestId("shell-status");
    expect(badge).toHaveAttribute("data-state", "demo");
    expect(badge).toHaveTextContent("Demo data");
    // The colour is not the success colour: a demo build must not read as a
    // healthy connection.
    expect(badge).toHaveAttribute("data-tone", "demo");

    expect(screen.queryByText("Platform connected")).not.toBeInTheDocument();
    expect(screen.queryByText("Live")).not.toBeInTheDocument();
  });

  it("offers no reconnect action in a state that reconnecting cannot fix", async () => {
    fetchMe.mockResolvedValue({
      principal_id: "dev-fixture-user",
      principal_type: "USER",
      organization_id: null,
      roles: ["Analyst"],
      persona: null,
      identity_provider: "DEVELOPMENT",
    });
    const App = await loadApp();
    render(<App />);

    await screen.findByTestId("shell-status");
    expect(screen.queryByTestId("session-reconnect")).not.toBeInTheDocument();
  });
});

describe("navigation drops the page you left behind", () => {
  it("does not carry one screen's filters into the next screen", async () => {
    // `status` means something different on Data quality and on Studio; the
    // old shell defaulted the query to `location.search`, so it arrived
    // anyway (F09).
    history.replaceState(null, "", "/?severity=HIGH&status=OPEN#/quality");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    render(<App />);

    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    fireEvent.click(nav.getByRole("button", { name: "Reviewer" }));
    fireEvent.click(nav.getByRole("button", { name: "Review queue" }));

    expect(location.hash).toBe("#/governance");
    expect(location.search).not.toContain("severity=HIGH");
    expect(location.search).not.toContain("status=OPEN");
  });
});

describe("the command palette is a real modal", () => {
  /* F21's own worked example. The palette declared `role="dialog"
   * aria-modal="true"` with an autofocused input and an Escape handler, and
   * had none of the three properties those attributes advertise. Driving the
   * running app in a real browser confirmed it: 52 Tab presses walked out of
   * the open modal into the sidebar behind it.
   *
   * jsdom cannot assert that — it implements no sequential focus navigation,
   * which is precisely why the defect survived a green unit suite. What jsdom
   * CAN assert is the structural guarantee underneath: the palette renders
   * through the shared `Dialog`, so the background really is marked inert
   * while it is open and really is released afterwards. The Tab-order proof
   * belongs to `Dialog`'s own tests and to interactive validation. */
  it("marks the background inert while open and releases it on close", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    render(<App />);
    await screen.findByRole("button", { name: /Jump to/ });

    const backgroundBefore = [...document.body.children].map((el) => el.hasAttribute("inert"));
    expect(backgroundBefore.some(Boolean)).toBe(false);

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = await screen.findByRole("dialog", { name: /Quick navigation/ });
    expect(dialog.getAttribute("aria-modal")).toBe("true");

    // The shell's own panes are inert; the portal the dialog lives in is not.
    const inertFlags = [...document.body.children].map((el) => el.hasAttribute("inert"));
    expect(inertFlags.filter(Boolean).length).toBeGreaterThan(0);
    expect(inertFlags.some((flag) => !flag)).toBe(true);

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /Quick navigation/ })).toBeNull(),
    );
    // Released, not merely hidden: a stuck `inert` would leave the whole shell
    // unreachable to keyboard and assistive technology after one Ctrl+K.
    expect([...document.body.children].every((el) => !el.hasAttribute("inert"))).toBe(true);
  });
});
