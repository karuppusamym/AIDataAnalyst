import { useEffect, useId, useState } from "react";
import { ApiError } from "../lib/http";
import {
  fetchOkfImportReview,
  type OkfImportReview,
  type OkfImportReviewChange,
  type OkfImportReviewDocument,
} from "../lib/api/okfImport";
import { Button, Pill, type Tone } from "./primitives";
import "./OkfImportReviewPreview.css";

/* ---------------------------------------------------------------------------
   R11-OKF03 — what a reviewer reads before deciding an OKF import.

   An import turns edits made in a downloaded bundle into pending proposals.
   By the time a reviewer opens one, the file may be days old, so the one
   question the generic diff cannot answer is the one that matters most: will
   approving publish what I am reading, or has Atlas moved on since the file
   was exported? This preview answers it change by change, grouped per
   document (a table with its columns, or a routine), with the text before and
   after side by side and the approval's own verdict in words
   (`approval_effect`, composed server-side from the comparisons the approval
   itself makes -- so this screen never restates the rule and cannot drift
   from it).

   It gates the decision the way `ReviewChangePreview` does: `onReady(id)` only
   once a page has loaded for exactly this review, `onReady(null)` whenever the
   preview is not readable. Decisions stay blocked until then.

   Keyboard: every control is a native radio or button in document order; the
   page range is announced politely when it changes. Nothing is conveyed by
   colour alone -- each state is also a word.
--------------------------------------------------------------------------- */

/** Documents per page: small enough that a page is a reading unit. */
export const OKF_REVIEW_PAGE_SIZE = 25;

const STATE_LABEL: Record<string, string> = {
  APPLIES: "applies",
  CONFLICT: "conflict",
  TARGET_UNAVAILABLE: "target unavailable",
  DECIDED: "decided",
};

const STATE_TONE: Record<string, Tone> = {
  APPLIES: "ok",
  CONFLICT: "warn",
  TARGET_UNAVAILABLE: "bad",
  DECIDED: "mute",
};

const NEEDS_ATTENTION = new Set(["CONFLICT", "TARGET_UNAVAILABLE"]);

function fieldLabel(change: OkfImportReviewChange): string {
  if (change.field.startsWith("column:")) return `Column ${change.field.slice("column:".length)}`;
  if (change.subject_type === "ROUTINE") return "Routine purpose";
  return "Purpose";
}

function versionLabel(version: number | null): string {
  return version === null ? "no approved text" : `v${version}`;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const details = error.details ?? {};
    const reason = typeof details.reason_code === "string" ? details.reason_code : null;
    const said = typeof details.detail === "string" ? details.detail : error.detail;
    return reason ? `${said} (${reason})` : said;
  }
  return (error as Error)?.message ?? "The preview could not be loaded.";
}

function Text({ value, empty }: { value: string | null; empty: string }) {
  return value === null ? (
    <p className="okfrv__text okfrv__text--none">{empty}</p>
  ) : (
    <p className="okfrv__text">{value}</p>
  );
}

function Change({ change }: { change: OkfImportReviewChange }) {
  const headingId = useId();
  const decided = change.state === "DECIDED";
  /* A null "before" means one of two things, and they are not the same to a
     reviewer: the field was empty when exported, or screening withholds the
     text it held. The expected version says which. */
  const beforeEmpty =
    change.expected_version === null
      ? "No approved text when the file was exported."
      : "Withheld by export screening.";
  return (
    <li className={`okfrv__chg okfrv__chg--${change.state.toLowerCase()}`}>
      <article aria-labelledby={headingId}>
        <header className="okfrv__chghead">
          <h5 id={headingId} className="okfrv__chgh">
            {fieldLabel(change)}
          </h5>
          <Pill tone={STATE_TONE[change.state] ?? "mute"}>
            {STATE_LABEL[change.state] ?? change.state.toLowerCase()}
          </Pill>
          {decided ? <Pill tone="info">{change.status.toLowerCase().replace(/_/g, " ")}</Pill> : null}
          {change.target_active === false && !decided ? (
            <Pill tone="warn">target not active</Pill>
          ) : null}
        </header>
        <div className="okfrv__texts">
          <figure className="okfrv__fig">
            <figcaption className="okfrv__cap">
              Before · approved {versionLabel(change.expected_version)} when exported
            </figcaption>
            <Text value={change.before_value} empty={beforeEmpty} />
          </figure>
          <figure className="okfrv__fig okfrv__fig--after">
            <figcaption className="okfrv__cap">Proposed</figcaption>
            <Text value={change.proposed_value} empty="Withheld by export screening." />
          </figure>
        </div>
        {change.state === "CONFLICT" ? (
          <figure className="okfrv__fig okfrv__fig--now">
            <figcaption className="okfrv__cap">
              Approved now · {versionLabel(change.current_version)}
            </figcaption>
            <Text
              value={change.current_value}
              empty={
                change.current_version === null
                  ? "No approved text now."
                  : "The current text is not shown here (unchanged, or withheld by export screening)."
              }
            />
          </figure>
        ) : null}
        <p className="okfrv__effect">{change.approval_effect}</p>
        {change.skip_reason ? <p className="okfrv__skip">{change.skip_reason}</p> : null}
      </article>
    </li>
  );
}

