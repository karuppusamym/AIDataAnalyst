# Deployment alignment and enablement runbook

> Status: Authoritative for review-2026-09-16 F03, F04 and F05, written 2026-09-16.
> Audience: whoever is about to make the running deployment match the reviewed
> commit, and then turn on the parts of it that ship inert.
> Related: `scripts/check_deployment_parity.py`, `tests/test_deployment_parity.py`, `aida.workflows.scheduler`, `aida.authorization_posture`, `aida.workspace_access`, `atlas.platform.config`.

Like [12 Notification delivery](12-notification-delivery-runbook.md) and unlike
the dated review snapshots elsewhere in `Docs/`, this file is maintained in
place: it tells an operator what to do *now*, so a stale procedure here is a
wrong instruction rather than a historical record.

Three findings, one procedure, because they have to be done in this order.

| Finding | What it says | Section |
|---|---|---|
| F03 | The deployment is not running the reviewed code: an older image and a database one migration behind, while every source-side gate is green | [1](#1-the-decision-before-the-deploy), [2](#2-deploy-the-reviewed-commit), [3](#3-verify-parity-afterwards) |
| F04 | The maintenance loop ships inert, and the intended cadence was recorded nowhere an operator would look | [4](#4-enable-the-maintenance-loop), [5](#5-discovery-cadence) |
| F05 | The authorization posture and the outbound delivery worker ship off, and enabling either badly is hard to undo | [6](#6-authorization-posture), [7](#7-notification-delivery) |

**Every step below is marked READ-ONLY or STATE-CHANGING.** The read-only ones
are the evidence for the state-changing ones, and each state-changing step
names its consequence and how reversible it is. Section
[8](#8-the-state-changing-steps-in-one-table) is the same list as a checklist,
which is the part to get approved before starting.

## 0. What was measured, and how to re-measure it

On 2026-09-16, against the local stack:

* Source Alembic head `f4a8c1d7e236`; the configured database at
  `b7e2d9c4f158`. Exactly one migration behind, and the edge between them is
  `migrations/versions/f4a8c1d7e236_r11fp07_routine_edge_via_routine.py` — one
  additive nullable column with a clean `downgrade()`. (Later the same day
  three further revisions were authored against that head by three parallel
  sessions; re-read the head count before deploying, per
  [§2.2](#22-the-deploy-and-the-migration-are-one-action-and-cannot-be-separated).)
* The live `/openapi.json` served 404 paths and 501 schemas;
  `Docs/90-reference/openapi-baseline.json` and a spec freshly generated from
  the working tree both had 405 and 503. **The baseline and the source agree
  exactly, so the drift is entirely deployment-side.** The one missing path was
  `/v1/datasources/{datasource_id}/footprint-gaps/{kind}`, and the live
  `AgentAnalysisRequest` had no `context_product_key`.
* The deployed image's `Settings` had no `footprint_metrics_interval_seconds`,
  which exists in the tree — so the running scheduler did not contain
  `run_footprint_metrics_pass` at all, at any configured value, while the API
  answered 200 to everything.

That last point is the shape of F03 worth remembering: **a healthy deployment
is not an aligned deployment.** Nothing in CI could have caught it, because
`scripts/openapi_diff.py` generates the spec in-process and never talks to a
deployment, and the CI migration gate only counts `alembic heads` source-side.
Both compare source against source.

READ-ONLY. Re-measure all of it with one command:

```bash
python scripts/check_deployment_parity.py --base-url http://localhost:8000
```

Exit 0 is parity, 1 is drift, 2 is "could not tell" — an unreachable
deployment is never reported as parity. See section
[3](#3-verify-parity-afterwards) for what it checks and how it runs in CI.

## 1. The decision before the deploy

STATE-CHANGING, and the only genuinely irreversible risk in this document is
here rather than in any of the switches below.

The deploy applies a migration (section [2](#2-deploy-the-reviewed-commit)
explains why they are one action). This particular migration adds one nullable
column and drops it again on `downgrade()`, so its own rollback is clean. That
is a property of *this* edge, not of migrations in general: re-read the pending
revision before relying on it, and re-read
[05 CI/CD and release](05-ci-cd-and-release.md) for the release and rollback
contract.

Decide and record, before touching anything:

1. **Do you have a backup you have restored from?** A backup nobody has
   restored is a hope. `postgres-data` is a Docker volume on the local stack;
   in a cluster it is whatever the database's own backup policy is.
2. **What is the rollback?** For a one-column additive revision: redeploy the
   previous image and run `alembic downgrade -1`. For a set of revisions, the
   rollback is the oldest revision's `downgrade()`, and any revision that drops
   or rewrites data does not have one.
3. **Who is told, and when?** The API is unavailable between the old container
   stopping and the new one passing its health check.

## 2. Deploy the reviewed commit

STATE-CHANGING. Consequence: the API, worker and scheduler restart on a new
image, and the database schema moves to the source head. Reversibility: see
section [1](#1-the-decision-before-the-deploy).

### 2.1 The rebuild must repeat the original profile flags

`compose.yaml` puts everything optional behind a profile —
`cache`, `graph`, `events`, `archive`, `temporal-ui`, and `full` for all of
them. A profile that is not named on the command line is not just unstarted:
**Compose will not recreate a running service whose profile is disabled.** So a
plain

```bash
docker compose up -d --build     # WRONG if the stack was started with profiles
```

against a stack that was brought up with `--profile full` rebuilds the default
services and silently leaves the profiled ones — on the measured stack:
`redis`, `neo4j`, the graph projector and the outbox publisher — running the
*old* image. That is a mixed-version set: two images against one database, and
the symptom is an intermittent, unreproducible bug in whichever half you are
not looking at.

READ-ONLY — find out which profiles are actually running before you rebuild:

```bash
docker compose ps --services                 # what is up now
docker ps --format '{{.Names}}\t{{.CreatedAt}}'   # and how old each image is
```

If the list includes any service the `compose.yaml` header marks as profiled,
repeat that profile. For the measured stack (16 containers, including MinIO,
the Temporal UI and the Redpanda console) that is:

```bash
ATLAS_BUILD_COMMIT=$(git rev-parse HEAD) docker compose --profile full up -d --build
```

`ATLAS_BUILD_COMMIT` is optional. It labels the image with the commit it was
built from, so a later parity run can say "built from b76842d, 2 commits
behind" instead of only "different". Parity does not depend on it: see
comparison 5 in section [3](#3-verify-parity-afterwards).

Then confirm every container's `CreatedAt` moved. One that did not is a service
whose profile you forgot.

### 2.2 The deploy and the migration are one action, and cannot be separated

`compose.yaml` line 375 runs `alembic upgrade heads` in the `migrate` service,
and `api`, `metadata-worker` and `fleet-scheduler` each declare
`depends_on: migrate: condition: service_completed_successfully`. So:

* You cannot deploy the image without applying the migration — the app services
  will not start until `migrate` exits 0.
* You cannot apply the migration without deploying the image — `migrate` builds
  from the same `Dockerfile` as the app services.
* If the migration fails, nothing starts, and the old containers are already
  gone. **That is the failure mode section
  [1](#1-the-decision-before-the-deploy) exists for.**

`heads`, not `head`, is deliberate: this repository has independent Alembic
branches and `head` fails as soon as there is more than one.

READ-ONLY, and do this before deploying:

```bash
alembic heads      # expect exactly one line ending "(head)"
python scripts/check_deployment_parity.py   # reports every head it found
```

Several heads means several sessions authored a revision against the same
parent. `alembic upgrade heads` will still apply all of them, so the deploy
does not fail — which is the problem: the deployed schema is then the union of
three branches that were never reviewed together, and the CI `migrations` job
(which pins exactly one head) will be red on the commit you just shipped.
Resolve it with a merge revision first, so the reviewed commit names one
schema state. This is not hypothetical: on 2026-09-16 the tree briefly carried
three heads, all children of `f4a8c1d7e236`, from three parallel sessions.

In Kubernetes the same coupling has to be built rather than inherited: run the
migration as a `Job` (or an init container) that the Deployment's readiness
depends on. A rolling update that lets new pods serve against an un-migrated
schema is the same finding in a different shape.

## 3. Verify parity afterwards

READ-ONLY, always. `scripts/check_deployment_parity.py` issues `GET`s, one
`SELECT version_num FROM alembic_version`, and one introspection command inside
the running API container. It never migrates, rebuilds, restarts or writes, and
it never regenerates the OpenAPI baseline — the committed baseline is the
promise being checked, so regenerating it there would erase the finding instead
of reporting it.

Five comparisons, each naming what differs rather than counting it:

1. **Migrations.** The deployed `alembic_version` against the source heads,
   with every unapplied revision named in apply order. A deployed revision this
   tree does not contain is reported as a diverged history, not as "behind",
   because `alembic upgrade heads` is the wrong advice for it.
2. **HTTP surface.** The live `/openapi.json` path set and schema set against
   `Docs/90-reference/openapi-baseline.json`, as a symmetric difference by
   name. Symmetric because an image *newer* than the baseline is drift too.
3. **Readiness.** Every `required` dependency must be UP, and the `controls`,
   `delivery_backlog`, `outbox_backlog` and `workspace_authorization` signals
   are printed verbatim — they are the inputs to sections
   [4](#4-enable-the-maintenance-loop) to [7](#7-notification-delivery).
4. **Settings.** Every setting the source declares must exist in the deployed
   image's `Settings`. This is the comparison that catches an image older than
   the code while the API answers 200 to everything, and it shares its parser
   with `scripts/generate_configuration_inventory.py` so the two cannot
   disagree about what the source declares.
5. **Code identity.** The `build.source_digest` readiness signal against the
   same digest computed over this checkout — a SHA-256 over every file the
   Dockerfile copies (`src/`, `sdk/`, `migrations/`, `pyproject.toml`,
   `uv.lock`, `alembic.ini`), line endings normalised, bytecode ignored
   (`aida.source_identity`). Where `docker exec` works it also names the files
   that differ. Comparisons 1 to 4 only see a change that moves schema, routes
   or settings: on 2026-09-19 they all matched, and the script printed "running
   this tree", against an image 40 minutes older than HEAD that was missing two
   fixes (R11-D17). A commit hash would not close that gap, because the image is
   built from the working tree and can hold edits no commit names — so the
   digest decides, and `build.commit` is only printed beside it. An image built
   before this check existed publishes no digest and is reported UNKNOWN, never
   MATCH. Not covered: the base image and the Dockerfile itself.

```bash
# After the deploy. Expect: "Parity: the deployment is running this tree."
python scripts/check_deployment_parity.py

# Kubernetes, where there is no local container to exec into:
kubectl exec deploy/aida-api -- python -c \
  'import json;from atlas.platform.config import Settings;print(json.dumps(sorted(Settings.model_fields)))' \
  > settings.json
python scripts/check_deployment_parity.py --base-url https://atlas.internal --settings-json settings.json
```

A skip is not a pass. An unreachable deployment, an absent `docker`, a
container that cannot be introspected: each is reported UNKNOWN and exits 2.
`--check` collapses that to 0, which is how the `deployment-parity` job in
`.github/workflows/ci.yml` runs it — a CI job with no deployment to reach
must not fail the build for that reason, while real drift still fails it.

The comparison logic is covered by `tests/test_deployment_parity.py` against
fixtures, including `test_an_unreachable_deployment_does_not_fail_a_ci_build`
and `test_the_committed_baseline_does_not_drift_against_itself`, so the gate
can be trusted not to be noise — and by
`test_a_stale_image_with_matching_schema_routes_and_settings_is_not_parity`,
the 2026-09-19 false parity as a test. `tests/test_source_identity.py` pins the
digest to the Dockerfile's COPY lines, so a new COPY cannot ship code the
digest does not see.

## 4. Enable the maintenance loop

Every interval here ships at `0`, and `0` means never: the pass returns before
it opens a database session (`aida.workflows.scheduler`). That is a recorded
product decision, not an oversight — each one is an *Opt-in* in
`scripts/configuration_decisions.py` with its precondition named, and
`tests/test_configuration_inventory.py` fails if a setting ships off without a
decision. The intended production values now live as commented entries in
`.env.example` and `infra/k8s/base/configmap.yaml`; this section is the order.

**The hazard, stated once.** Two of these passes OPEN data-quality incidents,
and an open CRITICAL incident fails governed tools closed (DQ-3, enforced on
both the REST and the agent execution path). **Setting the interval back to 0
does not close the incidents it already opened.** Only a person working the
incident queue does, through
`POST /v1/quality-incidents/{incident_id}/transition`. So an interval is not a
switch you can flip back: it is a switch that can generate work only a human
can clear.

Set these on the **scheduler**, not only the API. The scheduler is the process
that runs the passes; a value set only on the API changes nothing. On the local
stack both read `.env`, so one file covers both; in Kubernetes the API and
scheduler have separate pod specs.

### 4.1 First, produce something to consume

READ-ONLY check:

```bash
docker exec aida-platform-postgres-1 \
  psql -U aida -d aida -tAc "SELECT count(*) FROM metadata_change_signal"
```

On the measured deployment this is **0**. Change-signal processing consumes
`PENDING` rows from that table, and nothing writes one until a rescan observes
a change against a source that has actually changed. Turning the interval on
first is not harmful, but it is not a test of anything either: the pass will
sweep nothing and look healthy.

So run a rescan first (section [5](#5-discovery-cadence)), against a source
where something has been altered, and confirm a row appears.

### 4.2 Then, in this order

STATE-CHANGING, one at a time, with a look at the incident queue between each.

1. `AIDA_CHANGE_SIGNAL_PROCESSING_INTERVAL_MINUTES=15` — consequence: a
   redefined view or a retired table opens a CRITICAL incident that holds the
   governed tools over it; a table that changed shape opens a WARNING, and
   those tools still run and say why they might be wrong
   (`aida.change_signal_processing`). Reversibility: **partial** — back to 0
   stops new incidents, and leaves every incident already opened.
2. `AIDA_CONTEXT_REBUILD_INTERVAL_MINUTES=30` — consequence: regenerated
   tools, descriptions and context product versions are drafted into their
   review queues, and a source-change hold is resolved once nothing standing on
   the view is stale. It opens no incidents; it creates human review load.
   Reversibility: back to 0 stops new drafts; the drafts already filed wait for
   a reviewer. Enable it *after* change-signal processing, because that is what
   produces the staleness it acts on.
3. `AIDA_FRESHNESS_EVALUATION_INTERVAL_MINUTES=60` — consequence: watermark
   contracts are evaluated per datasource, and a table nobody has observed
   opens a CRITICAL freshness incident which then holds the tools over it
   (R11-B8). Reversibility: **partial**, exactly as (1). Approve the watermark
   expectations *first*, or this is a scheduled outage of the answers that
   depend on the quietest tables.
4. `AIDA_CLASSIFICATION_PROPAGATION_INTERVAL_MINUTES=1440` — consequence:
   classification proposals are filed for human review on a cadence. Proposals,
   not changes: nothing is applied without a reviewer. Reversibility: clean.

READ-ONLY between each step:

```bash
docker exec aida-platform-postgres-1 psql -U aida -d aida -tAc \
  "SELECT severity, status, count(*) FROM data_quality_incident GROUP BY 1,2 ORDER BY 1,2"
```

### 4.3 The agent intervals do nothing on their own

`AIDA_STEWARD_AGENT_INTERVAL_MINUTES`, and the same for `lineage`, `quality`
and `tool`, schedule governed agent runs (ADR-0029). An interval alone changes
nothing: nothing runs until an APPROVED AGENT-kind version exists carrying a
governing agreement for that agent's principal — `agent:steward`,
`agent:lineage`, `agent:quality`, `agent:tool`. Setting an interval with none
approved is inert, and is not evidence that the schedule works.

When one is approved, each run stays bounded by that agent's
`_max_proposals_per_run` and `_max_pending_proposals` defaults, and every
proposal is T2: it waits for a person. Reversibility: clean — back to 0 and no
further runs are scheduled; the proposals already filed wait for review.

## 5. Discovery cadence

Rescan cadence is **not a setting**. It is a per-datasource `ScanPolicy` row,
read and written through `GET` / `PUT
/v1/datasources/{datasource_id}/scan-policy`, and the API's own default is 60
minutes.

READ-ONLY. On the measured deployment, one datasource had an enabled daily
`FULL` policy that last ran 2026-09-16 00:34 UTC, and three were **disabled at
525600 minutes — one year**, which is "off" written as a number:

```bash
docker exec aida-platform-postgres-1 psql -U aida -d aida -tAc \
  "SELECT datasource_id, enabled, interval_minutes, mode, last_triggered_at FROM scan_policy ORDER BY enabled DESC"
```

STATE-CHANGING. Enabling one of those three, or shortening its interval, queues
real discovery runs against the source: connector load, ingestion batches, and
— once section [4](#4-enable-the-maintenance-loop) is on — change signals and
the incidents they open. Do one datasource at a time, and watch
`GET /v1/datasources/{datasource_id}/metadata-ingestions` before doing the
next. Reversibility: clean for the policy itself (set `enabled` back to false);
the discovery runs already queued will still run, and anything they discovered
stays discovered.

A single rescan on demand, without changing the policy, is
`POST /v1/datasources/{datasource_id}/metadata-ingestions`. That is the right
way to produce the first change signal for section
[4.1](#41-first-produce-something-to-consume).

## 6. Authorization posture

READ-ONLY first, always. `/health/ready` reports
`controls.workspace_authorization`, and on the measured deployment it said
`OBSERVING`, with `workspaces_total=1, enforcing=0, observing=1` and
`unresolved_scope=PROCEEDS_UNDECIDED`. That is the ADR-0018 migration state:
workspace decisions are logged and **not** enforced.

The migration to enforcement is four ordered steps, recorded in
`atlas.platform.config` beside the setting itself and in
[ADR-0018](../10-architecture/adr/ADR-0018-three-axis-tenancy-and-classification.md):

1. READ-ONLY. Drive `authorization.workspace_unresolved` — the gate's own
   warning log — to zero for this environment. It counts the callers not yet
   passing a workspace id, and while it is non-zero step 3 would start refusing
   real traffic.
2. READ-ONLY, then STATE-CHANGING per workspace. Decide each workspace on
   `GET /v1/organizations/{organization_id}/enforcement-readiness`
   (`aida.workspace_access.enforcement_readiness`), which summarises the shadow
   record: would-be denials, distinct principals affected, top reason codes.
   Read its own caveat before trusting it — `ready` is a blunt "no recorded
   would-be denials in the window", and a workspace nobody used this week also
   has none. Then flip that workspace SHADOW → ENFORCE. Consequence: requests
   that were logged as would-be denials are now denied. Reversibility: clean —
   set the workspace back to SHADOW.
3. STATE-CHANGING. `AIDA_UNRESOLVED_WORKSPACE_POSTURE=DENY`. Consequence: a
   request whose workspace cannot be resolved is refused instead of proceeding
   undecided. Reversibility: clean — back to `SHADOW`, and note that step 4
   then refuses to start.
4. STATE-CHANGING, and the one with teeth.
   `AIDA_WORKSPACE_AUTHORIZATION_POSTURE=ENFORCING` makes steps 1–3
   permanently checked rather than remembered: `Settings` refuses to construct
   unless step 3 is set, and
   `aida.authorization_posture.assert_startup_posture` refuses to start the API
   if any ACTIVE workspace is not in ENFORCE. **So after step 4, regressing
   steps 2 or 3 is an API that will not start.** Reversibility: clean in
   itself — set it back to `OBSERVING` — but a deployment that regressed a
   workspace while ENFORCING has to fix the workspace or the setting before it
   will boot, and there is no way to discover which from a crash-looping
   container. Only an *observed* contradiction fails startup: a report whose
   workspace inventory could not be read is deliberately not a startup
   failure, because a database outage at boot must not become a crash loop.

Do not set step 4 in the same change as steps 2 and 3.

## 7. Notification delivery

READ-ONLY first, and this one matters more than it looks.

```bash
curl -s http://localhost:8000/health/ready | python -m json.tool | grep delivery_backlog
python scripts/delivery_history.py --state RETRYING
```

On the measured deployment: `failed=0; queued=9; queued_notification=9;
oldest_age_seconds≈324000; worker=disabled`. All nine intents were `RETRYING`
with `attempt_count=5`, enqueued in the same millisecond on 2026-09-12 18:25
UTC, with `last_error = transport error: [Errno 111] Connection refused`.

**`delivery_max_attempts` is 6.** So those nine intents are one attempt from
terminal. Enabling the worker while the destination is still unreachable spends
the last attempt on each and moves all nine to `DEAD_LETTER`, which is terminal
and not re-queueable. The queue is not a buffer that waits for you; it is a
budget that is nearly spent.

STATE-CHANGING, in this order, and not before:

1. **Verify the destination is reachable, with the destination's own
   evidence.** Do not re-derive the procedure: [12 Notification delivery
   §3](12-notification-delivery-runbook.md#3-verifying-a-real-destination) is
   the authority, including §3.1 for Slack, §3.2 for the Teams Workflows path
   (where a 2xx is *not* evidence anyone saw the message) and §3.4 for SIEM
   collectors.
2. Then `AIDA_DELIVERY_WORKER_ENABLED=true`. Consequence: the nine queued
   intents are attempted. Against a verified destination they deliver; against
   an unreachable one they dead-letter. Reversibility: setting it back to false
   stops the worker, and does not un-dead-letter anything.
3. `AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED` is **also off**, so nothing new is
   being enqueued while you work. That is why step 2 is safe to do on its own
   and watch: the queue is a fixed nine, not a growing stream. Turn this on
   last, once the worker has drained the nine. Consequence: governance events
   start enqueuing intents. Reversibility: clean.

If the nine are already lost, that is nine notifications nobody received about
governance decisions taken on 2026-09-12; the audit ledger still records the
decisions themselves, which is the invariant that holds
(`aida.delivery_intents`).

## 8. The state-changing steps, in one table

Everything above that changes state, in the order it must happen. Read-only
steps are omitted — run all of them freely, and run the parity check between
every row here.

| # | Action | Consequence | Reversible? |
|---|---|---|---|
| 1 | Confirm a restored-from backup and a named rollback | None; this is the gate for everything below | n/a |
| 2 | `docker compose --profile <the original profiles> up -d --build` | API, worker and scheduler restart on the new image; `alembic upgrade heads` applies `f4a8c1d7e236`. Brief API unavailability. Omitting a profile leaves a mixed-version set | Redeploy the previous image and `alembic downgrade -1`. Clean for this revision (one nullable column); not a general property |
| 3 | Enable change-signal processing (15) | CRITICAL incidents that hold governed tools closed, once a rescan has produced signals | **Partial.** Back to 0 stops new incidents; the ones already opened need a person |
| 4 | Enable context rebuild (30) | Regenerated tools, descriptions and product versions drafted into review queues | Back to 0 stops new drafts; filed drafts wait for review |
| 5 | Enable freshness evaluation (60) | CRITICAL freshness incidents for unobserved tables, holding their tools closed | **Partial**, as (3). Approve watermark expectations first |
| 6 | Enable classification propagation (1440) | Proposals filed for review; nothing applied without a reviewer | Clean |
| 7 | Enable a task agent interval | Inert until an APPROVED AGENT-kind version governs that principal; then bounded T2 proposals | Clean |
| 8 | Enable or shorten a `ScanPolicy` (one datasource at a time) | Real discovery runs against the source; with (3) on, real incidents | Policy: clean. Queued runs still run; discovered metadata stays |
| 9 | Flip one workspace SHADOW → ENFORCE | Requests previously logged as would-be denials are now denied | Clean — back to SHADOW |
| 10 | `AIDA_UNRESOLVED_WORKSPACE_POSTURE=DENY` | A request whose workspace cannot be resolved is refused | Clean, unless (11) is already set |
| 11 | `AIDA_WORKSPACE_AUTHORIZATION_POSTURE=ENFORCING` | Makes 9 and 10 permanently checked: the API refuses to start if either regresses | Clean in itself; a regression afterwards is a non-starting API |
| 12 | `AIDA_DELIVERY_WORKER_ENABLED=true`, **after** §7 step 1 | The nine queued intents are attempted. Unreachable destination ⇒ all nine `DEAD_LETTER` | **The dead-lettering is not reversible.** The switch is |
| 13 | `AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED=true` | Governance events start enqueuing intents | Clean |

## 9. What this runbook does not do

* It does not set any of these values. `.env.example` and
  `infra/k8s/base/configmap.yaml` carry them as commented entries with their
  intended production value; the live `.env` is untouched, and every default in
  `atlas.platform.config` is unchanged.
* It does not make the parity check a blocking CI gate against a deployment.
  CI has no deployment to reach, and a gate that fails for that reason gets
  switched off — see `.github/workflows/ci.yml`.
* It does not verify a real destination for section
  [7](#7-notification-delivery). Nothing automated can;
  [12 Notification delivery §1](12-notification-delivery-runbook.md#1-what-is-proven-and-what-is-not)
  is explicit about which rows are live-infrastructure-only and why.
