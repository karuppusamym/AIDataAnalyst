# Acceptance testing guide

*Written 2026-09-12, after running everything in it against the Docker Desktop
stack on this machine. A dated snapshot; status lives in
[tracker section P](../60-delivery/03-tracker.md) and capability evidence in
the [capability register](../60-delivery/20-capability-register.md).*

This is the document to follow. Section 1 says what has already been verified
and what has not, so you know what you are re-checking versus checking for the
first time. Sections 2–4 bring the stack up and run the automated layers.
Section 5 is the part only a person can do. Section 6 covers the integrations
that need an account you own. Section 7 is what to report back.

---

## 1. What is certified, and what is not

I will not certify the platform "ready" as a whole, because that is not a
statement evidence can support. Here is what is actually true, split by how it
was established.

### Verified end to end on a running deployment (2026-09-12)

Run against the Docker Desktop stack, real PostgreSQL, real connectors, and a
live Gemini model route. Reproduce all of it with one command (section 4.3).

| Verified | How |
|---|---|
| Readiness reports dependencies **and** control posture | `/health/ready` |
| Delivery backlog is readable, and says whether the worker is on | `delivery_backlog.detail` |
| Catalog: datasources, discovered tables, discovered columns | governed API |
| **Ask refuses a governed tool by naming the input it needs**, then answers from it | API and browser |
| **Ask answers by live model generation** | Gemini, `generation_source=MODEL_GATEWAY` |
| Audit export works, and **refuses an unprivileged caller with 403** | authorized and unauthorized calls |
| Audit ledger records query execution | `query.execute` present |
| Enforcement readiness names its blockers | 3 unbound datasources found |
| Vector index builds and serves | 70 entries, `PERSISTED_INDEX` |
| **A retired provider model is detected** | planted route read UNREACHABLE, live route REACHABLE |
| **OIDC sign-in, authorization code + PKCE** | real issuer, real JWKS |
| Dev identity headers are **refused** under OIDC | HTTP 401 |
| Claim → role mapping, and the persona from the verified groups claim | `atlas-steward` → 4 roles, Steward |
| **A token cannot name its way to a platform role** | see the warning below |

### Not verified, and what each needs

| Not verified | Needs |
|---|---|
| Slack delivery to a real workspace | a Slack workspace you own (section 6.1) |
| Teams delivery to a real tenant | a Microsoft tenant you own (section 6.2) |
| Audit archive to a real S3 bucket | an AWS bucket; `Docs/50-security/audit-archive-destination-verification.md` |
| A corporate IdP: directory, MFA, consent, revocation | your real IdP. The mock issuer proves the protocol, nothing about policy |
| Accessibility with a screen reader, contrast, zoom, multi-screen | a person; `Docs/60-delivery/24-accessibility-acceptance-2026-09-12.md` |
| Safe unattended reviewer approvals | **do not enable.** Measured 9 of 14 false twins approved. Section 7 |
| Any non-Postgres connector beyond the sample SQL Server | real Oracle, BigQuery, Snowflake, Databricks accounts |
| Scale beyond the sample estate | the estate is 9 tables. Nothing here says anything about 1000+ |

### One thing to read before anything else

An authorization defect was found and fixed during this pass, by running the
OIDC flow rather than by any test. A token whose roles claim contained the
literal string `PlatformAdmin` was granted PlatformAdmin, with no entry for it
in the deployment's configured mapping. **If you are running any build from
before 2026-09-12, treat that as live.** The mapping is now closed: an external
role with no mapping entry grants nothing. Section 5.4 is how you confirm it
yourself, and you should.

---

## 2. Prerequisites

- Docker Desktop running.
- Python virtualenv at the repository root (`.venv`). Every command below uses
  it explicitly; `uv` is not installed on this machine.
- Node for the UI suite. **Note:** the user-level `~/.npmrc` pins `os=linux`,
  so `npm install` fetches Linux binaries and the test runner will not start
  until two Windows packages are placed by hand. Section 4.2 has the recipe.
- A Gemini API key for the model and embedding paths. An OpenAI key is
  optional; the account used for this pass returns `billing_not_active`, and
  the platform correctly falls back.

---

## 3. Bring the stack up

### 3.1 Start it

```bash
docker compose up -d --build
```

Nineteen services. The ones that matter for testing: `api` (port 8000),
`ui-next` (port 3001), `postgres`, `metadata-worker`, `fleet-scheduler`.

**If you have pulled new code, rebuild — do not just restart.** A running
container keeps the image and the environment it started with. This pass began
with containers ten hours old that silently lacked a whole feature's code and
every embedding variable.

### 3.2 Migrate

