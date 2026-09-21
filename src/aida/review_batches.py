"""R11-REV01: a filtered, change-focused review queue, and frozen batch decisions over it.

Design: `Docs/10-architecture/22-context-enrichment-and-review-workspace.md`, "Review at
estate scale and playbook decisions" (items 20/22). This module is the first slice of it:

* **One queue, filtered and paged by the server.** `list_change_queue` reads the unified
  `GovernanceReview` queue -- the same population `GET /v1/governance/reviews/queue` serves --
  filtered by status, object type, review family, change kind (`requested_action`), object
  id and table scope, ordered by `(created_at, id)` and paged with an opaque keyset cursor.
  Offset paging is not offered: a queue that is being decided while it is read shifts under
  an offset, so page 2 would skip or repeat rows. A keyset does not.
* **Bounded loading.** A page is composed by `aida.review_queue_read_model.
  compose_review_queue` (a fixed number of batched queries whatever the page size) plus at
  most three queries of this module's own, so the number of SQL statements per page does not
  depend on how many reviews are waiting or which page is read.
  `tests/test_review_batches_scale.py` counts them at the DBAPI cursor.
* **A frozen batch binds versions, not a live filter.** `freeze_review_batch` records each
  member's id *and* the SHA-256 fingerprint of the content the reviewer inspected
  (`item_fingerprint`: the composed evidence, diff, confidence and identity of the review).
  When the reviewer sends the fingerprints they saw on each page, a member that changed
  between viewing and freezing is excluded as `STALE_EVIDENCE` immediately.
* **Every member is re-checked when the batch is decided.** `decide_review_batch` recomposes
  every eligible member (one batched pass) and refuses, per member: `NOT_FOUND`,
  `ALREADY_DECIDED` (someone else decided it since the freeze), `STALE_EVIDENCE` (its
  evidence fingerprint moved), `EVIDENCE_NOT_SHOWN` / `INDIVIDUAL_DECISION_REQUIRED` (the
  approve gate below), `RATIONALE_REQUIRED`. Everything else goes through
  `governance_decision_service.decide_review` -- the one place a review is transitioned --
  inside its own savepoint, so a refusal there (`CONCURRENT_DECISION`, `MAKER_CHECKER`,
  `NOT_AUTHORIZED`, `UNSUPPORTED_TYPE`, `TARGET_REFUSED`) unwinds that member only.
* **Partial outcomes and corrections.** The result reports every member's outcome and reason
  code, and each applied member's *correction*: the governed lifecycle action that undoes
  it where one exists (`correction_for`), stated as unavailable -- with a reason -- where
  none does, rather than implying one.

What this module deliberately does **not** do:

* It adds no decision path. Maker-checker (INV-8, including the delegator rule of PG-4), the
  compare-and-set claim (F05), the agent-oversight guard (ADR-0027) and every object type's
  own adapter gates are exactly the ones `decide_review` applies to every other caller.
* It never decides for a non-human principal. A batch is a human reviewer's binding of what
  they inspected, so `AGENT` principals are refused outright (`AGENT_PRINCIPAL_REFUSED`) --
  the reviewer agent keeps its own bounded path, and the production refusal of unattended
  reviewer-agent approval (`atlas.platform.config`, R11-C3) is untouched.
* It does not merge review families. Relationship candidates, cross-source candidates and
  parsed-lineage reviews keep their own queues and contracts (R11-S13 declined that merge);
  this queue is the `GovernanceReview` family only, and `review_family_for` labels object
  types within it for filtering.

**The approve gate, per object type.** Batch *approval* of a member requires three things.
It is not a T3 trust-boundary change (policy, access, model routes, agent registrations),
which stays a one-at-a-time decision whatever it composes (`INDIVIDUAL_DECISION_REQUIRED`).
Its object type has an *evidence contract* in `BATCH_APPROVAL_EVIDENCE` -- the facts that
type's review actually rests on, read off what the shared read model (and this module's
bulk-operation supplement) composes for it: a description draft's proposed text *and* the
signals it was built from, a model or glossary version's structured diff, a bulk operation's
subject set and parameters, and so on. A type with no contract composes nothing a batch
could bind, so it is **reject-only** in a batch, explicitly (`NO_EVIDENCE_CONTRACT`) rather
than by accident. And the member itself composed every fact its contract names: nothing at
all is `EVIDENCE_NOT_SHOWN`; some but not all is `REQUIRED_EVIDENCE_MISSING`, with the
missing fact names reported on the queue row and in the decision's detail. Batch
*rejection* is not gated: it publishes nothing. Members failing the gate can still be decided
individually through `POST /v1/governance/reviews/{id}/decision`, which is unchanged. A
model's confidence score is deliberately *not* a contracted fact: a score is not evidence,
and the design is explicit that one must not grant approval.

**Resumable decisions.** Deciding a batch is chunked (`DECISION_CHUNK_SIZE` members a
transaction), and each member's outcome row is written *inside* the savepoint that decides
it, so a member's decision and the record of it commit together or not at all. A decision
that is interrupted -- a dropped connection, a crashed worker, an error in one member's
target -- keeps every chunk committed before it; the batch stays `FROZEN` with its decision
recorded, and calling the decision again with the same decision resumes at the first
undecided member without re-deciding any member already recorded. A batch's decision is
fixed when its first chunk commits (the claim commits with that chunk, so a call that fails
inside its first chunk leaves the batch as if never decided): resuming with the other
decision is `REVIEW_BATCH_DECISION_MISMATCH`.
Two concurrent decisions of the same batch serialize on the batch row: each chunk begins
with a conditional `UPDATE` of it, which PostgreSQL holds until that chunk commits, so the
second caller waits and then continues with whatever is still undecided (each member is
decided once) -- `tests/test_review_batches_postgres.py` races it on a real server.

**Value-free (INV-6).** Fingerprints are hashes; reason codes are codes. The only free text
that reaches the database is the reviewer's own rationale, on `governance_review.
decision_reason`, exactly where every other decision path puts it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from aida.context import get_correlation_id
from aida.events import record_audit
from aida.governance_decision_service import (
    GovernanceDecisionRefused,
    check_decision_permitted,
    claimable_columns,
    decide_review,
    delegation_details,
    lock_reviews_for_decision,
    record_decision_audit,
    record_decision_outbox,
    registered_object_types,
)
from aida.models import (
    AccessPolicy,
    AssetDescriptionDraft,
    BulkStewardshipOperation,
    ColumnDescriptionDraft,
    GlossaryTermVersion,
    GovernanceReview,
    SemanticModelVersion,
    WorkspaceMembership,
)
from aida.review_batch_models import ReviewBatch, ReviewBatchItem
from aida.review_queue_read_model import compose_review_queue
from aida.review_queue_schemas import ReviewQueueProposalRead
from aida.review_risk_tiers import TIER_T3, risk_tier_for
from aida.schemas import EvidenceItemRead
from aida.security_types import SecurityContext

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

QUEUE_PAGE_DEFAULT: Final = 50
QUEUE_PAGE_MAX: Final = 200
#: The most members one frozen batch may bind. The design's exit fixture is a
#: 1,000-table estate and a 1,000-column table; a batch covers either in one decision.
REVIEW_BATCH_MAX_ITEMS: Final = 1000
BATCH_ITEMS_PAGE_MAX: Final = 500
DETAIL_MAX_IDS: Final = 50
#: A table-scoped filter resolves its description drafts first; past this many the scope
#: is refused as too broad rather than turned into an unbounded IN list.
TABLE_SCOPE_MAX_IDS: Final = 10_000
EVIDENCE_PREVIEW_ITEMS: Final = 3

AGENT_PRINCIPAL_TYPE: Final = "AGENT"

# ---------------------------------------------------------------------------
# Review families -- labels over object types, for filtering. Not a new state machine.
# ---------------------------------------------------------------------------

FAMILY_DESCRIPTION: Final = "DESCRIPTION"
FAMILY_SEMANTIC: Final = "SEMANTIC"
FAMILY_STEWARDSHIP: Final = "STEWARDSHIP"
FAMILY_PUBLICATION: Final = "PUBLICATION"
FAMILY_ACCESS: Final = "ACCESS"
FAMILY_OTHER: Final = "OTHER"

_FAMILY_OF: Final[Mapping[str, str]] = {
    # Language attached to catalog objects, and its retirement.
    "ASSET_DESCRIPTION_DRAFT": FAMILY_DESCRIPTION,
    "COLUMN_DESCRIPTION_DRAFT": FAMILY_DESCRIPTION,
    "ROUTINE_DESCRIPTION_DRAFT": FAMILY_DESCRIPTION,
    "ASSET_DOCUMENTATION_VERSION": FAMILY_DESCRIPTION,
    "DESCRIPTION_WITHDRAWAL": FAMILY_DESCRIPTION,
    "DOCUMENT_CLAIM": FAMILY_DESCRIPTION,
    "MODEL_IMPORT_BATCH": FAMILY_DESCRIPTION,
    "METADATA_ENRICHMENT_PROPOSAL": FAMILY_DESCRIPTION,
    "BUSINESS_ANNOTATION": FAMILY_DESCRIPTION,
    # Published meaning: models, metrics, glossary, ontology.
    "SEMANTIC_MODEL_VERSION": FAMILY_SEMANTIC,
    "SEMANTIC_METRIC": FAMILY_SEMANTIC,
    "SEMANTIC_METRIC_PROPOSAL": FAMILY_SEMANTIC,
    "QUERY_HISTORY_METRIC_CANDIDATE": FAMILY_SEMANTIC,
    "GLOSSARY_TERM": FAMILY_SEMANTIC,
    "GLOSSARY_TERM_VERSION": FAMILY_SEMANTIC,
    "GLOSSARY_LINK_PROPOSAL": FAMILY_SEMANTIC,
    "GLOSSARY_CONFLICT": FAMILY_SEMANTIC,
    "TERM_SEMANTIC_BINDING": FAMILY_SEMANTIC,
    "ONTOLOGY_VERSION": FAMILY_SEMANTIC,
    # Classification, tagging, ownership, certification, quality controls.
    "BULK_STEWARDSHIP_OPERATION": FAMILY_STEWARDSHIP,
    "COLUMN_CLASSIFICATION_PROMOTION": FAMILY_STEWARDSHIP,
    "QUALITY_RULE_PROPOSAL": FAMILY_STEWARDSHIP,
    # Executable or consumable capability.
    "GOVERNED_TOOL": FAMILY_PUBLICATION,
    "GOVERNED_TOOL_VERSION": FAMILY_PUBLICATION,
    "TOOL_CERTIFICATION_RUN": FAMILY_PUBLICATION,
    "CONTEXT_PRODUCT_VERSION": FAMILY_PUBLICATION,
    "DATA_PRODUCT_VERSION": FAMILY_PUBLICATION,
    "DATA_CONTRACT_VERSION": FAMILY_PUBLICATION,
    # The trust boundary.
    "MODEL_ROUTE_CONFIGURATION": FAMILY_ACCESS,
    "AI_ASSET": FAMILY_ACCESS,
    "AI_ASSET_VERSION": FAMILY_ACCESS,
    "AGENT_CONTRACT": FAMILY_ACCESS,
    "AGENT_CONTRACT_REQUEST": FAMILY_ACCESS,
    "CROSS_BOUNDARY_GRANT": FAMILY_ACCESS,
    "DATA_PRODUCT_ACCESS_REQUEST": FAMILY_ACCESS,
    "ACCESS_POLICY": FAMILY_ACCESS,
    "SOURCE_BINDING": FAMILY_ACCESS,
    "WORKSPACE_MEMBERSHIP": FAMILY_ACCESS,
}

REVIEW_FAMILIES: Final[tuple[str, ...]] = (
    FAMILY_DESCRIPTION,
    FAMILY_SEMANTIC,
    FAMILY_STEWARDSHIP,
    FAMILY_PUBLICATION,
    FAMILY_ACCESS,
    FAMILY_OTHER,
)


def review_family_for(object_type: str) -> str:
    """The family label of one object type. Unknown types are `OTHER`, never guessed."""
    return _FAMILY_OF.get(object_type, FAMILY_OTHER)


def object_types_in_family(family: str) -> frozenset[str]:
    return frozenset(key for key, value in _FAMILY_OF.items() if value == family)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReviewBatchError(Exception):
    """A whole-request refusal, carried as a reason code the router returns verbatim."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _refuse_agents(context: SecurityContext) -> None:
    """No non-human principal freezes or decides a batch. See the module docstring."""
    if context.principal_type == AGENT_PRINCIPAL_TYPE:
        raise ReviewBatchError("AGENT_PRINCIPAL_REFUSED", 403)


