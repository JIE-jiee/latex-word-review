"""Independent, source-bound inspection for the static LaTeX changes display DOCX."""

from __future__ import annotations

import io
import uuid
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.canonical import sha256_bytes
from latex_word_review.docx_reader import DocxPackage, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.revision_macros import (
    RevisionDisplayExpectation,
    RevisionMacroInventory,
)

W_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS: Final = "http://schemas.openxmlformats.org/package/2006/content-types"
SETTINGS_PART: Final = "word/settings.xml"
REVISION_DISPLAY_PROFILE_NAME: Final = "latex-revision-blue-display"
REVISION_DISPLAY_PROFILE_VERSION: Final = 2
REVISION_BLUE_HEX: Final = "0000FF"
_MAX_MATCH_CANDIDATES: Final = 100_000
_MAX_MATCH_WORK_UNITS: Final = 25_000_000
_MAX_DOCX_BYTES: Final = 128 * 1024 * 1024
_MATH_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W14_NS: Final = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15_NS: Final = "http://schemas.microsoft.com/office/word/2012/wordml"
_MIXED_STYLE: Final[tuple[object, ...]] = ("<mixed-style>",)
_DISPLAY_MARKER_ROLE: Final = "latex_changes_display_docx"
_DISPLAY_MARKER_PROFILE: Final = "lwr-existing-changes-display-v1"
_DISPLAY_MARKER_NAMES: Final = (
    "LWR_ARTIFACT_ROLE",
    "LWR_ARTIFACT_PROFILE",
    "LWR_RUN_ID",
)
_PROTECTED_XML_PARTS: Final = frozenset(
    {
        "[Content_Types].xml",
        SETTINGS_PART,
        "word/fontTable.xml",
        "word/numbering.xml",
        "word/styles.xml",
    }
)
_IGNORED_SAVE_ATTRIBUTE_LOCALS: Final = frozenset(
    {
        "anchorId",
        "dirty",
        "editId",
        "paraId",
        "rsidDel",
        "rsidP",
        "rsidR",
        "rsidRDefault",
        "rsidRPr",
        "rsidSect",
        "textId",
    }
)
_REVISION_LOCALS: Final = frozenset(
    {
        "cellDel",
        "cellIns",
        "cellMerge",
        "del",
        "ins",
        "moveFrom",
        "moveFromRangeEnd",
        "moveFromRangeStart",
        "moveTo",
        "moveToRangeEnd",
        "moveToRangeStart",
        "numberingChange",
        "pPrChange",
        "rPrChange",
        "sectPrChange",
        "tblGridChange",
        "tblPrChange",
        "tblPrExChange",
        "tcPrChange",
        "trPrChange",
    }
)


def _w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


_MATCH_BARRIER: Final = "\x00"
_PARAGRAPH_CONTAINERS: Final = frozenset(
    {
        _w("bdo"),
        _w("customXml"),
        _w("dir"),
        _w("fldSimple"),
        _w("hyperlink"),
        _w("sdt"),
        _w("sdtContent"),
        _w("smartTag"),
    }
)
_PARAGRAPH_ZERO_WIDTH: Final = frozenset(
    {
        _w("bookmarkEnd"),
        _w("bookmarkStart"),
        _w("commentRangeEnd"),
        _w("commentRangeStart"),
        _w("pPr"),
        _w("permEnd"),
        _w("permStart"),
        _w("proofErr"),
        _w("sdtPr"),
    }
)


def _paragraph_events(element: etree._Element) -> Iterator[etree._Element | None]:
    for child in element:
        if child.tag == _w("r"):
            yield child
        elif child.tag in _PARAGRAPH_ZERO_WIDTH:
            continue
        elif child.tag in _PARAGRAPH_CONTAINERS:
            yield from _paragraph_events(child)
        else:
            yield None


def _enabled(raw: str | None) -> bool:
    return (raw or "true").casefold() not in {"0", "false", "off", "no", "none"}


@dataclass(frozen=True, slots=True)
class RevisionDisplayInspection:
    """Observable presentation facts, independent of the converter report."""

    blue_text_characters: int
    strike_text_characters: int
    highlighted_text_characters: int
    native_revision_elements: int
    track_revisions_enabled: bool
    verified_expectations: int = 0
    verified_blue_text_characters: int = 0
    verified_strike_text_characters: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {
            "revision_display_profile_version": REVISION_DISPLAY_PROFILE_VERSION,
            "revision_display_blue_text_characters": self.blue_text_characters,
            "revision_display_strike_text_characters": self.strike_text_characters,
            "revision_display_highlighted_text_characters": self.highlighted_text_characters,
            "revision_display_native_revision_elements": self.native_revision_elements,
            "revision_display_track_revisions_enabled": int(self.track_revisions_enabled),
            "revision_display_verified_expectations": self.verified_expectations,
            "revision_display_verified_blue_text_characters": (self.verified_blue_text_characters),
            "revision_display_verified_strike_text_characters": (
                self.verified_strike_text_characters
            ),
        }


@dataclass(frozen=True, slots=True)
class _StyledCharacter:
    value: str
    blue: bool
    strike: bool
    double_strike: bool
    highlighted: bool
    style_full: tuple[object, ...]
    style_without_revision: tuple[object, ...]


