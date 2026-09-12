/* ---------------------------------------------------------------------------
   The route table and the one link builder (review 2026-09-05, F08 · T11).

   THE DEFECT this module exists to remove: a screen is selected by the hash
   (`#/catalog`), but the copy-link actions across the app built their URLs as
   `origin + pathname + '?' + selection`. The screen was simply missing. Paste
   that link into a fresh tab and you land on Overview -- or on whatever the
   persona default is -- with the selection query sitting in the URL, read by
   nobody. Sharing "the row I am looking at" was broken on every screen that
   offered it, and so was reopening your own work from a bookmark.

   THE INVARIANT: a link to an object in Atlas is built in exactly one place,
   and that place always writes the screen. `buildLink` is that place. If a
   caller cannot express a link through it, the route table is missing a field
   -- add the field, do not hand-roll another `location.origin + ...`.

   THE SECOND INVARIANT: a URL is a *request*, never an authorization. Every
   field below is a hint about what to show. The backend decides whether this
   principal may see it, in this organization, on every request. Nothing here
   is trusted; `organization_id` deliberately is NOT a link field, because a
   link that carried its own tenant would look like one.

   WHY FIELDS ARE DECLARED PER SCREEN. The shell used to carry the previous
   page's whole query string into the next screen (`App.tsx`'s `navigate`
   defaulted to `location.search`), so a filter set on Catalog arrived on
   Quality, where a different screen read `status` to mean something else.
   Declaring the fields each screen owns means a screen change drops filters
   that belong to the screen you left, and keeps only the estate context you
   are working in (`CONTEXT_FIELDS`).
--------------------------------------------------------------------------- */

/** Every screen the shell can render. The hash names one of these. */
export const SCREEN_IDS = [
  "home",
  "inbox",
  "analyst",
  "catalog",
  "semantics",
  "tools",
  "tool-plans",
  "lineage",
  "unified-lineage",
  "marketplace",
  "context",
  "developer",
  "portfolio-analytics",
  "stewardship",
  "worklist",
  "task-agents",
  "playbooks",
  "negative-knowledge",
  "meaning",
  "description-drafts",
  "data-dictionaries",
  "relationships",
  "cross-source",
  "transformations",
  "quality",
  "studio",
  "governance",
  "refusals",
  "reviewer-agent",
  "sources",
  "operations",
  "agents",
  "ai",
  "agent-roster",
  "access-policies",
  "workspace-access",
  "delegations",
  "reliability",
  "administration",
  "audit",
  "compliance",
] as const;

export type ScreenId = (typeof SCREEN_IDS)[number];

const SCREEN_ID_SET: ReadonlySet<string> = new Set<string>(SCREEN_IDS);

/** The screen a URL with no recognised hash resolves to. */
export const DEFAULT_SCREEN: ScreenId = "home";

export function isScreenId(value: string | null | undefined): value is ScreenId {
  return typeof value === "string" && SCREEN_ID_SET.has(value);
}

/* ---------------------------------------------------------------------------
   JOURNEYS — R11-S10.

   The shell had grouped its *sidebar* by work area since UX-20, but the route
   table underneath stayed flat: forty-four sibling ids, `#/catalog` next to
   `#/reviewer-agent`, with nothing in the URL saying which of them are the
   same job. A person landing on a pasted link could not tell what
   neighbourhood they were in, and the grouping existed only for as long as
   the sidebar was on screen.

   The journey is now a property of the ROUTE, not of the navigation
   component: `#/analyst/catalog`. `App.tsx`'s nav reads its groups from this
   table rather than declaring its own copy, so the sidebar and the URL cannot
   disagree about which job a screen belongs to.

   The slugs are `lib/workAreas.ts`'s `WORK_AREAS`, lowercased. That file
   still owns the display labels and the persona landing map; this one owns
   the path segment, because the route table is what has to parse it.
--------------------------------------------------------------------------- */

export const JOURNEYS = [
  "inbox",
  "analyst",
  "consumer",
  "developer",
  "steward",
  "reviewer",
  "operator",
  "auditor",
] as const;

export type Journey = (typeof JOURNEYS)[number];

/** The journey each screen belongs to. Exhaustive by construction: a new
 *  screen id without an entry here is a compile error, not a screen that
 *  quietly lands in whatever group is first. */
