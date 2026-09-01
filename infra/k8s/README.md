# aida-api Kubernetes manifests

Tracker: **AU-9**. Audit: `Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` §4 —
*"No production deployment artifact exists. `infra/` contains four `init.sql` seed files.
No k8s, Helm, Terraform or systemd anywhere."* This directory is the first one.

## What this is

A plain, kustomize-composable set of Kubernetes manifests for the `aida-api` service
(the FastAPI process built by the repo-root `Dockerfile`, `uvicorn aida.main:app`). It is
**reviewable** — every value that the audit's C1/C2 findings care about is pinned in
`base/configmap.yaml`, not left to a deployer to remember:

- `AIDA_ENVIRONMENT=production` (audit C1: this defaults to `"development"` in
  `src/atlas/platform/config.py`, and `extra="ignore"` means a typo'd variable name is
  silently dropped rather than failing startup — pinning the *correct* name in a reviewed
  file is the mitigation available without an application-code change).
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
   pipeline produced. **That pipeline does not exist yet** (audit remediation item #12: "add
   dependency and secret scanning to CI, and build the container image in CI at all" is
   still open). Until it lands, the intended flow is: CI builds the image from this repo's
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

- **No non-`env` `SecretProvider` is implemented.** `configmap.yaml` sets
  `AIDA_CREDENTIAL_PROVIDER=vault` because `Settings` forbids `credential_provider=="env"`
  when `environment=="production"` — but audit remediation item #10 confirms only the
  `Protocol` and caching exist in `src/aida/secrets.py`; no real Vault/CyberArk/AWS-SM/
  Azure-KV/GCP-SM fetch is wired. Setting this value lets the process pass config
  validation at startup; it does not mean connector credential resolution actually works.
  That gap is tracked under item #10, not fixed by this manifest.
- **Resource requests/limits are defaults, not measurements.** No load test or profiling
  run exists for this codebase yet (audit §5). Treat the numbers in `deployment.yaml` as a
  reasonable starting point to watch in staging and revise, not a capacity-planning result.
- **The Temporal-outage readiness coupling (audit remediation #11)** is unfixed in
  application code — a Temporal outage can still take down `/health/ready` for reasons the
  probe wiring in this manifest cannot paper over. Out of scope here (no application code
  was touched for this item, by design).
- **No NetworkPolicy, HPA, or autoscaling** is included. Minimum reviewable bar was the
  goal; add these once there's a real cluster and traffic pattern to tune them against.
- **No CI pipeline builds or scans this image yet** (see "pinned image digest" above and
  audit remediation item #12). This manifest's *shape* refuses a floating tag; it cannot by
  itself make a digest appear.
