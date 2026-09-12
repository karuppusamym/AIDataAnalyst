import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  findOrgDatasourceById,
  findOrgProjectById,
  findOrgWorkspaceById,
  listOrgDatasources,
  listOrgProjects,
  listOrgWorkspaces,
  fetchWorkspaceSourceBindings,
} from "./api";
import { useOrgId } from "./org";
import type { DataSourceRead, ProjectRead, SourceBindingRead, WorkspaceRead } from "./types";

/* ---------------------------------------------------------------------------
   The active data scope: one organization, one workspace, one project, one
   source (review 2026-09-05, F10/F15 · T12/T13).

   THE DEFECTS this module exists to remove, in the order they bit:

   1. SCOPE OUTLIVED ITS ORGANIZATION. Switching tenant left the previous
      organization's workspace list, project list, source list, bindings AND
      selected ids on screen until three separate requests resolved. For that
      window the picker named organization B while every id under it belonged
      to organization A, and any screen reading `datasourceId` asked B's API
      about A's source.

   2. SELECTION WAS ADOPTED IN PHASES. Workspace resolved in one effect,
      project in a second keyed on the derived visible list, source in a
      third. Each intermediate combination rendered. A user could see -- and
      click -- a project that the source list was about to invalidate.

   3. PERSISTENCE COULD WRITE ACROSS TENANTS. `useEffect(() => persist(orgId,
      "project", projectId), [orgId, projectId])` fires when EITHER changes,
      so the render that had the new `orgId` and the old `projectId` wrote the
      previous tenant's project under the new tenant's storage key. The next
      visit then restored a foreign id as a remembered preference.

   4. OUT-OF-ORDER RESPONSES POPULATED THE WRONG VIEW. The only guard was
      `AbortController`, which covers unmount and re-run but not a response
      that resolves after a newer one. Two fast tenant switches could leave
      the first organization's data adopted last.

   5. WRITES WERE ENABLED BEFORE SCOPE EXISTED. Screens could act while
      `datasourceId` was still "" or still the previous tenant's.

   THE INVARIANTS now enforced here:

     - Every piece of scope state carries the organization (and, for bindings,
       the workspace) it was fetched FOR. Nothing is adopted unless those tags
       still match the current selection, so a late response is discarded
       rather than rendered.
     - An organization change empties scope IMMEDIATELY, during the render
       that observes it, not when a request resolves. Empty and honest beats
       populated and wrong.
     - Workspace, project and source are resolved TOGETHER, by one pure
       function, from one consistent snapshot, and adopted in one `setState`.
       There is no intermediate combination to observe.
     - Persistence is written from the transition that chose the value, using
       the organization that transition belonged to. No effect writes storage.
     - `ready` is false until scope resolves, and every setter is inert until
       then.

   NONE OF THIS IS AUTHORIZATION. The backend decides what this principal may
   see in this organization on every request (`enforce_organization`, workspace
   authorization). This is about the client not asking the wrong question and
   not showing an answer to a question it has stopped asking.
--------------------------------------------------------------------------- */

const STORAGE_PREFIX = "atlas.scope";

export interface ScopeTruncation {
  /** Rows held on the client. */
  readonly loaded: number;
  /** Rows the server says exist. */
  readonly total: number;
  /** True when `loaded < total` -- the picker is showing a prefix (F15). */
  readonly truncated: boolean;
}

const NO_TRUNCATION: ScopeTruncation = { loaded: 0, total: 0, truncated: false };

export interface ScopeCounts {
  readonly workspaces: ScopeTruncation;
  readonly projects: ScopeTruncation;
  readonly datasources: ScopeTruncation;
}

