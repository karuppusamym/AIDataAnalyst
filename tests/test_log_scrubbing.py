"""OB-8 / TS-3 (`Docs/60-delivery/03-tracker.md`): log-scrubbing verification.

The logs slice of INV-6 (`Docs/10-architecture/01-principles-and-invariants.md`):
raw secret material must not reach a rendered log line. `test_sentinel_scan_*`
runs the real `configure_logging` pipeline end to end and greps the captured
output for a sentinel value the way an operational sentinel scan would --
the other tests exercise the `redact_sensitive_data` processor directly for
faster, more precise coverage of what does and does not get redacted.
"""

import io
import json
import logging
from contextlib import redirect_stdout

import structlog

from atlas.platform.logging import configure_logging, redact_sensitive_data

_SENTINEL = "SENTINEL-DO-NOT-LEAK-9f18b2c4a6"


def test_redacts_known_sensitive_keys() -> None:
    event = {
        "event": "resolved credential",
        "password": _SENTINEL,
        "db_password": _SENTINEL,
        "api_key": _SENTINEL,
        "apiKey": _SENTINEL,
        "Authorization": _SENTINEL,
        "client_secret": _SENTINEL,
        "hmac_key": _SENTINEL,
        "connection_string": _SENTINEL,
        "cookie": _SENTINEL,
    }

    result = redact_sensitive_data(None, "info", event)

    for key in event:
        if key == "event":
            continue
        assert result[key] == "[REDACTED]", key
    assert result["event"] == "resolved credential"


def test_preserves_non_sensitive_fields() -> None:
    event = {
        "event": "ingested table",
        "tenant_id": "t-123",
        "table_name": "orders",
        "row_count": 42,
    }

    result = redact_sensitive_data(None, "info", event)

    assert result == event


def test_redacts_nested_structures() -> None:
    event = {
        "event": "resolved datasource",
        "datasource": {
            "host": "db.internal",
            "connection": {"password": _SENTINEL, "username": "svc-account"},
        },
        "attempts": [{"token": _SENTINEL}, {"token": _SENTINEL}],
    }

    result = redact_sensitive_data(None, "info", event)

    assert result["datasource"]["connection"]["password"] == "[REDACTED]"  # noqa: S105 -- redaction marker, not a credential
    assert result["datasource"]["connection"]["username"] == "svc-account"
    assert result["datasource"]["host"] == "db.internal"
    assert all(
        item["token"] == "[REDACTED]"  # noqa: S105 -- redaction marker, not a credential
        for item in result["attempts"]
    )


def test_redacts_whole_container_when_container_key_is_itself_sensitive() -> None:
    """A key that is itself secret-shaped (e.g. `credentials`) is redacted
    wholesale rather than recursed into -- a nested non-sensitive field
    inside a container named `credentials` is not a safe thing to assume,
    so the safer default is to drop the whole value.
    """
    event = {"credentials": {"username": "svc-account", "password": _SENTINEL}}

    result = redact_sensitive_data(None, "info", event)

    assert result["credentials"] == "[REDACTED]"


def test_redacts_secret_shaped_values_in_free_text() -> None:
    jwt = f"eyJhbGciOiJIUzI1NiJ9.{_SENTINEL}.signaturepart"
    dsn = f"postgresql://svc:{_SENTINEL}@db.internal:5432/atlas"
    event = {
        "event": f"connection failed for {dsn}",
        "bearer": f"Authorization header was Bearer {_SENTINEL}",
        "jwt_in_message": f"decoded token {jwt}",
    }

    result = redact_sensitive_data(None, "info", event)

    for value in result.values():
        assert _SENTINEL not in value


def test_sentinel_scan_end_to_end_log_output() -> None:
    """Configures the real logging pipeline and scans rendered output for
    a sentinel secret -- the same shape of check an operational sentinel
    scan (OB-8) would run against a log stream.
    """
    configure_logging("INFO")
    logger = structlog.get_logger("test.log_scrubbing")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        logger.info(
            "resolved secret for datasource",
            password=_SENTINEL,
            credentials={"api_key": _SENTINEL},
            tenant_id="t-123",
        )

    output = buffer.getvalue()
    assert _SENTINEL not in output
    assert "[REDACTED]" in output
    record = json.loads(output)
    assert record["tenant_id"] == "t-123"

    logging.shutdown()
