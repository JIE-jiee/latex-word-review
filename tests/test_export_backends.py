"""Backend execution, cleanup, E0 contract, and pipeline tests."""

from __future__ import annotations

import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

import pytest

import latex_word_review.backends.pandoc as pandoc_module
import latex_word_review.backends.tex2word as tex2word_module
from latex_word_review.backends import BackendRequest, PandocBackend, Tex2WordBackend
from latex_word_review.backends.base import BackendCapabilities, BackendResult
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import ExportBindings, export_review_docx
from latex_word_review.hashing import digest_file
from latex_word_review.inspection import inspect_docx
from latex_word_review.runtime import CommandResult
from tests._docx_factory import write_docx
from tests.test_image_materializer import _minimal_pdf, _require_pdf_runtime

FIXTURE_ROOT = Path(__file__).parent / "fixtures/e0-minimal-paper"


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nPlain text.\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def test_tex2word_real_e0_contract_on_a_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT / "source", source)
    output = tmp_path / "output" / "review.docx"

    result = Tex2WordBackend().export(BackendRequest(source, "main.tex", output))
    inspection = inspect_docx(output)

    assert result.succeeded
    assert result.capabilities.tool_version == "1.0.5"
    assert result.native_report["math_omml"] == 5
    assert (
        inspection.paragraphs,
        inspection.omml_objects,
        inspection.images,
        inspection.tables,
        inspection.bookmarks,
    ) == (34, 5, 1, 1, 9)
    assert (inspection.seq_fields, inspection.ref_fields, inspection.pageref_fields) == (5, 6, 0)
    assert not list(output.parent.glob(".review.docx.tex2word-*.docx"))


def test_real_tex2word_embeds_selected_pdf_page_as_related_png(
    tmp_path: Path,
) -> None:
    _require_pdf_runtime()
    backend = Tex2WordBackend()
    if backend.capabilities().tool_version != "1.0.5":
        pytest.skip("locked tex2word runtime is unavailable")
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    pdf_path = source / "figures/multi.pdf"
    pdf_path.write_bytes(_minimal_pdf(((1, 0, 0), (0, 0, 1)), width=72, height=36))
    pdf_before = pdf_path.read_bytes()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n\n"
        "Review the selected second page.\n\n"
        "\\includegraphics[page=2]{figures/multi.pdf}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output/review.docx"

    outcome = export_review_docx(
        backend,
        BackendRequest(source, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "d" * 64,
            artifact_path="artifacts/review.docx",
            confidentiality="public_fixture",
        ),
    )

    assert outcome.report.status == "success"
    assert outcome.inspection is not None
    assert outcome.inspection.image_instances == 1
    assert pdf_path.read_bytes() == pdf_before
    assert outcome.image_overlay is not None
    overlay_pngs = tuple(outcome.image_overlay.derived_root.rglob("preview.png"))
    assert len(overlay_pngs) == 1
    assert overlay_pngs[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    with zipfile.ZipFile(output) as package:
        members = set(package.namelist())
        media_pngs = sorted(
            name for name in members if name.startswith("word/media/") and name.endswith(".png")
        )
        assert len(media_pngs) == 1
        assert package.read(media_pngs[0]).startswith(b"\x89PNG\r\n\x1a\n")
        relationships = ET.fromstring(package.read("word/_rels/document.xml.rels"))
        image_targets = {
            relationship.attrib["Target"]
            for relationship in relationships
            if relationship.attrib.get("Type", "").endswith("/image")
        }
        assert image_targets
        assert {(PurePosixPath("word") / target).as_posix() for target in image_targets} <= members


def test_tex2word_worker_timeout_preserves_output_and_cleans_owned_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out" / "review.docx"
    output.parent.mkdir()
    output.write_bytes(b"existing")
    observed: dict[str, object] = {}

    def fake_run_command(
        executable: str | Path,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_s: float,
        max_output_bytes: int,
        environment: dict[str, str],
    ) -> CommandResult:
        del executable, max_output_bytes, environment
        observed.update(cwd=cwd, timeout_s=timeout_s, arguments=arguments)
        stage = Path(arguments[arguments.index("--output") + 1])
        stage.write_bytes(b"partial")
        return CommandResult(-9, "", "", True, False, 4, "sha256:" + "c" * 64)

    monkeypatch.setattr(tex2word_module, "run_command", fake_run_command)
    result = Tex2WordBackend().export(BackendRequest(source, "main.tex", output, timeout_s=0.05))

    assert not result.succeeded
    assert result.timed_out
    assert output.read_bytes() == b"existing"
    assert observed["cwd"] == source.resolve()
    assert observed["timeout_s"] == 0.05
    arguments = observed["arguments"]
    assert isinstance(arguments, tuple)
    assert arguments[:2] == ("-m", "latex_word_review.backends._tex2word_worker")
    assert not list(output.parent.glob(".review.docx.tex2word-*.docx"))
    assert not list(output.parent.glob(".review.docx.tex2word-report-*.json"))


def test_tex2word_success_never_overwrites_an_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out" / "review.docx"
    output.parent.mkdir()
    output.write_bytes(b"existing")

    result = Tex2WordBackend().export(BackendRequest(source, "main.tex", output))

    assert not result.succeeded
    assert output.read_bytes() == b"existing"
    assert not list(output.parent.glob(".review.docx.tex2word-*.docx"))


def test_full_e0_pipeline_adds_only_exact_source_bookmarks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT / "source", source)
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output" / "review.docx"

    outcome = export_review_docx(
        Tex2WordBackend(),
        BackendRequest(source, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "a" * 64,
            artifact_path="artifacts/review.docx",
            confidentiality="public_fixture",
        ),
    )

    assert outcome.report.status == "success"
    assert outcome.anchoring is not None
    assert outcome.anchoring.coverage == {
        "total": 2,
        "exact": 2,
        "degraded": 0,
        "unmapped": 0,
        "conflict": 0,
    }
    assert outcome.inspection is not None
    assert outcome.inspection.bookmarks == 11
    assert output.is_file()


