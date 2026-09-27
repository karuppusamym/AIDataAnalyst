import { beforeEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";

import { normalizeLocation, patchQuery, pushLocation, replaceLocation } from "./location";
import { useAppLocation, useUrlState } from "./useUrlState";

/* ---------------------------------------------------------------------------
   The regressions these guard are F09's three symptoms, each of which came
   from `useUrlState` keeping a private copy of the query string and
   subscribing to nothing:

     - Back/Forward moved the URL and left the screen behind,
     - a same-screen link changed only the query, so the shell (keyed on the
       route id) never remounted and the hook never re-read,
     - `navigateTo` shouted `hashchange`, which the shell heard and the hook
       did not.
--------------------------------------------------------------------------- */

function Probe() {
  const location_ = useAppLocation();
  const [params] = useUrlState();
  return (
    <output data-testid="probe">
      {location_.screen}|{params.get("asset") ?? "-"}|{params.get("q") ?? "-"}
    </output>
  );
}

function probeText(): string {
  return screen.getByTestId("probe").textContent ?? "";
}

beforeEach(() => {
  history.replaceState(null, "", "/#/catalog");
});

describe("the location store", () => {
  it("renders what the URL says on first paint", () => {
    history.replaceState(null, "", "/?asset=t_1#/catalog");
    render(<Probe />);
    expect(probeText()).toBe("catalog|t_1|-");
  });

  it("re-renders when a same-screen link changes only the selection", () => {
    history.replaceState(null, "", "/?asset=t_1#/catalog");
    render(<Probe />);
    act(() => pushLocation({ screen: "catalog", params: { asset: "t_2" } }));
    expect(probeText()).toBe("catalog|t_2|-");
  });

  it("re-renders on Back", () => {
    history.replaceState(null, "", "/?asset=t_1#/catalog");
    render(<Probe />);
    act(() => pushLocation({ screen: "catalog", params: { asset: "t_2" } }));
    expect(probeText()).toBe("catalog|t_2|-");

    // jsdom's history does not run the popstate task itself; dispatching the
    // event after rewriting the URL is how the browser's behaviour is modelled.
    act(() => {
      history.replaceState(null, "", "/?asset=t_1#/catalog");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(probeText()).toBe("catalog|t_1|-");
  });

  it("re-renders when the screen changes", () => {
    render(<Probe />);
    act(() => pushLocation({ screen: "audit" }));
    expect(probeText()).toBe("audit|-|-");
  });

  it("merges a filter patch without touching the screen", () => {
    history.replaceState(null, "", "/?asset=t_1#/catalog");
    render(<Probe />);
    act(() => patchQuery({ q: "orders" }));
    expect(probeText()).toBe("catalog|t_1|orders");
    expect(location.hash).toBe("#/analyst/catalog");
  });

  it("removes a field when the patch value is null or empty", () => {
    history.replaceState(null, "", "/?asset=t_1&q=orders#/catalog");
    render(<Probe />);
    act(() => patchQuery({ q: null }));
    expect(probeText()).toBe("catalog|t_1|-");
    act(() => patchQuery({ asset: "" }));
    expect(probeText()).toBe("catalog|-|-");
  });

  it("keeps a field the route table does not declare when patching in place", () => {
    // A same-screen merge writes that screen's own fields; dropping one here
    // would blank a control the user is typing into. The allow-list belongs
    // to link building, not to filter editing.
    history.replaceState(null, "", "/#/catalog");
    render(<Probe />);
    act(() => patchQuery({ unlisted: "kept" }));
    expect(new URLSearchParams(location.search).get("unlisted")).toBe("kept");
  });

  it("uses replaceState for filter edits so Back does not eat keystrokes", () => {
    render(<Probe />);
    const before = history.length;
    act(() => patchQuery({ q: "o" }));
    act(() => patchQuery({ q: "or" }));
    act(() => patchQuery({ q: "ord" }));
    expect(history.length).toBe(before);
  });

  it("does not push a duplicate entry for the location already shown", () => {
    history.replaceState(null, "", "/?asset=t_1#/analyst/catalog");
    render(<Probe />);
    const before = history.length;
    act(() => pushLocation({ screen: "catalog", params: { asset: "t_1" } }));
    expect(history.length).toBe(before);
  });

  /* R11-S10: the same location, spelled the old way. Someone sitting on a
     saved `#/catalog` who clicks the nav item for the screen they are already
     on has not navigated, and must not be given a history entry that the Back
     button then has to eat -- the guard compares what a URL RESOLVES to, not
     the characters it is written with. */
  it("recognises the pre-grouping spelling as the location already shown", () => {
    history.replaceState(null, "", "/?asset=t_1#/catalog");
    render(<Probe />);
    const before = history.length;
    act(() => pushLocation({ screen: "catalog", params: { asset: "t_1" } }));
    expect(history.length).toBe(before);
  });

  it("replaceLocation changes the view without a history entry", () => {
    render(<Probe />);
    const before = history.length;
    act(() => replaceLocation({ screen: "audit" }));
    expect(probeText()).toBe("audit|-|-");
    expect(history.length).toBe(before);
  });
});

/* ---------------------------------------------------------------------------
   R11-S10 — normalizing a URL that resolved through an alias.

   The route table's own tests prove every old route still RESOLVES. These
   prove the other half: that arriving through one rewrites the address bar to
   the current spelling, keeps the filters the old link carried, folds in the
   ones a retired route implies, and does not cost the user a Back press.
--------------------------------------------------------------------------- */

describe("normalizing an aliased URL", () => {
  it("rewrites the flat spelling to the grouped one without a history entry", () => {
    history.replaceState(null, "", "/?asset=t_1#/catalog");
    render(<Probe />);
    const before = history.length;

    act(() => {
      normalizeLocation();
    });

    expect(location.hash).toBe("#/analyst/catalog");
    // The selection the old link carried survives the rewrite.
    expect(new URLSearchParams(location.search).get("asset")).toBe("t_1");
    expect(history.length).toBe(before);
    expect(probeText()).toBe("catalog|t_1|-");
  });

  it("opens a merged-away route on the screen that absorbed it, with its filter", () => {
    history.replaceState(null, "", "/#/steward-agent");
    render(<Probe />);

    act(() => {
      normalizeLocation();
    });

    expect(location.hash).toBe("#/steward/task-agents");
    expect(new URLSearchParams(location.search).get("agent")).toBe("steward");
  });

  it("keeps a retired route's own filters beside the ones it implies", () => {
    // The lineage agent built exactly this link for a procedure proposal.
    history.replaceState(null, "", "/?type=ROUTINE#/parsed-lineage-review");
    render(<Probe />);

    act(() => {
      normalizeLocation();
    });

    expect(location.hash).toBe("#/reviewer/governance");
    const params = new URLSearchParams(location.search);
    expect(params.get("queue")).toBe("parsed-lineage");
    expect(params.get("type")).toBe("ROUTINE");
  });

  it("leaves an already-canonical URL alone", () => {
    history.replaceState(null, "", "/?asset=t_1#/analyst/catalog");
    render(<Probe />);

    let rewrote = true;
    act(() => {
      rewrote = normalizeLocation();
    });

    expect(rewrote).toBe(false);
    expect(location.hash).toBe("#/analyst/catalog");
  });

  it("leaves a URL naming no screen alone, so persona landing still happens", () => {
    /* The shell lands a fresh session in the persona's own work area, and its
       test for that is "the hash names no screen". Writing `#/inbox/home` over
       an empty hash here would make that landing unreachable. */
    history.replaceState(null, "", "/");
    render(<Probe />);

    let rewrote = true;
    act(() => {
      rewrote = normalizeLocation();
    });

    expect(rewrote).toBe(false);
    expect(location.hash).toBe("");
  });
});
