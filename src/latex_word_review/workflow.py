"""Conservative, fixed-layout orchestration for the Windows review workflow.

The domain objects and safety gates remain in their dedicated library modules.
This module only removes repetitive path plumbing: every operation targets a
new fixed-layout run directory, validates all upstream bindings, and never
approves or applies a change on the user's behalf.
"""

from __future__ import annotations

import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from latex_word_review.backends import BackendRequest, PandocBackend, Tex2WordBackend
from latex_word_review.canonical import compute_payload_sha256
from latex_word_review.contracts import load_contract_json
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import ExportBindings, export_review_docx
from latex_word_review.hashing import FileDigest, digest_file, read_stable_bytes
from latex_word_review.ids import new_run_id
from latex_word_review.image_materializer import verify_image_cache_entry
from latex_word_review.image_overlay import IMAGE_OVERLAY_FORMAT, IMAGE_OVERLAY_MANIFEST
from latex_word_review.ingest import archive_returned_docx, verify_returned_archive
from latex_word_review.jsonio import read_contract_file, write_new_json
from latex_word_review.paths import ensure_disjoint_roots, resolve_within, validate_relative_path
from latex_word_review.revisions import build_changeset
from latex_word_review.snapshot import SNAPSHOT_MANIFEST, snapshot_project
from latex_word_review.workflow_objects import (
    bookmark_bindings_from_source_map,
    build_backend_capabilities_document,
    build_export_report_document,
    build_review_ir_document,
    build_revision_reader_capabilities_document,
    build_source_manifest_document,
    build_source_map_document,
    utc_now,
)

Confidentiality = Literal["public_fixture", "local_private", "derived_private"]
BackendName = Literal["tex2word", "pandoc"]

_SOURCE_MANIFEST = "objects/source-manifest.json"
_SNAPSHOT = "snapshot"
_EXPORT = "export"
_EXPORT_DOCX = "export/review.docx"
_EXPORT_OBJECTS = "export/objects"
_EXPORT_IMAGE_OVERLAY = "export/review.docx.image-overlay"
_EXPORT_IMAGE_MANIFEST = f"{_EXPORT_IMAGE_OVERLAY}/{IMAGE_OVERLAY_MANIFEST}"
_RECEIVE = "receive"
_RETURNED_ARCHIVE = "receive/original"
_READER = "receive/revision-reader.json"
_CHANGESET = "receive/changeset.json"
_STAGING = ".lwr-staging"
_MAX_DOCX_BYTES = 128 * 1024 * 1024
_MAX_CONTRACT_BYTES = 16 * 1024 * 1024
_STAGE_PREFIXES = (
    ".lwr-stage-export-",
    ".lwr-stage-receive-",
    ".lwr-failed-export-",
    ".lwr-failed-receive-",
)
_OWNED_STAGE_RE = re.compile(r"^\.lwr-(?:stage|failed)-(?:export|receive)-[A-Za-z0-9_-]{6,64}$")


@dataclass(frozen=True, slots=True)
class _CoreState:
    root: Path
    source_manifest: dict[str, Any]
    source_manifest_sha256: str
    discovery: ProjectDiscovery
    run_id: str


@dataclass(frozen=True, slots=True)
class _ExportState:
    source_map: dict[str, Any]
    source_map_sha256: str
    counts: dict[str, int]


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    try:
        return path.is_symlink() or bool(junction_probe is not None and junction_probe())
    except OSError as exc:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "path link status is unavailable") from exc


def _require_real_directory(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, f"{label} must not be a link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} is unavailable") from exc
    if not resolved.is_dir() or _is_link_or_junction(resolved):
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} must be a directory")
    return resolved


def _require_regular_file(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, f"{label} must not be a link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} is unavailable") from exc
    if not resolved.is_file() or _is_link_or_junction(resolved):
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} must be a regular file")
    return resolved


def _tree_entries_without_links(root: Path) -> list[Path]:
    """Return a bounded-by-filesystem tree walk after rejecting reparse points."""

    entries: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "workflow tree cannot be inspected",
            ) from exc
        for child in children:
            if _is_link_or_junction(child):
                raise ContractError(
                    ErrorCode.PATH_LINK_ESCAPE,
                    "workflow tree contains a symlink or junction",
                )
            entries.append(child)
            try:
                if child.is_dir():
                    pending.append(child)
                elif not child.is_file():
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID,
                        "workflow tree contains a non-file entry",
                    )
            except OSError as exc:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "workflow tree entry cannot be inspected",
                ) from exc
    return entries


