"""Conservative source-unit span and stability tests."""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest

import latex_word_review.source_units as source_units_module
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.source_units import build_text_provenance, scan_source_units

FIXTURE_SOURCE = Path(__file__).parent / "fixtures/e0-minimal-paper/source"


def test_scanner_keeps_complete_paragraphs_and_exact_inline_islands(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = """\\documentclass{article}
\\begin{document}
Safe plain paragraph across
two source lines.

Mixed formula $x=1$ makes this whole paragraph unsafe.
This suffix becomes an independent exact island.

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
    assert [unit.normalized_text for unit in first] == [
        "Safe plain paragraph across two source lines.",
        "Mixed formula",
        "makes this whole paragraph unsafe.",
        "This suffix becomes an independent exact island.",
    ]
    unit = first[0]
    assert unit.normalized_text == "Safe plain paragraph across two source lines."
    raw = (source / "main.tex").read_bytes()[unit.start_byte : unit.end_byte]
    assert raw == b"Safe plain paragraph across\ntwo source lines."
    assert unit.source_location()["slice_sha256"] == unit.slice_sha256
    assert unit.unit_id.startswith("unit_")
    assert [segment.transformation for segment in unit.text_provenance] == [
        "identity",
        "whitespace-collapse",
        "identity",
    ]
    assert unit.text_provenance[1].auto_patchable is False
    assert unit.text_provenance[-1].source_end_byte == len(raw)


def test_e0_scanner_is_intentionally_conservative() -> None:
    discovery = discover_project(FIXTURE_SOURCE, main_document="main.tex")
    units = scan_source_units(FIXTURE_SOURCE, discovery)

    normalized = {unit.normalized_text for unit in units}
    assert "The synthetic kinetic energy is defined by" in normalized
    assert "and the balanced response is represented by the two-line system" in normalized
    assert len(units) > 2
    assert all(unit.start_byte < unit.end_byte for unit in units)
    assert all(unit.unit_id.startswith("unit_") for unit in units)


def test_scanner_keeps_provable_plain_text_islands_around_inline_tex(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "The uniquely identifiable introduction uses "
        "\\emph{critical wording} before \\cite{private-key}, "
        "followed by a uniquely identifiable conclusion.\n\n"
        "This formula prefix is visible $secret_math_token$ "
        "and this formula suffix is also visible.\n"
        "\\end{document}\n"
    )
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)

    normalized = [unit.normalized_text for unit in units]
    assert normalized == [
        "The uniquely identifiable introduction uses",
        "critical wording",
        "before",
        ", followed by a uniquely identifiable conclusion.",
        "This formula prefix is visible",
        "and this formula suffix is also visible.",
    ]
    assert all("private-key" not in item for item in normalized)
    assert all("secret_math_token" not in item for item in normalized)
    assert all("emph" not in item and "cite" not in item for item in normalized)
    source_bytes = (source / "main.tex").read_bytes()
    assert [
        source_bytes[unit.start_byte : unit.end_byte].decode("utf-8") for unit in units
    ] == normalized


def test_unbounded_command_disables_all_inline_islands_for_that_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = """\\documentclass{article}
\\begin{document}
Safe prefix before \\unknowncommand{opaque} transformed material must remain manual.

\\begin{quote}
Environment prose must remain manual even when it looks plain.
\\end{quote}
\\end{document}
"""
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)

    assert units == ()


@pytest.mark.parametrize(
    "payload",
    [
        r"\unknowncommand{opaque}",
        "$unclosed math",
        r"\begin{quote}opaque",
    ],
    ids=["unknown-control", "unclosed-math", "unclosed-environment"],
)
def test_untrusted_nested_command_argument_disables_file_v2_islands(
    tmp_path: Path,
    payload: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = "\n".join(
        [
            r"\documentclass{article}",
            r"\begin{document}",
            "Safe paragraph before nested syntax.",
            "",
            rf"\emph{{{payload}}} VisibleSensitiveToken",
            r"\end{document}",
            "",
        ]
    )
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)

    assert [unit.normalized_text for unit in units] == ["Safe paragraph before nested syntax."]
    assert all("VisibleSensitiveToken" not in unit.normalized_text for unit in units)


def test_closed_bounded_metadata_does_not_disable_later_inline_islands(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = "\n".join(
        [
            r"\documentclass{article}",
            r"\begin{document}",
            r"\affiliation{organization={Universit\'{e} Example}, country={France}}",
            r"\maketitle",
            r"\linenumbers",
            r"\added{Structured formula $x=1$ remains manual.}",
            (
                r"Visible prefix before \cite{private-key} continues at 2.0\% "
                r"and ends near \figref{fig:private}."
            ),
            r"\nolinenumbers",
            r"\bibliographystyle{plain}",
            r"\end{document}",
            "",
        ]
    )
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)
    normalized = [unit.normalized_text for unit in units]

    assert normalized == [
        "Visible prefix before",
        "continues at 2.0",
        "and ends near",
    ]
    assert all("private-key" not in item for item in normalized)
    assert all("fig:private" not in item for item in normalized)
    assert all("Structured formula" not in item for item in normalized)


def test_multiline_commands_math_and_environments_never_become_source_units(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = """\\documentclass{article}
\\begin{document}\\begin{equation}
equation body token
\\end{equation}

Safe paragraph before citation.

\\cite{
private-key
}

$$
formula body token
$$

\\[
bracket math token
\\]

Safe paragraph after displayed mathematics.
\\end{document}
"""
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)
    normalized = [unit.normalized_text for unit in units]

    assert normalized == [
        "Safe paragraph before citation.",
        "Safe paragraph after displayed mathematics.",
    ]
    assert all(
        forbidden not in item
        for item in normalized
        for forbidden in (
            "equation body token",
            "private-key",
            "formula body token",
            "bracket math token",
        )
    )


def test_inline_island_fragmentation_is_bounded_and_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    fragmented = ("a~" * 10_001) + "a"
    text = f"\\documentclass{{article}}\n\\begin{{document}}\n{fragmented}\n\\end{{document}}\n"
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    assert scan_source_units(source, discovery) == ()


def test_project_source_unit_limit_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = """\\documentclass{article}
\\begin{document}
Alpha paragraph.

Beta paragraph.

Gamma paragraph.
\\end{document}
"""
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")
    monkeypatch.setattr("latex_word_review.source_units.MAX_SOURCE_UNITS", 2)

    with pytest.raises(ContractError) as raised:
        scan_source_units(source, discovery)

    assert raised.value.code is ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize("command", ["cite", "label", "footnote", "unknowncommand"])
def test_unbraced_cross_line_command_token_never_becomes_a_source_unit(
    tmp_path: Path,
    command: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = (
        r"\documentclass{article}"
        "\n"
        r"\begin{document}"
        "\n"
        "Safe paragraph before the unsafe command.\n\n"
        rf"\{command}"
        "\n"
        "SensitiveParameterToken\n"
        "Visible-looking text after the token must remain manual.\n"
        r"\end{document}"
        "\n"
    )
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)

    assert [unit.normalized_text for unit in units] == ["Safe paragraph before the unsafe command."]
    assert all("SensitiveParameterToken" not in unit.normalized_text for unit in units)


def test_cr_only_source_positions_are_line_and_utf8_accurate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    raw = (
        r"\documentclass{article}"
        "\r"
        r"\begin{document}"
        "\r"
        "中文起点\r"
        "continuation text\r"
        "\r"
        r"\end{document}"
        "\r"
    ).encode()
    (source / "main.tex").write_bytes(raw)
    discovery = discover_project(source, main_document="main.tex")

    units = scan_source_units(source, discovery)

    assert len(units) == 1
    unit = units[0]
    assert unit.normalized_text == "中文起点 continuation text"
    assert (unit.start_line, unit.start_column) == (3, 1)
    assert (unit.end_line, unit.end_column) == (4, len("continuation text") + 1)
    assert raw[unit.start_byte : unit.end_byte] == "中文起点\rcontinuation text".encode()


def test_source_positions_are_resolved_once_per_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    text = r"""\documentclass{article}
\begin{document}
Alpha paragraph.

Beta paragraph.

Gamma paragraph.

\end{document}
"""
    (source / "main.tex").write_text(text, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")
    calls: list[tuple[int, ...]] = []
    original_positions = source_units_module._positions_for_offsets

    def counted_positions(
        data: bytes,
        offsets: tuple[int, ...],
    ) -> dict[int, tuple[int, int]]:
        calls.append(offsets)
        return original_positions(data, offsets)

    monkeypatch.setattr(
        source_units_module,
        "_positions_for_offsets",
        counted_positions,
    )

    units = scan_source_units(source, discovery)

    assert [unit.normalized_text for unit in units] == [
        "Alpha paragraph.",
        "Beta paragraph.",
        "Gamma paragraph.",
    ]
    expected_offsets = tuple(
        offset for unit in units for offset in (unit.start_byte, unit.end_byte)
    )
    assert calls == [expected_offsets]


def test_text_provenance_does_not_allocate_per_character_integer_offsets() -> None:
    raw = b"a" * 500_000

    tracemalloc.start()
    try:
        normalized, segments = build_text_provenance(raw)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert normalized == raw.decode("ascii")
    assert len(segments) == 1
    # A per-character Python int offset table is far larger than the source.
    # Keep platform slack for normalized text while rejecting that old design.
    assert peak < len(raw) * 6


def test_text_provenance_keeps_utf8_identity_bytes_and_marks_collapsed_space() -> None:
    raw = "中文🙂  revised\n\ttext".encode()
    normalized, segments = build_text_provenance(raw)

    assert normalized == "中文🙂 revised text"
    assert [segment.transformation for segment in segments] == [
        "identity",
        "whitespace-collapse",
        "identity",
        "whitespace-collapse",
        "identity",
    ]
    assert segments[0].source_end_byte == len("中文🙂".encode())
    assert segments[-1].source_end_byte == len(raw)
    assert all(
        segment.auto_patchable == (segment.transformation == "identity") for segment in segments
    )
