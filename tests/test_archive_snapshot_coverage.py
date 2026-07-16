"""Failure-path coverage for immutable snapshots and returned-DOCX archives."""

from __future__ import annotations

import json
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.ingest as ingest_module
import latex_word_review.snapshot as snapshot_module
from latex_word_review.canonical import canonical_json
from latex_word_review.discovery import (
    DEFAULT_DISCOVERY_LIMITS,
    DiscoveryLimits,
    ProjectDiscovery,
    discover_project,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_file
from latex_word_review.ingest import (
    ARCHIVE_DOCX_NAME,
    ARCHIVE_MANIFEST_NAME,
    archive_returned_docx,
    verify_returned_archive,
)
from latex_word_review.snapshot import SNAPSHOT_MANIFEST, snapshot_manifest_sha256, snapshot_project

FIXTURE_DOCX = Path(__file__).parent / "fixtures/e0-minimal-paper/base/review-base.docx"
RUN_ID = "run_019bc0ab-2400-7000-8000-000000000007"
EXPORTED_SHA = "sha256:" + "a" * 64


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> None:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


def _write_project(root: Path, text: str = "Plain text.") -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        f"\\documentclass{{article}}\n\\begin{{document}}\n{text}\n\\end{{document}}\n",
        encoding="utf-8",
        newline="\n",
    )


def _archive(tmp_path: Path, *, confidentiality: str = "local_private") -> Path:
    received = tmp_path / "received.docx"
    shutil.copyfile(FIXTURE_DOCX, received)
    destination = tmp_path / "archive"
    archive_returned_docx(
        received,
        destination,
        run_id=RUN_ID,
        exported_docx_sha256=EXPORTED_SHA,
        confidentiality=cast("Any", confidentiality),
    )
    return destination


def _make_archive_writable(directory: Path) -> None:
    for path in directory.iterdir():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_snapshot_manifest_reader_rejects_duplicate_invalid_and_nonobject(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"a":1,"a":2}', encoding="utf-8")
    _assert_error(
        ErrorCode.SCHEMA_INVALID, lambda: snapshot_module._read_manifest(path, max_bytes=100)
    )
    path.write_bytes(b"\xff")
    _assert_error(
        ErrorCode.SCHEMA_INVALID, lambda: snapshot_module._read_manifest(path, max_bytes=100)
    )
    path.write_text("[]", encoding="utf-8")
    _assert_error(
        ErrorCode.SCHEMA_INVALID, lambda: snapshot_module._read_manifest(path, max_bytes=100)
    )


def test_snapshot_exclusive_write_and_cleanup_ownership(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.write_bytes(b"original")
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: snapshot_module._write_exclusive(existing, b"replacement"),
    )
    assert existing.read_bytes() == b"original"

    parent = tmp_path / "parent"
    parent.mkdir()
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: snapshot_module._remove_owned_tree(
            parent / "foreign",
            parent,
            prefix=".owned-",
        ),
    )
    outside = tmp_path / ".owned-outside"
    outside.mkdir()
    _assert_error(
        ErrorCode.PATH_LINK_ESCAPE,
        lambda: snapshot_module._remove_owned_tree(outside, parent, prefix=".owned-"),
    )
    missing = parent / ".owned-missing"
    snapshot_module._remove_owned_tree(missing, parent, prefix=".owned-")


def test_snapshot_copy_rejects_source_and_destination_digest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    discovery = discover_project(source)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    (source / "main.tex").write_text("drift", encoding="utf-8")
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: snapshot_module._copy_files(
            source,
            temporary,
            discovery,
            DEFAULT_DISCOVERY_LIMITS,
        ),
    )

    shutil.rmtree(source)
    _write_project(source)
    discovery = discover_project(source)
    original_digest = digest_file

    def wrong_digest(path: Path, *, max_bytes: int) -> object:
        digest = original_digest(path, max_bytes=max_bytes)
        if path.is_relative_to(temporary):
            return type(digest)(digest.size_bytes, "sha256:" + "0" * 64)
        return digest

    monkeypatch.setattr("latex_word_review.snapshot.digest_file", wrong_digest)
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: snapshot_module._copy_files(
            source,
            temporary,
            discovery,
            DEFAULT_DISCOVERY_LIMITS,
        ),
    )


