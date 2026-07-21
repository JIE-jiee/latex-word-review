#!/usr/bin/env python3
"""Build/install one distribution through locked, fail-closed release boundaries."""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import unicodedata
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


class CleanInstallError(RuntimeError):
    """Raised when the isolated installation smoke test fails."""


class _BoundedReader:
    """Count every decompressed tar byte, including PAX/GNU metadata and padding."""

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
            raise CleanInstallError("sdist decompressed stream exceeds its hard limit")
        return data


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = PROJECT_ROOT / "build"
LOCK_FILE = PROJECT_ROOT / "uv.lock"
PUBLIC_E0_DEMO = PROJECT_ROOT / "scripts" / "run_public_e0_cli_demo.py"
LOCKED_BUILD_PACKAGES = ("build", "hatchling", "pip", "setuptools", "wheel")
EXPECTED_PROJECT_NAME = "latex-word-review"
EXPECTED_PROJECT_VERSION_PATH = "src/latex_word_review/__about__.py"
EXPECTED_REQUIRES_PYTHON = ">=3.12,<3.14"
EXPECTED_METADATA_REQUIRES_PYTHON = "<3.14,>=3.12"
EXPECTED_SDIST_INCLUDES = (
    "/.agents",
    "/CHANGELOG.md",
    "/CODE_OF_CONDUCT.md",
    "/CONTRIBUTING.md",
    "/docs",
    "/LICENSE",
    "/README.md",
    "/SECURITY.md",
    "/skills",
    "/plugins",
    "/SUPPORT.md",
    "/third_party",
    "/THIRD_PARTY_NOTICES.md",
    "/src",
)
MAX_SDIST_BYTES = 50 * 1024 * 1024
MAX_SDIST_MEMBERS = 4096
MAX_SDIST_MEMBER_BYTES = 20 * 1024 * 1024
MAX_SDIST_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_SDIST_STREAM_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_REQUIREMENTS_BYTES = 2 * 1024 * 1024
MAX_PYPROJECT_BYTES = 2 * 1024 * 1024
HASH_RE = re.compile(r"--hash=(sha256:[0-9a-f]{64})(?:\s|$)")
PIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[^\s;]+")
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", choices=("wheel", "sdist"), required=True)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument(
        "--public-e0-output",
        type=Path,
        help=(
            "fresh build/ child used for the installed-package public E0 CLI demo; "
            "omit to run only entry-point smoke tests"
        ),
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def select_artifact(directory: Path, kind: str) -> Path:
    pattern = "*.whl" if kind == "wheel" else "*.tar.gz"
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise CleanInstallError(f"expected exactly one {kind} artifact; found {len(matches)}")
    return matches[0]


def environment_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def console_script(root: Path) -> Path:
    return root / ("Scripts/latex-word-review.exe" if os.name == "nt" else "bin/latex-word-review")


def run(
    command: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=env,
        timeout=timeout,
        shell=False,
    )
    if result.returncode != 0:
        program = Path(command[0]).name if command else "unknown"
        raise CleanInstallError(f"{program} command failed with exit code {result.returncode}")


