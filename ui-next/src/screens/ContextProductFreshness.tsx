import { useCallback, useMemo, useRef, useState } from "react";
import {
  CONTEXT_PRODUCT_COVERAGE_ROLES,
  fetchContextProductChangesSincePublished,
  fetchContextProductChangesSummary,
} from "../lib/api";
import type { ContextProductCoverageChange } from "../lib/api";
import { readDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import { Pill } from "../components/primitives";
import { failureText, useAsyncResource } from "../components/screenState";
import type { StatusChannel } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Staleness of a published version (R11-FP12): what it covers that moved after
   it was published, asked for on demand and shown on the registry row.

   TWO READINGS. A version's own definition never changes, so `ContextProductRead`
   has nothing to show: what moves is what it stands on -- a covered view's or
   routine's definition, an approved description. HOW MANY moved comes for the whole
   list in one request when the screen opens (`useChangesSincePublished`, since
   2026-09-22), and the row, the agent gateway's exposure list, Ask's picker and the
   rollout version list all show it with no click. WHICH moved is still per version,
   on request, from `contextProductCoverage` (see
   `fetchContextProductChangesSincePublished` for its cost: each such read is recorded
   as a consumption of the version), so the detail is asked for only about a row a
   person asked about.

   THREE ANSWERS, NEVER TWO. "Stale" needs a reason a steward can act on, so it
   lists what moved. "Nothing changed" is its own answer and says what was looked
   at -- a column added or removed is deliberately not counted, or every product
   would go stale whenever the source gained a column. And "could not check" is
   neither of them: a refused or failed read must not read as current, and must not
   read as stale either.

   HOW IT IS ANNOUNCED. The screen has one message strip that every action reports
   through, and it is the only live region involved: this reports each check there
   once. What sits on the row is plain text, so a screen reader is not told twice and
   a row that was checked earlier does not speak when the list scrolls.
--------------------------------------------------------------------------- */

/** Versions a consumer is served. A draft, a version in review and a rejected one have
 *  never been published, so they have no baseline to be stale against and the server
 *  would say nothing; a deprecated or retired one is no longer what anyone is given. */
export function canCheckFreshness(status: string): boolean {
  return status === "PUBLISHED" || status === "SUPPORTED";
}

export type VersionFreshness =
  | { readonly kind: "checking" }
  | {
      readonly kind: "stale";
      readonly total: number;
      readonly changes: readonly ContextProductCoverageChange[];
    }
  | { readonly kind: "unchanged" }
  | { readonly kind: "unavailable"; readonly reason: string };

const sentence = (text: string): string => `${text.trim().replace(/[.!?]+$/, "")}.`;

export function useVersionFreshness(channel: StatusChannel) {
  const [byVersion, setByVersion] = useState<Readonly<Record<string, VersionFreshness>>>({});
  // A ref, not the `checking` state: two clicks in one tick both read the state
  // out of the render that captured them, and would each issue (and record) a read.
  const inflight = useRef(new Set<string>());

  const check = useCallback(
    async (versionId: string, label: string) => {
      if (inflight.current.has(versionId)) return;
      inflight.current.add(versionId);
      const settle = (state: VersionFreshness) =>
        setByVersion((current) => ({ ...current, [versionId]: state }));
      settle({ kind: "checking" });
      channel.info(`Checking ${label} for changes since publication…`);
      try {
        const { total, changes } = await fetchContextProductChangesSincePublished(versionId);
        if (total > 0 || changes.length > 0) {
          const count = Math.max(total, changes.length);
          settle({ kind: "stale", total: count, changes });
          channel.info(
            `${label} is stale: ${count === 1 ? "1 change" : `${count} changes`} to what it covers since it was published.`,
          );
        } else {
          settle({ kind: "unchanged" });
          channel.success(`${label}: nothing it covers has changed since it was published.`);
        }
      } catch (reason) {
        const detail = failureText(reason);
        settle({ kind: "unavailable", reason: detail });
        channel.failure(`Could not check ${label} for changes since publication. ${sentence(detail)}`);
      } finally {
        inflight.current.delete(versionId);
      }
    },
    [channel],
  );

  return { byVersion, check } as const;
}

const SUBJECT_LABEL: Readonly<Record<string, string>> = {
  VIEW: "View",
  ROUTINE: "Routine",
  TABLE: "Table",
  COLUMN: "Column",
};

const words = (code: string): string => code.toLowerCase().replace(/_/g, " ");

/** One entry's why, from the server's own codes -- `change` says what kind of move and
 *  `change_class` how (see `ResolvedCoverageChange`). An unknown code is shown as
 *  itself rather than dropped: the presence of an entry *is* the product being stale. */
export function describeChange(entry: ContextProductCoverageChange): string {
  switch (entry.change) {
    case "DEFINITION_CHANGED":
      return entry.changeClass === "LITERAL_ONLY"
        ? "definition changed (literal values only)"
        : entry.changeClass === "STRUCTURAL"
          ? "definition changed (structural)"
          : "definition changed";
    case "DEPRECATED":
      return entry.changeClass === "SIGNATURE_CHANGED"
        ? "no longer in the source (replaced by a new signature)"
        : "no longer in the source";
    case "REACTIVATED":
      return "back in the source";
    case "MEANING_RETIRED":
      return entry.changeClass === "MEANING_WITHDRAWN"
        ? "approved description withdrawn (no approved text stands now)"
        : entry.changeClass === "MEANING_REPLACED"
          ? "approved description replaced"
          : "approved description retired";
    default:
      return entry.changeClass ? `${words(entry.change)} (${words(entry.changeClass)})` : words(entry.change);
  }
}

/** How many entries the row lists before it says "and N more". */
const SHOWN = 5;

/** The row's badge. Text, not colour alone; only for a version the server said is stale. */
export function FreshnessPill({ state }: { state: VersionFreshness | undefined }) {
  return state?.kind === "stale" ? <Pill tone="warn">stale</Pill> : null;
}

/** The row's explanation. Deliberately plain text with no live-region role: the message
 *  strip announces the check once, and this is what a reader finds on the row afterwards. */
export function FreshnessNote({ state, version }: { state: VersionFreshness | undefined; version: number }) {
  if (!state || state.kind === "checking") return null;
  if (state.kind === "unavailable") {
    return (
      <div className="cprow__fresh cprow__fresh--unknown">
        <p className="cprow__freshhead">
          <strong>Not checked.</strong> {sentence(state.reason)} v{version} is neither marked stale nor
          confirmed current.
        </p>
      </div>
    );
  }
  if (state.kind === "unchanged") {
    return (
      <div className="cprow__fresh cprow__fresh--ok">
        <p className="cprow__freshhead">
          <strong>Checked.</strong> No view or routine definition that v{version} covers, and no approved
          description of it, has changed since it was published. A column added or removed is not counted.
        </p>
      </div>
    );
  }
  const shown = state.changes.slice(0, SHOWN);
  const hidden = state.total - shown.length;
  return (
    <div className="cprow__fresh cprow__fresh--stale">
      <p className="cprow__freshhead">
        <strong>Stale since publication.</strong> {state.total === 1 ? "1 change" : `${state.total} changes`}{" "}
        to what v{version} covers:
      </p>
      <ul className="cprow__freshlist" aria-label="What changed since publication">
        {shown.map((entry) => (
          <li key={`${entry.subjectKind}:${entry.subjectId}:${entry.change}`}>
            {SUBJECT_LABEL[entry.subjectKind] ?? words(entry.subjectKind)} <code>{entry.subjectId}</code>
            {" — "}
            {describeChange(entry)}
          </li>
        ))}
      </ul>
      {hidden > 0 ? <p className="cprow__freshmore">and {hidden} more.</p> : null}
    </div>
  );
}

/* ---------------------------------------------------------------------------
   The passive reading: how many covered subjects moved since publication, for every
   row at once (R11-FP12, 2026-09-22). Asked only by a session known to hold the
   coverage roles (`readDecision`): the product list admits roles the coverage doors
   refuse, and a badge is not worth a 403 per screen for them.
--------------------------------------------------------------------------- */

/** Count per version id: a number for a published version (0 is "nothing moved"), `null`
 *  for one never published. A version the answer does not name is absent: no reading. */
export type ChangesSincePublished = ReadonlyMap<string, number | null>;

const NO_READING: ChangesSincePublished = new Map();

/** One request for the project -- or, with `productId`, for every version of one product --
 *  when the screen opens. A failed or refused read leaves every row without a badge, which
 *  is "no reading", never "nothing moved". */
export function useChangesSincePublished(
  projectId: string | null | undefined,
  productId?: string | null,
): { byVersion: ChangesSincePublished; error: string | null } {
  const session = useSession();
  const decision = readDecision(session, CONTEXT_PRODUCT_COVERAGE_ROLES);
  const summary = useAsyncResource(
    (signal) => fetchContextProductChangesSummary(projectId!, { productId }, signal),
    [projectId, productId],
    { enabled: Boolean(projectId) && decision === "ask" },
  );
  const byVersion = useMemo<ChangesSincePublished>(
    () =>
      summary.data
        ? new Map(summary.data.items.map((item) => [item.version_id, item.changed_subjects]))
        : NO_READING,
    [summary.data],
  );
  return { byVersion, error: summary.error };
}

/** The words for a count, or `null` when there is nothing to say (never published, nothing
 *  moved, or no reading). For a `<select>` option, where a pill cannot go. */
export function changedSincePublishedText(count: number | null | undefined): string | null {
  if (!count) return null;
  return count === 1 ? "1 change since published" : `${count} changes since published`;
}

/** The passive badge: text, not colour alone, and only when something moved. */
export function ChangedSincePublishedPill({ count }: { count: number | null | undefined }) {
  const text = changedSincePublishedText(count);
  return text ? <Pill tone="warn">{text}</Pill> : null;
}
