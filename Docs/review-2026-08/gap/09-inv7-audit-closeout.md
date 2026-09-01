# INV-7 audit closeout — thirteen unaudited mutations, closed

> Gap item **E4** follow-on. Closes the live breach recorded in
> `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` §6.1, implements the `src/`
> change requested in §12 rows 1 and 3, and answers the open architectural question
> in §6.2 with a recommendation for the architecture owner to ratify.
> Written 2026-08-30.

INV-7, in the invariants document's own words:

> Every mutation produces an audit record carrying actor identity, resource, action,
> tenant boundary, correlation ID, and timestamp, written in the same transaction as
> the mutation.

Thirteen endpoints breached it. All thirteen now call `record_audit(...)` before the
`session.commit()` that persists their mutation, so the audit row and the change it
describes share one transaction and one fate. The strict `xfail` that held the
finding is gone and `tests/test_inv7_attributability.py` passes as an ordinary test
with **no exemption list at all**.

---

## 1. The thirteen endpoints and the action each now emits

Every call carries the correlation id (`get_correlation_id()`), the principal and
principal type (from `SecurityContext`), the resource type and id, and the outcome.
Each uses `replace(context, organization_id=<the resource's organization_id>)` so the
audit row's tenant boundary is the tenant of the **resource**, not of the caller —
which matters because `enforce_organization` lets a `PlatformAdmin` through with a
different (or absent) `organization_id`, and an audit row filed under the wrong tenant
is invisible to the only people who would look for it.

### `src/aida/ai_registry_api.py` — 6 handlers

| Endpoint | Handler | Action | Resource type |
|---|---|---|---|
| `POST /v1/ai-assets/{asset_id}/versions` | `create_ai_asset_version` | `ai_registry.version.create` | `ai_asset_version` |
| `POST /v1/ai-asset-versions/{version_id}/submit` | `submit_ai_asset_version` | `ai_registry.version.submit` | `ai_asset_version` |
| `POST /v1/ai-asset-versions/{version_id}/provider-sync` | `sync_ai_provider_evidence` | `ai_registry.version.provider_sync` | `ai_asset_version` |
| `POST /v1/ai-asset-versions/{version_id}/remediations` | `create_ai_remediation` | `ai_registry.remediation.create` | `ai_remediation` |
| `PUT /v1/ai-remediations/{remediation_id}` | `update_ai_remediation` | `ai_registry.remediation.update` | `ai_remediation` |
| `POST /v1/ai-assets/{asset_id}/retire` | `request_ai_asset_retirement` | `ai_registry.asset.retirement_request` | `ai_asset` |

### `src/aida/product_marketplace_api.py` — 7 handlers

| Endpoint | Handler | Action | Resource type |
|---|---|---|---|
| `POST /v1/data-products/{product_id}/versions` | `create_data_product_version` | `data_product.version.create` | `data_product_version` |
| `PUT /v1/data-product-versions/{version_id}` | `update_data_product_version` | `data_product.version.update` | `data_product_version` |
| `POST /v1/data-product-versions/{version_id}/submit` | `submit_data_product_version` | `data_product.version.submit` | `data_product_version` |
| `POST /v1/data-product-versions/{version_id}/retire` | `request_data_product_retirement` | `data_product.version.retirement_request` | `data_product_version` |
| `POST /v1/data-products/{product_id}/contracts` | `create_data_contract` | `data_contract.create` | `data_contract_version` |
| `POST /v1/data-contract-versions/{contract_id}/submit` | `submit_data_contract` | `data_contract.submit` | `data_contract_version` |
| `POST /v1/marketplace/access-requests/{request_id}/revoke` | `revoke_marketplace_access` | `marketplace.access.revoke` | `data_product_access_request` |

### 1.1 A fourteenth call: the denial path

`PUT /v1/ai-remediations/{remediation_id}` refuses to let the same principal accept
the risk on their own finding unless they hold `PlatformAdmin`, `Reviewer` or
`ModelRiskManager`. That refusal is an independence check with a maker-checker
flavour, and it was previously invisible: the request 403'd and left nothing behind.
It now writes

