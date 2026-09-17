import { useCallback, useEffect, useRef, useState } from "react";
import {
  classifyDefinitionHistoryError,
  fetchRoutineDefinitionHistory,
} from "../lib/api/definitionHistory";
import type {
  RoutineDefinitionHistoryRead,
  RoutineDefinitionVersionRead,
} from "../lib/types";
import { AsyncState, Pill } from "./primitives";
import type { Tone } from "./primitives";
import "./DefinitionHistoryPanel.css";

/* ---------------------------------------------------------------------------
   What changed in this procedure, and when. R11-FP03's UI half.

   `metadata_routine_definition_version` has appended a row per capture and per
   moved definition since 2026-09-15 and nothing could read it -- so a steward
   looking at a routine whose body Atlas cannot release, or whose description a
   reviewer refused as `DEFINITION_MOVED`, had no way to find out what moved.
   This panel is that read, rendered as a timeline.

   Four rules it encodes, each one a decision and three of them borrowed
   wholesale from `ProfilePanel`, because this panel is the same kind of thing:
   a derived claim that is worthless without its limitation.

   1. THE LIMITATION LEADS. The read and write sets are derived on read by
      re-parsing each stored definition; they are not the reviewed lineage
      edges, and they do not follow calls into other routines. "This procedure
      started writing a second table" means one thing as a reviewed fact and
      something weaker as a parse nobody has approved, so the basis line renders
      above everything it qualifies.

   2. ABSENT IS NOT UNCHANGED, AND THE REASON IS SHOWN. A version whose
      footprint was not derived -- withheld, never captured, or past the
      server's parse budget -- says which. An empty "what changed" line on such
      a row would read as "this capture touched the same tables", which is a
      claim nobody made.

   3. WITHHELD IS NOT MISSING. A version the screening gate withholds keeps its
      row, with the server's marker and reason. Dropping it would let a reader
      draw a conclusion about the data from a fact about their own entitlement.

   4. THE BODY IS NOT HERE, AND THE PANEL SAYS SO. Not as an omission a reader
      has to notice: the header states it, because a steward who assumes the
      text is one click away stops looking for the answer this panel does give.

   What is deliberately absent and must stay absent: any definition text, any
   excerpt of one, and any control that would request one. There is no "show
   SQL" affordance to add later -- the route does not serve it.
--------------------------------------------------------------------------- */

/** Long digests are noise in a timeline; the first seven characters are enough
 *  to see that two versions differ, which is all the pair is for. */
const shortDigest = (digest: string | null) => (digest === null ? "—" : digest.slice(0, 7));

const dayOf = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

function changeClassWords(version: RoutineDefinitionVersionRead): string {
  if (version.change_class === null) return "first capture";
  if (version.change_class === "LITERAL_ONLY") return "literals only";
  if (version.change_class === "STRUCTURAL") return "structural change";
  return version.change_class.toLowerCase().replace(/_/g, " ");
}

function changeClassTone(version: RoutineDefinitionVersionRead): Tone {
  if (version.change_class === "STRUCTURAL") return "warn";
  if (version.change_class === null) return "info";
  return "mute";
}

/** The one sentence that qualifies every table name below it. */
function basisSentence(basis: string): string {
  if (basis === "REPARSED_STORED_DEFINITION") {
    return (
      "The read and write tables below are derived when you open this panel, by re-parsing each " +
      "stored, value-free definition. They are not the reviewed lineage edges, and they do not " +
      "follow calls into other routines — so treat them as what this procedure's own statements " +
      "say, not as approved lineage."
    );
  }
  return `Derived on a basis this screen does not recognise (${basis}); treat the tables below as unqualified.`;
}

/** Why a version's stored definition may not be read, and -- the part that
 *  matters -- who could change that. */
function withheldWords(code: string | null): string {
  switch (code) {
    case "ROUTINE_BODY_UNAVAILABLE":
      return "the source did not give Atlas this body; a wider source grant would";
    case "ROUTINE_BODY_QUARANTINED":
      return "prompt-risk screening quarantined it, so nothing is derived from it";
    case "ROUTINE_BODY_NOT_STORED":
      return "no value-free form of the text could be stored, so none was kept";
    case "ROUTINE_BODY_MISSING":
      return "the capture recorded the body as available and stored none";
    default:
      return code === null ? "" : code.toLowerCase().replace(/_/g, " ");
  }
}

