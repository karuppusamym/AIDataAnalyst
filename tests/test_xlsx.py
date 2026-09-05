"""`aida.xlsx` -- the dependency-free workbook writer.

The writer hand-builds OOXML, so the thing worth asserting is not "it returned
bytes" but that those bytes are a structurally valid package: every part a
reader dereferences is present, every relationship resolves, and every part
parses as XML. A malformed workbook fails at the point a steward tries to open
it, which is far too late and produces a bug report that says only "Excel says
the file is corrupt".
"""

from __future__ import annotations

import io
import zipfile

import pytest
from defusedxml import ElementTree

from aida.xlsx import (
    MAX_CELL_CHARS,
    Sheet,
    column_letter,
    write_workbook,
)

_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _archive(sheets: list[Sheet]) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(write_workbook(sheets)))


def _sheet_text(archive: zipfile.ZipFile, index: int = 1) -> str:
    return archive.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")


def _cell_values(archive: zipfile.ZipFile, index: int = 1) -> list[list[str | None]]:
    """Every row's cell text, in order, with empty cells as None."""
    root = ElementTree.fromstring(_sheet_text(archive, index))
    rows = []
    for row in root.findall(".//main:sheetData/main:row", _NS):
        values: list[str | None] = []
        for cell in row.findall("main:c", _NS):
            inline = cell.find("main:is/main:t", _NS)
            numeric = cell.find("main:v", _NS)
            if inline is not None:
                values.append(inline.text)
            elif numeric is not None:
                values.append(numeric.text)
            else:
                values.append(None)
        rows.append(values)
    return rows


def test_column_letter_crosses_the_26_boundary() -> None:
    assert column_letter(1) == "A"
    assert column_letter(26) == "Z"
    assert column_letter(27) == "AA"
    assert column_letter(52) == "AZ"
    assert column_letter(703) == "AAA"


def test_column_letter_rejects_zero_and_negatives() -> None:
    for index in (0, -1):
        with pytest.raises(ValueError):
            column_letter(index)


def test_package_contains_every_part_a_reader_dereferences() -> None:
    archive = _archive(
        [
            Sheet(name="One", headers=["a"], rows=[["x"]]),
            Sheet(name="Two", headers=["b"], rows=[["y"]]),
        ]
    )
    assert set(archive.namelist()) == {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
    }


def test_every_part_parses_as_xml() -> None:
    archive = _archive([Sheet(name="One", headers=["a"], rows=[["x"]])])
    for name in archive.namelist():
        # Raises ParseError on malformed XML, which is exactly the failure a
        # hand-built package is prone to.
        ElementTree.fromstring(archive.read(name).decode("utf-8"))


def test_every_worksheet_relationship_resolves_to_a_part() -> None:
    """A workbook naming `rId3` with no matching relationship, or a
    relationship pointing at a missing part, opens as corrupt -- and both are
    easy to produce when sheet count and relationship ids are generated
    separately, as they are here.
    """
    sheets = [Sheet(name=f"S{i}", headers=["a"], rows=[["x"]]) for i in range(1, 4)]
    archive = _archive(sheets)
    rels_root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels").decode("utf-8"))
    targets = {element.attrib["Id"]: element.attrib["Target"] for element in rels_root}
    workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml").decode("utf-8"))
    referenced = [
        element.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        for element in workbook_root.findall(".//main:sheets/main:sheet", _NS)
    ]
    assert len(referenced) == 3
    names = set(archive.namelist())
    for relationship_id in referenced:
        assert relationship_id in targets
        assert f"xl/{targets[relationship_id]}" in names
    # The styles relationship the content types declare must resolve too.
    assert "xl/styles.xml" in names


def test_header_row_and_body_rows_land_in_order() -> None:
    archive = _archive(
        [Sheet(name="One", headers=["first", "second"], rows=[["a", "b"], ["c", "d"]])]
    )
    assert _cell_values(archive) == [["first", "second"], ["a", "b"], ["c", "d"]]


