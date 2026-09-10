import { useEffect, useMemo, useState } from "react";
import type {
  DataSourceRead,
  DbtArtifactImportRead,
  DbtLineageRead,
  DbtProjectRead,
  ProjectRead,
} from "../lib/types";
import type { DbtResourceRead } from "../lib/api";
import {
  ApiError,
  fetchDbtArtifactImports,
  fetchDbtLineage,
  fetchDbtProjects,
  fetchDbtResources,
  fetchOrgDatasources,
  fetchOrgProjects,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import { useAsyncResource } from "../components/screenState";
import { DbtDisabledState, ImportManifestForm, RegisterProjectForm } from "./TransformationsIngest";
import { ResourceDetailPane, ResourceRow } from "./TransformationsEvidence";
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

   Backend, `src/aida/dbt_api.py` -- every one of the reads/writes below
   scopes through `_project_scope`/`_dbt_project_scope`/`_artifact_scope`
   (lines 58-92), each of which calls `_require_dbt_integration` (line 138)
   before doing anything else. That raises `403 "dbt integration is disabled
   for this organization"` the moment `transformation_metadata_integration_
   enabled(policy, "dbt")` is false -- so every fetch below can 403, not just
   the create calls, and this screen renders that exact detail string
   (`DbtDisabledState`, `TransformationsIngest.tsx`) rather than a generic
   error banner, matching legacy's own `renderDbtDisabledState()`.

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

   THE INVARIANT this file exists to hold: the selection cascade. Delivery
   project decides the dbt projects, a dbt project decides the imports, an
   import decides the resources and lineage -- and each level auto-selects its
   first entry only once its own list has *finished* loading. Gating on the
   loading flag is not cosmetic: without it, switching delivery project
   auto-selects an id from the PREVIOUS project's still-displayed list during
   the fetch gap, and the URL then carries a selection that does not exist in
   the project it names. That rule spans three reads, which is why the three
   reads stay in one file. What each of them *renders* does not, and lives in
   `TransformationsIngest.tsx` (the two writes) and
   `TransformationsEvidence.tsx` (one resource, as a row and as evidence).

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
const MAX_EDGES_SHOWN = 100;

const projectStatusTone = (s: string): Tone => (s === "ACTIVE" ? "ok" : "mute");
const importStatusTone = (s: string): Tone => (s === "IMPORTED" ? "ok" : s === "FAILED" ? "bad" : "mute");

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
        <span className="txdp__name" title={project.display_name}>
          {project.display_name}
        </span>
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
        <span>
          {artifact.model_count} models / {artifact.source_count} sources / {artifact.test_count} tests
        </span>
        <span>&middot;</span>
        <span>
          {artifact.matched_resource_count} matched, {artifact.unmatched_resource_count} open
        </span>
      </div>
    </button>
  );
}

interface Inventory {
  readonly resources: DbtResourceRead[];
  readonly total: number;
  readonly lineage: DbtLineageRead;
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

  /* The org-wide pickers. A failure here is not surfaced: neither list is a
     result the user asked for, and the panels below report their own. */
  const bootstrap = useAsyncResource<{ projects: ProjectRead[]; datasources: DataSourceRead[] }>(
    async (signal) => {
      const [projectPage, datasourcePage] = await Promise.all([
        fetchOrgProjects(ORG, signal),
        fetchOrgDatasources(ORG, signal),
      ]);
      return { projects: projectPage.items, datasources: datasourcePage.items };
    },
    [ORG],
  );
  const orgProjects = bootstrap.data?.projects ?? [];
  const datasources = useMemo(() => bootstrap.data?.datasources ?? [], [bootstrap.data]);

  const projectDatasources = useMemo(
    () => datasources.filter((d) => d.project_id === orgProjectId),
    [datasources, orgProjectId],
  );

