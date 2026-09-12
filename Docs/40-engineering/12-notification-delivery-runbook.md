# Notification and SIEM delivery: verification and operations runbook

> Status: Authoritative for R11-B10, written 2026-09-12.
> Audience: platform operators, and whoever is asked "did that alert actually go anywhere?".
> Related: `src/aida/delivery_intents.py`, `src/aida/governance_notifications.py`, `src/aida/readiness.py`, `tests/test_delivery_backlog_readiness.py`.

Governance notifications (Slack, Teams) and SIEM security events share one
durable ledger, one worker and one set of controls. This runbook covers both,
because in production they fail together and are diagnosed together.

## 1. What is proven, and what is not

This distinction is the point of the document. Do not read past it.

| Property | Proven how | Status |
|---|---|---|
| A governance event durably records that it owes a destination a message, inside the business transaction | `tests/test_governance_notifications.py` | Automated |
| Nothing is sent from the request path, so a downed destination cannot fail a governance decision | `tests/test_governance_notifications.py` | Automated |
| The scheduler's worker drains the ledger and a real HTTP destination receives the bytes | `tests/test_delivery_backlog_readiness.py::test_the_backlog_clears_only_once_the_destination_has_received_the_bytes`, driven from `notify_governance_event` through `run_delivery_worker_pass` against a loopback HTTP server | Automated, **against a loopback stub** |
| A refusing destination stays queued, ages, and is visible; an exhausted budget reads as failed | `tests/test_delivery_backlog_readiness.py` | Automated |
| Queue depth, dead letters and oldest-undelivered age are readable without logs | `aida.readiness.probe_delivery_backlog`, on `/health/ready` | Automated |
| **A real Slack / Teams / SOC collector accepts these bytes** | Nothing automated can show this | **Live-infrastructure-only — section 3** |

The last row is the honest gap. Everything above it is exercised through the
same functions production calls, but the destination in those tests is a
`ThreadingHTTPServer` on `127.0.0.1` (`tests/support/stub_servers.py`). What a
loopback stub cannot exercise is the part that is specific to a real vendor:
TLS chain and cipher negotiation against their endpoint, their authentication
of the webhook URL, their rate limits and 429 behaviour, their payload schema
validation, and any egress proxy or firewall between the platform and them.
Section 3 is the procedure that closes it, and it needs a real endpoint.

## 2. Reading the queue

`GET /health/ready` carries the delivery reading in two places: the probe's
state in `optional.delivery_backlog`, and its numbers in
`signals."delivery_backlog.detail"` as a `;`-separated `key=value` string.

```bash
curl -s https://<host>/health/ready | jq -r '.signals["delivery_backlog.detail"]'
# queued=3;queued_notification=2;queued_siem=1;failed=0;oldest_age_seconds=41.2;worker=enabled
```

| Key | Meaning | Acts on it |
|---|---|---|
| `queued` | Still owed to a destination: `PENDING`, `RETRYING` and `DELIVERING` rows. A claimed row is not a delivered one. | — |
| `queued_notification` / `queued_siem` | The same total split by kind, because a queued security event is not the same problem as a queued chat message. | Security, for `siem` |
| `failed` | `DEAD_LETTER` only. These will **never** be retried and need a person. | Operator |
| `failed_notification` / `failed_siem` | The dead letters split by kind. A dropped security event and a dropped chat message need different people. | Security, for `siem` |
| `oldest_age_seconds` | Age of the oldest undelivered row, measured from `requested_at` — when the governance decision committed, not when it was last attempted. Absent when the queue is empty. | Operator |
| `worker` | `enabled` or `disabled`, reflecting `delivery_worker_enabled`. | — |

**Alert on age, not on depth.** A backlog of 900 with
`oldest_age_seconds=30` is a busy system draining normally. A backlog of 3 with
`oldest_age_seconds=86400` is a wedged worker, a dead scheduler or a
destination that has been refusing for a day. Depth alone cannot tell those
apart, which is why the age is reported next to it.

Suggested thresholds, given `scheduler_poll_seconds` defaults to 10 and the
retry backoff is capped at `delivery_backoff_max_seconds` (default 900):

- `failed > 0` — page. Nothing recovers a dead letter on its own.
- `oldest_age_seconds > 3600` while `worker=enabled` — page.
- `worker=disabled` with `queued` rising — not an incident; it is a deployment
  that never opted in. Decide whether it should have.

The probe is **optional** and never fails readiness, however large the queue
gets: a wedged chat webhook is not a reason to take the API out of rotation. It
reports `DOWN` only when it could not read the ledger at all.

## 3. Verifying a real destination

This is the procedure that closes the gap in section 1. It needs an endpoint
the vendor issued; there is no way to shortcut that.

### 3.1 Slack or Teams

1. Create an incoming webhook in a **throwaway channel**, not a channel anyone
   watches. The URL is a bearer credential: anyone holding it can post as the
   integration. The platform never persists it — `destination_label` in
   `src/aida/delivery_intents.py` reduces it to a scheme, host and digest
   before it reaches the ledger or any log — but your shell history will hold
   it, so use a file or a secret manager rather than an inline argument.

