/* ---------------------------------------------------------------------------
   Scheduler passes (R11-VAL04) -- the fleet scheduler's last outcome for each
   maintenance pass, as the leading replica persists it once per iteration. A
   failing pass no longer stops the scheduler; this is how an operator sees one
   that fails on every iteration without reading its logs.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import type { SchedulerPassStatusListRead } from "../types";

/** `GET /v1/operations/scheduler-passes` (`operational_api.py`). Platform-wide, for
 *  PlatformAdmin and Operations. Demo mode reports no passes rather than inventing some. */
export function fetchSchedulerPasses(signal?: AbortSignal): Promise<SchedulerPassStatusListRead> {
  return demoOr(
    async () => ({
      generated_at: new Date().toISOString(),
      stale_after_seconds: 300,
      failing: 0,
      stale: 0,
      never_run: 0,
      items: [],
    }),
    async () => get<SchedulerPassStatusListRead>("/v1/operations/scheduler-passes", signal),
  );
}
