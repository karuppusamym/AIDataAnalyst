import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { resetLocationCacheForTests } from "../lib/location";
import type { MeRead } from "../lib/types";
import type { Session } from "../lib/session";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { SearchScreen } from "./SearchScreen";

/* ---------------------------------------------------------------------------
   Search against the DEMO estate (R11-AUD08), end to end: the real screen, the
   real API module, the real demo catalog -- nothing mocked but who is signed in.
   The demo user holds Analyst, DataSteward and Viewer (`makeFixtureMe`), so the
   Catalog admits it and a table result is a link.
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

function mount(url: string) {
  window.history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<SearchScreen />);
}

describe("the demo search", () => {
  it("finds demo tables by name, grouped, paged, and openable in the Catalog", async () => {
    mount("/?q=customer#/analyst/search");

    expect(await screen.findByRole("heading", { name: /^Tables/ })).toBeInTheDocument();
    expect(screen.getByText(/matches for “customer”: \d+ tables/)).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Search result pages" })).toBeInTheDocument();

    const open = screen.getAllByRole("button", { name: /^Open .* in the Catalog$/ })[0]!;
    fireEvent.click(open);

    await waitFor(() => expect(location.hash).toBe("#/analyst/catalog"));
    const params = new URLSearchParams(location.search);
    expect(params.get("asset")).toMatch(/^t_/);
    expect(params.get("ds")).toMatch(/^ds_/);
  });

  it("says nothing matched when nothing in the demo estate did", async () => {
    mount("/?q=zzzznotatable#/analyst/search");

    expect(await screen.findByText("No tables or columns match “zzzznotatable”")).toBeInTheDocument();
  });

  it("offers demo table names as the person types", async () => {
    const { container } = mount("/#/analyst/search");
    const box = screen.getByRole("combobox", { name: "Search by name" });

    fireEvent.change(box, { target: { value: "custom" } });

    await waitFor(() =>
      expect(container.querySelectorAll(`datalist#${CSS.escape(box.getAttribute("list")!)} option`).length).toBeGreaterThan(0),
    );
  });

  it("has no detectable WCAG A/AA violation, populated from the demo estate", async () => {
    const { container } = mount("/?q=customer#/analyst/search");
    await screen.findByRole("heading", { name: /^Tables/ });

    await expectNoAxeViolations(container);
    expect(unnamedFocusableElements(container)).toEqual([]);
  });
});
