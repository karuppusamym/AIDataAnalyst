# Scraping and alerting on the metrics Atlas already publishes

**Tracker: R11-FP17.** The row's remaining work said "no deployment scrapes these
gauges yet, so the alert thresholds are unset". This directory is the scrape
configuration and the alert rules.

**Updated 2026-09-17.** A Prometheus now runs in the compose stack and reads
these files: it loads all 24 rules without error, scrapes the API, and fires
three alerts that correctly report the rest is unreachable — see "Verified
against a running Prometheus" below. Two things that were true before are still
true and are not fixed by that: the thresholds needing an operator's number are
still unset (deliberately, and labelled as such in every rule that carries one),
and the footprint and projection gauges are still unreachable because
`AIDA_WORKER_METRICS_PORT` is 0 — an operator's decision this repository does
not get to take. So "nothing has ever fired" is no longer accurate; "no alert
has fired on a threshold anyone chose" is.

**Updated 2026-09-21 (R11-AUD03, R11-AUD04).** Three alerts and four series were
added for the two operability signals the tracker still listed as open: the
scheduler's leader election (`AtlasSchedulerNoLeader`,
`AtlasSchedulerLeadershipFlapping`) and the newly-created-table drafter's Kafka
consumer (`AtlasNewlyCreatedTableDrafterConsumerDown`). The drafter's series are
published by the Temporal worker, which **had no metrics listener and no series
at all before this** -- so it now calls `serve_worker_metrics`, has a scrape job
(`atlas-metadata-worker`) and a PodMonitor, and `compose.yaml` passes it
`AIDA_WORKER_METRICS_PORT`. Nothing here was scraped by a running Prometheus;
"What was verified, and what was not" ends with exactly what was checked.

## The finding that came out of writing this

There were 19 Prometheus series across four modules before this change, and
**most of them were published into processes with no way to reach them.**
`prometheus_client` keeps one registry per process, and only `aida.main` — the
`api` service — serves `/metrics` over HTTP:

| Series | Published by | Runs in | Was it reachable? |
|---|---|---|---|
| `aida_http_*` (2) | `aida.main` middleware | `api` | yes |
| `aida_retrieval_*` (8) | `aida.retrieval_metrics` | `api` (request path) | yes |
| `aida_footprint_*` (3) | `aida.footprint_metrics` | **`fleet-scheduler`** | **no** |
| `aida_graph_projection_*` (10) | `aida.projection_metrics` | **`graph-projector`** | **no** |
| `aida_scheduler_*` (2, added 2026-09-21) | `aida.scheduler_leadership` | **`fleet-scheduler`** | **no**, until the port is set |
| `aida_newly_created_table_drafter_*` (2, added 2026-09-21) | `aida.newly_created_table_drafter` | **`metadata-worker`** | **no**, until the port is set |

So "no deployment scrapes these gauges" was not only a missing scrape config.
Thirteen of the nineteen series had nothing at the other end of a scrape at all.
`src/aida/worker_metrics.py` is the other end: one helper, called at the top of
`run_scheduler()` and `run_projector()`, that starts `prometheus_client`'s own
exporter when `worker_metrics_port` is set. It defaults to 0 (listen on nothing),
because opening a port changes a deployment's network surface and belongs with
whoever configures the scrape — and it never takes the process down if the bind
fails, because a crash-looping scheduler is a worse outcome than an unscrapeable
one. `AtlasTargetDown` reports the dead target.

`outbox-publisher` publishes no Prometheus series at all today, so it has no job
here. Adding one would create a permanently-down target that means nothing.
`metadata-worker` was in the same position until R11-AUD03: the drafter
consumer's two series are the first it has published, and it got a listener, a
job and a PodMonitor in the same change -- which is what this paragraph said to
do, and `tests/test_monitoring_rules.py` now checks instead of trusting.

## What is in here

