import { useState } from "react";
import type {
  BiConnectionRead,
  DataSourceRead,
  ProjectRead,
  SourceBindingRead,
  WorkspaceMembershipRead,
  WorkspaceRead,
} from "../lib/types";
import {
  listOrgDatasources,
  fetchOrgProjects,
  fetchOrgWorkspaces,
  fetchProjectBiConnections,
  fetchWorkspaceMembers,
  fetchWorkspaceSourceBindings,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useScopeSelection } from "../lib/scope";
import { Empty, ErrorState, Field, Pill } from "../components/primitives";
import { useAsyncResource } from "../components/screenState";
import { AddMemberForm, MembersPanel, PendingBindingsPanel } from "./WorkspaceAccessMembership";
import { BiConnectionsPanel, CreateBiConnectionForm } from "./WorkspaceAccessBi";
import "./WorkspaceAccessScreen.css";

/* ---------------------------------------------------------------------------
   Workspace access -- nav id `workspace-access`, the slice of the legacy
   Enterprise Control Center's Access tab (`ui/scripts/features/control-center.js`
   `renderAccess`/`renderBi`, `loadWorkspaceDetail`/`loadBiConnections`, the
   `#workspace-member-form`/binding-decision click handler/`#bi-connection-form`/
   `#bi-import-form` submit handlers) this screen owns: workspace *members*,
   the *decision* half of the maker-checker source-binding flow, and BI/Tableau
   lineage connections. Workspace creation and source-binding *request* already
   live in `AdministrationScreen` (`createWorkspace`/`requestSourceBinding`) --
   this screen reads the same `fetchOrgWorkspaces`/`fetchWorkspaceSourceBindings`
   but never creates a workspace or requests a binding itself.

   What remains in this file is the part neither section can own: three reads
   keyed on two independent pickers. The workspace picker drives membership and
   pending bindings (`WorkspaceAccessMembership.tsx`); the project picker
   drives BI connections (`WorkspaceAccessBi.tsx`). Keeping the reads together
   is what makes the picker boundary legible -- each `useAsyncResource` names
   the selection it is keyed on, so a picker change can never leave the other
   section showing the previous selection's rows.

   Both pickers are local to this screen, not the shared `ScopeSelection`
   (`lib/scope.tsx`) -- that context exists to scope *reads* the rest of the
   app makes against one workspace/project/datasource at a time, while this
   screen's job is to administer *every* pending binding and BI connection
   under an org, one workspace/project at a time by choice, not by the shell's
   ambient selection. It does call `useScopeSelection()?.refresh()` after a
   binding decision, though: approving a binding changes which datasources
   `ScopeProvider` treats as accessible (its own `status === "ACTIVE"` filter),
   and every other screen reads that same provider.
--------------------------------------------------------------------------- */

interface Directory {
  readonly workspaces: WorkspaceRead[];
  readonly projects: ProjectRead[];
  readonly datasources: DataSourceRead[];
}

interface WorkspaceDetail {
  readonly members: WorkspaceMembershipRead[];
  readonly pendingBindings: SourceBindingRead[];
}

const NO_DIRECTORY: Directory = { workspaces: [], projects: [], datasources: [] };
const NO_DETAIL: WorkspaceDetail = { members: [], pendingBindings: [] };

