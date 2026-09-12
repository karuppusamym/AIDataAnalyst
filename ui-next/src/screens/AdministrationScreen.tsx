import type {
  DataSourceRead,
  LineOfBusinessRead,
  ProjectRead,
  SourceBindingRead,
  WorkspaceRead,
} from "../lib/types";
import {
  listOrgDatasources,
  fetchOrgLinesOfBusiness,
  fetchOrgProjects,
  fetchOrgWorkspaces,
  fetchWorkspaceSourceBindings,
} from "../lib/api";
import { useOrgId, useOrgSelection } from "../lib/org";
import { useScopeSelection } from "../lib/scope";
import { ErrorState } from "../components/primitives";
import { useAsyncResource } from "../components/screenState";
import {
  AddLineOfBusinessForm,
  AddProjectForm,
  BindSourceForm,
  CreateOrganizationForm,
  CreateWorkspaceForm,
  RegisterDatasourceForm,
} from "./AdministrationForms";
import { ProgressPanel, ScopeSummary } from "./AdministrationSummary";
import "./AdministrationScreen.css";

/* ---------------------------------------------------------------------------
   Administration -- nav id `administration`, the tenant/onboarding wizard
   ported from the legacy portal's `administration-view` (`ui/index.html`,
   forms bound in `ui/app.js`'s `bindDirectEvents`). Six writes, each the
   real, already-merged route the legacy portal itself posts to -- not an
   invented "setup" API:

     1. Create organization   POST /v1/organizations                (api.py:584)
     2. Create workspace       POST /v1/organizations/{id}/workspaces
     3. Add line of business  POST /v1/organizations/{id}/lines-of-business
                                                                       (api.py:677)
     4. Add project            POST /v1/lines-of-business/{lob_id}/projects
                                                                       (api.py:901)
     5. Register data source   POST /v1/projects/{project_id}/datasources
                                                                       (api.py:1021)
     6. Request binding        POST /v1/workspaces/{id}/source-bindings

   All six live in `AdministrationForms.tsx`; the read-only rail lives in
   `AdministrationSummary.tsx`. What remains here is the one thing neither of
   those can own: the single organization-wide read every form writes into and
   the rail counts from, so a created object appears in the summary without a
   round trip and the next `reload` reconciles it with the server.

   Reads: `fetchOrgProjects` and `listOrgDatasources` (already used by
   `SemanticsScreen`/`SourcesScreen`) cover this screen's project and
   datasource lists; `fetchOrgLinesOfBusiness` (new, `api.py:463`) is the one
   read nothing existing exposed. All are scoped to `useOrgId()`, the same
   shared organization selection every migrated screen reads (see `OrgPicker`
   in the shell nav) -- unlike the legacy portal, this screen has no
   organization `<select>` of its own for the line-of-business/project/
   datasource forms; they act on the organization currently selected in the
   shell, exactly like `SourcesScreen`/`AskScreen`'s datasource pickers act on
   it, not on a second independent choice.

   Scope cuts, stated rather than silently dropped:
     - The legacy screen's "Transformation metadata surfaces" integration-policy
       form (`#integration-policy-form`, `PUT /organizations/{id}/integration-policy`)
       is an unrelated write (dbt/OpenLineage/Airflow reservation flags, not
       tenant hierarchy) -- left out; the task scoped this port to the
       onboarding forms plus the read-only summary.
     - No data-domain picker: `ProjectCreate.data_domain_id` is left unset on
       every project this screen creates, so `create_project`'s own
       `resolve_domain` falls back to the line of business's default domain
       (api.py:922) -- the same "no explicit domain" path the legacy form
       takes (it has no data-domain field either).
     - No post-registration connectivity test: the legacy portal chains
       `POST /datasources/{id}/test` after registration; this screen registers
       only, leaving the test as a separate, deliberate step (already the
       Sources screen's `fetchDatasourceHealth`'s job to reflect, not this
       wizard's).
     - Newly created organizations are not retroactively added to the shell's
       `OrgPicker` list: `OrgProvider` (`lib/org.tsx`) fetches `fetchOrganizations`
       once, on mount, with no exposed refetch -- an existing, honest limitation
       of that shared context this screen does not attempt to work around by
       duplicating org-list state. A freshly created organization becomes
       selectable after the next full reload; the confirmation message says so.
--------------------------------------------------------------------------- */

interface Hierarchy {
  readonly lobs: LineOfBusinessRead[];
  readonly projects: ProjectRead[];
  readonly datasources: DataSourceRead[];
  readonly workspaces: WorkspaceRead[];
  readonly bindings: SourceBindingRead[];
}

const EMPTY: Hierarchy = { lobs: [], projects: [], datasources: [], workspaces: [], bindings: [] };

