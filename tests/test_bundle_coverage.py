"""Security-focused branch tests for deterministic offline audit bundles."""

from __future__ import annotations

import copy
import os
import stat
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.bundle as bundle_module
from latex_word_review.bundle import (
    BundleItem,
    BundleLimits,
    create_audit_bundle,
    object_binding,
    verify_audit_bundle,
)
from latex_word_review.canonical import canonical_json, seal_envelope
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import stable_id
from tests.test_contracts import _golden_contracts
from tests.test_ledger_bundle import _seal_verification_extensions

GENERATED_AT = "2026-07-16T15:00:00+09:00"


def _documents() -> dict[str, dict[str, Any]]:
    return _seal_verification_extensions(_golden_contracts())


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> None:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


def _source_and_item(tmp_path: Path) -> tuple[Path, BundleItem]:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.json").write_bytes(b'{"safe":true}\n')
    return source, BundleItem("evidence.json", "evidence", documents["ChangeSet"])


def _make_valid_bundle(tmp_path: Path) -> tuple[Path, dict[str, dict[str, Any]]]:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.json").write_bytes(b'{"safe":true}\n')
    destination = tmp_path / "audit.zip"
    create_audit_bundle(
        source,
        destination,
        allowlist=[BundleItem("evidence.json", "evidence", documents["ChangeSet"])],
        run_manifest=documents["RunManifest"],
        verification_report=documents["VerificationReport"],
        content_classification="public_fixture",
        generated_at=GENERATED_AT,
    )
    return destination, documents


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _rewrite_manifest(path: Path, document: dict[str, Any]) -> None:
    members = _members(path)
    members[bundle_module._AUDIT_MANIFEST_PATH] = canonical_json(document) + b"\n"
    path.unlink()
    bundle_module._write_zip(path, members)


def _reseal_bundle_document(document: dict[str, Any]) -> dict[str, Any]:
    payload = cast("dict[str, Any]", document["payload"])
    document["object_id"] = stable_id("bundle_", payload)
    return seal_envelope(document)


@pytest.mark.parametrize(
    "values",
    [
        {"max_entries": 0},
        {"max_file_bytes": 0},
        {"max_total_bytes": 0},
        {"max_compression_ratio": 0},
    ],
)
def test_bundle_limits_must_be_positive(values: dict[str, int]) -> None:
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: BundleLimits(**values))


def test_object_binding_and_mapping_allowlist_are_validated(tmp_path: Path) -> None:
    documents = _documents()
    binding = object_binding(documents["ChangeSet"])
    assert binding["schema_name"] == "ChangeSet"
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.json").write_bytes(b"safe\n")
    result = create_audit_bundle(
        source,
        tmp_path / "mapping.zip",
        allowlist=[
            {
                "path": "evidence.json",
                "role": "evidence",
                "source_object": documents["ChangeSet"],
            }
        ],
        run_manifest=documents["RunManifest"],
        verification_report=documents["VerificationReport"],
        content_classification="local_private",
        generated_at=GENERATED_AT,
    )
    assert result.entry_count == 1


