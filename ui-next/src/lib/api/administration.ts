/* ---------------------------------------------------------------------------
   Administration — tenancy, access and the bindings between them.

   The onboarding chain (organization, line of business, project,
   datasource registration) and the source-binding request/decision pair;
   ABAC access policies with the authorization simulator; workspace
   membership; time-bounded delegations; and BI connections with their
   artifact imports.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import type {
  AccessPolicyCreate,
  AccessPolicyRead,
  AuthorizationSimulationRead,
  AuthorizationSimulationRequest,
  BiArtifactImportRead,
  BiArtifactImportRequest,
  BiConnectionCreate,
  BiConnectionRead,
  DataSourceCreate,
  DataSourceRead,
  DelegationCreate,
  DelegationRead,
  LineOfBusinessCreate,
  LineOfBusinessRead,
  OrganizationCreate,
  OrganizationRead,
  ProjectCreate,
  ProjectRead,
  SourceBindingCreate,
  SourceBindingDecision,
  SourceBindingRead,
  WorkspaceCreate,
  WorkspaceMembershipCreate,
  WorkspaceMembershipRead,
  WorkspaceRead,
} from "../types";
import type { PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   Administration -- nav id `administration`, the tenant/onboarding wizard
   ported from the legacy portal's `administration-view` (`ui/app.js`'s
   `#organization-form`/`#lob-form`/`#project-form`/`#datasource-form`
   handlers). Every call below hits a real, already-merged route the legacy
   portal itself posts to -- the deliberate four-step hierarchy the backend
   enforces (organization -> line of business -> project -> datasource), not
   an invented "setup" API. `fetchOrganizations`, `fetchOrgProjects` and
   `fetchOrgDatasources` in `./identity.ts` already cover this screen's
   organization, project and datasource reads; `fetchOrgLinesOfBusiness`
   below is the one read nothing existing exposed yet.
--------------------------------------------------------------------------- */

/** `POST /v1/organizations` (`create_organization`, `api.py:584`) -- the
 *  platform-admin-gated tenant creation the legacy portal's
 *  `#organization-form` posts to. Requires the `PlatformAdmin` role. */
export function createOrganization(
  body: OrganizationCreate,
  signal?: AbortSignal,
): Promise<OrganizationRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateOrganization(body),
    async () => {
      return postJson<OrganizationRead>("/v1/organizations", body, signal);
    },
  );
}

/** Create an access workspace. Projects remain a separate technical axis;
 * sources are attached to this workspace with `requestSourceBinding`. */
export function createWorkspace(
  organizationId: string,
  body: WorkspaceCreate,
  signal?: AbortSignal,
): Promise<WorkspaceRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateWorkspace(organizationId, body),
    async () => {
      return postJson<WorkspaceRead>(
        `/v1/organizations/${organizationId}/workspaces`,
        body,
        signal,
      );
    },
  );
}

/** Request maker-checker-governed access from a workspace to a source. */
export function requestSourceBinding(
  workspaceId: string,
  body: SourceBindingCreate,
  signal?: AbortSignal,
): Promise<SourceBindingRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRequestSourceBinding(workspaceId, body),
    async () => {
      return postJson<SourceBindingRead>(
        `/v1/workspaces/${workspaceId}/source-bindings`,
        body,
        signal,
      );
    },
  );
}

/** `GET /v1/organizations/{organization_id}/lines-of-business`
 *  (`list_lines_of_business`, `api.py:463`) -- the one hierarchy read
 *  `fetchOrgProjects`/`fetchOrgDatasources` (`./identity.ts`) don't already
 *  cover; feeds
 *  both the "Add project" line-of-business picker and the scope-summary
 *  tree in `AdministrationScreen`. */
