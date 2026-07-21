---
name: latex-word-review
description: Launch or explicitly orchestrate latex-word-review for a local, immutable, auditable LaTeX-to-Word review cycle with tracked-change ingestion, per-change approval, exact diff preview, clean-copy application, verification, ledgers, and audit bundles. Use when Codex must help a Windows user prepare a LaTeX project for Word review, process a returned tracked-changes DOCX, resume a review session, or explain the safe two-gate workflow without overwriting the original.
---

# LaTeX Word Review

Use the installed `latex-word-review` application and CLI as the only workflow authority. Keep this
Skill thin: do not reimplement DOCX parsing, hashes, approval state, patching, verification, or
sealed-object validation.

## Establish the boundary

1. Treat the original LaTeX tree and the returned Word file as immutable.
2. Never operate on a private document merely because it is discoverable.
3. Never regenerate an entire LaTeX project from Word or replace the authoritative source with a
   Word-derived document.
4. Keep paper contents, reviewer identities, ledgers, and bundles local unless the user explicitly
   authorizes sharing a specific artifact.
5. Stop on any hash, Schema, path, capability, or safety error. Report the stable error code and
   affected stage; never bypass a failure or edit a sealed JSON object.
6. Never infer or automatically cross either gate. Per-change approval and the later exact-diff
   confirmation are human decisions, not Agent completion steps.

## Prefer the local Windows application

For an ordinary request such as “review this LaTeX paper in Word” or “process the returned Word”,
launch the local application and let the user make decisions there.

Select the command prefix:

- In a source checkout with `pyproject.toml` and `uv.lock`, use
  `uv run --frozen latex-word-review`.
- With an installed or portable CLI, use `latex-word-review`.
- Treat this Skill revision as compatible with the `0.2.x` application/CLI and `v1alpha` sealed
  objects. If the installed version has another major/minor line or lacks a named command, stop and
  obtain the matching Skill.
- Run `--version` and `doctor` before the first workflow action. Do not install Word, TeX, Pandoc,
  or any other external tool implicitly.

Then run `<prefix> app`. The application opens a loopback-only Chinese interface and stores durable
sessions below `%LOCALAPPDATA%\LatexWordReview` unless the user explicitly chose another data root.

Explain only the four user actions:

1. choose `main.tex` and generate the Word review;
2. select the returned `.docx`;
3. decide every change and complete the first gate;
4. inspect the exact per-file diff and personally confirm the second gate.

After launch, stop command orchestration and hand control to the user. Do not inspect or echo private
before/after text merely to prove the application opened. Closing the browser tab does not exit the
server; tell the user to use **Exit application**. Reopening the application resumes from sealed disk
evidence, not browser cache.

## Preserve both human gates

The first gate finalizes the immutable ApprovalSet. It records user intent but does not modify
LaTeX and does not grant unsafe changes automatic eligibility.

The second gate shows the exact current PatchPlan hash and unified diff. Only the user's explicit
confirmation in the local application may start result generation. Approval of Word changes is not
permission to apply a patch.

The restricted safe-text bulk action is still a user control. It may fill only still-undecided exact
`plain_text_candidate` changes and must never overwrite an existing decision. It must exclude
formulas, references, structure, moves, formatting, comments, low-confidence mappings, and
conflicts. A request to “finish” or “process everything” is not blanket acceptance.

If planning reports `accepted_but_blocked`, do not work around it or infer replacement decisions.
Hand control back so the user explicitly selects the application's re-approval action and changes
the blocked items to manual or rejected. Preserve existing decisions and every immutable revision.

## Use CLI only for an explicit advanced request

Use CLI commands only when the user explicitly requests command-line/Agent orchestration,
automation, detailed evidence inspection, or granular recovery. State that the local application is
the supported ordinary-user path.

### High-level recovery through returned-Word ingest

The following commands remain the resumable CLI sequence:

1. `workflow init <source> <run-root> --main main.tex`
2. `workflow export <run-root>`
3. `workflow receive <run-root> <returned.docx>`
4. `workflow status <run-root>`
5. `workflow clean <run-root>`
6. `workflow clean <run-root> --execute`

The receive step must archive the returned original and verify it against the sealed export baseline before ingest.
Stop on baseline drift, Accept All, untracked edits, damaged anchors, or a mismatched round.

`workflow status <run-root>` covers only snapshot/export/receive in the legacy granular workflow.
Once approval begins, resume from the latest sealed ApprovalSet, PatchPlan, and explicit output
paths. The high-level workflow does not approve changes or apply them to LaTeX.

`workflow clean <run-root>` is a dry-run and must not remove anything without `--execute`. Use the
execute form only after the user reviews and explicitly authorizes the listed tool-owned staging
directories.

### Granular recovery

Use the safety-critical commands only after reading their current `<command> --help`:

- `doctor` diagnoses but never installs.
- `snapshot` creates an immutable source copy.
- `export` creates the review DOCX and SourceMap.
- `archive` preserves the returned Word original.
- `ingest` creates the complete ChangeSet.
- `approve` records user-supplied decisions in new revisions.
- `plan` creates a dry-run PatchPlan and exact diff.
- `apply` writes only a new absent destination.
- `verify` reconciles the actual diff and creates clean/latexdiff evidence.
- `ledger` creates the bound JSON/HTML decision record.
- `run-manifest` seals the delivery inventory.
- `bundle` creates an allowlisted audit ZIP.
- `verify-bundle` checks the delivered archive.

Do not treat this list as authorization to execute the next command. Before the first gate, present
each change and return control to the user. Before `apply`, present the exact diff and return control
again. The Agent may prepare evidence and explain commands, but must not proxy either human action.

## Report and resume safely

At every pause, identify:

- the completed stage;
- the immutable input hashes or sealed object paths;
- the next safe action;
- every blocked, degraded, manual, or unverified item.

Use fresh output paths for retries. Verification retry creates a new immutable attempt and never
overwrites an earlier report. Missing TeX tools may leave a valid revised LaTeX copy with partial
PDF evidence; do not report that as full completion.

Do not claim a public installer or portable download exists while the repository's Windows binary
license audit blocks redistribution. If discussing an unsigned future binary, instruct the user to
verify the official release SHA-256 and signing status rather than bypassing SmartScreen.
