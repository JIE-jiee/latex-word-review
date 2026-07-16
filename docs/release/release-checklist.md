# Release checklist

## Source and governance

- [ ] Version and changelog agree; release tag is exactly `v<version>`.
- [ ] License, `third_party/` license texts, third-party notices, security policy, support matrix, and
      known limitations are current.
- [ ] Package metadata and public documentation use the canonical
      `https://github.com/JIE-jiee/latex-word-review` repository, documentation, Issues, Changelog,
      and security-reporting URLs.
- [ ] GitHub private vulnerability reporting is enabled and tested; security reports are not routed
      through public Issues.
- [ ] No private paper, returned Word original, reviewer identity, secret, personal path, generated
      run artifact, TeX temporary file, or non-ignored runtime cache is present in the public
      candidate set.
- [ ] Repository public-material scan passes; every allowlisted attack string or document binary is
      synthetic, intentional, and covered by fixture/test provenance.
- [ ] Every fixture is synthetic or redistributable and has machine-readable provenance.

## Required CI

- [ ] Quality gate passes with the frozen lockfile; public extras resolve/install from their lowest
      direct bounds on Python 3.12 and 3.13.
- [ ] Windows 3.12 and 3.13 closed-loop, fixture, and package lanes pass.
- [ ] Windows 3.12 real XeLaTeX/CTeX/`latexmk`/`latexdiff` lane records one pass, zero skips,
      the expected MiKTeX Setup Utility filename and official SHA-256, all 29 manifest-bound MiKTeX
      packages and their digests, an unchanged complete installed-package inventory count/digest,
      resolved CTeX/Fandol resources, a prebuilt XeLaTeX format, and actual tool versions in the v3
      evidence; the release workflow
      rerun also passes.
- [ ] No Linux or macOS result is treated as a release prerequisite or support claim.
- [ ] Public fixture visual review is recorded as `pass`; no deferred release gate remains.

## Candidate artifacts

- [ ] Two separate output builds use the locked Hatchling 1.31.0 backend with `--no-isolation` and
      are byte-for-byte identical with a commit-derived `SOURCE_DATE_EPOCH`.
- [ ] Wheel and sdist pass archive-boundary/privacy checks and `twine check`.
- [ ] Wheel and sdist metadata contain exactly the reviewed Python interval, base requirements,
      public extras, extra requirements, identity, and Apache-2.0 license expression.
- [ ] Archive checks reject non-canonical/Windows-aliased names, symlink/non-regular members,
      undeclared binary/top-level payloads, duplicate critical metadata, and compressed/member/full
      decompression-limit violations.
- [ ] Wheel and sdist each install the `uv.lock`-exported hash-pinned runtime closure from a locally
      re-hashed wheelhouse, install the project artifact with `--no-index --no-deps`, pass `pip check`,
      resolve
      `latex_word_review` below their fresh venv prefix, expose both module and console CLI entry
      points, and complete the public E0 core loop through `apply` with isolated imports.
- [ ] The safely unpacked sdist produces a wheel whose ordered members and member bytes match the
      checked release wheel; ZIP timestamp/container differences alone may be ignored.
- [ ] Before the sdist PEP 517 subprocess starts, its pyproject, static version, PKG-INFO, archive/root
      identity, and reviewed Hatchling-only build contract are bound to the checkout/reference wheel.
- [ ] `SHA256SUMS`, runtime-closure CycloneDX SBOM, unsigned SLSA v1-compatible custom provenance
      statement, and evidence manifest verify locally; release notes do not claim a full
      build/Actions/Windows-runner/MiKTeX-bootstrap/package-repository supply-chain inventory.
- [ ] Candidate source SHA and provenance subject digests match the reviewed commit and files.

## Promotion

- [ ] Candidate is downloaded and independently verified on another Windows machine.
- [ ] Maintainer explicitly authorizes the target GitHub prerelease/package-index operation.
- [ ] Release notes state support tiers, security-relevant changes, schema/CLI compatibility, and known
      limitations.
- [ ] Published bytes are never replaced; fixes use a new version.
