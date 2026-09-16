/* ---------------------------------------------------------------------------
   Footprint gaps (R11-FP05/FP17) -- what Atlas does not yet know about each
   source the caller may read, why, and who can close it. A source the caller
   may not read is left out by the server, not counted.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import type { FootprintGapDetailRead, FootprintGapsRead } from "../types";

/** `GET /v1/organizations/{organization_id}/footprint-gaps` (`footprint_gaps_api.py`).
 *  Demo mode reports no gaps rather than inventing some. */
export function fetchFootprintGaps(
  organizationId: string,
  signal?: AbortSignal,
): Promise<FootprintGapsRead> {
  return demoOr(
    async () => ({
      organization_id: organizationId,
      generated_at: new Date().toISOString(),
      datasources: [],
      totals: {},
    }),
    async () => get<FootprintGapsRead>(`/v1/organizations/${organizationId}/footprint-gaps`, signal),
  );
}

/** `GET /v1/datasources/{datasource_id}/footprint-gaps/{kind}` (`footprint_gaps_api.py`) --
 *  the objects behind one source's count of one kind, so a steward can act on the gap rather
 *  than go looking for it. Bounded by the server; `truncated` says when there are more.
 *  Demo mode reports none, matching the summary above. */
export function fetchFootprintGapObjects(
  datasourceId: string,
  kind: string,
  signal?: AbortSignal,
): Promise<FootprintGapDetailRead> {
  return demoOr(
    async () => ({
      datasource_id: datasourceId,
      kind,
      resolution: "SOURCE_ACCESS",
      owner: "source administrator",
      explanation: "",
      objects: [],
      truncated: false,
      note: null,
    }),
    async () =>
      get<FootprintGapDetailRead>(
        `/v1/datasources/${datasourceId}/footprint-gaps/${encodeURIComponent(kind)}`,
        signal,
      ),
  );
}
