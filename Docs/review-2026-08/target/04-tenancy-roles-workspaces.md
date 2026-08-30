# Target Design 4 — Tenancy, roles, workspaces, governance

Status: Proposal, clean-room. This is the largest single departure from the current
design; the argument for it is in `00-design-brief.md` §2.

---

## 1. The three axes, restated as schema

```
ACCESS AXIS (short, stable, hard boundary)
  organization
  └── workspace            ← the grant boundary
      ├── membership (principal, role)
      ├── source_binding[]
      └── project[]

ORGANISATIONAL AXIS (versioned classification, no permission semantics)
  business_node   LOB / SUB_LOB / DOMAIN / SUB_DOMAIN / CONCEPT, self-referencing
  business_assignment   many-to-many onto assets, with effective dates

TECHNICAL AXIS (discovered, not designed)
  datasource → catalog → schema → table/view/procedure → column
```

**Nothing in the organisational axis grants access.** It *informs* access through
attribute-based policy, which is a different and much more flexible relationship.
A domain is a label a policy can key on, not a container a permission lives in.

**Isolation boundaries** are the escape hatch for genuine Chinese walls:

```
isolation_boundary   id, organization_id, name, mode ∈ {STRICT, ADVISORY}
workspace.isolation_boundary_id   nullable
```

A `STRICT` boundary means no cross-boundary grant is possible at all — not by policy,
not by exception, not by an administrator. Keep the set small and explicit; a bank
typically has a handful, not one per LOB. Everything softer is a policy.

### Why this is worth the migration

| Event | Today (LOB/domain in the tenancy path) | Proposed |
|---|---|---|
| Bank reorganises LOBs | Data migration across every governed table, graph node, and audit record; historical audit describes an org chart that no longer exists | Update the classification tree; add assignments with new effective dates; history still resolves correctly |
| Asset belongs to two domains | Not expressible | Two assignments |
| Domain spans three workspaces | Not expressible | Normal |
| New workspace for a project team | New tenancy subtree | One row plus memberships |
| "Everything under Retail Banking" | Tenancy-path prefix query, breaks on reorg | Recursive CTE over the tree at a point in time |

---

## 2. Roles

Two layers, deliberately. Global roles say *which surfaces you can open*; workspace
roles say *what you can do inside a workspace*. This is the shape Collibra, Purview
and Alation all converged on independently, which is decent evidence it is right.

### Global roles (organization-scoped, few)

| Role | Grants |
|---|---|
| `platform_admin` | Platform configuration, connectors, model routes, secret providers. **Cannot read business metadata or approve governed objects** |
| `security_admin` | Policies, classifications, isolation boundaries, break-glass. Cannot publish semantics or tools |
| `governance_admin` | Business graph, glossary standards, review routing, certification standards |
| `auditor` | Read-only across audit ledger, evidence, versions, and refusals. Cannot mutate anything |
| `workspace_creator` | May create workspaces |

The separation of `platform_admin` from `security_admin`, and of both from anything
that can approve a governed object, is deliberate — it is how you answer "can one
person compromise the system alone" with a no.

### Workspace roles

| Role | Read | Propose | Approve | Execute | Admin |
|---|---|---|---|---|---|
| `viewer` | ✓ | | | | |
| `analyst` | ✓ | ✓ (analyses, tool drafts) | | ✓ (published tools) | |
| `steward` | ✓ | ✓ (meaning, glossary, wiki, lineage, domains) | | ✓ | |
| `reviewer` | ✓ | | ✓ | ✓ | |
| `workspace_owner` | ✓ | ✓ | ✓ (except own proposals) | ✓ | ✓ (membership, bindings, budgets) |

Constraints:

- **Maker ≠ checker holds regardless of role.** A `workspace_owner` who proposes
  cannot approve that proposal. This is already enforced in code and tested; do not
  weaken it for convenience.
- **Roles are additive across memberships**, and a deny from policy always wins over
  a grant from a role.
- **Personas are derived from IdP group claims**, not chosen in the UI. The current
  design already says this; keep it.

---

## 3. Attribute-based policy

RBAC alone stops scaling at exactly the point a bank estate becomes interesting.
Databricks' ABAC is the reference implementation to learn from: policies key on
governed tags, tags inherit down the object hierarchy, and one policy applies to
every current *and future* matching object.

```
policy
  id, version, organization_id, name, effect ∈ {ALLOW, DENY, MASK, FILTER}
  subject_match      role, group, principal kind (HUMAN | AGENT | SERVICE),
                     purpose, workspace, isolation boundary
  resource_match     classification, business_node (with descendants),
                     certification status, source, schema pattern, tag
  action_match       READ_METADATA | READ_DATA | PROPOSE | APPROVE | EXECUTE_TOOL |
                     CONSUME_CONTEXT | EXPORT
  transform          masking profile, row filter expression
  condition          time window, break-glass state, freshness state, quality state
```

Five things this makes possible that RBAC alone does not:

1. **`principal_kind = AGENT` as a first-class attribute.** "Humans may see full
   account numbers; agents never do" is one policy. Today this is inexpressible, and
   it is the single most-requested control once agents reach production.
