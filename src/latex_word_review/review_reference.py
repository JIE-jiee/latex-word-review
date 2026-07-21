# ruff: noqa: E501 - fixed OOXML literals intentionally keep one element per line
"""Deterministic built-in Word style profile for editable review handoffs.

The project deliberately does not ship a binary DOCX template.  Instead this
module builds the tiny reference package from fixed XML and materializes it
only inside a worker-owned staging directory.  This keeps the wheel text-only,
gives the backend an exact configuration hash, and avoids trusting arbitrary
user-supplied Office packages.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final
from xml.etree import ElementTree

from latex_word_review.canonical import sha256_canonical

PROFILE_ID: Final[str] = "academic-review-v1"
REFERENCE_DOCX_SHA256: Final[str] = (
    "4aafefa263401aedb82c556ab08db2971b4dce950c4f2a62c04378b0afed4127"
)
REFERENCE_CONFIG_SHA256: Final[str] = sha256_canonical(
    {
        "cjk_font": "SimSun",
        "latin_font": "Times New Roman",
        "line_spacing_twips": 320,
        "margins_twips": 1440,
        "page": "A4-portrait",
        "profile": PROFILE_ID,
        "zip": "stored-fixed-metadata-v1",
    }
)

_CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_PACKAGE_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOCUMENT_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_DOCUMENT = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p/>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
      <w:cols w:num="1" w:space="720"/>
    </w:sectPr>
  </w:body>
</w:document>"""

_STYLES = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr>
      <w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="SimSun" w:cs="Times New Roman"/>
      <w:sz w:val="22"/><w:szCs w:val="22"/>
      <w:lang w:val="en-US" w:eastAsia="zh-CN"/>
    </w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr>
      <w:widowControl/><w:spacing w:after="120" w:line="320" w:lineRule="auto"/><w:jc w:val="both"/>
    </w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/><w:qFormat/>
    <w:pPr><w:widowControl/><w:spacing w:after="120" w:line="320" w:lineRule="auto"/><w:jc w:val="both"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:after="200"/><w:jc w:val="center"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="172B4D"/><w:sz w:val="44"/><w:szCs w:val="44"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="0" w:after="0" w:line="276" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="566573"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="320" w:after="160"/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="1"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="180" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading4">
    <w:name w:val="heading 4"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="160" w:after="80"/><w:outlineLvl w:val="3"/></w:pPr>
    <w:rPr><w:b/><w:i/><w:color w:val="1F4E79"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading5">
    <w:name w:val="heading 5"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="120" w:after="60"/><w:outlineLvl w:val="4"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Caption">
    <w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="60" w:after="100" w:line="276" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>
    <w:rPr><w:color w:val="404040"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Quote">
    <w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:ind w:left="648" w:right="648"/><w:spacing w:after="120"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="404040"/><w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="SourceCode">
    <w:name w:val="Source Code"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:shd w:val="clear" w:fill="F5F7FA"/><w:spacing w:after="80" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="SimSun" w:cs="Consolas"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Abstract">
    <w:name w:val="Abstract"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:ind w:left="504" w:right="504"/><w:spacing w:after="160" w:line="288" w:lineRule="auto"/><w:jc w:val="both"/></w:pPr>
    <w:rPr><w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Bibliography">
    <w:name w:val="Bibliography"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:after="60" w:line="276" w:lineRule="auto"/><w:ind w:left="360" w:hanging="360"/><w:jc w:val="left"/></w:pPr>
    <w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>
  </w:style>
  <w:style w:type="character" w:styleId="Hyperlink">
    <w:name w:val="Hyperlink"/><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="FootnoteText">
    <w:name w:val="footnote text"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr>
    <w:rPr><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
  </w:style>
  <w:style w:type="character" w:styleId="FootnoteReference">
    <w:name w:val="footnote reference"/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>
  </w:style>
