# End-to-End Audit — 2026-08-30

> Four independent audits: test suite, dead/unwired code, the primary user journey, and
> production/security readiness. Every claim below is from code that was opened, not from
> a docstring or a tracker row. Method is in §7.

## 1. Verdict: do not restart

The question that prompted this was whether to restart development. No — and not out of
sunk-cost. The spine of this platform is correct, and a rewrite would destroy work that is
demonstrably good while reproducing the actual defect, which is not architectural.

**What is genuinely strong, verified:**

- **The query execution gateway is the best part of the system.** One deterministic
  validation pipeline shared by `validate` and `execute` (`query_gateway.py:315-345`), so
  an agent cannot be told a statement is valid and then have execution disagree. Guard →
  catalog allow-list → column resolution → cost gate → row limits, with the connector
  opened only after the statement passes. Masking propagates through aliases and derived
  expressions. Refusals are recorded *before* the connector opens, so denials are
  attributable. The failure branch (`:708`) deliberately stores a constant instead of the
  driver's message.
- **The OIDC verifier is correct.** `oidc.py:160-199`: algorithm allow-list excluding
  `none` and every HMAC variant, mandatory `kid`, mandatory issuer *and* audience,
  `require` on five claims, bounded JWKS cache with forced refresh, `follow_redirects=False`.
  No `verify=False` path exists.
- **Route authorization coverage is complete.** All 320 route decorators parsed: exactly
  three lack a security dependency, and all three are `/health/live`, `/health/ready`,
  `/metrics`. Tenancy enforced at 263 `enforce_organization` sites.
- **Token revocation fails closed** and is wired into every authenticated request
  (`token_revocation.py:38-51`, `security.py:54`). `50-security/01` claims this is
  unimplemented; it is implemented.
- **Maker-checker genuinely works** end to end, including maker ≠ checker, supersede-on-
  publish, and a single shared decision core so the bulk path cannot drift from the single path.
- **The Tier-0 invariant suite is real work**, not theatre — `tests/support/app_surface.py`
  enumerates the live app rather than a hand-maintained list, and the exemption lists are
  themselves asserted closed.
- **CI's correctness gates are stronger than most**: `mypy --strict`, import-linter
  architectural contracts, ruff's flake8-bandit rule set, single-Alembic-head, and an
  OpenAPI breaking-change gate.

A restart would throw all of that away. The defect is elsewhere.

## 2. The systemic finding: implemented ≠ wired

**Roughly 4,600 lines of implementation, behind 17 tracker rows marked DONE — six of them
P0 — are unreachable from the running application.** Not "lightly used". Zero call sites
outside their own file and their own tests, verified against a full AST import graph built
from five real entry points (`main`, `worker`, `scheduler`, `graph_projector`,
`outbox_publisher`), with dynamic dispatch explicitly ruled out (no `importlib`, no entry
points, no `getattr` module lookup anywhere in `src/`).

| Module(s) | Lines | Tracker rows claiming DONE | What is actually false |
|---|---|---|---|
| `ai_decision_lineage.record_decision` | — | **AG-5, LN-3** (both P0) | No decision, rejection or refusal is ever recorded. Three endpoints — including `list_refusals` — query a permanently empty table. None of the five `DecisionType` values is ever set. |
| `observability.py` | 222 | **OB-1** (P0) | `configure_tracing` is never called; `opentelemetry` appears nowhere else in `src/`. No traces or metrics are exported. |
| `siem_routing.py` | 148 | **OB-2** (P0) | Zero call sites. No security event reaches a SOC. |
| `worm_archive.py` | 163 | **OB-3** (P0) | Zero call sites. Nothing writes `AuditArchiveRecord`, yet `GET /observability/archive/status` reads it — so it returns zeros forever while looking healthy. Legal hold cannot be applied to anything. |
| `quality_coupling.py`, `trust_scoring.py` | 416 | **DQ-3, RT-7, AG-6, TL-3** | `check_tool_gate` gates nothing; no trust warning is ever emitted; the trust factor never enters ranking. |
| `retrieval.py` + `fusion_ranking` + `vector_store` + `embedding_provider` + `graph_retrieval` + `vector_retrieval` | ~2,320 | **RT-1, RT-2, RT-3, RT-9, SM-2** | The live retrieval path is a different implementation (`agent_intelligence.GovernedRetriever`). `retrieval.py:43-52` even documents the hand-off that was never performed. Nothing embeds the catalogue. |
| `injection_defense.py`, `injection_corpus.py` | 726 | **AG-1, AG-2, TS-6** | Live screening is a different, 86-line module (`ingest_screening.py`). Note the *live* path does work — but `is_eligible_for_model_context`, whose docstring calls it "the one question every model-context builder must ask", has zero callers. |
| `abac.py` | — | **PG-1, PG-6, PG-8** | Real enforcement runs through `policy_engine.evaluate`. `abac.py` is imported only by its own router and its own test. |
| `data_contracts.py` | 272 | — | Orphaned duplicate of the live `runtime_contracts.py`. Zero importers, zero tests. |

