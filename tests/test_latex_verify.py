"""Bounded command, immutable-input and verification binding tests."""

from __future__ import annotations

import copy
import difflib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from latex_word_review.applier import apply_patch_plan
from latex_word_review.canonical import seal_envelope, sha256_bytes
from latex_word_review.contracts import (
    compute_payload_sha256,
    compute_source_tree_sha256,
    validate_contract,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import derive_artifact_id, derive_source_manifest_id
from latex_word_review.latex_verify import VerificationPolicy, verify_latex_project
from latex_word_review.ledger import build_ledger
from latex_word_review.planner import plan_patch
from latex_word_review.runtime import CommandResult
from tests.test_contracts import _golden_contracts
from tests.test_plan_apply import _changeset, _final_approval, _write_source


def _diff() -> bytes:
    return "".join(
        difflib.unified_diff(
            ["Hello\n"],
            ["carefully Hello\n"],
            fromfile="a/main.tex",
            tofile="b/main.tex",
            lineterm="\n",
        )
    ).encode()


def _workflow(tmp_path: Path) -> tuple[dict[str, dict[str, Any]], Path, Path, Path, Path]:
    contracts = _golden_contracts()
    plan = copy.deepcopy(contracts["PatchPlan"])
    diff = _diff()
    digest = sha256_bytes(diff)
    plan["payload"]["unified_diff"] = {
        "artifact_id": derive_artifact_id(digest),
        "path": "plan/changes.patch",
        "path_base": "run_root",
        "role": "unified_diff",
        "media_type": "text/x-diff",
        "size_bytes": len(diff),
        "sha256": digest,
        "immutable": True,
        "confidentiality": "derived_private",
    }
    contracts["PatchPlan"] = seal_envelope(plan)
    validate_contract(contracts["PatchPlan"])

    original = tmp_path / "original"
    revised = tmp_path / "revised"
    original.mkdir()
    revised.mkdir()
    (original / "main.tex").write_bytes(b"Hello\n")
    (revised / "main.tex").write_bytes(b"carefully Hello\n")
    returned = tmp_path / "returned.docx"
    returned.write_bytes(b"returned-original")
    output = tmp_path / "verified"
    return contracts, original, revised, returned, output


def _success_runner(calls: list[dict[str, Any]]) -> Any:
    def fake_run(
        executable: str | Path,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        max_output_bytes: int,
        environment: Mapping[str, str] | None,
    ) -> CommandResult:
        args = tuple(arguments)
        calls.append(
            {
                "executable": os.fspath(executable),
                "arguments": args,
                "cwd": cwd,
                "timeout_s": timeout_s,
                "max_output_bytes": max_output_bytes,
                "environment": dict(environment or {}),
            }
        )
        if os.fspath(executable).startswith("latexdiff"):
            stdout = "\\documentclass{article}\n\\begin{document}changed\\end{document}\n"
        else:
            out_argument = next(item for item in args if item.startswith("-outdir="))
            outdir = (cwd / out_argument.partition("=")[2]).resolve()
            outdir.mkdir(parents=True, exist_ok=True)
            pdf_name = "latexdiff.pdf" if args[-1].endswith("latexdiff.tex") else "main.pdf"
            (outdir / pdf_name).write_bytes(b"%PDF-1.4\nsynthetic\n")
            stdout = "compile ok"
        return CommandResult(
            returncode=0,
            stdout=stdout,
            stderr="",
            timed_out=False,
            output_truncated=False,
            duration_ms=1,
            output_sha256=sha256_bytes(stdout.encode()),
        )

    return fake_run


def _verify(
    workflow: tuple[dict[str, dict[str, Any]], Path, Path, Path, Path],
    **kwargs: Any,
) -> Any:
    contracts, original, revised, returned, output = workflow
    return verify_latex_project(
        original,
        revised,
        returned,
        contracts["SourceManifest"],
        contracts["ChangeSet"],
        contracts["ApprovalSet"],
        contracts["PatchPlan"],
        output,
        generated_at="2026-07-16T16:00:00+09:00",
        **kwargs,
    )


def test_verified_outputs_bind_every_authorization_hash_and_keep_inputs_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    contracts, original, revised, returned, output = workflow
    original_before = (original / "main.tex").read_bytes()
    revised_before = (revised / "main.tex").read_bytes()
    returned_before = returned.read_bytes()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("latex_word_review.latex_verify.run_command", _success_runner(calls))
    monkeypatch.setenv("SYNTHETIC_SECRET", "must-not-be-forwarded")

    result = _verify(workflow)

    assert result.status == "pass"
    validate_contract(result.report)
    extension = result.report["extensions"]["org.latex-word-review.verification"]
    assert (
        extension["source_manifest_payload_sha256"]
        == contracts["SourceManifest"]["integrity"]["payload_sha256"]
    )
    assert (
        extension["approval_set_payload_sha256"]
        == contracts["ApprovalSet"]["integrity"]["payload_sha256"]
    )
    assert (
        extension["patch_plan_payload_sha256"]
        == contracts["PatchPlan"]["integrity"]["payload_sha256"]
    )
    assert extension["applied_source_tree_sha256"] == result.revised_source_tree_sha256
    ledger = build_ledger(
        contracts["ChangeSet"],
        contracts["ApprovalSet"],
        contracts["PatchPlan"],
        result.report,
    )
    assert ledger.document["status"]["verification"] == "pass"
    assert b"applied_verified" in ledger.html_bytes
    assert result.report["payload"]["patch_reconciliation"] == {
        "planned_count": 1,
        "applied_count": 1,
        "missing_count": 0,
        "extra_count": 0,
        "duplicate_count": 0,
        "unapproved_modification_count": 0,
    }
    assert (output / "revised-clean/main.tex").read_bytes() == revised_before
    assert (output / "revised-clean.pdf").read_bytes().startswith(b"%PDF")
    assert (output / "latexdiff.tex").is_file()
    assert (output / "latexdiff.pdf").is_file()
    assert not (output / "_work").exists()
    assert (original / "main.tex").read_bytes() == original_before
    assert (revised / "main.tex").read_bytes() == revised_before
    assert returned.read_bytes() == returned_before
    assert len(calls) == 3
    assert all("SYNTHETIC_SECRET" not in call["environment"] for call in calls)
    assert all(not Path(cast("Path", call["cwd"])).is_relative_to(original) for call in calls)
    assert all(not Path(cast("Path", call["cwd"])).is_relative_to(revised) for call in calls)
    for call in calls:
        if str(call["executable"]).startswith("latexmk"):
            assert "-no-shell-escape" in call["arguments"]
            assert "-shell-escape" not in call["arguments"]
    logs = b"".join(path.read_bytes() for path in sorted((output / "logs").iterdir()))
    assert os.fspath(tmp_path).encode() not in logs


def test_executable_metacharacters_remain_one_shell_free_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("latex_word_review.latex_verify.run_command", _success_runner(calls))

    _verify(
        workflow,
        latexmk_executable="latexmk;touch injected",
        latexdiff_executable="latexdiff&&touch injected",
    )

    assert calls[0]["executable"] == "latexmk;touch injected"
    assert calls[1]["executable"] == "latexdiff&&touch injected"
    assert not (tmp_path / "injected").exists()


def test_real_planner_and_applier_outputs_verify_without_contract_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "source"
    source_tree = _write_source(original, "Hello\n")
    source_manifest = copy.deepcopy(_golden_contracts()["SourceManifest"])
    file_record = {
        "path": "main.tex",
        "role": "tex",
        "media_type": "text/x-tex",
        "size_bytes": 6,
        "sha256": sha256_bytes(b"Hello\n"),
        "encoding": "utf-8",
        "newline": "lf",
    }
    assert compute_source_tree_sha256([file_record]) == source_tree
    source_id = derive_source_manifest_id(source_tree)
    source_manifest["object_id"] = source_id
    source_manifest["payload"]["source_manifest_id"] = source_id
    source_manifest["payload"]["source_tree_sha256"] = source_tree
    source_manifest["payload"]["files"] = [file_record]
    source_manifest = seal_envelope(source_manifest)
    validate_contract(source_manifest)

    changeset = _changeset("Hello\n", [("insertion", 0, 0, "Careful ")])
    changeset["payload"]["source_manifest_sha256"] = compute_payload_sha256(source_manifest)
    changeset = seal_envelope(changeset)
    approval = _final_approval(changeset, [("accepted", None)])
    plan = plan_patch(
        original,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at="2026-07-16T15:00:00+09:00",
    )
    revised = tmp_path / "applied"
    apply_patch_plan(
        original,
        revised,
        patch_plan=plan.document,
        unified_diff=plan.unified_diff,
        changeset=changeset,
        approval=approval,
    )
    returned = tmp_path / "returned.docx"
    returned.write_bytes(b"returned-original")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("latex_word_review.latex_verify.run_command", _success_runner(calls))

    result = verify_latex_project(
        original,
        revised,
        returned,
        source_manifest,
        changeset,
        approval,
        plan.document,
        tmp_path / "verified-real-chain",
        generated_at="2026-07-16T16:00:00+09:00",
    )

    assert result.status == "pass"
    assert result.report["payload"]["patch_reconciliation"]["applied_count"] == 1
    assert (tmp_path / "verified-real-chain/revised-clean/main.tex").read_text(
        encoding="utf-8"
    ) == "Careful Hello\n"


def test_timeout_is_recorded_and_private_work_tree_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)

    def timeout_run(
        executable: str | Path,
        arguments: Sequence[str],
        **kwargs: Any,
    ) -> CommandResult:
        del executable, arguments, kwargs
        return CommandResult(
            returncode=-9,
            stdout="",
            stderr="timed out at C:/Users/synthetic/private/main.tex",
            timed_out=True,
            output_truncated=False,
            duration_ms=10,
            output_sha256=sha256_bytes(b"timeout"),
        )

    monkeypatch.setattr("latex_word_review.latex_verify.run_command", timeout_run)
    result = _verify(workflow, policy=VerificationPolicy(timeout_s=1))

    assert result.status == "fail"
    assert result.report["payload"]["compile"]["timed_out"] is True
    output = workflow[-1]
    assert output.is_dir()
    assert not (output / "_work").exists()
    assert b"C:/Users" not in (output / "logs/revised-compile.json").read_bytes()


