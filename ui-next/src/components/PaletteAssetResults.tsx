import { useEffect, useState } from "react";
import {
  SEARCH_ROLES,
  SUGGEST_QUERY_MAX_LENGTH,
  fetchSearchSuggestions,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { readDecision, roleAllows } from "../lib/roles";
import { CATALOG_ROWS_ROLES, objectTypeLabel, searchTargetFor } from "../lib/searchTargets";
import { useSession } from "../lib/session";
import { useAsyncResource } from "./screenState";
import "./PaletteAssetResults.css";

/* ---------------------------------------------------------------------------
   Ctrl+K, past the page names (R11-AUD08).

   THE PALETTE FILTERS THE SHELL'S OWN PAGE LIST. `/v1/search/suggest` exists
   "for the command palette" (its own docstring) and the palette never called
   it, so a person who knew a table's name and pressed Ctrl+K got "No matching
   page". This is the second half of that box: under the pages that match, the
   tables whose names contain what was typed, and a way into the full Search
   screen for the rest.

   A COMPONENT OF ITS OWN so the shell's change is one element and its state
   (debounce, in-flight request, roles) does not live in `App.tsx`. The shell
   hands it the query and the shell's own `navigate` -- which asks the
   unsaved-changes guard and closes the palette -- and it owns everything else.

   WHAT IT ASKS, AND WHEN:

     * Nothing under two characters (a `contains` over every table name is noise
       and the palette is a navigator first), and nothing over 200 (the route
       refuses it). Typing is debounced, so a run of keystrokes is one request,
       and a superseded one is aborted.
     * Nothing from a session known to hold none of the five roles the route
       admits -- and nothing while `/v1/me` is in flight, because the request a
       session that turns out not to be admitted would send is a 403 per
       keystroke burst (`readDecision`). Such a session is told, when no page
       matched either, that searching is not available to its roles; when a page
       did match it is not nagged.
     * Table suggestions only when the session can OPEN the Catalog
       (`CATALOG_ROWS_ROLES`), because that is where a table opens and a row that
       ends in a refusal is a control that cannot work. A session that can search
       and cannot open the Catalog still gets "Search all", which lists the same
       tables plainly.

   WHAT IT SHOWS is the API's own answer: table names, nothing derived. An empty
   answer says "No tables match", a failure says it failed and why, and neither
   is a blank space that reads as "there is nothing".

   WHERE A TABLE OPENS is `searchTargetFor`, the one function the Search screen
   uses too. A suggestion carries no datasource, so it opens the Catalog on that
   table with the table's name as the Catalog's filter.
--------------------------------------------------------------------------- */

/** The palette is a navigator: a handful of names, and "Search all" for the rest. */
const SUGGESTION_LIMIT = 6;
const MIN_QUERY_LENGTH = 2;
const DEBOUNCE_MS = 250;

export function PaletteAssetResults({
  query,
  pagesMatched,
  onOpen,
}: {
  /** What is typed in the palette's box, untrimmed. */
  query: string;
  /** Whether any PAGE matched it -- decides whether a session that cannot search is told so. */
  pagesMatched: boolean;
  /** The shell's `navigate`. */
  onOpen: (screen: string, params?: Record<string, string>) => void;
}) {
  const ORG = useOrgId();
  const session = useSession();
  const roles = session.me?.roles;
  const read = readDecision(session, SEARCH_ROLES);
  const canOpenCatalog = roleAllows(roles, CATALOG_ROWS_ROLES);

  const needle = query.trim();
  const wanted = needle.length >= MIN_QUERY_LENGTH && needle.length <= SUGGEST_QUERY_MAX_LENGTH;

  // The query as it was a quarter-second ago: what the request is for.
  const [settled, setSettled] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setSettled(needle), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [needle]);
  const settledWanted = settled.length >= MIN_QUERY_LENGTH && settled.length <= SUGGEST_QUERY_MAX_LENGTH;

  const suggestions = useAsyncResource(
    (signal) => fetchSearchSuggestions(ORG, settled, SUGGESTION_LIMIT, signal),
    [ORG, settled],
    { enabled: read === "ask" && canOpenCatalog && settledWanted },
  );

  if (!wanted || read === "wait") return null;
  if (read === "skip") {
    return pagesMatched ? null : (
      <p className="palette__hint">Searching tables and columns is not available to your roles.</p>
    );
  }

  // Asked for something else a moment ago: whatever is held is not an answer to this.
  const stale = settled !== needle;
  const items = (suggestions.data ?? []).flatMap((suggestion) => {
    const target = searchTargetFor(suggestion);
    return target ? [{ suggestion, target }] : [];
  });

  return (
    <div className="palette__group" role="group" aria-label="Tables and columns">
      <div className="palette__grouphead">Tables and columns</div>
      {canOpenCatalog ? (
        stale || suggestions.loading ? (
          <div className="palette__note" role="status">Searching tables…</div>
        ) : suggestions.error ? (
          <div className="palette__note" role="status">Table search failed: {suggestions.error}</div>
        ) : items.length === 0 ? (
          <div className="palette__note" role="status">No tables match “{needle}”.</div>
        ) : (
          items.map(({ suggestion, target }) => (
            <button
              key={`${suggestion.object_type}:${suggestion.object_id}`}
              type="button"
              className="palette__item"
              onClick={() => onOpen(target.screen, target.params)}
            >
              <span className="snav__icon" aria-hidden="true">▦</span>
              <span>
                <b>{suggestion.display_name}</b>{" "}
                <small>{objectTypeLabel(suggestion.object_type)} · opens in the Catalog</small>
              </span>
              <span className="palette__arrow" aria-hidden="true">→</span>
            </button>
          ))
        )
      ) : null}
      <button type="button" className="palette__item" onClick={() => onOpen("search", { q: needle })}>
        <span className="snav__icon" aria-hidden="true">⌕</span>
        <span>
          <b>Search all tables and columns for “{needle}”</b>{" "}
          <small>Opens Search</small>
        </span>
        <span className="palette__arrow" aria-hidden="true">→</span>
      </button>
    </div>
  );
}
