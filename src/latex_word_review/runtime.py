"""Bounded, UTF-8 subprocess probes for environment diagnostics."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_limits import (
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    validate_export_timeout,
)
from latex_word_review.frozen_runtime import internal_worker_command
from latex_word_review.hashing import digest_bytes

_PASSTHROUGH_ENVIRONMENT = (
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "USERPROFILE",
    "WINDIR",
)
_TERMINATION_GRACE_SECONDS = 5.0
_PIPE_DRAIN_GRACE_SECONDS = 0.25


def _which_windows_path_only(
    command: str,
    *,
    search_path: str,
    path_extensions: str,
) -> str | None:
    """Search absolute PATH entries without Windows' implicit cwd fallback."""

    suffixes: tuple[str, ...] = ("",)
    if not Path(command).suffix:
        configured = tuple(
            item for item in path_extensions.split(os.pathsep) if item and item.startswith(".")
        )
        suffixes = configured or (".COM", ".EXE", ".BAT", ".CMD")
    for raw_directory in search_path.split(os.pathsep):
        raw_directory = raw_directory.strip().strip('"')
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        if not directory.is_absolute():
            continue
        for suffix in suffixes:
            candidate = directory / f"{command}{suffix}"
            try:
                if candidate.is_file():
                    return os.fspath(candidate)
            except OSError:
                continue
    return None


def _resolve_windows_executable(
    executable: str | Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> str:
    """Resolve a Windows command without implicitly trusting the tool cwd.

    ``CreateProcess`` and the standard Windows command search can otherwise select an executable
    planted in the current working directory.  A bare command is therefore
    resolved from ``PATH`` only.  Callers that intentionally use a relative
    executable must include a path component, which is resolved against the
    supplied command cwd before the contained launcher is started.
    """

    requested = os.fspath(executable)
    requested_path = Path(requested)
    has_path_component = (
        requested_path.is_absolute()
        or requested_path.parent != Path(".")
        or "/" in requested
        or "\\" in requested
    )
    if has_path_component:
        candidate = requested_path if requested_path.is_absolute() else cwd / requested_path
        try:
            located = os.fspath(candidate.resolve(strict=True))
        except OSError:
            located = None
    else:
        located = _which_windows_path_only(
            requested,
            search_path=environment.get("PATH", ""),
            path_extensions=environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        )
    if located is None:
        raise ContractError(ErrorCode.TOOL_MISSING, "tool process could not be started")
    try:
        resolved = Path(located).resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.TOOL_MISSING, "tool process could not be started") from exc
    if not resolved.is_file():
        raise ContractError(ErrorCode.TOOL_MISSING, "tool process could not be started")
    return os.fspath(resolved)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Path-free result from one read-only tool probe."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool
    duration_ms: int
    output_sha256: str


