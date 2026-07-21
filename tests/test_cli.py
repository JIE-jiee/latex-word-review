"""Tests for the deliberately small F1 command-line scaffold."""

from __future__ import annotations

import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

import latex_word_review.cli as cli_module
from latex_word_review import __version__
from latex_word_review.cli import build_parser, main
from latex_word_review.errors import ContractError, ErrorCode, ExitCode


def test_package_version_is_pep_440_compatible_pre_release() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:a|b|rc)\d+", __version__)


def test_parser_uses_public_command_name() -> None:
    assert build_parser().prog == "latex-word-review"


def test_main_without_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "approval-gated" in output
    assert "--version" in output


@pytest.mark.parametrize("error", [False, True])
def test_emit_is_safe_without_windowed_console_streams(
    monkeypatch: pytest.MonkeyPatch, error: bool
) -> None:
    monkeypatch.setattr(sys, "stderr" if error else "stdout", None)
    cli_module._emit({"status": "safe"}, error=error)


class _FailingStream:
    def __init__(self, *, binary: bool, operation: str, error_type: type[Exception]) -> None:
        self.buffer = self if binary else None
        self.operation = operation
        self.error_type = error_type

    def write(self, _data: object) -> None:
        if self.operation == "write":
            raise self.error_type()

    def flush(self) -> None:
        if self.operation == "flush":
            raise self.error_type()


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_emit_is_safe_when_console_write_or_flush_fails(
    monkeypatch: pytest.MonkeyPatch,
    binary: bool,
    operation: str,
    error_type: type[Exception],
) -> None:
    stream = _FailingStream(binary=binary, operation=operation, error_type=error_type)
    monkeypatch.setattr(sys, "stdout", stream)
    cli_module._emit({"status": "safe"})


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            ContractError(ErrorCode.EXPORT_SILENT_LOSS, "synthetic primary failure"),
            int(ExitCode.BACKEND_OR_EXPORT),
        ),
        (OSError("synthetic private failure"), int(ExitCode.INTERNAL)),
    ],
)
def test_main_preserves_exit_code_without_error_stream(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: int,
) -> None:
    def fail(_args: object) -> int:
        raise failure

    parser = SimpleNamespace(parse_args=lambda _argv: SimpleNamespace(handler=fail))
    monkeypatch.setattr(cli_module, "build_parser", lambda: parser)
    monkeypatch.setattr(sys, "stderr", None)

    assert cli_module.main([]) == expected


def test_python_module_version_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "latex_word_review", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"latex-word-review {__version__}"
    assert completed.stderr == ""
