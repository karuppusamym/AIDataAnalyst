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
