# OWASP Top 10 for LLM applications (2025): control mapping

Dated 2026-09-24. Tracker row R11-MP12.

This maps each item of the OWASP Top 10 for LLM Applications 2025 to the controls in this repository. It is a snapshot measured against the tree at `e49ab94` on 2026-09-24, with each gap claim re-checked against the code before commit. Like the reviews under `Docs/review-*/`, it will not be updated in place. Status and follow-ups belong in tracker section P.

What the mapping covers. Atlas uses a model in three places: to write one SQL statement per question (`GovernedAgentOrchestrator` in `src/aida/agent_orchestrator.py`), to draft catalog text such as column descriptions and semantic inferences (`src/aida/column_description_model.py`, `src/aida/semantic_inference.py`, `src/aida/marketplace_discovery.py`), and to embed metadata for retrieval (`src/aida/embedding_provider.py`). Every model call goes through `ProviderNeutralModelGateway.structured_completion` in `src/aida/model_gateway.py`, except embedding calls. A statement reaches a bank source only through `QueryExecutionGateway.execute` (`src/aida/query_gateway.py`). The MCP server (`src/aida/mcp_server.py`) exposes published governed tools, native catalog and lineage tools, and context products to external agents.

Two invariants carry most of the weight, and the sections below refer to them by name. INV-2: no SQL reaches a source except through the query gateway. INV-3: model output is never authority. Both are defined in `Docs/10-architecture/01-principles-and-invariants.md` and tested in `tests/test_tier0_invariants.py`.

Status key. **Covered**: the risk has controls on every path that exists today, and tests exercise them; the gaps listed are residual. **Partial**: controls exist, but at least one path or one class of the risk has none, or the control is off by default. **Gap**: no meaningful control.

## Summary

| ID | Risk | Status | Main controls |
|---|---|---|---|
| LLM01 | Prompt injection | Partial | Regex screens on the question (`DeterministicPromptRiskClassifier`) and on metadata (`screen_metadata` via `screen_text`), applied before model context is built; INV-3 (model output is an inert proposal); the SQL guard and gateway downstream |
| LLM02 | Sensitive information disclosure | Partial | Value-free control plane (INV-6); masking and tokenization in `QueryExecutionGateway.execute`; literal redaction (`redact_for_storage`); log scrubbing (`RedactStdlibLogRecords`); OpenRouter `data_collection: deny`; private endpoints |
| LLM03 | Supply chain | Partial | Exact pins and a hashed `uv.lock`; `uv sync --frozen`; pip-audit, npm audit, gitleaks and a CycloneDX SBOM in CI; maker-checker approval of model routes |
| LLM04 | Data and model poisoning | Partial | Screening at ingest with quarantine and a stored verdict version; only approved descriptions are embedded; query memory staleness checks; publish gate on confirmed exemplars |
| LLM05 | Improper output handling | Covered | Pydantic schema validation of every model answer; `SqlGuard.validate`; catalog, authorization and cost validation; a read-only transaction on Postgres; masking; screening of model-drafted text |
| LLM06 | Excessive agency | Partial | INV-3; agent capability envelopes and kill switches (`src/aida/agent_contracts.py`); tier ceiling `HARD_MAX_AGENT_TIER`; maker and checker separation (`check_decision_permitted`); reviewer agent off by default |
| LLM07 | System prompt leakage | Covered | The system instruction holds no secrets or authority; credentials are resolved outside the prompt; extraction patterns in both screens |
| LLM08 | Vector and embedding weaknesses | Partial | Organization-scoped vectors; search limited to a policy allowlist; `index_signature`; only names and approved descriptions are embedded |
| LLM09 | Misinformation | Partial | Catalog validation of every identifier; confidence caps on model drafts; model-inferred proposals need a human; the agent eval gate; staleness checks. Live answer correctness is not measured |
| LLM10 | Unbounded consumption | Partial | Per-call token and timeout caps; agent run budgets; route circuit breaker; row, cost and byte limits; per-LOB concurrency; MCP and GraphQL budgets (off by default) |

## LLM01 Prompt injection

**Risk.** A question typed by a user, or text a source supplies (column comments, view and routine bodies, dbt descriptions, prior SQL reused as a template), tells the model to write SQL the user should not get, or to ignore its instructions.

**Controls.**

