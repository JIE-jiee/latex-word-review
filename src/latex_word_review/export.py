"""Atomic post-export bookmark anchoring, source maps, and report assembly."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.backends.base import BackendRequest, BackendResult, ExportBackend
from latex_word_review.backends.tex2word import (
    SUPPORTED_TEX2WORD_VERSION,
    TEX2WORD_INTERFACE_VERSION,
)
from latex_word_review.canonical import sha256_canonical
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.docx_anchor import AnchorReason, insert_unique_source_bookmarks
from latex_word_review.docx_reader import DocxPackage, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import (
    ExportFeatureResult,
    ExportFinding,
    ExportReport,
    ExportValidation,
    ReviewDocxArtifact,
    export_report_commitment,
)
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.image_overlay import (
    IMAGE_OVERLAY_MANIFEST,
    TEX2WORD_COMPATIBILITY_PROFILE,
    ImageOverlayResult,
    build_image_overlay,
    discard_image_overlay,
)
from latex_word_review.inspection import DocxInspection, inspect_docx
from latex_word_review.paths import ensure_disjoint_roots, validate_relative_path
from latex_word_review.review_layout import apply_review_layout
from latex_word_review.source_features import (
    reconcile_source_features,
    scan_source_features,
)
from latex_word_review.source_units import (
    SourceUnit,
    review_ir_payload,
    scan_source_units,
    text_provenance_profile,
)
from latex_word_review.word_fields import (
    finalize_word_fields,
    validate_frozen_field_state,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCUMENT_PART = "word/document.xml"
SETTINGS_PART = "word/settings.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"
DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"
SETTINGS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
)
SETTINGS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
ANCHOR_PROFILE_VERSION = "2"

_SETTINGS_AFTER_TRACK_REVISIONS = frozenset(
    {
        "doNotTrackMoves",
        "doNotTrackFormatting",
        "documentProtection",
        "autoFormatOverride",
        "styleLockTheme",
        "styleLockQFSet",
        "defaultTabStop",
        "autoHyphenation",
        "consecutiveHyphenLimit",
        "hyphenationZone",
        "doNotHyphenateCaps",
        "showEnvelope",
        "summaryLength",
        "clickAndTypeStyle",
        "defaultTableStyle",
        "evenAndOddHeaders",
        "bookFoldRevPrinting",
        "bookFoldPrinting",
        "bookFoldPrintingSheets",
        "drawingGridHorizontalSpacing",
        "drawingGridVerticalSpacing",
        "displayHorizontalDrawingGridEvery",
        "displayVerticalDrawingGridEvery",
        "doNotUseMarginsForDrawingGridOrigin",
        "drawingGridHorizontalOrigin",
        "drawingGridVerticalOrigin",
        "doNotShadeFormData",
        "noPunctuationKerning",
        "characterSpacingControl",
        "printTwoOnOne",
        "strictFirstAndLastChars",
        "noLineBreaksAfter",
        "noLineBreaksBefore",
        "savePreviewPicture",
        "doNotValidateAgainstSchema",
        "saveInvalidXml",
        "ignoreMixedContent",
        "alwaysShowPlaceholderText",
        "doNotDemarcateInvalidXml",
        "saveXmlDataOnly",
        "useXSLTWhenSaving",
        "saveThroughXslt",
        "showXMLTags",
        "alwaysMergeEmptyNamespace",
        "updateFields",
        "hdrShapeDefaults",
        "footnotePr",
        "endnotePr",
        "compat",
        "docVars",
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
            "text_provenance": {
                "profile": text_provenance_profile(),
                "review_length": len(self.unit.normalized_text),
                "segments": [segment.as_contract() for segment in self.unit.text_provenance],
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
    image_overlay: dict[str, object] | None = None

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
        report_commitment: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if self.image_overlay is None:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "SourceMap requires an image-overlay binding",
            )
        payload: dict[str, object] = {
            "source_manifest_sha256": source_manifest_sha256,
            "review_ir_sha256": review_ir_sha256,
            "review_docx_sha256": self.docx_sha256,
            "image_overlay": dict(self.image_overlay),
            "anchor_profile": {
                "name": "unique-normalized-substring-bookmark",
                "version": ANCHOR_PROFILE_VERSION,
                "configuration_sha256": sha256_canonical(
                    {
                        "name": "unique-normalized-substring-bookmark",
                        "version": ANCHOR_PROFILE_VERSION,
                        "ambiguity": "never-guess",
                        "range": "plain-direct-word-runs",
                    }
                ),
            },
            "mappings": [mapping.as_contract() for mapping in self.mappings],
            "coverage": self.coverage,
            "diagnostics": [finding.as_diagnostic() for finding in self.findings],
        }
        if report_commitment is not None:
            payload["export_report_commitment"] = dict(report_commitment)
        return payload


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
    image_overlay: ImageOverlayResult | None = None


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


def _enabled_on_off(element: etree._Element) -> bool:
    value = element.get(f"{{{W_NS}}}val")
    return value is None or value.casefold() not in {"0", "false", "off", "no"}


def _settings_xml(package: DocxPackage) -> bytes:
    if SETTINGS_PART in package.part_names:
        root = package.xml_root(SETTINGS_PART)
        if root.tag != f"{{{W_NS}}}settings":
            raise ContractError(
                ErrorCode.REVIEW_TRACKING_DISABLED,
                "DOCX settings part has an unexpected root element",
            )
    else:
        root = etree.Element(f"{{{W_NS}}}settings", nsmap={"w": W_NS})

    existing = root.findall(f"{{{W_NS}}}trackRevisions")
    if len(existing) > 1:
        raise ContractError(
            ErrorCode.REVIEW_TRACKING_DISABLED,
            "DOCX settings repeat the Track Changes control",
        )
    if existing:
        track_revisions = existing[0]
        track_revisions.attrib.pop(f"{{{W_NS}}}val", None)
    else:
        track_revisions = etree.Element(f"{{{W_NS}}}trackRevisions")
        insert_at = len(root)
        for index, child in enumerate(root):
            if etree.QName(child).namespace == W_NS and etree.QName(child).localname in (
                _SETTINGS_AFTER_TRACK_REVISIONS
            ):
                insert_at = index
                break
        root.insert(insert_at, track_revisions)
    for do_not_track in root.findall(f"{{{W_NS}}}doNotTrackFormatting"):
        root.remove(do_not_track)
    return _xml_bytes(root)


def _content_types_xml(package: DocxPackage) -> bytes:
    root = package.xml_root(CONTENT_TYPES_PART)
    override_tag = f"{{{CONTENT_TYPES_NS}}}Override"
    matches = [
        child
        for child in root.findall(override_tag)
        if child.get("PartName") == "/word/settings.xml"
    ]
    if len(matches) > 1 or (matches and matches[0].get("ContentType") != SETTINGS_CONTENT_TYPE):
        raise ContractError(
            ErrorCode.REVIEW_TRACKING_DISABLED,
            "DOCX settings content type is duplicated or invalid",
        )
    if not matches:
        override = etree.SubElement(root, override_tag)
        override.set("PartName", "/word/settings.xml")
        override.set("ContentType", SETTINGS_CONTENT_TYPE)
    return _xml_bytes(root)


def _relationship_target(relationship: etree._Element) -> str:
    target = relationship.get("Target") or ""
    return target.replace("\\", "/").removeprefix("./")


def _document_relationships_xml(package: DocxPackage) -> bytes:
    relationship_tag = f"{{{PACKAGE_REL_NS}}}Relationship"
    if DOCUMENT_RELS_PART in package.part_names:
        root = package.xml_root(DOCUMENT_RELS_PART)
    else:
        root = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships")
    matches = [
        child for child in root.findall(relationship_tag) if child.get("Type") == SETTINGS_REL_TYPE
    ]
    if len(matches) > 1 or (matches and _relationship_target(matches[0]) != "settings.xml"):
        raise ContractError(
            ErrorCode.REVIEW_TRACKING_DISABLED,
            "DOCX settings relationship is duplicated or invalid",
        )
    if not matches:
        numeric_ids = []
        for relationship in root.findall(relationship_tag):
            suffix = (relationship.get("Id") or "").removeprefix("rId")
            if suffix.isdigit():
                numeric_ids.append(int(suffix))
        relationship = etree.SubElement(root, relationship_tag)
        relationship.set("Id", f"rId{max(numeric_ids, default=0) + 1}")
        relationship.set("Type", SETTINGS_REL_TYPE)
        relationship.set("Target", "settings.xml")
    return _xml_bytes(root)


def _tracked_package_replacements(package: DocxPackage, document_xml: bytes) -> dict[str, bytes]:
    return {
        DOCUMENT_PART: document_xml,
        SETTINGS_PART: _settings_xml(package),
        CONTENT_TYPES_PART: _content_types_xml(package),
        DOCUMENT_RELS_PART: _document_relationships_xml(package),
    }


def _validate_tracking_enabled(package: DocxPackage) -> None:
    root = package.xml_root(SETTINGS_PART)
    controls = root.findall(f"{{{W_NS}}}trackRevisions")
    if len(controls) != 1 or not _enabled_on_off(controls[0]):
        raise ContractError(
            ErrorCode.REVIEW_TRACKING_DISABLED,
            "exported review DOCX does not have Track Changes enabled",
        )
    validate_frozen_field_state(package)
    revision_locals = {
        "ins",
        "del",
        "moveFrom",
        "moveTo",
        "rPrChange",
        "pPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "sectPrChange",
    }
    for part_uri in package.story_parts:
        story = package.xml_root(part_uri)
        if any(
            etree.QName(element).namespace == W_NS
            and etree.QName(element).localname in revision_locals
            for element in story.iter()
        ):
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "exported baseline unexpectedly contains pre-existing revisions",
            )


def _validate_anchor_destination(source: Path, destination: Path) -> None:
    try:
        source_identity = source.resolve(strict=True)
        destination_identity = destination.resolve(strict=False)
    except OSError as exc:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "anchored DOCX paths could not be resolved",
        ) from exc
    if source_identity == destination_identity:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "anchored DOCX destination must differ from its source",
        )
    if destination.exists() or destination.is_symlink():
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "anchored DOCX destination already exists",
        )


def _write_anchored_package(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
    *,
    expected_sha256: str,
) -> str:
    _validate_anchor_destination(source, destination)
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
            existing_names: set[str] = set()
            for info in archive.infolist():
                existing_names.add(info.filename)
                data = replacements.get(info.filename, archive.read(info.filename))
                output.writestr(info, data)
            for name in sorted(set(replacements) - existing_names):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                output.writestr(info, replacements[name])
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        package = read_docx_package(temporary)
        _validate_tracking_enabled(package)
        anchored_sha256 = package.file_sha256
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "anchored DOCX destination appeared during publication",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "anchored DOCX could not be atomically published",
            ) from exc
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise
    with suppress(OSError):
        temporary.unlink()
    return anchored_sha256


def anchor_source_units(
    source_docx: Path,
    destination_docx: Path,
    units: tuple[SourceUnit, ...],
) -> AnchoringResult:
    """Insert bookmarks only for globally unique, provable Word text ranges."""

    _validate_anchor_destination(source_docx, destination_docx)
    package = read_docx_package(source_docx)
    root = package.xml_root(DOCUMENT_PART)
    existing_ids = [
        int(value)
        for element in root.iter(f"{{{W_NS}}}bookmarkStart")
        if (value := element.get(f"{{{W_NS}}}id")) is not None and value.isdigit()
    ]
    placements = insert_unique_source_bookmarks(
        root,
        units,
        first_numeric_id=max(existing_ids, default=0) + 1,
    )
    if len(placements) != len(units):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor placement count differs")
    mappings: list[SourceMapping] = []
    findings: list[ExportFinding] = []
    for unit, placement in zip(units, placements, strict=True):
        if placement.unit_id != unit.unit_id:
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor placement order differs")
        if placement.status == "exact":
            mappings.append(
                SourceMapping(
                    unit=unit,
                    status="exact",
                    bookmark_name=placement.bookmark_name,
                    bookmark_id=placement.bookmark_id,
                    paragraph_id=placement.paragraph_id,
                )
            )
            continue
        ambiguous = placement.status == "conflict"
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
                    "source text has multiple or overlapping DOCX ranges; no anchor was chosen"
                    if ambiguous
                    else (
                        "source text has no uniquely provable editable DOCX range; "
                        "no anchor was chosen"
                    )
                ),
                recoverable=True,
                fingerprint=sha256_canonical(
                    {
                        "unit_id": unit.unit_id,
                        "normalized_text_sha256": unit.normalized_text_sha256,
                    }
                ),
                unit_id=unit.unit_id,
                source_location=unit.source_location(),
                remediation=(
                    "map this unit manually before applying returned revisions"
                    if placement.reason
                    not in {AnchorReason.NON_NORMALIZED_RANGE, AnchorReason.UNSAFE_STRUCTURE}
                    else "keep this unit manual because its Word range is not byte-exact plain text"
                ),
            )
        )
    document_xml = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    replacements = _tracked_package_replacements(package, document_xml)
    anchored_sha256 = _write_anchored_package(
        source_docx,
        destination_docx,
        replacements,
        expected_sha256=package.file_sha256,
    )
    return AnchoringResult(
        mappings=tuple(mappings),
        findings=tuple(findings),
        docx_sha256=anchored_sha256,
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


def _overlay_manifest_artifact_path(bindings: ExportBindings) -> str:
    review_path = PurePosixPath(bindings.artifact_path)
    manifest_path = (
        review_path.parent / f"{review_path.name}.image-overlay" / (IMAGE_OVERLAY_MANIFEST)
    )
    return validate_relative_path(manifest_path.as_posix())


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
    """Convert, normalize layout, anchor, inspect, then publish one review DOCX."""

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
    if not units:
        raise ContractError(
            ErrorCode.EXPORT_SILENT_LOSS,
            "no eligible plain-text review units were found in the LaTeX snapshot",
            details={
                "source_file_count": len(discovery.files),
                "remediation": (
                    "confirm the main document and supported text boundaries before exporting"
                ),
            },
        )
    overlay_destination = final_output.with_name(f"{final_output.name}.image-overlay")
    backend_capabilities = backend.capabilities()
    compatibility_profile = (
        TEX2WORD_COMPATIBILITY_PROFILE
        if (
            backend_capabilities.backend_id == "tex2word-public-api"
            and backend_capabilities.tool_name == "tex2word"
            and backend_capabilities.tool_version == SUPPORTED_TEX2WORD_VERSION
            and backend_capabilities.interface_version == TEX2WORD_INTERFACE_VERSION
        )
        else None
    )
    image_overlay = build_image_overlay(
        source_root,
        overlay_destination,
        discovery,
        compatibility_profile=compatibility_profile,
    )
    if not image_overlay.ready:
        raise ContractError(
            ErrorCode.BACKEND_CAPABILITY_MISSING,
            "one or more image occurrences require manual handling before export",
            details={
                "manifest_path": (f"{overlay_destination.name}/{IMAGE_OVERLAY_MANIFEST}"),
                "manifest_sha256": image_overlay.manifest_sha256,
                "diagnostic_ids": [item.diagnostic_id for item in image_overlay.diagnostics],
                "diagnostics": [item.as_dict() for item in image_overlay.diagnostics],
            },
        )
    image_overlay_binding = image_overlay.as_contract(
        manifest_artifact_path=_overlay_manifest_artifact_path(bindings),
        confidentiality=bindings.confidentiality,
    )
    source_image_instances = image_overlay.source_image_instances
    feature_inventory = scan_source_features(
        source_root,
        discovery,
        image_instances=source_image_instances,
        revision_view=request.revision_view,
        revision_aliases=request.revision_aliases,
    )
    source_metrics = {
        "paragraphs": len(units),
        **feature_inventory.as_metrics(),
    }
    review_payload = review_ir_payload(
        discovery,
        units,
        source_manifest_sha256=bindings.source_manifest_sha256,
    )
    review_ir_sha256 = sha256_canonical(review_payload)
    backend_stage = _pipeline_path(final_output, "backend")
    layout_stage = _pipeline_path(final_output, "layout")
    anchored_stage = _pipeline_path(final_output, "anchored")
    fields_stage = _pipeline_path(final_output, "fields")
    backend_result: BackendResult | None = None
    output_published = False
    try:
        backend_result = backend.export(
            BackendRequest(
                source_root=image_overlay.derived_root,
                main_document=request.main_document,
                output_path=backend_stage,
                expected_source_tree_sha256=image_overlay.discovery.source_tree_sha256,
                timeout_s=request.timeout_s,
                max_output_bytes=request.max_output_bytes,
                revision_view=request.revision_view,
                revision_aliases=request.revision_aliases,
            )
        )
        backend_result = replace(
            backend_result,
            native_report={
                **backend_result.native_report,
                "image_overlay": image_overlay.as_dict(),
            },
        )
        if not backend_result.succeeded:
            report = ExportReport(
                status="failed",
                source_manifest_sha256=bindings.source_manifest_sha256,
                backend_capabilities_sha256=backend_result.capabilities.payload_sha256,
                review_ir_sha256=None,
                source_map_sha256=None,
                review_docx=None,
                image_overlay=image_overlay_binding,
                source_metrics=source_metrics,
                output_metrics={},
                feature_results=(),
                findings=backend_result.findings,
                validation=ExportValidation("blocked", "not_run", "blocked", "blocked"),
            )
            return ExportOutcome(
                backend_result,
                report,
                units,
                None,
                None,
                None,
                image_overlay,
            )
        layout_report = apply_review_layout(backend_stage, layout_stage)
        field_report = finalize_word_fields(layout_stage, fields_stage)
        field_findings: tuple[ExportFinding, ...] = ()
        if field_report.unresolved_fields:
            field_findings = (
                ExportFinding(
                    code=ErrorCode.EXPORT_DEGRADED,
                    severity="warning",
                    phase="export",
                    message=(
                        "one or more source references have no exported Word bookmark; "
                        "explicit unresolved-reference placeholders were retained"
                    ),
                    recoverable=True,
                    fingerprint=sha256_canonical(
                        {"unresolved_fields": field_report.unresolved_fields}
                    ),
                    remediation="repair the missing LaTeX label or review the marked reference",
                ),
            )
        backend_result = replace(
            backend_result,
            findings=backend_result.findings + field_findings,
            native_report={
                **backend_result.native_report,
                "review_layout": asdict(layout_report),
                "word_field_finalization": asdict(field_report),
            },
        )
        anchoring = replace(
            anchor_source_units(fields_stage, anchored_stage, units),
            image_overlay=image_overlay_binding,
        )
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
                image_overlay=image_overlay_binding,
                source_metrics=source_metrics,
                output_metrics=inspection.as_metrics(),
                feature_results=(),
                findings=findings,
                validation=inspection.validation,
            )
            return ExportOutcome(
                backend_result,
                report,
                units,
                anchoring,
                inspection,
                None,
                image_overlay,
            )
        feature_reconciliation = reconcile_source_features(
            feature_inventory,
            inspection,
            backend_result,
        )
        findings += feature_reconciliation.findings
        if feature_reconciliation.failed:
            report = ExportReport(
                status="failed",
                source_manifest_sha256=bindings.source_manifest_sha256,
                backend_capabilities_sha256=backend_result.capabilities.payload_sha256,
                review_ir_sha256=None,
                source_map_sha256=None,
                review_docx=None,
                image_overlay=image_overlay_binding,
                source_metrics=source_metrics,
                output_metrics=inspection.as_metrics(),
                feature_results=feature_reconciliation.feature_results,
                findings=findings,
                validation=inspection.validation,
            )
            return ExportOutcome(
                backend_result,
                report,
                units,
                anchoring,
                inspection,
                None,
                image_overlay,
            )
        artifact = ReviewDocxArtifact.from_file(
            anchored_stage,
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
            source_map_sha256=None,
            review_docx=artifact,
            image_overlay=image_overlay_binding,
            source_metrics=source_metrics,
            output_metrics=inspection.as_metrics(),
            feature_results=(
                ExportFeatureResult(
                    feature="body_text",
                    status=feature_status,
                    source_count=len(units),
                    output_count=exact,
                    diagnostic_ids=feature_ids,
                ),
                *feature_reconciliation.feature_results,
            ),
            findings=findings,
            validation=inspection.validation,
        )
        source_map_payload = anchoring.source_map_payload(
            source_manifest_sha256=bindings.source_manifest_sha256,
            review_ir_sha256=review_ir_sha256,
            report_commitment=export_report_commitment(report.as_payload()),
        )
        source_map_sha256 = sha256_canonical(source_map_payload)
        report = replace(report, source_map_sha256=source_map_sha256)
        outcome = ExportOutcome(
            backend_result,
            report,
            units,
            anchoring,
            inspection,
            final_output,
            image_overlay,
        )
        _cleanup_pipeline_path(backend_stage, final_output)
        _cleanup_pipeline_path(layout_stage, final_output)
        _cleanup_pipeline_path(fields_stage, final_output)
        try:
            os.link(anchored_stage, final_output, follow_symlinks=False)
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
        output_published = True
        with suppress(OSError):
            anchored_stage.unlink()
        return outcome
    finally:
        if not output_published:
            _cleanup_pipeline_path(backend_stage, final_output)
            _cleanup_pipeline_path(anchored_stage, final_output)
            _cleanup_pipeline_path(layout_stage, final_output)
            _cleanup_pipeline_path(fields_stage, final_output)
            discard_image_overlay(image_overlay)


__all__ = [
    "ANCHOR_PROFILE_VERSION",
    "AnchoringResult",
    "ExportBindings",
    "ExportOutcome",
    "SourceMapping",
    "anchor_source_units",
    "export_review_docx",
]