```bash
./.venv/Scripts/alembic.exe upgrade head
./.venv/Scripts/alembic.exe current
```

`current` must print one revision marked `(head)`. Two heads means two changes
branched from the same point; they need re-chaining before you go further.

### 3.3 Configure

Put these in `.env` at the repository root. It is gitignored; keep it that way,
and never paste a key into a file git tracks.

```
AIDA_ENVIRONMENT=development
GEMINI_API_KEY=<your key>

# Generation
AIDA_MODEL_GENERATION_ENABLED=true
AIDA_MODEL_ROUTE=gemini-bank-sql
AIDA_MODEL_ROUTE_FALLBACKS=openai-bank-sql

# Embeddings and the vector stage
AIDA_EMBEDDING_PROVIDER=gemini
AIDA_EMBEDDING_CREDENTIAL_REFERENCE=env://GEMINI_API_KEY
AIDA_EMBEDDING_MODEL_ID=gemini-embedding-001
AIDA_EMBEDDING_DIMENSIONS=768
```

`env://` is a development-only credential reference and the platform refuses
that scheme outright when `AIDA_ENVIRONMENT=production`, so this cannot follow
the repository into a deployment.

**A model route is a governed object, not a setting.** `AIDA_MODEL_ROUTE` names
a route that must exist and be APPROVED in the database, through maker-checker:

```bash
./.venv/Scripts/python.exe scripts/seed_model_route.py \
  --route-key gemini-bank-sql --provider GOOGLE_GEMINI \
  --model-id gemini-3.6-flash --credential env://GEMINI_API_KEY
```

It asks the provider whether it serves that model before drafting anything.
That check exists because it was missed once: a route was approved for
`gemini-2.0-flash`, which Google had retired, and it looked perfectly healthy
while every generated answer failed with a 404. To see the maker-checker
control refuse a self-approval, add `--same-identity`.

To supersede a route later, same command plus `--new-version`. The old version
stays SUPERSEDED with its approval intact.

### 3.4 Seed the estate

```bash
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/seed_sample_estate.py
```

This registers three sample datasources across two Postgres databases and SQL
Server, runs the platform's own connector discovery against them, requests and
approves cross-boundary grants, and publishes one parameterised governed tool
through draft → submit → independent approval. Every decision is made by a
second identity from the one that requested it, because the API enforces that.

It is safe to re-run.

---

## 4. Run the automated layers

### 4.1 Backend

```bash
./.venv/Scripts/ruff.exe check .
./.venv/Scripts/mypy.exe src
./.venv/Scripts/lint-imports.exe
./.venv/Scripts/pytest.exe
```

`AIDA_ENVIRONMENT` must be **unset** for pytest and **set** for the generator
scripts — they disagree deliberately. Do not pass `-q`; it hides the summary.

**Those four are not the whole gate set.** CI runs ten more, and a green
`pytest` says nothing about them — two of them were red on this branch while
the suite was passing, because a doc derived from the source tree was not
regenerated when a module landed. Run them:

```bash
export AIDA_ENVIRONMENT=development
./.venv/Scripts/python.exe scripts/shim_register.py --check
./.venv/Scripts/python.exe scripts/generate_architecture_map.py --check
./.venv/Scripts/python.exe scripts/generate_destination_inventory.py --check
./.venv/Scripts/python.exe scripts/check_frontend_reachability.py --check
./.venv/Scripts/python.exe scripts/check_npm_audit.py --check
./.venv/Scripts/python.exe scripts/check_docs_links.py
./.venv/Scripts/python.exe scripts/check_image_packaging.py
./.venv/Scripts/python.exe scripts/check_proxy_contract.py
./.venv/Scripts/python.exe scripts/openapi_diff.py
./.venv/Scripts/python.exe scripts/generate_ui_types.py
unset AIDA_ENVIRONMENT
```

Every one exits non-zero on a finding and prints the command that fixes it.
The `--check` ones compare a committed document against what the code
currently says; drop `--check` to regenerate. The last two rewrite the OpenAPI
baseline and `ui-next/src/lib/types.ts` in place, so check `git status`
afterwards — and if either moved, read the diff before committing it, because
those two files are the usual collision between concurrent sessions.

The full suite takes about 20 minutes. Expect roughly ten thousand passing
tests and zero failures.

**Run one suite at a time on this machine.** Five test files need a real
Postgres and reset a scratch database to do it, and two of those databases are
named after the deployment's own, so two concurrent runs resolve the same
database and one drops the schema under the other. That produced a false
failure twice during this pass, and the failure it produces is
`test_migration_orm_drift` reporting drift -- the single most alarming thing
this suite can say, and it was not true either time.

