"""Conservative, fixed-layout orchestration for the Windows review workflow.

The domain objects and safety gates remain in their dedicated library modules.
This module only removes repetitive path plumbing: every operation targets a
new fixed-layout run directory, validates all upstream bindings, and never
approves or applies a change on the user's behalf.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.atomic_publish import publish_new_directory
from latex_word_review.backends import (
    BackendRequest,
    ExportBackend,
    PandocBackend,
    Tex2WordBackend,
)
from latex_word_review.canonical import compute_payload_sha256
from latex_word_review.contracts import load_contract_json
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.docx_reader import DocxPackage, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import ExportBindings, ExportOutcome, export_review_docx
from latex_word_review.export_models import (
    ExportReport,
    ReviewDocxArtifact,
    export_report_commitment,
)
from latex_word_review.hashing import FileDigest, digest_file, read_stable_bytes
from latex_word_review.ids import derive_artifact_id, new_run_id
from latex_word_review.image_materializer import SealedImageCacheVerifier
from latex_word_review.image_overlay import IMAGE_OVERLAY_FORMAT, IMAGE_OVERLAY_MANIFEST
from latex_word_review.ingest import archive_returned_docx, verify_returned_archive
from latex_word_review.inspection import inspect_docx
from latex_word_review.jsonio import read_contract_file, write_new_json
from latex_word_review.paths import (
    ensure_disjoint_roots,
    resolve_within,
    validate_relative_path,
    windows_extended_path,
)
from latex_word_review.review_layout import apply_review_layout
from latex_word_review.revision_display import (
    inspect_revision_display,
    validate_revision_display,
)
from latex_word_review.revision_macros import RevisionMacroInventory, scan_revision_macros
from latex_word_review.revisions import build_changeset
from latex_word_review.snapshot import SNAPSHOT_MANIFEST, snapshot_project
from latex_word_review.source_features import (
    INVENTORY_PROFILE_VERSION,
    independently_observed_label_count,
    scan_source_features,
)
from latex_word_review.word_fields import finalize_word_fields
from latex_word_review.workflow_objects import (
    EXPORT_REPORT_INTERFACE_VERSION,
    LEGACY_SOURCE_MAP_INTERFACE_VERSION,
    PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
    SOURCE_MANIFEST_INTERFACE_VERSION,
    SOURCE_MAP_INTERFACE_VERSION,
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
_EXPORT_CHANGES_DISPLAY = "export/existing-changes-display.docx"
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
_WORD_SETTINGS_PART = "word/settings.xml"
_WORDPROCESSINGML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DISPLAY_MARKER_ROLE: Literal["latex_changes_display_docx"] = "latex_changes_display_docx"
_DISPLAY_MARKER_PROFILE = "lwr-existing-changes-display-v1"
_ARTIFACT_ROLE_DOCVAR = "LWR_ARTIFACT_ROLE"
_ARTIFACT_PROFILE_DOCVAR = "LWR_ARTIFACT_PROFILE"
_ARTIFACT_RUN_DOCVAR = "LWR_RUN_ID"
_ARTIFACT_MARKER_DOCVARS = (
    _ARTIFACT_ROLE_DOCVAR,
    _ARTIFACT_PROFILE_DOCVAR,
    _ARTIFACT_RUN_DOCVAR,
)
_SETTINGS_AFTER_DOCVARS = frozenset(
    {
        "rsids",
        "mathPr",
        "uiCompat97To2003",
        "attachedSchema",
        "themeFontLang",
        "clrSchemeMapping",
        "doNotIncludeSubdocsInStats",
        "doNotAutoCompressPictures",
        "forceUpgrade",
        "captions",
        "readModeInkLockDown",
        "smartTagType",
        "schemaLibrary",
        "shapeDefaults",
        "doNotEmbedSmartTags",
        "decimalSymbol",
        "listSeparator",
    }
)
_LEGACY_EXPORT_REPORT_INTERFACE_VERSION = "export-report-builder-v1"
_LEGACY_SOURCE_MANIFEST_INTERFACE_VERSION = "source-manifest-builder-v1"
_SOURCE_FEATURE_METRICS = {
    "images": "image_instances",
    "math": "math_objects",
    "tables": "table_instances",
    "references": "reference_instances",
    "labels": "label_instances",
    "citations": "citation_instances",
}
_SOURCE_FEATURE_AUX_METRICS = {
    "tables": ("non_equivalent_table_instances",),
    "references": ("non_equivalent_reference_instances",),
    "labels": ("dynamic_label_instances",),
}
_REQUIRED_SOURCE_FEATURE_METRICS = frozenset(
    {
        "source_feature_inventory_version",
        "source_tex_files",
        "inline_math_instances",
        "display_math_instances",
        "skipped_dynamic_regions",
        *_SOURCE_FEATURE_METRICS.values(),
        *(key for values in _SOURCE_FEATURE_AUX_METRICS.values() for key in values),
    }
)
_CURRENT_SOURCE_FEATURE_MARKER_METRICS = _REQUIRED_SOURCE_FEATURE_METRICS - {
    "source_feature_inventory_version",
    "image_instances",
}
_CURRENT_SOURCE_FEATURE_RESULT_MARKERS = frozenset(
    {"math", "tables", "references", "labels", "citations"}
)
_CRITICAL_SOURCE_FEATURES = frozenset({"images", "math", "tables", "references", "labels"})
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
    non_returnable_docx_digests: frozenset[FileDigest]


@dataclass(frozen=True, slots=True)
class _EmbeddedArtifactMarker:
    role: str
    profile: str
    run_id: str


def _word_tag(local: str) -> str:
    return f"{{{_WORDPROCESSINGML_NS}}}{local}"


def _invalid_artifact_marker(code: ErrorCode, message: str) -> ContractError:
    return ContractError(code, message)


def _artifact_marker_from_package(
    package: DocxPackage,
    *,
    invalid_code: ErrorCode,
) -> _EmbeddedArtifactMarker | None:
    if _WORD_SETTINGS_PART not in package.part_names:
        return None
    root = package.xml_root(_WORD_SETTINGS_PART)
    if root.tag != _word_tag("settings"):
        raise _invalid_artifact_marker(invalid_code, "DOCX artifact marker settings are invalid")
    containers = root.findall(_word_tag("docVars"))
    if len(containers) > 1:
        raise _invalid_artifact_marker(invalid_code, "DOCX repeats artifact marker variables")
    if not containers:
        return None

    expected_by_fold = {name.casefold(): name for name in _ARTIFACT_MARKER_DOCVARS}
    values: dict[str, str] = {}
    for variable in containers[0].findall(_word_tag("docVar")):
        raw_name = variable.get(_word_tag("name"))
        if raw_name is None:
            continue
        canonical_name = expected_by_fold.get(raw_name.casefold())
        if canonical_name is None:
            continue
        value = variable.get(_word_tag("val"))
        if canonical_name in values or value is None or not value:
            raise _invalid_artifact_marker(invalid_code, "DOCX artifact marker is malformed")
        values[canonical_name] = value
    if not values:
        return None
    if set(values) != set(_ARTIFACT_MARKER_DOCVARS):
        raise _invalid_artifact_marker(invalid_code, "DOCX artifact marker is incomplete")
    return _EmbeddedArtifactMarker(
        role=values[_ARTIFACT_ROLE_DOCVAR],
        profile=values[_ARTIFACT_PROFILE_DOCVAR],
        run_id=values[_ARTIFACT_RUN_DOCVAR],
    )


def _read_embedded_artifact_marker(
    path: Path,
    *,
    invalid_code: ErrorCode,
) -> _EmbeddedArtifactMarker | None:
    return _artifact_marker_from_package(
        read_docx_package(path),
        invalid_code=invalid_code,
    )


def _display_artifact_marker(run_id: str) -> _EmbeddedArtifactMarker:
    return _EmbeddedArtifactMarker(
        role=_DISPLAY_MARKER_ROLE,
        profile=_DISPLAY_MARKER_PROFILE,
        run_id=run_id,
    )


def _xml_bytes(root: etree._Element) -> bytes:
    return cast(
        "bytes",
        etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        ),
    )


def _write_display_artifact_marker(source: Path, destination: Path, *, run_id: str) -> None:
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.BACKEND_FAILED, "display marker output already exists")
    package = read_docx_package(source)
    expected_source = FileDigest(package.size_bytes, package.file_sha256)
    if (
        _artifact_marker_from_package(
            package,
            invalid_code=ErrorCode.EXPORT_SILENT_LOSS,
        )
        is not None
    ):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "display source unexpectedly contains a reserved artifact marker",
        )
    if _WORD_SETTINGS_PART not in package.part_names:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "display DOCX has no settings part for the artifact marker",
        )
    settings = package.xml_root(_WORD_SETTINGS_PART)
    containers = settings.findall(_word_tag("docVars"))
    if containers:
        doc_vars = containers[0]
    else:
        doc_vars = etree.Element(_word_tag("docVars"))
        insert_at = len(settings)
        for index, child in enumerate(settings):
            name = etree.QName(child).localname
            if etree.QName(child).namespace == _WORDPROCESSINGML_NS and name in (
                _SETTINGS_AFTER_DOCVARS
            ):
                insert_at = index
                break
        settings.insert(insert_at, doc_vars)
    marker = _display_artifact_marker(run_id)
    marker_values = {
        _ARTIFACT_ROLE_DOCVAR: marker.role,
        _ARTIFACT_PROFILE_DOCVAR: marker.profile,
        _ARTIFACT_RUN_DOCVAR: marker.run_id,
    }
    for name in _ARTIFACT_MARKER_DOCVARS:
        variable = etree.SubElement(doc_vars, _word_tag("docVar"))
        variable.set(_word_tag("name"), name)
        variable.set(_word_tag("val"), marker_values[name])
    replacement = _xml_bytes(settings)

    try:
        with zipfile.ZipFile(source, mode="r") as input_package:
            infos = input_package.infolist()
            with zipfile.ZipFile(destination, mode="x") as output_package:
                for info in infos:
                    data = (
                        replacement
                        if info.filename == _WORD_SETTINGS_PART
                        else input_package.read(info.filename)
                    )
                    output_package.writestr(info, data)
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
        if digest_file(source, max_bytes=_MAX_DOCX_BYTES) != expected_source:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "display DOCX changed while its artifact marker was written",
            )
        observed = _read_embedded_artifact_marker(
            destination,
            invalid_code=ErrorCode.EXPORT_SILENT_LOSS,
        )
        if observed != marker:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "display artifact marker was not preserved in the DOCX package",
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _synchronize_display_table_look(
    clean_reference: Path,
    source: Path,
    destination: Path,
) -> None:
    """Copy only table-look defaults from the sealed clean review.

    Tex2word can emit different ``w:tblLook`` defaults when the same table is
    converted with clean versus display-only revision macros.  The review
    layout pass fixes direct geometry and header formatting but intentionally
    leaves that backend default intact.  Synchronizing this single property
    makes the reference display inherit the clean review's visible table-style
    switches; all table order, geometry, content, fields, and relationships are
    still compared independently afterwards.
    """

    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.BACKEND_FAILED, "table-look output already exists")
    clean_package = read_docx_package(clean_reference)
    source_package = read_docx_package(source)
    expected_clean = FileDigest(clean_package.size_bytes, clean_package.file_sha256)
    expected_source = FileDigest(source_package.size_bytes, source_package.file_sha256)
    if clean_package.story_parts != source_package.story_parts:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "LaTeX revision display story parts differ before table-look synchronization",
        )

    table_tag = _word_tag("tbl")
    properties_tag = _word_tag("tblPr")
    look_tag = _word_tag("tblLook")
    replacements: dict[str, bytes] = {}
    expected_looks: list[tuple[str, int, tuple[tuple[str, str], ...] | None]] = []
    for part_uri in clean_package.story_parts:
        clean_root = clean_package.xml_root(part_uri)
        source_root = source_package.xml_root(part_uri)
        clean_tables = tuple(clean_root.iter(table_tag))
        source_tables = tuple(source_root.iter(table_tag))
        if len(clean_tables) != len(source_tables):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display table count differs before table-look synchronization",
                details={
                    "part_uri": part_uri,
                    "clean_tables": len(clean_tables),
                    "display_tables": len(source_tables),
                },
            )
        changed = False
        for table_index, (clean_table, source_table) in enumerate(
            zip(clean_tables, source_tables, strict=True)
        ):
            clean_properties = clean_table.find(properties_tag)
            source_properties = source_table.find(properties_tag)
            clean_look = None if clean_properties is None else clean_properties.find(look_tag)
            source_look = None if source_properties is None else source_properties.find(look_tag)
            signature = (
                None
                if clean_look is None
                else tuple(sorted((str(name), value) for name, value in clean_look.attrib.items()))
            )
            expected_looks.append((part_uri, table_index, signature))
            if clean_look is None:
                if source_look is not None and source_properties is not None:
                    source_properties.remove(source_look)
                    changed = True
                continue
            if source_properties is None:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display table properties are missing",
                    details={"part_uri": part_uri, "table_index": table_index},
                )
            copied = etree.fromstring(etree.tostring(clean_look))
            if source_look is None:
                assert clean_properties is not None
                clean_index = list(clean_properties).index(clean_look)
                source_properties.insert(min(clean_index, len(source_properties)), copied)
            else:
                source_properties.replace(source_look, copied)
            changed = True
        if changed:
            replacements[part_uri] = _xml_bytes(source_root)

    try:
        with zipfile.ZipFile(source, mode="r") as input_package:
            infos = input_package.infolist()
            with zipfile.ZipFile(destination, mode="x") as output_package:
                for info in infos:
                    output_package.writestr(
                        info,
                        replacements.get(info.filename, input_package.read(info.filename)),
                    )
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
        if digest_file(clean_reference, max_bytes=_MAX_DOCX_BYTES) != expected_clean:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "clean review changed during table-look synchronization",
            )
        if digest_file(source, max_bytes=_MAX_DOCX_BYTES) != expected_source:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "display DOCX changed during table-look synchronization",
            )
        synchronized = read_docx_package(destination)
        observed_looks: list[tuple[str, int, tuple[tuple[str, str], ...] | None]] = []
        for part_uri in synchronized.story_parts:
            root = synchronized.xml_root(part_uri)
            for table_index, table in enumerate(root.iter(table_tag)):
                properties = table.find(properties_tag)
                look = None if properties is None else properties.find(look_tag)
                signature = (
                    None
                    if look is None
                    else tuple(sorted((str(name), value) for name, value in look.attrib.items()))
                )
                observed_looks.append((part_uri, table_index, signature))
        if tuple(observed_looks) != tuple(expected_looks):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display table-look synchronization was not preserved",
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _reject_embedded_non_returnable_artifact(path: Path) -> None:
    marker = _read_embedded_artifact_marker(
        path,
        invalid_code=ErrorCode.REVISION_BASELINE_DRIFT,
    )
    if marker is None:
        return
    raise ContractError(
        ErrorCode.REVISION_BASELINE_DRIFT,
        "embedded DOCX artifact role is not returnable as a reviewed file",
        details={
            "artifact_role": marker.role,
            "artifact_profile": marker.profile,
            "artifact_run_id": marker.run_id,
        },
    )


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
    pending = [windows_extended_path(root)]
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
    filesystem_path = windows_extended_path(path)
    filesystem_parent = windows_extended_path(parent)
    resolved_parent = _require_real_directory(filesystem_parent, "workflow stage parent")
    try:
        resolved_path = filesystem_path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow stage is unavailable") from exc
    if resolved_path.parent != resolved_parent or _is_link_or_junction(filesystem_path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "workflow stage escaped its parent")
    _make_tree_writable(filesystem_path)
    try:
        shutil.rmtree(filesystem_path)
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
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".lwr-stage-{operation}-",
            dir=windows_extended_path(staging),
        )
    )
    payload = stage / "payload"
    payload.mkdir()
    return stage, payload


def _publish_payload(payload: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow output already exists")
    expected_staging = windows_extended_path(
        destination.parent.resolve(strict=True) / _STAGING
    ).resolve(strict=True)
    if payload.parent.parent.resolve(strict=True) != expected_staging:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "workflow payload is not owned")
    try:
        publish_new_directory(payload, destination)
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
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=windows_extended_path(parent)))
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
            publish_new_directory(staged, target)
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


def _revision_aliases_for_inventory(
    inventory: RevisionMacroInventory,
) -> tuple[Literal["add", "delete"], ...]:
    aliases: list[Literal["add", "delete"]] = []
    if inventory.alias_counts.add:
        aliases.append("add")
    if inventory.alias_counts.delete:
        aliases.append("delete")
    return tuple(aliases)


def _write_existing_changes_display(
    core: _CoreState,
    payload: Path,
    *,
    backend: ExportBackend,
    outcome: ExportOutcome,
    inventory: RevisionMacroInventory,
    timeout_s: float,
    confidentiality: Confidentiality,
) -> tuple[ReviewDocxArtifact, dict[str, int]]:
    """Create one sealed, non-authoritative static view from the existing overlay."""

    if inventory.total <= 0:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "revision display requested without revision macros",
        )
    overlay = outcome.image_overlay
    if overlay is None or not overlay.ready:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "revision display requires a ready image overlay",
        )
    clean_reference = outcome.output_path
    if clean_reference is None:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "revision display requires a clean review reference",
        )
    destination = payload / "existing-changes-display.docx"
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.BACKEND_FAILED, "revision display output already exists")

    with tempfile.TemporaryDirectory(
        prefix=".lwr-revision-display-",
        dir=windows_extended_path(payload),
    ) as temporary_name:
        temporary = Path(temporary_name)
        backend_docx = temporary / "backend.docx"
        layout_docx = temporary / "layout.docx"
        final_docx = temporary / "final.docx"
        synchronized_docx = temporary / "synchronized.docx"
        marked_docx = temporary / "marked.docx"
        result = backend.export(
            BackendRequest(
                source_root=overlay.derived_root,
                main_document=core.discovery.main_document,
                output_path=backend_docx,
                expected_source_tree_sha256=overlay.discovery.source_tree_sha256,
                timeout_s=timeout_s,
                revision_view="display",
                revision_aliases=_revision_aliases_for_inventory(inventory),
            )
        )
        if not result.succeeded:
            primary = next(
                (item for item in result.findings if item.severity in {"error", "fatal"}),
                None,
            )
            code = primary.code if primary is not None else ErrorCode.BACKEND_FAILED
            details: dict[str, object] = {
                "stage": "revision_display_backend",
                "backend_status": result.status,
                "finding_count": len(result.findings),
                "timed_out": "yes" if result.timed_out else "no",
            }
            if result.returncode is not None:
                details["returncode"] = result.returncode
            raise ContractError(
                code,
                "LaTeX revision display backend did not produce a DOCX",
                details=details,
            )
        if result.findings != outcome.backend_result.findings:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display backend findings differ from the clean review",
                details={
                    "stage": "revision_display_backend_parity",
                    "clean_finding_count": len(outcome.backend_result.findings),
                    "display_finding_count": len(result.findings),
                },
            )
        native_parity_keys = (
            "error_count",
            "math_image",
            "math_omml",
            "math_raw",
            "reference_loaded",
            "reference_profile",
            "reference_sha256",
            "warning_count",
            "warning_constructs",
        )
        if any(
            result.native_report.get(key) != outcome.backend_result.native_report.get(key)
            for key in native_parity_keys
        ):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display backend evidence differs from the clean review",
            )
        apply_review_layout(backend_docx, layout_docx)
        before_field_refresh = inspect_revision_display(layout_docx)
        finalize_word_fields(layout_docx, final_docx)
        _synchronize_display_table_look(clean_reference, final_docx, synchronized_docx)
        _write_display_artifact_marker(synchronized_docx, marked_docx, run_id=core.run_id)
        independent = inspect_docx(marked_docx)
        if (
            not independent.package_valid
            or not independent.structure_inspected
            or independent.external_relationships
        ):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display failed independent DOCX inspection",
            )
        if outcome.output_path is None:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "clean review output is unavailable for display parity",
            )
        clean_independent = inspect_docx(outcome.output_path)
        structural_metrics = (
            "body_paragraphs",
            "paragraphs",
            "omml_objects",
            "omml_paragraphs",
            "images",
            "image_instances",
            "tables",
            "seq_fields",
            "ref_fields",
            "pageref_fields",
            "relationships",
            "external_relationships",
        )
        for metric in structural_metrics:
            clean_value = getattr(clean_independent, metric)
            display_value = getattr(independent, metric)
            if clean_value != display_value:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "LaTeX revision display structure differs from the clean review",
                    details={
                        "metric": metric,
                        "clean_value": clean_value,
                        "display_value": display_value,
                    },
                )
        clean_package = read_docx_package(outcome.output_path)
        display_package = read_docx_package(marked_docx)
        if clean_package.story_parts != display_package.story_parts:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "LaTeX revision display story parts differ from the clean review",
            )
        display = validate_revision_display(
            marked_docx,
            inventory=inventory,
            clean_reference=clean_reference,
        )
        if (
            display.blue_text_characters != before_field_refresh.blue_text_characters
            or display.strike_text_characters != before_field_refresh.strike_text_characters
            or display.highlighted_text_characters
            != before_field_refresh.highlighted_text_characters
            or display.native_revision_elements != before_field_refresh.native_revision_elements
            or display.track_revisions_enabled != before_field_refresh.track_revisions_enabled
        ):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "Word field refresh changed the LaTeX revision display styling",
            )

        artifact = ReviewDocxArtifact.from_file(
            marked_docx,
            artifact_path=_EXPORT_CHANGES_DISPLAY,
            confidentiality=confidentiality,
            role=_DISPLAY_MARKER_ROLE,
        )
        try:
            os.link(marked_docx, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "revision display output appeared during publication",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "revision display output could not be atomically published",
            ) from exc
        return artifact, display.as_metrics()


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
    revision_inventory = scan_revision_macros(core.root / _SNAPSHOT, core.discovery)
    revision_aliases = _revision_aliases_for_inventory(revision_inventory)
    if revision_inventory.total and backend_name != "tex2word":
        raise ContractError(
            ErrorCode.BACKEND_CAPABILITY_MISSING,
            "the selected backend cannot create the LaTeX revision display",
            details={
                "backend": backend_name,
                "revision_macro_instances": revision_inventory.total,
                "remediation": "use the tex2word backend for this review task",
            },
        )
    outcome = export_review_docx(
        backend,
        BackendRequest(
            core.root / _SNAPSHOT,
            cast("str", source_payload["main_document"]),
            output,
            expected_source_tree_sha256=core.discovery.source_tree_sha256,
            timeout_s=timeout_s,
            revision_view=(
                "clean" if backend_name == "tex2word" and revision_inventory.total else "source"
            ),
            revision_aliases=revision_aliases,
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
    write_new_json(objects / "backend-capabilities.json", capabilities, contract=True)
    if outcome.output_path is None or outcome.anchoring is None:
        raise _export_failure_error(outcome, backend_name=backend_name)
    if not _is_reviewable_export_report(outcome.report):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "workflow export contains a blocking or non-recoverable finding",
            details={
                "export_status": outcome.report.status,
                "finding_count": len(outcome.report.findings),
            },
        )
    display_artifact: ReviewDocxArtifact | None = None
    display_metrics: dict[str, int] = {"revision_display_available": 0}
    if revision_inventory.total:
        display_artifact, observed_display_metrics = _write_existing_changes_display(
            core,
            payload,
            backend=backend,
            outcome=outcome,
            inventory=revision_inventory,
            timeout_s=timeout_s,
            confidentiality=confidentiality,
        )
        display_metrics = {
            "revision_display_available": 1,
            **observed_display_metrics,
        }
    outcome = replace(
        outcome,
        report=replace(
            outcome.report,
            source_map_sha256=None,
            existing_changes_display_docx=display_artifact,
            source_metrics={
                **outcome.report.source_metrics,
                **revision_inventory.as_metrics(),
            },
            output_metrics={
                **outcome.report.output_metrics,
                **display_metrics,
            },
        ),
    )
    _validate_source_feature_report_payload(
        cast("dict[str, Any]", outcome.report.as_payload()),
        source_manifest_interface_version=SOURCE_MANIFEST_INTERFACE_VERSION,
        expected_source_metrics=outcome.report.source_metrics,
        expected_output_metrics=outcome.report.output_metrics,
        producer_interface_version=EXPORT_REPORT_INTERFACE_VERSION,
    )
    review_ir = build_review_ir_document(
        core.discovery,
        outcome,
        run_id=core.run_id,
        source_manifest_sha256=core.source_manifest_sha256,
        generated_at=timestamp,
    )
    assert outcome.anchoring is not None
    source_map = build_source_map_document(
        outcome.anchoring,
        run_id=core.run_id,
        source_manifest_sha256=core.source_manifest_sha256,
        review_ir_sha256=compute_payload_sha256(review_ir),
        generated_at=timestamp,
        export_report_payload=cast("dict[str, Any]", outcome.report.as_payload()),
    )
    outcome = replace(
        outcome,
        report=replace(
            outcome.report,
            source_map_sha256=compute_payload_sha256(source_map),
        ),
    )
    report = build_export_report_document(outcome, run_id=core.run_id, generated_at=timestamp)
    write_new_json(objects / "review-ir.json", review_ir, contract=True)
    write_new_json(objects / "source-map.json", source_map, contract=True)
    write_new_json(objects / "export-report.json", report, contract=True)
    coverage = cast("dict[str, int]", cast("dict[str, Any]", source_map["payload"])["coverage"])
    return {
        "review_units": coverage["total"],
        "exact_mappings": coverage["exact"],
        "export_findings": len(outcome.report.findings),
        "revision_macro_instances": revision_inventory.total,
        "revision_display_available": int(display_artifact is not None),
    }


def _is_reviewable_export_report(report: ExportReport) -> bool:
    """Accept a complete baseline with explicit recoverable warnings.

    ``partial`` is useful for journal-specific formatting or source units that
    cannot be mapped automatically.  It is still safe to review because the
    immutable DOCX, image count, OOXML structure and SourceMap have already
    passed their independent gates.  Errors, fatal findings and any
    non-recoverable warning remain blocking.
    """

    if report.status == "success":
        return True
    return (
        report.status == "partial"
        and bool(report.findings)
        and all(
            finding.severity == "warning" and finding.recoverable for finding in report.findings
        )
    )


def _export_failure_error(
    outcome: ExportOutcome,
    *,
    backend_name: BackendName,
) -> ContractError:
    """Preserve a backend's stable cause without exposing logs or host paths."""

    primary = next(
        (item for item in outcome.report.findings if item.severity in {"error", "fatal"}),
        None,
    )
    code = primary.code if primary is not None else ErrorCode.BACKEND_FAILED
    backend_result = outcome.backend_result
    details: dict[str, object] = {
        "provider": backend_name,
        "stage": "backend_export" if not backend_result.succeeded else "review_validation",
        "export_status": outcome.report.status,
        "backend_status": backend_result.status,
        "backend_error_code": code.value,
        "timed_out": "yes" if backend_result.timed_out else "no",
        "finding_count": len(outcome.report.findings),
    }
    if backend_result.returncode is not None:
        details["returncode"] = backend_result.returncode
    native_report = backend_result.native_report
    for key in ("duration_ms", "error_count", "warning_count"):
        value = native_report.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            details[key] = value
    output_truncated = native_report.get("output_truncated")
    if isinstance(output_truncated, bool):
        details["output_truncated"] = "yes" if output_truncated else "no"
    failure_kind = native_report.get("failure_kind")
    if isinstance(failure_kind, str):
        details["failure_kind"] = failure_kind
    return ContractError(
        code,
        "workflow export did not produce a review DOCX",
        details=details,
    )


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
    export_published = False
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
        state = _validate_export(core, export_root=payload)
        _publish_payload(payload, destination)
        export_published = True
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
        primary_error_active = sys.exc_info()[0] is not None
        if stage.exists():
            try:
                _remove_owned_tree(stage, stage.parent, prefixes=(".lwr-stage-export-",))
            except ContractError:
                # The directory rename above is the publication commit point. A
                # post-commit cleanup conflict must leave a discoverable stale
                # stage for ``workflow clean`` instead of reporting a false
                # export failure that cannot be retried. Cleanup must likewise
                # never mask the original pre-commit failure.
                if not export_published and not primary_error_active:
                    raise