export function fetchOrgLinesOfBusiness(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<LineOfBusinessRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureOrgLinesOfBusiness(organizationId),
    async () => {
      return get<PageOf<LineOfBusinessRead>>(
        `/v1/organizations/${organizationId}/lines-of-business?limit=500`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/lines-of-business`
 *  (`create_line_of_business`, `api.py:677`). Requires `PlatformAdmin` or
 *  `OrganizationAdmin`. */
export function createLineOfBusiness(
  organizationId: string,
  body: LineOfBusinessCreate,
  signal?: AbortSignal,
): Promise<LineOfBusinessRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateLineOfBusiness(organizationId, body),
    async () => {
      return postJson<LineOfBusinessRead>(
        `/v1/organizations/${organizationId}/lines-of-business`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/lines-of-business/{lob_id}/projects` (`create_project`,
 *  `api.py:901`). `body.data_domain_id` is left unset on purpose --
 *  `create_project`'s own `resolve_domain` falls back to the line of
 *  business's default domain when it is omitted (`api.py:922`), and this
 *  screen has no data-domain picker of its own (a stated scope cut, see
 *  `AdministrationScreen`'s file-top comment). Requires `PlatformAdmin` or
 *  `ProjectAdmin`. */
export function createProject(
  lobId: string,
  body: ProjectCreate,
  signal?: AbortSignal,
): Promise<ProjectRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateProject(lobId, body),
    async () => {
      return postJson<ProjectRead>(`/v1/lines-of-business/${lobId}/projects`, body, signal);
    },
  );
}

/** `POST /v1/projects/{project_id}/datasources` (`create_datasource`,
 *  `api.py:1021`) -- the same registration path `SourcesScreen`'s fleet is
 *  read back from (via `fetchOrgDatasources`), scoped to one project.
 *  `credential_reference` must reference the configured secret provider
 *  (`_validate_datasource_create`, `api.py:960`); a raw connection string
 *  comes back as a 422, same as the legacy portal. Requires `PlatformAdmin`
 *  or `DataAdmin`. No post-registration connectivity test is fired here
 *  (`POST /v1/datasources/{id}/test`, `api.py:1299`, is the legacy portal's
 *  own separate follow-up call, `ui/app.js:1683`) -- a stated scope cut, not
 *  a silently dropped step. */
export function registerDatasource(
  projectId: string,
  body: DataSourceCreate,
  signal?: AbortSignal,
): Promise<DataSourceRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRegisterDatasource(projectId, body),
    async () => {
      return postJson<DataSourceRead>(`/v1/projects/${projectId}/datasources`, body, signal);
    },
  );
}

/* ---------------------------------------------------------------------------
   ABAC access policies + authorization simulation -- nav id `access-policies`.
   Both routes live in `workspace_api.py` despite the domain name (confirmed
   by direct source read, not `api.py`):

     - GET  /v1/organizations/{organization_id}/access-policies      list_access_policies, workspace_api.py:511
     - POST /v1/organizations/{organization_id}/access-policies       create_access_policy, workspace_api.py:527
     - POST /v1/workspaces/{workspace_id}/authorization-simulations   simulate_authorization, workspace_api.py:620

   `subject_match` / `resource_match` / `transform` / `condition` / `subjects`
   are genuinely free-form policy data (matches the legacy portal's
   `#abac-policy-form` / `#abac-simulate-form`, `control-center.js:201-202`) --
   the screen parses their raw JSON textareas client-side rather than
   building a structured editor for them.
--------------------------------------------------------------------------- */

