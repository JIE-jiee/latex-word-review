"""Tests for the deliberately small F1 command-line scaffold."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

from latex_word_review import __version__
from latex_word_review.cli import build_parser, main


def test_package_version_is_pep_440_compatible_pre_release() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:a|b|rc)\d+", __version__)


def test_parser_uses_public_command_name() -> None:
    assert build_parser().prog == "latex-word-review"


def test_main_without_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "approval-gated" in output
    assert "--version" in output


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
