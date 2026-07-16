# Release process

The canonical source repository is
[`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review). It deliberately has no
workflow that publishes to PyPI, creates a GitHub Release, pushes a tag, or writes repository
contents. The release workflow has only `contents: read` permission and uploads a short-lived
Actions artifact after every gate passes. Promotion remains a separate, explicit maintainer action.

## Candidate preconditions

Before creating a candidate:

1. choose the version in `src/latex_word_review/__about__.py` and update the changelog;
2. confirm all Windows 3.12/3.13 CI lanes and the Windows/Python 3.12 real-TeX lane are green for
   the exact source commit;
3. confirm the public fixture provenance, license, privacy, determinism, and visual review all pass;
4. confirm schemas, CLI behavior, known limitations, support matrix, security policy, and third-party
   notices describe the candidate;
5. ensure the source commit contains no private document, reviewer metadata, local path, build output,
   credentials, or unlicensed fixture;
6. enable and test
   [private vulnerability reporting](https://github.com/JIE-jiee/latex-word-review/security/advisories/new)
   without placing a private document or secret in the test report;
7. create a signed or protected tag named exactly `v<package-version>` only after review.

`workflow_dispatch` is useful for an untagged rehearsal. It produces a candidate artifact but does
not turn the selected branch into a release.

## Automated fail-closed gates

`.github/workflows/release-artifacts.yml` performs the following on Windows/Python 3.12:

- binds a `v*` tag exactly to the package version;
- derives `SOURCE_DATE_EPOCH` from the checked-out commit;
- re-runs lock, lint, format, typing, tests, and branch coverage;
- verifies the public extras' bounded lowest-direct dependency sets install on the supported Python
  versions in the package matrix;
- downloads the official MiKTeX Setup Utility `miktexsetup-5.5.0+1763023-x64.zip`, verifies its
  pinned SHA-256, performs a non-interactive basic installation in isolated user roots, proves the
  exact roots and non-shared configuration through `initexmf --report`, explicitly installs/verifies
  `xetex`, `ctex`, `fandol`, `amsmath`, `booktabs`, `graphics`, `hyperref`, `latexmk`, and
  `latexdiff`, records v2 bootstrap-filename/hash, package-digest, CTeX/Fandol resource, and
  tool-version evidence, disables on-the-fly package installation through MiKTeX's dedicated
  `--disable-installer` control and verifies the disabled value, prebuilds and resolves the
  `xelatex` format outside the bounded business command, then requires exactly one real public E2E
  pass with zero skips;
- scans every non-ignored public candidate path, including untracked files, for private/runtime
  material, secret/private-path patterns, and unapproved document binaries; only exact synthetic
  attack tokens and the provenance-controlled public fixture are allowlisted;
- regenerates the public fixture and requires zero tracked-byte differences;
- requires all fixture gates, including independent visual review;
- builds wheel and sdist twice with locked Hatchling 1.31.0 and `--no-isolation`, then requires
  identical filenames and SHA-256 digests;
- inspects archive paths, required metadata/schemas/license, forbidden private/runtime content, and
  secret/private-path patterns, then runs `twine check`;
- exports the exact runtime closure and upstream hashes from `uv.lock`, produces a local wheelhouse
  with the verified locked builder and no PEP 517 isolation, re-hashes those wheels, and installs
  both runtime and project artifacts with `--no-index --no-deps` in separate venvs;
- safely unpacks the sdist and builds its installation wheel with the locked builder and
  `--no-isolation`, rather than allowing pip to create an implicit build environment, then requires
  its ordered member list and member bytes to match the checked release wheel;
- rejects source-tree/import-environment leakage, runs `pip check`, and uses each installed artifact
  to complete the public E0
  snapshot→export→archive→ingest→approve→plan→apply loop;
- creates `SHA256SUMS`, a CycloneDX 1.6 Python runtime-closure SBOM, an unsigned SLSA v1-compatible
  custom provenance statement, and an evidence manifest;
- verifies all evidence against the candidate bytes before uploading a 14-day Actions artifact,
  including the Windows real-TeX JUnit and toolchain records.

No step uses a package index token, GitHub release token, trusted publisher, or write permission.

## Human promotion

After downloading the candidate artifact, a maintainer must verify it from a separate checkout of
the exact source SHA (the verification scripts are intentionally not copied into the sdist):

```text
uv sync --frozen --all-groups --python 3.12
uv run --frozen python .github/scripts/release_checks.py repo --root . --include-untracked
uv run --frozen python .github/scripts/release_checks.py dist --dir dist --twine
uv run --frozen python .github/scripts/release_checks.py evidence --dist-dir dist --evidence-dir release-evidence
uv run --frozen python .github/scripts/clean_install.py --artifact wheel --dist-dir dist --venv build/manual-wheel-venv --public-e0-output build/manual-wheel-e0
uv run --frozen python .github/scripts/clean_install.py --artifact sdist --dist-dir dist --venv build/manual-sdist-venv --public-e0-output build/manual-sdist-e0
```

Compare `SHA256SUMS` with freshly calculated hashes, inspect the SBOM and unsigned provenance, and
inspect both `demo-summary.json` files. Only then may an authorized maintainer create a GitHub
prerelease or upload to a package index through a separately reviewed process. That process is
intentionally not encoded in this repository yet.

The clean-install helper requires distinct, initially absent venv, work, and demo roots below
`build/`. Existing, symlinked, outside-build, traversal-containing, or overlapping roots are
rejected without modification. Sdist traversal, links/devices, case collisions, multiple roots,
oversized members, and decompression growth beyond fixed limits fail closed. The full decompressed
tar stream is bounded, including PAX/GNU metadata and padding. Names must be canonical NFC portable
paths; Windows device names, alternate-data-stream colons, trailing dots/spaces, case-folded or
implicit-directory aliases, and file/directory prefix collisions are rejected. A failed installation
or demo removes only roots newly owned by that invocation; successful venv/demo roots remain for
inspection and the transient work root is removed.

Before PEP 517 starts, the sdist `pyproject.toml` must be byte-identical to the reviewed checkout and
must match the fixed declarative Hatchling contract. `backend-path`, custom build/metadata hooks,
executable version sources, and any filename/root/PKG-INFO/source/reference-wheel identity mismatch
fail before a build subprocess is launched. The installed-artifact demo passes
`--skip-verification` because the separate real-TeX release gate already proves
Windows XeLaTeX/CTeX/`latexmk`/`latexdiff` behavior for that exact commit.

The SBOM is intentionally scoped to the project and its reachable locked Python runtime closure. It
does not include the build backend, GitHub Actions, Windows runner image, MiKTeX Setup Utility,
MiKTeX repository, or MiKTeX packages. The unsigned custom provenance statement is machine-checkable
and uses the SLSA v1 predicate type, but it is not a signed SLSA attestation and must not be
presented as complete supply-chain coverage.

If any byte, tag, source SHA, provenance subject, fixture report, or clean-install result differs,
discard the candidate. Do not rebuild under the same version and replace published bytes.

## Rollback and revocation

Published package files are immutable. For a defect, yank the affected package-index release where
available, mark the GitHub release as affected, publish a security/advisory notice when appropriate,
and issue a new version. Never overwrite an existing artifact or move an existing release tag.