```
infra/monitoring/
  README.md                              # this file
  prometheus/
    prometheus.yml                       # plain-Prometheus scrape config, for the compose stack
    rules/atlas.rules.yml                # 4 recording rules, 25 alerts -- THE SOURCE OF TRUTH
  k8s/
    kustomization.yaml                   # kustomize base (needs Prometheus Operator CRDs)
    servicemonitor.yaml                  # scrapes the existing aida-api Service
    podmonitor.yaml                      # pre-wired for the three worker Deployments that do not exist yet
    prometheusrule.yaml                  # GENERATED from atlas.rules.yml
```

`prometheusrule.yaml` is rendered by `scripts/generate_prometheus_rule.py` and
checked by `tests/test_monitoring_rules.py`. Edit `atlas.rules.yml` and
regenerate; never edit the generated copy. Two hand-maintained copies of
twenty-five alerts drift, and the drift is silent in the worst way — the alert fires in one
environment and not the other, and nobody finds out until the incident it was
written for.

## Wiring it up

### Compose (local and dev stacks)

**Both halves of this are now in `compose.yaml`** — this section used to say the
file "was not edited here" and give the YAML to add. That is done, so what
follows is what an operator still has to *decide*, not what they have to write.

1. **The Prometheus service exists**, profile-gated so it stays out of the
   default `up`: `profiles: ["monitoring", "full"]`, `prom/prometheus:v3.5.0`,
   this directory's `prometheus.yml` and `rules/` mounted read-only, port 9090
   published, and a `prometheus-data` volume with 15-day local retention. Start
   it with:

   ```
   docker compose --profile monitoring up -d prometheus
   ```

2. **`AIDA_WORKER_METRICS_PORT` is plumbed per-service but defaults to 0.**
   `compose.yaml` passes `${AIDA_WORKER_METRICS_PORT:-0}` to `fleet-scheduler`,
   `graph-projector` and (since 2026-09-21) `metadata-worker` individually (not
   through the shared `*app-environment` anchor, which would also set it on the
   API, where it is unused and misleading). 0 means "listen on nothing", and it
   is the default because opening a port changes the deployment's network
   surface and is the operator's decision, not this file's. Compose publishes no
   host port for any of the three: the listener is reachable only from the
   compose network, which is where Prometheus is.

   **The one action still outstanding**, and the reason the series in the
   scheduler, projector and worker are unreachable: set
   `AIDA_WORKER_METRICS_PORT=9108` in the environment those services read, then
   recreate just those containers:

   ```
   AIDA_WORKER_METRICS_PORT=9108 docker compose up -d fleet-scheduler graph-projector metadata-worker
   ```

   Anywhere that variable reaches the worker's environment through a shared env
   file or ConfigMap, the worker now listens too; before 2026-09-21 it read the
   setting and ignored it.

   Until then `atlas-fleet-scheduler`, `atlas-graph-projector` and
   `atlas-metadata-worker` report DOWN, `AtlasTargetDown` fires for all three,
   `AtlasFootprintMetricsAbsent` fires, and
   every other footprint and projection rule evaluates against an empty vector.
   That is the honest reading and not a defect: the scheduler logs
   `worker_metrics_disabled` with "this process publishes no scrapeable
   endpoint" on every start, and `aida_footprint_gaps` is unreachable **by
   design** rather than missing. `prometheus_client` keeps one registry per
   process and only `aida.main` serves `/metrics`; no scrape configuration can
   reach a gauge in a process that is not listening.

### Kubernetes

```
kubectl apply -k infra/monitoring/k8s
```

Three things to know before that command does anything useful:

- **It needs the Prometheus Operator CRDs.** `monitoring.coreos.com/v1` is not
  core Kubernetes. Without kube-prometheus-stack (or another operator that
  installs those CRDs) the apply fails with "no matches for kind". That is why
  this is a separate kustomize base from `infra/k8s/base` — folding it in would
  make a cluster without the operator unable to apply the API Deployment either.
- **Set the `release:` label** on all three resources to whatever the estate's
  Prometheus `serviceMonitorSelector` / `ruleSelector` matches, or drop it if
  that Prometheus selects everything. The files ship
  `REPLACE_ME_PROMETHEUS_RELEASE_LABEL`.
