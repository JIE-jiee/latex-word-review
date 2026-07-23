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
- `scan_source_features()` and `reconcile_source_features()` for bounded
  pre-conversion inventory and post-conversion structural accounting;
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

The worker also uses tex2word 1.0.5's public `reference_doc` parameter. The
`academic-review-v1` DOCX is generated from five fixed OPC/XML members with
fixed ZIP metadata, verified against a pinned SHA-256, materialized only in the
owned conversion stage, and removed in `finally`. It is not a wheel asset. The
profile supplies A4 single-column geometry, Times New Roman/SimSun defaults,
heading/caption/bibliography styles, and review-oriented paragraph rhythm.
`reference-doc/info` evidence is mandatory; an upstream warning or silent
fallback blocks publication. The profile ID, package hash, and generator-config
hash are included in `BackendCapabilities.configuration_sha256`.

Microsoft Word may rewrite style IDs and localized font names while refreshing
live fields. Before sealing the reviewer-facing copy, the field finalizer
reapplies deterministic table/image geometry and restores only trusted
paragraph/run/table properties by stable style name. Word's IDs and document
references are retained, while the pre-refresh style defaults are restored.
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
those layout options. When a raster target was found through `\graphicspath`,
the derived command records and uses the verified root-relative path instead
of leaving an unresolved shorthand for the backend.

Materialization uses at most two workers. Requests with the same cache-key
input share one future, while manifest entries remain in original LaTeX order
regardless of completion order. This bounds Windows memory use and avoids
rendering the same PDF page twice without weakening per-occurrence evidence.

tex2word 1.0.5 has one narrowly version-gated compatibility profile for a
known parser loss: several direct `minipage` children of `figure` can otherwise
collapse to the final image. In the derived tree only, a `minipage` is renamed
to `subfigure` only when it is a direct `figure`/`figure*` child, contains
exactly one `\includegraphics` and one `\caption`, and contains no existing
subfigure/subfloat construct. Every transformation records its source span and
original/derived block hashes in the overlay manifest. Other shapes remain
untouched and fail the normal image-count gate if the backend loses them.

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
reconciles counts, and revalidates each materialized cache's exact member set,
request, content-addressed key, renderer identity, manifest shape, and PNG byte
size/SHA-256. Pixel decoding and canonical re-encoding already passed during
cache creation; repeating them cannot add evidence once the exact PNG bytes
and pixel hash are sealed. A changed manifest, derived dependency/PNG, count,
path, request, renderer, or re-sealed binding therefore still fails closed.

The shared `ImageOverlayBinding` Schema is closed to unknown fields and
requires exactly this security-relevant shape: `status`, `manifest` (`ArtifactRef`),
`original_source_tree_sha256`, `derived_source_tree_sha256`,
`source_image_instances`, `materialized_pdf_instances`, and
`passthrough_raster_instances`. Both `SourceMap.image_overlay` and
`ExportReport.image_overlay` are required; the binding cannot be omitted on a
successful v1alpha export.

## Bounded complex-structure accounting

Before conversion, `scan_source_features()` builds a source-tree-bound,
bounded lower-bound inventory across the already discovered UTF-8 TeX files.
After conversion, `reconcile_source_features()` independently compares that
inventory with DOCX structure and locked-backend counters:

- images: `\includegraphics` instances versus DOCX drawing instances;
- equivalent `tabular` tables versus Word tables, with non-equivalent table
  environments reported as degraded;
- recognized math objects versus final-DOCX OMML objects; backend image/raw
  counters can only add a degraded warning and never fill an OMML deficit;
- static references versus final-DOCX `REF`/`PAGEREF` fields; a field deficit
  blocks handoff because a backend warning does not prove visible fallback text;
- static labels versus version-gated tex2word bookmark names;
- citations, which are inventoried but currently marked `unsupported` rather
  than claimed structurally equivalent.

A provable deficit in a supported count produces non-recoverable
`E_EXPORT_SILENT_LOSS` and prevents reviewer handoff. An independently
present backend math fallback, dynamic/colliding label, or non-equivalent
construct produces recoverable
`E_EXPORT_DEGRADED` for manual inspection. The checks are lower bounds, not
claims of visual or semantic equivalence. Inventory version, counts, feature
results, and diagnostic IDs are sealed into `ExportReport` and revalidated
both before workflow publication and when sealed state is reloaded. The image
overlay manifest remains the stronger per-occurrence evidence for images.

