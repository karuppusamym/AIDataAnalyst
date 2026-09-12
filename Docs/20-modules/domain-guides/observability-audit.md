# Domain guide — observability_audit

> Orientation, not specification. The full spec is
> [`../20-observability-and-audit.md`](../20-observability-and-audit.md); the
> generated shape of the module is in
> [`../../10-architecture/14-generated-architecture-map.md`](../../10-architecture/14-generated-architecture-map.md).

**Code:** `src/atlas/modules/observability_audit/` · **Spec:** [`../20-observability-and-audit.md`](../20-observability-and-audit.md)

## What it owns

Evidence, and the machinery that proves the evidence left the building. Eleven
tables in four families:

- **The ledger** — `audit_event`, and `outbox_event`, the transactional outbox
  every domain event is written into alongside the state change that caused it.
- **The archive** — `audit_archive_record`, `audit_archive_membership`,
  `audit_archive_lease`. Membership is an explicit fact, not a range inferred
  from a cursor; the lease is a row with an expiry, because it has to survive the
  gap between an upload and its verification.
- **External delivery** — `delivery_intent` and `delivery_attempt`, the durable
  record behind SIEM and webhook routing.
- **Compliance artefacts** — `compliance_pack`, `access_review_report`.
  Service levels used to live here too (`slo_definition`, `slo_measurement`);
  both tables were retired on 2026-09-12 (R11-D10, migration `f3a91c27b5de`)
  because nothing ever wrote a measurement and there was no indicator source
  to write one from — an SLO was bound to no measurable signal, and nothing in
  this repository scrapes the Prometheus exposition on `/metrics`.

## Invariants it must uphold

- **INV-7, attributability.** This context is where attributability is *stored*.
  If the ledger is wrong, no other module's audit call means anything.
- **INV-6, value-freedom.** Control-plane state records that something happened,
  not the customer values it happened to. Audit rows must not become a copy of
  the data they describe.
- **Success means a destination acknowledged it.** The 2026-09-05 review's first
  two findings were an archive and a SIEM route that both reported completion
  without storing or delivering anything. Progress advances only past bytes a
  destination acknowledged *and* handed back intact; `DELIVERED` is unreachable
  from the enqueue path by construction.
- **The checksum covers what an auditor needs protected.** The archive envelope
  is a versioned, length-delimited canonical serialization of the whole envelope,
  so a new field cannot silently escape the hash and field boundaries cannot be
  shifted.

## Entry points

- **HTTP** — 2 routes, the smallest surface of the five contexts, and
  deliberately so: the archive status and cost showback. Reading the ledger
  itself is not an HTTP route here. The three SLO routes
  (`POST`/`GET /v1/observability/slo` and the budget read) were retired on
  2026-09-12 — see R11-D10 above.
- **Mounted through a shim.** `aida.main` imports this router as
  `aida.observability_api`; two tests import handler functions from that path
  directly. Recorded in
  [`../../40-engineering/09-compatibility-shim-register.md`](../../40-engineering/09-compatibility-shim-register.md).
- **In-process, and this is the main one.** `aida.events` writes audit and outbox
  rows; nearly every mutating route in the system reaches this context that way
  rather than over HTTP.
- **Processes** — the outbox publisher and graph projector both run against
  `outbox_event`, and the archive sweep runs from the application's own lifespan.

## What it deliberately does not own

- **Emitting the events.** Every other module decides what is worth recording;
  this context stores it. `aida.events` is the write helper and lives outside
  the module.
- **The archive and delivery workers.** `aida.worm_archive`, `aida.siem_delivery`
  and `aida.siem_routing` hold that behaviour. This context owns their tables,
  not their loops.
- **Tracing and metrics.** OpenTelemetry configuration and the Prometheus
  registry are `aida.observability` and `aida.main`, not here.
- **Deciding retention or legal hold policy.** It executes and records hold
  state; what must be held, and for how long, is a governance decision made
  elsewhere.

## Current shape, honestly

`models.py` is substantial and real; `schemas.py` and `router.py` are small
because most of this context is written to in-process rather than called over
HTTP. `service.py`, `repository.py`, `contracts.py`, `events.py` and `workers/`
are empty scaffolds — note the irony that the module's own `events.py` is a
scaffold while the platform's real event-writing helper lives at `aida.events`
outside it. That is the seam to close first if this context is extracted further.

The `observability_audit module privacy` import-linter contract protects the
internals and names `aida.observability_api` as a permitted importer.