export interface ScopeSelection {
  workspaceId: string;
  projectId: string;
  datasourceId: string;
  workspaces: WorkspaceRead[];
  projects: ProjectRead[];
  datasources: DataSourceRead[];
  bindings: SourceBindingRead[];
  visibleProjects: ProjectRead[];
  visibleDatasources: DataSourceRead[];
  setWorkspaceId: (id: string) => void;
  setProjectId: (id: string) => void;
  setDatasourceId: (id: string) => void;
  refresh: () => void;
  loading: boolean;
  error: string | null;
  /**
   * True once scope is resolved for the CURRENT organization.
   *
   * Screens that write should gate their controls on this. `loading === false`
   * is not the same thing: a failed load is also not loading, and scope for
   * the organization you just left is also not loading.
   *
   * Optional on the interface, always supplied by the provider: a test double
   * standing in for the whole selection should not have to invent one, and an
   * absent value reads as "not ready", which is the safe direction. The real
   * enforcement is in the provider's setters, which are inert until scope
   * resolves regardless of who reads this flag.
   */
  ready?: boolean;
  /** What the pickers are showing versus what exists (F15). Optional for the
   *  same reason as `ready`; absent means "no truncation known". */
  counts?: ScopeCounts;
}

const ScopeContext = createContext<ScopeSelection | null>(null);

function storageKey(orgId: string, key: string): string {
  return `${STORAGE_PREFIX}.${orgId}.${key}`;
}

function stored(orgId: string, key: string): string {
  try {
    return localStorage.getItem(storageKey(orgId, key)) ?? "";
  } catch {
    return "";
  }
}

/** Written only from the transition that chose the value, with that
 *  transition's own organization id. Never from an effect -- see defect 3. */
function persist(orgId: string, key: string, value: string): void {
  try {
    if (value) localStorage.setItem(storageKey(orgId, key), value);
    else localStorage.removeItem(storageKey(orgId, key));
  } catch {
    // Selection remains valid for this session when storage is unavailable.
  }
}

/* --- the state, and the one function that resolves a selection ----------- */

interface ScopeState {
  /** The organization every field below belongs to. */
  readonly orgId: string;
  readonly status: "loading" | "ready" | "error";
  readonly error: string | null;
  readonly workspaces: WorkspaceRead[];
  readonly projects: ProjectRead[];
  readonly datasources: DataSourceRead[];
  /** The workspace `bindings` were fetched for; "" when none. */
  readonly bindingsFor: string;
  readonly bindings: SourceBindingRead[];
  readonly workspaceId: string;
  readonly projectId: string;
  readonly datasourceId: string;
  readonly counts: ScopeCounts;
}

function emptyState(orgId: string): ScopeState {
  return {
    orgId,
    status: "loading",
    error: null,
    workspaces: [],
    projects: [],
    datasources: [],
    bindingsFor: "",
    bindings: [],
    workspaceId: "",
    projectId: "",
    datasourceId: "",
    counts: { workspaces: NO_TRUNCATION, projects: NO_TRUNCATION, datasources: NO_TRUNCATION },
  };
}

/** Sources a workspace's ACTIVE bindings reach. Without a workspace the user
 *  is browsing technically, so every source in the tenant is offered. */
function reachableDatasources(state: ScopeState): DataSourceRead[] {
  return datasourcesInScope({ ...state, projectId: "" }, state.datasources);
}

function visibleProjectsOf(state: ScopeState): ProjectRead[] {
  if (!state.workspaceId) return state.projects;
  const owning = new Set(reachableDatasources(state).map((item) => item.project_id));
  return state.projects.filter((item) => owning.has(item.id));
}

/**
 * The rule for "which of these sources is the active scope actually about".
 *
 * Exported because `useDatasourcePicker` has to apply it to rows the SERVER
 * selected -- a `q=` search answers from the whole fleet, and those results
 * still have to be cut down to what this workspace's bindings reach and this
 * project owns. Two copies of that rule would mean a searched picker offering
 * sources an unsearched one does not.
 */
export function datasourcesInScope(
  scope: Pick<ScopeSelection, "workspaceId" | "projectId" | "bindings">,
  items: readonly DataSourceRead[],
): DataSourceRead[] {
  let reachable = [...items];
  if (scope.workspaceId) {
    const active = new Set(
      scope.bindings.filter((item) => item.status === "ACTIVE").map((item) => item.datasource_id),
    );
    reachable = reachable.filter((item) => active.has(item.id));
  }
  return scope.projectId
    ? reachable.filter((item) => item.project_id === scope.projectId)
    : reachable;
}

function visibleDatasourcesOf(state: ScopeState): DataSourceRead[] {
  return datasourcesInScope(state, state.datasources);
}

