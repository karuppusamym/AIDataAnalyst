/* ---------------------------------------------------------------------------
   Operations — the fleet's runtime posture and the schedules that act on it.

   Org-wide operational reads (fleet summary, analysis runs, the outbox and
   its requeue, ingestion batches); reliability (SLO definitions and budgets,
   notification rules, archive/WORM posture, data-contract evaluation and
   violations); and playbooks, the saved bulk-metadata automation.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { deleteRequest, demoOr, get, patchJson, postJson } from "./transport";
import type {
  AnalysisRunCreate,
  AnalysisRunRead,
  ArchiveStatusRead,
  EvaluationResponse,
  FleetSummaryRead,
  MetadataIngestionBatchRead,
  NotificationRuleCreate,
  NotificationRuleRead,
  OutboxEventRead,
  PlaybookCreate,
  PlaybookRead,
  PlaybookRunResultRead,
  PlaybookUpdate,
  SlaStatusResponse,
  SloBudgetRead,
  SloDefinitionCreate,
  SloDefinitionRead,
} from "../types";
import type { PageOf, ViolationRead } from "../ui-types";

/* ---------------------------------------------------------------------------
   UX-16: Operations. Composed from four org-wide, already-merged
   `operational_api.py` routes -- fleet-summary, analysis-runs, outbox-events
   and its requeue action -- plus, as an optional per-datasource drill-down,
   `ingestion_api.py`'s metadata-ingestion-batches. There is no single
   endpoint that aggregates ingestion-batch/Temporal-workflow status across
   every datasource in an org; see `OperationsScreen.tsx`'s own module
   comment for why this screen does not fake one.
--------------------------------------------------------------------------- */

/** `GET /v1/organizations/{organization_id}/fleet-summary` (`operational_api.py::fleet_summary`)
 *  -- the dashboard tiles at the top of the Operations screen. Org-wide, no
 *  datasource picker needed. */
export function fetchFleetSummary(
  organizationId: string,
  signal?: AbortSignal,
): Promise<FleetSummaryRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureFleetSummary(organizationId),
    async () => {
      return get<FleetSummaryRead>(`/v1/organizations/${organizationId}/fleet-summary`, signal);
    },
  );
}

