"""F01 / R11-FP12: the table boundary a published context product narrows execution to.

`ContextProductScope` (`aida.agent_orchestrator`) decides what an answer may be
*grounded* in. This module decides what a statement may *read*, and it is
deliberately a separate, narrower thing:

* it is resolved once, at the boundary every execution path shares
  (`QueryExecutionGateway`), rather than at each caller. F01 found the MCP
  `tools/call` surface resolving a product and forwarding nothing, and the
  direct-SQL endpoint with no product concept at all -- so the same principal,
  product-scoped through Ask, could submit the same statement unscoped
  elsewhere. Enforcement that lives in one caller is enforcement one caller has.
* it only ever *narrows*. The datasource allowlist
  (`QueryExecutionGateway.allowed_tables`), the ABAC axes
  (`aida.policy_resource_attributes`) and the product-entitlement revocation
  check all run first and unchanged; a product scope adds refusals and removes
  none. A caller with no product passes `None` and behaves exactly as before.

**Why this module has its own resolver.**
`policy_resource_attributes.resolve_referenced_table_ids` is deliberately
permissive -- it discards everything before the last `.` and keeps every
leaf-name match -- and it says why: every axis it feeds is collapsed to its
worst case, so picking up an extra same-named table from another schema can only
make a decision more conservative, never less. **That justification does not
transfer to a set-membership test.** Used as a scope check, the same imprecision
changes sign twice over:

* a name that resolves to nothing contributes nothing to the set, so
  "every resolved id is in the product" is vacuously true and an unresolved
  reference passes the boundary (F01 G4);
* a bare or two-part name that matches several same-named tables refuses the
  statement over a table it never read (F01 G5).

So `resolve_scope_names` below is schema-aware and accounts for every name
separately, and the existing permissive resolver keeps its own semantics for the
axes it was written for. A reference is inside a product only when it resolves
to exactly one ACTIVE table in this datasource *and* the product names that
table. Anything else is refused: a reference the platform cannot resolve is a
reference it cannot prove is inside the product, and this is the last narrowing
before a connector is opened (INV-2).

Name keying matches `QueryExecutionGateway.allowed_tables` and
`quality_coupling.resolve_table_ids` -- `schema.table`,
`catalog.schema.table`, and a bare table name only when it is unambiguous --
so a name that authorises as a table resolves as a table here too. The one
place this is stricter than `allowed_tables` is a two-part `schema.table` that
several catalogs answer to: the allowlist admits it by set membership, and this
refuses it, because a scope test has to know *which* table it admitted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    ContextProduct,
    ContextProductVersion,
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
)

#: A reference that resolved to a real table the product does not name.
CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE: Final = "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE"
#: A reference that did not resolve to exactly one ACTIVE table in this
#: datasource, so it cannot be shown to be inside the product. Kept distinct
#: from the code above because the two need different operator responses: one
#: is a curation question ("should the product name this table?"), the other is
#: a statement or catalog question ("what does this name even refer to?").
CONTEXT_PRODUCT_TABLE_UNRESOLVED: Final = "CONTEXT_PRODUCT_TABLE_UNRESOLVED"
#: An eligible governed tool version declares a dependency the product does not
#: name -- see `GOVERNED_TOOL_DEPENDENCY_CONTRACT` below.
CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE: Final = (
    "CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE"
)

#: F01: the contract the review asked to be made explicit -- "define how an
#: eligible governed tool's approved dependencies fit that scope; do not
#: silently widen it".
#:
#: **A product's `eligible_tool_version_ids` says which tools may be selected.
#: It never enlarges the table scope.** Every table an eligible tool version
#: declares (`GovernedToolVersion.referenced_tables`, authorised at draft
#: creation against the *datasource-wide* allowlist) must be named by the
#: product, and the tool's rendered SQL is then held to the same scope as any
#: other statement. Before F01 the orchestrator exempted the GOVERNED_TOOL
#: strategy outright, on the stated ground that "the product declared that
#: version eligible" -- but nothing at any of the four lifecycle points
#: (product version create/update, product version approval, tool version
#: approval, tool draft creation) ever compared a tool's tables with a
#: product's, so eligibility silently widened the boundary to whatever the tool
#: happened to read.
#:
#: **Transitive reach is deliberately not followed.** A view-derived tool's
#: `referenced_tables` names the view, not the view's base tables. The object
#: the statement reads is the object governed: it is what the guard reports,
#: what `allowed_tables` authorises, what the ABAC axes are resolved for, and
#: what `QueryExecution.referenced_tables` records. So a product that names a
#: view covers a tool over that view without naming its base tables, and a tool
#: that reads those base tables directly is refused unless the product names
#: them. Resolving a view to its bases here would make the product boundary
#: disagree with every other control on the same statement, in the widening
#: direction.
#:
#: **Refused at execution, not only at authoring.** An authoring-time check
#: cannot be the boundary: a product version and a tool version are edited
#: independently and both can move after publication (a table retired, a
#: product version updated, a tool republished over a new table). This is
#: enforced where every surface passes -- the validate stage's pre-execution
#: check and the gateway's own pass -- so drift is refused rather than
#: grandfathered.
GOVERNED_TOOL_DEPENDENCY_CONTRACT: Final = (
    "an eligible tool version's declared tables must all be named by the product; "
    "eligibility selects a tool, it does not widen the table scope"
)


@dataclass(frozen=True, slots=True)
class ContextProductExecutionScope:
    """The published product version a statement is being executed through.

    Carries only what an execution-time boundary decision needs -- the version
    receipt F01 asks to be preserved through the answer, and the table
    allowlist that narrows the datasource's own. Deliberately not
    `ContextProductScope`: that type also carries the retrieval axes (tool
    versions, routines, pinned meaning) and lives in
    `aida.agent_orchestrator`, which imports `aida.query_gateway`. The gateway
    needs the boundary, not the grounding rules, and taking only the boundary
    is what keeps the dependency pointing one way.
    """

    version_id: UUID
    version: int
    table_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScopeResolution:
    """Per-name accounting for one statement's references against one product.

    Three disjoint buckets, and the two that are not `in_scope` are both
    refusals. Keeping them apart rather than returning a bare boolean is the
    whole point of this type: the refusal has to be able to name which
    reference failed and why, or a product-boundary denial is indistinguishable
    from a typo (F01 G4).
    """

    in_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    unresolved: tuple[str, ...]

    @property
    def admitted(self) -> bool:
        return not self.out_of_scope and not self.unresolved

    def refusal_names(self) -> tuple[str, ...]:
        """Every reference that could not be proven inside the product."""
        return tuple(sorted({*self.out_of_scope, *self.unresolved}))


async def load_execution_scope(
    session: AsyncSession,
    *,
    organization_id: UUID,
    product_key: str,
    roles: frozenset[str],
) -> ContextProductExecutionScope | None:
    """The published product `product_key` names, if this caller may ask through it.

    `None` covers "no such product in this organization", "it has no published
    version" and "this caller is not one of its consumer roles" together, so
    the caller answers all three the same way. Telling them apart would report
    which products exist to a principal who cannot read them, which is the same
    side channel the governed-tool surfaces close by answering "not found or
    not published" to an unauthorized caller.

    The lookup and the role rule are the ones
    `GovernedAgentOrchestrator._stage_retrieve` already applies -- published
    version, and `PlatformAdmin` or an intersection with
    `allowed_consumer_roles` -- so the direct-SQL endpoint cannot admit a
    product Ask would refuse. Deliberately *narrower* than MCP's
    `_resolve_context_product_scope`, which also serves a pinned SUPPORTED
    version and applies an agent capability envelope: this path takes a bare
    key with no version in it, so there is no pinned version to honour, and
    widening it to SUPPORTED versions would let a key select a version the
    caller never named.
    """
    row = (
        await session.execute(
            select(ContextProductVersion)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(
                ContextProduct.organization_id == organization_id,
                ContextProduct.product_key == product_key,
                ContextProductVersion.status == "PUBLISHED",
            )
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    if "PlatformAdmin" not in roles and roles.isdisjoint(set(row.allowed_consumer_roles)):
        return None
    return ContextProductExecutionScope(
        version_id=row.id,
        version=row.version,
        table_ids=frozenset(str(table_id) for table_id in row.table_ids),
    )


async def _resolve_unique_table_ids(
    session: AsyncSession,
    datasource: DataSource,
    referenced_tables: Sequence[str],
) -> dict[str, UUID]:
    """Map each name form onto the one table it unambiguously identifies.

    Same catalog binding, tenancy filter and ACTIVE-only rule as
    `QueryExecutionGateway.allowed_tables`, keyed with the same qualified and
    unqualified variants, and bounded by the statement's own leaf names rather
    than loading the datasource's whole table catalog. A key that several rows
    answer to is dropped rather than overwritten -- an ambiguous name is not a
    resolution, and silently keeping the last row read would make the boundary
    depend on row order.
    """
    leaf_names = {name.rsplit(".", 1)[-1].lower() for name in referenced_tables}
    if not leaf_names:
        return {}
    rows = (
        await session.execute(
            select(
                MetadataCatalog.name,
                MetadataSchema.name,
                MetadataTable.name,
                MetadataTable.id,
            )
            .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
            .join(MetadataTable, MetadataTable.schema_id == MetadataSchema.id)
            .where(
                MetadataCatalog.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                func.lower(MetadataTable.name).in_(leaf_names),
            )
        )
    ).all()
    candidates: dict[str, set[UUID]] = {}
    for catalog_name, schema_name, table_name, table_id in rows:
        for key in (
            table_name.lower(),
            f"{schema_name}.{table_name}".lower(),
            f"{catalog_name}.{schema_name}.{table_name}".lower(),
        ):
            candidates.setdefault(key, set()).add(table_id)
    return {key: next(iter(ids)) for key, ids in candidates.items() if len(ids) == 1}


async def resolve_scope_names(
    session: AsyncSession,
    datasource: DataSource,
    referenced_tables: Sequence[str],
    *,
    table_ids: Iterable[str],
) -> ScopeResolution:
    """Decide, name by name, whether a statement stays inside `table_ids`.

    `referenced_tables` is the guard's own list -- physical tables only, with
    CTE names already removed (`aida.sql_guard`) -- so a name here is something
    the statement really reads from the source.
    """
    scope = frozenset(str(value) for value in table_ids)
    resolved = await _resolve_unique_table_ids(session, datasource, referenced_tables)
    in_scope: list[str] = []
    out_of_scope: list[str] = []
    unresolved: list[str] = []
    for name in referenced_tables:
        table_id = resolved.get(name.lower())
        if table_id is None:
            unresolved.append(name)
        elif str(table_id) in scope:
            in_scope.append(name)
        else:
            out_of_scope.append(name)
    return ScopeResolution(
        in_scope=tuple(in_scope),
        out_of_scope=tuple(out_of_scope),
        unresolved=tuple(unresolved),
    )
