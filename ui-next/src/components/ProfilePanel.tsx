import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchTableProfile } from "../lib/api";
import type { ColumnProfileRead, ProfileFacetStatusRead, TableProfileRead } from "../lib/types";
import { Button, Pill } from "./primitives";
import "./ProfilePanel.css";

/* ---------------------------------------------------------------------------
   Statistical evidence, and what it did not see. R11-FP04 / FP-04's UI half.

   `GET /v1/tables/{id}/profile` has existed, been gated and been typed for a
   long time, and nothing in this app called it -- so every statistic the
   platform measures on every discovery run was invisible to the people it was
   measured for. That is the gap this panel closes.

   Three rules it encodes, each a decision:

   1. THE LIMITATION LEADS. FP-04's acceptance is that Catalog shows
      "statistical evidence and sampling limitations", and the order matters:
      "18 of 20 values are distinct" means one thing about a table that was
      read in full and something much weaker about the first thousand rows of
      ten million. So the scope line renders above the numbers, and a profile
      whose scope was never recorded says so rather than letting the numbers
      imply a scope they do not have.

   2. ABSENT IS NOT ZERO, AND THE REASON IS SHOWN. Every facet is optional
      because every one is genuinely missing for some engine, some column type
      or some profile age. A blank cell would read as "none found". So a
      missing facet renders as its reason: this engine cannot do it, this type
      has no text form, Atlas does not compute it yet, the source refused.
      Those are four different answers to "should I ask again", and collapsing
      them into one dash is how a reader stops asking.

   3. WITHHELD IS NOT MISSING. A column whose facets policy withheld renders
      with its marker and stays in the list. Dropping it would let a reader
      draw a conclusion about the data from a fact about their own entitlement.

   What is deliberately absent, and must stay absent: any value. No minimum, no
   maximum, no top values, no sample rows, no bucket edges -- the payload
   carries none, by construction (ADR-0014), and the governed sample-row half
   of FP-04 is a separately designed surface that does not exist yet.
--------------------------------------------------------------------------- */

const nf = new Intl.NumberFormat("en-US");
const pct = (n: number) => `${(n * 100).toFixed(n >= 0.999 || n === 0 ? 0 : 1)}%`;

/** A server facet code as a noun a reader recognises. */
function facetWords(facet: string): string {
  switch (facet) {
    case "DISTINCT":
      return "distinct count";
    case "LENGTH":
      return "value length";
    case "BLANKS":
      return "blank and whitespace counts";
    case "LENGTH_DISTRIBUTION":
      return "length distribution";
    case "ENTROPY":
      return "distribution entropy";
    case "PATTERN_CLASS":
      return "pattern evidence";
    case "UNITS":
      return "units";
    default:
      return facet.toLowerCase().replace(/_/g, " ");
  }
}

/** Why a facet is absent, and -- the part that matters -- whether asking again could help. */
function reasonWords(status: ProfileFacetStatusRead): string {
  switch (status.reason_code) {
    case "ENGINE_LACKS_FACET":
      return "this engine cannot compute it";
    case "TYPE_HAS_NO_TEXT_FORM":
      return "this column's type has no text form";
    case "TYPE_IS_REPEATED":
      return "this column holds a repeated value";
    case "SOURCE_DENIED_READ":
      return "the source refused the read";
    case "FACET_QUERY_FAILED":
      return "the measurement did not complete on this run";
    case "NOT_IMPLEMENTED":
      return "Atlas does not measure it on this engine yet";
    case "WOULD_CARRY_A_VALUE":
      return "it could only be stated by carrying a source value, which profiles never do";
    default:
      return status.status.toLowerCase().replace(/_/g, " ");
  }
}

/** The one sentence that qualifies every number below it. */
function scopeSentence(profile: TableProfileRead): string {
  const sampled = nf.format(profile.sampled_row_count);
  const estimate =
    profile.row_count_estimate === null || profile.row_count_estimate === undefined
      ? null
      : nf.format(profile.row_count_estimate);
  switch (profile.observation_scope) {
    case "FULL":
      return `Measured over every row: ${sampled} rows.`;
    case "SAMPLE":
      return estimate
        ? `Measured over a bounded sample of ${sampled} rows, from about ${estimate}. Every statistic below describes the sample, not the table.`
        : `Measured over a bounded sample of ${sampled} rows. The table's full size was not read, so every statistic below describes the sample, not the table.`;
    case "UNKNOWN":
      return `Measured over ${sampled} rows. The connector could not say whether that was the whole table, so treat every statistic below as a lower bound on what the table contains.`;
    default:
      return `Measured over ${sampled} rows. This profile predates observation-scope recording, so whether it saw the whole table is not known — a re-profile will say.`;
  }
}