# ---------------------------------------------------------------------------
# Filters and cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueFilter:
    """The change-focused filters. Empty tuples mean "no narrowing on that axis"."""

    status: str | None = "PENDING"
    object_types: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    change_kinds: tuple[str, ...] = ()
    object_id: str | None = None
    table_id: UUID | None = None
    decidable_only: bool = False

    def as_record(self) -> dict[str, Any]:
        """Codes and ids only: safe to echo and to audit."""
        return {
            "status": self.status,
            "object_types": list(self.object_types),
            "families": list(self.families),
            "change_kinds": list(self.change_kinds),
            "object_id": self.object_id,
            "table_id": str(self.table_id) if self.table_id else None,
            "decidable_only": self.decidable_only,
        }


def encode_cursor(created_at: datetime, review_id: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), review_id.hex], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at_text, review_hex = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(created_at_text), UUID(hex=review_hex)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ReviewBatchError("INVALID_CURSOR", 422) from exc


def _position_cursor(position: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"p": position}).encode()).decode().rstrip("=")


def decode_position_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))["p"]
        if not isinstance(value, int) or value < 0:
            raise ValueError("position")
        return value
    except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise ReviewBatchError("INVALID_CURSOR", 422) from exc


async def _table_scope_object_ids(
    session: AsyncSession, organization_id: UUID, table_id: UUID
) -> list[str]:
    """The review object ids of every description draft about one table and its columns.

    Two statements, engine-independent: `governance_review.object_id` is a string while the
    draft ids are UUIDs, and a SQL-side cast renders differently on PostgreSQL and SQLite,
    so the ids are resolved first and matched as strings.
    """
    table_drafts = (
        await session.scalars(
            select(AssetDescriptionDraft.id).where(
                AssetDescriptionDraft.organization_id == organization_id,
                AssetDescriptionDraft.table_id == table_id,
            )
        )
    ).all()
    column_drafts = (
        await session.scalars(
            select(ColumnDescriptionDraft.id)
            .where(
                ColumnDescriptionDraft.organization_id == organization_id,
                ColumnDescriptionDraft.table_id == table_id,
            )
            .limit(TABLE_SCOPE_MAX_IDS + 1)
        )
    ).all()
    ids = [str(value) for value in (*table_drafts, *column_drafts)]
    if len(ids) > TABLE_SCOPE_MAX_IDS:
        raise ReviewBatchError("TABLE_SCOPE_TOO_BROAD", 422)
    return ids


