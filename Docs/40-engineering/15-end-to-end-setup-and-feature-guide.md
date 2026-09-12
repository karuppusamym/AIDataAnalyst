# End-to-end setup and feature guide

*Written 2026-09-12 against the Docker Desktop stack on this machine. Every
number and command below was run, not recalled. A dated snapshot; status lives
in [tracker section P](../60-delivery/03-tracker.md).*

This is the front door. It takes a machine with Docker Desktop and nothing
else, and walks every capability the platform has — including the agents and
what to configure for each.

**Three documents, and what each is for.** Use this one to set up and to see
the product work. Use the [local runbook](07-local-runbook.md) for day-to-day
operation and failure triage. Use the
[acceptance testing guide](14-acceptance-testing-guide.md) to run the test and
gate layers and to record findings. This guide does not repeat either of them;
where they own a procedure, it links.

---

## 1. Setup, in five commands

```bash
docker compose --profile full up -d --build
./.venv/Scripts/alembic.exe upgrade head
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/seed_sample_estate.py
./.venv/Scripts/python.exe scripts/seed_model_route.py --route-key gemini-bank-sql \
  --provider GOOGLE_GEMINI --model-id gemini-3.6-flash --credential env://GEMINI_API_KEY
./.venv/Scripts/python.exe scripts/verify_end_to_end.py --json scratch/e2e.json
```

The last command is the one that tells you whether it worked: 17 HTTP checks
against the running deployment. It verifies and never fixes, so a failure is a
finding about the deployment rather than about the script.

`.env` at the repository root holds the configuration. It is gitignored; keep
it that way and never paste a key into a file git tracks. Section 3 says what
goes in it.

---

## 2. Why a fresh `up -d` looks half-empty

**Read this before concluding anything was removed.** `compose.yaml` defines
**19 services and 7 volumes**, and a plain `docker compose up -d` starts
**ten** of the nineteen. The other nine are behind Compose *profiles*, so they
do not start unless you ask for them:

| Profile | Services it starts |
|---|---|
| *(default — no flag)* | `postgres`, `temporal`, `sample-source`, `sample-mssql-source`, `sample-mssql-source-init`, `migrate`, `api`, `ui-next`, `metadata-worker`, `fleet-scheduler` |
| `cache` | `redis` |
| `archive` | `minio` |
| `events` | `redpanda`, `redpanda-console`, `outbox-publisher` |
| `graph` | `neo4j`, `redpanda`, `outbox-publisher`, `graph-projector` |
| `temporal-ui` | `temporal-ui` |
| `seed` | `seed` |
| `full` | every one of the above except `seed` |

So `--profile full` is what you want while testing. Naming a service
explicitly also enables its profile, so `docker compose up -d redis` works
without the flag.

**Three services are meant to exit.** `migrate` runs Alembic and stops.
`sample-mssql-source-init` seeds the SQL Server fixture and stops.
`seed` is opt-in. All three exiting `0` is success, not failure — `docker
compose ps` hides them because it lists running containers only. Use
`docker compose ps -a` to see them.

### Redpanda, MinIO, Redis and Neo4j are used, and here is the evidence

The question is fair — running is not the same as used — so this was measured
rather than assumed, on 2026-09-12:

| Service | What it holds | Measured |
|---|---|---|
| Redpanda | `aida.platform.events.v1`, the domain event log | **465 events**; the newest two were `query.execution.completed.v1` and `agent.analysis.completed.v1` from that afternoon's Ask journey |
| Neo4j | the lineage and catalog graph | 196 `Column`, 43 `Constraint`, 30 `UnifiedLineageNode`, 27 `Table`, 25 `Schema`, 12 `Catalog` |
| MinIO | the audit archive | **264 per-organization prefixes** in `aida-audit-archive-objectlock-test`, last written the same afternoon |
| Redis | MCP budget counters and the lineage cache | 26,890 commands served; **0 keys at rest**, which is correct — the keys are short-TTL counters, so an idle stack holds none |

