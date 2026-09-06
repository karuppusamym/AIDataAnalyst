#!/usr/bin/env python3
"""Generate the surface-to-control matrix from the running application.

Review 2026-09-05, section 6 item 1: "Map every REST/MCP/export/bulk/job/SDK
surface to tenant/workspace checks, roles, side effects, audit, and cancellation
behavior." The review's point is that the coverage should be *auditable* -- not
that it should look complete -- so this script has two rules.

**Everything is derived, nothing is asserted.** Routes come from the live
FastAPI application object; roles come from the closure FastAPI actually wired
into the route's dependency tree; tenant, workspace, audit and cancellation
answers come from walking the static call graph out of each handler. There is no
hand-maintained table anywhere in this file, because a hand-written matrix is
stale the day after it is written.

**A cell the analysis cannot determine says `unknown`.** It never guesses, and
it never omits the row. The summary counts the unknowns and lists them, because
the honest gap list is the useful output: a matrix with no unknowns produced by
a guessing analyser is worse than a matrix that says where it could not see.

What the analysis genuinely cannot see, stated plainly:

* A control enforced by data rather than by a call -- a row-level filter, a
  policy row -- is invisible to a static call-graph walk.
* A control reached through a dynamically-dispatched callable (a registry, a
  `getattr`, a handler looked up by string) is not followed.
* "Writes audit" means the handler can *reach* `record_audit`, not that it does
  so on every path through the handler. A conditional audit reads as covered
  here; only the INV-7 suite's per-route persistence tests distinguish those.

The derivation machinery is imported from `tests/support/app_surface.py` rather
than reimplemented. That module is already this repository's single authoritative
"enumerate the runtime surface from the live app" implementation -- written
expressly to never use a hand-maintained list -- and a second copy in `scripts/`
would be exactly the drifting duplicate this matrix exists to prevent.

Usage
-----
    python scripts/generate_surface_control_matrix.py            # write the doc
    python scripts/generate_surface_control_matrix.py --check    # fail if stale
    python scripts/generate_surface_control_matrix.py --stdout   # print only
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.support.app_surface import (  # noqa: E402
    iter_api_routes,
    reaches_call,
    reaches_reference,
    reaches_session_write,
    require_roles_gate,
)

DEFAULT_OUTPUT = REPO_ROOT / "Docs" / "50-security" / "surface-control-matrix.md"

UNKNOWN = "unknown"

# The call names each control is recognised by. These are the same names the
# Tier-0 invariant suites gate on (`tests/test_inv5_tenant_isolation.py`,
# `tests/test_inv4_authorization_wiring.py`, `tests/test_inv7_attributability.py`),
# so this matrix and those gates cannot disagree about what "checked" means.
TENANT_REFERENCES = frozenset({"organization_id"})
WORKSPACE_CALLS = frozenset({"gate", "gate_read", "resolve_workspace", "authorize_enforced"})
AUDIT_CALLS = frozenset({"record_audit"})
CANCELLATION_CALLS = frozenset(
    {
        # Cooperative, checked-between-chunks stops.
        "_batch_control_status",
        "BatchControlSignal",
        "is_cancelled",
        "check_cancelled",
        # Operator-initiated cancellation of a long-running run.
        "cancel_analysis_run",
        "request_cancellation",
    }
)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# How a path is classified into the surface families the review names. Ordered:
# the first pattern that matches wins, so `/exports/.../bulk` is an export.
SURFACE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MCP", ("/mcp",)),
    ("EXPORT", ("/export", "/exports", "/download")),
    ("BULK", ("/bulk", "/batch", "/batches")),
    # A "job" surface is one that starts, inspects, resumes or stops a
    # long-running unit of work. Matched on path segments rather than on a
    # leading slash because the estate names them `analysis-runs`,
    # `agent-runs`, `certification-runs` and so on -- a `/runs` prefix test
    # would have found none of them and reported an empty JOB family.
    (
        "JOB",
        (
            "-runs",
            "/runs",
            "/jobs",
            "/workflows",
            "/schedule",
            "/reaper",
            "/tasks",
            "/run",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class SurfaceRow:
    """One surface and every control cell derived for it.

    `unknown_cells` is part of the row rather than computed at render time so the
    summary counts exactly what the table shows.
    """

    surface: str
    family: str
    handler: str
    roles: str
    tenant_check: str
    workspace_check: str
    side_effects: str
    writes_audit: str
    cancellation: str

    @property
    def unknown_cells(self) -> list[str]:
        return [
            name
            for name, value in (
                ("roles", self.roles),
                ("tenant", self.tenant_check),
                ("workspace", self.workspace_check),
                ("side effects", self.side_effects),
                ("audit", self.writes_audit),
                ("cancellation", self.cancellation),
            )
            if value == UNKNOWN
        ]


def _family(path: str) -> str:
    lowered = path.lower()
    for family, patterns in SURFACE_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return family
    return "REST"


def _analysable(module: str, name: str) -> bool:
    """Whether the call-graph walker can see this handler's source at all.

    A handler defined outside the walked packages (a third-party router, a
    dynamically-created endpoint) yields `unknown` for every derived cell rather
    than a confident "no".
    """
    return module.startswith(("aida.", "atlas.")) and bool(name)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _rest_rows() -> list[SurfaceRow]:
    rows: list[SurfaceRow] = []
    for route in iter_api_routes():
        endpoint = route.endpoint
        module, name = endpoint.__module__, endpoint.__name__
        methods = sorted(route.methods or set())
        mutating = bool(set(methods) & MUTATING_METHODS)
        gate = require_roles_gate(route)
        # Sorted, not in declaration order: `require_roles(*COMPILER_ROLES)`
        # expands a set constant, so the tuple FastAPI closed over has no stable
        # order between processes -- and a generated file that reshuffles itself
        # every run cannot be diffed, reviewed, or checked for staleness. The
        # roles are an any-of set, so ordering carries no meaning to lose.
        roles = ", ".join(sorted(gate[1])) if gate else "none declared"

        if not _analysable(module, name):
            rows.append(
                SurfaceRow(
                    surface=f"`{' '.join(methods)} {route.path}`",
                    family=_family(route.path),
                    handler=f"{module}.{name}",
                    roles=roles if gate else UNKNOWN,
                    tenant_check=UNKNOWN,
                    workspace_check=UNKNOWN,
                    side_effects=UNKNOWN,
                    writes_audit=UNKNOWN,
                    cancellation=UNKNOWN,
                )
            )
            continue

        writes = reaches_session_write(module, name)
        rows.append(
            SurfaceRow(
                surface=f"`{' '.join(methods)} {route.path}`",
                family=_family(route.path),
                handler=f"{module}.{name}",
                roles=roles,
                tenant_check=_yes_no(reaches_reference(module, name, TENANT_REFERENCES)),
                workspace_check=_yes_no(reaches_call(module, name, WORKSPACE_CALLS)),
                side_effects=(
                    "writes"
                    if writes
                    else ("mutating verb, no write found" if mutating else "read")
                ),
                writes_audit=_yes_no(reaches_call(module, name, AUDIT_CALLS)),
                cancellation=(
                    "cooperative"
                    if reaches_call(module, name, CANCELLATION_CALLS)
                    else "not cancellable"
                ),
            )
        )
    return rows


def _mcp_rows() -> list[SurfaceRow]:
    """One row per JSON-RPC method the MCP endpoint dispatches.

    The methods are read out of `mcp_server`'s dispatch source rather than
    listed here, so a method added to the server appears in the matrix without
    this script being touched. Their handlers are then analysed exactly like a
    REST handler.
    """
    import ast

    source_path = REPO_ROOT / "src" / "aida" / "mcp_server.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    dispatch: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "method" or len(node.comparators) != 1:
            continue
        comparator = node.comparators[0]
        if not isinstance(comparator, ast.Constant) or not isinstance(comparator.value, str):
            continue
        dispatch.setdefault(comparator.value, "")

    # Pair each method with the handler assigned in its branch, when there is one.
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name):
            continue
        if test.left.id != "method" or not isinstance(test.comparators[0], ast.Constant):
            continue
        method = test.comparators[0].value
        if not isinstance(method, str):
            continue
        for called in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(called, ast.Call) and isinstance(called.func, ast.Name):
                if called.func.id.startswith("_handle_"):
                    dispatch[method] = called.func.id
                    break

    rows: list[SurfaceRow] = []
    for method in sorted(dispatch):
        handler = dispatch[method]
        if not handler:
            rows.append(
                SurfaceRow(
                    surface=f"`MCP {method}`",
                    family="MCP",
                    handler=UNKNOWN,
                    roles=UNKNOWN,
                    tenant_check=UNKNOWN,
                    workspace_check=UNKNOWN,
                    side_effects=UNKNOWN,
                    writes_audit=UNKNOWN,
                    cancellation=UNKNOWN,
                )
            )
            continue
        module = "aida.mcp_server"
        writes = reaches_session_write(module, handler)
        rows.append(
            SurfaceRow(
                surface=f"`MCP {method}`",
                family="MCP",
                handler=f"{module}.{handler}",
                # The MCP endpoint's own route carries the identity dependency;
                # per-method eligibility is decided inside the handler from the
                # caller's roles, so there is no declared role tuple to read.
                roles="per-tool role eligibility (see `_tool_role_eligible`)",
                tenant_check=_yes_no(reaches_reference(module, handler, TENANT_REFERENCES)),
                workspace_check=_yes_no(reaches_call(module, handler, WORKSPACE_CALLS)),
                side_effects="writes" if writes else "read",
                writes_audit=_yes_no(reaches_call(module, handler, AUDIT_CALLS)),
                cancellation=(
                    "cooperative"
                    if reaches_call(module, handler, CANCELLATION_CALLS)
                    else "not cancellable"
                ),
            )
        )
    return rows


def _sdk_rows(rest_rows: list[SurfaceRow]) -> list[SurfaceRow]:
    """The public Tool SDK's network surface.

    The SDK is a thin client, not a second server: `ToolDraftClient.submit_draft`
    POSTs to one REST route. Its controls are therefore *that route's* controls,
    resolved by matching the path the SDK builds against the REST rows above --
    so this row can never claim a different posture from the endpoint it calls.
    """
    from uuid import UUID

    from aida_tool_sdk.serialization import draft_submission_url

    probe = UUID("00000000-0000-0000-0000-000000000000")
    url = draft_submission_url("", probe)
    template = url.replace(str(probe), "{project_id}")
    for row in rest_rows:
        if template and template in row.surface and "POST" in row.surface:
            return [
                SurfaceRow(
                    surface="`SDK aida_tool_sdk.ToolDraftClient.submit_draft`",
                    family="SDK",
                    handler=row.handler,
                    roles=row.roles,
                    tenant_check=row.tenant_check,
                    workspace_check=row.workspace_check,
                    side_effects=row.side_effects,
                    writes_audit=row.writes_audit,
                    cancellation=row.cancellation,
                )
            ]
    return [
        SurfaceRow(
            surface="`SDK aida_tool_sdk.ToolDraftClient.submit_draft`",
            family="SDK",
            handler=UNKNOWN,
            roles=UNKNOWN,
            tenant_check=UNKNOWN,
            workspace_check=UNKNOWN,
            side_effects=UNKNOWN,
            writes_audit=UNKNOWN,
            cancellation=UNKNOWN,
        )
    ]


def collect_rows() -> list[SurfaceRow]:
    rest = _rest_rows()
    rows = rest + _mcp_rows() + _sdk_rows(rest)
    return sorted(rows, key=lambda row: (row.family, row.surface))


def _table(rows: Iterable[SurfaceRow]) -> list[str]:
    lines = [
        "| Surface | Family | Handler | Required roles | Tenant check | "
        "Workspace check | Side effects | Writes audit | Cancellation |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.surface} | {row.family} | `{row.handler}` | {row.roles} | "
            f"{row.tenant_check} | {row.workspace_check} | {row.side_effects} | "
            f"{row.writes_audit} | {row.cancellation} |"
        )
    return lines


def render(rows: list[SurfaceRow]) -> str:
    unknown_rows = [row for row in rows if row.unknown_cells]
    unknown_cells = sum(len(row.unknown_cells) for row in rows)
    families: dict[str, int] = {}
    for row in rows:
        families[row.family] = families.get(row.family, 0) + 1

    lines = [
        "# Surface-to-control matrix",
        "",
        "**Generated file. Do not edit by hand.**",
        "Regenerate with `python scripts/generate_surface_control_matrix.py`;",
        "`--check` fails when this file is out of date with the application.",
        "",
        "Review 2026-09-05, section 6 item 1 asks for every REST / MCP / export /",
        "bulk / job / SDK surface to be mapped to its tenant and workspace checks,",
        "required roles, side effects, audit behaviour and cancellation behaviour --",
        "and for that coverage to be *auditable*. Every cell below is derived from",
        "the live FastAPI application and from a static walk of the handler call",
        "graph. Nothing here is hand-maintained.",
        "",
        "## What a cell means",
        "",
        "- **Required roles** -- the role tuple in the `require_roles` dependency",
        "  FastAPI actually wired into the route. `none declared` means the route",
        "  carries no such dependency and is gated some other way (identity only,",
        "  or a role check inside the handler body so the denial can be audited",
        "  before the 403).",
        "- **Tenant check** -- the handler's call graph reaches an",
        "  `organization_id` boundary reference.",
        "- **Workspace check** -- the handler's call graph reaches the workspace",
        "  authorization gate (`authorization_gate.gate` / `gate_read` /",
        "  `resolve_workspace` / `policy_engine.authorize_enforced`).",
        "- **Side effects** -- `writes` when the call graph reaches a session",
        "  write; `mutating verb, no write found` flags a POST/PUT/PATCH/DELETE",
        "  whose write the walker could not see, which is a finding, not a pass.",
        "- **Writes audit** -- the handler *can reach* `record_audit`. It does not",
        "  prove every path through the handler audits; the INV-7 suite's",
        "  per-route persistence tests are what prove that.",
        "- **Cancellation** -- `cooperative` when the call graph reaches a",
        "  pause/cancel checkpoint; `not cancellable` otherwise. Most",
        "  request-path surfaces are short-lived and legitimately not cancellable.",
        "",
        "## What this analysis cannot see",
        "",
        "- A control enforced by data rather than by a call (a row-level filter, a",
        "  policy row) is invisible to a call-graph walk.",
        "- A control reached through dynamic dispatch (a registry, `getattr`, a",
        "  handler looked up by string) is not followed.",
        "- `unknown` is emitted wherever the analysis could not determine a cell.",
        "  Those rows are listed in full below rather than dropped.",
        "",
        "## Coverage",
        "",
        f"- Surfaces covered: **{len(rows)}**",
        "- By family: "
        + ", ".join(f"{family} {count}" for family, count in sorted(families.items())),
        f"- Rows with at least one `unknown` cell: **{len(unknown_rows)}**",
        f"- `unknown` cells in total: **{unknown_cells}**",
        "",
    ]

    if unknown_rows:
        lines += [
            "### Gap list -- surfaces the analysis could not fully determine",
            "",
            "| Surface | Undetermined cells |",
            "|---|---|",
        ]
        for row in unknown_rows:
            lines.append(f"| {row.surface} | {', '.join(row.unknown_cells)} |")
        lines.append("")
    else:
        lines += [
            "### Gap list",
            "",
            "No `unknown` cells: every surface's controls were determinable from",
            "the application and its call graph.",
            "",
        ]

    lines += ["## Matrix", ""]
    lines += _table(rows)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the committed file differs from what would be generated.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args()

    rows = collect_rows()
    content = render(rows)
    unknown_cells = sum(len(row.unknown_cells) for row in rows)

    if args.stdout:
        print(content)
        return 0
    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist; run without --check to create it.")
            return 1
        if args.output.read_text(encoding="utf-8") != content:
            print(f"{args.output} is out of date with the application; regenerate it.")
            return 1
        print(f"{args.output} is up to date ({len(rows)} surfaces).")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(
        f"wrote {args.output} -- {len(rows)} surfaces, {unknown_cells} unknown cells",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