def _is_owned_stage_name(name: str) -> bool:
    return _OWNED_STAGE_RE.fullmatch(name) is not None


def _seal_file(path: Path) -> None:
    _require_regular_file(path, "sealed workflow artifact")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "workflow artifact could not be made read-only",
        ) from exc


def _seal_tree_files(root: Path) -> None:
    for entry in _tree_entries_without_links(root):
        if entry.is_file():
            _seal_file(entry)


def _require_read_only(path: Path, label: str) -> None:
    target = _require_regular_file(path, label)
    try:
        writable = bool(target.stat().st_mode & stat.S_IWUSR)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} cannot be inspected") from exc
    if writable:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, f"{label} is no longer read-only")


def _make_tree_writable(root: Path) -> None:
    entries = _tree_entries_without_links(root)
    for path in reversed(entries):
        try:
            if path.is_dir():
                path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            else:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "owned workflow stage could not be prepared for cleanup",
            ) from exc
    try:
        root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "owned workflow stage could not be prepared for cleanup",
        ) from exc


def _remove_owned_tree(path: Path, parent: Path, *, prefixes: tuple[str, ...]) -> None:
    if not any(path.name.startswith(prefix) for prefix in prefixes):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "refusing to remove an unowned tree")
    resolved_parent = _require_real_directory(parent, "workflow stage parent")
    try:
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow stage is unavailable") from exc
    if resolved_path.parent != resolved_parent or _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "workflow stage escaped its parent")
    _make_tree_writable(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "workflow stage cleanup failed") from exc


def _prepare_stage(root: Path, operation: Literal["export", "receive"]) -> tuple[Path, Path]:
    staging = root / _STAGING
    if staging.exists() or staging.is_symlink():
        staging = _require_real_directory(staging, "workflow staging directory")
    else:
        try:
            staging.mkdir()
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "workflow staging directory could not be created",
            ) from exc
    stage = Path(tempfile.mkdtemp(prefix=f".lwr-stage-{operation}-", dir=staging))
    payload = stage / "payload"
    payload.mkdir()
    return stage, payload


def _publish_payload(payload: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow output already exists")
    expected_staging = destination.parent.resolve(strict=True) / _STAGING
    if payload.parent.parent.resolve(strict=True) != expected_staging:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "workflow payload is not owned")
    try:
        payload.rename(destination)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "workflow output could not be published atomically",
        ) from exc


def _initialize_result(core: _CoreState) -> dict[str, Any]:
    return {
        "run_id": core.run_id,
        "phase": "snapshotted",
        "integrity": "workflow_bindings_verified",
        "counts": {
            "source_files": len(core.discovery.files),
            "source_bytes": sum(item.size_bytes for item in core.discovery.files),
        },
        "next_action": "export the immutable Word review baseline",
        "next_command": "latex-word-review workflow export .",
        "working_directory": "run_root",
    }


