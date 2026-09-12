import { useCallback, useMemo, useState } from "react";
import type { DataSourceRead, GovernedToolVersionRead, ProjectRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import {
  createToolVersion,
  listOrgDatasources,
  fetchOrgProjects,
  fetchTools,
  requestToolDeprecation,
  submitToolForReview,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { useOrgId } from "../lib/org";
import { VirtualList } from "../components/VirtualList";
import { CrossLinks } from "../components/CrossLinks";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import {
  LoadingPanel,
  StatusStrip,
  useAsyncResource,
  useStatusChannel,
  useVersionLifecycle,
} from "../components/screenState";
import {
  CreateToolPanel,
  INITIAL_DRAFT,
  blankParameter,
  draftFromVersion,
  toToolVersionCreate,
} from "./ToolContract";
import type { ParameterDraft, ToolDraft } from "./ToolContract";
import { ExecutionPanel } from "./ToolExecution";
import "./ToolRegistryScreen.css";

/* ---------------------------------------------------------------------------
   Tool registry -- the legacy portal's `tools` view (`ui/index.html#tools-view`,
   nav `data-view="tools"` / sidebar "Tool registry"), ported onto the real,
   already-merged `tool_api.py` routes that view calls:

     - GET  /v1/projects/{project_id}/tools                  list_tools, tool_api.py:609
     - POST /v1/projects/{project_id}/tools                   create_tool_version, tool_api.py:348
     - POST /v1/tool-versions/{version_id}/submit              submit_tool_for_review, tool_api.py:692
     - POST /v1/tool-versions/{version_id}/deprecation-submit  submit_tool_deprecation, tool_api.py:756
     - POST /v1/tool-versions/{version_id}/execute              execute_tool, tool_api.py:881

   Three pieces, matching the legacy screen's own `tools-layout` exactly. This
   file owns the first two -- the inventory and one version's contract as
   published -- because they are the same object at two zoom levels and must
   agree about its status badge. Authoring a version is `ToolContract.tsx`;
   running one is `ToolExecution.tsx`.

     1. registry     the `Version inventory` list (`#tools-table`), filterable
                      by status, one row per `GovernedToolVersion` -- project-
                      scoped, same `fetchOrgProjects` picker `SemanticsScreen`/
                      `ContextProductsScreen` use (`list_tools` takes a
                      `project_id`; there is no org-wide tool browse).
     2. detail        the selected version's contract (`#tool-detail`): SQL
                      template, data source, allowed roles, referenced tables,
                      fingerprint, plus lifecycle actions matching legacy's
                      `selectTool()` (`data-submit-tool` / `data-deprecate-tool`
                      / `data-new-version`).

   Left out of scope, same as the legacy screen: the multi-table blueprint
   helper (`create_multi_table_tool_blueprint`, `create_view_tool_blueprint`)
   and the certification-cases/certification-runs sub-flow
   (`tool_api.py` lines 400-524 and 1056-1400+) -- legacy's `tools-view` never
   calls any of those; they belong to a different, not-yet-ported surface.
--------------------------------------------------------------------------- */

const statusTone = (s: string): Tone =>
  s === "PUBLISHED"
    ? "ok"
    : s === "DRAFT"
      ? "mute"
      : s === "REVIEW_REQUIRED"
        ? "info"
        : s === "DEPRECATED"
          ? "bad"
          : "warn";

function ToolRow({
  tool,
  selected,
  onSelect,
}: {
  tool: GovernedToolVersionRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`trrow${selected ? " trrow--sel" : ""}`} aria-label={tool.name}>
      <button className="trrow__click" onClick={onSelect}>
        <div className="trrow__badges">
          <Pill tone={statusTone(tool.status)}>{tool.status.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
        <h3 className="trrow__title">{tool.name}</h3>
        <div className="trrow__key">
          {tool.slug} · version {tool.version}
        </div>
      </button>
    </article>
  );
}

function ToolDetail({
  tool,
  datasourceName,
  busy,
  onSubmitReview,
  onRequestDeprecation,
  onNewVersion,
}: {
  tool: GovernedToolVersionRead | null;
  datasourceName: string;
  busy: boolean;
  onSubmitReview: () => void;
  onRequestDeprecation: () => void;
  onNewVersion: () => void;
}) {
  if (!tool) {
    return (
      <article className="trdetail">
        <Empty title="Select a tool version" hint="Review its contract, SQL boundary, roles, and lifecycle actions." />
      </article>
    );
  }

  return (
    <article className="trdetail">
      <header className="trdetail__head">
        <div>
          <p className="trdetail__eyebrow">
            {tool.slug} / VERSION {tool.version}
          </p>
          <h2 className="trdetail__h2">{tool.name}</h2>
        </div>
        <Pill tone={statusTone(tool.status)}>{tool.status.toLowerCase().replace(/_/g, " ")}</Pill>
      </header>
      <p className="trdetail__desc">{tool.description}</p>
      <div className="trdetail__grid">
        <div>
          <span>Data source</span>
          <strong>{datasourceName || tool.datasource_id}</strong>
        </div>
        <div>
          <span>Allowed roles</span>
          <strong>{tool.allowed_roles.join(", ")}</strong>
        </div>
        <div>
          <span>Parameters</span>
          <strong>{tool.parameters.length}</strong>
        </div>
        <div>
          <span>Referenced tables</span>
          <strong>{tool.referenced_tables.join(", ") || "Validated at execution"}</strong>
        </div>
        <div>
          <span>Maker</span>
          <strong>{tool.created_by}</strong>
        </div>
        <div>
          <span>Fingerprint</span>
          <strong>{tool.fingerprint.slice(0, 16)}</strong>
        </div>
      </div>
      <pre className="trdetail__sql">{tool.sql_template}</pre>
      {/* A published tool is simultaneously a step in a plan and a callable an
          external agent sees as `atlas__<slug>`. Neither was reachable from
          here, so the registry read as a dead end. */}
      {tool.status === "PUBLISHED" ? (
        <CrossLinks
          label="Use this tool in"
          links={[
            { screen: "tool-plans", label: "Tool plans", title: "Compose this tool into a multi-step, budgeted plan" },
            {
              screen: "developer",
              label: "Agent gateway",
              params: { project: tool.project_id },
              title: `External agents see this as atlas__${tool.slug}`,
            },
            { screen: "lineage", label: "Lineage", params: { ds: tool.datasource_id }, title: "What this tool reads from" },
          ]}
        />
      ) : null}
      <div className="trdetail__actions">
        {tool.status === "DRAFT" ? (
          <Button disabled={busy} onClick={onSubmitReview}>
            {busy ? "Submitting…" : "Submit for review"}
          </Button>
        ) : null}
        {tool.status === "PUBLISHED" ? (
          <Button disabled={busy} onClick={onRequestDeprecation}>
            {busy ? "Requesting…" : "Request deprecation"}
          </Button>
        ) : null}
        <Button onClick={onNewVersion}>New version</Button>
      </div>
    </article>
  );
}

export function ToolRegistryScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const projectId = params.get("project");
  const statusFilter = params.get("status") ?? "ALL";
  const toolId = params.get("tool");

  const channel = useStatusChannel();

  /* The org-wide pickers. The datasource list degrades to empty on failure --
     it only feeds a `<select>` in the authoring panel -- while the project
     list does not, because without it nothing on this screen is reachable. */
  const projects = useAsyncResource<ProjectRead[]>(
    async (signal) => (await fetchOrgProjects(ORG, signal)).items,
    [ORG],
  );
  const datasources = useAsyncResource<DataSourceRead[]>(
    async (signal) => {
      try {
        return (await listOrgDatasources(ORG, signal)).items;
      } catch (reason) {
        // An abort still has to propagate, or a superseded request would be
        // written to state as "this org has no sources".
        if (signal.aborted) throw reason;
        return [];
      }
    },
    [ORG],
  );

  // `enabled` is what makes the assertion below safe: with no project there is
  // no org-wide tool browse to fall back to, so the request is not made.
  const registry = useAsyncResource<PageOf<GovernedToolVersionRead>>(
    (signal) =>
      fetchTools(projectId!, { status: statusFilter !== "ALL" ? statusFilter : null, limit: 200 }, signal),
    [projectId, statusFilter],
    { enabled: Boolean(projectId) },
  );

  const tools = useMemo(() => registry.data?.items ?? [], [registry.data]);
  const allDatasources = useMemo(() => datasources.data ?? [], [datasources.data]);

  const selectedTool = useMemo(() => tools.find((t) => t.id === toolId) ?? null, [tools, toolId]);
  const projectDatasources = useMemo(
    () => (projectId ? allDatasources.filter((d) => d.project_id === projectId) : []),
    [allDatasources, projectId],
  );
  const datasourceName = useMemo(
    () => (selectedTool ? (allDatasources.find((d) => d.id === selectedTool.datasource_id)?.name ?? "") : ""),
    [allDatasources, selectedTool],
  );

  const reloadRegistry = registry.reload;
  const lifecycle = useVersionLifecycle(channel, reloadRegistry);

  const [draft, setDraft] = useState<ToolDraft>(INITIAL_DRAFT);
  const [parameters, setParameters] = useState<ParameterDraft[]>([blankParameter()]);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const resetDraft = useCallback(() => {
    setDraft(INITIAL_DRAFT);
    setParameters([blankParameter()]);
    setEditingSlug(null);
  }, []);

  const submitCreate = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!projectId) {
        channel.failure("Select a project before creating a tool version.");
        return;
      }
      let body;
      try {
        body = toToolVersionCreate(draft, parameters);
      } catch (reason) {
        // A draft that cannot be serialized is never posted: the author is
        // told which parameter is wrong instead of reading the server's
        // rejection of a body they did not intend to send.
        channel.failure(reason);
        return;
      }
      setCreating(true);
      channel.info("Validating SQL contract…");
      try {
        await createToolVersion(projectId, body);
        resetDraft();
        channel.success("Governed tool draft created and SQL contract validated.");
        reloadRegistry();
      } catch (reason) {
        channel.failure(reason);
      } finally {
        setCreating(false);
      }
    },
    [projectId, draft, parameters, channel, reloadRegistry, resetDraft],
  );

  return (
    <div className="trscreen">
      <header className="trscreen__head">
        <div>
          <p className="trscreen__eyebrow">SAFE REUSE</p>
          <h1 className="trscreen__h1">Governed tool registry</h1>
          <p className="trscreen__lede">
            Author parameter-bound analytical tools, route them through review, and execute published versions.
          </p>
        </div>
        <div className="trscreen__filters">
          <Field label="Project">
            <select
              value={projectId ?? ""}
              onChange={(e) => setParams({ project: e.target.value || null, tool: null })}
            >
              <option value="">Select a project…</option>
              {(projects.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          <Button onClick={registry.reload}>Refresh</Button>
        </div>
      </header>

      {projects.error ? (
        <p className="trscreen__pickerr" role="alert">
          {projects.error}
        </p>
      ) : null}
      <StatusStrip status={channel.status} />

      <div className="trscreen__body">
        <div className="trscreen__main">
          <article className="trregistry">
            <header className="trregistry__head">
              <div>
                <p className="trregistry__eyebrow">CATALOG</p>
                <h2 className="trregistry__h2">Version inventory</h2>
              </div>
              <Field label="Status">
                <select
                  value={statusFilter}
                  onChange={(e) => setParams({ status: e.target.value === "ALL" ? null : e.target.value })}
                >
                  <option value="ALL">All</option>
                  <option value="DRAFT">Draft</option>
                  <option value="REVIEW_REQUIRED">Review required</option>
                  <option value="PUBLISHED">Published</option>
                  <option value="DEPRECATED">Deprecated</option>
                </select>
              </Field>
            </header>
            {!projectId ? (
              <Empty
                title="Pick a project to see its tool registry"
                hint="Governed tools are project-scoped, same as Semantics and Context products."
              />
            ) : registry.error ? (
              <ErrorState title="Tool versions could not be loaded" detail={registry.error} onRetry={registry.reload} />
            ) : registry.loading ? (
              <LoadingPanel label="Loading tool versions…" />
            ) : (
              <VirtualList
                items={tools}
                getKey={(t) => t.id}
                ariaLabel="Tool versions"
                estimateSize={92}
                totalCount={registry.data?.total ?? null}
                emptyState={<Empty title="No tool versions match" hint="Create a parameter-bound SQL tool to begin." />}
                renderItem={(t) => <ToolRow tool={t} selected={t.id === toolId} onSelect={() => setParams({ tool: t.id })} />}
              />
            )}
          </article>

          <ToolDetail
            tool={selectedTool}
            datasourceName={datasourceName}
            busy={lifecycle.busyVersionId === selectedTool?.id}
            onSubmitReview={() =>
              selectedTool &&
              void lifecycle.run(selectedTool.id, {
                pending: "Submitting for review",
                done: "Tool version submitted for independent review.",
                action: submitToolForReview,
              })
            }
            onRequestDeprecation={() =>
              selectedTool &&
              void lifecycle.run(selectedTool.id, {
                pending: "Requesting deprecation",
                done: "Tool deprecation submitted for independent review.",
                action: requestToolDeprecation,
              })
            }
            onNewVersion={() => {
              if (!selectedTool) return;
              const prefilled = draftFromVersion(selectedTool);
              setDraft(prefilled.draft);
              setParameters(prefilled.parameters);
              setEditingSlug(selectedTool.slug);
            }}
          />

          <ExecutionPanel tool={selectedTool} />
        </div>

        <aside className="trscreen__rail">
          <CreateToolPanel
            projectId={projectId}
            datasourceOptions={projectDatasources}
            draft={draft}
            setDraft={setDraft}
            parameters={parameters}
            setParameters={setParameters}
            creating={creating}
            onSubmit={(e) => void submitCreate(e)}
            editingSlug={editingSlug}
            onCancelEdit={resetDraft}
          />
        </aside>
      </div>
    </div>
  );
}
