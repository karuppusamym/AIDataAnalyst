"""R11-FP02 remainder: a refusal classified from each driver's own structured code.

`capability_states.is_permission_refusal` used to read SQLSTATE only, so the three
drivers that report none -- `pytds`, `oracledb`, `google-api-core` -- recorded every
refusal as UNAVAILABLE. Each does carry the vendor's own code as a *field*: SQL Server's
error `number`, Oracle's `_Error.code`, BigQuery's HTTP status with a structured
`reason`. A numeric code the server assigned is as honest a signal as SQLSTATE, and
reading it is still never reading the message, which can quote a value (INV-6).

Every exception here is built with the installed driver's own error class, carrying the
code that vendor really sends, and each positive case has a negative twin: a code that
means something else, a code the vendor uses for both "missing" and "hidden", a message
that *says* refusal with no code behind it, and the same number on an exception from a
different driver.

Live evidence: SQL Server's 229 is proven against the real sample container in
`tests/test_facet_refusal_sqlserver_live.py`. Oracle, Snowflake, BigQuery and Databricks
have no live instance here; for them these tests are the evidence, against the drivers'
own classes.
"""

from __future__ import annotations

import pytest

from aida.capability_states import (
    BIGQUERY_REFUSAL_REASONS,
    HIDDEN_RELATION_ORACLE_ERRORS,
    HIDDEN_RELATION_SNOWFLAKE_ERRORS,
    ORACLE_PRIVILEGE_ERRORS,
    SQLSERVER_PRIVILEGE_ERRORS,
    CapabilityState,
    is_permission_refusal,
)
from aida.connectors.discovery import classify_read_failure

# ---------------------------------------------------------------------------
# SQL Server / pytds -- `number`, the TDS ERROR token's sys.messages id.
# ---------------------------------------------------------------------------


def _pytds(number: int, text: str = "server message quoting 'secret-value-42'") -> Exception:
    import pytds

    error = pytds.OperationalError(text)
    error.number = number
    error.msg_no = number
    return error


@pytest.mark.parametrize("number", sorted(SQLSERVER_PRIVILEGE_ERRORS))
def test_every_sql_server_permission_number_is_a_refusal(number: int) -> None:
    assert is_permission_refusal(_pytds(number)) is True


@pytest.mark.parametrize(
    ("number", "why"),
    [
        (208, "invalid object name -- a genuinely missing object"),
        (1088, "'does not exist or you do not have permissions' -- the server will not say"),
        (15151, "'does not exist or you do not have permission' -- the same refusal to say"),
        (18456, "login failed -- also a wrong password, like SQLSTATE 28P01"),
        (4060, "cannot open database -- also a database that is not there"),
        (1205, "deadlock victim -- transient"),
        (0, "a client-side pytds error carries no server number"),
    ],
)
def test_a_sql_server_number_that_does_not_only_mean_refused_is_not_one(
    number: int, why: str
) -> None:
    assert is_permission_refusal(_pytds(number)) is False, why


def test_the_sql_server_set_is_exactly_the_numbers_checked_against_sys_messages() -> None:
    """Pinned so widening it is a decision someone writes down, not a drive-by."""
    assert SQLSERVER_PRIVILEGE_ERRORS == frozenset({229, 230, 262, 297, 300, 916, 15247})


# ---------------------------------------------------------------------------
# Oracle / python-oracledb -- `_Error.code`, the ORA number.
# ---------------------------------------------------------------------------


def _ora(code: int, text: str) -> Exception:
    from oracledb import errors as oracledb_errors

    error = oracledb_errors._Error(text, code=code)
    return error.exc_type(error)


def test_ora_01031_and_ora_01045_are_refusals() -> None:
    assert ORACLE_PRIVILEGE_ERRORS == frozenset({1031, 1045})
    assert is_permission_refusal(_ora(1031, "ORA-01031: insufficient privileges")) is True
    assert (
        is_permission_refusal(_ora(1045, "ORA-01045: user lacks CREATE SESSION privilege"))
        is True
    )


def test_ora_00942_is_a_refusal_only_when_the_relation_is_known_to_exist() -> None:
    """The ORA-00942 decision, both halves. Oracle answers "table or view does not
    exist" to a login with no privilege on an object -- and to a query naming a table
    that really is not there. From the code alone the two cannot be told apart, so it is
    not a refusal; a caller whose statement names only relations the engine always has
    (an `ALL_*` dictionary view) rules the second reading out, and then it is."""
    hidden = _ora(942, "ORA-00942: table or view does not exist")
    assert HIDDEN_RELATION_ORACLE_ERRORS == frozenset({942})
    assert is_permission_refusal(hidden) is False
    assert is_permission_refusal(hidden, known_relations=True) is True
    assert classify_read_failure(hidden)[0] is CapabilityState.UNAVAILABLE
    assert (
        classify_read_failure(hidden, known_relations=True)[0]
        is CapabilityState.PERMISSION_DENIED
    )


