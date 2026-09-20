# aida-api Kubernetes manifests — an example for one service, not a deployment

> **What this is not (R11-X10, 2026-09-11).** This is a reviewable *sketch* of a single
> process. It is not a deployable topology, it has never been applied to a cluster, its
> image digests are placeholders for an image no pipeline publishes, and `migration-job.yaml`
> has never been run (it uses `alembic upgrade head`, which resolves today because CI
> enforces a single head; `heads`, as `compose.yaml` runs it, is the safer form).
> `compose.yaml` runs six application processes; this
> directory covers two of them. `base/README.md` lists exactly what is missing and why it
> matters — read it before treating anything here as production-ready.

Tracker: **AU-9**. Audit: `Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` §4 —
*"No production deployment artifact exists. `infra/` contains four `init.sql` seed files.
No k8s, Helm, Terraform or systemd anywhere."* This directory is the first one.

## What this is

A plain, kustomize-composable set of Kubernetes manifests for the `aida-api` service
(the FastAPI process built by the repo-root `Dockerfile`, `uvicorn aida.main:app`). It is
**reviewable** — every value that the audit's C1/C2 findings care about is pinned in
`base/configmap.yaml`, not left to a deployer to remember:

- `AIDA_ENVIRONMENT=production` (audit C1: this defaults to `"development"` in
  `src/atlas/platform/config.py`. The application side has since been tightened:
  `Settings` uses `extra="forbid"`, `reject_unrecognized_aida_env_vars` refuses an
  `AIDA_*` variable whose name is a near-miss of a real setting, and
  `reject_implicit_environment_outside_tests` refuses to start without an explicit
  `AIDA_ENVIRONMENT`. A name that is not a close match of any real setting is still
  dropped, so pinning the *correct* name in a reviewed file remains worthwhile).
- `AIDA_IDENTITY_PROVIDER=oidc` (audit C2: the default `"development"` identity provider
  trusts an unauthenticated `X-Roles` header; `oidc` plus the required issuer/audience/JWKS
  settings are the only way `Settings.reject_insecure_production_configuration` allows
  `environment=production` to pass at all).
- `runAsNonRoot`, non-root `runAsUser: 10001` at both pod and container level, matching
  the `Dockerfile`'s existing `USER aida` (uid/gid 10001) — **the Dockerfile already ran as
  non-root**; no image fix was needed here.
- CPU/memory requests and limits inline in `deployment.yaml`, commented as tunable
  defaults, not measured (see "What's honestly still missing" below).
- No hardcoded secret values anywhere: `base/secret.example.yaml` is a *template*
  documenting the required keys, deliberately excluded from `base/kustomization.yaml` so
  it can never be `kubectl apply -k`'d by mistake with placeholder data.
- No floating tag: `image:` is `REPLACE_ME_REGISTRY/aida-api@sha256:REPLACE_ME...` — the
  manifest's *shape* forbids `:latest`, matching the audit's specific call-out of
  `compose.yaml`'s floating-tag anti-pattern.
- `readinessProbe`/`livenessProbe` wired to the existing `/health/ready` and
  `/health/live` routes (`src/aida/main.py`).

## Layout

```
infra/k8s/
  base/
    README.md                 # what these manifests are NOT — read first
    namespace.yaml            # the `aida` namespace
    serviceaccount.yaml        # dedicated SA, no API token mounted (least privilege)
    configmap.yaml              # non-secret config incl. AIDA_ENVIRONMENT / AIDA_IDENTITY_PROVIDER
    secret.example.yaml         # TEMPLATE — not applied by kustomization.yaml, see below
    deployment.yaml              # 3 replicas, non-root, resource limits, digest-pinned image
    service.yaml                  # ClusterIP :80 -> :8000
    poddisruptionbudget.yaml       # minAvailable: 1
    migration-job.yaml              # alembic upgrade head, run before/alongside rollout
    kustomization.yaml               # ties the above together (excl. the secret template)
  README.md                          # this file
```

There is no `overlays/` directory yet (e.g. per-environment digest/replica tweaks via
kustomize patches) — one environment's worth of manifests is the honest scope of this
change. Add overlays when a second real environment exists to diverge from.

## Validating this without a real cluster

This sandbox has no cluster to deploy to — said plainly, per the task's own instruction.
What *was* verified locally:

```
kubectl kustomize infra/k8s/base                                    # renders cleanly, no errors
kubeconform -strict -summary <(kubectl kustomize infra/k8s/base)     # 7/7 resources valid
                                                                       # against real k8s v1.30
                                                                       # OpenAPI schemas
kubeconform -strict -summary infra/k8s/base/secret.example.yaml      # 1/1 valid (template only)
```

`kubectl apply --dry-run=client` was attempted but requires live API-server discovery even
in client mode on this kubectl version (it calls out to `localhost:8080` for the resource
mapping cache) — there's no cluster in this sandbox to provide that, so `kubeconform`
(schema validation against the real, versioned k8s OpenAPI spec, no cluster required) was
used instead and is the stronger check of the two for catching malformed/misplaced fields.
Neither tool is a substitute for `kubectl apply --dry-run=server` against a real cluster,
which is the next real validation step once one exists (e.g. in a CI job with `kind`).

## What a deployer must supply

1. **`aida-api-secrets` Secret** — every key listed in `base/secret.example.yaml`, populated
   with real values (`kubectl create secret generic aida-api-secrets --from-literal=...`,
   or preferably a secrets operator — External Secrets Operator, Sealed Secrets, Vault Agent
   Injector — reading from your actual secret store). Never fill in real values in that file
   and commit it.