`test_migration_orm_drift` is now safe: concurrent clients serialize on a
Postgres advisory lock, so a second run waits a few seconds instead of
colliding. The other four are not, and are listed here rather than hardened
because none has been observed to fail and every one already takes an
override:

| File | Override |
|---|---|
| `test_agent_budget_postgres_concurrency.py` | `AIDA_BUDGET_CONCURRENCY_TEST_DATABASE_URL` |
| `test_governance_decision_postgres_concurrency.py` | `AIDA_DECISION_CONCURRENCY_TEST_DATABASE_URL` |
| `test_document_claims_postgres_concurrency.py` | `AIDA_DOCUMENT_CLAIMS_TEST_DATABASE_URL` |
| `test_reviewer_agent_postgres_suspension.py` | `AIDA_REVIEWER_SUSPENSION_TEST_DATABASE_URL` |

If you genuinely need two suites at once, point each at its own scratch
database with those. Otherwise just don't, and if you see a drift or race
failure, **re-run that one file alone before reporting it** — that is the
check that separates a real finding from a collision.

### 4.2 Frontend

```bash
cd ui-next
npm install
npm run typecheck
npm test
npm run build
```

If the test runner dies with `rollup/dist/native.js MODULE_NOT_FOUND`, that is
the `os=linux` pin. Fix it by placing the two Windows binaries by hand, with
versions matching what was installed:

```bash
cd ui-next/node_modules/@rollup && npm pack @rollup/rollup-win32-x64-msvc@4.63.1 \
  && tar -xzf *.tgz && rm -f *.tgz && mv package rollup-win32-x64-msvc
cd ../@esbuild && npm pack @esbuild/win32-x64@0.28.2 \
  && tar -xzf *.tgz && rm -f *.tgz && mv package win32-x64
```

Do not edit `~/.npmrc`; the pin is presumably there for building Linux images.
Never run `npm ci` or delete `node_modules` while another session is working —
that directory is shared and has been destroyed that way.

### 4.3 The deployment itself

```bash
mkdir -p scratch && ./.venv/Scripts/python.exe scripts/verify_end_to_end.py --json scratch/e2e.json
```

Seventeen checks over HTTP against the running API, with a live provider. It
verifies and never fixes, so a failure is a finding about the deployment. A
SKIP is not a PASS and is counted separately. Add `--skip-model` to avoid the
one check that costs money.

The report goes under `scratch/`, which is gitignored. Several sessions work
this branch and an untracked file in the repository root is what somebody's
`git add -A` picks up by accident.

---

## 5. What only a person can do

### 5.1 The Ask journey, in a browser

Open http://localhost:3001. Switch the organization in the scope picker to
**Northwind Retail Bank (sample)** — the shell may default to a leftover
verification organization.

Go to Ask Atlas and ask: **"Which accounts are booked at a branch?"**

- [ ] It **refuses**, and the refusal *names the input it needs* — a form
      appears asking for `branch_code`. A refusal that does not say what is
      missing is a failure.
- [ ] The status badge stays **CONNECTED**. A governed refusal is an answer,
      not a broken connection. It used to read DEGRADED here; if you see that,
      you are on an old build.
- [ ] Answer `BR-101` and submit. You get governed rows, a statement of which
      columns were masked, and a note that the rows are not stored.

Then ask something no approved tool matches, e.g. **"How many customers are
there?"**

- [ ] It answers, and the run is attributed to the model gateway rather than to
      a tool.

### 5.2 Deep links and reload

- [ ] Paste `http://localhost:3001/#/catalog`. It resolves, and the address
      rewrites to `#/analyst/catalog` — routes are journey-grouped and every
      older route is kept as an alias.
- [ ] Reload mid-journey. You stay where you were.

### 5.3 Least privilege

Switch the persona control to **Analyst** and repeat 5.1.

- [ ] Screens an analyst should not administer are absent or refuse.
- [ ] Any refusal says *why*, with something you could act on. A blank screen,
      a spinner that never ends, or "something went wrong" is a defect worth
      reporting.

### 5.4 Authentication — do not skip this one

The default stack uses development identity headers, which a production
backend rejects. To exercise real authentication:

```bash
docker compose -f compose.yaml -f compose.oidc.yaml up -d --build
```

Then at http://localhost:3001:

- [ ] You get a **sign-in gate**, naming the issuer, before any data.
- [ ] Sign in. The mock issuer's form takes a subject and a claims JSON. Use:
      `{"roles":["atlas-steward"],"groups":["atlas-stewards"],"organization_id":"<the sample org id>"}`
- [ ] You land in the **Steward** workspace — the persona came from the
      verified groups claim, and note the persona selector is **gone**: under
      OIDC it is not browser-selectable.
