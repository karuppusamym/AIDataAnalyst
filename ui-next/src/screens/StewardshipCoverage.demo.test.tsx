import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { resetLocationCacheForTests } from "../lib/location";
import type { MeRead } from "../lib/types";
import type { Session } from "../lib/session";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";

/* ---------------------------------------------------------------------------
   The coverage scorecard against the DEMO estate (R11-AUD08), end to end:
   the real screen, the real API module, the real fixtures -- nothing mocked but
   who is signed in. The demo build is what the accessibility sweep and a first
   look at the product both see, so it has to be a working scorecard and not a
   screen that renders only when a test feeds it.

   The demo user holds Analyst, DataSteward and Viewer (`makeFixtureMe`).
--------------------------------------------------------------------------- */

vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  const me: MeRead = {
    principal_id: "dev-fixture-user", principal_type: "USER", organization_id: null,
    roles: ["Analyst", "DataSteward", "Viewer"], persona: null, identity_provider: "DEVELOPMENT",
  };
  return {
    ...actual,
    useSession: (): Session => ({
      state: "demo", me, lapsed: false, lastSuccessAt: null, error: null,
      dataMode: "fixtures", authMode: "development", authModeInferred: false, reload: () => undefined,
    }),
  };
});

async function mount(url = "/?view=coverage#/steward/stewardship") {
  window.history.replaceState(null, "", url);
  resetLocationCacheForTests();
  const { StewardshipCoverage } = await import("./StewardshipCoverage");
  return render(<StewardshipCoverage />);
}

beforeEach(() => {
  // A fresh fixture store per test: the demo history is module state, like the server's table.
  vi.resetModules();
});

describe("the demo scorecard", () => {
  it("shows the demo figures, the six dimensions and a seeded history with its trend", async () => {
    await mount();

    expect(await screen.findByText(/overall for the whole organization/)).toBeInTheDocument();
    const dims = screen.getByRole("table", { name: /Coverage of the whole organization by dimension/ });
    expect(within(dims).getAllByRole("row")).toHaveLength(7); // header + six
    const history = await screen.findByRole("table", { name: /Stored coverage snapshots/ });
    expect(within(history).getAllByRole("row").length).toBeGreaterThan(2);
    expect(await screen.findByRole("img", { name: /Overall coverage across \d+ snapshots, oldest to newest/ })).toBeInTheDocument();
  });

  it("takes a snapshot in the demo: a new row on top of the history, said in words", async () => {
    await mount();
    await screen.findByRole("table", { name: /Stored coverage snapshots/ });
    const rowsBefore = within(screen.getByRole("table", { name: /Stored coverage snapshots/ })).getAllByRole("row").length;

    fireEvent.click(screen.getByRole("button", { name: "Take a snapshot" }));
    const dialog = await screen.findByRole("dialog", { name: "Take a coverage snapshot?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));

    expect(await screen.findByText(/Snapshot stored for the whole organization: 48 active tables, overall /)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        within(screen.getByRole("table", { name: /Stored coverage snapshots/ })).getAllByRole("row"),
      ).toHaveLength(rowsBefore + 1),
    );
    // Newest first: the demo steward's row is the first body row.
    const first = within(screen.getByRole("table", { name: /Stored coverage snapshots/ })).getAllByRole("row")[1]!;
    expect(first).toHaveTextContent("demo.steward");
  });

  it("scopes to a demo datasource: fewer tables, and an empty history of its own", async () => {
    await mount();
    await screen.findByText(/overall for the whole organization/);
    const scope = screen.getByRole("combobox", { name: "Scope" });
    await waitFor(() => expect(within(scope).getAllByRole("option").length).toBeGreaterThan(1));
    const source = within(scope).getAllByRole("option")[1]!;

    fireEvent.change(scope, { target: { value: (source as HTMLOptionElement).value } });

    expect(await screen.findByText(new RegExp(`overall for ${source.textContent}`))).toBeInTheDocument();
    expect(await screen.findByText(new RegExp(`No snapshots have been stored for ${source.textContent}`))).toBeInTheDocument();
  });

  it("has no detectable WCAG A/AA violation, populated from the demo estate", async () => {
    const { container } = await mount();
    await screen.findByRole("table", { name: /Stored coverage snapshots/ });

    await expectNoAxeViolations(container);
    expect(unnamedFocusableElements(container)).toEqual([]);
  });
});