2. **A pinned image digest.** Replace `REPLACE_ME_REGISTRY/aida-api@sha256:REPLACE_ME...`
   in both `deployment.yaml` and `migration-job.yaml` with the real digest your CI/CD
   pipeline produced. **That publishing step does not exist yet.** CI now builds the image
   (`docker-build` job) and scans dependencies and secrets (audit remediation item #12 as
   worded), but nothing pushes the image, records its digest or scans the image itself.
   Until it lands, the intended flow is: CI builds the image from this repo's
   `Dockerfile`, pushes it, records the resulting `sha256` digest, and a deploy step (or a
   kustomize `images:` patch in a future overlay) substitutes it in — never a person typing
   a tag by hand, and never `:latest`.
3. **OIDC values** — `AIDA_OIDC_ISSUER`, `AIDA_OIDC_AUDIENCE`, `AIDA_OIDC_JWKS_URL` in
   `configmap.yaml`, plus real `AIDA_OIDC_ROLE_MAPPINGS` / `AIDA_OIDC_PERSONA_MAPPINGS` JSON
   (empty by default, which fails safe — no roles granted — rather than falling open the way
   the dev header path does).
4. **Ingress and TLS termination — out of scope, not included.** This ships a `ClusterIP`
   Service only. Fronting it with an Ingress controller / gateway, terminating TLS, and any
   WAF/rate-limiting at the edge is left to the deployer's existing platform conventions,
   which this repo has no visibility into.
5. **The datastores themselves** (PostgreSQL, Redis, Temporal, Neo4j, Redpanda, object
   storage) are referenced by hostname in `configmap.yaml` as if they ran in-cluster under
   the `aida` namespace — this directory does **not** include manifests for them (that would
   be its own tracker item; `compose.yaml`'s service definitions are the closest thing to a
   spec for what each one needs, but a production Postgres/Kafka/Neo4j deployment is
   normally an operator-managed StatefulSet or a managed cloud service, not something to
   hand-roll here).

## What's honestly still missing

- **Four of the six application processes have no manifest at all** — `metadata-worker`
  (`python -m aida.workflows.worker`), `fleet-scheduler`
  (`python -m aida.workflows.scheduler`), `outbox-publisher`
  (`python -m aida.projectors.outbox_publisher`) and `graph-projector`
  (`python -m aida.projectors.graph_projector`), all of which `compose.yaml` runs. So does
  `ui-next`. Apply this directory and the API serves requests while no workflow runs, no
  scan is scheduled, no outbox row is ever published and the Neo4j graph is never built —
  a silent no-op, not a visible failure. `base/README.md` has the full comparison.
- **`migration-job.yaml` uses the fragile form.** It runs `alembic upgrade head`
  (singular) where the `migrate` service in `compose.yaml` runs `heads` (plural). The graph
  is 192 revisions with a single head as of 2026-09-20, and the CI `migrations` job fails on
  any second head, so the singular form resolves today — but this repository
  merges independent Alembic branches routinely (46 of those revisions are merges), and
  any moment with two live heads makes `head` abort with "Multiple head revisions are
  present" while `heads` keeps working. This Job has never been run against this schema,
  and it stays unapplied while its image digest and Secret are placeholders.
- ~~**No non-`env` `SecretProvider` is implemented.**~~ **Stale as of 2026-08-31; corrected
  2026-09-11 (R11-X10).** This entry said only the `Protocol` and caching existed in
  `src/aida/secrets.py`. AU-10 closed the same day this README landed:
  `VaultKvSecretProvider` reads HashiCorp Vault's KV v2 engine, and `SecretResolver` builds
  it when `AIDA_CREDENTIAL_PROVIDER=vault` is configured **and** `AIDA_SECRETS_VAULT_URL`
  and `AIDA_SECRETS_VAULT_TOKEN` are set. Nothing under `infra/` supplies those two, so
  `configmap.yaml` setting `vault` passes startup validation but resolves nothing until a
  deployer adds them (the token is a bootstrap credential, so not for the ConfigMap).
  Still true: no CyberArk / AWS Secrets Manager / Azure Key Vault / GCP Secret Manager
  provider exists (the setting accepts those names, but none is implemented), so a
  deployer whose secret store is not Vault has nothing to point `vault` at.
- **Resource requests/limits are defaults, not measurements.** No load test or profiling
  run exists for this codebase yet (audit §5). Treat the numbers in `deployment.yaml` as a
  reasonable starting point to watch in staging and revise, not a capacity-planning result.
- ~~**The Temporal-outage readiness coupling (audit remediation #11)** is unfixed in
  application code — a Temporal outage can still take down `/health/ready` for reasons the
  probe wiring in this manifest cannot paper over.~~ **Fixed in application code;
  corrected 2026-09-20.** `src/aida/readiness.py` treats Temporal as an optional probe: the
  only `required` dependency of the API's `/health/ready` is PostgreSQL, and the API starts
  and serves degraded through a Temporal outage (AU-12, with a background reconnect in
  `src/aida/main.py`). This directory still touches no application code.
- **No NetworkPolicy, HPA, or autoscaling** is included. Minimum reviewable bar was the
  goal; add these once there's a real cluster and traffic pattern to tune them against.
- **No pipeline publishes or scans this image yet** (see "pinned image digest" above and
  audit remediation item #12). CI builds it and smoke-imports it (`docker-build`), but does
  not push it, record a digest or scan the image. This manifest's *shape* refuses a
  floating tag; it cannot by itself make a digest appear.