def initialize_workflow(
    source: Path,
    run_root: Path,
    *,
    main_document: str | None = None,
    confidentiality: Confidentiality = "derived_private",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Atomically create a new fixed-layout run with an immutable snapshot."""

    if confidentiality not in {"public_fixture", "local_private", "derived_private"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid workflow confidentiality")
    source_root = _require_real_directory(source, "source project")
    target = run_root.absolute()
    if not target.name or target.name in {".", ".."}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "run root name is invalid")
    if target.exists() or target.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "run root already exists")
    # The source and target must be siblings or otherwise disjoint; this also
    # rejects links or junctions in the destination path.
    _, target = ensure_disjoint_roots(source_root, target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "run root parent is unavailable") from exc
    parent = _require_real_directory(target.parent, "run root parent")
    prefix = f".{target.name}.lwr-init-"
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    published = False
    try:
        snapshot = staged / _SNAPSHOT
        run_id = new_run_id()
        result = snapshot_project(source_root, snapshot, main_document=main_document)
        discovery = discover_project(snapshot, main_document=result.main_document)
        objects = staged / "objects"
        objects.mkdir()
        source_manifest = build_source_manifest_document(
            discovery,
            run_id=run_id,
            snapshot_manifest_file=snapshot / SNAPSHOT_MANIFEST,
            snapshot_artifact_path=f"{_SNAPSHOT}/{SNAPSHOT_MANIFEST}",
            confidentiality=confidentiality,
            generated_at=generated_at,
        )
        source_manifest_path = staged / _SOURCE_MANIFEST
        write_new_json(source_manifest_path, source_manifest, contract=True)
        _seal_file(source_manifest_path)
        (staged / _STAGING).mkdir()
        try:
            staged.rename(target)
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "run root could not be published atomically",
            ) from exc
        published = True
        core = _load_core(target)
        return _initialize_result(core)
    finally:
        if not published and staged.exists():
            _remove_owned_tree(staged, parent, prefixes=(prefix,))


def _load_core(run_root: Path) -> _CoreState:
    root = _require_real_directory(run_root, "run root")
    source_manifest_path = root / _SOURCE_MANIFEST
    snapshot_root = _require_real_directory(root / _SNAPSHOT, "source snapshot")
    try:
        snapshot_root_writable = bool(snapshot_root.stat().st_mode & stat.S_IWUSR)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "snapshot root cannot be inspected") from exc
    if snapshot_root_writable:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source snapshot root is no longer read-only",
        )
    snapshot_entries = _tree_entries_without_links(snapshot_root)
    for entry in snapshot_entries:
        try:
            writable = bool(entry.stat().st_mode & stat.S_IWUSR)
        except OSError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "snapshot entry cannot be inspected",
            ) from exc
        if writable:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source snapshot is no longer read-only",
            )
    _require_read_only(source_manifest_path, "SourceManifest")
    source_manifest = read_contract_file(
        source_manifest_path,
        expected_schema="SourceManifest",
        max_bytes=_MAX_CONTRACT_BYTES,
    )
    payload = cast("dict[str, Any]", source_manifest["payload"])
    artifact = cast("dict[str, Any]", payload["snapshot_artifact"])
    if artifact["path_base"] != "run_root" or artifact["path"] != (
        f"{_SNAPSHOT}/{SNAPSHOT_MANIFEST}"
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot artifact path differs")
    snapshot_manifest = _require_regular_file(
        snapshot_root / SNAPSHOT_MANIFEST,
        "snapshot manifest",
    )
    snapshot_digest = digest_file(snapshot_manifest, max_bytes=_MAX_CONTRACT_BYTES)
    expected_snapshot = FileDigest(
        cast("int", artifact["size_bytes"]),
        cast("str", artifact["sha256"]),
    )
    if snapshot_digest != expected_snapshot:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot manifest hash differs")
    main_document = cast("str", payload["main_document"])
    discovery = discover_project(snapshot_root, main_document=main_document)
    if discovery.source_tree_sha256 != payload["source_tree_sha256"]:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot source tree hash differs")
    if [item.as_dict() for item in discovery.files] != payload["files"]:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot file inventory differs")
    run_id = cast("str", source_manifest["run_id"])
    return _CoreState(
        root=root,
        source_manifest=source_manifest,
        source_manifest_sha256=compute_payload_sha256(source_manifest),
        discovery=discovery,
        run_id=run_id,
    )


def _write_export_objects(
    core: _CoreState,
    payload: Path,
    *,
    backend_name: BackendName,
    timeout_s: float,
    confidentiality: Confidentiality,
    generated_at: str | None,
) -> dict[str, int]:
    if timeout_s <= 0 or timeout_s > 3600:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "export timeout must be in (0, 3600]")
    objects = payload / "objects"
    objects.mkdir()
    output = payload / "review.docx"
    backend = Tex2WordBackend() if backend_name == "tex2word" else PandocBackend()
    source_payload = cast("dict[str, Any]", core.source_manifest["payload"])
    outcome = export_review_docx(
        backend,
        BackendRequest(
            core.root / _SNAPSHOT,
            cast("str", source_payload["main_document"]),
            output,
            expected_source_tree_sha256=core.discovery.source_tree_sha256,
            timeout_s=timeout_s,
        ),
        core.discovery,
        ExportBindings(
            source_manifest_sha256=core.source_manifest_sha256,
            artifact_path=_EXPORT_DOCX,
            confidentiality=confidentiality,
        ),
    )
    timestamp = generated_at or utc_now()
    capabilities = build_backend_capabilities_document(
        outcome.backend_result.capabilities,
        run_id=core.run_id,
        generated_at=timestamp,
    )
    report = build_export_report_document(outcome, run_id=core.run_id, generated_at=timestamp)
    write_new_json(objects / "backend-capabilities.json", capabilities, contract=True)
    if outcome.output_path is None or outcome.anchoring is None:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "workflow export did not produce a review DOCX",
            details={"export_status": outcome.report.status},
        )
    if outcome.report.status != "success":
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "workflow export requires a fully successful report",
            details={
                "export_status": outcome.report.status,
                "finding_count": len(outcome.report.findings),
            },
        )
    review_ir = build_review_ir_document(
        core.discovery,
        outcome,
        run_id=core.run_id,
        source_manifest_sha256=core.source_manifest_sha256,
        generated_at=timestamp,
    )
    source_map = build_source_map_document(
        outcome.anchoring,
        run_id=core.run_id,
        source_manifest_sha256=core.source_manifest_sha256,
        review_ir_sha256=compute_payload_sha256(review_ir),
        generated_at=timestamp,
    )
    write_new_json(objects / "review-ir.json", review_ir, contract=True)
    write_new_json(objects / "source-map.json", source_map, contract=True)
    write_new_json(objects / "export-report.json", report, contract=True)
    coverage = cast("dict[str, int]", cast("dict[str, Any]", source_map["payload"])["coverage"])
    return {
        "review_units": coverage["total"],
        "exact_mappings": coverage["exact"],
        "export_findings": len(outcome.report.findings),
    }


def export_workflow(
    run_root: Path,
    *,
    backend: BackendName = "tex2word",
    timeout_s: float = 60.0,
    confidentiality: Confidentiality = "derived_private",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Create and seal the immutable review baseline under ``export/``."""

    if backend not in {"tex2word", "pandoc"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "unsupported workflow backend")
    if confidentiality not in {"public_fixture", "local_private", "derived_private"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid workflow confidentiality")
    core = _load_core(run_root)
    destination = core.root / _EXPORT
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow export already exists")
    stage, payload = _prepare_stage(core.root, "export")
    try:
        _write_export_objects(
            core,
            payload,
            backend_name=backend,
            timeout_s=timeout_s,
            confidentiality=confidentiality,
            generated_at=generated_at,
        )
        _seal_tree_files(payload)
        _publish_payload(payload, destination)
        state = _validate_export(core)
        return {
            "run_id": core.run_id,
            "phase": "exported",
            "integrity": "workflow_bindings_verified",
            "counts": state.counts,
            "next_action": "send export/review.docx for Word Track Changes review",
            "next_command": "latex-word-review workflow receive . <returned.docx>",
            "working_directory": "run_root",
        }
    finally:
        if stage.exists():
            _remove_owned_tree(stage, stage.parent, prefixes=(".lwr-stage-export-",))


def _validate_export(core: _CoreState) -> _ExportState:
    export_root = _require_real_directory(core.root / _EXPORT, "workflow export")
    _tree_entries_without_links(export_root)
    paths = {
        "review": export_root / "review.docx",
        "capabilities": export_root / "objects/backend-capabilities.json",
        "review_ir": export_root / "objects/review-ir.json",
        "source_map": export_root / "objects/source-map.json",
        "report": export_root / "objects/export-report.json",
    }
    for label, path in paths.items():
        _require_read_only(path, f"export {label}")
    capabilities = read_contract_file(paths["capabilities"], expected_schema="BackendCapabilities")
    review_ir = read_contract_file(paths["review_ir"], expected_schema="ReviewIR")
    source_map = read_contract_file(paths["source_map"], expected_schema="SourceMap")
    report = read_contract_file(paths["report"], expected_schema="ExportReport")
    for document in (capabilities, review_ir, source_map, report):
        if document["run_id"] != core.run_id:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export run binding differs")
    capabilities_payload = cast("dict[str, Any]", capabilities["payload"])
    if capabilities_payload["backend_role"] != "export":
        raise ContractError(ErrorCode.BACKEND_CAPABILITY_MISSING, "export backend role differs")
    review_payload = cast("dict[str, Any]", review_ir["payload"])
    map_payload = cast("dict[str, Any]", source_map["payload"])
    report_payload = cast("dict[str, Any]", report["payload"])
    if review_payload["source_manifest_sha256"] != core.source_manifest_sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "ReviewIR source binding differs")
    if map_payload["source_manifest_sha256"] != core.source_manifest_sha256 or map_payload[
        "review_ir_sha256"
    ] != compute_payload_sha256(review_ir):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "SourceMap binding differs")
    _validate_image_overlay(core, export_root, map_payload, report_payload)
    review_digest = digest_file(paths["review"], max_bytes=_MAX_DOCX_BYTES)
    artifact = cast("dict[str, Any]", report_payload["review_docx"])
    expected_review = FileDigest(
        cast("int", artifact["size_bytes"]), cast("str", artifact["sha256"])
    )
    if (
        report_payload["status"] != "success"
        or report_payload["source_manifest_sha256"] != core.source_manifest_sha256
        or report_payload["backend_capabilities_sha256"] != compute_payload_sha256(capabilities)
        or report_payload["review_ir_sha256"] != compute_payload_sha256(review_ir)
        or report_payload["source_map_sha256"] != compute_payload_sha256(source_map)
        or artifact["path_base"] != "run_root"
        or artifact["path"] != _EXPORT_DOCX
        or artifact["immutable"] is not True
        or review_digest != expected_review
        or map_payload["review_docx_sha256"] != review_digest.sha256
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export object binding differs")
    coverage = cast("dict[str, int]", map_payload["coverage"])
    counts = {
        "source_files": len(core.discovery.files),
        "review_units": coverage["total"],
        "exact_mappings": coverage["exact"],
        "export_findings": len(cast("list[Any]", report_payload["findings"])),
    }
    return _ExportState(
        source_map=source_map,
        source_map_sha256=compute_payload_sha256(source_map),
        counts=counts,
    )


def _validate_image_overlay(
    core: _CoreState,
    export_root: Path,
    map_payload: dict[str, Any],
    report_payload: dict[str, Any],
) -> None:
    """Recompute the sealed image-overlay evidence used by the DOCX export."""

    map_binding = cast("dict[str, Any]", map_payload["image_overlay"])
    report_binding = cast("dict[str, Any]", report_payload["image_overlay"])
    if map_binding != report_binding or map_binding["status"] != "ready":
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "image-overlay object bindings differ",
        )
    manifest_ref = cast("dict[str, Any]", map_binding["manifest"])
    if (
        manifest_ref["path_base"] != "run_root"
        or manifest_ref["path"] != _EXPORT_IMAGE_MANIFEST
        or manifest_ref["role"] != "image_overlay_manifest"
        or manifest_ref["media_type"] != "application/json"
        or manifest_ref["immutable"] is not True
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "image-overlay manifest reference differs",
        )

    overlay_root = _require_real_directory(
        export_root / "review.docx.image-overlay",
        "workflow image overlay",
    )
    for entry in _tree_entries_without_links(overlay_root):
        if entry.is_file():
            _require_read_only(entry, "workflow image-overlay artifact")
    manifest_path = core.root / _EXPORT_IMAGE_MANIFEST
    _require_read_only(manifest_path, "workflow image-overlay manifest")
    manifest_bytes = read_stable_bytes(manifest_path, max_bytes=_MAX_CONTRACT_BYTES)
    manifest_digest = digest_file(manifest_path, max_bytes=_MAX_CONTRACT_BYTES)
    if manifest_digest != FileDigest(
        cast("int", manifest_ref["size_bytes"]),
        cast("str", manifest_ref["sha256"]),
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "image-overlay manifest digest differs",
        )
    manifest = load_contract_json(manifest_bytes)
    source = manifest.get("source")
    derived = manifest.get("derived")
    counts = manifest.get("counts")
    entries = manifest.get("entries")
    if (
        manifest.get("format") != IMAGE_OVERLAY_FORMAT
        or manifest.get("status") != "ready"
        or not isinstance(source, dict)
        or not isinstance(derived, dict)
        or not isinstance(counts, dict)
        or not isinstance(entries, list)
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "image-overlay manifest shape differs",
        )
    derived_discovery = discover_project(
        overlay_root,
        main_document=core.discovery.main_document,
    )
    if (
        map_binding["original_source_tree_sha256"] != core.discovery.source_tree_sha256
        or source.get("source_tree_sha256") != core.discovery.source_tree_sha256
        or source.get("profile_sha256") != core.discovery.profile_sha256
        or map_binding["derived_source_tree_sha256"] != derived_discovery.source_tree_sha256
        or derived.get("source_tree_sha256") != derived_discovery.source_tree_sha256
        or derived.get("profile_sha256") != derived_discovery.profile_sha256
        or derived.get("main_document") != core.discovery.main_document
        or derived.get("image_root") != "lwr-images"
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "image-overlay source-tree binding differs",
        )
    for field in (
        "source_image_instances",
        "materialized_pdf_instances",
        "passthrough_raster_instances",
    ):
        if map_binding[field] != counts.get(field):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "image-overlay count binding differs",
            )
    if counts.get("manual_instances") != 0:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "ready image overlay contains manual instances",
        )
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "materialized_pdf":
            continue
        materialized = entry.get("materialized")
        if not isinstance(materialized, dict):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "materialized image evidence is missing",
            )
        _validate_materialized_image(overlay_root, entry)


