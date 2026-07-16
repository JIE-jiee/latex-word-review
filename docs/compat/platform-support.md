# Platform and tool support matrix

This matrix defines the release support target and the repeatable evidence required to claim it. A
GitHub-hosted runner label is deliberately recorded instead of guessing a permanent operating-system
image version; runner image migrations must be reviewed through the CI result and this document.

**Evidence status (2026-07-16):** the canonical public repository is
[`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review). The workflows below are
defined and locally statically audited; the authoritative hosted result is the GitHub Actions state
attached to the exact source commit. A tagged public prerelease must remain blocked until the
required remote lanes are observed passing. The table describes configured gates, not a fabricated
historical CI result.

## Support tiers

| Platform | Python | Target tier | Configured automated gate | Release impact |
|---|---:|---|---|---|
| `ubuntu-latest` | 3.12, 3.13 | Tier 1 | complete pytest suite and branch-coverage gate; real `tex2word` public-fixture path; clean-room fixture regeneration, structural/oracle/privacy QA; wheel and sdist build, inspection, separate clean installs, and installed-package E0 core loops; Python 3.12 additionally has a real XeLaTeX/CTeX/`latexmk`/`latexdiff` E2E gate | required |
| `windows-latest` | 3.12, 3.13 | Tier 1 | same repository-controlled test, fixture, package, clean-install, and installed-package E0 matrix as Ubuntu | required |
| `macos-latest` | 3.12 | Tier 2 | core/integration pytest suite; wheel/sdist build and inspection; clean wheel installation and installed-package E0 core loop | required for the tested subset, but not a claim of full external-tool coverage |
| Any platform | 3.14 or another interpreter | Outside current metadata | no blocking matrix; `Requires-Python` is `>=3.12,<3.14` | not installable/supported until a reviewed metadata and CI change |

Tier 1 covers the deterministic workflow owned by this repository. Tests may substitute bounded fake
`latexmk`/`latexdiff` processes where they are verifying command construction and audit binding. The
dedicated Ubuntu/Python 3.12 `real-tex` lane is the exception: it installs the declared TeX packages
and must complete the public E2E test with exactly one pass and zero skips. This is evidence for that
specific Ubuntu toolchain, not a claim that every matrix runner contains TeX, Microsoft Word, or
LibreOffice.

Tier 2 means that failures in the declared core subset are defects, while native Word behavior,
LibreOffice rendering, and full TeX toolchain combinations require separate evidence before they can
be promoted to Tier 1.

## Backend and external-tool boundaries

| Component | Supported/checked combination | Support statement |
|---|---|---|
| `tex2word` | Python package `1.0.5` | pinned default conversion backend; executed in the project's bounded worker and covered by public-fixture tests |
| Pandoc baseline | Pandoc `3.9.0.2` plus `pandoc-crossref 0.3.24a` | documented compatible pair; external executables are not bundled and must be detected by `doctor` |
| Pandoc without crossref | separately detected Pandoc executable | a baseline/degraded backend only; capability loss must be reported |
| `latexmk` / XeLaTeX | Ubuntu apt packages below in the real gate; otherwise user-installed and runtime-detected | required for real PDF verification; no shell escape. The Ubuntu gate records package/tool versions separately. `doctor` detects tools, while the current VerificationReport records command status/exit/output hashes but does not yet bind executable version/hash |
| `latexdiff` | Ubuntu apt package below in the real gate; otherwise user-installed and runtime-detected | required for marked review output; the Ubuntu gate and runtime verifier both fail when absent rather than silently succeeding |
| Microsoft Word / LibreOffice | not bundled | used only for document review or visual QA; repository CI does not automate proprietary Word UI behavior |

The upstream compatibility evidence and replacement decisions are in
[`upstream-dependency-matrix.md`](upstream-dependency-matrix.md). “Installed successfully” is not a
capability claim: `doctor`, backend capability objects, fixture checks, and verification reports are
the authority.

### Ubuntu real-TeX package set

The local composite action fixes these Ubuntu package **names** and installs them with
`--no-install-recommends`: `latexmk`, `latexdiff`, `texlive-xetex`, `texlive-lang-chinese`, and
`texlive-latex-recommended`. Exact versions are intentionally allowed to receive updates from the
GitHub-hosted Ubuntu repository and are recorded by `dpkg-query` in `build/real-tex/toolchain.json`.
The gate also records the first version line for `xelatex`, `latexmk`, and `latexdiff`, and requires
`kpsewhich ctex.sty` to resolve successfully.

The selected pytest parameter is `installed-latexmk-latexdiff`. JUnit evidence must contain exactly
one test, one pass, zero failures/errors, and zero skips. Missing tools, missing CTeX, malformed
package evidence, a deselected test, or pytest's otherwise-successful skip result all fail the job.
Pandoc remains a baseline/degraded external backend and is intentionally not added to this heavy
toolchain lane.

## What each CI lane is configured to prove

1. `quality` proves the lock is current; production/tests pass lint, formatting, and strict typing;
   release trust-root scripts receive an explicit lint/format check and fixture scripts receive an
   explicit static lint check despite their global tooling exclusion. Focused standard-library
   self-tests cover release path/privacy/archive/fixture-gate failures.
2. `tests` proves the repository-controlled closed loop on all four Tier 1 OS/Python combinations.
3. `public-fixture` rebuilds the synthetic fixture and proves byte determinism, structural/oracle
   correctness, provenance, and privacy on the same four combinations.
4. `real-tex` installs the fixed minimal Ubuntu package set on Python 3.12, verifies tool/package
   identity and CTeX resolution, and rejects anything other than one real E2E pass with zero skips.
5. `package` resolves and installs all public extras from their bounded lowest direct dependencies,
   builds both archives with the locked backend and `--no-isolation`, validates both archives,
   clean-installs both wheel and sdist on all four combinations, then uses each fresh venv Python to complete
   snapshot→export→archive→ingest→approve→plan→apply against the public E0 fixture.
6. `macos-core` records the Tier 2 core/package evidence on macOS 3.12, including the same E0 core
   loop from the installed wheel.
7. `release-artifacts` explicitly reruns the same real-TeX action plus all other release-critical
   checks, requires every fixture release gate, performs a two-build byte comparison, and emits local
   candidate evidence without publishing.

The fixture visual-review gate is intentionally stricter than ordinary CI. Ordinary CI may report
only `visual_review_complete` as deferred; a release candidate cannot be produced until that check is
recorded as passing. Any other deferred or failed fixture gate fails ordinary CI as well.

### Installed-artifact isolation boundary

The package lanes invoke `.github/scripts/clean_install.py` with explicit, distinct, initially
absent children of `build/`: a venv, a helper-owned transient work directory, and (when requested) a
public-demo output directory. Before creating the venv, the helper verifies that the installed
`build`, Hatchling, pip, setuptools, and wheel versions match `uv.lock` and that Hatchling is exactly
1.31.0. It exports the base runtime closure and hashes from the frozen lock, builds every selected
runtime artifact with `pip wheel --require-hashes --no-build-isolation`, and re-hashes the resulting
local wheels. The fresh venv installs only that local hash-bound wheel set and the selected project
wheel with `--no-index --no-deps`, then runs `pip check` and rejects missing or unexpected
distributions.

For an sdist, the helper never asks pip to install the archive. It rejects unsafe tar paths, links,
devices, case collisions, multiple roots, and bounded-size violations, writes regular members into
an owned directory, and applies a hard limit to the complete decompressed stream, including PAX/GNU
metadata and padding. Before `python -m build --wheel --no-isolation` can run, the helper byte-binds
the sdist pyproject to the checkout; permits only the reviewed static Hatchling backend, target, and
version-source shape; and binds filename/root/PKG-INFO/source/reference-wheel identity. The helper
removes `PYTHONHOME` and `PYTHONPATH`, disables the user site, enables Python safe-path mode,
and proves that `latex_word_review` resolves below the fresh venv prefix. It then launches
`scripts/run_public_e0_cli_demo.py --skip-verification` with that venv's Python. All CLI subprocesses
inherit the same interpreter and isolated environment; the repository `src/` tree is therefore not
an import source for this gate. The final machine-readable log receipt records
`import_origin: fresh_venv_prefix`, `runtime_lock: uv.lock-hashes-to-local-wheel-hashes`, and
`public_e0_demo: pass`.

Existing venv/work/demo paths, symlink components, parent traversal, paths outside `build/`, and
overlapping/nested roots are rejected before installation. On failure, the helper removes only the
fresh roots it owns; it never deletes an existing path. On success the venv and demo remain as CI
evidence while the transient work directory is removed. External TeX verification is covered
separately by `real-tex`, so the installed-artifact gate intentionally stops after `apply`.

## Changing support

A platform or version can be promoted only after a blocking CI lane covers tests, public fixture QA,
archive checks, and clean installation. A combination can be demoted when upstream support ends or a
reproducible failure remains unresolved; the change must update this matrix and the changelog. Do not
infer support from a permissive dependency version specifier.
