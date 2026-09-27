"""The live sample estate the R11-FP13 answer corpus is scored over.

Tracker row R11-FP13 records the answer evaluation as blocked on two things, one
of which is an estate: `infra/sample-source/init.sql` seeded three operational
tables with no view, no routine, no derivable lineage and no column whose meaning
was anything other than its name, so
`scripts/answer_evaluation_benchmark.py --live` refused rather than grade the
wrong estate. Nothing in the repository read that file, so nothing noticed.

This module reads it. Every assertion here is derived from the two corpora --
`answer_evaluation_corpus.json` and `footprint_enrichment_corpus.json` -- and from
`scripts/quality_benchmark.py`'s own fixture constants, rather than from a list of
object names somebody kept in step by hand. A corpus case that names an object the
live estate does not build fails here, and so does an estate object that quietly
breaks the corpora's calibration.

What this module does **not** claim. It does not execute a line of SQL against a
database: PostgreSQL's own acceptance of this DDL is a separate check
(`scripts/verify_database_footprint_live.py` is the pattern, and it needs a running
container). What it proves is the property the footprint product actually rests on
-- that Atlas's *own* parsers derive the lineage the corpora depend on from the
bodies as they would be **stored**, i.e. after `redact_for_storage`, because the
stored form is the only form the lineage agent and a reviewer ever see.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aida.procedure_lineage import parse_procedure_lineage
from aida.sql_lineage_parser import parse_view_lineage
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES, redact_for_storage
from scripts.quality_benchmark import FOOTPRINT_CONCEPT, FOOTPRINT_ROUTINE_DESCRIPTION
from tests.test_inv6_value_freedom import _VALUE_BEARING_COLUMN_FRAGMENTS

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Module-level and re-read on every call, so a snapshot of another revision can be
#: pointed at it to show which of these assertions a given tree fails.
INIT_SQL = REPO_ROOT / "infra" / "sample-source" / "init.sql"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "quality_benchmark_corpus"
ANSWER_CORPUS = CORPUS_DIR / "answer_evaluation_corpus.json"
ENRICHMENT_CORPUS = CORPUS_DIR / "footprint_enrichment_corpus.json"

SCHEMA = "warehouse"
BEGIN_MARKER = "-- === warehouse estate: begin"
END_MARKER = "-- === warehouse estate: end ==="

#: A question word shorter than this carries no lexical signal worth asserting
#: about, and a handful of longer ones are pure grammar. Kept deliberately small:
#: every word this set removes is a word the calibration assertions stop checking.
_MIN_WORD = 4
_GRAMMAR_WORDS = frozenset(
    {"show", "give", "which", "does", "many", "were", "that", "this", "from", "with", "each"}
)

# ---------------------------------------------------------------------------
# Reading the estate out of the init script
# ---------------------------------------------------------------------------


def _warehouse_section(text: str) -> str:
    """The marked warehouse block, or "" when the tree has no such block.

    Returns empty rather than raising so the assertions below report the missing
    estate as the thing that failed, instead of every test erroring identically.
    """
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if start < 0 or end < 0:
        return ""
    return text[start:end]


def _split_statements(section: str) -> list[str]:
    """Split on `;` while respecting `$$` bodies, `'...''...'` strings and `--` comments."""
    statements: list[str] = []
    current: list[str] = []
    index = 0
    length = len(section)
    while index < length:
        char = section[index]
        if char == "-" and section.startswith("--", index):
            newline = section.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if section.startswith("$$", index):
            close = section.find("$$", index + 2)
            close = length if close < 0 else close + 2
            current.append(section[index:close])
            index = close
            continue
        if char == "'":
            cursor = index + 1
            while cursor < length:
                if section[cursor] == "'":
                    if section.startswith("''", cursor):
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            current.append(section[index:cursor])
            index = cursor
            continue
        if char == ";":
            statements.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


_TABLE_RE = re.compile(rf"^CREATE\s+TABLE\s+{SCHEMA}\.(\w+)", re.IGNORECASE)
_VIEW_RE = re.compile(rf"^CREATE\s+VIEW\s+{SCHEMA}\.(\w+)", re.IGNORECASE)
_MATVIEW_RE = re.compile(rf"^CREATE\s+MATERIALIZED\s+VIEW\s+{SCHEMA}\.(\w+)", re.IGNORECASE)
_ROUTINE_RE = re.compile(rf"^CREATE\s+(?:PROCEDURE|FUNCTION)\s+{SCHEMA}\.(\w+)", re.IGNORECASE)
_TRIGGER_RE = re.compile(r"^CREATE\s+TRIGGER\s+(\w+)", re.IGNORECASE)
_TABLE_COMMENT_RE = re.compile(
    rf"^COMMENT\s+ON\s+(?:TABLE|VIEW)\s+{SCHEMA}\.(\w+)\s+IS\s+'(.*)'$",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_COMMENT_RE = re.compile(
    rf"^COMMENT\s+ON\s+COLUMN\s+{SCHEMA}\.(\w+)\.(\w+)\s+IS\s+'(.*)'$",
    re.IGNORECASE | re.DOTALL,
)
_ROUTINE_COMMENT_RE = re.compile(
    rf"^COMMENT\s+ON\s+(?:PROCEDURE|FUNCTION)\s+{SCHEMA}\.(\w+)\([^)]*\)\s+IS\s+'(.*)'$",
    re.IGNORECASE | re.DOTALL,
)
#: An abbreviated column name -- the kind whose meaning cannot be read off the
#: name, and which therefore has to carry a comment or it is dead weight in a
#: footprint. Matched as a suffix so a new one added tomorrow is covered.
_ABBREVIATED_SUFFIXES = ("_ind", "_cd", "_bkt", "_bps", "_cls", "_cyc", "_lvl")


class Estate:
    """Everything the warehouse section declares, keyed by bare object name."""

    def __init__(self, section: str) -> None:
        self.section = section
        self.tables: dict[str, str] = {}
        self.views: dict[str, str] = {}
        self.materialized_views: dict[str, str] = {}
        self.routines: dict[str, str] = {}
        self.triggers: list[str] = []
        self.object_comments: dict[str, str] = {}
        self.column_comments: dict[tuple[str, str], str] = {}
        for statement in _split_statements(section):
            for pattern, target in (
                (_MATVIEW_RE, self.materialized_views),
                (_TABLE_RE, self.tables),
                (_VIEW_RE, self.views),
                (_ROUTINE_RE, self.routines),
            ):
                match = pattern.match(statement)
                if match:
                    target[match.group(1).casefold()] = statement
                    break
            else:
                trigger = _TRIGGER_RE.match(statement)
                if trigger:
                    self.triggers.append(trigger.group(1).casefold())
                    continue
                for pattern in (_TABLE_COMMENT_RE, _ROUTINE_COMMENT_RE):
                    match = pattern.match(statement)
                    if match:
                        self.object_comments[match.group(1).casefold()] = match.group(2)
                        break
                else:
                    column = _COLUMN_COMMENT_RE.match(statement)
                    if column:
                        self.column_comments[
                            (column.group(1).casefold(), column.group(2).casefold())
                        ] = column.group(3)

    def columns_of(self, table: str) -> list[str]:
        body = self.tables.get(table, "")
        inner = body[body.find("(") + 1 :]
        names = []
        for line in inner.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            first = stripped.split()[0].strip(",()")
            if first.upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK"}:
                continue
            if re.fullmatch(r"\w+", first):
                names.append(first.casefold())
        return names

    def ingested_text(self) -> str:
        """Everything the connector actually carries into the catalog: object and
        column names, every `COMMENT ON` text, and the view and routine bodies.

        Deliberately *not* the file's `--` prose. That prose explains the fixture to
        whoever reads it and reaches no catalog, so asserting over it would fail this
        module for describing its own calibration -- which is how the first version
        of this assertion failed.
        """
        parts: list[str] = []
        for group in (self.tables, self.views, self.materialized_views, self.routines):
            for name, statement in group.items():
                parts.append(name.replace("_", " "))
                parts.append(statement)
        parts += [name.replace("_", " ") for name in self.triggers]
        parts += list(self.object_comments.values())
        parts += list(self.column_comments.values())
        return " ".join(parts)

    def described_text(self, name: str) -> str:
        """An object's name plus every comment on it -- everything lexical retrieval
        of that object could match on, and nothing else."""
        parts = [name.replace("_", " "), self.object_comments.get(name, "")]
        parts += [
            text for (table, _column), text in self.column_comments.items() if table == name
        ]
        return " ".join(parts)


def estate() -> Estate:
    return Estate(_warehouse_section(INIT_SQL.read_text(encoding="utf-8")))


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.casefold())}


def _significant(question: str) -> set[str]:
    return {
        word
        for word in _words(question)
        if len(word) >= _MIN_WORD and word not in _GRAMMAR_WORDS
    }


def _bare(name: str) -> str:
    return name.rsplit(".", 1)[-1].strip('"').casefold()


def _answer_cases() -> list[dict]:
    return list(json.loads(ANSWER_CORPUS.read_text(encoding="utf-8"))["cases"])


def _enrichment_cases() -> list[dict]:
    return list(json.loads(ENRICHMENT_CORPUS.read_text(encoding="utf-8"))["cases"])


def _stored(sql: str) -> tuple[str, str]:
    """`(text, status)` as ingestion would persist this body -- the only form the
    lineage agent or a reviewer ever reads."""
    prepared = redact_for_storage(sql, dialect="postgres")
    assert prepared is not None and prepared.redacted is not None, "body could not be stored"
    return prepared.redacted, prepared.status


# ---------------------------------------------------------------------------
# Does the estate hold what the corpora name?
# ---------------------------------------------------------------------------


def test_the_estate_holds_every_table_the_answer_corpus_names() -> None:
    """The corpus matches a live estate's objects by bare name, so a table it names
    and the estate does not build scores as a miss no threshold would catch."""
    live = estate()
    wanted: set[str] = set()
    for case in _answer_cases():
        wanted |= {_bare(t) for t in case.get("expected_tables", ())}
        wanted |= {_bare(t) for t in case.get("forbidden_tables", ())}
        for ref in (*case.get("expected_evidence", ()), *case.get("forbidden_evidence", ())):
            if ref["object_type"] == "TABLE":
                wanted.add(_bare(ref["object_key"]))

    missing = sorted(wanted - set(live.tables))
    assert missing == [], (
        "the answer corpus names tables the live sample estate does not build, so a "
        f"--live run would score them as absent rather than as wrong: {missing}"
    )


def test_the_estate_holds_every_routine_the_corpora_name() -> None:
    live = estate()
    wanted = {
        _bare(ref["object_key"])
        for case in _answer_cases()
        for ref in (*case.get("expected_evidence", ()), *case.get("forbidden_evidence", ()))
        if ref["object_type"] == "ROUTINE"
    } | {
        _bare(case["expected_object_key"])
        for case in _enrichment_cases()
        if case["expected_object_type"] == "ROUTINE"
    }

    missing = sorted(wanted - set(live.routines))
    assert missing == [], f"routines the corpora name but the estate does not declare: {missing}"


def test_the_gold_sql_can_be_executed_against_this_estate() -> None:
    """The one metric the offline mode can never measure is result-set equivalence,
    because the fixture catalog holds metadata and no rows. It becomes measurable
    only if the estate's own schema holds the table the gold SQL names, and holds
    rows in it."""
    live = estate()
    gold = [case["gold_sql"] for case in _answer_cases() if case.get("gold_sql")]
    assert gold, "no gold_sql in the corpus; this assertion would be vacuous"

    for sql in gold:
        referenced = {_bare(name) for name in re.findall(r"\bFROM\s+([\w.\"]+)", sql, re.I)}
        unknown = sorted(referenced - set(live.tables) - set(live.views))
        assert unknown == [], f"gold_sql reads objects this estate does not build: {unknown}"
        for table in referenced:
            assert re.search(
                rf"INSERT\s+INTO\s+{SCHEMA}\.{table}\b", live.section, re.IGNORECASE
            ), f"{table} is declared but seeded with no rows, so gold_sql cannot be scored"


def test_the_estate_declares_the_object_kinds_a_footprint_is_made_of() -> None:
    """Three tables were not a footprint. A view, a materialized view over it, a
    read-only routine, two writing routines and a trigger are."""
    live = estate()
    assert live.views, "no view: view lineage has nothing to derive"
    assert live.materialized_views, "no materialized view"
    assert live.triggers, "no trigger: trigger discovery has nothing live to discover"
    assert len(live.routines) >= 3, live.routines
    assert re.search(r"\bBIGSERIAL\b", live.section, re.IGNORECASE), (
        "no sequence-backed column: sequence discovery has nothing live to discover"
    )


# ---------------------------------------------------------------------------
# Is the lineage the corpora depend on actually derivable from the bodies?
# ---------------------------------------------------------------------------


def test_the_reviewed_routines_write_edge_is_derivable_from_its_body() -> None:
    """`routine-lineage-answers-over-written-table` stands on exactly one thing: that
    the rollup's body is evidence it produces the balance figures. No foreign key
    and no shared word connects the two tables, so if the body does not parse, that
    case is measuring a hand-written fixture edge and nothing else."""
    live = estate()
    body, status = _stored(live.routines["nightly_settlement_rollup"])
    result = parse_procedure_lineage(body, "postgres")

    assert status in VALUE_FREE_REDACTION_STATUSES
    assert result.is_fully_parsed, result.errors
    assert result.is_read_only is False, "a routine that writes must not read as read-only"
    writes = {
        (_bare(edge.source_table), _bare(edge.target_table))
        for edge in result.edges
        if edge.is_write and edge.source_resolved
    }
    assert ("fact_payments", "fact_account_balances") in writes, writes


def test_the_gap_routines_write_edge_is_derivable_and_transitive() -> None:
    """`gap-proposed-lineage-supports-no-answer` needs a real edge to leave
    unapproved. It arrives through a temp table, so the derived edge is transitive:
    a reviewer is deciding a derivation, which is the harder thing to leave alone."""
    live = estate()
    body, status = _stored(live.routines["quarterly_fee_accrual"])
    result = parse_procedure_lineage(body, "postgres")

    assert status in VALUE_FREE_REDACTION_STATUSES
    assert result.is_fully_parsed, result.errors
    transitive = {
        (_bare(edge.source_table), _bare(edge.target_table))
        for edge in result.edges
        if edge.is_write and edge.source_resolved and edge.via_temp_table
    }
    assert ("fact_loan_applications", "fact_fraud_alerts") in transitive, transitive


def test_the_estate_holds_one_routine_a_read_tool_could_come_from() -> None:
    live = estate()
    read_only = []
    for name, sql in live.routines.items():
        body, _ = _stored(sql)
        result = parse_procedure_lineage(body, "postgres")
        if result.is_fully_parsed and result.is_read_only:
            read_only.append(name)
    assert read_only, "every routine writes, so no read-tool blueprint is possible here"


def test_view_lineage_crosses_a_schema_boundary() -> None:
    """A view whose sources are all in its own schema proves nothing the table list
    did not already say."""
    live = estate()
    sources: set[str] = set()
    for sql in (*live.views.values(), *live.materialized_views.values()):
        body, status = _stored(sql)
        assert status in VALUE_FREE_REDACTION_STATUSES
        result = parse_view_lineage(body, "postgres")
        assert result.edges, f"no lineage derived from {sql[:60]!r}"
        sources |= {edge.source_table.casefold() for edge in result.edges if edge.source_resolved}

    assert any(source.startswith("customer.") for source in sources), sources
    assert any(source.startswith(f"{SCHEMA}.vw_") for source in sources), (
        "nothing is built on a view, so there is no two-hop lineage path"
    )


def test_every_routine_body_stores_value_free() -> None:
    """INV-6 at the one place this change could break it: a body the platform cannot
    redact is a body whose literals would be persisted."""
    live = estate()
    for name, sql in live.routines.items():
        _, status = _stored(sql)
        assert status in VALUE_FREE_REDACTION_STATUSES, f"{name} stores as {status}"


def test_no_source_column_is_named_for_a_source_value() -> None:
    """The INV-6 naming ratchet, applied to the fixture rather than to the control
    plane. A source column called `raw_value` is not itself a breach, but it is the
    shape that becomes one the moment the catalog carries the name forward, and the
    fixture should not be the thing that introduces it."""
    live = estate()
    offenders = [
        f"{table}.{column} ({hit})"
        for table in live.tables
        for column in live.columns_of(table)
        if (hit := [f for f in _VALUE_BEARING_COLUMN_FRAGMENTS if f in column])
    ]
    assert offenders == [], offenders


def test_every_abbreviated_column_carries_the_meaning_its_name_withholds() -> None:
    """The enrichment path that is pure DDL: a column called `eod_ind` means nothing,
    and its comment is the only place the meaning exists."""
    live = estate()
    undocumented = [
        f"{table}.{column}"
        for table in live.tables
        for column in live.columns_of(table)
        if column.endswith(_ABBREVIATED_SUFFIXES)
        and (table, column) not in live.column_comments
    ]
    assert undocumented == [], (
        "these columns cannot be understood from their names and carry no comment, so "
        f"the footprint has no meaning to offer for them: {undocumented}"
    )
    assert len(live.column_comments) >= 6, live.column_comments


# ---------------------------------------------------------------------------
# Does the estate keep the corpora's calibration honest?
# ---------------------------------------------------------------------------


def test_the_governed_enrichment_phrases_appear_nowhere_in_the_source() -> None:
    """The two phrases that must exist only in the control plane: the published
    concept's alias, and the steward-approved routine description. Both are read
    from `quality_benchmark`'s own constants, so if the fixture's wording changes
    this assertion changes with it instead of silently guarding the old words."""
    live = estate()
    _key, _name, aliases, _table = FOOTPRINT_CONCEPT
    _routine, description = FOOTPRINT_ROUTINE_DESCRIPTION
    haystack = live.ingested_text().casefold()

    for alias in aliases:
        assert alias.casefold() not in haystack, (
            f"the concept alias {alias!r} is in the source DDL, so the question that must "
            "only be answerable through the published concept is answerable from a name"
        )
    # The description's distinctive words, not the whole sentence: retrieval matches
    # words, and the corpus's claim is that the question shares none with the source.
    leaked = sorted(_significant(description) & _words(haystack))
    assert leaked == [], (
        "the approved routine description's own words are in the source DDL, so the "
        f"R11-FP08 case no longer needs the reviewed description to land: {leaked}"
    )


@pytest.mark.parametrize("case", _enrichment_cases(), ids=lambda c: str(c["id"]))
def test_an_enrichment_target_table_is_not_reachable_from_its_own_name(case: dict) -> None:
    """The retrieval corpus's stated invariant, asserted against the live estate:
    "No question shares a word with its target table's name or source description,
    so the target is reachable only through the enrichment.\""""
    if case["expected_object_type"] != "TABLE":
        pytest.skip("only a table target can be reached from a table's own name")
    live = estate()
    table = _bare(case["expected_object_key"])
    assert table in live.tables, table

    shared = sorted(_significant(case["question"]) & _words(live.described_text(table)))
    assert shared == [], (
        f"case {case['id']}: {table} shares {shared} with its own question, so it is "
        "reachable lexically and the case stops measuring the enrichment"
    )


@pytest.mark.parametrize("case", _answer_cases(), ids=lambda c: str(c["id"]))
def test_an_answer_corpus_target_is_not_reachable_from_its_own_name(case: dict) -> None:
    """The same invariant at answer level. The corpus declares its one exception by
    id -- the `lexical-control-*` case is deliberately reachable without enrichment,
    which is what keeps the corpus from becoming a test of enrichment alone."""
    if str(case["id"]).startswith("lexical-control-"):
        pytest.skip("the corpus's declared lexical control")
    live = estate()
    targets = {_bare(t) for t in case.get("expected_tables", ())} | {
        _bare(t) for t in case.get("forbidden_tables", ())
    }
    targets |= {
        _bare(ref["object_key"])
        for ref in (*case.get("expected_evidence", ()), *case.get("forbidden_evidence", ()))
        if ref["object_type"] == "TABLE"
    }
    if not targets:
        pytest.skip("a refusal decided before retrieval names no table")

    question = _significant(case["question"])
    for table in sorted(targets):
        assert table in live.tables, table
        shared = sorted(question & _words(live.described_text(table)))
        assert shared == [], (
            f"case {case['id']}: {table} shares {shared} with its own question, so the "
            "answer can reach it without the enrichment this case exists to score"
        )
