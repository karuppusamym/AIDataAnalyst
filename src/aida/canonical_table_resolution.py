"""RL-2: canonical table resolution -- pure detection logic.

Across a real enterprise catalog the "same" logical table often exists more
than once: a production table and its read-replica/reporting mirror, the
same table cloned into a dev/staging environment, or several near-duplicate
ingestions of the same source from different pipelines. When several
``MetadataTable`` rows plausibly represent one logical entity, retrieval and
lineage should be able to point at a single canonical representative rather
than surfacing N near-identical hits.

This module implements the *detection* half as small, pure functions over
already-fetched, plain data -- no session, no I/O -- following the exact
split established by ``aida.table_family_intelligence`` (RL-1) and
``aida.composite_key_inference`` (PR-1): the API layer
(``aida.canonical_table_api``) does the bounded database fetch and persists
whatever this module proposes as ``PENDING`` ``CanonicalTableGroup`` rows,
de-duplicating against what is already persisted. That keeps the heuristic
directly unit-testable without a database and makes it idempotent -- running
it twice on the same input always returns the same groups.

Unlike RL-1 (bounded to one schema) or PR-1 (bounded to one table), a
canonical-duplicate group can legitimately span schemas, catalogs, and even
datasources (e.g. an Oracle production table replicated into a Snowflake
reporting warehouse) -- so the caller fetches one *organization's* tables at
a time, and every bound below exists to keep that organization-wide scan
from becoming an unbounded O(n^2) comparison (see "Bounds" below).

Two independent signals must both fire before two tables are ever grouped:

1. **Name signal** -- the bare table names, once case- and
   separator-normalized, are identical. This is deliberately an exact match,
   not fuzzy string similarity: the same discipline PR-1 documents (a low
   signal must never be sufficient alone) applies just as much to name
   matching as to statistical matching -- a fuzzy name match (edit distance,
   token overlap) invites exactly the false positives the exit condition
   warns about ("do NOT group two tables that merely have the same name but
   structurally different columns" implies the converse care is also
   needed: don't group differently-named tables just because they merely
   *look* similar).
2. **Shape signal** -- the tables' column signatures (name + a
   width/precision-stripped base type, so ``VARCHAR(50)`` and
   ``VARCHAR(100)`` count as the same base type) are near-identical, scored
   by Jaccard similarity over the two signature sets and required to clear
   ``MIN_COLUMN_SIGNATURE_SIMILARITY``. If the two tables' ``fingerprint``
   values (see ``aida.workflows.activities.fingerprint`` -- a SHA-256 of the
   entire discovered table shape, columns included) are byte-identical, that
   is treated as a maximal shape match on its own: a fingerprint collision
   means the two tables were independently discovered with an
   indistinguishable name/type/column/constraint shape, which is strictly
   stronger evidence than any similarity score derived from columns alone.

Confidence is scaled by the *weakest* pairwise shape-similarity found
anywhere in a group, not just the edge that first linked two members
together -- see ``detect_canonical_table_groups`` for why.

Bare-name equality alone should never itself be trusted at any confidence
(``rename_detection``/CT-4-style structural gating is why): a same-named,
differently-shaped pair never groups here, matching the exit condition's
explicit negative example.

Picking a *default* canonical member is a documented, deterministic,
override-able suggestion, never a decision -- see ``pick_default_canonical``.
A steward can always override it at approval time; the detector's guess is
recorded in ``evidence`` precisely so a reviewer can see it was only a
default.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# ---------------------------------------------------------------------------
# Bounds -- every constant here exists to keep this heuristic's search space
# bounded and explicit, in the spirit of `composite_key_inference.MAX_*` /
# `dbt_artifacts.MAX_RESOURCES`. None of this is a substitute for the API
# layer's own fetch cap (`CANONICAL_TABLE_SCAN_MAX_TABLES` in
# `canonical_table_api`) -- these bounds protect the pure function itself
# against any caller, including a future one that is less careful about its
# own fetch size.
# ---------------------------------------------------------------------------

# Hard ceiling on how many tables one call to `detect_canonical_table_groups`
# will consider at all. The API layer's own fetch limit is already well
# below this; this is defense-in-depth so the pure function can never be
# handed an unbounded organization-wide table list and start an O(n^2) scan.
MAX_TABLES_EVALUATED = 20_000

# A bucket of tables sharing one normalized bare name larger than this is
# skipped entirely (no groups are emitted from it at all) rather than
# partially evaluated. A very common generic table name (`orders`, `events`)
# appearing dozens of times across an organization is far more likely to be
# coincidental repetition across unrelated systems than a small number of
# intentional duplicates, and exhaustively comparing every pair in a huge
# bucket would dominate the pair budget below for a low-value result.
MAX_NAME_BUCKET_SIZE = 25

# Hard ceiling on how many pairwise column-signature comparisons are
# actually scored across the whole call, enforced independently of
# MAX_NAME_BUCKET_SIZE so a pathological input (many mid-sized buckets) can
# never turn this into an unbounded blow-up either.
MAX_CANDIDATE_PAIRS_EVALUATED = 5_000

# A connected component (after transitively merging every qualifying pair)
# larger than this is dropped rather than emitted as one giant group. A
# genuine "same logical entity, multiple copies" situation rarely spans more
# than a handful of environments/pipelines; a component this large more
# likely means an overly generic bare name slipped past MAX_NAME_BUCKET_SIZE
# with a permissive shape (e.g. a common 2-3 column junction/lookup table
# shape shared by many unrelated tables) than a real duplicate family.
MAX_GROUP_SIZE = 8

# A group must have at least this many members to be worth reporting --
# structurally identical to requiring 2, but named for clarity at call
# sites.
MIN_GROUP_SIZE = 2

# At most this many groups are returned per call (highest confidence
# first) -- a reviewer should see a short, ranked shortlist, not every
# component that happened to clear the bar.
MAX_GROUPS_RETURNED = 50

# Both tables in a candidate pair must have at least this many columns
# before their column signatures are compared at all. Below this, a
# signature match is too easily coincidental (e.g. two unrelated 1-column
# lookup tables both named `id`) to trust -- the same "require enough
# structure to be meaningful" posture PR-1 takes with
# MIN_MEMBER_DISTINCT_RATIO.
MIN_COLUMNS_FOR_SIGNATURE_MATCH = 3

# Minimum Jaccard similarity between two tables' column signature sets
# (name + base type pairs) to treat them as "near-identical, allowing minor
# drift" -- e.g. one column added/renamed/dropped out of several, or a type
# width/precision difference (already normalized away, see
# `_normalize_physical_type`). Below this, the two tables are treated as
# structurally different and never grouped, regardless of name match --
# this is the check that keeps a same-named-but-differently-shaped pair from
# ever grouping, per the exit condition's explicit negative example.
MIN_COLUMN_SIGNATURE_SIMILARITY = 0.8

# This heuristic combines two independent signals (exact bare-name match +
# near-identical column shape), which is stronger corroboration than either
# alone -- e.g. PR-1's single-signal statistical guess, capped at 0.6 -- but
# it is still never a data-level proof of identity (no connector access, no
# row-level comparison; two independently-designed systems can coincidentally
# converge on both a common name and a common shape for an unrelated table,
# e.g. a vendor-standard `audit_log` table). The ceiling reflects that: high
# enough to rank above a bare statistical guess, capped well short of
# certainty.
MAX_CONFIDENCE = 0.75

DETECTION_RULE = "CANONICAL_NAME_MATCH_COLUMN_SIGNATURE_V1"

# Environment-tier tokens used only to steer the *default* canonical pick
# (never to exclude a member from the group). Derived from the vocabulary
# this codebase's own `DataSource.environment` field is populated with
# across its test fixtures and settings (`PROD` / `PRODUCTION` vs
# `DEVELOPMENT` / `TEST`, plus `mcp_budget.py`'s own
# `{"staging", "production"}` check) generalized to the handful of
# additional tokens enterprise catalogs commonly use for the same purpose.
# `schema_name`/`catalog_name` are tokenized on non-alphanumeric characters
# and matched as whole tokens (never substrings) specifically so a name like
# `latest_orders` is never mistaken for containing `test`.
NON_PROD_NAME_TOKENS = frozenset(
    {
        "dev",
        "development",
        "staging",
        "stage",
        "stg",
        "sandbox",
        "sbox",
        "tmp",
        "temp",
        "test",
        "qa",
        "uat",
        "demo",
        "poc",
    }
)

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]")
_BASE_TYPE_RE = re.compile(r"^[a-z_]+")

# A single table's column, as fetched by the caller: (column_name, physical_type).
ColumnInput = tuple[str, str]

# A single table, as fetched by the caller:
# (table_id, name, schema_name, catalog_name, datasource_id, fingerprint,
#  row_count_estimate, columns).
TableInput = tuple[UUID, str, str, str, UUID, str, int | None, Sequence[ColumnInput]]


@dataclass(frozen=True)
class CanonicalGroupDraft:
    """One detector finding, ready for the API layer to dedupe and persist."""

    member_table_ids: list[UUID]
    default_canonical_table_id: UUID
    detection_rule: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def member_key(self) -> frozenset[UUID]:
        """Identity used for de-duplication: the member set alone.

        Unlike `TableFamilyCandidate.member_key()` there is no second
        dimension (a family "type") -- a canonical-duplicate group is
        identified purely by which tables it claims are the same logical
        entity.
        """
        return frozenset(self.member_table_ids)


@dataclass(frozen=True)
class _TableRecord:
    table_id: UUID
    name: str
    schema_name: str
    catalog_name: str
    datasource_id: UUID
    fingerprint: str
    row_count_estimate: int | None
    signature: frozenset[tuple[str, str]]


def _normalize_name(name: str) -> str:
    """Case- and separator-normalized bare table name, for bucket identity."""
    return _NAME_NORMALIZE_RE.sub("", name.casefold())


def _normalize_physical_type(physical_type: str) -> str:
    """Strip width/precision/scale (`VARCHAR(255)` -> `varchar`) and casefold.

    Keeps the base type family so `NUMBER(38,0)` and `NUMBER(10,2)`, or
    `VARCHAR(50)` and `VARCHAR(100)`, count as the same type for signature
    matching -- exactly the "minor drift" the module docstring allows for.
    """
    lowered = physical_type.strip().casefold()
    match = _BASE_TYPE_RE.match(lowered)
    return match.group(0) if match else lowered


def _column_signature(columns: Sequence[ColumnInput]) -> frozenset[tuple[str, str]]:
    return frozenset(
        (name.casefold(), _normalize_physical_type(physical_type))
        for name, physical_type in columns
    )


def _jaccard(left: frozenset[Any], right: frozenset[Any]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_SPLIT_RE.split(text.casefold()) if t)


def _looks_non_prod(schema_name: str, catalog_name: str) -> bool:
    """Whole-token match only -- see NON_PROD_NAME_TOKENS's docstring note on
    why this never does substring matching."""
    tokens = _tokens(schema_name) | _tokens(catalog_name)
    return not tokens.isdisjoint(NON_PROD_NAME_TOKENS)


def pick_default_canonical(members: Sequence[_TableRecord]) -> tuple[UUID, dict[str, Any]]:
    """Deterministic *default* canonical pick -- a suggestion, never a mandate.

    Rule, applied in order (each step only breaks ties left by the previous
    one):

    1. Prefer members whose schema/catalog name carries no recognized
       non-production token (see `NON_PROD_NAME_TOKENS`) over ones that do.
       If every member is flagged, or none is, this step is a no-op (the
       full member set carries forward unchanged) -- it only ever narrows
       when it can actually discriminate.
    2. Within what is left, prefer the highest `row_count_estimate` (a
       missing estimate, `None`, is treated as lower than any real number,
       but a member with no estimate can still win if it is the only one
       left).
    3. Final, purely mechanical tie-break: lowest `table_id` (as a string),
       so the pick is always fully deterministic and reproducible.

    Returns `(table_id, reason)` where `reason` documents which steps
    actually discriminated, for the evidence payload -- a reviewer should be
    able to see *why* this table was suggested, not just that it was.
    """
    flagged = {m.table_id: _looks_non_prod(m.schema_name, m.catalog_name) for m in members}
    clean = [m for m in members if not flagged[m.table_id]]
    pool = clean if clean else list(members)
    used_environment_filter = bool(clean) and len(clean) < len(members)

    best_row_count = max((m.row_count_estimate or -1) for m in pool)
    row_count_ties = [m for m in pool if (m.row_count_estimate or -1) == best_row_count]
    used_row_count = len(pool) > 1 and len(row_count_ties) < len(pool)

    winner = min(row_count_ties, key=lambda m: str(m.table_id))
    reason = {
        "steps_applied": [
            step
            for step, used in (
                ("preferred_non_dev_staging_sandbox_schema_or_catalog", used_environment_filter),
                ("preferred_highest_row_count_estimate", used_row_count),
                ("tie_broken_by_lowest_table_id", len(row_count_ties) > 1),
            )
            if used
        ],
        "candidate_pool_size": len(pool),
        "flagged_non_prod_table_ids": sorted(
            str(tid) for tid, is_flagged in flagged.items() if is_flagged
        ),
    }
    return winner.table_id, reason


def _union_find_groups(
    indices: Sequence[int], edges: Sequence[tuple[int, int, float]]
) -> list[list[int]]:
    """Standard union-find over a bucket's (global) member indices, merged by
    `edges`. An index with no qualifying edge ends up alone in its own
    singleton component, which the caller filters out via `MIN_GROUP_SIZE`."""
    parent: dict[int, int] = {index: index for index in indices}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_i] = root_j

    for i, j, _similarity in edges:
        union(i, j)

    components: dict[int, list[int]] = {}
    for index in indices:
        components.setdefault(find(index), []).append(index)
    return list(components.values())


def detect_canonical_table_groups(tables: Sequence[TableInput]) -> list[CanonicalGroupDraft]:
    """Group `MetadataTable` rows within one organization that plausibly
    represent the same logical entity.

    `tables` should be every table the caller wants scanned together --
    normally one organization's ACTIVE tables, already capped by the caller
    (see `canonical_table_api.CANONICAL_TABLE_SCAN_MAX_TABLES`). Grouping can
    freely cross schema/catalog/datasource boundaries within that input --
    unlike RL-1's family detectors, which are deliberately bounded to one
    schema -- because the whole point of this detector is to catch
    cross-environment/cross-datasource duplication (see module docstring).

    Returns an empty list for fewer than two tables. Silently truncates to
    `MAX_TABLES_EVALUATED` tables (in input order) if handed more -- see
    that constant's docstring.
    """
    if len(tables) < MIN_GROUP_SIZE:
        return []
    tables = tables[:MAX_TABLES_EVALUATED]

    records = [
        _TableRecord(
            table_id=row[0],
            name=row[1],
            schema_name=row[2],
            catalog_name=row[3],
            datasource_id=row[4],
            fingerprint=row[5],
            row_count_estimate=row[6],
            signature=_column_signature(row[7]),
        )
        for row in tables
    ]

    buckets: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        buckets.setdefault(_normalize_name(record.name), []).append(index)

    pairs_evaluated = 0
    groups: list[CanonicalGroupDraft] = []
    for indices in buckets.values():
        if len(indices) < MIN_GROUP_SIZE or len(indices) > MAX_NAME_BUCKET_SIZE:
            continue

        edges: list[tuple[int, int, float]] = []
        for a in range(len(indices)):
            if pairs_evaluated >= MAX_CANDIDATE_PAIRS_EVALUATED:
                break
            for b in range(a + 1, len(indices)):
                if pairs_evaluated >= MAX_CANDIDATE_PAIRS_EVALUATED:
                    break
                pairs_evaluated += 1
                i, j = indices[a], indices[b]
                left, right = records[i], records[j]
                if (
                    len(left.signature) < MIN_COLUMNS_FOR_SIGNATURE_MATCH
                    or len(right.signature) < MIN_COLUMNS_FOR_SIGNATURE_MATCH
                ):
                    continue
                exact_fingerprint_match = (
                    bool(left.fingerprint)
                    and bool(right.fingerprint)
                    and left.fingerprint == right.fingerprint
                )
                if exact_fingerprint_match:
                    similarity = 1.0
                else:
                    similarity = _jaccard(left.signature, right.signature)
                if similarity < MIN_COLUMN_SIGNATURE_SIMILARITY:
                    continue
                edges.append((i, j, similarity))

        if not edges:
            continue

        for member_indices in _union_find_groups(indices, edges):
            if len(member_indices) < MIN_GROUP_SIZE or len(member_indices) > MAX_GROUP_SIZE:
                continue
            local_members = [records[i] for i in member_indices]

            # Score from every *directly evaluated and qualifying* pair inside
            # this component -- which, for a component of more than two
            # members, may be a subset of all C(n,2) pairs if some were only
            # joined transitively (A-B and B-C matched but A-C was never
            # itself re-evaluated/qualifying). Scoring from the weakest of
            # those firing edges keeps a transitively-formed group from
            # reading as more confident than the evidence that actually
            # connected it.
            component_pair_similarities = [
                similarity
                for i, j, similarity in edges
                if i in member_indices and j in member_indices
            ]
            min_similarity = min(component_pair_similarities)
            confidence = round(MAX_CONFIDENCE * min_similarity, 4)

            default_canonical_id, pick_reason = pick_default_canonical(local_members)
            datasource_ids = sorted({str(m.datasource_id) for m in local_members})
            groups.append(
                CanonicalGroupDraft(
                    member_table_ids=[m.table_id for m in local_members],
                    default_canonical_table_id=default_canonical_id,
                    detection_rule=DETECTION_RULE,
                    confidence=confidence,
                    evidence={
                        "normalized_name": _normalize_name(local_members[0].name),
                        "min_pairwise_column_signature_similarity": round(min_similarity, 4),
                        "spans_multiple_datasources": len(datasource_ids) > 1,
                        "datasource_ids": datasource_ids,
                        "default_canonical_table_id": str(default_canonical_id),
                        "default_canonical_pick_reason": pick_reason,
                        "members": [
                            {
                                "table_id": str(m.table_id),
                                "table_name": m.name,
                                "schema_name": m.schema_name,
                                "catalog_name": m.catalog_name,
                                "datasource_id": str(m.datasource_id),
                                "row_count_estimate": m.row_count_estimate,
                                "column_count": len(m.signature),
                            }
                            for m in local_members
                        ],
                    },
                )
            )

    groups.sort(key=lambda g: g.confidence, reverse=True)
    return groups[:MAX_GROUPS_RETURNED]