def _validate_materialized_image(
    overlay_root: Path,
    entry: dict[str, Any],
) -> None:
    materialized = entry.get("materialized")
    request = entry.get("request")
    expected_fields = {
        "cache_key_sha256",
        "request_sha256",
        "png_path",
        "cache_manifest_path",
        "png_sha256",
        "pixel_sha256",
        "width_px",
        "height_px",
        "dpi",
        "renderer",
        "reused",
    }
    if (
        not isinstance(materialized, dict)
        or set(materialized) != expected_fields
        or not isinstance(request, dict)
        or not isinstance(materialized.get("renderer"), dict)
        or type(materialized.get("reused")) is not bool
        or entry.get("request_sha256") != materialized.get("request_sha256")
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "materialized image evidence shape differs",
        )
    try:
        png_relative = validate_relative_path(cast("str", materialized["png_path"]))
        cache_relative = validate_relative_path(cast("str", materialized["cache_manifest_path"]))
    except (ContractError, KeyError, TypeError) as exc:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "materialized image paths differ",
        ) from exc
    png_path = resolve_within(overlay_root, png_relative)
    cache_path = resolve_within(overlay_root, cache_relative)
    _require_read_only(png_path, "materialized overlay PNG")
    _require_read_only(cache_path, "materialized image cache manifest")
    cache_prefix = "lwr-images/"
    if (
        not png_relative.startswith(cache_prefix)
        or not cache_relative.startswith(cache_prefix)
        or png_path.parent.resolve(strict=True) != cache_path.parent.resolve(strict=True)
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "materialized image cache target differs",
        )
    try:
        verified = verify_image_cache_entry(
            cache_path.parent,
            request=cast("dict[str, Any]", request),
            renderer=cast("dict[str, Any]", materialized["renderer"]),
        )
    except ContractError as exc:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "materialized image cache verification failed",
            details={"cause": exc.code.value},
        ) from exc
    if (
        verified.cache_key_sha256 != materialized["cache_key_sha256"]
        or verified.request_sha256 != materialized["request_sha256"]
        or f"{cache_prefix}{verified.png_path}" != png_relative
        or f"{cache_prefix}{verified.manifest_path}" != cache_relative
        or verified.png_sha256 != materialized["png_sha256"]
        or verified.pixel_sha256 != materialized["pixel_sha256"]
        or verified.width_px != materialized["width_px"]
        or verified.height_px != materialized["height_px"]
        or verified.dpi != materialized["dpi"]
        or verified.renderer.as_dict() != materialized["renderer"]
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "materialized image cache binding differs",
        )


