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


@pytest.fixture(autouse=True)
def _make_default_tool_resolution_host_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most verifier tests use a fake runner and must not depend on host TeX."""

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        "latex_word_review.latex_verify._common_miktex_candidates",
        lambda _name: (),
    )


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
        if Path(os.fspath(executable)).stem.casefold().startswith("latexdiff"):
            stdout = (
                "\\documentclass{article}\n\\begin{document}"
                "\\DIFdel{Hello}\\DIFadd{carefully Hello}\\end{document}\n"
            )
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
    miktex_roots = {
        "MIKTEX_USERCONFIG": os.fspath(tmp_path / "miktex-config"),
        "MIKTEX_USERDATA": os.fspath(tmp_path / "miktex-data"),
        "MIKTEX_USERINSTALL": os.fspath(tmp_path / "miktex-install"),
    }
    for name, value in miktex_roots.items():
        monkeypatch.setenv(name, value)

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
    assert "--flatten" not in calls[1]["arguments"]
    assert calls[1]["arguments"][-2:] == (
        "latexdiff-input/original.tex",
        "latexdiff-input/revised.tex",
    )
    assert all("SYNTHETIC_SECRET" not in call["environment"] for call in calls)
    assert all(call["environment"]["NoDefaultCurrentDirectoryInExePath"] == "1" for call in calls)
    assert all(Path(call["environment"]["HOME"]).is_relative_to(tmp_path) for call in calls)
    assert all(
        all(call["environment"].get(name) != value for name, value in miktex_roots.items())
        for call in calls
    )
    assert all(not Path(cast("Path", call["cwd"])).is_relative_to(original) for call in calls)
    assert all(not Path(cast("Path", call["cwd"])).is_relative_to(revised) for call in calls)
    for call in calls:
        if str(call["executable"]).startswith("latexmk"):
            assert "-norc" in call["arguments"]
            assert "-disable-installer" not in call["arguments"]
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
    for name in (
        "revised-compile.json",
        "latexdiff-generate.json",
        "latexdiff-compile.json",
    ):
        assert b"C:/Users" not in (output / "logs" / name).read_bytes()


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
        if Path(os.fspath(executable)).stem.casefold() == "latexdiff":
            raise ContractError(ErrorCode.TOOL_MISSING, "synthetic missing tool")
        return cast("CommandResult", success(executable, arguments, **kwargs))

    monkeypatch.setattr("latex_word_review.latex_verify.run_command", missing_latexdiff)
    result = _verify(workflow)

    assert result.status == "blocked"
    assert result.report["payload"]["latexdiff"]["status"] == "blocked"
    command = result.report["extensions"]["org.latex-word-review.verification"]["commands"][1]
    assert command["error_code"] == ErrorCode.TOOL_MISSING.value


def test_static_latexdiff_flattening_uses_sealed_unicode_include_edges() -> None:
    from latex_word_review import latex_verify

    tree = latex_verify._SourceTree(
        files={
            "main.tex": (
                "% \\input{章节/方法}\n\\input{章节/方法}\n\\begin{document}正文\\end{document}\n"
            ).encode(),
            "章节/方法.tex": "方法正文。\\input{片段}\n".encode(),
            "片段.tex": "嵌套片段。\n".encode(),
        },
        roles={"main.tex": "tex", "章节/方法.tex": "tex", "片段.tex": "tex"},
        records=(),
        tree_sha256=sha256_bytes(b"synthetic-tree"),
    )
    source_manifest = {
        "payload": {
            "dependency_edges": [
                {"from": "main.tex", "kind": "input", "to": "章节/方法.tex"},
                {"from": "章节/方法.tex", "kind": "input", "to": "片段.tex"},
            ]
        }
    }

    flattened = latex_verify._flatten_latexdiff_source(
        tree,
        source_manifest,
        "main.tex",
        max_bytes=4096,
    ).decode()

    assert "% \\input{章节/方法}" in flattened
    assert flattened.count("方法正文。") == 1
    assert flattened.count("嵌套片段。") == 1
    assert "\\input{片段}" not in flattened


def test_latexdiff_marker_gate_ignores_only_preamble_definitions() -> None:
    from latex_word_review import latex_verify

    definitions_only = (
        b"\\providecommand{\\DIFadd}[1]{#1} %DIF PREAMBLE\n"
        b"\\providecommand{\\DIFdel}[1]{} %DIF PREAMBLE\n"
        b"\\begin{document}unchanged\\end{document}\n"
    )
    assert latex_verify._latexdiff_has_change_markers(definitions_only) is False
    assert (
        latex_verify._latexdiff_has_change_markers(
            b"\\begin{document}\\DIFaddbegin added\\DIFaddend\\end{document}\n"
        )
        is True
    )


def test_nonempty_patch_without_actual_latexdiff_markers_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow(tmp_path)
    calls: list[dict[str, Any]] = []
    success = _success_runner(calls)

    def definitions_only_runner(
        executable: str | Path,
        arguments: Sequence[str],
        **kwargs: Any,
    ) -> CommandResult:
        result = cast("CommandResult", success(executable, arguments, **kwargs))
        if Path(os.fspath(executable)).stem.casefold().startswith("latexdiff"):
            stdout = (
                "\\providecommand{\\DIFadd}[1]{#1} %DIF PREAMBLE\n"
                "\\begin{document}unchanged\\end{document}\n"
            )
            return CommandResult(
                returncode=0,
                stdout=stdout,
                stderr="included files were not expanded",
                timed_out=False,
                output_truncated=False,
                duration_ms=1,
                output_sha256=sha256_bytes(stdout.encode()),
            )
        return result

    monkeypatch.setattr("latex_word_review.latex_verify.run_command", definitions_only_runner)
    result = _verify(workflow)

    assert result.status == "fail"
    assert result.report["payload"]["latexdiff"]["status"] == "fail"
    assert not (workflow[-1] / "latexdiff.tex").exists()
    assert len(calls) == 2


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


def test_miktex_installer_prompt_is_disabled_only_for_complete_isolated_roots() -> None:
    from latex_word_review import latex_verify

    complete = {name: f"C:/isolated/{name}" for name in latex_verify._MIKTEX_ROOT_ENVIRONMENT}
    assert latex_verify._miktex_latexmk_arguments(complete) == ("-disable-installer",)
    incomplete = dict(complete)
    incomplete.pop("MIKTEX_USERDATA")
    assert latex_verify._miktex_latexmk_arguments(incomplete) == ()


def _synthetic_miktex_install(root: Path) -> tuple[Path, Path]:
    config = root / "miktex" / "config"
    binary = root / "miktex" / "bin" / "x64"
    config.mkdir(parents=True)
    binary.mkdir(parents=True)
    for name in ("scripts.ini", "packages.ini", "mpm.ini", "package-manifests.ini"):
        (config / name).write_text(f"synthetic {name}\n", encoding="utf-8")
    for name in (
        "initexmf.exe",
        "latexmk.exe",
        "latexdiff.exe",
        "pdflatex.exe",
        "xelatex.exe",
        "lualatex.exe",
        "bibtex.exe",
        "biber.exe",
        "makeindex.exe",
    ):
        (binary / name).write_bytes(f"synthetic {name}\n".encode())
    return binary / "latexmk.exe", binary / "latexdiff.exe"


def _set_synthetic_perl(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = root / "synthetic-perl"
    binary.mkdir()
    perl = binary / "perl.exe"
    perl.write_bytes(b"synthetic perl executable")
    monkeypatch.setenv("PATH", os.fspath(binary))
    return perl


def test_windows_miktex_toolchain_uses_private_state_and_ignores_inherited_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from latex_word_review import latex_verify

    install = tmp_path / "installed"
    latexmk, latexdiff = _synthetic_miktex_install(install)
    perl = _set_synthetic_perl(tmp_path, monkeypatch)
    outside = tmp_path / "inherited-outside"
    for name in latex_verify._MIKTEX_ROOT_ENVIRONMENT:
        monkeypatch.setenv(name, os.fspath(outside / name))
    work = tmp_path / "work"
    work.mkdir()

    toolchain = latex_verify._prepare_tex_toolchain(work, latexmk, latexdiff, "-xelatex")

    additions = toolchain.environment_additions
    assert toolchain.latexmk_arguments[0] == "-disable-installer"
    assert any(argument.startswith("-xelatex=") for argument in toolchain.latexmk_arguments)
    assert Path(additions["MIKTEX_USERINSTALL"]) == install.resolve()
    for name in ("MIKTEX_USERCONFIG", "MIKTEX_USERDATA", "HOME", "USERPROFILE"):
        value = Path(additions[name])
        assert value.is_relative_to(work)
        assert not value.is_relative_to(outside)
    assert toolchain.temp_root.is_relative_to(work)
    assert additions["PATH"].split(os.pathsep)[0] == os.fspath(latexmk.parent)
    assert len(toolchain.installation_state) == 12
    assert any(item.path == perl.resolve() for item in toolchain.installation_state)


def test_windows_default_tools_fall_back_to_standard_local_miktex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from latex_word_review import latex_verify

    install = tmp_path / "local" / "Programs" / "MiKTeX"
    latexmk, latexdiff = _synthetic_miktex_install(install)
    _set_synthetic_perl(tmp_path, monkeypatch)
    monkeypatch.setattr(
        latex_verify,
        "_common_miktex_candidates",
        lambda name: (latexmk if name == "latexmk" else latexdiff,),
    )
    work = tmp_path / "work"
    work.mkdir()

    toolchain = latex_verify._prepare_tex_toolchain(work, "latexmk", "latexdiff", "-xelatex")

    assert Path(toolchain.latexmk_executable) == latexmk.resolve()
    assert Path(toolchain.latexdiff_executable) == latexdiff.resolve()
    assert toolchain.latexmk_arguments[0] == "-disable-installer"


def test_windows_miktex_toolchain_rejects_mixed_or_incomplete_installations(
    tmp_path: Path,
) -> None:
    from latex_word_review import latex_verify

    install = tmp_path / "installed"
    _latexmk, latexdiff = _synthetic_miktex_install(install)
    texlive_latexmk = tmp_path / "texlive" / "bin" / "windows" / "latexmk.exe"
    texlive_latexmk.parent.mkdir(parents=True)
    texlive_latexmk.write_bytes(b"synthetic texlive latexmk")
    mixed_work = tmp_path / "mixed-work"
    mixed_work.mkdir()
    with pytest.raises(ContractError) as mixed:
        latex_verify._prepare_tex_toolchain(
            mixed_work,
            texlive_latexmk,
            latexdiff,
            "-xelatex",
        )
    assert mixed.value.code is ErrorCode.TOOL_VERSION_UNSUPPORTED

    incomplete = tmp_path / "fake" / "miktex" / "bin" / "x64" / "latexmk.exe"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_bytes(b"not a complete install")
    incomplete_work = tmp_path / "incomplete-work"
    incomplete_work.mkdir()
    with pytest.raises(ContractError) as invalid:
        latex_verify._prepare_tex_toolchain(
            incomplete_work,
            incomplete,
            tmp_path / "missing-latexdiff.exe",
            "-xelatex",
        )
    assert invalid.value.code is ErrorCode.TOOL_VERSION_UNSUPPORTED


def test_windows_miktex_installation_drift_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from latex_word_review import latex_verify

    install = tmp_path / "installed"
    latexmk, latexdiff = _synthetic_miktex_install(install)
    _set_synthetic_perl(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    toolchain = latex_verify._prepare_tex_toolchain(work, latexmk, latexdiff, "-xelatex")
    (install / "miktex" / "config" / "packages.ini").write_text(
        "tampered inventory\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError) as drift:
        latex_verify._verify_miktex_installation_state(toolchain)
    assert drift.value.code is ErrorCode.VERIFY_COMPILE_FAILED


def test_windows_texlive_paths_are_explicitly_unsupported(tmp_path: Path) -> None:
    from latex_word_review import latex_verify

    binary = tmp_path / "texlive" / "bin" / "windows"
    binary.mkdir(parents=True)
    latexmk = binary / "latexmk.exe"
    latexdiff = binary / "latexdiff.exe"
    latexmk.write_bytes(b"synthetic texlive latexmk")
    latexdiff.write_bytes(b"synthetic texlive latexdiff")
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(ContractError) as unsupported:
        latex_verify._prepare_tex_toolchain(work, latexmk, latexdiff, "-xelatex")
    assert unsupported.value.code is ErrorCode.TOOL_VERSION_UNSUPPORTED


def test_windows_relative_tool_path_is_rejected(tmp_path: Path) -> None:
    from latex_word_review import latex_verify

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ContractError) as invalid:
        latex_verify._prepare_tex_toolchain(
            work,
            Path("tools/latexmk.exe"),
            "latexdiff",
            "-xelatex",
        )
    assert invalid.value.code is ErrorCode.SCHEMA_INVALID


def test_windows_runtime_does_not_resolve_bare_tools_from_command_cwd(tmp_path: Path) -> None:
    from latex_word_review import runtime

    planted = tmp_path / "latexmk.exe"
    planted.write_bytes(b"must not be selected")
    with pytest.raises(ContractError) as missing:
        runtime._resolve_windows_executable(
            "latexmk",
            cwd=tmp_path,
            environment={"PATH": "", "PATHEXT": ".EXE"},
        )
    assert missing.value.code is ErrorCode.TOOL_MISSING

    environment = runtime.minimal_environment(temp_root=tmp_path)
    assert environment["NoDefaultCurrentDirectoryInExePath"] == "1"


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