export function AdministrationScreen() {
  const ORG = useOrgId();
  const orgSelection = useOrgSelection();
  const sharedScope = useScopeSelection();
  const orgLabel = orgSelection?.organizations.find((o) => o.id === ORG)?.name ?? ORG;

  /* One read for the whole screen. The binding list is a second round trip
     that cannot start until the workspaces are known, so it is sequenced
     inside the same request rather than raced as a separate resource -- a
     rail that showed workspaces but not yet their bindings would report "0
     approved" for an organization that has some. */
  const hierarchy = useAsyncResource<Hierarchy>(
    async (signal) => {
      const [lobPage, projectPage, dsPage, workspacePage] = await Promise.all([
        fetchOrgLinesOfBusiness(ORG, signal),
        fetchOrgProjects(ORG, signal),
        listOrgDatasources(ORG, signal),
        fetchOrgWorkspaces(ORG, signal),
      ]);
      const bindingPages = await Promise.all(
        workspacePage.items.map((workspace) => fetchWorkspaceSourceBindings(workspace.id, signal)),
      );
      return {
        lobs: lobPage.items,
        projects: projectPage.items,
        datasources: dsPage.items,
        workspaces: workspacePage.items,
        bindings: bindingPages.flatMap((page) => page.items),
      };
    },
    [ORG],
  );

  const { lobs, projects, datasources, workspaces, bindings } = hierarchy.data ?? EMPTY;

  /** Show a just-created object immediately; `reload` still decides the truth. */
  const append = <K extends keyof Hierarchy>(key: K, item: Hierarchy[K][number]) =>
    hierarchy.setData((previous) => {
      const base = previous ?? EMPTY;
      // The computed key defeats inference across the five list types; the
      // signature above is what actually constrains `item`.
      return { ...base, [key]: [...base[key], item] } as Hierarchy;
    });

  return (
    <div className="adminscreen">
      <header className="adminscreen__head">
        <div>
          <h1 className="adminscreen__h1">Administration</h1>
          <p className="adminscreen__lede">
            Configure access and technical ownership deliberately: organization and workspace,
            then project/application, one or more sources, and approved workspace bindings.
          </p>
        </div>
      </header>

      <div className="adminscreen__main">
        <div className="adminscreen__forms">
          <div className="adminscreen__setupgrid">
            <CreateOrganizationForm onCreated={(organization) => orgSelection?.addOrganization(organization)} />
            <CreateWorkspaceForm
              orgId={ORG}
              onCreated={(workspace) => {
                append("workspaces", workspace);
                sharedScope?.refresh();
              }}
            />
            <AddLineOfBusinessForm orgId={ORG} onCreated={(lob) => append("lobs", lob)} />
            <AddProjectForm
              lobs={lobs}
              onCreated={(project) => {
                append("projects", project);
                sharedScope?.refresh();
              }}
            />
          </div>
          <RegisterDatasourceForm
            projects={projects}
            onCreated={(ds) => {
              append("datasources", ds);
              sharedScope?.refresh();
            }}
          />
          <BindSourceForm
            workspaces={workspaces}
            datasources={datasources}
            bindings={bindings}
            onCreated={(binding) => {
              append("bindings", binding);
              sharedScope?.refresh();
            }}
          />
        </div>

        <aside className="adminscreen__rail">
          <article className="adminpanel adminpanel--subtle">
            <div className="adminpanel__head">
              <p className="adminpanel__eyebrow">ONBOARDING FLOW</p>
              <h2 className="adminpanel__h2">Sequence and progress</h2>
            </div>
            {hierarchy.loading ? (
              <p className="adminpanel__note">Loading…</p>
            ) : (
              <ProgressPanel
                orgLabel={orgLabel}
                lobCount={lobs.length}
                projectCount={projects.length}
                datasourceCount={datasources.length}
                workspaceCount={workspaces.length}
                activeBindingCount={bindings.filter((item) => item.status === "ACTIVE").length}
              />
            )}
          </article>
          <article className="adminpanel">
            <div className="adminpanel__head">
              <p className="adminpanel__eyebrow">CURRENT HIERARCHY</p>
              <h2 className="adminpanel__h2">Scope summary</h2>
            </div>
            {hierarchy.error ? (
              <ErrorState
                title="The hierarchy could not be loaded"
                detail={hierarchy.error}
                onRetry={hierarchy.reload}
              />
            ) : hierarchy.loading ? (
              <p className="adminpanel__note">Loading…</p>
            ) : (
              <ScopeSummary
                lobs={lobs}
                projects={projects}
                datasources={datasources}
                workspaces={workspaces}
                bindings={bindings}
              />
            )}
          </article>
        </aside>
      </div>
    </div>
  );
}
