# External destination and credential inventory

**Generated file. Do not edit by hand.**
Regenerate with `python scripts/generate_destination_inventory.py`;
`--check` fails when this file is out of date with `Settings`.

Review 2026-09-05, section 6 item 7 asks for an inventory of every external
destination and credential reference, distinguishing **configured**,
**approved**, **active**, **healthy** and **verified**. Those five are not
synonyms, and the whole value of this table is that it refuses to treat them
as one. Every row is derived from `atlas.platform.config.Settings` and from a
static read of how `src/` consumes it. Nothing here is hand-maintained.

## No value from this configuration appears in this file

Several of the settings below are credentials, and `database_url`'s shipped
default carries a password in its userinfo. The generator therefore prints
the setting **name**, a **characterization** of its default (`unset`,
`empty string`, `placeholder`, `localhost`, ...) and, for destinations, the
**host and scheme only**, with any `user:password@` removed. No setting's
value is written here -- not a credential's, not a URL's, not even a default
that is already visible in `config.py`. Read `config.py` for the defaults;
read the deployment's secret store for the values.

## What each state column means

- **Configured** -- does the software ship naming a real destination? This
  generator reads source, not a deployment's environment, so it answers that
  question and no other. A deployment that sets the variable is configured;
  this table cannot see that and does not claim to.
- **Approved** -- does a governance decision gate the use, or does whoever
  set the environment variable decide? Derived from whether the *function*
  that reads the setting also names an approval marker (`APPROVED`,
  `GovernanceReview`, `evaluate_entitlement`, `authorize_enforced`). Module
  scope was tried first and was useless -- `aida.graph_store` mentions
  `APPROVED` somewhere, which made this table claim a governance gate on the
  Neo4j password. Even at function scope this is a weak signal that says a
  governance identifier is in the same scope, not that the destination is
  approved, and the cell says so.
