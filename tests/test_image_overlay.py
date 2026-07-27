"""Public synthetic contracts for the derived PDF-image overlay."""

from __future__ import annotations

import importlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import latex_word_review.image_overlay as overlay_module
from latex_word_review.backends import BackendRequest, Tex2WordBackend
from latex_word_review.backends.base import BackendCapabilities, BackendResult
from latex_word_review.canonical import canonical_json
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import ExportBindings, ExportOutcome, export_review_docx
from latex_word_review.hashing import digest_bytes, digest_file, read_stable_bytes
from latex_word_review.image_materializer import GraphicOption, scan_graphics_text
from latex_word_review.image_overlay import (
    IMAGE_OVERLAY_MANIFEST,
    IMAGE_OVERLAY_ROOT,
    TEX2WORD_COMPATIBILITY_PROFILE,
    ImageOverlayDiagnostic,
    build_image_overlay,
)
from latex_word_review.inspection import inspect_docx
from latex_word_review.snapshot import snapshot_project
from tests._docx_factory import write_docx
from tests.test_image_materializer import _minimal_pdf, _require_pdf_runtime

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _write_png(path: Path) -> None:
    image_module = importlib.import_module("PIL.Image")
    output = io.BytesIO()
    image = image_module.new("RGB", (4, 3), (0, 180, 0))
    try:
        image.save(output, format="PNG", compress_level=9, optimize=False)
    finally:
        image.close()
    path.write_bytes(output.getvalue())


def _write_complex_source(root: Path) -> None:
    (root / "figures").mkdir(parents=True)
    (root / "figures/multi.pdf").write_bytes(
        _minimal_pdf(((1, 0, 0), (0, 0, 1)), width=72, height=36)
    )
    _write_png(root / "figures/raster.png")
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\graphicspath{{figures/}}\n"
        "\\begin{document}\n\n"
        "Public synthetic review text.\n\n"
        "\\includegraphics[page=2,trim=6bp 0 6bp 0,clip,angle=90,"
        "width=.72\\linewidth,keepaspectratio]{multi.pdf}\n"
        "\\includegraphics[page=1,scale=.5]{multi.pdf}\n"
        "\\includegraphics[height=2cm]{raster.png}\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _snapshot_bytes(snapshot: Path) -> dict[str, bytes]:
    return {
        path.relative_to(snapshot).as_posix(): path.read_bytes()
        for path in sorted(snapshot.rglob("*"))
        if path.is_file()
    }


