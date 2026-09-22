import { useRef, useState } from "react";
import type {
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioConflict,
  StudioTestResultRead,
} from "../lib/types";
import { detectStudioConflicts, runStudioTests } from "../lib/api";
import type { StudioPublishedState } from "../lib/api";
import { Button, ConfirmDialog, Dialog, Field, Pill } from "../components/primitives";
import { FormError, useSubmitAction } from "../components/screenState";
import { parseJsonObject, showStamp, showValue } from "./studioForm";
import { ITEMS_EDITABLE_STATUS } from "./studioRoles";

/* ---------------------------------------------------------------------------
   "Run tests" and "Detect conflicts" (R11-AUD08) -- the two actions that read like
   checks and are WRITES.

   RUN TESTS (`POST .../test`, `run_tests`) is not a dry run. It moves the change set
   to TESTING, stores each item's PASSED or FAILED, runs the eval-regression gate
   against every mined question for an object the change set touches and stores that
   run, and writes audit rows. There is no way back from TESTING to DRAFT, and items
   can only be added or removed while a change set is DRAFT -- so running the tests
   on a DRAFT is what LOCKS its items, and the confirmation says so. From TESTING it
   is a re-run: nothing further is locked, so it asks nothing.

   What it COSTS is therefore not compute (the validators are pure and in-process;
   the SQL is parsed and rendered, never executed) but the transition and the audit
   trail. The result it returns is totals and a verdict; per-item outcomes are read
   back from the item list, which the screen re-fetches.

   DETECT CONFLICTS (`POST .../detect-conflicts`) compares the items with a published
   state the CALLER supplies, and records CONFLICTED or CLEAN on the change set. The
   API cannot look the published state up for itself, and it does not refuse an empty
   one -- it treats "nothing published" literally, so every UPDATE reads as NOT_FOUND
   and every DELETE as ALREADY_DELETED. This dialog therefore asks for the state,
   lists the exact keys the server will look each item up by, and warns when leaving
   it empty would manufacture conflicts. The docstring on the route says an omitted
   state means "no conflicts"; `detect_conflicts` (`studio.py`) does not do that for
   UPDATE and DELETE items, and the code is what runs.
--------------------------------------------------------------------------- */

