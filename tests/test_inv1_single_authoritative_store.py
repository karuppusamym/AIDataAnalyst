"""INV-1 -- single authoritative store.

**Statement.** PostgreSQL holds authoritative state. Neo4j, vector indexes, search
indexes, Redis, and object-storage indexes are rebuildable projections and are never
read as truth for an authorization, approval, or correctness decision.

**Enforcement.** Projections are written only by outbox projectors, never by
request-path code. No service dual-writes PostgreSQL and a projection.

**Why it is Tier 0.** The rejected-architectures table names "dual-write to
PostgreSQL and Neo4j" explicitly, with the reason: unreconcilable divergence. Once
two stores can both be written directly there is no procedure that tells you which
one is right, and every answer the platform gives becomes unfalsifiable. The
"rebuildable" half is what makes the projection disposable -- if the graph can be
deleted and replayed, its divergence is an inconvenience; if it cannot, it is data
loss.

**What is proven here, and what still needs the drill.** The dual-write prohibition
and the read-only-outside-projectors rule are proven exhaustively: every Cypher
statement in `src/aida` is extracted and classified, so a write added anywhere but
the projector package fails immediately. The rebuild property is proven as *replay
determinism from authoritative state* -- the projector is driven twice against a
fixed PostgreSQL fixture with the graph discarded in between, and the two
projections must be identical and must account for every authoritative row.

That is not the same as the live drill. It proves the projection is a pure function
of PostgreSQL and that a wipe-and-replay reproduces it; it does not prove Neo4j
ingests it correctly, because no Neo4j is running. Gap item E5 (projection rebuild
drill) remains open and this suite does not claim otherwise. What it does remove is
the failure mode where the projector silently stopped being replayable months before
anyone tried the drill.
"""

import ast
import re
from typing import Any
from uuid import UUID

import pytest

from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
)
from tests.support.app_surface import SRC_ROOT
from tests.support.doubles import ModelRoutedSession, RecordingGraphDriver

_PROJECTOR_PACKAGE = "projectors"

# Cypher clauses that change graph state. `CREATE` appears both as a write and in
# schema DDL (`CREATE CONSTRAINT`, `CREATE INDEX`), which the classifier separates
# below -- schema DDL is idempotent setup, not a projection write.
_WRITE_CLAUSES = ("MERGE", "SET ", "DELETE", "DETACH", "REMOVE", "CREATE")
_SCHEMA_DDL = re.compile(r"CREATE\s+(CONSTRAINT|INDEX)", re.IGNORECASE)

# The two neo4j driver entry points that take a Cypher statement. Finding Cypher
# by *call site* rather than by looking for keywords in every string literal is
# what keeps the scan honest: "MATCH" and "RETURN" appear in SQL, in docstrings
# and in prose all over this codebase, and a keyword scan reports thirty modules
# as graph clients when two of them are.
_GRAPH_CALLS = frozenset({"run", "execute_query"})
_GRAPH_RECEIVER_HINTS = ("graph_session", "graph", "driver")


