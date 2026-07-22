#!/usr/bin/env python3
"""Fail-closed CI and release checks that require only the Python standard library."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

MAX_SCAN_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_STREAM_BYTES = 128 * 1024 * 1024
# Hatchling/packaging canonicalize the pyproject spelling ``>=3.12,<3.14`` in metadata.
EXPECTED_REQUIRES_PYTHON = "<3.14,>=3.12"
EXPECTED_PROVIDES_EXTRA = frozenset({"citations", "math-fallback", "pdf-figures"})
EXPECTED_REQUIRES_DIST = frozenset(
    {
        "jsonschema<5,>=4.26",
        "lxml<7,>=6.1",
        "referencing<1,>=0.37",
        "regex<2027,>=2026.7.10",
        "rfc8785<0.2,>=0.1.4",
        "tex2word==1.0.5",
        "citeproc-py<0.11,>=0.10; extra == 'citations'",
        "tex2word[csl]==1.0.5; extra == 'citations'",
        "kiwisolver<1.6,>=1.4.8; extra == 'math-fallback'",
        "latex2mathml<4,>=3.77; extra == 'math-fallback'",
        "matplotlib<3.12,>=3.10; extra == 'math-fallback'",
        "tex2word[mathimg,mathml]==1.0.5; extra == 'math-fallback'",
        "pillow<13,>=12; extra == 'pdf-figures'",
        "pypdfium2<6,>=5; extra == 'pdf-figures'",
        "tex2word[pdf]==1.0.5; extra == 'pdf-figures'",
    }
)
PRIVATE_PATH_RE = re.compile(
    rb"(?:[A-Za-z]:[\\/]Users[\\/]|/(?:Users|home)/[^/\s]+/|"
    rb"file:(?://+)?(?:[A-Za-z]:[\\/]Users[\\/]|/(?:Users|home)/))",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    rb"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}|"
    rb"sk-(?:(?:proj|svcacct)-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{20,})|"
    rb"(?:AKIA|ASIA)[A-Z0-9]{16}|"
    rb"AIza[0-9A-Za-z_-]{35})"
)
TEXT_SUFFIXES = {
    ".bat",
    ".bib",
    ".cfg",
    ".cls",
    ".css",
    ".csv",
    ".cmd",
    ".htm",
    ".html",
    ".ini",
    ".js",
    ".jsx",
    ".json",
    ".lock",
    ".ltx",
    ".lua",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".rst",
    ".sh",
    ".sty",
    ".tex",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_BASENAMES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "LICENSE",
    "METADATA",
    "PKG-INFO",
    "RECORD",
    "README",
    "WHEEL",
}
REQUIRED_PACKAGE_ASSETS = frozenset(
    {
        "latex_word_review/assets/app.css",
        "latex_word_review/assets/finalize_review_fields.ps1",
    }
)
SCHEMA_PACKAGE_PREFIX = "latex_word_review/schemas/v1alpha/"
SCHEMA_SOURCE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "latex_word_review" / "schemas" / "v1alpha"
)
SCHEMA_CATALOG_FILENAME = "catalog.json"
EXPECTED_SCHEMA_VERSION = "1.0.0-alpha.1"
MAX_SCHEMA_CATALOG_BYTES = 64 * 1024
MAX_SCHEMA_COUNT = 64
SCHEMA_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
SCHEMA_FILENAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.schema\.json$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUNDLE_ATTACK_TOKENS = (
    b"/home/alice/paper.tex",
    b"/Users/Alice/paper.tex",
    b"-----BEGIN PRIVATE KEY-----",
)
VERIFY_ATTACK_TOKENS = (b"C:/Users/synthetic/private/main.tex",)
SELFTEST_ATTACK_TOKENS = (
    b"C:/Users/person/paper.tex",
    b"C:/Users/person/private/paper.tex",
    b"/home/alice/paper.tex",
    b"-----BEGIN PRIVATE KEY-----",
)
ATTACK_VECTOR_TOKENS = {
    ".github/scripts/release_checks.py": (
        *BUNDLE_ATTACK_TOKENS,
        *VERIFY_ATTACK_TOKENS,
        *SELFTEST_ATTACK_TOKENS,
    ),
    ".github/scripts/selftest_release_tools.py": SELFTEST_ATTACK_TOKENS,
    "tests/test_bundle_coverage.py": BUNDLE_ATTACK_TOKENS,
    "tests/test_latex_verify.py": VERIFY_ATTACK_TOKENS,
}
WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"\\|?*')
WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
SDIST_ALLOWED_TOP_LEVEL = frozenset(
    {
        ".agents",
        ".gitignore",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "SECURITY.md",
        "SUPPORT.md",
        "THIRD_PARTY_NOTICES.md",
        "docs",
        "pyproject.toml",
        "plugins",
        "skills",
        "src",
        "third_party",
    }
)
PLUGIN_NAME = "latex-word-review"
PLUGIN_MANIFEST_RELATIVE = "plugins/latex-word-review/.codex-plugin/plugin.json"
MARKETPLACE_RELATIVE = ".agents/plugins/marketplace.json"
CANONICAL_SKILL_RELATIVE = "skills/latex-word-review/SKILL.md"
CANONICAL_OPENAI_YAML_RELATIVE = "skills/latex-word-review/agents/openai.yaml"
EMBEDDED_SKILL_RELATIVE = "plugins/latex-word-review/skills/latex-word-review/SKILL.md"
EMBEDDED_OPENAI_YAML_RELATIVE = (
    "plugins/latex-word-review/skills/latex-word-review/agents/openai.yaml"
)
PLUGIN_DISTRIBUTION_RELATIVE_FILES = frozenset(
    {
        MARKETPLACE_RELATIVE,
        PLUGIN_MANIFEST_RELATIVE,
        CANONICAL_SKILL_RELATIVE,
        CANONICAL_OPENAI_YAML_RELATIVE,
        EMBEDDED_SKILL_RELATIVE,
        EMBEDDED_OPENAI_YAML_RELATIVE,
    }
)


class ReleaseCheckError(RuntimeError):
    """Raised when a release invariant is not satisfied."""


class _BoundedReader:
    """Count all decompressed tar bytes, including metadata and padding."""

    def __init__(self, handle: BinaryIO, limit: int) -> None:
        self._handle = handle
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit - self._read
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self._handle.read(requested)
        self._read += len(data)
        if self._read > self._limit:
            raise ReleaseCheckError("sdist decompressed stream exceeds its hard limit")
        return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseCheckError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseCheckError(f"cannot read valid UTF-8 JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseCheckError(f"expected a JSON object: {path.name}")
    return value


def load_json_bytes(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=reject_duplicate_keys
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseCheckError(f"cannot read valid UTF-8 JSON: {label}") from exc
    if not isinstance(value, dict):
        raise ReleaseCheckError(f"expected a JSON object: {label}")
    return value


def _schema_catalog_entries(catalog_bytes: bytes) -> dict[str, str]:
    if not catalog_bytes or len(catalog_bytes) > MAX_SCHEMA_CATALOG_BYTES:
        raise ReleaseCheckError("schema catalog size is outside its accepted range")
    catalog = load_json_bytes(catalog_bytes, label=SCHEMA_CATALOG_FILENAME)
    if set(catalog) != {"catalog_format", "schema_version", "common", "objects"}:
        raise ReleaseCheckError("schema catalog has an invalid root shape")
    if type(catalog["catalog_format"]) is not int or catalog["catalog_format"] != 1:
        raise ReleaseCheckError("schema catalog format is unsupported")
    if catalog["schema_version"] != EXPECTED_SCHEMA_VERSION:
        raise ReleaseCheckError("schema catalog version differs from the supported contract")

    common = catalog["common"]
    if not isinstance(common, dict) or set(common) != {"file", "sha256"}:
        raise ReleaseCheckError("schema catalog common entry is invalid")
    objects = catalog["objects"]
    if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_SCHEMA_COUNT:
        raise ReleaseCheckError("schema catalog object count is invalid")

    entries: dict[str, str] = {}
    object_names: set[str] = set()
    for entry, is_common in [(common, True), *((item, False) for item in objects)]:
        expected_keys = {"file", "sha256"} if is_common else {"name", "file", "sha256"}
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise ReleaseCheckError("schema catalog entry has unexpected fields")
        filename = entry.get("file")
        digest = entry.get("sha256")
        if not isinstance(filename, str) or SCHEMA_FILENAME_RE.fullmatch(filename) is None:
            raise ReleaseCheckError("schema catalog entry has an invalid filename")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ReleaseCheckError("schema catalog entry has an invalid SHA-256")
        if filename in entries:
            raise ReleaseCheckError("schema catalog contains a duplicate filename")
        entries[filename] = digest
        if not is_common:
            name = entry.get("name")
            if not isinstance(name, str) or SCHEMA_NAME_RE.fullmatch(name) is None:
                raise ReleaseCheckError("schema catalog entry has an invalid object name")
            if name in object_names:
                raise ReleaseCheckError("schema catalog contains a duplicate object name")
            object_names.add(name)
    return entries


def authoritative_schema_payloads() -> dict[str, bytes]:
    """Read the reviewed source catalog and its exact current schema byte set."""

    if SCHEMA_SOURCE_ROOT.is_symlink() or not SCHEMA_SOURCE_ROOT.is_dir():
        raise ReleaseCheckError("authoritative source schema directory is unavailable")
    try:
        paths = sorted(SCHEMA_SOURCE_ROOT.glob("*.json"), key=lambda path: path.name)
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ReleaseCheckError("authoritative source schema set contains a special file")
        payloads = {path.name: path.read_bytes() for path in paths}
    except OSError as exc:
        raise ReleaseCheckError("authoritative source schema set cannot be read") from exc
    catalog_bytes = payloads.get(SCHEMA_CATALOG_FILENAME)
    if catalog_bytes is None:
        raise ReleaseCheckError("authoritative source schema catalog is missing")
    entries = _schema_catalog_entries(catalog_bytes)
    expected = {SCHEMA_CATALOG_FILENAME, *entries}
    if set(payloads) != expected:
        raise ReleaseCheckError("authoritative source schema set differs from its catalog")
    for filename, expected_sha256 in entries.items():
        data = payloads[filename]
        if not data or len(data) > MAX_SCAN_BYTES:
            raise ReleaseCheckError(f"authoritative schema size is invalid: {filename}")
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ReleaseCheckError(f"authoritative schema hash differs from catalog: {filename}")
        load_json_bytes(data, label=filename)
    return payloads


def require_exact_schema_payloads(payloads: dict[str, bytes], *, label: str) -> None:
    """Require an artifact to ship the reviewed catalog and no other JSON schemas."""

    expected = authoritative_schema_payloads()
    missing = sorted(set(expected) - set(payloads))
    unexpected = sorted(set(payloads) - set(expected))
    if missing or unexpected:
        raise ReleaseCheckError(
            f"{label} schema payload set is incomplete or unexpected: "
            + json.dumps({"missing": missing, "unexpected": unexpected}, sort_keys=True)
        )
    changed = next(
        (filename for filename in sorted(expected) if payloads[filename] != expected[filename]),
        None,
    )
    if changed is not None:
        raise ReleaseCheckError(f"{label} schema payload differs from source catalog: {changed}")


def validate_plugin_distribution_payloads(payloads: dict[str, bytes]) -> None:
    missing = sorted(PLUGIN_DISTRIBUTION_RELATIVE_FILES - set(payloads))
    unexpected = sorted(set(payloads) - PLUGIN_DISTRIBUTION_RELATIVE_FILES)
    if missing or unexpected:
        raise ReleaseCheckError(
            "plugin distribution payload set is incomplete or unexpected: "
            + json.dumps({"missing": missing, "unexpected": unexpected}, sort_keys=True)
        )

    for canonical, embedded in (
        (CANONICAL_SKILL_RELATIVE, EMBEDDED_SKILL_RELATIVE),
        (CANONICAL_OPENAI_YAML_RELATIVE, EMBEDDED_OPENAI_YAML_RELATIVE),
    ):
        if payloads[canonical] != payloads[embedded]:
            raise ReleaseCheckError(
                f"packaged plugin copy differs from canonical Skill: {embedded}"
            )

    manifest = load_json_bytes(payloads[PLUGIN_MANIFEST_RELATIVE], label="plugin.json")
    if (
        manifest.get("name") != PLUGIN_NAME
        or manifest.get("skills") != "./skills/"
        or not isinstance(manifest.get("version"), str)
        or not manifest["version"]
    ):
        raise ReleaseCheckError("packaged plugin manifest identity or skills path is invalid")
    if any(field in manifest for field in ("apps", "hooks", "mcpServers")):
        raise ReleaseCheckError(
            "packaged plugin declares a companion component that is not shipped"
        )

    marketplace = load_json_bytes(payloads[MARKETPLACE_RELATIVE], label="marketplace.json")
    entries = marketplace.get("plugins")
    if not isinstance(entries, list):
        raise ReleaseCheckError("packaged marketplace is missing its plugins array")
    matching = [
        entry for entry in entries if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME
    ]
    expected_entry = {
        "name": PLUGIN_NAME,
        "source": {"source": "local", "path": "./plugins/latex-word-review"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    if matching != [expected_entry]:
        raise ReleaseCheckError("packaged marketplace entry does not bind the packaged plugin")

    todo = next((name for name, data in payloads.items() if b"[TODO:" in data), None)
    if todo is not None:
        raise ReleaseCheckError(f"plugin distribution contains a TODO placeholder: {todo}")


def safe_member_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ReleaseCheckError(f"unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseCheckError(f"unsafe archive member name: {name!r}")
    if path.as_posix() != name:
        raise ReleaseCheckError(f"non-canonical archive member name: {name!r}")
    for part in path.parts:
        if (
            unicodedata.normalize("NFC", part) != part
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or any(character in WINDOWS_FORBIDDEN_CHARS for character in part)
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_BASENAMES
        ):
            raise ReleaseCheckError(f"non-portable archive member name: {name!r}")
    return path


def register_archive_member(
    path: PurePosixPath,
    *,
    is_directory: bool,
    entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]],
) -> None:
    """Reject portable-filesystem aliases and file/directory prefix collisions."""
    key = tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)
    for length in range(1, len(key)):
        prefix_key = key[:length]
        raw_prefix = path.parts[:length]
        ancestor = entries.get(prefix_key)
        if ancestor is None:
            entries[prefix_key] = (None, raw_prefix)
            continue
        ancestor_kind, ancestor_raw = ancestor
        if ancestor_raw != raw_prefix:
            raise ReleaseCheckError(f"portable directory spelling collision: {path.as_posix()}")
        if ancestor_kind is False:
            raise ReleaseCheckError(f"archive member is nested below a file: {path.as_posix()}")
    raw_path = path.parts
    existing = entries.get(key)
    if existing is not None:
        existing_kind, existing_raw = existing
        if existing_raw != raw_path:
            raise ReleaseCheckError(
                f"duplicate or portable-path-colliding member: {path.as_posix()}"
            )
        if existing_kind is None and is_directory:
            entries[key] = (True, raw_path)
            return
        raise ReleaseCheckError(f"duplicate or path-colliding member: {path.as_posix()}")
    entries[key] = (is_directory, raw_path)


def artifact_paths(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseCheckError(
            f"expected exactly one wheel and one sdist; found {len(wheels)} and {len(sdists)}"
        )
    return wheels[0], sdists[0]


def should_scan_text(name: str) -> bool:
    path = Path(name)
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_BASENAMES


def scan_member(name: str, data: bytes, *, allowed_tokens: tuple[bytes, ...] = ()) -> None:
    if not should_scan_text(name):
        return
    if len(data) > MAX_SCAN_BYTES:
        raise ReleaseCheckError(f"text member exceeds scan limit: {name}")
    if b"\x00" in data:
        raise ReleaseCheckError(f"text member contains NUL bytes: {name}")
    for token in allowed_tokens:
        data = data.replace(token, b"")
    if PRIVATE_PATH_RE.search(data):
        raise ReleaseCheckError(f"private absolute path pattern found in {name}")
    if SECRET_RE.search(data):
        raise ReleaseCheckError(f"secret-like token found in {name}")


def require_dependency_metadata(metadata: Any, *, label: str) -> None:
    extras = [str(value) for value in metadata.get_all("Provides-Extra", [])]
    requirements = [str(value) for value in metadata.get_all("Requires-Dist", [])]
    if len(extras) != len(set(extras)) or set(extras) != EXPECTED_PROVIDES_EXTRA:
        raise ReleaseCheckError(f"{label} public extras differ from the reviewed contract")
    if len(requirements) != len(set(requirements)) or set(requirements) != EXPECTED_REQUIRES_DIST:
        raise ReleaseCheckError(
            f"{label} runtime/extra requirements differ from the reviewed contract"
        )


def check_wheel(path: Path) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseCheckError("wheel must be a regular file")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ReleaseCheckError("wheel compressed size is outside the accepted range")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ReleaseCheckError("wheel member count is outside the accepted range")
            entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
            expanded = 0
            normalized: list[str] = []
            schema_payloads: dict[str, bytes] = {}
            for info in infos:
                member = safe_member_name(info.filename)
                if info.is_dir():
                    raise ReleaseCheckError("wheel contains an explicit directory member")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if (
                    info.create_system not in {0, 3}
                    or file_type not in {0, stat.S_IFREG}
                    or info.external_attr & 0x10
                ):
                    raise ReleaseCheckError(
                        f"wheel contains a symlink or non-regular member: {info.filename}"
                    )
                register_archive_member(member, is_directory=False, entries=entries)
                if info.flag_bits & 0x1:
                    raise ReleaseCheckError("wheel contains an encrypted member")
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ReleaseCheckError(f"wheel member exceeds its size limit: {info.filename}")
                expanded += info.file_size
                if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise ReleaseCheckError("wheel exceeds its expanded size limit")
                normalized.append(member.as_posix())
            required_exact = {
                "latex_word_review/__about__.py",
                "latex_word_review/_image_worker.py",
                "latex_word_review/image_materializer.py",
                "latex_word_review/image_overlay.py",
                "latex_word_review/offset_mapping.py",
                "latex_word_review/py.typed",
                "latex_word_review/schema_catalog.py",
                "latex_word_review/word_semantics.py",
                "latex_word_review/workflow.py",
                *REQUIRED_PACKAGE_ASSETS,
            }
            missing = sorted(required_exact - set(normalized))
            if missing:
                raise ReleaseCheckError(f"wheel is missing required members: {missing}")
            metadata_infos = [
                info
                for info, name in zip(infos, normalized, strict=True)
                if name.endswith(".dist-info/METADATA")
            ]
            entry_points = [
                name for name in normalized if name.endswith(".dist-info/entry_points.txt")
            ]
            licenses = [name for name in normalized if ".dist-info/licenses/LICENSE" in name]
            wheel_metadata = [name for name in normalized if name.endswith(".dist-info/WHEEL")]
            records = [name for name in normalized if name.endswith(".dist-info/RECORD")]
            if any(
                count != 1
                for count in (
                    len(metadata_infos),
                    len(entry_points),
                    len(licenses),
                    len(wheel_metadata),
                    len(records),
                )
            ):
                raise ReleaseCheckError("wheel metadata, CLI entry point, or license is incomplete")
            dist_info_root = PurePosixPath(metadata_infos[0].filename).parts[0]
            if not (
                dist_info_root.startswith("latex_word_review-")
                and dist_info_root.endswith(".dist-info")
            ):
                raise ReleaseCheckError("wheel has an unexpected dist-info root")
            allowed_dist_info = {
                f"{dist_info_root}/METADATA",
                f"{dist_info_root}/RECORD",
                f"{dist_info_root}/WHEEL",
                f"{dist_info_root}/entry_points.txt",
                f"{dist_info_root}/licenses/LICENSE",
            }
            for name in normalized:
                if name.startswith("latex_word_review/"):
                    if name.startswith("latex_word_review/assets/"):
                        if name not in REQUIRED_PACKAGE_ASSETS:
                            raise ReleaseCheckError(f"unexpected package asset: {name}")
                    elif not (
                        name == "latex_word_review/py.typed"
                        or name.endswith(".py")
                        or name.endswith(".json")
                    ):
                        raise ReleaseCheckError(f"unexpected non-text wheel payload: {name}")
                elif name not in allowed_dist_info:
                    raise ReleaseCheckError(f"unexpected wheel payload path: {name}")
            metadata_bytes: bytes | None = None
            for info, name in zip(infos, normalized, strict=True):
                if name.lower().endswith((".docx", ".pdf")):
                    raise ReleaseCheckError(
                        f"document artifact must not be shipped in wheel: {name}"
                    )
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise ReleaseCheckError(f"wheel member size mismatch: {info.filename}")
                if name.startswith(SCHEMA_PACKAGE_PREFIX) and name.endswith(".json"):
                    schema_payloads[name.removeprefix(SCHEMA_PACKAGE_PREFIX)] = data
                if should_scan_text(name):
                    if len(data) > MAX_SCAN_BYTES:
                        raise ReleaseCheckError(f"text member exceeds scan limit: {name}")
                    scan_member(name, data)
                if info is metadata_infos[0]:
                    metadata_bytes = data
            require_exact_schema_payloads(schema_payloads, label="wheel")
            if metadata_bytes is None:
                raise ReleaseCheckError("wheel metadata could not be read")
            metadata = BytesParser().parsebytes(metadata_bytes)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseCheckError(f"invalid wheel: {path.name}") from exc

    critical = {
        field: metadata.get_all(field, [])
        for field in ("Name", "Version", "Requires-Python", "License-Expression")
    }
    if any(len(values) != 1 for values in critical.values()):
        raise ReleaseCheckError("wheel critical metadata fields must occur exactly once")
    name = str(critical["Name"][0])
    version = str(critical["Version"][0])
    if name != "latex-word-review" or not version:
        raise ReleaseCheckError(f"unexpected wheel identity: {name!r} {version!r}")
    if dist_info_root != f"latex_word_review-{version}.dist-info":
        raise ReleaseCheckError("wheel dist-info root differs from its metadata identity")
    if path.name != f"latex_word_review-{version}-py3-none-any.whl":
        raise ReleaseCheckError(
            "wheel filename differs from its metadata identity or supported tag"
        )
    if str(critical["Requires-Python"][0]) != EXPECTED_REQUIRES_PYTHON:
        raise ReleaseCheckError(f"wheel Requires-Python must be exactly {EXPECTED_REQUIRES_PYTHON}")
    if str(critical["License-Expression"][0]) != "Apache-2.0":
        raise ReleaseCheckError("wheel must declare the Apache-2.0 license expression")
    require_dependency_metadata(metadata, label="wheel")
    return name, version


def check_sdist(path: Path, expected_version: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseCheckError("sdist must be a regular file")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ReleaseCheckError("sdist compressed size is outside the accepted range")
    names: list[str] = []
    entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
    expanded = 0
    pkg_info_candidates: dict[str, bytes] = {}
    plugin_payloads: dict[str, bytes] = {}
    schema_payloads: dict[str, bytes] = {}
    try:
        with (
            path.open("rb") as compressed,
            gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed,
        ):
            bounded = _BoundedReader(decompressed, MAX_ARCHIVE_STREAM_BYTES)
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    if len(names) >= MAX_ARCHIVE_MEMBERS:
                        raise ReleaseCheckError("sdist member count exceeds its limit")
                    if not (member.isfile() or member.isdir()):
                        raise ReleaseCheckError(
                            f"sdist contains a link, device, or unsupported member: {member.name}"
                        )
                    safe = safe_member_name(member.name)
                    register_archive_member(safe, is_directory=member.isdir(), entries=entries)
                    if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ReleaseCheckError(
                            f"sdist member exceeds its size limit: {member.name}"
                        )
                    expanded += member.size
                    if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise ReleaseCheckError("sdist exceeds its expanded size limit")
                    name = safe.as_posix()
                    names.append(name)
                    if member.isdir():
                        continue
                    if name.lower().endswith((".docx", ".pdf")):
                        raise ReleaseCheckError(
                            f"document artifact must not be shipped in sdist: {name}"
                        )
                    capture = should_scan_text(name)
                    if not capture and safe.name != "py.typed":
                        raise ReleaseCheckError(f"unexpected non-text sdist payload: {name}")
                    if capture and member.size > MAX_SCAN_BYTES:
                        raise ReleaseCheckError(f"text member exceeds scan limit: {name}")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ReleaseCheckError(f"cannot inspect sdist member: {name}")
                    chunks: list[bytes] = []
                    copied = 0
                    while True:
                        block = extracted.read(1024 * 1024)
                        if not block:
                            break
                        copied += len(block)
                        if copied > member.size:
                            raise ReleaseCheckError(f"sdist member size overflow: {name}")
                        if capture:
                            chunks.append(block)
                    if copied != member.size:
                        raise ReleaseCheckError(f"sdist member size mismatch: {name}")
                    if capture:
                        data = b"".join(chunks)
                        scan_member(name, data)
                        if safe.name == "PKG-INFO":
                            pkg_info_candidates[name] = data
                        if len(safe.parts) > 1:
                            relative = PurePosixPath(*safe.parts[1:]).as_posix()
                            if relative in PLUGIN_DISTRIBUTION_RELATIVE_FILES:
                                plugin_payloads[relative] = data
                            schema_prefix = "src/" + SCHEMA_PACKAGE_PREFIX
                            if relative.startswith(schema_prefix) and relative.endswith(".json"):
                                schema_payloads[relative.removeprefix(schema_prefix)] = data
            while bounded.read(1024 * 1024):
                pass
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseCheckError(f"invalid sdist: {path.name}") from exc

    if not names:
        raise ReleaseCheckError("sdist contains no members")
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        raise ReleaseCheckError("sdist must have exactly one top-level directory")
    root = next(iter(roots))
    if root != f"latex_word_review-{expected_version}":
        raise ReleaseCheckError("sdist root differs from the wheel metadata identity")
    unexpected_top = sorted(
        {
            PurePosixPath(name).parts[1]
            for name in names
            if len(PurePosixPath(name).parts) > 1
            and PurePosixPath(name).parts[1] not in SDIST_ALLOWED_TOP_LEVEL
        }
    )
    if unexpected_top:
        raise ReleaseCheckError(f"sdist has unexpected top-level payloads: {unexpected_top}")
    required = {
        f"{root}/LICENSE",
        f"{root}/PKG-INFO",
        f"{root}/README.md",
        f"{root}/SUPPORT.md",
        f"{root}/pyproject.toml",
        *(f"{root}/{relative}" for relative in PLUGIN_DISTRIBUTION_RELATIVE_FILES),
        f"{root}/src/latex_word_review/__about__.py",
        f"{root}/src/latex_word_review/py.typed",
        f"{root}/src/latex_word_review/schema_catalog.py",
        f"{root}/third_party/Contributor-Covenant-LICENSE.txt",
        f"{root}/third_party/python-docx-LICENSE.txt",
    }
    missing = sorted(required - set(names))
    if missing:
        raise ReleaseCheckError(f"sdist is missing required members: {missing}")
    require_exact_schema_payloads(schema_payloads, label="sdist")
    validate_plugin_distribution_payloads(plugin_payloads)
    pkg_info_bytes = pkg_info_candidates.get(f"{root}/PKG-INFO")
    if pkg_info_bytes is None:
        raise ReleaseCheckError("cannot inspect sdist PKG-INFO")
    pkg_info = BytesParser().parsebytes(pkg_info_bytes)
    critical = {
        field: pkg_info.get_all(field, [])
        for field in ("Name", "Version", "Requires-Python", "License-Expression")
    }
    if any(len(values) != 1 for values in critical.values()):
        raise ReleaseCheckError("sdist critical metadata fields must occur exactly once")
    if str(critical["Name"][0]) != "latex-word-review":
        raise ReleaseCheckError("sdist PKG-INFO has an unexpected project name")
    if str(critical["Version"][0]) != expected_version:
        raise ReleaseCheckError("sdist PKG-INFO version differs from the wheel")
    if str(critical["Requires-Python"][0]) != EXPECTED_REQUIRES_PYTHON:
        raise ReleaseCheckError(f"sdist Requires-Python must be exactly {EXPECTED_REQUIRES_PYTHON}")
    if str(critical["License-Expression"][0]) != "Apache-2.0":
        raise ReleaseCheckError("sdist must declare the Apache-2.0 license expression")
    require_dependency_metadata(pkg_info, label="sdist")
    forbidden_prefixes = (
        f"{root}/build/",
        f"{root}/output/",
        f"{root}/samples/",
        f"{root}/tests/",
    )
    forbidden = next((name for name in names if name.startswith(forbidden_prefixes)), None)
    if forbidden is not None:
        raise ReleaseCheckError(f"private/runtime/fixture material in sdist: {forbidden}")

    if path.name != f"latex_word_review-{expected_version}.tar.gz":
        raise ReleaseCheckError("sdist filename differs from its metadata identity")


def run_twine(paths: tuple[Path, Path]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "twine", "check", *(str(path) for path in paths)],
        check=False,
        timeout=120,
        shell=False,
    )
    if result.returncode != 0:
        raise ReleaseCheckError("twine metadata validation failed")


def command_dist(args: argparse.Namespace) -> None:
    directory = args.dir.resolve()
    wheel, sdist = artifact_paths(directory)
    _, version = check_wheel(wheel)
    check_sdist(sdist, version)
    if args.twine:
        run_twine((wheel, sdist))
    print(json.dumps({"status": "pass", "version": version, "artifacts": [wheel.name, sdist.name]}))


def report_from_summary(summary_path: Path, relative: str) -> dict[str, Any]:
    candidate = (summary_path.parent / relative).resolve()
    try:
        candidate.relative_to(summary_path.parent.resolve())
    except ValueError as exc:
        raise ReleaseCheckError("fixture report path escapes its output directory") from exc
    return load_json(candidate)


def command_fixture(args: argparse.Namespace) -> None:
    summary_path = args.summary.resolve()
    summary = load_json(summary_path)
    if summary.get("fixture_integrity_status") != "pass":
        raise ReleaseCheckError("public fixture integrity did not pass")
    report_paths = summary.get("reports")
    if not isinstance(report_paths, dict):
        raise ReleaseCheckError("fixture summary is missing report paths")
    reports = {
        name: report_from_summary(summary_path, relative)
        for name, relative in report_paths.items()
        if isinstance(name, str) and isinstance(relative, str)
    }
    for name in ("structure", "changeset_oracle", "privacy", "provenance"):
        if name not in reports:
            raise ReleaseCheckError(f"fixture summary is missing {name} report")
    for name in ("structure", "changeset_oracle", "privacy"):
        if reports[name].get("status") != "pass":
            raise ReleaseCheckError(f"fixture {name} report did not pass")

    provenance = reports["provenance"]
    checks = provenance.get("release_checks")
    if not isinstance(checks, list):
        raise ReleaseCheckError("fixture provenance report is missing release checks")
    failed_names = {
        str(item.get("name"))
        for item in checks
        if isinstance(item, dict) and item.get("pass") is not True
    }
    malformed = [item for item in checks if not isinstance(item, dict) or "pass" not in item]
    if malformed:
        raise ReleaseCheckError("fixture provenance contains malformed release checks")

    if args.mode == "release":
        if failed_names or summary.get("release_readiness") != "ready":
            raise ReleaseCheckError(
                "public fixture is not release-ready; failed gates: "
                + ", ".join(sorted(failed_names or {"release_readiness"}))
            )
        if summary.get("status") != "pass" or provenance.get("status") != "pass":
            raise ReleaseCheckError("fixture release reports are not fully passing")
    else:
        unexpected = failed_names - {"visual_review_complete"}
        if unexpected:
            raise ReleaseCheckError(
                "fixture has non-visual release failures: " + ", ".join(sorted(unexpected))
            )
        expected_readiness = "blocked" if failed_names else "ready"
        if summary.get("release_readiness") != expected_readiness:
            raise ReleaseCheckError("fixture release-readiness state is inconsistent")

    print(
        json.dumps(
            {
                "status": "pass",
                "mode": args.mode,
                "release_readiness": summary.get("release_readiness"),
                "deferred_gates": sorted(failed_names),
            }
        )
    )


def tracked_files(root: Path, *, include_untracked: bool = False) -> list[str]:
    arguments = ["git", "ls-files", "-z"]
    if include_untracked:
        arguments = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    result = subprocess.run(
        arguments,
        cwd=root,
        check=False,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    if result.returncode != 0:
        raise ReleaseCheckError("git ls-files failed")
    try:
        names = result.stdout.decode("utf-8", errors="strict").split("\x00")
    except UnicodeDecodeError as exc:
        raise ReleaseCheckError("tracked path is not UTF-8") from exc
    return sorted({name for name in names if name})


def command_repo(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    include_untracked = bool(getattr(args, "include_untracked", False))
    names = tracked_files(root, include_untracked=include_untracked)
    if not names:
        if include_untracked:
            raise ReleaseCheckError("no public candidate files found")
        print(json.dumps({"status": "skipped", "reason": "no_tracked_files"}))
        return
    forbidden_prefixes = ("build/", "output/", "runs/", "archives/", "samples/private/")
    checked_text = 0
    findings: list[str] = []
    entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
    for name in names:
        safe = safe_member_name(name)
        register_archive_member(safe, is_directory=False, entries=entries)
        normalized = safe.as_posix()
        root_part = safe.parts[0]
        if (
            normalized.startswith(forbidden_prefixes)
            or root_part.lower().startswith("par-")
            or normalized.lower().endswith(".tmp")
        ):
            findings.append(f"private/runtime path is tracked: {normalized}")
            continue
        path = (root / normalized).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ReleaseCheckError(f"tracked path escapes repository: {normalized}") from exc
        if path.is_symlink() or not path.is_file():
            findings.append(f"tracked path is not a regular file: {normalized}")
            continue
        lower = normalized.lower()
        if lower.endswith((".docx", ".docm", ".pdf")) and not normalized.startswith(
            "tests/fixtures/e0-minimal-paper/"
        ):
            findings.append(f"unapproved document binary is tracked: {normalized}")
            continue
        if should_scan_text(normalized):
            size = path.stat().st_size
            if size > MAX_SCAN_BYTES:
                findings.append(f"tracked text exceeds scan limit: {normalized}")
                continue
            try:
                scan_member(
                    normalized,
                    path.read_bytes(),
                    allowed_tokens=ATTACK_VECTOR_TOKENS.get(normalized, ()),
                )
            except ReleaseCheckError as exc:
                findings.append(str(exc))
            checked_text += 1
    if findings:
        raise ReleaseCheckError(
            f"public repository scan found {len(findings)} issue(s): " + "; ".join(findings[:20])
        )
    print(
        json.dumps(
            {
                "status": "pass",
                "candidate_files": len(names),
                "included_untracked": include_untracked,
                "privacy_scanned_text": checked_text,
            }
        )
    )


def directory_hashes(directory: Path) -> dict[str, str]:
    wheel, sdist = artifact_paths(directory)
    return {path.name: sha256_file(path) for path in (wheel, sdist)}


def command_compare(args: argparse.Namespace) -> None:
    left = args.left.resolve()
    right = args.right.resolve()
    left_hashes = directory_hashes(left)
    right_hashes = directory_hashes(right)
    if left_hashes != right_hashes:
        raise ReleaseCheckError(
            "two isolated builds are not byte-for-byte reproducible: "
            + json.dumps({"first": left_hashes, "second": right_hashes}, sort_keys=True)
        )
    output = args.output.resolve()
    if output.exists():
        raise ReleaseCheckError("published distribution directory already exists")
    output.mkdir(parents=True)
    for name in sorted(left_hashes):
        shutil.copyfile(left / name, output / name)
    print(json.dumps({"status": "pass", "artifacts": left_hashes}, sort_keys=True))


def project_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', text, re.MULTILINE)
    if len(matches) != 1:
        raise ReleaseCheckError("could not read exactly one package version")
    return matches[0]


def command_version(args: argparse.Namespace) -> None:
    version = project_version(args.version_file.resolve())
    ref = args.ref
    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        if tag != f"v{version}":
            raise ReleaseCheckError(f"tag {tag!r} does not match package version v{version}")
        ref_kind = "tag"
    else:
        ref_kind = "workflow_dispatch_candidate"
    print(json.dumps({"status": "pass", "version": version, "ref_kind": ref_kind}))


def command_source_date_epoch(args: argparse.Namespace) -> None:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
        shell=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value.isascii() or not value.isdigit():
        raise ReleaseCheckError("could not derive SOURCE_DATE_EPOCH from the checked-out commit")
    with args.github_env.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"SOURCE_DATE_EPOCH={value}\n")
    print(json.dumps({"status": "pass", "source_date_epoch": value}))


def parse_checksum_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if match is None or match.group(2) in result:
            raise ReleaseCheckError("invalid or duplicate SHA256SUMS entry")
        result[match.group(2)] = match.group(1)
    return result


def command_evidence(args: argparse.Namespace) -> None:
    dist_dir = args.dist_dir.resolve()
    evidence_dir = args.evidence_dir.resolve()
    expected_evidence_names = {
        "SHA256SUMS",
        "evidence-manifest.json",
        "provenance.intoto.json",
        "sbom.cdx.json",
    }
    actual_evidence_names = {path.name for path in evidence_dir.iterdir() if path.is_file()}
    if actual_evidence_names != expected_evidence_names:
        raise ReleaseCheckError("release evidence directory has missing or unexpected files")
    wheel, _ = artifact_paths(dist_dir)
    _, dist_version = check_wheel(wheel)
    dist_hashes = directory_hashes(dist_dir)
    sums = parse_checksum_file(evidence_dir / "SHA256SUMS")
    if sums != dist_hashes:
        raise ReleaseCheckError("SHA256SUMS does not match release distributions")

    sbom = load_json(evidence_dir / "sbom.cdx.json")
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        raise ReleaseCheckError("SBOM is not CycloneDX 1.6")
    metadata = sbom.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if (
        not isinstance(component, dict)
        or component.get("name") != "latex-word-review"
        or component.get("version") != dist_version
    ):
        raise ReleaseCheckError("SBOM does not identify the release component")

    provenance = load_json(evidence_dir / "provenance.intoto.json")
    if provenance.get("_type") != "https://in-toto.io/Statement/v1":
        raise ReleaseCheckError("provenance is not an in-toto Statement v1")
    if provenance.get("predicateType") != "https://slsa.dev/provenance/v1":
        raise ReleaseCheckError("custom provenance does not use the SLSA v1 predicate type")
    subjects = provenance.get("subject")
    if not isinstance(subjects, list):
        raise ReleaseCheckError("provenance subjects are missing")
    subject_hashes = {
        str(item.get("name")): str(item.get("digest", {}).get("sha256"))
        for item in subjects
        if isinstance(item, dict) and isinstance(item.get("digest"), dict)
    }
    if len(subjects) != len(subject_hashes) or subject_hashes != dist_hashes:
        raise ReleaseCheckError("provenance subjects do not match release distributions")

    manifest = load_json(evidence_dir / "evidence-manifest.json")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ReleaseCheckError("evidence manifest is missing files")
    manifest_hashes: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ReleaseCheckError("malformed evidence manifest entry")
        relative = item.get("path")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ReleaseCheckError("malformed evidence manifest path or digest")
        safe = safe_member_name(relative)
        root = dist_dir.parent
        candidate = (root / safe.as_posix()).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ReleaseCheckError("evidence manifest path escapes release root") from exc
        if not candidate.is_file() or sha256_file(candidate) != digest:
            raise ReleaseCheckError(f"evidence manifest digest mismatch: {relative}")
        if relative in manifest_hashes:
            raise ReleaseCheckError(f"duplicate evidence manifest path: {relative}")
        manifest_hashes[relative] = digest

    release_root = dist_dir.parent.resolve()
    expected_manifest_paths = {
        path.resolve().relative_to(release_root).as_posix()
        for path in [
            *artifact_paths(dist_dir),
            evidence_dir / "SHA256SUMS",
            evidence_dir / "provenance.intoto.json",
            evidence_dir / "sbom.cdx.json",
        ]
    }
    if set(manifest_hashes) != expected_manifest_paths:
        raise ReleaseCheckError("evidence manifest file set is incomplete or contains extras")
    predicate = provenance.get("predicate")
    build_definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
    external = (
        build_definition.get("externalParameters") if isinstance(build_definition, dict) else None
    )
    source = manifest.get("source")
    if not isinstance(external, dict) or not isinstance(source, dict):
        raise ReleaseCheckError("release source binding is missing")
    if source.get("ref") != external.get("source_ref") or source.get("sha") != external.get(
        "source_sha"
    ):
        raise ReleaseCheckError("evidence source ref/SHA bindings disagree")

    print(json.dumps({"status": "pass", "artifacts": sorted(dist_hashes)}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dist = subparsers.add_parser("dist", help="validate wheel and sdist contents")
    dist.add_argument("--dir", type=Path, required=True)
    dist.add_argument("--twine", action="store_true")
    dist.set_defaults(func=command_dist)

    fixture = subparsers.add_parser("fixture", help="validate fixture QA reports")
    fixture.add_argument("--summary", type=Path, required=True)
    fixture.add_argument("--mode", choices=("ci", "release"), required=True)
    fixture.set_defaults(func=command_fixture)

    repo = subparsers.add_parser("repo", help="scan public repository candidate material")
    repo.add_argument("--root", type=Path, default=Path("."))
    repo.add_argument(
        "--include-untracked",
        action="store_true",
        help="also scan non-ignored untracked files (for an unborn/local candidate)",
    )
    repo.set_defaults(func=command_repo)

    compare = subparsers.add_parser("compare", help="compare and publish two identical builds")
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(func=command_compare)

    version = subparsers.add_parser("version", help="bind a tag to the package version")
    version.add_argument(
        "--version-file", type=Path, default=Path("src/latex_word_review/__about__.py")
    )
    version.add_argument("--ref", required=True)
    version.set_defaults(func=command_version)

    epoch = subparsers.add_parser("source-date-epoch", help="set reproducible build time")
    epoch.add_argument("--github-env", type=Path, required=True)
    epoch.set_defaults(func=command_source_date_epoch)

    evidence = subparsers.add_parser("evidence", help="verify release evidence")
    evidence.add_argument("--dist-dir", type=Path, required=True)
    evidence.add_argument("--evidence-dir", type=Path, required=True)
    evidence.set_defaults(func=command_evidence)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (OSError, UnicodeError, ReleaseCheckError, subprocess.TimeoutExpired) as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
