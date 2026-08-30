"""Doc-claim regression gate (tracker TS-12).

`Docs/` cites concrete, named artefacts — test files and test functions, source
modules, and import-linter contract names — as evidence for claims about what the
system does today. A prior manual pass (see `Docs/60-delivery/06-accomplishment-log.md`,
"documentation truth pass") found and fixed several such citations that had gone stale
(a renamed/removed test, a moved module). Nothing stops that class of defect from
silently coming back as the code and docs keep evolving — this module is the automated
gate that catches it.

Scope — what this checks (mechanically resolvable, named-artefact citations only):

1. Test citations: a backtick-quoted `` `tests/xxx.py` `` path, optionally qualified
   with `` ::test_name ``, plus a bare backtick-quoted `` `test_name` `` mention.
   The path must exist; a qualified or bare function name must exist as a
   `def`/`async def` somewhere in the file (or anywhere under `tests/` for a bare
   mention), verified with `ast` — no pytest collection needed.
2. Module/path citations: a backtick-quoted `` `src/aida/...` `` path (file or
   directory), or a bare `` `module.py` `` filename, or a dotted `` `aida.x.y` ``
   module path. A bare filename resolves against `src/aida/<name>` directly if that
   exists, else against exactly one recursive match under `src/aida/`.
3. Import-linter contract names: a backtick-quoted, hyphenated lowercase slug
   (`` `module-privacy` ``, `` `gateway-exclusivity` `` ...) mentioned on a line that
   also says "contract" or "import-linter", checked against the `name = "..."`
   values inside `pyproject.toml`'s `[[tool.importlinter.contracts]]` blocks.

Deliberately NOT covered — free-text claims with no named artefact to resolve
mechanically (e.g. "the pipeline enforces X", "every mutation is audited"), fenced
code blocks (illustrative config/snippets, not citations), and generic per-module
template filenames from the *target* modular-monolith layout
(`10-architecture/04-module-decomposition.md`, `40-engineering/02-repository-layout.md`)
that name a *pattern* every future module will have, not one file that exists once —
see EXEMPT_BARE_FILENAMES below for the closed, documented list.

Two forms of citation are intentionally not asserted false even though they do not
currently resolve to a real artefact, because the surrounding doc is not making a
present-tense claim:

- A citation immediately preceded by an imperative build verb ("Add `x`", "Create
  `x`") is a backlog/blueprint action item, not a claim that `x` exists now.
- Import-linter contract names are checked against pyproject.toml only once
  pyproject.toml defines at least one contract. Today it defines zero — the whole
  import-linter mechanism is tracker item ST-02/ST-09, still TODO, and every doc that
  mentions a contract name says so explicitly (each opens by describing the *current*
  code as a flat, undivided package). Flagging every one of those forward-looking
  mentions as "broken" would not be a regression (nothing that used to work stopped
  working) and would fight the structural-foundation work already tracked elsewhere.
  The moment a real contract is added, this gate starts checking citations against it
  for real, and a rename or removal of a real contract is caught from then on.
- A citation on a line that itself says the artefact is planned / not (yet) written
  (the "Implementation status" callout convention already used across Docs/, e.g.
  `10-architecture/01-principles-and-invariants.md`'s
  `**Test — Planned, not written (DATE).**` labels) is not re-asserted false — the
  doc is already telling the truth about it.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "Docs"
SRC_ROOT = REPO_ROOT / "src" / "aida"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Directories under Docs/ that are deliberately retired/archival and are not held to
# a "still true today" standard.
EXCLUDED_DOC_DIR_NAMES = {"_superseded"}

# Bare filenames that name a *pattern* in the target modular-monolith layout
# (every future module gets its own `service.py`/`repository.py`/... — see
# `10-architecture/04-module-decomposition.md` #2 and `30-contracts/03-internal-module-contracts.md`
# #2) rather than one file that exists once today. `src/` is still a flat package
# (tracker ST-01..ST-10, TODO), so these have no current single resolution by design,
# and never will have a *unique* one even once built. `snowflake.py` is a documented
# future connector (tracker CN-2, TODO; `Docs/competitors/06-codebase-architecture-reference.md`
# #5 lists it under "Files to Create / Modify").
EXEMPT_BARE_FILENAMES = {
    "service.py",
    "repository.py",
    "router.py",
    "contracts.py",
    "projector.py",
    "snowflake.py",
}

# `src/...` path prefixes that name the pre-rename target package (`atlas`) used in
# illustrative target-architecture examples (e.g. `40-engineering/03-coding-standards.md`
# #2's example `pyproject.toml` snippet, `40-engineering/06-refactor-plan.md`'s phase
# table). The real package is `src/aida`; `src/atlas` is not, and is not meant to be,
# resolvable today.
EXEMPT_SRC_PATH_PREFIXES = ("src/atlas",)

# A bare `test_xxx` mention that is a real, non-test artefact, verified by hand:
# `Connector.test_connection` is an interface method every connector implements
# (see `src/aida/connectors/base.py`), not a pytest test function.
EXCLUDED_BARE_TEST_NAMES = {"test_connection"}

# Citations immediately preceded by one of these words are backlog/blueprint action
# items ("Add `x.py`", "Create `src/atlas/`") describing what to build, not a claim
# that the artefact exists now.
IMPERATIVE_BUILD_VERBS = {
    "Add",
    "Adds",
    "Create",
    "Creates",
    "Build",
    "Builds",
    "Implement",
    "Implements",
    "Introduce",
    "Introduces",
    "Extract",
    "Extracts",
    "Split",
    "Splits",
    "Write",
    "Writes",
    "Define",
    "Defines",
    "Register",
    "Registers",
    "Move",
    "Moves",
}

_PRECEDING_WORD_RE = re.compile(r"([A-Za-z]+)[\s*`\"'.:]*$")
_PLANNED_MARKER_RE = re.compile(
    r"\bplanned\b|\bnot(?: yet)? written\b|\bnot yet built\b"
    r"|\bdoes not exist\b|\bto be (?:written|built|added)\b",
    re.IGNORECASE,
)
_ACCEPTANCE_LABEL_RE = re.compile(r"^\*\*(Acceptance|Exit)\*\*\s*$")
_BULLET_RE = re.compile(r"^\s*-\s")


def _acceptance_block_lines(text: str) -> set[int]:
    """Line numbers (1-based) inside a `**Acceptance**` / `**Exit**` bullet block —
    e.g. `60-delivery/02-epic-backlog.md`'s per-epic acceptance criteria. These name
    the test that will make a still-`TODO` epic done; they are not a claim that the
    test exists today, the same way a tracker `Exit` cell is not."""
    lines = text.split("\n")
    exempt: set[int] = set()
    in_block = False
    for i, line in enumerate(lines, start=1):
        if _ACCEPTANCE_LABEL_RE.match(line.strip()):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            continue
        if _BULLET_RE.match(line):
            exempt.add(i)
        else:
            in_block = False
    return exempt


def _iter_doc_files():
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        if EXCLUDED_DOC_DIR_NAMES & set(path.relative_to(DOCS_ROOT).parts[:-1]):
            continue
        yield path


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _preceded_by_imperative_verb(text: str, match_start: int) -> bool:
    preceding = text[max(0, match_start - 40) : match_start]
    m = _PRECEDING_WORD_RE.search(preceding)
    return bool(m and m.group(1) in IMPERATIVE_BUILD_VERBS)


def _line_has_planned_marker(text: str, offset: int) -> bool:
    """True if the line containing `offset` says the cited artefact is planned /
    not yet written (the "Implementation status" callout convention used across
    Docs/ — e.g. `10-architecture/01-principles-and-invariants.md`'s
    `**Test — Planned, not written (DATE).**` labels). Such a citation is not a
    present-tense claim that the artefact exists, so it is not drift to flag."""
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    return bool(_PLANNED_MARKER_RE.search(text[line_start:line_end]))


def _doc_rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


@dataclass(frozen=True)
class Citation:
    doc: str
    line: int
    text: str
    extra: str = ""

    @property
    def id(self) -> str:
        suffix = f" {self.extra}" if self.extra else ""
        return f"{self.doc}:{self.line}: `{self.text}`{suffix}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

TEST_PATH_RE = re.compile(r"`(tests/[A-Za-z0-9_./-]+\.py)(?:::([A-Za-z0-9_]+))?`")
BARE_TEST_NAME_RE = re.compile(r"`(test_[A-Za-z0-9_]+)`")
SRC_PATH_RE = re.compile(r"`(src/[A-Za-z0-9_./-]+)`")
BARE_PY_RE = re.compile(r"`([A-Za-z0-9_]+\.py)`")
AIDA_DOTTED_RE = re.compile(r"`(aida(?:\.[A-Za-z0-9_]+)+)`")
LINK_PATH_RE = re.compile(r"\(file:///[^)]*?(src/aida/[A-Za-z0-9_./-]+\.py)\)")
CONTRACT_SLUG_RE = re.compile(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks so illustrative config/snippets are never
    read as citations, while preserving line numbers for everything else."""

    def _blank(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")

    return FENCE_RE.sub(_blank, text)


def collect_test_path_citations() -> list[tuple[Citation, str, str | None]]:
    out = []
    for doc in _iter_doc_files():
        text = _strip_fences(doc.read_text(encoding="utf-8"))
        for m in TEST_PATH_RE.finditer(text):
            if _line_has_planned_marker(text, m.start()):
                continue
            path_str, func = m.group(1), m.group(2)
            cite_text = path_str + (f"::{func}" if func else "")
            citation = Citation(_doc_rel(doc), _line_number(text, m.start()), cite_text)
            out.append((citation, path_str, func))
    return out


def collect_bare_test_name_citations() -> list[Citation]:
    out = []
    for doc in _iter_doc_files():
        text = _strip_fences(doc.read_text(encoding="utf-8"))
        acceptance_lines = _acceptance_block_lines(text)
        for m in BARE_TEST_NAME_RE.finditer(text):
            name = m.group(1)
            if name in EXCLUDED_BARE_TEST_NAMES:
                continue
            line = _line_number(text, m.start())
            if line in acceptance_lines or _line_has_planned_marker(text, m.start()):
                continue
            out.append(Citation(_doc_rel(doc), line, name))
    return out


def collect_src_path_citations() -> list[Citation]:
    out = []
    for doc in _iter_doc_files():
        text = _strip_fences(doc.read_text(encoding="utf-8"))
        for m in SRC_PATH_RE.finditer(text):
            path_str = m.group(1)
            if path_str.startswith(EXEMPT_SRC_PATH_PREFIXES):
                continue
            if _preceded_by_imperative_verb(text, m.start()):
                continue
            if _line_has_planned_marker(text, m.start()):
                continue
            out.append(Citation(_doc_rel(doc), _line_number(text, m.start()), path_str))
        for m in LINK_PATH_RE.finditer(text):
            path_str = m.group(1)
            out.append(
                Citation(
                    _doc_rel(doc),
                    _line_number(text, m.start()),
                    path_str,
                    extra="(markdown link)",
                )
            )
    return out


def collect_bare_py_citations() -> list[Citation]:
    out = []
    for doc in _iter_doc_files():
        text = _strip_fences(doc.read_text(encoding="utf-8"))
        for m in BARE_PY_RE.finditer(text):
            name = m.group(1)
            if name in EXEMPT_BARE_FILENAMES:
                continue
            if _preceded_by_imperative_verb(text, m.start()):
                continue
            if _line_has_planned_marker(text, m.start()):
                continue
            if text[m.end() : m.end() + 7] == "](file:":
                # Markdown link text like [`discovery.py`](file:///.../discovery.py) —
                # the link target disambiguates it; checked separately via LINK_PATH_RE.
                continue
            out.append(Citation(_doc_rel(doc), _line_number(text, m.start()), name))
    return out


def collect_aida_dotted_citations() -> list[Citation]:
    out = []
    for doc in _iter_doc_files():
        text = _strip_fences(doc.read_text(encoding="utf-8"))
        for m in AIDA_DOTTED_RE.finditer(text):
            out.append(Citation(_doc_rel(doc), _line_number(text, m.start()), m.group(1)))
    return out


def collect_contract_name_citations() -> list[Citation]:
    out = []
    for doc in _iter_doc_files():
        text = _strip_fences(doc.read_text(encoding="utf-8"))
        for lineno, line in enumerate(text.split("\n"), start=1):
            if not re.search(r"contract|import-linter", line, re.IGNORECASE):
                continue
            for m in CONTRACT_SLUG_RE.finditer(line):
                out.append(Citation(_doc_rel(doc), lineno, m.group(1)))
    return out


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _functions_in_file(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _all_test_functions() -> set[str]:
    names: set[str] = set()
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        names |= _functions_in_file(path)
    return names


def _import_linter_contract_names() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    contracts = data.get("tool", {}).get("importlinter", {}).get("contracts", [])
    return {c["name"] for c in contracts if isinstance(c, dict) and "name" in c}


# ---------------------------------------------------------------------------
# Collected data (module import time)
# ---------------------------------------------------------------------------

TEST_PATH_CITATIONS = collect_test_path_citations()
BARE_TEST_NAME_CITATIONS = collect_bare_test_name_citations()
SRC_PATH_CITATIONS = collect_src_path_citations()
BARE_PY_CITATIONS = collect_bare_py_citations()
AIDA_DOTTED_CITATIONS = collect_aida_dotted_citations()
CONTRACT_NAME_CITATIONS = collect_contract_name_citations()
ALL_TEST_FUNCTION_NAMES = _all_test_functions()
IMPORT_LINTER_CONTRACT_NAMES = _import_linter_contract_names()


def _ids(citations):
    return [c.id for c in citations]


# ---------------------------------------------------------------------------
# Sanity checks on the scanner itself, so a silent zero-citation collapse
# (e.g. a regex typo, or Docs/ being pointed at the wrong root) cannot make
# this whole gate pass by finding nothing.
# ---------------------------------------------------------------------------


def test_scanner_found_the_docs_tree():
    doc_files = list(_iter_doc_files())
    assert len(doc_files) > 50, (
        f"Only found {len(doc_files)} Docs/*.md files (excluding "
        f"{EXCLUDED_DOC_DIR_NAMES}) — the doc-claim scanner may be pointed at the "
        "wrong root."
    )


def test_scanner_found_test_path_and_src_path_citations():
    # Docs/ reliably still cites at least one real `tests/....py` path and one real
    # `src/aida/...` path today; a regex regression turning either into a no-op would
    # zero these out silently. (Bare `test_xxx` and bare `module.py` citations are
    # NOT asserted non-empty here: a healthy doc-truth pass can legitimately fix every
    # currently-failing one of those down to zero, exactly as this change just did for
    # bare `test_xxx` — see the synthetic extraction tests below for the real regex
    # regression guard.)
    assert TEST_PATH_CITATIONS, "expected at least one `tests/....py` citation in Docs/"
    assert SRC_PATH_CITATIONS, "expected at least one `src/aida/...` citation in Docs/"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("See `tests/test_foo.py` for details.", [("tests/test_foo.py", None)]),
        ("`tests/test_foo.py::test_bar` covers this.", [("tests/test_foo.py", "test_bar")]),
    ],
)
def test_extraction_test_path_regex(text, expected):
    got = [(m.group(1), m.group(2)) for m in TEST_PATH_RE.finditer(text)]
    assert got == expected


