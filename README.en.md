<p align="center">
  <strong>Language / 语言 / 言語</strong><br>
  <a href="README.md">简体中文</a> |
  <strong>English</strong> |
  <a href="README.ja.md">日本語</a>
</p>

# LaTeX Word Review

**Let reviewers work in Word, then bring only approved and safely located text changes into a new LaTeX copy.**

> [!IMPORTANT]
> **System requirement: 64-bit Windows and 64-bit Windows PowerShell 5.1 only. PowerShell 7 (`pwsh`), 32-bit Windows, and 32-bit Windows PowerShell are not supported.** The double-click launcher selects the required Windows PowerShell automatically.

## The real problem

You write a paper in LaTeX, but your supervisor, coauthor, or editor prefers Microsoft Word. You send a Word copy and receive it back with Track Changes and comments.

Copying every edit by hand is slow and easy to get wrong. Converting the entire edited Word file back to LaTeX can damage equations, citations, labels, commands, and document structure.

LaTeX Word Review provides a controlled bridge. Word is the review interface, while LaTeX remains the master document.

## What it does

- Creates an editable Word review copy from a LaTeX project.
- Shows change markup already present in LaTeX: plain-text `\added`, `\deleted`, and `\replaced` calls receive an exact uniform-blue view; formulas, references, or formatting are reported without preventing the clean review Word from being created.
- Reads tracked changes and comments from the returned Word file.
- Lets you review proposed changes before they reach LaTeX.
- Shows the exact LaTeX diff before writing anything.
- Creates a new revised project copy after a second confirmation.

The original LaTeX project and the returned Word original are not overwritten.

This project does **not** convert the whole modified Word document back into LaTeX.

## What's new

- Change markup already present in LaTeX can now be saved as a separate uniform-blue reference Word file, with deleted text shown in blue strikethrough.
- A change containing a formula, reference, or formatting no longer blocks the whole clean review Word merely because it cannot be displayed perfectly. The app explains the affected item and shows what it safely can.
- Everyday use is simpler: there is one root double-click launcher, you can choose where to save the review Word file, and you can remove recent tasks you no longer need.

## Who it is for

This project may suit you if:

- you write papers, theses, reports, or manuscripts in LaTeX;
- a supervisor or collaborator prefers Word Track Changes;
- you use Windows;
- you want to approve changes yourself;
- you want the review workflow to stay on your own computer.

It is not a general Word-to-LaTeX converter and does not reproduce a journal PDF layout inside Word.

## Core benefits

- **The original stays safe.** Results are written only to a new project copy.
- **You stay in control.** Approval and writing are two separate confirmation steps.
- **Uncertain edits are not guessed.** Risky or ambiguous changes stay manual.
- **The workflow is local.** The core application does not actively upload the paper.
- **The result is traceable.** Decisions and outcomes are recorded in a ledger.

## Four steps

### 1. Select the paper

Choose the main `.tex` file. The application creates a read-only task snapshot and checks the project.

### 2. Generate and receive Word

The app creates a clean review Word file. Save its exact editable copy, send that copy to the reviewer, and import the returned copy into the same task. The optional change-display Word file is for reference only and must never be imported.

Ask the reviewer to:

- keep **Track Changes** enabled;
- use tracked edits for text changes;
- use comments for discussion;
- return the same `.docx` review round;
- avoid **Accept All Changes** or deleting review bookmarks.

When the file comes back, select it in the application. The returned original is archived read-only before analysis.

### 3. Review the changes

Inspect the detected edits and decide which to accept, revise, reject, or leave for manual work. Completing this step records your decisions but does not change LaTeX.

### 4. Check the diff and create the result

Review the proposed file changes. Only after a second confirmation will the application create a new LaTeX project copy. A change that cannot be located safely will not be applied automatically.

## If the LaTeX source already contains change markup

The app can recognize `\added{new text}`, `\deleted{old text}`, and
`\replaced{new text}{old text}`, with conservative support for `\add` and `\delete` when their source
can be proven statically.

- **Plain-text changes are exact:** additions are uniform blue, deletions are blue with a
  single strikethrough, and replacements show the old blue struck text followed by the new blue text.