def test_missing_latexdiff_is_explicitly_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    success = _success_runner([])

    def missing_latexdiff(
        executable: str | Path,
        arguments: Sequence[str],
        **kwargs: Any,
    ) -> CommandResult:
        if os.fspath(executable) == "latexdiff":
            raise ContractError(ErrorCode.TOOL_MISSING, "synthetic missing tool")
        return cast("CommandResult", success(executable, arguments, **kwargs))

    monkeypatch.setattr("latex_word_review.latex_verify.run_command", missing_latexdiff)
    result = _verify(workflow)

    assert result.status == "blocked"
    assert result.report["payload"]["latexdiff"]["status"] == "blocked"
    command = result.report["extensions"]["org.latex-word-review.verification"]["commands"][1]
    assert command["error_code"] == ErrorCode.TOOL_MISSING.value


def test_shell_escape_dependent_source_is_denied_before_any_command() -> None:
    from latex_word_review import latex_verify

    tree = latex_verify._SourceTree(
        files={"main.tex": b"\\immediate\\write18{touch owned}\n"},
        roles={"main.tex": "tex"},
        records=(),
        tree_sha256=sha256_bytes(b"tree"),
    )
    with pytest.raises(ContractError) as denied:
        latex_verify._reject_shell_escape_sources(tree)
    assert denied.value.code is ErrorCode.VERIFY_COMPILE_FAILED