export const SCREEN_JOURNEY: Record<ScreenId, Journey> = {
  home: "inbox",
  inbox: "inbox",
  analyst: "analyst",
  catalog: "analyst",
  semantics: "analyst",
  tools: "analyst",
  "tool-plans": "analyst",
  lineage: "analyst",
  "unified-lineage": "analyst",
  marketplace: "consumer",
  "portfolio-analytics": "consumer",
  context: "developer",
  developer: "developer",
  stewardship: "steward",
  worklist: "steward",
  "task-agents": "steward",
  playbooks: "steward",
  "negative-knowledge": "steward",
  meaning: "steward",
  "description-drafts": "steward",
  "data-dictionaries": "steward",
  relationships: "steward",
  "cross-source": "steward",
  transformations: "steward",
  quality: "steward",
  studio: "steward",
  governance: "reviewer",
  "reviewer-agent": "reviewer",
  sources: "operator",
  operations: "operator",
  agents: "operator",
  ai: "operator",
  "agent-roster": "operator",
  "access-policies": "operator",
  "workspace-access": "operator",
  delegations: "operator",
  reliability: "operator",
  administration: "operator",
  audit: "auditor",
  refusals: "auditor",
  compliance: "auditor",
};

/** `analyst/catalog` — the canonical path segment pair for a screen. */
export function canonicalPath(screen: ScreenId): string {
  return `${SCREEN_JOURNEY[screen]}/${screen}`;
}

/* ---------------------------------------------------------------------------
   ALIASES — every route that resolved before this row still resolves.

   Two kinds live here:

     1. The flat `#/<screen>` form every link in the wild uses. Handled
        structurally by `resolveHash` rather than listed, so a screen added
        tomorrow is aliased the day it exists.

     2. Screens that were MERGED AWAY. These cannot be resolved structurally
        -- `#/steward-agent` names a screen that no longer exists -- so each
        maps to the screen that absorbed it plus the filter that reproduces
        what the old route showed. A bookmark to the steward agent's console
        opens the merged console with the steward agent selected, which is the
        page the person bookmarked.

   Nothing is ever removed from this table. A 404 on a link someone saved is
   the failure this row exists to avoid.
--------------------------------------------------------------------------- */

export interface ScreenAlias {
  readonly screen: ScreenId;
  /** Filters that reproduce what the retired route used to show. */
  readonly params?: Readonly<Record<string, string>>;
}

export const RETIRED_SCREEN_ALIASES: Readonly<Record<string, ScreenAlias>> = {
  // R11-S10: three task-agent consoles were one component behind three routes.
  "steward-agent": { screen: "task-agents", params: { agent: "steward" } },
  "lineage-agent": { screen: "task-agents", params: { agent: "lineage" } },
  "quality-agent": { screen: "task-agents", params: { agent: "quality" } },
  // R11-S10: the parsed-lineage queue is now a queue OF the review surface.
  "parsed-lineage-review": { screen: "governance", params: { queue: "parsed-lineage" } },
};

/**
 * Estate context that MAY survive a screen change.
 *
 * These name *where in the estate you are working* rather than *what you
 * filtered on this page*. Moving from Data quality to Lineage should keep the
 * datasource you were looking at; it should not keep Data quality's severity
 * filter.
 *
 * Inheritance is conditional, not automatic: a context field is carried only
 * when the TARGET screen declares that it reads it. Catalog does not read
 * `ds`, so arriving there with `?ds=…` would leave a field in a shareable URL
 * that the screen it points at does not understand -- which is the same class
 * of defect as the shell carrying the previous page's whole query string.
 */
export const CONTEXT_FIELDS = ["ds", "project", "dom"] as const;

/**
 * The query fields each screen reads, including any of `CONTEXT_FIELDS`.
 *
 * Derived from what each screen actually reads out of the query string. A
 * screen missing from this table declares nothing: `buildLink` passes its
 * params through unchanged and warns in development. Add the screen rather
 * than relying on that -- an undeclared field is a field nobody can tell you
 * is misspelled.
 */