- **Changes containing formulas or references are handled item by item:** they no longer block the
  whole paper merely because they cannot be colored exactly. `review.docx` remains the clean Word
  file to send. The app explains each affected item, and the reference view is shown where possible.

| File | Purpose |
|---|---|
| `review.docx` and its exact editable saved copy | The clean review document. Only a saved copy in this review role may be sent out and imported as the returned Word file |
| `existing-changes-display.docx` | An optional reference-only view. Plain-text changes are displayed exactly in blue/blue strikethrough; changes containing formulas or references are explained item by item and shown where possible. This feature adds no highlighting |

The display file does not use native Word Track Changes, must never be imported as a returned review,
and does not participate in automatic writeback. Writeback remains limited to exact plain-body edits
from the returned Word file, approved one by one and written only into a new LaTeX copy. The application
never replaces LaTeX with a whole Word-to-LaTeX conversion.

Dangerous or escaping paths, unproven dynamic macros, and proven silent loss of real images,
equations, tables, or references in the clean review still stop the export. Those are security or
content-integrity failures, not ordinary display limitations.

<details>
<summary>Static recognition and display-validation details</summary>

Canonical calls require a supported static `changes` package declaration. A same-named
`changes.sty` in the bound project tree, a detected `\input@path` override, or a direct/dynamic
definition or redefinition stops conversion. `\add` and `\delete` use stricter static declaration or
direct-wrapper rules; a `trackchanges.sty` shadow, conflicting source, or unproven source is not
guessed. This bounded syntactic check does not run TeX or prove which file, version, or hash a system
or user TEXMF tree or `TEXINPUTS` would ultimately load.

For plain text, source-derived text, blue/strike effects, and unique same-paragraph deletion context
must match exactly. For structured changes, the exact plain-text subset is still checked, while every
unproven item is source-bound and reported. If the best-effort display as a whole cannot pass bounded
validation, the application omits it and retains the integrity-checked clean review. Empty payloads
such as `\added{}`, `\deleted{}`, or `\replaced{}{}` cannot establish display evidence and stop with
`E_SCHEMA_INVALID`.

This feature does not create a new SourceMap from each LaTeX call to a Word coordinate. See
[Export backends and DOCX inspection](docs/reference/export-and-inspection.md) for the complete
alias-provenance, scan-boundary, structured-content, artifact-role, and validation contracts.

</details>

## Windows quick start

You do not need to preinstall Python, `uv`, Git, or use administrator permission.