def _reject_parent_components(path: Path, root: Path, *, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CleanInstallError(f"{label} escapes its required root") from exc
    cursor = root
    if cursor.is_symlink():
        raise CleanInstallError(f"{label} root must not be a symbolic link")
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CleanInstallError(f"{label} must not contain symbolic-link components")


def _input_path(path: Path) -> Path:
    if ".." in path.parts:
        raise CleanInstallError("paths containing parent traversal are not accepted")
    return path if path.is_absolute() else PROJECT_ROOT / path


def _build_child(path: Path, *, label: str) -> Path:
    raw = _input_path(path)
    root = BUILD_ROOT.resolve()
    absolute = Path(os.path.abspath(raw))
    _reject_parent_components(absolute, root, label=label)
    resolved = absolute.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise CleanInstallError(f"{label} must be inside build/") from exc
    if not relative.parts:
        raise CleanInstallError(f"{label} cannot be the build directory itself")
    return resolved


def _repository_directory(path: Path, *, label: str) -> Path:
    raw = _input_path(path)
    root = PROJECT_ROOT.resolve()
    absolute = Path(os.path.abspath(raw))
    _reject_parent_components(absolute, root, label=label)
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise CleanInstallError(f"{label} does not exist") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CleanInstallError(f"{label} must be inside the repository") from exc
    if not resolved.is_dir():
        raise CleanInstallError(f"{label} must be a directory")
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _isolated_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def _locked_versions() -> dict[str, str]:
    try:
        value = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CleanInstallError("cannot parse the repository uv.lock") from exc
    packages = value.get("package")
    if not isinstance(packages, list):
        raise CleanInstallError("uv.lock has no package inventory")
    versions: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            if name in versions and versions[name] != version:
                raise CleanInstallError(f"uv.lock contains multiple versions for {name}")
            versions[name] = version
    return versions


def require_locked_builder() -> dict[str, str]:
    locked = _locked_versions()
    verified: dict[str, str] = {}
    for name in LOCKED_BUILD_PACKAGES:
        expected = locked.get(name)
        if expected is None:
            raise CleanInstallError(f"{name} is absent from the locked build environment")
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise CleanInstallError(f"locked build package {name} is not installed") from exc
        if installed != expected:
            raise CleanInstallError(
                f"locked build package {name} is {installed}, expected {expected}"
            )
        verified[name] = installed
    if verified["hatchling"] != "1.31.0":
        raise CleanInstallError("release builds require hatchling 1.31.0")
    return verified


def _logical_requirements(content: str) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1].strip() + " "
            continue
        logical.append((current + line).strip())
        current = ""
    if current:
        raise CleanInstallError("runtime requirement export ends with a continuation")
    return logical


def validate_runtime_requirements(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise CleanInstallError("runtime requirement export is missing or not a regular file")
    if path.stat().st_size > MAX_RUNTIME_REQUIREMENTS_BYTES:
        raise CleanInstallError("runtime requirement export exceeds its size limit")
    content = path.read_text(encoding="utf-8")
    requirements = _logical_requirements(content)
    if not requirements:
        raise CleanInstallError("runtime requirement export is empty")
    for requirement in requirements:
        lowered = requirement.lower()
        if any(
            token in lowered
            for token in (" @ ", "file:", "http:", "https:", "-e ", "--index", "--find-links")
        ):
            raise CleanInstallError("runtime requirement export contains a non-registry source")
        head = requirement.split(maxsplit=1)[0]
        if PIN_RE.fullmatch(head) is None:
            raise CleanInstallError(f"runtime requirement is not exactly pinned: {head}")
        hashes = HASH_RE.findall(requirement)
        if not hashes or requirement.count("--hash=") != len(hashes):
            raise CleanInstallError(f"runtime requirement has missing or malformed hashes: {head}")
    return len(requirements)


def export_runtime_requirements(work_root: Path, environment: dict[str, str]) -> tuple[Path, int]:
    uv = shutil.which("uv")
    if uv is None:
        raise CleanInstallError("uv is required to export the frozen runtime closure")
    output = work_root / "runtime-requirements.txt"
    run(
        [
            uv,
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--output-file",
            str(output),
        ],
        timeout=120,
        cwd=PROJECT_ROOT,
        env=environment,
    )
    return output, validate_runtime_requirements(output)


def _canonical_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise CleanInstallError(f"wheel has a CRC failure: {path.name}")
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise CleanInstallError(f"wheel has invalid metadata cardinality: {path.name}")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise CleanInstallError(f"cannot inspect wheel: {path.name}") from exc
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise CleanInstallError(f"wheel identity fields are duplicated or missing: {path.name}")
    name = str(names[0])
    version = str(versions[0])
    if not name or not version:
        raise CleanInstallError(f"wheel identity is incomplete: {path.name}")
    return _canonical_distribution_name(name), version


def require_matching_wheel_payload(reference: Path, candidate: Path) -> None:
    """Require identical installed bytes while ignoring only ZIP container metadata."""
    try:
        with zipfile.ZipFile(reference) as left, zipfile.ZipFile(candidate) as right:
            if left.testzip() is not None or right.testzip() is not None:
                raise CleanInstallError("wheel payload comparison found a CRC failure")
            left_names = left.namelist()
            right_names = right.namelist()
            if len(left_names) != len(set(left_names)) or len(right_names) != len(set(right_names)):
                raise CleanInstallError("wheel payload comparison found duplicate members")
            if left_names != right_names:
                raise CleanInstallError(
                    "sdist-derived wheel member list differs from release wheel"
                )
            for name in left_names:
                if left.read(name) != right.read(name):
                    raise CleanInstallError(
                        f"sdist-derived wheel payload differs from release wheel: {name}"
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        raise CleanInstallError("cannot compare release and sdist-derived wheel payloads") from exc


def build_runtime_wheelhouse(
    requirements: Path, work_root: Path, environment: dict[str, str]
) -> tuple[Path, Path, dict[str, str]]:
    wheelhouse = work_root / "runtime-wheelhouse"
    wheelhouse.mkdir()
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--quiet",
            "--require-hashes",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            "--requirement",
            str(requirements),
        ],
        timeout=600,
        cwd=PROJECT_ROOT,
        env=environment,
    )
    wheels = sorted(wheelhouse.iterdir())
    if not wheels or any(
        path.is_symlink() or not path.is_file() or path.suffix != ".whl" for path in wheels
    ):
        raise CleanInstallError("runtime wheelhouse contains missing or non-wheel material")
    identities: dict[str, str] = {}
    locked_wheels = work_root / "runtime-wheels.txt"
    lines: list[str] = []
    for wheel in wheels:
        name, version = _wheel_identity(wheel)
        if name == "latex-word-review":
            raise CleanInstallError("runtime closure unexpectedly contains the project artifact")
        if name in identities:
            raise CleanInstallError(f"runtime wheelhouse contains duplicate distribution {name}")
        identities[name] = version
        lines.append(f"{wheel.resolve().as_uri()} --hash=sha256:{sha256_file(wheel)}")
    locked_wheels.write_text("\n".join(lines) + "\n", encoding="utf-8")
    validate_local_wheel_requirements(locked_wheels, len(wheels))
    return wheelhouse, locked_wheels, identities


