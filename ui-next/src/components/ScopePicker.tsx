import { useOrgSelection } from "../lib/org";
import { useScopeSelection } from "../lib/scope";

export function ScopePicker() {
  const org = useOrgSelection();
  const scope = useScopeSelection();
  if (!org || !scope) return null;

  const currentBinding = scope.bindings.find((item) => item.datasource_id === scope.datasourceId);
  const hasWorkspaceButNoSources = Boolean(scope.workspaceId) && scope.visibleDatasources.length === 0;

  return (
    <div className="scopepicker" data-testid="scope-picker">
      <p className="scopepicker__eyebrow">ACTIVE DATA SCOPE</p>
      <label htmlFor="scope-org">Organization</label>
      <select id="scope-org" value={org.orgId} onChange={(event) => org.setOrgId(event.target.value)} disabled={org.loading}>
        {org.organizations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>

      <label htmlFor="scope-workspace">Workspace <span>access</span></label>
      <select id="scope-workspace" value={scope.workspaceId} onChange={(event) => scope.setWorkspaceId(event.target.value)} disabled={scope.loading || scope.workspaces.length === 0}>
        {scope.workspaces.length === 0 ? <option value="">Not configured</option> : null}
        {scope.workspaces.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>

      <label htmlFor="scope-project">Project <span>application</span></label>
      <select id="scope-project" value={scope.projectId} onChange={(event) => scope.setProjectId(event.target.value)} disabled={scope.loading || scope.visibleProjects.length === 0}>
        {scope.visibleProjects.length === 0 ? <option value="">No accessible project</option> : null}
        {scope.visibleProjects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>

      <label htmlFor="scope-source">Source <span>technical</span></label>
      <select id="scope-source" value={scope.datasourceId} onChange={(event) => scope.setDatasourceId(event.target.value)} disabled={scope.loading || scope.visibleDatasources.length === 0}>
        {scope.visibleDatasources.length === 0 ? <option value="">No active binding</option> : null}
        {scope.visibleDatasources.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>

      <p className={`scopepicker__status${hasWorkspaceButNoSources ? " scopepicker__status--warn" : ""}`}>
        {scope.error
          ? "Scope could not be loaded"
          : hasWorkspaceButNoSources
            ? "Workspace has no active source binding"
            : currentBinding
              ? `${currentBinding.status.toLowerCase()} binding · ${currentBinding.masking_profile.toLowerCase()} masking`
              : "Technical browsing without a workspace binding"}
      </p>
    </div>
  );
}

