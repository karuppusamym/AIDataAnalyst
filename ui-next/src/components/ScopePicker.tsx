import { useMemo, useState } from "react";
import { useOrgSelection } from "../lib/org";
import { useScopeSelection } from "../lib/scope";
import type { ScopeTruncation } from "../lib/scope";

/* ---------------------------------------------------------------------------
   The active data scope, chosen once for the whole shell.

   F15/T13: these four lists used to be rendered straight from a single capped
   page (`limit=500`, `limit=200`) with nothing on screen to say so. On an
   estate larger than the cap, a source that exists and that this principal may
   use was simply not in the dropdown, and no amount of scrolling would find
   it. `lib/scope.tsx` now pages up to a budget and reports what it holds
   against what the server says exists; this component's job is to render that
   difference instead of hiding it, and to give the user a filter so a long
   list is navigable at all.

   The filter is CLIENT-SIDE, over the rows actually loaded, because the four
   backend list endpoints accept no `q=` parameter today (see the banner in
   `lib/api.ts`'s picker section). That is exactly why the truncation line has
   to sit next to it: filtering a prefix and calling it a search is how a
   picker convinces someone a record does not exist.

   F10/T12: every control is disabled until scope is `ready`. Selecting into a
   half-resolved scope is how a previous tenant's id ends up submitted against
   the organization you just switched to.
--------------------------------------------------------------------------- */

function truncationNote(what: string, counts: ScopeTruncation | undefined): string | null {
  if (!counts || !counts.truncated) return null;
  return `${counts.loaded.toLocaleString()} of ${counts.total.toLocaleString()} ${what} loaded`;
}

function matches(text: string, needle: string): boolean {
  return !needle || text.toLowerCase().includes(needle);
}

export function ScopePicker() {
  const org = useOrgSelection();
  const scope = useScopeSelection();
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();

  const visibleProjects = useMemo(
    () => (scope ? scope.visibleProjects.filter((item) => matches(item.name, needle)) : []),
    [scope, needle],
  );
  const visibleDatasources = useMemo(
    () => (scope ? scope.visibleDatasources.filter((item) => matches(item.name, needle)) : []),
    [scope, needle],
  );
  const visibleWorkspaces = useMemo(
    () => (scope ? scope.workspaces.filter((item) => matches(item.name, needle)) : []),
    [scope, needle],
  );

  if (!org || !scope) return null;

  const ready = scope.ready === true;
  const currentBinding = scope.bindings.find((item) => item.datasource_id === scope.datasourceId);
  const hasWorkspaceButNoSources = Boolean(scope.workspaceId) && scope.visibleDatasources.length === 0;

  /* A filter can hide the current selection. Keeping its option in the list
   * anyway means the `<select>` still shows what is actually selected --
   * without it the control would display the first matching row and quietly
   * misreport the scope every screen is reading. */
  const withSelected = <T extends { id: string; name: string }>(
    filtered: T[],
    all: T[],
    selectedId: string,
  ): T[] => {
    if (!selectedId || filtered.some((item) => item.id === selectedId)) return filtered;
    const selected = all.find((item) => item.id === selectedId);
    return selected ? [selected, ...filtered] : filtered;
  };

  const workspaceOptions = withSelected(visibleWorkspaces, scope.workspaces, scope.workspaceId);
  const projectOptions = withSelected(visibleProjects, scope.visibleProjects, scope.projectId);
  const datasourceOptions = withSelected(visibleDatasources, scope.visibleDatasources, scope.datasourceId);

  const workspaceNote = truncationNote("workspaces", scope.counts?.workspaces);
  const projectNote = truncationNote("projects", scope.counts?.projects);
  const sourceNote = truncationNote("sources", scope.counts?.datasources);

  return (
    <div className="scopepicker" data-testid="scope-picker" data-ready={ready}>
      <p className="scopepicker__eyebrow">ACTIVE DATA SCOPE</p>
      <label htmlFor="scope-org">Organization</label>
      <select id="scope-org" value={org.orgId} onChange={(event) => org.setOrgId(event.target.value)} disabled={org.loading}>
        {org.organizations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>

      <label htmlFor="scope-filter">Filter <span>this estate</span></label>
      <input
        id="scope-filter"
        className="scopepicker__search"
        type="search"
        value={filter}
        placeholder="Filter workspaces, projects, sources"
        onChange={(event) => setFilter(event.target.value)}
        disabled={!ready}
        aria-describedby="scope-filter-note"
      />
      <p id="scope-filter-note" className="scopepicker__count">
        Filters the rows loaded below. Server-side search is not available on these lists.
      </p>

      <label htmlFor="scope-workspace">Workspace <span>access</span></label>
      <select id="scope-workspace" value={scope.workspaceId} onChange={(event) => scope.setWorkspaceId(event.target.value)} disabled={!ready || scope.workspaces.length === 0}>
        {scope.workspaces.length === 0 ? <option value="">Not configured</option> : null}
        {workspaceOptions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      {workspaceNote ? <p className="scopepicker__count scopepicker__count--warn">{workspaceNote}</p> : null}

      <label htmlFor="scope-project">Project <span>application</span></label>
      <select id="scope-project" value={scope.projectId} onChange={(event) => scope.setProjectId(event.target.value)} disabled={!ready || scope.visibleProjects.length === 0}>
        {scope.visibleProjects.length === 0 ? <option value="">No accessible project</option> : null}
        {projectOptions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      {projectNote ? <p className="scopepicker__count scopepicker__count--warn">{projectNote}</p> : null}

      <label htmlFor="scope-source">Source <span>technical</span></label>
      <select id="scope-source" value={scope.datasourceId} onChange={(event) => scope.setDatasourceId(event.target.value)} disabled={!ready || scope.visibleDatasources.length === 0}>
        {scope.visibleDatasources.length === 0 ? <option value="">No active binding</option> : null}
        {datasourceOptions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      {sourceNote ? <p className="scopepicker__count scopepicker__count--warn">{sourceNote}</p> : null}

      <p className={`scopepicker__status${hasWorkspaceButNoSources ? " scopepicker__status--warn" : ""}`}>
        {scope.error
          ? "Scope could not be loaded"
          : !ready
            ? "Resolving scope…"
            : hasWorkspaceButNoSources
              ? "Workspace has no active source binding"
              : currentBinding
                ? `${currentBinding.status.toLowerCase()} binding · ${currentBinding.masking_profile.toLowerCase()} masking`
                : "Technical browsing without a workspace binding"}
      </p>
    </div>
  );
}
