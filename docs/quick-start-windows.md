# Windows Quick Start

This guide is the shortest supported path from a LaTeX project to a Word review and back to a new
LaTeX copy. The original LaTeX tree and the returned Word file remain immutable. Run every command
in PowerShell on Windows with Python 3.12 or 3.13.

## 1. Install the core CLI

Clone a reviewed commit of the public repository and install the locked development environment:

```powershell
git clone https://github.com/JIE-jiee/latex-word-review.git
Set-Location latex-word-review
uv sync --frozen --group fixture --extra pdf-figures --python 3.12
$Lwr = (Resolve-Path .\.venv\Scripts\latex-word-review.exe).Path
& $Lwr --version
& $Lwr doctor
```

The `pdf-figures` extra enables safe PDF-page previews for Word. It does not install Microsoft Word,
MiKTeX, Pandoc, or other external programs implicitly.

`$Lwr` is an absolute path, so it remains valid after changing to a run directory. With an installed
wheel, set `$Lwr = "latex-word-review"` instead.

## 2. Create an immutable run and export Word

Choose a new, absent run directory outside the source project:

```powershell
& $Lwr workflow init C:\research\paper C:\review-runs\paper-r1 --main main.tex
Set-Location C:\review-runs\paper-r1
& $Lwr workflow export .
& $Lwr workflow status .
```

Send `export\review.docx` to the reviewer. Ask them to edit a copy in Microsoft Word with Track
Changes enabled and to use comments for discussion. Keep `export\review.docx` unchanged: it is the
semantic baseline used to detect untracked edits, Accept All, or damaged anchors.

## 3. Receive and inspect the returned Word file

Keep the file received from the reviewer outside the run directory and ingest it once:

```powershell
& $Lwr workflow receive . C:\received\reviewed.docx
& $Lwr workflow status .
```

The command archives the returned original read-only, compares its reject-changes view with the
immutable export baseline, and writes `receive\changeset.json`. A mismatch fails closed; do not work
around it by editing a sealed JSON file. At this point `workflow status` reports `ingested` and the
first approval command. It does not later discover ApprovalSet, PatchPlan, or revised-tree progress.

## 4. Decide each change locally

Create the first approval revision and open the loopback-only review page:

```powershell
& $Lwr approve init receive\changeset.json approvals\approval-r1.json `
  --actor-id maintainer --actor-name "Maintainer"
& $Lwr approve serve receive\changeset.json `
  approvals\approval-r1.json approvals --open-browser
```

For each item, inspect before/after text, author, time, source range, confidence, risk, raw Word
evidence, and diagnostics. Record `accepted`, `accepted_with_edit`, `rejected`, `manual`, or
`conflict`, then finalize only after every item has a decision. Approval writes a new immutable JSON
revision; it never changes LaTeX.

## 5. Preview the exact patch, then cross the second gate

Create a dry-run plan from the final approval revision:

```powershell
& $Lwr plan snapshot objects\source-manifest.json receive\changeset.json `
  approvals\approval-rN.json plans\plan-r1
```

Review `plans\plan-r1\changes.patch`, the planned operations, and every
`accepted_but_blocked` item. Only after a separate decision to apply that exact plan, write a new
LaTeX tree:

```powershell
& $Lwr apply snapshot plans\plan-r1 `
  receive\changeset.json `
  approvals\approval-rN.json revised-clean
```

The destination must not already exist. The command rechecks hashes and source byte ranges before
publishing it. It never overwrites `snapshot` or the original project.

## Pause, resume, and clean safely

Before granular approval begins, run this to resume the high-level lifecycle:

```powershell
& $Lwr workflow status .
```

It verifies the snapshot/export/receive objects and hashes without changing state. Its phase is only
`snapshotted`, `exported`, or `ingested`; after `receive`, it keeps printing the generic first
approval command even when later approval or plan files exist.

Once granular work begins, record the new output path from every `approve`, `plan`, `apply`,
`verify`, `ledger`, and `bundle` command. Resume from the latest sealed ApprovalSet/PatchPlan and the
relevant command receipt, using `validate` and `<command> --help` when needed. Do not use
`workflow status` to infer granular progress.

To inspect abandoned tool-owned staging directories at any phase, run:

```powershell
& $Lwr workflow clean .
```

This is a dry run. Remove only the listed staging directories by repeating it with `--execute`.
`workflow clean` cannot remove the snapshot, export, received original, ChangeSet, approvals, plans,
or revised tree.

For verification PDFs, `latexdiff`, ledgers, and audit bundles, continue with the commands in
[CLI and run-directory contract](reference/cli.md). Missing external tools are reported as blocked,
not silently skipped.

## Optional Codex Plugin

The Plugin supplies the `$latex-word-review` orchestration Skill; the Python CLI above remains the
workflow authority. From a published repository revision:

```powershell
codex plugin marketplace add JIE-jiee/latex-word-review --ref main
codex plugin add latex-word-review@personal
```

Pin the marketplace to a reviewed tag or commit for repeatable use. The Skill may run and explain
the same commands, but it must stop for per-change decisions and again before `apply`. Its use of
`workflow status` has the same receive-stage boundary described above.