def test_export_refuses_a_successful_zero_unit_review(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\[x^2\\]\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output" / "review.docx"

    with pytest.raises(ContractError) as raised:
        export_review_docx(
            Tex2WordBackend(),
            BackendRequest(source, "main.tex", output),
            discovery,
            ExportBindings(
                source_manifest_sha256="sha256:" + "a" * 64,
                artifact_path="artifacts/review.docx",
            ),
        )

    assert raised.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert not output.exists()


def test_export_blocks_when_a_source_image_instance_is_missing_from_docx(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\nPlain text.\n\n"
        "\\includegraphics{plot.png}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    (source / "plot.png").write_bytes(b"synthetic-static-image")
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output" / "review.docx"
    document_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Plain text.</w:t></w:r></w:p></w:body>
    </w:document>"""

    class MissingImageBackend:
        def capabilities(self) -> BackendCapabilities:
            return Tex2WordBackend().capabilities()

        def export(self, request: BackendRequest) -> BackendResult:
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
        MissingImageBackend(),
        BackendRequest(source, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "a" * 64,
            artifact_path="artifacts/review.docx",
        ),
    )

    assert outcome.output_path is None
    assert outcome.report.status == "failed"
    image_feature = next(
        feature for feature in outcome.report.feature_results if feature.feature == "images"
    )
    assert (image_feature.source_count, image_feature.output_count, image_feature.status) == (
        1,
        0,
        "failed",
    )
    assert not output.exists()
    assert not output.with_name(f"{output.name}.image-overlay").exists()

    repeated = export_review_docx(
        MissingImageBackend(),
        BackendRequest(source, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "a" * 64,
            artifact_path="artifacts/review.docx",
        ),
    )
    assert repeated.report.status == "failed"
    assert not output.with_name(f"{output.name}.image-overlay").exists()


def test_pandoc_forces_source_root_cwd_and_fixed_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out" / "review.docx"
    observed: dict[str, object] = {}

    def fake_run_command(
        executable: str | Path,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_s: float,
        max_output_bytes: int,
        environment: dict[str, str],
    ) -> CommandResult:
        del executable, timeout_s, max_output_bytes, environment
        observed["cwd"] = cwd
        observed["arguments"] = arguments
        stage = Path(
            next(item.split("=", 1)[1] for item in arguments if item.startswith("--output="))
        )
        shutil.copyfile(FIXTURE_ROOT / "base/review-base.docx", stage)
        return CommandResult(0, "", "", False, False, 1, "sha256:" + "b" * 64)

    monkeypatch.setattr(pandoc_module, "run_command", fake_run_command)
    result = PandocBackend(version_override="test").export(
        BackendRequest(source, "main.tex", output)
    )

    assert result.succeeded
    assert observed["cwd"] == source.resolve()
    arguments = observed["arguments"]
    assert isinstance(arguments, tuple)
    assert arguments[0] == "main.tex"
    assert "--resource-path=." in arguments


def test_pandoc_timeout_preserves_existing_output_and_cleans_stage(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out" / "review.docx"
    output.parent.mkdir()
    output.write_bytes(b"existing")
    backend = PandocBackend(
        sys.executable,
        executable_arguments=("-c", "import time; time.sleep(2)"),
        version_override="test",
    )

    result = backend.export(BackendRequest(source, "main.tex", output, timeout_s=0.05))

    assert not result.succeeded
    assert result.timed_out
    assert output.read_bytes() == b"existing"
    assert not list(output.parent.glob(".review.docx.pandoc-*.docx"))


def test_pandoc_partial_artifact_is_removed_on_nonzero_exit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out" / "review.docx"
    output.parent.mkdir()
    output.write_bytes(b"existing")
    code = (
        "import pathlib,sys;"
        "p=next(x.split('=',1)[1] for x in sys.argv[1:] if x.startswith('--output='));"
        "pathlib.Path(p).write_bytes(b'partial');"
        "raise SystemExit(9)"
    )
    backend = PandocBackend(
        sys.executable,
        executable_arguments=("-c", code),
        version_override="test",
    )

    result = backend.export(BackendRequest(source, "main.tex", output, timeout_s=5))

    assert not result.succeeded
    assert result.returncode == 9
    assert output.read_bytes() == b"existing"
    assert not list(output.parent.glob(".review.docx.pandoc-*.docx"))


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc executable not available")
def test_pandoc_real_e0_contract_when_available(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT / "source", source)
    output = tmp_path / "out" / "pandoc.docx"

    result = PandocBackend().export(BackendRequest(source, "main.tex", output))

    assert result.succeeded
    assert inspect_docx(output).package_valid
