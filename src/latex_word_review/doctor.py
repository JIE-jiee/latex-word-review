"""Read-only environment diagnosis with path-free serialized results."""

from __future__ import annotations

import importlib.metadata
import platform
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.frozen_runtime import is_frozen_application
from latex_word_review.hashing import digest_file
from latex_word_review.runtime import minimal_environment, run_command

_VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,3}(?:[-+._]?[0-9A-Za-z]+)*)")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    candidates: tuple[str, ...]
    version_arguments: tuple[str, ...] = ("--version",)
    required: bool = False
    probe_timeout_s: float = 5.0


@dataclass(frozen=True, slots=True)
class ToolProbe:
    name: str
    required: bool
    status: str
    version: str | None
    executable_sha256: str | None
    output_sha256: str | None
    error_code: str | None

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "version": self.version,
            "executable_sha256": self.executable_sha256,
            "output_sha256": self.output_sha256,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class PackageProbe:
    name: str
    required: bool
    status: str
    version: str | None
    error_code: str | None

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "version": self.version,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    status: str
    os: str
    architecture: str
    python_version: str
    tools: tuple[ToolProbe, ...]
    packages: tuple[PackageProbe, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "platform": {
                "os": self.os,
                "architecture": self.architecture,
                "python_version": self.python_version,
            },
            "tools": [probe.as_dict() for probe in self.tools],
            "packages": [probe.as_dict() for probe in self.packages],
        }


DEFAULT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    # tex2word 1.0.5 exposes no --version option.  Probe its executable with
    # --help and bind the actual release through the required package probe.
    ToolSpec("tex2word", ("tex2word",), version_arguments=("--help",), required=True),
    ToolSpec("pandoc", ("pandoc",)),
    ToolSpec("pandoc-crossref", ("pandoc-crossref",)),
    ToolSpec("latexmk", ("latexmk",)),
    ToolSpec("latexdiff", ("latexdiff",)),
    ToolSpec("xelatex", ("xelatex",)),
    ToolSpec("pdflatex", ("pdflatex",)),
    ToolSpec("lualatex", ("lualatex",)),
    # Standalone Biber distributions unpack a sizeable PAR runtime on their
    # first invocation.  Keep that work in the owned probe directory and give
    # the cold start an explicit, bounded budget.
    ToolSpec("biber", ("biber",), probe_timeout_s=60.0),
    ToolSpec("tectonic", ("tectonic",)),
    ToolSpec("libreoffice", ("soffice", "libreoffice")),
)


def _find_executable(candidates: Sequence[str]) -> Path | None:
    for candidate in candidates:
        candidate_path = Path(candidate)
        if candidate_path.is_file():
            return candidate_path.resolve()
        located = shutil.which(candidate)
        if located:
            return Path(located).resolve()
    return None


def _extract_version(stdout: str, stderr: str) -> str | None:
    match = _VERSION_RE.search(f"{stdout}\n{stderr}")
    return match.group(1) if match else None


def probe_tool(
    spec: ToolSpec,
    *,
    cwd: Path,
    timeout_s: float | None = None,
    max_output_bytes: int = 1024 * 1024,
) -> ToolProbe:
    """Probe one executable without installing, modifying, or reporting its path."""

    executable = _find_executable(spec.candidates)
    if executable is None:
        return ToolProbe(
            spec.name,
            spec.required,
            "missing",
            None,
            None,
            None,
            ErrorCode.TOOL_MISSING.value,
        )
    try:
        resolved_cwd = cwd.resolve(strict=True)
        if not resolved_cwd.is_dir():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "probe cwd must be a directory")
        executable_digest = digest_file(executable, max_bytes=256 * 1024 * 1024).sha256
        with tempfile.TemporaryDirectory(
            prefix="latex-word-review-doctor-",
            ignore_cleanup_errors=True,
        ) as temp_directory:
            probe_root = Path(temp_directory)
            result = run_command(
                executable,
                spec.version_arguments,
                cwd=probe_root,
                timeout_s=spec.probe_timeout_s if timeout_s is None else timeout_s,
                max_output_bytes=max_output_bytes,
                environment=minimal_environment(temp_root=probe_root),
            )
    except (ContractError, OSError) as exc:
        exception_code = (
            exc.code.value if isinstance(exc, ContractError) else ErrorCode.INTERNAL_INVARIANT.value
        )
        return ToolProbe(
            spec.name,
            spec.required,
            "failed",
            None,
            None,
            None,
            exception_code,
        )
    error_code: str | None
    if result.timed_out:
        status = "timed_out"
        error_code = ErrorCode.TOOL_VERSION_UNSUPPORTED.value
    elif result.output_truncated or result.returncode != 0:
        status = "failed"
        error_code = ErrorCode.TOOL_VERSION_UNSUPPORTED.value
    else:
        status = "available"
        error_code = None
    return ToolProbe(
        spec.name,
        spec.required,
        status,
        _extract_version(result.stdout, result.stderr),
        executable_digest,
        result.output_sha256,
        error_code,
    )


def probe_package(name: str, *, required: bool) -> PackageProbe:
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return PackageProbe(
            name,
            required,
            "missing",
            None,
            ErrorCode.TOOL_MISSING.value,
        )
    return PackageProbe(name, required, "available", version, None)


def diagnose_environment(
    *,
    cwd: Path,
    tool_specs: Sequence[ToolSpec] = DEFAULT_TOOL_SPECS,
) -> DoctorReport:
    """Return a deterministic, path-free capability snapshot; never install tools."""

    packages = (
        probe_package("tex2word", required=True),
        probe_package("lxml", required=True),
        probe_package("pypdfium2", required=False),
        probe_package("Pillow", required=False),
    )
    if is_frozen_application():
        embedded_package = packages[0]
        embedded_tex2word = ToolProbe(
            name="tex2word",
            required=True,
            status=embedded_package.status,
            version=embedded_package.version,
            executable_sha256=None,
            output_sha256=None,
            error_code=embedded_package.error_code,
        )
        tools = (
            embedded_tex2word,
            *(probe_tool(spec, cwd=cwd) for spec in tool_specs if spec.name != "tex2word"),
        )
    else:
        tools = tuple(probe_tool(spec, cwd=cwd) for spec in tool_specs)
    required_failed = any(probe.required and probe.status != "available" for probe in tools)
    required_failed = required_failed or any(
        probe.required and probe.status != "available" for probe in packages
    )
    optional_failed = any(not probe.required and probe.status != "available" for probe in tools)
    optional_failed = optional_failed or any(
        not probe.required and probe.status != "available" for probe in packages
    )
    status = "blocked" if required_failed else "degraded" if optional_failed else "pass"
    return DoctorReport(
        status=status,
        os=platform.system().lower() or "unknown",
        architecture=platform.machine().lower() or "unknown",
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        tools=tools,
        packages=packages,
    )


__all__ = [
    "DEFAULT_TOOL_SPECS",
    "DoctorReport",
    "PackageProbe",
    "ToolProbe",
    "ToolSpec",
    "diagnose_environment",
    "probe_package",
    "probe_tool",
]
