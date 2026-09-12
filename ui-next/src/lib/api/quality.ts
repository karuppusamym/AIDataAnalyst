/* ---------------------------------------------------------------------------
   Quality — the per-datasource summary, the incident list, one incident's
   triage evidence, and the governed transition that closes it.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import type {
  DataQualityIncidentRead,
  DataQualityIncidentTransition,
  DataQualityIncidentTriageRead,
  DataQualitySummaryRead,
} from "../types";
import type { PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   Quality — UX-15/UX-16, `QualityScreen`.

   Both real, already-merged routes (`quality_api.py`), gated by `USE_FIXTURES`
   the same way as every other call in this client. `list_quality_incidents`
   and `quality_summary` are scoped per datasource, matching UX-20's
   `fetchLineageImpact` (`./lineage.ts`) rather than `fetchCatalogRows`'s
   organization scoping.
--------------------------------------------------------------------------- */

export interface QualityIncidentsQuery {
  /** `null`/omitted means "every status" — the endpoint's own default when
   *  `status` is left off the query string entirely (unlike
   *  `fetchReviewQueue`'s explicit-empty-string convention, this endpoint has
   *  no server-side default status to override, so simply omitting the param
   *  is the correct "all statuses" request here). */
  status?: string | null;
  severity?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{id}/quality-summary` (`quality_api.py::quality_summary`)
 *  — the dashboard tiles: observed/table counts, open/critical incident
 *  counts, the datasource's rolled-up average quality score, and the
 *  metadata-scan freshness state. */
export function fetchQualitySummary(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<DataQualitySummaryRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureQualitySummary(datasourceId),
    async () => {
      return get<DataQualitySummaryRead>(
        `/v1/datasources/${datasourceId}/quality-summary`,
        signal,
      );
    },
  );
}

/** `GET /v1/datasources/{id}/quality-incidents` (`quality_api.py::list_quality_incidents`)
 *  — the primary incidents list, filterable by `status`/`severity`. */
export function fetchQualityIncidents(
  datasourceId: string,
  query: QualityIncidentsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<DataQualityIncidentRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureQualityIncidents(datasourceId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      if (query.severity) params.set("severity", query.severity);
      params.set("limit", String(query.limit ?? 200));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<DataQualityIncidentRead>>(
        `/v1/datasources/${datasourceId}/quality-incidents?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/quality-incidents/{id}/transition` (`quality_api.py::transition_quality_incident`)
 *  — acknowledge or resolve an open incident. The endpoint requires a
 *  non-empty (>=3 char) `reason` on both transitions and refuses (409) to
 *  transition an incident that is already RESOLVED. */
export function transitionQualityIncident(
  incidentId: string,
  body: DataQualityIncidentTransition,
  signal?: AbortSignal,
): Promise<DataQualityIncidentRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureTransitionQualityIncident(incidentId, body),
    async () => {
      return postJson<DataQualityIncidentRead>(
        `/v1/quality-incidents/${incidentId}/transition`,
        body,
        signal,
      );
    },
  );
}

/** `GET /v1/quality-incidents/{id}/triage` (`quality_api.py::
 *  get_quality_incident_triage`, `dq_triage_agent.suggest_triage`) -- a
 *  deterministic root-cause hint for one incident. Read-only, computed
 *  fresh every call, nothing persisted. */
export function fetchQualityIncidentTriage(
  incidentId: string,
  signal?: AbortSignal,
): Promise<DataQualityIncidentTriageRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureQualityIncidentTriage(incidentId),
    async () => {
      return get<DataQualityIncidentTriageRead>(`/v1/quality-incidents/${incidentId}/triage`, signal);
    },
  );
}
