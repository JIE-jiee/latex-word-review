"""Bounded ``changes``-style macro inventory and tex2word view tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tex2word.frontend.macros import expand_macros

from latex_word_review import revision_macros as revision_macros_module
from latex_word_review.canonical import sha256_bytes
from latex_word_review.discovery import SourceFile, discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.revision_macros import (
    REVISION_MACRO_PROFILE_ID,
    REVISION_MACRO_PROFILE_NAME,
    REVISION_MACRO_PROFILE_VERSION,
    RevisionMacroInventory,
    RevisionMacroMode,
    inject_revision_macros,
    scan_revision_macros,
)


def _project(tmp_path: Path, main: str, *, included: str | None = None) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(main, encoding="utf-8", newline="\n")
    if included is not None:
        (source / "section.tex").write_text(included, encoding="utf-8", newline="\n")
    return source


def _scan(source: Path) -> RevisionMacroInventory:
    discovery = discover_project(source, main_document="main.tex")
    return scan_revision_macros(source, discovery)


def test_inventory_counts_active_canonical_and_alias_macros_across_inputs(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\usepackage{trackchanges}
\newcommand{\delete}[2][]{\deleted[#1]{#2}}
\added{inactive preamble}
\begin{document}
\added[id=AA, comment={new [nested]}]{新增🙂 {with braces}}
\deleted[comment={旧的说明}]{旧内容}
\replaced[id=AA]{新内容}{旧内容}
\input{section}
\end{document}
\delete{inactive tail}
""",
        included=r"""\add[comment={short alias}]{别名新增}
\delete{别名删除}
""",
    )

    inventory = _scan(source)

    assert inventory.source_tree_sha256.startswith("sha256:")
    assert inventory.tex_files == 2
    assert inventory.canonical_counts.as_dict() == {
        "added": 1,
        "deleted": 1,
        "replaced": 1,
    }
    assert inventory.alias_counts.as_dict() == {"add": 1, "delete": 1}
    assert inventory.total == 5
    assert inventory.skipped_dynamic_regions == 0
    assert inventory.as_dict() == {
        "profile": {
            "name": REVISION_MACRO_PROFILE_NAME,
            "version": REVISION_MACRO_PROFILE_VERSION,
        },
        "source_tree_sha256": inventory.source_tree_sha256,
        "tex_files": 2,
        "canonical_counts": {"added": 1, "deleted": 1, "replaced": 1},
        "alias_counts": {"add": 1, "delete": 1},
        "total": 5,
        "skipped_dynamic_regions": 0,
        "expected_display_expectations": 5,
        "expected_blue_text_characters": 32,
        "expected_strike_text_characters": 10,
    }
    assert inventory.as_metrics() == {
        "revision_macro_inventory_version": 2,
        "revision_macro_tex_files": 2,
        "revision_macros_total": 5,
        "revision_macros_added": 1,
        "revision_macros_deleted": 1,
        "revision_macros_replaced": 1,
        "revision_macros_alias_add": 1,
        "revision_macros_alias_delete": 1,
        "revision_macro_skipped_dynamic_regions": 0,
        "revision_display_expected_expectations": 5,
        "revision_display_expected_blue_text_characters": 32,
        "revision_display_expected_strike_text_characters": 10,
    }
    assert len(inventory.display_expectations) == 5
    assert sum(item.macro_instances for item in inventory.display_expectations) == 5
    assert all(
        item.source_call_sha256.startswith("sha256:") for item in inventory.display_expectations
    )