def _source_manifest_interface_version(core: _CoreState) -> str:
    producer = core.source_manifest.get("producer")
    if not isinstance(producer, dict):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source manifest producer is invalid",
        )
    interface_version = producer.get("interface_version")
    if interface_version not in {
        _LEGACY_SOURCE_MANIFEST_INTERFACE_VERSION,
        SOURCE_MANIFEST_INTERFACE_VERSION,
    }:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source manifest producer interface is unsupported",
        )
    return cast("str", interface_version)


def _validate_export_generation(
    core: _CoreState,
    source_map: dict[str, Any],
    report: dict[str, Any],
    map_payload: dict[str, Any],
    report_payload: dict[str, Any],
) -> None:
    map_producer = source_map.get("producer")
    report_producer = report.get("producer")
    if not isinstance(map_producer, dict) or not isinstance(report_producer, dict):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export producer is invalid")
    generation = (
        _source_manifest_interface_version(core),
        map_producer.get("interface_version"),
        report_producer.get("interface_version"),
    )
    current = (
        SOURCE_MANIFEST_INTERFACE_VERSION,
        SOURCE_MAP_INTERFACE_VERSION,
        EXPORT_REPORT_INTERFACE_VERSION,
    )
    previous = (
        SOURCE_MANIFEST_INTERFACE_VERSION,
        SOURCE_MAP_INTERFACE_VERSION,
        PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
    )
    legacy = (
        _LEGACY_SOURCE_MANIFEST_INTERFACE_VERSION,
        LEGACY_SOURCE_MAP_INTERFACE_VERSION,
        _LEGACY_EXPORT_REPORT_INTERFACE_VERSION,
    )
    if generation in {current, previous}:
        if map_payload.get("export_report_commitment") != export_report_commitment(report_payload):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "SourceMap ExportReport commitment differs",
            )
        return
    if generation == legacy:
        if "export_report_commitment" in map_payload:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "legacy SourceMap unexpectedly contains a report commitment",
            )
        return
    raise ContractError(
        ErrorCode.HASH_SOURCE_MISMATCH,
        "export object producer generations differ or are unsupported",
    )


