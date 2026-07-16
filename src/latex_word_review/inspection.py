"""Read-only DOCX structure acceptance built on the canonical S3 reader."""

from __future__ import annotations

import io
import math
import re
import zipfile
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.canonical import sha256_bytes
from latex_word_review.docx_reader import DocxReadLimits, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import ExportFinding, ExportValidation
from latex_word_review.hashing import read_stable_bytes

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_RELATIONSHIP = f"{{{PKG_REL_NS}}}Relationship"
_FIELD_RE = re.compile(r"^\s*(PAGEREF|SEQ|REF)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _RelationshipInventory:
    file_sha256: str | None = None
    relationships: int = 0
    external_relationships: int = 0
    image_relationships: int = 0


@dataclass(frozen=True, slots=True)
class DocxInspection:
    package_valid: bool
    structure_inspected: bool
    file_sha256: str | None
    body_paragraphs: int
    paragraphs: int
    omml_objects: int
    omml_paragraphs: int
    images: int
    image_instances: int
    tables: int
    bookmarks: int
    bookmark_names: tuple[str, ...]
    seq_fields: int
    ref_fields: int
    pageref_fields: int
    relationships: int
    external_relationships: int
    findings: tuple[ExportFinding, ...]
    validation: ExportValidation

    @property
    def live_fields(self) -> int:
        return self.seq_fields + self.ref_fields + self.pageref_fields

    def as_metrics(self) -> dict[str, int]:
        return {
            "body_paragraphs": self.body_paragraphs,
            "paragraphs": self.paragraphs,
            "omml_objects": self.omml_objects,
            "omml_paragraphs": self.omml_paragraphs,
            "images": self.images,
            "image_instances": self.image_instances,
            "tables": self.tables,
            "bookmarks": self.bookmarks,
            "seq_fields": self.seq_fields,
            "ref_fields": self.ref_fields,
            "pageref_fields": self.pageref_fields,
            "live_fields": self.live_fields,
            "relationships": self.relationships,
            "external_relationships": self.external_relationships,
        }


def _safe_xml(data: bytes) -> etree._Element:
    declaration_scan = data.upper().replace(b"\x00", b"")
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "unsafe XML declaration")
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )
    try:
        return etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid relationship XML") from exc


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(
        name
        and "\x00" not in name
        and "\\" not in name
        and not path.is_absolute()
        and path.as_posix() == name
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _relationship_inventory(path: Path, limits: DocxReadLimits) -> _RelationshipInventory:
    data = read_stable_bytes(path, max_bytes=limits.max_file_bytes)
    file_sha256 = sha256_bytes(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_entries:
                raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "too many ZIP members")
            if any(count > 1 for count in Counter(info.filename for info in infos).values()):
                raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "duplicate ZIP members")
            total = 0
            for info in infos:
                if not _safe_member_name(info.filename.rstrip("/")):
                    raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "unsafe ZIP member name")
                if info.flag_bits & 0x1 or info.file_size > limits.max_member_bytes:
                    raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "unsafe ZIP member")
                total += info.file_size
                if total > limits.max_total_uncompressed_bytes:
                    raise ContractError(
                        ErrorCode.DOCX_INVALID_PACKAGE,
                        "ZIP expansion is too large",
                    )
                ratio = info.file_size / max(info.compress_size, 1)
                if not math.isfinite(ratio) or ratio > limits.max_compression_ratio:
                    raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "unsafe compression ratio")
            relationships = 0
            external = 0
            image_relationships = 0
            for info in infos:
                if not info.filename.endswith(".rels") or info.is_dir():
                    continue
                if info.file_size > limits.max_xml_bytes:
                    raise ContractError(
                        ErrorCode.DOCX_INVALID_PACKAGE,
                        "relationship XML is too large",
                    )
                root = _safe_xml(archive.read(info.filename))
                if sum(1 for _ in root.iter()) > limits.max_xml_nodes:
                    raise ContractError(
                        ErrorCode.DOCX_INVALID_PACKAGE,
                        "relationship XML is too deep",
                    )
                for relationship in root.iter(_RELATIONSHIP):
                    relationships += 1
                    target = relationship.get("Target", "")
                    mode = relationship.get("TargetMode", "")
                    split = urlsplit(target)
                    if mode.casefold() not in {"", "internal"} or split.scheme or split.netloc:
                        external += 1
                    if relationship.get("Type", "").rstrip("/").endswith("/image"):
                        image_relationships += 1
            return _RelationshipInventory(
                file_sha256=file_sha256,
                relationships=relationships,
                external_relationships=external,
                image_relationships=image_relationships,
            )
    except ContractError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as exc:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid DOCX ZIP") from exc


