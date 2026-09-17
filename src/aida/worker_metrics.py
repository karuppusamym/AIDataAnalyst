"""R11-FP17: let a scrape reach the metrics a background process already publishes.

The gauges were the easy half. `aida.footprint_metrics` publishes
`aida_footprint_gaps`, `aida_footprint_oldest_pending_change_signal_seconds`
and `aida_footprint_metrics_organizations`; `aida.projection_metrics` publishes
ten graph-projection series. Both were written so an alert could be pointed at
them, and neither could be scraped by anything, for a reason nothing in the
tracker row said out loud: **`prometheus_client`'s registry is per process, and
only `aida.main` serves `/metrics` over HTTP.**

`run_footprint_metrics_pass` runs in the fleet scheduler
(`aida.workflows.scheduler`). The projection series are set in the graph
projector (`aida.projectors.graph_projector`). Both are long-lived asyncio loops
with no HTTP server of any kind. Every value they set landed in a registry
inside a process with no way in -- so "no deployment scrapes these gauges yet"
was not only a missing scrape config. There was nothing at the other end of it.

This module is the other end. One helper, called once at the top of each of
those two processes' entry points, which starts `prometheus_client`'s own WSGI
exporter on a thread and returns the port it bound.

**Off unless a port is set.** `worker_metrics_port` defaults to 0, which means
"do not listen". A process that opens a port nobody asked for is a change to a
deployment's network surface, and this one should be a deliberate act by whoever
also configures the scrape. With the default, this function does nothing and
returns `None` -- and the scrape jobs in `infra/monitoring/` will report those
targets as down, which is the honest reading rather than a silent success.

**A failed listener never takes the process down.** The scheduler's job is to
admit runs; the projector's job is to project events. Neither should die because
a monitoring port was already in use. A bind failure is logged at error and
swallowed, and the resulting dead target is exactly what `AtlasTargetDown`
exists to report. The alternative -- crash-looping a scheduler over a metrics
port -- converts an observability gap into an outage.

**What it does not do.** It adds no metric and changes no metric. It publishes
nothing about the process itself beyond what `prometheus_client` already
registers by default (the process and platform collectors). Every series a
scrape finds here was published by the module that owns it, with the labels that
module chose -- including `footprint_metrics`' deliberate refusal to put a
tenant identifier in one.
"""

from __future__ import annotations

import structlog
from prometheus_client import start_http_server

from aida.config import Settings

_log = structlog.get_logger(__name__)

#: Bound once per process. A second call is a no-op returning the same port
#: rather than a second listener: the entry points below are the only callers
#: today, but a future one calling this twice should not get an OSError from
#: `start_http_server` for a port that is already serving the right registry.
_bound_port: int | None = None

#: The address the exporter binds. All interfaces, on purpose and only when an
#: operator has set a port: a listener bound to 127.0.0.1 inside a container is
#: unreachable from the Prometheus pod that is the entire reason it exists. The
#: endpoint carries no tenant-scoped data by construction -- `footprint_metrics`
#: and `projection_metrics` both keep organization identifiers out of every
#: label, and `tests/test_inv5_tenant_isolation.py` records `GET /metrics` among
#: the routes serving no tenant-scoped data -- but it is still an internal
#: surface, and keeping it off the ingress is the deployment's job, not this
#: module's.
_BIND_HOST = "0.0.0.0"  # noqa: S104 -- see the comment above; opt-in via a port


def serve_worker_metrics(settings: Settings, *, process: str) -> int | None:
    """Expose this process's Prometheus registry over HTTP, if configured.

    Returns the bound port, or `None` when `worker_metrics_port` is 0 (the
    default) or the bind failed. `process` names the caller for the log line
    only -- it is never a metric label, because the scrape's own `job` already
    carries it and a second copy inside the exposition would be one more place
    for the two to disagree.
    """
    global _bound_port
    port = settings.worker_metrics_port
    if port == 0:
        _log.info(
            "worker_metrics_disabled",
            process=process,
            detail="worker_metrics_port is 0; this process publishes no scrapeable endpoint",
        )
        return None
    if _bound_port is not None:
        return _bound_port
    try:
        start_http_server(port, addr=_BIND_HOST)
    except OSError:
        # Logged, not raised: see the module docstring. The dead target is what
        # `AtlasTargetDown` reports, and a crash-looping scheduler would be a
        # worse outcome than an unscrapeable one.
        _log.exception("worker_metrics_bind_failed", process=process, port=port)
        return None
    _bound_port = port
    _log.info("worker_metrics_serving", process=process, port=port, path="/metrics")
    return port
