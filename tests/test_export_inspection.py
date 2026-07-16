"""DOCX inspection and never-guess bookmark mapping tests."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from latex_word_review.discovery import discover_project
from latex_word_review.errors import ErrorCode
from latex_word_review.export import anchor_source_units
from latex_word_review.inspection import inspect_docx
from latex_word_review.source_units import scan_source_units

FIXTURE_ROOT = Path(__file__).parent / "fixtures/e0-minimal-paper"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" ContentType="application/vnd.'
        b'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )
    root_rels = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/officeDocument" '
        f'Target="word/document.xml"/></Relationships>'
    ).encode()
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    ).encode()
    document_rels = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_REL_NS}"/>'
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_rels)


def _write_project(path: Path, paragraphs: list[str]) -> None:
    path.mkdir()
    body = "\n\n".join(paragraphs)
    (path / "main.tex").write_text(
        f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n",
        encoding="utf-8",
        newline="\n",
    )


def _add_external_relationship(source: Path, destination: Path) -> None:
    raw = source.read_bytes()
    with (
        zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive,
        zipfile.ZipFile(destination, mode="w") as output,
    ):
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "word/_rels/document.xml.rels":
                relationship = (
                    b'<Relationship Id="rIdExternalTest" '
                    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    b'relationships/hyperlink" Target="https://example.invalid/" '
                    b'TargetMode="External"/>'
                )
                data = data.replace(b"</Relationships>", relationship + b"</Relationships>")
            output.writestr(info, data)


def test_fixture_structure_counts_are_read_only_and_exact() -> None:
    base = inspect_docx(FIXTURE_ROOT / "base/review-base.docx")
    returned = inspect_docx(FIXTURE_ROOT / "returned/returned-reviewed.docx")

    assert base.package_valid and returned.package_valid
    assert (base.tables, base.images, base.omml_objects, base.omml_paragraphs) == (1, 1, 1, 1)
    assert (returned.tables, returned.images, returned.omml_objects) == (1, 1, 1)
    assert base.bookmarks == returned.bookmarks == 7
    assert base.external_relationships == returned.external_relationships == 0


def test_external_relationship_is_counted_but_never_followed(tmp_path: Path) -> None:
    unsafe = tmp_path / "external.docx"
    _add_external_relationship(FIXTURE_ROOT / "base/review-base.docx", unsafe)

    inspection = inspect_docx(unsafe)

    assert inspection.package_valid is False
    assert inspection.structure_inspected is False
    assert inspection.external_relationships == 1
    assert inspection.relationships > 1
    assert inspection.findings[0].code is ErrorCode.DOCX_UNSAFE_RELATIONSHIP
    assert inspection.validation.relationships == "fail"


def test_ambiguous_source_text_creates_no_bookmark(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source, ["Repeated paragraph.", "Repeated paragraph."])
    discovery = discover_project(source, main_document="main.tex")
    units = scan_source_units(source, discovery)
    unanchored = tmp_path / "unanchored.docx"
    anchored = tmp_path / "anchored.docx"
    _write_minimal_docx(unanchored, ["Repeated paragraph.", "Repeated paragraph."])

    result = anchor_source_units(unanchored, anchored, units)

    assert result.coverage == {
        "total": 2,
        "exact": 0,
        "degraded": 0,
        "unmapped": 0,
        "conflict": 2,
    }
    assert all(finding.code is ErrorCode.MAP_AMBIGUOUS for finding in result.findings)
    assert inspect_docx(anchored).bookmarks == 0


def test_unmatched_source_text_creates_no_bookmark(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source, ["Source-only paragraph."])
    discovery = discover_project(source, main_document="main.tex")
    units = scan_source_units(source, discovery)
    unanchored = tmp_path / "unanchored.docx"
    anchored = tmp_path / "anchored.docx"
    _write_minimal_docx(unanchored, ["Different target paragraph."])

    result = anchor_source_units(unanchored, anchored, units)

    assert result.coverage["unmapped"] == 1
    assert result.findings[0].code is ErrorCode.MAP_UNMATCHED
    assert inspect_docx(anchored).bookmarks == 0
