"""Structured logging configuration (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`).

Moved verbatim from `aida.logging`. `aida.logging` now re-exports from here
for backward compatibility; new code should import from this module
directly.

**OB-8 / TS-3 (`Docs/60-delivery/03-tracker.md`)**: every log record passes
through `_redact_sensitive_data` before rendering. This is the logs slice of
INV-6 (`Docs/10-architecture/01-principles-and-invariants.md`) -- raw
secret material and source values must not reach a log line by default.
Redaction happens by key name (denylist, case-insensitive, checked against
every nested mapping key) and, as defense in depth for secrets logged into
free-text messages rather than structured fields, by value pattern
(bearer tokens, JWTs, credentialed connection strings, common cloud key
shapes). It is intentionally conservative about false positives: an
unmatched value is left alone rather than guessed at.
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_REDACTED = "[REDACTED]"

# Key names that carry secret or credential material somewhere in the
# codebase (`aida.secrets`, `aida.config`, OIDC/JWT handling, connector
# credentials). Matched case-insensitively against the full key and against
# '_'-delimited tokens within it, so `db_password`, `apiKey`, and
# `client-secret` all match without over-matching unrelated keys like
# `secret_id` being missed or `is_secret_configured` being falsely spared.
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "apikey",
        "api_key",
        "authorization",
        "auth",
        "jwt",
        "hmac",
        "hmackey",
        "privatekey",
        "private_key",
        "connectionstring",
        "connection_string",
        "dsn",
        "cookie",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "sessionkey",
        "signingkey",
    }
)

_VALUE_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{10,}=*"),
    re.compile(r"\w+://[^:@/\s]+:[^@/\s]+@[^\s]+"),  # user:pass@host DSNs
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
]


def _key_is_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", key.lower())
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern in _VALUE_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        return redacted
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list | tuple | set):
        return type(value)(_redact_value(item) for item in value)
    return value


def _redact_mapping(mapping: dict[Any, Any]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key, value in mapping.items():
        if isinstance(key, str) and _key_is_sensitive(key):
            result[key] = _REDACTED
        else:
            result[key] = _redact_value(value)
    return result


def redact_sensitive_data(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """structlog processor: redacts secret-shaped keys and values in place.

    Public so it can be unit-tested directly (`tests/test_log_scrubbing.py`)
    without going through the full logging pipeline.
    """
    return _redact_mapping(dict(event_dict))


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_sensitive_data,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
