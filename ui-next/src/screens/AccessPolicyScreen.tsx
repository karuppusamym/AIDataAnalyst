import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AccessPolicyCreate,
  AccessPolicyRead,
  AuthorizationSimulationRequest,
  SimulatedDecision,
  SimulatedSubject,
  WorkspaceRead,
} from "../lib/types";
import { ApiError, createAccessPolicy, fetchAccessPolicies, fetchOrgWorkspaces, simulateAuthorization } from "../lib/api";
import { useOrgId } from "../lib/org";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./AccessPolicyScreen.css";

/* ---------------------------------------------------------------------------
   ABAC access policies + authorization simulation -- the legacy portal's
   `renderPolicy()` / `#abac-policy-form` / `#abac-simulate-form`
   (`ui/scripts/features/control-center.js:98-102, 201-202`), ported onto the
   real `workspace_api.py` routes (confirmed by direct source read, not
   `api.py` despite the domain name):

     - GET  /v1/organizations/{organization_id}/access-policies      list_access_policies, workspace_api.py:511
     - POST /v1/organizations/{organization_id}/access-policies       create_access_policy, workspace_api.py:527
     - POST /v1/workspaces/{workspace_id}/authorization-simulations   simulate_authorization, workspace_api.py:620

   Three pieces, matching the legacy view's own shape:
     1. list        one row per `AccessPolicyRead`, with `version` surfaced
                     because the real endpoint auto-increments it per `code`
                     on repeated creates -- several rows can share a code.
     2. create form  `subject_match` / `resource_match` / `transform` /
                     `condition` stay raw JSON textareas, same as legacy --
                     this is free-form policy data, not something worth a
                     structured builder for. Parsed client-side with a clear
                     per-field error on invalid JSON. `status` defaults to
                     `DRAFT` unless the operator explicitly checks "Activate
                     immediately", so nobody activates a policy without
                     meaning to.
     3. simulation   "who could see this?" against a picked workspace +
                     hypothetical `subjects` (also raw JSON, same reasoning);
                     renders the returned `decisions` as a small table.

   Create is scoped narrower (`PlatformAdmin`/`OrganizationAdmin`) than list
   (`+ DataAdmin, Reviewer`) and simulate (any workspace member) -- a 403 from
   the create form is expected for a Reviewer/DataAdmin principal and is
   surfaced through the same `ApiError` detail path as any other failure,
   not specially handled.
--------------------------------------------------------------------------- */

type Kind = "info" | "success" | "error";

const EFFECTS: AccessPolicyCreate["effect"][] = ["ALLOW", "DENY", "MASK", "FILTER"];
const ACTIONS: AuthorizationSimulationRequest["action"][] = [
  "READ_METADATA",
  "READ_DATA",
  "PROPOSE",
  "APPROVE",
  "EXECUTE_TOOL",
  "CONSUME_CONTEXT",
  "EXPORT",
];

const effectTone = (effect: string): Tone =>
  effect === "ALLOW" ? "ok" : effect === "DENY" ? "bad" : effect === "MASK" ? "warn" : "info";

const statusTone = (status: string): Tone => (status === "ACTIVE" ? "ok" : "mute");

const splitList = (value: string): string[] =>
  value.split(",").map((v) => v.trim()).filter(Boolean);

/** Parses one JSON-object textarea, returning `{}` for a blank field and
 *  throwing a message naming the field for anything that fails to parse. */
