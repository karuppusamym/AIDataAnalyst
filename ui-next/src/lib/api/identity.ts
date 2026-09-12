/* ---------------------------------------------------------------------------
   Identity, tenancy and the shell's data scope (review 2026-09-05, R05).

   The first domain lifted out of `api.ts`. It is the client's answer to four
   questions the shell asks before any screen loads: who am I, which
   organizations may I choose, which workspaces/projects/sources does this one
   contain, and which of those does the selected workspace actually reach.

   Grouped together because they share a lifecycle -- an organization change
   invalidates every one of them at once, which is what `lib/scope.tsx`'s
   atomic invalidation depends on -- and because they are the four lists F15
   found being rendered from a capped first page.

   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import type {
  DataSourceRead,
  MeRead,
  OrganizationRead,
  ProjectRead,
  SourceBindingRead,
  WorkspaceRead,
} from "../types";
import type { PageOf } from "../ui-types";

/**
 * UX-1 / module 21 §5: the one call that decides whether the shell may offer a
 * persona picker at all. `identity_provider` is the server's own prod/dev gate
 * (`Settings.identity_provider`, `aida.security.get_security_context`) — the shell
 * defers to it rather than inferring its own, and in `OIDC` mode `persona` is the
 * only persona the UI is allowed to use, never a client-selected value.
 *
 * `GET /v1/me` exists today (unlike the read-model calls above), so flip
 * `VITE_USE_FIXTURES=0` to see the real thing; fixture mode reports `DEVELOPMENT`
 * with no persona so the manual switcher below still works for pure-frontend
 * iteration with no backend running.
 */
export function fetchMe(signal?: AbortSignal): Promise<MeRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureMe(),
    () => get<MeRead>("/v1/me", signal),
  );
}

/**
 * `GET /v1/organizations` — the tenant list the shell's organization picker
 * needs.
 *
 * Reads through `listOrganizations` rather than one `limit=200` page (F15), so
 * a tenant past the first page is still selectable; the flat array shape is
 * kept because every existing caller expects it. Fixture mode returns the
 * single development organization the other fixtures are written against.
 */
export function fetchOrganizations(signal?: AbortSignal): Promise<OrganizationRead[]> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrganizations(),
    async () => (await listOrganizations(signal)).items,
  );
}

/** `GET /v1/organizations/{id}/projects` (`operational_api.py::list_organization_projects`)
 *  — real, already-merged, and NOT the org-wide semantic-model browse this
 *  screen would ideally have; it lists projects so a project can be picked,
 *  one call away from the project-scoped semantic-model-versions list below. */
export function fetchOrgProjects(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<ProjectRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrgProjects(),
    () =>
      get<PageOf<ProjectRead>>(
        `/v1/organizations/${organizationId}/projects?limit=500`,
        signal,
      ),
  );
}

/* `fetchOrgDatasources` — one `limit=500` page of an organization's sources —
 * used to live here, and eleven screens called it instead of the picker
 * section below. It is deleted rather than deprecated (R11-D7): a capped
 * fetch that is still exported is a capped fetch that gets called again, and
 * the whole point of F15 is that no screen may render a prefix as a fleet.
 * `listOrgDatasources` is the replacement for every one of those callers. */

/** Access-axis workspaces for an organization (ADR-0018). A workspace does
 * not own projects; it reaches project-owned sources through bindings. */
export function fetchOrgWorkspaces(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<WorkspaceRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrgWorkspaces(organizationId),
    () =>
      get<PageOf<WorkspaceRead>>(
        `/v1/organizations/${organizationId}/workspaces?limit=200`,
        signal,
      ),
  );
}

/** Grants connecting the selected workspace to one or more datasources. */
export function fetchWorkspaceSourceBindings(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PageOf<SourceBindingRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureWorkspaceSourceBindings(workspaceId),
    () =>
      get<PageOf<SourceBindingRead>>(
        `/v1/workspaces/${workspaceId}/source-bindings`,
        signal,
      ),
  );
}