function scopeTone(scope: TableProfileRead["observation_scope"]): "ok" | "warn" | "mute" {
  if (scope === "FULL") return "ok";
  if (scope === "SAMPLE") return "warn";
  return "mute";
}

function ColumnRow({ column }: { column: ColumnProfileRead }) {
  const rows = column.null_count + column.non_null_count;
  const nullShare = rows > 0 ? column.null_count / rows : null;
  const unavailable = column.unavailable_facets ?? [];

  return (
    <li className="prof__row">
      <div className="prof__head">
        <span className="prof__name">{column.column_name}</span>
        {column.cardinality_class ? (
          <Pill tone={column.effectively_unique ? "accent" : "mute"}>
            {column.cardinality_class.toLowerCase().replace(/_/g, " ")}
          </Pill>
        ) : null}
        <span className="prof__spacer" />
        <span className="prof__class">{column.classification}</span>
      </div>

      {column.facets_withheld ? (
        /* Rule 3: the column stays, the marker says why its facets are gone. */
        <div className="prof__withheld">
          {column.withheld_marker ?? "[withheld by policy]"}
          {column.withheld_reason_code ? ` · ${column.withheld_reason_code}` : ""}
          <div className="prof__note">
            Counts are shown; the uniqueness, distribution and length facets are withheld for
            this column's classification. Ask for a grant if you need them.
          </div>
        </div>
      ) : null}

      <dl className="prof__facts">
        <div>
          <dt>Non-null</dt>
          <dd className="tnum">{nf.format(column.non_null_count)}</dd>
        </div>
        <div>
          <dt>Null</dt>
          <dd className="tnum">
            {nf.format(column.null_count)}
            {nullShare !== null ? ` (${pct(nullShare)})` : ""}
          </dd>
        </div>
        <div>
          <dt>Distinct</dt>
          <dd className="tnum">
            {nf.format(column.approximate_distinct_count)}
            {column.distinct_ratio !== null && column.distinct_ratio !== undefined
              ? ` (${pct(column.distinct_ratio)} of non-null)`
              : ""}
          </dd>
        </div>
        {column.min_length !== null && column.max_length !== null ? (
          <div>
            <dt>Length</dt>
            <dd className="tnum">{`${nf.format(column.min_length)}–${nf.format(column.max_length)}`}</dd>
          </div>
        ) : null}
        {column.blank_count !== null && column.blank_count !== undefined ? (
          <div>
            <dt>Blank</dt>
            <dd className="tnum">
              {nf.format(column.blank_count)}
              {column.whitespace_only_count
                ? ` · ${nf.format(column.whitespace_only_count)} whitespace-only`
                : ""}
            </dd>
          </div>
        ) : null}
        {column.frequency_entropy_bits !== null && column.frequency_entropy_bits !== undefined ? (
          <div>
            <dt>Entropy</dt>
            <dd className="tnum">{`${column.frequency_entropy_bits.toFixed(2)} bits`}</dd>
          </div>
        ) : null}
      </dl>

      {column.length_bucket_counts && column.length_bucket_counts.length > 0 ? (
        /* Counts per bucket, and the scheme they are aligned to. The boundaries
           are not in the payload on purpose (ADR-0014), so the bars are
           labelled by position and named by scheme rather than by edge. */
        <div className="prof__dist">
          <div className="prof__label">Length distribution</div>
          <div className="prof__bars">
            {column.length_bucket_counts.map((count, index) => {
              const peak = Math.max(...(column.length_bucket_counts ?? [1]), 1);
              return (
                <span
                  key={index}
                  className="prof__bar"
                  style={{ height: `${Math.max(2, Math.round((count / peak) * 22))}px` }}
                  title={`Bucket ${index + 1} of ${column.length_bucket_scheme ?? "the scheme"}: ${nf.format(count)} values`}
                />
              );
            })}
          </div>
          <div className="prof__source">
            {`Shortest to longest, ${column.length_bucket_scheme ?? "scheme unnamed"} · counts only`}
          </div>
        </div>
      ) : null}

      {unavailable.length > 0 ? (
        /* Rule 2. */
        <ul className="prof__absent">
          {unavailable.map((status) => (
            <li key={`${status.facet}-${status.status}`}>
              {`No ${facetWords(status.facet)} — ${reasonWords(status)}`}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function ProfilePanel({ tableId }: { tableId: string }) {
  const [profile, setProfile] = useState<TableProfileRead | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [expanded, setExpanded] = useState(false);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const { signal } = controller;
    setError(null);
    setProfile(null);
    fetchTableProfile(tableId, signal)
      .then((row) => {
        if (!signal.aborted) setProfile(row);
      })
      .catch((e: unknown) => {
        if (signal.aborted || (e as Error)?.name === "AbortError") return;
        setError(e as Error);
      });
  }, [tableId]);

  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, [load]);

  // Collapsed on every new asset: a wide table would otherwise push the
  // evidence items the pane leads with off the screen (the same reason
  // `ColumnPanel` collapses).
  useEffect(() => {
    setExpanded(false);
  }, [tableId]);

  if (error) {
    // 404 is not a failure: most tables in a fresh estate have never been
    // profiled, and saying "no profile yet" is the true answer. Saying
    // "could not load" would send a steward looking for a fault.
    if (error instanceof ApiError && error.status === 404) {
      return (
        <div className="prof">
          <div className="prof__sub">Statistical evidence</div>
          <div className="prof__none">
            This table has not been profiled yet. Statistics appear after a discovery run
            profiles it.
          </div>
        </div>
      );
    }
    return (
      <div className="prof">
        <div className="prof__sub">Statistical evidence</div>
        <div className="prof__error" role="alert">
          {error instanceof ApiError && error.status === 403
            ? "You are not authorized to view this table's profile."
            : `The profile could not be loaded: ${
                error instanceof ApiError ? error.detail : error.message
              }`}
        </div>
        <Button onClick={() => load()}>Retry profile</Button>
      </div>
    );
  }

  if (profile === null) {
    return (
      <div className="prof">
        <div className="prof__sub">Statistical evidence</div>
        <div className="prof__load" role="status">
          Loading profile…
        </div>
      </div>
    );
  }

  const shown = expanded ? profile.columns : profile.columns.slice(0, 5);
  const uncomputed = profile.uncomputed_facets ?? [];
  const withheldCount = profile.withheld_column_count ?? 0;

  return (
    <div className="prof">
      <div className="prof__sub">
        Statistical evidence
        <span className="prof__count">
          {`${profile.columns.length} column${profile.columns.length === 1 ? "" : "s"} · ${profile.profile_version}`}
        </span>
      </div>

      {/* Rule 1: the limitation is rendered before anything it qualifies. */}
      <div className={`prof__scope prof__scope--${scopeTone(profile.observation_scope)}`}>
        <span className="prof__scopetag">
          {profile.observation_scope ?? "NOT RECORDED"}
        </span>
        <span>{scopeSentence(profile)}</span>
      </div>
      <div className="prof__source">
        {`Profiled ${new Date(profile.created_at).toLocaleString()} · no source values are stored or shown (ADR-0014)`}
      </div>

      {withheldCount > 0 ? (
        <div className="prof__notice" role="status">
          {`${withheldCount} column${withheldCount === 1 ? "" : "s"} had their facets withheld by an access policy. They are listed below with their marker, not removed.`}
        </div>
      ) : null}

      {profile.columns.length === 0 ? (
        <div className="prof__none">The profile recorded no columns.</div>
      ) : (
        <>
          <ol className="prof__list">
            {shown.map((column) => (
              <ColumnRow key={column.column_id} column={column} />
            ))}
          </ol>
          {profile.columns.length > shown.length ? (
            <button className="prof__more" onClick={() => setExpanded(true)}>
              {`Show ${profile.columns.length - shown.length} more column${
                profile.columns.length - shown.length === 1 ? "" : "s"
              }`}
            </button>
          ) : null}
        </>
      )}

      {uncomputed.length > 0 ? (
        <ul className="prof__absent prof__absent--table">
          {uncomputed.map((status) => (
            <li key={status.facet}>{`No ${facetWords(status.facet)} — ${reasonWords(status)}`}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
