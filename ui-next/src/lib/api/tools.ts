/* ---------------------------------------------------------------------------
   Tools — the governed tool registry and the plans that orchestrate it.

   One tool version's lifecycle (create, submit for review, request
   deprecation, execute) and multi-step tool plans (create, validate,
   execute, cancel) with the evidence a plan run leaves behind.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import type {
  ExecutionRead,
  GovernanceReviewRead,
  GovernedToolVersionCreate,
  GovernedToolVersionRead,
  ToolExecutionRequest,
  ToolExecutionResponse,
  ToolPlanCreate,
  ToolPlanDetailRead,
  ToolPlanRead,
  ValidationResponse,
} from "../types";
import type { PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   Tool registry -- nav id `tools`, `ToolRegistryScreen`'s own routes. See
   that screen's file-top comment for the full endpoint list and what was
   deliberately left out of scope (the multi-table blueprint helper and the
   certification-cases/certification-runs sub-flow -- legacy's `tools-view`
   never calls either). Datasource options for the create panel reuse the
   already-existing `listOrgDatasources` (`./identity.ts`), filtered client-side by
   `project_id` -- exactly what the legacy screen's own
   `populateProjectSources()` (`ui/scripts/core.js`) does against its
   org-wide `state.sources`; there is no project-scoped datasource-list
   endpoint to call instead. */

export interface ToolQuery {
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{project_id}/tools` (`list_tools`, `tool_api.py:609`)
 *  -- usage-ranked, optionally filtered by `status` (`DRAFT` /
 *  `REVIEW_REQUIRED` / `PUBLISHED` / `DEPRECATED`); matches the legacy
 *  screen's own `loadTools()`. */
export function fetchTools(
  projectId: string,
  query: ToolQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<GovernedToolVersionRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureTools(projectId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      params.set("limit", String(query.limit ?? 200));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<GovernedToolVersionRead>>(`/v1/projects/${projectId}/tools?${params}`, signal);
    },
  );
}

/** `POST /v1/projects/{project_id}/tools` (`create_tool_version`,
 *  `tool_api.py:348`) -- validates the SQL template server-side (guarded
 *  table access, placeholders matching declared parameters exactly) and
 *  persists a new `DRAFT` version; matches the legacy screen's
 *  `#tool-author-form` submit. Reusing an existing `slug` within this
 *  project attaches the draft to that tool as its next version instead of
 *  creating a new one (`_persist_tool_version_draft`, `tool_api.py:201`) --
 *  what "New version" in this screen relies on. */
export function createToolVersion(
  projectId: string,
  body: GovernedToolVersionCreate,
  signal?: AbortSignal,
): Promise<GovernedToolVersionRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateToolVersion(projectId, body),
    async () => {
      return postJson<GovernedToolVersionRead>(`/v1/projects/${projectId}/tools`, body, signal);
    },
  );
}

/** `POST /v1/tool-versions/{version_id}/submit` (`submit_tool_for_review`,
 *  `tool_api.py:692`) -- moves a `DRAFT` version into the same
 *  `GovernanceReview` queue `ReviewQueueScreen` reads; matches the legacy
 *  screen's `data-submit-tool` action. */
export function submitToolForReview(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSubmitToolForReview(versionId),
    async () => {
      return postJson<GovernanceReviewRead>(`/v1/tool-versions/${versionId}/submit`, {}, signal);
    },
  );
}

/** `POST /v1/tool-versions/{version_id}/deprecation-submit`
 *  (`submit_tool_deprecation`, `tool_api.py:756`) -- requests retirement
 *  review for a `PUBLISHED` version, recording its computed blast radius as
 *  audit evidence before the review is even decided; matches the legacy
 *  screen's `data-deprecate-tool` action. */
export function requestToolDeprecation(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRequestToolDeprecation(versionId),
    async () => {
      return postJson<GovernanceReviewRead>(`/v1/tool-versions/${versionId}/deprecation-submit`, {}, signal);
    },
  );
}

/** `POST /v1/tool-versions/{version_id}/execute` (`execute_tool`,
 *  `tool_api.py:881`, `response_model=ToolExecutionResponse`) -- runs a
 *  `PUBLISHED` version's SQL template through the same governed query
 *  gateway `AskScreen`'s freeform path uses, bound to the caller-supplied
 *  parameters; matches the legacy screen's `executeSelectedTool()`. A
 *  non-null `quality_gate` on the response means `check_tool_gate` demoted
 *  this run to WARN over an open, non-critical upstream incident -- a BLOCK
 *  never reaches this response at all (refused with 409 before execution). */
export function executeToolVersion(
  versionId: string,
  body: ToolExecutionRequest,
  signal?: AbortSignal,
): Promise<ToolExecutionResponse> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureExecuteToolVersion(versionId, body),
    async () => {
      return postJson<ToolExecutionResponse>(`/v1/tool-versions/${versionId}/execute`, body, signal);
    },
  );
}

