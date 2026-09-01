import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
});