def _validate_existing_changes_display(
    core: _CoreState,
    export_root: Path,
    report_payload: dict[str, Any],
    *,
    producer_interface_version: object,
) -> tuple[RevisionMacroInventory | None, dict[str, int], FileDigest | None]:
    """Rebuild source evidence and validate the optional sealed display artifact."""

    display_path = export_root / "existing-changes-display.docx"
    display_exists = display_path.exists() or display_path.is_symlink()
    declared = "existing_changes_display_docx" in report_payload
    current_report = producer_interface_version == EXPORT_REPORT_INTERFACE_VERSION
    legacy_report = producer_interface_version in {
        _LEGACY_EXPORT_REPORT_INTERFACE_VERSION,
        PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
    }
    if not current_report and not legacy_report:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "export report producer interface is unsupported",
        )
    if legacy_report:
        if declared or display_exists:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "legacy export report contains current LaTeX revision display evidence",
            )
        metrics = report_payload.get("metrics")
        source_metrics = metrics.get("source") if isinstance(metrics, dict) else None
        output_metrics = metrics.get("output") if isinstance(metrics, dict) else None
        if not isinstance(source_metrics, dict) or not isinstance(output_metrics, dict):
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export metrics are invalid")
        if any(
            key.startswith(("revision_macro_", "revision_macros_", "revision_display_"))
            for key in (*source_metrics, *output_metrics)
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "legacy export report contains current revision display metrics",
            )
        return None, {"revision_display_available": 0}, None
    if not declared:
        if display_exists:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "workflow export contains an unbound LaTeX revision display",
            )
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "current export report omits the revision display field",
        )

    revision_inventory = scan_revision_macros(core.root / _SNAPSHOT, core.discovery)
    metrics = report_payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export metrics are invalid")
    source_metrics = metrics.get("source")
    output_metrics = metrics.get("output")
    if not isinstance(source_metrics, dict) or not isinstance(output_metrics, dict):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export metric groups are invalid")
    for name, expected in revision_inventory.as_metrics().items():
        value = source_metrics.get(name)
        if value != expected or isinstance(value, bool):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "revision macro metrics differ from the sealed snapshot",
                details={"metric": name},
            )

    artifact = report_payload.get("existing_changes_display_docx")
    expected_available = int(revision_inventory.total > 0)
    if output_metrics.get("revision_display_available") != expected_available:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "revision display availability metric differs",
        )
    if not revision_inventory.total:
        if artifact is not None or display_exists:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "revision display exists without supported source macros",
            )
        return revision_inventory, {"revision_display_available": 0}, None

    if not isinstance(artifact, dict) or not display_exists:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "revision display artifact is missing",
        )
    _require_read_only(display_path, "export LaTeX revision display")
    display_digest = digest_file(display_path, max_bytes=_MAX_DOCX_BYTES)
    expected_display = FileDigest(
        cast("int", artifact["size_bytes"]),
        cast("str", artifact["sha256"]),
    )
    review_artifact = cast("dict[str, Any]", report_payload["review_docx"])
    if (
        artifact["path_base"] != "run_root"
        or artifact["path"] != _EXPORT_CHANGES_DISPLAY
        or artifact["role"] != "latex_changes_display_docx"
        or artifact["media_type"]
        != "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or artifact["immutable"] is not True
        or artifact["confidentiality"] != review_artifact["confidentiality"]
        or artifact["artifact_id"] != derive_artifact_id(expected_display.sha256)
        or display_digest != expected_display
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "revision display artifact binding differs",
        )
    marker = _read_embedded_artifact_marker(
        display_path,
        invalid_code=ErrorCode.HASH_SOURCE_MISMATCH,
    )
    if marker != _display_artifact_marker(core.run_id):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "revision display embedded artifact marker differs",
        )
    independent = inspect_docx(display_path)
    if (
        not independent.package_valid
        or not independent.structure_inspected
        or independent.external_relationships
    ):
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "sealed revision display failed independent DOCX inspection",
        )

    display = validate_revision_display(
        display_path,
        inventory=revision_inventory,
        clean_reference=export_root / "review.docx",
    )
    observed_metrics = {
        "revision_display_available": 1,
        **display.as_metrics(),
    }
    for name, expected in observed_metrics.items():
        value = output_metrics.get(name)
        if value != expected or isinstance(value, bool):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "revision display metrics differ from the sealed DOCX",
                details={"metric": name},
            )
    return revision_inventory, observed_metrics, display_digest