1. Download the [current GitHub source ZIP](https://github.com/JIE-jiee/latex-word-review/archive/refs/heads/main.zip).
2. In File Explorer, choose **Extract All**. Do not run it inside the ZIP preview.
3. Open the extracted folder.
4. Double-click **`Start-Latex-Word-Review.cmd`**.
5. Stay online during the first setup and wait for the local page to open.
6. Later, double-click the same file to start it again.

The current application interface is **Simplified Chinese**. The language links at the top switch the GitHub documentation only. English and Japanese application interfaces have not shipped.

The current download is a source ZIP with a double-click bootstrap. It is not a signed Windows installer.

## Important limits

- Windows is the only supported platform.
- The current version is beta software and may still contain conversion, layout, performance, packaging, or edge-case defects.
- The generated Word file is for readable review, not a pixel-perfect copy of the LaTeX PDF.
- The change-display Word file is static reference-only formatting, not native Word Track Changes, and cannot be imported. Plain-text changes are exact; structured formulas, references, or formatting are best-effort with item-level warnings, and the display file may be omitted while the clean review remains available.
- Structured display difficulty alone does not block the paper. Dangerous/escaping paths, unproven dynamic macros, or proven silent loss of images, equations, tables, or references still block export.
- Formulas, citations, labels, references, environments, figures, moves, formatting-only edits, comments, and uncertain matches are not automatically rewritten. Automatic writeback remains limited to individually approved, exactly located plain-body text.
- A reviewer who accepts all revisions, turns off Track Changes, removes mapping bookmarks, or returns the wrong review round may cause the workflow to stop rather than guess.
- The reviewer needs Microsoft Word for the normal review workflow. Some papers with live Word fields also require Word on the application computer during export.
- Final PDF creation requires a suitable local TeX setup. A marked PDF also requires `latexdiff`.
- The first setup does not install Word, MiKTeX, TeX Live, Pandoc, Perl, fonts, or missing LaTeX packages.

### A detected change may still require manual work

A complex returned Word file can be read successfully and its changes can appear in the review list, while the original LaTeX mapping does not cover enough of the edited text.

In that situation, the number of changes eligible for safe automatic backfill may be **zero**.

- Detected changes are still recorded.
- Unmapped changes remain manual or ledger-only.
- The application does not use whole-document fuzzy matching or the nearest bookmark to guess a source location.

"0 safe backfills" means no detected change had a sufficiently proven LaTeX location. It does not mean the Word file had no changes.

## What you receive

Depending on the available Word and TeX tools, a task can produce:

| Result | Purpose |
| --- | --- |
| Review Word document | The editable file sent to the reviewer |
| `existing-changes-display.docx` | A reference-only view generated only when existing LaTeX change macros are detected |
| Returned Word archive | A read-only copy of what was received |
| New revised LaTeX copy | Contains only approved changes that passed safety checks |
| Clean PDF | The revised paper when compilation succeeds |
| Marked PDF | Actual LaTeX source differences when `latexdiff` succeeds |
| Change ledger | Records decisions, outcomes, and verification results |
| Audit bundle | Keeps selected evidence and hashes for later checking |

LaTeX does not have Word-style review balloons. Reviewer names, times, comments, and decisions stay in the ledger. A task with no applied source change does not receive artificial revision marks.

## Privacy and recovery

- The application runs locally on your Windows computer.
- The core workflow does not actively upload the paper.
- Recent tasks can be reopened and checked before continuing.
- Application-owned task data can be deleted after explicit confirmation.
- Deleting a task does not delete the original paper or Word copies saved elsewhere.

For sensitive work, keep the task local and do not share private paper files, returned reviewer files, or task folders with an AI agent or public issue.

## Vibe Coding and contributions

This project was created through **Vibe Coding with OpenAI Codex**.

The maintainer defined the workflow, corrected direction, set safety boundaries, and made release decisions. AI coding agents assisted with research, design, implementation, testing, diagnostics, and documentation.

This describes the development process. It is not a guarantee that the software is correct.

Careful testing, documentation improvements, compatibility reports, Issues, and focused Pull Requests are welcome. Please use a small sanitized example and never upload a private paper or reviewer identity.

<details>
<summary><strong>Developer, release, and audit information</strong></summary>

[![CI](https://github.com/JIE-jiee/latex-word-review/actions/workflows/ci.yml/badge.svg)](https://github.com/JIE-jiee/latex-word-review/actions/workflows/ci.yml)
[![Windows source bootstrap](https://github.com/JIE-jiee/latex-word-review/actions/workflows/windows-source-bootstrap.yml/badge.svg)](https://github.com/JIE-jiee/latex-word-review/actions/workflows/windows-source-bootstrap.yml)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4)](docs/compat/platform-support.md)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

The source version is `0.2.0b1` and sealed objects use schema `v1alpha`. There is no GitHub Release, PyPI release, or public signed installer. Public frozen binaries remain blocked until the native-library licensing and relinking evidence is complete.

For a reproducible source review, use a full inspected commit SHA, for example `git checkout --detach <reviewed-40-character-commit-sha>`, then follow the [English Windows Quick Start](docs/quick-start-windows.md).

Technical references:

- [Documentation index](docs/README.md)
- [Upstream strategy](docs/adr/0001-upstream-strategy.md)
- [Windows product design](docs/adr/0003-windows-product-experience.md)
- [Security threat model](docs/security/threat-model.md)
- [Windows binary license audit](docs/reviews/windows-binary-license-audit-2026-07.md)
- [Contributing](CONTRIBUTING.md) and [Security policy](SECURITY.md)
- [Apache License 2.0](LICENSE) and [Third-Party Notices](THIRD_PARTY_NOTICES.md)

The recorded 2026-07-21 source-candidate snapshot reported `1209 passed, 9 skipped, 0 failed`, branch coverage of `90.47%`, and passing Ruff, format, strict mypy, and lock checks. This does not claim compatibility with every LaTeX package, Word editing pattern, or Windows configuration.

</details>
