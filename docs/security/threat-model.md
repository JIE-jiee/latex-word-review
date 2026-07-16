# Threat model

## Scope and assets

The protected assets are the authoritative LaTeX tree, the returned Word original, reviewer
identity/timestamps/comments, approval intent, source-to-Word mappings, and the integrity of every
derived patch and deliverable. The project treats LaTeX, DOCX/ZIP/XML, JSON contracts, filesystem
paths, external-tool output, and browser requests as untrusted at their respective boundaries.

The primary security property is not confidentiality alone: an attacker or accidental workflow
must not cause an unapproved source change, silently lose review evidence, replace an immutable
input, or make a failed/degraded conversion appear successful.

The supported operational boundary is Windows with CPython 3.12/3.13. Behavior observed on Linux
or macOS is not release evidence and does not extend the threat-model support claim.

## Trust boundaries

```text
authoritative source (read-only)
  -> bounded discovery and immutable snapshot
  -> isolated conversion worker / external backend
  -> untrusted DOCX package and reviewer metadata
  -> canonical ChangeSet
  -> human ApprovalSet (no source access)
  -> independently recomputed PatchPlan
  -> new applied tree only
  -> private TeX/latexdiff work copies
  -> explicit allowlist audit bundle
```

Sealed JSON is authorization evidence only after Schema validation, RFC 8785 canonicalization,
payload/document hash verification, stable-ID verification, and upstream hash binding. A valid
JSON file is not automatically trusted merely because it has the expected filename.

## Adversaries and failures considered

- malformed, oversized, duplicate-member, encrypted, symlink-bearing, entity-bearing, or
  relationship-abusing DOCX/ZIP/XML input;
- malformed or oversized PDF/raster input, decompression/pixel bombs, unsafe image paths/options,
  renderer failure, stale cache entries, and image loss between LaTeX and DOCX;
- path traversal, absolute paths, UNC/drive paths, symlink/junction escape, special files and
  output aliasing;
- source, returned Word, approval, plan or diff substitution between workflow phases;
- stale/concurrent approval submissions, CSRF, Host-header rebinding and reviewer text used as XSS;
- command/option injection, shell escape, inherited secrets, runaway converters or TeX tools;
- an accepted high-risk Word edit being mistaken for permission to edit LaTeX structure;
- partial writes, retries, destination collisions and audit ZIP metadata/member tampering;
- private paths, email addresses, credentials or private-key material entering a public bundle;
- ordinary process crashes, tool absence, unsupported versions and converter degradation.

## Controls

### Filesystem and publication

Portable relative paths are validated before joining. Input/output roots must be disjoint;
symlinks, junctions and non-regular files are rejected at security-sensitive boundaries. Source
and returned-original bytes are hashed before and after long operations. Files use exclusive or
hard-link no-clobber publication where available; directory outputs are fully staged beside the
target, checked again, and renamed only to a target that the calling workflow has already proved
unused. Existing destinations are never treated as permission to overwrite.

The library assumes the workspace parent directory is controlled by the invoking user. It does
not claim transactional isolation against another process with the same account deliberately
racing directory entries. Run sensitive work in a directory not writable by untrusted local
processes.

### Untrusted documents and contracts

DOCX readers enforce member count, uncompressed size, compression method/ratio, CRC, relationship,
XML and DTD/entity limits and never follow external relationships. Revisions retain raw canonical
evidence before normalization. Unknown, incomplete, moved, formatted or ambiguously mapped content
remains visible as manual/conflict/ledger-only evidence.

PDF images are never rendered in the authoritative snapshot. A bounded worker materializes static
PDF requests as canonical RGB PNGs in a disjoint, content-addressed overlay; copied TeX commands are
rewritten only in that derived tree. Requests bind the original image digest, page/crop/rotation,
quality profile, renderer identity, cache key, PNG bytes, and decoded pixels. Existing cache entries
are revalidated and never silently overwritten. SVG/EPS/PostScript and any path or option that
cannot be interpreted statically remain manual and block the backend. The overlay manifest is
canonical JSON whose digest and original/derived tree bindings enter the sealed `SourceMap` and
`ExportReport`; the manifest alone is not a signed or sealed authorization object.
It can contain source-relative paths and original `\includegraphics` commands, so its ArtifactRef
inherits the run's confidentiality class and it is not a public diagnostic by default.

DOCX inspection requires the output image-instance count to be at least the source
`\includegraphics` occurrence count. This detects a lower-bound class of silent loss; it does not
prove one-to-one relationship identity, rendering fidelity, or pixel equality inside Word.

Text revisions are eligible for automatic mapping only when the revision wrapper contains plain
text runs with a narrow OOXML whitelist. Field instructions/results, hyperlinks, drawings/text
boxes, nested structures, and other mixed content retain their raw evidence but are classified as
structured and forced to manual review. Exact byte offsets alone never promote structured evidence
to a patch candidate.

