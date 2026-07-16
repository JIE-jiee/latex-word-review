"""Fail-closed LaTeX compilation, latexdiff generation and verification.

The verifier never runs tools against authoritative inputs.  It first proves
the complete SourceManifest -> ChangeSet -> ApprovalSet -> PatchPlan binding,
copies the allowlisted source files into a private staging tree, and only then
invokes external tools with bounded, shell-free commands.
"""

from __future__ import annotations

import copy
import difflib
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from latex_word_review.__about__ import __version__
from latex_word_review.canonical import (
    canonical_json,
    sha256_bytes,
    sha256_canonical,
)
from latex_word_review.contracts import compute_source_tree_sha256, make_envelope, validate_contract
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, digest_bytes, digest_file, read_stable_bytes
from latex_word_review.ids import derive_artifact_id, stable_id
from latex_word_review.paths import ensure_disjoint_roots, resolve_within, validate_relative_path
from latex_word_review.runtime import CommandResult, minimal_environment, run_command

_INTERFACE_VERSION: Final = "verification-v1alpha1"
_RESERVED_SOURCE_METADATA: Final = {
    ".latex-word-review-applied.json",
    ".latex-word-review-plan.json",
    "snapshot-manifest.json",
}
_SHELL_ESCAPE_PATTERNS: Final = (
    re.compile(rb"\\(?:immediate\s*)?write18\b", re.IGNORECASE),
    re.compile(rb"\\input\s*\|", re.IGNORECASE),
    re.compile(rb"\\(?:usepackage|RequirePackage)(?:\[[^]]*\])?\{minted\}", re.IGNORECASE),
    re.compile(rb"\\begin\{minted\}", re.IGNORECASE),
)
_WINDOWS_ABSOLUTE_RE: Final = re.compile(r"(?<![\w.])[A-Za-z]:[\\/][^\s\"'<>]*")
_POSIX_ABSOLUTE_RE: Final = re.compile(r"(?<![\w.])/(?:[^\s\"'<>]+)")
_MIKTEX_ROOT_ENVIRONMENT: Final = (
    "MIKTEX_USERCONFIG",
    "MIKTEX_USERDATA",
    "MIKTEX_USERINSTALL",
)

VerificationStatus = Literal["pass", "fail", "blocked"]
CommandStatus = Literal["pass", "fail", "blocked", "not_run"]


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """Bounded external-tool policy included in the report's authorization hash."""

    timeout_s: float = 60.0
    max_output_bytes: int = 4 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    require_latexdiff: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.timeout_s <= 60:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "verification timeout must be in (0, 60]")
        if not 0 < self.max_output_bytes <= 16 * 1024 * 1024:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "verification output limit is invalid")
        if not 0 < self.max_file_bytes <= 512 * 1024 * 1024:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "verification file limit is invalid")

    def as_dict(self) -> dict[str, int | float | bool]:
        return {
            "timeout_s": self.timeout_s,
            "max_output_bytes": self.max_output_bytes,
            "max_file_bytes": self.max_file_bytes,
            "require_latexdiff": self.require_latexdiff,
        }

    @property
    def payload_sha256(self) -> str:
        return sha256_canonical(self.as_dict())


