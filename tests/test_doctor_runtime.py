"""Tests for bounded subprocess probes and path-free doctor reports."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from latex_word_review import runtime as runtime_module
from latex_word_review.doctor import (
    DEFAULT_TOOL_SPECS,
    ToolSpec,
    diagnose_environment,
    probe_package,
    probe_tool,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.runtime import run_command


def test_runtime_decodes_utf8_and_returns_no_command_path(tmp_path: Path) -> None:
    result = run_command(
        sys.executable,
        ("-c", "print('环境✓')"),
        cwd=tmp_path,
        timeout_s=5,
        max_output_bytes=1024,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "环境✓"
    serialized = json.dumps(asdict(result))
    assert str(Path(sys.executable).parent) not in serialized


def test_runtime_replaces_invalid_utf8_and_nul(tmp_path: Path) -> None:
    result = run_command(
        sys.executable,
        ("-c", "import sys;sys.stdout.buffer.write(b'\\xff\\x00ok')"),
        cwd=tmp_path,
        timeout_s=5,
        max_output_bytes=1024,
    )

    assert result.returncode == 0
    assert result.stdout == "��ok"
    assert "\x00" not in result.stdout


def test_runtime_missing_executable_preserves_tool_missing_error(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as captured:
        run_command(
            "latex-word-review-command-that-does-not-exist-61a7",
            ("--version",),
            cwd=tmp_path,
        )

    assert captured.value.code == ErrorCode.TOOL_MISSING


class _ReadFailureStream:
    def __init__(self) -> None:
        self.closed = False

    def read(self, _: int = -1) -> bytes:
        raise OSError("synthetic read failure")

    def close(self) -> None:
        self.closed = True


def test_bounded_reader_treats_forced_read_failure_as_truncation() -> None:
    stream = _ReadFailureStream()
    overflow = threading.Event()
    destination = bytearray()

    runtime_module._read_bounded(cast(BinaryIO, stream), 16, overflow, destination)

    assert overflow.is_set()
    assert destination == b""
    assert stream.closed is True


class _NeverReapedProcess:
    pid = 8617

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(
            cmd="synthetic", timeout=0.0 if timeout is None else timeout
        )


def test_wait_after_termination_returns_sentinel_after_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = cast(subprocess.Popen[bytes], _NeverReapedProcess())
    terminations: list[int | None] = []

    def record_termination(
        _: subprocess.Popen[bytes],
        *,
        windows_job: int | None,
    ) -> None:
        terminations.append(windows_job)

    monkeypatch.setattr(runtime_module, "_terminate_process_tree", record_termination)

    assert runtime_module._wait_after_termination(process, windows_job=37) == -1
    assert terminations == [37]


def test_runtime_enforces_timeout(tmp_path: Path) -> None:
    result = run_command(
        sys.executable,
        ("-c", "import time; time.sleep(2)"),
        cwd=tmp_path,
        timeout_s=0.05,
        max_output_bytes=1024,
    )
    assert result.timed_out is True
    assert result.returncode != 0


def _parent_with_pipe_holding_child(*, write_overflow: bool) -> str:
    child = "import time; time.sleep(20)"
    statements = [
        "import subprocess,sys,time",
        (
            "subprocess.Popen([sys.executable,'-c',"
            f"{child!r}],stdout=sys.stdout,stderr=sys.stderr,close_fds=False)"
        ),
        "print('child-started',flush=True)",
    ]
    if write_overflow:
        statements.append("sys.stdout.write('x'*65536);sys.stdout.flush()")
    statements.append("time.sleep(20)")
    return ";".join(statements)


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ("tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid_for_test(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(pid), "/T", "/F"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def test_runtime_timeout_terminates_descendant_holding_output_pipe(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_command(
        sys.executable,
        ("-c", _parent_with_pipe_holding_child(write_overflow=False)),
        cwd=tmp_path,
        timeout_s=1.0,
        max_output_bytes=1024,
    )

    assert result.timed_out is True
    assert "child-started" in result.stdout
    # A surviving descendant keeps the inherited pipe open.  The bounded
    # runner must return promptly instead of waiting for that 20-second child.
    assert time.monotonic() - started < 4


def test_runtime_parent_exit_does_not_wait_for_or_leak_pipe_holding_descendant(
    tmp_path: Path,
) -> None:
    child = "import time; time.sleep(20)"
    parent = (
        "import subprocess,sys;"
        "process=subprocess.Popen([sys.executable,'-c',"
        f"{child!r}],stdout=sys.stdout,stderr=sys.stderr,close_fds=False);"
        "print(process.pid,flush=True)"
    )
    started = time.monotonic()
    result = run_command(
        sys.executable,
        ("-c", parent),
        cwd=tmp_path,
        timeout_s=2,
        max_output_bytes=1024,
    )
    descendant_pid = int(result.stdout.strip())

    try:
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.output_truncated is True
        assert time.monotonic() - started < 4
        cleanup_deadline = time.monotonic() + 2
        while _pid_is_running(descendant_pid) and time.monotonic() < cleanup_deadline:
            time.sleep(0.02)
        assert _pid_is_running(descendant_pid) is False
    finally:
        if _pid_is_running(descendant_pid):
            _kill_pid_for_test(descendant_pid)


class _FakeWinFunction:
    def __init__(self, result: int) -> None:
        self.result = result
        self.argtypes: object | None = None
        self.restype: object | None = None
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *arguments: object) -> int:
        self.calls.append(arguments)
        return self.result


class _FakeKernel32:
    def __init__(self, *, create: int, configure: int, assign: int) -> None:
        self.CreateJobObjectW = _FakeWinFunction(create)
        self.SetInformationJobObject = _FakeWinFunction(configure)
        self.AssignProcessToJobObject = _FakeWinFunction(assign)
        self.CloseHandle = _FakeWinFunction(1)


class _HandleProcess:
    def __init__(self, handle: object) -> None:
        self._handle = handle


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object setup paths")
@pytest.mark.parametrize(
    ("create", "configure", "process_handle", "assign", "expected_close_calls"),
    [
        (0, 1, 73, 1, 0),
        (41, 0, 73, 1, 1),
        (41, 1, None, 1, 1),
        (41, 1, 73, 0, 1),
    ],
    ids=("create-failed", "configure-failed", "handle-missing", "assign-failed"),
)
def test_windows_job_setup_failures_return_no_uncontained_job(
    monkeypatch: pytest.MonkeyPatch,
    create: int,
    configure: int,
    process_handle: object,
    assign: int,
    expected_close_calls: int,
) -> None:
    import ctypes

    kernel32 = _FakeKernel32(create=create, configure=configure, assign=assign)

    def load_kernel32(_: str, *, use_last_error: bool) -> _FakeKernel32:
        assert use_last_error is True
        return kernel32

    monkeypatch.setattr(ctypes, "WinDLL", load_kernel32)
    process = cast(subprocess.Popen[bytes], _HandleProcess(process_handle))

    assert runtime_module._create_windows_kill_job(process) is None
    assert len(kernel32.CloseHandle.calls) == expected_close_calls


class _BrokenGate:
    def write(self, _: bytes) -> int:
        raise OSError("synthetic gate failure")

    def close(self) -> None:
        return None


class _GateFailureProcess:
    pid = 9173

    def __init__(self) -> None:
        self.stdin = _BrokenGate()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher gate path")
def test_windows_launcher_gate_failure_terminates_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = cast(subprocess.Popen[bytes], _GateFailureProcess())
    cleanup: list[tuple[str, int | None]] = []

    def fake_popen(*_: object, **__: object) -> subprocess.Popen[bytes]:
        return process

    def fake_create_job(_: subprocess.Popen[bytes]) -> int:
        return 83

    def fake_terminate(
        _: subprocess.Popen[bytes],
        *,
        windows_job: int | None,
    ) -> None:
        cleanup.append(("terminate", windows_job))

    def fake_wait(
        _: subprocess.Popen[bytes],
        *,
        windows_job: int | None,
    ) -> int:
        cleanup.append(("wait", windows_job))
        return 1

    def fake_close(windows_job: int | None) -> None:
        cleanup.append(("close", windows_job))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_module, "_create_windows_kill_job", fake_create_job)
    monkeypatch.setattr(runtime_module, "_terminate_process_tree", fake_terminate)
    monkeypatch.setattr(runtime_module, "_wait_after_termination", fake_wait)
    monkeypatch.setattr(runtime_module, "_close_windows_job", fake_close)

    with pytest.raises(ContractError) as captured:
        run_command(sys.executable, ("--version",), cwd=tmp_path)

    assert captured.value.code == ErrorCode.INTERNAL_INVARIANT
    assert cleanup == [("terminate", 83), ("wait", 83), ("close", 83)]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object fail-closed path")
def test_windows_containment_failure_never_releases_requested_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "must-not-exist.txt"

    def reject_containment(_: subprocess.Popen[bytes]) -> None:
        return None

    monkeypatch.setattr(runtime_module, "_create_windows_kill_job", reject_containment)
    with pytest.raises(ContractError) as captured:
        run_command(
            sys.executable,
            ("-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"),
            cwd=tmp_path,
            timeout_s=2,
            max_output_bytes=1024,
        )

    assert captured.value.code == ErrorCode.INTERNAL_INVARIANT
    assert marker.exists() is False


def test_runtime_overflow_terminates_descendant_holding_output_pipe(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_command(
        sys.executable,
        ("-c", _parent_with_pipe_holding_child(write_overflow=True)),
        cwd=tmp_path,
        timeout_s=5,
        max_output_bytes=128,
    )

    assert result.output_truncated is True
    assert result.timed_out is False
    assert len(result.stdout.encode("utf-8")) <= 128
    assert time.monotonic() - started < 4


def test_missing_tool_is_reported_without_install_attempt(tmp_path: Path) -> None:
    spec = ToolSpec(
        "definitely-missing",
        ("latex-word-review-tool-that-does-not-exist-9f79",),
        required=True,
    )
    probe = probe_tool(spec, cwd=tmp_path)
    assert probe.status == "missing"
    assert probe.error_code == ErrorCode.TOOL_MISSING.value


def test_probe_tool_rejects_non_directory_cwd_as_schema_failure(tmp_path: Path) -> None:
    cwd_file = tmp_path / "not-a-directory"
    cwd_file.write_text("sentinel", encoding="utf-8")
    spec = ToolSpec("python", (sys.executable,), required=True)

    probe = probe_tool(spec, cwd=cwd_file)

    assert probe.status == "failed"
    assert probe.error_code == ErrorCode.SCHEMA_INVALID.value
    assert probe.executable_sha256 is None


def test_probe_tool_reports_timeout_without_internal_error(tmp_path: Path) -> None:
    spec = ToolSpec(
        "slow-python",
        (sys.executable,),
        ("-c", "import time;time.sleep(5)"),
    )

    probe = probe_tool(spec, cwd=tmp_path, timeout_s=0.05)

    assert probe.status == "timed_out"
    assert probe.error_code == ErrorCode.TOOL_VERSION_UNSUPPORTED.value
    assert probe.executable_sha256 is not None


@pytest.mark.parametrize(
    ("arguments", "max_output_bytes"),
    [
        (("-c", "raise SystemExit(7)"), 1024),
        (("-c", "print('x'*4096)"), 64),
    ],
    ids=("nonzero", "output-truncated"),
)
def test_probe_tool_fails_closed_on_bad_probe_result(
    tmp_path: Path,
    arguments: tuple[str, ...],
    max_output_bytes: int,
) -> None:
    spec = ToolSpec("bad-python", (sys.executable,), arguments)

    probe = probe_tool(spec, cwd=tmp_path, max_output_bytes=max_output_bytes)

    assert probe.status == "failed"
    assert probe.error_code == ErrorCode.TOOL_VERSION_UNSUPPORTED.value
    assert probe.executable_sha256 is not None


def test_probe_tool_allows_path_free_output_without_numeric_version(tmp_path: Path) -> None:
    spec = ToolSpec("versionless-python", (sys.executable,), ("-c", "print('ready')"))

    probe = probe_tool(spec, cwd=tmp_path)

    assert probe.status == "available"
    assert probe.version is None
    assert probe.error_code is None


def test_probe_package_reports_missing_distribution() -> None:
    probe = probe_package("latex-word-review-package-that-does-not-exist-61a7", required=False)

    assert probe.status == "missing"
    assert probe.version is None
    assert probe.error_code == ErrorCode.TOOL_MISSING.value


def test_doctor_report_does_not_leak_executable_path(tmp_path: Path) -> None:
    spec = ToolSpec("python", (sys.executable,), ("--version",), required=True)
    report = diagnose_environment(cwd=tmp_path, tool_specs=(spec,))
    serialized = json.dumps(report.as_dict(), ensure_ascii=False)
    assert report.tools[0].status == "available"
    assert str(Path(sys.executable).parent) not in serialized
    assert "executable_path" not in serialized


def test_required_missing_tool_blocks_doctor(tmp_path: Path) -> None:
    spec = ToolSpec("missing", ("missing-tool-52aa2",), required=True)
    report = diagnose_environment(cwd=tmp_path, tool_specs=(spec,))
    assert report.status == "blocked"


def test_doctor_uses_owned_temporary_cwd_and_leaves_requested_cwd_unchanged(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = sorted(path.name for path in tmp_path.iterdir())
    code = (
        "import os;from pathlib import Path;"
        "Path(os.environ['TEMP'],'par-synthetic').mkdir();"
        "Path('mik-synthetic.tmp').write_text('temporary',encoding='utf-8');"
        "print('synthetic-tool 1.2.3')"
    )
    spec = ToolSpec("synthetic", (sys.executable,), ("-c", code), required=True)

    report = diagnose_environment(cwd=tmp_path, tool_specs=(spec,))

    assert report.tools[0].status == "available"
    assert report.tools[0].version == "1.2.3"
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_biber_cold_start_has_an_explicit_bounded_probe_budget() -> None:
    biber = next(spec for spec in DEFAULT_TOOL_SPECS if spec.name == "biber")
    assert biber.probe_timeout_s == 60.0
    assert 5.0 < biber.probe_timeout_s <= 60.0