def _statement_text(node: ast.AST) -> str | None:
    """The literal text of a Cypher argument, including f-string constant parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        return "".join(parts) if parts else None
    return None


def _cypher_literals() -> list[tuple[str, str]]:
    """Every Cypher statement in `src/aida`, as (module path, statement).

    Located by AST: a call to `.run(...)` or `.execute_query(...)` on a receiver
    whose name marks it as a graph session or driver. That is narrow enough to
    exclude SQLAlchemy's `session.execute` and an orchestrator's `.run()`, and
    wide enough to catch a statement written as an f-string, which
    `lineage_graph_store` does for its variable-depth traversal.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = str(path.relative_to(SRC_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _GRAPH_CALLS:
                continue
            receiver = func.value
            receiver_name = ""
            if isinstance(receiver, ast.Name):
                receiver_name = receiver.id
            elif isinstance(receiver, ast.Attribute):
                receiver_name = receiver.attr
            if not any(hint in receiver_name.lower() for hint in _GRAPH_RECEIVER_HINTS):
                continue
            for argument in node.args:
                text = _statement_text(argument)
                if text:
                    found.append((relative, text))
    return found


def _is_write(statement: str) -> bool:
    stripped = _SCHEMA_DDL.sub("", statement)
    return any(clause in stripped.upper() for clause in _WRITE_CLAUSES)


def test_the_cypher_scan_finds_the_statements_it_is_supposed_to() -> None:
    """Tripwire for the extraction below. If Cypher moved into a resource file,
    a template, or a driver helper, every INV-1 scan in this module would pass by
    examining nothing.
    """
    literals = _cypher_literals()
    assert len(literals) >= 10, f"only {len(literals)} Cypher statements found"
    assert any(path.startswith(_PROJECTOR_PACKAGE) for path, _ in literals)
    assert any(not path.startswith(_PROJECTOR_PACKAGE) for path, _ in literals)
    assert any("MERGE" in statement.upper() for _, statement in literals)
    assert any("MATCH" in statement.upper() for _, statement in literals)


def test_projections_are_written_only_by_projectors() -> None:
    """INV-1's enforcement clause: no request-path code writes a projection.

    Classifies every Cypher statement in `src/aida` as read or write and requires
    every write to live in the projector package. Prevents the dual-write the
    architecture explicitly rejected -- the one that produces a graph and a
    database that disagree with no way to say which is right.
    """
    offenders = sorted(
        {
            path
            for path, statement in _cypher_literals()
            if _is_write(statement) and not path.startswith(_PROJECTOR_PACKAGE)
        }
    )
    assert offenders == [], (
        "these modules outside the projector package write to the graph; INV-1 "
        f"forbids dual-writing PostgreSQL and a projection: {offenders}"
    )


def test_request_path_graph_access_is_read_only_and_closed() -> None:
    """INV-1's other half: a projection may be *read* on the request path (that is
    what it is for), but only from modules that are known to fall back to
    PostgreSQL when the graph is unavailable or disagrees.

    The list is closed rather than pattern-matched so that a new module reaching
    into Neo4j is a reviewable change. Both current entries degrade to the
    authoritative store by design: `api.get_graph_summary` reconciles the graph's
    counts against PostgreSQL before returning them, and
    `lineage_graph_store.read_bounded_impact` returns `None` on any graph failure
    so the caller recomputes from PostgreSQL.
    """
    permitted_readers = {
        "api.py": "graph summary, reconciled against PostgreSQL counts in the same handler",
        "lineage_graph_store.py": (
            "bounded lineage impact; returns None on any graph error so PostgreSQL "
            "remains the fallback authority"
        ),
    }
    readers = sorted(
        {
            path
            for path, _ in _cypher_literals()
            if not path.startswith(_PROJECTOR_PACKAGE)
        }
    )
    unexpected = [path for path in readers if path not in permitted_readers]
    assert unexpected == [], (
        "these modules read the graph on the request path and are not on the "
        f"reviewed list: {unexpected}"
    )
    stale = sorted(set(permitted_readers) - set(readers))
    assert stale == [], f"permitted_readers names modules that no longer read the graph: {stale}"


def test_projection_writes_are_idempotent() -> None:
    """P5 and INV-1 together: a projection that cannot be replayed safely is not
    rebuildable, and replay is the *normal* case for an at-least-once consumer.

    Every write statement in the projector package must create nodes and
    relationships with `MERGE`, never a bare `CREATE`. Prevents the projector that
    works perfectly until Kafka redelivers a message and doubles every node.
    """
    non_idempotent = []
    for path, statement in _cypher_literals():
        if not path.startswith(_PROJECTOR_PACKAGE) or not _is_write(statement):
            continue
        without_ddl = _SCHEMA_DDL.sub("", statement).upper()
        if "CREATE" in without_ddl:
            non_idempotent.append(f"{path}: {statement.strip()[:80]}")
    assert non_idempotent == [], (
        "these projector writes use CREATE rather than MERGE and are not safe to "
        f"replay: {non_idempotent}"
    )


# --- rebuild from authoritative state ---------------------------------------

_ORGANIZATION_ID = UUID("11111111-1111-1111-1111-111111111111")
_DATASOURCE_ID = UUID("22222222-2222-2222-2222-222222222222")
_CATALOG_ID = UUID("33333333-3333-3333-3333-333333333333")
_SCHEMA_ID = UUID("44444444-4444-4444-4444-444444444444")
_TABLE_ID = UUID("55555555-5555-5555-5555-555555555555")
_COLUMN_ID = UUID("66666666-6666-6666-6666-666666666666")
_CONSTRAINT_ID = UUID("77777777-7777-7777-7777-777777777777")


def _authoritative_state() -> dict[type, list[Any]]:
    """One datasource's complete metadata, as PostgreSQL would hold it.

    Fixed UUIDs rather than `uuid4()`: the rebuild assertion compares two
    projections for equality, and random identities would make an equal result
    prove only that the same objects were passed twice.
    """
    return {
        MetadataCatalog: [
            MetadataCatalog(
                id=_CATALOG_ID,
                organization_id=_ORGANIZATION_ID,
                datasource_id=_DATASOURCE_ID,
                name="analytics_db",
                status="ACTIVE",
                fingerprint="f" * 64,
            )
        ],
        MetadataSchema: [
            MetadataSchema(
                id=_SCHEMA_ID,
                organization_id=_ORGANIZATION_ID,
                catalog_id=_CATALOG_ID,
                name="analytics",
                status="ACTIVE",
                fingerprint="f" * 64,
            )
        ],
        MetadataTable: [
            MetadataTable(
                id=_TABLE_ID,
                organization_id=_ORGANIZATION_ID,
                datasource_id=_DATASOURCE_ID,
                schema_id=_SCHEMA_ID,
                name="customers",
                object_type="BASE_TABLE",
                status="ACTIVE",
                fingerprint="f" * 64,
            )
        ],
        MetadataColumn: [
            MetadataColumn(
                id=_COLUMN_ID,
                organization_id=_ORGANIZATION_ID,
                table_id=_TABLE_ID,
                name="customer_id",
                ordinal_position=1,
                physical_type="uuid",
                nullable=False,
                classification="INTERNAL",
                status="ACTIVE",
                fingerprint="f" * 64,
            )
        ],
        MetadataConstraint: [
            MetadataConstraint(
                id=_CONSTRAINT_ID,
                organization_id=_ORGANIZATION_ID,
                table_id=_TABLE_ID,
                name="pk_customers",
                constraint_type="PRIMARY_KEY",
                columns=["customer_id"],
                referenced_table_id=None,
                referenced_columns=[],
                status="ACTIVE",
                fingerprint="f" * 64,
            )
        ],
    }


def _authoritative_datasource() -> DataSource:
    return DataSource(
        id=_DATASOURCE_ID,
        organization_id=_ORGANIZATION_ID,
        line_of_business_id=UUID("88888888-8888-8888-8888-888888888888"),
        data_domain_id=UUID("99999999-9999-9999-9999-999999999999"),
        project_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        name="rebuild-fixture",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://rebuild",
        status="ACTIVE",
    )


async def _project_once(monkeypatch: pytest.MonkeyPatch) -> RecordingGraphDriver:
    """Run the real projector against the fixture, into an empty graph."""
    from aida.projectors import graph_projector

    session = ModelRoutedSession(
        rows_by_model=_authoritative_state(),
        get_results={_DATASOURCE_ID: _authoritative_datasource()},
    )
    monkeypatch.setattr(graph_projector, "session_factory", lambda: session)

    driver = RecordingGraphDriver()
    await graph_projector.project_discovery(
        driver,
        {
            "event_id": "evt-rebuild",
            "organization_id": str(_ORGANIZATION_ID),
            "event_type": "metadata.discovery.completed.v1",
            "payload": {"datasource_id": str(_DATASOURCE_ID)},
        },
    )
    return driver


async def test_projection_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-1: the projection is rebuildable -- delete it entirely, replay from
    authoritative state, and get the same graph back.

    Runs the real `project_discovery` twice against the same PostgreSQL fixture
    with the recorded graph discarded in between, and requires the two projections
    to be byte-identical. Equality is the whole claim: if the projector's output
    depended on anything but authoritative state -- the previous graph contents, a
    wall clock, a set iteration order -- a rebuild would not reproduce it, and the
    "just replay it" recovery procedure the architecture depends on would silently
    not work.

    Does not prove Neo4j applies the projection correctly; see the module
    docstring and gap item E5.
    """
    first = await _project_once(monkeypatch)
    # "delete the graph entirely": the second run starts from a fresh recorder
    # with no knowledge of the first.
    second = await _project_once(monkeypatch)

    assert first.log, "the projector wrote nothing; there is no projection to rebuild"
    assert first.log == second.log, (
        "replaying from identical authoritative state produced a different "
        "projection; the projector is not a pure function of PostgreSQL and "
        "cannot be rebuilt by replay"
    )


async def test_the_rebuilt_projection_accounts_for_every_authoritative_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-1's "assert full reconstruction": a replay that loses rows is not a
    rebuild, it is a partial restore that nobody notices until a lineage query
    comes back short.

    Compares the platform ids present in the projected node payloads against the
    ids of every authoritative row in the fixture. Prevents the projector that
    keeps working after a load query starts filtering something out.
    """
    driver = await _project_once(monkeypatch)

    projected_ids = {row["platform_id"] for row in driver.projected_rows()}
    expected_ids = {
        str(instance.id)
        for instances in _authoritative_state().values()
        for instance in instances
    }
    assert expected_ids <= projected_ids, (
        "the rebuilt projection is missing authoritative rows: "
        f"{sorted(expected_ids - projected_ids)}"
    )


async def test_every_projected_node_carries_its_tenant_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-5 inside INV-1: "graph nodes preserve these boundaries".

    A projection is a second copy of the metadata, and a copy without the tenancy
    path is a copy that cannot be filtered -- a domain-scoped traversal would have
    to walk edges first and check afterwards, which is how a cross-tenant lineage
    result gets returned before anything notices.
    """
    driver = await _project_once(monkeypatch)
    rows = driver.projected_rows()
    assert rows, "nothing was projected"

    for row in rows:
        for boundary in ("organization_id", "line_of_business_id", "data_domain_id", "project_id"):
            assert row.get(boundary), (
                f"a projected node is missing its {boundary}: {row}"
            )
        assert row["organization_id"] == str(_ORGANIZATION_ID)


async def test_the_projection_carries_no_source_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6 inside INV-1: "graph nodes ... preserve these boundaries" is not the
    only constraint on a projection -- it is a control-plane store like any other,
    so it may hold names, types and classifications but never a source value.

    Enumerates every key of every projected node against the set the projector is
    supposed to emit, so a new field added to the projection payload has to be
    justified here rather than shipped silently.
    """
    driver = await _project_once(monkeypatch)
    permitted_keys = {
        "platform_id",
        "organization_id",
        "line_of_business_id",
        "data_domain_id",
        "project_id",
        "datasource_id",
        "catalog_id",
        "schema_id",
        "table_id",
        "name",
        "status",
        "object_type",
        "ordinal_position",
        "physical_type",
        "classification",
        "constraint_type",
        "columns",
        "referenced_table_id",
        "referenced_columns",
    }
    unexpected = sorted(
        {key for row in driver.projected_rows() for key in row} - permitted_keys
    )
    assert unexpected == [], (
        "the graph projection gained fields that are not structural metadata; "
        f"INV-6 keeps source values out of every control-plane store: {unexpected}"
    )
