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
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from latex_word_review.__about__ import __version__
from latex_word_review.atomic_publish import publish_new_directory
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
_INPUT_COMMAND_RE: Final = re.compile(r"\\(input|include)\s*\{([^{}]+)\}")
_LATEXDIFF_USE_RE: Final = re.compile(r"(?:\\DIF(?:add|del)(?:FL)?(?:begin|end)?\b|%DIF\s+[<>])")
_MAX_LATEXDIFF_INCLUDE_DEPTH: Final = 256
_WINDOWS_ABSOLUTE_RE: Final = re.compile(r"(?<![\w.])[A-Za-z]:[\\/][^\s\"'<>]*")
_POSIX_ABSOLUTE_RE: Final = re.compile(r"(?<![\w.])/(?:[^\s\"'<>]+)")
_MIKTEX_ROOT_ENVIRONMENT: Final = (
    "MIKTEX_USERCONFIG",
    "MIKTEX_USERDATA",
    "MIKTEX_USERINSTALL",
)
_MIKTEX_CONFIG_RELATIVE: Final = Path("miktex") / "config"
_MIKTEX_INSTALL_PROBE_LIMIT: Final = 64 * 1024 * 1024

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


@dataclass(frozen=True, slots=True)
class _ResolvedExternalTool:
    executable: str | Path
    path: Path | None
    miktex_install_root: Path | None
    miktex_bin_root: Path | None


@dataclass(frozen=True, slots=True)
class _InstallationFileState:
    path: Path
    digest: FileDigest | None
    mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class _TexToolchain:
    latexmk_executable: str | Path
    latexdiff_executable: str | Path
    temp_root: Path
    environment_additions: Mapping[str, str]
    latexmk_arguments: tuple[str, ...]
    installation_state: tuple[_InstallationFileState, ...]


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


def _mask_tex_comments(text: str) -> str:
    """Mask ordinary TeX line comments without changing string offsets."""

    masked = list(text)
    offset = 0
    for line in text.splitlines(keepends=True):
        comment_at: int | None = None
        for index, character in enumerate(line):
            if character != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                comment_at = index
                break
        if comment_at is not None:
            for index in range(comment_at, len(line)):
                if line[index] not in "\r\n":
                    masked[offset + index] = " "
        offset += len(line)
    return "".join(masked)


def _input_reference_candidates(reference: str) -> tuple[str, ...]:
    raw = reference.strip()
    if not raw or any(token in raw for token in ("\\", "#", "$", "~", "{", "}")):
        return ()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        return ()
    try:
        normalized = validate_relative_path(raw)
        if PurePosixPath(normalized).suffix:
            return (normalized,)
        return (validate_relative_path(f"{normalized}.tex"),)
    except ContractError:
        return ()


