import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ExecutionRead,
  ToolPlanCreate,
  ToolPlanDetailRead,
  ValidationResponse,
} from "../lib/types";
import {
  ApiError,
  cancelToolPlan,
  createToolPlan,
  executeToolPlan,
  fetchToolPlan,
  fetchToolPlanEvidence,
  validateToolPlan,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./ToolPlansScreen.css";

/* ---------------------------------------------------------------------------
   Tool plans -- multi-step tool orchestration, distinct from the single
   governed-tool-version CRUD/execute `ToolRegistryScreen` already owns. A
   "tool plan" is a budgeted, dependency-ordered *sequence* of governed-tool
   invocations (`tool_plans_api.py`, `/v1` prefix):

     - POST /v1/tool-plans                       create_tool_plan
     - GET  /v1/tool-plans/{plan_id}              get_tool_plan
     - POST /v1/tool-plans/{plan_id}/validate     validate_tool_plan
     - POST /v1/tool-plans/{plan_id}/execute      execute_tool_plan
     - POST /v1/tool-plans/{plan_id}/cancel       cancel_tool_plan
     - GET  /v1/tool-plans/{plan_id}/evidence     list_tool_plan_evidence

   Ported from the legacy portal's `#tool-plan-form` submit and its
   `plan-validate` / `plan-execute` / `plan-evidence` / `plan-cancel` button
   bindings (`ui/scripts/features/control-center.js`, end of `bindEvents`).

   SCOPE CUT, deliberate: like the legacy form, "Create plan" below only ever
   builds a *single-step* plan (one tool_id/tool_version/parameters/timeout/
   expected_cost). `ToolPlanCreate.steps` is a real array and the backend
   happily accepts many steps with cross-step `dependencies` -- multi-step
   plan *authoring* (an add-step / reorder / wire-a-dependency UI) is left
   as a future enhancement, not something forced into this first cut. Tool
   id/version are plain text inputs rather than a picker sourced from
   `fetchTools` -- that call is project-scoped and this screen has no
   project selector of its own; free-typing matches what the legacy form
   did.

   Every route above is gated by BOTH a role check AND an edition
   entitlement check for capability `"multi_step_tool_plans"`
   (`_deny_unless_entitled`, `tool_plans_api.py:138`) -- some org editions
   don't carry this feature at all. The two denials are distinguishable by
   the 403 body's `detail` string alone: the entitlement gate's is a bare
   reason code (`ENTITLEMENT_EDITION_INSUFFICIENT` /
   `ENTITLEMENT_CAPABILITY_UNREGISTERED`), while `require_roles`'s reads
   `"one of these roles is required: ..."` -- `describeApiError` below keys
   off that shape so the two render as clearly different messages instead
   of one generic "Forbidden".
--------------------------------------------------------------------------- */

type Kind = "info" | "success" | "error";

const planStatusTone = (status: string): Tone =>
  status === "COMPLETED" ? "ok"
    : status === "FAILED" ? "bad"
    : status === "CANCELLED" ? "mute"
    : status === "EXECUTING" ? "warn"
    : status === "VALIDATED" ? "info"
    : "mute"; // DRAFT

const stepStatusTone = (status: string): Tone =>
  status === "COMPLETED" ? "ok"
    : status === "FAILED" ? "bad"
    : status === "RUNNING" ? "warn"
    : status === "CANCELLED" || status === "SKIPPED" ? "mute"
    : "mute"; // PENDING

const severityTone = (severity: string): Tone =>
  severity === "ERROR" ? "bad" : severity === "WARNING" ? "warn" : "info";

const relTime = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

/** Distinguishes an edition-entitlement 403 (this org's edition simply does
 *  not carry `multi_step_tool_plans`) from an ordinary role 403 or any other
 *  API failure, purely from the `detail` string shape both server checks
 *  produce -- see this file's top comment. */
function describeApiError(e: unknown): string {
  if (e instanceof ApiError && e.status === 403 && e.detail.startsWith("ENTITLEMENT_")) {
    return `This organization's edition does not include multi-step tool plans (reason: ${e.detail}). Ask your platform admin about upgrading the edition -- this is not a permissions problem you can fix by requesting a role.`;
  }
  if (e instanceof ApiError) return e.detail;
  return e instanceof Error ? e.message : String(e);
}

/* -------------------------------- create form ------------------------------- */

interface StepDraft {
  toolId: string;
  toolVersion: string;
  parametersJson: string;
  timeoutSeconds: string;
  expectedCost: string;
}

interface BudgetDraft {
  maxSteps: string;
  maxTimeSeconds: string;
  maxTokens: string;
  maxCostUnits: string;
}

interface CreateFormState {
  name: string;
  step: StepDraft;
}

const INITIAL_STEP: StepDraft = {
  toolId: "",
  toolVersion: "",
  parametersJson: "{}",
  timeoutSeconds: "300",
  expectedCost: "0",
};

const INITIAL_BUDGET: BudgetDraft = {
  maxSteps: "20",
  maxTimeSeconds: "600",
  maxTokens: "100000",
  maxCostUnits: "100",
};

const INITIAL_FORM: CreateFormState = { name: "", step: INITIAL_STEP };

function CreatePlanPanel({
  form,
  setForm,
  budget,
  setBudget,
  creating,
  onSubmit,
}: {
  form: CreateFormState;
  setForm: (f: CreateFormState) => void;
  budget: BudgetDraft;
  setBudget: (b: BudgetDraft) => void;
  creating: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form className="tpform" onSubmit={onSubmit}>
      <header className="tpform__head">
        <div>
          <p className="tpform__eyebrow">NEW</p>
          <h2 className="tpform__h2">Create plan</h2>
          <p className="tpform__lede">
            One step to start -- the plan model supports many dependency-ordered
            steps, but multi-step authoring here is a future enhancement.
          </p>
        </div>
      </header>

      <div className="tpform__grid">
        <div className="tpform__span2">
          <Field label="Plan name">
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Nightly delinquency remediation"
              required
              minLength={2}
            />
          </Field>
        </div>

        <Field label="Tool id">
          <input
            value={form.step.toolId}
            onChange={(e) => setForm({ ...form, step: { ...form.step, toolId: e.target.value } })}
            placeholder="t_delinquency"
            required
          />
        </Field>
        <Field label="Tool version">
          <input
            value={form.step.toolVersion}
            onChange={(e) => setForm({ ...form, step: { ...form.step, toolVersion: e.target.value } })}
            placeholder="1"
            required
          />
        </Field>

        <div className="tpform__span2">
          <Field label="Parameters (JSON)">
            <textarea
              className="tpform__mono"
              rows={3}
              value={form.step.parametersJson}
              onChange={(e) => setForm({ ...form, step: { ...form.step, parametersJson: e.target.value } })}
            />
          </Field>
        </div>

        <Field label="Timeout (seconds)">
          <input
            type="number"
            min={1}
            max={3600}
            value={form.step.timeoutSeconds}
            onChange={(e) => setForm({ ...form, step: { ...form.step, timeoutSeconds: e.target.value } })}
          />
        </Field>
        <Field label="Expected cost">
          <input
            type="number"
            min={0}
            step="0.1"
            value={form.step.expectedCost}
            onChange={(e) => setForm({ ...form, step: { ...form.step, expectedCost: e.target.value } })}
          />
        </Field>
      </div>

      <div className="tpform__subhead">
        <h3>Budget</h3>
        <p>Defaults match the server's own `PlanBudgetCreate` -- tweak before creating.</p>
      </div>
      <div className="tpform__grid">
        <Field label="Max steps">
          <input
            type="number"
            min={1}
            max={100}
            value={budget.maxSteps}
            onChange={(e) => setBudget({ ...budget, maxSteps: e.target.value })}
          />
        </Field>
        <Field label="Max time (seconds)">
          <input
            type="number"
            min={1}
            max={86400}
            value={budget.maxTimeSeconds}
            onChange={(e) => setBudget({ ...budget, maxTimeSeconds: e.target.value })}
          />
        </Field>
        <Field label="Max tokens">
          <input
            type="number"
            min={0}
            value={budget.maxTokens}
            onChange={(e) => setBudget({ ...budget, maxTokens: e.target.value })}
          />
        </Field>
        <Field label="Max cost units">
          <input
            type="number"
            min={0}
            step="0.1"
            value={budget.maxCostUnits}
            onChange={(e) => setBudget({ ...budget, maxCostUnits: e.target.value })}
          />
        </Field>
      </div>

      <div className="tpform__actions">
        <Button type="submit" variant="primary" disabled={creating}>
          {creating ? "Creating…" : "Create plan"}
        </Button>
      </div>
    </form>
  );
}

/* -------------------------------- detail panel ------------------------------- */

function PlanDetail({
  plan,
  validation,
  busy,
  onValidate,
  onExecute,
  onCancel,
}: {
  plan: ToolPlanDetailRead;
  validation: ValidationResponse | null;
  busy: boolean;
  onValidate: () => void;
  onExecute: () => void;
  onCancel: () => void;
}) {
  const canExecute = plan.status === "DRAFT" || plan.status === "VALIDATED";
  const canCancel = plan.status !== "COMPLETED" && plan.status !== "CANCELLED";
  const budget = plan.budget as Record<string, unknown>;

  return (
    <article className="tpdetail">
      <header className="tpdetail__head">
        <div>
          <p className="tpdetail__eyebrow">PLAN</p>
          <h2 className="tpdetail__h2">{plan.name}</h2>
          <p className="tpdetail__key">{plan.id}</p>
        </div>
        <Pill tone={planStatusTone(plan.status)}>{plan.status}</Pill>
      </header>

      <div className="tpdetail__grid">
        <div><span>Created by</span><strong>{plan.created_by}</strong></div>
        <div><span>Created</span><strong>{relTime(plan.created_at)}</strong></div>
        <div><span>Updated</span><strong>{relTime(plan.updated_at)}</strong></div>
        {Object.entries(budget).map(([k, v]) => (
          <div key={k}><span>{k}</span><strong>{String(v)}</strong></div>
        ))}
      </div>

      <div className="tpdetail__actions">
        <Button onClick={onValidate} disabled={busy}>Validate</Button>
        <Button onClick={onExecute} disabled={busy || !canExecute} title={!canExecute ? `Only a DRAFT or VALIDATED plan can execute (this one is ${plan.status})` : undefined}>
          Execute
        </Button>
        <Button onClick={onCancel} disabled={busy || !canCancel} title={!canCancel ? `A ${plan.status} plan cannot be cancelled` : undefined}>
          Cancel
        </Button>
      </div>

      {validation ? (
        <div className={`tpvalidation tpvalidation--${validation.valid ? "ok" : "bad"}`} role="status">
          <p className="tpvalidation__head">{validation.valid ? "Valid — no issues found" : "Not valid — issues found"}</p>
          {validation.issues.length ? (
            <ul className="tpvalidation__issues">
              {validation.issues.map((issue, i) => (
                <li key={i}>
                  <Pill tone={severityTone(issue.severity)}>{issue.severity}</Pill>
                  <span>step {issue.step_sequence}: {issue.issue}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <div className="tpsteps">
        <h3 className="tpsteps__h3">Steps ({plan.steps.length})</h3>
        <ul className="tpsteps__list">
          {plan.steps.map((step) => (
            <li key={step.id} className="tpstep">
              <div className="tpstep__row">
                <Pill tone={stepStatusTone(step.status)}>{step.status}</Pill>
                <strong>#{step.sequence} {step.tool_id}@{step.tool_version}</strong>
              </div>
              <div className="tpstep__meta">
                <span>timeout {step.timeout_seconds}s</span>
                <span>cost {step.expected_cost}</span>
                {step.dependencies.length ? <span>depends on {step.dependencies.join(", ")}</span> : null}
              </div>
              {step.error_message ? <p className="tpstep__err">{step.error_message}</p> : null}
            </li>
          ))}
        </ul>
      </div>
    </article>
  );
}

function EvidenceRow({ execution }: { execution: ExecutionRead }) {
  const consumed = execution.budget_consumed as Record<string, unknown>;
  return (
    <li className="tpexecrow">
      <div className="tpexecrow__row">
        <Pill tone={planStatusTone(execution.status)}>{execution.status}</Pill>
        <span className="tpexecrow__key">{execution.id}</span>
      </div>
      <div className="tpexecrow__meta">
        <span>started {relTime(execution.started_at)}</span>
        <span>completed {relTime(execution.completed_at)}</span>
        <span>by {execution.executed_by}</span>
      </div>
      {Object.keys(consumed).length ? (
        <div className="tpexecrow__budget">
          {Object.entries(consumed).map(([k, v]) => (
            <span key={k}>{k}: {String(v)}</span>
          ))}
        </div>
      ) : null}
    </li>
  );
}

/* -------------------------------- screen ------------------------------- */

export function ToolPlansScreen() {
  const [params, setParams] = useUrlState();
  const planId = params.get("plan");

  const [plan, setPlan] = useState<ToolPlanDetailRead | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [validation, setValidation] = useState<ValidationResponse | null>(null);

  const [evidence, setEvidence] = useState<ExecutionRead[]>([]);
  const [evidenceTotal, setEvidenceTotal] = useState<number | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);

  const [statusMsg, setStatusMsg] = useState<{ text: string; kind: Kind } | null>(null);
  const [busy, setBusy] = useState(false);

  const detailInflight = useRef<AbortController | null>(null);
  const detailSeq = useRef(0);

  const loadPlan = useCallback(async (id: string) => {
    detailInflight.current?.abort();
    const ac = new AbortController();
    detailInflight.current = ac;
    const seq = ++detailSeq.current;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const detail = await fetchToolPlan(id, ac.signal);
      if (seq !== detailSeq.current) return;
      setPlan(detail);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== detailSeq.current) return;
      setDetailError(describeApiError(e));
      setPlan(null);
    } finally {
      if (seq === detailSeq.current) setDetailLoading(false);
    }
  }, []);

  const loadEvidence = useCallback(async (id: string) => {
    try {
      const page = await fetchToolPlanEvidence(id, { limit: 20 });
      setEvidence(page.items);
      setEvidenceTotal(page.total);
      setEvidenceError(null);
    } catch (e) {
      setEvidenceError(describeApiError(e));
    }
  }, []);

  useEffect(() => {
    if (!planId) {
      setPlan(null);
      setValidation(null);
      setEvidence([]);
      setEvidenceTotal(null);
      return;
    }
    void loadPlan(planId);
    void loadEvidence(planId);
    return () => detailInflight.current?.abort();
  }, [planId, loadPlan, loadEvidence]);

  const [form, setForm] = useState<CreateFormState>(INITIAL_FORM);
  const [budget, setBudget] = useState<BudgetDraft>(INITIAL_BUDGET);
  const [creating, setCreating] = useState(false);

  const submitCreate = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      let parameters: Record<string, unknown>;
      try {
        parameters = form.step.parametersJson.trim() ? JSON.parse(form.step.parametersJson) : {};
      } catch {
        setStatusMsg({ text: "Parameters must be valid JSON.", kind: "error" });
        return;
      }
      const body: ToolPlanCreate = {
        name: form.name,
        steps: [
          {
            sequence: 1,
            tool_id: form.step.toolId,
            tool_version: form.step.toolVersion,
            parameters,
            dependencies: [],
            timeout_seconds: Number(form.step.timeoutSeconds) || 300,
            expected_cost: Number(form.step.expectedCost) || 0,
          },
        ],
        budget: {
          max_steps: Number(budget.maxSteps) || 20,
          max_time_seconds: Number(budget.maxTimeSeconds) || 600,
          max_tokens: Number(budget.maxTokens) || 0,
          max_cost_units: Number(budget.maxCostUnits) || 0,
        },
      };
      setCreating(true);
      setStatusMsg({ text: "Creating plan…", kind: "info" });
      try {
        const created = await createToolPlan(body);
        setForm(INITIAL_FORM);
        setBudget(INITIAL_BUDGET);
        setValidation(null);
        setParams({ plan: created.id });
        setStatusMsg({ text: `Plan "${created.name}" created as ${created.status}.`, kind: "success" });
      } catch (e) {
        setStatusMsg({ text: describeApiError(e), kind: "error" });
      } finally {
        setCreating(false);
      }
    },
    [form, budget, setParams],
  );

  const runAction = useCallback(
    async (verb: string, action: () => Promise<unknown>, after?: () => void) => {
      if (!planId) return;
      setBusy(true);
      setStatusMsg({ text: `${verb}…`, kind: "info" });
      try {
        await action();
        after?.();
        await loadPlan(planId);
        await loadEvidence(planId);
        setStatusMsg({ text: `${verb} succeeded.`, kind: "success" });
      } catch (e) {
        setStatusMsg({ text: describeApiError(e), kind: "error" });
      } finally {
        setBusy(false);
      }
    },
    [planId, loadPlan, loadEvidence],
  );

  const onValidate = useCallback(() => {
    if (!planId) return;
    void (async () => {
      setBusy(true);
      setStatusMsg({ text: "Validating…", kind: "info" });
      try {
        const result = await validateToolPlan(planId);
        setValidation(result);
        await loadPlan(planId);
        setStatusMsg({
          text: result.valid ? "Plan is valid." : `Plan has ${result.issues.length} issue(s).`,
          kind: result.valid ? "success" : "error",
        });
      } catch (e) {
        setStatusMsg({ text: describeApiError(e), kind: "error" });
      } finally {
        setBusy(false);
      }
    })();
  }, [planId, loadPlan]);

  const onExecute = useCallback(() => {
    void runAction("Execute", () => executeToolPlan(planId as string));
  }, [planId, runAction]);

  const onCancel = useCallback(() => {
    void runAction("Cancel", () => cancelToolPlan(planId as string));
  }, [planId, runAction]);

  const evidenceCount = useMemo(() => evidenceTotal ?? evidence.length, [evidenceTotal, evidence]);

  return (
    <div className="tpscreen">
      <header className="tpscreen__head">
        <div>
          <p className="tpscreen__eyebrow">NEW</p>
          <h1 className="tpscreen__h1">Tool plans</h1>
          <p className="tpscreen__lede">
            Budgeted, multi-step sequences of governed-tool invocations -- validate a plan before
            spending its budget, execute it, and review every run's evidence.
          </p>
        </div>
      </header>

      {statusMsg ? (
        <div className={`tpscreen__status tpscreen__status--${statusMsg.kind}`} role="status">{statusMsg.text}</div>
      ) : null}

      <div className="tpscreen__body">
        <div className="tpscreen__main">
          {!planId ? (
            <Empty title="No plan selected" hint="Create one from the panel on the right, or open one by id." />
          ) : detailError ? (
            <ErrorState title="Tool plan could not be loaded" detail={detailError} onRetry={() => void loadPlan(planId)} />
          ) : detailLoading && !plan ? (
            <div className="tpscreen__skeleton" role="status" aria-live="polite">Loading plan…</div>
          ) : plan ? (
            <>
              <PlanDetail
                plan={plan}
                validation={validation}
                busy={busy}
                onValidate={onValidate}
                onExecute={onExecute}
                onCancel={onCancel}
              />
              <article className="tpevidence">
                <header className="tpevidence__head">
                  <p className="tpevidence__eyebrow">HISTORY</p>
                  <h2 className="tpevidence__h2">Evidence ({evidenceCount})</h2>
                </header>
                {evidenceError ? (
                  <ErrorState title="Evidence could not be loaded" detail={evidenceError} onRetry={() => void loadEvidence(planId)} />
                ) : evidence.length ? (
                  <ul className="tpevidence__list">
                    {evidence.map((ex) => <EvidenceRow key={ex.id} execution={ex} />)}
                  </ul>
                ) : (
                  <Empty title="No executions yet" hint="Evidence appears here once this plan has been executed." />
                )}
              </article>
            </>
          ) : null}
        </div>

        <aside className="tpscreen__rail">
          <CreatePlanPanel
            form={form}
            setForm={setForm}
            budget={budget}
            setBudget={setBudget}
            creating={creating}
            onSubmit={(e) => void submitCreate(e)}
          />
        </aside>
      </div>
    </div>
  );
}