def _element_record(element: etree._Element) -> tuple[object, ...]:
    attributes = tuple(
        sorted(
            (name, value)
            for name, value in element.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )
    children = tuple(_element_record(child) for child in element)
    return (element.tag, attributes, element.text or "", children)


def _semantic_element_record(element: etree._Element) -> tuple[object, ...]:
    attributes = tuple(
        sorted(
            (name, value)
            for name, value in element.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )
    return (
        element.tag,
        attributes,
        element.text or "",
        tuple(_semantic_element_record(child) for child in element),
    )


def _display_run_id_is_valid(value: str) -> bool:
    if not value.startswith("run_"):
        return False
    try:
        parsed = uuid.UUID(value.removeprefix("run_"))
    except ValueError:
        return False
    return value == f"run_{parsed}" and parsed.version == 7 and parsed.variant == uuid.RFC_4122


def _settings_artifact_marker(root: etree._Element) -> tuple[str, str, str] | None:
    containers = root.findall(_w("docVars"))
    expected_by_fold = {name.casefold(): name for name in _DISPLAY_MARKER_NAMES}
    values: dict[str, str] = {}
    for container in containers:
        for variable in container.findall(_w("docVar")):
            raw_name = variable.get(_w("name"))
            if raw_name is None:
                continue
            canonical_name = expected_by_fold.get(raw_name.casefold())
            if canonical_name is None:
                continue
            value = variable.get(_w("val"))
            if (
                canonical_name in values
                or value is None
                or not value
                or set(variable.attrib) != {_w("name"), _w("val")}
                or len(variable)
                or bool((variable.text or "").strip())
            ):
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display settings artifact marker is malformed",
                )
            values[canonical_name] = value
    if not values:
        return None
    if len(containers) != 1 or set(values) != set(_DISPLAY_MARKER_NAMES):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display settings artifact marker is malformed",
        )
    role, profile, run_id = (values[name] for name in _DISPLAY_MARKER_NAMES)
    if (
        role != _DISPLAY_MARKER_ROLE
        or profile != _DISPLAY_MARKER_PROFILE
        or not _display_run_id_is_valid(run_id)
    ):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display settings artifact marker is invalid",
        )
    return role, profile, run_id


def _settings_element_record(element: etree._Element) -> tuple[object, ...] | None:
    if element.tag in {
        _w("trackRevisions"),
        _w("rsids"),
        f"{{{_W14_NS}}}docId",
        f"{{{_W15_NS}}}docId",
    }:
        return None
    attributes = tuple(
        sorted(
            (name, value)
            for name, value in element.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )
    expected_names = {name.casefold() for name in _DISPLAY_MARKER_NAMES}
    children: list[tuple[object, ...]] = []
    for child in element:
        if (
            element.tag == _w("docVars")
            and child.tag == _w("docVar")
            and (child.get(_w("name")) or "").casefold() in expected_names
        ):
            continue
        child_record = _settings_element_record(child)
        if child_record is not None:
            children.append(child_record)
    if element.tag == _w("docVars") and not attributes and not element.text and not children:
        return None
    return (element.tag, attributes, element.text or "", tuple(children))


def _styles_element_record(element: etree._Element) -> tuple[object, ...] | None:
    if element.tag == _w("rsid"):
        return None
    attributes = tuple(
        sorted(
            (name, value)
            for name, value in element.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )
    children = tuple(
        record for child in element if (record := _styles_element_record(child)) is not None
    )
    return (element.tag, attributes, element.text or "", children)


def _content_types_record(root: etree._Element) -> tuple[object, ...]:
    attributes = tuple(sorted(root.attrib.items()))
    children = tuple(sorted((_semantic_element_record(child) for child in root), key=repr))
    return (root.tag, attributes, root.text or "", children)


def _abstract_numbering_record(element: etree._Element) -> tuple[object, ...]:
    def visit(node: etree._Element) -> tuple[object, ...]:
        attributes = tuple(
            sorted(
                (name, value)
                for name, value in node.attrib.items()
                if not (
                    (node is element and etree.QName(name).localname == "abstractNumId")
                    or etree.QName(name).localname == "tplc"
                    or etree.QName(name).localname in _IGNORED_SAVE_ATTRIBUTE_LOCALS
                )
            )
        )
        children = tuple(
            visit(child) for child in node if child.tag not in {_w("nsid"), _w("tmpl")}
        )
        return (node.tag, attributes, node.text or "", children)

    return visit(element)


def _numbering_record(root: etree._Element) -> tuple[object, ...]:
    abstract_by_id: dict[str, tuple[object, ...]] = {}
    abstract_records: list[tuple[object, ...]] = []
    for abstract in root.findall(_w("abstractNum")):
        abstract_id = abstract.get(_w("abstractNumId"))
        if abstract_id is None or abstract_id in abstract_by_id:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display numbering definitions are malformed",
            )
        record = _abstract_numbering_record(abstract)
        abstract_by_id[abstract_id] = record
        abstract_records.append(record)

    number_records: list[tuple[object, ...]] = []
    for number in root.findall(_w("num")):
        reference = number.find(_w("abstractNumId"))
        reference_id = None if reference is None else reference.get(_w("val"))
        if reference is None or reference_id not in abstract_by_id:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display numbering references are malformed",
            )
        attributes = tuple(
            sorted(
                (name, value)
                for name, value in number.attrib.items()
                if etree.QName(name).localname not in {"numId", "durableId"}
                and etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
            )
        )
        children: list[tuple[object, ...]] = []
        for child in number:
            if child is reference:
                extra_attributes = tuple(
                    sorted(
                        (name, value) for name, value in child.attrib.items() if name != _w("val")
                    )
                )
                children.append(
                    (
                        child.tag,
                        extra_attributes,
                        child.text or "",
                        abstract_by_id[reference_id],
                    )
                )
            else:
                children.append(_semantic_element_record(child))
        number_records.append((number.tag, attributes, number.text or "", tuple(children)))

    handled = {_w("abstractNum"), _w("num")}
    other_records = tuple(
        _semantic_element_record(child) for child in root if child.tag not in handled
    )
    root_attributes = tuple(sorted(root.attrib.items()))
    return (
        root.tag,
        root_attributes,
        root.text or "",
        tuple(sorted(abstract_records, key=repr)),
        tuple(sorted(number_records, key=repr)),
        other_records,
    )