2. **Purpose-bound access.** The same analyst gets different visibility under
   *fraud investigation* than under *marketing analytics*. Purpose is declared per
   session and recorded in the audit record.
3. **Future-proofing by classification.** A policy on `classification = PII` covers
   the column discovered next Tuesday, with no administrative action. This is the
   whole argument for governance-by-classification.
4. **Quality- and freshness-conditioned access.** "Do not serve this metric to an
   agent when its quality incident is open." The current design names this as
   whitespace D4/W1 and does not build it; ABAC conditions make it a policy rather
   than a subsystem.
5. **Deny as a hard ceiling.** A DENY cannot be overridden by any role, including
   owner or admin. Atlan's Purposes model has exactly this property and it is the
   right call.

**Enforcement points**, all of which must consult the same policy engine: retrieval
(before ranking), the query gateway (per object, per column), tool invocation,
context-product consumption, wiki page read, and export.

---

## 4. Granting a workspace

The requirement — *"how can grant the workspace"* — deserves a concrete flow, because
this is where governance products become unusable.

**Creating a workspace** (`workspace_creator` or `governance_admin`):
name, purpose, isolation boundary (usually none), initial owner, budget ceiling.
One screen. This must be minutes, not a project.

**Binding a source** — the two-party step, and the one that must not be skippable:

1. Workspace owner requests a binding: which datasource, which schemas, what purpose,
   for how long.
2. The request routes to the **source owner** — the steward accountable for that
   datasource, not a central admin queue. Central queues are where these die.
3. Approval creates a `source_binding` carrying scope, permitted classifications,
   masking profile, cost ceiling, and expiry.
4. **Bindings expire.** Default 12 months, renewal is a review, not a rubber stamp.
   Expiry is the mechanism that stops entitlement creep, and it is the thing almost
   every platform omits.

**Adding a member**: owner assigns a role; if the workspace touches restricted
classifications, membership additionally requires the security policy's conditions to
be met (training, attestation, group membership) — expressed as policy conditions,
not as a manual checklist.

**Requesting access to something not yet bound**: a consumer browsing the catalogue
hits an asset outside their workspace's bindings. They see it exists, see its business
description, and see a request button — they do not see its data or its detailed
structure. The request carries a purpose and routes to the source owner. This is the
Purview data-product request pattern (one request instead of fifteen) and it is worth
copying.

**Cross-boundary access** requires an explicit, expiring, audited
`cross_boundary_grant` — the current ADR-0017 already designs this well. Keep the
mechanism; re-key it from tenancy path to workspace + classification. Without a grant,
cross-boundary results are returned as `withheld: no_grant` — visible, counted,
honestly labelled, not silently dropped.

---

## 5. Maker–checker and the review queue

The existing architecture makes exactly one correct, non-obvious choice here: **one
platform-level approval service, one unified review queue across every governed object
type.** Feature modules cannot implement their own approval. Keep this. Every product
that lets features grow their own approval flows ends up with seven inboxes and
stewards who use none of them.

Governed object types in the queue: semantic annotations, metrics, glossary terms,
business-graph assignments, **lineage edges**, relationship candidates, **wiki page
versions**, tools, agents, context products, model routes, policies, source bindings,
cross-boundary grants, document claims.

Queue requirements that decide whether it is used:

- **Ordered by impact, not by arrival.** Blast radius, certification status and
  downstream dependency count drive the order.
- **Bulk decisions with per-item rationale**, grouped by pattern. The persona document
  and module 17's interface both promise this; the implementation does not have it.
  It is the difference between a reviewable queue and an abandoned one.
- **Delegation with expiry**, so absence does not stall governance.
- **Rejection always writes negative knowledge**, so the system does not re-propose
  what a human already refused.
- **Every decision is evidence**: actor, resource, before/after, rationale, timestamp,
  correlation id, written in the same transaction as the mutation.

---

## 6. Audit

Unchanged in principle from the current design, which is strong here, plus three
additions the new capabilities require:

- **Wiki page publication and block-level provenance** — for any published statement
  about the business, who or what wrote it and from which inputs.
- **Context product consumption** — which agent read which version, when, under what
  purpose. Already designed; make sure `resources/read` is covered, not just
  `tools/call`.
- **Federated execution** — one audit record per leaf query plus one for the join,
  correlated. An auditor must be able to reconstruct exactly which sources were
  touched by a single federated tool call.

---

## 7. Migration from the current model

Not a rewrite. Roughly:

1. Add `workspace`; create one workspace per existing `project`, preserving names.
2. Move `line_of_business` and `data_domain` from tenancy columns to
   `business_node` rows; generate `business_assignment` rows from the existing
   tenancy columns, with `effective_from` set to the migration date and
   `assignment_kind = MIGRATED`.
3. Keep the tenancy columns nullable and read-only for one release so nothing breaks
   while call sites move to workspace scoping.
4. Introduce the policy engine alongside RBAC; RBAC results become the default ALLOW
   policies, so behaviour is identical on day one.
5. Retire the tenancy columns once the repository base class scopes on
   `(organization_id, workspace_id)` and the invariant test proves it.
6. `legal_entity` — it does not exist in code. Do not build it. If a legal-entity
   requirement appears, it is either an isolation boundary or a classification
   attribute; both are already available.
