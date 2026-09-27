import { useCallback, useState } from "react";
import { ApiError } from "../lib/http";
import { runPlaybookAsPreviewed, storePlaybookDryRun } from "../lib/api/playbookDryRuns";
import type { PlaybookBoundRun, PlaybookStoredDryRun } from "../lib/api/playbookDryRuns";
import type { PlaybookRead, PlaybookRunResultRead } from "../lib/types";
import { VirtualList } from "./VirtualList";
import { Button, Pill } from "./primitives";
import type { Tone } from "./primitives";
import "./PlaybookDryRunPanel.css";

/* ---------------------------------------------------------------------------
   R11-REV01 — a playbook's dry run, and a run bound to it.

   Self-contained on purpose: Playbooks is mounted in more than one place (its
   own screen and the Stewardship workspace's Automation view), and this panel
   needs nothing from either but the playbook.

   What a steward reads, per action, before anything is applied:

     - what the run would do with the matched set, by the run's own rule:
       apply it with no human (0 < matched <= auto-apply max), queue it for
       maker-checker review, or nothing -- never "playbooks are human-only";
     - how this action is undone in each case: the compensating operation when
       reviewed, and plainly none when applied automatically (no before-image);
     - that no model is consulted;
     - each matched object's before -> after, and whether the matcher truncated.

   "Run as previewed" then binds the run to that stored preview: the server
   re-evaluates, and refuses to run if the rule, the matched objects or any
   object's state moved -- saying how many were added, removed or changed.
--------------------------------------------------------------------------- */

const DISPOSITION: Record<string, { tone: Tone; text: (preview: PlaybookStoredDryRun) => string }> = {
  AUTOMATIC: {
    tone: "warn",
    text: (p) =>
      `Would apply to all ${p.matched_count} automatically, with no human review ` +
      `(${p.matched_count} is within the auto-apply limit of ${p.auto_apply_max_items}).`,
  },
  HUMAN_REVIEW: {
    tone: "info",
    text: (p) =>
      `Would queue one ${p.automation.reviewed_operation_type} operation over ` +
      `${p.matched_count} object(s) for maker-checker review.`,
  },
  NO_MATCHES: { tone: "mute", text: () => "Would do nothing: no object matches the rule." },
};

const REASONS: Record<string, string> = {
  RULE_CHANGED: "the rule was edited",
  MATCH_SET_CHANGED: "different objects match",
  EVIDENCE_CHANGED: "matched objects changed",
  RUN_DIVERGED_FROM_PREVIEW: "the objects changed while it ran",
  PLAYBOOK_DRY_RUN_ALREADY_BOUND: "This preview has already been used for a run. Preview again.",
  PLAYBOOK_DRY_RUN_NOT_FOUND: "This preview no longer exists. Preview again.",
};

function describeFailure(error: unknown): string {
  if (error instanceof ApiError) return REASONS[error.detail] ?? error.detail;
  return (error as Error).message;
}

function short(value: string): string {
  return value.slice(0, 12);
}

function changeTone(change: string): Tone {
  if (change === "NO_CHANGE") return "mute";
  if (change === "CREATE") return "ok";
  return "warn";
}

function AutomationFacts({ preview }: { preview: PlaybookStoredDryRun }) {
  const automation = preview.automation;
  return (
    <ul className="pdr__facts">
      <li>
        {automation.automatic_branch_enabled
          ? `Automatic apply is on for this playbook: up to ${preview.auto_apply_max_items} matched object(s) apply with no review, as ${automation.automatic_principal}.`
          : "Automatic apply is off for this playbook (limit 0): every run with matches goes to review."}
      </li>
      <li>
        If reviewed: applied as {automation.reviewed_operation_type}, undone by{" "}
        {automation.compensating_operation_when_reviewed}.
      </li>
      <li>
        If applied automatically:{" "}
        {automation.compensating_operation_when_automatic
          ? `undone by ${automation.compensating_operation_when_automatic}.`
          : "no governed reversal -- no before-image is recorded."}
      </li>
      <li>{automation.involves_model ? "A model is consulted." : "No model is consulted: the rule is applied as written."}</li>
    </ul>
  );
}

