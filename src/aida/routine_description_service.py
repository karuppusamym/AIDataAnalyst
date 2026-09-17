"""R11-FP08: deterministic, evidence-scored routine description drafting.

The routine-level member of the description family. Tables have
`asset_description_service` (GL-9), columns have `column_description_service`;
a routine had nothing at all, so the only thing the platform could say about a
procedure was the source system's own comment, re-derived and overwritten by
every rescan. This module closes that, on exactly the contract the other two
carry -- restated here because it is the point of the module:

* **No model call.** Every sentence is composed from rows already in this
  database: the routine's kind and engine subkind, its signature and its
  parameter rows, its declared return type and execution semantics, the state
  of its captured body, and the tables a person's own parse says it reads and
  writes. Nothing is read into the routine's *name*: `sp_recalc` with no
  captured body and no lineage produces a thin draft that scores below the
  review bar, not a confident sentence about what it recalculates.
* **The score orders review; it never replaces it.** Publishing happens only
  through an independent APPROVE on the draft's `GovernanceReview`
  (`semantic_api._decide_routine_description_draft`), and a draft below
  `MINIMUM_EVIDENCE_FOR_REVIEW` -- the same single threshold tables and columns
  use -- cannot be submitted at all.
* **A draft cannot overwrite what it did not see.** `base_description_version`
  records the routine's description version when the draft was composed, and
  approval refuses if it has moved (`column_description_service`'s rule, which
  the table draft lacks and is worse for).

**Why this generalises the existing service rather than forking it.**
`asset_description_service` is already parameterised where it matters:
`refusal_reason`, `signals_fingerprint`, `text_fingerprint`, `ensure_reviewable`
and `ConfidenceBreakdown` are all kind-agnostic, and
`column_description_service` already reuses `refusal_reason` with its own signal
set. So this module defines `RoutineEvidence` and `ROUTINE_EVIDENCE_SIGNALS` and
reuses those helpers verbatim; a second copy of the refusal rule or the score
shape is how the three paths would drift into three different governance
contracts for one kind of fact. R11-S4 freezes new governed-artifact *families*;
this is the narrow extension of an existing one that the freeze permits, not the
broad consolidation into a polymorphic description store that it defers.

**The body is never quoted.** A procedure body is the largest
indirect-injection surface in the estate (see the note on
`MetadataRoutine.body_sql_redacted`), and a description is prose every reader of
the routine will read. `_BODY_SENTENCES` says what *state* the body is in and
nothing about its contents, exactly as `_DEFINITION_SENTENCES` does for a view.
The body-state vocabulary is the routine one rather than a copy of the view one:
`SOURCE_MISSING` and `SOURCE_RETIRED` come from
`tool_source_binding.current_source_definition`, the function that already
answers "does this bound routine still stand?", so the two cannot disagree.

**`PACKAGE` is out of scope, and refused by name.** Every other
routine-consuming path in the platform excludes it -- `footprint_gaps`,
`footprint_gap_detail` and `procedure_tool_blueprint` all do -- and the tracker
holds packages behind R11-FP03. A package is a container for subprograms, not a
callable unit with a signature, a return type or lineage of its own, so a
description of one would be a description of nothing. Refused with a reason
rather than silently skipped: a steward who asks for a description of a package
and gets an empty result has learned nothing about why.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    DEFINITION_MOVED,
    REFUSED_WITHDRAWN,
    ConfidenceBreakdown,
    refusal_reason,
    text_fingerprint,
)
from aida.envelope_models import (
    AVAILABLE,
    MetadataRoutine,
    MetadataRoutineDefinitionVersion,
    MetadataRoutineParameter,
    RoutineDescriptionDraft,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.ingest_screening import is_eligible_for_model_context
from aida.models import MetadataCatalog, MetadataSchema, MetadataTable
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.refusal import RefusalDetail
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES
from aida.tool_source_binding import (
    REASON_SOURCE_MISSING,
    REASON_SOURCE_RETIRED,
    SOURCE_KIND_ROUTINE,
    SourceBinding,
    current_source_definition,
)

#: `GovernanceReview.object_type` for a submitted routine draft. Registered in
#: `review_risk_tiers` (T0) and in `semantic_api._TARGET_EFFECT_ADAPTERS`; those
#: two plus `DescriptionWithdrawal.subject_type` are the only places the
#: description family actually discriminates on a subject kind.
ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE: Final = "ROUTINE_DESCRIPTION_DRAFT"

#: Statuses in which a draft is still a live proposal for its routine. At most
#: one draft per routine may be in either (`uq_routine_description_draft_open`).
OPEN_DRAFT_STATUSES: Final = ("DRAFT", "PENDING_APPROVAL")

#: Where a draft's text came from, recorded as `evidence["origin"]`. The same
#: vocabulary the other two paths use, so one reader can branch on all three. An
#: edit appends `_WITH_HUMAN_EDITS` and keeps the first half.
ORIGIN_METADATA: Final = "METADATA"

#: Ceiling on drafts one generation request may create. Refused, not sliced --
#: `column_description_api`'s rule, because a silently sliced result looks
#: complete.
GENERATE_ROUTINE_LIMIT: Final = 2_000

#: The routine kinds this module describes. `PACKAGE` is deliberately absent;
#: see the module docstring, and `ensure_routine_is_describable` below.
DESCRIBABLE_ROUTINE_TYPES: Final = frozenset({"PROCEDURE", "FUNCTION"})

#: Refused because a package is a container, not a callable unit.
PACKAGE_NOT_DESCRIBABLE: Final = "PACKAGE_NOT_DESCRIBABLE"

# R11-FP08: `DEFINITION_MOVED` is imported rather than redefined. The code a
# routine's drift refusal carries is deliberately the *same* one
# `asset_description_service` already raises for a view: it is the same fact
# about the same kind of object from a reader's point of view, and the UI maps a
# 409 from any description-draft endpoint through one classifier
# (`ui-next/src/lib/api/catalog.ts::classifyDescriptionDraftError`), which a
# second code for the same condition would have to learn.

#: How many neighbouring tables one query reads, and how many a sentence names.
_LINEAGE_QUERY_LIMIT: Final = 50
_LINEAGE_PROSE_LIMIT: Final = 3
#: An UNPARSED edge is a marker that a statement could not be read, not a claim
#: about a table, so it is not evidence of a read or a write.
_UNPARSED: Final = "UNPARSED"


# ---------------------------------------------------------------------------
# Body state: what Atlas holds of the routine's body, never the body itself.
# ---------------------------------------------------------------------------

#: The source gave the whole body, literal-redacted and screened clean.
BODY_CAPTURED: Final = "CAPTURED"
#: The source gave a prefix only.
BODY_TRUNCATED: Final = "TRUNCATED"
#: Captured, then set aside by prompt-risk screening.
BODY_QUARANTINED: Final = "QUARANTINED"
#: The source declined to hand the body over at all.
BODY_WITHHELD: Final = "WITHHELD"
#: The source answered and handed over no body text at all.
BODY_NOT_CAPTURED: Final = "NOT_CAPTURED"

#: R11-FP08: what a routine's body state lets a reader rely on. Each sentence
#: describes the body's *availability*; none quotes it, so no redacted constant
#: and no fragment of a statement is ever put back into prose a model will read.
#: Modelled on `asset_description_service._DEFINITION_SENTENCES`.
_BODY_SENTENCES: Final[Mapping[str, str]] = {
    BODY_CAPTURED: (
        "Its body was captured from the source, so the tables it reads and writes are read "
        "from the body itself."
    ),
    BODY_TRUNCATED: (
        "The source gave only part of its body, so the tables named below may be incomplete."
    ),
    BODY_QUARANTINED: (
        "Its captured body was set aside by prompt-risk screening and is not read for lineage."
    ),
    BODY_WITHHELD: (
        "The source withholds its body from the scanning principal, so what it touches cannot "
        "be confirmed here."
    ),
    BODY_NOT_CAPTURED: "Its body has not been captured from the source.",
}

#: Portable routine kinds, as a reader would say them.
_ROUTINE_NOUNS: Final[Mapping[str, str]] = {
    "PROCEDURE": "stored procedure",
    "FUNCTION": "function",
}


def routine_body_facts(routine: MetadataRoutine) -> tuple[str, str | None]:
    """The routine's body state, and a digest of the stored value-free text.

    The four predicates `asset_description_service._definition_facts` reads --
    `availability`, `truncated`, `redaction_status` against
    `VALUE_FREE_REDACTION_STATUSES`, and `screening_status` through
    `is_eligible_for_model_context` -- applied to a routine's own
    `body_sql_redacted` / `body_fingerprint` instead of a view definition row.
    The order matters and is the same: what the source refused comes before what
    screening set aside, because "we were not allowed to look" and "we looked
    and quarantined it" are different facts about the same absence.

    `NOT_CAPTURED` is the one state a routine has to derive differently, and the
    difference is forced by a database constraint rather than chosen. A view's
    definition lives in its own row, so the row's absence *is* the state. A
    routine's body lives on the routine, and
    `ck_metadata_routine_availability_matches_body` makes
    `availability = 'AVAILABLE'` exactly equivalent to a non-NULL
    `body_sql_redacted` -- so "AVAILABLE with a NULL body" cannot exist, and the
    view's NOT_CAPTURED has no direct analogue. What *can* exist is the fourth
    row of this module's own encoding table (see `MetadataViewDefinition`'s
    docstring): AVAILABLE with an empty string, the source answering and handing
    over no body text. That is this state. The NULL branch below is kept so the
    function is total rather than relying on the constraint being present in
    every dialect a test might build.

    The digest is of the *stored* text (value-free), never of the
    literal-bearing original: `body_fingerprint` is that, and stays internal.
    """
    if routine.availability != AVAILABLE:
        return BODY_WITHHELD, None
    stored = routine.body_sql_redacted
    if not stored:
        # No text held: nothing to quarantine, truncate or digest.
        return BODY_NOT_CAPTURED, None
    digest = (
        hashlib.sha256(stored.encode("utf-8")).hexdigest()
        if routine.redaction_status in VALUE_FREE_REDACTION_STATUSES
        else None
    )
    if not is_eligible_for_model_context(routine.screening_status):
        return BODY_QUARANTINED, digest
    return (BODY_TRUNCATED if routine.truncated else BODY_CAPTURED), digest


def is_describable_routine(routine: MetadataRoutine) -> bool:
    """False only for a package. The predicate behind `ensure_routine_is_describable`.

    Split out so a batch caller can name *every* offending id in one refusal
    rather than raising on the first: `column_description_api`'s rule that a
    request is refused whole rather than silently reduced only works if the
    refusal can list what to drop.
    """
    return routine.routine_type.strip().upper() != "PACKAGE"


def ensure_routines_are_describable(routines: Iterable[MetadataRoutine]) -> None:
    """Refuse a routine kind this module does not describe, by name.

    Only `PACKAGE` is refused today, and it is refused rather than skipped: a
    request naming a package and getting an empty result has learned nothing
    about why. It is also refused *whole* rather than sliced -- the refusal
    lists every offending id, so the client can drop them and ask again, which
    is `column_description_api`'s own rule that a silently reduced result looks
    complete. An unrecognised kind from a future connector is *not* refused
    here: a procedure by another name still has a signature, parameters and
    lineage, and the score thins out on its own if it has none of them.

    The message lives here and not in the router so one sentence answers the
    question wherever it is asked.
    """
    refused = [routine for routine in routines if not is_describable_routine(routine)]
    if refused:
        raise HTTPException(
            status_code=422,
            detail=RefusalDetail(
                code=PACKAGE_NOT_DESCRIBABLE,
                message=(
                    "A package is a container for subprograms, not a callable unit with a "
                    "signature, a return type or lineage of its own, so there is nothing here "
                    "to describe. Drop these ids and describe the package's procedures and "
                    "functions individually. Packages themselves wait on R11-FP03."
                ),
                routine_ids=[str(routine.id) for routine in refused],
            ),
        )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutineEvidence:
    """Value-free, DB-derived signals about one routine. No body, no values."""

    routine_id: UUID
    datasource_id: UUID
    routine_name: str
    qualified_name: str
    schema_name: str
    routine_type: str
    native_subtype: str | None
    language: str | None
    #: The declared parameter list as the source states it. Already treated as
    #: releasable elsewhere (`context_product_coverage`, MCP's transformation
    #: detail), unlike a parameter's `default_expression`, which is
    #: literal-bearing and is only ever counted here.
    signature: str
    parameter_names: tuple[str, ...]
    parameter_count: int
    output_parameter_count: int
    defaulted_parameter_count: int
    return_type: str | None
    is_deterministic: bool | None
    security_mode: str | None
    #: The source system's own comment. Evidence, never authority -- the same
    #: standing `MetadataObjectDescription`'s docstring gives a source comment.
    source_description: str | None
    #: What Atlas holds of the body, and a digest of the stored value-free text.
    body_state: str
    body_digest: str | None
    #: The captured definition this draft was written against, when there is
    #: one. A named immutable row, which is why drift is a version comparison
    #: here rather than a re-derived digest (see `routine_definition_moved`).
    source_definition_version_id: UUID | None
    #: Tables a person's own parse says it reads and writes, in this datasource.
    reads_table_names: tuple[str, ...]
    writes_table_names: tuple[str, ...]
    lineage_edge_ids: tuple[UUID, ...]
    current_description_version: int | None

    @property
    def lineage_edge_count(self) -> int:
        return len(self.lineage_edge_ids)

    @property
    def has_declared_interface(self) -> bool:
        return self.parameter_count > 0 or bool(self.return_type)


#: The keys `routine_evidence_payload` writes: the signals a routine draft
#: stands on. Who drafted it, the run, the rank and any edit history are not
#: evidence, so they never make two otherwise identical proposals look
#: different (R11-FP10's rule, applied to the third draft kind).
ROUTINE_EVIDENCE_SIGNALS: Final = frozenset(
    {
        "routine_type",
        "native_subtype",
        "language",
        "signature",
        "parameter_count",
        "output_parameter_count",
        "defaulted_parameter_count",
        "return_type",
        "is_deterministic",
        "security_mode",
        "source_description_present",
        "body_state",
        "body_digest",
        "source_definition_version_id",
        "reads_table_names",
        "writes_table_names",
        "lineage_edge_ids",
        "base_description_version",
    }
)


def routine_evidence_payload(evidence: RoutineEvidence) -> dict[str, Any]:
    """JSON-safe evidence record: the raw signals a draft was built from.

    `source_description_present` rather than the comment itself, exactly as the
    table and column payloads record `dbt_description_present`: the payload is
    matched against earlier refusals, and a source comment is text the source
    may reword without changing what the proposal stands on.
    """
    return {
        "routine_type": evidence.routine_type,
        "native_subtype": evidence.native_subtype,
        "language": evidence.language,
        "signature": evidence.signature,
        "parameter_count": evidence.parameter_count,
        "output_parameter_count": evidence.output_parameter_count,
        "defaulted_parameter_count": evidence.defaulted_parameter_count,
        "return_type": evidence.return_type,
        "is_deterministic": evidence.is_deterministic,
        "security_mode": evidence.security_mode,
        "source_description_present": bool(evidence.source_description),
        "body_state": evidence.body_state,
        "body_digest": evidence.body_digest,
        "source_definition_version_id": (
            str(evidence.source_definition_version_id)
            if evidence.source_definition_version_id
            else None
        ),
        "reads_table_names": list(evidence.reads_table_names),
        "writes_table_names": list(evidence.writes_table_names),
        "lineage_edge_ids": [str(value) for value in evidence.lineage_edge_ids],
        "base_description_version": evidence.current_description_version,
    }


def score_routine_evidence(evidence: RoutineEvidence) -> ConfidenceBreakdown:
    """Score drafted routine evidence on the four shared dimensions.

    `ConfidenceBreakdown` is `asset_description_service`'s, unchanged, so one
    reviewer's queue compares three kinds of draft on one scale. Each sub-score
    is a monotone function of the evidence: more corroboration always scores at
    or above less, and there is no learned weight and no external call.

    - accuracy: how much of the draft rests on something other than the
      routine's declaration -- the source's own comment, a person-approved
      parse of its body, a body fully held.
    - clarity: how much readable context (as opposed to a bare signature) the
      draft can carry.
    - style: how well-formed the draft can be -- a real declared interface, and
      something concrete to say about what it touches.
    - completeness: the fraction of the evidence categories this codebase
      tracks for a routine that are actually present.
    """
    touches = bool(evidence.reads_table_names) or bool(evidence.writes_table_names)
    categories = (
        # A declared interface: parameters, or a return type for a function.
        evidence.has_declared_interface,
        # A routine declares no keys; a fully held body is its structural
        # evidence -- the line `score_evidence` draws for a view's definition.
        evidence.body_state == BODY_CAPTURED,
        evidence.lineage_edge_count > 0,
        bool(evidence.source_description),
        # Execution semantics the engine states rather than Atlas inferring.
        evidence.is_deterministic is not None or bool(evidence.security_mode),
    )
    completeness = sum(1 for present in categories if present) / len(categories)

    accuracy = 0.4
    if evidence.source_description:
        accuracy += 0.25
    if touches:
        accuracy += 0.25
    if evidence.body_state == BODY_CAPTURED:
        accuracy += 0.10
    accuracy = min(accuracy, 1.0)

    clarity = 0.25
    if evidence.source_description:
        clarity += 0.35
    if evidence.writes_table_names:
        clarity += 0.20
    if evidence.body_state in (BODY_CAPTURED, BODY_TRUNCATED):
        clarity += 0.20
    clarity = min(clarity, 1.0)

    style = 0.30
    if evidence.signature.strip():
        style += 0.25
    if evidence.parameter_count >= 1:
        style += 0.20
    if evidence.lineage_edge_count > 0:
        style += 0.25
    style = min(style, 1.0)

    overall = round((accuracy + clarity + style + completeness) / 4, 4)
    return ConfidenceBreakdown(
        accuracy=round(accuracy, 4),
        clarity=round(clarity, 4),
        style=round(style, 4),
        completeness=round(completeness, 4),
        overall=overall,
    )


def compose_routine_draft_text(evidence: RoutineEvidence) -> str:
    """Assemble readable prose entirely from evidence fields. No model call.

    Nothing here quotes the body, a parameter default, or any other
    literal-bearing text. What the routine *is* comes from its declaration; what
    it *does* comes from a person-approved parse of its body; and what Atlas
    does not know is said rather than left out, which is the whole reason
    `_BODY_SENTENCES` exists.
    """
    noun = _ROUTINE_NOUNS.get(evidence.routine_type.strip().upper(), "routine")
    sentences = [
        f"{evidence.routine_name} is a {noun} in the {evidence.schema_name} schema."
    ]
    if evidence.native_subtype:
        sentences.append(f"The source records its kind as {evidence.native_subtype}.")
    if evidence.signature.strip():
        sentences.append(f"It is declared as {evidence.signature.strip()}.")
    if evidence.parameter_count:
        parameter_word = "parameter" if evidence.parameter_count == 1 else "parameters"
        named = ", ".join(evidence.parameter_names[:_LINEAGE_PROSE_LIMIT])
        listing = f" ({named})" if named else ""
        sentences.append(
            f"It takes {evidence.parameter_count} {parameter_word}{listing}."
        )
    if evidence.output_parameter_count:
        output_word = "parameter" if evidence.output_parameter_count == 1 else "parameters"
        sentences.append(
            f"{evidence.output_parameter_count} of them return values to the caller as "
            f"output {output_word}."
        )
    if evidence.return_type:
        sentences.append(f"It returns {evidence.return_type}.")
    if evidence.is_deterministic is not None:
        sentences.append(
            "The source declares it deterministic."
            if evidence.is_deterministic
            else "The source declares it non-deterministic."
        )
    if evidence.security_mode:
        sentences.append(f"It executes under {evidence.security_mode} rights.")
    if evidence.body_state in _BODY_SENTENCES:
        sentences.append(_BODY_SENTENCES[evidence.body_state])
    if evidence.reads_table_names:
        sentences.append(
            "It reads from "
            + ", ".join(evidence.reads_table_names[:_LINEAGE_PROSE_LIMIT])
            + "."
        )
    if evidence.writes_table_names:
        sentences.append(
            "It writes to "
            + ", ".join(evidence.writes_table_names[:_LINEAGE_PROSE_LIMIT])
            + "."
        )
    if evidence.source_description:
        sentences.append(f"The source describes it as: {evidence.source_description.strip()}")
    return " ".join(sentences)


async def _qualified_name(session: AsyncSession, routine: MetadataRoutine) -> tuple[str, str]:
    """`catalog.schema.routine`, and the schema name on its own."""
    row = (
        await session.execute(
            select(MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(MetadataSchema.id == routine.schema_id)
        )
    ).first()
    if row is None:
        return routine.name, "unknown"
    schema_name, catalog_name = str(row[0]), str(row[1])
    return f"{catalog_name}.{schema_name}.{routine.name}", schema_name


async def _current_definition_version_id(
    session: AsyncSession, routine_id: UUID
) -> UUID | None:
    """The newest captured definition of this routine, if one was ever captured.

    `MetadataRoutineDefinitionVersion` is immutable and append-only, so the
    highest `version_number` is the body as it stands. Nothing here reads the
    stored text: the id is the whole point (see `routine_definition_moved`).
    """
    version_id: UUID | None = await session.scalar(
        select(MetadataRoutineDefinitionVersion.id)
        .where(MetadataRoutineDefinitionVersion.routine_id == routine_id)
        .order_by(MetadataRoutineDefinitionVersion.version_number.desc())
        .limit(1)
    )
    return version_id


async def _lineage_neighbours(
    session: AsyncSession, routine: MetadataRoutine
) -> tuple[list[str], list[str], list[UUID]]:
    """The tables this routine is parsed as reading, and those it writes.

    Only `ACTIVE` edges -- approved by a person, or activated by a person's own
    parse -- between this routine and a table of its *own* datasource. A
    `PROPOSED` edge, such as every edge the lineage agent writes until someone
    approves it, is not evidence yet; a `REJECTED` one is evidence of nothing;
    an `UNPARSED` marker is a statement nobody could read rather than a claim
    about a table. A hop into a temp table is the procedure's own plumbing, so
    `is_intermediate` rows are excluded -- the filter
    `asset_description_service._parsed_lineage_neighbours` already applies to
    the same table.

    Naming a table in another datasource would disclose it past the per-read
    cross-source grant check (ADR-0017), which is the line both sibling services
    draw.
    """
    rows = (
        await session.execute(
            select(
                DeepProcedureLineageEdge.id,
                DeepProcedureLineageEdge.is_write,
                MetadataTable.name,
            )
            .join(
                MetadataTable,
                MetadataTable.id
                == func.coalesce(
                    DeepProcedureLineageEdge.target_table_id,
                    DeepProcedureLineageEdge.source_table_id,
                ),
            )
            .where(
                DeepProcedureLineageEdge.routine_id == routine.id,
                DeepProcedureLineageEdge.organization_id == routine.organization_id,
                DeepProcedureLineageEdge.review_status == "ACTIVE",
                DeepProcedureLineageEdge.is_intermediate.is_(False),
                DeepProcedureLineageEdge.transformation_type != _UNPARSED,
                MetadataTable.datasource_id == routine.datasource_id,
            )
            .order_by(MetadataTable.name, DeepProcedureLineageEdge.id)
            .limit(_LINEAGE_QUERY_LIMIT)
        )
    ).all()
    reads: list[str] = []
    writes: list[str] = []
    edge_ids: list[UUID] = []
    for edge_id, is_write, name in rows:
        edge_ids.append(edge_id)
        bucket = writes if is_write else reads
        if name not in bucket:
            bucket.append(name)
    return reads, writes, edge_ids


async def gather_routine_evidence(
    session: AsyncSession, routine: MetadataRoutine
) -> RoutineEvidence:
    """Collect value-free evidence for `routine` from data already in this DB."""
    qualified_name, schema_name = await _qualified_name(session, routine)
    parameters = (
        await session.execute(
            select(
                MetadataRoutineParameter.name,
                MetadataRoutineParameter.mode,
                MetadataRoutineParameter.default_expression,
            )
            .where(
                MetadataRoutineParameter.routine_id == routine.id,
                MetadataRoutineParameter.status == "ACTIVE",
            )
            .order_by(MetadataRoutineParameter.ordinal_position)
        )
    ).all()
    # A parameter's `default_expression` is literal-bearing, so it is counted
    # and never quoted -- the same treatment the body gets.
    parameter_names = tuple(str(name) for name, _mode, _default in parameters if name)
    output_parameter_count = sum(
        1 for _name, mode, _default in parameters if str(mode).upper() in ("OUT", "INOUT")
    )
    defaulted_parameter_count = sum(
        1 for _name, _mode, default in parameters if default is not None
    )
    body_state, body_digest = routine_body_facts(routine)
    reads, writes, edge_ids = await _lineage_neighbours(session, routine)
    current = await current_routine_description(session, routine.id)
    return RoutineEvidence(
        routine_id=routine.id,
        datasource_id=routine.datasource_id,
        routine_name=routine.name,
        qualified_name=qualified_name,
        schema_name=schema_name,
        routine_type=routine.routine_type,
        native_subtype=routine.native_subtype,
        language=routine.language,
        signature=routine.signature,
        parameter_names=parameter_names,
        parameter_count=len(parameters),
        output_parameter_count=output_parameter_count,
        defaulted_parameter_count=defaulted_parameter_count,
        return_type=routine.return_type,
        is_deterministic=routine.is_deterministic,
        security_mode=routine.security_mode,
        source_description=routine.source_description,
        body_state=body_state,
        body_digest=body_digest,
        source_definition_version_id=await _current_definition_version_id(session, routine.id),
        reads_table_names=tuple(reads),
        writes_table_names=tuple(writes),
        lineage_edge_ids=tuple(edge_ids),
        current_description_version=current.version if current is not None else None,
    )


# ---------------------------------------------------------------------------
# R11-FP10: a refused proposal does not come back unchanged.
# ---------------------------------------------------------------------------


async def rejected_routine_drafts(
    session: AsyncSession, routine_ids: Iterable[UUID]
) -> dict[UUID, list[tuple[str, Mapping[str, Any]]]]:
    """(text fingerprint, evidence) of every REJECTED draft, per routine, in one read."""
    ids = list(routine_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(
            RoutineDescriptionDraft.routine_id,
            RoutineDescriptionDraft.text_fingerprint,
            RoutineDescriptionDraft.evidence,
        ).where(
            RoutineDescriptionDraft.routine_id.in_(ids),
            RoutineDescriptionDraft.status == "REJECTED",
        )
    )
    refused: dict[UUID, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for routine_id, fingerprint, evidence in rows.all():
        refused[routine_id].append((fingerprint, evidence or {}))
    return dict(refused)


def routine_refusal(
    drafted_text: str,
    payload: Mapping[str, Any],
    refused: Iterable[tuple[str, Mapping[str, Any] | None]],
) -> str | None:
    """`asset_description_service.refusal_reason`, over routine evidence."""
    return refusal_reason(
        drafted_text=drafted_text,
        payload=payload,
        refused=refused,
        signal_keys=ROUTINE_EVIDENCE_SIGNALS,
    )


async def routine_refusal_reason(
    session: AsyncSession,
    routine_id: UUID,
    *,
    drafted_text: str,
    payload: Mapping[str, Any],
) -> str | None:
    """`routine_refusal` against this routine's REJECTED drafts, then its
    WITHDRAWN descriptions -- the two-step `table_refusal` performs."""
    refused = await rejected_routine_drafts(session, [routine_id])
    reason = routine_refusal(drafted_text, payload, refused.get(routine_id, []))
    if reason is not None:
        return reason
    withdrawn = (
        await session.scalars(
            select(RoutineDocumentationVersion.description)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id == routine_id,
                RoutineDocumentationVersion.status == "WITHDRAWN",
            )
        )
    ).all()
    digest = text_fingerprint(drafted_text)
    if any(text_fingerprint(description) == digest for description in withdrawn):
        return REFUSED_WITHDRAWN
    return None


# ---------------------------------------------------------------------------
# The store: read, publish, resolve.
# ---------------------------------------------------------------------------


async def current_routine_description(
    session: AsyncSession, routine_id: UUID
) -> RoutineDocumentationVersion | None:
    """The approved description of one routine, if it has one."""
    version: RoutineDocumentationVersion | None = await session.scalar(
        select(RoutineDocumentationVersion)
        .join(
            RoutineDocumentation,
            RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
        )
        .where(
            RoutineDocumentation.routine_id == routine_id,
            RoutineDocumentationVersion.status == "APPROVED",
        )
        .order_by(RoutineDocumentationVersion.version.desc())
        .limit(1)
    )
    return version


async def current_routine_descriptions(
    session: AsyncSession, routine_ids: Sequence[UUID]
) -> dict[UUID, RoutineDocumentationVersion]:
    """The approved description per routine, in one read.

    The batched form every read surface needs, so a page of routines costs one
    query rather than one per row -- `column_documentation`'s
    `current_descriptions_by_column_id` shape.
    """
    if not routine_ids:
        return {}
    rows = (
        await session.execute(
            select(RoutineDocumentationVersion, RoutineDocumentation.routine_id)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id.in_(list(routine_ids)),
                RoutineDocumentationVersion.status == "APPROVED",
            )
            .order_by(RoutineDocumentationVersion.version)
        )
    ).all()
    # Ascending, so the last write per routine wins -- the newest approved one.
    return {routine_id: version for version, routine_id in rows}


async def latest_withdrawn_routine_version(
    session: AsyncSession, routine_id: UUID
) -> RoutineDocumentationVersion | None:
    """The most recently withdrawn description for one routine, if any.

    Lets a read surface say "this was described, and the description was
    retired" instead of silently reverting to looking never-documented -- which
    would make a withdrawal indistinguishable from a routine nobody has reached
    yet. The table-level counterpart is
    `description_withdrawal.latest_withdrawn_table_version`.
    """
    rows = (
        await session.scalars(
            select(RoutineDocumentationVersion)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id == routine_id,
                RoutineDocumentationVersion.status == "WITHDRAWN",
            )
            .order_by(RoutineDocumentationVersion.version)
        )
    ).all()
    return rows[-1] if rows else None


@dataclass(frozen=True, slots=True)
class ResolvedRoutineDescription:
    """The one description a routine read surface should show, and its standing."""

    text: str | None
    #: True when `text` is a draft nobody has approved. Never presented as
    #: something the platform asserts.
    is_proposed: bool
    #: True when this routine had an approved description and a reviewer retired
    #: it, so a reader can be told that rather than shown a blank.
    is_withdrawn: bool
    #: True when `text` is the source system's own comment rather than authored
    #: Atlas content.
    is_source_comment: bool


async def resolve_routine_description(
    session: AsyncSession, routine: MetadataRoutine
) -> ResolvedRoutineDescription:
    """The catalog precedence chain, for a routine.

    Modelled rung for rung on `atlas.modules.catalog.service._description`, which
    is where this platform already settled what a detail read shows when several
    things could speak:

    * an **approved** `RoutineDocumentationVersion` wins -- it is what this
      platform asserts;
    * else a **PENDING_APPROVAL** draft shows, flagged `is_proposed`, because it
      is carried as a proposal and never as an assertion;
    * else `MetadataRoutine.source_description` shows. It deliberately still
      shows after a withdrawal, for the reason the table chain gives: it is not
      this platform speaking, it is the source system's own comment, re-derived
      by every rediscovery pass, and it is what this field carried before anyone
      here described the routine. Withdrawal returns the routine to that state
      rather than suppressing observed source metadata Atlas has no authority
      over.

    The rung the table chain has and this one does not is the business
    annotation: `MetadataBusinessAnnotation` is keyed by `table_id` and a
    routine has no annotation, so there is nothing to skip and no
    `documentation_withdrawn` suppression to apply. `is_withdrawn` is still
    reported, because "described once, retired" is a different answer from
    "never described".
    """
    approved = await current_routine_description(session, routine.id)
    if approved is not None:
        return ResolvedRoutineDescription(approved.description, False, False, False)
    withdrawn = await latest_withdrawn_routine_version(session, routine.id)
    pending: RoutineDescriptionDraft | None = await session.scalar(
        select(RoutineDescriptionDraft)
        .where(
            RoutineDescriptionDraft.routine_id == routine.id,
            RoutineDescriptionDraft.status == "PENDING_APPROVAL",
        )
        .order_by(RoutineDescriptionDraft.created_at.desc())
        .limit(1)
    )
    if pending is not None:
        return ResolvedRoutineDescription(
            pending.drafted_text, True, withdrawn is not None, False
        )
    return ResolvedRoutineDescription(
        routine.source_description, False, withdrawn is not None, True
    )


async def publish_routine_documentation_version(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    routine_id: UUID,
    description: str,
    created_by: str,
    approved_by: str,
    approved_at: datetime,
    source_definition_version_id: UUID | None = None,
) -> RoutineDocumentationVersion:
    """Publish `description` as the routine's new current documentation version.

    Copied in shape from `publish_asset_documentation_version`, and extracted
    rather than inlined for the same reason: a second approval route already
    needs it (a reinstatement, `description_withdrawal._apply_reinstatement`),
    and two append-and-supersede implementations for one store is how they
    drift.

    Append-only: the prior `APPROVED` row moves to `SUPERSEDED` in this same
    transaction and is never edited for content.
    """
    documentation = await session.scalar(
        select(RoutineDocumentation).where(RoutineDocumentation.routine_id == routine_id)
    )
    if documentation is None:
        documentation = RoutineDocumentation(
            organization_id=organization_id,
            datasource_id=datasource_id,
            routine_id=routine_id,
        )
        session.add(documentation)
        await session.flush()
    latest_version = await session.scalar(
        select(func.max(RoutineDocumentationVersion.version)).where(
            RoutineDocumentationVersion.documentation_id == documentation.id
        )
    )
    await session.execute(
        update(RoutineDocumentationVersion)
        .where(
            RoutineDocumentationVersion.documentation_id == documentation.id,
            RoutineDocumentationVersion.status == "APPROVED",
        )
        .values(status="SUPERSEDED", updated_at=approved_at)
    )
    version = RoutineDocumentationVersion(
        organization_id=organization_id,
        documentation_id=documentation.id,
        version=(latest_version or 0) + 1,
        status="APPROVED",
        description=description,
        source_definition_version_id=source_definition_version_id,
        created_by=created_by,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    session.add(version)
    await session.flush()
    return version


# ---------------------------------------------------------------------------
# Drift, and the one path that publishes a draft.
# ---------------------------------------------------------------------------


async def routine_definition_moved(
    session: AsyncSession, draft: RoutineDescriptionDraft
) -> RefusalDetail | None:
    """Why a draft no longer describes its routine as it is, or `None`.

    `definition_moved`'s routine twin, and deliberately a *stronger* check than
    the view one rather than a copy of it. A view has no immutable
    definition-version table, so `definition_moved` has to re-derive the state
    and re-hash the stored text and compare both. A routine does have one
    (`MetadataRoutineDefinitionVersion`), so the comparison here is the **named
    version id**: one integer-width comparison instead of a hash of the whole
    body, and it cannot be fooled by a retire-and-recapture cycle that happens
    to produce byte-identical text -- which a digest comparison would call
    unchanged.

    Whether the routine still stands at all is asked of
    `tool_source_binding.current_source_definition`, the function that already
    answers exactly that for a bound tool, so a retired or removed routine
    cannot mean one thing to a tool and another to a description.

    A draft recording no `body_state` -- written before this was recorded -- is
    never refused here, matching the view rule.
    """
    evidence = draft.evidence or {}
    if "body_state" not in evidence:
        return None
    binding = SourceBinding(SOURCE_KIND_ROUTINE, draft.routine_id)
    current = await current_source_definition(session, draft.organization_id, binding)
    if current.reason is not None:
        return RefusalDetail(
            code=DEFINITION_MOVED,
            message=(
                "The routine this description was drafted for has been retired or removed, so "
                "there is nothing left for it to describe. Reject it."
                if current.reason == REASON_SOURCE_RETIRED
                else "The routine this description was drafted for is no longer in this "
                "organization's catalog. Reject it."
            ),
            drafted_definition_state=evidence.get("body_state"),
            current_definition_state=current.reason,
        )
    routine = await session.get(MetadataRoutine, draft.routine_id)
    if routine is None:
        # `current_source_definition` already reported SOURCE_MISSING for this;
        # the re-read keeps the type honest rather than asserting past it.
        return RefusalDetail(
            code=DEFINITION_MOVED,
            message="The routine this description was drafted for no longer exists. Reject it.",
            drafted_definition_state=evidence.get("body_state"),
            current_definition_state=REASON_SOURCE_MISSING,
        )
    state, _digest = routine_body_facts(routine)
    version_id = await _current_definition_version_id(session, routine.id)
    drafted = (
        evidence.get("body_state"),
        evidence.get("source_definition_version_id"),
    )
    if drafted == (state, str(version_id) if version_id else None):
        return None
    return RefusalDetail(
        code=DEFINITION_MOVED,
        message=(
            "The routine's body changed after this description was drafted, so the draft may "
            "describe a routine that no longer exists. Reject it and draft again from the "
            "current body."
        ),
        drafted_definition_state=drafted[0],
        current_definition_state=state,
    )


def _version_label(version: int | None) -> str:
    return "no description" if version is None else f"v{version}"


async def apply_routine_description_draft(
    session: AsyncSession,
    draft: RoutineDescriptionDraft,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, RoutineDocumentationVersion]:
    """Publish an approved draft as the routine's new current description.

    Called only from `semantic_api._decide_routine_description_draft`, after the
    shared maker-checker guard (status PENDING, requester independent of the
    approver) has already passed. There is no other call site that can move a
    routine draft to APPROVED, and there is no direct-publish endpoint.

    Three refusals of its own, all 409 so the review stays PENDING and the
    reviewer can reject it instead:

    * the routine must still be ACTIVE and present -- a draft about a dropped
      procedure has nothing left to describe (`ensure_reviewable`'s sibling
      check in `apply_column_description_draft`);
    * the body it was written against must still be the current one
      (`routine_definition_moved`), enforced exactly where
      `apply_asset_description_draft` enforces the view equivalent;
    * the routine's description must still be the version the draft was composed
      against, or someone published, retired or republished in the meantime and
      approving would silently replace text this draft never saw. Retirement
      counts: it is a decision, not an absence.
    """
    if draft.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="draft is no longer pending review")
    routine = await session.get(MetadataRoutine, draft.routine_id)
    if routine is None or routine.status != "ACTIVE":
        raise HTTPException(
            status_code=409, detail="the routine this draft describes is no longer active"
        )
    # R11-FP08: a routine's draft is published only while the body it describes stands.
    moved = await routine_definition_moved(session, draft)
    if moved is not None:
        raise HTTPException(status_code=409, detail=moved)
    current = await current_routine_description(session, draft.routine_id)
    current_version = current.version if current is not None else None
    if current_version != draft.base_description_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "this routine's description changed after the draft was composed "
                f"({_version_label(draft.base_description_version)} -> "
                f"{_version_label(current_version)}); reject this draft and generate a new one"
            ),
        )
    version = await publish_routine_documentation_version(
        session,
        organization_id=draft.organization_id,
        datasource_id=draft.datasource_id,
        routine_id=draft.routine_id,
        description=draft.drafted_text,
        created_by=draft.created_by,
        approved_by=reviewer,
        approved_at=now,
        source_definition_version_id=await _current_definition_version_id(session, routine.id),
    )
    draft.status = "APPROVED"
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    draft.published_version_id = version.id
    return "routine_description.approved.v1", version


async def reject_routine_description_draft(
    draft: RoutineDescriptionDraft,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a draft. Retained, not deleted, as negative knowledge: the next
    generation for this routine skips text -- or evidence -- identical to a
    rejected draft (`routine_refusal_reason`)."""
    if draft.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="draft is no longer pending review")
    draft.status = "REJECTED"
    draft.reviewed_by = reviewer
    draft.reviewed_at = now
    return "routine_description.rejected.v1"