def _protected_part_records(
    package: DocxPackage,
) -> tuple[
    tuple[tuple[str, tuple[object, ...]], ...],
    tuple[str, str, str] | None,
]:
    records: list[tuple[str, tuple[object, ...]]] = []
    artifact_marker: tuple[str, str, str] | None = None
    protected_parts = sorted(
        name
        for name in package.part_names
        if name in _PROTECTED_XML_PARTS
        or (name.startswith("word/theme/") and name.endswith(".xml"))
    )
    for part_uri in protected_parts:
        root = package.xml_root(part_uri)
        if part_uri == SETTINGS_PART:
            artifact_marker = _settings_artifact_marker(root)
            record = _settings_element_record(root)
            if record is None:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display settings evidence is empty",
                )
        elif part_uri == "word/styles.xml":
            record = _styles_element_record(root)
            if record is None:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display style evidence is empty",
                )
        elif part_uri == "word/numbering.xml":
            record = _numbering_record(root)
        elif part_uri == "[Content_Types].xml":
            record = _content_types_record(root)
        else:
            record = _semantic_element_record(root)
        records.append((part_uri, record))
    return tuple(records), artifact_marker


def _style_signature(
    properties: etree._Element | None,
    *,
    omit_revision_properties: bool,
) -> tuple[object, ...]:
    if properties is None:
        return ()
    excluded = {_w("color"), _w("strike")} if omit_revision_properties else set()
    attributes = tuple(
        sorted(
            (name, value)
            for name, value in properties.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )
    children = tuple(_element_record(child) for child in properties if child.tag not in excluded)
    if not attributes and not children:
        return ()
    return (attributes, children)


def _append_character(
    output: list[_StyledCharacter],
    *,
    value: str,
    blue: bool,
    strike: bool,
    double_strike: bool,
    highlighted: bool,
    style_full: tuple[object, ...],
    style_without_revision: tuple[object, ...],
) -> None:
    canonical = " " if value.isspace() else value
    if canonical == " " and output and output[-1].value == " ":
        previous = output[-1]
        output[-1] = _StyledCharacter(
            value=" ",
            blue=previous.blue and blue,
            strike=previous.strike if previous.strike == strike else False,
            double_strike=previous.double_strike or double_strike,
            highlighted=previous.highlighted or highlighted,
            style_full=(previous.style_full if previous.style_full == style_full else _MIXED_STYLE),
            style_without_revision=(
                previous.style_without_revision
                if previous.style_without_revision == style_without_revision
                else _MIXED_STYLE
            ),
        )
        return
    output.append(
        _StyledCharacter(
            canonical,
            blue,
            strike,
            double_strike,
            highlighted,
            style_full,
            style_without_revision,
        )
    )


def _append_barrier(output: list[_StyledCharacter]) -> None:
    if output and output[-1].value == _MATCH_BARRIER:
        return
    output.append(
        _StyledCharacter(
            value=_MATCH_BARRIER,
            blue=False,
            strike=False,
            double_strike=False,
            highlighted=False,
            style_full=(),
            style_without_revision=(),
        )
    )


@dataclass(frozen=True, slots=True)
class _DocumentEvidence:
    inspection: RevisionDisplayInspection
    part_names: tuple[str, ...]
    protected_part_records: tuple[tuple[str, tuple[object, ...]], ...]
    artifact_marker: tuple[str, str, str] | None
    story_parts: tuple[str, ...]
    paragraphs_by_part: tuple[
        tuple[str, tuple[tuple[_StyledCharacter, ...], ...]],
        ...,
    ]
    table_records: tuple[str, ...]
    omml_records: tuple[str, ...]
    field_instruction_records: tuple[str, ...]
    field_control_records: tuple[str, ...]
    paragraph_structure_records: tuple[str, ...]
    section_property_records: tuple[str, ...]
    relationship_records: tuple[str, ...]
    binary_part_records: tuple[tuple[str, int, str], ...]

    @property
    def paragraphs(self) -> tuple[tuple[_StyledCharacter, ...], ...]:
        return tuple(
            paragraph
            for _part_uri, part_paragraphs in self.paragraphs_by_part
            for paragraph in part_paragraphs
        )

    @property
    def paragraph_layout(self) -> tuple[str, ...]:
        return tuple(
            part_uri
            for part_uri, part_paragraphs in self.paragraphs_by_part
            for _paragraph in part_paragraphs
        )


def _table_element_record(element: etree._Element) -> tuple[object, ...]:
    attributes = tuple(
        sorted(
            (name, value)
            for name, value in element.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )
    if element.tag == _w("p"):
        paragraph_properties = element.find(_w("pPr"))
        return (
            element.tag,
            attributes,
            None if paragraph_properties is None else _element_record(paragraph_properties),
        )
    children = tuple(_table_element_record(child) for child in element)
    return (
        element.tag,
        attributes,
        element.text or "",
        children,
    )


def _normalized_field_text(value: str) -> str:
    return " ".join(value.split())


def _structure_attributes(element: etree._Element) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (name, value)
            for name, value in element.attrib.items()
            if etree.QName(name).localname not in _IGNORED_SAVE_ATTRIBUTE_LOCALS
        )
    )


