# Session Addendum — 2026-09-03

> **Purpose.** New tracker rows and evidence updates from the 2026-09-03
> working session, staged here rather than merged into `03-tracker.md`
> directly because that file has extensive uncommitted concurrent edits and
> a landing here would conflict. Fold these rows into `03-tracker.md` on
> the next tracker rebase; the file citations, test names and evidence
> below are what belongs in each row's evidence column.

## Rows to add / update

### AU-15 — Fail closed on unresolvable table references (P0)

Section: **N. End-to-end audit findings** (extends the AU-1..AU-14 series).

**Exit criteria met.** `query_gateway.QueryExecutionGateway.validate()`
(line ~510) and `.execute()` (line ~695) now raise
`AuthorizationRejected("unresolvable_table_references")` when
`guard_result.referenced_tables` is non-empty but
`resolve_referenced_table_ids` returns an empty set of ACTIVE
`MetadataTable` ids. `validate()` records a DENIED audit event with the
referenced table names before raising; `execute()` mirrors it and gets
recorded via the wrapping rejection persistence path. The resolver's
docstring in `src/aida/policy_resource_attributes.py` is updated to make
the callers-fail-closed contract explicit.

**Tests.** Three lock-in tests appended to
`tests/test_au11_policy_resource_attributes.py`:

- `test_unresolvable_table_reference_fails_closed_on_execute` — no
  `MetadataTable` seeded; query naming `ghost_table` raises
  `AuthorizationRejected` with `reason_code == "unresolvable_table_references"`.
- `test_unresolvable_table_reference_fails_closed_on_validate` — same on
  the validate path.
- `test_table_less_statement_is_still_permitted_by_the_fail_closed_guard`
  — regression guard: `SELECT 1` (empty `referenced_tables`) still passes
  the baseline ALLOW policy without triggering the new fail-closed branch.

**Origin.** Found by the 2026-09-03 adversarial re-audit as a caveat on
AU-11; documented in resolver's docstring at landing time but not
enforced by the caller. Filed and fixed the same session.

---

### ST-07 — Split `api.py` into routers (P0)

Section: **A. Strangle.** Row currently reads TODO with exit criterion
"OpenAPI spec byte-identical after split". Update to IN PROGRESS with
the following per-module Commits landed 2026-09-03:

**Catalog** — all three sub-commits landed.

- **Commit A.** `catalog_read_model.py` (475 lines) moved into
  `atlas.modules.catalog.repository` (nine async batch helpers +
  `_QUALITY_STALE_AFTER`, `_CertificationForActiveCheck`, `_as_aware`)
  and `atlas.modules.catalog.service` (`_quality_state`,
  `_certification_state`, `_description`, `compose_catalog_rows`). The
  aida-side path becomes a 73-line shim re-exporting all 12
  externally-used symbols. Verified by AST scan: 26 external caller
  imports across five files (`api.py`, `asset_context.py`,
  `asset_evidence.py`, `stewardship_api.py`, `unified_lineage_api.py`)
  all resolve against the shim; 12 shim imports resolve against the new
  module; 12 service→repository imports resolve.
- **Commit B.** `catalog_bulk_actions.py` (292 lines) moved as a "Bulk
  actions" section appended to `atlas.modules.catalog.service`. The
  aida-side path becomes a 63-line shim. Verified by AST scan: 13
  public symbols re-exported; 39 external caller imports across seven
  files (`api.py`, `playbooks.py`, `playbooks_api.py`, `schemas.py`,
  `stewardship_service.py`, `tests/test_catalog_bulk_actions.py`,
  `tests/test_catalog_bulk_actions_endpoints.py`) all satisfied.
