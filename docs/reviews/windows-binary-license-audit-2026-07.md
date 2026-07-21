# Windows Binary License Audit — July 2026

**Date:** 2026-07-17
**Scope:** Windows x64 frozen application candidate `0.1.0b2`
**Engineering status:** **BLOCKED for public binary publication**

This is an engineering risk record, not legal advice, a legal opinion, or an independent legal
audit. It records what is present in one concrete candidate and establishes a conservative release
control. A qualified professional must be consulted if a legal conclusion is required.

## Decision

Do not upload the current PyInstaller onedir, portable ZIP, or Inno Setup installer to a public
GitHub Release. The lxml 6.1.1 Windows binary statically incorporates native code, including an
enabled iconv implementation identified upstream as LGPL-2.1, but the wheel's shipped license
payload does not contain the native libraries' complete terms. The project has not assembled and
verified a source/relinking route for that exact binary.

This hold is deliberately narrow:

| Item | Status under this audit | Reason |
|---|---|---|
| Git repository source, documentation, and build scripts | May be published | They do not vendor the inspected lxml extension. Normal repository privacy/license checks still apply. |
| Project sdist or Python wheel | Not automatically blocked by this finding | The current project package declares lxml as a separately installed dependency and does not embed its implementation. Normal package-release review still applies. |
| Local onedir, portable ZIP, and installer candidates | May be retained and rebuilt for local QA | Keeping a local test artifact is not the public redistribution addressed by this hold. Do not upload it. |
| Public onedir/ZIP/installer asset, including a GitHub Release | **Blocked** | It would redistribute the unresolved native binary and incomplete accompanying license evidence. |

No tag or public binary Release is approved by this document.

## Candidate examined

All paths below are repository-relative. `build/` is ignored local evidence and is not intended for
source publication.

| Evidence | Observed value |
|---|---|
| Frozen tree | `build/windows-release/dist/latex-word-review/` |
| Public-shaped local artifacts | `latex-word-review-0.1.0b2-windows-x64-portable.zip` (33,022,941 bytes) and `latex-word-review-0.1.0b2-windows-x64-setup.exe` (21,240,004 bytes) |
| Artifact SHA-256 | ZIP `e22cf2fb06f9d530694a45d89bce0abfa85325a658ec704e0e66819ff7b479b5`; installer `d635772cab7d25673081a265554a7e25ca538b262402bdb2762b0f53ba1c48d3` |
| Locked upstream wheel | `lxml-6.1.1-cp312-cp312-win_amd64.whl`, SHA-256 `26e6eda8d38c1fcab1090dd196ee87cbd13788e531937610e2589085de074e77` |
| Collected extension | `_internal/lxml/etree.cp312-win_amd64.pyd`, 4,033,536 bytes, SHA-256 `b5cef7d22ab972de3fee18e3787730ba74ecd12d5896eaa3660ebde418219669` |
| Collected lxml BSD file | `_internal/lxml-6.1.1.dist-info/licenses/LICENSE.txt`, 1,538 bytes, SHA-256 `e7c3ce8d76331b0101cc46790ab43958ea90a364bcf962ed8763d5a818340e69` |
| Collected lxml exceptions file | `_internal/lxml-6.1.1.dist-info/licenses/LICENSES.txt`, 1,543 bytes / 29 lines, SHA-256 `ce53f508d0cb88bdb4621e236ca34881a3fa1805f97dfccf38960128a4e2ead9` |

The artifact hashes above came from the candidate's existing `SHA256SUMS.txt`; the internal hashes
were independently recomputed and agree with its `CONTENTS.sha256` where listed.

## Technical findings

### 1. Native implementations are incorporated in the lxml extension

Loading the exact extension with CPython 3.12 reported:

- lxml `6.1.1`;
- compiled and runtime libxml2 `2.11.9`;
- compiled and runtime libxslt `1.1.45`; and
- `LIBXML_FEATURES` containing both `iconv` and `zlib`.

PE import-table inspection found only these direct DLL imports:

```text
ADVAPI32.dll
KERNEL32.dll
VCRUNTIME140.dll
WS2_32.dll
api-ms-win-crt-convert-l1-1-0.dll
api-ms-win-crt-environment-l1-1-0.dll
api-ms-win-crt-filesystem-l1-1-0.dll
api-ms-win-crt-heap-l1-1-0.dll
api-ms-win-crt-locale-l1-1-0.dll
api-ms-win-crt-math-l1-1-0.dll
api-ms-win-crt-runtime-l1-1-0.dll
api-ms-win-crt-stdio-l1-1-0.dll
api-ms-win-crt-string-l1-1-0.dll
api-ms-win-crt-time-l1-1-0.dll
api-ms-win-crt-utility-l1-1-0.dll
python312.dll
```

