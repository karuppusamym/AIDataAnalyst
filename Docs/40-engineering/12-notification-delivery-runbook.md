# Notification and SIEM delivery: verification and operations runbook

> Status: Authoritative for R11-B10, written 2026-09-12. Teams rewritten
> 2026-09-12 for the Workflows / Adaptive Card path, after Microsoft's
> retirement of Office 365 connectors completed in May 2026 — section 3.2.
> Audience: platform operators, and whoever is asked "did that alert actually go anywhere?".
> Related: `src/aida/delivery_intents.py`, `src/aida/governance_notifications.py`, `src/aida/readiness.py`, `tests/test_delivery_backlog_readiness.py`, `tests/test_governance_notifications.py`.

Unlike the dated review snapshots elsewhere in `Docs/`, this file is
maintained in place: it tells an operator what to do *now*, so a stale
procedure here is not a historical record but a wrong instruction. Section 3.2
exists because the previous one told a Teams administrator to create an
incoming webhook, which since May 2026 produces nothing that works.

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
| The Teams body leaving the process is the Workflows message envelope carrying an Adaptive Card | `tests/test_governance_notifications.py::test_teams_receives_an_adaptive_card_from_the_real_entry_point`, driven from `notify_governance_event` through `run_delivery_worker_pass` | Automated, **against a loopback stub** |
| The legacy connector `MessageCard` body is still reachable by configuration, for a tenant that needs it | `tests/test_governance_notifications.py::test_the_legacy_message_card_is_still_selectable_end_to_end` | Automated |
| Neither Teams card carries an action, an input, or anything else a person could act on | `tests/test_governance_notifications.py::test_no_adaptive_card_carries_anything_to_act_on`, over every event kind and the whole card tree | Automated |
| **A real Slack / Teams / SOC collector accepts these bytes** | Nothing automated can show this | **Live-infrastructure-only — section 3** |
| **A Teams Workflow posts the card into the channel** | A Workflows webhook answers before its flow runs — see below | **Live-infrastructure-only — section 3.2** |

The last two rows are the honest gap. Everything above them is exercised
through the same functions production calls, but the destination in those
tests is a `ThreadingHTTPServer` on `127.0.0.1`
(`tests/support/stub_servers.py`). What a loopback stub cannot exercise is the
part that is specific to a real vendor: TLS chain and cipher negotiation
against their endpoint, their authentication of the webhook URL, their rate
limits and 429 behaviour, their payload schema validation, and any egress
proxy or firewall between the platform and them. Section 3 is the procedure
that closes it, and it needs a real endpoint.

**Teams has a second gap on top of that one, and it is a trap.** A Workflows
(Power Automate) webhook answers the caller `202 Accepted` as soon as its
trigger has taken the request, *before* the flow's "post card" action runs. A
flow that then fails — a payload its action rejects, a disabled or orphaned
flow, a deleted channel — fails silently as far as this platform can see: the
intent is `DELIVERED`, `status_code` is 202, and no card was ever posted. So
for Teams, unlike Slack, **a 2xx is not evidence that anybody saw the
message.** Only the channel itself and the flow's own run history are. Section
3.2 says where to look.

## 2. Reading the queue

`GET /health/ready` carries the delivery reading in two places: the probe's
state in `optional.delivery_backlog`, and its numbers in
`signals."delivery_backlog.detail"` as a `;`-separated `key=value` string.

```bash
curl -s https://<host>/health/ready | jq -r '.signals["delivery_backlog.detail"]'
# failed=0;oldest_age_seconds=41.2;queued=3;queued_notification=2;queued_siem=1;worker=enabled
```

Keys are emitted in alphabetical order, and the per-kind and `oldest_age_seconds`
keys appear only when there is something to report — an empty queue has no
oldest row and names no kinds.

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

### 3.1 Slack

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
   looks like. (For Teams this is necessary but **not** sufficient — see
   3.2.4.)

### 3.2 Microsoft Teams

Teams is not "Slack with a different URL", and the difference is not
cosmetic. What follows replaces the old instruction to create an incoming
webhook, which no longer produces a working destination.

#### 3.2.1 Why the procedure changed

