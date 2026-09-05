import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  DataSourceRead,
  DbtArtifactImportRead,
  DbtArtifactImportRequest,
  DbtLineageRead,
  DbtProjectCreate,
  DbtProjectRead,
  ProjectRead,
} from "../lib/types";
import type { DbtResourceRead } from "../lib/api";
import {
  ApiError,
  createDbtProject,
  fetchDbtArtifactImports,
  fetchDbtLineage,
  fetchDbtProjects,
  fetchDbtResources,
  fetchOrgDatasources,
  fetchOrgProjects,
  importDbtManifest,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./TransformationsScreen.css";

/* ---------------------------------------------------------------------------
   Transformations -- nav id `transformations`, ported from the legacy
   portal's `transformations-view` (`ui/index.html`, rendering logic in
   `ui/scripts/features/transformation-workbench.js`).

   Locating this screen took more than the usual grep: `data-view=
   "transformations"` IS a normal top-level sidebar button
   (`ui/index.html`'s nav, `ui/app.js`'s `NAV_INDEX`) -- it just does not
   render like one until an administrator opts in. `applyIntegrationPolicy
   Visibility` (`ui/scripts/features/integration-policy.js`) hides the nav
   button with an `integration-hidden` class, redirects `#transformations`
   to `#administration`, and even bounces you off the view if you are
   already on it, all whenever `transformationMetadataSurfaceEnabled()` is
   false -- i.e. whenever the organization's integration policy has no
   transformation-metadata adapter (dbt/OpenLineage/Airflow/generic ELT)
   turned on. A default organization ships with that policy unset, which is
   exactly why an earlier click on the nav button "did nothing": there was
   nothing to click. This port does not attempt to reproduce that
   nav-hiding behavior (nav wiring is centralized in `App.tsx` and out of
   this task's scope) -- it reproduces the more important part: the same
   backend gate applied to every dbt route, surfaced with the same real
   detail string.

   Backend, `src/aida/dbt_api.py` -- every one of the five reads/writes
   below scopes through `_project_scope`/`_dbt_project_scope`/
   `_artifact_scope` (lines 58-92), each of which calls
   `_require_dbt_integration` (line 138) before doing anything else. That
   raises `403 "dbt integration is disabled for this organization"` the
   moment `transformation_metadata_integration_enabled(policy, "dbt")` is
   false -- so every fetch below can 403, not just the create calls, and
   this screen renders that exact detail string (`DbtDisabledState` below)
   rather than a generic error banner, matching legacy's own
   `renderDbtDisabledState()`.

     1. List dbt projects     GET  /v1/projects/{project_id}/dbt-projects
                                                          (dbt_api.py:200)
     2. Register dbt project  POST /v1/projects/{project_id}/dbt-projects
                                                          (dbt_api.py:149)
     3. List artifact imports GET  /v1/dbt-projects/{id}/artifact-imports
                                                          (dbt_api.py:432)
     4. Import a manifest     POST /v1/dbt-projects/{id}/artifact-imports
                                                          (dbt_api.py:232)
     5. List resources        GET  /v1/dbt-artifact-imports/{id}/resources
                                                          (dbt_api.py:467)
     6. Get lineage edges     GET  /v1/dbt-artifact-imports/{id}/lineage
                                                          (dbt_api.py:507)

   `DbtResourceRead` is not in the shared `types.ts` (the four other dbt
   reads already are, reused verbatim); it is defined next to the fetch
   function that needs it in `api.ts`, matching that file's own precedent
   for response shapes with no other caller yet (`AgentAskError`,
   `ReviewQueueQuery`) rather than growing the large shared file for a type
   this port is the only consumer of.

   The project picker reuses `fetchOrgProjects` -- the same delivery-project
   scoping `SemanticsScreen`/`ContextProductsScreen` already use, since dbt
   projects are registered per delivery project, not per organization
   (`create_dbt_project`'s own `_project_scope`). The datasource picker in
   "Register dbt project" reuses `fetchOrgDatasources` and filters to
   `project_id === projectId` client-side, the exact rule legacy's own
   `populateProjectSources` applies (`ui/scripts/core.js:50`).

   Scope cut, stated rather than silently dropped: the legacy DAG canvas
   (Cytoscape-based, `ui/scripts/graph-engine.js`, with an interactive
   column-lineage mode and node-expand column popovers) is not reimplemented
   here. `NarratedLineageScreen`'s own comment set the precedent for this
   codebase: re-implementing a legacy Cytoscape canvas in `ui-next` before
   `ui/` is actually retired duplicates real, already-shipped work rather
   than migrating it. What ships here instead is the same real dependency
   data the DAG's "Edge List" mode already renders -- source resource,
   target resource, and edge type, from the same `GET .../lineage` response
   -- as a flat list, not a fabricated graph.
--------------------------------------------------------------------------- */

const RESOURCE_TYPES = ["ALL", "MODEL", "SOURCE", "TEST", "SEED", "SNAPSHOT", "SEMANTIC_MODEL", "METRIC"] as const;
const MATCH_FILTERS = ["ALL", "MATCHED", "UNMATCHED"] as const;

const projectStatusTone = (s: string): Tone => (s === "ACTIVE" ? "ok" : "mute");
const importStatusTone = (s: string): Tone => (s === "IMPORTED" ? "ok" : s === "FAILED" ? "bad" : "mute");
const testTone = (status: string | null): Tone =>
  status === "PASS" ? "ok" : status === "FAIL" || status === "ERROR" ? "bad" : status === "SKIPPED" ? "mute" : "warn";
const matchTone = (matched: boolean): Tone => (matched ? "ok" : "warn");

function DbtDisabledState({ detail }: { detail: string }) {
  return (
    <div className="txdisabled" role="alert">
      <div className="txdisabled__title">Transformation metadata is unavailable for this organization</div>
      <p className="txdisabled__body">{detail}</p>
      <p className="txdisabled__hint">
        An administrator enables transformation-metadata adapters from the legacy portal&rsquo;s
        Administration &rarr; &ldquo;Transformation metadata surfaces&rdquo; panel
        (<code>PUT /v1/organizations/&#123;id&#125;/integration-policy</code>). That control has not
        been ported to this shell&rsquo;s Administration screen yet (a stated scope cut there), so
        enabling dbt currently requires the legacy portal at :3000.
      </p>
    </div>
  );
}

function DbtProjectRow({
  project,
  datasourceName,
  selected,
  onSelect,
}: {
  project: DbtProjectRead;
  datasourceName: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button className={`txdp${selected ? " txdp--sel" : ""}`} onClick={onSelect} aria-current={selected}>
      <div className="txdp__head">
        <span className="txdp__name" title={project.display_name}>{project.display_name}</span>
        <Pill tone={projectStatusTone(project.status)}>{project.status.toLowerCase()}</Pill>
      </div>
      <div className="txdp__meta">
        <span>{project.project_key}</span>
        <span>&middot;</span>
        <span>target {project.target_name}</span>
        <span>&middot;</span>
        <span>{datasourceName}</span>
      </div>
    </button>
  );
}

function ArtifactImportRow({
  artifact,
  selected,
  onSelect,
}: {
  artifact: DbtArtifactImportRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button className={`txim${selected ? " txim--sel" : ""}`} onClick={onSelect} aria-current={selected}>
      <div className="txim__head">
        <span className="txim__when">{new Date(artifact.generated_at ?? artifact.created_at).toLocaleString()}</span>
        <Pill tone={importStatusTone(artifact.status)}>{artifact.status.toLowerCase()}</Pill>
      </div>
      <div className="txim__meta">
        <span>dbt {artifact.dbt_version ?? "unknown"}</span>
        <span>&middot;</span>
        <span>{artifact.model_count} models / {artifact.source_count} sources / {artifact.test_count} tests</span>
        <span>&middot;</span>
        <span>{artifact.matched_resource_count} matched, {artifact.unmatched_resource_count} open</span>
      </div>
    </button>
  );
}

function ResourceRow({
  resource,
  selected,
  onSelect,
}: {
  resource: DbtResourceRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`txres${selected ? " txres--sel" : ""}`} aria-label={resource.name}>
      <button className="txres__click" onClick={onSelect}>
        <div className="txres__head">
          <span className="txres__name" title={resource.name}>{resource.name}</span>
          <div className="txres__badges">
            <Pill tone="mute">{resource.resource_type.toLowerCase()}</Pill>
            {resource.test_status ? (
              <Pill tone={testTone(resource.test_status)}>
                {resource.test_status.toLowerCase()}{resource.test_failures ? ` (${resource.test_failures})` : ""}
              </Pill>
            ) : null}
          </div>
        </div>
        <div className="txres__meta">
          <span>{resource.package_name}</span>
          <span>&middot;</span>
          <span className="txres__uid">{resource.unique_id}</span>
        </div>
        <div className="txres__meta">
          <span>{resource.materialization ?? "not applicable"}</span>
          <span>&middot;</span>
          <Pill tone={matchTone(Boolean(resource.matched_table_id))}>
            {resource.matched_table_id ? "matched" : "unmatched"}
          </Pill>
          <span>&middot;</span>
          <span>{resource.column_names.length} columns</span>
        </div>
      </button>
    </article>
  );
}

function ResourceDetailPane({
  resource,
  onClose,
}: {
  resource: DbtResourceRead;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const permalink = useMemo(() => {
    const p = new URLSearchParams(location.search);
    p.set("resource", resource.id);
    return `${location.origin}${location.pathname}?${p.toString()}`;
  }, [resource.id]);

  return (
    <aside className="evp" aria-label={`Detail for ${resource.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={resource.name}>{resource.name}</div>
          <div className="evp__path">{resource.resource_type.toLowerCase()} &middot; {resource.unique_id}</div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close resource detail">&times;</button>
      </header>
      <div className="evp__body">
        {resource.test_status ? (
          <div className={`txtestbanner txtestbanner--${testTone(resource.test_status)}`} role="status">
            <strong>Test execution: {resource.test_status.toLowerCase()}</strong>
            <p>
              {resource.test_failures !== null && resource.test_failures !== undefined
                ? `${resource.test_failures} failing row${resource.test_failures === 1 ? "" : "s"} observed.`
                : "Assertion executed with no recorded failure count."}
              {resource.test_execution_time !== null && resource.test_execution_time !== undefined
                ? ` Execution time ${resource.test_execution_time.toFixed(2)}s.`
                : ""}
            </p>
          </div>
        ) : null}

        <ol className="evl">
          <li className="evi evi--info">
            <div className="evi__label">Relation</div>
            <div className="evi__value">{resource.relation_name ?? "Not a warehouse relation"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Materialization</div>
            <div className="evi__value">{resource.materialization ?? "Not applicable"}</div>
          </li>
          <li className={`evi evi--${resource.matched_table_id ? "ok" : "warn"}`}>
            <div className="evi__label">Catalog mapping</div>
            <div className="evi__value">{resource.matched_table_id ?? "Unmatched"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Source file</div>
            <div className="evi__value">{resource.original_file_path ?? "Not recorded"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Tags</div>
            <div className="evi__value">{resource.tags.join(", ") || "None"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">SQL fingerprint</div>
            <div className="evi__value">{resource.compiled_sql_hash ?? "No compiled SQL"}</div>
          </li>
        </ol>

        {resource.column_names.length > 0 ? (
          <>
            <div className="evp__sub" style={{ marginTop: 14 }}>Columns & physical schema types</div>
            <div className="txcolwrap">
              <table className="txcoltable">
                <thead>
                  <tr><th>Column</th><th>Physical type</th><th>Documentation</th></tr>
                </thead>
                <tbody>
                  {resource.column_names.map((col) => (
                    <tr key={col}>
                      <td><strong>{col}</strong></td>
                      <td><code>{resource.column_types[col] ?? "Not resolved"}</code></td>
                      <td>{resource.column_descriptions[col] ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}

        {Object.keys(resource.extra_metadata).length > 0 ? (
          <>
            <div className="evp__sub" style={{ marginTop: 14 }}>Downstream & exposure metadata</div>
            <ol className="evl">
              {Object.entries(resource.extra_metadata).map(([k, v]) => (
                <li key={k} className="evi evi--info">
                  <div className="evi__label">{k.replace(/_/g, " ")}</div>
                  <div className="evi__value">{String(v)}</div>
                </li>
              ))}
            </ol>
          </>
        ) : null}

        <div className="evp__sub" style={{ marginTop: 14 }}>Literal-redacted compiled SQL</div>
        {resource.compiled_sql_redacted ? (
          <pre className="txsql">{resource.compiled_sql_redacted}</pre>
        ) : (
          <p className="txnone">
            Compiled SQL was not present or could not be safely normalized; only its fingerprint is retained.
          </p>
        )}
      </div>
      <footer className="evp__foot">
        <Button
          onClick={() => {
            void navigator.clipboard?.writeText(permalink);
            setCopied(true);
          }}
        >
          {copied ? "Link copied" : "Copy resource link"}
        </Button>
        <span className="evp__hint">Evidence, not source values &mdash; literals are redacted</span>
      </footer>
    </aside>
  );
}

function RegisterProjectForm({
  orgProjectId,
  datasources,
  onCreated,
}: {
  orgProjectId: string;
  datasources: DataSourceRead[];
  onCreated: (project: DbtProjectRead) => void;
}) {
  const [projectKey, setProjectKey] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [datasourceId, setDatasourceId] = useState("");
  const [targetName, setTargetName] = useState("prod");
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDatasourceId((current) => (datasources.some((d) => d.id === current) ? current : datasources[0]?.id ?? ""));
  }, [datasources]);

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!datasourceId) {
        setError("This delivery project has no registered warehouse source to bind the dbt project to.");
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const body: DbtProjectCreate = {
          project_key: projectKey,
          display_name: displayName,
          datasource_id: datasourceId,
          repository_url: repositoryUrl.trim() || null,
          target_name: targetName.trim() || "prod",
        };
        const created = await createDbtProject(orgProjectId, body);
        onCreated(created);
        setProjectKey("");
        setDisplayName("");
        setRepositoryUrl("");
        setTargetName("prod");
      } catch (err) {
        setError(err instanceof ApiError ? err.detail : (err as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [orgProjectId, projectKey, displayName, datasourceId, targetName, repositoryUrl, onCreated],
  );

  return (
    <form className="txform" onSubmit={(e) => void submit(e)}>
      <div className="txform__grid">
        <Field label="Project key">
          <input
            required
            pattern="[a-z][a-z0-9_-]{1,99}"
            placeholder="consumer_analytics"
            value={projectKey}
            onChange={(e) => setProjectKey(e.target.value)}
          />
        </Field>
        <Field label="Display name">
          <input
            required
            minLength={2}
            placeholder="Consumer analytics transformations"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </Field>
        <Field label="Warehouse data source">
          <select required value={datasourceId} onChange={(e) => setDatasourceId(e.target.value)}>
            {datasources.length === 0 ? <option value="">No sources in project</option> : null}
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Target name">
          <input required value={targetName} onChange={(e) => setTargetName(e.target.value)} />
        </Field>
        <div className="txform__span2">
          <Field label="Repository URL (optional)">
            <input
              placeholder="https://git.example/bank/consumer-analytics"
              value={repositoryUrl}
              onChange={(e) => setRepositoryUrl(e.target.value)}
            />
          </Field>
        </div>
      </div>
      <p className="txform__privacy">
        Repository credentials are not accepted. This registration stores only ownership and warehouse mapping.
      </p>
      {error ? <div className="txform__err" role="alert">{error}</div> : null}
      <Button type="submit" variant="primary" disabled={busy}>
        {busy ? "Registering…" : "Register dbt project"}
      </Button>
    </form>
  );
}

function ImportManifestForm({
  dbtProjectId,
  onImported,
}: {
  dbtProjectId: string;
  onImported: (artifact: DbtArtifactImportRead) => void;
}) {
  const manifestRef = useRef<HTMLInputElement>(null);
  const catalogRef = useRef<HTMLInputElement>(null);
  const runResultsRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const manifestFile = manifestRef.current?.files?.[0];
      if (!manifestFile) {
        setError("Choose a dbt manifest.json file.");
        return;
      }
      if (manifestFile.size > 32 * 1024 * 1024) {
        setError("The manifest exceeds the 32 MiB ingestion limit.");
        return;
      }
      setBusy(true);
      setError(null);
      try {
        let manifest: Record<string, unknown>;
        try {
          manifest = JSON.parse(await manifestFile.text()) as Record<string, unknown>;
        } catch {
          throw new Error("The manifest.json file is not valid JSON.");
        }
        const catalogFile = catalogRef.current?.files?.[0];
        let catalog: Record<string, unknown> | null = null;
        if (catalogFile) {
          try {
            catalog = JSON.parse(await catalogFile.text()) as Record<string, unknown>;
          } catch {
            throw new Error("The catalog.json file is not valid JSON.");
          }
        }
        const runResultsFile = runResultsRef.current?.files?.[0];
        let runResults: Record<string, unknown> | null = null;
        if (runResultsFile) {
          try {
            runResults = JSON.parse(await runResultsFile.text()) as Record<string, unknown>;
          } catch {
            throw new Error("The run_results.json file is not valid JSON.");
          }
        }
        const body: DbtArtifactImportRequest = { manifest, catalog, run_results: runResults };
        const imported = await importDbtManifest(dbtProjectId, body);
        onImported(imported);
        if (manifestRef.current) manifestRef.current.value = "";
        if (catalogRef.current) catalogRef.current.value = "";
        if (runResultsRef.current) runResultsRef.current.value = "";
      } catch (err) {
        setError(err instanceof ApiError ? err.detail : (err as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [dbtProjectId, onImported],
  );

  return (
    <form className="txform" onSubmit={(e) => void submit(e)}>
      <div className="txform__grid">
        <div className="txform__span2">
          <Field label="manifest.json (required)">
            <input ref={manifestRef} type="file" accept="application/json,.json" required />
          </Field>
        </div>
        <Field label="catalog.json (optional — physical types)">
          <input ref={catalogRef} type="file" accept="application/json,.json" />
        </Field>
        <Field label="run_results.json (optional — test results)">
          <input ref={runResultsRef} type="file" accept="application/json,.json" />
        </Field>
      </div>
      <p className="txform__privacy">
        Atlas retains resource metadata, column descriptions, catalog data types, test execution
        outcomes, and literal-redacted compiled SQL. Raw artifacts are not persisted.
      </p>
      {error ? <div className="txform__err" role="alert">{error}</div> : null}
      <Button type="submit" variant="primary" disabled={busy}>
        {busy ? "Validating artifact…" : "Validate and import"}
      </Button>
    </form>
  );
}

export function TransformationsScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const orgProjectId = params.get("project");
  const dbtProjectId = params.get("dbtProject");
  const importId = params.get("import");
  const resourceId = params.get("resource");
  const typeFilter = params.get("type") ?? "ALL";
  const matchFilter = params.get("match") ?? "ALL";

  const [orgProjects, setOrgProjects] = useState<ProjectRead[]>([]);
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);

  useEffect(() => {
    const ac = new AbortController();
    Promise.all([fetchOrgProjects(ORG, ac.signal), fetchOrgDatasources(ORG, ac.signal)])
      .then(([projectPage, datasourcePage]) => {
        setOrgProjects(projectPage.items);
        setDatasources(datasourcePage.items);
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const projectDatasources = useMemo(
    () => datasources.filter((d) => d.project_id === orgProjectId),
    [datasources, orgProjectId],
  );

  // dbt projects for the selected delivery project.
  const [dbtProjects, setDbtProjects] = useState<DbtProjectRead[]>([]);
  const [dbtProjectsLoading, setDbtProjectsLoading] = useState(false);
  const [dbtDisabledDetail, setDbtDisabledDetail] = useState<string | null>(null);
  const [dbtProjectsError, setDbtProjectsError] = useState<string | null>(null);
  const [showRegisterForm, setShowRegisterForm] = useState(false);

  const dbtInflight = useRef<AbortController | null>(null);
  const loadDbtProjects = useCallback(async () => {
    if (!orgProjectId) {
      setDbtProjects([]);
      setDbtDisabledDetail(null);
      setDbtProjectsError(null);
      return;
    }
    dbtInflight.current?.abort();
    const ac = new AbortController();
    dbtInflight.current = ac;
    setDbtProjectsLoading(true);
    setDbtProjectsError(null);
    setDbtDisabledDetail(null);
    try {
      const page = await fetchDbtProjects(orgProjectId, ac.signal);
      setDbtProjects(page.items);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (e instanceof ApiError && e.status === 403) {
        setDbtDisabledDetail(e.detail);
        setDbtProjects([]);
      } else {
        setDbtProjectsError(e instanceof ApiError ? e.detail : (e as Error).message);
      }
    } finally {
      setDbtProjectsLoading(false);
    }
  }, [orgProjectId]);

  useEffect(() => {
    void loadDbtProjects();
    return () => dbtInflight.current?.abort();
  }, [loadDbtProjects]);

  // Auto-select the first dbt project once its list loads, mirroring
  // legacy's `selectDbtProject(preferred)` in `loadDbtProjects()`. Gated on
  // `dbtProjectsLoading` so a delivery-project switch never auto-selects
  // the PREVIOUS project's stale dbt-project list during the fetch gap.
  useEffect(() => {
    if (dbtProjectsLoading) return;
    if (dbtProjectId && dbtProjects.some((p) => p.id === dbtProjectId)) return;
    if (dbtProjects.length > 0) setParams({ dbtProject: dbtProjects[0]!.id, import: null, resource: null });
    else if (dbtProjectId) setParams({ dbtProject: null, import: null, resource: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dbtProjects, dbtProjectsLoading]);

  // Artifact imports for the selected dbt project.
  const [imports, setImports] = useState<DbtArtifactImportRead[]>([]);
  const [importsLoading, setImportsLoading] = useState(false);
  const [importsError, setImportsError] = useState<string | null>(null);
  const [showImportForm, setShowImportForm] = useState(false);

  const importsInflight = useRef<AbortController | null>(null);
  const loadImports = useCallback(async () => {
    if (!dbtProjectId) {
      setImports([]);
      return;
    }
    importsInflight.current?.abort();
    const ac = new AbortController();
    importsInflight.current = ac;
    setImportsLoading(true);
    setImportsError(null);
    try {
      const page = await fetchDbtArtifactImports(dbtProjectId, ac.signal);
      setImports(page.items);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setImportsError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setImportsLoading(false);
    }
  }, [dbtProjectId]);

  useEffect(() => {
    void loadImports();
    return () => importsInflight.current?.abort();
  }, [loadImports]);

  useEffect(() => {
    if (importsLoading) return;
    if (importId && imports.some((i) => i.id === importId)) return;
    if (imports.length > 0) setParams({ import: imports[0]!.id, resource: null });
    else if (importId) setParams({ import: null, resource: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imports, importsLoading]);

  const selectedArtifact = useMemo(() => imports.find((i) => i.id === importId) ?? null, [imports, importId]);

  // Resources + lineage for the selected artifact import.
  const [resources, setResources] = useState<DbtResourceRead[]>([]);
  const [resourceTotal, setResourceTotal] = useState<number | null>(null);
  const [resourcesLoading, setResourcesLoading] = useState(false);
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [lineage, setLineage] = useState<DbtLineageRead | null>(null);

  const resourcesInflight = useRef<AbortController | null>(null);
  const loadResources = useCallback(async () => {
    if (!importId) {
      setResources([]);
      setResourceTotal(null);
      setLineage(null);
      return;
    }
    resourcesInflight.current?.abort();
    const ac = new AbortController();
    resourcesInflight.current = ac;
    setResourcesLoading(true);
    setResourcesError(null);
    try {
      const [resourcePage, lineageRead] = await Promise.all([
        fetchDbtResources(
          importId,
          { resourceType: typeFilter === "ALL" ? null : typeFilter, matched: matchFilter === "ALL" ? null : matchFilter === "MATCHED", limit: 500 },
          ac.signal,
        ),
        fetchDbtLineage(importId, ac.signal),
      ]);
      setResources(resourcePage.items);
      setResourceTotal(resourcePage.total);
      setLineage(lineageRead);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setResourcesError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setResourcesLoading(false);
    }
  }, [importId, typeFilter, matchFilter]);

  useEffect(() => {
    void loadResources();
    return () => resourcesInflight.current?.abort();
  }, [loadResources]);

  const selectedResource = useMemo(() => resources.find((r) => r.id === resourceId) ?? null, [resources, resourceId]);

  const nodeLabel = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of lineage?.nodes ?? []) map.set(node.id, node.label);
    return map;
  }, [lineage]);

  return (
    <div className="txscreen">
      <header className="txscreen__head">
        <div>
          <p className="txscreen__eyebrow">TRANSFORMATION METADATA</p>
          <h1 className="txscreen__h1">Transformations</h1>
          <p className="txscreen__lede">
            Ingest external transformation metadata as evidence for lineage, impact analysis,
            search, and AI grounding &mdash; without running dbt inside Atlas.
          </p>
        </div>
        <div className="txscreen__filters">
          <Field label="Delivery project">
            <select
              value={orgProjectId ?? ""}
              onChange={(e) => setParams({ project: e.target.value || null, dbtProject: null, import: null, resource: null })}
            >
              <option value="">Select a project&hellip;</option>
              {orgProjects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </Field>
          <Button onClick={() => { void loadDbtProjects(); void loadImports(); void loadResources(); }}>Refresh</Button>
        </div>
      </header>

      {!orgProjectId ? (
        <Empty title="Pick a delivery project" hint="dbt projects are registered per delivery project, same as Semantics and Context products." />
      ) : dbtDisabledDetail ? (
        <DbtDisabledState detail={dbtDisabledDetail} />
      ) : dbtProjectsError ? (
        <ErrorState title="Transformation metadata could not be loaded" detail={dbtProjectsError} onRetry={() => void loadDbtProjects()} />
      ) : (
        <div className="txscreen__body">
          <div className="txscreen__main">
            <section className="txpanel">
              <header className="txpanel__head">
                <div>
                  <p className="txpanel__eyebrow">DBT PROJECTS</p>
                  <h2 className="txpanel__h2">Warehouse transformation estates</h2>
                </div>
                <Button onClick={() => setShowRegisterForm((v) => !v)}>
                  {showRegisterForm ? "Cancel" : "Register dbt project"}
                </Button>
              </header>
              {showRegisterForm ? (
                <RegisterProjectForm
                  orgProjectId={orgProjectId}
                  datasources={projectDatasources}
                  onCreated={(created) => {
                    // Re-fetch from the server rather than append locally --
                    // the create-then-list pattern `ContextProductsScreen`'s
                    // `submitCreate` already established for this codebase.
                    setShowRegisterForm(false);
                    setParams({ dbtProject: created.id, import: null, resource: null });
                    void loadDbtProjects();
                  }}
                />
              ) : null}
              {dbtProjectsLoading ? (
                <div className="txskeleton" role="status">Loading dbt projects&hellip;</div>
              ) : dbtProjects.length === 0 ? (
                <Empty title="No dbt projects registered" hint="Register one against a governed warehouse source above." />
              ) : (
                <div className="txlist">
                  {dbtProjects.map((p) => (
                    <DbtProjectRow
                      key={p.id}
                      project={p}
                      datasourceName={datasources.find((d) => d.id === p.datasource_id)?.name ?? p.datasource_id}
                      selected={p.id === dbtProjectId}
                      onSelect={() => setParams({ dbtProject: p.id, import: null, resource: null })}
                    />
                  ))}
                </div>
              )}
            </section>

            {dbtProjectId ? (
              <section className="txpanel">
                <header className="txpanel__head">
                  <div>
                    <p className="txpanel__eyebrow">DBT IMPORTS</p>
                    <h2 className="txpanel__h2">Immutable artifact history</h2>
                  </div>
                  <Button onClick={() => setShowImportForm((v) => !v)}>
                    {showImportForm ? "Cancel" : "Import dbt manifest"}
                  </Button>
                </header>
                {showImportForm ? (
                  <ImportManifestForm
                    dbtProjectId={dbtProjectId}
                    onImported={(imported) => {
                      setShowImportForm(false);
                      setParams({ import: imported.id, resource: null });
                      void loadImports();
                    }}
                  />
                ) : null}
                {importsLoading ? (
                  <div className="txskeleton" role="status">Loading artifact imports&hellip;</div>
                ) : importsError ? (
                  <ErrorState title="Artifact imports could not be loaded" detail={importsError} onRetry={() => void loadImports()} />
                ) : imports.length === 0 ? (
                  <Empty title="No manifest imports yet" hint="Import a manifest.json above to begin." />
                ) : (
                  <div className="txlist">
                    {imports.map((i) => (
                      <ArtifactImportRow
                        key={i.id}
                        artifact={i}
                        selected={i.id === importId}
                        onSelect={() => setParams({ import: i.id, resource: null })}
                      />
                    ))}
                  </div>
                )}
              </section>
            ) : null}

            {selectedArtifact ? (
              <>
                <section className="txmetrics">
                  <div className="txmetric"><p>Models</p><strong>{selectedArtifact.model_count}</strong><small>Compiled transformation nodes</small></div>
                  <div className="txmetric"><p>Sources</p><strong>{selectedArtifact.source_count}</strong><small>Declared upstream relations</small></div>
                  <div className="txmetric"><p>Catalog matches</p><strong>{selectedArtifact.matched_resource_count}</strong><small>{selectedArtifact.unmatched_resource_count} relation mappings need attention</small></div>
                  <div className="txmetric"><p>Lineage edges</p><strong>{selectedArtifact.lineage_edge_count}</strong><small>{selectedArtifact.test_count} test nodes included</small></div>
                </section>

                <section className="txpanel">
                  <div className="txfilterbar">
                    <Field label="Resource type">
                      <select value={typeFilter} onChange={(e) => setParams({ type: e.target.value === "ALL" ? null : e.target.value })}>
                        {RESOURCE_TYPES.map((t) => (
                          <option key={t} value={t}>{t === "ALL" ? "All resources" : t}</option>
                        ))}
                      </select>
                    </Field>
                    <Field label="Catalog link">
                      <select value={matchFilter} onChange={(e) => setParams({ match: e.target.value === "ALL" ? null : e.target.value })}>
                        {MATCH_FILTERS.map((m) => (
                          <option key={m} value={m}>{m === "ALL" ? "All resources" : m === "MATCHED" ? "Matched relations" : "Not catalog-linked"}</option>
                        ))}
                      </select>
                    </Field>
                    <span className="txfilterbar__count">{resourceTotal ?? resources.length} resource{(resourceTotal ?? resources.length) === 1 ? "" : "s"}</span>
                  </div>

                  {resourcesLoading ? (
                    <div className="txskeleton" role="status">Loading resource inventory&hellip;</div>
                  ) : resourcesError ? (
                    <ErrorState title="Resource inventory could not be loaded" detail={resourcesError} onRetry={() => void loadResources()} />
                  ) : resources.length === 0 ? (
                    <Empty title="No resources match these filters" hint="Try clearing the resource type or catalog-link filter." />
                  ) : (
                    <div className="txreslist">
                      {resources.map((r) => (
                        <ResourceRow key={r.id} resource={r} selected={r.id === resourceId} onSelect={() => setParams({ resource: r.id })} />
                      ))}
                    </div>
                  )}
                </section>

                <section className="txpanel">
                  <header className="txpanel__head">
                    <div>
                      <p className="txpanel__eyebrow">DEPENDENCY EVIDENCE</p>
                      <h2 className="txpanel__h2">Lineage edges</h2>
                    </div>
                  </header>
                  {!lineage || lineage.edges.length === 0 ? (
                    <Empty title="No dependencies declared" />
                  ) : (
                    <div className="txedges">
                      {lineage.edges.slice(0, 100).map((edge) => (
                        <div className="txedge" key={edge.id}>
                          <div className="txedge__node">
                            <strong>{nodeLabel.get(edge.source_resource_id) ?? edge.source_resource_id}</strong>
                          </div>
                          <span className="txedge__arrow" aria-hidden="true">&rarr;</span>
                          <div className="txedge__node">
                            <strong>{nodeLabel.get(edge.target_resource_id) ?? edge.target_resource_id}</strong>
                          </div>
                          <Pill tone="mute">{edge.edge_type === "COLUMN_DEPENDS_ON" ? "column" : "depends on"}</Pill>
                        </div>
                      ))}
                      {lineage.edges.length > 100 ? (
                        <p className="txform__privacy">Showing first 100 of {lineage.edges.length} edges.</p>
                      ) : null}
                    </div>
                  )}
                </section>
              </>
            ) : null}
          </div>

          {selectedResource ? (
            <ResourceDetailPane resource={selectedResource} onClose={() => setParams({ resource: null })} />
          ) : (
            <aside className="evp evp--idle" aria-label="Resource detail">
              <Empty
                title={importId ? "Select a resource" : "Select an artifact import"}
                hint="Columns, physical types, catalog mapping, and literal-redacted compiled SQL appear here."
              />
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