def test_markup_in_a_value_is_escaped_not_injected() -> None:
    """A source-system comment containing `<` must not close the cell element."""
    archive = _archive(
        [Sheet(name="One", headers=["h"], rows=[['<is><t>injected</t></is> & "q"']])]
    )
    assert _cell_values(archive) == [["h"], ['<is><t>injected</t></is> & "q"']]


def test_control_characters_are_stripped_rather_than_written() -> None:
    """XML 1.0 cannot represent most control characters; writing one raw makes
    the whole workbook unopenable, so a NUL in a source comment must not be
    able to take the export down.
    """
    archive = _archive([Sheet(name="One", headers=["h"], rows=[["before\x00\x07after\tkept"]])])
    assert _cell_values(archive) == [["h"], ["beforeafter\tkept"]]


def test_numbers_and_booleans_keep_their_types() -> None:
    archive = _archive([Sheet(name="One", headers=["h"], rows=[[7], [1.5], [True], [False]])])
    root = ElementTree.fromstring(_sheet_text(archive))
    body = root.findall(".//main:sheetData/main:row", _NS)[1:]
    types = [row.find("main:c", _NS).attrib.get("t") for row in body]
    # `False` is written as a boolean, not silently collapsed into an empty
    # cell -- bool is an int subclass, so the ordering of the isinstance
    # checks is what makes this true.
    assert types == [None, None, "b", "b"]
    assert _cell_values(archive)[1:] == [["7"], ["1.5"], ["1"], ["0"]]


def test_none_and_empty_string_produce_an_empty_cell() -> None:
    archive = _archive([Sheet(name="One", headers=["h"], rows=[[None], [""]])])
    assert _cell_values(archive)[1:] == [[None], [None]]


def test_short_rows_are_padded_to_the_header_width() -> None:
    """A rectangular sheet is what `dimension` and `autoFilter` already claim;
    a ragged row would contradict both.
    """
    archive = _archive([Sheet(name="One", headers=["a", "b", "c"], rows=[["x"]])])
    assert _cell_values(archive)[1] == ["x", None, None]


def test_long_rows_are_truncated_to_the_header_width() -> None:
    archive = _archive([Sheet(name="One", headers=["a"], rows=[["x", "dropped"]])])
    assert _cell_values(archive)[1] == ["x"]


def test_an_over_long_value_is_truncated_with_a_visible_marker() -> None:
    archive = _archive([Sheet(name="One", headers=["h"], rows=[["z" * (MAX_CELL_CHARS + 500)]])])
    value = _cell_values(archive)[1][0]
    assert value is not None
    assert len(value) == MAX_CELL_CHARS
    assert value.endswith("...[truncated]")


def test_sheet_names_are_sanitized_to_what_excel_accepts() -> None:
    archive = _archive([Sheet(name="A/B:C[D]" + "x" * 40, headers=["h"], rows=[])])
    root = ElementTree.fromstring(archive.read("xl/workbook.xml").decode("utf-8"))
    name = root.findall(".//main:sheets/main:sheet", _NS)[0].attrib["name"]
    assert len(name) <= 31
    assert not set(name) & set("[]:*?/\\")


def test_output_is_deterministic_for_identical_input() -> None:
    """The export endpoint publishes an `X-Artifact-SHA256` over these bytes;
    a timestamp leaking into the zip would make that hash change on every
    download and mean nothing.
    """
    sheets = [Sheet(name="One", headers=["a", "b"], rows=[["x", 1], [None, True]])]
    assert write_workbook(sheets) == write_workbook(sheets)


def test_an_empty_workbook_is_rejected() -> None:
    with pytest.raises(ValueError):
        write_workbook([])


def test_a_sheet_without_headers_is_rejected() -> None:
    with pytest.raises(ValueError):
        write_workbook([Sheet(name="One", headers=[], rows=[["x"]])])


def test_a_sheet_with_no_body_rows_is_still_valid() -> None:
    archive = _archive([Sheet(name="Empty", headers=["a", "b"], rows=[])])
    assert _cell_values(archive) == [["a", "b"]]
