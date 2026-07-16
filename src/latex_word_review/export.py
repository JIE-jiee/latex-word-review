"""Atomic post-export bookmark anchoring, source maps, and report assembly."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.backends.base import BackendRequest, BackendResult, ExportBackend
from latex_word_review.canonical import sha256_canonical
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.docx_reader import read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import (
    ExportFeatureResult,
    ExportFinding,
    ExportReport,
    ExportValidation,
    ReviewDocxArtifact,
)
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.inspection import DocxInspection, inspect_docx
from latex_word_review.paths import ensure_disjoint_roots, validate_relative_path
from latex_word_review.source_units import (
    SourceUnit,
    normalize_review_text,
    review_ir_payload,
    scan_source_units,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
DOCUMENT_PART = "word/document.xml"
ANCHOR_PROFILE_VERSION = "1"


@dataclass(frozen=True, slots=True)
class SourceMapping:
    unit: SourceUnit
    status: Literal["exact", "degraded", "unmapped", "conflict"]
    bookmark_name: str | None
    bookmark_id: str | None
    paragraph_id: str | None

    def as_contract(self) -> dict[str, object]:
        exact = self.status == "exact"
        return {
            "unit_id": self.unit.unit_id,
            "source_location": self.unit.source_location(),
            "docx_anchor": {
                "part_uri": DOCUMENT_PART,
                "kind": "bookmark" if exact else "none",
                "name": self.bookmark_name,
                "paragraph_id": self.paragraph_id,
                "object_id": self.bookmark_id,
            },
            "source_fingerprint": {
                "slice_sha256": self.unit.slice_sha256,
                "normalized_text_sha256": self.unit.normalized_text_sha256,
                "neighbor_sha256": sha256_canonical(list(self.unit.neighbor_unit_ids)),
            },
            "mapping_method": "bookmark" if exact else "none",
            "confidence": 1.0 if exact else 0.0,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class AnchoringResult:
    mappings: tuple[SourceMapping, ...]
    findings: tuple[ExportFinding, ...]
    docx_sha256: str

    @property
    def coverage(self) -> dict[str, int]:
        counts = Counter(mapping.status for mapping in self.mappings)
        return {
            "total": len(self.mappings),
            "exact": counts["exact"],
            "degraded": counts["degraded"],
            "unmapped": counts["unmapped"],
            "conflict": counts["conflict"],
        }

    def source_map_payload(
        self,
        *,
        source_manifest_sha256: str,
        review_ir_sha256: str,
    ) -> dict[str, object]:
        return {
            "source_manifest_sha256": source_manifest_sha256,
            "review_ir_sha256": review_ir_sha256,
            "review_docx_sha256": self.docx_sha256,
            "anchor_profile": {
                "name": "unique-normalized-text-bookmark",
                "version": ANCHOR_PROFILE_VERSION,
                "configuration_sha256": sha256_canonical(
                    {
                        "name": "unique-normalized-text-bookmark",
                        "version": ANCHOR_PROFILE_VERSION,
                        "ambiguity": "never-guess",
                    }
                ),
            },
            "mappings": [mapping.as_contract() for mapping in self.mappings],
            "coverage": self.coverage,
            "diagnostics": [finding.as_diagnostic() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class ExportBindings:
    source_manifest_sha256: str
    artifact_path: str
    confidentiality: Literal["public_fixture", "local_private", "derived_private"] = (
        "derived_private"
    )

    def __post_init__(self) -> None:
        validate_relative_path(self.artifact_path)


@dataclass(frozen=True, slots=True)
class ExportOutcome:
    backend_result: BackendResult
    report: ExportReport
    units: tuple[SourceUnit, ...]
    anchoring: AnchoringResult | None
    inspection: DocxInspection | None
    output_path: Path | None


def _paragraph_text(paragraph: etree._Element) -> str:
    fragments: list[str] = []
    for element in paragraph.iter():
        if element.tag == f"{{{W_NS}}}t" and element.text:
            fragments.append(element.text)
        elif element.tag in {f"{{{W_NS}}}tab", f"{{{W_NS}}}br", f"{{{W_NS}}}cr"}:
            fragments.append(" ")
    return normalize_review_text("".join(fragments))


def _bookmark_name(unit_id: str) -> str:
    suffix = unit_id.removeprefix("unit_")
    return f"lwr_{suffix[:32]}"


def _insert_bookmark(
    paragraph: etree._Element,
    *,
    name: str,
    numeric_id: int,
) -> None:
    start = etree.Element(f"{{{W_NS}}}bookmarkStart")
    start.set(f"{{{W_NS}}}id", str(numeric_id))
    start.set(f"{{{W_NS}}}name", name)
    end = etree.Element(f"{{{W_NS}}}bookmarkEnd")
    end.set(f"{{{W_NS}}}id", str(numeric_id))
    insert_at = 1 if len(paragraph) and paragraph[0].tag == f"{{{W_NS}}}pPr" else 0
    paragraph.insert(insert_at, start)
    paragraph.append(end)


def _write_anchored_package(
    source: Path,
    destination: Path,
    document_xml: bytes,
    *,
    expected_sha256: str,
) -> None:
    raw = read_stable_bytes(source, max_bytes=128 * 1024 * 1024)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "DOCX changed before anchoring")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.anchor-",
        suffix=".docx",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive,
            zipfile.ZipFile(temporary, mode="w") as output,
        ):
            for info in archive.infolist():
                data = (
                    document_xml if info.filename == DOCUMENT_PART else archive.read(info.filename)
                )
                output.writestr(info, data)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        read_docx_package(temporary)
        os.replace(temporary, destination)
    except Exception:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise


def anchor_source_units(
    source_docx: Path,
    destination_docx: Path,
    units: tuple[SourceUnit, ...],
) -> AnchoringResult:
    """Insert bookmarks only for one-to-one normalized paragraph matches."""

    package = read_docx_package(source_docx)
    root = package.xml_root(DOCUMENT_PART)
    paragraphs_by_text: dict[str, list[etree._Element]] = defaultdict(list)
    for paragraph in root.iter(f"{{{W_NS}}}p"):
        normalized = _paragraph_text(paragraph)
        if normalized:
            paragraphs_by_text[normalized].append(paragraph)
    source_counts = Counter(unit.normalized_text for unit in units)
    existing_ids = [
        int(value)
        for element in root.iter(f"{{{W_NS}}}bookmarkStart")
        if (value := element.get(f"{{{W_NS}}}id")) is not None and value.isdigit()
    ]
    next_id = max(existing_ids, default=0) + 1
    mappings: list[SourceMapping] = []
    findings: list[ExportFinding] = []
    for unit in units:
        candidates = paragraphs_by_text.get(unit.normalized_text, [])
        if source_counts[unit.normalized_text] == 1 and len(candidates) == 1:
            paragraph = candidates[0]
            name = _bookmark_name(unit.unit_id)
            bookmark_id = str(next_id)
            _insert_bookmark(paragraph, name=name, numeric_id=next_id)
            next_id += 1
            mappings.append(
                SourceMapping(
                    unit=unit,
                    status="exact",
                    bookmark_name=name,
                    bookmark_id=bookmark_id,
                    paragraph_id=paragraph.get(f"{{{W14_NS}}}paraId"),
                )
            )
            continue
        ambiguous = source_counts[unit.normalized_text] > 1 or len(candidates) > 1
        code = ErrorCode.MAP_AMBIGUOUS if ambiguous else ErrorCode.MAP_UNMATCHED
        status: Literal["unmapped", "conflict"] = "conflict" if ambiguous else "unmapped"
        mappings.append(
            SourceMapping(
                unit=unit,
                status=status,
                bookmark_name=None,
                bookmark_id=None,
                paragraph_id=None,
            )
        )
        findings.append(
            ExportFinding(
                code=code,
                severity="warning",
                phase="export",
                message=(
                    "source text has multiple possible DOCX paragraphs; no anchor was chosen"
                    if ambiguous
                    else "source text has no exact DOCX paragraph; no anchor was chosen"
                ),
                recoverable=True,
                fingerprint=unit.normalized_text_sha256,
                unit_id=unit.unit_id,
                source_location=unit.source_location(),
                remediation="map this unit manually before applying returned revisions",
            )
        )
    document_xml = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    _write_anchored_package(
        source_docx,
        destination_docx,
        document_xml,
        expected_sha256=package.file_sha256,
    )
    anchored_package = read_docx_package(destination_docx)
    return AnchoringResult(
        mappings=tuple(mappings),
        findings=tuple(findings),
        docx_sha256=anchored_package.file_sha256,
    )


def _pipeline_path(final_output: Path, label: str) -> Path:
    final_output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{final_output.name}.{label}-",
        suffix=".docx",
        dir=final_output.parent,
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _cleanup_pipeline_path(path: Path, final_output: Path) -> None:
    if path.parent.resolve(strict=True) != final_output.parent.resolve(strict=True):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "pipeline stage escaped its parent")
    if not path.name.startswith(f".{final_output.name}."):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "refusing to clean unowned pipeline stage",
        )
    with suppress(FileNotFoundError):
        path.unlink()


def export_review_docx(
    backend: ExportBackend,
    request: BackendRequest,
    discovery: ProjectDiscovery,
    bindings: ExportBindings,
) -> ExportOutcome:
    """Convert, anchor, inspect, then atomically publish one review DOCX."""

    source_root, final_output = ensure_disjoint_roots(request.source_root, request.output_path)
    if final_output.exists() or final_output.is_symlink():
        raise ContractError(ErrorCode.BACKEND_FAILED, "review DOCX output already exists")
    current = discover_project(source_root, main_document=request.main_document)
    if (
        current.source_tree_sha256 != discovery.source_tree_sha256
        or current.files != discovery.files
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source discovery binding changed")
    units = scan_source_units(source_root, discovery)
    review_payload = review_ir_payload(
        discovery,
        units,
        source_manifest_sha256=bindings.source_manifest_sha256,
    )
    review_ir_sha256 = sha256_canonical(review_payload)
    backend_stage = _pipeline_path(final_output, "backend")
    anchored_stage = _pipeline_path(final_output, "anchored")
    backend_result: BackendResult | None = None
    try:
        backend_result = backend.export(
            BackendRequest(
                source_root=source_root,
                main_document=request.main_document,
                output_path=backend_stage,
                expected_source_tree_sha256=discovery.source_tree_sha256,
                timeout_s=request.timeout_s,
                max_output_bytes=request.max_output_bytes,
            )
        )
        if not backend_result.succeeded:
            report = ExportReport(
                status="failed",
                source_manifest_sha256=bindings.source_manifest_sha256,
                backend_capabilities_sha256=backend_result.capabilities.payload_sha256,
                review_ir_sha256=None,
                source_map_sha256=None,
                review_docx=None,
                source_metrics={"paragraphs": len(units)},
                output_metrics={},
                feature_results=(),
                findings=backend_result.findings,
                validation=ExportValidation("blocked", "not_run", "blocked", "blocked"),
            )
            return ExportOutcome(backend_result, report, units, None, None, None)
        anchoring = anchor_source_units(backend_stage, anchored_stage, units)
        inspection = inspect_docx(anchored_stage)
        findings = backend_result.findings + anchoring.findings + inspection.findings
        if not inspection.package_valid or not inspection.structure_inspected:
            report = ExportReport(
                status="failed",
                source_manifest_sha256=bindings.source_manifest_sha256,
                backend_capabilities_sha256=backend_result.capabilities.payload_sha256,
                review_ir_sha256=None,
                source_map_sha256=None,
                review_docx=None,
                source_metrics={"paragraphs": len(units)},
                output_metrics=inspection.as_metrics(),
                feature_results=(),
                findings=findings,
                validation=inspection.validation,
            )
            return ExportOutcome(backend_result, report, units, anchoring, inspection, None)
        source_map_payload = anchoring.source_map_payload(
            source_manifest_sha256=bindings.source_manifest_sha256,
            review_ir_sha256=review_ir_sha256,
        )
        source_map_sha256 = sha256_canonical(source_map_payload)
        try:
            os.link(anchored_stage, final_output, follow_symlinks=False)
            anchored_stage.unlink()
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "review DOCX output appeared during publication",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "review DOCX could not be atomically published",
            ) from exc
        artifact = ReviewDocxArtifact.from_file(
            final_output,
            artifact_path=bindings.artifact_path,
            confidentiality=bindings.confidentiality,
        )
        exact = anchoring.coverage["exact"]
        status: Literal["success", "partial"] = (
            "success" if exact == len(units) and not findings else "partial"
        )
        feature_status: Literal["preserved", "degraded"] = (
            "preserved" if exact == len(units) else "degraded"
        )
        feature_ids = tuple(finding.diagnostic_id for finding in anchoring.findings)
        report = ExportReport(
            status=status,
            source_manifest_sha256=bindings.source_manifest_sha256,
            backend_capabilities_sha256=backend_result.capabilities.payload_sha256,
            review_ir_sha256=review_ir_sha256,
            source_map_sha256=source_map_sha256,
            review_docx=artifact,
            source_metrics={"paragraphs": len(units)},
            output_metrics=inspection.as_metrics(),
            feature_results=(
                ExportFeatureResult(
                    feature="body_text",
                    status=feature_status,
                    source_count=len(units),
                    output_count=exact,
                    diagnostic_ids=feature_ids,
                ),
            ),
            findings=findings,
            validation=inspection.validation,
        )
        return ExportOutcome(
            backend_result,
            report,
            units,
            anchoring,
            inspection,
            final_output,
        )
    finally:
        _cleanup_pipeline_path(backend_stage, final_output)
        _cleanup_pipeline_path(anchored_stage, final_output)


__all__ = [
    "ANCHOR_PROFILE_VERSION",
    "AnchoringResult",
    "ExportBindings",
    "ExportOutcome",
    "SourceMapping",
    "anchor_source_units",
    "export_review_docx",
]
