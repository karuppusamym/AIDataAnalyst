# Scraping and alerting on the metrics Atlas already publishes

**Tracker: R11-FP17.** The row's remaining work said "no deployment scrapes these
gauges yet, so the alert thresholds are unset". This directory is the scrape
configuration and the alert rules. It is not a deployment, and the thresholds that
need an operator's number are still unset — deliberately, and labelled as such in
every rule that carries one.

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

`compose.yaml` is owned by another change and **was not edited here**. Two things
have to be added to it, and both are small:

1. **A Prometheus service, profile-gated** so it stays out of the default `up`:

   ```yaml
     prometheus:
       profiles: ["monitoring"]
       image: prom/prometheus:v3.1.0            # pin a digest in any shared environment
       command:
         - --config.file=/etc/prometheus/prometheus.yml
       volumes:
         - ./infra/monitoring/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
         - ./infra/monitoring/prometheus/rules:/etc/prometheus/rules:ro
       ports:
         - "9090:9090"
   ```

   `prometheus.yml`'s `rule_files` already points at `/etc/prometheus/rules/*.rules.yml`,
   which is where the second mount lands.

2. **`AIDA_WORKER_METRICS_PORT=9108` on the `fleet-scheduler` and `graph-projector`
   services.** Without it those two serve nothing and their jobs stay down. The
   shared `*app-environment` anchor would set it for every service including the
   API, which is harmless (the API's own port is untouched and the extra listener
   is unused) but misleading; per-service is clearer.

Then `docker compose --profile monitoring up -d prometheus`.

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
| `AtlasRetrievalLatencyHigh` | `5s` p95 | The steady-state p95 that `scripts/scale_harness/fp17_change_burst_latency.py` reports against a real deployment. That script's baseline percentile is exactly this input. |
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
(an alert on a metric nobody emits is silence that looks like health), that every
alert declares a `threshold_status` from the closed set, that every placeholder
rule explains itself in its annotation, and that the generated PrometheusRule
matches the source file.

**Not verified, and not claimed:**

- **`promtool check rules` was not run.** `promtool` is not installed on this
  machine and installing tooling was out of scope, so the PromQL in these rules
  has been parsed as YAML and reviewed by eye, never by Prometheus itself. This
  is the weakest link in the validation above: a rule whose YAML is valid and
  whose PromQL is malformed loads as a broken rule group. Run
  `promtool check rules infra/monitoring/prometheus/rules/atlas.rules.yml`
  before relying on any of it.
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
