# Third-Party Notices

This document records the third-party material used by the repository and by the supported
Windows distributions. It is based on `pyproject.toml`, `uv.lock`, the PyInstaller recipe, and
the license metadata in the currently resolved Windows wheels. It is an engineering inventory,
not legal advice, a legal opinion, or an independent third-party audit. The complete license text
shipped by each upstream project remains authoritative.

The versions below reflect the current lock file. A release maintainer must inspect the exact
candidate again: membership in `uv.lock` does not prove that a component is bundled, and absence
from this document does not prove that PyInstaller did not discover it from the build environment.

**Windows binary release status (2026-07-17): blocked.** The inspected lxml 6.1.1 Windows wheel
statically incorporates native code while its shipped license payload does not contain the native
libraries' complete terms. Do not publish the current onedir, portable ZIP, or installer in a
public GitHub Release until the exit criteria in the
[Windows binary license audit](docs/reviews/windows-binary-license-audit-2026-07.md) are met. This
engineering hold does not prohibit publishing the repository source/build scripts or retaining a
local candidate for testing.

## Distribution boundaries

| Artifact | Third-party implementation code included? | License material and boundary |
|---|---|---|
| Git checkout / GitHub source archive | The synthetic fixture contains `python-docx` template-derived OOXML members. Runtime dependencies are not vendored. | Repository notices and `third_party/` license texts are present. |
| Hatch sdist | No runtime dependency implementation. The sdist contains project source, documentation, Skills, governance material, and `third_party/` notices. The current Hatch include list excludes `tests/`, so the public fixture itself is not in the sdist. | `THIRD_PARTY_NOTICES.md`, the project `LICENSE`, and `third_party/` are included. |
| Python wheel | No dependency implementation; dependencies are resolved separately by the installer/package manager. The wheel contains the `latex_word_review` package and distribution metadata only. | The project license is carried as wheel metadata. Repository documentation and fixtures are not wheel payload. |
| PyInstaller onedir | Yes. It contains CPython, the application, collected direct and transitive Python packages, native extension modules, and the PDF rendering stack described below. | The recipe places the project `LICENSE`, this notice, the exact interpreter's `LICENSE.txt`, the pinned official CPython 3.12.13 complete license document, and copied distribution metadata under `_internal/`. |
| Portable ZIP | Yes. It is the verified PyInstaller onedir tree, prefixed by one directory and accompanied inside that tree by `CONTENTS.sha256`. | Same application bytes and notices as the onedir. It does not contain Inno Setup. |
| Inno Setup installer | Yes. It embeds the same verified onedir payload plus the Inno Setup setup/uninstall engine. | It shows the project license and this notice during setup. The Inno Setup compiler is a build tool and is not installed with the application. |

The project itself is Apache-2.0 licensed and is not third-party material.

## Windows frozen runtime inventory

### Interpreter and packaging components

