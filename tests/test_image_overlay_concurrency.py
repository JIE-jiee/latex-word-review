"""Deterministic and bounded image-overlay materialization contracts."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import pytest

import latex_word_review.image_overlay as overlay_module
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.image_materializer import (
    ImageRequest,
    MaterializedImage,
    RendererIdentity,
)
from latex_word_review.image_overlay import IMAGE_OVERLAY_MANIFEST, build_image_overlay

_RENDERER = RendererIdentity(
    name="pypdfium2-pillow",
    pypdfium2_version="5.0.0",
    pdfium_version="1.2.3.4",
    pdfium_binary_sha256="sha256:" + "a" * 64,
    pillow_version="12.0.0",
)


def _write_source(root: Path, targets: tuple[str, ...]) -> None:
    root.mkdir()
    for target in dict.fromkeys(targets):
        (root / target).write_bytes(f"%PDF-1.4\n% synthetic {target}\n".encode())
    commands = "".join(f"\\includegraphics{{{target}}}\n" for target in targets)
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n\n"
        "Public bounded-concurrency fixture.\n\n"
        f"{commands}"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fake_result(
    request: ImageRequest,
    *,
    cache_root: Path,
) -> MaterializedImage:
    key = digest_bytes(f"cache:{request.request_sha256}".encode()).sha256
    entry_name = key.removeprefix("sha256:")
    entry = cache_root / entry_name
    entry.mkdir(parents=True)
    png_bytes = f"synthetic-png:{request.source_path}".encode()
    manifest_bytes = b"{}\n"
    (entry / "preview.png").write_bytes(png_bytes)
    (entry / "manifest.json").write_bytes(manifest_bytes)
    return MaterializedImage(
        cache_key_sha256=key,
        request_sha256=request.request_sha256,
        png_path=f"{entry_name}/preview.png",
        manifest_path=f"{entry_name}/manifest.json",
        png_sha256=digest_bytes(png_bytes).sha256,
        pixel_sha256=digest_bytes(b"pixels:" + png_bytes).sha256,
        width_px=32,
        height_px=24,
        dpi=request.quality.dpi,
        renderer=_RENDERER,
        reused=False,
    )


def _run_bounded_overlay(
    source: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    delays: Mapping[str, float],
) -> tuple[bytes, int, Counter[str]]:
    lock = threading.Lock()
    first_pair = threading.Barrier(2)
    state = {"active": 0, "maximum": 0, "started": 0}
    calls: Counter[str] = Counter()

    def fake_materialize(
        request: ImageRequest,
        *,
        source_root: Path,
        cache_root: Path,
    ) -> MaterializedImage:
        assert source_root == source.resolve()
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            state["started"] += 1
            ordinal = state["started"]
            calls[request.request_sha256] += 1
        try:
            if ordinal <= 2:
                first_pair.wait(timeout=2.0)
            time.sleep(delays[request.source_path])
            return _fake_result(request, cache_root=cache_root)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(overlay_module, "materialize_image", fake_materialize)
    discovery = discover_project(source, main_document="main.tex")
    overlay = build_image_overlay(source, destination, discovery)
    manifest_bytes = (destination / IMAGE_OVERLAY_MANIFEST).read_bytes()
    assert overlay.manifest_sha256 == digest_bytes(manifest_bytes).sha256
    return manifest_bytes, state["maximum"], calls


def test_materialization_is_bounded_deduplicated_ordered_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    targets = ("a.pdf", "b.pdf", "c.pdf", "d.pdf", "a.pdf")
    _write_source(source, targets)
    original = _tree_bytes(source)

    first_bytes, first_maximum, first_calls = _run_bounded_overlay(
        source,
        tmp_path / "overlay-first",
        monkeypatch,
        delays={"a.pdf": 0.06, "b.pdf": 0.01, "c.pdf": 0.04, "d.pdf": 0.01},
    )
    second_bytes, second_maximum, second_calls = _run_bounded_overlay(
        source,
        tmp_path / "overlay-second",
        monkeypatch,
        delays={"a.pdf": 0.01, "b.pdf": 0.06, "c.pdf": 0.01, "d.pdf": 0.04},
    )

    assert first_maximum == second_maximum == 2
    assert sum(first_calls.values()) == sum(second_calls.values()) == 4
    assert set(first_calls.values()) == set(second_calls.values()) == {1}
    assert first_bytes == second_bytes
    manifest = json.loads(first_bytes)
    entries = manifest["entries"]
    assert [entry["original_target"] for entry in entries] == list(targets)
    assert [entry["status"] for entry in entries] == ["materialized_pdf"] * 5
    assert entries[0]["request_sha256"] == entries[4]["request_sha256"]
    assert entries[0]["materialized"]["reused"] is False
    assert entries[4]["materialized"]["reused"] is True
    assert _tree_bytes(source) == original
    assert not list(tmp_path.glob(".lwr-img-*"))


def test_one_contained_materialization_failure_only_marks_that_item_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    targets = ("a.pdf", "bad.pdf", "c.pdf")
    _write_source(source, targets)
    original = _tree_bytes(source)

    def fake_materialize(
        request: ImageRequest,
        *,
        source_root: Path,
        cache_root: Path,
    ) -> MaterializedImage:
        assert source_root == source.resolve()
        if request.source_path == "bad.pdf":
            raise ContractError(ErrorCode.BACKEND_FAILED, "synthetic contained failure")
        return _fake_result(request, cache_root=cache_root)

    monkeypatch.setattr(overlay_module, "materialize_image", fake_materialize)
    discovery = discover_project(source, main_document="main.tex")
    destination = tmp_path / "overlay"

    overlay = build_image_overlay(source, destination, discovery)

    assert not overlay.ready
    assert overlay.materialized_pdf_instances == 2
    manifest = json.loads((destination / IMAGE_OVERLAY_MANIFEST).read_bytes())
    assert [entry["original_target"] for entry in manifest["entries"]] == list(targets)
    assert [entry["status"] for entry in manifest["entries"]] == [
        "materialized_pdf",
        "manual_required",
        "materialized_pdf",
    ]
    assert manifest["entries"][1]["diagnostics"][0]["code"] == "E_BACKEND_FAILED"
    assert _tree_bytes(source) == original
    assert not list(tmp_path.glob(".lwr-img-*"))


def test_unexpected_parallel_failure_waits_and_cleans_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _write_source(source, ("a.pdf", "bad.pdf", "c.pdf", "d.pdf"))
    original = _tree_bytes(source)

    def fake_materialize(
        request: ImageRequest,
        *,
        source_root: Path,
        cache_root: Path,
    ) -> MaterializedImage:
        assert source_root == source.resolve()
        if request.source_path == "bad.pdf":
            raise RuntimeError("unexpected synthetic failure")
        time.sleep(0.02)
        return _fake_result(request, cache_root=cache_root)

    monkeypatch.setattr(overlay_module, "materialize_image", fake_materialize)
    discovery = discover_project(source, main_document="main.tex")
    destination = tmp_path / "overlay"

    with pytest.raises(RuntimeError, match="unexpected synthetic failure"):
        build_image_overlay(source, destination, discovery)

    assert not destination.exists()
    assert not list(tmp_path.glob(".lwr-img-*"))
    assert _tree_bytes(source) == original
