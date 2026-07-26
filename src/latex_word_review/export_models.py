"""Typed export findings and the v1alpha ExportReport payload model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_file
from latex_word_review.ids import derive_artifact_id, derive_diagnostic_id
from latex_word_review.paths import validate_relative_path

Severity = Literal["info", "warning", "error", "fatal"]
EXPORT_REPORT_COMMITMENT_PROFILE_NAME = "export-report-payload-source-map-null"
EXPORT_REPORT_COMMITMENT_PROFILE_VERSION = "1"


def normalized_export_report_payload(report_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only the self-cyclic SourceMap hash before commitment hashing."""

    normalized = dict(report_payload)
    if "source_map_sha256" not in normalized:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "ExportReport commitment requires source_map_sha256",
        )
    normalized["source_map_sha256"] = None
    return normalized


def export_report_commitment(report_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Commit to the complete ExportReport payload using a fixed JCS profile."""

    profile_configuration = {
        "canonicalization": "RFC8785",
        "normalization": "source_map_sha256=null",
        "scope": "complete-export-report-payload",
    }
    return {
        "profile": {
            "name": EXPORT_REPORT_COMMITMENT_PROFILE_NAME,
            "version": EXPORT_REPORT_COMMITMENT_PROFILE_VERSION,
            "configuration_sha256": sha256_canonical(profile_configuration),
        },
        "payload_sha256": sha256_canonical(normalized_export_report_payload(report_payload)),
    }


Phase = Literal["export", "inspect"]


@dataclass(frozen=True, slots=True)
class ExportFinding:
    """One path-free finding compatible with the public Diagnostic schema."""

    code: ErrorCode
    severity: Severity
    phase: Phase
    message: str
    recoverable: bool
    fingerprint: str | None = None
    unit_id: str | None = None
    source_location: dict[str, object] | None = None
    remediation: str | None = None

    @property
    def diagnostic_id(self) -> str:
        return derive_diagnostic_id(
            code=self.code.value,
            phase=self.phase,
            location_or_evidence_fingerprint=self.fingerprint or self.unit_id,
        )

    def as_diagnostic(self) -> dict[str, object]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "code": self.code.value,
            "severity": self.severity,
            "phase": self.phase,
            "message": self.message,
            "recoverable": self.recoverable,
            "unit_id": self.unit_id,
            "change_id": None,
            "source_location": self.source_location,
            "evidence": None,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class ReviewDocxArtifact:
    """A final DOCX artifact reference without an absolute host path."""

    path: str
    size_bytes: int
    sha256: str
    confidentiality: Literal["public_fixture", "local_private", "derived_private"]
    role: Literal["review_docx", "latex_changes_display_docx"] = "review_docx"

    @classmethod
    def from_file(
        cls,
        file_path: Path,
        *,
        artifact_path: str,
        confidentiality: Literal["public_fixture", "local_private", "derived_private"],
        role: Literal["review_docx", "latex_changes_display_docx"] = "review_docx",
    ) -> ReviewDocxArtifact:
        digest = digest_file(file_path, max_bytes=128 * 1024 * 1024)
        return cls(
            path=validate_relative_path(artifact_path),
            size_bytes=digest.size_bytes,
            sha256=digest.sha256,
            confidentiality=confidentiality,
            role=role,
        )

    def as_contract(self) -> dict[str, object]:
        return {
            "artifact_id": derive_artifact_id(self.sha256),
            "path": self.path,
            "path_base": "run_root",
            "role": self.role,
            "media_type": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "immutable": True,
            "confidentiality": self.confidentiality,
        }


@dataclass(frozen=True, slots=True)
class ExportFeatureResult:
    feature: str
    status: Literal["preserved", "degraded", "unsupported", "failed"]
    source_count: int | None
    output_count: int | None
    diagnostic_ids: tuple[str, ...] = ()

    def as_contract(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "status": self.status,
            "source_count": self.source_count,
            "output_count": self.output_count,
            "diagnostic_ids": list(self.diagnostic_ids),
        }


@dataclass(frozen=True, slots=True)
class ExportValidation:
    ooxml: Literal["pass", "fail", "blocked", "not_run"]
    openability: Literal["pass", "fail", "blocked", "not_run"]
    relationships: Literal["pass", "fail", "blocked", "not_run"]
    structure: Literal["pass", "fail", "blocked", "not_run"]

    def as_contract(self) -> dict[str, str]:
        return {
            "ooxml": self.ooxml,
            "openability": self.openability,
            "relationships": self.relationships,
            "structure": self.structure,
        }


@dataclass(frozen=True, slots=True)
class ExportReport:
    """Schema-shaped export result; envelope sealing remains a caller concern."""

    status: Literal["success", "partial", "failed"]
    source_manifest_sha256: str
    backend_capabilities_sha256: str
    review_ir_sha256: str | None
    source_map_sha256: str | None
    review_docx: ReviewDocxArtifact | None
    image_overlay: dict[str, object]
    source_metrics: dict[str, int]
    output_metrics: dict[str, int]
    feature_results: tuple[ExportFeatureResult, ...]
    findings: tuple[ExportFinding, ...]
    validation: ExportValidation
    existing_changes_display_docx: ReviewDocxArtifact | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "source_manifest_sha256": self.source_manifest_sha256,
            "backend_capabilities_sha256": self.backend_capabilities_sha256,
            "review_ir_sha256": self.review_ir_sha256,
            "source_map_sha256": self.source_map_sha256,
            "review_docx": None if self.review_docx is None else self.review_docx.as_contract(),
            "existing_changes_display_docx": (
                None
                if self.existing_changes_display_docx is None
                else self.existing_changes_display_docx.as_contract()
            ),
            "image_overlay": dict(self.image_overlay),
            "metrics": {
                "source": dict(sorted(self.source_metrics.items())),
                "output": dict(sorted(self.output_metrics.items())),
            },
            "feature_results": [item.as_contract() for item in self.feature_results],
            "findings": [item.as_diagnostic() for item in self.findings],
            "validation": self.validation.as_contract(),
        }
        return payload


__all__ = [
    "EXPORT_REPORT_COMMITMENT_PROFILE_NAME",
    "EXPORT_REPORT_COMMITMENT_PROFILE_VERSION",
    "ExportFeatureResult",
    "ExportFinding",
    "ExportReport",
    "ExportValidation",
    "ReviewDocxArtifact",
    "export_report_commitment",
    "normalized_export_report_payload",
]