  /* A 403 from any dbt route means the integration is off for this whole
     organization, not that this one list failed -- so it is carried as part
     of the answer rather than as an error, and replaces the entire body.
     Derived from the response, never held as separate state: a flag set
     alongside the list can outlive the list it described. */
  const dbtProjectsResource = useAsyncResource<{ items: DbtProjectRead[]; disabled: string | null }>(
    async (signal) => {
      try {
        return { items: (await fetchDbtProjects(orgProjectId!, signal)).items, disabled: null };
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 403) return { items: [], disabled: reason.detail };
        throw reason;
      }
    },
    [orgProjectId],
    { enabled: Boolean(orgProjectId) },
  );
  const dbtProjects = useMemo(() => dbtProjectsResource.data?.items ?? [], [dbtProjectsResource.data]);
  const dbtDisabledDetail = dbtProjectsResource.data?.disabled ?? null;

  const importsResource = useAsyncResource<DbtArtifactImportRead[]>(
    async (signal) => (await fetchDbtArtifactImports(dbtProjectId!, signal)).items,
    [dbtProjectId],
    { enabled: Boolean(dbtProjectId) },
  );
  const imports = useMemo(() => importsResource.data ?? [], [importsResource.data]);

  const inventory = useAsyncResource<Inventory>(
    async (signal) => {
      const [resourcePage, lineage] = await Promise.all([
        fetchDbtResources(
          importId!,
          {
            resourceType: typeFilter === "ALL" ? null : typeFilter,
            matched: matchFilter === "ALL" ? null : matchFilter === "MATCHED",
            limit: 500,
          },
          signal,
        ),
        fetchDbtLineage(importId!, signal),
      ]);
      return { resources: resourcePage.items, total: resourcePage.total, lineage };
    },
    [importId, typeFilter, matchFilter],
    { enabled: Boolean(importId) },
  );
  const resources = useMemo(() => inventory.data?.resources ?? [], [inventory.data]);
  const lineage = inventory.data?.lineage ?? null;

  /* The selection cascade. Each level waits for its own list to finish
     loading before it auto-selects, mirroring legacy's
     `selectDbtProject(preferred)`: without the gate, switching delivery
     project selects an id out of the previous project's list. */
  useEffect(() => {
    if (dbtProjectsResource.loading) return;
    if (dbtProjectId && dbtProjects.some((p) => p.id === dbtProjectId)) return;
    if (dbtProjects.length > 0) setParams({ dbtProject: dbtProjects[0]!.id, import: null, resource: null });
    else if (dbtProjectId) setParams({ dbtProject: null, import: null, resource: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dbtProjects, dbtProjectsResource.loading]);

  useEffect(() => {
    if (importsResource.loading) return;
    if (importId && imports.some((i) => i.id === importId)) return;
    if (imports.length > 0) setParams({ import: imports[0]!.id, resource: null });
    else if (importId) setParams({ import: null, resource: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imports, importsResource.loading]);

  const selectedArtifact = useMemo(() => imports.find((i) => i.id === importId) ?? null, [imports, importId]);
  const selectedResource = useMemo(() => resources.find((r) => r.id === resourceId) ?? null, [resources, resourceId]);

  const nodeLabel = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of lineage?.nodes ?? []) map.set(node.id, node.label);
    return map;
  }, [lineage]);

  const [showRegisterForm, setShowRegisterForm] = useState(false);
  const [showImportForm, setShowImportForm] = useState(false);
  const resourceCount = inventory.data?.total ?? resources.length;

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
              onChange={(e) =>
                setParams({ project: e.target.value || null, dbtProject: null, import: null, resource: null })
              }
            >
              <option value="">Select a project&hellip;</option>
              {orgProjects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          <Button
            onClick={() => {
              dbtProjectsResource.reload();
              importsResource.reload();
              inventory.reload();
            }}
          >
            Refresh
          </Button>
        </div>
      </header>

      {!orgProjectId ? (
        <Empty
          title="Pick a delivery project"
          hint="dbt projects are registered per delivery project, same as Semantics and Context products."
        />
      ) : dbtDisabledDetail ? (
        <DbtDisabledState detail={dbtDisabledDetail} />
      ) : dbtProjectsResource.error ? (
        <ErrorState
          title="Transformation metadata could not be loaded"
          detail={dbtProjectsResource.error}
          onRetry={dbtProjectsResource.reload}
        />
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
                    // own create flow already established for this codebase.
                    setShowRegisterForm(false);
                    setParams({ dbtProject: created.id, import: null, resource: null });
                    dbtProjectsResource.reload();
                  }}
                />
              ) : null}
              {dbtProjectsResource.loading ? (
                <div className="txskeleton" role="status">
                  Loading dbt projects&hellip;
                </div>
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
                      importsResource.reload();
                    }}
                  />
                ) : null}
                {importsResource.loading ? (
                  <div className="txskeleton" role="status">
                    Loading artifact imports&hellip;
                  </div>
                ) : importsResource.error ? (
                  <ErrorState
                    title="Artifact imports could not be loaded"
                    detail={importsResource.error}
                    onRetry={importsResource.reload}
                  />
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
                  <div className="txmetric">
                    <p>Models</p>
                    <strong>{selectedArtifact.model_count}</strong>
                    <small>Compiled transformation nodes</small>
                  </div>
                  <div className="txmetric">
                    <p>Sources</p>
                    <strong>{selectedArtifact.source_count}</strong>
                    <small>Declared upstream relations</small>
                  </div>
                  <div className="txmetric">
                    <p>Catalog matches</p>
                    <strong>{selectedArtifact.matched_resource_count}</strong>
                    <small>{selectedArtifact.unmatched_resource_count} relation mappings need attention</small>
                  </div>
                  <div className="txmetric">
                    <p>Lineage edges</p>
                    <strong>{selectedArtifact.lineage_edge_count}</strong>
                    <small>{selectedArtifact.test_count} test nodes included</small>
                  </div>
                </section>

                <section className="txpanel">
                  <div className="txfilterbar">
                    <Field label="Resource type">
                      <select
                        value={typeFilter}
                        onChange={(e) => setParams({ type: e.target.value === "ALL" ? null : e.target.value })}
                      >
                        {RESOURCE_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {t === "ALL" ? "All resources" : t}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label="Catalog link">
                      <select
                        value={matchFilter}
                        onChange={(e) => setParams({ match: e.target.value === "ALL" ? null : e.target.value })}
                      >
                        {MATCH_FILTERS.map((m) => (
                          <option key={m} value={m}>
                            {m === "ALL" ? "All resources" : m === "MATCHED" ? "Matched relations" : "Not catalog-linked"}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <span className="txfilterbar__count">
                      {resourceCount} resource{resourceCount === 1 ? "" : "s"}
                    </span>
                  </div>

                  {inventory.loading ? (
                    <div className="txskeleton" role="status">
                      Loading resource inventory&hellip;
                    </div>
                  ) : inventory.error ? (
                    <ErrorState
                      title="Resource inventory could not be loaded"
                      detail={inventory.error}
                      onRetry={inventory.reload}
                    />
                  ) : resources.length === 0 ? (
                    <Empty title="No resources match these filters" hint="Try clearing the resource type or catalog-link filter." />
                  ) : (
                    <div className="txreslist">
                      {resources.map((r) => (
                        <ResourceRow
                          key={r.id}
                          resource={r}
                          selected={r.id === resourceId}
                          onSelect={() => setParams({ resource: r.id })}
                        />
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
                      {lineage.edges.slice(0, MAX_EDGES_SHOWN).map((edge) => (
                        <div className="txedge" key={edge.id}>
                          <div className="txedge__node">
                            <strong>{nodeLabel.get(edge.source_resource_id) ?? edge.source_resource_id}</strong>
                          </div>
                          <span className="txedge__arrow" aria-hidden="true">
                            &rarr;
                          </span>
                          <div className="txedge__node">
                            <strong>{nodeLabel.get(edge.target_resource_id) ?? edge.target_resource_id}</strong>
                          </div>
                          <Pill tone="mute">{edge.edge_type === "COLUMN_DEPENDS_ON" ? "column" : "depends on"}</Pill>
                        </div>
                      ))}
                      {lineage.edges.length > MAX_EDGES_SHOWN ? (
                        <p className="txform__privacy">
                          Showing first {MAX_EDGES_SHOWN} of {lineage.edges.length} edges.
                        </p>
                      ) : null}
                    </div>
                  )}
                </section>
              </>
            ) : null}
          </div>

          {selectedResource ? (
            <ResourceDetailPane
              resource={selectedResource}
              context={{ project: orgProjectId, dbtProject: dbtProjectId }}
              onClose={() => setParams({ resource: null })}
            />
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
