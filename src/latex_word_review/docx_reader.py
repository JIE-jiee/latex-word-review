"""Fail-closed, read-only OPC/DOCX package reader.

The reader never extracts members to disk and never invokes Word, LibreOffice,
macros, embedded objects, or relationship targets.  It validates the complete
ZIP container and every XML part before revision ingestion can observe content.
"""

from __future__ import annotations

import io
import math
import posixpath
import re
import unicodedata
import zipfile
import zlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.canonical import sha256_bytes
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes

PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_RELATIONSHIP = f"{{{PACKAGE_REL_NS}}}Relationship"
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_XML_SUFFIXES = (".xml", ".rels")
_REQUIRED_PARTS = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True, slots=True)
class DocxReadLimits:
    """Resource limits applied before any package content is trusted."""

    max_file_bytes: int = 128 * 1024 * 1024
    max_entries: int = 4_096
    max_member_bytes: int = 64 * 1024 * 1024
    max_total_uncompressed_bytes: int = 256 * 1024 * 1024
    max_xml_bytes: int = 16 * 1024 * 1024
    max_xml_nodes: int = 1_000_000
    max_compression_ratio: float = 1_000.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_file_bytes,
            self.max_entries,
            self.max_member_bytes,
            self.max_total_uncompressed_bytes,
            self.max_xml_bytes,
            self.max_xml_nodes,
        )
        if any(value <= 0 for value in integer_limits):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "DOCX read limits must be positive")
        if not math.isfinite(self.max_compression_ratio) or self.max_compression_ratio <= 0:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "DOCX compression ratio limit must be finite and positive",
            )


@dataclass(frozen=True, slots=True)
class DocxPackage:
    """Validated in-memory evidence for a single immutable DOCX read."""

    source_name: str
    file_sha256: str
    size_bytes: int
    part_names: tuple[str, ...]
    entry_count: int
    compressed_bytes: int
    uncompressed_bytes: int
    _xml_parts: Mapping[str, bytes] = field(repr=False)
    _xml_sha256: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_xml_parts", MappingProxyType(dict(self._xml_parts)))
        object.__setattr__(self, "_xml_sha256", MappingProxyType(dict(self._xml_sha256)))

    def xml_bytes(self, part_uri: str) -> bytes:
        """Return immutable raw bytes for one validated XML part."""

        try:
            return self._xml_parts[part_uri]
        except KeyError as exc:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "requested XML part is absent from the validated package",
                details={"part_uri": part_uri},
            ) from exc

    def xml_sha256(self, part_uri: str) -> str:
        """Return the raw-byte SHA-256 of one validated XML part."""

        try:
            return self._xml_sha256[part_uri]
        except KeyError as exc:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "requested XML part hash is unavailable",
                details={"part_uri": part_uri},
            ) from exc

    def xml_root(self, part_uri: str) -> etree._Element:
        """Parse a fresh, non-networked tree from already validated bytes."""

        return _parse_xml(self.xml_bytes(part_uri), part_uri, max_nodes=None)

    @property
    def story_parts(self) -> tuple[str, ...]:
        """Return revision-bearing story parts in deterministic merge order."""

        return tuple(
            sorted((name for name in self.part_names if _is_story_part(name)), key=_story_key)
        )

    @property
    def comments_part(self) -> str | None:
        return "word/comments.xml" if "word/comments.xml" in self.part_names else None


def _translate_read_error(exc: ContractError, *, post_read: bool) -> ContractError:
    if post_read or exc.code is ErrorCode.HASH_SOURCE_MISMATCH:
        return ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned DOCX changed or became unreadable during evidence extraction",
        )
    return ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX cannot be read safely")


def _stable_docx_bytes(path: Path, limits: DocxReadLimits, *, post_read: bool) -> bytes:
    try:
        return read_stable_bytes(path, max_bytes=limits.max_file_bytes)
    except ContractError as exc:
        raise _translate_read_error(exc, post_read=post_read) from exc


def _decoded_member_name(name: str) -> str:
    if _PERCENT_ESCAPE.search(name):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid percent escape in OPC part")
    try:
        decoded = unquote(name, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE, "invalid UTF-8 escape in OPC part"
        ) from exc
    if unicodedata.normalize("NFC", decoded) != decoded:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "non-canonical Unicode OPC part name")
    return decoded