- **Active** -- the feature flag that decides whether the code opens the
  connection at all, and which way it defaults. Almost every outbound path
  here is off by default, deliberately (see the review's F01/F04 notes);
  that is a different fact from being unconfigured.
- **Healthy** -- whether `/health/ready` observes it. Derived from
  `aida.readiness` and the `aida.*` modules it imports directly.
- **Verified** -- whether a real destination has acknowledged real traffic.

## What this analysis cannot see

- **Every `Verified` cell is `unknown`, and that is the correct answer.**
  Verification is runtime evidence: a bucket that stored bytes and handed
  them back, a collector that acknowledged an event. No static generator can
  produce it. This is the same answer F01, F04 and section 6 item 8 give --
  filesystem archival and loopback delivery are proven; a real object-lock
  bucket and a real SOC collector are not.
- A deployment's actual environment is invisible here. `Configured` is a
  statement about the shipped default only.
- `Healthy` uses a deliberately narrow one-hop scope around
  `aida.readiness`, because a full transitive walk would reach most of the
  package through `aida.models` and claim health coverage that does not
  exist. A probe is rarely handed the setting itself -- `probe_postgresql`
  receives a session factory and `probe_temporal` a client, both built
  elsewhere -- so a setting also counts as probed when a module that reads
  it shares a leaf name with a module the probe imports, or when its name
  shares a word with a `probe_*` function. Those are structural rules, not
  a hand-written mapping, and they can be wrong in both directions.
- `Healthy` says a probe observes the destination. It does NOT say the probe
  gates: per F18, PostgreSQL is the only required probe; Temporal is
  reported and never gating.
- `Healthy` is about what the endpoint observes **when it is scraped**. A
  destination checked on a cadence by a background pass, whose verdict
  `/health/ready` then reports from storage, reads as `no readiness probe`
  here -- correctly for this column, and not the same as unwatched. Reading
  a recorded verdict is not taking a measurement, and conflating the two
  would let a sweep that stopped running months ago still look like a live
  probe.
- A destination reached through a dynamically-built string, or configured
  per-organization in a database row rather than in `Settings`, is not a
  field on this model and is therefore not in this table.

## Coverage

- Settings inventoried: **40** (24 destinations, 16 credential references)
- Rows with at least one `unknown` cell: **40**
- `unknown` cells in total: **40** (of which 40 are the `Verified` column, by construction)

### Gap list -- cells the analysis could not determine

| Cell | Rows | Which |
|---|---|---|
| verified | 40 | every row |

### Findings

- **Settings no code in `src/` reads (0):** none. A destination or credential that nothing consumes is either dead configuration or a consumer that reads it some way this analysis cannot see; either way it should not sit in `Settings` unexplained.
- **Destinations with no readiness probe (22 of 24):** `audit_archive_bucket_name`, `audit_archive_filesystem_root`, `dq_itsm_webhook_url`, `entitlement_webhook_url`, `gemini_base_url`, `hmac_signing_vault_url`, `kafka_bootstrap_servers`, `model_endpoint_urls`, `neo4j_uri`, `object_store_endpoint`, `oidc_issuer`, `oidc_jwks_url`, `openai_base_url`, `otel_endpoint`, `portal_base_url`, `redis_url`, `secrets_vault_url`, `siem_endpoint`, `slack_webhook_url`, `teams_webhook_url`, `tokenization_vault_url`, `vector_index_url`. `/health/ready` gates on PostgreSQL only and reports Temporal, the archive task, the reconnect task and the outbox backlog (F18); nothing else below is observed at all.
- **Destinations that ship pointing somewhere real (2):** `gemini_base_url`, `openai_base_url`. Every other destination is inert on arrival, which is the posture the review's F01/F04 notes describe: a deployment that has named nothing talks to nothing, rather than to a default somebody forgot about.

## Inventory

| Setting | Kind | Points at | Default | Default inert? | Consumed by | Configured | Approved | Active | Healthy | Verified |
|---|---|---|---|---|---|---|---|---|---|---|
| `audit_archive_bucket_name` | destination | an object-store bucket, named by this setting (name not shown) | bare literal (value not shown) | yes -- names nothing | `aida.main` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | `audit_archive_enabled` defaults on | no readiness probe | unknown |
| `audit_archive_filesystem_root` | destination | a local filesystem path | empty string | yes -- names nothing | `aida.main` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | `audit_archive_enabled` defaults on | no readiness probe | unknown |
| `audit_hmac_key` | credential | a credential held in process configuration | placeholder literal | yes -- development placeholder | `aida.signing` | no -- the shipped default is a placeholder | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `database_url` | destination | `localhost:5432` over `postgresql+asyncpg` | localhost default (host only shown) | yes -- local only, not an external destination | `atlas.platform.db` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | probed by `/health/ready` | unknown |
| `dq_itsm_webhook_token` | credential | a credential held in process configuration | unset (None) | yes -- names nothing | `aida.quality_service` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | `dq_itsm_webhook_enabled` defaults off | no readiness probe | unknown |
| `dq_itsm_webhook_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.quality_service` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | `dq_itsm_webhook_enabled` defaults off | no readiness probe | unknown |
| `embedding_credential_reference` | credential | a secret-store reference resolved by `aida.secrets.SecretResolver` | empty string | yes -- names nothing | `aida.embedding_provider` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `entitlement_webhook_token` | credential | a credential held in process configuration | unset (None) | yes -- names nothing | `aida.entitlements` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `entitlement_webhook_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.entitlements` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `gemini_api_key` | credential | a credential held in process configuration | unset (None) | yes -- names nothing | `aida.embedding_provider`, `aida.model_gateway`, `aida.model_route_health` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `gemini_base_url` | destination | `generativelanguage.googleapis.com` over `https` | names a remote host (host only shown) | no -- ships pointing somewhere | `aida.embedding_provider`, `aida.model_gateway`, `aida.model_route_health` | yes -- ships with a default target | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `hmac_signing_vault_key_name` | credential | the *name* of a key inside the external secret store, not the key | non-empty placeholder (value not shown) | yes -- development placeholder | `aida.signing` | no -- the shipped default is a placeholder | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `hmac_signing_vault_token_reference` | credential | a secret-store reference resolved by `aida.secrets.SecretResolver` | empty string | yes -- names nothing | `aida.signing` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `hmac_signing_vault_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.signing` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `kafka_bootstrap_servers` | destination | `localhost:19092` | localhost default (host only shown) | yes -- local only, not an external destination | `aida.newly_created_table_drafter`, `aida.projectors.graph_projector`, `aida.projectors.outbox_publisher` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `model_endpoint_urls` | destination | operator-supplied alias -> URL map | empty map | yes -- names nothing | `aida.model_gateway` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `neo4j_password` | credential | a credential held in process configuration | empty string | yes -- names nothing | `aida.graph_reconciliation`, `aida.graph_store`, `aida.projectors.graph_projector` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `neo4j_uri` | destination | `localhost:7687` over `bolt` | localhost default (host only shown) | yes -- local only, not an external destination | `aida.graph_reconciliation`, `aida.graph_store`, `aida.projectors.graph_projector` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `neo4j_user` | credential | a credential held in process configuration | non-empty placeholder (value not shown) | yes -- development placeholder | `aida.graph_reconciliation`, `aida.graph_store`, `aida.projectors.graph_projector` | no -- the shipped default is a placeholder | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `object_store_access_key` | credential | a credential held in process configuration | non-empty placeholder (value not shown) | yes -- development placeholder | `aida.main` | no -- the shipped default is a placeholder | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `object_store_endpoint` | destination | `localhost:9000` over `http` | localhost default (host only shown) | yes -- local only, not an external destination | `aida.main` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `object_store_secret_key` | credential | a credential held in process configuration | empty string | yes -- names nothing | `aida.main` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `oidc_issuer` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.api`, `aida.oidc`, `aida.security` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `oidc_jwks_json` | credential | a credential held in process configuration | unset (None) | yes -- names nothing | `aida.api`, `aida.oidc`, `aida.security` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `oidc_jwks_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.api`, `aida.oidc`, `aida.security` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `openai_api_key` | credential | a credential held in process configuration | unset (None) | yes -- names nothing | `aida.embedding_provider`, `aida.model_gateway`, `aida.model_route_health` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `openai_base_url` | destination | `api.openai.com` over `https` | names a remote host (host only shown) | no -- ships pointing somewhere | `aida.embedding_provider`, `aida.model_gateway`, `aida.model_route_health` | yes -- ships with a default target | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `otel_endpoint` | destination | `localhost:4317` over `http` | localhost default (host only shown) | yes -- local only, not an external destination | `aida.main` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `portal_base_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.governance_notifications` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `redis_url` | destination | `localhost:6379` over `redis` | localhost default (host only shown) | yes -- local only, not an external destination | `aida.mcp_budget`, `aida.unified_lineage_api` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `secrets_vault_token` | credential | a credential held in process configuration | unset (None) | yes -- names nothing | `aida.secrets` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `secrets_vault_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.secrets` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `siem_endpoint` | destination | nothing -- `internal://` is a deliberate placeholder scheme | placeholder scheme `internal://` (value not shown) | yes -- names nothing | `aida.siem_delivery` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | `siem_enabled` defaults on | no readiness probe | unknown |
| `slack_webhook_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.delivery_intents`, `aida.governance_notifications` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `teams_webhook_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.delivery_intents`, `aida.governance_notifications` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `temporal_address` | destination | `localhost:7233` | localhost default (host only shown) | yes -- local only, not an external destination | `aida.main`, `aida.workflows.scheduler`, `aida.workflows.worker` | no external target -- localhost default only | no approval gate found -- whoever sets the variable decides | `temporal_enabled` defaults on | probed by `/health/ready` | unknown |
| `tokenization_key` | credential | a credential held in process configuration | placeholder literal | yes -- development placeholder | `aida.tokenization` | no -- the shipped default is a placeholder | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `tokenization_vault_token_reference` | credential | a secret-store reference resolved by `aida.secrets.SecretResolver` | empty string | yes -- names nothing | `aida.tokenization` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `tokenization_vault_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.tokenization` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
| `vector_index_url` | destination | whatever the deployment supplies -- unset by default | unset (None) | yes -- names nothing | `aida.vector_store` | no -- the shipped default names nothing | no approval gate found -- whoever sets the variable decides | no enabling flag -- used whenever read | no readiness probe | unknown |