def _paragraph_structure_node(element: etree._Element) -> tuple[object, ...] | None:
    """Return an ordered non-body-text token for one paragraph descendant."""

    if element.tag in {_w("bookmarkStart"), _w("bookmarkEnd"), _w("proofErr")}:
        return None
    if element.tag in {_w("t"), _w("rPr")}:
        return None
    children = tuple(
        record for child in element if (record := _paragraph_structure_node(child)) is not None
    )
    if element.tag == _w("r") and not children and not _structure_attributes(element):
        return None
    text = element.text or ""
    if element.tag == _w("instrText"):
        text = _normalized_field_text(text)
    return (element.tag, _structure_attributes(element), text, children)


def _paragraph_structure_record(element: etree._Element) -> tuple[object, ...]:
    return tuple(
        record for child in element if (record := _paragraph_structure_node(child)) is not None
    )


def _relationship_records(package: DocxPackage) -> tuple[str, ...]:
    relationship_tag = f"{{{PACKAGE_REL_NS}}}Relationship"
    records: list[str] = []
    for part_uri in sorted(name for name in package.part_names if name.endswith(".rels")):
        root = package.xml_root(part_uri)
        for relationship in root.iter(relationship_tag):
            attributes = tuple(sorted(relationship.attrib.items()))
            records.append(f"{part_uri}\0{attributes!r}")
    return tuple(sorted(records))


def _binary_part_records(path: Path, package: DocxPackage) -> tuple[tuple[str, int, str], ...]:
    raw = read_stable_bytes(path, max_bytes=_MAX_DOCX_BYTES)
    if sha256_bytes(raw) != package.file_sha256:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "DOCX changed between package and binary-part inspection",
        )
    records: list[tuple[str, int, str]] = []
    with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
        for name in package.part_names:
            if not (name.startswith("word/media/") or name.startswith("word/embeddings/")):
                continue
            payload = archive.read(name)
            records.append((name, len(payload), sha256_bytes(payload)))
    return tuple(sorted(records))


def _inspect_document(path: Path) -> _DocumentEvidence:
    """Inspect bounded logical text, styles, structure, and binary parts."""

    package = read_docx_package(path)
    protected_part_records, artifact_marker = _protected_part_records(package)
    blue_text_characters = 0
    strike_text_characters = 0
    highlighted_text_characters = 0
    native_revision_elements = 0
    revision_tags = {_w(local) for local in _REVISION_LOCALS}
    paragraphs_by_part: list[tuple[str, tuple[tuple[_StyledCharacter, ...], ...]]] = []
    table_records: list[str] = []
    omml_records: list[str] = []
    field_instruction_records: list[str] = []
    field_control_records: list[str] = []
    paragraph_structure_records: list[str] = []
    section_property_records: list[str] = []
    math_tag = f"{{{_MATH_NS}}}oMath"

    for part_uri in package.story_parts:
        root = package.xml_root(part_uri)
        native_revision_elements += sum(
            1 for element in root.iter() if element.tag in revision_tags
        )
        for section_index, section in enumerate(root.iter(_w("sectPr"))):
            section_property_records.append(
                f"{part_uri}\0{section_index}\0{_element_record(section)!r}"
            )
        part_paragraphs: list[tuple[_StyledCharacter, ...]] = []
        for table_index, table in enumerate(root.iter(_w("tbl"))):
            table_records.append(f"{part_uri}\0{table_index}\0{_table_element_record(table)!r}")
        for math_index, math in enumerate(root.iter(math_tag)):
            omml_records.append(f"{part_uri}\0{math_index}\0{_semantic_element_record(math)!r}")
        field_instruction_index = 0
        for field_element in root.iter():
            if field_element.tag == _w("instrText"):
                field_kind = "instrText"
                normalized = _normalized_field_text(field_element.text or "")
            elif field_element.tag == _w("fldSimple"):
                field_kind = "fldSimple"
                normalized = _normalized_field_text(field_element.get(_w("instr")) or "")
            else:
                continue
            field_instruction_records.append(
                f"{part_uri}\0{field_instruction_index}\0{field_kind}\0{normalized}"
            )
            field_instruction_index += 1
        for control_index, control in enumerate(root.iter(_w("fldChar"))):
            field_control_records.append(
                f"{part_uri}\0{control_index}\0{control.get(_w('fldCharType')) or ''}"
            )
        for paragraph_index, paragraph in enumerate(root.iter(_w("p"))):
            paragraph_structure_records.append(
                f"{part_uri}\0{paragraph_index}\0{_paragraph_structure_record(paragraph)!r}"
            )
            styled: list[_StyledCharacter] = []
            for event in _paragraph_events(paragraph):
                if event is None:
                    _append_barrier(styled)
                    continue
                run = event
                properties = run.find(_w("rPr"))
                style_full = _style_signature(
                    properties,
                    omit_revision_properties=False,
                )
                style_without_revision = _style_signature(
                    properties,
                    omit_revision_properties=True,
                )
                color = None if properties is None else properties.find(_w("color"))
                blue = (
                    color is not None and (color.get(_w("val")) or "").upper() == REVISION_BLUE_HEX
                )
                strike = None if properties is None else properties.find(_w("strike"))
                struck = strike is not None and _enabled(strike.get(_w("val")))
                double_strike = None if properties is None else properties.find(_w("dstrike"))
                double_struck = double_strike is not None and _enabled(double_strike.get(_w("val")))
                highlight = None if properties is None else properties.find(_w("highlight"))
                highlighted = highlight is not None and _enabled(highlight.get(_w("val")))
                for child in run:
                    if child.tag == _w("rPr"):
                        continue
                    if child.tag != _w("t"):
                        _append_barrier(styled)
                        continue
                    value = child.text or ""
                    if blue:
                        blue_text_characters += len(value)
                    if struck:
                        strike_text_characters += len(value)
                    if highlighted:
                        highlighted_text_characters += len(value)
                    for character in value:
                        _append_character(
                            styled,
                            value=character,
                            blue=blue,
                            strike=struck,
                            double_strike=double_struck,
                            highlighted=highlighted,
                            style_full=style_full,
                            style_without_revision=style_without_revision,
                        )
            part_paragraphs.append(tuple(styled))
        paragraphs_by_part.append((part_uri, tuple(part_paragraphs)))

    tracking = False
    if SETTINGS_PART in package.part_names:
        settings = package.xml_root(SETTINGS_PART)
        tracking = any(
            _enabled(control.get(_w("val"))) for control in settings.findall(_w("trackRevisions"))
        )
    inspection = RevisionDisplayInspection(
        blue_text_characters=blue_text_characters,
        strike_text_characters=strike_text_characters,
        highlighted_text_characters=highlighted_text_characters,
        native_revision_elements=native_revision_elements,
        track_revisions_enabled=tracking,
    )
    return _DocumentEvidence(
        inspection=inspection,
        part_names=package.part_names,
        protected_part_records=protected_part_records,
        artifact_marker=artifact_marker,
        story_parts=package.story_parts,
        paragraphs_by_part=tuple(paragraphs_by_part),
        table_records=tuple(table_records),
        omml_records=tuple(omml_records),
        field_instruction_records=tuple(field_instruction_records),
        field_control_records=tuple(field_control_records),
        paragraph_structure_records=tuple(paragraph_structure_records),
        section_property_records=tuple(section_property_records),
        relationship_records=_relationship_records(package),
        binary_part_records=_binary_part_records(path, package),
    )