export interface AccessPolicyQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/access-policies` -- visible to
 *  `PlatformAdmin`/`OrganizationAdmin`/`DataAdmin`/`Reviewer`, a wider set
 *  than who may create one below. Multiple rows can share a `code`: creating
 *  again under the same code auto-increments `version` server-side rather
 *  than replacing the row, so the list carries `version` per row. */
export function fetchAccessPolicies(
  organizationId: string,
  query: AccessPolicyQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AccessPolicyRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureAccessPolicies(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 200));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<AccessPolicyRead>>(
        `/v1/organizations/${organizationId}/access-policies?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/access-policies` -- narrower
 *  than the list above (`PlatformAdmin`/`OrganizationAdmin` only). A new
 *  policy always starts `DRAFT` unless the caller explicitly sets
 *  `status: "ACTIVE"` in the body. */
export function createAccessPolicy(
  organizationId: string,
  body: AccessPolicyCreate,
  signal?: AbortSignal,
): Promise<AccessPolicyRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateAccessPolicy(organizationId, body),
    async () => {
      return postJson<AccessPolicyRead>(`/v1/organizations/${organizationId}/access-policies`, body, signal);
    },
  );
}

/** `POST /v1/workspaces/{workspace_id}/authorization-simulations` --
 *  "who could see this?" against the live policy engine, open to any
 *  workspace member. `body.workspace_id` must match the path param or the
 *  real endpoint returns 422; callers must set both from the same picked
 *  workspace id. */
export function simulateAuthorization(
  workspaceId: string,
  body: AuthorizationSimulationRequest,
  signal?: AbortSignal,
): Promise<AuthorizationSimulationRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSimulateAuthorization(workspaceId, body),
    async () => {
      return postJson<AuthorizationSimulationRead>(
        `/v1/workspaces/${workspaceId}/authorization-simulations`,
        body,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   Workspace membership, source-binding decisions, BI/Tableau lineage
   connections -- the piece of the legacy Enterprise Control Center's
   `renderAccess`/`renderBi` that `fetchOrgWorkspaces`/
   `fetchWorkspaceSourceBindings` (`./identity.ts`) and `createWorkspace`/
   `requestSourceBinding` (above) do not cover: workspace *members* (`workspace_api.py:160-208`),
   the *decision* half of the maker-checker source-binding flow
   (`workspace_api.py:293`, `createWorkspace`/`requestSourceBinding` only
   create/request), and BI connections (`bi_api.py`, which nothing else in
   this client touches).
--------------------------------------------------------------------------- */

/** `POST /v1/workspaces/{workspace_id}/members` (`workspace_api.py:160`,
 *  `_ADMIN` only: PlatformAdmin/OrganizationAdmin/DataAdmin). 409s if the
 *  principal already has a membership in this workspace. */
export function addWorkspaceMember(
  workspaceId: string,
  body: WorkspaceMembershipCreate,
  signal?: AbortSignal,
): Promise<WorkspaceMembershipRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureAddWorkspaceMember(workspaceId, body),
    async () => {
      return postJson<WorkspaceMembershipRead>(
        `/v1/workspaces/${workspaceId}/members`,
        body,
        signal,
      );
    },
  );
}

/** `GET /v1/workspaces/{workspace_id}/members` (`workspace_api.py:207`,
 *  `_ANY_MEMBER`: the `_ADMIN` roles plus Steward/Analyst/Reviewer). No
 *  limit/offset -- the route returns every membership unpaginated. */
export function fetchWorkspaceMembers(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PageOf<WorkspaceMembershipRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureWorkspaceMembers(workspaceId),
    async () => {
      return get<PageOf<WorkspaceMembershipRead>>(
        `/v1/workspaces/${workspaceId}/members`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   PG-4: delegations -- time-bounded, audited handoff of a principal's own
   governance roles to another principal (e.g. a steward or reviewer going on
   leave). Real, already-merged routes (`delegation_api.py`); `DelegationsScreen`
   is the first frontend for this. `status` on the wire is only `"ACTIVE"` or
   `"REVOKED"` -- nothing flips the column at expiry by design (the module's
   own docstring), so the screen computes an "expired" state client-side from
   `status === "ACTIVE"` plus `expires_at` having passed.
--------------------------------------------------------------------------- */

export interface DelegationsQuery {
  delegatePrincipalId?: string | null;
  delegatorPrincipalId?: string | null;
  /** Server-side status filter only ever sees `"ACTIVE"`/`"REVOKED"` -- the
   *  "expired" split is computed client-side, never sent on the wire. */
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/delegations` (`delegation_api.py::list_delegations`).
 *  All three filters are optional and omitted from the query string when unset
 *  (never sent as an empty string). */
