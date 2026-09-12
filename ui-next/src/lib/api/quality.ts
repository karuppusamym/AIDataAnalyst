/* ---------------------------------------------------------------------------
   Quality — the per-datasource summary, the incident list, one incident's
   triage evidence, and the governed transition that closes it.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { USE_FIXTURES } from "../appConfig";
import { demoOr, get, postJson, putJson } from "./transport";
import type {
  DataQualityIncidentRead,
  DataQualityIncidentTransition,
  DataQualityIncidentTriageRead,
  DataQualitySummaryRead,
  FreshnessConfigRead,
  FreshnessConfigUpsert,
  FreshnessStatusRead,
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

/* ---------------------------------------------------------------------------
   DQ-2 freshness watermark contracts (R11-B8), `QualityScreen`.

   All four routes already existed in `quality_api.py` and no screen called
   any of them, so a watermark contract could not be created, could not be
   approved, and therefore never left PENDING_APPROVAL -- which is why every
   table reported AWAITING_APPROVAL forever and the scheduled evaluation this
   row adds would have had nothing approved to evaluate.

   NO DEMO ARM. `demoOr` needs a fixture generator per call and
   `lib/fixtures.ts` has none for freshness; inventing one would mean the demo
   estate showing an approval flow whose 403 is the whole point of the
   feature. These four use the inline `USE_FIXTURES` branch the transport's
   own docstring reserves for exactly this ("the demo arm refuses"), so a demo
   build renders the panel's empty state instead of a fake contract.
--------------------------------------------------------------------------- */

/** `GET /v1/datasources/{id}/freshness` (`quality_api.py::list_freshness_configs`)
 *  — every watermark contract on a datasource, approved or not. Paged; the
 *  screen reads one page, because the evaluated state of each contract costs
 *  a call of its own below. */
export async function fetchFreshnessConfigs(
  datasourceId: string,
  query: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<FreshnessConfigRead>> {
  if (USE_FIXTURES) return { items: [], limit: query.limit ?? 100, offset: 0, total: 0 };
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<FreshnessConfigRead>>(
    `/v1/datasources/${datasourceId}/freshness?${params}`,
    signal,
  );
}

/** `GET /v1/datasources/{id}/freshness/{table_id}` (`quality_api.py::get_freshness_status`)
 *  — the evaluated state of ONE table: FRESH / STALE / AWAITING_APPROVAL /
 *  NOT_CONFIGURED, from the real data watermark.
 *
 *  ADR-0016, worth restating at the call site: this is not scan age. The
 *  summary tile beside it (`metadata_scan_status`) is when Atlas last looked;
 *  this is when the data itself last moved. Presenting the first as the
 *  second is the invariant that ADR forbids. */
export async function fetchFreshnessStatus(
  datasourceId: string,
  tableId: string,
  signal?: AbortSignal,
): Promise<FreshnessStatusRead> {
  if (USE_FIXTURES) {
    return {
      table_id: tableId,
      status: "NOT_CONFIGURED",
      last_watermark: null,
      age_minutes: null,
      threshold_minutes: null,
      evidence: { reason: "demo data mode does not model freshness observations" },
    };
  }
  return get<FreshnessStatusRead>(
    `/v1/datasources/${datasourceId}/freshness/${tableId}`,
    signal,
  );
}

/** `PUT /v1/datasources/{id}/freshness-config/{table_id}` (`quality_api.py::
 *  upsert_freshness_config`) — the MAKER half. Creating or editing a contract
 *  always leaves it PENDING_APPROVAL: an edit to an already-approved contract
 *  resets its approval, by design, so a threshold cannot be quietly relaxed
 *  by the person it would excuse. */
export async function upsertFreshnessConfig(
  datasourceId: string,
  tableId: string,
  body: FreshnessConfigUpsert,
  signal?: AbortSignal,
): Promise<FreshnessConfigRead> {
  if (USE_FIXTURES) throw new Error("Demo data mode cannot save a freshness contract.");
  return putJson<FreshnessConfigRead>(
    `/v1/datasources/${datasourceId}/freshness-config/${tableId}`,
    body,
    signal,
  );
}

/** `POST /v1/datasources/{id}/freshness-config/{table_id}/approve`
 *  (`quality_api.py::approve_freshness_config`) — the CHECKER half, and the
 *  step that actually activates freshness evaluation for a table.
 *
 *  Refuses with 403 when the approver is the contract's own author, and 409
 *  when it is not PENDING_APPROVAL. Both are surfaced, never hidden: the
 *  caller shows the server's own detail, because "you cannot approve your own
 *  contract" is the rule working, not an error to swallow. */
export async function approveFreshnessConfig(
  datasourceId: string,
  tableId: string,
  signal?: AbortSignal,
): Promise<FreshnessConfigRead> {
  if (USE_FIXTURES) throw new Error("Demo data mode cannot approve a freshness contract.");
  return postJson<FreshnessConfigRead>(
    `/v1/datasources/${datasourceId}/freshness-config/${tableId}/approve`,
    {},
    signal,
  );
}