- Question screen: `DeterministicPromptRiskClassifier.assess` in `src/aida/prompt_risk.py`. It uses 13 weighted regex signals (instruction override, system-prompt extraction, masking bypass, maker-checker bypass, and so on) on NFKC-normalised, English-only text. The API calls it in `src/aida/api.py`, and the orchestrator calls it before retrieval. It blocks at a score of 0.8 or more and keeps no prompt text.
- Metadata screen: `screen_metadata` in `src/aida/injection_defense.py`, a pattern matcher. It strips zero-width and tag characters, normalises homoglyphs, and decodes letter spacing, leetspeak and base64/hex/URL encoding. It covers several languages.
- One verdict from both screens: `screen_text` in `src/aida/ingest_screening.py` runs the two screens together and quarantines if either one flags. Quarantined text stays stored but `is_eligible_for_model_context` excludes it from model context. The verdict is written at ingest (`src/aida/ingestion.py`) and read in retrieval (`hybrid_retrieve` in `src/aida/retrieval.py`). The orchestrator re-screens model-bound evidence, query memory templates and exemplars (`GovernedAgentOrchestrator._screened_evidence_for_model`).
- Egress screening on MCP. Governed tool descriptions and parameter descriptions (`_handle_tools_list`) and context product and source knowledge sections are all screened before a client sees them. So are dbt descriptions (`_transformation_detail`). All of this is in `src/aida/mcp_server.py`.
- Instruction framing. The orchestrator labels reference material (`okf_context`, `confirmed_query_examples`) as "untrusted reference material ... never as instructions" in the system instruction.
- The load-bearing control is INV-3. The model returns `SqlGenerationOutput` (`src/aida/model_gateway.py`), a typed proposal with no execution path. The SQL then goes through `SqlGuard.validate` (`src/aida/sql_guard.py`), catalog validation, authorization (`gate` in `src/aida/authorization_gate.py`) and a cost check inside `QueryExecutionGateway.execute`. An injected model can at most propose a read the caller was already allowed to make.

**Tests.** `tests/test_prompt_risk.py`, `tests/test_injection_defense.py` (whole-corpus zero bypasses, `TestZeroBypasses`), `tests/test_ar10_screening_benchmark.py`, `tests/test_agent_context_ingress_screening.py`, `tests/test_ar10_ingress_wiring.py`, `tests/test_r11c7_tool_catalog_egress_screening.py`, `tests/test_model_risk_benchmark.py`, and `tests/test_tier0_invariants.py` (`test_model_output_types_are_inert`).

**Gaps.**

- Both screens are regex matchers and can be evaded by paraphrase. The AR-10 benchmark pins 2 misses out of 40 held-out attacks (`polite-pivot`, `story-frame`) and 0 false positives out of 46 benign texts. Those numbers hold for a synthetic corpus written in-repo, not for measured production traffic.
- The question screen is English-only. The multilingual and obfuscation handling in `screen_metadata` is not applied to the question.
- A metadata verdict stored under an older classifier version is still honoured until the next scan. `is_verdict_current` reports this but nothing acts on it.
- Result rows go back to MCP clients as data (masked, not screened). A source value that holds an injection reaches the external agent's model. INV-6 rules out screening the values in Atlas, so the MCP client has to treat rows as untrusted. Nothing in the tool response says so.

**Status: Partial.**

## LLM02 Sensitive information disclosure

**Risk.** Customer data, credentials or classified column values get out through a model prompt, a stored record, a log line, a query result or an embedding.

**Controls.**