def _inspect_with_paragraphs(
    path: Path,
) -> tuple[RevisionDisplayInspection, tuple[tuple[_StyledCharacter, ...], ...]]:
    evidence = _inspect_document(path)
    return evidence.inspection, evidence.paragraphs


def inspect_revision_display(path: Path) -> RevisionDisplayInspection:
    """Inspect visible Word runs without trusting source or backend evidence."""

    inspection, _ = _inspect_with_paragraphs(path)
    return inspection


def _matches(
    paragraph: tuple[_StyledCharacter, ...],
    start: int,
    expectation: RevisionDisplayExpectation,
) -> bool:
    for offset, (expected, struck) in enumerate(
        zip(expectation.text, expectation.strike_mask, strict=True)
    ):
        observed = paragraph[start + offset]
        if (
            observed.value != expected
            or not observed.blue
            or observed.strike != struck
            or observed.double_strike
            or observed.highlighted
        ):
            return False
    return True


@dataclass(slots=True)
class _MatchBudget:
    used: int = 0

    def consume(self, units: int) -> None:
        self.used += max(1, units)
        if self.used > _MAX_MATCH_WORK_UNITS:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display matching exceeded its total work safety limit",
            )


@dataclass(frozen=True, slots=True)
class _MatchedExpectationGroup:
    expectations: tuple[RevisionDisplayExpectation, ...]
    intervals: tuple[tuple[int, int, int], ...]


def _text_candidate_intervals(
    paragraphs: tuple[tuple[_StyledCharacter, ...], ...],
    text: str,
    *,
    budget: _MatchBudget,
    stop_after: int | None = None,
) -> list[tuple[int, int, int]]:
    candidates: list[tuple[int, int, int]] = []
    if not text:
        return candidates
    if stop_after is not None and stop_after <= 0:
        return candidates
    for paragraph_index, paragraph in enumerate(paragraphs):
        budget.consume(len(paragraph))
        haystack = "".join(character.value for character in paragraph)
        search_from = 0
        while search_from <= len(haystack):
            budget.consume(len(haystack) - search_from + len(text))
            start = haystack.find(text, search_from)
            if start < 0:
                break
            if len(candidates) >= _MAX_MATCH_CANDIDATES:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display text matching exceeded its safety limit",
                )
            candidates.append((paragraph_index, start, start + len(text)))
            if stop_after is not None and len(candidates) >= stop_after:
                return candidates
            search_from = start + 1
    return candidates


def _candidate_intervals(
    paragraphs: tuple[tuple[_StyledCharacter, ...], ...],
    expectation: RevisionDisplayExpectation,
    textual_candidates: tuple[tuple[int, int, int], ...],
    *,
    budget: _MatchBudget,
) -> list[tuple[int, int, int]]:
    candidates: list[tuple[int, int, int]] = []
    for paragraph_index, start, end in textual_candidates:
        budget.consume(len(expectation.text))
        if _matches(paragraphs[paragraph_index], start, expectation):
            if len(candidates) >= _MAX_MATCH_CANDIDATES:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display matching exceeded its safety limit",
                )
            candidates.append((paragraph_index, start, end))
    return candidates