- [ ] **The escalation check.** Sign out, sign in again with
      `{"roles":["totally-unmapped-group","PlatformAdmin"],"groups":["not-a-known-group"],...}`.
      You must get **NOT PERMITTED** and no roles. If you are granted
      PlatformAdmin, the mapping is open and that is a privilege escalation —
      stop and report it immediately.
- [ ] With the overlay running, a request carrying only development headers
      must get **401**.

The mock issuer proves the protocol — real RS256, real JWKS, real expiry — and
nothing about a corporate IdP. It has no directory, no password, no MFA and no
revocation, and will mint a token for whatever claims you type. That is why it
must never be reachable from anything but your machine.

Return to the default stack with `docker compose up -d --build api ui-next`
and stop the issuer.

---

## 6. Integrations that need an account you own

Neither is required to run the platform. Both are optional destinations, and
both are **destination-unverified** today because verifying them needs an
account only you can create. I cannot create accounts or enter credentials on
your behalf.

### 6.1 Slack

You need a Slack workspace and an Incoming Webhook app; that produces a webhook
URL. Set `AIDA_SLACK_WEBHOOK_URL`, plus
`AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED=true` and
`AIDA_DELIVERY_WORKER_ENABLED=true` — the worker defaults **off**, which is why
the backlog signal reads `worker=disabled` on a fresh install.

Procedure and the one command that proves delivery:
`Docs/40-engineering/12-notification-delivery-runbook.md` section 3.1.

### 6.2 Microsoft Teams

Read this before configuring: **Office 365 connectors in Teams are retired.**
Microsoft rolled the deprecation out between 2026-05-18 and 2026-05-22, so the
old "incoming webhook" instructions produce nothing that works. The current
mechanism is a **Workflows** (Power Automate) webhook, and the platform now
sends an Adaptive Card to it by default.

A Teams administrator: channel → **More options (…)** → **Workflows** →
template *Post to a channel when a webhook request is received* → Save → copy
the URL, which is shown once and is a bearer credential. **Add a co-owner to
the flow** — a workflow belongs to a person, and an orphaned one goes silent.

Then set `AIDA_TEAMS_WEBHOOK_URL`. `AIDA_TEAMS_CARD_FORMAT` can be omitted; the
default is the format that works.

**The thing to know about verifying Teams:** a Workflows webhook answers
`202 Accepted` from its *trigger*, before the post-card action runs. So a flow
that then fails — bad payload, deleted channel, orphaned flow — fails
invisibly: the platform records DELIVERED with status 202 and no card was ever
posted. For Teams, unlike Slack, **a 2xx is not evidence anyone saw the
message.** The only real evidence is the Power Automate run history.

Full procedure, including that check: the same runbook, section 3.2.

---

## 7. What to report back

For anything that fails, the useful report is: what you did, what you expected,
what happened, and the correlation id if the UI showed one.

Please pay particular attention to these, which are known-weak rather than
unknown:

1. **Any refusal that does not tell you what to do.** Several were fixed this
   pass — a swallowed 403 on the organization list, a mislabelled refusal on
   Ask, two paged screens that stopped loading silently — and that class of
   defect is easy to miss from the inside.
2. **Unattended reviewer approvals stay off.** Measured against independent
   labels: 9 of 14 deliberately-false proposals were approved and 0 pairs were
   told apart. Do not enable `reviewer_agent_enabled`. If you want this
   capability, the gap is a missing check rather than an unanswerable question,
   but it needs its own work.
3. **Semantic search does not work the way the word suggests.** Embeddings here
   *re-rank* the candidates policy already authorized; they do not discover. A
   table that keyword matching never surfaced will not be found by similarity.
   Measured: five such questions, none found. Reasoning and the options:
   `Docs/10-architecture/19-embeddings-design.md`.
4. **Workspace authorization is in OBSERVING, not enforcing.** That is the
   shipped default and it is deliberate, but it means workspace-level denials
   are recorded rather than applied. `/v1/organizations/{id}/enforcement-readiness`
   tells you what would break if you turned it on; on this estate it named
   three unbound datasources.
5. **The estate is nine tables.** Nothing here has been tested at the scale the
   product is aimed at.
6. **Watch `model_routes.detail` in `/health/ready`.** It reads
   `approved=N;unreachable=N;never_checked=N;sweep=enabled`. A provider can
   retire a model under an approved route at any time, and until this existed
   the only symptom was every generated answer failing with a 404 that pointed
   at nothing. `unreachable` above zero means supersede that route — the
   command is in section 3.3 with `--new-version`. Note that REACHABLE means
   the model still exists and **not** that generation works: an account with a
   billing or quota problem lists its models perfectly well, and that failure
   is visible the first time anyone asks a question anyway.