The reject-view baseline comparison is deliberately scoped. It verifies visible text,
paragraph/table and field structure, bookmarks, and insert/delete/move revision semantics needed by
the plain-text patch gate. Paragraph-mark revisions, formatting, OMML, images, hyperlink and
relationship targets, content controls/custom XML, and embedded/alternate-content objects are
explicitly listed as unverified and require manual integrity review; `verified_for_text_patch` must not be
interpreted as whole-document visual or semantic equivalence.

Every public domain object has a versioned JSON Schema with unknown security fields denied.
Approval and plan state transitions revalidate the complete hash chain; old revisions remain
immutable. Stable error and process exit codes allow automation to fail closed without parsing
human text.

### Approval and patch application

The loopback review server accepts only literal `127.0.0.1`, exact Host and Origin, a high-entropy
session cookie, a separate CSRF token, strict Fetch Metadata, bounded exact form fields, and stale
revision/hash checks. It has no endpoint or capability for reading or writing LaTeX.

Acceptance records reviewer intent but does not elevate a change's safety class. v0.1 plans only
exact, unique, high-confidence UTF-8 plain-text insertion/deletion/replacement operations and
rejects LaTeX structural characters, line breaks, all whitespace except ordinary U+0020 spaces,
normalization-changing space boundaries, overlaps and source drift. Extended grapheme clusters are
segmented with Unicode `\X`; a patch boundary that splits a cluster is forced to manual review.
Apply independently
regenerates the plan and diff, then materializes a new tree from inventoried bytes. Verification
again replays every operation and proves that the actual diff contains no missing, extra,
duplicate or unapproved change.

### External processes

The tex2word Python API and PDF renderer run in bounded child workers. Pandoc, latexmk and latexdiff
use fixed list argv, no shell, fixed working directories, a minimal allowlisted environment,
bounded output and hard timeout. On Windows the TeX verifier adds only MiKTeX's three documented
isolated-root variables (`MIKTEX_USERINSTALL`, `MIKTEX_USERCONFIG`, and `MIKTEX_USERDATA`) to that
minimal environment; unrelated environment values remain excluded, and other backends do not
inherit the MiKTeX-specific values. When all three isolated roots are present, `latexmk` also
receives MiKTeX's fixed `-disable-installer` option and forwards it to the TeX engine, so an
engine-level missing package fails instead of opening an unattended installation prompt. The hosted public gate also
compares MiKTeX's complete installed-package inventory before and after the test, so an unexpected
package installed by a helper process fails the gate. This comparison is detection, not a network
sandbox; the fixed public fixture and explicit package closure remain part of the trust boundary.
TeX runs only on private copies with `-no-shell-escape`; known shell-escape constructs are rejected
before execution and checked again in generated latexdiff source. This reduces risk but is not a
general sandbox for a hostile TeX engine. Process documents from untrusted authors inside an OS or
container sandbox with network and host-file access removed.

### Deliverables and privacy

Audit ZIP creation is allowlist-only and deterministic. A normal entry must resolve to an immutable
`ArtifactRef` already sealed inside its source contract payload; extensions are never artifact
authority. Contract-document entries must be exact
canonical sealed bytes. The exact RunManifest, VerificationReport and every referenced source
contract are themselves explicit allowlist members, and RunManifest must authorize those source
objects. Offline verification reloads that chain and checks selector, path, role, size, hash and
artifact ID in addition to member names, set, type, metadata, compression, privacy report and bundle
stable ID. A valid same-run object cannot sponsor unrelated bytes. `public_fixture` bundles fail if the built-in scanner finds common absolute user paths,
email addresses, private-key headers or API-key patterns. This scan is a release gate, not a proof
of anonymity, copyright clearance or absence of all secrets; human review remains required.

## Explicit non-goals and residual risk

- The tool does not make Microsoft Word, LibreOffice, tex2word, Pandoc, TeX or latexdiff trusted.
- The tool does not make pypdfium2/PDFium or Pillow trusted, and image-instance counts are not visual
  equivalence proofs.
- Loopback binding does not isolate a malicious process running as the same operating-system user.
- Hashes prove byte identity and binding, not the truth or quality of reviewer decisions.
- The v0.1 source scanner is intentionally conservative and is not a complete TeX parser.
- Automatic application of formulas, macros, references, labels, environments, tables, moves,
  formatting changes and comments is out of scope.
- Public-bundle privacy scanning is intentionally narrow and cannot replace legal/privacy review.
- Remote CI, package registries and GitHub releases have their own supply-chain trust model;
  release workflows use least privilege and require explicit maintainer environments/approval.

Security reports should follow [SECURITY.md](../../SECURITY.md). Do not attach a private paper,
returned reviewer document, credential or non-redacted audit bundle to a public issue.
