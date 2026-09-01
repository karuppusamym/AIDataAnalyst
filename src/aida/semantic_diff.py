"""Structured semantic-object version diff (SM-7).

"Reviewers see version deltas": when a reviewer opens a pending
`GovernanceReview` for a versioned semantic object (a semantic model version
and its metrics, a glossary term version, ...) they should see a structured,
field-level delta between the proposed (draft) content and the currently
published content -- not just the raw proposed row, and not a wall of JSON
they have to diff by eye.

This module is the pure, DB-free half of that feature, mirroring the
`aida.connector_health` / `aida.tool_first_rate` / `aida.knowledge_graph`
idiom already established in this codebase: it takes plain `dict[str, Any]`
snapshots -- already-fetched field values, the shape a caller's own read
already takes -- and returns a structured diff with no session, no model
import, and no I/O of any kind, so it is fully unit-testable
(`tests/test_semantic_diff.py`) without a database. The DB-facing half that
turns ORM rows (`SemanticModelVersion` + its `SemanticMetricVersion`s,
`GlossaryTermVersion`, ...) into these snapshot dicts, and finds the
currently-published sibling version to diff against, lives in
`aida.semantic_api` (`get_governance_review_diff`), the only place that
touches a session for this feature.

Each snapshot is a flat or nested mapping of field name -> value (nesting is
how a bundled sub-object -- e.g. a semantic model version's metrics, keyed by
metric slug -- gets its own per-field delta rather than being reported as one
opaque "changed" blob). A field is:

* ``added`` -- present in `after`, absent in `before`.
* ``removed`` -- present in `before`, absent in `after`.
* ``changed`` -- present in both, with a different value.

Fields with an unchanged value are omitted entirely: a reviewer looking at a
delta wants to see what changed, not confirmation of everything that didn't.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

ChangeKind = Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class FieldDelta:
    """One field-level difference between two semantic-object snapshots.

    `field` is a dotted path (e.g. ``"metrics.revenue.aggregation"``) when the
    difference is inside a nested mapping, and the bare field name otherwise.
    """

    field: str
    change: ChangeKind
    before: Any = None
    after: Any = None


@dataclass(frozen=True, slots=True)
class SemanticDiff:
    """The complete structured delta between two semantic-object snapshots."""

    entries: list[FieldDelta] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.entries)

    @property
    def changed_fields(self) -> list[str]:
        return [entry.field for entry in self.entries]


def diff_semantic_object(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    ignore_fields: frozenset[str] = frozenset(),
) -> SemanticDiff:
    """Compute a structured diff between two semantic-object snapshots.

    `before` is the currently published version's content, `after` is the
    proposed (draft) version's content -- either may be `None` (or `{}`), in
    which case every field on the other side is reported ``added`` or
    ``removed`` respectively; that covers a semantic object's very first
    submission, which has no published predecessor to diff against.

    A field present in both snapshots as a nested mapping (e.g. a semantic
    model version's ``metrics``, keyed by metric slug so an added or removed
    metric is itself reported as one ``added``/``removed`` entry rather than
    a full-object replacement) is recursed into, with `field` on each nested
    `FieldDelta` carrying a dotted path back to the top-level field it lives
    under. Any other differing value -- including two unequal lists, so a
    changed `synonyms` or `allowed_dimension_column_ids` is reported as one
    ``changed`` entry carrying both full lists -- is reported as a single
    ``changed`` entry at that field's own path.
    """
    return SemanticDiff(entries=_diff_mapping(before or {}, after or {}, ignore_fields, prefix=""))


def _diff_mapping(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    ignore_fields: frozenset[str],
    *,
    prefix: str,
) -> list[FieldDelta]:
    entries: list[FieldDelta] = []
    for key in sorted(set(before.keys()) | set(after.keys())):
        if key in ignore_fields:
            continue
        path = f"{prefix}.{key}" if prefix else key
        has_before = key in before
        has_after = key in after

        if has_before and not has_after:
            entries.append(FieldDelta(field=path, change="removed", before=before[key], after=None))
            continue
        if has_after and not has_before:
            entries.append(FieldDelta(field=path, change="added", before=None, after=after[key]))
            continue

        before_val = before[key]
        after_val = after[key]
        if before_val == after_val:
            continue
        if isinstance(before_val, Mapping) and isinstance(after_val, Mapping):
            entries.extend(_diff_mapping(before_val, after_val, ignore_fields, prefix=path))
            continue
        entries.append(FieldDelta(field=path, change="changed", before=before_val, after=after_val))
    return entries
