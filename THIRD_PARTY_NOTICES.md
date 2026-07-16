# Third-Party Notices

This file is an auditable inventory and does not replace license text shipped by each upstream
project. Before publishing a wheel, sdist, container, bundled executable, or sample archive,
maintainers must regenerate the dependency inventory from `uv.lock` and inspect the exact artifacts.

## Direct runtime dependencies

| Component | Current constraint | Role | Upstream license | Bundled in project artifacts? |
|---|---:|---|---|---|
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | `>=4.26,<5` | JSON Schema Draft 2020-12 validation | MIT | No; installed separately by package manager |
| [lxml](https://github.com/lxml/lxml) | `>=6.1,<7` | Namespace-aware OOXML processing foundation | BSD-3-Clause | No; installed separately by package manager |
| [referencing](https://github.com/python-jsonschema/referencing) | `>=0.37,<1` | In-memory JSON Schema resource registry | MIT | No; installed separately by package manager |
| [regex](https://github.com/mrabarnett/mrab-regex) | `>=2026.7.10,<2027` | Unicode UAX #29 extended-grapheme boundary enforcement | Apache-2.0 AND CNRI-Python | No; installed separately by package manager |
| [rfc8785](https://github.com/trailofbits/rfc8785.py) | `>=0.1.4,<0.2` | RFC 8785/JCS canonical JSON for audit hashes | Apache-2.0 | No; installed separately by package manager |
| [tex2word](https://github.com/yfyang86/tex2word) | `==1.0.5` | Preferred LaTeX-to-DOCX backend | MIT | No; installed separately by package manager |

Optional `tex2word` extras may add PDF rasterization, MathML/image fallback, or CSL dependencies.
The public extras constrain `citeproc-py` to `>=0.10,<0.11`, Matplotlib to `>=3.10,<3.12`,
Kiwisolver to `>=1.4.8,<1.6`, `latex2mathml` to `>=3.77,<4`, Pillow to `>=12,<13`, and
`pypdfium2` to `>=5,<6`. They are not installed by default; CI resolves and installs their lowest
direct bounds on Python 3.12 and 3.13, and each release still requires lock-file/license review.

## Content redistributed in the public fixture

The paper text, review operations, identities, timestamps, bibliography and figure in
`tests/fixtures/e0-minimal-paper/` are original synthetic content licensed under Apache-2.0. The
two DOCX packages are seeded from the `python-docx` 1.2.0 default template and therefore contain
template-derived style, theme, font-table, web-settings and application-property members.

`python-docx` is MIT licensed, Copyright (c) 2013 Steve Canny. The required complete notice is
preserved in [`third_party/python-docx-LICENSE.txt`](third_party/python-docx-LICENSE.txt), and the
exact fixture scope is recorded in the fixture `provenance.json`.

## Governance text

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) adapts Contributor Covenant version 2.1, authored by
Coraline Ada Ehmke with contributors to the Contributor Covenant project. The upstream source is
<https://www.contributor-covenant.org/version/2/1/code_of_conduct.html> and is licensed under
Creative Commons Attribution 4.0 International (`CC-BY-4.0`). This repository changes the template
enforcement-contact wording and Markdown layout; the adaptation notice is kept in the Code of
Conduct. The complete CC BY 4.0 legal code is preserved in
[`third_party/Contributor-Covenant-LICENSE.txt`](third_party/Contributor-Covenant-LICENSE.txt).

The project-wide Apache-2.0 license does not replace the license or attribution for this adapted
third-party governance text.

## External executables

Pandoc, pandoc-crossref, TeX distributions, `latexmk`, `latexdiff`, LibreOffice, and Microsoft Word
are discovered or invoked as separately installed programs. They are not included in this Python
wheel or sdist. Their own licenses and distribution terms continue to apply. In particular, calling
a user-installed GPL program does not authorize copying that program into this Apache-2.0 source
distribution.

## Development and CI tooling

Hatchling (exactly 1.31.0), pip, setuptools, wheel, uv, build, pytest, pytest-cov, Ruff, mypy,
`types-jsonschema`, Twine, and the pinned GitHub Actions are build or development tools rather than
runtime library contents. Their versions are recorded in `uv.lock` or workflow files. Release
review must still preserve any notices required by artifacts that are actually redistributed.

No private paper, real returned reviewer document, proprietary Word template, font, CSL file, or
Pandoc/TeX executable is intentionally vendored. The python-docx-derived public fixture members
described above are the only intentionally redistributed third-party template material.
