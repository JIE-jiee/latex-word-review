# Codex Skill forward-test record

Date: 2026-07-16

Scope: repository `skills/latex-word-review/` with the `latex-word-review 0.1.0b1` CLI and public
Apache-2.0 E0 fixture only. No private paper, returned reviewer file, repository source, fixture, or
authoritative input was modified.

## Structural validation

The Skill was initialized with the official `skill-creator` initializer and contains only:

- `SKILL.md`;
- `agents/openai.yaml`.

The official `quick_validate.py` passed. On this Windows host it had to be invoked with
`PYTHONUTF8=1` because the validator otherwise used the system GBK default for a UTF-8 file. The
repository also carries `tests/test_skill_contract.py`, which checks the frontmatter, UTF-8 UI
metadata, command coverage, absence of placeholders/personal paths, and the no-business-code
two-file boundary.

## Forward test 1: no delegated decisions

Prompt shape: process the public returned tracked-changes fixture, make as much safe progress as
possible, and report what is needed next.

Result:

- snapshot, export, inspect, immutable return archive, ingest, sealed-object validation, and
  `ApprovalSet` initialization completed;
- nine raw events were retained as seven normalized changes;
- all seven decisions remained `pending`;
- the Skill stopped at the human approval gate and did not finalize, plan, apply, or modify source;
- the static returned fixture was correctly reported as unmatched to a fresh export rather than
  being force-bound to unrelated bookmarks.

This test confirms that “process the return” is not treated as authorization to accept changes. It
also demonstrates why a real returned DOCX must descend from the DOCX exported by the same run; a
canonical static revision fixture is an extraction oracle, not a substitute for that binding.

## Forward test 2: explicit decision and apply policy

Prompt shape: accept only exact, uniquely mapped plain-text insertion/deletion/replacement changes
with confidence at least `0.99`; mark everything else `manual`; inspect the dry-run diff; then apply
through the separately authorized second gate and complete verification/bundling.

Result:

- a returned DOCX was derived from the current public export with one synthetic replacement;
- two raw events normalized to one exact replacement with confidence `1.0`;
- one change was accepted; manual, blocked, excluded, overlapping, and unapproved counts were zero;
- the dry-run changed one phrase in one source line and no other source byte;
- apply wrote a new work tree only;
- clean XeLaTeX compilation, reference/resource checks, patch reconciliation, `latexdiff` generation,
  ledger generation, and offline audit verification all passed;
- the audit ZIP contained exactly seven allowlisted delivery entries;
- the six-file public fixture and returned Word original retained their pre-run hashes.

The agent deliberately did not use the convenience demo's combined control flow for the two
decision gates: it called the authoritative CLI stages individually, reviewed the plan, and invoked
`apply` only after the prompt's separate authorization.

## Conclusion

The Skill forwards both under-authorized and explicitly authorized requests to the same core state
machine without copying OOXML, approval, patch, or verification logic. It pauses at the correct human
boundary, preserves resumability, and can complete the public workflow when an exact policy and the
second-gate instruction are both present.