DEFAULT_VERIFICATION_POLICY: Final = VerificationPolicy()


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Published verification report and content hashes."""

    report: dict[str, Any]
    output_root: Path
    revised_source_tree_sha256: str
    status: VerificationStatus


@dataclass(frozen=True, slots=True)
class _SourceTree:
    files: dict[str, bytes]
    roles: dict[str, str]
    records: tuple[dict[str, Any], ...]
    tree_sha256: str


@dataclass(frozen=True, slots=True)
class _ToolRun:
    name: str
    status: CommandStatus
    result: CommandResult | None
    error_code: str | None

    def extension_record(self) -> dict[str, Any]:
        result = self.result
        return {
            "name": self.name,
            "status": self.status,
            "exit_code": None if result is None else result.returncode,
            "timed_out": False if result is None else result.timed_out,
            "output_truncated": False if result is None else result.output_truncated,
            "output_sha256": None if result is None else result.output_sha256,
            "error_code": self.error_code,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _require_time(value: str | None) -> str:
    resolved = value or _utc_now()
    try:
        parsed = datetime.fromisoformat(
            resolved[:-1] + "+00:00" if resolved.endswith("Z") else resolved
        )
    except ValueError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "verification time is not ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "verification time needs a UTC offset")
    return resolved


def _require_schema(document: Mapping[str, Any], expected: str) -> str:
    receipt = validate_contract(document)
    if receipt.schema_name != expected:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"expected {expected}",
            path=("schema_name",),
        )
    return receipt.payload_sha256


def _require_equal(
    expected: object,
    actual: object,
    *,
    code: ErrorCode,
    label: str,
) -> None:
    if expected != actual:
        raise ContractError(
            code,
            f"{label} binding does not match",
            details={"expected": expected, "actual": actual},
        )


def _validate_bindings(
    source_manifest: Mapping[str, Any],
    changeset: Mapping[str, Any],
    approval_set: Mapping[str, Any],
    patch_plan: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    source_sha = _require_schema(source_manifest, "SourceManifest")
    changeset_sha = _require_schema(changeset, "ChangeSet")
    approval_sha = _require_schema(approval_set, "ApprovalSet")
    plan_sha = _require_schema(patch_plan, "PatchPlan")
    run_ids = {
        cast("str", source_manifest["run_id"]),
        cast("str", changeset["run_id"]),
        cast("str", approval_set["run_id"]),
        cast("str", patch_plan["run_id"]),
    }
    if len(run_ids) != 1:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "workflow run binding differs")
    run_id = next(iter(run_ids))

    source_payload = cast("Mapping[str, Any]", source_manifest["payload"])
    change_payload = cast("Mapping[str, Any]", changeset["payload"])
    approval_payload = cast("Mapping[str, Any]", approval_set["payload"])
    plan_payload = cast("Mapping[str, Any]", patch_plan["payload"])
    _require_equal(
        source_sha,
        change_payload["source_manifest_sha256"],
        code=ErrorCode.HASH_SOURCE_MISMATCH,
        label="ChangeSet source manifest",
    )
    _require_equal(
        source_sha,
        approval_payload["source_manifest_sha256"],
        code=ErrorCode.HASH_APPROVAL_MISMATCH,
        label="ApprovalSet source manifest",
    )
    _require_equal(
        changeset_sha,
        approval_payload["changeset_sha256"],
        code=ErrorCode.HASH_APPROVAL_MISMATCH,
        label="ApprovalSet ChangeSet",
    )
    if approval_payload["status"] != "final":
        raise ContractError(ErrorCode.APPROVAL_NOT_FINAL, "verification needs a final ApprovalSet")
    for field, expected in (
        ("source_manifest_sha256", source_sha),
        ("source_tree_sha256", source_payload["source_tree_sha256"]),
        ("changeset_sha256", changeset_sha),
        ("approval_set_sha256", approval_sha),
    ):
        _require_equal(
            expected,
            plan_payload[field],
            code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
            label=f"PatchPlan {field}",
        )
    if plan_payload["status"] == "blocked":
        raise ContractError(
            ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED,
            "a blocked PatchPlan cannot be verified as an applied revision",
        )
    _validate_plan_approval_partition(change_payload, approval_payload, plan_payload)
    return run_id, source_sha, changeset_sha, approval_sha, plan_sha


def _validate_plan_approval_partition(
    change_payload: Mapping[str, Any],
    approval_payload: Mapping[str, Any],
    plan_payload: Mapping[str, Any],
) -> None:
    changes = {
        cast("str", item["change_id"]): item
        for item in cast("Sequence[Mapping[str, Any]]", change_payload["changes"])
    }
    decisions = {
        cast("str", item["change_id"]): item
        for item in cast("Sequence[Mapping[str, Any]]", approval_payload["decisions"])
    }
    if set(decisions) != set(changes):
        raise ContractError(
            ErrorCode.HASH_APPROVAL_MISMATCH,
            "final ApprovalSet does not cover the exact ChangeSet",
        )
    for change_id, decision in decisions.items():
        _require_equal(
            changes[change_id]["change_fingerprint"],
            decision["change_fingerprint"],
            code=ErrorCode.HASH_APPROVAL_MISMATCH,
            label="approval change fingerprint",
        )
    operations = cast("Sequence[Mapping[str, Any]]", plan_payload["operations"])
    operation_ids = {cast("str", item["change_id"]) for item in operations}
    accepted_ids = {
        change_id
        for change_id, decision in decisions.items()
        if decision["decision"] in {"accepted", "accepted_with_edit"}
    }
    if operation_ids != accepted_ids:
        raise ContractError(
            ErrorCode.PATCH_UNAPPROVED_CHANGE,
            "PatchPlan operations are not the exact set of accepted decisions",
            details={
                "missing": sorted(accepted_ids - operation_ids),
                "extra": sorted(operation_ids - accepted_ids),
            },
        )
    for operation in operations:
        change_id = cast("str", operation["change_id"])
        decision = decisions[change_id]
        change = changes[change_id]
        resolution = cast("Mapping[str, Any]", change["resolution"])
        if (
            change["safety_class"] != "plain_text_candidate"
            or change["kind"] not in {"insertion", "deletion", "replacement"}
            or resolution["status"] != "exact"
            or cast("float", resolution["confidence"]) < 0.99
            or resolution["candidates"]
        ):
            raise ContractError(
                ErrorCode.PATCH_UNSAFE_KIND,
                "PatchPlan contains an operation outside the v0.1 safe subset",
            )
        _require_equal(
            change["source_location"],
            operation["target"],
            code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
            label="operation source location",
        )
        _require_equal(
            change["unit_id"],
            operation["unit_id"],
            code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
            label="operation source unit",
        )
        _require_equal(
            change["before"],
            operation["before_text"],
            code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
            label="operation original text",
        )
        if decision["decision"] == "accepted_with_edit":
            replacement = decision["final_text"]
        else:
            replacement = change["after"]
        _require_equal(
            replacement,
            operation["replacement_text"],
            code=ErrorCode.PATCH_UNAPPROVED_CHANGE,
            label="approved replacement text",
        )


def _read_original_tree(
    root: Path,
    source_manifest: Mapping[str, Any],
    *,
    max_file_bytes: int,
) -> _SourceTree:
    payload = cast("Mapping[str, Any]", source_manifest["payload"])
    manifest_files = cast("Sequence[Mapping[str, Any]]", payload["files"])
    files: dict[str, bytes] = {}
    roles: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for item in manifest_files:
        path = validate_relative_path(cast("str", item["path"]))
        data = read_stable_bytes(resolve_within(root, path), max_bytes=max_file_bytes)
        digest = digest_bytes(data)
        expected = FileDigest(cast("int", item["size_bytes"]), cast("str", item["sha256"]))
        if digest != expected:
            raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source file digest drifted")
        files[path] = data
        roles[path] = cast("str", item["role"])
        records.append(
            {
                "path": path,
                "role": item["role"],
                "size_bytes": digest.size_bytes,
                "sha256": digest.sha256,
            }
        )
    tree_sha = compute_source_tree_sha256(records)
    _require_equal(
        payload["source_tree_sha256"],
        tree_sha,
        code=ErrorCode.HASH_SOURCE_MISMATCH,
        label="source tree",
    )
    return _SourceTree(files, roles, tuple(records), tree_sha)


def _read_revised_tree(
    root: Path,
    original: _SourceTree,
    *,
    max_file_bytes: int,
) -> _SourceTree:
    files: dict[str, bytes] = {}
    records: list[dict[str, Any]] = []
    for path in sorted(original.files):
        try:
            resolved = resolve_within(root, path)
        except ContractError as exc:
            raise ContractError(
                ErrorCode.VERIFY_DIFF_MISMATCH,
                "revised source is missing a manifest file",
                details={"path": path},
            ) from exc
        data = read_stable_bytes(resolved, max_bytes=max_file_bytes)
        digest = digest_bytes(data)
        files[path] = data
        records.append(
            {
                "path": path,
                "role": original.roles[path],
                "size_bytes": digest.size_bytes,
                "sha256": digest.sha256,
            }
        )
    _reject_revised_extras(root, set(files))
    return _SourceTree(
        files,
        dict(original.roles),
        tuple(records),
        compute_source_tree_sha256(records),
    )


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def _reject_revised_extras(root: Path, allowlisted: set[str]) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "revised source root is unavailable") from exc
    for candidate in resolved_root.rglob("*"):
        if _is_link_or_junction(candidate):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "revised source contains a link")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "revised source contains a non-file")
        relative = validate_relative_path(candidate.relative_to(resolved_root).as_posix())
        if relative not in allowlisted and relative not in _RESERVED_SOURCE_METADATA:
            raise ContractError(
                ErrorCode.VERIFY_DIFF_MISMATCH,
                "revised source contains a file outside the source manifest",
                details={"path": relative},
            )


def _apply_plan(original: _SourceTree, patch_plan: Mapping[str, Any]) -> dict[str, bytes]:
    payload = cast("Mapping[str, Any]", patch_plan["payload"])
    operations = cast("Sequence[Mapping[str, Any]]", payload["operations"])
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for operation in operations:
        target = cast("Mapping[str, Any]", operation["target"])
        path = validate_relative_path(cast("str", target["path"]))
        if path not in original.files or original.roles[path] != "tex":
            raise ContractError(ErrorCode.PATCH_UNSAFE_KIND, "patch targets a non-TeX file")
        _require_equal(
            digest_bytes(original.files[path]).sha256,
            operation["target_file_sha256"],
            code=ErrorCode.PATCH_SOURCE_DRIFT,
            label="operation target file",
        )
        grouped.setdefault(path, []).append(operation)

    expected = dict(original.files)
    for path, file_operations in grouped.items():
        data = expected[path]
        ordered = sorted(
            file_operations,
            key=lambda item: (
                cast("Mapping[str, Any]", item["target"])["start_byte"],
                cast("Mapping[str, Any]", item["target"])["end_byte"],
            ),
            reverse=True,
        )
        prior_start: int | None = None
        for operation in ordered:
            target = cast("Mapping[str, Any]", operation["target"])
            start = cast("int", target["start_byte"])
            end = cast("int", target["end_byte"])
            if not 0 <= start <= end <= len(data) or (
                prior_start is not None and end > prior_start
            ):
                raise ContractError(ErrorCode.PATCH_OVERLAP, "patch operation ranges overlap")
            before = data[start:end]
            _require_equal(
                sha256_bytes(before),
                target["slice_sha256"],
                code=ErrorCode.PATCH_SOURCE_DRIFT,
                label="operation target slice",
            )
            _require_equal(
                sha256_bytes(before),
                operation["expected_bytes_sha256"],
                code=ErrorCode.PATCH_SOURCE_DRIFT,
                label="operation expected bytes",
            )
            try:
                before_text = before.decode("utf-8")
                replacement = cast("str", operation["replacement_text"]).encode("utf-8")
            except (UnicodeError, AttributeError) as exc:
                raise ContractError(ErrorCode.PATCH_UNSAFE_KIND, "patch text is not UTF-8") from exc
            _require_equal(
                before_text,
                operation["before_text"],
                code=ErrorCode.PATCH_SOURCE_DRIFT,
                label="operation before text",
            )
            kind = operation["kind"]
            if (
                (kind == "insert" and start != end)
                or (kind == "delete" and (start == end or replacement))
                or (kind == "replace" and start == end)
            ):
                raise ContractError(
                    ErrorCode.HASH_PATCHPLAN_MISMATCH,
                    "patch kind does not match its byte span and replacement",
                )
            data = data[:start] + replacement + data[end:]
            prior_start = start
        expected[path] = data
    return expected


def _unified_diff(original: _SourceTree, revised: Mapping[str, bytes]) -> bytes:
    chunks: list[str] = []
    for path in sorted(original.files):
        before = original.files[path]
        after = revised[path]
        if before == after:
            continue
        try:
            before_lines = before.decode("utf-8").splitlines(keepends=True)
            after_lines = after.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise ContractError(
                ErrorCode.VERIFY_DIFF_MISMATCH,
                "a changed source file is not UTF-8 text",
            ) from exc
        chunks.extend(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="\n",
            )
        )
    return "".join(chunks).encode("utf-8")


def _reconcile(
    original: _SourceTree,
    revised: _SourceTree,
    patch_plan: Mapping[str, Any],
) -> tuple[bytes, dict[str, int]]:
    expected = _apply_plan(original, patch_plan)
    mismatches = [path for path in sorted(expected) if expected[path] != revised.files[path]]
    if mismatches:
        raise ContractError(
            ErrorCode.VERIFY_DIFF_MISMATCH,
            "revised source differs from the approved PatchPlan operations",
            details={"paths": mismatches},
        )
    diff = _unified_diff(original, revised.files)
    plan_payload = cast("Mapping[str, Any]", patch_plan["payload"])
    operations = cast("Sequence[Mapping[str, Any]]", plan_payload["operations"])
    planned_diff = plan_payload["unified_diff"]
    if operations:
        if not isinstance(planned_diff, Mapping):
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "PatchPlan lacks its diff")
        _require_equal(
            planned_diff["sha256"],
            sha256_bytes(diff),
            code=ErrorCode.VERIFY_DIFF_MISMATCH,
            label="actual diff",
        )
        _require_equal(
            planned_diff["size_bytes"],
            len(diff),
            code=ErrorCode.VERIFY_DIFF_MISMATCH,
            label="actual diff size",
        )
    elif diff:
        raise ContractError(ErrorCode.VERIFY_DIFF_MISMATCH, "noop plan produced an actual diff")
    count = len(operations)
    return diff, {
        "planned_count": count,
        "applied_count": count,
        "missing_count": 0,
        "extra_count": 0,
        "duplicate_count": 0,
        "unapproved_modification_count": 0,
    }


def _reject_shell_escape_sources(tree: _SourceTree) -> None:
    for path, data in tree.files.items():
        if tree.roles[path] != "tex":
            continue
        if any(pattern.search(data) is not None for pattern in _SHELL_ESCAPE_PATTERNS):
            raise ContractError(
                ErrorCode.VERIFY_COMPILE_FAILED,
                "source requests a shell-escape dependent feature",
                details={"path": path},
            )


def _latexmk_mode(source_manifest: Mapping[str, Any]) -> str:
    payload = cast("Mapping[str, Any]", source_manifest["payload"])
    hints = cast("Sequence[Mapping[str, Any]]", payload["engine_hints"])
    engines = {
        cast("str", hint["engine"]).casefold()
        for hint in hints
        if cast("str", hint["engine"]).casefold() in {"pdflatex", "xelatex", "lualatex"}
    }
    if len(engines) > 1:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source has conflicting TeX engine hints")
    engine = next(iter(engines), "pdflatex")
    return {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}[engine]


def _miktex_latexmk_arguments(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Disable MiKTeX package prompts for explicitly isolated MiKTeX roots."""

    if all(environment.get(name) for name in _MIKTEX_ROOT_ENVIRONMENT):
        return ("-disable-installer",)
    return ()


