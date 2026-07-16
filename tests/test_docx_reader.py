"""Security and immutability tests for the production DOCX/OPC reader."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import latex_word_review.docx_reader as reader_module
from latex_word_review.docx_reader import DocxReadLimits, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from tests._docx_factory import (
    MINIMAL_DOCUMENT,
    mark_first_member_encrypted,
    patch_first_central_crc,
    write_docx,
)


def assert_rejected(path: Path, code: ErrorCode) -> None:
    with pytest.raises(ContractError) as caught:
        read_docx_package(path)
    assert caught.value.code is code


def test_reads_valid_package_without_writing(tmp_path: Path) -> None:
    path = write_docx(tmp_path / "valid.docx")
    before = path.read_bytes()
    package = read_docx_package(path)
    assert package.source_name == "valid.docx"
    assert package.story_parts == ("word/document.xml",)
    assert package.comments_part is None
    assert package.file_sha256.startswith("sha256:")
    assert package.xml_bytes("word/document.xml") == MINIMAL_DOCUMENT
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_rejects_duplicate_and_canonically_unsafe_members(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.docx"
    with pytest.warns(UserWarning, match="Duplicate name"):
        write_docx(duplicate, duplicate_document=True)
    assert_rejected(duplicate, ErrorCode.DOCX_INVALID_PACKAGE)

    traversal = write_docx(
        tmp_path / "traversal.docx",
        extra_parts={"word/../escape.xml": b"<escape/>"},
    )
    assert_rejected(traversal, ErrorCode.DOCX_INVALID_PACKAGE)


def test_rejects_encryption_unsupported_compression_and_bad_crc(tmp_path: Path) -> None:
    encrypted = write_docx(tmp_path / "encrypted.docx")
    mark_first_member_encrypted(encrypted)
    assert_rejected(encrypted, ErrorCode.DOCX_INVALID_PACKAGE)

    unsupported = write_docx(tmp_path / "bzip2.docx", compression=zipfile.ZIP_BZIP2)
    assert_rejected(unsupported, ErrorCode.DOCX_INVALID_PACKAGE)

    corrupt = write_docx(tmp_path / "bad-crc.docx")
    patch_first_central_crc(corrupt)
    assert_rejected(corrupt, ErrorCode.DOCX_INVALID_PACKAGE)


def test_rejects_member_total_and_compression_bomb_limits(tmp_path: Path) -> None:
    path = write_docx(
        tmp_path / "large.docx",
        extra_parts={"word/large.xml": b"<root>" + b"A" * 4096 + b"</root>"},
    )
    with pytest.raises(ContractError) as member_error:
        read_docx_package(path, limits=DocxReadLimits(max_member_bytes=1_024))
    assert member_error.value.code is ErrorCode.DOCX_INVALID_PACKAGE

    with pytest.raises(ContractError) as total_error:
        read_docx_package(path, limits=DocxReadLimits(max_total_uncompressed_bytes=2_048))
    assert total_error.value.code is ErrorCode.DOCX_INVALID_PACKAGE

    with pytest.raises(ContractError) as ratio_error:
        read_docx_package(path, limits=DocxReadLimits(max_compression_ratio=2.0))
    assert ratio_error.value.code is ErrorCode.DOCX_INVALID_PACKAGE


def test_rejects_dtd_entity_and_external_relationship(tmp_path: Path) -> None:
    dtd = write_docx(
        tmp_path / "dtd.docx",
        document_xml=(
            b'<?xml version="1.0"?>'
            + b" " * 5_000
            + b'<!DOCTYPE w:document [<!ENTITY x "boom">]>'
            + b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            + b'wordprocessingml/2006/main"><w:body/></w:document>'
        ),
    )
    assert_rejected(dtd, ErrorCode.DOCX_INVALID_PACKAGE)

    external_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1"
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
        Target="https://example.invalid/payload" TargetMode="External"/>
    </Relationships>"""
    external = write_docx(tmp_path / "external.docx", root_rels=external_rels)
    assert_rejected(external, ErrorCode.DOCX_UNSAFE_RELATIONSHIP)


def test_rejects_input_hash_change_between_pre_and_post_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_docx(tmp_path / "changes.docx")
    real_reader = cast("Callable[..., bytes]", reader_module.__dict__["read_stable_bytes"])
    call_count = 0

    def changing_read(candidate: Path, *, max_bytes: int) -> bytes:
        nonlocal call_count
        call_count += 1
        data = real_reader(candidate, max_bytes=max_bytes)
        return data if call_count == 1 else data + b"changed"

    monkeypatch.setattr(reader_module, "read_stable_bytes", changing_read)
    assert_rejected(path, ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH)