Dynamic conditional regions such as `\ifthenelse`, `\IfFileExists`, and
`\InputIfFileExists` are skipped as complete regions. Their contents do not
contribute to the inventory and therefore do not participate in a claim that a
structure was preserved. Primitive TeX conditionals are handled with the same
fail-closed boundary.

## Conservative source units

The v2 scanner keeps the v1 complete UTF-8 prose paragraphs and additionally
emits byte-exact plain-text islands on a single source line around explicitly
bounded inline constructs. This expands useful body-text coverage without
treating a whole LaTeX line as plain text. Every unit records the half-open byte
span, exact slice SHA-256, line/column evidence, normalized-text hash,
source-tree binding, and a stable C2 `unit_id`. Normalization still collapses
whitespace only; it does not case-fold or apply implicit Unicode normalization.

Every `SourceMap` mapping also carries `text_provenance`: a profile, normalized
review length, and ordered segments with review character offsets, relative
source UTF-8 byte offsets, transformation (`identity` or
`whitespace-collapse`), and `auto_patchable`. Identity spans can later be
narrowed to the exact local insertion/deletion/replacement. Collapsed
whitespace is explicitly lossy and therefore never made automatically
patchable merely because the surrounding bookmark matched.

Island scanning recognizes only a fixed list of commands with a bounded number
of balanced groups. It may expose the inner prose of simple text-formatting
commands such as `textbf` or `emph`, but only when that inner slice is itself
plain text. Math, labels, reference/citation keys, URLs, image arguments,
structural commands, and TeX-special content are skipped. An unknown,
unbalanced, cross-line, or differently shaped command disables all v2 island
discovery for that file rather than guessing; already recognized v1 complete
safe paragraphs may remain. Comments, structural environments, command
definitions, and unsafe quote/dash substitutions remain excluded. Excluded
content is never silently promoted to a low-risk source unit.

## Never-guess bookmark mapping

After conversion, visible `w:t` paragraph text is normalized with the same
whitespace-only profile. A bookmark is inserted only when the normalized text
occurs exactly once in the source-unit set and exactly once in
`word/document.xml`. The Word occurrence may be a whole paragraph or one
contiguous substring of a longer paragraph. A substring is exact only when its
grapheme and word boundaries are safe, its selected raw Word text already
equals the normalized source text character-for-character, and all endpoints
and intervening content are direct plain `w:r` text runs. The exporter splits
only those safe runs and surrounds precisely the matched text. Collapsed Word
whitespace, fields, hyperlinks, content controls, nested runs, overlapping
source ranges, or any ambiguous occurrence remain unanchored. Bookmark names
are deterministic `lwr_*` names derived from the stable unit ID and remain
within Word's 40-character limit.

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
The beta protection summary also does not bind bookmark span endpoints, every
DrawingML layout attribute, every OMML property, or the semantic use of each
relationship ID by a specific drawing element. Those structures are preserved
and checked at the currently documented semantic/count level, but they remain
an explicit manual-review boundary rather than an automatic-patch guarantee.

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
Changes insertion, complete source-feature reconciliation, and
package/structure acceptance all succeed.

## E0 contract evidence

With tex2word 1.0.5, the public E0 source produces 34 total paragraphs, 5 OMML
objects, 1 image, 1 table, 9 upstream bookmarks, and 11 live fields (5 `SEQ`,
6 `REF`, 0 `PAGEREF`). The v2 scanner exposes 23 byte-exact plain-text units:
19 receive exact Word anchors and 4 deliberately remain ambiguous with
`E_MAP_AMBIGUOUS`, so the export is honestly `partial` rather than guessed.

The structure inventory reconciles images 1/1, tables 1/1, math 5/5,
references 6/6, and static labels 8/8 as `preserved`; citations remain
explicitly `unsupported`. The final DOCX enables Track Changes. Separate
synthetic tests cover missing images, tables, math, references, and labels,
visible fallbacks, non-equivalent structures, bounded inventory behavior, a
multi-page PDF with crop/rotation, cache/pixel hashes, and source immutability.

The local Pandoc contract test is skipped with the reason `pandoc executable
not available` when the tool is absent. Unit tests still exercise forced cwd,
fixed argv, UTF-8 bounded execution, timeout cleanup, and partial-artifact
cleanup through a shell-free test launcher.

An aggregate-only local complex-project stress record is maintained in
[`docs/reviews/private-complex-stress.md`](../reviews/private-complex-stress.md).
It is supporting compatibility evidence, not a redistributable fixture or a
replacement for the public E0 closure test.
