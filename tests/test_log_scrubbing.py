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

import httpx
import structlog

from atlas.platform.logging import (
    RedactStdlibLogRecords,
    configure_logging,
    redact_log_text,
    redact_sensitive_data,
)

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


def test_redacts_value_shaped_keys() -> None:
    """AU-4 / C3 (`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md`): defense
    in depth for INV-6, not just secrets -- a raw source exception or SQL echo
    reaching a log call must not survive rendering even if the call site that put
    it there was missed. `exception`, `error_message`, `sql`, `parameters` and
    `row` are exactly the field names the audit found evaluating non-sensitive
    under the secret-shaped denylist.
    """
    event = {
        "event": "table profiling failed",
        "exception": f"Traceback ...\nKey (account_no)=({_SENTINEL}) already exists",
        "error_message": _SENTINEL,
        "sql": f"SELECT * FROM t WHERE account_no = '{_SENTINEL}'",  # noqa: S608
        "parameters": {"account_no": _SENTINEL},
        "row": {"account_no": _SENTINEL},
    }

    result = redact_sensitive_data(None, "info", event)

    for key in event:
        if key == "event":
            continue
        assert result[key] == "[REDACTED]", key
    assert result["event"] == "table profiling failed"


def test_value_shaped_redaction_does_not_over_match_similar_keys() -> None:
    """The value-shaped set matches by exact key name, not substring -- unlike
    `_SENSITIVE_KEY_TOKENS`, which intentionally over-matches for secrets. `row`
    must not also redact `row_count`, a plain non-sensitive integer, and
    `error_class` (the safe half of the pattern this fix uses everywhere) must
    stay untouched.
    """
    event = {
        "event": "ingested table",
        "row_count": 42,
        "error_class": "RuntimeError",
        "sql_dialect": "postgres",
    }

    result = redact_sensitive_data(None, "info", event)

    assert result == event


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


# --- R11-AUD14: a key in a URL reached the container log through httpx ----------------------------


def test_redact_log_text_hides_secret_query_parameters_and_keeps_the_rest() -> None:
    text = (
        f'HTTP Request: GET https://provider.example/v1beta/models?key={_SENTINEL}&pageSize=50 '
        '"HTTP/1.1 200 OK"'
    )

    redacted = redact_log_text(text)

    assert _SENTINEL not in redacted
    assert "?key=[REDACTED]&pageSize=50" in redacted
    assert redacted.endswith('"HTTP/1.1 200 OK"')


def test_redact_log_text_matches_parameter_names_case_insensitively() -> None:
    for name in ("key", "KEY", "api_key", "api-key", "access_token", "token", "sig", "Signature"):
        redacted = redact_log_text(f"GET https://x.example/p?a=1&{name}={_SENTINEL}")
        assert _SENTINEL not in redacted, name
        assert "a=1" in redacted, name


def test_redact_log_text_leaves_an_ordinary_url_alone() -> None:
    text = "HTTP Request: GET https://api.example/v1/models?limit=10&page=2 \"HTTP/1.1 200 OK\""

    assert redact_log_text(text) == text


class _Collect(logging.Handler):
    """Renders records the way a real handler would: from `getMessage()`, after the filters ran."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_an_httpx_request_line_never_carries_the_key_to_a_handler() -> None:
    """The real scenario: an httpx client sends `?key=...`, httpx logs the whole URL at INFO, and
    the handler must render the line without it. No network: a mock transport answers."""
    httpx_logger = logging.getLogger("httpx")
    handler = _Collect()
    handler.addFilter(RedactStdlibLogRecords())
    previous_level = httpx_logger.level
    httpx_logger.addHandler(handler)
    httpx_logger.setLevel(logging.INFO)
    try:
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.get("https://provider.example/v1beta/models", params={"key": _SENTINEL})
    finally:
        httpx_logger.removeHandler(handler)
        httpx_logger.setLevel(previous_level)

    lines = [message for message in handler.messages if "HTTP Request" in message]
    assert lines, "httpx did not log the request; the test is not exercising what it claims to"
    assert all(_SENTINEL not in line for line in lines)
    assert any("key=[REDACTED]" in line for line in lines)


def test_configure_logging_installs_the_stdlib_redaction_on_every_root_handler() -> None:
    configure_logging("INFO")
    configure_logging("INFO")  # every process calls it, and tests do again: it must not stack

    handlers = logging.getLogger().handlers
    assert handlers
    for handler in handlers:
        installed = [f for f in handler.filters if isinstance(f, RedactStdlibLogRecords)]
        assert len(installed) == 1, handler
