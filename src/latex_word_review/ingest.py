"""Immutable archival gate for a returned Word review original."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from latex_word_review.canonical import canonical_json, sha256_bytes
from latex_word_review.contracts import load_contract_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, read_stable_bytes
from latex_word_review.ids import derive_artifact_id, stable_id_from_sha256

ARCHIVE_DOCX_NAME = "returned-original.docx"
ARCHIVE_MANIFEST_NAME = "returned-original.manifest.json"
_ARCHIVE_FORMAT = "latex-word-review-returned-archive-v1"
_MAX_DOCX_BYTES = 128 * 1024 * 1024
_RUN_ID_RE = re.compile(
    r"^run_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@dataclass(frozen=True, slots=True)
class ReturnedArchive:
    directory: Path
    docx_path: Path
    manifest_path: Path
    returned_docx_sha256: str
    manifest_sha256: str
    size_bytes: int
    run_id: str
    exported_docx_sha256: str
    reused: bool


def _validate_bindings(run_id: str, exported_docx_sha256: str) -> None:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "archive run_id must be a UUIDv7 run ID")
    stable_id_from_sha256("art_", exported_docx_sha256)


def _manifest(
    *,
    run_id: str,
    returned_sha256: str,
    exported_docx_sha256: str,
    size_bytes: int,
    confidentiality: Literal["public_fixture", "local_private", "derived_private"],
) -> dict[str, Any]:
    return {
        "format": _ARCHIVE_FORMAT,
        "run_id": run_id,
        "exported_review_docx_sha256": exported_docx_sha256,
        "artifact": {
            "artifact_id": derive_artifact_id(returned_sha256),
            "path": ARCHIVE_DOCX_NAME,
            "role": "returned_docx_original",
            "media_type": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            "size_bytes": size_bytes,
            "sha256": returned_sha256,
            "immutable": True,
            "confidentiality": confidentiality,
        },
        "immutability": {
            "source_observed_pre_sha256": returned_sha256,
            "source_observed_post_sha256": returned_sha256,
            "archive_copy_sha256": returned_sha256,
            "parse_original_directly": False,
        },
    }


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "archive stage write failed") from exc


def _make_read_only(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT, "archive could not be made read-only"
        ) from exc


def _remove_stage(path: Path, parent: Path, prefix: str) -> None:
    if path.parent.resolve(strict=True) != parent.resolve(strict=True) or not path.name.startswith(
        prefix
    ):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "refusing to clean an unowned archive")
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_symlink():
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "archive stage contains a link")
        with suppress(OSError):
            child.chmod(stat.S_IRUSR | stat.S_IWUSR)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "archive stage cleanup failed") from exc


def _archive_result(directory: Path, manifest: dict[str, Any], *, reused: bool) -> ReturnedArchive:
    artifact = cast("dict[str, Any]", manifest["artifact"])
    manifest_bytes = canonical_json(manifest) + b"\n"
    return ReturnedArchive(
        directory=directory,
        docx_path=directory / ARCHIVE_DOCX_NAME,
        manifest_path=directory / ARCHIVE_MANIFEST_NAME,
        returned_docx_sha256=cast("str", artifact["sha256"]),
        manifest_sha256=sha256_bytes(manifest_bytes),
        size_bytes=cast("int", artifact["size_bytes"]),
        run_id=cast("str", manifest["run_id"]),
        exported_docx_sha256=cast("str", manifest["exported_review_docx_sha256"]),
        reused=reused,
    )


def verify_returned_archive(
    directory: Path,
    *,
    expected_run_id: str | None = None,
    expected_returned_docx_sha256: str | None = None,
    max_docx_bytes: int = _MAX_DOCX_BYTES,
) -> ReturnedArchive:
    """Verify the exact two-file archive before any revision parsing."""

    if directory.is_symlink():
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive must not be a link",
        )
    try:
        root = directory.resolve(strict=True)
    except OSError as exc:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive is unavailable",
        ) from exc
    if not root.is_dir() or root.is_symlink():
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive must be a real directory",
        )
    children = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    if any(item.is_symlink() or not item.is_file() for item in children):
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive contains an unsafe entry",
        )
    if {item.name for item in children} != {ARCHIVE_DOCX_NAME, ARCHIVE_MANIFEST_NAME}:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive file set differs",
        )
    manifest_path = root / ARCHIVE_MANIFEST_NAME
    manifest_bytes = read_stable_bytes(manifest_path, max_bytes=64 * 1024)
    manifest = load_contract_json(manifest_bytes)
    if canonical_json(manifest) + b"\n" != manifest_bytes:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive manifest is not canonical",
        )
    if (
        set(manifest)
        != {
            "format",
            "run_id",
            "exported_review_docx_sha256",
            "artifact",
            "immutability",
        }
        or manifest.get("format") != _ARCHIVE_FORMAT
    ):
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive manifest shape differs",
        )
    run_id = manifest.get("run_id")
    exported_hash = manifest.get("exported_review_docx_sha256")
    if not isinstance(run_id, str) or not isinstance(exported_hash, str):
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive bindings are invalid",
        )
    try:
        _validate_bindings(run_id, exported_hash)
    except ContractError as exc:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive bindings are invalid",
        ) from exc
    artifact = manifest.get("artifact")
    immutability = manifest.get("immutability")
    if not isinstance(artifact, dict) or not isinstance(immutability, dict):
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive evidence is invalid",
        )
    confidentiality = artifact.get("confidentiality")
    if confidentiality not in {"public_fixture", "local_private", "derived_private"}:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive confidentiality is invalid",
        )
    returned_bytes = read_stable_bytes(root / ARCHIVE_DOCX_NAME, max_bytes=max_docx_bytes)
    digest = digest_bytes(returned_bytes)
    expected_artifact = _manifest(
        run_id=run_id,
        returned_sha256=digest.sha256,
        exported_docx_sha256=exported_hash,
        size_bytes=digest.size_bytes,
        confidentiality=cast(
            "Literal['public_fixture', 'local_private', 'derived_private']", confidentiality
        ),
    )
    if manifest != expected_artifact:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive content or manifest hash differs",
        )
    if expected_run_id is not None and run_id != expected_run_id:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH, "archive run binding differs"
        )
    if expected_returned_docx_sha256 is not None and digest.sha256 != expected_returned_docx_sha256:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive hash binding differs",
        )
    return _archive_result(root, manifest, reused=True)


def archive_returned_docx(
    source: Path,
    directory: Path,
    *,
    run_id: str,
    exported_docx_sha256: str,
    confidentiality: Literal[
        "public_fixture", "local_private", "derived_private"
    ] = "local_private",
    max_docx_bytes: int = _MAX_DOCX_BYTES,
) -> ReturnedArchive:
    """Copy returned DOCX bytes once, publish atomically, and never parse the source path."""

    _validate_bindings(run_id, exported_docx_sha256)
    if source.suffix.lower() != ".docx":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "returned original must have a .docx suffix")
    if source.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "returned original must not be a link")
    if directory.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "returned archive must not be a link")
    try:
        source_path = source.resolve(strict=True)
    except OSError as exc:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned original is unavailable",
        ) from exc
    destination = directory.resolve(strict=False)
    if source_path == destination / ARCHIVE_DOCX_NAME or destination in source_path.parents:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "archive destination contains its source")
    try:
        before = read_stable_bytes(source_path, max_bytes=max_docx_bytes)
    except ContractError as exc:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned original could not be read immutably",
        ) from exc
    digest = digest_bytes(before)
    manifest = _manifest(
        run_id=run_id,
        returned_sha256=digest.sha256,
        exported_docx_sha256=exported_docx_sha256,
        size_bytes=digest.size_bytes,
        confidentiality=confidentiality,
    )
    if destination.exists() or destination.is_symlink():
        existing = verify_returned_archive(
            destination,
            expected_run_id=run_id,
            expected_returned_docx_sha256=digest.sha256,
            max_docx_bytes=max_docx_bytes,
        )
        existing_manifest = load_contract_json(
            read_stable_bytes(existing.manifest_path, max_bytes=64 * 1024)
        )
        if existing_manifest != manifest:
            raise ContractError(
                ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
                "returned archive bindings or confidentiality differ",
            )
        if read_stable_bytes(source_path, max_bytes=max_docx_bytes) != before:
            raise ContractError(
                ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
                "returned original changed during archive reuse",
            )
        return existing

    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{destination.name}.returned-"
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=destination.parent))
    try:
        _write_exclusive(staged / ARCHIVE_DOCX_NAME, before)
        _write_exclusive(staged / ARCHIVE_MANIFEST_NAME, canonical_json(manifest) + b"\n")
        archived = read_stable_bytes(staged / ARCHIVE_DOCX_NAME, max_bytes=max_docx_bytes)
        try:
            after = read_stable_bytes(source_path, max_bytes=max_docx_bytes)
        except ContractError as exc:
            raise ContractError(
                ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
                "returned original changed during archival",
            ) from exc
        if archived != before or after != before:
            raise ContractError(
                ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
                "returned original or archive bytes changed",
            )
        _make_read_only(staged / ARCHIVE_DOCX_NAME)
        _make_read_only(staged / ARCHIVE_MANIFEST_NAME)
        try:
            staged.rename(destination)
        except OSError as exc:
            raise ContractError(
                ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
                "returned archive publication failed",
            ) from exc
    except Exception:
        if staged.exists():
            _remove_stage(staged, destination.parent, prefix)
        raise
    verified = verify_returned_archive(
        destination,
        expected_run_id=run_id,
        expected_returned_docx_sha256=digest.sha256,
        max_docx_bytes=max_docx_bytes,
    )
    return ReturnedArchive(
        directory=verified.directory,
        docx_path=verified.docx_path,
        manifest_path=verified.manifest_path,
        returned_docx_sha256=verified.returned_docx_sha256,
        manifest_sha256=verified.manifest_sha256,
        size_bytes=verified.size_bytes,
        run_id=verified.run_id,
        exported_docx_sha256=verified.exported_docx_sha256,
        reused=False,
    )


__all__ = [
    "ARCHIVE_DOCX_NAME",
    "ARCHIVE_MANIFEST_NAME",
    "ReturnedArchive",
    "archive_returned_docx",
    "verify_returned_archive",
]