def _flatten_latexdiff_source(
    tree: _SourceTree,
    source_manifest: Mapping[str, Any],
    main_document: str,
    *,
    max_bytes: int,
) -> bytes:
    """Expand sealed static ``input/include`` edges before invoking latexdiff.

    MiKTeX's Perl-based ``latexdiff --flatten`` cannot reliably resolve files
    when its Windows working directory contains non-ASCII characters.  The
    verifier already owns a bounded, immutable source tree and a sealed static
    dependency graph, so expanding only those proven edges in memory avoids
    handing path discovery back to Perl.  Dynamic or unresolved commands stay
    untouched and therefore cannot escape the verified source closure.
    """

    if max_bytes <= 0:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "latexdiff input limit is invalid")
    selected_main = validate_relative_path(main_document)
    if selected_main not in tree.files:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "latexdiff main document is missing")

    payload = cast("Mapping[str, Any]", source_manifest["payload"])
    dependency_edges = cast("Sequence[Mapping[str, Any]]", payload["dependency_edges"])
    outgoing: dict[tuple[str, str], set[str]] = {}
    for edge in dependency_edges:
        kind = cast("str", edge["kind"])
        if kind not in {"input", "include"}:
            continue
        source = validate_relative_path(cast("str", edge["from"]))
        target = validate_relative_path(cast("str", edge["to"]))
        if source not in tree.files or target not in tree.files:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "latexdiff dependency edge is outside the sealed source tree",
            )
        outgoing.setdefault((source, kind), set()).add(target)

    cache: dict[str, tuple[str, int]] = {}

    def expand(path: str, stack: tuple[str, ...]) -> tuple[str, int]:
        cached = cache.get(path)
        if cached is not None:
            return cached
        if path in stack or len(stack) >= _MAX_LATEXDIFF_INCLUDE_DEPTH:
            raise ContractError(
                ErrorCode.VERIFY_COMPILE_FAILED,
                "latexdiff static include graph is cyclic or too deep",
            )
        try:
            text = tree.files[path].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContractError(
                ErrorCode.VERIFY_COMPILE_FAILED,
                "latexdiff source input is not valid UTF-8",
            ) from exc

        visible = _mask_tex_comments(text)
        parts: list[str] = []
        byte_count = 0
        cursor = 0

        def append(piece: str, piece_bytes: int | None = None) -> None:
            nonlocal byte_count
            encoded_size = len(piece.encode("utf-8")) if piece_bytes is None else piece_bytes
            if byte_count + encoded_size > max_bytes:
                raise ContractError(
                    ErrorCode.VERIFY_COMPILE_FAILED,
                    "flattened latexdiff input exceeds the configured output limit",
                )
            parts.append(piece)
            byte_count += encoded_size

        for match in _INPUT_COMMAND_RE.finditer(visible):
            preceding_backslashes = 0
            preceding_at = match.start() - 1
            while preceding_at >= 0 and visible[preceding_at] == "\\":
                preceding_backslashes += 1
                preceding_at -= 1
            if preceding_backslashes % 2 == 1:
                continue

            kind = match.group(1)
            candidates = {item.casefold() for item in _input_reference_candidates(match.group(2))}
            targets = {
                target
                for target in outgoing.get((path, kind), set())
                if target.casefold() in candidates
            }
            if not targets:
                continue
            if len(targets) != 1:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "latexdiff input command maps to multiple sealed dependencies",
                )
            target = next(iter(targets))
            expanded, expanded_bytes = expand(target, (*stack, path))
            append(text[cursor : match.start()])
            append(expanded, expanded_bytes)
            cursor = match.end()
        append(text[cursor:])
        flattened = "".join(parts)
        result = (flattened, byte_count)
        cache[path] = result
        return result

    flattened, _ = expand(selected_main, ())
    return flattened.encode("utf-8")


def _latexdiff_has_change_markers(data: bytes) -> bool:
    """Return true only for actual latexdiff uses, not its macro definitions."""

    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return any(
        "%DIF PREAMBLE" not in line and _LATEXDIFF_USE_RE.search(line) is not None
        for line in text.splitlines()
    )


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


def _safe_windows_search_path(value: str) -> str:
    """Drop empty and relative PATH entries before resolving a top-level tool."""

    entries: list[str] = []
    seen: set[str] = set()
    for raw_entry in value.split(os.pathsep):
        raw_entry = raw_entry.strip().strip('"')
        if not raw_entry:
            continue
        candidate = Path(raw_entry)
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        key = os.path.normcase(os.fspath(resolved))
        if key not in seen:
            seen.add(key)
            entries.append(os.fspath(resolved))
    return os.pathsep.join(entries)


def _common_miktex_candidates(tool_name: str) -> tuple[Path, ...]:
    """Return exact per-user MiKTeX locations without scanning the host."""

    if os.name != "nt":
        return ()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return ()
    root = Path(local_app_data)
    if not root.is_absolute():
        return ()
    base = root / "Programs" / "MiKTeX" / "miktex" / "bin"
    return tuple(base / architecture / f"{tool_name}.exe" for architecture in ("x64", "x86"))


def _find_on_windows_path(command: str, search_path: str) -> Path | None:
    suffixes: tuple[str, ...] = ("",)
    if not Path(command).suffix:
        configured = tuple(
            item
            for item in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if item and item.startswith(".")
        )
        suffixes = configured or (".COM", ".EXE", ".BAT", ".CMD")
    for raw_directory in search_path.split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        for suffix in suffixes:
            candidate = directory / f"{command}{suffix}"
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def _resolve_regular_tool(path: Path) -> Path | None:
    try:
        if _is_link_or_junction(path):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "tool executable must not be a link")
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContractError(ErrorCode.TOOL_MISSING, "tool executable is unavailable") from exc
    if not resolved.is_file() or _is_link_or_junction(resolved):
        raise ContractError(ErrorCode.TOOL_MISSING, "tool executable is not a regular file")
    return resolved


