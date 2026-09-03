import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  DataSourceCreate,
  DataSourceRead,
  LineOfBusinessCreate,
  LineOfBusinessRead,
  OrganizationCreate,
  OrganizationRead,
  ProjectCreate,
  ProjectRead,
  SourceBindingCreate,
  SourceBindingRead,
  WorkspaceCreate,
  WorkspaceRead,
} from "../lib/types";
import {
  ApiError,
  createLineOfBusiness,
  createOrganization,
  createProject,
  createWorkspace,
  fetchOrgDatasources,
  fetchOrgLinesOfBusiness,
  fetchOrgProjects,
  fetchOrgWorkspaces,
  fetchWorkspaceSourceBindings,
  registerDatasource,
  requestSourceBinding,
} from "../lib/api";
import { useOrgId, useOrgSelection } from "../lib/org";
import { useScopeSelection } from "../lib/scope";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import "./AdministrationScreen.css";

/* ---------------------------------------------------------------------------
   Administration -- nav id `administration`, the tenant/onboarding wizard
   ported from the legacy portal's `administration-view` (`ui/index.html`,
   forms bound in `ui/app.js`'s `bindDirectEvents`). Four writes, each the
   real, already-merged route the legacy portal itself posts to -- not an
   invented "setup" API:

     1. Create organization   POST /v1/organizations                (api.py:584)
     2. Add line of business  POST /v1/organizations/{id}/lines-of-business
                                                                       (api.py:677)
     3. Add project            POST /v1/lines-of-business/{lob_id}/projects
                                                                       (api.py:901)
     4. Register data source   POST /v1/projects/{project_id}/datasources
                                                                       (api.py:1021)

   Reads: `fetchOrgProjects` and `fetchOrgDatasources` (already used by
   `SemanticsScreen`/`SourcesScreen`) cover this screen's project and
   datasource lists; `fetchOrgLinesOfBusiness` (new, `api.py:463`) is the one
   read nothing existing exposed. All three are scoped to `useOrgId()`, the
   same shared organization selection every migrated screen reads (see
   `OrgPicker` in the shell nav) -- unlike the legacy portal, this screen has
   no organization `<select>` of its own for the line-of-business/project/
   datasource forms; they act on the organization currently selected in the
   shell, exactly like `SourcesScreen`/`AskScreen`'s datasource pickers act on
   it, not on a second independent choice.

   Scope cuts, stated rather than silently dropped:
     - The legacy screen's "Transformation metadata surfaces" integration-policy
       form (`#integration-policy-form`, `PUT /organizations/{id}/integration-policy`)
       is a fifth, unrelated write (dbt/OpenLineage/Airflow reservation flags,
       not tenant hierarchy) -- left out; the task scoped this port to the
       four onboarding forms plus the read-only summary.
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
       selectable after the next full reload; the confirmation message below
       says so.
--------------------------------------------------------------------------- */

const ORG_SLUG_RE = /^[a-z0-9][a-z0-9-]{1,99}$/;
const LOB_CODE_RE = /^[A-Z0-9][A-Z0-9_-]{1,49}$/;
const ENVIRONMENT_RE = /^[A-Z][A-Z0-9_-]{1,29}$/;

/** `connector_registry`'s three BETA connectors the legacy form offers
 *  (`aida/connectors/registry.py`) -- each paired with its registered
 *  dialect so the request this screen sends can never carry a connector/
 *  dialect mismatch the legacy form's two independent `<select>`s allowed
 *  (it even shipped its own client-side remap for the mismatch its own
 *  option values caused, `ui/app.js:1681`). BigQuery, Snowflake and
 *  Databricks are also registered but omitted here, matching the legacy
 *  form's own three-connector scope. */
const CONNECTOR_OPTIONS: { value: string; label: string; dialect: string }[] = [
  { value: "postgres", label: "PostgreSQL", dialect: "postgres" },
  { value: "oracle", label: "Oracle Database", dialect: "oracle" },
  { value: "sqlserver", label: "Microsoft SQL Server", dialect: "tsql" },
];
const DEFAULT_CONNECTOR_TYPE = CONNECTOR_OPTIONS[0]!.value;

function FormError({ detail }: { detail: string }) {
  return (
    <p className="adminpanel__err" role="alert">
      {detail}
    </p>
  );
}

