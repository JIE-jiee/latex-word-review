"""Microsoft Word field refresh and reviewer-facing baseline sealing.

The LaTeX converter emits live ``SEQ``/``REF``/``PAGEREF`` fields whose cached
results are deliberately stale.  This module refreshes those fields while
Track Changes is disabled, verifies Word did not lose review content, and then
freezes automatic refresh before the anchoring stage enables Track Changes.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import io
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.contracts import load_contract_json
from latex_word_review.docx_reader import DocxPackage, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.inspection import DocxInspection, inspect_docx
from latex_word_review.review_layout import apply_review_layout, validate_review_layout
from latex_word_review.runtime import minimal_environment, run_command

W_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/math"
PKG_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCUMENT_PART: Final = "word/document.xml"
SETTINGS_PART: Final = "word/settings.xml"
STYLES_PART: Final = "word/styles.xml"
MAX_DOCX_BYTES: Final = 128 * 1024 * 1024
WORD_FIELD_SCRIPT: Final = "finalize_review_fields.ps1"
_UNRESOLVED_PLACEHOLDER: Final = "[reference unresolved]"

_REVISION_LOCALS: Final = frozenset(
    {
        "ins",
        "del",
        "moveFrom",
        "moveTo",
        "rPrChange",
        "pPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "sectPrChange",
    }
)
_SEQ_FIELD = re.compile(
    r"^SEQ\s+(?P<target>[A-Za-z][A-Za-z0-9_]{0,63})\s+\\\*\s+ARABIC$",
    re.IGNORECASE,
)
_REFERENCE_FIELD = re.compile(
    r"^(?P<kind>REF|PAGEREF)\s+"
    r"(?P<target>[A-Za-z_][A-Za-z0-9_]{0,254})(?P<switches>(?:\s+\\[hr])*)$",
    re.IGNORECASE,
)
_WORD_FAILURE_STAGE = re.compile(r"(?:^|\n)LWR_WORD_FAILURE_STAGE=([a-z_]+)(?:\r?\n|$)")
_WORD_FAILURE_HRESULT = re.compile(r"(?:^|\n)LWR_WORD_FAILURE_HRESULT=(-?\d{1,11})(?:\r?\n|$)")
_WORD_FAILURE_STAGES: Final[frozenset[str]] = frozenset(
    {
        "preflight",
        "word_create",
        "word_ownership",
        "word_ownership_no_new_process",
        "word_ownership_multiple_new_processes",
        "word_ownership_unknown",
        "word_configuration",
        "document_open",
        "document_preflight",
        "document_preflight_disable_tracking",
        "document_preflight_revision_count",
        "document_preflight_revisions_present",
        "first_update",
        "first_repaginate",
        "second_update",
        "second_repaginate",
        "third_update",
        "post_update_validation",
        "save",
        "document_close",
        "word_quit",
        "word_exit",
        "report_output_hash",
        "report_object",
    }
)
_PROTECTED_STRUCTURE_LOCALS: Final[frozenset[str]] = frozenset({"p", "tbl", "tr", "tc"})
_OMML_CORE_LOCALS: Final[frozenset[str]] = frozenset(
    {
        "acc",
        "bar",
        "box",
        "borderBox",
        "d",
        "deg",
        "den",
        "e",
        "eqArr",
        "f",
        "fName",
        "func",
        "groupChr",
        "lim",
        "limLow",
        "limUpp",
        "m",
        "mr",
        "nary",
        "num",
        "phant",
        "rad",
        "sPre",
        "sSub",
        "sSubSup",
        "sSup",
        "sub",
        "sup",
    }
)
_OFFICE_REL_BASE: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_ALLOWED_WORD_RELATIONSHIP_ADDITIONS: Final[frozenset[tuple[str, str, str, str]]] = frozenset(
    {
        (
            "_rels/.rels",
            _OFFICE_REL_BASE + "extended-properties",
            "docProps/app.xml",
            "",
        ),
        (
            "word/_rels/document.xml.rels",
            _OFFICE_REL_BASE + "endnotes",
            "endnotes.xml",
            "",
        ),
        (
            "word/_rels/document.xml.rels",
            _OFFICE_REL_BASE + "fontTable",
            "fontTable.xml",
            "",
        ),
        (
            "word/_rels/document.xml.rels",
            _OFFICE_REL_BASE + "footnotes",
            "footnotes.xml",
            "",
        ),
        (
            "word/_rels/document.xml.rels",
            _OFFICE_REL_BASE + "theme",
            "theme/theme1.xml",
            "",
        ),
        (
            "word/_rels/document.xml.rels",
            _OFFICE_REL_BASE + "webSettings",
            "webSettings.xml",
            "",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class WordFieldFinalizationReport:
    """Path-free evidence for one field-refresh and sealing operation."""

    source_sha256: str
    word_input_sha256: str
    word_output_sha256: str | None
    output_sha256: str
    word_automation_used: bool
    word_version: str | None
    field_count: int
    updated_fields: int
    unresolved_fields: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class _FieldCode:
    kind: str
    target: str
    normalized_code: str


@dataclass(frozen=True, slots=True)
class _WordReport:
    word_version: str
    field_count: int
    updated_fields: int
    unresolved_fields: int
    revision_count: int
    bookmark_count: int
    source_sha256: str
    output_sha256: str


@dataclass(frozen=True, slots=True)
class _ProtectedInventory:
    story_content: tuple[tuple[str, str], ...]
    omml_text: tuple[tuple[str, str], ...]
    omml_structure: tuple[tuple[str, str], ...]
    media: tuple[tuple[str, str], ...]
    relationships: tuple[tuple[str, str, str, str], ...]


def _w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


def _xml_bytes(root: etree._Element) -> bytes:
    return cast(
        bytes,
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True),
    )


def _token_digest(tokens: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for token in tokens:
        encoded = token.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _protected_story_digest(root: etree._Element) -> str:
    """Hash reviewer text outside mutable Word-field result ranges."""

    tokens: list[str] = []
    text: list[str] = []
    field_state = "outside"

    def flush_text() -> None:
        if text:
            tokens.append("TEXT:" + "".join(text))
            text.clear()

    def emit_marker(value: str) -> None:
        flush_text()
        tokens.append(value)

    def walk(element: etree._Element) -> None:
        nonlocal field_state
        name = etree.QName(element)
        namespace = name.namespace
        local = name.localname

        if namespace == W_NS and local == "fldSimple":
            emit_marker("FIELD_SIMPLE")
            return
        if namespace == W_NS and local == "fldChar":
            field_type = (element.get(_w("fldCharType")) or "").casefold()
            if field_type == "begin":
                emit_marker("FIELD_BEGIN")
                field_state = "instruction"
            elif field_type == "separate":
                emit_marker("FIELD_SEPARATE")
                field_state = "result"
            elif field_type == "end":
                emit_marker("FIELD_END")
                field_state = "outside"
            return
        if namespace == M_NS:
            return

        is_structure = namespace == W_NS and local in _PROTECTED_STRUCTURE_LOCALS
        if is_structure:
            emit_marker(f"START:{local}")

        if namespace == W_NS and local == "t" and field_state == "outside":
            text.append(element.text or "")
        elif (
            namespace == W_NS
            and local in {"tab", "br", "cr", "noBreakHyphen", "softHyphen"}
            and field_state == "outside"
        ):
            if local == "tab":
                text.append("\t")
            elif local in {"br", "cr"}:
                break_type = (element.get(_w("type")) or "textWrapping").casefold()
                if break_type == "textwrapping":
                    text.append("\n")
                else:
                    emit_marker(f"BREAK:{break_type}")
            elif local == "noBreakHyphen":
                text.append("\u2011")
            else:
                text.append("\u00ad")
        elif namespace == W_NS and local == "sym" and field_state == "outside":
            emit_marker(
                "SYM:" + (element.get(_w("font")) or "") + ":" + (element.get(_w("char")) or "")
            )

        if not (namespace == W_NS and local in {"t", "instrText"}):
            for child in element:
                walk(child)

        if is_structure:
            emit_marker(f"END:{local}")

    walk(root)
    flush_text()
    return _token_digest(tokens)


def _omml_digests(root: etree._Element) -> tuple[str, str]:
    text = "".join(element.text or "" for element in root.iter(f"{{{M_NS}}}t"))
    structure = (
        etree.QName(element).localname
        for element in root.iter()
        if etree.QName(element).namespace == M_NS
        and etree.QName(element).localname in _OMML_CORE_LOCALS
    )
    return _token_digest((text,)), _token_digest(structure)


def _relationship_inventory(package: DocxPackage) -> tuple[tuple[str, str, str, str], ...]:
    relationships: list[tuple[str, str, str, str]] = []
    for part_uri in package.part_names:
        if not part_uri.endswith(".rels"):
            continue
        root = package.xml_root(part_uri)
        for relationship in root.iter(f"{{{PKG_REL_NS}}}Relationship"):
            relationships.append(
                (
                    part_uri,
                    relationship.get("Type") or "",
                    relationship.get("Target") or "",
                    relationship.get("TargetMode") or "",
                )
            )
    return tuple(sorted(relationships))


def _media_inventory(path: Path, package: DocxPackage) -> tuple[tuple[str, str], ...]:
    raw = read_stable_bytes(path, max_bytes=MAX_DOCX_BYTES)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != package.file_sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "DOCX changed during protection scan")
    media: list[tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
        for name in package.part_names:
            if name.startswith("word/media/"):
                media.append((name, hashlib.sha256(archive.read(name)).hexdigest()))
    return tuple(sorted(media))


def _is_default_note_story(part_uri: str, root: etree._Element) -> bool:
    note_local = {
        "word/footnotes.xml": "footnote",
        "word/endnotes.xml": "endnote",
    }.get(part_uri)
    if note_local is None:
        return False
    for note in root.iter(_w(note_local)):
        raw_identifier = note.get(_w("id"))
        try:
            identifier = int(raw_identifier) if raw_identifier is not None else 1
        except ValueError:
            return False
        if identifier > 0:
            return False
    if any((element.text or "") for element in root.iter(_w("t"))):
        return False
    if any(True for _ in root.iter(f"{{{M_NS}}}oMath")):
        return False
    protected_elements = {
        "bookmarkStart",
        "drawing",
        "fldChar",
        "fldSimple",
        "object",
        "pict",
        "tbl",
    }
    return not any(root.find(f".//{_w(local)}") is not None for local in protected_elements)


def _protected_inventory(path: Path, package: DocxPackage) -> _ProtectedInventory:
    story_content: list[tuple[str, str]] = []
    omml_text: list[tuple[str, str]] = []
    omml_structure: list[tuple[str, str]] = []
    for part_uri in package.story_parts:
        root = package.xml_root(part_uri)
        if _is_default_note_story(part_uri, root):
            continue
        story_content.append((part_uri, _protected_story_digest(root)))
        text_digest, structure_digest = _omml_digests(root)
        omml_text.append((part_uri, text_digest))
        omml_structure.append((part_uri, structure_digest))
    return _ProtectedInventory(
        story_content=tuple(story_content),
        omml_text=tuple(omml_text),
        omml_structure=tuple(omml_structure),
        media=_media_inventory(path, package),
        relationships=_relationship_inventory(package),
    )


def _relationships_compatible(
    before: tuple[tuple[str, str, str, str], ...],
    after: tuple[tuple[str, str, str, str], ...],
) -> bool:
    before_set = set(before)
    after_set = set(after)
    return before_set.issubset(after_set) and (after_set - before_set).issubset(
        _ALLOWED_WORD_RELATIONSHIP_ADDITIONS
    )


def _compare_protected_content(
    before: _ProtectedInventory,
    after: _ProtectedInventory,
) -> None:
    categories = {
        "story_content": (before.story_content, after.story_content),
        "omml_text": (before.omml_text, after.omml_text),
        "omml_structure": (before.omml_structure, after.omml_structure),
        "media": (before.media, after.media),
    }
    changed = [name for name, values in categories.items() if values[0] != values[1]]
    if not _relationships_compatible(before.relationships, after.relationships):
        changed.append("relationships")
    changed.sort()
    if changed:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "Microsoft Word changed protected review content during field refresh",
            details={"stage": "field_finalize", "changed_categories": changed},
        )


def _enabled(element: etree._Element) -> bool:
    return _enabled_value(element.get(_w("val")))


def _enabled_value(raw: str | None) -> bool:
    value = raw
    return value is None or value.casefold() not in {"0", "false", "off", "no"}


def _normalized_instruction(value: str) -> str:
    return " ".join(value.split())


def _parse_field_code(value: str) -> _FieldCode:
    normalized = _normalized_instruction(value)
    if match := _SEQ_FIELD.fullmatch(normalized):
        return _FieldCode("SEQ", match.group("target"), normalized)
    if match := _REFERENCE_FIELD.fullmatch(normalized):
        kind = match.group("kind").upper()
        switches = tuple(item.casefold() for item in match.group("switches").split())
        if (
            len(switches) != len(set(switches))
            or any(item not in {"\\h", "\\r"} for item in switches)
            or (kind == "PAGEREF" and "\\r" in switches)
        ):
            raise ContractError(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "review DOCX contains a field switch that is unsafe to refresh automatically",
                details={
                    "stage": "field_finalize",
                    "failure_kind": "unsupported_field",
                },
            )
        return _FieldCode(kind, match.group("target"), normalized)
    raise ContractError(
        ErrorCode.BACKEND_CAPABILITY_MISSING,
        "review DOCX contains a field that is unsafe to refresh automatically",
        details={"stage": "field_finalize", "failure_kind": "unsupported_field"},
    )


def _field_codes_in_root(root: etree._Element) -> tuple[_FieldCode, ...]:
    codes: list[_FieldCode] = []
    active: list[str] | None = None
    for element in root.iter():
        if element.tag == _w("fldSimple"):
            if active is not None:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "nested Word fields cannot be refreshed safely",
                )
            instruction = element.get(_w("instr"))
            if not instruction:
                raise ContractError(ErrorCode.EXPORT_SILENT_LOSS, "Word field code is missing")
            codes.append(_parse_field_code(instruction))
            continue
        if element.tag == _w("fldChar"):
            field_type = (element.get(_w("fldCharType")) or "").casefold()
            if field_type == "begin":
                if active is not None:
                    raise ContractError(
                        ErrorCode.EXPORT_SILENT_LOSS,
                        "nested Word fields cannot be refreshed safely",
                    )
                active = []
            elif field_type == "end":
                if active is None:
                    raise ContractError(
                        ErrorCode.EXPORT_SILENT_LOSS,
                        "Word field terminator has no matching start",
                    )
                instruction = "".join(active)
                if not instruction.strip():
                    raise ContractError(ErrorCode.EXPORT_SILENT_LOSS, "Word field code is missing")
                codes.append(_parse_field_code(instruction))
                active = None
            continue
        if element.tag == _w("instrText") and active is not None:
            active.append(element.text or "")
    if active is not None:
        raise ContractError(ErrorCode.EXPORT_SILENT_LOSS, "Word field is not terminated")
    return tuple(codes)


def _field_inventory(package: DocxPackage) -> tuple[_FieldCode, ...]:
    for part_uri in package.story_parts:
        root = package.xml_root(part_uri)
        if part_uri != DOCUMENT_PART and (
            root.find(f".//{_w('fldChar')}") is not None
            or root.find(f".//{_w('fldSimple')}") is not None
        ):
            raise ContractError(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "fields outside the main Word story require manual handling",
                details={"stage": "field_finalize", "failure_kind": "unsupported_story"},
            )
    document = package.xml_root(DOCUMENT_PART)
    for textbox in document.iter(_w("txbxContent")):
        if (
            textbox.find(f".//{_w('fldChar')}") is not None
            or textbox.find(f".//{_w('fldSimple')}") is not None
        ):
            raise ContractError(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "fields inside Word text boxes require manual handling",
                details={
                    "stage": "field_finalize",
                    "failure_kind": "unsupported_story",
                },
            )
    return _field_codes_in_root(document)


def _missing_reference_fields(
    package: DocxPackage,
    fields: tuple[_FieldCode, ...],
) -> tuple[_FieldCode, ...]:
    root = package.xml_root(DOCUMENT_PART)
    bookmarks = {
        name.casefold()
        for item in root.iter(_w("bookmarkStart"))
        if (name := item.get(_w("name"))) is not None
    }
    return tuple(
        field
        for field in fields
        if field.kind in {"REF", "PAGEREF"} and field.target.casefold() not in bookmarks
    )


def _validate_no_revision_xml(package: DocxPackage) -> None:
    for part_uri in package.story_parts:
        root = package.xml_root(part_uri)
        if any(
            etree.QName(element).namespace == W_NS
            and etree.QName(element).localname in _REVISION_LOCALS
            for element in root.iter()
        ):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "review baseline contains revisions before reviewer handoff",
            )


def validate_frozen_field_state(package: DocxPackage) -> None:
    """Reject any field flag that would auto-refresh after tracking is enabled."""

    if SETTINGS_PART in package.part_names:
        settings = package.xml_root(SETTINGS_PART)
        controls = settings.findall(_w("updateFields"))
        if len(controls) > 1 or (controls and _enabled(controls[0])):
            raise ContractError(
                ErrorCode.REVIEW_TRACKING_DISABLED,
                "review baseline still enables automatic field refresh",
            )
    for part_uri in package.story_parts:
        root = package.xml_root(part_uri)
        for field in root.iter(_w("fldChar")):
            if (field.get(_w("fldCharType")) or "").casefold() != "begin":
                continue
            if _w("dirty") in field.attrib and _enabled_value(field.get(_w("dirty"))):
                raise ContractError(
                    ErrorCode.REVIEW_TRACKING_DISABLED,
                    "review baseline still contains a dirty field",
                )
        for field in root.iter(_w("fldSimple")):
            if _w("dirty") in field.attrib and _enabled_value(field.get(_w("dirty"))):
                raise ContractError(
                    ErrorCode.REVIEW_TRACKING_DISABLED,
                    "review baseline still contains a dirty field",
                )


def _temporary_path(destination: Path, label: str, suffix: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.{label}-",
        suffix=suffix,
        dir=destination.parent,
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _rewrite_package(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
    *,
    expected_sha256: str,
    owner: str,
) -> str:
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.BACKEND_FAILED, f"{owner} output already exists")
    raw = read_stable_bytes(source, max_bytes=MAX_DOCX_BYTES)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, f"DOCX changed before {owner}")
    temporary = _temporary_path(destination, owner, ".docx")
    try:
        with (
            zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive,
            zipfile.ZipFile(temporary, mode="w") as output,
        ):
            existing: set[str] = set()
            for info in archive.infolist():
                existing.add(info.filename)
                output.writestr(info, replacements.get(info.filename, archive.read(info.filename)))
            for name in sorted(set(replacements) - existing):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                output.writestr(info, replacements[name])
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        output_package = read_docx_package(temporary)
        current = read_stable_bytes(source, max_bytes=MAX_DOCX_BYTES)
        if "sha256:" + hashlib.sha256(current).hexdigest() != expected_sha256:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, f"DOCX changed during {owner}")
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(ErrorCode.BACKEND_FAILED, f"{owner} output appeared") from exc
        except OSError as exc:
            raise ContractError(ErrorCode.BACKEND_FAILED, f"{owner} output cannot publish") from exc
        return output_package.file_sha256
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _trackless_settings(package: DocxPackage) -> bytes:
    if SETTINGS_PART not in package.part_names:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX settings part is missing")
    root = package.xml_root(SETTINGS_PART)
    for control in root.findall(_w("trackRevisions")):
        root.remove(control)
    return _xml_bytes(root)


def _prepare_word_input(source: Path, destination: Path) -> str:
    package = read_docx_package(source)
    return _rewrite_package(
        source,
        destination,
        {SETTINGS_PART: _trackless_settings(package)},
        expected_sha256=package.file_sha256,
        owner="word-input",
    )


def _style_name(style: etree._Element) -> str:
    name = style.find(_w("name"))
    value = None if name is None else cast("str | None", name.get(_w("val")))
    if not value or not value.strip():
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "Word style has no stable name")
    return value.strip().casefold()


def _styles_by_name(root: etree._Element) -> dict[str, etree._Element]:
    if root.tag != _w("styles"):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "Word styles root is invalid")
    result: dict[str, etree._Element] = {}
    identifiers: set[str] = set()
    for style in root.findall(_w("style")):
        identifier = style.get(_w("styleId"))
        name = _style_name(style)
        if not identifier or identifier in identifiers or name in result:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "Word styles contain a duplicate or missing identity",
            )
        identifiers.add(identifier)
        result[name] = style
    return result


def _restore_review_style_properties(current: bytes, reference: bytes) -> bytes:
    try:
        current_root = etree.fromstring(current)
        reference_root = etree.fromstring(reference)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "Word styles XML is invalid") from exc
    current_styles = _styles_by_name(current_root)
    reference_styles = _styles_by_name(reference_root)
    missing = sorted(set(reference_styles).difference(current_styles))
    if missing:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "Microsoft Word removed one or more review styles",
            details={"missing_style_count": len(missing)},
        )

    reference_defaults = reference_root.find(_w("docDefaults"))
    if reference_defaults is None:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "review style defaults are missing")
    current_defaults = current_root.find(_w("docDefaults"))
    restored_defaults = deepcopy(reference_defaults)
    if current_defaults is None:
        current_root.insert(0, restored_defaults)
    else:
        current_root.replace(current_defaults, restored_defaults)

    property_tags = {
        _w("pPr"),
        _w("rPr"),
        _w("tblPr"),
        _w("trPr"),
        _w("tcPr"),
    }
    for name, reference_style in reference_styles.items():
        current_style = current_styles[name]
        for child in tuple(current_style):
            if child.tag in property_tags:
                current_style.remove(child)
        for child in reference_style:
            if child.tag in property_tags:
                current_style.append(deepcopy(child))
    return _xml_bytes(current_root)


def _sealed_replacements(
    package: DocxPackage,
    *,
    reference_styles: bytes | None,
) -> dict[str, bytes]:
    replacements: dict[str, bytes] = {}
    for part_uri in package.story_parts:
        root = package.xml_root(part_uri)
        changed = False
        for field in root.iter(_w("fldChar")):
            if _w("dirty") in field.attrib:
                field.attrib.pop(_w("dirty"), None)
                changed = True
        for field in root.iter(_w("fldSimple")):
            if _w("dirty") in field.attrib:
                field.attrib.pop(_w("dirty"), None)
                changed = True
        if changed:
            replacements[part_uri] = _xml_bytes(root)

    if SETTINGS_PART not in package.part_names:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX settings part is missing")
    settings = package.xml_root(SETTINGS_PART)
    for control in settings.findall(_w("trackRevisions")):
        settings.remove(control)
    updates = settings.findall(_w("updateFields"))
    if updates:
        update = updates[0]
        for duplicate in updates[1:]:
            settings.remove(duplicate)
    else:
        update = etree.SubElement(settings, _w("updateFields"))
    update.set(_w("val"), "false")
    replacements[SETTINGS_PART] = _xml_bytes(settings)
    if reference_styles is not None:
        if STYLES_PART not in package.part_names:
            raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX styles part is missing")
        replacements[STYLES_PART] = _restore_review_style_properties(
            package.xml_bytes(STYLES_PART),
            reference_styles,
        )
    return replacements


def _seal_fields(
    source: Path,
    destination: Path,
    *,
    reference_styles: bytes | None,
) -> str:
    package = read_docx_package(source)
    output_sha256 = _rewrite_package(
        source,
        destination,
        _sealed_replacements(package, reference_styles=reference_styles),
        expected_sha256=package.file_sha256,
        owner="field-seal",
    )
    sealed = read_docx_package(destination)
    validate_frozen_field_state(sealed)
    _validate_no_revision_xml(sealed)
    validate_review_layout(destination, field_refresh="frozen")
    return output_sha256


def _word_automation_path(path: Path) -> str:
    """Return the ordinary Win32 spelling required by Word COM."""

    value = os.path.abspath(os.fspath(path))
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        if not (re.match(r"^[A-Za-z]:\\", value) or value.startswith("\\\\")):
            raise ContractError(
                ErrorCode.PATH_ABSOLUTE,
                "Word automation path is not an ordinary absolute Windows path",
            )
        if len(value) > 259:
            raise ContractError(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "Word automation path exceeds the legacy Office path bound",
                details={
                    "stage": "field_finalize",
                    "failure_kind": "word_path_too_long",
                    "path_length": len(value),
                },
            )
    return value


def _powershell_executable() -> Path:
    if os.name != "nt":
        raise ContractError(ErrorCode.TOOL_MISSING, "Microsoft Word refresh requires Windows")
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if not system_root:
        raise ContractError(ErrorCode.TOOL_MISSING, "Windows PowerShell is unavailable")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.TOOL_MISSING, "Windows PowerShell is unavailable") from exc
    if not resolved.is_file():
        raise ContractError(ErrorCode.TOOL_MISSING, "Windows PowerShell is unavailable")
    return resolved


def _read_word_report(path: Path) -> _WordReport:
    try:
        payload = load_contract_json(read_stable_bytes(path, max_bytes=64 * 1024))
    except ContractError as exc:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "Word field report is invalid",
            details={"stage": "field_finalize", "failure_kind": "invalid_report"},
        ) from exc
    expected = {
        "schema_version",
        "status",
        "word_version",
        "field_count",
        "updated_fields",
        "unresolved_fields",
        "revision_count",
        "bookmark_count",
        "source_sha256",
        "output_sha256",
    }
    if set(payload) != expected or payload.get("schema_version") != "word-field-refresh/v1":
        raise ContractError(ErrorCode.BACKEND_FAILED, "Word field report is invalid")
    if payload.get("status") != "passed":
        raise ContractError(ErrorCode.BACKEND_FAILED, "Word field refresh did not pass")
    word_version = payload.get("word_version")
    source_sha256 = payload.get("source_sha256")
    output_sha256 = payload.get("output_sha256")
    counters = {
        key: payload.get(key)
        for key in (
            "field_count",
            "updated_fields",
            "unresolved_fields",
            "revision_count",
            "bookmark_count",
        )
    }
    if (
        not isinstance(word_version, str)
        or not word_version
        or len(word_version) > 32
        or not isinstance(source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or not isinstance(output_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", output_sha256)
        or any(type(value) is not int or value < 0 for value in counters.values())
    ):
        raise ContractError(ErrorCode.BACKEND_FAILED, "Word field report values are invalid")
    return _WordReport(
        word_version=word_version,
        field_count=cast(int, counters["field_count"]),
        updated_fields=cast(int, counters["updated_fields"]),
        unresolved_fields=cast(int, counters["unresolved_fields"]),
        revision_count=cast(int, counters["revision_count"]),
        bookmark_count=cast(int, counters["bookmark_count"]),
        source_sha256=source_sha256,
        output_sha256=output_sha256,
    )


def _cleanup_owned_word_process(pid_state: Path, *, cwd: Path) -> None:
    del cwd
    if os.name != "nt":
        return
    if not pid_state.is_file():
        return
    try:
        state = load_contract_json(read_stable_bytes(pid_state, max_bytes=4096))
    except ContractError:
        return
    if set(state) != {
        "schema_version",
        "owned",
        "pid",
        "started_filetime_utc",
    }:
        return
    pid = state.get("pid")
    started_filetime_utc = state.get("started_filetime_utc")
    if state.get("schema_version") != "word-field-process/v1" or state.get("owned") is not True:
        return
    if (
        type(pid) is not int
        or not 0 < pid <= 0xFFFFFFFF
        or type(started_filetime_utc) is not int
        or not 0 < started_filetime_utc <= 0xFFFFFFFFFFFFFFFF
    ):
        return

    import ctypes
    from ctypes import wintypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("low", wintypes.DWORD),
            ("high", wintypes.DWORD),
        ]

    process_query_limited_information = 0x1000
    process_terminate = 0x0001
    synchronize = 0x00100000
    wait_timeout_ms = 10_000
    kernel32 = loader("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    get_process_times.restype = wintypes.BOOL
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_image.restype = wintypes.BOOL
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate.restype = wintypes.BOOL

    access = process_query_limited_information | process_terminate | synchronize
    handle = open_process(access, False, pid)
    if not handle:
        return
    try:
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return
        actual_started = (int(creation.high) << 32) | int(creation.low)
        if actual_started != started_filetime_utc:
            return
        image_size = wintypes.DWORD(32_768)
        image_buffer = ctypes.create_unicode_buffer(image_size.value)
        if not query_image(handle, 0, image_buffer, ctypes.byref(image_size)):
            return
        if Path(image_buffer.value).name.casefold() != "winword.exe":
            return
        if terminate(handle, 1):
            kernel32.WaitForSingleObject(handle, wait_timeout_ms)
    finally:
        kernel32.CloseHandle(handle)


def _invoke_word(
    source: Path,
    working: Path,
    destination: Path,
    report_path: Path,
    pid_state: Path,
    *,
    expected_fields: int,
    expected_unresolved: int,
) -> tuple[_WordReport, int]:
    automation_source = _word_automation_path(source)
    automation_working = _word_automation_path(working)
    automation_destination = _word_automation_path(destination)
    automation_report = _word_automation_path(report_path)
    automation_pid_state = _word_automation_path(pid_state)
    automation_root = Path(_word_automation_path(destination.parent))
    resource = importlib.resources.files("latex_word_review").joinpath("assets", WORD_FIELD_SCRIPT)
    try:
        with importlib.resources.as_file(resource) as script:
            if not script.is_file():
                raise ContractError(ErrorCode.TOOL_MISSING, "Word field refresh asset is missing")
            result = run_command(
                _powershell_executable(),
                (
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Sta",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    _word_automation_path(script),
                    "-SourceDocx",
                    automation_source,
                    "-WorkingDocx",
                    automation_working,
                    "-DestinationDocx",
                    automation_destination,
                    "-ReportPath",
                    automation_report,
                    "-PidStatePath",
                    automation_pid_state,
                    "-ExpectedFieldCount",
                    str(expected_fields),
                    "-ExpectedUnresolvedFields",
                    str(expected_unresolved),
                    "-UnresolvedPlaceholder",
                    _UNRESOLVED_PLACEHOLDER,
                ),
                cwd=automation_root,
                timeout_s=60,
                max_output_bytes=64 * 1024,
                environment=minimal_environment(temp_root=automation_root),
            )
    finally:
        _cleanup_owned_word_process(pid_state, cwd=destination.parent)
    stage_match = _WORD_FAILURE_STAGE.search(result.stderr)
    word_failure_stage = None if stage_match is None else stage_match.group(1)
    if word_failure_stage not in _WORD_FAILURE_STAGES:
        word_failure_stage = None
    hresult_match = _WORD_FAILURE_HRESULT.search(result.stderr)
    word_failure_hresult = None if hresult_match is None else int(hresult_match.group(1))
    if word_failure_hresult is not None and not (-(2**31) <= word_failure_hresult <= 2**31 - 1):
        word_failure_hresult = None

    if result.timed_out or result.output_truncated or result.returncode != 0:
        code = ErrorCode.TOOL_MISSING if result.returncode == 3 else ErrorCode.BACKEND_FAILED
        raise ContractError(
            code,
            "Microsoft Word could not safely refresh the review fields",
            details={
                "stage": "field_finalize",
                "failure_kind": (
                    "timeout"
                    if result.timed_out
                    else "output_truncated"
                    if result.output_truncated
                    else "word_unavailable"
                    if result.returncode == 3
                    else "nonzero_exit"
                ),
                "timed_out": result.timed_out,
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
                "output_truncated": result.output_truncated,
                "word_failure_stage": word_failure_stage,
                "word_failure_hresult": word_failure_hresult,
            },
        )
    if not destination.is_file() or not report_path.is_file():
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "Microsoft Word returned success without its field artifacts",
        )
    return _read_word_report(report_path), result.duration_ms


def _compare_structure(before: DocxInspection, after: DocxInspection) -> None:
    if not before.package_valid or not before.structure_inspected:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "field input DOCX is invalid")
    if not after.package_valid or not after.structure_inspected:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "Word field output DOCX is invalid")
    exact_metrics = (
        "images",
        "image_instances",
        "tables",
        "seq_fields",
        "ref_fields",
        "pageref_fields",
        "external_relationships",
    )
    changed = tuple(key for key in exact_metrics if getattr(before, key) != getattr(after, key))
    if changed:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "Microsoft Word changed protected review structures during field refresh",
            details={
                "stage": "field_finalize",
                "mismatched_metrics": list(changed),
                "before": {key: getattr(before, key) for key in changed},
                "after": {key: getattr(after, key) for key in changed},
            },
        )
    if not set(before.bookmark_names).issubset(after.bookmark_names):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "Microsoft Word removed a review bookmark during field refresh",
        )


def finalize_word_fields(source_docx: Path, destination_docx: Path) -> WordFieldFinalizationReport:
    """Refresh supported live fields and publish one frozen, trackless copy."""

    if destination_docx.exists() or destination_docx.is_symlink():
        raise ContractError(ErrorCode.BACKEND_FAILED, "field output already exists")
    validate_review_layout(source_docx, field_refresh="pending")
    source_package = read_docx_package(source_docx)
    _validate_no_revision_xml(source_package)
    fields = _field_inventory(source_package)
    missing = _missing_reference_fields(source_package, fields)
    source_inspection = inspect_docx(source_docx)
    source_protection = _protected_inventory(source_docx, source_package)
    source_sha256 = source_package.file_sha256

    destination_docx.parent.mkdir(parents=True, exist_ok=True)
    run_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_docx.name}.word-run-",
            dir=destination_docx.parent,
        )
    )
    word_input = run_directory / "word-input.docx"
    working = run_directory / "word-working.docx"
    word_output = run_directory / "word-output.docx"
    report_path = run_directory / "word-report.json"
    pid_state = run_directory / "word-process.json"
    normalized_candidate = run_directory / "normalized.docx"
    sealed_candidate = run_directory / "sealed.docx"
    word_report: _WordReport | None = None
    duration_ms = 0
    try:
        word_input_sha256 = _prepare_word_input(source_docx, word_input)
        prepared_package = read_docx_package(word_input)
        if prepared_package.xml_root(SETTINGS_PART).find(_w("trackRevisions")) is not None:
            raise ContractError(
                ErrorCode.REVIEW_TRACKING_DISABLED,
                "Word field input still enables Track Changes",
            )
        raw_output = word_input
        if fields:
            word_report, duration_ms = _invoke_word(
                word_input,
                working,
                word_output,
                report_path,
                pid_state,
                expected_fields=len(fields),
                expected_unresolved=len(missing),
            )
            raw_output = word_output
            if (
                word_report.field_count != len(fields)
                or word_report.updated_fields != len(fields) - len(missing)
                or word_report.unresolved_fields != len(missing)
                or word_report.revision_count != 0
                or word_report.source_sha256 != word_input_sha256.removeprefix("sha256:")
            ):
                raise ContractError(ErrorCode.BACKEND_FAILED, "Word field evidence is inconsistent")

            word_package = read_docx_package(word_output)
            if word_report.output_sha256 != word_package.file_sha256.removeprefix("sha256:"):
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "Word field output hash differs",
                )
            _validate_no_revision_xml(word_package)
            if _field_inventory(word_package) != fields:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "Microsoft Word changed the protected field instructions",
                )
            word_inspection = inspect_docx(word_output)
            if word_report.bookmark_count != word_inspection.bookmarks:
                raise ContractError(
                    ErrorCode.BACKEND_FAILED,
                    "Word field bookmark evidence is inconsistent",
                )
            _compare_protected_content(
                source_protection,
                _protected_inventory(word_output, word_package),
            )
            _compare_structure(source_inspection, word_inspection)

        # Word may legally normalize away explicit paragraph/table geometry
        # while refreshing fields.  Reapply the deterministic review profile
        # to the controlled copy before freezing field state.
        apply_review_layout(raw_output, normalized_candidate)
        output_sha256 = _seal_fields(
            normalized_candidate,
            sealed_candidate,
            reference_styles=(
                source_package.xml_bytes(STYLES_PART)
                if STYLES_PART in source_package.part_names
                else None
            ),
        )
        final_package = read_docx_package(sealed_candidate)
        if _field_inventory(final_package) != fields:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "field sealing changed field codes",
            )
        _compare_protected_content(
            source_protection,
            _protected_inventory(sealed_candidate, final_package),
        )
        _compare_structure(source_inspection, inspect_docx(sealed_candidate))
        final_source = read_docx_package(source_docx)
        if final_source.file_sha256 != source_sha256:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "field source DOCX changed")

        final_report = WordFieldFinalizationReport(
            source_sha256=source_sha256,
            word_input_sha256=word_input_sha256,
            word_output_sha256=(
                None if word_report is None else "sha256:" + word_report.output_sha256
            ),
            output_sha256=output_sha256,
            word_automation_used=bool(fields),
            word_version=None if word_report is None else word_report.word_version,
            field_count=len(fields),
            updated_fields=len(fields) - len(missing),
            unresolved_fields=len(missing),
            duration_ms=duration_ms,
        )
        try:
            os.link(sealed_candidate, destination_docx, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "field output appeared during publication",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "field output cannot publish",
            ) from exc
        return final_report
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)


__all__ = [
    "WORD_FIELD_SCRIPT",
    "WordFieldFinalizationReport",
    "finalize_word_fields",
    "validate_frozen_field_state",
]
