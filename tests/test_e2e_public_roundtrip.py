"""Public E0 full loop: Word revision -> approved LaTeX -> marked PDF -> audit ZIP."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from latex_word_review.applier import apply_patch_plan
from latex_word_review.approval import create_approval_set, finalize_approval_set, record_decision
from latex_word_review.backends import BackendRequest, Tex2WordBackend
from latex_word_review.bundle import BundleItem, create_audit_bundle, verify_audit_bundle
from latex_word_review.canonical import canonical_json
from latex_word_review.contracts import compute_payload_sha256, validate_contract
from latex_word_review.discovery import discover_project
from latex_word_review.export import ExportBindings, export_review_docx
from latex_word_review.hashing import digest_bytes, digest_file
from latex_word_review.ingest import archive_returned_docx
from latex_word_review.latex_verify import verify_latex_project
from latex_word_review.ledger import build_ledger
from latex_word_review.planner import plan_patch
from latex_word_review.revisions import build_changeset
from latex_word_review.runtime import CommandResult
from latex_word_review.snapshot import SNAPSHOT_MANIFEST, snapshot_project
from latex_word_review.workflow_objects import (
    bookmark_bindings_from_source_map,
    build_backend_capabilities_document,
    build_export_report_document,
    build_review_ir_document,
    build_revision_reader_capabilities_document,
    build_run_manifest_document,
    build_source_manifest_document,
    build_source_map_document,
)
from tests.e2e_helpers import create_whole_bookmark_replacement
from tests.test_ledger_bundle import _bundle_artifact

FIXTURE = Path(__file__).parent / "fixtures/e0-minimal-paper"
RUN_ID = "run_019b0000-0000-7000-8000-000000000001"
TIME = "2026-07-16T15:00:00+09:00"
REAL_LATEX_TOOLS = shutil.which("latexmk") is not None and shutil.which("latexdiff") is not None
_PUBLIC_REAL_TEX_LOGS = (
    ("logs/revised-compile.json", True),
    ("logs/latexdiff-generate.json", False),
    ("logs/latexdiff-compile.json", True),
)
_MAX_PUBLIC_DIAGNOSTIC_FILE_BYTES = 128 * 1024
_MAX_PUBLIC_DIAGNOSTIC_STREAM_CHARS = 2048
_WINDOWS_OR_UNC_ABSOLUTE = re.compile(r"(?i)(?<![\w.])(?:[a-z]:[\\/]|\\\\)[^\s\"'<>]*")
_POSIX_ABSOLUTE = re.compile(r"(?<![\w.])/(?:[^\s\"'<>]+)")


def _redact_public_diagnostic(value: str) -> str:
    sanitized = value.replace("\x00", "�")
    sanitized = _WINDOWS_OR_UNC_ABSOLUTE.sub("<absolute-path>", sanitized)
    return _POSIX_ABSOLUTE.sub("<absolute-path>", sanitized)


def _public_real_tex_log_tails(output_root: Path) -> dict[str, dict[str, Any]]:
    """Return bounded, defense-in-depth diagnostics for this public fixture only."""

    diagnostics: dict[str, dict[str, Any]] = {}
    safe_fields = (
        "name",
        "status",
        "exit_code",
        "timed_out",
        "output_truncated",
        "output_sha256",
        "error_code",
    )
    for relative, include_stdout in _PUBLIC_REAL_TEX_LOGS:
        path = output_root.joinpath(*relative.split("/"))
        try:
            if path.is_symlink() or path.stat().st_size > _MAX_PUBLIC_DIAGNOSTIC_FILE_BYTES:
                raise ValueError
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            diagnostics[relative] = {"diagnostic_status": "unavailable"}
            continue
        record: dict[str, Any] = {"diagnostic_status": "available"}
        for key in safe_fields:
            value = payload.get(key)
            if value is None or isinstance(value, (bool, int)):
                record[key] = value
            elif isinstance(value, str) and len(value) <= 200:
                record[key] = _redact_public_diagnostic(value)
        for stream in ("stderr", "stdout"):
            if stream == "stdout" and not include_stdout:
                continue
            value = payload.get(stream)
            if isinstance(value, str):
                record[f"{stream}_tail"] = _redact_public_diagnostic(value)[
                    -_MAX_PUBLIC_DIAGNOSTIC_STREAM_CHARS:
                ]
        diagnostics[relative] = record
    return diagnostics


def _format_public_verification_failure(summary: Mapping[str, Any], output_root: Path) -> str:
    failure = dict(summary)
    failure["public_real_tex_log_tails"] = _public_real_tex_log_tails(output_root)
    return json.dumps(failure, sort_keys=True)


def _fake_tools(calls: list[tuple[str, tuple[str, ...]]]) -> Any:
    def run(
        executable: str | Path,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        max_output_bytes: int,
        environment: Mapping[str, str] | None,
    ) -> CommandResult:
        del timeout_s, max_output_bytes
        name = str(executable)
        args = tuple(arguments)
        calls.append((name, args))
        if Path(name).stem.casefold() == "initexmf":
            assert environment is not None
            stdout = "\n".join(
                (
                    "SharedSetup: no",
                    "PathOkay: yes",
                    f"LinkTargetDirectory: {Path(name).parent}",
                    f"UserInstall: {environment['MIKTEX_USERINSTALL']}",
                    f"UserConfig: {environment['MIKTEX_USERCONFIG']}",
                    f"UserData: {environment['MIKTEX_USERDATA']}",
                )
            )
        elif "latexdiff" in name:
            stdout = (
                "\\documentclass{article}\n"
                "\\begin{document}\\DIFdel{defined}\\DIFadd{carefully defined}"
                "\\end{document}\n"
            )
        else:
            out_argument = next(item for item in args if item.startswith("-outdir="))
            outdir = (cwd / out_argument.partition("=")[2]).resolve()
            outdir.mkdir(parents=True, exist_ok=True)
            pdf_name = "latexdiff.pdf" if args[-1] == "./latexdiff.tex" else "main.pdf"
            (outdir / pdf_name).write_bytes(b"%PDF-1.4\npublic synthetic verification\n")
            stdout = "compile ok"
        return CommandResult(
            0,
            stdout,
            "",
            False,
            False,
            1,
            "sha256:" + "c" * 64,
        )

    return run


def test_public_real_tex_diagnostics_are_bounded_redacted_and_exclude_diff_source(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    common = {
        "error_code": None,
        "exit_code": 1,
        "name": "latexmk-revised",
        "output_sha256": "sha256:" + "a" * 64,
        "output_truncated": False,
        "status": "fail",
        "timed_out": True,
    }
    absolute_paths = " D:/public-workspace/main.tex /opt/public-workspace/main.tex"
    (logs / "revised-compile.json").write_text(
        json.dumps({**common, "stdout": "x" * 5000 + absolute_paths, "stderr": absolute_paths}),
        encoding="utf-8",
    )
    (logs / "latexdiff-generate.json").write_text(
        json.dumps(
            {
                **common,
                "name": "latexdiff-generate",
                "stdout": "DO NOT EMIT GENERATED LATEX SOURCE",
                "stderr": absolute_paths,
            }
        ),
        encoding="utf-8",
    )
    (logs / "latexdiff-compile.json").write_text(
        json.dumps({**common, "name": "latexmk-latexdiff", "stdout": "bounded", "stderr": ""}),
        encoding="utf-8",
    )

    diagnostic = _public_real_tex_log_tails(tmp_path)
    serialized = json.dumps(diagnostic, sort_keys=True)

    assert len(serialized) < 10_000
    assert "D:/public-workspace" not in serialized
    assert "/opt/public-workspace" not in serialized
    assert "DO NOT EMIT GENERATED LATEX SOURCE" not in serialized
    assert (
        len(diagnostic["logs/revised-compile.json"]["stdout_tail"])
        <= _MAX_PUBLIC_DIAGNOSTIC_STREAM_CHARS
    )


def test_public_real_tex_diagnostics_fail_closed_when_logs_are_unavailable(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latexdiff-generate.json").write_text("{", encoding="utf-8")
    (logs / "latexdiff-compile.json").write_text(
        "x" * (_MAX_PUBLIC_DIAGNOSTIC_FILE_BYTES + 1), encoding="utf-8"
    )

    diagnostic = _public_real_tex_log_tails(tmp_path)

    assert set(diagnostic) == {relative for relative, _ in _PUBLIC_REAL_TEX_LOGS}
    assert all(value == {"diagnostic_status": "unavailable"} for value in diagnostic.values())


@pytest.mark.parametrize(
    "use_real_latex_tools",
    [
        pytest.param(False, id="bounded-command-double"),
        pytest.param(
            True,
            id="installed-latexmk-latexdiff",
            marks=pytest.mark.skipif(
                not REAL_LATEX_TOOLS,
                reason="latexmk and latexdiff are not both installed",
            ),
        ),
    ],
)
def test_public_roundtrip_produces_clean_marked_and_ledger_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_real_latex_tools: bool,
) -> None:
    origin = tmp_path / "authoritative-origin"
    shutil.copytree(FIXTURE / "source", origin)
    origin_before = {
        path.relative_to(origin): path.read_bytes() for path in origin.rglob("*") if path.is_file()
    }
    run_root = tmp_path / "run"
    snapshot = run_root / "snapshot"
    snapshot_project(origin, snapshot, main_document="main.tex")
    discovery = discover_project(snapshot, main_document="main.tex")
    source_manifest = build_source_manifest_document(
        discovery,
        run_id=RUN_ID,
        snapshot_manifest_file=snapshot / SNAPSHOT_MANIFEST,
        snapshot_artifact_path="snapshot/snapshot-manifest.json",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    source_sha = compute_payload_sha256(source_manifest)

    exported = run_root / "export/review.docx"
    outcome = export_review_docx(
        Tex2WordBackend(),
        BackendRequest(snapshot, "main.tex", exported),
        discovery,
        ExportBindings(source_sha, "export/review.docx", "public_fixture"),
    )
    assert outcome.output_path == exported
    assert outcome.anchoring is not None
    export_capabilities = build_backend_capabilities_document(
        outcome.backend_result.capabilities,
        run_id=RUN_ID,
        generated_at=TIME,
    )
    review_ir = build_review_ir_document(
        discovery,
        outcome,
        run_id=RUN_ID,
        source_manifest_sha256=source_sha,
        generated_at=TIME,
    )
    source_map = build_source_map_document(
        outcome.anchoring,
        run_id=RUN_ID,
        source_manifest_sha256=source_sha,
        review_ir_sha256=compute_payload_sha256(review_ir),
        generated_at=TIME,
        export_report_payload=outcome.report.as_payload(),
    )
    export_report = build_export_report_document(outcome, run_id=RUN_ID, generated_at=TIME)
    exact_mapping: dict[str, Any] | None = None
    before = ""
    for candidate in cast("list[dict[str, Any]]", source_map["payload"]["mappings"]):
        if candidate["status"] != "exact":
            continue
        location = cast("dict[str, Any]", candidate["source_location"])
        source_file = snapshot / cast("str", location["path"])
        source_bytes = source_file.read_bytes()
        start = cast("int", location["start_byte"])
        end = cast("int", location["end_byte"])
        candidate_text = source_bytes[start:end].decode("utf-8")
        if " is " in candidate_text:
            exact_mapping = candidate
            before = candidate_text
            break
    assert exact_mapping is not None
    after = before.replace(" is ", " is carefully ", 1)
    bookmark_name = cast("str", cast("dict[str, Any]", exact_mapping["docx_anchor"])["name"])

    received = run_root / "received/synthetic-reviewed.docx"
    create_whole_bookmark_replacement(
        exported,
        received,
        bookmark_name=bookmark_name,
        before=before,
        after=after,
    )
    received_before = received.read_bytes()
    exported_hash = cast("str", source_map["payload"]["review_docx_sha256"])
    archive = archive_returned_docx(
        received,
        run_root / "returned",
        run_id=RUN_ID,
        exported_docx_sha256=exported_hash,
        confidentiality="public_fixture",
    )
    reader_capabilities = build_revision_reader_capabilities_document(
        run_id=RUN_ID,
        generated_at=TIME,
    )
    changeset = build_changeset(
        archive.docx_path,
        export_baseline_path=exported,
        export_baseline_sha256=exported_hash,
        run_id=RUN_ID,
        source_manifest_sha256=source_sha,
        source_map_sha256=compute_payload_sha256(source_map),
        revision_reader_capabilities_sha256=compute_payload_sha256(reader_capabilities),
        returned_artifact_path="returned/returned-original.docx",
        confidentiality="public_fixture",
        bookmark_bindings=bookmark_bindings_from_source_map(source_map),
        generated_at=TIME,
    )
    changes = cast("list[dict[str, Any]]", changeset["payload"]["changes"])
    assert len(changes) == 1
    change = changes[0]
    assert (change["kind"], change["before"], change["after"]) == (
        "replacement",
        before,
        after,
    )
    assert change["resolution"] == {
        "status": "exact",
        "method": "bookmark",
        "confidence": 1.0,
        "candidates": [],
    }
    assert change["safety_class"] == "plain_text_candidate"

    actor = {"id": "fixture-author", "display_name": "Public Fixture Author"}
    approval = create_approval_set(changeset, decided_by=actor, generated_at=TIME)
    approval = record_decision(
        changeset,
        approval,
        change_id=cast("str", change["change_id"]),
        decision="accepted",
        decision_source="cli",
        decided_at=TIME,
        generated_at=TIME,
    )
    approval = finalize_approval_set(changeset, approval, generated_at=TIME)
    plan = plan_patch(
        snapshot,
        source_tree_sha256=discovery.source_tree_sha256,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
        confidentiality="public_fixture",
    )
    assert plan.document["payload"]["status"] == "ready"
    revised = run_root / "revised"
    apply_patch_plan(
        snapshot,
        revised,
        patch_plan=plan.document,
        unified_diff=plan.unified_diff,
        changeset=changeset,
        approval=approval,
    )
    revised_bytes = (revised / cast("str", location["path"])).read_bytes()
    assert revised_bytes[start : start + len(after.encode())].decode("utf-8") == after

    calls: list[tuple[str, tuple[str, ...]]] = []
    if not use_real_latex_tools:
        monkeypatch.setattr("latex_word_review.latex_verify.run_command", _fake_tools(calls))
    verification = verify_latex_project(
        snapshot,
        revised,
        archive.docx_path,
        source_manifest,
        changeset,
        approval,
        plan.document,
        run_root / "verification",
        generated_at=TIME,
    )
    verification_payload = cast("Mapping[str, Any]", verification.report["payload"])
    failure_summary = {
        "compile": verification_payload["compile"],
        "diagnostics": verification_payload["diagnostics"],
        "latexdiff": verification_payload["latexdiff"],
        "references": verification_payload["references"],
        "status": verification_payload["status"],
    }
    assert verification.status == "pass", _format_public_verification_failure(
        failure_summary, verification.output_root
    )
    if not use_real_latex_tools:
        assert any("-xelatex" in arguments for _, arguments in calls)
    assert b"DIFadd" in (verification.output_root / "latexdiff.tex").read_bytes()
    ledger = build_ledger(changeset, approval, plan.document, verification.report)
    assert ledger.document["changes"][0]["status"] == "applied_verified"
    assert ledger.document["changes"][0]["author"] == "Synthetic Roundtrip Reviewer"

    delivery = run_root / "delivery"
    delivery.mkdir()
    for source, name in (
        (verification.output_root / "verification-report.json", "verification-report.json"),
        (verification.output_root / "actual.diff", "actual.diff"),
        (verification.output_root / "latexdiff.tex", "latexdiff.tex"),
        (verification.output_root / "latexdiff.pdf", "latexdiff.pdf"),
        (verification.output_root / "revised-clean.pdf", "revised-clean.pdf"),
    ):
        shutil.copyfile(source, delivery / name)
    (delivery / "ledger.json").write_bytes(ledger.json_bytes)
    (delivery / "ledger.html").write_bytes(ledger.html_bytes)
    ledger_artifacts = (
        _bundle_artifact("ledger.html", "review_ledger_html", ledger.html_bytes, "text/html"),
        _bundle_artifact(
            "ledger.json",
            "review_ledger_json",
            ledger.json_bytes,
            "application/json",
        ),
    )
    run_manifest = build_run_manifest_document(
        source_manifest,
        objects=[
            review_ir,
            source_map,
            export_report,
            changeset,
            approval,
            plan.document,
            verification.report,
        ],
        backend_capabilities=[export_capabilities, reader_capabilities],
        artifacts=ledger_artifacts,
        generated_at=TIME,
    )
    (delivery / "run-manifest.json").write_bytes(canonical_json(run_manifest) + b"\n")
    items = [
        BundleItem(
            "verification-report.json",
            "verification_report",
            verification.report,
            "$document",
        ),
        BundleItem("actual.diff", "actual_diff", verification.report),
        BundleItem("latexdiff.tex", "latexdiff_tex", verification.report),
        BundleItem("latexdiff.pdf", "latexdiff_pdf", verification.report),
        BundleItem("revised-clean.pdf", "revised_clean_pdf", verification.report),
        BundleItem("ledger.json", "review_ledger_json", run_manifest),
        BundleItem("ledger.html", "review_ledger_html", run_manifest),
        BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"),
    ]
    bundle = create_audit_bundle(
        delivery,
        run_root / "audit.zip",
        allowlist=items,
        run_manifest=run_manifest,
        verification_report=verification.report,
        content_classification="public_fixture",
        generated_at=TIME,
    )
    assert validate_contract(bundle.document).schema_name == "AuditBundle"
    verified_bundle = verify_audit_bundle(
        bundle.path,
        expected_run_manifest=run_manifest,
        expected_verification_report=verification.report,
    )
    assert verified_bundle.file_sha256 == bundle.file_sha256
    assert (
        digest_file(received, max_bytes=128 * 1024 * 1024).sha256
        == digest_bytes(received_before).sha256
    )
    assert received.read_bytes() == received_before
    assert {
        path.relative_to(origin): path.read_bytes() for path in origin.rglob("*") if path.is_file()
    } == origin_before