export function fetchDelegations(
  organizationId: string,
  query: DelegationsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<DelegationRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDelegations(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.delegatePrincipalId) params.set("delegate_principal_id", query.delegatePrincipalId);
      if (query.delegatorPrincipalId) params.set("delegator_principal_id", query.delegatorPrincipalId);
      if (query.status) params.set("status", query.status);
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<DelegationRead>>(
        `/v1/organizations/${organizationId}/delegations?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/delegations` (`delegation_api.py::grant_delegation`).
 *  422s on self-delegation, on delegating a role the caller does not itself
 *  hold, if `expires_at <= starts_at`, or if the window exceeds 180 days --
 *  all surfaced as-is via `postJson`'s `ApiError`. */
export function grantDelegation(
  organizationId: string,
  body: DelegationCreate,
  signal?: AbortSignal,
): Promise<DelegationRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureGrantDelegation(organizationId, body),
    async () => {
      return postJson<DelegationRead>(
        `/v1/organizations/${organizationId}/delegations`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/delegations/{delegation_id}/revoke` (`delegation_api.py::revoke_delegation`).
 *  409s if the delegation is not currently ACTIVE, 403s if the caller is
 *  neither the original delegator nor a platform admin. No request body --
 *  `{}` matches this file's own convention of never sending an
 *  optional-looking empty POST without an explicit body. */
export function revokeDelegation(
  delegationId: string,
  signal?: AbortSignal,
): Promise<DelegationRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRevokeDelegation(delegationId),
    async () => {
      return postJson<DelegationRead>(`/v1/delegations/${delegationId}/revoke`, {}, signal);
    },
  );
}

/** `POST /v1/source-bindings/{binding_id}/decision` (`workspace_api.py:293`,
 *  roles `_ADMIN` + Reviewer) -- the maker-checker approve/reject a pending
 *  binding from `requestSourceBinding` above needs. The endpoint 403/409s
 *  when the decider is the same principal who requested the binding; that
 *  detail string is surfaced as-is by `postJson`'s `ApiError`, not swallowed. */
export function decideSourceBinding(
  bindingId: string,
  body: SourceBindingDecision,
  signal?: AbortSignal,
): Promise<SourceBindingRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDecideSourceBinding(bindingId, body),
    async () => {
      return postJson<SourceBindingRead>(
        `/v1/source-bindings/${bindingId}/decision`,
        body,
        signal,
      );
    },
  );
}

export interface BiConnectionQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{project_id}/bi-connections` (`bi_api.py:226`) -- roles
 *  add DataSteward/Auditor/Viewer on top of the create roles below. 403s via
 *  `_require_bi_integration` when the organization's integration policy has
 *  not enabled `"bi"`; that detail is a legitimate expected state for orgs
 *  that have not opted in, not a bug, and is surfaced the same way. */
export function fetchProjectBiConnections(
  projectId: string,
  opts: BiConnectionQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<BiConnectionRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureProjectBiConnections(projectId, opts),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(opts.limit ?? 100));
      params.set("offset", String(opts.offset ?? 0));
      return get<PageOf<BiConnectionRead>>(
        `/v1/projects/${projectId}/bi-connections?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/projects/{project_id}/bi-connections` (`bi_api.py:171`, roles
 *  PlatformAdmin/DataAdmin/MetadataAdmin) -- registers a Tableau/Power BI/
 *  Looker connection against one of the project's own datasources. */
export function createBiConnection(
  projectId: string,
  body: BiConnectionCreate,
  signal?: AbortSignal,
): Promise<BiConnectionRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateBiConnection(projectId, body),
    async () => {
      return postJson<BiConnectionRead>(
        `/v1/projects/${projectId}/bi-connections`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/bi-connections/{connection_id}/artifact-imports` (`bi_api.py:258`,
 *  same create roles) -- `body.artifact` is the raw exported BI artifact
 *  JSON, same as the legacy `#bi-import-form`'s textarea (`control-center.js`);
 *  parsing that text into JSON is the caller's job, not this function's. */
export function importBiArtifact(
  connectionId: string,
  body: BiArtifactImportRequest,
  signal?: AbortSignal,
): Promise<BiArtifactImportRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureImportBiArtifact(connectionId, body),
    async () => {
      return postJson<BiArtifactImportRead>(
        `/v1/bi-connections/${connectionId}/artifact-imports`,
        body,
        signal,
      );
    },
  );
}