function parseJsonObject(raw: string, label: string): Record<string, unknown> {
  const trimmed = raw.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error(`${label} must be valid JSON.`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object, e.g. {}.`);
  }
  return parsed as Record<string, unknown>;
}

/** Parses the `subjects` JSON array, required and non-empty (the real
 *  endpoint takes 1-25 entries). */
function parseSubjects(raw: string): SimulatedSubject[] {
  const trimmed = raw.trim();
  if (!trimmed) {
    throw new Error("Subjects is required -- provide a JSON array of at least one hypothetical subject.");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error("Subjects must be valid JSON.");
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error("Subjects must be a JSON array with at least one entry.");
  }
  if (parsed.length > 25) {
    throw new Error("Subjects accepts at most 25 entries.");
  }
  return parsed as SimulatedSubject[];
}

/* -------------------------------- create form ------------------------------- */

interface PolicyFormState {
  code: string;
  name: string;
  description: string;
  effect: AccessPolicyCreate["effect"];
  priority: string;
  actionMatch: string;
  subjectMatch: string;
  resourceMatch: string;
  transform: string;
  condition: string;
  activateNow: boolean;
}

const INITIAL_POLICY_FORM: PolicyFormState = {
  code: "",
  name: "",
  description: "",
  effect: "ALLOW",
  priority: "100",
  actionMatch: "",
  subjectMatch: "{}",
  resourceMatch: "{}",
  transform: "{}",
  condition: "{}",
  activateNow: false,
};

function CreatePolicyPanel({
  form,
  setForm,
  creating,
  onSubmit,
}: {
  form: PolicyFormState;
  setForm: (f: PolicyFormState) => void;
  creating: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
}) {
  const setField = <K extends keyof PolicyFormState>(key: K, value: PolicyFormState[K]) =>
    setForm({ ...form, [key]: value });

  return (
    <article className="apform">
      <form onSubmit={onSubmit}>
        <header className="apform__head">
          <div>
            <p className="apform__eyebrow">ACCESS POLICY</p>
            <h2 className="apform__h2">New policy</h2>
            <p className="apform__lede">Free-form subject/resource matches and transforms -- raw JSON, parsed here.</p>
          </div>
          <Pill tone={form.activateNow ? "ok" : "mute"}>{form.activateNow ? "ACTIVE" : "DRAFT"}</Pill>
        </header>

        <div className="apform__grid">
          <Field label="Code">
            <input
              required
              pattern="[a-z0-9][a-z0-9-]{1,79}"
              placeholder="mask-pii-columns"
              value={form.code}
              onChange={(e) => setField("code", e.target.value)}
            />
          </Field>
          <Field label="Name">
            <input required placeholder="Mask PII columns" value={form.name} onChange={(e) => setField("name", e.target.value)} />
          </Field>
          <Field label="Effect">
            <select value={form.effect} onChange={(e) => setField("effect", e.target.value as PolicyFormState["effect"])}>
              {EFFECTS.map((effect) => (
                <option key={effect} value={effect}>{effect}</option>
              ))}
            </select>
          </Field>
          <Field label="Priority (0-10000, lower evaluates first)">
            <input type="number" min={0} max={10000} value={form.priority} onChange={(e) => setField("priority", e.target.value)} />
          </Field>
          <Field label="Actions (comma-separated, blank = all)">
            <input placeholder="READ_DATA,EXPORT" value={form.actionMatch} onChange={(e) => setField("actionMatch", e.target.value)} />
          </Field>
        </div>

        <Field label="Description">
          <input placeholder="What this policy does and why" value={form.description} onChange={(e) => setField("description", e.target.value)} />
        </Field>

        <div className="apform__jsongrid">
          <Field label="Subject match (JSON object)">
            <textarea rows={4} value={form.subjectMatch} onChange={(e) => setField("subjectMatch", e.target.value)} />
          </Field>
          <Field label="Resource match (JSON object)">
            <textarea rows={4} value={form.resourceMatch} onChange={(e) => setField("resourceMatch", e.target.value)} />
          </Field>
          <Field label="Transform (JSON object)">
            <textarea rows={4} value={form.transform} onChange={(e) => setField("transform", e.target.value)} />
          </Field>
          <Field label="Condition (JSON object)">
            <textarea rows={4} value={form.condition} onChange={(e) => setField("condition", e.target.value)} />
          </Field>
        </div>

        <label className="apform__toggle">
          <input type="checkbox" checked={form.activateNow} onChange={(e) => setField("activateNow", e.target.checked)} />
          Activate immediately (otherwise saved as DRAFT and has no effect until activated)
        </label>

        <Button type="submit" variant="primary" disabled={creating}>
          {creating ? "Creating…" : "Create policy"}
        </Button>
      </form>
    </article>
  );
}

/* -------------------------------- policy list -------------------------------- */

function PolicyList({ policies }: { policies: AccessPolicyRead[] }) {
  if (policies.length === 0) {
    return <Empty title="No access policies" hint="Create one with the form to define who can see, mask, or export what." />;
  }
  return (
    <div className="aptable" role="table" aria-label="Access policies">
      <table>
        <thead>
          <tr>
            <th>Code</th>
            <th>Ver.</th>
            <th>Name</th>
            <th>Effect</th>
            <th>Priority</th>
            <th>Actions</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {policies.map((policy) => (
            <tr key={policy.id}>
              <td className="aptable__code">{policy.code}</td>
              <td>{policy.version}</td>
              <td>{policy.name}</td>
              <td><Pill tone={effectTone(policy.effect)}>{policy.effect}</Pill></td>
              <td>{policy.priority}</td>
              <td className="aptable__actions">{policy.action_match.length ? policy.action_match.join(", ") : "ALL"}</td>
              <td><Pill tone={statusTone(policy.status)}>{policy.status}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* -------------------------------- simulation -------------------------------- */

interface SimFormState {
  workspaceId: string;
  action: AuthorizationSimulationRequest["action"];
  resourceType: string;
  resourceId: string;
  datasourceId: string;
  schemaName: string;
  classifications: string;
  certification: string;
  qualityState: string;
  freshnessState: string;
  subjects: string;
}

const INITIAL_SIM_FORM: SimFormState = {
  workspaceId: "",
  action: "READ_DATA",
  resourceType: "",
  resourceId: "",
  datasourceId: "",
  schemaName: "",
  classifications: "",
  certification: "",
  qualityState: "",
  freshnessState: "",
  subjects: `[{"principal_kind":"HUMAN","roles":["Analyst"],"purpose":"ad-hoc analysis"}]`,
};

function DecisionTable({ decisions }: { decisions: SimulatedDecision[] }) {
  if (decisions.length === 0) {
    return <Empty title="No decisions returned" />;
  }
  return (
    <div className="aptable" role="table" aria-label="Simulation decisions">
      <table>
        <thead>
          <tr>
            <th>Principal kind</th>
            <th>Roles</th>
            <th>Allowed</th>
            <th>Reason</th>
            <th>Matched policy</th>
            <th>Masked classifications</th>
            <th>Row filters</th>
          </tr>
        </thead>
        <tbody>
          {decisions.map((decision, i) => (
            <tr key={i}>
              <td>{decision.principal_kind}</td>
              <td>{decision.roles.length ? decision.roles.join(", ") : "—"}</td>
              <td><Pill tone={decision.allowed ? "ok" : "bad"}>{decision.allowed ? "ALLOWED" : "DENIED"}</Pill></td>
              <td>{decision.reason_code}</td>
              <td>{decision.matched_policy_code ?? "—"}</td>
              <td>{decision.masked_classifications.length ? decision.masked_classifications.join(", ") : "—"}</td>
              <td>{decision.row_filters.length ? decision.row_filters.join(", ") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SimulationPanel({ workspaces }: { workspaces: WorkspaceRead[] }) {
  const [form, setForm] = useState<SimFormState>(INITIAL_SIM_FORM);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<SimulatedDecision[] | null>(null);

  useEffect(() => {
    if (!form.workspaceId && workspaces.length > 0) {
      setForm((prev) => ({ ...prev, workspaceId: workspaces[0]?.id ?? prev.workspaceId }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaces]);

  const setField = <K extends keyof SimFormState>(key: K, value: SimFormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      setError(null);
      if (!form.workspaceId) {
        setError("Choose a workspace before running a simulation.");
        return;
      }
      let subjects: SimulatedSubject[];
      try {
        subjects = parseSubjects(form.subjects);
      } catch (err) {
        setError((err as Error).message);
        return;
      }
      const body: AuthorizationSimulationRequest = {
        workspace_id: form.workspaceId,
        action: form.action,
        resource_type: form.resourceType,
        resource_id: form.resourceId || null,
        datasource_id: form.datasourceId || null,
        schema_name: form.schemaName || null,
        classifications: splitList(form.classifications),
        certification: form.certification || null,
        quality_state: form.qualityState || null,
        freshness_state: form.freshnessState || null,
        subjects,
      };
      setRunning(true);
      setDecisions(null);
      try {
        const result = await simulateAuthorization(form.workspaceId, body);
        setDecisions(result.decisions);
      } catch (err) {
        setError(err instanceof ApiError ? err.detail : (err as Error).message);
      } finally {
        setRunning(false);
      }
    },
    [form],
  );

  return (
    <article className="apsim">
      <header className="apsim__head">
        <p className="apsim__eyebrow">AUTHORIZATION SIMULATION</p>
        <h2 className="apsim__h2">Who could see this?</h2>
        <p className="apsim__lede">Runs against the live policy engine without changing any access.</p>
      </header>
      <form onSubmit={(e) => void submit(e)}>
        <div className="apsim__grid">
          <Field label="Workspace">
            <select required value={form.workspaceId} onChange={(e) => setField("workspaceId", e.target.value)}>
              <option value="">{workspaces.length ? "Select a workspace…" : "No workspaces available"}</option>
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Action">
            <select value={form.action} onChange={(e) => setField("action", e.target.value as SimFormState["action"])}>
              {ACTIONS.map((action) => (
                <option key={action} value={action}>{action}</option>
              ))}
            </select>
          </Field>
          <Field label="Resource type">
            <input required placeholder="table" value={form.resourceType} onChange={(e) => setField("resourceType", e.target.value)} />
          </Field>
          <Field label="Resource id (optional)">
            <input value={form.resourceId} onChange={(e) => setField("resourceId", e.target.value)} />
          </Field>
          <Field label="Datasource id (optional)">
            <input value={form.datasourceId} onChange={(e) => setField("datasourceId", e.target.value)} />
          </Field>
          <Field label="Schema name (optional)">
            <input value={form.schemaName} onChange={(e) => setField("schemaName", e.target.value)} />
          </Field>
          <Field label="Classifications (comma-separated)">
            <input placeholder="PII,RESTRICTED" value={form.classifications} onChange={(e) => setField("classifications", e.target.value)} />
          </Field>
          <Field label="Certification (optional)">
            <input value={form.certification} onChange={(e) => setField("certification", e.target.value)} />
          </Field>
          <Field label="Quality state (optional)">
            <input value={form.qualityState} onChange={(e) => setField("qualityState", e.target.value)} />
          </Field>
          <Field label="Freshness state (optional)">
            <input value={form.freshnessState} onChange={(e) => setField("freshnessState", e.target.value)} />
          </Field>
        </div>
        <Field label="Subjects (JSON array, 1-25 entries)">
          <textarea rows={4} value={form.subjects} onChange={(e) => setField("subjects", e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" disabled={running}>
          {running ? "Running…" : "Run simulation"}
        </Button>
      </form>
      {error ? <ErrorState title="Simulation could not run" detail={error} onRetry={() => setError(null)} /> : null}
      {decisions ? <DecisionTable decisions={decisions} /> : null}
    </article>
  );
}

/* -------------------------------- screen -------------------------------- */

export function AccessPolicyScreen() {
  const ORG = useOrgId();

  const [policies, setPolicies] = useState<AccessPolicyRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<{ text: string; kind: Kind } | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceRead[]>([]);

  const inflight = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    setLoading(true);
    setListError(null);
    try {
      const page = await fetchAccessPolicies(ORG, { limit: 200 }, ac.signal);
      setPolicies(page.items);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setListError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  }, [ORG]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  useEffect(() => {
    const ac = new AbortController();
    fetchOrgWorkspaces(ORG, ac.signal)
      .then((page) => setWorkspaces(page.items))
      .catch(() => {
        /* the workspace picker degrades to empty; the rest of the screen still works */
      });
    return () => ac.abort();
  }, [ORG]);

  const [form, setForm] = useState<PolicyFormState>(INITIAL_POLICY_FORM);
  const [creating, setCreating] = useState(false);

  const submitPolicy = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      setStatusMsg(null);
      let subjectMatch: Record<string, unknown>;
      let resourceMatch: Record<string, unknown>;
      let transform: Record<string, unknown>;
      let condition: Record<string, unknown>;
      try {
        subjectMatch = parseJsonObject(form.subjectMatch, "Subject match");
        resourceMatch = parseJsonObject(form.resourceMatch, "Resource match");
        transform = parseJsonObject(form.transform, "Transform");
        condition = parseJsonObject(form.condition, "Condition");
      } catch (err) {
        setStatusMsg({ text: (err as Error).message, kind: "error" });
        return;
      }
      const priority = Number(form.priority);
      if (!Number.isFinite(priority) || priority < 0 || priority > 10000) {
        setStatusMsg({ text: "Priority must be a number between 0 and 10000.", kind: "error" });
        return;
      }
      const body: AccessPolicyCreate = {
        code: form.code,
        name: form.name,
        description: form.description,
        effect: form.effect,
        priority,
        subject_match: subjectMatch,
        resource_match: resourceMatch,
        action_match: splitList(form.actionMatch),
        transform,
        condition,
        status: form.activateNow ? "ACTIVE" : "DRAFT",
      };
      setCreating(true);
      try {
        await createAccessPolicy(ORG, body);
        setStatusMsg({ text: `Policy "${form.code}" created.`, kind: "success" });
        setForm(INITIAL_POLICY_FORM);
        await load();
      } catch (err) {
        setStatusMsg({ text: err instanceof ApiError ? err.detail : (err as Error).message, kind: "error" });
      } finally {
        setCreating(false);
      }
    },
    [ORG, form, load],
  );

  return (
    <div className="apscreen">
      <header className="apscreen__head">
        <div>
          <p className="apscreen__eyebrow">ABAC</p>
          <h1 className="apscreen__h1">Access policies</h1>
          <p className="apscreen__lede">
            Define who can see, mask, or export what, and simulate the live policy engine against hypothetical subjects
            before anything changes.
          </p>
        </div>
      </header>

      {statusMsg ? <div className={`apscreen__status apscreen__status--${statusMsg.kind}`} role="status">{statusMsg.text}</div> : null}

      <div className="apscreen__body">
        <div className="apscreen__main">
          <article className="aplist">
            <header className="aplist__head">
              <p className="aplist__eyebrow">POLICIES</p>
              <h2 className="aplist__h2">Access policy list</h2>
            </header>
            {loading ? <div className="apscreen__skeleton">Loading access policies…</div> : null}
            {!loading && listError ? <ErrorState title="Access policies could not be loaded" detail={listError} onRetry={() => void load()} /> : null}
            {!loading && !listError ? <PolicyList policies={policies} /> : null}
          </article>
        </div>
        <div className="apscreen__rail">
          <CreatePolicyPanel form={form} setForm={setForm} creating={creating} onSubmit={(e) => void submitPolicy(e)} />
        </div>
      </div>

      <SimulationPanel workspaces={workspaces} />
    </div>
  );
}