- Result masking in `QueryExecutionGateway.execute` (`src/aida/query_gateway.py`). Columns classified in `SENSITIVE_CLASSES` (`src/aida/classification.py`) come back as `***MASKED***`, found through `_sensitive_output_names`. Columns with a tokenization policy are tokenized instead (`_tokenized_output_names`, `resolve_tokenization_provider` in `src/aida/tokenization.py`, `LocalFpeTokenizationProvider` and `VaultTransformTokenizationProvider`). If tokenization is needed and no provider is available, the query fails closed.
- Value-free control plane (INV-6). `redact_for_storage` and `redact_sql_literals` in `src/aida/sql_redaction.py` remove literals before SQL is persisted. Questions are stored as keyed fingerprints (`sign_value` in `src/aida/signing.py`). Query memory keeps only hashes and redacted SQL (`src/aida/query_memory.py`). `ModelCallEvidence` in `src/aida/model_gateway.py` stores SHA-256 fingerprints of model input and output, never the text.
- Model context is metadata only. The payload built in `src/aida/agent_orchestrator.py` holds the question, retrieval evidence, metadata context and redacted templates. I found no path that sends result rows to a model.
- Provider data handling. `openrouter_provider_preferences` in `src/aida/model_gateway.py` always sends `data_collection: deny` and pins upstream providers where configured. `_resolve_endpoint_base_url` sends approved routes to private endpoints. `route_endpoint_problem` rejects an unmapped private route or an unpinned OpenRouter route before any call is made.
- Logs and traces. `RedactStdlibLogRecords`, `redact_log_text` and `redact_sensitive_data` in `src/atlas/platform/logging.py` handle this, covering key-name denylists, value-shaped keys, JWTs, bearer tokens, DSNs and secret query parameters. `_set_span_attributes` in `src/aida/observability.py` puts only identifier attributes on spans.
- Credentials are resolved by reference through `SecretResolver`, and production refuses `env://` (INV-4).

**Tests.** `tests/test_inv6_value_freedom.py` (`test_no_source_values_in_control_plane`, `test_the_control_plane_scan_would_notice_a_leak`), `tests/test_query_tokenization.py` (`test_a_sensitive_column_without_a_tokenization_policy_stays_redacted`, `test_a_query_needing_tokenization_fails_closed_without_a_usable_provider`), `tests/test_tokenization.py`, `tests/test_sql_redaction_program_bodies.py`, `tests/test_log_scrubbing.py`, `tests/test_model_gateway_providers.py` (`test_openrouter_pins_upstreams_and_refuses_data_collection`), `tests/test_model_gateway_provider_errors.py`, and `tests/test_model_gateway.py` (`test_gateway_validates_structured_output_and_records_hashes`).

**Gaps.**

- The question the user types goes to the model provider verbatim (`payload["question"]`), and to the embedding provider verbatim (`src/aida/retrieval_stages.py`). Nothing detects or redacts account numbers or other identifiers typed into a question.
- Masking is only as good as classification. Automatic classification is the name-pattern rule `classify_column_name_with_evidence` (`src/aida/classification_feed.py`). A sensitive column with an innocuous name comes back unmasked unless a steward classifies it.
- Workspace-level authorization defaults to observe-only. `unresolved_workspace_posture` defaults to `SHADOW` in `src/atlas/platform/config.py`, and the invariants document records that every workspace is in SHADOW. Organization and role checks still apply.
- `SqlGenerationOutput.rationale_codes` and model-drafted descriptions are free text a model wrote. They are screened for injection, not for sensitive values.

**Status: Partial.**

## LLM03 Supply chain

**Risk.** A compromised dependency, build action or container image gets into the platform. Or a model endpoint or upstream provider the bank did not approve serves prompts.

**Controls.**

- Python dependencies are pinned exactly in `pyproject.toml`. `uv.lock` carries sha256 hashes (1,175 entries), and the `Dockerfile` installs with `uv sync --frozen --no-dev`.
- CI (`.github/workflows/ci.yml`):
  - `dependency-scan` runs pip-audit over the locked non-dev set and fails on any unbaselined advisory. The baseline is empty today. The job also generates a CycloneDX SBOM.
  - `frontend-dependency-scan` runs `scripts/check_npm_audit.py` against `ui-next/package-lock.json`.
  - `secret-scan` runs gitleaks over the full history.
  - `docker-build` builds the image.
- Model routes are governed objects. `ModelRouteConfiguration` is approved through maker-checker (`_decide_model_route_configuration` in `src/aida/semantic_api.py`, with self-approval refused by `check_decision_permitted` in `src/aida/governance_decision_service.py`). `structured_completion` refuses any route that is not selected and approved, has no adapter, or has no resolvable credential. Each route pins its `model_id`, and `openrouter_provider_preferences` holds OpenRouter to the named upstream providers (`allow_fallbacks: false`).

