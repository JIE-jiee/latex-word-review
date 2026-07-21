"""Vertical UI tests for saving an editable review Word copy."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from typing import cast

import pytest

import latex_word_review.app_server as app_server_module
from latex_word_review.app_server import LOOPBACK_HOST, AppHTTPServer, AppState
from latex_word_review.app_views import render_app_page
from latex_word_review.application import ApplicationSession
from latex_word_review.errors import ContractError, ErrorCode
from tests.test_app_server import _credentials, _post_form, _serving


def _session_key(character: str = "f") -> str:
    return f"session_{character * 32}"


def _create_session_boundary(state: AppState, session_key: str) -> Path:
    session_root = state.runs_root / session_key
    session_root.mkdir()
    return session_root


class _SuccessfulCopySession:
    def __init__(self, payload: bytes = b"editable-review-copy") -> None:
        self.payload = payload
        self.destinations: list[Path] = []

    def save_review_copy(self, destination: Path) -> Path:
        self.destinations.append(destination)
        destination.write_bytes(self.payload)
        return destination.resolve()


class _RejectingCopySession:
    def save_review_copy(self, _destination: Path) -> Path:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output path already exists")


def _as_application_session(value: object) -> ApplicationSession:
    return cast("ApplicationSession", value)


def test_waiting_page_save_form_never_accepts_a_browser_path() -> None:
    session_key = _session_key()
    view: dict[str, object] = {
        "page": "waiting_word",
        "step": 2,
        "project_name": "paper",
        "form_fields": {"csrf": "csrf-token", "session": session_key},
        "exit_form_fields": {"csrf": "csrf-token"},
        "notices": [],
        "review_docx_name": "review.docx",
        "exported_at": "2026-07-19T00:00:00Z",
        "open_action": "/session/open-review-docx",
        "save_copy_action": "/session/save-review-copy",
        "receive_action": "/session/receive",
    }

    page = render_app_page(view)
    save_form = page.split('action="/session/save-review-copy"', 1)[1].split("</form>", 1)[0]

    assert "另存可编辑 Word" in save_form
    assert 'name="csrf" value="csrf-token"' in save_form
    assert f'name="session" value="{session_key}"' in save_form
    assert 'name="path"' not in save_form
    assert 'name="destination"' not in save_form
    assert 'type="file"' not in save_form
    assert "程序不会覆盖已有文件" in page


def test_app_state_cancel_has_no_copy_or_success_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker_calls: list[Path | None] = []

    def cancel(initial: Path | None) -> Path | None:
        picker_calls.append(initial)
        return None

    state = AppState(tmp_path / "app", review_copy_picker=cancel)
    session_key = _session_key("a")
    _create_session_boundary(state, session_key)
    previous_output = tmp_path / "previous-output"
    state._last_output_directory = previous_output

    def unexpected_action(_session_key: str) -> ApplicationSession:
        raise AssertionError("cancel must not construct an action session")

    monkeypatch.setattr(state, "_action_session", unexpected_action)
    try:
        assert state.save_review_copy(session_key) is False
    finally:
        state.close()

    assert picker_calls == [previous_output]
    assert state._last_output_directory == previous_output


def test_app_state_success_reuses_only_output_directory_and_emits_one_safe_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "chosen-output"
    output_directory.mkdir()
    destination = output_directory / "advisor-copy.docx"
    picker_initials: list[Path | None] = []

    def choose(initial: Path | None) -> Path:
        picker_initials.append(initial)
        return destination

    state = AppState(tmp_path / "app", review_copy_picker=choose)
    session_key = _session_key("b")
    _create_session_boundary(state, session_key)
    copy_session = _SuccessfulCopySession()
    monkeypatch.setattr(
        state,
        "_action_session",
        lambda _key: _as_application_session(copy_session),
    )
    monkeypatch.setattr(state, "load_session", lambda _key: object())
    monkeypatch.setattr(
        app_server_module,
        "render_session_view",
        lambda *_args, **_kwargs: {"page": "waiting_word", "notices": []},
    )

    try:
        assert state.save_review_copy(session_key) is True
        first_view = state.session_view(session_key)
        second_view = state.session_view(session_key)
    finally:
        state.close()

    assert picker_initials == [None]
    assert copy_session.destinations == [destination]
    assert destination.read_bytes() == copy_session.payload
    assert state._last_output_directory == output_directory
    notices = cast("list[dict[str, str]]", first_view["notices"])
    assert notices[0]["title"] == "审阅 Word 副本已保存"
    assert "已另存副本：advisor-copy.docx" in notices[0]["message"]
    assert str(output_directory) not in repr(first_view)
    assert second_view["notices"] == []


def test_app_state_existing_target_error_preserves_user_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "chosen-output"
    output_directory.mkdir()
    destination = output_directory / "existing.docx"
    destination.write_bytes(b"keep-user-content")
    state = AppState(
        tmp_path / "app",
        review_copy_picker=lambda _initial: destination,
    )
    session_key = _session_key("c")
    _create_session_boundary(state, session_key)
    previous_output = tmp_path / "previous-output"
    state._last_output_directory = previous_output
    monkeypatch.setattr(
        state,
        "_action_session",
        lambda _key: _as_application_session(_RejectingCopySession()),
    )

    try:
        with pytest.raises(ContractError) as captured:
            state.save_review_copy(session_key)
    finally:
        state.close()

    assert captured.value.code is ErrorCode.SCHEMA_INVALID
    assert destination.read_bytes() == b"keep-user-content"
    assert state._last_output_directory == previous_output


def test_job_started_while_save_dialog_is_open_blocks_the_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "chosen-output"
    output_directory.mkdir()
    destination = output_directory / "must-not-be-created.docx"
    picker_opened = threading.Event()
    release_picker = threading.Event()
    job_started = threading.Event()
    release_job = threading.Event()

    def delayed_choice(_initial: Path | None) -> Path:
        picker_opened.set()
        assert release_picker.wait(timeout=5)
        return destination

    state = AppState(
        tmp_path / "app",
        review_copy_picker=delayed_choice,
        max_workers=1,
    )
    session_key = _session_key("e")
    _create_session_boundary(state, session_key)
    copy_session = _SuccessfulCopySession()
    monkeypatch.setattr(
        state,
        "_action_session",
        lambda _key: _as_application_session(copy_session),
    )

    def background_job(_progress: object) -> dict[str, object]:
        job_started.set()
        assert release_job.wait(timeout=5)
        return {}

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            save_future = executor.submit(state.save_review_copy, session_key)
            assert picker_opened.wait(timeout=5)
            job = state._submit_session_job(
                session_key,
                "receive_review",
                background_job,
            )
            assert job_started.wait(timeout=5)
            release_picker.set()
            with pytest.raises(ContractError) as captured:
                save_future.result(timeout=5)
            assert captured.value.code is ErrorCode.SCHEMA_INVALID
            release_job.set()
            assert state.jobs.wait(job.job_id, timeout=5).status == "succeeded"
    finally:
        release_picker.set()
        release_job.set()
        state.close()

    assert copy_session.destinations == []
    assert not destination.exists()


def test_http_route_accepts_only_csrf_and_session_not_a_destination(tmp_path: Path) -> None:
    picker_calls: list[Path | None] = []

    def cancel(initial: Path | None) -> Path | None:
        picker_calls.append(initial)
        return None

    state = AppState(tmp_path / "app", review_copy_picker=cancel)
    session_key = _session_key("d")
    _create_session_boundary(state, session_key)
    server = AppHTTPServer((LOOPBACK_HOST, 0), state)

    with _serving(server):
        cookie, csrf, _body, _headers = _credentials(server)
        cancelled = _post_form(
            server,
            "/session/save-review-copy",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        injected = _post_form(
            server,
            "/session/save-review-copy",
            {
                "csrf": csrf,
                "session": session_key,
                "destination": str(tmp_path / "browser-must-not-control.docx"),
            },
            cookie=cookie,
        )

    assert cancelled[0] == HTTPStatus.SEE_OTHER
    assert cancelled[1]["location"] == f"/session/{session_key}"
    assert injected[0] == HTTPStatus.BAD_REQUEST
    assert picker_calls == [None]
    assert not (tmp_path / "browser-must-not-control.docx").exists()