@pytest.mark.parametrize(
    ("allowlist_factory", "code"),
    [
        (lambda docs: [{"path": "evidence.json"}], ErrorCode.SCHEMA_INVALID),
        (
            lambda docs: [BundleItem("manifest/audit-bundle.json", "x", docs["ChangeSet"])],
            ErrorCode.PATH_TRAVERSAL,
        ),
        (
            lambda docs: [
                BundleItem("evidence.json", "x", docs["ChangeSet"]),
                BundleItem("evidence.json", "y", docs["ChangeSet"]),
            ],
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda docs: [BundleItem("evidence.json", "", docs["ChangeSet"])],
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda docs: [BundleItem("directory", "x", docs["ChangeSet"])],
            ErrorCode.SCHEMA_INVALID,
        ),
    ],
)
def test_allowlist_rejects_malformed_reserved_duplicate_and_nonfiles(
    tmp_path: Path,
    allowlist_factory: Callable[[dict[str, dict[str, Any]]], list[object]],
    code: ErrorCode,
) -> None:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.json").write_bytes(b"safe")
    (source / "directory").mkdir()
    allowlist = cast("list[BundleItem | dict[str, Any]]", allowlist_factory(documents))
    _assert_error(
        code,
        lambda: create_audit_bundle(
            source,
            tmp_path / "rejected.zip",
            allowlist=allowlist,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    assert not (tmp_path / "rejected.zip").exists()


def test_allowlist_rejects_wrong_run_and_resource_limits(tmp_path: Path) -> None:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"aa")
    (source / "b.txt").write_bytes(b"bb")
    foreign = copy.deepcopy(documents["ChangeSet"])
    foreign["run_id"] = "run_019bc0ab-2400-7000-8000-000000000099"
    foreign = seal_envelope(foreign)

    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "foreign.zip",
            allowlist=[BundleItem("a.txt", "x", foreign)],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            tmp_path / "entries.zip",
            allowlist=[
                BundleItem("a.txt", "x", documents["ChangeSet"]),
                BundleItem("b.txt", "x", documents["ChangeSet"]),
            ],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
            limits=BundleLimits(max_entries=1),
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            tmp_path / "bytes.zip",
            allowlist=[
                BundleItem("a.txt", "x", documents["ChangeSet"]),
                BundleItem("b.txt", "x", documents["ChangeSet"]),
            ],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
            limits=BundleLimits(max_total_bytes=3),
        ),
    )


def test_allowlist_root_and_link_state_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _documents()
    missing = tmp_path / "missing"
    item = BundleItem("evidence.json", "x", documents["ChangeSet"])
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: bundle_module._prepare_items(
            missing,
            [item],
            run_id=cast("str", documents["RunManifest"]["run_id"]),
            limits=BundleLimits(),
        ),
    )
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: bundle_module._prepare_items(
            regular,
            [item],
            run_id=cast("str", documents["RunManifest"]["run_id"]),
            limits=BundleLimits(),
        ),
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.json").write_bytes(b"safe")
    monkeypatch.setattr(bundle_module, "_is_link_or_junction", lambda path: path == source)
    _assert_error(
        ErrorCode.PATH_LINK_ESCAPE,
        lambda: bundle_module._prepare_items(
            source,
            [item],
            run_id=cast("str", documents["RunManifest"]["run_id"]),
            limits=BundleLimits(),
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"/home/alice/paper.tex",
        b"/Users/Alice/paper.tex",
        b"alice@example.org",
        b"-----BEGIN PRIVATE KEY-----",
        b"sk-proj-abcdefghijklmnop",
    ],
)
def test_privacy_rules_cover_public_release_secrets(tmp_path: Path, payload: bytes) -> None:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.txt").write_bytes(payload)
    _assert_error(
        ErrorCode.BUNDLE_PRIVATE_RELEASE,
        lambda: create_audit_bundle(
            source,
            tmp_path / "private.zip",
            allowlist=[BundleItem("evidence.txt", "evidence", documents["ChangeSet"])],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        ),
    )


def _valid_info(name: str = "file.txt") -> zipfile.ZipInfo:
    info = bundle_module._zip_info(name)
    info.file_size = 1
    info.compress_size = 1
    return info


def _unsafe_path(info: zipfile.ZipInfo) -> None:
    info.filename = "../file.txt"


def _directory(info: zipfile.ZipInfo) -> None:
    info.filename = "directory/"


def _encrypted(info: zipfile.ZipInfo) -> None:
    info.flag_bits |= 0x1


def _stored(info: zipfile.ZipInfo) -> None:
    info.compress_type = zipfile.ZIP_STORED


def _symlink(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFLNK | 0o777) << 16


def _oversized(info: zipfile.ZipInfo) -> None:
    info.file_size = 11


