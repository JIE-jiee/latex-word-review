# Contributing

Thank you for helping make LaTeX Word Review safer and more reusable. The project is a beta
candidate: public contracts and safety invariants take precedence over adding commands quickly.

## Development setup

Use Python 3.12 or 3.13 and a recent `uv` release. From the repository root:

```console
uv sync --frozen --python 3.12
uv run latex-word-review --version
uv run pytest --cov=latex_word_review --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m build --no-isolation
uv run twine check dist/*
```

The lock file is authoritative for development and CI. Official artifacts are built with
`--no-isolation` only after the locked development environment has installed the exact Hatchling
backend. Change dependencies with `uv lock`, then include the resulting `uv.lock` diff in the same
pull request. Do not remove upper bounds from a public extra without adding 3.12/3.13 resolution
and installation evidence.

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
4. Run the quality, test, and package commands above on a clean checkout.
5. Describe security implications, fallback behavior, and any untested platform in the pull
   request template.

By intentionally submitting a contribution for inclusion, you agree that it is licensed under
Apache License 2.0 as described in Section 5 of `LICENSE`, unless you conspicuously designate it
in writing as not a contribution. The project does not currently require a CLA or DCO; maintainers
must document any future policy change before enforcing it.
