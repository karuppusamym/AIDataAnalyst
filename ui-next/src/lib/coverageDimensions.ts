/* ---------------------------------------------------------------------------
   What each stewardship-coverage dimension COUNTS (R11-AUD08).

   The API returns a bare key -- `quality_monitored`, `semantically_mapped` -- and
   a percentage, and a percentage of an undefined thing is not information: a
   steward reading "certified 33%" has to know whether an expired certification
   counts, and whether a certified column makes its table certified. The
   sentences below are read off `_coverage` and `active_certified_table_ids` in
   `src/aida/stewardship_api.py` and `stewardship_service.py`, one per dimension,
   and are the only text in this client that says what the API measures. If the
   server's rule changes, this is the file to change with it.

   Order is the server's (`COVERAGE_DIMENSIONS`). A key the server adds later is
   still shown -- `dimensionLabel` humanizes it -- it just carries no definition
   until someone writes one here.
--------------------------------------------------------------------------- */

export interface CoverageDimensionInfo {
  readonly key: string;
  readonly label: string;
  /** What the server counts as covered, for a table. */
  readonly counts: string;
}

export const COVERAGE_DIMENSION_INFO: readonly CoverageDimensionInfo[] = [
  {
    key: "documented",
    label: "Documented",
    counts: "The table has an approved description.",
  },
  {
    key: "owned",
    label: "Owned",
    counts:
      "The table has an active owner: an ownership assignment, or approved documentation that names one.",
  },
  {
    key: "classified",
    label: "Classified",
    counts: "At least one of the table's columns has a classification other than UNCLASSIFIED.",
  },
  {
    key: "certified",
    label: "Certified",
    counts:
      "The table's own certification is active and has not expired. A certified column does not make its table certified.",
  },
  {
    key: "quality_monitored",
    label: "Quality monitored",
    counts:
      "An enabled data-quality policy covers the table, directly or as a policy for its whole datasource.",
  },
  {
    key: "semantically_mapped",
    label: "Semantically mapped",
    counts: "The table has a business annotation.",
  },
];

const BY_KEY = new Map(COVERAGE_DIMENSION_INFO.map((info) => [info.key, info]));

/** The label for a dimension key, humanized when the server has added one this client does not know. */
export function dimensionLabel(key: string): string {
  const known = BY_KEY.get(key);
  if (known) return known.label;
  const words = key.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function dimensionCounts(key: string): string | null {
  return BY_KEY.get(key)?.counts ?? null;
}

/**
 * The API's dimensions in the order a scorecard reads them: the server's own
 * six first, then anything it added since, in the order it sent them.
 */
export function orderedDimensionKeys(keys: readonly string[]): string[] {
  const known = COVERAGE_DIMENSION_INFO.map((info) => info.key).filter((key) => keys.includes(key));
  return [...known, ...keys.filter((key) => !BY_KEY.has(key))];
}