def minimal_environment(
    *,
    temp_root: Path,
    additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a small deterministic environment without copying secrets."""

    environment = {
        key: os.environ[key]
        for key in _PASSTHROUGH_ENVIRONMENT
        if key in os.environ and os.environ[key]
    }
    environment.update(
        {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NoDefaultCurrentDirectoryInExePath": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TEMP": os.fspath(temp_root),
            "TMP": os.fspath(temp_root),
            "TMPDIR": os.fspath(temp_root),
        }
    )
    if additions:
        environment.update(additions)
    return environment


def _read_bounded(
    stream: BinaryIO,
    limit: int,
    overflow: threading.Event,
    destination: bytearray,
) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = limit - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if remaining <= 0 or len(chunk) > remaining:
                overflow.set()
                # Keep draining until the process tree is terminated.  Closing
                # the pipe here can let the direct child exit before its
                # descendants are found, leaving an inherited writer alive.
    except (OSError, ValueError):
        # A forced close is a bounded-output condition, not an internal
        # invariant failure.  Callers can fail closed on output_truncated.
        overflow.set()
    finally:
        with suppress(OSError, ValueError):
            stream.close()


def _decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\x00", "�")


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    """Attach a Windows process to a kill-on-close Job Object when possible."""

    if os.name != "nt":
        return None

    # ctypes is deliberately local: importing Windows-only loader symbols must
    # not make the module unimportable on POSIX.
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        )

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    job_handle = int(job)
    information = _ExtendedLimitInformation()
    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.  Closing the owned handle therefore
    # also catches descendants that outlive a successful direct child.
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    process_handle = getattr(process, "_handle", None)
    assigned = bool(
        configured
        and isinstance(process_handle, int)
        and kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process_handle))
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return job_handle


def _terminate_windows_job(job_handle: int | None) -> None:
    if os.name != "nt" or job_handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    with suppress(OSError):
        kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1)


def _close_windows_job(job_handle: int | None) -> None:
    if os.name != "nt" or job_handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    with suppress(OSError):
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    windows_job: int | None,
) -> None:
    """Best-effort hard termination of the process and all descendants."""

    if os.name == "nt":
        _terminate_windows_job(windows_job)
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
        taskkill = (
            Path(system_root) / "System32" / "taskkill.exe" if system_root else Path("taskkill.exe")
        )
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603 - fixed system utility and fixed argv
                [os.fspath(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_TERMINATION_GRACE_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    else:
        with suppress(OSError):
            # run_command starts a fresh session, so the child's PID is also
            # the process-group ID.  Killing the group closes pipes inherited
            # by grandchildren as well as terminating the direct child.
            os.kill(-process.pid, 9)

    if process.poll() is None:
        with suppress(OSError):
            process.kill()


def _wait_after_termination(
    process: subprocess.Popen[bytes],
    *,
    windows_job: int | None,
) -> int:
    """Reap a terminated process without turning cleanup lag into E_INTERNAL."""

    try:
        return process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process, windows_job=windows_job)
        try:
            return process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return -1


def _run_command_with_environment(
    executable: str | Path,
    arguments: Sequence[str],
    *,
    resolved_cwd: Path,
    timeout_s: float,
    max_output_bytes: int,
    environment: Mapping[str, str],
) -> CommandResult:
    requested_command = [os.fspath(executable), *arguments]
    command = requested_command
    child_stdin: int = subprocess.DEVNULL
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    if os.name == "nt":
        requested_command[0] = _resolve_windows_executable(
            executable,
            cwd=resolved_cwd,
            environment=environment,
        )
        creation_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # The trusted launcher cannot spawn the requested tool until its stdin
        # gate is released.  This closes the CreateProcess -> AssignJob race:
        # every requested process is born under the already-assigned launcher.
        launcher = internal_worker_command("windows-gated-launcher", requested_command)
        command = [launcher.executable, *launcher.arguments]
        child_stdin = subprocess.PIPE
    started = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, shell is never used
            command,
            cwd=resolved_cwd,
            env=dict(environment),
            stdin=child_stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Raw FileIO pipes have no BufferedReader lock.  If an escaped
            # descendant inherits a writer, cleanup can close our read handle
            # without deadlocking behind another thread's buffered read.
            bufsize=0,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ContractError(ErrorCode.TOOL_MISSING, "tool process could not be started") from exc
    try:
        windows_job = _create_windows_kill_job(process)
    except (AttributeError, OSError, TypeError, ValueError):
        windows_job = None
    if os.name == "nt" and windows_job is None:
        # The launcher is still blocked on its gate, so no requested tool or
        # descendant exists yet.  Never fall back to an uncontained execution.
        _terminate_process_tree(process, windows_job=None)
        _wait_after_termination(process, windows_job=None)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "Windows process containment could not be established",
        )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen invariant
        _terminate_process_tree(process, windows_job=windows_job)
        _close_windows_job(windows_job)
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "probe pipes were not created")
    if os.name == "nt":
        if process.stdin is None:  # pragma: no cover - Popen invariant
            _terminate_process_tree(process, windows_job=windows_job)
            _close_windows_job(windows_job)
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "launcher gate was not created")
        try:
            process.stdin.write(b"1")
            process.stdin.close()
        except OSError as exc:
            _terminate_process_tree(process, windows_job=windows_job)
            _wait_after_termination(process, windows_job=windows_job)
            _close_windows_job(windows_job)
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "contained launcher could not be released",
            ) from exc

    overflow = threading.Event()
    stdout_data = bytearray()
    stderr_data = bytearray()
    threads = (
        threading.Thread(
            target=_read_bounded,
            args=(process.stdout, max_output_bytes, overflow, stdout_data),
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded,
            args=(process.stderr, max_output_bytes, overflow, stderr_data),
            daemon=True,
        ),
    )
    try:
        for thread in threads:
            thread.start()

        deadline = started + timeout_s
        direct_exit_observed_at: float | None = None
        timed_out = False
        terminated = False
        while True:
            process_done = process.poll() is not None
            readers_done = all(not thread.is_alive() for thread in threads)
            if process_done and readers_done:
                break
            now = time.monotonic()
            if overflow.is_set():
                _terminate_process_tree(process, windows_job=windows_job)
                terminated = True
                break
            if now >= deadline:
                timed_out = True
                _terminate_process_tree(process, windows_job=windows_job)
                terminated = True
                break
            if process_done:
                if direct_exit_observed_at is None:
                    direct_exit_observed_at = now
                elif now - direct_exit_observed_at >= _PIPE_DRAIN_GRACE_SECONDS:
                    # A direct child that exits while an inherited writer stays
                    # open is not a completed command.  Kill its containment
                    # tree and fail closed instead of waiting for pipe EOF.
                    overflow.set()
                    _terminate_process_tree(process, windows_job=windows_job)
                    terminated = True
                    break
            time.sleep(0.01)

        returncode = (
            _wait_after_termination(process, windows_job=windows_job)
            if terminated
            else process.wait()
        )
        readers_deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
        for thread in threads:
            thread.join(timeout=max(0.0, readers_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            overflow.set()
            for stream in (process.stdout, process.stderr):
                with suppress(OSError, ValueError):
                    stream.close()
            readers_deadline = time.monotonic() + 1.0
            for thread in threads:
                thread.join(timeout=max(0.0, readers_deadline - time.monotonic()))

        raw_stdout = bytes(stdout_data)
        raw_stderr = bytes(stderr_data)
        combined = raw_stdout + b"\x00" + raw_stderr
        return CommandResult(
            returncode=returncode,
            stdout=_decode_output(raw_stdout),
            stderr=_decode_output(raw_stderr),
            timed_out=timed_out,
            output_truncated=overflow.is_set(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            output_sha256=digest_bytes(combined).sha256,
        )
    finally:
        if process.poll() is None:
            _terminate_process_tree(process, windows_job=windows_job)
            _wait_after_termination(process, windows_job=windows_job)
        elif os.name != "nt":
            # The session/process group can still contain descendants after a
            # successful direct child has been reaped.
            with suppress(OSError):
                os.kill(-process.pid, 9)
        _close_windows_job(windows_job)


def _run_validated_command(
    executable: str | Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
) -> CommandResult:
    """Execute one command after its caller-specific timeout validation."""

    if not 0 < max_output_bytes <= 16 * 1024 * 1024:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "probe output limit is invalid")
    try:
        resolved_cwd = cwd.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "probe cwd is unavailable") from exc
    if not resolved_cwd.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "probe cwd must be a directory")

    if environment is not None:
        return _run_command_with_environment(
            executable,
            arguments,
            resolved_cwd=resolved_cwd,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
            environment=environment,
        )

    # A caller that does not provide an environment still receives an isolated
    # temporary root.  In particular, TEMP/TMP must never silently point at the
    # source or diagnostic cwd.
    with tempfile.TemporaryDirectory(
        prefix="latex-word-review-runtime-",
        ignore_cleanup_errors=True,
    ) as temp_directory:
        isolated_environment = minimal_environment(temp_root=Path(temp_directory))
        return _run_command_with_environment(
            executable,
            arguments,
            resolved_cwd=resolved_cwd,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
            environment=isolated_environment,
        )


def run_command(
    executable: str | Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = 5.0,
    max_output_bytes: int = 1024 * 1024,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one non-conversion command with the existing 60-second hard limit.

    The executable and cwd are intentionally absent from the returned object so
    callers cannot accidentally serialize personal installation paths.
    """

    if not 0 < timeout_s <= 60:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "probe timeout must be in (0, 60]")
    return _run_validated_command(
        executable,
        arguments,
        cwd=cwd,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
        environment=environment,
    )


def run_conversion_command(
    executable: str | Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = DEFAULT_EXPORT_TIMEOUT_SECONDS,
    max_output_bytes: int = 1024 * 1024,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one LaTeX-to-Word conversion with the export-specific hard limit."""

    validated_timeout = validate_export_timeout(timeout_s)
    return _run_validated_command(
        executable,
        arguments,
        cwd=cwd,
        timeout_s=validated_timeout,
        max_output_bytes=max_output_bytes,
        environment=environment,
    )


__all__ = [
    "CommandResult",
    "minimal_environment",
    "run_command",
    "run_conversion_command",
]
