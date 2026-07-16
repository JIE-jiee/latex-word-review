## Summary

Describe the user-visible outcome and the smallest reusable problem this change solves.

## Verification

- [ ] Tests added or updated
- [ ] `uv run pytest --cov=latex_word_review --cov-report=term-missing`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] Wheel and sdist build and pass `twine check`

## Safety and data handling

- [ ] No private paper, returned Word original, personal path, secret, or identifying metadata is
      included
- [ ] Source LaTeX and returned Word originals remain immutable
- [ ] Approval and application remain separate gates
- [ ] Hash, schema, anchor, capability, and path mismatches fail closed
- [ ] New fixtures are synthetic or redistributable and include provenance

## Compatibility and upstreams

List affected platforms/backends, fallback behavior, ADR or schema changes, and upstream projects
considered. State any untested condition explicitly.