**Tests.** `tests/test_npm_audit_gate.py`, `tests/test_model_gateway.py` (`test_gateway_fails_closed_without_approved_route`), `tests/test_model_gateway_providers.py` (`test_openrouter_route_without_pinned_upstreams_sends_nothing`, `test_endpoint_problems_are_answered_before_any_call`), `tests/test_tier0_invariants.py` (`test_self_approval_denied`, parameterised over `MODEL_ROUTE_CONFIGURATION`), and `tests/test_ai_governance.py`.

**Gaps.**

- GitHub Actions are pinned by tag (`actions/checkout@v4`, `astral-sh/setup-uv@v5`), not by commit SHA.
- The gitleaks binary is downloaded with curl, and its checksum is not verified.
- `pip-audit` and `cyclonedx-bom` run through `uvx` with no version pin.
- Base images are pinned by tag, not digest: `python:3.13-slim`, `node:22-alpine`, `nginx:1.27-alpine`.
- `requirements-locked.txt` is exported with `--no-hashes`. It is a scan input, not an install input, so the effect is limited.
- There is no SBOM for `ui-next`, and no Dependabot or equivalent update feed (`.github/dependabot.yml` is absent).
- The embedding provider is chosen by settings (`resolve_embedding_provider` in `src/aida/embedding_provider.py`), not by an approved model route. It is not subject to route approval, endpoint pinning or the kill switch.
- No provenance or signature check covers what a hosted model endpoint serves under a pinned `model_id`. That is outside what the repository can verify.

**Status: Partial.**

## LLM04 Data and model poisoning

**Risk.** Atlas does not train or fine-tune models, so the poisoning surface is the text it feeds into model context and retrieval: source comments and routine bodies, imported knowledge bundles, approved descriptions, and prior queries reused as templates or few-shot examples.

**Controls.**

- Screening at write time with quarantine (`screen_text`, `is_eligible_for_model_context`, `src/aida/ingest_screening.py`). The stored verdict carries `SCREENING_VERSION`, so `is_verdict_current` can find verdicts left stale by a classifier upgrade.
- Embedded text is limited. `vector_text` in `src/aida/vector_index_service.py` embeds names, plus the approved description for routines only (`approved_routine_descriptions`). It never embeds routine bodies, unreviewed source comments or drafts.
- Query memory. `check_candidate_staleness` and `find_query_memory_match` in `src/aida/query_memory.py` drop a candidate once the semantic version or a referenced table has changed. Negative feedback suppresses a candidate. Adaptation is off by default (`agent_query_memory_enabled`). Templates and exemplars are screened again before they reach the model.
- Knowledge bundle import refuses markup, links, code fences and control characters (`text_refusal` in `src/aida/okf_import_bundle.py`).
- Agent publication requires steward-confirmed exemplars (`evaluate_agent_eval_gate` in `src/aida/agent_eval_gate.py`).

**Tests.** `tests/test_ar10_screening_benchmark.py`, `tests/test_envelope_v11.py` (`is_verdict_current`), `tests/test_query_memory.py`, `tests/test_agent_orchestrator_query_memory.py` (`test_memory_adapted_sql_still_rejected_by_the_same_guard`, `test_memory_adaptation_disabled_by_default_setting`), `tests/test_ar10_ingress_wiring.py` (`test_a_prior_query_that_fails_screening_is_not_sent_as_a_template`), `tests/test_okf_import_hostile.py`, and `tests/test_agent_eval_gate.py`.

**Gaps.**

- The "confirmed" in few-shot exemplars means one positive rating from the run's own owner. `upsert_query_feedback` in `src/aida/intelligence_api.py` marks a run's memory `ELIGIBLE` on the owner's own positive rating. `find_query_memory_matches` then offers it to every user's generation on that datasource. `exemplar_fewshot_k` defaults to 3, so this is on by default. Screening and the gateway still apply, but no second person checks the exemplar (no maker-checker).
- The screens are regex (see LLM01). A stale verdict is honoured until the next scan.
- Nothing detects a gradual, plausible-looking drift in descriptions a steward approves. That risk sits with human review.

**Status: Partial.**

## LLM05 Improper output handling

**Risk.** SQL the model wrote runs against a bank source without enough checking: it mutates data, reaches outside the database, reads unauthorised tables, or runs unbounded. Or model-drafted text reaches users or other models unchecked.

**Controls.**