/** What the table lists on this row are, and are not. Rule 2: every state other
 *  than a plain derivation says why it is not one. */
function footprintWords(version: RoutineDefinitionVersionRead): string | null {
  switch (version.footprint_state) {
    case "COMPUTED":
      return null;
    case "UNCHANGED_LITERALS_ONLY":
      return "Only literal values moved, so the stored definition — and the tables below — are unchanged from the version before.";
    case "COMPUTED_NO_BASELINE":
      return "The version before this one was not derived, so what this capture changed is not known — only what it reads and writes.";
    case "NOT_COMPUTED":
      return "Past this request's parse budget, so nothing was derived here. Read fewer versions at a time to see it.";
    case "WITHHELD":
      return `Nothing derived: ${withheldWords(version.withheld_reason_code)}.`;
    case "UNAVAILABLE":
      return `No definition to derive from: ${withheldWords(version.withheld_reason_code)}.`;
    default:
      return `Derivation state ${version.footprint_state}, which this screen does not recognise.`;
  }
}

/** The steward-facing sentence: what this capture changed, in tables.
 *
 *  Built only from what the server reported as added or removed. A version with
 *  a derived footprint and nothing in those lists genuinely changed no table,
 *  and says so; a version with no footprint says nothing here at all, because
 *  its `footprintWords` line is the honest answer instead. */
function changeSentences(version: RoutineDefinitionVersionRead): string[] {
  const lines: string[] = [];
  if (version.writes_added.length > 0) {
    lines.push(`Started writing ${version.writes_added.join(", ")}`);
  }
  if (version.writes_removed.length > 0) {
    lines.push(`Stopped writing ${version.writes_removed.join(", ")}`);
  }
  if (version.reads_added.length > 0) {
    lines.push(`Started reading ${version.reads_added.join(", ")}`);
  }
  if (version.reads_removed.length > 0) {
    lines.push(`Stopped reading ${version.reads_removed.join(", ")}`);
  }
  return lines;
}

