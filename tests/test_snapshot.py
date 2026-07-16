"""Immutable snapshot, drift, idempotence, and conflict tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import latex_word_review.snapshot as snapshot_module
from latex_word_review.discovery import DiscoveryLimits, ProjectDiscovery
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_file
from latex_word_review.snapshot import SNAPSHOT_MANIFEST, snapshot_project


def _write_project(root: Path, text: str = "Baseline Unicode 正文。") -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        f"\\documentclass{{article}}\n\\begin{{document}}\n{text}\n\\end{{document}}\n",
        encoding="utf-8",
        newline="\n",
    )


def test_snapshot_is_atomic_relative_read_only_and_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshots" / "baseline"
    _write_project(source)
    before = digest_file(source / "main.tex", max_bytes=4096)

    result = snapshot_project(source, destination)

    assert result.reused is False
    assert digest_file(source / "main.tex", max_bytes=4096) == before
    assert result.manifest_path == SNAPSHOT_MANIFEST
    assert str(tmp_path) not in json.dumps(result.as_dict(), ensure_ascii=False)
    manifest = json.loads((destination / SNAPSHOT_MANIFEST).read_bytes())
    assert manifest["main_document"] == "main.tex"
    assert manifest["immutability"]["source_origin_pre_sha256"] == result.source_tree_sha256
    assert manifest["immutability"]["source_origin_post_sha256"] == result.source_tree_sha256
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)
    assert (destination / "main.tex").stat().st_mode & stat.S_IWUSR == 0


def test_snapshot_reuses_exact_content_and_rejects_conflict(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    _write_project(source)
    first = snapshot_project(source, destination)
    second = snapshot_project(source, destination)
    assert first.source_tree_sha256 == second.source_tree_sha256
    assert second.reused is True

    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}Changed\\end{document}\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as raised:
        snapshot_project(source, destination)
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_snapshot_reuse_rejects_writable_entry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    _write_project(source)
    snapshot_project(source, destination)
    (destination / "main.tex").chmod(stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(ContractError) as raised:
        snapshot_project(source, destination)
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_snapshot_reuse_rejects_simulated_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    _write_project(source)
    snapshot_project(source, destination)
    concrete_path = type(destination)
    original_is_junction = concrete_path.is_junction

    def simulated_is_junction(self: Path) -> bool:
        if self == destination / "main.tex":
            return True
        return original_is_junction(self)

    monkeypatch.setattr(concrete_path, "is_junction", simulated_is_junction)
    with pytest.raises(ContractError) as raised:
        snapshot_project(source, destination)
    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_snapshot_rejects_output_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)
    with pytest.raises(ContractError) as raised:
        snapshot_project(source, source / "snapshot")
    assert raised.value.code is ErrorCode.PATH_TRAVERSAL


def test_snapshot_detects_source_drift_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    _write_project(source)
    original_copy = snapshot_module._copy_files

    def copy_then_mutate(
        source_root: Path,
        temporary_root: Path,
        discovery: ProjectDiscovery,
        limits: DiscoveryLimits,
    ) -> None:
        original_copy(source_root, temporary_root, discovery, limits)
        (source_root / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}Drift\\end{document}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(snapshot_module, "_copy_files", copy_then_mutate)
    with pytest.raises(ContractError) as raised:
        snapshot_project(source, destination)
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH
    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot.tmp-*"))
    assert not (tmp_path / ".snapshot.snapshot.lock").exists()


def test_snapshot_rejects_preexisting_unrelated_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    _write_project(source)
    destination.mkdir()
    (destination / "unrelated.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ContractError):
        snapshot_project(source, destination)
    assert (destination / "unrelated.txt").read_text(encoding="utf-8") == "do not overwrite"
