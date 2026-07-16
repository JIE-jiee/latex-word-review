# Threat model

## Scope and assets

The protected assets are the authoritative LaTeX tree, the returned Word original, reviewer
identity/timestamps/comments, approval intent, source-to-Word mappings, and the integrity of every
derived patch and deliverable. The project treats LaTeX, DOCX/ZIP/XML, JSON contracts, filesystem
paths, external-tool output, and browser requests as untrusted at their respective boundaries.

The primary security property is not confidentiality alone: an attacker or accidental workflow
must not cause an unapproved source change, silently lose review evidence, replace an immutable
input, or make a failed/degraded conversion appear successful.

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
rejects LaTeX structural characters, line breaks, overlaps and source drift. Apply independently
regenerates the plan and diff, then materializes a new tree from inventoried bytes. Verification
again replays every operation and proves that the actual diff contains no missing, extra,
duplicate or unapproved change.

### External processes

The tex2word Python API runs in a bounded child worker. Pandoc, latexmk and latexdiff use fixed list
argv, no shell, fixed working directories, a minimal environment, bounded output and hard timeout.
TeX runs only on private copies with `-no-shell-escape`; known shell-escape constructs are rejected
before execution and checked again in generated latexdiff source. This reduces risk but is not a
general sandbox for a hostile TeX engine. Process documents from untrusted authors inside an OS or
container sandbox with network and host-file access removed.

### Deliverables and privacy

Audit ZIP creation is allowlist-only and deterministic. Offline verification checks member names,
set, type, metadata, compression, sizes, hashes, privacy report, source-object bindings and bundle
stable ID. `public_fixture` bundles fail if the built-in scanner finds common absolute user paths,
email addresses, private-key headers or API-key patterns. This scan is a release gate, not a proof
of anonymity, copyright clearance or absence of all secrets; human review remains required.

## Explicit non-goals and residual risk

- The tool does not make Microsoft Word, LibreOffice, tex2word, Pandoc, TeX or latexdiff trusted.
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