def _validate_export(
    core: _CoreState,
    *,
    export_root: Path | None = None,
) -> _ExportState:
    export_root = _require_real_directory(
        core.root / _EXPORT if export_root is None else export_root,
        "workflow export",
    )
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
    _validate_export_generation(core, source_map, report, map_payload, report_payload)
    if review_payload["source_manifest_sha256"] != core.source_manifest_sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "ReviewIR source binding differs")
    if map_payload["source_manifest_sha256"] != core.source_manifest_sha256 or map_payload[
        "review_ir_sha256"
    ] != compute_payload_sha256(review_ir):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "SourceMap binding differs")
    source_image_instances = _validate_image_overlay(core, export_root, map_payload, report_payload)
    review_digest = digest_file(paths["review"], max_bytes=_MAX_DOCX_BYTES)
    artifact = cast("dict[str, Any]", report_payload["review_docx"])
    expected_review = FileDigest(
        cast("int", artifact["size_bytes"]), cast("str", artifact["sha256"])
    )
    if (
        not _is_reviewable_export_payload(report_payload)
        or report_payload["source_manifest_sha256"] != core.source_manifest_sha256
        or report_payload["backend_capabilities_sha256"] != compute_payload_sha256(capabilities)
        or report_payload["review_ir_sha256"] != compute_payload_sha256(review_ir)
        or report_payload["source_map_sha256"] != compute_payload_sha256(source_map)
        or artifact["path_base"] != "run_root"
        or artifact["path"] != _EXPORT_DOCX
        or artifact["role"] != "review_docx"
        or artifact["media_type"]
        != "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or artifact["immutable"] is not True
        or review_digest != expected_review
        or map_payload["review_docx_sha256"] != review_digest.sha256
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export object binding differs")
    review_marker = _read_embedded_artifact_marker(
        paths["review"],
        invalid_code=ErrorCode.HASH_SOURCE_MISMATCH,
    )
    if review_marker is not None:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "sealed review DOCX carries a non-review artifact marker",
        )
    inspection = inspect_docx(paths["review"])
    if not inspection.package_valid or not inspection.structure_inspected:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "sealed review DOCX failed independent structure inspection",
        )
    report_producer = cast("dict[str, Any]", report["producer"])
    revision_inventory, revision_display_metrics, display_digest = (
        _validate_existing_changes_display(
            core,
            export_root,
            report_payload,
            producer_interface_version=report_producer.get("interface_version"),
        )
    )
    inventory = scan_source_features(
        core.root / _SNAPSHOT,
        core.discovery,
        image_instances=source_image_instances,
    )
    tool = cast("dict[str, Any]", capabilities_payload["tool"])
    label_output_count = independently_observed_label_count(
        inventory,
        inspection,
        backend_id=cast("str", capabilities_payload["backend_id"]),
        tool_name=cast("str", tool["name"]),
        tool_version=cast("str | None", tool["version"]),
        interface_version=cast("str | None", tool["interface_version"]),
    )
    expected_locations: dict[str, dict[str, object] | None] = {}
    for feature in _SOURCE_FEATURE_METRICS:
        location = inventory.location_for(cast("Any", feature))
        expected_locations[feature] = None if location is None else location.as_contract()
    _validate_source_feature_report_payload(
        report_payload,
        producer_interface_version=report_producer.get("interface_version"),
        source_manifest_interface_version=_source_manifest_interface_version(core),
        expected_source_metrics=inventory.as_metrics(),
        expected_output_metrics=inspection.as_metrics(),
        expected_feature_locations=expected_locations,
        expected_feature_output_counts={"labels": label_output_count},
    )
    coverage = cast("dict[str, int]", map_payload["coverage"])
    counts = {
        "source_files": len(core.discovery.files),
        "review_units": coverage["total"],
        "exact_mappings": coverage["exact"],
        "export_findings": len(cast("list[Any]", report_payload["findings"])),
        "revision_macro_instances": 0 if revision_inventory is None else revision_inventory.total,
        "revision_display_available": revision_display_metrics["revision_display_available"],
    }
    return _ExportState(
        source_map=source_map,
        source_map_sha256=compute_payload_sha256(source_map),
        counts=counts,
        non_returnable_docx_digests=(
            frozenset() if display_digest is None else frozenset({display_digest})
        ),
    )


