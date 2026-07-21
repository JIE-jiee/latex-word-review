# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir recipe for the supported Windows distribution.

The build script invokes this file from the repository root and uses the
resulting directory as the *only* byte source for both the portable ZIP and
the Inno Setup installer.  Runtime dependencies are collected from the
already-provisioned build environment; this recipe never downloads them.
"""

from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version as distribution_version
from importlib.util import find_spec
from pathlib import Path
import re
import sys

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


PROJECT_ROOT = Path(SPECPATH).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
ENTRY_POINT = SOURCE_ROOT / "latex_word_review" / "__main__.py"
VERSION_SOURCE = SOURCE_ROOT / "latex_word_review" / "__about__.py"
PYTHON_LICENSE = Path(sys.base_prefix) / "LICENSE.txt"
CPYTHON_LICENSE_BUNDLE = PROJECT_ROOT / "third_party" / "cpython-3.12.13-license.rst"
EXPECTED_CPYTHON = (3, 12, 13)
EXPECTED_CPYTHON_LICENSE_SHA256 = (
    "341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5"
)

if not ENTRY_POINT.is_file():
    raise RuntimeError(f"application entry point is missing: {ENTRY_POINT}")
if not VERSION_SOURCE.is_file():
    raise RuntimeError(f"application version source is missing: {VERSION_SOURCE}")
version_match = re.search(
    r'__version__\s*=\s*"([0-9A-Za-z.+-]+)"',
    VERSION_SOURCE.read_text(encoding="utf-8"),
)
if version_match is None:
    raise RuntimeError("application source version could not be read")
source_version = version_match.group(1)
try:
    installed_version = distribution_version("latex-word-review")
except PackageNotFoundError as exc:
    raise RuntimeError("latex-word-review distribution metadata is missing") from exc
if installed_version != source_version:
    raise RuntimeError(
        "latex-word-review distribution metadata does not match the application source"
    )
if not PYTHON_LICENSE.is_file():
    raise RuntimeError(f"Python runtime license is missing: {PYTHON_LICENSE}")
if sys.version_info[:3] != EXPECTED_CPYTHON:
    raise RuntimeError("the supported frozen application requires exact CPython 3.12.13")
if not CPYTHON_LICENSE_BUNDLE.is_file():
    raise RuntimeError("the pinned CPython incorporated-software license bundle is missing")
if sha256(CPYTHON_LICENSE_BUNDLE.read_bytes()).hexdigest() != EXPECTED_CPYTHON_LICENSE_SHA256:
    raise RuntimeError("the pinned CPython incorporated-software license bundle was modified")

required_packages = ("latex_word_review", "tex2word", "PIL", "pypdfium2", "pypdfium2_raw")
missing_packages = tuple(package for package in required_packages if find_spec(package) is None)
if missing_packages:
    raise RuntimeError(
        "the offline Windows build environment is incomplete: " + ", ".join(missing_packages)
    )

datas = [
    (str(PROJECT_ROOT / "LICENSE"), "."),
    (str(PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(PYTHON_LICENSE), "."),
    (str(CPYTHON_LICENSE_BUNDLE), "licenses/cpython-3.12.13"),
]
datas += collect_data_files(
    "latex_word_review",
    includes=("py.typed", "schemas/**/*.json", "assets/**/*"),
)
for distribution in (
    "attrs",
    "jsonschema",
    "jsonschema-specifications",
    "latex-word-review",
    "lxml",
    "Pillow",
    "pylatexenc",
    "pypdfium2",
    "referencing",
    "regex",
    "rfc8785",
    "rpds-py",
    "tex2word",
    "typing-extensions",
):
    datas += copy_metadata(distribution)

binaries = []
hiddenimports = collect_submodules("latex_word_review")
for package in ("tex2word", "PIL", "pypdfium2", "pypdfium2_raw"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# These are external applications, build/test-only Python packages, or an
# unused online HTTP stack.  They must never enter the offline supported
# runtime bundle through an accidental import.
excluded_modules = [
    "certifi",
    "charset_normalizer",
    "citeproc",
    "contourpy",
    "cycler",
    "dateutil",
    "fontTools",
    "idna",
    "kiwisolver",
    "latex2mathml",
    "matplotlib",
    "mypy",
    "numpy",
    "pip",
    "playwright",
    "pypandoc",
    "pyparsing",
    "pytest",
    "requests",
    "ruff",
    "scipy",
    "setuptools",
    "six",
    "urllib3",
    "wheel",
]

analysis = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(SOURCE_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

cli_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="latex-word-review",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
    uac_uiaccess=False,
)

gui_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="LatexWordReview",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
    uac_uiaccess=False,
)

collection = COLLECT(
    gui_exe,
    cli_exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="latex-word-review",
)
