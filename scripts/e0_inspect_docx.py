#!/usr/bin/env python3
"""Read-only DOCX structure observer for E0 contract experiments.

This script is deliberately independent of candidate revision libraries.  It
provides a small, deterministic OOXML oracle for public fixtures and writes
JSON to stdout.  It is development tooling, not the production ingest path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

NS = {"w": W, "m": M, "r": R, "pr": PKG_REL}

REVISION_TAGS = {
    f"{{{W}}}ins": "insert",
    f"{{{W}}}del": "delete",
    f"{{{W}}}moveFrom": "move_from",
    f"{{{W}}}moveTo": "move_to",
    f"{{{W}}}rPrChange": "run_format",
    f"{{{W}}}pPrChange": "paragraph_format",
    f"{{{W}}}sectPrChange": "section_format",
    f"{{{W}}}tblPrChange": "table_format",
    f"{{{W}}}trPrChange": "row_format",
    f"{{{W}}}tcPrChange": "cell_format",
}

STORY_PART_PREFIXES = (
    "word/header",
    "word/footer",
)
STORY_PART_NAMES = {
    "word/document.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
}
ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class ObservationError(RuntimeError):
    """Raised when a DOCX is unsafe or structurally unreadable."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_member_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return False
    candidate = name[:-1] if name.endswith("/") else name
    if not candidate:
        return False
    pure = PurePosixPath(candidate)
    if not pure.parts or pure.is_absolute():
        return False
    if any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        return False
    return pure.as_posix() == candidate


def is_story_part(part: str) -> bool:
    if part in STORY_PART_NAMES:
        return True
    return part.endswith(".xml") and any(
        part.startswith(prefix) and "/" not in part[len(prefix) :] for prefix in STORY_PART_PREFIXES
    )


def validate_archive(
    archive: zipfile.ZipFile,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
    max_xml_bytes: int,
    max_ratio: float,
) -> dict[str, Any]:
    infos = archive.infolist()
    if len(infos) > max_entries:
        raise ObservationError(f"archive has {len(infos)} entries; limit is {max_entries}")

    names = [info.filename for info in infos]
    duplicate_names = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicate_names:
        raise ObservationError(f"archive contains duplicate member: {duplicate_names[0]!r}")

    total_uncompressed = 0
    suspicious: list[str] = []
    for info in infos:
        if not safe_member_name(info.filename):
            raise ObservationError(f"unsafe archive member: {info.filename!r}")
        if info.flag_bits & 0x1:
            raise ObservationError(f"encrypted archive member is forbidden: {info.filename!r}")
        if info.compress_type not in ALLOWED_COMPRESSION:
            raise ObservationError(
                f"unsupported compression method {info.compress_type} for {info.filename!r}"
            )
        total_uncompressed += info.file_size
        if total_uncompressed > max_uncompressed_bytes:
            raise ObservationError(f"archive expands beyond {max_uncompressed_bytes} bytes")
        if info.filename.lower().endswith((".xml", ".rels")) and info.file_size > max_xml_bytes:
            raise ObservationError(f"XML part {info.filename!r} exceeds {max_xml_bytes} bytes")
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > max_ratio:
            suspicious.append(info.filename)

    if suspicious:
        raise ObservationError(
            f"archive member compression ratio exceeds {max_ratio}: {suspicious[0]!r}"
        )

    bad_member = archive.testzip()
    if bad_member is not None:
        raise ObservationError(f"archive CRC check failed for {bad_member!r}")

    return {
        "entry_count": len(infos),
        "compressed_bytes": sum(item.compress_size for item in infos),
        "uncompressed_bytes": total_uncompressed,
    }


def read_xml(archive: zipfile.ZipFile, part: str) -> tuple[ET.Element, bytes]:
    data = archive.read(part)
    # Scan the complete, size-limited part. Removing NUL bytes also catches the
    # ASCII declaration tokens in ordinary UTF-16/UTF-32 XML encodings.
    upper = data.upper().replace(b"\x00", b"")
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ObservationError(f"DTD or entity declaration is forbidden in {part}")
    try:
        return ET.fromstring(data), data
    except ET.ParseError as exc:
        raise ObservationError(f"invalid XML in {part}: {exc}") from exc


def attr(element: ET.Element, local_name: str) -> str | None:
    return element.attrib.get(f"{{{W}}}{local_name}")


def on_off_property(rpr: ET.Element | None, local_name: str) -> bool:
    if rpr is None:
        return False
    node = rpr.find(f"{{{W}}}{local_name}")
    if node is None:
        return False
    value = attr(node, "val")
    return value is None or value.strip().lower() not in {"0", "false", "off", "no"}


def element_text(element: ET.Element, *, deleted: bool = False) -> str:
    tags = {f"{{{W}}}delText"} if deleted else {f"{{{W}}}t", f"{{{W}}}tab", f"{{{W}}}br"}
    chunks: list[str] = []
    for child in element.iter():
        if child.tag not in tags:
            continue
        if child.tag == f"{{{W}}}tab":
            chunks.append("\t")
        elif child.tag == f"{{{W}}}br":
            chunks.append("\n")
        else:
            chunks.append(child.text or "")
    return "".join(chunks)


