"""Boundary coverage for the frozen worker dispatcher without real child processes."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from typing import Any, TextIO, cast

import pytest

from latex_word_review import frozen_runtime, worker_dispatch


class _Function:
    def __init__(self, implementation: Any) -> None:
        self.implementation = implementation
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *arguments: object) -> object:
        return self.implementation(*arguments)


class _Kernel32:
    def __init__(self, *, handle: object = 41, duplicate: bool = True) -> None:
        self.closed: list[object] = []
        self.GetStdHandle = _Function(lambda _kind: handle)
        self.GetCurrentProcess = _Function(lambda: 7)

        def duplicate_handle(*arguments: object) -> bool:
            if duplicate:
                arguments[3]._obj.value = 99  # type: ignore[attr-defined]
            return duplicate

        self.DuplicateHandle = _Function(duplicate_handle)
        self.CloseHandle = _Function(self._close)

    def _close(self, handle: object) -> bool:
        self.closed.append(handle)
        return True


def test_write_stderr_tolerates_windowed_and_broken_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    worker_dispatch._write_stderr("ignored")

    class Broken:
        def write(self, _message: str) -> None:
            raise ValueError

        def flush(self) -> None:
            raise AssertionError

    monkeypatch.setattr(sys, "stderr", Broken())
    worker_dispatch._write_stderr("ignored")


def test_standard_binary_stream_prefers_buffer_then_opens_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = io.BytesIO()

    class Text:
        buffer = expected

    assert (
        worker_dispatch._standard_binary_stream(
            cast("TextIO", Text()), file_descriptor=1, mode="wb"
        )
        is expected
    )

    monkeypatch.setattr(os, "dup", lambda descriptor: descriptor + 10)
    monkeypatch.setattr(os, "fdopen", lambda *_args, **_kwargs: expected)
    assert worker_dispatch._standard_binary_stream(None, file_descriptor=1, mode="wb") is expected


@pytest.mark.parametrize(
    ("file_descriptor", "mode"),
    [(9, "wb"), (1, "w")],
)
def test_windows_standard_stream_rejects_unknown_descriptor_or_mode(
    file_descriptor: int,
    mode: str,
) -> None:
    assert (
        worker_dispatch._windows_standard_binary_stream(
            file_descriptor=file_descriptor,
            mode=mode,
        )
        is None
    )


def test_windows_standard_stream_duplicates_a_valid_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import msvcrt

    kernel32 = _Kernel32()
    expected = io.BytesIO()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)
    monkeypatch.setattr(msvcrt, "open_osfhandle", lambda handle, flags: handle + flags)
    monkeypatch.setattr(os, "fdopen", lambda *_args, **_kwargs: expected)

    assert worker_dispatch._windows_standard_binary_stream(file_descriptor=1, mode="wb") is expected


@pytest.mark.parametrize("handle", [None, 0, 18446744073709551615])
def test_windows_standard_stream_rejects_invalid_handle(
    monkeypatch: pytest.MonkeyPatch,
    handle: object,
) -> None:
    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: _Kernel32(handle=handle))
    assert worker_dispatch._windows_standard_binary_stream(file_descriptor=0, mode="rb") is None


def test_windows_standard_stream_rejects_failed_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: _Kernel32(duplicate=False),
    )
    assert worker_dispatch._windows_standard_binary_stream(file_descriptor=2, mode="wb") is None


def test_windows_standard_stream_closes_handle_when_crt_adoption_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import msvcrt

    kernel32 = _Kernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)
    monkeypatch.setattr(msvcrt, "open_osfhandle", lambda *_args: (_ for _ in ()).throw(OSError()))

    assert worker_dispatch._windows_standard_binary_stream(file_descriptor=1, mode="wb") is None
    assert len(kernel32.closed) == 1


def test_windows_standard_stream_closes_crt_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import msvcrt

    closed: list[int] = []
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: _Kernel32())
    monkeypatch.setattr(msvcrt, "open_osfhandle", lambda *_args: 123)
    monkeypatch.setattr(os, "fdopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(os, "close", closed.append)

    assert worker_dispatch._windows_standard_binary_stream(file_descriptor=1, mode="wb") is None
    assert closed == [123]


@pytest.mark.parametrize("failure", [OSError(), ValueError()])
def test_gated_launcher_rejects_unreadable_gate(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    class BrokenGate:
        def read(self, _size: int) -> bytes:
            raise failure

    monkeypatch.setattr(
        worker_dispatch,
        "_standard_binary_stream",
        lambda *_args, **_kwargs: BrokenGate(),
    )
    assert worker_dispatch._run_windows_gated_launcher(("tool.exe",)) == 125


def test_gated_launcher_handles_missing_gate_and_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_dispatch, "_standard_binary_stream", lambda *_a, **_k: None)
    assert worker_dispatch._run_windows_gated_launcher(("tool.exe",)) == 125

    streams = {0: io.BytesIO(b"1"), 1: None, 2: io.BytesIO()}
    monkeypatch.setattr(
        worker_dispatch,
        "_standard_binary_stream",
        lambda _stream, *, file_descriptor, mode: streams[file_descriptor],
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    assert worker_dispatch._run_windows_gated_launcher(("tool.exe",)) == 126
    stderr = streams[2]
    assert stderr is not None
    assert b"could not start" in stderr.getvalue()


def test_worker_failures_are_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    from latex_word_review import _image_worker
    from latex_word_review.backends import _tex2word_worker

    monkeypatch.setattr(_tex2word_worker, "main", lambda _argv: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(_image_worker, "main", lambda _argv: (_ for _ in ()).throw(RuntimeError()))

    tex_args = (
        "--source",
        "a",
        "--output",
        "b",
        "--report",
        "c",
        "--revision-view",
        "source",
        "--revision-aliases",
        "none",
    )
    image_args = (
        "--source",
        "a",
        "--request",
        "b",
        "--output",
        "c",
        "--report",
        "d",
    )
    assert worker_dispatch.dispatch_worker("tex2word", tex_args) == 22
    assert worker_dispatch.dispatch_worker("image-render", image_args) == 22


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("none", ()),
        ("add", ("add",)),
        ("delete", ("delete",)),
        ("add,delete", ("add", "delete")),
    ],
)
def test_tex2word_worker_revision_alias_selector_contract(
    selector: str,
    expected: tuple[str, ...],
) -> None:
    from latex_word_review.backends import _tex2word_worker

    assert _tex2word_worker._parse_revision_aliases(selector) == expected

    if selector == "none":
        with pytest.raises(ValueError, match="invalid revision alias selector"):
            _tex2word_worker._parse_revision_aliases("delete,add")


def test_main_rejects_empty_and_dispatches_explicit_or_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[object, tuple[str, ...]]] = []

    def fake_dispatch(worker: object, arguments: tuple[str, ...]) -> int:
        observed.append((worker, tuple(arguments)))
        return 9

    monkeypatch.setattr(worker_dispatch, "dispatch_worker", fake_dispatch)

    assert worker_dispatch.main(()) == 64
    assert worker_dispatch.main(("tex2word", "x")) == 9
    monkeypatch.setattr(sys, "argv", ["dispatcher", "image-render", "y"])
    assert worker_dispatch.main() == 9
    assert observed == [("tex2word", ("x",)), ("image-render", ("y",))]


def test_internal_dispatch_accepts_the_allow_list_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_dispatch, "dispatch_worker", lambda worker, arguments: 13)
    assert (
        worker_dispatch.dispatch_internal_worker(
            (frozen_runtime.INTERNAL_WORKER_FLAG, "tex2word", "x")
        )
        == 13
    )
