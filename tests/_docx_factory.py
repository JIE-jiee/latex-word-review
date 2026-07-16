"""Small synthetic OPC packages for revision-reader security tests."""

from __future__ import annotations

import struct
import zipfile
from collections.abc import Mapping
from pathlib import Path

CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels"
    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

ROOT_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

MINIMAL_DOCUMENT = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p/></w:body>
</w:document>"""


def write_docx(
    path: Path,
    *,
    document_xml: bytes = MINIMAL_DOCUMENT,
    root_rels: bytes = ROOT_RELS,
    extra_parts: Mapping[str, bytes] | None = None,
    document_rels: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    duplicate_document: bool = False,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document_xml)
        if document_rels is not None:
            archive.writestr("word/_rels/document.xml.rels", document_rels)
        for name, data in (extra_parts or {}).items():
            archive.writestr(name, data)
        if duplicate_document:
            archive.writestr("word/document.xml", document_xml)
    return path


def patch_first_central_crc(path: Path) -> None:
    data = bytearray(path.read_bytes())
    offset = data.index(b"PK\x01\x02") + 16
    crc = struct.unpack_from("<I", data, offset)[0]
    struct.pack_into("<I", data, offset, crc ^ 1)
    path.write_bytes(data)


def mark_first_member_encrypted(path: Path) -> None:
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", data, central + 8)[0]
    struct.pack_into("<H", data, central + 8, flags | 1)
    path.write_bytes(data)