def _canonical_member_key(name: str, *, directory: bool = False) -> str:
    if not name or "\x00" in name or "\\" in name or "?" in name or "#" in name:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "unsafe OPC part name")
    if directory:
        if not name.endswith("/"):
            raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid OPC directory member")
        name = name[:-1]
    elif name.endswith("/"):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid OPC file member")
    if not name:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "empty OPC part name")

    raw = PurePosixPath(name)
    if raw.is_absolute() or raw.as_posix() != name:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "non-canonical OPC part path")
    if any(part in {"", ".", ".."} or ":" in part for part in raw.parts):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "unsafe OPC part path segment")

    decoded = _decoded_member_name(name)
    decoded_path = PurePosixPath(decoded)
    if decoded_path.is_absolute() or any(
        part in {"", ".", ".."} or ":" in part for part in decoded_path.parts
    ):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "encoded unsafe OPC part path")
    if "\\" in decoded or "\x00" in decoded:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "encoded unsafe OPC separator")
    return decoded.casefold()


def _validate_members(
    archive: zipfile.ZipFile,
    limits: DocxReadLimits,
) -> tuple[tuple[str, ...], int, int]:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX has too many ZIP members")

    exact_counts = Counter(info.filename for info in infos)
    if any(count > 1 for count in exact_counts.values()):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX contains duplicate ZIP members")

    canonical_keys: dict[str, str] = {}
    total_uncompressed = 0
    compressed_bytes = 0
    file_names: list[str] = []
    for info in infos:
        key = _canonical_member_key(info.filename, directory=info.is_dir())
        prior = canonical_keys.get(key)
        if prior is not None:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "DOCX contains canonically duplicate ZIP members",
                details={"first": prior, "second": info.filename},
            )
        canonical_keys[key] = info.filename
        if info.flag_bits & 0x1:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE, "encrypted ZIP members are forbidden"
            )
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "DOCX uses an unsupported ZIP compression method",
            )
        if info.file_size > limits.max_member_bytes:
            raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX member exceeds size limit")
        if info.filename.lower().endswith(_XML_SUFFIXES) and info.file_size > limits.max_xml_bytes:
            raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX XML part exceeds size limit")
        total_uncompressed += info.file_size
        compressed_bytes += info.compress_size
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE, "DOCX expands beyond total size limit"
            )
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > limits.max_compression_ratio:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE, "DOCX member compression ratio is unsafe"
            )
        if info.is_dir():
            if info.file_size or info.compress_size:
                raise ContractError(
                    ErrorCode.DOCX_INVALID_PACKAGE, "non-empty ZIP directory member"
                )
        else:
            file_names.append(info.filename)

    bad_member = archive.testzip()
    if bad_member is not None:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "DOCX ZIP CRC validation failed")
    names = tuple(sorted(file_names))
    missing = sorted(_REQUIRED_PARTS.difference(names))
    if missing:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "DOCX is missing required OPC parts",
            details={"missing": missing},
        )
    return names, compressed_bytes, total_uncompressed


def _parse_xml(data: bytes, part_uri: str, *, max_nodes: int | None) -> etree._Element:
    declaration_scan = data.upper().replace(b"\x00", b"")
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "DTD and entity declarations are forbidden in DOCX XML",
            details={"part_uri": part_uri},
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "DOCX contains invalid XML",
            details={"part_uri": part_uri},
        ) from exc
    if max_nodes is not None and sum(1 for _ in root.iter()) > max_nodes:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "DOCX XML part exceeds node limit",
            details={"part_uri": part_uri},
        )
    return root


def _relationship_source_part(rels_part: str) -> str:
    if rels_part == "_rels/.rels":
        return ""
    marker = "/_rels/"
    if marker not in rels_part or not rels_part.endswith(".rels"):
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid relationship part name")
    parent, filename = rels_part.rsplit(marker, 1)
    source_filename = filename.removesuffix(".rels")
    if not source_filename:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid relationship source")
    return f"{parent}/{source_filename}"


def _resolve_internal_target(source_part: str, target: str) -> str:
    if not target or "\x00" in target or "\\" in target:
        raise ContractError(ErrorCode.DOCX_UNSAFE_RELATIONSHIP, "unsafe OPC relationship target")
    split = urlsplit(target)
    if split.scheme or split.netloc or split.query:
        raise ContractError(
            ErrorCode.DOCX_UNSAFE_RELATIONSHIP, "external relationship is forbidden"
        )
    try:
        decoded_path = unquote(split.path, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError(
            ErrorCode.DOCX_UNSAFE_RELATIONSHIP,
            "invalid UTF-8 escape in OPC relationship target",
        ) from exc
    if not decoded_path and split.fragment:
        return source_part
    if decoded_path.startswith("/"):
        resolved = posixpath.normpath(decoded_path.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), decoded_path))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise ContractError(
            ErrorCode.DOCX_UNSAFE_RELATIONSHIP,
            "relationship target escapes the OPC package",
        )
    _canonical_member_key(resolved)
    return resolved