| Action | Outcome | Resource type |
|---|---|---|
| `ai_registry.remediation.risk_acceptance_denied` | `DENIED` | `ai_remediation` |

and commits that row **before** raising, following the pattern already used for
`context_product.read.purpose_denied` and `context_product.read.quality_denied` in
`src/aida/context_product_api.py`. Without the explicit commit the row would be
rolled back by `get_session`'s context manager on the way out — a denial audit that
only survives when nothing was denied is not an audit.

This was not on the §6.1 list. It is added because a repeated, refused attempt to
self-accept a model-risk finding is exactly the pattern a risk review looks for, and
counting only successes would have left it unrecorded.

### 1.2 Naming convention

The existing vocabulary is `resource.verb`, deepening to `namespace.resource.verb`
where a module already namespaces itself. The new actions were chosen to sit inside
what is already there rather than beside it:

- `ai_registry.*` continues `ai_registry.asset.create`, `ai_registry.assessment.create`,
  `ai_registry.trust.read` — the three calls already in that file.
- `data_product.version.*` mirrors `context_product.version.create` /
  `.update` / `.submit` / `.deprecation_request` in `context_product_api.py`, which is
  the same lifecycle on a sibling object.
- `data_product.create` (already emitted for the product-plus-first-version path) is
  left alone; `data_product.version.create` is the subsequent-version case.
- `marketplace.access.revoke` deliberately **avoids** `marketplace.entitlement.revoke`,
  which `fulfill_marketplace_entitlement` already emits via
  `f"marketplace.entitlement.{body.action.lower()}"`. These are two different events —
  revoking the grant versus deprovisioning it at the provider — and collapsing them
  into one action name would make the trail unreadable at exactly the point someone
  asks when access actually ended. `marketplace.access.revoke` pairs with the existing
  `marketplace.access.request`.

### 1.3 Same-transaction placement

Five handlers previously called `session.add(...)` and went straight to
`session.commit()`, so the new row's server-assigned `id` did not exist yet
(primary keys use a Python-side `default=uuid4`, applied at flush). Those handlers
gained an `await session.flush()` before `record_audit(...)`, matching
`create_ai_asset`. In `create_ai_asset_version` and `create_data_contract` the flush
is placed **inside** the existing `try:` so an `IntegrityError` still becomes a 409
rather than escaping as a 500. Eleven of the thirteen already emitted an outbox
event; in each of those the `record_audit` call is placed immediately before
`record_outbox`, so audit, outbox and mutation commit together or not at all.

---

## 2. What the tests now assert

`tests/test_inv7_attributability.py`:

- `_KNOWN_UNAUDITED_MUTATIONS` is now `{}`. It is kept as an empty dict rather than
  deleted outright, because `test_every_mutation_audits` skips whatever it contains
  and `test_no_unaudited_mutation_remains` requires it to contain nothing. Together
  those are two jaws of one ratchet: a fourteenth unaudited endpoint fails
  `test_every_mutation_audits` immediately, and excusing it by adding an entry fails
  `test_no_unaudited_mutation_remains` in the same commit. Deleting the dict would
  have left only the first jaw.
