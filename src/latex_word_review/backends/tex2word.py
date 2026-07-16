"""Bounded tex2word 1.0.5 adapter using its documented public Python API."""

from __future__ import annotations

import os
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from latex_word_review.canonical import sha256_canonical
from latex_word_review.contracts import load_contract_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import ExportFinding
from latex_word_review.hashing import read_stable_bytes
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

SUPPORTED_TEX2WORD_VERSION = "1.0.5"


def _evidence(label: str) -> str:
    return sha256_canonical({"backend": "tex2word", "version": "1.0.5", "evidence": label})


def _capabilities(version: str | None) -> BackendCapabilities:
    partial = FeatureCapability(
        support="partial",
        representations=("native-docx",),
        diagnostic_codes=(ErrorCode.EXPORT_DEGRADED,),
        evidence_sha256=_evidence("e0-contract"),
    )
    return BackendCapabilities(
        backend_id="tex2word-public-api",
        tool_name="tex2word",
        tool_version=version,
        interface_version="python-api-convert-source-v1",
        distribution="PyPI tex2word==1.0.5",
        configuration_sha256=runtime_configuration_sha256(
            "tex2word",
            {
                "embed_manifest": False,
                "frontend": "pure",
                "citation_mode": "static",
            },
        ),
        features=(
            (
                "body_text",
                FeatureCapability("full", ("w:p", "w:r", "w:t"), evidence_sha256=_evidence("body")),
            ),
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
            (
                "source_spans",
                FeatureCapability(
                    "none",
                    (),
                    diagnostic_codes=(ErrorCode.BACKEND_CAPABILITY_MISSING,),
                    evidence_sha256=_evidence("no-native-source-spans"),
                ),
            ),
        ),
        determinism="verified" if version == SUPPORTED_TEX2WORD_VERSION else "failed",
        tested_contracts=(
            TestedContract(
                fixture="e0-minimal-paper",
                result="pass" if version == SUPPORTED_TEX2WORD_VERSION else "blocked",
                evidence_sha256=_evidence("34p-5omml-1image-1table-9bookmarks"),
            ),
        ),
        limitations=(
            CapabilityLimitation(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "source_spans",
                "conservative normalized-text bookmark anchoring",
            ),
        ),
        security_requirements=(
            "immutable_source_snapshot",
            "explicit_source_root_base_dir",
            "no_shell_escape",
            "atomic_output_publication",
            "post_export_ooxml_inspection",
        ),
    )


