import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AgentEvaluationRunRead, AiRuntimeStatusRead, ModelRouteConfigurationCreate, ModelRouteConfigurationRead } from "../lib/types";
import {
  ApiError,
  createModelRoute,
  fetchAgentEvaluations,
  fetchAiRuntimeStatus,
  fetchModelRoutes,
  runAgentEvaluation,
  submitModelRoute,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./AiGovernanceScreen.css";

/* ---------------------------------------------------------------------------
   AI governance -- the legacy portal's `agents-view` (`ui/index.html`,
   page heading "Models, agents, and evaluations"), ported onto the real,
   already-merged `ai_governance_api.py` model-route routes plus `api.py`'s
   `/ai/runtime-status` and `/agent-evaluations` routes that view calls
   (`ui/scripts/features/product-ai-control-plane.js` does NOT own this
   view -- `ui/app.js` wires `#model-route-form`, `#model-routes-table`,
   `#ai-runtime` and `#evaluation-table` directly; see the `Model route
   definition` block there for the source this file mirrors).

   Not to be confused with `AiRegistryScreen` (nav id "ai", legacy's
   `ai-registry-view`): that screen is AI *asset* trust/registry --
   published agents/tools and their computed trust score. This screen is
   the separate upstream concern of *defining and approving the model
   routes* those assets are eventually allowed to call, plus running the
   org's repeatable control-evaluation suite. Neither screen's data models
   overlap (`ModelRouteConfigurationRead` vs `AiAssetVersionRead`).

   Endpoints used (file:line as read on 2026-09-03):
     - POST /v1/organizations/{org}/model-routes   create_model_route     ai_governance_api.py:94
     - GET  /v1/organizations/{org}/model-routes    list_model_routes      ai_governance_api.py:167
     - POST /v1/model-routes/{id}/submit            submit_model_route     ai_governance_api.py:209
     - GET  /v1/ai/runtime-status                   ai_runtime_status      api.py:181
     - POST /v1/organizations/{org}/agent-evaluations run_agent_evaluation api.py:294
     - GET  /v1/organizations/{org}/agent-evaluations list_agent_evaluations api.py:349

   Scope, honestly:

   The legacy screen's 5-step "activation chain" (Version route -> Approve ->
   Select -> Register adapter -> Enable) is NOT five clickable actions in the
   legacy JS either -- grep `ui/app.js` and the only DOM wiring under
   `agents-view` is the create form and one `data-submit-route` button. It is
   a static legend explaining the maker-checker lifecycle a route's own
   `activation_status` (computed server-side in `_route_read`,
   `ai_governance_api.py:43`) narrates per row: DRAFT/PENDING_REVIEW/REJECTED
   mirror `status` directly; APPROVED_NOT_SELECTED means an approved route
   the deployed `settings.model_route` doesn't point at yet; GENERATION_DISABLED
   and ADAPTER_REGISTRATION_REQUIRED are environment/config gates; READY is
   the only state a route can actually be called from. "Approve" happens
   through the same governance review queue `ReviewQueueScreen` already
   reads (this screen's `submit` opens exactly that review, same as
   `StudioChangeSetsScreen`/`ContextProductsScreen`'s own submit flows) --
   this screen does not re-implement approval. "Select"/"Register adapter"/
   "Enable" are deployment-config and adapter-registration facts with no
   corresponding write route in `ai_governance_api.py` at all; they are read
   here, not actioned. This screen is honest about that: the chain renders
   as a legend, never as buttons that would silently do nothing.

   Deliberately left out:
     - The kill switch (`/organizations/{org}/kill-switch/{engage,release}`,
       `list_kill_switch_state`, `ai_governance_api.py:305/359/414`) --
       explicitly a *different*, single-operator/immediately-effective
       control (module 15's own file comment calls this out: "Deliberately
       NOT the ModelRouteConfiguration maker-checker lifecycle"). Legacy's
       `agents-view` never renders it either (no kill-switch DOM under
       `#agents-view` in `ui/index.html`); it belongs to the
       Operations-shaped surface, not here.
     - `AgentEvalGateRead`/`.../eval-gate` (`ai_registry_api.py:704/734`) --
       the per-AI-asset-version replay gate. That's `AiRegistryScreen`'s
       data (keyed by `ai_asset_version_id`), not this screen's org-wide
       control suite (`AgentEvaluationRunRead`, keyed by organization); the
       legacy `agents-view` never calls the eval-gate routes either.
     - Evaluation run detail beyond the list: legacy's generic
       `record-dialog` (any JSON row, opened via `data-record`) has no
       equivalent component in `ui-next` yet, so a run's raw `findings`
       array is shown inline on expand instead of in a modal -- same data,
       different chrome.
--------------------------------------------------------------------------- */

const PROVIDER_TYPES: ModelRouteConfigurationCreate["provider_type"][] = [
  "OPENAI",
  "GOOGLE_GEMINI",
  "AZURE_OPENAI",
  "AWS_BEDROCK",
  "GOOGLE_VERTEX",
  "OPENAI_COMPATIBLE_PRIVATE",
  "ON_PREM",
];

const RETENTION_POLICIES: ModelRouteConfigurationCreate["retention_policy"][] = [
  "ZERO_RETENTION",
  "BANK_MANAGED",
  "PROVIDER_CONTRACT",
];

const CAPABILITIES: { value: ModelRouteConfigurationCreate["capabilities"][number]; label: string; defaultOn: boolean }[] = [
  { value: "SQL_GENERATION", label: "SQL generation", defaultOn: true },
  { value: "CLASSIFICATION", label: "Metadata inference", defaultOn: true },
  { value: "EXPLANATION", label: "Explanation", defaultOn: false },
  { value: "EMBEDDINGS", label: "Embeddings", defaultOn: false },
];

const LIFECYCLE_STEPS = [
  { n: 1, label: "Version route", hint: "Maker definition" },
  { n: 2, label: "Approve", hint: "Independent checker" },
  { n: 3, label: "Select", hint: "Deployment config" },
  { n: 4, label: "Register adapter", hint: "Private runtime" },
  { n: 5, label: "Enable", hint: "Evaluated generation" },
] as const;

const human = (value: string) => value.toLowerCase().replace(/_/g, " ");

const statusTone = (s: string): Tone =>
  s === "APPROVED" ? "ok" : s === "PENDING_REVIEW" ? "info" : s === "REJECTED" ? "bad" : "warn";

const activationTone = (s: string): Tone =>
  s === "READY"
    ? "ok"
    : s === "APPROVED_NOT_SELECTED"
      ? "info"
      : s === "PENDING_REVIEW"
        ? "info"
        : s === "REJECTED"
          ? "bad"
          : s === "DRAFT"
            ? "mute"
            : "warn"; // GENERATION_DISABLED, ADAPTER_REGISTRATION_REQUIRED

// Same "flag the not-yet-configured raw values" rule the legacy `renderRuntime()`
// applies before humanizing them for display.
const RUNTIME_WARN_VALUES = new Set(["NOT_CONFIGURED", "development", "env"]);

type Kind = "info" | "success" | "error";

interface FormState {
  routeKey: string;
  displayName: string;
  providerType: ModelRouteConfigurationCreate["provider_type"];
  modelId: string;
  endpointAlias: string;
  credentialReference: string;
  dataResidency: string;
  retentionPolicy: ModelRouteConfigurationCreate["retention_policy"];
  maxInputTokens: string;
  maxOutputTokens: string;
  timeoutSeconds: string;
  capabilities: Set<string>;
}

const INITIAL_FORM: FormState = {
  routeKey: "",
  displayName: "",
  providerType: "OPENAI",
  modelId: "",
  endpointAlias: "",
  credentialReference: "",
  dataResidency: "US",
  retentionPolicy: "ZERO_RETENTION",
  maxInputTokens: "8000",
  maxOutputTokens: "2000",
  timeoutSeconds: "30",
  capabilities: new Set(CAPABILITIES.filter((c) => c.defaultOn).map((c) => c.value)),
};

function RuntimeTiles({ runtime }: { runtime: AiRuntimeStatusRead }) {
  const rows: [string, string, string][] = [
    ["Orchestration", runtime.orchestration_mode, runtime.runtime],
    [
      "Model route",
      runtime.model_route_status,
      `${runtime.available_model_providers.join(" / ")} adapters; generation enabled: ${runtime.model_generation_enabled}`,
    ],
    ["Identity", runtime.identity_provider, human(runtime.identity_verification)],
    [
      "Secrets",
      runtime.credential_provider,
      runtime.credential_provider_available ? "Adapter available" : "Adapter registration required",
    ],
  ];
  return (
    <div className="aig__runtime">
      {rows.map(([label, raw, detail]) => (
        <div className="aig__rtile" key={label}>
          <p>{label}</p>
          <strong className={RUNTIME_WARN_VALUES.has(raw) ? "aig__rtile-warn" : ""}>{human(raw)}</strong>
          <small>{detail}</small>
        </div>
      ))}
    </div>
  );
}

function RouteRow({
  route,
  busy,
  onSubmit,
}: {
  route: ModelRouteConfigurationRead;
  busy: boolean;
  onSubmit: () => void;
}) {
  return (
    <tr>
      <td>
        <span className="aig__primary">{route.display_name}</span>
        <span className="aig__secondary">{route.route_key} / version {route.version}</span>
      </td>
      <td><Pill tone={statusTone(route.status)}>{human(route.status)}</Pill></td>
      <td>
        {human(route.provider_type)}
        <span className="aig__secondary">{route.model_id}</span>
      </td>
      <td>{route.data_residency} / {human(route.retention_policy)}</td>
      <td>
        <Pill tone={activationTone(route.activation_status)}>{human(route.activation_status)}</Pill>
        <span className="aig__secondary">Adapter {route.adapter_available ? "registered" : "not registered"}</span>
      </td>
      <td>
        {route.status === "DRAFT" ? (
          <Button disabled={busy} onClick={onSubmit}>{busy ? "Submitting…" : "Submit"}</Button>
        ) : null}
      </td>
    </tr>
  );
}

function EvaluationRow({ run }: { run: AgentEvaluationRunRead }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        <td>
          <button className="aig__linkbtn" onClick={() => setOpen((v) => !v)}>{run.suite_version}</button>
          <span className="aig__secondary">{run.id}</span>
        </td>
        <td><Pill tone={run.status === "PASSED" ? "ok" : "bad"}>{human(run.status)}</Pill></td>
        <td>{run.passed_count} / {run.scenario_count}</td>
        <td>{(run.pass_rate * 100).toFixed(0)}%</td>
        <td>{run.created_at.slice(0, 10)}</td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={5}>
            <pre className="aig__pre">{JSON.stringify(run.findings, null, 2)}</pre>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function AiGovernanceScreen() {
  const ORG = useOrgId();

  const [runtime, setRuntime] = useState<AiRuntimeStatusRead | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);

  const [routes, setRoutes] = useState<ModelRouteConfigurationRead[]>([]);
  const [routesLoading, setRoutesLoading] = useState(true);
  const [routesError, setRoutesError] = useState<string | null>(null);

  const [evaluations, setEvaluations] = useState<AgentEvaluationRunRead[]>([]);
  const [evaluationsLoading, setEvaluationsLoading] = useState(true);
  const [evaluationsError, setEvaluationsError] = useState<string | null>(null);

  const [status, setStatus] = useState<{ text: string; kind: Kind } | null>(null);

  const routesInflight = useRef<AbortController | null>(null);
  const routesSeq = useRef(0);

  const loadRoutes = useCallback(async () => {
    routesInflight.current?.abort();
    const ac = new AbortController();
    routesInflight.current = ac;
    const seq = ++routesSeq.current;
    setRoutesLoading(true);
    setRoutesError(null);
    try {
      const page = await fetchModelRoutes(ORG, { limit: 200 }, ac.signal);
      if (seq !== routesSeq.current) return;
      setRoutes(page.items);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== routesSeq.current) return;
      setRoutesError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === routesSeq.current) setRoutesLoading(false);
    }
  }, [ORG]);

  const loadEvaluations = useCallback(async () => {
    setEvaluationsLoading(true);
    setEvaluationsError(null);
    try {
      const page = await fetchAgentEvaluations(ORG, { limit: 100 });
      setEvaluations(page.items);
    } catch (e) {
      setEvaluationsError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setEvaluationsLoading(false);
    }
  }, [ORG]);

  const loadRuntime = useCallback(async () => {
    try {
      setRuntime(await fetchAiRuntimeStatus());
      setRuntimeError(null);
    } catch (e) {
      setRuntimeError(e instanceof ApiError ? e.detail : (e as Error).message);
    }
  }, []);

  useEffect(() => {
    void loadRuntime();
  }, [loadRuntime]);

  useEffect(() => {
    void loadRoutes();
    return () => routesInflight.current?.abort();
  }, [loadRoutes]);

  useEffect(() => {
    void loadEvaluations();
  }, [loadEvaluations]);

  const [busyRouteId, setBusyRouteId] = useState<string | null>(null);
  const submitRoute = useCallback(
    async (routeId: string) => {
      setBusyRouteId(routeId);
      setStatus({ text: "Submitting for independent review…", kind: "info" });
      try {
        await submitModelRoute(routeId);
        setStatus({ text: "Model route submitted for independent review.", kind: "success" });
        await loadRoutes();
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setBusyRouteId(null);
      }
    },
    [loadRoutes],
  );

  const [evaluationRunning, setEvaluationRunning] = useState(false);
  const runEvaluation = useCallback(async () => {
    setEvaluationRunning(true);
    setStatus({ text: "Running control evaluation…", kind: "info" });
    try {
      await runAgentEvaluation(ORG);
      setStatus({ text: "Agent control evaluation completed.", kind: "success" });
      await loadEvaluations();
    } catch (e) {
      setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
    } finally {
      setEvaluationRunning(false);
    }
  }, [ORG, loadEvaluations]);

  const [form, setForm] = useState<FormState>(INITIAL_FORM);
  const [creating, setCreating] = useState(false);
  const setField = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  }, []);
  const toggleCapability = useCallback((value: string) => {
    setForm((prev) => {
      const next = new Set(prev.capabilities);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return { ...prev, capabilities: next };
    });
  }, []);

  const submitCreate = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const body: ModelRouteConfigurationCreate = {
        route_key: form.routeKey,
        display_name: form.displayName,
        provider_type: form.providerType,
        model_id: form.modelId,
        endpoint_alias: form.endpointAlias,
        credential_reference: form.credentialReference.trim() || null,
        data_residency: form.dataResidency,
        retention_policy: form.retentionPolicy,
        capabilities: CAPABILITIES.map((c) => c.value).filter((v) => form.capabilities.has(v)),
        max_input_tokens: Number(form.maxInputTokens || 8000),
        max_output_tokens: Number(form.maxOutputTokens || 2000),
        timeout_seconds: Number(form.timeoutSeconds || 30),
      };
      setCreating(true);
      setStatus({ text: "Creating governed draft…", kind: "info" });
      try {
        await createModelRoute(ORG, body);
        setForm(INITIAL_FORM);
        setStatus({ text: "Governed model route draft created.", kind: "success" });
        await loadRoutes();
      } catch (e) {
        setStatus({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setCreating(false);
      }
    },
    [ORG, form, loadRoutes],
  );

  const draftCount = useMemo(() => routes.filter((r) => r.status === "DRAFT").length, [routes]);

  return (
    <div className="aig">
      <header className="aig__head">
        <div>
          <p className="aig__eyebrow">AI CONTROL CENTER</p>
          <h1 className="aig__h1">Models, agents, and evaluations</h1>
          <p className="aig__lede">Govern definitions here; activate provider infrastructure through deployment controls.</p>
        </div>
        <Button variant="primary" disabled={evaluationRunning} onClick={() => void runEvaluation()}>
          {evaluationRunning ? "Running…" : "Run control evaluation"}
        </Button>
      </header>

      {status ? <div className={`aig__status aig__status--${status.kind}`} role="status">{status.text}</div> : null}

      <div className="aig__body">
        <div className="aig__main">
          <article className="aig__panel aig__panel--emphasis">
            <div className="aig__panelhead">
              <div>
                <p className="aig__eyebrow">ROUTE AUTHORING</p>
                <h2 className="aig__h2">Model route definition</h2>
                <p className="aig__lede">Define the version once, then progress it through separate governance and runtime activation steps.</p>
              </div>
              <Pill tone="warn">DRAFT</Pill>
            </div>

            <ol className="aig__chain" aria-label="Model route lifecycle">
              {LIFECYCLE_STEPS.map((step) => (
                <li key={step.n}>
                  <span className="aig__chain-n">{step.n}</span>
                  <strong>{step.label}</strong>
                  <small>{step.hint}</small>
                </li>
              ))}
            </ol>

            <form onSubmit={(e) => void submitCreate(e)}>
              <div className="aig__grid">
                <Field label="Route key">
                  <input
                    required
                    pattern="[a-z0-9][a-z0-9._\-]{2,99}"
                    placeholder="bank-sql-primary"
                    value={form.routeKey}
                    onChange={(e) => setField("routeKey", e.target.value)}
                  />
                </Field>
                <Field label="Display name">
                  <input
                    required
                    minLength={3}
                    placeholder="Bank SQL generation"
                    value={form.displayName}
                    onChange={(e) => setField("displayName", e.target.value)}
                  />
                </Field>
                <Field label="Provider">
                  <select value={form.providerType} onChange={(e) => setField("providerType", e.target.value as FormState["providerType"])}>
                    {PROVIDER_TYPES.map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Deployment alias">
                  <input
                    required
                    placeholder="approved-deployment-alias"
                    value={form.modelId}
                    onChange={(e) => setField("modelId", e.target.value)}
                  />
                </Field>
                <Field label="Endpoint alias">
                  <input
                    required
                    placeholder="private-ai-east-01"
                    value={form.endpointAlias}
                    onChange={(e) => setField("endpointAlias", e.target.value)}
                  />
                </Field>
                <Field label="Credential reference">
                  <input
                    placeholder="env://AIDA_LOCAL_MODEL_KEY"
                    value={form.credentialReference}
                    onChange={(e) => setField("credentialReference", e.target.value)}
                  />
                </Field>
                <Field label="Data residency">
                  <input
                    required
                    value={form.dataResidency}
                    onChange={(e) => setField("dataResidency", e.target.value)}
                  />
                </Field>
                <Field label="Retention">
                  <select value={form.retentionPolicy} onChange={(e) => setField("retentionPolicy", e.target.value as FormState["retentionPolicy"])}>
                    {RETENTION_POLICIES.map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Input token ceiling">
                  <input
                    type="number"
                    min={100}
                    max={1000000}
                    value={form.maxInputTokens}
                    onChange={(e) => setField("maxInputTokens", e.target.value)}
                  />
                </Field>
                <Field label="Output token ceiling">
                  <input
                    type="number"
                    min={100}
                    max={100000}
                    value={form.maxOutputTokens}
                    onChange={(e) => setField("maxOutputTokens", e.target.value)}
                  />
                </Field>
                <Field label="Timeout seconds">
                  <input
                    type="number"
                    min={1}
                    max={300}
                    value={form.timeoutSeconds}
                    onChange={(e) => setField("timeoutSeconds", e.target.value)}
                  />
                </Field>
                <fieldset className="aig__span2 aig__capabilities">
                  <legend>Capabilities</legend>
                  {CAPABILITIES.map((c) => (
                    <label className="aig__checkbox" key={c.value}>
                      <input
                        type="checkbox"
                        checked={form.capabilities.has(c.value)}
                        onChange={() => toggleCapability(c.value)}
                      />
                      {c.label}
                    </label>
                  ))}
                </fieldset>
              </div>
              <p className="aig__privacy">Only opaque references are accepted. Approval never activates credentials or model calls.</p>
              <Button type="submit" variant="primary" disabled={creating}>
                {creating ? "Creating…" : "Create governed draft"}
              </Button>
            </form>
          </article>

          <article className="aig__panel">
            <div className="aig__panelhead aig__panelhead--padded">
              <div>
                <p className="aig__eyebrow">EVALUATION EVIDENCE</p>
                <h2 className="aig__h2">Repeatable control suite</h2>
              </div>
            </div>
            {evaluationsError ? (
              <ErrorState title="Evaluation evidence could not be loaded" detail={evaluationsError} onRetry={() => void loadEvaluations()} />
            ) : evaluationsLoading ? (
              <div className="aig__skeleton" role="status">Loading governed records…</div>
            ) : evaluations.length === 0 ? (
              <Empty title="No evaluation evidence is available" hint="Run the control suite to record the first evaluation." />
            ) : (
              <div className="aig__scroll">
                <table className="aig__table">
                  <thead>
                    <tr><th>Suite / run</th><th>Status</th><th>Passed</th><th>Rate</th><th>Executed</th></tr>
                  </thead>
                  <tbody>
                    {evaluations.map((run) => (
                      <EvaluationRow key={run.id} run={run} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </article>
        </div>

        <aside className="aig__rail">
          {runtimeError ? (
            <ErrorState title="Runtime status could not be loaded" detail={runtimeError} onRetry={() => void loadRuntime()} />
          ) : runtime ? (
            <RuntimeTiles runtime={runtime} />
          ) : (
            <div className="aig__skeleton" role="status">Loading runtime status…</div>
          )}

          <article className="aig__panel aig__panel--subtle">
            <div className="aig__panelhead aig__panelhead--padded">
              <div>
                <p className="aig__eyebrow">MODEL ROUTE REGISTRY</p>
                <h2 className="aig__h2">Versions and activation</h2>
                <p className="aig__lede">Track approved routes separately from runtime readiness and adapter registration.</p>
              </div>
            </div>
            {routesError ? (
              <ErrorState title="Model routes could not be loaded" detail={routesError} onRetry={() => void loadRoutes()} />
            ) : routesLoading ? (
              <div className="aig__skeleton" role="status">Loading governed records…</div>
            ) : routes.length === 0 ? (
              <Empty title="No governed model route definitions" hint="Create the first version from the form on the left." />
            ) : (
              <div className="aig__scroll">
                <table className="aig__table">
                  <thead>
                    <tr><th>Route</th><th>Governance</th><th>Provider</th><th>Residency / retention</th><th>Activation</th><th>Action</th></tr>
                  </thead>
                  <tbody>
                    {routes.map((route) => (
                      <RouteRow key={route.id} route={route} busy={busyRouteId === route.id} onSubmit={() => void submitRoute(route.id)} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {draftCount > 0 ? <p className="aig__hint">{draftCount} draft route{draftCount === 1 ? "" : "s"} awaiting submission.</p> : null}
          </article>
        </aside>
      </div>
    </div>
  );
}