- Schema validation. `structured_completion` validates every answer against the caller's Pydantic model and raises `ModelOutputInvalid` on a mismatch (`src/aida/model_gateway.py`). `SqlGenerationOutput` bounds the length of every field.
- `SqlGuard.validate` (`src/aida/sql_guard.py`) does the following:
  - requires exactly one statement, and that it is a query;
  - refuses mutating or admin nodes, `SELECT INTO`, locking reads and T-SQL lock hints, cross or vacuous joins, wildcards, table-valued sources and sequence advances;
  - applies per-dialect function denylists (dblink, lo_*, pg_read_file, xp_cmdshell, openrowset and others);
  - refuses unrecognised functions and functions qualified by a user schema;
  - clamps the row limit.
- Catalog, context product and cost validation in `src/aida/sql_validation.py` (`findings_from_catalog`, `findings_from_columns`, `findings_from_context_product_scope`, `findings_from_estimate`). The same pipeline runs inside `QueryExecutionGateway.execute` and `QueryExecutionGateway.structural_findings`.
- The gateway is the only execution path (INV-2), enforced by type, by an import-linter rule in `pyproject.toml`, and by an AST scan.
- Postgres runs statements in `connection.transaction(readonly=True)` with a `statement_timeout` (`src/aida/connectors/postgres.py`). Oracle rolls back after the read (`src/aida/connectors/oracle.py`).
- The repair loop (`GovernedAgentOrchestrator._repair_generated_statement`) sends only structural findings (`REPAIRABLE_FINDING_CODES`) back to the model. A security or boundary refusal is never offered for repair, and the repaired statement goes through `execute` again in full.
- Model-drafted descriptions are screened, capped and checked against the requested columns (`validate_model_drafts` in `src/aida/column_description_model.py`). They enter review as drafts.
- The UI does not render model or catalog text as HTML: no `dangerouslySetInnerHTML` or `innerHTML` assignment in `ui-next/src` (grep, 2026-09-24).

**Tests.** `tests/test_sql_guard.py`, `tests/test_adversarial_sql_corpus.py` (per-dialect corpus, zero bypasses, and the executor raises if it is ever called), `tests/test_sql_validation.py`, `tests/test_sql_repair.py` (`test_a_security_or_boundary_refusal_is_never_offered_back`, `test_structural_findings_reads_the_catalog_and_opens_no_connector`), `tests/test_tier0_invariants.py` (`test_no_connector_execution_outside_gateway`, `test_the_connector_handed_to_the_platform_has_no_sql_surface`), `tests/test_model_gateway.py` (`test_gateway_rejects_invalid_provider_output`), and `tests/test_column_description.py`.

**Gaps.**

- The server enforces read-only on Postgres only. The `sql_guard.py` code says so. The SQL Server connector (`src/aida/connectors/sqlserver.py`) opens pytds with `readonly=True`, which sets the TDS read-only application intent: a routing hint for availability-group replicas, not a refusal of writes. For SQL Server, Snowflake, BigQuery and Databricks, anything past the AST guard depends on the source account's grants.
- The function rule lets through built-in functions the parser recognises, unless they are denylisted. A built-in with side effects that no denylist names would pass.
- The adversarial SQL corpus is hand-written and in-repo.

**Status: Covered.**

## LLM06 Excessive agency

**Risk.** An agent or model can do more than its task needs, for example approve or publish governed objects, call write-capable MCP tools, or keep acting after an operator has told it to stop.

**Controls.**

- INV-3: proposals are structurally distinct from commands (`test_model_output_types_are_inert`).
- Agent capability envelopes in `src/aida/agent_contracts.py`:
  - `envelope_violation` and `native_tool_violation` enforce the tool allowlist;
  - `context_product_violation` enforces the product boundary;
  - `agent_kill_blocking_reason` enforces per-agent and organization-wide kill switches;
  - `contract_widening` sends any widening edit to review;
  - `load_contract_for_principal` refuses an agent identity that has no envelope, or more than one.