- **`podmonitor.yaml` selects pods that do not exist.** `infra/k8s/base/README.md`
  records that four of the six application processes have no manifest, and three
  of those four are the ones that publish the footprint, projection,
  leader-election and drafter-consumer series. The PodMonitors are committed
  pre-wired so that whoever writes those Deployments does not have to rediscover
  the metrics-listener problem; applied today they reconcile to zero targets.
  Each needs a Deployment carrying the matching `app.kubernetes.io/name` label
  and a container port named `metrics`, plus `AIDA_WORKER_METRICS_PORT` in its
  environment. A scheduler Deployment with more than one replica is what the
  leader-election series are for, and every replica has to be a scraped target
  (a PodMonitor selects pods, so it does); `sum(aida_scheduler_is_leader)` is
  the number of leaders only over all of them.

## Thresholds: what is measured, and what is still an operator's number

Every alert carries a `threshold_status` label. **Structural** means the
comparison needed no magnitude anybody had to choose — a target that is down, a
queue whose oldest entry keeps ageing, a projector holding a backlog while
writing nothing, a provider that reported no usage at all. **Placeholder** means
the number in the expression is a documented starting point, not a measurement,
and a firing instance says so in its own labels so the person paged can tell "the
estate broke its objective" from "nobody has set this yet".

No load test of this platform has ever been run against a real deployment, so
**no latency or backlog magnitude in these rules is a measurement.** Nine of the
twenty-five alerts are placeholders:

