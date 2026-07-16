# Export backends and DOCX structure acceptance

This module boundary converts an immutable LaTeX source snapshot into a review
DOCX without importing a backend-native parser tree into the core domain. The
public entry points are:

- `latex_word_review.backends.BackendCapabilities`, `BackendRequest`, and the
  `ExportBackend` protocol;
- `Tex2WordBackend` and `PandocBackend`;
- `scan_source_units()` for conservative byte spans;
- `anchor_source_units()` and `export_review_docx()` for never-guess source
  mapping and atomic publication;
- `inspect_docx()` for read-only structural acceptance;
- `ExportReport.as_payload()` for the C2 `ExportReport` schema payload.

## Backend isolation

`Tex2WordBackend` is locked to tex2word 1.0.5 and calls only the public
`convert_source()` API. It passes the validated source root as `base_dir`, uses
the pure frontend, and disables tex2word's timestamped embedded manifest. The
adapter consumes only returned DOCX bytes and public report counters. It does
not copy or import tex2word parser, IR, writer, or OMML internals.
tex2word 1.0.5 can expose a report entry's severity either as its enum object
or as the enum's string value, depending on the construct. The isolated worker
normalizes both documented runtime shapes before counting warnings and errors;
unknown non-string values fail closed instead of escaping into the parent
process.

`PandocBackend` is a comparison baseline. Its conversion command is a fixed
argument vector equivalent to:

```text
pandoc main.tex --from=latex --to=docx --standalone \
  --output=<owned-stage.docx> --resource-path=.
```

The process always runs with `cwd` equal to the validated source root. The S3
bounded runtime uses no shell, provides a small UTF-8 environment, closes
stdin, limits stdout/stderr, enforces a timeout of at most 60 seconds, and
kills a process that exceeds either bound. Executable paths and raw process
output are not serialized into reports. A missing Pandoc installation is a
clear blocked/skip condition, never a simulated pass.

Both adapters rediscover the source after conversion. Source drift fails the
run. They write only an owned sibling stage, validate it with the canonical S3
DOCX reader, and call `os.replace` only after success. A timeout, non-zero exit,
invalid package, missing output, or tex2word error removes the stage and leaves
an existing destination unchanged.

## Conservative source units

The v1 scanner accepts only complete UTF-8 prose paragraphs. It records the
half-open byte span, exact slice SHA-256, line/column evidence, normalized text
hash, source-tree binding, and a stable C2 `unit_id`. Normalization collapses
whitespace only; it does not case-fold or apply implicit Unicode normalization.

The scanner rejects the whole paragraph when it encounters comments, commands,
labels, references, citations, math delimiters, TeX-special characters, TeX
quote/dash substitutions, or content inside a non-document environment. It is
therefore intentionally incomplete. Excluded content is not silently promoted
to a low-risk source unit.

## Never-guess bookmark mapping

After conversion, visible `w:t` paragraph text is normalized with the same
whitespace-only profile. A bookmark is inserted only when the normalized text
occurs exactly once in the source-unit set and exactly once in
`word/document.xml`. Bookmark names are deterministic `lwr_*` names derived
from the stable unit ID and remain within Word's 40-character limit.

No fuzzy match, positional guess, or tie-breaker is used:

- no target becomes `unmapped` with `E_MAP_UNMATCHED`;
- multiple source or DOCX candidates become `conflict` with
  `E_MAP_AMBIGUOUS`;
- both cases add a recoverable finding, insert no bookmark, and make the export
  `partial` rather than falsely exact.

The `SourceMap` payload records the source location, source/neighbor
fingerprints, bookmark anchor, confidence, status, coverage, and diagnostics.

## Read-only DOCX acceptance

`inspect_docx()` first inventories relationship parts under explicit ZIP/XML
limits and then delegates package trust to the canonical S3 reader. It never
extracts members, follows a relationship, invokes Office/LibreOffice, executes
macros, or opens embedded objects. A validated package reports:

- direct body and total paragraph counts;
- `m:oMath` and `m:oMathPara` counts;
- image relationships and drawing instances;
- tables, bookmarks, and bookmark names;
- `SEQ`, `REF`, and `PAGEREF` field counts;
- total and external relationship counts.

External relationships are counted but never followed. Because the canonical
reader rejects them, other structural inspection is blocked and the result
contains `E_DOCX_UNSAFE_RELATIONSHIP`. Malformed, encrypted, duplicate-member,
oversized, traversal, DTD/entity, or otherwise invalid packages produce
`E_DOCX_INVALID_PACKAGE` findings.

`openability` remains `not_run`: package and structure acceptance is not a
claim that Microsoft Word was launched. The final pipeline publishes only
after backend conversion, bookmark insertion, and package/structure acceptance
all succeed.

## E0 contract evidence

With tex2word 1.0.5, the public E0 source produces 34 total paragraphs, 5 OMML
objects, 1 image, 1 table, 9 upstream bookmarks, and 11 live fields (5 `SEQ`,
6 `REF`, 0 `PAGEREF`). The conservative scanner exposes two unique plain-text
units; the integrated pipeline maps both exactly and produces 11 bookmarks in
the final review DOCX.

The local Pandoc contract test is skipped with the reason `pandoc executable
not available` when the tool is absent. Unit tests still exercise forced cwd,
fixed argv, UTF-8 bounded execution, timeout cleanup, and partial-artifact
cleanup through a shell-free test launcher.

An aggregate-only local complex-project stress record is maintained in
[`docs/reviews/private-complex-stress.md`](../reviews/private-complex-stress.md).
It is supporting compatibility evidence, not a redistributable fixture or a
replacement for the public E0 closure test.
