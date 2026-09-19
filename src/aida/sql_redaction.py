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

**A parse is not proof that every value was seen (2026-09-15).** The node-level pass
replaces `exp.Literal` nodes, and sqlglot does not always put values in one. A statement
it cannot read becomes an opaque `Command` holding the raw text; a PostgreSQL or Snowflake
routine body (`AS $$ ... $$`) becomes a `Heredoc` or a `Block`; a BigQuery JavaScript body
a `RawString`. Each of those rendered its values back verbatim while the row was labelled
`PARSED` -- every dollar-quoted PostgreSQL routine ingested before this date stored its
literals. So `PARSED` now also requires that a value-aware lexical scan of the rendered
text finds nothing left to remove; otherwise the text is scrubbed lexically and labelled
`LEXICAL`. Rows stored before the fix are repaired by `scripts/reredact_stored_sql.py`.

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

    * `PARSED`  -- the statement parsed, every literal node was replaced, and nothing
      value-shaped survived outside a literal node. Precise.
    * `LEXICAL` -- the statement did not parse, or parsed into nodes that hide values
      from the node-level pass, so literals were removed by scanning the text instead.
      Less precise, and still safe to store.
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


#: Statuses whose stored text is value-free. A reader that needs the text's *structure*
#: -- a lineage parser, a reviewer reading a routine -- may use either; `UNPARSED` stores
#: nothing to read.
VALUE_FREE_REDACTION_STATUSES: frozenset[str] = frozenset({"PARSED", "LEXICAL"})


def sql_fingerprint(sql: str) -> str:
    """A stable digest of the original text, for change detection only.

    Unkeyed on purpose, unlike `query_gateway.audit_sql_hash`: this is not evidence of
    what ran, it is a "has the definition changed since the last scan" marker, and it
    needs to compare equal across environments that do not share an audit key.
    """
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def redact_sql_literals(sql: str, *, dialect: str) -> str:
    """Replace every literal with a placeholder. Raises if the statement will not parse.

    The result is value-free even when the parse hid values from the node-level pass:
    that text is scrubbed lexically instead of being returned as it was.
    """
    precise = _redact_precisely(sql, dialect=dialect)
    return precise if precise is not None else scrub_literals_lexically(sql, dialect=dialect)


def _redact_precisely(sql: str, *, dialect: str) -> str | None:
    """Node-level redaction, or None when the parse did not expose every value.

    Raises `ParseError`/`TokenError` when the statement does not parse at all.
    """
    statement = parse_one(sql, read=dialect)
    if _has_quoted_routine_body(statement):
        # `CREATE FUNCTION ... AS '<body>'`: the program is held in a string literal.
        # Replacing that literal would store a placeholder where the routine was, so the
        # lexical pass keeps the program and removes the values inside it.
        return None
    if statement is not None and any(isinstance(node, exp.Command) for node in statement.walk()):
        # A `Command` is text sqlglot could not model, and its re-render is not
        # guaranteed to be the text it was built from. This is the rule the module
        # docstring already states ("`Command` ... text is scrubbed lexically and labelled
        # `LEXICAL`"), but it was only enforced indirectly, through the value scan below --
        # and a render that *drops* text passes a value scan trivially, because text that
        # is gone contains no values.
        #
        # Found 2026-09-19: T-SQL `END EXEC(@sql);` with no semicolon after `END` parses as
        # one `Command` that re-renders as just `END`, so the stored body lost its dynamic
        # SQL and the lineage parser reported a fully understood routine whose real
        # behaviour is decided at runtime. BigQuery and Snowflake `EXECUTE IMMEDIATE` take
        # the same path. Measured over the adversarial corpus before this line was added:
        # it moves exactly 2 of 104 PARSED bodies to LEXICAL, both `EXECUTE IMMEDIATE`, and
        # nothing else -- a check keyed on lost words instead flipped 16, mostly cosmetic.
        #
        # Returning None costs precision, never value-freedom: the caller falls back to the
        # lexical scrub, and LEXICAL is in `VALUE_FREE_REDACTION_STATUSES`.
        return None
    redacted = statement.transform(
        lambda node: exp.Placeholder(this="redacted") if isinstance(node, exp.Literal) else node
    ).sql(dialect=dialect, pretty=True)
    if contains_value_shaped_text(redacted, dialect=dialect):
        return None
    return redacted


