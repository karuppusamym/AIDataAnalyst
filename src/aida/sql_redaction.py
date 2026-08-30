"""Literal-free SQL for storage, and a fingerprint of what was removed.

Extracted from `aida.query_gateway` so that the ingestion path can use it without an
L1-imports-L3 edge. That edge is the same layering mistake `10-architecture/04` §5.3
records: the gateway is runtime, ingestion is foundation, and foundation must not reach
upward. A shared leaf module is the fix rather than the import.

**Why persisted SQL is redacted at all.** INV-6 says source values do not enter platform
tables. A SQL statement carries values in its literals -- `WHERE ssn = '123-45-6789'` is a
source value written in a different syntax, and storing the statement stores the value.
The project already decided this for dbt (`dbt_artifacts.py` keeps
`compiled_sql_hash` + `compiled_sql_redacted` and never the raw artifact); this module is
that same decision made reusable, after view definitions and routine bodies were briefly
stored raw.

**What redaction costs, stated honestly.** Lineage does not depend on literal values, so
the main consumer loses nothing: `SELECT a FROM t WHERE x = :redacted` parses to the same
column graph as the original. What is lost is the ability to read a filter predicate later
-- "this view excludes test accounts" is visible in the raw text and not in the redacted
one. That is a real cost to business-meaning inference, accepted deliberately, because the
alternative is a control plane holding account numbers in view DDL.

The fingerprint exists so that "did this definition change?" stays answerable without
keeping the thing that changed.
"""

import hashlib
import re
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError


@dataclass(frozen=True, slots=True)
class RedactedSql:
    """The storable form of a SQL statement.

    `status` distinguishes the two ways redaction can end, which callers must not
    conflate:

    * `PARSED`  -- the statement parsed and every literal node was replaced. Precise.
    * `LEXICAL` -- the statement did not parse, so literals were removed by scanning the
      text instead. Less precise, and still safe to store.
    * `UNPARSED` -- neither worked, so **no text is returned at all**.

    The `LEXICAL` tier exists because fail-closed alone would have destroyed the point of
    envelope 1.1. Stored procedure bodies frequently do not parse -- `BEGIN ... END` blocks
    are procedural, not a single statement, and every dialect spells them differently --
    so "store nothing unless it parses" would discard most procedure bodies, and with them
    procedure lineage, which is one of the few capabilities no competitor offers.

    Removing literals does not actually require a parse: string and numeric literals are
    lexically identifiable, and identifiers, keywords and operators -- everything a later
    parser needs -- survive untouched. So the fallback keeps the structure and drops the
    values, which is the property that matters.
    """

    status: str
    redacted: str | None
    fingerprint: str


def sql_fingerprint(sql: str) -> str:
    """A stable digest of the original text, for change detection only.

    Unkeyed on purpose, unlike `query_gateway.audit_sql_hash`: this is not evidence of
    what ran, it is a "has the definition changed since the last scan" marker, and it
    needs to compare equal across environments that do not share an audit key.
    """
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def redact_sql_literals(sql: str, *, dialect: str) -> str:
    """Replace every literal with a placeholder. Raises if the statement will not parse."""
    statement = parse_one(sql, read=dialect)
    redacted = statement.transform(
        lambda node: exp.Placeholder(this="redacted") if isinstance(node, exp.Literal) else node
    )
    return redacted.sql(dialect=dialect, pretty=True)


#: Single-quoted SQL string literals, including doubled-quote escapes. Deliberately does
#: not touch double-quoted or bracketed text: in SQL those are identifiers, not values,
#: and scrubbing them would destroy exactly the structure a later parser needs.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
#: Numeric literals. Scrubbed as well as strings, because an account number or an
#: identifier is as likely to appear unquoted as quoted. The cost is that `LIMIT 100` and
#: `varchar(50)` lose their numbers too -- accepted, because the alternative is guessing
#: which numbers are values.
_NUMERIC_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")

_PLACEHOLDER = ":redacted"


def scrub_literals_lexically(sql: str) -> str:
    """Remove literals without parsing. Structure survives; values do not."""
    without_strings = _STRING_LITERAL.sub(_PLACEHOLDER, sql)
    return _NUMERIC_LITERAL.sub(_PLACEHOLDER, without_strings)


def redact_for_storage(sql: str | None, *, dialect: str) -> RedactedSql | None:
    """Prepare source-supplied SQL for persistence. `None` in, `None` out.

    Tries a real parse first, because node-level replacement is precise. Falls back to a
    lexical scrub rather than to storing nothing, so that unparseable procedure bodies are
    still usable later. What it never does is return the raw text: "keep the original when
    we cannot parse it" inverts the safety property exactly when it matters most, since an
    unparseable statement is the one most likely to contain something unusual.
    """
    if sql is None:
        return None
    fingerprint = sql_fingerprint(sql)
    if not sql.strip():
        return RedactedSql(status="PARSED", redacted="", fingerprint=fingerprint)
    try:
        return RedactedSql(
            status="PARSED",
            redacted=redact_sql_literals(sql, dialect=dialect),
            fingerprint=fingerprint,
        )
    except (ParseError, TokenError, ValueError, RecursionError):
        pass
    try:
        return RedactedSql(
            status="LEXICAL", redacted=scrub_literals_lexically(sql), fingerprint=fingerprint
        )
    except (re.error, RecursionError):
        return RedactedSql(status="UNPARSED", redacted=None, fingerprint=fingerprint)
