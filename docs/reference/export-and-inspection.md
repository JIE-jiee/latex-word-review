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

The process always runs with `cwd` equal to the validated source root. The
conversion runtime uses no shell, provides a small UTF-8 environment, closes
stdin, limits stdout/stderr, defaults to 300 seconds, and enforces a hard
maximum of 600 seconds for each conversion invocation. When a clean review and
an existing-changes display are both requested, each conversion receives its
own full budget; elapsed time from the clean conversion is not subtracted from
the display conversion. The Pandoc version probe, image renderer, Word COM
refresh, and other non-conversion commands retain their narrower existing
limits (at most 60 seconds). Executable paths and raw process output are not
serialized into reports. A missing Pandoc installation is a clear blocked/skip
condition, never a simulated pass.

Both adapters rediscover the derived input after conversion. Derived-input or
authoritative-source drift fails the run. They write only an owned sibling
stage, validate it with the canonical S3 DOCX reader, and call `os.replace`
only after success. A timeout, non-zero exit, invalid package, missing output,
or tex2word error removes the stage and leaves an existing destination
unchanged.

## Existing LaTeX change markup

The export stage recognizes the canonical [CTAN `changes`](https://ctan.org/pkg/changes) forms
`\added[options]{new}`, `\deleted[options]{old}`, and `\replaced[options]{new}{old}` only when a
supported static `changes` package declaration is visible in the discovery-bound project
dependencies. This is a bounded syntactic provenance gate, not proof of which package file TeX
actually loaded. One balanced optional argument is accepted and ignored for display rendering. A
bare canonical call, a discovery-bound project-local `changes.sty`, a statically visible
`\input@path` modification, or a direct or dynamic source-local definition/redefinition of
`\added`, `\deleted`, or `\replaced` fails closed with `E_SCHEMA_INVALID`. The scanner does not use
TeX load order to guess which definition would win.

### Alias provenance gate

`\add` and `\delete` are not accepted merely because their control-sequence names match a short
alias. Every alias that is actually called must have exactly one accepted static semantic source:

- `\add` may come from exactly one static `trackchanges` package declaration, whose documented
  command is [`\add`](https://trackchanges.sourceforge.net/help_stylefile.html), or from exactly one
  local direct wrapper to `\added`;
- `\delete` may come only from exactly one local direct wrapper to `\deleted`; it is not a documented
  `trackchanges` command;
- a local wrapper target must independently satisfy the canonical static-`changes` declaration gate;
- a bare call, an incompatible local definition, a source inside a dynamic conditional, conflicting
  definitions, or multiple otherwise valid sources fails closed with `E_SCHEMA_INVALID`;
- an optional alias argument is accepted only when the one accepted source supports it.

The audit covers all discovery-bound UTF-8 `.tex`, `.sty`, and `.cls` files and recognizes only fixed
direct forwarding shapes (including supported command-definition and direct `\let` forms). Its claim
is limited to **a statically visible canonical package declaration plus no discovery-bound project-tree
shadow or detected `\input@path` search override**. It does not execute TeX, run `kpsewhich`, resolve
`TEXINPUTS`/TEXMF or system/user package trees, expand arbitrary wrappers, choose definitions by load
order, or bind the actually loaded package path and hash. A discovery-bound `changes.sty` or
`trackchanges.sty` is therefore treated as shadowing, not as evidence of the upstream package.

### Discovery and scan boundary

- Only direct, literal macro calls are recognized.
- The main file is scanned only inside the `document` body; other discovery-bound UTF-8 `.tex` files are scanned in full.
- TeX comments, `\verb`, `verbatim`, `verbatim*`, `Verbatim`, `minted`, and `lstlisting` are skipped. Definition bodies are inert for call discovery, while definitions of the target canonical names and aliases are still provenance-audited.
- Every branch of a dynamic conditional region is skipped. Conditions are not evaluated.
- Wrapper macros and `\csname` calls are not expanded.

The bounded profile accepts at most 10,000 revision-macro instances, 32 levels of revision-macro
nesting, 32 levels of argument-group nesting, and 8 KiB per optional argument. Independent Word
matching has a document-wide budget of 100,000 candidates and 25,000,000 work units. Text searches
stop as soon as one extra location proves ambiguity; textual candidates are reused for style checks;
and paragraph joins, each remaining search window, per-character style checks, masks, and state writes
reserve work before execution. Exceeding any limit fails closed.

A revision argument may contain plain UTF-8 text (including CJK and emoji), ordinary source line breaks within one paragraph, nested plain groups, nested revision macros, escaped literals (`\ `, `\#`, `\$`, `\%`, `\&`, `\_`, `\{`, `\}`), and these simple inline wrappers. Blank-line paragraph breaks are rejected:

`\emph`, `\textbf`, `\textit`, `\textmd`, `\textnormal`, `\textsc`, `\textsf`, `\textsl`, `\textsubscript`, `\textsuperscript`, `\texttt`, `\textrm`, `\textup`, `\underline`.

Unescaped structural characters and every other control command are rejected in a directly recognized argument. In particular, mathematics, cross-references, citations, images, environments, footnotes, labels, and paragraph breaks fail closed with `E_SCHEMA_INVALID` before the conversion backend runs. Calls inside skipped dynamic regions are outside this detection guarantee.

Empty change payloads such as `\added{}`, `\deleted{}`, or `\replaced{}{}` remain part of the source
inventory, but they cannot establish non-empty display evidence. Display export therefore fails
closed with `E_SCHEMA_INVALID` instead of treating a clean-looking Word file as a verified display.

### Two conversion profiles

The project injects in-memory macro definitions and wraps the locked `tex2word==1.0.5` macro expansion, intermediate representation, `textcolor` / `sout` handling, and Word writer.

| Profile | Semantics | Artifact |
|---|---|---|
| clean | additions and replacements keep new text; deletions disappear | `review.docx` |
| display | additions are `0000FF` blue; deletions are `0000FF` blue with single strikethrough; replacements render old blue single-struck text immediately followed by new blue text | `existing-changes-display.docx` |

The display profile adds no highlighting. It emits static OOXML `w:color="0000FF"` and `w:strike`
formatting, not native Word revision elements, and Track Changes is not enabled. In particular,
`\replaced{new}{old}` projects to the old text with blue single strikethrough immediately followed by
the new text in blue with strike explicitly absent; no highlight is used.

The display artifact is never an accepted ingestion source. `review.docx` is the sealed clean baseline;
only its exact editable saved copy has the review role that may be sent out and imported on return.
After field finalization, the display package receives standard `word/settings.xml` `w:docVars`:

- `LWR_ARTIFACT_ROLE=latex_changes_display_docx`;
- `LWR_ARTIFACT_PROFILE=lwr-existing-changes-display-v1`;
- `LWR_RUN_ID=<current run ID>`.

The workflow seals the final display digest as non-returnable. Receive first rejects an exact sealed
display digest, then reads the embedded marker and rejects a saved or ZIP-repacked copy that retains
the display role. The review baseline must contain no reserved marker. Color is deliberately not a role
signal: a legitimate returned review with blue tracked text remains eligible for the normal baseline
checks. If an external tool maliciously strips the marker, that does not grant a review role; the file
still has to pass the clean reject-view, bookmark, run, and revision-semantic gates.

The clean projection does not by itself create new exact LaTeX `SourceMap` provenance for macro-derived text. A reviewer's later Word edit in such a fragment may still be recorded completely in the ledger, but it remains manual when no exact source location can be proven; this feature does not promise automatic writeback for that text.

With zero recognized macros, no display artifact is generated and the optional report field does not reference one. With one or more recognized macros, `ExportReport.existing_changes_display_docx` carries an artifact whose role is `latex_changes_display_docx`. Both Word files are built and validated in the same staged export, and any failure prevents publication of the final `export/` directory.

Validation uses source-derived per-instance expectations, not an aggregate style-count gate. For each
top-level revision-macro tree, the scanner records a diagnostic source-call SHA-256 and offset, its
visible text/strike segments, and the number of nested macro instances it covers. Coverage across
expectations must equal the complete source inventory. The independent DOCX reader then canonicalizes
paragraph whitespace and requires:

- one non-overlapping Word interval per non-empty expectation;
- every matched character to be `0000FF` blue and to have exactly the expected strike state;
- repeated identical text/strike expectations to have the required multiplicity;
- each deletion boundary to be uniquely proven by neighboring visible text in the same paragraph;
- verified visible-text and strike-character totals to equal the source-derived totals;
- the clean/display deltas for blue, strike, and highlight characters to equal the source profile;
- equations, images, tables, fields, relationships, and story parts to match the clean review;
- the exact OPC part-name set and the semantic content of `[Content_Types].xml`, `styles.xml`,
  `numbering.xml`, `fontTable.xml`, every `word/theme/*.xml`, and general `settings.xml` to match;
- no native revision elements and no enabled Track Changes setting.

Protected-part comparison preserves real style, numbering, font, theme, content-type, and setting
changes. It ignores only known Word save identifiers, numbering-definition identifiers after resolving
their references, the clean/display Track Changes role difference, and the complete, exact display-role
`docVars` triplet shown above. Missing, malformed, duplicated, or additional reserved marker values and
all unrelated `docVars` remain evidence and fail closed when they drift.

Thus insufficient non-overlapping matches, an incorrect style, a non-unique or missing same-paragraph
deletion boundary, a clean/display style-delta mismatch, or non-revision structure loss fails with
`E_EXPORT_SILENT_LOSS`. Unchanged blue or struck text elsewhere in the paper is present in both views
and cannot compensate for a missing display-profile delta. The generation profile itself adds no
highlighting. Unrelated highlighting is allowed only when it is unchanged between the clean and display
views; highlighting is not artifact-role evidence.

The deletion-context check prevents struck text from being moved to an unproven paragraph boundary.
It still does not cryptographically bind a Word character position to a LaTeX source offset or grant
writeback provenance. The source digest and offset remain diagnostic provenance; exact Word-to-LaTeX
writeback still depends on the separate clean-review bookmark and SourceMap contracts.


This is an adopt-and-wrap decision. The project does not rebuild a general TeX parser, general intermediate representation, or OMML writer; it does own the bounded scanner, safety policy, two profiles, artifact roles, and independent OOXML acceptance checks.

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
explicitly `unsupported`. The final `review.docx` enables Track Changes;
`existing-changes-display.docx` does not. Separate synthetic tests cover missing
images, tables, math, references, and labels,
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
