"""Small, redacted support bundles for the local Windows application.

This module is deliberately independent from workflow contracts and artifacts.
It never reads a run directory and accepts no caller-provided mapping, log, path,
document, or exception message.  Only a fixed allowlist of bounded scalar
diagnostics can enter the two-member ZIP.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from latex_word_review.__about__ import __version__
from latex_word_review.canonical import canonical_json
from latex_word_review.contracts import load_contract_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, read_stable_bytes

SUPPORT_BUNDLE_FILENAME: Final = "latex-word-review-support.zip"
_README_NAME: Final = "README.txt"
_METADATA_NAME: Final = "support-metadata.json"
_ENTRY_NAMES: Final = (_README_NAME, _METADATA_NAME)
_FORMAT_VERSION: Final = "latex-word-review-redacted-support-v1"
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
_FILE_MODE: Final = stat.S_IFREG | 0o644
_REPARSE_ATTRIBUTE: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_ARCHIVE_BYTES: Final = 64 * 1024
_MAX_METADATA_BYTES: Final = 4 * 1024
_MAX_TASK_IDENTIFIER_CHARS: Final = 256
_MAX_TASK_IDENTIFIER_BYTES: Final = 1024
_MAX_DURATION_MS: Final = 7 * 24 * 60 * 60 * 1000
_MAX_WINERROR: Final = 0xFFFFFFFF

_STAGE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_BUILD_PATTERN: Final = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._+-]{0,95}|sha256:[0-9a-f]{64})")
_VERSION_PATTERN: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
_EXCEPTION_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}")
_ANONYMOUS_TASK_PATTERN: Final = re.compile(r"task_[0-9a-f]{64}")
_METADATA_FIELDS: Final = frozenset(
    {
        "format_version",
        "product_version",
        "build_identifier",
        "error_code",
        "stage",
        "anonymous_task_id",
        "duration_ms",
        "exception_type",
        "winerror",
    }
)

_README_BYTES: Final = (
    "LaTeX Word Review - redacted support bundle\n"
    "===========================================\n\n"
    "This archive contains only bounded, allowlisted technical metadata:\n"
    "the stable error code and stage, product/build identifiers, a one-way\n"
    "anonymous task token, and optional duration, exception type, and Windows\n"
    "winerror number.\n\n"
    "It intentionally contains no source or document files, document text,\n"
    "absolute paths, sealed workflow JSON, raw logs, exception messages, or\n"
    "arbitrary application fields. The anonymous token is derived from the task\n"
    "identifier; the original identifier is not stored.\n\n"
    "Before sharing the ZIP, you may inspect support-metadata.json.\n\n"
    "此压缩包只包含有界、白名单化的技术元数据。它不包含论文源文件、Word\n"
    "文件或正文、绝对路径、密封工作流 JSON、原始日志、异常消息，也不会复制\n"
    "调用方提供的任意字典字段。分享前可自行检查 support-metadata.json。\n"
).encode()

type MetadataValue = str | int | None


@dataclass(frozen=True, slots=True)
class SupportBundleLimits:
    """Hard-capped read/write limits for one redacted support archive."""

    max_archive_bytes: int = _MAX_ARCHIVE_BYTES
    max_metadata_bytes: int = _MAX_METADATA_BYTES

    def __post_init__(self) -> None:
        for value, hard_max, label in (
            (self.max_archive_bytes, _MAX_ARCHIVE_BYTES, "archive"),
            (self.max_metadata_bytes, _MAX_METADATA_BYTES, "metadata"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > hard_max
            ):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    f"support bundle {label} limit is outside the fixed safe range",
                )


DEFAULT_SUPPORT_BUNDLE_LIMITS: Final = SupportBundleLimits()


@dataclass(frozen=True, slots=True)
class SupportBundleRequest:
    """The only caller-controlled values eligible for redacted serialization.

    ``task_identifier`` is accepted only to derive a one-way token. Its original
    bytes are never returned or written. No free-form details mapping exists by
    design.
    """

    error_code: ErrorCode | str
    stage: str
    build_identifier: str
    task_identifier: str
    duration_ms: int | None = None
    exception_type: str | None = None
    winerror: int | None = None

    def __post_init__(self) -> None:
        try:
            normalized_code = (
                self.error_code
                if isinstance(self.error_code, ErrorCode)
                else ErrorCode(self.error_code)
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "support bundle error code must be a stable public ErrorCode",
            ) from exc
        object.__setattr__(self, "error_code", normalized_code)
        _require_pattern(self.stage, _STAGE_PATTERN, "stage")
        _require_pattern(self.build_identifier, _BUILD_PATTERN, "build identifier")
        _validate_task_identifier(self.task_identifier)
        _validate_optional_integer(
            self.duration_ms,
            maximum=_MAX_DURATION_MS,
            label="duration_ms",
        )
        if self.exception_type is not None:
            _require_pattern(self.exception_type, _EXCEPTION_PATTERN, "exception type")
        _validate_optional_integer(
            self.winerror,
            maximum=_MAX_WINERROR,
            label="winerror",
        )


@dataclass(frozen=True, slots=True)
class SupportBundleVerification:
    """Verified, path-free facts about one redacted support ZIP."""

    metadata: MappingProxyType[str, MetadataValue]
    sha256: str
    size_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class SupportBundleResult:
    """Result of a successful atomic no-clobber publication."""

    path: Path
    metadata: MappingProxyType[str, MetadataValue]
    sha256: str
    size_bytes: int
    entry_count: int


def _require_pattern(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"support bundle {label} is not a bounded safe token",
        )
    return value


def _validate_task_identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support bundle task identifier must be non-empty",
        )
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support bundle task identifier must be valid UTF-8 text",
        ) from exc
    if (
        len(value) > _MAX_TASK_IDENTIFIER_CHARS
        or len(encoded) > _MAX_TASK_IDENTIFIER_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support bundle task identifier exceeds its fixed safe boundary",
        )
    return value


def _validate_optional_integer(value: object, *, maximum: int, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"support bundle {label} is outside its fixed safe range",
        )
    return value


def _anonymous_task_id(task_identifier: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"latex-word-review-support-task-v1\x00")
    digest.update(task_identifier.encode("utf-8", errors="strict"))
    return f"task_{digest.hexdigest()}"


def _metadata(request: SupportBundleRequest) -> dict[str, MetadataValue]:
    if not isinstance(request, SupportBundleRequest):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support bundle request must use the fixed typed contract",
        )
    return {
        "format_version": _FORMAT_VERSION,
        "product_version": __version__,
        "build_identifier": request.build_identifier,
        "error_code": cast("ErrorCode", request.error_code).value,
        "stage": request.stage,
        "anonymous_task_id": _anonymous_task_id(request.task_identifier),
        "duration_ms": request.duration_ms,
        "exception_type": request.exception_type,
        "winerror": request.winerror,
    }


def _validate_metadata(value: object) -> dict[str, MetadataValue]:
    if not isinstance(value, dict) or set(value) != _METADATA_FIELDS:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support metadata fields differ from the fixed allowlist",
        )
    metadata = cast("dict[str, object]", value)
    if metadata.get("format_version") != _FORMAT_VERSION:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support bundle format version is unsupported",
        )
    error_code = metadata.get("error_code")
    if not isinstance(error_code, str):
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support metadata error code is not stable",
        )
    try:
        ErrorCode(error_code)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support metadata error code is not stable",
        ) from exc
    try:
        _require_pattern(metadata.get("product_version"), _VERSION_PATTERN, "product version")
        _require_pattern(metadata.get("build_identifier"), _BUILD_PATTERN, "build identifier")
        _require_pattern(metadata.get("stage"), _STAGE_PATTERN, "stage")
        _require_pattern(
            metadata.get("anonymous_task_id"),
            _ANONYMOUS_TASK_PATTERN,
            "anonymous task identifier",
        )
        _validate_optional_integer(
            metadata.get("duration_ms"),
            maximum=_MAX_DURATION_MS,
            label="duration_ms",
        )
        exception_type = metadata.get("exception_type")
        if exception_type is not None:
            _require_pattern(exception_type, _EXCEPTION_PATTERN, "exception type")
        _validate_optional_integer(
            metadata.get("winerror"),
            maximum=_MAX_WINERROR,
            label="winerror",
        )
    except ContractError as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support metadata contains an invalid allowlisted value",
        ) from exc
    return cast("dict[str, MetadataValue]", metadata)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = _FILE_MODE << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _build_archive_bytes(members: dict[str, bytes]) -> bytes:
    if tuple(sorted(members)) != _ENTRY_NAMES:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "support bundle member set is not fixed",
        )
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for name in _ENTRY_NAMES:
                archive.writestr(_zip_info(name), members[name], compress_type=zipfile.ZIP_STORED)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support ZIP could not be constructed",
        ) from exc
    return output.getvalue()


def _status_without_following(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE,
            "support output path status is unavailable",
        ) from exc


def _is_link_or_reparse(path: Path, status: os.stat_result | None = None) -> bool:
    current = _status_without_following(path) if status is None else status
    if current is None:
        return False
    attributes = getattr(current, "st_file_attributes", 0)
    junction_probe = getattr(path, "is_junction", None)
    try:
        is_junction = bool(junction_probe is not None and junction_probe())
    except OSError as exc:
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE,
            "support output reparse state is unavailable",
        ) from exc
    return bool(stat.S_ISLNK(current.st_mode) or attributes & _REPARSE_ATTRIBUTE or is_junction)


def _require_real_directory(path: Path, label: str) -> Path:
    status = _status_without_following(path)
    if status is None:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"support {label} is unavailable")
    if _is_link_or_reparse(path, status):
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE,
            f"support {label} must not be a reparse point",
        )
    if not stat.S_ISDIR(status.st_mode):
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"support {label} must be a directory")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"support {label} is unavailable") from exc


def _validated_target(destination: str | Path, allowed_root: str | Path) -> Path:
    raw_target = Path(destination)
    raw_root = Path(allowed_root)
    if not raw_target.is_absolute() or not raw_root.is_absolute():
        raise ContractError(
            ErrorCode.PATH_ABSOLUTE,
            "support destination and allowed root must be absolute",
        )
    if raw_target.name != SUPPORT_BUNDLE_FILENAME:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support destination must use the fixed safe filename",
        )

    lexical_root = Path(os.path.abspath(os.fspath(raw_root)))
    lexical_target = Path(os.path.abspath(os.fspath(raw_target)))
    try:
        relative_parent = lexical_target.parent.relative_to(lexical_root)
    except ValueError as exc:
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "support destination is outside the allowed output root",
        ) from exc

    resolved_root = _require_real_directory(lexical_root, "allowed output root")
    current = lexical_root
    for component in relative_parent.parts:
        current /= component
        _require_real_directory(current, "output directory")
    resolved_parent = _require_real_directory(current, "output directory")
    if not resolved_parent.is_relative_to(resolved_root):
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE,
            "support destination resolves outside the allowed output root",
        )
    target = resolved_parent / SUPPORT_BUNDLE_FILENAME
    target_status = _status_without_following(target)
    if target_status is not None:
        if _is_link_or_reparse(target, target_status):
            raise ContractError(
                ErrorCode.PATH_LINK_ESCAPE,
                "support destination must not be a reparse point",
            )
        raise ContractError(ErrorCode.SCHEMA_INVALID, "support destination already exists")
    return target


def _archive_members(data: bytes, *, limits: SupportBundleLimits) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as archive:
            if archive.comment:
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH,
                    "support ZIP comment is forbidden",
                )
            infos = archive.infolist()
            if tuple(info.filename for info in infos) != _ENTRY_NAMES:
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH,
                    "support ZIP member set or order differs",
                )
            members: dict[str, bytes] = {}
            for info in infos:
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or info.flag_bits & 0x1
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.date_time != _FIXED_ZIP_TIME
                    or info.create_system != 3
                    or info.extra
                    or info.comment
                    or info.internal_attr != 0
                    or mode != _FILE_MODE
                ):
                    raise ContractError(
                        ErrorCode.BUNDLE_HASH_MISMATCH,
                        "support ZIP member metadata differs from the fixed safe form",
                    )
                if info.filename == _README_NAME:
                    maximum = len(_README_BYTES)
                else:
                    maximum = limits.max_metadata_bytes
                if info.file_size > maximum or info.compress_size != info.file_size:
                    raise ContractError(
                        ErrorCode.BUNDLE_HASH_MISMATCH,
                        "support ZIP member exceeds its fixed size boundary",
                    )
                members[info.filename] = archive.read(info)
    except ContractError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support ZIP is invalid or unreadable",
        ) from exc
    return members


def verify_support_bundle_bytes(
    data: bytes,
    *,
    limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
) -> SupportBundleVerification:
    """Verify one already-pinned archive byte string without filesystem races."""

    if not isinstance(data, bytes) or not data or len(data) > limits.max_archive_bytes:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support ZIP bytes are empty or exceed the fixed size boundary",
        )
    members = _archive_members(data, limits=limits)
    if members[_README_NAME] != _README_BYTES:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support README differs from the fixed redaction notice",
        )
    metadata_bytes = members[_METADATA_NAME]
    try:
        parsed = load_contract_json(metadata_bytes)
    except ContractError as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support metadata is not strict UTF-8 JSON",
        ) from exc
    metadata = _validate_metadata(parsed)
    if metadata_bytes != canonical_json(metadata) + b"\n":
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support metadata is not in the fixed canonical form",
        )
    if data != _build_archive_bytes(members):
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "support ZIP contains non-canonical or trailing bytes",
        )
    digest = digest_bytes(data)
    return SupportBundleVerification(
        metadata=MappingProxyType(dict(metadata)),
        sha256=digest.sha256,
        size_bytes=digest.size_bytes,
        entry_count=len(_ENTRY_NAMES),
    )


def _read_support_bundle_bytes(
    path: str | Path,
    *,
    limits: SupportBundleLimits,
) -> bytes:
    archive_path = Path(path)
    status = _status_without_following(archive_path)
    if status is None:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "support ZIP is unavailable")
    if _is_link_or_reparse(archive_path, status):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "support ZIP must not be a reparse point")
    if not stat.S_ISREG(status.st_mode):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "support ZIP must be a regular file")
    return read_stable_bytes(archive_path, max_bytes=limits.max_archive_bytes)


def read_verified_support_bundle(
    path: str | Path,
    *,
    limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
) -> bytes:
    """Read one regular file stably and verify the exact bytes that are returned."""

    data = _read_support_bundle_bytes(path, limits=limits)
    verify_support_bundle_bytes(data, limits=limits)
    return data


def verify_support_bundle(
    path: str | Path,
    *,
    limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
) -> SupportBundleVerification:
    """Verify exact members, metadata allowlist, bounds, and canonical ZIP bytes."""

    data = _read_support_bundle_bytes(path, limits=limits)
    return verify_support_bundle_bytes(data, limits=limits)


def build_support_bundle_bytes(
    request: SupportBundleRequest,
    *,
    limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
) -> bytes:
    """Build and verify one canonical, redacted support archive in memory."""

    metadata = _metadata(request)
    metadata_bytes = canonical_json(metadata) + bytes((10,))
    if len(metadata_bytes) > limits.max_metadata_bytes:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support metadata exceeds its fixed size boundary",
        )
    members = {_README_NAME: _README_BYTES, _METADATA_NAME: metadata_bytes}
    archive_bytes = _build_archive_bytes(members)
    if len(archive_bytes) > limits.max_archive_bytes:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support ZIP exceeds its fixed size boundary",
        )
    verify_support_bundle_bytes(archive_bytes, limits=limits)
    return archive_bytes


def create_support_bundle(
    destination: str | Path,
    *,
    allowed_root: str | Path,
    request: SupportBundleRequest,
    limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
) -> SupportBundleResult:
    """Create, verify, and atomically publish a fixed-name redacted support ZIP.

    The destination must be the fixed filename inside an existing, caller-owned
    ``allowed_root``. Every directory below that trust root must be a real
    directory rather than a symlink, junction, or other reparse point. Existing
    targets are never replaced.
    """

    metadata = _metadata(request)
    metadata_bytes = canonical_json(metadata) + b"\n"
    if len(metadata_bytes) > limits.max_metadata_bytes:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support metadata exceeds its fixed size boundary",
        )
    members = {_README_NAME: _README_BYTES, _METADATA_NAME: metadata_bytes}
    archive_bytes = _build_archive_bytes(members)
    if len(archive_bytes) > limits.max_archive_bytes:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "support ZIP exceeds its fixed size boundary",
        )
    target = _validated_target(destination, allowed_root)

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{SUPPORT_BUNDLE_FILENAME}.support-",
            suffix=".tmp",
            dir=target.parent,
        )
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "support ZIP stage could not be created",
        ) from exc
    staged = Path(temporary_name)
    linked = False
    published = False
    try:
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(archive_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            staged.chmod(0o600)
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "support ZIP stage could not be written safely",
            ) from exc
        staged_verification = verify_support_bundle(staged, limits=limits)
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "support destination already exists",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "support ZIP could not be atomically published",
            ) from exc
        linked = True
        final_verification = verify_support_bundle(target, limits=limits)
        if final_verification != staged_verification:
            raise ContractError(
                ErrorCode.BUNDLE_HASH_MISMATCH,
                "published support ZIP differs from its verified stage",
            )
        published = True
        return SupportBundleResult(
            path=target,
            metadata=final_verification.metadata,
            sha256=final_verification.sha256,
            size_bytes=final_verification.size_bytes,
            entry_count=final_verification.entry_count,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if linked and not published:
            with suppress(OSError):
                if os.path.samefile(staged, target):
                    target.unlink()
        with suppress(FileNotFoundError):
            staged.unlink()


__all__ = [
    "DEFAULT_SUPPORT_BUNDLE_LIMITS",
    "SUPPORT_BUNDLE_FILENAME",
    "SupportBundleLimits",
    "SupportBundleRequest",
    "SupportBundleResult",
    "SupportBundleVerification",
    "build_support_bundle_bytes",
    "create_support_bundle",
    "read_verified_support_bundle",
    "verify_support_bundle",
    "verify_support_bundle_bytes",
]