def _field_counts(root: etree._Element) -> tuple[int, int, int]:
    instructions: list[str] = []
    for element in root.iter(f"{{{W_NS}}}instrText"):
        if element.text:
            instructions.append(element.text)
    for element in root.iter(f"{{{W_NS}}}fldSimple"):
        instruction = element.get(f"{{{W_NS}}}instr")
        if instruction:
            instructions.append(instruction)
    counts = Counter[str]()
    for instruction in instructions:
        match = _FIELD_RE.match(instruction)
        if match is not None:
            counts[match.group(1).upper()] += 1
    return counts["SEQ"], counts["REF"], counts["PAGEREF"]


def inspect_docx(path: str | Path, *, limits: DocxReadLimits | None = None) -> DocxInspection:
    """Inspect without following relationships, extracting files, or invoking Office."""

    resolved_limits = limits or DocxReadLimits()
    source = Path(path)
    inventory = _RelationshipInventory()
    with suppress(ContractError):
        inventory = _relationship_inventory(source, resolved_limits)
    try:
        package = read_docx_package(source, limits=resolved_limits)
    except ContractError as exc:
        finding = ExportFinding(
            code=exc.code,
            severity="error",
            phase="inspect",
            message=(
                "DOCX contains an external or unsafe relationship"
                if exc.code is ErrorCode.DOCX_UNSAFE_RELATIONSHIP
                else "DOCX failed safe package validation"
            ),
            recoverable=False,
            fingerprint=inventory.file_sha256,
            remediation="remove unsafe relationships or regenerate the review DOCX",
        )
        return DocxInspection(
            package_valid=False,
            structure_inspected=False,
            file_sha256=inventory.file_sha256,
            body_paragraphs=0,
            paragraphs=0,
            omml_objects=0,
            omml_paragraphs=0,
            images=inventory.image_relationships,
            image_instances=0,
            tables=0,
            bookmarks=0,
            bookmark_names=(),
            seq_fields=0,
            ref_fields=0,
            pageref_fields=0,
            relationships=inventory.relationships,
            external_relationships=inventory.external_relationships,
            findings=(finding,),
            validation=ExportValidation(
                ooxml="blocked",
                openability="not_run",
                relationships=(
                    "fail" if exc.code is ErrorCode.DOCX_UNSAFE_RELATIONSHIP else "blocked"
                ),
                structure="blocked",
            ),
        )

    root = package.xml_root("word/document.xml")
    body = root.find(f"{{{W_NS}}}body")
    body_paragraphs = 0 if body is None else len(body.findall(f"{{{W_NS}}}p"))
    paragraphs = sum(1 for _ in root.iter(f"{{{W_NS}}}p"))
    omml_objects = sum(1 for _ in root.iter(f"{{{M_NS}}}oMath"))
    omml_paragraphs = sum(1 for _ in root.iter(f"{{{M_NS}}}oMathPara"))
    tables = sum(1 for _ in root.iter(f"{{{W_NS}}}tbl"))
    bookmarks_elements = tuple(root.iter(f"{{{W_NS}}}bookmarkStart"))
    bookmark_names = tuple(
        sorted(
            name
            for element in bookmarks_elements
            if (name := element.get(f"{{{W_NS}}}name")) is not None
        )
    )
    image_instances = sum(1 for _ in root.iter(f"{{{A_NS}}}blip"))
    seq_fields, ref_fields, pageref_fields = _field_counts(root)
    return DocxInspection(
        package_valid=True,
        structure_inspected=True,
        file_sha256=package.file_sha256,
        body_paragraphs=body_paragraphs,
        paragraphs=paragraphs,
        omml_objects=omml_objects,
        omml_paragraphs=omml_paragraphs,
        images=inventory.image_relationships,
        image_instances=image_instances,
        tables=tables,
        bookmarks=len(bookmarks_elements),
        bookmark_names=bookmark_names,
        seq_fields=seq_fields,
        ref_fields=ref_fields,
        pageref_fields=pageref_fields,
        relationships=inventory.relationships,
        external_relationships=inventory.external_relationships,
        findings=(),
        validation=ExportValidation(
            ooxml="pass",
            openability="not_run",
            relationships="pass",
            structure="pass",
        ),
    )


__all__ = ["DocxInspection", "inspect_docx"]
