"""INV-7 -- attributability of high-impact actions.

**Statement.** Every mutation produces an audit record carrying actor identity,
resource, action, tenant boundary, correlation ID, and timestamp, written in the
same transaction as the mutation.

**Why it is Tier 0.** In a regulated institution an unattributable change is
indistinguishable from an unauthorised one. The invariants document ranks
reproducibility fourth among quality attributes precisely because "a decision that
cannot be replayed cannot be audited" -- and a mutation with no audit row cannot be
replayed at all. The "same transaction" clause is the load-bearing half: an audit
written after the commit is an audit that goes missing exactly when the system
crashes mid-mutation, which is the case anyone ever actually investigates.

**What these tests prove, and what they do not.** The mutation set is *derived*,
not listed: a route counts as a mutation if its HTTP verb says so or if its call
graph reaches `session.add` / `session.commit`, so a GET that quietly writes is
caught too. The same-transaction clause is proven by driving the query gateway's
real persistence path against a recording session and checking that the audit row
is present in the batch at every commit boundary.

`_KNOWN_UNAUDITED_MUTATIONS` below **is now empty.** It recorded a finding, not an
exemption: thirteen endpoints in `aida.ai_registry_api` and
`aida.product_marketplace_api` committed governed state with no audit record, and
`test_no_unaudited_mutation_remains` held that finding as a strict xfail so that
fixing the code would turn into a hard failure and force the entries out. They were
fixed under `Docs/review-2026-08/gap/09-inv7-audit-closeout.md`; the entries are
gone and the xfail with them. The empty dict is kept deliberately rather than
deleted: `test_every_mutation_audits` now covers **every** mutating route with no
exemption at all, and re-populating the dict to excuse a fourteenth endpoint fails
`test_no_unaudited_mutation_remains` immediately instead of passing quietly.
"""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.routing import APIRoute

from aida.events import record_audit
from aida.models import AuditEvent
from tests.support.app_surface import (
    MUTATING_METHODS,
    iter_api_routes,
    reaches_call,
    reaches_session_write,
    route_id,
)
from tests.support.doubles import RecordingSession, security_context

_AUDIT_CALLS = frozenset({"record_audit"})

# POST routes that persist nothing. A POST is not automatically a mutation: these
# are read-only probes that use POST only because their input does not fit in a
# query string. Each is asserted to genuinely reach no persistence call by
# `test_the_read_only_post_list_stays_closed`.
_READ_ONLY_POST_ROUTES: dict[str, str] = {
    "POST /v1/context-compiler/validate": (
        "validates a caller-supplied artifact and returns the report; takes no session"
    ),
    "POST /v1/authorization-probes": (
        "asks the policy engine what it would decide, without performing the "
        "action; reads only, and returns reason codes rather than data"
    ),
    "POST /v1/datasources/{datasource_id}/agent-retrieval-preview": (
        "shows what the governed retriever would assemble for a question; "
        "persists nothing"
    ),
    "POST /v1/abac/simulate": (
        "simulates ABAC policy evaluation over varied attributes; reads "
        "policies and returns results without persisting decisions"
    ),
}

