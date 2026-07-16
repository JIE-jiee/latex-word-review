# Export backends and DOCX structure acceptance

This module boundary converts an immutable LaTeX source snapshot into a review
DOCX without importing a backend-native parser tree into the core domain. The
backend receives a separately derived image-overlay tree; the authoritative
snapshot remains the source of `ReviewIR`, `SourceMap`, and source hashes. The
public entry points are:

- `latex_word_review.backends.BackendCapabilities`, `BackendRequest`, and the
  `ExportBackend` protocol;
- `Tex2WordBackend` and `PandocBackend`;
- `scan_source_units()` for conservative byte spans;
- `build_image_overlay()` for a source-bound, derived image working tree;
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
pandoc main.tex --from=latex --to=docx --standalone --output=<owned-stage.docx> --resource-path=.
```

The process always runs with `cwd` equal to the validated source root. The S3
bounded runtime uses no shell, provides a small UTF-8 environment, closes
stdin, limits stdout/stderr, enforces a timeout of at most 60 seconds, and
kills a process that exceeds either bound. Executable paths and raw process
output are not serialized into reports. A missing Pandoc installation is a
clear blocked/skip condition, never a simulated pass.

Both adapters rediscover the derived input after conversion. Derived-input or
authoritative-source drift fails the run. They write only an owned sibling
stage, validate it with the canonical S3 DOCX reader, and call `os.replace`
only after success. A timeout, non-zero exit, invalid package, missing output,
or tex2word error removes the stage and leaves an existing destination
unchanged.

## Derived image overlay

Before a conversion backend runs, `export_review_docx()` creates a sibling
`<review.docx>.image-overlay/` working tree. It copies the discovered snapshot,
rechecks the original discovery and every source digest, and rewrites only the
copied TeX occurrences that can be handled deterministically. The backend is
given this derived root and its derived tree hash. `ReviewIR` and source units
are still built from the original snapshot, and the original LaTeX, PDF, and
raster bytes are rechecked before and after overlay publication.

With the `pdf-figures` extra, a static `\includegraphics` reference to a PDF is
resolved inside the source root and rendered by bounded pypdfium2/Pillow worker
code into a canonical RGB PNG. Page selection, `trim`/`viewport`/`clip`, and
rotation are baked into the pixels. The safe layout-only options `width`,
`height`, `totalheight`, `scale`, and `keepaspectratio` remain on the derived
command. Existing PNG/JPEG images pass through unchanged when they use only
those layout options.

SVG, EPS/PostScript, dynamic or ambiguous paths/options, malformed commands,
out-of-range pages, rendering failures, and unsupported pixel operations
produce path-safe `manual_required` diagnostics. If any occurrence is manual,
export stops before invoking the backend; it never silently calls an external
SVG/EPS/PostScript converter.

The canonical `image-overlay-manifest.json` records:

- original and derived main-document, tree, and discovery-profile hashes;
- the quality profile and source/materialized/passthrough/manual counts;
- each occurrence ID, original command and command hash, source character and
  UTF-8 byte span, line/column, original target/options, and resolved source
  digest when resolution succeeds;
- the normalized render request and request hash, derived command and hash,
  content-addressed cache key, cache-manifest/PNG paths, PNG-byte and decoded
  pixel hashes, dimensions/DPI, and renderer identity when materialization
  succeeds;
- stable manual diagnostic IDs and locations when it does not.

The manifest is canonical JSON, not by itself a public sealed domain-object
envelope. Its artifact reference and SHA-256, original/derived tree hashes,
status, and instance counts are embedded in the versioned `SourceMap` and
`ExportReport` payloads. The high-level workflow validates and writes those as
sealed contract objects. On `workflow status` and `workflow receive`, it
requires the SourceMap/ExportReport bindings to be identical, re-hashes the
fixed-path manifest ArtifactRef, rediscovers the original and derived trees,
reconciles counts, and revalidates each materialized PNG/cache binding. A
changed manifest, derived dependency/PNG, count, path, or re-sealed binding
therefore fails closed.

The shared `ImageOverlayBinding` Schema is closed to unknown fields and
requires exactly this security-relevant shape: `status`, `manifest` (`ArtifactRef`),
`original_source_tree_sha256`, `derived_source_tree_sha256`,
`source_image_instances`, `materialized_pdf_instances`, and
`passthrough_raster_instances`. Both `SourceMap.image_overlay` and
`ExportReport.image_overlay` are required; the binding cannot be omitted on a
successful v1alpha export.

After conversion, structural inspection counts drawing/image instances. Export
fails with `E_EXPORT_SILENT_LOSS` when the DOCX contains fewer image instances
than the LaTeX source contained `\includegraphics` occurrences. This is a
fail-closed lower-bound count check, not a claim of one-to-one DOCX relationship
mapping, visual equality, or pixel equality for every Word image. The overlay
manifest remains the per-source-occurrence evidence.

## Conservative source units

The v1 scanner accepts only complete UTF-8 prose paragraphs. It records the
half-open byte span, exact slice SHA-256, line/column evidence, normalized text
hash, source-tree binding, and a stable C2 `unit_id`. Normalization collapses
whitespace only; it does not case-fold or apply implicit Unicode normalization.

Every `SourceMap` mapping also carries `text_provenance`: a profile, normalized
review length, and ordered segments with review character offsets, relative
source UTF-8 byte offsets, transformation (`identity` or
`whitespace-collapse`), and `auto_patchable`. Identity spans can later be
narrowed to the exact local insertion/deletion/replacement. Collapsed
whitespace is explicitly lossy and therefore never made automatically
patchable merely because the surrounding bookmark matched.

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
fingerprints, text provenance, bookmark anchor, confidence, status, coverage,
diagnostics, and the image-overlay artifact/tree binding.

## Track Changes baseline

Anchoring also creates or repairs the Word settings content type and document
relationship, and publishes exactly one enabled `w:trackRevisions` control in
`word/settings.xml`. A disabled or duplicate control fails closed. The exported
baseline must contain no pre-existing insertion, deletion, move, or formatting
revision elements; otherwise export reports silent-loss risk instead of mixing
backend history with the reviewer's later edits.

This setting asks Microsoft Word to track later edits; it does not prove that a
reviewer left Track Changes enabled or avoided **Accept All**. Ingest separately
binds the returned original to this exact exported baseline and compares the
returned reject/original semantic view. Accept All, untracked visible drift,
and missing/changed export bookmarks fail before a `ChangeSet` is accepted.
That comparison is deliberately scoped: formatting, paragraph-mark revisions,
OMML, and images still require manual integrity review.

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
claim that Microsoft Word was launched. A separate Windows-only Microsoft Word
COM contract harness covers real save/reopen and tracked-edit negative cases;
it does not turn package inspection into an Office sandbox. The final pipeline
publishes only after overlay creation, backend conversion, bookmark/Track
Changes insertion, image-count reconciliation, and package/structure
acceptance all succeed.

## E0 contract evidence

With tex2word 1.0.5, the public E0 source produces 34 total paragraphs, 5 OMML
objects, 1 image, 1 table, 9 upstream bookmarks, and 11 live fields (5 `SEQ`,
6 `REF`, 0 `PAGEREF`). The conservative scanner exposes two unique plain-text
units; the integrated pipeline maps both exactly, produces 11 bookmarks in the
final review DOCX, and enables Track Changes. Separate synthetic tests cover a
multi-page PDF with page selection/crop/rotation, cache and pixel hashes,
original-source immutability, manual image paths, and DOCX image-count loss.

The local Pandoc contract test is skipped with the reason `pandoc executable
not available` when the tool is absent. Unit tests still exercise forced cwd,
fixed argv, UTF-8 bounded execution, timeout cleanup, and partial-artifact
cleanup through a shell-free test launcher.

An aggregate-only local complex-project stress record is maintained in
[`docs/reviews/private-complex-stress.md`](../reviews/private-complex-stress.md).
It is supporting compatibility evidence, not a redistributable fixture or a
replacement for the public E0 closure test.