class Tex2WordBackend:
    """Wrap the locked API in a hard-timeout child process."""

    def __init__(self) -> None:
        self._version: str | None
        try:
            self._version = metadata.version("tex2word")
        except metadata.PackageNotFoundError:
            self._version = None

    def capabilities(self) -> BackendCapabilities:
        return _capabilities(self._version)

    @staticmethod
    def _prepare_report_path(output_parent: Path, output_name: str) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{output_name}.tex2word-report-",
            suffix=".json",
            dir=output_parent,
        )
        os.close(descriptor)
        path = Path(name)
        path.unlink()
        return path

    @staticmethod
    def _cleanup_report_path(path: Path, output_parent: Path, output_name: str) -> None:
        if path.parent.resolve(strict=True) != output_parent.resolve(strict=True):
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "worker report escaped its parent")
        if not path.name.startswith(f".{output_name}.tex2word-report-") or path.suffix != ".json":
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "refusing to clean unowned report")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT, "worker report cleanup failed"
            ) from exc

    @staticmethod
    def _read_report(path: Path) -> dict[str, object]:
        report = load_contract_json(read_stable_bytes(path, max_bytes=64 * 1024))
        integer_fields = (
            "entry_count",
            "warning_count",
            "error_count",
            "math_omml",
            "math_image",
            "math_raw",
        )
        if set(report) != {*integer_fields, "constructs", "warning_constructs"}:
            raise ContractError(ErrorCode.BACKEND_FAILED, "worker report has unknown fields")
        if any(not isinstance(report[field], int) or report[field] < 0 for field in integer_fields):
            raise ContractError(ErrorCode.BACKEND_FAILED, "worker report has invalid counters")
        for field in ("constructs", "warning_constructs"):
            constructs = report[field]
            if (
                not isinstance(constructs, list)
                or len(constructs) > 256
                or any(not isinstance(item, str) or len(item) > 128 for item in constructs)
            ):
                raise ContractError(
                    ErrorCode.BACKEND_FAILED, "worker report has invalid constructs"
                )
        return report

    def export(self, request: BackendRequest) -> BackendResult:
        capabilities = self.capabilities()
        if self._version != SUPPORTED_TEX2WORD_VERSION:
            return failed_result(
                capabilities,
                code=ErrorCode.TOOL_VERSION_UNSUPPORTED,
                message="the locked tex2word 1.0.5 backend is unavailable",
            )
        prepared = prepare_export(request, owner="tex2word")
        report_path = self._prepare_report_path(
            prepared.output_path.parent,
            prepared.output_path.name,
        )
        native_report: dict[str, object] = {}
        try:
            result = run_command(
                sys.executable,
                (
                    "-m",
                    "latex_word_review.backends._tex2word_worker",
                    "--source",
                    prepared.main_document,
                    "--output",
                    os.fspath(prepared.temporary_path),
                    "--report",
                    os.fspath(report_path),
                ),
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
            if report_path.is_file():
                native_report.update(self._read_report(report_path))
            if result.timed_out or result.output_truncated or result.returncode != 0:
                error_count = native_report.get("error_count", 0)
                silent_loss = isinstance(error_count, int) and error_count > 0
                return failed_result(
                    capabilities,
                    code=(
                        ErrorCode.EXPORT_SILENT_LOSS if silent_loss else ErrorCode.BACKEND_FAILED
                    ),
                    message=(
                        "tex2word conversion timed out"
                        if result.timed_out
                        else "tex2word conversion failed or exceeded its output bound"
                    ),
                    timed_out=result.timed_out,
                    returncode=result.returncode,
                    native_report=native_report,
                )
            if not prepared.temporary_path.is_file() or not report_path.is_file():
                return failed_result(
                    capabilities,
                    message="tex2word returned success without its DOCX and report artifacts",
                    returncode=result.returncode,
                    native_report=native_report,
                )
            digest = publish_stage(prepared)
            warning_constructs = native_report["warning_constructs"]
            assert isinstance(warning_constructs, list)
            findings = tuple(
                ExportFinding(
                    code=ErrorCode.EXPORT_DEGRADED,
                    severity="warning",
                    phase="export",
                    message="tex2word reported a recoverable unsupported construct",
                    recoverable=True,
                    fingerprint=sha256_canonical(
                        {"backend": capabilities.backend_id, "construct": construct}
                    ),
                    remediation="inspect the affected structure before review",
                )
                for construct in warning_constructs
            )
            if native_report["math_raw"]:
                findings += (
                    ExportFinding(
                        code=ErrorCode.EXPORT_DEGRADED,
                        severity="warning",
                        phase="export",
                        message="one or more equations remained raw instead of native OMML",
                        recoverable=True,
                        fingerprint=sha256_canonical(
                            {
                                "backend": capabilities.backend_id,
                                "math_raw": native_report["math_raw"],
                            }
                        ),
                    ),
                )
            return successful_result(
                prepared,
                capabilities,
                digest,
                findings=findings,
                native_report=native_report,
                returncode=result.returncode,
            )
        except (
            ContractError,
            UnicodeDecodeError,
            ImportError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            code = exc.code if isinstance(exc, ContractError) else ErrorCode.BACKEND_FAILED
            return failed_result(
                capabilities,
                code=code,
                message="tex2word conversion failed without publishing an artifact",
                native_report=native_report,
            )
        finally:
            cleanup_stage(prepared)
            self._cleanup_report_path(
                report_path,
                prepared.output_path.parent,
                prepared.output_path.name,
            )


__all__ = ["SUPPORTED_TEX2WORD_VERSION", "Tex2WordBackend"]
