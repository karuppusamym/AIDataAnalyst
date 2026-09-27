/* ---------------------------------------------------------------------------
   Definition history (R11-FP03) -- every captured definition of one routine,
   newest first, and never the body.

   `GET /v1/routines/{id}/definition-history` is the read half of a table that
   has been written since 2026-09-15 with nothing able to read it. The server
   decides everything substantive: what the screening gate releases, which
   versions' footprints it derived, and what changed between them. This module
   carries the request and classifies the refusal, and computes nothing of its
   own -- a client that re-derived "did this change" from two digests would be a
   second opinion on a question the server already answered.

   Not re-exported from `lib/api.ts`. Its one consumer imports it by path, the
   way `./footprintGaps` is imported, so the barrel does not grow a name that
   only one surface uses.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import { ApiError } from "../http";
import type { RoutineDefinitionHistoryRead } from "../types";

/** What a failed history read means for the surface asking.
 *
 *  `NOT_CAPTURED` is the one worth spelling out: a routine with no captured
 *  definition answers 200 with an empty list, so a 404 here means the routine
 *  itself is gone -- which is a different sentence from "nothing was captured",
 *  and the pane must not print the wrong one. Modelled on
 *  `catalog.classifyDescriptionDraftError`'s discriminated kinds rather than a
 *  status number, so the copy lives next to the case instead of inside a chain
 *  of comparisons in the component. */
export type DefinitionHistoryErrorKind =
  | "ROUTINE_NOT_FOUND"
  | "UNAUTHORIZED"
  | "SERVER_ERROR"
  | "UNKNOWN";

export interface DefinitionHistoryError {
  kind: DefinitionHistoryErrorKind;
  status: number;
  detail: string;
}

export function classifyDefinitionHistoryError(error: unknown): DefinitionHistoryError {
  if (!(error instanceof ApiError)) {
    return { kind: "UNKNOWN", status: 0, detail: (error as Error)?.message ?? "" };
  }
  const { status, detail } = error;
  if (status === 404) return { kind: "ROUTINE_NOT_FOUND", status, detail };
  if (status === 401 || status === 403) return { kind: "UNAUTHORIZED", status, detail };
  if (status >= 500) return { kind: "SERVER_ERROR", status, detail };
  return { kind: "UNKNOWN", status, detail };
}

/** `GET /v1/routines/{routine_id}/definition-history` (`definition_history_api.py`).
 *
 *  Demo mode reports an empty history rather than inventing versions: a made-up
 *  timeline of a procedure's changes is the one fixture that would be read as
 *  evidence. */
export function fetchRoutineDefinitionHistory(
  routineId: string,
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<RoutineDefinitionHistoryRead> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  return demoOr(
    async () => ({
      routine_id: routineId,
      routine_qualified_name: routineId,
      routine_type: "PROCEDURE",
      signature: "()",
      status: "ACTIVE",
      dialect: "postgres",
      footprint_basis: "REPARSED_STORED_DEFINITION",
      footprint_parse_budget: 0,
      versions: [],
      limit: options.limit ?? 20,
      offset: options.offset ?? 0,
      total: 0,
    }),
    async () =>
      get<RoutineDefinitionHistoryRead>(
        `/v1/routines/${encodeURIComponent(routineId)}/definition-history${
          query ? `?${query}` : ""
        }`,
        signal,
      ),
  );
}