Redis holding nothing is the one that looks alarming and is not. If you want
to see keys, enable `AIDA_MCP_BUDGET_ENABLED=true` and make a few agent calls.

### Ports

| Service | URL |
|---|---|
| API | http://localhost:8000 (`/docs` for the OpenAPI UI) |
| UI | http://localhost:3001 |
| Redpanda console | http://localhost:8081 |
| Temporal UI | http://localhost:8080 |
| MinIO console | http://localhost:9001 |
| Neo4j browser | http://localhost:7474 |
| PostgreSQL | `localhost:5432` |
| Sample Postgres source | `localhost:55432` |
| Sample SQL Server source | `localhost:14330` |

---

## 3. Configuration

### 3.1 The minimum `.env`

```
AIDA_ENVIRONMENT=development
GEMINI_API_KEY=<your key>

# Generation
AIDA_MODEL_GENERATION_ENABLED=true
AIDA_MODEL_ROUTE=gemini-bank-sql
AIDA_MODEL_ROUTE_FALLBACKS=openai-bank-sql

# Embeddings and the vector re-ranking stage
AIDA_EMBEDDING_PROVIDER=gemini
AIDA_EMBEDDING_CREDENTIAL_REFERENCE=env://GEMINI_API_KEY
AIDA_EMBEDDING_MODEL_ID=gemini-embedding-001
AIDA_EMBEDDING_DIMENSIONS=768
```

`env://` is a development-only credential scheme and the platform refuses it
outright under `AIDA_ENVIRONMENT=production`, so this cannot follow the
repository into a deployment.

### 3.2 Fifteen of twenty-four feature flags ship off

This is the other reason a fresh install looks like it does less than it does.
Nothing here is broken or unfinished by virtue of being off — off is the
shipped posture for anything that talks to the outside world or spends money.

| Flag | Default | What turning it on gets you |
|---|---|---|
| `AIDA_MODEL_GENERATION_ENABLED` | off | Ask answers by model generation when no governed tool matches |
| `AIDA_DELIVERY_WORKER_ENABLED` | off | notifications actually leave the process (otherwise they queue) |
| `AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED` | off | review and approval events produce notifications to deliver |
| `AIDA_MCP_BUDGET_ENABLED` | off | per-agent call budgets, counted in Redis |
| `AIDA_AGENT_QUERY_MEMORY_ENABLED` | off | Ask reuses prior authorized query shapes |
| `AIDA_LINEAGE_CACHE_ENABLED` | off | lineage reads served from Redis |
| `AIDA_LINEAGE_NEO4J_READ_ENABLED` | off | lineage reads served from the graph rather than SQL |
| `AIDA_PRINCIPAL_RECONCILIATION_ENABLED` | off | the scheduler reconciles principals against the IdP |
| `AIDA_QUALITY_CERTIFICATION_EXPIRY_ENABLED` | off | certifications expire and warn |
| `AIDA_QUALITY_SEASONAL_THRESHOLDS_ENABLED` | off | seasonal DQ thresholds |
| `AIDA_QUALITY_SEASONAL_MONTH_END_ENABLED` | off | month-end DQ variance handling |
| `AIDA_DQ_ITSM_WEBHOOK_ENABLED` | off | DQ incidents raise ITSM tickets |
| `AIDA_AUDIT_ARCHIVE_LEGAL_HOLD_ENABLED` | off | object-lock legal hold on archived audit |
| `AIDA_REVIEWER_AGENT_ENABLED` | off | **leave it off.** See section 5.5 |
| `AIDA_REVIEWER_AGENT_SUSPENDED` | off | the kill switch's resting position |

The nine that ship **on**: `audit_archive`, `business_rollup_rebuild`,
`model_route_health`, `otel_metrics`, `otel_tracing`, `reaper`, `siem`,
`temporal`, `vector_index_rebuild`.

To see everything except the reviewer agent, add this block to `.env` and
rebuild `api` and `fleet-scheduler`:

