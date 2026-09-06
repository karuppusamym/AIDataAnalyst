import { useCallback, useState } from "react";
import type {
  BiArtifactImportRead,
  BiConnectionCreate,
  BiConnectionRead,
  DataSourceRead,
} from "../lib/types";
import { createBiConnection, importBiArtifact } from "../lib/api";
import { Button, Empty, Field, Pill } from "../components/primitives";
import { FormError, FormSuccess, useSubmitAction } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Everything scoped to the selected project: the BI tools whose reports and
   metrics join the catalog's lineage.

   Registering a connection and importing an artifact are one unit because
   neither means anything alone -- a connection with no import contributes no
   lineage, and an import has nowhere to attach without a connection. They are
   also the only part of this screen keyed on a *project* rather than a
   workspace, which is why they are not in `WorkspaceAccessMembership.tsx`.

     Register BI conn   POST /v1/projects/{id}/bi-connections          (bi_api.py:171)
     List BI conns      GET  /v1/projects/{id}/bi-connections          (bi_api.py:226)
     Import BI artifact POST /v1/bi-connections/{id}/artifact-imports  (bi_api.py:258)

   No Power BI/Looker artifact-shape-specific parsing: `BiArtifactImportRequest.artifact`
   is posted as whatever JSON the textarea parses to, exactly like the legacy
   `#bi-import-form`'s `parseJson(data.get("artifact"), "Artifact")` -- this
   screen does not attempt to validate report/metric shape client-side beyond
   "is it JSON".
--------------------------------------------------------------------------- */

const CONNECTION_KEY_RE = /^[a-z][a-z0-9_-]{1,99}$/;
const BI_TOOLS: BiConnectionCreate["bi_tool"][] = ["TABLEAU", "POWER_BI", "LOOKER"];

export function CreateBiConnectionForm({
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
  const action = useSubmitAction<BiConnectionRead>();

  const projectDatasources = datasources.filter((item) => item.project_id === projectId);
  const valid = Boolean(datasourceId) && CONNECTION_KEY_RE.test(connectionKey) && displayName.trim().length >= 2;

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: BiConnectionCreate = {
      datasource_id: datasourceId,
      bi_tool: biTool,
      connection_key: connectionKey,
      display_name: displayName.trim(),
      site_or_workspace: siteOrWorkspace.trim() || null,
    };
    const connection = await action.run(() => createBiConnection(projectId, body));
    if (!connection) return;
    setConnectionKey("");
    setDisplayName("");
    setSiteOrWorkspace("");
    onCreated(connection);
  }, [valid, projectId, datasourceId, biTool, connectionKey, displayName, siteOrWorkspace, action, onCreated]);

  if (projectDatasources.length === 0) {
    return (
      <div className="wsaccess-panel" aria-label="Register BI connection">
        <div className="wsaccess-panel__head">
          <p className="wsaccess-panel__eyebrow">BI / TABLEAU LINEAGE</p>
          <h2 className="wsaccess-panel__h2">Register BI connection</h2>
        </div>
        <Empty
          title="No sources under this project"
          hint="Register a datasource for this project before connecting a BI tool."
        />
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
          <select value={biTool} onChange={(event) => setBiTool(event.target.value as BiConnectionCreate["bi_tool"])}>
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
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? <FormSuccess>Registered "{action.result.display_name}".</FormSuccess> : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Registering..." : "Register connection"}
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
  const action = useSubmitAction<BiArtifactImportRead>();

  const submit = useCallback(async () => {
    // Malformed JSON is reported the same way a rejected import is, in the
    // same place, rather than as a browser exception the user never sees.
    let artifact: Record<string, unknown>;
    try {
      artifact = JSON.parse(artifactText) as Record<string, unknown>;
    } catch {
      action.fail("Artifact is not valid JSON.");
      return;
    }
    const importRead = await action.run(() => importBiArtifact(connectionId, { bi_tool: biTool, artifact }));
    if (!importRead) return;
    setArtifactText("");
    onImported(connectionId, importRead);
  }, [artifactText, biTool, connectionId, action, onImported]);

  if (!open) {
    return <Button onClick={() => setOpen(true)}>Import artifact</Button>;
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
        <select value={biTool} onChange={(event) => setBiTool(event.target.value as BiConnectionCreate["bi_tool"])}>
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
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? (
        <FormSuccess>
          Imported: {action.result.report_count} reports, {action.result.metric_count} metrics,{" "}
          {action.result.matched_column_count} matched / {action.result.unmatched_column_count} unmatched columns.
        </FormSuccess>
      ) : null}
      <div className="wsaccess-import__actions">
        <Button type="submit" variant="primary" disabled={action.submitting || artifactText.trim().length === 0}>
          {action.submitting ? "Importing..." : "Import"}
        </Button>
        <Button onClick={() => setOpen(false)}>Close</Button>
      </div>
    </form>
  );
}

export function BiConnectionsPanel({
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
