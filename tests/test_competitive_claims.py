"""Competitive claims in the product docs carry a source, a scope and an expiry (AR-12).

The 2026-09-09 architecture review (AR-12) found unqualified competitor-absence
claims -- "nobody", "every competitor", "none of the incumbents" -- stated as
fact in the product documents, with no date and in one case no source. It
corrected `00-product/08` and the review's own comparison. The rest of
`00-product/` was not swept, and nothing stopped a new claim from appearing the
same way. This is that discipline, enforced.

A product doc that makes an absolute competitive claim declares, in its header,
a machine-readable claims line:

    > Claims: assessed YYYY-MM-DD against <scope>; re-verify by YYYY-MM-DD; sources: `<path>`

or, for a document kept as history:

    > Claims: historical as of YYYY-MM-DD; <why it is not current>; sources: <where>

An assessed line names at least one source file that exists under `Docs/`, and
its re-verify date must not have passed. The expiry is the point: a claim about
a moving market is true for a while, and the build says when that while is over
instead of letting the claim harden into fact. To clear an expired line,
re-check the claims against current sources and move both dates, or mark the
document historical.

What counts as a claim is deliberately narrow: a phrase that is comparative on
its own ("every competitor", "none of the incumbents", "nobody else"), or a bare
absolute ("nobody", "no one", "first to") on a line that also names a
competitor, vendor, segment or market. "A catalog nobody fills in" is not a
competitive claim and is not flagged.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "Docs"
PRODUCT_DOCS = sorted((DOCS_ROOT / "00-product").glob("*.md"))

#: How far into a document the claims line may sit: it belongs in the header.
HEADER_LINES = 20

_COMPARATIVE = re.compile(
    r"\b(?:every (?:competitor|incumbent|vendor)s?"
    r"|none of the (?:incumbents|competitors|vendors)"
    r"|nobody else|no other (?:vendor|product|platform|competitor)|industry[- ]first)\b",
    re.IGNORECASE,
)
_ABSOLUTE = re.compile(r"\b(?:nobody|no one|first to)\b", re.IGNORECASE)
_COMPETITIVE_CONTEXT = re.compile(
    r"\b(?:competitors?|incumbents?|vendors?|segment|market|atlan|collibra|alation|purview"
    r"|databricks|snowflake|unity catalog|monte carlo|anomalo|datahub|openmetadata"
    r"|informatica)\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_ASSESSED = re.compile(
    r"^> Claims: assessed (?P<assessed>\d{4}-\d{2}-\d{2}) against (?P<scope>[^;]+); "
    r"re-verify by (?P<expires>\d{4}-\d{2}-\d{2}); sources: (?P<sources>.+)$"
)
_HISTORICAL = re.compile(r"^> Claims: historical as of (?P<as_of>\d{4}-\d{2}-\d{2}); .+$")
_BACKTICKED = re.compile(r"`([^`]+)`")


def is_competitive_claim(line: str) -> bool:
    if _COMPARATIVE.search(line):
        return True
    return bool(_ABSOLUTE.search(line) and _COMPETITIVE_CONTEXT.search(line))


def competitive_claims(text: str) -> list[tuple[int, str]]:
    """Every claim line, with its line number. Fenced blocks are not prose."""
    blanked = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return [
        (number, line.strip())
        for number, line in enumerate(blanked.split("\n"), start=1)
        if not line.lstrip().startswith("> Claims:") and is_competitive_claim(line)
    ]


def claims_line(text: str) -> str | None:
    for line in text.split("\n")[:HEADER_LINES]:
        if line.startswith("> Claims:"):
            return line.strip()
    return None


def _source_exists(reference: str) -> bool:
    candidate = reference.strip()
    return (DOCS_ROOT / candidate).is_file() or (DOCS_ROOT / "00-product" / candidate).is_file()


def claims_problem(text: str, doc: str, today: date) -> str | None:
    """Why this document's competitive claims are not disciplined, or None."""
    found = competitive_claims(text)
    if not found:
        return None
    first_line, first_text = found[0]
    line = claims_line(text)
    if line is None:
        return (
            f"{doc}:{first_line} makes a competitive claim ({first_text[:90]!r}) and the "
            f"document has no '> Claims:' line in its first {HEADER_LINES} lines. See "
            "tests/test_competitive_claims.py for the format."
        )
    if _HISTORICAL.match(line):
        return None
    match = _ASSESSED.match(line)
    if match is None:
        return f"{doc}: its claims line is malformed: {line!r}"
    assessed = date.fromisoformat(match["assessed"])
    expires = date.fromisoformat(match["expires"])
    if expires < assessed:
        return f"{doc}: its claims expire ({expires}) before they were assessed ({assessed})"
    sources = _BACKTICKED.findall(match["sources"])
    if not any(_source_exists(source) for source in sources):
        return f"{doc}: its claims line names no source file that exists under Docs/: {sources}"
    if today > expires:
        return (
            f"{doc}: its competitive claims were assessed {assessed} and were due for "
            f"re-verification by {expires}. Re-check them against current sources and move "
            "both dates, or mark the document historical."
        )
    return None


