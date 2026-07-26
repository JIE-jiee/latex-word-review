"""Backend execution, cleanup, E0 contract, and pipeline tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

import latex_word_review.backends.pandoc as pandoc_module
import latex_word_review.backends.tex2word as tex2word_module
from latex_word_review.backends import BackendRequest, PandocBackend, Tex2WordBackend
from latex_word_review.backends.base import BackendCapabilities, BackendResult, prepare_export
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import ExportBindings, ExportOutcome, export_review_docx
from latex_word_review.export_models import ExportReport, ReviewDocxArtifact
from latex_word_review.hashing import digest_file
from latex_word_review.inspection import inspect_docx
from latex_word_review.review_reference import PROFILE_ID, REFERENCE_DOCX_SHA256
from latex_word_review.runtime import CommandResult
from tests._docx_factory import write_docx
from tests.test_image_materializer import _minimal_pdf, _require_pdf_runtime

FIXTURE_ROOT = Path(__file__).parent / "fixtures/e0-minimal-paper"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


def _style_by_name(root: ET.Element, name: str) -> ET.Element:
    for style in root.findall(f"{W}style"):
        name_node = style.find(f"{W}name")
        if name_node is not None and name_node.get(f"{W}val") == name:
            return style
    raise AssertionError(f"missing Word style: {name}")


def _required_child(root: ET.Element, path: str) -> ET.Element:
    child = root.find(path)
    assert child is not None
    return child


def _direct_run_style(root: ET.Element, needle: str) -> tuple[str | None, bool, bool]:
    matches: list[tuple[str | None, bool, bool]] = []
    for run in root.iter(f"{W}r"):
        text = "".join(node.text or "" for node in run.iter(f"{W}t"))
        if needle not in text:
            continue
        properties = run.find(f"{W}rPr")
        color = None if properties is None else properties.find(f"{W}color")
        matches.append(
            (
                None if color is None else color.get(f"{W}val"),
                properties is not None and properties.find(f"{W}strike") is not None,
                properties is not None and properties.find(f"{W}highlight") is not None,
            )
        )
    assert len(matches) == 1, f"expected one run containing {needle!r}, got {len(matches)}"
    return matches[0]


def _assert_no_native_revisions(root: ET.Element) -> None:
    for local in (
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
    ):
        assert root.find(f".//{W}{local}") is None


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nPlain text.\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_revision_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n\n"
        "Added: \\added{ADDED}.\n\n"
        "Deleted: \\deleted{DELETED}.\n\n"
        "Replaced: \\replaced{CURRENT}{FORMER}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


class _MinimalReviewBackend:
    def capabilities(self) -> BackendCapabilities:
        return Tex2WordBackend().capabilities()

    def export(self, request: BackendRequest) -> BackendResult:
        write_docx(
            request.output_path,
            document_xml=(
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
                b'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                b"Plain text.</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
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


def test_tex2word_real_e0_contract_on_a_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT / "source", source)
    output = tmp_path / "output" / "review.docx"

    result = Tex2WordBackend().export(BackendRequest(source, "main.tex", output))
    inspection = inspect_docx(output)

    assert result.succeeded
    assert result.capabilities.tool_version == "1.0.5"
    assert result.capabilities.interface_version == tex2word_module.TEX2WORD_INTERFACE_VERSION
    assert result.native_report["reference_loaded"] is True
    assert result.native_report["reference_profile"] == "academic-review-v1"
    assert result.native_report["revision_view"] == "source"
    assert result.native_report["revision_aliases"] == []
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
    with zipfile.ZipFile(output) as package:
        styles = ET.fromstring(package.read("word/styles.xml"))
        document = ET.fromstring(package.read("word/document.xml"))
    fonts = styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr/{W}rFonts")
    defaults = styles.find(f"{W}docDefaults/{W}pPrDefault/{W}pPr")
    assert fonts is not None and defaults is not None
    assert fonts.get(f"{W}ascii") == "Times New Roman"
    assert fonts.get(f"{W}eastAsia") == "SimSun"
    assert _required_child(defaults, f"{W}jc").get(f"{W}val") == "both"
    assert _required_child(_style_by_name(styles, "Title"), f"{W}rPr/{W}sz").get(f"{W}val") == "44"
    section = document.find(f".//{W}sectPr")
    assert section is not None
    assert _required_child(section, f"{W}pgSz").attrib == {
        f"{W}w": "11906",
        f"{W}h": "16838",
    }


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
    assert result.native_report["failure_kind"] == "timeout"
    assert result.native_report["failure_code"] == ErrorCode.BACKEND_FAILED.value
    assert not any("private" in str(value) for value in result.native_report.values())
    assert output.read_bytes() == b"existing"
    assert observed["cwd"] == source.resolve()
    assert observed["timeout_s"] == 0.05
    arguments = observed["arguments"]
    assert isinstance(arguments, tuple)
    assert arguments[:2] == ("-m", "latex_word_review.backends._tex2word_worker")
    alias_index = arguments.index("--revision-aliases")
    assert arguments[alias_index + 1] == "none"
    assert not list(output.parent.glob(".review.docx.tex2word-*.docx"))
    assert not list(output.parent.glob(".lwr-t2w-report-*.json"))


def test_tex2word_report_path_has_a_fixed_short_owned_name(tmp_path: Path) -> None:
    report = Tex2WordBackend._prepare_report_path(tmp_path)

    assert report.parent == tmp_path
    assert report.name.startswith(".lwr-t2w-report-")
    assert report.suffix == ".json"
    assert len(report.name) < 40
    assert not report.exists()
    Tex2WordBackend._cleanup_report_path(report, tmp_path)


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

    assert outcome.report.status == "partial"
    assert outcome.anchoring is not None
    assert outcome.anchoring.coverage == {
        "total": 23,
        "exact": 19,
        "degraded": 0,
        "unmapped": 0,
        "conflict": 4,
    }
    assert outcome.inspection is not None
    assert outcome.inspection.bookmarks == 28
    assert {feature.feature: feature.status for feature in outcome.report.feature_results} == {
        "body_text": "degraded",
        "images": "preserved",
        "tables": "preserved",
        "math": "preserved",
        "references": "preserved",
        "labels": "preserved",
        "citations": "unsupported",
    }
    assert output.is_file()
    with zipfile.ZipFile(output) as package:
        styles = ET.fromstring(package.read("word/styles.xml"))
        document = ET.fromstring(package.read("word/document.xml"))
    fonts = styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr/{W}rFonts")
    defaults = styles.find(f"{W}docDefaults/{W}pPrDefault/{W}pPr")
    assert fonts is not None and defaults is not None
    assert fonts.get(f"{W}ascii") == "Times New Roman"
    assert fonts.get(f"{W}eastAsia") == "SimSun"
    assert _required_child(defaults, f"{W}spacing").get(f"{W}line") == "320"
    assert (
        _required_child(_style_by_name(styles, "heading 1"), f"{W}rPr/{W}color").get(f"{W}val")
        == "1F4E79"
    )
    section = document.find(f".//{W}sectPr")
    assert section is not None
    assert _required_child(section, f"{W}pgSz").attrib == {
        f"{W}w": "11906",
        f"{W}h": "16838",
    }
    assert _required_child(section, f"{W}cols").get(f"{W}num") == "1"


def test_tex2word_clean_revision_view_keeps_only_current_text(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_revision_project(source)
    output = tmp_path / "output/clean.docx"

    result = Tex2WordBackend().export(
        BackendRequest(source, "main.tex", output, revision_view="clean")
    )

    assert result.succeeded
    assert result.native_report["revision_view"] == "clean"
    assert result.native_report["revision_aliases"] == []
    with zipfile.ZipFile(output) as package:
        document = ET.fromstring(package.read("word/document.xml"))
    text = "".join(node.text or "" for node in document.iter(f"{W}t"))
    assert "ADDED" in text
    assert "CURRENT" in text
    assert "DELETED" not in text
    assert "FORMER" not in text
    assert document.find(f".//{W}color") is None
    assert document.find(f".//{W}strike") is None
    assert document.find(f".//{W}highlight") is None
    _assert_no_native_revisions(document)


def test_tex2word_display_revision_view_uses_blue_runs_and_strike(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_revision_project(source)
    output = tmp_path / "output/display.docx"

    result = Tex2WordBackend().export(
        BackendRequest(source, "main.tex", output, revision_view="display")
    )

    assert result.succeeded
    assert result.native_report["revision_view"] == "display"
    assert result.native_report["revision_aliases"] == []
    with zipfile.ZipFile(output) as package:
        document = ET.fromstring(package.read("word/document.xml"))
    text = "".join(node.text or "" for node in document.iter(f"{W}t"))
    assert text.index("FORMER") < text.index("CURRENT")
    assert _direct_run_style(document, "ADDED") == ("0000FF", False, False)
    assert _direct_run_style(document, "DELETED") == ("0000FF", True, False)
    assert _direct_run_style(document, "FORMER") == ("0000FF", True, False)
    assert _direct_run_style(document, "CURRENT") == ("0000FF", False, False)
    assert document.find(f".//{W}highlight") is None
    _assert_no_native_revisions(document)


def test_tex2word_display_revision_aliases_are_explicit_and_styled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Alias add: \\add{ALIASADDED}.\n"
        "Alias delete: \\delete{ALIASDELETED}.\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "output/aliases.docx"

    result = Tex2WordBackend().export(
        BackendRequest(
            source,
            "main.tex",
            output,
            revision_view="display",
            revision_aliases=("add", "delete"),
        )
    )

    assert result.succeeded
    assert result.native_report["revision_aliases"] == ["add", "delete"]
    with zipfile.ZipFile(output) as package:
        document = ET.fromstring(package.read("word/document.xml"))
    assert _direct_run_style(document, "ALIASADDED") == ("0000FF", False, False)
    assert _direct_run_style(document, "ALIASDELETED") == ("0000FF", True, False)
    assert document.find(f".//{W}dstrike") is None


def test_prepare_export_rejects_unknown_revision_view(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)

    with pytest.raises(ContractError) as raised:
        prepare_export(
            BackendRequest(
                source,
                "main.tex",
                tmp_path / "output/review.docx",
                revision_view="unknown",  # type: ignore[arg-type]
            ),
            owner="test",
        )

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("revision_view", "revision_aliases"),
    [
        ("source", ("add",)),
        ("clean", ("delete", "add")),
        ("display", ("add", "add")),
        ("display", ("unknown",)),
    ],
)
def test_prepare_export_rejects_invalid_revision_alias_contracts(
    tmp_path: Path,
    revision_view: str,
    revision_aliases: tuple[str, ...],
) -> None:
    source = tmp_path / "source"
    _write_project(source)

    with pytest.raises(ContractError) as raised:
        prepare_export(
            BackendRequest(
                source,
                "main.tex",
                tmp_path / "output/review.docx",
                revision_view=cast("Any", revision_view),
                revision_aliases=cast("Any", revision_aliases),
            ),
            owner="test",
        )

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("revision_aliases", "revision_view", "message"),
    [
        (["delete"], "display", "invalid aliases"),
        (["add"], "clean", "invalid reference data"),
    ],
)
def test_tex2word_worker_report_rejects_revision_contract_drift(
    tmp_path: Path,
    revision_aliases: list[str],
    revision_view: str,
    message: str,
) -> None:
    report_path = tmp_path / "worker-report.json"
    report_path.write_text(
        json.dumps(
            {
                "constructs": [],
                "entry_count": 0,
                "error_count": 0,
                "math_image": 0,
                "math_omml": 0,
                "math_raw": 0,
                "reference_loaded": True,
                "reference_profile": PROFILE_ID,
                "reference_sha256": REFERENCE_DOCX_SHA256,
                "revision_aliases": revision_aliases,
                "revision_view": revision_view,
                "warning_constructs": [],
                "warning_count": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError, match=message) as raised:
        Tex2WordBackend._read_report(
            report_path,
            expected_revision_view="display",
            expected_revision_aliases=("add",),
        )
    assert raised.value.code is ErrorCode.BACKEND_FAILED


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


def test_pandoc_rejects_non_source_revision_view_without_invoking_tool(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out/review.docx"

    result = PandocBackend(version_override="test").export(
        BackendRequest(source, "main.tex", output, revision_view="display")
    )

    assert not result.succeeded
    assert result.findings[0].code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert dict(result.capabilities.features)["revision_view"].support == "none"
    assert not output.exists()
    assert not list(output.parent.glob(".review.docx.pandoc-*.docx"))


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


def test_silent_loss_gate_counts_redundantly_grouped_image_instances(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "plot.png").write_bytes(b"synthetic-static-image")
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\nPlain text.\n\n"
        "\\includegraphics{{plot.png}}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output" / "review.docx"
    document_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Plain text.</w:t></w:r></w:p></w:body>
    </w:document>"""

    class MissingGroupedImageBackend:
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
        MissingGroupedImageBackend(),
        BackendRequest(source, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "d" * 64,
            artifact_path="artifacts/review.docx",
        ),
    )

    assert "plot.png" in {item.path for item in discovery.files}
    assert outcome.output_path is None
    assert outcome.report.status == "failed"
    assert outcome.image_overlay is not None
    assert outcome.image_overlay.source_image_instances == 1
    image_feature = next(
        feature for feature in outcome.report.feature_results if feature.feature == "images"
    )
    assert (image_feature.source_count, image_feature.output_count, image_feature.status) == (
        1,
        0,
        "failed",
    )
    assert any(finding.code is ErrorCode.EXPORT_SILENT_LOSS for finding in outcome.report.findings)
    assert not output.exists()
    assert not output.with_name(f"{output.name}.image-overlay").exists()


