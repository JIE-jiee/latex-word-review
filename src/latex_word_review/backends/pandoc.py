"""Pandoc external-process baseline with bounded, shell-free execution."""

from __future__ import annotations

import re
from pathlib import Path

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.runtime import minimal_environment, run_command

from .base import (
    BackendCapabilities,
    BackendRequest,
    BackendResult,
    CapabilityLimitation,
    FeatureCapability,
    TestedContract,
    cleanup_stage,
    failed_result,
    prepare_export,
    publish_stage,
    runtime_configuration_sha256,
    successful_result,
)

_VERSION_RE = re.compile(r"\bpandoc\s+([0-9]+(?:\.[0-9]+){1,3})\b", re.IGNORECASE)


def _evidence(label: str) -> str:
    return sha256_canonical({"backend": "pandoc", "evidence": label})


class PandocBackend:
    """Reference baseline; Pandoc is never used as a revision reader."""

    def __init__(
        self,
        executable: str | Path = "pandoc",
        *,
        executable_arguments: tuple[str, ...] = (),
        version_override: str | None = None,
    ) -> None:
        if any("\x00" in item for item in executable_arguments):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "pandoc argv contains a NUL byte")
        self._executable = executable
        self._executable_arguments = executable_arguments
        self._detected_version = version_override

    def capabilities(self) -> BackendCapabilities:
        version = self._detected_version
        partial = FeatureCapability(
            support="partial",
            representations=("docx",),
            diagnostic_codes=(ErrorCode.EXPORT_DEGRADED,),
            evidence_sha256=_evidence("baseline-contract"),
        )
        none = FeatureCapability(
            support="none",
            representations=(),
            diagnostic_codes=(ErrorCode.BACKEND_CAPABILITY_MISSING,),
            evidence_sha256=_evidence("unsupported"),
        )
        return BackendCapabilities(
            backend_id="pandoc-external-baseline",
            tool_name="pandoc",
            tool_version=version,
            interface_version="cli-latex-to-docx-v1",
            distribution="external executable",
            configuration_sha256=runtime_configuration_sha256(
                "pandoc",
                {
                    "from": "latex",
                    "to": "docx",
                    "standalone": True,
                    "resource_path": ".",
                    "launcher_prefix_sha256": sha256_canonical(list(self._executable_arguments)),
                },
            ),
            features=(
                ("body_text", partial),
                ("revision_view", none),
                ("cjk", partial),
                ("inline_math", partial),
                ("display_math", partial),
                ("omml", partial),
                ("images", partial),
                ("tables", partial),
                ("bookmarks", partial),
                ("live_fields", partial),
                ("labels", partial),
                ("references", partial),
                ("citations", partial),
                ("source_spans", none),
            ),
            determinism="not_verified",
            tested_contracts=(
                TestedContract(
                    fixture="e0-minimal-paper",
                    result="blocked" if version is None else "pass",
                    evidence_sha256=_evidence("e0-local-contract"),
                ),
            ),
            limitations=(
                CapabilityLimitation(
                    ErrorCode.BACKEND_CAPABILITY_MISSING,
                    "revision_round_trip",
                    "comparison baseline only",
                ),
                CapabilityLimitation(
                    ErrorCode.BACKEND_CAPABILITY_MISSING,
                    "source_spans",
                    "conservative normalized-text bookmark anchoring",
                ),
            ),
            security_requirements=(
                "cwd_equals_source_root",
                "fixed_argv_without_shell",
                "bounded_timeout_and_output",
                "utf8_minimal_environment",
                "atomic_output_publication",
                "post_export_ooxml_inspection",
            ),
            external_tools=(("pandoc", version),),
        )

    def _probe_version(self, request: BackendRequest, output_parent: Path) -> BackendResult | None:
        if self._detected_version is not None:
            return None
        try:
            probe = run_command(
                self._executable,
                (*self._executable_arguments, "--version"),
                cwd=request.source_root,
                timeout_s=min(request.timeout_s, 5.0),
                max_output_bytes=min(request.max_output_bytes, 64 * 1024),
                environment=minimal_environment(temp_root=output_parent),
            )
        except ContractError as exc:
            return failed_result(
                self.capabilities(),
                code=exc.code,
                message="the Pandoc baseline executable is unavailable",
            )
        if probe.timed_out or probe.returncode != 0 or probe.output_truncated:
            return failed_result(
                self.capabilities(),
                code=ErrorCode.BACKEND_FAILED,
                message="the Pandoc version probe failed",
                timed_out=probe.timed_out,
                returncode=probe.returncode,
                native_report={"probe_output_sha256": probe.output_sha256},
            )
        match = _VERSION_RE.search(probe.stdout)
        self._detected_version = match.group(1) if match is not None else "unknown"
        return None

    def export(self, request: BackendRequest) -> BackendResult:
        prepared = prepare_export(request, owner="pandoc")
        try:
            if request.revision_view != "source":
                return failed_result(
                    self.capabilities(),
                    code=ErrorCode.BACKEND_CAPABILITY_MISSING,
                    message="the Pandoc baseline supports only the source revision view",
                )

            probe_failure = self._probe_version(request, prepared.output_path.parent)
            if probe_failure is not None:
                return probe_failure
            capabilities = self.capabilities()
            arguments = (
                *self._executable_arguments,
                prepared.main_document,
                "--from=latex",
                "--to=docx",
                "--standalone",
                f"--output={prepared.temporary_path}",
                "--resource-path=.",
            )
            result = run_command(
                self._executable,
                arguments,
                cwd=prepared.source_root,
                timeout_s=request.timeout_s,
                max_output_bytes=request.max_output_bytes,
                environment=minimal_environment(temp_root=prepared.output_path.parent),
            )
            native_report = {
                "duration_ms": result.duration_ms,
                "output_sha256": result.output_sha256,
                "output_truncated": result.output_truncated,
            }
            if result.timed_out or result.returncode != 0 or result.output_truncated:
                return failed_result(
                    capabilities,
                    code=ErrorCode.BACKEND_FAILED,
                    message=(
                        "Pandoc conversion timed out"
                        if result.timed_out
                        else "Pandoc conversion failed or exceeded its output bound"
                    ),
                    timed_out=result.timed_out,
                    returncode=result.returncode,
                    native_report=native_report,
                )
            if not prepared.temporary_path.is_file():
                return failed_result(
                    capabilities,
                    message="Pandoc returned success without a DOCX artifact",
                    returncode=result.returncode,
                    native_report=native_report,
                )
            digest = publish_stage(prepared)
            return successful_result(
                prepared,
                capabilities,
                digest,
                native_report=native_report,
                returncode=result.returncode,
            )
        except ContractError as exc:
            return failed_result(
                self.capabilities(),
                code=exc.code,
                message="Pandoc failed without publishing an artifact",
            )
        finally:
            cleanup_stage(prepared)


__all__ = ["PandocBackend"]
