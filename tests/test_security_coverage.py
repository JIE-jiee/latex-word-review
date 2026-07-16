"""Additional branch coverage for fail-closed security boundaries.

These tests intentionally target rejection and cleanup paths.  They are not
coverage-only smoke tests: every case asserts the stable public error category
or the no-clobber / immutable filesystem effect that callers rely on.
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import latex_word_review.backends._tex2word_worker as worker
import latex_word_review.backends.pandoc as pandoc_module
import latex_word_review.hashing as hashing_module
from latex_word_review.backends.base import (
    BackendRequest,
    cleanup_stage,
    prepare_export,
    publish_stage,
    verify_source_unchanged,
    write_stage_bytes,
)
from latex_word_review.backends.pandoc import PandocBackend
from latex_word_review.backends.tex2word import Tex2WordBackend
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import (
    FileDigest,
    TreeEntry,
    read_stable_bytes,
    source_tree_sha256,
    verify_file_digest,
)
from latex_word_review.jsonio import write_new_bytes, write_new_json
from latex_word_review.paths import (
    ensure_disjoint_roots,
    path_identity,
    relative_path_from,
    resolve_within,
    validate_relative_path,
)
from latex_word_review.runtime import CommandResult, minimal_environment, run_command

FIXTURE_DOCX = Path(__file__).parent / "fixtures/e0-minimal-paper/base/review-base.docx"


def _write_project(root: Path, body: str = "Plain text.") -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n",
        encoding="utf-8",
        newline="\n",
    )


def _assert_error(code: ErrorCode, operation: object) -> None:
    assert callable(operation)
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


@dataclass(slots=True)
class _Severity:
    value: str


@dataclass(slots=True)
class _Entry:
    severity: _Severity | str
    construct: str


@dataclass(slots=True)
class _Report:
    entries: list[_Entry]
    math_omml: int = 1
    math_image: int = 0
    math_raw: int = 0


@dataclass(slots=True)
class _Result:
    report: _Report
    docx: bytes


class _FakeTex2Word:
    def __init__(self, result: _Result) -> None:
        self._result = result

    def convert_source(
        self,
        source: str,
        base_dir: str = ".",
        *,
        embed_manifest: bool = True,
        citation_mode: str = "static",
        frontend: str = "pure",
    ) -> _Result:
        assert source == "source"
        assert (base_dir, embed_manifest, citation_mode, frontend) == (
            ".",
            False,
            "static",
            "pure",
        )
        return self._result


def test_tex2word_worker_success_sanitizes_and_bounds_constructs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "main.tex"
    output = tmp_path / "review.docx"
    report = tmp_path / "report.json"
    source.write_text("source", encoding="utf-8")
    entries = [_Entry(_Severity("warning"), f"construct-{index}") for index in range(260)]
    entries[0].construct = "unsafe\x00" + "x" * 200
    fake = _FakeTex2Word(_Result(_Report(entries), b"docx"))
    monkeypatch.setattr(importlib, "import_module", lambda name: fake)

    assert (
        worker.main(["--source", str(source), "--output", str(output), "--report", str(report)])
        == 0
    )
    document = __import__("json").loads(report.read_text(encoding="utf-8"))
    assert output.read_bytes() == b"docx"
    assert document["entry_count"] == 260
    assert document["warning_count"] == 260
    assert len(document["constructs"]) == 256
    assert all("\x00" not in item and len(item) <= 128 for item in document["constructs"])


def test_tex2word_worker_errors_and_missing_output_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "main.tex"
    source.write_text("source", encoding="utf-8")
    output = tmp_path / "review.docx"
    report = tmp_path / "report.json"
    error = _Entry(_Severity("error"), "unsupported")
    fake = _FakeTex2Word(_Result(_Report([error]), b"must-not-publish"))
    monkeypatch.setattr(importlib, "import_module", lambda name: fake)

    assert worker._run(source, output, report) == 20
    assert not output.exists()
    assert '"error_count":1' in report.read_text(encoding="utf-8")

    report.unlink()
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: _FakeTex2Word(_Result(_Report([]), b"")),
    )
    assert worker._run(source, output, report) == 21
    assert not output.exists()
    assert not report.exists()


def test_tex2word_worker_accepts_string_warning_and_error_severities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "main.tex"
    source.write_text("source", encoding="utf-8")
    output = tmp_path / "review.docx"
    report = tmp_path / "report.json"
    warning = _Entry("warning", "recoverable")
    fake = _FakeTex2Word(_Result(_Report([warning]), b"docx"))
    monkeypatch.setattr(importlib, "import_module", lambda name: fake)

    assert worker._run(source, output, report) == 0
    assert output.read_bytes() == b"docx"
    assert '"warning_count":1' in report.read_text(encoding="utf-8")

    output.unlink()
    report.unlink()
    error = _Entry("error", "unsupported")
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: _FakeTex2Word(_Result(_Report([error]), b"must-not-publish")),
    )

    assert worker._run(source, output, report) == 20
    assert not output.exists()
    assert '"error_count":1' in report.read_text(encoding="utf-8")


def test_tex2word_worker_exclusive_write_never_clobbers(tmp_path: Path) -> None:
    target = tmp_path / "owned.bin"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        worker._write_exclusive(target, b"replacement")
    assert target.read_bytes() == b"original"


@pytest.mark.parametrize(
    ("request_factory", "code"),
    [
        (
            lambda source, output: BackendRequest(source, "main.tex", output, timeout_s=0),
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda source, output: BackendRequest(
                source, "main.tex", output, max_output_bytes=17 * 1024 * 1024
            ),
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda source, output: BackendRequest(source, "main.txt", output),
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda source, output: BackendRequest(source, "main.tex", output.with_suffix(".pdf")),
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda source, output: BackendRequest(source, "missing.tex", output),
            ErrorCode.PATH_LINK_ESCAPE,
        ),
        (
            lambda source, output: BackendRequest(
                source,
                "main.tex",
                output,
                expected_source_tree_sha256="sha256:" + "0" * 64,
            ),
            ErrorCode.HASH_SOURCE_MISMATCH,
        ),
    ],
)
def test_prepare_export_rejects_invalid_and_unbound_requests(
    tmp_path: Path,
    request_factory: object,
    code: ErrorCode,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out/review.docx"
    assert callable(request_factory)
    request = request_factory(source, output)
    _assert_error(code, lambda: prepare_export(request, owner="test"))


def test_prepare_export_rejects_external_reference(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source, "\\input{../secret}")
    (tmp_path / "secret.tex").write_text("private", encoding="utf-8")
    request = BackendRequest(source, "main.tex", tmp_path / "out/review.docx")
    _assert_error(ErrorCode.PATH_TRAVERSAL, lambda: prepare_export(request, owner="test"))


def test_backend_stage_write_drift_publish_and_cleanup_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out/review.docx"
    prepared = prepare_export(BackendRequest(source, "main.tex", output), owner="test")

    prepared.temporary_path.mkdir()
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: write_stage_bytes(prepared, b"data"))
    prepared.temporary_path.rmdir()

    (source / "main.tex").write_text("drift", encoding="utf-8")
    _assert_error(ErrorCode.HASH_SOURCE_MISMATCH, lambda: verify_source_unchanged(prepared))
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nPlain text.\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    write_stage_bytes(prepared, FIXTURE_DOCX.read_bytes())
    output.write_bytes(b"existing")
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: publish_stage(prepared))
    assert output.read_bytes() == b"existing"
    assert prepared.temporary_path.exists()

    output.unlink()
    original_link = os.link

    def denied_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("denied")

    monkeypatch.setattr(os, "link", denied_link)
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: publish_stage(prepared))
    monkeypatch.setattr(os, "link", original_link)

    escaped = replace(prepared, temporary_path=tmp_path / "escape.docx")
    _assert_error(ErrorCode.INTERNAL_INVARIANT, lambda: cleanup_stage(escaped))
    unowned = replace(prepared, temporary_path=prepared.output_path.parent / "foreign.docx")
    _assert_error(ErrorCode.INTERNAL_INVARIANT, lambda: cleanup_stage(unowned))
    cleanup_stage(prepared)
    cleanup_stage(prepared)


def test_backend_cleanup_oserror_is_not_silenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    prepared = prepare_export(
        BackendRequest(source, "main.tex", tmp_path / "out/review.docx"), owner="test"
    )
    prepared.temporary_path.write_bytes(b"owned")
    concrete_path = type(prepared.temporary_path)
    original_unlink = concrete_path.unlink

    def denied_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == prepared.temporary_path:
            raise PermissionError("denied")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(concrete_path, "unlink", denied_unlink)
    _assert_error(ErrorCode.INTERNAL_INVARIANT, lambda: cleanup_stage(prepared))


def test_tex2word_report_and_cleanup_reject_untrusted_shapes(tmp_path: Path) -> None:
    backend = Tex2WordBackend()
    report = tmp_path / "report.json"
    report.write_text('{"unexpected":1}', encoding="utf-8")
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: backend._read_report(report))

    report.write_text(
        '{"entry_count":-1,"warning_count":0,"error_count":0,'
        '"math_omml":0,"math_image":0,"math_raw":0,"constructs":[],"warning_constructs":[]}',
        encoding="utf-8",
    )
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: backend._read_report(report))

    report.write_text(
        '{"entry_count":0,"warning_count":0,"error_count":0,'
        '"math_omml":0,"math_image":0,"math_raw":0,"constructs":[1],"warning_constructs":[]}',
        encoding="utf-8",
    )
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: backend._read_report(report))

    parent = tmp_path / "out"
    parent.mkdir()
    escaped = tmp_path / "foreign.json"
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: backend._cleanup_report_path(escaped, parent, "review.docx"),
    )
    unowned = parent / "foreign.json"
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: backend._cleanup_report_path(unowned, parent, "review.docx"),
    )


def test_pandoc_probe_failures_and_missing_artifact_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_project(source)
    output = tmp_path / "out/review.docx"
    with pytest.raises(ContractError) as raised:
        PandocBackend(executable_arguments=("bad\x00arg",))
    assert raised.value.code is ErrorCode.SCHEMA_INVALID

    backend = PandocBackend()

    def missing(*args: object, **kwargs: object) -> CommandResult:
        del args, kwargs
        raise ContractError(ErrorCode.TOOL_MISSING, "missing")

    monkeypatch.setattr(pandoc_module, "run_command", missing)
    result = backend.export(BackendRequest(source, "main.tex", output))
    assert not result.succeeded
    assert result.findings[0].code is ErrorCode.TOOL_MISSING
    assert not output.exists()

    probe_failure = CommandResult(2, "", "", False, False, 1, "sha256:" + "a" * 64)
    monkeypatch.setattr(pandoc_module, "run_command", lambda *args, **kwargs: probe_failure)
    result = PandocBackend().export(BackendRequest(source, "main.tex", output))
    assert not result.succeeded
    assert result.returncode == 2

    calls = 0

    def no_artifact(*args: object, **kwargs: object) -> CommandResult:
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            return CommandResult(
                0, "pandoc unparseable-version", "", False, False, 1, "sha256:" + "b" * 64
            )
        return CommandResult(0, "", "", False, False, 1, "sha256:" + "c" * 64)

    monkeypatch.setattr(pandoc_module, "run_command", no_artifact)
    result = PandocBackend().export(BackendRequest(source, "main.tex", output))
    assert not result.succeeded
    assert result.capabilities.tool_version == "unknown"
    assert not output.exists()


@pytest.mark.parametrize("timeout", [0, 61])
def test_runtime_rejects_invalid_timeout(tmp_path: Path, timeout: float) -> None:
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: run_command(sys.executable, ("--version",), cwd=tmp_path, timeout_s=timeout),
    )


@pytest.mark.parametrize("limit", [0, 17 * 1024 * 1024])
def test_runtime_rejects_invalid_output_limit(tmp_path: Path, limit: int) -> None:
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: run_command(
            sys.executable,
            ("--version",),
            cwd=tmp_path,
            max_output_bytes=limit,
        ),
    )


def test_runtime_rejects_bad_cwd_and_bounds_invalid_output(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: run_command(sys.executable, ("--version",), cwd=missing),
    )
    regular = tmp_path / "file"
    regular.write_text("x", encoding="utf-8")
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: run_command(sys.executable, ("--version",), cwd=regular),
    )

    code = "import sys;sys.stdout.buffer.write(b'\\xff\\x00'+b'x'*10000)"
    result = run_command(
        sys.executable,
        ("-c", code),
        cwd=tmp_path,
        timeout_s=5,
        max_output_bytes=16,
    )
    assert result.output_truncated
    assert "�" in result.stdout
    assert "\x00" not in result.stdout


def test_minimal_environment_only_adds_explicit_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    miktex_roots = {
        "MIKTEX_USERCONFIG": os.fspath(tmp_path / "miktex-config"),
        "MIKTEX_USERDATA": os.fspath(tmp_path / "miktex-data"),
        "MIKTEX_USERINSTALL": os.fspath(tmp_path / "miktex-install"),
    }
    for name, value in miktex_roots.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LWR_UNTRUSTED_ENV", "must-not-pass")
    environment = minimal_environment(temp_root=tmp_path, additions={"LWR_TEST": "yes"})
    assert environment["LWR_TEST"] == "yes"
    assert environment["TEMP"] == os.fspath(tmp_path)
    assert "PYTHONUTF8" in environment
    assert all(name not in environment for name in miktex_roots)
    assert "LWR_UNTRUSTED_ENV" not in environment


def test_hashing_rejects_nonregular_drift_duplicate_and_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: read_stable_bytes(tmp_path, max_bytes=1))
    path = tmp_path / "file.tex"
    path.write_bytes(b"abc")
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: read_stable_bytes(path, max_bytes=0))
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: source_tree_sha256(
            [
                TreeEntry("same.tex", "tex", 1, "sha256:" + "a" * 64),
                TreeEntry("same.tex", "tex", 1, "sha256:" + "b" * 64),
            ]
        ),
    )
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: verify_file_digest(
            path,
            FileDigest(3, "sha256:" + "0" * 64),
            max_bytes=10,
        ),
    )

    original = hashing_module._stat_fingerprint
    calls = 0

    def drift(candidate: Path) -> tuple[int, int, int, int]:
        nonlocal calls
        calls += 1
        fingerprint = original(candidate)
        if calls == 2:
            return (*fingerprint[:3], fingerprint[3] + 1)
        return fingerprint

    monkeypatch.setattr(hashing_module, "_stat_fingerprint", drift)
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: read_stable_bytes(path, max_bytes=10),
    )


def test_path_helpers_reject_unavailable_and_overlapping_roots(tmp_path: Path) -> None:
    assert validate_relative_path("folder/name.tex") == "folder/name.tex"
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: resolve_within(tmp_path / "missing", "name.tex"),
    )
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: resolve_within(regular, "name.tex"))

    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.tex"
    inside.write_text("x", encoding="utf-8")
    assert relative_path_from(root, inside) == "inside.tex"
    outside = tmp_path / "outside.tex"
    outside.write_text("x", encoding="utf-8")
    _assert_error(ErrorCode.PATH_LINK_ESCAPE, lambda: relative_path_from(root, outside))
    _assert_error(ErrorCode.PATH_TRAVERSAL, lambda: ensure_disjoint_roots(root, root / "out"))
    _assert_error(ErrorCode.PATH_TRAVERSAL, lambda: ensure_disjoint_roots(root, tmp_path))
    assert path_identity(inside) == path_identity(inside)


def test_json_publication_rejects_bad_paths_and_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: write_new_bytes(Path(), b"data"))
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: write_new_bytes(tmp_path / "missing/file.bin", b"data"),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: write_new_json(tmp_path / "wrong.txt", {"value": 1}),
    )

    original_link = os.link

    def denied_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("denied")

    monkeypatch.setattr(os, "link", denied_link)
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: write_new_bytes(tmp_path / "result.bin", b"data"),
    )
    monkeypatch.setattr(os, "link", original_link)
    assert not (tmp_path / "result.bin").exists()
    assert not list(tmp_path.glob(".result.bin.publish-*.tmp"))