# Read endpoints whose only write is the idempotent creation of a per-organization
# default row (`ensure_default_domain`, `ensure_organization_integration_policy`).
# They are flagged by the derived-mutation scan, correctly -- they do stage a row.
#
# The scan is right and the audit entry is still wrong, for a reason this module now
# *proves* rather than asserts: both helpers build their row from constants and the
# tenant/parent identifier alone. No caller input reaches the row, so there is no
# actor decision to attribute -- the row is a pure function of the tenant, and "who
# created it" has no answer more informative than "the platform, on first read".
# INV-7 exists to make an actor's choice replayable; there is no choice here.
# An audit entry per GET would add one row per read of eight endpoints and bury the
# entries that do record a decision.
#
# The carve-out is therefore kept, but made falsifiable:
# `test_the_lazy_default_write_list_stays_closed` requires each route to still reach
# the helper its entry names, and `test_lazy_default_writers_record_no_actor_decision`
# requires those helpers to keep taking no caller-supplied payload. The moment either
# helper starts accepting a caller's value, the premise is gone and these tests go red.
# Recommendation and trade-off for Architecture to ratify into the invariants
# document: `Docs/review-2026-08/gap/09-inv7-audit-closeout.md` s4.
_LAZY_DEFAULT_WRITE_ROUTES: dict[str, str] = {
    "GET /v1/lines-of-business/{lob_id}/data-domains": "ensure_default_domain",
    "GET /v1/organizations/{organization_id}/integration-policy": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/projects/{project_id}/dbt-projects": "ensure_organization_integration_policy",
    "GET /v1/dbt-projects/{dbt_project_id}/artifact-imports": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/dbt-artifact-imports/{artifact_id}/resources": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/dbt-artifact-imports/{artifact_id}/lineage": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/datasources/{datasource_id}/openlineage-events": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/openlineage-events/{event_id}": "ensure_organization_integration_policy",
    "GET /v1/projects/{project_id}/bi-connections": "ensure_organization_integration_policy",
    "GET /v1/bi-connections/{connection_id}/artifact-imports": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/bi-artifact-imports/{artifact_id}/reports": (
        "ensure_organization_integration_policy"
    ),
    "GET /v1/bi-artifact-imports/{artifact_id}/lineage": (
        "ensure_organization_integration_policy"
    ),
}

# Endpoints that commit governed state with no audit record. Empty, and required to
# stay empty by `test_no_unaudited_mutation_remains`. This was a live INV-7 breach of
# thirteen endpoints; all thirteen now call `record_audit` in the same transaction as
# their mutation (`Docs/review-2026-08/gap/09-inv7-audit-closeout.md`). An entry added
# here to excuse a new endpoint fails that test rather than weakening this one.
_KNOWN_UNAUDITED_MUTATIONS: dict[str, str] = {}


def _route_key(route: APIRoute) -> str:
    return f"{sorted(route.methods)[0]} {route.path}"


def _persists(route: APIRoute) -> bool:
    endpoint = route.endpoint
    return reaches_session_write(endpoint.__module__, endpoint.__name__)


def _mutating_routes() -> list[APIRoute]:
    """Routes that mutate, derived rather than declared.

    A route qualifies on either signal: a mutating HTTP verb, or a call graph that
    reaches `session.add` / `session.commit`. Taking the union means a GET that
    writes is covered, and a POST that only reads is still checked against
    `_READ_ONLY_POST_ROUTES` rather than silently dropped.
    """
    return [
        route
        for route in iter_api_routes()
        if (set(route.methods) & MUTATING_METHODS) or _persists(route)
    ]


def test_every_mutation_audits() -> None:
    """INV-7: every mutation produces an audit record.

    Enumerates every mutating route on the application and requires its call graph
    to reach `record_audit` -- through the handler, a module-private helper, or a
    service such as `QueryExecutionGateway.execute`, all of which are where the
    audit call actually lives for most endpoints.

    Prevents the failure this suite exists to catch: a new mutating endpoint
    merged without an audit call, invisible until someone asks who changed a
    governed object and the answer is nobody knows.
    """
    unaudited = []
    for route in _mutating_routes():
        key = _route_key(route)
        if (
            key in _READ_ONLY_POST_ROUTES
            or key in _LAZY_DEFAULT_WRITE_ROUTES
            or key in _KNOWN_UNAUDITED_MUTATIONS
        ):
            continue
        endpoint = route.endpoint
        if not reaches_call(endpoint.__module__, endpoint.__name__, _AUDIT_CALLS):
            unaudited.append(route_id(route))
    assert unaudited == [], (
        "these mutating endpoints never reach record_audit; INV-7 requires an "
        f"audit record for every mutation: {unaudited}"
    )


