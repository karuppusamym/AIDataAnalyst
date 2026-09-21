# Module 01 — Identity and Tenancy

> Layer L1 · Schema `identity` · Owner: Platform Security

## 1. Purpose

Establishes *who is asking* and *on whose behalf*, and holds the enterprise isolation hierarchy every other module's data is scoped by. This module is the root of the trust model: if it is wrong, every other control is decoration.

It deliberately separates **authentication** (proving identity — here) from **authorization** (deciding permission — module 17). Conflating them is how systems end up with role checks scattered through feature code.

## 2. Jobs served

P4 (rotate a credential without an outage), and the tenancy foundation for every other job.

## 3. Responsibilities

- OIDC token verification: signature, issuer, audience, expiry, algorithm, subject.
- JWKS retrieval, caching, refresh, and pinned-key support.
- Configurable claim paths mapping tokens to organization, roles, and groups.
- The tenancy hierarchy: organization → line of business → data domain → project → datasource (five levels; `legal_entity` is not one, see §5).
- Principal registry: human users and workload identities. *Not built: a principal exists only as the verified claims of a request (§5).*
- Secret **reference** handling (never secret values): a reference is a column on the object that uses it, resolved by `src/aida/secrets.py`; there is no reference registry (§5).
- Development identity provider — local only, refused in production.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Permission decisions | 17 policy-governance |
| Secret **values** | Enterprise secret manager |
| User provisioning | Enterprise IdP |
| Session UI | 21 experience-shell |
| Audit writing | 20 observability-audit |

## 5. Domain model

```text
# Access axis (ADR-0018) -- the only axis with permission semantics
organization, workspace, workspace_membership, source_binding

# Classification axis (ADR-0018) -- grants nothing; policy keys on it
business_node, business_assignment

# Pre-ADR-0018 tenancy levels: still authoritative during the transition
line_of_business, data_domain, project

principal (kind: USER | WORKLOAD | SYSTEM)
principal_role, role_mapping
secret_reference (scheme, path, provider, never a value)
identity_provider_config
```

> **`legal_entity` is not part of this model** (corrected 2026-08-30). It appeared here and in
> ADR-0005 but has never existed in the schema — there is no `LegalEntity` model anywhere in
> `src/`. ADR-0018 withdraws it rather than deferring it: a legal-entity requirement is served
> by a classification attribute and a policy that denies across it (ADR-0018's 2026-09-13
> addendum retires the unenforced `isolation_boundary` until a hard wall is built with its
> enforcement). Two documents previously disagreed in writing about this and about `data_domain`
> (which *does* exist, with a model and a migration); both are now settled.
>
> **Direction of travel (ADR-0018).** `line_of_business` and `data_domain` move off the tenancy
> path and become `business_node` classification records with many-to-many, effective-dated
> assignments. The tenancy scope this module enforces becomes `(organization_id, workspace_id)`.
> Until that migration lands, the four levels above are what the repository base class scopes on.

> **Implementation status (2026-09-20).** The block above is the design, not the schema.
> `src/atlas/modules/identity_tenancy/models.py` defines 17 tables (as of 2026-09-20):
> `organization`, `line_of_business`, `data_domain`, `project`, `organization_integration_policy`,
> `workspace`, `workspace_membership`, `workspace_access_rule`, `source_binding`,
> `authorization_shadow_record`, `business_node`, `business_assignment`, `business_node_closure`,
> `business_node_rollup`, `delegation`, `revoked_token` and `cross_boundary_grant`. There is no
> `principal`, `principal_role`, `role_mapping`, `secret_reference` or `identity_provider_config`
> table. A principal exists only as the verified claims of a request (`src/aida/oidc.py`,
> `context_from_claims`); role mappings and the identity provider are settings
> (`oidc_role_mappings`, `identity_provider`); a secret reference is the `credential_reference`
> column of the datasource (`src/atlas/modules/connectivity/models.py`), resolved by
> `src/aida/secrets.py`.

`identity` is the only schema other modules may hold foreign keys into (ADR-0015).

## 5a. Roles

