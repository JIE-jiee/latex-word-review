---
name: latex-word-review
description: Orchestrate the latex-word-review CLI for an immutable, auditable LaTeX-to-Word review cycle with tracked-change ingestion, per-change human approval, dry-run patch planning, clean-copy application, LaTeX/latexdiff verification, and audit bundles. Use when Codex must prepare a LaTeX project for Word review, process a returned tracked-changes DOCX, help a user accept or reject changes individually, generate revised LaTeX without overwriting the original, or verify and package the review evidence.
---

# LaTeX Word Review

Use the installed `latex-word-review` CLI as the only workflow authority. Keep this Skill as an
orchestrator: do not reimplement DOCX parsing, hashes, approval state, patching, or verification.

## Establish the boundary

1. Treat the original LaTeX tree and the received Word file as immutable.
2. Work only in a dedicated run directory approved by the user. Never write generated files into the
   source tree and never operate on a private document merely because it is discoverable.
3. Never regenerate an entire LaTeX project from Word or replace the authoritative source with a
   Word-derived document.
4. Keep document contents, reviewer identities, ledgers, and audit bundles local unless the user
   explicitly authorizes sharing a specific artifact.
5. Stop on a hash, Schema, path, capability, or safety error. Report the stable error code and the
   affected stage; do not bypass a gate or edit a sealed JSON object by hand.

## Select the command prefix

- In a source checkout containing `pyproject.toml` and `uv.lock`, use
  `uv run --frozen latex-word-review`.
- With an installed wheel, use `latex-word-review`.
- Treat this Skill revision as compatible with the `0.1.x` core CLI and `v1alpha` sealed objects.
  If the installed CLI has another major/minor line or lacks a named command, stop and obtain the
  matching Skill instead of adapting a safety-critical sequence by guesswork.
- Run `--version`, top-level `--help`, and `doctor` before the first workflow action. Do not install
  external tools implicitly. If a command signature is uncertain, read `<command> --help` instead
  of guessing.

## Prefer the high-level workflow

Use the `workflow` commands for the normal resumable lifecycle through returned-Word ingest. Append
these exact arguments to the selected command prefix:

1. Initialize a new, absent run root and immutable snapshot with
   `workflow init <source> <run-root> --main main.tex`. If the main file is not `main.tex`, determine
   it from the project or run `workflow init --help`; do not guess.
2. Create the sealed Word review baseline with `workflow export <run-root>`. Hand the resulting DOCX
   to the reviewer only after checking the reported inspection and degradation status.
3. On return, run `workflow receive <run-root> <returned.docx>`. This must archive the received
   original and verify it against the sealed export baseline before ingest. Stop on baseline drift,
   accepted-all revisions, untracked edits, damaged anchors, or any other verification failure.
4. Run `workflow status <run-root>` after `init`, `export`, and `receive`, and when resuming before
   granular approval begins. It recognizes only `snapshotted`, `exported`, and `ingested`. After
   `receive`, it does not discover ApprovalSet, PatchPlan, revised-tree, verification, ledger, or
   bundle progress and will keep returning the generic first approval command.
5. Run `workflow clean <run-root>` only to preview the tool-owned stages eligible for removal. It is
   a dry-run and must not remove anything without `--execute`. Use
   `workflow clean <run-root> --execute` only after the user explicitly authorizes that cleanup.

The high-level workflow does not approve changes or apply them to LaTeX. After `receive`, preserve
the two independent gates below: record per-change decisions, then obtain a separate instruction
before `apply`. Never infer or automatically cross either gate. Once granular work starts, use its
explicit output paths and latest sealed objects as the resume state; do not treat `workflow status`
as a granular task tracker.

## Use granular commands for gates and recovery

Use the commands below for approval and application, detailed inspection, or recovery from a failed
stage. Before approval starts, use `workflow status <run-root>`; afterward, use the relevant
`<command> --help`, explicit command receipts/paths, and the latest sealed ApprovalSet or PatchPlan.
Reuse fixed run paths rather than bypassing the workflow or guessing arguments.

### 1. Freeze and export (granular recovery)

1. Generate a run ID with `new-run`.
2. Run `snapshot` from the original source into a new run directory. Supply the main `.tex` file
   explicitly when discovery is ambiguous.
