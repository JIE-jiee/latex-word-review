from __future__ import annotations

import os
import stat
import threading
from http import HTTPStatus
from pathlib import Path

import pytest

import latex_word_review.app_server as app_server_module
from latex_word_review.app_server import AppState
from latex_word_review.app_views import render_app_page
from latex_word_review.errors import ContractError, ErrorCode
from tests.test_app_server import _credentials, _post_form, _request, _running_server


def _session_key(character: str) -> str:
    return f"session_{character * 32}"


class _ReadPastDeleteLimit(RuntimeError):
    pass


class _GuardedScandir:
    def __init__(self, entries: tuple[os.DirEntry[str], ...]) -> None:
        self._entries = entries
        self._index = 0
        self.requests = 0

    def __enter__(self) -> _GuardedScandir:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> _GuardedScandir:
        return self

    def __next__(self) -> os.DirEntry[str]:
        self.requests += 1
        if self._index >= len(self._entries):
            raise _ReadPastDeleteLimit("scandir was consumed after the safety limit failed")
        entry = self._entries[self._index]
        self._index += 1
        return entry


def test_corrupt_readonly_task_requires_csrf_confirmation_and_is_deleted(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path / "app") as server:
        state = server.app_state
        session_key = _session_key("d")
        session_root = state.runs_root / session_key
        nested = session_root / "sealed"
        nested.mkdir(parents=True)
        evidence = nested / "evidence.json"
        evidence.write_text("corrupt but owned", encoding="utf-8")
        evidence.chmod(stat.S_IREAD)

        cookie, csrf, home, _headers = _credentials(server)
        assert f"/session/{session_key}/delete".encode("ascii") in home

        confirmation = _request(
            server,
            "GET",
            f"/session/{session_key}/delete",
            headers={"Host": server.expected_host, "Cookie": cookie},
        )
        stale_csrf = _post_form(
            server,
            "/session/delete",
            {
                "csrf": "wrong",
                "session": session_key,
                "confirm_delete": "yes",
            },
            cookie=cookie,
        )
        missing_confirmation = _post_form(
            server,
            "/session/delete",
            {
                "csrf": csrf,
                "session": session_key,
                "confirm_delete": "no",
            },
            cookie=cookie,
        )

        assert confirmation[0] == HTTPStatus.OK
        confirmation_html = confirmation[2].decode("utf-8")
        assert "确认删除本机任务" in confirmation_html
        assert 'method="post" action="/session/delete"' in confirmation_html
        assert 'name="confirm_delete" type="checkbox" value="yes" required' in confirmation_html
        assert "无法读取的任务" in confirmation_html
        assert stale_csrf[0] == HTTPStatus.SEE_OTHER
        assert missing_confirmation[0] in {HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT}
        assert session_root.exists()

        deleted = _post_form(
            server,
            "/session/delete",
            {
                "csrf": csrf,
                "session": session_key,
                "confirm_delete": "yes",
            },
            cookie=cookie,
        )

        assert deleted[0] == HTTPStatus.SEE_OTHER
        assert deleted[1]["location"] == "/"
        assert not session_root.exists()


