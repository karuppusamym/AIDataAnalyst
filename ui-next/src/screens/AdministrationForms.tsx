import { useCallback, useState } from "react";
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
  createLineOfBusiness,
  createOrganization,
  createProject,
  createWorkspace,
  registerDatasource,
  requestSourceBinding,
} from "../lib/api";
import { Button, Empty, Field, Pill } from "../components/primitives";
import { FormError, FormSuccess, useSubmitAction } from "../components/screenState";

/* ---------------------------------------------------------------------------
   The six onboarding writes -- every POST `AdministrationScreen` makes, and
   nothing else. Each is the real, already-merged route the legacy portal
   itself posts to (endpoint list in `AdministrationScreen.tsx`).

   These belong together because they are one sequence, not six features: an
   organization contains a workspace and a line of business, a line of
   business contains a project, a project owns a data source, and a binding is
   the approval that joins the access axis to the technical one. Every form
   validates locally before it posts, reports the server's own refusal, and
   hands the created object up so the summary rail can count it. That shared
   shape is `useSubmitAction`; what each form knows on its own is which fields
   the route accepts and which of them may be left out.
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

function PanelHead({ eyebrow, title, aside }: { eyebrow: string; title: string; aside?: React.ReactNode }) {
  return (
    <div className="adminpanel__head">
      <div>
        <p className="adminpanel__eyebrow">{eyebrow}</p>
        <h2 className="adminpanel__h2">{title}</h2>
      </div>
      {aside}
    </div>
  );
}

export function CreateOrganizationForm({ onCreated }: { onCreated: (org: OrganizationRead) => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const action = useSubmitAction<OrganizationRead>();

  const valid = name.trim().length >= 2 && ORG_SLUG_RE.test(slug);

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: OrganizationCreate = { name: name.trim(), slug };
    const org = await action.run(() => createOrganization(body));
    if (!org) return;
    setName("");
    setSlug("");
    onCreated(org);
  }, [valid, name, slug, action, onCreated]);

  return (
    <form
      className="adminpanel"
      aria-label="Create organization"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <PanelHead eyebrow="PLATFORM ADMIN" title="Create organization" />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} minLength={2} required placeholder="Northstar Bank" />
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
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? (
        <FormSuccess>Created "{action.result.name}" and switched the application to it.</FormSuccess>
      ) : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Creating…" : "Create organization"}
      </Button>
    </form>
  );
}

export function AddLineOfBusinessForm({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: (lob: LineOfBusinessRead) => void;
}) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const action = useSubmitAction<LineOfBusinessRead>();

  const valid = name.trim().length >= 2 && LOB_CODE_RE.test(code);

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: LineOfBusinessCreate = { name: name.trim(), code };
    const lob = await action.run(() => createLineOfBusiness(orgId, body));
    if (!lob) return;
    setName("");
    setCode("");
    onCreated(lob);
  }, [valid, orgId, name, code, action, onCreated]);

  return (
    <form
      className="adminpanel"
      aria-label="Add line of business"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <PanelHead eyebrow="OWNERSHIP" title="Add line of business" />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} minLength={2} required placeholder="Consumer Banking" />
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
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? (
        <FormSuccess>
          Created "{action.result.name}" ({action.result.code}).
        </FormSuccess>
      ) : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Adding…" : "Add line of business"}
      </Button>
    </form>
  );
}

export function AddProjectForm({
  lobs,
  onCreated,
}: {
  lobs: LineOfBusinessRead[];
  onCreated: (project: ProjectRead) => void;
}) {
  const [lobId, setLobId] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const action = useSubmitAction<ProjectRead>();

  const valid = Boolean(lobId) && name.trim().length >= 2 && ORG_SLUG_RE.test(slug);

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: ProjectCreate = { name: name.trim(), slug };
    const project = await action.run(() => createProject(lobId, body));
    if (!project) return;
    setName("");
    setSlug("");
    onCreated(project);
  }, [valid, lobId, name, slug, action, onCreated]);

  // A project cannot exist without a line of business to classify it, so the
  // form is not rendered disabled -- it is replaced by the step that unblocks it.
  if (lobs.length === 0) {
    return (
      <div className="adminpanel" aria-label="Add project">
        <PanelHead eyebrow="DELIVERY" title="Add project" />
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
      <PanelHead eyebrow="DELIVERY" title="Add project" />
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
        <input value={name} onChange={(e) => setName(e.target.value)} minLength={2} required placeholder="Customer 360" />
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
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? <FormSuccess>Created "{action.result.name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Adding…" : "Add project"}
      </Button>
    </form>
  );
}

export function CreateWorkspaceForm({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: (workspace: WorkspaceRead) => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [purpose, setPurpose] = useState("");
  const action = useSubmitAction<WorkspaceRead>();

  const valid = name.trim().length >= 2 && ORG_SLUG_RE.test(slug) && purpose.trim().length >= 3;

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: WorkspaceCreate = { name: name.trim(), slug, purpose: purpose.trim() };
    const workspace = await action.run(() => createWorkspace(orgId, body));
    if (!workspace) return;
    setName("");
    setSlug("");
    setPurpose("");
    onCreated(workspace);
  }, [valid, orgId, name, slug, purpose, action, onCreated]);

  return (
    <form
      className="adminpanel"
      aria-label="Create workspace"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <PanelHead eyebrow="ACCESS BOUNDARY" title="Create workspace" />
      <Field label="Name">
        <input value={name} onChange={(event) => setName(event.target.value)} minLength={2} required placeholder="Governed analytics" />
      </Field>
      <Field label="Slug">
        <input
          value={slug}
          onChange={(event) => setSlug(event.target.value)}
          pattern="[a-z0-9][a-z0-9\-]{1,99}"
          required
          placeholder="governed-analytics"
        />
      </Field>
      <Field label="Purpose">
        <input
          value={purpose}
          onChange={(event) => setPurpose(event.target.value)}
          minLength={3}
          required
          placeholder="Approved customer analysis"
        />
      </Field>
      <p className="adminpanel__note">A workspace controls who may use which sources. It does not own projects.</p>
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? <FormSuccess>Created "{action.result.name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Creating…" : "Create workspace"}
      </Button>
    </form>
  );
}

export function BindSourceForm({
  workspaces,
  datasources,
  bindings,
  onCreated,
}: {
  workspaces: WorkspaceRead[];
  datasources: DataSourceRead[];
  bindings: SourceBindingRead[];
  onCreated: (binding: SourceBindingRead) => void;
}) {
  const [workspaceId, setWorkspaceId] = useState("");
  const [datasourceId, setDatasourceId] = useState("");
  const [purpose, setPurpose] = useState("");
  const action = useSubmitAction<SourceBindingRead>();

  // A source already bound to this workspace is not offered again: the route
  // rejects the duplicate, and offering it invites a refusal the user could
  // not have predicted from the form.
  const existing = new Set(
    bindings.filter((item) => item.workspace_id === workspaceId).map((item) => item.datasource_id),
  );
  const candidates = datasources.filter((item) => !existing.has(item.id));
  const valid = Boolean(workspaceId && datasourceId) && purpose.trim().length >= 3;

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: SourceBindingCreate = { datasource_id: datasourceId, purpose: purpose.trim() };
    const binding = await action.run(() => requestSourceBinding(workspaceId, body));
    if (!binding) return;
    setDatasourceId("");
    setPurpose("");
    onCreated(binding);
  }, [valid, workspaceId, datasourceId, purpose, action, onCreated]);

  if (!workspaces.length || !datasources.length) {
    return (
      <div className="adminpanel adminpanel--wide" aria-label="Bind source to workspace">
        <PanelHead eyebrow="ACCESS GRANT" title="Bind source to workspace" />
        <Empty title="Workspace and source required" hint="Create both before requesting a governed binding." />
      </div>
    );
  }

  return (
    <form
      className="adminpanel adminpanel--wide"
      aria-label="Bind source to workspace"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <PanelHead eyebrow="ACCESS GRANT" title="Bind source to workspace" aside={<Pill tone="warn">maker-checker</Pill>} />
      <div className="adminpanel__grid">
        <Field label="Workspace">
          <select
            value={workspaceId}
            onChange={(event) => {
              setWorkspaceId(event.target.value);
              setDatasourceId("");
            }}
            required
          >
            <option value="">Select…</option>
            {workspaces.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Project source">
          <select
            value={datasourceId}
            onChange={(event) => setDatasourceId(event.target.value)}
            required
            disabled={!workspaceId}
          >
            <option value="">Select…</option>
            {candidates.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Access purpose">
          <input
            value={purpose}
            onChange={(event) => setPurpose(event.target.value)}
            minLength={3}
            required
            placeholder="Reconciliations and governed analysis"
          />
        </Field>
      </div>
      <p className="adminpanel__note">
        The request is pending until a different reviewer approves it. Active bindings become selectable in the
        application scope.
      </p>
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? (
        <FormSuccess>Binding requested. Current status: {action.result.status.toLowerCase()}.</FormSuccess>
      ) : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Requesting…" : "Request binding"}
      </Button>
    </form>
  );
}

export function RegisterDatasourceForm({
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
  const action = useSubmitAction<DataSourceRead>();

  // The dialect is derived, never chosen: see CONNECTOR_OPTIONS above.
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
    if (!valid) return;
    const body: DataSourceCreate = {
      name: name.trim(),
      connector_type: connectorType,
      dialect,
      environment,
      network_zone: networkZone,
      credential_reference: credentialReference.trim(),
      max_concurrency: maxConcurrency,
    };
    const ds = await action.run(() => registerDatasource(projectId, body));
    if (!ds) return;
    setName("");
    setCredentialReference("");
    onCreated(ds);
  }, [
    valid,
    projectId,
    name,
    connectorType,
    dialect,
    environment,
    networkZone,
    credentialReference,
    maxConcurrency,
    action,
    onCreated,
  ]);

  if (projects.length === 0) {
    return (
      <div className="adminpanel adminpanel--wide" aria-label="Register data source">
        <PanelHead eyebrow="CONNECTION CONTRACT" title="Register data source" />
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
      <PanelHead
        eyebrow="CONNECTION CONTRACT"
        title="Register data source"
        aside={<Pill tone="mute">{connectorType.toUpperCase()}</Pill>}
      />
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
          <input value={name} onChange={(e) => setName(e.target.value)} minLength={2} required placeholder="Consumer warehouse" />
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
          <input value={networkZone} onChange={(e) => setNetworkZone(e.target.value)} required />
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
        Connection strings and secrets are rejected. Register only a reference for the configured secret provider.
      </p>
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? <FormSuccess>Registered "{action.result.name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Registering…" : "Register source"}
      </Button>
    </form>
  );
}