def validate_local_wheel_requirements(path: Path, expected: int) -> None:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != expected:
        raise CleanInstallError("local wheel requirement count does not match the wheelhouse")
    for line in lines:
        if not line.startswith("file:") or line.count("--hash=sha256:") != 1:
            raise CleanInstallError("local wheel requirement is not file-and-hash bound")
        digest = line.rsplit("--hash=sha256:", 1)[1]
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CleanInstallError("local wheel requirement contains a malformed digest")


def _safe_sdist_member(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise CleanInstallError("sdist contains an unsafe member name")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CleanInstallError(f"sdist contains an unsafe member name: {name!r}")
    if path.as_posix() != name:
        raise CleanInstallError(f"sdist contains a non-canonical member name: {name!r}")
    for part in path.parts:
        if (
            unicodedata.normalize("NFC", part) != part
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or any(character in WINDOWS_FORBIDDEN_CHARS for character in part)
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_BASENAMES
        ):
            raise CleanInstallError(f"sdist contains a non-portable member name: {name!r}")
    return path


def _register_sdist_member(
    path: PurePosixPath,
    *,
    is_directory: bool,
    entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]],
) -> None:
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
            raise CleanInstallError("sdist contains a portable directory spelling collision")
        if ancestor_kind is False:
            raise CleanInstallError("sdist contains a member nested below a file")
    raw_path = path.parts
    existing = entries.get(key)
    if existing is not None:
        existing_kind, existing_raw = existing
        if existing_raw != raw_path:
            raise CleanInstallError("sdist contains portable-path-colliding members")
        if existing_kind is None and is_directory:
            entries[key] = (True, raw_path)
            return
        raise CleanInstallError("sdist contains duplicate or path-colliding members")
    entries[key] = (is_directory, raw_path)


