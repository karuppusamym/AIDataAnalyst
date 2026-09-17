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

So "no deployment scrapes these gauges" was not only a missing scrape config.
Thirteen of the nineteen series had nothing at the other end of a scrape at all.
`src/aida/worker_metrics.py` is the other end: one helper, called at the top of
`run_scheduler()` and `run_projector()`, that starts `prometheus_client`'s own
exporter when `worker_metrics_port` is set. It defaults to 0 (listen on nothing),
because opening a port changes a deployment's network surface and belongs with
whoever configures the scrape — and it never takes the process down if the bind
fails, because a crash-looping scheduler is a worse outcome than an unscrapeable
one. `AtlasTargetDown` reports the dead target.

`metadata-worker` and `outbox-publisher` publish no Prometheus series at all
today, so they have no job here. Adding one would create a permanently-down
target that means nothing.

## What is in here

```
infra/monitoring/
  README.md                              # this file
  prometheus/
    prometheus.yml                       # plain-Prometheus scrape config, for the compose stack
    rules/atlas.rules.yml                # 4 recording rules, 20 alerts -- THE SOURCE OF TRUTH
  k8s/
    kustomization.yaml                   # kustomize base (needs Prometheus Operator CRDs)
    servicemonitor.yaml                  # scrapes the existing aida-api Service
    podmonitor.yaml                      # pre-wired for the two worker Deployments that do not exist yet
    prometheusrule.yaml                  # GENERATED from atlas.rules.yml
```

`prometheusrule.yaml` is rendered by `scripts/generate_prometheus_rule.py` and
checked by `tests/test_monitoring_rules.py`. Edit `atlas.rules.yml` and
regenerate; never edit the generated copy. Two hand-maintained copies of twenty
alerts drift, and the drift is silent in the worst way — the alert fires in one
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
   `compose.yaml` passes `${AIDA_WORKER_METRICS_PORT:-0}` to `fleet-scheduler`
   and `graph-projector` individually (not through the shared
   `*app-environment` anchor, which would also set it on the API, where it is
   unused and misleading). 0 means "listen on nothing", and it is the default
   because opening a port changes the deployment's network surface and is the
   operator's decision, not this file's.

   **The one action still outstanding**, and the reason 13 of the 19 series are
   unreachable: set `AIDA_WORKER_METRICS_PORT=9108` in the environment those two
   services read, then recreate just those two containers:

   ```
   AIDA_WORKER_METRICS_PORT=9108 docker compose up -d fleet-scheduler graph-projector
   ```

   Until then `atlas-fleet-scheduler` and `atlas-graph-projector` report DOWN,
   `AtlasTargetDown` fires for both, `AtlasFootprintMetricsAbsent` fires, and
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
  records that four of the six application processes have no manifest, and two of
  those four are the ones that publish the footprint and projection series. The
  PodMonitors are committed pre-wired so that whoever writes those Deployments
  does not have to rediscover the metrics-listener problem; applied today they
  reconcile to zero targets. Each needs a Deployment carrying the matching
  `app.kubernetes.io/name` label and a container port named `metrics`, plus
  `AIDA_WORKER_METRICS_PORT` in its environment.

## Thresholds: what is measured, and what is still an operator's number

Every alert carries a `threshold_status` label. **Structural** means the
comparison needed no magnitude anybody had to choose — a target that is down, a
queue whose oldest entry keeps ageing, a projector holding a backlog while
writing nothing, a provider that reported no usage at all. **Placeholder** means
the number in the expression is a documented starting point, not a measurement,
and a firing instance says so in its own labels so the person paged can tell "the
estate broke its objective" from "nobody has set this yet".

No load test of this platform has ever been run against a real deployment, so
**no latency or backlog magnitude in these rules is a measurement.** Six of the
twenty alerts are placeholders:

| Alert | Placeholder | How to replace it |
|---|---|---|
| `AtlasChangeSignalQueueStale` | `3600s` | A fraction (a third is a common start) of the estate's freshness commitment for catalog metadata: how stale may a table's shape be before an answer built on it is wrong? This is the threshold `src/aida/readiness.py` explicitly declines to own — see below. |
| `AtlasProjectionLagHigh` | `900s` | How out of date the graph explorer and unified lineage may be before an answer drawn from them misleads. Set below that. |
| `AtlasProjectionBacklogAgeHigh` | `1800s` | A multiple of the observed steady-state oldest-backlog age. `projection_metrics.py` calls this gauge "the signal a tenant-budget policy should be chosen from", which is an admission the number is not yet chosen. |
| `AtlasRetrievalLatencyHigh` | `5s` p95 | The steady-state `aida_retrieval_duration_seconds` p95 an estate observes under its own load. **Not** what `scripts/scale_harness/fp17_change_burst_latency.py` reports, which this row used to claim: that harness times `footprint_gaps` and `list_tables` against a change burst and never touches the retrieval path, so its baseline is not this input. It was run for the first time on 2026-09-17 (see that script's docstring), and it still leaves this number unset. |
| `AtlasHttpServerErrorRateHigh` | `0.05` | The complement of the estate's availability objective. There is no agreed one for this platform. |
| `AtlasHttpLatencyHigh` | `3s` p95 | Same as the retrieval ceiling, over the whole API surface — or split per route template once the estate knows which routes are interactive and which are reports. |

The other fourteen are structural and can be trusted as shipped:

| Group | Alerts |
|---|---|
| `atlas.scrape-health` | `AtlasTargetDown`, `AtlasFootprintMetricsAbsent`, `AtlasFootprintSweepReadNoOrganizations` |
| `atlas.footprint` | `AtlasLineageParseBacklogNotDraining`, `AtlasLineageReviewBacklogNotDraining`, `AtlasSourceChangeHoldsRising`, `AtlasChangeSignalQueueHeadNotMoving`, `AtlasQuarantinedCodeRising` |
| `atlas.projection` | `AtlasProjectionStalled`, `AtlasProjectionBacklogGrowing` |
| `atlas.retrieval` | `AtlasRetrievalChannelProviderUnavailable` |
| `atlas.cost-and-quota` | `AtlasModelSpendEntirelyEstimated`, `AtlasUsageQuotaRefusals`, `AtlasParserFailures` |

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
  gauges, and it is not fixed by the same setting: the metadata worker has no
  metrics listener and no scrape job at all.
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
