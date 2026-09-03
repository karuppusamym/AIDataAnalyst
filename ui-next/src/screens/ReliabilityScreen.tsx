import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ArchiveStatusRead,
  EvaluationResponse,
  NotificationRuleCreate,
  NotificationRuleRead,
  SlaStatusResponse,
  SloBudgetRead,
  SloDefinitionCreate,
  SloDefinitionRead,
} from "../lib/types";
import type { PageOf, ViolationRead } from "../lib/ui-types";
import {
  ApiError,
  createNotificationRule,
  createSloDefinition,
  evaluateDataContract,
  fetchArchiveStatus,
  fetchContractSlaStatus,
  fetchContractViolations,
  fetchNotificationRules,
  fetchSloBudget,
  fetchSloDefinitions,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill, StateDot } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./ReliabilityScreen.css";

/* ---------------------------------------------------------------------------
   Reliability — operational reliability posture: SLOs and their error
   budgets, notification/escalation rules, archive/WORM evidence, and runtime
   data-contract evaluation. Ports the legacy portal's `renderReliability()`
   panel (`ui/scripts/features/control-center.js`, the `#slo-form`/
   `#notification-rule-form`/`#contract-inspect-form` submit handlers, and
   the `data-slo-budget` click handler) onto the real, already-merged
   `observability_api.py` / `notification_api.py` / `runtime_contracts_api.py`
   routes those legacy handlers themselves call — see `lib/api.ts`'s
   "Reliability" block for the exact endpoint list, roles, and the one
   pre-existing backend/header gap that block documents (org-scoped reads
   here take no `organization_id` on the wire; a live run 400s until
   `identityHeaders()` — shared, out of this screen's scope — sends
   `X-Organization-Id`).

   One status strip, not per-panel state, mirrors both the legacy screen's
   single `#control-message` target and `ContextProductsScreen`'s own choice
   to do the same for its multiple actions.

   Deliberately out of scope: an org-wide contract picker. There is no
   `GET /v1/data-contracts` (or any org-wide list/search) endpoint — contracts
   only exist scoped under a data product
   (`GET /v1/data-products/{product_id}/contracts`), which is out of scope
   for this screen to wire up product selection for. So the contract
   inspector below takes a contract id as a plain text input, exactly like
   the legacy `#contract-inspect-form` does (`<input name="contract_id">`,
   no picker there either) — a deliberate scope cut, not an oversight.
--------------------------------------------------------------------------- */

