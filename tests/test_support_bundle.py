"""Redacted, bounded, atomic support-bundle contract tests."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.support_bundle as support_bundle_module
from latex_word_review.__about__ import __version__
from latex_word_review.canonical import canonical_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.support_bundle import (
    DEFAULT_SUPPORT_BUNDLE_LIMITS,
    SUPPORT_BUNDLE_FILENAME,
    SupportBundleLimits,
    SupportBundleRequest,
    SupportBundleResult,
    SupportBundleVerification,
    build_support_bundle_bytes,
    create_support_bundle,
    verify_support_bundle,
    verify_support_bundle_bytes,
)

_EXPECTED_FIELDS = {
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


def _request(
    *,
    error_code: ErrorCode | str = ErrorCode.BACKEND_FAILED,
    stage: str = "export_review",
    build_identifier: str = "commit-deadbeef01234567",
    task_identifier: str = "session_0123456789abcdef0123456789abcdef",
    duration_ms: int | None = 12_345,
    exception_type: str | None = "PermissionError",
    winerror: int | None = 5,
) -> SupportBundleRequest:
    return SupportBundleRequest(
        error_code=error_code,
        stage=stage,
        build_identifier=build_identifier,
        task_identifier=task_identifier,
        duration_ms=duration_ms,
        exception_type=exception_type,
        winerror=winerror,
    )


def _destination(root: Path) -> Path:
    return root / SUPPORT_BUNDLE_FILENAME


def _temporary_stages(root: Path) -> tuple[Path, ...]:
    return tuple(root.glob(f".{SUPPORT_BUNDLE_FILENAME}.support-*.tmp"))


def _fixed_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path, mode="r") as archive:
        return {
            "README.txt": archive.read("README.txt"),
            "support-metadata.json": archive.read("support-metadata.json"),
        }


def _write_fixed_archive(path: Path, members: dict[str, bytes]) -> None:
    path.write_bytes(support_bundle_module._build_archive_bytes(members))


def test_in_memory_builder_is_deterministic_verified_and_does_not_touch_disk(
    tmp_path: Path,
) -> None:
    request = _request()

    first = build_support_bundle_bytes(request)
    second = build_support_bundle_bytes(request)
    verification = verify_support_bundle_bytes(first)

    assert first == second
    assert verification.entry_count == 2
    assert verification.size_bytes == len(first)
    assert not tuple(tmp_path.iterdir())

    destination = _destination(tmp_path)
    create_support_bundle(destination, allowed_root=tmp_path, request=request)
    assert destination.read_bytes() == first


def test_create_contains_only_fixed_redacted_members_and_allowlisted_metadata(
    tmp_path: Path,
) -> None:
    private_task_probe = (
        r"Z:\synthetic-private\paper\main.tex::SYNTHETIC_SECRET_BODY_7429::导师返回稿.docx"
    )
    request = _request(task_identifier=private_task_probe)
    destination = _destination(tmp_path)

    result = create_support_bundle(
        destination,
        allowed_root=tmp_path,
        request=request,
    )
    verification = verify_support_bundle(destination)

    assert result.path == destination.resolve()
    assert result.sha256 == verification.sha256
    assert result.size_bytes == verification.size_bytes
    assert result.entry_count == verification.entry_count == 2
    assert 0 < result.size_bytes <= DEFAULT_SUPPORT_BUNDLE_LIMITS.max_archive_bytes
    assert set(result.metadata) == _EXPECTED_FIELDS
    assert result.metadata == verification.metadata
    assert result.metadata["product_version"] == __version__
    assert result.metadata["build_identifier"] == request.build_identifier
    assert result.metadata["error_code"] == ErrorCode.BACKEND_FAILED.value
    assert result.metadata["stage"] == "export_review"
    assert result.metadata["duration_ms"] == 12_345
    assert result.metadata["exception_type"] == "PermissionError"
    assert result.metadata["winerror"] == 5
    anonymous = result.metadata["anonymous_task_id"]
    assert isinstance(anonymous, str)
    assert anonymous.startswith("task_")
    assert len(anonymous) == 69
    assert private_task_probe not in anonymous

    with zipfile.ZipFile(destination, mode="r") as archive:
        assert archive.namelist() == ["README.txt", "support-metadata.json"]
        readme = archive.read("README.txt").decode("utf-8")
        metadata_bytes = archive.read("support-metadata.json")
    metadata = json.loads(metadata_bytes)
    assert set(metadata) == _EXPECTED_FIELDS
    assert metadata == dict(result.metadata)
    assert "no source or document files" in readme
    assert "sealed workflow JSON" in readme
    assert "绝对路径" in readme
    assert "raw logs" in readme

    raw_archive = destination.read_bytes()
    for leak_probe in (
        private_task_probe,
        r"Z:\synthetic-private\paper\main.tex",
        "SYNTHETIC_SECRET_BODY_7429",
        "导师返回稿.docx",
        "payload_sha256",
        "document_sha256",
        '"integrity"',
        '"object_id"',
        '"message"',
        '"details"',
        '"log"',
    ):
        assert leak_probe.encode("utf-8") not in raw_archive
    assert _temporary_stages(tmp_path) == ()


def test_task_identifier_is_one_way_and_deterministic_without_entering_the_archive(
    tmp_path: Path,
) -> None:
    task_identifier = r"Y:\redaction-probe\secret.tex::private body"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = create_support_bundle(
        _destination(first_root),
        allowed_root=first_root,
        request=_request(task_identifier=task_identifier),
    )
    second = create_support_bundle(
        _destination(second_root),
        allowed_root=second_root,
        request=_request(task_identifier=task_identifier),
    )

    assert first.metadata["anonymous_task_id"] == second.metadata["anonymous_task_id"]
    assert _destination(first_root).read_bytes() == _destination(second_root).read_bytes()
    assert task_identifier.encode("utf-8") not in _destination(first_root).read_bytes()


def test_optional_diagnostics_are_explicit_nulls_and_remain_allowlisted(tmp_path: Path) -> None:
    destination = _destination(tmp_path)
    result = create_support_bundle(
        destination,
        allowed_root=tmp_path,
        request=_request(duration_ms=None, exception_type=None, winerror=None),
    )

    assert result.metadata["duration_ms"] is None
    assert result.metadata["exception_type"] is None
    assert result.metadata["winerror"] is None
    assert set(result.metadata) == _EXPECTED_FIELDS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("error_code", "E_NOT_A_PUBLIC_CODE"),
        ("stage", "export/review"),
        ("stage", "X"),
        ("stage", "a" * 65),
        ("build_identifier", r"C:\private\build"),
        ("build_identifier", "x" * 97),
        ("task_identifier", ""),
        ("task_identifier", "x" * 257),
        ("task_identifier", "session\nsecret"),
        ("task_identifier", "\ud800"),
        ("duration_ms", True),
        ("duration_ms", -1),
        ("duration_ms", 604_800_001),
        ("exception_type", "module.Error"),
        ("exception_type", r"C:\Error"),
        ("exception_type", "E" * 97),
        ("winerror", True),
        ("winerror", -1),
        ("winerror", 0x1_0000_0000),
    ],
)
def test_request_rejects_unsafe_or_oversized_values(field: str, value: object) -> None:
    untyped_replace = cast(Any, replace)
    with pytest.raises(ContractError) as raised:
        untyped_replace(_request(), **{field: value})

    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_create_rejects_untyped_mapping_instead_of_copying_arbitrary_fields(
    tmp_path: Path,
) -> None:
    request = cast(
        Any,
        {
            "error_code": ErrorCode.BACKEND_FAILED.value,
            "stage": "export_review",
            "details": {"absolute_path": r"Z:\private\main.tex", "raw_log": "secret"},
        },
    )

    with pytest.raises(ContractError) as raised:
        create_support_bundle(
            _destination(tmp_path),
            allowed_root=tmp_path,
            request=request,
        )

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not _destination(tmp_path).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", "future-format"),
        ("error_code", 5),
        ("error_code", "E_NOT_A_PUBLIC_CODE"),
        ("stage", r"Z:\\private"),
        ("exception_type", "module.Error"),
        ("winerror", -1),
    ],
)
def test_metadata_validator_fails_closed_on_invalid_allowlisted_values(
    field: str,
    value: object,
) -> None:
    metadata = support_bundle_module._metadata(_request())
    metadata[field] = cast(Any, value)

    with pytest.raises(ContractError) as raised:
        support_bundle_module._validate_metadata(metadata)

    assert raised.value.code is ErrorCode.BUNDLE_HASH_MISMATCH


def test_archive_builder_rejects_nonfixed_members_and_wraps_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ContractError) as wrong_members:
        support_bundle_module._build_archive_bytes({"arbitrary.log": b"private"})
    assert wrong_members.value.code is ErrorCode.INTERNAL_INVARIANT

    def fail_zip(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic ZIP writer failure")

    monkeypatch.setattr(zipfile, "ZipFile", fail_zip)
    with pytest.raises(ContractError) as writer_failure:
        support_bundle_module._build_archive_bytes(
            {"README.txt": b"readme", "support-metadata.json": b"{}"}
        )
    assert writer_failure.value.code is ErrorCode.BUNDLE_HASH_MISMATCH


def test_target_is_absolute_fixed_name_and_contained_in_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(ContractError) as relative:
        create_support_bundle(
            Path(SUPPORT_BUNDLE_FILENAME),
            allowed_root=root,
            request=_request(),
        )
    assert relative.value.code is ErrorCode.PATH_ABSOLUTE

    with pytest.raises(ContractError) as wrong_name:
        create_support_bundle(
            root / "custom-name.zip",
            allowed_root=root,
            request=_request(),
        )
    assert wrong_name.value.code is ErrorCode.SCHEMA_INVALID

    with pytest.raises(ContractError) as escaped:
        create_support_bundle(
            _destination(outside),
            allowed_root=root,
            request=_request(),
        )
    assert escaped.value.code is ErrorCode.PATH_TRAVERSAL
    assert not _destination(outside).exists()
    assert _temporary_stages(root) == ()


def test_missing_or_nondirectory_output_roots_fail_before_writing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ContractError) as unavailable:
        create_support_bundle(
            _destination(missing),
            allowed_root=missing,
            request=_request(),
        )
    assert unavailable.value.code is ErrorCode.SCHEMA_INVALID

    root_file = tmp_path / "not-a-directory"
    root_file.write_bytes(b"ordinary file")
    with pytest.raises(ContractError) as nondirectory:
        create_support_bundle(
            root_file / SUPPORT_BUNDLE_FILENAME,
            allowed_root=root_file,
            request=_request(),
        )
    assert nondirectory.value.code is ErrorCode.SCHEMA_INVALID
    assert _temporary_stages(tmp_path) == ()


def test_path_status_and_reparse_probe_os_errors_are_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "status-error"
    original_lstat = os.lstat

    def fail_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if Path(path) == sentinel:
            raise PermissionError("synthetic lstat failure")
        return original_lstat(path)

    monkeypatch.setattr(os, "lstat", fail_lstat)
    with pytest.raises(ContractError) as status_error:
        support_bundle_module._status_without_following(sentinel)
    assert status_error.value.code is ErrorCode.PATH_LINK_ESCAPE

    monkeypatch.setattr(os, "lstat", original_lstat)
    concrete_path = type(tmp_path)
    original_junction = concrete_path.is_junction

    def fail_junction(self: Path) -> bool:
        if self == tmp_path:
            raise OSError("synthetic reparse probe failure")
        return original_junction(self)

    monkeypatch.setattr(concrete_path, "is_junction", fail_junction)
    with pytest.raises(ContractError) as probe_error:
        support_bundle_module._is_link_or_reparse(tmp_path, os.lstat(tmp_path))
    assert probe_error.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_target_rejects_simulated_reparse_directory_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    original = support_bundle_module._is_link_or_reparse

    def simulated_reparse(path: Path, status: os.stat_result | None = None) -> bool:
        if path == nested:
            return True
        return original(path, status)

    monkeypatch.setattr(support_bundle_module, "_is_link_or_reparse", simulated_reparse)
    with pytest.raises(ContractError) as raised:
        create_support_bundle(
            _destination(nested),
            allowed_root=tmp_path,
            request=_request(),
        )

    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE
    assert not _destination(nested).exists()
    assert _temporary_stages(nested) == ()


def test_existing_reparse_target_is_rejected_without_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _destination(tmp_path)
    target.write_bytes(b"competitor-owned-target")
    original = support_bundle_module._is_link_or_reparse

    def simulated_reparse(path: Path, status: os.stat_result | None = None) -> bool:
        if path == target:
            return True
        return original(path, status)

    monkeypatch.setattr(support_bundle_module, "_is_link_or_reparse", simulated_reparse)
    with pytest.raises(ContractError) as raised:
        create_support_bundle(target, allowed_root=tmp_path, request=_request())

    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE
    assert target.read_bytes() == b"competitor-owned-target"


def test_existing_target_is_never_overwritten(tmp_path: Path) -> None:
    destination = _destination(tmp_path)
    first = create_support_bundle(
        destination,
        allowed_root=tmp_path,
        request=_request(stage="export_review"),
    )
    original = destination.read_bytes()

    with pytest.raises(ContractError) as raised:
        create_support_bundle(
            destination,
            allowed_root=tmp_path,
            request=_request(stage="receive_review"),
        )

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert destination.read_bytes() == original
    assert verify_support_bundle(destination).sha256 == first.sha256
    assert _temporary_stages(tmp_path) == ()


def test_concurrent_publication_has_exactly_one_winner_and_no_partial(
    tmp_path: Path,
) -> None:
    destination = _destination(tmp_path)
    barrier = threading.Barrier(2)

    def attempt(stage: str) -> SupportBundleResult | ContractError:
        barrier.wait(timeout=5)
        try:
            return create_support_bundle(
                destination,
                allowed_root=tmp_path,
                request=_request(stage=stage),
            )
        except ContractError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(attempt, ("export_review", "receive_review")))

    successes = tuple(item for item in outcomes if isinstance(item, SupportBundleResult))
    failures = tuple(item for item in outcomes if isinstance(item, ContractError))
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ErrorCode.SCHEMA_INVALID
    assert verify_support_bundle(destination).sha256 == successes[0].sha256
    assert _temporary_stages(tmp_path) == ()


def test_post_publish_verification_failure_removes_only_owned_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _destination(tmp_path)
    original_verify = support_bundle_module.verify_support_bundle
    calls = 0

    def fail_final(
        path: str | Path,
        *,
        limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
    ) -> SupportBundleVerification:
        nonlocal calls
        calls += 1
        if Path(path).name == SUPPORT_BUNDLE_FILENAME:
            raise ContractError(
                ErrorCode.BUNDLE_HASH_MISMATCH,
                "synthetic post-publication verification failure",
            )
        return original_verify(path, limits=limits)

    monkeypatch.setattr(support_bundle_module, "verify_support_bundle", fail_final)
    with pytest.raises(ContractError) as raised:
        create_support_bundle(destination, allowed_root=tmp_path, request=_request())

    assert raised.value.code is ErrorCode.BUNDLE_HASH_MISMATCH
    assert calls == 2
    assert not destination.exists()
    assert _temporary_stages(tmp_path) == ()


def test_stage_creation_and_write_failures_leave_no_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _destination(tmp_path)

    def fail_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        del args, kwargs
        raise PermissionError("synthetic stage creation failure")

    monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)
    with pytest.raises(ContractError) as stage_creation:
        create_support_bundle(destination, allowed_root=tmp_path, request=_request())
    assert stage_creation.value.code is ErrorCode.INTERNAL_INVARIANT
    assert not destination.exists()

    monkeypatch.undo()

    def fail_fdopen(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic stage open failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    with pytest.raises(ContractError) as stage_write:
        create_support_bundle(destination, allowed_root=tmp_path, request=_request())
    assert stage_write.value.code is ErrorCode.INTERNAL_INVARIANT
    assert not destination.exists()
    assert _temporary_stages(tmp_path) == ()


def test_link_failure_and_final_mismatch_leave_no_owned_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _destination(tmp_path)

    def fail_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("synthetic hard-link failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ContractError) as link_failure:
        create_support_bundle(destination, allowed_root=tmp_path, request=_request())
    assert link_failure.value.code is ErrorCode.INTERNAL_INVARIANT
    assert not destination.exists()
    assert _temporary_stages(tmp_path) == ()

    monkeypatch.undo()
    original_verify = support_bundle_module.verify_support_bundle

    def mismatch_final(
        path: str | Path,
        *,
        limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
    ) -> SupportBundleVerification:
        verified = original_verify(path, limits=limits)
        if Path(path).name == SUPPORT_BUNDLE_FILENAME:
            return replace(verified, sha256="sha256:" + "0" * 64)
        return verified

    monkeypatch.setattr(support_bundle_module, "verify_support_bundle", mismatch_final)
    with pytest.raises(ContractError) as mismatch:
        create_support_bundle(destination, allowed_root=tmp_path, request=_request())
    assert mismatch.value.code is ErrorCode.BUNDLE_HASH_MISMATCH
    assert not destination.exists()
    assert _temporary_stages(tmp_path) == ()


@pytest.mark.parametrize(
    "limits",
    [
        SupportBundleLimits(max_metadata_bytes=64),
        SupportBundleLimits(max_archive_bytes=512),
    ],
)
def test_configured_tighter_size_bounds_fail_without_partial(
    tmp_path: Path,
    limits: SupportBundleLimits,
) -> None:
    destination = _destination(tmp_path)

    with pytest.raises(ContractError) as raised:
        create_support_bundle(
            destination,
            allowed_root=tmp_path,
            request=_request(),
            limits=limits,
        )

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not destination.exists()
    assert _temporary_stages(tmp_path) == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_archive_bytes": 0},
        {"max_archive_bytes": 65_537},
        {"max_metadata_bytes": 0},
        {"max_metadata_bytes": 4_097},
        {"max_archive_bytes": True},
    ],
)
def test_limits_cannot_expand_or_disable_hard_caps(kwargs: dict[str, object]) -> None:
    constructor = cast(Any, SupportBundleLimits)
    with pytest.raises(ContractError) as raised:
        constructor(**kwargs)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_verifier_rejects_unknown_metadata_fields_and_trailing_raw_bytes(tmp_path: Path) -> None:
    destination = _destination(tmp_path)
    create_support_bundle(destination, allowed_root=tmp_path, request=_request())

    with zipfile.ZipFile(destination, mode="r") as archive:
        readme = archive.read("README.txt")
        metadata = json.loads(archive.read("support-metadata.json"))
    metadata["absolute_path"] = r"Z:\synthetic-private\paper\main.tex"
    malicious = support_bundle_module._build_archive_bytes(
        {
            "README.txt": readme,
            "support-metadata.json": canonical_json(metadata) + b"\n",
        }
    )
    unknown_field_archive = tmp_path / "unknown-field.zip"
    unknown_field_archive.write_bytes(malicious)

    with pytest.raises(ContractError) as unknown:
        verify_support_bundle(unknown_field_archive)
    assert unknown.value.code is ErrorCode.BUNDLE_HASH_MISMATCH

    destination.write_bytes(destination.read_bytes() + b"RAW_LOG_PRIVATE_PATH_Z:\\secret\\main.tex")
    with pytest.raises(ContractError) as trailing:
        verify_support_bundle(destination)
    assert trailing.value.code is ErrorCode.BUNDLE_HASH_MISMATCH


def test_verifier_rejects_every_noncanonical_container_and_payload_form(tmp_path: Path) -> None:
    base_root = tmp_path / "base"
    base_root.mkdir()
    base = _destination(base_root)
    create_support_bundle(base, allowed_root=base_root, request=_request())
    members = _fixed_members(base)
    metadata = json.loads(members["support-metadata.json"])

    def assert_bundle_mismatch(path: Path) -> None:
        with pytest.raises(ContractError) as raised:
            verify_support_bundle(path)
        assert raised.value.code is ErrorCode.BUNDLE_HASH_MISMATCH

    commented = tmp_path / "commented.zip"
    commented.write_bytes(base.read_bytes())
    with zipfile.ZipFile(commented, mode="a") as archive:
        archive.comment = b"raw-log-comment"
    assert_bundle_mismatch(commented)

    wrong_members = tmp_path / "wrong-members.zip"
    with zipfile.ZipFile(wrong_members, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("support-metadata.json", members["support-metadata.json"])
        archive.writestr("README.txt", members["README.txt"])
        archive.writestr("raw.log", b"private")
    assert_bundle_mismatch(wrong_members)

    wrong_member_metadata = tmp_path / "wrong-member-metadata.zip"
    with zipfile.ZipFile(
        wrong_member_metadata,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("README.txt", members["README.txt"])
        archive.writestr("support-metadata.json", members["support-metadata.json"])
    assert_bundle_mismatch(wrong_member_metadata)

    oversized_member = tmp_path / "oversized-member.zip"
    with zipfile.ZipFile(oversized_member, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(support_bundle_module._zip_info("README.txt"), members["README.txt"])
        archive.writestr(
            support_bundle_module._zip_info("support-metadata.json"),
            b"x" * 65,
        )
    with pytest.raises(ContractError) as oversized:
        verify_support_bundle(
            oversized_member,
            limits=SupportBundleLimits(max_metadata_bytes=64),
        )
    assert oversized.value.code is ErrorCode.BUNDLE_HASH_MISMATCH

    invalid_zip = tmp_path / "invalid.zip"
    invalid_zip.write_bytes(b"not a ZIP")
    assert_bundle_mismatch(invalid_zip)

    changed_readme = tmp_path / "changed-readme.zip"
    _write_fixed_archive(
        changed_readme,
        {**members, "README.txt": b"replacement redaction notice"},
    )
    assert_bundle_mismatch(changed_readme)

    invalid_json = tmp_path / "invalid-json.zip"
    _write_fixed_archive(
        invalid_json,
        {**members, "support-metadata.json": b"{not-json}\n"},
    )
    assert_bundle_mismatch(invalid_json)

    noncanonical_json = tmp_path / "noncanonical-json.zip"
    _write_fixed_archive(
        noncanonical_json,
        {
            **members,
            "support-metadata.json": json.dumps(metadata, indent=2).encode() + b"\n",
        },
    )
    assert_bundle_mismatch(noncanonical_json)


def test_verifier_rejects_missing_path_and_directory(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as missing:
        verify_support_bundle(tmp_path / "missing.zip")
    assert missing.value.code is ErrorCode.SCHEMA_INVALID

    directory = tmp_path / "directory.zip"
    directory.mkdir()
    with pytest.raises(ContractError) as not_file:
        verify_support_bundle(directory)
    assert not_file.value.code is ErrorCode.SCHEMA_INVALID


def test_verifier_rejects_simulated_reparse_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _destination(tmp_path)
    create_support_bundle(destination, allowed_root=tmp_path, request=_request())
    original = support_bundle_module._is_link_or_reparse

    def simulated_reparse(path: Path, status: os.stat_result | None = None) -> bool:
        if path == destination:
            return True
        return original(path, status)

    monkeypatch.setattr(support_bundle_module, "_is_link_or_reparse", simulated_reparse)
    with pytest.raises(ContractError) as raised:
        verify_support_bundle(destination)

    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE
