import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useUnsavedChanges } from "../components/primitives";
import { resetUnsavedRegistryForTests } from "../lib/unsavedChanges";
import { resetLocationCacheForTests } from "../lib/location";
import { StewardshipWorkspace, stewardshipViewFrom } from "./StewardshipWorkspace";

/* ---------------------------------------------------------------------------
   R11-S13 (items 15/17) — the stewardship workspace SHELL.

   The three views are components that already existed and keep their own
   tests (`StewardshipScreen.test.tsx`, `PlaybooksScreen.test.tsx`). What is
   only true of the workspace is here:

     * the view axis is the URL, so a view is linkable and survives a reload;
     * every link that opened the old page still opens what it showed --
       including a bulk-form link written before `view` existed;
     * an unrun bulk action is not discarded in silence by a view switch,
       whether the switch came from a click or from the keyboard;
     * the keyboard model of a tablist: one tab stop, arrows, Home/End;
     * the contextual links go to the destinations that stayed separate,
       without absorbing them.

   The views are stubbed, because a test about the shell that mounts three
   real screens is a test about three screens' mocks.
--------------------------------------------------------------------------- */

/** Stands in for `StewardshipBulkActions` holding an edited, unrun action:
 *  the same hook, reporting into the same registry the tab bar consults. */
function DirtyBulk() {
  useUnsavedChanges(true, "Discard the bulk action you have not run?");
  return <p>bulk content</p>;
}
let bulkDirty = false;
vi.mock("./StewardshipScreen", () => ({
  StewardshipWorkQueue: () => <p>queue content</p>,
  StewardshipBulkActions: () => (bulkDirty ? <DirtyBulk /> : <p>bulk content</p>),
}));

vi.mock("./PlaybooksScreen", () => ({
  PlaybooksScreen: () => <p>playbooks content</p>,
}));

/* Imported statically, and `vi.resetModules()` is deliberately NOT called:
   the stubbed dirty view and the tab bar's guard must talk to the SAME
   `lib/unsavedChanges` instance (see `DocumentationWorkspace.test.tsx`). */
function mount(url = "/#/steward/stewardship") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<StewardshipWorkspace />);
}

const viewParam = () => new URLSearchParams(location.search).get("view");
const tab = (name: string) => screen.getByRole("tab", { name });

beforeEach(() => {
  bulkDirty = false;
  resetUnsavedRegistryForTests();
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetUnsavedRegistryForTests();
});

describe("the stewardship workspace view axis", () => {
  it("leads with the Work queue and keeps the canonical URL free of ?view=", async () => {
    mount();

    expect(await screen.findByText("queue content")).toBeInTheDocument();
    expect(tab("Work queue")).toHaveAttribute("aria-selected", "true");
    expect(viewParam()).toBeNull();
    // The three views, in the order design 21 §17 leads with.
    expect(screen.getAllByRole("tab").map((element) => element.textContent)).toEqual([
      "Work queue",
      "Bulk actions",
      "Automation",
    ]);
    // The panel is named by the tab in front of it.
    expect(screen.getByRole("tabpanel", { name: "Work queue" })).toBeInTheDocument();
  });

  it("puts the view in the URL, so a view is a link", async () => {
    mount();
    await screen.findByText("queue content");

    fireEvent.click(tab("Automation"));
    await waitFor(() => expect(viewParam()).toBe("automation"));
    expect(await screen.findByText("playbooks content")).toBeInTheDocument();

    fireEvent.click(tab("Bulk actions"));
    await waitFor(() => expect(viewParam()).toBe("bulk"));
    expect(await screen.findByText("bulk content")).toBeInTheDocument();

    // Back to the default drops the field rather than writing `?view=queue`,
    // so one view does not have two spellings.
    fireEvent.click(tab("Work queue"));
    await waitFor(() => expect(viewParam()).toBeNull());
  });

  it("opens the view a URL names, and says which population that view reads", async () => {
    mount("/?view=automation#/steward/stewardship");

    expect(await screen.findByText("playbooks content")).toBeInTheDocument();
    expect(tab("Automation")).toHaveAttribute("aria-selected", "true");
    // Bulk actions read the scope picker's sources; a playbook may name any
    // source in the tenant. The workspace says which is in front.
    expect(screen.getByText(/any source in the tenant/)).toBeInTheDocument();
  });

  it("falls back to the Work queue for a ?view= value it does not know", async () => {
    mount("/?view=whatever#/steward/stewardship");

    // A link from the future, or a typo. Either way the workspace opens.
    expect(await screen.findByText("queue content")).toBeInTheDocument();
  });
});