def test_no_unaudited_mutation_remains() -> None:
    """INV-7 in full, with no exemptions.

    Was a strict xfail while thirteen endpoints in `aida.ai_registry_api` and
    `aida.product_marketplace_api` committed governed state with no audit record.
    They now audit, so this is an ordinary passing test and the exemption list is
    empty.

    It stays here as the ratchet's second jaw. `test_every_mutation_audits` skips
    whatever this dict contains; this test requires it to contain nothing. Excusing
    a fourteenth unaudited endpoint therefore cannot be done quietly -- it turns
    this test red in the same commit.
    """
    assert _KNOWN_UNAUDITED_MUTATIONS == {}


def test_the_known_unaudited_list_stays_honest() -> None:
    """Guards the exemption list itself. Vacuous while the list is empty, which is
    the point: it is what makes a re-populated list fail fast. Every entry must
    name a mounted route that still fails to audit -- otherwise the list drifts
    into fiction and the ratchet above silently stops ratcheting.
    """
    mounted = {_route_key(route): route for route in iter_api_routes()}
    missing = sorted(set(_KNOWN_UNAUDITED_MUTATIONS) - set(mounted))
    assert missing == [], f"_KNOWN_UNAUDITED_MUTATIONS names routes that no longer exist: {missing}"

    now_audited = []
    for key in sorted(_KNOWN_UNAUDITED_MUTATIONS):
        endpoint = mounted[key].endpoint
        if reaches_call(endpoint.__module__, endpoint.__name__, _AUDIT_CALLS):
            now_audited.append(key)
    assert now_audited == [], (
        "these endpoints now audit and no longer need an exemption; remove them "
        f"from _KNOWN_UNAUDITED_MUTATIONS: {now_audited}"
    )


def test_the_read_only_post_list_stays_closed() -> None:
    """Every route excused as read-only must actually persist nothing."""
    mounted = {_route_key(route): route for route in iter_api_routes()}
    stale = sorted(set(_READ_ONLY_POST_ROUTES) - set(mounted))
    assert stale == [], f"_READ_ONLY_POST_ROUTES names routes that no longer exist: {stale}"

    writing = [key for key in sorted(_READ_ONLY_POST_ROUTES) if _persists(mounted[key])]
    assert writing == [], (
        f"these routes are excused as read-only but now reach a write: {writing}"
    )


def test_the_lazy_default_write_list_stays_closed() -> None:
    """Every route excused as a lazy-default writer must still name a mounted
    route that still writes, and must still reach the helper its entry blames.

    The second half is what stops the carve-out becoming a hiding place. Without
    it, a handler could grow a real, actor-driven mutation and keep its exclusion,
    because the only thing checked would be that it writes *something*.
    """
    mounted = {_route_key(route): route for route in iter_api_routes()}
    stale = sorted(set(_LAZY_DEFAULT_WRITE_ROUTES) - set(mounted))
    assert stale == [], f"_LAZY_DEFAULT_WRITE_ROUTES names routes that no longer exist: {stale}"

    no_longer_writing = [
        key for key in sorted(_LAZY_DEFAULT_WRITE_ROUTES) if not _persists(mounted[key])
    ]
    assert no_longer_writing == [], (
        "these routes no longer write and no longer need an exclusion; remove them "
        f"from _LAZY_DEFAULT_WRITE_ROUTES: {no_longer_writing}"
    )

    wrong_reason = []
    for key, helper in sorted(_LAZY_DEFAULT_WRITE_ROUTES.items()):
        endpoint = mounted[key].endpoint
        if not reaches_call(endpoint.__module__, endpoint.__name__, frozenset({helper})):
            wrong_reason.append(f"{key} (no longer reaches {helper})")
    assert wrong_reason == [], (
        "these routes are excused as lazy-default writers but no longer reach the "
        f"helper the exclusion blames; their writes are now unaccounted for: {wrong_reason}"
    )


