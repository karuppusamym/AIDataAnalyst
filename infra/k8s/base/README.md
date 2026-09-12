# These manifests are an example, not a deployment

**Read this before assuming the directory you are standing in can run Atlas.**

This is a **reviewable sketch of one service**. It is *not* a deployable topology, it has
never been applied to a cluster, and applying it as-is would fail — see "Known to be
broken as written" below.

## What it is

Kubernetes manifests for exactly one of the platform's processes — the FastAPI API
(`aida.main`) — plus a one-shot schema-migration Job. Their value is that the
security-relevant configuration is pinned in a reviewed file rather than left to a
deployer's memory (non-root, dropped capabilities, read-only root filesystem, no
hardcoded secrets, no floating image tag, probes wired to real routes). `../README.md`
explains each of those choices.

## What it is not

It is not the topology. `compose.yaml` at the repository root runs **six** application
processes built from this repo's image. This directory covers two of them:

| `compose.yaml` service | Command | Manifest here |
|---|---|---|
| `api` | `uvicorn aida.main:app` | `deployment.yaml` (3 replicas) |
| `migrate` | `alembic upgrade heads` | `migration-job.yaml` (but see below) |
| `metadata-worker` | `python -m aida.workflows.worker` | **none** |
| `fleet-scheduler` | `python -m aida.workflows.scheduler` | **none** |
| `outbox-publisher` | `python -m aida.projectors.outbox_publisher` | **none** |
| `graph-projector` | `python -m aida.projectors.graph_projector` | **none** |
| `ui-next` | the React app's own image | **none** |

Deploy only what is here and the API answers requests while nothing behind it moves:
no metadata ingestion or profiling workflows run (Temporal worker), no scheduled scans
fire (scheduler), no domain event ever leaves the outbox table (publisher), and the Neo4j
knowledge graph is never populated (projector). Lineage, the graph explorer and every
background job silently do nothing rather than visibly fail.

The datastores those processes need — PostgreSQL, Redis, Neo4j, Temporal, Redpanda,
object storage — have no manifests here either. `configmap.yaml` references them by
in-cluster hostname as if they already existed. `compose.yaml` is the closest thing this
repository has to a specification of what each one needs.

There is no `overlays/` directory, no Ingress, no TLS termination, no NetworkPolicy and
no HPA.

## The image digests are placeholders

Both `deployment.yaml` and `migration-job.yaml` carry the literal string:

```
REPLACE_ME_REGISTRY/aida-api@sha256:REPLACE_ME_WITH_REAL_DIGEST
```

That is not a redaction of a real digest — no digest exists. Nothing in CI builds,
pushes or scans this image today, so there is no pipeline output to substitute in. The
`@sha256:` *shape* is deliberate (it makes a floating `:latest` tag impossible to write
here by accident); it is a constraint on a future pipeline, not evidence of one.

## Known to be broken as written

`migration-job.yaml` runs `alembic upgrade head` (singular) while `compose.yaml` runs
`alembic upgrade heads` (plural), and only the plural form is safe here. The revision
graph is 153 revisions with a single head today, so the singular form happens to resolve
right now -- but this repository merges independent Alembic branches routinely (46 of
those revisions are merges), and any moment with two live heads makes `head` abort with
"Multiple head revisions are present" while `heads` keeps working. The Job has never been
run against this schema either way. It is left uncorrected on purpose: fixing it is part
of making these manifests real (tracker AU-9 follow-up), and this note is part of not
claiming they already are.

## Where the numbers are

`Docs/10-architecture/13-connection-pool-and-worker-budgets.md` §4.2 works through what
the missing Deployments would cost in database connections, and deliberately does not
invent replica counts for them.