**The one that matters most commercially.** The competitive analysis in §L positions the
**refusal record** as the differentiator Atlan structurally cannot copy. It is never written.
`ai_decision_lineage.record_decision` has zero callers. Any positioning built on it is
currently unsupported by the product.

**Why this happened, and why it will recur.** Every one of these modules was last committed
2026-08-30 — the same day the tracker was marked reviewed. The pattern is: code landed,
unit-tested in isolation, tracker updated *from the module* rather than from the call graph,
wiring step never taken. Nothing in CI detects it, because a module with passing unit tests
and no callers is indistinguishable from a healthy one.

## 3. The primary journey actually works — with two config-shaped breaks

Traced function by function.

| Stage | Verdict | Note |
|---|---|---|
| 1 Onboard a source | **Connected in dev, broken in prod** | Credentials are never stored — only an opaque provider URI — which is right. But `SecretResolver` registers exactly one provider, `env` (`secrets.py:59`), and production config *forbids* `env` (`config.py:255`). In any production-valid configuration nothing resolves and every downstream stage is dead. |
| 2 Crawl metadata | **Connected** | Real `information_schema` discovery through Temporal to `MetadataTable`/`MetadataColumn`; worker and scheduler both run as services. Caveat: nothing auto-creates a `ScanPolicy`, so a new source is never scanned until an admin sets one. |
| 3 Enrich | **Partial** | Business meaning is connected end to end and *is* read by retrieval (`agent_intelligence.py:210-272`). Relationship inference and glossary linking persist but never reach the ask path — an ACCEPTED inferred relationship never becomes join evidence. |
| 4 Author | **Connected** | Fully, including maker ≠ checker and supersede-on-publish. |
| 5 Ask | **Partial — pipeline wired, NL→SQL unreachable by default** | Prompt-risk screening, retrieval, tool selection, gateway validation, masking and audit are all genuinely called. But `model_generation_enabled` defaults `False` and `model_route` defaults `None` (`config.py:205-206`), so a plain English question 503s. The project's own smoke test never exercises NL→SQL — `scripts/verify-local.ps1:449` always passes `candidate_sql`. |
| 6 Serve an agent (MCP) | **Connected** | Budgets, role binding, audit and consumption evidence are real, and `tools/call` runs the same governed stack as Stage 5. |

**Demoable today:** onboarding (dev config), crawl, authoring with a real maker-checker
refusal, MCP tool invocation, and "ask" *when the analyst matches a published governed tool*.
The headline "ask in English, get governed SQL" is not demoable without model-route
configuration that the shipped defaults do not provide.

**A governance caveat that matters.** The ABAC gate is invoked with
`classifications=frozenset(), certification=None, quality_state=None, freshness_state=None`
(`query_gateway.py:579-588`), so every policy rule keyed on those axes is structurally
unreachable from the money path. Combined with `unresolved_workspace_posture` defaulting to
SHADOW (allow-and-log), enforcement today is substantially thinner than the documentation
implies.

## 4. Critical for a bank deployment

**C1 — Configuration fails open on a typo.** `environment` defaults to `"development"`
(`config.py:24`) and every production guard is gated on `self.environment == "production"`.
`model_config` sets `extra="ignore"` (`config.py:21`), so a misspelled *variable name*
(`AIDA_ENVIRONMNET=production`) is silently discarded and the process boots with every guard
disabled — no error, no log line, no health-check failure. A mistyped *value* fails closed;
a mistyped *name* fails open. There is no startup assertion and no deployment artifact that
pins it.

**C2 — In the default identity mode, roles come from unauthenticated headers.**
`security.py:61-74`, reached whenever `identity_provider == "development"` — the default
(`config.py:26`), what `.env.example:16` ships, and what `compose.yaml:4` runs. Sending
`X-Roles: PlatformAdmin` grants full cross-tenant admin on all 320 endpoints, and
`PlatformAdmin` short-circuits tenancy at `security.py:93-94`. Roles in this branch are not
even filtered against `PLATFORM_ROLES`, unlike the OIDC path. **C1 + C2 together mean the
artifacts as shipped deploy an unauthenticated admin API.**

**C3 — Business data leaks into the value-free control plane, violating ADR-0014 / INV-6.**
`workflows/activities.py:1039,1254` (and five more sites) do
`run.error_message = str(exc)[:4000]` on whatever the source connector raised. `db.py:31-36`
creates the engine without `hide_parameters=True`, so SQLAlchemy appends
`[SQL: ...] [parameters: (...)]` — real bound values — and driver errors routinely quote row
data (`Key (account_no)=(...)`). The log redactor (`platform/logging.py:36-98`) is a
*secret*-shaped denylist; `exception`, `error_message`, `sql`, `parameters` and `row` all
evaluate non-sensitive. The codebase already knows the right pattern — `query_gateway.py:708`
stores a constant and audits only `error_class` — it was simply not carried into the worker.

