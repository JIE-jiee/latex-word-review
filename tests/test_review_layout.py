"""Synthetic, redistributable regression tests for deterministic review layout."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import cast

import pytest
from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.review_layout import apply_review_layout, validate_review_layout
from tests._docx_factory import write_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W_NS, "wp": WP_NS, "a": A_NS, "m": M_NS}


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _paragraph(text: str, *, bold: bool = False) -> etree._Element:
    paragraph = etree.Element(_q(W_NS, "p"))
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    if bold:
        properties = etree.SubElement(run, _q(W_NS, "rPr"))
        etree.SubElement(properties, _q(W_NS, "b"))
    node = etree.SubElement(run, _q(W_NS, "t"))
    node.text = text
    return paragraph


def _image_cell(label: str) -> etree._Element:
    cell = etree.Element(_q(W_NS, "tc"))
    paragraph = etree.SubElement(cell, _q(W_NS, "p"))
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    drawing = etree.SubElement(run, _q(W_NS, "drawing"))
    inline = etree.SubElement(drawing, _q(WP_NS, "inline"))
    extent = etree.SubElement(inline, _q(WP_NS, "extent"))
    extent.set("cx", "1000000")
    extent.set("cy", "500000")
    graphic = etree.SubElement(inline, _q(A_NS, "graphic"))
    transform = etree.SubElement(graphic, _q(A_NS, "xfrm"))
    drawing_extent = etree.SubElement(transform, _q(A_NS, "ext"))
    drawing_extent.set("cx", "1000000")
    drawing_extent.set("cy", "500000")
    cell.append(_paragraph(label))
    return cell


def _image_paragraph(width: int, height: int) -> etree._Element:
    paragraph = etree.Element(_q(W_NS, "p"))
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    drawing = etree.SubElement(run, _q(W_NS, "drawing"))
    inline = etree.SubElement(drawing, _q(WP_NS, "inline"))
    extent = etree.SubElement(inline, _q(WP_NS, "extent"))
    extent.set("cx", str(width))
    extent.set("cy", str(height))
    graphic = etree.SubElement(inline, _q(A_NS, "graphic"))
    etree.SubElement(graphic, _q(A_NS, "blip"))
    transform = etree.SubElement(graphic, _q(A_NS, "xfrm"))
    drawing_extent = etree.SubElement(transform, _q(A_NS, "ext"))
    drawing_extent.set("cx", str(width))
    drawing_extent.set("cy", str(height))
    return paragraph


def _text_cell(text: str) -> etree._Element:
    cell = etree.Element(_q(W_NS, "tc"))
    cell.append(_paragraph(text))
    return cell


def _table(rows: tuple[tuple[etree._Element, ...], ...]) -> etree._Element:
    table = etree.Element(_q(W_NS, "tbl"))
    properties = etree.SubElement(table, _q(W_NS, "tblPr"))
    width = etree.SubElement(properties, _q(W_NS, "tblW"))
    width.set(_q(W_NS, "type"), "auto")
    width.set(_q(W_NS, "w"), "0")
    grid = etree.SubElement(table, _q(W_NS, "tblGrid"))
    etree.SubElement(grid, _q(W_NS, "gridCol")).set(_q(W_NS, "w"), "0")
    for cells in rows:
        row = etree.SubElement(table, _q(W_NS, "tr"))
        for cell in cells:
            row.append(cell)
    return table


def _synthetic_document() -> bytes:
    root = etree.Element(
        _q(W_NS, "document"),
        nsmap={"w": W_NS, "wp": WP_NS, "a": A_NS, "m": M_NS},
    )
    body = etree.SubElement(root, _q(W_NS, "body"))
    body.append(_paragraph("Body formatting survives.", bold=True))
    body.append(_image_paragraph(10_000_000, 5_000_000))
    body.append(_image_paragraph(1_000_000, 500_000))

    break_paragraph = etree.SubElement(body, _q(W_NS, "p"))
    break_properties = etree.SubElement(break_paragraph, _q(W_NS, "pPr"))
    break_section = etree.SubElement(break_properties, _q(W_NS, "sectPr"))
    section_type = etree.SubElement(break_section, _q(W_NS, "type"))
    section_type.set(_q(W_NS, "val"), "continuous")
    break_columns = etree.SubElement(break_section, _q(W_NS, "cols"))
    break_columns.set(_q(W_NS, "num"), "2")

    figure = etree.SubElement(body, _q(W_NS, "sdt"))
    figure_properties = etree.SubElement(figure, _q(W_NS, "sdtPr"))
    tag = etree.SubElement(figure_properties, _q(W_NS, "tag"))
    tag.set(_q(W_NS, "val"), "tex2word:figure")
    figure_content = etree.SubElement(figure, _q(W_NS, "sdtContent"))
    figure_content.append(
        _table(
            (
                (
                    _image_cell("(a)"),
                    _image_cell("(b)"),
                    _image_cell("(c)"),
                    _image_cell("(d)"),
                    _text_cell("[sub-figure omitted] (e)"),
                ),
            )
        )
    )
    caption = _paragraph("Figure 1. Synthetic composite.")
    caption_properties = etree.Element(_q(W_NS, "pPr"))
    caption_style = etree.SubElement(caption_properties, _q(W_NS, "pStyle"))
    caption_style.set(_q(W_NS, "val"), "Caption")
    caption.insert(0, caption_properties)
    figure_content.append(caption)

    body.append(
        _table(
            (
                (_text_cell("Name"), _text_cell("Long narrative heading")),
                (_text_cell("A"), _text_cell("A body value that remains reviewable")),
            )
        )
    )

    equation = etree.SubElement(body, _q(W_NS, "p"))
    equation_properties = etree.SubElement(equation, _q(W_NS, "pPr"))
    tabs = etree.SubElement(equation_properties, _q(W_NS, "tabs"))
    for value, position in (("center", "1000"), ("right", "2000")):
        tab = etree.SubElement(tabs, _q(W_NS, "tab"))
        tab.set(_q(W_NS, "val"), value)
        tab.set(_q(W_NS, "pos"), position)
    begin_run = etree.SubElement(equation, _q(W_NS, "r"))
    begin = etree.SubElement(begin_run, _q(W_NS, "fldChar"))
    begin.set(_q(W_NS, "fldCharType"), "begin")
    begin.set(_q(W_NS, "dirty"), "false")
    instruction_run = etree.SubElement(equation, _q(W_NS, "r"))
    instruction = etree.SubElement(instruction_run, _q(W_NS, "instrText"))
    instruction.text = " SEQ Equation \\* ARABIC "
    math = etree.SubElement(equation, _q(M_NS, "oMath"))
    etree.SubElement(math, _q(M_NS, "r"))

    final_section = etree.SubElement(body, _q(W_NS, "sectPr"))
    page_size = etree.SubElement(final_section, _q(W_NS, "pgSz"))
    page_size.set(_q(W_NS, "w"), "12240")
    page_size.set(_q(W_NS, "h"), "15840")
    margins = etree.SubElement(final_section, _q(W_NS, "pgMar"))
    margins.set(_q(W_NS, "left"), "1440")
    margins.set(_q(W_NS, "right"), "1440")
    columns = etree.SubElement(final_section, _q(W_NS, "cols"))
    columns.set(_q(W_NS, "num"), "2")
    columns.set(_q(W_NS, "space"), "720")
    return cast(
        bytes,
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True),
    )


def _settings() -> bytes:
    root = etree.Element(_q(W_NS, "settings"), nsmap={"w": W_NS})
    update = etree.SubElement(root, _q(W_NS, "updateFields"))
    update.set(_q(W_NS, "val"), "false")
    return cast(
        bytes,
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True),
    )


def _write_synthetic(path: Path) -> Path:
    return write_docx(
        path,
        document_xml=_synthetic_document(),
        extra_parts={"word/settings.xml": _settings()},
    )


def _package_xml(path: Path, part: str) -> etree._Element:
    with zipfile.ZipFile(path) as package:
        return etree.fromstring(package.read(part))


def test_review_layout_repairs_sections_tables_figures_fields_and_pagination(
    tmp_path: Path,
) -> None:
    source = _write_synthetic(tmp_path / "source.docx")
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()

    report = apply_review_layout(source, destination)
    validate_review_layout(destination)

    assert source.read_bytes() == source_before
    assert report.usable_width_twips == 9_360
    assert report.section_breaks_removed == 1
    assert (report.figure_tables, report.normal_tables) == (1, 1)
    assert report.figure_tables_reflowed == 1
    assert report.placeholders_removed == 1
    assert report.images_scaled == 5
    assert report.equations_retabbed == 1
    assert report.fields_marked_dirty == 1

    root = _package_xml(destination, "word/document.xml")
    sections = root.xpath("//w:sectPr", namespaces=NS)
    assert len(sections) == 1
    assert sections[0].getparent().tag == _q(W_NS, "body")
    assert sections[0].find("w:cols", NS).get(_q(W_NS, "num")) == "1"
    assert not root.xpath("//w:p[w:pPr/w:sectPr]", namespaces=NS)

    body_paragraph = root.xpath("//w:p[w:r/w:t='Body formatting survives.']", namespaces=NS)[0]
    assert body_paragraph.find("w:r/w:rPr/w:b", NS) is not None

    standalone_extents = root.xpath("/w:document/w:body/w:p//wp:extent", namespaces=NS)
    assert [(int(node.get("cx")), int(node.get("cy"))) for node in standalone_extents] == [
        (5_943_600, 2_971_800),
        (1_000_000, 500_000),
    ]
    assert all(
        node.xpath("ancestor::w:p/w:pPr/w:jc[@w:val='center']", namespaces=NS)
        for node in standalone_extents
    )

    figure_table = root.xpath(
        "//w:sdt[w:sdtPr/w:tag[@w:val='tex2word:figure']]//w:tbl",
        namespaces=NS,
    )[0]
    figure_rows = figure_table.findall("w:tr", NS)
    assert [len(row.findall("w:tc", NS)) for row in figure_rows] == [2, 2]
    assert "sub-figure omitted" not in "".join(figure_table.itertext())
    figure_grid = [
        int(column.get(_q(W_NS, "w"))) for column in figure_table.findall("w:tblGrid/w:gridCol", NS)
    ]
    assert figure_grid == [4_680, 4_680]
    extents = figure_table.findall(".//wp:inline/wp:extent", NS)
    assert {(int(node.get("cx")), int(node.get("cy"))) for node in extents} == {
        (2_819_400, 1_409_700)
    }
    assert all(
        paragraph.find("w:pPr/w:keepNext", NS) is not None
        for paragraph in figure_table.findall(".//w:p[w:drawing]", NS)
    )

    ordinary_table = root.xpath("/w:document/w:body/w:tbl", namespaces=NS)[0]
    ordinary_grid = [
        int(column.get(_q(W_NS, "w")))
        for column in ordinary_table.findall("w:tblGrid/w:gridCol", NS)
    ]
    assert sum(ordinary_grid) == 9_360
    first_row = ordinary_table.find("w:tr", NS)
    assert first_row is not None
    assert first_row.find("w:trPr/w:tblHeader", NS) is not None
    assert all(cell.find(".//w:rPr/w:b", NS) is not None for cell in first_row.findall("w:tc", NS))
    assert all(
        row.find("w:trPr/w:cantSplit", NS) is not None for row in ordinary_table.findall("w:tr", NS)
    )
    assert all(
        paragraph.find("w:pPr/w:spacing", NS).get(_q(W_NS, "before")) == "0"
        and paragraph.find("w:pPr/w:spacing", NS).get(_q(W_NS, "after")) == "0"
        for paragraph in ordinary_table.findall(".//w:p", NS)
    )

    equation = root.xpath("//w:p[.//w:instrText[contains(., 'SEQ Equation')]]", namespaces=NS)[0]
    equation_tabs = [
        (tab.get(_q(W_NS, "val")), int(tab.get(_q(W_NS, "pos"))))
        for tab in equation.findall("w:pPr/w:tabs/w:tab", NS)
    ]
    assert equation_tabs == [("center", 4_680), ("right", 9_360)]
    assert equation.find(".//w:fldChar", NS).get(_q(W_NS, "dirty")) == "true"
    caption = root.xpath("//w:p[w:pPr/w:pStyle[@w:val='Caption']]", namespaces=NS)[0]
    assert caption.find("w:pPr/w:keepLines", NS) is not None

    settings = _package_xml(destination, "word/settings.xml")
    assert settings.find("w:updateFields", NS).get(_q(W_NS, "val")) == "true"
    content_types = _package_xml(destination, "[Content_Types].xml")
    assert content_types.find(f"{{{CT_NS}}}Override[@PartName='/word/settings.xml']") is not None
    relationships = _package_xml(destination, "word/_rels/document.xml.rels")
    assert (
        relationships.find(
            f"{{{REL_NS}}}Relationship[@Type="
            "'http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings']"
        )
        is not None
    )


def test_review_layout_is_byte_deterministic_and_idempotent(tmp_path: Path) -> None:
    source = _write_synthetic(tmp_path / "source.docx")
    first = tmp_path / "first.docx"
    second = tmp_path / "second.docx"

    first_report = apply_review_layout(source, first)
    second_report = apply_review_layout(first, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_report.output_sha256 == second_report.output_sha256
    assert second_report.section_breaks_removed == 0
    assert second_report.figure_tables_reflowed == 0
    assert second_report.images_scaled == 0


def test_review_layout_never_overwrites_an_existing_destination(tmp_path: Path) -> None:
    source = _write_synthetic(tmp_path / "source.docx")
    destination = tmp_path / "existing.docx"
    destination.write_bytes(b"keep me")

    with pytest.raises(ContractError) as raised:
        apply_review_layout(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert destination.read_bytes() == b"keep me"