def _zero_compressed(info: zipfile.ZipInfo) -> None:
    info.compress_size = 0


def _ratio(info: zipfile.ZipInfo) -> None:
    info.file_size = 10
    info.compress_size = 1


def _metadata(info: zipfile.ZipInfo) -> None:
    info.date_time = (1981, 1, 1, 0, 0, 0)


@pytest.mark.parametrize(
    ("mutator", "code", "limits"),
    [
        (_unsafe_path, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits()),
        (_directory, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits()),
        (_encrypted, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits()),
        (_stored, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits()),
        (_symlink, ErrorCode.PATH_LINK_ESCAPE, BundleLimits()),
        (_oversized, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits(max_file_bytes=10)),
        (_zero_compressed, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits()),
        (_ratio, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits(max_compression_ratio=2)),
        (_metadata, ErrorCode.BUNDLE_HASH_MISMATCH, BundleLimits()),
    ],
)
def test_zip_member_validation_rejects_unsafe_metadata(
    mutator: Callable[[zipfile.ZipInfo], None],
    code: ErrorCode,
    limits: BundleLimits,
) -> None:
    info = _valid_info()
    mutator(info)
    _assert_error(code, lambda: bundle_module._validate_zip_member(info, limits))


def _write_raw_zip(path: Path, names: list[str], data: bytes = b"x") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(bundle_module._zip_info(name), data)


def test_zip_reader_rejects_invalid_duplicate_unsorted_and_resource_excess(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: bundle_module._read_verified_members(corrupt, BundleLimits()),
    )

    duplicate = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _write_raw_zip(duplicate, ["same.txt", "same.txt"])
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: bundle_module._read_verified_members(duplicate, BundleLimits()),
    )

    unsorted = tmp_path / "unsorted.zip"
    _write_raw_zip(unsorted, ["z.txt", "a.txt"])
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: bundle_module._read_verified_members(unsorted, BundleLimits()),
    )

    many = tmp_path / "many.zip"
    _write_raw_zip(many, ["a", "b", "c", "d"])
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: bundle_module._read_verified_members(many, BundleLimits(max_entries=1)),
    )

    total = tmp_path / "total.zip"
    _write_raw_zip(total, ["a", "b"], b"xx")
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: bundle_module._read_verified_members(total, BundleLimits(max_total_bytes=3)),
    )


def test_bundle_verification_rejects_missing_wrong_and_resealed_tamper(tmp_path: Path) -> None:
    missing = tmp_path / "missing.zip"
    _write_raw_zip(missing, ["evidence.txt"])
    _assert_error(ErrorCode.BUNDLE_HASH_MISMATCH, lambda: verify_audit_bundle(missing))

    valid, documents = _make_valid_bundle(tmp_path)
    wrong = tmp_path / "wrong-schema.zip"
    wrong.write_bytes(valid.read_bytes())
    members = _members(wrong)
    members[bundle_module._AUDIT_MANIFEST_PATH] = canonical_json(documents["ChangeSet"]) + b"\n"
    wrong.unlink()
    bundle_module._write_zip(wrong, members)
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: verify_audit_bundle(wrong))

    stable = tmp_path / "stable-id.zip"
    stable.write_bytes(valid.read_bytes())
    document = cast(
        "dict[str, Any]",
        __import__("json").loads(_members(stable)[bundle_module._AUDIT_MANIFEST_PATH]),
    )
    original_id = cast("str", document["object_id"])
    document["object_id"] = original_id[:-1] + ("a" if original_id[-1] != "a" else "b")
    document = seal_envelope(document)
    _rewrite_manifest(stable, document)
    _assert_error(ErrorCode.BUNDLE_HASH_MISMATCH, lambda: verify_audit_bundle(stable))