def test_public_multi_page_pdf_overlay_is_auditable_and_snapshot_stays_immutable(
    tmp_path: Path,
) -> None:
    _require_pdf_runtime()
    source = tmp_path / "source"
    source.mkdir()
    _write_complex_source(source)
    snapshot = tmp_path / "snapshot"
    snapshot_project(source, snapshot, main_document="main.tex")
    discovery = discover_project(snapshot, main_document="main.tex")
    original_bytes = _snapshot_bytes(snapshot)

    overlay = build_image_overlay(snapshot, tmp_path / "derived-overlay", discovery)

    assert overlay.ready
    assert (
        overlay.source_image_instances,
        overlay.materialized_pdf_instances,
        overlay.passthrough_raster_instances,
    ) == (3, 2, 1)
    assert _snapshot_bytes(snapshot) == original_bytes
    assert overlay.discovery.source_tree_sha256 != discovery.source_tree_sha256

    derived_tex = (overlay.derived_root / "main.tex").read_text(encoding="utf-8")
    assert "page=" not in derived_tex
    assert "trim=" not in derived_tex
    assert "angle=" not in derived_tex
    assert "width=.72\\linewidth,keepaspectratio" in derived_tex
    assert "scale=.5" in derived_tex
    assert derived_tex.count("\\includegraphics[") == 3
    assert derived_tex.count(f"{{{IMAGE_OVERLAY_ROOT}/") == 2
    assert "\\includegraphics[height=2cm]{figures/raster.png}" in derived_tex
    assert "\\includegraphics[height=2cm]{raster.png}" not in derived_tex
    assert "page=2,trim=6bp 0 6bp 0,clip,angle=90" in original_bytes["main.tex"].decode("utf-8")

    manifest_bytes = read_stable_bytes(overlay.manifest_path, max_bytes=1024 * 1024)
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes == canonical_json(manifest) + b"\n"
    assert digest_bytes(manifest_bytes).sha256 == overlay.manifest_sha256
    assert manifest["status"] == "ready"
    assert manifest["source"]["source_tree_sha256"] == discovery.source_tree_sha256
    assert manifest["derived"]["source_tree_sha256"] == overlay.discovery.source_tree_sha256
    entries = manifest["entries"]
    assert [entry["status"] for entry in entries] == [
        "materialized_pdf",
        "materialized_pdf",
        "passthrough_raster",
    ]
    assert entries[2]["original_target"] == "raster.png"
    assert entries[2]["resolved_source"]["path"] == "figures/raster.png"
    assert entries[2]["derived_command"] == ("\\includegraphics[height=2cm]{figures/raster.png}")
    assert entries[2]["derived_command_sha256"].startswith("sha256:")
    assert [entry["request"]["page"] for entry in entries[:2]] == [2, 1]
    assert entries[0]["request"]["angle_millidegrees"] == 90_000
    assert entries[0]["request"]["trim_micro_bp"] == [6_000_000, 0, 6_000_000, 0]
    assert [item["key"] for item in entries[0]["passthrough_options"]] == [
        "width",
        "keepaspectratio",
    ]
    for entry in entries[:2]:
        assert entry["source_span"]["end_utf8"] > entry["source_span"]["start_utf8"]
        assert entry["original_command_sha256"].startswith("sha256:")
        assert entry["request_sha256"].startswith("sha256:")
        assert entry["materialized"]["cache_key_sha256"].startswith("sha256:")
        assert entry["materialized"]["png_sha256"].startswith("sha256:")
        assert entry["materialized"]["pixel_sha256"].startswith("sha256:")
        assert (overlay.derived_root / entry["materialized"]["png_path"]).is_file()
    assert entries[0]["materialized"]["pixel_sha256"] != entries[1]["materialized"]["pixel_sha256"]