def test_engine_hint_selects_xelatex_and_conflicts_fail_closed() -> None:
    from latex_word_review import latex_verify

    source = copy.deepcopy(_golden_contracts()["SourceManifest"])
    source["payload"]["engine_hints"] = [
        {"engine": "xelatex", "source": "main.tex", "value": "xelatex"}
    ]
    assert latex_verify._latexmk_mode(source) == "-xelatex"
    source["payload"]["engine_hints"].append(
        {"engine": "lualatex", "source": "main.tex", "value": "lualatex"}
    )
    with pytest.raises(ContractError) as raised:
        latex_verify._latexmk_mode(source)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_reference_check_uses_final_tex_log_not_transient_latexmk_warnings(
    tmp_path: Path,
) -> None:
    from latex_word_review import latex_verify

    build = tmp_path / "build"
    build.mkdir()
    (build / "main.log").write_text("Output written on main.pdf.\n", encoding="utf-8")
    transcript = "LaTeX Warning: There were undefined references on pass one."
    result = CommandResult(
        returncode=0,
        stdout=transcript,
        stderr="",
        timed_out=False,
        output_truncated=False,
        duration_ms=1,
        output_sha256=sha256_bytes(transcript.encode()),
    )
    run = latex_verify._ToolRun("latexmk-revised", "pass", result, None)

    final_log = latex_verify._final_compile_log_text(
        build,
        "main.tex",
        run,
        max_file_bytes=1024,
    )

    assert latex_verify._compile_checks(final_log, run.status) == ("pass", "pass")