def test_export_publication_race_preserves_the_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output" / "review.docx"
    original_link = os.link

    def racing_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(destination_path) == output:
            output.write_bytes(b"racing-destination")
            raise FileExistsError(destination_path)
        original_link(
            source_path,
            destination_path,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("latex_word_review.export.os.link", racing_link)
    with pytest.raises(ContractError) as raised:
        export_review_docx(
            _MinimalReviewBackend(),
            BackendRequest(source, "main.tex", output),
            discovery,
            ExportBindings(
                source_manifest_sha256="sha256:" + "e" * 64,
                artifact_path="artifacts/review.docx",
                confidentiality="public_fixture",
            ),
        )

    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert output.read_bytes() == b"racing-destination"
    assert not output.with_name(f"{output.name}.image-overlay").exists()
    assert not list(output.parent.glob(f".{output.name}.*-*.docx"))


def test_export_hardlink_is_the_last_business_commit_and_cleanup_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    discovery = discover_project(source, main_document="main.tex")
    output = tmp_path / "output" / "review.docx"
    state = {
        "artifact_built": False,
        "report_built": False,
        "outcome_built": False,
        "linked": False,
    }
    original_artifact = ReviewDocxArtifact.from_file
    original_report = ExportReport
    original_outcome = ExportOutcome
    original_link = os.link
    concrete_path = type(output)
    original_unlink = concrete_path.unlink
    failed_unlinks: list[Path] = []

    def track_artifact(
        cls: type[Any],
        file_path: Path,
        *,
        artifact_path: str,
        confidentiality: Any,
    ) -> Any:
        del cls
        assert not state["linked"]
        state["artifact_built"] = True
        return original_artifact(
            file_path,
            artifact_path=artifact_path,
            confidentiality=confidentiality,
        )

    def track_report(*args: Any, **kwargs: Any) -> Any:
        assert not state["linked"]
        state["report_built"] = True
        return original_report(*args, **kwargs)

    def track_outcome(*args: Any, **kwargs: Any) -> Any:
        assert not state["linked"]
        state["outcome_built"] = True
        return original_outcome(*args, **kwargs)

    def tracking_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        original_link(
            source_path,
            destination_path,
            follow_symlinks=follow_symlinks,
        )
        if Path(destination_path) == output:
            assert state["artifact_built"]
            assert state["report_built"]
            assert state["outcome_built"]
            state["linked"] = True

    def fail_first_post_link_stage_unlink(
        self: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if (
            state["linked"]
            and self.name.startswith(f".{output.name}.anchored-")
            and not failed_unlinks
        ):
            failed_unlinks.append(self)
            raise PermissionError("simulated post-link cleanup failure")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(
        ReviewDocxArtifact,
        "from_file",
        classmethod(track_artifact),
    )
    monkeypatch.setattr("latex_word_review.export.ExportReport", track_report)
    monkeypatch.setattr("latex_word_review.export.ExportOutcome", track_outcome)
    monkeypatch.setattr("latex_word_review.export.os.link", tracking_link)
    monkeypatch.setattr(concrete_path, "unlink", fail_first_post_link_stage_unlink)

    outcome = export_review_docx(
        _MinimalReviewBackend(),
        BackendRequest(source, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256="sha256:" + "f" * 64,
            artifact_path="artifacts/review.docx",
            confidentiality="public_fixture",
        ),
    )

    assert state == {
        "artifact_built": True,
        "report_built": True,
        "outcome_built": True,
        "linked": True,
    }
    assert outcome.output_path == output
    assert outcome.report.status == "success"
    assert outcome.report.review_docx is not None
    assert (
        outcome.report.review_docx.sha256
        == digest_file(
            output,
            max_bytes=128 * 1024 * 1024,
        ).sha256
    )
    assert len(failed_unlinks) == 1
    assert failed_unlinks[0].exists()
    assert os.path.samefile(failed_unlinks[0], output)

    monkeypatch.setattr(concrete_path, "unlink", original_unlink)
    failed_unlinks[0].unlink()
    assert output.is_file()
    assert not list(output.parent.glob(f".{output.name}.*-*.docx"))
