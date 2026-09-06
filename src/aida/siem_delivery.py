"""SIEM transports: a real webhook, a real syslog socket, and a refusal.

**Invariant this module exists to hold:** a SIEM receipt is a destination's
answer. `send` either gets an HTTP status from a collector or a completed
socket write to one, or it raises -- there is no third path that logs and
returns something success-shaped, because that was the defect
(`Docs/review-2026-09-05/REVIEW.md` F04).

Two transports, both stdlib or `httpx` (no new dependency):

* **webhook** -- HTTP POST of the minimised JSON payload, via
  `aida.delivery_intents.WebhookTransport`. 2xx is a receipt; 5xx, 408, 423,
  425, 429, timeouts and connection errors are retryable; every other 4xx is
  permanent, because repeating a message a collector rejects on its content
  is noise rather than resilience.
* **syslog** -- the CEF message wrapped in an **RFC 5424** header
  (`<PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG`, facility local0,
  severity mapped from the event's own, MSG prefixed with a UTF-8 BOM as
  RFC 5424 §6.4 specifies). Over **UDP** the message is one datagram. Over
  **TCP** it is framed with **RFC 6587 §3.4.1 octet counting** --
  `MSG-LEN SP SYSLOG-MSG`, where MSG-LEN counts the octets of the encoded
  message, not its characters. Getting that count wrong desynchronises a
  collector's stream for every subsequent message, so it is computed from the
  encoded bytes and covered by its own test.

A datagram write is not an acknowledgement and this module does not pretend
otherwise: the UDP receipt says `sent (udp, unacknowledged)`. TCP's receipt
records the octets the peer accepted. If a deployment needs delivery
confirmation from the collector, it should use TCP or the webhook.

`NullSiemTransport` is the default whenever `siem_endpoint` names nothing,
so an unconfigured deployment records NOT_CONFIGURED rather than a
fabricated success.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from aida.config import Settings
from aida.delivery_intents import (
    DeliveryTransport,
    NullTransport,
    TransportError,
    TransportReceipt,
    WebhookTransport,
)
from aida.models import DeliveryIntent
from aida.siem_routing import (
    SYSLOG_SEVERITY_MAP,
    SiemConfig,
    format_cef,
    format_webhook_payload,
    parse_siem_endpoint,
    routing_state,
)

logger = structlog.get_logger(__name__)

#: RFC 5424 facility 16 (local0). PRI = facility * 8 + severity.
SYSLOG_FACILITY: Final = 16
SYSLOG_APP_NAME: Final = "atlas"
SYSLOG_MSG_ID: Final = "CEF"
#: RFC 5424 §6.4: a UTF-8 MSG begins with the byte-order mark.
_BOM: Final = b"\xef\xbb\xbf"
_NILVALUE: Final = "-"


def syslog_priority(severity: str) -> int:
    """PRI value for one of our four severities. Facility is always local0."""
    return SYSLOG_FACILITY * 8 + SYSLOG_SEVERITY_MAP.get(severity.upper(), 5)


def format_syslog_message(payload: Mapping[str, Any], *, hostname: str, procid: str) -> bytes:
    """One RFC 5424 message, encoded. No framing -- see `frame_octet_counted`."""
    timestamp = payload.get("timestamp")
    moment = datetime.fromisoformat(str(timestamp)) if timestamp else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    # RFC 5424 TIMESTAMP is RFC 3339 with at most 6 fractional digits.
    stamp = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    priority = syslog_priority(str(payload.get("severity", "MEDIUM")))
    header = (
        f"<{priority}>1 {stamp} {hostname or _NILVALUE} {SYSLOG_APP_NAME} "
        f"{procid or _NILVALUE} {SYSLOG_MSG_ID} {_NILVALUE} "
    )
    return header.encode("utf-8") + _BOM + format_cef(dict(payload)).encode("utf-8")


def frame_octet_counted(message: bytes) -> bytes:
    """RFC 6587 §3.4.1 framing: `MSG-LEN SP SYSLOG-MSG`.

    MSG-LEN is the number of **octets** in `message`. Deriving it from
    `len(str)` instead of `len(bytes)` is the classic bug here: a CEF
    extension carrying a non-ASCII principal id would then under-count, and
    every subsequent message on that connection would be misparsed.
    """
    return f"{len(message)} ".encode("ascii") + message


class SyslogSiemTransport:
    """RFC 5424 over a real socket. UDP datagram, or TCP with octet counting."""

    name = "syslog"
    available = True

    def __init__(
        self,
        host: str,
        port: int,
        *,
        protocol: str = "udp",
        timeout_seconds: float = 5.0,
        hostname: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.protocol = protocol.lower()
        self.destination = f"syslog+{self.protocol}://{host}:{port}"
        self._timeout = timeout_seconds
        self._hostname = hostname or socket.gethostname()

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        message = format_syslog_message(payload, hostname=self._hostname, procid=str(os.getpid()))
        if self.protocol == "tcp":
            return self._send_tcp(frame_octet_counted(message))
        return self._send_udp(message)

    def _send_udp(self, message: bytes) -> TransportReceipt:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self._timeout)
                sock.sendto(message, (self.host, self.port))
        except OSError as exc:
            raise TransportError(f"syslog udp send failed: {exc}", retryable=True) from exc
        return TransportReceipt(
            destination=self.destination,
            transport=self.name,
            detail=f"sent (udp, unacknowledged) {len(message)} octets",
        )

    def _send_tcp(self, frame: bytes) -> TransportReceipt:
        try:
            with socket.create_connection((self.host, self.port), timeout=self._timeout) as sock:
                sock.settimeout(self._timeout)
                sock.sendall(frame)
        except (TimeoutError, ConnectionError) as exc:
            raise TransportError(f"syslog tcp send failed: {exc}", retryable=True) from exc
        except OSError as exc:
            raise TransportError(f"syslog tcp send failed: {exc}", retryable=True) from exc
        return TransportReceipt(
            destination=self.destination,
            transport=self.name,
            detail=f"accepted {len(frame)} octets (tcp, rfc6587 octet-counted)",
        )


class SiemWebhookTransport(WebhookTransport):
    """The generic webhook, narrowed to the SIEM body shape.

    The stored payload is already minimised (`aida.siem_routing.siem_payload`),
    so this only drops null optional fields. `include_details` is not
    re-checked here and must not be: if details were suppressed at enqueue
    they are not in the payload at all.
    """

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        return super().send(format_webhook_payload(dict(payload)))


def build_siem_transport(config: SiemConfig) -> DeliveryTransport:
    """Resolve a `SiemConfig` to a transport. Never a silent no-op."""
    outcome, destination = routing_state(config)
    if not destination.usable:
        return NullTransport(destination.reason or f"SIEM routing is {outcome}")
    if destination.kind == "webhook":
        return SiemWebhookTransport(
            destination.url,
            timeout_seconds=config.timeout_seconds,
            verify_tls=config.verify_tls,
        )
    parsed = parse_siem_endpoint(config)
    return SyslogSiemTransport(
        parsed.host,
        parsed.port,
        protocol=parsed.protocol,
        timeout_seconds=config.timeout_seconds,
    )


def siem_config_from_settings(settings: Settings) -> SiemConfig:
    """The one place settings become a `SiemConfig`, so the two call sites and
    the worker cannot drift apart on which fields matter."""
    return SiemConfig(
        transport=settings.siem_transport,
        endpoint=settings.siem_endpoint,
        enabled=settings.siem_enabled,
        include_details=settings.siem_include_details,
        timeout_seconds=settings.siem_delivery_timeout_seconds,
        verify_tls=settings.delivery_webhook_verify_tls,
    )


def siem_transport_for(intent: DeliveryIntent, settings: Settings) -> DeliveryTransport:
    """`TransportResolver` for `KIND_SIEM`.

    The destination is re-read from settings rather than from the intent, so
    a corrected endpoint applies to everything already queued -- which is what
    turns "the collector address was wrong" from a permanent data loss into a
    configuration change plus one worker pass.
    """
    return build_siem_transport(siem_config_from_settings(settings))
