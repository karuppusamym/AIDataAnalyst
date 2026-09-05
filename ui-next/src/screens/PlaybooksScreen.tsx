import { useCallback, useEffect, useState } from "react";
import type {
  DataSourceRead,
  PlaybookCreate,
  PlaybookRead,
  PlaybookRunResultRead,
} from "../lib/types";
import {
  ApiError,
  createPlaybook,
  deletePlaybook,
  fetchOrgDatasources,
  fetchPlaybooks,
  runPlaybookNow,
  updatePlaybook,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/workflow-author.css";
import "./PlaybooksScreen.css";

/* ---------------------------------------------------------------------------
   Playbooks — AT-1.

   A playbook is a saved rule: "on datasource X, wherever the table name
   matches pattern Y, apply action Z" — run on a schedule or on demand,
   producing either an auto-applied bulk action or a governance-review
   proposal (`aida.playbooks::evaluate_and_run_playbook`, called both by the
   fleet scheduler and by this screen's "Run now"). Every call below hits a
   real, already-merged route (`playbooks_api.py`); see `../lib/api.ts`'s own
   Playbooks section for the endpoint-by-endpoint mapping.
--------------------------------------------------------------------------- */

/** Derived from the generated `PlaybookCreate` shape rather than hand-named
 *  literal-union types (`types.ts` inlines these on the field, it does not
 *  export separate `PlaybookAction`/`PlaybookMatchField` names). */
type PlaybookAction = PlaybookCreate["action"];
type PlaybookMatchField = NonNullable<PlaybookCreate["match_field"]>;

const ACTIONS: PlaybookAction[] = ["TAG", "CLASSIFY", "OWN", "CERTIFY"];
const MATCH_FIELDS: PlaybookMatchField[] = ["TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME"];
const CLASSIFICATIONS = ["UNCLASSIFIED", "PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "PHI", "PCI", "SECRET"];

function actionTone(action: string): Tone {
  switch (action) {
    case "TAG": return "info";
    case "CLASSIFY": return "warn";
    case "OWN": return "accent";
    case "CERTIFY": return "ok";
    default: return "mute";
  }
}

function relative(iso: string | null): string {
  if (!iso) return "never run";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (!Number.isFinite(minutes)) return "never run";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function describeRunResult(name: string, result: PlaybookRunResultRead): string {
  const outcome = result.outcome.toLowerCase().replace(/_/g, " ");
  let ref = "";
  if (result.bulk_action_run_id) ref = ` (bulk action run ${result.bulk_action_run_id.slice(0, 8)})`;
  else if (result.bulk_stewardship_operation_id) ref = ` (stewardship operation ${result.bulk_stewardship_operation_id.slice(0, 8)})`;
  else if (result.governance_review_id) ref = ` (governance review ${result.governance_review_id.slice(0, 8)})`;
  return `"${name}" matched ${result.matched_count} object(s) — ${outcome}${ref}.`;
}

function PlaybookRow({
  playbook,
  busy,
  onRun,
  onToggle,
  onDelete,
}: {
  playbook: PlaybookRead;
  busy: boolean;
  onRun: (playbook: PlaybookRead) => void;
  onToggle: (playbook: PlaybookRead) => void;
  onDelete: (playbook: PlaybookRead) => void;
}) {
  return (
    <li className="pbk__row">
      <div className="pbk__rowmain">
        <div className="pbk__rowtitle">
          <strong>{playbook.name}</strong>
          <Pill tone={actionTone(playbook.action)}>{playbook.action}</Pill>
          <Pill tone={playbook.enabled ? "ok" : "mute"}>{playbook.enabled ? "enabled" : "disabled"}</Pill>
        </div>
        <dl className="pbk__facts">
          <div>
            <dt>Datasource</dt>
            <dd className="pbk__mono">{playbook.datasource_id}</dd>
          </div>
          <div>
            <dt>Match</dt>
            <dd>
              {playbook.match_field} · <code>{playbook.match_pattern}</code>
            </dd>
          </div>
          <div>
            <dt>Schedule</dt>
            <dd>every {playbook.schedule_interval_minutes}m</dd>
          </div>
          <div>
            <dt>Last run</dt>
            <dd>{relative(playbook.last_run_at)}</dd>
          </div>
        </dl>
      </div>
      <div className="pbk__rowactions">
        <Button
          disabled={busy || !playbook.enabled}
          onClick={() => onRun(playbook)}
          title={playbook.enabled ? undefined : "Enable this playbook to run it"}
        >
          Run now
        </Button>
        <Button disabled={busy} onClick={() => onToggle(playbook)}>
          {playbook.enabled ? "Disable" : "Enable"}
        </Button>
        <Button disabled={busy} onClick={() => onDelete(playbook)}>
          Delete
        </Button>
      </div>
    </li>
  );
}

function CreatePlaybookForm({
  organizationId,
  onCreated,
}: {
  organizationId: string;
  onCreated: (playbook: PlaybookRead) => void;
}) {
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);
  const [name, setName] = useState("");
  const [action, setAction] = useState<PlaybookAction>("TAG");
  const [datasourceId, setDatasourceId] = useState("");
  const [matchField, setMatchField] = useState<PlaybookMatchField>("TABLE_NAME");
  const [matchPattern, setMatchPattern] = useState("");
  const [columnNamePattern, setColumnNamePattern] = useState("");
  const [tagKey, setTagKey] = useState("");
  const [classification, setClassification] = useState("PII");
  const [ownerType, setOwnerType] = useState<"INDIVIDUAL" | "GROUP">("INDIVIDUAL");
  const [ownerPrincipal, setOwnerPrincipal] = useState("");
  const [rationale, setRationale] = useState("");
  const [expiresAfterDays, setExpiresAfterDays] = useState(90);
  const [scheduleMinutes, setScheduleMinutes] = useState(60);
  const [autoApplyMax, setAutoApplyMax] = useState(0);
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const run = async (action_: () => Promise<void>) => {
    setBusy(true);
    setMessage("");
    try {
      await action_();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const actionParameters = (): Record<string, unknown> => {
    switch (action) {
      case "TAG": return { tag_key: tagKey };
      case "CLASSIFY": return { classification };
      case "OWN": return { owner_type: ownerType, owner_principal: ownerPrincipal };
      case "CERTIFY": return { rationale, expires_after_days: expiresAfterDays };
    }
  };

  const canSubmit =
    name.trim().length > 0 &&
    datasourceId.length > 0 &&
    matchPattern.trim().length > 0 &&
    (action !== "CLASSIFY" || columnNamePattern.trim().length > 0) &&
    (action !== "TAG" || tagKey.trim().length > 0) &&
    (action !== "OWN" || ownerPrincipal.trim().length >= 2) &&
    (action !== "CERTIFY" || (rationale.trim().length >= 10 && expiresAfterDays > 0)) &&
    scheduleMinutes >= 5 &&
    scheduleMinutes <= 10_080;

  const reset = () => {
    setName("");
    setMatchPattern("");
    setColumnNamePattern("");
    setTagKey("");
    setOwnerPrincipal("");
    setRationale("");
    setExpiresAfterDays(90);
  };

  return (
    <details
      className="workflow-author"
      onToggle={(event) => {
        if (event.currentTarget.open && datasources.length === 0) {
          void run(async () => setDatasources((await fetchOrgDatasources(organizationId)).items));
        }
      }}
    >
      <summary>Create playbook</summary>
      <p>
        A playbook is a saved rule: on one datasource, wherever an object's name matches a pattern, apply
        one action on a schedule. Each run either auto-applies (bounded by "auto-apply max items") or
        queues a governance review.
      </p>
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Action">
        <select value={action} onChange={(e) => setAction(e.target.value as PlaybookAction)}>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
      </Field>
      <Field label="Datasource">
        <select value={datasourceId} onChange={(e) => setDatasourceId(e.target.value)}>
          <option value="">Select datasource</option>
          {datasources.map((d) => (
            <option key={d.id} value={d.id}>{d.name}</option>
          ))}
        </select>
      </Field>
      <Field label="Match field">
        <select value={matchField} onChange={(e) => setMatchField(e.target.value as PlaybookMatchField)}>
          {MATCH_FIELDS.map((f) => (
            <option key={f} value={f}>{f}</option>
          ))}
        </select>
      </Field>
      <Field label="Match pattern">
        <input
          value={matchPattern}
          onChange={(e) => setMatchPattern(e.target.value)}
          placeholder="e.g. stg_% or finance.%"
        />
      </Field>
      <Field label={action === "CLASSIFY" ? "Column name pattern (required for CLASSIFY)" : "Column name pattern (optional)"}>
        <input
          value={columnNamePattern}
          onChange={(e) => setColumnNamePattern(e.target.value)}
          placeholder="e.g. %email%"
        />
      </Field>

      {action === "TAG" ? (
        <Field label="Tag key">
          <input value={tagKey} onChange={(e) => setTagKey(e.target.value)} />
        </Field>
      ) : null}
      {action === "CLASSIFY" ? (
        <Field label="Classification">
          <select value={classification} onChange={(e) => setClassification(e.target.value)}>
            {CLASSIFICATIONS.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </Field>
      ) : null}
      {action === "OWN" ? (
        <>
          <Field label="Owner type">
            <select value={ownerType} onChange={(e) => setOwnerType(e.target.value as "INDIVIDUAL" | "GROUP")}>
              <option value="INDIVIDUAL">INDIVIDUAL</option>
              <option value="GROUP">GROUP</option>
            </select>
          </Field>
          <Field label="Owner principal">
            <input value={ownerPrincipal} onChange={(e) => setOwnerPrincipal(e.target.value)} />
          </Field>
        </>
      ) : null}
      {action === "CERTIFY" ? (
        <>
          <Field label="Rationale (at least 10 characters)">
            <textarea value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </Field>
          <Field label="Expires after (days)">
            <input
              type="number"
              min={1}
              value={expiresAfterDays}
              onChange={(e) => setExpiresAfterDays(Number(e.target.value))}
            />
          </Field>
        </>
      ) : null}

      <Field label="Schedule interval (minutes, 5-10080)">
        <input
          type="number"
          min={5}
          max={10_080}
          value={scheduleMinutes}
          onChange={(e) => setScheduleMinutes(Number(e.target.value))}
        />
      </Field>
      <Field label="Auto-apply max items">
        <input
          type="number"
          min={0}
          value={autoApplyMax}
          onChange={(e) => setAutoApplyMax(Number(e.target.value))}
        />
      </Field>
      <label>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled
      </label>

      <Button
        disabled={busy || !canSubmit}
        onClick={() => void run(async () => {
          const body: PlaybookCreate = {
            name: name.trim(),
            action,
            datasource_id: datasourceId,
            match_field: matchField,
            match_pattern: matchPattern.trim(),
            column_name_pattern: columnNamePattern.trim() || null,
            action_parameters: actionParameters(),
            schedule_interval_minutes: scheduleMinutes,
            auto_apply_max_items: autoApplyMax,
            enabled,
          };
          const created = await createPlaybook(organizationId, body);
          onCreated(created);
          reset();
          setMessage(`Playbook "${created.name}" created.`);
        })}
      >
        Create playbook
      </Button>
      {message ? <p role="status">{message}</p> : null}
    </details>
  );
}

export function PlaybooksScreen() {
  const organizationId = useOrgId();
  const [playbooks, setPlaybooks] = useState<PlaybookRead[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      fetchPlaybooks(organizationId, {}, signal)
        .then((page) => {
          setPlaybooks(page.items);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setError(err instanceof ApiError ? err.message : String(err));
          setLoading(false);
        });
    },
    [organizationId],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const withBusy = async (id: string, fn: () => Promise<void>) => {
    setBusyIds((prev) => new Set(prev).add(id));
    try {
      await fn();
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const handleRun = (playbook: PlaybookRead) =>
    void withBusy(playbook.id, async () => {
      const result = await runPlaybookNow(playbook.id);
      setNotice(describeRunResult(playbook.name, result));
      setPlaybooks((prev) =>
        prev?.map((p) => (p.id === playbook.id ? { ...p, last_run_at: new Date().toISOString() } : p)) ?? prev,
      );
    });

  const handleToggle = (playbook: PlaybookRead) =>
    void withBusy(playbook.id, async () => {
      const updated = await updatePlaybook(playbook.id, { enabled: !playbook.enabled });
      setPlaybooks((prev) => prev?.map((p) => (p.id === playbook.id ? updated : p)) ?? prev);
      setNotice(`"${playbook.name}" is now ${updated.enabled ? "enabled" : "disabled"}.`);
    });

  const handleDelete = (playbook: PlaybookRead) => {
    if (!window.confirm(`Delete playbook "${playbook.name}"? This cannot be undone.`)) return;
    void withBusy(playbook.id, async () => {
      await deletePlaybook(playbook.id);
      setPlaybooks((prev) => prev?.filter((p) => p.id !== playbook.id) ?? prev);
      setNotice(`"${playbook.name}" deleted.`);
    });
  };

  const handleCreated = (playbook: PlaybookRead) => {
    setPlaybooks((prev) => (prev ? [playbook, ...prev] : [playbook]));
  };

  if (error) {
    return (
      <section className="pbk">
        <ErrorState title="Playbooks could not be loaded" detail={error} onRetry={() => load()} />
      </section>
    );
  }

  return (
    <section className="pbk">
      <header className="pbk__head">
        <div>
          <h1>Playbooks</h1>
          <p className="pbk__sub">
            Saved, scheduled bulk-metadata rules: on one datasource, wherever an object's name matches a
            pattern, apply tag, classify, own, or certify — automatically or as a governance proposal.
          </p>
        </div>
      </header>

      <CreatePlaybookForm organizationId={organizationId} onCreated={handleCreated} />

      {notice ? <p className="pbk__notice" role="status">{notice}</p> : null}

      {loading && !playbooks ? (
        <p role="status" className="pbk__loading">Loading playbooks…</p>
      ) : playbooks && playbooks.length > 0 ? (
        <ul className="pbk__list">
          {playbooks.map((playbook) => (
            <PlaybookRow
              key={playbook.id}
              playbook={playbook}
              busy={busyIds.has(playbook.id)}
              onRun={handleRun}
              onToggle={handleToggle}
              onDelete={handleDelete}
            />
          ))}
        </ul>
      ) : (
        <Empty
          title="No playbooks yet."
          hint="Create one above to automate a bulk metadata action on a schedule."
        />
      )}
    </section>
  );
}