```
AIDA_DELIVERY_WORKER_ENABLED=true
AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED=true
AIDA_MCP_BUDGET_ENABLED=true
AIDA_AGENT_QUERY_MEMORY_ENABLED=true
AIDA_LINEAGE_CACHE_ENABLED=true
AIDA_LINEAGE_NEO4J_READ_ENABLED=true
AIDA_PRINCIPAL_RECONCILIATION_ENABLED=true
AIDA_QUALITY_CERTIFICATION_EXPIRY_ENABLED=true
```

```bash
docker compose up -d --build api fleet-scheduler
```

**A running container keeps the image and environment it started with.** An
edit to `.env` does nothing until you rebuild. This has bitten twice: a stack
ten hours old silently lacked a whole feature's code and every embedding
variable, while looking perfectly healthy.

### 3.3 A model route is a governed object, not a setting

`AIDA_MODEL_ROUTE` names a route that must exist and be APPROVED in the
database, through maker-checker by two different identities:

```bash
./.venv/Scripts/python.exe scripts/seed_model_route.py --route-key gemini-bank-sql \
  --provider GOOGLE_GEMINI --model-id gemini-3.6-flash --credential env://GEMINI_API_KEY
```

It asks the provider whether it serves that model before drafting anything.
That pre-flight exists because it was missed once: a route was approved for
`gemini-2.0-flash`, which Google had retired, and it looked entirely healthy
while every generated answer failed with a 404 that pointed at nothing.

- `--same-identity` shows the maker-checker control refusing a self-approval.
- `--new-version` supersedes a route; the old version stays SUPERSEDED with
  its approval intact.

---

## 4. Seed the estate

```bash
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/seed_sample_estate.py
```

This registers three sample datasources across two PostgreSQL databases and
SQL Server, runs the platform's own connector discovery against them, requests
and approves cross-boundary grants, and publishes one parameterised governed
tool through draft → submit → independent approval. Every decision is taken by
a second identity from the one that requested it, because the API enforces
that. Safe to re-run.

It produces nine tables. **Nothing in this guide says anything about scale** —
the estate is a fixture, not a benchmark.

---

## 5. Walk the capabilities

Open http://localhost:3001. Switch the organization in the scope picker to
**Northwind Retail Bank (sample)** — the shell may default to a leftover
verification organization.

### 5.1 Catalog and discovery

- [ ] **Sources** lists the three seeded datasources with connection state.
- [ ] **Catalog** lists discovered tables and columns. These came from the
      platform's own connector discovery, not from a fixture file.
- [ ] Open a column. Classification, ownership and description are separate
      facts with separate lifecycles — a blank description never means
      "delete", it means nobody has published one.
- [ ] **Lineage** shows column-level lineage within a datasource.
      **Unified lineage** crosses datasources and needs a grant per read.

### 5.2 Governance: maker-checker

The invariant worth testing by hand is that nobody approves their own work.

- [ ] Propose a description change as one persona. Submit it.
- [ ] Try to approve it as the same persona. It must refuse, naming why.
- [ ] Switch persona and approve. The decision records both identities.
- [ ] **Review queues** show pending items with their evidence.

### 5.3 Ask — the interactive agent

Go to Ask Atlas and ask: **"Which accounts are booked at a branch?"**

- [ ] It **refuses, and the refusal names the input it needs** — a form appears
      asking for `branch_code`. A refusal that does not say what is missing is
      a failure.
- [ ] The status badge stays **CONNECTED**. A governed refusal is an answer,
      not a broken connection.
- [ ] Answer `BR-101` and submit. You get governed rows, a statement of which
      columns were masked, and a note that the rows are not stored.

Then ask something no approved tool matches, e.g. **"How many customers are
there?"**

- [ ] It answers by model generation, attributed to the model gateway rather
      than to a tool.

