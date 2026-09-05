"""Minimal, dependency-free .xlsx reader -- the parsing half of the round trip.

`aida.xlsx` writes the workbook this reads back. The two are deliberately not
symmetric, because the file that comes back is not the file that went out:
Excel rewrites a workbook wholesale on save. In particular it moves every
string into a shared-string table (`xl/sharedStrings.xml`) rather than the
inline strings the writer emits, so a reader that only understood its own
output would fail on every file a steward had actually opened and saved. That
asymmetry is the single most important thing this module handles.

Also handled because real saved workbooks contain them:

* **Sparse rows.** Excel omits empty cells entirely, so `A1, C1` with no `B1`
  is normal. Cells are placed by their `r` reference, never by position.
* **String runs.** A shared-string entry that carries formatting is split into
  several `<r><t>` runs; the value is their concatenation.
* **Formula cells.** `t="str"` holds a cached formula result in `<v>`. A
  steward who types `=TRIM(B2)` gets a value this reads, not a blank.
* **Error cells.** `t="e"` (`#REF!`, `#N/A`) reads as `None` rather than the
  literal error text, which is not a description anyone meant to write.

Untrusted input, so: parsed with `defusedxml` (already pinned) rather than
`xml.etree`, and bounded before anything is decompressed -- see
`MAX_UNCOMPRESSED_BYTES`, which is what stops a small upload from expanding
into an out-of-memory kill.

Not handled, because nothing in this round trip needs it: dates (every date
this workbook carries is written as an ISO string, not an Excel serial),
styles, merged cells, and the streaming `.xlsb` binary format.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from typing import IO, Any

from defusedxml import ElementTree

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL_DOC = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: Ceiling on the total uncompressed size of the parts this reader touches.
#: Checked against the zip directory *before* decompressing anything, so a
#: "zip bomb" (a few KB that expands to gigabytes) is refused rather than
#: absorbed. 128MB is far above any real catalog workbook -- the export caps
#: itself at 50k rows per sheet, which lands well under 20MB uncompressed.
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024

#: Ceiling on rows read from any one sheet, mirroring
#: `model_export.EXPORT_MAX_ROWS_PER_SHEET`. A sheet longer than this is
#: truncated and reported, never silently cut.
MAX_ROWS_PER_SHEET = 50_000

#: Ceiling on the number of sheets. A real workbook here has four.
MAX_SHEETS = 64


class WorkbookParseError(Exception):
    """The upload is not a workbook this reader can make sense of.

    Raised for structural problems (not a zip, no workbook part, a sheet
    relationship that does not resolve), never for a workbook whose *content*
    is wrong -- that is the diff's job to report per row, with a reason a
    steward can act on.
    """


@dataclass(frozen=True, slots=True)
class ParsedSheet:
    """One worksheet, read back as a header row plus dict-per-row.

    Rows are dicts keyed by header text rather than positional lists, so a
    steward who inserts or reorders a column does not silently shift every
    value into the wrong field -- the single most likely way a hand-edited
    workbook goes wrong.
    """

    name: str
    headers: list[str]
    rows: list[dict[str, str]]
    truncated: bool


CellValue = str | None


def column_index(reference: str) -> int:
    """0-based column index from a cell reference (`"A1"` -> 0, `"AB12"` -> 27)."""
    index = 0
    for char in reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    if index == 0:
        raise WorkbookParseError(f"cell reference {reference!r} names no column")
    return index - 1


def _text_of(element: Any) -> str:
    """Concatenated text of a `<si>` or `<is>`, including formatting runs.

    Typed `Any` rather than `Element`: `defusedxml` ships no type stubs, so an
    `Element` annotation here would be a fiction mypy could not check anyway.
    """
    parts: list[str] = []
    for node in element.iter(f"{_NS_MAIN}t"):
        if node.text:
            parts.append(str(node.text))
    return "".join(parts)


def _guard_size(archive: zipfile.ZipFile) -> None:
    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise WorkbookParseError(
            "workbook is too large to process "
            f"({total} bytes uncompressed, limit {MAX_UNCOMPRESSED_BYTES})"
        )


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """The shared-string table, or an empty list when the file has none.

    Excel always writes one; `aida.xlsx` never does. Both are valid.
    """
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml").decode("utf-8"))
    return [_text_of(si) for si in root.findall(f"{_NS_MAIN}si")]


def _sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """`(sheet name, part path)` in workbook order.

    Resolved through the workbook's relationships rather than by guessing
    `sheet1.xml`, `sheet2.xml`...: Excel does not renumber parts when sheets
    are reordered or deleted, so position and part number diverge in any
    workbook that has been edited.
    """
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names:
        raise WorkbookParseError("not an .xlsx workbook: xl/workbook.xml is missing")

    targets: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels_root = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        )
        for relationship in rels_root:
            target = relationship.attrib.get("Target", "")
            # Targets are usually relative to xl/ but may be absolute.
            path = target[1:] if target.startswith("/") else f"xl/{target}"
            targets[relationship.attrib.get("Id", "")] = path.replace("xl/./", "xl/")

    workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml").decode("utf-8"))
    parts: list[tuple[str, str]] = []
    for sheet in workbook_root.iter(f"{_NS_MAIN}sheet"):
        relationship_id = sheet.attrib.get(f"{_NS_REL_DOC}id")
        name = sheet.attrib.get("name", "")
        part = targets.get(relationship_id or "")
        if part is None or part not in names:
            raise WorkbookParseError(f"sheet {name!r} points at a part that is not in the file")
        parts.append((name, part))
        if len(parts) > MAX_SHEETS:
            raise WorkbookParseError(f"workbook has more than {MAX_SHEETS} sheets")
    if not parts:
        raise WorkbookParseError("workbook declares no sheets")
    return parts


def _cell_value(cell: Any, shared: list[str]) -> CellValue:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{_NS_MAIN}is")
        return _text_of(inline) if inline is not None else None
    if cell_type == "e":
        # `#REF!` / `#N/A`: a broken formula, not content anyone authored.
        return None
    value = cell.find(f"{_NS_MAIN}v")
    if value is None or value.text is None:
        return None
    if cell_type == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError) as exc:
            raise WorkbookParseError(
                "a cell references a shared string that is not in the table"
            ) from exc
    if cell_type == "b":
        return "TRUE" if value.text.strip() == "1" else "FALSE"
    # "str" (cached formula result), "n"/absent (number), and anything else
    # readable come back as their literal text -- every field this round trip
    # reads back is a string, so no numeric coercion is wanted here.
    return str(value.text)


def _parse_sheet(archive: zipfile.ZipFile, name: str, part: str, shared: list[str]) -> ParsedSheet:
    root = ElementTree.fromstring(archive.read(part).decode("utf-8"))
    raw_rows: list[list[CellValue]] = []
    truncated = False
    for row in root.iter(f"{_NS_MAIN}row"):
        if len(raw_rows) >= MAX_ROWS_PER_SHEET + 1:
            truncated = True
            break
        cells: list[CellValue] = []
        for cell in row.findall(f"{_NS_MAIN}c"):
            reference = cell.attrib.get("r")
            index = column_index(reference) if reference else len(cells)
            # Excel omits empty cells, so pad to the cell's real position
            # rather than trusting document order to be dense.
            while len(cells) < index:
                cells.append(None)
            cells.append(_cell_value(cell, shared))
        raw_rows.append(cells)

    if not raw_rows:
        return ParsedSheet(name=name, headers=[], rows=[], truncated=False)

    headers = [(value or "").strip() for value in raw_rows[0]]
    while headers and headers[-1] == "":
        headers.pop()

    body: list[dict[str, str]] = []
    for cells in raw_rows[1 : MAX_ROWS_PER_SHEET + 1]:
        if all(value is None or value == "" for value in cells):
            # A blank row is Excel's leftovers, not a row a steward deleted
            # content from -- skipping it avoids reporting hundreds of
            # "unmatched row" errors for the empty tail of an edited sheet.
            continue
        row_values: dict[str, str] = {}
        for position, header in enumerate(headers):
            if not header:
                continue
            value = cells[position] if position < len(cells) else None
            row_values[header] = (value or "").strip()
        body.append(row_values)
    if len(raw_rows) > MAX_ROWS_PER_SHEET + 1:
        truncated = True

    return ParsedSheet(name=name, headers=headers, rows=body, truncated=truncated)


def read_workbook(source: IO[bytes] | bytes) -> dict[str, ParsedSheet]:
    """Parse a workbook into `{sheet name: ParsedSheet}`.

    Raises `WorkbookParseError` for anything structurally wrong with the file.
    Content problems are not this function's business -- it returns whatever
    the sheets say, and the caller decides what is valid.
    """
    try:
        archive = zipfile.ZipFile(source if not isinstance(source, bytes) else _as_stream(source))
    except zipfile.BadZipFile as exc:
        raise WorkbookParseError(
            "the upload is not a readable .xlsx file (an .xls or .csv saved with an "
            ".xlsx extension will fail here -- re-save as .xlsx)"
        ) from exc

    with archive:
        _guard_size(archive)
        shared = _shared_strings(archive)
        return {
            name: _parse_sheet(archive, name, part, shared) for name, part in _sheet_parts(archive)
        }


def _as_stream(data: bytes) -> IO[bytes]:
    import io

    return io.BytesIO(data)
