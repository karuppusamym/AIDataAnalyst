import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useUnsavedChanges } from "../components/primitives";
import { resetUnsavedRegistryForTests } from "../lib/unsavedChanges";
import { resetLocationCacheForTests } from "../lib/location";
import { DocumentationWorkspace } from "./DocumentationWorkspace";

/* ---------------------------------------------------------------------------
   R11-S13 (M3) — the documentation workspace SHELL.

   The three tabs are the screens that already existed and keep their own test
   files; what is only true of the merge is here:

     * the tab axis is the URL, so a tab is linkable and survives a reload;
     * each tab's own filters are left alone by a tab switch, which is what
       lets a steward come back to the tab they left and find their place;
     * AND THE ONE THAT WOULD HAVE BEEN A SILENT REGRESSION: a half-written
       description is not thrown away by switching tabs. The shell asks
       `lib/unsavedChanges` before every `pushLocation`, but a tab switch is
       `patchQuery` -- a filter edit, deliberately not navigation -- so it does
       not pass that choke point. The tab bar has to ask for itself.

   The tabs are stubbed, because a test about the shell that mounts three real
   screens is a test about three screens' mocks.
--------------------------------------------------------------------------- */

vi.mock("./DocumentationWorklistScreen", () => ({
  DocumentationWorklistScreen: () => <p>priorities content</p>,
}));

/** Stands in for `DescriptionDraftsScreen` holding a dirty `DescriptionEditor`:
 *  the same hook, reporting into the same registry the shell consults. */
function DirtyDrafts() {
  useUnsavedChanges(true, "Discard your unsaved description?");
  return <p>drafts content</p>;
}
let draftsDirty = false;
vi.mock("./DescriptionDraftsScreen", () => ({
  DescriptionDraftsScreen: () => (draftsDirty ? <DirtyDrafts /> : <p>drafts content</p>),
}));

vi.mock("./DataDictionariesScreen", () => ({
  DataDictionariesScreen: () => <p>imports content</p>,
}));

/* Imported statically, and `vi.resetModules()` is deliberately NOT called in
   this file. Both halves of the behaviour under test -- the dirty reporter the
   stubbed Drafts tab registers, and the guard the tab bar asks -- have to be
   talking to the SAME `lib/unsavedChanges` module instance. Re-importing the
   workspace after a module reset gives it a second copy of that registry, the
   reporter and the reader stop seeing each other, and the test passes or fails
   for a reason that has nothing to do with the app. */
function mount(url = "/#/steward/worklist") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<DocumentationWorkspace />);
}

beforeEach(() => {
  draftsDirty = false;
  resetUnsavedRegistryForTests();
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetUnsavedRegistryForTests();
});

describe("the documentation workspace tab axis", () => {
  it("opens Priorities by default and keeps the canonical URL free of ?view=", async () => {
    mount();

    expect(await screen.findByText("priorities content")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Priorities" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(new URLSearchParams(location.search).get("view")).toBeNull();
  });

  it("puts the tab in the URL, so a tab is a link", async () => {
    mount();
    await screen.findByText("priorities content");

    fireEvent.click(screen.getByRole("tab", { name: "Imports" }));

    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBe("imports"));
    expect(await screen.findByText("imports content")).toBeInTheDocument();
    // …and back to the default drops the field rather than writing
    // `?view=priorities`, so one view does not have two spellings.
    fireEvent.click(screen.getByRole("tab", { name: "Priorities" }));
    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBeNull());
  });

  it("opens the tab a URL names, and says which scope that tab reads", async () => {
    mount("/?view=imports#/steward/worklist");

    expect(await screen.findByText("imports content")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Imports" })).toHaveAttribute("aria-selected", "true");
    // The two scope axes are a real difference between these surfaces, so the
    // workspace states which one is in front rather than leaving it to be
    // inferred from an empty list.
    expect(screen.getByText(/project selected in the scope picker/)).toBeInTheDocument();
  });

  it("falls back to Priorities for a ?view= value it does not know", async () => {
    mount("/?view=whatever#/steward/worklist");

    // A link from the future, or a typo. Either way the workspace opens.
    expect(await screen.findByText("priorities content")).toBeInTheDocument();
  });

  it("leaves the other tabs' own filters in the URL across a switch", async () => {
    mount("/?document=doc_7&ranking=query_volume#/steward/worklist");
    await screen.findByText("priorities content");

    fireEvent.click(screen.getByRole("tab", { name: "Imports" }));

    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBe("imports"));
    const params = new URLSearchParams(location.search);
    // Imports' selected document and Priorities' ranking both survive: the
    // fields do not collide, so keeping them is what makes coming back to a
    // tab return you to where you were.
    expect(params.get("document")).toBe("doc_7");
    expect(params.get("ranking")).toBe("query_volume");
  });
});

describe("an unsaved description is not discarded by a tab switch", () => {
  it("asks first, and stays put when the answer is no", async () => {
    draftsDirty = true;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount("/?view=drafts#/steward/worklist");
    await screen.findByText("drafts content");

    fireEvent.click(screen.getByRole("tab", { name: "Imports" }));

    expect(confirm).toHaveBeenCalledWith("Discard your unsaved description?");
    // Declining leaves the tab where it was -- URL included, or the address bar
    // would claim a tab the screen is not showing.
    expect(new URLSearchParams(location.search).get("view")).toBe("drafts");
    expect(screen.getByText("drafts content")).toBeInTheDocument();
  });

  it("switches when the answer is yes", async () => {
    draftsDirty = true;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/?view=drafts#/steward/worklist");
    await screen.findByText("drafts content");

    fireEvent.click(screen.getByRole("tab", { name: "Imports" }));

    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBe("imports"));
  });

  it("asks nothing when no draft is dirty", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/?view=drafts#/steward/worklist");
    await screen.findByText("drafts content");

    fireEvent.click(screen.getByRole("tab", { name: "Imports" }));

    expect(confirm).not.toHaveBeenCalled();
    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBe("imports"));
  });
});
