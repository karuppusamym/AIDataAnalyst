import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  SCREEN_IDS,
  SCREEN_QUERY_FIELDS,
  buildLink,
  buildRelativeLink,
  isScreenId,
  screenFromHash,
} from "./routes";

/* ---------------------------------------------------------------------------
   The regression these guard is F08: copy actions across the app built
   `origin + pathname + '?' + selection` and left the screen out, so every
   pasted link landed on Overview instead of the object it named.
--------------------------------------------------------------------------- */

const BASE = { origin: "https://atlas.example", pathname: "/", search: "" };

beforeEach(() => {
  history.replaceState(null, "", "/");
});

describe("buildLink", () => {
  it("always writes the screen into the link", () => {
    const link = buildLink({ screen: "catalog", params: { asset: "t_1" } }, BASE);
    expect(link).toBe("https://atlas.example/?asset=t_1#/catalog");
  });

  it("produces a link with no query when there is no selection", () => {
    expect(buildLink({ screen: "audit" }, BASE)).toBe("https://atlas.example/#/audit");
  });

  it("orders fields deterministically so the same target copies the same text", () => {
    const a = buildLink({ screen: "catalog", params: { q: "orders", asset: "t_1" } }, BASE);
    const b = buildLink({ screen: "catalog", params: { asset: "t_1", q: "orders" } }, BASE);
    expect(a).toBe(b);
    expect(a).toBe("https://atlas.example/?asset=t_1&q=orders#/catalog");
  });

  it("omits empty and null values rather than writing bare keys", () => {
    const link = buildLink(
      { screen: "catalog", params: { asset: "t_1", q: "", cert: null, type: undefined } },
      BASE,
    );
    expect(link).toBe("https://atlas.example/?asset=t_1#/catalog");
  });

  it("drops fields the target screen does not declare", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    // `incident` belongs to Data quality; on Catalog it is a stale filter in a
    // shareable URL that the target screen cannot act on.
    const link = buildLink({ screen: "catalog", params: { asset: "t_1", incident: "i_1" } }, BASE);
    expect(link).toBe("https://atlas.example/?asset=t_1#/catalog");
    warn.mockRestore();
  });

  it("carries estate context only to a screen that reads it", () => {
    const from = { ...BASE, search: "?ds=ds_1&severity=HIGH" };
    // Lineage and Catalog read `ds`; the screen-specific severity filter is dropped.
    expect(buildRelativeLink({ screen: "lineage", params: { node: "t_1" } }, from.search)).toBe(
      "?ds=ds_1&node=t_1#/lineage",
    );
    expect(buildRelativeLink({ screen: "catalog", params: { asset: "t_1" } }, from.search)).toBe(
      "?asset=t_1&ds=ds_1#/catalog",
    );
  });

  it("does not carry context when the caller opts out", () => {
    expect(
      buildRelativeLink(
        { screen: "lineage", params: { node: "t_1" }, inheritContext: false },
        "?ds=ds_1",
      ),
    ).toBe("?node=t_1#/lineage");
  });

  it("never carries an organization: a link is a request, not an authorization", () => {
    const declared = Object.values(SCREEN_QUERY_FIELDS).flat();
    expect(declared).not.toContain("organization_id");
    expect(declared).not.toContain("org");
  });
});

describe("screenFromHash", () => {
  it("resolves a known screen", () => {
    expect(screenFromHash("#/catalog")).toBe("catalog");
    expect(screenFromHash("#catalog")).toBe("catalog");
  });

  it("falls back to Overview for an unknown or missing screen", () => {
    expect(screenFromHash("#/nope")).toBe("home");
    expect(screenFromHash("")).toBe("home");
  });
});

describe("the route table", () => {
  it("declares query fields for every screen", () => {
    const undeclared = SCREEN_IDS.filter((id) => SCREEN_QUERY_FIELDS[id] === undefined);
    expect(undeclared).toEqual([]);
  });

  it("recognises exactly the declared screens", () => {
    expect(isScreenId("catalog")).toBe(true);
    expect(isScreenId("catalogue")).toBe(false);
    expect(isScreenId(null)).toBe(false);
  });
});
