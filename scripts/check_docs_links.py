"""Validate every relative link in the repository's Markdown documentation.

D06 (`Docs/review-2026-09-05/REVIEW.md`) found `README.md` pointing at a
`Docs/60-delivery/04-status-matrix.md` and a `Docs/competitors/...` planning
document that had both been moved or never existed. A dead cross-reference in a
"read this first" table is worse than no reference: it tells a reader the answer
is written down somewhere when it is not.

This is a link *existence* checker, deliberately narrow:

* Only relative links are resolved. `http(s)://` and `mailto:` targets are
  reported as external and never fetched -- CI must not depend on the network,
  and a doc gate that flakes on someone else's outage gets disabled.
* A link to a file resolves against the linking file's own directory.
* A `#fragment` on a Markdown target is checked against that file's headings
  (GitHub's slug rules), because a stale anchor is the same class of defect.
  A fragment on a non-Markdown target is not checked.
* Reference-style definitions (`[label]: target`) are checked as well as
  inline `[text](target)` links.

Standard library only, so it runs in CI without adding a dependency.

Usage::

    python scripts/check_docs_links.py            # whole repository
    python scripts/check_docs_links.py README.md  # named files only

Exit code 1 if any relative link cannot be resolved.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that hold vendored, generated or third-party Markdown. Their links
# are not ours to fix and a failure there would be noise. Every dot-directory is
# skipped as well (`.venv*`, `.uv-cache*`, `.git`, `.claude`, ...) except the
# allow-listed ones below -- this repository keeps several parallel virtualenvs,
# and third-party package READMEs would otherwise dominate the report.
SKIPPED_DIRECTORIES = frozenset(
    {
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "artifacts",
        "scratch",
        "_to_delete",
    }
)
ALLOWED_DOT_DIRECTORIES = frozenset({".github"})

# `[text](target)` -- the target stops at whitespace so `(path "title")` works.
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# `[label]: target` at the start of a line.
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?", re.MULTILINE)
# Fenced code blocks: links inside them are illustrative, not navigational.
FENCED_BLOCK = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)


def _strip_code_blocks(text: str) -> str:
    """Blank out fenced code, preserving line count so numbers stay honest."""

    def blank(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    return FENCED_BLOCK.sub(blank, text)


def _slugify(heading: str) -> str:
    """GitHub's heading-anchor rules, as far as this repository needs them."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links -> their text
    text = text.replace("`", "").replace("*", "").replace("_", "")
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def _anchors(markdown: str) -> set[str]:
    body = _strip_code_blocks(markdown)
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for match in ATX_HEADING.finditer(body):
        slug = _slugify(match.group(2))
        if not slug:
            continue
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    # Explicit HTML anchors, e.g. `<a id="foo">`.
    anchors.update(re.findall(r"<a\s+(?:id|name)=\"([^\"]+)\"", body))
    return anchors


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _markdown_files(targets: list[Path]) -> list[Path]:
    if targets:
        return [t.resolve() for t in targets]
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*.md"):
        parts = path.relative_to(REPO_ROOT).parts
        if SKIPPED_DIRECTORIES.intersection(parts):
            continue
        if any(part.startswith(".") and part not in ALLOWED_DOT_DIRECTORIES for part in parts[:-1]):
            continue
        found.append(path)
    return sorted(found)


def check_file(path: Path) -> list[str]:
    """Return one message per unresolvable relative link in `path`."""
    text = path.read_text(encoding="utf-8", errors="replace")
    body = _strip_code_blocks(text)
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    problems: list[str] = []

    matches = [(m.start(1), m.group(1)) for m in INLINE_LINK.finditer(body)]
    matches += [(m.start(1), m.group(1)) for m in REFERENCE_LINK.finditer(body)]

    for offset, raw in sorted(matches):
        target = raw.strip()
        if not target:
            continue
        lowered = target.lower()
        if lowered.startswith(("http://", "https://", "mailto:", "tel:", "data:", "#")):
            # Same-file anchors are checked against this file's own headings.
            if target.startswith("#") and target[1:] and target[1:] not in _anchors(text):
                problems.append(
                    f"{relative_path}:{_line_of(body, offset)}: "
                    f"anchor not found in this file: {target}"
                )
            continue
        if target.startswith("<") or "://" in target:
            continue

        file_part, _, fragment = target.partition("#")
        if not file_part:
            continue
        resolved = (path.parent / file_part).resolve()
        line = _line_of(body, offset)

        if not resolved.exists():
            problems.append(f"{relative_path}:{line}: missing target: {target}")
            continue
        if fragment and resolved.is_file() and resolved.suffix.lower() == ".md":
            other = resolved.read_text(encoding="utf-8", errors="replace")
            if fragment not in _anchors(other):
                problems.append(
                    f"{relative_path}:{line}: anchor not found in "
                    f"{resolved.relative_to(REPO_ROOT).as_posix()}: #{fragment}"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Markdown files to check. Defaults to every tracked .md file.",
    )
    args = parser.parse_args(argv)

    files = _markdown_files(args.paths)
    problems: list[str] = []
    for path in files:
        problems.extend(check_file(path))

    if problems:
        print(f"Broken relative links ({len(problems)}) in {len(files)} Markdown files:\n")
        for problem in problems:
            print(f"  {problem}")
        print("\nFix the target or remove the link. External URLs are never fetched.")
        return 1

    print(f"OK: every relative link in {len(files)} Markdown files resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
