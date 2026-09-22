"""A mutating route that writes must commit, or its write is rolled back with the request.

`get_session` closes each request's session, and closing rolls back what was not committed. Five
Studio routes and the deep procedure-lineage parse flushed, audited and returned without a
commit, so a 201 or a 200 left nothing behind (R11-AUD08; `tests/test_studio_writes_persist.py`
and `tests/test_lineage_parse_persists.py` are the behaviour tests). No route test saw it: they
share one session across every call, where a flush is still visible.

This is the cheap sweep that would have found both. It reads every `POST`/`PUT`/`PATCH`/`DELETE`
handler under `src` and lists the ones that write (`add`, `add_all`, `flush`, `delete`,
`execute`, `record_audit`) without calling `commit` themselves.

It is a heuristic and says so: a handler that hands the whole write to a helper that commits is
not seen writing, and a handler that reads through `session.execute` is flagged. The second is what
`REVIEWED` is for: each entry names a handler someone read and why it needs no commit of its own.
A new handler that trips the scan is either a real defect or one more reviewed entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
MUTATING = {"post", "put", "patch", "delete"}
WRITE_CALLS = {"add", "add_all", "flush", "delete", "execute"}

#: Handlers the scan flags that need no commit of their own, read on 2026-09-21.
REVIEWED = {
    "aida/api.py::execute_query": "`QueryExecutionGateway.execute` commits its own execution rows",
    "aida/graphql_api.py::graphql_query": "reads only; its budgets are counted outside the session",
    "aida/tool_plans_api.py::recommend_tool_plan": (
        "a read of published tools; the entitlement refusal commits its own audit row"
    ),
}


def _is_mutating_route(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any(
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr in MUTATING
        for dec in fn.decorator_list
    )


def writes_without_commit(source: str) -> list[str]:
    """Names of the mutating route handlers in `source` that write and never commit."""
    flagged = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if not _is_mutating_route(node):
            continue
        attrs: set[str] = set()
        names: set[str] = set()
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                if isinstance(call.func, ast.Attribute):
                    attrs.add(call.func.attr)
                elif isinstance(call.func, ast.Name):
                    names.add(call.func.id)
        writes = bool(attrs & WRITE_CALLS) or "record_audit" in names
        commits = "commit" in attrs or any(n.lstrip("_").startswith("commit") for n in names)
        if writes and not commits:
            flagged.append(node.name)
    return flagged


def _flagged_in_src() -> dict[str, str]:
    flagged = {}
    for path in sorted(SRC.rglob("*.py")):
        for name in writes_without_commit(path.read_text(encoding="utf-8")):
            flagged[f"{path.relative_to(SRC).as_posix()}::{name}"] = name
    return flagged


def test_every_mutating_route_that_writes_commits_or_has_been_reviewed() -> None:
    unreviewed = sorted(set(_flagged_in_src()) - set(REVIEWED))
    assert not unreviewed, (
        "these mutating routes write and never commit, so the request's session rolls the write "
        f"back when it closes: {unreviewed}. Commit before returning, or, if the handler is read "
        "or hands the write to a helper that commits, add it to REVIEWED with the reason."
    )


def test_a_reviewed_entry_is_still_a_handler_the_scan_flags() -> None:
    stale = sorted(set(REVIEWED) - set(_flagged_in_src()))
    assert not stale, f"remove these from REVIEWED, the scan no longer flags them: {stale}"


_WRITES_AND_RETURNS = """
@router.post("/things")
async def create_thing(session = Depends(get_session)):
    session.add(Thing())
    await session.flush()
    record_audit(session)
    return {}
"""


def test_the_scan_flags_a_write_that_never_commits() -> None:
    assert writes_without_commit(_WRITES_AND_RETURNS) == ["create_thing"]


def test_the_scan_passes_the_same_handler_once_it_commits() -> None:
    fixed = _WRITES_AND_RETURNS.replace(
        "    return {}", "    await session.commit()\n    return {}"
    )
    assert writes_without_commit(fixed) == []


def test_the_scan_ignores_a_read_route() -> None:
    assert writes_without_commit(_WRITES_AND_RETURNS.replace("router.post", "router.get")) == []
