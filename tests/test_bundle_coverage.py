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
from latex_word_review.canonical import canonical_json, seal_envelope, sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.ids import derive_artifact_id, stable_id
from tests.test_contracts import _golden_contracts
from tests.test_ledger_bundle import _bundle_artifact, _seal_verification_extensions

GENERATED_AT = "2026-07-16T15:00:00+09:00"


def _documents() -> dict[str, dict[str, Any]]:
    documents = _seal_verification_extensions(_golden_contracts())
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["payload"]["object_bindings"] = [object_binding(documents["VerificationReport"])]
    documents["RunManifest"] = seal_envelope(run_manifest)
    return documents


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> None:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


def _bound_source(
    tmp_path: Path,
    documents: dict[str, dict[str, Any]],
    files: list[tuple[str, str, bytes]],
) -> tuple[Path, tuple[BundleItem, ...]]:
    source = tmp_path / "source"
    source.mkdir()
    artifacts = [_bundle_artifact(path, role, data) for path, role, data in files]
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["payload"]["artifacts"] = artifacts
    run_manifest = seal_envelope(run_manifest)
    documents["RunManifest"] = run_manifest
    for path, _role, data in files:
        (source / path).write_bytes(data)
    (source / "run-manifest.json").write_bytes(canonical_json(run_manifest) + b"\n")
    verification = documents["VerificationReport"]
    (source / "verification-report.json").write_bytes(canonical_json(verification) + b"\n")
    items = [
        BundleItem(path, role, run_manifest, artifact["artifact_id"])
        for (path, role, _data), artifact in zip(files, artifacts, strict=True)
    ]
    items.extend(
        (
            BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"),
            BundleItem(
                "verification-report.json",
                "verification_report",
                verification,
                "$document",
            ),
        )
    )
    return source, tuple(items)


def _source_and_items(
    tmp_path: Path, documents: dict[str, dict[str, Any]]
) -> tuple[Path, tuple[BundleItem, ...]]:
    return _bound_source(tmp_path, documents, [("evidence.json", "evidence", b'{"safe":true}\n')])


def _make_valid_bundle(tmp_path: Path) -> tuple[Path, dict[str, dict[str, Any]]]:
    documents = _documents()
    source, allowlist = _source_and_items(tmp_path, documents)
    destination = tmp_path / "audit.zip"
    create_audit_bundle(
        source,
        destination,
        allowlist=allowlist,
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


def test_bundle_rejects_tampered_file_with_valid_source_contract(tmp_path: Path) -> None:
    documents = _documents()
    source, allowlist = _source_and_items(tmp_path, documents)
    (source / "evidence.json").write_bytes(b'{"tampered":true}\n')
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "tampered-source.zip",
            allowlist=allowlist,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        ),
    )


def test_offline_verifier_rejects_resealed_member_not_bound_by_artifact_ref(
    tmp_path: Path,
) -> None:
    valid, _documents_value = _make_valid_bundle(tmp_path)
    members = _members(valid)
    document = cast(
        "dict[str, Any]",
        __import__("json").loads(members[bundle_module._AUDIT_MANIFEST_PATH]),
    )
    payload = cast("dict[str, Any]", document["payload"])
    entries = cast("list[dict[str, Any]]", payload["entries"])
    evidence = next(entry for entry in entries if entry["path"] == "evidence.json")
    tampered = b'{"tampered":true}\n'
    tampered_digest = digest_bytes(tampered)
    members["evidence.json"] = tampered
    evidence["size_bytes"] = tampered_digest.size_bytes
    evidence["sha256"] = tampered_digest.sha256
    payload["manifest_sha256"] = sha256_canonical(entries)

    privacy_report = bundle_module._privacy_report(
        {cast("str", entry["path"]): members[cast("str", entry["path"])] for entry in entries}
    )
    privacy_bytes = canonical_json(privacy_report) + b"\n"
    members[bundle_module._PRIVACY_REPORT_PATH] = privacy_bytes
    privacy_artifact = cast("dict[str, Any]", payload["privacy_scan"]["report"])
    privacy_digest = digest_bytes(privacy_bytes)
    privacy_artifact["size_bytes"] = privacy_digest.size_bytes
    privacy_artifact["sha256"] = privacy_digest.sha256
    payload["privacy_scan"]["status"] = privacy_report["status"]

    forged = tmp_path / "resealed-unbound.zip"
    document = _reseal_bundle_document(document)
    members[bundle_module._AUDIT_MANIFEST_PATH] = canonical_json(document) + b"\n"
    bundle_module._write_zip(forged, members)
    _assert_error(ErrorCode.BUNDLE_HASH_MISMATCH, lambda: verify_audit_bundle(forged))


