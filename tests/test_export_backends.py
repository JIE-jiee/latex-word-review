"""Backend execution, cleanup, E0 contract, and pipeline tests."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import latex_word_review.backends.pandoc as pandoc_module
import latex_word_review.backends.tex2word as tex2word_module
from latex_word_review.backends import BackendRequest, PandocBackend, Tex2WordBackend
from latex_word_review.discovery import discover_project
from latex_word_review.export import ExportBindings, export_review_docx
from latex_word_review.inspection import inspect_docx
from latex_word_review.runtime import CommandResult

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