async def _queue_predicates(
    session: AsyncSession,
    *,
    organization_id: UUID,
    filt: QueueFilter,
    context: SecurityContext,
) -> list[ColumnElement[bool]]:
    predicates: list[ColumnElement[bool]] = [GovernanceReview.organization_id == organization_id]
    if filt.status:
        predicates.append(GovernanceReview.status == filt.status.upper())
    if filt.object_types:
        predicates.append(GovernanceReview.object_type.in_([v.upper() for v in filt.object_types]))
    if filt.families:
        families = {value.upper() for value in filt.families}
        unknown = families - set(REVIEW_FAMILIES)
        if unknown:
            raise ReviewBatchError("UNKNOWN_REVIEW_FAMILY", 422)
        named = sorted(
            {t for family in families - {FAMILY_OTHER} for t in object_types_in_family(family)}
        )
        clauses: list[ColumnElement[bool]] = []
        if named:
            clauses.append(GovernanceReview.object_type.in_(named))
        if FAMILY_OTHER in families:
            clauses.append(GovernanceReview.object_type.not_in(sorted(_FAMILY_OF)))
        predicates.append(or_(*clauses))
    if filt.change_kinds:
        predicates.append(
            GovernanceReview.requested_action.in_([v.upper() for v in filt.change_kinds])
        )
    if filt.object_id:
        predicates.append(GovernanceReview.object_id == filt.object_id)
    if filt.table_id is not None:
        scoped = await _table_scope_object_ids(session, organization_id, filt.table_id)
        predicates.append(
            GovernanceReview.object_type.in_(
                ("ASSET_DESCRIPTION_DRAFT", "COLUMN_DESCRIPTION_DRAFT")
            )
        )
        # An empty IN list is a valid, always-false predicate: the scope has no drafts.
        predicates.append(GovernanceReview.object_id.in_(scoped))
    if filt.decidable_only:
        # The SQL form of `check_decision_permitted`'s maker-checker half, so a page of
        # "things I may decide" is paged by the server rather than thinned by the client.
        predicates.append(GovernanceReview.requested_by != context.principal_id)
        if context.active_delegator_principal_id is not None:
            predicates.append(
                GovernanceReview.requested_by != context.active_delegator_principal_id
            )
    return predicates


# ---------------------------------------------------------------------------
# Composition and fingerprints
# ---------------------------------------------------------------------------

_DIFF_TARGET_TYPES: Final[Mapping[str, Any]] = {
    "SEMANTIC_MODEL_VERSION": SemanticModelVersion,
    "GLOSSARY_TERM_VERSION": GlossaryTermVersion,
    "ACCESS_POLICY": AccessPolicy,
    "WORKSPACE_MEMBERSHIP": WorkspaceMembership,
}


@dataclass(slots=True)
class ComposedMember:
    """One review as the queue shows it, plus the fingerprint of exactly that content."""

    review: GovernanceReview
    proposal: ReviewQueueProposalRead | None
    supplement: list[EvidenceItemRead]
    fingerprint: str
    target_unavailable: bool = False

    @property
    def evidence(self) -> list[EvidenceItemRead]:
        base = list(self.proposal.evidence) if self.proposal is not None else []
        return [*base, *self.supplement]

    @property
    def evidence_shown(self) -> bool:
        if self.proposal is None:
            return False
        return bool(self.evidence) or self.proposal.diff.diffable


def _json_default(value: Any) -> str:
    """Datetimes are normalized to aware UTC: SQLite hands back naive values for a row that
    an in-memory ORM object holds as aware, and the same content must hash the same way
    whichever of the two a request happened to read."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def item_fingerprint(
    review: GovernanceReview,
    proposal: ReviewQueueProposalRead | None,
    supplement: Sequence[EvidenceItemRead],
    *,
    target_unavailable: bool = False,
) -> str:
    """SHA-256 of what a reviewer is shown for one review, and nothing else.

    Covered: identity (review id, object type/id, change kind, requester), status, the
    numeric confidence, every evidence item (category, claim, source, time), the structured
    diff (before, after, entries) and this module's bulk-operation supplement. So an edited
    draft, a re-scored proposal, a newly published predecessor that moves a diff's "before",
    or a bulk operation whose subjects changed all produce a new fingerprint.

    Not covered, deliberately: `created_at`/`updated_at` (a touch that changes nothing a
    reviewer reads is not a change) and the ADR-0027 pre-review columns (an agent's
    recommendation is not the evidence under review).
    """
    payload: dict[str, Any] = {
        "review_id": str(review.id),
        "object_type": review.object_type,
        "object_id": review.object_id,
        "requested_action": review.requested_action,
        "requested_by": review.requested_by,
        "status": review.status,
        "target_unavailable": target_unavailable,
        "supplement": [
            [item.category, item.claim, item.source, item.occurred_at] for item in supplement
        ],
    }
    if proposal is not None:
        payload["confidence"] = proposal.confidence
        payload["evidence"] = [
            [item.category, item.claim, item.source, item.occurred_at]
            for item in proposal.evidence
        ]
        diff = proposal.diff
        payload["diff"] = {
            "diffable": diff.diffable,
            "before": diff.before,
            "after": diff.after,
            "message": diff.message,
            "entries": [
                [entry.field, entry.change, entry.before, entry.after] for entry in diff.entries
            ],
        }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _bulk_operation_evidence(operation: BulkStewardshipOperation) -> list[EvidenceItemRead]:
    """What the queue shows for a bulk stewardship operation, which the shared read model
    composes no evidence for. The subject *set* is summarized by count and digest -- the
    ids themselves are paged from the operation's own endpoint, never inlined here."""
    source = f"bulk_stewardship_operation:{operation.id}"
    subjects = sorted(str(value) for value in (operation.subject_ids or []))
    digest = hashlib.sha256(_canonical(subjects).encode()).hexdigest()[:16]
    items = [
        EvidenceItemRead(
            category="BULK_STEWARDSHIP_OPERATION",
            claim=(
                f"{operation.operation_type} on {len(subjects)} "
                f"{operation.subject_type.lower()} subject(s)"
            ),
            source=source,
            occurred_at=operation.created_at,
        ),
        EvidenceItemRead(
            category="BULK_STEWARDSHIP_OPERATION",
            claim=f"subject set digest: {digest}",
            source=f"{source}.subject_ids",
        ),
        EvidenceItemRead(
            category="BULK_STEWARDSHIP_OPERATION",
            claim=f"operation status: {operation.status}",
            source=source,
        ),
    ]
    if operation.reverses_operation_id is not None:
        items.append(
            EvidenceItemRead(
                category="BULK_STEWARDSHIP_OPERATION",
                claim=f"reverses operation {operation.reverses_operation_id}",
                source=f"{source}.reverses_operation_id",
            )
        )
    for key, value in sorted((operation.parameters or {}).items(), key=lambda kv: kv[0]):
        if key == "before_images":
            recorded = len(value) if isinstance(value, Mapping) else 0
            items.append(
                EvidenceItemRead(
                    category="BULK_STEWARDSHIP_OPERATION",
                    claim=f"before_images: {recorded} recorded",
                    source=f"{source}.parameters",
                )
            )
            continue
        items.append(
            EvidenceItemRead(
                category="BULK_STEWARDSHIP_OPERATION",
                claim=f"{key}: {value}",
                source=f"{source}.parameters",
            )
        )
    return items


async def compose_members(
    session: AsyncSession, organization_id: UUID, reviews: Sequence[GovernanceReview]
) -> dict[UUID, ComposedMember]:
    """Compose and fingerprint a page (or a batch) of reviews in a bounded number of queries.

    `compose_review_queue` raises 409 for the whole list when one diffable review's target
    has vanished; that would let one broken row take down a page of a thousand. Targets are
    therefore checked first (one query per diffable type present) and a review whose target
    is gone is carried as `target_unavailable` -- shown, fingerprinted, never decidable.
    """
    missing: set[UUID] = set()
    for object_type, model in _DIFF_TARGET_TYPES.items():
        wanted: dict[UUID, UUID] = {}
        for review in reviews:
            if review.object_type != object_type:
                continue
            try:
                wanted[review.id] = UUID(review.object_id)
            except ValueError:
                continue
        if not wanted:
            continue
        present = set(
            (
                await session.scalars(
                    select(model.id).where(
                        model.organization_id == organization_id,
                        model.id.in_(set(wanted.values())),
                    )
                )
            ).all()
        )
        missing.update(rid for rid, oid in wanted.items() if oid not in present)

    composable = [review for review in reviews if review.id not in missing]
    proposals = {item.review_id: item for item in await compose_review_queue(session, composable)}

    operation_ids: list[UUID] = []
    for review in composable:
        if review.object_type != "BULK_STEWARDSHIP_OPERATION":
            continue
        try:
            operation_ids.append(UUID(review.object_id))
        except ValueError:
            continue
    operations: dict[str, BulkStewardshipOperation] = {}
    if operation_ids:
        rows = (
            await session.scalars(
                select(BulkStewardshipOperation).where(
                    BulkStewardshipOperation.organization_id == organization_id,
                    BulkStewardshipOperation.id.in_(operation_ids),
                )
            )
        ).all()
        operations = {str(row.id): row for row in rows}

    composed: dict[UUID, ComposedMember] = {}
    for review in reviews:
        proposal = proposals.get(review.id)
        supplement: list[EvidenceItemRead] = []
        if review.object_type == "BULK_STEWARDSHIP_OPERATION":
            operation = operations.get(review.object_id)
            if operation is not None:
                supplement = _bulk_operation_evidence(operation)
        unavailable = review.id in missing
        composed[review.id] = ComposedMember(
            review=review,
            proposal=proposal,
            supplement=supplement,
            fingerprint=item_fingerprint(
                review, proposal, supplement, target_unavailable=unavailable
            ),
            target_unavailable=unavailable,
        )
    return composed


