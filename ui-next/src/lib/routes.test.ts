import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CONTEXT_FIELDS,
  RETIRED_SCREEN_ALIASES,
  SCREEN_IDS,
  SCREEN_JOURNEY,
  SCREEN_QUERY_FIELDS,
  buildLink,
  buildRelativeLink,
  canonicalPath,
  isScreenId,
  resolveHash,
  resolveScreenRef,
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
    expect(link).toBe("https://atlas.example/?asset=t_1#/analyst/catalog");
  });

  it("produces a link with no query when there is no selection", () => {
    expect(buildLink({ screen: "audit" }, BASE)).toBe("https://atlas.example/#/auditor/audit");
  });

  it("orders fields deterministically so the same target copies the same text", () => {
    const a = buildLink({ screen: "catalog", params: { q: "orders", asset: "t_1" } }, BASE);
    const b = buildLink({ screen: "catalog", params: { asset: "t_1", q: "orders" } }, BASE);
    expect(a).toBe(b);
    expect(a).toBe("https://atlas.example/?asset=t_1&q=orders#/analyst/catalog");
  });

  it("omits empty and null values rather than writing bare keys", () => {
    const link = buildLink(
      { screen: "catalog", params: { asset: "t_1", q: "", cert: null, type: undefined } },
      BASE,
    );
    expect(link).toBe("https://atlas.example/?asset=t_1#/analyst/catalog");
  });

  it("drops fields the target screen does not declare", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    // `incident` belongs to Data quality; on Catalog it is a stale filter in a
    // shareable URL that the target screen cannot act on.
    const link = buildLink({ screen: "catalog", params: { asset: "t_1", incident: "i_1" } }, BASE);
    expect(link).toBe("https://atlas.example/?asset=t_1#/analyst/catalog");
    warn.mockRestore();
  });

  it("carries estate context only to a screen that reads it", () => {
    const from = { ...BASE, search: "?ds=ds_1&severity=HIGH" };
    // Lineage and Catalog read `ds`; the screen-specific severity filter is dropped.
    expect(buildRelativeLink({ screen: "lineage", params: { node: "t_1" } }, from.search)).toBe(
      "?ds=ds_1&node=t_1#/analyst/lineage",
    );
    expect(buildRelativeLink({ screen: "catalog", params: { asset: "t_1" } }, from.search)).toBe(
      "?asset=t_1&ds=ds_1#/analyst/catalog",
    );
  });

  it("does not carry context when the caller opts out", () => {
    expect(
      buildRelativeLink(
        { screen: "lineage", params: { node: "t_1" }, inheritContext: false },
        "?ds=ds_1",
      ),
    ).toBe("?node=t_1#/analyst/lineage");
  });

  /* ---------------------------------------------------------------------------
     R11-FP12 (F08): the context product Ask asked through must survive being
     shared. `product` was undeclared for `analyst`, so `buildSearch` dropped it
     from every link the app built -- a permalink to "the answer this curated
     product gave" reopened as a question against the whole datasource.
  --------------------------------------------------------------------------- */

  it("keeps the context product in a link to Ask", () => {
    const link = buildLink(
      { screen: "analyst", params: { run: "run_1", product: "customer-revenue" } },
      BASE,
    );
    expect(link).toBe(
      "https://atlas.example/?product=customer-revenue&run=run_1#/analyst/analyst",
    );
  });

  it("does not inherit the context product across screens", () => {
    // `product` names a marketplace listing on Marketplace and a context
    // product on Ask. Carrying Ask's key there would put one screen's
    // identifier into another screen's differently-meant field, so it is
    // screen-local rather than estate context -- see SCREEN_QUERY_FIELDS.
    expect(buildRelativeLink({ screen: "marketplace" }, "?product=customer-revenue")).toBe(
      "#/consumer/marketplace",
    );
    expect(CONTEXT_FIELDS).not.toContain("product");
    // `normalizeLocation` (lib/location.ts) filters a pasted non-canonical URL
    // through this same declaration, which is what keeps the key on the link.
    expect(SCREEN_QUERY_FIELDS.analyst).toContain("product");
  });

  it("never carries an organization: a link is a request, not an authorization", () => {
    const declared = Object.values(SCREEN_QUERY_FIELDS).flat();
    expect(declared).not.toContain("organization_id");
    expect(declared).not.toContain("org");
  });
});