def test_snapshot_read_only_and_cleanup_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    child = root / "child"
    child.write_bytes(b"x")
    monkeypatch.setattr(snapshot_module, "_is_link_or_junction", lambda path: path == child)
    _assert_error(ErrorCode.PATH_LINK_ESCAPE, lambda: snapshot_module._make_tree_read_only(root))
    monkeypatch.undo()

    concrete_path = type(root)
    original_chmod = concrete_path.chmod

    def fail_root(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == root:
            raise PermissionError("denied")
        original_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(concrete_path, "chmod", fail_root)
    _assert_error(ErrorCode.INTERNAL_INVARIANT, lambda: snapshot_module._make_tree_read_only(root))
    snapshot_module._make_tree_writable(root)


def test_snapshot_reuse_rejects_root_mode_file_set_and_content_tamper(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)

    writable = tmp_path / "writable"
    snapshot_project(source, writable)
    writable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, writable))

    extra = tmp_path / "extra"
    snapshot_project(source, extra)
    extra.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    extra_file = extra / "extra.txt"
    extra_file.write_bytes(b"extra")
    extra_file.chmod(stat.S_IRUSR)
    extra.chmod(stat.S_IRUSR | stat.S_IXUSR)
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, extra))

    content = tmp_path / "content"
    snapshot_project(source, content)
    content_file = content / "main.tex"
    content_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    content_file.write_bytes(b"tampered")
    content_file.chmod(stat.S_IRUSR)
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, content))
    assert snapshot_manifest_sha256(content).startswith("sha256:")


def test_snapshot_lock_reserved_destination_and_external_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)
    destination = tmp_path / "snapshot"
    lock = tmp_path / ".snapshot.snapshot.lock"
    lock.write_bytes(b"busy")
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, destination))
    assert lock.read_bytes() == b"busy"

    lock.unlink()
    destination.write_bytes(b"unrelated")
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, destination))
    destination.unlink()

    (source / SNAPSHOT_MANIFEST).write_text("reserved", encoding="utf-8")
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\input{snapshot-manifest.json}\n",
        encoding="utf-8",
    )
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: snapshot_project(source, destination))

    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\input{../outside}\n",
        encoding="utf-8",
    )
    (tmp_path / "outside.tex").write_text("outside", encoding="utf-8")
    _assert_error(ErrorCode.PATH_TRAVERSAL, lambda: snapshot_project(source, destination))


def test_snapshot_discovery_and_publication_races_roll_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    destination = tmp_path / "snapshot"
    original_discover = discover_project
    calls = 0

    def drift_after_lock(
        root: Path,
        *,
        main_document: str | None = None,
        limits: DiscoveryLimits = DEFAULT_DISCOVERY_LIMITS,
    ) -> ProjectDiscovery:
        nonlocal calls
        calls += 1
        result = original_discover(root, main_document=main_document, limits=limits)
        if calls == 2:
            return ProjectDiscovery(
                main_document=result.main_document,
                files=result.files,
                dependency_edges=result.dependency_edges,
                external_references=result.external_references,
                engine_hints=result.engine_hints,
                source_tree_sha256="sha256:" + "0" * 64,
                profile_sha256=result.profile_sha256,
            )
        return result

    monkeypatch.setattr(snapshot_module, "discover_project", drift_after_lock)
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, destination))
    assert not destination.exists()
    monkeypatch.undo()

    concrete_path = type(destination)
    original_rename = concrete_path.rename

    def race_rename(self: Path, target: Path) -> Path:
        if target == destination:
            destination.mkdir()
            raise FileExistsError("race")
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", race_rename)
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: snapshot_project(source, destination))
    assert destination.is_dir()


def test_archive_binding_suffix_missing_and_containment_rejections(tmp_path: Path) -> None:
    received = tmp_path / "received.docx"
    shutil.copyfile(FIXTURE_DOCX, received)
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: archive_returned_docx(
            received,
            tmp_path / "bad-run",
            run_id="bad",
            exported_docx_sha256=EXPORTED_SHA,
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: archive_returned_docx(
            received,
            tmp_path / "bad-hash",
            run_id=RUN_ID,
            exported_docx_sha256="bad",
        ),
    )
    wrong_suffix = tmp_path / "received.bin"
    wrong_suffix.write_bytes(b"docx")
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: archive_returned_docx(
            wrong_suffix,
            tmp_path / "suffix",
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
        ),
    )
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: archive_returned_docx(
            tmp_path / "missing.docx",
            tmp_path / "missing",
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
        ),
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_source = nested / ARCHIVE_DOCX_NAME
    shutil.copyfile(FIXTURE_DOCX, nested_source)
    _assert_error(
        ErrorCode.PATH_TRAVERSAL,
        lambda: archive_returned_docx(
            nested_source,
            nested,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
        ),
    )