@pytest.mark.parametrize(
    ("code", "why"),
    [
        (1017, "invalid username/password -- a credential, not a refusal"),
        (28000, "the account is locked -- a state of the account"),
        (3113, "end-of-file on communication channel -- transport"),
        (4043, "object does not exist -- not a conflated code"),
    ],
)
def test_an_ora_code_that_is_not_a_refusal_is_not_one(code: int, why: str) -> None:
    assert is_permission_refusal(_ora(code, f"ORA-{code:05}: x"), known_relations=True) is False, (
        why
    )


def test_an_oracle_message_that_says_refused_is_not_read() -> None:
    """`full_code` is sliced out of the message text by the driver, and the message is
    never read -- so an error with no structured code is not a refusal, whatever it says."""
    import oracledb

    worded = oracledb.DatabaseError("ORA-01031: insufficient privileges")
    assert is_permission_refusal(worded, known_relations=True) is False


# ---------------------------------------------------------------------------
# Snowflake -- SQLSTATE where it sends one, errno 2003 where it conflates.
# ---------------------------------------------------------------------------


def _snowflake(errno: int, sqlstate: str) -> Exception:
    from snowflake.connector import errors as snowflake_errors

    return snowflake_errors.ProgrammingError(msg="x", errno=errno, sqlstate=sqlstate)


def test_snowflake_2003_is_a_refusal_only_when_the_object_is_known_to_exist() -> None:
    hidden = _snowflake(2003, "02000")
    assert HIDDEN_RELATION_SNOWFLAKE_ERRORS == frozenset({2003})
    assert is_permission_refusal(hidden) is False
    assert is_permission_refusal(hidden, known_relations=True) is True
    assert is_permission_refusal(_snowflake(3001, "42501")) is True
    assert is_permission_refusal(_snowflake(2043, "02000"), known_relations=True) is False


# ---------------------------------------------------------------------------
# BigQuery / google-api-core -- HTTP 403 *and* the structured reason.
# ---------------------------------------------------------------------------


def _bigquery(status: int, reason: str | None) -> Exception:
    from google.api_core import exceptions as google_exceptions

    errors = [{"reason": reason, "message": "x"}] if reason is not None else []
    return google_exceptions.from_http_status(status, "x", errors=errors)


def test_a_403_with_reason_access_denied_is_a_refusal() -> None:
    assert BIGQUERY_REFUSAL_REASONS == frozenset({"accessDenied"})
    assert is_permission_refusal(_bigquery(403, "accessDenied")) is True


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "quotaExceeded"),
        (403, "billingNotEnabled"),
        (403, "policyViolation"),
        (403, "responseTooLarge"),
        (403, None),
        (404, "notFound"),
        (401, "accessDenied"),
        (500, "backendError"),
    ],
)
def test_a_bigquery_error_that_is_not_a_denial_is_not_one(status: int, reason: str | None) -> None:
    assert is_permission_refusal(_bigquery(status, reason)) is False


# ---------------------------------------------------------------------------
# Across drivers.
# ---------------------------------------------------------------------------


def test_a_vendor_code_is_read_only_from_its_own_driver() -> None:
    """229 is SQL Server's and 403 is an HTTP status. The same number on an exception
    from any other package means nothing, so it is not read there."""

    class Foreign(Exception):
        number = 229
        code = 403
        errno = 2003
        errors = [{"reason": "accessDenied"}]

    assert is_permission_refusal(Foreign("x"), known_relations=True) is False


def test_a_wrapped_driver_refusal_is_still_found() -> None:
    """Walks the chain exactly as it does for SQLSTATE: `raise ... from` and `.orig`."""
    try:
        try:
            raise _pytds(229)
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_permission_refusal(outer) is True


def test_nothing_about_the_refusal_reaches_the_recorded_outcome() -> None:
    """INV-6: what is recorded is a state and a reason code from closed vocabularies."""
    state, reason = classify_read_failure(_pytds(229, "denied on 'ssn=123-45-6789'"))
    assert state is CapabilityState.PERMISSION_DENIED
    assert "123-45-6789" not in reason
    assert reason == "SOURCE_DENIED_READ"
