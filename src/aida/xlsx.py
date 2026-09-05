"""Minimal, dependency-free .xlsx writer.

`pyproject.toml` pins no spreadsheet library, and `asset_evidence_api`'s own
export docstring states the constraint this follows: do not add a dependency
when an existing, dependency-free format is honest. An .xlsx is a zip of XML
parts, and the subset needed to emit a tabular workbook that Excel, Excel for
the web, LibreOffice and Sheets all open cleanly is small enough to own
outright -- which is what this module is. It writes; it does not read. (The
re-import half of the round trip will parse the same shape back, and this repo
already pins `defusedxml` for exactly that kind of untrusted-XML parsing.)

Deliberate simplifications, each one a thing a general-purpose library would
do differently and none of which this caller needs:

* **Inline strings, not a shared-string table.** `t="inlineStr"` stores text
  in the cell. A shared-string table saves space when values repeat heavily; a
  catalog export is mostly unique identifiers and prose, where it would save
  little and cost a second pass over every row.
* **Two cell formats.** Bold for the header row, default for everything else.
  No number formats, colours, or widths beyond a fixed per-column hint.
* **Deterministic bytes.** Every zip entry gets a fixed timestamp and the parts
  are written in a fixed order, so the same rows always produce the same bytes
  -- which is what makes the `X-Artifact-SHA256` header on the export endpoint
  mean anything.

Streaming is out of scope: the whole workbook is built in memory. The export
endpoint that calls this caps its own row count for that reason.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

#: Fixed zip entry timestamp (1980-01-01, the DOS epoch zipfile floors to), so
#: two exports of identical content hash identically.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: Excel's own hard limits. A caller exceeding either would produce a file
#: Excel refuses to open, so this module raises instead of writing one.
MAX_ROWS_PER_SHEET = 1_048_576
MAX_COLUMNS_PER_SHEET = 16_384

#: Longest string Excel will hold in a single cell. Longer values are truncated
#: with an explicit marker rather than silently cut, so a reader can tell the
#: cell is not the whole value.
MAX_CELL_CHARS = 32_767
_TRUNCATION_MARKER = "...[truncated]"

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

CellValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class Sheet:
    """One worksheet: a name, a header row, and body rows.

    `name` is sanitized by `_sanitize_sheet_name` -- Excel rejects several
    punctuation characters and anything over 31 characters in a sheet name, and
    silently producing an unopenable file would be worse than renaming. Every
    row is padded or truncated to `len(headers)` so the sheet stays rectangular
    even if a caller's row builder drifts.
    """

    name: str
    headers: list[str]
    rows: list[list[CellValue]]


def column_letter(index: int) -> str:
    """1-based column index to its spreadsheet letter (1 -> A, 27 -> AA)."""
    if index < 1:
        raise ValueError("column index is 1-based")
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _escape(text: str) -> str:
    """XML-escape `text` and drop characters XML 1.0 cannot represent.

    Control characters below 0x20 other than tab/LF/CR are not legal in XML and
    make Excel report the file as corrupt. Source-system comments do
    occasionally carry them, so they are stripped here rather than trusted.
    """
    cleaned = "".join(
        char for char in text if char in "\t\n\r" or (ord(char) >= 0x20 and ord(char) != 0x7F)
    )
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sanitize_sheet_name(name: str) -> str:
    for illegal in "[]:*?/\\":
        name = name.replace(illegal, "_")
    name = name.strip("'").strip()
    return (name or "Sheet")[:31]


def _cell_xml(reference: str, value: CellValue, *, style: int) -> str:
    style_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{reference}"{style_attr}/>'
    if isinstance(value, bool):
        # Checked before int: bool is an int subclass, and writing True as the
        # number 1 would lose the distinction on the way back in.
        return f'<c r="{reference}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    return (
        f'<c r="{reference}"{style_attr} t="inlineStr">'
        f'<is><t xml:space="preserve">{_escape(text)}</t></is></c>'
    )


def _row_xml(row_number: int, values: list[CellValue], *, style: int) -> str:
    cells = "".join(
        _cell_xml(f"{column_letter(index)}{row_number}", value, style=style)
        for index, value in enumerate(values, start=1)
    )
    return f'<row r="{row_number}">{cells}</row>'


def _sheet_xml(sheet: Sheet) -> str:
    width = len(sheet.headers)
    if width == 0:
        raise ValueError(f"sheet {sheet.name!r} has no headers")
    if width > MAX_COLUMNS_PER_SHEET:
        raise ValueError(f"sheet {sheet.name!r} exceeds Excel's column limit")
    if len(sheet.rows) + 1 > MAX_ROWS_PER_SHEET:
        raise ValueError(f"sheet {sheet.name!r} exceeds Excel's row limit")

    last_column = column_letter(width)
    last_row = len(sheet.rows) + 1
    body = [_row_xml(1, list(sheet.headers), style=1)]
    for offset, row in enumerate(sheet.rows, start=2):
        padded: list[CellValue] = list(row[:width])
        padded.extend([None] * (width - len(padded)))
        body.append(_row_xml(offset, padded, style=0))
    # A generous fixed width beats no width at all (Excel's default clips
    # description prose to unreadability) and beats measuring text, which
    # cannot be done correctly without font metrics.
    cols = "".join(
        f'<col min="{i}" max="{i}" width="28" customWidth="1"/>' for i in range(1, width + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS_MAIN}">'
        f'<dimension ref="A1:{last_column}{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols}</cols>"
        f"<sheetData>{''.join(body)}</sheetData>"
        f'<autoFilter ref="A1:{last_column}{last_row}"/>'
        "</worksheet>"
    )


def _content_types_xml(sheet_count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}"
        "</Types>"
    )


def _workbook_xml(sheets: list[Sheet]) -> str:
    entries = "".join(
        f'<sheet name="{_escape(_sanitize_sheet_name(sheet.name))}" sheetId="{i}" r:id="rId{i}"/>'
        for i, sheet in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
        f"<sheets>{entries}</sheets>"
        "</workbook>"
    )


def _workbook_rels_xml(sheet_count: int) -> str:
    sheet_rels = "".join(
        f'<Relationship Id="rId{i}" Type="{_NS_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, sheet_count + 1)
    )
    styles_rel = (
        f'<Relationship Id="rId{sheet_count + 1}" Type="{_NS_REL}/styles" Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PACKAGE_REL}">'
        f"{sheet_rels}{styles_rel}"
        "</Relationships>"
    )


_ROOT_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_NS_PACKAGE_REL}">'
    f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

# Two fills are not optional: Excel expects index 0 (none) and index 1
# (gray125) to exist even when nothing references them.
_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<styleSheet xmlns="{_NS_MAIN}">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="2">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    "</fills>"
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    "</cellXfs>"
    "</styleSheet>"
)


def write_workbook(sheets: list[Sheet]) -> bytes:
    """Render `sheets` as .xlsx bytes.

    Deterministic: identical input always produces identical output, so the
    caller's content hash is stable across processes and machines.
    """
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")

    buffer = io.BytesIO()
    parts: list[tuple[str, str]] = [
        ("[Content_Types].xml", _content_types_xml(len(sheets))),
        ("_rels/.rels", _ROOT_RELS_XML),
        ("xl/workbook.xml", _workbook_xml(sheets)),
        ("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(sheets))),
        ("xl/styles.xml", _STYLES_XML),
    ]
    parts.extend(
        (f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheet))
        for i, sheet in enumerate(sheets, start=1)
    )
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()
