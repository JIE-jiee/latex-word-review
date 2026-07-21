"""Integration tests for the conservative fixed-layout workflow facade."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.image_materializer as image_materializer_module
import latex_word_review.workflow as workflow_module
from latex_word_review.backends import Tex2WordBackend
from latex_word_review.backends.base import BackendResult
from latex_word_review.canonical import canonical_json, seal_envelope
from latex_word_review.cli import main
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export import ExportOutcome
from latex_word_review.export_models import ExportFinding, ExportReport, ExportValidation
from latex_word_review.hashing import digest_file
from latex_word_review.jsonio import read_contract_file
from latex_word_review.paths import windows_extended_path
from latex_word_review.workflow import (
    clean_workflow,
    export_workflow,
    initialize_workflow,
    receive_workflow,
    workflow_status,
)
from tests.e2e_helpers import create_whole_bookmark_replacement
from tests.test_image_materializer import _minimal_pdf, _require_pdf_runtime

FIXTURE = Path(__file__).parent / "fixtures/e0-minimal-paper/source"
TIME = "2026-07-17T00:00:00Z"


def _copy_source(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    shutil.copytree(FIXTURE, origin)
    return origin


def _bytes_by_path(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _initialize(tmp_path: Path) -> tuple[Path, Path]:
    origin = _copy_source(tmp_path)
    run = tmp_path / "run"
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    return origin, run


def _export(tmp_path: Path) -> tuple[Path, Path]:
    origin, run = _initialize(tmp_path)
    export_workflow(run, confidentiality="public_fixture", generated_at=TIME)
    return origin, run


def _make_returned(run: Path, destination: Path) -> None:
    source_map = read_contract_file(
        run / "export/objects/source-map.json",
        expected_schema="SourceMap",
    )
    payload = cast("dict[str, Any]", source_map["payload"])
    mapping = next(
        item
        for item in cast("list[dict[str, Any]]", payload["mappings"])
        if item["status"] == "exact"
    )
    location = cast("dict[str, Any]", mapping["source_location"])
    source = (run / "snapshot" / cast("str", location["path"])).read_bytes()
    start = cast("int", location["start_byte"])
    end = cast("int", location["end_byte"])
    before = source[start:end].decode("utf-8")
    anchor = cast("dict[str, Any]", mapping["docx_anchor"])
    create_whole_bookmark_replacement(
        run / "export/review.docx",
        destination,
        bookmark_name=cast("str", anchor["name"]),
        before=before,
        after=f"{before} reviewed",
    )


def _rewrite_sealed_contract(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    document = read_contract_file(path)
    mutate(cast("dict[str, Any]", document["payload"]))
    resealed = seal_envelope(document)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    path.write_bytes(canonical_json(resealed) + b"\n")
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def test_init_is_atomic_no_clobber_and_keeps_sealed_json_path_free(tmp_path: Path) -> None:
    origin = _copy_source(tmp_path)
    before = _bytes_by_path(origin)
    run = tmp_path / "run"

    result = initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    assert result["phase"] == "snapshotted"
    assert result["next_command"] == "latex-word-review workflow export ."
    assert (run / "snapshot/snapshot-manifest.json").is_file()
    source_manifest_path = run / "objects/source-manifest.json"
    source_manifest = read_contract_file(source_manifest_path, expected_schema="SourceManifest")
    artifact = cast("dict[str, Any]", source_manifest["payload"])["snapshot_artifact"]
    assert artifact["path"] == "snapshot/snapshot-manifest.json"
    serialized = source_manifest_path.read_text(encoding="utf-8")
    assert str(origin) not in serialized
    assert str(run) not in serialized
    assert not source_manifest_path.stat().st_mode & stat.S_IWUSR
    assert _bytes_by_path(origin) == before
    assert not list(tmp_path.glob(".run.lwr-init-*"))

    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        initialize_workflow(origin, run, main_document="main.tex")
    assert _bytes_by_path(origin) == before


def test_export_uses_fixed_paths_is_read_only_and_rejects_reentry(tmp_path: Path) -> None:
    _origin, run = _initialize(tmp_path)

    result = export_workflow(run, confidentiality="public_fixture", generated_at=TIME)

    assert result["phase"] == "exported"
    assert result["counts"]["review_units"] > 0
    review = run / "export/review.docx"
    assert review.is_file()
    assert not review.stat().st_mode & stat.S_IWUSR
    before = digest_file(review, max_bytes=128 * 1024 * 1024)
    status = workflow_status(run)
    assert status["phase"] == "exported"
    assert status["integrity"] == "workflow_bindings_verified"
    assert status["apply_enabled"] is False

    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        export_workflow(run, confidentiality="public_fixture")
    assert digest_file(review, max_bytes=128 * 1024 * 1024) == before
    assert not list((run / ".lwr-staging").iterdir())


def test_receive_archives_original_without_approving_or_applying(tmp_path: Path) -> None:
    origin, run = _export(tmp_path)
    origin_before = _bytes_by_path(origin)
    returned = tmp_path / "returned.docx"
    _make_returned(run, returned)
    returned_before = returned.read_bytes()

    result = receive_workflow(
        run,
        returned,
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    assert result["phase"] == "ingested"
    assert result["apply_enabled"] is False
    assert result["counts"]["changes"] == 1
    archived = run / "receive/original/returned-original.docx"
    assert archived.read_bytes() == returned_before
    assert not archived.stat().st_mode & stat.S_IWUSR
    changeset = read_contract_file(run / "receive/changeset.json", expected_schema="ChangeSet")
    payload = cast("dict[str, Any]", changeset["payload"])
    baseline = cast("dict[str, Any]", payload["baseline_verification"])
    assert baseline["status"] == "verified_for_text_patch"
    assert baseline["automatic_patch_scope"] == "plain_text_only"
    assert baseline["manual_integrity_review_required"] is True
    assert baseline["verified_scope"] == [
        "visible_text",
        "paragraph_table_structure",
        "field_structure",
        "bookmarks",
        "insert_delete_revisions",
        "move_revisions",
    ]
    assert baseline["unverified_scope"] == [
        "paragraph_mark_revisions",
        "formatting",
        "omml",
        "images",
        "hyperlink_and_relationship_targets",
        "content_controls_and_custom_xml",
        "embedded_objects_and_alternate_content",
    ]
    assert result["baseline_verification"] == baseline
    assert result["manual_integrity_review_required"] is True
    assert payload["returned_original"]["path"] == ("receive/original/returned-original.docx")
    assert returned.read_bytes() == returned_before
    assert _bytes_by_path(origin) == origin_before
    assert not (run / "approvals").exists()
    assert not (run / "revised").exists()
    for sealed_json in run.rglob("*.json"):
        serialized = sealed_json.read_text(encoding="utf-8")
        assert str(run) not in serialized
        assert str(tmp_path) not in serialized

    status = workflow_status(run)
    assert status["phase"] == "ingested"
    assert status["counts"]["changes"] == 1
    assert status["baseline_verification"] == baseline
    assert status["manual_integrity_review_required"] is True
    assert "approve init" in status["next_command"]
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        receive_workflow(run, returned, confidentiality="public_fixture")
    assert archived.read_bytes() == returned_before


def test_status_fails_closed_on_export_hash_drift(tmp_path: Path) -> None:
    _origin, run = _export(tmp_path)
    review = run / "export/review.docx"
    review.chmod(stat.S_IRUSR | stat.S_IWUSR)
    try:
        review.write_bytes(review.read_bytes() + b"drift")
        review.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
            workflow_status(run)
    finally:
        review.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_status_fails_closed_on_image_overlay_manifest_drift(tmp_path: Path) -> None:
    _origin, run = _export(tmp_path)
    manifest = run / "export/review.docx.image-overlay/image-overlay-manifest.json"
    manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    manifest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)


def test_status_rejects_resealed_image_overlay_binding_mismatch(tmp_path: Path) -> None:
    _origin, run = _export(tmp_path)
    source_map = run / "export/objects/source-map.json"

    def change_derived_hash(payload: dict[str, Any]) -> None:
        binding = cast("dict[str, Any]", payload["image_overlay"])
        binding["derived_source_tree_sha256"] = "sha256:" + "f" * 64

    _rewrite_sealed_contract(source_map, change_derived_hash)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)


def test_status_rejects_resealed_image_overlay_manifest_path(tmp_path: Path) -> None:
    _origin, run = _export(tmp_path)

    def change_manifest_path(payload: dict[str, Any]) -> None:
        binding = cast("dict[str, Any]", payload["image_overlay"])
        manifest = cast("dict[str, Any]", binding["manifest"])
        manifest["path"] = "export/untrusted/image-overlay-manifest.json"

    for name in ("source-map.json", "export-report.json"):
        _rewrite_sealed_contract(run / "export/objects" / name, change_manifest_path)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)


def test_status_fails_closed_on_image_overlay_png_drift(tmp_path: Path) -> None:
    _origin, run = _export(tmp_path)
    overlay_root = run / "export/review.docx.image-overlay"
    png = next(overlay_root.rglob("*.png"))
    png.chmod(stat.S_IRUSR | stat.S_IWUSR)
    png.write_bytes(png.read_bytes() + b"drift")
    png.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)


def _tamper_cache_unknown_field(cache: dict[str, Any]) -> None:
    cache["unauthorized_extra"] = "tamper"


def _tamper_cache_format(cache: dict[str, Any]) -> None:
    cache["format"] = "tampered-cache-format"


def _tamper_cache_renderer(cache: dict[str, Any]) -> None:
    cache["cache_key"]["renderer"]["pdfium_version"] = "0.0.0.0"


def _tamper_cache_request(cache: dict[str, Any]) -> None:
    cache["request"]["page"] = 1


def _tamper_cache_key(cache: dict[str, Any]) -> None:
    cache["cache_key_sha256"] = "sha256:" + "0" * 64


def _tamper_cache_output(cache: dict[str, Any]) -> None:
    cache["output"]["sha256"] = "sha256:" + "0" * 64


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_cache_unknown_field,
        _tamper_cache_format,
        _tamper_cache_renderer,
        _tamper_cache_request,
        _tamper_cache_key,
        _tamper_cache_output,
    ],
    ids=["unknown-field", "format", "renderer", "request", "cache-key", "output"],
)
def test_status_verifies_materialized_pdf_cache_evidence(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[dict[str, Any]], None],
) -> None:
    _require_pdf_runtime()
    tmp_path = tmp_path_factory.mktemp("p")
    origin = tmp_path / "origin"
    (origin / "figures").mkdir(parents=True)
    (origin / "figures/multi.pdf").write_bytes(
        _minimal_pdf(((1, 0, 0), (0, 0, 1)), width=72, height=36)
    )
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n\n"
        "Review text.\n\n\\includegraphics[page=2]{figures/multi.pdf}\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    run = tmp_path / "run"
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    export_workflow(run, confidentiality="public_fixture", generated_at=TIME)

    def forbidden_pixel_reload(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("sealed workflow reload must not decode or re-encode PNG pixels")

    monkeypatch.setattr(image_materializer_module, "_decode_png", forbidden_pixel_reload)
    monkeypatch.setattr(image_materializer_module, "_encode_canonical_png", forbidden_pixel_reload)
    monkeypatch.setattr(
        image_materializer_module,
        "_load_pillow_image_module",
        forbidden_pixel_reload,
    )
    assert workflow_status(run)["integrity"] == "workflow_bindings_verified"

    overlay_root = run / "export/review.docx.image-overlay"
    cache_manifest = next((overlay_root / "lwr-images").rglob("manifest.json"))
    cache = json.loads(cache_manifest.read_bytes())
    tamper(cache)
    cache_manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    cache_manifest.write_bytes(canonical_json(cache) + b"\n")
    cache_manifest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)


def test_status_fails_closed_on_snapshot_hash_drift(tmp_path: Path) -> None:
    _origin, run = _initialize(tmp_path)
    snapshot_root = run / "snapshot"
    snapshot_root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)
    snapshot_root.chmod(stat.S_IRUSR | stat.S_IXUSR)
    source_manifest = run / "objects/source-manifest.json"
    source_manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)
    source_manifest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    source = run / "snapshot/main.tex"
    source.chmod(stat.S_IRUSR | stat.S_IWUSR)
    try:
        source.write_text(source.read_text(encoding="utf-8") + "% drift\n", encoding="utf-8")
        source.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
            workflow_status(run)
    finally:
        source.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_clean_is_dry_run_by_default_and_uses_a_narrow_allowlist(tmp_path: Path) -> None:
    _origin, run = _initialize(tmp_path)
    staging = run / ".lwr-staging"
    stage = staging / ".lwr-stage-export-deadbeef"
    failed = staging / ".lwr-failed-receive-deadbeef"
    ignored = staging / "keep-me"
    stage.mkdir()
    failed.mkdir()
    ignored.mkdir()
    (stage / "partial.tmp").write_bytes(b"partial")
    (failed / "failure.tmp").write_bytes(b"failure")
    (ignored / "user.txt").write_bytes(b"user")

    preview = clean_workflow(run)

    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 2
    assert preview["removed_count"] == 0
    assert all(not Path(item).is_absolute() for item in preview["candidates"])
    assert stage.exists() and failed.exists() and ignored.exists()
    assert (run / "snapshot").exists()

    executed = clean_workflow(run, execute=True)
    assert executed["dry_run"] is False
    assert executed["removed_count"] == 2
    assert not stage.exists() and not failed.exists()
    assert (ignored / "user.txt").read_bytes() == b"user"
    assert (run / "snapshot").exists()
    assert (run / "objects/source-manifest.json").exists()


def test_clean_rejects_linked_stage_before_removing_other_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, run = _initialize(tmp_path)
    staging = run / ".lwr-staging"
    safe = staging / ".lwr-stage-export-safesix"
    unsafe = staging / ".lwr-stage-receive-linked"
    safe.mkdir()
    unsafe.mkdir()
    original_probe = workflow_module._is_link_or_junction

    def simulated_reparse_point(path: Path) -> bool:
        return path == unsafe or original_probe(path)

    monkeypatch.setattr(workflow_module, "_is_link_or_junction", simulated_reparse_point)

    with pytest.raises(ContractError, match=ErrorCode.PATH_LINK_ESCAPE.value):
        clean_workflow(run, execute=True)
    assert safe.exists()
    assert unsafe.exists()


def test_failed_export_cleans_its_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, run = _initialize(tmp_path)
    (run / ".lwr-staging").rmdir()

    def fail_export(*_args: object, **_kwargs: object) -> object:
        raise ContractError(ErrorCode.BACKEND_FAILED, "synthetic backend failure")

    monkeypatch.setattr("latex_word_review.workflow.export_review_docx", fail_export)
    with pytest.raises(ContractError, match=ErrorCode.BACKEND_FAILED.value):
        export_workflow(run, confidentiality="public_fixture")
    assert not (run / "export").exists()
    assert (run / ".lwr-staging").is_dir()
    assert not list((run / ".lwr-staging").iterdir())


def test_failed_export_preserves_safe_backend_cause_without_paths() -> None:
    capabilities = Tex2WordBackend().capabilities()
    finding = ExportFinding(
        code=ErrorCode.TOOL_VERSION_UNSUPPORTED,
        severity="error",
        phase="export",
        message="synthetic path-free failure",
        recoverable=False,
    )
    backend_result = BackendResult(
        status="failed",
        capabilities=capabilities,
        artifact_name=None,
        artifact_sha256=None,
        artifact_size_bytes=None,
        findings=(finding,),
        native_report={
            "duration_ms": 12_345,
            "output_truncated": False,
            "error_count": 2,
            "warning_count": 1,
            "failure_kind": "unsupported_version",
            "private_path": r"C:\private\paper.tex",
        },
        timed_out=False,
        returncode=7,
    )
    report = ExportReport(
        status="failed",
        source_manifest_sha256="sha256:" + "a" * 64,
        backend_capabilities_sha256=capabilities.payload_sha256,
        review_ir_sha256=None,
        source_map_sha256=None,
        review_docx=None,
        image_overlay={},
        source_metrics={},
        output_metrics={},
        feature_results=(),
        findings=(finding,),
        validation=ExportValidation("blocked", "not_run", "blocked", "blocked"),
    )
    outcome = ExportOutcome(
        backend_result=backend_result,
        report=report,
        units=(),
        anchoring=None,
        inspection=None,
        output_path=None,
    )
    error = workflow_module._export_failure_error(outcome, backend_name="tex2word")

    assert error.code is ErrorCode.TOOL_VERSION_UNSUPPORTED
    assert error.violation.details == {
        "provider": "tex2word",
        "stage": "backend_export",
        "export_status": "failed",
        "backend_status": "failed",
        "backend_error_code": ErrorCode.TOOL_VERSION_UNSUPPORTED.value,
        "timed_out": "no",
        "finding_count": 1,
        "returncode": 7,
        "duration_ms": 12_345,
        "error_count": 2,
        "warning_count": 1,
        "output_truncated": "no",
        "failure_kind": "unsupported_version",
    }
    assert "private" not in str(error.as_dict())


def test_partial_export_is_reviewable_only_for_recoverable_warnings() -> None:
    capabilities = Tex2WordBackend().capabilities()
    warning = ExportFinding(
        code=ErrorCode.EXPORT_DEGRADED,
        severity="warning",
        phase="export",
        message="synthetic recoverable compatibility warning",
        recoverable=True,
    )
    report = ExportReport(
        status="partial",
        source_manifest_sha256="sha256:" + "a" * 64,
        backend_capabilities_sha256=capabilities.payload_sha256,
        review_ir_sha256="sha256:" + "b" * 64,
        source_map_sha256="sha256:" + "c" * 64,
        review_docx=None,
        image_overlay={},
        source_metrics={},
        output_metrics={},
        feature_results=(),
        findings=(warning,),
        validation=ExportValidation("pass", "not_run", "pass", "pass"),
    )

    assert workflow_module._is_reviewable_export_report(report)
    assert workflow_module._is_reviewable_export_payload(report.as_payload())

    blocking = replace(
        report,
        findings=(
            ExportFinding(
                code=ErrorCode.EXPORT_SILENT_LOSS,
                severity="error",
                phase="inspect",
                message="synthetic missing content",
                recoverable=False,
            ),
        ),
    )
    assert not workflow_module._is_reviewable_export_report(blocking)
    assert not workflow_module._is_reviewable_export_payload(blocking.as_payload())


def test_public_workflow_arguments_fail_closed(tmp_path: Path) -> None:
    origin = _copy_source(tmp_path)
    invalid_confidentiality = cast("Any", "not-a-classification")
    invalid_backend = cast("Any", "not-a-backend")
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        initialize_workflow(
            origin,
            tmp_path / "invalid-run",
            confidentiality=invalid_confidentiality,
        )

    run = tmp_path / "run"
    initialize_workflow(origin, run, main_document="main.tex", generated_at=TIME)
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        export_workflow(run, backend=invalid_backend)
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        export_workflow(run, confidentiality=invalid_confidentiality)
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        export_workflow(run, timeout_s=0)
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        receive_workflow(run, tmp_path / "missing.docx", confidentiality=invalid_confidentiality)
    assert not list((run / ".lwr-staging").iterdir())


def test_init_failure_cleans_atomic_sibling_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _copy_source(tmp_path)
    run = tmp_path / "failed-run"

    def fail_snapshot(*_args: object, **_kwargs: object) -> object:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "synthetic snapshot drift")

    monkeypatch.setattr(workflow_module, "snapshot_project", fail_snapshot)
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        initialize_workflow(origin, run, main_document="main.tex")
    assert not run.exists()
    assert not list(tmp_path.glob(".failed-run.lwr-init-*"))


def test_status_rejects_receive_without_an_export(tmp_path: Path) -> None:
    _origin, run = _initialize(tmp_path)
    (run / ".lwr-staging").rmdir()
    status = workflow_status(run)
    assert status["counts"]["stale_stages"] == 0
    (run / "receive").mkdir()
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        workflow_status(run)


def test_existing_export_directory_is_never_clobbered(tmp_path: Path) -> None:
    _origin, run = _initialize(tmp_path)
    export = run / "export"
    export.mkdir()
    marker = export / "user-marker.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        export_workflow(run, confidentiality="public_fixture")
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not list((run / ".lwr-staging").iterdir())


def test_cli_workflow_status_and_clean_emit_machine_readable_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    origin = _copy_source(tmp_path)
    run = tmp_path / "run"
    assert (
        main(
            [
                "workflow",
                "init",
                str(origin),
                str(run),
                "--main",
                "main.tex",
                "--confidentiality",
                "public_fixture",
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["phase"] == "snapshotted"
    assert main(["workflow", "status", str(run)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["integrity"] == "workflow_bindings_verified"
    assert main(["workflow", "clean", str(run)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True


def test_path_and_publication_guards_reject_ambiguous_filesystem_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    regular = tmp_path / "regular.txt"
    directory = tmp_path / "directory"
    regular.write_text("content", encoding="utf-8")
    directory.mkdir()
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        workflow_module._require_real_directory(missing, "missing")
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        workflow_module._require_real_directory(regular, "file")
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        workflow_module._require_regular_file(missing, "missing")
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        workflow_module._require_regular_file(directory, "directory")

    linklike_directory = tmp_path / "linklike-directory"
    linklike_file = tmp_path / "linklike-file"
    tree = tmp_path / "tree"
    linklike_directory.mkdir()
    linklike_file.write_text("content", encoding="utf-8")
    tree.mkdir()
    tree_link = tree / "linked-child"
    tree_link.mkdir()
    simulated_targets = {
        windows_extended_path(path) for path in (linklike_directory, linklike_file, tree_link)
    }
    original_probe = workflow_module._is_link_or_junction

    def simulated_links(path: Path) -> bool:
        return windows_extended_path(path) in simulated_targets or original_probe(path)

    monkeypatch.setattr(workflow_module, "_is_link_or_junction", simulated_links)
    with pytest.raises(ContractError, match=ErrorCode.PATH_LINK_ESCAPE.value):
        workflow_module._require_real_directory(linklike_directory, "link")
    with pytest.raises(ContractError, match=ErrorCode.PATH_LINK_ESCAPE.value):
        workflow_module._require_regular_file(linklike_file, "link")
    with pytest.raises(ContractError, match=ErrorCode.PATH_LINK_ESCAPE.value):
        workflow_module._tree_entries_without_links(tree)

    parent = tmp_path / "stages"
    other_parent = tmp_path / "other-stages"
    parent.mkdir()
    other_parent.mkdir()
    unowned = parent / "user-data"
    unowned.mkdir()
    with pytest.raises(ContractError, match=ErrorCode.INTERNAL_INVARIANT.value):
        workflow_module._remove_owned_tree(
            unowned,
            parent,
            prefixes=(".lwr-stage-export-",),
        )
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        workflow_module._remove_owned_tree(
            parent / ".lwr-stage-export-missing",
            parent,
            prefixes=(".lwr-stage-export-",),
        )
    escaped = parent / ".lwr-stage-export-escaped"
    escaped.mkdir()
    with pytest.raises(ContractError, match=ErrorCode.PATH_LINK_ESCAPE.value):
        workflow_module._remove_owned_tree(
            escaped,
            other_parent,
            prefixes=(".lwr-stage-export-",),
        )

    publication_root = tmp_path / "publication"
    staging = publication_root / ".lwr-staging/stage"
    staging.mkdir(parents=True)
    payload = staging / "payload"
    payload.mkdir()
    destination = publication_root / "export"
    destination.mkdir()
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        workflow_module._publish_payload(payload, destination)
    destination.rmdir()
    foreign = tmp_path / "foreign/payload"
    foreign.mkdir(parents=True)
    with pytest.raises(ContractError, match=ErrorCode.INTERNAL_INVARIANT.value):
        workflow_module._publish_payload(foreign, destination)


@pytest.mark.skipif(os.name != "nt", reason="Win32 MAX_PATH regression")
def test_owned_workflow_stage_supports_paths_beyond_legacy_max_path(tmp_path: Path) -> None:
    parent = tmp_path / "stages"
    parent.mkdir()
    stage = parent / ".lwr-stage-export-abcdefgh"
    stage.mkdir()
    filesystem_stage = windows_extended_path(stage)
    nested = filesystem_stage / ("n" * 120)
    nested.mkdir()
    long_file = nested / ("f" * 120 + ".txt")
    long_file.write_bytes(b"long-path-evidence")

    legacy_spelling = stage / nested.name / long_file.name
    assert len(str(legacy_spelling)) > 260
    # Legacy spelling depends on machine policy; extended I/O must work either way.

    entries = workflow_module._tree_entries_without_links(stage)
    assert long_file in entries
    workflow_module._seal_tree_files(stage)
    assert not long_file.stat().st_mode & stat.S_IWUSR

    workflow_module._remove_owned_tree(
        stage,
        parent,
        prefixes=(".lwr-stage-export-",),
    )
    assert not stage.exists()
