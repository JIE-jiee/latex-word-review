"""Common export backend protocol, capabilities, and atomic staging helpers."""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from latex_word_review.canonical import sha256_canonical
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.docx_reader import read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import ExportFinding
from latex_word_review.hashing import FileDigest, digest_file
from latex_word_review.ids import stable_id
from latex_word_review.paths import ensure_disjoint_roots, resolve_within, validate_relative_path


@dataclass(frozen=True, slots=True)
class FeatureCapability:
    support: Literal["full", "partial", "none", "unknown"]
    representations: tuple[str, ...]
    preserves_metadata: tuple[str, ...] = ()
    diagnostic_codes: tuple[ErrorCode, ...] = ()
    evidence_sha256: str = "sha256:" + "0" * 64

    def as_contract(self) -> dict[str, object]:
        return {
            "support": self.support,
            "representations": list(self.representations),
            "preserves_metadata": list(self.preserves_metadata),
            "diagnostic_codes": [code.value for code in self.diagnostic_codes],
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class TestedContract:
    fixture: str
    result: Literal["pass", "fail", "blocked"]
    evidence_sha256: str

    def as_contract(self) -> dict[str, str]:
        return {
            "fixture": self.fixture,
            "result": self.result,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class CapabilityLimitation:
    code: ErrorCode
    structure_kind: str
    fallback: str | None

    def as_contract(self) -> dict[str, str | None]:
        return {
            "code": self.code.value,
            "structure_kind": self.structure_kind,
            "fallback": self.fallback,
        }


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Versioned capability evidence shared by every export backend."""

    backend_id: str
    tool_name: str
    tool_version: str | None
    interface_version: str
    distribution: str | None
    configuration_sha256: str
    features: tuple[tuple[str, FeatureCapability], ...]
    determinism: Literal["verified", "claimed", "not_verified", "failed"]
    tested_contracts: tuple[TestedContract, ...]
    limitations: tuple[CapabilityLimitation, ...]
    security_requirements: tuple[str, ...]
    external_tools: tuple[tuple[str, str | None], ...] = ()

    @property
    def object_id(self) -> str:
        return stable_id("cap_", [self.backend_id, self.tool_version, self.interface_version])

    @property
    def payload_sha256(self) -> str:
        return sha256_canonical(self.as_payload())

    def as_payload(self) -> dict[str, object]:
        return {
            "backend_id": self.backend_id,
            "backend_role": "export",
            "tool": {
                "name": self.tool_name,
                "version": self.tool_version,
                "interface_version": self.interface_version,
                "distribution": self.distribution,
                "executable_sha256": None,
                "configuration_sha256": self.configuration_sha256,
            },
            "probe_platform": {
                "os": platform.system() or "unknown",
                "architecture": platform.machine() or "unknown",
                "python_version": platform.python_version() or None,
                "external_tools": dict(self.external_tools),
            },
            "operations": ["export_docx"],
            "features": {name: capability.as_contract() for name, capability in self.features},
            "determinism": self.determinism,
            "tested_contracts": [item.as_contract() for item in self.tested_contracts],
            "limitations": [item.as_contract() for item in self.limitations],
            "security_requirements": list(self.security_requirements),
            "probe_evidence": [],
        }


@dataclass(frozen=True, slots=True)
class BackendRequest:
    source_root: Path
    main_document: str
    output_path: Path
    expected_source_tree_sha256: str | None = None
    timeout_s: float = 60.0
    max_output_bytes: int = 1024 * 1024
    revision_view: Literal["source", "clean", "display"] = "source"
    revision_aliases: tuple[Literal["add", "delete"], ...] = ()


@dataclass(frozen=True, slots=True)
class BackendResult:
    status: Literal["success", "failed"]
    capabilities: BackendCapabilities
    artifact_name: str | None
    artifact_sha256: str | None
    artifact_size_bytes: int | None
    findings: tuple[ExportFinding, ...]
    native_report: dict[str, object]
    timed_out: bool = False
    returncode: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@runtime_checkable
class ExportBackend(Protocol):
    """Minimal adapter boundary; no backend-native AST crosses it."""

    def capabilities(self) -> BackendCapabilities: ...

    def export(self, request: BackendRequest) -> BackendResult: ...


@dataclass(frozen=True, slots=True)
class PreparedExport:
    source_root: Path
    main_document: str
    main_path: Path
    output_path: Path
    temporary_path: Path
    discovery: ProjectDiscovery


def prepare_export(request: BackendRequest, *, owner: str) -> PreparedExport:
    if not 0 < request.timeout_s <= 60:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "backend timeout must be in (0, 60]")
    if not 0 < request.max_output_bytes <= 16 * 1024 * 1024:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "backend output limit is invalid")
    if request.revision_view not in {"source", "clean", "display"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "backend revision view is invalid")
    if request.revision_aliases not in {
        (),
        ("add",),
        ("delete",),
        ("add", "delete"),
    }:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "backend revision aliases must be unique and canonically ordered",
        )
    if request.revision_view == "source" and request.revision_aliases:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "backend revision aliases require a clean or display revision view",
        )
    main_document = validate_relative_path(request.main_document)
    if not main_document.lower().endswith(".tex"):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "main document must be a .tex file")
    source_root, output_path = ensure_disjoint_roots(request.source_root, request.output_path)
    if output_path.suffix.lower() != ".docx":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "backend output must have a .docx suffix")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    main_path = resolve_within(source_root, main_document)
    if not main_path.is_file():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "main document is not a regular file")
    discovery = discover_project(source_root, main_document=main_document)
    if discovery.external_references:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "source has rejected external references")
    if (
        request.expected_source_tree_sha256 is not None
        and request.expected_source_tree_sha256 != discovery.source_tree_sha256
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source tree binding does not match")

    prefix = f".{output_path.name}.{owner}-"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=prefix,
        suffix=".docx",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()
    return PreparedExport(
        source_root=source_root,
        main_document=main_document,
        main_path=main_path,
        output_path=output_path,
        temporary_path=temporary_path,
        discovery=discovery,
    )


def write_stage_bytes(prepared: PreparedExport, data: bytes) -> None:
    try:
        with prepared.temporary_path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(ErrorCode.BACKEND_FAILED, "backend stage could not be written") from exc


def verify_source_unchanged(prepared: PreparedExport) -> None:
    after = discover_project(prepared.source_root, main_document=prepared.main_document)
    if (
        after.source_tree_sha256 != prepared.discovery.source_tree_sha256
        or after.files != prepared.discovery.files
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "backend modified the source tree")


def publish_stage(prepared: PreparedExport) -> FileDigest:
    read_docx_package(prepared.temporary_path)
    verify_source_unchanged(prepared)
    try:
        os.link(prepared.temporary_path, prepared.output_path, follow_symlinks=False)
        prepared.temporary_path.unlink()
    except FileExistsError as exc:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "backend output already exists; refusing to overwrite it",
        ) from exc
    except OSError as exc:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "backend artifact publication failed",
        ) from exc
    return digest_file(prepared.output_path, max_bytes=128 * 1024 * 1024)


def cleanup_stage(prepared: PreparedExport) -> None:
    path = prepared.temporary_path
    expected_prefix = f".{prepared.output_path.name}."
    if path.parent.resolve(strict=True) != prepared.output_path.parent.resolve(strict=True):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "backend stage escaped its parent")
    if not path.name.startswith(expected_prefix) or path.suffix.lower() != ".docx":
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "refusing to clean an unowned stage")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "backend stage cleanup failed") from exc


def successful_result(
    prepared: PreparedExport,
    capabilities: BackendCapabilities,
    digest: FileDigest,
    *,
    findings: tuple[ExportFinding, ...] = (),
    native_report: dict[str, object] | None = None,
    returncode: int | None = None,
) -> BackendResult:
    return BackendResult(
        status="success",
        capabilities=capabilities,
        artifact_name=prepared.output_path.name,
        artifact_sha256=digest.sha256,
        artifact_size_bytes=digest.size_bytes,
        findings=findings,
        native_report=native_report or {},
        returncode=returncode,
    )


def failed_result(
    capabilities: BackendCapabilities,
    *,
    code: ErrorCode = ErrorCode.BACKEND_FAILED,
    message: str = "export backend failed without publishing an artifact",
    timed_out: bool = False,
    returncode: int | None = None,
    native_report: dict[str, object] | None = None,
) -> BackendResult:
    return BackendResult(
        status="failed",
        capabilities=capabilities,
        artifact_name=None,
        artifact_sha256=None,
        artifact_size_bytes=None,
        findings=(
            ExportFinding(
                code=code,
                severity="error",
                phase="export",
                message=message,
                recoverable=False,
                fingerprint=capabilities.payload_sha256,
            ),
        ),
        native_report=native_report or {},
        timed_out=timed_out,
        returncode=returncode,
    )


def runtime_configuration_sha256(label: str, values: dict[str, object]) -> str:
    return sha256_canonical(
        {"adapter": label, "python_major_minor": list(sys.version_info[:2]), **values}
    )


__all__ = [
    "BackendCapabilities",
    "BackendRequest",
    "BackendResult",
    "CapabilityLimitation",
    "ExportBackend",
    "FeatureCapability",
    "PreparedExport",
    "TestedContract",
]
