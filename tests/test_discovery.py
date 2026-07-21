"""Tests for bounded, conservative LaTeX dependency discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

import latex_word_review.discovery as discovery_module
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


def test_discovers_recursive_project_local_runtime_closure(tmp_path: Path) -> None:
    for directory in ("Classes", "Config", "Refs", "Styles"):
        (tmp_path / directory).mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass[\n11pt\n]{././classes/review}\n"
        "\\usepackage[\nmode={a,b}\n]{./styles/review, xcolor}\n"
        "\\bibliographystyle{./refs/review}\n"
        "\\IfFileExists{config/extra.cfg}{}{}\n"
        "\\InputIfFileExists{config/missing.cfg}{}{}\n"
        "\\begin{document}Synthetic\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "Classes" / "Review.CLS").write_text(
        "\\LoadClassWithOptions{classes/base}\n\\RequirePackageWithOptions{styles/nested}\n",
        encoding="utf-8",
    )
    (tmp_path / "Classes" / "Base.CLS").write_text(
        "\\LoadClass{article}\n",
        encoding="utf-8",
    )
    (tmp_path / "Styles" / "Review.STY").write_text(
        "\\newcommand{\\runtimeinput}[2]{\\input{#2}}\n\\RequirePackage{styles/nested}\n",
        encoding="utf-8",
    )
    (tmp_path / "Styles" / "Nested.STY").write_text(
        "\\RequirePackage{styles/review}\n",
        encoding="utf-8",
    )
    (tmp_path / "Styles" / "Probe.STY").write_text("% synthetic\n", encoding="utf-8")
    (tmp_path / "Config" / "Extra.CFG").write_text(
        "\\RequirePackage{styles/probe}\n",
        encoding="utf-8",
    )
    (tmp_path / "Refs" / "Review.BST").write_text("ENTRY{}{}{}\n", encoding="utf-8")

    first = discover_project(tmp_path)
    second = discover_project(tmp_path)

    assert first == second
    assert {item.path for item in first.files} == {
        "Classes/Base.CLS",
        "Classes/Review.CLS",
        "Config/Extra.CFG",
        "Refs/Review.BST",
        "Styles/Nested.STY",
        "Styles/Probe.STY",
        "Styles/Review.STY",
        "main.tex",
    }
    inventory = {item.path: item for item in first.files}
    assert inventory["Classes/Review.CLS"].role == "class"
    assert inventory["Styles/Review.STY"].role == "style"
    assert inventory["Config/Extra.CFG"].media_type == "text/x-tex"
    assert inventory["Refs/Review.BST"].media_type == "text/x-bibtex-style"
    assert all(item.encoding == "utf-8" for item in first.files)
    assert ("main.tex", "Classes/Review.CLS", "class") in {
        (edge.source, edge.target, edge.kind) for edge in first.dependency_edges
    }
    assert ("main.tex", "Refs/Review.BST", "bibliography") in {
        (edge.source, edge.target, edge.kind) for edge in first.dependency_edges
    }
    assert ("Config/Extra.CFG", "Styles/Probe.STY", "package") in {
        (edge.source, edge.target, edge.kind) for edge in first.dependency_edges
    }
    assert first.external_references == ()


@pytest.mark.parametrize(
    ("command", "blocked", "kind", "reason"),
    [
        ("\\usepackage{systempackage}", False, None, None),
        ("\\usepackage{missing.sty}", True, "package", "missing"),
        ("\\usepackage{local/missing}", True, "package", "missing"),
        ("\\usepackage{./missing}", True, "package", "missing"),
        ("\\IfFileExists{local/missing.cfg}{}{}", False, None, None),
        ("\\usepackage{\\dynamicpackage}", True, "package", "dynamic_or_unsafe"),
        ("\\input{#2}", True, "input", "dynamic_or_unsafe"),
    ],
)
def test_runtime_missing_and_dynamic_reference_policy(
    tmp_path: Path,
    command: str,
    blocked: bool,
    kind: str | None,
    reason: str | None,
) -> None:
    (tmp_path / "main.tex").write_text(
        f"\\documentclass{{article}}\n{command}\n\\begin{{document}}x\\end{{document}}\n",
        encoding="utf-8",
    )

    discovery = discover_project(tmp_path)

    assert discovery.blocked is blocked
    if blocked:
        assert [(item.kind, item.reason) for item in discovery.external_references] == [
            (kind, reason)
        ]
    else:
        assert discovery.external_references == ()


def test_runtime_casefold_collision_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "Styles").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{styles/review}\n"
        "\\begin{document}x\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "Styles" / "Review.STY").write_text("% fixture\n", encoding="utf-8")
    real_entries = discovery_module._directory_entries

    def duplicate_casefold_entry(
        state: object,
        directory: Path,
        relative: str,
    ) -> tuple[Path, ...]:
        entries = real_entries(state, directory, relative)  # type: ignore[arg-type]
        if relative == "Styles":
            return (*entries, directory / "REVIEW.STY")
        return entries

    monkeypatch.setattr(discovery_module, "_directory_entries", duplicate_casefold_entry)

    with pytest.raises(ContractError) as raised:
        discover_project(tmp_path)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_non_utf8_local_runtime_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{local}\n\\begin{document}x\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "local.sty").write_bytes(b"\\ProvidesPackage{local}\n% \xff")

    with pytest.raises(ContractError) as raised:
        discover_project(tmp_path)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_repeated_current_directory_prefixes_resolve_for_input_and_graphics(
    tmp_path: Path,
) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "figures").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\graphicspath{{././figures/}}\n"
        "\\begin{document}\\input{././sections/body}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "sections" / "body.tex").write_text(
        "\\includegraphics{././plot}\n",
        encoding="utf-8",
    )
    (tmp_path / "figures" / "plot.png").write_bytes(b"fixture")

    discovery = discover_project(tmp_path)

    assert {item.path for item in discovery.files} == {
        "figures/plot.png",
        "main.tex",
        "sections/body.tex",
    }
    assert {(edge.kind, edge.target) for edge in discovery.dependency_edges} == {
        ("graphic", "figures/plot.png"),
        ("input", "sections/body.tex"),
    }
    assert discovery.external_references == ()


@pytest.mark.parametrize(
    ("reference", "reason"),
    [
        ("./../secret", "path_rejected"),
        ("./C:/secret", "path_rejected"),
        ("./folder\\secret", "dynamic_or_unsafe"),
    ],
)
def test_current_directory_prefix_does_not_relax_unsafe_paths(
    tmp_path: Path,
    reference: str,
    reason: str,
) -> None:
    (tmp_path / "main.tex").write_text(
        f"\\documentclass{{article}}\n\\begin{{document}}\\input{{{reference}}}\\end{{document}}\n",
        encoding="utf-8",
    )

    discovery = discover_project(tmp_path)

    assert discovery.blocked is True
    assert len(discovery.external_references) == 1
    external = discovery.external_references[0]
    assert external.kind == "input"
    assert external.reason == reason
    assert external.reference.startswith("unsafe-path:")


def test_current_directory_prefix_still_enforces_link_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\\input{./linked/secret}\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "secret.tex").write_text("fixture", encoding="utf-8")

    real_link_probe = discovery_module._is_link_or_junction

    def simulate_link(path: Path) -> bool:
        return path.name == "linked" or real_link_probe(path)

    monkeypatch.setattr(discovery_module, "_is_link_or_junction", simulate_link)

    discovery = discover_project(tmp_path)

    assert discovery.blocked is True
    assert len(discovery.external_references) == 1
    external = discovery.external_references[0]
    assert external.kind == "input"
    assert external.reason == "link_escape"
    assert external.reference == "linked/secret.tex"


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


def test_discovers_one_redundant_literal_graphic_group(tmp_path: Path) -> None:
    figure_name = "3.Stiffness\N{EN DASH}drift response curve under NC loading.pdf"
    (tmp_path / "Figure").mkdir()
    (tmp_path / "Figure" / figure_name).write_bytes(b"synthetic-pdf")
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\graphicspath{{Figure/}}\n"
        "\\begin{document}\n"
        f"\\includegraphics{{{{{figure_name}}}}}\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    discovery = discover_project(tmp_path, main_document="main.tex")

    assert {item.path for item in discovery.files} == {
        "main.tex",
        f"Figure/{figure_name}",
    }
    assert {(edge.kind, edge.target) for edge in discovery.dependency_edges} == {
        ("graphic", f"Figure/{figure_name}"),
    }
    assert discovery.external_references == ()
    assert discovery.blocked is False


@pytest.mark.parametrize(
    "command",
    [
        r"\includegraphics{{{plot.pdf}}}",
        r"\includegraphics{{plot.pdf}suffix}",
        r"\includegraphics{prefix{plot.pdf}}",
        r"\includegraphics{\jobname.pdf}",
        r"\includegraphics{{C:/private/plot.pdf}}",
        r"\includegraphics{{\\server\share\plot.pdf}}",
        r"\includegraphics{{../plot.pdf}}",
    ],
)
def test_grouped_dynamic_and_unsafe_graphics_still_block_discovery(
    tmp_path: Path,
    command: str,
) -> None:
    (tmp_path / "main.tex").write_text(
        f"\\documentclass{{article}}\n\\begin{{document}}\n{command}\n\\end{{document}}\n",
        encoding="utf-8",
        newline="\n",
    )

    discovery = discover_project(tmp_path, main_document="main.tex")

    assert discovery.blocked is True
    assert [(item.kind, item.status) for item in discovery.external_references] == [
        ("graphic", "rejected")
    ]