def _has_current_source_feature_markers(
    source_metrics: dict[str, Any],
    raw_results: object,
) -> bool:
    if _CURRENT_SOURCE_FEATURE_MARKER_METRICS.intersection(source_metrics):
        return True
    if not isinstance(raw_results, list):
        return False
    return any(
        isinstance(item, dict) and item.get("feature") in _CURRENT_SOURCE_FEATURE_RESULT_MARKERS
        for item in raw_results
    )


def _validate_source_feature_report_payload(
    report_payload: dict[str, Any],
    *,
    producer_interface_version: object,
    source_manifest_interface_version: object | None = None,
    expected_source_metrics: dict[str, int] | None = None,
    expected_output_metrics: dict[str, int] | None = None,
    expected_feature_locations: dict[str, dict[str, object] | None] | None = None,
    expected_feature_output_counts: dict[str, int | None] | None = None,
) -> None:
    """Recheck report semantics against an independently sealed evidence generation."""

    independent_evidence_required = source_manifest_interface_version is not None
    if source_manifest_interface_version is None:
        source_manifest_interface_version = (
            SOURCE_MANIFEST_INTERFACE_VERSION
            if producer_interface_version
            in {
                PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
                EXPORT_REPORT_INTERFACE_VERSION,
            }
            else _LEGACY_SOURCE_MANIFEST_INTERFACE_VERSION
        )
    if source_manifest_interface_version not in {
        _LEGACY_SOURCE_MANIFEST_INTERFACE_VERSION,
        SOURCE_MANIFEST_INTERFACE_VERSION,
    }:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source manifest producer interface is unsupported",
        )
    current_source_evidence = source_manifest_interface_version == SOURCE_MANIFEST_INTERFACE_VERSION
    current_report = producer_interface_version in {
        PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
        EXPORT_REPORT_INTERFACE_VERSION,
    }
    if current_source_evidence != current_report:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source manifest and export report evidence generations differ",
        )

    supported_interfaces = {
        _LEGACY_EXPORT_REPORT_INTERFACE_VERSION,
        PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
        EXPORT_REPORT_INTERFACE_VERSION,
    }
    if producer_interface_version not in supported_interfaces:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "export report producer interface is unsupported",
        )
    metrics = report_payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export metrics are invalid")
    source = metrics.get("source")
    output = metrics.get("output")
    if not isinstance(source, dict) or not isinstance(output, dict):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "export metric groups are invalid")
    raw_results = report_payload.get("feature_results")
    version = source.get("source_feature_inventory_version")
    if not current_source_evidence:
        if version is not None or _has_current_source_feature_markers(source, raw_results):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "legacy export report contains current source feature evidence",
            )
        return
    if version is None:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "current source feature report cannot be downgraded to legacy evidence",
        )
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != INVENTORY_PROFILE_VERSION
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source feature inventory version is unsupported",
        )

    raw_findings = report_payload.get("findings")
    if independent_evidence_required and (
        expected_source_metrics is None or expected_output_metrics is None
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "current export report lacks independent source or DOCX evidence",
        )
    if expected_source_metrics is not None:
        for metric, expected in expected_source_metrics.items():
            if source.get(metric) != expected or isinstance(source.get(metric), bool):
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "export source metrics differ from the sealed snapshot inventory",
                    details={"metric": metric},
                )
    if expected_output_metrics is not None:
        for metric, expected in expected_output_metrics.items():
            if output.get(metric) != expected or isinstance(output.get(metric), bool):
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "export output metrics differ from the final DOCX inspection",
                    details={"metric": metric},
                )
    if not isinstance(raw_results, list) or not isinstance(raw_findings, list):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source feature reconciliation evidence is invalid",
        )
    findings_by_id: dict[str, dict[str, Any]] = {}
    for finding in raw_findings:
        if not isinstance(finding, dict):
            continue
        diagnostic_id = finding.get("diagnostic_id")
        if not isinstance(diagnostic_id, str):
            continue
        if diagnostic_id in findings_by_id:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature diagnostic is duplicated",
                details={"diagnostic_id": diagnostic_id},
            )
        findings_by_id[diagnostic_id] = finding
    finding_ids = set(findings_by_id)
    indexed: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature result is invalid",
            )
        feature = item.get("feature")
        if not isinstance(feature, str) or feature not in _SOURCE_FEATURE_METRICS:
            continue
        if feature in indexed:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature result is duplicated",
                details={"feature": feature},
            )
        indexed[feature] = item
    missing = sorted(set(_SOURCE_FEATURE_METRICS).difference(indexed))
    if missing:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source feature reconciliation evidence is incomplete",
            details={"missing_features": ",".join(missing)},
        )

    def metric_count(container: dict[str, Any], key: str) -> int:
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature metric is missing or invalid",
                details={"metric": key},
            )
        return value

    for source_metric in sorted(
        _REQUIRED_SOURCE_FEATURE_METRICS - {"source_feature_inventory_version"}
    ):
        metric_count(source, source_metric)
    skipped_dynamic_regions = metric_count(source, "skipped_dynamic_regions")
    dynamic_results = [
        item
        for item in raw_results
        if isinstance(item, dict) and item.get("feature") == "dynamic_regions"
    ]
    if len(dynamic_results) != (1 if skipped_dynamic_regions else 0):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "dynamic source-region reconciliation evidence is incomplete",
        )
    dynamic_partial_required = False
    if skipped_dynamic_regions:
        dynamic_result = dynamic_results[0]
        dynamic_ids = dynamic_result.get("diagnostic_ids")
        if (
            dynamic_result.get("status") != "degraded"
            or dynamic_result.get("source_count") != skipped_dynamic_regions
            or dynamic_result.get("output_count") is not None
            or not isinstance(dynamic_ids, list)
            or len(dynamic_ids) != 1
            or not isinstance(dynamic_ids[0], str)
            or dynamic_ids[0] not in findings_by_id
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "dynamic source-region reconciliation evidence differs",
            )
        dynamic_finding = findings_by_id[dynamic_ids[0]]
        if (
            dynamic_finding.get("code") != ErrorCode.EXPORT_DEGRADED.value
            or dynamic_finding.get("phase") != "inspect"
            or dynamic_finding.get("recoverable") is not True
            or dynamic_finding.get("source_location") is not None
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "dynamic source-region diagnostic binding differs",
            )
        dynamic_partial_required = True
    expected_output_counts: dict[str, int | None] = {
        "images": metric_count(output, "image_instances"),
        "math": metric_count(output, "omml_objects"),
        "tables": metric_count(output, "tables"),
        "references": metric_count(output, "ref_fields") + metric_count(output, "pageref_fields"),
    }
    partial_required = dynamic_partial_required
    unsupported_critical_features: list[str] = []
    diagnostic_owners: dict[str, str] = {}
    if expected_feature_output_counts is not None:
        expected_output_counts.update(expected_feature_output_counts)
    for feature, source_metric in _SOURCE_FEATURE_METRICS.items():
        result = indexed[feature]
        expected_source_count = metric_count(source, source_metric)
        auxiliary_source_count = sum(
            metric_count(source, metric) for metric in _SOURCE_FEATURE_AUX_METRICS.get(feature, ())
        )
        source_count = result.get("source_count")
        if source_count != expected_source_count or isinstance(source_count, bool):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature count differs from inventory metrics",
                details={"feature": feature},
            )
        output_count = result.get("output_count")
        if feature in expected_output_counts:
            if output_count != expected_output_counts[feature] or isinstance(output_count, bool):
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "source feature count differs from DOCX metrics",
                    details={"feature": feature},
                )
        elif output_count is not None and (
            isinstance(output_count, bool) or not isinstance(output_count, int) or output_count < 0
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature output count is invalid",
                details={"feature": feature},
            )
        expected_output_count = expected_output_counts.get(feature)
        if expected_output_count is not None and expected_output_count < expected_source_count:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "source feature output count is lower than the inventory count",
                details={"feature": feature},
            )

        status = result.get("status")
        if status not in {"preserved", "degraded", "unsupported", "failed"}:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature status is invalid",
                details={"feature": feature},
            )
        if feature == "citations" and expected_source_count > 0 and status != "unsupported":
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "source citations cannot be marked preserved without count-equivalent evidence",
                details={"feature": feature},
            )
        diagnostic_ids = result.get("diagnostic_ids")
        if not isinstance(diagnostic_ids, list) or any(
            not isinstance(item, str) for item in diagnostic_ids
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature diagnostics are invalid",
                details={"feature": feature},
            )
        if not set(diagnostic_ids).issubset(finding_ids):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature diagnostics are not bound to report findings",
                details={"feature": feature},
            )
        if status == "failed":
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "source feature reconciliation contains a blocking loss",
                details={"feature": feature},
            )
        if status in {"degraded", "unsupported"}:
            partial_required = True
        requires_diagnostic = status in {"degraded", "unsupported"}
        if requires_diagnostic and not diagnostic_ids:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "non-preserved source feature lacks an explicit diagnostic",
                details={"feature": feature},
            )
        if requires_diagnostic and any(
            findings_by_id[diagnostic_id].get("recoverable") is not True
            for diagnostic_id in diagnostic_ids
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "non-preserved source feature lacks a recoverable finding",
                details={"feature": feature},
            )
        if requires_diagnostic and len(diagnostic_ids) != len(set(diagnostic_ids)):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source feature repeats a diagnostic binding",
                details={"feature": feature},
            )
        if requires_diagnostic:
            expected_code = ErrorCode.EXPORT_DEGRADED.value
            for diagnostic_id in diagnostic_ids:
                finding = findings_by_id[diagnostic_id]
                owner = diagnostic_owners.get(diagnostic_id)
                if owner is not None and owner != feature:
                    raise ContractError(
                        ErrorCode.HASH_SOURCE_MISMATCH,
                        "source feature diagnostic is bound to multiple features",
                        details={"feature": feature, "other_feature": owner},
                    )
                diagnostic_owners[diagnostic_id] = feature
                if finding.get("code") != expected_code or finding.get("phase") != "inspect":
                    raise ContractError(
                        ErrorCode.HASH_SOURCE_MISMATCH,
                        "source feature diagnostic code or inspection phase differs",
                        details={"feature": feature},
                    )
                if expected_feature_locations is not None and finding.get(
                    "source_location"
                ) != expected_feature_locations.get(feature):
                    raise ContractError(
                        ErrorCode.HASH_SOURCE_MISMATCH,
                        "source feature diagnostic evidence differs from the sealed snapshot",
                        details={"feature": feature},
                    )
        if status == "unsupported" and feature in _CRITICAL_SOURCE_FEATURES:
            unsupported_critical_features.append(feature)
        if status == "preserved" and auxiliary_source_count > 0:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "source feature marked preserved despite non-equivalent source constructs",
                details={"feature": feature},
            )
        if (
            status == "preserved"
            and expected_source_count > 0
            and (output_count is None or output_count < expected_source_count)
        ):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "source feature marked preserved despite a lower output count",
                details={"feature": feature},
            )

    if partial_required and report_payload.get("status") != "partial":
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "non-preserved source features require a partial export status",
        )
    if unsupported_critical_features:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "critical source feature cannot be unsupported",
            details={"features": ",".join(unsupported_critical_features)},
        )


