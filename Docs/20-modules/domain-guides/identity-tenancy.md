# Domain guide — identity_tenancy

> Orientation, not specification. The full spec is
> [`../01-identity-and-tenancy.md`](../01-identity-and-tenancy.md); the generated
> shape of the module is in
> [`../../10-architecture/14-generated-architecture-map.md`](../../10-architecture/14-generated-architecture-map.md).

**Code:** `src/atlas/modules/identity_tenancy/` · **Spec:** [`../01-identity-and-tenancy.md`](../01-identity-and-tenancy.md)

## What it owns

Who is asking, and on behalf of which part of the bank. The largest of the five
contexts by owned state — 19 tables, in four families:

- **The tenant hierarchy** — `organization`, `line_of_business`, `data_domain`,
  `project`, `isolation_boundary`, `organization_integration_policy`.
- **Workspaces** — `workspace`, `workspace_membership`, `workspace_access_rule`,
  `source_binding`, `authorization_shadow_record`. A workspace is where work
  happens; a source binding is a *requested and decided* right to reach a source
  from one, not an implicit consequence of being in the same tenant.
- **The business hierarchy** — `business_node`, `business_assignment`,
  `business_assignment_rule`, `business_node_closure`, `business_node_rollup`.
  The closure and rollup tables are projections kept correct as the tree changes.
- **Delegated and revoked authority** — `delegation`, `revoked_token`,
  `cross_boundary_grant`.

## Invariants it must uphold

- **INV-5, tenant isolation is total.** This context defines what a tenant *is*,
  so a defect here is not one leak, it is the shape every other module's leak
  would take.
- **A cross-boundary grant is per-read, not ambient.** Reaching data across a
  domain boundary requires a grant that names the direction; the absence of a
  grant must be indistinguishable from the absence of the data (ADR-0017).
- **INV-7, attributability.** The router audits its mutating routes — twelve
  audit call sites, more than any of the other four contexts, because almost
  everything here is somebody granting somebody else access.
- **Lazy default rows are not decisions.** The default `data_domain` a line of
  business gets on first read carries no caller-supplied value, so it records no
  actor. See INV-7's ratified scope note in
  [`../../10-architecture/01-principles-and-invariants.md`](../../10-architecture/01-principles-and-invariants.md).

## Entry points

- **HTTP** — 28 routes, the largest surface of the five. Organizations, lines of
  business, data domains, projects, cross-boundary grants, workspaces,
  memberships, source bindings and their decisions, the business tree and its
  rollup, access policies, and two read-only probes (`POST /v1/authorization-probes`
  and workspace authorization simulation) that answer *would this be allowed*
  without doing it.
- **Mounted through a shim.** `aida.main` still imports this router as
  `aida.workspace_api`, not through `atlas.modules.identity_tenancy.api`. That
  shim, its one caller, and the condition for removing it are recorded in
  [`../../40-engineering/09-compatibility-shim-register.md`](../../40-engineering/09-compatibility-shim-register.md).
- **In-process** — `Organization`, `Workspace`, `DataDomain` and friends are
  imported across most of the tree, largely through the `aida.models` re-export
  block.

## What it deliberately does not own

- **Authentication.** Establishing *who* the caller is happens in `aida.security`
  and `aida.oidc`. This context answers what that identity is entitled to inside
  the tenant model, given an already-authenticated principal.
- **The policy decision itself.** `aida.policy_engine` and
  `aida.authorization_gate` evaluate and enforce; this context stores the
  memberships, rules and grants they evaluate against. Keeping the store and the
  evaluator apart is what lets the gate be the single choke point.
- **Secrets.** `aida.secrets` owns secret material; rows here hold references.
- **Persona and navigation.** A persona is an experience-shell concept
  (`ui-next/`), separated from work area during the 2026-09-05 review. It is not
  a tenancy object and must not become one.

## Current shape, honestly

`models.py`, `schemas.py` and `router.py` hold real content; `service.py`,
`repository.py`, `contracts.py`, `events.py` and `workers/` are empty scaffolds.
The router therefore holds this context's business rules as well as its
translation layer — the largest single file of the five, and the most obvious
candidate for the service/repository split the module layout anticipates.

The `identity_tenancy module privacy` import-linter contract protects the
internals, with `aida.models`, `aida.schemas` and `aida.workspace_api` named as
permitted importers because they are the sanctioned compatibility shims.