def _validate_source_expectations(
    paragraphs: tuple[tuple[_StyledCharacter, ...], ...],
    inventory: RevisionMacroInventory,
    *,
    match_budget: _MatchBudget,
) -> tuple[
    int,
    int,
    int,
    tuple[tuple[bool | None, ...], ...],
    tuple[_MatchedExpectationGroup, ...],
]:
    if any(not item.text for item in inventory.display_expectations):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "revision display expectations must contain visible text",
        )
    expectations = inventory.display_expectations
    covered_instances = sum(item.macro_instances for item in inventory.display_expectations)
    if covered_instances != inventory.total:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "revision display expectations do not cover the source inventory",
        )

    grouped: dict[
        tuple[str, tuple[bool, ...]],
        list[RevisionDisplayExpectation],
    ] = defaultdict(list)
    expected_text_multiplicity: Counter[str] = Counter()
    for expectation in expectations:
        grouped[(expectation.text, expectation.strike_mask)].append(expectation)
        expected_text_multiplicity[expectation.text] += 1

    if sum(expected_text_multiplicity.values()) > _MAX_MATCH_CANDIDATES:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display matching exceeded its document safety limit",
        )

    candidate_count = 0
    textual_candidates_by_text: dict[str, tuple[tuple[int, int, int], ...]] = {}
    for expected_text, expected_count in expected_text_multiplicity.items():
        textual_candidates = _text_candidate_intervals(
            paragraphs,
            expected_text,
            budget=match_budget,
            stop_after=expected_count + 1,
        )
        candidate_count += len(textual_candidates)
        if candidate_count > _MAX_MATCH_CANDIDATES:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display matching exceeded its document safety limit",
            )
        if len(textual_candidates) != expected_count:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display text location is ambiguous",
                details={
                    "expected_text_instances": expected_count,
                    "observed_text_instances": len(textual_candidates),
                    "expected_characters": len(expected_text),
                },
            )
        textual_candidates_by_text[expected_text] = tuple(textual_candidates)

    occupied: dict[int, bytearray] = {}

    def occupation_mask(paragraph_index: int) -> bytearray:
        mask = occupied.get(paragraph_index)
        if mask is None:
            match_budget.consume(len(paragraphs[paragraph_index]))
            mask = bytearray(len(paragraphs[paragraph_index]))
            occupied[paragraph_index] = mask
        return mask

    state_characters = sum(len(paragraph) for paragraph in paragraphs)
    match_budget.consume(state_characters)
    revision_states: list[list[bool | None]] = [[None] * len(paragraph) for paragraph in paragraphs]
    matched_groups: list[_MatchedExpectationGroup] = []
    verified_expectations = 0
    verified_blue_text_characters = 0
    verified_strike_text_characters = 0
    ordered_groups = sorted(
        grouped.values(),
        key=lambda items: (-len(items[0].text), items[0].source_call_sha256),
    )
    for items in ordered_groups:
        representative = items[0]
        candidates = _candidate_intervals(
            paragraphs,
            representative,
            textual_candidates_by_text[representative.text],
            budget=match_budget,
        )
        selected: list[tuple[int, int, int]] = []
        for candidate in candidates:
            paragraph_index, start, end = candidate
            interval_length = end - start
            mask = occupation_mask(paragraph_index)
            match_budget.consume(interval_length)
            if any(mask[start:end]):
                continue
            match_budget.consume(interval_length)
            mask[start:end] = b"\x01" * interval_length
            selected.append(candidate)
        if len(selected) != len(items):
            missing = items[min(len(selected), len(items) - 1)]
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display does not have one unambiguous styled source instance",
                details={
                    "path": missing.source_path,
                    "character_offset": missing.source_character_offset,
                    "source_call_sha256": missing.source_call_sha256,
                    "expected_instances": len(items),
                    "observed_instances": len(selected),
                    "expected_characters": len(missing.text),
                    "expected_strike_characters": missing.strike_text_characters,
                },
            )
        matched_groups.append(
            _MatchedExpectationGroup(
                expectations=tuple(items),
                intervals=tuple(selected),
            )
        )
        for paragraph_index, start, end in selected:
            match_budget.consume(end - start)
            for offset, struck in enumerate(representative.strike_mask):
                revision_states[paragraph_index][start + offset] = struck
        verified_expectations += len(items)
        verified_blue_text_characters += sum(len(item.text) for item in items)
        verified_strike_text_characters += sum(item.strike_text_characters for item in items)
    if verified_blue_text_characters != inventory.expected_blue_text_characters:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "verified revision display character total differs from source expectations",
        )
    if verified_strike_text_characters != inventory.expected_strike_text_characters:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "verified revision display deletion total differs from source expectations",
        )
    return (
        verified_expectations,
        verified_blue_text_characters,
        verified_strike_text_characters,
        tuple(tuple(states) for states in revision_states),
        tuple(matched_groups),
    )


def _assert_record_parity(
    kind: str,
    clean_records: tuple[object, ...],
    display_records: tuple[object, ...],
) -> None:
    if clean_records == display_records:
        return
    raise ContractError(
        ErrorCode.EXPORT_SILENT_LOSS,
        f"LaTeX revision display {kind} differs from the clean review",
        details={
            "evidence_kind": kind,
            "clean_record_count": len(clean_records),
            "display_record_count": len(display_records),
        },
    )