def _miktex_identity(path: Path, expected_name: str) -> tuple[Path, Path] | None:
    """Validate an exact MiKTeX bin layout and its installed script registry."""

    architecture_root = path.parent
    if architecture_root.name.casefold() in {"x64", "x86"}:
        bin_root = architecture_root.parent
        executable_root = architecture_root
    else:
        bin_root = architecture_root
        executable_root = architecture_root
    is_layout = bin_root.name.casefold() == "bin" and bin_root.parent.name.casefold() == "miktex"
    looks_like_miktex = any(part.casefold() == "miktex" for part in path.parts)
    if not is_layout:
        if looks_like_miktex:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX executable layout is not recognized",
            )
        return None
    if path.stem.casefold() != expected_name.casefold():
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX tool identity does not match the requested command",
        )
    install_root = bin_root.parent.parent
    scripts_registry = install_root / _MIKTEX_CONFIG_RELATIVE / "scripts.ini"
    initexmf = executable_root / "initexmf.exe"
    if _resolve_regular_tool(scripts_registry) is None or _resolve_regular_tool(initexmf) is None:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX installation root is incomplete",
        )
    return install_root.resolve(strict=True), executable_root.resolve(strict=True)


def _resolve_external_tool(executable: str | Path, expected_name: str) -> _ResolvedExternalTool:
    """Resolve defaults from PATH or the standard per-user MiKTeX installation."""

    if os.name != "nt":
        return _ResolvedExternalTool(executable, None, None, None)
    requested = os.fspath(executable)
    requested_path = Path(requested)
    has_path_component = (
        requested_path.is_absolute()
        or requested_path.parent != Path(".")
        or "/" in requested
        or "\\" in requested
    )
    located: Path | None = None
    if has_path_component:
        if not requested_path.is_absolute():
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "tool executable must be a bare name or an absolute path",
            )
        located = _resolve_regular_tool(requested_path)
    else:
        safe_path = _safe_windows_search_path(os.environ.get("PATH", ""))
        path_result = _find_on_windows_path(requested, safe_path)
        if path_result is not None:
            located = _resolve_regular_tool(path_result)
        canonical_names = {expected_name.casefold(), f"{expected_name}.exe".casefold()}
        if located is None and requested.casefold() in canonical_names:
            for candidate in _common_miktex_candidates(expected_name):
                located = _resolve_regular_tool(candidate)
                if located is not None:
                    break
    if located is None:
        return _ResolvedExternalTool(executable, None, None, None)
    identity = _miktex_identity(located, expected_name)
    if identity is None:
        return _ResolvedExternalTool(located, located, None, None)
    install_root, bin_root = identity
    return _ResolvedExternalTool(located, located, install_root, bin_root)


def _same_windows_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _installation_file_state(path: Path) -> _InstallationFileState:
    try:
        exists = path.exists()
    except OSError as exc:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX installation identity is unavailable",
        ) from exc
    if not exists:
        return _InstallationFileState(path, None, None)
    resolved = _resolve_regular_tool(path)
    if resolved is None:  # pragma: no cover - guarded by exists
        return _InstallationFileState(path, None, None)
    try:
        metadata = resolved.stat()
        digest = digest_file(resolved, max_bytes=_MIKTEX_INSTALL_PROBE_LIMIT)
    except OSError as exc:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX installation identity cannot be read",
        ) from exc
    return _InstallationFileState(resolved, digest, metadata.st_mtime_ns)


def _capture_miktex_installation_state(
    install_root: Path,
    bin_root: Path,
    latexmk_mode: str,
    *,
    companion_tools: Sequence[Path] = (),
) -> tuple[_InstallationFileState, ...]:
    helper_names = {
        "-pdf": "pdflatex.exe",
        "-xelatex": "xelatex.exe",
        "-lualatex": "lualatex.exe",
    }
    engine_name = helper_names[latexmk_mode]
    config_root = install_root / _MIKTEX_CONFIG_RELATIVE
    paths = (
        config_root / "scripts.ini",
        config_root / "packages.ini",
        config_root / "mpm.ini",
        config_root / "package-manifests.ini",
        bin_root / "initexmf.exe",
        bin_root / "latexmk.exe",
        bin_root / "latexdiff.exe",
        bin_root / engine_name,
        bin_root / "bibtex.exe",
        bin_root / "biber.exe",
        bin_root / "makeindex.exe",
        *companion_tools,
    )
    return tuple(_installation_file_state(path) for path in paths)


def _perl_command_assignment(variable: str, executable: Path) -> str:
    command = f'"{executable.as_posix()}" %O %S'
    escaped = command.replace("\\", "\\\\").replace("'", "\\'")
    return f"${variable} = '{escaped}';"


