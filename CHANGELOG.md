# Changelog

All notable changes to this project will be documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) after the public contracts are frozen.

## [Unreleased]

The canonical repository identity is now
[`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review). The source repository
and private vulnerability reporting are public and enabled; tag creation and artifact publication
remain explicit maintainer promotion steps. No GitHub or package-index release is implied by the
source version below.

### Added

- Add a grouped documentation index that separates user workflows, public contracts, upstream
  decisions, security material, and maintainer evidence.
- Add a detailed Chinese Windows guide covering installation, the two approval gates, PDF-page
  previews, verification, ledger creation, delivery staging, and offline audit-bundle verification.
- Add a public development-provenance record that discloses the maintainer-driven Vibe Coding and
  OpenAI Codex collaboration, separates human and AI responsibilities, and states the evidence and
  limitations behind the project.

### Changed

- Ignore common Python, LaTeX, editor, and interrupted-tool artifacts without hiding legitimate
  PostScript figure sources.
- Rebuild the GitHub README around the real LaTeX/Word review pain, the auditable-review-bridge idea,
  concrete outputs, supported and manual-only behavior, upstream reuse, and three runnable entry
  paths.
- Correct the Windows source-checkout instructions so bounded child processes can resolve the
  virtual environment's executables, and document explicit `local_private` handling for real papers.
- Clarify that text-patch baseline verification is not whole-DOCX integrity, manual LaTeX edits must
  follow automatic-tree verification in a separate copy, `pagebox` remains manual, optional Codex
  use has a separate data-policy boundary, and delivery staging is fail-closed no-clobber.
- Clarify the last passing Windows evidence commit, untagged rehearsal versus formal promotion,
  reusable release-checklist status, and the receive-only boundary of `workflow status`.
- Add canonical Homepage, Repository, Documentation, Issues, Changelog, clone, support, and private
  vulnerability-reporting URLs for the public source repository.
- Narrow the maintained platform scope to Windows with CPython 3.12/3.13. Linux and macOS are no
  longer CI, release, or support targets; historical upstream and security evidence remains intact.

### Removed

- Remove superseded implementation plans and historical decision snapshots after preserving their
  current contracts in ADR, compatibility, reference, release, and provenance documentation.

## [0.1.0b2] - 2026-07-17

### Added

- Add a no-clobber Windows workflow layer with fixed `snapshot/`, `objects/`, `export/`, and
  `receive/` layout plus `workflow init`, `export`, `receive`, `status`, and allowlisted cleanup.
- Add a current Codex Plugin package, personal marketplace manifest, reusable thin Skill, Windows
  Quick Start, and isolated installation/forward-test contracts.
- Materialize statically referenced PDF pages as deterministic PNG review previews in a derived
  overlay while preserving the authoritative LaTeX/PDF bytes; record page, crop, rotation, pixel,
  renderer, source, cache, and derived-tree evidence.
- Add real Microsoft Word COM contract tests for tracked insert/delete/replace/comment round trips,
  Track Changes disabled edits, Accept All, bookmark damage, and unchanged saves.

### Changed

- Enable exactly one `w:trackRevisions` setting in every produced review DOCX and require the
  returned document to match the immutable export under a reject-changes semantic projection before
  revision extraction.
- Map supported bookmark-local edits to exact LaTeX UTF-8 byte spans, including repeated text, CJK,
  emoji, and Unicode extended grapheme boundaries; ambiguous or lossy whitespace remains manual.
- Expand the loopback review UI with filters, progress, complete raw evidence/diagnostics, explicit
  risk acknowledgement, automatic-applicability explanations, and baseline-scope warnings.
- Restrict automatic patch text to ordinary U+0020 spaces with normalization-safe source boundaries;
  tab, NBSP, Unicode separators, structural text, fields, hyperlinks, drawings, and mixed OOXML are
  auditable manual items.
- Describe baseline verification honestly as `verified_for_text_patch`: visible text, structure,
  bookmarks, and insert/delete/move semantics are checked, while formatting, OMML, images, and
  paragraph-mark revisions still require human integrity review.

### Security

- Recheck returned DOCX bytes between semantic comparison and extraction, fail closed on complex
  field-state injection, and bind PDF-overlay evidence into sealed export contracts.
- Isolate native PDFium rendering behind a bounded Windows worker and preserve renderer failures as
  explicit manual diagnostics without running EPS/PostScript.
- Use the maintained `regex` implementation of Unicode `\X` rather than an incomplete local
  grapheme heuristic, and refresh frozen dependency, license, OSV, and PyPI audit evidence.

## [0.1.0b1] - 2026-07-16

### Added

- Complete `doctor` → `snapshot` → `export` → `archive` → `ingest` → `approve` → `plan` →
  `apply` → `verify` → `ledger` → `bundle` CLI and Python workflow.
- Versioned, fail-closed JSON Schemas and sealed domain objects for manifests, source maps,
  revisions, decisions, patch plans, verification reports, and audit bundles.
- Bounded `tex2word==1.0.5` backend plus an explicitly degraded Pandoc comparison backend.
- Canonical read-only OOXML revision extraction for insertions, deletions, replacements, moves,
  formatting changes, and comments with reviewer/time evidence.
- Immutable source snapshots and returned-Word archives, hash-bound per-change approvals, a local
  loopback browser UI, dry-run patch planning, and atomic application to a new work tree.
- XeLaTeX/`latexmk` compilation, `latexdiff` review output, cross-reference/resource checks,
  reviewer-aware ledger generation, and allowlisted deterministic audit ZIPs.
- Fully synthetic Apache-2.0 bilingual LaTeX/DOCX fixture with deterministic clean-room generator,
  structural oracle, privacy checks, and Microsoft Word visual QA evidence.
- Windows-only Python 3.12/3.13 CI definitions, including a fail-closed Windows real
  XeLaTeX/CTeX/`latexdiff` gate configured around a digest-pinned official MiKTeX Setup Utility,
  an explicit verified package set including Fandol fonts, a fixture that does not depend on
  optional Windows Chinese fonts, and a non-publishing reproducible release-candidate workflow.
  Support still requires an observed hosted result for the exact commit.
- Wheel/sdist archive inspection, separate clean-install tests, deterministic double builds,
  SHA-256 sums, a runtime-closure CycloneDX SBOM, an unsigned SLSA v1-compatible custom provenance
  statement, and evidence-manifest verification.
- Apache-2.0 project licensing, Contributor Covenant 2.1 with CC BY 4.0 attribution, security and
  support policies, upstream/license matrices, threat model, API/CLI references, and release guide.

### Security

- Inputs are bounded against path traversal, symlink escape, malformed ZIP/XML, decompression bombs,
  unsafe external commands, partial writes, hash drift, schema confusion, and approval bypass.
- Approval recording cannot write LaTeX; automatic application is limited to uniquely anchored,
  high-confidence plain-text changes and always targets a new directory.
- Repository and distribution gates reject private documents, reviewer metadata, credentials,
  personal paths, runtime output, and unapproved document binaries.

### Fixed

- Emit Word-compatible `w:t` text inside `w:moveFrom` while retaining `w:delText` for true
  deletions, so Microsoft Word opens the synthetic tracked-move fixture without repair.
- Resolve extensionless `\includegraphics` basenames containing dots through `\graphicspath`, while
  preserving ambiguity as a fail-closed discovery error.
- Accept both enum and string severity values emitted by `tex2word 1.0.5` reports without masking
  unsupported report shapes.
- Prefer the final TeX log over transient `latexmk` console passes when deciding whether references
  and compilation succeeded.
- Preserve MiKTeX's three isolated user-root variables in the bounded external-tool environment,
  so a verified runner installation remains discoverable inside real `latexmk`/`latexdiff`
  subprocesses without admitting unrelated environment values.
- Prebuild and verify the XeLaTeX format during the bounded Windows toolchain bootstrap, preventing
  a first-run format build from consuming the 60-second document-compilation timeout.
- Scan non-ignored untracked release-candidate files, LaTeX/source text types, TeX temporary files,
  and `par-*` runtime caches before a first commit can accidentally publish them.
- Reject non-canonical, case-colliding, Windows-unsafe, linked, oversized, or unexpected wheel,
  sdist, and repository members; scan current provider key prefixes; and validate an sdist's static
  Hatchling contract and identity before any PEP 517 subprocess can run.
- Run environment probes in owned temporary directories and terminate complete process trees on
  timeout or output overflow, including a bounded 60-second Biber cold-start probe.
- Gracefully and boundedly drain rejected loopback HTTP requests before closing them, and close
  every approval response explicitly, preventing Windows connection resets and stale handler
  threads without weakening request limits or approval semantics.
- Declare `referencing` as a direct runtime dependency because the contract registry imports it
  directly, instead of relying on `jsonschema` to install it transitively.
- Bound published Python support to 3.12/3.13, constrain `lxml` and every public optional extra to
  tested intervals, pin Hatchling 1.31.0, setuptools 83.0.0, and wheel 0.47.0, build without an
  implicit PEP 517 environment, and verify wheel/sdist installs through the hash-locked runtime
  closure exported from `uv.lock`.

### Known limitations

- v0.1 automatic patching does not apply formulas, labels, references, commands, environments,
  figures, tables, moves, formatting changes, or comments; these remain auditable manual items.
- Pandoc is a baseline/degraded external backend and is not equivalent to the pinned default
  `tex2word` path.
- A source-only runtime dependency such as `pylatexenc` is hash-bound before its local wheel is
  built and re-hashed afterward, but that generated dependency wheel is not yet compared across
  two independent clean-install runs; reproducibility claims apply to this project's wheel/sdist.
- Linux and macOS are outside the maintained platform scope even if the pure Python wheel happens
  to install or some commands happen to run there.
- Hosted CI status is commit-specific and must be checked in GitHub Actions; no tagged GitHub or
  package-index prerelease has been published.