def test_bundle_rejects_missing_artifact_selector(tmp_path: Path) -> None:
    documents = _documents()
    source, allowlist = _source_and_items(tmp_path, documents)
    evidence = allowlist[0]
    wrong_selector = derive_artifact_id(digest_bytes(b"absent artifact").sha256)
    rejected = (
        BundleItem(evidence.path, evidence.role, evidence.source_object, wrong_selector),
        *allowlist[1:],
    )
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "wrong-selector.zip",
            allowlist=rejected,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        ),
    )


def test_bundle_rejects_unregistered_same_run_source_contract(tmp_path: Path) -> None:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    evidence_bytes = b'{"safe":true}\n'
    (source / "evidence.json").write_bytes(evidence_bytes)
    artifact = _bundle_artifact("evidence.json", "evidence", evidence_bytes)
    changeset = copy.deepcopy(documents["ChangeSet"])
    changeset["extensions"]["org.latex-word-review.bundle-artifacts"] = {"artifacts": [artifact]}
    changeset = seal_envelope(changeset)
    (source / "changeset.json").write_bytes(canonical_json(changeset) + b"\n")
    (source / "run-manifest.json").write_bytes(canonical_json(documents["RunManifest"]) + b"\n")
    (source / "verification-report.json").write_bytes(
        canonical_json(documents["VerificationReport"]) + b"\n"
    )
    allowlist = (
        BundleItem("changeset.json", "changeset", changeset, "$document"),
        BundleItem("evidence.json", "evidence", changeset, artifact["artifact_id"]),
        BundleItem(
            "run-manifest.json",
            "run_manifest",
            documents["RunManifest"],
            "$document",
        ),
        BundleItem(
            "verification-report.json",
            "verification_report",
            documents["VerificationReport"],
            "$document",
        ),
    )
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "unregistered-source.zip",
            allowlist=allowlist,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        ),
    )


def test_bundle_rejects_run_manifest_extension_as_artifact_authority(tmp_path: Path) -> None:
    documents = _documents()
    source = tmp_path / "source"
    source.mkdir()
    evidence_bytes = b'{"safe":true}\n'
    (source / "evidence.json").write_bytes(evidence_bytes)
    artifact = _bundle_artifact("evidence.json", "evidence", evidence_bytes)
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["extensions"]["org.latex-word-review.bundle-artifacts"] = {"artifacts": [artifact]}
    run_manifest = seal_envelope(run_manifest)
    (source / "run-manifest.json").write_bytes(canonical_json(run_manifest) + b"\n")
    (source / "verification-report.json").write_bytes(
        canonical_json(documents["VerificationReport"]) + b"\n"
    )
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / "extension-authority.zip",
            allowlist=(
                BundleItem("evidence.json", "evidence", run_manifest, artifact["artifact_id"]),
                BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"),
                BundleItem(
                    "verification-report.json",
                    "verification_report",
                    documents["VerificationReport"],
                    "$document",
                ),
            ),
            run_manifest=run_manifest,
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        ),
    )