export const SCREEN_QUERY_FIELDS: Partial<Record<ScreenId, readonly string[]>> = {
  /* T15: Overview reads `ds` so first-source setup resumes on the source you
     were actually setting up. It is a CONTEXT field, so it is also inherited
     when you come back here from Sources or Operations -- which is the point:
     returning to the landing page should not silently switch which source the
     setup steps are describing. */
  home: ["ds"],
  inbox: ["persona"],
  analyst: ["ds", "run"],
  catalog: ["asset", "cert", "ds", "q", "type"],
  semantics: ["metric", "model", "project"],
  tools: ["project", "status", "tool"],
  "tool-plans": ["plan"],
  lineage: ["depth", "ds", "node", "view"],
  "unified-lineage": ["dom", "ds", "node", "scope", "tab"],
  marketplace: ["class", "domain", "product", "q", "sort"],
  context: ["project"],
  developer: ["project", "tab"],
  "portfolio-analytics": ["window"],
  stewardship: ["action", "ds", "field", "pattern"],
  worklist: ["ranking", "zero"],
  /* R11-S10: `agent` is which task agent's console is open. It replaces the
     three routes that differed only by that value. */
  "task-agents": ["agent"],
  playbooks: [],
  "negative-knowledge": ["assertion_type", "subject", "suppression"],
  meaning: ["asset", "ds", "node", "q", "view"],
  "description-drafts": ["focus", "type"],
  "data-dictionaries": ["document"],
  relationships: ["candidate", "ds"],
  "cross-source": ["dom", "status"],
  transformations: ["dbtProject", "import", "match", "project", "resource", "type"],
  quality: ["ds", "incident", "severity", "status"],
  studio: ["cs", "status"],
  /* R11-S10: `queue` chooses which review queue is in front of the reviewer.
     `review`, `status` and `type` mean the same thing in both queues -- the
     focused item, its state, and its object/edge type -- which is why the
     merge did not need a second set of field names. */
  governance: ["queue", "review", "status", "type"],
  refusals: ["run"],
  "reviewer-agent": ["offset", "outcome", "window"],
  sources: ["q", "source", "status"],
  operations: ["batch_ds", "ds", "outbox_status", "run_status"],
  agents: ["ai"],
  ai: ["ai"],
  "agent-roster": ["window"],
  "access-policies": [],
  "workspace-access": [],
  delegations: ["delegate", "delegator", "status"],
  reliability: ["contract"],
  administration: [],
  audit: ["action", "correlation_id", "event", "resource_type", "since", "until"],
  compliance: [],
};

/** The fields a screen may carry, or `null` when the screen declares none. */
export function allowedFieldsFor(screen: ScreenId): readonly string[] | null {
  return SCREEN_QUERY_FIELDS[screen] ?? null;
}

export interface LinkTarget {
  /** The screen to open. Always written into the link. */
  screen: ScreenId;
  /** Selection and filters. `null`/`undefined`/`""` values are omitted. */
  params?: Readonly<Record<string, string | number | null | undefined>>;
  /**
   * Carry the estate context (`CONTEXT_FIELDS`) from the current URL when the
   * caller did not supply it. Defaults to true: a link built from a screen
   * that is already scoped to a datasource should stay in that datasource.
   */
  inheritContext?: boolean;
}

function warnUndeclared(screen: ScreenId, dropped: readonly string[]): void {
  if (dropped.length === 0) return;
  if (typeof import.meta !== "undefined" && import.meta.env?.DEV) {
    // Not an exception: a mistyped field must not blank a user's screen. It
    // must be loud in development and inert in production.
    console.warn(
      `buildLink: dropped field(s) not declared for screen "${screen}": ${dropped.join(", ")}. ` +
        "Add them to SCREEN_QUERY_FIELDS in lib/routes.ts.",
    );
  }
}

/**
 * The query string for a target, with undeclared fields removed.
 *
 * Exported for the location store, which needs the same filtering when the
 * shell navigates between screens.
 */
