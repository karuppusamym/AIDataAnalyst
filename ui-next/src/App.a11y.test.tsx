import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MeRead } from "./lib/types";
import { expectFocusStaysWithin, expectNoAxeViolations, unnamedFocusableElements } from "./test/a11y";

/* ---------------------------------------------------------------------------
   R11-C2 — the shell, proved from the keyboard (carrying F21 · UX-5 · TS-9).

   Separate from `App.test.tsx` on purpose. That file tests what the shell
   DOES; this one tests whether a person who cannot use a mouse, or cannot
   see, can get at it. They fail for different reasons and are read by
   different people.

   The standing note in `App.test.tsx` — "jsdom cannot assert that, it
   implements no sequential focus navigation, which is precisely why the
   defect survived a green unit suite" — was true when it was written and is
   no longer. `@testing-library/user-event` implements sequential focus
   navigation in userland, so the 52-Tab escape that the 2026-09-05 review had
   to drive a real browser to find is now a test.

   WHAT THESE TESTS STILL CANNOT SEE, so that nobody reads a green run as
   conformance:

   * **No CSS is applied.** jsdom does not load the stylesheet, so every
     element is "visible" to `user-event`. That makes the Tab order asserted
     here an OVER-approximation of the real one — safe for proving focus is
     contained (a real browser can only visit fewer elements, never more) and
     useless for proving something is hidden. The `visibility:hidden` fix to
     the mobile drawer in `App.css` is therefore NOT proved here; it is on the
     human checklist.
   * **No screen reader.** `getByRole(name:)` proves the accessibility tree
     has the right name. It does not prove NVDA reads it at the right moment,
     or that the reading order makes sense.
   * **No contrast, no zoom, no second monitor.** See
     `Docs/60-delivery/24-accessibility-acceptance-2026-09-12.md`.
--------------------------------------------------------------------------- */

const fetchMe = vi.fn<() => Promise<MeRead>>();
vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return { ...actual, fetchMe: () => fetchMe() };
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

describe("the shell can be operated without a mouse", () => {
  it("puts a skip link first in the Tab order, and it reaches the main landmark", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);
    await screen.findByRole("button", { name: /Jump to/ });

    await user.tab();
    const skip = screen.getByRole("button", { name: "Skip to main content" });
    expect(document.activeElement).toBe(skip);

    await user.click(skip);
    // The landmark itself takes focus, so the next Tab continues from the
    // content and not from the top of the navigation again.
    expect(document.activeElement).toBe(screen.getByRole("main"));
  });

  it("gives every focusable control in the shell an accessible name", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    const { container } = render(<App />);
    await screen.findByRole("button", { name: /Jump to/ });

    const unnamed = unnamedFocusableElements(container);
    expect(
      unnamed.map((el) => `${el.tagName.toLowerCase()}.${(el as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no WCAG A/AA violation axe can detect", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    const { container } = render(<App />);
    await screen.findByRole("button", { name: /Jump to/ });

    await expectNoAxeViolations(container);
  });
});

describe("the command palette really does trap focus", () => {
  /* F21's own worked example, and the one the review had to open a browser to
   * settle: 52 Tab presses walked out of the open palette and into the
   * sidebar behind it. The count is the test — a two-press version passes on
   * the broken build. */
  it("keeps 60 consecutive Tab presses inside the dialog", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);
    const opener = await screen.findByRole("button", { name: /Jump to/ });

    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: /Quick navigation/ });

    await expectFocusStaysWithin(user, dialog, 60);
    /* An explicit budget, for the same reason the screen sweep has one: sixty
       sequential `userEvent` Tab presses through a real dialog take ~4s in
       jsdom on their own, which is inside the 5s default only until the suite
       is busy. The assertion is unchanged -- this is the time it is allowed to
       take, not what it checks. Found flaking here under the full run while
       passing in isolation. */
  }, 20000);

  it("returns focus to the control that opened it", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);
    const opener = await screen.findByRole("button", { name: /Jump to/ });

    await user.click(opener);
    await screen.findByRole("dialog", { name: /Quick navigation/ });
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /Quick navigation/ })).toBeNull(),
    );
    expect(document.activeElement).toBe(opener);
  });

  it("is reachable and operable by keyboard alone, end to end", async () => {
    history.replaceState(null, "", "/#/catalog");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);
    await screen.findByRole("button", { name: /Jump to/ });

    // Ctrl+K, type, Tab to the result, Enter. No pointer event anywhere in
    // this test: this is the whole journey a keyboard-only user makes to
    // change screen, and every step of it is asserted.
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: /Quick navigation/ });
    expect(document.activeElement).toBe(
      within(dialog).getByRole("textbox", { name: "Search pages" }),
    );

    await user.keyboard("review queue");
    const match = await within(dialog).findByRole("button", { name: /Review queue/ });

    // Tab off the search box and onto the single remaining result, then
    // activate it. `Enter` on a focused `<button>` is a real activation.
    await user.tab();
    expect(document.activeElement).toBe(match);
    await user.keyboard("{Enter}");

    await waitFor(() => expect(location.hash).toBe("#/reviewer/governance"));
    expect(screen.queryByRole("dialog", { name: /Quick navigation/ })).toBeNull();
  });
});

describe("a route change is announced and takes focus", () => {
  it("moves focus into the new screen and names it in a live region", async () => {
    history.replaceState(null, "", "/#/catalog");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);

    const nav = within(await screen.findByRole("navigation", { name: "Main" }));
    // The announcer exists from the first render and is empty: a live region
    // mounted together with its message announces nothing.
    expect(screen.getByTestId("route-announcer")).toHaveTextContent("");

    await user.click(nav.getByRole("button", { name: "Reviewer" }));
    await user.click(nav.getByRole("button", { name: "Review queue" }));

    // `findByRole` and not `getByRole`: the screen behind this route is a lazy
    // chunk, so the region is not in the tree on the tick the click returns.
    const region = await screen.findByRole("region", { name: "Review queue" });
    expect(document.activeElement).toBe(region);
    await waitFor(() =>
      expect(screen.getByTestId("route-announcer")).toHaveTextContent("Review queue, Reviewer"),
    );
  });

  it("does not steal focus on first render", async () => {
    history.replaceState(null, "", "/#/catalog");
    fetchMe.mockReturnValue(new Promise(() => {}));
    const App = await loadApp();
    render(<App />);
    await screen.findByRole("button", { name: /Jump to/ });

    expect(document.activeElement).toBe(document.body);
    expect(screen.getByTestId("route-announcer")).toHaveTextContent("");
  });
});

describe("the mobile navigation drawer manages focus", () => {
  /* Below 1148px the sidebar is a drawer. jsdom applies no CSS, so the
   * breakpoint itself is not what is under test here — the focus contract is,
   * and that contract is pure JavaScript and identical at every width. */
  it("moves focus into the drawer on open and back to the opener on Escape", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);

    const menu = await screen.findByRole("button", { name: "Open navigation" });
    await user.click(menu);

    const close = screen.getByRole("button", { name: "Close navigation" });
    await waitFor(() => expect(document.activeElement).toBe(close));

    await user.keyboard("{Escape}");
    await waitFor(() => expect(document.activeElement).toBe(menu));
  });

  it("returns focus to the opener when the drawer's own close button is used", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const App = await loadApp();
    render(<App />);

    const menu = await screen.findByRole("button", { name: "Open navigation" });
    await user.click(menu);
    await user.click(screen.getByRole("button", { name: "Close navigation" }));

    await waitFor(() => expect(document.activeElement).toBe(menu));
  });
});