- `test_no_unaudited_mutation_remains` is no longer an `xfail`. It passes.
- The test's own list of offenders was **correct** — thirteen entries, 6 + 7, every
  one naming a mounted route that genuinely did not reach `record_audit`. Nothing in
  it needed fixing. Two stale references to an earlier count ("Eleven endpoints", "a
  twelfth") survived in the module docstring from before the list grew; those are
  rewritten, since the docstring now describes a closed finding rather than an open one.

`tests/test_inv5_tenant_isolation.py`:

- `_TRANSITIVELY_SCOPED_WORKERS` is now `{}` — see §3.

---

## 3. `plan_profile_tasks` — the last transitively-scoped worker

`aida.workflows.activities.plan_profile_tasks` selected its table list with

```python
MetadataTable.datasource_id == datasource.id,
MetadataTable.status == "ACTIVE",
MetadataTable.object_type == "BASE_TABLE",
```

which is correct today — a datasource belongs to exactly one organization, and the
datasource was loaded from the run — but it is the only query in the platform that
takes its tenant boundary from a foreign key instead of stating it. It now carries

```python
MetadataTable.organization_id == run.organization_id,
```

as the first predicate, with a comment naming INV-5 and the reason. The exemption
entry in `_TRANSITIVELY_SCOPED_WORKERS` is deleted and the dict is empty.

**The exemption list shrank by one rather than growing.** Every registered Temporal
activity and every projector entry point now reaches an explicit `organization_id`
scope with no exemption, and `test_every_background_worker_is_tenant_scoped` fails on
the first one that does not. The empty dict is retained for the same two-jaw reason as
above: the test also asserts that nothing in the list is stale, so re-adding an entry
is a visible act.

---

## 4. Ratified — does INV-7's "mutation" cover lazily-created default rows?

> **Ratified 2026-09-01 (tracker `ST-18`).** The recommendation below was adopted
> without change. `Docs/10-architecture/01-principles-and-invariants.md`'s INV-7
> section now carries the ratified scope note verbatim (independently re-verified
> against `ensure_default_domain` and `ensure_organization_integration_policy` as
> they exist in the tree today, not just against this write-up). This section is
> kept as the full trade-off record the invariants document points back to; it is
> no longer an open question.

This answers the open question in `06-tier0-invariant-suite.md` §6.2.

### The subject

Two helpers lazily create a per-organization default row on first read:

- `ensure_default_domain` (`src/aida/domain_service.py`) — creates the line of
  business's one `is_default=True` "Ungoverned" `DataDomain`.
- `ensure_organization_integration_policy` (`src/aida/integration_service.py`) —
  creates the organization's `OrganizationIntegrationPolicy`.

Eight `GET` routes reach one of them and are therefore flagged, correctly, by the
derived-mutation scan. They are listed in `_LAZY_DEFAULT_WRITE_ROUTES`.

### Recommendation

**"Mutation" in INV-7 should mean "records an actor's decision", not "stages a row".
These eight routes should stay excused, and the invariants document should say so in
one sentence rather than leaving the reading to the test file.**

### Reasoning

The load-bearing fact is not that these writes are small or frequent. It is that
**no caller input reaches the row.** Read both helpers:

```python
domain = DataDomain(
    organization_id=lob.organization_id,
    line_of_business_id=lob.id,
    name="Ungoverned",
    code="UNGOVERNED",
    is_default=True,
)
```

```python
policy = OrganizationIntegrationPolicy(organization_id=organization_id)
```

Every field is either a constant or the tenant/parent identifier. The row is a pure
function of the tenant. Two different principals hitting the endpoint in either order
produce a byte-identical row; the one who happened to arrive first is an accident of
scheduling, not an author. INV-7 exists to make an actor's choice replayable and
attributable — "a decision that cannot be replayed cannot be audited". There is no
decision here to replay. Recording one principal as the creator would not preserve
information; it would **manufacture** it, and imply an authorship that a reviewer
could then wrongly rely on.

### The trade-off, stated honestly

**Auditing lazy creation costs audit volume on read paths.** Not one row per
organization — the write is idempotent — but the audit call would sit on eight `GET`
handlers that are among the most-called in the platform, and getting it to fire only
on the creating request means either threading a "did I create it" flag back out of
both helpers or auditing every read. The first is a real change to two service
functions and their eight call sites; the second dilutes the trail with high-volume
noise that records nothing anyone chose. Neither is free, and the second actively
degrades the artefact INV-7 exists to produce.

**Not auditing it costs this: a governed row exists with no attributable creator.**
That is a real cost and should not be waved away. If someone asks "who created this
organization's integration policy", the audit trail has no answer. The mitigation is
that the question has no *interesting* answer — the row's content is fixed by the
schema, so "the platform, on first read" is the complete and true answer — and the
row's `created_at` still bounds when it happened. What would change the calculus is
either helper gaining a caller-supplied value: a chosen domain name, a policy
override. Then the row starts carrying somebody's choice and the exemption's premise
is gone.

### What was implemented, so the recommendation cannot rot

The carve-out is kept, but made **falsifiable** rather than declarative. Two tests
now hold its premise:

- `test_the_lazy_default_write_list_stays_closed` gained a third assertion: each
  excused route must still *reach the helper its entry names*. Previously it only
  checked that the route still wrote something — so a handler could have grown a
  real, actor-driven mutation and kept its exclusion. It cannot now.
- `test_lazy_default_writers_record_no_actor_decision` (new) pins both helper
  signatures exactly: `(session, lob)` and `(session, organization_id)`. The moment
  either gains a parameter, the "no caller input" premise is false and the test says
  so, naming the drift, instead of the exclusion quietly outliving its justification.

If Architecture rules the other way — that "mutation" means "stages a row" — the
change is bounded and known: return a created/found flag from both helpers and audit
on the creating branch only, at eight call sites. Nothing in this closeout blocks it.

---

## 5. Proving the ratchet bites

Per the invariant suite's own standard that every property be shown capable of
failing, the new coverage was mutation-tested.

**Mutation used.** The `record_audit(...)` call was deleted from
`revoke_marketplace_access` in `src/aida/product_marketplace_api.py` — chosen because
revoking a marketplace entitlement is the single most audit-relevant action in the
marketplace, so it is the one a silently-passing test would be worst at missing. The
edit was made in place, the suite run, and the file restored byte-for-byte from a
backup held outside the repository.

**Result — `test_every_mutation_audits` went red, naming the exact route:**

```
E       AssertionError: these mutating endpoints never reach record_audit; INV-7 requires an audit record for every mutation: ['POST /v1/marketplace/access-requests/{request_id}/revoke -> aida.product_marketplace_api.revoke_marketplace_access']
FAILED tests/test_inv7_attributability.py::test_every_mutation_audits
```

With the file restored, the module passes again. The ratchet bites, and it bites with
a message that points at the handler rather than at a count.

---

## 6. Verification

```
$ ruff check src/aida/ai_registry_api.py src/aida/product_marketplace_api.py \
    src/aida/workflows/activities.py tests/test_inv7_attributability.py \
    tests/test_inv5_tenant_isolation.py
All checks passed!

$ pytest -p no:cacheprovider --no-header tests/test_inv7_attributability.py \
    tests/test_inv5_tenant_isolation.py tests/test_ai_registry.py \
    tests/test_agentic_platform.py tests/test_fleet_scheduling.py
94 passed, 30 warnings in 5.50s
```

At the point this change was complete and before a concurrent workstream's in-flight
edit to `src/aida/ingestion.py` landed, the whole tree was green:

```
$ ruff check .
All checks passed!

$ mypy --cache-dir=$HOME/mypycache src
Success: no issues found in 114 source files

$ pytest -p no:cacheprovider --no-header
577 passed, 1 xfailed, 30 warnings in 16.23s
```

Baseline before this change was **575 passed, 2 xfailed**. The arithmetic:
INV-7's strict xfail became a pass (+1 passed, −1 xfailed), and
`test_lazy_default_writers_record_no_actor_decision` is new (+1 passed).
The one remaining xfail is INV-9's, which is blocked on **E12** (a connector
certification corpus that does not exist) and was deliberately not touched.