def test_tex2word_profile_rewrites_only_proven_direct_figure_minipages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    _write_png(source / "figures/column.png")
    (source / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\graphicspath{{figures/}}\n"
        "\\begin{document}\n\n"
        "Public compatibility review text.\n\n"
        "\\begin{figure}\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{column.png}\n\\caption{Left eligible}\n"
        "\\end{minipage}\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{column.png}\n\\caption{Right eligible}\n"
        "\\end{minipage}\n"
        "\\end{figure}\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{column.png}\n\\caption{Outside figure}\n"
        "\\end{minipage}\n"
        "\\begin{figure}\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{column.png}\nNo caption here.\n"
        "\\end{minipage}\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{column.png}\n\\caption{Nested construct}\n"
        "\\subfloat{Already a subfigure construct}\n"
        "\\end{minipage}\n"
        "\\begin{center}\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{column.png}\n\\caption{Not a direct child}\n"
        "\\end{minipage}\n"
        "\\end{center}\n"
        "\\end{figure}\n"
        "% \\begin{minipage} commented and ignored\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    before = _snapshot_bytes(source)

    overlay = build_image_overlay(
        source,
        tmp_path / "tex2word-overlay",
        discovery,
        compatibility_profile=TEX2WORD_COMPATIBILITY_PROFILE,
    )

    assert overlay.ready
    assert overlay.compatibility_profile == TEX2WORD_COMPATIBILITY_PROFILE
    assert overlay.compatibility_transformations == 1
    assert overlay.source_image_instances == 6
    assert overlay.passthrough_raster_instances == 6
    assert _snapshot_bytes(source) == before
    derived = (overlay.derived_root / "main.tex").read_text(encoding="utf-8")
    masked = overlay_module._mask_tex_comments(derived)
    assert "\\begin{subfigure}" not in derived
    assert masked.count("\\begin{figure}") == 3
    assert masked.count("\\begin{minipage}") == 6
    assert "\\includegraphics{figures/column.png}" in derived
    assert "\\includegraphics{column.png}" not in derived
    assert "\\caption{Left eligible}" in derived
    assert "\\caption{Outside figure}" in derived

    manifest = json.loads(overlay.manifest_path.read_text(encoding="utf-8"))
    compatibility = manifest["compatibility"]
    assert compatibility["profile"] == TEX2WORD_COMPATIBILITY_PROFILE
    assert compatibility["transformation_count"] == 1
    transformations = compatibility["transformations"]
    assert len({item["transformation_id"] for item in transformations}) == 1
    item = transformations[0]
    assert item["kind"] == "figure_minipage_split"
    assert item["source_path"] == "main.tex"
    assert item["original_environment"] == "figure"
    assert item["derived_environment"] == "figure_sequence"
    assert item["direct_child_count"] == 2
    assert item["includegraphics_count"] == 2
    assert item["caption_count"] == 2
    assert item["label_count"] == 0
    assert item["labels"] == []
    assert item["nested_subfigure_constructs"] == 0
    assert item["original_block_sha256"].startswith("sha256:")
    assert item["derived_block_sha256"].startswith("sha256:")


def test_export_backend_receives_only_the_derived_root_and_source_map_uses_snapshot(
    tmp_path: Path,
) -> None:
    _require_pdf_runtime()
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    (source / "figures/page.pdf").write_bytes(_minimal_pdf(((1, 0, 0),)))
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n\n"
        "Original review text.\n\n\\begin{figure}\n"
        "\\begin{minipage}{.8\\linewidth}\n"
        "\\includegraphics{figures/page.pdf}\n\\caption{Public page}\n"
        "\\end{minipage}\n\\end{figure}\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    snapshot = tmp_path / "snapshot"
    snapshot_project(source, snapshot, main_document="main.tex")
    discovery = discover_project(snapshot, main_document="main.tex")
    before = _snapshot_bytes(snapshot)
    output = tmp_path / "output/review.docx"
    observed: dict[str, object] = {}
    document_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}" xmlns:a="{A_NS}"><w:body>
  <w:p><w:r><w:t>Original review text.</w:t></w:r></w:p>
  <w:p><w:r><w:drawing><a:blip/></w:drawing></w:r></w:p>
