from __future__ import annotations

import json
from pathlib import Path

from latex_word_review.discovery import discover_project
from latex_word_review.image_overlay import (
    IMAGE_OVERLAY_MANIFEST,
    TEX2WORD_COMPATIBILITY_PROFILE,
    build_image_overlay,
)
from latex_word_review.tex2word_compat import (
    normalize_tex2word_manual_figure_minipage_body,
    rewrite_tex2word_front_matter,
    rewrite_tex2word_layout_controls,
    rewrite_tex2word_subcaptionboxes,
)

PROFILE = "tex2word-1.0.5-review-compat-v3"


def test_front_matter_is_rewritten_for_readable_review_output() -> None:
    source = r"""\documentclass{publisher}
\begin{document}
\title{Synthetic review paper}
\author[1,2]{Ada Example}
\address[1]{\orgdiv{Department of Tests, }\orgname{Example University, }%
\orgaddress{\state{Example City, }\country{Exampleland}}}
\authormark{EXAMPLE ET AL.}
\titlemark{Publisher running title}
\corres{Ada Example (\email{ada@example.invalid})}
\editor{}
\presentaddress{New Laboratory}
\fundingInfo{}
\keywords{review | conversion}
\abstract[ABSTRACT]{A public synthetic abstract.}
\maketitle
\section{Introduction}
Body text with \address{an ordinary body command that must remain}.
\end{document}
"""

    rewritten, evidence = rewrite_tex2word_front_matter(
        source,
        source_path="main.tex",
        profile=PROFILE,
    )

    assert source.startswith("\\documentclass")
    assert "\\author{Ada Example}" in rewritten
    assert "\\author[1,2]" not in rewritten
    assert "Affiliation 1:" in rewritten
    assert "Department of Tests" in rewritten
    assert "Example University" in rewritten
    assert "Exampleland" in rewritten
    assert "\\orgdiv" not in rewritten
    assert "Correspondence: Ada Example (ada@example.invalid)" in rewritten
    assert "\\authormark" not in rewritten
    assert "\\titlemark" not in rewritten
    assert "Editor:" not in rewritten
    assert "Present address: New Laboratory" in rewritten
    assert "Funding:" not in rewritten
    assert "\\textbf{Keywords:} review | conversion" in rewritten
    assert "\\begin{abstract}\nA public synthetic abstract.\n\\end{abstract}" in rewritten
    assert "Body text with \\address{an ordinary body command that must remain}." in rewritten
    assert {item["command"] for item in evidence} == {
        "author",
        "address",
        "authormark",
        "titlemark",
        "corres",
        "editor",
        "presentaddress",
        "fundingInfo",
        "keywords",
        "abstract",
    }
    assert all(item["source_path"] == "main.tex" for item in evidence)
    assert all("original_text" not in item for item in evidence)


def test_front_matter_rewrite_is_noop_without_complete_document_boundary() -> None:
    incomplete = r"\author[1]{Synthetic Author}\address[1]{Somewhere}"

    rewritten, evidence = rewrite_tex2word_front_matter(
        incomplete,
        source_path="fragment.tex",
        profile=PROFILE,
    )

    assert rewritten == incomplete
    assert evidence == []


def test_unbalanced_front_matter_command_is_left_unchanged() -> None:
    source = "\\begin{document}\n\\author[1]{Unclosed\n\\section{Body}\n"

    rewritten, evidence = rewrite_tex2word_front_matter(
        source,
        source_path="main.tex",
        profile=PROFILE,
    )

    assert rewritten == source
    assert evidence == []