const relTime = (iso: string | null | undefined): string => {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

const nf = new Intl.NumberFormat("en-US");
function humanize(s: string): string {
  return s.toLowerCase().replace(/_/g, " ");
}

const archiveTone = (status: string): Tone =>
  status === "HEALTHY" ? "ok" : status === "LEGAL_HOLD_ACTIVE" ? "warn" : "mute";
const budgetTone = (status: string): Tone =>
  status === "HEALTHY" ? "ok" : status === "AT_RISK" ? "warn" : status === "BREACHED" ? "bad" : "mute";
const severityTone = (severity: string): Tone => (severity === "CRITICAL" ? "bad" : "warn");
const enforcementTone = (action: string): Tone =>
  action === "ALLOW" ? "ok" : action === "WARN" ? "warn" : action === "BLOCK" ? "bad" : "mute";

type Kind = "info" | "success" | "error";

const SLO_KEY_RE = /^[a-z][a-z0-9_-]{1,99}$/;

interface SloFormState {
  sloKey: string;
  name: string;
  target: string;
  windowDays: string;
  threshold: string;
}

const INITIAL_SLO_FORM: SloFormState = {
  sloKey: "",
  name: "",
  target: "99.9",
  windowDays: "30",
  threshold: "99",
};

interface NotificationFormState {
  name: string;
  conditions: string;
  channel: "EMAIL" | "WEBHOOK" | "ITSM";
  recipients: string;
  escalationAfterMinutes: string;
  enabled: boolean;
}

const INITIAL_NOTIFICATION_FORM: NotificationFormState = {
  name: "",
  conditions: "{}",
  channel: "EMAIL",
  recipients: "",
  escalationAfterMinutes: "",
  enabled: true,
};

function CreateSloForm({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: (slo: SloDefinitionRead) => void;
}) {
  const [form, setForm] = useState<SloFormState>(INITIAL_SLO_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const setField = useCallback(<K extends keyof SloFormState>(key: K, value: SloFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  }, []);

  const target = Number(form.target);
  const windowDays = Number(form.windowDays);
  const threshold = Number(form.threshold);
  const valid =
    SLO_KEY_RE.test(form.sloKey) &&
    form.name.trim().length > 0 &&
    Number.isFinite(target) && target >= 0 && target <= 100 &&
    Number.isInteger(windowDays) && windowDays >= 1 && windowDays <= 365 &&
    Number.isFinite(threshold) && threshold >= 0 && threshold <= 100;

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!valid || submitting) return;
      setSubmitting(true);
      setError(null);
      try {
        const body: SloDefinitionCreate = {
          slo_key: form.sloKey, name: form.name.trim(),
          target, window_days: windowDays, threshold,
        };
        const slo = await createSloDefinition(orgId, body);
        setForm(INITIAL_SLO_FORM);
        onCreated(slo);
      } catch (e2) {
        setError(e2 instanceof ApiError ? e2.detail : (e2 as Error).message);
      } finally {
        setSubmitting(false);
      }
    },
    [valid, submitting, orgId, form, target, windowDays, threshold, onCreated],
  );

  return (
    <form className="relpanel" aria-label="Create SLO" onSubmit={(e) => void submit(e)}>
      <div className="relpanel__head">
        <p className="relpanel__eyebrow">ERROR BUDGET</p>
        <h2 className="relpanel__h2">Define an SLO</h2>
      </div>
      <div className="relpanel__grid">
        <Field label="Key">
          <input
            value={form.sloKey}
            onChange={(e) => setField("sloKey", e.target.value.trim().toLowerCase())}
            placeholder="agent-answer-latency-p95"
            required
          />
        </Field>
        <Field label="Name">
          <input
            value={form.name}
            onChange={(e) => setField("name", e.target.value)}
            placeholder="Agent answer latency (p95)"
            required
          />
        </Field>
        <Field label="Target %">
          <input
            type="number" min={0} max={100} step="0.1"
            value={form.target}
            onChange={(e) => setField("target", e.target.value)}
            required
          />
        </Field>
        <Field label="Threshold %">
          <input
            type="number" min={0} max={100} step="0.1"
            value={form.threshold}
            onChange={(e) => setField("threshold", e.target.value)}
            required
          />
        </Field>
        <Field label="Window (days)">
          <input
            type="number" min={1} max={365} step="1"
            value={form.windowDays}
            onChange={(e) => setField("windowDays", e.target.value)}
            required
          />
        </Field>
      </div>
      {error ? <p className="relpanel__err" role="alert">{error}</p> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Creating…" : "Create SLO"}
      </Button>
    </form>
  );
}

