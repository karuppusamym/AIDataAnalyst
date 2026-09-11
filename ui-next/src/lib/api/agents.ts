/* ---------------------------------------------------------------------------
   Agents and AI governance — everything an agent does and everything that
   governs it.

   Ask (the single-shot governed question) with its run history, grounding
   receipts and error classification; the AI registry, trust scores and
   remediation loop; model routes and evaluations; contract requests; the
   agent inbox, roster and kill switch; and the reviewer-agent console.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson, putJson } from "./transport";
import { USE_FIXTURES } from "../appConfig";
import {
  makeFixtureAgentAnalysis,
  makeFixtureAgentContractRequests,
  makeFixtureAgentEvaluations,
  makeFixtureAgentInbox,
  makeFixtureAgentRoster,
  makeFixtureAgentRun,
  makeFixtureAgentRunGroundingReceipts,
  makeFixtureAgentRuns,
  makeFixtureAiAssessmentTemplates,
  makeFixtureAiAssets,
  makeFixtureAiRemediations,
  makeFixtureAiRuntimeStatus,
  makeFixtureAiTrust,
  makeFixtureCreateModelRoute,
  makeFixtureDisagreementRates,
  makeFixtureModelRoutes,
  makeFixtureReviewerAgentPreReview,
  makeFixtureReviewerAgentRun,
  makeFixtureReviewerAgentSamples,
  makeFixtureReviewerAgentState,
  makeFixtureRunAgentEvaluation,
  makeFixtureTaskAgentRun,
  makeFixtureTaskAgentState,
  makeFixtureSubmitAgentContractRequest,
  makeFixtureSubmitModelRoute,
  makeFixtureUpdateAiRemediation,
} from "../fixtures";
import { ApiError } from "../http";
import type {
  AgentAnalysisRequest,
  AgentAnalysisResponse,
  AgentContractRequestCreate,
  AgentContractRequestRead,
  AgentEvaluationRunRead,
  AgentInboxRead,
  AgentRosterRead,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  AiAssessmentTemplateRead,
  AiAssetVersionRead,
  AiRemediationRead,
  AiRemediationUpdate,
  AiRuntimeStatusRead,
  AiTrustScoreRead,
  DisagreementReportRead,
  GovernanceReviewRead,
  ModelRouteConfigurationCreate,
  ModelRouteConfigurationRead,
  ReviewAuditSampleRead,
  ReviewerAgentRunResult,
  ReviewerAgentStateRead,
  TaskAgentRunRead,
  TaskAgentStateRead,
} from "../types";
import type { PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   Ask (UX-15/UX-16, tracker rows UX-15/UX-16): the single-shot governed
   question-answering endpoint (`run_agent_analysis`, `api.py:2912`) and its
   history/evidence reads. Every one of these hits a real, already-merged
   route -- no backend stub, no invented endpoint, same standing as the
   UX-15/UX-20 calls above.
--------------------------------------------------------------------------- */

/** `POST /v1/datasources/{id}/agent-analyses` (`run_agent_analysis`,
 *  `api.py:2912`) -- ask a governed question against one datasource.
 *  Single-shot, not streaming: one JSON response carrying the explanation,
 *  the query that was actually run, and every piece of evidence
 *  (`step_trace`/`retrieval_evidence`/`plan_evidence`) behind it.
 *
 *  This can fail closed several distinct ways, each a *different* HTTP
 *  status the route maps deliberately rather than collapsing to one error
 *  shape (`api.py:2912`'s own except clauses):
 *    409  `AgentClarificationRequired` -- most importantly AT-9's ambiguous-
 *         governed-term refusal (`_check_definition_ambiguity` /
 *         `format_ambiguous_definition_refusal`, semantic_inference.py),
 *         but also a governed tool needing parameters this v1 form never
 *         sends. Also 409 for a disabled datasource (`ensure_datasource_enabled`,
 *         fleet.py) -- same status, different `detail`, so a caller must read
 *         `detail`, not just the status, to tell them apart. See
 *         `classifyAgentAskError` below.
 *    422  `AgentPolicyRejected` / `QueryRejected` -- the deterministic policy
 *         or query layer refused the request or the generated query.
 *    503  `ModelRouteUnavailable` -- no model route could serve the request.
 *    502  anything unhandled -- `"agent analysis execution failed"`.
 */