def test_extraction_bare_test_name_regex():
    text = "`test_something_specific` proves it."
    assert [m.group(1) for m in BARE_TEST_NAME_RE.finditer(text)] == ["test_something_specific"]


def test_extraction_bare_py_regex():
    assert [m.group(1) for m in BARE_PY_RE.finditer("Owned by `made_up_module.py`.")] == [
        "made_up_module.py"
    ]


def test_extraction_src_path_regex():
    assert [m.group(1) for m in SRC_PATH_RE.finditer("Lives in `src/aida/made_up/module.py`.")] == [
        "src/aida/made_up/module.py"
    ]


def test_extraction_aida_dotted_regex():
    assert [m.group(1) for m in AIDA_DOTTED_RE.finditer("Import `aida.made_up.module` there.")] == [
        "aida.made_up.module"
    ]


def test_extraction_contract_slug_requires_contract_keyword():
    with_keyword = "The `made-up-contract` contract passes."
    without_keyword = "See `made-up-slug` for details."
    assert re.search(r"contract|import-linter", with_keyword, re.IGNORECASE)
    assert not re.search(r"contract|import-linter", without_keyword, re.IGNORECASE)
    assert [m.group(1) for m in CONTRACT_SLUG_RE.finditer(with_keyword)] == ["made-up-contract"]


