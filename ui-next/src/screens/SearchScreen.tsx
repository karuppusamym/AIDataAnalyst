import { useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import {
  SEARCH_PAGE_SIZE,
  SEARCH_QUERY_MAX_LENGTH,
  SEARCH_ROLES,
  SUGGEST_QUERY_MAX_LENGTH,
  fetchSearchResults,
  fetchSearchSuggestions,
} from "../lib/api";
import type { SearchResultRead } from "../lib/api";
import { navigateTo } from "../lib/navigate";
import { useOrgId } from "../lib/org";
import { readDecision, roleAllows } from "../lib/roles";
import { CATALOG_ROWS_ROLES, objectTypeLabel, searchTargetFor } from "../lib/searchTargets";
import { listOr } from "../lib/sentences";
import { useSession } from "../lib/session";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field } from "../components/primitives";
import { useAsyncResource } from "../components/screenState";
import "./SearchScreen.css";

/* ---------------------------------------------------------------------------
   Search (R11-AUD08) -- `GET /v1/search` and `GET /v1/search/suggest`, which
   the command palette's own docstring promised and no screen ever called.

   WHAT IT SEARCHES, said on the page because a person types differently for it:
   the NAMES of tables and columns, across every source the caller can read. A
   description is not searched (`search_api.global_search` picks candidates by a
   case-insensitive `contains` on the name and only then ranks). The Catalog's
   own search box is the one that looks in descriptions.

   THE ANSWER, AS THE API GIVES IT. One ranked list, paged by the API
   (`limit`/`offset`, with `total`), and a facet count per object type. The list
   is shown grouped by type -- Tables, Columns -- because a person scanning for
   a table should not have to step over columns to find it; the headings carry
   the API's per-type counts, and when a page holds only some of a type's hits
   the heading says how many of them (`3 of 7`). The relevance shown is the
   API's `score` and nothing else: there is no percentage here that the server
   did not send.

   WHERE A RESULT OPENS (`lib/searchTargets.ts`, shared with the palette): a
   TABLE opens in the Catalog, on that table. A COLUMN does not, and the page
   says why in one sentence rather than pretending: the search answer names a
   column and gives neither its table nor its datasource, so five columns called
   `customer_id` are five identical rows with nowhere to go. That is the API's
   gap to close (its handler builds the table id and then drops it); this screen
   opens a column's table the day the answer carries it. A session whose roles
   the Catalog refuses (`CATALOG_ROWS_ROLES`) gets the same results with no
   links and a sentence saying why -- never a link that ends in a refusal.

   WHO MAY ASK. Both routes admit the same five roles. A session known to hold
   none of them is asked nothing (`readDecision`) and told search is not
   available to its roles; while `/v1/me` is in flight nothing is asked, and if
   identity never answers the server stays the authority and the search is made.

   THE QUERY LIVES IN THE URL (`?q=`, `?type=`, `?offset=`), so a search is a
   link and the palette can open this screen already running. It runs on submit
   -- Enter or the button -- not on every keystroke: the suggestions are the
   as-you-type help (a native `<datalist>`, which a screen reader announces
   without any of this file's help), and a ranked search of every name in the
   estate is a request worth choosing to make.
--------------------------------------------------------------------------- */

/** The object types the API can filter by and actually searches. `ANNOTATION` is named in the
 *  route's own description and has no branch behind it, so it is not offered. */
const TYPE_FILTERS = [
  { value: "", label: "Tables and columns" },
  { value: "TABLE", label: "Tables" },
  { value: "COLUMN", label: "Columns" },
] as const;

const capitalized = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);

/** Hits grouped by object type, in the order each type first appears in the ranked page. */
function groupByType(items: readonly SearchResultRead[]): Array<[string, SearchResultRead[]]> {
  const groups = new Map<string, SearchResultRead[]>();
  for (const item of items) {
    const group = groups.get(item.object_type);
    if (group) group.push(item);
    else groups.set(item.object_type, [item]);
  }
  return [...groups.entries()];
}