def test_front_matter_rewrite_is_applied_only_to_the_derived_overlay(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = (
        "\\documentclass{article}\n\\captionsetup{font=small}\n\\begin{document}\n"
        "\\author[1]{Synthetic Author}\n"
        "\\address[1]{\\orgname{Example University}}\n"
        "\\linenumbers\n\\section{Body}\nPublic body.\n\\end{document}\n"
    )
    main = source / "main.tex"
    main.write_text(original, encoding="utf-8", newline="\n")
    discovery = discover_project(source, main_document="main.tex")

    result = build_image_overlay(
        source,
        tmp_path / "derived",
        discovery,
        compatibility_profile=TEX2WORD_COMPATIBILITY_PROFILE,
    )

    assert main.read_text(encoding="utf-8") == original
    derived = (result.derived_root / "main.tex").read_text(encoding="utf-8")
    assert "\\author{Synthetic Author}" in derived
    assert "Affiliation 1: Example University" in derived
    assert "\\captionsetup{font=small}" in derived
    assert "\\linenumbers" not in derived
    manifest = json.loads((result.derived_root / IMAGE_OVERLAY_MANIFEST).read_bytes())
    assert manifest["compatibility"]["profile"] == TEX2WORD_COMPATIBILITY_PROFILE
    assert manifest["compatibility"]["transformation_count"] == 3


def test_cas_front_matter_is_rewritten_without_touching_body_commands() -> None:
    source = r"""\documentclass{cas-sc}
\begin{document}
\shorttitle{Publisher running title}
\shortauthors{Example et al.}
\title[mode=title]{Synthetic CAS paper}
\author[1]{Ada Example}
\cormark[1]
\ead{ada@example.invalid}
\affiliation[1]{organization={Example Institute},
addressline={1 Test Road},city={Example City},postcode={000000},country={Exampleland}}
\cortext[1]{Corresponding author}
\begin{abstract}
A public synthetic abstract.
\end{abstract}
\begin{keywords}
structural engineering \sep review workflow
\end{keywords}
\maketitle
\section{Introduction}
Body text with \affiliation{an ordinary body command that must remain}.
\end{document}
"""

    rewritten, evidence = rewrite_tex2word_front_matter(
        source,
        source_path="main.tex",
        profile=PROFILE,
    )

    assert "\\title{Synthetic CAS paper}" in rewritten
    assert "\\title[mode=title]" not in rewritten
    assert "\\author{Ada Example}" in rewritten
    assert "\\shorttitle" not in rewritten
    assert "\\shortauthors" not in rewritten
    assert "\\cormark" not in rewritten
    assert "Email: ada@example.invalid" in rewritten
    assert (
        "Affiliation 1: Example Institute, 1 Test Road, Example City, 000000, Exampleland"
        in rewritten
    )
    assert "Correspondence: Corresponding author" in rewritten
    assert "\\textbf{Keywords:} structural engineering ; review workflow" in rewritten
    assert "Body text with \\affiliation{an ordinary body command that must remain}." in rewritten
    assert {
        "title",
        "author",
        "cormark",
        "ead",
        "affiliation",
        "cortext",
        "keywords_environment",
    } <= {item["command"] for item in evidence}


def test_layout_controls_and_center_captionof_are_narrowly_normalized() -> None:
    source = r"""\documentclass{article}
\captionsetup{labelsep=period}
% \linenumbers and \captionsetup{commented=true} must stay comments
\begin{document}
\linenumbers
\section{Body}
\begin{table}
\captionsetup[table]{font=small}
\caption{A synthetic table}
\end{table}
\begin{center}
  \includegraphics{plot.png}
  \captionof{figure}{Synthetic caption~\protect\cite{public_ref}}
  \label{fig:synthetic}
\end{center}
\nolinenumbers
\begin{center}
  \captionof{table}{Unsupported shapes remain explicit}
\end{center}
\end{document}
Inline \\linenumbers stays explicit.
\captionsetup{type=figure}
\begin{center}
  \includegraphics{first.png}
  \includegraphics{second.png}
  \captionof{figure}{Two-image centers remain explicit}
\end{center}
"""

    rewritten, evidence = rewrite_tex2word_layout_controls(
        source,
        source_path="main.tex",
        profile=PROFILE,
    )

    assert "% \\linenumbers and \\captionsetup{commented=true}" in rewritten
    assert "\\captionsetup{labelsep=period}" in rewritten
    assert "\\captionsetup{type=figure}" in rewritten
    assert "\\captionsetup[table]{font=small}" not in rewritten
    assert rewritten.count("\\linenumbers") == 2
    assert "\\nolinenumbers" not in rewritten
    assert "\\begin{figure}\n\\centering" in rewritten
    assert "\\caption{Synthetic caption~\\protect\\cite{public_ref}}" in rewritten
    assert "\\label{fig:synthetic}" in rewritten
    assert "\\end{figure}" in rewritten
    assert "\\captionof{figure}{Synthetic caption" not in rewritten
    assert "\\captionof{figure}{Two-image centers remain explicit}" in rewritten
    assert "\\captionof{table}{Unsupported shapes remain explicit}" in rewritten
    commands = [item["command"] for item in evidence]
    assert commands.count("captionsetup") == 1
    assert commands.count("linenumbers") == 1
    assert commands.count("nolinenumbers") == 1
    assert commands.count("captionof_figure_center") == 1
    assert all(item["span_basis"] == "derived_overlay_after_image_rewrite" for item in evidence)
    assert all("original_text" not in item for item in evidence)


def test_unbalanced_layout_control_is_left_unchanged() -> None:
    source = "\\captionsetup{unclosed\n\\linenumbersXYZ\n"

    rewritten, evidence = rewrite_tex2word_layout_controls(
        source,
        source_path="main.tex",
        profile=PROFILE,
    )

    assert rewritten == source
    assert evidence == []


def test_direct_static_subcaptionbox_is_normalized_without_nesting() -> None:
    source = r"""\documentclass{article}
\begin{document}
\begin{figure}
\subcaptionbox{Eligible panel\label{fig:eligible}}
  {\includegraphics[width=.4\linewidth]{eligible.png}}
\subcaptionbox[short]{Optional form\label{fig:optional}}{\includegraphics{optional.png}}
\subcaptionbox{Extra content\label{fig:extra}}{text \includegraphics{extra.png}}
\begin{subfigure}{.4\linewidth}
\subcaptionbox{Nested panel\label{fig:nested}}{\includegraphics{nested.png}}
\end{subfigure}
\end{figure}
\subcaptionbox{Outside figure\label{fig:outside}}{\includegraphics{outside.png}}
\end{document}
"""

    rewritten, evidence = rewrite_tex2word_subcaptionboxes(
        source,
        source_path="main.tex",
        profile=PROFILE,
    )

    assert rewritten.count("\\begin{subfigure}") == 2
    assert "\\caption{Eligible panel}" in rewritten
    assert "\\label{fig:eligible}" in rewritten
    assert "\\includegraphics[width=.4\\linewidth]{eligible.png}" in rewritten
    assert "\\subcaptionbox[short]{Optional form" in rewritten
    assert "\\subcaptionbox{Extra content" in rewritten
    assert "\\subcaptionbox{Nested panel" in rewritten
    assert "\\subcaptionbox{Outside figure" in rewritten
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "figure_subcaptionbox_normalization"
    assert evidence[0]["command"] == "subcaptionbox"
    assert evidence[0]["source_path"] == "main.tex"
    original_sha256 = evidence[0]["original_block_sha256"]
    derived_sha256 = evidence[0]["derived_block_sha256"]
    assert isinstance(original_sha256, str) and original_sha256.startswith("sha256:")
    assert isinstance(derived_sha256, str) and derived_sha256.startswith("sha256:")


def test_manual_figure_counter_minipage_body_is_narrowly_normalized() -> None:
    body = r"""[t]{0.48\textwidth}
\centering
\includegraphics[height=5.5cm]{panel.png}
\par\vspace{4pt}
\refstepcounter{figure}\label{fig:panel}
{\small\bfseries Fig.~\thefigure:} {\small Public panel~\protect\cite{public}}
\par
\addcontentsline{lof}{figure}{\protect\numberline{\thefigure}Public panel}
"""

    normalized = normalize_tex2word_manual_figure_minipage_body(body)

    assert normalized is not None
    derived, label, caption = normalized
    assert label == "fig:panel"
    assert caption == r"Public panel~\protect\cite{public}"
    assert "\\includegraphics[height=5.5cm]{panel.png}" in derived
    assert "\\caption{Public panel~\\protect\\cite{public}}" in derived
    assert "\\label{fig:panel}" in derived
    assert "\\refstepcounter" not in derived
    assert "\\addcontentsline" not in derived
    assert normalize_tex2word_manual_figure_minipage_body(body + "extra") is None
    assert (
        normalize_tex2word_manual_figure_minipage_body(body.replace("figure}", "table}", 1)) is None
    )
    assert normalize_tex2word_manual_figure_minipage_body("% comment\n" + body) is None