**What "semantic search" does and does not do here.** Embeddings *re-rank* the
candidates policy has already authorized; they do not discover. A table that
keyword matching never surfaced will not be found by similarity — measured on
five such questions, none found. The reasoning is in
[the embeddings design](../10-architecture/19-embeddings-design.md).

### 5.4 The three task agents

The platform runs three task agents, registered in one list per ADR-0029:
**steward**, **lineage** and **quality**. Each proposes; none decides. Each has
four controls:

```
AIDA_STEWARD_AGENT_INTERVAL_MINUTES     AIDA_STEWARD_AGENT_PRINCIPAL_ID
AIDA_STEWARD_AGENT_MAX_PROPOSALS_PER_RUN
AIDA_STEWARD_AGENT_MAX_PENDING_PROPOSALS
```

…and the same four for `LINEAGE_` and `QUALITY_`.

**All three ship disabled**, so out of the box they do nothing and that is not
a fault. An interval of `0` disables an agent and the pass returns before
opening a session. The shipped defaults:

| Agent | Interval | Per run | Max pending | Principal |
|---|---|---|---|---|
| steward | `0` (off) | 25 | 100 | `agent:steward` |
| lineage | `0` (off) | 25 | 500 | `agent:lineage` |
| quality | `0` (off) | 25 | 50 | `agent:quality` |

- [ ] Set an interval to `1` for one agent and rebuild `fleet-scheduler`.
- [ ] Watch it work: `docker compose logs -f fleet-scheduler`.
- [ ] Its proposals land in the review queue **as proposals**, attributed to
      the agent's own workload identity — not to you, and not applied.
- [ ] `MAX_PENDING_PROPOSALS` is a back-pressure ceiling: once that many of its
      proposals are unresolved, it stops proposing rather than burying the
      queue. Verify by setting it to `2`.

Authority is resolved by **identity comparison against configuration**, never
by a match on a name or a key. That is why each agent has a `PRINCIPAL_ID`.

### 5.5 The reviewer agent — leave this off

`AIDA_REVIEWER_AGENT_ENABLED` ships off and should stay off. This is not
caution about an unknown; it was measured. Against independently authored
labels, **9 of 14 deliberately-false proposals were approved, and 0 pairs were
told apart.** Both twins in a pair score identically because the
recommendation function takes no proposal content.

Its oversight controls exist and work — ceiling (`MAX_TIER`), sampling
(`SAMPLING_RATE`), confidence floor (`APPROVE_CONFIDENCE`), evidence age, and
a suspension kill switch — and are documented in the
[reviewer agent oversight runbook](11-reviewer-agent-oversight-runbook.md).
Exercise them if you want to see the controls. Do not use it to approve real
work.

The kill switch is worth one test on its own, because it silently did nothing
once: at REPEATABLE READ, 47 decisions went through past suspension and 48
approvals became durable. The isolation level is now enforced.

### 5.6 The profile-gated four

- [ ] **Events** — open http://localhost:8081, topic
      `aida.platform.events.v1`. Ask a question in the UI, then watch two new
      events arrive: `query.execution.completed.v1` and
      `agent.analysis.completed.v1`.
- [ ] **Graph** — http://localhost:7474, then
      `MATCH (n) RETURN labels(n)[0], count(*)`. The projector fills this from
      the event log, so it is only populated under the `graph` profile.
- [ ] **Archive** — http://localhost:9001. The audit archive task writes
      per-organization prefixes. Enabling legal hold adds object-lock.
- [ ] **Cache** — enable `MCP_BUDGET` or `LINEAGE_CACHE`, exercise them, then
      `docker compose exec redis redis-cli DBSIZE`.

### 5.7 Delivery

Queue and worker are separate on purpose. With
`AIDA_DELIVERY_WORKER_ENABLED=false` (the default) notifications accumulate
and nothing leaves the process — visible in `/health/ready` as
`delivery_backlog.detail = failed=0;queued=0;worker=disabled`.

Turn the worker on to drain it. Where it drains *to* needs an account you own;
see section 7.

---

## 6. The fleet scheduler