export function runAgentAnalysis(
  datasourceId: string,
  body: AgentAnalysisRequest,
  signal?: AbortSignal,
): Promise<AgentAnalysisResponse> {
  return demoOr(
    async () => makeFixtureAgentAnalysis(datasourceId, body),
    async () => {
      return postJson<AgentAnalysisResponse>(
        `/v1/datasources/${datasourceId}/agent-analyses`,
        body,
        signal,
      );
    },
  );
}

export interface AgentRunsQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{id}/agent-runs` (`list_agent_runs`, `api.py:2965`)
 *  -- past questions asked against this datasource, newest first. Offset-
 *  paged (the route's own `limit`/`offset` query params), unlike the
 *  cursor-paged catalog/tables routes above. `AgentRunRead` (./types.ts)
 *  carries no `question` text field -- the server never persists the raw
 *  question string on the run row -- so a history row is identified by its
 *  id/status/generation_source/timestamps, not by the question that produced
 *  it; only a run whose answer is still held in this session's own state
 *  (just asked, not yet reloaded from a permalink) has its question visible
 *  client-side. */
export function fetchAgentRuns(
  datasourceId: string,
  query: AgentRunsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AgentRunRead>> {
  return demoOr(
    async () => makeFixtureAgentRuns(datasourceId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<AgentRunRead>>(`/v1/datasources/${datasourceId}/agent-runs?${params}`, signal);
    },
  );
}

/** `GET /v1/agent-runs/{id}` (`get_agent_run`, `api.py:3001`) -- one run's
 *  full detail, for the evidence panel behind a history item or a `run`
 *  URL permalink that outlives this session's own in-memory answer. */
export function fetchAgentRun(
  agentRunId: string,
  signal?: AbortSignal,
): Promise<AgentRunRead> {
  return demoOr(
    async () => makeFixtureAgentRun(agentRunId),
    async () => {
      return get<AgentRunRead>(`/v1/agent-runs/${agentRunId}`, signal);
    },
  );
}

/** `GET /v1/agent-runs/{id}/grounding-receipts` (`get_agent_run_grounding_receipts`,
 *  `api.py:3018`) -- AT-6 replay proof: resolves every grounding-fragment
 *  digest this run recorded back to the actual source content (e.g. the
 *  business-annotation text) the answer was grounded on, with
 *  `digest_verified` confirming the stored content still matches what the
 *  run saw. Powers the "how this was answered" evidence panel for both a
 *  freshly-asked question and a reopened history item. */
export function fetchAgentRunGroundingReceipts(
  agentRunId: string,
  signal?: AbortSignal,
): Promise<AgentRunGroundingReceiptsRead> {
  return demoOr(
    async () => makeFixtureAgentRunGroundingReceipts(agentRunId),
    async () => {
      return get<AgentRunGroundingReceiptsRead>(
        `/v1/agent-runs/${agentRunId}/grounding-receipts`,
        signal,
      );
    },
  );
}

/** AT-9 / row UX-15's error-mapping requirement: `run_agent_analysis` maps
 *  several distinct failures onto only four HTTP statuses (see
 *  `runAgentAnalysis`'s own doc comment), so the screen must read `detail`,
 *  not just `status`, to render each as its own state rather than one
 *  generic failure banner. The ambiguity case additionally carries every
 *  competing definition inline in `detail`
 *  (`format_ambiguous_definition_refusal`, semantic_inference.py) --
 *  `alternatives` below parses those back out so the refusal can render each
 *  definition and its owner as its own item instead of one wall of text.
 *  Parsing is a front-end convenience only: if the format ever changes,
 *  `alternatives` degrades to `[]` and the raw `detail` is still shown. */
export type AgentAskErrorKind =
  | "AMBIGUOUS_DEFINITION"
  | "DATASOURCE_DISABLED"
  | "POLICY_REJECTED"
  | "MODEL_UNAVAILABLE"
  | "MODEL_THROTTLED"
  | "CLARIFICATION_NEEDED"
  | "SERVER_ERROR"
  | "UNKNOWN";

export interface AgentAskErrorAlternative {
  businessNodeId: string;
  displayName: string;
  owner: string;
  definition: string;
}

export interface AgentAskError {
  kind: AgentAskErrorKind;
  status: number;
  detail: string;
  /** Only populated for `AMBIGUOUS_DEFINITION`. */
  alternatives: AgentAskErrorAlternative[];
}

const AMBIGUOUS_DEFINITION_RE =
  /^the term '.+' resolves to \d+ equally applicable governed definitions/;

const AMBIGUOUS_ALTERNATIVE_RE = /^([^\]]+)\] '([^']+)' \(owner: ([^)]+)\) -- ([\s\S]+)$/;

function parseAmbiguousAlternatives(detail: string): AgentAskErrorAlternative[] {
  const marker = " [business_node=";
  const firstIdx = detail.indexOf(marker);
  if (firstIdx === -1) return [];
  const segments = detail
    .slice(firstIdx + marker.length)
    .split(marker)
    .filter(Boolean);
  const alternatives: AgentAskErrorAlternative[] = [];
  for (const segment of segments) {
    const m = AMBIGUOUS_ALTERNATIVE_RE.exec(segment);
    if (!m) continue;
    alternatives.push({
      businessNodeId: m[1]!,
      displayName: m[2]!,
      owner: m[3]!,
      definition: m[4]!.trim(),
    });
  }
  return alternatives;
}

export function classifyAgentAskError(error: ApiError): AgentAskError {
  const { status, detail } = error;
  if (status === 409 && detail === "datasource is disabled") {
    return { kind: "DATASOURCE_DISABLED", status, detail, alternatives: [] };
  }
  if (status === 409 && AMBIGUOUS_DEFINITION_RE.test(detail)) {
    return {
      kind: "AMBIGUOUS_DEFINITION",
      status,
      detail,
      alternatives: parseAmbiguousAlternatives(detail),
    };
  }
  if (status === 409) return { kind: "CLARIFICATION_NEEDED", status, detail, alternatives: [] };
  if (status === 422) return { kind: "POLICY_REJECTED", status, detail, alternatives: [] };
  if (status === 429) return { kind: "MODEL_THROTTLED", status, detail, alternatives: [] };
  if (status === 503) return { kind: "MODEL_UNAVAILABLE", status, detail, alternatives: [] };
  if (status === 502) return { kind: "SERVER_ERROR", status, detail, alternatives: [] };
  return { kind: "UNKNOWN", status, detail, alternatives: [] };
}

/* ---------------------------------------------------------------------------
   AI governance (module 15 / CP-7,CP-8) — the AI registry, trust scoring and
   remediation loop. The backend (ai_registry_api.py) has carried these since
   the AI-trust slice landed; ui-next had the types but no screen. Same
   USE_FIXTURES gate and self-contained-block convention as the relationships
   block above.
--------------------------------------------------------------------------- */

/** `GET /v1/organizations/{org}/ai-assets` — one row per AI asset at its
 *  latest version (name, provider, risk tier, and the version id the trust and
 *  remediation calls below are scoped by). */
export function fetchAiAssets(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<AiAssetVersionRead>> {
  return demoOr(
    async () => makeFixtureAiAssets(organizationId),
    async () => {
      return get<PageOf<AiAssetVersionRead>>(
        `/v1/organizations/${organizationId}/ai-assets?limit=200`,
        signal,
      );
    },
  );
}

/** `GET /v1/ai-asset-versions/{id}/trust` — the deterministic trust score,
 *  grade, per-factor breakdown and blocking findings for one asset version. */
export function fetchAiAssetTrust(
  versionId: string,
  signal?: AbortSignal,
): Promise<AiTrustScoreRead> {
  return demoOr(
    async () => makeFixtureAiTrust(versionId),
    async () => {
      return get<AiTrustScoreRead>(`/v1/ai-asset-versions/${versionId}/trust`, signal);
    },
  );
}

/** `GET /v1/ai-asset-versions/{id}/remediations` — the findings-to-remediation
 *  log for one asset version. */
export function fetchAiRemediations(
  versionId: string,
  signal?: AbortSignal,
): Promise<PageOf<AiRemediationRead>> {
  return demoOr(
    async () => makeFixtureAiRemediations(versionId),
    async () => {
      return get<PageOf<AiRemediationRead>>(
        `/v1/ai-asset-versions/${versionId}/remediations?limit=200`,
        signal,
      );
    },
  );
}

/** `PUT /v1/ai-remediations/{id}` — advance a remediation's status. Moving one
 *  to ACCEPTED_RISK is enforced server-side to an independent risk role. */
export function updateAiRemediation(
  remediationId: string,
  body: AiRemediationUpdate,
  signal?: AbortSignal,
): Promise<AiRemediationRead> {
  return demoOr(
    async () => makeFixtureUpdateAiRemediation(remediationId, body),
    async () => {
      return putJson<AiRemediationRead>(`/v1/ai-remediations/${remediationId}`, body, signal);
    },
  );
}

/** `GET /v1/ai-assessment-templates` — the built-in control checklists
 *  (EU AI Act, NIST AI RMF, enterprise use-case) an assessment is seeded from. */
export function fetchAiAssessmentTemplates(
  signal?: AbortSignal,
): Promise<AiAssessmentTemplateRead[]> {
  return demoOr(
    async () => makeFixtureAiAssessmentTemplates(),
    async () => {
      return get<AiAssessmentTemplateRead[]>("/v1/ai-assessment-templates", signal);
    },
  );
}

/* ---------------------------------------------------------------------------
   AI governance -- the legacy portal's `agents-view` (`ui/index.html`,
   heading "Models, agents, and evaluations"), ported onto the real,
   already-merged `ai_governance_api.py` model-route routes plus `api.py`'s
   `/ai/runtime-status` and `/agent-evaluations` routes that view calls.
   See `AiGovernanceScreen.tsx`'s own header comment
   for the full endpoint list, file:line citations, and what was
   deliberately left out (the kill switch; `AgentEvalGateRead`, which is
   `AiRegistryScreen`'s per-asset-version concern, not this org-wide suite).
--------------------------------------------------------------------------- */

export interface ModelRouteQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/model-routes` (`list_model_routes`,
 *  `ai_governance_api.py:167`) -- one row per route version, newest version
 *  first per `route_key`, exactly as the query orders them server-side;
 *  matches `loadModelRoutes()`'s call in the legacy screen. */
