"""R11-MP08: the SQL-generation instruction as a governed, versioned AI asset.

The instruction Ask gives the model was a string literal in the orchestrator. It
could only change by a code change, and nothing could propose a better one from
evidence. It is now two parts:

* **`SQL_SAFETY_CLAUSE`** -- fixed, in code, always first: one read-only
  statement, catalogued identifiers only, no source values. No prompt version
  can remove or reorder it.
* **Guidance** -- an optional instruction held as a PROMPT-kind `AiAssetVersion`
  under `SQL_INSTRUCTION_ASSET_KEY`, proposed by the offline optimiser
  (`aida.prompt_optimizer`) or by a person, and used only once it is APPROVED
  through the ordinary AI asset review (maker-checker, `semantic_api`).

With no approved version, the instruction is exactly the safety clause -- the
string Ask has always sent.

A version's instruction lives in `runtime_evidence["instruction"]` beside its
SHA-256 (`instruction_sha256`). Approval and runtime both check the two agree,
so an instruction edited after it was scored and approved is not used.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AiAsset, AiAssetVersion

SQL_INSTRUCTION_ASSET_KEY: Final = "sql-generation-instruction"
PROMPT_ASSET_KIND: Final = "PROMPT"

#: The fixed part, always first. Byte-identical to the instruction Ask sent
#: before this module existed, so an estate with no approved prompt sees no change.
SQL_SAFETY_CLAUSE: Final = (
    "Return exactly one read-only SQL SELECT statement for the supplied "
    "dialect. "
    "Use only qualified tables, columns, and joins present in the supplied metadata "
    "context. Never invent an identifier or include source values."
)

#: Longest guidance a prompt version may carry. A guard against a runaway
#: optimiser as much as against a person: it all goes into every question's input.
MAX_GUIDANCE_CHARS: Final = 4_000


def instruction_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compose_instruction(guidance: str | None) -> str:
    """The system instruction for SQL generation: the safety clause, then guidance."""
    if not guidance or not guidance.strip():
        return SQL_SAFETY_CLAUSE
    return f"{SQL_SAFETY_CLAUSE} {guidance.strip()}"


def version_guidance(version: AiAssetVersion) -> str | None:
    """The guidance a PROMPT version carries, or None when it carries none that
    can be trusted: absent, empty, too long, or not what its fingerprint says."""
    evidence: dict[str, Any] = version.runtime_evidence or {}
    text = evidence.get("instruction")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_GUIDANCE_CHARS:
        return None
    if evidence.get("instruction_sha256") != instruction_sha256(text):
        return None
    return text


@dataclass(frozen=True, slots=True)
class ActiveInstruction:
    text: str
    version_id: UUID | None

    def evidence(self) -> dict[str, object]:
        return {
            "source": "APPROVED_PROMPT" if self.version_id else "SAFETY_CLAUSE_ONLY",
            "prompt_version_id": str(self.version_id) if self.version_id else None,
            "instruction_sha256": instruction_sha256(self.text),
        }


async def active_sql_instruction(session: AsyncSession, organization_id: UUID) -> ActiveInstruction:
    """The instruction Ask uses for this organization now: the newest APPROVED
    PROMPT version's guidance after the safety clause, or the clause alone."""
    rows = (
        await session.scalars(
            select(AiAssetVersion)
            .join(AiAsset, AiAsset.id == AiAssetVersion.asset_id)
            .where(
                AiAsset.organization_id == organization_id,
                AiAsset.asset_key == SQL_INSTRUCTION_ASSET_KEY,
                AiAsset.asset_kind == PROMPT_ASSET_KIND,
                AiAssetVersion.status == "APPROVED",
            )
            .order_by(AiAssetVersion.version.desc())
            .limit(1)
        )
    ).all()
    for version in rows:
        guidance = version_guidance(version)
        if guidance is not None:
            return ActiveInstruction(text=compose_instruction(guidance), version_id=version.id)
    return ActiveInstruction(text=SQL_SAFETY_CLAUSE, version_id=None)


#: What an approval requires of a PROMPT version's optimiser evidence.
MIN_EVALUATED_CASES: Final = 5


def prompt_approval_problem(version: AiAssetVersion) -> str | None:
    """Why this PROMPT version may not be approved, or None.

    Stored evidence cannot be recomputed here without model calls, so it is held
    to what it must show and to the text it was produced for: guidance present
    and matching its fingerprint; an optimiser result over at least
    `MIN_EVALUATED_CASES` cases that scored no worse than the baseline and made
    no unsafe statement; and that result computed for this exact instruction.
    """
    guidance = version_guidance(version)
    if guidance is None:
        return "the version carries no instruction matching its fingerprint"
    result = (version.evaluation_evidence or {}).get("prompt_optimizer")
    if not isinstance(result, dict):
        return "the version carries no optimiser evaluation"
    if result.get("instruction_sha256") != instruction_sha256(guidance):
        return "the evaluation was computed for a different instruction"
    cases = result.get("evaluated_cases")
    if not isinstance(cases, int) or cases < MIN_EVALUATED_CASES:
        return f"fewer than {MIN_EVALUATED_CASES} evaluated cases"
    baseline, candidate = result.get("baseline_mean"), result.get("candidate_mean")
    if not isinstance(baseline, int | float) or not isinstance(candidate, int | float):
        return "the evaluation has no baseline or candidate score"
    if candidate < baseline:
        return f"the instruction scored below the baseline ({candidate:.3f} < {baseline:.3f})"
    if result.get("unsafe_cases") != 0:
        return "the instruction produced an unsafe statement in evaluation"
    return None
