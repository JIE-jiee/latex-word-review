"""Allow-listed dispatch for workers spawned by a frozen Windows build."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from typing import BinaryIO, TextIO, cast

from latex_word_review.errors import ContractError
from latex_word_review.frozen_runtime import (
    INTERNAL_WORKER_FLAG,
    InternalWorker,
    validate_internal_worker_arguments,
)

_REJECTED_EXIT = 64
_WORKER_FAILED_EXIT = 22
_GATE_REJECTED_EXIT = 125
_LAUNCH_FAILED_EXIT = 126
_WINDOWS_STANDARD_HANDLES = {0: -10, 1: -11, 2: -12}


def _write_stderr(message: str) -> None:
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(message)
        stream.flush()
    except (OSError, ValueError):
        return


def _standard_binary_stream(
    stream: TextIO | None,
    *,
    file_descriptor: int,
    mode: str,
) -> BinaryIO | None:
    """Return an inherited binary stream, including a windowed-bundle fallback."""

    if stream is not None:
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            return cast(BinaryIO, binary)
    try:
        duplicate = os.dup(file_descriptor)
        return cast(BinaryIO, os.fdopen(duplicate, mode, buffering=0))
    except OSError:
        return _windows_standard_binary_stream(file_descriptor=file_descriptor, mode=mode)


def _windows_standard_binary_stream(
    *,
    file_descriptor: int,
    mode: str,
) -> BinaryIO | None:
    """Recover redirected Win32 standard handles when CRT descriptors are absent."""

    if os.name != "nt" or file_descriptor not in _WINDOWS_STANDARD_HANDLES:
        return None
    if mode not in {"rb", "wb"}:
        return None

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    raw_handle = kernel32.GetStdHandle(_WINDOWS_STANDARD_HANDLES[file_descriptor])
    invalid_handle = ctypes.c_void_p(-1).value
    if not isinstance(raw_handle, int) or raw_handle in {0, invalid_handle}:
        return None

    current_process = kernel32.GetCurrentProcess()
    duplicated_handle = wintypes.HANDLE()
    duplicated = kernel32.DuplicateHandle(
        current_process,
        wintypes.HANDLE(raw_handle),
        current_process,
        ctypes.byref(duplicated_handle),
        0,
        False,
        0x00000002,  # DUPLICATE_SAME_ACCESS
    )
    if not duplicated or duplicated_handle.value is None:
        return None

    flags = os.O_RDONLY if mode == "rb" else os.O_WRONLY
    flags |= os.O_BINARY
    try:
        crt_descriptor = msvcrt.open_osfhandle(int(duplicated_handle.value), flags)
    except OSError:
        kernel32.CloseHandle(duplicated_handle)
        return None
    try:
        return cast(BinaryIO, os.fdopen(crt_descriptor, mode, buffering=0))
    except (OSError, ValueError):
        with suppress(OSError):
            os.close(crt_descriptor)
        return None


def _run_windows_gated_launcher(arguments: tuple[str, ...]) -> int:
    stdin = _standard_binary_stream(sys.stdin, file_descriptor=0, mode="rb")
    if stdin is None:
        return _GATE_REJECTED_EXIT
    try:
        if stdin.read(1) != b"1":
            return _GATE_REJECTED_EXIT
    except (OSError, ValueError):
        return _GATE_REJECTED_EXIT

    stdout = _standard_binary_stream(sys.stdout, file_descriptor=1, mode="wb")
    stderr = _standard_binary_stream(sys.stderr, file_descriptor=2, mode="wb")
    try:
        child = subprocess.Popen(  # noqa: S603 - argv fixed by the parent protocol
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
        )
    except OSError:
        if stderr is not None:
            try:
                stderr.write(b"contained launcher could not start command\n")
                stderr.flush()
            except (OSError, ValueError):
                pass
        return _LAUNCH_FAILED_EXIT
    return child.wait()


def _run_tex2word(arguments: tuple[str, ...]) -> int:
    try:
        from latex_word_review.backends import _tex2word_worker

        return _tex2word_worker.main(list(arguments))
    except (ImportError, OSError, RuntimeError, UnicodeError, ValueError):
        _write_stderr("tex2word worker failed\n")
        return _WORKER_FAILED_EXIT


def _run_image_renderer(arguments: tuple[str, ...]) -> int:
    try:
        from latex_word_review import _image_worker

        return _image_worker.main(list(arguments))
    except (ContractError, ImportError, OSError, RuntimeError, UnicodeError, ValueError):
        _write_stderr("image render worker failed\n")
        return _WORKER_FAILED_EXIT


def dispatch_worker(worker: InternalWorker | str, arguments: Sequence[str]) -> int:
    """Dispatch one allow-listed worker without accepting module names."""

    try:
        normalized = validate_internal_worker_arguments(worker, arguments)
    except ContractError:
        _write_stderr("internal worker invocation rejected\n")
        return _REJECTED_EXIT
    if worker == "windows-gated-launcher":
        return _run_windows_gated_launcher(normalized)
    if worker == "tex2word":
        return _run_tex2word(normalized)
    if worker == "image-render":
        return _run_image_renderer(normalized)
    _write_stderr("internal worker invocation rejected\n")
    return _REJECTED_EXIT


def dispatch_internal_worker(argv: Sequence[str]) -> int | None:
    """Handle a frozen self-spawn argv or return ``None`` for normal startup.

    A GUI/CLI entry point must call this before importing or parsing its normal
    user interface. Returning an integer means the process is an internal
    worker and must exit with that code.
    """

    normalized = tuple(argv)
    if not normalized or normalized[0] != INTERNAL_WORKER_FLAG:
        return None
    if len(normalized) < 2:
        _write_stderr("internal worker invocation rejected\n")
        return _REJECTED_EXIT
    return dispatch_worker(normalized[1], normalized[2:])


def main(argv: Sequence[str] | None = None) -> int:
    """Development module entry point used by focused protocol tests."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments:
        _write_stderr("internal worker invocation rejected\n")
        return _REJECTED_EXIT
    return dispatch_worker(arguments[0], arguments[1:])


if __name__ == "__main__":  # pragma: no branch - private process entry point
    raise SystemExit(main())


__all__ = ["dispatch_internal_worker", "dispatch_worker", "main"]