def _validate_positional_alignment(
    clean: _DocumentEvidence,
    display: _DocumentEvidence,
    revision_states: tuple[tuple[bool | None, ...], ...],
    *,
    match_budget: _MatchBudget,
) -> tuple[tuple[int, ...], ...]:
    _assert_record_parity("OPC part names", clean.part_names, display.part_names)
    if clean.artifact_marker is not None:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "clean review unexpectedly contains a display artifact marker",
        )
    if display.artifact_marker is None:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display is missing its artifact marker",
        )
    _assert_record_parity(
        "protected DOCX parts",
        clean.protected_part_records,
        display.protected_part_records,
    )
    if clean.story_parts != display.story_parts:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display story parts differ from the clean review",
        )
    if clean.paragraph_layout != display.paragraph_layout:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display paragraph layout differs from the clean review",
            details={
                "clean_paragraphs": len(clean.paragraphs),
                "display_paragraphs": len(display.paragraphs),
            },
        )
    if len(revision_states) != len(display.paragraphs):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "revision position evidence does not cover every display paragraph",
        )

    projection_boundaries: list[tuple[int, ...]] = []
    for paragraph_index, (clean_paragraph, display_paragraph, paragraph_states) in enumerate(
        zip(clean.paragraphs, display.paragraphs, revision_states, strict=True)
    ):
        if len(paragraph_states) != len(display_paragraph):
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "revision position evidence has an invalid paragraph length",
            )
        match_budget.consume(len(clean_paragraph) + (2 * len(display_paragraph)) + 1)
        clean_index = 0
        paragraph_boundaries = [0] * (len(display_paragraph) + 1)
        for display_index, display_character in enumerate(display_paragraph):
            paragraph_boundaries[display_index] = clean_index
            revision_state = paragraph_states[display_index]
            if revision_state is True:
                paragraph_boundaries[display_index + 1] = clean_index
                continue
            adjacent_to_deletion = (
                display_index > 0 and paragraph_states[display_index - 1] is True
            ) or (
                display_index + 1 < len(paragraph_states)
                and paragraph_states[display_index + 1] is True
            )
            if (
                display_character.value == " "
                and adjacent_to_deletion
                and (
                    clean_index >= len(clean_paragraph) or clean_paragraph[clean_index].value != " "
                )
            ):
                paragraph_boundaries[display_index + 1] = clean_index
                continue
            if clean_index >= len(clean_paragraph):
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display adds non-deletion body content",
                    details={
                        "paragraph_index": paragraph_index,
                        "display_character_index": display_index,
                    },
                )
            clean_character = clean_paragraph[clean_index]
            if (
                clean_character.value == " "
                and display_character.value != " "
                and adjacent_to_deletion
            ):
                clean_index += 1
                paragraph_boundaries[display_index] = clean_index
                if clean_index >= len(clean_paragraph):
                    raise ContractError(
                        ErrorCode.EXPORT_SILENT_LOSS,
                        "LaTeX revision display omits clean-review body content",
                        details={"paragraph_index": paragraph_index},
                    )
                clean_character = clean_paragraph[clean_index]
            if display_character.value != clean_character.value:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display body text differs from the clean review",
                    details={
                        "paragraph_index": paragraph_index,
                        "clean_character_index": clean_index,
                        "display_character_index": display_index,
                        "clean_codepoint": ord(clean_character.value),
                        "display_codepoint": ord(display_character.value),
                    },
                )
            if revision_state is False:
                if (
                    display_character.style_without_revision
                    != clean_character.style_without_revision
                ):
                    raise ContractError(
                        ErrorCode.EXPORT_SILENT_LOSS,
                        "LaTeX revision display changes non-revision run properties",
                        details={
                            "paragraph_index": paragraph_index,
                            "clean_character_index": clean_index,
                            "display_character_index": display_index,
                        },
                    )
            elif display_character.style_full != clean_character.style_full:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display moves or changes styling outside a revision macro",
                    details={
                        "paragraph_index": paragraph_index,
                        "clean_character_index": clean_index,
                        "display_character_index": display_index,
                    },
                )
            clean_index += 1
            paragraph_boundaries[display_index + 1] = clean_index
        if clean_index != len(clean_paragraph):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display omits clean-review body content",
                details={
                    "paragraph_index": paragraph_index,
                    "remaining_clean_characters": len(clean_paragraph) - clean_index,
                },
            )
        projection_boundaries.append(tuple(paragraph_boundaries))

    _assert_record_parity("table structure", clean.table_records, display.table_records)
    _assert_record_parity("OMML equations", clean.omml_records, display.omml_records)
    _assert_record_parity(
        "field instructions",
        clean.field_instruction_records,
        display.field_instruction_records,
    )
    _assert_record_parity(
        "field controls",
        clean.field_control_records,
        display.field_control_records,
    )
    _assert_record_parity(
        "paragraph structural tokens",
        clean.paragraph_structure_records,
        display.paragraph_structure_records,
    )
    _assert_record_parity(
        "section properties",
        clean.section_property_records,
        display.section_property_records,
    )
    _assert_record_parity(
        "OPC relationships",
        clean.relationship_records,
        display.relationship_records,
    )
    _assert_record_parity(
        "media and embeddings",
        clean.binary_part_records,
        display.binary_part_records,
    )
    return tuple(projection_boundaries)


def _revision_segment_offsets(
    expectation: RevisionDisplayExpectation,
) -> tuple[tuple[int, int], ...]:
    """Return each struck segment's display offset and clean-projection offset."""

    display_offset = 0
    clean_offset = 0
    offsets: list[tuple[int, int]] = []
    for segment in expectation.segments:
        if segment.strike:
            offsets.append((display_offset, clean_offset))
        else:
            clean_offset += len(segment.text)
        display_offset += len(segment.text)
    return tuple(offsets)


def _append_normalized_text(output: list[str], value: str) -> None:
    for character in value:
        canonical = " " if character.isspace() else character
        if canonical == " " and output and output[-1] == " ":
            continue
        output.append(canonical)


def _deletion_context_pattern(
    expectation: RevisionDisplayExpectation,
) -> tuple[str, tuple[int, ...]]:
    """Build the clean projection and deletion offsets with Word's space fold."""

    clean_projection = "".join(
        segment.text for segment in expectation.segments if not segment.strike
    )
    left_context = expectation.left_context
    if (
        not clean_projection
        and left_context.endswith(" ")
        and not expectation.right_context.startswith(" ")
    ):
        # An empty TeX macro before punctuation (or paragraph end) does not
        # preserve its preceding interword space in the clean Word projection.
        left_context = left_context[:-1]

    output: list[str] = []
    deletion_offsets: list[int] = []
    _append_normalized_text(output, left_context)
    for segment in expectation.segments:
        if segment.strike:
            deletion_offsets.append(len(output))
        else:
            _append_normalized_text(output, segment.text)
    _append_normalized_text(output, expectation.right_context)
    return "".join(output), tuple(deletion_offsets)


