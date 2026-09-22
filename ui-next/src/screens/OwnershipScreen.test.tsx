import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useUnsavedChanges } from "../components/primitives";
import { resetUnsavedRegistryForTests } from "../lib/unsavedChanges";
import { resetLocationCacheForTests } from "../lib/location";
import { OwnershipScreen, ownershipViewFrom } from "./OwnershipScreen";

/* ---------------------------------------------------------------------------
   The Ownership SHELL (R11-AUD08, part 2).

   The three panels have their own suites (`OwnershipAssignments.test.tsx`,
   `OwnershipRules.test.tsx`, `OwnershipLeaver.test.tsx`). What is only true of the
   screen is here:

     * the view is the URL, so a view is a link and survives a reload, and an
       unknown `view` opens the default rather than nothing;
     * an unfinished rule or an unsent reassignment is not thrown away in silence by
       a view switch, whether the switch came from a click or from the keyboard;
     * the keyboard model of a tablist: one tab stop, arrows wrap, Home/End;
     * the two destinations next door are one link away, on their own routes.

   The panels are stubbed: a test about the shell that mounts three real panels is a
   test about three panels' mocks.
--------------------------------------------------------------------------- */

/** Stands in for a panel holding typed, unsent work: the same hook, into the same registry the tab bar asks. */
function DirtyLeaver() {
  useUnsavedChanges(true, "Discard the leaver reassignment you have not requested?");
  return <p>leaver content</p>;
}
let leaverDirty = false;
vi.mock("./OwnershipAssignments", () => ({ OwnershipAssignments: () => <p>assignments content</p> }));
vi.mock("./OwnershipRules", () => ({ OwnershipRules: () => <p>rules content</p> }));
vi.mock("./OwnershipLeaver", () => ({ OwnershipLeaver: () => (leaverDirty ? <DirtyLeaver /> : <p>leaver content</p>) }));

function mount(url = "/#/steward/ownership") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<OwnershipScreen />);
}

const viewParam = () => new URLSearchParams(location.search).get("view");
const tab = (name: string) => screen.getByRole("tab", { name });

beforeEach(() => {
  leaverDirty = false;
  resetUnsavedRegistryForTests();
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetUnsavedRegistryForTests();
});

