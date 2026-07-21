"""Bounded, fail-closed recent-task rendering tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.app_server as app_server_module
from latex_word_review.app_server import AppState
from latex_word_review.app_views import render_app_page


def _session_key(index: int) -> str:
    return f"session_{index:032x}"


def _create_summary(run_root: Path) -> None:
    objects = run_root / "objects"
    objects.mkdir(parents=True)
    (objects / "source-manifest.json").write_text("{}", encoding="utf-8")


def _read_summary(
    path: Path,
    *,
    expected_schema: str,
    max_bytes: int,
) -> dict[str, object]:
    assert expected_schema == "SourceManifest"
    assert max_bytes == 4 * 1024 * 1024
    return {
        "payload": {
            "main_document": f"{path.parent.parent.name}.tex",
        }
    }


def test_home_validates_only_newest_visible_tasks_without_status_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    roots: list[Path] = []
    for index in range(25):
        run_root = state.runs_root / _session_key(index)
        _create_summary(run_root)
        timestamp = 1_700_000_000 + index
        os.utime(run_root, (timestamp, timestamp))
        roots.append(run_root)

    class Loader:
        @staticmethod
        def load(_run_root: Path) -> Any:
            raise AssertionError("home must not load ApplicationSession")

    monkeypatch.setattr(app_server_module, "read_contract_file", _read_summary)
    monkeypatch.setattr(app_server_module, "ApplicationSession", Loader)
    expected = list(reversed(roots[-20:]))
    try:
        first = state.home_view()
        second = state.home_view()
    finally:
        state.close()

    first_recent = cast("list[dict[str, object]]", first["recent_sessions"])
    second_recent = cast("list[dict[str, object]]", second["recent_sessions"])
    assert len(first_recent) == len(second_recent) == 20
    assert [item["name"] for item in first_recent] == [path.name for path in expected]
    assert first_recent[0]["status"] == "打开时核验"
    assert first_recent[0]["action_label"] == "打开并核验"
    assert "完整核验密封证据" in cast("str", first_recent[0]["next_step"])
    assert first_recent[0]["delete_href"] == f"/session/{expected[0].name}/delete"

    page = render_app_page(first)
    assert "按最近更新排序；打开任务时会完整核验状态。" in page
    assert "下一步：" in page
    assert page.count('class="button button--danger"') == 20


def test_home_directory_enumeration_is_actually_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    roots: list[Path] = []
    for index in range(4):
        run_root = state.runs_root / _session_key(index)
        _create_summary(run_root)
        roots.append(run_root)

    class BoundedRunsRoot:
        def iterdir(self) -> Any:
            yield from roots[:3]
            raise AssertionError("home_view enumerated beyond the configured bound")

    class Loader:
        @staticmethod
        def load(_run_root: Path) -> Any:
            raise AssertionError("home must not load ApplicationSession")

    monkeypatch.setattr(app_server_module, "_MAX_SCANNED_SESSIONS", 3)
    monkeypatch.setattr(app_server_module, "read_contract_file", _read_summary)
    monkeypatch.setattr(app_server_module, "ApplicationSession", Loader)
    state.runs_root = cast("Any", BoundedRunsRoot())
    try:
        view = state.home_view()
    finally:
        state.close()

    assert len(cast("list[dict[str, object]]", view["recent_sessions"])) == 3


def test_broken_visible_task_is_path_free_and_cannot_auto_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = _session_key(1)
    run_root = state.runs_root / session_key
    run_root.mkdir()

    class Loader:
        @staticmethod
        def load(_path: Path) -> Any:
            raise AssertionError("home must not load ApplicationSession")

    monkeypatch.setattr(app_server_module, "ApplicationSession", Loader)
    try:
        view = state.home_view()
    finally:
        state.close()

    recent = cast("list[dict[str, object]]", view["recent_sessions"])
    assert recent == [
        recent[0]
        | {
            "name": "无法读取的任务",
            "status": "需要检查",
            "tone": "danger",
            "action_label": "打开并检查",
            "next_step": "打开后查看安全诊断；任何操作前仍会完整核验。",
        }
    ]
    assert str(tmp_path) not in render_app_page(view)