/** A `Record<string, unknown>` as a definition list, keys and values as the API sent them. */
export function EvidenceList({ evidence }: { evidence: Record<string, unknown> }) {
  const entries = Object.entries(evidence);
  if (entries.length === 0) return null;
  return (
    <dl className="cs__kv">
      {entries.map(([key, value]) => (
        <div key={key} className="cs__kv__row">
          <dt>{key}</dt>
          <dd>{showValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The verdict and totals `POST .../test` returned -- and what it did not. */
export function TestResultView({ result }: { result: StudioTestResultRead }) {
  return (
    <section className="cs__result" aria-label="Test run">
      <div className="evp__sub">Test run</div>
      <div className="cs__result__head" role="status">
        <Pill tone={result.passed ? "ok" : "bad"}>{result.passed ? "passed" : "failed"}</Pill>
        <span className="cs__result__when">
          started {showStamp(result.started_at)} · completed {showStamp(result.completed_at)}
        </span>
      </div>
      <EvidenceList evidence={result.evidence} />
      {result.passed ? null : (
        <p className="cs__none">
          The API returns totals for a test run, not the reason an item failed. Each item&rsquo;s status is listed above; a
          TOOL or CONTEXT_PRODUCT item can be checked to see its contract errors, and a failed eval question shows its
          reasons in the eval run.
        </p>
      )}
    </section>
  );
}

/** What `POST .../detect-conflicts` found, and what it was compared with. */
export function ConflictsView({
  conflicts,
  suppliedKeys,
}: {
  conflicts: readonly StudioConflict[];
  /** How many published snapshots the caller supplied -- 0 means it compared with nothing. */
  suppliedKeys: number;
}) {
  const against =
    suppliedKeys > 0
      ? `${suppliedKeys} published snapshot${suppliedKeys === 1 ? "" : "s"} you supplied`
      : "an empty published state (none was supplied)";
  return (
    <section className="cs__result" aria-label="Conflicts">
      <div className="evp__sub">Conflicts ({conflicts.length})</div>
      {conflicts.length === 0 ? (
        <p className="cs__none" role="status">
          No conflicts found, compared with {against}.
        </p>
      ) : (
        <>
          <p className="cs__none" role="status">
            {conflicts.length} conflict{conflicts.length === 1 ? "" : "s"} found, compared with {against}.
          </p>
          <ul className="cs__conflicts">
            {conflicts.map((conflict, index) => (
              <li key={`${conflict.object_type}:${conflict.object_id}:${conflict.field_name}:${index}`}>
                <div className="cs__conflicts__obj">
                  {conflict.object_type} · {conflict.object_id}
                </div>
                <div className="cs__conflicts__field">
                  field <code>{conflict.field_name}</code>
                </div>
                <div className="cs__conflicts__vals">
                  <span>
                    change set: <code>{showValue(conflict.change_set_value)}</code>
                  </span>
                  <span>
                    published: <code>{showValue(conflict.current_value)}</code>
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** `POST .../test`, behind a confirmation whose wording depends on what it will lock. */
export function RunTestsDialog({
  changeSet,
  onClose,
  onDone,
}: {
  changeSet: StudioChangeSetRead;
  onClose: () => void;
  onDone: (result: StudioTestResultRead) => void;
}) {
  const run = useSubmitAction<StudioTestResultRead>();
  const locks = changeSet.status === ITEMS_EDITABLE_STATUS;
  const confirm = async () => {
    const result = await run.run(() => runStudioTests(changeSet.id));
    if (result) onDone(result);
  };
  return (
    <ConfirmDialog
      title="Run the tests?"
      description={
        "Runs each item's checks and the eval-regression gate for the objects this change set touches, and records the outcome in the audit ledger." +
        (locks
          ? " It also moves the change set to TESTING, and from then on items can no longer be added or removed."
          : " The change set is already TESTING, so this re-runs the checks and replaces each item's status.")
      }
      confirmLabel="Run tests"
      destructive={locks}
      busy={run.submitting}
      error={run.error}
      onConfirm={() => void confirm()}
      onCancel={onClose}
    />
  );
}

/**
 * `POST .../detect-conflicts`: the published state, and the result.
 *
 * The keys listed are the ones `detect_conflicts` builds -- `OBJECT_TYPE:object_id` --
 * so an author can write the JSON without guessing the format.
 */
export function DetectConflictsDialog({
  changeSet,
  items,
  onClose,
  onDone,
}: {
  changeSet: StudioChangeSetRead;
  items: readonly StudioChangeItemRead[];
  onClose: () => void;
  onDone: (conflicts: StudioConflict[], suppliedKeys: number) => void;
}) {
  const [text, setText] = useState("");
  const detect = useSubmitAction<StudioConflict[]>();
  const stateRef = useRef<HTMLTextAreaElement>(null); // open on the field the author has to fill in
  const needsState = items.some((item) => item.operation !== "CREATE");

  const submit = async () => {
    const parsed = parseJsonObject(text, "Published state");
    if (!parsed.ok) return detect.fail(parsed.error);
    const state = parsed.value as StudioPublishedState | null;
    const conflicts = await detect.run(() => detectStudioConflicts(changeSet.id, state));
    if (conflicts) onDone(conflicts, state ? Object.keys(state).length : 0);
  };

  return (
    <Dialog
      title="Detect conflicts"
      description={`Compares the items in “${changeSet.name}” with the published state you supply and records the result on the change set.`}
      onClose={onClose}
      dismissOnBackdrop={false}
      initialFocusRef={stateRef}
      className="dlg--wide"
      footer={
        <>
          <Button onClick={onClose} disabled={detect.submitting}>
            Cancel
          </Button>
          <Button variant="primary" disabled={detect.submitting} onClick={() => void submit()}>
            {detect.submitting ? "Detecting…" : "Detect conflicts"}
          </Button>
        </>
      }
    >
      {items.length > 0 ? (
        <>
          <p className="dlg__hint">The published snapshot for each item is looked up by this key:</p>
          <ul className="csform__keys">
            {items.map((item) => (
              <li key={item.id}>
                <code>{`${item.object_type}:${item.object_id}`}</code> ({item.operation.toLowerCase()})
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="dlg__hint">This change set has no items, so there is nothing to compare.</p>
      )}
      <Field label="Published state (JSON, optional)">
        <textarea
          ref={stateRef}
          className="csform__json"
          rows={8}
          spellCheck={false}
          placeholder={'{ "METRIC:metric:revenue": { "filter": "status != \'void\'" } }'}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      </Field>
      {needsState ? (
        <p className="dlg__hint">
          Without a published snapshot for its key, an UPDATE item is reported as NOT_FOUND and a DELETE item as
          ALREADY_DELETED, and the change set is recorded as conflicted. Leave the state empty only when every item is a
          CREATE.
        </p>
      ) : null}
      {detect.error ? <FormError detail={detect.error} /> : null}
    </Dialog>
  );
}