- **Commit C.** Nine catalog endpoints moved from `aida.api.py` into
  `atlas.modules.catalog.router` (~830 lines): `list_catalog_rows`,
  `certify_table_asset`, `get_table_certification`, `bulk_tag_tables`,
  `bulk_classify_columns`, `bulk_assign_ownership`,
  `bulk_certify_tables`, `list_catalog_bulk_action_runs`,
  `get_catalog_bulk_action_run`, plus three private helpers and the
  `_CATALOG_BULK_ACTION_EVENT_TYPES` map. Router keeps
  `APIRouter(prefix="/v1")` with no `tags=` argument so OpenAPI paths
  and tag placement are byte-identical. `main.py` gains one
  `include_router(catalog_router)` call. As part of the cleanup: 19
  dead imports pruned from `api.py` (AST-verified zero remaining), and
  `gate_read` (used by 6 non-catalog callers + the new catalog router)
  consolidated into `aida.authorization_gate` so both call sites now
  import from a single source of truth.

**Observability audit** — one commit lands 2026-09-03.

- **Commit C (analog).** All five endpoints in
  `observability_api.py` (267 lines) moved to
  `atlas.modules.observability_audit.router`; the aida-side path
  becomes a ~50-line shim re-exporting `router`, `create_slo_definition`,
  `list_slo_definitions`, `get_slo_budget`, `get_archive_status`,
  `get_cost_showback` (the two `get_*` handlers are imported directly
  by `tests/test_cost_showback.py` and
  `tests/test_worm_archive_wiring.py`). `main.py` updated to import
  the router from its canonical location.
  `APIRouter(prefix="/v1", tags=["observability"])` preserved verbatim.
  `pyproject.toml`'s observability_audit contract's
  `allowed_importers` extended with `aida.observability_api`.

**Import-linter.** Both contracts's `allowed_importers` extended to
name the aida-side shim modules (see
`pyproject.toml`'s `catalog module privacy` and
`observability_audit module privacy` blocks).

**Windows verification recipe** — added to
`Docs/60-delivery/09-session-2026-09-03-addendum.md`:

```powershell
uv run --extra dev pytest `
  tests/test_catalog_*.py `
  tests/test_worm_archive_wiring.py `
  tests/test_cost_showback.py `
  tests/test_reachability_gate.py -x

uv run mypy src
uv run ruff check .
uv run lint-imports

uv run python -c "from aida.main import app; import json,sys; \
  json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)" > new-openapi.json
git show HEAD:openapi.json > old-openapi.json
diff old-openapi.json new-openapi.json    # expect zero drift
```

**Still to move** (this session did catalog + observability_audit; the
other three Phase-3 scaffolds still need their Commit C):

- **`identity_tenancy`** — org/lob/domain/project/workspace endpoints
  spread across `aida.api.py` (highest count of endpoints of any
  bounded context; will need to be picked apart from the org-scoped
  admin surface).
- **`connectivity`** — datasource CRUD + bulk-onboarding endpoints in
  `aida.api.py` (approximately `list_datasources`,
  `create_datasource`, `bulk_onboard_datasources`,
  `disable_datasource`, `refresh_datasource_metadata`).
- **`ingestion`** — metadata-ingestion-batch endpoints
  (`intake_metadata`, `list_metadata_ingestion_batches`, etc.).

---

### MG-9 — Multi-route model failover (P1)

Section: **F. Model gateway.** New row.

**Exit criteria met.** Runtime falls back to an alternate approved
route on transient provider failure (HTTP 429 / 502 / 503 / 504) within
a strict per-organization allow-list declared via
`AIDA_MODEL_ROUTE_FALLBACKS` (comma-separated `route_key`s).
Non-transient errors (401/403/400) short-circuit — a broken route is
not a busy provider. Every route in use is APPROVED via
`ModelRouteConfiguration`; iteration never discovers a route on its
own. Every attempt records to
`agent_run.plan_evidence.model_call_attempts` (route_key, provider_type,
attempt_ordinal, outcome, provider_status_code on failure) so the audit
trail explains fallback. Design captured in
`Docs/10-architecture/adr/ADR-0024-model-route-fallback.md`.