/**
 * Resolve workspace, project and source together from one snapshot.
 *
 * `preferred` is what the user or their last session asked for; anything that
 * is not selectable in this snapshot is replaced by the first thing that is.
 * All three are decided here so the caller can adopt them in a single commit
 * -- that is the whole point (defect 2).
 */
function resolveSelection(
  base: ScopeState,
  preferred: { workspaceId?: string; projectId?: string; datasourceId?: string },
): ScopeState {
  const workspaceId = base.workspaces.some((item) => item.id === preferred.workspaceId)
    ? preferred.workspaceId!
    : (base.workspaces[0]?.id ?? "");

  // Bindings belonging to a different workspace must not influence visibility
  // while the new workspace's bindings are still in flight.
  const withWorkspace: ScopeState = {
    ...base,
    workspaceId,
    bindings: base.bindingsFor === workspaceId ? base.bindings : [],
    bindingsFor: base.bindingsFor === workspaceId ? base.bindingsFor : "",
  };

  const projects = visibleProjectsOf(withWorkspace);
  const projectId = projects.some((item) => item.id === preferred.projectId)
    ? preferred.projectId!
    : (projects[0]?.id ?? "");

  const withProject: ScopeState = { ...withWorkspace, projectId };
  const datasources = visibleDatasourcesOf(withProject);
  const datasourceId = datasources.some((item) => item.id === preferred.datasourceId)
    ? preferred.datasourceId!
    : (datasources[0]?.id ?? "");

  return { ...withProject, datasourceId };
}

/** Persist a resolved selection under the organization it belongs to. */
function persistSelection(state: ScopeState): void {
  persist(state.orgId, "workspace", state.workspaceId);
  persist(state.orgId, "project", state.projectId);
  persist(state.orgId, "datasource", state.datasourceId);
}

