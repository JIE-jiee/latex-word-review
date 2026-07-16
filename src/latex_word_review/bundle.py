"""Deterministic, allowlist-only audit bundle creation and verification."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from latex_word_review.__about__ import __version__
from latex_word_review.canonical import canonical_json, sha256_canonical
from latex_word_review.contracts import (
    SCHEMA_VERSION,
    load_contract_json,
    make_envelope,
    validate_contract,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, digest_file, read_stable_bytes
from latex_word_review.ids import derive_artifact_id, stable_id
from latex_word_review.paths import resolve_within, validate_relative_path

_AUDIT_MANIFEST_PATH: Final = "manifest/audit-bundle.json"
_PRIVACY_REPORT_PATH: Final = "manifest/privacy-scan.json"
_INTERNAL_PATHS: Final = frozenset({_AUDIT_MANIFEST_PATH, _PRIVACY_REPORT_PATH})
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
_TIMESTAMP_POLICY: Final = "fixed-1980-01-01T00:00:00"
_PRIVACY_RULES_VERSION: Final = "privacy-rules-v1"
_INTERFACE_VERSION: Final = "audit-bundle-v1alpha1"
_PRIVACY_RULES: Final = (
    (
        "windows-user-profile",
        re.compile(rb"[A-Za-z]:\\Users\\[^\\/\r\n\x00]+", re.IGNORECASE),
    ),
    (
        "posix-user-home",
        re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+", re.IGNORECASE),
    ),
    (
        "email-address",
        re.compile(
            rb"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "api-key",
        re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    ),
)
_CONFIGURATION_SHA256: Final = sha256_canonical(
    {
        "format": SCHEMA_VERSION,
        "compression": "deflate-level-9",
        "entry_order": "lexicographic_path",
        "timestamp_policy": _TIMESTAMP_POLICY,
        "privacy_rules": [rule_id for rule_id, _ in _PRIVACY_RULES],
        "privacy_rules_version": _PRIVACY_RULES_VERSION,
    }
)


@dataclass(frozen=True, slots=True)
class BundleItem:
    """One explicit allowlist item and its validated provenance object."""

    path: str
    role: str
    source_object: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BundleLimits:
    max_entries: int = 4096
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: int = 200

    def __post_init__(self) -> None:
        if (
            min(
                self.max_entries,
                self.max_file_bytes,
                self.max_total_bytes,
                self.max_compression_ratio,
            )
            <= 0
        ):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle limits must be positive")


_DEFAULT_BUNDLE_LIMITS: Final = BundleLimits()


@dataclass(frozen=True, slots=True)
class BundleResult:
    path: Path
    document: Mapping[str, Any]
    file_sha256: str
    size_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class BundleVerification:
    document: Mapping[str, Any]
    file_sha256: str
    size_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    path: str
    role: str
    source_path: Path
    data: bytes
    source_object: Mapping[str, str]


def _producer() -> dict[str, Any]:
    return {
        "name": "latex-word-review",
        "version": __version__,
        "interface_version": _INTERFACE_VERSION,
        "distribution": "python-package",
        "executable_sha256": None,
        "configuration_sha256": _CONFIGURATION_SHA256,
    }


def object_binding(document: Mapping[str, Any]) -> dict[str, str]:
    """Validate a full contract and return its exact ObjectBinding."""

    receipt = validate_contract(document)
    return {
        "schema_name": receipt.schema_name,
        "object_id": cast("str", document["object_id"]),
        "payload_sha256": receipt.payload_sha256,
        "document_sha256": receipt.document_sha256,
    }


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    try:
        return path.is_symlink() or bool(junction_probe is not None and junction_probe())
    except OSError as exc:
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE, "bundle path link state is unavailable"
        ) from exc


def _source_path(root: Path, relative_path: str) -> Path:
    if _is_link_or_junction(root):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "bundle source root cannot be a link")
    candidate = root
    for component in relative_path.split("/"):
        candidate /= component
        if _is_link_or_junction(candidate):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "bundle allowlist crosses a link")
    resolved = resolve_within(root, relative_path)
    if not resolved.is_file():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle allowlist item is not a regular file")
    return resolved


def _normalize_item(value: BundleItem | Mapping[str, Any]) -> BundleItem:
    if isinstance(value, BundleItem):
        return value
    try:
        path = cast("str", value["path"])
        role = cast("str", value["role"])
        source_object = cast("Mapping[str, Any]", value["source_object"])
    except (KeyError, TypeError) as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "bundle allowlist items require path, role and source_object",
        ) from exc
    return BundleItem(path=path, role=role, source_object=source_object)


def _prepare_items(
    source_root: Path,
    allowlist: Sequence[BundleItem | Mapping[str, Any]],
    *,
    run_id: str,
    limits: BundleLimits,
) -> tuple[_PreparedItem, ...]:
    try:
        root = source_root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle source root is unavailable") from exc
    if not root.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle source root must be a directory")
    if len(allowlist) > limits.max_entries:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle allowlist exceeds entry limit")
    prepared: list[_PreparedItem] = []
    seen: set[str] = set()
    total = 0
    for raw_item in allowlist:
        item = _normalize_item(raw_item)
        path = validate_relative_path(item.path)
        if path in _INTERNAL_PATHS:
            raise ContractError(
                ErrorCode.PATH_TRAVERSAL, "allowlist path is reserved by bundle metadata"
            )
        if path in seen:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID, "bundle allowlist contains duplicate paths"
            )
        seen.add(path)
        if not isinstance(item.role, str) or not item.role:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle entry role must be non-empty")
        receipt = validate_contract(item.source_object)
        source_run_id = item.source_object.get("run_id")
        if source_run_id not in {None, run_id}:
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "bundle entry provenance is bound to a different run",
            )
        source_path = _source_path(root, path)
        data = read_stable_bytes(source_path, max_bytes=limits.max_file_bytes)
        total += len(data)
        if total > limits.max_total_bytes:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID, "bundle allowlist exceeds total byte limit"
            )
        prepared.append(
            _PreparedItem(
                path=path,
                role=item.role,
                source_path=source_path,
                data=data,
                source_object={
                    "schema_name": receipt.schema_name,
                    "object_id": cast("str", item.source_object["object_id"]),
                    "payload_sha256": receipt.payload_sha256,
                    "document_sha256": receipt.document_sha256,
                },
            )
        )
    prepared.sort(key=lambda value: value.path)
    return tuple(prepared)


def _privacy_findings(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in sorted(files):
        data = files[path]
        for rule_id, pattern in _PRIVACY_RULES:
            findings.extend(
                {"path": path, "rule_id": rule_id, "byte_offset": match.start()}
                for match in pattern.finditer(data)
            )
    findings.sort(key=lambda item: (item["path"], item["byte_offset"], item["rule_id"]))
    return findings


def _privacy_report(files: Mapping[str, bytes]) -> dict[str, Any]:
    findings = _privacy_findings(files)
    return {
        "format": "latex-word-review-privacy-scan-v1",
        "rules_version": _PRIVACY_RULES_VERSION,
        "status": "fail" if findings else "pass",
        "files_scanned": len(files),
        "bytes_scanned": sum(len(data) for data in files.values()),
        "findings": findings,
    }


def _artifact(path: str, data: bytes, confidentiality: str) -> dict[str, Any]:
    digest = digest_bytes(data)
    return {
        "artifact_id": derive_artifact_id(digest.sha256),
        "path": path,
        "path_base": "bundle_root",
        "role": "privacy_report",
        "media_type": "application/json",
        "size_bytes": digest.size_bytes,
        "sha256": digest.sha256,
        "immutable": True,
        "confidentiality": confidentiality,
    }


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _write_zip(path: Path, members: Mapping[str, bytes]) -> None:
    try:
        with zipfile.ZipFile(
            path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for name in sorted(members):
                archive.writestr(
                    _zip_info(name),
                    members[name],
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        with path.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP could not be written"
        ) from exc


def _validate_zip_member(info: zipfile.ZipInfo, limits: BundleLimits) -> str:
    try:
        name = validate_relative_path(info.filename)
    except ContractError as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP contains an unsafe path"
        ) from exc
    if info.is_dir() or name.endswith("/"):
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP contains a directory entry")
    if info.flag_bits & 0x1:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "encrypted audit ZIP entries are forbidden"
        )
    if info.compress_type != zipfile.ZIP_DEFLATED:
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP compression method differs")
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_IFMT(mode) == stat.S_IFLNK:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "audit ZIP contains a symbolic link")
    if info.file_size > limits.max_file_bytes:
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP member exceeds size limit")
    if info.file_size and (
        info.compress_size == 0
        or info.file_size > info.compress_size * limits.max_compression_ratio
    ):
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP compression ratio is unsafe")
    if (
        info.date_time != _FIXED_ZIP_TIME
        or info.create_system != 3
        or info.extra
        or info.comment
        or info.internal_attr != 0
        or mode != (stat.S_IFREG | 0o644)
    ):
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP metadata is not deterministic"
        )
    return name


def _read_verified_members(
    path: Path, limits: BundleLimits
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    try:
        with zipfile.ZipFile(path, mode="r") as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_entries + len(_INTERNAL_PATHS):
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP has too many entries"
                )
            names = tuple(_validate_zip_member(info, limits) for info in infos)
            if len(names) != len(set(names)):
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP has duplicate entries"
                )
            if names != tuple(sorted(names)):
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP entries are not sorted"
                )
            total = sum(info.file_size for info in infos)
            if total > limits.max_total_bytes:
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP exceeds total size limit"
                )
            members = {name: archive.read(info) for name, info in zip(names, infos, strict=True)}
    except ContractError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP is invalid or unreadable"
        ) from exc
    return members, names


def verify_audit_bundle(
    path: str | Path,
    *,
    expected_run_manifest: Mapping[str, Any] | None = None,
    expected_verification_report: Mapping[str, Any] | None = None,
    limits: BundleLimits = _DEFAULT_BUNDLE_LIMITS,
) -> BundleVerification:
    """Verify archive safety, deterministic metadata, hashes and v1alpha bindings."""

    archive_path = Path(path)
    members, names = _read_verified_members(archive_path, limits)
    if _AUDIT_MANIFEST_PATH not in members or _PRIVACY_REPORT_PATH not in members:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP metadata members are missing"
        )
    document = load_contract_json(members[_AUDIT_MANIFEST_PATH])
    receipt = validate_contract(document)
    if receipt.schema_name != "AuditBundle":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "archive manifest is not an AuditBundle")
    payload = cast("Mapping[str, Any]", document["payload"])
    if document["object_id"] != stable_id("bundle_", payload):
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "AuditBundle stable ID is invalid")
    entries = tuple(cast("Sequence[Mapping[str, Any]]", payload["entries"]))
    expected_names = tuple(
        sorted([cast("str", entry["path"]) for entry in entries] + list(_INTERNAL_PATHS))
    )
    if names != expected_names:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP member set differs from manifest"
        )
    for entry in entries:
        data = members[cast("str", entry["path"])]
        digest = digest_bytes(data)
        if digest.size_bytes != entry["size_bytes"] or digest.sha256 != entry["sha256"]:
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "bundle entry hash or size differs")
    privacy_bytes = members[_PRIVACY_REPORT_PATH]
    privacy_artifact = cast(
        "Mapping[str, Any]", cast("Mapping[str, Any]", payload["privacy_scan"])["report"]
    )
    if (
        privacy_artifact["path"] != _PRIVACY_REPORT_PATH
        or privacy_artifact["path_base"] != "bundle_root"
        or privacy_artifact["role"] != "privacy_report"
        or privacy_artifact["media_type"] != "application/json"
        or privacy_artifact["immutable"] is not True
        or privacy_artifact["confidentiality"] != payload["content_classification"]
    ):
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "privacy report artifact metadata differs",
        )
    privacy_digest = digest_bytes(privacy_bytes)
    if (
        privacy_digest.size_bytes != privacy_artifact["size_bytes"]
        or privacy_digest.sha256 != privacy_artifact["sha256"]
    ):
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "privacy report artifact differs")
    recomputed_report = _privacy_report(
        {cast("str", entry["path"]): members[cast("str", entry["path"])] for entry in entries}
    )
    if privacy_bytes != canonical_json(recomputed_report) + b"\n":
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "privacy scan report is not reproducible"
        )
    if cast("Mapping[str, Any]", payload["privacy_scan"])["status"] != recomputed_report["status"]:
        raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "privacy scan status differs")

    if expected_run_manifest is not None:
        run_receipt = validate_contract(expected_run_manifest)
        if run_receipt.schema_name != "RunManifest":
            raise ContractError(ErrorCode.SCHEMA_INVALID, "expected object is not a RunManifest")
        if (
            payload["run_manifest_sha256"] != run_receipt.payload_sha256
            or document["run_id"] != expected_run_manifest["run_id"]
        ):
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "RunManifest binding differs")
    if expected_verification_report is not None:
        verification_receipt = validate_contract(expected_verification_report)
        if verification_receipt.schema_name != "VerificationReport":
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "expected object is not a VerificationReport",
            )
        if (
            payload["verification_report_sha256"] != verification_receipt.payload_sha256
            or document["run_id"] != expected_verification_report["run_id"]
        ):
            raise ContractError(
                ErrorCode.BUNDLE_HASH_MISMATCH, "VerificationReport binding differs"
            )
    digest = digest_file(archive_path, max_bytes=limits.max_total_bytes)
    return BundleVerification(
        document=document,
        file_sha256=digest.sha256,
        size_bytes=digest.size_bytes,
        entry_count=len(entries),
    )


def _verify_sources_unchanged(items: Sequence[_PreparedItem], limits: BundleLimits) -> None:
    for item in items:
        if _is_link_or_junction(item.source_path):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "bundle source became a link")
        current = read_stable_bytes(item.source_path, max_bytes=limits.max_file_bytes)
        if current != item.data:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH, "bundle source changed during creation"
            )


def create_audit_bundle(
    source_root: str | Path,
    destination: str | Path,
    *,
    allowlist: Sequence[BundleItem | Mapping[str, Any]],
    run_manifest: Mapping[str, Any],
    verification_report: Mapping[str, Any],
    content_classification: str,
    generated_at: str,
    limits: BundleLimits = _DEFAULT_BUNDLE_LIMITS,
) -> BundleResult:
    """Create, verify and atomically publish one no-clobber audit ZIP."""

    run_receipt = validate_contract(run_manifest)
    verification_receipt = validate_contract(verification_report)
    if (
        run_receipt.schema_name != "RunManifest"
        or verification_receipt.schema_name != "VerificationReport"
    ):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "bundle inputs require RunManifest and VerificationReport objects",
        )
    if run_manifest["run_id"] != verification_report["run_id"]:
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH, "bundle inputs belong to different runs"
        )
    if content_classification not in {"public_fixture", "local_private"}:
        raise ContractError(
            ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD, "unknown bundle classification"
        )
    root = Path(source_root)
    prepared = _prepare_items(
        root,
        allowlist,
        run_id=cast("str", run_manifest["run_id"]),
        limits=limits,
    )
    file_members = {item.path: item.data for item in prepared}
    privacy_report = _privacy_report(file_members)
    findings = cast("Sequence[Mapping[str, Any]]", privacy_report["findings"])
    if content_classification == "public_fixture" and findings:
        raise ContractError(
            ErrorCode.BUNDLE_PRIVATE_RELEASE,
            "public bundle privacy scan found private-looking content",
            details={
                "finding_count": len(findings),
                "rule_ids": sorted({cast("str", item["rule_id"]) for item in findings}),
            },
        )
    privacy_bytes = canonical_json(privacy_report) + b"\n"
    entries = [
        {
            "path": item.path,
            "role": item.role,
            "size_bytes": len(item.data),
            "sha256": digest_bytes(item.data).sha256,
            "source_object": dict(item.source_object),
        }
        for item in prepared
    ]
    payload: dict[str, Any] = {
        "bundle_format_version": SCHEMA_VERSION,
        "run_manifest_sha256": run_receipt.payload_sha256,
        "verification_report_sha256": verification_receipt.payload_sha256,
        "content_classification": content_classification,
        "entries": entries,
        "excluded_entries": [],
        "privacy_scan": {
            "status": privacy_report["status"],
            "rules_version": _PRIVACY_RULES_VERSION,
            "report": _artifact(
                _PRIVACY_REPORT_PATH,
                privacy_bytes,
                content_classification,
            ),
        },
        "manifest_sha256": sha256_canonical(entries),
        "reproducibility": {
            "entry_order": "lexicographic_path",
            "timestamp_policy": _TIMESTAMP_POLICY,
            "tool": _producer(),
        },
    }
    bundle_id = stable_id("bundle_", payload)
    document = make_envelope(
        schema_name="AuditBundle",
        object_id=bundle_id,
        run_id=cast("str", run_manifest["run_id"]),
        generated_at=generated_at,
        producer=_producer(),
        payload=payload,
    )
    validate_contract(document)
    members = {
        **file_members,
        _AUDIT_MANIFEST_PATH: canonical_json(document) + b"\n",
        _PRIVACY_REPORT_PATH: privacy_bytes,
    }

    destination_path = Path(destination)
    if destination_path.suffix.casefold() != ".zip" or not destination_path.name:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID, "bundle destination must be an explicit .zip path"
        )
    try:
        parent = destination_path.parent.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID, "bundle destination parent is unavailable"
        ) from exc
    if not parent.is_dir():
        raise ContractError(
            ErrorCode.SCHEMA_INVALID, "bundle destination parent must be a directory"
        )
    target = parent / destination_path.name
    if target.resolve(strict=False).is_relative_to(resolved_root):
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL, "bundle destination cannot modify its source tree"
        )
    if target.exists() or target.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "bundle destination already exists")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.bundle-", suffix=".tmp", dir=parent
    )
    os.close(descriptor)
    staged = Path(temporary_name)
    published = False
    try:
        _write_zip(staged, members)
        staged_verification = verify_audit_bundle(
            staged,
            expected_run_manifest=run_manifest,
            expected_verification_report=verification_report,
            limits=limits,
        )
        _verify_sources_unchanged(prepared, limits)
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID, "bundle destination already exists"
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.BUNDLE_HASH_MISMATCH,
                "audit bundle could not be atomically published",
            ) from exc
        published = True
        final_verification = verify_audit_bundle(
            target,
            expected_run_manifest=run_manifest,
            expected_verification_report=verification_report,
            limits=limits,
        )
        if final_verification != staged_verification:
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "published audit bundle differs")
        _verify_sources_unchanged(prepared, limits)
    except Exception:
        if published and target.exists():
            try:
                if os.path.samefile(staged, target):
                    target.unlink()
            except OSError:
                pass
        raise
    finally:
        with suppress(FileNotFoundError):
            staged.unlink()
    return BundleResult(
        path=destination_path,
        document=document,
        file_sha256=final_verification.file_sha256,
        size_bytes=final_verification.size_bytes,
        entry_count=len(entries),
    )


build_audit_bundle = create_audit_bundle


__all__ = [
    "BundleItem",
    "BundleLimits",
    "BundleResult",
    "BundleVerification",
    "build_audit_bundle",
    "create_audit_bundle",
    "object_binding",
    "verify_audit_bundle",
]