2. Configure and enable, then restart the API and the scheduler:

   ```bash
   export AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED=true
   export AIDA_SLACK_WEBHOOK_URL="$(cat ./slack-webhook.secret)"
   export AIDA_DELIVERY_WORKER_ENABLED=true          # default is false
   export AIDA_PORTAL_BASE_URL="https://<portal-host>"
   ```

   `delivery_worker_enabled` is the switch that matters. With it false the
   intents still queue durably — nothing is lost — but nothing is ever sent.

3. Confirm the queue is empty before you start, so what arrives is yours:

   ```bash
   curl -s https://<host>/health/ready | jq -r '.signals["delivery_backlog.detail"]'
   ```

4. Trigger a real governance event rather than injecting a row. Requesting
   approval on any governed object emits `REVIEW_REQUESTED`, which is in
   `governance_notification_events` by default. Using the product is the point:
   it exercises the hook point, the routing and the render together.

5. Watch the queue cross the scheduler interval. Within
   `scheduler_poll_seconds` (default 10) of the event, `queued` should rise by
   one per configured channel and then return to its previous value:

   ```bash
   for i in $(seq 1 12); do
     curl -s https://<host>/health/ready | jq -r '.signals["delivery_backlog.detail"]'
     sleep 5
   done
   ```

6. **Confirm receipt in the channel itself.** This is the step that has no
   automated equivalent. The message carries a headline, the object type and
   id, the risk tier, and a deep link back into the portal. Follow the link and
   check it lands on the right object.

7. Confirm the platform agrees it was delivered, rather than trusting the
   counter alone. In the database, the intent must carry a `delivered_at` and
   a `DeliveryAttempt` row with the destination's own status code:

   ```sql
   SELECT i.state, i.attempt_count, i.delivered_at, a.status_code, a.detail
     FROM delivery_intent i
     JOIN delivery_attempt a ON a.intent_id = i.id
    WHERE i.kind = 'GOVERNANCE_NOTIFICATION'
    ORDER BY i.requested_at DESC
    LIMIT 5;
   ```

   `state = 'DELIVERED'` with `status_code` in the 2xx range is the vendor's
   own acknowledgement. Anything else did not arrive, whatever the channel
   looks like.

### 3.2 Failure and recovery, against the same real endpoint

Verifying the happy path alone proves half of it. Repeat with the destination
broken, because that is the case the ledger exists for:

1. Revoke or rotate the webhook in the vendor's UI, leaving the platform
   configured with the stale URL.
2. Trigger another governance event.
3. `queued` stays at one and `oldest_age_seconds` climbs past the backoff
   intervals. Confirm `delivery_attempt` accumulates rows carrying the vendor's
   real rejection (Slack answers a revoked hook with `404 invalid_token`).
4. After `delivery_max_attempts` (default 6), the row becomes `DEAD_LETTER` and
   the reading moves from `queued` to `failed`. **Nothing retries it after
   this point** — that is what makes `failed > 0` a page.
5. Restore a valid URL and trigger a third event. It should deliver on the next
   scheduler tick, proving the outage was survivable rather than lossy.

### 3.3 SIEM collectors

Same shape, with `AIDA_SIEM_ENABLED` and `AIDA_SIEM_ENDPOINT`. Note that the
shipped default `internal://security-log-pipeline` names no destination and
resolves to `NOT_CONFIGURED` — an upgrade sends nothing until a real endpoint
is set. Syslog transports (`src/aida/siem_delivery.py`) are UDP or TCP rather
than HTTP; UDP in particular cannot acknowledge, so a UDP destination can only
ever prove the bytes left this host. Prefer the webhook or TCP transport when
receipt matters.

## 4. Triage

| Reading | Most likely cause | Next step |
|---|---|---|
| `worker=disabled`, `queued` rising | Never opted in | Set `delivery_worker_enabled`; the queue drains from the beginning |
| `queued` rising, `oldest_age_seconds` rising, `failed=0` | Destination refusing retryably (5xx, 429, timeout) or scheduler dead | Read `last_error` on the intent; check the scheduler process is alive |
| `queued` flat and non-zero, age large, worker enabled | Rows stuck `DELIVERING` behind a worker that died mid-attempt | Claims self-release after `delivery_claim_seconds` (default 300); if not, the scheduler is not running |
| `failed` non-zero | Permanent rejection, or retry budget exhausted | `last_error` names it. Fix the destination, then re-queue — there is no automatic replay |
| Probe `DOWN` | The ledger could not be read | Check the `postgresql` probe; this usually accompanies it |

## 5. What this does not do

- **No automatic replay of dead letters.** Re-queueing is a deliberate,
  manual act, because the reason a message died permanently is usually a
  misconfiguration that would kill the replay too.
- **At-least-once, not exactly-once.** A process that dies between a
  destination's acknowledgement and the local commit will send again on the
  next pass. `claim_due_intents` in `src/aida/delivery_intents.py` documents
  why that is the right side to err on for a security event.
- **No per-recipient routing.** Every configured channel receives every
  selected event kind. Filtering is `governance_notification_events`, which is
  per deployment, not per person.