export interface AnalysisRunsQuery {
  organizationId: string;
  runStatus?: string | null;
  datasourceId?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/analysis-runs`
 *  (`operational_api.py::list_organization_analysis_runs`) -- the screen's
 *  primary list, filterable by run status and/or datasource. */
export function fetchAnalysisRuns(
  query: AnalysisRunsQuery,
  signal?: AbortSignal,
): Promise<PageOf<AnalysisRunRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureAnalysisRuns(query),
    async () => {
      const params = new URLSearchParams();
      if (query.runStatus) params.set("run_status", query.runStatus);
      if (query.datasourceId) params.set("datasource_id", query.datasourceId);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<AnalysisRunRead>>(
        `/v1/organizations/${query.organizationId}/analysis-runs?${params}`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   T15 — the two scan routes a first-source setup needs.

   The org-wide `analysis-runs` list above answers "what is the fleet doing";
   neither of these did exist in this client, because no screen had ever asked
   "has THIS datasource been scanned, and how did that scan end". Setup
   readiness cannot be derived from the org-wide list: it is capped and
   ordered across every source, so an empty page there is not evidence that a
   particular source has never been scanned.
--------------------------------------------------------------------------- */

/** `GET /v1/datasources/{datasource_id}/analysis-runs` (`api.py::list_analysis_runs`)
 *  — this datasource's own run history, newest first. The run row carries the
 *  outcome (`status`) AND what the run found (`discovered_tables`,
 *  `created_objects`), which is what makes "the scan succeeded and returned
 *  nothing" distinguishable from "the scan failed". */
export function fetchDatasourceAnalysisRuns(
  datasourceId: string,
  query: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<AnalysisRunRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDatasourceAnalysisRuns(datasourceId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 20));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<AnalysisRunRead>>(
        `/v1/datasources/${datasourceId}/analysis-runs?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/datasources/{datasource_id}/analysis-runs` (`api.py::create_analysis_run`,
 *  202) — reserve and submit a scan. Returns the QUEUED run; the workflow is
 *  submitted after the commit, so a 202 is an accepted request, not a
 *  completed scan, and the caller must read the run back to learn its
 *  outcome. Roles: PlatformAdmin / MetadataAdmin / DataAdmin — a principal
 *  without one gets a 403 the caller has to show as a missing permission
 *  rather than as a failed scan. */
export function createAnalysisRun(
  datasourceId: string,
  body: AnalysisRunCreate = {},
  signal?: AbortSignal,
): Promise<AnalysisRunRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateAnalysisRun(datasourceId, body),
    async () => {
      return postJson<AnalysisRunRead>(
        `/v1/datasources/${datasourceId}/analysis-runs`,
        body,
        signal,
      );
    },
  );
}

export interface OutboxEventsQuery {
  organizationId: string;
  status?: string | null;
  eventType?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/outbox-events`
 *  (`operational_api.py::list_outbox_events`) -- the event-backlog / dead-
 *  letter panel beneath the analysis-runs list. */
export function fetchOutboxEvents(
  query: OutboxEventsQuery,
  signal?: AbortSignal,
): Promise<PageOf<OutboxEventRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureOutboxEvents(query),
    async () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      if (query.eventType) params.set("event_type", query.eventType);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<OutboxEventRead>>(
        `/v1/organizations/${query.organizationId}/outbox-events?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/outbox-events/{event_id}/requeue` (`operational_api.py::requeue_outbox_event`)
 *  -- moves one DEAD_LETTER event back to PENDING with a reset attempt count.
 *  The route takes no request body; `{}` matches this file's own convention
 *  (see `submitStudioChangeSet`) of never sending an optional-looking empty
 *  POST without an explicit body. */
export function requeueOutboxEvent(
  eventId: string,
  signal?: AbortSignal,
): Promise<OutboxEventRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRequeueOutboxEvent(eventId),
    async () => {
      return postJson<OutboxEventRead>(`/v1/outbox-events/${eventId}/requeue`, {}, signal);
    },
  );
}

export interface IngestionBatchesQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{datasource_id}/metadata-ingestion-batches`
 *  (`ingestion_api.py::list_metadata_ingestion_batches`) -- per-datasource
 *  only, no org-wide equivalent exists. Used by this screen's secondary
 *  drill-down panel, one datasource at a time, never fanned out across the
 *  fleet. */
export function fetchIngestionBatches(
  datasourceId: string,
  opts: IngestionBatchesQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<MetadataIngestionBatchRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureIngestionBatches(datasourceId, opts),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(opts.limit ?? 100));
      params.set("offset", String(opts.offset ?? 0));
      return get<PageOf<MetadataIngestionBatchRead>>(
        `/v1/datasources/${datasourceId}/metadata-ingestion-batches?${params}`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   Reliability -- SLOs, notification rules, archive/WORM evidence posture,
   and runtime data-contract evaluation. Ports the legacy portal's
   `renderReliability()` (`ui/scripts/features/control-center.js`) onto the
   real, already-merged `observability_api.py` / `notification_api.py` /
   `runtime_contracts_api.py` routes -- the legacy screen's own
   `loadControlCenter()` calls these exact paths.

   Honest scope note: `organizationId` is accepted below on the SLO and
   notification-rule functions for parity with every other org-scoped fetch
   in this file (and to key fixture data the same way other screens do), but
   it has nowhere to go on the wire for these particular routes.
   `observability_api.py`'s and `notification_api.py`'s routes take no
   `organization_id` path or query param at all -- unlike, say,
   `fetchModelRoutes`'s `/v1/organizations/{organization_id}/model-routes` --
   because the server instead reads `context.require_organization()`, which
   resolves from the `X-Organization-Id` header
   (`security.py::get_security_context`). `identityHeaders()` above does not
   send that header today. That gap is pre-existing, shared infrastructure
   (identityHeaders is explicitly out of this addition's scope) and the
   legacy portal has the identical gap -- its own `api()` helper never sends
   `X-Organization-Id` either, so this is not a regression, just a limit on
   what a live (`VITE_USE_FIXTURES=0`) run of this screen can do today: those
   two endpoint families will 400 with "organization context is required for
   this operation" until that header is added, exactly as they would against
   the legacy UI. Fixture mode (the default) is unaffected -- it never
   depended on the header.
--------------------------------------------------------------------------- */

export interface SloDefinitionQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/observability/slo` (`observability_api.py::list_slo_definitions`,
 *  roles PlatformAdmin/DataAdmin/Operations/Viewer) -- every SLO definition
 *  for the caller's organization, newest first. */
