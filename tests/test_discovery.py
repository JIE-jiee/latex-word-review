"""Tests for bounded, conservative LaTeX dependency discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from latex_word_review.discovery import DiscoveryLimits, discover_project
from latex_word_review.errors import ContractError, ErrorCode


def _write_unicode_project(root: Path) -> None:
    (root / "章节").mkdir(parents=True)
    (root / "图").mkdir()
    (root / "main.tex").write_text(
        """\\documentclass{article}
\\graphicspath{{图/}}
\\begin{document}
\\input{章节/intro}
\\bibliography{refs}
\\includegraphics{曲线}
\\end{document}
""",
        encoding="utf-8",
        newline="\n",
    )
    (root / "章节" / "intro.tex").write_text("Unicode 正文。\n", encoding="utf-8")
    (root / "refs.bib").write_text("@misc{fixture,title={Synthetic}}\n", encoding="utf-8")
    (root / "图" / "曲线.png").write_bytes(b"synthetic-png-bytes")


def test_discovers_main_inputs_bibliography_graphics_and_unicode(tmp_path: Path) -> None:
    _write_unicode_project(tmp_path)
    first = discover_project(tmp_path)
    second = discover_project(tmp_path)

    assert first == second
    assert first.main_document == "main.tex"
    assert [item.path for item in first.files] == [
        "main.tex",
        "refs.bib",
        "图/曲线.png",
        "章节/intro.tex",
    ]
    assert {(edge.kind, edge.target) for edge in first.dependency_edges} == {
        ("input", "章节/intro.tex"),
        ("bibliography", "refs.bib"),
        ("graphic", "图/曲线.png"),
    }
    assert first.external_references == ()
    assert first.blocked is False


def test_unsafe_reference_is_redacted_and_blocks_discovery(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{../secret}\n\\end{document}\n",
        encoding="utf-8",
    )
    discovery = discover_project(tmp_path)
    assert discovery.blocked is True
    assert len(discovery.external_references) == 1
    reference = discovery.external_references[0].reference
    assert reference.startswith("unsafe-path:")
    assert ".." not in reference


def test_graphicspath_is_root_relative_for_an_included_source(tmp_path: Path) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "figures").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\graphicspath{{figures/}}\n"
        "\\begin{document}\\input{sections/body}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "sections" / "body.tex").write_text(
        "\\includegraphics{plot}\n",
        encoding="utf-8",
    )
    (tmp_path / "figures" / "plot.png").write_bytes(b"fixture")

    discovery = discover_project(tmp_path)

    assert "figures/plot.png" in {item.path for item in discovery.files}
    assert discovery.external_references == ()


def test_graphicspath_appends_extension_after_dotted_graphic_basename(
    tmp_path: Path,
) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "figures").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\\input{sections/body}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "sections" / "body.tex").write_text(
        "\\graphicspath{{figures/}}\n\\includegraphics{response.v2}\n",
        encoding="utf-8",
    )
    (tmp_path / "figures" / "response.v2.pdf").write_bytes(b"fixture")

    discovery = discover_project(tmp_path)

    assert "figures/response.v2.pdf" in {item.path for item in discovery.files}
    assert discovery.external_references == ()


def test_dotted_graphic_basename_with_two_real_candidates_is_ambiguous(
    tmp_path: Path,
) -> None:
    (tmp_path / "figures").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\graphicspath{{figures/}}\n"
        "\\begin{document}\\includegraphics{response.v2}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "figures" / "response.v2").write_bytes(b"exact")
    (tmp_path / "figures" / "response.v2.pdf").write_bytes(b"with-extension")

    discovery = discover_project(tmp_path)

    assert discovery.blocked is True
    assert len(discovery.external_references) == 1
    assert discovery.external_references[0].kind == "graphic"
    assert discovery.external_references[0].reason == "ambiguous"


def test_plain_graphic_reference_in_included_file_is_source_root_relative(
    tmp_path: Path,
) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\\input{sections/body}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "sections" / "body.tex").write_text(
        "\\includegraphics{assets/plot.png}\n",
        encoding="utf-8",
    )
    (tmp_path / "assets" / "plot.png").write_bytes(b"fixture")

    discovery = discover_project(tmp_path)

    assert "assets/plot.png" in {item.path for item in discovery.files}
    assert discovery.external_references == ()


def test_unsafe_graphicspath_blocks_even_without_graphic_reference(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\graphicspath{{../private/}}\n"
        "\\begin{document}text\\end{document}\n",
        encoding="utf-8",
    )

    discovery = discover_project(tmp_path)

    assert discovery.blocked is True
    assert discovery.external_references[0].kind == "graphicspath"
    assert discovery.external_references[0].reference.startswith("unsafe-path:")


def test_non_utf8_bibliography_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\\bibliography{refs}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "refs.bib").write_bytes(b"@misc{x,title={\xff}}")

    with pytest.raises(ContractError) as raised:
        discover_project(tmp_path)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_discovery_enforces_file_count_limit(tmp_path: Path) -> None:
    _write_unicode_project(tmp_path)
    limits = DiscoveryLimits(max_files=2, max_file_bytes=1024, max_total_bytes=4096)
    with pytest.raises(ContractError) as raised:
        discover_project(tmp_path, limits=limits)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_main_document_must_be_unambiguous(tmp_path: Path) -> None:
    for name in ("first.tex", "second.tex"):
        (tmp_path / name).write_text(
            "\\documentclass{article}\n\\begin{document}x\\end{document}\n",
            encoding="utf-8",
        )
    with pytest.raises(ContractError) as raised:
        discover_project(tmp_path)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_main_fallback_enumeration_is_bounded(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"note-{index}.txt").write_text("fixture", encoding="utf-8")

    limits = DiscoveryLimits(
        max_files=2,
        max_file_bytes=1024,
        max_total_bytes=4096,
        max_dependencies_per_file=16,
    )
    with pytest.raises(ContractError) as raised:
        discover_project(tmp_path, limits=limits)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