function FormSuccess({ children }: { children: ReactNode }) {
  return (
    <p className="adminpanel__ok" role="status">
      {children}
    </p>
  );
}

function CreateOrganizationForm({
  onCreated,
}: {
  onCreated: (org: OrganizationRead) => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<OrganizationRead | null>(null);

  const valid = name.trim().length >= 2 && ORG_SLUG_RE.test(slug);

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: OrganizationCreate = { name: name.trim(), slug };
      const org = await createOrganization(body);
      setCreated(org);
      setName("");
      setSlug("");
      onCreated(org);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, name, slug, onCreated]);

  return (
    <form
      className="adminpanel"
      aria-label="Create organization"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="adminpanel__head">
        <p className="adminpanel__eyebrow">PLATFORM ADMIN</p>
        <h2 className="adminpanel__h2">Create organization</h2>
      </div>
      <Field label="Name">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          minLength={2}
          required
          placeholder="Northstar Bank"
        />
      </Field>
      <Field label="Slug">
        <input
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          pattern="[a-z0-9][a-z0-9\-]{1,99}"
          required
          placeholder="northstar-bank"
        />
      </Field>
      {error ? <FormError detail={error} /> : null}
      {created ? (
        <FormSuccess>
          Created "{created.name}" and switched the application to it.
        </FormSuccess>
      ) : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Creating…" : "Create organization"}
      </Button>
    </form>
  );
}

function AddLineOfBusinessForm({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: (lob: LineOfBusinessRead) => void;
}) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<LineOfBusinessRead | null>(null);

  const valid = name.trim().length >= 2 && LOB_CODE_RE.test(code);

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: LineOfBusinessCreate = { name: name.trim(), code };
      const lob = await createLineOfBusiness(orgId, body);
      setCreated(lob);
      setName("");
      setCode("");
      onCreated(lob);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, orgId, name, code, onCreated]);

  return (
    <form
      className="adminpanel"
      aria-label="Add line of business"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="adminpanel__head">
        <p className="adminpanel__eyebrow">OWNERSHIP</p>
        <h2 className="adminpanel__h2">Add line of business</h2>
      </div>
      <Field label="Name">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          minLength={2}
          required
          placeholder="Consumer Banking"
        />
      </Field>
      <Field label="Code">
        <input
          value={code}
          onChange={(e) => setCode(e.target.value.toUpperCase())}
          pattern="[A-Z0-9][A-Z0-9_\-]{1,49}"
          required
          placeholder="CONSUMER"
        />
      </Field>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Created "{created.name}" ({created.code}).</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Adding…" : "Add line of business"}
      </Button>
    </form>
  );
}

function AddProjectForm({
  lobs,
  onCreated,
}: {
  lobs: LineOfBusinessRead[];
  onCreated: (project: ProjectRead) => void;
}) {
  const [lobId, setLobId] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ProjectRead | null>(null);

  const valid = Boolean(lobId) && name.trim().length >= 2 && ORG_SLUG_RE.test(slug);

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: ProjectCreate = { name: name.trim(), slug };
      const project = await createProject(lobId, body);
      setCreated(project);
      setName("");
      setSlug("");
      onCreated(project);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, lobId, name, slug, onCreated]);

  if (lobs.length === 0) {
    return (
      <div className="adminpanel" aria-label="Add project">
        <div className="adminpanel__head">
          <p className="adminpanel__eyebrow">DELIVERY</p>
          <h2 className="adminpanel__h2">Add project</h2>
        </div>
        <Empty title="No lines of business yet" hint="Add one above before creating a project." />
      </div>
    );
  }

  return (
    <form
      className="adminpanel"
      aria-label="Add project"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="adminpanel__head">
        <p className="adminpanel__eyebrow">DELIVERY</p>
        <h2 className="adminpanel__h2">Add project</h2>
      </div>
      <Field label="Line of business">
        <select value={lobId} onChange={(e) => setLobId(e.target.value)} required>
          <option value="">Select…</option>
          {lobs.map((lob) => (
            <option key={lob.id} value={lob.id}>
              {lob.name} ({lob.code})
            </option>
          ))}
        </select>
      </Field>
      <Field label="Name">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          minLength={2}
          required
          placeholder="Customer 360"
        />
      </Field>
      <Field label="Slug">
        <input
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          pattern="[a-z0-9][a-z0-9\-]{1,99}"
          required
          placeholder="customer-360"
        />
      </Field>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Created "{created.name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Adding…" : "Add project"}
      </Button>
    </form>
  );
}