def test_planned_marker_suppresses_a_synthetic_failing_citation():
    # Same shape as the real fixes in `10-architecture/01-principles-and-invariants.md`:
    # a doc that correctly labels a cited test as not-yet-written must not be flagged.
    text = "**Test — Planned, not written (2026-08-30).** `test_totally_made_up_name`: ..."
    m = next(BARE_TEST_NAME_RE.finditer(text))
    assert _line_has_planned_marker(text, m.start())


def test_acceptance_block_lines_matches_epic_backlog_shape():
    text = "\n".join(
        [
            "**Acceptance**",
            "- `test_totally_made_up_name` proves it.",
            "",
            "### Next section",
            "- `test_should_not_be_exempt` is not in a block.",
        ]
    )
    exempt = _acceptance_block_lines(text)
    assert 2 in exempt
    assert 5 not in exempt


# ---------------------------------------------------------------------------
# 1. Test file / test function citations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "citation,path_str,func",
    TEST_PATH_CITATIONS,
    ids=_ids([c for c, _, _ in TEST_PATH_CITATIONS]),
)
def test_cited_test_path_resolves(citation: Citation, path_str: str, func: str | None):
    full_path = REPO_ROOT / path_str
    assert full_path.is_file(), (
        f"{citation.doc}:{citation.line} cites `{citation.text}`, but "
        f"{path_str} does not exist in the repository."
    )
    if func:
        found = _functions_in_file(full_path)
        assert func in found, (
            f"{citation.doc}:{citation.line} cites `{citation.text}`, but {path_str} "
            f"defines no function named `{func}` (found: {sorted(found) or 'none'})."
        )