- MCP applies the same checks on every tool path (`_native_tool_contract_denial` and `_handle_tools_call` in `src/aida/mcp_server.py`). Tools are listed only if they are PUBLISHED and the caller holds an allowed role.
- Model kill switch. `kill_switch_blocking_state` is a live database read, checked first in `structured_completion`.
- Review autonomy is bounded. `HARD_MAX_AGENT_TIER` and `effective_agent_ceiling` in `src/aida/review_risk_tiers.py` clamp agent decisions to T1, whatever the configuration says. `reviewer_agent_enabled` defaults to False. The reviewer agent (`src/aida/reviewer_agent.py`) sends model-inferred proposals to a human (`EVIDENCE_MODEL_INFERRED`), and `sampled_for_audit` sends a deterministic sample to audit.
- Maker-checker for every governed object type (`check_decision_permitted` in `src/aida/governance_decision_service.py`).

**Tests.** `tests/test_agent_contract.py`, `tests/test_r11c6_agent_contract_authority.py`, `tests/test_r11c6_native_mcp_tool_contract.py` (`test_a_killed_agent_cannot_call_any_native_tool`, `test_an_organization_wide_kill_scope_stops_a_native_tool`), `tests/test_kill_switch_drill.py`, `tests/test_reviewer_agent.py` (`test_no_t2_or_t3_type_is_ever_agent_decidable_at_the_default_ceiling`, `test_the_reviewer_agent_cannot_reach_a_publish_or_activate_path`), `tests/test_ar03_false_approval_benchmark.py`, `tests/test_tier0_invariants.py` (`test_self_approval_denied`), and `tests/test_mcp_server.py`.

**Gaps.**

- Tool certification (`run_certification_corpus` and `certification_is_active` in `src/aida/tool_certification.py`) is recorded and reported by `src/aida/tool_api.py`. I found no reference to it in the publish path or in MCP `tools/list`, so an uncertified PUBLISHED tool is callable.
- Workspace authorization is observe-only by default (`unresolved_workspace_posture`, see LLM02). Agent identities are bounded by their envelopes, but an agent with the roles to call a tool is not stopped at the workspace level.
- The reviewer agent does not meet the AR-03 adversarial bar. The benchmark pins 9 false approvals and 0 pairs told apart. It is safe only because it is off by default. Its abstention on model-drafted text rested on an optional `evidence["origin"]` key and approved a draft that lacked it; R11-MP19 made it fail closed after this snapshot, and `test_a_draft_with_no_recognised_origin_is_left_to_a_person` now pins the abstention.
- Humans without an envelope reach the native MCP tools, including the one that writes (the marketplace access request), on their roles alone. This is by design, but external agents running under a human token inherit it.

**Status: Partial.**

## LLM07 System prompt leakage

**Risk.** The system instruction gets extracted and turns out to contain credentials, internal rules or authorization logic that an attacker could use.

**Controls.**

- The SQL generation instruction in `src/aida/agent_orchestrator.py` is a short, static, non-secret string. It holds no credentials, table allowlists or policy. Authorization, masking and limits are enforced in code (`gate`, `QueryExecutionGateway.execute`), not by the prompt. Leaking it gives an attacker nothing to act on.
- Credentials never enter the payload. `_resolve_model_credential` resolves them and the adapters send them as headers.
- Both screens carry extraction patterns: `SYSTEM_PROMPT_EXTRACTION_ATTEMPT` in `src/aida/prompt_risk.py`, and `PROMPT_EXTRACTION_PATTERNS` in `src/aida/injection_defense.py`.

**Tests.** `tests/test_prompt_risk.py` (`test_high_risk_prompts_are_blocked_without_retaining_prompt_text`), `tests/test_injection_defense.py`, `tests/test_model_gateway.py` (`test_openai_adapter_uses_responses_json_schema_without_leaking_key`), and `tests/test_embedding_provider.py` (`test_gemini_sends_the_key_in_a_header_never_the_url`).

**Gaps.**

- A model can still echo its instruction inside a string literal of a valid SELECT, and the literal would come back as a result. That is low impact given what the instruction holds.
- The AR-10 residual miss `story-frame` is exactly a "print your rules" framing.

**Status: Covered.**

## LLM08 Vector and embedding weaknesses

**Risk.** Vector search leaks objects across tenants or past policy, embeddings of sensitive text leave the bank, or poisoned or incomparable vectors skew retrieval.

**Controls.**

