"""Deterministic and safety tests for the built-in Word review profile."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.docx_reader import read_docx_package
from latex_word_review.review_reference import (
    PROFILE_ID,
    REFERENCE_CONFIG_SHA256,
    REFERENCE_DOCX_SHA256,
    build_review_reference_bytes,
    materialized_review_reference,
    verify_review_reference_bytes,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}


def test_review_reference_is_small_deterministic_and_structurally_valid(tmp_path: Path) -> None:
    first = build_review_reference_bytes()
    second = build_review_reference_bytes()

    assert first == second
    assert len(first) < 16 * 1024
    assert hashlib.sha256(first).hexdigest() == REFERENCE_DOCX_SHA256
    assert PROFILE_ID == "academic-review-v1"
    assert REFERENCE_CONFIG_SHA256.startswith("sha256:")

    with materialized_review_reference(tmp_path) as path:
        assert path.read_bytes() == first
        package = read_docx_package(path)
        assert package.entry_count == 5
        styles = package.xml_root("word/styles.xml")
        document = package.xml_root("word/document.xml")
        assert (
            styles.xpath(
                "string(w:docDefaults/w:rPrDefault/w:rPr/w:rFonts/@w:ascii)", namespaces=NS
            )
            == "Times New Roman"
        )
        assert (
            styles.xpath(
                "string(w:docDefaults/w:rPrDefault/w:rPr/w:rFonts/@w:eastAsia)", namespaces=NS
            )
            == "SimSun"
        )
        assert (
            styles.xpath("string(w:style[@w:styleId='Normal']/w:pPr/w:jc/@w:val)", namespaces=NS)
            == "both"
        )
        assert document.xpath("string(.//w:pgSz/@w:w)", namespaces=NS) == "11906"
        assert document.xpath("string(.//w:pgSz/@w:h)", namespaces=NS) == "16838"
        materialized = path
    assert not materialized.exists()


def test_review_reference_rejects_tampering_and_cleans_after_errors(tmp_path: Path) -> None:
    data = bytearray(build_review_reference_bytes())
    data[-1] ^= 1
    with pytest.raises((RuntimeError, zipfile.BadZipFile)):
        verify_review_reference_bytes(bytes(data))

    with (
        pytest.raises(RuntimeError, match="sentinel"),
        materialized_review_reference(tmp_path) as path,
    ):
        assert path.exists()
        raise RuntimeError("sentinel")
    assert not list(tmp_path.glob(".lwr-review-reference-*.docx"))


def test_review_reference_has_no_external_relationships_or_active_content(tmp_path: Path) -> None:
    path = tmp_path / "reference.docx"
    path.write_bytes(build_review_reference_bytes())
    with zipfile.ZipFile(path) as archive:
        assert tuple(info.filename for info in archive.infolist()) == (
            "[Content_Types].xml",
            "_rels/.rels",
            "word/_rels/document.xml.rels",
            "word/document.xml",
            "word/styles.xml",
        )
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        assert not any(
            name.endswith(("vbaProject.bin", ".exe", ".dll")) for name in archive.namelist()
        )
        for name in ("_rels/.rels", "word/_rels/document.xml.rels"):
            root = etree.fromstring(archive.read(name))
            assert not root.xpath("//*[local-name()='Relationship'][@TargetMode='External']")