@pytest.mark.parametrize("citation", BARE_TEST_NAME_CITATIONS, ids=_ids(BARE_TEST_NAME_CITATIONS))
def test_cited_bare_test_name_resolves(citation: Citation):
    assert citation.text in ALL_TEST_FUNCTION_NAMES, (
        f"{citation.doc}:{citation.line} cites `{citation.text}` as a test, but no "
        f"function named `{citation.text}` exists anywhere under tests/."
    )


# ---------------------------------------------------------------------------
# 2. Module / file path citations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("citation", SRC_PATH_CITATIONS, ids=_ids(SRC_PATH_CITATIONS))
def test_cited_src_path_resolves(citation: Citation):
    full_path = REPO_ROOT / citation.text
    suffix = f" {citation.extra}" if citation.extra else ""
    assert full_path.is_file() or full_path.is_dir(), (
        f"{citation.doc}:{citation.line} cites `{citation.text}`{suffix}, "
        f"but no such file or directory exists in the repository."
    )


@pytest.mark.parametrize("citation", BARE_PY_CITATIONS, ids=_ids(BARE_PY_CITATIONS))
def test_cited_bare_filename_resolves(citation: Citation):
    name = citation.text
    direct = SRC_ROOT / name
    if direct.is_file():
        return
    matches = sorted(SRC_ROOT.rglob(name))
    assert len(matches) == 1, (
        f"{citation.doc}:{citation.line} cites `{name}`, which should resolve to exactly "
        f"one file under src/aida/, but {len(matches)} matched "
        f"({[str(p.relative_to(REPO_ROOT)) for p in matches]})."
    )


