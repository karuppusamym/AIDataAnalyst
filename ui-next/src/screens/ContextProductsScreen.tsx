import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ContextCompilationRead,
  ContextProductConsumerBindingRead,
  ContextProductCreate,
  ContextProductRead,
  ContextProductVersionRead,
  ProjectRead,
} from "../lib/types";
import {
  ApiError,
  compileContextProductVersion,
  createContextProduct,
  downloadCompiledContextProduct,
  fetchCatalogRows,
  fetchContextProductBindings,
  fetchContextProducts,
  fetchContextProductVersions,
  fetchOrgProjects,
  fetchSemanticModelVersions,
  fetchTools,
  removeContextProductBinding,
  requestContextProductDeprecation,
  setContextProductBinding,
  submitContextProductVersion,
} from "../lib/api";
import { listGlossaryTerms } from "../lib/_api_append";
import { useUrlState } from "../lib/useUrlState";
import { navigateTo } from "../lib/navigate";
import { useOrgId } from "../lib/org";
import { VirtualList } from "../components/VirtualList";
import { ReferencePicker, usePickerOptions } from "../components/ReferencePicker";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./ContextProductsScreen.css";

/* ---------------------------------------------------------------------------
   Context products — the legacy portal's `context-products` view
   (`ui/index.html#context-products-view`,
   `ui/scripts/features/context-lineage-control-plane.js`), ported onto the
   real, already-merged `context_product_api.py` / `context_compiler_api.py`
   routes that view calls (see `lib/api.ts`'s "Context products" block for
   the exact endpoint list and what was deliberately left out).

   Three pieces, same shape as the legacy screen:
     1. registry     one row per product at its latest version (project-
                      scoped, same `fetchOrgProjects` picker `SemanticsScreen`
                      uses — `list_context_products` takes a `project_id`,
                      there is no org-wide browse), with Submit / Request
                      deprecation / Compile actions matching the version's
                      real lifecycle status.
     2. compiler      target picker + the last compiled artifact, driven by
                      the real `GET .../compile` (deterministic: the same
                      version and target always produce the same hash).
     3. create draft  every field `ContextProductCreate` accepts, matching
                      the legacy form's own field set and defaults
                      (`policy_summary` fixed to gateway-only/no-raw-context,
                      exactly as the legacy screen hard-codes it).

   One message strip, not per-panel state, mirrors the legacy screen's own
   single `#context-product-message` target — every action (load, create,
   submit, deprecate, compile) reports through the same place a steward is
   already looking.
--------------------------------------------------------------------------- */

const versionStatusTone = (s: string): Tone =>
  s === "PUBLISHED" || s === "SUPPORTED"
    ? "ok"
    : s === "REVIEW_REQUIRED"
      ? "info"
      : s === "DEPRECATED" || s === "RETIRED"
        ? "bad"
        : "warn";

const COMPILE_TARGETS = ["MCP", "REST", "YAML", "OSI", "ODCS", "SNOWFLAKE_SEMANTIC_VIEW", "DATABRICKS_METRIC_VIEW"] as const;

type Kind = "info" | "success" | "error";

interface FormState {
  productKey: string;
  name: string;
  ownerType: "GROUP" | "INDIVIDUAL";
  ownerPrincipal: string;
  description: string;
  purpose: string;
  /* Governed references are held as ordered id arrays, which is exactly the
     shape `ContextProductCreate` wants -- there is no parse step at submit
     time and therefore no way for a typo to become a 422. */
  tableIds: string[];
  semanticIds: string[];
  glossaryIds: string[];
  toolIds: string[];
  consumerRoles: string;
  lineageDepth: string;
  minimumScore: string;
  denyOnCriticalIncident: boolean;
}

const INITIAL_FORM: FormState = {
  productKey: "",
  name: "",
  ownerType: "GROUP",
  ownerPrincipal: "",
  description: "",
  purpose: "",
  tableIds: [],
  semanticIds: [],
  glossaryIds: [],
  toolIds: [],
  consumerRoles: "Analyst",
  lineageDepth: "2",
  minimumScore: "85",
  denyOnCriticalIncident: true,
};

const splitIds = (value: string): string[] =>
  value.split(",").map((v) => v.trim()).filter(Boolean);