def _is_reviewable_export_payload(report_payload: dict[str, Any]) -> bool:
    """Re-validate the sealed success/partial policy without trusting Python objects."""

    status = report_payload.get("status")
    if status == "success":
        return True
    findings = report_payload.get("findings")
    if status != "partial" or not isinstance(findings, list) or not findings:
        return False
    return all(
        isinstance(finding, dict)
        and finding.get("severity") == "warning"
        and finding.get("recoverable") is True
        for finding in findings
    )


def _validate_image_overlay(
    core: _CoreState,
    export_root: Path,
    map_payload: dict[str, Any],
    report_payload: dict[str, Any],
) -> int:
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
    manifest_path = overlay_root / IMAGE_OVERLAY_MANIFEST
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
    sealed_image_verifier: SealedImageCacheVerifier | None = None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "materialized_pdf":
            continue
        materialized = entry.get("materialized")
        if not isinstance(materialized, dict):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "materialized image evidence is missing",
            )
        sealed_image_verifier = _validate_materialized_image(
            overlay_root,
            entry,
            sealed_image_verifier,
        )
    source_image_instances = counts.get("source_image_instances")
    if (
        isinstance(source_image_instances, bool)
        or not isinstance(source_image_instances, int)
        or source_image_instances < 0
    ):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "image-overlay source count is invalid",
        )
    return source_image_instances