One polling loop drives nineteen passes. Each is independently gated and each
logs its own outcome, so `docker compose logs fleet-scheduler` is the single
place to see background work:

`certification_expiry_warning`, `classification_propagation`,
`custom_rule_pack`, `delivery_worker`, `due_playbooks`, `due_rule_packs`,
`entitlement_fulfilment`, `freshness_evaluation`, `graph_reconciliation`,
`graph_reconciliation_scheduler`, `model_route_reachability`, `owner_routing`,
`ownership_expiry`, `principal_reconciliation`, `reaper_scheduler`,
`review_notification`, `rollup_rebuild`, `task_agent_schedule`,
`vector_index_rebuild`.

Two are worth watching specifically:

- **`model_route_reachability`** reports through `/health/ready` as
  `model_routes.detail = approved=N;unreachable=N;never_checked=N;sweep=enabled`.
  A provider can retire a model under an approved route at any time. It
  **lists** models and never generates, never changes a route's `status`, and
  reports UNKNOWN as its own answer rather than collapsing it into
  UNREACHABLE. REACHABLE means the model exists — **not** that generation
  works; an account with a billing problem lists its models perfectly well.
- **`vector_index_rebuild`** keeps the embedding index current. Before it
  existed nothing scheduled a rebuild, and the persisted index silently
  dropped a whole candidate type.

---

## 7. What needs an account you own

I cannot create accounts or enter credentials on your behalf, so these four
stay unverified until you do.

| Capability | What it needs |
|---|---|
| Slack delivery | a Slack workspace and an Incoming Webhook app → `AIDA_SLACK_WEBHOOK_URL`. Procedure: [notification delivery runbook](12-notification-delivery-runbook.md) §3.1 |
| Teams delivery | a Microsoft tenant. **Office 365 connectors in Teams are retired** (rolled out 2026-05-18 to 2026-05-22), so the old "incoming webhook" instructions produce nothing that works. Use a **Workflows** webhook; the platform sends an Adaptive Card by default. Runbook §3.2 |
| Audit archive to real object storage | an AWS bucket with object lock. [Destination verification](../50-security/audit-archive-destination-verification.md) |
| A corporate IdP | your real IdP. The mock issuer proves the protocol — real RS256, real JWKS, real expiry — and nothing about directory, MFA, consent or revocation |

**The one thing to know about verifying Teams:** a Workflows webhook answers
`202 Accepted` from its *trigger*, before the post-card action runs. A flow
that then fails — bad payload, deleted channel, orphaned flow — fails
invisibly: the platform records DELIVERED with status 202 and no card was ever
posted. For Teams, **a 2xx is not evidence anyone saw the message.** The only
real evidence is the Power Automate run history. Add a co-owner to the flow; a
workflow belongs to a person, and an orphaned one goes silent.

### Authentication is worth doing even without an IdP

```bash
docker compose -f compose.yaml -f compose.oidc.yaml up -d --build
```

This is authorization-code + PKCE against a real local issuer. Section 5.4 of
the [acceptance testing guide](14-acceptance-testing-guide.md) has the
checklist, including the escalation check you should not skip — an
authorization defect was found there by running the flow rather than by any
test, and a token whose roles claim merely contained the string
`PlatformAdmin` was granted it.

---

## 8. Reporting back

For anything that fails: what you did, what you expected, what happened, and
the correlation id if the UI showed one.

Four classes of finding are worth more than the rest:

1. **Any refusal that does not tell you what to do.** A blank screen, an
   endless spinner, or "something went wrong" is a defect even when the
   underlying denial is correct.
2. **Anything attributed to the wrong identity** — an agent's proposal
   credited to you, an approval that took one identity where it should take
   two.
3. **Any value that reached a place it should not.** The control plane is
   value-free by design; a source value in a log, an event or a catalog field
   is a serious finding.
4. **Anything this guide told you to expect that did not happen.** That is a
   defect in the guide, and I would rather hear it than have you work around
   it.