def _expected_deletion_boundaries(
    clean: _DocumentEvidence,
    expectation: RevisionDisplayExpectation,
    *,
    match_budget: _MatchBudget,
) -> tuple[tuple[int, int], ...]:
    offsets = _revision_segment_offsets(expectation)
    if not offsets:
        return ()
    if not expectation.left_context and not expectation.right_context:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision deletion has no safe same-paragraph source context",
            details={
                "path": expectation.source_path,
                "character_offset": expectation.source_character_offset,
                "source_call_sha256": expectation.source_call_sha256,
            },
        )
    context_pattern, deletion_offsets = _deletion_context_pattern(expectation)

    candidates = _text_candidate_intervals(
        clean.paragraphs,
        context_pattern,
        budget=match_budget,
        stop_after=2,
    )
    if len(candidates) != 1:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision deletion context does not identify one clean-review boundary",
            details={
                "path": expectation.source_path,
                "character_offset": expectation.source_character_offset,
                "source_call_sha256": expectation.source_call_sha256,
                "observed_context_locations": len(candidates),
                "left_context_characters": len(expectation.left_context),
                "right_context_characters": len(expectation.right_context),
                "clean_projection_characters": sum(
                    len(segment.text) for segment in expectation.segments if not segment.strike
                ),
            },
        )
    paragraph_index, pattern_start, _pattern_end = candidates[0]
    return tuple(
        (paragraph_index, pattern_start + deletion_offset) for deletion_offset in deletion_offsets
    )


def _observed_deletion_boundaries(
    expectation: RevisionDisplayExpectation,
    interval: tuple[int, int, int],
    projection_boundaries: tuple[tuple[int, ...], ...],
) -> tuple[tuple[int, int], ...]:
    paragraph_index, display_start, _display_end = interval
    paragraph_boundaries = projection_boundaries[paragraph_index]
    return tuple(
        (paragraph_index, paragraph_boundaries[display_start + display_offset])
        for display_offset, _clean_offset in _revision_segment_offsets(expectation)
    )


def _validate_deletion_boundaries(
    clean: _DocumentEvidence,
    matched_groups: tuple[_MatchedExpectationGroup, ...],
    projection_boundaries: tuple[tuple[int, ...], ...],
    *,
    match_budget: _MatchBudget,
) -> None:
    for group in matched_groups:
        representative = group.expectations[0]
        if not _revision_segment_offsets(representative):
            continue
        expected = Counter(
            _expected_deletion_boundaries(
                clean,
                expectation,
                match_budget=match_budget,
            )
            for expectation in group.expectations
        )
        observed = Counter(
            _observed_deletion_boundaries(
                representative,
                interval,
                projection_boundaries,
            )
            for interval in group.intervals
        )
        if expected != observed:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision deletion is not at its source-bound clean-review position",
                details={
                    "source_call_sha256": representative.source_call_sha256,
                    "expected_instances": sum(expected.values()),
                    "observed_instances": sum(observed.values()),
                    "expected_unique_boundaries": len(expected),
                    "observed_unique_boundaries": len(observed),
                },
            )


def validate_revision_display(
    path: Path,
    *,
    inventory: RevisionMacroInventory,
    clean_reference: Path,
) -> RevisionDisplayInspection:
    """Fail closed unless the static display is an exact, position-bound derivative."""

    if inventory.total <= 0:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "revision display expectations are invalid")
    display = _inspect_document(path)
    inspection = display.inspection
    if inspection.native_revision_elements or inspection.track_revisions_enabled:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX changes display contains native Word revision state",
        )
    clean = _inspect_document(clean_reference)
    blue_delta = inspection.blue_text_characters - clean.inspection.blue_text_characters
    strike_delta = inspection.strike_text_characters - clean.inspection.strike_text_characters
    highlight_delta = (
        inspection.highlighted_text_characters - clean.inspection.highlighted_text_characters
    )
    if (
        blue_delta != inventory.expected_blue_text_characters
        or strike_delta != inventory.expected_strike_text_characters
        or highlight_delta != 0
    ):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display style delta differs from the clean review",
            details={
                "observed_blue_delta": blue_delta,
                "expected_blue_delta": inventory.expected_blue_text_characters,
                "observed_strike_delta": strike_delta,
                "expected_strike_delta": inventory.expected_strike_text_characters,
                "observed_highlight_delta": highlight_delta,
            },
        )
    match_budget = _MatchBudget()
    (
        verified_expectations,
        verified_blue_text_characters,
        verified_strike_text_characters,
        revision_states,
        matched_groups,
    ) = _validate_source_expectations(
        display.paragraphs,
        inventory,
        match_budget=match_budget,
    )
    projection_boundaries = _validate_positional_alignment(
        clean,
        display,
        revision_states,
        match_budget=match_budget,
    )
    _validate_deletion_boundaries(
        clean,
        matched_groups,
        projection_boundaries,
        match_budget=match_budget,
    )
    return replace(
        inspection,
        verified_expectations=verified_expectations,
        verified_blue_text_characters=verified_blue_text_characters,
        verified_strike_text_characters=verified_strike_text_characters,
    )


__all__ = [
    "REVISION_BLUE_HEX",
    "REVISION_DISPLAY_PROFILE_NAME",
    "REVISION_DISPLAY_PROFILE_VERSION",
    "RevisionDisplayInspection",
    "inspect_revision_display",
    "validate_revision_display",
]