/* ---------------------------------------------------------------------------
   Tool plans -- multi-step tool orchestration, distinct from the single
   governed-tool-version CRUD/execute `ToolRegistryScreen` already owns. All
   in `tool_plans_api.py`, `/v1` prefix:

     - POST /v1/tool-plans                       create_tool_plan,   tool_plans_api.py:174
     - GET  /v1/tool-plans/{plan_id}              get_tool_plan,      tool_plans_api.py:233
     - POST /v1/tool-plans/{plan_id}/validate     validate_tool_plan, tool_plans_api.py:275
     - POST /v1/tool-plans/{plan_id}/execute      execute_tool_plan,  tool_plans_api.py:350
     - POST /v1/tool-plans/{plan_id}/cancel       cancel_tool_plan,   tool_plans_api.py:469
     - GET  /v1/tool-plans/{plan_id}/evidence     list_tool_plan_evidence, tool_plans_api.py:512

   None of these carry `{organization_id}` in the path, unlike every sibling
   domain above -- each handler calls `context.require_organization()` and
   scopes/verifies against the row's own `organization_id` instead
   (`enforce_organization`), so the org is derived from auth context alone.

   Every route is additionally gated by an edition entitlement check
   (`_deny_unless_entitled`, `tool_plans_api.py:138`) for capability
   `"multi_step_tool_plans"`, on top of the ordinary `require_roles` check.
   A denial from the entitlement gate is a plain 403 whose `detail` is the
   reason code itself (`ENTITLEMENT_EDITION_INSUFFICIENT` /
   `ENTITLEMENT_CAPABILITY_UNREGISTERED`, `edition_entitlements.py`), while a
   plain role denial's `detail` reads
   `"one of these roles is required: ..."` -- distinguishable string shapes,
   which is what `ToolPlansScreen` keys off of to show "this org's edition
   doesn't include multi-step tool plans" instead of a generic Forbidden.
--------------------------------------------------------------------------- */

/** `POST /v1/tool-plans` -- creates a `DRAFT` plan from one or more steps
 *  plus a budget (both default-filled server-side if omitted beyond
 *  `name`/`steps`). Matches the legacy `#tool-plan-form` submit, which only
 *  ever built a single-step plan -- the create form here does the same;
 *  the model supports many steps, but multi-step plan *authoring* in the UI
 *  is left as a documented future enhancement (see `ToolPlansScreen.tsx`). */
export function createToolPlan(
  body: ToolPlanCreate,
  signal?: AbortSignal,
): Promise<ToolPlanRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateToolPlan(body),
    async () => {
      return postJson<ToolPlanRead>(`/v1/tool-plans`, body, signal);
    },
  );
}

/** `GET /v1/tool-plans/{plan_id}` -- the plan plus its ordered steps. */
export function fetchToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ToolPlanDetailRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureToolPlan(planId),
    async () => {
      return get<ToolPlanDetailRead>(`/v1/tool-plans/${planId}`, signal);
    },
  );
}

/** `POST /v1/tool-plans/{plan_id}/validate` -- no body. Checks step
 *  ordering/dependencies/budget without executing anything; matches
 *  legacy's `plan-validate` button. */
export function validateToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ValidationResponse> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureValidateToolPlan(planId),
    async () => {
      return postJson<ValidationResponse>(`/v1/tool-plans/${planId}/validate`, {}, signal);
    },
  );
}

/** `POST /v1/tool-plans/{plan_id}/execute` -- no body. 409s when the plan's
 *  `status` is not `DRAFT`/`VALIDATED`; matches legacy's `plan-execute`
 *  button. */
export function executeToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ExecutionRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureExecuteToolPlan(planId),
    async () => {
      return postJson<ExecutionRead>(`/v1/tool-plans/${planId}/execute`, {}, signal);
    },
  );
}

/** `POST /v1/tool-plans/{plan_id}/cancel` -- no body, narrower roles than
 *  the rest of this file (`PlatformAdmin`/`ToolDeveloper` only, no
 *  `DataEngineer`). 409s when the plan is already `COMPLETED`/`CANCELLED`;
 *  matches legacy's `plan-cancel` button. */
export function cancelToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ToolPlanRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCancelToolPlan(planId),
    async () => {
      return postJson<ToolPlanRead>(`/v1/tool-plans/${planId}/cancel`, {}, signal);
    },
  );
}

export interface ToolPlanEvidenceQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/tool-plans/{plan_id}/evidence` -- paged `ExecutionRead` history
 *  for the plan; matches legacy's `plan-evidence` button. */
export function fetchToolPlanEvidence(
  planId: string,
  query: ToolPlanEvidenceQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ExecutionRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureToolPlanEvidence(planId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ExecutionRead>>(`/v1/tool-plans/${planId}/evidence?${params}`, signal);
    },
  );
}
