"""R11-FP03 remainder: selection patterns that follow a member's package (Oracle).

**The contract** (`discovery_selection.routine_in_scope`): a member subprogram of an Oracle
package enters and leaves the scan with its package -- judged as kind PACKAGE under the
name `schema.package`, never by its own name or kind. Including the package (by pattern
or by the PACKAGE kind) reaches every member; excluding it excludes every member.

**What is wired today, and what is not.** A FULL run retires every existing routine it did
not see unless `workflows.activities.out_of_scope_existing` counts it as out of scope --
and that function still judges a member by its own name and kind. So the scan may only
drop a member that rule also counts as out of scope. The include half of the contract
only ever *admits* members, which is safe, and is live. The exclude half would drop a
member the retirement rule counts as in scope -- and tombstone it -- so it waits for the
retirement half to call `routine_in_scope` in the same change. The last test pins the
safety property over a grid of selections, which is what would catch the exclude half
being wired alone.
"""

from __future__ import annotations

import itertools

import pytest

from aida.connectors.base import DiscoveredCatalog, DiscoveredRoutine, DiscoveredSchema
from aida.discovery_selection import (
    DiscoverySelection,
    apply_selection,
    routine_in_scope,
)


def _routine(name: str, routine_type: str, package: str | None = None) -> DiscoveredRoutine:
    return DiscoveredRoutine(
        name=name,
        routine_type=routine_type,
        language=None,
        body_sql=None,
        attributes={"package_name": package} if package else {},
    )


def _catalog() -> tuple[DiscoveredCatalog, ...]:
    """HR: the package RISK_PKG and its two members, and a standalone SCORE function
    that shares a member's name -- the pair R11-FP03's identity work keeps apart."""
    return (
        DiscoveredCatalog(
            name="BANK",
            schemas=(
                DiscoveredSchema(
                    name="HR",
                    tables=(),
                    routines=(
                        _routine("RISK_PKG", "PACKAGE"),
                        _routine("SCORE", "FUNCTION", package="RISK_PKG"),
                        _routine("RECALC", "PROCEDURE", package="RISK_PKG"),
                        _routine("SCORE", "FUNCTION"),
                    ),
                ),
            ),
        ),
    )


def _kept(selection: DiscoverySelection) -> set[tuple[str, str]]:
    outcome = apply_selection(_catalog(), selection)
    return {
        (routine.attributes.get("package_name") or "", routine.name)
        for catalog in outcome.catalogs
        for schema in catalog.schemas
        for routine in schema.routines
    }


def test_including_a_package_by_name_reaches_its_members() -> None:
    """Before: `hr.risk_pkg` kept the package and dropped both members, because each was
    judged by its own name -- the selection reached the container and none of it."""
    kept = _kept(DiscoverySelection(include_objects=["hr.risk_pkg"]))

    assert kept == {("", "RISK_PKG"), ("RISK_PKG", "SCORE"), ("RISK_PKG", "RECALC")}


def test_the_package_kind_reaches_its_members_and_not_a_standalone_function() -> None:
    kept = _kept(DiscoverySelection(object_kinds=["PACKAGE"]))

    assert kept == {("", "RISK_PKG"), ("RISK_PKG", "SCORE"), ("RISK_PKG", "RECALC")}
    assert ("", "SCORE") not in kept


def test_a_standalone_routine_is_judged_exactly_as_before() -> None:
    kept = _kept(DiscoverySelection(include_objects=["hr.score"]))

    assert ("", "SCORE") in kept
    assert ("", "RISK_PKG") not in kept


def test_the_full_contract_excludes_members_with_their_package() -> None:
    """`routine_in_scope` is the contract, both halves -- the exclude half included."""
    excluding = DiscoverySelection(exclude_objects=["hr.risk_pkg"])
    assert routine_in_scope(excluding, "HR", "SCORE", "FUNCTION", "RISK_PKG") is False
    assert routine_in_scope(excluding, "HR", "RECALC", "PROCEDURE", "RISK_PKG") is False
    assert routine_in_scope(excluding, "HR", "SCORE", "FUNCTION", None) is True

    # A member is never selectable apart from its package, by name or by kind.
    by_name = DiscoverySelection(include_objects=["hr.score"])
    assert routine_in_scope(by_name, "HR", "SCORE", "FUNCTION", "RISK_PKG") is False
    by_kind = DiscoverySelection(object_kinds=["FUNCTION"])
    assert routine_in_scope(by_kind, "HR", "SCORE", "FUNCTION", "RISK_PKG") is False


def test_excluding_a_package_drops_its_members_from_the_scan() -> None:
    """The exclude half, wired now that `out_of_scope_existing` calls `routine_in_scope`
    too: both halves judge a member by its package, so dropping it here is safe."""
    kept = _kept(DiscoverySelection(exclude_objects=["hr.risk_pkg"]))

    assert kept == {("", "SCORE")}


_PATTERNS = ["hr.risk_pkg", "hr.score", "hr.*", "hr.risk*", "sales.*"]
_KINDS = [["PACKAGE"], ["FUNCTION"], ["PROCEDURE"], ["PACKAGE", "FUNCTION"], []]


def _grid() -> list[DiscoverySelection]:
    selections = []
    for kinds, include, exclude in itertools.product(
        _KINDS, [None, *_PATTERNS], [None, *_PATTERNS]
    ):
        selections.append(
            DiscoverySelection(
                object_kinds=kinds,
                include_objects=[include] if include else [],
                exclude_objects=[exclude] if exclude else [],
            )
        )
    return selections


@pytest.mark.parametrize("selection", _grid(), ids=lambda s: s.model_dump_json())
def test_the_scan_never_drops_a_routine_the_retirement_rule_counts_as_in_scope(
    selection: DiscoverySelection,
) -> None:
    """Retirement safety, over every combination of the kinds and patterns above.

    `out_of_scope_existing` counts a routine as seen when `routine_in_scope` is False.
    Anything the scan drops must be so counted, or a FULL run reads the drop as a
    deletion -- and since both halves now call the same function, they agree exactly.
    """
    kept = _kept(selection)
    for routine in _catalog()[0].schemas[0].routines:
        package = routine.attributes.get("package_name") or None
        identity = (package or "", routine.name)
        in_scope = routine_in_scope(selection, "HR", routine.name, routine.routine_type, package)
        assert (identity in kept) is in_scope, identity
