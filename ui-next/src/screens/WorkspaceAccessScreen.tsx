import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  BiArtifactImportRead,
  BiConnectionCreate,
  BiConnectionRead,
  DataSourceRead,
  ProjectRead,
  SourceBindingDecision,
  SourceBindingRead,
  WorkspaceMembershipCreate,
  WorkspaceMembershipRead,
  WorkspaceRead,
} from "../lib/types";
import {
  ApiError,
  addWorkspaceMember,
  createBiConnection,
  decideSourceBinding,
  fetchOrgDatasources,
  fetchOrgProjects,
  fetchOrgWorkspaces,
  fetchProjectBiConnections,
  fetchWorkspaceMembers,
  fetchWorkspaceSourceBindings,
  importBiArtifact,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useScopeSelection } from "../lib/scope";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
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

     1. Add member         POST /v1/workspaces/{id}/members          (workspace_api.py:160, _ADMIN)
     2. List members       GET  /v1/workspaces/{id}/members          (workspace_api.py:207, _ANY_MEMBER)
     3. Decide binding      POST /v1/source-bindings/{id}/decision    (workspace_api.py:293, _ADMIN + Reviewer)
     4. Register BI conn    POST /v1/projects/{id}/bi-connections     (bi_api.py:171)
     5. List BI conns       GET  /v1/projects/{id}/bi-connections     (bi_api.py:226)
     6. Import BI artifact  POST /v1/bi-connections/{id}/artifact-imports (bi_api.py:258)

   Both pickers (workspace, project) are local to this screen, not the shared
   `ScopeSelection` (`lib/scope.tsx`) -- that context exists to scope *reads*
   the rest of the app makes against one workspace/project/datasource at a
   time, while this screen's job is to administer *every* pending binding and
   BI connection under an org, one workspace/project at a time by choice, not
   by the shell's ambient selection. It does call `useScopeSelection()?.refresh()`
   after a binding decision, though: approving a binding changes which
   datasources `ScopeProvider` treats as accessible (its own `status ===
   "ACTIVE"` filter), and every other screen reads that same provider.

   Scope cuts, stated rather than silently dropped:
     - No membership edit/revoke: the legacy screen has no such control either
       (`renderAccess` renders `members` as a read-only table) -- `add_member`
       is the only membership write the backend exposes at all.
     - No workspace/project *creation* here: that is `AdministrationScreen`'s
       job (`CreateWorkspaceForm`); this screen's pickers list only what
       already exists, matching the legacy `#control-workspace`/`#bi-project`
       selects, which are populated, never created from, by this tab.
     - No Power BI/Looker artifact-shape-specific parsing: `BiArtifactImportRequest.artifact`
       is posted as whatever JSON the textarea parses to, exactly like the
       legacy `#bi-import-form`'s `parseJson(data.get("artifact"), "Artifact")`
       -- this screen does not attempt to validate report/metric shape
       client-side beyond "is it JSON".
--------------------------------------------------------------------------- */

const CONNECTION_KEY_RE = /^[a-z][a-z0-9_-]{1,99}$/;
const MEMBER_ROLES: WorkspaceMembershipCreate["role"][] = [
  "viewer",
  "analyst",
  "steward",
  "reviewer",
  "workspace_owner",
];
const PRINCIPAL_KINDS: NonNullable<WorkspaceMembershipCreate["principal_kind"]>[] = [
  "HUMAN",
  "AGENT",
  "SERVICE",
];
const BI_TOOLS: BiConnectionCreate["bi_tool"][] = ["TABLEAU", "POWER_BI", "LOOKER"];

function FormError({ detail }: { detail: string }) {
  return (
    <p className="wsaccess-panel__err" role="alert">
      {detail}
    </p>
  );
}

function FormSuccess({ children }: { children: ReactNode }) {
  return (
    <p className="wsaccess-panel__ok" role="status">
      {children}
    </p>
  );
}

function datasourceLabel(datasources: DataSourceRead[], datasourceId: string): string {
  return datasources.find((item) => item.id === datasourceId)?.name ?? datasourceId;
}