def receive_workflow(
    run_root: Path,
    returned_docx: Path,
    *,
    confidentiality: Confidentiality = "local_private",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Archive a returned original and ingest it against the export baseline."""

    if confidentiality not in {"public_fixture", "local_private", "derived_private"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid workflow confidentiality")
    core = _load_core(run_root)
    export_state = _validate_export(core)
    source = _require_regular_file(returned_docx, "returned Word original")
    destination = core.root / _RECEIVE
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow receive output already exists")
    stage, payload = _prepare_stage(core.root, "receive")
    try:
        map_payload = cast("dict[str, Any]", export_state.source_map["payload"])
        archive = archive_returned_docx(
            source,
            payload / "original",
            run_id=core.run_id,
            exported_docx_sha256=cast("str", map_payload["review_docx_sha256"]),
            confidentiality=confidentiality,
        )
        timestamp = generated_at or utc_now()
        reader = build_revision_reader_capabilities_document(
            run_id=core.run_id,
            generated_at=timestamp,
        )
        reader_path = payload / "revision-reader.json"
        write_new_json(reader_path, reader, contract=True)
        changeset = build_changeset(
            archive.docx_path,
            export_baseline_path=core.root / _EXPORT_DOCX,
            export_baseline_sha256=cast("str", map_payload["review_docx_sha256"]),
            run_id=core.run_id,
            source_manifest_sha256=core.source_manifest_sha256,
            source_map_sha256=export_state.source_map_sha256,
            revision_reader_capabilities_sha256=compute_payload_sha256(reader),
            returned_artifact_path=f"{_RECEIVE}/original/returned-original.docx",
            confidentiality=confidentiality,
            bookmark_bindings=bookmark_bindings_from_source_map(export_state.source_map),
            generated_at=timestamp,
        )
        write_new_json(payload / "changeset.json", changeset, contract=True)
        _seal_tree_files(payload)
        _publish_payload(payload, destination)
        counts = _validate_receive(core, export_state)
        changeset_payload = cast("dict[str, Any]", changeset["payload"])
        baseline = cast("dict[str, Any]", changeset_payload["baseline_verification"])
        return {
            "run_id": core.run_id,
            "phase": "ingested",
            "integrity": "workflow_bindings_verified",
            "baseline_verification": baseline,
            "manual_integrity_review_required": baseline["manual_integrity_review_required"],
            "counts": counts,
            "next_action": "create an ApprovalSet and decide each change; apply remains disabled",
            "next_command": (
                "latex-word-review approve init receive/changeset.json "
                "approvals/approval-r1.json --actor-id <id> --actor-name <name>"
            ),
            "working_directory": "run_root",
            "apply_enabled": False,
        }
    finally:
        if stage.exists():
            _remove_owned_tree(stage, stage.parent, prefixes=(".lwr-stage-receive-",))


def _validate_receive(core: _CoreState, export_state: _ExportState) -> dict[str, int]:
    receive_root = _require_real_directory(core.root / _RECEIVE, "workflow receive output")
    _tree_entries_without_links(receive_root)
    reader_path = receive_root / "revision-reader.json"
    changeset_path = receive_root / "changeset.json"
    _require_read_only(reader_path, "revision reader capabilities")
    _require_read_only(changeset_path, "ChangeSet")
    archive = verify_returned_archive(receive_root / "original", expected_run_id=core.run_id)
    _require_read_only(archive.docx_path, "returned Word original archive")
    _require_read_only(archive.manifest_path, "returned Word archive manifest")
    reader = read_contract_file(reader_path, expected_schema="BackendCapabilities")
    changeset = read_contract_file(changeset_path, expected_schema="ChangeSet")
    if reader["run_id"] != core.run_id or changeset["run_id"] != core.run_id:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "receive run binding differs")
    reader_payload = cast("dict[str, Any]", reader["payload"])
    if reader_payload["backend_role"] != "revision_reader":
        raise ContractError(
            ErrorCode.BACKEND_CAPABILITY_MISSING,
            "revision reader role differs",
        )
    payload = cast("dict[str, Any]", changeset["payload"])
    returned = cast("dict[str, Any]", payload["returned_original"])
    baseline = cast("dict[str, Any]", payload["baseline_verification"])
    source_map_payload = cast("dict[str, Any]", export_state.source_map["payload"])
    if (
        archive.exported_docx_sha256 != source_map_payload["review_docx_sha256"]
        or payload["source_manifest_sha256"] != core.source_manifest_sha256
        or payload["source_map_sha256"] != export_state.source_map_sha256
        or payload["revision_reader_capabilities_sha256"] != compute_payload_sha256(reader)
        or returned["path_base"] != "run_root"
        or returned["path"] != f"{_RECEIVE}/original/returned-original.docx"
        or returned["sha256"] != archive.returned_docx_sha256
        or returned["size_bytes"] != archive.size_bytes
        or returned["immutable"] is not True
        or baseline["status"] != "verified_for_text_patch"
        or baseline["automatic_patch_scope"] != "plain_text_only"
        or baseline["manual_integrity_review_required"] is not True
        or baseline["exported_docx_sha256"] != source_map_payload["review_docx_sha256"]
        or baseline["returned_original_docx_sha256"] != archive.returned_docx_sha256
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "receive object binding differs")
    counts = cast("dict[str, int]", payload["counts"])
    return {
        "source_files": len(core.discovery.files),
        "review_units": export_state.counts["review_units"],
        "raw_events": counts["raw_events"],
        "changes": counts["changes"],
        "unmatched": counts["unmatched"],
        "conflicts": counts["conflicts"],
    }


def _collect_stage_candidates(root: Path) -> tuple[list[tuple[Path, int]], int]:
    staging = root / _STAGING
    if not staging.exists() and not staging.is_symlink():
        return [], 0
    staging = _require_real_directory(staging, "workflow staging directory")
    candidates: list[tuple[Path, int]] = []
    ignored = 0
    try:
        children = sorted(staging.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow stages cannot be listed") from exc
    for child in children:
        if not _is_owned_stage_name(child.name):
            ignored += 1
            continue
        if _is_link_or_junction(child) or not child.is_dir():
            raise ContractError(
                ErrorCode.PATH_LINK_ESCAPE,
                "owned workflow stage must be a real directory",
            )
        entries = _tree_entries_without_links(child)
        size_bytes = 0
        for entry in entries:
            if entry.is_file():
                try:
                    size_bytes += entry.stat().st_size
                except OSError as exc:
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID,
                        "workflow stage size cannot be inspected",
                    ) from exc
        candidates.append((child, size_bytes))
    return candidates, ignored


def workflow_status(run_root: Path) -> dict[str, Any]:
    """Read and cross-check every object that exists in one workflow run."""

    core = _load_core(run_root)
    candidates, ignored = _collect_stage_candidates(core.root)
    counts: dict[str, int] = {
        "source_files": len(core.discovery.files),
        "stale_stages": len(candidates),
        "ignored_staging_entries": ignored,
    }
    export_exists = (core.root / _EXPORT).exists() or (core.root / _EXPORT).is_symlink()
    receive_exists = (core.root / _RECEIVE).exists() or (core.root / _RECEIVE).is_symlink()
    baseline: dict[str, Any] | None = None
    if receive_exists and not export_exists:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "receive exists without export")
    if receive_exists:
        export_state = _validate_export(core)
        counts.update(_validate_receive(core, export_state))
        changeset = read_contract_file(
            core.root / _RECEIVE / "changeset.json",
            expected_schema="ChangeSet",
        )
        changeset_payload = cast("dict[str, Any]", changeset["payload"])
        baseline = cast("dict[str, Any]", changeset_payload["baseline_verification"])
        phase = "ingested"
        next_action = "create an ApprovalSet and decide each change; no source has been modified"
        next_command = (
            "latex-word-review approve init receive/changeset.json "
            "approvals/approval-r1.json --actor-id <id> --actor-name <name>"
        )
    elif export_exists:
        export_state = _validate_export(core)
        counts.update(export_state.counts)
        phase = "exported"
        next_action = "send export/review.docx for review, then archive the returned copy"
        next_command = "latex-word-review workflow receive . <returned.docx>"
    else:
        phase = "snapshotted"
        next_action = "export the immutable Word review baseline"
        next_command = "latex-word-review workflow export ."
    result: dict[str, Any] = {
        "run_id": core.run_id,
        "phase": phase,
        "integrity": "workflow_bindings_verified",
        "counts": counts,
        "next_action": next_action,
        "next_command": next_command,
        "working_directory": "run_root",
        "apply_enabled": False,
    }
    if baseline is not None:
        result["baseline_verification"] = baseline
        result["manual_integrity_review_required"] = baseline["manual_integrity_review_required"]
    return result


def clean_workflow(run_root: Path, *, execute: bool = False) -> dict[str, Any]:
    """List or remove only allowlisted tool-owned staging directories."""

    core = _load_core(run_root)
    candidates, ignored = _collect_stage_candidates(core.root)
    paths = [f"{_STAGING}/{path.name}" for path, _size in candidates]
    total_bytes = sum(size for _path, size in candidates)
    removed = 0
    if execute:
        for path, _size in candidates:
            _remove_owned_tree(path, path.parent, prefixes=_STAGE_PREFIXES)
            removed += 1
    return {
        "run_id": core.run_id,
        "dry_run": not execute,
        "candidate_count": len(candidates),
        "candidate_bytes": total_bytes,
        "removed_count": removed,
        "ignored_staging_entries": ignored,
        "candidates": paths,
        "protected": [
            _SNAPSHOT,
            _SOURCE_MANIFEST,
            _EXPORT,
            _RECEIVE,
            "run_root",
            "user_source",
        ],
    }


__all__ = [
    "clean_workflow",
    "export_workflow",
    "initialize_workflow",
    "receive_workflow",
    "workflow_status",
]