The role catalog is `PLATFORM_ROLES` in `src/aida/oidc.py`. Under OIDC these fifteen are the only roles a token can carry: `oidc_role_mappings` is a closed mapping (an external role with no entry grants nothing), and a mapped name outside the catalog is dropped. Roles are additive sets, not a hierarchy. `require_roles` checks that the caller holds one of the names listed on the route, so `Analyst` is not a superset of `Viewer` (as of 2026-09-20, 68 REST routes in the [surface-control matrix](../50-security/surface-control-matrix.md) list `Viewer` without `Analyst`).

| Role | Purpose |
|---|---|
| `PlatformAdmin` | Cross-tenant administrator. `enforce_organization` (`src/aida/security.py`) lets it cross the organization boundary by design; that is the deliberate exception to INV-5. The break-glass role for agent-contract edits (audited) and the only role that may engage or release the model kill switch. |
| `OrganizationAdmin` | Tenant administrator: lines of business, workspaces, members, access policies. |
| `MetadataAdmin` | Catalog and ingestion operator. |
| `DataAdmin` | Source owner: creates and tests datasources, quality. |
| `SemanticAdmin` | Author of glossary, metrics and context products. |
| `DataSteward` | Domain steward, and a reviewer of proposals. |
| `ToolDeveloper` | Authors governed tools. |
| `ToolConsumer` | Executes governed tools; execute-only. |
| `AgentDeveloper` | Model routes, AI assets, agent contracts, Ask. |
| `Reviewer` | Independent checker of governance reviews. |
| `MetadataReviewer` | Checker of structural-metadata proposals (relationship candidates, parsed lineage). |
| `Auditor` | Reads audit evidence and exports. Not strictly read-only and refused some evidence routes; see `00-product/02-personas-and-jobs.md` §2.6. |
| `Operations` | Runs the platform. |
| `Analyst` | Asks questions and consumes governed data. |
| `Viewer` | Read baseline. |

**Not grantable under OIDC.** Nine further names appear in `require_roles` guards but are not in the catalog, so no token can carry them: `ComplianceOfficer`, `DataEngineer`, `DataProductOwner`, `DataScientist`, `DataConsumer`, `MetadataIngestor`, `ModelRiskManager`, `ProjectAdmin` and `Steward`. As of 2026-09-20 they sit in the guards of 126 of the 554 surfaces in the surface-control matrix, always beside at least one grantable name. Under the development identity provider any string is accepted as a role (`src/aida/security.py`), and the UI's default development identity carries seventeen (the catalog's fifteen plus `ProjectAdmin` and `MetadataIngestor`), so a route guarded by one of these behaves differently in development than under OIDC. Example: `POST /v1/organizations/{organization_id}/business-nodes` (`src/atlas/modules/identity_tenancy/router.py`) accepts `PlatformAdmin`, `OrganizationAdmin`, `DataAdmin` or `Steward`; a `DataSteward` receives 403, and under the development provider a caller who sends the name `Steward` passes the role check.

**Worker labels.** `SchedulerWorker`, `MetadataWorker` and `ReaperWorker` are labels the platform's own background jobs give themselves (`principal_type` `WORKER`) so their audit rows are attributable. No guard names them and no token can carry them.

**Workspace roles.** Six lowercase roles (`viewer`, `analyst`, `steward`, `reviewer`, `auditor`, `workspace_owner`; `src/atlas/modules/identity_tenancy/schemas.py`) are a different axis: what a principal may do inside one workspace. They apply only where the workspace is in `ENFORCE` mode; a workspace in `SHADOW` records what it would have decided and allows.

**Bundles and personas.** An identity-provider group maps to a bundle of roles, and the bundles are defined in the deployment's `AIDA_OIDC_ROLE_MAPPINGS` (in the compose OIDC overlay, `compose.oidc.yaml`). Because roles do not imply one another, a steward bundle carries `Analyst` and `Viewer` beside the steward roles. The persona is a separate claim: `AIDA_OIDC_PERSONA_MAPPINGS` maps a group to one of five navigation personas and authorizes nothing.

> **Implementation status (2026-09-20).** A persona is only as usable as the role bundle its user signs in with, and the two are configured separately. A persona whose group is mapped, but for which no least-privilege bundle is defined, can only be signed in with some other bundle: a persona of `Auditor` with the role `Viewer` lands on a ledger it may not read. Check the bundles in `compose.oidc.yaml` for which personas are covered rather than assuming the persona mappings imply them.

