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
  "playbooks",
  "negative-knowledge",
  "meaning",
  "description-drafts",
  "relationships",
  "cross-source",
  "transformations",
  "quality",
  "studio",
  "governance",
  "parsed-lineage-review",
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

/**
 * Estate context that survives a screen change.
 *
 * These name *where in the estate you are working*, not *what you filtered on
 * this page*. Moving from Catalog to Lineage should keep the datasource you
 * were looking at; it should not keep Catalog's text search. Everything not
 * listed here is screen-owned and is dropped when the screen changes.
 */
export const CONTEXT_FIELDS = ["ds", "project", "dom"] as const;

/**
 * The query fields each screen owns, beyond `CONTEXT_FIELDS`.
 *
 * A screen missing from this table declares no fields: `buildLink` will pass
 * its params through unchanged and warn in development. Add the screen rather
 * than relying on that -- an undeclared field is a field nobody can tell you
 * is misspelled.
 */
export const SCREEN_QUERY_FIELDS: Partial<Record<ScreenId, readonly string[]>> = {
  home: [],
  inbox: ["persona"],
  analyst: ["run"],
  catalog: ["asset", "cert", "q", "type"],
  semantics: ["metric", "model"],
  tools: ["status", "tool"],
  "tool-plans": ["plan"],
  lineage: ["depth", "node", "view"],
  "unified-lineage": ["node", "scope", "tab"],
  marketplace: ["class", "domain", "product", "q", "sort"],
  context: [],
  developer: ["tab"],
  "portfolio-analytics": ["window"],
  stewardship: ["action", "field", "pattern"],
  worklist: ["ranking", "zero"],
  playbooks: [],
  "negative-knowledge": ["assertion_type", "subject", "suppression"],
  meaning: ["asset", "node", "q", "view"],
  "description-drafts": ["focus", "type"],
  relationships: ["candidate"],
  "cross-source": ["status"],
  transformations: ["dbtProject", "import", "match", "resource", "type"],
  quality: ["incident", "severity", "status"],
  studio: ["cs", "status"],
  governance: ["review", "status", "type"],
  "parsed-lineage-review": ["review", "status"],
  refusals: ["run"],
  "reviewer-agent": ["offset", "outcome", "window"],
  sources: ["q", "source", "status"],
  operations: ["batch_ds", "outbox_status", "run_status"],
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

/** Fields a screen may carry: its own, plus the shared estate context. */
export function allowedFieldsFor(screen: ScreenId): readonly string[] | null {
  const own = SCREEN_QUERY_FIELDS[screen];
  return own ? [...CONTEXT_FIELDS, ...own] : null;
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

  if (target.inheritContext !== false) {
    for (const field of CONTEXT_FIELDS) {
      const value = current.get(field);
      if (value) next.set(field, value);
    }
  }

  const dropped: string[] = [];
  const allowed = allowedFieldsFor(target.screen);
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

/** `?a=1&b=2#/screen`, or `#/screen` when there is nothing to carry. */
export function buildRelativeLink(
  target: LinkTarget,
  currentSearch: string | URLSearchParams = "",
): string {
  const query = buildSearch(target, currentSearch).toString();
  return `${query ? `?${query}` : ""}#/${target.screen}`;
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

/** Parse a screen id out of a hash (`#/catalog` -> `catalog`). */
export function screenFromHash(hash: string): ScreenId {
  const candidate = hash.replace(/^#\/?/, "");
  return isScreenId(candidate) ? candidate : DEFAULT_SCREEN;
}