/* ---------------------------------------------------------------------------
   Scope pickers that do not lie about the size of the estate
   (review 2026-09-05, F15 · T13).

   THE DEFECT this section exists to remove: the organization, workspace,
   project and source pickers each fetched ONE page -- `limit=500`, `limit=200`
   -- and rendered `.items` as though it were the whole list. On any estate
   larger than the cap, valid resources were simply unselectable, a deep link
   to one of them resolved to nothing, and the screen said not a word about
   it. A capped list rendered as a complete list is worse than an error: it is
   a wrong answer delivered confidently.

   WHAT THE BACKEND ACTUALLY SUPPORTS (checked, not assumed --
   `operational_api.list_organization_projects`,
   `operational_api.list_organization_datasources`,
   `identity_tenancy.router.list_workspaces` and `.list_organizations`):

     - `limit`/`offset` paging with a real `total` on all four. Paging is
       therefore implementable client-side today, and `total` is trustworthy.
     - `q=` search on all four, and a fetch-by-id route for each. Both landed
       on the server after this section was first written (the two gaps its
       original banner reported; `tests/test_scope_picker_search.py` is the
       suite that carries them, and it is the tenant-isolation half of that
       change that makes them safe to call).

   SO WHAT THIS DOES:

     - pages through the estate up to an explicit budget and reports `total`
       and `truncated`, so a picker can say "1,000 of 4,812 loaded" rather
       than implying 1,000 is all there is;
     - resolves one id in ONE request, so a deep-linked or remembered
       selection past the loaded prefix still resolves to a name instead of
       disappearing from its own picker;
     - pushes a search term to the server for the source list, so a name
       typed into a picker is matched against the whole fleet rather than
       against the prefix the client happens to hold.

   R11-D7 finished the datasource half: `fetchOrgDatasourcePage` takes `q`,
   `findOrgDatasourceById` is one GET rather than a 25-page scan, and
   `listOrgDatasources` is the single entry point every screen now uses.
   Workspaces, projects and organizations still search client-side over their
   loaded prefix -- the server supports better, and `ScopePicker`'s note says
   so rather than implying the filter is a search.
--------------------------------------------------------------------------- */

/** A collected list, plus what it is a list *of*. */
export interface ScopeList<T> {
  items: T[];
  /** The server's own count for the whole collection. */
  total: number;
  /** True when `items` is a prefix of the collection rather than all of it. */
  truncated: boolean;
  /** How many requests produced `items`. */
  pagesFetched: number;
}

/**
 * How far a picker pages before it stops and says so.
 *
 * Not an estimate of how large estates are: it is the point past which
 * preloading stops being a picker and becomes a bulk export. Beyond it the
 * answer is server-side search, not a larger number.
 */
const SCOPE_PAGE_BUDGET = 5;

/** How far a by-id scan pages. Wider, because failing to resolve the id a
 *  link names costs the user their link, and a few extra reads do not. */
const LOOKUP_PAGE_BUDGET = 25;

async function collectPages<T>(
  load: (limit: number, offset: number) => Promise<PageOf<T>>,
  pageSize: number,
  budget: number,
): Promise<ScopeList<T>> {
  const items: T[] = [];
  let total = 0;
  let pagesFetched = 0;

  for (let page = 0; page < budget; page += 1) {
    const result = await load(pageSize, items.length);
    pagesFetched += 1;
    total = result.total ?? result.items.length;
    items.push(...result.items);
    // Stop on a short page as well as on `total`: an endpoint that ignores
    // `offset` would otherwise be asked for the same page until the budget
    // ran out.
    if (result.items.length === 0 || result.items.length < pageSize) break;
    if (items.length >= total) break;
  }

  return {
    items,
    total: Math.max(total, items.length),
    truncated: items.length < total,
    pagesFetched,
  };
}

