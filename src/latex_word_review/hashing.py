"""Stable file and source-tree hashing with drift detection."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.paths import validate_relative_path


@dataclass(frozen=True, slots=True)
class FileDigest:
    """Raw-byte digest and size for one regular file."""

    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """The exact fields bound by the source-tree hash contract."""

    path: str
    role: str
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "path": validate_relative_path(self.path),
            "role": self.role,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def _stat_fingerprint(path: Path) -> tuple[int, int, int, int]:
    status = path.stat()
    if not stat.S_ISREG(status.st_mode):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "expected a regular file")
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns


def read_stable_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read a bounded regular file and reject changes during the read."""

    if max_bytes <= 0:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "file size limit must be positive")
    try:
        before = _stat_fingerprint(path)
        if before[2] > max_bytes:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "file exceeds the configured size limit")
        data = bytearray()
        with path.open("rb") as stream:
            while chunk := stream.read(min(1024 * 1024, max_bytes + 1 - len(data))):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID,
                        "file exceeds the configured size limit",
                    )
        after = _stat_fingerprint(path)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "file cannot be read safely") from exc
    if before != after or len(data) != after[2]:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "file changed while being hashed")
    return bytes(data)


def digest_bytes(data: bytes | bytearray | memoryview) -> FileDigest:
    raw = bytes(data)
    return FileDigest(len(raw), f"sha256:{hashlib.sha256(raw).hexdigest()}")


def digest_file(path: Path, *, max_bytes: int) -> FileDigest:
    """Hash a bounded file's exact bytes."""

    return digest_bytes(read_stable_bytes(path, max_bytes=max_bytes))


def source_tree_sha256(entries: Iterable[TreeEntry]) -> str:
    """Hash sorted contract records using RFC 8785 canonical JSON."""

    records = [entry.as_dict() for entry in entries]
    records.sort(key=lambda record: str(record["path"]))
    paths = [str(record["path"]) for record in records]
    if len(paths) != len(set(paths)):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source tree contains duplicate paths")
    return sha256_canonical(records)


def verify_file_digest(path: Path, expected: FileDigest, *, max_bytes: int) -> None:
    """Raise a stable drift error unless size and raw-byte hash both match."""

    actual = digest_file(path, max_bytes=max_bytes)
    if actual != expected:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "file digest no longer matches")


__all__ = [
    "FileDigest",
    "TreeEntry",
    "digest_bytes",
    "digest_file",
    "read_stable_bytes",
    "source_tree_sha256",
    "verify_file_digest",
]