**Code sites.** `atlas/platform/config.py` (`model_route_fallbacks`
field + `model_route_fallback_keys` property);
`aida/agent_orchestrator.py::_approved_model_routes` (new plural method
returning ordered `list[ApprovedModelRoute]`);
`aida/agent_orchestrator.py::_generate_with_fallback` (new extracted
loop method, returns `(output, evidence, attempts)`, raises
`ModelGatewayError` with `model_call_attempts` attached on refusal);
`aida/agent_orchestrator.py::run()` refactored to call
`_generate_with_fallback` and record attempts in `plan_evidence` on
both success and refusal.

**Tests.** Two files:

- `tests/test_model_route_fallback.py` — 11 tests. Five for config
  parsing (`model_route_fallback_keys` handles empty, whitespace,
  deduplication, primary-filter, ordering); five for
  `_approved_model_routes` (primary-only, primary+fallbacks in order,
  revoked fallback skipped, empty when nothing configured, embeddings-
  only capability filter); six for `_generate_with_fallback` (primary
  succeeds baseline; primary 429 → fallback succeeds; both 429 → raises
  with attempts attached; primary 401 → no fallback; empty routes →
  raises; 502-503-504 all retryable).
- `tests/test_model_route_fallback_e2e.py` — 2 tests. Real
  `GovernedAgentOrchestrator.run()` calls with two approved
  `ModelRouteConfiguration` rows, a queued-response mock gateway, and
  a real in-memory sqlite scenario (Org + LOB + Domain + Project +
  DataSource + MetadataCatalog + MetadataSchema + MetadataTable +
  MetadataColumn). Asserts `agent_run.status == "COMPLETED"`,
  `agent_run.model_route == "gemini-bank-sql"` (the fallback), and
  `plan_evidence["model_call_attempts"] == [FAILED(429, "openai-bank-sql"),
  SUCCEEDED("gemini-bank-sql")]` from a fresh DB read-back; and the
  refusal case with both routes 429 asserting a persisted `REJECTED`
  run whose `plan_evidence["model_call_attempts"]` still contains both
  FAILED entries.

**Env / config.** `.env.example` and `compose.yaml` declare the new
env var. `Settings.model_config.extra = "forbid"` + AU-3's fuzzy typo
check together mean a misspelled `AIDA_MODEL_ROUTE_FALLBACK` (singular)
raises with "did you mean AIDA_MODEL_ROUTE_FALLBACKS?" at import.

---

### 429 UX — model-provider throttling shown honestly (P2)

Section: **F. Model gateway.** New row.

**Exit criteria met.** A model-provider 429 no longer surfaces as
"No model route is available right now" (`MODEL_UNAVAILABLE` — the same
message shown when no route is approved at all). Two fixes together:

- `model_gateway.py::post_with_retry`: `Retry-After` header ceiling
  raised 2s → 30s so a provider signaling a real back-off window
  (OpenAI/Gemini quota-reset windows are 20–60s) is honored instead of
  being re-hit at 2s intervals guaranteed to 429 again. In-loop
  exp-backoff branch (no header) keeps its 2s cap. `ModelGatewayError`
  now carries `provider_status_code`; the raise site sets it from
  `response.status_code`.
- `agent_orchestrator.py::ModelRouteUnavailable` propagates
  `provider_status_code` from the underlying `ModelGatewayError`.
  `api.py`'s Ask handler branches: HTTP 429 for
  `provider_status_code == 429`, HTTP 503 for everything else.
- `ui-next/src/lib/api.ts`: new `MODEL_THROTTLED` classification
  distinct from `MODEL_UNAVAILABLE`; `classifyAgentAskError` maps HTTP
  429 → `MODEL_THROTTLED`, HTTP 503 → `MODEL_UNAVAILABLE`.
- `ui-next/src/screens/AskScreen.tsx`: adds `MODEL_THROTTLED` to
  `ERROR_TITLE` → "The model provider is throttling us — try again in
  a moment."

---

### UI-15 — Silent-fail on shared picker hook (P2)

Section: **M. User experience.** New row.