# ---------------------------------------------------------------------------
# Per-type evidence contracts for batch approval
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequiredEvidence:
    """One fact a reviewer must have been shown before a member of a type is batch-approved.

    Matched against the member's composed evidence by category and by the *source* the
    composer stamps on each item, because the source is what says which record a fact came
    from (the draft's text, the draft's evidence payload, the operation's subject list). A
    `structured_diff` requirement is met by a diffable diff instead of an item.
    """

    name: str
    category: str | None = None
    source: re.Pattern[str] | None = None
    structured_diff: bool = False

    def satisfied_by(self, member: ComposedMember) -> bool:
        if self.structured_diff:
            return member.proposal is not None and member.proposal.diff.diffable
        return any(
            (self.category is None or item.category == self.category)
            and (self.source is None or self.source.search(item.source) is not None)
            and _states_something(item.claim)
            for item in member.evidence
        )


def _states_something(claim: str) -> bool:
    """A `key: value` claim whose value is blank shows the reviewer nothing -- the case that
    matters is a draft whose proposed text is empty, rendered `proposed_description: `."""
    _, separator, value = claim.partition(":")
    return bool((value if separator else claim).strip())


def _source(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _description_contract(record: str) -> tuple[RequiredEvidence, ...]:
    # `review_queue_read_model` composes a draft as its proposed text first (`.drafted_text`)
    # and then one item per key of the evidence payload the draft was built from
    # (`.evidence`): the text being published, and the source references it stands on.
    return (
        RequiredEvidence(
            "PROPOSED_TEXT", "DESCRIPTION_DRAFT", _source(rf"^{record}:[^.]+\.drafted_text$")
        ),
        RequiredEvidence(
            "SOURCE_SIGNALS", "DESCRIPTION_DRAFT", _source(rf"^{record}:[^.]+\.evidence$")
        ),
    )


#: The evidence contract of every object type a batch may *approve*, derived from what
#: `aida.review_queue_read_model.compose_review_queue` (and `_bulk_operation_evidence` here)
#: composes for it. Any type absent from this mapping is reject-only in a batch
#: (`NO_EVIDENCE_CONTRACT`); a T3 type is individual-only even if it were present.
#: `tests/test_review_batch_evidence_gates.py` composes each contracted type through the real
#: read model and asserts the contract is met, so a composer change that drops a fact fails
#: the build instead of quietly making the type unapprovable -- or approvable without it.
BATCH_APPROVAL_EVIDENCE: Final[Mapping[str, tuple[RequiredEvidence, ...]]] = {
    "ASSET_DESCRIPTION_DRAFT": _description_contract("asset_description_draft"),
    "COLUMN_DESCRIPTION_DRAFT": _description_contract("column_description_draft"),
    "ROUTINE_DESCRIPTION_DRAFT": _description_contract("routine_description_draft"),
    "METADATA_ENRICHMENT_PROPOSAL": (
        # Which engine proposed it, from which inference run, on which evidence ids.
        RequiredEvidence(
            "ENGINE_PROVENANCE",
            "BUSINESS_SEMANTICS_PROPOSAL",
            _source(r"^metadata_enrichment_proposal:[^.]+$"),
        ),
        RequiredEvidence(
            "INFERENCE_RUN", "BUSINESS_SEMANTICS_PROPOSAL", _source(r"^semantic_inference_run$")
        ),
        RequiredEvidence(
            "EVIDENCE_REFERENCES",
            "BUSINESS_SEMANTICS_PROPOSAL",
            _source(r"^metadata_enrichment_proposal:[^.]+\.evidence\.evidence_ids$"),
        ),
    ),
    "GLOSSARY_LINK_PROPOSAL": (
        RequiredEvidence(
            "MATCH_EVIDENCE",
            "GLOSSARY_LINK_PROPOSAL",
            _source(r"^glossary_link_proposal:[^.]+\.evidence$"),
        ),
    ),
    "SEMANTIC_METRIC_PROPOSAL": (
        RequiredEvidence(
            "PROPOSAL_EVIDENCE",
            "METRIC_PROPOSAL",
            _source(r"^semantic_metric_proposal:[^.]+\.evidence$"),
        ),
    ),
    "TERM_SEMANTIC_BINDING": (
        RequiredEvidence(
            "BINDING_IDENTITY", "TERM_BINDING", _source(r"^term_semantic_binding:[^.]+$")
        ),
    ),
    "QUALITY_RULE_PROPOSAL": (
        RequiredEvidence(
            "PROPOSED_RULE",
            "QUALITY_RULE_PROPOSAL",
            _source(r"^quality_rule_proposal:[^.]+$"),
        ),
        RequiredEvidence(
            "PROFILE_EVIDENCE",
            "QUALITY_RULE_PROPOSAL",
            _source(r"^quality_rule_proposal:[^.]+\.evidence$"),
        ),
    ),
    # Human-authored versions: the structured before/after *is* the content under review.
    "SEMANTIC_MODEL_VERSION": (RequiredEvidence("STRUCTURED_DIFF", structured_diff=True),),
    "GLOSSARY_TERM_VERSION": (RequiredEvidence("STRUCTURED_DIFF", structured_diff=True),),
    "BULK_STEWARDSHIP_OPERATION": (
        # The design's classification/certification row: what is done, to which objects,
        # with which parameters (policy value, owner, expiry).
        RequiredEvidence(
            "OPERATION_SUMMARY",
            "BULK_STEWARDSHIP_OPERATION",
            _source(r"^bulk_stewardship_operation:[^.]+$"),
        ),
        RequiredEvidence(
            "AFFECTED_SUBJECTS",
            "BULK_STEWARDSHIP_OPERATION",
            _source(r"^bulk_stewardship_operation:[^.]+\.subject_ids$"),
        ),
        RequiredEvidence(
            "ACTION_PARAMETERS",
            "BULK_STEWARDSHIP_OPERATION",
            _source(r"^bulk_stewardship_operation:[^.]+\.parameters$"),
        ),
    ),
}


def required_evidence(object_type: str) -> tuple[str, ...]:
    """The fact names batch approval of this type requires; empty when it has no contract."""
    return tuple(item.name for item in BATCH_APPROVAL_EVIDENCE.get(object_type, ()))


def missing_evidence(member: ComposedMember) -> tuple[str, ...]:
    """The contracted facts this member did not compose, in contract order."""
    return tuple(
        item.name
        for item in BATCH_APPROVAL_EVIDENCE.get(member.review.object_type, ())
        if not item.satisfied_by(member)
    )


def approve_gate_detail(member: ComposedMember, code: str | None) -> str | None:
    """The operator-facing sentence for a gate refusal -- response only, never persisted."""
    if code == "REQUIRED_EVIDENCE_MISSING":
        return "missing required evidence: " + ", ".join(missing_evidence(member))
    if code == "NO_EVIDENCE_CONTRACT":
        return (
            f"{member.review.object_type} has no batch evidence contract; "
            "reject it in a batch or decide it individually"
        )
    return None


# ---------------------------------------------------------------------------
# Per-member gates
# ---------------------------------------------------------------------------


def _permission_code(refusal: GovernanceDecisionRefused) -> str:
    """Map a decision-service refusal to a stable code. The service's sentences are
    constants in code, but only the code is persisted."""
    if refusal.outcome == "CONFLICT":
        return "CONCURRENT_DECISION"
    if refusal.http_status == 403:
        return "NOT_AUTHORIZED"
    if "maker-checker" in refusal.detail:
        return "MAKER_CHECKER"
    if "unsupported" in refusal.detail:
        return "UNSUPPORTED_TYPE"
    return "NOT_PERMITTED"


def decide_blocker(member: ComposedMember, context: SecurityContext) -> str | None:
    """Why this caller may not decide this review at all right now, or None.

    The maker-checker half is `governance_decision_service.check_decision_permitted` itself,
    called rather than re-implemented, so the queue's per-row answer and the decision path's
    refusal cannot disagree.
    """
    review = member.review
    if review.status != "PENDING":
        return "NOT_PENDING"
    try:
        check_decision_permitted(review, context)
    except GovernanceDecisionRefused as refusal:
        return _permission_code(refusal)
    if review.object_type not in registered_object_types():
        return "UNSUPPORTED_TYPE"
    if member.target_unavailable:
        return "TARGET_UNAVAILABLE"
    return None


def approve_gate(member: ComposedMember) -> str | None:
    """Why *batch approval* is refused for this member even when it is current, or None.

    Order: the trust boundary first (a T3 change is individual whatever it composes), then
    the type's evidence contract (none: reject-only), then this member's own evidence
    against that contract. See the module docstring.
    """
    object_type = member.review.object_type
    if risk_tier_for(object_type) == TIER_T3:
        return "INDIVIDUAL_DECISION_REQUIRED"
    contract = BATCH_APPROVAL_EVIDENCE.get(object_type)
    if contract is None:
        return "NO_EVIDENCE_CONTRACT"
    if not member.evidence_shown:
        return "EVIDENCE_NOT_SHOWN"
    if missing_evidence(member):
        return "REQUIRED_EVIDENCE_MISSING"
    return None


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class QueuePage:
    organization_id: UUID
    members: list[ComposedMember]
    next_cursor: str | None
    total: int | None
    limit: int


def _order() -> tuple[Any, Any]:
    return (GovernanceReview.created_at, GovernanceReview.id)


async def list_change_queue(
    session: AsyncSession,
    *,
    context: SecurityContext,
    filt: QueueFilter,
    cursor: str | None,
    limit: int,
    include_total: bool = True,
) -> QueuePage:
    """One keyset page of the filtered queue, composed and fingerprinted.

    Statements: table scope (0 or 2) + page (1) + count (0 or 1) + composition (the shared
    read model's fixed set, plus at most one diff-target check per diffable type and one
    bulk-operation load). None of them grows with the queue or with the page index.
    """
    organization_id = context.require_organization()
    limit = max(1, min(limit, QUEUE_PAGE_MAX))
    predicates = await _queue_predicates(
        session, organization_id=organization_id, filt=filt, context=context
    )
    statement = select(GovernanceReview).where(*predicates)
    if cursor:
        created_at, review_id = decode_cursor(cursor)
        statement = statement.where(
            or_(
                GovernanceReview.created_at > created_at,
                and_(GovernanceReview.created_at == created_at, GovernanceReview.id > review_id),
            )
        )
    rows = list(
        (await session.scalars(statement.order_by(*_order()).limit(limit + 1))).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    total: int | None = None
    if include_total:
        counted = await session.scalar(
            select(func.count()).select_from(GovernanceReview).where(*predicates)
        )
        total = int(counted or 0)
    composed = await compose_members(session, organization_id, rows)
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return QueuePage(
        organization_id=organization_id,
        members=[composed[row.id] for row in rows],
        next_cursor=next_cursor,
        total=total,
        limit=limit,
    )


async def load_queue_details(
    session: AsyncSession, *, context: SecurityContext, review_ids: Sequence[UUID]
) -> list[ComposedMember]:
    """Full composition for a handful of rows the reviewer expanded -- detail on demand."""
    organization_id = context.require_organization()
    wanted = list(dict.fromkeys(review_ids))
    if len(wanted) > DETAIL_MAX_IDS:
        raise ReviewBatchError("TOO_MANY_DETAIL_IDS", 422)
    if not wanted:
        return []
    rows = (
        await session.scalars(
            select(GovernanceReview).where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.id.in_(wanted),
            )
        )
    ).all()
    composed = await compose_members(session, organization_id, rows)
    return [composed[review_id] for review_id in wanted if review_id in composed]


# ---------------------------------------------------------------------------
# Freezing a batch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Selection:
    review_id: UUID
    #: The fingerprint the reviewer saw on the page, when the client sends it.
    seen_fingerprint: str | None = None


def _selection_fingerprint(pairs: Sequence[tuple[UUID, str | None]]) -> str:
    return hashlib.sha256(
        _canonical([[str(review_id), fingerprint] for review_id, fingerprint in pairs]).encode()
    ).hexdigest()


async def freeze_review_batch(
    session: AsyncSession,
    *,
    context: SecurityContext,
    selections: Sequence[Selection] | None,
    filt: QueueFilter | None,
) -> ReviewBatch:
    """Bind explicit reviews (or a capped snapshot of a filter) at their current versions.

    Explicit selections may span any number of queue pages: the client accumulates them.
    Each member is recorded ELIGIBLE, or EXCLUDED with the reason it could never be decided
    through this batch. Nothing is decided here. The caller commits.
    """
    _refuse_agents(context)
    organization_id = context.require_organization()
    truncated = False
    if selections is not None:
        mode = "EXPLICIT"
        ordered: dict[UUID, str | None] = {}
        for selection in selections:
            if selection.review_id not in ordered:
                ordered[selection.review_id] = selection.seen_fingerprint
        if len(ordered) > REVIEW_BATCH_MAX_ITEMS:
            raise ReviewBatchError("TOO_MANY_ITEMS", 422)
    else:
        mode = "FILTER"
        assert filt is not None
        predicates = await _queue_predicates(
            session, organization_id=organization_id, filt=filt, context=context
        )
        ids = list(
            (
                await session.scalars(
                    select(GovernanceReview.id)
                    .where(*predicates)
                    .order_by(*_order())
                    .limit(REVIEW_BATCH_MAX_ITEMS + 1)
                )
            ).all()
        )
        truncated = len(ids) > REVIEW_BATCH_MAX_ITEMS
        ordered = {review_id: None for review_id in ids[:REVIEW_BATCH_MAX_ITEMS]}
    if not ordered:
        raise ReviewBatchError("EMPTY_SELECTION", 422)

    reviews = (
        await session.scalars(
            select(GovernanceReview).where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.id.in_(list(ordered)),
            )
        )
    ).all()
    composed = await compose_members(session, organization_id, reviews)

    batch = ReviewBatch(
        organization_id=organization_id,
        created_by=context.principal_id,
        created_by_type=context.principal_type,
        selection_mode=mode,
        selection_truncated=truncated,
        status="FROZEN",
        item_count=len(ordered),
        eligible_count=0,
        selection_fingerprint="",
    )
    session.add(batch)
    await session.flush()

    items: list[ReviewBatchItem] = []
    pairs: list[tuple[UUID, str | None]] = []
    for position, (review_id, seen) in enumerate(ordered.items()):
        member = composed.get(review_id)
        if member is None:
            items.append(
                ReviewBatchItem(
                    batch_id=batch.id,
                    organization_id=organization_id,
                    review_id=review_id,
                    position=position,
                    eligibility="EXCLUDED",
                    exclusion_code="NOT_FOUND",
                    outcome="PENDING",
                )
            )
            pairs.append((review_id, None))
            continue
        exclusion = decide_blocker(member, context)
        if exclusion is None and seen is not None and seen != member.fingerprint:
            exclusion = "STALE_EVIDENCE"
        # The version bound is the one the reviewer saw when they sent it; otherwise the
        # current one, which the member listing lets them inspect before deciding.
        bound = seen if seen is not None else member.fingerprint
        items.append(
            ReviewBatchItem(
                batch_id=batch.id,
                organization_id=organization_id,
                review_id=review_id,
                position=position,
                object_type=member.review.object_type,
                review_family=review_family_for(member.review.object_type),
                frozen_status=member.review.status,
                evidence_fingerprint=bound,
                eligibility="EXCLUDED" if exclusion else "ELIGIBLE",
                exclusion_code=exclusion,
                approve_gate_code=approve_gate(member),
                outcome="PENDING",
            )
        )
        pairs.append((review_id, bound))
    session.add_all(items)
    batch.eligible_count = sum(1 for item in items if item.eligibility == "ELIGIBLE")
    batch.selection_fingerprint = _selection_fingerprint(pairs)
    await session.flush()
    record_audit(
        session,
        context,
        action="governance_review.batch_freeze",
        resource_type="review_batch",
        resource_id=str(batch.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "selection_mode": mode,
            "selection_truncated": truncated,
            "item_count": batch.item_count,
            "eligible_count": batch.eligible_count,
            "selection_fingerprint": batch.selection_fingerprint,
            "exclusion_counts": dict(
                Counter(item.exclusion_code for item in items if item.exclusion_code)
            ),
            "filter": filt.as_record() if filt is not None else None,
        },
    )
    return batch