def _miktex_bound_latexmk_arguments(bin_root: Path, latexmk_mode: str) -> tuple[str, ...]:
    engine_name = {
        "-pdf": "pdflatex.exe",
        "-xelatex": "xelatex.exe",
        "-lualatex": "lualatex.exe",
    }[latexmk_mode]
    engine_option = {
        "-pdf": "-pdflatex=",
        "-xelatex": "-xelatex=",
        "-lualatex": "-lualatex=",
    }[latexmk_mode]
    engine = bin_root / engine_name
    helpers = {
        "bibtex": bin_root / "bibtex.exe",
        "biber": bin_root / "biber.exe",
        "makeindex": bin_root / "makeindex.exe",
    }
    for executable in (engine, *helpers.values()):
        if _resolve_regular_tool(executable) is None:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX verification helper is missing",
            )
    engine_command = f'"{engine.as_posix()}" %O %S'
    return (
        "-disable-installer",
        f"{engine_option}{engine_command}",
        "-e",
        _perl_command_assignment("bibtex", helpers["bibtex"]),
        "-e",
        _perl_command_assignment("biber", helpers["biber"]),
        "-e",
        _perl_command_assignment("makeindex", helpers["makeindex"]),
    )


def _verify_miktex_installation_state(toolchain: _TexToolchain) -> None:
    for expected in toolchain.installation_state:
        observed = _installation_file_state(expected.path)
        if observed.digest != expected.digest or observed.mtime_ns != expected.mtime_ns:
            raise ContractError(
                ErrorCode.VERIFY_COMPILE_FAILED,
                "MiKTeX installation changed during verification",
            )


def _isolated_miktex_path(
    bin_root: Path,
    *,
    companion_bins: Sequence[Path] = (),
) -> str:
    entries = [os.fspath(bin_root)]
    for companion in companion_bins:
        companion_value = os.fspath(companion)
        if companion_value not in entries:
            entries.append(companion_value)
    system_root_value = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if system_root_value:
        system_root = Path(system_root_value)
        if system_root.is_absolute():
            for candidate in (system_root / "System32", system_root):
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if resolved.is_dir() and os.fspath(resolved) not in entries:
                    entries.append(os.fspath(resolved))
    return os.pathsep.join(entries)