function ProductRow({
  product,
  busy,
  selected,
  onSubmit,
  onDeprecate,
  onCompile,
  onRollout,
}: {
  product: ContextProductRead;
  busy: string | null;
  selected: boolean;
  onSubmit: () => void;
  onDeprecate: () => void;
  onCompile: () => void;
  onRollout: () => void;
}) {
  const v = product.latest_version;
  const isBusy = busy === v.id;
  return (
    <article className={`cprow${selected ? " cprow--selected" : ""}`} aria-label={v.name}>
      <div className="cprow__main">
        <div className="cprow__badges">
          <Pill tone={versionStatusTone(v.status)}>{v.status.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
        <h3 className="cprow__title">{v.name}</h3>
        <div className="cprow__key">{product.product_key} · v{v.version}</div>
        <div className="cprow__grid">
          <div><span className="cprow__label">Owner</span><span>{v.owner_principal}</span></div>
          <div><span className="cprow__label">Consumers</span><span>{v.allowed_consumer_roles.join(", ") || "—"}</span></div>
          <div><span className="cprow__label">Fingerprint</span><code>{v.fingerprint.slice(0, 12)}</code></div>
        </div>
      </div>
      <div className="cprow__actions">
        <Button onClick={onRollout} title="Pin named consumers to a specific version">
          {selected ? "Rollout ✓" : "Rollout"}
        </Button>
        {v.status === "DRAFT" ? (
          <Button disabled={isBusy} onClick={onSubmit}>{isBusy ? "Submitting…" : "Submit"}</Button>
        ) : null}
        {v.status === "PUBLISHED" || v.status === "SUPPORTED" ? (
          <Button disabled={isBusy} onClick={onDeprecate}>{isBusy ? "Requesting…" : "Deprecate"}</Button>
        ) : null}
        <Button variant="primary" disabled={isBusy} onClick={onCompile}>{isBusy ? "Compiling…" : "Compile"}</Button>
      </div>
    </article>
  );
}

function CompilerPanel({
  target,
  onTargetChange,
  result,
  compiling,
  onDownload,
  downloading,
}: {
  target: string;
  onTargetChange: (t: string) => void;
  result: ContextCompilationRead | null;
  compiling: boolean;
  onDownload: () => void;
  downloading: boolean;
}) {
  return (
    <article className="cpcompiler">
      <header className="cpcompiler__head">
        <div>
          <p className="cpcompiler__eyebrow">DETERMINISTIC DELIVERY</p>
          <h2 className="cpcompiler__h2">Context compiler</h2>
          <p className="cpcompiler__lede">Compile one immutable version for MCP, REST, YAML, OSI, ODCS, Snowflake, or Databricks.</p>
        </div>
        <Field label="Target">
          <select value={target} onChange={(e) => onTargetChange(e.target.value)}>
            {COMPILE_TARGETS.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </Field>
      </header>
      <div className="cpcompiler__output">
        {compiling ? (
          <div className="cpcompiler__hint" role="status">Compiling…</div>
        ) : result ? (
          <>
            <div className="cpcompiler__meta">
              <Pill tone="info">{result.target}</Pill>
              <code>artifact {result.artifact_hash.slice(0, 16)}</code>
              <code>source {result.source_fingerprint.slice(0, 16)}</code>
              {/* The artifact is what an agent developer actually installs.
                  Reading it on screen is not the same as having the file. */}
              <Button onClick={onDownload} disabled={downloading}>
                {downloading ? "Preparing…" : "Download artifact"}
              </Button>
            </div>
            <pre className="cpcompiler__pre">{result.content}</pre>
          </>
        ) : (
          <Empty title="Select Compile on a product version" hint="The generated artifact and stable hash will appear here." />
        )}
      </div>
    </article>
  );
}

/* ---------------------------------------------------------------------------
   Rollout — the AT-7(b) consumer-binding registry.

   Publishing a version makes it available to every principal holding an
   allowed consumer role. A binding is the staged-rollout control on top of
   that: it pins one *named* consumer (usually an agent's service principal)
   to one specific version, so a new version can be proven against one agent
   before every agent moves. Unpinning returns that consumer to the published
   version; it never grants or revokes access, which stays governed by
   `allowed_consumer_roles`.

   Both endpoints shipped with the API and had no client at all, which is why
   a Context Product could be compiled but not actually operated.
--------------------------------------------------------------------------- */

function RolloutPanel({
  product,
  versions,
  bindings,
  loading,
  error,
  busyConsumer,
  onBind,
  onUnbind,
  onReload,
  onClose,
}: {
  product: ContextProductRead;
  versions: ContextProductVersionRead[];
  bindings: ContextProductConsumerBindingRead[];
  loading: boolean;
  error: string | null;
  busyConsumer: string | null;
  onBind: (consumerPrincipalId: string, versionId: string) => void;
  onUnbind: (consumerPrincipalId: string) => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const [consumer, setConsumer] = useState("");
  const [versionId, setVersionId] = useState("");

  // Default the version picker to the published version -- the one a new
  // consumer would resolve to anyway -- so the common case is one field.
  useEffect(() => {
    if (versionId && versions.some((v) => v.id === versionId)) return;
    const published = versions.find((v) => v.status === "PUBLISHED") ?? versions[0];
    setVersionId(published?.id ?? "");
  }, [versions, versionId]);

  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = consumer.trim();
    if (!trimmed || !versionId) return;
    onBind(trimmed, versionId);
    setConsumer("");
  };

  return (
    <article className="cprollout" aria-label={`Rollout for ${product.product_key}`}>
      <header className="cprollout__head">
        <div>
          <p className="cprollout__eyebrow">STAGED ROLLOUT</p>
          <h2 className="cprollout__h2">{product.product_key}</h2>
          <p className="cprollout__lede">
            Pin a named consumer to one version. Anyone not pinned here resolves to the
            published version.
          </p>
        </div>
        <div className="cprollout__headactions">
          <Button onClick={onReload}>Refresh</Button>
          <Button onClick={onClose}>Close</Button>
        </div>
      </header>

      {error ? (
        <ErrorState title="Rollout could not be loaded" detail={error} onRetry={onReload} />
      ) : loading ? (
        <div className="cpscreen__skeleton" role="status" aria-live="polite">Loading rollout…</div>
      ) : (
        <>
          <form className="cprollout__form" onSubmit={submit}>
            <Field label="Consumer principal">
              <input
                required
                placeholder="risk-copilot@agents.tenant.example"
                value={consumer}
                onChange={(e) => setConsumer(e.target.value)}
              />
            </Field>
            <Field label="Bound version">
              <select value={versionId} onChange={(e) => setVersionId(e.target.value)}>
                {versions.map((v) => (
                  <option key={v.id} value={v.id}>
                    v{v.version} · {v.status.toLowerCase().replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </Field>
            <Button type="submit" variant="primary" disabled={busyConsumer !== null || versions.length === 0}>
              {busyConsumer !== null ? "Saving…" : "Pin consumer"}
            </Button>
          </form>

          {bindings.length === 0 ? (
            <Empty
              title="No pinned consumers"
              hint="Every eligible consumer resolves to the published version. Pin one to stage a new version against it first."
            />
          ) : (
            <table className="cprollout__table">
              <caption className="cprollout__caption">
                {bindings.length} pinned consumer{bindings.length === 1 ? "" : "s"}
              </caption>
              <thead>
                <tr><th scope="col">Consumer</th><th scope="col">Version</th><th scope="col">Pinned</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {bindings.map((b) => (
                  <tr key={b.id}>
                    <td><code>{b.consumer_principal_id}</code></td>
                    <td><Pill tone="info">v{b.bound_version_number}</Pill></td>
                    <td>{new Date(b.updated_at).toLocaleDateString()}</td>
                    <td className="cprollout__rowaction">
                      <Button
                        disabled={busyConsumer !== null}
                        onClick={() => onUnbind(b.consumer_principal_id)}
                      >
                        {busyConsumer === b.consumer_principal_id ? "Removing…" : "Unpin"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </article>
  );
}

export function ContextProductsScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const projectId = params.get("project");

  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    fetchOrgProjects(ORG, ac.signal)
      .then((page) => setProjects(page.items))
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setProjectsError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [items, setItems] = useState<ContextProductRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<{ text: string; kind: Kind } | null>(null);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    if (!projectId) {
      setItems([]);
      setTotal(null);
      setLoading(false);
      setError(null);
      return;
    }
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchContextProducts(projectId, { limit: 200 }, ac.signal);
      if (seq !== reqSeq.current) return;
      setItems(page.items);
      setTotal(page.total);
      setStatus({ text: `${page.total} governed product${page.total === 1 ? "" : "s"} in this project.`, kind: "success" });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      const detail = e instanceof ApiError ? e.detail : (e as Error).message;
      setError(detail);
      setStatus({ text: detail, kind: "error" });
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const [busyVersionId, setBusyVersionId] = useState<string | null>(null);

  const runVersionAction = useCallback(
    async (versionId: string, kind: "submit" | "deprecate", verb: string, action: (id: string) => Promise<unknown>) => {
      setBusyVersionId(versionId);
      setStatus({ text: `${verb}…`, kind: "info" });
      try {
        await action(versionId);
        setStatus({
          text: kind === "submit" ? "Publication review requested." : "Deprecation review requested.",
          kind: "success",
        });
        await load();
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setBusyVersionId(null);
      }
    },
    [load],
  );

  const [compileTarget, setCompileTarget] = useState<string>("MCP");
  const [compileResult, setCompileResult] = useState<ContextCompilationRead | null>(null);
  const [compiling, setCompiling] = useState(false);

  const compile = useCallback(
    async (versionId: string) => {
      setBusyVersionId(versionId);
      setCompiling(true);
      setStatus({ text: `Compiling deterministic ${compileTarget} artifact...`, kind: "info" });
      try {
        const artifact = await compileContextProductVersion(versionId, compileTarget);
        setCompileResult(artifact);
        setStatus({ text: "Artifact compiled. Repeating this request against the same version produces the same hash.", kind: "success" });
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setBusyVersionId(null);
        setCompiling(false);
      }
    },
    [compileTarget],
  );

  const [downloading, setDownloading] = useState(false);
  const download = useCallback(async () => {
    if (!compileResult) return;
    const versionId = compileResult.generated_from?.context_product_version_id;
    if (typeof versionId !== "string") {
      setStatus({ text: "This artifact does not name the version it came from; recompile it.", kind: "error" });
      return;
    }
    setDownloading(true);
    try {
      await downloadCompiledContextProduct(versionId, compileResult.target);
      setStatus({ text: "Artifact saved. Ship it alongside your agent's client configuration.", kind: "success" });
    } catch (e) {
      setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
    } finally {
      setDownloading(false);
    }
  }, [compileResult]);

  /* ---- Rollout (AT-7(b) consumer bindings) ---------------------------- */
  const [rolloutProduct, setRolloutProduct] = useState<ContextProductRead | null>(null);
  const [versions, setVersions] = useState<ContextProductVersionRead[]>([]);
  const [bindings, setBindings] = useState<ContextProductConsumerBindingRead[]>([]);
  const [rolloutLoading, setRolloutLoading] = useState(false);
  const [rolloutError, setRolloutError] = useState<string | null>(null);
  const [busyConsumer, setBusyConsumer] = useState<string | null>(null);

  const loadRollout = useCallback(
    async (product: ContextProductRead, signal?: AbortSignal) => {
      setRolloutLoading(true);
      setRolloutError(null);
      try {
        // Both reads are independent; a failure in either should report as
        // one failure of "the rollout", not leave half a panel on screen.
        const [versionPage, bindingPage] = await Promise.all([
          fetchContextProductVersions(product.id, { limit: 200 }, signal),
          fetchContextProductBindings(product.id, { limit: 200 }, signal),
        ]);
        if (signal?.aborted) return;
        setVersions(versionPage.items);
        setBindings(bindingPage.items);
      } catch (e) {
        if (signal?.aborted || (e as Error)?.name === "AbortError") return;
        setRolloutError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        if (!signal?.aborted) setRolloutLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!rolloutProduct) return;
    const ac = new AbortController();
    void loadRollout(rolloutProduct, ac.signal);
    return () => ac.abort();
  }, [rolloutProduct, loadRollout]);

  const bindConsumer = useCallback(
    async (consumerPrincipalId: string, versionId: string) => {
      if (!rolloutProduct) return;
      setBusyConsumer(consumerPrincipalId);
      try {
        await setContextProductBinding(rolloutProduct.id, consumerPrincipalId, versionId);
        setStatus({ text: `${consumerPrincipalId} is pinned. Every other consumer stays on the published version.`, kind: "success" });
        await loadRollout(rolloutProduct);
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setBusyConsumer(null);
      }
    },
    [rolloutProduct, loadRollout],
  );

  const unbindConsumer = useCallback(
    async (consumerPrincipalId: string) => {
      if (!rolloutProduct) return;
      setBusyConsumer(consumerPrincipalId);
      try {
        await removeContextProductBinding(rolloutProduct.id, consumerPrincipalId);
        setStatus({ text: `${consumerPrincipalId} now resolves to the published version.`, kind: "success" });
        await loadRollout(rolloutProduct);
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setBusyConsumer(null);
      }
    },
    [rolloutProduct, loadRollout],
  );

  const [form, setForm] = useState<FormState>(INITIAL_FORM);
  const [creating, setCreating] = useState(false);
  const setField = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  }, []);

  /* The four governed-reference pickers. Each reads the same list the screen
     that owns those objects reads, so what a steward can put in a package is
     exactly what the platform has approved -- and the ids never pass through
     a human's clipboard. Tables are organization-scoped; the other three are
     project-scoped, so they stay empty (and say so) until a project is
     chosen, rather than showing another project's objects. */
  const tableOptions = usePickerOptions(
    (signal) => fetchCatalogRows({ organizationId: ORG, limit: 200 }, signal).then((p) => p.items),
    (row) => ({
      id: row.id,
      label: `${row.schema_name}.${row.name}`,
      hint: row.owner ? `owner ${row.owner}` : "unowned",
      badge: row.certification === "CERTIFIED" ? "certified" : undefined,
    }),
    [ORG],
  );

  const semanticOptions = usePickerOptions(
    (signal) => fetchSemanticModelVersions(projectId ?? "", { limit: 200 }, signal).then((p) => p.items),
    (v) => ({ id: v.id, label: v.name, hint: `v${v.version}`, badge: v.status.toLowerCase() }),
    [projectId],
    { enabled: Boolean(projectId) },
  );

  const glossaryOptions = usePickerOptions(
    (signal) => listGlossaryTerms(ORG, { status: "APPROVED", limit: 200 }, signal).then((p) => p.items),
    (term) => ({ id: term.id, label: term.display_name, hint: term.term_key, badge: `v${term.version}` }),
    [ORG],
  );

  const toolOptions = usePickerOptions(
    (signal) => fetchTools(projectId ?? "", { status: "PUBLISHED", limit: 200 }, signal).then((p) => p.items),
    (tool) => ({ id: tool.id, label: tool.name, hint: `${tool.slug} v${tool.version}`, badge: tool.status.toLowerCase() }),
    [projectId],
    { enabled: Boolean(projectId) },
  );

  const submitCreate = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!projectId) {
        setStatus({ text: "Select a project before creating a draft.", kind: "error" });
        return;
      }
      const body: ContextProductCreate = {
        product_key: form.productKey,
        name: form.name,
        description: form.description,
        purpose: form.purpose,
        owner_type: form.ownerType,
        owner_principal: form.ownerPrincipal,
        table_ids: form.tableIds,
        semantic_model_version_ids: form.semanticIds,
        glossary_term_version_ids: form.glossaryIds,
        eligible_tool_version_ids: form.toolIds,
        allowed_consumer_roles: splitIds(form.consumerRoles),
        lineage_depth: Number(form.lineageDepth || 2),
        quality_requirements: {
          minimum_score: Number(form.minimumScore || 0),
          deny_on_critical_incident: form.denyOnCriticalIncident,
        },
        policy_summary: {
          source_values: "GATEWAY_ONLY",
          retention: "NO_RAW_CONTEXT",
          permitted_actions: ["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"],
        },
      };
      setCreating(true);
      setStatus({ text: "Validating governed references...", kind: "info" });
      try {
        await createContextProduct(projectId, body);
        setForm(INITIAL_FORM);
        setStatus({ text: "Draft created. Submit it for independent review when ready.", kind: "success" });
        await load();
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setCreating(false);
      }
    },
    [projectId, form, load],
  );

  const projectOptions = useMemo(() => projects, [projects]);

  return (
    <div className="cpscreen">
      <header className="cpscreen__head">
        <div>
          <p className="cpscreen__eyebrow">GOVERNED AGENT CONTEXT</p>
          <h1 className="cpscreen__h1">Context products</h1>
          <p className="cpscreen__lede">
            Package exact metadata, semantics, terms, quality gates, and eligible tools into
            immutable versions.
          </p>
        </div>
        <div className="cpscreen__filters">
          <Field label="Project">
            <select
              value={projectId ?? ""}
              onChange={(e) => setParams({ project: e.target.value || null })}
            >
              <option value="">Select a project…</option>
              {projectOptions.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </Field>
          <Button onClick={() => void load()}>Refresh</Button>
          {/* The package is only half the story: the gateway is where an
              agent developer learns how to actually connect to it. */}
          <Button onClick={() => navigateTo("developer", projectId ? { project: projectId } : {})}>
            Agent gateway →
          </Button>
        </div>
      </header>

      {projectsError ? <p className="cpscreen__pickerr" role="alert">{projectsError}</p> : null}
      {status ? <div className={`cpscreen__status cpscreen__status--${status.kind}`} role="status">{status.text}</div> : null}

      <div className="cpscreen__body">
        <div className="cpscreen__main">
          <article className="cpregistry">
            <header className="cpregistry__head">
              <p className="cpregistry__eyebrow">VERSION REGISTRY</p>
              <h2 className="cpregistry__h2">Publication posture</h2>
              <p className="cpregistry__lede">Review lifecycle, consumers, and fingerprints before compiling or submitting any version.</p>
            </header>
            {!projectId ? (
              <Empty title="Pick a project to see its context products" hint="Context products are project-scoped, same as Semantics." />
            ) : error ? (
              <ErrorState title="Context products could not be loaded" detail={error} onRetry={() => void load()} />
            ) : loading ? (
              <div className="cpscreen__skeleton" role="status" aria-live="polite">Loading governed products…</div>
            ) : (
              <VirtualList
                items={items}
                getKey={(p) => p.id}
                ariaLabel="Context products"
                estimateSize={132}
                totalCount={total}
                emptyState={
                  <Empty title="No Context Products" hint="Create a bounded product from approved tables, semantics, terms, and tools." />
                }
                renderItem={(p) => (
                  <ProductRow
                    product={p}
                    busy={busyVersionId}
                    selected={rolloutProduct?.id === p.id}
                    onSubmit={() => void runVersionAction(p.latest_version.id, "submit", "Requesting publication review", submitContextProductVersion)}
                    onDeprecate={() => void runVersionAction(p.latest_version.id, "deprecate", "Requesting deprecation review", requestContextProductDeprecation)}
                    onCompile={() => void compile(p.latest_version.id)}
                    onRollout={() => setRolloutProduct((current) => (current?.id === p.id ? null : p))}
                  />
                )}
              />
            )}
          </article>

          {rolloutProduct ? (
            <RolloutPanel
              product={rolloutProduct}
              versions={versions}
              bindings={bindings}
              loading={rolloutLoading}
              error={rolloutError}
              busyConsumer={busyConsumer}
              onBind={(consumer, version) => void bindConsumer(consumer, version)}
              onUnbind={(consumer) => void unbindConsumer(consumer)}
              onReload={() => void loadRollout(rolloutProduct)}
              onClose={() => setRolloutProduct(null)}
            />
          ) : null}

          <CompilerPanel
            target={compileTarget}
            onTargetChange={setCompileTarget}
            result={compileResult}
            compiling={compiling}
            onDownload={() => void download()}
            downloading={downloading}
          />
        </div>

        <aside className="cpscreen__rail">
          <article className="cpform">
            <form onSubmit={(e) => void submitCreate(e)}>
              <header className="cpform__head">
                <div>
                  <p className="cpform__eyebrow">NEW GOVERNED PACKAGE</p>
                  <h2 className="cpform__h2">Create draft</h2>
                  <p className="cpform__lede">Assemble only approved tables, semantics, glossary terms, and eligible tools into one bounded package.</p>
                </div>
                <Pill tone="warn">DRAFT</Pill>
              </header>

              <div className="cpform__grid">
                <Field label="Stable key">
                  <input
                    required
                    pattern="[a-z][a-z0-9_-]{1,99}"
                    placeholder="consumer-risk-context"
                    value={form.productKey}
                    onChange={(e) => setField("productKey", e.target.value)}
                  />
                </Field>
                <Field label="Name">
                  <input
                    required
                    minLength={3}
                    placeholder="Consumer risk analysis"
                    value={form.name}
                    onChange={(e) => setField("name", e.target.value)}
                  />
                </Field>
                <Field label="Owner type">
                  <select value={form.ownerType} onChange={(e) => setField("ownerType", e.target.value as FormState["ownerType"])}>
                    <option value="GROUP">Group</option>
                    <option value="INDIVIDUAL">Individual</option>
                  </select>
                </Field>
                <Field label="Owner principal">
                  <input
                    required
                    minLength={2}
                    placeholder="risk-data-stewards"
                    value={form.ownerPrincipal}
                    onChange={(e) => setField("ownerPrincipal", e.target.value)}
                  />
                </Field>
                <div className="cpform__span2">
                  <Field label="Description">
                    <textarea
                      required
                      minLength={3}
                      rows={2}
                      placeholder="What this package contains"
                      value={form.description}
                      onChange={(e) => setField("description", e.target.value)}
                    />
                  </Field>
                </div>
                <div className="cpform__span2">
                  <Field label="Approved purpose">
                    <textarea
                      required
                      minLength={10}
                      rows={2}
                      placeholder="Bounded purpose for agent and analyst consumption"
                      value={form.purpose}
                      onChange={(e) => setField("purpose", e.target.value)}
                    />
                  </Field>
                </div>
                <div className="cpform__span2">
                  <ReferencePicker
                    label="Governed tables"
                    options={tableOptions.options}
                    loading={tableOptions.loading}
                    error={tableOptions.error}
                    selected={form.tableIds}
                    onChange={(ids) => setField("tableIds", ids)}
                    searchPlaceholder="Filter by table or schema…"
                    emptyHint="This organization has no catalogued tables yet. Run a scan from Sources first."
                  />
                </div>
                <div className="cpform__span2">
                  <ReferencePicker
                    label="Semantic model versions"
                    options={semanticOptions.options}
                    loading={semanticOptions.loading}
                    error={semanticOptions.error}
                    selected={form.semanticIds}
                    onChange={(ids) => setField("semanticIds", ids)}
                    emptyHint="No semantic model versions in this project yet."
                    visibleRows={4}
                  />
                </div>
                <div className="cpform__span2">
                  <ReferencePicker
                    label="Glossary terms"
                    options={glossaryOptions.options}
                    loading={glossaryOptions.loading}
                    error={glossaryOptions.error}
                    selected={form.glossaryIds}
                    onChange={(ids) => setField("glossaryIds", ids)}
                    emptyHint="No approved glossary terms yet. Author them in Business meaning."
                    visibleRows={4}
                  />
                </div>
                <div className="cpform__span2">
                  <ReferencePicker
                    label="Eligible tools"
                    options={toolOptions.options}
                    loading={toolOptions.loading}
                    error={toolOptions.error}
                    selected={form.toolIds}
                    onChange={(ids) => setField("toolIds", ids)}
                    emptyHint="No published tools in this project. Publish one from Tool registry."
                    visibleRows={4}
                  />
                </div>
                <Field label="Consumer roles">
                  <input required value={form.consumerRoles} onChange={(e) => setField("consumerRoles", e.target.value)} />
                </Field>
                <Field label="Lineage depth">
                  <input
                    type="number"
                    min={0}
                    max={4}
                    value={form.lineageDepth}
                    onChange={(e) => setField("lineageDepth", e.target.value)}
                  />
                </Field>
                <Field label="Minimum quality score">
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={form.minimumScore}
                    onChange={(e) => setField("minimumScore", e.target.value)}
                  />
                </Field>
                <label className="cpform__checkbox cpform__span2">
                  <input
                    type="checkbox"
                    checked={form.denyOnCriticalIncident}
                    onChange={(e) => setField("denyOnCriticalIncident", e.target.checked)}
                  />
                  Deny consumption while a referenced table has an active critical incident
                </label>
              </div>

              <p className="cpform__privacy">
                The control plane stores identifiers and approved metadata only. Source values
                remain gateway-only and are never retained in Context Products.
              </p>

              <Button type="submit" variant="primary" disabled={creating || !projectId}>
                {creating ? "Creating…" : "Create governed draft"}
              </Button>
            </form>
          </article>

          <article className="cpguide">
            <p className="cpguide__eyebrow">COMPOSITION GUIDE</p>
            <h2 className="cpguide__h2">What belongs here</h2>
            <div className="cpguide__list">
              <div>
                <strong>Approved references only</strong>
                <span>Use reviewed table, semantic, glossary, and tool versions so the package can be compiled deterministically.</span>
              </div>
              <div>
                <strong>Purpose before payload</strong>
                <span>State the exact consumer purpose first, then include only the context needed for that purpose.</span>
              </div>
              <div>
                <strong>Quality gates stay visible</strong>
                <span>Set lineage depth and minimum quality so consumers inherit operational guardrails with the package.</span>
              </div>
            </div>
          </article>
        </aside>
      </div>
    </div>
  );
}