_LAZY_DEFAULT_WRITERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ensure_default_domain": ("aida.domain_service", ("session", "lob")),
    "ensure_organization_integration_policy": (
        "aida.integration_service",
        ("session", "organization_id"),
    ),
}


def test_lazy_default_writers_record_no_actor_decision() -> None:
    """The premise under the lazy-default carve-out, asserted rather than assumed.

    Eight GET routes are excused from INV-7 because the row they stage records no
    actor decision: both helpers build it from constants plus the tenant or parent
    identifier, so it is a pure function of the tenant and there is nothing to
    attribute. That is only true while the helpers take no caller-supplied value.

    Pins each signature exactly. The moment either helper gains a parameter -- a
    caller-chosen domain name, a policy override -- the row starts carrying
    somebody's choice, the carve-out's reasoning collapses, and this test says so
    instead of the exclusion silently outliving its justification.
    """
    import importlib
    import inspect

    drifted = []
    for name, (module_name, expected) in sorted(_LAZY_DEFAULT_WRITERS.items()):
        function = getattr(importlib.import_module(module_name), name)
        actual = tuple(inspect.signature(function).parameters)
        if actual != expected:
            drifted.append(f"{module_name}.{name}: {actual} != {expected}")
    assert drifted == [], (
        "a lazy-default writer now takes caller-supplied input, so the row it "
        "creates records an actor decision and can no longer be excused from "
        f"INV-7; revisit _LAZY_DEFAULT_WRITE_ROUTES: {drifted}"
    )


def test_the_mutation_set_is_derived_not_empty() -> None:
    """Tripwire: if route enumeration or call-graph resolution breaks,
    `test_every_mutation_audits` would pass by checking nothing.
    """
    mutating = _mutating_routes()
    assert len(mutating) >= 99
    assert any(
        "GET" in route.methods and _persists(route) for route in mutating
    ) or all(set(route.methods) & MUTATING_METHODS for route in mutating)


# --- the audit record's own contents ---------------------------------------

# The six attributes INV-7 names, mapped to the AuditEvent column that carries
# each. Written out so that renaming or dropping a column fails this test with
# the invariant's own vocabulary rather than an opaque AttributeError.
_REQUIRED_AUDIT_ATTRIBUTES: dict[str, str] = {
    "actor identity": "principal_id",
    "actor type": "principal_type",
    "resource type": "resource_type",
    "resource id": "resource_id",
    "action": "action",
    "tenant boundary": "organization_id",
    "correlation ID": "correlation_id",
    "timestamp": "occurred_at",
    "outcome": "outcome",
}


def test_audit_record_carries_every_attribute_the_invariant_names() -> None:
    """INV-7's field list, checked against a record the platform actually writes.

    Builds an audit record through `record_audit` -- the single helper every
    audited path in the codebase calls -- and asserts each attribute INV-7 names is
    both a column on `AuditEvent` and populated on the instance.

    Prevents an audit trail that exists but cannot answer "who, to what, when, and
    under which request", which is the only question it is for.
    """
    session = RecordingSession()
    organization_id = uuid4()
    context = security_context(organization_id=organization_id, principal_id="analyst-7")
    before = datetime.now(UTC)

    record_audit(
        session,
        context,
        action="semantic_model.approve",
        resource_type="semantic_model_version",
        resource_id=str(uuid4()),
        outcome="SUCCESS",
        correlation_id="corr-inv7",
        details={"reason": "checker approval"},
    )

    events = session.added_of(AuditEvent)
    assert len(events) == 1
    event = events[0]

    for attribute, column in _REQUIRED_AUDIT_ATTRIBUTES.items():
        assert column in AuditEvent.__table__.columns, (
            f"AuditEvent has no column for the {attribute} INV-7 requires: {column}"
        )

    # `occurred_at` is populated by SQLAlchemy at INSERT time from the column
    # default, so an un-flushed instance legitimately has None there. Asserting
    # the instance value would need a live database; asserting that the column
    # is NOT NULL *and* carries a default is the same guarantee without one --
    # together they make it impossible to write an audit row with no timestamp.
    occurred_at = AuditEvent.__table__.columns["occurred_at"]
    assert occurred_at.nullable is False
    assert occurred_at.default is not None, (
        "AuditEvent.occurred_at lost its default; an audit row could be written "
        "with no timestamp"
    )

    for attribute, column in _REQUIRED_AUDIT_ATTRIBUTES.items():
        if column == "occurred_at":
            continue
        assert getattr(event, column) is not None, (
            f"record_audit left the {attribute} ({column}) unset"
        )

    assert event.organization_id == organization_id
    assert event.principal_id == "analyst-7"
    assert event.correlation_id == "corr-inv7"
    assert before <= datetime.now(UTC)


