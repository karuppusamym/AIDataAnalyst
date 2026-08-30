import asyncio
import json
import signal
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiokafka import AIOKafkaProducer
from sqlalchemy import select

from aida.config import get_settings
from aida.db import session_factory
from aida.logging import configure_logging
from aida.models import OutboxEvent


@dataclass(slots=True)
class PublisherState:
    stopping: bool = False


def serialize_event(event: OutboxEvent) -> bytes:
    envelope: dict[str, Any] = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "organization_id": str(event.organization_id) if event.organization_id else None,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": event.payload,
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()


def retry_delay_seconds(attempt_count: int, maximum_seconds: int) -> int:
    exponential_delay: int = 2 ** min(max(1, attempt_count), 20)
    return min(exponential_delay, maximum_seconds)


def record_publish_success(event: OutboxEvent, *, now: datetime) -> None:
    """Mark a delivery attempt as successful. Mutates ``event`` in place."""
    event.status = "PUBLISHED"
    event.published_at = now
    event.last_error = None
    event.attempt_count += 1


def record_publish_failure(
    event: OutboxEvent,
    error: BaseException,
    *,
    max_attempts: int,
    max_backoff_seconds: int,
    now: datetime,
) -> None:
    """Record a failed delivery attempt, dead-lettering once attempts are exhausted.

    Mutates ``event`` in place. Below ``max_attempts`` the event stays PENDING with
    an exponential-backoff ``next_attempt_at``; at or beyond it, the event moves to
    DEAD_LETTER and stops being picked up by :func:`publish_batch` until an operator
    explicitly requeues it (see ``requeue_outbox_event`` in operational_api.py).
    """
    event.attempt_count += 1
    event.last_error = type(error).__name__
    if event.attempt_count >= max_attempts:
        event.status = "DEAD_LETTER"
    else:
        delay = retry_delay_seconds(event.attempt_count, max_backoff_seconds)
        event.next_attempt_at = now + timedelta(seconds=delay)


async def publish_batch(producer: AIOKafkaProducer, *, batch_size: int = 100) -> int:
    settings = get_settings()
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        events = (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.next_attempt_at <= now,
                )
                .order_by(OutboxEvent.occurred_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for event in events:
            try:
                await producer.send_and_wait(
                    "aida.platform.events.v1",
                    value=serialize_event(event),
                    key=event.aggregate_id.encode(),
                    headers=[("event_type", event.event_type.encode())],
                )
                record_publish_success(event, now=datetime.now(UTC))
            except Exception as exc:
                record_publish_failure(
                    event,
                    exc,
                    max_attempts=settings.outbox_max_attempts,
                    max_backoff_seconds=settings.outbox_max_backoff_seconds,
                    now=now,
                )
        return len(events)


async def run_publisher() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger(__name__)
    state = PublisherState()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, setattr, state, "stopping", True)

    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        client_id="aida-outbox-publisher",
        acks="all",
        enable_idempotence=True,
    )
    await producer.start()
    logger.info("outbox_publisher_started")
    try:
        while not state.stopping:
            published = await publish_batch(producer)
            if published:
                logger.info("outbox_batch_published", event_count=published)
            else:
                await asyncio.sleep(1)
    finally:
        await producer.stop()
        logger.info("outbox_publisher_stopped")


if __name__ == "__main__":
    asyncio.run(run_publisher())
