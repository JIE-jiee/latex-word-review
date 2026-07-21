"""Focused fail-closed branch coverage for LaTeX verification helpers."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import latex_word_review.latex_verify as verify_module
from latex_word_review.canonical import sha256_bytes
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest
from latex_word_review.latex_verify import (
    VerificationPolicy,
    _apply_plan,
    _command_log,
    _common_miktex_candidates,
    _compile_checks,
    _final_compile_log_text,
    _find_on_windows_path,
    _flatten_latexdiff_source,
    _input_reference_candidates,
    _installation_file_state,
    _InstallationFileState,
    _isolated_miktex_path,
    _latexdiff_has_change_markers,
    _miktex_bound_latexmk_arguments,
    _miktex_identity,
    _overall_status,
    _perl_command_assignment,
    _read_original_tree,
    _read_revised_tree,
    _reconcile,
    _reject_revised_extras,
    _remove_owned_tree,
    _require_equal,
    _require_schema,
    _require_time,
    _resolve_regular_tool,
    _safe_windows_search_path,
    _sanitize_log_text,
    _SourceTree,
    _TexToolchain,
    _tool_run,
    _ToolRun,
    _unified_diff,
    _validate_bindings,
    _validate_plan_approval_partition,
    _verify_miktex_installation_state,
    verify_latex_project,
)
from latex_word_review.runtime import CommandResult


def _assert_code(caught: pytest.ExceptionInfo[ContractError], code: ErrorCode) -> None:
    assert caught.value.code is code


def _tree(data: bytes = b"a", *, role: str = "tex") -> _SourceTree:
    return _SourceTree(
        files={"main.tex": data},
        roles={"main.tex": role},
        records=(),
        tree_sha256="tree-sha",
    )


def _operation(data: bytes = b"a") -> dict[str, Any]:
    return {
        "change_id": "c1",
        "kind": "replace",
        "target": {
            "path": "main.tex",
            "start_byte": 0,
            "end_byte": len(data),
            "slice_sha256": sha256_bytes(data),
        },
        "target_file_sha256": sha256_bytes(data),
        "expected_bytes_sha256": sha256_bytes(data),
        "before_text": data.decode("utf-8", errors="replace"),
        "replacement_text": "b",
        "unit_id": "u1",
    }


def _partition() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    change = {
        "change_id": "c1",
        "change_fingerprint": "fingerprint",
        "safety_class": "plain_text_candidate",
        "kind": "replacement",
        "resolution": {"status": "exact", "confidence": 1.0, "candidates": []},
        "source_location": {"path": "main.tex", "start_byte": 0, "end_byte": 1},
        "unit_id": "u1",
        "before": "a",
        "after": "b",
    }
    decision = {
        "change_id": "c1",
        "change_fingerprint": "fingerprint",
        "decision": "accepted",
        "final_text": None,
    }
    operation = {
        "change_id": "c1",
        "target": copy.deepcopy(change["source_location"]),
        "unit_id": "u1",
        "before_text": "a",
        "replacement_text": "b",
    }
    return {"changes": [change]}, {"decisions": [decision]}, {"operations": [operation]}


def _result(
    *,
    returncode: int = 0,
    timed_out: bool = False,
    output_truncated: bool = False,
    stdout: str = "ok",
    stderr: str = "",
) -> CommandResult:
    return CommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        output_truncated=output_truncated,
        duration_ms=1,
        output_sha256=sha256_bytes((stdout + stderr).encode()),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_s": 0.0},
        {"timeout_s": 61.0},
        {"max_output_bytes": 0},
        {"max_output_bytes": 16 * 1024 * 1024 + 1},
        {"max_file_bytes": 0},
        {"max_file_bytes": 512 * 1024 * 1024 + 1},
    ],
)
def test_verification_policy_rejects_unbounded_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ContractError) as caught:
        VerificationPolicy(**kwargs)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)


def test_time_schema_and_equality_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ContractError) as caught:
        _require_time("not-a-time")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(ContractError) as caught:
        _require_time("2026-07-21T12:00:00")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    monkeypatch.setattr(
        verify_module,
        "validate_contract",
        lambda _document: SimpleNamespace(schema_name="Other", payload_sha256="sha"),
    )
    with pytest.raises(ContractError) as caught:
        _require_schema({}, "SourceManifest")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    with pytest.raises(ContractError) as caught:
        _require_equal("expected", "actual", code=ErrorCode.HASH_SOURCE_MISMATCH, label="x")
    _assert_code(caught, ErrorCode.HASH_SOURCE_MISMATCH)


def test_binding_guards_reject_run_finality_and_blocked_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = {
        "SourceManifest": "source",
        "ChangeSet": "changes",
        "ApprovalSet": "approval",
        "PatchPlan": "plan",
    }
    monkeypatch.setattr(verify_module, "_require_schema", lambda _doc, name: hashes[name])
    source = {"run_id": "run", "payload": {"source_tree_sha256": "tree"}}
    changes = {
        "run_id": "run",
        "payload": {"source_manifest_sha256": "source", "changes": []},
    }
    approval = {
        "run_id": "run",
        "payload": {
            "source_manifest_sha256": "source",
            "changeset_sha256": "changes",
            "status": "final",
            "decisions": [],
        },
    }
    plan = {
        "run_id": "other",
        "payload": {
            "source_manifest_sha256": "source",
            "source_tree_sha256": "tree",
            "changeset_sha256": "changes",
            "approval_set_sha256": "approval",
            "status": "ready",
            "operations": [],
        },
    }
    with pytest.raises(ContractError) as caught:
        _validate_bindings(source, changes, approval, plan)
    _assert_code(caught, ErrorCode.HASH_PATCHPLAN_MISMATCH)

    plan["run_id"] = "run"
    cast("dict[str, Any]", approval["payload"])["status"] = "draft"
    with pytest.raises(ContractError) as caught:
        _validate_bindings(source, changes, approval, plan)
    _assert_code(caught, ErrorCode.APPROVAL_NOT_FINAL)

    cast("dict[str, Any]", approval["payload"])["status"] = "final"
    cast("dict[str, Any]", plan["payload"])["status"] = "blocked"
    with pytest.raises(ContractError) as caught:
        _validate_bindings(source, changes, approval, plan)
    _assert_code(caught, ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_decision", ErrorCode.HASH_APPROVAL_MISMATCH),
        ("fingerprint", ErrorCode.HASH_APPROVAL_MISMATCH),
        ("missing_operation", ErrorCode.PATCH_UNAPPROVED_CHANGE),
        ("unsafe", ErrorCode.PATCH_UNSAFE_KIND),
        ("target", ErrorCode.HASH_PATCHPLAN_MISMATCH),
        ("unit", ErrorCode.HASH_PATCHPLAN_MISMATCH),
        ("before", ErrorCode.HASH_PATCHPLAN_MISMATCH),
        ("edited_text", ErrorCode.PATCH_UNAPPROVED_CHANGE),
    ],
)
def test_plan_approval_partition_rejects_authority_drift(
    mutation: str,
    expected: ErrorCode,
) -> None:
    changes, approval, plan = _partition()
    change = changes["changes"][0]
    decision = approval["decisions"][0]
    operation = plan["operations"][0]
    if mutation == "missing_decision":
        approval["decisions"] = []
    elif mutation == "fingerprint":
        decision["change_fingerprint"] = "wrong"
    elif mutation == "missing_operation":
        plan["operations"] = []
    elif mutation == "unsafe":
        change["resolution"]["confidence"] = 0.5
    elif mutation == "target":
        operation["target"] = {"path": "other.tex"}
    elif mutation == "unit":
        operation["unit_id"] = "other"
    elif mutation == "before":
        operation["before_text"] = "wrong"
    else:
        decision["decision"] = "accepted_with_edit"
        decision["final_text"] = "edited"

    with pytest.raises(ContractError) as caught:
        _validate_plan_approval_partition(changes, approval, plan)
    _assert_code(caught, expected)


def test_original_and_revised_tree_guards_detect_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "main.tex").write_bytes(b"actual")
    manifest = {
        "payload": {
            "files": [{"path": "main.tex", "role": "tex", "size_bytes": 1, "sha256": "0" * 64}],
            "source_tree_sha256": "0" * 64,
        }
    }
    with pytest.raises(ContractError) as caught:
        _read_original_tree(tmp_path, manifest, max_file_bytes=1024)
    _assert_code(caught, ErrorCode.HASH_SOURCE_MISMATCH)

    original = _tree()

    def missing_file(_root: Path, _relative: str) -> Path:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "missing")

    monkeypatch.setattr(verify_module, "resolve_within", missing_file)
    with pytest.raises(ContractError) as caught:
        _read_revised_tree(tmp_path, original, max_file_bytes=1024)
    _assert_code(caught, ErrorCode.VERIFY_DIFF_MISMATCH)


def test_revised_tree_rejects_link_and_unallowlisted_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_like = tmp_path / "linked.tex"
    link_like.write_text("x", encoding="utf-8")
    monkeypatch.setattr(verify_module, "_is_link_or_junction", lambda path: path == link_like)
    with pytest.raises(ContractError) as caught:
        _reject_revised_extras(tmp_path, set())
    _assert_code(caught, ErrorCode.PATH_LINK_ESCAPE)

    monkeypatch.setattr(verify_module, "_is_link_or_junction", lambda _path: False)
    with pytest.raises(ContractError) as caught:
        _reject_revised_extras(tmp_path, set())
    _assert_code(caught, ErrorCode.VERIFY_DIFF_MISMATCH)


def test_apply_plan_rejects_non_tex_digest_range_and_kind_drift() -> None:
    operation = _operation()
    with pytest.raises(ContractError) as caught:
        _apply_plan(_tree(role="asset"), {"payload": {"operations": [operation]}})
    _assert_code(caught, ErrorCode.PATCH_UNSAFE_KIND)

    wrong_digest = copy.deepcopy(operation)
    wrong_digest["target_file_sha256"] = "0" * 64
    with pytest.raises(ContractError) as caught:
        _apply_plan(_tree(), {"payload": {"operations": [wrong_digest]}})
    _assert_code(caught, ErrorCode.PATCH_SOURCE_DRIFT)

    invalid_range = copy.deepcopy(operation)
    invalid_range["target"]["end_byte"] = 2
    with pytest.raises(ContractError) as caught:
        _apply_plan(_tree(), {"payload": {"operations": [invalid_range]}})
    _assert_code(caught, ErrorCode.PATCH_OVERLAP)

    invalid_kind = copy.deepcopy(operation)
    invalid_kind["kind"] = "insert"
    with pytest.raises(ContractError) as caught:
        _apply_plan(_tree(), {"payload": {"operations": [invalid_kind]}})
    _assert_code(caught, ErrorCode.HASH_PATCHPLAN_MISMATCH)

    wrong_before = copy.deepcopy(operation)
    wrong_before["before_text"] = "wrong"
    with pytest.raises(ContractError) as caught:
        _apply_plan(_tree(), {"payload": {"operations": [wrong_before]}})
    _assert_code(caught, ErrorCode.PATCH_SOURCE_DRIFT)


def test_apply_plan_and_diff_reject_non_utf8_text() -> None:
    data = b"\xff"
    operation = _operation(data)
    with pytest.raises(ContractError) as caught:
        _apply_plan(_tree(data), {"payload": {"operations": [operation]}})
    _assert_code(caught, ErrorCode.PATCH_UNSAFE_KIND)

    original = _tree(data)
    with pytest.raises(ContractError) as caught:
        _unified_diff(original, {"main.tex": b"changed"})
    _assert_code(caught, ErrorCode.VERIFY_DIFF_MISMATCH)


def test_reconcile_rejects_unplanned_or_misbound_diffs() -> None:
    original = _tree()
    operation = _operation()
    changed = _tree(b"b")

    with pytest.raises(ContractError) as caught:
        _reconcile(original, _tree(b"unexpected"), {"payload": {"operations": []}})
    _assert_code(caught, ErrorCode.VERIFY_DIFF_MISMATCH)

    with pytest.raises(ContractError) as caught:
        _reconcile(
            original,
            changed,
            {"payload": {"operations": [operation], "unified_diff": None}},
        )
    _assert_code(caught, ErrorCode.HASH_PATCHPLAN_MISMATCH)

    diff = _unified_diff(original, changed.files)
    wrong_binding = {"sha256": "0" * 64, "size_bytes": len(diff)}
    with pytest.raises(ContractError) as caught:
        _reconcile(
            original,
            changed,
            {"payload": {"operations": [operation], "unified_diff": wrong_binding}},
        )
    _assert_code(caught, ErrorCode.VERIFY_DIFF_MISMATCH)

    with pytest.raises(ContractError) as caught:
        _reconcile(original, changed, {"payload": {"operations": [], "unified_diff": None}})
    _assert_code(caught, ErrorCode.VERIFY_DIFF_MISMATCH)


@pytest.mark.parametrize("reference", ["", "../escape", "bad\\path", "value#macro", "./"])
def test_input_reference_candidates_reject_dynamic_or_unsafe_paths(reference: str) -> None:
    assert _input_reference_candidates(reference) == ()


def test_flatten_latexdiff_rejects_invalid_bounds_graphs_and_encoding() -> None:
    manifest: dict[str, Any] = {"payload": {"dependency_edges": []}}
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(_tree(), manifest, "main.tex", max_bytes=0)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(_tree(), manifest, "missing.tex", max_bytes=100)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(_tree(b"too large"), manifest, "main.tex", max_bytes=1)
    _assert_code(caught, ErrorCode.VERIFY_COMPILE_FAILED)
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(_tree(b"\xff"), manifest, "main.tex", max_bytes=100)
    _assert_code(caught, ErrorCode.VERIFY_COMPILE_FAILED)

    outside = {
        "payload": {"dependency_edges": [{"kind": "input", "from": "main.tex", "to": "other.tex"}]}
    }
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(_tree(b"\\input{other}"), outside, "main.tex", max_bytes=100)
    _assert_code(caught, ErrorCode.HASH_SOURCE_MISMATCH)

    cyclic_tree = _SourceTree(
        {"main.tex": b"\\input{child}", "child.tex": b"\\input{main}"},
        {"main.tex": "tex", "child.tex": "tex"},
        (),
        "tree",
    )
    cyclic_manifest = {
        "payload": {
            "dependency_edges": [
                {"kind": "input", "from": "main.tex", "to": "child.tex"},
                {"kind": "input", "from": "child.tex", "to": "main.tex"},
            ]
        }
    }
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(cyclic_tree, cyclic_manifest, "main.tex", max_bytes=100)
    _assert_code(caught, ErrorCode.VERIFY_COMPILE_FAILED)


def test_flatten_latexdiff_rejects_ambiguous_casefolded_dependencies() -> None:
    tree = _SourceTree(
        {
            "main.tex": b"\\input{section}",
            "section.tex": b"lower",
            "SECTION.tex": b"upper",
        },
        {"main.tex": "tex", "section.tex": "tex", "SECTION.tex": "tex"},
        (),
        "tree",
    )
    manifest = {
        "payload": {
            "dependency_edges": [
                {"kind": "input", "from": "main.tex", "to": "section.tex"},
                {"kind": "input", "from": "main.tex", "to": "SECTION.tex"},
            ]
        }
    }
    with pytest.raises(ContractError) as caught:
        _flatten_latexdiff_source(tree, manifest, "main.tex", max_bytes=100)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)


def test_latexdiff_marker_probe_is_encoding_and_preamble_safe() -> None:
    assert _latexdiff_has_change_markers(b"\xff") is False
    assert _latexdiff_has_change_markers(b"%DIF PREAMBLE \\DIFadd{definition}\n") is False
    assert _latexdiff_has_change_markers(b"\\DIFadd{real change}\n") is True


def test_windows_search_and_miktex_candidate_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    search = os.pathsep.join(
        ("", "relative", os.fspath(first), os.fspath(first), os.fspath(second))
    )
    assert _safe_windows_search_path(search).split(os.pathsep) == [
        os.fspath(first.resolve()),
        os.fspath(second.resolve()),
    ]

    executable = first / "tool.EXE"
    executable.write_bytes(b"exe")
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert _find_on_windows_path("tool", os.fspath(first)) == executable
    assert _find_on_windows_path("missing", os.fspath(first)) is None

    original_name = os.name
    monkeypatch.setattr(os, "name", "posix")
    assert _common_miktex_candidates("latexmk") == ()
    monkeypatch.setattr(os, "name", original_name)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert _common_miktex_candidates("latexmk") == ()
    monkeypatch.setenv("LOCALAPPDATA", "relative")
    assert _common_miktex_candidates("latexmk") == ()


def test_regular_tool_and_miktex_identity_fail_closed(tmp_path: Path) -> None:
    assert _resolve_regular_tool(tmp_path / "missing.exe") is None
    with pytest.raises(ContractError) as caught:
        _resolve_regular_tool(tmp_path)
    _assert_code(caught, ErrorCode.TOOL_MISSING)

    non_miktex = tmp_path / "tools/tool.exe"
    assert _miktex_identity(non_miktex, "tool") is None
    malformed = tmp_path / "MiKTeX/weird/latexmk.exe"
    with pytest.raises(ContractError) as caught:
        _miktex_identity(malformed, "latexmk")
    _assert_code(caught, ErrorCode.TOOL_VERSION_UNSUPPORTED)

    wrong_name = tmp_path / "root/miktex/bin/x64/other.exe"
    with pytest.raises(ContractError) as caught:
        _miktex_identity(wrong_name, "latexmk")
    _assert_code(caught, ErrorCode.TOOL_VERSION_UNSUPPORTED)
    incomplete = tmp_path / "root/miktex/bin/x64/latexmk.exe"
    with pytest.raises(ContractError) as caught:
        _miktex_identity(incomplete, "latexmk")
    _assert_code(caught, ErrorCode.TOOL_VERSION_UNSUPPORTED)


def test_miktex_installation_state_and_helpers_detect_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _installation_file_state(tmp_path / "missing")
    assert missing.digest is None and missing.mtime_ns is None

    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"tool")

    def unreadable(_path: Path, *, max_bytes: int) -> FileDigest:
        assert max_bytes > 0
        raise OSError("unreadable")

    monkeypatch.setattr(verify_module, "digest_file", unreadable)
    with pytest.raises(ContractError) as caught:
        _installation_file_state(executable)
    _assert_code(caught, ErrorCode.TOOL_VERSION_UNSUPPORTED)

    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    with pytest.raises(ContractError) as caught:
        _miktex_bound_latexmk_arguments(bin_root, "-pdf")
    _assert_code(caught, ErrorCode.TOOL_VERSION_UNSUPPORTED)

    expected = _InstallationFileState(executable, FileDigest(4, "a" * 64), 1)
    toolchain = _TexToolchain("latexmk", "latexdiff", tmp_path, {}, (), (expected,))
    monkeypatch.setattr(
        verify_module,
        "_installation_file_state",
        lambda path: _InstallationFileState(path, FileDigest(4, "b" * 64), 1),
    )
    with pytest.raises(ContractError) as caught:
        _verify_miktex_installation_state(toolchain)
    _assert_code(caught, ErrorCode.VERIFY_COMPILE_FAILED)

    assignment = _perl_command_assignment("bibtex", Path("C:/Program Files/a'b.exe"))
    assert "$bibtex" in assignment and "\\'" in assignment


def test_isolated_miktex_path_deduplicates_and_keeps_system_bins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_root = tmp_path / "bin"
    system_root = tmp_path / "Windows"
    (system_root / "System32").mkdir(parents=True)
    bin_root.mkdir()
    monkeypatch.setenv("SYSTEMROOT", os.fspath(system_root))
    value = _isolated_miktex_path(bin_root, companion_bins=(bin_root, system_root))
    entries = value.split(os.pathsep)
    assert entries.count(os.fspath(bin_root)) == 1
    assert os.fspath((system_root / "System32").resolve()) in entries


def test_tool_run_rejects_nul_and_maps_process_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = VerificationPolicy()
    with pytest.raises(ContractError) as caught:
        _tool_run("tool", "tool", ("bad\x00arg",), cwd=tmp_path, environment={}, policy=policy)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    def missing(*_args: Any, **_kwargs: Any) -> CommandResult:
        raise ContractError(ErrorCode.TOOL_MISSING, "missing")

    monkeypatch.setattr(verify_module, "run_command", missing)
    blocked = _tool_run("tool", "tool", (), cwd=tmp_path, environment={}, policy=policy)
    assert blocked.status == "blocked" and blocked.error_code == ErrorCode.TOOL_MISSING.value

    def invalid(*_args: Any, **_kwargs: Any) -> CommandResult:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid")

    monkeypatch.setattr(verify_module, "run_command", invalid)
    with pytest.raises(ContractError) as caught:
        _tool_run("tool", "tool", (), cwd=tmp_path, environment={}, policy=policy)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    for result in (_result(returncode=1), _result(timed_out=True), _result(output_truncated=True)):
        monkeypatch.setattr(
            verify_module, "run_command", lambda *_args, _result=result, **_kwargs: _result
        )
        assert (
            _tool_run("tool", "tool", (), cwd=tmp_path, environment={}, policy=policy).status
            == "fail"
        )


def test_logs_status_and_compile_checks_cover_failure_edges(tmp_path: Path) -> None:
    root = tmp_path / "secret"
    sanitized = _sanitize_log_text(f"{root} C:\\private\\file /private/file\x00", (root,))
    assert os.fspath(root) not in sanitized
    assert "C:\\private" not in sanitized

    blocked = _ToolRun("blocked", "blocked", None, ErrorCode.TOOL_MISSING.value)
    failed = _ToolRun("failed", "fail", _result(returncode=1), None)
    passed = _ToolRun("passed", "pass", _result(), None)
    assert b'"stdout":""' in _command_log(blocked, ())
    assert _overall_status((blocked,), require_latexdiff=True) == "blocked"
    assert _overall_status((failed,), require_latexdiff=True) == "fail"
    assert _overall_status((passed, failed), require_latexdiff=False) == "pass"
    assert _compile_checks("", "fail") == ("fail", "fail")
    assert _compile_checks("There were undefined references", "pass") == ("fail", "pass")
    assert _compile_checks("clean", "pass") == ("pass", "pass")

    build = tmp_path / "build"
    build.mkdir()
    assert _final_compile_log_text(build, "main.tex", blocked, max_file_bytes=100) == ""
    assert "ok" in _final_compile_log_text(build, "main.tex", passed, max_file_bytes=100)
    (build / "main.log").write_text("final log", encoding="utf-8")
    assert _final_compile_log_text(build, "main.tex", passed, max_file_bytes=100) == "final log"


def test_owned_tree_cleanup_rejects_wrong_owner_and_escape(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    wrong = parent / "wrong"
    wrong.mkdir()
    with pytest.raises(ContractError) as caught:
        _remove_owned_tree(wrong, parent, ".owned-")
    _assert_code(caught, ErrorCode.INTERNAL_INVARIANT)

    escaped = tmp_path / ".owned-stage"
    escaped.mkdir()
    with pytest.raises(ContractError) as caught:
        _remove_owned_tree(escaped, parent, ".owned-")
    _assert_code(caught, ErrorCode.PATH_LINK_ESCAPE)


def test_verify_latex_project_rejects_inconsistent_output_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "original"
    revised = tmp_path / "revised"
    original.mkdir()
    revised.mkdir()
    output = tmp_path / "output"
    monkeypatch.setattr(
        verify_module,
        "_validate_bindings",
        lambda *_args: ("run", "source", "changes", "approval", "plan"),
    )
    calls = 0

    def inconsistent(source: Path, _target: Path) -> tuple[Path, Path]:
        nonlocal calls
        calls += 1
        return source.resolve(), (output if calls == 1 else tmp_path / "other").absolute()

    monkeypatch.setattr(verify_module, "ensure_disjoint_roots", inconsistent)
    with pytest.raises(ContractError) as caught:
        verify_latex_project(original, revised, tmp_path / "return.docx", {}, {}, {}, {}, output)
    _assert_code(caught, ErrorCode.PATH_LINK_ESCAPE)
