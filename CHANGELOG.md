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

### Changed

- Add canonical Homepage, Repository, Documentation, Issues, Changelog, clone, support, and private
  vulnerability-reporting URLs for the public source repository.
- Narrow the maintained platform scope to Windows with CPython 3.12/3.13. Linux and macOS are no
  longer CI, release, or support targets; historical upstream and security evidence remains intact.

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
