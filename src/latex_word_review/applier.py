"""Atomic application of one sealed and reproducible PatchPlan to a new tree."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from latex_word_review.atomic_publish import publish_new_directory
from latex_word_review.canonical import canonical_json, compute_payload_sha256, sha256_canonical
from latex_word_review.discovery import DEFAULT_DISCOVERY_LIMITS, DiscoveryLimits
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, read_stable_bytes
from latex_word_review.paths import ensure_disjoint_roots, validate_relative_path
from latex_word_review.planner import (
    apply_operations_to_bytes,
    build_unified_diff,
    plan_patch,
    verify_plan_identity,
)

APPLY_MARKER: Final = ".latex-word-review-applied.json"
_MAX_TREE_FILES: Final = 4096
_MAX_TREE_BYTES: Final = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ApplyResult:
    patch_plan_sha256: str
    source_tree_sha256: str
    unified_diff_sha256: str
    output_file_count: int
    reused: bool


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def _inventory(root: Path, *, max_file_bytes: int) -> tuple[dict[str, bytes], str]:
    files: dict[str, bytes] = {}
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if _is_link_or_junction(path):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "apply tree contains a link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "apply tree contains a special file")
        relative = validate_relative_path(path.relative_to(root).as_posix())
        if relative in files:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "apply tree contains duplicate paths")
        if len(files) >= _MAX_TREE_FILES:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "apply tree exceeds the file count limit")
        data = read_stable_bytes(path, max_bytes=max_file_bytes)
        total_bytes += len(data)
        if total_bytes > _MAX_TREE_BYTES:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "apply tree exceeds the byte limit")
        files[relative] = data
    records = [
        {
            "path": path,
            "size_bytes": len(data),
            "sha256": digest_bytes(data).sha256,
        }
        for path, data in files.items()
    ]
    return files, sha256_canonical(records)


def _make_writable(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        try:
            if _is_link_or_junction(path):
                continue
            if path.is_dir():
                path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            else:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "work copy is not writable") from exc


def _remove_owned_tree(path: Path, parent: Path, prefix: str) -> None:
    if not path.name.startswith(prefix) or path.resolve(strict=False).parent != parent.resolve(
        strict=True
    ):
        raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "refusing to clean an unowned tree")
    if not path.exists():
        return
    _make_writable(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "work copy cleanup failed") from exc


def _marker(
    patch_plan: Mapping[str, Any],
    unified_diff: bytes,
    expected_files: Mapping[str, bytes],
) -> dict[str, Any]:
    payload = cast("Mapping[str, Any]", patch_plan["payload"])
    return {
        "format": "latex-word-review-apply-marker-v1",
        "patch_plan_sha256": compute_payload_sha256(patch_plan),
        "source_tree_sha256": payload["source_tree_sha256"],
        "unified_diff_sha256": digest_bytes(unified_diff).sha256,
        "output_files": [
            {
                "path": path,
                "size_bytes": len(data),
                "sha256": digest_bytes(data).sha256,
            }
            for path, data in sorted(expected_files.items())
        ],
    }


def _write_file(path: Path, data: bytes) -> None:
    try:
        with path.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "work copy file write failed") from exc


def _verify_output(
    root: Path,
    marker: Mapping[str, Any],
    expected_files: Mapping[str, bytes],
    *,
    max_file_bytes: int,
) -> None:
    observed, _ = _inventory(root, max_file_bytes=max_file_bytes)
    expected_paths = {*expected_files, APPLY_MARKER}
    if set(observed) != expected_paths:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "work copy file set differs")
    marker_bytes = canonical_json(marker) + b"\n"
    if observed[APPLY_MARKER] != marker_bytes:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "apply marker differs")
    for path, data in expected_files.items():
        if observed[path] != data:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "work copy content differs")


def _publish_directory(staged: Path, destination: Path) -> None:
    try:
        publish_new_directory(staged, destination)
    except FileExistsError as exc:
        raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "apply destination appeared") from exc
    except OSError as exc:
        raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "work copy publication failed") from exc


def apply_patch_plan(
    source_root: Path,
    destination: Path,
    *,
    patch_plan: Mapping[str, Any],
    unified_diff: bytes,
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    limits: DiscoveryLimits = DEFAULT_DISCOVERY_LIMITS,
) -> ApplyResult:
    """Apply one ready/noop plan to a new sibling-staged directory, or verify reuse."""

    verify_plan_identity(patch_plan)
    payload = cast("Mapping[str, Any]", patch_plan["payload"])
    if payload["status"] == "blocked":
        raise ContractError(
            ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED,
            "blocked PatchPlan cannot be applied",
        )
    if payload["mode"] != "dry_run":
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "unsupported PatchPlan mode")
    diff_artifact = cast("Mapping[str, Any] | None", payload["unified_diff"])
    if diff_artifact is None:
        if unified_diff:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "unexpected diff bytes")
        diff_path = "apply/changes.patch"
        confidentiality = "derived_private"
    else:
        if digest_bytes(unified_diff).sha256 != diff_artifact["sha256"]:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "unified diff hash differs")
        if len(unified_diff) != diff_artifact["size_bytes"]:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "unified diff size differs")
        diff_path = cast("str", diff_artifact["path"])
        confidentiality = cast("str", diff_artifact["confidentiality"])

    try:
        expected_plan = plan_patch(
            source_root,
            source_tree_sha256=cast("str", payload["source_tree_sha256"]),
            changeset=changeset,
            approval=approval,
            generated_at=cast("str", patch_plan["generated_at"]),
            diff_path=diff_path,
            confidentiality=confidentiality,
            limits=limits,
        )
    except ContractError as exc:
        if exc.code is ErrorCode.HASH_SOURCE_MISMATCH:
            raise ContractError(
                ErrorCode.PATCH_SOURCE_DRIFT,
                "source no longer satisfies the PatchPlan",
            ) from exc
        raise
    if expected_plan.document != dict(patch_plan) or expected_plan.unified_diff != unified_diff:
        raise ContractError(
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
            "PatchPlan is not reproducible from its authorized inputs",
        )

    source, target = ensure_disjoint_roots(source_root, destination)
    source_files, source_inventory_sha256 = _inventory(source, max_file_bytes=limits.max_file_bytes)
    if APPLY_MARKER in source_files:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source contains the reserved apply marker")
    operations = cast("Sequence[Mapping[str, Any]]", payload["operations"])
    operation_paths = {
        cast("str", cast("Mapping[str, Any]", item["target"])["path"]) for item in operations
    }
    operation_originals = {path: source_files[path] for path in operation_paths}
    operation_revised = apply_operations_to_bytes(operation_originals, operations)
    if build_unified_diff(operation_originals, operation_revised) != unified_diff:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "actual diff differs from PatchPlan")
    expected_files = dict(source_files)
    expected_files.update(operation_revised)
    marker = _marker(patch_plan, unified_diff, expected_files)

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if not target.is_dir() or target.is_symlink():
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "apply destination conflicts")
        _verify_output(
            target,
            marker,
            expected_files,
            max_file_bytes=limits.max_file_bytes,
        )
        final_source_files, final_source_sha256 = _inventory(
            source, max_file_bytes=limits.max_file_bytes
        )
        if final_source_sha256 != source_inventory_sha256 or final_source_files != source_files:
            raise ContractError(
                ErrorCode.PATCH_SOURCE_DRIFT, "source changed during idempotent reuse"
            )
        return ApplyResult(
            patch_plan_sha256=cast("str", marker["patch_plan_sha256"]),
            source_tree_sha256=cast("str", marker["source_tree_sha256"]),
            unified_diff_sha256=cast("str", marker["unified_diff_sha256"]),
            output_file_count=len(expected_files),
            reused=True,
        )

    prefix = f".{target.name}.apply-"
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=target.parent))
    published = False
    try:
        for path, data in sorted(source_files.items()):
            target_file = staged.joinpath(*validate_relative_path(path).split("/"))
            target_file.parent.mkdir(parents=True, exist_ok=True)
            _write_file(target_file, data)
        for path, data in operation_revised.items():
            target_file = staged.joinpath(*validate_relative_path(path).split("/"))
            _write_file(target_file, data)
        _write_file(staged / APPLY_MARKER, canonical_json(marker) + b"\n")
        _verify_output(
            staged,
            marker,
            expected_files,
            max_file_bytes=limits.max_file_bytes,
        )
        current_source_files, current_source_sha256 = _inventory(
            source, max_file_bytes=limits.max_file_bytes
        )
        if current_source_sha256 != source_inventory_sha256 or current_source_files != source_files:
            raise ContractError(ErrorCode.PATCH_SOURCE_DRIFT, "source changed before publication")
        _publish_directory(staged, target)
        published = True
        _verify_output(
            target,
            marker,
            expected_files,
            max_file_bytes=limits.max_file_bytes,
        )
        final_source_files, final_source_sha256 = _inventory(
            source, max_file_bytes=limits.max_file_bytes
        )
        if final_source_sha256 != source_inventory_sha256 or final_source_files != source_files:
            raise ContractError(ErrorCode.PATCH_SOURCE_DRIFT, "source changed during publication")
    except Exception:
        if not published and staged.exists():
            _remove_owned_tree(staged, target.parent, prefix)
        # Once the atomic rename succeeds, do not recursively remove the public
        # destination on a later verification error: another local process could
        # have replaced it.  The exception remains fail-closed for callers.
        raise
    return ApplyResult(
        patch_plan_sha256=cast("str", marker["patch_plan_sha256"]),
        source_tree_sha256=cast("str", marker["source_tree_sha256"]),
        unified_diff_sha256=cast("str", marker["unified_diff_sha256"]),
        output_file_count=len(expected_files),
        reused=False,
    )


__all__ = ["APPLY_MARKER", "ApplyResult", "apply_patch_plan"]