Atlas used to post a `MessageCard` — the Office 365 connector body. Microsoft
[retired Office 365 connectors within Microsoft
Teams](https://devblogs.microsoft.com/microsoft365dev/retirement-of-office-365-connectors-within-microsoft-teams/)
(notice last updated 2026-04-14), disabling them over **2026-05-18 to
2026-05-22**. That date is in the past. A connector URL issued before it is
dead, and in a regulated tenant connectors were very often switched off by
policy long before Microsoft got to them.

The supported mechanism is a **Workflows (Power Automate)** webhook taking an
**Adaptive Card**, which is what `teams_card_format` defaults to
(`ADAPTIVE_CARD` in `atlas.platform.config`). The legacy body is still
selectable — see 3.2.5 — because a tenant running under an extension, or a
workflow built on an action that happens to accept it, should not be broken by
an upgrade. It is a compatibility hatch, not a second supported path.

#### 3.2.2 What the Teams administrator does

This part is done in Teams, by someone who can create a workflow in the target
channel. It is theirs, not the platform operator's; the two halves meet at a
URL.

1. In **Teams**, open the channel that should receive governance
   notifications. Use a **throwaway channel** for verification, not one people
   watch.
2. Select **More options (…)** next to the channel name, then **Workflows**.
3. Choose the template **Post to a channel when a webhook request is
   received** (in some tenants: *Send webhook alerts to a channel*). If no
   template fits, create a workflow from scratch with the **When a Teams
   webhook request is received** trigger followed by a **Post card in a chat
   or channel** action.
4. Confirm the workflow's parameters — the team and channel it posts to, and
   who it posts as — then **Save**.
5. Copy the generated webhook URL. **This is the only time it is shown.**
6. Hand it over out of band (a secret manager, not email or chat) and record
   who owns the workflow.

Two things to tell the administrator, because both have bitten real
deployments:

- **A workflow belongs to a person, not to the channel.** If its owner leaves
  the organization it becomes an orphan flow and stops running — notifications
  go quiet with no error anywhere in Atlas. Add at least one co-owner at
  creation time. Microsoft documents this under
  [manage orphan flows](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/flow-management/manage-orphan-flow-when-owner-leaves-org).
- **The URL is a bearer credential.** Anyone holding it can post into that
  channel as the flow. Atlas never persists it (`destination_label` reduces it
  to scheme, host and digest before it reaches the ledger or any log), but
  everything upstream of Atlas will.

#### 3.2.3 What the platform operator pastes where

Exactly one variable carries the URL, and one more selects the format:

```bash
export AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED=true
export AIDA_TEAMS_WEBHOOK_URL="$(cat ./teams-webhook.secret)"
export AIDA_TEAMS_CARD_FORMAT=ADAPTIVE_CARD        # the default; see 3.2.5
export AIDA_DELIVERY_WORKER_ENABLED=true           # default is false
export AIDA_PORTAL_BASE_URL="https://<portal-host>"
```

`AIDA_TEAMS_CARD_FORMAT` may be omitted entirely — `ADAPTIVE_CARD` is the
shipped default, and it is the value a current tenant needs. Restart the API
and the scheduler afterwards; settings are read at process start.

#### 3.2.4 The one command that proves delivery

This posts the **real** payload — generated by the same builder the delivery
worker uses, not a hand-written approximation that could drift from it —
straight at the webhook, and prints the HTTP status. Run it **in the API's own
environment**, on a host where the service's variables are already set, so
that `get_settings()` resolves the same `teams_card_format` and
`portal_base_url` production will use:

```bash
python -c "
import json
from aida.config import get_settings
from aida.governance_notifications import render_message
print(json.dumps(render_message(get_settings(), 'REVIEW_REQUESTED', {
    'object_type': 'GLOSSARY_TERM', 'object_name': 'Webhook delivery probe',
    'object_id': '00000000-0000-0000-0000-000000000000', 'risk_tier': 'T1',
    'principal_id': 'operator:verification'}, channel='TEAMS')))
" > ./teams-probe.json && curl -sS -X POST -H 'Content-Type: application/json' \
     --data-binary @./teams-probe.json -w '\nHTTP %{http_code}\n' \
     "$(cat ./teams-webhook.secret)"
```

The `&&` and the intermediate file are deliberate, not clumsiness. Piping
`python | curl` looks tidier and is actively misleading here: if the render
raises — a missing variable, a settings validator — `curl` still runs, posts
an **empty body**, and a Workflows webhook answers `202` to that just as
cheerfully as to a real card. The chain above cannot post what was never
rendered, and leaves `teams-probe.json` for you to read.

**Read the result carefully — this is the trap in the Teams path.**

| What you see | What it means |
|---|---|
| `HTTP 202` **and a card in the channel** | Delivered. This is the only success. |
| `HTTP 202` and **no card** | The trigger accepted the request and the flow then failed. Atlas would record this as `DELIVERED`. Open **Workflows → your workflow → run history** and read the failed action. A `Property 'type' must be 'AdaptiveCard'` there means the flow's action cannot take the configured format — see 3.2.5. |
| `HTTP 400` / `403` / `404` | The URL is wrong, revoked, or the flow is turned off. Atlas retries 5xx/408/429 and dead-letters these. |
| `HTTP 429` | Throttling. Teams throttles above roughly four requests a second; Atlas treats 429 as retryable. |

A Workflows webhook answers `202 Accepted` from its **trigger**, before the
"post card" action runs. So a 2xx proves the request was accepted for
processing and nothing more. The channel and the flow's run history are the
only evidence a human saw anything — which is precisely the part no test in
this repository can reach.

Once the probe posts a card, repeat steps 4–7 of 3.1 to prove the same thing
through the product: trigger a real governance event, watch `queued` rise and
fall across a scheduler tick, and confirm the intent reached `DELIVERED`.

#### 3.2.5 If the card does not render

In order, cheapest first:

1. **Check the flow's run history** before changing anything here. Most
   failures are the flow, not the payload: a turned-off flow, an orphaned one,
   a deleted channel, or a connection needing re-authentication.
2. **Check the action the workflow uses.** Microsoft accepts a legacy
   `MessageCard` at the webhook *endpoint*, but the **Post card in a chat or
   channel** action takes Adaptive Cards only. A workflow assembled by hand
   with a text-only **Post message** action will not render either format's
   card.
3. **Only then consider the legacy format.** If this tenant genuinely still
   has a working connector URL, or a workflow whose action wants a
   MessageCard:

   ```bash
   export AIDA_TEAMS_CARD_FORMAT=MESSAGE_CARD
   ```

   Re-run 3.2.4 and expect `"@type": "MessageCard"` in the posted body.
   Buttons will not render in this format under Workflows and Atlas emits none
   anyway (see 3.2.6). Treat this as a temporary state and record why it is
   set — the mechanism behind it is retired, not merely old.

#### 3.2.6 What the card deliberately does not do

The Adaptive Card presents information and a deep link. It carries **no**
`Action.Submit`, `Action.Execute` or `Action.Http`, no `Input.*` field, and no
`actions` array at all; the portal link is a Markdown link inside a text
block, not a button.

That is a governance constraint, not a rendering preference, and it survived
the format change deliberately. Approving, publishing or granting in Atlas is
authorized in the portal against the portal's own authentication, tiering and
maker-checker rules. A card that could approve from a chat client would be a
second control surface with none of those checks — and a button rendered
beside the headline "Approval requested" is the one thing a reviewer in a
hurry could misread as being the approval.
`tests/test_governance_notifications.py::test_no_adaptive_card_carries_anything_to_act_on`
holds this over every event kind and the whole card tree. If someone asks for
approve buttons in Teams, that is a product decision about where authorization
happens, and it needs an ADR rather than an edit here.

### 3.3 Failure and recovery, against the same real endpoint

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

### 3.4 SIEM collectors

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
| **Teams:** intents reach `DELIVERED` with `status_code = 202` but no card appears | The Workflow's trigger accepted the request and the flow then failed. Nothing in this platform can see that | Workflows → the workflow → run history. §3.2.4 and §3.2.5 |
| **Teams:** notifications went quiet with nothing queued or failed | Most often an orphaned workflow whose owner left the organization, or one that was turned off | Have the Teams admin check the flow is on and has a co-owner. §3.2.2 |
| **Teams:** the flow's history says `Property 'type' must be 'AdaptiveCard'` | `teams_card_format` is `MESSAGE_CARD` against an action that only takes Adaptive Cards | Unset `AIDA_TEAMS_CARD_FORMAT` to return to the default. §3.2.5 |

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