async def get_review_batch(
    session: AsyncSession, *, context: SecurityContext, batch_id: UUID
) -> ReviewBatch:
    organization_id = context.require_organization()
    batch = await session.scalar(
        select(ReviewBatch).where(
            ReviewBatch.id == batch_id, ReviewBatch.organization_id == organization_id
        )
    )
    if batch is None:
        raise ReviewBatchError("REVIEW_BATCH_NOT_FOUND", 404)
    return batch


@dataclass(frozen=True, slots=True)
class BatchCounts:
    exclusion_counts: dict[str, int]
    approve_gate_counts: dict[str, int]
    outcome_counts: dict[str, int]


async def review_batch_counts(session: AsyncSession, batch: ReviewBatch) -> BatchCounts:
    """The batch preview's counts, from one grouped statement over its members."""
    rows = (
        await session.execute(
            select(
                ReviewBatchItem.eligibility,
                ReviewBatchItem.exclusion_code,
                ReviewBatchItem.approve_gate_code,
                ReviewBatchItem.outcome,
                ReviewBatchItem.reason_code,
                func.count(),
            )
            .where(
                ReviewBatchItem.batch_id == batch.id,
                ReviewBatchItem.organization_id == batch.organization_id,
            )
            .group_by(
                ReviewBatchItem.eligibility,
                ReviewBatchItem.exclusion_code,
                ReviewBatchItem.approve_gate_code,
                ReviewBatchItem.outcome,
                ReviewBatchItem.reason_code,
            )
        )
    ).all()
    exclusions: Counter[str] = Counter()
    gates: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for eligibility, exclusion, gate, outcome, reason, count in rows:
        count = int(count)
        if eligibility == "EXCLUDED":
            exclusions[str(exclusion)] += count
        elif gate:
            gates[str(gate)] += count
        outcomes[outcome if outcome in ("APPLIED", "PENDING") else f"{outcome}:{reason}"] += count
    return BatchCounts(dict(exclusions), dict(gates), dict(outcomes))