def test_verification_replay_allows_adjacent_non_overlapping_operations(tmp_path: Path) -> None:
    from latex_word_review import latex_verify

    source = tmp_path / "source"
    source_tree = _write_source(source, "Hello\n")
    changeset = _changeset(
        "Hello\n",
        [
            ("replacement", 0, 2, "Hi"),
            ("replacement", 2, 5, "ya"),
        ],
    )
    approval = _final_approval(changeset, [("accepted", None), ("accepted", None)])
    plan = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at="2026-07-16T15:00:00+09:00",
    )
    original = latex_verify._SourceTree(
        files={"main.tex": b"Hello\n"},
        roles={"main.tex": "tex"},
        records=(),
        tree_sha256=source_tree,
    )

    revised = latex_verify._apply_plan(original, plan.document)

    assert revised["main.tex"] == b"Hiya\n"


def test_source_drift_and_resealed_plan_substitution_publish_nothing(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    contracts, _original, revised, _returned, output = workflow
    revised.joinpath("main.tex").write_bytes(b"carefully Hello!\n")
    with pytest.raises(ContractError) as drift:
        _verify(workflow)
    assert drift.value.code is ErrorCode.VERIFY_DIFF_MISMATCH
    assert not output.exists()

    revised.joinpath("main.tex").write_bytes(b"carefully Hello\n")
    forged = copy.deepcopy(contracts["PatchPlan"])
    forged["payload"]["approval_set_sha256"] = sha256_bytes(b"forged")
    forged = seal_envelope(forged)
    contracts["PatchPlan"] = forged
    with pytest.raises(ContractError) as binding:
        _verify(workflow)
    assert binding.value.code is ErrorCode.HASH_PATCHPLAN_MISMATCH
    assert not output.exists()


def test_staging_failure_rolls_back_and_keeps_authoritative_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(tmp_path)
    _contracts, original, revised, returned, output = workflow
    before = (
        (original / "main.tex").read_bytes(),
        (revised / "main.tex").read_bytes(),
        returned.read_bytes(),
    )
    from latex_word_review import latex_verify

    real_write = latex_verify._write_output_file
    writes = 0

    def fail_midway(path: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 5:
            raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "synthetic staging failure")
        real_write(path, data)

    monkeypatch.setattr(latex_verify, "_write_output_file", fail_midway)
    with pytest.raises(ContractError) as failure:
        _verify(workflow)
    assert failure.value.code is ErrorCode.APPLY_PARTIAL_WRITE
    assert not output.exists()
    assert not list(tmp_path.glob(".verified.verify-*"))
    assert (
        (original / "main.tex").read_bytes(),
        (revised / "main.tex").read_bytes(),
        returned.read_bytes(),
    ) == before


def test_returned_original_hash_binding_is_enforced(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    workflow[-2].write_bytes(b"tampered returned original")
    with pytest.raises(ContractError) as caught:
        _verify(workflow)
    assert caught.value.code is ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH
    assert not workflow[-1].exists()
