# Contributing

Thank you for helping make LaTeX Word Review safer and more reusable. The project is a beta
candidate: public contracts and safety invariants take precedence over adding commands quickly.

## Development setup

Use Python 3.12 or 3.13 and the pinned-compatible `uv` release. From the repository root:

```console
uv sync --frozen --group fixture --extra pdf-figures --python 3.12
uv run --frozen latex-word-review --version
```

The lock file is authoritative for development and CI. Official artifacts are built with
`--no-isolation` only after the locked development environment has installed the exact Hatchling
backend. Change dependencies with `uv lock`, then include the resulting `uv.lock` diff in the same
pull request. Do not remove upper bounds from a public extra without adding 3.12/3.13 resolution
and installation evidence.

The repository has one Windows maintenance entry point:

```powershell
.\scripts\check.ps1 -Profile Quick
.\scripts\check.ps1 -Profile Full
```

Use `Quick` while editing. It selects tests from the unstaged, staged, and untracked paths and does
not launch real Microsoft Word, a system browser, or an installer. Use `Full` before opening or
updating a pull request; it runs the offline lock, repository-boundary, lint, format, type, release
tool self-tests, and complete branch-coverage suite. See
[`docs/architecture/code-map.md`](docs/architecture/code-map.md) for module ownership,
change-to-test mapping, and the dependency direction.

Do not treat the release profile as a build shortcut. After following the documented Windows
candidate build process, maintainers can delegate verification of the existing candidate to the
authoritative script:

```powershell
.\scripts\check.ps1 -Profile Release -ReleaseRoot build\windows-release
```

This intentionally calls `scripts/verify-windows-release.ps1`; it does not duplicate candidate
construction, installer testing, licensing review, or release promotion.

## Safety and test material

- Never commit private papers, returned reviewer documents, personal paths, credentials, or
  identifiable reviewer metadata.
- Reproduce document bugs with a minimal synthetic fixture under `tests/fixtures/`. Every public
  fixture must state its provenance, redistribution permission, expected structure, and hashes.
- Treat source LaTeX and returned Word originals as immutable. Tests must operate on copies in a
  temporary directory.
- Approval recording and patch application are separate gates. No contribution may make approval
  mutate source files directly or replace a complete LaTeX project with Word-derived text.
- Fail closed when hashes, anchors, schema versions, or capabilities do not match.

## Before opening a pull request

1. Search the upstream dependency matrix and existing issues before building an overlapping
   converter or parser feature.
2. Keep changes focused and add tests that fail without the change.
3. Update `CHANGELOG.md` for user-visible behavior and the relevant ADR or compatibility document
   for contract decisions.
4. Run `.\scripts\check.ps1 -Profile Full` on a clean checkout. Follow the separate release process
   only when producing a Windows candidate.
5. Describe security implications, fallback behavior, and any untested platform in the pull
   request template.

## AI-assisted contributions

This project was itself developed through maintainer-driven Vibe Coding with OpenAI Codex. AI use
is welcome, but responsibility stays with the contributor. If an AI tool materially influenced the
design or implementation, disclose that in the pull request and describe how the result was
verified.

Do not submit security-critical code that you cannot explain. Do not copy private papers, reviewer
documents, credentials, personal prompt content, or unlicensed training examples into the
repository. Maintainers may ask for a smaller reproducer, additional tests, or a human explanation
before reviewing an AI-assisted change. See
[`docs/development-provenance.md`](docs/development-provenance.md) for the project's own disclosure.

By intentionally submitting a contribution for inclusion, you agree that it is licensed under
Apache License 2.0 as described in Section 5 of `LICENSE`, unless you conspicuously designate it
in writing as not a contribution. The project does not currently require a CLA or DCO; maintainers
must document any future policy change before enforcing it.