export function fetchSloDefinitions(
  organizationId: string,
  query: SloDefinitionQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<SloDefinitionRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSloDefinitions(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<SloDefinitionRead>>(`/v1/observability/slo?${params}`, signal);
    },
  );
}

/** `POST /v1/observability/slo` (`observability_api.py::create_slo_definition`,
 *  roles PlatformAdmin/DataAdmin/Operations) -- 409s if `slo_key` already
 *  exists for this organization. */
export function createSloDefinition(
  organizationId: string,
  body: SloDefinitionCreate,
  signal?: AbortSignal,
): Promise<SloDefinitionRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateSloDefinition(organizationId, body),
    async () => {
      return postJson<SloDefinitionRead>("/v1/observability/slo", body, signal);
    },
  );
}

/** `GET /v1/observability/slo/{slo_id}/budget` (`observability_api.py::get_slo_budget`)
 *  -- computed live from the SLO's most recent `SloMeasurement`, never
 *  stored: `status` is HEALTHY/AT_RISK/BREACHED once a measurement exists
 *  (compared against `target`/`threshold`), NO_DATA when none ever landed. */
export function fetchSloBudget(
  sloId: string,
  signal?: AbortSignal,
): Promise<SloBudgetRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSloBudget(sloId),
    async () => {
      return get<SloBudgetRead>(`/v1/observability/slo/${sloId}/budget`, signal);
    },
  );
}

/** `GET /v1/observability/archive/status` (`observability_api.py::get_archive_status`)
 *  -- WORM audit-archive posture: counts, latest archive id/checksum, and
 *  legal-hold count, rolled into one of NO_ARCHIVES/LEGAL_HOLD_ACTIVE/HEALTHY.
 *  Org-scoped via the security context only, same gap noted in this block's
 *  banner comment -- no `organizationId` parameter to thread through. */
export function fetchArchiveStatus(signal?: AbortSignal): Promise<ArchiveStatusRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureArchiveStatus(),
    async () => {
      return get<ArchiveStatusRead>("/v1/observability/archive/status", signal);
    },
  );
}

export interface NotificationRuleQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/notification-rules` (`notification_api.py::list_notification_rules`,
 *  roles PlatformAdmin/DataAdmin/Operations/Viewer). */
export function fetchNotificationRules(
  organizationId: string,
  query: NotificationRuleQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<NotificationRuleRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureNotificationRules(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<NotificationRuleRead>>(`/v1/notification-rules?${params}`, signal);
    },
  );
}

/** `POST /v1/notification-rules` (`notification_api.py::create_notification_rule`,
 *  roles PlatformAdmin/DataAdmin/Operations). `conditions` is a free-form
 *  JSON matcher object -- the screen collects it from a raw JSON textarea,
 *  same as the legacy `#notification-rule-form`. */
export function createNotificationRule(
  organizationId: string,
  body: NotificationRuleCreate,
  signal?: AbortSignal,
): Promise<NotificationRuleRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateNotificationRule(organizationId, body),
    async () => {
      return postJson<NotificationRuleRead>("/v1/notification-rules", body, signal);
    },
  );
}

/** `POST /v1/data-contracts/{contract_id}/evaluate`
 *  (`runtime_contracts_api.py::evaluate_data_contract`, roles PlatformAdmin/
 *  DataSteward/DataEngineer/Viewer) -- no request body, just the path id.
 *  Evaluates the contract against current schema/quality/freshness state,
 *  persists any violations found, and returns the same evaluation the
 *  enforcement path itself acts on (`allowed`/`enforcement_action`). */