@dataclass(slots=True)
class BatchItemPage:
    items: list[ReviewBatchItem]
    next_cursor: str | None


async def list_review_batch_items(
    session: AsyncSession,
    *,
    context: SecurityContext,
    batch_id: UUID,
    cursor: str | None,
    limit: int,
    outcome: str | None = None,
    eligibility: str | None = None,
) -> BatchItemPage:
    """A keyset page of one batch's members, in selection order. Two statements."""
    batch = await get_review_batch(session, context=context, batch_id=batch_id)
    limit = max(1, min(limit, BATCH_ITEMS_PAGE_MAX))
    statement = select(ReviewBatchItem).where(
        ReviewBatchItem.batch_id == batch.id,
        ReviewBatchItem.organization_id == batch.organization_id,
    )
    if outcome:
        statement = statement.where(ReviewBatchItem.outcome == outcome.upper())
    if eligibility:
        statement = statement.where(ReviewBatchItem.eligibility == eligibility.upper())
    if cursor:
        statement = statement.where(ReviewBatchItem.position > decode_position_cursor(cursor))
    rows = list(
        (
            await session.scalars(statement.order_by(ReviewBatchItem.position).limit(limit + 1))
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return BatchItemPage(
        items=rows,
        next_cursor=_position_cursor(rows[-1].position) if has_more and rows else None,
    )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Correction:
    """How an applied member is undone through the existing lifecycle -- or why it is not.

    `available=False` is an honest answer, not a missing one: it names what does not exist
    so a reviewer does not believe a correction was filed or is one click away.
    """

    kind: str
    available: bool
    method: str | None = None
    path: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    reason_code: str | None = None


#: Description drafts publish an append-only version that a withdrawal retires
#: (`POST /v1/descriptions/withdrawals`, itself reviewed; `aida.description_withdrawal_api`).
_DESCRIPTION_SUBJECTS: Final[Mapping[str, tuple[str, str]]] = {
    "ASSET_DESCRIPTION_DRAFT": ("TABLE", "table_id"),
    "COLUMN_DESCRIPTION_DRAFT": ("COLUMN", "column_id"),
    "ROUTINE_DESCRIPTION_DRAFT": ("ROUTINE", "routine_id"),
}


def correction_subject(
    object_type: str, review: GovernanceReview, effect_payload: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """The subject a correction acts on, read from the decision's own effect payload."""
    described = _DESCRIPTION_SUBJECTS.get(object_type)
    if described is not None:
        subject_type, key = described
        value = effect_payload.get(key)
        return (subject_type, str(value)) if value else (None, None)
    if object_type == "BULK_STEWARDSHIP_OPERATION":
        return "BULK_STEWARDSHIP_OPERATION", review.object_id
    return None, None


def correction_for(
    object_type: str | None,
    decision: str | None,
    subject_type: str | None,
    subject_id: str | None,
) -> Correction:
    if object_type is None or decision is None:
        return Correction(kind="NONE", available=False, reason_code="NOT_APPLIED")
    if decision == "REJECT":
        # A rejection publishes nothing. Rejected proposals are retained as negative
        # knowledge; changing one's mind means proposing again through the type's own path.
        return Correction(kind="REPROPOSE", available=False, reason_code="NO_REOPEN_PATH")
    if object_type in _DESCRIPTION_SUBJECTS and subject_id:
        return Correction(
            kind="WITHDRAW_DESCRIPTION",
            available=True,
            method="POST",
            path="/v1/descriptions/withdrawals",
            subject_type=subject_type,
            subject_id=subject_id,
        )
    if object_type == "BULK_STEWARDSHIP_OPERATION":
        # The compensating operation exists (`stewardship_service.
        # request_bulk_operation_reversal`, reviewed at T2), but the only HTTP route that
        # raises it is the reviewer-agent sample correction. Said, not implied.
        return Correction(
            kind="REVERSE_BULK_OPERATION",
            available=False,
            subject_type=subject_type,
            subject_id=subject_id,
            reason_code="NO_HUMAN_REVERSAL_ROUTE",
        )
    return Correction(
        kind="NONE_DEFINED", available=False, reason_code="CORRECT_THROUGH_OBJECT_PATH"
    )


# ---------------------------------------------------------------------------
# Deciding a batch
# ---------------------------------------------------------------------------

#: An adapter refusal whose detail is itself a code (e.g. `DEFINITION_MOVED`) keeps it;
#: anything else is recorded as TARGET_REFUSED. Sentences are never persisted.
_CODE_SHAPED = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

#: Members decided per transaction. Every chunk commits, so an interrupted decision loses at
#: most the chunk it was in -- which nothing of committed, so resuming simply redoes it. A
#: chunk's fixed overhead (the batch hold, the member load, the review lock, composition) is
#: a handful of statements, a few hundredths of a statement per member at this size, and a
#: second decider of the same batch waits at most one chunk for the hold.
DECISION_CHUNK_SIZE: Final = 100


@dataclass(slots=True)
class MemberOutcome:
    item: ReviewBatchItem
    correction: Correction
    #: The decision service's or adapter's own operator-facing sentence (or the approve
    #: gate's), returned in this response only -- never persisted -- for a refusal this call
    #: recorded. A member recorded by an earlier call carries its reason code alone.
    detail: str | None = None
    outcome: str = "PENDING"
    reason_code: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    #: False for a member an earlier, interrupted call of this decision recorded (or a
    #: concurrent caller of the same batch did): reported here, never decided twice.
    decided_in_this_call: bool = False


@dataclass(slots=True)
class BatchDecisionResult:
    batch: ReviewBatch
    outcomes: list[MemberOutcome] = field(default_factory=list)
    #: This call continued a decision an earlier call of the same batch had started.
    resumed: bool = False

    @property
    def applied_count(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "APPLIED")

    @property
    def refused_count(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "REFUSED")

    @property
    def skipped_count(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "SKIPPED")

    @property
    def decided_in_this_call_count(self) -> int:
        return sum(1 for o in self.outcomes if o.decided_in_this_call)

    @property
    def overall(self) -> str:
        return _overall(self.applied_count, self.refused_count + self.skipped_count)


def _overall(applied: int, not_applied: int) -> str:
    if applied and not not_applied:
        return "SUCCESS"
    if applied:
        return "PARTIAL_SUCCESS"
    return "FAILURE"


class _AlreadyRecorded(Exception):
    """Raised inside a member's savepoint when its outcome row is no longer PENDING, so the
    decision just applied unwinds with the savepoint -- someone else recorded the member."""


async def _hold_batch(
    session: AsyncSession,
    batch: ReviewBatch,
    decision: str,
    now: datetime,
    *,
    claim: bool,
) -> bool:
    """The conditional UPDATE that fixes a batch's decision (`claim`) and, at the start of
    every later chunk, holds the batch for that chunk.

    The hold is what serializes two deciders of the same batch. On PostgreSQL this UPDATE's
    row lock lasts until the chunk commits, so a second caller's hold blocks, then
    re-evaluates its predicate against the committed row: it continues with whatever is
    still undecided (same decision, still FROZEN), or matches nothing because the batch was
    closed. It is also each chunk's first write, which matters on SQLite: pysqlite defers
    BEGIN to the first DML, and a member's SAVEPOINT opened before any would otherwise be
    the outermost transaction, releasing -- committing -- on its own.
    """
    predicates: list[ColumnElement[bool]] = [
        ReviewBatch.id == batch.id,
        ReviewBatch.organization_id == batch.organization_id,
        ReviewBatch.status == "FROZEN",
    ]
    values: dict[str, Any] = {"updated_at": now}
    if claim:
        predicates.append(ReviewBatch.decision.is_(None))
        values["decision"] = decision
    else:
        predicates.append(ReviewBatch.decision == decision)
    held = cast(
        "CursorResult[Any]",
        await session.execute(
            update(ReviewBatch)
            .where(*predicates)
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    return held.rowcount == 1


async def _batch_state(session: AsyncSession, batch: ReviewBatch) -> tuple[str, str | None]:
    """The committed status and decision, read as columns -- not through the identity map,
    whose copy of the batch may predate another caller's claim."""
    row = (
        await session.execute(
            select(ReviewBatch.status, ReviewBatch.decision).where(
                ReviewBatch.id == batch.id,
                ReviewBatch.organization_id == batch.organization_id,
            )
        )
    ).one()
    return str(row.status), row.decision


async def _record_member(
    session: AsyncSession,
    item_id: UUID,
    organization_id: UUID,
    *,
    outcome: str,
    reason_code: str | None,
    now: datetime,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> bool:
    """Write one member's outcome, only while it is still PENDING.

    A statement, not an attribute write: an ORM change flushed inside one member's savepoint
    is expired -- and reverted -- when a later member's savepoint rolls back, and a recorded
    outcome must survive that. For an applied member this runs *inside* its savepoint, so
    the decision and the record of it commit together or not at all: a resumed decision can
    therefore trust PENDING to mean "not decided through this batch".
    """
    written = cast(
        "CursorResult[Any]",
        await session.execute(
            update(ReviewBatchItem)
            .where(
                ReviewBatchItem.id == item_id,
                ReviewBatchItem.organization_id == organization_id,
                ReviewBatchItem.outcome == "PENDING",
            )
            .values(
                outcome=outcome,
                reason_code=reason_code,
                decided_at=now,
                updated_at=now,
                correction_subject_type=subject_type,
                correction_subject_id=subject_id,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    return written.rowcount == 1


async def _decide_chunk(
    session: AsyncSession,
    *,
    context: SecurityContext,
    batch: ReviewBatch,
    decision: Literal["APPROVE", "REJECT"],
    reason: str | None,
    rationale_by_review_id: Mapping[UUID, str] | None,
    items: Sequence[ReviewBatchItem],
    now: datetime,
    details: dict[UUID, str],
) -> set[UUID]:
    """Re-check and decide one chunk of pending eligible members; the ids recorded."""
    # Plain values captured up front: nothing below reads a batch-item attribute after a
    # member's savepoint has opened, so an expiry can never force a lazy load mid-loop.
    frozen = [(item.id, item.review_id, item.evidence_fingerprint) for item in items]
    locked = await lock_reviews_for_decision(session, [review_id for _, review_id, _ in frozen])
    # INV-5: `lock_reviews_for_decision` loads by id; the organization is restated here.
    reviews = {
        review_id: review
        for review_id, review in locked.items()
        if review.organization_id == batch.organization_id
    }
    current = await compose_members(session, batch.organization_id, list(reviews.values()))

    recorded: set[UUID] = set()
    for item_id, review_id, bound_fingerprint in frozen:
        member = current.get(review_id)
        refusal_code: str | None = None
        if member is None:
            refusal_code = "NOT_FOUND"
        elif member.review.status != "PENDING":
            refusal_code = "ALREADY_DECIDED"
        elif member.fingerprint != bound_fingerprint:
            refusal_code = "STALE_EVIDENCE"
        elif decision == "APPROVE":
            refusal_code = approve_gate(member)
        item_reason = (
            rationale_by_review_id.get(review_id) if rationale_by_review_id else None
        ) or reason
        if refusal_code is None and decision == "REJECT" and not item_reason:
            refusal_code = "RATIONALE_REQUIRED"
        if refusal_code is not None:
            if await _record_member(
                session,
                item_id,
                batch.organization_id,
                outcome="REFUSED",
                reason_code=refusal_code,
                now=now,
            ):
                recorded.add(item_id)
                gate_detail = (
                    approve_gate_detail(member, refusal_code) if member is not None else None
                )
                if gate_detail:
                    details[item_id] = gate_detail
            continue
        assert member is not None
        review = member.review
        object_type = review.object_type
        unclaimed = claimable_columns(review)
        code: str
        detail: str
        try:
            async with session.begin_nested():
                effect = await decide_review(
                    session,
                    review,
                    decision=decision,
                    reason=item_reason,
                    context=context,
                    now=now,
                )
                record_decision_outbox(session, review, effect)
                record_decision_audit(
                    session,
                    review,
                    context=context,
                    action="governance.review.batch_decide",
                    details={
                        "decision": decision,
                        "batch_id": str(batch.id),
                        "object_id": review.object_id,
                        "evidence_fingerprint": bound_fingerprint,
                        **delegation_details(context),
                    },
                )
                subject_type, subject_id = correction_subject(object_type, review, effect.payload)
                if not await _record_member(
                    session,
                    item_id,
                    batch.organization_id,
                    outcome="APPLIED",
                    reason_code=None,
                    now=now,
                    subject_type=subject_type,
                    subject_id=subject_id,
                ):
                    raise _AlreadyRecorded
        except _AlreadyRecorded:
            # The savepoint undid the claim in the database; put the in-memory review back
            # too, as `decide_review` does for an adapter failure (a clean object is not part
            # of what a nested rollback restores).
            for column, value in unclaimed.items():
                set_committed_value(review, column, value)
            continue
        except GovernanceDecisionRefused as refused:
            code, detail = _permission_code(refused), refused.detail
        except HTTPException as exc:
            detail = str(exc.detail)
            code = detail if _CODE_SHAPED.match(detail) else "TARGET_REFUSED"
        else:
            recorded.add(item_id)
            continue
        if await _record_member(
            session,
            item_id,
            batch.organization_id,
            outcome="REFUSED",
            reason_code=code,
            now=now,
        ):
            recorded.add(item_id)
            details[item_id] = detail
    return recorded


async def _close_batch(
    session: AsyncSession,
    *,
    context: SecurityContext,
    batch: ReviewBatch,
    decision: str,
    now: datetime,
    resumed: bool,
    decided_in_this_call: int,
) -> None:
    """Mark excluded members SKIPPED, count every outcome, close the batch, audit it once."""
    await session.execute(
        update(ReviewBatchItem)
        .where(
            ReviewBatchItem.batch_id == batch.id,
            ReviewBatchItem.organization_id == batch.organization_id,
            ReviewBatchItem.eligibility == "EXCLUDED",
            ReviewBatchItem.outcome == "PENDING",
        )
        .values(
            outcome="SKIPPED",
            reason_code=ReviewBatchItem.exclusion_code,
            decided_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    counts = (await review_batch_counts(session, batch)).outcome_counts
    await session.execute(
        update(ReviewBatch)
        .where(
            ReviewBatch.id == batch.id,
            ReviewBatch.organization_id == batch.organization_id,
            ReviewBatch.status == "FROZEN",
            ReviewBatch.decision == decision,
        )
        .values(status="DECIDED", decided_at=now, updated_at=now, outcome_counts=counts)
        .execution_options(synchronize_session=False)
    )
    applied = counts.get("APPLIED", 0)
    refused = sum(value for key, value in counts.items() if key.startswith("REFUSED:"))
    skipped = sum(value for key, value in counts.items() if key.startswith("SKIPPED:"))
    record_audit(
        session,
        context,
        action="governance_review.batch_decide",
        resource_type="review_batch",
        resource_id=str(batch.id),
        outcome=_overall(applied, refused + skipped),
        correlation_id=get_correlation_id(),
        details={
            "decision": decision,
            "selection_fingerprint": batch.selection_fingerprint,
            "item_count": batch.item_count,
            "applied_count": applied,
            "refused_count": refused,
            "skipped_count": skipped,
            "outcome_counts": counts,
            "resumed": resumed,
            "decided_in_this_call": decided_in_this_call,
            **delegation_details(context),
        },
    )


async def decide_review_batch(
    session: AsyncSession,
    *,
    context: SecurityContext,
    batch_id: UUID,
    decision: Literal["APPROVE", "REJECT"],
    reason: str | None,
    rationale_by_review_id: Mapping[UUID, str] | None = None,
    chunk_size: int = DECISION_CHUNK_SIZE,
) -> BatchDecisionResult:
    """Apply one decision to a frozen batch -- or resume one an earlier call started --
    re-checking every member first.

    Order of refusal for each member: excluded at freeze (SKIPPED) -> gone -> no longer
    pending -> evidence moved -> approve gate -> rationale -> the decision service (its own
    permission check, compare-and-set claim and adapter).

    **Commits.** Unlike the rest of this module, this function commits: once per chunk of
    `chunk_size` members, and once more to close the batch. That is the point -- see
    "Resumable decisions" in the module docstring. Anything the caller left uncommitted in
    the session is committed with the first chunk. If it raises part-way, every chunk
    committed before the failure stands and the caller must roll back the rest.
    """
    _refuse_agents(context)
    batch = await get_review_batch(session, context=context, batch_id=batch_id)
    if batch.created_by != context.principal_id:
        raise ReviewBatchError("REVIEW_BATCH_NOT_OWNED", 403)
    organization_id = batch.organization_id
    now = datetime.now(UTC)
    held = await _hold_batch(session, batch, decision, now, claim=True)
    resumed = not held
    if resumed:
        status, recorded_decision = await _batch_state(session, batch)
        if status == "DECIDED":
            raise ReviewBatchError("REVIEW_BATCH_ALREADY_DECIDED", 409)
        if recorded_decision != decision:
            raise ReviewBatchError("REVIEW_BATCH_DECISION_MISMATCH", 409)

    details: dict[UUID, str] = {}
    decided: set[UUID] = set()
    closed_here = False
    size = max(1, chunk_size)
    while True:
        if not held and not await _hold_batch(session, batch, decision, now, claim=False):
            # A concurrent caller of this same batch recorded its last member and closed it
            # while this one waited for the hold: there is nothing left to decide.
            break
        held = False
        pending = list(
            (
                await session.scalars(
                    select(ReviewBatchItem)
                    .where(
                        ReviewBatchItem.batch_id == batch.id,
                        ReviewBatchItem.organization_id == organization_id,
                        ReviewBatchItem.eligibility == "ELIGIBLE",
                        ReviewBatchItem.outcome == "PENDING",
                    )
                    .order_by(ReviewBatchItem.position)
                    .limit(size)
                )
            ).all()
        )
        if not pending:
            await _close_batch(
                session,
                context=context,
                batch=batch,
                decision=decision,
                now=now,
                resumed=resumed,
                decided_in_this_call=len(decided),
            )
            await session.commit()
            closed_here = True
            break
        decided |= await _decide_chunk(
            session,
            context=context,
            batch=batch,
            decision=decision,
            reason=reason,
            rationale_by_review_id=rationale_by_review_id,
            items=pending,
            now=now,
            details=details,
        )
        await session.commit()

    await session.refresh(batch)
    items = (
        await session.scalars(
            select(ReviewBatchItem)
            .where(
                ReviewBatchItem.batch_id == batch.id,
                ReviewBatchItem.organization_id == organization_id,
            )
            .order_by(ReviewBatchItem.position)
            .execution_options(populate_existing=True)
        )
    ).all()
    result = BatchDecisionResult(batch=batch, resumed=resumed)
    for item in items:
        result.outcomes.append(
            MemberOutcome(
                item,
                outcome_correction(item, batch.decision),
                details.get(item.id),
                outcome=item.outcome,
                reason_code=item.reason_code,
                subject_type=item.correction_subject_type,
                subject_id=item.correction_subject_id,
                decided_in_this_call=item.id in decided
                or (closed_here and item.eligibility == "EXCLUDED"),
            )
        )
    return result


def outcome_correction(item: ReviewBatchItem, decision: str | None) -> Correction:
    """The correction for a stored member, as the member listing reports it."""
    if item.outcome != "APPLIED":
        return correction_for(None, None, None, None)
    return correction_for(
        item.object_type, decision, item.correction_subject_type, item.correction_subject_id
    )


def http_error(error: ReviewBatchError) -> HTTPException:
    return HTTPException(status_code=error.http_status, detail=error.code)


def preview_evidence(member: ComposedMember) -> list[EvidenceItemRead]:
    return member.evidence[:EVIDENCE_PREVIEW_ITEMS]