describe("links written for the old Stewardship page", () => {
  it("opens a bulk-form link that predates ?view= on Bulk actions, filter intact", async () => {
    mount("/?action=certify&ds=ds_1&field=SCHEMA_NAME&pattern=raw_%25#/steward/stewardship");

    expect(await screen.findByText("bulk content")).toBeInTheDocument();
    expect(tab("Bulk actions")).toHaveAttribute("aria-selected", "true");
    const params = new URLSearchParams(location.search);
    expect(params.get("action")).toBe("certify");
    expect(params.get("pattern")).toBe("raw_%");
  });

  it("spells the Work queue out when a bulk filter would otherwise re-infer Bulk actions", async () => {
    /* THE TRAP. Absence of `view` means Work queue -- unless a bulk filter is
       in the URL, in which case absence means "an old bulk link". Switching
       to the Work queue by dropping `view` would therefore land straight back
       on Bulk actions. The filter is kept (coming back should find it), so
       the Work queue has to be written explicitly. */
    mount("/?view=bulk&action=own&pattern=fin_%25#/steward/stewardship");
    await screen.findByText("bulk content");

    fireEvent.click(tab("Work queue"));

    await waitFor(() => expect(viewParam()).toBe("queue"));
    expect(await screen.findByText("queue content")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("pattern")).toBe("fin_%");

    fireEvent.click(tab("Bulk actions"));
    await waitFor(() => expect(viewParam()).toBe("bulk"));
    expect(new URLSearchParams(location.search).get("action")).toBe("own");
  });

  it("does not read an inherited datasource as a bulk link", () => {
    // `ds` is estate context every datasource-scoped screen carries across a
    // screen change. Arriving from Relationships with `?ds=` is not asking for
    // the bulk form.
    expect(stewardshipViewFrom(new URLSearchParams("ds=ds_1"))).toBe("queue");
    expect(stewardshipViewFrom(new URLSearchParams("ds=ds_1&pattern=raw_%25"))).toBe("bulk");
    expect(stewardshipViewFrom(new URLSearchParams("view=automation&action=tag"))).toBe(
      "automation",
    );
  });

  it("opens Bulk actions with the selection intact for a Catalog explicit-id link, 17B", async () => {
    // `?ids=` alone (no `action`/`field`/`pattern`) is the explicit-selection
    // alternative to those fields (`CatalogScreen`'s row selection), read the
    // same way those already are: present with no `view` still means "this
    // was a bulk link".
    expect(stewardshipViewFrom(new URLSearchParams("ids=t1,t2"))).toBe("bulk");

    mount("/?action=certify&ids=t1,t2#/steward/stewardship");
    expect(await screen.findByText("bulk content")).toBeInTheDocument();
    expect(tab("Bulk actions")).toHaveAttribute("aria-selected", "true");
    expect(new URLSearchParams(location.search).get("ids")).toBe("t1,t2");
  });
});

describe("an unrun bulk action is not discarded by a view switch", () => {
  it("asks first, and stays put when the answer is no", async () => {
    bulkDirty = true;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");

    fireEvent.click(tab("Automation"));

    expect(confirm).toHaveBeenCalledWith("Discard the bulk action you have not run?");
    // Declining leaves the view where it was -- URL included, or the address
    // bar would claim a view the screen is not showing.
    expect(viewParam()).toBe("bulk");
    expect(screen.getByText("bulk content")).toBeInTheDocument();
  });

  it("switches when the answer is yes", async () => {
    bulkDirty = true;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");

    fireEvent.click(tab("Automation"));

    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(viewParam()).toBe("automation"));
  });

  it("asks nothing when the bulk form is clean", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");

    fireEvent.click(tab("Work queue"));

    expect(confirm).not.toHaveBeenCalled();
    await waitFor(() => expect(viewParam()).toBeNull());
  });
});