def _write_output_file(path: Path, data: bytes) -> None:
    """Write one staged file exclusively; kept small for fault-injection tests."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(
            ErrorCode.APPLY_PARTIAL_WRITE, "verification staging write failed"
        ) from exc


def _copy_tree_bytes(destination: Path, tree: _SourceTree) -> None:
    for path in sorted(tree.files):
        target = destination.joinpath(*path.split("/"))
        _write_output_file(target, tree.files[path])


def _tool_run(
    name: str,
    executable: str | Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    policy: VerificationPolicy,
) -> _ToolRun:
    if any("\x00" in argument for argument in arguments):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "tool argv contains a NUL byte")
    try:
        result = run_command(
            executable,
            tuple(arguments),
            cwd=cwd,
            timeout_s=policy.timeout_s,
            max_output_bytes=policy.max_output_bytes,
            environment=environment,
        )
    except ContractError as exc:
        if exc.code != ErrorCode.TOOL_MISSING:
            raise
        return _ToolRun(name, "blocked", None, exc.code.value)
    status: CommandStatus = "pass"
    if result.timed_out or result.output_truncated or result.returncode != 0:
        status = "fail"
    return _ToolRun(name, status, result, None)


def _sanitize_log_text(value: str, sensitive_roots: Sequence[Path]) -> str:
    sanitized = value.replace("\x00", "�")
    for root in sensitive_roots:
        variants = {os.fspath(root), os.fspath(root).replace("\\", "/")}
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                sanitized = sanitized.replace(variant, "<workspace>")
    sanitized = _WINDOWS_ABSOLUTE_RE.sub("<absolute-path>", sanitized)
    sanitized = _POSIX_ABSOLUTE_RE.sub("<absolute-path>", sanitized)
    return sanitized


def _command_log(run: _ToolRun, sensitive_roots: Sequence[Path]) -> bytes:
    result = run.result
    payload: dict[str, Any] = run.extension_record()
    payload["stdout"] = "" if result is None else _sanitize_log_text(result.stdout, sensitive_roots)
    payload["stderr"] = "" if result is None else _sanitize_log_text(result.stderr, sensitive_roots)
    return canonical_json(payload) + b"\n"


def _artifact(
    path: str,
    data: bytes,
    *,
    role: str,
    media_type: str,
) -> dict[str, Any]:
    normalized = validate_relative_path(path)
    digest = digest_bytes(data)
    return {
        "artifact_id": derive_artifact_id(digest.sha256),
        "path": normalized,
        "path_base": "run_root",
        "role": role,
        "media_type": media_type,
        "size_bytes": digest.size_bytes,
        "sha256": digest.sha256,
        "immutable": True,
        "confidentiality": "derived_private",
    }


def _tool_identity(name: str, policy: VerificationPolicy) -> dict[str, Any]:
    return {
        "name": name,
        "version": None,
        "interface_version": _INTERFACE_VERSION,
        "distribution": "external executable",
        "executable_sha256": None,
        "configuration_sha256": policy.payload_sha256,
    }


def _project_tool(policy: VerificationPolicy) -> dict[str, Any]:
    return {
        "name": "latex-word-review",
        "version": __version__,
        "interface_version": _INTERFACE_VERSION,
        "distribution": "latex-word-review",
        "executable_sha256": None,
        "configuration_sha256": policy.payload_sha256,
    }


def _overall_status(runs: Sequence[_ToolRun], *, require_latexdiff: bool) -> VerificationStatus:
    required = list(runs if require_latexdiff else runs[:1])
    if any(run.status == "blocked" for run in required):
        return "blocked"
    if any(run.status != "pass" for run in required):
        return "fail"
    return "pass"


def _compile_checks(log_text: str, compile_status: CommandStatus) -> tuple[str, str]:
    if compile_status != "pass":
        return compile_status, compile_status
    lowered = log_text.casefold()
    reference_failure = any(
        marker in lowered
        for marker in (
            "undefined references",
            "citation `",
            "there were undefined",
            "rerun to get cross-references right",
        )
    )
    return ("fail" if reference_failure else "pass"), "pass"


def _final_compile_log_text(
    build_root: Path,
    main_document: str,
    run: _ToolRun,
    *,
    max_file_bytes: int,
) -> str:
    """Prefer the final TeX log over latexmk's multi-pass transcript.

    A successful latexmk transcript intentionally contains warnings from early
    passes that were resolved later.  Treating the whole transcript as the
    final reference state produces false failures.  Test doubles may not emit a
    TeX log, so the bounded command output remains the conservative fallback.
    """

    final_log = build_root / f"{Path(main_document).stem}.log"
    if final_log.is_file():
        return read_stable_bytes(final_log, max_bytes=max_file_bytes).decode(
            "utf-8", errors="replace"
        )
    if run.result is None:
        return ""
    return f"{run.result.stdout}\n{run.result.stderr}"


def _remove_owned_tree(path: Path, parent: Path, prefix: str) -> None:
    if not path.name.startswith(prefix):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "refusing to clean an unowned tree")
    if path.resolve(strict=False).parent != parent.resolve(strict=True):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "verification stage escaped its parent")
    with suppress(FileNotFoundError):
        shutil.rmtree(path)


def verify_latex_project(
    original_source_root: Path,
    revised_source_root: Path,
    returned_original: Path,
    source_manifest: Mapping[str, Any],
    changeset: Mapping[str, Any],
    approval_set: Mapping[str, Any],
    patch_plan: Mapping[str, Any],
    output_root: Path,
    *,
    latexmk_executable: str | Path = "latexmk",
    latexdiff_executable: str | Path = "latexdiff",
    policy: VerificationPolicy = DEFAULT_VERIFICATION_POLICY,
    generated_at: str | None = None,
) -> VerificationResult:
    """Verify an applied tree and atomically publish clean/diff deliverables.

    Tool absence is represented as a ``blocked`` VerificationReport.  Binding,
    source drift, path and write-safety failures raise :class:`ContractError`
    and publish nothing.
    """

    timestamp = _require_time(generated_at)
    run_id, source_sha, changeset_sha, approval_sha, plan_sha = _validate_bindings(
        source_manifest, changeset, approval_set, patch_plan
    )
    original_root, target = ensure_disjoint_roots(original_source_root, output_root)
    revised_root, second_target = ensure_disjoint_roots(revised_source_root, output_root)
    if target != second_target:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "output root resolved inconsistently")
    ensure_disjoint_roots(original_root, revised_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "verification output already exists")

    original_pre = _read_original_tree(
        original_root, source_manifest, max_file_bytes=policy.max_file_bytes
    )
    revised_pre = _read_revised_tree(
        revised_root, original_pre, max_file_bytes=policy.max_file_bytes
    )
    _reject_shell_escape_sources(original_pre)
    _reject_shell_escape_sources(revised_pre)
    actual_diff, reconciliation = _reconcile(original_pre, revised_pre, patch_plan)

    returned_expected = cast(
        "str", cast("Mapping[str, Any]", changeset["payload"])["returned_original"]["sha256"]
    )
    if _is_link_or_junction(returned_original):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "returned Word original is a link")
    returned_data = read_stable_bytes(returned_original, max_bytes=policy.max_file_bytes)
    returned_pre = digest_bytes(returned_data)
    returned_artifact = cast(
        "Mapping[str, Any]", cast("Mapping[str, Any]", changeset["payload"])["returned_original"]
    )
    expected_returned = FileDigest(cast("int", returned_artifact["size_bytes"]), returned_expected)
    if returned_pre != expected_returned:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned Word original binding does not match",
        )

    prefix = f".{target.name}.verify-"
    temporary = Path(tempfile.mkdtemp(prefix=prefix, dir=target.parent))
    try:
        work = temporary / "_work"
        original_work = work / "original"
        revised_work = work / "revised"
        revised_compile = work / "revised-compile"
        diff_compile = work / "diff-compile"
        for directory in (original_work, revised_work, revised_compile, diff_compile):
            directory.mkdir(parents=True, exist_ok=False)
        _copy_tree_bytes(original_work, original_pre)
        _copy_tree_bytes(revised_work, revised_pre)
        _copy_tree_bytes(revised_compile, revised_pre)
        _copy_tree_bytes(diff_compile, revised_pre)
        _write_output_file(work / "returned-original.docx", returned_data)
        contract_work = work / "contracts"
        for name, document in (
            ("source-manifest.json", source_manifest),
            ("changeset.json", changeset),
            ("approval-set.json", approval_set),
            ("patch-plan.json", patch_plan),
        ):
            _write_output_file(contract_work / name, canonical_json(document) + b"\n")

        source_payload = cast("Mapping[str, Any]", source_manifest["payload"])
        main_document = validate_relative_path(cast("str", source_payload["main_document"]))
        safe_main_argument = f"./{main_document}"
        latexmk_mode = _latexmk_mode(source_manifest)
        build_root = work / "build"
        build_root.mkdir(parents=True, exist_ok=False)
        revised_build = build_root / "revised"
        diff_build = build_root / "latexdiff"
        revised_build.mkdir()
        diff_build.mkdir()
        tex_environment = {"openin_any": "p", "openout_any": "p", "shell_escape": "0"}
        tex_environment.update(
            {name: value for name in _MIKTEX_ROOT_ENVIRONMENT if (value := os.environ.get(name))}
        )
        environment = minimal_environment(temp_root=work, additions=tex_environment)
        miktex_latexmk_arguments = _miktex_latexmk_arguments(environment)

        revised_run = _tool_run(
            "latexmk-revised",
            latexmk_executable,
            (
                latexmk_mode,
                *miktex_latexmk_arguments,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "-no-shell-escape",
                "-outdir=../build/revised",
                safe_main_argument,
            ),
            cwd=revised_compile,
            environment=environment,
            policy=policy,
        )

        latexdiff_run = _tool_run(
            "latexdiff-generate",
            latexdiff_executable,
            (
                "--flatten",
                "--encoding=utf8",
                f"original/{main_document}",
                f"revised/{main_document}",
            ),
            cwd=work,
            environment=environment,
            policy=policy,
        )
        latexdiff_data: bytes | None = None
        if latexdiff_run.status == "pass" and latexdiff_run.result is not None:
            candidate_diff = latexdiff_run.result.stdout.encode("utf-8")
            if not candidate_diff or any(
                pattern.search(candidate_diff) is not None for pattern in _SHELL_ESCAPE_PATTERNS
            ):
                latexdiff_run = _ToolRun("latexdiff-generate", "fail", latexdiff_run.result, None)
                diff_compile_run = _ToolRun("latexmk-latexdiff", "not_run", None, None)
            else:
                latexdiff_data = candidate_diff
                _write_output_file(diff_compile / "latexdiff.tex", latexdiff_data)
                diff_compile_run = _tool_run(
                    "latexmk-latexdiff",
                    latexmk_executable,
                    (
                        latexmk_mode,
                        *miktex_latexmk_arguments,
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-file-line-error",
                        "-no-shell-escape",
                        "-outdir=../build/latexdiff",
                        "./latexdiff.tex",
                    ),
                    cwd=diff_compile,
                    environment=environment,
                    policy=policy,
                )
        else:
            diff_compile_run = _ToolRun("latexmk-latexdiff", "not_run", None, None)

        revised_pdf = revised_build / f"{Path(main_document).stem}.pdf"
        if revised_run.status == "pass" and not revised_pdf.is_file():
            revised_run = _ToolRun("latexmk-revised", "fail", revised_run.result, None)
        diff_pdf = diff_build / "latexdiff.pdf"
        if diff_compile_run.status == "pass" and not diff_pdf.is_file():
            diff_compile_run = _ToolRun("latexmk-latexdiff", "fail", diff_compile_run.result, None)
        runs = (revised_run, latexdiff_run, diff_compile_run)
        status = _overall_status(runs, require_latexdiff=policy.require_latexdiff)
        sensitive_roots = (
            original_root,
            revised_root,
            returned_original.parent,
            temporary,
        )
        logs: dict[str, bytes] = {
            "logs/revised-compile.json": _command_log(revised_run, sensitive_roots),
            "logs/latexdiff-generate.json": _command_log(latexdiff_run, sensitive_roots),
            "logs/latexdiff-compile.json": _command_log(diff_compile_run, sensitive_roots),
        }
        for path, data in logs.items():
            _write_output_file(temporary.joinpath(*path.split("/")), data)

        _write_output_file(temporary / "actual.diff", actual_diff)
        _copy_tree_bytes(temporary / "revised-clean", revised_pre)

        deliverables: list[dict[str, Any]] = []
        actual_diff_ref = _artifact(
            "actual.diff", actual_diff, role="actual_diff", media_type="text/x-diff"
        )
        deliverables.append(actual_diff_ref)
        if revised_run.status == "pass" and revised_pdf.is_file():
            revised_pdf_data = read_stable_bytes(revised_pdf, max_bytes=policy.max_file_bytes)
            _write_output_file(temporary / "revised-clean.pdf", revised_pdf_data)
            deliverables.append(
                _artifact(
                    "revised-clean.pdf",
                    revised_pdf_data,
                    role="revised_clean_pdf",
                    media_type="application/pdf",
                )
            )

        latexdiff_tex_ref: dict[str, Any] | None = None
        latexdiff_pdf_ref: dict[str, Any] | None = None
        if latexdiff_data is not None:
            _write_output_file(temporary / "latexdiff.tex", latexdiff_data)
            latexdiff_tex_ref = _artifact(
                "latexdiff.tex",
                latexdiff_data,
                role="latexdiff_tex",
                media_type="application/x-tex",
            )
            deliverables.append(latexdiff_tex_ref)
        if diff_compile_run.status == "pass" and diff_pdf.is_file():
            diff_pdf_data = read_stable_bytes(diff_pdf, max_bytes=policy.max_file_bytes)
            _write_output_file(temporary / "latexdiff.pdf", diff_pdf_data)
            latexdiff_pdf_ref = _artifact(
                "latexdiff.pdf",
                diff_pdf_data,
                role="latexdiff_pdf",
                media_type="application/pdf",
            )
            deliverables.append(latexdiff_pdf_ref)

        revised_log_ref = _artifact(
            "logs/revised-compile.json",
            logs["logs/revised-compile.json"],
            role="compile_log",
            media_type="application/json",
        )
        compile_log_text = _final_compile_log_text(
            revised_build,
            main_document,
            revised_run,
            max_file_bytes=policy.max_file_bytes,
        )
        references_status, structure_status = _compile_checks(compile_log_text, revised_run.status)
        if references_status == "fail" and status == "pass":
            status = "fail"

        compile_payload = {
            "status": revised_run.status,
            "tool": _tool_identity("latexmk", policy),
            "exit_code": None if revised_run.result is None else revised_run.result.returncode,
            "timed_out": False if revised_run.result is None else revised_run.result.timed_out,
            "log": revised_log_ref,
        }
        latexdiff_status: CommandStatus
        if latexdiff_run.status != "pass":
            latexdiff_status = latexdiff_run.status
        else:
            latexdiff_status = diff_compile_run.status

        payload_without_id: dict[str, Any] = {
            "status": status,
            "patch_plan_sha256": plan_sha,
            "source_manifest_sha256": source_sha,
            "revised_source_manifest_sha256": revised_pre.tree_sha256,
            "original_source_pre_sha256": original_pre.tree_sha256,
            "original_source_post_sha256": original_pre.tree_sha256,
            "returned_original_pre_sha256": returned_pre.sha256,
            "returned_original_post_sha256": returned_pre.sha256,
            "actual_diff": actual_diff_ref,
            "patch_reconciliation": reconciliation,
            "compile": compile_payload,
            "references": {"status": references_status},
            "structure": {"status": structure_status},
            "latexdiff": {
                "status": latexdiff_status,
                "tex": latexdiff_tex_ref,
                "pdf": latexdiff_pdf_ref,
            },
            "deliverables": sorted(deliverables, key=lambda item: cast("str", item["path"])),
            "diagnostics": [],
        }
        verification_id = stable_id(
            "verify_",
            {
                "run_id": run_id,
                "bindings": [source_sha, changeset_sha, approval_sha, plan_sha],
                "applied_tree": revised_pre.tree_sha256,
                "payload": payload_without_id,
            },
        )
        extensions = {
            "org.latex-word-review.verification": {
                "changeset_payload_sha256": changeset_sha,
                "approval_set_payload_sha256": approval_sha,
                "patch_plan_payload_sha256": plan_sha,
                "source_manifest_payload_sha256": source_sha,
                "original_source_tree_sha256": original_pre.tree_sha256,
                "applied_source_tree_sha256": revised_pre.tree_sha256,
                "policy_sha256": cast("Mapping[str, Any]", patch_plan["payload"])["policy_sha256"],
                "verification_policy_sha256": policy.payload_sha256,
                "commands": [run.extension_record() for run in runs],
            }
        }
        report = make_envelope(
            schema_name="VerificationReport",
            object_id=verification_id,
            run_id=run_id,
            generated_at=timestamp,
            producer=_project_tool(policy),
            payload=payload_without_id,
            extensions=extensions,
        )
        _write_output_file(temporary / "verification-report.json", canonical_json(report) + b"\n")

        original_post = _read_original_tree(
            original_root, source_manifest, max_file_bytes=policy.max_file_bytes
        )
        returned_post = digest_file(returned_original, max_bytes=policy.max_file_bytes)
        if original_post.tree_sha256 != original_pre.tree_sha256 or returned_post != returned_pre:
            raise ContractError(
                ErrorCode.VERIFY_ORIGINAL_MUTATED,
                "an authoritative input changed during verification",
            )
        revised_post = _read_revised_tree(
            revised_root, original_pre, max_file_bytes=policy.max_file_bytes
        )
        if revised_post.tree_sha256 != revised_pre.tree_sha256:
            raise ContractError(ErrorCode.PATCH_SOURCE_DRIFT, "revised input changed during verify")

        shutil.rmtree(work)
        try:
            temporary.rename(target)
        except OSError as exc:
            raise ContractError(
                ErrorCode.APPLY_PARTIAL_WRITE,
                "verification output could not be atomically published",
            ) from exc
        return VerificationResult(
            report=copy.deepcopy(report),
            output_root=output_root,
            revised_source_tree_sha256=revised_pre.tree_sha256,
            status=status,
        )
    except Exception:
        if temporary.exists():
            _remove_owned_tree(temporary, target.parent, prefix)
        raise


__all__ = [
    "DEFAULT_VERIFICATION_POLICY",
    "VerificationPolicy",
    "VerificationResult",
    "verify_latex_project",
]
