# Security Policy

LaTeX Word Review processes untrusted ZIP/XML-based Word files and may eventually produce local
source patches. Treat path traversal, entity expansion, decompression bombs, unsafe external
process invocation, hash-bypass, approval-bypass, private-document disclosure, and unintended
source mutation as security-sensitive issues.

## Supported versions

There is no stable or tagged prerelease yet. The public `0.1.0b1` source tree is a beta candidate.

| Version | Supported |
|---|---|
| Current `main` development line | Yes, best effort |
| Historical pre-alpha snapshots | No |

This table will be replaced by a release support window before the first tagged public prerelease.

## Reporting a vulnerability

Do not open a public issue or attach a private document. Use this repository's
[private vulnerability reporting form](https://github.com/JIE-jiee/latex-word-review/security/advisories/new),
which is enabled for this repository. If the form is temporarily unavailable, do not fall back to a
public issue; contact the maintainer through an existing trusted private channel and provide a
minimal synthetic reproducer whenever possible.

Please include the affected version or commit, platform, impact, prerequisites, and reproduction
steps. Redact local paths, names, document contents, access tokens, and reviewer metadata.

The maintainers aim to acknowledge a complete private report within three business days and provide
an initial severity assessment within seven business days. Timelines for a fix and coordinated
disclosure depend on impact and release status.

## Scope boundaries

- Vulnerabilities in this project's code, schemas, packaging, and documented default workflows are
  in scope.
- Issues that require deliberately disabling documented safety gates may be closed as hardening
  requests, but will still be assessed.
- Upstream vulnerabilities in `tex2word`, `lxml`, Pandoc, TeX, Microsoft Word, or other tools should
  also be reported to the relevant upstream. We will coordinate or constrain affected versions when
  the project is exposed.