- Every `VectorIndex` operation in `src/aida/vector_store.py` is organization-scoped.
- `PostgresBruteForceIndex.search` scores only the policy-filtered candidate allowlist, and matches on the full (owner_type, owner_id) pair. An empty allowlist returns nothing, and an oversized one is refused rather than truncated.
- `index_signature` (in `src/aida/vector_store.py` and `src/aida/embedding_provider.py`) pins model, version, dimensions and chunking. A mismatch triggers a rebuild rather than a mix of incomparable vectors.
- `resolve_embedding_provider` fails closed with no provider. It never falls back to a hash. `_validate` refuses short or wrong-width batches.
- Only names and approved routine descriptions are embedded (`vector_text` in `src/aida/vector_index_service.py`). pgvector is used only when the extension is actually installed.

**Tests.** `tests/test_vector_store.py` (`test_search_is_confined_to_the_candidate_allowlist`, `test_an_empty_candidate_set_returns_nothing_rather_than_everything`, `test_vectors_from_a_different_model_are_not_comparable`), `tests/test_embedding_provider.py`, `tests/test_vector_index_service.py`, and `tests/test_hybrid_retrieval.py`.

**Gaps.**

- `ExternalVectorIndex.search` sends only `owner_ids` and trusts the remote service's response. It does not re-check the (owner_type, owner_id) pair or the organization. The brute-force backend's own comment explains why an id-only filter admits the wrong object type.
- The user's question and metadata texts go to a hosted embedding API (OpenAI or Gemini). That path is outside route approval, the kill switch, OpenRouter preferences and token accounting (LLM03, LLM10).
- The rule that an external index must sit inside the bank network is stated in the module docstring. It is not enforced in code.

**Status: Partial.**

## LLM09 Misinformation

**Risk.** The model writes SQL that is valid and authorised but answers a different question, or drafts plausible but wrong catalog text that a reviewer approves.

**Controls.**

- Every identifier must exist in the catalog. Unknown tables and columns are blocking findings (`findings_from_catalog` and `findings_from_columns` in `src/aida/sql_validation.py`).
- Model drafts are capped at confidence 0.70, or 0.5 when based on the name only (`MODEL_DRAFT_CONFIDENCE_CAP` and `NAME_ONLY_CONFIDENCE_CAP` in `src/aida/column_description_model.py`), and enter review as drafts. `EVIDENCE_MODEL_INFERRED` in `src/aida/reviewer_agent.py` keeps model-inferred proposals away from auto-decision.
- `evaluate_agent_eval_gate` (`src/aida/agent_eval_gate.py`) blocks publication with too few confirmed exemplars or too low a pass rate. It never passes silently on zero exemplars.
- Query memory is dropped once the semantics or tables have changed (`check_candidate_staleness` in `src/aida/query_memory.py`).
- Calibration of the metric suggestion score (`score_evidence` in `src/aida/metric_suggestion_service.py`) is measured by `scripts/confidence_calibration_benchmark.py` on a synthetic corpus.

**Tests.** `tests/test_sql_validation.py`, `tests/test_column_description.py`, `tests/test_agent_eval_gate.py` (`test_zero_exemplars_is_insufficient_data_never_a_silent_pass`), `tests/test_query_memory.py`, `tests/test_confidence_calibration_benchmark.py`, `tests/test_answer_evaluation_gate.py`, and `tests/test_ar03_false_approval_benchmark.py`.

**Gaps.**

- Answer correctness has not been measured live. `scripts/answer_evaluation_benchmark.py` says so in its own docstring: nothing it outputs is an answer-quality result until someone runs it with `--live`.
- Nothing at run time checks that a valid statement answers the question asked. `SqlGenerationOutput.confidence` is the model's own figure.
- Calibration is measured for metric suggestions only, on a synthetic corpus. The AR-03 benchmark shows the reviewer's assessment cannot tell true proposals from false twins (LLM06).

**Status: Partial.**

## LLM10 Unbounded consumption

**Risk.** Questions, agents or MCP clients run up model spend, source compute or platform load without limit, whether by accident, through a retry storm, or deliberately.

**Controls.**