def test_archive_stage_write_cleanup_and_mode_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"existing")
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT, lambda: ingest_module._write_exclusive(target, b"x")
    )

    parent = tmp_path / "parent"
    parent.mkdir()
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: ingest_module._remove_stage(tmp_path / "foreign", parent, ".owned-"),
    )
    owned = parent / ".owned-stage"
    ingest_module._remove_stage(owned, parent, ".owned-")
    owned.mkdir()
    link = owned / "link"
    link.write_bytes(b"synthetic link placeholder")
    monkeypatch.setattr(type(link), "is_symlink", lambda self: self == link)
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT, lambda: ingest_module._remove_stage(owned, parent, ".owned-")
    )
    monkeypatch.undo()

    concrete_path = type(target)
    original_chmod = concrete_path.chmod

    def denied(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == target:
            raise PermissionError("denied")
        original_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(concrete_path, "chmod", denied)
    _assert_error(ErrorCode.INTERNAL_INVARIANT, lambda: ingest_module._make_read_only(target))


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda directory: (directory / "extra").write_bytes(b"extra"),
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        ),
        (
            lambda directory: (directory / ARCHIVE_MANIFEST_NAME).write_bytes(
                (directory / ARCHIVE_MANIFEST_NAME).read_bytes().rstrip() + b" "
            ),
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        ),
    ],
)
def test_archive_verifier_rejects_file_set_and_noncanonical_manifest(
    tmp_path: Path,
    mutate: Callable[[Path], object],
    code: ErrorCode,
) -> None:
    directory = _archive(tmp_path)
    _make_archive_writable(directory)
    mutate(directory)
    _assert_error(code, lambda: verify_returned_archive(directory))


def test_archive_verifier_rejects_shape_bindings_evidence_confidentiality_and_hash(
    tmp_path: Path,
) -> None:
    mutations: list[Callable[[dict[str, Any]], None]] = [
        lambda manifest: manifest.__setitem__("format", "wrong"),
        lambda manifest: manifest.__setitem__("run_id", "bad"),
        lambda manifest: manifest.__setitem__("artifact", "bad"),
        lambda manifest: cast("dict[str, Any]", manifest["artifact"]).__setitem__(
            "confidentiality", "secret"
        ),
        lambda manifest: cast("dict[str, Any]", manifest["artifact"]).__setitem__(
            "sha256", "sha256:" + "0" * 64
        ),
    ]
    for index, mutate in enumerate(mutations):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        received = case / "received.docx"
        shutil.copyfile(FIXTURE_DOCX, received)
        directory = case / "archive"
        archive_returned_docx(
            received,
            directory,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
        )
        _make_archive_writable(directory)
        manifest_path = directory / ARCHIVE_MANIFEST_NAME
        manifest = cast("dict[str, Any]", json.loads(manifest_path.read_bytes()))
        mutate(manifest)
        manifest_path.write_bytes(canonical_json(manifest) + b"\n")

        def verify_case(case_directory: Path = directory) -> object:
            return verify_returned_archive(case_directory)

        _assert_error(ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH, verify_case)


def test_archive_expected_bindings_and_existing_confidentiality_are_enforced(
    tmp_path: Path,
) -> None:
    directory = _archive(tmp_path)
    verified = verify_returned_archive(directory)
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: verify_returned_archive(
            directory, expected_run_id="run_019bc0ab-2400-7000-8000-000000000099"
        ),
    )
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: verify_returned_archive(
            directory,
            expected_returned_docx_sha256="sha256:" + "0" * 64,
        ),
    )
    received = tmp_path / "received.docx"
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: archive_returned_docx(
            received,
            directory,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
            confidentiality="public_fixture",
        ),
    )
    assert digest_file(directory / ARCHIVE_DOCX_NAME, max_bytes=128 * 1024 * 1024).sha256 == (
        verified.returned_docx_sha256
    )


def test_archive_source_drift_and_publish_failure_clean_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = tmp_path / "received.docx"
    shutil.copyfile(FIXTURE_DOCX, received)
    destination = tmp_path / "archive"
    original_write = ingest_module._write_exclusive
    calls = 0

    def mutate_source(path: Path, data: bytes) -> None:
        nonlocal calls
        original_write(path, data)
        calls += 1
        if calls == 2:
            received.write_bytes(received.read_bytes() + b"drift")

    monkeypatch.setattr(ingest_module, "_write_exclusive", mutate_source)
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: archive_returned_docx(
            received,
            destination,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
        ),
    )
    assert not destination.exists()
    assert not list(tmp_path.glob(".archive.returned-*"))
    monkeypatch.undo()

    shutil.copyfile(FIXTURE_DOCX, received)
    concrete_path = type(destination)
    original_rename = concrete_path.rename

    def deny_rename(self: Path, target: Path) -> Path:
        if target == destination:
            raise PermissionError("denied")
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", deny_rename)
    _assert_error(
        ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
        lambda: archive_returned_docx(
            received,
            destination,
            run_id=RUN_ID,
            exported_docx_sha256=EXPORTED_SHA,
        ),
    )
    assert not destination.exists()
    assert not list(tmp_path.glob(".archive.returned-*"))