def test_active_task_is_not_deleted_and_post_fails_closed(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        state = server.app_state
        session_key = _session_key("e")
        session_root = state.runs_root / session_key
        session_root.mkdir()
        (session_root / "keep.txt").write_text("keep", encoding="utf-8")
        started = threading.Event()
        release = threading.Event()

        def active_task(_progress: object) -> dict[str, object]:
            started.set()
            assert release.wait(timeout=5)
            return {}

        job = state.jobs.submit(session_key, "synthetic_active", active_task)
        assert started.wait(timeout=5)
        cookie, csrf, _body, _headers = _credentials(server)
        try:
            confirmation = _request(
                server,
                "GET",
                f"/session/{session_key}/delete",
                headers={"Host": server.expected_host, "Cookie": cookie},
            )
            blocked = _post_form(
                server,
                "/session/delete",
                {
                    "csrf": csrf,
                    "session": session_key,
                    "confirm_delete": "yes",
                },
                cookie=cookie,
            )
            assert confirmation[0] == HTTPStatus.OK
            assert "任务处理中，暂时不能删除" in confirmation[2].decode("utf-8")
            assert blocked[0] in {HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT}
            assert (session_root / "keep.txt").read_text(encoding="utf-8") == "keep"
        finally:
            release.set()
            state.jobs.wait(job.job_id, timeout=5)


def test_delete_rejects_nested_reparse_before_removing_any_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = _session_key("f")
    session_root = state.runs_root / session_key
    suspicious = session_root / "suspicious"
    suspicious.mkdir(parents=True)
    safe_file = session_root / "safe.txt"
    safe_file.write_text("untouched", encoding="utf-8")
    (suspicious / "outside-pointer.txt").write_text("still here", encoding="utf-8")
    original = app_server_module._is_link_or_junction

    def simulate_reparse(path: Path) -> bool:
        if path == suspicious:
            return True
        return original(path)

    monkeypatch.setattr(app_server_module, "_is_link_or_junction", simulate_reparse)
    try:
        with pytest.raises(ContractError) as raised:
            state.delete_session(session_key)
    finally:
        state.close()

    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE
    assert safe_file.read_text(encoding="utf-8") == "untouched"
    assert (suspicious / "outside-pointer.txt").exists()


def test_delete_scan_stops_before_eagerly_consuming_past_entry_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_root = tmp_path / "session"
    session_root.mkdir()
    files = tuple(session_root / f"entry-{index}.txt" for index in range(3))
    for path in files:
        path.write_text("keep", encoding="utf-8")
    with os.scandir(session_root) as iterator:
        entries = tuple(iterator)
    guarded = _GuardedScandir(entries)

    def guarded_scandir(_directory: Path) -> _GuardedScandir:
        return guarded

    monkeypatch.setattr(app_server_module, "_MAX_DELETE_TREE_ENTRIES", 2)
    monkeypatch.setattr(os, "scandir", guarded_scandir)

    with pytest.raises(ContractError) as raised:
        app_server_module._validated_delete_tree(session_root)

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert guarded.requests == 3
    assert all(path.read_text(encoding="utf-8") == "keep" for path in files)


def test_delete_never_uses_an_escaped_resolved_session_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = _session_key("1")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "must-remain.txt"
    marker.write_text("outside", encoding="utf-8")
    original = app_server_module._require_real_directory

    def escape_review_session(path: Path, label: str) -> Path:
        if label == "review session":
            return outside.resolve()
        return original(path, label)

    monkeypatch.setattr(app_server_module, "_require_real_directory", escape_review_session)
    try:
        with pytest.raises(ContractError) as raised:
            state.delete_session(session_key)
    finally:
        state.close()

    assert raised.value.code is ErrorCode.PATH_TRAVERSAL
    assert marker.read_text(encoding="utf-8") == "outside"


def test_delete_confirmation_view_requires_checkbox_and_disables_active_delete() -> None:
    base = {
        "page": "delete_confirm",
        "session_name": "<paper>",
        "status": "等待返回 Word",
        "updated_at": "2026-07-19 12:00",
        "can_delete": True,
        "delete_action": "/session/delete",
        "cancel_href": "/",
        "form_fields": {"csrf": "<csrf>", "session": "session_" + "a" * 32},
    }

    ready = render_app_page(base)
    active = render_app_page(base | {"can_delete": False})

    assert "<paper>" not in ready
    assert "&lt;paper&gt;" in ready
    assert "删除后无法恢复" in ready
    assert 'method="post" action="/session/delete"' in ready
    assert 'name="confirm_delete" type="checkbox" value="yes" required' in ready
    assert "永久删除此任务" in ready
    assert 'method="post" action="/session/delete"' not in active
    assert "任务处理中，暂时不能删除" in active
