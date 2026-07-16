"""Adversarial OPC container and relationship tests for the read-only DOCX gate."""

from __future__ import annotations

import io
import math
import unicodedata
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import latex_word_review.docx_reader as reader_module
from latex_word_review.docx_reader import DocxReadLimits, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode

FIXTURE_DOCX = Path(__file__).parent / "fixtures/e0-minimal-paper/base/review-base.docx"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> None:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


def _zip_bytes(
    entries: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _required_entries() -> list[tuple[str, bytes]]:
    return [
        ("[Content_Types].xml", b"<Types/>"),
        (
            "_rels/.rels",
            (
                f'<Relationships xmlns="{REL_NS}">'
                '<Relationship Id="rId1" Target="word/document.xml"/>'
                "</Relationships>"
            ).encode(),
        ),
        ("word/document.xml", b"<document/>"),
    ]


def _unsafe_ratio(info: zipfile.ZipInfo) -> None:
    info.file_size = 2
    info.compress_size = 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_bytes": 0},
        {"max_entries": 0},
        {"max_member_bytes": 0},
        {"max_total_uncompressed_bytes": 0},
        {"max_xml_bytes": 0},
        {"max_xml_nodes": 0},
        {"max_compression_ratio": 0},
        {"max_compression_ratio": math.inf},
    ],
)
def test_docx_limits_reject_nonpositive_and_nonfinite_values(kwargs: dict[str, Any]) -> None:
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: DocxReadLimits(**kwargs))


def test_package_xml_access_is_bounded_and_story_metadata_is_deterministic() -> None:
    package = read_docx_package(FIXTURE_DOCX)
    assert package.xml_sha256("word/document.xml").startswith("sha256:")
    assert package.xml_root("word/document.xml").tag.endswith("document")
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: package.xml_bytes("word/missing.xml"),
    )
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: package.xml_sha256("word/missing.xml"),
    )
    assert package.story_parts[0] == "word/document.xml"
    assert package.comments_part is None


def test_read_error_translation_distinguishes_initial_invalid_and_post_read_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.docx"
    _assert_error(ErrorCode.DOCX_INVALID_PACKAGE, lambda: read_docx_package(missing))

    def fail_read(path: Path, *, max_bytes: int) -> bytes:
        del path, max_bytes
        raise ContractError(ErrorCode.SCHEMA_INVALID, "unreadable")

    monkeypatch.setattr(reader_module, "read_stable_bytes", fail_read)
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: reader_module._stable_docx_bytes(
            missing,
            DocxReadLimits(),
            post_read=True,
        ),
    )


@pytest.mark.parametrize(
    "name",
    [
        "bad%escape.xml",
        "%FF.xml",
        unicodedata.normalize("NFD", "é.xml"),
    ],
)
def test_member_decoding_rejects_invalid_percent_utf8_and_unicode_form(name: str) -> None:
    _assert_error(ErrorCode.DOCX_INVALID_PACKAGE, lambda: reader_module._decoded_member_name(name))


@pytest.mark.parametrize(
    ("name", "directory"),
    [
        ("", False),
        ("bad\x00.xml", False),
        ("bad\\name.xml", False),
        ("bad?query.xml", False),
        ("bad#fragment.xml", False),
        ("directory", True),
        ("directory/", False),
        ("/absolute.xml", False),
        ("a//b.xml", False),
        ("a/../b.xml", False),
        ("drive:C.xml", False),
        ("%2e%2e/escape.xml", False),
        ("folder/%5Cescape.xml", False),
    ],
)
def test_member_names_reject_noncanonical_and_encoded_traversal(name: str, directory: bool) -> None:
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: reader_module._canonical_member_key(name, directory=directory),
    )


def _open_archive(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data), "r")


def test_member_validation_rejects_entry_count_duplicates_and_canonical_collisions() -> None:
    data = _zip_bytes(_required_entries())
    with _open_archive(data) as archive:
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, DocxReadLimits(max_entries=2)),
        )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        duplicate = _zip_bytes(_required_entries() + [("word/document.xml", b"<other/>")])
    with _open_archive(duplicate) as archive:
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, DocxReadLimits()),
        )

    collision = _zip_bytes(_required_entries() + [("WORD/DOCUMENT.XML", b"<other/>")])
    with _open_archive(collision) as archive:
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, DocxReadLimits()),
        )


@pytest.mark.parametrize(
    ("limits", "mutator"),
    [
        (DocxReadLimits(), lambda info: setattr(info, "flag_bits", info.flag_bits | 1)),
        (DocxReadLimits(), lambda info: setattr(info, "compress_type", zipfile.ZIP_BZIP2)),
        (DocxReadLimits(max_member_bytes=1), lambda info: setattr(info, "file_size", 2)),
        (DocxReadLimits(max_xml_bytes=1), lambda info: setattr(info, "file_size", 2)),
        (
            DocxReadLimits(max_total_uncompressed_bytes=3),
            lambda info: setattr(info, "file_size", 4),
        ),
        (
            DocxReadLimits(max_compression_ratio=1),
            _unsafe_ratio,
        ),
    ],
)
def test_member_validation_rejects_encryption_compression_and_resource_abuse(
    limits: DocxReadLimits,
    mutator: Callable[[zipfile.ZipInfo], object],
) -> None:
    data = _zip_bytes(_required_entries(), compression=zipfile.ZIP_STORED)
    with _open_archive(data) as archive:
        info = archive.infolist()[-1]
        mutator(info)
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, limits),
        )