function Report({ preview }: { preview: PlaybookStoredDryRun }) {
  const disposition = DISPOSITION[preview.predicted_disposition];
  const truncated = preview.tables_truncated || preview.columns_truncated;
  return (
    <div className="pdr__report">
      <p className="pdr__headline">
        <Pill tone={disposition?.tone ?? "mute"}>{preview.predicted_disposition}</Pill>{" "}
        {disposition ? disposition.text(preview) : preview.predicted_disposition}
      </p>
      {truncated ? (
        <p className="pdr__warn" role="note">
          The matcher stopped at {preview.matched_count} {preview.columns_truncated ? "columns" : "tables"}: a run acts on
          these, not on everything the pattern names.
        </p>
      ) : null}
      <AutomationFacts preview={preview} />
      <p className="pdr__counts">
        {Object.entries(preview.change_counts).map(([change, count]) => (
          <span key={change}>
            <b className="tnum">{count}</b> {change.toLowerCase().replace(/_/g, " ")}
          </span>
        ))}
        <span className="pdr__version">
          rule version <code>{short(preview.rule_version)}</code> · preview <code>{short(preview.dry_run_id)}</code>
        </span>
      </p>
      {preview.items.length > 0 ? (
        <div className="pdr__items">
          <VirtualList
            items={preview.items}
            getKey={(item) => item.subject_id}
            renderItem={(item) => (
              <div className="pdr__item">
                <code className="pdr__name">{item.qualified_name}</code>
                <span className="pdr__change">
                  {item.current_value ?? "(none)"} → {item.proposed_value ?? "(none)"}
                </span>
                <Pill tone={changeTone(item.change)}>{item.change}</Pill>
              </div>
            )}
            estimateSize={40}
            ariaLabel="Objects this run would act on"
          />
        </div>
      ) : null}
    </div>
  );
}

function BindingOutcome({ bound }: { bound: PlaybookBoundRun }) {
  const binding = bound.binding;
  if (bound.ran && bound.run) {
    return (
      <p className="pdr__outcome" role="status">
        <Pill tone="ok">{binding.status === "MATCHES" ? "Ran as previewed" : "Ran, differing from the preview"}</Pill>{" "}
        {bound.run.matched_count} object(s): {bound.run.outcome.toLowerCase().replace(/_/g, " ")}.
      </p>
    );
  }
  const reasons = binding.reasons.map((code) => REASONS[code] ?? code).join("; ");
  return (
    <p className="pdr__outcome pdr__outcome--refused" role="alert">
      Not run, nothing applied: {reasons || "the preview no longer matches"}.{" "}
      <span className="tnum">{binding.added_count}</span> added,{" "}
      <span className="tnum">{binding.removed_count}</span> removed,{" "}
      <span className="tnum">{binding.changed_count}</span> changed since the preview. Preview again to see what a
      run would do now.
    </p>
  );
}

export function PlaybookDryRunPanel({
  playbook,
  onRan,
}: {
  playbook: PlaybookRead;
  onRan?: (result: PlaybookRunResultRead) => void;
}) {
  const [preview, setPreview] = useState<PlaybookStoredDryRun | null>(null);
  const [bound, setBound] = useState<PlaybookBoundRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runPreview = useCallback(async () => {
    setBusy(true);
    setError(null);
    setBound(null);
    try {
      setPreview(await storePlaybookDryRun(playbook.id));
    } catch (e) {
      setError(describeFailure(e));
    } finally {
      setBusy(false);
    }
  }, [playbook.id]);

  const runAsPreviewed = useCallback(async () => {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const result = await runPlaybookAsPreviewed(playbook.id, preview.dry_run_id, { require_match: true });
      setBound(result);
      if (result.ran && result.run) onRan?.(result.run);
    } catch (e) {
      setError(describeFailure(e));
    } finally {
      setBusy(false);
    }
  }, [onRan, playbook.id, preview]);

  const used = bound !== null;
  return (
    <section className="pdr" aria-label={`Dry run of ${playbook.name}`}>
      <div className="pdr__bar">
        <Button disabled={busy} onClick={() => void runPreview()}>
          {preview ? "Preview again" : "Preview (dry run)"}
        </Button>
        {preview ? (
          <Button
            variant="primary"
            disabled={busy || used || !playbook.enabled}
            title={playbook.enabled ? undefined : "Enable this playbook to run it"}
            onClick={() => void runAsPreviewed()}
          >
            Run as previewed
          </Button>
        ) : null}
      </div>
      {error ? (
        <p className="pdr__err" role="alert">
          {error}
        </p>
      ) : null}
      {preview ? (
        <Report preview={preview} />
      ) : (
        <p className="pdr__hint">A dry run applies nothing and queues nothing; it shows what a run would do now.</p>
      )}
      {bound ? <BindingOutcome bound={bound} /> : null}
    </section>
  );
}