export function ScopeProvider({ children }: { children: ReactNode }) {
  const orgId = useOrgId();
  const [state, setState] = useState<ScopeState>(() => emptyState(orgId));
  const [revision, setRevision] = useState(0);

  /* Defect 1: invalidate DURING the render that observes the new
   * organization. React's supported "adjust state when a prop changes"
   * pattern -- an effect would let one paint through with the previous
   * tenant's ids under the new tenant's name. */
  if (state.orgId !== orgId) {
    setState(emptyState(orgId));
  }

  /* Every request carries the org (and workspace) it was issued for, and the
   * adopting `setState` re-checks that tag against the state it is updating.
   * That, not the AbortController, is what makes a late response harmless
   * (defect 4) -- abort covers re-runs, never a slow response that lands
   * after a fast one. */
  useEffect(() => {
    const ac = new AbortController();
    const requestedOrg = orgId;

    /* `allSettled`, not `all`, and the difference is a real defect rather than
     * a style preference. The three axes are authorized separately, so a
     * least-privilege principal routinely holds one and not another -- and
     * under `all` a single 403 rejected the whole bootstrap, emptying the two
     * lists the caller *could* read and leaving the scope picker blank. The
     * browser journey (R11-B11) hit it on every seat it defined, and had to
     * widen an Analyst identity with an extra role to get past it, which is
     * the opposite of what that suite is for.
     *
     * A refused axis now degrades to an empty list and the run continues.
     * `status: "error"` is reserved for every axis failing, because that is
     * the only case where there is no scope to pick at all. */
    Promise.allSettled([
      listOrgWorkspaces(requestedOrg, ac.signal),
      listOrgProjects(requestedOrg, ac.signal),
      listOrgDatasources(requestedOrg, ac.signal),
    ])
      .then(async (settled) => {
        if (ac.signal.aborted) return;
        const firstRejection = settled.find((outcome) => outcome.status === "rejected");
        if (firstRejection?.status === "rejected" && settled.every((o) => o.status === "rejected")) {
          throw firstRejection.reason;
        }
        const [workspaceList, projectList, datasourceList] = settled.map((outcome) =>
          outcome.status === "fulfilled"
            ? outcome.value
            : { items: [], total: 0, truncated: false },
        ) as [
          Awaited<ReturnType<typeof listOrgWorkspaces>>,
          Awaited<ReturnType<typeof listOrgProjects>>,
          Awaited<ReturnType<typeof listOrgDatasources>>,
        ];
        const refused = settled
          .map((outcome, index) =>
            outcome.status === "rejected" ? ["workspaces", "projects", "datasources"][index] : null,
          )
          .filter((axis): axis is string => axis !== null);

        const remembered = {
          workspaceId: stored(requestedOrg, "workspace"),
          projectId: stored(requestedOrg, "project"),
          datasourceId: stored(requestedOrg, "datasource"),
        };

        /* F15: a remembered or deep-linked selection can sit past the pages
         * the picker preloaded. Resolve it by id and splice it in, so the
         * user keeps their scope instead of being silently moved to whatever
         * happened to be first on page one. */
        const [workspace, project, datasource] = await Promise.all([
          resolveMissing(workspaceList.items, remembered.workspaceId, workspaceList.truncated, (id) =>
            findOrgWorkspaceById(requestedOrg, id, ac.signal),
          ),
          resolveMissing(projectList.items, remembered.projectId, projectList.truncated, (id) =>
            findOrgProjectById(requestedOrg, id, ac.signal),
          ),
          resolveMissing(datasourceList.items, remembered.datasourceId, datasourceList.truncated, (id) =>
            findOrgDatasourceById(requestedOrg, id, ac.signal),
          ),
        ]);
        if (ac.signal.aborted) return;

        setState((previous) => {
          if (previous.orgId !== requestedOrg) return previous;
          const loaded: ScopeState = {
            ...previous,
            status: "ready",
            // Partly refused is still usable, and saying which axis was
            // refused is what lets a reader tell "you have no workspaces" from
            // "you may not list workspaces".
            error:
              refused.length > 0
                ? `no access to ${refused.join(" or ")} in this organization`
                : null,
            workspaces: workspace ? [...workspaceList.items, workspace] : workspaceList.items,
            projects: project ? [...projectList.items, project] : projectList.items,
            datasources: datasource ? [...datasourceList.items, datasource] : datasourceList.items,
            counts: {
              workspaces: countsOf(workspaceList),
              projects: countsOf(projectList),
              datasources: countsOf(datasourceList),
            },
          };
          const resolved = resolveSelection(loaded, remembered);
          persistSelection(resolved);
          return resolved;
        });
      })
      .catch((reason: unknown) => {
        if (ac.signal.aborted) return;
        setState((previous) =>
          previous.orgId !== requestedOrg
            ? previous
            : {
                ...previous,
                status: "error",
                error: reason instanceof Error ? reason.message : String(reason),
              },
        );
      });

    return () => ac.abort();
  }, [orgId, revision]);

  /* Bindings are tagged with BOTH ids: a response for workspace W of
   * organization A must not be adopted into workspace W' of organization B,
   * and rapid workspace switching produces exactly that race. */
  const workspaceId = state.workspaceId;
  useEffect(() => {
    if (!workspaceId) return;
    const ac = new AbortController();
    const requestedOrg = orgId;
    const requestedWorkspace = workspaceId;

    fetchWorkspaceSourceBindings(requestedWorkspace, ac.signal)
      .then((page) => {
        if (ac.signal.aborted) return;
        setState((previous) => {
          if (previous.orgId !== requestedOrg || previous.workspaceId !== requestedWorkspace) {
            return previous;
          }
          const withBindings: ScopeState = {
            ...previous,
            bindings: page.items,
            bindingsFor: requestedWorkspace,
          };
          // Bindings change what is reachable, so the project and source have
          // to be re-validated in the SAME commit that adopts them.
          const resolved = resolveSelection(withBindings, {
            workspaceId: previous.workspaceId,
            projectId: previous.projectId,
            datasourceId: previous.datasourceId,
          });
          persistSelection(resolved);
          return resolved;
        });
      })
      .catch((reason: unknown) => {
        if (ac.signal.aborted) return;
        setState((previous) => {
          if (previous.orgId !== requestedOrg || previous.workspaceId !== requestedWorkspace) {
            return previous;
          }
          /* Mark the bindings resolved-as-empty rather than leaving them
           * unknown. Unknown would hold `ready` false forever and freeze the
           * pickers; empty is the fail-closed reading of "this workspace
           * reaches nothing we could confirm", and the error is on screen. */
          return {
            ...previous,
            bindings: [],
            bindingsFor: requestedWorkspace,
            error: reason instanceof Error ? reason.message : String(reason),
          };
        });
      });

    return () => ac.abort();
  }, [orgId, workspaceId, revision]);

  /* Scope is not resolved until the reachable-source set is known. With a
   * workspace selected but its bindings still in flight, the project and
   * source lists are empty for a reason that has nothing to do with what the
   * user may see -- writing a selection out of that moment is exactly the
   * half-resolved state T12 is about. */
  const ready =
    state.status === "ready" && (!state.workspaceId || state.bindingsFor === state.workspaceId);

  /* Defect 5: a setter that fires before scope resolves would be writing a
   * selection into a snapshot that the pending load is about to replace. The
   * guard is loud in development because the caller should be disabling its
   * control, not relying on the no-op. */
  const readyRef = useRef(ready);
  readyRef.current = ready;
  const guard = useCallback((what: string): boolean => {
    if (readyRef.current) return true;
    if (import.meta.env?.DEV) {
      console.warn(`scope: ignored ${what} while scope was still resolving.`);
    }
    return false;
  }, []);

  const setWorkspaceId = useCallback(
    (id: string) => {
      if (!guard("setWorkspaceId")) return;
      setState((previous) => {
        // Project and source are re-resolved from scratch: the workspace is
        // what decides which of them are reachable at all.
        const resolved = resolveSelection({ ...previous, bindings: [], bindingsFor: "" }, {
          workspaceId: id,
        });
        persistSelection(resolved);
        return resolved;
      });
    },
    [guard],
  );

  const setProjectId = useCallback(
    (id: string) => {
      if (!guard("setProjectId")) return;
      setState((previous) => {
        const resolved = resolveSelection(previous, {
          workspaceId: previous.workspaceId,
          projectId: id,
        });
        persistSelection(resolved);
        return resolved;
      });
    },
    [guard],
  );

  const setDatasourceId = useCallback(
    (id: string) => {
      if (!guard("setDatasourceId")) return;
      setState((previous) => {
        const resolved = resolveSelection(previous, {
          workspaceId: previous.workspaceId,
          projectId: previous.projectId,
          datasourceId: id,
        });
        persistSelection(resolved);
        return resolved;
      });
    },
    [guard],
  );

  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  const visibleProjects = useMemo(() => visibleProjectsOf(state), [state]);
  const visibleDatasources = useMemo(() => visibleDatasourcesOf(state), [state]);

  const value = useMemo<ScopeSelection>(
    () => ({
      workspaceId: state.workspaceId,
      projectId: state.projectId,
      datasourceId: state.datasourceId,
      workspaces: state.workspaces,
      projects: state.projects,
      datasources: state.datasources,
      bindings: state.bindings,
      visibleProjects,
      visibleDatasources,
      setWorkspaceId,
      setProjectId,
      setDatasourceId,
      refresh,
      loading: state.status === "loading",
      error: state.error,
      ready,
      counts: state.counts,
    }),
    [state, visibleProjects, visibleDatasources, setWorkspaceId, setProjectId, setDatasourceId, refresh, ready],
  );

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}

function countsOf(list: { items: unknown[]; total: number; truncated: boolean }): ScopeTruncation {
  return { loaded: list.items.length, total: list.total, truncated: list.truncated };
}

/**
 * Resolve `id` when it is not in the loaded prefix.
 *
 * Returns null when there is nothing to do: no id, already present, or the
 * list is complete so the id genuinely does not exist. A failed lookup is
 * also null -- a picker that cannot resolve a remembered id must fall back to
 * a valid selection, not break.
 */
async function resolveMissing<T extends { id: string }>(
  items: T[],
  id: string,
  truncated: boolean,
  lookup: (id: string) => Promise<T | null>,
): Promise<T | null> {
  if (!id || !truncated) return null;
  if (items.some((item) => item.id === id)) return null;
  try {
    return await lookup(id);
  } catch {
    return null;
  }
}

export function useScopeSelection(): ScopeSelection | null {
  return useContext(ScopeContext);
}