function CreateWorkspaceForm({ orgId, onCreated }: {
  orgId: string;
  onCreated: (workspace: WorkspaceRead) => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [purpose, setPurpose] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<WorkspaceRead | null>(null);
  const valid = name.trim().length >= 2 && ORG_SLUG_RE.test(slug) && purpose.trim().length >= 3;

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: WorkspaceCreate = { name: name.trim(), slug, purpose: purpose.trim() };
      const workspace = await createWorkspace(orgId, body);
      setCreated(workspace);
      setName("");
      setSlug("");
      setPurpose("");
      onCreated(workspace);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, orgId, name, slug, purpose, onCreated]);

  return (
    <form className="adminpanel" aria-label="Create workspace" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <div className="adminpanel__head"><div><p className="adminpanel__eyebrow">ACCESS BOUNDARY</p><h2 className="adminpanel__h2">Create workspace</h2></div></div>
      <Field label="Name"><input value={name} onChange={(event) => setName(event.target.value)} minLength={2} required placeholder="Governed analytics" /></Field>
      <Field label="Slug"><input value={slug} onChange={(event) => setSlug(event.target.value)} pattern="[a-z0-9][a-z0-9\-]{1,99}" required placeholder="governed-analytics" /></Field>
      <Field label="Purpose"><input value={purpose} onChange={(event) => setPurpose(event.target.value)} minLength={3} required placeholder="Approved customer analysis" /></Field>
      <p className="adminpanel__note">A workspace controls who may use which sources. It does not own projects.</p>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Created "{created.name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>{submitting ? "Creatingâ€¦" : "Create workspace"}</Button>
    </form>
  );
}