def _validate_relationships(
    xml_parts: Mapping[str, bytes],
    part_names: tuple[str, ...],
    limits: DocxReadLimits,
) -> None:
    part_keys = {_canonical_member_key(name): name for name in part_names}
    for rels_part in sorted(name for name in xml_parts if name.endswith(".rels")):
        source_part = _relationship_source_part(rels_part)
        root = _parse_xml(xml_parts[rels_part], rels_part, max_nodes=limits.max_xml_nodes)
        seen_ids: set[str] = set()
        for relationship in root.iter(_RELATIONSHIP):
            relationship_id = relationship.get("Id")
            target = relationship.get("Target")
            target_mode = relationship.get("TargetMode")
            if not relationship_id or relationship_id in seen_ids or not target:
                raise ContractError(
                    ErrorCode.DOCX_INVALID_PACKAGE,
                    "relationship is missing a unique Id or Target",
                    details={"part_uri": rels_part},
                )
            seen_ids.add(relationship_id)
            if target_mode is not None and target_mode.casefold() != "internal":
                raise ContractError(
                    ErrorCode.DOCX_UNSAFE_RELATIONSHIP,
                    "external OPC relationship is forbidden",
                    details={"part_uri": rels_part, "relationship_id": relationship_id},
                )
            resolved = _resolve_internal_target(source_part, target)
            if _canonical_member_key(resolved) not in part_keys:
                raise ContractError(
                    ErrorCode.DOCX_INVALID_PACKAGE,
                    "relationship target is missing from the OPC package",
                    details={"part_uri": rels_part, "relationship_id": relationship_id},
                )


def _is_story_part(part_uri: str) -> bool:
    if part_uri in {"word/document.xml", "word/footnotes.xml", "word/endnotes.xml"}:
        return True
    if not part_uri.endswith(".xml") or "/" in part_uri.removeprefix("word/"):
        return False
    filename = part_uri.removeprefix("word/")
    return filename.startswith("header") or filename.startswith("footer")


def _story_key(part_uri: str) -> tuple[int, str]:
    if part_uri == "word/document.xml":
        return 0, part_uri
    if part_uri.startswith("word/header"):
        return 1, part_uri
    if part_uri.startswith("word/footer"):
        return 2, part_uri
    if part_uri == "word/footnotes.xml":
        return 3, part_uri
    if part_uri == "word/endnotes.xml":
        return 4, part_uri
    return 5, part_uri


def read_docx_package(
    path: str | Path,
    *,
    limits: DocxReadLimits | None = None,
) -> DocxPackage:
    """Validate and read one DOCX without writing or mutating any input.

    The source is read and hashed again after package validation.  Any byte or
    readability change raises ``E_HASH_RETURNED_ORIGINAL_MISMATCH``.
    """

    resolved_limits = limits or DocxReadLimits()
    source = Path(path)
    if source.suffix.lower() != ".docx":
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "input must have a .docx suffix")
    initial_bytes = _stable_docx_bytes(source, resolved_limits, post_read=False)
    initial_sha256 = sha256_bytes(initial_bytes)

    try:
        with zipfile.ZipFile(io.BytesIO(initial_bytes), mode="r") as archive:
            part_names, compressed_bytes, uncompressed_bytes = _validate_members(
                archive, resolved_limits
            )
            xml_parts: dict[str, bytes] = {}
            xml_sha256: dict[str, str] = {}
            for part_uri in part_names:
                if not part_uri.lower().endswith(_XML_SUFFIXES):
                    continue
                data = archive.read(part_uri)
                _parse_xml(data, part_uri, max_nodes=resolved_limits.max_xml_nodes)
                xml_parts[part_uri] = data
                xml_sha256[part_uri] = sha256_bytes(data)
            _validate_relationships(xml_parts, part_names, resolved_limits)
    except ContractError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError, zlib.error) as exc:
        raise ContractError(ErrorCode.DOCX_INVALID_PACKAGE, "invalid DOCX ZIP container") from exc

    final_bytes = _stable_docx_bytes(source, resolved_limits, post_read=True)
    final_sha256 = sha256_bytes(final_bytes)
    if final_sha256 != initial_sha256:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned DOCX changed during evidence extraction",
        )
    return DocxPackage(
        source_name=source.name,
        file_sha256=initial_sha256,
        size_bytes=len(initial_bytes),
        part_names=part_names,
        entry_count=len(part_names),
        compressed_bytes=compressed_bytes,
        uncompressed_bytes=uncompressed_bytes,
        _xml_parts=xml_parts,
        _xml_sha256=xml_sha256,
    )


__all__ = ["DocxPackage", "DocxReadLimits", "read_docx_package"]