export function WorkspaceAccessScreen() {
  const orgId = useOrgId();
  const sharedScope = useScopeSelection();

  const [workspaceId, setWorkspaceId] = useState("");
  const [projectId, setProjectId] = useState("");

  const directory = useAsyncResource<Directory>(
    async (signal) => {
      const [workspacePage, projectPage, dsPage] = await Promise.all([
        fetchOrgWorkspaces(orgId, signal),
        fetchOrgProjects(orgId, signal),
        listOrgDatasources(orgId, signal),
      ]);
      // Default each picker to its first entry, and keep a selection that is
      // still valid: an org switch must not leave a workspace id from the
      // previous org selected, which would read as "this workspace has no
      // members" rather than "you are looking at the wrong org". Skipped for
      // a superseded response, which must not move a picker the user has
      // already changed.
      if (signal.aborted) return NO_DIRECTORY;
      setWorkspaceId((current) =>
        workspacePage.items.some((item) => item.id === current) ? current : (workspacePage.items[0]?.id ?? ""),
      );
      setProjectId((current) =>
        projectPage.items.some((item) => item.id === current) ? current : (projectPage.items[0]?.id ?? ""),
      );
      return {
        workspaces: workspacePage.items,
        projects: projectPage.items,
        datasources: dsPage.items,
      };
    },
    [orgId],
  );

  const { workspaces, projects, datasources } = directory.data ?? NO_DIRECTORY;

  const detail = useAsyncResource<WorkspaceDetail>(
    async (signal) => {
      const [memberPage, bindingPage] = await Promise.all([
        fetchWorkspaceMembers(workspaceId, signal),
        fetchWorkspaceSourceBindings(workspaceId, signal),
      ]);
      return {
        members: memberPage.items,
        pendingBindings: bindingPage.items.filter((item) => item.status === "PENDING_APPROVAL"),
      };
    },
    [workspaceId],
    { enabled: Boolean(workspaceId) },
  );

  const { members, pendingBindings } = detail.data ?? NO_DETAIL;

  const biConnections = useAsyncResource<BiConnectionRead[]>(
    async (signal) => (await fetchProjectBiConnections(projectId, undefined, signal)).items,
    [projectId],
    { enabled: Boolean(projectId) },
  );

  const selectedWorkspace = workspaces.find((item) => item.id === workspaceId) ?? null;
  const selectedProject = projects.find((item) => item.id === projectId) ?? null;

  return (
    <div className="wsaccess">
      <header className="wsaccess__head">
        <div>
          <h1 className="wsaccess__h1">Workspace access</h1>
          <p className="wsaccess__lede">
            Manage who belongs to a workspace, decide pending source-binding requests, and connect BI
            tools so Tableau/Power BI/Looker lineage joins the catalog.
          </p>
        </div>
      </header>

      {directory.error ? (
        <ErrorState title="Workspaces could not be loaded" detail={directory.error} onRetry={directory.reload} />
      ) : directory.loading ? (
        <p className="wsaccess-panel__note">Loading...</p>
      ) : (
        <div className="wsaccess__main">
          <section className="wsaccess__section">
            <div className="wsaccess__sectionhead">
              <Field label="Workspace">
                <select value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)}>
                  <option value="">Select a workspace...</option>
                  {workspaces.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </Field>
              {selectedWorkspace ? <Pill tone="mute">{selectedWorkspace.slug}</Pill> : null}
            </div>

            {!workspaceId ? (
              <Empty title="No workspace selected" hint="Choose a workspace to manage its members and bindings." />
            ) : detail.error ? (
              <ErrorState
                title="Workspace detail could not be loaded"
                detail={detail.error}
                onRetry={detail.reload}
              />
            ) : detail.loading ? (
              <p className="wsaccess-panel__note">Loading...</p>
            ) : (
              <div className="wsaccess__cols">
                <article className="wsaccess-panel">
                  <div className="wsaccess-panel__head">
                    <p className="wsaccess-panel__eyebrow">MEMBERSHIP</p>
                    <h2 className="wsaccess-panel__h2">Members</h2>
                  </div>
                  <MembersPanel members={members} />
                </article>
                <AddMemberForm
                  workspaceId={workspaceId}
                  onAdded={(member) =>
                    detail.setData((previous) => ({
                      ...(previous ?? NO_DETAIL),
                      members: [...(previous ?? NO_DETAIL).members, member],
                    }))
                  }
                />
                <article className="wsaccess-panel wsaccess-panel--wide">
                  <div className="wsaccess-panel__head">
                    <p className="wsaccess-panel__eyebrow">MAKER-CHECKER</p>
                    <h2 className="wsaccess-panel__h2">Pending source-binding requests</h2>
                    <Pill tone="warn">{pendingBindings.length}</Pill>
                  </div>
                  <PendingBindingsPanel
                    bindings={pendingBindings}
                    datasources={datasources}
                    onDecided={(decided) => {
                      // A decided request is no longer pending, whichever way
                      // it went; the reviewer's queue must shrink by one.
                      detail.setData((previous) => ({
                        ...(previous ?? NO_DETAIL),
                        pendingBindings: (previous ?? NO_DETAIL).pendingBindings.filter(
                          (item) => item.id !== decided.id,
                        ),
                      }));
                      sharedScope?.refresh();
                    }}
                  />
                </article>
              </div>
            )}
          </section>

          <section className="wsaccess__section">
            <div className="wsaccess__sectionhead">
              <Field label="Project">
                <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                  <option value="">Select a project...</option>
                  {projects.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </Field>
              {selectedProject ? <Pill tone="mute">{selectedProject.slug}</Pill> : null}
            </div>

            {!projectId ? (
              <Empty title="No project selected" hint="Choose a project to manage its BI connections." />
            ) : biConnections.error ? (
              <ErrorState
                title="BI connections could not be loaded"
                detail={biConnections.error}
                onRetry={biConnections.reload}
              />
            ) : biConnections.loading ? (
              <p className="wsaccess-panel__note">Loading...</p>
            ) : (
              <div className="wsaccess__cols">
                <article className="wsaccess-panel wsaccess-panel--wide">
                  <div className="wsaccess-panel__head">
                    <p className="wsaccess-panel__eyebrow">BI / TABLEAU LINEAGE</p>
                    <h2 className="wsaccess-panel__h2">Connections</h2>
                  </div>
                  <BiConnectionsPanel
                    connections={biConnections.data ?? []}
                    onImported={() => {
                      /* the import result is shown inline by ImportArtifactForm itself */
                    }}
                  />
                </article>
                <CreateBiConnectionForm
                  projectId={projectId}
                  datasources={datasources}
                  onCreated={(connection) =>
                    biConnections.setData((previous) => [...(previous ?? []), connection])
                  }
                />
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
