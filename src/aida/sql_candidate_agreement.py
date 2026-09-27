"""R11-MP07: do two independently generated statements agree?

DataPilot asks a second, cheaper model for a candidate and accepts when both
return the same rows. Executing a second candidate against a bank source is a
cost and a load this platform does not take on for a signal, so the comparison
here is structural and runs on the SQL text alone:

* ``IDENTICAL`` -- the same statement once both are parsed and rendered in one
  canonical form (case, whitespace and identifier quoting normalised away).
* ``SAME_SOURCES`` -- different statements over the same tables and columns: the
  models agree on *where* the answer is and differ in how they compute it.
* ``DIFFERENT`` -- they read different tables or columns.
* ``UNPARSEABLE`` -- one of them does not parse in the source dialect.

Agreement is evidence about the answer, recorded beside it; it changes nothing
about which statement runs. Only counts and the level are returned -- never an
identifier list or SQL text -- so the result can go into plan evidence without
a second copy of either statement (INV-6).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError


class AgreementLevel(StrEnum):
    IDENTICAL = "IDENTICAL"
    SAME_SOURCES = "SAME_SOURCES"
    DIFFERENT = "DIFFERENT"
    UNPARSEABLE = "UNPARSEABLE"


@dataclass(frozen=True, slots=True)
class CandidateAgreement:
    level: AgreementLevel
    shared_tables: int
    primary_only_tables: int
    candidate_only_tables: int

    def evidence(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "shared_tables": self.shared_tables,
            "primary_only_tables": self.primary_only_tables,
            "candidate_only_tables": self.candidate_only_tables,
        }


def _parse(sql: str, dialect: str) -> exp.Expr | None:
    try:
        return parse_one(sql, read=dialect)
    except (ParseError, TokenError, ValueError):
        return None


def _tables(tree: exp.Expr) -> frozenset[str]:
    names: set[str] = set()
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name or name in ctes:
            continue
        schema = table.db.lower() if table.db else ""
        names.add(f"{schema}.{name}" if schema else name)
    return frozenset(names)


def _columns(tree: exp.Expr) -> frozenset[str]:
    return frozenset(column.name.lower() for column in tree.find_all(exp.Column) if column.name)


def _canonical(tree: exp.Expr, dialect: str) -> str:
    """One rendering for comparison only: every identifier unquoted and lower-cased,
    then the whole statement printed in the dialect and lower-cased."""
    copy = tree.copy()
    for identifier in copy.find_all(exp.Identifier):
        identifier.set("this", str(identifier.this).lower())
        identifier.set("quoted", False)
    return copy.sql(dialect=dialect, pretty=False).lower()


def compare_candidates(primary_sql: str, candidate_sql: str, *, dialect: str) -> CandidateAgreement:
    """How far two statements for the same question agree. Pure; no I/O."""
    primary = _parse(primary_sql, dialect)
    candidate = _parse(candidate_sql, dialect)
    if primary is None or candidate is None:
        return CandidateAgreement(AgreementLevel.UNPARSEABLE, 0, 0, 0)
    primary_tables, candidate_tables = _tables(primary), _tables(candidate)
    shared = len(primary_tables & candidate_tables)
    primary_only = len(primary_tables - candidate_tables)
    candidate_only = len(candidate_tables - primary_tables)
    if _canonical(primary, dialect) == _canonical(candidate, dialect):
        level = AgreementLevel.IDENTICAL
    elif primary_tables == candidate_tables and _columns(primary) == _columns(candidate):
        level = AgreementLevel.SAME_SOURCES
    else:
        level = AgreementLevel.DIFFERENT
    return CandidateAgreement(level, shared, primary_only, candidate_only)