DOCS_WITH_CLAIMS = [
    path for path in PRODUCT_DOCS if competitive_claims(path.read_text(encoding="utf-8"))
]


def test_the_sweep_finds_the_claims_it_is_about() -> None:
    """If the detector stopped matching, every check below would pass on nothing."""
    assert len(DOCS_WITH_CLAIMS) >= 4, [path.name for path in DOCS_WITH_CLAIMS]


@pytest.mark.parametrize("path", DOCS_WITH_CLAIMS, ids=[path.name for path in DOCS_WITH_CLAIMS])
def test_competitive_claims_carry_a_source_a_scope_and_an_expiry(path: Path) -> None:
    doc = str(path.relative_to(REPO_ROOT))
    problem = claims_problem(path.read_text(encoding="utf-8"), doc, date.today())
    assert problem is None, problem


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Every competitor has made the same architectural choice.", True),
        ("Three markets are colliding and none of the incumbents owns the intersection.", True),
        ("## 9. The seven capabilities nobody else has", True),
        ("Nobody in this segment connects quality evidence into the runtime decision.", True),
        ("**Why nobody has it.** Monte Carlo and Anomalo detect superbly.", True),
        ("The blank-catalog problem (a catalog nobody fills in).", False),
        ("Atlas is a read-only platform.", False),
    ],
)
def test_what_counts_as_a_competitive_claim(line: str, expected: bool) -> None:
    assert is_competitive_claim(line) is expected


_CLAIM = "Every competitor ships an MCP server."
_SOURCE = "`90-reference/03-sources.md`"


def _doc(claims: str | None) -> str:
    header = ["# A product doc", "", "> Status: Authoritative."]
    if claims is not None:
        header.append(claims)
    return "\n".join([*header, "", _CLAIM])


def test_a_claim_with_no_claims_line_is_refused() -> None:
    problem = claims_problem(_doc(None), "doc.md", date(2026, 9, 11))
    assert problem is not None and "no '> Claims:' line" in problem


def test_an_expired_assessment_is_refused() -> None:
    line = (
        f"> Claims: assessed 2026-01-01 against vendor pages; re-verify by 2026-04-01; "
        f"sources: {_SOURCE}"
    )
    problem = claims_problem(_doc(line), "doc.md", date(2026, 9, 11))
    assert problem is not None and "due for re-verification by 2026-04-01" in problem


def test_an_unexpired_assessment_with_a_real_source_passes() -> None:
    line = (
        f"> Claims: assessed 2026-08-28 against vendor pages; re-verify by 2026-11-28; "
        f"sources: {_SOURCE}"
    )
    assert claims_problem(_doc(line), "doc.md", date(2026, 9, 11)) is None


def test_a_source_that_does_not_exist_is_refused() -> None:
    line = (
        "> Claims: assessed 2026-08-28 against vendor pages; re-verify by 2026-11-28; "
        "sources: `90-reference/no-such-file.md`"
    )
    problem = claims_problem(_doc(line), "doc.md", date(2026, 9, 11))
    assert problem is not None and "no source file" in problem


def test_a_historical_document_needs_no_expiry() -> None:
    line = "> Claims: historical as of 2026-09-09; kept as history; sources: Appendix A."
    assert claims_problem(_doc(line), "doc.md", date(2030, 1, 1)) is None


def test_a_malformed_line_is_refused() -> None:
    problem = claims_problem(_doc("> Claims: trust us"), "doc.md", date(2026, 9, 11))
    assert problem is not None and "malformed" in problem