function nonNegativeInt(raw: string | null): number {
  const value = Number.parseInt(raw ?? "", 10);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function SearchScreen() {
  const ORG = useOrgId();
  const session = useSession();
  const roles = session.me?.roles;
  const read = readDecision(session, SEARCH_ROLES);
  // Whether a table result can OPEN. Fails open while identity is in flight (a link is not a request).
  const canOpenCatalog = roleAllows(roles, CATALOG_ROWS_ROLES);

  const [params, setParams] = useUrlState();
  const q = (params.get("q") ?? "").trim();
  const typeParam = params.get("type") ?? "";
  const type = TYPE_FILTERS.some((filter) => filter.value === typeParam) ? typeParam : "";
  const offset = nonNegativeInt(params.get("offset"));

  // What is typed, before it is submitted. Follows the URL when the URL moves (Back, or a link from the palette).
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);
  const listId = useId();

  /* Typeahead: debounced so a run of keystrokes is one request. Below two characters a `contains`
     over every table name says nothing, and the route refuses a query over 200. */
  const [suggestFor, setSuggestFor] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setSuggestFor(draft.trim()), 250);
    return () => clearTimeout(timer);
  }, [draft]);
  const suggest = useAsyncResource(
    (signal) => fetchSearchSuggestions(ORG, suggestFor, 8, signal),
    [ORG, suggestFor],
    { enabled: read === "ask" && suggestFor.length >= 2 && suggestFor.length <= SUGGEST_QUERY_MAX_LENGTH },
  );
  const suggestedNames = [...new Set((suggest.data ?? []).map((suggestion) => suggestion.display_name))];

  const results = useAsyncResource(
    (signal) => fetchSearchResults(ORG, { q, objectType: type || null, limit: SEARCH_PAGE_SIZE, offset }, signal),
    [ORG, q, type, offset],
    { enabled: read === "ask" && q !== "" },
  );
  const reloadResults = results.reload;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = draft.trim();
    // The same words again is a request to look again, and the URL would not change to say so.
    if (next === q) reloadResults();
    else setParams({ q: next || null, offset: null });
  };

  const page = results.data;
  const groups = page ? groupByType(page.items) : [];
  const facetCounts = new Map(
    (page?.facets ?? []).filter((facet) => facet.field === "object_type").map((facet) => [facet.value, facet.count]),
  );
  const columnsWithoutTable = page?.items.some((hit) => hit.object_type === "COLUMN" && !searchTargetFor(hit)) ?? false;
  const tablesWithoutLink = !canOpenCatalog && (page?.items.some((hit) => searchTargetFor(hit) !== null) ?? false);

  return (
    <div className="srch">
      <header className="srch__head">
        <h1 className="srch__h1">Search</h1>
        <p className="srch__lede">
          Find a table or a column by name across every source you can read. Results are ranked by how well the name
          matches what you typed; descriptions are not searched.
        </p>
      </header>

      {read === "skip" ? (
        <section className="srch__panel" aria-label="Search">
          <p className="srch__note">
            Search is not available to your roles: only sessions holding {listOr(SEARCH_ROLES)} can search tables and
            columns.
          </p>
        </section>
      ) : read === "wait" ? (
        <section className="srch__panel" aria-label="Search">
          <p className="srch__note" role="status">Loading search…</p>
        </section>
      ) : (
        <>
          <form className="srch__form" role="search" aria-label="Search tables and columns" onSubmit={submit}>
            <Field label="Search by name">
              <input
                type="search"
                value={draft}
                list={listId}
                maxLength={SEARCH_QUERY_MAX_LENGTH}
                autoComplete="off"
                placeholder="customer, order_total…"
                onChange={(event) => setDraft(event.target.value)}
              />
            </Field>
            <datalist id={listId}>
              {suggestedNames.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
            <Field label="Look in">
              <select value={type} onChange={(event) => setParams({ type: event.target.value || null, offset: null })}>
                {TYPE_FILTERS.map((filter) => (
                  <option key={filter.value} value={filter.value}>{filter.label}</option>
                ))}
              </select>
            </Field>
            <Button type="submit" variant="primary" disabled={draft.trim() === ""}>Search</Button>
          </form>
          {suggest.error ? (
            <p className="srch__note" role="status">Suggestions are unavailable: {suggest.error}</p>
          ) : null}

          <section className="srch__panel" aria-label="Search results">
            {q === "" ? (
              <p className="srch__note">
                Type part of a table or column name and press Search. Suggestions of matching table names appear as you
                type.
              </p>
            ) : results.error ? (
              <ErrorState title="The search could not be run" detail={results.error} onRetry={reloadResults} />
            ) : results.loading || !page ? (
              <p className="srch__note" role="status">Loading results…</p>
            ) : page.total === 0 ? (
              <Empty
                title={`No tables or columns match “${q}”`}
                hint="Search matches parts of table and column names. Words shorter than two letters and common words such as “the” are ignored, and descriptions are not searched."
              />
            ) : page.items.length === 0 ? (
              <Empty
                title="This page of results is empty"
                hint={`There are ${page.total} matches; this page starts past the last of them.`}
              />
            ) : (
              <>
                <p className="srch__summary" role="status">
                  {page.total} {page.total === 1 ? "match" : "matches"} for “{q}”
                  {facetCounts.size > 0
                    ? `: ${[...facetCounts.entries()]
                        .map(([value, count]) => `${count} ${objectTypeLabel(value, count !== 1)}`)
                        .join(", ")}`
                    : ""}
                </p>

                {groups.map(([objectType, hits]) => {
                  const total = facetCounts.get(objectType);
                  return (
                    <section key={objectType} className="srch__group">
                      <h2 className="srch__grouphead">
                        {capitalized(objectTypeLabel(objectType, true))}{" "}
                        <span className="srch__groupcount">
                          {total !== undefined && total !== hits.length ? `${hits.length} of ${total} on this page` : hits.length}
                        </span>
                      </h2>
                      <ul className="srch__hits">
                        {hits.map((hit) => {
                          const target = searchTargetFor(hit);
                          const opens = target !== null && canOpenCatalog;
                          return (
                            <li key={`${hit.object_type}:${hit.object_id}`} className="srch__hit">
                              {opens ? (
                                <button
                                  type="button"
                                  className="srch__hitname"
                                  aria-label={
                                    hit.object_type === "TABLE"
                                      ? `Open ${hit.display_name} in the Catalog`
                                      : `Open the table of column ${hit.display_name} in the Catalog`
                                  }
                                  onClick={() => navigateTo(target.screen, target.params)}
                                >
                                  {hit.display_name}
                                  <span aria-hidden="true"> →</span>
                                </button>
                              ) : (
                                <span className="srch__hitname srch__hitname--plain">{hit.display_name}</span>
                              )}
                              <span className="srch__hitmeta">
                                {hit.qualified_name && hit.qualified_name !== hit.display_name ? (
                                  <code>{hit.qualified_name}</code>
                                ) : null}
                                {hit.datasource_name ? <span>{hit.datasource_name}</span> : null}
                                {hit.description ? <span>{hit.description}</span> : null}
                                <span>relevance {hit.score.toFixed(2)}</span>
                                {hit.object_type === "COLUMN" && target === null ? <code>{hit.object_id}</code> : null}
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    </section>
                  );
                })}

                {columnsWithoutTable ? (
                  <p className="srch__note">
                    The search names a column but does not say which table it belongs to, so a column result cannot open
                    its table from here.
                  </p>
                ) : null}
                {tablesWithoutLink ? (
                  <p className="srch__note">
                    Opening a table needs {listOr(CATALOG_ROWS_ROLES)}, which your roles do not include, so these
                    results are listed without links.
                  </p>
                ) : null}

                {page.total > page.limit || page.offset > 0 ? (
                  <nav className="srch__pager" aria-label="Search result pages">
                    <span role="status">
                      Showing {page.offset + 1}–{page.offset + page.items.length} of {page.total}
                    </span>
                    <Button
                      disabled={page.offset === 0}
                      onClick={() => {
                        const previous = Math.max(0, page.offset - page.limit);
                        setParams({ offset: previous > 0 ? String(previous) : null });
                      }}
                    >
                      Previous
                    </Button>
                    <Button
                      disabled={page.offset + page.limit >= page.total}
                      onClick={() => setParams({ offset: String(page.offset + page.limit) })}
                    >
                      Next
                    </Button>
                  </nav>
                ) : null}
              </>
            )}
          </section>
        </>
      )}
    </div>
  );
}