async function scanForId<T extends { id: string }>(
  load: (limit: number, offset: number) => Promise<PageOf<T>>,
  pageSize: number,
  id: string,
): Promise<T | null> {
  let seen = 0;
  for (let page = 0; page < LOOKUP_PAGE_BUDGET; page += 1) {
    const result = await load(pageSize, seen);
    const match = result.items.find((item) => item.id === id);
    if (match) return match;
    seen += result.items.length;
    if (result.items.length === 0 || result.items.length < pageSize) return null;
    if (seen >= (result.total ?? seen)) return null;
  }
  return null;
}

/** One page of `GET /v1/organizations/{id}/workspaces` (server cap: 200). */
export function fetchOrgWorkspacePage(
  organizationId: string,
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<PageOf<WorkspaceRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrgWorkspaces(organizationId),
    () =>
      get<PageOf<WorkspaceRead>>(
        `/v1/organizations/${organizationId}/workspaces?limit=${limit}&offset=${offset}`,
        signal,
      ),
  );
}

/** One page of `GET /v1/organizations/{id}/projects` (server cap: 500). */
export function fetchOrgProjectPage(
  organizationId: string,
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<PageOf<ProjectRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrgProjects(),
    () =>
      get<PageOf<ProjectRead>>(
        `/v1/organizations/${organizationId}/projects?limit=${limit}&offset=${offset}`,
        signal,
      ),
  );
}

/**
 * One page of `GET /v1/organizations/{id}/datasources` (server cap: 500).
 *
 * `search` is the route's own `q=` (`operational_api.list_organization_
 * datasources`), which matches on name -- a datasource has no slug or key, so
 * name is the only thing a person types. It is the parameter that makes the
 * difference between a picker searching the fleet and a picker searching the
 * page it already has; the fixture path filters by the same rule so a
 * fixtures build exercises the search, not a stubbed-out no-op.
 */
export function fetchOrgDatasourcePage(
  organizationId: string,
  limit: number,
  offset: number,
  signal?: AbortSignal,
  search?: string,
): Promise<PageOf<DataSourceRead>> {
  const term = search?.trim() ?? "";
  return demoOr(
    async (fixtures) => {
      const page = await fixtures.makeFixtureOrgDatasources();
      if (!term) return page;
      const needle = term.toLowerCase();
      const items = page.items.filter((item) => item.name.toLowerCase().includes(needle));
      return { ...page, items, total: items.length };
    },
    () =>
      get<PageOf<DataSourceRead>>(
        `/v1/organizations/${organizationId}/datasources?limit=${limit}&offset=${offset}` +
          (term ? `&q=${encodeURIComponent(term)}` : ""),
        signal,
      ),
  );
}

/**
 * `GET /v1/datasources/{id}` -- one source, by id.
 *
 * The route a picker needs when the id it must display is not in the pages it
 * loaded: a link someone was sent, or a selection remembered from a session
 * when the fleet was smaller. Answers `DataSourceSummaryRead`, the same
 * projection the list route returns, so a resolved-by-id row and a listed row
 * render identically.
 */
