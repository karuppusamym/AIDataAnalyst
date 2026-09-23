"""A context product's boundary names every hit type retrieval can emit, and refuses the rest.

`ContextProductScope.admits()` decides whether a retrieval hit is evidence an answer asked through
a context product may stand on. It used to decide the kinds it listed and then end in a
fallthrough that admitted any hit carrying no `table_id`, so the boundary was safe only for the
kinds someone had remembered to list. Review 2026-09-16 F02 was that default admitting ROUTINE
hits from outside the product; the fix added a ROUTINE branch and left the default alone, so the
next candidate kind would have walked through the same way -- the trigger-lineage work declined
to add a TRIGGER candidate for exactly that reason. **2026-09-22:** TRIGGER is now one of the real
emitted kinds (a SQL Server or Oracle trigger that carries its own body -- R11-FP01), decided on
its one firing table (`_owning_table`), added in the same change as its rule; the tests below that
used to demonstrate "an unrecognised kind" with `TRIGGER` now use `SEQUENCE`, a kind retrieval
still does not emit, for the same demonstration.

Three groups here:

* **The structural gate.** Every hit type retrieval can emit is derived from the code -- a scan of
  every constructor that produces a retrieval hit or a candidate that becomes one -- and must have
  a rule of its own in `ContextProductScope.HIT_TYPE_RULES`; every rule must name a type retrieval
  emits. A new candidate kind fails here before it ships, instead of being admitted by default.
* **The rules.** Each emitted kind decided both ways, and an unrecognised kind refused.
* **The orchestrator.** Through `GovernedAgentOrchestrator.run()` on the retrieval-wiring scenario:
  a product refuses an unrecognised kind and an unmatched dbt resource (the one real kind that
  had relied on the fallthrough), while a product-free run never consults `admits()` at all.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_intelligence import GovernedRetriever, RetrievalHit
from aida.agent_orchestrator import (
    AgentClarificationRequired,
    ContextProductScope,
    GovernedAgentOrchestrator,
)
from aida.config import Settings
from aida.models import (
    AgentRun,
    ContextProduct,
    ContextProductVersion,
    DbtArtifactImport,
    DbtProject,
    DbtResource,
)
from aida.vector_index_service import INDEXED_OWNER_TYPES
from tests.support.app_surface import SRC_DIR, SRC_ROOTS
from tests.test_agent_orchestrator_retrieval_wiring import _Scenario, db  # noqa: F401

# ---------------------------------------------------------------------------
# The structural gate
# ---------------------------------------------------------------------------

#: Every constructor whose argument is the type of a retrieval hit, or of a candidate that becomes
#: one, with that argument's keyword and positional index. `HybridRetrievalHit` is what every
#: retrieval stage returns and `RetrievalHit` what `GovernedRetriever` hands the orchestrator;
#: `RankedCandidate` and `SignalContribution` are the pool entries fusion ranks and evidence
#: assembly turns back into hits; `GraphNode` and `GraphHit` are how graph expansion introduces a
#: candidate no earlier channel found.
_TYPE_ARGUMENT: dict[str, tuple[str, int]] = {
    "HybridRetrievalHit": ("object_type", 0),
    "RetrievalHit": ("object_type", 0),
    "RankedCandidate": ("object_type", 0),
    "SignalContribution": ("object_type", 0),
    "GraphHit": ("object_type", 0),
    "GraphNode": ("node_type", 1),
}

#: A type argument read off another object's type is a relay: it passes on a type some other
#: scanned constructor introduced (or, for `owner_type`, a persisted vector-index entry, whose
#: types are `INDEXED_OWNER_TYPES`).
_RELAY_ATTRIBUTES = frozenset({"object_type", "node_type", "owner_type"})


@dataclass(frozen=True, slots=True)
class _Site:
    path: str
    line: int
    constructor: str
    expression: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.constructor}({self.expression})"


@dataclass(slots=True)
class _Scan:
    #: Literal hit type -> every site that constructs it.
    emitted: dict[str, list[_Site]] = field(default_factory=dict)
    #: Sites that pass on a type another site introduced.
    relays: list[_Site] = field(default_factory=list)
    #: Sites whose type the scan cannot determine. The gate refuses these rather than guessing.
    unclassified: list[_Site] = field(default_factory=list)

    def sites(self) -> Iterator[_Site]:
        for sites in self.emitted.values():
            yield from sites
        yield from self.relays


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _type_argument(call: ast.Call, keyword: str, position: int) -> ast.expr | None:
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if len(call.args) > position and not any(
        isinstance(arg, ast.Starred) for arg in call.args[: position + 1]
    ):
        return call.args[position]
    return None


def _names(target: ast.expr) -> set[str]:
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _string_constants(node: ast.expr) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _classify_name(name: str, scope: ast.AST) -> tuple[str, set[str]]:
    """How a bare name used as a hit type was bound in its enclosing scope.

    `("relay", ...)` when it is bound only as a loop or comprehension target over something that
    is not a literal collection -- the vector channel's `for object_type, object_id, similarity in
    scored`. `("literal", types)` when the loop runs over a literal collection, whose strings are
    then the types it emits. `("unclassified", ...)` for anything else: an assignment, a parameter,
    a computed value -- a type the scan cannot see is a type the gate cannot vouch for.
    """
    loop_iterables: list[ast.expr] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            if name in _names(node.target):
                loop_iterables.append(node.iter)
        elif isinstance(node, ast.Assign):
            if any(name in _names(target) for target in node.targets):
                return "unclassified", set()
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            if name in _names(node.target):
                return "unclassified", set()
        elif isinstance(node, ast.arg) and node.arg == name:
            return "unclassified", set()
    if not loop_iterables:
        return "unclassified", set()
    literal: set[str] = set()
    for iterable in loop_iterables:
        if isinstance(iterable, ast.Tuple | ast.List | ast.Set):
            literal |= _string_constants(iterable)
        else:
            return "relay", set()
    return "literal", literal


def _scan_sources(sources: Iterable[tuple[str, str]]) -> _Scan:
    scan = _Scan()
    for label, source in sources:
        if not any(constructor in source for constructor in _TYPE_ARGUMENT):
            continue
        tree = ast.parse(source, filename=label)
        enclosing: dict[ast.AST, ast.AST] = {}
        for scope in ast.walk(tree):
            if isinstance(scope, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                for child in ast.walk(scope):
                    # `ast.walk` visits outer scopes first, so the innermost one wins.
                    if child is not scope:
                        enclosing[child] = scope
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            constructor = _callee(call)
            if constructor not in _TYPE_ARGUMENT:
                continue
            keyword, position = _TYPE_ARGUMENT[constructor]
            argument = _type_argument(call, keyword, position)
            if argument is None:
                continue
            site = _Site(label, call.lineno, constructor, ast.unparse(argument))
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                scan.emitted.setdefault(argument.value, []).append(site)
            elif isinstance(argument, ast.Attribute) and argument.attr in _RELAY_ATTRIBUTES:
                scan.relays.append(site)
            elif isinstance(argument, ast.Name):
                kind, literal = _classify_name(argument.id, enclosing.get(call, tree))
                if kind == "relay":
                    scan.relays.append(site)
                elif kind == "literal":
                    for hit_type in literal:
                        scan.emitted.setdefault(hit_type, []).append(site)
                else:
                    scan.unclassified.append(site)
            else:
                scan.unclassified.append(site)
    return scan


@lru_cache(maxsize=1)
def _code_scan() -> _Scan:
    """Every shipped package, not a list of retrieval modules: a producer added anywhere counts."""
    sources = [
        (str(path.relative_to(SRC_DIR)).replace("\\", "/"), path.read_text(encoding="utf-8"))
        for _package, root in SRC_ROOTS
        for path in sorted(root.rglob("*.py"))
    ]
    return _scan_sources(sources)


def _emitted_types() -> frozenset[str]:
    """What retrieval can emit: every literal a producer constructs, plus every type the persisted
    vector index holds (the one relay whose source is a stored row rather than another site)."""
    return frozenset(_code_scan().emitted) | frozenset(INDEXED_OWNER_TYPES)


def test_the_scan_finds_the_producers_it_is_supposed_to() -> None:
    """Anchors, so a broken scan cannot pass the gate by finding nothing.

    The two kinds the boundary's history is about -- TABLE and ROUTINE, both constructed by the
    lexical stage -- the graph channel's own TABLE node, and the relays that carry every
    candidate through the orchestrator's retriever and evidence assembly.
    """
    scan = _code_scan()

    def files(sites: Iterable[_Site]) -> set[str]:
        return {Path(site.path).name for site in sites}

    assert "retrieval.py" in files(scan.emitted.get("ROUTINE", []))
    assert {"retrieval.py", "retrieval_stages.py"} <= files(scan.emitted.get("TABLE", []))
    assert any(site.constructor == "GraphNode" for site in scan.emitted["TABLE"])
    assert {"agent_intelligence.py", "retrieval_stages.py", "graph_retrieval.py"} <= files(
        scan.relays
    )


def test_every_hit_producer_is_one_the_gate_can_read() -> None:
    """A producer whose type the scan cannot determine is refused, not assumed to be a relay."""
    unclassified = [str(site) for site in _code_scan().unclassified]

    assert not unclassified, (
        "these construct a retrieval hit or candidate with a type the structural gate cannot "
        "determine -- pass a string literal, or relay another hit's `.object_type`:\n  "
        + "\n  ".join(unclassified)
    )


def test_every_hit_type_retrieval_can_emit_has_an_explicit_rule() -> None:
    """The gate that stops F02 happening a third time.

    `admits()` refuses a hit type it has no rule for, so a kind missing here is never evidence an
    answer asked through a product may stand on. That is safe, but silent -- this makes it loud.
    """
    missing = sorted(_emitted_types() - set(ContextProductScope.HIT_TYPE_RULES))
    scan = _code_scan()
    where = {
        hit_type: [str(site) for site in scan.emitted.get(hit_type, [])] or ["INDEXED_OWNER_TYPES"]
        for hit_type in missing
    }

    assert not missing, (
        f"retrieval can emit {missing} but `ContextProductScope.HIT_TYPE_RULES` has no rule for "
        "it, so a context product refuses it outright. Decide it explicitly: a kind that names a "
        "table decides on that table; a kind that carries meaning follows the pinned-meaning "
        "rule; a kind that governs nothing a product scopes is admitted by name, with the reason. "
        f"Constructed at: {where}"
    )


def test_every_rule_names_a_type_retrieval_emits() -> None:
    """A rule for a type nobody emits is an admission granted in advance to whatever later takes
    that name -- the default this gate replaces, spelled as a dead branch. `METRIC` was one."""
    dead = sorted(set(ContextProductScope.HIT_TYPE_RULES) - _emitted_types())

    assert not dead, f"`ContextProductScope.HIT_TYPE_RULES` decides {dead}, which nothing emits"


def test_a_new_candidate_type_is_caught_by_the_scan() -> None:
    """The case the trigger-lineage work avoided until R11-FP01 added a real TRIGGER candidate:
    a new emitted kind with no rule yet, caught here with a still-hypothetical `SEQUENCE` one."""
    source = textwrap.dedent(
        """
        async def sequence_hits(rows):
            return [
                HybridRetrievalHit(
                    object_type="SEQUENCE",
                    object_id=str(row.id),
                    display_name=row.name,
                    score=1.0,
                    reason_codes=[],
                    metadata={"sequence_id": str(row.id)},
                )
                for row in rows
            ]
        """
    )

    scan = _scan_sources([("synthetic/retrieval.py", source)])

    assert set(scan.emitted) == {"SEQUENCE"}
    assert "SEQUENCE" not in ContextProductScope.HIT_TYPE_RULES


def test_the_scan_reads_loop_literals_and_refuses_computed_types() -> None:
    source = textwrap.dedent(
        """
        def literal_loop():
            return [HybridRetrievalHit(kind, "id", "n", 1.0, [], {}) for kind in ("A", "B")]

        def relay(hits):
            return [RetrievalHit(object_type=h.object_type) for h in hits]

        def computed(row):
            kind = row.kind.upper()
            return HybridRetrievalHit(object_type=kind)

        def formatted(row):
            return HybridRetrievalHit(object_type=f"{row.kind}")

        def graph():
            return GraphNode("TABLE:1", "VIEW", "v", None)
        """
    )

    scan = _scan_sources([("synthetic.py", source)])

    assert set(scan.emitted) == {"A", "B", "VIEW"}
    assert [site.line for site in scan.relays] == [6]
    assert sorted(site.expression for site in scan.unclassified) == ["f'{row.kind}'", "kind"]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _scope(
    *,
    table_ids: Iterable[str] = (),
    tool_version_ids: Iterable[str] = (),
    routine_ids: Iterable[str] = (),
    ontology_version_ids: Iterable[str] = (),
    glossary_term_version_ids: Iterable[str] = (),
    semantic_model_version_ids: Iterable[str] = (),
) -> ContextProductScope:
    return ContextProductScope(
        version_id=uuid4(),
        version=1,
        table_ids=frozenset(table_ids),
        tool_version_ids=frozenset(tool_version_ids),
        routine_ids=frozenset(routine_ids),
        ontology_version_ids=frozenset(ontology_version_ids),
        glossary_term_version_ids=frozenset(glossary_term_version_ids),
        semantic_model_version_ids=frozenset(semantic_model_version_ids),
    )


def _hit(object_type: str, object_id: str, metadata: dict[str, Any]) -> RetrievalHit:
    return RetrievalHit(
        object_type=object_type,
        object_id=object_id,
        display_name="candidate",
        score=1.0,
        reason_codes=[],
        metadata=metadata,
    )


def test_an_unrecognised_hit_type_is_refused() -> None:
    """The fallthrough, closed. It used to admit this because it carries no `table_id`.

    `SEQUENCE` stands in for "a kind nobody decided" -- `TRIGGER` filled that role until
    R11-FP01 gave it a real rule (`test_a_hit_that_belongs_to_a_table_is_decided_on_that_table`).
    """
    scope = _scope(table_ids=[str(uuid4())])

    assert not scope.admits(_hit("SEQUENCE", str(uuid4()), {"sequence_id": str(uuid4())}))


def test_an_unrecognised_hit_type_is_refused_even_naming_a_product_table() -> None:
    """A kind nobody decided is not evidence because it happens to carry an in-scope table id: a
    kind's rule is chosen when the kind is added, not inferred from the shape of its metadata."""
    table_id = str(uuid4())
    scope = _scope(table_ids=[table_id])

    assert not scope.admits(_hit("SEQUENCE", str(uuid4()), {"table_id": table_id}))


def test_the_retired_metric_alias_is_no_longer_a_rule() -> None:
    """`METRIC` was decided beside `SEMANTIC_METRIC`, but no producer emits it, so it only ever
    admitted in advance whatever might later take that name."""
    scope = _scope(table_ids=[str(uuid4())])

    assert not scope.admits(_hit("METRIC", str(uuid4()), {}))
    assert scope.admits(_hit("SEMANTIC_METRIC", str(uuid4()), {}))


@pytest.mark.parametrize("hit_type", ["COLUMN", "BUSINESS_ANNOTATION", "DBT_RESOURCE", "TRIGGER"])
def test_a_hit_that_belongs_to_a_table_is_decided_on_that_table(hit_type: str) -> None:
    inside, outside = str(uuid4()), str(uuid4())
    scope = _scope(table_ids=[inside])

    assert scope.admits(_hit(hit_type, str(uuid4()), {"table_id": inside}))
    assert not scope.admits(_hit(hit_type, str(uuid4()), {"table_id": outside}))


@pytest.mark.parametrize("hit_type", ["COLUMN", "BUSINESS_ANNOTATION"])
def test_a_table_bound_hit_that_names_no_table_is_refused(hit_type: str) -> None:
    """Retrieval always stamps these with their table; one that arrives without it cannot be placed
    inside the product, and the fallthrough used to admit it for exactly that reason."""
    scope = _scope(table_ids=[str(uuid4())])

    assert not scope.admits(_hit(hit_type, str(uuid4()), {"table_id": None}))


@pytest.mark.parametrize("resource_type", ["MODEL", "SOURCE", "EXPOSURE", "METRIC", "TEST"])
def test_an_unmatched_dbt_resource_is_refused(resource_type: str) -> None:
    """The one emitted kind that relied on the fallthrough.

    A dbt resource whose relation Atlas could not match to a catalog table carries `table_id:
    None`, so the fallthrough admitted it. It names nothing the product governs -- products have
    no dbt reference group, and the compiler publishes no dbt content -- and it puts no table into
    the model's context. A relation kind that failed to match is an unresolved reference, which is
    the one thing F01's boundary refuses rather than admits.
    """
    scope = _scope(table_ids=[str(uuid4())])
    resource_id = str(uuid4())

    assert not scope.admits(
        _hit(
            "DBT_RESOURCE",
            resource_id,
            {"dbt_resource_id": resource_id, "resource_type": resource_type, "table_id": None},
        )
    )


def test_a_trigger_whose_firing_table_did_not_resolve_is_refused() -> None:
    """R11-FP01: retrieval stamps `table_id: None` when a trigger's firing table is not in this
    datasource's catalog (out of the discovery selection, or not yet scanned) -- an unresolved
    reference, the same shape an unmatched dbt resource is, not a guess and not the fallthrough."""
    scope = _scope(table_ids=[str(uuid4())])
    trigger_id = str(uuid4())

    assert not scope.admits(
        _hit("TRIGGER", trigger_id, {"trigger_id": trigger_id, "table_id": None})
    )


def test_a_table_reached_by_graph_expansion_is_decided_on_its_own_id() -> None:
    """A graph-introduced TABLE carries no `table_id` at all -- only the graph path -- so it is its
    own id that decides, never the absence of a table id."""
    inside, outside = str(uuid4()), str(uuid4())
    scope = _scope(table_ids=[inside])

    assert scope.admits(_hit("TABLE", inside, {"graph_expansion_path": ["TABLE:x", inside]}))
    assert not scope.admits(_hit("TABLE", outside, {"graph_expansion_path": ["TABLE:x", outside]}))


def test_every_rule_refuses_something_or_is_pinned_meaning() -> None:
    """No rule is an unconditional admit in disguise.

    A data-access kind must refuse a hit from outside a product that names something else; a
    meaning kind must refuse a hit of a version the product did not pin. Driven from the rule
    table itself, so a rule added later is held to the same bar.
    """
    other = str(uuid4())
    narrowing = _scope(
        table_ids=[str(uuid4())],
        tool_version_ids=[str(uuid4())],
        routine_ids=[str(uuid4())],
        ontology_version_ids=[str(uuid4())],
        glossary_term_version_ids=[str(uuid4())],
        semantic_model_version_ids=[str(uuid4())],
    )
    outsider_metadata = {
        "table_id": other,
        "routine_id": other,
        "ontology_version_id": other,
        "term_version_id": other,
        "semantic_model_version_id": other,
    }

    admitted = [
        hit_type
        for hit_type in ContextProductScope.HIT_TYPE_RULES
        if narrowing.admits(_hit(hit_type, other, outsider_metadata))
    ]

    assert admitted == []


# ---------------------------------------------------------------------------
# Through the orchestrator
# ---------------------------------------------------------------------------

QUESTION = "orders"


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> AsyncIterator[_Scenario]:  # noqa: F811
    yield await _Scenario(db).build()


@dataclass(frozen=True, slots=True)
class _DbtResources:
    matched: DbtResource
    unmatched: DbtResource


async def _dbt_resources(scenario: _Scenario) -> _DbtResources:
    """Two dbt models whose names match the question: one matched to `fact_orders`, one Atlas could
    not place in the catalog -- the real shape of the hit that relied on the fallthrough."""
    session = scenario.db
    project = DbtProject(
        organization_id=scenario.organization.id,
        project_id=scenario.project.id,
        datasource_id=scenario.datasource.id,
        project_key="commerce",
        display_name="Commerce dbt",
        target_name="prod",
        created_by="steward-1",
    )
    session.add(project)
    await session.flush()
    artifact = DbtArtifactImport(
        organization_id=scenario.organization.id,
        dbt_project_id=project.id,
        manifest_fingerprint="f" * 64,
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json",
        resource_count=2,
        model_count=2,
        source_count=0,
        test_count=0,
        lineage_edge_count=0,
        matched_resource_count=1,
        unmatched_resource_count=1,
        imported_by="steward-1",
    )
    session.add(artifact)
    await session.flush()

    def resource(name: str, matched_table_id: UUID | None) -> DbtResource:
        return DbtResource(
            organization_id=scenario.organization.id,
            artifact_import_id=artifact.id,
            unique_id=f"model.commerce.{name}",
            resource_type="MODEL",
            package_name="commerce",
            name=name,
            schema_name="public",
            sql_parse_status="PARSED",
            matched_table_id=matched_table_id,
        )

    resources = _DbtResources(
        matched=resource("orders_model", scenario.fact_orders.id),
        unmatched=resource("orders_rollup", None),
    )
    session.add_all([resources.matched, resources.unmatched])
    await session.flush()
    return resources


async def _publish(scenario: _Scenario, key: str) -> None:
    """A product naming `fact_orders` and the scenario's tool, so the run reaches CLARIFICATION."""
    session = scenario.db
    product = ContextProduct(
        organization_id=scenario.organization.id,
        project_id=scenario.project.id,
        product_key=key,
        created_by="steward-1",
    )
    session.add(product)
    await session.flush()
    session.add(
        ContextProductVersion(
            organization_id=scenario.organization.id,
            product_id=product.id,
            version=1,
            status="PUBLISHED",
            name="Order context",
            description="What an agent needs to answer questions about orders.",
            purpose="Answer order questions.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            table_ids=[str(scenario.fact_orders.id)],
            eligible_tool_version_ids=[str(scenario.tool_version.id)],
            allowed_consumer_roles=["Analyst"],
            fingerprint=f"fp-{uuid4().hex[:8]}",
            created_by="steward-1",
        )
    )
    await session.flush()


def _with_unrecognised_candidate(monkeypatch: pytest.MonkeyPatch) -> str:
    """Retrieval as it is, plus one candidate of a kind `admits()` has never heard of.

    `SEQUENCE` (R11-FP01 modelled the object, discovery reads it, but nothing yet retrieves it)
    stands in here; `TRIGGER` filled this role until R11-FP01 gave it a real rule, and using it
    for "unrecognised" now would be testing the wrong thing.
    """
    sequence_id = str(uuid4())
    original = GovernedRetriever.score_candidates

    async def score_candidates(
        self: GovernedRetriever, session: AsyncSession, **kwargs: Any
    ) -> list[RetrievalHit]:
        hits = await original(self, session, **kwargs)
        return [
            *hits,
            RetrievalHit(
                object_type="SEQUENCE",
                object_id=sequence_id,
                display_name="orders_seq",
                score=0.5,
                reason_codes=["BM25_SEQUENCE_NAME"],
                metadata={"sequence_id": sequence_id},
            ),
        ]

    monkeypatch.setattr(GovernedRetriever, "score_candidates", score_candidates)
    return sequence_id


async def _ask(scenario: _Scenario, *, product_key: str | None) -> AgentRun:
    with pytest.raises(AgentClarificationRequired):
        await GovernedAgentOrchestrator(Settings(_env_file=None)).run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id=f"corr-{uuid4().hex[:8]}",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=scenario.tool_version.id,
            tool_parameters={},
            requested_limit=None,
            context_product_key=product_key,
        )
    run = await scenario.db.scalar(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(1))
    assert run is not None
    return run


def _evidence_ids(run: AgentRun) -> set[tuple[str, str]]:
    return {(hit["object_type"], hit["object_id"]) for hit in run.retrieval_evidence}


async def test_a_product_refuses_what_it_has_no_rule_for_and_keeps_what_it_names(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = await _dbt_resources(scenario)
    sequence_id = _with_unrecognised_candidate(monkeypatch)
    await _publish(scenario, "orders-context")

    run = await _ask(scenario, product_key="orders-context")

    evidence = _evidence_ids(run)
    # What the product names is still evidence: its table, its tool, and the dbt model matched
    # to its table.
    assert ("TABLE", str(scenario.fact_orders.id)) in evidence
    assert ("GOVERNED_TOOL", str(scenario.tool_version.id)) in evidence
    assert ("DBT_RESOURCE", str(resources.matched.id)) in evidence
    # What it does not is not: the kind nobody decided, and the dbt model Atlas could not place.
    assert ("SEQUENCE", sequence_id) not in evidence
    assert ("DBT_RESOURCE", str(resources.unmatched.id)) not in evidence


async def test_a_product_free_run_never_consults_the_product_boundary(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`admits()` is only asked when a request carries a product; without one, nothing narrows."""
    resources = await _dbt_resources(scenario)
    sequence_id = _with_unrecognised_candidate(monkeypatch)
    await _publish(scenario, "orders-context")

    def consulted(self: ContextProductScope, hit: RetrievalHit) -> bool:
        raise AssertionError(f"a product-free run consulted admits() for {hit.object_type}")

    monkeypatch.setattr(ContextProductScope, "admits", consulted)

    run = await _ask(scenario, product_key=None)

    evidence = _evidence_ids(run)
    assert ("SEQUENCE", sequence_id) in evidence
    assert ("DBT_RESOURCE", str(resources.unmatched.id)) in evidence
    assert ("DBT_RESOURCE", str(resources.matched.id)) in evidence
    assert ("TABLE", str(scenario.dim_customer.id)) in evidence
    resolved = [step for step in run.step_trace if step.get("stage") == "RESOLVED"]
    assert resolved and "context_product_version" not in resolved[-1]["details"]


async def test_every_type_real_retrieval_returned_has_a_rule(
    scenario: _Scenario,
) -> None:
    """The runtime half of the structural gate, over what this scenario's real retrieval returns:
    tables (lexical and graph-expanded), the governed tool, and both dbt resources."""
    await _dbt_resources(scenario)

    run = await _ask(scenario, product_key=None)

    returned = {hit["object_type"] for hit in run.retrieval_evidence}
    assert {"TABLE", "GOVERNED_TOOL", "DBT_RESOURCE"} <= returned
    assert returned <= set(ContextProductScope.HIT_TYPE_RULES)
