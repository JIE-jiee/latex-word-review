"""Returned Word original archival and drift tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.ingest import (
    ARCHIVE_DOCX_NAME,
    ARCHIVE_MANIFEST_NAME,
    archive_returned_docx,
    verify_returned_archive,
)

FIXTURE = Path(__file__).parent / "fixtures/e0-minimal-paper/returned/returned-reviewed.docx"
RUN_ID = "run_019b0000-0000-7000-8000-000000000001"
EXPORTED_HASH = "sha256:" + "a" * 64


def test_archive_is_fixed_name_read_only_exact_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "mentor-private-name.docx"
    shutil.copyfile(FIXTURE, source)
    original = source.read_bytes()
    destination = tmp_path / "run" / "returned"

    first = archive_returned_docx(
        source,
        destination,
        run_id=RUN_ID,
        exported_docx_sha256=EXPORTED_HASH,
        confidentiality="public_fixture",
    )
    second = archive_returned_docx(
        source,
        destination,
        run_id=RUN_ID,
        exported_docx_sha256=EXPORTED_HASH,
        confidentiality="public_fixture",
    )

    assert first.reused is False
    assert second.reused is True
    assert first.returned_docx_sha256 == second.returned_docx_sha256
    assert set(path.name for path in destination.iterdir()) == {
        ARCHIVE_DOCX_NAME,
        ARCHIVE_MANIFEST_NAME,
    }
    assert (destination / ARCHIVE_DOCX_NAME).read_bytes() == original
    assert source.read_bytes() == original
    assert not os.access(destination / ARCHIVE_DOCX_NAME, os.W_OK) or os.name == "nt"
    verified = verify_returned_archive(
        destination,
        expected_run_id=RUN_ID,
        expected_returned_docx_sha256=first.returned_docx_sha256,
    )
    assert verified.manifest_sha256 == first.manifest_sha256


def test_conflicting_or_tampered_archive_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "returned.docx"
    shutil.copyfile(FIXTURE, source)
    destination = tmp_path / "archive"
    result = archive_returned_docx(
        source,
        destination,
        run_id=RUN_ID,
        exported_docx_sha256=EXPORTED_HASH,
        confidentiality="public_fixture",
    )
    archived = destination / ARCHIVE_DOCX_NAME
    archived.chmod(0o600)
    archived.write_bytes(b"tampered")

    with pytest.raises(ContractError) as raised:
        archive_returned_docx(
            source,
            destination,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_HASH,
            confidentiality="public_fixture",
        )
    assert raised.value.code is ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH
    assert archived.read_bytes() == b"tampered"
    assert source.read_bytes() != archived.read_bytes()
    assert result.returned_docx_sha256 != "sha256:" + "0" * 64


def test_existing_archive_rejects_different_binding_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "returned.docx"
    shutil.copyfile(FIXTURE, source)
    destination = tmp_path / "archive"
    archive_returned_docx(
        source,
        destination,
        run_id=RUN_ID,
        exported_docx_sha256=EXPORTED_HASH,
        confidentiality="public_fixture",
    )
    manifest_before = (destination / ARCHIVE_MANIFEST_NAME).read_bytes()

    with pytest.raises(ContractError) as raised:
        archive_returned_docx(
            source,
            destination,
            run_id=RUN_ID,
            exported_docx_sha256="sha256:" + "b" * 64,
            confidentiality="public_fixture",
        )
    assert raised.value.code is ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH
    assert (destination / ARCHIVE_MANIFEST_NAME).read_bytes() == manifest_before


def test_source_change_during_archive_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "returned.docx"
    shutil.copyfile(FIXTURE, source)
    destination = tmp_path / "archive"
    actual_read = read_stable_bytes
    calls = 0

    def changing_read(path: Path, *, max_bytes: int) -> bytes:
        nonlocal calls
        data = actual_read(path, max_bytes=max_bytes)
        if path == source.resolve():
            calls += 1
            if calls == 2:
                return data + b"changed"
        return data

    monkeypatch.setattr("latex_word_review.ingest.read_stable_bytes", changing_read)
    with pytest.raises(ContractError) as raised:
        archive_returned_docx(
            source,
            destination,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_HASH,
        )
    assert raised.value.code is ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH
    assert not destination.exists()
    assert not list(tmp_path.glob(".archive.returned-*"))


def test_archive_rejects_invalid_bindings_and_self_destination(tmp_path: Path) -> None:
    source = tmp_path / "returned.docx"
    shutil.copyfile(FIXTURE, source)
    with pytest.raises(ContractError) as bad_run:
        archive_returned_docx(
            source,
            tmp_path / "archive",
            run_id="run_not-v7",
            exported_docx_sha256=EXPORTED_HASH,
        )
    assert bad_run.value.code is ErrorCode.SCHEMA_INVALID

    directory = tmp_path / "self"
    directory.mkdir()
    self_source = directory / ARCHIVE_DOCX_NAME
    shutil.copyfile(FIXTURE, self_source)
    with pytest.raises(ContractError) as self_archive:
        archive_returned_docx(
            self_source,
            directory,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_HASH,
        )
    assert self_archive.value.code is ErrorCode.PATH_TRAVERSAL