def _has_quoted_routine_body(statement: exp.Expr | None) -> bool:
    if not isinstance(statement, exp.Create):
        return False
    kind = str(statement.args.get("kind") or "").upper()
    body = statement.expression
    return kind in {"FUNCTION", "PROCEDURE"} and isinstance(body, exp.Literal) and body.is_string


#: Numeric literals. Scrubbed as well as strings, because an account number or an
#: identifier is as likely to appear unquoted as quoted. The cost is that `LIMIT 100` and
#: `varchar(50)` lose their numbers too -- accepted, because the alternative is guessing
#: which numbers are values. A PostgreSQL positional parameter (`$1`) is not a value.
_NUMERIC_LITERAL = re.compile(r"(?<![$\w])\d+(?:\.\d+)?\b")
_DOLLAR_QUOTE_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_ROUTINE_HEADER = re.compile(
    r"^\s*CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:(?:TEMP|TEMPORARY|SECURE)\s+)*"
    r"(?:FUNCTION|PROCEDURE)\b",
    re.IGNORECASE,
)
_DECLARED_LANGUAGE = re.compile(r"\bLANGUAGE\s+'?([A-Za-z0-9_]+)'?", re.IGNORECASE)
#: Routine languages that quote the SQL way. Any other declared language -- JavaScript,
#: Python, Java, C -- also quotes values with double quotes and backticks.
_SQL_LANGUAGES = frozenset({"sql", "plpgsql"})
#: Dialects in which double-quoted text is a string value, not an identifier.
_DOUBLE_QUOTED_VALUE_DIALECTS = frozenset({"bigquery", "databricks", "spark", "hive", "mysql"})
#: Dialects in which backticks quote identifiers.
_BACKTICK_IDENTIFIER_DIALECTS = frozenset({"bigquery", "databricks", "spark", "hive", "mysql"})

_PLACEHOLDER = ":redacted"


@dataclass(frozen=True, slots=True)
class _Quoting:
    double_quoted_is_value: bool
    backtick_is_value: bool
    brackets_are_identifiers: bool


def _quoting_for(dialect: str | None) -> _Quoting:
    return _Quoting(
        double_quoted_is_value=dialect in _DOUBLE_QUOTED_VALUE_DIALECTS,
        backtick_is_value=False,
        brackets_are_identifiers=dialect == "tsql",
    )


_FOREIGN_LANGUAGE_QUOTING = _Quoting(
    double_quoted_is_value=True, backtick_is_value=True, brackets_are_identifiers=False
)


def scrub_literals_lexically(sql: str, *, dialect: str | None = None) -> str:
    """Remove literals without parsing. Structure survives; values do not.

    Quoted text is read the way the dialect reads it: a single-quoted string is a value,
    a double-quoted one an identifier except where the dialect makes it a string too.
    Dollar-quoted text is a routine body when `AS` or `DO` introduces it -- program text
    with values of its own, so it is scrubbed rather than dropped -- and a value anywhere
    else. So is the single-quoted body of `CREATE FUNCTION ... AS '<body>'`. A body in a
    non-SQL language has its double-quoted and backtick strings removed as well.
    """
    return _scrub_statement(sql, dialect=dialect, comment_numbers=True)


def contains_value_shaped_text(sql: str, *, dialect: str | None = None) -> bool:
    """True if a lexical scrub would still remove something from `sql`.

    Comments are not counted: the node-level pass keeps them, prompt-risk screening reads
    them, and a digit in `-- v2 of the report` is not what this check is for.
    """
    return _scrub_statement(sql, dialect=dialect, comment_numbers=False) != sql


def _scrub_statement(sql: str, *, dialect: str | None, comment_numbers: bool) -> str:
    quoting = _quoting_for(dialect)
    language_match = _DECLARED_LANGUAGE.search(sql)
    language = language_match.group(1).lower() if language_match else None
    body_quoting = (
        _FOREIGN_LANGUAGE_QUOTING if language is not None and language not in _SQL_LANGUAGES
        else quoting
    )
    return _scrub(
        sql,
        quoting,
        body_quoting,
        quoted_body_allowed=bool(_ROUTINE_HEADER.match(sql)) and dialect != "tsql",
        comment_numbers=comment_numbers,
    )


