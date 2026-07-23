"""Builders that seal runtime models into the public v1alpha contracts."""

from __future__ import annotations

import platform
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from latex_word_review.__about__ import __version__
from latex_word_review.backends.base import BackendCapabilities
from latex_word_review.canonical import sha256_canonical
from latex_word_review.contracts import make_envelope, validate_contract
from latex_word_review.discovery import ProjectDiscovery
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import AnchoringResult, ExportOutcome
from latex_word_review.export_models import export_report_commitment
from latex_word_review.hashing import digest_file
from latex_word_review.ids import derive_artifact_id, derive_source_manifest_id, stable_id
from latex_word_review.paths import validate_relative_path
from latex_word_review.planner import POLICY_SHA256
from latex_word_review.revisions import BookmarkBinding
from latex_word_review.source_units import TextProvenanceSegment, review_ir_payload

Confidentiality = Literal["public_fixture", "local_private", "derived_private"]
SOURCE_MANIFEST_INTERFACE_VERSION = "source-manifest-builder-v2"
SOURCE_MAP_INTERFACE_VERSION = "source-map-builder-v2"
EXPORT_REPORT_INTERFACE_VERSION = "export-report-builder-v2"
LEGACY_SOURCE_MAP_INTERFACE_VERSION = "source-map-builder-v1"
RunPhase = Literal[
    "initialized",
    "snapshotted",
    "exported",
    "ingested",
    "reviewed",
    "planned",
    "applied",
    "verified",
    "bundled",
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def producer_identity(interface_version: str) -> dict[str, Any]:
    return {
        "name": "latex-word-review",
        "version": __version__,
        "interface_version": interface_version,
        "distribution": "python-package",
        "executable_sha256": None,
        "configuration_sha256": sha256_canonical(
            {"interface": interface_version, "schema": "1.0.0-alpha.1"}
        ),
    }


def _object_binding(document: Mapping[str, Any]) -> dict[str, str]:
    receipt = validate_contract(document)
    return {
        "schema_name": receipt.schema_name,
        "object_id": receipt.object_id,
        "payload_sha256": receipt.payload_sha256,
        "document_sha256": receipt.document_sha256,
    }


def _collect_artifacts(documents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    required = {
        "artifact_id",
        "path",
        "path_base",
        "role",
        "media_type",
        "size_bytes",
        "sha256",
        "immutable",
        "confidentiality",
    }
    found: dict[tuple[str, str, str], dict[str, Any]] = {}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if set(value) == required:
                artifact = dict(value)
                key = (
                    cast("str", artifact["path_base"]),
                    cast("str", artifact["path"]),
                    cast("str", artifact["sha256"]),
                )
                found[key] = artifact
                return
            for nested in value.values():
                visit(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for nested in value:
                visit(nested)

    for document in documents:
        visit(document.get("payload"))
    return [found[key] for key in sorted(found)]


def build_run_manifest_document(
    source_manifest: Mapping[str, Any],
    *,
    objects: Sequence[Mapping[str, Any]],
    backend_capabilities: Sequence[Mapping[str, Any]] = (),
    artifacts: Sequence[Mapping[str, Any]] = (),
    current_phase: RunPhase = "verified",
    status: Literal["active", "blocked", "failed", "completed", "aborted"] = "completed",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Seal a path-free run inventory from already validated workflow objects."""

    source_receipt = validate_contract(source_manifest)
    if source_receipt.schema_name != "SourceManifest":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "run manifest requires SourceManifest")
    run_id = cast("str", source_manifest["run_id"])
    source_payload = cast("Mapping[str, Any]", source_manifest["payload"])
    all_documents = [source_manifest, *objects]
    bindings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for document in all_documents:
        receipt = validate_contract(document)
        if document["run_id"] not in {None, run_id}:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "run object binding differs")
        key = (receipt.schema_name, receipt.object_id)
        if key in seen:
            continue
        seen.add(key)
        bindings.append(_object_binding(document))
    bindings.sort(key=lambda item: (item["schema_name"], item["object_id"]))

    selected: list[dict[str, str]] = []
    for document in backend_capabilities:
        receipt = validate_contract(document)
        if receipt.schema_name != "BackendCapabilities":
            raise ContractError(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "selected backend must be BackendCapabilities",
            )
        if document["run_id"] not in {None, run_id}:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "backend run binding differs")
        backend_payload = cast("Mapping[str, Any]", document["payload"])
        selected.append(
            {
                "backend_id": cast("str", backend_payload["backend_id"]),
                "role": cast("str", backend_payload["backend_role"]),
                "capabilities_sha256": receipt.payload_sha256,
            }
        )
    selected.sort(key=lambda item: (item["role"], item["backend_id"]))
    timestamp = generated_at or utc_now()
    object_hashes = sorted(item["payload_sha256"] for item in bindings)
    event_status = {
        "active": "started",
        "blocked": "blocked",
        "failed": "failed",
        "completed": "completed",
        "aborted": "failed",
    }[status]
    collected_artifacts = _collect_artifacts(all_documents)
    explicit_keys: set[tuple[object, object]] = set()
    for value in artifacts:
        artifact = dict(value)
        artifact_key = (artifact.get("path_base"), artifact.get("path"))
        if artifact_key in explicit_keys:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "run artifact paths must be unique")
        explicit_keys.add(artifact_key)
        existing = next(
            (
                item
                for item in collected_artifacts
                if (item.get("path_base"), item.get("path")) == artifact_key
            ),
            None,
        )
        if existing is not None:
            if existing != artifact:
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "run artifact conflicts with an existing workflow artifact",
                )
            continue
        collected_artifacts.append(artifact)
    collected_artifacts.sort(
        key=lambda item: (
            cast("str", item["path_base"]),
            cast("str", item["path"]),
            cast("str", item["sha256"]),
        )
    )

    payload_without_id = {
        "manifest_revision": 1,
        "previous_manifest_payload_sha256": None,
        "status": status,
        "current_phase": current_phase,
        "policy_profile": {
            "name": "v0.1-exact-plain-text-only",
            "version": "1",
            "payload_sha256": POLICY_SHA256,
        },
        "source_manifest": _object_binding(source_manifest),
        "selected_backends": selected,
        "phase_events": [
            {
                "phase": current_phase,
                "status": event_status,
                "started_at": timestamp,
                "completed_at": None if event_status == "started" else timestamp,
                "input_sha256s": object_hashes,
                "output_sha256s": object_hashes,
                "diagnostic_ids": [],
            }
        ],
        "artifacts": collected_artifacts,
        "object_bindings": bindings,
        "source_origin_pre_sha256": source_payload["source_tree_sha256"],
        "source_origin_post_sha256": source_payload["source_tree_sha256"],
        "diagnostics": [],
    }
    object_id = stable_id("runm_", [run_id, 1, payload_without_id])
    return make_envelope(
        schema_name="RunManifest",
        object_id=object_id,
        run_id=run_id,
        generated_at=timestamp,
        producer=producer_identity("run-manifest-builder-v1"),
        payload=payload_without_id,
    )


def build_source_manifest_document(
    discovery: ProjectDiscovery,
    *,
    run_id: str,
    snapshot_manifest_file: Path,
    snapshot_artifact_path: str,
    confidentiality: Confidentiality = "derived_private",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Seal one immutable discovery result and its snapshot-manifest evidence."""

    snapshot_digest = digest_file(snapshot_manifest_file, max_bytes=16 * 1024 * 1024)
    source_manifest_id = derive_source_manifest_id(discovery.source_tree_sha256)
    payload = {
        "source_manifest_id": source_manifest_id,
        "main_document": discovery.main_document,
        "snapshot_artifact": {
            "artifact_id": derive_artifact_id(snapshot_digest.sha256),
            "path": validate_relative_path(snapshot_artifact_path),
            "path_base": "run_root",
            "role": "source_snapshot_manifest",
            "media_type": "application/json",
            "size_bytes": snapshot_digest.size_bytes,
            "sha256": snapshot_digest.sha256,
            "immutable": True,
            "confidentiality": confidentiality,
        },
        "source_tree_sha256": discovery.source_tree_sha256,
        "files": [item.as_dict() for item in discovery.files],
        "dependency_edges": [item.as_dict() for item in discovery.dependency_edges],
        "engine_hints": [item.as_dict() for item in discovery.engine_hints],
        "external_references": [
            {
                "reference": item.reference,
                "status": item.status,
                "diagnostic_ids": [],
            }
            for item in discovery.external_references
        ],
        "discovery_profile": {
            "name": "latex-project-discovery",
            "version": "1",
            "configuration_sha256": discovery.profile_sha256,
        },
        "immutability": {
            "snapshot_read_only": True,
            "source_origin_sha256": discovery.source_tree_sha256,
        },
    }
    return make_envelope(
        schema_name="SourceManifest",
        object_id=source_manifest_id,
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        producer=producer_identity(SOURCE_MANIFEST_INTERFACE_VERSION),
        payload=payload,
    )


def build_backend_capabilities_document(
    capabilities: BackendCapabilities,
    *,
    run_id: str | None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    return make_envelope(
        schema_name="BackendCapabilities",
        object_id=capabilities.object_id,
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        producer=producer_identity("backend-capabilities-builder-v1"),
        payload=capabilities.as_payload(),
    )


def build_revision_reader_capabilities_document(
    *,
    run_id: str | None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Describe the canonical OOXML reader implemented by this package."""

    evidence = sha256_canonical(
        {
            "fixture": "e0-minimal-paper",
            "raw_events": 9,
            "normalized_changes": 7,
            "precision": 1.0,
            "recall": 1.0,
        }
    )
    full_feature = {
        "support": "full",
        "representations": ["wordprocessingml"],
        "preserves_metadata": ["author", "timestamp", "native_id", "fragment_sha256"],
        "diagnostic_codes": [],
        "evidence_sha256": evidence,
    }
    payload = {
        "backend_id": "canonical-ooxml-revision-reader",
        "backend_role": "revision_reader",
        "tool": producer_identity("revision-reader-v1alpha1"),
        "probe_platform": {
            "os": platform.system() or "unknown",
            "architecture": platform.machine() or "unknown",
            "python_version": platform.python_version() or None,
            "external_tools": {},
        },
        "operations": ["read_revisions"],
        "features": {
            name: dict(full_feature)
            for name in (
                "raw_insert",
                "raw_delete",
                "move_pair",
                "format_revision",
                "comments",
                "comment_range",
            )
        },
        "determinism": "verified",
        "tested_contracts": [
            {
                "fixture": "e0-minimal-paper",
                "result": "pass",
                "evidence_sha256": evidence,
            }
        ],
        "limitations": [
            {
                "code": "E_BACKEND_CAPABILITY_MISSING",
                "structure_kind": "modern-comments-extension-semantics",
                "fallback": "preserve canonical classic comment evidence",
            }
        ],
        "security_requirements": [
            "immutable_returned_original",
            "bounded_zip_and_xml",
            "no_external_relationships",
            "no_macro_execution",
        ],
        "probe_evidence": [],
    }
    object_id = stable_id("cap_", payload)
    return make_envelope(
        schema_name="BackendCapabilities",
        object_id=object_id,
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        producer=producer_identity("revision-reader-capabilities-builder-v1"),
        payload=payload,
    )


def bookmark_bindings_from_source_map(
    source_map: Mapping[str, Any],
) -> dict[str, BookmarkBinding]:
    """Convert only exact, named bookmark mappings from a sealed SourceMap."""

    receipt = validate_contract(source_map)
    if receipt.schema_name != "SourceMap":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "bookmark bindings require a SourceMap")
    payload = cast("Mapping[str, Any]", source_map["payload"])
    mappings = cast("list[Mapping[str, Any]]", payload["mappings"])
    result: dict[str, BookmarkBinding] = {}
    for mapping in mappings:
        anchor = cast("Mapping[str, Any]", mapping["docx_anchor"])
        if mapping["status"] != "exact" or anchor["kind"] != "bookmark":
            continue
        name = anchor["name"]
        if not isinstance(name, str) or not name:
            continue
        if name in result:
            raise ContractError(ErrorCode.MAP_AMBIGUOUS, "SourceMap repeats a bookmark name")
        fingerprint = cast("Mapping[str, Any]", mapping["source_fingerprint"])
        provenance = cast("Mapping[str, Any]", mapping["text_provenance"])
        segments = cast("list[Mapping[str, Any]]", provenance["segments"])
        result[name] = BookmarkBinding(
            unit_id=cast("str", mapping["unit_id"]),
            source_location=cast("Mapping[str, Any]", mapping["source_location"]),
            normalized_text_sha256=cast("str", fingerprint["normalized_text_sha256"]),
            review_length=cast("int", provenance["review_length"]),
            text_provenance=tuple(
                TextProvenanceSegment(
                    review_start=cast("int", segment["review_start"]),
                    review_end=cast("int", segment["review_end"]),
                    source_start_byte=cast("int", segment["source_start_byte"]),
                    source_end_byte=cast("int", segment["source_end_byte"]),
                    transformation=cast("str", segment["transformation"]),
                    auto_patchable=cast("bool", segment["auto_patchable"]),
                )
                for segment in segments
            ),
        )
    return result


def build_review_ir_document(
    discovery: ProjectDiscovery,
    outcome: ExportOutcome,
    *,
    run_id: str,
    source_manifest_sha256: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    payload = review_ir_payload(
        discovery,
        outcome.units,
        source_manifest_sha256=source_manifest_sha256,
    )
    object_id = stable_id("ir_", payload)
    return make_envelope(
        schema_name="ReviewIR",
        object_id=object_id,
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        producer=producer_identity("review-ir-builder-v1"),
        payload=payload,
    )


def build_source_map_document(
    anchoring: AnchoringResult,
    *,
    run_id: str,
    source_manifest_sha256: str,
    review_ir_sha256: str,
    export_report_payload: Mapping[str, Any] | None = None,
    interface_version: str = SOURCE_MAP_INTERFACE_VERSION,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if interface_version == SOURCE_MAP_INTERFACE_VERSION:
        if export_report_payload is None:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "SourceMap v2 requires an ExportReport payload commitment",
            )
        commitment = export_report_commitment(export_report_payload)
    elif interface_version == LEGACY_SOURCE_MAP_INTERFACE_VERSION:
        if export_report_payload is not None:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "legacy SourceMap cannot contain an ExportReport commitment",
            )
        commitment = None
    else:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "unsupported SourceMap producer interface",
        )
    payload = anchoring.source_map_payload(
        source_manifest_sha256=source_manifest_sha256,
        report_commitment=commitment,
        review_ir_sha256=review_ir_sha256,
    )
    object_id = stable_id("map_", payload)
    return make_envelope(
        schema_name="SourceMap",
        object_id=object_id,
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        producer=producer_identity(interface_version),
        payload=payload,
    )


def build_export_report_document(
    outcome: ExportOutcome,
    *,
    run_id: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    payload = outcome.report.as_payload()
    object_id = stable_id("export_", payload)
    return make_envelope(
        schema_name="ExportReport",
        object_id=object_id,
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        producer=producer_identity(EXPORT_REPORT_INTERFACE_VERSION),
        payload=payload,
    )


__all__ = [
    "Confidentiality",
    "EXPORT_REPORT_INTERFACE_VERSION",
    "LEGACY_SOURCE_MAP_INTERFACE_VERSION",
    "RunPhase",
    "SOURCE_MANIFEST_INTERFACE_VERSION",
    "SOURCE_MAP_INTERFACE_VERSION",
    "build_backend_capabilities_document",
    "build_export_report_document",
    "build_revision_reader_capabilities_document",
    "build_review_ir_document",
    "build_run_manifest_document",
    "build_source_manifest_document",
    "build_source_map_document",
    "bookmark_bindings_from_source_map",
    "producer_identity",
    "utc_now",
]
