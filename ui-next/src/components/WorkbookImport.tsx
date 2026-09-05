import { useCallback, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import {
  fetchModelImportChanges,
  setModelImportExclusion,
  submitModelImport,
  uploadModelWorkbook,
  type ModelImportBatchRead,
  type ModelImportChangeRead,
} from "../lib/_column_documentation_api";
import { Button, Pill } from "./primitives";
import type { Tone } from "./primitives";
import "./WorkbookImport.css";

/* ---------------------------------------------------------------------------
   Re-import an edited model workbook.

   The three steps are separate on purpose, and this component's whole job is
   to keep them that way in front of the user:

     1. Choose a file      -> parses and diffs. Publishes NOTHING.
     2. Read the diff      -> including the rows that did not work.
     3. Submit for review  -> someone else decides.

   Two rules this encodes:

   1. The preview leads with what went wrong. Rejected rows render first and
      with their reason, because a steward whose upload half-failed needs to
      see that before the count of what succeeded -- an import UI that shows
      "312 changes ready" above "88 rows could not be matched" is telling the
      truth in the wrong order.
   2. Nothing here publishes. The submit button says "for review", and the
      component never claims a change is live; the server would refuse a
      self-approval anyway, but the UI should not imply one is possible.
   3. A wrong row can be dropped without rejecting the file. A reviewer decides
      the batch as one thing -- that is what batching is for -- so the release
      valve sits on the uploader's side of the review boundary: rows can be
      excluded while the batch is DRAFT, and are frozen the moment it is
      submitted. Excluding is not deciding; an excluded row is withdrawn before
      anyone was asked to look at it.
--------------------------------------------------------------------------- */

const STATUS_TONE: Record<string, Tone> = {
  PENDING: "info",
  APPLIED: "ok",
  SKIPPED_STALE: "warn",
  SKIPPED_MISSING: "warn",
  REJECTED: "bad",
  EXCLUDED: "mute",
};

const STATUS_LABEL: Record<string, string> = {
  PENDING: "ready",
  EXCLUDED: "excluded",
  APPLIED: "applied",
  SKIPPED_STALE: "superseded",
  SKIPPED_MISSING: "gone",
  REJECTED: "not applied",
};

function truncate(value: string, limit = 160): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function ChangeRow({
  change,
  editable,
  onToggle,
}: {
  change: ModelImportChangeRead;
  /** Only a DRAFT batch can be trimmed; after submit the set is fixed. */
  editable: boolean;
  onToggle: (change: ModelImportChangeRead) => void;
}) {
  const excluded = change.status === "EXCLUDED";
  const failed =
    change.status !== "PENDING" && change.status !== "APPLIED" && !excluded;
  return (
    <li
      className={`wbi__row${failed ? " wbi__row--bad" : ""}${
        excluded ? " wbi__row--excluded" : ""
      }`}
    >
      <div className="wbi__rowhead">
        {editable && (change.status === "PENDING" || excluded) ? (
          <input
            type="checkbox"
            className="wbi__include"
            checked={!excluded}
            aria-label={`Include ${change.subject_label} ${change.field}`}
            onChange={() => onToggle(change)}
          />
        ) : null}
        <span className="wbi__subject" title={change.subject_label}>
          {change.subject_label}
        </span>
        <span className="wbi__field">{change.field}</span>
        <span className="wbi__spacer" />
        <span className="wbi__where">
          {change.sheet_name} row {change.row_number}
        </span>
        <Pill tone={STATUS_TONE[change.status] ?? "mute"}>
          {STATUS_LABEL[change.status] ?? change.status.toLowerCase()}
        </Pill>
      </div>
      {change.skip_reason ? (
        <div className="wbi__reason">{change.skip_reason}</div>
      ) : (
        <div className="wbi__values">
          {change.old_value ? (
            <div className="wbi__old" title={change.old_value}>
              {truncate(change.old_value)}
            </div>
          ) : (
            <div className="wbi__old wbi__old--empty">(no current value)</div>
          )}
          <div className="wbi__new" title={change.new_value}>
            {truncate(change.new_value)}
          </div>
        </div>
      )}
    </li>
  );
}

export function WorkbookImport({ datasourceId }: { datasourceId: string }) {
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [batch, setBatch] = useState<ModelImportBatchRead | null>(null);
  const [changes, setChanges] = useState<ModelImportChangeRead[]>([]);
  const [busy, setBusy] = useState<"upload" | "submit" | "exclude" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggleInclusion = useCallback(
    async (change: ModelImportChangeRead) => {
      if (!batch || batch.status !== "DRAFT") return;
      const excluded = change.status !== "EXCLUDED";
      setBusy("exclude");
      setError(null);
      // Optimistic: the row flips immediately, and the server's updated
      // change_count replaces the batch below. A failure re-reads the truth
      // rather than leaving the checkbox lying.
      setChanges((prev) =>
        prev.map((c) =>
          c.id === change.id ? { ...c, status: excluded ? "EXCLUDED" : "PENDING" } : c,
        ),
      );
      try {
        setBatch(await setModelImportExclusion(batch.id, [change.id], excluded));
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
        try {
          setChanges(await fetchModelImportChanges(batch.id));
        } catch {
          /* the error above is the one worth showing */
        }
      } finally {
        setBusy(null);
      }
    },
    [batch],
  );

  const reset = useCallback(() => {
    setBatch(null);
    setChanges([]);
    setError(null);
    if (fileInput.current) fileInput.current.value = "";
  }, []);

  const onFile = useCallback(
    async (file: File) => {
      setBusy("upload");
      setError(null);
      setBatch(null);
      setChanges([]);
      try {
        const uploaded = await uploadModelWorkbook(datasourceId, file);
        setBatch(uploaded);
        setChanges(await fetchModelImportChanges(uploaded.id));
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [datasourceId],
  );

  const onSubmit = useCallback(async () => {
    if (!batch) return;
    setBusy("submit");
    setError(null);
    try {
      setBatch(await submitModelImport(batch.id));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusy(null);
    }
  }, [batch]);

  // Failures first: see rule 1 in the header comment.
  const ordered = [...changes].sort((a, b) => {
    const aBad = a.status === "REJECTED" ? 0 : 1;
    const bBad = b.status === "REJECTED" ? 0 : 1;
    if (aBad !== bBad) return aBad - bBad;
    return a.row_number - b.row_number;
  });

  return (
    <div className="wbi">
      <div className="wbi__sub">
        Import edited workbook
        {batch ? (
          <button className="wbi__reset" onClick={reset}>
            Start over
          </button>
        ) : null}
      </div>

      {batch === null ? (
        <>
          <p className="wbi__lede">
            Upload a workbook exported from this source. It is checked against the
            current model and nothing is published until a reviewer approves it.
          </p>
          <input
            ref={fileInput}
            className="wbi__file"
            type="file"
            accept=".xlsx"
            aria-label="Edited model workbook"
            disabled={busy !== null}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onFile(file);
            }}
          />
          {busy === "upload" ? (
            <div className="wbi__load" role="status">
              Checking the workbook…
            </div>
          ) : null}
        </>
      ) : (
        <>
          <div className="wbi__counts">
            <span className="wbi__file-name" title={batch.filename}>
              {batch.filename}
            </span>
            {batch.rejected_row_count > 0 ? (
              <Pill tone="bad">{batch.rejected_row_count} rows not applied</Pill>
            ) : null}
            <Pill tone={batch.change_count > 0 ? "info" : "mute"}>
              {batch.change_count} {batch.change_count === 1 ? "change" : "changes"}
            </Pill>
            {changes.some((c) => c.status === "EXCLUDED") ? (
              <Pill tone="mute">
                {changes.filter((c) => c.status === "EXCLUDED").length} excluded
              </Pill>
            ) : null}
            {batch.status === "APPLIED" ? (
              <Pill tone="ok">{batch.applied_count} applied</Pill>
            ) : null}
            {batch.status === "APPLIED" && batch.skipped_count > 0 ? (
              <Pill tone="warn">{batch.skipped_count} skipped</Pill>
            ) : null}
          </div>

          {batch.status === "DRAFT" && batch.change_count === 0 ? (
            <div className="wbi__none">
              This workbook matches the current model — there is nothing to submit.
            </div>
          ) : null}

          {ordered.length > 0 ? (
            <ol className="wbi__list">
              {ordered.map((change) => (
                <ChangeRow
                  key={change.id}
                  change={change}
                  editable={batch.status === "DRAFT" && busy === null}
                  onToggle={(c) => void toggleInclusion(c)}
                />
              ))}
            </ol>
          ) : null}

          {batch.status === "PENDING_REVIEW" ? (
            <div className="wbi__queued" role="status">
              Submitted. It is in the review queue now, and someone other than you has
              to approve it before anything is published.
            </div>
          ) : null}

          {batch.status === "DRAFT" && batch.change_count > 0 ? (
            <Button variant="primary" disabled={busy !== null} onClick={() => void onSubmit()}>
              {busy === "submit"
                ? "Submitting…"
                : `Submit ${batch.change_count} ${
                    batch.change_count === 1 ? "change" : "changes"
                  } for review`}
            </Button>
          ) : null}
        </>
      )}

      {error ? (
        <div className="wbi__error" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}