| Alert | Placeholder | How to replace it |
|---|---|---|
| `AtlasChangeSignalQueueStale` | `3600s` | A fraction (a third is a common start) of the estate's freshness commitment for catalog metadata: how stale may a table's shape be before an answer built on it is wrong? This is the threshold `src/aida/readiness.py` explicitly declines to own — see below. |
| `AtlasProjectionLagHigh` | `900s` | How out of date the graph explorer and unified lineage may be before an answer drawn from them misleads. Set below that. |
| `AtlasProjectionBacklogAgeHigh` | `1800s` | A multiple of the observed steady-state oldest-backlog age. `projection_metrics.py` calls this gauge "the signal a tenant-budget policy should be chosen from", which is an admission the number is not yet chosen. |
| `AtlasRetrievalLatencyHigh` | `5s` p95 | The steady-state `aida_retrieval_duration_seconds` p95 an estate observes under its own load. **Not** what `scripts/scale_harness/fp17_change_burst_latency.py` reports, which this row used to claim: that harness times `footprint_gaps` and `list_tables` against a change burst and never touches the retrieval path, so its baseline is not this input. It was run for the first time on 2026-09-17 (see that script's docstring), and it still leaves this number unset. |
| `AtlasHttpServerErrorRateHigh` | `0.05` | The complement of the estate's availability objective. There is no agreed one for this platform. |
| `AtlasHttpLatencyHigh` | `3s` p95 | Same as the retrieval ceiling, over the whole API surface — or split per route template once the estate knows which routes are interactive and which are reports. |
| `AtlasSchedulerLeadershipFlapping` | `3` involuntary losses in `1h` | A number above the estate's own steady-state rate of `lost` transitions (`increase(aida_scheduler_leadership_transitions_total{transition="lost"}[1h])` on a healthy week). No failover drill has been run and nobody has measured how often a healthy leader's lock connection fails, so three is where a second loss from the same incident stops being the explanation, not an observation. |
| `AtlasSchedulerPassFailing` | `3` failures of one pass in `15m` | Above the estate's own steady-state failure rate per pass (`increase(aida_scheduler_pass_failures_total[15m])` on a healthy week). The scheduler backs a failing pass off (30 s doubling to 240 s), so three in fifteen minutes is a pass failing on nearly every attempt; nobody has measured how often a healthy pass fails once. |
| `AtlasNewlyCreatedTableDrafterRestarting` | `5` consumer starts in `30m` | Above the estate's own rate of `aida_newly_created_table_drafter_starts_total` on a healthy week, where a broker restart or a rebalance costs one or two. A message that kills every consumer restarts it about two dozen times in thirty minutes on the supervisor's 60 s cap, so five sits between the two; neither end has been measured. |

The other sixteen are structural and can be trusted as shipped:

| Group | Alerts |
|---|---|
| `atlas.scrape-health` | `AtlasTargetDown`, `AtlasFootprintMetricsAbsent`, `AtlasFootprintSweepReadNoOrganizations` |
| `atlas.footprint` | `AtlasLineageParseBacklogNotDraining`, `AtlasLineageReviewBacklogNotDraining`, `AtlasSourceChangeHoldsRising`, `AtlasChangeSignalQueueHeadNotMoving`, `AtlasQuarantinedCodeRising` |
| `atlas.projection` | `AtlasProjectionStalled`, `AtlasProjectionBacklogGrowing` |
| `atlas.retrieval` | `AtlasRetrievalChannelProviderUnavailable` |
| `atlas.cost-and-quota` | `AtlasModelSpendEntirelyEstimated`, `AtlasUsageQuotaRefusals`, `AtlasParserFailures` |
| `atlas.scheduler` | `AtlasSchedulerNoLeader` (the placeholder above is its group-mate) |
| `atlas.drafter` | `AtlasNewlyCreatedTableDrafterConsumerDown` |

Two of the three added on 2026-09-21 need a sentence each, because their `for`
is a decision and it is easy to change one without the other:

* **`AtlasSchedulerNoLeader` waits five minutes** because a failover with the
  keepalive settings `Docs/40-engineering/07-local-runbook.md` (section 9c)
  recommends takes about two minutes (60 s idle, then 6 probes 10 s apart, plus
  the standby's 5 s retry). It fires on the operating system's defaults, where a
  vanished leader holds the lock for about 2 h 11 min, and stays firing until
  PostgreSQL lets go. `tests/test_monitoring_rules.py` reads the runbook's numbers
  and fails if the window stops outlasting them. It does **not** fire where no
  replica reports the gauge (an empty `sum`), on purpose: that is
  `AtlasTargetDown`'s reading, and claiming "no leader" about a scheduler that
  never opened its port would be a guess.
* **`AtlasNewlyCreatedTableDrafterConsumerDown` waits fifteen minutes** and
  **fires on the default stack by design**: `auto_enqueue_on_ingest` defaults to
  true, the default stack has no broker, and the gauge is 0. The annotation says
  so where the person paged reads it. It can only fire where the worker
  publishes the gauge -- the series does not exist until the drafter supervisor
  starts -- so an estate that turned the feature off, or never opened the port,
  is silent. What it cannot see is a consumer killed by the same message over and
  over: the gauge is 1 for the moment each attempt starts, so a scrape can land
  on it and restart the alert's clock. `AtlasNewlyCreatedTableDrafterRestarting`
  (added later on 2026-09-21, a placeholder above) reads
  `aida_newly_created_table_drafter_starts_total` for that loop instead: it counts
  only the starts that joined the group, so a missing broker leaves it flat and
  the two alerts never fire for the same cause. The failures counter cannot make
  that distinction; it rises once a minute in both.

There is deliberately no alert for **more than one leader**. The gauge shows it
(`sum(aida_scheduler_is_leader) > 1`), but a deposed leader keeps reading 1
until the pass it is running ends, and nobody has measured how long that is, so a
fixed window would either page on a normal handover or wait out a real fault.
A leader count that stays above one is the sign of a lock that excludes nobody,
most likely a transaction-mode pooler in front of PostgreSQL.

Most of the footprint and projection alerts use `deriv(...) > 0` over a window
rather than a magnitude comparison. That is not a trick to avoid choosing a
number: a backlog that is large and falling is a queue doing its job, and a
backlog that is small and growing is the leading indicator. "Bigger than it was"
needs no service objective, and it is the question an operator actually has.

### Two facts the rules respect rather than work around

**`aida_footprint_gaps` has no tenant label, by design, so no alert here can be
per-tenant.** `src/aida/footprint_metrics.py` exports gap totals by `kind` only
and sends the per-organization figures to structlog, because an organization or
datasource id is unbounded metric cardinality (review F17) and would put a tenant
identifier on a scrapeable surface. The consequence is concrete: a fleet total
that stays flat while one tenant's backlog grows will not fire. Making it
per-tenant means reversing that labelling decision in `footprint_metrics.py`,
which is a change to the finding it implements — not something an alert file gets
to do quietly. Until then the per-tenant answer is the
`footprint_metrics_organization` structlog line and
`GET /v1/organizations/{id}/footprint-gaps`. The same rule governs
`aida_usage_quota_decisions_total`: it says a quota refused, never whose.

**The queue-age threshold belongs in these rules, and that is the design intent.**
`src/aida/readiness.py` says it twice, for both backlog probes: the probe "does
not own the threshold at which a backlog is an incident. It reports the two
numbers; alerting decides." `AtlasChangeSignalQueueStale` is where that decision
lives. It is a placeholder because the decision has not been made, not because
the probe forgot to make it.

## What was verified, and what was not

Verified locally:

```
kubectl kustomize infra/monitoring/k8s          # renders cleanly, exit 0, 4 resources
python -c "import yaml; yaml.safe_load(...)"    # every file here parses as YAML
python scripts/generate_prometheus_rule.py --check
pytest tests/test_monitoring_rules.py
```

`tests/test_monitoring_rules.py` is the substantive one: it checks that every
metric name referenced by a rule is actually published somewhere under `src/`
(an alert on a metric nobody emits is silence that looks like health), that
every label matcher names a label that metric declares (`{kimd="..."}` parses,
loads and never fires), that every alert declares a `threshold_status` from the
closed set plus a severity, summary and runbook, that every placeholder rule
explains itself in its annotation, and that the generated PrometheusRule matches
the source file.

**That file was named here before it existed.** It was written 2026-09-17, to
this description. Until then the four properties above were claimed and not
checked, and the drift the generator exists to prevent had nothing watching it.
`--check` happened to be passing when the test was added, so nothing had drifted
— but that was luck, not a guard.

### Verified against a running Prometheus, 2026-09-17

`promtool` **is** available: it ships inside the `prom/prometheus` image, so it
needs no local install. Run against the mounted copies, in the container the
compose stack starts:

```
docker exec aida-platform-prometheus-1 promtool check config /etc/prometheus/prometheus.yml
  Checking /etc/prometheus/prometheus.yml
    SUCCESS: 1 rule files found
   SUCCESS: /etc/prometheus/prometheus.yml is valid prometheus config file syntax
  Checking /etc/prometheus/rules/atlas.rules.yml
    SUCCESS: 24 rules found
```

What that Prometheus (v3.5.0) reports about itself:

- `/api/v1/status/runtimeinfo` — `reloadConfigSuccess: true`, `corruptionCount: 0`.
- `/api/v1/rules` — all 7 groups and all 24 rules loaded, every one
  `health: "ok"` with an empty `lastError`. So the PromQL is not merely
  YAML-valid: Prometheus has parsed and is evaluating every expression.
- `/api/v1/targets` — `atlas-api` (`http://api:8000/metrics`) **up**;
  `atlas-fleet-scheduler` and `atlas-graph-projector` (`:9108`) **down**,
  `connection refused`, because `AIDA_WORKER_METRICS_PORT` is 0. See "Wiring it
  up" above.
- `/api/v1/alerts` — **three alerts firing**, and all three are correct:
  `AtlasTargetDown` twice (one per unreachable worker) and
  `AtlasFootprintMetricsAbsent`. The rules' first real-world job was to report
  that two thirds of the series are unreachable, and they did.
- Series actually present: `aida_http_*` and `aida_retrieval_*`, plus
  `aida_usage_quota_decisions_total`, and the two HTTP/retrieval p95 recording
  rules. `aida_footprint_*` and `aida_graph_projection_*` are absent, as the
  table at the top of this file predicts.

**Still not verified, and not claimed:**

- **No alert has fired on a real threshold.** The three firing alerts are all
  `threshold_status: structural` and all say "this is not being scraped". No
  placeholder threshold has been exercised, because nothing has driven this
  deployment past one.
- **`aida_parser_*` and `aida_model_*` are only partly reachable.** They are
  declared in the API's registry, so `atlas-api` exposes their `# HELP` lines,
  but they are incremented wherever the work happens: `sql_lineage_parser` and
  `lineage_agent` run in the fleet scheduler, and `model_gateway` runs in
  several processes. `AtlasParserFailures` and `AtlasModelSpendEntirelyEstimated`
  therefore see only the share of that activity that happens inside
  `aida.main`. This is the same per-process-registry cause as the footprint
  gauges, and it is not fixed by the same setting: the metadata worker had no
  metrics listener and no scrape job at all. *(Since 2026-09-21 it has both, off
  by default, so a parser or model call made inside the worker is visible to a
  scrape once `AIDA_WORKER_METRICS_PORT` is set there.)*
- **No cluster.** `kubectl kustomize` renders manifests without an API server;
  it does not validate them against real resource schemas. `kubeconform` is not
  installed here either. `kubectl apply --dry-run=server` against a cluster with
  the operator CRDs installed is the real next check.
- **Nothing was ever scraped.** No Prometheus was started, no target came up, no
  rule was evaluated and no alert has ever fired. The scrape jobs, the port, the
  listener and the rules are all reviewable and none of them is proven.
- **No alert threshold rests on a measurement of this platform.** See the table
  above. `scripts/scale_harness/fp17_change_burst_latency.py` exists to produce
  the latency inputs and **has not been run** — it needs a live stack, and this
  change started nothing.

### Added 2026-09-21 (R11-AUD03, R11-AUD04): what was checked, and what was not

Checked, against a working tree with no running stack:

- `python scripts/generate_prometheus_rule.py --check` is current, and the rule
  file has 4 recording rules and 25 alerts (`tests/test_monitoring_rules.py`,
  13 tests).
- The series exist under the names the rules read. A real HTTP scrape of
  `prometheus_client`'s exporter on an ephemeral loopback port, in a throwaway
  script, returned `aida_scheduler_is_leader`,
  `aida_scheduler_leadership_transitions_total{transition="acquired"|"lost"}`
  (both label values at `0.0` before any event) and
  `aida_newly_created_table_drafter_failures_total`; and returned
  `aida_newly_created_table_drafter_consumer_up{consumer_group=...}` **only after
  the supervisor had run** -- before it, a `# TYPE` line and no sample, which is
  what keeps the drafter alert silent where the feature is off.
- Behaviour, with a fake lock and a stub consumer:
  `tests/test_scheduler_leadership.py` (45 passed, 1 skipped -- the real
  PostgreSQL test, which needs a scratch database) and
  `tests/test_newly_created_table_drafter_supervisor.py` (28 passed). Each
  behaviour added was mutation-checked: with the line undone, the matching test
  fails.
- Every process that calls `serve_worker_metrics` has a scrape job, a PodMonitor
  and, in compose, the port variable (`tests/test_monitoring_rules.py`).

**Not checked, and not claimed:**

- **No Prometheus evaluated any of these three rules.** `promtool` is not on this
  machine and no PromQL parser is installed, so the expressions have been read,
  not parsed. They are short, but a syntax or semantics error would show only
  when Prometheus loads the file.
- **`aida_scheduler_is_leader` has never been scraped from a real scheduler, and
  no failover has ever been run.** The `for` of `AtlasSchedulerNoLeader` is
  derived from the keepalive numbers in the runbook; neither has been measured.
- **The worker's listener has never been started against a real Prometheus**, and
  `compose.yaml` was not brought up. `serve_worker_metrics` is unchanged; only its
  third caller is new.
- **Recovery against a real Redpanda coming up** is still stub-only.
- **A consumer killed by the same message repeatedly** is not reliably caught by
  the gauge alert (see its annotation). *Later the same day:*
  `AtlasNewlyCreatedTableDrafterRestarting` reads the new
  `aida_newly_created_table_drafter_starts_total` for it, checked against the stub
  consumer only (`tests/test_newly_created_table_drafter_supervisor.py`); no real
  poison message has been sent through a real broker.
