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

/** `GET /v1/organizations/{id}/datasources` — resolves a datasource's display
 *  name to the id UX-20's lineage-impact call needs (`CatalogRowRead` only
 *  carries `datasource_name`, per this file's own catalog-rows note; the
 *  unified-lineage routes are scoped by `datasource_id`, so this bridges the
 *  two without a backend change). */
export function fetchOrgDatasources(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<DataSourceRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrgDatasources(),
    () =>
      get<PageOf<DataSourceRead>>(
        `/v1/organizations/${organizationId}/datasources?limit=500`,
        signal,
      ),
  );
}

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
     - NO `q=`/search parameter on any of them. Server-side search does not
       exist to call.
     - NO fetch-by-id route for an organization, workspace, project or
       datasource. Only sub-resources of a datasource are addressable
       (`/tables`, `/health`, `/scan-policy`), never the record itself.

   SO WHAT THIS DOES, HONESTLY:

     - pages through the estate up to an explicit budget and reports `total`
       and `truncated`, so a picker can say "1,000 of 4,812 loaded" rather
       than implying 1,000 is all there is;
     - resolves one id by a bounded paged scan (`findOrg*ById`) so a
       deep-linked or remembered selection past the first page still resolves
       to a name instead of disappearing from its own picker;
     - leaves text filtering to the caller, over the pages actually held --
       which is why `truncated` has to be rendered next to the filter box.

   THE TWO SERVER GAPS ARE REPORTED, NOT PAPERED OVER. A `q=` parameter on
   these four routes, and a fetch-by-id for each, would turn `findOrg*ById`
   into one request and make search correct rather than best-effort over a
   prefix. Until they exist the client half is complete and the truncation is
   visible.
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

/** One page of `GET /v1/organizations/{id}/datasources` (server cap: 500). */
export function fetchOrgDatasourcePage(
  organizationId: string,
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<PageOf<DataSourceRead>> {
  return demoOr(
    (fixtures) => fixtures.makeFixtureOrgDatasources(),
    () =>
      get<PageOf<DataSourceRead>>(
        `/v1/organizations/${organizationId}/datasources?limit=${limit}&offset=${offset}`,
        signal,
      ),
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

export function listOrgDatasources(
  organizationId: string,
  signal?: AbortSignal,
): Promise<ScopeList<DataSourceRead>> {
  return collectPages(
    (limit, offset) => fetchOrgDatasourcePage(organizationId, limit, offset, signal),
    500,
    SCOPE_PAGE_BUDGET,
  );
}

export function listOrganizations(signal?: AbortSignal): Promise<ScopeList<OrganizationRead>> {
  return collectPages(
    (limit, offset) => fetchOrganizationPage(limit, offset, signal),
    500,
    SCOPE_PAGE_BUDGET,
  );
}

/* The by-id resolvers. Each is a bounded paged scan, because the backend has
 * no fetch-by-id route for these four resources -- see this section's banner.
 * They exist so a link naming a source, or a selection remembered from a
 * previous session, still resolves when it sits past the pages the picker
 * preloaded. */

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

export function findOrgDatasourceById(
  organizationId: string,
  datasourceId: string,
  signal?: AbortSignal,
): Promise<DataSourceRead | null> {
  return scanForId(
    (limit, offset) => fetchOrgDatasourcePage(organizationId, limit, offset, signal),
    500,
    datasourceId,
  );
}