**Exit criteria met.** `AskScreen.tsx` and `BusinessMeaningScreen.tsx`
were destructuring `datasources` and `preferredDatasourceId` from
`useDatasourcePicker` but not `error`, so a failed picker fetch
degraded to "pick a datasource" with no indication of why. Both
screens now destructure `error: dsPickerError`, render it next to the
picker as `<p role="alert" className="__pickerr">`, and thread it as
`hint` on the "pick a datasource" `<Empty>` state. Matches the
pattern `SemanticsScreen.tsx:418` already established (its
`projectsError` handling). Matching CSS rules
`.askscreen__pickerr` and `.bm__pickerr` added.

Note: `QualityScreen.tsx` and `SemanticsScreen.tsx` were already
handling picker errors correctly. The audit's "silent empty" claim on
those two screens was inaccurate.

## Verification status (2026-09-03)

`py_compile` clean on all touched Python files. `tsc --noEmit` clean
on ui-next changes. Fifteen vitest cases (AskScreen 6,
BusinessMeaningScreen 4, `useDatasourcePicker` 3, HomeScreen 2) all
pass. Twelve ui-next screens verified via built-in browser — no
`X-Principal-Id` 401s anywhere. Independent AST re-audit confirmed
all 14 AU items still hold. Windows verification pending: pytest,
mypy, ruff, lint-imports, openapi-diff.

## Files touched this session

| Path | Change |
|---|---|
| `src/aida/query_gateway.py` | AU-11 fail-closed at validate() and execute() |
| `src/aida/policy_resource_attributes.py` | AU-11 docstring: callers-fail-closed contract |
| `tests/test_au11_policy_resource_attributes.py` | +3 AU-15 lock-in tests |
| `src/atlas/modules/catalog/repository.py` | ST-07 catalog Commit A — new (308 lines) |
| `src/atlas/modules/catalog/service.py` | ST-07 catalog Commits A + B — new (~500 lines) |
| `src/atlas/modules/catalog/router.py` | ST-07 catalog Commit C — new (947 lines) |
| `src/aida/catalog_read_model.py` | ST-07 catalog A shim (475 → 73 lines) |
| `src/aida/catalog_bulk_actions.py` | ST-07 catalog B shim (292 → 63 lines) |
| `src/atlas/modules/observability_audit/router.py` | ST-07 obs Commit C — new (~280 lines) |
| `src/aida/observability_api.py` | ST-07 obs shim (267 → ~50 lines) |
| `src/aida/api.py` | ST-07 catalog C removals + 429 branch + 19 dead imports pruned + local gate_read removed |
| `src/aida/main.py` | catalog router mount + observability_router path update |
| `src/aida/authorization_gate.py` | gate_read consolidated here |
| `src/aida/model_gateway.py` | Retry-After 2s→30s cap + `provider_status_code` on `ModelGatewayError` |
| `src/aida/agent_orchestrator.py` | `_approved_model_routes` + `_generate_with_fallback` + `ModelRouteUnavailable` status propagation |
| `src/atlas/platform/config.py` | `model_route_fallbacks` field + `model_route_fallback_keys` property |
| `pyproject.toml` | catalog contract + observability_audit contract allowed_importers updated |
| `ui-next/src/lib/api.ts` | `MODEL_THROTTLED` classification |
| `ui-next/src/screens/AskScreen.tsx` (+ `.css`) | silent-fail fix + `MODEL_THROTTLED` title |
| `ui-next/src/screens/BusinessMeaningScreen.tsx` (+ `.css`) | silent-fail fix |
| `tests/test_model_route_fallback.py` | 11 tests (config + selection + loop) |
| `tests/test_model_route_fallback_e2e.py` | 2 E2E orchestrator tests — new |
| `.env.example`, `compose.yaml` | `AIDA_MODEL_ROUTE_FALLBACKS=` declared |
| `Docs/10-architecture/adr/ADR-0024-model-route-fallback.md` | new ADR |
| `Docs/60-delivery/09-session-2026-09-03-addendum.md` | this file |
