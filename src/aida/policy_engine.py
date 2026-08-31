"""Attribute-based access evaluation (ADR-0018).

RBAC alone stops scaling at the point a bank estate becomes interesting: a role
grant enumerates the objects it covers, so every newly discovered column needs an
administrative action before it is governed. A policy that keys on what a resource
*is* -- its classification, its business node, its certification -- covers the
column discovered next Tuesday with no action at all.

Three properties are load-bearing, and each is a deliberate design choice rather
than an implementation detail:

**DENY is a hard ceiling.** It is evaluated first and cannot be overridden by any
role, priority or effect, including workspace owner and platform admin. A model in
which a sufficiently privileged principal can override a deny is not a model a bank
control framework will accept.

**Default is deny (INV-4).** A request that matches no ALLOW is refused. An empty
policy set therefore denies everything, which is the correct behaviour for a
misconfigured or partially-loaded control plane -- the failure mode of "no policies
loaded, so everything is permitted" is the one that ends up in an incident report.

**`principal_kind` is a first-class subject attribute.** "Humans may see full
account numbers, agents never do" is one policy rather than an inexpressible
intention. This is the control most often asked for once agents reach production
and it is not expressible under role-based access alone.

The engine is deliberately pure: it takes an already-loaded tuple of policy records
and returns a decision. It touches no session and performs no I/O, so it is
exhaustively testable without infrastructure, and the loading strategy (cache,
per-request, pinned version) can change without touching the decision logic.

Value-freedom (INV-6): a decision carries reason codes and policy identifiers only.
No resource value, and no policy expression, is ever placed in a decision, an audit
record, or an error message returned to a caller. Refusals deliberately do not name
which specific rule fired -- see `30-contracts/01-contract-strategy.md`.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID

# The action verbs a policy can govern. Kept small and closed on purpose: a policy
# language whose action set grows per feature becomes impossible to reason about,
# and every one of these maps to a real enforcement point.
ACTIONS = frozenset(
    {
        "READ_METADATA",
        "READ_DATA",
        "PROPOSE",
        "APPROVE",
        "EXECUTE_TOOL",
        "CONSUME_CONTEXT",
        "EXPORT",
    }
)

EFFECTS = frozenset({"ALLOW", "DENY", "MASK", "FILTER"})

PRINCIPAL_KINDS = frozenset({"HUMAN", "AGENT", "SERVICE"})


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """One immutable policy version, as loaded from `access_policy`."""

    id: UUID
    code: str
    version: int
    effect: str
    priority: int
    subject_match: dict[str, Any] = field(default_factory=dict)
    resource_match: dict[str, Any] = field(default_factory=dict)
    action_match: tuple[str, ...] = ()
    transform: dict[str, Any] = field(default_factory=dict)
    condition: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Subject:
    """Who is asking, and in what capacity."""

    principal_id: str
    principal_kind: str
    roles: frozenset[str]
    workspace_id: UUID | None = None
    purpose: str | None = None
    isolation_boundary_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Resource:
    """What is being asked for, described by its attributes rather than its identity.

    `business_node_ids` must already be the *closure* -- the node the resource is
    assigned to plus every ancestor -- so that a policy written against "Retail
    Banking" also covers assets assigned to its sub-domains. Computing the closure
    is the caller's job (`business_graph.ancestor_closure`) because it is a query,
    and this module performs no I/O.
    """

    resource_type: str
    resource_id: str | None = None
    classifications: frozenset[str] = frozenset()
    business_node_ids: frozenset[UUID] = frozenset()
    certification: str | None = None
    datasource_id: UUID | None = None
    schema_name: str | None = None
    quality_state: str | None = None
    freshness_state: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason_code: str
    matched_policy_id: UUID | None = None
    matched_policy_code: str | None = None
    # Column-level obligations gathered from every matching MASK policy, and row
    # filters from every matching FILTER policy. Obligations accumulate: two MASK
    # policies both apply, because the stricter of two masking requirements is the
    # only safe resolution.
    masked_classifications: frozenset[str] = frozenset()
    masking_profile: str | None = None
    row_filters: tuple[str, ...] = ()
    evaluated_policy_ids: tuple[UUID, ...] = ()


def _matches_subject(policy: PolicyRecord, subject: Subject) -> bool:
    match = policy.subject_match
    if not match:
        return True
    roles = match.get("roles")
    if roles is not None and subject.roles.isdisjoint(roles):
        return False
    kind = match.get("principal_kind")
    if kind is not None and subject.principal_kind != kind:
        return False
    principals = match.get("principal_ids")
    if principals is not None and subject.principal_id not in principals:
        return False
    purposes = match.get("purposes")
    if purposes is not None and (subject.purpose is None or subject.purpose not in purposes):
        return False
    workspaces = match.get("workspace_ids")
    if workspaces is not None:
        if subject.workspace_id is None or str(subject.workspace_id) not in {
            str(value) for value in workspaces
        }:
            return False
    return True


def _matches_resource(policy: PolicyRecord, resource: Resource) -> bool:
    match = policy.resource_match
    if not match:
        return True
    classifications = match.get("classifications")
    if classifications is not None and resource.classifications.isdisjoint(classifications):
        return False
    nodes = match.get("business_node_ids")
    if nodes is not None:
        wanted = {str(value) for value in nodes}
        if not wanted & {str(value) for value in resource.business_node_ids}:
            return False
    types = match.get("resource_types")
    if types is not None and resource.resource_type not in types:
        return False
    certifications = match.get("certifications")
    if certifications is not None and resource.certification not in certifications:
        return False
    datasources = match.get("datasource_ids")
    if datasources is not None:
        if resource.datasource_id is None or str(resource.datasource_id) not in {
            str(value) for value in datasources
        }:
            return False
    pattern = match.get("schema_pattern")
    if pattern is not None:
        if resource.schema_name is None or not fnmatchcase(resource.schema_name, pattern):
            return False
    return True


def _matches_condition(policy: PolicyRecord, *, now: datetime) -> bool:
    """Time-of-day and state conditions.

    Quality and freshness conditions are evaluated against the resource by the
    caller and passed in on the `Resource`; only clock conditions are decided here,
    because only the clock is not an attribute of the request.
    """
    condition = policy.condition
    if not condition:
        return True
    not_before = condition.get("not_before")
    if not_before is not None and now < datetime.fromisoformat(not_before):
        return False
    not_after = condition.get("not_after")
    if not_after is not None and now > datetime.fromisoformat(not_after):
        return False
    return True


def _matches_state_condition(policy: PolicyRecord, resource: Resource) -> bool:
    condition = policy.condition
    forbidden_quality = condition.get("deny_when_quality_state_in")
    if forbidden_quality is not None and resource.quality_state in forbidden_quality:
        return False
    forbidden_freshness = condition.get("deny_when_freshness_state_in")
    if forbidden_freshness is not None and resource.freshness_state in forbidden_freshness:
        return False
    return True


def _applies(
    policy: PolicyRecord, subject: Subject, resource: Resource, action: str, now: datetime
) -> bool:
    if policy.action_match and action not in policy.action_match:
        return False
    return (
        _matches_subject(policy, subject)
        and _matches_resource(policy, resource)
        and _matches_condition(policy, now=now)
        and _matches_state_condition(policy, resource)
    )


def evaluate(
    policies: tuple[PolicyRecord, ...],
    subject: Subject,
    resource: Resource,
    action: str,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    """Decide one request. Pure, deterministic, no I/O.

    Order is significant and is the whole security argument:

    1. Every DENY that applies wins immediately. Nothing later can lift it.
    2. Obligations (MASK, FILTER) are gathered from every policy that applies.
    3. The highest-priority ALLOW that applies decides. Ties break on the lowest
       policy id, so the outcome is stable across evaluations and reproducible
       when a decision is replayed a year later.
    4. No ALLOW means deny (INV-4).
    """
    if action not in ACTIONS:
        # An unknown verb is a programming error, and the fail-closed reading of a
        # programming error is refusal rather than a permissive default.
        return PolicyDecision(allowed=False, reason_code="UNKNOWN_ACTION")

    evaluated: list[UUID] = []
    applicable: list[PolicyRecord] = []
    resolved_now = now or datetime.now(UTC)
    for policy in policies:
        evaluated.append(policy.id)
        if _applies(policy, subject, resource, action, resolved_now):
            applicable.append(policy)

    evaluated_ids = tuple(evaluated)

    for policy in applicable:
        if policy.effect == "DENY":
            return PolicyDecision(
                allowed=False,
                reason_code="DENIED_BY_POLICY",
                matched_policy_id=policy.id,
                matched_policy_code=policy.code,
                evaluated_policy_ids=evaluated_ids,
            )

    masked: set[str] = set()
    profile: str | None = None
    filters: list[str] = []
    for policy in applicable:
        if policy.effect == "MASK":
            masked.update(policy.transform.get("classifications", ()))
            profile = policy.transform.get("masking_profile", profile)
        elif policy.effect == "FILTER":
            row_filter = policy.transform.get("row_filter")
            if row_filter:
                filters.append(str(row_filter))

    allows = [policy for policy in applicable if policy.effect == "ALLOW"]
    if not allows:
        return PolicyDecision(
            allowed=False,
            reason_code="NO_APPLICABLE_ALLOW_POLICY",
            evaluated_policy_ids=evaluated_ids,
        )

    winner = sorted(allows, key=lambda item: (-item.priority, str(item.id)))[0]
    return PolicyDecision(
        allowed=True,
        reason_code="ALLOWED_BY_POLICY",
        matched_policy_id=winner.id,
        matched_policy_code=winner.code,
        masked_classifications=frozenset(masked),
        masking_profile=profile,
        row_filters=tuple(filters),
        evaluated_policy_ids=evaluated_ids,
    )


def simulate(
    policies: tuple[PolicyRecord, ...],
    subjects: tuple[Subject, ...],
    resource: Resource,
    action: str,
    *,
    now: datetime | None = None,
) -> tuple[PolicyDecision, ...]:
    """"Who could see this?" (PG-8) -- one resource, many hypothetical subjects.

    Pure and deterministic like `evaluate`, which it is built directly on: one
    decision per entry in `subjects`, in the same order, against the same
    policy set, resource and moment in time. No I/O, so a caller varying
    principal_kind or role combinations to answer an access review's question
    pays only the cost of the evaluations themselves -- the real, wired engine
    (`aida.workspace_service.load_policies` loads `policies`; a caller
    resolves `resource`'s classification closure the same way `authorize`
    does) rather than a second, disconnected evaluator.
    """
    return tuple(evaluate(policies, subject, resource, action, now=now) for subject in subjects)