describe("the Ownership view axis", () => {
  it("leads with Assignments, in the order Rules and Leaver reassignment follow, and keeps the canonical URL free of ?view=", () => {
    mount();

    expect(screen.getByText("assignments content")).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((element) => element.textContent)).toEqual([
      "Assignments",
      "Rules",
      "Leaver reassignment",
    ]);
    expect(tab("Assignments")).toHaveAttribute("aria-selected", "true");
    expect(viewParam()).toBeNull();
    // The panel is named by the tab in front of it.
    expect(screen.getByRole("tabpanel", { name: "Assignments" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Ownership" })).toBeInTheDocument();
  });

  it("puts the view in the URL, so a view is a link", async () => {
    mount();

    fireEvent.click(tab("Rules"));

    await waitFor(() => expect(viewParam()).toBe("rules"));
    expect(screen.getByText("rules content")).toBeInTheDocument();
    expect(tab("Rules")).toHaveAttribute("aria-selected", "true");
    fireEvent.click(tab("Leaver reassignment"));
    await waitFor(() => expect(viewParam()).toBe("leaver"));
    expect(screen.getByText("leaver content")).toBeInTheDocument();
    fireEvent.click(tab("Assignments"));
    await waitFor(() => expect(viewParam()).toBeNull());
  });

  it.each([
    ["/?view=rules#/steward/ownership", "rules content"],
    ["/?view=leaver#/steward/ownership", "leaver content"],
    ["/?view=assignments#/steward/ownership", "assignments content"],
  ])("opens the view a link names: %s", (url, content) => {
    mount(url);

    expect(screen.getByText(content)).toBeInTheDocument();
  });

  it("opens the default for a view it does not know rather than nothing", () => {
    mount("/?view=from-the-future#/steward/ownership");

    expect(screen.getByText("assignments content")).toBeInTheDocument();
    expect(ownershipViewFrom(new URLSearchParams("view=from-the-future"))).toBe("assignments");
    expect(ownershipViewFrom(new URLSearchParams(""))).toBe("assignments");
  });

  it("keeps the Assignments filter in the URL while another view is open, for when the steward comes back", async () => {
    mount("/?subject_type=TABLE&subject_id=t1#/steward/ownership");

    fireEvent.click(tab("Rules"));

    await waitFor(() => expect(viewParam()).toBe("rules"));
    const params = new URLSearchParams(location.search);
    expect(params.get("subject_type")).toBe("TABLE");
    expect(params.get("subject_id")).toBe("t1");
  });

  it("says what the view in front reads and does, and that a request needs a different reviewer", () => {
    mount("/?view=rules#/steward/ownership");

    expect(screen.getByText(/Applying one asks a reviewer; nothing is assigned until they approve it/)).toBeInTheDocument();
    fireEvent.click(tab("Leaver reassignment"));
    expect(screen.getByText(/It asks a reviewer; nothing moves until they approve it/)).toBeInTheDocument();
  });
});

describe("an unsent reassignment is not discarded in silence by a view switch", () => {
  it("asks before leaving, stays where it was when declined, and switches when accepted", async () => {
    leaverDirty = true;
    mount("/?view=leaver#/steward/ownership");
    expect(screen.getByText("leaver content")).toBeInTheDocument();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    fireEvent.click(tab("Rules"));

    expect(confirm).toHaveBeenCalledWith("Discard the leaver reassignment you have not requested?");
    expect(viewParam()).toBe("leaver");
    expect(screen.getByText("leaver content")).toBeInTheDocument();

    confirm.mockReturnValue(true);
    fireEvent.click(tab("Rules"));
    await waitFor(() => expect(viewParam()).toBe("rules"));
  });

  it("does not ask when nothing is unsent", async () => {
    mount("/?view=leaver#/steward/ownership");
    const confirm = vi.spyOn(window, "confirm");

    fireEvent.click(tab("Rules"));

    await waitFor(() => expect(viewParam()).toBe("rules"));
    expect(confirm).not.toHaveBeenCalled();
  });

  it("leaves focus on the tab that is still selected when a keyboard move is declined", () => {
    leaverDirty = true;
    mount("/?view=leaver#/steward/ownership");
    tab("Leaver reassignment").focus();
    vi.spyOn(window, "confirm").mockReturnValue(false);

    fireEvent.keyDown(tab("Leaver reassignment"), { key: "ArrowLeft" });

    expect(viewParam()).toBe("leaver");
    expect(document.activeElement).toBe(tab("Leaver reassignment"));
  });
});

describe("the keyboard model of the tab bar", () => {
  it("has one tab stop, and only the selected tab names the panel", () => {
    mount("/?view=rules#/steward/ownership");

    expect(tab("Rules")).toHaveAttribute("tabindex", "0");
    expect(tab("Assignments")).toHaveAttribute("tabindex", "-1");
    expect(tab("Leaver reassignment")).toHaveAttribute("tabindex", "-1");
    expect(tab("Rules")).toHaveAttribute("aria-controls", "own-panel");
    expect(tab("Assignments")).not.toHaveAttribute("aria-controls");
  });

  it("moves with the arrows, wrapping at both ends, and focus follows the selection", async () => {
    mount();
    tab("Assignments").focus();

    fireEvent.keyDown(tab("Assignments"), { key: "ArrowLeft" });
    await waitFor(() => expect(viewParam()).toBe("leaver"));
    expect(document.activeElement).toBe(tab("Leaver reassignment"));

    fireEvent.keyDown(tab("Leaver reassignment"), { key: "ArrowRight" });
    await waitFor(() => expect(viewParam()).toBeNull());
    expect(document.activeElement).toBe(tab("Assignments"));

    fireEvent.keyDown(tab("Assignments"), { key: "ArrowRight" });
    await waitFor(() => expect(viewParam()).toBe("rules"));
  });

  it("jumps to the ends with Home and End", async () => {
    mount("/?view=rules#/steward/ownership");

    fireEvent.keyDown(tab("Rules"), { key: "End" });
    await waitFor(() => expect(viewParam()).toBe("leaver"));
    fireEvent.keyDown(tab("Leaver reassignment"), { key: "Home" });
    await waitFor(() => expect(viewParam()).toBeNull());
  });

  it("does not intercept Enter, Space, Tab or a vertical arrow", () => {
    mount();

    for (const key of ["Enter", " ", "Tab", "ArrowDown", "ArrowUp"]) {
      expect(fireEvent.keyDown(tab("Assignments"), { key })).toBe(true);
    }
    expect(viewParam()).toBeNull();
  });
});

describe("the destinations next door are one link away", () => {
  it("links to the Review queue, on its own route -- opened, not embedded", async () => {
    mount();
    const related = within(screen.getByRole("navigation", { name: "Related work" }));
    expect(related.getAllByRole("button").map((element) => element.textContent)).toEqual([
      "Unowned assets →",
      "Review queue →",
    ]);

    fireEvent.click(related.getByRole("button", { name: /Review queue/ }));

    // The Review queue keeps its own scope and authorization.
    await waitFor(() => expect(location.hash).toBe("#/reviewer/governance"));
  });

  it("links back to the Work queue from every view, not only the first", async () => {
    mount("/?view=leaver#/steward/ownership");

    fireEvent.click(within(screen.getByRole("navigation", { name: "Related work" })).getByRole("button", { name: /Unowned assets/ }));

    await waitFor(() => expect(location.hash).toBe("#/steward/stewardship"));
  });
});