def observe_story(part: str, root: ET.Element) -> dict[str, Any]:
    revisions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    parents = {child: parent for parent in root.iter() for child in parent}
    for element in root.iter():
        kind = REVISION_TAGS.get(element.tag)
        if kind is None:
            continue
        counts[kind] += 1
        # Move sources use ordinary w:t; the enclosing w:moveFrom supplies the
        # revision semantics. Only true w:del content uses w:delText.
        is_deleted = kind == "delete"
        event = {
            "kind": kind,
            "id": attr(element, "id"),
            "author": attr(element, "author"),
            "date": attr(element, "date"),
            "text": element_text(element, deleted=is_deleted),
        }
        if kind == "run_format":
            current_rpr = parents.get(element)
            run = parents.get(current_rpr) if current_rpr is not None else None
            if run is not None and run.tag == f"{{{W}}}r":
                event["text"] = element_text(run)
            prior_rpr = element.find(f"{{{W}}}rPr")
            event["format_before"] = {"bold": on_off_property(prior_rpr, "b")}
            event["format_after"] = {"bold": on_off_property(current_rpr, "b")}
        revisions.append(event)

    bookmark_names = sorted(
        value
        for value in (
            node.attrib.get(f"{{{W}}}name") for node in root.iter(f"{{{W}}}bookmarkStart")
        )
        if value
    )
    field_instructions = [
        " ".join((node.text or "").split())
        for node in root.iter(f"{{{W}}}instrText")
        if (node.text or "").strip()
    ]

    return {
        "part": part,
        "paragraphs": sum(1 for _ in root.iter(f"{{{W}}}p")),
        "tables": sum(1 for _ in root.iter(f"{{{W}}}tbl")),
        "math_objects": sum(1 for _ in root.iter(f"{{{M}}}oMath")),
        "math_paragraphs": sum(1 for _ in root.iter(f"{{{M}}}oMathPara")),
        "bookmark_names": bookmark_names,
        "field_instructions": field_instructions,
        "revision_counts": dict(sorted(counts.items())),
        "revisions": revisions,
        "comment_anchors": {
            "start": [attr(node, "id") for node in root.iter(f"{{{W}}}commentRangeStart")],
            "end": [attr(node, "id") for node in root.iter(f"{{{W}}}commentRangeEnd")],
            "reference": [attr(node, "id") for node in root.iter(f"{{{W}}}commentReference")],
        },
    }


def observe_comments(root: ET.Element) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for node in root.iter(f"{{{W}}}comment"):
        comments.append(
            {
                "id": attr(node, "id"),
                "author": attr(node, "author"),
                "initials": attr(node, "initials"),
                "date": attr(node, "date"),
                "text": element_text(node),
            }
        )
    return comments


def observe_relationships(archive: zipfile.ZipFile) -> dict[str, Any]:
    external: list[dict[str, str | None]] = []
    types: Counter[str] = Counter()
    for name in sorted(archive.namelist()):
        if not name.endswith(".rels"):
            continue
        root, _ = read_xml(archive, name)
        for relationship in root.iter(f"{{{PKG_REL}}}Relationship"):
            rel_type = relationship.attrib.get("Type", "")
            types[rel_type] += 1
            if relationship.attrib.get("TargetMode") == "External":
                external.append(
                    {
                        "part": name,
                        "id": relationship.attrib.get("Id"),
                        "type": rel_type,
                        "target": relationship.attrib.get("Target"),
                    }
                )
    return {"type_counts": dict(sorted(types.items())), "external": external}


def inspect_docx(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    if path.suffix.lower() != ".docx":
        raise ObservationError("input must use the .docx extension")
    if not path.is_file():
        raise ObservationError(f"input does not exist: {path}")

    with zipfile.ZipFile(path) as archive:
        archive_summary = validate_archive(
            archive,
            max_entries=args.max_entries,
            max_uncompressed_bytes=args.max_uncompressed_bytes,
            max_xml_bytes=args.max_xml_bytes,
            max_ratio=args.max_ratio,
        )
        names = set(archive.namelist())
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ObservationError("input is not a minimal WordprocessingML package")

        stories: list[dict[str, Any]] = []
        for part in sorted(names):
            if not is_story_part(part):
                continue
            root, _ = read_xml(archive, part)
            stories.append(observe_story(part, root))

        comments: list[dict[str, Any]] = []
        if "word/comments.xml" in names:
            comments_root, _ = read_xml(archive, "word/comments.xml")
            comments = observe_comments(comments_root)

        return {
            "schema_version": "e0-docx-observation-v1",
            "input": {"name": path.name, "sha256": sha256_file(path)},
            "archive": archive_summary,
            "parts": sorted(names),
            "stories": stories,
            "comments": comments,
            "relationships": observe_relationships(archive),
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--max-entries", type=int, default=5_000)
    parser.add_argument("--max-uncompressed-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--max-xml-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--max-ratio", type=float, default=1_000.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.max_entries <= 0 or args.max_uncompressed_bytes <= 0 or args.max_xml_bytes <= 0:
        print(json.dumps({"error": "archive limits must be positive"}), file=sys.stderr)
        return 2
    if args.max_ratio <= 0:
        print(json.dumps({"error": "compression ratio limit must be positive"}), file=sys.stderr)
        return 2
    try:
        result = inspect_docx(args.docx.resolve(), args)
    except (ObservationError, zipfile.BadZipFile, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
