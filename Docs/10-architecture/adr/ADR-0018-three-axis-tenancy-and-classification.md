# ADR-0018 — Three-Axis Tenancy: Access, Classification, Technical

**Status:** Accepted | **Date:** 2026-08-30 | **Owner:** Architecture
**Supersedes:** [ADR-0017](ADR-0017-domain-complete-tenancy-and-cross-source-graph.md) (Proposed, never accepted)
**Amends:** [ADR-0005](ADR-0005-tenancy-hierarchy.md)

## Context

Three different hierarchies exist in any metadata platform, and ADR-0005 fused two of them
into one path:

```
organization → legal_entity → line_of_business → data_domain → project → datasource
```

That single chain is doing three unrelated jobs at once — deciding who may see what,
recording what data *means* and who owns that meaning, and describing where bytes physically
live. ADR-0017 proposed deepening the fusion: making `data_domain` a real tenancy level with
self-referencing sub-domains, stamping a tenancy path onto every graph node and edge, and
making domain the default traversal scope.

Four things forced a re-examination.

**1. The hierarchy is partly fictional.** `LegalEntity` does not exist anywhere in `src/`.
It appears in ADR-0005, in module 01's domain model, and in the data-architecture diagram,
and nowhere in the schema. Module 01 and ADR-0017 disagree in writing about whether
`data_domain` exists (it does — model and migration both). A hierarchy that two authoritative
documents describe differently is not yet a decision.

**2. ADR-0017's own reversal condition is already met.** It records that it should be
reversed if *"domain taxonomy turns out not to nest cleanly (a table genuinely needs two
sibling domains)."* In a bank this is not a hypothetical: a `customer` table belongs to
Retail Banking *and* Financial Crime; a `position` table belongs to Markets *and* Risk. A
containment hierarchy cannot express it. The condition is structural, not eventual.

**3. Reorganisation cost.** A bank restructures its lines of business every 18–36 months. If
LOB is a tenancy level, a reorg is a data migration across every governed table, every graph
node and edge, and every audit record — and afterwards the audit history describes an
organisation chart that no longer exists, which is the opposite of what an audit record is for.

**4. The market has already learned this.** Collibra fuses taxonomy and permission in its
Community/Domain hierarchy, and banks routinely model "Community = LOB" for permissions while
also using a separate "Line of Business" asset for taxonomy, then cannot reconcile the two;
implementation partners name this as a root cause of stalled rollouts. Atlan, by contrast,
separates Personas and Purposes (access) from Domains and Products (classification) from the
Connection hierarchy (technical), and Databricks enforces policy on classification tags rather
than on containers. The separation is what the more successful designs have in common.

## Decision

**Model three axes independently. Only one of them grants access.**

### Axis 1 — Access (short, stable, the only one with permission semantics)

```
organization
└── workspace                  the unit of grant, membership, budget, blast radius
    ├── membership(principal, role)
    ├── source_binding[]       scoped, masked, cost-capped, expiring
    └── project[]              analysis scopes inside the workspace
```

Two levels. The tenancy scope carried on every governed record and enforced by the repository
base class becomes `(organization_id, workspace_id)`.

Genuine hard walls — an advisory desk that must not see a trading desk — are expressed as an
explicit `isolation_boundary` with mode `STRICT` or `ADVISORY`. `STRICT` admits no
cross-boundary grant by any mechanism, including administrator action. The set is small and
explicit; a bank has a handful, not one per LOB.

### Axis 2 — Classification (versioned, many-to-many, grants nothing)

```
business_node        LOB | SUB_LOB | DOMAIN | SUB_DOMAIN | CONCEPT, self-referencing
business_assignment  (business_node, target_ref, kind, confidence, effective_from/to)
```

`target_ref` reaches any governed object: table, column, view, metric, glossary term, data
product, knowledge page. Assignments carry effective dates, so a reorg updates the tree and
adds assignments while history continues to resolve against the tree as it stood.
Assignments may be `MANUAL`, `RULE`-driven (`schema LIKE 'rtl_%' → Retail Banking`), or
`INFERRED`; rule-driven assignments are governed objects that produce proposals on drift
rather than silent reassignment.

### Axis 3 — Technical (discovered, never designed)

`datasource → catalog → schema → table | view | procedure → column`. Unchanged.

### Access decisions read axis 2 without being contained by it

Policy is attribute-based and keys on classification, domain membership, certification status,
purpose, and principal kind. "Everyone in Retail Banking may read certified assets in the
Retail Banking domain" becomes a policy, not a tree walk. `principal_kind = AGENT` becomes
expressible, which it is not today.

### `legal_entity` is not built

It exists in no schema. Where a legal-entity requirement appears it is either an
`isolation_boundary` or a classification attribute; both already exist above. ADR-0005's
mention of it is withdrawn rather than deferred.

## Implementation status (2026-08-30)

Steps 1-4 of the migration below are **built**; step 5 is deliberately a later release.