describe("screenFromHash", () => {
  it("resolves a known screen", () => {
    expect(screenFromHash("#/analyst/catalog")).toBe("catalog");
    expect(screenFromHash("#/catalog")).toBe("catalog");
    expect(screenFromHash("#catalog")).toBe("catalog");
  });

  it("falls back to Overview for an unknown or missing screen", () => {
    expect(screenFromHash("#/nope")).toBe("home");
    expect(screenFromHash("")).toBe("home");
  });
});

/* ---------------------------------------------------------------------------
   R11-S10 — EVERY ROUTE THAT RESOLVED BEFORE THIS ROW STILL RESOLVES.

   This is the row's own hard requirement, and it is the one thing here that a
   reader should be able to check exhaustively rather than by sampling. The
   flat form is generated from `SCREEN_IDS`, so a screen added later is covered
   without anyone remembering to add a case; the retired routes are listed by
   hand, because a route that no longer names a screen cannot be derived from
   anything.

   A 404 on a link somebody saved is the failure this row exists to avoid, so
   these assert `!== "home"` explicitly: falling back to Overview IS the 404
   here, and a test that only checked "it returns a screen" would pass on it.
--------------------------------------------------------------------------- */

describe("journey-grouped routes keep every old route working", () => {
  it("writes the journey into every link it builds", () => {
    for (const id of SCREEN_IDS) {
      const link = buildRelativeLink({ screen: id });
      expect(link).toBe(`#/${SCREEN_JOURNEY[id]}/${id}`);
    }
  });

  it("resolves the canonical grouped form for every screen", () => {
    for (const id of SCREEN_IDS) {
      const resolved = resolveHash(`#/${canonicalPath(id)}`);
      expect(resolved.screen).toBe(id);
      expect(resolved.canonical).toBe(true);
    }
  });

  it("still resolves the flat pre-R11-S10 form for every screen", () => {
    for (const id of SCREEN_IDS) {
      const resolved = resolveHash(`#/${id}`);
      expect(resolved.screen).toBe(id);
      // It works, and it is not the spelling we emit -- so the shell rewrites it.
      expect(resolved.canonical).toBe(false);
    }
  });

  it("still resolves a screen filed under a journey it has since left", () => {
    // `refusals` moved from Reviewer to Auditor in this row. A bookmark taken
    // the day before must not 404 on the grouping having changed.
    const resolved = resolveHash("#/reviewer/refusals");
    expect(resolved.screen).toBe("refusals");
    expect(resolved.canonical).toBe(false);
    expect(resolveHash("#/auditor/refusals").canonical).toBe(true);
  });

  it("resolves every route that was merged away, with the filter that reproduces it", () => {
    const retired: ReadonlyArray<readonly [string, string, Record<string, string>]> = [
      ["steward-agent", "task-agents", { agent: "steward" }],
      ["lineage-agent", "task-agents", { agent: "lineage" }],
      ["quality-agent", "task-agents", { agent: "quality" }],
      ["parsed-lineage-review", "governance", { queue: "parsed-lineage" }],
      // R11-S13 (M3): three steps of documenting an estate, one workspace.
      ["description-drafts", "worklist", { view: "drafts" }],
      ["data-dictionaries", "worklist", { view: "imports" }],
      // R11-S13 (M1): two routes over one lineage question, one destination.
      ["unified-lineage", "lineage", { view: "graph" }],
      // R11-S13 (items 15/17): scheduled bulk actions are the Automation view
      // of the one stewardship workspace.
      ["playbooks", "stewardship", { view: "automation" }],
    ];

    for (const [old, screen, params] of retired) {
      for (const hash of [`#/${old}`, `#/steward/${old}`, `#/reviewer/${old}`]) {
        const resolved = resolveHash(hash);
        expect(resolved.screen).toBe(screen);
        // Not Overview: a saved link to a merged screen opens the screen that
        // absorbed it, not the dashboard.
        expect(resolved.screen).not.toBe("home");
        expect(resolved.params).toEqual(params);
        expect(resolved.canonical).toBe(false);
      }
    }
  });

  /* R11-S13 (items 15/17): what the stewardship consolidation deliberately
     did NOT merge. Task agents (bounded agent execution), Documentation and
     Business Meaning (workspaces of their own), the review queue (maker-checker
     separation), and Relationships and Cross-source as TWO queues -- the M2
     decision: different scope axes, read models and write contracts. Each is
     linked to from where it is relevant; none is an alias of anything. */
  it("keeps the destinations the stewardship workspace links to as destinations", () => {
    for (const id of [
      "task-agents",
      "worklist",
      "meaning",
      "governance",
      "negative-knowledge",
      "relationships",
      "cross-source",
    ]) {
      expect(isScreenId(id), id).toBe(true);
      expect(RETIRED_SCREEN_ALIASES[id], id).toBeUndefined();
      expect(resolveHash(`#/${id}`).screen).toBe(id);
    }
  });

  it("every retired alias names a screen that still exists", () => {
    for (const alias of Object.values(RETIRED_SCREEN_ALIASES)) {
      expect(isScreenId(alias.screen)).toBe(true);
    }
  });

  /* -------------------------------------------------------------------------
     R11-S13 — an IN-APP link to a merged-away screen still opens it.

     `resolveHash` covers the pasted URL. This covers the other door:
     `navigateTo` and `App.navigate` both take a `string`, because
     `components/CrossLinks.tsx` is deliberately not a router, so the compiler
     cannot catch a link that names a screen which has since been merged away.
     The old check was `isScreenId`, which answers "is this LIVE" -- and under
     it `EvidencePane`'s "Impact" cross-link, which names `unified-lineage`,
     landed on Overview.
  ------------------------------------------------------------------------- */
  it("resolves a merged-away screen id handed over as a plain string", () => {
    for (const [id, alias] of Object.entries(RETIRED_SCREEN_ALIASES)) {
      const resolved = resolveScreenRef(id);
      expect(resolved).not.toBeNull();
      expect(resolved!.screen).toBe(alias.screen);
      // Not Overview: the whole point of the table.
      expect(resolved!.screen).not.toBe("home");
      expect(resolved!.params).toEqual(alias.params);
    }
  });

  it("resolves a live screen id unchanged, and an invented one to null", () => {
    for (const id of SCREEN_IDS) {
      expect(resolveScreenRef(id)).toEqual({ screen: id });
    }
    // A caller bug, and reported as one rather than silently opening something.
    expect(resolveScreenRef("catalogue")).toBeNull();
  });

  it("keeps a retired route's own filters alongside the ones it implies", () => {
    // `#/parsed-lineage-review?type=ROUTINE` was a real, linkable view: the
    // lineage agent built exactly this link for a procedure proposal.
    const resolved = resolveHash("#/parsed-lineage-review");
    expect(resolved.screen).toBe("governance");
    // `type` survives because the merged screen declares it -- see
    // `SCREEN_QUERY_FIELDS.governance`, and `normalizeLocation` in
    // `lib/location.ts`, which is what folds the two together.
    expect(SCREEN_QUERY_FIELDS.governance).toContain("type");
    expect(SCREEN_QUERY_FIELDS.governance).toContain("queue");
  });

  /* -------------------------------------------------------------------------
     R11-S13 — the field every retired route implies must be DECLARED by the
     screen that absorbed it.

     This is the quiet half of a merge. `normalizeLocation` filters a pasted
     link through `allowedFieldsFor(target)` before it rewrites it, so an alias
     that implies `view=drafts` onto a screen which does not declare `view`
     resolves to the right screen and then drops the very field that chose the
     tab -- the bookmark opens the workspace's default view and nothing warns.
     Asserted over the whole table rather than per merge, so the next merge
     inherits the check.
  ------------------------------------------------------------------------- */
  it("declares every field a retired route's alias implies", () => {
    for (const [route, alias] of Object.entries(RETIRED_SCREEN_ALIASES)) {
      const declared = SCREEN_QUERY_FIELDS[alias.screen];
      for (const field of Object.keys(alias.params ?? {})) {
        expect(
          declared,
          `#/${route} implies "${field}", which screen "${alias.screen}" does not declare`,
        ).toContain(field);
      }
    }
  });

  it("gives every screen a journey", () => {
    const missing = SCREEN_IDS.filter((id) => !SCREEN_JOURNEY[id]);
    expect(missing).toEqual([]);
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
