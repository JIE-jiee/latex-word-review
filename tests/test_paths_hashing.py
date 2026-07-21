"""Security and determinism tests for path and hashing primitives."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import TreeEntry, digest_file, source_tree_sha256
from latex_word_review.paths import (
    ensure_disjoint_roots,
    resolve_within,
    validate_relative_path,
    windows_extended_path,
)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("/etc/passwd", ErrorCode.PATH_ABSOLUTE),
        ("C:/private/paper.tex", ErrorCode.PATH_ABSOLUTE),
        (r"C:\private\paper.tex", ErrorCode.PATH_ABSOLUTE),
        (r"\\server\share\paper.tex", ErrorCode.PATH_ABSOLUTE),
        ("../paper.tex", ErrorCode.PATH_TRAVERSAL),
        ("sections/../paper.tex", ErrorCode.PATH_TRAVERSAL),
        ("sections//paper.tex", ErrorCode.PATH_TRAVERSAL),
        ("sections/./paper.tex", ErrorCode.PATH_TRAVERSAL),
        ("sections\\paper.tex", ErrorCode.PATH_TRAVERSAL),
        ("paper.tex\x00ignored", ErrorCode.PATH_TRAVERSAL),
        ("NUL.tex", ErrorCode.PATH_TRAVERSAL),
        ("figures/plot?.png", ErrorCode.PATH_TRAVERSAL),
    ],
)
def test_relative_path_rejects_ambiguous_or_escaping_values(
    value: str,
    code: ErrorCode,
) -> None:
    with pytest.raises(ContractError) as raised:
        validate_relative_path(value)
    assert raised.value.code is code


def test_relative_path_preserves_unicode() -> None:
    assert validate_relative_path("章节/方法与结果.tex") == "章节/方法与结果.tex"


def test_windows_extended_path_is_idempotent_and_preserves_location(tmp_path: Path) -> None:
    target = tmp_path / "paper"
    target.mkdir()

    extended = windows_extended_path(target)

    assert extended.is_dir()
    assert windows_extended_path(extended) == extended
    if os.name == "nt":
        assert str(extended).startswith("\\\\?\\")
        dotted = target / "child" / ".."
        assert windows_extended_path(dotted) == extended
        assert ".." not in windows_extended_path(dotted).parts
    else:
        assert extended == target


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.tex").write_text("secret", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit test symlinks")

    with pytest.raises(ContractError) as raised:
        resolve_within(root, "escape/secret.tex")
    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_resolve_within_rejects_resolved_escape_without_platform_link_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the containment branch even on locked-down Windows runners."""

    root = tmp_path / "source"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    candidate = root / "escape" / "secret.tex"
    escaped = outside / "secret.tex"
    concrete_path = type(root)
    original_resolve = concrete_path.resolve

    def simulated_resolve(self: Path, strict: bool = False) -> Path:
        if self == candidate:
            return escaped
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(concrete_path, "resolve", simulated_resolve)
    with pytest.raises(ContractError) as raised:
        resolve_within(root, "escape/secret.tex")
    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_disjoint_roots_reject_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    link = tmp_path / "destination"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit test symlinks")

    with pytest.raises(ContractError) as raised:
        ensure_disjoint_roots(source, link)
    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_disjoint_roots_reject_simulated_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    concrete_path = type(destination)
    original_is_junction = concrete_path.is_junction

    def simulated_is_junction(self: Path) -> bool:
        if self == destination.absolute():
            return True
        return original_is_junction(self)

    monkeypatch.setattr(concrete_path, "is_junction", simulated_is_junction)
    with pytest.raises(ContractError) as raised:
        ensure_disjoint_roots(source, destination)
    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_file_and_tree_hashes_are_raw_byte_and_order_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "a.tex"
    second = tmp_path / "文献.bib"
    first.write_bytes(b"alpha\r\n")
    second.write_bytes("标题".encode())
    first_digest = digest_file(first, max_bytes=1024)
    second_digest = digest_file(second, max_bytes=1024)
    entries = (
        TreeEntry("文献.bib", "bib", second_digest.size_bytes, second_digest.sha256),
        TreeEntry("a.tex", "tex", first_digest.size_bytes, first_digest.sha256),
    )
    assert source_tree_sha256(entries) == source_tree_sha256(reversed(entries))
    assert first_digest.sha256 != digest_file(second, max_bytes=1024).sha256


def test_file_hash_enforces_resource_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.tex"
    path.write_bytes(b"x" * 11)
    with pytest.raises(ContractError) as raised:
        digest_file(path, max_bytes=10)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