def safe_unpack_sdist(artifact: Path, destination: Path) -> Path:
    if artifact.is_symlink() or not artifact.is_file():
        raise CleanInstallError("sdist must be a regular file")
    compressed_size = artifact.stat().st_size
    if compressed_size <= 0 or compressed_size > MAX_SDIST_BYTES:
        raise CleanInstallError("sdist compressed size is outside the accepted range")
    destination.mkdir()
    root_name: str | None = None
    member_count = 0
    expanded = 0
    entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
    try:
        with (
            artifact.open("rb") as compressed,
            gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed,
        ):
            bounded = _BoundedReader(decompressed, MAX_SDIST_STREAM_BYTES)
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    member_count += 1
                    if member_count > MAX_SDIST_MEMBERS:
                        raise CleanInstallError("sdist member count exceeds its limit")
                    path = _safe_sdist_member(member.name)
                    if root_name is None:
                        root_name = path.parts[0]
                    elif path.parts[0] != root_name:
                        raise CleanInstallError(
                            "sdist must contain exactly one top-level directory"
                        )
                    if not (member.isfile() or member.isdir()):
                        raise CleanInstallError(
                            "sdist contains a link, device, or unsupported member"
                        )
                    _register_sdist_member(path, is_directory=member.isdir(), entries=entries)
                    if member.size < 0 or member.size > MAX_SDIST_MEMBER_BYTES:
                        raise CleanInstallError("sdist member exceeds its size limit")
                    expanded += member.size
                    if expanded > MAX_SDIST_EXPANDED_BYTES:
                        raise CleanInstallError("sdist exceeds its expanded size limit")

                    target = destination.joinpath(*path.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise CleanInstallError(f"cannot read sdist member {member.name!r}")
                    copied = 0
                    with target.open("xb") as handle:
                        while True:
                            block = extracted.read(1024 * 1024)
                            if not block:
                                break
                            copied += len(block)
                            if copied > member.size:
                                raise CleanInstallError(
                                    "sdist member expanded beyond its declared size"
                                )
                            handle.write(block)
                    if copied != member.size:
                        raise CleanInstallError("sdist member size does not match its declaration")
            while bounded.read(1024 * 1024):
                pass
    except (OSError, tarfile.TarError) as exc:
        raise CleanInstallError("sdist could not be safely unpacked") from exc
    if member_count == 0 or root_name is None:
        raise CleanInstallError("sdist contains no members")
    root = destination / root_name
    if root.is_symlink() or not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise CleanInstallError("sdist root is missing a regular pyproject.toml")
    return root


def _read_bounded_regular(path: Path, *, label: str, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CleanInstallError(f"sdist {label} must be a regular file")
    size = path.stat().st_size
    if size <= 0 or size > limit:
        raise CleanInstallError(f"sdist {label} size is outside the accepted range")
    return path.read_bytes()


def _static_version(path: Path) -> str:
    data = _read_bounded_regular(path, label="version source", limit=64 * 1024)
    try:
        tree = ast.parse(data.decode("utf-8"), filename=path.name)
    except (SyntaxError, UnicodeError) as exc:
        raise CleanInstallError("sdist version source is not static UTF-8 Python") from exc
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    if len(body) != 1 or not isinstance(body[0], ast.Assign):
        raise CleanInstallError("sdist version source may only contain a static assignment")
    assignment = body[0]
    if (
        len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.targets[0].id != "__version__"
        or not isinstance(assignment.value, ast.Constant)
        or not isinstance(assignment.value.value, str)
    ):
        raise CleanInstallError("sdist version source has an unsafe assignment")
    return assignment.value.value


def validate_sdist_build_contract(
    source_root: Path,
    artifact: Path,
    *,
    expected_name: str,
    expected_version: str,
) -> None:
    """Validate the complete declarative Hatchling execution boundary before PEP 517 runs."""
    if expected_name != EXPECTED_PROJECT_NAME:
        raise CleanInstallError("reference wheel has an unexpected project name")
    expected_stem = EXPECTED_PROJECT_NAME.replace("-", "_")
    if artifact.name != f"{expected_stem}-{expected_version}.tar.gz":
        raise CleanInstallError("sdist filename does not exactly match the reference wheel")
    if source_root.name != f"{expected_stem}-{expected_version}":
        raise CleanInstallError("sdist root does not exactly match the reference wheel")

    pyproject_path = source_root / "pyproject.toml"
    pyproject_bytes = _read_bounded_regular(
        pyproject_path, label="pyproject.toml", limit=MAX_PYPROJECT_BYTES
    )
    try:
        value = tomllib.loads(pyproject_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CleanInstallError("sdist pyproject.toml is not valid UTF-8 TOML") from exc
    trusted_pyproject = _read_bounded_regular(
        PROJECT_ROOT / "pyproject.toml",
        label="repository pyproject.toml",
        limit=MAX_PYPROJECT_BYTES,
    )
    if pyproject_bytes != trusted_pyproject:
        raise CleanInstallError(
            "sdist pyproject.toml is not byte-bound to the reviewed source tree"
        )

    build_system = value.get("build-system")
    if not isinstance(build_system, dict) or set(build_system) != {"build-backend", "requires"}:
        raise CleanInstallError("sdist build-system must contain only build-backend and requires")
    if build_system.get("build-backend") != "hatchling.build":
        raise CleanInstallError("sdist build backend must be exactly hatchling.build")
    if build_system.get("requires") != ["hatchling==1.31.0"]:
        raise CleanInstallError("sdist build requirements must be exactly hatchling==1.31.0")

    project = value.get("project")
    if not isinstance(project, dict):
        raise CleanInstallError("sdist is missing static project metadata")
    if project.get("name") != expected_name:
        raise CleanInstallError("sdist project name differs from the reference wheel")
    if "version" in project or project.get("dynamic") != ["version"]:
        raise CleanInstallError(
            "sdist project version must use only the reviewed static path source"
        )
    if project.get("requires-python") != EXPECTED_REQUIRES_PYTHON:
        raise CleanInstallError("sdist project Requires-Python differs from the reviewed interval")
    if (
        project.get("readme") != "README.md"
        or project.get("license") != "Apache-2.0"
        or project.get("license-files") != ["LICENSE"]
    ):
        raise CleanInstallError(
            "sdist project readme/license paths differ from the reviewed contract"
        )

    tool = value.get("tool")
    hatch = tool.get("hatch") if isinstance(tool, dict) else None
    if not isinstance(hatch, dict) or set(hatch) != {"build", "version"}:
        raise CleanInstallError("sdist tool.hatch configuration contains an unreviewed entry")
    version_config = hatch.get("version")
    if version_config != {"path": EXPECTED_PROJECT_VERSION_PATH}:
        raise CleanInstallError("sdist Hatch version source differs from the reviewed static path")
    build_config = hatch.get("build")
    if not isinstance(build_config, dict) or set(build_config) != {"targets"}:
        raise CleanInstallError("sdist Hatch build configuration contains an unreviewed entry")
    targets = build_config.get("targets")
    if not isinstance(targets, dict) or set(targets) != {"sdist", "wheel"}:
        raise CleanInstallError("sdist Hatch targets differ from the reviewed contract")
    if targets.get("sdist") != {"include": list(EXPECTED_SDIST_INCLUDES)}:
        raise CleanInstallError("sdist Hatch source include set differs from the reviewed contract")
    if targets.get("wheel") != {"packages": ["src/latex_word_review"]}:
        raise CleanInstallError("sdist Hatch wheel package set differs from the reviewed contract")

    for shadow in (source_root / "hatchling.py", source_root / "hatchling"):
        if shadow.exists() or shadow.is_symlink():
            raise CleanInstallError("sdist may not shadow the locked Hatchling backend")
    version = _static_version(source_root / Path(EXPECTED_PROJECT_VERSION_PATH))
    if version != expected_version:
        raise CleanInstallError("sdist static version differs from the reference wheel")

    pkg_info_bytes = _read_bounded_regular(
        source_root / "PKG-INFO", label="PKG-INFO", limit=MAX_PYPROJECT_BYTES
    )
    pkg_info = BytesParser().parsebytes(pkg_info_bytes)
    expected_headers = {
        "Name": expected_name,
        "Version": expected_version,
        "Requires-Python": EXPECTED_METADATA_REQUIRES_PYTHON,
        "License-Expression": "Apache-2.0",
    }
    for header, expected in expected_headers.items():
        values = pkg_info.get_all(header, [])
        if values != [expected]:
            raise CleanInstallError(f"sdist PKG-INFO {header} differs from the reviewed contract")


def build_sdist_wheel(
    artifact: Path,
    work_root: Path,
    environment: dict[str, str],
    *,
    expected_name: str,
    expected_version: str,
) -> Path:
    source_root = safe_unpack_sdist(artifact, work_root / "sdist-source")
    validate_sdist_build_contract(
        source_root,
        artifact,
        expected_name=expected_name,
        expected_version=expected_version,
    )
    output = work_root / "project-wheel"
    output.mkdir()
    run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(source_root),
        ],
        timeout=300,
        cwd=PROJECT_ROOT,
        env=environment,
    )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file():
        raise CleanInstallError("sdist build did not produce exactly one regular wheel")
    identity = _wheel_identity(wheels[0])
    if identity != (expected_name, expected_version):
        raise CleanInstallError("sdist produced an unexpected project wheel identity")
    return wheels[0]


def _verify_runtime_inventory(
    python: Path,
    identities: dict[str, str],
    environment: dict[str, str],
) -> None:
    code = (
        "import importlib.metadata as m,json,re,sys;"
        "canon=lambda s:re.sub(r'[-_.]+','-',s).lower();"
        "expected=json.loads(sys.argv[1]);"
        "installed={canon(d.metadata['Name']):d.version for d in m.distributions()};"
        "assert all(installed.get(k)==v for k,v in expected.items());"
        "allowed=set(expected)|{'pip','latex-word-review'};"
        "assert set(installed)<=allowed"
    )
    run(
        [str(python), "-c", code, json.dumps(identities, sort_keys=True)],
        timeout=60,
        cwd=PROJECT_ROOT,
        env=environment,
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    dist_dir = _repository_directory(args.dist_dir, label="distribution directory")
    venv_root = _build_child(args.venv, label="clean-install venv")
    demo_output = (
        _build_child(args.public_e0_output, label="public E0 output")
        if args.public_e0_output is not None
        else None
    )
    work_root = _build_child(
        venv_root.with_name(f".{venv_root.name}-clean-install-work"),
        label="clean-install work directory",
    )
    protected = [venv_root, work_root, *([demo_output] if demo_output is not None else [])]
    for index, left in enumerate(protected):
        for right in protected[index + 1 :]:
            if _paths_overlap(left, right):
                raise CleanInstallError("clean-install owned paths must not overlap")
    if any(_paths_overlap(dist_dir, path) for path in protected):
        raise CleanInstallError("distribution directory must not overlap clean-install owned paths")

    artifact = select_artifact(dist_dir, args.artifact)
    if artifact.is_symlink() or not artifact.is_file():
        raise CleanInstallError("distribution artifact must be a regular file")
    if artifact.stat().st_size <= 0:
        raise CleanInstallError("distribution artifact must not be empty")
    for path, label in (
        (venv_root, "clean-install environment"),
        (work_root, "clean-install work directory"),
    ):
        if path.exists() or path.is_symlink():
            raise CleanInstallError(f"{label} already exists")
    if demo_output is not None and (demo_output.exists() or demo_output.is_symlink()):
        raise CleanInstallError("public E0 output already exists")
    if demo_output is not None and (PUBLIC_E0_DEMO.is_symlink() or not PUBLIC_E0_DEMO.is_file()):
        raise CleanInstallError("repository public E0 demo script is missing or not a regular file")

    original_artifact_hash = sha256_file(artifact)
    reference_wheel = select_artifact(dist_dir, "wheel") if args.artifact == "sdist" else None
    if reference_wheel is not None and (
        reference_wheel.is_symlink() or not reference_wheel.is_file()
    ):
        raise CleanInstallError("reference release wheel must be a regular file")
    expected_identity = (
        _wheel_identity(reference_wheel)
        if reference_wheel is not None
        else _wheel_identity(artifact)
    )
    if expected_identity[0] != EXPECTED_PROJECT_NAME:
        raise CleanInstallError("release wheel has an unexpected project name")
    builder_versions = require_locked_builder()
    isolated_environment = _isolated_environment()

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        work_root.mkdir(parents=True, exist_ok=False)
        runtime_requirements, runtime_requirement_count = export_runtime_requirements(
            work_root, isolated_environment
        )
        _, local_wheel_requirements, runtime_identities = build_runtime_wheelhouse(
            runtime_requirements, work_root, isolated_environment
        )
        project_wheel = (
            artifact
            if args.artifact == "wheel"
            else build_sdist_wheel(
                artifact,
                work_root,
                isolated_environment,
                expected_name=expected_identity[0],
                expected_version=expected_identity[1],
            )
        )
        if reference_wheel is not None:
            require_matching_wheel_payload(reference_wheel, project_wheel)

        venv_root.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(venv_root)
        python = environment_python(venv_root)
        script = console_script(venv_root)
        if not python.is_file():
            raise CleanInstallError("fresh venv does not contain a Python executable")
        run(
            [
                str(python),
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--require-hashes",
                "--no-deps",
                "--requirement",
                str(local_wheel_requirements),
            ],
            timeout=600,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        run(
            [
                str(python),
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                "--only-binary=:all:",
                str(project_wheel),
            ],
            timeout=300,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        run(
            [str(python), "-m", "pip", "--isolated", "check"],
            timeout=120,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        _verify_runtime_inventory(python, runtime_identities, isolated_environment)
        run(
            [
                str(python),
                "-c",
                (
                    "from pathlib import Path; import sys; import latex_word_review; "
                    "import importlib.resources as resources; "
                    "import latex_word_review.contracts; "
                    "module = Path(latex_word_review.__file__).resolve(); "
                    "module.relative_to(Path(sys.prefix).resolve()); "
                    "assets = resources.files('latex_word_review').joinpath('assets'); "
                    "expected = {'app.css', 'finalize_review_fields.ps1'}; "
                    "found = {item.name for item in assets.iterdir() if item.is_file()}; "
                    "assert found == expected; "
                    "assert all(assets.joinpath(name).read_bytes() for name in expected); "
                    "assert sys.flags.safe_path"
                ),
            ],
            timeout=60,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        run(
            [str(python), "-m", "latex_word_review", "--version"],
            timeout=60,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        run(
            [str(python), "-m", "latex_word_review", "--help"],
            timeout=60,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        if not script.is_file():
            raise CleanInstallError("distribution did not install the console entry point")
        run(
            [str(script), "--version"],
            timeout=60,
            cwd=PROJECT_ROOT,
            env=isolated_environment,
        )
        if demo_output is not None:
            run(
                [
                    str(python),
                    str(PUBLIC_E0_DEMO),
                    "--fixture-profile",
                    "portable",
                    "--output",
                    str(demo_output),
                    "--skip-verification",
                ],
                timeout=600,
                cwd=PROJECT_ROOT,
                env=isolated_environment,
            )
            summary = demo_output / "demo-summary.json"
            if not summary.is_file():
                raise CleanInstallError("public E0 demo did not produce demo-summary.json")
            value = json.loads(summary.read_text(encoding="utf-8"))
            core_loop = value.get("core_loop") if isinstance(value, dict) else None
            export_contract = value.get("export_contract") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict)
                or value.get("demo_status") != "core_pass"
                or value.get("fixture_profile") != "portable"
                or not isinstance(export_contract, dict)
                or export_contract.get("seq_fields") != 0
                or export_contract.get("ref_fields") != 0
                or export_contract.get("pageref_fields") != 0
                or export_contract.get("live_fields") != 0
                or not isinstance(export_contract.get("exact_mapping_count"), int)
                or export_contract["exact_mapping_count"] < 1
                or not isinstance(core_loop, dict)
                or core_loop.get("status") != "pass"
                or core_loop.get("source_fixture_unchanged") is not True
                or core_loop.get("returned_word_unchanged_after_archive") is not True
                or not isinstance(core_loop.get("accepted"), int)
                or core_loop["accepted"] < 1
            ):
                raise CleanInstallError(
                    "portable public E0 summary did not prove the core contract"
                )
        if sha256_file(artifact) != original_artifact_hash:
            raise CleanInstallError(
                "distribution artifact changed during clean-install verification"
            )
        # Retain all fresh build roots as diagnostic evidence. GitHub-hosted
        # runner teardown, not attacker-influenced path deletion, disposes them.
    except BaseException as exc:
        # The same retention rule applies on failure. A pull-request process
        # must not gain a path-deletion primitive through clean-install cleanup.
        exc.add_note(
            "fresh clean-install build roots were retained for diagnosis; "
            "hosted-runner teardown is the disposal boundary"
        )
        raise

    return {
        "status": "pass",
        "artifact": artifact.name,
        "artifact_sha256": original_artifact_hash,
        "builder": builder_versions,
        "import_origin": "fresh_venv_prefix",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "public_e0_demo": "pass" if demo_output is not None else "not_requested",
        "runtime_lock": "uv.lock-hashes-to-local-wheel-hashes",
        "runtime_requirements": runtime_requirement_count,
        "runtime_wheels": len(runtime_identities),
        "fresh_build_roots": "retained_until_runner_teardown",
        "sdist_build_isolation": "disabled" if args.artifact == "sdist" else "not_applicable",
        "sdist_wheel_payload_match": args.artifact == "sdist",
    }


def main() -> int:
    receipt = execute(parse_args())
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