def test_member_validation_rejects_nonempty_directory_crc_and_missing_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _zip_bytes(_required_entries() + [("word/media/", b"x")])
    with _open_archive(directory) as archive:
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, DocxReadLimits()),
        )

    empty_directory = _zip_bytes(
        _required_entries() + [("word/media/", b"")],
        compression=zipfile.ZIP_STORED,
    )
    with _open_archive(empty_directory) as archive:
        names, _, _ = reader_module._validate_members(archive, DocxReadLimits())
        assert "word/media/" not in names

    valid = _zip_bytes(_required_entries())
    with _open_archive(valid) as archive:
        monkeypatch.setattr(archive, "testzip", lambda: "word/document.xml")
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, DocxReadLimits()),
        )
    missing = _zip_bytes([("word/document.xml", b"<document/>")])
    with _open_archive(missing) as archive:
        _assert_error(
            ErrorCode.DOCX_INVALID_PACKAGE,
            lambda: reader_module._validate_members(archive, DocxReadLimits()),
        )


def test_xml_parser_rejects_dtd_invalid_syntax_and_node_limit() -> None:
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: reader_module._parse_xml(
            b'<!DOCTYPE x [<!ENTITY y "z">]><x>&y;</x>',
            "word/document.xml",
            max_nodes=10,
        ),
    )
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: reader_module._parse_xml(b"<unclosed>", "word/document.xml", max_nodes=10),
    )
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: reader_module._parse_xml(b"<a><b/><c/></a>", "word/document.xml", max_nodes=2),
    )


@pytest.mark.parametrize("part", ["bad.rels", "word/_rels/.rels"])
def test_relationship_source_names_must_be_canonical(part: str) -> None:
    _assert_error(
        ErrorCode.DOCX_INVALID_PACKAGE,
        lambda: reader_module._relationship_source_part(part),
    )
    assert reader_module._relationship_source_part("_rels/.rels") == ""
    assert (
        reader_module._relationship_source_part("word/_rels/document.xml.rels")
        == "word/document.xml"
    )


@pytest.mark.parametrize(
    "target",
    [
        "",
        "bad\\target.xml",
        "https://example.org/file.xml",
        "target.xml?query=1",
        "%FF.xml",
        "../../../escape.xml",
    ],
)
def test_internal_relationship_targets_reject_external_and_escaping_values(target: str) -> None:
    _assert_error(
        ErrorCode.DOCX_UNSAFE_RELATIONSHIP,
        lambda: reader_module._resolve_internal_target("word/document.xml", target),
    )
    assert reader_module._resolve_internal_target("word/document.xml", "#bookmark") == (
        "word/document.xml"
    )
    assert reader_module._resolve_internal_target("word/document.xml", "/word/styles.xml") == (
        "word/styles.xml"
    )


def _relationships(*items: str) -> bytes:
    return f'<Relationships xmlns="{REL_NS}">{"".join(items)}</Relationships>'.encode()


@pytest.mark.parametrize(
    ("xml", "code"),
    [
        (
            _relationships('<Relationship Target="word/document.xml"/>'),
            ErrorCode.DOCX_INVALID_PACKAGE,
        ),
        (
            _relationships(
                '<Relationship Id="r1" Target="word/document.xml"/>',
                '<Relationship Id="r1" Target="word/document.xml"/>',
            ),
            ErrorCode.DOCX_INVALID_PACKAGE,
        ),
        (
            _relationships(
                '<Relationship Id="r1" Target="word/document.xml" TargetMode="External"/>'
            ),
            ErrorCode.DOCX_UNSAFE_RELATIONSHIP,
        ),
        (
            _relationships('<Relationship Id="r1" Target="word/missing.xml"/>'),
            ErrorCode.DOCX_INVALID_PACKAGE,
        ),
    ],
)
def test_relationship_validation_rejects_missing_duplicate_external_and_dangling(
    xml: bytes,
    code: ErrorCode,
) -> None:
    parts = {
        "_rels/.rels": xml,
        "word/document.xml": b"<document/>",
    }
    _assert_error(
        code,
        lambda: reader_module._validate_relationships(
            parts,
            tuple(sorted(parts)),
            DocxReadLimits(),
        ),
    )


def test_story_classification_and_sorting_cover_all_story_kinds() -> None:
    parts = [
        "word/endnotes.xml",
        "word/footer2.xml",
        "word/header1.xml",
        "word/footnotes.xml",
        "word/document.xml",
        "word/comments.xml",
        "custom.xml",
    ]
    stories = [part for part in parts if reader_module._is_story_part(part)]
    assert sorted(stories, key=reader_module._story_key) == [
        "word/document.xml",
        "word/header1.xml",
        "word/footer2.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
    ]
    assert reader_module._story_key("word/other.xml") == (5, "word/other.xml")


def test_read_docx_rejects_suffix_bad_zip_and_detects_post_read_hash_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = tmp_path / "file.bin"
    wrong.write_bytes(b"data")
    _assert_error(ErrorCode.DOCX_INVALID_PACKAGE, lambda: read_docx_package(wrong))
    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"not a zip")
    _assert_error(ErrorCode.DOCX_INVALID_PACKAGE, lambda: read_docx_package(corrupt))

    original = FIXTURE_DOCX.read_bytes()

    def drift(path: Path, limits: DocxReadLimits, *, post_read: bool) -> bytes:
        del path, limits
        return original + b"drift" if post_read else original

    monkeypatch.setattr(reader_module, "_stable_docx_bytes", drift)
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: read_docx_package(tmp_path / "synthetic.docx"),
    )