function BindSourceForm({ workspaces, datasources, bindings, onCreated }: {
  workspaces: WorkspaceRead[];
  datasources: DataSourceRead[];
  bindings: SourceBindingRead[];
  onCreated: (binding: SourceBindingRead) => void;
}) {
  const [workspaceId, setWorkspaceId] = useState("");
  const [datasourceId, setDatasourceId] = useState("");
  const [purpose, setPurpose] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<SourceBindingRead | null>(null);
  const existing = new Set(bindings.filter((item) => item.workspace_id === workspaceId).map((item) => item.datasource_id));
  const candidates = datasources.filter((item) => !existing.has(item.id));
  const valid = Boolean(workspaceId && datasourceId) && purpose.trim().length >= 3;

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: SourceBindingCreate = { datasource_id: datasourceId, purpose: purpose.trim() };
      const binding = await requestSourceBinding(workspaceId, body);
      setCreated(binding);
      setDatasourceId("");
      setPurpose("");
      onCreated(binding);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, workspaceId, datasourceId, purpose, onCreated]);

  if (!workspaces.length || !datasources.length) {
    return <div className="adminpanel adminpanel--wide" aria-label="Bind source to workspace"><div className="adminpanel__head"><div><p className="adminpanel__eyebrow">ACCESS GRANT</p><h2 className="adminpanel__h2">Bind source to workspace</h2></div></div><Empty title="Workspace and source required" hint="Create both before requesting a governed binding." /></div>;
  }

  return (
    <form className="adminpanel adminpanel--wide" aria-label="Bind source to workspace" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <div className="adminpanel__head"><div><p className="adminpanel__eyebrow">ACCESS GRANT</p><h2 className="adminpanel__h2">Bind source to workspace</h2></div><Pill tone="warn">maker-checker</Pill></div>
      <div className="adminpanel__grid">
        <Field label="Workspace"><select value={workspaceId} onChange={(event) => { setWorkspaceId(event.target.value); setDatasourceId(""); }} required><option value="">Selectâ€¦</option>{workspaces.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label="Project source"><select value={datasourceId} onChange={(event) => setDatasourceId(event.target.value)} required disabled={!workspaceId}><option value="">Selectâ€¦</option>{candidates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label="Access purpose"><input value={purpose} onChange={(event) => setPurpose(event.target.value)} minLength={3} required placeholder="Reconciliations and governed analysis" /></Field>
      </div>
      <p className="adminpanel__note">The request is pending until a different reviewer approves it. Active bindings become selectable in the application scope.</p>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Binding requested. Current status: {created.status.toLowerCase()}.</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>{submitting ? "Requestingâ€¦" : "Request binding"}</Button>
    </form>
  );
}

function RegisterDatasourceForm({
  projects,
  onCreated,
}: {
  projects: ProjectRead[];
  onCreated: (ds: DataSourceRead) => void;
}) {
  const [projectId, setProjectId] = useState("");
  const [name, setName] = useState("");
  const [connectorType, setConnectorType] = useState(DEFAULT_CONNECTOR_TYPE);
  const [environment, setEnvironment] = useState("DEV");
  const [networkZone, setNetworkZone] = useState("default");
  const [credentialReference, setCredentialReference] = useState("");
  const [maxConcurrency, setMaxConcurrency] = useState(4);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<DataSourceRead | null>(null);

  const dialect = CONNECTOR_OPTIONS.find((c) => c.value === connectorType)?.dialect ?? "";
  const valid =
    Boolean(projectId) &&
    name.trim().length >= 2 &&
    ENVIRONMENT_RE.test(environment) &&
    networkZone.trim().length > 0 &&
    credentialReference.trim().length >= 6 &&
    maxConcurrency >= 1 &&
    maxConcurrency <= 100;

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: DataSourceCreate = {
        name: name.trim(),
        connector_type: connectorType,
        dialect,
        environment,
        network_zone: networkZone,
        credential_reference: credentialReference.trim(),
        max_concurrency: maxConcurrency,
      };
      const ds = await registerDatasource(projectId, body);
      setCreated(ds);
      setName("");
      setCredentialReference("");
      onCreated(ds);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [
    valid,
    submitting,
    projectId,
    name,
    connectorType,
    dialect,
    environment,
    networkZone,
    credentialReference,
    maxConcurrency,
    onCreated,
  ]);

  if (projects.length === 0) {
    return (
      <div className="adminpanel adminpanel--wide" aria-label="Register data source">
        <div className="adminpanel__head">
          <p className="adminpanel__eyebrow">CONNECTION CONTRACT</p>
          <h2 className="adminpanel__h2">Register data source</h2>
        </div>
        <Empty title="No projects yet" hint="Add a project above before registering a source." />
      </div>
    );
  }

  return (
    <form
      className="adminpanel adminpanel--wide"
      aria-label="Register data source"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="adminpanel__head">
        <div>
          <p className="adminpanel__eyebrow">CONNECTION CONTRACT</p>
          <h2 className="adminpanel__h2">Register data source</h2>
        </div>
        <Pill tone="mute">{connectorType.toUpperCase()}</Pill>
      </div>
      <div className="adminpanel__grid">
        <Field label="Project">
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} required>
            <option value="">Select…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Source name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            minLength={2}
            required
            placeholder="Consumer warehouse"
          />
        </Field>
        <Field label="Connector">
          <select value={connectorType} onChange={(e) => setConnectorType(e.target.value)}>
            {CONNECTOR_OPTIONS.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Dialect">
          <input value={dialect} disabled readOnly />
        </Field>
        <Field label="Environment">
          <input
            value={environment}
            onChange={(e) => setEnvironment(e.target.value.toUpperCase())}
            pattern="[A-Z][A-Z0-9_\-]{1,29}"
            required
          />
        </Field>
        <Field label="Network zone">
          <input
            value={networkZone}
            onChange={(e) => setNetworkZone(e.target.value)}
            required
          />
        </Field>
        <Field label="Credential reference">
          <input
            value={credentialReference}
            onChange={(e) => setCredentialReference(e.target.value)}
            minLength={6}
            required
            placeholder="env://AIDA_SAMPLE_SOURCE_DSN"
          />
        </Field>
        <Field label="Maximum concurrency">
          <input
            type="number"
            min={1}
            max={100}
            value={maxConcurrency}
            onChange={(e) => setMaxConcurrency(Number(e.target.value))}
          />
        </Field>
      </div>
      <p className="adminpanel__note">
        Connection strings and secrets are rejected. Register only a reference for the configured
        secret provider.
      </p>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Registered "{created.name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Registering…" : "Register source"}
      </Button>
    </form>
  );
}

