"""AT-6: resolve an `AgentRun`'s stored grounding-fragment digests back to the
exact content they were computed from.

`AgentRun.grounding_fragment_digests` (populated in
`agent_orchestrator._compute_grounding_fragment_digests`) is value-free: a
SHA-256 digest per fragment, never the fragment's content. This module is the
other half of the receipt -- given a run, resolve each `BUSINESS_ANNOTATION`
fragment's `annotation_version_id` back to its `MetadataBusinessAnnotationVersion`
row (which is never mutated for content, only ever superseded -- see
`business_annotation_versions.py`) and recompute the digest from that row's
*current* stored content to prove it still matches what was recorded on the
run. A mismatch would mean the "immutable" version row was tampered with;
under normal operation every entry matches by construction.

This is the proof the AT-6 exit criterion asks for: an `AgentRun` can be
replayed against exactly the content it was grounded on, not against
whatever the live annotation happens to say today.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import (
    annotation_version_content_digest,
    resolve_annotation_version,
)
from aida.models import AgentRun, MetadataBusinessAnnotationVersion


@dataclass(frozen=True, slots=True)
class ResolvedGroundingFragment:
    """One `grounding_fragment_digests` entry, resolved back to source content
    where that content is versioned (`BUSINESS_ANNOTATION` fragments today).
    """

    object_type: str
    object_id: str
    fragment_digest: str
    annotation_version_id: str | None
    # None for a non-BUSINESS_ANNOTATION fragment, or if the version row is
    # somehow gone (should not happen -- CASCADE only fires on the parent
    # annotation being deleted, which nothing in this codebase does).
    resolved_annotation_version: MetadataBusinessAnnotationVersion | None
    # True iff the version row was resolved AND its current content still
    # hashes to `fragment_digest` -- the actual replay proof for this fragment.
    digest_verified: bool
    # The version's status *now* -- "APPROVED" if it is still the live
    # content, "SUPERSEDED" if a later approval has since replaced it. Either
    # way `resolved_annotation_version` carries the exact content this run saw.
    current_status: str | None


def _verify_business_annotation_digest(
    version: MetadataBusinessAnnotationVersion, fragment_digest: str
) -> bool:
    return annotation_version_content_digest(version) == fragment_digest


async def resolve_grounding(
    session: AsyncSession, agent_run: AgentRun
) -> list[ResolvedGroundingFragment]:
    """Resolve every fragment digest stored on `agent_run` back to source
    content, verifying the digest still matches where content is versioned.
    """
    resolved: list[ResolvedGroundingFragment] = []
    for entry in agent_run.grounding_fragment_digests:
        object_type = str(entry.get("object_type", ""))
        object_id = str(entry.get("object_id", ""))
        fragment_digest = str(entry.get("fragment_digest", ""))
        raw_version_id = entry.get("annotation_version_id")
        version: MetadataBusinessAnnotationVersion | None = None
        digest_verified = False
        if object_type == "BUSINESS_ANNOTATION" and raw_version_id:
            version = await resolve_annotation_version(session, UUID(str(raw_version_id)))
            if version is not None:
                digest_verified = _verify_business_annotation_digest(version, fragment_digest)
        resolved.append(
            ResolvedGroundingFragment(
                object_type=object_type,
                object_id=object_id,
                fragment_digest=fragment_digest,
                annotation_version_id=str(raw_version_id) if raw_version_id else None,
                resolved_annotation_version=version,
                digest_verified=digest_verified,
                current_status=version.status if version is not None else None,
            )
        )
    return resolved