function CreateNotificationRuleForm({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: (rule: NotificationRuleRead) => void;
}) {
  const [form, setForm] = useState<NotificationFormState>(INITIAL_NOTIFICATION_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const setField = useCallback(<K extends keyof NotificationFormState>(key: K, value: NotificationFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  }, []);

  const recipients = form.recipients.split(",").map((r) => r.trim()).filter(Boolean);
  const valid = form.name.trim().length >= 3 && recipients.length > 0;

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!valid || submitting) return;
      let conditions: Record<string, unknown>;
      try {
        conditions = form.conditions.trim() ? JSON.parse(form.conditions) : {};
      } catch {
        setError("Conditions must be valid JSON.");
        return;
      }
      setSubmitting(true);
      setError(null);
      try {
        const body: NotificationRuleCreate = {
          name: form.name.trim(),
          conditions,
          channel: form.channel,
          recipients,
          escalation_after_minutes: form.escalationAfterMinutes.trim()
            ? Number(form.escalationAfterMinutes)
            : null,
          enabled: form.enabled,
        };
        const rule = await createNotificationRule(orgId, body);
        setForm(INITIAL_NOTIFICATION_FORM);
        onCreated(rule);
      } catch (e2) {
        setError(e2 instanceof ApiError ? e2.detail : (e2 as Error).message);
      } finally {
        setSubmitting(false);
      }
    },
    [valid, submitting, orgId, form, recipients, onCreated],
  );

  return (
    <form className="relpanel" aria-label="Create notification rule" onSubmit={(e) => void submit(e)}>
      <div className="relpanel__head">
        <p className="relpanel__eyebrow">ESCALATION ROUTING</p>
        <h2 className="relpanel__h2">Create notification rule</h2>
      </div>
      <div className="relpanel__grid">
        <Field label="Name">
          <input
            value={form.name}
            onChange={(e) => setField("name", e.target.value)}
            minLength={3}
            placeholder="SLO breach — page on-call"
            required
          />
        </Field>
        <Field label="Channel">
          <select value={form.channel} onChange={(e) => setField("channel", e.target.value as NotificationFormState["channel"])}>
            <option value="EMAIL">Email</option>
            <option value="WEBHOOK">Webhook</option>
            <option value="ITSM">ITSM</option>
          </select>
        </Field>
        <Field label="Escalate after (minutes)">
          <input
            type="number" min={1} max={525600} step="1"
            value={form.escalationAfterMinutes}
            onChange={(e) => setField("escalationAfterMinutes", e.target.value)}
            placeholder="optional"
          />
        </Field>
        <label className="relcheck">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setField("enabled", e.target.checked)}
          />
          Enabled
        </label>
      </div>
      <Field label="Recipients (comma-separated)">
        <input
          value={form.recipients}
          onChange={(e) => setField("recipients", e.target.value)}
          placeholder="oncall@tenant.example, steward@tenant.example"
          required
        />
      </Field>
      <Field label="Conditions (JSON matcher)">
        <textarea
          className="relpanel__textarea"
          value={form.conditions}
          onChange={(e) => setField("conditions", e.target.value)}
          rows={3}
          spellCheck={false}
        />
      </Field>
      {error ? <p className="relpanel__err" role="alert">{error}</p> : null}
      <Button type="submit" variant="primary" disabled={!valid || submitting}>
        {submitting ? "Creating…" : "Create rule"}
      </Button>
    </form>
  );
}

interface ContractEvidence {
  evaluation: EvaluationResponse;
  sla: SlaStatusResponse;
  violations: PageOf<ViolationRead>;
}