@pytest.mark.parametrize("citation", AIDA_DOTTED_CITATIONS, ids=_ids(AIDA_DOTTED_CITATIONS))
def test_cited_aida_dotted_module_resolves(citation: Citation):
    parts = citation.text.split(".")[1:]  # drop leading "aida"
    module_file = SRC_ROOT.joinpath(*parts).with_suffix(".py")
    package_init = SRC_ROOT.joinpath(*parts, "__init__.py")
    assert module_file.is_file() or package_init.is_file(), (
        f"{citation.doc}:{citation.line} cites `{citation.text}`, but neither "
        f"{module_file.relative_to(REPO_ROOT)} nor {package_init.relative_to(REPO_ROOT)} exists."
    )


# ---------------------------------------------------------------------------
# 3. Import-linter contract name citations
# ---------------------------------------------------------------------------

if IMPORT_LINTER_CONTRACT_NAMES:
    _contract_params = CONTRACT_NAME_CITATIONS
else:
    _contract_params = []


@pytest.mark.parametrize("citation", _contract_params, ids=_ids(_contract_params))
def test_cited_import_linter_contract_name_resolves(citation: Citation):
    assert citation.text in IMPORT_LINTER_CONTRACT_NAMES, (
        f"{citation.doc}:{citation.line} cites `{citation.text}` as an import-linter "
        f"contract, but pyproject.toml's [[tool.importlinter.contracts]] blocks define "
        f"no contract with that name (defined: {sorted(IMPORT_LINTER_CONTRACT_NAMES)})."
    )


def test_import_linter_contract_check_status():
    """Documents, rather than hides, why contract-name citations are not being
    checked one-by-one right now: pyproject.toml defines zero import-linter
    contracts (tracker ST-02/ST-09, still TODO), so there is nothing for a
    citation to resolve against yet. The moment a real contract is added this
    test's sibling (`test_cited_import_linter_contract_name_resolves`) starts
    running for real, over every citation found today.
    """
    if IMPORT_LINTER_CONTRACT_NAMES:
        pytest.skip(
            f"{len(IMPORT_LINTER_CONTRACT_NAMES)} contract(s) configured — "
            "per-citation checks are active above."
        )
    print(
        f"pyproject.toml defines zero import-linter contracts; "
        f"{len(CONTRACT_NAME_CITATIONS)} contract-name citation(s) in Docs/ are "
        "target-architecture language and are not yet checked (see module docstring).",
        file=sys.stderr,
    )
