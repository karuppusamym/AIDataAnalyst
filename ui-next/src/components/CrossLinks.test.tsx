import { beforeEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CrossLinks } from "./CrossLinks";

/* ---------------------------------------------------------------------------
   CrossLinks.

   The regression this guards is the one that made the shell a set of islands:
   a link that changes the screen but drops the selection is worse than no
   link, because the person arrives on the target screen and has to search for
   the row they were already looking at. So the assertions are about the URL
   the click produces, not about the button rendering.
--------------------------------------------------------------------------- */

beforeEach(() => {
  history.replaceState(null, "", "/");
});

describe("CrossLinks", () => {
  it("writes the screen into the hash and the selection into the query string", () => {
    render(
      <CrossLinks
        links={[{ screen: "lineage", label: "Lineage", params: { ds: "ds_1", node: "t_1" } }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Lineage/ }));

    // The shell routes on the hash; every migrated screen reads its selection
    // through `useUrlState`, which reads `location.search`.
    expect(location.hash).toBe("#/lineage");
    const params = new URLSearchParams(location.search);
    expect(params.get("ds")).toBe("ds_1");
    expect(params.get("node")).toBe("t_1");
  });

  it("notifies the shell so the view changes without a reload", () => {
    let heard = 0;
    const listener = () => { heard += 1; };
    window.addEventListener("hashchange", listener);
    render(<CrossLinks links={[{ screen: "quality", label: "Quality" }]} />);

    fireEvent.click(screen.getByRole("button", { name: /Quality/ }));
    window.removeEventListener("hashchange", listener);

    // `history.pushState` alone fires no event, so the shell's hashchange
    // subscription would never run and the screen would not change.
    expect(heard).toBe(1);
  });

  it("replaces the previous selection rather than merging into it", () => {
    history.replaceState(null, "", "/?ds=ds_old&incident=inc_1");
    render(<CrossLinks links={[{ screen: "catalog", label: "Catalog", params: { asset: "t_2" } }]} />);

    fireEvent.click(screen.getByRole("button", { name: /Catalog/ }));

    // Carrying `incident=inc_1` onto the Catalog would leave a stale filter in
    // a shareable URL that the target screen does not understand.
    const params = new URLSearchParams(location.search);
    expect(params.get("asset")).toBe("t_2");
    expect(params.get("incident")).toBeNull();
    expect(params.get("ds")).toBeNull();
  });

  it("renders nothing at all when there is nothing to link to", () => {
    const { container } = render(<CrossLinks links={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