</w:styles>"""

_EXPECTED_MEMBERS: Final[tuple[str, ...]] = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/_rels/document.xml.rels",
    "word/document.xml",
    "word/styles.xml",
)
_FIXED_ZIP_TIME: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
_MAX_BYTES: Final[int] = 64 * 1024
_W_NS: Final[str] = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS: Final[str] = "http://schemas.openxmlformats.org/package/2006/relationships"


def _build_unchecked() -> bytes:
    members = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _PACKAGE_RELS,
        "word/_rels/document.xml.rels": _DOCUMENT_RELS,
        "word/document.xml": _DOCUMENT,
        "word/styles.xml": _STYLES,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, members[name])
        archive.comment = b""
    return output.getvalue()


def _validate_structure(data: bytes) -> None:
    if not 0 < len(data) <= _MAX_BYTES:
        raise RuntimeError("built-in review reference exceeds its size boundary")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = tuple(info.filename for info in infos)
        if names != tuple(sorted(_EXPECTED_MEMBERS)) or len(names) != len(set(names)):
            raise RuntimeError("built-in review reference has an invalid member set")
        if any(
            info.compress_type != zipfile.ZIP_STORED
            or info.date_time != _FIXED_ZIP_TIME
            or info.flag_bits & 0x1
            for info in infos
        ):
            raise RuntimeError("built-in review reference has unstable ZIP metadata")
        xml = {name: ElementTree.fromstring(archive.read(name)) for name in names}
    for name in ("_rels/.rels", "word/_rels/document.xml.rels"):
        if any(
            relationship.get("TargetMode") == "External"
            for relationship in xml[name].findall(f"{{{_REL_NS}}}Relationship")
        ):
            raise RuntimeError("built-in review reference contains an external relationship")
    styles = {
        style.get(f"{{{_W_NS}}}styleId")
        for style in xml["word/styles.xml"].findall(f"{{{_W_NS}}}style")
    }
    required_styles = {
        "Normal",
        "Title",
        "Subtitle",
        "Heading1",
        "Heading2",
        "Heading3",
        "Heading4",
        "Heading5",
        "Caption",
        "Quote",
        "SourceCode",
        "Abstract",
        "Bibliography",
        "Hyperlink",
        "FootnoteText",
        "FootnoteReference",
    }
    if not required_styles.issubset(styles):
        raise RuntimeError("built-in review reference is missing required styles")
    section = xml["word/document.xml"].find(f".//{{{_W_NS}}}sectPr")
    page = None if section is None else section.find(f"{{{_W_NS}}}pgSz")
    margins = None if section is None else section.find(f"{{{_W_NS}}}pgMar")
    if (
        page is None
        or margins is None
        or page.get(f"{{{_W_NS}}}w") != "11906"
        or page.get(f"{{{_W_NS}}}h") != "16838"
        or any(
            margins.get(f"{{{_W_NS}}}{side}") != "1440"
            for side in ("top", "right", "bottom", "left")
        )
    ):
        raise RuntimeError("built-in review reference has unexpected page geometry")


def verify_review_reference_bytes(
    data: bytes, *, expected_sha256: str = REFERENCE_DOCX_SHA256
) -> None:
    _validate_structure(data)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise RuntimeError("built-in review reference hash mismatch")


def build_review_reference_bytes() -> bytes:
    data = _build_unchecked()
    verify_review_reference_bytes(data)
    return data


@contextmanager
def materialized_review_reference(directory: Path) -> Iterator[Path]:
    parent = directory.resolve(strict=True)
    if not parent.is_dir():
        raise RuntimeError("review reference parent is not a directory")
    descriptor, name = tempfile.mkstemp(
        prefix=".lwr-review-reference-",
        suffix=".docx",
        dir=parent,
    )
    path = Path(name)
    try:
        data = build_review_reference_bytes()
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        verify_review_reference_bytes(path.read_bytes())
        yield path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            path.unlink()


__all__ = [
    "PROFILE_ID",
    "REFERENCE_CONFIG_SHA256",
    "REFERENCE_DOCX_SHA256",
    "build_review_reference_bytes",
    "materialized_review_reference",
    "verify_review_reference_bytes",
]