3. Run `export` against the immutable snapshot and its sealed `SourceManifest`. Use the pinned
   `tex2word` backend by default; use Pandoc only when the user explicitly requests the documented
   baseline/degraded path.
4. Run `inspect` on the produced DOCX. Check the export report for formulas, figures, tables,
   references, bookmarks, conflicts, and declared degradation before handing it to a reviewer.
5. Preserve the exported DOCX and all sealed objects. Ask the reviewer to edit a copy using Word
   tracked changes and comments.

When no returned Word file exists yet, stop after a concise handoff that identifies the review DOCX,
its hash, the immutable run directory, and the exact command needed to resume.

### 2. Archive and ingest the return (granular recovery)

1. Verify the returned file is `.docx`, not `.docm` or another executable container.
2. Run `archive` before parsing. Continue only from the immutable archived copy and its manifest.
3. Create sealed reader capabilities with `reader-capabilities`.
4. Run `ingest` using the archived copy, original `SourceManifest`, `SourceMap`, and reader
   capabilities. Preserve the complete `ChangeSet`, including moves, formatting revisions, comments,
   unknown evidence, authors, and timestamps even when a change cannot be auto-applied.
5. Summarize counts by change kind and resolution without treating ledger-only evidence as a patch.

### 3. Record decisions one change at a time

1. Run `approve init` with a named actor and retain the first immutable `ApprovalSet` revision.
2. Prefer `approve serve` for an interactive local review. Keep its host restricted to the CLI's
   fixed `127.0.0.1` binding and close the service when finished. Use `approve set` for an equivalent
   CLI-driven review.
3. Present each change's before/after text, author, timestamp, type, source location, confidence,
   resolution, and risk before recording one of `accepted`, `accepted_with_edit`, `rejected`,
   `manual`, or `conflict`.
4. Do not infer blanket acceptance from a request to “process” or “finish” a review. Record a decision
   on the user's behalf only when the user explicitly supplies that decision or delegates a precise
   policy. Never bulk-accept structural, ambiguous, low-confidence, move, format, or comment items.
5. Create a new output filename for every approval revision. Never overwrite an older revision.
6. Run `approve finalize` only after every change has a decision. Explain that final approval records
   intent but does not modify LaTeX and does not guarantee automatic applicability.

### 4. Plan, preview, and apply through the second gate

1. Run `plan` with the immutable snapshot, sealed `SourceManifest`, `ChangeSet`, and final
   `ApprovalSet`. Treat it as dry-run.
2. Show the exact unified diff, planned operations, exclusions, and `accepted_but_blocked` items.
   Require blocked accepted items to be reclassified as `manual` or otherwise resolved; do not hide
   or silently drop them.
3. Obtain a distinct user instruction to apply the reviewed plan. Approval of individual Word
   changes is not that instruction.
4. Run `apply` only into a new, absent destination directory. Recheck that the original source tree,
   returned Word original, and sealed objects retain their hashes.

### 5. Verify and package evidence

1. Run `verify` against original and revised trees plus all bound workflow objects. Require real
   `latexmk` and `latexdiff` for release-quality PDF evidence; report `blocked` when required tools
   are unavailable. Never enable shell escape.
2. Confirm the clean revised tree/PDF and `latexdiff.tex`/PDF are both present. Treat the marked
   version as a derived visual review artifact, not the authoritative source.
3. Run `ledger` to produce the bound JSON and self-contained HTML decision record.
4. Run `run-manifest`, then create an allowlisted audit archive with `bundle`. Run `verify-bundle`
   before delivery.
5. Report hashes and locations for the revised clean copy, marked diff, ledger, verification report,
   and audit bundle. State every skipped, degraded, manual, or blocked item explicitly.

## Preserve resumability

At every pause, identify the completed stage, immutable input hashes, latest sealed object, next
command, and any blocker. Resume from those artifacts rather than repeating conversion or mutating an
earlier object. Remember that `workflow status` validates only the high-level snapshot/export/receive
chain; maintain granular progress from the immutable filenames and receipts you created. Use fresh
output paths for retries unless the CLI explicitly reports an identical request as safely reusable.
