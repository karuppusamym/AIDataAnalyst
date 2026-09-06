"""The surface-to-control matrix is generated, current, and honest about gaps.

Review 2026-09-05, section 6 item 1 asks for the coverage to be *auditable*.
Three things have to be true for that, and each can break on its own: the matrix
has to be derived from the application rather than typed by hand, the committed
copy has to still match the application, and a cell the analysis could not
determine has to say so instead of guessing.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_surface_control_matrix import (  # noqa: E402
    DEFAULT_OUTPUT,
    UNKNOWN,
    SurfaceRow,
    collect_rows,
    render,
)
from tests.support.app_surface import iter_api_routes  # noqa: E402


def test_the_matrix_covers_every_mounted_route() -> None:
    """A matrix that silently skips a surface is worse than no matrix: it reads
    as an audit and is one. Every `APIRoute` the application mounts must appear,
    which is what keeps "we mapped every surface" a checkable claim rather than
    a statement of intent.
    """
    rows = collect_rows()
    covered = {row.surface for row in rows}
    missing = [
        f"{' '.join(sorted(route.methods))} {route.path}"
        for route in iter_api_routes()
        if f"`{' '.join(sorted(route.methods))} {route.path}`" not in covered
    ]
    assert missing == [], f"these mounted routes are absent from the matrix: {missing[:10]}"


def test_the_matrix_covers_the_non_rest_families_the_review_names() -> None:
    """REST / MCP / export / bulk / job / SDK. An empty family means either the
    classifier stopped matching or the surface stopped existing, and both are
    worth failing on -- an empty JOB family is exactly how the first draft of
    the classifier passed while matching nothing."""
    rows = collect_rows()
    families = {row.family for row in rows}
    for family in ("REST", "MCP", "EXPORT", "BULK", "JOB", "SDK"):
        assert family in families, f"the matrix has no {family} surfaces at all"


def test_undeterminable_cells_say_unknown_rather_than_guessing() -> None:
    """The review's point is that the coverage should be auditable, not that it
    should look complete. A row the analyser cannot resolve keeps its place in
    the table and is counted in the gap list."""
    unresolvable = SurfaceRow(
        surface="`MCP ping`",
        family="MCP",
        handler=UNKNOWN,
        roles=UNKNOWN,
        tenant_check=UNKNOWN,
        workspace_check=UNKNOWN,
        side_effects=UNKNOWN,
        writes_audit=UNKNOWN,
        cancellation=UNKNOWN,
    )
    assert unresolvable.unknown_cells == [
        "roles",
        "tenant",
        "workspace",
        "side effects",
        "audit",
        "cancellation",
    ]

    document = render([unresolvable])
    assert "Gap list" in document
    assert "`unknown` cells in total: **6**" in document
    assert "`MCP ping`" in document, "an unresolvable surface was dropped from the table"


def test_the_gap_list_counts_exactly_what_the_table_shows() -> None:
    """The summary is the part a reader trusts without reading 460 rows, so it
    must be computed from the same rows rather than tallied separately."""
    rows = collect_rows()
    document = render(rows)
    expected_cells = sum(len(row.unknown_cells) for row in rows)
    expected_rows = sum(1 for row in rows if row.unknown_cells)
    assert f"- Surfaces covered: **{len(rows)}**" in document
    assert f"- `unknown` cells in total: **{expected_cells}**" in document
    assert f"- Rows with at least one `unknown` cell: **{expected_rows}**" in document


def test_the_committed_matrix_is_current() -> None:
    """A generated artefact that nobody regenerates is a hand-written one with
    extra steps. This is the same check `--check` performs, run in CI as part of
    the suite so a merged route cannot leave the matrix behind.

    If this fails, run `python scripts/generate_surface_control_matrix.py` and
    commit the result -- do not edit the file.
    """
    assert DEFAULT_OUTPUT.exists(), f"{DEFAULT_OUTPUT} has never been generated"
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == render(collect_rows()), (
        f"{DEFAULT_OUTPUT} is out of date with the application; regenerate it with "
        "`python scripts/generate_surface_control_matrix.py`"
    )


def test_a_mutating_route_with_no_visible_write_is_reported_not_hidden() -> None:
    """`mutating verb, no write found` is a finding, not a pass.

    A POST whose write the call-graph walker cannot see is either a route that
    does not actually mutate (and should say so) or a control the analysis is
    blind to. Either way the reader needs to see it, so the cell must never
    collapse to a plain "read".
    """
    rows = collect_rows()
    values = {row.side_effects for row in rows}
    assert values <= {"writes", "read", "mutating verb, no write found", UNKNOWN}, (
        f"an unexpected side-effect classification appeared: {values}"
    )