export function ReliabilityScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();

  const [archive, setArchive] = useState<ArchiveStatusRead | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [slos, setSlos] = useState<SloDefinitionRead[]>([]);
  const [sloError, setSloError] = useState<string | null>(null);
  const [rules, setRules] = useState<NotificationRuleRead[]>([]);
  const [rulesError, setRulesError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<{ text: string; kind: Kind } | null>(null);

  const [budgets, setBudgets] = useState<Record<string, SloBudgetRead | "loading" | string>>({});

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;
    setLoading(true);
    const [archiveResult, sloResult, ruleResult] = await Promise.allSettled([
      fetchArchiveStatus(ac.signal),
      fetchSloDefinitions(ORG, { limit: 200 }, ac.signal),
      fetchNotificationRules(ORG, { limit: 200 }, ac.signal),
    ]);
    if (seq !== reqSeq.current) return;

    if (archiveResult.status === "fulfilled") {
      setArchive(archiveResult.value);
      setArchiveError(null);
    } else if ((archiveResult.reason as Error)?.name !== "AbortError") {
      setArchiveError(archiveResult.reason instanceof ApiError ? archiveResult.reason.detail : String(archiveResult.reason));
    }

    if (sloResult.status === "fulfilled") {
      setSlos(sloResult.value.items);
      setSloError(null);
    } else if ((sloResult.reason as Error)?.name !== "AbortError") {
      setSloError(sloResult.reason instanceof ApiError ? sloResult.reason.detail : String(sloResult.reason));
    }

    if (ruleResult.status === "fulfilled") {
      setRules(ruleResult.value.items);
      setRulesError(null);
    } else if ((ruleResult.reason as Error)?.name !== "AbortError") {
      setRulesError(ruleResult.reason instanceof ApiError ? ruleResult.reason.detail : String(ruleResult.reason));
    }

    setLoading(false);
  }, [ORG]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const viewBudget = useCallback(async (sloId: string) => {
    setBudgets((prev) => ({ ...prev, [sloId]: "loading" }));
    try {
      const budget = await fetchSloBudget(sloId);
      setBudgets((prev) => ({ ...prev, [sloId]: budget }));
    } catch (e) {
      setBudgets((prev) => ({ ...prev, [sloId]: e instanceof ApiError ? e.detail : (e as Error).message }));
    }
  }, []);

  const [contractId, setContractId] = useState(params.get("contract") ?? "");
  const [evidence, setEvidence] = useState<ContractEvidence | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);

  const evaluate = useCallback(async () => {
    const id = contractId.trim();
    if (!id) return;
    setParams({ contract: id });
    setEvidenceLoading(true);
    setEvidenceError(null);
    setStatus({ text: "Evaluating contract against current state...", kind: "info" });
    try {
      const [evaluation, sla, violations] = await Promise.all([
        evaluateDataContract(id),
        fetchContractSlaStatus(id, 30),
        fetchContractViolations(id, { limit: 100, offset: 0 }),
      ]);
      setEvidence({ evaluation, sla, violations });
      setStatus({ text: "Contract evaluation completed.", kind: "success" });
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : (e as Error).message;
      setEvidenceError(detail);
      setStatus({ text: detail, kind: "error" });
    } finally {
      setEvidenceLoading(false);
    }
  }, [contractId, setParams]);

  return (
    <div className="rel">
      <header className="rel__head">
        <div>
          <h1 className="rel__h1">Reliability</h1>
          <p className="rel__lede">
            Error-budget posture for the SLOs governing agent and platform behavior, the
            escalation routes that page someone when they slip, the WORM archive evidence
            trail behind every audit event, and a live contract inspector for the runtime
            data-contract enforcement path.
          </p>
        </div>
      </header>

      {status ? <div className={`rel__status rel__status--${status.kind}`} role="status">{status.text}</div> : null}

      <section className="relsec" aria-label="Archive and WORM evidence posture">
        <header className="relsec__head">
          <p className="relsec__eyebrow">WORM EVIDENCE</p>
          <h2 className="relsec__h2">Archive posture</h2>
        </header>
        {archiveError ? (
          <ErrorState title="Archive status could not be loaded" detail={archiveError} onRetry={() => void load()} />
        ) : loading && !archive ? (
          <div className="rel__skeleton" role="status" aria-live="polite">Loading archive status…</div>
        ) : archive ? (
          <div className="rel__tiles">
            <div className="tile">
              <div className="tile__status">
                <StateDot tone={archiveTone(archive.status)} title={archive.status} />
                <Pill tone={archiveTone(archive.status)}>{humanize(archive.status)}</Pill>
              </div>
              <div className="tile__l">archive status</div>
            </div>
            <div className="tile">
              <div className="tile__n tnum">{nf.format(archive.total_archives)}</div>
              <div className="tile__l">WORM archives</div>
            </div>
            <div className="tile">
              <div className="tile__n tnum">{nf.format(archive.total_events_archived)}</div>
              <div className="tile__l">events archived</div>
            </div>
            <div className={`tile${archive.legal_hold_count > 0 ? " tile--warn" : ""}`}>
              <div className="tile__n tnum">{nf.format(archive.legal_hold_count)}</div>
              <div className="tile__l">legal holds</div>
            </div>
          </div>
        ) : null}
        {archive?.latest_archive_id ? (
          <p className="rel__archivemeta">
            Latest archive <code>{archive.latest_archive_id}</code>
            {archive.latest_checksum ? <> · checksum <code>{archive.latest_checksum.slice(0, 16)}…</code></> : null}
          </p>
        ) : null}
      </section>

      <section className="relsec" aria-label="Service level objectives">
        <header className="relsec__head">
          <p className="relsec__eyebrow">ERROR BUDGET</p>
          <h2 className="relsec__h2">SLOs</h2>
        </header>
        <div className="relsec__body">
          <div className="relsec__list">
            {sloError ? (
              <ErrorState title="SLOs could not be loaded" detail={sloError} onRetry={() => void load()} />
            ) : loading && slos.length === 0 ? (
              <div className="rel__skeleton" role="status" aria-live="polite">Loading SLOs…</div>
            ) : slos.length === 0 ? (
              <Empty title="No SLO definitions" hint="Define one to start tracking an error budget." />
            ) : (
              <ul className="rellist">
                {slos.map((slo) => {
                  const budget = budgets[slo.id];
                  return (
                    <li key={slo.id} className="relrow">
                      <div className="relrow__main">
                        <div className="relrow__title">{slo.name}</div>
                        <div className="relrow__key"><code>{slo.slo_key}</code></div>
                        <div className="relrow__meta">
                          <span>target {slo.target}%</span>
                          <span>threshold {slo.threshold}%</span>
                          <span>{slo.window_days}-day window</span>
                        </div>
                      </div>
                      <div className="relrow__act">
                        {budget && budget !== "loading" && typeof budget !== "string" ? (
                          <span className="relrow__budget">
                            <StateDot tone={budgetTone(budget.status)} title={budget.status} />
                            <Pill tone={budgetTone(budget.status)}>{humanize(budget.status)}</Pill>
                            {budget.current_value !== null ? (
                              <span className="rel__budgetval tnum">{budget.current_value.toFixed(2)}%</span>
                            ) : null}
                          </span>
                        ) : typeof budget === "string" && budget !== "loading" ? (
                          <span className="relrow__budgeterr">{budget}</span>
                        ) : null}
                        <Button disabled={budget === "loading"} onClick={() => void viewBudget(slo.id)}>
                          {budget === "loading" ? "Loading…" : "View budget"}
                        </Button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          <CreateSloForm orgId={ORG} onCreated={(slo) => { setSlos((prev) => [slo, ...prev]); setStatus({ text: `SLO "${slo.name}" created.`, kind: "success" }); }} />
        </div>
      </section>

      <section className="relsec" aria-label="Notification rules">
        <header className="relsec__head">
          <p className="relsec__eyebrow">ESCALATION ROUTING</p>
          <h2 className="relsec__h2">Notification rules</h2>
        </header>
        <div className="relsec__body">
          <div className="relsec__list">
            {rulesError ? (
              <ErrorState title="Notification rules could not be loaded" detail={rulesError} onRetry={() => void load()} />
            ) : loading && rules.length === 0 ? (
              <div className="rel__skeleton" role="status" aria-live="polite">Loading notification rules…</div>
            ) : rules.length === 0 ? (
              <Empty title="No notification rules" hint="Create one to route an alert to a channel and a set of recipients." />
            ) : (
              <ul className="rellist">
                {rules.map((rule) => (
                  <li key={rule.id} className="relrow">
                    <div className="relrow__main">
                      <div className="relrow__title">{rule.name}</div>
                      <div className="relrow__meta">
                        <Pill tone="mute">{rule.channel}</Pill>
                        <Pill tone={rule.enabled ? "ok" : "mute"}>{rule.enabled ? "enabled" : "disabled"}</Pill>
                        {rule.escalation_after_minutes ? <span>escalate after {rule.escalation_after_minutes}m</span> : null}
                      </div>
                      <div className="relrow__recipients">{rule.recipients.join(", ")}</div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <CreateNotificationRuleForm orgId={ORG} onCreated={(rule) => { setRules((prev) => [rule, ...prev]); setStatus({ text: `Notification rule "${rule.name}" created.`, kind: "success" }); }} />
        </div>
      </section>

      <section className="relsec" aria-label="Data contract inspector">
        <header className="relsec__head">
          <p className="relsec__eyebrow">RUNTIME ENFORCEMENT</p>
          <h2 className="relsec__h2">Data contract inspector</h2>
          <p className="relsec__lede">
            Evaluate a contract against current schema, quality, and freshness state, and see
            its rolling SLA compliance and violation history side by side. There is no org-wide
            contract picker — contracts are scoped under a data product, not this organization —
            so paste the contract's id below, exactly as the legacy inspector form does.
          </p>
        </header>
        <div className="relinspect">
          <Field label="Contract ID">
            <input
              value={contractId}
              onChange={(e) => setContractId(e.target.value)}
              placeholder="e.g. 3f9c1e2a-...-000000000001"
            />
          </Field>
          <Button variant="primary" disabled={!contractId.trim() || evidenceLoading} onClick={() => void evaluate()}>
            {evidenceLoading ? "Evaluating…" : "Evaluate"}
          </Button>
        </div>
        {evidenceError ? (
          <ErrorState title="Contract evaluation failed" detail={evidenceError} onRetry={() => void evaluate()} />
        ) : evidence ? (
          <div className="relevidence">
            <div className="relevidence__row">
              <Pill tone={enforcementTone(evidence.evaluation.enforcement_action)}>
                {evidence.evaluation.enforcement_action}
              </Pill>
              <Pill tone={evidence.evaluation.allowed ? "ok" : "bad"}>
                {evidence.evaluation.allowed ? "allowed" : "blocked"}
              </Pill>
              {evidence.evaluation.reason ? <span className="relevidence__reason">{evidence.evaluation.reason}</span> : null}
            </div>

            <div className="rel__tiles rel__tiles--sla">
              <div className={`tile${evidence.sla.compliant ? " tile--ok" : " tile--bad"}`}>
                <div className="tile__n tnum">{evidence.sla.compliant ? "Compliant" : "Non-compliant"}</div>
                <div className="tile__l">SLA status (30d)</div>
              </div>
              <div className="tile">
                <div className="tile__n tnum">{evidence.sla.uptime_percent.toFixed(2)}%</div>
                <div className="tile__l">uptime</div>
              </div>
              <div className="tile">
                <div className="tile__n tnum">{nf.format(evidence.sla.violations_in_period)}</div>
                <div className="tile__l">violations in period</div>
              </div>
              <div className="tile">
                <div className="tile__n tnum">{nf.format(evidence.sla.breach_minutes)}</div>
                <div className="tile__l">breach minutes</div>
              </div>
            </div>

            {evidence.violations.items.length === 0 ? (
              <Empty title="No violations on record" hint="This contract has no persisted violations in its history." />
            ) : (
              <ul className="rellist">
                {evidence.violations.items.map((v) => (
                  <li key={v.id} className="relrow relrow--violation">
                    <div className="relrow__main">
                      <div className="relrow__meta">
                        <Pill tone={severityTone(v.severity)}>{v.severity.toLowerCase()}</Pill>
                        <Pill tone="mute">{humanize(v.violation_type)}</Pill>
                        <span>{relTime(v.detected_at)}</span>
                      </div>
                      <pre className="relrow__evidence">{JSON.stringify(v.evidence, null, 2)}</pre>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <Empty title="No contract evaluated yet" hint="Paste a contract id and select Evaluate to see its combined evidence." />
        )}
      </section>
    </div>
  );
}
