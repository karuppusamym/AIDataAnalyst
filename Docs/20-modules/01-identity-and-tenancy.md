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
- The tenancy hierarchy: organization → legal entity → LOB → data domain → project → datasource.
- Principal registry: human users and workload identities.
- Secret **reference** management (never secret values).
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
organization, workspace, workspace_membership, source_binding, isolation_boundary

# Classification axis (ADR-0018) -- grants nothing; policy keys on it
business_node, business_assignment, business_assignment_rule

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
> either by an `isolation_boundary` or by a classification attribute, both of which that ADR
> defines. Two documents previously disagreed in writing about this and about `data_domain`
> (which *does* exist, with a model and a migration); both are now settled.
>
> **Direction of travel (ADR-0018).** `line_of_business` and `data_domain` move off the tenancy
> path and become `business_node` classification records with many-to-many, effective-dated
> assignments. The tenancy scope this module enforces becomes `(organization_id, workspace_id)`.
> Until that migration lands, the four levels above are what the repository base class scopes on.

`identity` is the only schema other modules may hold foreign keys into (ADR-0015).

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

## 7. HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/me` | Current principal, roles, tenant scope |
| GET | `/v1/organizations` | Tenant inventory |
| POST | `/v1/organizations/{id}/legal-entities` | Create legal entity |
| POST | `/v1/lobs`, `/v1/projects` | Hierarchy management |
| GET | `/v1/identity/posture` | Runtime identity/secret readiness (operator) |

## 8. Events

Emits `principal.created`, `principal.role_changed`, `tenant.created`, `tenant.archived`, `secret_reference.rotated`.

## 9. Dependencies

None. This is the root module.

## 10. Controls and invariants

| Control | Behaviour |
|---|---|
| INV-4 fail closed | Production refuses development identity, `env://` resolution, weak audit keys, insecure JWKS URLs |
| INV-5 tenant isolation | Every scope resolution defaults to deny; no unscoped helper exists |
| Token validation | Failure denies **without leaking which check failed** |
| Secret handling | Inline DSNs rejected; exactly one configured provider; bounded cache; rotation invalidation |
| JWKS | Cached with TTL; refresh on unknown `kid`; pinned keys supported for air-gapped operation |

## 11. Current state → target

| Aspect | Now | Target |
|---|---|---|
| OIDC verification | Implemented — signature, issuer, audience, time, algorithm; configurable claim paths; JWKS cache/refresh; pinned keys | Certify against the bank issuer and group contract |
| Development identity | Implemented, production-refused | Unchanged |
| Secret references | Strict parsing; one configured provider; adapter contract; production rejects `env://` | Register and certify the bank Vault/CyberArk/cloud adapter |
| Workload identity | Not implemented | Required for connector agents and MCP consumers |
| Token revocation / replay policy | Not implemented | Required before production |
| Break-glass | Not implemented | Required before production |

## 12. Open work

| ID | Item | Priority |
|---|---|---|
| ID-1 | Register and certify the bank secret-manager adapter | P0 |
| ID-2 | Bank OIDC issuer, claim, and group certification | P0 |
| ID-3 | Workload identity for agents and connector agents | P0 |
| ID-4 | Token revocation and replay policy | P0 |
| ID-5 | Break-glass process with audited elevation | P1 |
| ID-6 | Rotation drill under load | P1 |
| ID-7 | Bulk onboarding and enterprise entitlement feed integration | P1 |
