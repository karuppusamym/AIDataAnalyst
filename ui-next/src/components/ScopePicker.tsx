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
  /* The four fields start collapsed, on every screen.

     Measured, not guessed: on a 1366x768 laptop at 100% zoom this block
     rendered 446px tall -- 58% of the viewport -- and pushed the first
     navigation link to y=525, so 6 of the sidebar's 19 rendered nav items were
     on screen and the Reviewer, Operator and Auditor groups were entirely
     below the fold. The sidebar scrolls, so nothing was unreachable; a laptop
     user simply opened the app and saw no navigation.

     A viewport threshold was tried first and abandoned. Expanded, the sidebar
     chrome is ~614px (brand + fields + footer), so showing most of a 19-item
     nav needs roughly 1120px of viewport height -- a 1440p monitor, not a
     laptop. At 1440x900 the threshold still left 8 of 19 items visible while
     making behaviour depend on which monitor the window happened to be on.
     Collapsed-by-default is the same on every screen and costs one click on
     the rare occasion scope is changed rather than read.

     Scope stays *readable* while collapsed: the summary line and the binding
     status sit outside the collapsible region, because a scope picker that
     hides which tenant you are in is a worse bug than the one being fixed. */
  const [fieldsOpen, setFieldsOpen] = useState(false);
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

  /* What the collapsed header has to carry on its own. Read from the same
   * selections the fields render, so the summary cannot disagree with the
   * `<select>` values underneath it. */
  const scopeSummary = [
    org.organizations.find((item) => item.id === org.orgId)?.name,
    scope.workspaces.find((item) => item.id === scope.workspaceId)?.name ?? "no workspace",
    scope.visibleDatasources.find((item) => item.id === scope.datasourceId)?.name ?? "no source",
  ]
    .filter(Boolean)
    .join(" · ");

  const workspaceNote = truncationNote("workspaces", scope.counts?.workspaces);
  const projectNote = truncationNote("projects", scope.counts?.projects);
  const sourceNote = truncationNote("sources", scope.counts?.datasources);

  return (
    <div className="scopepicker" data-testid="scope-picker" data-ready={ready}>
      <button
        type="button"
        className="scopepicker__toggle"
        /* Named explicitly: the eyebrow and the summary are adjacent spans, so
         * the computed name would otherwise run them together as
         * "ACTIVE DATA SCOPEAtlas Demo Bank". No "expand"/"collapse" verb here
         * -- `aria-expanded` already announces the state, and repeating it in
         * the name makes it announce twice. */
        aria-label={`Active data scope: ${scopeSummary}`}
        aria-expanded={fieldsOpen}
        aria-controls="scope-fields"
        onClick={() => setFieldsOpen((previous) => !previous)}
      >
        <span className="scopepicker__eyebrow">ACTIVE DATA SCOPE</span>
        <span className="scopepicker__summary">{scopeSummary}</span>
        <span className="scopepicker__chevron" aria-hidden="true">{fieldsOpen ? "−" : "+"}</span>
      </button>

      <div id="scope-fields" className="scopepicker__fields" hidden={!fieldsOpen}>
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
      </div>

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
