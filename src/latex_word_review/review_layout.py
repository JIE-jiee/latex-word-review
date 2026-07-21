"""Deterministic, single-column Word layout normalization for review exports.

The converter remains responsible for semantic LaTeX-to-DOCX translation.  This
module only repairs review-facing Word geometry after conversion: sections,
tables, composite figures, displayed-equation tabs, pagination hints, and field
refresh flags.  It never opens Word, extracts the package, or touches LaTeX.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import zipfile
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.docx_reader import DocxPackage, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes

W_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS: Final = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS: Final = "http://schemas.openxmlformats.org/drawingml/2006/main"
M_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/math"
CONTENT_TYPES_NS: Final = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"

DOCUMENT_PART: Final = "word/document.xml"
SETTINGS_PART: Final = "word/settings.xml"
CONTENT_TYPES_PART: Final = "[Content_Types].xml"
DOCUMENT_RELS_PART: Final = "word/_rels/document.xml.rels"
SETTINGS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
)
SETTINGS_REL_TYPE: Final = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
)
MAX_DOCX_BYTES: Final = 128 * 1024 * 1024
TWIP_TO_EMU: Final = 635
DEFAULT_USABLE_WIDTH_TWIPS: Final = 9_360
CELL_HORIZONTAL_MARGIN_TWIPS: Final = 120

_NS: Final = {"w": W_NS, "wp": WP_NS, "a": A_NS, "m": M_NS}
_PLACEHOLDER = re.compile(r"^\[sub-figureomitted\](?:\([a-z0-9]+\))?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ReviewLayoutReport:
    """Auditable summary of one successfully normalized review DOCX."""

    source_sha256: str
    output_sha256: str
    usable_width_twips: int
    section_breaks_removed: int
    figure_tables: int
    normal_tables: int
    figure_tables_reflowed: int
    placeholders_removed: int
    images_scaled: int
    equations_retabbed: int
    fields_marked_dirty: int


@dataclass(slots=True)
class _LayoutStats:
    section_breaks_removed: int = 0
    figure_tables: int = 0
    normal_tables: int = 0
    figure_tables_reflowed: int = 0
    placeholders_removed: int = 0
    images_scaled: int = 0
    equations_retabbed: int = 0
    fields_marked_dirty: int = 0


def _w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


def _wp(local: str) -> str:
    return f"{{{WP_NS}}}{local}"


def _a(local: str) -> str:
    return f"{{{A_NS}}}{local}"


def _m(local: str) -> str:
    return f"{{{M_NS}}}{local}"


def _xml_bytes(root: etree._Element) -> bytes:
    return cast(
        bytes,
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True),
    )


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _ensure_child(parent: etree._Element, tag: str, *, first: bool = False) -> etree._Element:
    child = parent.find(tag)
    if child is not None:
        return child
    child = etree.Element(tag)
    if first:
        parent.insert(0, child)
    else:
        parent.append(child)
    return child


def _ensure_on(parent: etree._Element, local: str) -> etree._Element:
    element = _ensure_child(parent, _w(local))
    element.attrib.pop(_w("val"), None)
    return element


def _paragraph_properties(paragraph: etree._Element) -> etree._Element:
    return _ensure_child(paragraph, _w("pPr"), first=True)


def _row_properties(row: etree._Element) -> etree._Element:
    return _ensure_child(row, _w("trPr"), first=True)


def _cell_properties(cell: etree._Element) -> etree._Element:
    return _ensure_child(cell, _w("tcPr"), first=True)


def _table_properties(table: etree._Element) -> etree._Element:
    return _ensure_child(table, _w("tblPr"), first=True)


def _set_width(element: etree._Element, width_twips: int) -> None:
    element.set(_w("type"), "dxa")
    element.set(_w("w"), str(width_twips))


def _section_usable_width(section: etree._Element) -> int:
    page_size = section.find(_w("pgSz"))
    page_margin = section.find(_w("pgMar"))
    if page_size is None:
        return DEFAULT_USABLE_WIDTH_TWIPS
    page_width = _positive_int(page_size.get(_w("w")), 12_240)
    left = _positive_int(page_margin.get(_w("left")), 1_440) if page_margin is not None else 1_440
    right = _positive_int(page_margin.get(_w("right")), 1_440) if page_margin is not None else 1_440
    usable = page_width - left - right
    return usable if 2_880 <= usable <= 20_000 else DEFAULT_USABLE_WIDTH_TWIPS


def _paragraph_is_structurally_empty(paragraph: etree._Element) -> bool:
    properties = paragraph.find(_w("pPr"))
    if properties is not None and len(properties) == 0 and not properties.attrib:
        paragraph.remove(properties)
    return len(paragraph) == 0 and not (paragraph.text or "").strip()


def _normalize_sections(root: etree._Element, stats: _LayoutStats) -> int:
    body = root.find(_w("body"))
    if body is None:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX document has no body")
    sections = list(root.iter(_w("sectPr")))
    direct = body.findall(_w("sectPr"))
    if direct:
        final_section = direct[-1]
    elif sections:
        final_section = sections[-1]
    else:
        final_section = etree.Element(_w("sectPr"))

    for section in sections:
        if section is final_section:
            continue
        parent = section.getparent()
        if parent is None:
            continue
        parent.remove(section)
        stats.section_breaks_removed += 1
        if parent.tag == _w("pPr") and len(parent) == 0 and not parent.attrib:
            paragraph = parent.getparent()
            if paragraph is not None and paragraph.tag == _w("p"):
                paragraph.remove(parent)
                container = paragraph.getparent()
                if container is not None and _paragraph_is_structurally_empty(paragraph):
                    container.remove(paragraph)

    current_parent = final_section.getparent()
    if current_parent is not None:
        current_parent.remove(final_section)
    for extra in body.findall(_w("sectPr")):
        body.remove(extra)
        stats.section_breaks_removed += 1

    section_type = final_section.find(_w("type"))
    if section_type is not None:
        final_section.remove(section_type)
    columns = _ensure_child(final_section, _w("cols"))
    for column in columns.findall(_w("col")):
        columns.remove(column)
    columns.set(_w("num"), "1")
    columns.set(_w("space"), "0")
    body.append(final_section)
    return _section_usable_width(final_section)


def _normalized_cell_text(cell: etree._Element) -> str:
    text = "".join(
        element.text or "" for element in cell.iter() if element.tag in {_w("t"), _w("delText")}
    )
    return re.sub(r"\s+", "", text)


def _is_omitted_placeholder(cell: etree._Element) -> bool:
    if any(
        cell.find(f".//{tag}") is not None
        for tag in (_w("drawing"), _w("fldChar"), _w("instrText"), _m("oMath"))
    ):
        return False
    return _PLACEHOLDER.fullmatch(_normalized_cell_text(cell)) is not None


def _is_figure_table(table: etree._Element) -> bool:
    if table.find(f".//{_w('drawing')}") is not None:
        return True
    return bool(
        table.xpath(
            "ancestor::w:sdt[w:sdtPr/w:tag[@w:val='tex2word:figure']]",
            namespaces=_NS,
        )
    )


def _blank_cell() -> etree._Element:
    cell = etree.Element(_w("tc"))
    cell.append(etree.Element(_w("tcPr")))
    cell.append(etree.Element(_w("p")))
    return cell


def _figure_column_count(item_count: int) -> int:
    if item_count <= 3:
        return max(1, item_count)
    if item_count == 4:
        return 2
    return 3


def _reflow_figure_table(table: etree._Element, stats: _LayoutStats) -> bool:
    rows = table.findall(_w("tr"))
    if len(rows) != 1:
        for row in rows:
            placeholders = [cell for cell in row.findall(_w("tc")) if _is_omitted_placeholder(cell)]
            for cell in placeholders:
                row.remove(cell)
                stats.placeholders_removed += 1
            if not row.findall(_w("tc")):
                row.append(_blank_cell())
        return False

    row = rows[0]
    cells = row.findall(_w("tc"))
    retained = [cell for cell in cells if not _is_omitted_placeholder(cell)]
    removed = len(cells) - len(retained)
    stats.placeholders_removed += removed
    if len(cells) <= 3 and removed == 0:
        return False

    table.remove(row)
    if not retained:
        retained = [_blank_cell()]
    column_count = _figure_column_count(len(retained))
    base_properties = row.find(_w("trPr"))
    for offset in range(0, len(retained), column_count):
        new_row = etree.Element(_w("tr"))
        if base_properties is not None:
            new_row.append(deepcopy(base_properties))
        chunk = retained[offset : offset + column_count]
        for cell in chunk:
            new_row.append(cell)
        for _ in range(column_count - len(chunk)):
            new_row.append(_blank_cell())
        table.append(new_row)
    stats.figure_tables_reflowed += 1
    return True


def _cell_span(cell: etree._Element) -> int:
    properties = cell.find(_w("tcPr"))
    span = properties.find(_w("gridSpan")) if properties is not None else None
    return min(_positive_int(span.get(_w("val")), 1), 256) if span is not None else 1


def _table_column_count(table: etree._Element) -> int:
    return max(
        (
            sum(_cell_span(cell) for cell in row.findall(_w("tc")))
            for row in table.findall(_w("tr"))
        ),
        default=1,
    )


def _allocate_widths(weights: list[int], total: int) -> list[int]:
    count = len(weights)
    if count == 0:
        return [total]
    minimum = min(480, max(1, total // count))
    remainder = max(0, total - minimum * count)
    weight_sum = max(1, sum(weights))
    raw_extras = [remainder * weight / weight_sum for weight in weights]
    extras = [int(value) for value in raw_extras]
    undistributed = remainder - sum(extras)
    order = sorted(range(count), key=lambda index: (-(raw_extras[index] - extras[index]), index))
    for index in order[:undistributed]:
        extras[index] += 1
    return [minimum + extra for extra in extras]


def _normal_table_widths(table: etree._Element, usable_width: int) -> list[int]:
    column_count = _table_column_count(table)
    weights = [6] * column_count
    for row in table.findall(_w("tr")):
        cursor = 0
        for cell in row.findall(_w("tc")):
            span = min(_cell_span(cell), column_count - cursor) if cursor < column_count else 1
            score = max(6, min(60, len(_normalized_cell_text(cell))))
            per_column = max(1, (score + span - 1) // span)
            for index in range(cursor, min(column_count, cursor + span)):
                weights[index] = max(weights[index], per_column)
            cursor += span
    return _allocate_widths(weights, usable_width)


def _set_cell_margins(properties: etree._Element) -> None:
    margins = _ensure_child(properties, _w("tblCellMar"))
    for name, value in (
        ("top", 80),
        ("left", CELL_HORIZONTAL_MARGIN_TWIPS),
        ("bottom", 80),
        ("right", CELL_HORIZONTAL_MARGIN_TWIPS),
    ):
        _set_width(_ensure_child(margins, _w(name)), value)


def _ensure_review_borders(properties: etree._Element) -> None:
    if properties.find(_w("tblBorders")) is not None:
        return
    borders = etree.SubElement(properties, _w("tblBorders"))
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = etree.SubElement(borders, _w(name))
        border.set(_w("val"), "single")
        border.set(_w("sz"), "4")
        border.set(_w("space"), "0")
        border.set(_w("color"), "B7C9D6")


def _bold_header_cell(cell: etree._Element) -> None:
    properties = _cell_properties(cell)
    if properties.find(_w("shd")) is None:
        shading = etree.SubElement(properties, _w("shd"))
        shading.set(_w("val"), "clear")
        shading.set(_w("color"), "auto")
        shading.set(_w("fill"), "EAF2F8")
    for run in cell.iter(_w("r")):
        run_properties = _ensure_child(run, _w("rPr"), first=True)
        _ensure_on(run_properties, "b")


def _scale_cell_images(
    cell: etree._Element,
    cell_width: int,
    *,
    fill_width: bool,
) -> int:
    maximum = max(1, cell_width - 2 * CELL_HORIZONTAL_MARGIN_TWIPS) * TWIP_TO_EMU
    scaled = 0
    for inline in cell.iter(_wp("inline")):
        extent = inline.find(_wp("extent"))
        if extent is None:
            continue
        width = _positive_int(extent.get("cx"), 0)
        height = _positive_int(extent.get("cy"), 0)
        if width <= 0 or height <= 0:
            continue
        if width == maximum or (not fill_width and width < maximum):
            continue
        new_width = maximum
        new_height = max(1, round(height * new_width / width))
        extent.set("cx", str(new_width))
        extent.set("cy", str(new_height))
        for drawing_extent in inline.findall(f".//{_a('xfrm')}/{_a('ext')}"):
            drawing_extent.set("cx", str(new_width))
            drawing_extent.set("cy", str(new_height))
        scaled += 1
    return scaled


def _center_figure_cell(cell: etree._Element) -> None:
    for paragraph in cell.iter(_w("p")):
        properties = _paragraph_properties(paragraph)
        justification = _ensure_child(properties, _w("jc"))
        justification.set(_w("val"), "center")
        _ensure_on(properties, "keepLines")


def _compact_cell_paragraphs(cell: etree._Element) -> None:
    for paragraph in cell.iter(_w("p")):
        properties = _paragraph_properties(paragraph)
        spacing = _ensure_child(properties, _w("spacing"))
        spacing.set(_w("before"), "0")
        spacing.set(_w("after"), "0")


def _configure_table(
    table: etree._Element,
    widths: list[int],
    *,
    figure: bool,
    fill_images: bool,
    stats: _LayoutStats,
) -> None:
    properties = _table_properties(table)
    total_width = sum(widths)
    _set_width(_ensure_child(properties, _w("tblW")), total_width)
    _set_width(_ensure_child(properties, _w("tblInd")), 0)
    layout = _ensure_child(properties, _w("tblLayout"))
    layout.set(_w("type"), "fixed")
    _set_cell_margins(properties)
    if not figure:
        _ensure_review_borders(properties)

    grid = table.find(_w("tblGrid"))
    if grid is None:
        grid = etree.Element(_w("tblGrid"))
        table.insert(table.index(properties) + 1, grid)
    else:
        for child in list(grid):
            grid.remove(child)
    for width in widths:
        column = etree.SubElement(grid, _w("gridCol"))
        column.set(_w("w"), str(width))

    rows = table.findall(_w("tr"))
    for row_index, row in enumerate(rows):
        row_properties = _row_properties(row)
        _ensure_on(row_properties, "cantSplit")
        if row_index == 0 and not figure:
            _ensure_on(row_properties, "tblHeader")
        cursor = 0
        for cell in row.findall(_w("tc")):
            span = _cell_span(cell)
            if cursor >= len(widths):
                cell_width = widths[-1]
            else:
                cell_width = sum(widths[cursor : min(len(widths), cursor + span)])
            cell_width = max(1, cell_width)
            cell_properties = _cell_properties(cell)
            _set_width(_ensure_child(cell_properties, _w("tcW")), cell_width)
            vertical = _ensure_child(cell_properties, _w("vAlign"))
            vertical.set(_w("val"), "center")
            _compact_cell_paragraphs(cell)
            if row_index == 0 and not figure:
                _bold_header_cell(cell)
            if figure:
                _center_figure_cell(cell)
            stats.images_scaled += _scale_cell_images(
                cell,
                cell_width,
                fill_width=fill_images,
            )
            cursor += span

    if figure and rows:
        for paragraph in rows[-1].iter(_w("p")):
            _ensure_on(_paragraph_properties(paragraph), "keepNext")


def _normalize_tables(root: etree._Element, usable_width: int, stats: _LayoutStats) -> None:
    for table in root.iter(_w("tbl")):
        figure = _is_figure_table(table)
        reflowed = False
        if figure:
            stats.figure_tables += 1
            reflowed = _reflow_figure_table(table, stats)
            column_count = _table_column_count(table)
            widths = _allocate_widths([1] * column_count, usable_width)
        else:
            stats.normal_tables += 1
            widths = _normal_table_widths(table, usable_width)
        _configure_table(
            table,
            widths,
            figure=figure,
            fill_images=reflowed,
            stats=stats,
        )


def _inside_table(element: etree._Element) -> bool:
    parent = element.getparent()
    while parent is not None:
        if parent.tag == _w("tbl"):
            return True
        parent = parent.getparent()
    return False


def _normalize_standalone_images(
    root: etree._Element,
    usable_width: int,
    stats: _LayoutStats,
) -> None:
    maximum = usable_width * TWIP_TO_EMU
    for inline in root.iter(_wp("inline")):
        if _inside_table(inline) or inline.find(f".//{_a('blip')}") is None:
            continue
        extent = inline.find(_wp("extent"))
        if extent is None:
            continue
        width = _positive_int(extent.get("cx"), 0)
        height = _positive_int(extent.get("cy"), 0)
        if width <= 0 or height <= 0:
            continue
        if width > maximum:
            new_width = maximum
            new_height = max(1, round(height * new_width / width))
            extent.set("cx", str(new_width))
            extent.set("cy", str(new_height))
            for drawing_extent in inline.findall(f".//{_a('xfrm')}/{_a('ext')}"):
                drawing_extent.set("cx", str(new_width))
                drawing_extent.set("cy", str(new_height))
            stats.images_scaled += 1
        paragraph = inline.getparent()
        while paragraph is not None and paragraph.tag != _w("p"):
            paragraph = paragraph.getparent()
        if paragraph is not None:
            properties = _paragraph_properties(paragraph)
            justification = _ensure_child(properties, _w("jc"))
            justification.set(_w("val"), "center")


def _is_display_equation(paragraph: etree._Element) -> bool:
    has_inline_math = paragraph.find(f".//{_m('oMath')}") is not None
    has_math_paragraph = paragraph.find(f".//{_m('oMathPara')}") is not None
    if not has_inline_math and not has_math_paragraph:
        return False
    instructions = " ".join(element.text or "" for element in paragraph.iter(_w("instrText")))
    return "SEQ EQUATION" in instructions.upper()


def _normalize_equation_tabs(root: etree._Element, usable_width: int, stats: _LayoutStats) -> None:
    for paragraph in root.iter(_w("p")):
        if not _is_display_equation(paragraph):
            continue
        properties = _paragraph_properties(paragraph)
        for old_tabs in properties.findall(_w("tabs")):
            properties.remove(old_tabs)
        _ensure_on(properties, "keepLines")
        tabs = etree.SubElement(properties, _w("tabs"))
        center = etree.SubElement(tabs, _w("tab"))
        center.set(_w("val"), "center")
        center.set(_w("pos"), str(usable_width // 2))
        right = etree.SubElement(tabs, _w("tab"))
        right.set(_w("val"), "right")
        right.set(_w("pos"), str(usable_width))
        stats.equations_retabbed += 1


def _normalize_pagination(root: etree._Element) -> None:
    for paragraph in root.iter(_w("p")):
        if paragraph.find(f".//{_w('drawing')}") is not None:
            properties = _paragraph_properties(paragraph)
            _ensure_on(properties, "keepLines")
            _ensure_on(properties, "keepNext")
        properties = paragraph.find(_w("pPr"))
        style = properties.find(_w("pStyle")) if properties is not None else None
        if style is not None and (style.get(_w("val")) or "").casefold() == "caption":
            _ensure_on(properties, "keepLines")


def _mark_fields_dirty(root: etree._Element, stats: _LayoutStats) -> None:
    for field in root.iter(_w("fldChar")):
        if (field.get(_w("fldCharType")) or "").casefold() != "begin":
            continue
        field.set(_w("dirty"), "true")
        stats.fields_marked_dirty += 1
    for field in root.iter(_w("fldSimple")):
        field.set(_w("dirty"), "true")
        stats.fields_marked_dirty += 1


def _settings_xml(package: DocxPackage) -> bytes:
    if SETTINGS_PART in package.part_names:
        root = package.xml_root(SETTINGS_PART)
        if root.tag != _w("settings"):
            raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX settings root is invalid")
    else:
        root = etree.Element(_w("settings"), nsmap={"w": W_NS})
    controls = root.findall(_w("updateFields"))
    if controls:
        update_fields = controls[0]
        for duplicate in controls[1:]:
            root.remove(duplicate)
    else:
        update_fields = etree.SubElement(root, _w("updateFields"))
    update_fields.set(_w("val"), "true")
    return _xml_bytes(root)


def _content_types_xml(package: DocxPackage) -> bytes:
    root = package.xml_root(CONTENT_TYPES_PART)
    tag = f"{{{CONTENT_TYPES_NS}}}Override"
    matches = [
        child for child in root.findall(tag) if child.get("PartName") == "/word/settings.xml"
    ]
    if len(matches) > 1 or (matches and matches[0].get("ContentType") != SETTINGS_CONTENT_TYPE):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX settings content type is invalid")
    if not matches:
        override = etree.SubElement(root, tag)
        override.set("PartName", "/word/settings.xml")
        override.set("ContentType", SETTINGS_CONTENT_TYPE)
    return _xml_bytes(root)


def _relationship_target(relationship: etree._Element) -> str:
    return (relationship.get("Target") or "").replace("\\", "/").removeprefix("./")


def _document_relationships_xml(package: DocxPackage) -> bytes:
    relationship_tag = f"{{{PACKAGE_REL_NS}}}Relationship"
    if DOCUMENT_RELS_PART in package.part_names:
        root = package.xml_root(DOCUMENT_RELS_PART)
    else:
        root = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships")
    matches = [
        child for child in root.findall(relationship_tag) if child.get("Type") == SETTINGS_REL_TYPE
    ]
    if len(matches) > 1 or (matches and _relationship_target(matches[0]) != "settings.xml"):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX settings relationship is invalid")
    if not matches:
        numeric_ids = [
            int(suffix)
            for relationship in root.findall(relationship_tag)
            if (suffix := (relationship.get("Id") or "").removeprefix("rId")).isdigit()
        ]
        relationship = etree.SubElement(root, relationship_tag)
        relationship.set("Id", f"rId{max(numeric_ids, default=0) + 1}")
        relationship.set("Type", SETTINGS_REL_TYPE)
        relationship.set("Target", "settings.xml")
    return _xml_bytes(root)


def _package_replacements(package: DocxPackage, document_xml: bytes) -> dict[str, bytes]:
    return {
        DOCUMENT_PART: document_xml,
        SETTINGS_PART: _settings_xml(package),
        CONTENT_TYPES_PART: _content_types_xml(package),
        DOCUMENT_RELS_PART: _document_relationships_xml(package),
    }


def _enabled(element: etree._Element) -> bool:
    return _enabled_value(element.get(_w("val")))


def _enabled_value(raw: str | None) -> bool:
    value = (raw or "true").casefold()
    return value not in {"0", "false", "off", "no"}


def _quality_error(message: str) -> ContractError:
    return ContractError(
        ErrorCode.INTERNAL_INVARIANT,
        f"review layout validation failed: {message}",
    )


def validate_review_layout(
    path: Path,
    *,
    field_refresh: Literal["pending", "frozen"] = "pending",
) -> None:
    """Fail closed unless ``path`` satisfies the deterministic review-layout profile.

    ``pending`` is the intermediate state produced immediately before the
    authoritative Word field refresh.  ``frozen`` is the reviewer-facing state:
    cached field results have already been updated and Word must not refresh
    them automatically after Track Changes is enabled.
    """

    package = read_docx_package(path)
    root = package.xml_root(DOCUMENT_PART)
    body = root.find(_w("body"))
    if body is None:
        raise _quality_error("missing document body")
    sections = list(root.iter(_w("sectPr")))
    if len(sections) != 1 or sections[0].getparent() is not body:
        raise _quality_error("document must contain one final body section")
    columns = sections[0].find(_w("cols"))
    if columns is None or _positive_int(columns.get(_w("num")), 1) != 1:
        raise _quality_error("document section is not single-column")
    usable_width = _section_usable_width(sections[0])

    for table in root.iter(_w("tbl")):
        properties = table.find(_w("tblPr"))
        width = properties.find(_w("tblW")) if properties is not None else None
        layout = properties.find(_w("tblLayout")) if properties is not None else None
        grid = table.find(_w("tblGrid"))
        grid_widths = (
            [_positive_int(column.get(_w("w")), 0) for column in grid.findall(_w("gridCol"))]
            if grid is not None
            else []
        )
        if (
            width is None
            or width.get(_w("type")) != "dxa"
            or _positive_int(width.get(_w("w")), 0) != usable_width
            or layout is None
            or layout.get(_w("type")) != "fixed"
            or not grid_widths
            or any(value <= 0 for value in grid_widths)
            or sum(grid_widths) != usable_width
        ):
            raise _quality_error("table geometry is not explicit and bounded")
        rows = table.findall(_w("tr"))
        for row in rows:
            row_properties = row.find(_w("trPr"))
            if row_properties is None or row_properties.find(_w("cantSplit")) is None:
                raise _quality_error("table row can split across pages")
            for cell in row.findall(_w("tc")):
                cell_properties = cell.find(_w("tcPr"))
                cell_width = (
                    cell_properties.find(_w("tcW")) if cell_properties is not None else None
                )
                if (
                    cell_width is None
                    or cell_width.get(_w("type")) != "dxa"
                    or _positive_int(cell_width.get(_w("w")), 0) <= 0
                ):
                    raise _quality_error("table cell width is missing")
                for paragraph in cell.iter(_w("p")):
                    paragraph_properties = paragraph.find(_w("pPr"))
                    spacing = (
                        paragraph_properties.find(_w("spacing"))
                        if paragraph_properties is not None
                        else None
                    )
                    if (
                        spacing is None
                        or spacing.get(_w("before")) != "0"
                        or spacing.get(_w("after")) != "0"
                    ):
                        raise _quality_error("table cell paragraph spacing is not compact")
        if rows and not _is_figure_table(table):
            first_properties = rows[0].find(_w("trPr"))
            if first_properties is None or first_properties.find(_w("tblHeader")) is None:
                raise _quality_error("ordinary table header does not repeat")

    if any(_is_omitted_placeholder(cell) for cell in root.iter(_w("tc"))):
        raise _quality_error("sub-figure placeholder remains visible")
    maximum_image_width = usable_width * TWIP_TO_EMU
    for inline in root.iter(_wp("inline")):
        extent = inline.find(_wp("extent"))
        if extent is None:
            continue
        width = _positive_int(extent.get("cx"), 0)
        height = _positive_int(extent.get("cy"), 0)
        if width <= 0 or height <= 0 or width > maximum_image_width:
            raise _quality_error("inline image geometry exceeds the review page")
    for paragraph in root.iter(_w("p")):
        if not _is_display_equation(paragraph):
            continue
        properties = paragraph.find(_w("pPr"))
        tabs = properties.find(_w("tabs")) if properties is not None else None
        positions = (
            [_positive_int(tab.get(_w("pos")), 0) for tab in tabs.findall(_w("tab"))]
            if tabs is not None
            else []
        )
        if positions != [usable_width // 2, usable_width]:
            raise _quality_error("display-equation tabs do not match page width")
    for field in root.iter(_w("fldChar")):
        if (field.get(_w("fldCharType")) or "").casefold() != "begin":
            continue
        dirty = _enabled_value(field.get(_w("dirty"))) if _w("dirty") in field.attrib else False
        if field_refresh == "pending" and not dirty:
            raise _quality_error("complex field is not marked dirty")
        if field_refresh == "frozen" and dirty:
            raise _quality_error("complex field still requests an automatic refresh")
    for field in root.iter(_w("fldSimple")):
        dirty = _enabled_value(field.get(_w("dirty"))) if _w("dirty") in field.attrib else False
        if field_refresh == "pending" and not dirty:
            raise _quality_error("simple field is not marked dirty")
        if field_refresh == "frozen" and dirty:
            raise _quality_error("simple field still requests an automatic refresh")
    if SETTINGS_PART not in package.part_names:
        raise _quality_error("settings part is missing")
    settings = package.xml_root(SETTINGS_PART)
    update_fields = settings.findall(_w("updateFields"))
    if len(update_fields) != 1:
        raise _quality_error("automatic field refresh control is not unique")
    if field_refresh == "pending" and not _enabled(update_fields[0]):
        raise _quality_error("automatic field refresh is not enabled")
    if field_refresh == "frozen" and _enabled(update_fields[0]):
        raise _quality_error("automatic field refresh is still enabled")


def _write_package(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
    *,
    expected_sha256: str,
) -> str:
    if destination.exists():
        raise ContractError(ErrorCode.BACKEND_FAILED, "review-layout output already exists")
    raw = read_stable_bytes(source, max_bytes=MAX_DOCX_BYTES)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "DOCX changed before layout repair")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.layout-",
        suffix=".docx",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    published = False
    try:
        with (
            zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive,
            zipfile.ZipFile(temporary, mode="w") as output,
        ):
            existing_names: set[str] = set()
            for info in archive.infolist():
                existing_names.add(info.filename)
                output.writestr(info, replacements.get(info.filename, archive.read(info.filename)))
            for name in sorted(set(replacements) - existing_names):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                output.writestr(info, replacements[name])
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        validate_review_layout(temporary)
        final_source = read_stable_bytes(source, max_bytes=MAX_DOCX_BYTES)
        if "sha256:" + hashlib.sha256(final_source).hexdigest() != expected_sha256:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "DOCX changed during layout repair")
        output_sha256 = "sha256:" + hashlib.sha256(temporary.read_bytes()).hexdigest()
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(ErrorCode.BACKEND_FAILED, "review-layout output appeared") from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "review-layout output cannot publish",
            ) from exc
        published = True
        temporary.unlink()
        return output_sha256
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
        if not published:
            with suppress(FileNotFoundError):
                destination.unlink()


def apply_review_layout(source_docx: Path, destination_docx: Path) -> ReviewLayoutReport:
    """Create a normalized review-layout copy without altering ``source_docx``."""

    package = read_docx_package(source_docx)
    root = package.xml_root(DOCUMENT_PART)
    stats = _LayoutStats()
    usable_width = _normalize_sections(root, stats)
    _normalize_tables(root, usable_width, stats)
    _normalize_standalone_images(root, usable_width, stats)
    _normalize_equation_tabs(root, usable_width, stats)
    _normalize_pagination(root)
    _mark_fields_dirty(root, stats)
    output_sha256 = _write_package(
        source_docx,
        destination_docx,
        _package_replacements(package, _xml_bytes(root)),
        expected_sha256=package.file_sha256,
    )
    return ReviewLayoutReport(
        source_sha256=package.file_sha256,
        output_sha256=output_sha256,
        usable_width_twips=usable_width,
        section_breaks_removed=stats.section_breaks_removed,
        figure_tables=stats.figure_tables,
        normal_tables=stats.normal_tables,
        figure_tables_reflowed=stats.figure_tables_reflowed,
        placeholders_removed=stats.placeholders_removed,
        images_scaled=stats.images_scaled,
        equations_retabbed=stats.equations_retabbed,
        fields_marked_dirty=stats.fields_marked_dirty,
    )


__all__ = ["ReviewLayoutReport", "apply_review_layout", "validate_review_layout"]