def test_scanner_skips_comments_verbatim_definitions_and_dynamic_regions(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\newcommand{\fake}[1]{\added{#1}}
\NewDocumentCommand{\xfake}{m}{\deleted{#1}}
\newenvironment{fakeenv}{\added{begin}}{\deleted{end}}
\NewDocumentEnvironment{xfakeenv}{m}{\add{begin}}{\delete{end}}
\def\primitive#1{\replaced{new}{#1}}
\begin{document}
% \added{comment}
\verb|\deleted{inline verbatim}|
\begin{verbatim}
\replaced{verbatim new}{verbatim old}
\end{verbatim}
\begin{minted}{text}
\add{minted}
\end{minted}
\begin{lstlisting}
\delete{listing}
\end{lstlisting}
\ifdefined\never
\added{conditional}
\else
\deleted{alternate}
\fi
\ifthenelse{\boolean{flag}}{\add{true}}{\delete{false}}
\added{counted}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.as_dict() == {
        "added": 1,
        "deleted": 0,
        "replaced": 0,
    }
    assert inventory.alias_counts.as_dict() == {"add": 0, "delete": 0}
    assert inventory.total == 1
    assert inventory.skipped_dynamic_regions == 2


def test_scanner_counts_nested_revision_macros_with_unicode_arguments(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\usepackage{trackchanges}
\newcommand{\delete}[1]{\deleted{#1}}
\begin{document}
\added{外层🙂 \deleted{内层 {嵌套大括号}}}
\replaced{新 \add{补充}}{旧 \delete{删减}}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.as_dict() == {
        "added": 1,
        "deleted": 1,
        "replaced": 1,
    }
    assert inventory.alias_counts.as_dict() == {"add": 1, "delete": 1}
    assert inventory.total == 5


def test_display_projection_binds_source_and_orders_replacement_old_before_new(
    tmp_path: Path,
) -> None:
    calls = (
        r"\added{outer \deleted{inner}}",
        r"\replaced{new \added{plus}}{old \deleted{gone}}",
    )
    main = (
        "\\documentclass{article}\n\\usepackage{changes}\n\\begin{document}\n"
        f"{calls[0]}\n{calls[1]}\n"
        "\\end{document}\n"
    )
    source = _project(tmp_path, main)

    inventory = _scan(source)

    assert inventory.total == 5
    assert len(inventory.display_expectations) == 2
    first, second = inventory.display_expectations
    assert first.text == "outer inner"
    assert first.strike_mask == (False,) * 6 + (True,) * 5
    assert first.macro_instances == 2
    assert second.text == "old gonenew plus"
    assert second.strike_mask == (True,) * 8 + (False,) * 8
    assert second.macro_instances == 3
    for expectation, call in zip(inventory.display_expectations, calls, strict=True):
        offset = expectation.source_character_offset
        assert main[offset : offset + len(call)] == call
        assert expectation.source_call_sha256 == sha256_bytes(call.encode("utf-8"))


def test_empty_revision_macros_remain_covered_without_visible_style_claims(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n\\usepackage{changes}\n\\begin{document}\n"
        "\\added{}\\deleted{}\\replaced{}{}\n"
        "\\end{document}\n",
    )

    inventory = _scan(source)

    assert inventory.total == 3
    assert len(inventory.display_expectations) == 3
    assert all(not expectation.text for expectation in inventory.display_expectations)
    assert sum(expectation.macro_instances for expectation in inventory.display_expectations) == 3
    assert inventory.expected_display_expectations == 0
    assert inventory.expected_blue_text_characters == 0
    assert inventory.expected_strike_text_characters == 0


def test_cr_only_comments_do_not_hide_following_revision_macros(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    main = (
        "\\documentclass{article}\r\\usepackage{changes}\r\\begin{document}\r"
        "% \\deleted{comment}\r\\added{visible}\r\\end{document}\r"
    )
    (source / "main.tex").write_bytes(main.encode("utf-8"))

    inventory = _scan(source)

    assert inventory.total == 1
    assert inventory.display_expectations[0].text == "visible"


def test_line_column_lookup_uses_precomputed_line_starts() -> None:
    scanner = revision_macros_module._FileScanner(
        "zero\n一二\nlast",
        "main.tex",
        revision_macros_module._MutableCounts(),
    )

    assert scanner._line_starts == (0, 5, 8)
    assert [scanner._line_column(index) for index in (0, 4, 5, 6, 7, 8, 12)] == [
        (1, 1),
        (1, 5),
        (2, 1),
        (2, 2),
        (2, 3),
        (3, 1),
        (3, 5),
    ]


def test_scanner_accepts_unique_proven_alias_sources(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\usepackage{trackchanges}
\NewDocumentCommand{\delete}{O{} m}{\deleted[#1]{#2}}
\begin{document}
\add[editor]{package-backed addition}
\delete[id=reviewer]{locally wrapped deletion}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.alias_counts.as_dict() == {"add": 1, "delete": 1}


@pytest.mark.parametrize(
    "inert_definition",
    [
        r"\newcommand{\factory}{\let\delete\deleted}",
        r"\NewDocumentCommand{\factory}{m}{\let\delete\deleted}",
        r"\def\factory{\let\delete\deleted}",
        r"\newenvironment{factory}{\let\delete\deleted}{}",
    ],
)
def test_alias_audit_skips_complete_non_alias_definitions(
    tmp_path: Path,
    inert_definition: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        f"{inert_definition}\n"
        "\\begin{document}\n"
        "\\delete{content}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "no accepted static semantic source" in str(caught.value)


def test_alias_audit_stops_at_endinput(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{local}
\begin{document}
\delete{content}
\end{document}
""",
    )
    (source / "local.sty").write_text(
        "\\ProvidesPackage{local}\n\\endinput\n\\let\\delete\\deleted\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "no accepted static semantic source" in str(caught.value)


@pytest.mark.parametrize(
    ("command", "definition", "call"),
    [
        ("added", r"\newcommand{\added}[1]{custom #1}", r"\added{content}"),
        (
            "deleted",
            r"\NewDocumentCommand{\deleted}{m}{custom #1}",
            r"\deleted{content}",
        ),
        (
            "replaced",
            r"\def\replaced#1#2{custom #1/#2}",
            r"\replaced{new}{old}",
        ),
        ("added", r"\let\added\textbf", r"\added{content}"),
    ],
)
def test_scanner_rejects_source_defined_canonical_revision_command(
    tmp_path: Path,
    command: str,
    definition: str,
    call: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        f"{definition}\n"
        "\\begin{document}\n"
        f"{call}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "canonical revision command conflicts" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["command"] == f"\\{command}"
    assert caught.value.violation.details["definition_command"]


def test_scanner_rejects_alias_wrapper_around_custom_canonical_command(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\newcommand{\deleted}[1]{custom #1}
\newcommand{\delete}[1]{\deleted{#1}}
\begin{document}
\delete{content}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "canonical revision command conflicts" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["command"] == r"\deleted"


def test_scanner_accepts_changes_package_and_unredefined_canonical_calls(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\newcommand{\factory}{\let\added\textbf}
\begin{document}
\added{new}\deleted{old}\replaced{current}{prior}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.as_dict() == {
        "added": 1,
        "deleted": 1,
        "replaced": 1,
    }


@pytest.mark.parametrize(
    ("command", "call"),
    [
        ("added", r"\added{content}"),
        ("deleted", r"\deleted{content}"),
        ("replaced", r"\replaced{new}{old}"),
    ],
)
def test_scanner_rejects_bare_canonical_without_static_changes_declaration(
    tmp_path: Path,
    command: str,
    call: str,
) -> None:
    source = _project(
        tmp_path,
        f"\\documentclass{{article}}\n\\begin{{document}}\n{call}\n\\end{{document}}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "no accepted static changes package declaration" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["command"] == f"\\{command}"


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("add", "added"), ("delete", "deleted")],
)
def test_alias_wrapper_cannot_supply_missing_static_changes_declaration(
    tmp_path: Path,
    alias: str,
    canonical: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        f"\\newcommand{{\\{alias}}}[1]{{\\{canonical}{{#1}}}}\n"
        "\\begin{document}\n"
        f"\\{alias}{{content}}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "no accepted static changes package declaration" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["command"] == f"\\{canonical}"


def test_local_style_can_statically_load_changes_package(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{reviewstyle}
\begin{document}
\added{content}
\end{document}
""",
    )
    (source / "reviewstyle.sty").write_text(
        "\\ProvidesPackage{reviewstyle}\n\\RequirePackage{changes}\n\\endinput\n",
        encoding="utf-8",
        newline="\n",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.added == 1


def test_dynamic_changes_package_load_does_not_prove_canonical_source(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\ifdefined\flag
  \usepackage{changes}
\fi
\begin{document}
\added{content}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "canonical revision command conflicts" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["source_kind"] == "dynamic_changes_package"


@pytest.mark.parametrize(
    ("command", "definition", "call"),
    [
        ("deleted", r"\csdef{deleted}#1{custom #1}", r"\deleted{content}"),
        ("deleted", r"\cseappto{deleted}{custom}", r"\deleted{content}"),
        ("deleted", r"\csgundef{deleted}", r"\deleted{content}"),
        ("added", r"\appto\added{custom}", r"\added{content}"),
        (
            "added",
            r"\patchcmd{\added}{old}{new}{}{}",
            r"\added{content}",
        ),
        ("deleted", r"\letcs\deleted{other}", r"\deleted{content}"),
        ("deleted", r"\robustify\deleted", r"\deleted{content}"),
        ("deleted", r"\undef\deleted", r"\deleted{content}"),
        (
            "replaced",
            r"\expandafter\def\csname replaced\endcsname#1#2{custom}",
            r"\replaced{new}{old}",
        ),
    ],
)
def test_scanner_rejects_dynamic_or_patch_redefinition_of_canonical_command(
    tmp_path: Path,
    command: str,
    definition: str,
    call: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        f"{definition}\n"
        "\\begin{document}\n"
        f"{call}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "canonical revision command conflicts" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["command"] == f"\\{command}"
    assert caught.value.violation.details["source_kind"] in {
        "computed_definition",
        "patch_definition",
    }


def test_unrelated_etoolbox_dynamic_definitions_do_not_create_false_conflicts(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\csdef{helper}#1{#1}
\cslet{helpercopy}\helper
\robustify\helper
\appto\helper{tail}
\begin{document}
\added{content}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.added == 1


def test_scanner_rejects_project_local_changes_package_shadow(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\begin{document}
\added{content}
\end{document}
""",
    )
    (source / "changes.sty").write_text(
        "\\ProvidesPackage{changes}\n\\endinput\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "project-local changes.sty" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["local_package_paths"] == ["changes.sty"]


def test_scanner_rejects_project_local_trackchanges_package_shadow(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{trackchanges}
\begin{document}
\add{content}
\end{document}
""",
    )
    (source / "trackchanges.sty").write_text(
        "\\ProvidesPackage{trackchanges}\n\\endinput\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "project-local trackchanges.sty" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["local_package_paths"] == ["trackchanges.sty"]


def test_scanner_rejects_input_path_override_for_changes_package(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\makeatletter
\def\input@path{{vendor/}}
\makeatother
\usepackage{changes}
\begin{document}
\added{content}
\end{document}
""",
    )
    vendor = source / "vendor"
    vendor.mkdir()
    (vendor / "changes.sty").write_text(
        "\\ProvidesPackage{changes}\n\\endinput\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "package search path" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["definition_command"] == "def"
    assert caught.value.violation.details["search_control_sequence"] == r"\input@path"


def test_unrelated_input_path_override_without_revision_calls_is_ignored(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\makeatletter
\def\input@path{{vendor/}}
\makeatother
\begin{document}
plain content
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.total == 0


def test_scanner_rejects_changes_package_hidden_in_unknown_group(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\newcommand{\holder}[1]{}
\holder{\usepackage{changes}}
\begin{document}
\added{content}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "canonical revision command conflicts" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["source_kind"] == "grouped_changes_package"


def test_alias_audit_skips_dynamic_constructs_inside_inert_macro_body(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\newcommand{\factory}{
  \csdef{deleted}{custom}
  \patchcmd{\added}{old}{new}{}{}
  \expandafter\def\csname replaced\endcsname{custom}
}
\begin{document}
\added{new}\deleted{old}\replaced{current}{prior}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.as_dict() == {
        "added": 1,
        "deleted": 1,
        "replaced": 1,
    }


def test_alias_audit_enforces_recursive_nesting_limit_without_recursion_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert revision_macros_module._MAX_ALIAS_AUDIT_NESTING == 32
    monkeypatch.setattr(revision_macros_module, "_MAX_ALIAS_AUDIT_NESTING", 2)
    nested = r"\let\delete\deleted"
    for _ in range(4):
        nested = rf"\ifthenelse{{flag}}{{{nested}}}{{}}"
    source = _project(
        tmp_path,
        f"\\documentclass{{article}}\n{nested}\n"
        "\\begin{document}\nplain text\n\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source audit nesting exceeds" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["max_nesting"] == 2


def test_alias_audit_enforces_total_command_node_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert revision_macros_module._MAX_ALIAS_AUDIT_COMMAND_NODES == 100_000
    monkeypatch.setattr(revision_macros_module, "_MAX_ALIAS_AUDIT_COMMAND_NODES", 2)
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\nplain text\n\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source audit command count exceeds" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["max_command_nodes"] == 2


def test_revision_display_context_is_same_paragraph_normalized_and_bounded(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n"
        "A\\deleted{OLD}B\n\n"
        "prefix \\replaced{NEW}{PRIOR} suffix\n\n"
        + ("X" * 40)
        + "\\deleted{GONE}"
        + ("Y" * 40)
        + "\n\\end{document}\n",
    )

    inventory = _scan(source)

    first, second, third = inventory.display_expectations
    assert (first.left_context, first.right_context) == ("A", "B")
    assert (second.left_context, second.right_context) == ("prefix ", " suffix")
    assert third.left_context == "X" * 32
    assert third.right_context == "Y" * 32


def test_revision_display_context_stops_at_paragraph_and_unsafe_commands(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\begin{document}
A

\deleted{OLD}

B

\textbf{unsafe}\deleted{GONE}\cite{x}
\end{document}
""",
    )

    inventory = _scan(source)

    paragraph_bounded, command_bounded = inventory.display_expectations
    assert (paragraph_bounded.left_context, paragraph_bounded.right_context) == ("", "")
    assert (command_bounded.left_context, command_bounded.right_context) == ("", "")


def test_scanner_rejects_multiple_static_changes_package_declarations(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\RequirePackage{changes}
\begin{document}
\added{content}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "multiple static changes package declarations" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["command"] == r"\added"
    assert caught.value.violation.details["source_count"] == 2


@pytest.mark.parametrize(
    ("alias", "definition"),
    [
        ("add", r"\newcommand{\add}[1]{\textbf{#1}}"),
        ("delete", r"\renewcommand{\delete}[1]{#1}"),
        ("add", r"\providecommand{\add}[1]{#1}"),
        ("delete", r"\DeclareRobustCommand{\delete}[1]{#1}"),
        ("add", r"\NewDocumentCommand{\add}{m}{#1}"),
        ("delete", r"\RenewExpandableDocumentCommand{\delete}{m}{#1}"),
        ("add", r"\def\add#1{#1}"),
        ("delete", r"\edef\delete#1{#1}"),
        ("add", r"\let\add\textbf"),
        ("delete", r"\futurelet\delete\somecommand"),
    ],
)
def test_scanner_rejects_source_defined_alias_with_unproven_semantics(
    tmp_path: Path,
    alias: str,
    definition: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        f"{definition}\n"
        "\\begin{document}\n"
        f"\\{alias}{{content}}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source-defined command" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["alias"] == f"\\{alias}"
    assert caught.value.violation.details["definition_command"]


@pytest.mark.parametrize("alias", ["add", "delete"])
def test_scanner_rejects_bare_alias_without_static_source(
    tmp_path: Path,
    alias: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        f"\\{alias}{{content}}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "no accepted static semantic source" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["alias"] == f"\\{alias}"


def test_scanner_rejects_multiple_add_sources_as_ambiguous(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\usepackage{trackchanges}
\let\add\added
\begin{document}
\add{content}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "multiple semantic sources" in str(caught.value)


def test_scanner_rejects_optional_alias_call_for_one_argument_wrapper(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\newcommand{\delete}[1]{\deleted{#1}}
\begin{document}
\delete[id=reviewer]{content}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "optional argument is not supported" in str(caught.value)


def test_scanner_audits_alias_definitions_in_local_style_and_dynamic_branch(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{local}
\begin{document}
\delete{content}
\end{document}
""",
    )
    (source / "local.sty").write_text(
        r"""\ProvidesPackage{local}
\ifdefined\someflag
  \newcommand{\delete}[1]{\deleted{#1}}
\else
  \newcommand{\delete}[1]{custom #1}
\fi
""",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["path"] == "local.sty"


@pytest.mark.parametrize(
    "conditional_source",
    [
        "\\ifdefined\\flag\n\\let\\delete\\deleted\n\\fi\n",
        "\\ifthenelse{\\boolean{flag}}{\\let\\delete\\deleted}{}\n",
    ],
)
def test_scanner_rejects_alias_source_that_only_exists_in_dynamic_region(
    tmp_path: Path,
    conditional_source: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n"
        f"{conditional_source}"
        "\\begin{document}\n"
        "\\delete{content}\n"
        "\\end{document}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source-defined command" in str(caught.value)
    assert caught.value.violation.details is not None
    assert str(caught.value.violation.details["source_kind"]).startswith("dynamic_")


def test_scanner_allows_plain_unicode_escaped_literals_and_simple_inline_formatting(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\begin{document}
\added{plain text 50\% \& \# \_ \textbf{bold \emph{nested}} \textit{italic} \textsl{slanted}
\textnormal{normal} \textrm{roman} \textsf{sans} \textmd{medium} \textup{upright} \textsubscript{2}}
\deleted{old {grouped} \texttt{code}}
\replaced{new \textsuperscript{2} \underline{under}}{old text \textsc{caps}}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.as_dict() == {
        "added": 1,
        "deleted": 1,
        "replaced": 1,
    }
    assert inventory.total == 3


@pytest.mark.parametrize(
    "body",
    [
        r"\added{math $E=mc^2$}",
        r"\added{math \(x+y\)}",
        r"\added{see \ref{sec:one}}",
        r"\added{cite \cite{source}}",
        r"\added{image \includegraphics{figure.png}}",
        r"\added{text\footnote{note}}",
        r"\added{\begin{quote}structured\end{quote}}",
        "\\added{first paragraph\n\nsecond paragraph}",
        r"\added{table cell A & B}",
        r"\replaced{safe new}{old \label{unsafe}}",
    ],
)
def test_scanner_fails_closed_for_structured_revision_arguments(
    tmp_path: Path,
    body: str,
) -> None:
    source = _project(
        tmp_path,
        f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n",
    )
    if "\\includegraphics" in body:
        (source / "figure.png").write_bytes(b"synthetic test image")

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["path"] == "main.tex"
    assert caught.value.violation.details["command"] in {"added", "replaced"}


def test_scanner_allows_a_single_source_line_break_inside_plain_text(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n\\usepackage{changes}\n\\begin{document}\n"
        "\\added{one physical line\nstill the same paragraph}\n"
        "\\end{document}\n",
    )

    assert _scan(source).canonical_counts.added == 1


@pytest.mark.parametrize(
    "body",
    [
        r"\added",
        r"\added*{new}",
        r"\added{one}{two}",
        r"\added[first][second]{new}",
        r"\replaced{new}",
        r"\replaced{new}{old}{extra}",
        r"\deleted[comment={unbalanced]{old}",
    ],
)
def test_scanner_fails_closed_for_malformed_revision_macro(
    tmp_path: Path,
    body: str,
) -> None:
    source = _project(
        tmp_path,
        f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["path"] == "main.tex"


def test_scanner_fails_closed_for_unbalanced_dynamic_conditional(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\begin{document}
\ifdefined\flag
\added{uncertain}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "conditional" in str(caught.value)


def test_scanner_enforces_occurrence_and_option_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurrence_source = _project(
        tmp_path,
        r"""\documentclass{article}
\begin{document}
\added{one}\deleted{two}
\end{document}
""",
    )
    monkeypatch.setattr(revision_macros_module, "_MAX_REVISION_MACRO_OCCURRENCES", 1)

    with pytest.raises(ContractError) as occurrence_error:
        _scan(occurrence_source)

    assert occurrence_error.value.code is ErrorCode.SCHEMA_INVALID
    assert "occurrence count" in str(occurrence_error.value)

    option_source = tmp_path / "option-source"
    option_source.mkdir()
    (option_source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\added[long]{new}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(revision_macros_module, "_MAX_OPTION_CHARACTERS", 3)

    with pytest.raises(ContractError) as option_error:
        _scan(option_source)

    assert option_error.value.code is ErrorCode.SCHEMA_INVALID
    assert "option exceeds" in str(option_error.value)


def test_alias_audit_command_budget_is_shared_across_project_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(revision_macros_module, "_MAX_ALIAS_AUDIT_COMMAND_NODES", 5)
    source = _project(
        tmp_path,
        "\\documentclass{article}\n\\input{section}\n"
        "\\begin{document}\nplain text\n\\end{document}\n",
        included="\\foo\\bar\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source audit command count exceeds" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["path"] == "section.tex"


def test_inventory_rejects_source_drift_after_discovery(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n\\begin{document}\n\\added{before}\n\\end{document}\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\added{after}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        scan_revision_macros(source, discovery)

    assert caught.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_source_injection_mode_is_an_exact_noop() -> None:
    source = "\\begin{document}\r\n\\added{正文🙂}\r\n\\end{document}"

    result = inject_revision_macros(source, "source")

    assert result is source


def test_clean_injection_expands_to_current_text_for_all_supported_commands() -> None:
    source = (
        r"\begin{document}"
        r"X\added[id=a]{A}Y\deleted{D}Z\replaced{N}{O}Q\add{S}\delete{T}."
        r"\end{document}"
    )

    injected = inject_revision_macros(source, "clean", aliases=("add", "delete"))
    expanded = expand_macros(injected)

    assert "XAYZNQS." in expanded
    assert "\\added" not in expanded
    assert "\\deleted" not in expanded
    assert "\\replaced" not in expanded
    assert "\\add" not in expanded
    assert "\\delete" not in expanded
    assert f"profile={REVISION_MACRO_PROFILE_ID} mode=clean" in injected
    assert "aliases=add,delete" in injected


def test_display_injection_uses_one_blue_and_old_before_new_without_highlight() -> None:
    source = (
        r"\begin{document}"
        r"X\added{A}\deleted{D}\replaced{N}{O}\add{S}\delete{T}Y"
        r"\end{document}"
    )

    injected = inject_revision_macros(source, "display", aliases=("add", "delete"))
    expanded = expand_macros(injected)

    expected = (
        r"X\textcolor{blue}{A}"
        r"\textcolor{blue}{\sout{D}}"
        r"\textcolor{blue}{\sout{O}}\textcolor{blue}{N}"
        r"\textcolor{blue}{S}"
        r"\textcolor{blue}{\sout{T}}Y"
    )
    assert expected in expanded
    assert r"\highlight" not in injected
    assert r"\hl" not in injected
    assert "0000FF" not in injected
    assert f"profile={REVISION_MACRO_PROFILE_ID} mode=display" in injected
    assert "aliases=add,delete" in injected


def test_injection_only_redefines_explicitly_requested_aliases() -> None:
    source = r"\begin{document}\added{A}\add{B}\delete{C}\end{document}"

    canonical_only = inject_revision_macros(source, "clean")
    add_only = inject_revision_macros(source, "clean", aliases=("add",))

    assert r"\renewcommand{\add}" not in canonical_only
    assert r"\renewcommand{\delete}" not in canonical_only
    assert r"\renewcommand{\add}" in add_only
    assert r"\renewcommand{\delete}" not in add_only
    assert "aliases=none" in canonical_only
    assert "aliases=add" in add_only


@pytest.mark.parametrize(
    "aliases",
    [
        ("delete", "add"),
        ("add", "add"),
        ("unknown",),
    ],
)
def test_injection_rejects_noncanonical_alias_sets(aliases: tuple[str, ...]) -> None:
    with pytest.raises(ContractError) as caught:
        inject_revision_macros("source", "display", aliases=cast("Any", aliases))

    assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_source_view_rejects_alias_redefinitions() -> None:
    with pytest.raises(ContractError) as caught:
        inject_revision_macros("source", "source", aliases=("add",))

    assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_injection_is_deterministic_idempotent_and_rejects_marker_collisions() -> None:
    source = "\\begin{document}\\added{new}\\end{document}"

    first = inject_revision_macros(source, "display", aliases=("add",))
    second = inject_revision_macros(source, "display", aliases=("add",))

    assert first == second
    assert inject_revision_macros(first, "display", aliases=("add",)) == first
    assert first.endswith("\n")
    with pytest.raises(ContractError) as collision:
        inject_revision_macros(first, "clean", aliases=("add",))
    assert collision.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(ContractError) as alias_collision:
        inject_revision_macros(first, "display", aliases=("delete",))
    assert alias_collision.value.code is ErrorCode.SCHEMA_INVALID


def test_injection_rejects_unknown_mode() -> None:
    with pytest.raises(ContractError) as caught:
        inject_revision_macros("source", cast("RevisionMacroMode", "unknown"))

    assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_display_models_reject_invalid_metadata_digest_context_and_segments() -> None:
    segment_type = revision_macros_module.RevisionDisplaySegment
    expectation_type = revision_macros_module.RevisionDisplayExpectation
    digest = "sha256:" + ("0" * 64)
    segment = segment_type("visible", False)

    with pytest.raises(ValueError, match="segments must not be empty"):
        segment_type("", False)

    for offset, instances in ((-1, 1), (0, 0)):
        with pytest.raises(ValueError, match="metadata is invalid"):
            expectation_type("main.tex", offset, digest, (segment,), instances)

    for invalid_digest in ("0" * 64, "sha256:" + ("0" * 63), "sha256:" + ("G" * 64)):
        with pytest.raises(ValueError, match="digest is invalid"):
            expectation_type("main.tex", 0, invalid_digest, (segment,), 1)

    for context in ("x" * 33, "left~right", "two  spaces", "tab\tseparated"):
        with pytest.raises(ValueError, match="left_context is not normalized"):
            expectation_type(
                "main.tex",
                0,
                digest,
                (segment,),
                1,
                left_context=context,
            )


def test_projection_builder_collapses_whitespace_without_inventing_strike() -> None:
    builder = revision_macros_module._ProjectionBuilder()

    with pytest.raises(AssertionError, match="single characters"):
        builder.append("two", strike=False)

    builder.append(" ", strike=True)
    builder.append("\t", strike=True)
    builder.append("~", strike=False)

    assert builder.finish() == (revision_macros_module.RevisionDisplaySegment(" ", False),)


def test_crlf_comments_and_package_options_bound_display_context(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    main = (
        "\\documentclass{article}\r\n"
        "\\usepackage[markup=underlined]{changes}\r\n"
        "\\begin{document}\r\n"
        "LEFT\r\n\\deleted{OLD}\r\nRIGHT\r\n\r\n"
        "A% comment hides the left candidate\r\n"
        "\\deleted{GONE}% comment hides the right candidate\r\n"
        "B\r\n"
        "\\added{new% comment inside the argument\r\n text}\r\n"
        "\\end{document}\r\n"
    )
    (source / "main.tex").write_bytes(main.encode("utf-8"))

    inventory = _scan(source)

    first, second, third = inventory.display_expectations
    assert (first.left_context, first.right_context) == ("LEFT ", " RIGHT")
    assert (second.left_context, second.right_context) == ("", "")
    assert third.text == "new text"


def test_comment_rich_static_alias_sources_and_inert_definitions_are_accepted(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage[final]{changes}
\newcommand*{\delete}% target separator
[1]% argument separator
{\deleted{#1}}
\let\add% assignment separator
=% source separator
\added
\def\helper#1% primitive separator
{#1}
\newtheorem{theorem}[shared]{Synthetic title}[section]
\begingroup
\let\unusedalias\added
\endgroup
\begin{document}
\verb*|\deleted{ignored}|
\add{new}\delete{old}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.alias_counts.as_dict() == {"add": 1, "delete": 1}


def test_noncommand_assignment_sources_do_not_fabricate_alias_provenance(
    tmp_path: Path,
) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\let\helper=x
\let\delete = x
\begin{document}
\delete{old}
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source-defined command" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["definition_command"] == "let"


def test_alias_definition_rejects_more_than_two_optional_groups(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\newcommand{\delete}[1][default][extra]{\deleted{#1}}
\begin{document}
plain
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "too many optional groups" in str(caught.value)


@pytest.mark.parametrize(
    "style_body",
    [
        "\\ProvidesPackage{local}\n\\csdef",
        "\\ProvidesPackage{local}\n\\expandafter\\def\\csname deleted",
    ],
)
def test_dynamic_definition_parser_reports_missing_or_unbalanced_targets(
    tmp_path: Path,
    style_body: str,
) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n\\usepackage{local}\n"
        "\\begin{document}\nplain\n\\end{document}\n",
    )
    (source / "local.sty").write_text(style_body, encoding="utf-8", newline="\n")

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["path"] == "local.sty"
    assert any(word in str(caught.value) for word in ("missing", "unbalanced"))


@pytest.mark.parametrize(
    "definition",
    [
        r"\appto X{body}",
        r"\patchcmd{\added{extra}}{old}{new}{}{}",
        r"\csdef{deleted\suffix}{body}",
        r"\expandafter\def\csname deleted\suffix\endcsname{body}",
        "\\expandafter\\def\\csname deleted% comment\r\n\\endcsname{body}",
    ],
)
def test_nonstatic_dynamic_targets_fail_closed_for_canonical_calls(
    tmp_path: Path,
    definition: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    main = (
        "\\documentclass{article}\r\n"
        "\\usepackage{changes}\r\n"
        f"{definition}\r\n"
        "\\begin{document}\r\n"
        "\\added{content}\\deleted{old}\\replaced{new}{prior}\r\n"
        "\\end{document}\r\n"
    )
    (source / "main.tex").write_bytes(main.encode("utf-8"))

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "canonical revision command conflicts" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["source_kind"] in {
        "computed_definition",
        "patch_definition",
    }


def test_unrelated_dynamic_tokens_do_not_create_canonical_conflicts(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\csdef x {body}
\csdef{helper}{body}
\expandafter\helper
\expandafter\def X{body}
\expandafter\def\helper{body}
\expandafter\let\csname helpercopy\endcsname = x
\begin{document}
\added{content}
\end{document}
""",
    )

    assert _scan(source).canonical_counts.added == 1


def test_scanner_skips_unrelated_xparse_latex3_definition(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\usepackage{changes}
\ExplSyntaxOn
\DeclareDocumentCommand \ca_organization { O{,} m } {#2#1}
\DeclareDocumentCommand \lwr:helper_name { m } {#1}
\ExplSyntaxOff
\begin{document}
\added{NEW}
\end{document}
""",
    )

    inventory = _scan(source)

    assert inventory.canonical_counts.added == 1
    assert inventory.total == 1


def test_raw_conditional_nesting_uses_the_same_stable_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(revision_macros_module, "_MAX_ALIAS_AUDIT_NESTING", 1)
    source = _project(
        tmp_path,
        r"""\documentclass{article}
\ifdefined\one
\ifdefined\two
\fi
\fi
\begin{document}
plain
\end{document}
""",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "source audit nesting exceeds" in str(caught.value)
    assert caught.value.violation.details is not None
    assert caught.value.violation.details["max_nesting"] == 1


@pytest.mark.parametrize(
    "body",
    [
        "\\verb\n",
        "\\verb|unterminated\n",
        "\\begin{verbatim}\nunterminated\n",
    ],
)
def test_unbalanced_verbatim_constructs_fail_closed(
    tmp_path: Path,
    body: str,
) -> None:
    source = _project(
        tmp_path,
        f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\\end{{document}}\n",
    )

    with pytest.raises(ContractError) as caught:
        _scan(source)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "unbalanced" in str(caught.value) or "delimiter is missing" in str(caught.value)


def test_scanner_rejects_discovery_with_external_references(tmp_path: Path) -> None:
    source = _project(
        tmp_path,
        "\\documentclass{article}\n\\begin{document}\n\\input{../outside}\n\\end{document}\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    assert discovery.external_references

    with pytest.raises(ContractError) as caught:
        scan_revision_macros(source, discovery)

    assert caught.value.code is ErrorCode.PATH_TRAVERSAL


def test_bound_source_rejects_hash_mismatch_and_invalid_utf8(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "main.tex"
    data = b"\xff"
    path.write_bytes(data)
    source_file_type = SourceFile

    wrong_hash = source_file_type(
        path="main.tex",
        role="tex",
        media_type="text/x-tex",
        size_bytes=1,
        sha256="sha256:" + ("0" * 64),
        encoding=None,
        newline=None,
    )
    with pytest.raises(ContractError) as mismatch:
        revision_macros_module._read_bound_source(source, wrong_hash)
    assert mismatch.value.code is ErrorCode.HASH_SOURCE_MISMATCH

    invalid_utf8 = source_file_type(
        path="main.tex",
        role="tex",
        media_type="text/x-tex",
        size_bytes=1,
        sha256=sha256_bytes(data),
        encoding=None,
        newline=None,
    )
    with pytest.raises(ContractError) as decoding:
        revision_macros_module._read_bound_source(source, invalid_utf8)
    assert decoding.value.code is ErrorCode.SCHEMA_INVALID
    assert "valid UTF-8" in str(decoding.value)