</w:body></w:document>""".encode()

    class CapturingBackend:
        def capabilities(self) -> BackendCapabilities:
            return replace(Tex2WordBackend().capabilities(), tool_version="1.0.5")

        def export(self, request: BackendRequest) -> BackendResult:
            observed["source_root"] = request.source_root
            observed["main_text"] = (request.source_root / request.main_document).read_text(
                encoding="utf-8"
            )
            observed["expected_source_tree_sha256"] = request.expected_source_tree_sha256
            derived = discover_project(request.source_root, main_document=request.main_document)
            observed["actual_source_tree_sha256"] = derived.source_tree_sha256
            write_docx(request.output_path, document_xml=document_xml)
            digest = digest_file(request.output_path, max_bytes=128 * 1024 * 1024)
            return BackendResult(
                status="success",
                capabilities=self.capabilities(),
                artifact_name=request.output_path.name,
                artifact_sha256=digest.sha256,
                artifact_size_bytes=digest.size_bytes,
                findings=(),
                native_report={},
            )

    outcome = export_review_docx(
        CapturingBackend(),
        BackendRequest(snapshot, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "a" * 64,
            artifact_path="artifacts/review.docx",
            confidentiality="public_fixture",
        ),
    )

    assert outcome.output_path == output
    assert outcome.image_overlay is not None
    observed_root = cast(Path, observed["source_root"])
    assert observed_root == outcome.image_overlay.derived_root
    assert observed_root.resolve() != snapshot.resolve()
    assert observed["expected_source_tree_sha256"] == observed["actual_source_tree_sha256"]
    assert discovery.source_tree_sha256 == outcome.image_overlay.original_source_tree_sha256
    assert f"{{{IMAGE_OVERLAY_ROOT}/" in str(observed["main_text"])
    assert "\\begin{subfigure}" not in str(observed["main_text"])
    assert "\\begin{minipage}" in str(observed["main_text"])
    assert outcome.image_overlay.compatibility_profile == TEX2WORD_COMPATIBILITY_PROFILE
    assert outcome.image_overlay.compatibility_transformations == 0
    assert _snapshot_bytes(snapshot) == before
    assert outcome.anchoring is not None
    mapping = outcome.anchoring.mappings[0]
    assert mapping.unit.path == "main.tex"
    original_main = before["main.tex"]
    assert (
        digest_bytes(original_main[mapping.unit.start_byte : mapping.unit.end_byte]).sha256
        == mapping.unit.slice_sha256
    )
    overlay_record = cast(
        dict[str, object],
        outcome.backend_result.native_report["image_overlay"],
    )
    assert overlay_record["manifest_sha256"] == outcome.image_overlay.manifest_sha256
    binding = cast(dict[str, object], outcome.report.as_payload()["image_overlay"])
    manifest_ref = cast(dict[str, object], binding["manifest"])
    assert manifest_ref["path"] == (
        "artifacts/review.docx.image-overlay/image-overlay-manifest.json"
    )
    assert manifest_ref["role"] == "image_overlay_manifest"
    assert manifest_ref["sha256"] == outcome.image_overlay.manifest_sha256
    assert manifest_ref["size_bytes"] == outcome.image_overlay.manifest_size_bytes
    assert binding["original_source_tree_sha256"] == discovery.source_tree_sha256
    assert binding["derived_source_tree_sha256"] == observed["actual_source_tree_sha256"]
    assert outcome.report.review_ir_sha256 is not None
    source_map_payload = outcome.anchoring.source_map_payload(
        source_manifest_sha256="sha256:" + "a" * 64,
        review_ir_sha256=outcome.report.review_ir_sha256,
    )
    assert source_map_payload["image_overlay"] == binding


def test_unsupported_image_publishes_manual_manifest_and_never_calls_backend(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "figure.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
        encoding="utf-8",
    )
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\nReview text.\n\n"
        "\\includegraphics{figure.svg}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    original = _snapshot_bytes(source)
    output = tmp_path / "output/review.docx"

    class NeverBackend:
        def capabilities(self) -> BackendCapabilities:
            return Tex2WordBackend().capabilities()

        def export(self, request: BackendRequest) -> BackendResult:
            del request
            raise AssertionError("backend must not run for a manual image overlay")

    with pytest.raises(ContractError) as raised:
        export_review_docx(
            NeverBackend(),
            BackendRequest(source, "main.tex", output),
            discovery,
            ExportBindings(
                source_manifest_sha256="sha256:" + "b" * 64,
                artifact_path="artifacts/review.docx",
            ),
        )

    assert raised.value.code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert not output.exists()
    assert _snapshot_bytes(source) == original
    overlay_root = output.with_name(f"{output.name}.image-overlay")
    manifest_path = overlay_root / IMAGE_OVERLAY_MANIFEST
    manifest = json.loads(read_stable_bytes(manifest_path, max_bytes=1024 * 1024))
    assert manifest["status"] == "manual_required"
    assert manifest["entries"][0]["status"] == "manual_required"
    assert manifest["entries"][0]["derived_command"] is None
    assert manifest["diagnostics"]


@pytest.mark.parametrize("failure_mode", ["backend", "inspection"])
def test_ready_overlay_is_cleaned_after_downstream_failure_and_retryable(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_png(source / "figure.png")
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\nReview text.\n\n"
        "\\includegraphics{figure.png}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output/review.docx"

    class DownstreamFailureBackend:
        def capabilities(self) -> BackendCapabilities:
            return Tex2WordBackend().capabilities()

        def export(self, request: BackendRequest) -> BackendResult:
            if failure_mode == "backend":
                return BackendResult(
                    status="failed",
                    capabilities=self.capabilities(),
                    artifact_name=None,
                    artifact_sha256=None,
                    artifact_size_bytes=None,
                    findings=(),
                    native_report={},
                )
            request.output_path.write_bytes(b"not a DOCX")
            digest = digest_file(request.output_path, max_bytes=1024)
            return BackendResult(
                status="success",
                capabilities=self.capabilities(),
                artifact_name=request.output_path.name,
                artifact_sha256=digest.sha256,
                artifact_size_bytes=digest.size_bytes,
                findings=(),
                native_report={},
            )

    for _attempt in range(2):

        def operation() -> ExportOutcome:
            return export_review_docx(
                DownstreamFailureBackend(),
                BackendRequest(source, "main.tex", output),
                discovery,
                ExportBindings(
                    source_manifest_sha256="sha256:" + "c" * 64,
                    artifact_path="artifacts/review.docx",
                ),
            )

        if failure_mode == "backend":
            assert operation().report.status == "failed"
        else:
            with pytest.raises(ContractError):
                operation()
        assert not output.exists()
        assert not output.with_name(f"{output.name}.image-overlay").exists()


def test_overlay_value_objects_and_rewrite_guards_are_fail_closed() -> None:
    with pytest.raises(ContractError) as raised:
        ImageOverlayDiagnostic("", "message", "main.tex", 1, 1)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(ContractError) as raised:
        ImageOverlayDiagnostic("CODE", "", "main.tex", 1, 1)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(ContractError) as raised:
        ImageOverlayDiagnostic("CODE", "message", "main.tex", 0, 1)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID

    diagnostic = ImageOverlayDiagnostic("CODE", "message", "main.tex", 1, 2)
    assert diagnostic.diagnostic_id.startswith("image_diag_")
    assert diagnostic.as_dict()["disposition"] == "manual"
    assert not overlay_module._has_unescaped_comment("no comment")
    assert not overlay_module._has_unescaped_comment(r"escaped \% marker")
    assert overlay_module._has_unescaped_comment(r"comment \\% marker")

    text = r"\includegraphics{x.png}"
    occurrence = scan_graphics_text(text, source_path="main.tex").includes[0]
    assert (
        overlay_module._derived_command(
            (GraphicOption("width", "1cm", 0),),
            "lwr-images/value/preview.png",
        )
        == r"\includegraphics[width=1cm]{lwr-images/value/preview.png}"
    )
    with pytest.raises(ContractError):
        overlay_module._derived_command((), "../preview.png")
    with pytest.raises(ContractError) as raised:
        overlay_module._rewrite_text("X" + text[1:], [(occurrence, "replacement")])
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH
    with pytest.raises(ContractError) as raised:
        overlay_module._rewrite_text(
            text,
            [(occurrence, "first"), (occurrence, "second")],
        )
    assert raised.value.code is ErrorCode.INTERNAL_INVARIANT
    outside = replace(
        occurrence,
        start_char=len(text) + 1,
        end_char=len(text) + 2,
    )
    with pytest.raises(ContractError) as raised:
        overlay_module._byte_offsets(text, (outside,))
    assert raised.value.code is ErrorCode.INTERNAL_INVARIANT


def test_overlay_rejects_existing_stale_and_case_colliding_reserved_paths(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n",
        encoding="utf-8",
    )
    clean_discovery = discover_project(clean, main_document="main.tex")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ContractError) as raised:
        build_image_overlay(clean, existing, clean_discovery)
    assert raised.value.code is ErrorCode.BACKEND_FAILED

    (clean / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nChanged.\n\\end{document}\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as raised:
        build_image_overlay(clean, tmp_path / "stale", clean_discovery)
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH

    reserved = tmp_path / "reserved"
    (reserved / "LWR-IMAGES").mkdir(parents=True)
    _write_png(reserved / "LWR-IMAGES/asset.png")
    (reserved / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nText.\n"
        "\\includegraphics{LWR-IMAGES/asset.png}\n\\end{document}\n",
        encoding="utf-8",
    )
    reserved_discovery = discover_project(reserved, main_document="main.tex")
    destination = tmp_path / "reserved-overlay"
    with pytest.raises(ContractError) as raised:
        build_image_overlay(reserved, destination, reserved_discovery)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not destination.exists()
    assert not list(tmp_path.glob(".lwr-img-*"))


def test_overlay_records_parse_lookup_option_and_render_failures_as_manual(
    tmp_path: Path,
) -> None:
    _require_pdf_runtime()
    cases = (
        (
            "malformed",
            r"\includegraphics[width={bad]{missing.png}",
            None,
            "IMAGE_PARSE_MALFORMED",
        ),
        (
            "missing",
            r"\includegraphics{missing.png}",
            None,
            ErrorCode.BACKEND_FAILED.value,
        ),
        (
            "dynamic",
            r"\includegraphics[width=\dynamic]{figure.png}",
            "png",
            "IMAGE_DYNAMIC_INPUT",
        ),
        (
            "raster-page",
            r"\includegraphics[page=1]{figure.png}",
            "png",
            "IMAGE_UNSUPPORTED_OPTION",
        ),
        (
            "comment",
            "\\includegraphics% comment\n{figure.png}",
            "png",
            "IMAGE_PARSE_MALFORMED",
        ),
        (
            "corrupt-pdf",
            r"\includegraphics{figure.pdf}",
            "pdf",
            ErrorCode.BACKEND_FAILED.value,
        ),
    )
    for name, command, asset_kind, expected_code in cases:
        source = tmp_path / name
        source.mkdir()
        if asset_kind == "png":
            _write_png(source / "figure.png")
        elif asset_kind == "pdf":
            (source / "figure.pdf").write_bytes(b"not a PDF")
        (source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nText.\n"
            f"{command}\n\\end{{document}}\n",
            encoding="utf-8",
            newline="\n",
        )
        discovery = discover_project(source, main_document="main.tex")

        result = build_image_overlay(source, tmp_path / f"{name}-overlay", discovery)

        assert not result.ready
        assert expected_code in {item.code for item in result.diagnostics}


@pytest.mark.parametrize("failure", [FileExistsError("race"), OSError("failure")])
def test_overlay_publication_failure_cleans_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n",
        encoding="utf-8",
    )
    discovery = discover_project(source, main_document="main.tex")
    destination = tmp_path / "overlay"

    def fail_rename(self: Path, target: Path) -> Path:
        del self, target
        raise failure

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(ContractError) as raised:
        build_image_overlay(source, destination, discovery)

    expected = (
        ErrorCode.BACKEND_FAILED
        if isinstance(failure, FileExistsError)
        else ErrorCode.INTERNAL_INVARIANT
    )
    assert raised.value.code is expected
    assert not destination.exists()
    assert not list(tmp_path.glob(".lwr-img-*"))


def test_overlay_final_manifest_verification_failure_removes_published_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n",
        encoding="utf-8",
    )
    discovery = discover_project(source, main_document="main.tex")
    destination = tmp_path / "overlay"

    def tamper_manifest(path: Path, *, max_bytes: int) -> bytes:
        if path.name == IMAGE_OVERLAY_MANIFEST and path.is_relative_to(destination):
            return b"tampered"
        return read_stable_bytes(path, max_bytes=max_bytes)

    monkeypatch.setattr(overlay_module, "read_stable_bytes", tamper_manifest)
    with pytest.raises(ContractError) as raised:
        build_image_overlay(source, destination, discovery)

    assert raised.value.code is ErrorCode.HASH_INTEGRITY_MISMATCH
    assert not destination.exists()


def test_redundant_literal_group_is_rewritten_only_in_the_derived_overlay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    figure_name = "Review \N{EN DASH} plot.png"
    _write_png(source / "figures" / figure_name)
    original_command = r"\includegraphics[height=2cm]{{" + figure_name + "}}"
    (source / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\graphicspath{{figures/}}\n"
        "\\begin{document}\nText.\n"
        f"{original_command}\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    original = _snapshot_bytes(source)

    overlay = build_image_overlay(source, tmp_path / "overlay", discovery)

    assert overlay.ready
    assert overlay.source_image_instances == 1
    assert overlay.passthrough_raster_instances == 1
    assert _snapshot_bytes(source) == original
    derived_command = f"\\includegraphics[height=2cm]{{figures/{figure_name}}}"
    derived_text = (overlay.derived_root / "main.tex").read_text(encoding="utf-8")
    assert original_command in original["main.tex"].decode("utf-8")
    assert original_command not in derived_text
    assert derived_command in derived_text

    manifest = json.loads(overlay.manifest_path.read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert entry["original_target"] == f"{{{figure_name}}}"
    assert entry["original_command"] == original_command
    assert entry["resolved_source"]["path"] == f"figures/{figure_name}"
    assert entry["derived_command"] == derived_command
    assert entry["source_span"]["end_char"] > entry["source_span"]["start_char"]
    assert entry["source_span"]["end_utf8"] > entry["source_span"]["start_utf8"]


def test_tex2word_profile_preserves_split_figure_reference_targets(tmp_path: Path) -> None:
    backend = Tex2WordBackend()
    if backend.capabilities().tool_version != "1.0.5":
        pytest.skip("locked tex2word runtime is unavailable")
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    _write_png(source / "figures/panel.png")
    (source / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{cleveref}\n"
        "\\begin{document}\n"
        "See \\cref{fig:left} and \\cref{fig:right}.\n"
        "\\begin{figure}[htbp]\n"
        "\\centering\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{figures/panel.png}\n"
        "\\caption{Left panel}\\label{fig:left}\n"
        "\\end{minipage}\n"
        "\\hfill\n"
        "\\begin{minipage}{.48\\linewidth}\n"
        "\\includegraphics{figures/panel.png}\n"
        "\\caption{Right panel}\\label{fig:right}\n"
        "\\end{minipage}\n"
        "\\end{figure}\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    original = _snapshot_bytes(source)

    overlay = build_image_overlay(
        source,
        tmp_path / "overlay",
        discovery,
        compatibility_profile=TEX2WORD_COMPATIBILITY_PROFILE,
    )
    derived = (overlay.derived_root / "main.tex").read_text(encoding="utf-8")
    assert derived.count("\\begin{figure}[htbp]") == 2
    assert "\\label{fig:left}" in derived
    assert "\\label{fig:right}" in derived
    assert _snapshot_bytes(source) == original

    output = tmp_path / "output/review.docx"
    result = backend.export(BackendRequest(overlay.derived_root, "main.tex", output))
    assert result.succeeded
    inspection = inspect_docx(output)
    assert inspection.seq_fields == 2
    assert inspection.ref_fields == 2
    assert inspection.image_instances == 2

    with zipfile.ZipFile(output) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    codes = ["".join(node.itertext()) for node in root.iter(f"{{{W_NS}}}instrText")]
    targets = {
        match.group(1)
        for code in codes
        if (match := re.search(r"\b(?:REF|PAGEREF)\s+([A-Za-z_][A-Za-z0-9_.]*)", code))
    }
    assert len(targets) == 2
    assert targets.issubset(set(inspection.bookmark_names))


def test_locked_tex2word_preserves_subcaptionboxes_and_manual_minipage_images(
    tmp_path: Path,
) -> None:
    backend = Tex2WordBackend()
    if backend.capabilities().tool_version != "1.0.5":
        pytest.skip("locked tex2word runtime is unavailable")
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    _write_png(source / "figures/panel.png")

    def manual_child(label: str, caption: str) -> str:
        return (
            "\\begin{minipage}[t]{0.48\\textwidth}\n"
            "\\centering\n"
            "\\includegraphics[width=\\linewidth]{figures/panel.png}\n"
            "\\par\\vspace{4pt}\n"
            f"\\refstepcounter{{figure}}\\label{{{label}}}\n"
            f"{{\\small\\bfseries Fig.~\\thefigure:}} {{\\small {caption}}}\\par\n"
            "\\addcontentsline{lof}{figure}"
            f"{{\\protect\\numberline{{\\thefigure}}{caption}}}\n"
            "\\end{minipage}\n"
        )

    subcaptionboxes = "".join(
        f"\\subcaptionbox{{Panel {index}\\label{{fig:sub{index}}}}}"
        "{\\includegraphics[width=.2\\linewidth]{figures/panel.png}}\n"
        for index in range(1, 5)
    )
    source_text = (
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{subcaption}\n"
        "\\begin{document}\n"
        "\\begin{figure}\n\\centering\n"
        + subcaptionboxes
        + "\\caption{Four public panels}\\label{fig:panels}\n\\end{figure}\n"
        + "\\begin{figure}\n\\centering\n"
        + manual_child("fig:left-a", "Left A")
        + "\\hfill\n"
        + manual_child("fig:right-a", "Right A")
        + "\\end{figure}\n"
        + "\\begin{figure}\n\\centering\n"
        + manual_child("fig:left-b", "Left B")
        + "\\hfill\n"
        + manual_child("fig:right-b", "Right B")
        + "\\end{figure}\n"
        + "\\end{document}\n"
    )
    (source / "main.tex").write_text(
        source_text,
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    original = _snapshot_bytes(source)

    overlay = build_image_overlay(
        source,
        tmp_path / "overlay",
        discovery,
        compatibility_profile=TEX2WORD_COMPATIBILITY_PROFILE,
    )

    assert overlay.ready
    assert overlay.source_image_instances == 8
    assert overlay.passthrough_raster_instances == 8
    assert overlay.compatibility_transformations == 6
    assert _snapshot_bytes(source) == original
    derived = (overlay.derived_root / "main.tex").read_text(encoding="utf-8")
    assert derived.count("\\begin{subfigure}") == 4
    assert derived.count("\\begin{figure}") == 5
    assert "\\subcaptionbox" not in derived
    assert "\\refstepcounter" not in derived
    assert "\\addcontentsline" not in derived
    assert derived.count("\\caption{") == 9
    manifest = json.loads(overlay.manifest_path.read_text(encoding="utf-8"))
    transformations = manifest["compatibility"]["transformations"]
    assert (
        sum(item["kind"] == "figure_subcaptionbox_normalization" for item in transformations) == 4
    )
    split = [item for item in transformations if item["kind"] == "figure_minipage_split"]
    assert len(split) == 2
    assert all(item["manual_caption_normalizations"] == 2 for item in split)
    assert all(item["source_caption_count"] == 0 for item in split)
    assert {label for item in split for label in item["labels"]} == {
        "fig:left-a",
        "fig:right-a",
        "fig:left-b",
        "fig:right-b",
    }

    output = tmp_path / "output/review.docx"
    result = backend.export(BackendRequest(overlay.derived_root, "main.tex", output))
    assert result.succeeded
    inspection = inspect_docx(output)
    assert inspection.image_instances == 8
    assert inspection.seq_fields == 5