**Concurrent-work note (2026-08-30, historical).** A later run of `ruff check .` and
`mypy src` showed 19 ruff errors and 16 mypy errors in `src/aida/ingestion.py`, plus one
unused import and four failures in a WIP test file from the concurrent connector/ingestion
workstream (`'MetadataSchemaEnvelope' object has no attribute 'routines'`). Those files
belonged to that workstream and were mid-edit; none of them was touched by this change and
none was fixed here. The WIP file itself no longer exists in the repo under the name this
note originally gave it — left as a historical record of that moment rather than a claim
about a file present today. Everything inside this workstream's ownership was clean, as the
scoped commands above show.

---

## 7. Proposed tracker rows

For `Docs/60-delivery/03-tracker.md` — proposed, not applied; that file was not edited.
Columns as defined there: `ID | Item | Mod | Ph | Pri | Status | Owner | Exit`.

### 7.1 New rows

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| ST-17 | INV-7 audit closeout — 13 unaudited mutations | 17,20 | 0 | P0 | DONE | — | Landed 2026-08-30. All 13 endpoints in `ai_registry_api.py` (6) and `product_marketplace_api.py` (7) call `record_audit(...)` before the `session.commit()` that persists their mutation, carrying principal, principal type, resource type, resource id, tenant boundary (`replace(context, organization_id=<resource org>)`), correlation id and outcome. A 14th call audits the `PUT /v1/ai-remediations/{id}` independence denial as `DENIED`, committed before the 403. Action vocabulary extends the existing `namespace.resource.verb` scheme: `ai_registry.version.{create,submit,provider_sync}`, `ai_registry.remediation.{create,update,risk_acceptance_denied}`, `ai_registry.asset.retirement_request`, `data_product.version.{create,update,submit,retirement_request}`, `data_contract.{create,submit}`, `marketplace.access.revoke`. `_KNOWN_UNAUDITED_MUTATIONS` is empty and `test_no_unaudited_mutation_remains` is no longer a strict xfail; `test_every_mutation_audits` now covers every mutating route with zero exemptions. Ratchet mutation-tested by deleting the `record_audit` in `revoke_marketplace_access` — the test fails naming that route. Detail: `Docs/review-2026-08/gap/09-inv7-audit-closeout.md` |
| ST-18 | Ratify INV-7's meaning of "mutation" for lazily-created default rows | 17 | 0 | P1 | TODO | — | Architecture decides whether "mutation" in INV-7 means "stages a row" or "records an actor's decision", and `Docs/10-architecture/01-principles-and-invariants.md` states which in INV-7's Statement or Enforcement. Recommendation, reasoning and both sides of the trade-off: `Docs/review-2026-08/gap/09-inv7-audit-closeout.md` §4 — recommend "records an actor's decision", on the ground that `ensure_default_domain` and `ensure_organization_integration_policy` build their row from constants plus the tenant identifier, so no caller input reaches it and naming a creator would manufacture attribution rather than preserve it. Exit: the document says which reading is binding; if "stages a row" wins, both helpers return a created/found flag and the 8 GET call sites audit on the creating branch only, and `_LAZY_DEFAULT_WRITE_ROUTES` is deleted |

### 7.2 Amendments to existing rows

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| ST-03 | Tier 0 invariant suite (9 tests) | all | 0 | P0 | DONE | — | *(existing Exit text, with the final xfail sentence replaced by:)* One strict xfail remains, and it records a codebase gap, not a suite gap: capability flags are hand-declared rather than derived from certification (INV-9, needs E12). The INV-7 xfail — 13 endpoints in `ai_registry_api`/`product_marketplace_api` committing governed state with no audit row — was closed 2026-08-30 under ST-17 and is now a passing test with no exemption list. INV-5's `_TRANSITIVELY_SCOPED_WORKERS` exemption is likewise empty: `plan_profile_tasks` carries an explicit `organization_id` predicate. Suite now 577 passed, 1 xfailed. Detail: `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` and `09-inv7-audit-closeout.md` |

### 7.3 Note for `06-tier0-invariant-suite.md`

That document's §6.1, §6.2 and §12 rows 1 and 3 describe requested-but-unmade changes
that are now made. It is another workstream's file and was not edited; whoever owns it
should mark §6.1 closed, point §6.2 at §4 here, and strike rows 1 and 3 of §12.
