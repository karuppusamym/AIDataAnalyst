import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ContextCompilationRead, ContextProductCreate, ContextProductRead, ProjectRead } from "../lib/types";
import {
  ApiError,
  compileContextProductVersion,
  createContextProduct,
  fetchContextProducts,
  fetchOrgProjects,
  requestContextProductDeprecation,
  submitContextProductVersion,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { useOrgId } from "../lib/org";
import { VirtualList } from "../components/VirtualList";
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
  tableIds: string;
  semanticIds: string;
  glossaryIds: string;
  toolIds: string;
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
  tableIds: "",
  semanticIds: "",
  glossaryIds: "",
  toolIds: "",
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
  onSubmit,
  onDeprecate,
  onCompile,
}: {
  product: ContextProductRead;
  busy: string | null;
  onSubmit: () => void;
  onDeprecate: () => void;
  onCompile: () => void;
}) {
  const v = product.latest_version;
  const isBusy = busy === v.id;
  return (
    <article className="cprow" aria-label={v.name}>
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
}: {
  target: string;
  onTargetChange: (t: string) => void;
  result: ContextCompilationRead | null;
  compiling: boolean;
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

  const [form, setForm] = useState<FormState>(INITIAL_FORM);
  const [creating, setCreating] = useState(false);
  const setField = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  }, []);

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
        table_ids: splitIds(form.tableIds),
        semantic_model_version_ids: splitIds(form.semanticIds),
        glossary_term_version_ids: splitIds(form.glossaryIds),
        eligible_tool_version_ids: splitIds(form.toolIds),
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
                    onSubmit={() => void runVersionAction(p.latest_version.id, "submit", "Requesting publication review", submitContextProductVersion)}
                    onDeprecate={() => void runVersionAction(p.latest_version.id, "deprecate", "Requesting deprecation review", requestContextProductDeprecation)}
                    onCompile={() => void compile(p.latest_version.id)}
                  />
                )}
              />
            )}
          </article>

          <CompilerPanel target={compileTarget} onTargetChange={setCompileTarget} result={compileResult} compiling={compiling} />
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
                  <Field label="Table UUIDs">
                    <input
                      placeholder="Comma-separated governed table UUIDs"
                      value={form.tableIds}
                      onChange={(e) => setField("tableIds", e.target.value)}
                    />
                  </Field>
                </div>
                <Field label="Semantic version UUIDs">
                  <input placeholder="Comma-separated" value={form.semanticIds} onChange={(e) => setField("semanticIds", e.target.value)} />
                </Field>
                <Field label="Glossary version UUIDs">
                  <input placeholder="Comma-separated" value={form.glossaryIds} onChange={(e) => setField("glossaryIds", e.target.value)} />
                </Field>
                <Field label="Eligible tool version UUIDs">
                  <input placeholder="Comma-separated" value={form.toolIds} onChange={(e) => setField("toolIds", e.target.value)} />
                </Field>
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