@pytest.mark.parametrize("field", ["path", "size_bytes", "sha256"])
def test_bundle_rejects_artifact_ref_metadata_mismatch(tmp_path: Path, field: str) -> None:
    documents = _documents()
    source, _allowlist = _source_and_items(tmp_path, documents)
    run_manifest = copy.deepcopy(documents["RunManifest"])
    artifact = cast("dict[str, Any]", run_manifest["payload"]["artifacts"][0])
    if field == "path":
        artifact["path"] = "different.json"
    elif field == "size_bytes":
        artifact["size_bytes"] += 1
    else:
        artifact["sha256"] = digest_bytes(b"different bytes").sha256
        artifact["artifact_id"] = derive_artifact_id(artifact["sha256"])
    run_manifest = seal_envelope(run_manifest)
    documents["RunManifest"] = run_manifest
    (source / "run-manifest.json").write_bytes(canonical_json(run_manifest) + b"\n")
    allowlist = (
        BundleItem(
            "evidence.json",
            "evidence",
            run_manifest,
            cast("str", artifact["artifact_id"]),
        ),
        BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"),
        BundleItem(
            "verification-report.json",
            "verification_report",
            documents["VerificationReport"],
            "$document",
        ),
    )
    _assert_error(
        ErrorCode.BUNDLE_HASH_MISMATCH,
        lambda: create_audit_bundle(
            source,
            tmp_path / f"wrong-{field}.zip",
            allowlist=allowlist,
            run_manifest=run_manifest,
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        ),
    )


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
    source, allowlist = _bound_source(
        tmp_path, documents, [("evidence.json", "evidence", b"safe\n")]
    )
    mapping_allowlist = [
        {
            "path": item.path,
            "role": item.role,
            "source_object": item.source_object,
            "artifact_selector": item.artifact_selector,
        }
        for item in allowlist
    ]
    result = create_audit_bundle(
        source,
        tmp_path / "mapping.zip",
        allowlist=mapping_allowlist,
        run_manifest=documents["RunManifest"],
        verification_report=documents["VerificationReport"],
        content_classification="local_private",
        generated_at=GENERATED_AT,
    )
    assert result.entry_count == 3


@pytest.mark.parametrize(
    ("allowlist_factory", "code"),
    [
        (lambda docs: [{"path": "evidence.json"}], ErrorCode.SCHEMA_INVALID),
        (
            lambda docs: [BundleItem("manifest/audit-bundle.json", "x", docs["RunManifest"])],
            ErrorCode.PATH_TRAVERSAL,
        ),
        (
            lambda docs: [
                BundleItem("evidence.json", "x", docs["RunManifest"]),
                BundleItem("evidence.json", "y", docs["RunManifest"]),
            ],
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda docs: [BundleItem("evidence.json", "", docs["RunManifest"])],
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            lambda docs: [BundleItem("directory", "x", docs["RunManifest"])],
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
    evidence = b"safe"
    (source / "evidence.json").write_bytes(evidence)
    (source / "directory").mkdir()
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["payload"]["artifacts"] = [_bundle_artifact("evidence.json", "x", evidence)]
    documents["RunManifest"] = seal_envelope(run_manifest)
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
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["payload"]["artifacts"] = [
        _bundle_artifact("a.txt", "x", b"aa"),
        _bundle_artifact("b.txt", "x", b"bb"),
    ]
    documents["RunManifest"] = seal_envelope(run_manifest)
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
                BundleItem("a.txt", "x", documents["RunManifest"]),
                BundleItem("b.txt", "x", documents["RunManifest"]),
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
                BundleItem("a.txt", "x", documents["RunManifest"]),
                BundleItem("b.txt", "x", documents["RunManifest"]),
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
    source, allowlist = _bound_source(tmp_path, documents, [("evidence.txt", "evidence", payload)])
    _assert_error(
        ErrorCode.BUNDLE_PRIVATE_RELEASE,
        lambda: create_audit_bundle(
            source,
            tmp_path / "private.zip",
            allowlist=allowlist,
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
    source, allowlist = _source_and_items(tmp_path, documents)
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: create_audit_bundle(
            source,
            tmp_path / "schema.zip",
            allowlist=allowlist,
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
            allowlist=allowlist,
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
            allowlist=allowlist,
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
            allowlist=allowlist,
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
            allowlist=allowlist,
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
            allowlist=allowlist,
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
            allowlist=allowlist,
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
    source, allowlist = _source_and_items(tmp_path, documents)
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
            allowlist=allowlist,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="local_private",
            generated_at=GENERATED_AT,
        ),
    )
    monkeypatch.setattr(os, "link", original_link)
    assert destination.read_bytes() == b"competitor"
