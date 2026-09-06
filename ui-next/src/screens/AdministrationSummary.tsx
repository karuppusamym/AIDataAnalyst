import type {
  DataSourceRead,
  LineOfBusinessRead,
  ProjectRead,
  SourceBindingRead,
  WorkspaceRead,
} from "../lib/types";
import { Empty } from "../components/primitives";

/* ---------------------------------------------------------------------------
   The read-only rail: where this organization has got to, and what it
   currently contains.

   Separate from the forms because it answers a different question. The forms
   know one route each and nothing about the others; this panel knows only the
   relationships -- which workspace has how many approved bindings, which
   project owns which sources, how many projects each line of business
   classifies -- and makes no requests at all. Every count here is derived
   from the same five lists the screen loaded once, so the rail can never
   disagree with the forms about what exists.
--------------------------------------------------------------------------- */

export function ProgressPanel({
  orgLabel,
  workspaceCount,
  lobCount,
  projectCount,
  datasourceCount,
  activeBindingCount,
}: {
  orgLabel: string;
  workspaceCount: number;
  lobCount: number;
  projectCount: number;
  datasourceCount: number;
  activeBindingCount: number;
}) {
  const steps: { n: number; label: string; detail: string; done: boolean }[] = [
    { n: 1, label: "Organization", detail: orgLabel, done: true },
    {
      n: 2,
      label: "Workspace",
      detail: workspaceCount > 0 ? `${workspaceCount} access boundary` : "Access boundary",
      done: workspaceCount > 0,
    },
    {
      n: 3,
      label: "Project",
      detail:
        projectCount > 0
          ? `${projectCount} application${projectCount === 1 ? "" : "s"}`
          : `${lobCount} business classifications`,
      done: projectCount > 0 && lobCount > 0,
    },
    {
      n: 4,
      label: "Data source",
      detail: datasourceCount > 0 ? `${datasourceCount} recorded` : "Read-only identity",
      done: datasourceCount > 0,
    },
    {
      n: 5,
      label: "Active binding",
      detail: activeBindingCount > 0 ? `${activeBindingCount} approved` : "Independent approval required",
      done: activeBindingCount > 0,
    },
  ];
  return (
    <div className="adminprogress" aria-label="Onboarding sequence and progress">
      {steps.map((s) => (
        <div key={s.n} className={`adminprogress__step${s.done ? " adminprogress__step--done" : ""}`}>
          <span className="adminprogress__n">{s.n}</span>
          <div>
            <strong>{s.label}</strong>
            <small>{s.detail}</small>
          </div>
        </div>
      ))}
    </div>
  );
}

export function ScopeSummary({
  lobs,
  projects,
  datasources,
  workspaces,
  bindings,
}: {
  lobs: LineOfBusinessRead[];
  projects: ProjectRead[];
  datasources: DataSourceRead[];
  workspaces: WorkspaceRead[];
  bindings: SourceBindingRead[];
}) {
  if (lobs.length === 0 && projects.length === 0 && datasources.length === 0) {
    return <Empty title="No hierarchy yet" hint="Create a workspace and a project/application to continue." />;
  }
  return (
    <div className="adminaxes" aria-label="Current access and technical structure">
      <section>
        <h3>Access axis</h3>
        <table>
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Bound sources</th>
            </tr>
          </thead>
          <tbody>
            {workspaces.map((workspace) => {
              const related = bindings.filter((item) => item.workspace_id === workspace.id);
              return (
                <tr key={workspace.id}>
                  <td>
                    <b>{workspace.name}</b>
                    <small>{workspace.purpose || workspace.slug}</small>
                  </td>
                  <td>
                    {related.filter((item) => item.status === "ACTIVE").length} active
                    <small>{related.filter((item) => item.status !== "ACTIVE").length} awaiting/restricted</small>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <section>
        <h3>Technical axis</h3>
        <table>
          <thead>
            <tr>
              <th>Project / application</th>
              <th>Owned sources</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((project) => {
              const owned = datasources.filter((item) => item.project_id === project.id);
              return (
                <tr key={project.id}>
                  <td>
                    <b>{project.name}</b>
                    <small>{project.slug}</small>
                  </td>
                  <td>
                    {owned.length}
                    <small>{owned.map((item) => item.name).join(", ") || "Register a source"}</small>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <p className="adminaxes__classification">Business classification</p>
      <ul className="adminhier" aria-label="Current business classifications">
        {lobs.map((lob) => {
          const projectCount = projects.filter((p) => p.line_of_business_id === lob.id).length;
          const sourceCount = datasources.filter((d) => d.line_of_business_id === lob.id).length;
          return (
            <li key={lob.id} className="adminhier__row">
              <strong>
                {lob.name} <span className="adminhier__code">({lob.code})</span>
              </strong>
              <small>
                {`${projectCount} project${projectCount === 1 ? "" : "s"} · ${sourceCount} source${
                  sourceCount === 1 ? "" : "s"
                }`}
              </small>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
