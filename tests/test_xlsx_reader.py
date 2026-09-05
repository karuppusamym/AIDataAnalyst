"""`aida.xlsx_reader` -- parsing a workbook that has been through Excel.

The round trip's real risk is not reading back what `aida.xlsx` wrote; it is
reading back what Excel wrote after a steward opened that file and saved it.
Excel rewrites the package wholesale: strings move into a shared-string table,
empty cells vanish, sheet parts stop matching sheet order, and formatting
splits a single string into runs. `_excel_style_workbook` below builds a
package with those properties deliberately, because a reader tested only
against its own writer's output would pass every test here and fail on the
first real upload.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from aida.xlsx import Sheet, write_workbook
from aida.xlsx_reader import (
    MAX_UNCOMPRESSED_BYTES,
    WorkbookParseError,
    column_index,
    read_workbook,
)

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _excel_style_workbook(
    *,
    sheet_name: str = "Columns",
    shared: list[str],
    rows_xml: str,
    part_name: str = "sheet7.xml",
    relationship_id: str = "rId9",
) -> bytes:
    """A package shaped the way Excel writes one, not the way `aida.xlsx` does.

    Differences that matter, all of them present here: a shared-string table,
    a sheet part whose number has nothing to do with its position, and a
    relationship id that is not `rId1`.
    """
    si_entries = "".join(f"<si><t>{value}</t></si>" for value in shared)
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{_NS_PKG}">'
            f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
            f"</Relationships>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="{relationship_id}"/></sheets>'
            f"</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{_NS_PKG}">'
            f'<Relationship Id="{relationship_id}" Type="{_NS_REL}/worksheet" '
            f'Target="worksheets/{part_name}"/>'
            f'<Relationship Id="rId20" Type="{_NS_REL}/sharedStrings" '
            f'Target="sharedStrings.xml"/>'
            f"</Relationships>"
        ),
        "xl/sharedStrings.xml": (
            f'<sst xmlns="{_NS_MAIN}" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">{si_entries}</sst>'
        ),
        f"xl/worksheets/{part_name}": (
            f'<worksheet xmlns="{_NS_MAIN}"><sheetData>{rows_xml}</sheetData></worksheet>'
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _shared_cell(reference: str, index: int) -> str:
    return f'<c r="{reference}" t="s"><v>{index}</v></c>'


# ---------------------------------------------------------------------------
# Cell references
# ---------------------------------------------------------------------------


def test_column_index_reads_single_and_multi_letter_references() -> None:
    assert column_index("A1") == 0
    assert column_index("Z9") == 25
    assert column_index("AA1") == 26
    assert column_index("AB12") == 27


def test_column_index_rejects_a_reference_with_no_column() -> None:
    with pytest.raises(WorkbookParseError):
        column_index("12")


# ---------------------------------------------------------------------------
# The Excel-shaped package
# ---------------------------------------------------------------------------


def test_reads_a_shared_string_table() -> None:
    """The single most important case: Excel puts every string here, and the
    writer this reads back never does.
    """
    content = _excel_style_workbook(
        shared=["column_id", "business_description", "c-1", "A described column."],
        rows_xml=(
            f'<row r="1">{_shared_cell("A1", 0)}{_shared_cell("B1", 1)}</row>'
            f'<row r="2">{_shared_cell("A2", 2)}{_shared_cell("B2", 3)}</row>'
        ),
    )
    sheets = read_workbook(content)
    assert sheets["Columns"].headers == ["column_id", "business_description"]
    assert sheets["Columns"].rows == [
        {"column_id": "c-1", "business_description": "A described column."}
    ]


def test_resolves_a_sheet_through_its_relationship_not_its_part_number() -> None:
    """Excel does not renumber parts when sheets are added or deleted, so the
    first sheet is routinely `sheet7.xml`. Guessing `sheet1.xml` would fail on
    any workbook that has been edited.
    """
    content = _excel_style_workbook(
        shared=["h", "v"],
        rows_xml=f'<row r="1">{_shared_cell("A1", 0)}</row>'
        f'<row r="2">{_shared_cell("A2", 1)}</row>',
        part_name="sheet7.xml",
        relationship_id="rId42",
    )
    assert read_workbook(content)["Columns"].rows == [{"h": "v"}]


def test_places_cells_by_reference_so_omitted_empties_do_not_shift_values() -> None:
    """Excel omits empty cells entirely. Reading positionally would move C2's
    value into B2 -- silently writing a description onto the wrong field.
    """
    content = _excel_style_workbook(
        shared=["column_id", "source_description", "business_description", "c-1", "the meaning"],
        rows_xml=(
            f'<row r="1">{_shared_cell("A1", 0)}{_shared_cell("B1", 1)}'
            f"{_shared_cell('C1', 2)}</row>"
            # No B2 at all.
            f'<row r="2">{_shared_cell("A2", 3)}{_shared_cell("C2", 4)}</row>'
        ),
    )
    row = read_workbook(content)["Columns"].rows[0]
    assert row == {
        "column_id": "c-1",
        "source_description": "",
        "business_description": "the meaning",
    }


def test_joins_formatting_runs_into_one_value() -> None:
    """A string a steward part-bolded is stored as several <r><t> runs."""
    content = _excel_style_workbook(
        shared=["h"],
        rows_xml=(
            f'<row r="1">{_shared_cell("A1", 0)}</row>'
            '<row r="2"><c r="A2" t="inlineStr"><is>'
            "<r><t>Customer </t></r><r><t>identifier</t></r>"
            "</is></c></row>"
        ),
    )
    assert read_workbook(content)["Columns"].rows == [{"h": "Customer identifier"}]


def test_reads_a_cached_formula_result() -> None:
    """`=TRIM(B2)` leaves t="str" with the computed value in <v>."""
    content = _excel_style_workbook(
        shared=["h"],
        rows_xml=(
            f'<row r="1">{_shared_cell("A1", 0)}</row>'
            '<row r="2"><c r="A2" t="str"><f>TRIM(B2)</f><v>trimmed text</v></c></row>'
        ),
    )
    assert read_workbook(content)["Columns"].rows == [{"h": "trimmed text"}]


def test_an_error_cell_reads_as_empty_not_as_its_error_text() -> None:
    """`#REF!` is a broken formula, not a description anyone wrote. Reading it
    literally would publish "#REF!" as a column's business description.
    """
    content = _excel_style_workbook(
        shared=["h", "v"],
        rows_xml=(
            f'<row r="1">{_shared_cell("A1", 0)}{_shared_cell("B1", 1)}</row>'
            f'<row r="2">{_shared_cell("A2", 1)}'
            '<c r="B2" t="e"><v>#REF!</v></c></row>'
        ),
    )
    assert read_workbook(content)["Columns"].rows == [{"h": "v", "v": ""}]


def test_blank_trailing_rows_are_dropped() -> None:
    """Excel leaves empty <row> elements behind after a deletion. Kept, they
    would each be reported as an unmatched row.
    """
    content = _excel_style_workbook(
        shared=["h", "v"],
        rows_xml=(
            f'<row r="1">{_shared_cell("A1", 0)}</row>'
            f'<row r="2">{_shared_cell("A2", 1)}</row>'
            '<row r="3"><c r="A3"/></row>'
            '<row r="4"/>'
        ),
    )
    assert read_workbook(content)["Columns"].rows == [{"h": "v"}]


# ---------------------------------------------------------------------------
# Round trip against this repo's own writer
# ---------------------------------------------------------------------------


def test_round_trips_a_workbook_this_repo_wrote() -> None:
    content = write_workbook(
        [
            Sheet(name="README", headers=["Field", "Value"], rows=[["Datasource", "prod"]]),
            Sheet(
                name="Columns",
                headers=["column_id", "business_description", "description_version"],
                rows=[["c-1", "Ampersands & <angle brackets>", 2], ["c-2", None, None]],
            ),
        ]
    )
    sheets = read_workbook(content)
    assert set(sheets) == {"README", "Columns"}
    assert sheets["Columns"].rows == [
        {
            "column_id": "c-1",
            "business_description": "Ampersands & <angle brackets>",
            "description_version": "2",
        },
        {"column_id": "c-2", "business_description": "", "description_version": ""},
    ]


def test_accepts_a_stream_as_well_as_bytes() -> None:
    content = write_workbook([Sheet(name="Columns", headers=["h"], rows=[["v"]])])
    assert read_workbook(io.BytesIO(content))["Columns"].rows == [{"h": "v"}]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_non_zip_upload_is_refused_with_an_actionable_message() -> None:
    with pytest.raises(WorkbookParseError) as exc_info:
        read_workbook(b"schema,table,column,description\npublic,customers,id,pk\n")
    # A CSV renamed to .xlsx is the likeliest wrong upload, so the message
    # names it rather than saying only "bad zip".
    assert ".csv" in str(exc_info.value)


def test_a_zip_without_a_workbook_part_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "not a workbook")
    with pytest.raises(WorkbookParseError, match="xl/workbook.xml"):
        read_workbook(buffer.getvalue())


def test_a_sheet_pointing_at_a_missing_part_is_refused() -> None:
    content = _excel_style_workbook(
        shared=["h"], rows_xml="", part_name="sheet1.xml", relationship_id="rId1"
    )
    # Rebuild with the worksheet part removed.
    source = zipfile.ZipFile(io.BytesIO(content))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in source.namelist():
            if name.startswith("xl/worksheets/"):
                continue
            archive.writestr(name, source.read(name))
    with pytest.raises(WorkbookParseError, match="not in the file"):
        read_workbook(buffer.getvalue())


def test_a_zip_bomb_is_refused_before_it_is_decompressed() -> None:
    """A few KB on the wire that expands past the memory limit. Refused on the
    directory's declared sizes, so nothing large is ever materialized.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"\0" * (MAX_UNCOMPRESSED_BYTES + 1))
    payload = buffer.getvalue()
    assert len(payload) < 1_000_000  # tiny on the wire
    with pytest.raises(WorkbookParseError, match="too large"):
        read_workbook(payload)