function ProgressPanel({
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
      detail: projectCount > 0 ? `${projectCount} application${projectCount === 1 ? "" : "s"}` : `${lobCount} business classifications`,
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

function ScopeSummary({
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
        <table><thead><tr><th>Workspace</th><th>Bound sources</th></tr></thead><tbody>
          {workspaces.map((workspace) => {
            const related = bindings.filter((item) => item.workspace_id === workspace.id);
            return <tr key={workspace.id}><td><b>{workspace.name}</b><small>{workspace.purpose || workspace.slug}</small></td><td>{related.filter((item) => item.status === "ACTIVE").length} active<small>{related.filter((item) => item.status !== "ACTIVE").length} awaiting/restricted</small></td></tr>;
          })}
        </tbody></table>
      </section>
      <section>
        <h3>Technical axis</h3>
        <table><thead><tr><th>Project / application</th><th>Owned sources</th></tr></thead><tbody>
          {projects.map((project) => {
            const owned = datasources.filter((item) => item.project_id === project.id);
            return <tr key={project.id}><td><b>{project.name}</b><small>{project.slug}</small></td><td>{owned.length}<small>{owned.map((item) => item.name).join(", ") || "Register a source"}</small></td></tr>;
          })}
        </tbody></table>
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

export function AdministrationScreen() {
  const ORG = useOrgId();
  const orgSelection = useOrgSelection();
  const sharedScope = useScopeSelection();
  const orgLabel = orgSelection?.organizations.find((o) => o.id === ORG)?.name ?? ORG;

  const [lobs, setLobs] = useState<LineOfBusinessRead[]>([]);
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceRead[]>([]);
  const [bindings, setBindings] = useState<SourceBindingRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const loadSummary = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const [lobPage, projectPage, dsPage, workspacePage] = await Promise.all([
        fetchOrgLinesOfBusiness(ORG, ac.signal),
        fetchOrgProjects(ORG, ac.signal),
        fetchOrgDatasources(ORG, ac.signal),
        fetchOrgWorkspaces(ORG, ac.signal),
      ]);
      const bindingPages = await Promise.all(
        workspacePage.items.map((workspace) => fetchWorkspaceSourceBindings(workspace.id, ac.signal)),
      );
      if (seq !== reqSeq.current) return;
      setLobs(lobPage.items);
      setProjects(projectPage.items);
      setDatasources(dsPage.items);
      setWorkspaces(workspacePage.items);
      setBindings(bindingPages.flatMap((page) => page.items));
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [ORG]);

  useEffect(() => {
    void loadSummary();
    return () => inflight.current?.abort();
  }, [loadSummary]);

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
            <CreateWorkspaceForm orgId={ORG} onCreated={(workspace) => { setWorkspaces((prev) => [...prev, workspace]); sharedScope?.refresh(); }} />
            <AddLineOfBusinessForm orgId={ORG} onCreated={(lob) => setLobs((prev) => [...prev, lob])} />
            <AddProjectForm
              lobs={lobs}
              onCreated={(project) => { setProjects((prev) => [...prev, project]); sharedScope?.refresh(); }}
            />
          </div>
          <RegisterDatasourceForm
            projects={projects}
            onCreated={(ds) => { setDatasources((prev) => [...prev, ds]); sharedScope?.refresh(); }}
          />
          <BindSourceForm
            workspaces={workspaces}
            datasources={datasources}
            bindings={bindings}
            onCreated={(binding) => { setBindings((prev) => [...prev, binding]); sharedScope?.refresh(); }}
          />
        </div>

        <aside className="adminscreen__rail">
          <article className="adminpanel adminpanel--subtle">
            <div className="adminpanel__head">
              <p className="adminpanel__eyebrow">ONBOARDING FLOW</p>
              <h2 className="adminpanel__h2">Sequence and progress</h2>
            </div>
            {loading ? (
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
            {error ? (
              <ErrorState
                title="The hierarchy could not be loaded"
                detail={error}
                onRetry={() => void loadSummary()}
              />
            ) : loading ? (
              <p className="adminpanel__note">Loading…</p>
            ) : (
              <ScopeSummary lobs={lobs} projects={projects} datasources={datasources} workspaces={workspaces} bindings={bindings} />
            )}
          </article>
        </aside>
      </div>
    </div>
  );
}
