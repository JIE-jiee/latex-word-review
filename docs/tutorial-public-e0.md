# Public E0 CLI tutorial

This tutorial runs a complete, reviewable LaTeX–Word round trip from the repository's
synthetic Apache-2.0 E0 fixture. It does not read a private paper and does not require
Microsoft Word.

The demo uses the real `latex-word-review` CLI for every project operation. Between
`export` and `archive`, the helper script performs the one action that normally happens
outside this project: it adds a deterministic tracked replacement to the DOCX just
exported by this run. That returned DOCX therefore carries the same fresh bookmark and
hash bindings as its source export.

## Prerequisites

- Windows (the maintained platform);
- Python 3.12 or 3.13;
- `uv` for the copyable source-checkout commands below;
- `latexmk`, XeLaTeX and `latexdiff` only if you want PDFs and the audit bundle.

From a clean source checkout:

```console
uv sync --frozen --group fixture --python 3.12
uv run --frozen python scripts/run_public_e0_cli_demo.py
```

The default output is a fresh
`build/public-e0-cli-demo/<run-id>/` directory. A specific path is also supported, but
it must not exist:

```powershell
uv run --frozen python scripts/run_public_e0_cli_demo.py --output build/my-public-e0-run
```

The no-clobber rule is intentional. Re-run without `--output`, or choose another fresh
directory, instead of deleting or overwriting audit evidence.

## What actually runs

The script starts child processes using `python -m latex_word_review`; it does not call
the workflow's Python business APIs in-process. The receipts printed for each step show
this sequence:

```text
new-run
  -> snapshot
  -> export --backend tex2word
  -> deterministic public Word review simulation
  -> reader-capabilities
  -> archive
  -> ingest
  -> approve init
  -> approve set (once per ChangeSet item)
  -> approve finalize
  -> plan
  -> apply
  -> [verify -> ledger -> run-manifest -> bundle -> verify-bundle]
```

The bracketed stages run only when both real `latexmk` and `latexdiff` executables are
available. If either is absent, the core review loop still completes and
`demo-summary.json` records `verification.status = "degraded"`, the exact missing tool
names and no ledger or bundle claim. A failed installed tool is a real failure and is not
silently downgraded.

For a fast core-only run even when TeX tools are installed:

```console
uv run --frozen python scripts/run_public_e0_cli_demo.py --skip-verification
```

That explicit choice is recorded as `verification.status = "skipped"` with
`reason = "user_requested"`.

## Approval and apply policy

The demo decides every `ChangeSet` item individually. It accepts only changes satisfying
all of these conditions:

- kind is `insertion`, `deletion` or `replacement`;
- safety class is `plain_text_candidate`;
- bookmark resolution is exact with confidence at least `0.99`;
- a sealed source location is present.

Everything else is marked `manual`; it is retained in the approval ledger but never
auto-applied. `approve finalize` only seals those decisions. The separate `plan` command
then recalculates the exact dry-run patch, and only the later `apply` command writes a
new `revised-clean/` work tree.

The supplied E0 run synthesizes one exact plain-text replacement, so a successful core
summary reports one accepted change, a `ready` plan and a non-empty unified diff.

## Outputs to inspect

The most useful files under the run directory are:

```text
demo-summary.json
objects/source-manifest.json
export/review.docx
returned/returned-original.docx
objects/changeset.json
objects/approval-final.json
plan/patch-plan.json
plan/changes.patch
revised-clean/
verification/verification-report.json  # only after real tool verification
verification/latexdiff.tex             # only after real tool verification
verification/latexdiff.pdf             # only after real tool verification
ledger/ledger.json                     # only after real tool verification
ledger/ledger.html                     # only after real tool verification
audit.zip                              # only after real tool verification
```

`demo-summary.json` is the quickest machine-readable result. The script also verifies
that the committed E0 source fixture and the simulated returned Word original remain
byte-for-byte unchanged after snapshot/archive/ingest/apply.

For deeper inspection:

```powershell
uv run --frozen latex-word-review validate build/my-public-e0-run/objects/changeset.json --schema ChangeSet
uv run --frozen latex-word-review validate build/my-public-e0-run/objects/approval-final.json --schema ApprovalSet
uv run --frozen latex-word-review validate build/my-public-e0-run/plan/patch-plan.json --schema PatchPlan
uv run --frozen latex-word-review verify-bundle build/my-public-e0-run/audit.zip --run-manifest build/my-public-e0-run/objects/run-manifest.json --verification build/my-public-e0-run/verification/verification-report.json
```

The last command applies only to a full verified run.

## Reading the result correctly

- `core_pass` means snapshot through apply passed and verification was explicitly
  skipped.
- `core_pass_degraded` means snapshot through apply passed, but at least one optional
  external TeX tool was unavailable.
- `full_pass` means clean compilation, marked `latexdiff` compilation, ledger creation
  and offline audit-bundle verification all passed.
- A nonzero script exit means a required CLI step or an installed verification tool
  failed. Keep the partial directory for diagnosis and re-run into a new path.

This is an executable public tutorial, not a substitute for reviewing real decisions.
For an actual returned Word document, replace the synthetic review step with the human
reviewed file while preserving the same `archive -> ingest -> approve -> plan -> apply`
gates.
