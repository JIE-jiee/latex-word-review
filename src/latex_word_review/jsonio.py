"""Bounded JSON reads and atomic no-clobber publication helpers."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from latex_word_review.canonical import canonical_json
from latex_word_review.contracts import load_contract_json, validate_contract
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes


def read_json_file(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    return load_contract_json(read_stable_bytes(path, max_bytes=max_bytes))


def read_contract_file(
    path: Path,
    *,
    expected_schema: str | None = None,
    max_bytes: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    document = read_json_file(path, max_bytes=max_bytes)
    receipt = validate_contract(document)
    if expected_schema is not None and receipt.schema_name != expected_schema:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"expected {expected_schema}, received {receipt.schema_name}",
        )
    return document


def write_new_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> Path:
    """Publish bytes to an explicit unused path without an overwrite race."""

    if not path.name:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output path must name a file")
    if path.parent.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "output parent must not be a link")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output parent is unavailable") from exc
    if not parent.is_dir() or parent.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "output parent must be a real directory")
    target = parent / path.name
    if target.exists() or target.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output path already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.publish-",
        suffix=".tmp",
        dir=parent,
    )
    staged = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(mode)
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "output path already exists") from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT, "output could not be published"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            staged.unlink()
    return target


def write_new_json(path: Path, document: dict[str, Any], *, contract: bool = False) -> Path:
    if path.suffix.lower() != ".json":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "JSON output must have a .json suffix")
    if contract:
        validate_contract(document)
    return write_new_bytes(path, canonical_json(document) + b"\n")


__all__ = ["read_contract_file", "read_json_file", "write_new_bytes", "write_new_json"]