def _prepare_tex_toolchain(
    work: Path,
    latexmk_executable: str | Path,
    latexdiff_executable: str | Path,
    latexmk_mode: str,
) -> _TexToolchain:
    """Prepare one private runtime and a coherent Windows TeX toolchain."""

    resolved_latexmk = _resolve_external_tool(latexmk_executable, "latexmk")
    resolved_latexdiff = _resolve_external_tool(latexdiff_executable, "latexdiff")
    runtime_root = work / "tool-runtime"
    runtime_root.mkdir(parents=False, exist_ok=False)
    temp_root = runtime_root / "temp"
    temp_root.mkdir(parents=False, exist_ok=False)
    home_root = runtime_root / "home"
    home_root.mkdir(parents=False, exist_ok=False)
    runtime_identity = {
        "HOME": os.fspath(home_root),
        "USERPROFILE": os.fspath(home_root),
    }
    if os.name == "nt":
        home_drive, home_path = os.path.splitdrive(os.fspath(home_root))
        if home_drive:
            runtime_identity["HOMEDRIVE"] = home_drive
            runtime_identity["HOMEPATH"] = home_path

    identified = tuple(
        tool
        for tool in (resolved_latexmk, resolved_latexdiff)
        if tool.miktex_install_root is not None and tool.miktex_bin_root is not None
    )
    if not identified:
        if os.name == "nt" and (
            resolved_latexmk.path is not None or resolved_latexdiff.path is not None
        ):
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "Windows PDF verification requires a recognized MiKTeX installation",
            )
        return _TexToolchain(
            resolved_latexmk.executable,
            resolved_latexdiff.executable,
            temp_root,
            runtime_identity,
            (),
            (),
        )

    install_root = cast("Path", identified[0].miktex_install_root)
    bin_root = cast("Path", identified[0].miktex_bin_root)
    for tool in identified[1:]:
        other_install = cast("Path", tool.miktex_install_root)
        other_bin = cast("Path", tool.miktex_bin_root)
        if not _same_windows_path(install_root, other_install) or not _same_windows_path(
            bin_root, other_bin
        ):
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX tools belong to different installations",
            )
    for tool in (resolved_latexmk, resolved_latexdiff):
        if tool.path is not None and tool.miktex_install_root is None:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX and non-MiKTeX verification tools cannot be mixed",
            )

    def bind_miktex_tool(tool: _ResolvedExternalTool, name: str) -> str | Path:
        if tool.path is not None:
            return tool.executable
        requested = os.fspath(tool.executable)
        if requested.casefold() not in {name.casefold(), f"{name}.exe".casefold()}:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "an unresolved custom tool cannot be mixed with MiKTeX",
            )
        # Keep an absent companion absolute as well.  run_command will report
        # it as E_TOOL_MISSING without consulting cwd or another distribution.
        return bin_root / f"{name}.exe"

    bound_latexmk = bind_miktex_tool(resolved_latexmk, "latexmk")
    bound_latexdiff = bind_miktex_tool(resolved_latexdiff, "latexdiff")
    resolved_perl = _resolve_external_tool("perl", "perl")
    if resolved_perl.path is None:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX latexmk and latexdiff require a verified Perl executable",
        )
    perl_path = resolved_perl.path

    config_root = runtime_root / "miktex-config"
    data_root = runtime_root / "miktex-data"
    config_root.mkdir(parents=False, exist_ok=False)
    data_root.mkdir(parents=False, exist_ok=False)
    additions = {
        **runtime_identity,
        "MIKTEX_USERCONFIG": os.fspath(config_root),
        "MIKTEX_USERDATA": os.fspath(data_root),
        "MIKTEX_USERINSTALL": os.fspath(install_root),
        "PATH": _isolated_miktex_path(bin_root, companion_bins=(perl_path.parent,)),
    }
    installer_arguments = _miktex_latexmk_arguments(additions)
    if installer_arguments != ("-disable-installer",):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "MiKTeX verification did not disable the package installer",
        )
    return _TexToolchain(
        bound_latexmk,
        bound_latexdiff,
        temp_root,
        additions,
        _miktex_bound_latexmk_arguments(bin_root, latexmk_mode),
        _capture_miktex_installation_state(
            install_root,
            bin_root,
            latexmk_mode,
            companion_tools=(perl_path,),
        ),
    )


