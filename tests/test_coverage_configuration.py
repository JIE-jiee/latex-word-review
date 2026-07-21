"""Contracts for complete branch-coverage collection across worker processes."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_coverage_gate_collects_subprocesses_without_weakening_threshold() -> None:
    """Keep real CLI and worker subprocess execution inside the coverage gate."""

    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)

    coverage = config["tool"]["coverage"]
    assert coverage["run"]["branch"] is True
    assert coverage["run"]["source"] == ["latex_word_review"]
    assert coverage["run"]["patch"] == ["subprocess"]
    assert coverage["report"]["fail_under"] == 90