**No production deployment artifact exists.** `infra/` contains four `init.sql` seed files.
No k8s, Helm, Terraform or systemd anywhere. `compose.yaml` is local-only with hardcoded
credentials and a floating `:latest` image. Whoever writes the first manifest decides whether
C1 is set correctly — and there is currently nothing to review.

## 5. What this means for the tracker

The tracker is the most-maintained document in the repo and it is **not currently reliable
as a statement of what works.** Beyond the 17 false DONE rows in §2:

- Its own metrics are off by 2–3×. ST-02/ST-03 claim "716 passed" and "199 FastAPI routes";
  actual is **1,458 test functions** and **320 routes**. ST-03 calls the Tier-0 suite
  "9 tests"; it is ~70 functions across 7 files.
- `pytest-cov` is a declared dependency invoked nowhere — in `addopts` or CI. **No one has
  ever measured line coverage on this codebase.**
- 16 of 36 API modules — 92 of 320 endpoints, 29% — are never imported by any test. The
  substitute is ~30 tests asserting a path string appears in `app.openapi()`. Route
  registration is not behaviour.
- `require_roles` has **348 call sites and zero behavioural tests**. Nothing constructs a
  principal with the wrong role and asserts 403. A route declared `require_roles("Viewer")`
  that should be `PlatformAdmin`-only passes every gate in this repo.
- **84 migrations, zero tests apply them.** All 19 DB-backed test files build schema from the
  ORM, so ORM↔migration drift is structurally invisible. The tracker records this bug firing
  once already (DQ-1) — the instance was fixed, no gate was added.
- 97 of 116 test files never execute a SQL statement; they hand handlers fake sessions that
  return preloaded objects regardless of the query, so `WHERE organization_id = :x` is never
  evaluated. This is the multiplier behind the three findings above.

The root cause is a process one: **rows are written from the module, not from the call
graph.** Until a DONE row requires proof of reachability, this will recur.

## 6. Remediation, in order

**Stop the bleeding (this week)**

1. **Add a reachability gate to CI.** A test that fails when a module in `src/aida/` is
   importable from no entry point. This is the single highest-leverage change in this
   document: it would have caught all 17 rows, and it prevents recurrence. Cheap — the audit
   script that found them is ~80 lines.
2. **Change the definition of DONE** to require a named call site on a live path, not a
   passing unit test.
3. **Fix C1**: fail closed on unknown `AIDA_*` variables (`extra="forbid"`), and assert at
   startup that `environment` was explicitly set.
4. **Fix C3**: `hide_parameters=True` on the engine, and replace the seven
   `str(exc)` sites with the `error_class` pattern already used in `query_gateway.py:708`.

**Make the claims true (next)**

5. **Wire `record_decision`** into the orchestrator's retrieval, tool-selection and refusal
   branches. This is the commercial differentiator and it is a small change — the writer, the
   table and the read API all exist.
6. Wire the remaining dead modules **or delete them and reopen the tracker rows.** Either is
   honest; leaving them is not. Recommend deleting `data_contracts.py` and `abac.py` outright
   (live duplicates exist) and wiring `quality_coupling`/`trust_scoring` (their rows are
   load-bearing for the product story).
7. **Behavioural authz tests**: a table-driven suite asserting the expected role set per route,
   generated from the live app so it cannot drift.
8. **One migration test** that applies all 84 to an empty database and diffs against
   `Base.metadata`.

**Make it deployable (parallel track)**

9. A real deployment artifact, with `AIDA_ENVIRONMENT=production` and `identity_provider=oidc`
   pinned in it, non-root, resource limits, pinned digests.
10. Implement one non-`env` secret provider (`secrets.py:59`) — the Protocol and caching exist;
    only the fetch is missing. Without it nothing works in production.
11. Guard the Temporal connect in `main.py:77-81` so a workflow-engine outage does not take
    down read-only endpoints — the readiness probe at `:191-193` is already written to report
    it but can never execute.
12. Add dependency and secret scanning to CI, and build the container image in CI at all.

**Then** resume feature work — the UI rebuild (§M) and the Atlan-derived surfaces (§L).

## 7. Method

Four parallel audits, each reading the tracker first so as to report only what it misses.
Reachability was established with a full AST import graph over all 181 modules from five real
entry points, with dynamic dispatch ruled out by grep for `importlib`/`import_module`/
`__import__`/`entry_points`/`getattr` module lookup and by checking `pyproject.toml` for
console entry points. Route coverage came from parsing all 320 decorators. The journey trace
opened each handler and followed its calls rather than inferring from endpoint existence.

Two findings carry an explicit confidence caveat, recorded so they are not over-trusted:
`MetadataTable.superseded_by_table_id` being write-only could not be fully ruled out (a
SQLAlchemy `relationship()` with `remote_side` under a different attribute name would not
appear in a name-based grep), and the `data_contracts.py` orphan status assumes no runtime
configuration selects it.
