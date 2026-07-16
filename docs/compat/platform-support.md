# Windows platform and tool support matrix

This matrix defines the Windows-only release support target and the repeatable evidence required to
claim it. A GitHub-hosted runner label is recorded instead of guessing a permanent Windows image
version; runner image migrations must be reviewed through the CI result and this document.

**Evidence status (2026-07-16):** the canonical public repository is
[`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review). The Windows workflows
publish the required evidence, but configuration alone is not a passing result. The authoritative
hosted state is the GitHub Actions result attached to the exact source commit. A tagged public
prerelease requires every required Windows lane to pass for the selected source commit.

## Supported platform

| Environment | Python | Support state | Required evidence |
|---|---:|---|---|
| `windows-latest` | 3.12, 3.13 | release target | complete tests and branch coverage; real `tex2word` fixture path; clean-room fixture QA; wheel/sdist inspection, separate clean installs, and installed-package E0 loops |
| Windows real-TeX lane | 3.12 | release target | pinned MiKTeX/tool identity plus exactly one real XeLaTeX/CTeX/`latexmk`/`latexdiff` E2E pass with zero skips |
| Windows | 3.14 or another interpreter | outside current metadata | no blocking lane; `Requires-Python` is `>=3.12,<3.14` |
| Linux or macOS | any | unsupported | no CI, installation, compatibility, triage, or release-blocking commitment |

The pure Python wheel may be technically installable on an unsupported operating system. That fact
does not expand this support matrix. Likewise, security checks that reject POSIX paths, symlinks,
case aliases, or non-portable archive names remain intentional defenses against untrusted input;
they are not Linux or macOS support promises.

## Backend and external-tool boundaries

| Component | Supported/checked combination | Support statement |
|---|---|---|
| `tex2word` | Python package `1.0.5` on supported Windows/Python combinations | pinned default conversion backend; executed in the project's bounded worker and covered by public-fixture tests |
| Pandoc baseline | Pandoc `3.9.0.2` plus `pandoc-crossref 0.3.24a` on Windows | documented compatible pair; external executables are not bundled and must be detected by `doctor` |
| Pandoc without crossref | separately detected Windows Pandoc executable | baseline/degraded backend only; capability loss must be reported |
| MiKTeX / XeLaTeX | official Setup Utility `miktexsetup-5.5.0+1763023-x64.zip`, SHA-256 `0571e90f6d94353089b4f189fd82a532f9fe559a388c7e7f1102b14b3c1ae27d` | required Windows distribution bootstrap for the hosted real-TeX gate; the gate must record actual identity and resolve `ctex.sty` plus `FandolSong-Regular.otf` before compiling |
| `latexmk` / `latexdiff` | MiKTeX `latexmk` and `latexdiff` packages | required for real PDF and marked-review verification; absence or version/identity failure blocks the gate |
| Microsoft Word for Windows | not bundled | primary review and visual-QA application; proprietary UI behavior is verified separately from repository automation |
| LibreOffice for Windows | not bundled, optional | optional visual cross-check only; it is not required for the maintained workflow |

The upstream compatibility evidence and replacement decisions are in
[`upstream-dependency-matrix.md`](upstream-dependency-matrix.md). “Installed successfully” is not a
capability claim: `doctor`, backend capability objects, fixture checks, verification reports, and
the exact commit's CI result are the authority.

## Windows real-TeX gate

The hosted real-TeX gate downloads MiKTeX's official Setup Utility
`miktexsetup-5.5.0+1763023-x64.zip` and requires SHA-256
`0571e90f6d94353089b4f189fd82a532f9fe559a388c7e7f1102b14b3c1ae27d` before extraction. It uses
the utility to download and install the basic package set non-interactively into three isolated
user roots under the runner's temporary directory. Before package operations, `initexmf --report`
must prove those exact install/config/data roots, a non-shared regular setup, and a valid executable
path. The bounded TeX subprocess environment carries only those three MiKTeX root variables from
the tool-specific configuration; it still excludes unrelated environment values. The gate then
explicitly installs and verifies the sorted 29-package E0 closure in
`.github/actions/real-tex-gate/miktex-packages.txt`. This includes the direct tools and the CTeX/
Hyperref packages observed in the fixed public fixture; it does not rely on MiKTeX's interactive
missing-package resolver. The public fixture explicitly selects CTeX's Fandol font set, so it does
not depend on optional Chinese supplemental fonts in the Windows runner image. Every MiKTeX
maintenance command after the explicit package-require step carries the dedicated disable-installer
option, and every isolated `latexmk` invocation forwards that option to its TeX engine, so an engine-level missing
package fails without a prompt. Because helper programs launched by `latexmk` do not necessarily
receive the engine option, the gate also snapshots the complete installed-package inventory before
and after the test and rejects any addition or metadata change. The gate explicitly builds the
`xelatex` format and warms a fixed public Fandol document outside the 60-second business-command
boundary, then requires
`kpsewhich --engine=xetex --format=fmt xelatex.fmt` to resolve it before the test starts.

The evidence uses schema `latex-word-review-real-tex-gate-v3`, records the verified Setup Utility
filename and SHA-256, package-manifest digest, required-package digests, complete installed-package
inventory count/digest, and tool versions; verifies that `kpsewhich` resolves `ctex.sty`,
`FandolSong-Regular.otf`, and the prebuilt `xelatex.fmt`; proves the full inventory is unchanged
across the test; and runs the selected pytest parameter
`installed-latexmk-latexdiff`. A local run against
an existing installation records `preinstalled_local` instead and cannot be substituted for the
hosted bootstrap evidence.

JUnit evidence must contain exactly one test, one pass, zero failures/errors, and zero skips.
Missing tools, unresolved CTeX, malformed toolchain evidence, a deselected test, or pytest's
otherwise-successful skip result all fail the job. The package version pin and package names are a
configuration contract, not evidence that the hosted installation succeeded; only the actual
GitHub Actions result for the source commit can establish that.

Pandoc remains a baseline/degraded external backend and is intentionally not added to this heavy
toolchain lane.

## What each CI lane is configured to prove

1. `quality` proves the lock is current; production/tests pass lint, formatting, and strict typing;
   release trust-root scripts and fixture scripts receive explicit static checks.
2. `tests` proves the repository-controlled closed loop on Windows with Python 3.12 and 3.13.
3. `public-fixture` rebuilds the synthetic fixture and proves byte determinism,
   structural/oracle correctness, provenance, and privacy on those two combinations.
4. `real-tex` binds the configured Windows MiKTeX/toolchain identity on Python 3.12 and rejects
   anything other than one real E2E pass with zero skips.
5. `package` resolves all public extras from their bounded lowest direct dependencies, builds and
   validates both archives, clean-installs wheel and sdist on the two supported Python versions,
   then uses each fresh venv to complete
   snapshot→export→archive→ingest→approve→plan→apply against the public E0 fixture.
6. `release-artifacts` reruns the Windows real-TeX action and the other release-critical checks,
   requires every fixture release gate, performs a two-build byte comparison, and emits candidate
   evidence without publishing.

The fixture visual-review gate is intentionally stricter than ordinary CI. Ordinary CI may report
only `visual_review_complete` as deferred; a release candidate cannot be produced until that check
is recorded as passing. Any other deferred or failed fixture gate fails ordinary CI as well.

### Installed-artifact isolation boundary

The package lanes invoke `.github/scripts/clean_install.py` with explicit, distinct, initially
absent children of `build/`: a venv, a helper-owned transient work directory, and, when requested, a
public-demo output directory. Before creating the venv, the helper verifies that the installed
`build`, Hatchling, pip, setuptools, and wheel versions match `uv.lock` and that Hatchling is exactly
1.31.0. It exports the base runtime closure and hashes from the frozen lock, builds every selected
runtime artifact with `pip wheel --require-hashes --no-build-isolation`, and re-hashes the resulting
local wheels. The fresh Windows venv installs only that local hash-bound wheel set and the selected
project wheel with `--no-index --no-deps`, then runs `pip check` and rejects missing or unexpected
distributions.

For an sdist, the helper never asks pip to install the archive. It rejects unsafe tar paths, links,
devices, case collisions, multiple roots, and bounded-size violations, writes regular members into
an owned directory, and applies a hard limit to the complete decompressed stream, including PAX/GNU
metadata and padding. Before `python -m build --wheel --no-isolation` can run, the helper byte-binds
the sdist pyproject to the checkout; permits only the reviewed static Hatchling backend, target, and
version-source shape; and binds filename/root/PKG-INFO/source/reference-wheel identity. The helper
removes `PYTHONHOME` and `PYTHONPATH`, disables the user site, enables Python safe-path mode, and
proves that `latex_word_review` resolves below the fresh venv prefix. It then launches
`scripts/run_public_e0_cli_demo.py --skip-verification` with that venv's Python. All CLI subprocesses
inherit the same interpreter and isolated environment; the repository `src/` tree is therefore not
an import source for this gate.

Existing venv/work/demo paths, symlink components, parent traversal, paths outside `build/`, and
overlapping/nested roots are rejected before installation. On failure, the helper removes only the
fresh roots it owns; it never deletes an existing path. On success the venv and demo remain as CI
evidence while the transient work root is removed. External TeX verification is covered separately
by `real-tex`, so the installed-artifact gate intentionally stops after `apply`.

## Changing support

A Windows/Python or Windows/external-tool combination can be promoted only after a blocking CI lane
covers its tests, public fixture QA, archive checks, and clean installation as applicable. A
combination can be demoted when upstream support ends or a reproducible failure remains unresolved;
the change must update this matrix and the changelog. Adding Linux or macOS support would require a
new explicit maintainer decision, dedicated blocking CI, release evidence, and documentation; it
must not be inferred from permissive dependency metadata or incidental user success.
