# Infra Unblock Runbook — 2026-09-02

Companion to `03-tracker.md`. These are the P0/P1 rows marked IN PROGRESS or
BLOCKED specifically because no live Postgres/Vault instance was reachable in
the sandbox that did the work — not because the feature isn't built. Run
these against your own Docker Desktop and send the output back so the
tracker can be updated with real live-infra evidence (per the AU-2
live-call-site rule: a live-infra proof, explicitly labeled as such, is
allowed evidence).

Run everything from the repo root, in the same shell you normally run
`uv run pytest` from.

## Phase 1 — CN-3: PostgreSQL 14 version fixture (fastest, fully wired already)

```bash
docker compose -f tests/fixtures/postgres_versions/compose.yml up -d
export AIDA_CN3_POSTGRES16_FIXTURE_DATABASE_URL=postgresql://aida:aida-local-only@localhost:55416/aida
export AIDA_CN3_POSTGRES14_FIXTURE_DATABASE_URL=postgresql://aida:aida-local-only@localhost:55414/aida
AIDA_ENVIRONMENT=development uv run pytest tests/test_postgres_version_fixtures.py -v
docker compose -f tests/fixtures/postgres_versions/compose.yml down
```

Expect both the 16 and 14 tests to pass with no SKIPPED. Send me the pytest
output; I'll update CN-3's row to say the 14 leg has now run live.

## Phase 2 — Vault-backed items (QG-5 signing, AU-10 secrets, QG-6 tokenization)

### 2a. Start a Vault dev server

```bash
docker run --cap-add=IPC_LOCK -d --name aida-vault-dev -p 8200:8200 \
  -e VAULT_DEV_ROOT_TOKEN_ID=root hashicorp/vault:latest
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=root
```

Dev mode auto-unseals and enables KV v2 at `secret/` automatically.

### 2b. AU-10 — KV v2 secret provider (`src/aida/secrets.py::VaultKvSecretProvider`)

```bash
curl -s -H "X-Vault-Token: root" -H "Content-Type: application/json" \
  -X POST -d '{"data": {"value": "hello-from-vault"}}' \
  http://127.0.0.1:8200/v1/secret/data/bank/data-sources/core

curl -s -H "X-Vault-Token: root" \
  http://127.0.0.1:8200/v1/secret/data/bank/data-sources/core | python3 -m json.tool
```

Expect `data.data.value == "hello-from-vault"` and `data.metadata.version == 1`
— that's the exact wire contract `VaultKvSecretProvider.resolve()` implements.
Send me this JSON.

Fuller proof (optional): point the app itself at it —

```bash
export AIDA_SECRETS_VAULT_URL=http://127.0.0.1:8200
export AIDA_SECRETS_VAULT_TOKEN=root
export AIDA_CREDENTIAL_PROVIDER=vault
```

then reference `vault://bank/data-sources/core` from a real credential
reference in the app and confirm it resolves — ask me for the exact call
site if you want to go this far.

### 2c. QG-5 — Transit engine / HMAC signing (`src/aida/signing.py::VaultTransitSigningProvider`)

```bash
curl -s -H "X-Vault-Token: root" -X POST http://127.0.0.1:8200/v1/sys/mounts/transit -d '{"type": "transit"}'
curl -s -H "X-Vault-Token: root" -X POST http://127.0.0.1:8200/v1/transit/keys/audit-hmac

curl -s -H "X-Vault-Token: root" -H "Content-Type: application/json" \
  -X POST -d "{\"input\": \"$(echo -n 'test-payload' | base64)\"}" \
  http://127.0.0.1:8200/v1/transit/hmac/audit-hmac | tee /tmp/hmac.json

HMAC=$(python3 -c "import json;print(json.load(open('/tmp/hmac.json'))['data']['hmac'])")

curl -s -H "X-Vault-Token: root" -H "Content-Type: application/json" \
  -X POST -d "{\"input\": \"$(echo -n 'test-payload' | base64)\", \"hmac\": \"$HMAC\"}" \
  http://127.0.0.1:8200/v1/transit/verify/audit-hmac
```

Expect `{"data":{"valid":true}}`. Send me the output.

### 2d. QG-6 — Transform engine / tokenization — read before running

HashiCorp Vault's **Transform** secrets engine (what
`VaultTransformTokenizationProvider` targets) is **Enterprise-only** — it
will not mount on the plain `hashicorp/vault` community image. Before
spending time here: do you have, or want to spin up, a Vault Enterprise
trial? If not, QG-6 stays genuinely blocked on licensing, not effort, and
the tracker row should say so rather than imply Docker alone solves it.

## Phase 3 — QG-2: native RLS/masking DDL against a live Postgres

No existing pytest env-var switch for this one yet — the row's own note says
the apply path has never run live. Reuse the Postgres 16 container from
Phase 1 (or start a standalone one). Tell me you're ready for this phase and
I'll write a short, one-off verification script against
`policy_native_sync.py`'s real apply path (checking its exact signature
first, rather than guessing) instead of a canned command block here.

## Phase 4 — heavier items (deliberately not turnkey commands)

- **LN-1** (real Airflow → OpenLineage event): needs a real Airflow instance
  with the OpenLineage provider installed, configured to POST to
  `POST /v1/lineage/openlineage`. Roughly half a day of setup (Airflow
  docker-compose + provider config + a DAG run), not a few commands — say
  the word and I'll build the full compose stack.
- **CT-2 / PR-5** (1M-table / 30M-column live-scale proof): before writing
  anything here, pick a real target scale — generating and profiling 1M
  synthetic tables takes real time and disk even in Docker. Tell me the
  scale you actually want proven (the tracker's literal 1M target, or a
  smaller number you're comfortable calling representative) and I'll build
  the data-generation + timing harness for that number.

## Not fixable with Docker at all

- **CN-1b / CN-1c / CN-2a / CN-2b** — need real Oracle/BigQuery/Snowflake/Databricks credentials.
- **SM-1, UX-4, AT-10** — BLOCKED on `models.py`/`schemas.py` scope decisions, not infrastructure.
- **UX-5** (accessibility audit) — needs a browser, not Docker. If your app
  is running under Docker Desktop, give me the URL and I can drive an
  axe-core/keyboard-nav pass through Chrome directly instead of a terminal
  command.

---

Paste the output of each phase back as you go; I'll fold it into the
matching tracker row in `03-tracker.md` with the evidence explicitly labeled
as a live-infra run, per the AU-2 rule.
