# E0 public fixture — Microsoft Word visual QA

Status: **pass**
Review date: 2026-07-16
Application: Microsoft Word for Windows `16.0.20131.20126`

## Artifacts reviewed

| Artifact | SHA-256 |
|---|---|
| `tests/fixtures/e0-minimal-paper/base/review-base.docx` | `0c9ded207528a84ef82bc63eccc7ca4a6c90870ac804fea333cdb523632fca97` |
| `tests/fixtures/e0-minimal-paper/returned/returned-reviewed.docx` | `1bfbfca694921d5caa7d1bc4f71a7a6f4d3d5b91aeb855f14080e3d20859be2b` |

Both files were opened directly from the public fixture. Word did not display an unreadable-content,
repair, permission, or conversion warning. The returned document displayed only the expected
informational notice that Track Changes is enabled. Neither document was edited or saved; their
post-review hashes remained identical to the values above.

## Page and feature checklist

- The base and returned documents both render as three pages without clipped body text, floating
  objects outside the page, or an unexpected blank page.
- The bilingual title fits on one line. English and Chinese body text render legibly.
- The native equation, synthetic response figure, bilingual figure/table captions, three-column
  table, headings, footer, page number, and reference entry are present and aligned.
- Word visibly renders the insertion, deletion, replacement, move-from/move-to, and bold-format
  revisions on the review-operations page. The formatting balloon names `Reviewer Alpha`.
- Word visibly renders both comment cards on the final page, with `Reviewer Alpha` and
  `Reviewer Beta`, the expected comment texts, and the intended range/point anchors.
- Show/Hide formatting marks was enabled in the reviewer profile during inspection; visible dots
  and paragraph marks are UI formatting aids, not fixture content.

## Evidence handling

No screenshot is committed. The Word window exposed the local Office profile and recent-document
UI outside the fixture, so retaining full-window screenshots would weaken the repository privacy
boundary. This versioned checklist binds the exact application version and artifact hashes; the
machine-verifiable structure, revision oracle, privacy, provenance, and determinism evidence is
regenerated with:

```console
uv run --group fixture python scripts/qa_e0_public_fixture.py
```

Structural QA does not replace visual QA, and this manual review does not replace the deterministic
OOXML and ChangeSet gates. Release requires both.