export function fetchModelRoutes(
  organizationId: string,
  query: ModelRouteQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ModelRouteConfigurationRead>> {
  return demoOr(
    async () => makeFixtureModelRoutes(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ModelRouteConfigurationRead>>(
        `/v1/organizations/${organizationId}/model-routes?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/model-routes` (`create_model_route`,
 *  `ai_governance_api.py:94`) -- creates a new `DRAFT` version for the given
 *  `route_key` (version auto-incremented server-side); matches the legacy
 *  screen's `#model-route-form` submit handler field-for-field. */
export function createModelRoute(
  organizationId: string,
  body: ModelRouteConfigurationCreate,
  signal?: AbortSignal,
): Promise<ModelRouteConfigurationRead> {
  return demoOr(
    async () => makeFixtureCreateModelRoute(organizationId, body),
    async () => {
      return postJson<ModelRouteConfigurationRead>(
        `/v1/organizations/${organizationId}/model-routes`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/model-routes/{route_id}/submit` (`submit_model_route`,
 *  `ai_governance_api.py:209`) -- moves a `DRAFT` route to `PENDING_REVIEW`
 *  and opens the same `GovernanceReview` `ReviewQueueScreen` reads; matches
 *  the legacy screen's `data-submit-route` action. */
export function submitModelRoute(
  routeId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    async () => makeFixtureSubmitModelRoute(routeId),
    async () => {
      return postJson<GovernanceReviewRead>(`/v1/model-routes/${routeId}/submit`, {}, signal);
    },
  );
}

/** `GET /v1/ai/runtime-status` (`ai_runtime_status`, `api.py:181`) -- the
 *  orchestration/model-route/identity/secrets posture the legacy screen's
 *  `#ai-runtime` rail renders via `renderRuntime()`. Org-independent (the
 *  route takes no organization id -- it reflects process-wide `Settings`,
 *  not a tenant's data). */
export function fetchAiRuntimeStatus(signal?: AbortSignal): Promise<AiRuntimeStatusRead> {
  return demoOr(
    async () => makeFixtureAiRuntimeStatus(),
    async () => {
      return get<AiRuntimeStatusRead>("/v1/ai/runtime-status", signal);
    },
  );
}

export interface AgentEvaluationQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/agent-evaluations`
 *  (`list_agent_evaluations`, `api.py:349`) -- the legacy screen's
 *  `#evaluation-table` evidence, newest run first. */
export function fetchAgentEvaluations(
  organizationId: string,
  query: AgentEvaluationQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AgentEvaluationRunRead>> {
  return demoOr(
    async () => makeFixtureAgentEvaluations(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<AgentEvaluationRunRead>>(
        `/v1/organizations/${organizationId}/agent-evaluations?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/agent-evaluations`
 *  (`run_agent_evaluation`, `api.py:294`) -- executes the deterministic
 *  repeatable control suite (`run_control_evaluation`, `agent_evals.py`)
 *  and records one `AgentEvaluationRunRead`; matches the legacy screen's
 *  `#run-evaluation` button. */
export function runAgentEvaluation(
  organizationId: string,
  signal?: AbortSignal,
): Promise<AgentEvaluationRunRead> {
  return demoOr(
    async () => makeFixtureRunAgentEvaluation(organizationId),
    async () => {
      return postJson<AgentEvaluationRunRead>(
        `/v1/organizations/${organizationId}/agent-evaluations`,
        {},
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   Agent contract requests -- the reviewed, eval-gated path alongside the
   direct-write `PUT .../agents/{version}/contract`. A `CONTRACT_AUTHORS`
   principal submits a requested `AgentContractDefinition`; a different
   principal decides it through the ordinary `GovernanceReview` queue
   (`object_type=AGENT_CONTRACT_REQUEST`); on APPROVE the AT-8/N17 eval gate
   is checked live before the contract is actually written. See
   `agent_contract_request_api.py`'s module docstring for the full flow and
   its honestly-scoped limitation (reuses `CONTRACT_AUTHORS`, does not invent
   a narrower "external agent" role).
--------------------------------------------------------------------------- */

export interface AgentContractRequestQuery {
  status?: "PENDING" | "ACTIVATED" | "REJECTED" | "EVAL_BLOCKED";
  aiAssetVersionId?: string;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/agent-contract-requests`
 *  (`list_agent_contract_requests`) -- newest submission first. */
export function fetchAgentContractRequests(
  organizationId: string,
  query: AgentContractRequestQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AgentContractRequestRead>> {
  return demoOr(
    async () => makeFixtureAgentContractRequests(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      if (query.aiAssetVersionId) params.set("ai_asset_version_id", query.aiAssetVersionId);
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<AgentContractRequestRead>>(
        `/v1/organizations/${organizationId}/agent-contract-requests?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/agent-contract-requests`
 *  (`submit_agent_contract_request`) -- opens a `GovernanceReview` rather
 *  than activating the contract; returns `202 Accepted` with the new
 *  request's `PENDING` state. */
export function submitAgentContractRequest(
  organizationId: string,
  body: AgentContractRequestCreate,
  signal?: AbortSignal,
): Promise<AgentContractRequestRead> {
  return demoOr(
    async () => makeFixtureSubmitAgentContractRequest(organizationId, body),
    async () => {
      return postJson<AgentContractRequestRead>(
        `/v1/organizations/${organizationId}/agent-contract-requests`,
        body,
        signal,
      );
    },
  );
}

/**
 * `GET /v1/organizations/{org}/agent-inbox` (agent_contract_api.py) — the
 * one screen a supervisor opens: what their agents did, and what is waiting
 * on a human. Composed server-side in a fixed number of queries, so this is
 * a single call rather than the five the screen would otherwise make.
 */
export function fetchAgentInbox(
  organizationId: string,
  persona: string,
  signal?: AbortSignal,
): Promise<AgentInboxRead> {
  return demoOr(
    async () => makeFixtureAgentInbox(organizationId, persona),
    async () => {
      return get<AgentInboxRead>(
        `/v1/organizations/${organizationId}/agent-inbox?persona=${encodeURIComponent(persona)}`,
        signal,
      );
    },
  );
}

/**
 * `POST .../agents/{version}/contract/kill` — engage one agent's kill switch.
 * Takes effect on that agent's very next run: the orchestrator queries the
 * switch live rather than caching it. Fixture mode refuses rather than
 * pretending, because a kill switch that silently did nothing is the worst
 * possible thing to mock.
 */
export async function engageAgentKillSwitch(
  organizationId: string,
  versionId: string,
  reason: string,
): Promise<void> {
  if (USE_FIXTURES) {
    throw new Error("Kill switch is unavailable in fixture mode — run against the API.");
  }
  await postJson<unknown>(
    `/v1/organizations/${organizationId}/agents/${versionId}/contract/kill`,
    { reason },
  );
}

/**
 * `GET /v1/organizations/{org}/ai-agents/roster` (`get_agent_roster`,
 * `agent_roster_api.py`) — UX-19: every registered `AGENT`-kind AI asset's
 * published purpose, an aggregated method summary (recent
 * `AgentRun.plan_evidence`/`generation_source`), a bounded window of recent
 * live results, and an honest auto-apply determination.
 */
export function fetchAgentRoster(
  organizationId: string,
  query: { windowDays?: number; recentResultsLimit?: number } = {},
  signal?: AbortSignal,
): Promise<AgentRosterRead> {
  return demoOr(
    async () => makeFixtureAgentRoster(organizationId, query.windowDays ?? 30),
    async () => {
      const params = new URLSearchParams();
      if (query.windowDays) params.set("window_days", String(query.windowDays));
      if (query.recentResultsLimit) params.set("recent_results_limit", String(query.recentResultsLimit));
      const suffix = params.toString() ? `?${params}` : "";
      return get<AgentRosterRead>(
        `/v1/organizations/${organizationId}/ai-agents/roster${suffix}`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   ADR-0027: the reviewer agent console. Every one of these hits a real,
   already-merged route in `agent_contract_api.py` — pre-review/auto-decide
   are governed writes with visible, honest counts; suspend/resume/resolve
   are safety-critical human actions that fixture mode refuses rather than
   pretends to perform, the same rule `engageAgentKillSwitch` above follows.
--------------------------------------------------------------------------- */

/** `GET /v1/organizations/{org}/reviewer-agent` — the agent's own state:
 *  enabled/suspended, the tier ceiling it may auto-decide up to, its
 *  sampling rate, and the principal it acts as. */
export function fetchReviewerAgentState(
  organizationId: string,
  signal?: AbortSignal,
): Promise<ReviewerAgentStateRead> {
  return demoOr(
    async () => makeFixtureReviewerAgentState(organizationId),
    async () => {
      return get<ReviewerAgentStateRead>(`/v1/organizations/${organizationId}/reviewer-agent`, signal);
    },
  );
}

/** `POST .../reviewer-agent/pre-review` — attaches tier/evidence/recommendation
 *  to pending review items. Decides nothing, and works even while the agent
 *  is disabled or suspended, so fixture mode returns real-looking counts
 *  rather than refusing. */
export function runReviewerAgentPreReview(
  organizationId: string,
  limit = 200,
  signal?: AbortSignal,
): Promise<ReviewerAgentRunResult> {
  return demoOr(
    async () => makeFixtureReviewerAgentPreReview(),
    async () => {
      return postJson<ReviewerAgentRunResult>(
        `/v1/organizations/${organizationId}/reviewer-agent/pre-review?limit=${limit}`,
        {},
        signal,
      );
    },
  );
}

/** `POST .../reviewer-agent/run` — actually auto-decides T0/T1 items. 409s
 *  (via `ApiError`) when the agent is disabled or suspended; the caller
 *  should read `err.message` for the reason. */
export function runReviewerAgent(
  organizationId: string,
  limit = 100,
  signal?: AbortSignal,
): Promise<ReviewerAgentRunResult> {
  return demoOr(
    async () => makeFixtureReviewerAgentRun(),
    async () => {
      return postJson<ReviewerAgentRunResult>(
        `/v1/organizations/${organizationId}/reviewer-agent/run?limit=${limit}`,
        {},
        signal,
      );
    },
  );
}

/** `POST .../reviewer-agent/suspend` — ADR-0027 condition (c): one human
 *  action, effective immediately. Fixture mode refuses rather than
 *  pretending, same rationale as `engageAgentKillSwitch`. */
export async function suspendReviewerAgent(
  organizationId: string,
  reason: string,
): Promise<ReviewerAgentStateRead> {
  if (USE_FIXTURES) {
    throw new Error("Suspend is unavailable in fixture mode — run against the API.");
  }
  return postJson<ReviewerAgentStateRead>(
    `/v1/organizations/${organizationId}/reviewer-agent/suspend`,
    { reason },
  );
}

/** `POST .../reviewer-agent/resume` — same rule as `suspendReviewerAgent`. */
export async function resumeReviewerAgent(
  organizationId: string,
  reason: string,
): Promise<ReviewerAgentStateRead> {
  if (USE_FIXTURES) {
    throw new Error("Resume is unavailable in fixture mode — run against the API.");
  }
  return postJson<ReviewerAgentStateRead>(
    `/v1/organizations/${organizationId}/reviewer-agent/resume`,
    { reason },
  );
}

/** `GET .../reviewer-agent/disagreement-rates` — ADR-0027's 5% revisit
 *  trigger, as a number per object type rather than a sentence. */
export function fetchDisagreementRates(
  organizationId: string,
  windowDays: number,
  signal?: AbortSignal,
): Promise<DisagreementReportRead> {
  return demoOr(
    async () => makeFixtureDisagreementRates(windowDays),
    async () => {
      return get<DisagreementReportRead>(
        `/v1/organizations/${organizationId}/reviewer-agent/disagreement-rates?window_days=${windowDays}`,
        signal,
      );
    },
  );
}

export interface ReviewerAgentSamplesQuery {
  outcome?: "PENDING" | "AGREED" | "DISAGREED" | "ALL";
  limit?: number;
  offset?: number;
}

/** `GET .../reviewer-agent/samples` — the sampled-decision audit queue, one
 *  outcome filter at a time (`outcome=PENDING` is the endpoint's own default). */
export function fetchReviewerAgentSamples(
  organizationId: string,
  query: ReviewerAgentSamplesQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ReviewAuditSampleRead>> {
  return demoOr(
    async () => makeFixtureReviewerAgentSamples(query),
    async () => {
      const params = new URLSearchParams();
      params.set("outcome", query.outcome ?? "PENDING");
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ReviewAuditSampleRead>>(
        `/v1/organizations/${organizationId}/reviewer-agent/samples?${params}`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   ADR-0029: task agents — the steward, lineage and quality agents. Each has
   two real routes, `/v1/organizations/{org}/{kind}-agent` and `…/run`, with
   the same response shapes (`task_agent_api.py`). A run only ever *opens*
   review items — no task agent decides anything — so fixture mode returns a
   representative result rather than refusing, the same standing as the
   reviewer agent's run fixture.
--------------------------------------------------------------------------- */

export type TaskAgentKind = "steward" | "lineage" | "quality";

/** What a run is asked to do. Each agent's own request model narrows
 *  `capabilities` to the keys it has; the shape is otherwise shared. */
export interface TaskAgentRunBody {
  capabilities: string[];
  limit: number;
  datasource_id: string | null;
  dry_run: boolean;
}

/** `GET /v1/organizations/{org}/{kind}-agent` — whether the agent could run
 *  here (and the refusal it would get if not), its tier and what that tier
 *  lets it do, any kill switch stopping it, and how its proposals have fared
 *  with reviewers. */
export function fetchTaskAgentState(
  organizationId: string,
  kind: TaskAgentKind,
  signal?: AbortSignal,
): Promise<TaskAgentStateRead> {
  return demoOr(
    async () => makeFixtureTaskAgentState(organizationId, kind),
    async () => {
      return get<TaskAgentStateRead>(`/v1/organizations/${organizationId}/${kind}-agent`, signal);
    },
  );
}

/** `POST .../{kind}-agent/run` — one bounded run. 409s (via `ApiError`) with a
 *  stable reason code in `detail` when the agent may not act; in that case
 *  nothing the run produced is kept. `dry_run` previews and opens nothing. */
export function runTaskAgent(
  organizationId: string,
  kind: TaskAgentKind,
  body: TaskAgentRunBody,
  signal?: AbortSignal,
): Promise<TaskAgentRunRead> {
  return demoOr(
    async () => makeFixtureTaskAgentRun(organizationId, kind, body),
    async () => {
      return postJson<TaskAgentRunRead>(
        `/v1/organizations/${organizationId}/${kind}-agent/run`,
        body,
        signal,
      );
    },
  );
}

/** `POST .../reviewer-agent/samples/{id}/resolve` — a human's verdict on one
 *  of the agent's sampled auto-decisions. `rationale` is mandatory. Fixture
 *  mode refuses rather than pretending, same rule as the actions above: the
 *  fixture list is recomputed fresh on every fetch, so a fake "resolved"
 *  here could never actually move the item out of the pending queue. */
export async function resolveAuditSample(
  organizationId: string,
  sampleId: string,
  body: { human_outcome: "AGREED" | "DISAGREED"; rationale: string },
): Promise<ReviewAuditSampleRead> {
  if (USE_FIXTURES) {
    throw new Error("Resolving a sample is unavailable in fixture mode — run against the API.");
  }
  return postJson<ReviewAuditSampleRead>(
    `/v1/organizations/${organizationId}/reviewer-agent/samples/${sampleId}/resolve`,
    body,
  );
}