def _validate_materialized_image(
    overlay_root: Path,
    entry: dict[str, Any],
    verifier: SealedImageCacheVerifier | None,
) -> SealedImageCacheVerifier:
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
        active_verifier = verifier or SealedImageCacheVerifier(
            cast("dict[str, Any]", materialized["renderer"]),
        )
        verified = active_verifier.verify(
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
    return active_verifier


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
    source_digest = digest_file(source, max_bytes=_MAX_DOCX_BYTES)
    if source_digest in export_state.non_returnable_docx_digests:
        raise ContractError(
            ErrorCode.REVISION_BASELINE_DRIFT,
            "LaTeX revision display artifacts cannot be used as returned review files",
            details={"artifact_role": _DISPLAY_MARKER_ROLE},
        )
    destination = core.root / _RECEIVE
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "workflow receive output already exists")
    stage, payload = _prepare_stage(core.root, "receive")
    receive_published = False
    try:
        map_payload = cast("dict[str, Any]", export_state.source_map["payload"])
        archive = archive_returned_docx(
            source,
            payload / "original",
            run_id=core.run_id,
            exported_docx_sha256=cast("str", map_payload["review_docx_sha256"]),
            confidentiality=confidentiality,
        )
        archived_digest = FileDigest(archive.size_bytes, archive.returned_docx_sha256)
        if archived_digest in export_state.non_returnable_docx_digests:
            raise ContractError(
                ErrorCode.REVISION_BASELINE_DRIFT,
                "LaTeX revision display artifacts cannot be used as returned review files",
                details={"artifact_role": _DISPLAY_MARKER_ROLE},
            )
        _reject_embedded_non_returnable_artifact(archive.docx_path)
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
        counts = _validate_receive(core, export_state, receive_root=payload)
        _publish_payload(payload, destination)
        receive_published = True
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
        primary_error_active = sys.exc_info()[0] is not None
        if stage.exists():
            try:
                _remove_owned_tree(stage, stage.parent, prefixes=(".lwr-stage-receive-",))
            except ContractError:
                if not receive_published and not primary_error_active:
                    raise


def _validate_receive(
    core: _CoreState,
    export_state: _ExportState,
    *,
    receive_root: Path | None = None,
) -> dict[str, int]:
    receive_root = _require_real_directory(
        core.root / _RECEIVE if receive_root is None else receive_root,
        "workflow receive output",
    )
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