describe("the view tabs from the keyboard", () => {
  it("is one tab stop: only the selected view's tab is in the tab order", async () => {
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");

    expect(tab("Bulk actions")).toHaveAttribute("tabindex", "0");
    expect(tab("Work queue")).toHaveAttribute("tabindex", "-1");
    expect(tab("Automation")).toHaveAttribute("tabindex", "-1");
  });

  it("moves with the arrow keys, wrapping at both ends, and focus follows", async () => {
    mount();
    await screen.findByText("queue content");
    act(() => tab("Work queue").focus());

    fireEvent.keyDown(tab("Work queue"), { key: "ArrowRight" });
    await waitFor(() => expect(viewParam()).toBe("bulk"));
    expect(tab("Bulk actions")).toHaveFocus();

    fireEvent.keyDown(tab("Bulk actions"), { key: "ArrowRight" });
    await waitFor(() => expect(viewParam()).toBe("automation"));
    expect(tab("Automation")).toHaveFocus();

    // Past the last tab: back to the first.
    fireEvent.keyDown(tab("Automation"), { key: "ArrowRight" });
    await waitFor(() => expect(viewParam()).toBeNull());
    expect(tab("Work queue")).toHaveFocus();

    // Before the first tab: round to the last.
    fireEvent.keyDown(tab("Work queue"), { key: "ArrowLeft" });
    await waitFor(() => expect(viewParam()).toBe("automation"));
    expect(tab("Automation")).toHaveFocus();
  });

  it("jumps to the ends with Home and End", async () => {
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");

    fireEvent.keyDown(tab("Bulk actions"), { key: "End" });
    await waitFor(() => expect(viewParam()).toBe("automation"));
    expect(tab("Automation")).toHaveFocus();

    fireEvent.keyDown(tab("Automation"), { key: "Home" });
    await waitFor(() => expect(viewParam()).toBeNull());
    expect(tab("Work queue")).toHaveFocus();
  });

  it("keeps the view and the focus where they were when the prompt is declined", async () => {
    bulkDirty = true;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");
    act(() => tab("Bulk actions").focus());

    fireEvent.keyDown(tab("Bulk actions"), { key: "ArrowRight" });

    expect(confirm).toHaveBeenCalled();
    expect(viewParam()).toBe("bulk");
    expect(tab("Bulk actions")).toHaveFocus();
    expect(tab("Bulk actions")).toHaveAttribute("aria-selected", "true");
  });

  it("leaves other keys to the browser", async () => {
    mount();
    await screen.findByText("queue content");

    // Enter and Space activate a <button> natively; Tab must still leave the
    // tablist. None of them is intercepted.
    for (const key of ["Enter", " ", "Tab", "ArrowDown"]) {
      const notCancelled = fireEvent.keyDown(tab("Work queue"), { key });
      expect(notCancelled).toBe(true);
    }
    expect(viewParam()).toBeNull();
  });
});

describe("the destinations that stayed separate are one link away", () => {
  it("links the Work queue to Documentation and Negative knowledge, on their own routes", async () => {
    mount();
    await screen.findByText("queue content");

    const related = within(screen.getByRole("navigation", { name: "Related work" }));
    fireEvent.click(related.getByRole("button", { name: /Documentation priorities/ }));
    // Documentation keeps its own workspace; the link opens it rather than
    // nesting its tab bar here.
    await waitFor(() => expect(location.hash).toBe("#/steward/worklist"));
  });

  it("links Automation to the task-agent console with the agent selected", async () => {
    mount("/?view=automation#/steward/stewardship");
    await screen.findByText("playbooks content");

    const runs = within(screen.getByRole("navigation", { name: "Agent runs" }));
    expect(runs.getAllByRole("button").map((element) => element.textContent)).toEqual([
      "Steward agent →",
      "Lineage agent →",
      "Quality agent →",
    ]);
    fireEvent.click(runs.getByRole("button", { name: /Lineage agent/ }));

    // Bounded agent execution is its own contract, so it stays its own
    // destination -- this opens it, it does not embed it.
    await waitFor(() => expect(location.hash).toBe("#/steward/task-agents"));
    expect(new URLSearchParams(location.search).get("agent")).toBe("lineage");
  });

  it("offers no links on Bulk actions", async () => {
    mount("/?view=bulk#/steward/stewardship");
    await screen.findByText("bulk content");

    expect(screen.queryByRole("navigation")).toBeNull();
  });
});