| Step | State |
|---|---|
| 1. Add `workspace` | Done — `workspace`, `workspace_membership`, `source_binding`, `isolation_boundary` models and migration `f1a2b3c4d5e6` |
| 2. Move LOB/domain into `business_node` + assignments | Done — migration backfills a node per `line_of_business` and per `data_domain`, preserves the parent chain, and writes `MIGRATED` assignments for every project and datasource |
| 3. Tenancy columns nullable and read-only for one release | **In effect now.** `line_of_business`, `data_domain` and the tenancy columns on `project`/`datasource` are untouched and remain authoritative; the new axes are additive and read alongside them |
| 4. Policy engine alongside RBAC, seeded to today's outcomes | Done — `policy_engine.py`, `access_policy`, and seeded RBAC-parity policies. The one policy that would change behaviour (agents denied sensitive classifications) is seeded `DRAFT`, so migration day changes nothing |
| 5. Retire tenancy columns once the repository base class scopes on `(organization_id, workspace_id)` | **Not started.** No repository base class exists yet — `src/atlas/platform/` has config, context, db and logging only. This step depends on the module decomposition (tracker ST-05/06/07) |

Enforcement entry point is `workspace_service.authorize`, which fails closed at every
step: workspace unavailable, cross-organization, no membership, role does not permit the
action, no active binding, binding expired, outside the binding's schema scope,
classification outside the binding, then the policy decision itself.

INV-5 (tenant isolation) is formalised in the Tier-0 suite against that entry point with a
real in-memory database — the first of the five previously-unformalised invariants to
close. It asserts that the authorization path denies across an organization boundary and
denies without membership.

### Rollout status (2026-08-30) — wired, measuring, not enforcing

`authorize` is reached from production traffic as of this date. Surfaces do not call it
directly; they call `authorization_gate.gate`, which resolves the workspace and then
defers to `workspace_service.authorize_enforced` so that a workspace's enforcement mode
is always honoured. Wired: `QueryExecutionGateway.execute` (`READ_DATA`) and `.validate`
(`READ_METADATA`) — the INV-2 choke point, so all four gateway callers are covered by one
change — plus `list_tables`, `list_columns`, `list_constraints`,
`get_latest_table_profile` (`READ_METADATA`) and `preview_agent_retrieval`
(`CONSUME_CONTEXT`).

**Workspace resolution is subject-independent** (`workspace_resolution.py`). A request
either names its workspace or has exactly one live binding to the datasource it touches;
two live bindings is `WORKSPACE_AMBIGUOUS`, a refusal to answer. Resolving by "which
workspace does this principal have access to" would pick the workspace by the answer and
then ask the question, which is not a check.

**Three postures, and today only the first is active.** A workspace in `SHADOW` records
what it would have denied and allows. A workspace in `ENFORCE` denies. A request whose
workspace cannot be resolved is a third state governed by
`Settings.unresolved_workspace_posture`, default `SHADOW`; the gate returns `decided=False`
for it, so no caller can report a check that did not happen. **Flipping that setting to
`DENY`, per environment, is the completion of this rollout** — measured on 2026-08-30 as
17 failing tests, each one a surface that does not yet pass a workspace id.

The endpoint-coverage question this section previously left open is answered by
`tests/test_inv4_authorization_wiring.py` rather than by an import-linter contract:
import-linter constrains modules, and what needs constraining here is which *functions*
reach a decision. The static scan asserts the gate is reachable from each wired surface,
that no module outside `workspace_service`/`workspace_api` calls `authorize` directly
(the probe endpoint wants the unmodulated answer; everything else must honour shadow
mode), and — as its own meta-test — that the scan can still tell a gated handler from an
ungated one.

## Consequences

### Positive

- An asset can belong to two domains, and a domain can span workspaces and sources. Neither is expressible today.
- A reorg is a classification change, not a data migration, and audit history stays truthful.
- One migration replaces the indefinite sequence of point fixes ADR-0017 would have started.
- ADR-0017's two actual goals — cross-source traversal and cross-source relationship inference — are met by workspace-scoped bindings plus classification-keyed policy, without a tenancy path on every graph node and edge.
- Policy applies to objects discovered next Tuesday, because it keys on what they *are*.

### Negative — costs accepted

- **A migration touching the repository base class and every scoped query.** This is the single most invasive change in the current plan. It is cheapest now, when 1 of 21 modules exists, and gets more expensive with every module added — which is the main reason for deciding it before further decomposition.
- Two lookups where there was one: authorisation resolves workspace scope, then policy evaluates classification. The authz latency budget (p95 < 50 ms) must absorb a classification lookup, which argues for caching the assignment closure per principal.
- A team that thinks in org charts must learn that the org chart is a label. This is a documentation and UI problem, and it is where Collibra deployments visibly fail.
- Versioned assignments with effective dates make historical queries correct and current queries slightly more complex. Every read needs an `as_of` default.

### Migration

1. Add `workspace`; create one per existing `project`, preserving names.
2. Move `line_of_business` and `data_domain` rows into `business_node`; generate `business_assignment` rows from existing tenancy columns with `assignment_kind = MIGRATED` and `effective_from` = migration date.
3. Keep tenancy columns nullable and read-only for one release while call sites move to workspace scoping.
4. Introduce the policy engine alongside RBAC, seeded so existing RBAC outcomes are the default ALLOW policies — behaviour identical on day one.
5. Retire the tenancy columns once the repository base class scopes on `(organization_id, workspace_id)` and a Tier-0 test proves it.

## Reversal condition

Reverse if a regulator or the bank's own control framework requires that the *permission
boundary itself* be the line of business — that is, if "may a Retail analyst see a Markets
table" must be answerable without evaluating policy, from containment alone. In that case
`isolation_boundary` is promoted from an exception to the norm and this ADR collapses back
toward ADR-0005. Note that this is a requirement about proof structure, not about strictness:
the ABAC model can already deny everything containment would deny.
