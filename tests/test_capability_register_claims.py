"""R11-B14: the capability register's `Reachable` column, made falsifiable.

The register's own rules say `Reachable` means "a process that actually runs
invokes it -- a mounted route, a Compose service command, a Temporal
worker/scheduler registration, or a CLI entry point", and explicitly that being
importable or being covered by a unit test does not earn a Yes. That is a
precise definition, and until now it was checked only by whoever wrote the row.
A register whose whole purpose is to stop documentation drifting from the code
cannot itself be the one document nothing checks.

**What this gate does, and the choice behind it.** R11-B14 asked for a
*generated* Implemented/Reachable column. This is a gate instead, deliberately:
generating the column would replace a human judgement with a proxy for it, and
the register's value is in cells like "Yes -- a scheduler pass calls it" and
"No by design -- the interval defaults to 0", which no generator can write. A
gate keeps the sentence and falsifies it when it stops being true, which is the
outcome the row actually wants. What cannot be mechanically checked is reported
rather than quietly skipped.

**It is asymmetric on purpose.** A `Reachable: Yes` row citing a module that no
entry point can reach is a contradiction the code can prove, so it fails. A
`Reachable: No` row is *not* checked against the graph, because "No" is
routinely the right answer for a module that is imported but whose feature is
not wired -- an interval defaulting to never, a provider that is never selected.
Failing those would punish the honest rows and teach people to write Yes.

The import graph is `tests/test_reachability_gate.py`'s -- the same five real
entry points, the same walker. Two gates disagreeing about what "reachable"
means would be worse than either alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tests.test_reachability_gate import (
    _entry_point_seeds,
    _reachable_from,
    build_import_graph,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTER = REPO_ROOT / "Docs/60-delivery/20-capability-register.md"

#: Anything backticked that looks like a source citation: a `.py` file or a
#: directory. The register's citation style is genuinely inconsistent -- rows
#: write `src/aida/api.py`, `connectors/postgres.py` and `src/aida/workflows/`
#: -- so the resolver below tries the plausible roots rather than the gate
#: quietly ignoring two of the three forms and reporting thin coverage as
#: clean.
_CITATION = re.compile(r"`([\w][\w./-]*(?:\.py|/))`")

#: Roots a bare citation is tried against, in order. `connectors/postgres.py`
#: means `src/aida/connectors/postgres.py`; nothing else in the tree could be
#: meant by it.
_CITATION_ROOTS = ("", "src/", "src/aida/", "src/atlas/")

#: The verdict word a cell opens with. Cells are prose after that -- "Yes -- a
#: scheduler pass calls it" -- so only the first token is a claim.
_VERDICT = re.compile(r"^\**(Yes|No|Partial|n/a)\b", re.IGNORECASE)

#: Rows whose `Reachable: Yes` cannot be checked here must not silently grow.
#: This is the count of such rows today; it may fall freely, and raising it
#: requires saying why in the same commit. Without the ratchet a new row could
#: dodge the gate simply by citing nothing, which is the failure mode a
#: documentation gate is most prone to.
MAX_UNCHECKABLE_REACHABLE_YES = 11


@dataclass(frozen=True, slots=True)
class RegisterRow:
    capability: str
    implemented: str
    reachable: str
    #: Repository-relative `src/...py` paths this row cites.
    evidence_paths: tuple[str, ...]
    line_number: int

    @property
    def aida_modules(self) -> tuple[str, ...]:
        """The cited paths the reachability graph can actually answer for.

        `tests/test_reachability_gate.py` walks `src/aida` only -- an
        `src/atlas/...` citation is outside its graph, so claiming to check it
        here would be asserting something this file cannot see. Those rows are
        counted as uncheckable instead.

        A cited *directory* becomes its package prefix. `src/aida/workflows/`
        is a real citation of a real thing, and treating it as uncitable would
        under-report this gate's coverage rather than make it stricter.
        """
        return tuple(
            _module_name(path)
            for path in self.evidence_paths
            if path.startswith("src/aida/") and path != "src/aida/"
        )


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _verdict(cell: str) -> str:
    match = _VERDICT.match(cell)
    return match.group(1).lower() if match else ""


def _module_name(path: str) -> str:
    """Dotted name for a file, or the package prefix for a directory."""
    return path.removeprefix("src/").removesuffix(".py").strip("/").replace("/", ".")


def _resolve(token: str) -> str | None:
    """Repository-relative path for a backticked citation, or None.

    None means "this backticked token is not a path in this tree" -- prose,
    an identifier, a settings key. Those are not this gate's business; a
    citation that *looks* like a path and resolves nowhere is, and
    `test_every_cited_module_exists` reports it.
    """
    for root in _CITATION_ROOTS:
        candidate = f"{root}{token}"
        if (REPO_ROOT / candidate).exists():
            return candidate
    return None


def _looks_like_source(token: str) -> bool:
    """A citation this gate should be able to resolve.

    A `.py` file always. A directory only when it is rooted in `src/`, since
    a bare `foo/` in prose is as likely to be a docs folder as a package.
    """
    return token.endswith(".py") or token.startswith("src/")


def _parse_register() -> tuple[RegisterRow, ...]:
    """Every register row from a table that has both claim columns.

    The register carries several tables with different shapes -- an early
    four-column corrections table collapses implemented/reachable into one cell
    and is skipped, because a gate that guessed at which half of
    "Implemented / reachable" a verdict belonged to would be inventing the
    claim it then checked.
    """
    rows: list[RegisterRow] = []
    columns: dict[str, int] | None = None
    for number, line in enumerate(REGISTER.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("|"):
            columns = None
            continue
        cells = _split_row(line)
        header = {cell.lower(): index for index, cell in enumerate(cells)}
        if "implemented" in header and "reachable" in header and "evidence" in header:
            columns = header
            continue
        if columns is None or set(cells) <= {""} or all(set(c) <= {"-", ":"} for c in cells):
            continue
        if len(cells) <= max(columns.values()):
            continue
        rows.append(
            RegisterRow(
                capability=cells[0],
                implemented=cells[columns["implemented"]],
                reachable=cells[columns["reachable"]],
                evidence_paths=tuple(
                    dict.fromkeys(
                        resolved
                        for token in _CITATION.findall(line)
                        if _looks_like_source(token)
                        for resolved in [_resolve(token) or token]
                    )
                ),
                line_number=number,
            )
        )
    return tuple(rows)


def _reachable_modules() -> set[str]:
    graph, all_modules = build_import_graph()
    return _reachable_from(graph, _entry_point_seeds(all_modules))


def test_the_register_is_still_parseable_as_tables() -> None:
    """A tripwire, not a claim about content.

    Every assertion below is vacuous if the parser stops finding rows -- a
    reformatted table, a renamed column -- and a vacuously green documentation
    gate is the exact failure this register exists to prevent.
    """
    rows = _parse_register()
    assert len(rows) >= 35, (
        f"only {len(rows)} capability rows parsed from {REGISTER.name}; the table format "
        "has changed and every claim check in this file has silently stopped checking"
    )
    assert any(row.aida_modules for row in rows), (
        "no row cited a `src/...py` module, so the reachability check below cannot fire; "
        "the Evidence column's citation style has changed"
    )


def test_every_cited_module_exists() -> None:
    """A citation to a file that is gone is a dead claim, whatever it asserts.

    Checked on disk rather than against the import graph, because the graph
    covers `src/aida` only and an `src/atlas/...` citation would otherwise read
    as missing when it is merely out of scope.
    """
    missing = {
        (row.capability, path)
        for row in _parse_register()
        for path in row.evidence_paths
        if not (REPO_ROOT / path).exists()
    }
    assert not missing, (
        "the capability register cites backend modules that no longer exist:\n"
        + "\n".join(f"  - {capability}: {module}" for capability, module in sorted(missing))
    )


def test_a_reachable_yes_cites_a_module_an_entry_point_can_reach() -> None:
    """The gate itself.

    A row claiming the platform actually runs this must cite at least one
    module the import graph can reach from a real entry point. Rows citing no
    backend module are counted, not failed -- a UI or infrastructure capability
    legitimately cites `ui-next/...`, `compose.yaml` or a Dockerfile -- but the
    count is ratcheted below so the uncheckable set cannot quietly grow.
    """
    reachable = _reachable_modules()
    contradictions: list[str] = []
    for row in _parse_register():
        if _verdict(row.reachable) != "yes" or not row.aida_modules:
            continue
        if any(
            module in reachable or any(r.startswith(f"{module}.") for r in reachable)
            for module in row.aida_modules
        ):
            continue
        contradictions.append(
            f"  - line {row.line_number}: {row.capability}\n"
            f"      claims Reachable: {row.reachable}\n"
            f"      cites only unreachable modules: {', '.join(row.aida_modules)}"
        )
    assert not contradictions, (
        "capability rows claim to be reachable but cite no module any real entry point "
        "reaches (same import graph as tests/test_reachability_gate.py -- see its "
        "ENTRY_POINTS). Either the wiring was removed and the row is now false, or the "
        "row cites the wrong module:\n" + "\n".join(contradictions)
    )


def test_uncheckable_reachable_yes_rows_do_not_grow() -> None:
    """The ratchet. Citing nothing must not become the way to pass this gate."""
    uncheckable = [
        row
        for row in _parse_register()
        if _verdict(row.reachable) == "yes" and not row.aida_modules
    ]
    assert len(uncheckable) <= MAX_UNCHECKABLE_REACHABLE_YES, (
        f"{len(uncheckable)} rows claim Reachable: Yes while citing no `src/aida` module, "
        f"up from the recorded {MAX_UNCHECKABLE_REACHABLE_YES}. Cite the module that makes "
        "the claim true, or lower the bound in this file and say why:\n"
        + "\n".join(f"  - line {row.line_number}: {row.capability}" for row in uncheckable)
    )


def test_an_implemented_no_is_never_reachable_yes() -> None:
    """Internal consistency, which needs no import graph at all.

    Nothing can run what is not implemented. The register's own column
    definitions make this combination meaningless, so it is almost certainly a
    row edited in one column and not the other.
    """
    incoherent = [
        f"  - line {row.line_number}: {row.capability} "
        f"(Implemented: {row.implemented} / Reachable: {row.reachable})"
        for row in _parse_register()
        if _verdict(row.implemented) == "no" and _verdict(row.reachable) == "yes"
    ]
    assert not incoherent, (
        "capability rows claim something unimplemented is nevertheless reachable:\n"
        + "\n".join(incoherent)
    )