| Component | Version in the recipe/current evidence | What is redistributed | License |
|---|---:|---|---|
| [CPython](https://www.python.org/) | The supported frozen recipe requires exact 64-bit CPython `3.12.13`; normal source installs continue to support Python 3.12/3.13 | Python runtime, standard library, extension modules, and candidate-dependent incorporated libraries | [Python Software Foundation License Version 2 and incorporated-software acknowledgements](https://docs.python.org/3.12/license.html). The build interpreter's `LICENSE.txt` is copied to `_internal/LICENSE.txt`; the official CPython v3.12.13 `Doc/license.rst`, pinned by SHA-256, is separately copied to `_internal/licenses/cpython-3.12.13/`. |
| [OpenSSL](https://github.com/openssl/openssl) | Candidate-dependent OpenSSL 3.x supplied by the pinned CPython runtime; the first local candidate reported `3.5.5` | `libcrypto-3-x64.dll`, `libssl-3-x64.dll`, and the Python `_hashlib`/`_ssl` consumers | [Apache-2.0](https://github.com/openssl/openssl/blob/openssl-3.5.5/LICENSE.txt). The complete Apache-2.0 text and OpenSSL attribution are included in the pinned CPython v3.12.13 license document; the verifier requires both DLLs and records their exact hashes. |
| [libffi](https://github.com/libffi/libffi) | ABI 8 DLL supplied by the pinned CPython runtime | `libffi-8.dll` used by Python `_ctypes` | [MIT-style libffi license](https://github.com/libffi/libffi/blob/v3.4.4/LICENSE). The complete text and copyright notice are included in the pinned CPython v3.12.13 license document; the verifier requires the DLL and records its exact hash. |
| [PyInstaller](https://pyinstaller.org/) | `6.21.0` is required by the Windows build script | Bootloader and PyInstaller runtime pieces in the two application executables; the PyInstaller compiler/package is not installed as an application package | [GPL 2.0 with the PyInstaller exception, with Apache-2.0 applying to certain files](https://pyinstaller.org/en/stable/license.html). The official exception permits generated bundles to use the application's license and says a PyInstaller notice is not required; it is disclosed here voluntarily. Dependency licenses still apply. |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | Major version 7 is required; the exact candidate must record the compiler version | Setup/uninstall engine in the installer executable only; not in the onedir or portable ZIP, and not installed as a compiler | [Inno Setup License](https://jrsoftware.org/files/is/license.txt). Copyright 1997-2026 Jordan Russell; portions Copyright 2000-2026 Martijn Laan. The generated binary must retain Inno Setup's embedded notices and addresses. Upstream does not request purchases from non-commercial users and requests, but says it does not strictly require, purchases from [commercial users](https://jrsoftware.org/isorder.php). |

The `release` dependency group also locks PyInstaller's build-time Python inputs: `altgraph`
0.17.5, `packaging` 26.2, `pefile` 2024.8.26, `pyinstaller-hooks-contrib` 2026.6,
`pywin32-ctypes` 0.2.3, and `setuptools` 83.0.0. They support analysis/build and are not target
application distributions. The community-hooks project licenses standard build hooks under
GPL-2.0-or-later and its executable-bundled runtime hooks under Apache-2.0; see its
[official license](https://github.com/pyinstaller/pyinstaller-hooks-contrib/blob/master/LICENSE).
The candidate audit must record any contributed runtime hooks actually embedded in the executables.

### Python distributions collected by the supported recipe

`Direct` means declared in the project's base runtime dependencies. `Transitive` means required by
another bundled Python distribution. `Frozen PDF support` means intentionally selected by the
Windows recipe even though it is exposed as the `pdf-figures` extra to normal Python installs.

| Distribution | Locked version | Inclusion path | Upstream license and shipped evidence |
|---|---:|---|---|
| [attrs](https://github.com/python-attrs/attrs) | `26.1.0` | Transitive through `jsonschema` / `referencing` | [MIT](https://github.com/python-attrs/attrs/blob/main/LICENSE); copied `attrs-*.dist-info/licenses/LICENSE` |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | `4.26.0` | Direct | [MIT](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING); copied `jsonschema-*.dist-info/licenses/COPYING` |
| [jsonschema-specifications](https://github.com/python-jsonschema/jsonschema-specifications) | `2025.9.1` | Transitive through `jsonschema` | [MIT](https://github.com/python-jsonschema/jsonschema-specifications/blob/main/COPYING); copied `jsonschema_specifications-*.dist-info/licenses/COPYING` |
| [lxml](https://github.com/lxml/lxml) | `6.1.1` | Direct | [BSD-3-Clause](https://github.com/lxml/lxml/blob/lxml-6.1.1/LICENSE.txt). The copied 6.1.1 [`LICENSES.txt`](https://github.com/lxml/lxml/blob/lxml-6.1.1/LICENSES.txt) covers lxml code/resource exceptions only; it does **not** contain the bundled native libraries' terms. See the [candidate audit](docs/reviews/windows-binary-license-audit-2026-07.md). |
| [Pillow](https://github.com/python-pillow/Pillow) | `12.3.0` | Frozen PDF support | [MIT-CMU plus component-specific licenses](https://github.com/python-pillow/Pillow/blob/main/LICENSE); the wheel's complete `pillow-*.dist-info/licenses/LICENSE` is copied |
| [pylatexenc](https://github.com/phfaist/pylatexenc) | `2.10` | Transitive through `tex2word` | [MIT](https://github.com/phfaist/pylatexenc/blob/main/LICENSE.txt); copied `pylatexenc-*.dist-info/licenses/LICENSE.txt` |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | `5.12.1` | Frozen PDF support | [Apache-2.0 / BSD-3-Clause for pypdfium2, with separate data/build dependency licenses](https://github.com/pypdfium2-team/pypdfium2#licensing); the full `pypdfium2-*.dist-info/licenses/` tree is copied |
| [referencing](https://github.com/python-jsonschema/referencing) | `0.37.0` | Direct and transitive | [MIT](https://github.com/python-jsonschema/referencing/blob/main/COPYING); copied `referencing-*.dist-info/licenses/COPYING` |
| [regex](https://github.com/mrabarnett/mrab-regex) | `2026.7.10` | Direct | [Apache-2.0 AND CNRI-Python](https://github.com/mrabarnett/mrab-regex/blob/hg/LICENSE.txt); copied `regex-*.dist-info/licenses/LICENSE.txt` |
| [rfc8785](https://github.com/trailofbits/rfc8785.py) | `0.1.4` | Direct | [Apache-2.0](https://github.com/trailofbits/rfc8785.py/blob/main/LICENSE). Its current wheel declares the license but does not carry a `License-File`; this notice links it and the bundled project `LICENSE` contains the same Apache-2.0 terms. |
| [rpds-py](https://github.com/crate-py/rpds) | `2026.6.3` | Transitive through `jsonschema` / `referencing` | [MIT](https://github.com/crate-py/rpds/blob/main/LICENSE); copied `rpds_py-*.dist-info/licenses/LICENSE` |
| [tex2word](https://github.com/yfyang86/tex2word) | `1.0.6` | Direct preferred conversion backend | [MIT](https://github.com/yfyang86/tex2word/blob/main/LICENSE); copied `tex2word-*.dist-info/licenses/LICENSE` |
| [typing-extensions](https://github.com/python/typing_extensions) | `4.16.0` | Transitive from `referencing` on Python `<3.13`; metadata is explicitly copied by the current frozen recipe | [PSF-2.0](https://github.com/python/typing_extensions/blob/main/LICENSE); copied `typing_extensions-*.dist-info/licenses/LICENSE` |

The `latex-word-review-*.dist-info` directory is also copied for runtime metadata, but it describes
this project rather than a third party.

### Native libraries carried inside Windows wheels

Package-level SPDX labels are not a complete inventory for binary wheels. The supported recipe
preserves upstream wheel license files, but the content of those files is candidate-specific and
must not be assumed complete:

- The inspected lxml 6.1.1 `cp312-win_amd64` extension reports libxml2 2.11.9, libxslt 1.1.45,
  and enabled `iconv`/`zlib` features, while its PE imports contain no separate libxml2, libxslt,
  iconv, or zlib DLL. This supports the engineering inference that the native implementations are
  statically incorporated. However, the wheel's 29-line `LICENSES.txt` contains only lxml
  code/resource exceptions. It does not include the native-library section now found on lxml's
  current development branch, nor the full zlib, iconv, libxml2, libxslt, or libexslt terms.
  In particular, lxml's current upstream inventory identifies iconv as LGPL-2.1. The candidate does
  not yet carry evidence resolving the corresponding source and relinking conditions. The detailed
  [Windows binary license audit](docs/reviews/windows-binary-license-audit-2026-07.md) therefore
  blocks public frozen-binary redistribution; merely copying the current development-branch notice
  would not by itself close that gap.
- The inspected Pillow 12.3.0 Windows wheel's single license inventory covers Pillow and its
  bundled `brotli`, `freetype`, `harfbuzz`, `lcms2`, `libavif`, `libjpeg-turbo`, `libpng`,
  `libwebp`, `openjpeg`, `tiff`, `xz`, and `zlib-ng` components. Their terms are not all
  MIT-CMU. The complete, unabridged wheel file is copied as
  `pillow-*.dist-info/licenses/LICENSE` and controls over this summary.
- The pypdfium2 5.12.1 Windows wheel supplies the `pypdfium2_raw` module and `pdfium.dll`; there
  is no separate `pypdfium2_raw` Python distribution. The inspected wheel identifies PDFium
  `152.0.7947.0`, sourced from `pdfium-binaries`. PDFium's current
  [official license](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/LICENSE) and the
  wheel's build notices must be preserved. The copied `BUILD_LICENSES` set includes notices for
  `abseil`, `agg23`, `fast_float`, `freetype`, `icu`, `lcms`, `libjpeg-turbo`, `libopenjpeg`,
  `libpng`, `libtiff`, `llvm-libc`, `pdfium-binaries`, PDFium, `simdutf`, and `zlib`.
- The inspected CPython 3.12.13 runtime's `_internal/LICENSE.txt` contains the interpreter's PSF
  license history but does not contain all incorporated-software notices. The recipe therefore
  also carries the official CPython v3.12.13
  [`Doc/license.rst`](https://github.com/python/cpython/blob/v3.12.13/Doc/license.rst), pinned to
  SHA-256 `341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5`.
  That document includes the OpenSSL, libffi, expat, libmpdec, and other CPython-incorporated
  notices. The release verifier requires this exact file plus `libcrypto-3-x64.dll`,
  `libssl-3-x64.dll`, and `libffi-8.dll`, and records their hashes. Neither a short `PSF-2.0`
  label nor `_internal/LICENSE.txt` alone is accepted.

## Material in the public repository

The paper text, review operations, identities, timestamps, bibliography, and figure under
`tests/fixtures/e0-minimal-paper/` are original synthetic content licensed under Apache-2.0. The
two DOCX packages are seeded from the `python-docx` 1.2.0 default template and therefore contain
template-derived style, theme, font-table, web-settings, and application-property members.

`python-docx` is MIT licensed, Copyright (c) 2013 Steve Canny. The complete notice is preserved in
[`third_party/python-docx-LICENSE.txt`](third_party/python-docx-LICENSE.txt), and the exact fixture
scope is recorded in the fixture `provenance.json`. `python-docx` is a fixture/development
dependency, not a target frozen-runtime dependency.

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) adapts Contributor Covenant version 2.1, authored by
Coraline Ada Ehmke with contributors to the Contributor Covenant project. The
[upstream text](https://www.contributor-covenant.org/version/2/1/code_of_conduct.html) is licensed
under Creative Commons Attribution 4.0 International (`CC-BY-4.0`). The repository changes the
template enforcement-contact wording and Markdown layout. The complete legal code is preserved in
[`third_party/Contributor-Covenant-LICENSE.txt`](third_party/Contributor-Covenant-LICENSE.txt).

The project-wide Apache-2.0 license does not replace the licenses or attributions above.

## Explicitly not bundled

The following are discovered, invoked, or controlled as separately installed software and are not
included in the supported PyInstaller onedir, portable ZIP, or installer payload:

- Microsoft Word and Microsoft Office;
- MiKTeX, TeX Live, `latexmk`, `latexdiff`, TeX engines, TeX packages, and fonts;
- Pandoc, pandoc-crossref, and LibreOffice;
- Playwright and its browser downloads; Playwright is test-only and explicitly excluded by the
  frozen recipe;
- a Chromium/Electron runtime: the application uses the user's default Windows browser;
- private papers, returned reviewer documents, proprietary Word templates, user CSL files, or
  other user data.

Calling a separately installed GPL or proprietary program does not authorize copying it into this
Apache-2.0 distribution. Users remain responsible for the licenses and terms of their separately
installed applications.

The lock file also records optional citation and math-fallback dependencies, fixture dependencies,
and development tools. Those entries do not by themselves make those packages part of the frozen
application. `citeproc-py`, Matplotlib, Kiwisolver, `latex2mathml`, and NumPy are not part of the
declared frozen target inventory. Because PyInstaller can follow imports that happen to be
available in its build environment, the final candidate must prove their absence or add their
exact licenses and notices before release.

## Development and CI tooling

Hatchling, pip, setuptools, wheel, uv, build, pytest, pytest-cov, Ruff, mypy,
`types-jsonschema`, Twine, and pinned GitHub Actions are build/development tools rather than
installed application packages. PyInstaller and Inno Setup are treated separately above because
their generated bootloader/setup components do enter the Windows executables.

## Release-time license gate

Before publishing any Windows portable ZIP or installer, maintainers must:

1. Build from a clean, intentionally provisioned Windows environment and record the exact CPython,
   PyInstaller, Inno Setup, wheel filenames, and hashes.
2. Inspect `CONTENTS.sha256`, the PyInstaller cross-reference/import graph, collected distribution
   metadata, and native PE imports. `uv.lock` alone is not an artifact manifest.
3. Compare the actual candidate against this inventory. Remove accidental optional dependencies or
   add their exact notices before release; do not silently widen the bundle.
4. Verify that `_internal/LICENSE.txt`, the hash-pinned
   `_internal/licenses/cpython-3.12.13/cpython-3.12.13-license.rst`, this notice, every copied
   `*.dist-info/licenses/` tree, Pillow `LICENSE`, and the complete pypdfium2 `BUILD_LICENSES`
   tree are present and readable in both the onedir and portable ZIP. Require and hash-inventory
   CPython's `libcrypto-3-x64.dll`, `libssl-3-x64.dll`, and `libffi-8.dll`. Presence is not proof
   of completeness: lxml 6.1.1's copied `LICENSES.txt` is known not to cover its incorporated
   native libraries.
5. Keep every public frozen-binary channel blocked until the
   [Windows binary license audit](docs/reviews/windows-binary-license-audit-2026-07.md) exit criteria
   are met: the exact source/notice/relinking route is implemented and verified, or a qualified
   professional reaches and documents another conclusion. This inventory does not reach that legal
   conclusion.
6. Review the exact lxml, Pillow, CPython, and PDFium wheels for native-library obligations.
7. Confirm that the installer's application payload is byte-for-byte the verified onedir tree and
   that the only additional executable framework is the recorded Inno Setup engine.
8. Re-run the repository privacy scan and confirm that no private paper, returned Word document,
   credential, personal path, or proprietary template entered any public artifact.

No release should describe this inventory as an independent legal or compliance audit.