- Per call, `structured_completion` enforces:
  - an input token cap (`model_max_input_tokens` and the route's cap, checked before the call);
  - an output token cap;
  - a timeout.

  The question is capped at 10,000 characters by the request schema in `src/aida/schemas.py`.
- Agent budgets in `src/aida/agent_budget.py`. `reserve_run_budget` and `reconcile_run_budget` reserve against a daily token cap atomically. `per_run_violation` and `wall_clock_violation` enforce the per-run caps.
- `RouteCircuitBreaker` (`src/aida/model_route_breaker.py`) skips a failing route for a cool-down. Repair attempts are capped at 2 (`agent_sql_repair_attempts`).
- Source protection:
  - row limits (default 5,000, hard cap 100,000) clamped in `SqlGuard.validate`;
  - `gate_query_estimate` and the cost and byte findings in `src/aida/sql_validation.py`;
  - a statement timeout;
  - a per-LOB concurrency slot (`LobConcurrencyController.slot` in `src/aida/lob_concurrency.py`).
- Tenant and source quotas (`consume_quota` in `src/aida/usage_quotas.py`) are enforced for `ANALYSIS_RUNS`.
- `consume_mcp_budget` and `consume_mcp_consumer_budget` (`src/aida/mcp_budget.py`) and `consume_window_budget` (`src/aida/request_budget.py`) rate-limit MCP and GraphQL.

**Tests.** `tests/test_agent_budget_and_envelope.py`, `tests/test_agent_budget_postgres_concurrency.py`, `tests/test_model_route_breaker.py`, `tests/test_usage_quotas.py`, `tests/test_mcp_source_knowledge.py` (`test_a_spent_tool_budget_stops_the_call_before_the_handler`), `tests/test_mcp_policy.py`, `tests/test_sql_validation.py` (`test_cost_ceiling_exceeded_is_a_finding_with_numbers_only`), and `tests/test_sql_repair.py`.

**Gaps.**

- `model_token_daily_quota_per_organization` and `model_token_daily_quota_per_datasource` exist in `src/atlas/platform/config.py`, but no caller runs `consume_quota` for `UsageDimension.MODEL_TOKENS`. `record_model_spend` in `src/aida/cost_metrics.py` only records spend after the fact.
- Runs without an agent envelope, which covers every human Ask, reserve no token budget.
- The REST Ask route has no request rate limit. `mcp_budget_enabled` and `graphql_budget_enabled` both default to False.
- Embedding calls (question-time and index rebuild) have no token cap, quota or kill switch.
- Every quota defaults to None, meaning undeclared, by design.

**Status: Partial.**

## Gaps to queue

Candidate follow-ups for tracker section P. They are not opened here, since section P is the only queue. Each one names the risk it closes.

1. Enforce the model token quota before the call. Wire `consume_quota(..., UsageDimension.MODEL_TOKENS)` ahead of `structured_completion`, so the declared `model_token_daily_quota_*` settings refuse work rather than only recording it (LLM10).
2. Add a per-principal request budget on the REST Ask route. Decide whether MCP and GraphQL budgets should default on in production posture (LLM10).
3. Put embedding calls under governance: route approval, the kill switch, endpoint pinning, and token accounting (LLM03, LLM08, LLM10).
4. Re-check `ExternalVectorIndex.search` results against the (owner_type, owner_id) allowlist and the organization before use (LLM08).
5. Require a second person for query memory eligibility. Today one positive rating from the run's owner makes a query a template or exemplar for every user on the datasource (LLM04).
6. Apply `screen_metadata`'s multilingual and obfuscation handling to the question, or document why the English-only question screen is enough (LLM01).
7. Detect or redact identifiers typed into a question before it goes to the model and embedding providers (LLM02).
8. Gate MCP exposure, or publication, on an active tool certification (LLM06).
9. Make the model-origin abstention in the reviewer agent fail closed when `evidence["origin"]` is absent (LLM06, LLM09).
10. Pin GitHub Actions by commit SHA and base images by digest. Verify the gitleaks checksum, pin the `uvx` scanner versions, add a `ui-next` SBOM and a dependency update feed (LLM03).
11. Run the live answer evaluation (`scripts/answer_evaluation_benchmark.py --live`) and record the result in the capability register (LLM09).
12. State the per-engine read-only posture: server-enforced on Postgres, grant-dependent elsewhere. Add a connection-time check that the source account cannot write, where the engine allows one (LLM05).
13. Label MCP tool results as untrusted source data for the consuming agent (LLM01).
14. Act on stale screening verdicts (`is_verdict_current`), for example with a targeted re-screen job, instead of waiting for the next scan (LLM01, LLM04).