export function evaluateDataContract(
  contractId: string,
  signal?: AbortSignal,
): Promise<EvaluationResponse> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureEvaluateDataContract(contractId),
    async () => {
      return postJson<EvaluationResponse>(`/v1/data-contracts/${contractId}/evaluate`, {}, signal);
    },
  );
}

export interface ContractViolationsQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/data-contracts/{contract_id}/violations`
 *  (`runtime_contracts_api.py::list_contract_violations`, same roles as
 *  evaluate above). `ViolationRead` (`./ui-types`) is hand-written -- see
 *  its own comment for why. */
export function fetchContractViolations(
  contractId: string,
  query: ContractViolationsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ViolationRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureContractViolations(contractId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ViolationRead>>(`/v1/data-contracts/${contractId}/violations?${params}`, signal);
    },
  );
}

/** `GET /v1/data-contracts/{contract_id}/sla-status`
 *  (`runtime_contracts_api.py::get_sla_status`, same roles as evaluate
 *  above) -- rolling compliance over the trailing `period_days` (server
 *  `Query` bounds: default 30, 1-365). */
export function fetchContractSlaStatus(
  contractId: string,
  periodDays = 30,
  signal?: AbortSignal,
): Promise<SlaStatusResponse> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureContractSlaStatus(contractId, periodDays),
    async () => {
      const params = new URLSearchParams();
      params.set("period_days", String(periodDays));
      return get<SlaStatusResponse>(`/v1/data-contracts/${contractId}/sla-status?${params}`, signal);
    },
  );
}

/* ---------------------------------------------------------------------------
   AT-1: Playbooks — saved, scheduled bulk-metadata automation rules
   (`playbooks_api.py`, prefix `/v1`). Every route here is real and
   already-merged; `USE_FIXTURES` gates each the same way as every call
   above, so `npm run dev`/`npm run test` need no backend.
--------------------------------------------------------------------------- */

export interface PlaybooksQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/playbooks`. */
export function fetchPlaybooks(
  organizationId: string,
  query: PlaybooksQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<PlaybookRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixturePlaybooks(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<PlaybookRead>>(
        `/v1/organizations/${organizationId}/playbooks?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/playbooks` — 409 if a playbook
 *  with this name already exists in the org. */
export function createPlaybook(
  organizationId: string,
  body: PlaybookCreate,
  signal?: AbortSignal,
): Promise<PlaybookRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreatePlaybook(organizationId, body),
    async () => {
      return postJson<PlaybookRead>(`/v1/organizations/${organizationId}/playbooks`, body, signal);
    },
  );
}

/** `PATCH /v1/playbooks/{playbook_id}` — every field optional; send only
 *  what changed (e.g. `{ enabled: !current }` to toggle). */
export function updatePlaybook(
  playbookId: string,
  body: PlaybookUpdate,
  signal?: AbortSignal,
): Promise<PlaybookRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureUpdatePlaybook(playbookId, body),
    async () => {
      return patchJson<PlaybookRead>(`/v1/playbooks/${playbookId}`, body, signal);
    },
  );
}

/** `DELETE /v1/playbooks/{playbook_id}` — 204, no response body. */
export function deletePlaybook(playbookId: string, signal?: AbortSignal): Promise<void> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDeletePlaybook(playbookId),
    async () => {
      return deleteRequest(`/v1/playbooks/${playbookId}`, signal);
    },
  );
}

/** `POST /v1/playbooks/{playbook_id}/run` — manual out-of-cycle trigger;
 *  409 if the playbook is disabled. Takes no meaningful body, but this file's
 *  own convention (see `submitStudioChangeSet`) is to always send an explicit
 *  `{}` rather than an optional-looking empty POST. */
export function runPlaybookNow(
  playbookId: string,
  signal?: AbortSignal,
): Promise<PlaybookRunResultRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRunPlaybook(playbookId),
    async () => {
      return postJson<PlaybookRunResultRead>(`/v1/playbooks/${playbookId}/run`, {}, signal);
    },
  );
}