export function fetchDatasourceById(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<DataSourceRead> {
  return demoOr(
    async (fixtures) => {
      const page = await fixtures.makeFixtureOrgDatasources();
      const match = page.items.find((item) => item.id === datasourceId);
      if (!match) throw new Error(`No fixture datasource ${datasourceId}`);
      return match;
    },
    () =>
      get<DataSourceRead>(`/v1/datasources/${encodeURIComponent(datasourceId)}`, signal),
  );
}

/** One page of `GET /v1/organizations` (server cap: 500). */
export function fetchOrganizationPage(
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<PageOf<OrganizationRead>> {
  return demoOr(
    async (fixtures) => {
      const items = await fixtures.makeFixtureOrganizations();
      return { items, limit, offset: 0, total: items.length };
    },
    () =>
      get<PageOf<OrganizationRead>>(`/v1/organizations?limit=${limit}&offset=${offset}`, signal),
  );
}

export function listOrgWorkspaces(
  organizationId: string,
  signal?: AbortSignal,
): Promise<ScopeList<WorkspaceRead>> {
  return collectPages(
    (limit, offset) => fetchOrgWorkspacePage(organizationId, limit, offset, signal),
    200,
    SCOPE_PAGE_BUDGET,
  );
}

export function listOrgProjects(
  organizationId: string,
  signal?: AbortSignal,
): Promise<ScopeList<ProjectRead>> {
  return collectPages(
    (limit, offset) => fetchOrgProjectPage(organizationId, limit, offset, signal),
    500,
    SCOPE_PAGE_BUDGET,
  );
}

/** What a source list is being asked for, beyond the organization. */
export interface DatasourceListOptions {
  /** Server-side `q=`, matched against the whole fleet rather than the
   *  loaded prefix. */
  readonly search?: string;
  /** An id the answer must contain even when the search excludes it or the
   *  page budget stops short of it -- a `<select>` that drops its own
   *  selected value silently reports the wrong scope. */
  readonly selectedId?: string | null;
}

/**
 * THE source list. Every screen reads its datasources through this (R11-D7);
 * there is no per-screen fetch to drift out of step with the picker.
 *
 * The selected-id splice is here rather than in each caller because it is the
 * half that is easy to forget and impossible to notice: without it a picker
 * looks right until the one user whose source sits on page six opens it.
 */
export async function listOrgDatasources(
  organizationId: string,
  signal?: AbortSignal,
  options?: DatasourceListOptions,
): Promise<ScopeList<DataSourceRead>> {
  const list = await collectPages(
    (limit, offset) =>
      fetchOrgDatasourcePage(organizationId, limit, offset, signal, options?.search),
    500,
    SCOPE_PAGE_BUDGET,
  );

  const selectedId = options?.selectedId;
  if (!selectedId || list.items.some((item) => item.id === selectedId)) return list;

  const selected = await findOrgDatasourceById(organizationId, selectedId, signal);
  // `total` and `truncated` describe the collection, not what is on screen, so
  // a spliced row must not make a truncated list look complete.
  return selected ? { ...list, items: [selected, ...list.items] } : list;
}

export function listOrganizations(signal?: AbortSignal): Promise<ScopeList<OrganizationRead>> {
  return collectPages(
    (limit, offset) => fetchOrganizationPage(limit, offset, signal),
    500,
    SCOPE_PAGE_BUDGET,
  );
}

/* The by-id resolvers. They exist so a link naming a source, or a selection
 * remembered from a previous session, still resolves when it sits past the
 * pages the picker preloaded. Workspace and project are still a bounded paged
 * scan; the datasource one is a single GET, and reads as `null` on any
 * failure so a picker falls back to a valid selection rather than breaking on
 * an id that has since been deleted. */

export function findOrgWorkspaceById(
  organizationId: string,
  workspaceId: string,
  signal?: AbortSignal,
): Promise<WorkspaceRead | null> {
  return scanForId(
    (limit, offset) => fetchOrgWorkspacePage(organizationId, limit, offset, signal),
    200,
    workspaceId,
  );
}

export function findOrgProjectById(
  organizationId: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectRead | null> {
  return scanForId(
    (limit, offset) => fetchOrgProjectPage(organizationId, limit, offset, signal),
    500,
    projectId,
  );
}

export async function findOrgDatasourceById(
  organizationId: string,
  datasourceId: string,
  signal?: AbortSignal,
): Promise<DataSourceRead | null> {
  // `organizationId` is unused: the route enforces the tenant boundary itself
  // (`load_datasource_in_scope`), so passing it would only let a caller
  // believe the client was doing the checking. Kept in the signature because
  // the three resolvers are called interchangeably by `lib/scope.tsx`.
  void organizationId;
  try {
    return await fetchDatasourceById(datasourceId, signal);
  } catch {
    return null;
  }
}
