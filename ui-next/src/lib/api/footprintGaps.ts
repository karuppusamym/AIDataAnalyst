/* ---------------------------------------------------------------------------
   Footprint gaps (R11-FP05/FP17) -- what Atlas does not yet know about each
   source the caller may read, why, and who can close it. A source the caller
   may not read is left out by the server, not counted.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import type { FootprintGapsRead } from "../types";

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