function AddMemberForm({
  workspaceId,
  onAdded,
}: {
  workspaceId: string;
  onAdded: (member: WorkspaceMembershipRead) => void;
}) {
  const [principalId, setPrincipalId] = useState("");
  const [principalKind, setPrincipalKind] =
    useState<NonNullable<WorkspaceMembershipCreate["principal_kind"]>>("HUMAN");
  const [role, setRole] = useState<WorkspaceMembershipCreate["role"]>("analyst");
  const [expiresAt, setExpiresAt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<WorkspaceMembershipRead | null>(null);

  const valid = principalId.trim().length >= 2;

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: WorkspaceMembershipCreate = {
        principal_id: principalId.trim(),
        principal_kind: principalKind,
        role,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      };
      const member = await addWorkspaceMember(workspaceId, body);
      setCreated(member);
      setPrincipalId("");
      setExpiresAt("");
      onAdded(member);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, workspaceId, principalId, principalKind, role, expiresAt, onAdded]);

  return (
    <form
      className="wsaccess-panel"
      aria-label="Add workspace member"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="wsaccess-panel__head">
        <p className="wsaccess-panel__eyebrow">MEMBERSHIP</p>
        <h2 className="wsaccess-panel__h2">Add member</h2>
      </div>
      <div className="wsaccess-panel__grid">
        <Field label="Principal id">
          <input
            value={principalId}
            onChange={(event) => setPrincipalId(event.target.value)}
            minLength={2}
            required
            placeholder="jordan.reyes"
          />
        </Field>
        <Field label="Principal kind">
          <select
            value={principalKind}
            onChange={(event) =>
              setPrincipalKind(event.target.value as NonNullable<WorkspaceMembershipCreate["principal_kind"]>)
            }
          >
            {PRINCIPAL_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Role">
          <select
            value={role}
            onChange={(event) => setRole(event.target.value as WorkspaceMembershipCreate["role"])}
          >
            {MEMBER_ROLES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Expires (optional)">
          <input
            type="date"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
          />
        </Field>
      </div>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Added "{created.principal_id}" as {created.role}.</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Adding..." : "Add member"}
      </Button>
    </form>
  );
}

function MembersPanel({ members }: { members: WorkspaceMembershipRead[] }) {
  if (members.length === 0) {
    return <Empty title="No members yet" hint="Add the first member with the form alongside this list." />;
  }
  return (
    <table className="wsaccess-table">
      <thead>
        <tr>
          <th>Principal</th>
          <th>Kind</th>
          <th>Role</th>
          <th>Status</th>
          <th>Expires</th>
        </tr>
      </thead>
      <tbody>
        {members.map((member) => (
          <tr key={member.id}>
            <td>{member.principal_id}</td>
            <td>{member.principal_kind}</td>
            <td>{member.role}</td>
            <td>
              <Pill tone={member.status === "ACTIVE" ? "ok" : "mute"}>{member.status}</Pill>
            </td>
            <td>{member.expires_at ? new Date(member.expires_at).toLocaleDateString() : "Never"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PendingBindingRow({
  binding,
  datasourceName,
  onDecided,
}: {
  binding: SourceBindingRead;
  datasourceName: string;
  onDecided: (binding: SourceBindingRead) => void;
}) {
  const [rationale, setRationale] = useState("");
  const [submitting, setSubmitting] = useState<"APPROVE" | "REJECT" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decide = useCallback(
    async (decision: SourceBindingDecision["decision"]) => {
      if (submitting) return;
      setSubmitting(decision);
      setError(null);
      try {
        const body: SourceBindingDecision = {
          decision,
          valid_for_days: 365,
          rationale: rationale.trim(),
        };
        const decided = await decideSourceBinding(binding.id, body);
        onDecided(decided);
      } catch (reason) {
        setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
      } finally {
        setSubmitting(null);
      }
    },
    [submitting, binding.id, rationale, onDecided],
  );

  return (
    <li className="wsaccess-bindingrow">
      <div className="wsaccess-bindingrow__meta">
        <strong>{datasourceName}</strong>
        <small>{binding.purpose}</small>
        <small>Requested by {binding.requested_by}</small>
      </div>
      <input
        className="wsaccess-bindingrow__rationale"
        value={rationale}
        onChange={(event) => setRationale(event.target.value)}
        placeholder="Decision rationale (optional)"
        aria-label={`Rationale for ${datasourceName} binding decision`}
      />
      <div className="wsaccess-bindingrow__actions">
        <Button
          variant="primary"
          disabled={submitting !== null}
          onClick={() => void decide("APPROVE")}
        >
          {submitting === "APPROVE" ? "Approving..." : "Approve"}
        </Button>
        <Button disabled={submitting !== null} onClick={() => void decide("REJECT")}>
          {submitting === "REJECT" ? "Rejecting..." : "Reject"}
        </Button>
      </div>
      {error ? <FormError detail={error} /> : null}
    </li>
  );
}

function PendingBindingsPanel({
  bindings,
  datasources,
  onDecided,
}: {
  bindings: SourceBindingRead[];
  datasources: DataSourceRead[];
  onDecided: (binding: SourceBindingRead) => void;
}) {
  if (bindings.length === 0) {
    return <Empty title="No pending source-binding requests" hint="Every request for this workspace has already been decided." />;
  }
  return (
    <ul className="wsaccess-bindinglist">
      {bindings.map((binding) => (
        <PendingBindingRow
          key={binding.id}
          binding={binding}
          datasourceName={datasourceLabel(datasources, binding.datasource_id)}
          onDecided={onDecided}
        />
      ))}
    </ul>
  );
}

function CreateBiConnectionForm({
  projectId,
  datasources,
  onCreated,
}: {
  projectId: string;
  datasources: DataSourceRead[];
  onCreated: (connection: BiConnectionRead) => void;
}) {
  const [datasourceId, setDatasourceId] = useState("");
  const [biTool, setBiTool] = useState<BiConnectionCreate["bi_tool"]>("TABLEAU");
  const [connectionKey, setConnectionKey] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [siteOrWorkspace, setSiteOrWorkspace] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<BiConnectionRead | null>(null);

  const projectDatasources = datasources.filter((item) => item.project_id === projectId);
  const valid =
    Boolean(datasourceId) && CONNECTION_KEY_RE.test(connectionKey) && displayName.trim().length >= 2;

  const submit = useCallback(async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      const body: BiConnectionCreate = {
        datasource_id: datasourceId,
        bi_tool: biTool,
        connection_key: connectionKey,
        display_name: displayName.trim(),
        site_or_workspace: siteOrWorkspace.trim() || null,
      };
      const connection = await createBiConnection(projectId, body);
      setCreated(connection);
      setConnectionKey("");
      setDisplayName("");
      setSiteOrWorkspace("");
      onCreated(connection);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [valid, submitting, projectId, datasourceId, biTool, connectionKey, displayName, siteOrWorkspace, onCreated]);

  if (projectDatasources.length === 0) {
    return (
      <div className="wsaccess-panel" aria-label="Register BI connection">
        <div className="wsaccess-panel__head">
          <p className="wsaccess-panel__eyebrow">BI / TABLEAU LINEAGE</p>
          <h2 className="wsaccess-panel__h2">Register BI connection</h2>
        </div>
        <Empty title="No sources under this project" hint="Register a datasource for this project before connecting a BI tool." />
      </div>
    );
  }

  return (
    <form
      className="wsaccess-panel"
      aria-label="Register BI connection"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="wsaccess-panel__head">
        <p className="wsaccess-panel__eyebrow">BI / TABLEAU LINEAGE</p>
        <h2 className="wsaccess-panel__h2">Register BI connection</h2>
      </div>
      <div className="wsaccess-panel__grid">
        <Field label="Project source">
          <select value={datasourceId} onChange={(event) => setDatasourceId(event.target.value)} required>
            <option value="">Select...</option>
            {projectDatasources.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="BI tool">
          <select
            value={biTool}
            onChange={(event) => setBiTool(event.target.value as BiConnectionCreate["bi_tool"])}
          >
            {BI_TOOLS.map((tool) => (
              <option key={tool} value={tool}>
                {tool}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Connection key">
          <input
            value={connectionKey}
            onChange={(event) => setConnectionKey(event.target.value)}
            pattern="[a-z][a-z0-9_\-]{1,99}"
            required
            placeholder="finance-tableau-prod"
          />
        </Field>
        <Field label="Display name">
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            minLength={2}
            required
            placeholder="Finance Tableau (Production)"
          />
        </Field>
        <Field label="Site / workspace (optional)">
          <input
            value={siteOrWorkspace}
            onChange={(event) => setSiteOrWorkspace(event.target.value)}
            placeholder="finance"
          />
        </Field>
      </div>
      {error ? <FormError detail={error} /> : null}
      {created ? <FormSuccess>Registered "{created.display_name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Registering..." : "Register connection"}
      </Button>
    </form>
  );
}

function ImportArtifactForm({
  connectionId,
  onImported,
}: {
  connectionId: string;
  onImported: (connectionId: string, importRead: BiArtifactImportRead) => void;
}) {
  const [open, setOpen] = useState(false);
  const [biTool, setBiTool] = useState<BiConnectionCreate["bi_tool"]>("TABLEAU");
  const [artifactText, setArtifactText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BiArtifactImportRead | null>(null);

  const submit = useCallback(async () => {
    if (submitting) return;
    setError(null);
    setResult(null);
    let artifact: Record<string, unknown>;
    try {
      artifact = JSON.parse(artifactText) as Record<string, unknown>;
    } catch {
      setError("Artifact is not valid JSON.");
      return;
    }
    setSubmitting(true);
    try {
      const importRead = await importBiArtifact(connectionId, { bi_tool: biTool, artifact });
      setResult(importRead);
      setArtifactText("");
      onImported(connectionId, importRead);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [submitting, artifactText, biTool, connectionId, onImported]);

  if (!open) {
    return (
      <Button onClick={() => setOpen(true)}>Import artifact</Button>
    );
  }

  return (
    <form
      className="wsaccess-import"
      aria-label={`Import BI artifact for connection ${connectionId}`}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field label="BI tool">
        <select
          value={biTool}
          onChange={(event) => setBiTool(event.target.value as BiConnectionCreate["bi_tool"])}
        >
          {BI_TOOLS.map((tool) => (
            <option key={tool} value={tool}>
              {tool}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Artifact JSON">
        <textarea
          className="wsaccess-import__textarea"
          value={artifactText}
          onChange={(event) => setArtifactText(event.target.value)}
          placeholder='{"reports": [], "metrics": []}'
          rows={5}
          required
        />
      </Field>
      {error ? <FormError detail={error} /> : null}
      {result ? (
        <FormSuccess>
          Imported: {result.report_count} reports, {result.metric_count} metrics,{" "}
          {result.matched_column_count} matched / {result.unmatched_column_count} unmatched columns.
        </FormSuccess>
      ) : null}
      <div className="wsaccess-import__actions">
        <Button type="submit" variant="primary" disabled={submitting || artifactText.trim().length === 0}>
          {submitting ? "Importing..." : "Import"}
        </Button>
        <Button onClick={() => setOpen(false)}>Close</Button>
      </div>
    </form>
  );
}

function BiConnectionsPanel({
  connections,
  onImported,
}: {
  connections: BiConnectionRead[];
  onImported: (connectionId: string, importRead: BiArtifactImportRead) => void;
}) {
  if (connections.length === 0) {
    return <Empty title="No BI connections for this project" hint="Register one with the form alongside this list." />;
  }
  return (
    <ul className="wsaccess-bilist">
      {connections.map((connection) => (
        <li key={connection.id} className="wsaccess-birow">
          <div className="wsaccess-birow__meta">
            <strong>{connection.display_name}</strong>
            <Pill tone="info">{connection.bi_tool}</Pill>
            <Pill tone={connection.status === "ACTIVE" ? "ok" : "mute"}>{connection.status}</Pill>
            <small>{connection.connection_key}</small>
          </div>
          <ImportArtifactForm connectionId={connection.id} onImported={onImported} />
        </li>
      ))}
    </ul>
  );
}

export function WorkspaceAccessScreen() {
  const orgId = useOrgId();
  const sharedScope = useScopeSelection();

  const [workspaces, setWorkspaces] = useState<WorkspaceRead[]>([]);
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [projectId, setProjectId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [members, setMembers] = useState<WorkspaceMembershipRead[]>([]);
  const [bindings, setBindings] = useState<SourceBindingRead[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [biConnections, setBiConnections] = useState<BiConnectionRead[]>([]);
  const [biLoading, setBiLoading] = useState(false);
  const [biError, setBiError] = useState<string | null>(null);

  const detailSeq = useRef(0);
  const biSeq = useRef(0);
  const summaryInflight = useRef<AbortController | null>(null);
  const detailInflight = useRef<AbortController | null>(null);
  const biInflight = useRef<AbortController | null>(null);

  const loadSummary = useCallback(async () => {
    summaryInflight.current?.abort();
    const ac = new AbortController();
    summaryInflight.current = ac;
    setLoading(true);
    setError(null);
    try {
      const [workspacePage, projectPage, dsPage] = await Promise.all([
        fetchOrgWorkspaces(orgId, ac.signal),
        fetchOrgProjects(orgId, ac.signal),
        fetchOrgDatasources(orgId, ac.signal),
      ]);
      setWorkspaces(workspacePage.items);
      setProjects(projectPage.items);
      setDatasources(dsPage.items);
      setWorkspaceId((current) =>
        workspacePage.items.some((item) => item.id === current) ? current : (workspacePage.items[0]?.id ?? ""),
      );
      setProjectId((current) =>
        projectPage.items.some((item) => item.id === current) ? current : (projectPage.items[0]?.id ?? ""),
      );
    } catch (reason) {
      if ((reason as Error)?.name === "AbortError") return;
      setError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  }, [orgId]);

  useEffect(() => {
    void loadSummary();
    return () => summaryInflight.current?.abort();
  }, [loadSummary]);

  const loadWorkspaceDetail = useCallback(async () => {
    detailInflight.current?.abort();
    if (!workspaceId) {
      setMembers([]);
      setBindings([]);
      return;
    }
    const ac = new AbortController();
    detailInflight.current = ac;
    const seq = ++detailSeq.current;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const [memberPage, bindingPage] = await Promise.all([
        fetchWorkspaceMembers(workspaceId, ac.signal),
        fetchWorkspaceSourceBindings(workspaceId, ac.signal),
      ]);
      if (seq !== detailSeq.current) return;
      setMembers(memberPage.items);
      setBindings(bindingPage.items.filter((item) => item.status === "PENDING_APPROVAL"));
    } catch (reason) {
      if ((reason as Error)?.name === "AbortError") return;
      if (seq !== detailSeq.current) return;
      setDetailError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      if (seq === detailSeq.current) setDetailLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void loadWorkspaceDetail();
    return () => detailInflight.current?.abort();
  }, [loadWorkspaceDetail]);

  const loadBiConnections = useCallback(async () => {
    biInflight.current?.abort();
    if (!projectId) {
      setBiConnections([]);
      return;
    }
    const ac = new AbortController();
    biInflight.current = ac;
    const seq = ++biSeq.current;
    setBiLoading(true);
    setBiError(null);
    try {
      const page = await fetchProjectBiConnections(projectId, undefined, ac.signal);
      if (seq !== biSeq.current) return;
      setBiConnections(page.items);
    } catch (reason) {
      if ((reason as Error)?.name === "AbortError") return;
      if (seq !== biSeq.current) return;
      setBiError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
    } finally {
      if (seq === biSeq.current) setBiLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadBiConnections();
    return () => biInflight.current?.abort();
  }, [loadBiConnections]);

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

      {error ? (
        <ErrorState title="Workspaces could not be loaded" detail={error} onRetry={() => void loadSummary()} />
      ) : loading ? (
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
            ) : detailError ? (
              <ErrorState
                title="Workspace detail could not be loaded"
                detail={detailError}
                onRetry={() => void loadWorkspaceDetail()}
              />
            ) : detailLoading ? (
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
                <AddMemberForm workspaceId={workspaceId} onAdded={(member) => setMembers((prev) => [...prev, member])} />
                <article className="wsaccess-panel wsaccess-panel--wide">
                  <div className="wsaccess-panel__head">
                    <p className="wsaccess-panel__eyebrow">MAKER-CHECKER</p>
                    <h2 className="wsaccess-panel__h2">Pending source-binding requests</h2>
                    <Pill tone="warn">{bindings.length}</Pill>
                  </div>
                  <PendingBindingsPanel
                    bindings={bindings}
                    datasources={datasources}
                    onDecided={(decided) => {
                      setBindings((prev) => prev.filter((item) => item.id !== decided.id));
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
            ) : biError ? (
              <ErrorState
                title="BI connections could not be loaded"
                detail={biError}
                onRetry={() => void loadBiConnections()}
              />
            ) : biLoading ? (
              <p className="wsaccess-panel__note">Loading...</p>
            ) : (
              <div className="wsaccess__cols">
                <article className="wsaccess-panel wsaccess-panel--wide">
                  <div className="wsaccess-panel__head">
                    <p className="wsaccess-panel__eyebrow">BI / TABLEAU LINEAGE</p>
                    <h2 className="wsaccess-panel__h2">Connections</h2>
                  </div>
                  <BiConnectionsPanel
                    connections={biConnections}
                    onImported={() => {
                      /* the import result is shown inline by ImportArtifactForm itself */
                    }}
                  />
                </article>
                <CreateBiConnectionForm
                  projectId={projectId}
                  datasources={datasources}
                  onCreated={(connection) => setBiConnections((prev) => [...prev, connection])}
                />
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