## 6. Public interface

```python
# identity/api.py
def verify_token(raw: str) -> Principal | AuthFailure
def resolve_tenant_scope(principal: Principal, requested: TenantRef) -> TenantScope | Denial
def get_tenant_hierarchy(organization_id: OrgId) -> TenantTree
def resolve_secret(ref: SecretReference) -> ResolvedSecret     # bounded cache; never logged
def invalidate_secret_cache(ref: SecretReference) -> None
def list_principals(scope: TenantScope, page: Page) -> Page[PrincipalDTO]
```

`ResolvedSecret` is a context-managed value that is never serialized, never logged, and never placed in an exception message.

> **Implementation status (2026-09-20).** There is no `identity/api.py`, and none of these six functions exists under these names. The pieces that do the work are called directly: `OidcVerifier.verify` and `context_from_claims` (`src/aida/oidc.py`) behind `get_security_context`, `require_roles` and `enforce_organization` (`src/aida/security.py`); `enforce_not_revoked` (`src/aida/token_revocation.py`); and `SecretResolver.resolve` and `invalidate` (`src/aida/secrets.py`). There is no principal listing, because there is no principal table (§5); the hierarchy is read through the routes in §7.

## 7. HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/me` | Current principal, roles, tenant scope, and server-derived persona |
| GET | `/v1/organizations` | Tenant inventory |
| POST | `/v1/organizations/{organization_id}/lines-of-business` | Create a line of business |
| POST | `/v1/lines-of-business/{lob_id}/data-domains`, `/v1/lines-of-business/{lob_id}/projects` | Hierarchy management |
| GET | `/v1/organizations/{organization_id}/enforcement-readiness` | What would break if workspace authorization were enforced (`PlatformAdmin`, `OrganizationAdmin`, `Auditor`, `Operations`) |

> **Implementation status (2026-09-20).** The rows above are the routes that exist. There is no `/v1/legal-entities`, `/v1/lobs`, `/v1/projects` or `/v1/identity/posture`, and no runtime identity/secret readiness endpoint has been built. The module's 29 routes (as of 2026-09-20) are in `src/atlas/modules/identity_tenancy/router.py` and, with their required roles, in the generated [surface-control matrix](../50-security/surface-control-matrix.md); `GET /v1/me` is in `src/aida/persona_api.py`.

## 8. Events

Designed to emit `principal.created`, `principal.role_changed`, `tenant.created`, `tenant.archived`, `secret_reference.rotated`.

> **Implementation status (2026-09-20).** `tenant.created` is emitted under its `.v1` spellings: `organization.created.v1`, `line_of_business.created.v1`, `data_domain.created.v1` and `project.created.v1` (the event catalog records the rename). The router also emits `workspace.created.v1` and the `source_binding` request and decision events. Proposing an access policy or a workspace member emits `governance.review_requested.v1`; the decision emits `access_policy.activated.v1` or `access_policy.rejected.v1`, and `workspace_membership.approved.v1` or `workspace_membership.rejected.v1`. `principal.created`, `principal.role_changed`, `tenant.archived` and `secret_reference.rotated` are never emitted: there is no principal, role-mapping or secret-reference table, and no tenant archive route.

## 9. Dependencies

None. This is the root module.

## 10. Controls and invariants

| Control | Behaviour |
|---|---|
| INV-4 fail closed | Production refuses development identity, `env://` resolution, weak audit keys, insecure JWKS URLs |
| INV-5 tenant isolation | Every scope resolution defaults to deny; no unscoped helper exists. `PlatformAdmin` is the deliberate exception: `enforce_organization` lets it cross the organization boundary (§5a) |
| Token validation | Failure denies with a generic 401. Expiry is the one reason named (`OidcTokenExpired`), so a client can tell "sign in again" from a token that can never work; signature, audience, issuer and revocation stay generic |
| Secret handling | Inline DSNs rejected; exactly one configured provider; bounded cache; rotation invalidation |
| JWKS | Cached with TTL; refresh on unknown `kid`; pinned keys supported for air-gapped operation |

