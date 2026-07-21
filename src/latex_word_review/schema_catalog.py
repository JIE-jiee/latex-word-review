"""Authoritative packaged JSON Schema catalog and byte-integrity checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any, Final

SCHEMA_PACKAGE: Final = "latex_word_review.schemas.v1alpha"
SCHEMA_CATALOG_FILENAME: Final = "catalog.json"
_MAX_CATALOG_BYTES: Final = 64 * 1024
_MAX_SCHEMA_BYTES: Final = 2 * 1024 * 1024
_SCHEMA_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
_SCHEMA_FILENAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.schema\.json$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True, slots=True)
class SchemaResource:
    """One named contract schema bound to an exact packaged file."""

    name: str
    filename: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SchemaCatalog:
    """The complete supported contract schema set for one exact version."""

    schema_version: str
    common: SchemaResource
    objects: tuple[SchemaResource, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"packaged schema catalog has duplicate key: {key!r}")
        result[key] = value
    return result


def _read_bounded_resource(filename: str, *, limit: int) -> bytes:
    try:
        data = resources.files(SCHEMA_PACKAGE).joinpath(filename).read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"packaged schema resource is unavailable: {filename}") from exc
    if not data or len(data) > limit:
        raise RuntimeError(f"packaged schema resource size is invalid: {filename}")
    return data


def _catalog_entry(value: object, *, common: bool = False) -> SchemaResource:
    if not isinstance(value, dict):
        raise RuntimeError("packaged schema catalog entry must be an object")
    expected_keys = {"file", "sha256"} if common else {"name", "file", "sha256"}
    if set(value) != expected_keys:
        raise RuntimeError("packaged schema catalog entry has unexpected fields")
    name = "Common" if common else value.get("name")
    filename = value.get("file")
    sha256 = value.get("sha256")
    if not isinstance(name, str) or not _SCHEMA_NAME_RE.fullmatch(name):
        raise RuntimeError("packaged schema catalog has an invalid object name")
    if not isinstance(filename, str) or not _SCHEMA_FILENAME_RE.fullmatch(filename):
        raise RuntimeError("packaged schema catalog has an invalid filename")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise RuntimeError("packaged schema catalog has an invalid SHA-256")
    return SchemaResource(name=name, filename=filename, sha256=sha256)


@cache
def schema_catalog() -> SchemaCatalog:
    """Load and fully verify the sole authoritative current schema catalog."""

    raw = _read_bounded_resource(SCHEMA_CATALOG_FILENAME, limit=_MAX_CATALOG_BYTES)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("packaged schema catalog is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "catalog_format",
        "schema_version",
        "common",
        "objects",
    }:
        raise RuntimeError("packaged schema catalog has an invalid root shape")
    if type(value["catalog_format"]) is not int or value["catalog_format"] != 1:
        raise RuntimeError("packaged schema catalog format is unsupported")
    schema_version = value["schema_version"]
    match = _SEMVER_RE.fullmatch(schema_version) if isinstance(schema_version, str) else None
    if match is None:
        raise RuntimeError("packaged schema catalog version is not valid SemVer")
    common = _catalog_entry(value["common"], common=True)
    raw_objects = value["objects"]
    if not isinstance(raw_objects, list) or not 1 <= len(raw_objects) <= 64:
        raise RuntimeError("packaged schema catalog object count is invalid")
    objects = tuple(_catalog_entry(entry) for entry in raw_objects)
    names = [entry.name for entry in objects]
    filenames = [common.filename, *(entry.filename for entry in objects)]
    if len(names) != len(set(names)) or len(filenames) != len(set(filenames)):
        raise RuntimeError("packaged schema catalog contains duplicate identities")

    expected_json = {SCHEMA_CATALOG_FILENAME, *filenames}
    try:
        actual_json = {
            item.name
            for item in resources.files(SCHEMA_PACKAGE).iterdir()
            if item.is_file() and item.name.endswith(".json")
        }
    except OSError as exc:
        raise RuntimeError("packaged schema directory cannot be enumerated") from exc
    if actual_json != expected_json:
        raise RuntimeError("packaged JSON Schema set differs from its catalog")

    for entry in (common, *objects):
        data = _read_bounded_resource(entry.filename, limit=_MAX_SCHEMA_BYTES)
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise RuntimeError(f"packaged JSON Schema differs from its catalog: {entry.filename}")
    return SchemaCatalog(
        schema_version=schema_version,
        common=common,
        objects=objects,
    )


@cache
def read_schema_resource(filename: str) -> bytes:
    """Read one cataloged schema after verifying the complete catalog."""

    catalog = schema_catalog()
    entries = (catalog.common, *catalog.objects)
    matching = next((entry for entry in entries if entry.filename == filename), None)
    if matching is None:
        raise RuntimeError(f"schema resource is not cataloged: {filename}")
    data = _read_bounded_resource(filename, limit=_MAX_SCHEMA_BYTES)
    if hashlib.sha256(data).hexdigest() != matching.sha256:
        raise RuntimeError(f"packaged JSON Schema differs from its catalog: {filename}")
    return data


_CATALOG: Final = schema_catalog()
SCHEMA_VERSION: Final = _CATALOG.schema_version
SCHEMA_MAJOR: Final = int(_SEMVER_RE.fullmatch(SCHEMA_VERSION).group("major"))  # type: ignore[union-attr]
COMMON_SCHEMA_FILENAME: Final = _CATALOG.common.filename
SCHEMA_FILES: Final[Mapping[str, str]] = MappingProxyType(
    {entry.name: entry.filename for entry in _CATALOG.objects}
)


__all__ = [
    "COMMON_SCHEMA_FILENAME",
    "SCHEMA_CATALOG_FILENAME",
    "SCHEMA_FILES",
    "SCHEMA_MAJOR",
    "SCHEMA_PACKAGE",
    "SCHEMA_VERSION",
    "SchemaCatalog",
    "SchemaResource",
    "read_schema_resource",
    "schema_catalog",
]