function Document({
  document,
  onlyAttention,
}: {
  document: OkfImportReviewDocument;
  onlyAttention: boolean;
}) {
  const headingId = useId();
  const changes = onlyAttention
    ? document.changes.filter((change) => NEEDS_ATTENTION.has(change.state))
    : document.changes;
  if (changes.length === 0) return null;
  return (
    <li className="okfrv__doc">
      <section aria-labelledby={headingId}>
        <header className="okfrv__dochead">
          <h4 id={headingId} className="okfrv__doch">
            {document.label}
          </h4>
          <span className="okfrv__kind">{document.object_type.toLowerCase().replace(/_/g, " ")}</span>
          {document.conflicts > 0 ? (
            <Pill tone="warn">
              {document.conflicts} {document.conflicts === 1 ? "needs" : "need"} attention
            </Pill>
          ) : null}
        </header>
        <ul className="okfrv__changes">
          {changes.map((change) => (
            <Change key={change.change_id} change={change} />
          ))}
        </ul>
      </section>
    </li>
  );
}

export function OkfImportReviewPreview({
  reviewId,
  onReady,
}: {
  reviewId: string;
  onReady: (id: string | null) => void;
}) {
  const [data, setData] = useState<OkfImportReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [onlyAttention, setOnlyAttention] = useState(false);
  const filterName = useId();

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    onReady(null);
    fetchOkfImportReview(
      reviewId,
      { offset: page * OKF_REVIEW_PAGE_SIZE, limit: OKF_REVIEW_PAGE_SIZE },
      controller.signal,
    )
      .then((value) => {
        if (controller.signal.aborted) return;
        // A response for another review must never unlock this one's decision.
        if (value.review_id !== reviewId) {
          throw new Error("The preview returned belongs to a different review.");
        }
        setData(value);
        onReady(reviewId);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(e));
        onReady(null);
      });
    return () => controller.abort();
  }, [reviewId, page, attempt, onReady]);

  if (error) {
    return (
      <div className="okfrv__err" role="alert">
        <p>The import preview could not be loaded: {error}</p>
        <Button onClick={() => setAttempt((value) => value + 1)}>Retry import preview</Button>
      </div>
    );
  }
  if (!data) return <p role="status">Loading import preview…</p>;

  const counts = data.counts;
  const attention = (counts.conflicts ?? 0) + (counts.target_unavailable ?? 0);
  const first = data.total_documents === 0 ? 0 : data.offset + 1;
  const last = Math.min(data.offset + data.limit, data.total_documents);
  const shown = data.documents.filter(
    (document) =>
      !onlyAttention || document.changes.some((change) => NEEDS_ATTENTION.has(change.state)),
  );

  return (
    <div className="okfrv">
      <p className="okfrv__authority">{data.authority}</p>
      <dl className="okfrv__facts">
        <div>
          <dt>Proposal</dt>
          <dd>{data.proposal_status.toLowerCase().replace(/_/g, " ")}</dd>
        </div>
        <div>
          <dt>Imported by</dt>
          <dd>{data.requested_by}</dd>
        </div>
        {data.filename ? (
          <div>
            <dt>File</dt>
            <dd>{data.filename}</dd>
          </div>
        ) : null}
        {data.archive_sha256 ? (
          <div>
            <dt>Archive</dt>
            <dd className="okfrv__digest" title={data.archive_sha256}>
              sha256 {data.archive_sha256.slice(0, 12)}…
            </dd>
          </div>
        ) : null}
      </dl>
      <ul className="okfrv__counts" aria-label="What approving does">
        <li>
          <b className="tnum">{counts.changes ?? 0}</b> changes in{" "}
          <b className="tnum">{counts.documents ?? 0}</b>{" "}
          {counts.documents === 1 ? "document" : "documents"}
        </li>
        <li>
          <b className="tnum">{counts.applies ?? 0}</b> apply
        </li>
        <li>
          <b className="tnum">{counts.conflicts ?? 0}</b> in conflict
        </li>
        <li>
          <b className="tnum">{counts.target_unavailable ?? 0}</b> target unavailable
        </li>
        {counts.decided ? (
          <li>
            <b className="tnum">{counts.decided}</b> already decided
          </li>
        ) : null}
      </ul>
      {attention > 0 ? (
        <p className="okfrv__warn">
          {attention} {attention === 1 ? "change" : "changes"} will not be published as proposed:
          the approved text or its target moved after the file was exported. Each is marked below
          with what approving does instead.
        </p>
      ) : null}
      <fieldset className="okfrv__filter">
        <legend>Show</legend>
        <label>
          <input
            type="radio"
            name={filterName}
            checked={!onlyAttention}
            onChange={() => setOnlyAttention(false)}
          />{" "}
          All changes
        </label>
        <label>
          <input
            type="radio"
            name={filterName}
            checked={onlyAttention}
            onChange={() => setOnlyAttention(true)}
          />{" "}
          Only changes that need attention
        </label>
      </fieldset>
      {shown.length === 0 ? (
        <p className="okfrv__none">
          {onlyAttention
            ? "Nothing on this page needs attention: every change applies as proposed."
            : "This page holds no documents."}
        </p>
      ) : (
        <ol className="okfrv__docs" aria-label="Documents in this import">
          {shown.map((document) => (
            <Document key={document.document_id} document={document} onlyAttention={onlyAttention} />
          ))}
        </ol>
      )}
      {data.total_documents > data.limit ? (
        <nav className="okfrv__pages" aria-label="Import document pages">
          <Button disabled={page === 0} onClick={() => setPage((value) => value - 1)}>
            Previous documents
          </Button>
          <span aria-live="polite">
            Documents {first}–{last} of {data.total_documents}
          </span>
          <Button
            disabled={data.offset + data.limit >= data.total_documents}
            onClick={() => setPage((value) => value + 1)}
          >
            Next documents
          </Button>
        </nav>
      ) : null}
    </div>
  );
}