> **Implementation status (2026-09-20).** The two access changes this module makes that were not under INV-8 (maker != checker) now are (R11-AUD02). Review types `ACCESS_POLICY` and `WORKSPACE_MEMBERSHIP` are risk tier T3 (`src/aida/review_risk_tiers.py`) and have review adapters (`src/aida/access_change_review.py`, registered in `src/aida/semantic_api.py`), so both routes in `src/atlas/modules/identity_tenancy/router.py` file a proposal and a review instead of making the change. `POST /v1/organizations/{organization_id}/access-policies` (`PlatformAdmin`, `OrganizationAdmin`) always creates the policy `DRAFT`, which the policy engine never loads, and answers 201 with the policy and a `governance_review_id`; a body asking for `status: ACTIVE` is a 422. Approving the review activates the policy, rejecting it marks it `REJECTED`. `POST /v1/workspaces/{workspace_id}/members` (`PlatformAdmin`, `OrganizationAdmin`, `DataAdmin`) files every role, not only `workspace_owner`, as a `PENDING_APPROVAL` membership and answers 201 with the membership and a `governance_review_id`. The tier is on the type, not the role, and `analyst` and `steward` already carry `READ_DATA`. A pending or rejected member holds no role, a rejected principal can be proposed again, and approval records the approver as `granted_by`. Both reviews are decided on `POST /v1/governance/reviews/{review_id}/decision` by a principal holding `PlatformAdmin`, `DataSteward` or `Reviewer` who is not the proposer (409), never by an agent (403), and, for a membership, never the member (409). `OrganizationAdmin` and `DataAdmin` can propose but cannot decide, so an organization needs a second principal in one of the deciding roles before its first policy or member takes effect. Both proposal and decision are audited (`ACCESS_POLICY_CREATED` and `ACCESS_POLICY_ACTIVATED` or `_REJECTED`; `WORKSPACE_MEMBER_PROPOSED` and `WORKSPACE_MEMBER_ADDED` or `_REJECTED`, the last now written when the member takes effect). Approving a policy does not retire an earlier `ACTIVE` version of the same `code`; versions are evaluated independently, as before. `create_workspace` still seats its creator as `workspace_owner` in the same call. Covered by `tests/test_access_change_review.py` against a SQLite schema. Deployed 2026-09-21: the running API refuses a policy created `ACTIVE` with a 422; the review loop itself has not been run against PostgreSQL. Source bindings and cross-boundary grants go through a maker-checker decision as well. The review-queue side of the same finding is in `50-security/04-compliance-and-evidence.md` §4.

## 11. Current state → target

| Aspect | Now | Target |
|---|---|---|
| OIDC verification | Implemented — signature, issuer, audience, time, algorithm; configurable claim paths; JWKS cache/refresh; pinned keys | Certify against the bank issuer and group contract |
| Development identity | Implemented, production-refused | Unchanged |
| Secret references | Strict parsing; one configured provider; adapter contract; production rejects `env://`. Only the env and vault providers are built in; the settings also accept cyberark, aws-sm, azure-kv and gcp-sm, and nothing resolves them until an adapter is registered | Register and certify the bank Vault/CyberArk/cloud adapter |
| Workload identity | Partly implemented — the MCP endpoint admits only `AGENT` and `SERVICE_ACCOUNT` principal types outside development (`mcp_require_workload_identity`, default on; `src/aida/mcp_server.py`). No connector agents exist | Required for connector agents; certify the principal-type claim with the bank issuer |
| Token revocation / replay policy | Implemented (ID-4) — a revoked token is refused on its next use, and a lookup that cannot be answered denies (`src/aida/token_revocation.py`) | Certify against the bank issuer's logout and compromise flow before production |
| Break-glass | Not implemented as a general process; `PlatformAdmin` is the audited break-glass role for agent-contract edits only | Required before production |

## 12. Open work

| ID | Item | Priority |
|---|---|---|
| ID-1 | Register and certify the bank secret-manager adapter | P0 |
| ID-2 | Bank OIDC issuer, claim, and group certification | P0 |
| ID-3 | Workload identity for agents and connector agents (the MCP gate is delivered; connector agents are not built) | P0 |
| ID-4 | Token revocation and replay policy (delivered 2026-08-30; certification remains) | P0 |
| ID-5 | Break-glass process with audited elevation | P1 |
| ID-6 | Rotation drill under load | P1 |
| ID-7 | Bulk onboarding and enterprise entitlement feed integration | P1 |
