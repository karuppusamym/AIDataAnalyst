"""One vocabulary for "can Atlas do this, here, to this kind of object?".

Review 2026-09-16 §5 asks for a per-engine capability matrix that
distinguishes `unsupported`, `not applicable`, `permission denied`,
`unavailable`, `truncated` and `unresolved`. Before this module the answer
was spread over five separate enumerations and two booleans, each honest on
its own and none able to express the others:

* `discovery_selection.CapabilityStatus` -- SUPPORTED / UNSUPPORTED /
  NOT_APPLICABLE, per object kind, for two facets;
* `discovery_receipt`'s per-facet `support` -- SUPPORTED / UNSUPPORTED only,
  with truncation as a *count* beside it;
* `envelope_models`' AVAILABLE / UNAVAILABLE, with `truncated` a separate
  boolean and `unavailable_reason` free text;
* `procedure_capability_matrix.ConstructRow`'s parser statuses;
* `procedure_lineage.UnparsedReason`, plus the callee-descent reasons and the
  footprint gap kinds.

`PERMISSION_DENIED` existed in none of them: a facet whose read the source
refused failed its whole run rather than being recorded (tracker R11-FP01,
R11-FP02). This module is the shared answer vocabulary those surfaces map
onto at their *reporting* boundary.

**Two different facts, deliberately in one vocabulary.** The architecture
document is explicit that "installed adapter support and the current
connection's permissions are different facts"
(`Docs/10-architecture/20-database-footprint-and-agent-context.md` §4.1), so
`ADAPTER_STATES` and `READ_OUTCOME_STATES` below name which states may answer
which question. An adapter is never `PERMISSION_DENIED` (a *login* is), and
one read is never `UNSUPPORTED` (an *adapter* is).

**What is deliberately absent.** The design target's sixth presentation
outcome, `VERIFIED`, is not a state here. `SUPPORTED` in this vocabulary
means "this code path exists and is reached"; it does not claim the path has
been exercised against a live instance of that engine. Collapsing the two
would reintroduce exactly the "architecture can represent it" / "support is
verified" conflation review §5 names. Live-validation evidence stays where it
is dated: `Docs/60-delivery/20-capability-register.md`, and the engine matrix
carries it as its own separate column rather than as a state.

**The boolean rule, which this module must not break.** Two booleans in
storage are load-bearing and stay booleans:
`procedure_lineage_models.DeepProcedureLineageEdge.source_resolved` (whose
comment records that an unresolved source is "never inferred by
string-comparing `source_table` against a cosmetic sentinel like
`\"UNRESOLVED\"`") and the `truncated` flag on a captured definition or body.
`UNRESOLVED` and `TRUNCATED` exist here as *report* states only, produced by
`resolution_state` and `definition_read_state` at the moment of answering a
reader. Nothing in this module writes a state into storage, and no caller
should: a stored sentinel string can collide with a real name, a stored
boolean cannot.

**Not to be confused with** `aida.connectors.base`'s `FACET_*` constants,
which name why one *column profile* facet (distinct counts, length buckets)
is missing for one column. That is a different axis at a different grain; the
overlapping spellings are the same English words, not the same vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class CapabilityState(StrEnum):
    """What Atlas can say about one capability facet of one object kind."""

    #: The code path exists, is reached, and returns the whole fact. Not a
    #: claim that it has been exercised against a live instance -- see the
    #: module docstring on `VERIFIED`.
    SUPPORTED = "SUPPORTED"
    #: Implemented, and known to return only part of the fact -- an explicitly
    #: degrading parser, value-free statistics without ranges, a bounded
    #: enumeration. The honest answer for anything F06.4 warns against
    #: labelling "understood" merely because it was inventoried.
    PARTIAL = "PARTIAL"
    #: The engine has the concept; Atlas has not implemented reading it.
    UNSUPPORTED = "UNSUPPORTED"
    #: The engine has no such concept, so there is nothing to implement. A
    #: different answer from UNSUPPORTED, and the one the UI must use to keep
    #: from offering an Oracle package on a source that has no packages.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Supported and applicable, and this scan's own selection left it out.
    NOT_SELECTED = "NOT_SELECTED"
    #: The source refused this read for this login. Asking again with other
    #: credentials could succeed, which is what separates it from UNSUPPORTED.
    PERMISSION_DENIED = "PERMISSION_DENIED"
    #: Supported, applicable, selected, permitted -- and this read did not get
    #: it: the source returned no text, the query failed, the fact lives
    #: somewhere Atlas did not capture.
    UNAVAILABLE = "UNAVAILABLE"
    #: Part of the text arrived and part did not, so anything derived from it
    #: is incomplete by construction.
    TRUNCATED = "TRUNCATED"
    #: The fact arrived but could not be tied to a named object -- a lineage
    #: source sqlglot could not resolve, a callee not captured here.
    UNRESOLVED = "UNRESOLVED"


#: States that may answer "what does the installed adapter implement?".
ADAPTER_STATES: Final[frozenset[CapabilityState]] = frozenset(
    {
        CapabilityState.SUPPORTED,
        CapabilityState.PARTIAL,
        CapabilityState.UNSUPPORTED,
        CapabilityState.NOT_APPLICABLE,
    }
)

#: States that may answer "what did this read, with this login, actually get?".
READ_OUTCOME_STATES: Final[frozenset[CapabilityState]] = frozenset(
    {
        CapabilityState.SUPPORTED,
        CapabilityState.PARTIAL,
        CapabilityState.NOT_APPLICABLE,
        CapabilityState.NOT_SELECTED,
        CapabilityState.PERMISSION_DENIED,
        CapabilityState.UNAVAILABLE,
        CapabilityState.TRUNCATED,
        CapabilityState.UNRESOLVED,
    }
)

#: Every state, as the plain strings an API contract and a published document use.
CAPABILITY_STATE_VALUES: Final[tuple[str, ...]] = tuple(state.value for state in CapabilityState)


# ---------------------------------------------------------------------------
# Why an answer is not a plain SUPPORTED, as a closed vocabulary.
#
# INV-6, and the same reasoning `connectors.base.FACET_REASON_CODES` gives for
# its own set: the natural implementation of a reason is to pass the driver's
# message through, and a driver's message routinely quotes the offending row.
# A code cannot carry a value.
# ---------------------------------------------------------------------------
#: The engine has no such object kind or facet at all.
REASON_ENGINE_LACKS_CONCEPT: Final = "ENGINE_LACKS_CONCEPT"
#: The engine has it; this adapter does not read it yet.
REASON_ADAPTER_NOT_IMPLEMENTED: Final = "ADAPTER_NOT_IMPLEMENTED"
#: No native adapter exists for this engine at all (registry `PLANNED`).
REASON_ADAPTER_NOT_CERTIFIED: Final = "ADAPTER_NOT_CERTIFIED"
#: This scan's discovery selection excluded it.
REASON_SELECTION_EXCLUDED: Final = "SELECTION_EXCLUDED"
#: The source refused the read for this login.
REASON_SOURCE_DENIED_READ: Final = "SOURCE_DENIED_READ"
#: The source returned no text where text was expected.
REASON_SOURCE_RETURNED_NO_TEXT: Final = "SOURCE_RETURNED_NO_TEXT"
#: The source returned part of the text.
REASON_SOURCE_TEXT_TRUNCATED: Final = "SOURCE_TEXT_TRUNCATED"
#: The facet's own query failed for a reason that is not a refusal.
REASON_FACET_QUERY_FAILED: Final = "FACET_QUERY_FAILED"
#: The text is held somewhere else Atlas captured instead (an Oracle package
#: member's source is its package's).
REASON_DEFINITION_HELD_BY_CONTAINER: Final = "DEFINITION_HELD_BY_CONTAINER"
#: The parser recognises the construct and refuses to resolve it, producing an
#: explicit marker (`procedure_lineage.UnparsedReason`) rather than a guess.
REASON_PARSER_DEGRADES_EXPLICITLY: Final = "PARSER_DEGRADES_EXPLICITLY"
#: The parser will not attempt this dialect at all.
REASON_PARSER_REFUSES_DIALECT: Final = "PARSER_REFUSES_DIALECT"
#: A name arrived that could not be resolved to a catalog object.
REASON_NAME_NOT_RESOLVED: Final = "NAME_NOT_RESOLVED"
#: The generator refuses this shape before reading it.
REASON_CANDIDATE_SHAPE_REFUSED: Final = "CANDIDATE_SHAPE_REFUSED"
#: Execution is refused because the connector cannot cost the statement first.
REASON_NO_QUERY_ESTIMATE: Final = "NO_QUERY_ESTIMATE"
#: A reason arrived that is not in this vocabulary. The reason is dropped; the
#: fact that the answer is not SUPPORTED is kept.
REASON_UNRECORDED: Final = "UNRECORDED"

CAPABILITY_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_ENGINE_LACKS_CONCEPT,
        REASON_ADAPTER_NOT_IMPLEMENTED,
        REASON_ADAPTER_NOT_CERTIFIED,
        REASON_SELECTION_EXCLUDED,
        REASON_SOURCE_DENIED_READ,
        REASON_SOURCE_RETURNED_NO_TEXT,
        REASON_SOURCE_TEXT_TRUNCATED,
        REASON_FACET_QUERY_FAILED,
        REASON_DEFINITION_HELD_BY_CONTAINER,
        REASON_PARSER_DEGRADES_EXPLICITLY,
        REASON_PARSER_REFUSES_DIALECT,
        REASON_NAME_NOT_RESOLVED,
        REASON_CANDIDATE_SHAPE_REFUSED,
        REASON_NO_QUERY_ESTIMATE,
        REASON_UNRECORDED,
    }
)


def reason_code(code: str | None) -> str:
    """`code` if it is in the closed vocabulary, else `REASON_UNRECORDED`.

    The one door a reason passes through before it is stored or published, so
    free text -- a driver's message, a source's own explanation of why it
    withheld something -- can never become one (INV-6).
    """
    if code is None:
        return REASON_UNRECORDED
    return code if code in CAPABILITY_REASON_CODES else REASON_UNRECORDED


# ---------------------------------------------------------------------------
# The reporting boundary. Each function below turns storage's own honest
# representation -- a flag, a pair of counters, an availability string -- into
# a state, at the moment of answering. None of them writes anything.
# ---------------------------------------------------------------------------


def flag_state(flag: bool | None) -> CapabilityState:
    """A connector capability flag as a state.

    `None` is `UNSUPPORTED`, not `NOT_APPLICABLE`: a flag the connector never
    declared reads as "absent", and INV-9 makes absent mean not implemented.
    Deciding that an engine has no such concept needs engine knowledge this
    function does not have, and the caller that has it says so itself.
    """
    return CapabilityState.SUPPORTED if flag else CapabilityState.UNSUPPORTED


def definition_read_state(
    *, available: bool, truncated: bool, denied: bool = False
) -> CapabilityState:
    """The state of one captured definition or routine body.

    Takes the booleans storage actually holds -- `availability == AVAILABLE`
    and the `truncated` flag -- rather than a string, so this module stays a
    leaf and so no sentinel is ever written next to the text it describes.
    `denied` is separate because "the source refused this login" is a fact
    about the read, not about the row, and a caller only knows it where a
    refusal was recorded as one.

    Precedence: a refusal outranks everything (nothing arrived), then
    unavailability, then truncation. A truncated definition is `TRUNCATED` and
    not `SUPPORTED`, which is the whole point of keeping the flag.
    """
    if denied:
        return CapabilityState.PERMISSION_DENIED
    if not available:
        return CapabilityState.UNAVAILABLE
    return CapabilityState.TRUNCATED if truncated else CapabilityState.SUPPORTED


def resolution_state(source_resolved: bool) -> CapabilityState:
    """`SUPPORTED` for a resolved lineage source, `UNRESOLVED` otherwise.

    The boolean stays the stored truth. This is the only sanctioned way to
    *report* it as a state, and it exists so that no caller is ever tempted to
    compare a stored name against `"UNRESOLVED"` instead
    (`procedure_lineage_models.DeepProcedureLineageEdge.source_resolved`).
    """
    return CapabilityState.SUPPORTED if source_resolved else CapabilityState.UNRESOLVED


def parse_coverage_state(*, parse_completed: bool, statement_count: int) -> CapabilityState:
    """How completely one routine body was understood.

    F06.4: an object that was inventoried is not therefore understood.
    `SUPPORTED` requires that every statement chunk resolved to a shape or was
    recognised as genuinely lineage-free -- `ProcedureParseResult`'s
    `is_fully_parsed`, which is never inferred from an empty edge list.
    A body with statements and at least one UNPARSED chunk is `PARTIAL`; a
    body with no statements at all is `UNAVAILABLE`, because nothing was read.
    """
    if statement_count <= 0:
        return CapabilityState.UNAVAILABLE
    return CapabilityState.SUPPORTED if parse_completed else CapabilityState.PARTIAL


# ---------------------------------------------------------------------------
# PERMISSION_DENIED, classified from structured codes -- never from prose.
# ---------------------------------------------------------------------------

#: SQLSTATE classes that mean "this login may not", from the SQL standard's own
#: class 42 (syntax error or access rule violation) and class 28
#: (invalid authorization specification). Deliberately narrow: `42501`
#: insufficient_privilege and `28000` invalid_authorization_specification say
#: exactly that, while the broader `42000` also covers a plain syntax error and
#: would make a typo look like a permission problem.
PRIVILEGE_SQLSTATES: Final[frozenset[str]] = frozenset({"42501", "28000"})

#: R11-FP02: SQL Server error numbers that mean "this login may not", as `pytds`
#: reports them on `DatabaseError.number` (the TDS ERROR token's own number
#: field -- the server's `sys.messages.message_id`, not text). Checked against
#: `sys.messages` on the live sample container on 2026-09-18, and chosen the
#: way `PRIVILEGE_SQLSTATES` is: only numbers whose whole meaning is a refusal.
#:
#: * 229 -- the <permission> permission was denied on the object (severity 14,
#:   SQL Server's security class). What a `DENY SELECT` on a catalog view
#:   produces, and the one the live test provokes.
#: * 230 -- the same, on a column.
#: * 262 -- <permission> permission denied in database (e.g. SHOWPLAN).
#: * 297 -- the user does not have permission to perform this action.
#: * 300 -- <permission> permission was denied on object ... database (e.g.
#:   VIEW SERVER STATE, VIEW DATABASE STATE).
#: * 916 -- the server principal is not able to access the database under the
#:   current security context.
#: * 15247 -- user does not have permission to perform this action (the
#:   system-procedure spelling of 297).
#:
#: Deliberately absent: 208 (invalid object name -- a genuinely missing object),
#: 1088 and 15151 ("does not exist *or* you do not have permission" -- SQL
#: Server itself declines to say which, so neither can we), and 18456 / 4060
#: (a failed login, which also covers a wrong password -- the analogue of
#: PostgreSQL's 28P01, which `PRIVILEGE_SQLSTATES` leaves out for the same
#: reason).
SQLSERVER_PRIVILEGE_ERRORS: Final[frozenset[int]] = frozenset(
    {229, 230, 262, 297, 300, 916, 15247}
)

#: R11-FP02: Oracle error numbers that mean "this login may not", as
#: `python-oracledb` reports them on the `_Error` object it raises with
#: (`exc.args[0].code`, an `int` the driver takes from the server's error
#: packet). `full_code` is *not* read: the driver derives it by slicing the
#: message text at its first colon, and this function never reads a message.
#:
#: * 1031 -- ORA-01031 insufficient privileges.
#: * 1045 -- ORA-01045 user lacks CREATE SESSION privilege; logon denied (the
#:   analogue of SQLSTATE 28000, "not permitted to log in").
#:
#: Deliberately absent: 1017 (invalid username/password -- a credential, not a
#: refusal) and 28000 (account locked -- a state of the account).
ORACLE_PRIVILEGE_ERRORS: Final[frozenset[int]] = frozenset({1031, 1045})

#: R11-FP02: codes whose meaning is "this relation does not exist *or* you may
#: not see it" -- the engine deliberately refuses to say which, so from the code
#: alone a refusal cannot be told from a genuinely missing object:
#:
#: * Oracle ORA-00942 (`code == 942`), "table or view does not exist". Oracle
#:   returns it for an object the login holds no privilege on at all, so as not
#:   to disclose that the object exists -- a revoked `ALL_TAB_PRIVS` fails this
#:   way, not with ORA-01031.
#: * Snowflake error 2003 (`errno == 2003`, SQLSTATE 02000), "object does not
#:   exist or not authorized".
#:
#: **The ORA-00942 decision.** Judged on the exception alone, these are *not*
#: refusals: a query naming a table that was dropped, or never existed, gets
#: exactly this code, and calling that PERMISSION_DENIED would send an
#: administrator to grant access on something that is not there. They count as
#: refusals only when the caller states, from its own knowledge of the
#: statement, that every relation it names is one the engine always has
#: (`known_relations=True`) -- an Oracle `ALL_*` dictionary view, a Snowflake
#: `INFORMATION_SCHEMA` view, or an object the same scan has just listed. Then
#: "does not exist" is ruled out by construction and only "you may not see it"
#: is left. That is a deduction from a structured code plus the caller's own
#: SQL, never a reading of the message; a caller that cannot say it keeps the
#: under-claiming answer.
HIDDEN_RELATION_ORACLE_ERRORS: Final[frozenset[int]] = frozenset({942})
HIDDEN_RELATION_SNOWFLAKE_ERRORS: Final[frozenset[int]] = frozenset({2003})

#: R11-FP02: the structured `reason` BigQuery puts on a job or API error when
#: the principal lacks a permission (`errors[i]["reason"]`, mapped to HTTP 403
#: by `google.cloud.bigquery.job.base._ERROR_REASON_TO_EXCEPTION`). A 403 alone
#: is *not* a refusal: BigQuery also answers 403 for `quotaExceeded`,
#: `billingNotEnabled`, `policyViolation`, `blocked` and `responseTooLarge`,
#: none of which a grant would fix. So both the status and this reason are
#: required.
BIGQUERY_REFUSAL_REASONS: Final[frozenset[str]] = frozenset({"accessDenied"})
_HTTP_FORBIDDEN: Final = 403


def _driver_module(exc: BaseException) -> str:
    """The top-level package of the class that raised `exc` -- which driver it is.

    Vendor codes are only meaningful for the vendor that assigned them (`number`
    229 is SQL Server's, `code` 403 is an HTTP status), so each code below is
    read only from an exception its own driver raised. The class's module is a
    structural fact about the exception, not text it carries.
    """
    return type(exc).__module__.partition(".")[0]


def _vendor_code_refusal(exc: BaseException, *, known_relations: bool) -> bool:
    """Whether a driver's own numeric code, read as a field, says "refused"."""
    module = type(exc).__module__
    driver = _driver_module(exc)
    if driver == "pytds":
        number = getattr(exc, "number", None)
        return isinstance(number, int) and number in SQLSERVER_PRIVILEGE_ERRORS
    if driver == "oracledb":
        # `oracledb.DatabaseError(_Error)`: the structured error object is the
        # exception's first argument, and its `code` is the ORA number.
        error = exc.args[0] if exc.args else None
        code = getattr(error, "code", None)
        if not isinstance(code, int) or isinstance(code, bool):
            return False
        if code in ORACLE_PRIVILEGE_ERRORS:
            return True
        return known_relations and code in HIDDEN_RELATION_ORACLE_ERRORS
    if driver == "snowflake":
        errno = getattr(exc, "errno", None)
        return (
            known_relations
            and isinstance(errno, int)
            and errno in HIDDEN_RELATION_SNOWFLAKE_ERRORS
        )
    if module.startswith("google.api_core"):
        if getattr(exc, "code", None) != _HTTP_FORBIDDEN:
            return False
        errors = getattr(exc, "errors", None)
        if not isinstance(errors, list | tuple):
            return False
        return any(
            isinstance(error, Mapping) and error.get("reason") in BIGQUERY_REFUSAL_REASONS
            for error in errors
        )
    return False


def is_permission_refusal(exc: BaseException | None, *, known_relations: bool = False) -> bool:
    """Whether `exc` is the source refusing a read, judged from structured codes alone.

    Walks the exception chain (a driver error is usually wrapped -- SQLAlchemy
    puts the original on `.orig`, and `raise ... from` puts it on `__cause__`)
    looking for a code that means "this login may not":

    * a standard SQLSTATE in `PRIVILEGE_SQLSTATES`, as an attribute (asyncpg,
      snowflake-connector) or in a driver's context mapping (databricks-sql);
    * a vendor's own numeric error code, read as the field the driver reports it
      in -- SQL Server's error number from `pytds` (`SQLSERVER_PRIVILEGE_ERRORS`),
      Oracle's ORA number from `oracledb` (`ORACLE_PRIVILEGE_ERRORS`), and
      BigQuery's HTTP 403 *with* the structured reason `accessDenied`
      (`BIGQUERY_REFUSAL_REASONS`);
    * with `known_relations=True` only, a vendor's "does not exist or not
      authorized" code (`HIDDEN_RELATION_*`) -- see that constant for why the
      caller has to vouch for the relations before it can mean a refusal.

    **Never the message.** A refusal has to be told apart from a failure
    without reading the driver's message, which can quote a value (INV-6) and
    which no two drivers spell the same way. SQLSTATE is one structured field; a
    vendor's numeric error code is another, assigned by the same server, carried
    by the driver as a number rather than as prose, and exactly as honest a
    signal. Before R11-FP02's follow-through this read SQLSTATE only, so `pytds`,
    `oracledb` and `google-api-core` -- which report none -- recorded every
    refusal as UNAVAILABLE. They no longer need to.

    **Where the structured fields cannot tell, the answer stays False.** An
    exception with no recognised code -- a timeout, a dropped connection, a
    driver error built without its code, a 403 for quota -- is not a refusal,
    and is recorded as UNAVAILABLE. That is the under-claiming direction INV-9
    requires: "we did not get it" is honest, while "the source refused you"
    without a code that says so would be a guess at the source's intent.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value in PRIVILEGE_SQLSTATES:
                return True
        # databricks-sql reports a real SQLSTATE, but in a mapping rather than
        # an attribute (`exc.context["sqlState"]`), so reading attributes alone
        # classified a Databricks refusal as UNAVAILABLE while the driver had
        # said 42501 plainly. Still SQLSTATE and nothing else -- only the place
        # this driver puts it.
        context = getattr(current, "context", None)
        if isinstance(context, Mapping):
            for key in ("sqlState", "sqlstate"):
                value = context.get(key)
                if isinstance(value, str) and value in PRIVILEGE_SQLSTATES:
                    return True
        if _vendor_code_refusal(current, known_relations=known_relations):
            return True
        nxt = getattr(current, "orig", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__ or current.__context__
        current = nxt if isinstance(nxt, BaseException) else None
    return False