def _scrub(
    text: str,
    quoting: _Quoting,
    body_quoting: _Quoting,
    *,
    quoted_body_allowed: bool,
    comment_numbers: bool,
) -> str:
    out: list[str] = []
    run_start = 0
    i, n = 0, len(text)

    def flush(end: int) -> None:
        out.append(_NUMERIC_LITERAL.sub(_PLACEHOLDER, text[run_start:end]))

    def scrub_body(body: str) -> str:
        return _scrub(
            body, body_quoting, body_quoting, quoted_body_allowed=False,
            comment_numbers=comment_numbers,
        )

    while i < n:
        ch = text[i]
        if text.startswith("--", i) or text.startswith("/*", i):
            end = _comment_end(text, i)
            flush(i)
            comment = text[i:end]
            out.append(_NUMERIC_LITERAL.sub(_PLACEHOLDER, comment) if comment_numbers else comment)
            i = run_start = end
            continue
        if ch == "$" and (tag := _DOLLAR_QUOTE_TAG.match(text, i)):
            close = text.find(tag.group(0), tag.end())
            if close != -1:
                flush(i)
                if _introduces_body(text, i):
                    body = scrub_body(text[tag.end() : close])
                    out.append(tag.group(0) + body + tag.group(0))
                else:
                    out.append(_PLACEHOLDER)
                i = run_start = close + len(tag.group(0))
                continue
        if ch == "'":
            end = _quoted_end(text, i, "'", backslash=_is_escape_string(text, i))
            flush(i)
            if quoted_body_allowed and _introduces_body(text, i):
                body = scrub_body(text[i + 1 : end - 1].replace("''", "'"))
                out.append("'" + body.replace("'", "''") + "'")
                quoted_body_allowed = False
            else:
                out.append(_PLACEHOLDER)
            i = run_start = end
            continue
        if ch == '"' or ch == "`" or (ch == "[" and quoting.brackets_are_identifiers):
            close_char = "]" if ch == "[" else ch
            is_value = (
                quoting.double_quoted_is_value if ch == '"'
                else quoting.backtick_is_value if ch == "`"
                else False
            )
            end = _quoted_end(text, i, close_char, backslash=is_value)
            flush(i)
            # An identifier is kept verbatim -- digits in `"sales_2024"` are a name.
            out.append(_PLACEHOLDER if is_value else text[i:end])
            i = run_start = end
            continue
        i += 1
    flush(n)
    return "".join(out)


def _comment_end(text: str, start: int) -> int:
    if text.startswith("--", start):
        newline = text.find("\n", start)
        return len(text) if newline == -1 else newline
    close = text.find("*/", start + 2)
    return len(text) if close == -1 else close + 2


def _quoted_end(text: str, start: int, close: str, *, backslash: bool) -> int:
    """Index just past the quoted span opened at `start`; the end of `text` if unclosed."""
    j, n = start + 1, len(text)
    while j < n:
        if backslash and text[j] == "\\":
            j += 2
            continue
        if text[j] == close:
            if close != "]" and text.startswith(close * 2, j):
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _is_escape_string(text: str, quote: int) -> bool:
    """PostgreSQL `E'...'`, where a backslash escapes the quote."""
    return (
        quote >= 1
        and text[quote - 1] in "Ee"
        and (quote == 1 or not (text[quote - 2].isalnum() or text[quote - 2] == "_"))
    )


def _introduces_body(text: str, position: int) -> bool:
    """Whether the keyword just before `position` is `AS` or `DO` -- a routine body."""
    j = position - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 1 or text[j - 1 : j + 1].upper() not in {"AS", "DO"}:
        return False
    return j < 2 or not (text[j - 2].isalnum() or text[j - 2] == "_")


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
        precise = _redact_precisely(sql, dialect=dialect)
    except (ParseError, TokenError, ValueError, RecursionError):
        precise = None
    if precise is not None:
        return RedactedSql(status="PARSED", redacted=precise, fingerprint=fingerprint)
    try:
        return RedactedSql(
            status="LEXICAL",
            redacted=scrub_literals_lexically(sql, dialect=dialect),
            fingerprint=fingerprint,
        )
    except (re.error, RecursionError):
        return RedactedSql(status="UNPARSED", redacted=None, fingerprint=fingerprint)