def _miktex_preflight(
    toolchain: _TexToolchain,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    policy: VerificationPolicy,
) -> _ToolRun | None:
    additions = toolchain.environment_additions
    if "MIKTEX_USERINSTALL" not in additions:
        return None
    latexmk_path = Path(toolchain.latexmk_executable)
    initexmf = latexmk_path.parent / "initexmf.exe"
    run = _tool_run(
        "miktex-root-preflight",
        initexmf,
        ("--report", "--disable-installer"),
        cwd=cwd,
        environment=environment,
        policy=policy,
    )
    if run.status != "pass" or run.result is None:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX root report could not be verified",
        )
    fields: dict[str, str] = {}
    for line in run.result.stdout.splitlines():
        name, separator, value = line.partition(":")
        if not separator:
            continue
        name = name.strip()
        if name in fields:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX root report contains duplicate fields",
            )
        fields[name] = value.strip()
    if (
        fields.get("SharedSetup", "").casefold() != "no"
        or fields.get("PathOkay", "").casefold() != "yes"
    ):
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "MiKTeX root report is not a regular non-shared setup",
        )
    expected_paths = {
        "UserInstall": additions["MIKTEX_USERINSTALL"],
        "UserConfig": additions["MIKTEX_USERCONFIG"],
        "UserData": additions["MIKTEX_USERDATA"],
        "LinkTargetDirectory": os.fspath(latexmk_path.parent),
    }
    for name, expected_value in expected_paths.items():
        observed_value = fields.get(name)
        if observed_value is None:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX root report is incomplete",
            )
        try:
            observed = Path(observed_value).resolve(strict=True)
            expected = Path(expected_value).resolve(strict=True)
        except OSError as exc:
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX root report path is unavailable",
            ) from exc
        if not _same_windows_path(observed, expected):
            raise ContractError(
                ErrorCode.TOOL_VERSION_UNSUPPORTED,
                "MiKTeX root report differs from the isolated profile",
            )
    return run


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

    def clear_owned_readonly_and_retry(
        function: Any,
        candidate: str,
        error: BaseException,
    ) -> None:
        if not isinstance(error, PermissionError):
            raise error
        try:
            os.chmod(candidate, stat.S_IREAD | stat.S_IWRITE)
            function(candidate)
        except OSError as retry_error:
            raise error from retry_error

    with suppress(FileNotFoundError):
        shutil.rmtree(path, onexc=clear_owned_readonly_and_retry)


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
    toolchain: _TexToolchain | None = None
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
        latexdiff_input = work / "latexdiff-input"
        _write_output_file(
            latexdiff_input / "original.tex",
            _flatten_latexdiff_source(
                original_pre,
                source_manifest,
                main_document,
                max_bytes=policy.max_output_bytes,
            ),
        )
        _write_output_file(
            latexdiff_input / "revised.tex",
            _flatten_latexdiff_source(
                revised_pre,
                source_manifest,
                main_document,
                max_bytes=policy.max_output_bytes,
            ),
        )
        latexmk_mode = _latexmk_mode(source_manifest)
        toolchain = _prepare_tex_toolchain(
            work,
            latexmk_executable,
            latexdiff_executable,
            latexmk_mode,
        )
        build_root = work / "build"
        build_root.mkdir(parents=True, exist_ok=False)
        revised_build = build_root / "revised"
        diff_build = build_root / "latexdiff"
        revised_build.mkdir()
        diff_build.mkdir()
        tex_environment = {
            "openin_any": "p",
            "openout_any": "p",
            "shell_escape": "0",
            **toolchain.environment_additions,
        }
        environment = minimal_environment(temp_root=toolchain.temp_root, additions=tex_environment)
        miktex_preflight_run = _miktex_preflight(
            toolchain,
            cwd=work,
            environment=environment,
            policy=policy,
        )

        revised_run = _tool_run(
            "latexmk-revised",
            toolchain.latexmk_executable,
            (
                "-norc",
                latexmk_mode,
                *toolchain.latexmk_arguments,
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
            toolchain.latexdiff_executable,
            (
                "--encoding=utf8",
                "latexdiff-input/original.tex",
                "latexdiff-input/revised.tex",
            ),
            cwd=work,
            environment=environment,
            policy=policy,
        )
        latexdiff_data: bytes | None = None
        if latexdiff_run.status == "pass" and latexdiff_run.result is not None:
            candidate_diff = latexdiff_run.result.stdout.encode("utf-8")
            missing_required_markers = bool(actual_diff) and not _latexdiff_has_change_markers(
                candidate_diff
            )
            if (
                not candidate_diff
                or missing_required_markers
                or any(
                    pattern.search(candidate_diff) is not None for pattern in _SHELL_ESCAPE_PATTERNS
                )
            ):
                latexdiff_run = _ToolRun("latexdiff-generate", "fail", latexdiff_run.result, None)
                diff_compile_run = _ToolRun("latexmk-latexdiff", "not_run", None, None)
            else:
                latexdiff_data = candidate_diff
                _write_output_file(diff_compile / "latexdiff.tex", latexdiff_data)
                diff_compile_run = _tool_run(
                    "latexmk-latexdiff",
                    toolchain.latexmk_executable,
                    (
                        "-norc",
                        latexmk_mode,
                        *toolchain.latexmk_arguments,
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

        _verify_miktex_installation_state(toolchain)

        revised_pdf = revised_build / f"{Path(main_document).stem}.pdf"
        if revised_run.status == "pass" and not revised_pdf.is_file():
            revised_run = _ToolRun("latexmk-revised", "fail", revised_run.result, None)
        diff_pdf = diff_build / "latexdiff.pdf"
        if diff_compile_run.status == "pass" and not diff_pdf.is_file():
            diff_compile_run = _ToolRun("latexmk-latexdiff", "fail", diff_compile_run.result, None)
        runs = (revised_run, latexdiff_run, diff_compile_run)
        command_runs = (miktex_preflight_run, *runs) if miktex_preflight_run is not None else runs
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
                "commands": [run.extension_record() for run in command_runs],
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

        _remove_owned_tree(work, temporary, "_work")
        try:
            publish_new_directory(temporary, target)
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
        if toolchain is not None:
            try:
                _verify_miktex_installation_state(toolchain)
            except ContractError:
                if temporary.exists():
                    _remove_owned_tree(temporary, target.parent, prefix)
                raise
        if temporary.exists():
            _remove_owned_tree(temporary, target.parent, prefix)
        raise


__all__ = [
    "DEFAULT_VERIFICATION_POLICY",
    "VerificationPolicy",
    "VerificationResult",
    "verify_latex_project",
]
