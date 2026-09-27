import { beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { resetLocationCacheForTests } from "../lib/location";
import {
  LINEAGE_VIEW_ALIASES,
  LineageWorkspace,
  lineageViewFrom,
} from "./LineageWorkspace";

/* ---------------------------------------------------------------------------
   R11-S13 (M1) — the lineage workspace SHELL.

   Both views are the screens that already existed and keep their own test
   files. What is only true of the merge is here:

     * `?view=` selects the view, so each of the three is a link;
     * EVERY SPELLING `#/lineage?view=` HAS EVER ANSWERED TO still resolves --
       this is the half a merge gets wrong quietly, because a link that lands
       on the default view looks like a working link;
     * the question (`ds`, `node`, `depth`, `direction`, `scope`, `dom`, `tab`)
       survives a view change, which is what makes these three views of one
       question rather than three screens sharing a URL;
     * Graph and Impact are ONE mounted component, so switching between them
       does not re-fetch the estate.
--------------------------------------------------------------------------- */

/* The two views are stubbed, because a test about the shell that mounts an
   869-line graph screen is a test about that screen's mocks. `unifiedMounts`
   counts MOUNTS rather than renders, so "switching Graph<->Impact does not
   remount" is measured rather than inferred from the markup. */
let unifiedMounts = 0;
let lastLead: string | undefined;
vi.mock("./NarratedLineageScreen", () => ({
  NarratedLineageScreen: () => <p>explain content</p>,
}));
vi.mock("./UnifiedLineageScreen", () => ({
  UnifiedLineageScreen: ({ lead }: { lead?: string }) => {
    lastLead = lead;
    useEffect(() => {
      unifiedMounts += 1;
    }, []);
    return <p>unified content, lead={lead}</p>;
  },
}));

function mount(url = "/#/analyst/lineage") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<LineageWorkspace />);
}

beforeEach(() => {
  unifiedMounts = 0;
  lastLead = undefined;
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

describe("every ?view= spelling the lineage route has answered to", () => {
  it("maps the legacy values onto the merged axis", () => {
    // The page `#/lineage` always opened.
    expect(lineageViewFrom(null)).toBe("explain");
    expect(lineageViewFrom("")).toBe("explain");
    // The two values the retired two-tab bar wrote.
    expect(lineageViewFrom("narrated")).toBe("explain");
    expect(lineageViewFrom("graph")).toBe("graph");
    // The merged axis itself.
    expect(lineageViewFrom("explain")).toBe("explain");
    expect(lineageViewFrom("impact")).toBe("impact");
    // A link from the future, or a typo: it is still a link to lineage.
    expect(lineageViewFrom("whatever")).toBe("explain");
  });

  it("keeps every legacy value in the alias table", () => {
    // Nothing is removed from this table, for the same reason nothing is
    // removed from `RETIRED_SCREEN_ALIASES`.
    expect(Object.keys(LINEAGE_VIEW_ALIASES).sort()).toEqual(["graph", "narrated"]);
  });
});

describe("the lineage workspace view axis", () => {
  it("opens Explain by default and keeps the canonical URL free of ?view=", async () => {
    mount();

    expect(await screen.findByText("explain content")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Explain" })).toHaveAttribute("aria-selected", "true");
    expect(new URLSearchParams(location.search).get("view")).toBeNull();
  });

  it("opens the merged graph for the legacy ?view=graph spelling", async () => {
    mount("/?view=graph#/analyst/lineage");

    expect(await screen.findByText(/unified content/)).toBeInTheDocument();
    expect(lastLead).toBe("graph");
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
  });

  it("leads with impact on ?view=impact, over the same component", async () => {
    mount("/?view=impact#/analyst/lineage");

    expect(await screen.findByText(/unified content/)).toBeInTheDocument();
    // Same screen, different emphasis -- not a second graph, a second endpoint
    // or a second permission contract.
    expect(lastLead).toBe("impact");
    expect(screen.getByRole("tab", { name: "Impact" })).toHaveAttribute("aria-selected", "true");
  });

  it("carries the whole question across a view change", async () => {
    mount("/?ds=ds_1&node=n_7&depth=3&direction=downstream&scope=domain&dom=dom_1&tab=edges#/analyst/lineage");
    await screen.findByText("explain content");

    fireEvent.click(screen.getByRole("tab", { name: "Impact" }));

    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBe("impact"));
    const params = new URLSearchParams(location.search);
    // The asset, the depth, the direction, the scope and the graph sub-tab are
    // the QUESTION. Only `view` is which answer is in front.
    expect(params.get("ds")).toBe("ds_1");
    expect(params.get("node")).toBe("n_7");
    expect(params.get("depth")).toBe("3");
    expect(params.get("direction")).toBe("downstream");
    expect(params.get("scope")).toBe("domain");
    expect(params.get("dom")).toBe("dom_1");
    expect(params.get("tab")).toBe("edges");
  });

  it("drops ?view= going back to the default, so one view has one spelling", async () => {
    mount("/?view=impact#/analyst/lineage");
    await screen.findByText(/unified content/);

    fireEvent.click(screen.getByRole("tab", { name: "Explain" }));

    await waitFor(() => expect(new URLSearchParams(location.search).get("view")).toBeNull());
    expect(await screen.findByText("explain content")).toBeInTheDocument();
  });

  it("switches Graph to Impact without remounting the graph", async () => {
    mount("/?view=graph#/analyst/lineage");
    await screen.findByText(/unified content/);
    const before = screen.getByText(/unified content/);

    fireEvent.click(screen.getByRole("tab", { name: "Impact" }));

    await waitFor(() => expect(lastLead).toBe("impact"));
    /* The same DOM node, re-rendered rather than replaced: React reconciled
       one element type across the two views. A remount here would re-fetch the
       whole estate graph and throw away the layer chips and asset filters just
       to change which pane leads. */
    expect(screen.getByText(/unified content/)).toBe(before);
    expect(unifiedMounts).toBe(1);
  });

  it("does remount when the view really is a different screen", async () => {
    mount("/?view=graph#/analyst/lineage");
    await screen.findByText(/unified content/);
    expect(unifiedMounts).toBe(1);

    fireEvent.click(screen.getByRole("tab", { name: "Explain" }));
    expect(await screen.findByText("explain content")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Graph" }));
    // Explain is a different screen, so coming back is a real mount. The
    // reconciliation above is specific to Graph<->Impact, which is one screen.
    await waitFor(() => expect(unifiedMounts).toBe(2));
  });
});
