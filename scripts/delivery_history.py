#!/usr/bin/env python3
"""Show what actually happened to each outbound notification.

`delivery_attempt` is append-only and holds the only record of how a
destination behaved: how many times it was tried, how far apart, with what
status code, and whether it ever answered. Its own docstring says it exists so
"an outage is legible after the fact" -- and until this script, nothing in the
product read it. The notification runbook handed the operator a five-line
`JOIN` to type into `psql` instead, which is a reader in the sense that a
person with database credentials is a reader.

R11-X2 filed the table as write-only and asked reader-or-retire. It is
emphatically not retire: the intent row carries current state, and this table
carries the history, which is the half you need precisely when something has
gone wrong and the current state says only "FAILED".

**Why this is a script and not an endpoint.** R11-X5 measured ~178 routes with
no UI calling them, and the finding was that the platform's problem is surfaces
built ahead of their front doors, not a shortage of routes. Adding a 179th
would have moved this from "no reader" to "no reachable reader". The operator
already runs `seed_model_route.py` and `verify_end_to_end.py` from the setup
guide; this belongs with those.

**On destinations.** A Slack or Teams webhook URL is a bearer credential in
its path, and this prints destinations. It is safe because the stored value is
already `delivery_intents.destination_label(...)` -- scheme, host and a
digest, never the path -- so the credential is not in the row to leak. A test
pins that, because the day someone stores the raw URL this becomes a
credential dump.

**Teams.** A 2xx here is *not* proof anyone saw the message. A Power Automate
Workflows webhook answers 202 from its trigger, before the post-card action
runs, so a flow that then fails -- bad payload, deleted channel, orphaned flow
-- records DELIVERED with status 202 and posts nothing. For Teams the only real
evidence is the flow's own run history. The output says so next to every Teams
row rather than in a footnote.

Usage:
    python scripts/delivery_history.py
    python scripts/delivery_history.py --state DEAD_LETTER
    python scripts/delivery_history.py --kind GOVERNANCE_NOTIFICATION --limit 20
    python scripts/delivery_history.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from aida.db import session_factory
from aida.models import DeliveryAttempt, DeliveryIntent

#: Transports whose 2xx does not mean the message was seen. Keyed on the
#: transport name the ledger stores, so a new transport with the same property
#: is added here rather than in the rendering.
RECEIPT_IS_NOT_PROVEN_BY_2XX = {"TEAMS"}


def _age(moment: datetime | None, *, now: datetime) -> str:
    if moment is None:
        return "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = (now - moment).total_seconds()
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


async def collect(
    *, state: str | None, kind: str | None, limit: int
) -> list[dict[str, Any]]:
    """Intents newest-first, each with its full attempt history."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        filters = []
        if state:
            filters.append(DeliveryIntent.state == state.upper())
        if kind:
            filters.append(DeliveryIntent.kind == kind.upper())
        intents = list(
            (
                await session.scalars(
                    select(DeliveryIntent)
                    .where(*filters)
                    .order_by(DeliveryIntent.requested_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        if not intents:
            return []
        attempts = list(
            (
                await session.scalars(
                    select(DeliveryAttempt)
                    .where(DeliveryAttempt.intent_id.in_([i.id for i in intents]))
                    .order_by(DeliveryAttempt.attempt_number)
                )
            ).all()
        )

    by_intent: dict[Any, list[DeliveryAttempt]] = {}
    for attempt in attempts:
        by_intent.setdefault(attempt.intent_id, []).append(attempt)

    rendered: list[dict[str, Any]] = []
    for intent in intents:
        history = by_intent.get(intent.id, [])
        rendered.append(
            {
                "intent_id": str(intent.id),
                "kind": intent.kind,
                "state": intent.state,
                "requested_at": intent.requested_at.isoformat()
                if intent.requested_at
                else None,
                "requested_age": _age(intent.requested_at, now=now),
                "delivered_at": intent.delivered_at.isoformat()
                if intent.delivered_at
                else None,
                "attempt_count": int(intent.attempt_count or 0),
                # An intent with state FAILED_* and no attempt rows has never
                # been tried, which is a different problem from having been
                # tried and refused -- usually the worker being switched off.
                "attempts": [
                    {
                        "number": a.attempt_number,
                        "outcome": a.outcome,
                        "transport": a.transport,
                        "destination": a.destination,
                        "status_code": a.status_code,
                        "detail": a.detail,
                        "at": a.started_at.isoformat() if a.started_at else None,
                        "age": _age(a.started_at, now=now),
                        "receipt_proven": _receipt_proven(a),
                    }
                    for a in history
                ],
            }
        )
    return rendered


def _receipt_proven(attempt: DeliveryAttempt) -> bool | None:
    """Whether this attempt's status code is evidence the message was seen.

    `None` means "cannot be concluded from here" and is not the same as False:
    a Teams 202 is a real acknowledgement *of the trigger*, so calling it a
    failure would be as wrong as calling it a receipt.
    """
    if attempt.status_code is None or not 200 <= attempt.status_code < 300:
        return False
    if attempt.transport.upper() in RECEIPT_IS_NOT_PROVEN_BY_2XX:
        return None
    return True


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "No delivery intents.\n\n"
            "If you expected some, the two usual reasons are that nothing has\n"
            "queued one (AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED ships off), or\n"
            "that they queued and nothing drains them\n"
            "(AIDA_DELIVERY_WORKER_ENABLED ships off too). /health/ready\n"
            "reports the second as delivery_backlog.detail=...;worker=disabled."
        )
    lines: list[str] = []
    unproven = 0
    for row in rows:
        lines.append(
            f"{row['state']:<18} {row['kind']:<26} "
            f"requested {row['requested_age']:<10} attempts={row['attempt_count']}"
        )
        if not row["attempts"]:
            lines.append("    (no attempt rows -- never tried, not refused)")
        for attempt in row["attempts"]:
            code = attempt["status_code"] if attempt["status_code"] is not None else "-"
            lines.append(
                f"    #{attempt['number']} {attempt['outcome']:<20} "
                f"{attempt['transport']:<8} {code!s:<5} {attempt['age']:<10} "
                f"{attempt['destination']}"
            )
            if attempt["detail"]:
                lines.append(f"        {attempt['detail']}")
            if attempt["receipt_proven"] is None:
                unproven += 1
                lines.append(
                    "        NOTE: a 2xx here is the Workflows *trigger* "
                    "answering, not a posted card."
                )
        lines.append("")
    if unproven:
        lines.append(
            f"{unproven} Teams attempt(s) answered 2xx without proving receipt. "
            "The only\nevidence that a card was posted is the flow's run history "
            "in Power Automate;\nsee Docs/40-engineering/12-notification-delivery-runbook.md 3.2.4."
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="filter by intent state, e.g. DEAD_LETTER")
    parser.add_argument("--kind", help="filter by intent kind")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    rows = asyncio.run(
        collect(state=args.state, kind=args.kind, limit=max(1, args.limit))
    )
    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
