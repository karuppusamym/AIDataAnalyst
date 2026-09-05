import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { fetchOrgDatasources, fetchOrgProjects, fetchOrgWorkspaces, fetchWorkspaceSourceBindings } from "./api";
import { useOrgId } from "./org";
import type { DataSourceRead, ProjectRead, SourceBindingRead, WorkspaceRead } from "./types";

const STORAGE_PREFIX = "atlas.scope";

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
}

const ScopeContext = createContext<ScopeSelection | null>(null);

function stored(orgId: string, key: string): string {
  try {
    return localStorage.getItem(`${STORAGE_PREFIX}.${orgId}.${key}`) ?? "";
  } catch {
    return "";
  }
}

function persist(orgId: string, key: string, value: string): void {
  try {
    if (value) localStorage.setItem(`${STORAGE_PREFIX}.${orgId}.${key}`, value);
    else localStorage.removeItem(`${STORAGE_PREFIX}.${orgId}.${key}`);
  } catch {
    // Selection remains valid for this session when storage is unavailable.
  }
}

export function ScopeProvider({ children }: { children: ReactNode }) {
  const orgId = useOrgId();
  const [workspaces, setWorkspaces] = useState<WorkspaceRead[]>([]);
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);
  const [bindings, setBindings] = useState<SourceBindingRead[]>([]);
  const [workspaceId, setWorkspaceIdState] = useState("");
  const [projectId, setProjectIdState] = useState("");
  const [datasourceId, setDatasourceIdState] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      fetchOrgWorkspaces(orgId, ac.signal),
      fetchOrgProjects(orgId, ac.signal),
      fetchOrgDatasources(orgId, ac.signal),
    ])
      .then(([workspacePage, projectPage, datasourcePage]) => {
        if (ac.signal.aborted) return;
        setWorkspaces(workspacePage.items);
        setProjects(projectPage.items);
        setDatasources(datasourcePage.items);
        setWorkspaceIdState((current) => {
          const candidate = current || stored(orgId, "workspace");
          return workspacePage.items.some((item) => item.id === candidate)
            ? candidate
            : (workspacePage.items[0]?.id ?? "");
        });
      })
      .catch((reason: unknown) => {
        if (!ac.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false);
      });
    return () => ac.abort();
  }, [orgId, revision]);

  useEffect(() => {
    if (!workspaceId) {
      setBindings([]);
      return;
    }
    const ac = new AbortController();
    fetchWorkspaceSourceBindings(workspaceId, ac.signal)
      .then((page) => {
        if (!ac.signal.aborted) setBindings(page.items);
      })
      .catch((reason: unknown) => {
        if (!ac.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => ac.abort();
  }, [workspaceId, revision]);

  const accessibleIds = useMemo(
    () => new Set(bindings.filter((item) => item.status === "ACTIVE").map((item) => item.datasource_id)),
    [bindings],
  );
  const visibleDatasourcesBeforeProject = useMemo(
    () => workspaceId ? datasources.filter((item) => accessibleIds.has(item.id)) : datasources,
    [workspaceId, datasources, accessibleIds],
  );
  const visibleProjects = useMemo(() => {
    if (!workspaceId) return projects;
    const projectIds = new Set(visibleDatasourcesBeforeProject.map((item) => item.project_id));
    return projects.filter((item) => projectIds.has(item.id));
  }, [workspaceId, projects, visibleDatasourcesBeforeProject]);
  const visibleDatasources = useMemo(
    () => projectId
      ? visibleDatasourcesBeforeProject.filter((item) => item.project_id === projectId)
      : visibleDatasourcesBeforeProject,
    [projectId, visibleDatasourcesBeforeProject],
  );

  useEffect(() => {
    const remembered = stored(orgId, "project");
    setProjectIdState((current) => {
      const candidate = current || remembered;
      return visibleProjects.some((item) => item.id === candidate)
        ? candidate
        : (visibleProjects[0]?.id ?? "");
    });
  }, [orgId, visibleProjects]);

  useEffect(() => {
    const remembered = stored(orgId, "datasource");
    setDatasourceIdState((current) => {
      const candidate = current || remembered;
      return visibleDatasources.some((item) => item.id === candidate)
        ? candidate
        : (visibleDatasources[0]?.id ?? "");
    });
  }, [orgId, visibleDatasources]);

  useEffect(() => persist(orgId, "workspace", workspaceId), [orgId, workspaceId]);
  useEffect(() => persist(orgId, "project", projectId), [orgId, projectId]);
  useEffect(() => persist(orgId, "datasource", datasourceId), [orgId, datasourceId]);

  const setWorkspaceId = useCallback((id: string) => {
    setWorkspaceIdState(id);
    setProjectIdState("");
    setDatasourceIdState("");
  }, []);
  const setProjectId = useCallback((id: string) => {
    setProjectIdState(id);
    setDatasourceIdState("");
  }, []);
  const setDatasourceId = useCallback((id: string) => setDatasourceIdState(id), []);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  const value = useMemo<ScopeSelection>(() => ({
    workspaceId,
    projectId,
    datasourceId,
    workspaces,
    projects,
    datasources,
    bindings,
    visibleProjects,
    visibleDatasources,
    setWorkspaceId,
    setProjectId,
    setDatasourceId,
    refresh,
    loading,
    error,
  }), [workspaceId, projectId, datasourceId, workspaces, projects, datasources, bindings, visibleProjects, visibleDatasources, setWorkspaceId, setProjectId, setDatasourceId, refresh, loading, error]);

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}

export function useScopeSelection(): ScopeSelection | null {
  return useContext(ScopeContext);
}