function VersionRow({ version }: { version: RoutineDefinitionVersionRead }) {
  const changes = changeSentences(version);
  const footprint = footprintWords(version);
  const derived =
    version.footprint_state === "COMPUTED" ||
    version.footprint_state === "COMPUTED_NO_BASELINE" ||
    version.footprint_state === "UNCHANGED_LITERALS_ONLY";

  return (
    <li className="dhist__row">
      <div className="dhist__head">
        <span className="dhist__when">{dayOf(version.captured_at)}</span>
        <span className="dhist__ver">{`v${version.version_number}`}</span>
        <Pill tone={changeClassTone(version)}>{changeClassWords(version)}</Pill>
        {version.truncated ? <Pill tone="warn">truncated by source</Pill> : null}
        {version.analysis_run_id === null ? <Pill tone="mute">run not recorded</Pill> : null}
      </div>

      {/* Rule 3: the row stays, and the marker stands where the definition
          would have been. The *reason* is said once, on the state line below --
          saying it here as well read as two separate findings about one row. */}
      {version.withheld_marker !== null ? (
        <div className="dhist__withheld">
          {version.withheld_marker}
          {version.unavailable_reason !== null ? (
            <div className="dhist__note">{`The source said: ${version.unavailable_reason}`}</div>
          ) : null}
        </div>
      ) : null}

      {changes.length > 0 ? (
        <ul className="dhist__changes">
          {changes.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : derived && version.change_class !== null ? (
        <p className="dhist__same">No change to the tables this procedure reads or writes.</p>
      ) : null}

      {footprint ? <p className="dhist__state">{footprint}</p> : null}

      {derived ? (
        <dl className="dhist__facts">
          <div>
            <dt>Writes</dt>
            <dd>{version.writes_table_names.length > 0 ? version.writes_table_names.join(", ") : "none"}</dd>
          </div>
          <div>
            <dt>Reads</dt>
            <dd>{version.reads_table_names.length > 0 ? version.reads_table_names.join(", ") : "none"}</dd>
          </div>
        </dl>
      ) : null}

      {version.parse_completed === false ? (
        <p className="dhist__partial">
          {`Not every statement in this definition could be read${
            version.unparsed_reason_codes.length > 0
              ? ` (${version.unparsed_reason_codes.join(", ")})`
              : ""
          }, so the tables above are what was understood, not everything it touches.`}
        </p>
      ) : null}

      <div className="dhist__digest">
        {`definition ${shortDigest(version.definition_digest)}`}
        {version.change_class === null
          ? ""
          : ` · previous ${shortDigest(version.previous_definition_digest)}`}
      </div>
    </li>
  );
}

export function DefinitionHistoryPanel({
  routineId,
  qualifiedName,
}: {
  routineId: string;
  /** What the surface already calls this routine, shown while the read is in
   *  flight so the disclosure is not anonymous. Cosmetic: the read resolves by
   *  `routineId` alone. */
  qualifiedName?: string;
}) {
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState<RoutineDefinitionHistoryRead | null>(null);
  const [error, setError] = useState<unknown>(null);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const { signal } = controller;
    setError(null);
    setHistory(null);
    fetchRoutineDefinitionHistory(routineId, {}, signal)
      .then((row) => {
        if (!signal.aborted) setHistory(row);
      })
      .catch((reason: unknown) => {
        if (signal.aborted || (reason as Error)?.name === "AbortError") return;
        setError(reason);
      });
  }, [routineId]);

  // Fetched when asked for rather than with the surface around it (the
  // `OperationsFootprintGaps` rule): most rows are read without anyone needing
  // the history, and every expansion is a parse on the server.
  useEffect(() => {
    if (!open) return;
    load();
    return () => request.current?.abort();
  }, [open, load]);

  // Collapsed again on a new routine: a stale expansion would show one
  // routine's history under another's name for as long as the read takes.
  useEffect(() => {
    setOpen(false);
    setHistory(null);
    setError(null);
  }, [routineId]);

  const classified = error === null ? null : classifyDefinitionHistoryError(error);

  return (
    <div className="dhist">
      <button
        type="button"
        className="dhist__toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "Hide definition history" : "Definition history"}
      </button>

      {open ? (
        <div className="dhist__body">
          {/* Rule 4, said before anything else: a reader who assumes the text is
              one click away stops looking for the answer this panel does give. */}
          <p className="dhist__never">
            The definition text itself is never served by this read, for any role. What follows is
            when each version was captured, how it was classified, its digest, and the tables its
            own statements touch.
          </p>

          <AsyncState
            loading={history === null && classified === null}
            error={error === null ? undefined : error}
            subject={`the definition history of ${qualifiedName ?? "this routine"}`}
            loadingLabel="Loading definition history…"
            errorTitle={
              classified?.kind === "ROUTINE_NOT_FOUND"
                ? "This routine no longer exists"
                : "The definition history could not be loaded"
            }
            onRetry={load}
          >
            {history === null ? null : history.total === 0 ? (
              /* Not an error, and not "no changes": nothing was ever captured.
                 A package member's body belongs to its package, which is the
                 ordinary way to arrive here. */
              <p className="dhist__none">
                No definition has been captured for this routine. A package member's body belongs
                to its package, and a routine whose body the source has never released has nothing
                to keep a history of.
              </p>
            ) : (
              <>
                {/* Rule 1: the limitation renders before anything it qualifies. */}
                <p className="dhist__basis">{basisSentence(history.footprint_basis)}</p>
                <div className="dhist__meta">
                  {`${history.total} version${history.total === 1 ? "" : "s"} captured · parsed as ${history.dialect}`}
                </div>
                <ol className="dhist__list">
                  {history.versions.map((version) => (
                    <VersionRow key={version.version_id} version={version} />
                  ))}
                </ol>
                {history.total > history.versions.length ? (
                  <p className="dhist__more">
                    {`Showing the ${history.versions.length} most recent of ${history.total}. The older captures are on the route's later pages.`}
                  </p>
                ) : null}
              </>
            )}
          </AsyncState>
        </div>
      ) : null}
    </div>
  );
}