def test_bundle_verification_rejects_member_hash_privacy_and_expected_bindings(
    tmp_path: Path,
) -> None:
    valid, documents = _make_valid_bundle(tmp_path)

    member_set = tmp_path / "member-set.zip"
    member_set.write_bytes(valid.read_bytes())
    document = cast(
        "dict[str, Any]",
        __import__("json").loads(_members(member_set)[bundle_module._AUDIT_MANIFEST_PATH]),
    )
    cast("list[dict[str, Any]]", document["payload"]["entries"]).clear()
    _rewrite_manifest(member_set, _reseal_bundle_document(document))
    _assert_error(ErrorCode.BUNDLE_HASH_MISMATCH, lambda: verify_audit_bundle(member_set))

    bad_hash = tmp_path / "bad-hash.zip"
    bad_hash.write_bytes(valid.read_bytes())
    document = cast(
        "dict[str, Any]",
        __import__("json").loads(_members(bad_hash)[bundle_module._AUDIT_MANIFEST_PATH]),
    )
    entry = cast("list[dict[str, Any]]", document["payload"]["entries"])[0]
    entry["sha256"] = "sha256:" + "0" * 64
    _rewrite_manifest(bad_hash, _reseal_bundle_document(document))
    _assert_error(ErrorCode.BUNDLE_HASH_MISMATCH, lambda: verify_audit_bundle(bad_hash))

    privacy = tmp_path / "privacy.zip"
    privacy.write_bytes(valid.read_bytes())
    document = cast(
        "dict[str, Any]",
        __import__("json").loads(_members(privacy)[bundle_module._AUDIT_MANIFEST_PATH]),
    )
    report = cast("dict[str, Any]", document["payload"]["privacy_scan"]["report"])
    report["role"] = "not_privacy_report"
    _rewrite_manifest(privacy, _reseal_bundle_document(document))
    _assert_error(ErrorCode.BUNDLE_HASH_MISMATCH, lambda: verify_audit_bundle(privacy))

    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: verify_audit_bundle(valid, expected_run_manifest=documents["ChangeSet"]),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: verify_audit_bundle(
            valid,
            expected_verification_report=documents["ChangeSet"],
        ),
    )


def test_bundle_creation_rejects_input_destination_and_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _documents()
    source, item = _source_and_item(tmp_path)
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            tmp_path / "schema.zip",
            allowlist=[item],
            run_manifest=documents["ChangeSet"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )

    different = copy.deepcopy(documents["VerificationReport"])
    different["run_id"] = "run_019bc0ab-2400-7000-8000-000000000099"
    different = seal_envelope(different)
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "runs.zip",
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=different,
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD,
        lambda: create_audit_bundle(
            source,
            tmp_path / "class.zip",
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="secret",
            generated_at=GENERATED_AT,
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            tmp_path / "wrong.txt",
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            tmp_path / "missing/out.zip",
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    _assert_error(
        ErrorCode.PATH_TRAVERSAL,
        lambda: create_audit_bundle(
            source,
            source / "inside.zip",
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )

    original_write_zip = bundle_module._write_zip

    def mutate_after_staging(path: Path, members: dict[str, bytes]) -> None:
        original_write_zip(path, members)
        (source / "evidence.json").write_bytes(b"drift")

    monkeypatch.setattr(bundle_module, "_write_zip", mutate_after_staging)
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "drift.zip",
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    assert not (tmp_path / "drift.zip").exists()


def test_bundle_publish_race_preserves_competing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _documents()
    source, item = _source_and_item(tmp_path)
    destination = tmp_path / "race.zip"
    original_link = os.link

    def race_link(source_path: Path, target_path: Path, *, follow_symlinks: bool) -> None:
        del source_path, follow_symlinks
        target_path.write_bytes(b"competitor")
        raise FileExistsError("race")

    monkeypatch.setattr(os, "link", race_link)
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            destination,
            allowlist=[item],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    monkeypatch.setattr(os, "link", original_link)
    assert destination.read_bytes() == b"competitor"
