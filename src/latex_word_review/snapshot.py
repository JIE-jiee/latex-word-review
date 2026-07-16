"""Immutable, content-addressed snapshots published atomically."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latex_word_review.canonical import canonical_json
from latex_word_review.discovery import (
    DEFAULT_DISCOVERY_LIMITS,
    DiscoveryLimits,
    ProjectDiscovery,
    discover_project,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, digest_bytes, digest_file, read_stable_bytes
from latex_word_review.ids import derive_source_manifest_id
from latex_word_review.paths import ensure_disjoint_roots, resolve_within, validate_relative_path

SNAPSHOT_MANIFEST = "snapshot-manifest.json"


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """Serializable snapshot result without an absolute destination path."""

    source_manifest_id: str
    source_tree_sha256: str
    main_document: str
    manifest_path: str
    file_count: int
    total_bytes: int
    reused: bool

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "source_manifest_id": self.source_manifest_id,
            "source_tree_sha256": self.source_tree_sha256,
            "main_document": self.main_document,
            "manifest_path": self.manifest_path,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "reused": self.reused,
        }


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def _manifest_for(discovery: ProjectDiscovery) -> dict[str, Any]:
    return {
        "manifest_format": "latex-word-review-snapshot-v1",
        "source_manifest_id": derive_source_manifest_id(discovery.source_tree_sha256),
        "main_document": discovery.main_document,
        "source_tree_sha256": discovery.source_tree_sha256,
        "files": [source_file.as_dict() for source_file in discovery.files],
        "dependency_edges": [edge.as_dict() for edge in discovery.dependency_edges],
        "engine_hints": [hint.as_dict() for hint in discovery.engine_hints],
        "external_references": [item.as_dict() for item in discovery.external_references],
        "discovery_profile": {
            "name": "latex-project-discovery",
            "version": "1",
            "configuration_sha256": discovery.profile_sha256,
        },
        "immutability": {
            "snapshot_read_only": True,
            "source_origin_pre_sha256": discovery.source_tree_sha256,
            "source_origin_post_sha256": discovery.source_tree_sha256,
        },
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot manifest has duplicate keys")
        result[key] = value
    return result


def _read_manifest(path: Path, *, max_bytes: int) -> dict[str, Any]:
    data = read_stable_bytes(path, max_bytes=max_bytes)
    try:
        parsed = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot manifest is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot manifest must be an object")
    return parsed


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "snapshot file could not be written",
        ) from exc


def _copy_files(
    source_root: Path,
    temporary_root: Path,
    discovery: ProjectDiscovery,
    limits: DiscoveryLimits,
) -> None:
    for source_file in discovery.files:
        relative_path = validate_relative_path(source_file.path)
        source = resolve_within(source_root, relative_path)
        data = read_stable_bytes(source, max_bytes=limits.max_file_bytes)
        actual = digest_bytes(data)
        expected = FileDigest(source_file.size_bytes, source_file.sha256)
        if actual != expected:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed before copying")
        target = temporary_root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(target, data)
        if digest_file(target, max_bytes=limits.max_file_bytes) != expected:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot copy digest mismatch")


def _make_tree_read_only(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        try:
            if _is_link_or_junction(path):
                raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "snapshot contains a link")
            if path.is_dir():
                path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
            else:
                path.chmod(stat.S_IRUSR | stat.S_IRGRP)
        except ContractError:
            raise
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "snapshot could not be made read-only",
            ) from exc
    try:
        root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "snapshot root is not read-only") from exc


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in (root, *root.rglob("*")):
        try:
            if path.is_dir():
                path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            else:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            continue


def _remove_owned_tree(path: Path, parent: Path, *, prefix: str, exact: bool = False) -> None:
    """Remove only a sibling tree with the caller-created safe prefix."""

    owned_name = path.name == prefix if exact else path.name.startswith(prefix)
    if not owned_name:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "refusing to remove an unowned tree")
    resolved_parent = parent.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    if resolved_path.parent != resolved_parent:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "temporary snapshot escaped its parent")
    _make_tree_writable(path)
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "temporary snapshot cleanup failed",
        ) from exc


def _snapshot_result(discovery: ProjectDiscovery, *, reused: bool) -> SnapshotResult:
    return SnapshotResult(
        source_manifest_id=derive_source_manifest_id(discovery.source_tree_sha256),
        source_tree_sha256=discovery.source_tree_sha256,
        main_document=discovery.main_document,
        manifest_path=SNAPSHOT_MANIFEST,
        file_count=len(discovery.files),
        total_bytes=sum(source_file.size_bytes for source_file in discovery.files),
        reused=reused,
    )


def _verify_existing_snapshot(
    destination: Path,
    discovery: ProjectDiscovery,
    limits: DiscoveryLimits,
) -> None:
    expected_manifest = _manifest_for(discovery)
    manifest_path = resolve_within(destination, SNAPSHOT_MANIFEST)
    actual_manifest = _read_manifest(manifest_path, max_bytes=limits.max_file_bytes)
    if actual_manifest != expected_manifest:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "destination already contains a different snapshot",
        )
    expected_paths = {source_file.path for source_file in discovery.files}
    observed_paths: set[str] = set()
    if destination.stat().st_mode & stat.S_IWUSR:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot root is not read-only")
    for path in destination.rglob("*"):
        if _is_link_or_junction(path):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "existing snapshot contains a link")
        if path.stat().st_mode & stat.S_IWUSR:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot entry is not read-only")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot contains a non-file entry")
        relative = path.relative_to(destination).as_posix()
        if relative != SNAPSHOT_MANIFEST:
            observed_paths.add(validate_relative_path(relative))
    if observed_paths != expected_paths:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot file set does not match")
    for source_file in discovery.files:
        snapshot_file = resolve_within(destination, source_file.path)
        expected = FileDigest(source_file.size_bytes, source_file.sha256)
        if digest_file(snapshot_file, max_bytes=limits.max_file_bytes) != expected:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot content does not match")


def _same_discovery(before: ProjectDiscovery, after: ProjectDiscovery) -> bool:
    return (
        before.source_tree_sha256 == after.source_tree_sha256
        and before.main_document == after.main_document
        and before.files == after.files
        and before.dependency_edges == after.dependency_edges
        and before.external_references == after.external_references
    )


def _acquire_lock(path: Path) -> int:
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot destination is busy") from exc
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot lock could not be created") from exc


def snapshot_project(
    source_root: Path,
    destination: Path,
    *,
    main_document: str | None = None,
    limits: DiscoveryLimits = DEFAULT_DISCOVERY_LIMITS,
) -> SnapshotResult:
    """Create or verify one immutable snapshot without modifying its source."""

    source, target = ensure_disjoint_roots(source_root, destination)
    if target.name in {"", ".", ".."}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot destination name is invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    before = discover_project(source, main_document=main_document, limits=limits)
    if before.external_references:
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "project contains unresolved or rejected external references",
        )
    if any(source_file.path == SNAPSHOT_MANIFEST for source_file in before.files):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source uses a reserved snapshot path")

    lock_path = target.with_name(f".{target.name}.snapshot.lock")
    lock_fd = _acquire_lock(lock_path)
    temporary: Path | None = None
    published_by_call = False
    try:
        after_lock = discover_project(
            source,
            main_document=before.main_document,
            limits=limits,
        )
        if not _same_discovery(before, after_lock):
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed during discovery")
        if target.exists():
            if not target.is_dir():
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "snapshot destination already exists and is not a directory",
                )
            _verify_existing_snapshot(target, before, limits)
            final_source = discover_project(
                source,
                main_document=before.main_document,
                limits=limits,
            )
            if not _same_discovery(before, final_source):
                raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed during reuse")
            return _snapshot_result(before, reused=True)

        prefix = f".{target.name}.tmp-"
        temporary = Path(tempfile.mkdtemp(prefix=prefix, dir=target.parent))
        _copy_files(source, temporary, before, limits)
        manifest_data = canonical_json(_manifest_for(before)) + b"\n"
        _write_exclusive(temporary / SNAPSHOT_MANIFEST, manifest_data)
        pre_publish = discover_project(
            source,
            main_document=before.main_document,
            limits=limits,
        )
        if not _same_discovery(before, pre_publish):
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed while copying")
        _make_tree_read_only(temporary)
        try:
            temporary.rename(target)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "snapshot destination appeared during publication",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "snapshot atomic publish failed",
            ) from exc
        published_by_call = True
        temporary = None
        final_source = discover_project(
            source,
            main_document=before.main_document,
            limits=limits,
        )
        if not _same_discovery(before, final_source):
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed during publication")
        _verify_existing_snapshot(target, before, limits)
        return _snapshot_result(before, reused=False)
    except Exception:
        if temporary is not None and temporary.exists():
            _remove_owned_tree(temporary, target.parent, prefix=f".{target.name}.tmp-")
        if published_by_call and target.exists():
            _remove_owned_tree(target, target.parent, prefix=target.name, exact=True)
        raise
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "snapshot lock cleanup failed",
            ) from exc


def snapshot_manifest_sha256(destination: Path, *, max_bytes: int = 1024 * 1024) -> str:
    """Hash an already-published manifest without exposing its absolute path."""

    manifest = resolve_within(destination, SNAPSHOT_MANIFEST)
    return digest_file(manifest, max_bytes=max_bytes).sha256


__all__ = [
    "SNAPSHOT_MANIFEST",
    "SnapshotResult",
    "snapshot_manifest_sha256",
    "snapshot_project",
]
