"""Conservative source-unit span and stability tests."""

from __future__ import annotations

from pathlib import Path

from latex_word_review.discovery import discover_project
from latex_word_review.source_units import scan_source_units

FIXTURE_SOURCE = Path(__file__).parent / "fixtures/e0-minimal-paper/source"


def test_scanner_keeps_only_complete_plain_paragraphs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = """\\documentclass{article}
\\begin{document}
Safe plain paragraph across
two source lines.

Mixed formula $x=1$ makes this whole paragraph unsafe.
This suffix must not become a unit.

\\label{unsafe}

\\begin{equation}
x = 2
\\end{equation}
\\end{document}
"""
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    first = scan_source_units(source, discovery)
    second = scan_source_units(source, discovery)

    assert first == second
    assert len(first) == 1
    unit = first[0]
    assert unit.normalized_text == "Safe plain paragraph across two source lines."
    raw = (source / "main.tex").read_bytes()[unit.start_byte : unit.end_byte]
    assert raw == b"Safe plain paragraph across\ntwo source lines."
    assert unit.source_location()["slice_sha256"] == unit.slice_sha256
    assert unit.unit_id.startswith("unit_")


def test_e0_scanner_is_intentionally_conservative() -> None:
    discovery = discover_project(FIXTURE_SOURCE, main_document="main.tex")
    units = scan_source_units(FIXTURE_SOURCE, discovery)

    assert [unit.normalized_text for unit in units] == [
        "The synthetic kinetic energy is defined by",
        "and the balanced response is represented by the two-line system",
    ]
    assert all(unit.path == "sections/methods.tex" for unit in units)