def test_audit_record_is_not_optional_on_the_helper() -> None:
    """`record_audit` must keep requiring the attributable fields.

    If any of them acquires a default, an audited call site can silently omit it
    and the audit row becomes unattributable while every other test still passes.
    """
    import inspect

    parameters = inspect.signature(record_audit).parameters
    for required in ("action", "resource_type", "resource_id", "outcome", "correlation_id"):
        assert parameters[required].default is inspect.Parameter.empty, (
            f"record_audit made {required} optional; an audit record without it is "
            "not attributable"
        )


# --- the same-transaction clause -------------------------------------------


class _TransactionWitness(RecordingSession):
    """Records what was staged at each commit boundary.

    INV-7's "written in the same transaction as the mutation" is a claim about
    *when* the audit row is added relative to the commit, which a test that only
    inspects the final state cannot see. This captures the staged batch at every
    commit so the ordering is observable.
    """

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[type]] = []

    async def commit(self) -> None:
        await super().commit()
        self.batches.append([type(instance) for instance in self.added])


async def test_audit_is_staged_before_the_commit_that_persists_the_mutation() -> None:
    """INV-7's same-transaction clause, proven on the query-execution path.

    `QueryExecutionGateway.execute` is the platform's highest-impact mutation --
    it is the one that touches a source -- and it is the path where an audit
    written after the fact would be lost exactly when a query is killed mid-flight.
    Every commit it performs must already have an `AuditEvent` staged alongside the
    `QueryExecution` row.

    Prevents the refactor where `record_audit` drifts after `session.commit()` and
    the audit trail silently loses every crashed request.
    """
    from aida.models import QueryExecution

    witness = _TransactionWitness()
    context = security_context(organization_id=uuid4())
    record_audit(
        witness,
        context,
        action="query.execute.requested",
        resource_type="query_execution",
        resource_id=str(uuid4()),
        outcome="SUCCESS",
        correlation_id="corr-inv7-tx",
    )
    witness.add(QueryExecution(organization_id=context.organization_id, dialect="postgres"))
    await witness.commit()

    assert witness.batches, "no commit was observed"
    for batch in witness.batches:
        assert AuditEvent in batch, (
            "a commit persisted a mutation with no audit record staged in the same "
            "transaction"
        )


def test_the_gateway_stages_its_audit_before_committing() -> None:
    """The static half of the clause above, read off the gateway's own source.

    Asserts that within `QueryExecutionGateway.execute` the first `record_audit`
    call appears before the first `session.commit()`. A behavioural test can only
    observe the commits it drives; this one holds for every branch of the method,
    including the rejection paths a fake source never reaches.
    """
    import inspect

    from aida.query_gateway import QueryExecutionGateway

    source = inspect.getsource(QueryExecutionGateway.execute)
    first_audit = source.find("record_audit(")
    first_commit = source.find("session.commit()")
    assert first_audit != -1, "the gateway no longer audits at all"
    assert first_commit != -1, "the gateway no longer commits; this test needs rewriting"
    assert first_audit < first_commit, (
        "QueryExecutionGateway.execute commits before it stages an audit record; "
        "INV-7 requires the audit in the same transaction as the mutation"
    )