There is no imported libxml2, libxslt, iconv, or zlib DLL. Together with the reported versions and
features, this supports the technical inference that these native implementations are statically
incorporated in `etree.cp312-win_amd64.pyd`. This is an inference about binary construction, not a
legal characterization.

### 2. The shipped lxml 6.1.1 license payload is incomplete for those native components

The candidate contains only `LICENSE.txt` and `LICENSES.txt` in the lxml distribution's license
directory. The latter is exactly 29 lines. Searches for `Binary wheels`, `iconv`, `libxml2`,
`libxslt`, `zlib`, and `LGPL` return no match. Its content covers lxml's ElementTree, cssselect,
test-runner, and isoschematron code/resource exceptions.

This matches the [lxml 6.1.1 tag's 29-line `LICENSES.txt`](https://github.com/lxml/lxml/blob/lxml-6.1.1/LICENSES.txt).
By contrast, lxml's [current development-branch `LICENSES.txt`](https://github.com/lxml/lxml/blob/master/LICENSES.txt)
now has a `Binary wheels` section identifying bundled zlib, iconv, libxml2, libxslt, and libexslt,
and identifies iconv as LGPL-2.1. That newer repository file is useful provenance evidence, but it
does not prove which full notices were delivered with the 6.1.1 wheel and does not retroactively
supply the materials required for this candidate.

Copying the newer summary into the bundle would improve notice coverage but would not, by itself,
resolve source availability or relinking requirements.

### 3. LGPL-2.1 compliance evidence has not been completed

The [LGPL-2.1 text](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.en.html) describes conditions
for distributing an executable linked with a covered library, including notices, the library's
complete corresponding source, and a route that permits modification and relinking. The
[GNU license FAQ](https://www.gnu.org/licenses/gpl-faq.html#LGPLStaticVsDynamic) specifically
describes providing application object form for static linking so a user can modify the library and
relink the application.

For this exact candidate, the project has not yet recorded or verified:

- the exact iconv source archive/commit, build configuration, patches, and build/install scripts
  corresponding to the code inside the wheel;
- the complete LGPL-2.1 notice and applicable copyright notices in the candidate;
- the object/source materials and reproducible procedure needed to relink the affected work with a
  modified compatible iconv, or another verified distribution route; or
- a qualified professional conclusion that a different route is sufficient for this artifact.

Accordingly, the engineering compliance evidence is incomplete. This document does not conclude
that a violation has occurred, nor does it decide which legal route is required. It blocks public
binary publication until one defensible route is implemented and verified.

### 4. The first candidate also omitted CPython-incorporated native-library notices

The inspected onedir contains CPython's `libcrypto-3-x64.dll` and `libssl-3-x64.dll`, both
reporting OpenSSL `3.5.5`, plus `libffi-8.dll`. Its `_internal/LICENSE.txt` is the exact file from
the selected CPython runtime, but that file does not contain the OpenSSL or libffi sections.
Therefore the `0.1.0b2` candidate has a second notice-completeness gap independent of lxml.

The source recipe for the next candidate now closes this specific gap conservatively:

- the supported frozen build is fixed to exact 64-bit CPython `3.12.13`;
- the GitHub no-publish candidate requests pinned `uv 0.11.16`, verifies the actual `uv --version`
  identity, installs the exact interpreter below the uv-managed Python root, then verifies
  `3.12.13|64|cpython` before dependency installation or packaging;
- the text evidence records the actual uv identity, managed installation key, and SHA-256 of the
  Python executable. It describes
  [Astral `python-build-standalone`](https://github.com/astral-sh/python-build-standalone) only as
  uv's documented default upstream provisioning policy: the workflow does not retain the downloaded
  archive or its digest, so it does not claim byte-level archive provenance;
- this is not a python.org Windows installer: Python 3.12.13 is a source-only security release, so
  `actions/setup-python` has no corresponding hosted Windows asset;

- the complete official CPython v3.12.13 `Doc/license.rst` is vendored as
  `third_party/cpython-3.12.13-license.rst`, with SHA-256
  `341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5`;
- that document contains the OpenSSL, libffi, expat, libmpdec, and other incorporated-software
  terms and notices; and
- the packaging verifier requires the exact notice hash plus `libcrypto-3-x64.dll`,
  `libssl-3-x64.dll`, and `libffi-8.dll`, then records each DLL hash.

This source change does not retroactively repair the examined `0.1.0b2` bytes. It must be confirmed
against the rebuilt `0.2.0b1` candidate before this sub-finding is marked closed. It also does not
resolve the separate lxml/iconv source and relinking gap, so the public binary hold remains.
The candidate workflow uploads only non-executable text evidence and explicitly records that the
managed-Python download archive digest is unavailable; it does not publish the locally built ZIP,
installer, or onedir.


### 5. Inno Setup commercial-license request is a separate consideration

The [Inno Setup License](https://jrsoftware.org/files/is/license.txt) grants permission to use the
software for any purpose, including commercial applications, subject to its redistribution and
representation conditions. Its current [commercial-license page](https://jrsoftware.org/isorder.php)
also says:

- non-commercial users are not requested to purchase a license;
- all commercial users are requested to purchase one; and
- that purchase is not strictly required, although upstream asks commercial users to support the
  project and expects purchase once installers are production-ready.

The current candidate is a local engineering rehearsal. This audit does not classify its maintainer
or users as commercial or non-commercial, and it does not assert that a commercial license has or
has not been acquired. Before commercial production use, the maintainer or organization should
re-check the then-current upstream license, purchase request, and its own classification. This
record makes no legal or procurement conclusion. The exact Inno Setup 7 point version also was not
captured in the candidate evidence and must be recorded before any future public installer release.

## Exit criteria for the binary hold

The public onedir/portable ZIP/installer hold remains in place until all applicable items below are
closed and recorded against a newly built candidate:

1. Identify the exact native source versions, source archives/commits, patches, configuration, and
   build/install scripts corresponding to the selected lxml Windows wheel.
2. Ship the complete applicable notices and license texts for lxml and its incorporated zlib,
   iconv, libxml2, libxslt, and libexslt code; do not treat the 29-line lxml file as complete.
3. Implement and test a source/object/relinking distribution route that addresses the LGPL-2.1
   conditions for the exact artifact, replace the dependency/build with a verified alternative, or
   obtain a documented qualified-professional conclusion approving another route.
4. Rebuild from a clean environment and repeat PE import, runtime-version/feature, artifact-content,
   notice, source-offer/material, and SHA-256 checks.
5. Verify the pinned CPython v3.12.13 complete license document and the exact hashes of the
   candidate's OpenSSL/libffi DLLs, in addition to the distribution-metadata license trees.
6. Record the exact CPython, PyInstaller, lxml wheel, and Inno Setup versions and hashes.
7. Obtain an explicit maintainer release decision that references this audit and the new evidence.

An upstream notice update alone does not close items 1–3. Until the exit criteria are met, GitHub
may host the source repository and build instructions, but not the affected frozen binary assets.

## Primary references

- [lxml 6.1.1 license exceptions](https://github.com/lxml/lxml/blob/lxml-6.1.1/LICENSES.txt)
- [lxml current binary-wheel inventory](https://github.com/lxml/lxml/blob/master/LICENSES.txt)
- [GNU Lesser General Public License 2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.en.html)
- [GNU FAQ: static and dynamic LGPL linking](https://www.gnu.org/licenses/gpl-faq.html#LGPLStaticVsDynamic)
- [Inno Setup License](https://jrsoftware.org/files/is/license.txt)
- [Inno Setup commercial-license request and Q&A](https://jrsoftware.org/isorder.php)
- [CPython v3.12.13 complete license document](https://github.com/python/cpython/blob/v3.12.13/Doc/license.rst)
- [Python 3.12.13 release (source-only; no Windows installers)](https://www.python.org/downloads/release/python-31213/)
- [uv managed Python documentation](https://docs.astral.sh/uv/concepts/python-versions/)
- [Astral python-build-standalone](https://github.com/astral-sh/python-build-standalone)
- [OpenSSL 3.5.5 license](https://github.com/openssl/openssl/blob/openssl-3.5.5/LICENSE.txt)
- [libffi 3.4.4 license](https://github.com/libffi/libffi/blob/v3.4.4/LICENSE)