export function buildSearch(
  target: LinkTarget,
  currentSearch: string | URLSearchParams = "",
): URLSearchParams {
  const current =
    typeof currentSearch === "string" ? new URLSearchParams(currentSearch) : currentSearch;
  const next = new URLSearchParams();

  const dropped: string[] = [];
  const allowed = allowedFieldsFor(target.screen);

  if (target.inheritContext !== false) {
    for (const field of CONTEXT_FIELDS) {
      // Only inherit what the target actually reads. See CONTEXT_FIELDS.
      if (!allowed?.includes(field)) continue;
      const value = current.get(field);
      if (value) next.set(field, value);
    }
  }

  for (const [key, raw] of Object.entries(target.params ?? {})) {
    if (raw === null || raw === undefined || raw === "") {
      next.delete(key);
      continue;
    }
    if (allowed && !allowed.includes(key)) {
      dropped.push(key);
      continue;
    }
    next.set(key, String(raw));
  }
  warnUndeclared(target.screen, dropped);

  // Stable ordering so the same target always produces the same string --
  // two people pasting "the same link" should paste identical text.
  const sorted = new URLSearchParams();
  for (const key of [...next.keys()].sort()) {
    const value = next.get(key);
    if (value !== null) sorted.set(key, value);
  }
  return sorted;
}

/**
 * `?a=1&b=2#/journey/screen`, or `#/journey/screen` when there is nothing to
 * carry.
 *
 * R11-S10: the journey segment is written by this one function, so every link
 * the app builds is canonical without a single call site knowing which group a
 * screen is in. The flat `#/screen` form those links used to have still
 * resolves -- see `resolveHash` -- it is simply no longer what we emit.
 */
export function buildRelativeLink(
  target: LinkTarget,
  currentSearch: string | URLSearchParams = "",
): string {
  const query = buildSearch(target, currentSearch).toString();
  return `${query ? `?${query}` : ""}#/${canonicalPath(target.screen)}`;
}

/**
 * The canonical, shareable, absolute URL for a target.
 *
 * This is what every "Copy link" button in the app must call. It always
 * contains the screen, so the link reopens the object rather than the
 * persona's default page.
 */
export function buildLink(target: LinkTarget, base?: { origin: string; pathname: string; search: string }): string {
  const location_ = base ?? {
    origin: window.location.origin,
    pathname: window.location.pathname,
    search: window.location.search,
  };
  return `${location_.origin}${location_.pathname}${buildRelativeLink(target, location_.search)}`;
}

/**
 * What a hash resolves to, and whether it was the canonical spelling.
 *
 * `canonical: false` means the URL still works but is not the form the app
 * writes -- a flat `#/catalog`, a wrong journey segment, or a route that was
 * merged away. The shell rewrites those in place (`replaceState`, so Back
 * still leaves the app) rather than leaving two spellings of one screen in
 * circulation.
 */
export interface ResolvedHash {
  readonly screen: ScreenId;
  readonly canonical: boolean;
  /** Filters a retired route implies. Empty for every live route. */
  readonly params?: Readonly<Record<string, string>>;
}

/**
 * Resolve a hash to a screen, accepting every spelling that has ever worked.
 *
 *   `#/analyst/catalog`  the canonical form
 *   `#/catalog`          the flat form every pre-R11-S10 link uses
 *   `#/steward/catalog`  a stale journey segment for a screen that moved
 *   `#/steward-agent`    a route that was merged away
 *
 * Only a hash naming no known screen falls back to Overview. That is the one
 * case that is genuinely a dead link, and it is the behaviour a stale bookmark
 * had before this row.
 */
export function resolveHash(hash: string): ResolvedHash {
  const path = hash.replace(/^#\/?/, "").replace(/\/+$/, "");
  if (!path) return { screen: DEFAULT_SCREEN, canonical: false };

  const segments = path.split("/");
  const last = segments[segments.length - 1]!;

  // `journey/screen`: the screen is what decides, and the journey segment is
  // checked only to spot a stale one worth rewriting. A screen that has moved
  // between journeys must not 404 on the old grouping.
  if (segments.length >= 2 && isScreenId(last)) {
    const canonical = segments.length === 2 && segments[0] === SCREEN_JOURNEY[last];
    return { screen: last, canonical };
  }

  // The flat form. Resolves, and is rewritten to the grouped one.
  if (isScreenId(path)) return { screen: path, canonical: false };

  // A route that was merged away, with the filter that reproduces it.
  const retired = RETIRED_SCREEN_ALIASES[path] ?? RETIRED_SCREEN_ALIASES[last];
  if (retired) return { screen: retired.screen, canonical: false, params: retired.params };

  return { screen: DEFAULT_SCREEN, canonical: false };
}

/** Parse a screen id out of a hash (`#/analyst/catalog` -> `catalog`). */
export function screenFromHash(hash: string): ScreenId {
  return resolveHash(hash).screen;
}
