"""Contracts for restricted self-spawn workers in PyInstaller builds."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from typing import BinaryIO, cast

import pytest

from latex_word_review import frozen_runtime, worker_dispatch
from latex_word_review.errors import ContractError, ErrorCode

TEX_ARGS = (
    "--source",
    "main.tex",
    "--output",
    "review.docx",
    "--report",
    "report.json",
)
IMAGE_ARGS = (
    "--source",
    "source.pdf",
    "--request",
    "request.json",
    "--output",
    "rendered.png",
    "--report",
    "report.json",
)


def test_development_commands_preserve_existing_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    tex = frozen_runtime.internal_worker_command("tex2word", TEX_ARGS)
    image = frozen_runtime.internal_worker_command("image-render", IMAGE_ARGS)
    launcher = frozen_runtime.internal_worker_command(
        "windows-gated-launcher", ("tool.exe", "--version")
    )

    assert tex.executable == sys.executable
    assert tex.arguments == (
        "-m",
        "latex_word_review.backends._tex2word_worker",
        *TEX_ARGS,
    )
    assert image.arguments == ("-m", "latex_word_review._image_worker", *IMAGE_ARGS)
    assert launcher.arguments[:3] == ("-I", "-S", "-c")
    assert launcher.arguments[-2:] == ("tool.exe", "--version")


@pytest.mark.parametrize(
    ("worker", "arguments"),
    [
        ("tex2word", TEX_ARGS),
        ("image-render", IMAGE_ARGS),
        ("windows-gated-launcher", ("tool.exe", "--version")),
    ],
)
def test_frozen_commands_use_one_hidden_self_spawn_protocol(
    monkeypatch: pytest.MonkeyPatch,
    worker: frozen_runtime.InternalWorker,
    arguments: tuple[str, ...],
) -> None:
    executable = r"C:\Program Files\LatexWordReview\LatexWordReview.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", executable)

    command = frozen_runtime.internal_worker_command(worker, arguments)

    assert command.executable == executable
    assert command.arguments == (
        frozen_runtime.INTERNAL_WORKER_FLAG,
        worker,
        *arguments,
    )
    assert "-m" not in command.arguments


@pytest.mark.parametrize(
    ("worker", "arguments"),
    [
        ("unknown", ()),
        ("tex2word", ("--output", "x", "--source", "y", "--report", "z")),
        ("image-render", IMAGE_ARGS + ("--extra", "x")),
        ("windows-gated-launcher", ()),
        ("windows-gated-launcher", ("bad\x00tool",)),
    ],
)
def test_protocol_rejects_unknown_worker_or_unfixed_argv(
    worker: str,
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(ContractError) as captured:
        frozen_runtime.validate_internal_worker_arguments(worker, arguments)
    assert captured.value.code is ErrorCode.INTERNAL_INVARIANT


def test_frozen_dispatch_is_inert_for_normal_startup_and_rejects_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert worker_dispatch.dispatch_internal_worker(("--version",)) is None
    assert worker_dispatch.dispatch_internal_worker((frozen_runtime.INTERNAL_WORKER_FLAG,)) == 64
    assert (
        worker_dispatch.dispatch_internal_worker(
            (frozen_runtime.INTERNAL_WORKER_FLAG, "arbitrary.module", "--run")
        )
        == 64
    )
    assert capsys.readouterr().err.count("internal worker invocation rejected") == 2


def test_standard_stream_falls_back_when_windowed_bundle_has_no_crt_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = io.BytesIO()

    def missing_descriptor(_descriptor: int) -> int:
        raise OSError

    monkeypatch.setattr(os, "dup", missing_descriptor)
    monkeypatch.setattr(
        worker_dispatch,
        "_windows_standard_binary_stream",
        lambda *, file_descriptor, mode: expected if (file_descriptor, mode) == (1, "wb") else None,
    )

    assert worker_dispatch._standard_binary_stream(None, file_descriptor=1, mode="wb") is expected


def test_dispatch_routes_only_fixed_tex2word_and_image_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from latex_word_review import _image_worker
    from latex_word_review.backends import _tex2word_worker

    observed: list[tuple[str, tuple[str, ...]]] = []

    def record_tex2word(argv: list[str] | None) -> int:
        observed.append(("tex2word", tuple(argv or ())))
        return 7

    def record_image(argv: list[str] | None) -> int:
        observed.append(("image-render", tuple(argv or ())))
        return 8

    monkeypatch.setattr(
        _tex2word_worker,
        "main",
        record_tex2word,
    )
    monkeypatch.setattr(
        _image_worker,
        "main",
        record_image,
    )

    assert worker_dispatch.dispatch_worker("tex2word", TEX_ARGS) == 7
    assert worker_dispatch.dispatch_worker("image-render", IMAGE_ARGS) == 8
    assert observed == [("tex2word", TEX_ARGS), ("image-render", IMAGE_ARGS)]


class _CompletedProcess:
    def wait(self) -> int:
        return 17


def test_gated_launcher_waits_for_release_before_starting_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = {0: io.BytesIO(b"1"), 1: io.BytesIO(), 2: io.BytesIO()}
    observed: dict[str, object] = {}

    def fake_stream(_: object, *, file_descriptor: int, mode: str) -> BinaryIO:
        del mode
        return streams[file_descriptor]

    def fake_popen(arguments: tuple[str, ...], **kwargs: object) -> subprocess.Popen[bytes]:
        observed.update(arguments=arguments, **kwargs)
        return cast("subprocess.Popen[bytes]", _CompletedProcess())

    monkeypatch.setattr(worker_dispatch, "_standard_binary_stream", fake_stream)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert worker_dispatch.dispatch_worker("windows-gated-launcher", ("tool.exe", "--x")) == 17
    assert observed["arguments"] == ("tool.exe", "--x")
    assert observed["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("gate", [b"", b"0"])
def test_gated_launcher_never_starts_child_without_release(
    monkeypatch: pytest.MonkeyPatch,
    gate: bytes,
) -> None:
    monkeypatch.setattr(
        worker_dispatch,
        "_standard_binary_stream",
        lambda *_args, **_kwargs: io.BytesIO(gate),
    )

    def forbidden_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("requested command must remain gated")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    assert worker_dispatch.dispatch_worker("windows-gated-launcher", ("tool.exe",)) == 125
